"""Verification helpers for generated OME-TIFF exports.

These checks do not rely on the OME XML schema (which may be unavailable
offline). Instead they parse the written OME-XML, walk every internal
reference, compare TiffData against the actual TIFF pages, and verify pixels
against expected arrays.
"""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass
from xml.etree import ElementTree as ET

import numpy as np
import tifffile

from .model import NS_PROV, localname, qname


@dataclass
class OutputImage:
    index: int
    image_id: str
    name: str | None
    dimension_order: str
    size_c: int
    size_t: int
    size_z: int
    size_y: int
    size_x: int
    channels: list[ET.Element]
    planes: list[ET.Element]
    tiffdata: list[ET.Element]
    instrument_ref: str | None
    roi_refs: list[str]


@dataclass
class OutputFile:
    path: str
    root: ET.Element
    images: list[OutputImage]
    instruments: dict[str, ET.Element]
    rois: dict[str, ET.Element]
    annotations: dict[str, ET.Element]
    provenance: dict[str, dict]

    def annotation_provenance(self, annotation_id: str) -> dict | None:
        element = self.annotations.get(annotation_id)
        if element is None:
            return None
        marker = element.find(f'{qname("Value")}/{{{NS_PROV}}}Provenance')
        if marker is None:
            return None
        return dict(marker.attrib)


def inspect(path: str) -> OutputFile:
    """Parse an exported OME-TIFF and index its elements."""
    with tifffile.TiffFile(path) as tif:
        assert tif.is_ome
        omexml = tif.ome_metadata
    assert omexml is not None
    root = ET.fromstring(omexml)
    instruments = {
        element.attrib['ID']: element
        for element in root.findall(qname('Instrument'))
    }
    rois = {
        element.attrib['ID']: element
        for element in root.findall(qname('ROI'))
    }
    annotations = {}
    container = root.find(qname('StructuredAnnotations'))
    if container is not None:
        for element in container:
            annotations[element.attrib['ID']] = element
    images: list[OutputImage] = []
    for index, element in enumerate(root.findall(qname('Image'))):
        pixels = element.find(qname('Pixels'))
        assert pixels is not None
        instrument_ref_element = element.find(qname('InstrumentRef'))
        images.append(
            OutputImage(
                index=index,
                image_id=element.attrib['ID'],
                name=element.attrib.get('Name'),
                dimension_order=pixels.attrib['DimensionOrder'],
                size_c=int(pixels.attrib['SizeC']),
                size_t=int(pixels.attrib['SizeT']),
                size_z=int(pixels.attrib['SizeZ']),
                size_y=int(pixels.attrib['SizeY']),
                size_x=int(pixels.attrib['SizeX']),
                channels=pixels.findall(qname('Channel')),
                planes=pixels.findall(qname('Plane')),
                tiffdata=pixels.findall(qname('TiffData')),
                instrument_ref=(
                    instrument_ref_element.attrib['ID']
                    if instrument_ref_element is not None
                    else None
                ),
                roi_refs=[
                    ref.attrib['ID']
                    for ref in element.findall(qname('ROIRef'))
                ],
            )
        )
    provenance = {}
    for annotation_id, element in annotations.items():
        marker = element.find(
            f'{qname("Value")}/{{{NS_PROV}}}Provenance'
        )
        if marker is not None:
            provenance[annotation_id] = dict(marker.attrib)
    return OutputFile(
        path=path,
        root=root,
        images=images,
        instruments=instruments,
        rois=rois,
        annotations=annotations,
        provenance=provenance,
    )


