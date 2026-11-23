"""Build the X and Y OME-TIFF source packages used by the scenario tests.

The packages intentionally share the same local OME IDs
(``Instrument:0``, ``Detector:0``, ``ROI:0`` ...) while differing in
acquisition attributes, shapes and positions. Some logical planes live in
companion TIFFs through TiffData/UUID elements, and the physical IFD order
does not match the XML element order.
"""

from __future__ import annotations

import os
from xml.etree import ElementTree as ET

import numpy as np
import tifffile

from .model import NS_OME, NS_PROV, qname, qprov

ET.register_namespace('', NS_OME)
ET.register_namespace('exp', NS_PROV)


def _provenance_element(
    annotation_id: str,
    *,
    kind: str,
    source_uuid: str,
    source_id: str,
    archive_id: str | None = None,
    calibration_version: str | None = None,
    gain: float | None = None,
    effective_date: str | None = None,
) -> ET.Element:
    annotation = ET.Element(
        qname('XMLAnnotation'),
        {'ID': annotation_id, 'Namespace': NS_PROV},
    )
    value = ET.SubElement(annotation, qname('Value'))
    provenance = ET.SubElement(
        value, qprov('Provenance'),
        {
            'kind': kind,
            'sourceUUID': source_uuid,
            'sourceID': source_id,
        },
    )
    if archive_id is not None:
        provenance.set('archiveID', archive_id)
    if calibration_version is not None:
        provenance.set('calibrationVersion', calibration_version)
    if gain is not None:
        provenance.set('gain', repr(gain))
    if effective_date is not None:
        provenance.set('effectiveDate', effective_date)
    return annotation


class SourceBuilder:
    """Collect OME elements and pixel blocks for one source package."""

    def __init__(self, root_uuid: str) -> None:
        self.root_uuid = root_uuid
        self.annotations: list[ET.Element] = []
        self.instruments: list[ET.Element] = []
        self.rois: list[ET.Element] = []
        self.images: list[ET.Element] = []
        self._annotation_counter = 0

    def annotation_id(self) -> str:
        annotation_id = f'Annotation:{self._annotation_counter}'
        self._annotation_counter += 1
        return annotation_id

    def provenance(
        self,
        kind: str,
        source_id: str,
        *,
        archive_id: str | None = None,
        calibration_version: str | None = None,
        gain: float | None = None,
        effective_date: str | None = None,
    ) -> str:
        annotation_id = self.annotation_id()
        self.annotations.append(
            _provenance_element(
                annotation_id,
                kind=kind,
                source_uuid=self.root_uuid,
                source_id=source_id,
                archive_id=archive_id,
                calibration_version=calibration_version,
                gain=gain,
                effective_date=effective_date,
            )
        )
        return annotation_id

    def calibration(
        self,
        source_detector_id: str,
        archive_id: str,
        version: str,
        gain: float,
        effective_date: str,
    ) -> str:
        return self.provenance(
            'Calibration',
            source_detector_id,
            archive_id=archive_id,
            calibration_version=version,
            gain=gain,
            effective_date=effective_date,
        )

    def to_xml(self) -> str:
        root = ET.Element(
            qname('OME'),
            {
                'UUID': f'urn:uuid:{self.root_uuid}',
                'Creator': 'ome_export fixture builder',
            },
        )
        for element in self.instruments:
            root.append(element)
        for element in self.rois:
            root.append(element)
        for element in self.images:
            root.append(element)
        structured = ET.SubElement(root, qname('StructuredAnnotations'))
        for element in self.annotations:
            structured.append(element)
        return (
            '<?xml version="1.0" encoding="UTF-8"?>'
            + ET.tostring(root, encoding='unicode')
        )


def _instrument(
    builder: SourceBuilder,
    instrument_id: str,
    *,
    archive_id: str,
    detectors: list[dict],
    objective: dict,
) -> ET.Element:
    instrument = ET.Element(qname('Instrument'), {'ID': instrument_id})
    instrument_annotation = builder.provenance(
        'Instrument', instrument_id, archive_id=archive_id
    )
    for detector in detectors:
        element = ET.SubElement(
            instrument, qname('Detector'),
            {
                'ID': detector['id'],
                'Type': detector.get('type', 'CCD'),
                'Model': detector['model'],
                'Manufacturer': detector['manufacturer'],
            },
        )
        if detector.get('archive_id'):
            ref = builder.provenance(
                'Detector',
                detector['id'],
                archive_id=detector['archive_id'],
            )
            ET.SubElement(element, qname('AnnotationRef'), {'ID': ref})
        for calibration in detector.get('calibrations', []):
            ref = builder.calibration(
                detector['id'],
                calibration['archive_id'],
                calibration['version'],
                calibration['gain'],
                calibration['effective_date'],
            )
            ET.SubElement(element, qname('AnnotationRef'), {'ID': ref})
    objective_element = ET.SubElement(
        instrument, qname('Objective'),
        {
            'ID': objective['id'],
            'Model': objective['model'],
            'NominalMagnification': repr(objective['magnification']),
        },
    )
    ET.SubElement(
        objective_element, qname('AnnotationRef'),
        {'ID': builder.provenance('Objective', objective['id'])},
    )
    ET.SubElement(
        instrument, qname('AnnotationRef'),
        {'ID': instrument_annotation},
    )
    builder.instruments.append(instrument)
    return instrument


