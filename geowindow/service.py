"""High-level services: loading, diagnostics, tiled and coarse reads."""

from __future__ import annotations

import numpy
from tifffile import TiffFile

from .raster import GeoRaster, load_georaster
from .window import WindowReader, WindowResult


def open_rasters(files: dict[str, str]) -> dict[str, GeoRaster]:
    """Load named GeoTIFF files into :class:`GeoRaster` objects."""
    rasters: dict[str, GeoRaster] = {}
    for name, path in files.items():
        with TiffFile(path) as tif:
            rasters[name] = load_georaster(tif, name, path=path)
    return rasters


def calibration_report(rasters: dict[str, GeoRaster]) -> list[dict]:
    """Return raw calibration evidence for every page and level."""
    reports: list[dict] = []
    for name, raster in rasters.items():
        for level in raster.levels:
            cal = level.calibration
            reports.append(
                {
                    'page': name,
                    'level': level.level,
                    'shape': cal.shape,
                    'convention': cal.convention,
                    'matrix_source': cal.matrix_source,
                    'transform': cal.transform.tolist(),
                    'raster_type': cal.raster_type,
                    'nodata': level.nodata,
                    'tiepoints': [
                        {
                            'raster': check.raster,
                            'declared_map': check.declared_map,
                            'matrix_map': check.matrix_map,
                            'residual': check.residual,
                            'consistent': check.consistent,
                        }
                        for check in cal.checks
                    ],
                }
            )
    return reports


def conflict_report(rasters: dict[str, GeoRaster]) -> list[dict]:
    """Return conflict diagnostics for pages rejected from compositing."""
    return [
        raster.conflict
        for raster in rasters.values()
        if raster.conflict is not None
    ]


def composite_reader(
    rasters: dict[str, GeoRaster], priority: list[str]
) -> WindowReader:
    """Build a reader; conflicting pages are skipped during compositing."""
    ordered = [rasters[name] for name in priority if name in rasters]
    return WindowReader(ordered, priority=priority)


def read_tiled(
    reader: WindowReader,
    xmin: float,
    ymin: float,
    xmax: float,
    ymax: float,
    pixel_size: float,
    *,
    split_col: int | None = None,
    allow_thumbnail: bool = False,
) -> WindowResult:
    """Read a window as two column tiles with swapped read order.

    The right tile is read first, then the left tile, and the pieces are
    stitched back into full-window arrays cell by cell.
    """
    ncols = int(round((xmax - xmin) / pixel_size))
    nrows = int(round((ymax - ymin) / pixel_size))
    if split_col is None:
        split_col = ncols // 2

    right = reader.read(
        xmin,
        ymin,
        xmax,
        ymax,
        pixel_size,
        allow_thumbnail=allow_thumbnail,
        col_range=(split_col, ncols),
    )
    left = reader.read(
        xmin,
        ymin,
        xmax,
        ymax,
        pixel_size,
        allow_thumbnail=allow_thumbnail,
        col_range=(0, split_col),
    )

    values = numpy.full((nrows, ncols), numpy.nan, dtype=float)
    coverage = numpy.zeros((nrows, ncols), dtype=float)
    cells: list[list] = [[None] * ncols for _ in range(nrows)]
    for result in (right, left):
        for row in range(nrows):
            for col in range(ncols):
                cell = result.cells[row][col]
                if cell is not None:
                    values[row, col] = cell.value
                    coverage[row, col] = cell.coverage
                    cells[row][col] = cell
    return WindowResult(
        xmin=xmin,
        ymin=ymin,
        xmax=xmax,
        ymax=ymax,
        pixel_size=pixel_size,
        values=values,
        coverage=coverage,
        cells=cells,
    )


def results_equal(a: WindowResult, b: WindowResult, tol: float = 1e-9) -> bool:
    """Compare two reads cell by cell including provenance."""
    if a.shape != b.shape:
        return False
    for row in range(a.shape[0]):
        for col in range(a.shape[1]):
            ca = a.cell(row, col)
            cb = b.cell(row, col)
            if not _cells_equal(ca, cb, tol):
                return False
    return True


def _cells_equal(ca, cb, tol: float) -> bool:
    if not numpy.isclose(ca.coverage, cb.coverage, atol=tol, rtol=tol):
        return False
    if numpy.isnan(ca.value) != numpy.isnan(cb.value):
        return False
    if not numpy.isnan(ca.value) and not numpy.isclose(
        ca.value, cb.value, atol=tol, rtol=tol
    ):
        return False
    if len(ca.contributions) != len(cb.contributions):
        return False

    def contribution_key(item):
        return (item.page, item.level, item.row, item.col)

    for pa, pb in zip(
        sorted(ca.contributions, key=contribution_key),
        sorted(cb.contributions, key=contribution_key),
    ):
        if contribution_key(pa) != contribution_key(pb):
            return False
        if not numpy.isclose(pa.area, pb.area, atol=tol, rtol=tol):
            return False
        if not numpy.isclose(pa.value, pb.value, atol=tol, rtol=tol):
            return False
    return True


def read_pixel(
    raster: GeoRaster, row: int, col: int, level: int = 0
) -> dict:
    """Read one pixel directly, including from conflict-rejected pages."""
    raster_level = raster.levels[level]
    return {
        'page': raster.name,
        'level': level,
        'row': row,
        'col': col,
        'value': raster_level.pixel_value(row, col),
        'valid': raster_level.is_valid(row, col),
        'nodata': raster_level.nodata,
        'map_corners': [
            raster_level.calibration.pixel_to_map(col, row),
            raster_level.calibration.pixel_to_map(col + 1, row),
            raster_level.calibration.pixel_to_map(col + 1, row + 1),
            raster_level.calibration.pixel_to_map(col, row + 1),
        ],
    }
