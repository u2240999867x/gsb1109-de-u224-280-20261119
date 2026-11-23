"""Core ledger for archivable, mergeable station ingest progress.

The ledger keeps three kinds of records:

* :class:`Submission` -- a collector delivers observations for a set of cells.
  An observation is either a valid value (including valid zero) or an explicit
  missing marker.
* :class:`Revocation` -- revokes part of one target submission's cells. A cell
  falls back to the collector's previous still-effective submission; an
  explicit missing marker only disappears when it is itself revoked.
* :class:`SealingReceipt` -- names a batch of submission/revocation ids. Once
  every named record has been seen, the named submissions can no longer be the
  target of a new revocation and their supporting history may be pruned.

A progress object is fully determined by its records, so two independently
collected progress files merge symmetrically and commute. Pruned records are
remembered as tombstones together with the current per-cell values, which lets
an older snapshot be merged back without reviving anything that was revoked or
superseded.
"""

from __future__ import annotations

import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

# Observation kinds stored inside a submission.
OBS_VALUE = 1
OBS_MISSING = 2

# Derived per-cell coverage states.
STATE_UNCOVERED = 0
STATE_VALUE = 1
STATE_MISSING = 2
STATE_CONFLICT = 3

#: A cell is ``(station, time, variable)``.
CellKey = tuple[str, str, str]
#: An observation is ``(kind, value)``; value is NaN for explicit missing.
Observation = tuple[int, float]


class IngestError(Exception):
    """Base class for ingest ledger errors."""


class DuplicateRecordError(IngestError):
    """Raised when a record id is reused with different content."""


class ConflictingRecordError(IngestError):
    """Raised when two progresses disagree about the same record id."""


class PrunedRecordError(IngestError):
    """Raised when archived history is reintroduced through direct intake."""


class SealedTargetError(IngestError):
    """Raised when a revocation targets a sealed submission."""


def obs_value(value: float) -> Observation:
    """Build a valid observation, preserving zero as a real reading."""
    return OBS_VALUE, float(value)


def obs_missing() -> Observation:
    """Build an explicit missing observation."""
    return OBS_MISSING, math.nan


def _same_float(left: float, right: float) -> bool:
    if math.isnan(left):
        return math.isnan(right)
    return left == right


def _same_observation(left: Observation, right: Observation) -> bool:
    return left[0] == right[0] and _same_float(left[1], right[1])


@dataclass(frozen=True)
class Submission:
    """One collector submission covering any subset of grid cells."""

    record_id: int
    collector: str
    observations: tuple[tuple[CellKey, Observation], ...]

    def __init__(
        self,
        record_id: int,
        collector: str,
        observations: (
            Mapping[CellKey, Observation] | Iterable[tuple[CellKey, Observation]]
        ),
    ) -> None:
        normalized = tuple(
            sorted(
                (cell, (int(kind), float(value)))
                for cell, (kind, value) in dict(observations).items()
            )
        )
        object.__setattr__(self, "record_id", int(record_id))
        object.__setattr__(self, "collector", str(collector))
        object.__setattr__(self, "observations", normalized)

    @property
    def cells(self) -> frozenset[CellKey]:
        return frozenset(cell for cell, _ in self.observations)

    def observation_at(self, cell: CellKey) -> Observation | None:
        for candidate, observation in self.observations:
            if candidate == cell:
                return observation
        return None

    def content_equal(self, other: Submission) -> bool:
        if self.record_id != other.record_id or self.collector != other.collector:
            return False
        if len(self.observations) != len(other.observations):
            return False
        return all(
            left_cell == right_cell
            and _same_observation(left_obs, right_obs)
            for (left_cell, left_obs), (right_cell, right_obs) in zip(
                self.observations, other.observations, strict=True
            )
        )


@dataclass(frozen=True)
class Revocation:
    """Partial revocation of one target submission of the same collector."""

    record_id: int
    target_id: int
    collector: str
    cells: frozenset[CellKey]

    def __init__(
        self,
        record_id: int,
        target_id: int,
        collector: str,
        cells: Iterable[CellKey],
    ) -> None:
        object.__setattr__(self, "record_id", int(record_id))
        object.__setattr__(self, "target_id", int(target_id))
        object.__setattr__(self, "collector", str(collector))
        object.__setattr__(self, "cells", frozenset(cells))

    def content_equal(self, other: Revocation) -> bool:
        return (
            self.record_id == other.record_id
            and self.target_id == other.target_id
            and self.collector == other.collector
            and self.cells == other.cells
        )


