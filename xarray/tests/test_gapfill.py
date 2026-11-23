from __future__ import annotations

from typing import Any

import numpy as np
import pytest

import xarray as xr
from xarray.core.gapfill import (
    GAP_STATUSES,
    GapChunk,
    MemoryGapStore,
    fill_short_gaps,
    iter_fill_short_gaps,
)
from xarray.tests import assert_equal, requires_cftime


def _example_readings() -> dict[str, dict[str, Any]]:
    return {
        "A": {
            "2001-02-27": np.nan,
            "2001-02-29": 10,
            "2001-03-01": np.nan,
            "2001-03-02": 16,
            "2001-03-04": np.nan,
        },
        "B": {
            "2001-02-27": np.nan,
            "2001-02-29": np.nan,
            "2001-03-01": np.nan,
            "2001-03-02": np.nan,
            "2001-03-04": np.nan,
        },
        "C": {
            "2001-02-27": 4,
            "2001-02-29": np.nan,
            "2001-03-01": np.nan,
            "2001-03-02": 16,
            "2001-03-04": np.nan,
        },
    }


def _status(frame: xr.Dataset) -> dict[tuple[str, str], str]:
    return {
        (str(time), str(station)): str(
            frame["status"].sel(time=time, station=station).item()
        )
        for time in frame["time"].values
        for station in frame["station"].values
    }


def _values(frame: xr.Dataset) -> dict[tuple[str, str], float]:
    return {
        (str(time), str(station)): float(
            frame["value"].sel(time=time, station=station).item()
        )
        for time in frame["time"].values
        for station in frame["station"].values
    }


@requires_cftime
def test_prompt_example_values_and_statuses() -> None:
    store = MemoryGapStore(_example_readings(), chunk_size=1)
    result = fill_short_gaps(
        store,
        "2001-02-30",
        "2001-03-01",
        stations=["A", "B", "C"],
        shutdown={"B": ["2001-02-30"]},
        max_gap=5,
    )

    values = _values(result)
    assert values[("2001-02-30 00:00:00", "A")] == pytest.approx(12)
    assert values[("2001-03-01 00:00:00", "A")] == pytest.approx(14)
    assert values[("2001-02-30 00:00:00", "C")] == pytest.approx(11.2)
    assert values[("2001-03-01 00:00:00", "C")] == pytest.approx(13.6)
    assert np.isnan(values[("2001-02-30 00:00:00", "B")])
    assert np.isnan(values[("2001-03-01 00:00:00", "B")])

    statuses = _status(result)
    assert statuses[("2001-02-30 00:00:00", "A")] == "filled"
    assert statuses[("2001-03-01 00:00:00", "A")] == "filled"
    assert statuses[("2001-02-30 00:00:00", "C")] == "filled"
    assert statuses[("2001-03-01 00:00:00", "C")] == "filled"
    assert statuses[("2001-02-30 00:00:00", "B")] == "shutdown"
    assert statuses[("2001-03-01 00:00:00", "B")] == "missing"

    # Feb 30 has no coordinate at all; Mar 1 does for every station.
    present = {
        (str(time), str(station)): bool(
            result["coordinate_present"].sel(time=time, station=station).item()
        )
        for time in result["time"].values
        for station in result["station"].values
    }
    assert present[("2001-02-30 00:00:00", "A")] is False
    assert present[("2001-02-30 00:00:00", "B")] is False
    assert present[("2001-02-30 00:00:00", "C")] is False
    assert present[("2001-03-01 00:00:00", "A")] is True
    assert present[("2001-03-01 00:00:00", "B")] is True
    assert present[("2001-03-01 00:00:00", "C")] is True


@requires_cftime
def test_unlisted_dates_are_delivered_with_360_day_calendar() -> None:
    readings = {"A": {"2001-02-29": 1.0, "2001-03-03": 5.0}}
    result = fill_short_gaps(readings, "2001-02-29", "2001-03-03", max_gap=4)
    labels = [str(time) for time in result["time"].values]
    assert labels == [
        "2001-02-29 00:00:00",
        "2001-02-30 00:00:00",
        "2001-03-01 00:00:00",
        "2001-03-02 00:00:00",
        "2001-03-03 00:00:00",
    ]
    np.testing.assert_allclose(
        result["value"].values.ravel(),
        [1.0, 2.0, 3.0, 4.0, 5.0],
    )
    assert set(result["status"].values.ravel()) == {"observed", "filled"}


