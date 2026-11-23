# test_review.py

"""Unittests for physical pyramid review mosaics."""

from __future__ import annotations

from fractions import Fraction

import numpy
import pytest
from numpy.testing import assert_array_equal

import tifffile
from tifffile import FILETYPE
from tifffile.review import (
    DisplayPartition,
    LayerCalibration,
    PhysicalWindow,
    ReviewMosaic,
    UnlocatableLevelError,
    assemble,
    crop_from_array,
    crop_from_tiff,
)


FINE = LayerCalibration(8, 8, 100, 200, 2, 1, 'um')
COARSE = LayerCalibration(3, 3, 101000, 200000, 4000, 2500, 'nm')
FINE_DATA = numpy.arange(64, dtype=numpy.uint16).reshape(8, 8) + 1000
COARSE_DATA = numpy.arange(9, dtype=numpy.uint16).reshape(3, 3) + 2000


def _crop(level: int, window: PhysicalWindow):
    if level == 0:
        return crop_from_array(FINE_DATA, FINE, window, level=0)
    return crop_from_array(COARSE_DATA, COARSE, window, level=1)


def _publish(
    mosaic: ReviewMosaic,
    name: str,
    revision: int,
    window: PhysicalWindow,
) -> None:
    for level in mosaic.levels:
        crop = None if mosaic.calibration(level) is None else _crop(
            level, window
        )
        mosaic.deliver(name, revision, level, crop)


def _window(x0, x1, y0=202, y1=207, unit='um') -> PhysicalWindow:
    return PhysicalWindow(x0, x1, y0, y1, unit)


def test_crop_includes_only_positive_area_overlap() -> None:
    window = _window(102, 107)
    fine = crop_from_array(FINE_DATA, FINE, window, level=0)
    # x: [102, 106) full pixels 1..2, [106, 108) pixel 3 partial to 107
    # y: [202, 207) full rows 2..6
    assert (fine.row_start, fine.row_stop) == (2, 7)
    assert (fine.col_start, fine.col_stop) == (1, 4)
    assert fine.shape == (5, 3)
    assert_array_equal(fine.array, FINE_DATA[2:7, 1:4])

    cases = {
        (2, 1): (Fraction(2), Fraction(1)),
        (2, 3): (Fraction(1), Fraction(1)),
        (6, 3): (Fraction(1), Fraction(1)),
    }
    for (row, col), (width, height) in cases.items():
        pixel = fine.pixel(row, col)
        assert pixel.bounds == FINE.pixel_bounds(row, col)
        assert pixel.covered.width == width
        assert pixel.covered.height == height
        assert pixel.value == FINE_DATA[row, col]
        assert pixel.full_area == 2

    # pixels touching the window boundary only are excluded
    with pytest.raises(IndexError):
        fine.pixel(7, 1)
    with pytest.raises(IndexError):
        fine.pixel(2, 4)
    assert fine.covered == window
    assert fine.missing == ()


def test_crop_converts_units_without_guessing_scale() -> None:
    window = _window(102000, 107000, 202000, 207000, 'nm')
    coarse = crop_from_array(COARSE_DATA, COARSE, window, level=1)
    # coarse pixels are 4000x2500 nm at origin (101000, 200000)
    # x: [102000, 105000) pixel 0, [105000, 109000) pixel 1 partial
    # y: [202000, 202500) pixel 0 partial, [202500, 205000) pixel 1,
    #    [205000, 207500) pixel 2 partial to 207000
    assert (coarse.row_start, coarse.row_stop) == (0, 3)
    assert (coarse.col_start, coarse.col_stop) == (0, 2)
    assert coarse.shape == (3, 2)
    assert_array_equal(coarse.array, COARSE_DATA[:3, :2])
    assert coarse.pixel(0, 0).covered == PhysicalWindow(
        102000, 105000, 202000, 202500, 'nm'
    )
    assert coarse.pixel(1, 1).covered == PhysicalWindow(
        105000, 107000, 202500, 205000, 'nm'
    )
    assert coarse.covered == window


def test_crop_outside_source_reports_missing() -> None:
    # coarse source ends at x=113000 nm
    window = _window(113000, 115000, 202000, 207000, 'nm')
    coarse = crop_from_array(COARSE_DATA, COARSE, window, level=1)
    assert coarse.shape == (0, 0)
    assert coarse.pixels == ()
    assert coarse.covered is None
    assert coarse.outside_source
    assert coarse.missing
    total = sum(
        (piece.area for piece in coarse.missing), Fraction(0)
    )
    assert total == window.area


