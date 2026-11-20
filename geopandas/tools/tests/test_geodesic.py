from shapely.geometry import LineString, MultiLineString, Point

import geopandas
from geopandas import GeoDataFrame, GeoSeries
from geopandas.tools import geodesic as geodesic_module
from geopandas.tools.geodesic import (
    SEGMENT_CLIPPED,
    SEGMENT_OUTSIDE,
    SEGMENT_PROJECTED,
    STATUS_OK,
    STATUS_OUTSIDE,
    STATUS_PARTIAL,
    STATUS_VERTEX_TRANSFORMED,
    geodesic_to_crs,
)

import pytest

pytest.importorskip("pyproj")

# UTM zone 32N is valid for longitudes 6E..12E; all in-range fixtures stay
# inside it so the area-of-use scan never interferes with the tolerance work.
TARGET_EPSG = 32632
# Web Mercator is only defined between 85.06S and 85.06N.
MERCATOR_EPSG = 3857


def _max_reference_error(geometry, source_segments, target_epsg, npts=2000):
    """Largest map distance between dense geodesic reference points and line."""
    from pyproj import Geod, Transformer

    geod = Geod(ellps="WGS84")
    transformer = Transformer.from_crs(4326, target_epsg, always_xy=True)
    worst = 0.0
    for start, end in source_segments:
        points = geod.npts(start[0], start[1], end[0], end[1], npts)
        lons = [point[0] for point in points]
        lats = [point[1] for point in points]
        xs, ys = transformer.transform(lons, lats)
        for x, y in zip(xs, ys):
            worst = max(worst, Point(x, y).distance(geometry))
    return worst


def test_module_is_exposed():
    assert geopandas.geodesic_to_crs is geodesic_to_crs
    assert geopandas.tools.geodesic_to_crs is geodesic_to_crs
    assert geodesic_module.STATUS_OK == STATUS_OK
    assert geodesic_module.SEGMENT_PROJECTED == SEGMENT_PROJECTED


def test_basic_track_densifies_and_keeps_station_lookup():
    track = GeoSeries([LineString([(10, 40), (11, 41)])], crs=4326)
    result = geodesic_to_crs(track, TARGET_EPSG, map_tolerance=1.0)

    assert result.status == [STATUS_OK]
    line = result.geometries[0]
    assert isinstance(line, LineString)
    # A geodesic leg must not collapse back to the two-vertex chord.
    assert len(line.coords) > 2

    records = result.survey_stations[0]
    assert [record["vertex"] for record in records] == [0, 1]
    assert all(record["geometry"] == 0 for record in records)
    station_coords = {record["coordinates"] for record in records}

    # Densification points are distinguishable from real survey stations.
    for index, coord in enumerate(line.coords):
        is_station = any(record["coord_index"] == index for record in records)
        assert (coord in station_coords) == is_station

    # Stations remain checkable against an ordinary point transform.
    plain = track.to_crs(TARGET_EPSG)
    plain_coords = list(plain.iloc[0].coords)
    for record, expected in zip(records, plain_coords):
        assert record["coordinates"] == pytest.approx(expected, abs=1e-6)

    segment = result.segment_status[0][0][0]
    assert segment["status"] == SEGMENT_PROJECTED
    assert segment["outside_fractions"] == []


def test_routes_with_same_endpoints_but_different_mid_station():
    endpoints = [(10, 40), (12, 42)]
    direct = GeoSeries([LineString(endpoints)], crs=4326)
    mid_station = (11.5, 41.8)
    via = GeoSeries([LineString([endpoints[0], mid_station, endpoints[1]])], crs=4326)
    tolerance = 2.0

    direct_result = geodesic_to_crs(direct, TARGET_EPSG, map_tolerance=tolerance)
    via_result = geodesic_to_crs(via, TARGET_EPSG, map_tolerance=tolerance)
    direct_line = direct_result.geometries[0]
    via_line = via_result.geometries[0]

    # The mid station genuinely changes the published surface track.
    assert not direct_line.equals(via_line)

    # Both polylines must hold the tolerance against geodesic reference
    # points, not merely land on the correct endpoints.
    direct_error = _max_reference_error(
        direct_line,
        [((10, 40), (12, 42))],
        TARGET_EPSG,
    )
    via_error = _max_reference_error(
        via_line,
        [((10, 40), mid_station), (mid_station, (12, 42))],
        TARGET_EPSG,
    )
    assert direct_error <= tolerance
    assert via_error <= tolerance

    records = via_result.survey_stations[0]
    assert [record["vertex"] for record in records] == [0, 1, 2]
    coord_indices = [record["coord_index"] for record in records]
    assert coord_indices == sorted(coord_indices)
    assert 0 < coord_indices[1] < len(via_line.coords) - 1

    from pyproj import Transformer

    transformer = Transformer.from_crs(4326, TARGET_EPSG, always_xy=True)
    expected_mid = transformer.transform(*mid_station)
    actual_mid = via_line.coords[coord_indices[1]]
    assert actual_mid == pytest.approx(expected_mid, abs=1e-6)


