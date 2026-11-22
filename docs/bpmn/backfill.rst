Preview-First Backfill
=======================

The :code:`SpiffWorkflow.backfill` package provides a small coordination layer
for backfills that span two stations (north and south), a shared month table,
an external warehouse, and an approval service.  It is deliberately separate
from the BPMN engine: previews and confirmations happen before any branch runs,
and external acceptance is never inferred from a local save.

The three stores are intentionally independent so they can live in different
systems and cannot share a transaction:

- :code:`ArchiveStore` stores previews, staged results, task-state history, and
  credential history.  It is local, can lag, and never proves that the
  warehouse accepted anything.
- :code:`ApprovalStore` stores per-preview, per-station, per-output grants and
  the confirmed scope.  It is the only source of permission.
- :code:`Warehouse` accepts or rejects each output independently against the
  version it currently holds, checks declared input versions, and can sign off
  a version to close it to rewrites.

Previewing
----------

Build a :code:`PreviewRequest` that names the station(s), the writes, the
declared capabilities for each output kind, and any downstream tables the
preview affects.  Previews never write anywhere::

    from SpiffWorkflow.backfill import (
        BackfillCoordinator, GrantOperation, OutputAuthorization, OutputKind,
        OutputTarget, PlannedWrite, PreviewRequest, Station, Warehouse,
    )

    north = coordinator.preview(PreviewRequest(
        request_id="backfill-north",
        stations=(Station.NORTH,),
        writes=(
            PlannedWrite(
                target=OutputTarget(OutputKind.NORTH_TRANSFORM, "north_transform", "2026-11"),
                operation=GrantOperation.CREATE_REVISION,
                base_version=4,
                write_id="north-redo-transform",
            ),
        ),
        authorizations={
            OutputKind.NORTH_TRANSFORM: OutputAuthorization(
                OutputKind.NORTH_TRANSFORM, can_create_revision=True
            ),
        },
        archive_id="archive-235",
        affected_tables=("2026-11",),
    ))

    south = coordinator.preview(PreviewRequest(...))

Both previews can list the same month table in ``impact.affected_tables`` and
``impact.shared_with`` while ``impact.writes_for`` only returns the outputs
that belong to that station.  A station preview that tries to write another
station's output raises :code:`CrossStationImpactError`.

Authorizations are explicit and default to false.  A rewrite grant can be
narrowed with ``rewrite_requires_unsigned=True``; when the warehouse signs the
version after staging, the write is rejected and the staged result remains
available for inspection.

Confirming and submitting
-------------------------

Confirmation records a decision in the approval service and must name only
outputs present in the preview.  Stage branch results with ``stage`` and submit
them with ``submit``.  Submission re-checks, just before calling the warehouse:

- the operation is declared and the grant is still active;
- the write is inside the confirmed scope;
- the base version is still the warehouse's current version;
- every declared input version is still the warehouse's current version;
- a conditional rewrite still targets an unsigned version.

Any mismatch raises an error without writing.  The warehouse also checks
independently, so a race between the local check and the external call is
reported as a rejection rather than treated as success.

Delayed acceptance
------------------

``warehouse.queue`` plus ``warehouse.deliver`` model a warehouse that answers
later.  Submitting with ``deliver=False`` leaves the preview in
``COMMITTING``; :code:`observe_delivery` then applies the answer.  A local save
recorded with :code:`archive.record_local_version` is compared against the
warehouse during :code:`recover`: a higher local version is reported as “local
save is not an external acceptance,” and a lower local version is reported as
archive lag.

Recovery and resume
-------------------

:code:`recover` returns a :code:`RecoveryReport` with the warehouse's actual
effective versions, local versions, uneffective staged results, unauthorized
steps, superseded previews, and dependency mismatches.  Nothing is resubmitted.

:code:`resume` re-presents an old preview.  By default it never submits; it
reports which results are uneffective and whether a fresh preview is required.
With ``allow_submit=True`` it is all-or-nothing: a single conflict, withdrawal,
scope violation, stale input, or signed-version rewrite prevents *all* writes
from being resent.  Old grants are not inherited: :code:`re_preview` and
:code:`rehydrate` start with empty declarations and draft status, and the new
preview must be confirmed on its own.

See :code:`tests/SpiffWorkflow/backfill/BackfillScenarioTest.py` for executable
examples of both branches of the narrative.
