.. _deadline_dispatch:

======================
Deadline based dispatch
======================

Snakemake can optionally order the dispatch of ready jobs by absolute
deadlines.  This is useful for nightly workflows whose remote execution end
keeps running when the local scheduler restarts.

Deadlines are absolute UNIX timestamps (or ISO-8601 datetimes) and are given per
rule name and, optionally, per wildcard value:

.. code-block:: console

    $ snakemake --scheduler deadline \
        --scheduler-deadline-deadlines 'align:sample=tumor@1763852400' \
        --scheduler-deadline-deadlines 'align:sample=control@2026-11-23T06:00:00Z'

The specification has the form ``RULE[:WILDCARD=VALUE,...]@TIME``.  If the
wildcard part is omitted, the deadline applies to every instance of the rule.
A JSON or YAML file with a list (or mapping) of such specifications can be
passed instead:

.. code-block:: console

    $ snakemake --scheduler deadline --scheduler-deadline-deadline-file deadlines.yaml

Providing deadlines via the API also selects the scheduler automatically:

.. code-block:: python

    from snakemake.settings.types import SchedulingSettings

    scheduling_settings = SchedulingSettings(
        deadlines=["align:sample=tumor@1763852400"],
    )

Ordering
--------

Jobs are ordered by

1. their original Snakemake priority (highest first),
2. jobs with a deadline before jobs without one,
3. the earliest deadline first,
4. the rule name, and
5. the sorted wildcard values.

Jobs whose deadline has already passed are **not** dropped; they are dispatched
(and ordered by their deadline) because a late result is usually still better
than no result.  Job groups are treated as one unit; their deadline is the
earliest deadline among their member jobs.

Only jobs that are ready and fit into the currently free memory are admitted.  A
job that does not fit does not consume memory or a submission permit; later,
smaller jobs can still be dispatched.

Restart safety
--------------

When ``--max-jobs-per-timespan`` is used, the submission permits are tracked in
a persistent dispatch ledger (``.snakemake/dispatch/ledger.json`` by default, or
the path given via ``SchedulingSettings.dispatch_ledger``).  The ledger stores
each planned submission as an *attempt*:

* a planned submission *reserves* a permit and its memory footprint;
* it *consumes* the permit only when the executor confirms acceptance, and the
  actual acceptance time is recorded;
* memory is released only when that exact attempt is reported finished or
  failed.

This makes duplicate or stale notifications harmless: replaying the failure of
attempt 1 cannot free the memory of attempt 2, and replaying a completion does
not free anything twice.

If the scheduler restarts, it reconciles its ledger against the execution end.
An executor can implement the optional ``dispatch_status(pending)`` hook and
return receipts containing the job key, attempt number, original acceptance
time, and still-running state, plus a list of submissions it definitively did
not accept.  Accepted attempts keep their original acceptance time and are never
resubmitted.  A planned submission for which the executor has no evidence yet
stays reserved: it is neither resubmitted nor allowed to free its permit until
the evidence arrives.  Executors without the hook leave all reservations in
place.

Workflows without deadlines, and all default runs, are unaffected.
