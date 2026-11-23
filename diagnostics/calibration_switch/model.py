"""Deterministic local model of the A + calibration combine_latest topology.

The current streamz implementation is used unchanged.  The model adds only:

* a controlled gate between source B and the merge node,
* records that carry an independent RefCounter and acknowledgement callback,
* an instrumented merge node that records internal state transitions.

Every traced event has a scope.  ``field`` events are the only ones a field
deployment retains (source read, switch request, write, acknowledgement);
``local`` events are library-internal and exist only in this reproduction.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from typing import Any

from streamz import RefCounter, Stream
from streamz.core import combine_latest


REQUEST_ID = "SW-1"
GATE_NAME = "B@merge"


class ManualLoop:
    """Run RefCounter callbacks only when the scenario explicitly drains."""

    def __init__(self):
        self._callbacks = deque()

    def add_callback(self, callback, *args, **kwargs):
        self._callbacks.append((callback, args, kwargs))

    def drain(self):
        while self._callbacks:
            callback, args, kwargs = self._callbacks.popleft()
            callback(*args, **kwargs)


@dataclass
class Record:
    rid: str
    source: str
    value: Any
    ref: RefCounter | None = None
    ack: bool = False
    ack_count: int = 0
    ack_t: int | None = None


class Trace:
    """Collect field and local evidence on one shared logical clock."""

    def __init__(self):
        self.loop = ManualLoop()
        self.clock = 0
        self.field: list[dict[str, Any]] = []
        self.local: list[dict[str, Any]] = []
        self.records: dict[str, Record] = {}
        self.db: list[dict[str, Any]] = []

    def _tick(self):
        self.clock += 1
        return self.clock

    def field_event(self, kind, **payload):
        event = {"t": self._tick(), "scope": "field", "kind": kind}
        event.update(payload)
        self.field.append(event)
        return event

    def local_event(self, kind, **payload):
        event = {"t": self._tick(), "scope": "local", "kind": kind}
        event.update(payload)
        self.local.append(event)
        return event

    def drain(self):
        self.loop.drain()

    def new_record(self, source, rid, value):
        record = Record(rid=rid, source=source, value=value)
        record.ref = RefCounter(
            initial=0, cb=lambda: self._ack(record), loop=self.loop
        )
        self.records[rid] = record
        return record

    def _ack(self, record):
        record.ack_count += 1
        if record.ack:
            return
        record.ack = True
        event = self.field_event("ack", source=record.source, rid=record.rid)
        record.ack_t = event["t"]
        self.local_event(
            "ack_local",
            rid=record.rid,
            count=record.ref.count,
            ack_count=record.ack_count,
        )


class SourceStream(Stream):
    def __init__(self, name, trace):
        super().__init__(stream_name=name)
        self.trace = trace

    def feed(self, rid, value):
        record = self.trace.new_record(self.name, rid, value)
        self.trace.field_event("source_read", source=self.name, rid=rid)
        try:
            self.emit(
                value,
                metadata=[
                    {"ref": record.ref, "rid": rid, "source": self.name}
                ],
            )
        except Exception as exc:
            self.trace.local_event(
                "source_emit_error",
                source=self.name,
                rid=rid,
                rids=[rid],
                error=type(exc).__name__,
                message=str(exc),
            )
        finally:
            self.trace.drain()
        return record


class Gate(Stream):
    """Controlled transport buffer between source B and the merge node."""

    def __init__(self, upstream, name, trace):
        super().__init__(upstream, stream_name=name)
        self.trace = trace
        self.held = deque()
        self.holds: list[dict[str, Any]] = []
        self.releases: list[dict[str, Any]] = []

    def update(self, x, who=None, metadata=None):
        self._retain_refs(metadata)
        rids = [m.get("rid") for m in metadata or []]
        counts = [m["ref"].count for m in metadata or [] if "ref" in m]
        self.held.append((x, metadata))
        hold = {"rids": rids, "counts": counts}
        self.holds.append(hold)
        self.trace.local_event(
            "transport_hold",
            gate=self.name,
            rids=rids,
            counts=counts,
            downstreams=[getattr(d, "name", None) for d in self.downstreams],
        )
        return []

    def release_one(self):
        if not self.held:
            self.trace.local_event("transport_release_empty", gate=self.name)
            return None
        x, metadata = self.held.popleft()
        rids = [m.get("rid") for m in metadata or []]
        downstreams = [getattr(d, "name", None) for d in self.downstreams]
        self.trace.local_event(
            "transport_release_begin",
            gate=self.name,
            rids=rids,
            downstreams=downstreams,
        )
        error = None
        try:
            self._emit(x, metadata=metadata)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            self.trace.local_event(
                "transport_release_error",
                gate=self.name,
                rids=rids,
                error=type(exc).__name__,
                message=str(exc),
            )
        finally:
            self._release_refs(metadata)
            self.trace.drain()
        self.releases.append(
            {"rids": rids, "downstreams": downstreams, "error": error}
        )
        self.trace.local_event(
            "transport_release_end",
            gate=self.name,
            rids=rids,
            downstreams=downstreams,
            error=error,
        )
        return None

    def held_rids(self):
        return [
            m.get("rid") for _, metadata in self.held for m in metadata or []
        ]


class MergeNode(combine_latest):
    """Instrument combine_latest without changing its behaviour."""

    def __init__(self, *upstreams, trace, **kwargs):
        self.trace = trace
        self.attempts: list[dict[str, Any]] = []
        super().__init__(*upstreams, **kwargs)

    def snapshot(self):
        return {
            "upstreams": [getattr(u, "name", None) for u in self.upstreams],
            "last": list(self.last),
            "last_present": [value is not None for value in self.last],
            "metadata_rids": [
                [m.get("rid") for m in md] if md else None
                for md in self.metadata
            ],
            "missing": [getattr(u, "name", None) for u in self.missing],
            "emit_on": [getattr(u, "name", None) for u in self.emit_on],
            "slot_count": len(self.last),
            "upstream_count": len(self.upstreams),
            "consistent": (
                len(self.upstreams) == len(self.last) == len(self.metadata)
            ),
        }

    def update(self, x, who=None, metadata=None):
        rids = [m.get("rid") for m in metadata or []]
        self.trace.local_event(
            "merge_update_begin",
            who=getattr(who, "name", None),
            rids=rids,
            **self.snapshot(),
        )
        attempt = {
            "rids": rids,
            "who": getattr(who, "name", None),
            "ok": True,
            "error": None,
        }
        try:
            result = super().update(x, who=who, metadata=metadata)
        except Exception as exc:
            attempt["ok"] = False
            attempt["error"] = f"{type(exc).__name__}: {exc}"
            self.attempts.append(attempt)
            self.trace.local_event(
                "merge_update_error",
                who=getattr(who, "name", None),
                rids=rids,
                error=type(exc).__name__,
                message=str(exc),
                **self.snapshot(),
            )
            raise
        self.attempts.append(attempt)
        self.trace.local_event(
            "merge_update_end",
            who=getattr(who, "name", None),
            rids=rids,
            **self.snapshot(),
        )
        return result


class Database(Stream):
    def __init__(self, upstream, trace):
        super().__init__(upstream, stream_name="DB")
        self.trace = trace

    def update(self, x, who=None, metadata=None):
        rids = [m.get("rid") for m in metadata or []]
        row = {
            "values": x,
            "rids": rids,
            "arity": len(x) if isinstance(x, tuple) else None,
        }
        self.trace.db.append(row)
        self.trace.field_event("db_write", rids=rids, arity=row["arity"])
        self.trace.local_event(
            "db_write_local", values=x, rids=rids, arity=row["arity"]
        )
        return []


class Bench:
    def __init__(self):
        self.trace = Trace()
        self.a = SourceStream("A", self.trace)
        self.b = SourceStream("B", self.trace)
        self.c = SourceStream("C", self.trace)
        self.gate = Gate(self.b, GATE_NAME, self.trace)
        self.merge = MergeNode(
            self.a,
            self.gate,
            emit_on=(self.a,),
            trace=self.trace,
            stream_name="merge",
        )
        self.db = Database(self.merge, self.trace)

    def topology(self):
        snapshot = self.merge.snapshot()
        snapshot["gate_held_rids"] = self.gate.held_rids()
        return snapshot

    def boundary(self, kind, case):
        self.trace.local_event(kind, case=case, **self.topology())

    def switch(self, request=REQUEST_ID, on_error="continue"):
        self.trace.field_event("switch_request", request=request)
        self.trace.local_event(
            "switch_begin", on_error=on_error, **self.topology()
        )
        result = {"ok": True, "stage": None, "error": None}
        try:
            self.gate.disconnect(self.merge)
        except Exception as exc:
            result = {
                "ok": False,
                "stage": "disconnect",
                "error": f"{type(exc).__name__}: {exc}",
            }
            self.trace.local_event(
                "disconnect_error",
                error=type(exc).__name__,
                message=str(exc),
                **self.topology(),
            )
            if on_error == "abort":
                self.trace.local_event("switch_abort", **self.topology())
                return result
        else:
            self.trace.local_event("disconnect_ok", **self.topology())

        try:
            self.c.connect(self.merge)
        except Exception as exc:
            result = {
                "ok": False,
                "stage": "connect",
                "error": f"{type(exc).__name__}: {exc}",
            }
            self.trace.local_event(
                "connect_error",
                error=type(exc).__name__,
                message=str(exc),
                **self.topology(),
            )
        else:
            self.trace.local_event("connect_ok", **self.topology())

        self.trace.local_event("switch_end", result=result, **self.topology())
        return result


def _case_result(bench, case, switch):
    return {"bench": bench, "switch": switch, "case": case}


def run_transit(on_error="continue"):
    """B1 is read but the gate holds it before the merge node."""
    bench = Bench()
    bench.a.feed("A1", 10)
    bench.b.feed("B1", 7)
    bench.boundary("boundary_before_switch", "transit")
    switch = bench.switch(on_error=on_error)
    bench.boundary("boundary_after_switch", "transit")
    bench.c.feed("C1", 7)
    bench.boundary("boundary_after_c1", "transit")
    bench.a.feed("A2", 20)
    bench.boundary("boundary_after_a2", "transit")
    # The late old-B record in this round is the B1 still held by the gate.
    bench.gate.release_one()
    bench.boundary("boundary_final", "transit")
    return _case_result(bench, "transit", switch)


def run_merged(on_error="continue"):
    """B1 has reached the merge node before the switch."""
    case = "merged_continue" if on_error == "continue" else "merged_abort"
    bench = Bench()
    bench.a.feed("A1", 10)
    bench.b.feed("B1", 7)
    bench.gate.release_one()
    bench.boundary("boundary_before_switch", case)
    switch = bench.switch(on_error=on_error)
    bench.boundary("boundary_after_switch", case)
    bench.c.feed("C1", 7)
    bench.boundary("boundary_after_c1", case)
    bench.a.feed("A2", 20)
    bench.boundary("boundary_after_a2", case)
    # The late old-B record in this round is a fresh B2 at the gate.
    bench.b.feed("B2", 7)
    bench.gate.release_one()
    bench.boundary("boundary_final", case)
    return _case_result(bench, case, switch)


def run_control():
    """No switch: B2 legitimately replaces the old calibration value."""
    bench = Bench()
    bench.a.feed("A1", 10)
    bench.b.feed("B1", 7)
    bench.gate.release_one()
    bench.b.feed("B2", 7)
    bench.gate.release_one()
    bench.boundary("boundary_after_replace", "control")
    bench.a.feed("A2", 20)
    bench.boundary("boundary_final", "control")
    return _case_result(bench, "control", None)


def run_reversed():
    """Same topology with B1 arriving before A1."""
    bench = Bench()
    bench.b.feed("B1", 7)
    bench.gate.release_one()
    bench.a.feed("A1", 10)
    bench.boundary("boundary_after_a1", "reversed")
    switch = bench.switch(on_error="continue")
    bench.c.feed("C1", 7)
    bench.a.feed("A2", 20)
    bench.b.feed("B2", 7)
    bench.gate.release_one()
    bench.boundary("boundary_final", "reversed")
    return _case_result(bench, "reversed", switch)


def run_all():
    return {
        "transit": run_transit(),
        "merged_continue": run_merged("continue"),
        "merged_abort": run_merged("abort"),
        "control": run_control(),
        "reversed": run_reversed(),
    }


def _db_rows_by_rid(trace):
    rows = {}
    for index, row in enumerate(trace.db):
        for rid in row["rids"]:
            rows.setdefault(rid, []).append(index)
    return rows


def _merge_slots_by_rid(merge):
    slots = {}
    for index, metadata in enumerate(merge.metadata):
        for item in metadata or []:
            rid = item.get("rid")
            if rid is not None:
                slots.setdefault(rid, []).append(index)
    return slots


def record_rows(bench):
    """Classify every record by evidence, never by reference count alone."""
    trace = bench.trace
    gate_now = set(bench.gate.held_rids())
    gate_ever = {rid for hold in bench.gate.holds for rid in hold["rids"]}
    released = {rid for item in bench.gate.releases for rid in item["rids"]}
    slots = _merge_slots_by_rid(bench.merge)
    db_rows = _db_rows_by_rid(trace)
    attempts = {}
    for attempt in bench.merge.attempts:
        for rid in attempt["rids"]:
            attempts.setdefault(rid, []).append(attempt)

    rows = []
    for rid, record in trace.records.items():
        rid_attempts = attempts.get(rid, [])
        rejected = any(not item["ok"] for item in rid_attempts)
        in_transport = rid in gate_now
        in_slot = bool(slots.get(rid))
        ref = record.ref.count

        if in_transport:
            state = "transport"
        elif in_slot:
            state = "merge_slot"
        elif record.ack:
            state = "settled"
        elif rejected and ref > 0:
            state = "merge_rejected"
        elif ref > 0:
            state = "orphaned_ref"
        else:
            state = "unadmitted_zero_ref"

        rows.append(
            {
                "rid": rid,
                "source": record.source,
                "value_local": record.value,
                "ref_count_local": ref,
                "ack": record.ack,
                "ack_count_local": record.ack_count,
                "ack_without_merge": _ack_without_merge(bench, rid),
                "gate_now": in_transport,
                "gate_ever": rid in gate_ever,
                "left_gate": rid in released,
                "merge_attempted": bool(rid_attempts),
                "merge_rejected": rejected,
                "merge_slots_local": slots.get(rid, []),
                "db_rows": db_rows.get(rid, []),
                "state": state,
            }
        )
    return rows


def _ack_without_merge(bench, rid):
    """True when the gate released this record with no downstream.

    A RefCounter callback still fires in that case, so the acknowledgement
    proves release, not admission into the merge node.
    """
    for release in bench.gate.releases:
        if rid in release["rids"]:
            return not release["downstreams"]
    return False


def _compact(event, keys):
    return {key: event.get(key) for key in keys}


TOPOLOGY_KEYS = (
    "upstreams",
    "last_present",
    "metadata_rids",
    "missing",
    "emit_on",
    "slot_count",
    "upstream_count",
    "consistent",
)


def state_after_switch(result):
    events = result["bench"].trace.local
    before = next((e for e in events if e["kind"] == "switch_begin"), None)
    after = next(
        (e for e in events if e["kind"] in ("switch_end", "switch_abort")),
        None,
    )
    if before is None or after is None:
        return None
    delta = {}
    for key in ("upstreams", "last_present", "metadata_rids", "missing",
                "emit_on", "consistent"):
        if before.get(key) != after.get(key):
            delta[key] = {"before": before.get(key), "after": after.get(key)}
    switch = result["switch"]
    return {
        "before": _compact(before, TOPOLOGY_KEYS),
        "after": _compact(after, TOPOLOGY_KEYS),
        "delta": delta,
        "error": switch["error"] if switch else None,
    }


def field_prefix_before_switch(bench):
    prefix = []
    for event in bench.trace.field:
        if event["kind"] == "switch_request":
            break
        prefix.append(
            {k: v for k, v in event.items() if k != "t"}
        )
    return prefix


COMPARABLE_RIDS = ("A1", "B1", "C1", "A2")
def comparable_observations(bench):
    """Field evidence restricted to records present in both rounds."""
    reads = [
        {"source": event["source"], "rid": event["rid"]}
        for event in bench.trace.field
        if event["kind"] == "source_read"
        and event["rid"] in COMPARABLE_RIDS
    ]
    acks = sorted(
        event["rid"]
        for event in bench.trace.field
        if event["kind"] == "ack" and event["rid"] in COMPARABLE_RIDS
    )
    writes = [
        {"rids": event["rids"], "arity": event["arity"]}
        for event in bench.trace.field
        if event["kind"] == "db_write"
        and all(rid in COMPARABLE_RIDS for rid in event["rids"])
    ]
    return {"reads": reads, "acks": acks, "writes": writes}


def field_divergence(transit, merged):
    """Compare field evidence after the switch, independent of callback order.

    Acknowledgements are compared as a set, writes as result shapes, and
    records introduced only by one round (B2) are excluded so the comparison
    stays like-for-like.
    """
    left = comparable_observations(transit["bench"])
    right = comparable_observations(merged["bench"])
    return {
        key: {"transit": left[key], "merged": right[key]}
        for key in ("reads", "acks", "writes")
        if left[key] != right[key]
    }


def earliest_field_consequence(bench):
    """Earliest field-visible consequence of A2 meeting the merge node.

    Returns the first write containing A2, or None when A2 never reaches the
    database.  Acknowledgement order is deliberately ignored.
    """
    seen_a2 = False
    for event in bench.trace.field:
        if event["kind"] == "source_read" and event.get("rid") == "A2":
            seen_a2 = True
        elif (
            seen_a2
            and event["kind"] == "db_write"
            and "A2" in event["rids"]
        ):
            return {"rids": event["rids"], "arity": event["arity"]}
    return None


def summarize(result):
    bench = result["bench"]
    field_prefix = field_prefix_before_switch(bench)
    return {
        "case": result["case"],
        "switch": result["switch"],
        "field": [
            {k: v for k, v in event.items() if k != "scope"}
            for event in bench.trace.field
        ],
        "local": bench.trace.local,
        "topology": bench.topology(),
        "records": record_rows(bench),
        "db": [
            {
                "rids": row["rids"],
                "arity": row["arity"],
                "values_local": row["values"],
            }
            for row in bench.trace.db
        ],
        "field_prefix_before_switch": field_prefix,
        "switch_state": state_after_switch(result),
        "earliest_a2_write": earliest_field_consequence(bench),
    }
