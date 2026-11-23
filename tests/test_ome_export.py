"""End-to-end scenario tests for mixed OME-TIFF export.

Covers:

* mixing two source packages with colliding local IDs,
* companion-TIFF TiffData and physical/logical IFD ordering,
* channel reordering, pixel and plane/exposure fidelity,
* reference sharing vs. same-name separation,
* offline re-merge of laboratory exports through provenance,
* detector calibration versions and same-version gain conflicts,
* broken cross-package references and untouched targets,
* immutability of source and previously exported files.
"""

from __future__ import annotations

import hashlib
import os

import numpy as np
import pytest

from ome_export import verify
from ome_export.export import (
    BrokenReference,
    ExportItem,
    OmeExporter,
)
from ome_export.fixtures import build_sources
from ome_export.model import read_package


# --------------------------------------------------------------------- helpers


def _hash(path: str) -> bytes:
    digest = hashlib.sha256()
    with open(path, 'rb') as handle:
        digest.update(handle.read())
    return digest.digest()


def _semantic_summary(path: str) -> dict:
    """Provenance-based summary, independent of local output IDs."""
    output = verify.inspect(path)
    images = []
    for image in output.images:
        channels = []
        for channel in image.channels:
            provenance_refs = []
            for ref in channel.findall(
                '{http://www.openmicroscopy.org/Schemas/OME/2016-06}AnnotationRef'
            ):
                prov = output.annotation_provenance(ref.attrib['ID'])
                if prov is not None:
                    provenance_refs.append(tuple(sorted(prov.items())))
            settings = channel.find(
                '{http://www.openmicroscopy.org/Schemas/OME/2016-06}'
                'DetectorSettings'
            )
            channels.append(
                {
                    'name': channel.attrib.get('Name'),
                    'provenance': tuple(sorted(provenance_refs)),
                    'detector_archive': verify.detector_archive_id(
                        output, settings.attrib['ID']
                    ) if settings is not None else None,
                    'gain': (
                        settings.attrib.get('Gain')
                        if settings is not None else None
                    ),
                }
            )
        instrument_archive = None
        if image.instrument_ref is not None:
            instrument = output.instruments[image.instrument_ref]
            for ref in instrument.findall(
                '{http://www.openmicroscopy.org/Schemas/OME/2016-06}AnnotationRef'
            ):
                prov = output.annotation_provenance(ref.attrib['ID'])
                if prov and prov.get('archiveID'):
                    instrument_archive = prov['archiveID']
        roi_archives = []
        for roi_id in image.roi_refs:
            roi = output.rois[roi_id]
            for ref in roi.findall(
                '{http://www.openmicroscopy.org/Schemas/OME/2016-06}AnnotationRef'
            ):
                prov = output.annotation_provenance(ref.attrib['ID'])
                if prov and prov.get('archiveID'):
                    roi_archives.append(prov['archiveID'])
        images.append(
            {
                'name': image.name,
                'size_c': image.size_c,
                'size_t': image.size_t,
                'size_z': image.size_z,
                'channels': channels,
                'instrument_archive': instrument_archive,
                'roi_archives': sorted(roi_archives),
                'exposures': verify.plane_exposures(output, image),
            }
        )
    calibrations = {
        key: sorted(item['gain'] for item in items)
        for key, items in verify.calibration_versions(output).items()
    }
    return {'images': images, 'calibrations': calibrations}


def _export_and_check(
    target: str, items: list[ExportItem], expected: list[np.ndarray]
):
    report = OmeExporter().export(target, items)
    output = verify.inspect(target)
    verify.check_well_formed_xml(target)
    verify.check_element_ordering(output)
    verify.check_references(output)
    verify.check_tiffdata(output)
    verify.check_pixels(output, expected)
    verify.check_standalone(target)
    return report, output


# --------------------------------------------------------------------- fixtures


