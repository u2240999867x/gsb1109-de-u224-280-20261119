"""End-to-end narrative test: split intake, archive, merge and revival guard."""

from __future__ import annotations

import math
import random
from pathlib import Path

import numpy as np
import pytest

from station_ingest import (
    OBS_MISSING,
    OBS_VALUE,
    STATE_CONFLICT,
    STATE_MISSING,
    STATE_UNCOVERED,
    STATE_VALUE,
    IngestProgress,
    Revocation,
    SealedTargetError,
    SealingReceipt,
    Submission,
    load_progress,
    merge_progresses,
    obs_missing,
    obs_value,
    save_progress,
)

STATION = "ST1"
T0, T1, T2 = "2026-01-01T00", "2026-01-01T01", "2026-01-01T02"
TEMP, RH = "temperature", "humidity"

CORE_KEYS = (
    "a_one",
    "b_one",
    "c_10",
    "c_11",
    "rev_12",
    "c_13",
    "rev_14",
    "receipt",
)


def cell(time: str, variable: str) -> tuple[str, str, str]:
    return STATION, time, variable


def make_progress() -> IngestProgress:
    return IngestProgress(
        stations=[STATION], times=[T0, T1, T2], variables=[TEMP, RH]
    )


@pytest.fixture
def records() -> dict[str, object]:
    return {
        "a_one": Submission(1, "A", {cell(T0, TEMP): obs_value(12)}),
        "b_one": Submission(
            2,
            "B",
            {
                cell(T0, TEMP): obs_value(12),
                cell(T1, TEMP): obs_value(7),
                cell(T0, RH): obs_value(55),
            },
        ),
        "c_10": Submission(
            10,
            "C",
            {cell(T0, TEMP): obs_value(13), cell(T1, TEMP): obs_value(0)},
        ),
        "c_11": Submission(
            11,
            "C",
            {cell(T0, TEMP): obs_missing(), cell(T2, TEMP): obs_value(21)},
        ),
        "rev_12": Revocation(12, 11, "C", [cell(T0, TEMP)]),
        "c_13": Submission(13, "C", {cell(T1, TEMP): obs_value(9)}),
        "rev_14": Revocation(14, 13, "C", [cell(T1, TEMP)]),
        "receipt": SealingReceipt(20, [10, 11, 12, 13, 14]),
        # Earlier submission that the receipt never names.
        "c_5": Submission(
            5,
            "C",
            {cell(T0, TEMP): obs_value(999), cell(T1, RH): obs_value(40)},
        ),
    }


def assert_views_match(left_view, right_view) -> None:
    np.testing.assert_array_equal(left_view.status, right_view.status)
    np.testing.assert_array_equal(
        np.nan_to_num(left_view.value, nan=-99999.0),
        np.nan_to_num(right_view.value, nan=-99999.0),
    )
    np.testing.assert_array_equal(
        left_view.contributor_count, right_view.contributor_count
    )
    assert left_view.conflict_details() == right_view.conflict_details()
    assert set(left_view.coverage) == set(right_view.coverage)
    for collector in left_view.coverage:
        np.testing.assert_array_equal(
            left_view.coverage[collector], right_view.coverage[collector]
        )
    for row in range(left_view.status.shape[0]):
        for col in range(left_view.status.shape[1]):
            assert tuple(left_view.contributors[row, col]) == tuple(
                right_view.contributors[row, col]
            )


def assert_final_state(progress: IngestProgress, late_present: bool = False) -> None:
    temp = progress.view(TEMP)
    rh = progress.view(RH)

    assert temp.status[0, 0] == STATE_CONFLICT
    parties = {p["collector"]: p for p in temp.conflict_details()[0]["parties"]}
    assert parties["A"]["value"] == 12 and parties["B"]["value"] == 12
    assert parties["C"]["value"] == 13 and parties["C"]["record_id"] == 10
    assert math.isnan(temp.value[0, 0]) and temp.contributor_count[0, 0] == 3

    assert temp.status[0, 1] == STATE_CONFLICT
    parties = {
        p["collector"]: p for p in temp.conflict_details()[1]["parties"]
    }
    assert parties["B"]["value"] == 7
    assert parties["C"]["value"] == 0 and parties["C"]["record_id"] == 10

    assert temp.status[0, 2] == STATE_VALUE and temp.value[0, 2] == 21

    assert rh.status[0, 0] == STATE_VALUE and rh.value[0, 0] == 55
    if late_present:
        assert rh.status[0, 1] == STATE_VALUE and rh.value[0, 1] == 40
    else:
        assert rh.status[0, 1] == STATE_UNCOVERED
    assert rh.status[0, 2] == STATE_UNCOVERED


