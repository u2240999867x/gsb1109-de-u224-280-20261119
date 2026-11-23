"""Mixed OME-TIFF export end-to-end demonstration.

Run with:

    PYTHONPATH=. .venv/bin/python examples/ome_mixed_export.py

The script builds two microscope source packages (X with two Images and a
companion TIFF, Y with one Image), mixes selected channels into a standalone
OME-TIFF, then re-selects channels both from that export and directly from
the originals to show the results match.
"""

from __future__ import annotations

import shutil

import numpy as np

from ome_export import ExportItem, OmeExporter, read_package, verify
from ome_export.fixtures import build_sources

WORKDIR = 'scratch/demo'


def main() -> None:
    shutil.rmtree(WORKDIR, ignore_errors=True)
    paths = build_sources(f'{WORKDIR}/sources')

    # 1) first mixed export: Y first, then both X images, reordered channels
    first_path = f'{WORKDIR}/first_mix.ome.tif'
    with read_package(paths['x']) as x_pkg, read_package(paths['y']) as y_pkg:
        report = OmeExporter().export(
            first_path,
            [
                ExportItem(y_pkg, 'Image:0', [1, 0], name='Y reordered'),
                ExportItem(x_pkg, 'Image:0', [1, 0], name='X0 subset'),
                ExportItem(x_pkg, 'Image:1', [1, 0], name='X1 reversed'),
            ],
        )
    first = verify.inspect(first_path)
    verify.check_references(first)
    verify.check_tiffdata(first)
    verify.check_pixels(
        first,
        [
            np.take(paths['y_pixels'], [1, 0], axis=2),
            np.take(paths['x0_pixels'], [1, 0], axis=2),
            np.take(paths['x1_pixels'], [1, 0], axis=2),
        ],
    )
    print('first export images:', [image.name for image in first.images])
    print('pruned records:', [(p.kind, p.source_id) for p in report.pruned])

    # 2a) second export, reading only the first export (originals still open
    #     here, but the identical path works with the originals deleted)
    from_output = f'{WORKDIR}/from_export.ome.tif'
    with read_package(first_path) as first_pkg:
        images = list(first_pkg.images.values())
        OmeExporter().export(
            from_output,
            [
                ExportItem(first_pkg, images[0].id, [1]),
                ExportItem(first_pkg, images[2].id, [1]),
            ],
        )

    # 2b) the same selection made straight from the original sources
    from_sources = f'{WORKDIR}/from_sources.ome.tif'
    with read_package(paths['x']) as x_pkg, read_package(paths['y']) as y_pkg:
        OmeExporter().export(
            from_sources,
            [
                ExportItem(y_pkg, 'Image:0', [0]),
                ExportItem(x_pkg, 'Image:1', [0]),
            ],
        )

    semantic_a = verify.inspect(from_output)
    semantic_b = verify.inspect(from_sources)
    verify.check_references(semantic_a)
    verify.check_tiffdata(semantic_a)
    verify.check_references(semantic_b)
    verify.check_tiffdata(semantic_b)
    print('standalone relocation:', verify.check_standalone(first_path))
    print('calibration groups:', verify.calibration_versions(first))
    print('second export image count:', len(semantic_a.images))


if __name__ == '__main__':
    main()