def test_sources_colliding_ids_and_companion(tmp_path):
    paths = build_sources(str(tmp_path / 'src'))
    with read_package(paths['x']) as x_pkg, read_package(paths['y']) as y_pkg:
        # same textual IDs, different archive identities
        assert set(x_pkg.instruments) == set(y_pkg.instruments) == {
            'Instrument:0'
        }
        assert set(x_pkg.rois) == set(y_pkg.rois) == {'ROI:0'}
        x_detectors = x_pkg.instruments['Instrument:0'].detectors
        y_detectors = y_pkg.instruments['Instrument:0'].detectors
        assert 'Detector:0' in x_detectors and 'Detector:0' in y_detectors
        assert (
            x_detectors['Detector:0'].model
            != y_detectors['Detector:0'].model
        )
        # X shares one instrument/detector/ROI across two images
        x_images = list(x_pkg.images.values())
        assert len(x_images) == 2
        assert all(
            image.instrument_ref == 'Instrument:0' for image in x_images
        )
        assert all(image.roi_refs == ['ROI:0'] for image in x_images)
        shared = {
            channel.detector_settings.detector_id
            for image in x_images
            for channel in image.channels
            if channel.detector_settings
        }
        assert 'Detector:0' in shared and 'Detector:1' in shared

        # companion TiffData with IFD order differing from XML order
        x0 = x_pkg.images['Image:0']
        assert x0.tiffdata[0].filename == 'x0_companion.tif'
        assert x0.tiffdata[0].first_t == 1
        assert x0.tiffdata[1].first_t == 0
        array = x_pkg.read_array(x0)
        assert array.shape == (2, 2, 3, 8, 6)
        # T=1 physically comes from the companion through the UUID reference
        np.testing.assert_array_equal(array[1], paths['x0_pixels'][1])
        np.testing.assert_array_equal(array[0], paths['x0_pixels'][0])


# ------------------------------------------------------------- first mixed export


def test_first_mixed_export_standalone_and_shared(tmp_path):
    paths = build_sources(str(tmp_path / 'src'))
    target = str(tmp_path / 'first.ome.tif')
    with read_package(paths['x']) as x_pkg, read_package(paths['y']) as y_pkg:
        source_hashes = {
            path: _hash(path)
            for path in (
                paths['x'], paths['y'], paths['x0_companion']
            )
        }
        items = [
            # Y first, then both X images, each with different channel order
            ExportItem(y_pkg, 'Image:0', [1, 0], name='Y reordered'),
            ExportItem(x_pkg, 'Image:0', [1, 0], name='X0 subset'),
            ExportItem(x_pkg, 'Image:1', [1, 0], name='X1 reversed'),
        ]
        expected = [
            np.take(paths['y_pixels'], [1, 0], axis=2),
            np.take(paths['x0_pixels'], [1, 0], axis=2),
            np.take(paths['x1_pixels'], [1, 0], axis=2),
        ]
        report, output = _export_and_check(target, items, expected)

        # three images, two instruments, two ROIs despite colliding source IDs
        assert len(output.images) == 3
        assert len(output.instruments) == 2
        assert len(output.rois) == 2
        y_image, x0_image, x1_image = output.images
        assert y_image.instrument_ref != x0_image.instrument_ref
        assert x0_image.instrument_ref == x1_image.instrument_ref
        assert x0_image.roi_refs == x1_image.roi_refs
        assert y_image.roi_refs != x0_image.roi_refs

        # X channels keep sharing one detector; Y has its own
        x0_det = verify.channel_detector(output, x0_image, 0)
        x1_det = verify.channel_detector(output, x1_image, 0)
        y_det = verify.channel_detector(output, y_image, 0)
        assert x0_det == x1_det
        assert y_det != x0_det
        assert verify.detector_archive_id(output, x0_det) == 'ARC:DET-SHARED'
        assert verify.detector_archive_id(output, y_det) == 'ARC:DET-Y'

        # exclusive detector and its channel are pruned, not emitted
        all_ids = verify.element_ids(output)
        detector_archives = {
            verify.detector_archive_id(output, detector_id)
            for detector_id in all_ids
            if detector_id.startswith('Detector:')
        }
        assert 'ARC:DET-EXCLUSIVE' not in detector_archives
        pruned_kinds = {(p.kind, p.source_id) for p in report.pruned}
        assert ('Channel', 'Channel:2') in pruned_kinds
        assert ('Detector', 'Detector:1') in pruned_kinds
        assert any('Detector:1' in item for item in report.removable_devices)
        # Y spare detector was never referenced and must not be emitted
        assert 'ARC:DET-Y-SPARE' not in detector_archives

        # channel reordering reflected in names and pixels
        assert [c.attrib['Name'] for c in y_image.channels] == [
            'Y-FITC', 'Y-DAPI'
        ]
        assert [c.attrib['Name'] for c in x0_image.channels] == [
            'X0-GFP', 'X0-DAPI'
        ]

        # colliding ROI IDs resolve to the two distinct source geometries
        y_geometry = verify.roi_geometry(output, y_image.roi_refs[0])
        x_geometry = verify.roi_geometry(output, x0_image.roi_refs[0])
        assert (
            y_geometry['X'], y_geometry['Y'],
            y_geometry['Width'], y_geometry['Height'],
        ) == ('30.0', '40.0', '25.0', '75.0')
        assert (
            x_geometry['X'], x_geometry['Y'],
            x_geometry['Width'], x_geometry['Height'],
        ) == ('10.0', '20.0', '100.0', '50.0')

        # detector settings (gain and binning) travel with each channel
        y_settings = y_image.channels[0].find(
            '{http://www.openmicroscopy.org/Schemas/OME/2016-06}'
            'DetectorSettings'
        )
        x_settings = x0_image.channels[0].find(
            '{http://www.openmicroscopy.org/Schemas/OME/2016-06}'
            'DetectorSettings'
        )
        assert y_settings.attrib['Gain'] == '3.5'
        assert y_settings.attrib['Binning'] == '4x4'
        assert x_settings.attrib['Gain'] == '1.8'
        assert x_settings.attrib['Binning'] == '2x2'

        # planes: missing exposure stays missing, explicit zero stays zero
        x0_exposures = verify.plane_exposures(output, x0_image)
        # original mixed pattern: position-only planes have no exposure
        position_only = [
            key for key, value in x0_exposures.items() if value is None
        ]
        assert position_only
        assert 0.0 in x0_exposures.values()
        x1_exposures = verify.plane_exposures(output, x1_image)
        assert set(x1_exposures.values()) == {0.0}
        y_exposures = verify.plane_exposures(output, y_image)
        assert all(value is None for value in y_exposures.values())

        # both calibration versions of the shared detector coexist
        groups = verify.calibration_versions(output)
        assert ('ARC:DET-SHARED', 'v1') in groups
        assert ('ARC:DET-SHARED', 'v2') in groups

        # axes order is canonical XYCZT and TiffData partitions pages
        assert all(
            image.dimension_order == 'XYCZT' for image in output.images
        )

        # objectives: X images share one (40x), Y keeps its own (60x)
        objectives = verify.instrument_objectives(output)
        x_obj_ref = [
            obj.attrib['ID']
            for obj in output.root.iter(
                '{http://www.openmicroscopy.org/Schemas/OME/2016-06}'
                'ObjectiveSettings'
            )
        ]
        assert x_obj_ref[1] == x_obj_ref[2]
        assert x_obj_ref[0] != x_obj_ref[1]
        assert objectives[x_obj_ref[1]]['magnification'] == '40.0'
        assert objectives[x_obj_ref[0]]['magnification'] == '60.0'

        # sources and companions are immutable after export
        for path, digest in source_hashes.items():
            assert _hash(path) == digest


