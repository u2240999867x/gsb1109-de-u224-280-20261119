"""Convex polygon geometry used for area-weighted window reads.

Everything operates on simple convex polygons represented as lists of
``(x, y)`` vertices in counter-clockwise order.  The map grid uses the
pixel outer-corner convention and boundary-only contact has zero area.
"""

from __future__ import annotations

from typing import Sequence

import numpy

Point = tuple[float, float]
Polygon = list[Point]

AREA_EPS = 1e-9


def signed_area(polygon: Sequence[Point]) -> float:
    """Return signed polygon area (positive for counter-clockwise)."""
    if len(polygon) < 3:
        return 0.0
    total = 0.0
    prev = polygon[-1]
    for point in polygon:
        total += prev[0] * point[1] - point[0] * prev[1]
        prev = point
    return 0.5 * total


def area(polygon: Sequence[Point]) -> float:
    """Return the absolute area of a polygon."""
    return abs(signed_area(polygon))


def ensure_ccw(polygon: Sequence[Point]) -> Polygon:
    """Return a counter-clockwise copy of a polygon."""
    points = list(polygon)
    if signed_area(points) < 0:
        points.reverse()
    return points


def bbox(polygon: Sequence[Point]) -> tuple[float, float, float, float]:
    """Return ``(xmin, ymin, xmax, ymax)``."""
    xs = [p[0] for p in polygon]
    ys = [p[1] for p in polygon]
    return min(xs), min(ys), max(xs), max(ys)


def transform(matrix: numpy.ndarray, polygon: Sequence[Point]) -> Polygon:
    """Apply a 3x3 homogeneous affine matrix to a polygon."""
    out: Polygon = []
    for x, y in polygon:
        v = matrix @ numpy.array([x, y, 1.0])
        out.append((float(v[0]), float(v[1])))
    return out


def invert_affine(matrix: numpy.ndarray) -> numpy.ndarray:
    """Invert a 3x3 affine matrix."""
    return numpy.linalg.inv(matrix)


def rectangle(
    xmin: float, ymin: float, xmax: float, ymax: float
) -> Polygon:
    """Return a counter-clockwise axis-aligned rectangle."""
    return [
        (xmin, ymin),
        (xmax, ymin),
        (xmax, ymax),
        (xmin, ymax),
    ]


def _line_intersection(
    start: Point,
    end: Point,
    fs: float,
    fe: float,
) -> Point:
    t = fs / (fs - fe)
    return (
        start[0] + t * (end[0] - start[0]),
        start[1] + t * (end[1] - start[1]),
    )


def clip_halfplane(
    subject: Sequence[Point],
    edge_start: Point,
    edge_end: Point,
) -> Polygon:
    """Clip a polygon against the left half-plane of an oriented edge.

    For a counter-clockwise clipping polygon the interior is on the left
    of every directed edge.  Points exactly on the edge are kept, but the
    resulting degenerate pieces collapse to zero area.
    """
    if not subject:
        return []
    ex = edge_end[0] - edge_start[0]
    ey = edge_end[1] - edge_start[1]

    def side(point: Point) -> float:
        return ex * (point[1] - edge_start[1]) - ey * (
            point[0] - edge_start[0]
        )

    result: Polygon = []
    previous = subject[-1]
    prev_side = side(previous)
    for current in subject:
        cur_side = side(current)
        if cur_side >= 0.0:
            if prev_side < 0.0:
                result.append(
                    _line_intersection(previous, current, prev_side, cur_side)
                )
            result.append(current)
        elif prev_side >= 0.0:
            result.append(
                _line_intersection(previous, current, prev_side, cur_side)
            )
        previous = current
        prev_side = cur_side
    return _dedupe(result)


def _dedupe(polygon: Polygon) -> Polygon:
    if not polygon:
        return []
    out: Polygon = []
    for point in polygon:
        if not out or abs(point[0] - out[-1][0]) > 1e-12 or abs(
            point[1] - out[-1][1]
        ) > 1e-12:
            out.append(point)
    if len(out) > 1 and (
        abs(out[0][0] - out[-1][0]) <= 1e-12
        and abs(out[0][1] - out[-1][1]) <= 1e-12
    ):
        out.pop()
    return out


def intersect(
    polygon_a: Sequence[Point], polygon_b: Sequence[Point]
) -> Polygon:
    """Intersect two convex polygons (Sutherland-Hodgman clipping)."""
    clip = ensure_ccw(polygon_b)
    result = ensure_ccw(polygon_a)
    for index, start in enumerate(clip):
        if not result:
            return []
        end = clip[(index + 1) % len(clip)]
        result = clip_halfplane(result, start, end)
    if area(result) <= AREA_EPS:
        return []
    return result


def point_in_polygon(point: Point, polygon: Sequence[Point]) -> bool:
    """Return True if a point is inside or on the boundary of a polygon."""
    poly = ensure_ccw(polygon)
    x, y = point
    for index, start in enumerate(poly):
        end = poly[(index + 1) % len(poly)]
        cross = (end[0] - start[0]) * (y - start[1]) - (
            end[1] - start[1]
        ) * (x - start[0])
        if cross < -1e-12:
            return False
    return True
