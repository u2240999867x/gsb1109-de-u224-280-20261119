Inbound Receiving Review
========================

:code:`SpiffWorkflow.inbound` is a small domain layer that combines a
SpiffWorkflow instance with an external reservation system for inbound
receiving review.  It is not part of the core BPMN engine and has no
additional dependencies.

The scenario it addresses is a receiving form of stable material lines, each
with a line number, a quantity and a destination station.  Two or more people
may edit the same version at the same time.  A line is identified by its
stable material line number; row order and the form-level remark are
metadata.  A row move, or clearing a remark, must never be interpreted as a
change to a line, and must never move a station edit from one material line to
another.

External Reservations
---------------------

Each station provides an inbound capacity (:code:`quota`).  Preverification
allocates a reservation for a line's :code:`(line_no, station, quantity)`.
That reservation has an identity (:code:`reservation_id`) and may be used
until it expires.  **Every preverification allocates a new identity.**
Returning a line to its original station and quantity does not revive an old
identity, and equal visible values are never evidence that a prior
confirmation applies to a newer reservation.

:class:`~SpiffWorkflow.inbound.ReservationLedger` is a small in-memory model
of that external system.  It is intentionally independent of the workflow
archive: the two are never written in one transaction.  A ledger tracks
accepted, rejected and unknown delivery requests, computes station occupancy
(accepted and unknown requests hold capacity; rejected ones do not), and
produces a checkable receipt when an acceptance is withdrawn.

Form Merge
----------

Two primitives are provided:

* :func:`~SpiffWorkflow.inbound.merge_by_line_no` merges station/quantity
  values by stable line number.  Row position is irrelevant.
* :func:`~SpiffWorkflow.inbound.model.merge_versions` is a three-way merge of
  an editor's version into the canonical form.  A line the editor actually
  changed takes the editor's value; an unchanged line keeps the server's
  value.  Row order and the remark come from whoever actually changed them,
  so a reorder or a cleared remark composes with a concurrent line edit
  without allocating reservations.  If both sides changed a line to
  different values, the merge raises rather than guessing.

Workflow Archive
----------------

:class:`~SpiffWorkflow.inbound.InboundReviewBatch` wraps a real
:class:`~SpiffWorkflow.Workflow` whose completed tasks are the workflow
display state: ``Draft``, ``Preverify``, ``Confirm``, ``Delivery``,
``Published``.  The archive can be saved and restored with
:meth:`~SpiffWorkflow.inbound.InboundReviewBatch.dump` and
:meth:`~SpiffWorkflow.inbound.InboundReviewBatch.load`; the serialized
workflow and the domain state are saved together, so an unknown delivery
outcome survives a save/restore cycle.

Confirmation and Publication
----------------------------

Confirmation snapshots the complete form and the concrete identity of every
line.  Publication archives that snapshot -- never the current working form
-- and only when every confirmed identity was accepted by its station.  A
working draft that happens to display the same station and quantity values is
not a substitute for an accepted reservation identity.

While a delivery outcome is unknown:

* no second submission is generated for any line;
* the saved original confirmation cannot be modified, re-confirmed or rolled
  back;
* working-draft edits may still allocate new identities (preverification), but
  they do not change the saved confirmation or the station's occupancy.

The outcome then forks on the external fact:

* if the second station accepts, the original confirmations are preserved and
  the confirmed version is warehoused; later edits are rejected because the
  version is archived;
* if the second station rejects, the batch remains invisible.  The first
  station's acceptance is withdrawn with a receipt, and the rejected line
  proceeds in the next confirmation with a **new** reservation identity.

Reconciliation
--------------

:meth:`~SpiffWorkflow.inbound.InboundReceivingService.reconcile` reports, for
each station, its quota, occupancy and delivery requests, and for each line
its visible values, current identity and confirmed identity.  A line can show
:code:`values_match_confirmation` while :code:`matches_confirmation` is false;
that is exactly the case in which the visible values were restored but the
reservation identity is new.

Example
-------

.. code-block:: python

    from SpiffWorkflow.inbound import (
        FormVersion, Line, InboundReceivingService, new_ledger,
    )

    form = FormVersion(
        version=1, form_id='BATCH-1', notes='dock 4',
        lines=[
            Line(line_no=1, station='S1', quantity=10),
            Line(line_no=2, station='S2', quantity=20),
        ],
    )
    ledger = new_ledger({'S1': 100, 'S2': 100})
    service = InboundReceivingService(ledger)
    batch = service.create_batch('BATCH-1', form)

    service.preverify(batch)
    service.confirm(batch)
    for outcome in service.deliver(batch):
        print(outcome.station, outcome.status)

    service.publish(batch)
    print(service.reconcile(batch))
