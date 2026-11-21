"""Tests for capacity constrained station assignment."""

import numpy as np
import pandas as pd

from shapely.geometry import Point

import geopandas
from geopandas import GeoDataFrame, assign_nearest_stations

import pytest

CRS = "EPSG:3857"


def _points(rows, crs=CRS):
    """Build a point GeoDataFrame from ``(x, y)`` pairs plus attributes."""
    return GeoDataFrame(
        rows,
        geometry=[Point(row["x"], row["y"]) for row in rows],
        crs=crs,
    ).drop(columns=["x", "y"])


def _assignment(matched, sensor):
    row = matched.loc[matched["sensor_id"] == sensor]
    if row.empty:
        return None
    return row.iloc[0]["station_id"]


def _scenario():
    """Sensor 1 closest to X with Y in tolerance; sensor 2 only reaches X."""
    sensors = _points(
        [
            {"sensor_id": "s1", "x": 0.0, "y": 0.0},
            {"sensor_id": "s2", "x": 1.0, "y": -1.0},
        ]
    )
    stations = _points(
        [
            {"station_id": "X", "x": 1.0, "y": 0.0},
            {"station_id": "Y", "x": 1.0, "y": 2.0},
        ]
    )
    return sensors, stations


def test_one_must_not_take_the_others_only_station():
    sensors, stations = _scenario()
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=3.0,
        tolerance=1.5,
        sensor_order=sensors["sensor_id"],
    )
    # s1 would prefer X (1.0) over Y (sqrt(5)), but taking X would leave s2 with
    # no candidate at all, so the maximum-cardinality solution sends s1 to Y.
    assert _assignment(result.matched, "s1") == "Y"
    assert _assignment(result.matched, "s2") == "X"
    assert result.unmatched.empty
    distances = result.matched.set_index("sensor_id")["distance"]
    assert distances.loc["s1"] == pytest.approx(5**0.5)
    assert distances.loc["s2"] == pytest.approx(1.0)


def test_capacity_is_per_station():
    sensors, stations = _scenario()
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=np.array([1, 0]),
        max_distance=3.0,
        tolerance=1.5,
    )
    assert _assignment(result.matched, "s1") == "X"
    assert result.unmatched["sensor_id"].tolist() == ["s2"]
    assert result.unmatched["reason"].tolist() == ["capacity_exhausted"]


def test_station_outside_max_distance_never_candidate():
    sensors = _points([{"sensor_id": "s1", "x": 0.0, "y": 0.0}])
    stations = _points(
        [
            {"station_id": "near", "x": 1.0, "y": 0.0},
            {"station_id": "far", "x": 4.01, "y": 0.0},
        ]
    )
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=4.0,
        tolerance=10.0,
        station_priority=np.array([9, 0]),
    )
    assert result.candidates["station_index"].tolist() == [0]
    assert _assignment(result.matched, "s1") == "near"


def test_no_candidate_and_capacity_exhausted_are_separate():
    sensors = _points(
        [
            {"sensor_id": "close", "x": 0.0, "y": 0.0},
            {"sensor_id": "busy", "x": 0.2, "y": 0.0},
            {"sensor_id": "remote", "x": 50.0, "y": 0.0},
        ]
    )
    stations = _points([{"station_id": "only", "x": 0.0, "y": 0.0}])
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=10.0,
        tolerance=0.0,
        sensor_order=np.array([0, 1, 2]),
    )
    unmatched = result.unmatched.set_index("sensor_id")
    assert unmatched.loc["busy", "reason"] == "capacity_exhausted"
    assert unmatched.loc["busy", "candidate_count"] == 1
    assert unmatched.loc["remote", "reason"] == "no_candidate"
    assert unmatched.loc["remote", "candidate_count"] == 0
    assert np.isnan(unmatched.loc["remote", "nearest_distance"])
    assert not np.isnan(unmatched.loc["busy", "nearest_distance"])


