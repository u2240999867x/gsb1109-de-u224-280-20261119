"""In-memory model and reader for OME-TIFF source packages.

A package is the set of TIFF files referenced by the OME-XML of one root
file. Logical planes may live in companion TIFFs through ``TiffData/UUID``
elements. Local OME IDs (``Image:0``, ``Instrument:0`` ...) are scoped to a
single package and are never assumed to match identifiers of the same name in
another package.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import numpy as np
import tifffile

NS_OME = 'http://www.openmicroscopy.org/Schemas/OME/2016-06'
NS_PROV = 'urn:tifffile:ome-export:provenance'


def qname(tag: str) -> str:
    """Return namespace qualified OME tag name."""
    return f'{{{NS_OME}}}{tag}'


def localname(tag: str) -> str:
    """Return local name of a possibly namespace qualified tag."""
    return tag.rsplit('}', 1)[-1]


def qprov(tag: str) -> str:
    """Return namespace qualified provenance tag name."""
    return f'{{{NS_PROV}}}{tag}'


def attr_float(element: ET.Element, name: str) -> float | None:
    value = element.attrib.get(name)
    if value is None:
        return None
    return float(value)


@dataclass
class TiffDataInfo:
    """One TiffData element describing where logical planes are stored."""

    first_t: int = 0
    first_c: int = 0
    first_z: int = 0
    ifd: int = 0
    plane_count: int | None = None
    uuid: str | None = None
    filename: str | None = None


@dataclass
class Plane:
    """One OME Plane record.

    ``exposure_time`` is ``None`` when the source does not declare an
    exposure; an explicit zero exposure is stored as ``0.0``.
    """

    the_z: int
    the_c: int
    the_t: int
    exposure_time: float | None = None
    delta_t: float | None = None
    position_x: float | None = None
    position_y: float | None = None
    position_z: float | None = None


@dataclass
class DetectorSettings:
    """DetectorSettings element attached to a channel or image."""

    detector_id: str
    gain: float | None = None
    offset: float | None = None
    binning: str | None = None
    readout_rate: float | None = None
    voltage: float | None = None


@dataclass
class Channel:
    """One Pixels/Channel element."""

    id: str
    index: int
    name: str | None = None
    samples_per_pixel: int = 1
    color: int | None = None
    emission_wavelength: float | None = None
    excitation_wavelength: float | None = None
    fluor: str | None = None
    detector_settings: DetectorSettings | None = None
    annotation_refs: list[str] = field(default_factory=list)
    provenance: Provenance | None = None


@dataclass
class Detector:
    id: str
    detector_type: str | None = None
    model: str | None = None
    manufacturer: str | None = None
    serial_number: str | None = None
    annotation_refs: list[str] = field(default_factory=list)
    provenance: Provenance | None = None


@dataclass
class Objective:
    id: str
    nominal_magnification: float | None = None
    model: str | None = None
    annotation_refs: list[str] = field(default_factory=list)
    provenance: Provenance | None = None


@dataclass
class Instrument:
    id: str
    detectors: dict[str, Detector] = field(default_factory=dict)
    objectives: dict[str, Objective] = field(default_factory=dict)
    annotation_refs: list[str] = field(default_factory=list)
    provenance: Provenance | None = None


@dataclass
class RoiShape:
    id: str
    kind: str
    attributes: dict[str, str]


@dataclass
class Roi:
    id: str
    name: str | None
    shapes: list[RoiShape] = field(default_factory=list)
    annotation_refs: list[str] = field(default_factory=list)
    provenance: Provenance | None = None


@dataclass
class Pixels:
    id: str
    dimension_order: str
    pixel_type: str
    size_x: int
    size_y: int
    size_z: int
    size_c: int
    size_t: int
    physical_size_x: float | None = None
    physical_size_y: float | None = None
    physical_size_z: float | None = None


@dataclass
class Image:
    id: str
    name: str | None
    series_index: int
    pixels: Pixels
    instrument_ref: str | None = None
    objective_settings_id: str | None = None
    channels: list[Channel] = field(default_factory=list)
    planes: list[Plane] = field(default_factory=list)
    tiffdata: list[TiffDataInfo] = field(default_factory=list)
    roi_refs: list[str] = field(default_factory=list)
    annotation_refs: list[str] = field(default_factory=list)
    provenance: Provenance | None = None


@dataclass
class Provenance:
    """Cross-file comparable source information carried by an export."""

    kind: str
    source_uuid: str
    source_id: str
    archive_id: str | None = None
    calibration_version: str | None = None
    gain: float | None = None
    effective_date: str | None = None


@dataclass
class Calibration:
    """One archived detector calibration version."""

    annotation_id: str
    detector_archive_id: str
    version: str
    gain: float
    effective_date: str


class OmePackage:
    """Parsed OME-TIFF package with pixel access through tifffile."""

    def __init__(
        self,
        path: str,
        root_uuid: str | None,
        instruments: dict[str, Instrument],
        rois: dict[str, Roi],
        images: dict[str, Image],
        annotations: dict[str, ET.Element],
        calibrations: dict[str, list[Calibration]],
        tif: tifffile.TiffFile,
    ) -> None:
        self.path = path
        self.root_uuid = root_uuid
        self.instruments = instruments
        self.rois = rois
        self.images = images
        self.annotations = annotations
        # detector local ID -> calibration versions declared in this package
        self.calibrations = calibrations
        self._tif = tif
        self._arrays: dict[str, np.ndarray] = {}

    @property
    def directory(self) -> str:
        return os.path.dirname(os.path.abspath(self.path))

    def close(self) -> None:
        self._tif.close()

    def __enter__(self) -> OmePackage:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def read_array(self, image: Image) -> np.ndarray:
        """Return full pixel array in series axis order (reverse DimOrder)."""
        if image.id not in self._arrays:
            series = self._tif.series[image.series_index]
            self._arrays[image.id] = series.asarray()
        return self._arrays[image.id]

    def calibration_for_channel(
        self, channel: Channel
    ) -> Calibration | None:
        """Return the calibration annotation referenced by a channel."""
        for ref in channel.annotation_refs:
            for calibration_list in self.calibrations.values():
                for calibration in calibration_list:
                    if calibration.annotation_id == ref:
                        return calibration
        return None

    def provenance_annotations(self) -> dict[str, Provenance]:
        """Return annotation ID -> provenance for export annotations."""
        result: dict[str, Provenance] = {}
        for annotation_id, element in self.annotations.items():
            provenance = parse_provenance(element)
            if provenance is not None:
                result[annotation_id] = provenance
        return result


def read_package(path: str) -> OmePackage:
    """Open and parse an OME-TIFF root file and its companions."""
    tif = tifffile.TiffFile(path)
    if tif.ome_metadata is None:
        tif.close()
        msg = f'{path!r} does not contain OME-XML metadata'
        raise ValueError(msg)
    root = ET.fromstring(tif.ome_metadata)
    root_uuid = root.attrib.get('UUID')

    annotations = _read_annotations(root)
    instruments = _read_instruments(root)
    rois = _read_rois(root)
    images = _read_images(root)
    calibrations = _read_calibrations(annotations)
    _attach_provenance(annotations, instruments, rois, images)

    return OmePackage(
        path=os.path.abspath(path),
        root_uuid=root_uuid,
        instruments=instruments,
        rois=rois,
        images=images,
        annotations=annotations,
        calibrations=calibrations,
        tif=tif,
    )


def _read_annotations(root: ET.Element) -> dict[str, ET.Element]:
    annotations: dict[str, ET.Element] = {}
    container = root.find(qname('StructuredAnnotations'))
    if container is None:
        return annotations
    for element in container:
        annotation_id = element.attrib.get('ID')
        if annotation_id is not None:
            annotations[annotation_id] = element
    return annotations


def _read_instruments(root: ET.Element) -> dict[str, Instrument]:
    instruments: dict[str, Instrument] = {}
    for element in root.findall(qname('Instrument')):
        instrument = Instrument(id=element.attrib['ID'])
        instrument.annotation_refs = _read_refs(element, 'AnnotationRef')
        for detector_element in element.findall(qname('Detector')):
            detector = Detector(
                id=detector_element.attrib['ID'],
                detector_type=detector_element.attrib.get('Type'),
                model=detector_element.attrib.get('Model'),
                manufacturer=detector_element.attrib.get('Manufacturer'),
                serial_number=detector_element.attrib.get('SerialNumber'),
                annotation_refs=_read_refs(detector_element, 'AnnotationRef'),
            )
            instrument.detectors[detector.id] = detector
        for objective_element in element.findall(qname('Objective')):
            objective = Objective(
                id=objective_element.attrib['ID'],
                nominal_magnification=attr_float(
                    objective_element, 'NominalMagnification'
                ),
                model=objective_element.attrib.get('Model'),
                annotation_refs=_read_refs(objective_element, 'AnnotationRef'),
            )
            instrument.objectives[objective.id] = objective
        instruments[instrument.id] = instrument
    return instruments


def _read_rois(root: ET.Element) -> dict[str, Roi]:
    rois: dict[str, Roi] = {}
    for element in root.findall(qname('ROI')):
        roi = Roi(
            id=element.attrib['ID'],
            name=element.attrib.get('Name'),
            annotation_refs=_read_refs(element, 'AnnotationRef'),
        )
        for shape_element in element:
            if localname(shape_element.tag) in {'Union', 'AnnotationRef'}:
                continue
            roi.shapes.append(
                RoiShape(
                    id=shape_element.attrib.get('ID', ''),
                    kind=localname(shape_element.tag),
                    attributes=dict(shape_element.attrib),
                )
            )
        for union in element.findall(qname('Union')):
            for shape_element in union:
                roi.shapes.append(
                    RoiShape(
                        id=shape_element.attrib.get('ID', ''),
                        kind=localname(shape_element.tag),
                        attributes=dict(shape_element.attrib),
                    )
                )
        rois[roi.id] = roi
    return rois


def _read_images(root: ET.Element) -> dict[str, Image]:
    images: dict[str, Image] = {}
    for series_index, image_element in enumerate(root.findall(qname('Image'))):
        pixels_element = image_element.find(qname('Pixels'))
        if pixels_element is None:
            continue
        pixels = Pixels(
            id=pixels_element.attrib['ID'],
            dimension_order=pixels_element.attrib['DimensionOrder'],
            pixel_type=pixels_element.attrib['Type'],
            size_x=int(pixels_element.attrib['SizeX']),
            size_y=int(pixels_element.attrib['SizeY']),
            size_z=int(pixels_element.attrib.get('SizeZ', 1)),
            size_c=int(pixels_element.attrib.get('SizeC', 1)),
            size_t=int(pixels_element.attrib.get('SizeT', 1)),
            physical_size_x=attr_float(pixels_element, 'PhysicalSizeX'),
            physical_size_y=attr_float(pixels_element, 'PhysicalSizeY'),
            physical_size_z=attr_float(pixels_element, 'PhysicalSizeZ'),
        )
        image = Image(
            id=image_element.attrib['ID'],
            name=image_element.attrib.get('Name'),
            series_index=series_index,
            pixels=pixels,
            instrument_ref=_read_ref(
                image_element.find(qname('InstrumentRef'))
            ),
            roi_refs=_read_refs(image_element, 'ROIRef'),
            annotation_refs=_read_refs(image_element, 'AnnotationRef'),
        )
        objective_settings = image_element.find(qname('ObjectiveSettings'))
        if objective_settings is not None:
            image.objective_settings_id = objective_settings.attrib.get('ID')

        for channel_index, channel_element in enumerate(
            pixels_element.findall(qname('Channel'))
        ):
            channel = Channel(
                id=channel_element.attrib['ID'],
                index=channel_index,
                name=channel_element.attrib.get('Name'),
                samples_per_pixel=int(
                    channel_element.attrib.get('SamplesPerPixel', 1)
                ),
                color=_read_color(channel_element.attrib.get('Color')),
                emission_wavelength=attr_float(
                    channel_element, 'EmissionWavelength'
                ),
                excitation_wavelength=attr_float(
                    channel_element, 'ExcitationWavelength'
                ),
                fluor=channel_element.attrib.get('Fluor'),
                annotation_refs=_read_refs(channel_element, 'AnnotationRef'),
            )
            settings_element = channel_element.find(qname('DetectorSettings'))
            if settings_element is not None:
                channel.detector_settings = _read_detector_settings(
                    settings_element
                )
            image.channels.append(channel)

        for plane_element in pixels_element.findall(qname('Plane')):
            image.planes.append(
                Plane(
                    the_z=int(plane_element.attrib.get('TheZ', 0)),
                    the_c=int(plane_element.attrib.get('TheC', 0)),
                    the_t=int(plane_element.attrib.get('TheT', 0)),
                    exposure_time=attr_float(
                        plane_element, 'ExposureTime'
                    ),
                    delta_t=attr_float(plane_element, 'DeltaT'),
                    position_x=attr_float(plane_element, 'PositionX'),
                    position_y=attr_float(plane_element, 'PositionY'),
                    position_z=attr_float(plane_element, 'PositionZ'),
                )
            )

        for data_element in pixels_element.findall(qname('TiffData')):
            uuid_element = data_element.find(qname('UUID'))
            plane_count = data_element.attrib.get('PlaneCount')
            if plane_count is None:
                plane_count = data_element.attrib.get('NumPlanes')
            image.tiffdata.append(
                TiffDataInfo(
                    first_t=int(data_element.attrib.get('FirstT', 0)),
                    first_c=int(data_element.attrib.get('FirstC', 0)),
                    first_z=int(data_element.attrib.get('FirstZ', 0)),
                    ifd=int(data_element.attrib.get('IFD', 0)),
                    plane_count=int(plane_count) if plane_count else None,
                    uuid=uuid_element.text if uuid_element is not None else None,
                    filename=(
                        uuid_element.attrib.get('FileName')
                        if uuid_element is not None
                        else None
                    ),
                )
            )
        images[image.id] = image
    return images


def _read_detector_settings(element: ET.Element) -> DetectorSettings:
    return DetectorSettings(
        detector_id=element.attrib['ID'],
        gain=attr_float(element, 'Gain'),
        offset=attr_float(element, 'Offset'),
        binning=element.attrib.get('Binning'),
        readout_rate=attr_float(element, 'ReadOutRate'),
        voltage=attr_float(element, 'Voltage'),
    )


def _read_ref(element: ET.Element | None) -> str | None:
    if element is None:
        return None
    return element.attrib.get('ID')


def _read_refs(element: ET.Element, tag: str) -> list[str]:
    return [
        child.attrib['ID']
        for child in element.findall(qname(tag))
        if child.attrib.get('ID')
    ]


def _read_color(value: str | None) -> int | None:
    if value is None:
        return None
    value = value.strip()
    if value.startswith('#'):
        return int(value[1:], 16)
    return int(value)


def _read_calibrations(
    annotations: dict[str, ET.Element],
) -> dict[str, list[Calibration]]:
    calibrations: dict[str, list[Calibration]] = {}
    for annotation_id, element in annotations.items():
        provenance = parse_provenance(element)
        if (
            provenance is None
            or provenance.kind != 'Calibration'
            or provenance.calibration_version is None
            or provenance.gain is None
        ):
            continue
        calibration = Calibration(
            annotation_id=annotation_id,
            detector_archive_id=provenance.archive_id or '',
            version=provenance.calibration_version,
            gain=provenance.gain,
            effective_date=provenance.effective_date or '',
        )
        calibrations.setdefault(provenance.source_id, []).append(calibration)
    return calibrations


def parse_provenance(element: ET.Element) -> Provenance | None:
    """Parse a provenance XMLAnnotation element, if it is one."""
    if localname(element.tag) != 'XMLAnnotation':
        return None
    if element.attrib.get('Namespace') != NS_PROV:
        return None
    marker = element.find(f'{qname("Value")}/{qprov("Provenance")}')
    if marker is None:
        return None
    attributes = marker.attrib
    gain = attributes.get('gain')
    return Provenance(
        kind=attributes.get('kind', ''),
        source_uuid=attributes.get('sourceUUID', ''),
        source_id=attributes.get('sourceID', ''),
        archive_id=attributes.get('archiveID'),
        calibration_version=attributes.get('calibrationVersion'),
        gain=float(gain) if gain is not None else None,
        effective_date=attributes.get('effectiveDate'),
    )


def _attach_provenance(
    annotations: dict[str, ET.Element],
    instruments: dict[str, Instrument],
    rois: dict[str, Roi],
    images: dict[str, Image],
) -> None:
    provenance_by_id: dict[str, Provenance] = {}
    for annotation_id, element in annotations.items():
        provenance = parse_provenance(element)
        if provenance is not None:
            provenance_by_id[annotation_id] = provenance

    def lookup(refs: list[str], *, kind: str) -> Provenance | None:
        for ref in refs:
            provenance = provenance_by_id.get(ref)
            if provenance is not None and provenance.kind == kind:
                return provenance
        return None

    for instrument in instruments.values():
        instrument.provenance = lookup(
            instrument.annotation_refs, kind='Instrument'
        )
        for detector in instrument.detectors.values():
            detector.provenance = lookup(
                detector.annotation_refs, kind='Detector'
            )
        for objective in instrument.objectives.values():
            objective.provenance = lookup(
                objective.annotation_refs, kind='Objective'
            )
    for roi in rois.values():
        roi.provenance = lookup(roi.annotation_refs, kind='ROI')
    for image in images.values():
        image.provenance = lookup(image.annotation_refs, kind='Image')
        for channel in image.channels:
            channel.provenance = lookup(
                channel.annotation_refs, kind='Channel'
            )