def check_references(output: OutputFile) -> None:
    """Every ID reference resolves to exactly one definition in this file."""
    defined = set(output.instruments) | set(output.rois) | set(output.annotations)
    for image in output.images:
        defined.add(image.image_id)
        for channel in image.channels:
            defined.add(channel.attrib['ID'])
    for instrument in output.instruments.values():
        for detector in instrument.findall(qname('Detector')):
            defined.add(detector.attrib['ID'])
        for objective in instrument.findall(qname('Objective')):
            defined.add(objective.attrib['ID'])

    # OME IDs must be unique document-wide
    raw_ids: list[str] = []
    raw_ids.extend(output.instruments)
    raw_ids.extend(output.rois)
    raw_ids.extend(output.annotations)
    raw_ids.extend(image.image_id for image in output.images)
    raw_ids.extend(
        channel.attrib['ID']
        for image in output.images
        for channel in image.channels
    )
    for instrument in output.instruments.values():
        raw_ids.extend(
            detector.attrib['ID']
            for detector in instrument.findall(qname('Detector'))
        )
        raw_ids.extend(
            objective.attrib['ID']
            for objective in instrument.findall(qname('Objective'))
        )
    assert len(raw_ids) == len(set(raw_ids)), 'duplicate OME IDs in output'

    def require(ref_id: str, *, location: str) -> None:
        assert ref_id in defined, f'unresolved {ref_id} at {location}'

    for image in output.images:
        if image.instrument_ref is not None:
            require(image.instrument_ref, location=image.image_id)
        for roi_id in image.roi_refs:
            require(roi_id, location=image.image_id)
        for channel in image.channels:
            for ref in channel.findall(qname('AnnotationRef')):
                require(ref.attrib['ID'], location=channel.attrib['ID'])
            settings = channel.find(qname('DetectorSettings'))
            if settings is not None:
                require(settings.attrib['ID'], location=channel.attrib['ID'])
    # element-level annotation references anywhere in the tree
    for ref in output.root.iter(qname('AnnotationRef')):
        require(ref.attrib['ID'], location='tree')
    for settings in output.root.iter(qname('ObjectiveSettings')):
        require(settings.attrib['ID'], location='ObjectiveSettings')


def check_element_ordering(output: OutputFile) -> None:
    """Assert child element order follows the OME 2016-06 schema sequence."""
    def ranks(parent: ET.Element, order: tuple[str, ...]) -> None:
        seen = -1
        for child in parent:
            name = localname(child.tag)
            if name not in order:
                continue
            rank = order.index(name)
            assert rank >= seen, f'{name} out of order in {parent.tag}'
            seen = rank

    for instrument in output.instruments.values():
        ranks(
            instrument,
            ('Detector', 'Objective', 'AnnotationRef'),
        )
        for detector in instrument.findall(qname('Detector')):
            ranks(detector, ('AnnotationRef',))
    for image in output.images:
        image_element = next(
            element
            for element in output.root.findall(qname('Image'))
            if element.attrib['ID'] == image.image_id
        )
        ranks(
            image_element,
            (
                'InstrumentRef', 'ObjectiveSettings', 'Pixels',
                'ROIRef', 'AnnotationRef',
            ),
        )
        pixels = image_element.find(qname('Pixels'))
        assert pixels is not None
        ranks(pixels, ('Channel', 'TiffData', 'Plane'))
        for channel in pixels.findall(qname('Channel')):
            ranks(channel, ('DetectorSettings', 'AnnotationRef'))
    for roi in output.rois.values():
        ranks(roi, ('Union', 'AnnotationRef'))


def check_tiffdata(output: OutputFile) -> None:
    """TiffData IFD/PlaneCount covers every page exactly once."""
    with tifffile.TiffFile(output.path) as tif:
        page_count = len(tif.pages)
        series_shapes = [series.shape for series in tif.series]
    covered: list[int] = []
    for image in output.images:
        expected_planes = image.size_t * image.size_z * image.size_c
        block_total = 0
        for data in image.tiffdata:
            uuid = data.find(qname('UUID'))
            # standalone files must not point at external companions
            assert uuid is None, 'export must be standalone (no UUID TiffData)'
            ifd = int(data.attrib['IFD'])
            count = int(data.attrib.get('PlaneCount', 1))
            block_total += count
            covered.extend(range(ifd, ifd + count))
        assert block_total == expected_planes, (
            f'{image.image_id}: TiffData {block_total} != {expected_planes}'
        )
        assert image.size_c == len(image.channels)
    assert sorted(covered) == list(range(page_count)), (
        'TiffData does not partition the TIFF pages'
    )
    assert len(series_shapes) == len(output.images)


