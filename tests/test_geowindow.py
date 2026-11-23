"""Tests for area-weighted GeoTIFF window reads."""

from __future__ import annotations

import math

import numpy
import pytest

from geowindow import geometry
from geowindow.scenario import (
    COARSE_PIXEL,
    FINE_PIXEL,
    WINDOW,
    build_scenario,
)
from geowindow.service import (
    calibration_report,
    composite_reader,
    conflict_report,
    open_rasters,
    read_pixel,
    read_tiled,
    results_equal,
)


@pytest.fixture(scope='module')
def loaded(tmp_path_factory):
    directory = tmp_path_factory.mktemp('geowindow')
    paths = build_scenario(str(directory))
    rasters = open_rasters(
        {
            'A': paths.page('A'),
            'B': paths.page('B'),
            'C': paths.page('C'),
            'D': paths.page('D'),
        }
    )
    return rasters


@pytest.fixture(scope='module')
def reader(loaded):
    return composite_reader(loaded, ['B', 'A', 'C'])


# ---------------------------------------------------------------- geometry

def test_rotated_footprint_area(loaded):
    # Rotation preserves area: an 8x8 page still covers 64 map units.
    assert math.isclose(
        geometry.area(loaded['A'].base.footprint()), 64.0, abs_tol=1e-9
    )
    assert math.isclose(
        geometry.area(loaded['B'].base.footprint()), 64.0, abs_tol=1e-9
    )


def test_thumbnail_footprint_uses_actual_dimensions(loaded):
    # B's thumbnail is one column short: 4 rows x 3 cols at scale 2,
    # so its true footprint is 6 x 8 = 48 map units rather than 64.
    thumb_b = loaded['B'].levels[1]
    assert thumb_b.calibration.shape == (4, 3)
    assert math.isclose(
        geometry.area(thumb_b.footprint()), 48.0, abs_tol=1e-9
    )
    thumb_a = loaded['A'].levels[1]
    assert thumb_a.calibration.shape == (4, 4)
    assert math.isclose(
        geometry.area(thumb_a.footprint()), 64.0, abs_tol=1e-9
    )


def test_b_footprint_starts_on_a_slanted_edge(loaded):
    foot_a = loaded['A'].base.footprint()
    foot_b = loaded['B'].base.footprint()
    b_start = (2.4, 1.8)
    assert geometry.point_in_polygon(b_start, foot_a)
    assert geometry.point_in_polygon(b_start, foot_b)
    # A's lower edge and B's lower edge are collinear (3x - 4y = 0):
    # B starts on A's slanted edge and extends into A on the interior side.
    a_edge_end = foot_a[1]
    assert 3.0 * b_start[0] - 4.0 * b_start[1] == pytest.approx(0.0)
    assert 3.0 * a_edge_end[0] - 4.0 * a_edge_end[1] == pytest.approx(0.0)
    # A point offset to the exterior side of that edge is in neither page.
    exterior = (b_start[0] + 0.6, b_start[1] - 0.8)
    assert not geometry.point_in_polygon(exterior, foot_a)
    assert not geometry.point_in_polygon(exterior, foot_b)
    # B continues beyond A's opposite slanted edge (4x + 3y = 40).
    beyond_a = foot_b[2]
    assert not geometry.point_in_polygon(beyond_a, foot_a)
    assert geometry.point_in_polygon(beyond_a, foot_b)


def test_boundary_contact_has_zero_area():
    square = geometry.rectangle(0.0, 0.0, 2.0, 2.0)
    touching = geometry.rectangle(2.0, 0.0, 3.0, 2.0)
    assert geometry.intersect(square, touching) == []
    overlapping = geometry.rectangle(1.0, 0.0, 3.0, 2.0)
    assert math.isclose(
        geometry.area(geometry.intersect(square, overlapping)),
        2.0,
        abs_tol=1e-9,
    )


def test_difference_partition_is_disjoint_and_exact():
    from geowindow.window import difference, subtract_mask

    subject = geometry.rectangle(0.0, 0.0, 4.0, 4.0)
    cutter = geometry.rectangle(1.0, 1.0, 3.0, 3.0)
    pieces = difference(subject, cutter)
    assert math.isclose(
        sum(geometry.area(piece) for piece in pieces), 12.0, abs_tol=1e-9
    )
    # Pieces are mutually disjoint.
    for index, piece in enumerate(pieces):
        for other in pieces[index + 1 :]:
            assert geometry.intersect(piece, other) == []
    # Subtracting a full cover leaves nothing.
    assert subtract_mask(subject, [subject]) == []
    # Subtracting a boundary-only contact keeps the full polygon.
    contact = geometry.rectangle(4.0, 0.0, 5.0, 4.0)
    remainders = subtract_mask(subject, [contact])
    assert math.isclose(
        sum(geometry.area(piece) for piece in remainders),
        16.0,
        abs_tol=1e-9,
    )


# ------------------------------------------------------------ calibration

def test_page_levels(loaded):
    assert [lvl.calibration.shape for lvl in loaded['A'].levels] == [
        (8, 8),
        (4, 4),
    ]
    assert [lvl.calibration.shape for lvl in loaded['B'].levels] == [
        (8, 8),
        (4, 3),
    ]
    assert len(loaded['C'].levels) == 1
    assert len(loaded['D'].levels) == 1


def test_pixel_area_and_pixel_point_conventions(loaded):
    cal_a = loaded['A'].base.calibration
    cal_b = loaded['B'].base.calibration
    assert cal_a.convention == 'pixel-area'
    assert cal_b.convention == 'pixel-point'
    # A declares outer-corner raster (0, 0) at the map origin.
    assert cal_a.pixel_to_map(0.0, 0.0) == pytest.approx((0.0, 0.0))
    # B's parsed outer-corner transform starts at 3 * (cos, sin).
    assert cal_b.pixel_to_map(0.0, 0.0) == pytest.approx((2.4, 1.8))


def test_consistent_tiepoints(loaded):
    for name in ('A', 'B', 'D'):
        for level in loaded[name].levels:
            assert level.calibration.consistent, name
    b_checks = loaded['B'].base.calibration.checks
    assert len(b_checks) == 4
    assert all(check.residual == pytest.approx(0.0, abs=1e-9)
               for check in b_checks)


def test_c_conflict_diagnostic(loaded):
    raster_c = loaded['C']
    assert not raster_c.calibration_ok
    conflict = raster_c.conflict
    assert conflict['raster_pixel'] == (7.0, 7.0)
    assert conflict['declared_map'] == pytest.approx((5.4, 11.6))
    assert conflict['matrix_map'] == pytest.approx((3.9, 12.3))
    assert conflict['residual'] == pytest.approx(
        math.hypot(1.5, -0.7), abs=1e-9
    )
    report = conflict_report(loaded)
    assert [item['page'] for item in report] == ['C']


def test_calibration_report_contains_raw_evidence(loaded):
    report = calibration_report(loaded)
    pages = {(item['page'], item['level']) for item in report}
    assert pages == {
        ('A', 0), ('A', 1),
        ('B', 0), ('B', 1),
        ('C', 0),
        ('D', 0),
    }
    b_base = next(
        item for item in report
        if item['page'] == 'B' and item['level'] == 0
    )
    assert b_base['nodata'] == 0.0
    assert len(b_base['tiepoints']) == 4
    a_base = next(
        item for item in report
        if item['page'] == 'A' and item['level'] == 0
    )
    assert a_base['nodata'] is None


def test_ordinary_pixel_scale_page(loaded):
    cal_d = loaded['D'].base.calibration
    assert cal_d.matrix_source == 'pixel-scale-tiepoint'
    assert cal_d.convention == 'pixel-area'
    # Tie-point at pixel (2, 2) -> (12, 16) fixes origin at (10, 18).
    assert cal_d.pixel_to_map(0.0, 0.0) == pytest.approx((10.0, 18.0))
    pixel = read_pixel(loaded['D'], 0, 0)
    assert pixel['map_corners'][0] == pytest.approx((10.0, 18.0))
    assert pixel['map_corners'][2] == pytest.approx((11.0, 17.0))


# ------------------------------------------------------------- nodata rules