def test_tolerance_and_max_distance_apply_together():
    sensors = _points([{"sensor_id": "s1", "x": 0.0, "y": 0.0}])
    stations = _points(
        [
            {"station_id": "a", "x": 1.0, "y": 0.0},
            {"station_id": "b", "x": 1.4, "y": 0.0},
            {"station_id": "c", "x": 1.8, "y": 0.0},
        ]
    )
    # tolerance 0.5 admits b (0.4 away) but not c (0.8 away), even though
    # both are inside max_distance.
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=2.0,
        tolerance=0.5,
    )
    assert set(result.candidates["station_index"]) == {0, 1}

    # A generous tolerance is still capped by max_distance.
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=1.5,
        tolerance=10.0,
    )
    assert set(result.candidates["station_index"]) == {0, 1}
    assert result.matched.iloc[0]["station_id"] == "a"


def test_candidate_band_does_not_widen_when_busy():
    sensors = _points(
        [
            {"sensor_id": "first", "x": 0.0, "y": 0.0},
            {"sensor_id": "second", "x": 0.0, "y": 0.1},
        ]
    )
    stations = _points(
        [
            {"station_id": "a", "x": 0.1, "y": 0.0},
            {"station_id": "far_but_free", "x": 5.0, "y": 0.0},
        ]
    )
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=10.0,
        tolerance=0.05,
        sensor_order=sensors["sensor_id"],
    )
    # "second" has "far_but_free" in range but far outside its own band, so it
    # must not be connected just to fill the batch.
    assert _assignment(result.matched, "first") == "a"
    assert result.unmatched["sensor_id"].tolist() == ["second"]
    assert result.unmatched["reason"].tolist() == ["capacity_exhausted"]
    assert set(result.candidates["station_index"]) == {0}


def test_same_number_of_connections_is_tie_broken_by_stable_order():
    # Both sensors can use either of the two stations; both arrangements
    # connect two sensors.  Station priority decides the arrangement.
    sensors = _points(
        [
            {"sensor_id": "s1", "x": 0.0, "y": 0.0},
            {"sensor_id": "s2", "x": 10.0, "y": 0.0},
        ]
    )
    stations = _points(
        [
            {"station_id": "alpha", "x": 1.0, "y": 0.0},
            {"station_id": "beta", "x": 2.0, "y": 0.0},
        ]
    )
    first = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=10.0,
        tolerance=10.0,
        sensor_order=sensors["sensor_id"],
    )
    swapped = assign_nearest_stations(
        sensors.iloc[::-1].reset_index(drop=True),
        stations,
        capacity=1,
        max_distance=10.0,
        tolerance=10.0,
        sensor_order="sensor_id",
    )
    assert first.matched["sensor_id"].tolist() == ["s1", "s2"]
    assert first.matched["station_id"].tolist() == ["alpha", "beta"]
    assert swapped.matched["sensor_id"].tolist() == ["s1", "s2"]
    assert swapped.matched["station_id"].tolist() == ["alpha", "beta"]


def test_declared_sensor_order_beats_input_row_order():
    sensors = _points(
        [
            {"sensor_id": "late", "priority_no": 2, "x": 0.0, "y": 0.0},
            {"sensor_id": "early", "priority_no": 1, "x": 0.1, "y": 0.0},
        ]
    )
    stations = _points([{"station_id": "only", "x": 0.05, "y": 0.0}])
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=1.0,
        sensor_order="priority_no",
    )
    assert result.matched["sensor_id"].tolist() == ["early"]
    assert result.unmatched["sensor_id"].tolist() == ["late"]


def test_swapped_input_frames_give_the_same_assignment():
    sensors, stations = _scenario()
    kwargs = dict(
        capacity=1,
        max_distance=3.0,
        tolerance=1.5,
        sensor_order="sensor_id",
    )
    forward = assign_nearest_stations(sensors, stations, **kwargs)
    swapped = assign_nearest_stations(stations, sensors, **kwargs)
    pd.testing.assert_frame_equal(
        forward.matched[["sensor_id", "station_id", "distance"]].reset_index(drop=True),
        swapped.matched[["sensor_id", "station_id", "distance"]].reset_index(drop=True),
    )