def _roi(
    builder: SourceBuilder,
    roi_id: str,
    *,
    name: str,
    x: float,
    y: float,
    width: float,
    height: float,
    archive_id: str | None = None,
) -> ET.Element:
    roi = ET.Element(qname('ROI'), {'ID': roi_id, 'Name': name})
    union = ET.SubElement(roi, qname('Union'))
    ET.SubElement(
        union, qname('Rectangle'),
        {
            'ID': f'{roi_id}:Shape:0',
            'X': repr(x),
            'Y': repr(y),
            'Width': repr(width),
            'Height': repr(height),
        },
    )
    ref = builder.provenance(
        'ROI', roi_id, archive_id=archive_id
    )
    ET.SubElement(roi, qname('AnnotationRef'), {'ID': ref})
    builder.rois.append(roi)
    return roi


def _image(
    builder: SourceBuilder,
    *,
    image_id: str,
    name: str,
    size: tuple[int, int, int, int, int],
    pixel_type: str,
    instrument_ref: str,
    objective_id: str,
    channels: list[dict],
    planes: list[dict],
    tiffdata: list[dict],
    roi_refs: list[str],
    physical: tuple[float, float],
) -> ET.Element:
    size_t, size_z, size_c, size_y, size_x = size
    image = ET.Element(qname('Image'), {'ID': image_id, 'Name': name})
    image_annotation = builder.provenance('Image', image_id)
    ET.SubElement(
        image, qname('InstrumentRef'), {'ID': instrument_ref}
    )
    ET.SubElement(
        image, qname('ObjectiveSettings'), {'ID': objective_id}
    )
    pixels = ET.SubElement(
        image, qname('Pixels'),
        {
            'ID': f'Pixels:{image_id.split(":")[-1]}',
            'DimensionOrder': 'XYCZT',
            'Type': pixel_type,
            'SizeX': str(size_x),
            'SizeY': str(size_y),
            'SizeZ': str(size_z),
            'SizeC': str(size_c),
            'SizeT': str(size_t),
            'PhysicalSizeX': repr(physical[0]),
            'PhysicalSizeY': repr(physical[1]),
        },
    )
    for channel in channels:
        channel_element = ET.SubElement(
            pixels, qname('Channel'),
            {
                'ID': channel['id'],
                'Name': channel['name'],
                'SamplesPerPixel': '1',
                'Color': channel['color'],
            },
        )
        settings = channel.get('detector_settings')
        if settings is not None:
            settings_element = ET.SubElement(
                channel_element, qname('DetectorSettings'),
                {'ID': settings['detector_id']},
            )
            if settings.get('gain') is not None:
                settings_element.set('Gain', repr(settings['gain']))
            if settings.get('binning'):
                settings_element.set('Binning', settings['binning'])
        if channel.get('calibration_ref'):
            ET.SubElement(
                channel_element, qname('AnnotationRef'),
                {'ID': channel['calibration_ref']},
            )
        channel_annotation = builder.provenance('Channel', channel['id'])
        ET.SubElement(
            channel_element, qname('AnnotationRef'),
            {'ID': channel_annotation},
        )
    # XML TiffData order is deliberately independent of physical IFD order
    for data in tiffdata:
        attributes = {
            'FirstT': str(data['first_t']),
            'FirstC': '0',
            'FirstZ': '0',
            'IFD': str(data['ifd']),
            'PlaneCount': str(data['plane_count']),
        }
        data_element = ET.SubElement(pixels, qname('TiffData'), attributes)
        if data.get('uuid'):
            uuid_element = ET.SubElement(
                data_element, qname('UUID'),
                {'FileName': data['filename']},
            )
            uuid_element.text = f'urn:uuid:{data["uuid"]}'
    for plane in planes:
        attributes = {
            'TheZ': str(plane['z']),
            'TheC': str(plane['c']),
            'TheT': str(plane['t']),
        }
        if plane.get('exposure') is not None:
            attributes['ExposureTime'] = repr(plane['exposure'])
        if plane.get('delta_t') is not None:
            attributes['DeltaT'] = repr(plane['delta_t'])
        if plane.get('position') is not None:
            px, py, pz = plane['position']
            attributes['PositionX'] = repr(px)
            attributes['PositionY'] = repr(py)
            attributes['PositionZ'] = repr(pz)
        ET.SubElement(pixels, qname('Plane'), attributes)
    for roi_id in roi_refs:
        ET.SubElement(image, qname('ROIRef'), {'ID': roi_id})
    ET.SubElement(image, qname('AnnotationRef'), {'ID': image_annotation})
    builder.images.append(image)
    return image