@requires_cftime
def test_endpoint_distance_equal_to_max_gap_is_filled() -> None:
    # Endpoints are five calendar days apart: exactly max_gap.
    readings = {"A": {"2001-02-29": 0.0, "2001-03-04": 10.0}}
    at_limit = fill_short_gaps(readings, "2001-03-01", "2001-03-01", max_gap=5)
    assert at_limit["status"].sel(station="A", time="2001-03-01").item() == "filled"
    # Mar 1 is two days after Feb 29 and three days before Mar 4.
    assert at_limit["value"].sel(
        station="A", time="2001-03-01"
    ).item() == pytest.approx(4.0)

    # Six days apart exceeds the limit.
    readings = {"A": {"2001-02-29": 0.0, "2001-03-05": 10.0}}
    beyond = fill_short_gaps(readings, "2001-03-02", "2001-03-02", max_gap=5)
    assert beyond["status"].sel(station="A", time="2001-03-02").item() == "missing"


@requires_cftime
def test_shutdown_blocks_borrowing_across_it() -> None:
    readings = {
        "A": {"2001-02-29": 1.0, "2001-03-02": 4.0},
        "B": {"2001-02-29": 1.0, "2001-03-02": 4.0},
    }
    result = fill_short_gaps(
        readings,
        "2001-02-29",
        "2001-03-02",
        stations=["A", "B"],
        shutdown={"B": ["2001-03-01"]},
        max_gap=5,
    )
    # A interpolates normally.
    assert result["value"].sel(station="A", time="2001-03-01").item() == pytest.approx(
        3.0
    )
    # B cannot borrow across its shutdown day.
    assert result["status"].sel(station="B", time="2001-03-01").item() == "shutdown"
    assert result["value"].sel(station="B", time="2001-03-02").item() == pytest.approx(
        4.0
    )


@requires_cftime
def test_filled_values_are_not_reused_as_endpoints() -> None:
    # 2 29 observed, then three missing days, then 3 3 observed.
    # Every internal day must interpolate from the two observations, not
    # chain off a previously filled neighbour.
    readings = {"A": {"2001-02-29": 0.0, "2001-03-03": 9.0}}
    result = fill_short_gaps(readings, "2001-02-29", "2001-03-03", max_gap=4)
    np.testing.assert_allclose(
        result["value"].values.ravel(), [0.0, 2.25, 4.5, 6.75, 9.0]
    )


@requires_cftime
def test_no_extrapolation_without_right_endpoint() -> None:
    readings = {"A": {"2001-02-27": 5.0, "2001-02-28": np.nan}}
    result = fill_short_gaps(readings, "2001-02-27", "2001-02-28", max_gap=5)
    assert result["value"].sel(station="A", time="2001-02-27").item() == pytest.approx(
        5.0
    )
    assert result["status"].sel(station="A", time="2001-02-28").item() == "missing"


@requires_cftime
def test_observed_values_are_preserved() -> None:
    readings = {"A": {"2001-02-27": 3.0, "2001-02-29": 7.0}}
    result = fill_short_gaps(readings, "2001-02-27", "2001-02-29", max_gap=5)
    assert result["value"].sel(station="A", time="2001-02-27").item() == pytest.approx(
        3.0
    )
    assert result["value"].sel(station="A", time="2001-02-29").item() == pytest.approx(
        7.0
    )
    assert result["status"].sel(station="A", time="2001-02-27").item() == "observed"


@requires_cftime
def test_result_does_not_depend_on_chunk_boundaries() -> None:
    readings = _example_readings()
    shutdown = {"B": ["2001-02-30"]}

    per_date: list[Any] = [
        ([station], [date]) for station, by_date in readings.items() for date in by_date
    ]
    per_station = [([station], list(by_date)) for station, by_date in readings.items()]

    results = []
    for chunks in (per_date, per_station, None):
        store = (
            MemoryGapStore(readings, chunks=chunks)
            if chunks
            else MemoryGapStore(readings)
        )
        results.append(
            fill_short_gaps(
                store,
                "2001-02-30",
                "2001-03-01",
                stations=["A", "B", "C"],
                shutdown=shutdown,
                max_gap=5,
            )
        )

    expected_values = results[0]["value"].values
    expected_statuses = results[0]["status"].values
    for result in results[1:]:
        np.testing.assert_allclose(result["value"].values, expected_values)
        np.testing.assert_array_equal(result["status"].values, expected_statuses)


