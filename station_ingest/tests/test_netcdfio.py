"""Tests for NetCDF persistence of ingest progress."""

from __future__ import annotations

import math
from pathlib import Path

import numpy as np
import pytest
import xarray as xr

from station_ingest import (
    OBS_MISSING,
    OBS_VALUE,
    STATE_MISSING,
    STATE_UNCOVERED,
    IngestProgress,
    Revocation,
    SealingReceipt,
    Submission,
    load_progress,
    save_progress,
)
from station_ingest.netcdfio import FORMAT_NAME, from_dataset, to_dataset

S = "站点-1"
T0, T1, T2 = "2026-01-01T00", "2026-01-01T01", "2026-01-01T02"
TEMP, RH = "temperature", "humidity"


def cell(time: str, variable: str) -> tuple[str, str, str]:
    return S, time, variable


def build_full_progress() -> IngestProgress:
    progress = IngestProgress(stations=[S], times=[T0, T1, T2], variables=[TEMP, RH])
    progress.add_submission(
        Submission(
            1,
            "A",
            {
                cell(T0, TEMP): (OBS_VALUE, 12.0),
            },
        )
    )
    progress.add_submission(
        Submission(
            2,
            "B",
            {
                cell(T0, TEMP): (OBS_VALUE, 12.0),
                cell(T1, TEMP): (OBS_VALUE, 7.0),
                cell(T0, RH): (OBS_VALUE, 55.0),
            },
        )
    )
    progress.add_submission(
        Submission(
            10,
            "C",
            {
                cell(T0, TEMP): (OBS_VALUE, 13.0),
                cell(T1, TEMP): (OBS_VALUE, 0.0),
            },
        )
    )
    progress.add_submission(
        Submission(
            11,
            "C",
            {
                cell(T0, TEMP): (OBS_MISSING, math.nan),
                cell(T2, TEMP): (OBS_VALUE, 21.0),
            },
        )
    )
    progress.add_revocation(Revocation(12, 11, "C", [cell(T0, TEMP)]))
    progress.add_submission(
        Submission(13, "C", {cell(T1, TEMP): (OBS_VALUE, 9.0)})
    )
    progress.add_revocation(Revocation(14, 13, "C", [cell(T1, TEMP)]))
    progress.add_sealing_receipt(SealingReceipt(20, [10, 11, 12, 13, 14]))
    return progress


def assert_views_equal(left: IngestProgress, right: IngestProgress) -> None:
    assert left.stations == right.stations
    assert left.times == right.times
    assert left.variables == right.variables
    for variable in left.variables:
        left_view = left.view(variable)
        right_view = right.view(variable)
        np.testing.assert_array_equal(left_view.status, right_view.status)
        np.testing.assert_array_equal(
            np.nan_to_num(left_view.value, nan=-1.0),
            np.nan_to_num(right_view.value, nan=-1.0),
        )
        np.testing.assert_array_equal(
            left_view.contributor_count, right_view.contributor_count
        )
        assert left_view.conflict_details() == right_view.conflict_details()


def test_round_trip_empty_progress(tmp_path: Path) -> None:
    path = tmp_path / "empty.nc"
    save_progress(IngestProgress(), path)
    loaded = load_progress(path)
    assert loaded.submissions == ()
    assert loaded.revocations == ()
    assert loaded.receipts == ()
    assert loaded.tombstones == frozenset()


def test_round_trip_full_ledger_and_views(tmp_path: Path) -> None:
    progress = build_full_progress()
    path = tmp_path / "full.nc"
    save_progress(progress, path)
    loaded = load_progress(path)

    assert {s.record_id for s in loaded.submissions} == {1, 2, 10, 11, 13}
    assert {r.record_id for r in loaded.revocations} == {12, 14}
    assert loaded.sealed_records == frozenset({10, 11, 12, 13, 14})
    assert_views_equal(progress, loaded)

    # Valid zero and explicit missing both survive the float/NaN encoding.
    c_t1 = loaded.source_states(S, T1, TEMP)["C"]
    assert c_t1 == (10, OBS_VALUE, 0.0)
    # Unicode coordinates survive the netCDF3 string encoding.
    assert loaded.stations == [S]


