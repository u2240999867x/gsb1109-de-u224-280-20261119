"""Persistent, attempt-aware bookkeeping for remote job dispatch.

The classes in this module separate three concerns that are otherwise easy to
mix up:

* **selection** (which jobs are dispatched next) - see
  :mod:`snakemake.scheduling.deadline`,
* **resource admission** (does a job fit into the currently free memory), and
* **submission permits** (how many remote submissions were actually accepted in
  a sliding time window).

The latter two are persisted, because a scheduler process may be restarted while
a remote executor keeps running.  The ledger records *attempts* rather than just
jobs: a stale failure notification for attempt 1 must not free the memory of
attempt 2, and a repeated completion notification must not free memory twice.

A submission permit is held as soon as a remote submission is planned
(``reserved``).  It is only converted into a consumed permit once the executor
confirms acceptance.  If the scheduler restarts before it could record an
acceptance, the reservation remains in place and is reconciled against the
executor's receipts.  Unknown reservations are kept (not released, not
resubmitted) until the executor provides evidence.
"""

from __future__ import annotations

import json
import os
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence, Union

from snakemake.scheduling.deadline import (
    DeadlineRegistry,
    dispatch_sort_key,
    parse_absolute_time,
)

# Entry states
RESERVED = "reserved"
ACCEPTED = "accepted"
UNKNOWN = "unknown"
FINISHED = "finished"
FAILED = "failed"
RELEASED = "released"

ACTIVE_STATES = frozenset({RESERVED, ACCEPTED, UNKNOWN})
RUNNING_STATES = frozenset({ACCEPTED})
FINAL_STATES = frozenset({FINISHED, FAILED, RELEASED})


def is_group(job: Any) -> bool:
    value = getattr(job, "is_group", None)
    if callable(value):
        try:
            return bool(value())
        except TypeError:
            return bool(value)
    return value is True


def job_members(job: Any) -> Sequence[Any]:
    if not is_group(job):
        return (job,)
    jobs = getattr(job, "jobs", None)
    if callable(jobs):
        jobs = jobs()
    return tuple(jobs or ())


def job_identity(job: Any) -> str:
    """Return a stable identity for a single job or a group job.

    Group ids (``job.group``) are user-provided and may be shared by several
    group jobs, so the identity of a group job is derived from its member jobs.
    ``GroupJobFactory`` keeps group objects stable for as long as the DAG keeps
    the same members, hence a content-based identity survives restarts.
    """
    if is_group(job):
        members = job_members(job)
        if members:
            return "group:" + "+".join(sorted(job_identity(m) for m in members))
        return f"group:{getattr(job, 'groupid', '')}"
    uuid_value = getattr(job, "uuid", None)
    if callable(uuid_value):
        try:
            return f"job:{uuid_value()}"
        except TypeError:
            pass
    rule = getattr(getattr(job, "rule", None), "name", getattr(job, "name", ""))
    wildcards = getattr(job, "wildcards_dict", None) or {}
    pairs = ",".join(f"{k}={v}" for k, v in sorted(wildcards.items()))
    return f"job:{rule}:{pairs}"


def job_attempt(job: Any) -> int:
    attempt = getattr(job, "attempt", 1)
    if callable(attempt):
        attempt = attempt()
    try:
        return int(attempt)
    except (TypeError, ValueError):
        return 1


def job_is_remote(job: Any) -> bool:
    """Whether the job is submitted to the remote execution end.

    Local rules are executed by the local executor even in remote mode and are
    therefore not counted against remote submission permits.
    """
    is_local = getattr(job, "is_local", False)
    if callable(is_local):
        try:
            is_local = is_local()
        except TypeError:
            pass
    return not bool(is_local)


def job_resources(job: Any) -> dict[str, int]:
    resources = getattr(job, "scheduler_resources", None)
    if callable(resources):
        resources = resources()
    result: dict[str, int] = {}
    for name, value in (resources or {}).items():
        if isinstance(value, str) or name == "_job_count":
            continue
        try:
            result[str(name)] = int(value)
        except (TypeError, ValueError):
            continue
    return result


def job_memory(job: Any) -> int:
    """Return the job's scheduled ``mem_mb`` footprint (0 if not declared)."""
    return job_resources(job).get("mem_mb", 0)