@requires_cftime
def test_split_requests_match_single_request() -> None:
    readings = _example_readings()
    shutdown = {"B": ["2001-02-30"]}
    kwargs = dict(stations=["A", "B", "C"], shutdown=shutdown, max_gap=5)
    full = fill_short_gaps(readings, "2001-02-30", "2001-03-01", **kwargs)
    first = fill_short_gaps(readings, "2001-02-30", "2001-02-30", **kwargs)
    second = fill_short_gaps(readings, "2001-03-01", "2001-03-01", **kwargs)
    split = xr.concat([first, second], dim="time")
    np.testing.assert_allclose(split["value"].values, full["value"].values)
    np.testing.assert_array_equal(split["status"].values, full["status"].values)


@requires_cftime
def test_reads_are_minimal_and_deduplicated() -> None:
    readings = _example_readings()
    store = MemoryGapStore(readings, chunk_size=1)
    fill_short_gaps(
        store,
        "2001-02-30",
        "2001-03-01",
        stations=["A", "B", "C"],
        shutdown={"B": ["2001-02-30"]},
        max_gap=5,
    )
    read_dates = {chunk.times[0] for chunk in store.read_log}

    # Station A is confirmed by its own Feb 29 observation, so it never needs
    # the Feb 27 chunk.  No station needs the Mar 4 chunk.
    a_dates = {chunk.times[0] for chunk in store.read_log if chunk.stations == ("A",)}
    assert "2001-02-27" not in a_dates
    assert "2001-02-29" in a_dates
    assert "2001-03-04" not in read_dates

    # Station C needs its Feb 27 observation to close the left side.
    c_dates = {chunk.times[0] for chunk in store.read_log if chunk.stations == ("C",)}
    assert "2001-02-27" in c_dates

    # Station B is fully shutdown on Feb 30 and never borrows, so no
    # out-of-request chunk is required for it.
    b_dates = {chunk.times[0] for chunk in store.read_log if chunk.stations == ("B",)}
    assert b_dates <= {"2001-03-01"}

    # Every raw chunk is read at most once.
    keys = [chunk.key for chunk in store.read_log]
    assert len(keys) == len(set(keys))


@requires_cftime
def test_nearer_chunk_resolved_before_farther_chunk() -> None:
    readings = {
        "A": {
            "2001-02-25": 1.0,
            "2001-02-27": 7.0,
            "2001-02-29": 10.0,
            "2001-03-02": 16.0,
        }
    }
    store = MemoryGapStore(readings, chunk_size=1)
    fill_short_gaps(store, "2001-02-30", "2001-03-01", stations=["A"], max_gap=5)
    read_order = [chunk.times[0] for chunk in store.read_log]
    # Feb 29 resolves the left side immediately, so Feb 27 and Feb 25 are
    # never read.
    assert read_order == ["2001-02-29", "2001-03-02"]


@requires_cftime
def test_shutdown_side_short_circuits_further_search() -> None:
    readings = {
        "A": {
            "2001-02-20": 1.0,
            "2001-02-27": 7.0,
            "2001-02-29": 10.0,
            "2001-03-02": 16.0,
        }
    }
    store = MemoryGapStore(readings, chunk_size=1)
    result = fill_short_gaps(
        store,
        "2001-02-30",
        "2001-02-30",
        stations=["A"],
        shutdown={"A": ["2001-03-01"]},
        max_gap=5,
    )
    # The right side is blocked by the shutdown day, so the gap is undecidable
    # without reading any farther chunk on either side.
    assert result["status"].sel(station="A", time="2001-02-30").item() == "missing"
    assert [chunk.times[0] for chunk in store.read_log] == ["2001-02-29"]


