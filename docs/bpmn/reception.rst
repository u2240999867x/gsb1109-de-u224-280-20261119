Supplier Batch Reception
========================

This module coordinates file reception across two or more BPMN processes that
may take over from one another.  It is intended for the case where the file
itself is delivered by an external message service and the message service
does not share a transaction with any workflow archive.

Receipt identity
----------------

A receipt is identified by supplier, integer batch number, upload generation,
kind (``file`` or ``checksum``), and the sender's receipt id::

    from SpiffWorkflow.bpmn.reception import Receipt, ReceiptKey, ReceiptKind

    key = ReceiptKey('supplier-a', 7, 2)
    file = Receipt(key, ReceiptKind.FILE, 'file-a2-1', {'name': 'batch.csv'})
    checksum = Receipt(key, ReceiptKind.CHECKSUM, 'checksum-a2-1', {'ok': True})

The supplier is part of the identity, so two suppliers may both use batch
``7`` without interacting.  A checksum receipt can never satisfy a file wait,
even if it arrives first; an early checksum is retained and applied only after
a legitimate checksum request has been issued.

The message service
-------------------

:class:`~SpiffWorkflow.bpmn.reception.MessageService` is a local stand-in for
an external service.  It keeps submissions, claims, handoffs, and checksum
requests in its own storage.  Duplicate submissions are attributed, and a
same-id/different-body submission is reported as a conflict that preserves the
accepted version.

Most importantly, an ordinary claim lookup answers ``UNKNOWN`` until evidence
is requested::

    session.claim_status()               # ClaimStatus.UNKNOWN
    session.claim_status(evidence=True)  # ClaimStatus.CLAIMED or UNCLAIMED

Two recovered endpoints with identical archives therefore cannot infer an
answer from the fact that they both look like they are waiting.

Handoffs
--------

A supervisor may request and then confirm a handoff::

    coordinator = HandoverCoordinator(service)
    coordinator.request(key, 'old-receiver', 'new-receiver')
    coordinator.confirm(key, successor_registration)

A requested handoff leaves the old endpoint in charge; a confirmed handoff
moves authority to the exact successor *registration*.  Registrations carry an
incarnation id, so a late registration under the same endpoint name as the old
receiver does not inherit authority.

Sessions and archives
---------------------

:class:`~SpiffWorkflow.bpmn.reception.ReceptionSession` keeps local progress
in the workflow's data under ``reception_state``.  It never stores the external
claim answer::

    instance = BpmnReceptionWorkflow.start(workflow, session, serializer)
    archive = instance.save_archive()

    restored = BpmnReceptionWorkflow.restore(archive, service, serializer, registration)
    restored.session.continue_after_handoff()
    restored.send_checksum()
    restored.deliver(checksum_receipt)

If the predecessor already committed a claim whose acknowledgement was lost,
the successor's ``continue_after_handoff`` resumes at the checksum step and
does not claim the file again.  If the predecessor only *proposed* a claim and
the service never committed it, the successor takes over the still-available
file receipt.

Every trace must end with at most one committed file claim and one checksum
request per key.  Existing message event usage that does not configure a
supplier batch continues to work unchanged.