def test_tightening_tolerance_adds_points():
    # A leg short enough to be a single scan leaf, so densification is driven
    # purely by the requested map tolerance.
    leg = GeoSeries([LineString([(9.5, 40), (10.5, 40)])], crs=4326)
    coarse = geodesic_to_crs(leg, TARGET_EPSG, map_tolerance=1.0)
    fine = geodesic_to_crs(leg, TARGET_EPSG, map_tolerance=0.01)
    assert len(fine.geometries[0].coords) > len(coarse.geometries[0].coords)

    error = _max_reference_error(
        fine.geometries[0], [((9.5, 40), (10.5, 40))], TARGET_EPSG
    )
    assert error <= 0.01


def test_long_leg_bows_even_though_endpoints_look_linear():
    # A long diagonal leg in Web Mercator: the endpoint chord is locally
    # straight at both ends but drifts tens of kilometres from the geodesic.
    start, end = (0, 0), (20, 20)
    leg = GeoSeries([LineString([start, end])], crs=4326)
    tolerance = 5000.0
    result = geodesic_to_crs(leg, MERCATOR_EPSG, map_tolerance=tolerance)
    geodesic_line = result.geometries[0]
    chord = leg.to_crs(MERCATOR_EPSG).iloc[0]

    geodesic_error = _max_reference_error(geodesic_line, [(start, end)], MERCATOR_EPSG)
    chord_error = _max_reference_error(chord, [(start, end)], MERCATOR_EPSG)
    assert geodesic_error <= tolerance
    # Endpoints alone are not enough: the plain chord visibly leaves the track.
    assert chord_error > geodesic_error
    assert chord_error > tolerance


def test_partial_track_reports_source_segment_without_bridging_gap():
    # Endpoints sit inside the Mercator band; the geodesic bows beyond 85.06N.
    track = GeoSeries([LineString([(-30, 84.5), (30, 84.5)])], crs=4326)
    result = geodesic_to_crs(track, MERCATOR_EPSG, map_tolerance=100.0)

    assert result.status == [STATUS_PARTIAL]
    geometry = result.geometries[0]
    assert isinstance(geometry, MultiLineString)
    assert len(geometry.geoms) == 2

    component = result.segment_status[0][0][0]
    assert component["status"] == SEGMENT_CLIPPED
    assert len(component["outside_fractions"]) == 1
    start, end = component["outside_fractions"][0]
    # Endpoints remain projectable, so the gap is strictly interior.
    assert 0.0 < start < end < 1.0

    # Each piece stops at the Mercator limit instead of bridging the gap.
    from pyproj import Transformer

    transformer = Transformer.from_crs(4326, MERCATOR_EPSG, always_xy=True)
    limit_y = transformer.transform(0.0, 85.06)[1]
    for part in geometry.geoms:
        assert max(y for _, y in part.coords) <= limit_y + 1e-6
    left, right = geometry.geoms
    assert max(x for x, _ in left.coords) < min(x for x, _ in right.coords)

    # The two real stations survive, one on each projected piece.
    stations = result.survey_stations[0]
    assert {record["vertex"] for record in stations} == {0, 1}
    assert {record["geometry"] for record in stations} == {0, 1}


def test_fully_outside_segment_is_not_drawn():
    track = GeoSeries([LineString([(-10, 88), (10, 88)])], crs=4326)
    result = geodesic_to_crs(track, MERCATOR_EPSG, map_tolerance=100.0)

    assert result.status == [STATUS_OUTSIDE]
    assert result.geometries == [None]
    segment = result.segment_status[0][0][0]
    assert segment["status"] == SEGMENT_OUTSIDE
    assert segment["outside_fractions"] == [(0.0, 1.0)]
    assert result.survey_stations[0] == []