@dataclass(eq=False, slots=True)
class SealingReceipt:
    """A receipt naming the records sealed as one batch."""

    receipt_id: int
    named: frozenset[int]
    completed: bool = False

    def __init__(self, receipt_id: int, named: Iterable[int]) -> None:
        self.receipt_id = int(receipt_id)
        self.named = frozenset(int(record_id) for record_id in named)
        self.completed = False

    def content_equal(self, other: SealingReceipt) -> bool:
        return self.receipt_id == other.receipt_id and self.named == other.named


@dataclass(slots=True)
class _SourceState:
    """The currently effective observation of one collector in one cell."""

    record_id: int
    kind: int
    value: float

    def copy(self) -> _SourceState:
        return _SourceState(self.record_id, self.kind, self.value)

    def equals(self, other: _SourceState) -> bool:
        return (
            self.record_id == other.record_id
            and self.kind == other.kind
            and _same_float(self.value, other.value)
        )


@dataclass(slots=True)
class GridView:
    """Materialized consensus view of one variable across station/time."""

    variable: str
    stations: list[str]
    times: list[str]
    status: np.ndarray
    value: np.ndarray
    contributor_count: np.ndarray
    contributors: np.ndarray
    coverage: dict[str, np.ndarray]

    def conflict_details(self) -> list[dict[str, Any]]:
        """Return one entry per conflicted cell with each party's current value."""
        details: list[dict[str, Any]] = []
        for station_index, station in enumerate(self.stations):
            for time_index, time in enumerate(self.times):
                if self.status[station_index, time_index] != STATE_CONFLICT:
                    continue
                details.append(
                    {
                        "station": station,
                        "time": time,
                        "variable": self.variable,
                        "parties": [
                            {
                                "collector": collector,
                                "record_id": record_id,
                                "kind": kind,
                                "value": None if math.isnan(value) else value,
                            }
                            for collector, record_id, kind, value in self.contributors[
                                station_index, time_index
                            ]
                        ],
                    }
                )
        return details