def test_delivery_requires_all_levels_and_switches_atomically() -> None:
    mosaic = ReviewMosaic({0: FINE, 1: COARSE}, cell=16)
    a1 = _window(102, 107)
    b = _window(106, 111)
    a2 = _window(108000, 115000, 202000, 207000, 'nm')

    for name, window in (('A', a1), ('B', b)):
        for level in (0, 1):
            mosaic.deliver(name, 1, level, _crop(level, window))
    assert mosaic.published_revisions() == {'A': 1, 'B': 1}
    opened = mosaic.preview()

    # coarse layer of the new revision arrives first: nothing switches
    assert not mosaic.deliver('A', 2, 1, _crop(1, a2))
    assert mosaic.published_revisions() == {'A': 1, 'B': 1}
    # stale fine layer from revision 1 is ignored
    assert not mosaic.deliver('A', 1, 0, _crop(0, a1))
    assert mosaic.published_revisions() == {'A': 1, 'B': 1}
    # latest fine layer completes the revision: both levels switch
    assert mosaic.deliver('A', 2, 0, _crop(0, a2))
    assert mosaic.published_revisions() == {'A': 2, 'B': 1}
    current = mosaic.preview()

    # the preview opened before the correction still sees the old view
    for x0, x1 in ((102, 106), (106, 109), (109, 111)):
        result = opened.partition(DisplayPartition(x0, x1, 202, 207, 'um'))
        for level in (0, 1):
            assert result.level(level).covered_area == (x1 - x0) * 5
    assert opened.work_orders == {'A': 1, 'B': 1}
    assert current.work_orders == {'A': 2, 'B': 1}

    # the new view: 102-106 no longer belongs to any work order
    empty = current.partition(DisplayPartition(102, 106, 202, 207, 'um'))
    assert empty.level(0).covered_area == 0
    assert empty.level(1).covered_area == 0
    assert empty.level(0).pixels == ()

    # B still contributes on 106-111; A overlaps on 108-111
    b_view = current.partition(DisplayPartition(106, 111, 202, 207, 'um'))
    assert b_view.level(1).covered_area == 25
    overlap = current.partition(
        DisplayPartition(108, 111, 202, 207, 'um')
    )
    assert overlap.level(1).covered_area == 15
    owners = {owner for pixel in overlap.level(1).pixels for owner in pixel.owners}
    assert owners == {'A', 'B'}


def test_revision_idempotence() -> None:
    mosaic = ReviewMosaic({0: FINE, 1: COARSE}, cell=16)
    a1 = _window(102, 107)
    a2 = _window(108000, 115000, 202000, 207000, 'nm')
    _publish(mosaic, 'A', 1, a1)
    before = mosaic.preview()
    before_result = before.partition(
        DisplayPartition(102, 107, 202, 207, 'um')
    )
    # re-sending a delivered level of the current revision is ignored
    assert not mosaic.deliver('A', 1, 0, _crop(0, a1))
    after = mosaic.preview()
    assert before.revision == after.revision
    assert result_values(before_result) == result_values(
        after.partition(DisplayPartition(102, 107, 202, 207, 'um'))
    )

    # deliver revision 2, then resend any old or new crop
    _publish(mosaic, 'A', 2, a2)
    snapshot = mosaic.preview()
    for revision, window in ((1, a1), (2, a2)):
        for level in (0, 1):
            assert not mosaic.deliver(
                'A', revision, level, _crop(level, window)
            )
    assert mosaic.published_revisions() == {'A': 2}
    assert mosaic.preview().revision == snapshot.revision


def result_values(mosaic) -> list[list[object]]:
    return [mosaic.level(level).values for level in mosaic]


def test_overlap_counted_once_and_coarse_column_trimmed() -> None:
    mosaic = ReviewMosaic({0: FINE, 1: COARSE}, cell=16)
    _publish(mosaic, 'A', 1, _window(102, 107))
    _publish(mosaic, 'B', 1, _window(106, 111))
    _publish(mosaic, 'A', 2, _window(108000, 115000, 202000, 207000, 'nm'))
    preview = mosaic.preview()

    # coarse column 1 spans x=[105, 109); only [106, 109) is covered
    left = preview.partition(DisplayPartition(105, 109, 202, 207, 'um'))
    coarse = left.level(1)
    columns = {pixel.source_col for pixel in coarse.pixels}
    assert columns == {1}
    assert min(pixel.covered.x0 for pixel in coarse.pixels) == 106
    assert coarse.covered_area == 15

    # overlap of A and B on 108-111 is attributed to both but counted once
    overlap = preview.partition(
        DisplayPartition(108, 111, 202, 207, 'um')
    )
    assert overlap.level(1).covered_area == 15
    assert overlap.owners(1) == {'A': Fraction(15), 'B': Fraction(15)}
    assert sum(
        (pixel.area for pixel in overlap.level(1).pixels), Fraction(0)
    ) == 15


def test_requested_beyond_source_differs_by_level() -> None:
    mosaic = ReviewMosaic({0: FINE, 1: COARSE}, cell=16)
    _publish(mosaic, 'A', 1, _window(108000, 115000, 202000, 207000, 'nm'))
    preview = mosaic.preview()
    result = preview.partition(DisplayPartition(113, 115, 202, 207, 'um'))
    # fine source extends to x=116
    assert result.level(0).beyond_source is False
    assert result.level(0).count == 10
    assert result.level(0).covered_area == 10
    # coarse source ends at x=113
    assert result.level(1).beyond_source is True
    assert result.level(1).count == 0
    assert result.level(1).outside_source is True


def test_uncalibrated_level_is_unlocatable_others_work() -> None:
    mosaic = ReviewMosaic({0: FINE, 1: None}, cell=16)
    _publish(mosaic, 'A', 1, _window(102, 107))
    result = mosaic.query(PhysicalWindow(102, 107, 202, 207, 'um'))
    assert result.level(0).locatable is True
    assert result.level(0).covered_area == 25
    assert result.level(1).locatable is False
    assert result.level(1).pixels == ()
    assert result.level(1).missing == (
        PhysicalWindow(102, 107, 202, 207, 'um'),
    )
    with pytest.raises(UnlocatableLevelError):
        mosaic.deliver('A', 2, 1, _crop(0, _window(102, 107)))


def test_assemble_matches_direct_query_across_partition_boundary() -> None:
    mosaic = ReviewMosaic({0: FINE, 1: COARSE}, cell=16)
    _publish(mosaic, 'A', 1, _window(102, 107))
    _publish(mosaic, 'B', 1, _window(106, 111))
    _publish(mosaic, 'A', 2, _window(108000, 115000, 202000, 207000, 'nm'))
    preview = mosaic.preview()

    # the partition boundary at x=107 crosses fine source pixel column 3
    partitions = [
        DisplayPartition(104, 107, 202, 205, 'um'),
        DisplayPartition(107, 110, 202, 205, 'um'),
        DisplayPartition(104, 110, 205, 207, 'um'),
    ]
    assembled = assemble(*(preview.partition(p) for p in partitions))
    direct = preview.query(PhysicalWindow(104, 110, 202, 207, 'um'))
    for level in (0, 1):
        left = assembled.level(level)
        right = direct.level(level)
        assert left.covered_area == right.covered_area
        assert left.source_keys == right.source_keys
        assert left.values == right.values
    # the boundary-crossing pixel is counted exactly once
    keys = assembled.level(0).source_keys
    assert keys.count((2, 3)) == 1


def test_arbitrary_window_and_delivery_order_agree() -> None:
    windows = (
        _window(100, 105, 200, 205),
        _window(104, 110, 201, 207),
        _window(102, 108, 203, 208),
    )
    results = []
    for order in (range(3), reversed(range(3))):
        mosaic = ReviewMosaic({0: FINE, 1: COARSE}, cell=16)
        for index in order:
            _publish(mosaic, f'W{index}', 1, windows[index])
        preview = mosaic.preview()
        direct = preview.query(PhysicalWindow(100, 110, 200, 208, 'um'))
        results.append(
            (
                direct.level(0).covered_area,
                direct.level(1).covered_area,
                sorted(
                    {
                        owner
                        for pixel in direct.level(1).pixels
                        for owner in pixel.owners
                    }
                ),
            )
        )
    assert results[0] == results[1]


def _pyramid_file(tmp_path):
    filename = tmp_path / 'pyramid.ome.tif'
    fine = numpy.arange(64 * 64, dtype=numpy.uint16).reshape(64, 64)
    coarse = numpy.arange(16 * 16, dtype=numpy.uint16).reshape(16, 16)
    with tifffile.TiffWriter(filename) as writer:
        writer.write(
            fine, subifds=1, tile=(16, 16), metadata={'axes': 'YX'}
        )
        writer.write(
            coarse,
            subfiletype=FILETYPE.REDUCEDIMAGE,
            tile=(16, 16),
            metadata={'axes': 'YX'},
        )
    return filename, fine, coarse