def check_pixels(
    output: OutputFile, expected: list[np.ndarray]
) -> None:
    """Read back every series and compare against expected TZCYX arrays."""
    with tifffile.TiffFile(output.path) as tif:
        assert len(tif.series) == len(expected)
        for series, expected_array in zip(tif.series, expected, strict=True):
            actual = series.asarray()
            # tifffile collapses a single-channel OME series to TZYX
            if actual.shape != expected_array.shape:
                assert actual.size == expected_array.size
                actual = actual.reshape(expected_array.shape)
            assert actual.dtype == expected_array.dtype
            np.testing.assert_array_equal(actual, expected_array)


def check_standalone(path: str) -> str:
    """Copy an export into an empty directory; it must still read fully."""
    target_dir = os.path.join(
        os.path.dirname(path),
        'relocated_' + os.path.basename(os.path.dirname(path)),
    )
    os.makedirs(target_dir, exist_ok=True)
    target = os.path.join(target_dir, os.path.basename(path))
    shutil.copy2(path, target)
    with tifffile.TiffFile(target) as tif:
        assert tif.is_ome
        for series in tif.series:
            series.asarray()
    return target


def channel_detector(output: OutputFile, image: OutputImage, channel_index: int):
    channel = image.channels[channel_index]
    settings = channel.find(qname('DetectorSettings'))
    return None if settings is None else settings.attrib['ID']


def detector_archive_id(
    output: OutputFile, detector_id: str
) -> str | None:
    for instrument in output.instruments.values():
        for detector in instrument.findall(qname('Detector')):
            if detector.attrib['ID'] != detector_id:
                continue
            for ref in detector.findall(qname('AnnotationRef')):
                provenance = output.annotation_provenance(ref.attrib['ID'])
                if provenance and provenance.get('archiveID'):
                    return provenance['archiveID']
    return None


def calibration_versions(output: OutputFile) -> dict[tuple, list[dict]]:
    """Group calibration annotations by (archive ID, version)."""
    grouped: dict[tuple, list[dict]] = {}
    for provenance in output.provenance.values():
        if provenance.get('kind') != 'Calibration':
            continue
        key = (
            provenance.get('archiveID'),
            provenance.get('calibrationVersion'),
        )
        grouped.setdefault(key, []).append(provenance)
    return grouped


def instrument_objectives(output: OutputFile) -> dict[str, dict]:
    """Map objective local ID to its instrument and magnification."""
    result = {}
    for instrument in output.instruments.values():
        for objective in instrument.findall(qname('Objective')):
            result[objective.attrib['ID']] = {
                'instrument': instrument.attrib['ID'],
                'magnification': objective.attrib.get('NominalMagnification'),
                'model': objective.attrib.get('Model'),
            }
    return result


def roi_geometry(output: OutputFile, roi_id: str) -> dict[str, str]:
    roi = output.rois[roi_id]
    union = roi.find(qname('Union'))
    assert union is not None and len(union) == 1
    return dict(union[0].attrib)


def plane_exposures(output: OutputFile, image: OutputImage) -> dict:
    result = {}
    for plane in image.planes:
        key = (
            int(plane.attrib['TheT']),
            int(plane.attrib['TheZ']),
            int(plane.attrib['TheC']),
        )
        result[key] = (
            float(plane.attrib['ExposureTime'])
            if 'ExposureTime' in plane.attrib
            else None
        )
    return result


def element_ids(output: OutputFile) -> set[str]:
    """All local IDs defined in the output."""
    ids = set(output.instruments) | set(output.rois) | set(output.annotations)
    for image in output.images:
        ids.add(image.image_id)
        ids.update(channel.attrib['ID'] for channel in image.channels)
    for instrument in output.instruments.values():
        ids.update(
            detector.attrib['ID']
            for detector in instrument.findall(qname('Detector'))
        )
        ids.update(
            objective.attrib['ID']
            for objective in instrument.findall(qname('Objective'))
        )
    return ids


def check_well_formed_xml(path: str) -> None:
    with tifffile.TiffFile(path) as tif:
        ET.fromstring(tif.ome_metadata)
