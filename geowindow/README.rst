geowindow
=========

Area-weighted reads of small map-aligned windows from a set of GeoTIFF
sheets, with full provenance back to the source page, pyramid level,
pixel row/column and intersected area.

The package only depends on NumPy and the bundled ``tifffile``; polygon
clipping and area math are implemented in :mod:`geowindow.geometry`.

Conventions
-----------

* Map cells use the **pixel outer-corner** convention.  Pixel
  ``(col, row)`` spans raster coordinates ``(col, row)`` to
  ``(col + 1, row + 1)``.
* ``RasterPixelIsArea`` pages map outer corners directly.
  ``RasterPixelIsPoint`` pages locate pixel centers; they are converted to
  the outer-corner frame with a minus-half-pixel shift on parsing.
* Contact on a boundary only produces zero-area intersections and never
  masks or erases an effective reading.
* A zero reading is valid unless the page explicitly declares a NoData
  sentinel via the GDAL_NODATA tag.  Decoder-side defaults (for example
  tifffile's ``TiffPage.nodata`` defaulting to zero) are not declarations.

Modules
-------

``geowindow.geometry``
    Convex polygon clipping (Sutherland-Hodgman), area, footprint and
    polygon-difference helpers.
``geowindow.calibration``
    Parses ModelTransformation / ModelPixelScale / ModelTiepoint tags,
    normalizes both raster conventions, and checks every declared
    tie-point against the matrix.
``geowindow.raster``
    Loads pages and SubIFD pyramid levels; each level keeps its own
    calibration, data, NoData declaration and true footprint.
``geowindow.window``
    Per-cell area-weighted composition with caller-defined priority,
    NoData/out-of-footprint/missing-column fallback and per-piece
    provenance.
``geowindow.service``
    Loading, calibration/conflict reports, direct versus tiled reads,
    read-equality checks and standalone pixel reads.
``geowindow.scenario``
    Deterministic A/B/C/D test scenario (rotated sheets, overlap,
    slanted-edge contact, conflicting page, thumbnails, plain page).

Composition rules
-----------------

Sources are processed in priority order.  For each output cell, the
highest-priority effective coverage masks lower-priority coverage;
declared NoData pixels, missing thumbnail columns and out-of-footprint
regions fall through to the next level or page.  The cell value is the
area-weighted mean of the contributing pieces, and coverage is the total
effective area divided by the full cell area.

Pages whose tie-points contradict their own matrix are rejected from
composition (the contradiction, including the declared and
matrix-predicted coordinates, is reported) but remain readable pixel by
pixel.

Thumbnails are only selected for coarse output and only within the
footprint implied by their actual dimensions and calibration; a
thumbnail missing an outer column never gets upscaled, and the missing
region is filled from the page's base level or another available sheet.

Tests and demo
--------------

Run the tests (the rerunfailures plugin is disabled here because it needs
a local socket)::

    python -m pytest tests/test_geowindow.py -p no:rerunfailures

Run the end-to-end demo::

    PYTHONPATH=. python examples/geowindow_demo.py