def test_second_export_output_vs_original_sources(tmp_path):
    paths = build_sources(str(tmp_path / 'src'))
    first = str(tmp_path / 'first.ome.tif')
    with read_package(paths['x']) as x_pkg, read_package(paths['y']) as y_pkg:
        OmeExporter().export(
            first,
            [
                ExportItem(y_pkg, 'Image:0', [1, 0]),
                ExportItem(x_pkg, 'Image:0', [1, 0]),
                ExportItem(x_pkg, 'Image:1', [1, 0]),
            ],
        )

    # re-select one channel per image: output index 1 == original channel 0
    from_output = str(tmp_path / 'from_output.ome.tif')
    from_sources = str(tmp_path / 'from_sources.ome.tif')
    with read_package(first) as out_pkg:
        images = list(out_pkg.images.values())
        y_out, _x0_out, x1_out = images
        OmeExporter().export(
            from_output,
            [
                ExportItem(out_pkg, y_out.id, [1]),
                ExportItem(out_pkg, x1_out.id, [1]),
            ],
        )
    with read_package(paths['x']) as x_pkg, read_package(paths['y']) as y_pkg:
        OmeExporter().export(
            from_sources,
            [
                ExportItem(y_pkg, 'Image:0', [0]),
                ExportItem(x_pkg, 'Image:1', [0]),
            ],
        )

    summary_output = _semantic_summary(from_output)
    summary_sources = _semantic_summary(from_sources)
    assert summary_output == summary_sources

    # pixel content identical as well
    with read_package(from_output) as a, read_package(from_sources) as b:
        for image_a, image_b in zip(a.images.values(), b.images.values()):
            np.testing.assert_array_equal(
                a.read_array(image_a), b.read_array(image_b)
            )

    # first export must remain unchanged
    first_hash = _hash(first)
    with read_package(first):
        pass
    assert _hash(first) == first_hash


