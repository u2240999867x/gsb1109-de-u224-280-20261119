ome_export
==========

Mixed, channel-level OME-TIFF export built on top of :mod:`tifffile`.

The package reads OME-TIFF source packages (including multi-file packages
whose logical planes live in companion TIFFs referenced by
``TiffData/UUID`` elements) and writes new standalone OME-TIFF files from
selected Images and channels.

Identity model
--------------

Local OME IDs such as ``Image:0`` or ``Instrument:0`` are only meaningful
inside one package. Two different packages may use the same textual IDs for
unrelated objects. The exporter therefore never joins objects on local IDs
across packages.

Every object with an experimental identity carries a provenance
``XMLAnnotation`` (namespace ``urn:tifffile:ome-export:provenance``) holding:

* the original source file UUID and original local ID,
* an optional laboratory archive ID for instruments and detectors,
* detector calibration version, gain and effective date.

Matching across files uses provenance:

* objects shared inside one output (one instrument for several Images, one
  detector for several channels, one ROI for several Images) are emitted once
  and referenced through a single new local ID;
* same-named objects from different sources get separate new definitions;
* archived detector identities and calibration version numbers are stable
  across repeated exports and are never renumbered.

Detector calibrations
---------------------

A detector may have several calibration versions. Channels explicitly
reference the version used at acquisition, so one output can contain
multiple versions of the same archived detector.

If the same archived detector and calibration version arrive from two
inputs with different attribute values (for example different gains), the
exporter reports a :class:`~ome_export.export.CalibrationConflict` in the
:class:`~ome_export.export.ExportReport`, keeps both definitions, and never
silently overwrites either. Every Image remains independently readable.

Reference validation
--------------------

Before writing, retained channel detector settings, instrument, objective
and ROI references are checked against the definitions of their own
package. A missing definition raises
:class:`~ome_export.export.BrokenReference` naming the source file, Image,
channel and reference. Definitions from another package - even under an
identical textual ID - are never borrowed.

A broken reference on a channel that is not selected does not block the
export. On failure the target path is left untouched (output is written to a
temporary file and atomically renamed).

Pruning
-------

Only records reachable from retained Images and channels are written:
channels that are dropped, detectors used only by dropped channels,
calibrations referenced only by dropped channels, and unselected Images are
excluded. The report lists pruned records and the detector definitions that
can safely be removed.

Standalone output
-----------------

All pixels are copied into the new TIFF, ``TiffData`` entries point only at
internal pages, and the output contains no external UUID/FileName
references. Moving or deleting the sources and companions does not affect
the new file.

Public API
----------

* :func:`ome_export.read_package` - open a source package,
* :class:`ome_export.ExportItem` - one Image with the desired channel order,
* :class:`ome_export.OmeExporter` - validate, merge and write exports,
* :mod:`ome_export.verify` - reference, TiffData, pixel and relocation
  checks for generated files.

See ``examples/ome_mixed_export.py`` and ``tests/test_ome_export.py`` for
runnable coverage of the full scenario.
