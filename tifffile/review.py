# review.py

"""Physical pyramid crops and revision-safe review mosaics.

This module implements the review side of a whole-slide scanning workflow.
Calibrated crops are extracted from pyramid levels of a TIFF file with
:py:func:`crop_from_tiff`, or built from in-memory arrays with
:py:func:`crop_from_array`.  The crops are next delivered to a
:py:class:`ReviewMosaic`; subsequent review queries are answered only from
the delivered crops and do not read the source TIFF again.

Geometry is evaluated with exact rational arithmetic.  A source pixel is
included in a crop when it overlaps the requested physical window with
positive area.  Every pixel contribution retains its source row and column,
raw pixel value, the full physical bounds of the pixel, and the part of the
pixel that the window actually covers.

Work orders are versioned.  A revision becomes visible only after every
calibrated pyramid level of that revision has been delivered; at that point
all levels switch together.  Re-delivering an old revision, or re-delivering
a level of the current revision, has no effect.  :py:meth:`ReviewMosaic.preview`
returns an immutable publication snapshot.  A preview opened before a later
publication keeps returning the earlier view.

Partition queries use a spatial index of the published crop regions.  A
publication copies cached partitions that are not touched by the new or old
crop windows of the corrected work order, so a correction affects only the
display partitions that its old and new windows intersect.

"""

from __future__ import annotations

import dataclasses
import math
import threading
from fractions import Fraction
from typing import TYPE_CHECKING

import numpy

if TYPE_CHECKING:
    from collections.abc import Iterable, Iterator, Mapping, Sequence
    from typing import Any

    from numpy.typing import ArrayLike, NDArray

    from .tifffile import TiffFile, TiffPage

__all__ = [
    'CropPatch',
    'CropPixel',
    'DisplayPartition',
    'LayerCalibration',
    'LayerQueryResult',
    'MosaicPreview',
    'PartitionMosaic',
    'PhysicalWindow',
    'PixelContribution',
    'ReviewMosaic',
    'UnlocatableLevelError',
    'assemble',
    'crop_from_array',
    'crop_from_page',
    'crop_from_tiff',
]


class UnlocatableLevelError(ValueError):
    """Pyramid level has no calibration and cannot be positioned."""


_UNIT_FACTORS: dict[str, Fraction] = {
    'nm': Fraction(1, 1000),
    'nanometer': Fraction(1, 1000),
    'nanometers': Fraction(1, 1000),
    'nanometre': Fraction(1, 1000),
    'nanometres': Fraction(1, 1000),
    'um': Fraction(1),
    'µm': Fraction(1),
    'μm': Fraction(1),
    'micron': Fraction(1),
    'microns': Fraction(1),
    'micrometer': Fraction(1),
    'micrometers': Fraction(1),
    'micrometre': Fraction(1),
    'micrometres': Fraction(1),
    'mm': Fraction(1000),
    'millimeter': Fraction(1000),
    'millimeters': Fraction(1000),
    'millimetre': Fraction(1000),
    'millimetres': Fraction(1000),
    'cm': Fraction(10000),
    'centimeter': Fraction(10000),
    'centimeters': Fraction(10000),
    'centimetre': Fraction(10000),
    'centimetres': Fraction(10000),
    'm': Fraction(1000000),
    'meter': Fraction(1000000),
    'meters': Fraction(1000000),
    'metre': Fraction(1000000),
    'metres': Fraction(1000000),
}


def _fraction(value: Any, name: str) -> Fraction:
    if isinstance(value, Fraction):
        return value
    if isinstance(value, bool):
        msg = f'{name} must be a finite number'
        raise ValueError(msg)
    if isinstance(value, int):
        return Fraction(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            msg = f'{name} must be a finite number'
            raise ValueError(msg)
        return Fraction(value)
    if isinstance(value, (str, bytes)):
        try:
            return Fraction(value)
        except (ValueError, ZeroDivisionError) as exc:
            msg = f'{name} must be a finite number'
            raise ValueError(msg) from exc
    try:
        return Fraction(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, ZeroDivisionError) as exc:
        msg = f'{name} must be a finite number'
        raise ValueError(msg) from exc


def _unit_factor(unit: str) -> Fraction:
    try:
        return _UNIT_FACTORS[unit.strip().lower()]
    except (AttributeError, KeyError):
        msg = f'unknown length unit {unit!r}'
        raise ValueError(msg) from None


def _floor(value: Fraction) -> int:
    return value.numerator // value.denominator


def _ceil(value: Fraction) -> int:
    return -((-value.numerator) // value.denominator)


@dataclasses.dataclass(frozen=True, order=True)
class PhysicalWindow:
    """Half-open physical rectangle ``[x0, x1) x [y0, y1)``.

    Parameters:
        x0, x1, y0, y1:
            Rectangle bounds.  Integers, floats, fractions, and numeric
            strings are accepted and stored as exact fractions.
        unit:
            Length unit.  Common metric aliases such as ``'nm'``, ``'um'``,
            ``'µm'``, and ``'mm'`` are accepted.

    """

    x0: Any
    x1: Any
    y0: Any
    y1: Any
    unit: str = 'um'

    def __post_init__(self) -> None:
        for name in ('x0', 'x1', 'y0', 'y1'):
            object.__setattr__(
                self, name, _fraction(getattr(self, name), name)
            )
        _unit_factor(self.unit)
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            msg = 'window must have positive area'
            raise ValueError(msg)

    @property
    def width(self) -> Fraction:
        return self.x1 - self.x0

    @property
    def height(self) -> Fraction:
        return self.y1 - self.y0

    @property
    def area(self) -> Fraction:
        """Area in square micrometers."""
        micron = self.to_micrometer()
        return micron.width * micron.height

    @property
    def area_native(self) -> Fraction:
        """Area in square units of :py:attr:`unit`."""
        return self.width * self.height

    def to_micrometer(self) -> PhysicalWindow:
        """Return window converted to micrometer."""
        factor = _unit_factor(self.unit)
        if factor == 1:
            return self
        return PhysicalWindow(
            self.x0 * factor,
            self.x1 * factor,
            self.y0 * factor,
            self.y1 * factor,
            'um',
        )

    def to_unit(self, unit: str) -> PhysicalWindow:
        """Return window converted to unit."""
        if unit == self.unit:
            return self
        factor = _unit_factor(self.unit) / _unit_factor(unit)
        return PhysicalWindow(
            self.x0 * factor,
            self.x1 * factor,
            self.y0 * factor,
            self.y1 * factor,
            unit,
        )

    def intersects(self, other: PhysicalWindow) -> bool:
        """Return whether windows share a positive-area region."""
        left = self.to_micrometer()
        right = other.to_micrometer()
        return (
            left.x0 < right.x1
            and right.x0 < left.x1
            and left.y0 < right.y1
            and right.y0 < left.y1
        )

    def intersection(self, other: PhysicalWindow) -> PhysicalWindow | None:
        """Return shared rectangle, or ``None`` for zero-area overlap."""
        left = self.to_micrometer()
        right = other.to_micrometer()
        x0 = max(left.x0, right.x0)
        x1 = min(left.x1, right.x1)
        y0 = max(left.y0, right.y0)
        y1 = min(left.y1, right.y1)
        if x1 <= x0 or y1 <= y0:
            return None
        return PhysicalWindow(x0, x1, y0, y1, 'um')

    def intersection_area(self, other: PhysicalWindow) -> Fraction:
        """Return shared area in square micrometers."""
        overlap = self.intersection(other)
        return Fraction(0) if overlap is None else overlap.area


def _as_window(
    window: PhysicalWindow | Sequence[Any] | Mapping[str, Any],
    unit: str | None = None,
) -> PhysicalWindow:
    if isinstance(window, PhysicalWindow):
        return window
    if isinstance(window, Mapping):
        try:
            x0 = window['x0']
            x1 = window['x1']
            y0 = window['y0']
            y1 = window['y1']
        except KeyError as exc:
            msg = f'missing window bound {exc.args[0]!r}'
            raise ValueError(msg) from exc
        window_unit = window.get('unit', unit)
    else:
        try:
            x0, x1, y0, y1 = window
        except (TypeError, ValueError) as exc:
            msg = 'window must be (x0, x1, y0, y1)'
            raise ValueError(msg) from exc
        window_unit = unit
    if window_unit is None:
        msg = 'window unit is required'
        raise ValueError(msg)
    return PhysicalWindow(x0, x1, y0, y1, str(window_unit))


@dataclasses.dataclass(frozen=True)
class LayerCalibration:
    """Physical calibration of one pyramid level.

    Parameters:
        width, height:
            Number of source pixels in X and Y.
        origin_x, origin_y:
            Physical coordinate of the top-left outer corner of the
            top-left source pixel.
        pixel_width, pixel_height:
            Physical size of one source pixel in X and Y.
        unit:
            Length unit of the physical coordinates and pixel sizes.

    """

    width: int
    height: int
    origin_x: Any
    origin_y: Any
    pixel_width: Any
    pixel_height: Any
    unit: str = 'um'

    def __post_init__(self) -> None:
        width = _fraction(self.width, 'width')
        height = _fraction(self.height, 'height')
        if width.denominator != 1 or width <= 0:
            msg = 'width must be a positive integer'
            raise ValueError(msg)
        if height.denominator != 1 or height <= 0:
            msg = 'height must be a positive integer'
            raise ValueError(msg)
        object.__setattr__(self, 'width', int(width))
        object.__setattr__(self, 'height', int(height))
        object.__setattr__(
            self, 'origin_x', _fraction(self.origin_x, 'origin_x')
        )
        object.__setattr__(
            self, 'origin_y', _fraction(self.origin_y, 'origin_y')
        )
        for name in ('pixel_width', 'pixel_height'):
            value = _fraction(getattr(self, name), name)
            if value <= 0:
                msg = f'{name} must be positive'
                raise ValueError(msg)
            object.__setattr__(self, name, value)
        _unit_factor(self.unit)

    @property
    def shape(self) -> tuple[int, int]:
        return (self.height, self.width)

    def to_unit(self, unit: str) -> LayerCalibration:
        """Return calibration converted to unit."""
        if unit == self.unit:
            return self
        factor = _unit_factor(self.unit) / _unit_factor(unit)
        return LayerCalibration(
            self.width,
            self.height,
            self.origin_x * factor,
            self.origin_y * factor,
            self.pixel_width * factor,
            self.pixel_height * factor,
            unit,
        )

    def pixel_bounds(self, row: int, col: int) -> PhysicalWindow:
        """Return full physical bounds of source pixel (row, col)."""
        if not 0 <= row < self.height:
            msg = f'row {row} out of bounds [0, {self.height})'
            raise IndexError(msg)
        if not 0 <= col < self.width:
            msg = f'column {col} out of bounds [0, {self.width})'
            raise IndexError(msg)
        x0 = self.origin_x + col * self.pixel_width
        y0 = self.origin_y + row * self.pixel_height
        return PhysicalWindow(
            x0,
            x0 + self.pixel_width,
            y0,
            y0 + self.pixel_height,
            self.unit,
        )

    def source_bounds(self) -> PhysicalWindow:
        """Return physical bounds of all source pixels."""
        return PhysicalWindow(
            self.origin_x,
            self.origin_x + self.width * self.pixel_width,
            self.origin_y,
            self.origin_y + self.height * self.pixel_height,
            self.unit,
        )

    def index_range(
        self, window: PhysicalWindow
    ) -> tuple[range, range]:
        """Return source row and column ranges intersecting window.

        A pixel is included only when it overlaps the window with positive
        area.  Pixels that merely touch a window boundary are excluded.

        """
        local = window.to_unit(self.unit)
        x0 = (local.x0 - self.origin_x) / self.pixel_width
        x1 = (local.x1 - self.origin_x) / self.pixel_width
        y0 = (local.y0 - self.origin_y) / self.pixel_height
        y1 = (local.y1 - self.origin_y) / self.pixel_height
        start_col = max(0, _floor(x0))
        stop_col = min(self.width, _ceil(x1))
        start_row = max(0, _floor(y0))
        stop_row = min(self.height, _ceil(y1))
        return (
            range(start_row, max(start_row, stop_row)),
            range(start_col, max(start_col, stop_col)),
        )

    def covers(self, window: PhysicalWindow) -> bool:
        """Return whether window lies within source bounds."""
        source = self.source_bounds().to_micrometer()
        target = window.to_micrometer()
        return (
            source.x0 <= target.x0
            and target.x1 <= source.x1
            and source.y0 <= target.y0
            and target.y1 <= source.y1
        )


@dataclasses.dataclass(frozen=True)
class CropPixel:
    """One source pixel segmented by a physical window.

    Attributes:
        row, col:
            Source row and column indices.
        value:
            Raw pixel value.  A scalar for single-sample pages; an array for
            multi-sample pages.
        bounds:
            Full physical bounds of the source pixel in the layer unit.
        covered:
            Part of the source pixel overlapped by the requested window, in
            the layer unit.

    """

    row: int
    col: int
    value: Any
    bounds: PhysicalWindow
    covered: PhysicalWindow

    @property
    def area(self) -> Fraction:
        """Covered area in square micrometers."""
        return self.covered.area

    @property
    def full_area(self) -> Fraction:
        """Full pixel area in square micrometers."""
        return self.bounds.area


@dataclasses.dataclass(frozen=True)
class CropPatch:
    """Calibrated crop of one pyramid level.

    Attributes:
        level:
            Pyramid level index.
        window:
            Requested physical window in its original unit.
        calibration:
            Calibration used to position the crop.
        row_start, col_start:
            Source indices of the top-left pixel in :py:attr:`array`.
        array:
            Raw pixel values of included source pixels.
        pixels:
            Included pixels in row-major order.  Each item retains source
            indices, raw value, full pixel bounds, and the covered part.
        covered:
            Physical region of the window covered by included pixels, or
            ``None`` when no source pixel intersects the window.
        missing:
            Parts of the window not covered by source pixels.
        outside_source:
            Whether the window extends outside the layer's source bounds.

    """

    level: int
    window: PhysicalWindow
    calibration: LayerCalibration
    row_start: int
    col_start: int
    array: NDArray[Any]
    pixels: tuple[CropPixel, ...]
    covered: PhysicalWindow | None
    missing: tuple[PhysicalWindow, ...] = ()
    outside_source: bool = False

    @property
    def shape(self) -> tuple[int, int]:
        """Shape of the source pixel array."""
        return (int(self.array.shape[0]), int(self.array.shape[1]))

    @property
    def row_stop(self) -> int:
        return self.row_start + self.shape[0]

    @property
    def col_stop(self) -> int:
        return self.col_start + self.shape[1]

    @property
    def source_shape(self) -> tuple[int, int]:
        return (self.calibration.height, self.calibration.width)

    @property
    def pixel_rows(self) -> range:
        return range(self.row_start, self.row_stop)

    @property
    def pixel_cols(self) -> range:
        return range(self.col_start, self.col_stop)

    def contains(self, row: int, col: int) -> bool:
        """Return whether absolute source pixel is in crop."""
        return (
            self.row_start <= row < self.row_stop
            and self.col_start <= col < self.col_stop
        )

    def pixel(self, row: int, col: int) -> CropPixel:
        """Return crop pixel for absolute source row and column."""
        if not self.contains(row, col):
            msg = f'pixel ({row}, {col}) not in crop'
            raise IndexError(msg)
        index = (row - self.row_start) * self.shape[1] + (
            col - self.col_start
        )
        return self.pixels[index]

    def value(self, row: int, col: int) -> Any:
        """Return raw value for absolute source row and column."""
        return self.pixel(row, col).value


def _subtract_rect(
    rect: PhysicalWindow, cut: PhysicalWindow
) -> list[PhysicalWindow]:
    rect = rect.to_micrometer()
    cut = cut.to_micrometer()
    x0 = max(rect.x0, cut.x0)
    x1 = min(rect.x1, cut.x1)
    y0 = max(rect.y0, cut.y0)
    y1 = min(rect.y1, cut.y1)
    if x1 <= x0 or y1 <= y0:
        return [rect]
    result: list[PhysicalWindow] = []
    if rect.x0 < x0:
        result.append(PhysicalWindow(rect.x0, x0, rect.y0, rect.y1, 'um'))
    if x1 < rect.x1:
        result.append(PhysicalWindow(x1, rect.x1, rect.y0, rect.y1, 'um'))
    if rect.y0 < y0:
        result.append(PhysicalWindow(x0, x1, rect.y0, y0, 'um'))
    if y1 < rect.y1:
        result.append(PhysicalWindow(x0, x1, y1, rect.y1, 'um'))
    return result


def subtract_windows(
    window: PhysicalWindow, cuts: Iterable[PhysicalWindow]
) -> list[PhysicalWindow]:
    """Return disjoint axis-aligned pieces of window not in cuts."""
    result = [window.to_micrometer()]
    for cut in cuts:
        next_result: list[PhysicalWindow] = []
        for item in result:
            next_result.extend(_subtract_rect(item, cut))
        result = next_result
        if not result:
            break
    return result


def _merge_rects(
    rects: Sequence[PhysicalWindow],
) -> list[PhysicalWindow]:
    """Return disjoint decomposition of the union of rectangles."""
    rects = [rect.to_micrometer() for rect in rects]
    rects = [rect for rect in rects if rect.width > 0 and rect.height > 0]
    if not rects:
        return []
    xs = sorted({rect.x0 for rect in rects} | {rect.x1 for rect in rects})
    ys = sorted({rect.y0 for rect in rects} | {rect.y1 for rect in rects})
    rows: list[list[PhysicalWindow]] = []
    for yindex in range(len(ys) - 1):
        y0 = ys[yindex]
        y1 = ys[yindex + 1]
        if y1 <= y0:
            continue
        mid = (y0 + y1) / 2
        spans: list[tuple[Fraction, Fraction]] = []
        for xindex in range(len(xs) - 1):
            x0 = xs[xindex]
            x1 = xs[xindex + 1]
            if x1 <= x0:
                continue
            mx = (x0 + x1) / 2
            if any(
                rect.x0 <= mx < rect.x1 and rect.y0 <= mid < rect.y1
                for rect in rects
            ):
                spans.append((x0, x1))
        merged: list[tuple[Fraction, Fraction]] = []
        for x0, x1 in spans:
            if merged and merged[-1][1] == x0:
                merged[-1] = (merged[-1][0], x1)
            else:
                merged.append((x0, x1))
        rows.append(
            [
                PhysicalWindow(x0, x1, y0, y1, 'um')
                for x0, x1 in merged
            ]
        )
    # merge vertically identical spans
    result: list[PhysicalWindow] = []
    active: dict[tuple[Fraction, Fraction], int] = {}
    active_y: dict[tuple[Fraction, Fraction], Fraction] = {}
    for yindex, row in enumerate(rows):
        y0 = ys[yindex]
        y1 = ys[yindex + 1]
        current = {(rect.x0, rect.x1) for rect in row}
        for span in list(active):
            if span not in current:
                result.append(
                    PhysicalWindow(
                        span[0],
                        span[1],
                        active_y.pop(span),
                        y0,
                        'um',
                    )
                )
                del active[span]
        for span in current:
            if span not in active:
                active[span] = yindex
                active_y[span] = y0
    for span in active:
        result.append(
            PhysicalWindow(
                span[0],
                span[1],
                active_y[span],
                ys[-1],
                'um',
            )
        )
    result.sort(key=lambda rect: (rect.y0, rect.x0))
    return result


def _crop_from_subset(
    subset: NDArray[Any],
    calibration: LayerCalibration,
    local_window: PhysicalWindow,
    rows: range,
    cols: range,
    *,
    level: int,
    values: NDArray[Any] | None = None,
) -> CropPatch:
    if values is None:
        values = subset
    pixels: list[CropPixel] = []
    for row_offset, row in enumerate(rows):
        for col_offset, col in enumerate(cols):
            bounds = calibration.pixel_bounds(row, col)
            covered = bounds.intersection(local_window)
            if covered is None:
                continue
            value = values[row_offset, col_offset]
            if isinstance(value, numpy.ndarray) and value.ndim == 0:
                value = value[()]
            pixels.append(
                CropPixel(
                    row=row,
                    col=col,
                    value=value,
                    bounds=bounds,
                    covered=covered.to_unit(calibration.unit),
                )
            )
    if not pixels:
        covered = None
        missing = (local_window,)
    else:
        x0 = min(pixel.bounds.x0 for pixel in pixels)
        x1 = max(pixel.bounds.x1 for pixel in pixels)
        y0 = min(pixel.bounds.y0 for pixel in pixels)
        y1 = max(pixel.bounds.y1 for pixel in pixels)
        covered = (
            PhysicalWindow(x0, x1, y0, y1, calibration.unit)
            .intersection(local_window)
        )
        assert covered is not None
        covered = covered.to_unit(local_window.unit)
        missing = tuple(subtract_windows(local_window, [covered]))
    return CropPatch(
        level=level,
        window=local_window,
        calibration=calibration,
        row_start=rows.start,
        col_start=cols.start,
        array=subset,
        pixels=tuple(pixels),
        covered=covered,
        missing=missing,
        outside_source=not calibration.covers(local_window),
    )


def crop_from_array(
    array: ArrayLike,
    calibration: LayerCalibration,
    window: PhysicalWindow | Sequence[Any] | Mapping[str, Any],
    *,
    level: int = 0,
) -> CropPatch:
    """Return calibrated physical crop of an in-memory pyramid level.

    Parameters:
        array:
            Array of shape ``(height, width)`` or
            ``(height, width, samples)`` matching ``calibration``.
        calibration:
            Physical calibration of the level.
        window:
            Requested physical window.  A bare sequence is interpreted as
            ``(x0, x1, y0, y1)`` and needs a window carrying ``unit``.
        level:
            Pyramid level index recorded in the patch.

    Returns:
        Crop containing only source pixels that overlap the window with
        positive area.  Pixels touching the window boundary only are
        excluded.

    """
    data = numpy.asarray(array)
    if data.ndim not in {2, 3}:
        msg = 'array must be 2D or 3D (height, width, samples)'
        raise ValueError(msg)
    if data.shape[0] != calibration.height or data.shape[1] != (
        calibration.width
    ):
        msg = (
            f'array shape {data.shape[:2]} does not match calibration '
            f'{(calibration.height, calibration.width)}'
        )
        raise ValueError(msg)
    local_window = _as_window(window)
    rows, cols = calibration.index_range(local_window)
    if len(rows) == 0 or len(cols) == 0:
        empty_shape = (0, 0) if data.ndim == 2 else (0, 0, data.shape[2])
        subset = numpy.empty(empty_shape, dtype=data.dtype)
    else:
        subset = data[rows.start : rows.stop, cols.start : cols.stop]
    return _crop_from_subset(
        subset, calibration, local_window, rows, cols, level=level
    )


def _mask_error() -> ValueError:
    return ValueError(
        'refusing to use a TIFF mask page as an image crop; '
        'same-sized masks are not image data'
    )


def _page_array(page: TiffPage) -> NDArray[Any]:
    if page.is_mask:
        raise _mask_error()
    data = page.asarray(squeeze=False)
    if page.planarconfig == 2 and page.samplesperpixel > 1:
        # shaped is (samples, depth, length, width, contig samples)
        if data.shape[1] != 1 or data.shape[4] != 1:
            msg = f'unexpected planar page array shape {data.shape}'
            raise ValueError(msg)
        return data[:, 0, :, :, 0]
    if data.shape[0] != 1:
        msg = f'unexpected page array shape {data.shape}'
        raise ValueError(msg)
    data = data[0]
    if data.shape[-1] == 1:
        data = data[..., 0]
    if data.ndim not in {2, 3}:
        msg = f'unsupported page array shape {data.shape}'
        raise ValueError(msg)
    return data


def _roi_array(
    page: TiffPage, rows: range, cols: range
) -> NDArray[Any]:
    """Decode only source segments intersecting the row/column ranges."""
    if page.is_mask:
        raise _mask_error()
    if (
        page.imagedepth != 1
        or page.planarconfig not in {1, 2}
        or page.samplesperpixel > 1
        and page.planarconfig == 2
    ):
        full = _page_array(page)
        if full.ndim == 3:
            # separate sample planes: crop the spatial axes only
            return full[
                ..., rows.start : rows.stop, cols.start : cols.stop
            ]
        return full[rows.start : rows.stop, cols.start : cols.stop]
    if page.is_tiled:
        if page.tiledepth not in {0, 1}:
            return _page_array(page)[
                rows.start : rows.stop, cols.start : cols.stop
            ]
        chunk_height = page.tilelength
        chunk_width = page.tilewidth
        ny = math.ceil(page.imagelength / chunk_height)
        nx = math.ceil(page.imagewidth / chunk_width)
    else:
        chunk_height = page.rowsperstrip
        chunk_width = page.imagewidth
        ny = math.ceil(page.imagelength / chunk_height)
        nx = 1
    if ny * nx != len(page.dataoffsets):
        return _page_array(page)[rows.start : rows.stop, cols.start : cols.stop]
    y_start = rows.start // chunk_height
    y_stop = (rows.stop - 1) // chunk_height + 1 if len(rows) else 0
    x_start = cols.start // chunk_width
    x_stop = (cols.stop - 1) // chunk_width + 1 if len(cols) else 0
    selected = [
        (yi, xi)
        for yi in range(max(0, y_start), min(ny, y_stop))
        for xi in range(max(0, x_start), min(nx, x_stop))
    ]
    samples = page.samplesperpixel if page.planarconfig == 1 else 1
    height = rows.stop - rows.start
    width = cols.stop - cols.start
    outshape: tuple[int, ...]
    if samples == 1:
        outshape = (height, width)
    else:
        outshape = (height, width, samples)
    assert page.dtype is not None
    output = numpy.full(
        outshape, page.fillvalue, dtype=page.dtype
    )
    decode = page.decode
    fh = page.parent.filehandle
    lock = fh.lock
    for yi, xi in selected:
        index = yi * nx + xi
        if index >= len(page.dataoffsets):
            continue
        offset = page.dataoffsets[index]
        bytecount = page.databytecounts[index]
        if offset <= 0 or bytecount <= 0:
            continue
        with lock:
            fh.seek(offset)
            encoded = fh.read(bytecount)
        kwargs: dict[str, Any] = {'_fullsize': True}
        if page.compression in {6, 7, 34892, 33007}:
            kwargs['jpegtables'] = page.jpegtables
            kwargs['jpegheader'] = page.jpegheader
        segment, (_s, _d, seg_y, seg_x, _c), shape = decode(
            encoded, index, **kwargs
        )
        if segment is None:
            continue
        if segment.ndim == 4:
            segment = segment[0]
        if segment.ndim == 3 and samples == 1:
            segment = segment[..., 0]
        segment_height = min(shape[1], page.imagelength - seg_y)
        segment_width = min(shape[2], page.imagewidth - seg_x)
        if segment_height <= 0 or segment_width <= 0:
            continue
        piece = segment[:segment_height, :segment_width]
        # intersect segment with requested ROI
        src_y0 = max(seg_y, rows.start)
        src_y1 = min(seg_y + segment_height, rows.stop)
        src_x0 = max(seg_x, cols.start)
        src_x1 = min(seg_x + segment_width, cols.stop)
        if src_y1 <= src_y0 or src_x1 <= src_x0:
            continue
        local_y0 = src_y0 - rows.start
        local_y1 = src_y1 - rows.start
        local_x0 = src_x0 - cols.start
        local_x1 = src_x1 - cols.start
        piece_y0 = src_y0 - seg_y
        piece_y1 = src_y1 - seg_y
        piece_x0 = src_x0 - seg_x
        piece_x1 = src_x1 - seg_x
        if samples == 1:
            output[local_y0:local_y1, local_x0:local_x1] = piece[
                piece_y0:piece_y1, piece_x0:piece_x1
            ]
        else:
            output[local_y0:local_y1, local_x0:local_x1, :] = piece[
                piece_y0:piece_y1, piece_x0:piece_x1, :
            ]
    return output


def crop_from_page(
    page: TiffPage,
    calibration: LayerCalibration,
    window: PhysicalWindow | Sequence[Any] | Mapping[str, Any],
    *,
    level: int = 0,
) -> CropPatch:
    """Return physical crop from a TIFF page.

    Only tiles or strips intersecting the crop are decoded.  Transparency
    mask pages (``NewSubfileType`` MASK) are rejected so that a same-sized
    mask cannot be substituted for image data.

    """
    if page.is_mask:
        raise _mask_error()
    local_window = _as_window(window)
    if page.imagelength != calibration.height or (
        page.imagewidth != calibration.width
    ):
        msg = (
            f'page shape {(page.imagelength, page.imagewidth)} does not '
            f'match calibration {(calibration.height, calibration.width)}'
        )
        raise ValueError(msg)
    rows, cols = calibration.index_range(local_window)
    planar_separate = page.planarconfig == 2 and page.samplesperpixel > 1
    if len(rows) and len(cols):
        subset = _roi_array(page, rows, cols)
    elif planar_separate:
        subset = numpy.empty(
            (page.samplesperpixel, 0, 0), dtype=page.dtype
        )
    else:
        shape = (0, 0) if page.samplesperpixel == 1 else (
            0,
            0,
            page.samplesperpixel,
        )
        subset = numpy.empty(shape, dtype=page.dtype)
    if planar_separate:
        # normalize to (length, width, samples)
        subset = numpy.moveaxis(subset, 0, -1)
    return _crop_from_subset(
        subset, calibration, local_window, rows, cols, level=level
    )


def crop_from_tiff(
    tif: TiffFile,
    level: int,
    window: PhysicalWindow | Sequence[Any] | Mapping[str, Any],
    calibration: LayerCalibration,
    *,
    series: int = 0,
) -> CropPatch:
    """Return physical crop from one level of a TIFF pyramid.

    Parameters:
        tif:
            Open :py:class:`tifffile.TiffFile`.
        level:
            Pyramid level index in ``series``.
        window:
            Requested physical window.
        calibration:
            Calibration of the requested level.
        series:
            Series index.  The default is the first series.

    Raises:
        ValueError:
            The selected level is a mask page or its array does not match
            the calibration.

    """
    page_series = tif.series[series]
    if page_series.keyframe.is_mask:
        raise _mask_error()
    levels = page_series.levels
    if not 0 <= level < len(levels):
        msg = f'level {level} out of bounds [0, {len(levels)})'
        raise IndexError(msg)
    level_series = levels[level]
    if level_series.keyframe.is_mask:
        raise _mask_error()
    if len(level_series) != 1:
        msg = f'pyramid level contains {len(level_series)} pages'
        raise ValueError(msg)
    page = level_series[0]
    if page is None:
        msg = 'pyramid level page is missing'
        raise ValueError(msg)
    return crop_from_page(page, calibration, window, level=level)


@dataclasses.dataclass(frozen=True)
class PixelContribution:
    """Disjoint source pixel contribution in a query.

    Attributes:
        level:
            Pyramid level index.
        source_row, source_col:
            Absolute source pixel indices.
        value:
            Raw pixel value.
        bounds:
            Full physical bounds of the source pixel in the layer unit.
        covered:
            Part of the pixel covered by the query window, in micrometers.
        contributors:
            Work orders and their published revisions overlapping this
            contribution, sorted by work-order name.
        revision:
            Revision of the first contributor.

    """

    level: int
    source_row: int
    source_col: int
    value: Any
    bounds: PhysicalWindow
    covered: PhysicalWindow
    contributors: tuple[tuple[str, int], ...]

    @property
    def row(self) -> int:
        return self.source_row

    @property
    def col(self) -> int:
        return self.source_col

    @property
    def owners(self) -> tuple[str, ...]:
        return tuple(name for name, _revision in self.contributors)

    @property
    def work_order(self) -> str:
        return self.contributors[0][0]

    @property
    def revision(self) -> int:
        return self.contributors[0][1]

    @property
    def area(self) -> Fraction:
        """Covered area in square micrometers."""
        return self.covered.area


@dataclasses.dataclass(frozen=True)
class LayerQueryResult:
    """Query result for one pyramid level.

    Attributes:
        level:
            Pyramid level index.
        window:
            Requested physical window.
        locatable:
            Whether the level has calibration.  An uncalibrated level is
            explicitly reported as unlocatable and contains no pixels.
        pixels:
            Disjoint pixel contributions.  Overlapping work orders share a
            contribution, so their overlap is counted once.
        requested_area:
            Area of the requested window in square micrometers.
        covered_area:
            Union of the requested window covered by source pixels, in
            square micrometers.
        missing:
            Parts of the requested window not covered by source pixels.
        outside_source:
            Whether part of the requested window lies outside source bounds.
        beyond_source:
            Whether the requested window has no positive-area intersection
            with source bounds.

    """

    level: int
    window: PhysicalWindow
    locatable: bool
    pixels: tuple[PixelContribution, ...]
    requested_area: Fraction
    covered_area: Fraction
    missing: tuple[PhysicalWindow, ...]
    outside_source: bool
    beyond_source: bool

    @property
    def count(self) -> int:
        return len(self.pixels)

    @property
    def complete(self) -> bool:
        return self.locatable and self.covered_area == self.requested_area

    @property
    def status(self) -> str:
        """Human-readable location status of this level result."""
        if not self.locatable:
            return 'unlocatable'
        if self.beyond_source:
            return 'beyond-source'
        if self.complete:
            return 'complete'
        return 'partial'

    @property
    def source_keys(self) -> list[tuple[int, int]]:
        """Unique source row/column keys in row-major order."""
        return sorted(
            {(pixel.source_row, pixel.source_col) for pixel in self.pixels}
        )

    @property
    def contributions(self) -> tuple[PixelContribution, ...]:
        """Disjoint contribution segments, including overlap splits."""
        return self.pixels

    @property
    def values(self) -> list[Any]:
        """Raw values, one per unique source pixel in row-major order."""
        seen: dict[tuple[int, int], Any] = {}
        for pixel in self.pixels:
            seen.setdefault(
                (pixel.source_row, pixel.source_col), pixel.value
            )
        return [seen[key] for key in sorted(seen)]

    def value(self, row: int, col: int) -> Any:
        """Return raw value of source pixel."""
        for pixel in self.pixels:
            if pixel.source_row == row and pixel.source_col == col:
                return pixel.value
        msg = f'pixel ({row}, {col}) not in query result'
        raise IndexError(msg)

    def owners_at(self, row: int, col: int) -> tuple[str, ...]:
        """Return sorted work orders covering source pixel."""
        owners: set[str] = set()
        for pixel in self.pixels:
            if pixel.source_row == row and pixel.source_col == col:
                owners.update(pixel.owners)
        return tuple(sorted(owners))

    def contribution(
        self, row: int, col: int
    ) -> list[PixelContribution]:
        """Return contributions for source pixel."""
        return [
            pixel
            for pixel in self.pixels
            if pixel.source_row == row and pixel.source_col == col
        ]

    def sources(self) -> dict[tuple[int, int], PhysicalWindow]:
        """Map source pixel to its covered region."""
        result: dict[tuple[int, int], PhysicalWindow] = {}
        for pixel in self.pixels:
            key = (pixel.source_row, pixel.source_col)
            existing = result.get(key)
            if existing is None:
                result[key] = pixel.covered
            else:
                result[key] = PhysicalWindow(
                    min(existing.x0, pixel.covered.x0),
                    max(existing.x1, pixel.covered.x1),
                    min(existing.y0, pixel.covered.y0),
                    max(existing.y1, pixel.covered.y1),
                    'um',
                )
        return result


@dataclasses.dataclass(frozen=True)
class DisplayPartition:
    """Rectangle loaded by the review client."""

    x0: Any
    x1: Any
    y0: Any
    y1: Any
    unit: str = 'um'

    def __post_init__(self) -> None:
        for name in ('x0', 'x1', 'y0', 'y1'):
            object.__setattr__(
                self, name, _fraction(getattr(self, name), name)
            )
        _unit_factor(self.unit)
        if self.x1 <= self.x0 or self.y1 <= self.y0:
            msg = 'partition must have positive area'
            raise ValueError(msg)

    @property
    def window(self) -> PhysicalWindow:
        return PhysicalWindow(self.x0, self.x1, self.y0, self.y1, self.unit)

    @property
    def key(self) -> tuple[Fraction, Fraction, Fraction, Fraction, str]:
        return (self.x0, self.x1, self.y0, self.y1, self.unit)


class PartitionMosaic:
    """Mosaic of one display partition for all levels of a preview."""

    def __init__(
        self,
        window: PhysicalWindow,
        levels: Mapping[int, LayerQueryResult],
    ) -> None:
        self.window = window
        self.levels = dict(levels)

    def level(self, level: int) -> LayerQueryResult:
        """Return result for level."""
        return self.levels[level]

    def __getitem__(self, level: int) -> LayerQueryResult:
        return self.levels[level]

    def __iter__(self) -> Iterator[int]:
        return iter(sorted(self.levels))

    def __len__(self) -> int:
        return len(self.levels)

    @property
    def requested_area(self) -> Fraction:
        return self.window.area

    @property
    def covered_area(self) -> Fraction:
        """Largest covered area among levels in square micrometers."""
        return max(
            (result.covered_area for result in self.levels.values()),
            default=Fraction(0),
        )

    @property
    def covered_areas(self) -> dict[int, Fraction]:
        """Covered area per level in square micrometers."""
        return {
            level: result.covered_area
            for level, result in self.levels.items()
        }

    @property
    def status(self) -> dict[int, str]:
        """Location status per level."""
        return {
            level: result.status for level, result in self.levels.items()
        }

    def values(self, level: int) -> list[Any]:
        """Return raw values for level in pixel order."""
        return self.levels[level].values

    def source_keys(self, level: int) -> list[tuple[int, int]]:
        """Return source row/column keys for level."""
        return self.levels[level].source_keys

    def owners(self, level: int) -> dict[str, Fraction]:
        """Return union area attributed to each work order."""
        areas: dict[str, Fraction] = {}
        for pixel in self.levels[level].pixels:
            for owner in pixel.owners:
                areas[owner] = areas.get(owner, Fraction(0)) + pixel.area
        return areas


def _merge_contributions(
    contributions: Sequence[PixelContribution],
) -> list[PixelContribution]:
    grouped: dict[
        tuple[int, int, int, tuple[tuple[str, int], ...]],
        list[PhysicalWindow],
    ] = {}
    templates: dict[
        tuple[int, int, int, tuple[tuple[str, int], ...]],
        PixelContribution,
    ] = {}
    for item in contributions:
        key = (
            item.level,
            item.source_row,
            item.source_col,
            item.contributors,
        )
        grouped.setdefault(key, []).append(item.covered)
        templates[key] = item
    result: list[PixelContribution] = []
    for key in sorted(grouped, key=lambda item: (item[0], item[1], item[2])):
        template = templates[key]
        for rect in _merge_rects(grouped[key]):
            result.append(
                dataclasses.replace(template, covered=rect)
            )
    return result


def assemble(*mosaics: PartitionMosaic) -> PartitionMosaic:
    """Assemble partition mosaics into one mosaic.

    Contributions of the same source pixel and same contributors are united,
    so a source pixel crossing a display partition boundary is counted only
    once.  Merged coverage and values match a direct query over the union of
    the partitions.

    """
    if not mosaics:
        msg = 'at least one mosaic is required'
        raise ValueError(msg)
    if len(mosaics) == 1:
        return mosaics[0]
    region = _merge_rects([mosaic.window for mosaic in mosaics])
    if not region:
        msg = 'partitions do not cover any area'
        raise ValueError(msg)
    xs = [rect.x0 for rect in region]
    ys = [rect.y0 for rect in region]
    xe = [rect.x1 for rect in region]
    ye = [rect.y1 for rect in region]
    window = PhysicalWindow(
        min(xs), max(xe), min(ys), max(ye), 'um'
    )
    requested = sum(
        (
            rect.area
            for rect in _merge_rects(
                [mosaic.window for mosaic in mosaics]
            )
        ),
        Fraction(0),
    )
    levels: dict[int, LayerQueryResult] = {}
    all_levels = sorted(
        {level for mosaic in mosaics for level in mosaic.levels}
    )
    for level in all_levels:
        entries: list[PixelContribution] = []
        locatable = False
        outside = False
        beyond = True
        missing: list[PhysicalWindow] = []
        present = False
        for mosaic in mosaics:
            result = mosaic.levels.get(level)
            if result is None:
                continue
            present = True
            locatable = locatable or result.locatable
            outside = outside or result.outside_source
            beyond = beyond and result.beyond_source
            missing.extend(result.missing)
            entries.extend(result.pixels)
        if not present:
            continue
        merged = _merge_contributions(entries)
        covered = sum(
            (pixel.area for pixel in merged), Fraction(0)
        )
        levels[level] = LayerQueryResult(
            level=level,
            window=window,
            locatable=locatable,
            pixels=tuple(merged),
            requested_area=requested,
            covered_area=covered,
            missing=tuple(missing),
            outside_source=outside,
            beyond_source=beyond,
        )
    return PartitionMosaic(window, levels)


class _SpatialIndex:
    """Persistent uniform grid index of published work-order regions.

    Indexes form a copy-on-write chain.  A correction records changed cells
    only; cells outside the corrected work order's old and new regions are
    shared with the previous index.

    """

    def __init__(
        self,
        cell: Fraction,
        parent: _SpatialIndex | None = None,
        changes: Mapping[
            tuple[int, int], frozenset[str]
        ]
        | None = None,
    ) -> None:
        self.cell = cell
        self._parent = parent
        self._changes = dict(changes or {})

    def _keys(self, rect: PhysicalWindow) -> set[tuple[int, int]]:
        rect = rect.to_micrometer()
        if rect.x1 <= rect.x0 or rect.y1 <= rect.y0:
            return set()
        x0 = _floor(rect.x0 / self.cell)
        x1 = _ceil(rect.x1 / self.cell) - 1
        y0 = _floor(rect.y0 / self.cell)
        y1 = _ceil(rect.y1 / self.cell) - 1
        return {
            (x, y)
            for x in range(x0, x1 + 1)
            for y in range(y0, y1 + 1)
        }

    def _names(self, key: tuple[int, int]) -> frozenset[str]:
        if key in self._changes:
            return self._changes[key]
        if self._parent is not None:
            return self._parent._names(key)
        return frozenset()

    def query(self, rect: PhysicalWindow) -> set[str]:
        result: set[str] = set()
        for key in self._keys(rect):
            result.update(self._names(key))
        return result

    def updated(
        self,
        removals: Iterable[tuple[str, PhysicalWindow]],
        additions: Iterable[tuple[str, PhysicalWindow]],
    ) -> _SpatialIndex:
        """Return new index with removals and additions applied."""
        removed: dict[tuple[int, int], set[str]] = {}
        added: dict[tuple[int, int], set[str]] = {}
        for name, rect in removals:
            for key in self._keys(rect):
                removed.setdefault(key, set()).add(name)
        for name, rect in additions:
            for key in self._keys(rect):
                added.setdefault(key, set()).add(name)
        changes: dict[tuple[int, int], frozenset[str]] = {}
        for key in removed.keys() | added.keys():
            names = set(self._names(key))
            names -= removed.get(key, set())
            names |= added.get(key, set())
            changes[key] = frozenset(names)
        return _SpatialIndex(self.cell, self, changes)

    def __len__(self) -> int:
        keys = set(self._changes)
        if self._parent is not None:
            keys.update(self._parent._keys_with_names())
        return len(keys)

    def _keys_with_names(self) -> set[tuple[int, int]]:
        keys = set(self._changes)
        if self._parent is not None:
            keys.update(self._parent._keys_with_names())
        return keys


@dataclasses.dataclass(frozen=True)
class _Published:
    """Frozen published crops of one work order."""

    name: str
    revision: int
    crops: Mapping[int, CropPatch]

    def regions(self) -> list[PhysicalWindow]:
        return [
            crop.covered
            for crop in self.crops.values()
            if crop.covered is not None
        ]


class _State:
    """Immutable publication state with a partition cache."""

    def __init__(
        self,
        revision: int,
        published: Mapping[str, _Published],
        calibrations: Mapping[int, LayerCalibration | None],
        cell: Fraction,
        cache: Mapping[
            tuple[Fraction, Fraction, Fraction, Fraction, str],
            PartitionMosaic,
        ]
        | None = None,
        indexes: Mapping[int, _SpatialIndex] | None = None,
    ) -> None:
        self.revision = revision
        self.published = dict(published)
        self.calibrations = dict(calibrations)
        self.levels = tuple(sorted(self.calibrations))
        self._cache = dict(cache or {})
        self._lock = threading.RLock()
        if indexes is None:
            self._indexes: dict[int, _SpatialIndex] = {
                level: _SpatialIndex(cell) for level in self.levels
            }
            for item in self.published.values():
                for level, crop in item.crops.items():
                    if level in self._indexes and crop.covered is not None:
                        index = self._indexes[level]
                        self._indexes[level] = index.updated(
                            (), ((item.name, crop.covered),)
                        )
        else:
            self._indexes = dict(indexes)

    def index(self, level: int) -> _SpatialIndex:
        """Return spatial index of level."""
        return self._indexes[level]

    @property
    def work_order_revisions(self) -> dict[str, int]:
        return {
            name: item.revision for name, item in self.published.items()
        }

    def cached(self) -> dict[
        tuple[Fraction, Fraction, Fraction, Fraction, str],
        PartitionMosaic,
    ]:
        with self._lock:
            return dict(self._cache)

    def partition(
        self, partition: DisplayPartition | PhysicalWindow
    ) -> PartitionMosaic:
        if isinstance(partition, DisplayPartition):
            window = partition.window
        else:
            window = partition
        key = (
            window.x0,
            window.x1,
            window.y0,
            window.y1,
            window.unit,
        )
        with self._lock:
            cached = self._cache.get(key)
            if cached is not None:
                return cached
            result = self.query(window)
            self._cache[key] = result
            return result

    def query(
        self,
        window: PhysicalWindow | Sequence[Any] | Mapping[str, Any],
        *,
        level: int | None = None,
    ) -> PartitionMosaic:
        target = _as_window(window)
        levels = (level,) if level is not None else self.levels
        results: dict[int, LayerQueryResult] = {}
        for current in levels:
            if current not in self.calibrations:
                msg = f'level {current} not configured'
                raise KeyError(msg)
            results[current] = self._query_level(current, target)
        return PartitionMosaic(target, results)

    def _query_level(
        self, level: int, window: PhysicalWindow
    ) -> LayerQueryResult:
        calibration = self.calibrations.get(level)
        target = window.to_micrometer()
        requested = target.area
        if calibration is None:
            return LayerQueryResult(
                level=level,
                window=window,
                locatable=False,
                pixels=(),
                requested_area=requested,
                covered_area=Fraction(0),
                missing=(window,),
                outside_source=True,
                beyond_source=False,
            )
        source = calibration.source_bounds().to_micrometer()
        intersection = PhysicalWindow(
            source.x0, source.x1, source.y0, source.y1, 'um'
        ).intersection(target)
        if intersection is None:
            return LayerQueryResult(
                level=level,
                window=window,
                locatable=True,
                pixels=(),
                requested_area=requested,
                covered_area=Fraction(0),
                missing=(window,),
                outside_source=True,
                beyond_source=True,
            )
        candidates = self._indexes[level].query(window)
        rows, cols = calibration.index_range(window)
        entries: dict[
            tuple[int, int],
            list[tuple[PhysicalWindow, str, int, Any, PhysicalWindow]],
        ] = {}
        for name in candidates:
            item = self.published[name]
            crop = item.crops.get(level)
            if crop is None or crop.shape == (0, 0):
                continue
            row_start = max(rows.start, crop.row_start)
            row_stop = min(rows.stop, crop.row_stop)
            col_start = max(cols.start, crop.col_start)
            col_stop = min(cols.stop, crop.col_stop)
            if row_stop <= row_start or col_stop <= col_start:
                continue
            for row in range(row_start, row_stop):
                for col in range(col_start, col_stop):
                    pixel = crop.pixel(row, col)
                    covered = pixel.covered.to_micrometer().intersection(
                        target
                    )
                    if covered is None:
                        continue
                    entries.setdefault((row, col), []).append(
                        (
                            covered,
                            name,
                            item.revision,
                            pixel.value,
                            pixel.bounds.to_unit(calibration.unit),
                        )
                    )
        contributions: list[PixelContribution] = []
        for (row, col), pixel_entries in sorted(entries.items()):
            for covered, contributors in _segment_pixel(pixel_entries):
                primary = contributors[0][0]
                selected = next(
                    entry
                    for entry in pixel_entries
                    if entry[1] == primary
                )
                contributions.append(
                    PixelContribution(
                        level=level,
                        source_row=row,
                        source_col=col,
                        value=selected[3],
                        bounds=selected[4],
                        covered=covered,
                        contributors=contributors,
                    )
                )
        contributions = _coalesce(contributions)
        covered_area = sum(
            (pixel.area for pixel in contributions), Fraction(0)
        )
        missing = subtract_windows(
            target, [pixel.covered for pixel in contributions]
        )
        return LayerQueryResult(
            level=level,
            window=window,
            locatable=True,
            pixels=tuple(contributions),
            requested_area=requested,
            covered_area=covered_area,
            missing=tuple(missing),
            outside_source=not calibration.covers(window),
            beyond_source=False,
        )


def _segment_pixel(
    entries: Sequence[
        tuple[PhysicalWindow, str, int, Any, PhysicalWindow]
    ],
) -> list[tuple[PhysicalWindow, tuple[tuple[str, int], ...]]]:
    """Split one source pixel into disjoint rectangles with contributors."""
    xs = sorted({rect.x0 for rect, *_ in entries} | {rect.x1 for rect, *_ in entries})
    ys = sorted({rect.y0 for rect, *_ in entries} | {rect.y1 for rect, *_ in entries})
    raw: list[tuple[PhysicalWindow, tuple[tuple[str, int], ...]]] = []
    for yindex in range(len(ys) - 1):
        y0 = ys[yindex]
        y1 = ys[yindex + 1]
        if y1 <= y0:
            continue
        my = (y0 + y1) / 2
        for xindex in range(len(xs) - 1):
            x0 = xs[xindex]
            x1 = xs[xindex + 1]
            if x1 <= x0:
                continue
            mx = (x0 + x1) / 2
            contributors: dict[str, int] = {}
            for rect, name, revision, _value, _bounds in entries:
                if (
                    rect.x0 <= mx < rect.x1
                    and rect.y0 <= my < rect.y1
                ):
                    contributors.setdefault(name, revision)
            if not contributors:
                continue
            raw.append(
                (
                    PhysicalWindow(x0, x1, y0, y1, 'um'),
                    tuple(sorted(contributors.items())),
                )
            )
    return _merge_segments(raw)


def _merge_segments(
    raw: Sequence[tuple[PhysicalWindow, tuple[tuple[str, int], ...]]],
) -> list[tuple[PhysicalWindow, tuple[tuple[str, int], ...]]]:
    merged: list[tuple[PhysicalWindow, tuple[tuple[str, int], ...]]] = []
    for rect, contributors in raw:
        combined = False
        for index, (existing, existing_contributors) in enumerate(merged):
            if existing_contributors != contributors:
                continue
            if (
                existing.x0 == rect.x0
                and existing.x1 == rect.x1
                and existing.y1 == rect.y0
            ):
                merged[index] = (
                    PhysicalWindow(
                        existing.x0,
                        existing.x1,
                        existing.y0,
                        rect.y1,
                        'um',
                    ),
                    contributors,
                )
                combined = True
                break
            if (
                existing.y0 == rect.y0
                and existing.y1 == rect.y1
                and existing.x1 == rect.x0
            ):
                merged[index] = (
                    PhysicalWindow(
                        existing.x0,
                        rect.x1,
                        existing.y0,
                        existing.y1,
                        'um',
                    ),
                    contributors,
                )
                combined = True
                break
        if not combined:
            merged.append((rect, contributors))
    return merged


def _coalesce(
    contributions: Sequence[PixelContribution],
) -> list[PixelContribution]:
    result: list[PixelContribution] = []
    for item in contributions:
        if result:
            previous = result[-1]
            if (
                previous.level == item.level
                and previous.source_row == item.source_row
                and previous.source_col == item.source_col
                and previous.contributors == item.contributors
                and previous.covered.x1 == item.covered.x0
                and previous.covered.y0 == item.covered.y0
                and previous.covered.y1 == item.covered.y1
            ):
                result[-1] = dataclasses.replace(
                    previous,
                    covered=PhysicalWindow(
                        previous.covered.x0,
                        item.covered.x1,
                        previous.covered.y0,
                        previous.covered.y1,
                        'um',
                    ),
                )
                continue
        result.append(item)
    return result


@dataclasses.dataclass
class _WorkOrder:
    name: str
    latest: int | None = None
    pending: dict[int, CropPatch | None] = dataclasses.field(
        default_factory=dict
    )  # keyed by pyramid level for the latest revision
    published_revision: int | None = None
    published: dict[int, CropPatch] = dataclasses.field(default_factory=dict)


class ReviewMosaic:
    """Revision-safe review mosaic assembled from delivered crops.

    Parameters:
        levels:
            Calibration of each pyramid level.  A value of ``None`` marks a
            level without calibration; queries report that level as
            unlocatable while other levels continue to work.
        cell:
            Grid cell size in micrometers for the partition index.

    """

    def __init__(
        self,
        levels: Mapping[int, LayerCalibration | None],
        *,
        cell: Any = 256,
    ) -> None:
        if not levels:
            msg = 'at least one level is required'
            raise ValueError(msg)
        self._calibrations = dict(levels)
        self._cell = _fraction(cell, 'cell')
        if self._cell <= 0:
            msg = 'cell must be positive'
            raise ValueError(msg)
        self._lock = threading.RLock()
        self._workorders: dict[str, _WorkOrder] = {}
        self._revision = 0
        self._state = _State(0, {}, self._calibrations, self._cell)

    @property
    def levels(self) -> tuple[int, ...]:
        return tuple(sorted(self._calibrations))

    @property
    def revision(self) -> int:
        """Monotonic publication revision."""
        return self._revision

    def calibration(self, level: int) -> LayerCalibration | None:
        """Return calibration of level."""
        try:
            return self._calibrations[level]
        except KeyError:
            msg = f'level {level} not configured'
            raise KeyError(msg) from None

    def set_calibration(
        self, level: int, calibration: LayerCalibration | None
    ) -> None:
        """Set or clear calibration of a configured level.

        Existing deliveries of the level are no longer positionable and are
        withdrawn from the published state until they are delivered again.

        """
        with self._lock:
            if level not in self._calibrations:
                msg = f'level {level} not configured'
                raise KeyError(msg)
            if self._calibrations[level] == calibration:
                return
            self._calibrations[level] = calibration
            removed: dict[int, list[PhysicalWindow]] = {level: []}
            for state in self._workorders.values():
                crop = state.published.pop(level, None)
                state.pending.pop(level, None)
                if crop is not None and crop.covered is not None:
                    removed[level].append(crop.covered)
            self._rebuild(removed, {}, '')

    def deliver(
        self,
        work_order: str,
        revision: int,
        level: int,
        crop: CropPatch | None,
    ) -> bool:
        """Deliver one level crop of a work-order revision.

        Parameters:
            work_order:
                Work-order identity.
            revision:
                Monotonic revision number.
            level:
                Pyramid level index.
            crop:
                Delivered crop.  Must be ``None`` for an uncalibrated level.

        Returns:
            ``True`` if this delivery published the revision, otherwise
            ``False`` when the crop is stored pending or ignored.

        Delivery is idempotent.  Re-delivering an old revision, or a level
        already delivered for the current revision, is ignored and leaves
        the published result unchanged.

        """
        if level not in self._calibrations:
            msg = f'level {level} not configured'
            raise KeyError(msg)
        revision = _fraction(revision, 'revision')
        if revision.denominator != 1 or revision < 1:
            msg = 'revision must be a positive integer'
            raise ValueError(msg)
        revision = int(revision)
        calibration = self._calibrations[level]
        with self._lock:
            state = self._workorders.get(work_order)
            if state is None:
                state = _WorkOrder(work_order)
                self._workorders[work_order] = state
            if state.latest is not None and revision < state.latest:
                return False
            if state.latest is None or revision > state.latest:
                state.latest = revision
                state.pending = {}
            if level in state.pending:
                return False
            if calibration is None:
                if crop is not None:
                    msg = (
                        f'level {level} has no calibration; deliver None '
                        'to mark it unlocatable'
                    )
                    raise UnlocatableLevelError(msg)
                state.pending[level] = None
            else:
                if crop is None:
                    msg = f'level {level} requires a calibrated crop'
                    raise ValueError(msg)
                self._validate_crop(level, crop)
                self._validate_window(state, crop)
                state.pending[level] = crop
            if not self._complete(state):
                return False
            self._publish(state, revision)
            return True

    submit = deliver

    def deliver_array(
        self,
        work_order: str,
        revision: int,
        level: int,
        window: PhysicalWindow | Sequence[Any] | Mapping[str, Any],
        array: ArrayLike,
    ) -> bool:
        """Build and deliver a crop from an array."""
        calibration = self._calibrations[level]
        if calibration is None:
            msg = f'level {level} has no calibration'
            raise UnlocatableLevelError(msg)
        crop = crop_from_array(array, calibration, window, level=level)
        return self.deliver(work_order, revision, level, crop)

    def _validate_crop(self, level: int, crop: CropPatch) -> None:
        expected = self._calibrations[level]
        if expected is None:
            msg = f'level {level} has no calibration'
            raise UnlocatableLevelError(msg)
        if crop.level != level:
            msg = f'crop is for level {crop.level}, not {level}'
            raise ValueError(msg)
        if crop.calibration.to_unit(expected.unit) != expected:
            msg = (
                f'crop calibration for level {level} does not match the '
                'mosaic calibration; one mosaic uses one scan, pyramid '
                'and calibration'
            )
            raise ValueError(msg)

    def _complete(self, state: _WorkOrder) -> bool:
        return all(
            level in state.pending or calibration is None
            for level, calibration in self._calibrations.items()
        )

    def _validate_window(self, state: _WorkOrder, crop: CropPatch) -> None:
        window = crop.window.to_micrometer()
        for pending in state.pending.values():
            if pending is None:
                continue
            other = pending.window.to_micrometer()
            if (
                other.x0,
                other.x1,
                other.y0,
                other.y1,
            ) != (
                window.x0,
                window.x1,
                window.y0,
                window.y1,
            ):
                msg = (
                    'all levels of a revision must cover the same physical '
                    'window'
                )
                raise ValueError(msg)

    def _publish(self, state: _WorkOrder, revision: int) -> None:
        crops: dict[int, CropPatch] = {}
        for level, calibration in self._calibrations.items():
            if calibration is None:
                continue
            crop = state.pending.get(level)
            if crop is not None:
                crops[level] = crop
        removed: dict[int, list[PhysicalWindow]] = {}
        added: dict[int, list[PhysicalWindow]] = {}
        for level in set(state.published) | set(crops):
            old = state.published.get(level)
            new = crops.get(level)
            if old is not None and old.covered is not None:
                removed.setdefault(level, []).append(old.covered)
            if new is not None and new.covered is not None:
                added.setdefault(level, []).append(new.covered)
        state.published_revision = revision
        state.published = crops
        self._rebuild(removed, added, state.name)

    def _published_map(self) -> dict[str, _Published]:
        return {
            name: _Published(
                name,
                state.published_revision,
                dict(state.published),
            )
            for name, state in self._workorders.items()
            if state.published_revision is not None
        }

    def _rebuild(
        self,
        removed: Mapping[int, Sequence[PhysicalWindow]],
        added: Mapping[int, Sequence[PhysicalWindow]],
        name: str,
    ) -> None:
        cache: dict[
            tuple[Fraction, Fraction, Fraction, Fraction, str],
            PartitionMosaic,
        ] = {}
        changed = [
            window
            for group in (removed, added)
            for windows in group.values()
            for window in windows
        ]
        for key, mosaic in self._state.cached().items():
            window = PhysicalWindow(
                key[0], key[1], key[2], key[3], key[4]
            )
            if all(not window.intersects(item) for item in changed):
                cache[key] = mosaic
        indexes = dict(self._state._indexes)
        for level in set(removed) | set(added):
            index = indexes[level]
            indexes[level] = index.updated(
                ((name, window) for window in removed.get(level, ())),
                ((name, window) for window in added.get(level, ())),
            )
        self._revision += 1
        self._state = _State(
            self._revision,
            self._published_map(),
            self._calibrations,
            self._cell,
            cache,
            indexes,
        )

    def preview(self) -> MosaicPreview:
        """Open an immutable preview of the current publication."""
        with self._lock:
            return MosaicPreview(self._state)

    def partition(
        self, partition: DisplayPartition | PhysicalWindow
    ) -> PartitionMosaic:
        """Load one display partition from the latest publication."""
        with self._lock:
            state = self._state
        return state.partition(partition)

    def query(
        self,
        window: PhysicalWindow | Sequence[Any] | Mapping[str, Any],
        *,
        level: int | None = None,
    ) -> PartitionMosaic:
        """Query an arbitrary physical window from the latest publication."""
        with self._lock:
            return self._state.query(window, level=level)

    def published_revisions(self) -> dict[str, int]:
        """Map published work orders to their revisions."""
        with self._lock:
            return self._state.work_order_revisions

    def withdraw(self, work_order: str) -> bool:
        """Remove a work order from the published mosaic.

        Other work orders keep their coverage.  Cached partitions outside
        the withdrawn work order's regions remain valid.

        Returns:
            ``True`` if a published work order was withdrawn.

        """
        with self._lock:
            state = self._workorders.get(work_order)
            if state is None:
                return False
            removed = {
                level: [crop.covered]
                for level, crop in state.published.items()
                if crop.covered is not None
            }
            del self._workorders[work_order]
            if state.published_revision is None:
                return False
            self._rebuild(removed, {}, work_order)
            return True

    def delivered_levels(
        self, work_order: str
    ) -> dict[int, set[int]]:
        """Map revision to delivered level indices."""
        with self._lock:
            state = self._workorders.get(work_order)
            if state is None or state.latest is None:
                return {}
            level_set = {
                level
                for level in self._calibrations
                if level in state.pending
            }
            result = {state.latest: level_set}
            if (
                state.published_revision is not None
                and state.published_revision != state.latest
            ):
                result[state.published_revision] = set(state.published)
            return result

    def cached_partitions(self) -> list[
        tuple[Fraction, Fraction, Fraction, Fraction, str]
    ]:
        """Return keys of partitions cached by the latest publication."""
        with self._lock:
            return sorted(self._state.cached())


class MosaicPreview:
    """Immutable view of a publication state.

    A preview keeps reading the state that was current when it was opened,
    even after later revisions are published.  Queries do not read the
    original TIFF.

    """

    def __init__(self, state: _State) -> None:
        self._state = state

    @property
    def revision(self) -> int:
        return self._state.revision

    @property
    def work_orders(self) -> dict[str, int]:
        return self._state.work_order_revisions

    def partition(
        self, partition: DisplayPartition | PhysicalWindow
    ) -> PartitionMosaic:
        """Load one display partition."""
        return self._state.partition(partition)

    def query(
        self,
        window: PhysicalWindow | Sequence[Any] | Mapping[str, Any],
        *,
        level: int | None = None,
    ) -> PartitionMosaic:
        """Query an arbitrary physical window."""
        return self._state.query(window, level=level)

    def cached_partitions(self) -> list[
        tuple[Fraction, Fraction, Fraction, Fraction, str]
    ]:
        """Return keys of partitions cached by this preview."""
        return sorted(self._state.cached())