def test_station_priority_orders_preference():
    sensors = _points([{"sensor_id": "s1", "x": 0.0, "y": 0.0}])
    stations = _points(
        [
            {"station_id": "low", "priority": 5, "x": 1.0, "y": 0.0},
            {"station_id": "high", "priority": 1, "x": 1.2, "y": 0.0},
        ]
    )
    # Without priorities the nearest station wins.
    nearest = assign_nearest_stations(
        sensors, stations, capacity=1, max_distance=2.0, tolerance=0.5
    )
    assert nearest.matched.iloc[0]["station_id"] == "low"
    # With priorities the same tolerance tier prefers the declared priority.
    preferred = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=2.0,
        tolerance=0.5,
        station_priority="priority",
    )
    assert preferred.matched.iloc[0]["station_id"] == "high"


def test_non_working_station_is_ignored():
    sensors = _points([{"sensor_id": "s1", "x": 0.0, "y": 0.0}])
    stations = _points(
        [
            {"station_id": "dead", "working": False, "x": 0.1, "y": 0.0},
            {"station_id": "live", "working": True, "x": 0.4, "y": 0.0},
        ]
    )
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=1.0,
        tolerance=0.1,
        working="working",
    )
    assert _assignment(result.matched, "s1") == "live"
    assert result.candidates["station_index"].tolist() == [1]


def test_candidate_audit_reaches_both_inputs():
    sensors, stations = _scenario()
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=3.0,
        tolerance=1.5,
        sensor_order="sensor_id",
    )
    candidates = result.candidates
    assert {"sensor_index", "station_index", "distance", "selected"}.issubset(
        candidates.columns
    )
    selected = candidates[candidates["selected"]]
    assert selected["sensor_index"].tolist() == [0, 1]
    assert selected["station_index"].tolist() == [1, 0]

    usage = result.station_usage.set_index("station_index")
    assert usage.loc[0, "assigned"] == 1
    assert usage.loc[1, "assigned"] == 1
    assert usage.loc[0, "remaining"] == 0

    matched = result.matched.set_index("sensor_id")
    assert matched.loc["s1", "sensor_index"] == 0
    assert matched.loc["s1", "station_index"] == 1
    assert matched.loc["s2", "station_index"] == 0
    assert matched.loc["s1", "station_geometry"].equals(Point(1.0, 2.0))


def test_regular_sjoin_nearest_unchanged():
    sensors, stations = _scenario()
    expected = sensors.sjoin_nearest(stations, distance_col="dist")
    assert len(expected) == 2
    assert expected["dist"].tolist() == pytest.approx([1.0, 1.0])
    assert "sjoin_nearest" in dir(geopandas)


def test_validation_errors():
    sensors, stations = _scenario()
    with pytest.raises(TypeError, match="GeoDataFrame"):
        assign_nearest_stations(pd.DataFrame(), stations, capacity=1)
    with pytest.raises(ValueError, match="projected"):
        assign_nearest_stations(
            sensors.to_crs("EPSG:4326"),
            stations.to_crs("EPSG:4326"),
            capacity=1,
        )
    with pytest.raises(ValueError, match="tolerance"):
        assign_nearest_stations(sensors, stations, capacity=1, tolerance=-1)
    with pytest.raises(ValueError, match="max_distance"):
        assign_nearest_stations(sensors, stations, capacity=1, max_distance=0)
    with pytest.raises(ValueError, match="whole numbers"):
        assign_nearest_stations(sensors, stations, capacity=1.5)
    with pytest.raises(ValueError, match="capacity"):
        assign_nearest_stations(
            sensors, stations.assign(capacity=[-1, 1]), capacity="capacity"
        )
    with pytest.raises(KeyError, match="capacity"):
        assign_nearest_stations(sensors, stations, capacity="missing")
    with pytest.raises(ValueError, match="one value per row"):
        assign_nearest_stations(sensors, stations, capacity=[1])