class IngestProgress:
    """Mergeable ingest progress for one station grid.

    The grid coordinates are learned from the submitted cells. Submissions take
    effect in record-id order, so out-of-order and late delivery converge to the
    same ledger state. Every mutation is deterministic, and the full state is
    reconstructible from the recorded submissions, revocations, receipts,
    tombstones and the retained current values of archived collectors.
    """

    def __init__(
        self,
        stations: Iterable[str] = (),
        times: Iterable[str] = (),
        variables: Iterable[str] = (),
    ) -> None:
        self._cells: dict[CellKey, dict[str, _SourceState]] = {}
        self._submissions: dict[int, Submission] = {}
        self._revocations: dict[int, Revocation] = {}
        self._receipts: dict[int, SealingReceipt] = {}
        self._sealed: set[int] = set()
        self._tombstones: set[int] = set()
        self._generation = 0
        self._declared_stations = tuple(sorted(stations))
        self._declared_times = tuple(sorted(times))
        self._declared_variables = tuple(sorted(variables))

    # ------------------------------------------------------------------ #
    # Construction helpers used by the NetCDF reader and by merge.
    # ------------------------------------------------------------------ #
    @classmethod
    def _from_parts(
        cls,
        cells: Mapping[CellKey, Mapping[str, _SourceState]],
        submissions: Iterable[Submission],
        revocations: Iterable[Revocation],
        receipts: Iterable[SealingReceipt],
        sealed: Iterable[int],
        tombstones: Iterable[int],
        generation: int,
        grid: tuple[Sequence[str], Sequence[str], Sequence[str]] | None = None,
    ) -> IngestProgress:
        progress = cls()
        if grid is not None:
            stations, times, variables = grid
            progress._declared_stations = tuple(sorted(stations))
            progress._declared_times = tuple(sorted(times))
            progress._declared_variables = tuple(sorted(variables))
        progress._cells = {
            cell: {collector: state.copy() for collector, state in states.items()}
            for cell, states in cells.items()
        }
        for submission in submissions:
            progress._submissions[submission.record_id] = submission
        for revocation in revocations:
            progress._revocations[revocation.record_id] = revocation
        for receipt in receipts:
            stored = SealingReceipt(receipt.receipt_id, receipt.named)
            stored.completed = receipt.completed
            progress._receipts[receipt.receipt_id] = stored
        progress._sealed = set(sealed)
        progress._tombstones = set(tombstones)
        progress._generation = int(generation)
        progress._refresh_sealed()
        return progress

    def copy(self) -> IngestProgress:
        return IngestProgress._from_parts(
            self._cells,
            self._submissions.values(),
            self._revocations.values(),
            self._receipts.values(),
            self._sealed,
            self._tombstones,
            self._generation,
            grid=(
                self._declared_stations,
                self._declared_times,
                self._declared_variables,
            ),
        )

    # ------------------------------------------------------------------ #
    # Introspection.
    # ------------------------------------------------------------------ #
    @property
    def generation(self) -> int:
        return self._generation

    @property
    def stations(self) -> list[str]:
        return sorted(
            {cell[0] for cell in self._cells} | set(self._declared_stations)
        )

    @property
    def times(self) -> list[str]:
        return sorted(
            {cell[1] for cell in self._cells} | set(self._declared_times)
        )

    @property
    def variables(self) -> list[str]:
        return sorted(
            {cell[2] for cell in self._cells} | set(self._declared_variables)
        )

    @property
    def submissions(self) -> tuple[Submission, ...]:
        return tuple(self._submissions[index] for index in sorted(self._submissions))

    @property
    def revocations(self) -> tuple[Revocation, ...]:
        return tuple(self._revocations[index] for index in sorted(self._revocations))

    @property
    def receipts(self) -> tuple[SealingReceipt, ...]:
        return tuple(self._receipts[index] for index in sorted(self._receipts))

    @property
    def sealed_records(self) -> frozenset[int]:
        return frozenset(self._sealed)

    @property
    def tombstones(self) -> frozenset[int]:
        return frozenset(self._tombstones)

    @property
    def pending_receipts(self) -> tuple[SealingReceipt, ...]:
        return tuple(
            receipt for receipt in self._receipts.values() if not receipt.completed
        )

    def source_states(
        self, station: str, time: str, variable: str
    ) -> dict[str, tuple[int, int, float]]:
        """Current per-collector state at one cell (copy, safe to mutate)."""
        states = self._cells.get((station, time, variable), {})
        return {
            collector: (state.record_id, state.kind, state.value)
            for collector, state in states.items()
        }

    # ------------------------------------------------------------------ #
    # Record intake.
    # ------------------------------------------------------------------ #
    def add_submission(self, submission: Submission) -> None:
        existing = self._submissions.get(submission.record_id)
        if existing is not None:
            if existing.content_equal(submission):
                return
            raise DuplicateRecordError(
                f"submission {submission.record_id} redefined with different content"
            )
        if submission.record_id in self._tombstones:
            raise PrunedRecordError(
                f"submission {submission.record_id} was archived and cannot return"
            )
        if submission.record_id in self._revocations:
            raise DuplicateRecordError(
                f"record {submission.record_id} is already used by a revocation"
            )
        pending = sorted(
            (
                revocation
                for revocation in self._revocations.values()
                if revocation.target_id == submission.record_id
            ),
            key=lambda item: item.record_id,
        )
        for revocation in pending:
            if revocation.collector != submission.collector:
                raise ValueError(
                    f"revocation {revocation.record_id} targets another "
                    f"collector's submission {submission.record_id}"
                )
        self._assert_no_rogue_after(submission.record_id)
        self._submissions[submission.record_id] = submission
        self._install_submission(submission)
        # A late-arriving target may activate revocations that arrived first.
        for revocation in pending:
            self._apply_revocation(revocation)
        self._refresh_sealed()
        self._generation += 1

    def add_revocation(self, revocation: Revocation) -> None:
        existing = self._revocations.get(revocation.record_id)
        if existing is not None:
            if existing.content_equal(revocation):
                return
            raise DuplicateRecordError(
                f"revocation {revocation.record_id} redefined with different content"
            )
        if revocation.record_id in self._tombstones:
            raise PrunedRecordError(
                f"revocation {revocation.record_id} was archived and cannot return"
            )
        if revocation.record_id in self._submissions:
            raise DuplicateRecordError(
                f"record {revocation.record_id} is already used by a submission"
            )
        if revocation.target_id in self._sealed:
            raise SealedTargetError(
                f"submission {revocation.target_id} is sealed and cannot be revoked"
            )
        target = self._submissions.get(revocation.target_id)
        if target is not None and target.collector != revocation.collector:
            raise ValueError(
                f"revocation {revocation.record_id} targets another collector's "
                f"submission {revocation.target_id}"
            )
        self._assert_no_rogue_after(revocation.record_id)
        self._revocations[revocation.record_id] = revocation
        if target is not None:
            self._apply_revocation(revocation)
        self._refresh_sealed()
        self._generation += 1

    def add_sealing_receipt(self, receipt: SealingReceipt) -> None:
        existing = self._receipts.get(receipt.receipt_id)
        if existing is not None:
            if not existing.content_equal(receipt):
                raise ConflictingRecordError(
                    f"sealing receipt {receipt.receipt_id} redefined"
                )
            return
        stored = SealingReceipt(receipt.receipt_id, receipt.named)
        stored.completed = receipt.completed
        available = (
            self._submissions.keys()
            | self._revocations.keys()
            | self._tombstones
        )
        future_sealed = set(self._sealed)
        if receipt.named.issubset(available):
            future_sealed.update(receipt.named)
        rogue = self._rogue_revocation(future_sealed)
        if rogue is not None:
            raise SealedTargetError(
                f"revocation {rogue.record_id} targets sealed submission "
                f"{rogue.target_id} but is not part of the sealing batch"
            )
        self._receipts[receipt.receipt_id] = stored
        self._refresh_sealed()
        self._generation += 1

    # ------------------------------------------------------------------ #
    # Revocation mechanics.
    # ------------------------------------------------------------------ #
    def _install_submission(self, submission: Submission) -> None:
        for cell, (kind, value) in submission.observations:
            states = self._cells.setdefault(cell, {})
            current = states.get(submission.collector)
            if current is None or submission.record_id > current.record_id:
                states[submission.collector] = _SourceState(
                    submission.record_id, kind, value
                )

    def _apply_revocation(
        self,
        revocation: Revocation,
        retained: Mapping[CellKey, Mapping[str, _SourceState]] | None = None,
    ) -> None:
        target = self._submissions.get(revocation.target_id)
        if target is None or target.collector != revocation.collector:
            return
        for cell in revocation.cells:
            states = self._cells.get(cell)
            if states is None:
                continue
            current = states.get(revocation.collector)
            if current is None or current.record_id != revocation.target_id:
                continue
            predecessor = self._predecessor(
                cell, revocation.collector, revocation.target_id, retained
            )
            if predecessor is None:
                del states[revocation.collector]
                if not states:
                    del self._cells[cell]
            else:
                record_id, (kind, value) = predecessor
                states[revocation.collector] = _SourceState(record_id, kind, value)

    def _predecessor(
        self,
        cell: CellKey,
        collector: str,
        target_id: int,
        retained: Mapping[CellKey, Mapping[str, _SourceState]] | None = None,
    ) -> tuple[int, Observation] | None:
        """Latest still-effective earlier submission of this collector/cell."""
        revoked_targets = {
            revocation.target_id
            for revocation in self._revocations.values()
            if revocation.collector == collector and cell in revocation.cells
        }
        candidates: list[tuple[int, Observation]] = []
        for submission in self._submissions.values():
            if (
                submission.collector == collector
                and submission.record_id < target_id
                and submission.record_id not in revoked_targets
            ):
                observation = submission.observation_at(cell)
                if observation is not None:
                    candidates.append((submission.record_id, observation))
        if retained is not None:
            seed_state = retained.get(cell, {}).get(collector)
            if (
                seed_state is not None
                and seed_state.record_id < target_id
                and seed_state.record_id not in revoked_targets
            ):
                candidates.append(
                    (seed_state.record_id, (seed_state.kind, seed_state.value))
                )
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])

    # ------------------------------------------------------------------ #
    # Sealing.
    # ------------------------------------------------------------------ #
    def _refresh_sealed(self) -> None:
        available = (
            self._submissions.keys() | self._revocations.keys() | self._tombstones
        )
        for receipt in self._receipts.values():
            if receipt.named.issubset(available):
                # Idempotent: also heals a receipt whose flag was set before its
                # sealed ids reached this ledger (e.g. merged snapshots).
                receipt.completed = True
                self._sealed.update(receipt.named)
        rogue = self._rogue_revocation(self._sealed)
        if rogue is not None:
            raise SealedTargetError(
                f"revocation {rogue.record_id} targets sealed submission "
                f"{rogue.target_id} but is not part of the sealing batch"
            )

    def _rogue_revocation(
        self, sealed: set[int]
    ) -> Revocation | None:
        for revocation in self._revocations.values():
            if (
                revocation.record_id not in sealed
                and revocation.target_id in sealed
            ):
                return revocation
        return None

    def _assert_no_rogue_after(self, *extra_available: int) -> None:
        available = (
            self._submissions.keys()
            | self._revocations.keys()
            | self._tombstones
            | set(extra_available)
        )
        future_sealed = set(self._sealed)
        for receipt in self._receipts.values():
            if receipt.named.issubset(available):
                future_sealed.update(receipt.named)
        rogue = self._rogue_revocation(future_sealed)
        if rogue is not None:
            raise SealedTargetError(
                f"revocation {rogue.record_id} targets sealed submission "
                f"{rogue.target_id} but is not part of the sealing batch"
            )

    # ------------------------------------------------------------------ #
    # Archiving.
    # ------------------------------------------------------------------ #
    def prune_archived(self) -> tuple[int, ...]:
        """Drop history sealed by complete receipts.

        Current per-cell values are retained; only records that can never
        participate again are removed, recorded as tombstones. Returns the
        pruned record ids.
        """
        self._refresh_sealed()
        pruned = tuple(sorted(self._sealed - self._tombstones))
        if not pruned:
            return pruned
        for record_id in pruned:
            self._submissions.pop(record_id, None)
            self._revocations.pop(record_id, None)
            self._tombstones.add(record_id)
        self._receipts = {
            receipt_id: receipt
            for receipt_id, receipt in self._receipts.items()
            if not receipt.completed
        }
        # Replay the surviving ledger on top of the retained current values.
        self._rebuild_cells(seed=self._cells)
        self._generation += 1
        return pruned

    def _rebuild_cells(
        self,
        seed: Mapping[CellKey, Mapping[str, _SourceState]],
    ) -> None:
        retained = {
            cell: {collector: state.copy() for collector, state in states.items()}
            for cell, states in seed.items()
        }
        self._cells = {
            cell: {collector: state.copy() for collector, state in states.items()}
            for cell, states in retained.items()
        }
        for submission in sorted(
            self._submissions.values(), key=lambda item: item.record_id
        ):
            self._install_submission(submission)
        for revocation in sorted(
            self._revocations.values(), key=lambda item: item.record_id
        ):
            self._apply_revocation(revocation, retained=retained)

    # ------------------------------------------------------------------ #
    # Merge.
    # ------------------------------------------------------------------ #
    @classmethod
    def merge(cls, left: IngestProgress, right: IngestProgress) -> IngestProgress:
        """Merge two progresses; order-independent and side-effect free."""
        merged = cls()

        merged._tombstones = set(left._tombstones) | set(right._tombstones)
        merged._sealed = set(left._sealed) | set(right._sealed)

        kind_overlap = (
            set(left._submissions) & set(right._revocations)
        ) | (set(right._submissions) & set(left._revocations))
        if kind_overlap:
            raise ConflictingRecordError(
                f"record id(s) {sorted(kind_overlap)} used as both submission "
                "and revocation"
            )

        submissions = cls._merge_records(
            left._submissions, right._submissions, merged._tombstones, "submission"
        )
        revocations = cls._merge_records(
            left._revocations, right._revocations, merged._tombstones, "revocation"
        )
        merged._submissions = submissions
        merged._revocations = revocations
        merged._receipts = cls._merge_receipts(left._receipts, right._receipts)

        seed = cls._merge_cell_seed(
            left._cells,
            right._cells,
            frozenset(left._tombstones),
            frozenset(right._tombstones),
            merged._tombstones,
        )
        merged._declared_stations = tuple(
            sorted(set(left._declared_stations) | set(right._declared_stations))
        )
        merged._declared_times = tuple(
            sorted(set(left._declared_times) | set(right._declared_times))
        )
        merged._declared_variables = tuple(
            sorted(set(left._declared_variables) | set(right._declared_variables))
        )
        merged._rebuild_cells(seed)
        merged._refresh_sealed()
        merged._generation = left._generation + right._generation + 1
        return merged

    @staticmethod
    def _merge_records(
        left: Mapping[int, Any],
        right: Mapping[int, Any],
        tombstones: set[int],
        kind: str,
    ) -> dict[int, Any]:
        combined: dict[int, Any] = {}
        for record_id in sorted(set(left) | set(right)):
            in_left = record_id in left
            in_right = record_id in right
            if in_left and in_right:
                if not left[record_id].content_equal(right[record_id]):
                    raise ConflictingRecordError(
                        f"{kind} {record_id} differs between progresses"
                    )
                record = left[record_id]
            else:
                record = left[record_id] if in_left else right[record_id]
            if record_id in tombstones:
                # Archived history must never come back through an old snapshot.
                continue
            combined[record_id] = record
        return combined

    @staticmethod
    def _merge_receipts(
        left: Mapping[int, SealingReceipt],
        right: Mapping[int, SealingReceipt],
    ) -> dict[int, SealingReceipt]:
        combined: dict[int, SealingReceipt] = {}
        for receipt_id in sorted(set(left) | set(right)):
            if receipt_id in left and receipt_id in right:
                left_receipt = left[receipt_id]
                right_receipt = right[receipt_id]
                if not left_receipt.content_equal(right_receipt):
                    raise ConflictingRecordError(
                        f"sealing receipt {receipt_id} differs between progresses"
                    )
                source = left_receipt
                completed = left_receipt.completed or right_receipt.completed
            elif receipt_id in left:
                source = left[receipt_id]
                completed = source.completed
            else:
                source = right[receipt_id]
                completed = source.completed
            receipt = SealingReceipt(source.receipt_id, source.named)
            receipt.completed = completed
            combined[receipt_id] = receipt
        return combined

    @staticmethod
    def _merge_cell_seed(
        left: Mapping[CellKey, dict[str, _SourceState]],
        right: Mapping[CellKey, dict[str, _SourceState]],
        left_tombstones: frozenset[int],
        right_tombstones: frozenset[int],
        tombstones: set[int],
    ) -> dict[CellKey, dict[str, _SourceState]]:
        seed: dict[CellKey, dict[str, _SourceState]] = {}
        for cell in set(left) | set(right):
            states: dict[str, _SourceState] = {}
            left_states = left.get(cell, {})
            right_states = right.get(cell, {})
            for collector in set(left_states) | set(right_states):
                chosen = IngestProgress._choose_seed_state(
                    left_states.get(collector),
                    right_states.get(collector),
                    left_tombstones,
                    right_tombstones,
                    tombstones,
                    cell,
                    collector,
                )
                if chosen is not None:
                    states[collector] = chosen.copy()
            if states:
                seed[cell] = states
        return seed

    @staticmethod
    def _choose_seed_state(
        left_state: _SourceState | None,
        right_state: _SourceState | None,
        left_tombstones: frozenset[int],
        right_tombstones: frozenset[int],
        tombstones: set[int],
        cell: CellKey,
        collector: str,
    ) -> _SourceState | None:
        if left_state is None:
            return right_state
        if right_state is None:
            return left_state
        if left_state.equals(right_state):
            return left_state
        if left_state.record_id == right_state.record_id:
            raise ConflictingRecordError(
                f"retained current value for {cell}/{collector} differs"
            )
        left_dead = left_state.record_id in tombstones
        right_dead = right_state.record_id in tombstones
        if not left_dead and not right_dead:
            # Both records stay in the ledger; canonical replay decides.
            return (
                left_state
                if left_state.record_id > right_state.record_id
                else right_state
            )
        if left_dead and not right_dead:
            # A live later delivery beats archived history.
            return right_state
        if right_dead and not left_dead:
            return left_state
        left_retained = left_state.record_id in left_tombstones
        right_retained = right_state.record_id in right_tombstones
        if left_retained and not right_retained:
            # The pruning side keeps the authoritative post-revocation value;
            # the other side is a stale snapshot predating the revocation.
            return left_state
        if right_retained and not left_retained:
            return right_state
        raise ConflictingRecordError(
            f"archived current values for {cell}/{collector} differ"
        )

    # ------------------------------------------------------------------ #
    # Consensus view.
    # ------------------------------------------------------------------ #
    def view(self, variable: str) -> GridView:
        return consensus_grid(self, variable)