@requires_cftime
def test_stations_discovered_from_chunks() -> None:
    readings = {"A": {"2001-02-29": 1.0}, "B": {"2001-02-29": 2.0}}
    store = MemoryGapStore(readings, chunk_size=1)
    result = fill_short_gaps(store, "2001-02-29", "2001-02-29", max_gap=5)
    assert list(result["station"].values) == ["A", "B"]


@requires_cftime
def test_explicit_coordinates_with_missing_values() -> None:
    # Every request day has a coordinate but no reading, and there is no
    # observation to borrow from.
    store = MemoryGapStore(
        {"B": {}},
        coordinates={"B": ["2001-03-01", "2001-03-02"]},
        chunk_size=1,
    )
    result = fill_short_gaps(
        store, "2001-03-01", "2001-03-02", stations=["B"], max_gap=5
    )
    assert list(result["status"].values.ravel()) == ["missing", "missing"]
    assert list(result["coordinate_present"].values.ravel()) == [True, True]
    assert np.isnan(result["value"].values).all()


@requires_cftime
def test_multi_station_chunk_is_read_once() -> None:
    readings = {
        "A": {"2001-02-29": 1.0, "2001-03-02": 4.0},
        "B": {"2001-02-29": 2.0, "2001-03-02": 5.0},
    }
    shared = GapChunk(
        stations=("A", "B"),
        times=("2001-02-29", "2001-03-02"),
        key="shared",
    )
    store = MemoryGapStore(readings, chunks=[shared])
    result = fill_short_gaps(
        store, "2001-02-29", "2001-03-02", stations=["A", "B"], max_gap=5
    )
    assert store.read_keys.count("shared") == 1
    assert result["value"].sel(station="A", time="2001-03-01").item() == pytest.approx(
        3.0
    )
    assert result["value"].sel(station="B", time="2001-03-01").item() == pytest.approx(
        4.0
    )


@requires_cftime
def test_multi_year_request_is_blocked_by_year() -> None:
    readings = {
        "A": {
            "2000-12-29": 0.0,
            "2001-01-02": 8.0,
            "2001-12-29": 1.0,
            "2002-01-03": 6.0,
        }
    }
    frames = list(
        iter_fill_short_gaps(
            readings, "2000-12-29", "2002-01-03", stations=["A"], max_gap=10
        )
    )
    assert len(frames) == 3
    assert str(frames[0]["time"].values[0]).startswith("2000-12-29")
    assert str(frames[0]["time"].values[-1]).startswith("2000-12-30")
    assert str(frames[1]["time"].values[0]).startswith("2001-01-01")
    assert str(frames[2]["time"].values[0]).startswith("2002-01-01")

    combined = xr.concat(frames, dim="time")
    assert combined.sizes["time"] == 2 + 360 + 3
    assert combined["status"].sel(time="2001-01-01").item() == "filled"
    assert combined["value"].sel(time="2001-01-01").item() == pytest.approx(16.0 / 3.0)

    # A single call with block_size also yields identical values.
    one = fill_short_gaps(
        readings, "2000-12-29", "2002-01-03", stations=["A"], max_gap=10
    )
    np.testing.assert_allclose(one["value"].values, combined["value"].values)


@requires_cftime
def test_block_size_request_matches_whole() -> None:
    readings = _example_readings()
    kwargs = dict(stations=["A", "B", "C"], shutdown={"B": ["2001-02-30"]}, max_gap=5)
    whole = fill_short_gaps(readings, "2001-02-27", "2001-03-04", **kwargs)
    blocked = fill_short_gaps(
        readings, "2001-02-27", "2001-03-04", block_size=2, **kwargs
    )
    np.testing.assert_allclose(blocked["value"].values, whole["value"].values)
    np.testing.assert_array_equal(blocked["status"].values, whole["status"].values)


@requires_cftime
def test_shutdown_dataset_and_boolean_variable() -> None:
    readings = _example_readings()
    times = xr.CFTimeIndex(
        list(
            xr.date_range("2001-02-30", periods=1, calendar="360_day", use_cftime=True)
        )
    )
    shutdown = xr.Dataset(
        {"shutdown": (("time", "station"), np.array([[True]]))},
        coords={"time": times, "station": ["B"]},
    )
    result = fill_short_gaps(
        readings,
        "2001-02-30",
        "2001-03-01",
        stations=["A", "B"],
        shutdown=shutdown,
        max_gap=5,
    )
    assert result["status"].sel(station="B", time="2001-02-30").item() == "shutdown"
    assert result["status"].sel(station="A", time="2001-02-30").item() == "filled"


