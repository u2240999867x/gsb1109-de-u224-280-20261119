"""In-memory raster pages built from GeoTIFF files.

A :class:`GeoRaster` owns one or more :class:`RasterLevel` instances
(typically a base level plus thumbnails stored as SubIFDs).  Each level
has its own calibration and data array, so a thumbnail that is one column
short only covers the footprint implied by its actual dimensions.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy

from . import geometry
from .calibration import Calibration, parse_calibration


@dataclass
class RasterLevel:
    """One pyramid level of a raster page."""

    name: str
    level: int
    data: numpy.ndarray
    calibration: Calibration
    nodata: float | None
    """Declared missing-data sentinel; ``None`` means no declaration."""

    @property
    def rows(self) -> int:
        return self.calibration.shape[0]

    @property
    def cols(self) -> int:
        return self.calibration.shape[1]

    @property
    def transform(self) -> numpy.ndarray:
        return self.calibration.transform

    def footprint(self) -> geometry.Polygon:
        """Outer-corner footprint of every pixel in this level."""
        corners = [
            (0.0, 0.0),
            (float(self.cols), 0.0),
            (float(self.cols), float(self.rows)),
            (0.0, float(self.rows)),
        ]
        return geometry.ensure_ccw(
            geometry.transform(self.transform, corners)
        )

    def pixel_polygon(self, row: int, col: int) -> geometry.Polygon:
        """Map-space outer-corner polygon of one pixel."""
        corners = [
            (float(col), float(row)),
            (float(col + 1), float(row)),
            (float(col + 1), float(row + 1)),
            (float(col), float(row + 1)),
        ]
        return geometry.ensure_ccw(
            geometry.transform(self.transform, corners)
        )

    def is_valid(self, row: int, col: int) -> bool:
        """Return whether a pixel holds an effective reading.

        A zero is valid unless the page explicitly declares it as NoData.
        """
        value = self.data[row, col]
        if self.nodata is None:
            return True
        if numpy.isnan(self.nodata):
            return not numpy.isnan(value)
        return value != self.nodata

    def pixel_value(self, row: int, col: int) -> float:
        return float(self.data[row, col])


@dataclass
class GeoRaster:
    """A named raster page with one or more pyramid levels."""

    name: str
    source_path: str | None = None
    levels: list[RasterLevel] = field(default_factory=list)
    conflict: dict | None = None
    """Tie-point conflict diagnostics for rejected pages."""

    @property
    def base(self) -> RasterLevel:
        return self.levels[0]

    @property
    def calibration_ok(self) -> bool:
        if self.conflict is not None:
            return False
        return all(
            level.calibration.consistent for level in self.levels
        )

    def level_for_resolution(
        self, output_pixel_size: float, *, allow_thumbnail: bool
    ) -> RasterLevel:
        """Pick the coarsest level whose pixel size fits the output grid.

        Thumbnails may only be selected when ``allow_thumbnail`` is True
        and their pixel size is no finer than half the output pixel size
        (they must genuinely be coarser than the base level).
        """
        base = self.base
        base_size = _pixel_size(base.transform)
        chosen = base
        if allow_thumbnail:
            for level in self.levels[1:]:
                size = _pixel_size(level.transform)
                if size >= 0.5 * output_pixel_size and size > base_size:
                    chosen = level
        return chosen


def _pixel_size(transform: numpy.ndarray) -> float:
    col_vector = transform[:2, 0]
    return float(numpy.linalg.norm(col_vector))


def _declared_nodata(page) -> float | None:
    tags = page.tags
    value = tags.valueof(42113)  # GDAL_NODATA
    if value is None:
        return None
    try:
        return float(str(value).replace(',', '.'))
    except (ValueError, TypeError):
        return None


def load_georaster(tif, name: str, path: str | None = None) -> GeoRaster:
    """Load all levels (pages plus SubIFDs) of an open TIFF."""
    raster = GeoRaster(name=name, source_path=path)
    seen: set[int] = set()
    for page in tif.pages:
        _load_page(tif, page, name, raster, seen, base=True)
    return raster


def _load_page(tif, page, name, raster: GeoRaster, seen, *, base: bool):
    key = id(page)
    if key in seen:
        return
    seen.add(key)
    calibration = parse_calibration(page)
    if calibration is None:
        return
    data = page.asarray()
    level = RasterLevel(
        name=name,
        level=len(raster.levels),
        data=data,
        calibration=calibration,
        nodata=_declared_nodata(page),
    )
    raster.levels.append(level)
    if not calibration.consistent and base:
        bad = next(
            check for check in calibration.checks if not check.consistent
        )
        raster.conflict = {
            'page': name,
            'level': level.level,
            'raster_pixel': bad.raster,
            'declared_map': bad.declared_map,
            'matrix_map': bad.matrix_map,
            'residual': bad.residual,
        }
    subifd_offsets = getattr(page, 'subifds', None) or ()
    for offset in subifd_offsets:
        if offset < 8:
            continue
        from .tifffile_compat import make_subifd_page

        subpage = make_subifd_page(tif, offset)
        _load_page(tif, subpage, name, raster, seen, base=False)