def test_undeclared_zero_is_valid(loaded):
    from tifffile import TiffFile

    level_a = loaded['A'].base
    assert level_a.nodata is None
    assert level_a.is_valid(2, 2)
    assert level_a.pixel_value(2, 2) == 0.0
    # tifffile's page attribute defaults to zero without a GDAL_NODATA tag,
    # but that decoder-side default must not be treated as a declaration.
    with TiffFile(loaded['A'].source_path) as tif:
        assert tif.pages[0].nodata == 0.0
        assert tif.pages[0].tags.valueof(42113) is None
    assert level_a.nodata is None


def test_declared_zero_is_nodata(loaded):
    level_b = loaded['B'].base
    assert level_b.nodata == 0.0
    assert not level_b.is_valid(0, 3)
    assert level_b.pixel_value(0, 3) == 0.0


def test_undeclared_zero_contributes_to_composite(reader):
    fine = reader.read(*WINDOW, FINE_PIXEL)
    cell = fine.cell(3, 0)
    zero = cell.find('A', 2, 2, level=0)
    assert zero is not None
    assert zero.value == 0.0
    assert zero.area == pytest.approx(5.0 / 6.0, abs=1e-9)


# ----------------------------------------------------------- compositing

def test_fine_read_uses_base_levels_only(reader):
    fine = reader.read(*WINDOW, FINE_PIXEL)
    for row in range(fine.shape[0]):
        for col in range(fine.shape[1]):
            assert all(
                contribution.level == 0
                for contribution in fine.cell(row, col).contributions
            )


def test_mixed_source_cell_area_weighting(reader):
    fine = reader.read(*WINDOW, FINE_PIXEL)
    cell = fine.cell(1, 2)
    b_piece = cell.find('B', 0, 0)
    a_piece = cell.find('A', 0, 2)
    assert b_piece is not None and a_piece is not None
    assert b_piece.area == pytest.approx(1.0 / 24.0, abs=1e-9)
    assert a_piece.area == pytest.approx(1.0 / 8.0, abs=1e-9)
    assert cell.coverage == pytest.approx(1.0 / 6.0, abs=1e-9)
    assert cell.value == pytest.approx(12.5, abs=1e-9)


def test_priority_changes_the_value(loaded, reader):
    fine_b_first = reader.read(*WINDOW, FINE_PIXEL)
    reader_a_first = composite_reader(loaded, ['A', 'B', 'C'])
    fine_a_first = reader_a_first.read(*WINDOW, FINE_PIXEL)
    cell = (2, 2)
    assert fine_b_first.cell(*cell).value == pytest.approx(
        450.0 / 23.0, abs=1e-9
    )
    assert fine_a_first.cell(*cell).value == pytest.approx(10.0, abs=1e-9)


def test_nodata_hole_backfills_from_lower_priority(reader):
    fine = reader.read(*WINDOW, FINE_PIXEL)
    cell = fine.cell(4, 4)
    # Full coverage is only reached because A fills B's NoData hole.
    assert cell.coverage == pytest.approx(1.0, abs=1e-9)
    assert cell.find('A', 0, 6) is not None
    # B's declared-NoData pixel never contributes.
    assert cell.find('B', 0, 3) is None
    assert cell.value == pytest.approx(15.0, abs=1e-9)


def test_backfill_required_for_full_coverage(loaded, reader):
    b_only = composite_reader(loaded, ['B'])
    fine_b = b_only.read(*WINDOW, FINE_PIXEL)
    fine = reader.read(*WINDOW, FINE_PIXEL)
    assert fine_b.cell(4, 4).coverage < 1.0
    assert fine.cell(4, 4).coverage == pytest.approx(1.0, abs=1e-9)


def test_edge_contact_cell_is_empty(reader):
    fine = reader.read(*WINDOW, FINE_PIXEL)
    cell = fine.cell(2, 4)
    assert cell.coverage == 0.0
    assert cell.contributions == []
    assert numpy.isnan(cell.value)


def test_out_of_footprint_falls_to_lower_priority(loaded, reader):
    # Cell (1, 1) has no interior intersection with B's footprint.
    foot_b = loaded['B'].base.footprint()
    cell_polygon = geometry.rectangle(1.0, 1.0, 2.0, 2.0)
    assert geometry.intersect(foot_b, cell_polygon) == []
    fine = reader.read(*WINDOW, FINE_PIXEL)
    cell = fine.cell(1, 1)
    assert cell.contributions
    assert {item.page for item in cell.contributions} == {'A'}
    assert cell.coverage == pytest.approx(5.0 / 6.0, abs=1e-9)


