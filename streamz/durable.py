"""
Durable, priority-aware dispatching of device events to one write table.

Two priorities ("urgent" and "normal") are handled independently, each with
its own enqueue sequence numbers.  Urgent events may overtake waiting normal
events, but while a normal event is waiting at most ``max_urgent_burst``
urgent events may be dispatched back to back before a normal event gets the
next dispatch slot.

Every write is an *attempt* that is acknowledged out of band by the writer
(a receipt).  An event is only considered confirmed once the receipt for its
current attempt arrives; an in-flight event is never marked successful or
dispatched again merely because the executor changed.  Explicit retries create
a new attempt for the same enqueued event, so the writer can tell the two
attempts apart and a late receipt for the old attempt can never settle the new
attempt, nor consume the dispatch slot promised to the waiting normal event.

Progress is checkpointed atomically to a JSON file.  Pointing a new dispatcher
(process) at the same state file and the same table resumes the queues and the
in-flight attempts, re-attaching to their open receipts.
"""
from __future__ import annotations

import asyncio
import json
import os
from collections import deque

from .core import Stream

STATE_VERSION = 1


class ReceiptHandle:
    """A single write attempt handed out by a :class:`DurableWriteTable`."""

    def __init__(self, event_id, attempt, future):
        self.event_id = event_id
        self.attempt = attempt
        self.future = future

    def __await__(self):
        return self.future.__await__()

    def __repr__(self):
        return (f"ReceiptHandle(event_id={self.event_id!r}, "
                f"attempt={self.attempt})")


class DurableWriteTable:
    """Stand-in for the Streamz-backed table that accepts event writes.

    A write is accepted (the writer has received it) via :meth:`submit`, which
    consumes an admission permit; its receipt is delivered later through the
    returned handle.  Every submission is one distinct attempt, including
    retries of the same enqueued event, so the two attempts never get mixed up.
    """

    def __init__(self):
        self._next_attempt = {}
        self._handles = {}
        self._permits = 0
        self._permit_event = asyncio.Event()
        self.dispatch_log = []
        self.receipts = []

    def admit(self, count=1):
        """Allow the next ``count`` submissions to be accepted by the writer."""
        self._permits += count
        self._permit_event.set()

    async def submit(self, event):
        event_id = event["event_id"]
        while self._permits == 0:
            self._permit_event.clear()
            await self._permit_event.wait()
        self._permits -= 1
        if self._permits == 0:
            self._permit_event.clear()

        attempt = self._next_attempt.get(event_id, 0) + 1
        self._next_attempt[event_id] = attempt
        handle = ReceiptHandle(
            event_id, attempt,
            asyncio.get_event_loop().create_future())
        self._handles[(event_id, attempt)] = handle
        self.dispatch_log.append({"event_id": event_id, "attempt": attempt})
        return handle

    def pending_handle(self, event_id, attempt):
        """Re-attach to an attempt that stayed in flight across a failover."""
        handle = self._handles[(event_id, attempt)]
        if handle.future.done():
            raise RuntimeError(
                f"attempt {attempt} of {event_id} already has a receipt")
        return handle

    def handle(self, event_id, attempt):
        return self._handles[(event_id, attempt)]

    def succeed(self, handle, result=None):
        if handle.future.done():
            raise RuntimeError(f"{handle} already settled")
        handle.future.set_result(result)
        self.receipts.append(
            {"event_id": handle.event_id, "attempt": handle.attempt,
             "status": "confirmed", "result": result})

    def fail(self, handle, error="write failed"):
        if handle.future.done():
            raise RuntimeError(f"{handle} already settled")
        handle.future.set_exception(RuntimeError(error))
        self.receipts.append(
            {"event_id": handle.event_id, "attempt": handle.attempt,
             "status": "failed", "error": error})

    def attempts_for(self, event_id):
        return [dict(item) for item in self.dispatch_log
                if item["event_id"] == event_id]


