"""Area-weighted GeoTIFF window reads with per-pixel provenance."""

from __future__ import annotations

from .calibration import (
    Calibration,
    TiepointCheck,
    parse_calibration,
)
from .geometry import Polygon, area, intersect, rectangle
from .raster import GeoRaster, RasterLevel, load_georaster
from .window import CellResult, Contribution, WindowReader, WindowResult

__all__ = [
    'Calibration',
    'TiepointCheck',
    'parse_calibration',
    'GeoRaster',
    'RasterLevel',
    'load_georaster',
    'WindowReader',
    'WindowResult',
    'CellResult',
    'Contribution',
    'Polygon',
    'area',
    'intersect',
    'rectangle',
]
