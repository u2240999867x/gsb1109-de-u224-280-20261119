"""Deterministic simulation of a restarting remote execution end.

This module implements a small, locally controllable "execution end" that is
used to exercise the deadline dispatch policy together with restart-safe
dispatch bookkeeping.  It is deliberately independent of real clusters: time is
advanced explicitly, attempts and memory footprints are recorded, and every
accepted submission carries a *receipt* that includes the attempt number and the
time of acceptance.

The same object doubles as an executor mixin (``ControllableExecutorMixin``) so
that a Snakemake workflow can be driven through a real ``JobScheduler``.  The
mixin implements the optional ``dispatch_status(pending)`` hook that
``JobScheduler`` uses for restart recovery; executors that do not implement it
are simply not reconciled (their reservations are kept).
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Optional, Sequence

from snakemake.scheduling.dispatch import DispatchStatus


@dataclass
class Attempt:
    key: str
    attempt: int
    mem_mb: int
    accepted_at: Optional[float] = None
    state: str = "planned"  # planned | accepted | running | finished | failed
    finished_at: Optional[float] = None
    submission: int = 0
    transaction_id: Optional[str] = None
    report: dict[str, Any] = field(default_factory=dict)
    collected: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "attempt": self.attempt,
            "mem_mb": self.mem_mb,
            "accepted_at": self.accepted_at,
            "state": self.state,
            "finished_at": self.finished_at,
            "submission": self.submission,
            "transaction_id": self.transaction_id,
            "report": self.report,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Attempt":
        return cls(
            key=str(data["key"]),
            attempt=int(data["attempt"]),
            mem_mb=int(data.get("mem_mb", 0)),
            accepted_at=data.get("accepted_at"),
            state=str(data.get("state", "planned")),
            finished_at=data.get("finished_at"),
            submission=int(data.get("submission", 0)),
            transaction_id=data.get("transaction_id"),
            report=dict(data.get("report") or {}),
        )


class SimulatedExecutionEnd:
    """A locally controllable remote execution end.

    Memory is admitted at *submission planning* time by the scheduler.  The
    execution end additionally validates that a submission does not exceed the
    total memory budget and keeps all accepted submissions until they are
    explicitly finished and a garbage collection has run.  This lets tests show
    that replaying a completion notification does not release anything.

    The execution end may be "lost" (scheduler restart) and queried again.  It
    records every attempt, including failed ones, and can answer with a receipt
    per attempt::

        {
            "receipts": [
                {"key": ..., "attempt": ..., "accepted_at": ..., "state": "running"},
                ...
            ],
            "not_accepted": [(key, attempt), ...],
        }

    Submissions that were planned at the scheduler but for which the execution
    end has no record yet are *not* listed in ``not_accepted`` unless the caller
    asked for
    :meth:`receipts`, which reports only what is known.  Callers can ask for a
    specific subset of planned submissions (``pending``) to learn which of them
    are definitively unknown.
    """

    def __init__(
        self,
        total_mem_mb: int,
        clock: Callable[[], float],
        max_submissions_per_window: Optional[int] = None,
        window: float = 10.0,
    ):
        self.total_mem_mb = int(total_mem_mb)
        self.clock = clock
        self.max_submissions_per_window = max_submissions_per_window
        self.window = float(window)
        self.attempts: dict[str, list[Attempt]] = {}
        self.submission_times: list[float] = []
        self.accepted: list[Attempt] = []
        self._generation = 0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------
    # queries
    # ------------------------------------------------------------------
    def _attempts_for(self, key: str) -> list[Attempt]:
        return self.attempts.setdefault(key, [])

    def latest(self, key: str) -> Optional[Attempt]:
        entries = self._attempts_for(key)
        return entries[-1] if entries else None

    def memory_in_use(self) -> int:
        """Memory of accepted/unfinished submissions still held by the end."""
        return sum(
            attempt.mem_mb
            for attempt in self.accepted
            if attempt.state in ("accepted", "running") and not attempt.collected
        )

    def accepted_attempts(self) -> list[Attempt]:
        return [
            attempt
            for attempt in self.accepted
            if attempt.accepted_at is not None
        ]

    def submission_times_in_window(self, now: Optional[float] = None) -> list[float]:
        now = self.clock() if now is None else now
        return [t for t in self.submission_times if t > now - self.window]

    def can_submit(self, now: Optional[float] = None) -> bool:
        if self.max_submissions_per_window is None:
            return True
        return (
            len(self.submission_times_in_window(now))
            < self.max_submissions_per_window
        )

    # ------------------------------------------------------------------
    # simulation steps
    # ------------------------------------------------------------------
    def submit(
        self,
        key: str,
        attempt: int,
        mem_mb: int,
        now: Optional[float] = None,
        transaction_id: Optional[str] = None,
        report: Optional[dict[str, Any]] = None,
        accept: bool = True,
    ) -> Attempt:
        """Plan (and possibly accept) one submission."""
        now = self.clock() if now is None else now
        with self._lock:
            entries = self._attempts_for(key)
            for existing in entries:
                if existing.attempt == attempt:
                    # Idempotent: the same attempt is never accepted twice.
                    return existing
            if accept and not self.can_submit(now):
                raise RuntimeError(
                    f"More than {self.max_submissions_per_window} submissions "
                    f"within {self.window}s (at {now})."
                )
            if accept:
                self.submission_times.append(now)
            attempt_record = Attempt(
                key=key,
                attempt=attempt,
                mem_mb=int(mem_mb),
                accepted_at=now if accept else None,
                state="accepted" if accept else "planned",
                submission=len(self.submission_times),
                transaction_id=transaction_id,
                report=dict(report or {}),
            )
            entries.append(attempt_record)
            if accept:
                if self.memory_in_use() + int(mem_mb) > self.total_mem_mb:
                    raise RuntimeError(
                        f"Execution end out of memory: {self.memory_in_use()} + "
                        f"{mem_mb} > {self.total_mem_mb}"
                    )
                self.accepted.append(attempt_record)
            return attempt_record

    def start_running(self, key: str, attempt: Optional[int] = None) -> Attempt:
        record = self._get(key, attempt)
        record.state = "running"
        return record

    def finish(
        self,
        key: str,
        attempt: Optional[int] = None,
        now: Optional[float] = None,
        failed: bool = False,
    ) -> bool:
        """Mark an attempt as finished. Idempotent; returns whether it changed."""
        now = self.clock() if now is None else now
        with self._lock:
            record = self._get(key, attempt)
            if record.state in ("finished", "failed"):
                return False
            record.state = "failed" if failed else "finished"
            record.finished_at = now
            return True

    def collect(self) -> int:
        """Garbage-collect finished attempts from the memory budget.

        The history of accepted attempts is retained (it is needed to show that
        a replayed completion cannot free current memory); only the memory of
        finished attempts is released.
        """
        with self._lock:
            released = 0
            for record in self.accepted:
                if record.state in ("finished", "failed") and not getattr(
                    record, "_collected", False
                ):
                    released += record.mem_mb
                    record._collected = True
            return released

    def restart(self) -> None:
        """Simulate a scheduler restart. Accepted attempts remain untouched."""
        self._generation += 1

    # ------------------------------------------------------------------
    # receipts
    # ------------------------------------------------------------------
    def receipts(
        self,
        pending: Optional[Iterable[Any]] = None,
        now: Optional[float] = None,
        explicitly_planned: Optional[Iterable[tuple[str, int]]] = None,
    ) -> dict[str, Any]:
        """Return what the execution end actually received.

        ``pending`` may contain ``DispatchEntry`` objects (or ``(key, attempt)``
        pairs).  For each pending submission the execution end knows about, a
        receipt is included.  For pending submissions where the execution end can
        prove it never accepted anything, the entry is added to
        ``not_accepted``.  Anything else is simply not mentioned; the caller must
        keep the reservation.
        """
        with self._lock:
            receipts = []
            not_accepted = []
            for reference in pending or ():
                if isinstance(reference, tuple):
                    key, attempt = reference
                    transaction_id = None
                else:
                    key, attempt = reference.key, reference.attempt
                    transaction_id = getattr(reference, "transaction_id", None)
                record = None
                for candidate in self._attempts_for(key):
                    if candidate.attempt == attempt:
                        record = candidate
                        break
                if record is not None and record.accepted_at is not None:
                    receipts.append(
                        {
                            "key": key,
                            "attempt": attempt,
                            "accepted_at": record.accepted_at,
                            "state": "running"
                            if record.state in ("accepted", "running")
                            else record.state,
                            "mem_mb": record.mem_mb,
                            "transaction_id": record.transaction_id
                            or transaction_id,
                        }
                    )
                elif explicitly_planned is not None and (key, attempt) in set(
                    explicitly_planned
                ):
                    not_accepted.append((key, attempt))
            return {
                "receipts": receipts,
                "not_accepted": not_accepted,
                "generation": self._generation,
            }

    def status_view(
        self,
        pending: Iterable[Any],
        explicitly_planned: Optional[Iterable[tuple[str, int]]] = None,
    ) -> DispatchStatus:
        return DispatchStatus.from_any(
            self.receipts(pending, explicitly_planned=explicitly_planned)
        )

    # ------------------------------------------------------------------
    # convenience for assertions
    # ------------------------------------------------------------------
    def report(self) -> dict[str, Any]:
        return {
            "generation": self._generation,
            "accepted": [attempt.to_dict() for attempt in self.accepted],
            "attempts": {
                key: [attempt.to_dict() for attempt in entries]
                for key, entries in self.attempts.items()
            },
            "memory_in_use": self.memory_in_use(),
            "submissions": list(self.submission_times),
        }

    def save(self, path: Path) -> None:
        path.write_text(json.dumps(self.report(), indent=2))

    def _get(self, key: str, attempt: Optional[int] = None) -> Attempt:
        entries = self._attempts_for(key)
        if not entries:
            raise KeyError(key)
        if attempt is None:
            return entries[-1]
        for record in entries:
            if record.attempt == attempt:
                return record
        raise KeyError((key, attempt))


class ControllableExecutorMixin:
    """Mixin adding the optional ``dispatch_status`` hook to an executor.

    The executor is expected to provide a ``dispatch_end`` attribute with a
    :class:`SimulatedExecutionEnd`.  Executors without the mixin are unaffected:
    the scheduler keeps reservations in the ledger until evidence arrives.
    """

    dispatch_end: SimulatedExecutionEnd

    def dispatch_status(self, pending: Sequence[Any]) -> DispatchStatus:
        return self.dispatch_end.status_view(pending)
