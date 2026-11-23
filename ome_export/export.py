"""Mixed OME-TIFF export engine.

The exporter combines selected Images/channels from several
:class:`~ome_export.model.OmePackage` objects into one standalone OME-TIFF.

Identity rules:

* Local OME IDs are scoped to a single package. Objects are matched across
  packages/files through provenance annotations (original source UUID and
  original local ID) or, when the caller declared a laboratory archive
  identity, through that archive ID.
* Objects shared inside one output (same instrument used by several Images,
  one detector referenced by several channels, one ROI referenced by several
  Images) are emitted once and referenced by a single new local ID.
* Same-named objects from different sources get separate new definitions.
* Only records reachable from retained content are emitted. Records that
  become unreferenced when a channel or Image is dropped are pruned.
"""

from __future__ import annotations

import copy
import os
import tempfile
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from xml.etree import ElementTree as ET

import numpy as np
import tifffile

from .model import (
    NS_OME,
    NS_PROV,
    Image,
    OmePackage,
    Plane,
    Provenance,
    parse_provenance,
    qname,
)

ET.register_namespace('', NS_OME)
ET.register_namespace('exp', NS_PROV)

_OME_DTYPES = {
    'uint8': 'uint8',
    'uint16': 'uint16',
    'uint32': 'uint32',
    'int8': 'int8',
    'int16': 'int16',
    'int32': 'int32',
    'float32': 'float',
    'float64': 'double',
}


class BrokenReference(Exception):
    """A retained element references an object missing in its own package.

    The reference is never resolved against another package even when that
    package happens to contain a same-named object.
    """

    def __init__(
        self,
        source_path: str,
        image_id: str,
        channel_id: str | None,
        reference_kind: str,
        missing_id: str,
    ) -> None:
        self.source_path = source_path
        self.image_id = image_id
        self.channel_id = channel_id
        self.reference_kind = reference_kind
        self.missing_id = missing_id
        location = f'{os.path.basename(source_path)}:{image_id}'
        if channel_id is not None:
            location = f'{location}/{channel_id}'
        super().__init__(
            f'broken {reference_kind} reference {missing_id!r} in {location}; '
            f'the referenced object is not defined in {source_path!r} and '
            'must not be borrowed from another source package'
        )


@dataclass
class CalibrationConflict:
    """Same archived calibration version carrying different attribute values."""

    archive_id: str
    version: str
    attribute: str
    value_a: object
    value_b: object
    source_a: str
    source_b: str
    annotation_id_a: str
    annotation_id_b: str


@dataclass
class ExportItem:
    """One Image to export, with channels in the desired output order."""

    package: OmePackage
    image_id: str
    channel_order: list[int]
    name: str | None = None

    @property
    def image(self) -> Image:
        return self.package.images[self.image_id]


@dataclass
class PrunedRecord:
    kind: str
    source_id: str
    source_path: str
    reason: str


@dataclass
class ExportReport:
    conflicts: list[CalibrationConflict] = field(default_factory=list)
    pruned: list[PrunedRecord] = field(default_factory=list)
    images: list[str] = field(default_factory=list)
    removable_devices: list[str] = field(default_factory=list)

    def raise_on_conflict(self) -> None:
        if self.conflicts:
            conflict = self.conflicts[0]
            msg = (
                f'calibration {conflict.archive_id} version {conflict.version} '
                f'conflicts on {conflict.attribute}: '
                f'{conflict.value_a!r} in {conflict.source_a} vs '
                f'{conflict.value_b!r} in {conflict.source_b}'
            )
            raise CalibrationConflictError(msg, self.conflicts)


class CalibrationConflictError(Exception):
    def __init__(
        self, message: str, conflicts: list[CalibrationConflict]
    ) -> None:
        super().__init__(message)
        self.conflicts = conflicts


@dataclass
class _Carried:
    package: OmePackage
    kind: str
    source_id: str
    provenance: Provenance | None
    key: tuple