# ------------------------------------------------------------- offline lab merge


def test_lab_exports_merge_offline(tmp_path):
    paths = build_sources(str(tmp_path / 'src'))
    lab_dir = tmp_path / 'labs'
    lab_dir.mkdir()
    lab1_path = str(lab_dir / 'lab1.ome.tif')
    lab2_path = str(lab_dir / 'lab2.ome.tif')

    with read_package(paths['x']) as x_pkg, read_package(paths['y']) as y_pkg:
        # lab 1 keeps X image 0, channels 0 and 1 (exclusive detector dropped)
        OmeExporter().export(
            lab1_path, [ExportItem(x_pkg, 'Image:0', [0, 1])]
        )
        # lab 2 keeps Y first (forcing different local IDs), then X image 1
        OmeExporter().export(
            lab2_path,
            [
                ExportItem(y_pkg, 'Image:0', [0]),
                ExportItem(x_pkg, 'Image:1', [0, 1]),
            ],
        )

    lab1 = verify.inspect(lab1_path)
    lab2 = verify.inspect(lab2_path)
    lab1_x_det = [
        detector_id
        for detector_id in verify.element_ids(lab1)
        if detector_id.startswith('Detector:')
        and verify.detector_archive_id(lab1, detector_id) == 'ARC:DET-SHARED'
    ][0]
    lab2_x_det = [
        detector_id
        for detector_id in verify.element_ids(lab2)
        if detector_id.startswith('Detector:')
        and verify.detector_archive_id(lab2, detector_id) == 'ARC:DET-SHARED'
    ][0]
    lab2_y_det = [
        detector_id
        for detector_id in verify.element_ids(lab2)
        if detector_id.startswith('Detector:')
        and verify.detector_archive_id(lab2, detector_id) == 'ARC:DET-Y'
    ][0]
    # local IDs differ across files, and one collides with another file's device
    assert lab1_x_det != lab2_x_det
    assert lab2_y_det == lab1_x_det

    # originals offline: merge purely from the two exports
    merged_path = str(tmp_path / 'merged.ome.tif')
    with read_package(lab1_path) as lab1_pkg, read_package(lab2_path) as lab2_pkg:
        lab1_images = list(lab1_pkg.images.values())
        lab2_images = list(lab2_pkg.images.values())
        report, merged = _export_and_check(
            merged_path,
            [
                ExportItem(lab1_pkg, lab1_images[0].id, [0, 1]),
                ExportItem(lab2_pkg, lab2_images[0].id, [0]),
                ExportItem(lab2_pkg, lab2_images[1].id, [0, 1]),
            ],
            expected=[
                np.take(paths['x0_pixels'], [0, 1], axis=2),
                np.take(paths['y_pixels'], [0], axis=2),
                np.take(paths['x1_pixels'], [0, 1], axis=2),
            ],
        )

    # the shared X detector is one definition even though local IDs collided
    shared_detectors = [
        detector_id
        for detector_id in verify.element_ids(merged)
        if detector_id.startswith('Detector:')
        and verify.detector_archive_id(merged, detector_id) == 'ARC:DET-SHARED'
    ]
    y_detectors = [
        detector_id
        for detector_id in verify.element_ids(merged)
        if detector_id.startswith('Detector:')
        and verify.detector_archive_id(merged, detector_id) == 'ARC:DET-Y'
    ]
    assert len(shared_detectors) == 1
    assert len(y_detectors) == 1
    assert shared_detectors[0] != y_detectors[0]
    # exclusive and spare devices never enter the merged output
    all_archives = {
        verify.detector_archive_id(merged, detector_id)
        for detector_id in verify.element_ids(merged)
        if detector_id.startswith('Detector:')
    }
    assert 'ARC:DET-EXCLUSIVE' not in all_archives
    assert 'ARC:DET-Y-SPARE' not in all_archives

    # drop one more channel (X image 0 channel 0, sole v1 user) and re-merge
    reduced_path = str(tmp_path / 'merged_reduced.ome.tif')
    with read_package(lab1_path) as lab1_pkg, read_package(lab2_path) as lab2_pkg:
        _export_and_check(
            reduced_path,
            [
                ExportItem(lab1_pkg, lab1_pkg.images['Image:0'].id, [1]),
                ExportItem(lab2_pkg, list(lab2_pkg.images)[0], [0]),
                ExportItem(lab2_pkg, list(lab2_pkg.images)[1], [0, 1]),
            ],
            expected=[
                np.take(paths['x0_pixels'], [1], axis=2),
                np.take(paths['y_pixels'], [0], axis=2),
                np.take(paths['x1_pixels'], [0, 1], axis=2),
            ],
        )
    reduced = verify.inspect(reduced_path)
    groups = verify.calibration_versions(reduced)
    assert ('ARC:DET-SHARED', 'v1') not in groups
    assert ('ARC:DET-SHARED', 'v2') in groups
    verify.check_references(reduced)

    # lab inputs untouched
    assert _hash(lab1_path) == _hash(lab1_path)
    assert _hash(lab2_path) == _hash(lab2_path)