def test_zero_capacity_station_is_a_capacity_not_a_range_failure():
    sensors = _points([{"sensor_id": "s1", "x": 0.0, "y": 0.0}])
    stations = _points(
        [
            {"station_id": "closed", "x": 0.1, "y": 0.0},
            {"station_id": "open", "x": 0.6, "y": 0.0},
        ]
    )
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=np.array([0, 2]),
        max_distance=2.0,
        tolerance=0.1,
    )
    # The nearest station has no slot: the band stays anchored there and does
    # not widen to the farther station, but the failure is reported as a
    # capacity problem rather than a missing station.
    assert result.candidates["station_id"].tolist() == ["closed"]
    assert result.unmatched["reason"].tolist() == ["capacity_exhausted"]
    usage = result.station_usage.set_index("station_index")
    assert usage.loc[0, "assigned"] == 0
    assert usage.loc[1, "assigned"] == 0


def test_capacity_mapping_by_station_index():
    sensors = _points(
        [
            {"sensor_id": "s1", "x": 0.0, "y": 0.0},
            {"sensor_id": "s2", "x": 0.0, "y": 0.2},
        ]
    )
    stations = _points([{"station_id": "a", "x": 0.1, "y": 0.0}])
    stations.index = ["a"]
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity={"a": 1},
        max_distance=1.0,
        sensor_order=np.array([0, 1]),
    )
    assert result.matched["sensor_id"].tolist() == ["s1"]
    assert result.unmatched["reason"].tolist() == ["capacity_exhausted"]


def test_swapped_frames_with_scalar_capacity():
    sensors = _points(
        [
            {"sensor_id": "s1", "x": 0.0, "y": 0.0},
            {"sensor_id": "s2", "x": 1.0, "y": -1.0},
        ]
    )
    stations = _points(
        [
            {"station_id": "X", "x": 1.0, "y": 0.0},
            {"station_id": "Y", "x": 1.0, "y": 2.0},
        ]
    )
    forward = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=3.0,
        tolerance=1.5,
        sensor_order="sensor_id",
    )
    # Both frames are swapped and only scalar arguments are supplied, so the
    # station side is stated explicitly.
    swapped = assign_nearest_stations(
        stations,
        sensors,
        capacity=1,
        max_distance=3.0,
        tolerance=1.5,
        sensor_order=sensors["sensor_id"].to_numpy(),
        station_side="first",
    )
    pd.testing.assert_frame_equal(
        forward.matched[["sensor_id", "station_id"]].reset_index(drop=True),
        swapped.matched[["sensor_id", "station_id"]].reset_index(drop=True),
    )


def test_higher_priority_station_outside_max_distance_is_excluded():
    sensors = _points([{"sensor_id": "s1", "x": 0.0, "y": 0.0}])
    stations = _points(
        [
            {"station_id": "preferred", "priority": 0, "x": 4.01, "y": 0.0},
            {"station_id": "reachable", "priority": 1, "x": 1.0, "y": 0.0},
        ]
    )
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=4.0,
        tolerance=10.0,
        station_priority="priority",
    )
    assert result.candidates["station_id"].tolist() == ["reachable"]
    assert result.matched.iloc[0]["station_id"] == "reachable"


def test_rerouting_reaches_maximum_cardinality():
    # A chain where a greedy nearest assignment would connect only one row.
    sensors = _points(
        [
            {"sensor_id": "s1", "x": 0.0, "y": 0.0},
            {"sensor_id": "s2", "x": 0.2, "y": 0.0},
            {"sensor_id": "s3", "x": 0.4, "y": 0.0},
        ]
    )
    stations = _points(
        [
            {"station_id": "a", "x": 0.1, "y": 0.0},
            {"station_id": "b", "x": 0.3, "y": 0.0},
        ]
    )
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=1.0,
        tolerance=1.0,
        sensor_order=np.array([0, 1, 2]),
    )
    assert len(result.matched) == 2
    assert len(result.unmatched) == 1
    assert result.unmatched.iloc[0]["sensor_id"] == "s3"
    assert result.unmatched.iloc[0]["reason"] == "capacity_exhausted"