class _Registry:
    """Maps cross-file object identities to fresh local IDs in one output."""

    def __init__(self) -> None:
        self._keys: dict[tuple, str] = {}
        self.carried: list[_Carried] = []
        self.conflicts: list[CalibrationConflict] = []
        self._counters: dict[str, int] = {}
        # (detector archive ID, calibration version) -> first carried annotation
        self._calibration_versions: dict[tuple, _Carried] = {}
        # conflicting calibration base key -> origin of the winning (first) copy
        self._calibration_winner: dict[tuple, tuple] = {}

    def _new_id(self, kind: str) -> str:
        index = self._counters.get(kind, 0)
        self._counters[kind] = index + 1
        return f'{kind}:{index}'

    def identity(
        self, package: OmePackage, kind: str, source_id: str
    ) -> tuple:
        """Return the cross-file identity key of a source object."""
        provenance = self._provenance_of(package, kind, source_id)
        origin = (package.root_uuid, kind, source_id)
        if provenance is not None:
            if (
                kind == 'Annotation'
                and provenance.kind == 'Calibration'
                and provenance.archive_id
                and provenance.calibration_version
            ):
                # same archived detector + calibration version is one entity,
                # independent of which input file carries it
                base_key = (
                    'calibration',
                    provenance.archive_id,
                    provenance.calibration_version,
                )
                winner = self._calibration_winner.get(base_key)
                if winner is not None and winner != origin:
                    # conflicting inputs keep separate definitions
                    return ('calibration-conflict',) + origin
                return base_key
            if provenance.archive_id and kind in {'Instrument', 'Detector'}:
                return ('archive', kind, provenance.archive_id)
            return (
                provenance.source_uuid,
                kind,
                provenance.source_id,
            )
        return origin

    @staticmethod
    def _provenance_of(
        package: OmePackage, kind: str, source_id: str
    ) -> Provenance | None:
        if kind == 'Instrument':
            obj = package.instruments.get(source_id)
        elif kind == 'Detector' or kind == 'Objective':
            for instrument in package.instruments.values():
                container = (
                    instrument.detectors
                    if kind == 'Detector'
                    else instrument.objectives
                )
                if source_id in container:
                    return container[source_id].provenance
            return None
        elif kind == 'ROI':
            obj = package.rois.get(source_id)
        elif kind == 'Image':
            obj = package.images.get(source_id)
        elif kind == 'Channel':
            for image in package.images.values():
                for channel in image.channels:
                    if channel.id == source_id:
                        return channel.provenance
            return None
        elif kind == 'Annotation':
            element = package.annotations.get(source_id)
            return parse_provenance(element) if element is not None else None
        else:
            return None
        return getattr(obj, 'provenance', None)

    def register(
        self, package: OmePackage, kind: str, source_id: str
    ) -> str:
        key = self.identity(package, kind, source_id)
        provenance = self._provenance_of(package, kind, source_id)
        if (
            kind == 'Annotation'
            and provenance is not None
            and provenance.kind == 'Calibration'
            and provenance.archive_id
            and provenance.calibration_version
            and key[0] == 'calibration'
        ):
            base_key = key
            version_key = (
                provenance.archive_id,
                provenance.calibration_version,
            )
            previous = self._calibration_versions.get(version_key)
            if previous is not None:
                previous_provenance = parse_provenance(
                    previous.package.annotations[previous.source_id]
                )
                assert previous_provenance is not None
                if provenance.gain != previous_provenance.gain:
                    self._check_conflict(
                        package, kind, source_id, base_key
                    )
                    winner_origin = (
                        previous.package.root_uuid,
                        'Annotation',
                        previous.source_id,
                    )
                    self._calibration_winner[base_key] = winner_origin
                    # the conflict now gets its own identity and local ID
                    key = self.identity(package, kind, source_id)
                    assert key not in self._keys
        if key in self._keys:
            return self._keys[key]
        new_id = self._new_id(kind)
        self._keys[key] = new_id
        carried = _Carried(package, kind, source_id, provenance, key)
        self.carried.append(carried)
        if (
            kind == 'Annotation'
            and provenance is not None
            and provenance.kind == 'Calibration'
            and provenance.archive_id
            and provenance.calibration_version
            and key[0] == 'calibration'
        ):
            self._calibration_versions[
                (provenance.archive_id, provenance.calibration_version)
            ] = carried
        return new_id

    def lookup(self, package: OmePackage, kind: str, source_id: str) -> str:
        return self._keys[self.identity(package, kind, source_id)]

    def contains(self, package: OmePackage, kind: str, source_id: str) -> bool:
        return self.identity(package, kind, source_id) in self._keys

    def _check_conflict(
        self,
        package: OmePackage,
        kind: str,
        source_id: str,
        base_key: tuple,
    ) -> None:
        provenance = parse_provenance(package.annotations.get(source_id))
        if provenance is None or provenance.kind != 'Calibration':
            return
        previous = self._calibration_versions[
            (provenance.archive_id, provenance.calibration_version)
        ]
        previous_provenance = parse_provenance(
            previous.package.annotations[previous.source_id]
        )
        assert previous_provenance is not None
        if provenance.gain != previous_provenance.gain:
            self.conflicts.append(
                CalibrationConflict(
                    archive_id=provenance.archive_id or '',
                    version=provenance.calibration_version or '',
                    attribute='Gain',
                    value_a=previous_provenance.gain,
                    value_b=provenance.gain,
                    source_a=(
                        f'{os.path.basename(previous.package.path)}'
                        f'/{previous.source_id}'
                    ),
                    source_b=(
                        f'{os.path.basename(package.path)}/{source_id}'
                    ),
                    annotation_id_a=previous.source_id,
                    annotation_id_b=source_id,
                )
            )


