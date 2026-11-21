Auditable archive deliveries
============================

``pyopensky.archive`` reconciles reception reports into a deterministic
coordinate delivery. It is designed for archives where the same report is seen
in more than one UTC slice, revisions correct earlier coordinates, and two
receivers can disagree about the same aircraft and observation time.

The module does not contact OpenSky. It accepts frames returned by
``Trino.history()``, or pages recorded locally, so an extraction can be replayed
and checked without another query.

Half-open UTC slices
--------------------

Every history request uses a half-open UTC interval::

    start <= time < stop

``utc_slices()`` cuts a window into contiguous intervals with the first start
and last stop preserved. ``HistoryCondition`` records both boundary flags and
the predicate that was actually sent. ``LocalHistory`` appends the condition of
every request it receives, which makes the extraction order auditable.

Report identity
---------------

A raw report is identified by ``(source, report_id, revision)``. A report
number is stable within its receiver source; the revision distinguishes
successive versions of that report. The highest revision is selected and every
lower revision is retained as a superseded report.

Pages and slices are not report identity. A page replayed in two slices, or the
same report returned by overlapping windows, is one raw report with several
recorded occurrences, not a second receiver.

Reconciliation rules
--------------------

Observations are keyed by aircraft address and observation time. Callsigns are
descriptive only: two aircraft may broadcast the same callsign and remain
separate observations.

For each aircraft/time key:

- identical coordinates from several sources become one observation with one
  entry per unique source;
- a coordinate confirmed by more sources is preferred over a single report,
  even when the single report has a larger revision;
- a different coordinate is retained as a conflict with its own sources and
  report references;
- the primary observation and unrelated aircraft/times are never blocked by a
  conflict.

Delivery tables
---------------

``ArchiveDelivery.frames()`` returns these tables:

``observations``
    One row per aircraft and observation time. Includes coordinates, callsign,
    all contributing sources, confirmation count, source report references,
    and the conflict count.

``conflicts``
    One row per retained alternative coordinate, with its own sources and
    report references.

``superseded``
    One row per older report revision. These rows are searchable but do not
    count as observations.

``provenance``
    One row per raw report revision, including every slice it appeared in,
    occurrence count, payload-variant count, and resolution status.

``history-conditions``
    The half-open condition used for each slice.

``raw_frame()``
    One row per raw occurrence with its slice boundaries and page sequence.
    This is the table to inspect when checking whether a report was replayed in
    more than one slice.

Example
-------

.. code-block:: python

    from pyopensky.archive import (
        ArchiveExtractor,
        LocalHistory,
        pages_from_frame,
        reports_from_frame,
        utc_slices,
    )

    pages = pages_from_frame(recorded_pages)
    client = LocalHistory(pages)
    delivery = ArchiveExtractor(client, step="6h").extract(
        "2026-11-18 00:00",
        "2026-11-20 00:00",
        request_order="forward",
        page_order="reverse",
    )

    observations = delivery.observations_frame()
    provenance = delivery.provenance_frame()
    conditions = delivery.conditions_frame()

The ``scripts/build_archive_delivery.py`` script builds a complete example
under ``artifacts/archive`` from ``tests/fixtures/archive_reports.csv``. It
checks two request orders, two page orders, daily and six-hour slicing, a
replayed old revision, and the stop boundary before writing the delivery.
