"""Mixed OME-TIFF export package.

Read OME-TIFF source packages (possibly multi-file through TiffData/UUID
companions), mix selected Images and channels from several sources into
standalone OME-TIFF files, and re-merge previously exported files without
access to the originals.

Local OME IDs are only meaningful inside one source. Every object that has a
laboratory archive identity keeps that identity (and detector calibration
versions) stable across exports; cross-file matching uses provenance
annotations rather than local IDs.
"""

from .export import (
    CalibrationConflict,
    BrokenReference,
    ExportItem,
    ExportReport,
    OmeExporter,
)
from .model import (
    Calibration,
    Channel,
    Detector,
    Image,
    Instrument,
    Objective,
    OmePackage,
    Plane,
    Roi,
    RoiShape,
    TiffDataInfo,
    read_package,
)

__all__ = [
    'Calibration',
    'CalibrationConflict',
    'BrokenReference',
    'Channel',
    'Detector',
    'ExportItem',
    'ExportReport',
    'Image',
    'Instrument',
    'Objective',
    'OmeExporter',
    'OmePackage',
    'Plane',
    'Roi',
    'RoiShape',
    'TiffDataInfo',
    'read_package',
]