def feed(progress: IngestProgress, record: object) -> None:
    if isinstance(record, SealingReceipt):
        progress.add_sealing_receipt(record)
    elif isinstance(record, Revocation):
        progress.add_revocation(record)
    else:
        progress.add_submission(record)  # type: ignore[arg-type]


def test_split_routes_merge_in_either_order_and_match_full_merge(
    tmp_path: Path, records: dict[str, object]
) -> None:
    # Single complete, ordered reference.
    reference = make_progress()
    for key in CORE_KEYS:
        feed(reference, records[key])
    assert_final_state(reference)

    # Route X: 10, 11 and the receipt first.
    route_x = make_progress()
    route_x.add_submission(records["c_10"])  # type: ignore[arg-type]
    route_x.add_submission(records["c_11"])  # type: ignore[arg-type]
    route_x.add_sealing_receipt(records["receipt"])  # type: ignore[arg-type]
    view_x = route_x.view(TEMP)
    assert view_x.status[0, 0] == STATE_MISSING  # explicit missing still active
    assert view_x.value[0, 1] == 0
    path_x = tmp_path / "route_x.nc"
    save_progress(route_x, path_x)

    # Route Y: revocations first (before their targets), then 13, then B.
    route_y = make_progress()
    route_y.add_revocation(records["rev_12"])  # type: ignore[arg-type]
    route_y.add_revocation(records["rev_14"])  # type: ignore[arg-type]
    route_y.add_submission(records["c_13"])  # type: ignore[arg-type]
    route_y.add_submission(records["b_one"])  # type: ignore[arg-type]
    view_y = route_y.view(TEMP)
    # 13 is revoked as soon as it lands; C has nothing at T1 on this route.
    assert view_y.status[0, 1] == STATE_VALUE and view_y.value[0, 1] == 7
    assert view_y.status[0, 0] == STATE_VALUE and view_y.value[0, 0] == 12
    path_y = tmp_path / "route_y.nc"
    save_progress(route_y, path_y)

    old_x = tmp_path / "route_x_old.nc"
    old_y = tmp_path / "route_y_old.nc"
    save_progress(load_progress(path_x), old_x)
    save_progress(load_progress(path_y), old_y)

    # Reopen and merge in both orders; A only arrives via direct intake.
    merged_xy = IngestProgress.merge(load_progress(path_x), load_progress(path_y))
    merged_yx = IngestProgress.merge(load_progress(path_y), load_progress(path_x))
    merged_xy.add_submission(records["a_one"])  # type: ignore[arg-type]
    merged_yx.add_submission(records["a_one"])  # type: ignore[arg-type]
    assert_final_state(merged_xy)
    assert_final_state(merged_yx)
    assert_views_match(merged_xy.view(TEMP), merged_yx.view(TEMP))
    assert_views_match(merged_xy.view(TEMP), reference.view(TEMP))
    assert_views_match(merged_xy.view(RH), reference.view(RH))

    # B's single submission is counted once even when its file is re-merged.
    re_merged = IngestProgress.merge(merged_xy, load_progress(path_y))
    assert re_merged.view(TEMP).contributor_count[0, 0] == 3
    assert len(re_merged.submissions) == len(merged_xy.submissions)
    assert_views_match(re_merged.view(TEMP), merged_xy.view(TEMP))


