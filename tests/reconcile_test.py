from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

import pytest

import narwhals as nw
import narwhals.reconcile as rec
from narwhals.reconcile import (
    ConflictingOccurrenceError,
    DeviceLedger,
    MissingTimePolicy,
    Occurrence,
    reconcile,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

UTC = timezone.utc
BACKENDS = ["pandas", "polars"]


def _at(day: int, hour: int, minute: int = 0, offset_hours: int = 0) -> datetime:
    base = datetime(2024, 1, day, hour, minute, tzinfo=UTC)
    return base.astimezone(timezone(timedelta(hours=offset_hours)))


# Source arrival group 1.
SHARD_1 = [
    # Current winner before the withdrawal.
    Occurrence("d1", "d1-w", _at(1, 12, 30), 3, 90, {"payload": "w"}, 1),
    # Same instant, same rank as d1-a, but a larger original input sequence.
    Occurrence("d1", "d1-b", _at(1, 12), 3, 70, {"payload": "b"}, 2),
    # Earlier instant, regardless of the arrival sequence.
    Occurrence("d1", "d1-e", _at(1, 11), 1, 20, {"payload": "e"}, 3),
    Occurrence("d2", "d2-a", _at(2, 9), 2, 200, {"payload": "a"}, 4),
    # Only occurrence for d3, with no effective time.
    Occurrence("d3", "d3-only", None, 1, 300, {"payload": "only"}, 5),
]

# Source arrival group 2, but completion order below is deliberately reversed.
SHARD_2 = [
    # Later arrival, yet a smaller original input sequence than d1-b.
    Occurrence("d1", "d1-a", _at(1, 12), 3, 50, {"payload": "a"}, 6),
    # Same absolute instant as d1-a/d1-b, different timezone and lower rank.
    Occurrence("d1", "d1-t", _at(1, 12, 0, offset_hours=1), 1, 10, {"payload": "t"}, 7),
    Occurrence("d1", "d1-c", None, 9, 95, {"payload": "c"}, 8),
    # Same instant and rank as d2-a, broken by original input sequence.
    Occurrence("d2", "d2-b", _at(2, 9, 0, offset_hours=2), 2, 201, {"payload": "b"}, 9),
    Occurrence("d2", "d2-c", _at(2, 8), 9, 202, {"payload": "c"}, 10),
]

# Backfill that arrives after both groups; its instant sits between d1-e and d1-a.
LATE = Occurrence("d1", "d1-late", _at(1, 11, 30), 2, 80, {"payload": "late"}, 11)

WITHDRAWN = "d1-w"

INITIAL_WINNERS = {"d1": "d1-w", "d2": "d2-b", "d3": "d3-only"}
FINAL_WINNERS = {"d1": "d1-b", "d2": "d2-b", "d3": "d3-only"}


def _rows(frame: Any) -> list[dict[str, Any]]:
    data = frame.to_dict(as_series=False)
    length = len(next(iter(data.values()), []))
    return [
        {name: values[index] for name, values in data.items()} for index in range(length)
    ]


def _is_null(value: Any) -> bool:
    if value is None:
        return True
    return bool(value != value)  # noqa: PLR0124 - NaN/NaT are never equal to themselves


def _normalized(frame: Any) -> list[dict[str, Any]]:
    return [
        {name: None if _is_null(value) else value for name, value in row.items()}
        for row in _rows(frame)
    ]


def _by_occurrence(frame: Any) -> dict[str, dict[str, Any]]:
    return {row["occurrence_id"]: row for row in _normalized(frame)}


def _selected(result: Any) -> dict[str, str]:
    return {row["device_id"]: row["occurrence_id"] for row in _rows(result.selected)}


def _backend_frame(backend: str, data: Mapping[str, Any]) -> Any:
    if backend == "pandas":
        import pandas as pd

        return pd.DataFrame(data)
    import polars as pl

    return pl.DataFrame(data)


def _full_ledger(backend: str) -> DeviceLedger:
    # Group 2 completes first; group 1 arrives first.
    shard_2 = DeviceLedger(SHARD_2, backend=backend)
    shard_1 = DeviceLedger(SHARD_1, backend=backend)
    return shard_2.merge(shard_1)


@pytest.mark.parametrize("backend", BACKENDS)
def test_reversed_merge_and_equal_instant_tiebreaks(backend: str) -> None:
    result = _full_ledger(backend).reconcile()
    assert _selected(result) == INITIAL_WINNERS

    rows = _by_occurrence(result.decisions)
    assert rows["d1-w"]["reason"] == "selected"
    assert rows["d1-b"]["reason"] == "earlier_effective_at"
    assert rows["d1-t"]["reason"] == "earlier_effective_at"
    assert rows["d2-a"]["reason"] == "lower_input_seq"
    assert rows["d2-c"]["reason"] == "earlier_effective_at"
    assert rows["d3-only"]["reason"] == "selected_missing_time"

    # d1-b and d1-a share the exact instant and rank, so the larger *original*
    # input sequence wins even though d1-a arrived later.
    assert rows["d1-b"]["effective_instant_us"] == rows["d1-a"]["effective_instant_us"]
    assert rows["d1-b"]["source_rank"] == rows["d1-a"]["source_rank"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_withdrawal_falls_back_to_sequence_tiebreak(backend: str) -> None:
    result = _full_ledger(backend).add_withdrawals([WITHDRAWN]).reconcile()
    rows = _by_occurrence(result.decisions)

    assert _selected(result)["d1"] == "d1-b"
    assert rows[WITHDRAWN]["withdrawn"] is True
    assert rows[WITHDRAWN]["selected"] is False
    assert rows[WITHDRAWN]["reason"] == "withdrawn"
    assert rows["d1-a"]["reason"] == "lower_input_seq"
    # The withdrawal points at one occurrence only.
    assert _selected(result)["d2"] == "d2-b"


@pytest.mark.parametrize("backend", BACKENDS)
def test_late_arrival_is_not_latest(backend: str) -> None:
    result = (
        _full_ledger(backend).add_withdrawals([WITHDRAWN]).add_records([LATE]).reconcile()
    )
    rows = _by_occurrence(result.decisions)
    assert _selected(result) == FINAL_WINNERS
    assert rows["d1-late"]["arrival_seq"] == 11.0
    assert rows["d1-late"]["reason"] == "earlier_effective_at"


@pytest.mark.parametrize("backend", BACKENDS)
def test_missing_time_policies(backend: str) -> None:
    ledger = _full_ledger(backend).add_withdrawals([WITHDRAWN]).add_records([LATE])

    rank_last = ledger.reconcile(MissingTimePolicy.RANK_LAST)
    assert _selected(rank_last)["d3"] == "d3-only"
    rows = _by_occurrence(rank_last.decisions)
    assert rows["d3-only"]["reason"] == "selected_missing_time"
    assert rows["d1-c"]["reason"] == "missing_time"

    reject = ledger.reconcile(MissingTimePolicy.REJECT)
    assert "d3" not in _selected(reject)
    rows = _by_occurrence(reject.decisions)
    assert rows["d3-only"]["selected"] is False
    assert rows["d3-only"]["reason"] == "missing_time_rejected"
    assert rows["d1-c"]["reason"] == "missing_time_rejected"


@pytest.mark.parametrize("backend", BACKENDS)
def test_missing_time_is_never_filled(backend: str) -> None:
    result = _full_ledger(backend).reconcile()
    row = _by_occurrence(result.selected)["d3-only"]
    assert row["effective_at"] is None
    assert row["has_effective_time"] is False
    assert row["effective_instant_us"] is None
    assert row["reason"] == "selected_missing_time"


@pytest.mark.parametrize("backend", BACKENDS)
def test_incremental_matches_full_replay(backend: str) -> None:
    incremental = DeviceLedger(backend=backend)
    for record in [*SHARD_2, *SHARD_1]:
        incremental = incremental.add_records([record])
    incremental = incremental.add_withdrawals([WITHDRAWN]).add_records([LATE])
    replay = _full_ledger(backend).add_withdrawals([WITHDRAWN]).add_records([LATE])
    for policy in MissingTimePolicy:
        left = incremental.reconcile(policy)
        right = replay.reconcile(policy)
        assert _normalized(left.selected) == _normalized(right.selected)
        assert _normalized(left.excluded) == _normalized(right.excluded)
        assert _normalized(left.decisions) == _normalized(right.decisions)


@pytest.mark.parametrize("backend", BACKENDS)
def test_withdrawal_replay_is_idempotent(backend: str) -> None:
    once = (
        _full_ledger(backend).add_withdrawals([WITHDRAWN]).add_records([LATE]).reconcile()
    )
    twice = (
        _full_ledger(backend)
        .add_withdrawals([WITHDRAWN])
        .add_withdrawals([WITHDRAWN])
        .add_records([LATE])
        .withdraw(WITHDRAWN)
        .reconcile()
    )
    assert _normalized(once.decisions) == _normalized(twice.decisions)
    assert once.dangling_withdrawals == twice.dangling_withdrawals == ()


@pytest.mark.parametrize("backend", BACKENDS)
def test_frames_are_reusable_on_both_backends(backend: str) -> None:
    rows = [
        {
            "device_id": record.device_id,
            "occurrence_id": record.occurrence_id,
            "effective_at": record.effective_at,
            "source_rank": record.source_rank,
            "input_seq": record.input_seq,
            "arrival_seq": record.arrival_seq,
            "payload": record.payload["payload"],
        }
        for record in [*SHARD_2, *SHARD_1, LATE]
    ]
    data: dict[str, list[Any]] = {name: [] for name in rows[0]}
    for row in rows:
        for name, value in row.items():
            data[name].append(value)
    frame = _backend_frame(backend, data)
    result = reconcile(frame, withdrawals=[WITHDRAWN], policy="rank_last")
    assert (
        result.selected.implementation.is_pandas()
        or result.selected.implementation.is_polars()
    )
    assert _selected(result) == FINAL_WINNERS


@pytest.mark.parametrize("backend", BACKENDS)
def test_conflicting_occurrence_id_raises(backend: str) -> None:
    conflicting = Occurrence("d1", "d1-w", _at(1, 12), 3, 90, {"payload": "x"}, 1)
    with pytest.raises(ConflictingOccurrenceError):
        DeviceLedger([*SHARD_1, conflicting], backend=backend)


@pytest.mark.parametrize("backend", BACKENDS)
def test_dangling_withdrawal_is_reported(backend: str) -> None:
    result = DeviceLedger(SHARD_1, backend=backend).reconcile(MissingTimePolicy.RANK_LAST)
    assert result.policy is MissingTimePolicy.RANK_LAST
    missing = DeviceLedger(SHARD_1, backend=backend).add_withdrawals(["nope"])
    assert missing.dangling_withdrawals() == ("nope",)
    assert missing.reconcile().dangling_withdrawals == ("nope",)


@pytest.mark.parametrize("backend", BACKENDS)
def test_naive_datetime_is_utc(backend: str) -> None:
    naive = Occurrence("dx", "dx-1", datetime(2024, 1, 5, 6), 1, 1, {"payload": "n"}, 1)
    aware = Occurrence("dx", "dx-2", _at(5, 6, offset_hours=2), 1, 2, {"payload": "a"}, 2)
    ledger = DeviceLedger([naive, aware], backend=backend)
    assert naive.effective_at == aware.effective_at
    assert naive.instant_us == aware.instant_us
    result = ledger.reconcile()
    assert _selected(result) == {"dx": "dx-2"}


def test_public_namespace_has_no_new_exports() -> None:
    assert "reconcile" not in nw.__all__


@pytest.mark.parametrize("backend", BACKENDS)
def test_reconcile_accepts_ledgers_and_frames(backend: str) -> None:
    frame = _backend_frame(
        backend,
        {
            "device_id": ["d9", "d9"],
            "occurrence_id": ["a", "b"],
            "effective_at": [_at(1, 1), _at(1, 2)],
            "source_rank": [1, 1],
            "input_seq": [1, 2],
        },
    )
    result = rec.reconcile_devices(
        [DeviceLedger(SHARD_1, backend=backend), frame], backend=backend
    )
    assert _selected(result)["d9"] == "b"
    assert _selected(result)["d1"] == "d1-w"


@pytest.mark.parametrize("backend", BACKENDS)
def test_merge_ledgers_helper(backend: str) -> None:
    merged = rec.merge_ledgers(
        DeviceLedger(SHARD_1, backend=backend),
        DeviceLedger(SHARD_2, backend=backend),
        DeviceLedger(backend=backend).add_withdrawals([WITHDRAWN]),
    )
    assert _selected(merged.reconcile())["d1"] == "d1-b"


def test_rejects_bad_arguments() -> None:
    with pytest.raises(ValueError, match="policy"):
        rec.reconcile([], backend="pandas", policy="soonest")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="reserved"):
        Occurrence("d", "o", None, 1, 1, payload={"reason": "x"})
    with pytest.raises(ValueError, match="effective_at"):
        Occurrence("d", "o", "not-a-date", 1, 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="source_rank"):
        Occurrence("d", "o", None, 1.5, 1)  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="occurrence_id"):
        Occurrence("d", "", None, 1, 1)
    with pytest.raises(ValueError, match="required reconciliation"):
        rec.reconcile(_backend_frame("pandas", {"device_id": ["d1"]}), backend="pandas")