def test_boundary_contact_does_not_erase_valid_value():
    # In-memory axis-aligned rasters: high-priority H abuts cell (0, 0)
    # only along the edge x = 1, while low-priority L covers it fully.
    from geowindow.calibration import Calibration
    from geowindow.raster import GeoRaster, RasterLevel
    from geowindow.window import WindowReader

    # North-up footprints: L spans x[0, 2] y[0, 2]; H spans x[1, 3] y[0, 2].
    low_transform = numpy.array(
        [[1.0, 0.0, 0.0], [0.0, -1.0, 2.0], [0.0, 0.0, 1.0]]
    )
    high_transform = numpy.array(
        [[1.0, 0.0, 1.0], [0.0, -1.0, 2.0], [0.0, 0.0, 1.0]]
    )
    low = GeoRaster(
        name='L',
        levels=[
            RasterLevel(
                name='L',
                level=0,
                data=numpy.full((2, 2), 10.0),
                calibration=Calibration(
                    transform=low_transform,
                    convention='pixel-area',
                    matrix_source='transformation',
                    shape=(2, 2),
                ),
                nodata=None,
            )
        ],
    )
    high = GeoRaster(
        name='H',
        levels=[
            RasterLevel(
                name='H',
                level=0,
                data=numpy.full((2, 2), 99.0),
                calibration=Calibration(
                    transform=high_transform,
                    convention='pixel-area',
                    matrix_source='transformation',
                    shape=(2, 2),
                ),
                nodata=None,
            )
        ],
    )
    reader = WindowReader([high, low], priority=['H', 'L'])
    result = reader.read(0.0, 0.0, 2.0, 2.0, 1.0)
    # The boundary-only contact leaves L's full valid coverage untouched.
    assert result.cell(0, 0).coverage == pytest.approx(1.0, abs=1e-9)
    assert result.cell(0, 0).value == pytest.approx(10.0, abs=1e-9)
    assert {item.page for item in result.cell(0, 0).contributions} == {'L'}
    # The adjacent cell genuinely overlaps H and therefore uses H.
    assert result.cell(0, 1).value == pytest.approx(99.0, abs=1e-9)


def test_provenance_partitions_cell_area(reader):
    fine = reader.read(*WINDOW, FINE_PIXEL)
    for row in range(fine.shape[0]):
        for col in range(fine.shape[1]):
            cell = fine.cell(row, col)
            total = sum(item.area for item in cell.contributions)
            assert total == pytest.approx(
                cell.coverage, abs=1e-9
            )
            for item in cell.contributions:
                assert item.area > 0.0
                assert isinstance(item.page, str)
                assert isinstance(item.row, int)


def test_conflicting_page_excluded_but_readable(loaded, reader):
    fine = reader.read(*WINDOW, FINE_PIXEL)
    for row in range(fine.shape[0]):
        for col in range(fine.shape[1]):
            assert all(
                item.page != 'C'
                for item in fine.cell(row, col).contributions
            )
    pixel = read_pixel(loaded['C'], 3, 3)
    assert pixel['valid']
    assert pixel['value'] == 30.0


# ------------------------------------------------------------- tiled reads

def test_tiled_fine_read_matches_direct(reader):
    direct = reader.read(*WINDOW, FINE_PIXEL)
    tiled = read_tiled(reader, *WINDOW, FINE_PIXEL, split_col=5)
    assert results_equal(direct, tiled)
    numpy.testing.assert_array_equal(
        numpy.isnan(direct.values), numpy.isnan(tiled.values)
    )


def test_tiled_coarse_read_matches_direct(reader):
    direct = reader.read(*WINDOW, COARSE_PIXEL, allow_thumbnail=True)
    tiled = read_tiled(
        reader,
        *WINDOW,
        COARSE_PIXEL,
        split_col=2,
        allow_thumbnail=True,
    )
    assert results_equal(direct, tiled)


def test_tiled_swap_order_keeps_provenance(reader):
    direct = reader.read(*WINDOW, FINE_PIXEL)
    tiled = read_tiled(reader, *WINDOW, FINE_PIXEL, split_col=3)
    cell_direct = direct.cell(4, 4)
    cell_tiled = tiled.cell(4, 4)

    def contribution_key(item):
        return (item.page, item.level, item.row, item.col, item.area)

    assert sorted(
        contribution_key(c) for c in cell_direct.contributions
    ) == sorted(
        contribution_key(c) for c in cell_tiled.contributions
    )


