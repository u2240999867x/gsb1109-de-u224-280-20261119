"""GeoTIFF calibration parsing and tie-point consistency checks.

Two raster-space conventions are supported:

* ``pixel-area`` (GeoTIFF ``RasterPixelIsArea``): matrix columns are pixel
  outer corners.  Pixel ``(col, row)`` occupies the square from
  ``(col, row)`` to ``(col + 1, row + 1)`` in raster coordinates.
* ``pixel-point`` (GeoTIFF ``RasterPixelIsPoint``): the matrix locates
  pixel centers.  Pixel ``(col, row)`` is centered at ``(col + 0.5,
  row + 0.5)``.

All internal transforms map raster coordinates to map coordinates with a
3x3 homogeneous matrix using the outer-corner convention, so pixel-point
pages get a half-pixel shift when parsed.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field

import numpy

# GeoTIFF tag IDs
MODEL_PIXEL_SCALE = 33550
MODEL_TIEPOINT = 33922
MODEL_TRANSFORMATION = 34264
GEO_KEY_DIRECTORY = 34735

# GeoKey IDs
GT_RASTER_TYPE = 1025
GT_MODEL_TYPE = 1024
PROJECTED_CS_TYPE = 3072

RASTER_PIXEL_IS_AREA = 1
RASTER_PIXEL_IS_POINT = 2

TIEPOINT_TOL = 1e-6


@dataclass
class TiepointCheck:
    """Result of checking one declared tie-point against the matrix."""

    raster: tuple[float, float]
    """Declared raster position ``(col, row)``."""
    declared_map: tuple[float, float]
    """Map coordinates stored in the tie-point."""
    matrix_map: tuple[float, float]
    """Map coordinates predicted by the matrix for the same pixel."""
    residual: float
    """Planar distance between declared and predicted coordinates."""
    consistent: bool


@dataclass
class Calibration:
    """Parsed calibration of one raster level."""

    transform: numpy.ndarray
    """3x3 affine matrix, raster outer-corner coordinates -> map."""
    convention: str
    """``pixel-area`` or ``pixel-point``."""
    matrix_source: str
    """``transformation`` or ``pixel-scale-tiepoint``."""
    shape: tuple[int, int]
    """Level shape ``(rows, cols)``."""
    tiepoints: list[tuple[float, float, float, float]] = field(
        default_factory=list
    )
    """Raw declared tie-points ``(col, row, x, y)``."""
    raster_type: int | None = None
    checks: list[TiepointCheck] = field(default_factory=list)

    @property
    def consistent(self) -> bool:
        """True when every declared tie-point agrees with the matrix."""
        return all(check.consistent for check in self.checks)

    def pixel_to_map(self, col: float, row: float) -> tuple[float, float]:
        """Map a raster outer-corner coordinate through the matrix."""
        v = self.transform @ numpy.array([col, row, 1.0])
        return float(v[0]), float(v[1])

    def declared_pixel_position(
        self, col: float, row: float
    ) -> tuple[float, float]:
        """Outer-frame raster coordinate of a declared pixel position."""
        if self.convention == 'pixel-point':
            return col + 0.5, row + 0.5
        return float(col), float(row)


def matrix_from_transformation(
    values: numpy.ndarray | list,
) -> numpy.ndarray:
    """Convert a GeoTIFF ModelTransformationTag value to a 3x3 matrix."""
    arr = numpy.asarray(values, dtype=float).reshape(4, 4)
    return numpy.array(
        [
            [arr[0, 0], arr[0, 1], arr[0, 3]],
            [arr[1, 0], arr[1, 1], arr[1, 3]],
            [0.0, 0.0, 1.0],
        ]
    )


def matrix_from_scale_tiepoint(
    scale: tuple[float, float],
    tiepoint: tuple[float, float, float, float],
    convention: str,
) -> numpy.ndarray:
    """Build an outer-corner raster->map matrix from scale plus tie-point.

    ``tiepoint`` is ``(col, row, x, y)`` and may use a non-zero pixel.
    """
    sx, sy = float(scale[0]), float(scale[1])
    col, row, map_x, map_y = (float(v) for v in tiepoint)
    if convention == 'pixel-point':
        # The tie-point raster coordinate is the pixel center; in the
        # outer-corner frame the same point sits at (col + 0.5, row + 0.5).
        col += 0.5
        row += 0.5
    origin_x = map_x - col * sx
    origin_y = map_y + row * sy
    return numpy.array(
        [
            [sx, 0.0, origin_x],
            [0.0, -sy, origin_y],
            [0.0, 0.0, 1.0],
        ]
    )


def parse_tiepoints(page) -> list[tuple[float, float, float, float]]:
    """Read raw ModelTiepointTag values from a tifffile page."""
    value = page.tags.valueof(MODEL_TIEPOINT)
    if value is None:
        return []
    arr = numpy.asarray(value, dtype=float).reshape(-1, 6)
    return [
        (float(row[0]), float(row[1]), float(row[3]), float(row[4]))
        for row in arr
    ]


def parse_calibration(page) -> Calibration | None:
    """Parse the calibration of a tifffile page.

    Returns ``None`` for pages without any georeferencing tags.
    """
    tags = page.tags
    shape = (int(page.imagelength), int(page.imagewidth))

    raster_type: int | None = None
    with contextlib.suppress(Exception):
        geo = page.geotiff_tags
    if geo is not None:
        raster_type_value = geo.get('GTRasterTypeGeoKey')
        if raster_type_value is not None:
            raster_type = int(raster_type_value)

    transformation = tags.valueof(MODEL_TRANSFORMATION)
    scale = tags.valueof(MODEL_PIXEL_SCALE)
    tiepoints = parse_tiepoints(page)

    if transformation is not None:
        convention = (
            'pixel-point'
            if raster_type == RASTER_PIXEL_IS_POINT
            else 'pixel-area'
        )
        declared = matrix_from_transformation(transformation)
        if convention == 'pixel-point':
            # Strict GeoTIFF: raster (col, row) is the pixel center, so the
            # outer-corner transform is the declared matrix shifted by
            # minus half a pixel in raster space.
            shift = numpy.eye(3)
            shift[0, 2] = -0.5
            shift[1, 2] = -0.5
            transform = declared @ shift
        else:
            transform = declared
        source = 'transformation'
    elif scale is not None and tiepoints:
        convention = (
            'pixel-point'
            if raster_type == RASTER_PIXEL_IS_POINT
            else 'pixel-area'
        )
        transform = matrix_from_scale_tiepoint(
            (scale[0], scale[1]), tiepoints[0], convention
        )
        source = 'pixel-scale-tiepoint'
    else:
        return None

    calibration = Calibration(
        transform=transform,
        convention=convention,
        matrix_source=source,
        shape=shape,
        tiepoints=tiepoints,
        raster_type=raster_type,
    )
    calibration.checks = check_tiepoints(calibration)
    return calibration


def check_tiepoints(
    calibration: Calibration,
    tol: float = TIEPOINT_TOL,
) -> list[TiepointCheck]:
    """Compare every declared tie-point with the matrix prediction."""
    checks: list[TiepointCheck] = []
    for col, row, map_x, map_y in calibration.tiepoints:
        raster_col, raster_row = calibration.declared_pixel_position(
            col, row
        )
        predicted_x, predicted_y = calibration.pixel_to_map(
            raster_col, raster_row
        )
        dx = predicted_x - map_x
        dy = predicted_y - map_y
        residual = float(numpy.hypot(dx, dy))
        checks.append(
            TiepointCheck(
                raster=(col, row),
                declared_map=(map_x, map_y),
                matrix_map=(predicted_x, predicted_y),
                residual=residual,
                consistent=residual <= tol,
            )
        )
    return checks


def transformation_tag(transform: numpy.ndarray):
    """Build a ModelTransformationTag extratag tuple."""
    matrix = numpy.eye(4)
    matrix[0, 0] = transform[0, 0]
    matrix[0, 1] = transform[0, 1]
    matrix[0, 3] = transform[0, 2]
    matrix[1, 0] = transform[1, 0]
    matrix[1, 1] = transform[1, 1]
    matrix[1, 3] = transform[1, 2]
    return (
        MODEL_TRANSFORMATION,
        'd',
        16,
        tuple(float(v) for v in matrix.reshape(-1)),
        True,
    )


def pixel_scale_tag(sx: float, sy: float):
    """Build a ModelPixelScaleTag extratag tuple."""
    return (MODEL_PIXEL_SCALE, 'd', 3, (float(sx), float(sy), 0.0), True)


def tiepoint_tag(points: list[tuple[float, float, float, float]]):
    """Build a ModelTiepointTag extratag tuple."""
    values: list[float] = []
    for col, row, map_x, map_y in points:
        values.extend(
            [float(col), float(row), 0.0, float(map_x), float(map_y), 0.0]
        )
    return (MODEL_TIEPOINT, 'd', len(values), tuple(values), True)


def raster_type_key(raster_type: int):
    """Build a GeoKeyDirectoryTag carrying only the raster type key."""
    directory = (
        1, 1, 0, 1,
        GT_RASTER_TYPE, 0, 1, raster_type,
    )
    return (GEO_KEY_DIRECTORY, 'H', len(directory), directory, True)


def projected_crs_keys(epsg: int, raster_type: int):
    """GeoKeyDirectoryTag for a projected CRS plus raster type."""
    directory = (
        1, 1, 0, 3,
        GT_MODEL_TYPE, 0, 1, 1,
        PROJECTED_CS_TYPE, 0, 1, epsg,
        GT_RASTER_TYPE, 0, 1, raster_type,
    )
    return (GEO_KEY_DIRECTORY, 'H', len(directory), directory, True)