def job_sort_description(job: Any) -> dict[str, Any]:
    if is_group(job):
        return {
            "group": str(getattr(job, "groupid", "")),
            "members": [job_sort_description(member) for member in job_members(job)],
        }
    rule = getattr(getattr(job, "rule", None), "name", getattr(job, "name", ""))
    wildcards = getattr(job, "wildcards_dict", None)
    if wildcards is None:
        wildcards = getattr(job, "wildcards", None) or {}
    try:
        wildcards = dict(wildcards.items())
    except AttributeError:
        wildcards = dict(wildcards) if isinstance(wildcards, Mapping) else {}
    return {
        "rule": str(rule),
        "wildcards": {str(k): str(v) for k, v in sorted(wildcards.items())},
    }


@dataclass
class DispatchReceipt:
    """A statement of the executor about one planned submission."""

    key: str
    attempt: int
    accepted_at: Optional[float] = None
    state: str = "running"
    mem_mb: Optional[int] = None
    transaction_id: Optional[str] = None


@dataclass
class DispatchStatus:
    """Result of asking the executor about pending submissions.

    ``receipts`` describe submissions the executor knows about.
    ``not_accepted`` lists planned submissions the executor definitively did not
    accept.  Everything else that was queried but not mentioned is considered
    unknown and keeps its reservation.
    """

    receipts: list[DispatchReceipt] = field(default_factory=list)
    not_accepted: list[tuple[str, int]] = field(default_factory=list)

    @classmethod
    def from_any(cls, value: Any) -> "DispatchStatus":
        if value is None:
            return cls()
        if isinstance(value, cls):
            return value
        if isinstance(value, Mapping):
            receipts = [
                _coerce_receipt(item) for item in value.get("receipts", ())
            ]
            not_accepted = [
                _coerce_key_attempt(item) for item in value.get("not_accepted", ())
            ]
            return cls(receipts=receipts, not_accepted=not_accepted)
        receipts = [_coerce_receipt(item) for item in value]
        return cls(receipts=receipts)


def _coerce_key_attempt(value: Any) -> tuple[str, int]:
    if isinstance(value, DispatchReceipt):
        return (value.key, int(value.attempt))
    if isinstance(value, str):
        # "key@attempt"
        if "@" in value:
            key, attempt = value.rsplit("@", 1)
            return (key, int(attempt))
        return (value, 1)
    if isinstance(value, Mapping):
        return (str(value["key"]), int(value.get("attempt", 1)))
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return (str(value[0]), int(value[1]))
    raise TypeError(f"Cannot interpret pending submission reference: {value!r}")


def _coerce_receipt(value: Any) -> DispatchReceipt:
    if isinstance(value, DispatchReceipt):
        return value
    if isinstance(value, Mapping):
        state = str(value.get("state", "running")).lower()
        accepted_at = value.get("accepted_at", value.get("accepted"))
        if accepted_at is not None:
            accepted_at = parse_absolute_time(accepted_at)
        return DispatchReceipt(
            key=str(value["key"]),
            attempt=int(value.get("attempt", 1)),
            accepted_at=accepted_at,
            state=state,
            mem_mb=value.get("mem_mb"),
            transaction_id=value.get("transaction_id"),
        )
    raise TypeError(f"Cannot interpret dispatch receipt: {value!r}")