def pixel_block(
    marker: int, size_t: int, size_z: int, size_c: int, size_y: int,
    size_x: int,
) -> np.ndarray:
    """Distinct, reproducible pixels for one source image (TZCYX)."""
    total = size_t * size_z * size_c * size_y * size_x
    base = np.arange(total, dtype=np.uint16).reshape(
        size_t, size_z, size_c, size_y, size_x
    )
    return (base + marker * 1000).astype(np.uint16)


def build_sources(
    directory: str,
    *,
    shared_calibration_override: tuple[str, float, str] | None = None,
    broken_detector: dict | None = None,
    y_shared_calibration: dict | None = None,
) -> dict[str, str]:
    """Create X (two Images, companion TIFF) and Y (one Image)."""
    os.makedirs(directory, exist_ok=True)

    if shared_calibration_override is not None:
        override_version, override_gain, override_date = (
            shared_calibration_override
        )
    else:
        override_version = override_gain = override_date = None

    # ---- X --------------------------------------------------------------
    x_uuid = 'x-source-uuid'
    x = SourceBuilder(x_uuid)
    _instrument(
        x, 'Instrument:0',
        archive_id='ARC:INSTR-X',
        detectors=[
            {
                'id': 'Detector:0',
                'model': 'X-Cam Shared',
                'manufacturer': 'LabX Optics',
                'archive_id': 'ARC:DET-SHARED',
                'calibrations': [
                    {
                        'archive_id': 'ARC:DET-SHARED',
                        'version': 'v1',
                        'gain': 1.5,
                        'effective_date': '2026-01-01',
                    },
                    {
                        'archive_id': 'ARC:DET-SHARED',
                        'version': 'v2',
                        'gain': (
                            override_gain
                            if override_version == 'v2'
                            else 1.8
                        ),
                        'effective_date': (
                            override_date
                            if override_version == 'v2'
                            else '2026-06-01'
                        ),
                    },
                ],
            },
            {
                'id': 'Detector:1',
                'model': 'X-Cam Exclusive',
                'manufacturer': 'LabX Optics',
                'archive_id': 'ARC:DET-EXCLUSIVE',
            },
        ],
        objective={
            'id': 'Objective:0',
            'model': 'X 40x',
            'magnification': 40.0,
        },
    )
    _roi(
        x, 'ROI:0', name='X shared ROI',
        x=10.0, y=20.0, width=100.0, height=50.0,
        archive_id='ARC:ROI-X',
    )

    x0_shape = (2, 2, 3, 8, 6)  # T, Z, C, Y, X
    x0_pixels = pixel_block(11, *x0_shape)
    # companion holds T=1 block; root file physically holds T=0 block
    x0_companion = os.path.join(directory, 'x0_companion.tif')
    with tifffile.TiffWriter(x0_companion) as writer:
        writer.write(
            x0_pixels[1].reshape(-1, 8, 6),
            photometric='minisblack',
            metadata=None,
        )
    cal_v1 = next(
        annotation.attrib['ID']
        for annotation in x.annotations
        if annotation.find(
            f'{qname("Value")}/{qprov("Provenance")}'
        ) is not None
        and annotation.find(
            f'{qname("Value")}/{qprov("Provenance")}'
        ).attrib.get('calibrationVersion') == 'v1'
    )
    cal_v2 = next(
        annotation.attrib['ID']
        for annotation in x.annotations
        if annotation.find(
            f'{qname("Value")}/{qprov("Provenance")}'
        ) is not None
        and annotation.find(
            f'{qname("Value")}/{qprov("Provenance")}'
        ).attrib.get('calibrationVersion') == 'v2'
    )
    x0_channels = [
        {
            'id': 'Channel:0', 'name': 'X0-DAPI', 'color': '#0000FF',
            'calibration_ref': cal_v1,
            'detector_settings': {
                'detector_id': 'Detector:0', 'gain': 1.5, 'binning': '2x2',
            },
        },
        {
            'id': 'Channel:1', 'name': 'X0-GFP', 'color': '#00FF00',
            'calibration_ref': cal_v2,
            'detector_settings': {
                'detector_id': 'Detector:0', 'gain': 1.8, 'binning': '2x2',
            },
        },
        {
            'id': 'Channel:2', 'name': 'X0-mCherry', 'color': '#FF0000',
            'detector_settings': {
                'detector_id': 'Detector:1', 'gain': 2.0, 'binning': '1x1',
            },
        },
    ]
    x0_planes = _plane_records(x0_shape, exposure_pattern='mixed')
    _image(
        x,
        image_id='Image:0', name='X image 0',
        size=x0_shape, pixel_type='uint16',
        instrument_ref='Instrument:0', objective_id='Objective:0',
        channels=x0_channels, planes=x0_planes,
        tiffdata=[
            # XML lists T=1 (companion) first though physically written later
            {
                'first_t': 1, 'ifd': 0, 'plane_count': 6,
                'uuid': 'x0-companion-uuid',
                'filename': 'x0_companion.tif',
            },
            {'first_t': 0, 'ifd': 0, 'plane_count': 6},
        ],
        roi_refs=['ROI:0'], physical=(0.11, 0.11),
    )

    x1_shape = (2, 3, 2, 8, 6)
    x1_pixels = pixel_block(22, *x1_shape)
    x1_channels = [
        {
            'id': 'Channel:3', 'name': 'X1-DAPI', 'color': '#0000FF',
            'calibration_ref': cal_v2,
            'detector_settings': {
                'detector_id': 'Detector:0', 'gain': 1.8, 'binning': '2x2',
            },
        },
        {
            'id': 'Channel:4', 'name': 'X1-GFP', 'color': '#00FF00',
            'detector_settings': {
                'detector_id': 'Detector:0', 'binning': '2x2',
            },
        },
    ]
    x1_planes = _plane_records(x1_shape, exposure_pattern='zero')
    _image(
        x,
        image_id='Image:1', name='X image 1',
        size=x1_shape, pixel_type='uint16',
        instrument_ref='Instrument:0', objective_id='Objective:0',
        channels=x1_channels, planes=x1_planes,
        tiffdata=[
            # X0 occupies root pages 0..5 (its T=0 block); X1 starts at IFD 6
            {'first_t': 0, 'ifd': 6, 'plane_count': 12},
        ],
        roi_refs=['ROI:0'], physical=(0.11, 0.11),
    )

    x_path = os.path.join(directory, 'x.ome.tif')
    # physical page order: X0 T=0 block, then all X1 pages
    x0_physical = x0_pixels[0].reshape(-1, 8, 6)
    x1_physical = x1_pixels.reshape(-1, 8, 6)

    # ---- Y: same textual IDs, different attributes ----------------------
    y_uuid = 'y-source-uuid'
    y = SourceBuilder(y_uuid)
    y_shape = (2, 2, 2, 5, 4)
    y_pixels = pixel_block(33, *y_shape)
    y_detector0: dict = {
        'id': 'Detector:0',
        'model': 'Y-Cam',
        'manufacturer': 'LabY Imaging',
        'archive_id': 'ARC:DET-Y',
    }
    if y_shared_calibration is not None:
        y_detector0['calibrations'] = [
            {
                'archive_id': y_shared_calibration['archive_id'],
                'version': y_shared_calibration['version'],
                'gain': y_shared_calibration['gain'],
                'effective_date': y_shared_calibration['effective_date'],
            }
        ]
    _instrument(
        y, 'Instrument:0',
        archive_id='ARC:INSTR-Y',
        detectors=[
            y_detector0,
            {
                # defined in Y but never referenced by a Y channel; an X
                # channel may be broken to point at this same textual ID,
                # which must not be borrowed across packages
                'id': 'Detector:2',
                'model': 'Y spare camera',
                'manufacturer': 'LabY Imaging',
                'archive_id': 'ARC:DET-Y-SPARE',
            },
        ],
        objective={
            'id': 'Objective:0',
            'model': 'Y 60x',
            'magnification': 60.0,
        },
    )
    _roi(
        y, 'ROI:0', name='Y ROI same local id',
        x=30.0, y=40.0, width=25.0, height=75.0,
        archive_id='ARC:ROI-Y',
    )
    y_channels = [
        {
            'id': 'Channel:0', 'name': 'Y-DAPI', 'color': '#0000FF',
            'detector_settings': {
                'detector_id': 'Detector:0', 'gain': 3.5, 'binning': '4x4',
            },
        },
        {
            'id': 'Channel:1', 'name': 'Y-FITC', 'color': '#00FF00',
            'detector_settings': {
                'detector_id': 'Detector:0', 'gain': 3.5, 'binning': '4x4',
            },
        },
    ]
    if y_shared_calibration is not None:
        y_cal_ref = _find_calibration_id(
            y,
            y_shared_calibration['archive_id'],
            y_shared_calibration['version'],
        )
        y_channels[0]['calibration_ref'] = y_cal_ref
    y_planes = _plane_records(y_shape, exposure_pattern='position_only')
    _image(
        y,
        image_id='Image:0', name='Y only image',
        size=y_shape, pixel_type='uint16',
        instrument_ref='Instrument:0', objective_id='Objective:0',
        channels=y_channels, planes=y_planes,
        tiffdata=[
            {'first_t': 0, 'ifd': 0, 'plane_count': 8},
        ],
        roi_refs=['ROI:0'], physical=(0.25, 0.25),
    )

    if broken_detector is not None:
        _break_detector_reference(x, broken_detector)
        y_instrument = y.instruments[0]
        y_detector_ids = [
            element.attrib.get('ID')
            for element in y_instrument.findall(qname('Detector'))
        ]
        assert broken_detector['missing_id'] in y_detector_ids

    with tifffile.TiffWriter(x_path) as writer:
        writer.write(
            x0_physical,
            photometric='minisblack',
            metadata=None,
            description=x.to_xml(),
        )
        writer.write(
            x1_physical,
            photometric='minisblack',
            metadata=None,
        )

    y_path = os.path.join(directory, 'y.ome.tif')
    with tifffile.TiffWriter(y_path) as writer:
        writer.write(
            y_pixels.reshape(-1, 5, 4),
            photometric='minisblack',
            metadata=None,
            description=y.to_xml(),
        )

    return {
        'x': x_path,
        'y': y_path,
        'x0_companion': x0_companion,
        'x0_pixels': x0_pixels,
        'x1_pixels': x1_pixels,
        'y_pixels': y_pixels,
    }