def test_numpy_integers_are_accepted() -> None:
    np = pytest.importorskip("numpy")
    record = Occurrence(
        "d", "o", _at(1, 1), np.int64(3), np.int32(4), {"payload": "p"}, np.int64(5)
    )
    assert record.source_rank == 3
    assert record.input_seq == 4
    assert record.arrival_seq == 5


def _normalized_result(result: Any) -> dict[str, list[dict[str, Any]]]:
    return {
        "selected": _normalized(result.selected),
        "excluded": _normalized(result.excluded),
        "decisions": _normalized(result.decisions),
    }


@pytest.mark.parametrize("policy", list(MissingTimePolicy))
def test_backends_produce_identical_results(policy: MissingTimePolicy) -> None:
    results = {}
    for backend in BACKENDS:
        results[backend] = _normalized_result(
            _full_ledger(backend)
            .add_withdrawals([WITHDRAWN])
            .add_records([LATE])
            .reconcile(policy)
        )
    assert results["pandas"] == results["polars"]


@pytest.mark.parametrize("backend", BACKENDS)
def test_all_withdrawn_device_has_no_selection(backend: str) -> None:
    ledger = DeviceLedger(SHARD_1, backend=backend).add_withdrawals(
        ["d1-w", "d1-b", "d1-e"]
    )
    result = ledger.reconcile()
    assert "d1" not in _selected(result)
    reasons = {
        row["occurrence_id"]: row["reason"]
        for row in _normalized(result.decisions)
        if row["device_id"] == "d1"
    }
    assert reasons == {"d1-w": "withdrawn", "d1-b": "withdrawn", "d1-e": "withdrawn"}