@Stream.register_api()
class durable_dispatch(Stream):
    """Dispatch events durably to a write table, with two priority lanes.

    Parameters
    ----------
    upstream: Stream
        Stream of event objects.  By default each event is a ``dict`` carrying
        a ``priority`` field whose value is ``urgent_name`` or ``normal_name``.
    table: DurableWriteTable
        The (remotely reachable) table that writes are submitted to.  The same
        table instance outlives a failover and keeps every attempt's handle.
    state_path: str
        Path of the JSON checkpoint file.  If it exists on construction and
        ``recover`` is true, queues, confirmed events and in-flight attempts
        are restored from it.
    priority_of: callable, optional
        ``priority_of(event) -> str``; defaults to ``event["priority"]``.
    urgent_name, normal_name: str
        Values used to label the two lanes.
    max_urgent_burst: int
        Maximum number of urgent events dispatched consecutively while a
        normal event is waiting.
    urgent_capacity, normal_capacity: int
        Maximum number of unacknowledged events per lane.
    recover: bool
        Load ``state_path`` on construction when it exists.
    start_paused: bool
        When true, events are accepted and queued but nothing is dispatched
        until :meth:`resume` is called.
    """

    def __init__(self, upstream, table, state_path,
                 priority_of=None, urgent_name="urgent", normal_name="normal",
                 max_urgent_burst=3, urgent_capacity=4, normal_capacity=4,
                 recover=True, start_paused=False, **kwargs):
        self.table = table
        self.state_path = state_path
        self._priority_of = priority_of or (lambda event: event["priority"])
        self.urgent_name = urgent_name
        self.normal_name = normal_name
        self.max_urgent_burst = max_urgent_burst
        self.capacity = {urgent_name: urgent_capacity,
                         normal_name: normal_capacity}
        self._paused = start_paused

        self.next_seq = {urgent_name: 0, normal_name: 0}
        self.events = {}
        self.queued = deque()
        self.inflight = {}
        self.confirmed = set()
        self.attempts = []
        self._dispatching = set()
        self._urgent_streak = 0

        self._tasks = set()
        self._pump_task = None

        kwargs["ensure_io_loop"] = True
        super().__init__(upstream, **kwargs)

        if recover and os.path.exists(state_path):
            self._load_state()
        self._schedule_pump()

    # ------------------------------------------------------------------ #
    # Stream API
    # ------------------------------------------------------------------ #
    def update(self, x, who=None, metadata=None):
        return self._spawn(self._enqueue(x))

    def pause(self):
        """Accept and checkpoint new events without dispatching them."""
        self._paused = True

    def resume(self):
        """Release :meth:`pause` and dispatch everything currently eligible."""
        self._paused = False
        self._schedule_pump()

    def close(self):
        """Hand this executor's work over to a successor.

        A final checkpoint is written and every local awaiter is cancelled so
        a receipt arriving later can only be observed by the new executor.
        """
        self._persist()
        for task in list(self._tasks):
            task.cancel()
        self._tasks.clear()
        self._pump_task = None

    # ------------------------------------------------------------------ #
    # Enqueueing and the fairness rule
    # ------------------------------------------------------------------ #
    async def _enqueue(self, event):
        priority = self._priority_of(event)
        if priority not in self.capacity:
            raise ValueError(f"unknown priority {priority!r}")
        self.next_seq[priority] += 1
        seq = self.next_seq[priority]
        key = (priority, seq)
        payload = dict(event)
        payload.setdefault("event_id", f"{priority}-{seq}")
        self.events[key] = payload
        self.queued.append({"priority": priority, "seq": seq,
                            "event_id": payload["event_id"]})
        self._persist()
        self._schedule_pump()

    def _head(self, priority):
        for record in self.queued:
            key = (record["priority"], record["seq"])
            if record["priority"] == priority and key not in self._dispatching:
                return record
        return None

    def _inflight_count(self, priority):
        return sum(1 for record in self.inflight.values()
                   if record["priority"] == priority)

    def _next_candidate(self):
        """Implement the urgent burst / normal fairness rule."""
        if self._paused:
            return None
        urgent = self._head(self.urgent_name)
        normal = self._head(self.normal_name)
        urgent_room = self._inflight_count(self.urgent_name) \
            < self.capacity[self.urgent_name]
        normal_room = self._inflight_count(self.normal_name) \
            < self.capacity[self.normal_name]

        if urgent and urgent_room and (
                self._urgent_streak < self.max_urgent_burst
                or normal is None):
            return urgent
        if normal and normal_room:
            return normal
        if urgent and urgent_room:
            return urgent
        return None

    def _schedule_pump(self):
        if self._pump_task is None or self._pump_task.done():
            self._pump_task = self._spawn(self._pump_loop())

    async def _pump_loop(self):
        while True:
            record = self._next_candidate()
            if record is None:
                return
            key = (record["priority"], record["seq"])
            self._dispatching.add(key)
            try:
                # The record stays queued (and thus checkpointed) until the
                # writer has accepted the write; a blocked submission simply
                # means the next executor will dispatch it.
                handle = await self.table.submit(self.events[key])
            finally:
                self._dispatching.discard(key)
            self.queued.remove(record)
            self._register_attempt(record, handle, retry=False)
            self._schedule_pump()

    def _register_attempt(self, record, handle, retry):
        key = (record["priority"], record["seq"])
        attempt = handle.attempt
        expected = max(
            (item["attempt"] for item in self.attempts
             if (item["priority"], item["seq"]) == key),
            default=0) + 1
        if attempt != expected:
            raise RuntimeError("writer attempt number out of sync with state")
        attempt_record = {
            "priority": record["priority"], "seq": record["seq"],
            "event_id": record["event_id"], "attempt": attempt,
            "status": "pending", "error": None, "superseded": False,
            "handle": handle,
        }
        self.attempts.append(attempt_record)
        self.inflight[key] = attempt_record
        if not retry:
            if record["priority"] == self.urgent_name:
                self._urgent_streak += 1
            else:
                self._urgent_streak = 0
        self._persist()
        self._spawn(self._settle(attempt_record))

    # ------------------------------------------------------------------ #
    # Receipts and retries
    # ------------------------------------------------------------------ #
    async def _settle(self, attempt_record):
        handle = attempt_record["handle"]
        key = (attempt_record["priority"], attempt_record["seq"])
        try:
            # Shield the writer's receipt future: when an executor hands over,
            # its awaiter is cancelled but the open receipt must survive for
            # the successor to observe.
            await asyncio.shield(handle.future)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            attempt_record["status"] = "failed"
            attempt_record["error"] = str(exc)
        else:
            attempt_record["status"] = "confirmed"
        self._persist()

        # A superseded attempt (an explicit retry happened first) only records
        # its own receipt; it must never touch the replacement attempt.
        if self.inflight.get(key) is not attempt_record:
            return

        if attempt_record["status"] == "confirmed":
            del self.inflight[key]
            self.confirmed.add(key)
        else:
            # The failed attempt no longer occupies an in-flight slot; keep
            # the event's original lane position by requeueing it ahead of
            # the other events still waiting in that lane.
            del self.inflight[key]
            self.queued.appendleft({
                "priority": attempt_record["priority"],
                "seq": attempt_record["seq"],
                "event_id": attempt_record["event_id"],
            })
        self._schedule_pump()

    async def retry(self, priority, seq):
        """Explicitly re-submit an in-flight event as a new attempt.

        Returns the new attempt number.  A confirmed event cannot be retried,
        and a retry neither reorders the queues nor consumes a dispatch slot.
        """
        key = (priority, seq)
        if key in self.confirmed:
            raise RuntimeError(
                f"event {key} already confirmed; nothing to retry")
        current = self.inflight.get(key)
        if current is None:
            raise RuntimeError(f"event {key} is not in flight")
        if current["status"] != "pending":
            raise RuntimeError(
                f"event {key} already has a receipt for attempt "
                f"{current['attempt']}; wait for it before retrying")

        current["superseded"] = True
        handle = await self.table.submit(self.events[key])
        record = {"priority": priority, "seq": seq,
                  "event_id": current["event_id"]}
        self._register_attempt(record, handle, retry=True)
        return handle.attempt

    # ------------------------------------------------------------------ #
    # Checkpoint / failover
    # ------------------------------------------------------------------ #
    def save_progress(self):
        """Atomically checkpoint queues, in-flight attempts and receipts."""
        self._persist()

    def _persist(self):
        snapshot = {
            "version": STATE_VERSION,
            "next_seq": dict(self.next_seq),
            "events": [
                [list(key), payload] for key, payload in sorted(self.events.items())
            ],
            "queued": [dict(item) for item in self.queued],
            "inflight": [{
                "priority": item["priority"], "seq": item["seq"],
                "event_id": item["event_id"], "attempt": item["attempt"],
            } for item in self.inflight.values()],
            "confirmed": [list(key) for key in sorted(self.confirmed)],
            "attempts": [{
                "priority": item["priority"], "seq": item["seq"],
                "attempt": item["attempt"], "status": item["status"],
                "error": item.get("error"),
                "superseded": item.get("superseded", False),
            } for item in self.attempts],
            "urgent_streak": self._urgent_streak,
        }
        tmp_path = f"{self.state_path}.tmp"
        with open(tmp_path, "w") as fp:
            json.dump(snapshot, fp, indent=2)
            fp.flush()
            os.fsync(fp.fileno())
        os.replace(tmp_path, self.state_path)

    def _load_state(self):
        with open(self.state_path) as fp:
            snapshot = json.load(fp)
        if snapshot.get("version") != STATE_VERSION:
            raise RuntimeError(
                f"unsupported checkpoint version {snapshot.get('version')!r}")
        self.next_seq = dict(snapshot["next_seq"])
        self.events = {tuple(key): payload for key, payload in
                       snapshot.get("events", [])}
        self.queued = deque(dict(item) for item in snapshot["queued"])
        self.confirmed = {tuple(item) for item in snapshot["confirmed"]}
        self.attempts = [{
            "priority": item["priority"], "seq": item["seq"],
            "event_id": f"{item['priority']}-{item['seq']}",
            "attempt": item["attempt"], "status": item["status"],
            "error": item.get("error"),
            "superseded": item.get("superseded", False),
            "handle": None,
        } for item in snapshot["attempts"]]

        self.inflight = {}
        for item in snapshot["inflight"]:
            key = (item["priority"], item["seq"])
            handle = self.table.pending_handle(item["event_id"],
                                               item["attempt"])
            attempt_record = next(
                record for record in self.attempts
                if (record["priority"], record["seq"]) == key
                and record["attempt"] == item["attempt"])
            attempt_record["handle"] = handle
            attempt_record["status"] = "pending"
            self.inflight[key] = attempt_record
            self._spawn(self._settle(attempt_record))
        self._urgent_streak = snapshot.get("urgent_streak", 0)

    # ------------------------------------------------------------------ #
    # Inspection
    # ------------------------------------------------------------------ #
    def event_records(self):
        """Per-event confirmation state with every attempt's receipt."""
        events = {}

        def ensure(priority, seq, event_id):
            key = (priority, seq)
            if key not in events:
                events[key] = {
                    "priority": priority, "seq": seq, "event_id": event_id,
                    "state": "queued", "attempts": [],
                }
            return events[key]

        for record in self.queued:
            ensure(record["priority"], record["seq"],
                   record["event_id"])["state"] = "queued"
        for key in self.confirmed:
            ensure(key[0], key[1], f"{key[0]}-{key[1]}"
                   )["state"] = "confirmed"
        for key, record in self.inflight.items():
            entry = ensure(key[0], key[1], record["event_id"])
            entry["state"] = record["status"]
        for record in self.attempts:
            entry = ensure(record["priority"], record["seq"],
                           record["event_id"])
            entry["attempts"].append({
                "attempt": record["attempt"],
                "status": record["status"],
                "error": record.get("error"),
                "superseded": record.get("superseded", False),
            })
        return [events[key] for key in sorted(events)]

    def dispatch_order(self):
        return [dict(item) for item in self.table.dispatch_log]

    def receipts(self):
        return [dict(item) for item in self.table.receipts]

    def _spawn(self, coro):
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task