def test_calibration_diagnostics_stable_across_reads(loaded, reader):
    # Diagnostics describe the pages, not a particular read path; running
    # direct and tiled (swapped-order) reads leaves them unchanged.
    conflicts_before = conflict_report(loaded)
    report_before = calibration_report(loaded)
    reader.read(*WINDOW, FINE_PIXEL)
    read_tiled(reader, *WINDOW, FINE_PIXEL, split_col=4)
    reader.read(*WINDOW, COARSE_PIXEL, allow_thumbnail=True)
    assert conflict_report(loaded) == conflicts_before
    assert calibration_report(loaded) == report_before
    assert all(
        item['page'] in {'A', 'B', 'C', 'D'} for item in report_before
    )


# -------------------------------------------------------- coarse/thumbnail

def test_thumbnail_selected_only_inside_its_coverage(reader):
    coarse = reader.read(*WINDOW, COARSE_PIXEL, allow_thumbnail=True)
    # Cell (2, 1) is fully covered by B's 4x3 thumbnail footprint.
    inside = coarse.cell(2, 1)
    assert inside.contributions
    assert {item.page for item in inside.contributions} == {'B'}
    assert {item.level for item in inside.contributions} == {1}
    # A thumbnail is never selected while B has higher priority coverage.
    assert all(
        item.page != 'A'
        for item in inside.contributions
    )


def test_missing_thumbnail_column_backfilled_from_base(reader):
    coarse = reader.read(*WINDOW, COARSE_PIXEL, allow_thumbnail=True)
    cell = coarse.cell(3, 3)
    thumb_pieces = [item for item in cell.contributions if item.level == 1]
    base_pieces = [item for item in cell.contributions if item.level == 0]
    assert thumb_pieces and base_pieces
    # Thumbnail only has three columns: no thumbnail pixel beyond col 2.
    assert max(item.col for item in thumb_pieces) == 2
    # Base-level B pixels fill the missing outer column region.
    assert {item.page for item in base_pieces} == {'B'}
    assert cell.coverage == pytest.approx(0.98958333333333, abs=1e-9)


def test_thumbnail_nodata_hole_backfilled_from_base(reader):
    coarse = reader.read(*WINDOW, COARSE_PIXEL, allow_thumbnail=True)
    cell = coarse.cell(3, 0)
    # Thumbnail pixel (row 2, col 1) is declared NoData and never appears.
    assert cell.find('B', 2, 1, level=1) is None
    # The same region is filled from B's base level instead.
    backfill = [
        item for item in cell.contributions
        if item.page == 'B' and item.level == 0
    ]
    assert backfill
    assert cell.coverage == pytest.approx(1.0, abs=1e-9)


def test_no_upscaling_to_manufacture_coverage(reader, loaded):
    coarse = reader.read(*WINDOW, COARSE_PIXEL, allow_thumbnail=True)
    thumb_b = loaded['B'].levels[1]
    thumb_footprint = thumb_b.footprint()
    for row in range(coarse.shape[0]):
        for col in range(coarse.shape[1]):
            for item in coarse.cell(row, col).contributions:
                if item.page == 'B' and item.level == 1:
                    pixel = thumb_b.pixel_polygon(item.row, item.col)
                    # Every thumbnail piece lies inside the true footprint.
                    assert geometry.intersect(pixel, thumb_footprint)


def test_coarse_cells_partition_to_full_area(reader):
    coarse = reader.read(*WINDOW, COARSE_PIXEL, allow_thumbnail=True)
    total_covered = sum(
        item.area
        for row in range(coarse.shape[0])
        for col in range(coarse.shape[1])
        for item in coarse.cell(row, col).contributions
    )
    fine = reader.read(*WINDOW, FINE_PIXEL)
    fine_total = sum(
        item.area
        for row in range(fine.shape[0])
        for col in range(fine.shape[1])
        for item in fine.cell(row, col).contributions
    )
    assert total_covered == pytest.approx(fine_total, abs=1e-8)