def test_non_line_geometry_uses_plain_vertex_transform():
    point = GeoSeries([Point(10, 40)], crs=4326)
    result = geodesic_to_crs(point, TARGET_EPSG, map_tolerance=1.0)

    assert result.status == [STATUS_VERTEX_TRANSFORMED]
    assert result.segment_status == [None]
    expected = point.to_crs(TARGET_EPSG).iloc[0]
    assert result.geometries[0].equals_exact(expected, tolerance=1e-9)


def test_default_to_crs_keeps_vertex_only_behavior():
    track = GeoSeries([LineString([(10, 40), (11, 41)])], crs=4326)
    legacy = track.to_crs(TARGET_EPSG)
    geodesic = geodesic_to_crs(track, TARGET_EPSG, map_tolerance=1.0)

    assert len(legacy.iloc[0].coords) == 2
    assert len(geodesic.geometries[0].coords) > 2
    assert not legacy.iloc[0].equals(geodesic.geometries[0])


def test_projected_source_crs_is_interpreted_on_ellipsoid():
    source = GeoSeries(
        [LineString([(500000, 4428000), (600000, 4540000)])], crs=TARGET_EPSG
    )
    result = geodesic_to_crs(source, 3857, map_tolerance=10.0)

    assert result.status == [STATUS_OK]
    line = result.geometries[0]
    assert len(line.coords) > 2
    records = result.survey_stations[0]
    assert [record["vertex"] for record in records] == [0, 1]

    expected = source.to_crs(3857).iloc[0]
    assert line.coords[0] == pytest.approx(expected.coords[0], abs=1e-6)
    assert line.coords[-1] == pytest.approx(expected.coords[-1], abs=1e-6)


def test_geodataframe_input_and_series_round_trip():
    frame = GeoDataFrame(
        {"name": ["leg"], "geometry": [LineString([(10, 40), (11, 41)])]},
        crs=4326,
    )
    result = geodesic_to_crs(frame, TARGET_EPSG, map_tolerance=1.0)
    series = result.as_geoseries(index=frame.index)

    assert isinstance(series, GeoSeries)
    assert series.crs == result.target_crs
    assert list(series.index) == list(frame.index)
    assert series.iloc[0].equals(result.geometries[0])


def test_multi_line_and_null_features():
    geometries = [
        MultiLineString(
            [
                LineString([(10, 40), (10.5, 40.5)]),
                LineString([(10.5, 40.5), (11, 41)]),
            ]
        ),
        None,
    ]
    tracks = GeoSeries(geometries, crs=4326)
    result = geodesic_to_crs(tracks, TARGET_EPSG, map_tolerance=1.0)

    assert result.status[0] == STATUS_OK
    assert result.status[1] == STATUS_OUTSIDE
    assert result.geometries[1] is None
    assert len(result.segment_status[0]) == 2
    assert result.segment_status[1] is None
    # Shared station between the two source components is still traceable.
    vertices = {
        record["vertex"]
        for record in result.survey_stations[0]
        if record["geometry"] == 0
    }
    assert vertices == {0, 1}


def test_validation_errors():
    track = GeoSeries([LineString([(10, 40), (11, 41)])], crs=4326)

    with pytest.raises(ValueError, match="target_crs or epsg"):
        geodesic_to_crs(track, map_tolerance=1.0)
    with pytest.raises(ValueError, match="map_tolerance"):
        geodesic_to_crs(track, TARGET_EPSG, map_tolerance=0)
    with pytest.raises(ValueError, match="source CRS"):
        geodesic_to_crs(
            [LineString([(10, 40), (11, 41)])],
            TARGET_EPSG,
            map_tolerance=1.0,
        )


def test_crs_without_area_of_use_is_never_clipped():
    custom_crs = "+proj=utm +zone=32 +datum=WGS84"
    track = GeoSeries([LineString([(10, 40), (11, 41)])], crs=4326)
    result = geodesic_to_crs(track, custom_crs, map_tolerance=1.0)

    assert result.status == [STATUS_OK]
    assert isinstance(result.geometries[0], LineString)
    segment = result.segment_status[0][0][0]
    assert segment["status"] == SEGMENT_PROJECTED