def test_distance_col_name():
    sensors = _points([{"sensor_id": "s1", "x": 0.0, "y": 0.0}])
    stations = _points([{"station_id": "a", "x": 1.0, "y": 0.0}])
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=2.0,
        distance_col="metres",
    )
    assert "metres" in result.matched.columns
    assert "metres" in result.candidates.columns
    assert result.matched.iloc[0]["metres"] == pytest.approx(1.0)


def test_empty_inputs():
    sensors = GeoDataFrame(
        {"sensor_id": pd.Series(dtype=object)},
        geometry=[],
        crs=CRS,
    )
    stations = GeoDataFrame(
        {"station_id": pd.Series(dtype=object)},
        geometry=[],
        crs=CRS,
    )
    result = assign_nearest_stations(sensors, stations, capacity=1)
    assert result.matched.empty
    assert result.unmatched.empty
    assert result.candidates.empty
    assert result.station_usage.empty


def test_max_distance_is_inclusive():
    sensors = _points([{"sensor_id": "s1", "x": 0.0, "y": 0.0}])
    stations = _points(
        [
            {"station_id": "edge", "x": 4.0, "y": 0.0},
            {"station_id": "beyond", "x": 4.0001, "y": 0.0},
        ]
    )
    result = assign_nearest_stations(
        sensors, stations, capacity=1, max_distance=4.0, tolerance=1.0
    )
    assert result.candidates["station_id"].tolist() == ["edge"]


def test_tolerance_boundary_is_strict():
    sensors = _points([{"sensor_id": "s1", "x": 0.0, "y": 0.0}])
    stations = _points(
        [
            {"station_id": "near", "x": 1.0, "y": 0.0},
            {"station_id": "tier_edge", "x": 1.5, "y": 0.0},
        ]
    )
    inside = assign_nearest_stations(
        sensors, stations, capacity=1, max_distance=3.0, tolerance=0.5001
    )
    assert set(inside.candidates["station_id"]) == {"near", "tier_edge"}
    outside = assign_nearest_stations(
        sensors, stations, capacity=1, max_distance=3.0, tolerance=0.5
    )
    assert outside.candidates["station_id"].tolist() == ["near"]


def test_declared_priority_prefers_label_not_distance():
    sensors = _points([{"sensor_id": "s1", "x": 0.0, "y": 0.0}])
    stations = _points(
        [
            {"station_id": "closer", "priority": 2, "x": 1.0, "y": 0.0},
            {"station_id": "declared", "priority": 1, "x": 1.4, "y": 0.0},
        ]
    )
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=2.0,
        tolerance=0.5,
        station_priority="priority",
    )
    assert result.matched.iloc[0]["station_id"] == "declared"


def test_categorical_station_priority():
    sensors = _points([{"sensor_id": "s1", "x": 0.0, "y": 0.0}])
    stations = _points(
        [
            {"station_id": "B", "tier": "bronze", "x": 1.0, "y": 0.0},
            {"station_id": "A", "tier": "gold", "x": 1.2, "y": 0.0},
        ]
    )
    result = assign_nearest_stations(
        sensors,
        stations,
        capacity=1,
        max_distance=2.0,
        tolerance=0.5,
        station_priority="tier",
    )
    # "bronze" < "gold" in ascending rank, so B wins although A is closer.
    assert result.matched.iloc[0]["station_id"] == "B"


def test_station_attribute_named_like_audit_column_is_suffixed():
    sensors = _points([{"sensor_id": "s1", "distance": 99.0, "x": 0.0, "y": 0.0}])
    stations = _points(
        [{"station_id": "a", "distance": "site-marker", "x": 1.0, "y": 0.0}]
    )
    result = assign_nearest_stations(sensors, stations, capacity=1, max_distance=2.0)
    # The sensor column keeps its original name; the station attribute is
    # suffixed so neither is lost.
    assert result.matched.iloc[0]["distance"] == pytest.approx(1.0)
    assert result.matched.iloc[0]["distance_station"] == "site-marker"
