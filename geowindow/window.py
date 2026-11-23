"""Area-weighted window reads with priority-based source composition.

For every output cell the reader intersects valid source pixels with the
cell polygon using exact convex-polygon clipping.  Sources are processed
in caller-supplied priority order: higher-priority valid coverage masks
lower-priority coverage, while missing pixels, missing thumbnail columns
and out-of-footprint regions fall through to the next source or level.

Boundary-only contact produces a zero-area intersection and therefore
never contributes.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy

from . import geometry
from .raster import GeoRaster, RasterLevel

AREA_EPS = 1e-9


@dataclass
class Contribution:
    """One effective piece of an output cell."""

    page: str
    level: int
    row: int
    col: int
    value: float
    area: float


@dataclass
class CellResult:
    """Composited value, coverage and provenance for one output cell."""

    row: int
    col: int
    value: float
    """Area-weighted mean of the contributing pieces (NaN if empty)."""
    coverage: float
    """Valid area divided by the full cell area."""
    contributions: list[Contribution] = field(default_factory=list)

    def find(self, page: str, row: int, col: int, level: int = 0):
        for contribution in self.contributions:
            if (
                contribution.page == page
                and contribution.row == row
                and contribution.col == col
                and contribution.level == level
            ):
                return contribution
        return None


@dataclass
class WindowResult:
    """Full output of a window read."""

    xmin: float
    ymin: float
    xmax: float
    ymax: float
    pixel_size: float
    values: numpy.ndarray
    coverage: numpy.ndarray
    cells: list[list[CellResult]]

    @property
    def shape(self) -> tuple[int, int]:
        return self.values.shape

    def cell(self, row: int, col: int) -> CellResult:
        return self.cells[row][col]


def difference(subject: geometry.Polygon, cutter: geometry.Polygon):
    """Subtract convex ``cutter`` from convex ``subject``.

    Returns a partition of the difference into disjoint convex polygons.
    The partition is built by walking the cutter edges: the part outside
    the current edge is finalized, the inside part continues.
    """
    if not subject:
        return []
    clip = geometry.ensure_ccw(cutter)
    inside = [geometry.ensure_ccw(subject)]
    outside_pieces: list[geometry.Polygon] = []
    for index, start in enumerate(clip):
        end = clip[(index + 1) % len(clip)]
        next_inside: list[geometry.Polygon] = []
        for polygon in inside:
            kept = geometry.clip_halfplane(polygon, start, end)
            if geometry.area(kept) > AREA_EPS:
                next_inside.append(kept)
            removed = geometry.clip_halfplane(
                polygon, end, start
            )
            if geometry.area(removed) > AREA_EPS:
                outside_pieces.append(removed)
        inside = next_inside
        if not inside:
            break
    return outside_pieces


def subtract_mask(
    polygon: geometry.Polygon, mask: list[geometry.Polygon]
) -> list[geometry.Polygon]:
    """Subtract a disjoint convex partition of covered area."""
    pieces = [polygon]
    for cutter in mask:
        remainders: list[geometry.Polygon] = []
        for piece in pieces:
            remainders.extend(difference(piece, cutter))
        pieces = remainders
        if not pieces:
            break
    return [piece for piece in pieces if geometry.area(piece) > AREA_EPS]


class WindowReader:
    """Reads map-aligned windows from a prioritized set of rasters."""

    def __init__(
        self,
        rasters: list[GeoRaster],
        priority: list[str] | None = None,
    ):
        self.rasters = {raster.name: raster for raster in rasters}
        if priority is None:
            priority = [raster.name for raster in rasters]
        self.priority = list(priority)

    def read(
        self,
        xmin: float,
        ymin: float,
        xmax: float,
        ymax: float,
        pixel_size: float,
        *,
        allow_thumbnail: bool = False,
        row_range: tuple[int, int] | None = None,
        col_range: tuple[int, int] | None = None,
    ) -> WindowResult:
        """Read the window, optionally restricted to output cell ranges."""
        ncols = int(round((xmax - xmin) / pixel_size))
        nrows = int(round((ymax - ymin) / pixel_size))
        values = numpy.full((nrows, ncols), numpy.nan, dtype=float)
        coverage = numpy.zeros((nrows, ncols), dtype=float)
        cells: list[list[CellResult | None]] = [
            [None] * ncols for _ in range(nrows)
        ]

        row_start, row_stop = row_range or (0, nrows)
        col_start, col_stop = col_range or (0, ncols)

        for out_row in range(row_start, row_stop):
            for out_col in range(col_start, col_stop):
                cell_polygon = geometry.rectangle(
                    xmin + out_col * pixel_size,
                    ymin + out_row * pixel_size,
                    xmin + (out_col + 1) * pixel_size,
                    ymin + (out_row + 1) * pixel_size,
                )
                result = self._read_cell(
                    cell_polygon,
                    out_row,
                    out_col,
                    pixel_size,
                    allow_thumbnail,
                )
                cells[out_row][out_col] = result
                values[out_row, out_col] = result.value
                coverage[out_row, out_col] = result.coverage

        return WindowResult(
            xmin=xmin,
            ymin=ymin,
            xmax=xmax,
            ymax=ymax,
            pixel_size=pixel_size,
            values=values,
            coverage=coverage,
            cells=cells,  # type: ignore[arg-type]
        )

    def _read_cell(
        self,
        cell_polygon: geometry.Polygon,
        out_row: int,
        out_col: int,
        pixel_size: float,
        allow_thumbnail: bool,
    ) -> CellResult:
        cell_area = geometry.area(cell_polygon)
        mask: list[geometry.Polygon] = []
        contributions: list[Contribution] = []

        for name in self.priority:
            raster = self.rasters[name]
            if raster.conflict is not None:
                continue
            for level in self._candidate_levels(
                raster, pixel_size, allow_thumbnail
            ):
                new_pieces = self._level_pieces(level, cell_polygon)
                for polygon, pixel_row, pixel_col in new_pieces:
                    remainders = subtract_mask(polygon, mask)
                    if not remainders:
                        continue
                    value = level.pixel_value(pixel_row, pixel_col)
                    total_area = sum(
                        geometry.area(piece) for piece in remainders
                    )
                    contributions.append(
                        Contribution(
                            page=name,
                            level=level.level,
                            row=pixel_row,
                            col=pixel_col,
                            value=value,
                            area=total_area,
                        )
                    )
                    mask.extend(remainders)

        valid_area = sum(item.area for item in contributions)
        if valid_area > AREA_EPS:
            weighted = sum(
                item.area * item.value for item in contributions
            )
            value = weighted / valid_area
        else:
            value = numpy.nan
        return CellResult(
            row=out_row,
            col=out_col,
            value=value,
            coverage=valid_area / cell_area,
            contributions=contributions,
        )

    def _candidate_levels(
        self,
        raster: GeoRaster,
        pixel_size: float,
        allow_thumbnail: bool,
    ) -> list[RasterLevel]:
        if not allow_thumbnail:
            # Fine reads never select thumbnails; base level only.
            return [raster.base]
        preferred = raster.level_for_resolution(
            pixel_size, allow_thumbnail=True
        )
        ordered = [preferred]
        for level in raster.levels:
            if level is not preferred:
                ordered.append(level)
        return ordered

    def _level_pieces(
        self,
        level: RasterLevel,
        cell_polygon: geometry.Polygon,
    ):
        footprint = level.footprint()
        overlap = geometry.intersect(footprint, cell_polygon)
        if not overlap:
            return []
        xmin, ymin, xmax, ymax = geometry.bbox(overlap)
        row_min, row_max, col_min, col_max = self._pixel_range(
            level, xmin, ymin, xmax, ymax
        )
        pieces = []
        for row in range(row_min, row_max + 1):
            for col in range(col_min, col_max + 1):
                if not level.is_valid(row, col):
                    continue
                pixel = level.pixel_polygon(row, col)
                intersection = geometry.intersect(pixel, cell_polygon)
                if intersection:
                    pieces.append((intersection, row, col))
        return pieces

    def _pixel_range(
        self,
        level: RasterLevel,
        xmin: float,
        ymin: float,
        xmax: float,
        ymax: float,
    ) -> tuple[int, int, int, int]:
        inverse = geometry.invert_affine(level.transform)
        corners = [
            (xmin, ymin),
            (xmax, ymin),
            (xmax, ymax),
            (xmin, ymax),
        ]
        raster = geometry.transform(inverse, corners)
        cols = [p[0] for p in raster]
        rows = [p[1] for p in raster]
        col_min = max(0, int(numpy.floor(min(cols))))
        col_max = min(level.cols - 1, int(numpy.ceil(max(cols))) - 1)
        row_min = max(0, int(numpy.floor(min(rows))))
        row_max = min(level.rows - 1, int(numpy.ceil(max(rows))) - 1)
        return row_min, row_max, col_min, col_max