def test_round_trip_after_pruning_keeps_only_tombstones_and_values(
    tmp_path: Path,
) -> None:
    progress = build_full_progress()
    progress.prune_archived()
    path = tmp_path / "pruned.nc"
    save_progress(progress, path)
    loaded = load_progress(path)

    assert loaded.tombstones == frozenset({10, 11, 12, 13, 14})
    assert {s.record_id for s in loaded.submissions} == {1, 2}
    assert loaded.revocations == ()
    assert_views_equal(progress, loaded)

    on_disk = xr.open_dataset(path, engine="scipy")
    try:
        assert on_disk.attrs["format_name"] == FORMAT_NAME
        # Sealed history is not retained as replayable observation rows.
        assert on_disk.sizes["obs"] == sum(
            len(submission.observations)
            for submission in loaded.submissions
        )
    finally:
        on_disk.close()


def test_save_overwrites_existing_file(tmp_path: Path) -> None:
    path = tmp_path / "progress.nc"
    first = IngestProgress(stations=[S], times=[T0], variables=[TEMP])
    first.add_submission(Submission(1, "A", {cell(T0, TEMP): (OBS_VALUE, 1.0)}))
    save_progress(first, path)

    second = IngestProgress(stations=[S], times=[T0, T1], variables=[TEMP])
    second.add_submission(Submission(2, "B", {cell(T1, TEMP): (OBS_VALUE, 2.0)}))
    save_progress(second, path)

    loaded = load_progress(path)
    assert {s.record_id for s in loaded.submissions} == {2}
    assert loaded.times == [T0, T1]


def test_accepts_path_like_and_default_scipy_engine(tmp_path: Path) -> None:
    progress = build_full_progress()
    path = tmp_path / "pathlike.nc"
    save_progress(progress, str(path))
    loaded = load_progress(path)
    assert_views_equal(progress, loaded)


def test_rejects_foreign_dataset(tmp_path: Path) -> None:
    foreign = tmp_path / "foreign.nc"
    xr.Dataset({"x": (("t",), [1.0])}).to_netcdf(foreign, engine="scipy")
    with pytest.raises(ValueError, match="not a station ingest progress"):
        load_progress(foreign)


def test_to_dataset_round_trip_in_memory() -> None:
    progress = build_full_progress()
    rebuilt = from_dataset(to_dataset(progress))
    assert_views_equal(progress, rebuilt)
    assert rebuilt.sealed_records == progress.sealed_records


def test_loaded_files_merge_like_in_memory_progresses(tmp_path: Path) -> None:
    left = IngestProgress(stations=[S], times=[T0], variables=[TEMP])
    left.add_submission(Submission(1, "A", {cell(T0, TEMP): (OBS_VALUE, 12.0)}))
    right = IngestProgress(stations=[S], times=[T0], variables=[TEMP])
    right.add_submission(Submission(2, "B", {cell(T0, TEMP): (OBS_VALUE, 13.0)}))

    left_path = tmp_path / "left.nc"
    right_path = tmp_path / "right.nc"
    save_progress(left, left_path)
    save_progress(right, right_path)

    from_files = IngestProgress.merge(
        load_progress(left_path), load_progress(right_path)
    )
    in_memory = IngestProgress.merge(left, right)
    assert_views_equal(from_files, in_memory)
    assert {s.record_id for s in from_files.submissions} == {1, 2}


def test_declared_grid_persists_uncovered_and_missing_cells(tmp_path: Path) -> None:
    progress = IngestProgress(stations=[S], times=[T0, T1], variables=[TEMP])
    progress.add_submission(
        Submission(1, "C", {cell(T0, TEMP): (OBS_MISSING, math.nan)})
    )
    path = tmp_path / "grid.nc"
    save_progress(progress, path)
    loaded = load_progress(path)

    view = loaded.view(TEMP)
    assert view.status[0, 0] == STATE_MISSING
    assert view.status[0, 1] == STATE_UNCOVERED
    assert loaded.times == [T0, T1]