@dataclass
class DispatchEntry:
    """One planned remote submission (single job or group job)."""

    transaction_id: str
    key: str
    attempt: int
    mem_mb: int
    state: str = RESERVED
    reserved_at: float = 0.0
    accepted_at: Optional[float] = None
    finished_at: Optional[float] = None
    description: dict[str, Any] = field(default_factory=dict)
    note: Optional[str] = None
    resources: dict[str, int] = field(default_factory=dict)
    counts_permit: bool = True

    @property
    def active(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def consumes_memory(self) -> bool:
        return self.state in ACTIVE_STATES

    @property
    def consumes_permit(self) -> bool:
        # A reservation already holds a permit, so that the scheduler cannot
        # plan more submissions than the window allows.  Accepted entries are
        # counted by their actual acceptance time instead.
        return self.state in (RESERVED, UNKNOWN) or (
            self.state == ACCEPTED and self.accepted_at is not None
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "transaction_id": self.transaction_id,
            "key": self.key,
            "attempt": self.attempt,
            "mem_mb": self.mem_mb,
            "state": self.state,
            "reserved_at": self.reserved_at,
            "accepted_at": self.accepted_at,
            "finished_at": self.finished_at,
            "description": self.description,
            "note": self.note,
            "resources": self.resources,
            "counts_permit": self.counts_permit,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DispatchEntry":
        return cls(
            transaction_id=str(data["transaction_id"]),
            key=str(data["key"]),
            attempt=int(data["attempt"]),
            mem_mb=int(data.get("mem_mb", 0)),
            state=str(data.get("state", RESERVED)),
            reserved_at=float(data.get("reserved_at", 0.0)),
            accepted_at=data.get("accepted_at"),
            finished_at=data.get("finished_at"),
            description=dict(data.get("description") or {}),
            note=data.get("note"),
            resources={
                str(k): int(v)
                for k, v in (data.get("resources") or {}).items()
            },
            counts_permit=bool(data.get("counts_permit", True)),
        )


class DispatchLedger:
    """Restart-safe record of dispatch attempts, memory, and permits.

    The ledger stores every attempt even after it reaches a final state, which
    makes duplicate notifications easy to detect.  Only entries in
    :data:`ACTIVE_STATES` occupy memory.
    """

    def __init__(
        self,
        path: Optional[Union[str, Path]] = None,
        max_jobs_per_window: Optional[int] = None,
        window: float = 10.0,
        clock: Callable[[], float] = time.time,
    ):
        self.path = None if path is None else Path(path)
        self.max_jobs_per_window = max_jobs_per_window
        self.window = float(window)
        self.clock = clock
        self.entries: list[DispatchEntry] = []
        self._by_transaction: dict[str, DispatchEntry] = {}
        self._lock = threading.RLock()
        if self.path is not None and self.path.exists():
            self.load()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def load(self) -> None:
        with self._lock:
            data = json.loads(self.path.read_text())
            self.entries = [
                DispatchEntry.from_dict(item) for item in data.get("entries", ())
            ]
            self._by_transaction = {
                entry.transaction_id: entry for entry in self.entries
            }

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            payload = {
                "version": 1,
                "entries": [entry.to_dict() for entry in self.entries],
            }
            tmp = self.path.with_suffix(self.path.suffix + f".{os.getpid()}.tmp")
            tmp.write_text(json.dumps(payload, indent=2, sort_keys=True))
            os.replace(tmp, self.path)

    def forget_final(self) -> None:
        """Drop finalized entries from the ledger.

        Called once a workflow finished successfully, so that the ledger does
        not grow forever.  Only finalized entries are dropped; a tournament
        that is still running (or whose acceptance is still unknown) is kept.
        """
        with self._lock:
            self.entries = [entry for entry in self.entries if entry.active]
            self._by_transaction = {
                entry.transaction_id: entry for entry in self.entries
            }
        self.save()

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def entries_for(self, key: str) -> list[DispatchEntry]:
        return [entry for entry in self.entries if entry.key == key]

    def entry_by_transaction(self, transaction_id: str) -> Optional[DispatchEntry]:
        return self._by_transaction.get(transaction_id)

    def latest_for(self, key: str) -> Optional[DispatchEntry]:
        matching = self.entries_for(key)
        if not matching:
            return None
        # Among entries for the same attempt, the most recently recorded one is
        # the current plan (an earlier one may have been released as "not
        # accepted" and then planned again).
        return max(
            matching,
            key=lambda entry: (
                entry.attempt,
                entry.reserved_at,
                self.entries.index(entry),
            ),
        )

    def current_attempt(self, key: str) -> Optional[int]:
        matching = self.entries_for(key)
        return max((entry.attempt for entry in matching), default=None)

    def active_for(self, key: str) -> Optional[DispatchEntry]:
        for entry in reversed(self.entries):
            if entry.key == key and entry.active:
                return entry
        return None

    def active_entries(self) -> list[DispatchEntry]:
        return [entry for entry in self.entries if entry.active]

    def pending_entries(self) -> list[DispatchEntry]:
        return [entry for entry in self.entries if entry.state in (RESERVED, UNKNOWN)]

    def usage_in_use(self) -> dict[str, int]:
        """Sum of resources held by active entries, per resource name."""
        usage: dict[str, int] = {}
        for entry in self.entries:
            if not entry.consumes_memory:
                continue
            resources = entry.resources or {"mem_mb": entry.mem_mb}
            for name, value in resources.items():
                usage[name] = usage.get(name, 0) + value
        return usage

    def memory_in_use(self) -> int:
        return self.usage_in_use().get("mem_mb", 0)

    def accepted_in_window(self, now: Optional[float] = None) -> list[DispatchEntry]:
        now = self.clock() if now is None else now
        lower = now - self.window
        return [
            entry
            for entry in self.entries
            if entry.counts_permit
            and entry.state in (ACCEPTED, FINISHED, FAILED)
            and entry.accepted_at is not None
            and entry.accepted_at > lower
        ]

    def held_permits(self, now: Optional[float] = None) -> int:
        now = self.clock() if now is None else now
        accepted = len(self.accepted_in_window(now))
        reserved = len(
            [
                entry
                for entry in self.entries
                if entry.counts_permit and entry.state in (RESERVED, UNKNOWN)
            ]
        )
        return accepted + reserved

    def available_permits(self, now: Optional[float] = None) -> Optional[int]:
        if self.max_jobs_per_window is None:
            return None
        return max(self.max_jobs_per_window - self.held_permits(now), 0)

    def time_until_permit(self, now: Optional[float] = None) -> float:
        if self.max_jobs_per_window is None:
            return self.window
        now = self.clock() if now is None else now
        in_window = sorted(
            entry.accepted_at
            for entry in self.accepted_in_window(now)
            if entry.accepted_at is not None
        )
        excess = len(in_window) + len(
            [e for e in self.entries if e.state in (RESERVED, UNKNOWN)]
        ) - self.max_jobs_per_window
        if excess <= 0 or not in_window:
            return self.window
        # The oldest accepted entry has to leave the window.
        return max(in_window[0] + self.window - now, 0.0)

    # ------------------------------------------------------------------
    # mutations
    # ------------------------------------------------------------------
    def reserve(
        self,
        job: Any,
        now: Optional[float] = None,
        transaction_id: Optional[str] = None,
    ) -> DispatchEntry:
        now = self.clock() if now is None else now
        key = job_identity(job)
        attempt = job_attempt(job)
        with self._lock:
            active = self.active_for(key)
            if active is not None:
                if active.attempt == attempt:
                    return active
                raise ValueError(
                    f"Cannot plan attempt {attempt} for {key}: attempt "
                    f"{active.attempt} is still active."
                )
            transaction_id = transaction_id or uuid.uuid4().hex
            entry = DispatchEntry(
                transaction_id=transaction_id,
                key=key,
                attempt=attempt,
                mem_mb=job_memory(job),
                state=RESERVED,
                reserved_at=now,
                description=job_sort_description(job),
                resources=job_resources(job),
                counts_permit=job_is_remote(job),
            )
            self.entries.append(entry)
            self._by_transaction[transaction_id] = entry
        self.save()
        return entry

    def accept(
        self,
        transaction_id: str,
        accepted_at: Optional[float] = None,
    ) -> Optional[DispatchEntry]:
        accepted_at = self.clock() if accepted_at is None else accepted_at
        with self._lock:
            entry = self._by_transaction.get(transaction_id)
            if entry is None:
                return None
            if entry.state in FINAL_STATES:
                return entry
            # If it became accepted already, keep the original acceptance time.
            # This is what makes recovered receipts idempotent: B keeps the time
            # at which the executor first accepted it, not the recovery time.
            if entry.state == ACCEPTED and entry.accepted_at is not None:
                return entry
            entry.state = ACCEPTED
            entry.accepted_at = accepted_at
            entry.note = None
        self.save()
        return entry

    def mark_unknown(self, transaction_id: str, note: Optional[str] = None) -> None:
        with self._lock:
            entry = self._by_transaction.get(transaction_id)
            if entry is None or entry.state in FINAL_STATES:
                return
            entry.state = UNKNOWN
            entry.note = note
        self.save()

    def release(self, transaction_id: str, note: Optional[str] = None) -> bool:
        """Definitively mark a planned submission as not accepted."""
        with self._lock:
            entry = self._by_transaction.get(transaction_id)
            if entry is None or entry.state in FINAL_STATES:
                return False
            entry.state = RELEASED
            entry.finished_at = self.clock()
            entry.note = note or "not accepted by executor"
        self.save()
        return True

    def finish(
        self,
        key: str,
        attempt: Optional[int] = None,
        now: Optional[float] = None,
        state: str = FINISHED,
        allow_stale: bool = False,
    ) -> bool:
        """Mark the given attempt as finished/failed.

        Returns ``True`` only if this call changed an active entry.  Repeated
        notifications and notifications for superseded attempts are ignored.
        """
        now = self.clock() if now is None else now
        with self._lock:
            if attempt is None:
                active = self.active_for(key)
                if active is None:
                    return False
                attempt = active.attempt
            current = self.current_attempt(key)
            if current is not None and attempt < current and not allow_stale:
                # A stale notification for an old attempt; never free the
                # memory of the current attempt.
                return False
            matching = [
                entry
                for entry in self.entries
                if entry.key == key and entry.attempt == attempt
            ]
            changed = False
            for entry in matching:
                if entry.state in (ACCEPTED, RESERVED, UNKNOWN):
                    if entry.accepted_at is None:
                        # A completion is proof that the submission was accepted
                        # even if the acceptance callback was not observed (e.g.
                        # a fast local job or a lost callback).
                        entry.accepted_at = now
                    entry.state = state
                    entry.finished_at = now
                    changed = True
            if changed:
                self.save()
            return changed

    def can_complete(self, key: str, attempt: int) -> bool:
        current = self.current_attempt(key)
        if current is not None and attempt < current:
            return False
        active = self.active_for(key)
        return active is not None and active.attempt == attempt

    # ------------------------------------------------------------------
    # recovery
    # ------------------------------------------------------------------
    def reconcile(
        self,
        status: DispatchStatus,
        now: Optional[float] = None,
        pending: Optional[Iterable[DispatchEntry]] = None,
    ) -> dict[str, list[DispatchEntry]]:
        """Apply executor receipts to pending reservations.

        ``pending`` defaults to all reservations.  Entries that are neither
        reported nor explicitly declared as not accepted stay reserved, i.e. the
        permit and memory remain blocked until evidence arrives.
        """
        now = self.clock() if now is None else now
        pending = list(self.pending_entries() if pending is None else pending)
        pending_by_id = {entry.transaction_id: entry for entry in pending}
        pending_by_key = {(entry.key, entry.attempt): entry for entry in pending}

        accepted: list[DispatchEntry] = []
        released: list[DispatchEntry] = []
        unknown: list[DispatchEntry] = []

        claimed: set[str] = set()
        for receipt in status.receipts:
            entry = None
            if receipt.transaction_id:
                entry = pending_by_id.get(receipt.transaction_id)
            if entry is None:
                entry = pending_by_key.get((receipt.key, receipt.attempt))
            if entry is None:
                continue
            claimed.add(entry.transaction_id)
            accepted_at = (
                receipt.accepted_at if receipt.accepted_at is not None else now
            )
            if entry.state in (RESERVED, UNKNOWN):
                self.accept(entry.transaction_id, accepted_at=accepted_at)
            if receipt.mem_mb is not None and entry.mem_mb == 0:
                entry.mem_mb = int(receipt.mem_mb)
            if receipt.state in ("finished", "succeeded", "success", "done"):
                self.finish(entry.key, entry.attempt, now=now, state=FINISHED)
            elif receipt.state in ("failed", "error", "errored"):
                self.finish(entry.key, entry.attempt, now=now, state=FAILED)
            else:
                entry.state = ACCEPTED
                if entry.accepted_at is None:
                    entry.accepted_at = accepted_at
            accepted.append(entry)

        not_accepted_by_id = {
            pending_by_key.get((key, attempt)).transaction_id: pending_by_key.get(
                (key, attempt)
            )
            for key, attempt in status.not_accepted
            if (key, attempt) in pending_by_key
        }
        for entry in pending:
            if entry.transaction_id in claimed:
                continue
            explicit = not_accepted_by_id.get(entry.transaction_id)
            if explicit is not None:
                self.release(entry.transaction_id)
                released.append(entry)
            else:
                # No evidence: keep the reservation. This prevents both a
                # duplicate submission and exceeding an already consumed
                # permit.
                self.mark_unknown(
                    entry.transaction_id,
                    note="executor has not reported this submission yet",
                )
                unknown.append(entry)

        self.save()
        return {"accepted": accepted, "released": released, "unknown": unknown}

    # ------------------------------------------------------------------
    # introspection (used for logs and tests)
    # ------------------------------------------------------------------
    def acceptance_order(self) -> list[DispatchEntry]:
        accepted = [
            entry for entry in self.entries if entry.accepted_at is not None
        ]
        return sorted(
            accepted, key=lambda entry: (entry.accepted_at, entry.reserved_at)
        )

    def memory_by_attempt(self) -> dict[str, int]:
        return {
            f"{entry.key}@{entry.attempt}": entry.mem_mb
            for entry in self.entries
            if entry.state in ACTIVE_STATES
        }

    def permit_snapshot(self, now: Optional[float] = None) -> dict[str, Any]:
        now = self.clock() if now is None else now
        accepted = self.accepted_in_window(now)
        reserved = [e for e in self.entries if e.state in (RESERVED, UNKNOWN)]
        return {
            "now": now,
            "window": self.window,
            "limit": self.max_jobs_per_window,
            "accepted": [
                (entry.key, entry.attempt, entry.accepted_at) for entry in accepted
            ],
            "reserved": [
                (entry.key, entry.attempt, entry.reserved_at)
                for entry in reserved
            ],
            "held": len(accepted) + len(reserved),
            "available": self.available_permits(now),
        }


@dataclass
class DispatchSelection:
    job: Any
    entry: DispatchEntry


class DispatchController:
    """Combine deadline ordering, resource admission, and the dispatch ledger."""

    def __init__(
        self,
        total_resources: Mapping[str, Union[int, str]],
        ledger: DispatchLedger,
        deadline_registry: Optional[DeadlineRegistry] = None,
        clock: Callable[[], float] = time.time,
    ):
        self.total_resources = dict(total_resources)
        self.ledger = ledger
        self.deadline_registry = deadline_registry or DeadlineRegistry()
        self.clock = clock
        self._jobs_by_key: dict[str, Any] = {}

    # ------------------------------------------------------------------
    # resources
    # ------------------------------------------------------------------
    def free_resources(self) -> dict[str, Union[int, str]]:
        free = dict(self.total_resources)
        usage = self.ledger.usage_in_use()
        for name, value in free.items():
            if isinstance(value, str):
                continue
            try:
                free[name] = value - usage.get(name, 0)
            except TypeError:
                pass
        return free

    def _fits(self, job: Any, free: Mapping[str, Union[int, str]]) -> bool:
        resources = getattr(job, "scheduler_resources", None)
        if callable(resources):
            resources = resources()
        resources = resources or {}
        for name, value in resources.items():
            if isinstance(value, str) or name == "_job_count":
                continue
            capacity = free.get(name)
            if capacity is None or isinstance(capacity, str):
                continue
            try:
                if value > capacity:
                    return False
            except TypeError:
                continue
        return True

    def _subtract(self, job: Any, free: dict[str, Union[int, str]]) -> None:
        resources = getattr(job, "scheduler_resources", None)
        if callable(resources):
            resources = resources()
        resources = resources or {}
        for name, value in resources.items():
            if isinstance(value, str) or name == "_job_count":
                continue
            if name not in free or isinstance(free[name], str):
                continue
            try:
                free[name] = free[name] - value
            except TypeError:
                continue

    # ------------------------------------------------------------------
    # selection
    # ------------------------------------------------------------------
    def order(self, jobs: Sequence[Any]) -> list[Any]:
        return sorted(
            jobs,
            key=lambda job: dispatch_sort_key(
                job, deadline_registry=self.deadline_registry
            ),
        )

    def select(self, ready_jobs: Sequence[Any]) -> list[Any]:
        """Select jobs to submit, reserving memory and permits for each.

        Only jobs that are already ready are considered (the caller passes the
        ready set).  A job that does not fit into the currently free resources
        is skipped without consuming memory or a permit, so a later, smaller job
        can still be selected.
        """
        now = self.clock()
        free = self.free_resources()
        permits = self.ledger.available_permits(now)
        selected = []
        for job in self.order(ready_jobs):
            key = job_identity(job)
            if self.ledger.active_for(key) is not None:
                # Already accepted/running (possibly recovered after a
                # restart); never submit it again.
                continue
            counts_permit = job_is_remote(job)
            if counts_permit and permits is not None and permits <= 0:
                # Do not skip ahead: without a submission permit no further
                # remote job may be dispatched in this round.
                continue
            if not self._fits(job, free):
                # Does not fit: do not consume memory or a permit. A later,
                # smaller job may still be selected.
                continue
            entry = self.ledger.reserve(job, now=now)
            self._jobs_by_key[key] = job
            self._subtract(job, free)
            if counts_permit and permits is not None:
                permits -= 1
            selected.append(job)
        return selected

    # ------------------------------------------------------------------
    # executor events
    # ------------------------------------------------------------------
    def accept(self, job: Any, at: Optional[float] = None) -> Optional[DispatchEntry]:
        key = job_identity(job)
        entry = self.ledger.active_for(key)
        if entry is None:
            return None
        if entry.attempt != job_attempt(job):
            return None
        return self.ledger.accept(entry.transaction_id, accepted_at=at)

    def not_accepted(self, job: Any, note: Optional[str] = None) -> bool:
        key = job_identity(job)
        entry = self.ledger.active_for(key)
        if entry is None:
            return False
        return self.ledger.release(entry.transaction_id, note=note)

    def finish(self, job: Any, at: Optional[float] = None) -> bool:
        key = job_identity(job)
        attempt = job_attempt(job)
        changed = self.ledger.finish(key, attempt=attempt, now=at, state=FINISHED)
        if changed:
            self._jobs_by_key.pop(key, None)
        return changed

    def fail(self, job: Any, at: Optional[float] = None) -> bool:
        key = job_identity(job)
        attempt = job_attempt(job)
        changed = self.ledger.finish(key, attempt=attempt, now=at, state=FAILED)
        if changed:
            self._jobs_by_key.pop(key, None)
        return changed

    def can_complete(self, job: Any) -> bool:
        return self.ledger.can_complete(job_identity(job), job_attempt(job))

    def should_process(self, job: Any, attempt: Optional[int] = None) -> bool:
        """Whether a notification for this job's attempt is still relevant.

        A stale notification (for an attempt older than the latest planned one)
        must be ignored entirely: it may neither free memory nor cause a retry.
        A duplicate notification for an attempt that already reached a final
        state must be ignored as well.  An explicit ``attempt`` (as carried by
        an executor notification) takes precedence over ``job.attempt``, because
        the job object's attempt counter may already have advanced.
        """
        key = job_identity(job)
        attempt = job_attempt(job) if attempt is None else int(attempt)
        current = self.ledger.current_attempt(key)
        if current is not None and attempt < current:
            return False
        for entry in self.ledger.entries_for(key):
            # A released entry was never accepted, so an identical attempt
            # number may legitimately be planned again after recovery and its
            # completion must still be processed.
            if (
                entry.attempt == attempt
                and entry.state in FINAL_STATES
                and entry.state != RELEASED
            ):
                return False
        active = self.ledger.active_for(key)
        # If an active other attempt exists, ignore notifications for this one.
        return active is None or active.attempt == attempt

    def reconcile(
        self,
        status: Any,
        now: Optional[float] = None,
        pending: Optional[Iterable[DispatchEntry]] = None,
    ) -> dict[str, list[DispatchEntry]]:
        return self.ledger.reconcile(
            DispatchStatus.from_any(status), now=now, pending=pending
        )

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------
    @property
    def memory_in_use(self) -> int:
        return self.ledger.memory_in_use()

    def time_until_permit(self) -> float:
        """Seconds until another permit becomes available, if any."""
        return self.ledger.time_until_permit()

    def permit_deferred(self, ready_jobs: Sequence[Any]) -> bool:
        """Whether a ready remote job is blocked by the submission window.

        Such a job is not blocked by memory, so time (not a completion) will
        make it dispatchable.
        """
        if not ready_jobs:
            return False
        permits = self.ledger.available_permits()
        if permits is None or permits > 0:
            return False
        free = self.free_resources()
        for job in ready_jobs:
            if self.ledger.active_for(job_identity(job)) is not None:
                continue
            if job_is_remote(job) and self._fits(job, free):
                return True
        return False

    def available_memory(self) -> int:
        return self.free_resources().get("mem_mb", 0)