def consensus_grid(progress: IngestProgress, variable: str) -> GridView:
    """Compute the consensus :class:`GridView` for one variable.

    Cells with no collector are uncovered. A cell where every covering
    collector explicitly reports missing is missing. Otherwise all covering
    collectors must agree on one valid value (zero included); any disagreement,
    including a missing marker against a value, is a conflict whose parties and
    current values are reported instead of a consensus value.
    """
    stations = progress.stations
    times = progress.times
    shape = (len(stations), len(times))
    status = np.full(shape, STATE_UNCOVERED, dtype=np.int8)
    values = np.full(shape, math.nan, dtype=np.float64)
    contributor_count = np.zeros(shape, dtype=np.int32)
    contributors: np.ndarray = np.empty(shape, dtype=object)
    coverage: dict[str, np.ndarray] = {}

    station_index = {station: index for index, station in enumerate(stations)}
    time_index = {time: index for index, time in enumerate(times)}
    for cell, states in progress._cells.items():
        station, time, cell_variable = cell
        if cell_variable != variable:
            continue
        row = station_index[station]
        column = time_index[time]
        entries = tuple(
            sorted(
                (collector, state.record_id, state.kind, state.value)
                for collector, state in states.items()
            )
        )
        contributors[row, column] = entries
        contributor_count[row, column] = len(entries)
        kinds = {kind for _, _, kind, _ in entries}
        if kinds == {OBS_MISSING}:
            status[row, column] = STATE_MISSING
        elif OBS_MISSING in kinds:
            status[row, column] = STATE_CONFLICT
        else:
            unique: list[float] = []
            for _, _, _, value in entries:
                if not any(_same_float(value, seen) for seen in unique):
                    unique.append(value)
            if len(unique) == 1:
                status[row, column] = STATE_VALUE
                values[row, column] = unique[0]
            else:
                status[row, column] = STATE_CONFLICT

    for row in range(shape[0]):
        for column in range(shape[1]):
            if contributors[row, column] is None:
                contributors[row, column] = ()

    all_collectors = sorted(
        {
            collector
            for states in progress._cells.values()
            for collector in states
        }
    )
    for collector in all_collectors:
        covered = np.zeros(shape, dtype=bool)
        for station, time, cell_variable in progress._cells:
            if cell_variable != variable:
                continue
            if collector in progress._cells[(station, time, cell_variable)]:
                covered[station_index[station], time_index[time]] = True
        coverage[collector] = covered

    return GridView(
        variable=variable,
        stations=stations,
        times=times,
        status=status,
        value=values,
        contributor_count=contributor_count,
        contributors=contributors,
        coverage=coverage,
    )


def merge_progresses(progresses: Sequence[IngestProgress]) -> IngestProgress:
    """Merge a sequence of progresses pairwise (left fold)."""
    if not progresses:
        return IngestProgress()
    merged = progresses[0].copy()
    for progress in progresses[1:]:
        merged = IngestProgress.merge(merged, progress)
    return merged
