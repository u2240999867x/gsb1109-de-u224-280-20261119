"""Deterministic GeoTIFF scenario used by the test-suite and demos.

Three equally sized rotated pages A, B, C plus one ordinary axis-aligned
page D are written to a directory:

* ``A.tif``  : pixel-area matrix with rotation, no NoData declaration,
  real zero readings, plus a full 4x4 thumbnail level.
* ``B.tif``  : pixel-point matrix with the same rotation, several
  matrix-consistent tie-points, NoData explicitly declared as zero, and a
  4x3 thumbnail (one outer column missing).
* ``C.tif``  : pixel-area matrix overlapping B; its last tie-point
  contradicts the matrix.  Readable alone but rejected from compositing.
* ``D.tif``  : ordinary page with only ModelPixelScale and one
  non-zero-location tie-point.

The rotation uses sin=3/5, cos=4/5 so every map coordinate is rational.
"""

from __future__ import annotations

import os
from dataclasses import dataclass

import numpy as np
from tifffile import TiffWriter

from .calibration import (
    MODEL_PIXEL_SCALE,
    MODEL_TIEPOINT,
    raster_type_key,
    tiepoint_tag,
    transformation_tag,
)

PAGE_SIZE = 8
COS = 0.8
SIN = 0.6
GDAL_NODATA = 42113

# Fine output window (axis aligned map grid, outer-corner convention).
WINDOW = (0.0, 0.0, 8.0, 8.0)
FINE_PIXEL = 1.0
COARSE_PIXEL = 2.0


def rotation_matrix(cos: float, sin: float, origin_x: float, origin_y: float):
    return np.array(
        [
            [cos, -sin, origin_x],
            [sin, cos, origin_y],
            [0.0, 0.0, 1.0],
        ]
    )


def scaled_rotation(scale: float, origin_x: float, origin_y: float):
    return rotation_matrix(
        scale * COS, scale * SIN, origin_x, origin_y
    )


def apply(matrix, col, row):
    v = matrix @ np.array([float(col), float(row), 1.0])
    return float(v[0]), float(v[1])


def nodata_tag(value: str):
    return (GDAL_NODATA, 2, None, value, True)


@dataclass
class ScenarioPaths:
    directory: str

    def page(self, name: str) -> str:
        return os.path.join(self.directory, f'{name}.tif')