@requires_cftime
def test_variable_selection_from_dataset_chunks() -> None:
    import xarray as xr

    times = xr.CFTimeIndex(
        [
            xr.date_range("2001-02-29", periods=1, calendar="360_day", use_cftime=True)[
                0
            ],
            xr.date_range("2001-03-02", periods=1, calendar="360_day", use_cftime=True)[
                0
            ],
        ]
    )
    chunk = xr.Dataset(
        {
            "temp": (("station", "time"), [[10.0, 16.0]]),
            "quality": (("station", "time"), [[1, 2]]),
        },
        coords={"station": ["A"], "time": times},
    )
    handle = GapChunk(stations=("A",), times=tuple(times), key="multi", data=chunk)
    store = MemoryGapStore({"A": {}}, chunks=[handle])
    result = fill_short_gaps(
        store,
        "2001-02-29",
        "2001-03-02",
        stations=["A"],
        max_gap=5,
        variable="temp",
    )
    assert result["temp"].sel(time="2001-03-01").item() == pytest.approx(14.0)


@requires_cftime
def test_overlapping_handles_fall_through_to_the_station_chunk() -> None:
    times = xr.CFTimeIndex(
        list(
            xr.date_range("2001-02-29", periods=1, calendar="360_day", use_cftime=True)
        )
    )
    other = xr.Dataset(
        {"value": (("station", "time"), [[99.0]])},
        coords={"station": ["B"], "time": times},
    )
    wanted = xr.Dataset(
        {"value": (("station", "time"), [[7.0]])},
        coords={"station": ["A"], "time": times},
    )
    handles = [
        GapChunk(stations=("A", "B"), times=tuple(times), key="both", data=other),
        GapChunk(stations=("A",), times=tuple(times), key="a", data=wanted),
    ]
    store = MemoryGapStore({"A": {}, "B": {}}, chunks=handles)
    result = fill_short_gaps(
        store, "2001-02-29", "2001-02-29", stations=["A"], max_gap=5
    )
    assert result["value"].sel(station="A", time="2001-02-29").item() == pytest.approx(
        7.0
    )


@requires_cftime
def test_single_variable_dataset_keeps_its_name() -> None:
    times = xr.CFTimeIndex(
        list(
            xr.date_range("2001-02-29", periods=2, calendar="360_day", use_cftime=True)
        )
    )
    readings = xr.Dataset(
        {"temp": (("station", "time"), [[1.0, 5.0]])},
        coords={"station": ["A"], "time": times},
    )
    store = MemoryGapStore(readings)
    result = fill_short_gaps(store, "2001-02-29", "2001-03-01", max_gap=5)
    assert "temp" in result
    assert result["temp"].sel(station="A", time="2001-02-29").item() == pytest.approx(
        1.0
    )


@requires_cftime
def test_gap_statuses_constant() -> None:
    assert GAP_STATUSES == ("observed", "filled", "shutdown", "missing")


@requires_cftime
def test_invalid_max_gap_raises() -> None:
    readings = {"A": {"2001-02-29": 1.0}}
    with pytest.raises(ValueError, match="max_gap"):
        fill_short_gaps(readings, "2001-02-29", "2001-02-29", max_gap=-1)
    with pytest.raises(ValueError, match="max_gap"):
        fill_short_gaps(readings, "2001-02-29", "2001-02-29", max_gap=1.5)


@requires_cftime
def test_end_before_start_raises() -> None:
    readings = {"A": {"2001-02-29": 1.0}}
    with pytest.raises(ValueError, match="end"):
        fill_short_gaps(readings, "2001-03-01", "2001-02-29", max_gap=5)


def test_interpolate_na_still_works() -> None:
    da = xr.DataArray([0.0, np.nan, 2.0], dims="x", coords={"x": [0, 1, 2]})
    actual = da.interpolate_na(dim="x")
    expected = xr.DataArray([0.0, 1.0, 2.0], dims="x", coords={"x": [0, 1, 2]})
    assert_equal(actual, expected)