class OmeExporter:
    """Build standalone OME-TIFF files from selected package content."""

    def export(
        self,
        target: str,
        items: list[ExportItem],
        *,
        strict_conflicts: bool = False,
        check_only: bool = False,
    ) -> ExportReport:
        """Write ``items`` to ``target``; never leave a partial file behind.

        With ``check_only``, run all reference validation and conflict
        detection but write nothing. The target path is never touched.
        """
        for item in items:
            self._validate_references(item)

        registry = _Registry()
        arrays: list[np.ndarray] = []
        image_specs: list[dict] = []
        report = ExportReport()

        for item in items:
            spec = self._register_item(registry, item)
            image_specs.append(spec)
            if not check_only:
                arrays.append(self._select_pixels(item))

        report.conflicts = list(registry.conflicts)
        if strict_conflicts and report.conflicts:
            report.raise_on_conflict()
        if check_only:
            return report

        pruned = self._collect_pruned(items, registry)
        report.pruned = pruned
        report.removable_devices = self._removable_devices(pruned)
        report.images = [spec['new_image_id'] for spec in image_specs]

        directory = os.path.dirname(os.path.abspath(target)) or '.'
        os.makedirs(directory, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix='.omeexport-', suffix='.tif', dir=directory
        )
        os.close(fd)
        try:
            root = self._build_xml(registry, image_specs, items)
            xml = ET.tostring(root, encoding='unicode')
            description = '<?xml version="1.0" encoding="UTF-8"?>' + xml
            self._write_pixels(tmp_path, description, image_specs, arrays)
            os.replace(tmp_path, target)
        except BaseException:
            with suppress(OSError):
                os.remove(tmp_path)
            raise
        return report

    # -- validation -------------------------------------------------------

    def _validate_references(self, item: ExportItem) -> None:
        package = item.package
        image = item.image
        if (
            image.instrument_ref is not None
            and image.instrument_ref not in package.instruments
        ):
            raise BrokenReference(
                package.path, image.id, None,
                'InstrumentRef', image.instrument_ref,
            )
        instrument = (
            package.instruments.get(image.instrument_ref)
            if image.instrument_ref
            else None
        )
        if (
            image.objective_settings_id is not None
            and (
                instrument is None
                or image.objective_settings_id
                not in instrument.objectives
            )
        ):
            raise BrokenReference(
                package.path, image.id, None,
                'ObjectiveSettings', image.objective_settings_id,
            )
        for roi_id in image.roi_refs:
            if roi_id not in package.rois:
                raise BrokenReference(
                    package.path, image.id, None, 'ROIRef', roi_id
                )
        for channel_index in item.channel_order:
            channel = image.channels[channel_index]
            self._require_annotations(package, image, channel.id, channel)
            if channel.detector_settings is None:
                continue
            detector_id = channel.detector_settings.detector_id
            if (
                instrument is None
                or detector_id not in instrument.detectors
            ):
                raise BrokenReference(
                    package.path,
                    image.id,
                    channel.id,
                    'DetectorSettings',
                    detector_id,
                )
            self._require_annotations(
                package, image, channel.id,
                instrument.detectors[detector_id],
            )
        self._require_annotations(package, image, None, image)
        for roi_id in image.roi_refs:
            self._require_annotations(
                package, image, None, package.rois[roi_id]
            )
        if instrument is not None:
            self._require_annotations(
                package, image, None, instrument
            )

    def _require_annotations(
        self,
        package: OmePackage,
        image: Image,
        channel_id: str | None,
        element: object,
    ) -> None:
        refs = getattr(element, 'annotation_refs', None)
        if refs is None:
            return
        for annotation_id in refs:
            if annotation_id not in package.annotations:
                raise BrokenReference(
                    package.path,
                    image.id,
                    channel_id,
                    'AnnotationRef',
                    annotation_id,
                )

    # -- registration / pruning ------------------------------------------

    def _register_item(
        self, registry: _Registry, item: ExportItem
    ) -> dict:
        package = item.package
        image = item.image
        new_image_id = registry.register(package, 'Image', image.id)
        self._register_annotation_refs(registry, package, image.annotation_refs)

        spec: dict = {
            'item': item,
            'new_image_id': new_image_id,
            'channels': [],
            'rois': [],
        }

        if image.instrument_ref is not None:
            new_instrument_id = registry.register(
                package, 'Instrument', image.instrument_ref
            )
            instrument = package.instruments[image.instrument_ref]
            self._register_annotation_refs(
                registry, package, instrument.annotation_refs
            )
            spec['instrument'] = (instrument, new_instrument_id)
            if image.objective_settings_id is not None:
                new_objective_id = registry.register(
                    package, 'Objective', image.objective_settings_id
                )
                objective = instrument.objectives[image.objective_settings_id]
                self._register_annotation_refs(
                    registry, package, objective.annotation_refs
                )
                spec['objective'] = (objective, new_objective_id)
        else:
            spec['instrument'] = None
            spec['objective'] = None

        for channel_index in item.channel_order:
            channel = image.channels[channel_index]
            new_channel_id = registry.register(package, 'Channel', channel.id)
            self._register_annotation_refs(
                registry, package, channel.annotation_refs
            )
            channel_spec = {
                'channel': channel,
                'new_id': new_channel_id,
                'new_index': len(spec['channels']),
                'detector': None,
            }
            if channel.detector_settings is not None:
                detector_id = channel.detector_settings.detector_id
                new_detector_id = registry.register(
                    package, 'Detector', detector_id
                )
                detector = package.instruments[
                    image.instrument_ref
                ].detectors[detector_id]
                self._register_annotation_refs(
                    registry, package,
                    self._non_calibration_refs(package, detector.annotation_refs),
                )
                channel_spec['detector'] = (detector, new_detector_id)
            spec['channels'].append(channel_spec)

        for roi_id in image.roi_refs:
            new_roi_id = registry.register(package, 'ROI', roi_id)
            roi = package.rois[roi_id]
            self._register_annotation_refs(
                registry, package, roi.annotation_refs
            )
            spec['rois'].append((roi, new_roi_id))
        return spec

    def _register_annotation_refs(
        self,
        registry: _Registry,
        package: OmePackage,
        refs: list[str],
    ) -> None:
        for annotation_id in refs:
            if annotation_id in package.annotations:
                registry.register(package, 'Annotation', annotation_id)

    @staticmethod
    def _non_calibration_refs(
        package: OmePackage, refs: list[str]
    ) -> list[str]:
        """Drop calibration refs; those are carried via channels only."""
        result = []
        for annotation_id in refs:
            element = package.annotations.get(annotation_id)
            if element is None:
                continue
            provenance = parse_provenance(element)
            if (
                provenance is not None
                and provenance.kind == 'Calibration'
            ):
                continue
            result.append(annotation_id)
        return result

    def _collect_pruned(
        self, items: list[ExportItem], registry: _Registry
    ) -> list[PrunedRecord]:
        pruned: list[PrunedRecord] = []
        seen: set[tuple] = set()
        selected_images = {
            (item.package.root_uuid, item.image.id) for item in items
        }
        for item in items:
            package = item.package
            image = item.image
            instrument = (
                package.instruments.get(image.instrument_ref)
                if image.instrument_ref
                else None
            )
            keep = set(item.channel_order)
            for index, channel in enumerate(image.channels):
                if index in keep:
                    continue
                key = (package.root_uuid, channel.id)
                if key in seen:
                    continue
                seen.add(key)
                pruned.append(
                    PrunedRecord(
                        'Channel', channel.id, package.path,
                        f'dropped from {image.id}',
                    )
                )
                if (
                    channel.detector_settings is not None
                    and instrument is not None
                ):
                    detector_id = channel.detector_settings.detector_id
                    if not self._detector_still_used(
                        registry, package, instrument.id, detector_id
                    ):
                        pruned.append(
                            PrunedRecord(
                                'Detector', detector_id, package.path,
                                f'only referenced by dropped {channel.id}',
                            )
                        )
        for package in {item.package for item in items}:
            for image_id, image in package.images.items():
                if (package.root_uuid, image_id) not in selected_images:
                    pruned.append(
                        PrunedRecord(
                            'Image', image_id, package.path,
                            'not selected for export',
                        )
                    )
        return pruned

    def _detector_still_used(
        self,
        registry: _Registry,
        package: OmePackage,
        instrument_id: str,
        detector_id: str,
    ) -> bool:
        return registry.contains(package, 'Detector', detector_id)

    def _removable_devices(self, pruned: list[PrunedRecord]) -> list[str]:
        return [
            f'{os.path.basename(record.source_path)}:{record.source_id}'
            for record in pruned
            if record.kind == 'Detector'
        ]

    #-- pixels ------------------------------------------------------------

    def _select_pixels(self, item: ExportItem) -> np.ndarray:
        """Return selected channels in canonical TZCYX order."""
        image = item.image
        array = item.package.read_array(image)
        series = item.package._tif.series[image.series_index]
        axes = series.axes
        target_axes = [ax for ax in 'TZCYX' if ax in axes]
        if 'C' not in axes:
            target_axes = [ax for ax in 'TZYX' if ax in axes]
        array = np.transpose(
            array, [axes.index(ax) for ax in target_axes]
        )
        if 'C' in target_axes:
            channel_axis = target_axes.index('C')
            array = np.take(array, item.channel_order, axis=channel_axis)
        else:
            # tifffile collapses a single-channel OME series to TZYX;
            # restore the C axis immediately before Y so order stays TZCYX
            channel_axis = target_axes.index('Y')
            array = np.expand_dims(array, axis=channel_axis)
        return np.ascontiguousarray(array)

    #-- XML ---------------------------------------------------------------

    def _build_xml(
        self,
        registry: _Registry,
        image_specs: list[dict],
        items: list[ExportItem],
    ) -> ET.Element:
        root = ET.Element(qname('OME'), {'UUID': _new_ome_uuid()})
        for carried in registry.carried:
            if carried.kind == 'Instrument':
                root.append(self._emit_instrument(registry, carried))
        ifd_cursor = 0
        for spec in image_specs:
            element, ifd_cursor = self._emit_image(
                registry, spec, ifd_cursor
            )
            root.append(element)
        for carried in registry.carried:
            if carried.kind == 'ROI':
                root.append(self._emit_roi(registry, carried))
        annotations = ET.SubElement(root, qname('StructuredAnnotations'))
        for carried in registry.carried:
            if carried.kind != 'Annotation':
                continue
            source_element = carried.package.annotations[carried.source_id]
            element = copy.deepcopy(source_element)
            element.set('ID', registry.lookup(
                carried.package, 'Annotation', carried.source_id
            ))
            annotations.append(element)
        return root

    def _emit_instrument(
        self, registry: _Registry, carried: _Carried
    ) -> ET.Element:
        package = carried.package
        instrument = package.instruments[carried.source_id]
        new_id = registry.lookup(package, 'Instrument', instrument.id)
        element = ET.Element(qname('Instrument'), {'ID': new_id})
        for detector_id, detector in instrument.detectors.items():
            if not registry.contains(package, 'Detector', detector_id):
                continue
            detector_element = ET.SubElement(
                element, qname('Detector'),
                self._detector_attributes(detector),
            )
            detector_element.set(
                'ID', registry.lookup(package, 'Detector', detector_id)
            )
            self._emit_annotation_refs(
                registry, package, detector_element,
                detector.annotation_refs,
            )
        for objective_id, objective in instrument.objectives.items():
            if not registry.contains(package, 'Objective', objective_id):
                continue
            objective_element = ET.SubElement(
                element, qname('Objective'),
                {'ID': registry.lookup(package, 'Objective', objective_id)},
            )
            if objective.nominal_magnification is not None:
                objective_element.set(
                    'NominalMagnification',
                    _format_number(objective.nominal_magnification),
                )
            if objective.model is not None:
                objective_element.set('Model', objective.model)
            self._emit_annotation_refs(
                registry, package, objective_element,
                objective.annotation_refs,
            )
        self._emit_annotation_refs(
            registry, package, element, instrument.annotation_refs
        )
        return element

    @staticmethod
    def _detector_attributes(detector) -> dict[str, str]:
        attributes: dict[str, str] = {}
        if detector.detector_type is not None:
            attributes['Type'] = detector.detector_type
        if detector.model is not None:
            attributes['Model'] = detector.model
        if detector.manufacturer is not None:
            attributes['Manufacturer'] = detector.manufacturer
        if detector.serial_number is not None:
            attributes['SerialNumber'] = detector.serial_number
        return attributes

    def _emit_roi(self, registry: _Registry, carried: _Carried) -> ET.Element:
        package = carried.package
        roi = package.rois[carried.source_id]
        attributes = {'ID': registry.lookup(package, 'ROI', roi.id)}
        if roi.name is not None:
            attributes['Name'] = roi.name
        element = ET.Element(qname('ROI'), attributes)
        union = ET.SubElement(element, qname('Union'))
        for shape in roi.shapes:
            shape_attributes = dict(shape.attributes)
            if shape.id:
                shape_attributes['ID'] = self._shape_id(registry, carried, shape.id)
            ET.SubElement(union, qname(shape.kind), shape_attributes)
        self._emit_annotation_refs(
            registry, package, element, roi.annotation_refs
        )
        return element

    def _shape_id(
        self, registry: _Registry, carried: _Carried, shape_id: str
    ) -> str:
        key = (carried.package.root_uuid, 'Shape', shape_id)
        existing = registry._keys.get(key)
        if existing is None:
            existing = registry._new_id('Shape')
            registry._keys[key] = existing
        return existing

    def _emit_image(
        self, registry: _Registry, spec: dict, ifd_cursor: int
    ) -> tuple[ET.Element, int]:
        item: ExportItem = spec['item']
        package = item.package
        image = item.image
        pixels = image.pixels
        image_attributes = {'ID': spec['new_image_id']}
        if item.name is not None:
            image_attributes['Name'] = item.name
        elif image.name is not None:
            image_attributes['Name'] = image.name
        element = ET.Element(qname('Image'), image_attributes)

        if spec['instrument'] is not None:
            _instrument, new_instrument_id = spec['instrument']
            ET.SubElement(
                element, qname('InstrumentRef'), {'ID': new_instrument_id}
            )
        if spec['objective'] is not None:
            _objective, new_objective_id = spec['objective']
            ET.SubElement(
                element,
                qname('ObjectiveSettings'),
                {'ID': new_objective_id},
            )

        pixels_attributes = {
            'ID': f'Pixels:{spec["new_image_id"].split(":")[-1]}',
            'DimensionOrder': 'XYCZT',
            'Type': _OME_DTYPES[np.dtype(
                package._tif.series[image.series_index].dtype
            ).name],
            'SizeX': str(pixels.size_x),
            'SizeY': str(pixels.size_y),
            'SizeZ': str(pixels.size_z),
            'SizeC': str(len(item.channel_order)),
            'SizeT': str(pixels.size_t),
        }
        for name, value in (
            ('PhysicalSizeX', pixels.physical_size_x),
            ('PhysicalSizeY', pixels.physical_size_y),
            ('PhysicalSizeZ', pixels.physical_size_z),
        ):
            if value is not None:
                pixels_attributes[name] = _format_number(value)
        pixels_element = ET.SubElement(element, qname('Pixels'), pixels_attributes)

        for channel_spec in spec['channels']:
            self._emit_channel(registry, package, image, pixels_element, channel_spec)

        planes_per_t = len(item.channel_order) * pixels.size_z
        for t_index in range(pixels.size_t):
            ET.SubElement(
                pixels_element,
                qname('TiffData'),
                {
                    'FirstT': str(t_index),
                    'FirstC': '0',
                    'FirstZ': '0',
                    'IFD': str(ifd_cursor + t_index * planes_per_t),
                    'PlaneCount': str(planes_per_t),
                },
            )
        ifd_cursor += planes_per_t * pixels.size_t

        remap_c = {
            source_channel: new_index
            for new_index, source_channel in enumerate(item.channel_order)
        }
        for plane in image.planes:
            if plane.the_c not in remap_c:
                continue
            self._emit_plane(pixels_element, plane, remap_c[plane.the_c])

        for _roi, new_roi_id in spec['rois']:
            ET.SubElement(element, qname('ROIRef'), {'ID': new_roi_id})
        self._emit_annotation_refs(
            registry, package, element, image.annotation_refs
        )
        return element, ifd_cursor

    def _emit_channel(
        self,
        registry: _Registry,
        package: OmePackage,
        image: Image,
        pixels_element: ET.Element,
        channel_spec: dict,
    ) -> None:
        channel: Channel = channel_spec['channel']
        attributes = {
            'ID': channel_spec['new_id'],
            'SamplesPerPixel': str(channel.samples_per_pixel),
        }
        if channel.name is not None:
            attributes['Name'] = channel.name
        if channel.color is not None:
            attributes['Color'] = f'#{channel.color:06X}'
        if channel.emission_wavelength is not None:
            attributes['EmissionWavelength'] = _format_number(
                channel.emission_wavelength
            )
        if channel.excitation_wavelength is not None:
            attributes['ExcitationWavelength'] = _format_number(
                channel.excitation_wavelength
            )
        if channel.fluor is not None:
            attributes['Fluor'] = channel.fluor
        element = ET.SubElement(pixels_element, qname('Channel'), attributes)
        if channel.detector_settings is not None:
            _detector, new_detector_id = channel_spec['detector']
            settings_attributes = {'ID': new_detector_id}
            settings = channel.detector_settings
            if settings.gain is not None:
                settings_attributes['Gain'] = _format_number(settings.gain)
            if settings.offset is not None:
                settings_attributes['Offset'] = _format_number(settings.offset)
            if settings.binning is not None:
                settings_attributes['Binning'] = settings.binning
            if settings.readout_rate is not None:
                settings_attributes['ReadOutRate'] = _format_number(
                    settings.readout_rate
                )
            if settings.voltage is not None:
                settings_attributes['Voltage'] = _format_number(settings.voltage)
            ET.SubElement(
                element, qname('DetectorSettings'), settings_attributes
            )
        self._emit_annotation_refs(
            registry, package, element, channel.annotation_refs
        )

    def _emit_plane(
        self, parent: ET.Element, plane: Plane, new_c: int
    ) -> None:
        attributes = {
            'TheZ': str(plane.the_z),
            'TheC': str(new_c),
            'TheT': str(plane.the_t),
        }
        if plane.delta_t is not None:
            attributes['DeltaT'] = _format_number(plane.delta_t)
        if plane.exposure_time is not None:
            attributes['ExposureTime'] = _format_number(plane.exposure_time)
        if plane.position_x is not None:
            attributes['PositionX'] = _format_number(plane.position_x)
        if plane.position_y is not None:
            attributes['PositionY'] = _format_number(plane.position_y)
        if plane.position_z is not None:
            attributes['PositionZ'] = _format_number(plane.position_z)
        ET.SubElement(parent, qname('Plane'), attributes)

    def _emit_annotation_refs(
        self,
        registry: _Registry,
        package: OmePackage,
        parent: ET.Element,
        refs: list[str],
    ) -> None:
        for annotation_id in refs:
            if not registry.contains(package, 'Annotation', annotation_id):
                continue
            new_id = registry.lookup(package, 'Annotation', annotation_id)
            ET.SubElement(parent, qname('AnnotationRef'), {'ID': new_id})

    #-- TIFF writing ------------------------------------------------------

    def _write_pixels(
        self,
        path: str,
        description: str,
        image_specs: list[dict],
        arrays: list[np.ndarray],
    ) -> None:
        with tifffile.TiffWriter(path) as writer:
            for index, (spec, array) in enumerate(
                zip(image_specs, arrays, strict=True)
            ):
                pixels = spec['item'].image.pixels
                page_array = array.reshape(-1, pixels.size_y, pixels.size_x)
                writer.write(
                    page_array,
                    photometric='minisblack',
                    metadata=None,
                    description=description if index == 0 else None,
                )


def _format_number(value: float) -> str:
    if float(value).is_integer():
        return f'{value:.1f}'
    return repr(float(value))


def _new_ome_uuid() -> str:
    return f'urn:uuid:{uuid.uuid4()}'