def build_scenario(directory: str) -> ScenarioPaths:
    os.makedirs(directory, exist_ok=True)
    paths = ScenarioPaths(directory)

    # A: pixel-area rotated matrix, origin at (0, 0).
    transform_a = rotation_matrix(COS, SIN, 0.0, 0.0)
    data_a = np.full((PAGE_SIZE, PAGE_SIZE), 10.0)
    data_a[2, 2] = 0.0  # real zero, no NoData declaration -> valid
    data_a[5, 1] = 0.0
    thumb_a = np.full((4, 4), 10.0)
    thumb_transform_a = scaled_rotation(2.0, 0.0, 0.0)
    tags_a = [
        transformation_tag(transform_a),
        raster_type_key(1),
    ]
    tags_a_thumb = [
        transformation_tag(thumb_transform_a),
        raster_type_key(1),
    ]
    with TiffWriter(paths.page('A')) as tif:
        tif.write(data_a, subifds=1, extratags=tags_a)
        tif.write(thumb_a, subfiletype=1, extratags=tags_a_thumb)

    # B: pixel-point rotated matrix.  Outer footprint starts at 3*u.
    origin_b = 3.0 * np.array([COS, SIN])  # (2.4, 1.8)
    outer_b = rotation_matrix(COS, SIN, float(origin_b[0]), float(origin_b[1]))
    # Strict RasterPixelIsPoint: declared matrix maps raster (col, row),
    # the pixel center, whose outer-corner raster coords are +0.5.
    center_shift = np.eye(3)
    center_shift[0, 2] = 0.5
    center_shift[1, 2] = 0.5
    declared_b = outer_b @ center_shift
    data_b = np.full((PAGE_SIZE, PAGE_SIZE), 20.0)
    # Declared NoData holes inside the A/B overlap so A can backfill them.
    # Pixel (row 0, col 3) center maps into fine output cell (row 4, col 4);
    # pixel (row 0, col 4) center maps into fine output cell (row 4, col 5).
    data_b[0, 3] = 0.0
    data_b[0, 4] = 0.0
    # thumbnail is one column short: 4 rows x 3 cols, pixel size 2.
    thumb_b = np.full((4, 3), 20.0)
    # NoData hole whose center maps into coarse output cell (row 3, col 0).
    thumb_b[2, 1] = 0.0
    thumb_outer_b = scaled_rotation(
        2.0, float(origin_b[0]), float(origin_b[1])
    )
    thumb_declared_b = thumb_outer_b @ center_shift
    b_pixels = [(0, 0), (4, 0), (0, 4), (7, 7)]
    # Pixel-point page: tie-points identify pixel centers, whose
    # outer-corner raster coordinates are (col + 0.5, row + 0.5).
    b_tiepoints = [
        (float(c), float(r), *apply(outer_b, c + 0.5, r + 0.5))
        for c, r in b_pixels
    ]
    tags_b = [
        transformation_tag(declared_b),
        raster_type_key(2),
        tiepoint_tag(b_tiepoints),
        nodata_tag('0'),
    ]
    tags_b_thumb = [
        transformation_tag(thumb_declared_b),
        raster_type_key(2),
        nodata_tag('0'),
    ]
    with TiffWriter(paths.page('B')) as tif:
        tif.write(data_b, subifds=1, extratags=tags_b)
        tif.write(thumb_b, subfiletype=1, extratags=tags_b_thumb)

    # C: pixel-area matrix overlapping B; last tie-point is contradictory.
    offset_c = origin_b + 0.5 * np.array([COS, SIN]) + 0.5 * np.array(
        [-SIN, COS]
    )
    transform_c = rotation_matrix(
        COS, SIN, float(offset_c[0]), float(offset_c[1])
    )
    data_c = np.full((PAGE_SIZE, PAGE_SIZE), 30.0)
    good_pixels = [(0, 0), (3, 3), (7, 0)]
    tiepoints_c = [
        (float(c), float(r), *apply(transform_c, c, r))
        for c, r in good_pixels
    ]
    bad_x, bad_y = apply(transform_c, 7, 7)
    tiepoints_c.append((7.0, 7.0, bad_x + 1.5, bad_y - 0.7))
    tags_c = [
        transformation_tag(transform_c),
        raster_type_key(1),
        tiepoint_tag(tiepoints_c),
    ]
    with TiffWriter(paths.page('C')) as tif:
        tif.write(data_c, extratags=tags_c)

    # D: plain page, pixel scale plus one non-zero-location tie-point.
    data_d = np.full((PAGE_SIZE, PAGE_SIZE), 40.0)
    # map = (10 + col, 18 - row); pixel (2, 2) -> (12, 16)
    scale_tag = (MODEL_PIXEL_SCALE, 'd', 3, (1.0, 1.0, 0.0), True)
    d_tie = (
        MODEL_TIEPOINT,
        'd',
        6,
        (2.0, 2.0, 0.0, 12.0, 16.0, 0.0),
        True,
    )
    tags_d = [scale_tag, d_tie, raster_type_key(1)]
    with TiffWriter(paths.page('D')) as tif:
        tif.write(data_d, extratags=tags_d)

    return paths


# Exposed for assertions and standalone reads.
SCENARIO_CONSTANTS = {
    'page_size': PAGE_SIZE,
    'cos': COS,
    'sin': SIN,
    'origin_b': (2.4, 1.8),
    'd_window': (10.0, 10.0, 18.0, 18.0),
    'd_origin': (10.0, 18.0),
    'c_conflict_shift': (1.5, -0.7),
    'c_bad_pixel': (7, 7),
    'b_thumbnail_shape': (4, 3),
    'a_thumbnail_shape': (4, 4),
}
