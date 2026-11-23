"""Demo: area-weighted GeoTIFF window reads with per-pixel provenance.

Run with::

    python examples/geowindow_demo.py

The script builds the A/B/C/D scenario in a temporary directory, prints
the raw calibration evidence of every page and level, the C tie-point
conflict, then performs fine (direct and tiled) and coarse window reads
and shows the value, coverage and source pieces of selected cells.
"""

from __future__ import annotations

import tempfile

import numpy

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
    read_tiled,
    results_equal,
)


def print_report(reports):
    print('=== raw calibration evidence ===')
    for item in reports:
        print(
            f"page {item['page']} level {item['level']} "
            f"shape {item['shape']} convention {item['convention']} "
            f"source {item['matrix_source']} nodata {item['nodata']}"
        )
        for tiepoint in item['tiepoints']:
            flag = 'ok' if tiepoint['consistent'] else 'CONFLICT'
            matrix = tuple(
                round(v, 4) for v in tiepoint['matrix_map']
            )
            print(
                f"  pixel {tiepoint['raster']} "
                f"declared {tiepoint['declared_map']} "
                f"matrix {matrix} "
                f"residual {tiepoint['residual']:.4f} [{flag}]"
            )


def print_cell(result, row, col):
    cell = result.cell(row, col)
    value = 'NaN' if numpy.isnan(cell.value) else f'{cell.value:.4f}'
    print(
        f'cell ({row}, {col}) value {value} '
        f'coverage {cell.coverage:.4f} pieces {len(cell.contributions)}'
    )
    for piece in cell.contributions:
        print(
            f'  page {piece.page} level {piece.level} '
            f'pixel ({piece.row}, {piece.col}) value {piece.value} '
            f'area {piece.area:.4f}'
        )


def main() -> None:
    with tempfile.TemporaryDirectory() as directory:
        paths = build_scenario(directory)
        rasters = open_rasters(
            {
                'A': paths.page('A'),
                'B': paths.page('B'),
                'C': paths.page('C'),
                'D': paths.page('D'),
            }
        )

        print_report(calibration_report(rasters))

        print('\n=== conflicts (excluded from compositing) ===')
        for conflict in conflict_report(rasters):
            print(conflict)

        reader = composite_reader(rasters, ['B', 'A', 'C'])

        print('\n=== fine read (pixel size 1) ===')
        fine = reader.read(*WINDOW, FINE_PIXEL)
        numpy.set_printoptions(precision=3, suppress=True, linewidth=120)
        print('values:\n', fine.values)
        print('coverage:\n', fine.coverage)
        print_cell(fine, 1, 2)  # two sources
        print_cell(fine, 2, 4)  # slanted-edge boundary contact only
        print_cell(fine, 4, 4)  # B NoData hole backfilled by A
        print_cell(fine, 3, 0)  # A's real zero contributes

        print('\n=== tiled read consistency ===')
        tiled = read_tiled(reader, *WINDOW, FINE_PIXEL, split_col=5)
        print('cell-by-cell identical:', results_equal(fine, tiled))

        print('\n=== coarse read (pixel size 2, thumbnails allowed) ===')
        coarse = reader.read(*WINDOW, COARSE_PIXEL, allow_thumbnail=True)
        print('coverage:\n', coarse.coverage)
        print_cell(coarse, 2, 1)  # fully served by B thumbnail
        print_cell(coarse, 3, 0)  # thumbnail hole backfilled from base
        print_cell(coarse, 3, 3)  # missing thumbnail column backfilled
        coarse_tiled = read_tiled(
            reader,
            *WINDOW,
            COARSE_PIXEL,
            split_col=2,
            allow_thumbnail=True,
        )
        print(
            'coarse cell-by-cell identical:',
            results_equal(coarse, coarse_tiled),
        )


if __name__ == '__main__':
    main()