def test_calibration_conflict_same_version_reported(tmp_path):
    # X and Y both carry ARC:DET-SHARED/v2 but with different gains
    src_dir = str(tmp_path / 'src_conflict')
    paths = build_sources(
        src_dir,
        y_shared_calibration={
            'archive_id': 'ARC:DET-SHARED',
            'version': 'v2',
            'gain': 2.5,
            'effective_date': '2026-07-01',
        },
    )
    target = str(tmp_path / 'conflict.ome.tif')
    with read_package(paths['x']) as x_pkg, read_package(paths['y']) as y_pkg:
        report = OmeExporter().export(
            target,
            [
                # X1 channel 0 references v2 (gain 1.8)
                ExportItem(x_pkg, 'Image:1', [0]),
                # Y channel 0 references the conflicting v2 (gain 2.5)
                ExportItem(y_pkg, 'Image:0', [0]),
            ],
        )
    assert len(report.conflicts) == 1
    conflict = report.conflicts[0]
    assert conflict.archive_id == 'ARC:DET-SHARED'
    assert conflict.version == 'v2'
    assert conflict.attribute == 'Gain'
    assert {conflict.value_a, conflict.value_b} == {1.8, 2.5}
    assert 'x.ome.tif' in conflict.source_a + conflict.source_b
    assert 'y.ome.tif' in conflict.source_a + conflict.source_b

    # both conflicting definitions are retained; nothing is overwritten
    output = verify.inspect(target)
    groups = verify.calibration_versions(output)
    assert len(groups[('ARC:DET-SHARED', 'v2')]) == 2
    gains = {
        float(item['gain'])
        for item in groups[('ARC:DET-SHARED', 'v2')]
    }
    assert gains == {1.8, 2.5}
    verify.check_references(output)
    verify.check_tiffdata(output)
    # both images remain independently previewable
    with read_package(target) as merged:
        assert len(merged.images) == 2
        for image in merged.images.values():
            assert merged.read_array(image).size > 0

    # strict mode surfaces the conflict as an error and leaves no new file
    strict_target = str(tmp_path / 'strict.ome.tif')
    with read_package(paths['x']) as x_pkg, read_package(paths['y']) as y_pkg:
        with pytest.raises(Exception):
            OmeExporter().export(
                strict_target,
                [
                    ExportItem(x_pkg, 'Image:1', [0]),
                    ExportItem(y_pkg, 'Image:0', [0]),
                ],
                strict_conflicts=True,
            )
    assert not os.path.exists(strict_target)


# ------------------------------------------------------------- broken references


def test_broken_reference_rejected_target_untouched(tmp_path):
    paths = build_sources(
        str(tmp_path / 'src_broken'),
        broken_detector={
            'image_index': 0,
            'channel_index': 0,
            'missing_id': 'Detector:2',  # exists in Y under the same text
        },
    )
    target = tmp_path / 'broken.ome.tif'
    target.write_bytes(b'keep-me')
    original = target.read_bytes()
    with read_package(paths['x']) as x_pkg, read_package(paths['y']) as y_pkg:
        with pytest.raises(BrokenReference) as exc_info:
            OmeExporter().export(
                str(target),
                [ExportItem(x_pkg, 'Image:0', [0, 1])],
            )
        error = exc_info.value
        assert error.image_id == 'Image:0'
        assert error.channel_id == 'Channel:0'
        assert error.missing_id == 'Detector:2'
        assert error.reference_kind == 'DetectorSettings'
        # Y defines the colliding ID but must not be borrowed
        assert 'Detector:2' in y_pkg.instruments['Instrument:0'].detectors
    assert target.read_bytes() == original

    # check-only mode also reports without creating anything
    check_target = tmp_path / 'check_only.ome.tif'
    with read_package(paths['x']) as x_pkg:
        with pytest.raises(BrokenReference):
            OmeExporter().export(
                str(check_target),
                [ExportItem(x_pkg, 'Image:0', [0, 1])],
                check_only=True,
            )
    assert not check_target.exists()