def _find_calibration_id(
    builder: SourceBuilder, archive_id: str, version: str
) -> str:
    for element in builder.annotations:
        marker = element.find(f'{qname("Value")}/{qprov("Provenance")}')
        if marker is None:
            continue
        attributes = marker.attrib
        if (
            attributes.get('kind') == 'Calibration'
            and attributes.get('archiveID') == archive_id
            and attributes.get('calibrationVersion') == version
        ):
            return element.attrib['ID']
    msg = f'calibration {archive_id}/{version} not found'
    raise KeyError(msg)


def _break_detector_reference(
    x_builder: SourceBuilder,
    spec: dict,
) -> None:
    """Point one X channel's DetectorSettings at a missing local detector.

    The same textual detector ID exists in Y; it must never be borrowed.
    """
    target_image = x_builder.images[spec['image_index']]
    pixels = target_image.find(qname('Pixels'))
    channels = pixels.findall(qname('Channel'))
    channel = channels[spec['channel_index']]
    settings = channel.find(qname('DetectorSettings'))
    assert settings is not None
    instrument = x_builder.instruments[0]
    local_ids = [
        element.attrib.get('ID')
        for element in instrument.findall(qname('Detector'))
    ]
    assert spec['missing_id'] not in local_ids
    settings.set('ID', spec['missing_id'])