@pytest.mark.parametrize("backend", BACKENDS)
def test_missing_against_missing_is_ranked_not_missing(backend: str) -> None:
    ledger = DeviceLedger(
        [
            Occurrence("d", "a", None, 1, 1),
            Occurrence("d", "b", None, 2, 2),
            Occurrence("e", "c", _at(1, 1), 1, 1),
            Occurrence("e", "d", None, 9, 9),
        ],
        backend=backend,
    )
    reasons = {
        row["occurrence_id"]: row["reason"]
        for row in _normalized(ledger.reconcile().decisions)
    }
    assert reasons == {
        "a": "lower_source_rank",
        "b": "selected_missing_time",
        "c": "selected",
        "d": "missing_time",
    }


@pytest.mark.parametrize("backend", BACKENDS)
def test_iso_string_timestamps_are_absolute(backend: str) -> None:
    frame = _backend_frame(
        backend,
        {
            "device_id": ["d", "d", "e"],
            "occurrence_id": ["a", "b", "c"],
            "effective_at": [
                "2024-01-01T12:00:00+00:00",
                "2024-01-01T13:00:00+01:00",
                None,
            ],
            "source_rank": [1, 2, 1],
            "input_seq": [10, 11, 12],
        },
    )
    result = reconcile(frame)
    rows = _by_occurrence(result.decisions)
    assert rows["a"]["effective_instant_us"] == rows["b"]["effective_instant_us"]
    assert _selected(result) == {"d": "b", "e": "c"}


@pytest.mark.parametrize("backend", BACKENDS)
def test_decisions_partition_selected_and_excluded(backend: str) -> None:
    result = (
        _full_ledger(backend)
        .add_withdrawals([WITHDRAWN])
        .add_records([LATE])
        .reconcile(MissingTimePolicy.RANK_LAST)
    )
    decisions = _normalized(result.decisions)
    assert sorted(map(str, decisions)) == sorted(
        map(str, [*_normalized(result.selected), *_normalized(result.excluded)])
    )
    assert all(row["selected"] for row in _normalized(result.selected))
    assert all(not row["selected"] for row in _normalized(result.excluded))
    assert [row["device_id"] for row in _normalized(result.selected)] == sorted(
        {row["device_id"] for row in decisions}
    )
