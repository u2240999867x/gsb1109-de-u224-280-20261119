Resumable backfill
^^^^^^^^^^^^^^^^^^

``copy_rows`` and ``copy_table_rows`` transfer a query result once.  If a
transfer is interrupted, the caller has to work out which rows reached the
destination.  ``backfill`` is a separate entry point for append-only sources
whose rows have a stable event number.  It records enough destination-side
state to resume without rewriting committed blocks.

.. literalinclude:: ../code_demos/backfill/demo_backfill.py
   :language: python

State tables
------------

``backfill`` uses three destination tables, created by
:func:`create_backfill_tables() <etlhelper.create_backfill_tables>` or on the
first call when ``create_tables=True``:

``etlhelper_backfill_run``
    The frozen event-number window for each ``run_id``.

``etlhelper_backfill_coverage``
    One row for each block that was read and committed, including the number
    of accepted and rejected events in that block.

``etlhelper_backfill_reject``
    One row for each event id that the destination refused, with the error
    message.

The names can be changed with :class:`BackfillTables
<etlhelper.BackfillTables>`.

Why coverage is explicit
------------------------

The archive table alone cannot answer "was event 43 missed or rejected?".  A
rejected row is absent from the archive, just like a row that was never read.
Coverage is therefore written in the same transaction as the archive rows and
the reject records for a block.  A committed coverage row proves the block was
processed, whatever happened to individual events.

Coverage is read from contiguous coverage rows starting at the beginning of the
frozen window.  It is never inferred from ``MAX(event_id)`` in the archive.
If an event at the expected boundary is missing, the run stops before the gap
and reports ``missing_event_at_boundary``; that range is not marked as
processed.

Resuming and late events
------------------------

Re-running the same ``run_id`` continues after the last confirmed block.  The
window is fixed on the first call, so events that arrive later (for example
event 46 while backfilling 41-45) are not included.  They are left for the next
run.  Using the same ``run_id`` with a different window is an error.

Rejected events
---------------

Rows are inserted as a block first.  If the database rejects any row, the
block is trialled row by row to isolate the rejects.  Accepted rows, reject
records and the coverage marker are then committed together.  The rejected
event ids are returned in ``BackfillResult.rejected_event_ids`` and can be read
later with :func:`rejected_events() <etlhelper.rejected_events>`.

Commit acknowledgement loss
---------------------------

A connection can drop after the destination has committed but before the
caller receives the acknowledgement.  In that case ``backfill`` checks the
destination for the block's coverage marker.  If the marker is present, the
block is treated as committed and is not written again.  If it is absent, the
block is left unprocessed and will be read again on the next run.

If the destination cannot be asked whether the commit landed, the outcome is
unknown.  By default ``backfill`` raises
:class:`ETLHelperCommitUnconfirmedError
<etlhelper.exceptions.ETLHelperCommitUnconfirmedError>` with the
``unconfirmed_range``; it does not write the block again.  Pass
``raise_on_unconfirmed=False`` to receive that range in
``BackfillResult.unconfirmed`` instead.  A ``confirm_commit`` callback can
provide a site-specific check, and ``dest_reconnect`` can supply a fresh
destination connection for verification after the original one dies.

Return value
------------

:class:`BackfillResult <etlhelper.BackfillResult>` contains the frozen
``window``, the confirmed ``coverage``, counts of accepted and rejected events,
the reject ids, and whether the window is ``complete``.