def _plane_records(
    shape: tuple[int, int, int, int, int], *, exposure_pattern: str
) -> list[dict]:
    size_t, size_z, size_c, _size_y, _size_x = shape
    planes: list[dict] = []
    for t_index in range(size_t):
        for z_index in range(size_z):
            for c_index in range(size_c):
                plane = {'t': t_index, 'z': z_index, 'c': c_index}
                if exposure_pattern == 'mixed':
                    # some planes position only, one explicit zero exposure,
                    # remaining planes carry a real exposure
                    if z_index == 0 and c_index == 0:
                        plane['position'] = (1.0 + t_index, 2.0, 0.5 * z_index)
                    elif t_index == 1 and z_index == 1 and c_index == 1:
                        plane['exposure'] = 0.0
                        plane['delta_t'] = 12.5
                    else:
                        plane['exposure'] = 10.0 + c_index
                        plane['delta_t'] = 5.0 * t_index
                elif exposure_pattern == 'zero':
                    plane['exposure'] = 0.0
                    plane['position'] = (0.0, 0.0, 0.25 * z_index)
                elif exposure_pattern == 'position_only':
                    plane['position'] = (
                        3.0 + t_index, 4.0 + z_index, 0.1 * z_index
                    )
                planes.append(plane)
    return planes
