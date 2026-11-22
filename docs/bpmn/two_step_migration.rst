Two-Step Acceptance Migration
=============================

.. note::

    This is a specialized utility for a particular kind of model change.  It
    complements the generic :ref:`spec diffs <diff-utilities>`; it does not
    replace them.

A common change is to split one ``WaitFile`` node, which waited for a whole
batch of files, into two acceptance stages: one wait for data and one wait for
a check.  The same named wait may appear in more than one supplier subprocess,
so a name based diff can align the wrong task, and a generic reset can drop a
half of a batch that the warehouse has already accepted.

The helpers in :mod:`SpiffWorkflow.bpmn.migration.two_step` instead:

- scope every old wait node to its supplier process;
- preserve the old task ID as the data stage, so old instance locations remain
  addressable;
- record every receipt in a processing journal using the original receipt ID as
  the business identity;
- keep a single processing record even though the new model has two nodes.

Planning and switching
----------------------

The migration is deliberately three-phase::

    migrator = TwoStepMigrator(mapping, ledger, writer_check=writer_check)
    plan = migrator.plan(workflow, new_spec, new_subprocess_specs, instance_id)

    # The old version may still run here.  For example it can acknowledge a
    # late data notification.

    result = migrator.apply(plan, workflow)

``plan`` refuses an archive with a runnable or errored task and previews how
each old wait will be represented.  ``apply`` re-reads the ledger immediately
before switching and raises :class:`MigrationConflict` *before any mutation* if:

- the archive is no longer dormant;
- a receipt cannot be proven to be a data receipt or a check receipt;
- a node mapping or target spec is missing;
- the ``writer_check`` callback reports that an old-version processor may still
  write.

Because the refusal happens before the first task is changed, the old workflow
can continue exactly where it was.

Delivering old receipts
-----------------------

After the switch, a client may resubmit a receipt ID that it saved before the
migration::

    migrator.deliver(workflow, "receipt-123")

The journal maps the receipt to its new node and to the old instance and task.
A process started after the cutover can recover the mapping with
``mapping_from_workflow(workflow)``, so it does not need to retain the original
configuration object.  Delivering the same ID again is idempotent.  Several files of one stage are
tracked separately; the wait is only satisfied once every receipt for that stage
is acknowledged.  A check that was acknowledged before the data stage completed
is held under its original ID and applied only after data arrives.

A receipt can be ``queued`` (waiting for delivery), ``held`` (acknowledged but
blocked by an earlier stage), or ``applied`` (the node it belongs to has been
satisfied).  ``status`` remains the external receipt state; ``applied`` says
whether the migration has already used it.  Keeping the two apart is what makes
resubmitting an old ID safe: the same business receipt cannot complete a second
node.

Receipt ledger
--------------

:class:`ReceiptLedger` is the small interface the migration needs from the
external receipt store: list receipts for an instance, look one up, and report
a revision.  :class:`InMemoryReceiptLedger` is provided for tests and small
applications.  The migration never commits the ledger and the workflow as one
transaction; the revision is recorded in the plan so an application can detect
that the plan is stale.

Inspecting the result
---------------------

``processing_records(workflow)`` returns all processing records and
``trace_receipt(workflow, receipt_id)`` returns the old instance, old task and
new node for one receipt.  Both survive the normal serializer save and restore
cycle because the journal is stored in the top level workflow data.
