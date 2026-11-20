"""Geodesic-aware reprojection of survey track lines.

The default :meth:`GeoSeries.to_crs` convention only transforms the vertices
of a geometry: every segment joining two vertices is redrawn as a straight
line in the target map space.  That is the correct behaviour for geometry that
was digitised in map coordinates, but a marine survey coordinate table stores
each leg as the shortest track between stations on the surface of the
ellipsoid (a geodesic).  Reprojecting the endpoints alone therefore bends the
published track away from the route the vessel actually sailed.

This module provides an opt-in convention, :func:`geodesic_to_crs`, that
densifies every source segment along the ellipsoidal geodesic before
projecting it.  The resulting polyline approximates the true track within a
caller supplied map tolerance, keeps the declared intermediate stations (and
the survey segment ordering) inspectable, and reports any source interval that
cannot be proven to stay inside the usable range of the target projection
instead of silently bridging it with a straight map line.

The plain vertex transform in ``to_crs`` is intentionally left untouched:
this module does not claim to repair arbitrary incoming geometry.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cache
from typing import Any

import numpy as np

import shapely
from shapely.geometry import LineString, MultiLineString

from geopandas._compat import requires_pyproj

#: Feature is fully representable in the target projection.
STATUS_OK = "ok"
#: Feature is representable only in pieces; some source intervals were dropped.
STATUS_PARTIAL = "partial"
#: No part of the feature could be projected.
STATUS_OUTSIDE = "outside"
#: Non-line geometry; vertices were transformed without geodesic densification.
STATUS_VERTEX_TRANSFORMED = "vertex_transformed"

#: The whole source segment was projected.
SEGMENT_PROJECTED = "projected"
#: Parts of the source segment left the usable range and were cut out.
SEGMENT_CLIPPED = "clipped"
#: The whole source segment is outside the usable range.
SEGMENT_OUTSIDE = "outside"

# Maximum number of interpolation intervals for a single projected arc.
_MAX_INTERVALS = 4096
# Arcs longer than this (metres) keep being subdivided while scanning the
# projection area of use, so a short excursion cannot hide between samples.
_MAX_LEAF_METRES = 100_000.0
_SCAN_DEPTH = 16
_BOUNDARY_ITERATIONS = 30


@dataclass
class GeodesicProjectionResult:
    """Outcome of a :func:`geodesic_to_crs` call.

    Attributes
    ----------
    geometries : list of shapely geometries or None
        Projected geometry for each input feature, aligned with the input
        order.  A line that leaves the usable range becomes a
        ``MultiLineString`` holding the projectable pieces; ``None`` marks a
        feature with no projectable part.
    target_crs : pyproj.CRS
        CRS of the projected geometries.
    status : list of str
        Per-feature status, one of :data:`STATUS_OK`, :data:`STATUS_PARTIAL`,
        :data:`STATUS_OUTSIDE` or :data:`STATUS_VERTEX_TRANSFORMED`.
    segment_status : list
        Per-feature detail for line work.  Each entry is a list (one item per
        source ``LineString`` component) of per-segment records of the form
        ``{"status": ..., "outside_fractions": [(start, end), ...]}`` where the
        fractions are arc-length positions along the source segment that could
        not be projected.  Non-line features carry ``None``.
    survey_stations : list
        Per-feature records of the original input vertices that survive in the
        output geometry, of the form
        ``{"vertex": int, "geometry": int, "coord_index": int,
        "coordinates": (x, y)}``.  Any output vertex without such a record is a
        densification or clipping point added by this module, not a real
        survey station.
    """

    geometries: list
    target_crs: Any
    status: list = field(default_factory=list)
    segment_status: list = field(default_factory=list)
    survey_stations: list = field(default_factory=list)

    def as_geoseries(self, index=None):
        """Return the projected geometries as a :class:`GeoSeries`.

        Parameters
        ----------
        index : array-like, optional
            Index for the returned ``GeoSeries``; defaults to a range index.

        Returns
        -------
        GeoSeries
        """
        from geopandas import GeoSeries

        return GeoSeries(self.geometries, crs=self.target_crs, index=index)


def _geographic_crs(crs):
    if crs.is_geographic:
        return crs
    geog = crs.geodetic_crs
    if geog is None:
        raise ValueError(
            f"CRS {crs!r} has no geographic counterpart; geodesic "
            "interpretation requires an ellipsoidal geographic CRS."
        )
    return geog


@cache
def _geod_for(geog_crs):
    from pyproj import Geod

    ellipsoid = geog_crs.ellipsoid
    return Geod(
        a=ellipsoid.semi_major_metre,
        rf=ellipsoid.inverse_flattening,
    )


@cache
def _transformer_for(source_crs, target_crs):
    from pyproj import Transformer

    return Transformer.from_crs(source_crs, target_crs, always_xy=True)


def _make_in_range(area_bounds):
    if area_bounds is None:
        return lambda lon, lat: True

    west, south, east, north = area_bounds

    if west <= east:

        def in_range(lon, lat):
            return west <= lon <= east and south <= lat <= north

    else:
        # Area of use crosses the antimeridian.
        def in_range(lon, lat):
            return (lon >= west or lon <= east) and south <= lat <= north

    return in_range


def _max_midpoint_deviation(coarse_coords, fine_coords):
    """Max distance of the fine polyline's new vertices to coarse chords.

    ``fine_coords`` has twice as many intervals as ``coarse_coords`` and shares
    every second vertex with it; the odd vertices are the inserted geodesic
    midpoints.  Their distance to the surrounding coarse chord measures how
    much the map polyline still bends away from the true track.
    """
    coarse = np.asarray(coarse_coords, dtype=float)
    fine = np.asarray(fine_coords, dtype=float)
    starts = coarse[:-1]
    ends = coarse[1:]
    chords = ends - starts
    chord_len = np.hypot(chords[:, 0], chords[:, 1])
    midpoints = fine[1::2]
    deltas = midpoints - starts
    if np.all(chord_len > 0):
        cross = chords[:, 0] * deltas[:, 1] - chords[:, 1] * deltas[:, 0]
        return float(np.max(np.abs(cross) / chord_len))
    # Degenerate chords: fall back to direct midpoint distances.
    return float(np.max(np.hypot(deltas[:, 0], deltas[:, 1])))


def _bisect_boundary(geod, in_range, point_a, point_b, fraction_a, fraction_b):
    """Split an arc whose endpoints differ in range status.

    Returns the last in-range point and first out-range point (with their
    arc-length fractions), each refined by bisection.
    """
    a_in = point_a[2]
    for _ in range(_BOUNDARY_ITERATIONS):
        mid_xy = geod.npts(point_a[0], point_a[1], point_b[0], point_b[1], 1)[0]
        mid_fraction = (fraction_a + fraction_b) / 2.0
        mid = (mid_xy[0], mid_xy[1], in_range(mid_xy[0], mid_xy[1]))
        if mid[2] == a_in:
            point_a, fraction_a = mid, mid_fraction
        else:
            point_b, fraction_b = mid, mid_fraction
    if a_in:
        return point_a, fraction_a, point_b, fraction_b
    return point_b, fraction_b, point_a, fraction_a


def _subdivide(
    geod, point_a, point_b, fraction_a, fraction_b, distance, in_range, depth, leaves
):
    in_a = point_a[2]
    in_b = point_b[2]
    arc_metres = distance * (fraction_b - fraction_a)
    if depth == 0 or (arc_metres <= _MAX_LEAF_METRES and in_a == in_b):
        leaves.append((fraction_a, point_a, fraction_b, point_b, in_a and in_b))
        return
    mid_xy = geod.npts(point_a[0], point_a[1], point_b[0], point_b[1], 1)[0]
    mid_point = (mid_xy[0], mid_xy[1], in_range(mid_xy[0], mid_xy[1]))
    fraction_mid = (fraction_a + fraction_b) / 2.0
    _subdivide(
        geod,
        point_a,
        mid_point,
        fraction_a,
        fraction_mid,
        distance,
        in_range,
        depth - 1,
        leaves,
    )
    _subdivide(
        geod,
        mid_point,
        point_b,
        fraction_mid,
        fraction_b,
        distance,
        in_range,
        depth - 1,
        leaves,
    )


def _scan_segment(geod, point_a, point_b, in_range):
    """Split one geodesic segment into in-range runs and out-range fractions.

    Returns
    -------
    runs : list of list of (lon, lat)
        Maximal arcs staying inside the usable area.  Boundary crossings are
        represented by refined boundary points.
    outside_fractions : list of (float, float)
        Arc-length fractions (0..1) of the source segment that are outside.
    """
    distance = geod.inv(point_a[0], point_a[1], point_b[0], point_b[1])[2]
    if distance == 0:
        if in_range(*point_a):
            return [[point_a]], []
        return [], [(0.0, 1.0)]

    tagged_a = (point_a[0], point_a[1], in_range(*point_a))
    tagged_b = (point_b[0], point_b[1], in_range(*point_b))
    leaves = []
    _subdivide(
        geod,
        tagged_a,
        tagged_b,
        0.0,
        1.0,
        distance,
        in_range,
        _SCAN_DEPTH,
        leaves,
    )

    runs = []
    outside_fractions = []
    active_run = None
    outside_start = None

    def close_outside(fraction):
        nonlocal outside_start
        if outside_start is not None and fraction > outside_start:
            outside_fractions.append((outside_start, fraction))
        outside_start = None

    for fraction_a, pa, fraction_b, pb, both_in_range in leaves:
        a_in = pa[2]
        b_in = pb[2]
        if a_in and b_in:
            if active_run is None:
                close_outside(fraction_a)
                active_run = [(pa[0], pa[1])]
            active_run.append((pb[0], pb[1]))
        elif not a_in and not b_in:
            if active_run is not None:
                # Defensive: a depth-limited mixed leaf was expected instead.
                runs.append(active_run)
                active_run = None
            if outside_start is None:
                outside_start = fraction_a
        else:
            in_point, _in_fraction, _out_point, out_fraction = _bisect_boundary(
                geod, in_range, pa, pb, fraction_a, fraction_b
            )
            if a_in:
                if active_run is None:
                    active_run = [(in_point[0], in_point[1])]
                else:
                    active_run.append((in_point[0], in_point[1]))
                runs.append(active_run)
                active_run = None
                if outside_start is None:
                    outside_start = out_fraction
            else:
                close_outside(out_fraction)
                active_run = [(in_point[0], in_point[1])]
                active_run.append((pb[0], pb[1]))
    close_outside(1.0)
    if active_run is not None:
        runs.append(active_run)

    return runs, outside_fractions


def _densify_arc(geod, transformer, point_a, point_b, map_tolerance):
    """Project one in-range geodesic arc, densified to the map tolerance.

    Starting from the endpoint chord, the arc is repeatedly halved along the
    geodesic until every inserted midpoint lies within ``map_tolerance`` map
    units of the chord it replaced.  The remaining projection error against
    the true track is then bounded by that tolerance.
    """
    distance = geod.inv(point_a[0], point_a[1], point_b[0], point_b[1])[2]
    if distance == 0:
        x, y = transformer.transform(point_a[0], point_a[1])
        return [(x, y)]

    def build(interval_count):
        if interval_count == 1:
            intermediates = []
        else:
            intermediates = geod.npts(
                point_a[0],
                point_a[1],
                point_b[0],
                point_b[1],
                interval_count - 1,
            )
        geodesic_points = [point_a, *intermediates, point_b]
        lons = [point[0] for point in geodesic_points]
        lats = [point[1] for point in geodesic_points]
        xs, ys = transformer.transform(lons, lats)
        return list(zip(xs, ys))

    coarse = build(1)
    intervals = 1
    while intervals < _MAX_INTERVALS:
        fine = build(intervals * 2)
        if _max_midpoint_deviation(coarse, fine) <= map_tolerance:
            return fine
        coarse = fine
        intervals *= 2
    return coarse


def _project_linestring(geometry, geod, to_geog, transformer, in_range, map_tolerance):
    if geometry.has_z:
        raise NotImplementedError(
            "geodesic_to_crs currently densifies 2D line work only; "
            f"got a Z-enabled {geometry.geom_type}."
        )

    geod_vertices = []
    for lon, lat in geometry.coords:
        if to_geog is not None:
            lon, lat = to_geog.transform(lon, lat)
        geod_vertices.append((lon, lat))

    output_runs = []
    segment_records = []

    for seg_index in range(len(geod_vertices) - 1):
        point_a = geod_vertices[seg_index]
        point_b = geod_vertices[seg_index + 1]
        runs, outside_fractions = _scan_segment(geod, point_a, point_b, in_range)

        if not runs:
            segment_records.append(
                {"status": SEGMENT_OUTSIDE, "outside_fractions": [(0.0, 1.0)]}
            )
            continue

        for run in runs:
            map_coords = []
            labels = []
            for arc_index in range(len(run) - 1):
                arc_coords = _densify_arc(
                    geod,
                    transformer,
                    run[arc_index],
                    run[arc_index + 1],
                    map_tolerance,
                )
                if arc_index == 0:
                    map_coords.extend(arc_coords)
                else:
                    map_coords.extend(arc_coords[1:])
            run_labels = [None] * len(map_coords)
            if run[0] == point_a:
                run_labels[0] = seg_index
            if run[-1] == point_b:
                run_labels[-1] = seg_index + 1
            output_runs.append((map_coords, run_labels))

        if outside_fractions:
            segment_records.append(
                {"status": SEGMENT_CLIPPED, "outside_fractions": outside_fractions}
            )
        else:
            segment_records.append(
                {"status": SEGMENT_PROJECTED, "outside_fractions": []}
            )

    # Stitch consecutive runs that share a projected station vertex.
    stitched = []
    for map_coords, labels in output_runs:
        if (
            stitched
            and labels[0] is not None
            and stitched[-1][1][-1] == labels[0]
            and np.allclose(stitched[-1][0][-1], map_coords[0])
        ):
            prev_coords, prev_labels = stitched[-1]
            stitched[-1] = (
                prev_coords + map_coords[1:],
                prev_labels + labels[1:],
            )
        else:
            stitched.append((list(map_coords), list(labels)))

    lines = []
    station_records = []
    for geometry_index, (map_coords, labels) in enumerate(stitched):
        lines.append(LineString(map_coords))
        for coord_index, vertex_index in enumerate(labels):
            if vertex_index is not None:
                station_records.append(
                    {
                        "vertex": vertex_index,
                        "geometry": geometry_index,
                        "coord_index": coord_index,
                        "coordinates": map_coords[coord_index],
                    }
                )

    if not lines:
        return None, segment_records, station_records, STATUS_OUTSIDE
    if all(record["status"] == SEGMENT_PROJECTED for record in segment_records):
        feature_status = STATUS_OK
    elif all(record["status"] == SEGMENT_OUTSIDE for record in segment_records):
        feature_status = STATUS_OUTSIDE
    else:
        feature_status = STATUS_PARTIAL
    return lines, segment_records, station_records, feature_status


def _transform_vertices(geometry, to_geog, to_target):
    """Ordinary point-by-point transform for non-line geometry."""

    def func(coords):
        xs = coords[:, 0]
        ys = coords[:, 1]
        if to_geog is not None:
            xs, ys = to_geog.transform(xs, ys)
        projected_x, projected_y = to_target.transform(xs, ys)
        if coords.shape[1] == 3:
            return np.column_stack([projected_x, projected_y, coords[:, 2]])
        return np.column_stack([projected_x, projected_y])

    return shapely.transform(geometry, func, include_z=geometry.has_z)


def _resolve_source_crs(geometries, source_crs):
    if source_crs is not None:
        from pyproj import CRS

        return CRS.from_user_input(source_crs)
    crs = getattr(geometries, "crs", None)
    if crs is None:
        raise ValueError(
            "A source CRS is required: pass source_crs explicitly or use a "
            "GeoSeries/GeoDataFrame whose crs is set."
        )
    return crs


@requires_pyproj
def geodesic_to_crs(
    geometries,
    target_crs=None,
    *,
    map_tolerance,
    source_crs=None,
    epsg=None,
):
    """Reproject survey lines, interpreting each segment as a geodesic.

    Each segment of an input line is treated as the shortest path on the
    ellipsoid joining its vertices.  The segment is densified along that track
    before every point is transformed to ``target_crs``, so the projected
    polyline follows the real route within ``map_tolerance`` map units.

    Segments (or parts of them) whose geodesic cannot be proven to stay inside
    the target CRS area of use are not silently bridged: the projectable
    pieces are returned as separate line parts and the affected source
    segment, including the arc-length fractions that had to be dropped, is
    reported on the returned :class:`GeodesicProjectionResult`.

    Only line geometry receives geodesic treatment.  Points and other geometry
    types go through the ordinary vertex transform and are flagged with
    :data:`STATUS_VERTEX_TRANSFORMED`; this function does not claim to repair
    arbitrary incoming geometry.  The regular :meth:`GeoSeries.to_crs` keeps
    its existing vertex-only behaviour.

    Parameters
    ----------
    geometries : GeoSeries, GeoDataFrame or array-like of shapely geometries
        Input survey geometry with a known CRS.
    target_crs : value, optional if ``epsg`` is given
        Anything accepted by :meth:`pyproj.CRS.from_user_input`.
    map_tolerance : float
        Maximum allowed deviation, in the linear units of the target CRS,
        between the projected polyline and the projected geodesic.  Each arc
        is halved along the geodesic until every inserted midpoint lies within
        this distance of the map chord it replaced.
    source_crs : value, optional
        Source CRS; required when ``geometries`` does not carry one.
    epsg : int, optional
        EPSG code of the target CRS as an alternative to ``target_crs``.

    Returns
    -------
    GeodesicProjectionResult

    Examples
    --------
    >>> from shapely.geometry import LineString
    >>> import geopandas as gpd
    >>> track = gpd.GeoSeries([LineString([(10, 40), (11, 41)])], crs=4326)
    >>> result = gpd.tools.geodesic_to_crs(track, 32632, map_tolerance=1.0)
    >>> result.status
    ['ok']
    >>> result.geometries[0].geom_type
    'LineString'
    """
    from pyproj import CRS

    if target_crs is not None:
        target_crs = CRS.from_user_input(target_crs)
    elif epsg is not None:
        target_crs = CRS.from_epsg(epsg)
    else:
        raise ValueError("Must pass either target_crs or epsg.")
    if map_tolerance <= 0:
        raise ValueError("map_tolerance must be strictly positive.")

    source_crs = _resolve_source_crs(geometries, source_crs)

    geog_crs = _geographic_crs(source_crs)
    geod = _geod_for(geog_crs)
    to_geog = (
        None if source_crs.is_geographic else _transformer_for(source_crs, geog_crs)
    )
    to_target = _transformer_for(geog_crs, target_crs)
    area_bounds = (
        target_crs.area_of_use.bounds if target_crs.area_of_use is not None else None
    )
    in_range = _make_in_range(area_bounds)

    if hasattr(geometries, "geometry"):
        raw_geometries = list(geometries.geometry.values)
    elif hasattr(geometries, "values"):
        raw_geometries = list(geometries.values)
    else:
        raw_geometries = list(geometries)

    out_geometries = []
    statuses = []
    segment_status = []
    station_lookup = []

    for geometry in raw_geometries:
        if geometry is None or (isinstance(geometry, float) and np.isnan(geometry)):
            out_geometries.append(None)
            statuses.append(STATUS_OUTSIDE)
            segment_status.append(None)
            station_lookup.append([])
            continue

        if isinstance(geometry, (LineString, MultiLineString)):
            components = (
                [geometry] if isinstance(geometry, LineString) else list(geometry.geoms)
            )
            component_records = []
            component_lines = []
            component_stations = []
            component_ok = True
            for component in components:
                lines, records, stations, feature_status = _project_linestring(
                    component,
                    geod,
                    to_geog,
                    to_target,
                    in_range,
                    map_tolerance,
                )
                component_records.append(records)
                if feature_status != STATUS_OK:
                    component_ok = False
                if lines is None:
                    continue
                line_offset = len(component_lines)
                component_lines.extend(lines)
                for record in stations:
                    record = dict(record)
                    record["geometry"] += line_offset
                    component_stations.append(record)

            if not component_lines:
                out_geometries.append(None)
                statuses.append(STATUS_OUTSIDE)
            elif len(component_lines) == 1:
                out_geometries.append(component_lines[0])
                statuses.append(STATUS_OK if component_ok else STATUS_PARTIAL)
            else:
                out_geometries.append(MultiLineString(component_lines))
                statuses.append(STATUS_OK if component_ok else STATUS_PARTIAL)
            segment_status.append(component_records)
            station_lookup.append(component_stations)
            continue

        out_geometries.append(_transform_vertices(geometry, to_geog, to_target))
        statuses.append(STATUS_VERTEX_TRANSFORMED)
        segment_status.append(None)
        station_lookup.append([])

    return GeodesicProjectionResult(
        geometries=out_geometries,
        target_crs=target_crs,
        status=statuses,
        segment_status=segment_status,
        survey_stations=station_lookup,
    )