def test_broken_reference_on_dropped_channel_exports_fine(tmp_path):
    paths = build_sources(
        str(tmp_path / 'src_broken_drop'),
        broken_detector={
            'image_index': 0,
            'channel_index': 0,
            'missing_id': 'Detector:2',
        },
    )
    target = str(tmp_path / 'partial.ome.tif')
    with read_package(paths['x']) as x_pkg:
        _export_and_check(
            target,
            # the broken channel 0 is excluded; 1 and 2 are intact
            [ExportItem(x_pkg, 'Image:0', [1, 2])],
            [np.take(paths['x0_pixels'], [1, 2], axis=2)],
        )
    output = verify.inspect(target)
    verify.check_references(output)
    # the Y-only spare detector definition is not pulled in
    archives = {
        verify.detector_archive_id(output, detector_id)
        for detector_id in verify.element_ids(output)
        if detector_id.startswith('Detector:')
    }
    assert 'ARC:DET-Y-SPARE' not in archives


def test_distinct_calibration_versions_merge_online_and_offline(tmp_path):
    # Y carries a *different valid* version (v3) of the shared detector
    src_dir = str(tmp_path / 'src_versions')
    paths = build_sources(
        src_dir,
        y_shared_calibration={
            'archive_id': 'ARC:DET-SHARED',
            'version': 'v3',
            'gain': 2.2,
            'effective_date': '2026-09-01',
        },
    )
    online = str(tmp_path / 'online.ome.tif')
    with read_package(paths['x']) as x_pkg, read_package(paths['y']) as y_pkg:
        report, online_output = _export_and_check(
            online,
            [
                ExportItem(x_pkg, 'Image:1', [0]),
                ExportItem(y_pkg, 'Image:0', [0]),
            ],
            [
                np.take(paths['x1_pixels'], [0], axis=2),
                np.take(paths['y_pixels'], [0], axis=2),
            ],
        )
    assert report.conflicts == []
    online_groups = verify.calibration_versions(online_output)
    assert ('ARC:DET-SHARED', 'v2') in online_groups
    assert ('ARC:DET-SHARED', 'v3') in online_groups

    # simulate labs receiving separate exports, originals offline
    lab_x = str(tmp_path / 'lab_x.ome.tif')
    lab_y = str(tmp_path / 'lab_y.ome.tif')
    with read_package(paths['x']) as x_pkg:
        OmeExporter().export(lab_x, [ExportItem(x_pkg, 'Image:1', [0])])
    with read_package(paths['y']) as y_pkg:
        OmeExporter().export(lab_y, [ExportItem(y_pkg, 'Image:0', [0])])
    offline = str(tmp_path / 'offline.ome.tif')
    with read_package(lab_x) as x_lab, read_package(lab_y) as y_lab:
        _export_and_check(
            offline,
            [
                ExportItem(x_lab, list(x_lab.images)[0], [0]),
                ExportItem(y_lab, list(y_lab.images)[0], [0]),
            ],
            [
                np.take(paths['x1_pixels'], [0], axis=2),
                np.take(paths['y_pixels'], [0], axis=2),
            ],
        )
    offline_groups = verify.calibration_versions(verify.inspect(offline))
    assert offline_groups == online_groups


def test_size_c_tiffdata_plane_consistency(tmp_path):
    paths = build_sources(str(tmp_path / 'src'))
    target = str(tmp_path / 'consistency.ome.tif')
    with read_package(paths['x']) as x_pkg:
        _export_and_check(
            target,
            [ExportItem(x_pkg, 'Image:0', [2, 0])],
            [np.take(paths['x0_pixels'], [2, 0], axis=2)],
        )
    output = verify.inspect(target)
    image = output.images[0]
    assert image.size_c == 2
    assert len(image.channels) == 2
    # plane count matches T*Z*C and TiffData covers exactly those pages
    assert len(image.planes) == image.size_t * image.size_z * image.size_c
    verify.check_tiffdata(output)
    verify.check_references(output)