def test_crop_from_tiff_reads_intersecting_tiles(tmp_path) -> None:
    filename, fine, coarse = _pyramid_file(tmp_path)
    with tifffile.TiffFile(filename) as tif:
        series = tif.series[0]
        fine_calibration = LayerCalibration(64, 64, 0, 0, 1, 1, 'um')
        coarse_calibration = LayerCalibration(16, 16, 0, 0, 4, 4, 'um')
        window = PhysicalWindow(5, 37, 6, 39, 'um')
        patch0 = crop_from_tiff(tif, 0, window, fine_calibration)
        patch1 = crop_from_tiff(tif, 1, window, coarse_calibration)
        assert patch0.row_start == 6
        assert patch0.col_start == 5
        assert patch0.row_stop == 39
        assert patch0.col_stop == 37
        assert_array_equal(
            patch0.array,
            series.levels[0].asarray()[6:39, 5:37],
        )
        assert patch1.row_start == 1
        assert patch1.col_start == 1
        assert patch1.row_stop == 10
        assert patch1.col_stop == 10
        assert_array_equal(
            patch1.array,
            series.levels[1].asarray()[1:10, 1:10],
        )
        assert patch0.pixels[0].value == fine[6, 5]
        assert patch1.pixels[0].value == coarse[1, 1]


def test_crop_from_tiff_rejects_mask_series(tmp_path) -> None:
    filename = tmp_path / 'mask.tif'
    mask = numpy.zeros((64, 64), dtype=bool)
    mask[::2, ::2] = True
    data = numpy.arange(64 * 64, dtype=numpy.uint16).reshape(64, 64)
    with tifffile.TiffWriter(filename) as writer:
        writer.write(
            mask,
            subfiletype=FILETYPE.MASK,
            photometric='minisblack',
            bitspersample=1,
        )
        writer.write(data, subfiletype=0)
    with tifffile.TiffFile(filename) as tif:
        assert tif.series[0].keyframe.is_mask
        with pytest.raises(ValueError, match='mask'):
            crop_from_tiff(
                tif,
                0,
                PhysicalWindow(0, 8, 0, 8, 'um'),
                LayerCalibration(64, 64, 0, 0, 1, 1, 'um'),
            )


def test_withdraw_keeps_other_work_orders() -> None:
    mosaic = ReviewMosaic({0: FINE, 1: COARSE}, cell=16)
    _publish(mosaic, 'A', 1, _window(102, 107))
    _publish(mosaic, 'B', 1, _window(106, 111))
    preview = mosaic.preview()
    preview.partition(DisplayPartition(106, 111, 202, 207, 'um'))
    assert mosaic.withdraw('A') is True
    assert mosaic.published_revisions() == {'B': 1}
    result = mosaic.query(PhysicalWindow(106, 111, 202, 207, 'um'))
    assert {
        owner
        for pixel in result.level(0).pixels
        for owner in pixel.owners
    } == {'B'}
    assert result.level(0).covered_area == 25
    assert mosaic.withdraw('A') is False


def test_partition_cache_only_invalidated_where_correction_touches() -> None:
    width = 100
    fine = LayerCalibration(width, width, 0, 0, 1, 1, 'um')
    coarse = LayerCalibration(10, 10, 0, 0, 10, 10, 'um')
    data = numpy.arange(width * width, dtype=numpy.uint16).reshape(
        width, width
    )
    mosaic = ReviewMosaic({0: fine, 1: coarse}, cell=16)
    for index in range(10):
        window = PhysicalWindow(
            index * 10, index * 10 + 10, 0, 10, 'um'
        )
        for level, calibration, array in (
            (0, fine, data),
            (1, coarse, data[::10, ::10]),
        ):
            mosaic.deliver(
                f'W{index}',
                1,
                level,
                crop_from_array(array, calibration, window, level=level),
            )
    preview = mosaic.preview()
    far = DisplayPartition(0, 10, 20, 30, 'um')
    near = DisplayPartition(90, 100, 0, 10, 'um')
    old = DisplayPartition(0, 10, 0, 10, 'um')
    preview.partition(far)
    preview.partition(near)
    preview.partition(old)
    assert len(preview.cached_partitions()) == 3

    # correct W0 from x=[0,10),y=[0,10) to x=[20,30),y=[20,30)
    corrected = PhysicalWindow(20, 30, 20, 30, 'um')
    for level, calibration, array in (
        (0, fine, data),
        (1, coarse, data[::10, ::10]),
    ):
        mosaic.deliver(
            'W0',
            2,
            level,
            crop_from_array(array, calibration, corrected, level=level),
        )
    updated = mosaic.preview()
    # untouched partitions stay cached; the partition over the old W0
    # window is discarded
    cached = updated.cached_partitions()
    assert any(key[0] == 90 for key in cached)
    assert any(key[0] == 0 and key[2] == 20 for key in cached)
    assert not any(key[0] == 0 and key[2] == 0 for key in cached)
    # a fresh query of an unaffected partition returns correct data
    assert updated.partition(near).level(0).covered_area == 100
