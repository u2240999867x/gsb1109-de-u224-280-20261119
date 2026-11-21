Track archives
==============

Rebuilt positions are observations, not a guaranteed continuous trajectory.
Reception gaps, contact-only messages, and later revisions can all change which
positions may be connected. The :class:`~pyopensky.track_archive.TrackArchive`
class turns reconstructed positions into segmented tracks and produces the
changes a downstream archive needs.

Segmentation
------------

Positions are grouped per aircraft and per second. Reports received from
several stations in the same second are coalesced into one temporal position
while all station provenance is kept. A position requires a finite latitude and
longitude; a record with only a signal reception remains a quality observation
and never contributes coordinates.

A segment connects consecutive positions that are at most ``max_gap`` apart.
The threshold is inclusive: with ``max_gap=10``, positions at seconds 0 and 10
belong to one segment, while a missing position at second 10 separates 0 from
20 into two segments.

.. code-block:: python

    from pyopensky.track_archive import TrackArchive

    archive = TrackArchive(max_gap=10)
    result = archive.write(
        [
            {"icao24": "abc123", "time": 0, "lat": 50.0, "lon": 4.0},
            {"icao24": "abc123", "time": 10, "valid": False},
            {"icao24": "abc123", "time": 20, "lat": 50.2, "lon": 4.2},
            {"icao24": "abc123", "time": 30, "lat": 50.3, "lon": 4.3},
        ],
        batch="initial",
    )

    # Two segments for abc123: seconds 0 and 20-30.
    for segment in result.segments:
        print(segment.segment_id, [point.time for point in segment.points])

Segments have a stable identifier derived from the aircraft and the first
position time. Each :class:`~pyopensky.track_archive.TrackPoint` exposes the
actual reports that contributed to it through ``observations``, including the
station, revision, and processing batch.

Batches and revisions
---------------------

:meth:`~pyopensky.track_archive.TrackArchive.write` appends a processing batch
and returns an :class:`~pyopensky.track_archive.ArchiveBatchResult`. The result
contains only changes that a downstream copy of the archive can apply:

- ``add`` for a new segment
- ``retract`` for a segment that no longer exists
- ``replace`` for a segment whose positions changed

The same result also carries the observations responsible for the change.

.. code-block:: python

    downstream = result.apply({})

    late = archive.write(
        [
            {
                "icao24": "abc123",
                "time": 10,
                "lat": 50.1,
                "lon": 4.1,
                "source": "s1",
                "revision": 1,
            },
            {
                "icao24": "abc123",
                "time": 10,
                "lat": 50.1,
                "lon": 4.1,
                "source": "s2",
                "revision": 1,
            },
        ],
        batch="late",
    )
    downstream = late.apply(downstream)

    # The two station reports produced one position at second 10, which
    # connects 0 and 20-30 into a single segment.
    assert len(archive.segments) == 1
    assert archive.segments[0].points[1].source_count == 2

The highest revision per ``(aircraft, second, station)`` is effective, with
ties resolved by the most recently written batch. A later contact-only revision
from every reporting station therefore withdraws the coordinates, while a
contact-only revision from only one station does not erase another station's
independent fix.

Applying changes validates the downstream copy. Adding an existing segment,
or retracting or replacing a segment that no longer matches, raises
:class:`~pyopensky.track_archive.ArchiveStateError` instead of silently
diverging.

Replay and recomputation
------------------------

Batches can be withdrawn and restored. Both operations return ordinary changes,
so a downstream archive stays synchronized through the same ``apply`` method.
After any sequence of writes, withdrawals, and restores, the downstream state
matches a full recomputation from the effective observations:

.. code-block:: python

    archive.retract_batch("late")
    archive.restore_batch("late")
    assert downstream == archive.state
    assert archive.recompute() == archive.segments

:meth:`~pyopensky.track_archive.TrackArchive.from_rebuild` accepts a frame
returned by :class:`~pyopensky.rebuild.Rebuild` directly, using its
``timestamp`` column and expanding ``sensors`` provenance.

.. note::

    Quality observations are reported through ``archive.contacts``. They keep
    their actual observation time and processing batch but no coordinates are
    invented for them.

.. autoclass:: pyopensky.track_archive.TrackArchive
    :members:

.. autoclass:: pyopensky.track_archive.ArchiveBatchResult
    :members:

.. autoclass:: pyopensky.track_archive.ArchiveChange
    :members:

.. autoclass:: pyopensky.track_archive.TrackSegment
    :members:

.. autoclass:: pyopensky.track_archive.TrackPoint
    :members:

.. autoclass:: pyopensky.track_archive.Observation
    :members:
