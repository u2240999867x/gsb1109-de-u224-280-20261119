import json
from uuid import UUID

from SpiffWorkflow import TaskState
from SpiffWorkflow.bpmn import BpmnEvent, BpmnWorkflow
from SpiffWorkflow.bpmn.migration.two_step import (
    CHECK,
    DATA,
    FileReceipt,
    InMemoryReceiptLedger,
    MigrationConflict,
    RECEIPT_ACKNOWLEDGED,
    TwoStepMapping,
    TwoStepMigrator,
    WaitNodeSplit,
    FieldRename,
    find_processing_record,
    mapping_from_workflow,
    processing_records,
    trace_receipt,
)
from SpiffWorkflow.bpmn.specs.event_definitions import MessageEventDefinition

from .BpmnWorkflowTestCase import BpmnWorkflowTestCase


# An old instance is dormant once both supplier subprocesses are parked on the
# shared WaitFile node.  The IDs below make the assertions readable; they are
# replaced by the real task IDs when a test builds the fixture.
INSTANCE = "instance-1"

MAPPING = TwoStepMapping(
    splits=[
        WaitNodeSplit("supplier_a", "WaitFile", "A_DataWait", "A_CheckWait"),
        WaitNodeSplit("supplier_b", "WaitFile", "B_DataWait", "B_CheckWait"),
    ],
    renames=[
        FieldRename("supplier_a", DATA, "batch", "a_data_batch"),
        FieldRename("supplier_a", CHECK, "batch", "a_check_batch"),
        FieldRename("supplier_b", DATA, "batch", "b_data_batch"),
        FieldRename("supplier_b", CHECK, "batch", "b_check_batch"),
    ],
)


class TwoStepMigrationTest(BpmnWorkflowTestCase):

    def setUp(self):
        super().setUp()
        self.old_spec, self.old_subprocesses = self.load_workflow_spec("two_step_old.bpmn", "Top")
        self.new_spec, self.new_subprocesses = self.load_workflow_spec("two_step_new.bpmn", "Top")
        self.workflow = BpmnWorkflow(self.old_spec, self.old_subprocesses)
        self.workflow.do_engine_steps()
        self.ledger = InMemoryReceiptLedger()

    # -- fixture helpers -------------------------------------------------
    def _wait_tasks(self, workflow=None):
        workflow = workflow or self.workflow
        result = []
        for subprocess in workflow.subprocesses.values():
            task = subprocess.get_next_task(state=TaskState.WAITING)
            if task is not None and (
                task.task_spec.name == "WaitFile" or task.task_spec.bpmn_name == "WaitFile"
            ):
                result.append(task)
        return sorted(result, key=lambda task: task.workflow.spec.name)

    def _task(self, process, node, workflow=None, states=TaskState.NOT_FINISHED_MASK):
        workflow = workflow or self.workflow
        for subprocess in workflow.subprocesses.values():
            if subprocess.spec.name == process:
                return subprocess.get_next_task(state=states, spec_name=node)
        return None

    def _receipt(self, receipt_id, supplier, kind, state=RECEIPT_ACKNOWLEDGED, payload=None, batch_id="batch-7"):
        return FileReceipt(
            receipt_id=receipt_id,
            instance_id=INSTANCE,
            supplier=supplier,
            kind=kind,
            state=state,
            batch_id=batch_id,
            old_task_id=None,
            payload=payload or {},
        )

    def _plan_and_apply(self, workflow=None, writer_check=None):
        workflow = workflow or self.workflow
        migrator = TwoStepMigrator(MAPPING, self.ledger, writer_check=writer_check)
        plan = migrator.plan(workflow, self.new_spec, self.new_subprocesses, INSTANCE)
        result = migrator.apply(plan, workflow)
        return migrator, plan, result

    # -- dormant archive checks -----------------------------------------
    def test_ready_archive_is_rejected_without_mutation(self):
        task = self._wait_tasks()[0]
        task._set_state(TaskState.READY)
        before = self.workflow.get_dump()
        migrator = TwoStepMigrator(MAPPING, self.ledger)
        with self.assertRaises(MigrationConflict) as ctx:
            migrator.plan(self.workflow, self.new_spec, self.new_subprocesses, INSTANCE)
        self.assertEqual("archive_not_dormant", ctx.exception.code)
        self.assertEqual(before, self.workflow.get_dump())

    def test_unclassified_receipt_is_rejected_and_archive_keeps_running(self):
        # A receipt whose classification is missing must not be guessed from
        # the order in which the wait tasks happen to appear.
        self.ledger.add(self._receipt("R-unknown", "supplier_a", None))
        migrator = TwoStepMigrator(MAPPING, self.ledger)
        with self.assertRaises(MigrationConflict) as ctx:
            migrator.plan(self.workflow, self.new_spec, self.new_subprocesses, INSTANCE)
        self.assertEqual("unclassified_receipt", ctx.exception.code)
        self.assertEqual("R-unknown", ctx.exception.receipt_id)
        # The old model is untouched and can still receive files and finish.
        self.assertEqual(2, len(self._wait_tasks()))
        for process in ("supplier_a", "supplier_b"):
            subprocess = next(
                sp for sp in self.workflow.subprocesses.values() if sp.spec.name == process
            )
            self.workflow.catch(BpmnEvent(MessageEventDefinition("file_batch"), {}, target=subprocess))
            self.workflow.do_engine_steps()
        self.assertTrue(self.workflow.is_completed())

    def test_old_writer_at_cutover_is_rejected_and_archive_keeps_running(self):
        self.ledger.add(self._receipt("R-A-check", "supplier_a", CHECK))
        migrator = TwoStepMigrator(MAPPING, self.ledger, writer_check=lambda instance: instance == INSTANCE)
        plan = migrator.plan(self.workflow, self.new_spec, self.new_subprocesses, INSTANCE)
        before = self.workflow.get_dump()
        with self.assertRaises(MigrationConflict) as ctx:
            migrator.apply(plan, self.workflow)
        self.assertEqual("old_version_writer", ctx.exception.code)
        self.assertEqual(before, self.workflow.get_dump())
        self.assertEqual(2, len(self._wait_tasks()))

    # -- main scenario 1 -------------------------------------------------
    def test_check_only_supplier_migrates_as_complete_supplier(self):
        """A's data is in the warehouse but its check is still missing.

        B already produced a check but has no data.  The plan can still be
        generated; the late B data arrives before the switch.  The receipt was
        acknowledged by the old engine, so B migrates with a complete result
        and no waiting data task.
        """
        self.ledger.add(self._receipt("R-A-data", "supplier_a", DATA, payload={"batch": "A-1"}))
        self.ledger.add(self._receipt("R-A-check", "supplier_a", CHECK, state="queued"))
        self.ledger.add(self._receipt("R-B-check", "supplier_b", CHECK))
        self.ledger.add(self._receipt("R-B-data", "supplier_b", DATA, state="queued"))

        # The dormant old archive has A data already accepted; both wait nodes
        # are parked.  Plan generation must not mutate anything.
        migrator = TwoStepMigrator(MAPPING, self.ledger)
        plan = migrator.plan(self.workflow, self.new_spec, self.new_subprocesses, INSTANCE)
        self.assertEqual(2, len(plan.preview))
        by_process = {d.process: d for d in plan.preview}
        self.assertTrue(by_process["supplier_a"].data_satisfied)
        self.assertFalse(by_process["supplier_a"].check_satisfied)
        self.assertFalse(by_process["supplier_b"].data_satisfied)
        self.assertTrue(by_process["supplier_b"].check_satisfied)

        # Between planning and switching, the old engine acknowledges B's late
        # data notification.
        self.ledger.acknowledge(INSTANCE, "R-B-data")
        decision = {d.process: d for d in migrator.plan(
            self.workflow, self.new_spec, self.new_subprocesses, INSTANCE).preview}
        self.assertTrue(decision["supplier_b"].data_satisfied)
        self.assertTrue(decision["supplier_b"].check_satisfied)

        result = migrator.apply(plan, self.workflow)

        # B is a complete migrated supplier, not a newly-created B data wait.
        self.assertIsNone(self._task("supplier_b", "B_DataWait"))
        self.assertIsNone(self._task("supplier_b", "B_CheckWait"))
        self.assertIsNotNone(self._task("supplier_b", "B_End", states=TaskState.COMPLETED))
        # A keeps its already-warehoused half unavailable for re-delivery and
        # still waits for the check.
        self.assertIsNone(self._task("supplier_a", "A_DataWait"))
        check = self._task("supplier_a", "A_CheckWait")
        self.assertIsNotNone(check)
        self.assertTrue(check.has_state(TaskState.WAITING))
        # The receipt IDs are the original ones and there is one record each.
        self.assertEqual(4, len(processing_records(self.workflow)))
        self.assertEqual("R-B-data", find_processing_record(self.workflow, "R-B-data").receipt_id)
        self.assertEqual(1, len([r for r in result.records if r.receipt_id == "R-B-data"]))
        # Re-submitting A's already-warehoused data receipt must not deliver
        # that half again.
        before = self.workflow.get_dump()
        migrator.deliver(self.workflow, "R-A-data")
        self.assertEqual(before, self.workflow.get_dump())

    # -- main scenario 2 -------------------------------------------------
    def test_late_data_queued_before_cutover_is_delivered_to_new_data_wait(self):
        """B's late data notification entered the waiting area but was not processed.

        A's data is already warehoused and A is missing its check.  Since the
        notification was not acknowledged, the new model must create a data wait
        for B and the queued notification must be delivered there.
        """
        self.ledger.add(self._receipt("R-A-data", "supplier_a", DATA))
        self.ledger.add(self._receipt("R-A-check", "supplier_a", CHECK, state="queued"))
        self.ledger.add(self._receipt("R-B-check", "supplier_b", CHECK))
        self.ledger.add(self._receipt("R-B-data", "supplier_b", DATA, state="queued"))

        migrator = TwoStepMigrator(MAPPING, self.ledger)
        plan = migrator.plan(self.workflow, self.new_spec, self.new_subprocesses, INSTANCE)
        # No B acknowledgment: B's queued data stays queued through the switch.
        result = migrator.apply(plan, self.workflow)

        data_wait = self._task("supplier_b", "B_DataWait")
        self.assertIsNotNone(data_wait)
        self.assertTrue(data_wait.has_state(TaskState.WAITING))
        # The new check node exists but is only predicted until data arrives.
        check_wait = self._task("supplier_b", "B_CheckWait")
        self.assertIsNotNone(check_wait)
        self.assertTrue(check_wait.has_state(TaskState.FUTURE))
        self.assertFalse(check_wait.has_state(TaskState.WAITING))

        # Re-submit the original client receipt ID.  It is delivered once to
        # the new B data wait, and the old B check remains held until then.
        record = migrator.deliver(self.workflow, "R-B-data")
        self.assertEqual(RECEIPT_ACKNOWLEDGED, record.status)
        self.assertEqual("B_DataWait", record.new_node)
        self.assertEqual(1, len([r for r in processing_records(self.workflow) if r.receipt_id == "R-B-data"]))

        # The old B check was valid all along; it is held only until the data
        # stage completes, then applied to B's own check wait.  It still must
        # never be used to advance A.
        self.assertIsNone(self._task("supplier_b", "B_CheckWait"))
        self.assertIsNotNone(self._task("supplier_b", "B_End", states=TaskState.COMPLETED))
        self.assertIsNone(self._task("supplier_a", "A_DataWait"))
        self.assertTrue(self._task("supplier_a", "A_CheckWait").has_state(TaskState.WAITING))
        # The B check receipt keeps its original identity and a single record.
        self.assertEqual(1, len([r for r in processing_records(self.workflow) if r.receipt_id == "R-B-check"]))
        held = find_processing_record(self.workflow, "R-B-check")
        self.assertTrue(held.applied)
        self.assertEqual("B_CheckWait", held.new_node)
        # Repeating the delivery does not complete a second node.
        before = self.workflow.get_dump()
        again = migrator.deliver(self.workflow, "R-B-data")
        self.assertEqual(RECEIPT_ACKNOWLEDGED, again.status)
        self.assertEqual(before, self.workflow.get_dump())

    def test_old_completed_wait_migrates_without_recreating_waits(self):
        # If the old engine already completed B's WaitFile when the data
        # notification arrived, B must still migrate as a complete supplier.
        self.ledger.add(self._receipt("R-A-data", "supplier_a", DATA))
        self.ledger.add(self._receipt("R-A-check", "supplier_a", CHECK, state="queued"))
        self.ledger.add(self._receipt("R-B-check", "supplier_b", CHECK))
        self.ledger.add(self._receipt("R-B-data", "supplier_b", DATA, state="queued"))
        subprocess = next(
            sp for sp in self.workflow.subprocesses.values() if sp.spec.name == "supplier_b"
        )
        wait = subprocess.get_next_task(state=TaskState.WAITING, spec_name="B_WaitFile")
        self.assertIsNotNone(wait)
        # The old engine acknowledges the notification and completes the wait.
        self.workflow.catch(BpmnEvent(MessageEventDefinition("file_batch"), {}, target=subprocess))
        self.workflow.do_engine_steps()
        self.ledger.acknowledge(INSTANCE, "R-B-data")
        self.assertIsNotNone(self._task("supplier_b", "B_End", states=TaskState.COMPLETED))

        migrator, _, result = self._plan_and_apply()
        self.assertIsNone(self._task("supplier_b", "B_DataWait"))
        self.assertIsNone(self._task("supplier_b", "B_CheckWait"))
        self.assertIsNotNone(self._task("supplier_b", "B_End", states=TaskState.COMPLETED))
        self.assertEqual("B_DataWait", find_processing_record(self.workflow, "R-B-data").new_node)

    def test_conflict_still_leaves_old_version_runnable(self):
        self.ledger.add(self._receipt("R-A-check", "supplier_a", CHECK))
        migrator = TwoStepMigrator(MAPPING, self.ledger, writer_check=lambda instance: True)
        plan = migrator.plan(self.workflow, self.new_spec, self.new_subprocesses, INSTANCE)
        with self.assertRaises(MigrationConflict):
            migrator.apply(plan, self.workflow)

        # The old version can be saved, reloaded and still finish its normal
        # work: both suppliers accept their file batch and the workflow ends.
        self.workflow = self.serializer.deserialize_json(self.serializer.serialize_json(self.workflow))
        self.assertEqual(2, len(self._wait_tasks()))
        for process in ("supplier_a", "supplier_b"):
            subprocess = next(
                sp for sp in self.workflow.subprocesses.values() if sp.spec.name == process
            )
            self.workflow.catch(BpmnEvent(MessageEventDefinition("file_batch"), {}, target=subprocess))
            self.workflow.do_engine_steps()
        self.assertTrue(self.workflow.is_completed())
        self.assertEqual([], processing_records(self.workflow))

    def test_held_check_is_applied_after_late_data(self):
        self.ledger.add(self._receipt("R-A-data", "supplier_a", DATA))
        self.ledger.add(self._receipt("R-A-check", "supplier_a", CHECK, state="queued"))
        self.ledger.add(self._receipt("R-B-check", "supplier_b", CHECK))
        self.ledger.add(self._receipt("R-B-data", "supplier_b", DATA, state="queued"))
        migrator, _, _ = self._plan_and_apply()

        # Re-submit B's check first: data is still missing, so it is held.
        held = migrator.deliver(self.workflow, "R-B-check")
        self.assertEqual(RECEIPT_ACKNOWLEDGED, held.status)
        self.assertFalse(held.applied)
        self.assertTrue(held.held)
        self.assertTrue(self._task("supplier_b", "B_DataWait").has_state(TaskState.WAITING))
        # The check did not advance A or create a second check task.
        self.assertTrue(self._task("supplier_a", "A_CheckWait").has_state(TaskState.WAITING))
        self.assertEqual(1, len([r for r in processing_records(self.workflow) if r.receipt_id == "R-B-check"]))

        # Now the data arrives; the held check is applied to B's own wait only.
        migrator.deliver(self.workflow, "R-B-data")
        self.assertIsNotNone(self._task("supplier_b", "B_End", states=TaskState.COMPLETED))
        self.assertIsNone(self._task("supplier_b", "B_CheckWait"))
        self.assertTrue(self._task("supplier_a", "A_CheckWait").has_state(TaskState.WAITING))
        # The already-warehoused A data half is never re-delivered.
        before = self.workflow.get_dump()
        migrator.deliver(self.workflow, "R-A-data")
        self.assertEqual(before, self.workflow.get_dump())

    def test_missing_node_mapping_is_located_and_refused(self):
        incomplete = TwoStepMapping(
            splits=[WaitNodeSplit("supplier_a", "WaitFile", "A_DataWait", "A_CheckWait")],
            renames=[],
        )
        migrator = TwoStepMigrator(incomplete, self.ledger)
        with self.assertRaises(MigrationConflict) as ctx:
            migrator.plan(self.workflow, self.new_spec, self.new_subprocesses, INSTANCE)
        self.assertEqual("missing_node_mapping", ctx.exception.code)
        self.assertEqual("supplier_b", ctx.exception.process)
        location = ctx.exception.locate()
        self.assertIn(location["task_id"], {str(t.id) for sp in self.workflow.subprocesses.values() for t in sp.get_tasks()})

    def test_unknown_target_node_is_located_and_refused(self):
        wrong = TwoStepMapping(
            splits=[
                WaitNodeSplit("supplier_a", "WaitFile", "A_DataWait", "A_CheckWait"),
                WaitNodeSplit("supplier_b", "WaitFile", "B_DataWait", "No_Such_Check"),
            ],
            renames=[],
        )
        migrator = TwoStepMigrator(wrong, self.ledger)
        with self.assertRaises(MigrationConflict) as ctx:
            migrator.plan(self.workflow, self.new_spec, self.new_subprocesses, INSTANCE)
        self.assertEqual("unknown_target_node", ctx.exception.code)
        self.assertEqual("No_Such_Check", ctx.exception.detail["check_node"])
        self.assertEqual(2, len(self._wait_tasks()))

    def test_supplier_fields_are_renamed_per_stage_and_supplier(self):
        for task in self._wait_tasks():
            task.set_data(batch="B7", common="kept")
        self.ledger.add(self._receipt("R-A-data", "supplier_a", DATA))
        self.ledger.add(self._receipt("R-A-check", "supplier_a", CHECK, state="queued"))
        self.ledger.add(self._receipt("R-B-check", "supplier_b", CHECK))
        self.ledger.add(self._receipt("R-B-data", "supplier_b", DATA, state="queued"))
        migrator, _, _ = self._plan_and_apply()

        def data(process, node):
            return self._task(process, node, states=TaskState.ANY_MASK).data

        self.assertEqual("B7", data("supplier_a", "A_DataWait")["a_data_batch"])
        self.assertEqual("B7", data("supplier_a", "A_CheckWait")["a_check_batch"])
        self.assertEqual("B7", data("supplier_b", "B_DataWait")["b_data_batch"])
        self.assertEqual("B7", data("supplier_b", "B_CheckWait")["b_check_batch"])
        for process, node in (
            ("supplier_a", "A_DataWait"), ("supplier_a", "A_CheckWait"),
            ("supplier_b", "B_DataWait"), ("supplier_b", "B_CheckWait"),
        ):
            self.assertNotIn("batch", data(process, node))
            self.assertEqual("kept", data(process, node)["common"])

    def test_delivered_payload_uses_supplier_field_name(self):
        self.ledger.add(self._receipt("R-A-data", "supplier_a", DATA))
        self.ledger.add(self._receipt("R-A-check", "supplier_a", CHECK, state="queued"))
        self.ledger.add(self._receipt("R-B-check", "supplier_b", CHECK))
        self.ledger.add(self._receipt(
            "R-B-data", "supplier_b", DATA, state="queued", payload={"batch": "late-B"}
        ))
        migrator, _, _ = self._plan_and_apply()
        record = migrator.deliver(self.workflow, "R-B-data")
        task = self.workflow.get_task_from_id(UUID(record.new_task_id))
        self.assertEqual("late-B", task.data["b_data_batch"])
        self.assertNotIn("batch", task.data)

    def test_multi_file_stage_waits_for_every_receipt(self):
        # The old WaitFile accepted a batch of several files.  One data file
        # is acknowledged before cutover, a second is only queued.  The data
        # stage must not complete on the first receipt.
        self.ledger.add(self._receipt("R-A-data-1", "supplier_a", DATA, payload={"batch": "first"}))
        self.ledger.add(self._receipt("R-A-data-2", "supplier_a", DATA, state="queued"))
        self.ledger.add(self._receipt("R-A-check-1", "supplier_a", CHECK, state="queued"))
        self.ledger.add(self._receipt("R-B-check", "supplier_b", CHECK))
        migrator, plan, _ = self._plan_and_apply()
        preview = {d.process: d for d in plan.preview}
        self.assertFalse(preview["supplier_a"].data_satisfied)

        data_wait = self._task("supplier_a", "A_DataWait")
        self.assertTrue(data_wait.has_state(TaskState.WAITING))
        self.assertEqual(
            {"R-A-data-1", "R-A-data-2"},
            {r.receipt_id for r in processing_records(self.workflow)
             if r.supplier == "supplier_a" and r.kind == DATA},
        )
        # The first file was a fact before the switch, so its value is kept
        # even though the stage as a whole is still open.
        data_wait = self._task("supplier_a", "A_DataWait")
        self.assertEqual("first", data_wait.data["a_data_batch"])

        # Delivering only the queued second file completes the data stage;
        # the check stage then becomes the active wait.
        migrator.deliver(self.workflow, "R-A-data-2")
        self.assertIsNone(self._task("supplier_a", "A_DataWait"))
        check_wait = self._task("supplier_a", "A_CheckWait")
        self.assertIsNotNone(check_wait)
        self.assertTrue(check_wait.has_state(TaskState.WAITING))

    def test_two_supplier_checks_do_not_cross(self):
        # Both supplier processes have a node with the same label but the
        # receipt and old task ID decide which one advances.
        self.ledger.add(self._receipt("R-A-data", "supplier_a", DATA))
        self.ledger.add(self._receipt("R-A-check", "supplier_a", CHECK, state="queued"))
        self.ledger.add(self._receipt("R-B-check", "supplier_b", CHECK))
        self.ledger.add(self._receipt("R-B-data", "supplier_b", DATA, state="queued"))
        migrator, _, _ = self._plan_and_apply()
        migrator.deliver(self.workflow, "R-B-data")
        # B completed, but A's check wait is still untouched.
        a_check = self._task("supplier_a", "A_CheckWait")
        self.assertTrue(a_check.has_state(TaskState.WAITING))
        self.assertTrue(self._task("supplier_a", "A_End", states=TaskState.FUTURE).has_state(TaskState.FUTURE))
        b_record = find_processing_record(self.workflow, "R-B-check")
        self.assertEqual("supplier_b", b_record.supplier)
        a_record = find_processing_record(self.workflow, "R-A-check")
        self.assertEqual("supplier_a", a_record.supplier)
        a_data = find_processing_record(self.workflow, "R-A-data")
        b_data = find_processing_record(self.workflow, "R-B-data")
        self.assertEqual(a_data.old_task_id, a_record.old_task_id)
        self.assertEqual(b_data.old_task_id, b_record.old_task_id)
        self.assertNotEqual(a_record.old_task_id, b_record.old_task_id)

    def test_conflict_report_is_json_friendly(self):
        self.ledger.add(self._receipt("R-unknown", "supplier_a", None))
        migrator = TwoStepMigrator(MAPPING, self.ledger)
        with self.assertRaises(MigrationConflict) as ctx:
            migrator.plan(self.workflow, self.new_spec, self.new_subprocesses, INSTANCE)
        location = json.loads(json.dumps(ctx.exception.locate()))
        self.assertEqual("unclassified_receipt", location["code"])
        self.assertEqual("supplier_a", location["process"])
        self.assertEqual("R-unknown", location["receipt_id"])
        self.assertTrue(location["task_id"])

    def test_preview_records_target_nodes(self):
        self.ledger.add(self._receipt("R-A-data", "supplier_a", DATA))
        self.ledger.add(self._receipt("R-A-check", "supplier_a", CHECK, state="queued"))
        self.ledger.add(self._receipt("R-B-check", "supplier_b", CHECK))
        self.ledger.add(self._receipt("R-B-data", "supplier_b", DATA, state="queued"))
        migrator = TwoStepMigrator(MAPPING, self.ledger)
        plan = migrator.plan(self.workflow, self.new_spec, self.new_subprocesses, INSTANCE)
        by_process = {d.process: d for d in plan.preview}
        self.assertEqual("A_DataWait", by_process["supplier_a"].data_node)
        self.assertEqual("A_CheckWait", by_process["supplier_a"].check_node)
        # The preview can be dumped without the workflow objects.
        json.dumps(plan.preview_dict())

    def test_replanning_after_migration_keeps_other_records(self):
        self.ledger.add(self._receipt("R-A-data", "supplier_a", DATA))
        self.ledger.add(self._receipt("R-A-check", "supplier_a", CHECK, state="queued"))
        self.ledger.add(self._receipt("R-B-check", "supplier_b", CHECK))
        self.ledger.add(self._receipt("R-B-data", "supplier_b", DATA, state="queued"))
        migrator, _, _ = self._plan_and_apply()
        migrator.deliver(self.workflow, "R-B-data")
        before = {r.receipt_id: r.to_dict() for r in processing_records(self.workflow)}

        # Plan and apply again: there is no old WaitFile left, so this is a
        # no-op for the task tree and must not discard existing records.
        plan = migrator.plan(self.workflow, self.new_spec, self.new_subprocesses, INSTANCE)
        self.assertEqual([], plan.preview)
        migrator.apply(plan, self.workflow)
        after = {r.receipt_id: r.to_dict() for r in processing_records(self.workflow)}
        self.assertEqual(before, after)

    # -- persistence and identity ---------------------------------------
    def test_saved_reloaded_new_model_traces_one_old_receipt_to_one_new_node(self):
        self.ledger.add(self._receipt("R-A-data", "supplier_a", DATA))
        self.ledger.add(self._receipt("R-A-check", "supplier_a", CHECK, state="queued"))
        self.ledger.add(self._receipt("R-B-check", "supplier_b", CHECK))
        self.ledger.add(self._receipt("R-B-data", "supplier_b", DATA, state="queued"))
        migrator, plan, result = self._plan_and_apply()

        # Save and reload after the cutover, then re-submit the client's
        # original notification ID.
        state = self.serializer.serialize_json(self.workflow)
        reloaded = self.serializer.deserialize_json(state)
        # The restarted process can recover the mapping from the new workflow
        # itself; it does not need the original configuration object.
        recovered = mapping_from_workflow(reloaded)
        self.assertEqual(MAPPING.splits, recovered.splits)
        self.assertEqual(MAPPING.renames, recovered.renames)
        record = TwoStepMigrator(recovered, self.ledger).deliver(reloaded, "R-B-data")
        self.assertEqual("R-B-data", record.receipt_id)
        self.assertEqual("B_DataWait", record.new_node)

        trace = trace_receipt(reloaded, "R-B-data")
        self.assertEqual(INSTANCE, trace["old_instance_id"])
        self.assertEqual("WaitFile", trace["old_node"])
        self.assertEqual(1, trace["business_receipts"])
        self.assertEqual(1, len([r for r in processing_records(reloaded) if r.receipt_id == "R-B-data"]))
        # The new check wait is a separate task, but the receipt ID did not
        # become a second business receipt.
        self.assertNotEqual(
            find_processing_record(reloaded, "R-B-data").new_task_id,
            find_processing_record(reloaded, "R-B-check").new_task_id,
        )
        self.assertEqual("R-B-check", find_processing_record(reloaded, "R-B-check").receipt_id)

    def test_previously_migrated_records_remain_available(self):
        self.ledger.add(self._receipt("R-A-data", "supplier_a", DATA))
        self.ledger.add(self._receipt("R-A-check", "supplier_a", CHECK, state="queued"))
        self.ledger.add(self._receipt("R-B-check", "supplier_b", CHECK))
        self.ledger.add(self._receipt("R-B-data", "supplier_b", DATA, state="queued"))
        migrator, _, _ = self._plan_and_apply()
        migrator.deliver(self.workflow, "R-B-data")
        state = self.serializer.serialize_json(self.workflow)
        reloaded = self.serializer.deserialize_json(state)
        migrator2 = TwoStepMigrator(MAPPING, self.ledger)
        migrator2.deliver(reloaded, "R-B-check")
        # A still waits for its check; it was never pushed by B's check.
        self.assertTrue(self._task("supplier_a", "A_CheckWait", workflow=reloaded).has_state(TaskState.WAITING))
        # B's receipt records are still present after a second reload.
        self.assertEqual(
            {"R-A-data", "R-A-check", "R-B-check", "R-B-data"},
            {r.receipt_id for r in processing_records(reloaded)},
        )


class TwoStepMessageRoutingTest(BpmnWorkflowTestCase):
    """Optional integration of the new BPMN fixture with the message manager."""

    def setUp(self):
        super().setUp()
        self.spec, self.subprocesses = self.load_workflow_spec("two_step_new.bpmn", "Top")
        self.workflow = BpmnWorkflow(self.spec, self.subprocesses)
        self.workflow.do_engine_steps()

    def _subprocess(self, name):
        for subprocess in self.workflow.subprocesses.values():
            if subprocess.spec.name == name:
                return subprocess
        return None

    def test_message_reaches_each_supplier_data_wait(self):
        for supplier, message in (("supplier_a", "a_data"), ("supplier_b", "b_data")):
            subprocess = self._subprocess(supplier)
            task = subprocess.get_next_task(state=TaskState.WAITING, spec_name=f"{supplier[-1].upper()}_DataWait")
            self.assertIsNotNone(task)
            event = BpmnEvent(MessageEventDefinition(message), {}, target=subprocess)
            self.assertEqual(1, self.workflow.event_manager.catch(event))
            self.workflow.do_engine_steps()
            check = subprocess.get_next_task(state=TaskState.WAITING, spec_name=f"{supplier[-1].upper()}_CheckWait")
            self.assertIsNotNone(check)