def test_archive_then_old_snapshots_and_late_records_cannot_revive(
    tmp_path: Path, records: dict[str, object]
) -> None:
    route_x = make_progress()
    for key in ("c_10", "c_11", "receipt"):
        feed(route_x, records[key])
    route_y = make_progress()
    for key in ("rev_12", "rev_14", "c_13", "b_one"):
        feed(route_y, records[key])
    path_x, path_y = tmp_path / "x.nc", tmp_path / "y.nc"
    save_progress(route_x, path_x)
    save_progress(route_y, path_y)
    old_x, old_y = tmp_path / "old_x.nc", tmp_path / "old_y.nc"
    save_progress(load_progress(path_x), old_x)
    save_progress(load_progress(path_y), old_y)

    merged = IngestProgress.merge(load_progress(path_x), load_progress(path_y))
    merged.add_submission(records["a_one"])  # type: ignore[arg-type]
    assert merged.sealed_records == frozenset({10, 11, 12, 13, 14})

    pruned = merged.prune_archived()
    assert tuple(pruned) == (10, 11, 12, 13, 14)
    assert {s.record_id for s in merged.submissions} == {1, 2}
    assert merged.revocations == ()
    assert_final_state(merged)

    archive_path = tmp_path / "archived.nc"
    save_progress(merged, archive_path)
    archived = load_progress(archive_path)
    assert_final_state(archived)
    assert archived.tombstones == frozenset({10, 11, 12, 13, 14})

    # Sealed submissions reject new revocations; fresh coordinates still work.
    with pytest.raises(SealedTargetError):
        archived.add_revocation(Revocation(30, 10, "C", [cell(T0, TEMP)]))
    archived.add_submission(Submission(31, "C", {cell(T0, TEMP): obs_value(18)}))
    parties = {
        p["collector"]: p
        for p in archived.view(TEMP).conflict_details()[0]["parties"]
    }
    assert parties["C"]["value"] == 18 and parties["C"]["record_id"] == 31

    # Every old snapshot, merged in either order, cannot revive revoked content.
    for stale_path in (old_x, old_y):
        forward = IngestProgress.merge(
            load_progress(archive_path), load_progress(stale_path)
        )
        reverse = IngestProgress.merge(
            load_progress(stale_path), load_progress(archive_path)
        )
        for combined in (forward, reverse):
            assert_final_state(combined)
            assert combined.tombstones == frozenset({10, 11, 12, 13, 14})
            assert combined.source_states(STATION, T0, TEMP)["C"] == (
                10,
                OBS_VALUE,
                13.0,
            )
            assert combined.source_states(STATION, T1, TEMP)["C"] == (
                10,
                OBS_VALUE,
                0.0,
            )

    # The unnamed earlier submission may still arrive and only adds its new
    # content; its stale T0 temperature cannot displace the restored chain.
    late = load_progress(archive_path)
    late.add_submission(records["c_5"])  # type: ignore[arg-type]
    assert_final_state(late, late_present=True)
    assert late.view(RH).value[0, 1] == 40
    assert late.source_states(STATION, T0, TEMP)["C"] == (10, OBS_VALUE, 13.0)


def test_shuffled_chunked_two_file_matches_single_merge(
    tmp_path: Path, records: dict[str, object]
) -> None:
    rng = random.Random(20261124)
    chunk_a = make_progress()
    chunk_b = make_progress()
    ordered_keys = [
        "a_one",
        "b_one",
        "c_5",
        "c_10",
        "c_11",
        "rev_12",
        "c_13",
        "rev_14",
        "receipt",
    ]
    shuffled = [records[key] for key in ordered_keys]
    rng.shuffle(shuffled)
    for index, record in enumerate(shuffled):
        target = chunk_a if index % 2 == 0 else chunk_b
        if isinstance(record, Submission):
            shuffled_cells = list(record.observations)
            rng.shuffle(shuffled_cells)
            feed(
                target,
                Submission(
                    record.record_id,
                    record.collector,
                    {cell_key: obs for cell_key, obs in shuffled_cells},
                ),
            )
        else:
            feed(target, record)

    path_a, path_b = tmp_path / "chunk_a.nc", tmp_path / "chunk_b.nc"
    save_progress(chunk_a, path_a)
    save_progress(chunk_b, path_b)

    reference = make_progress()
    for key in ordered_keys:
        feed(reference, records[key])

    from_files = merge_progresses([load_progress(path_b), load_progress(path_a)])
    assert_final_state(from_files, late_present=True)
    assert_views_match(from_files.view(TEMP), reference.view(TEMP))
    assert_views_match(from_files.view(RH), reference.view(RH))
