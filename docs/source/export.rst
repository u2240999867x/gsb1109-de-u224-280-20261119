Segmented export
----------------

Large Trino result sets can be streamed into independent, readable Parquet
segments instead of a single in-memory frame. The exporter drives the cursor
with controllable ``fetchmany`` calls, persists every confirmed segment to
disk and keeps a JSON manifest that makes an interrupted export resumable.

Recovery is bounded by an upstream, caller-verified snapshot identifier and a
unique row ordering key. A confirmed segment is only reused when its checksum
matches the manifest; a damaged segment invalidates itself and every later
segment, which are re-read from the same snapshot. Data from a different
snapshot can never be appended to an existing manifest, even when the SQL text
and bind parameters are identical. Segment bytes are written to a temporary
file and atomically renamed only after verification, so interrupted segments
never become visible to readers.

.. autoclass:: pyopensky.export.SegmentedExporter
    :members:
    :no-undoc-members:
    :show-inheritance:

.. autoclass:: pyopensky.export.ExportManifest
    :members:
    :no-undoc-members:

.. autoclass:: pyopensky.export.SegmentManifest
    :members:
    :no-undoc-members:

.. autoexception:: pyopensky.export.ExportError

.. autoexception:: pyopensky.export.SnapshotMismatchError

.. autoexception:: pyopensky.export.ResumeRejectedError

.. autoexception:: pyopensky.export.OrderKeyNotUniqueError
