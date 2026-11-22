from SpiffWorkflow import TaskState
from SpiffWorkflow.bpmn import BpmnEvent, BpmnWorkflow
from SpiffWorkflow.bpmn.reception import (
    BpmnReceptionWorkflow,
    ClaimStatus,
    HandoffStatus,
    HandoverCoordinator,
    MessageService,
    Receipt,
    ReceiptKey,
    ReceiptKind,
    ReceptionSession,
    SessionStatus,
    SubmissionStatus,
)
from SpiffWorkflow.bpmn.serializer import BpmnWorkflowSerializer

from .BpmnWorkflowTestCase import BpmnWorkflowTestCase


def file_receipt(key, receipt_id, name=None):
    return Receipt(key, ReceiptKind.FILE, receipt_id, {"name": name or f"{receipt_id}.dat"})


def checksum_receipt(key, receipt_id, ok=True):
    return Receipt(key, ReceiptKind.CHECKSUM, receipt_id, {"ok": ok})


class ReceptionTestCase(BpmnWorkflowTestCase):
    """Shared fixtures for supplier batch reception scenarios."""

    serializer = BpmnWorkflowSerializer()

    def setUp(self):
        self.spec, self.subprocesses = self.load_workflow_spec("reception/supplier_reception.bpmn", "reception")
        self.service = MessageService()
        self.coordinator = HandoverCoordinator(self.service)

    def new_workflow(self):
        workflow = BpmnWorkflow(self.spec, self.subprocesses)
        workflow.do_engine_steps()
        return workflow

    def waiting_names(self, workflow):
        return sorted(
            task.task_spec.event_definition.name
            for task in workflow.get_tasks(state=TaskState.WAITING)
            if hasattr(task.task_spec, "event_definition")
        )

    def register(self, key, endpoint, registration_id=None):
        return self.service.register(key, endpoint, registration_id)

    def start_session(self, key, endpoint, workflow, registration=None):
        session = ReceptionSession.create(endpoint, key, self.service, registration)
        return BpmnReceptionWorkflow.start(workflow, session, self.serializer)

    def restore_session(self, archive, registration=None):
        return BpmnReceptionWorkflow.restore(archive, self.service, self.serializer, registration)


class ReceptionTraceOneTest(ReceptionTestCase):
    """Old endpoint committed a claim whose acknowledgement was delayed."""

    key = ReceiptKey("supplier-a", 7, 2)

    def build(self):
        old_workflow = self.new_workflow()
        old = self.start_session(self.key, "old-receiver", old_workflow)

        new_registration = self.register(self.key, "new-receiver")
        new_workflow = self.new_workflow()
        new = self.start_session(self.key, "new-receiver", new_workflow, new_registration)
        old_archive = old.save_archive()
        new_archive = new.save_archive()

        file = file_receipt(self.key, "file-a2-1")
        self.service.submit(file)
        old.session.claim_and_commit(file)
        old_archive = old.save_archive()

        self.coordinator.request(self.key, "old-receiver", "new-receiver")
        # Before confirmation the service only reports a requested handoff.
        self.assertEqual(HandoffStatus.REQUESTED, self.coordinator.steps(self.key)["authority_status"])
        self.coordinator.confirm(self.key, new_registration)

        restored_old = self.restore_session(old_archive)
        restored_new = self.restore_session(new_archive, new_registration)
        return old, new, restored_old, restored_new, file

    def test_both_executors_see_same_pending_file(self):
        old, new, restored_old, restored_new, file = self.build()
        for session in (restored_old, restored_new):
            receipts = session.session.pending_file_receipts()
            self.assertEqual(["file-a2-1"], [r.receipt_id for r in receipts])

    def test_old_endpoint_is_disenfranchised(self):
        old, new, restored_old, restored_new, file = self.build()
        outcome = restored_old.session.send_checksum()
        self.assertEqual(SessionStatus.DISENFRANCHISED, outcome.status)
        self.assertEqual(1, self.service.claim_count(self.key))
        self.assertIsNone(self.service.checksum_request(self.key))

    def test_successor_continues_without_reclaiming(self):
        old, new, restored_old, restored_new, file = self.build()
        outcome = restored_new.session.continue_after_handoff()
        self.assertEqual(SessionStatus.FILE_CLAIMED, outcome.status)
        self.assertEqual(outcome.claim_sequence, self.service.claim(self.key).sequence)
        self.assertEqual("old-receiver", self.service.claim(self.key).claimant)

        self.assertEqual(SessionStatus.CHECKSUM_SENT, restored_new.session.send_checksum().status)
        self.assertEqual(SessionStatus.COMPLETE, restored_new.session.receive_checksum(
            checksum_receipt(self.key, "checksum-a2-1")
        ).status)

    def test_single_claim_and_single_checksum(self):
        old, new, restored_old, restored_new, file = self.build()
        restored_new.session.continue_after_handoff()
        restored_new.session.send_checksum()
        self.assertEqual(SessionStatus.BLOCKED, restored_new.session.send_checksum().status)
        self.assertEqual(SessionStatus.DISENFRANCHISED, restored_old.session.send_checksum().status)
        restored_new.session.receive_checksum(checksum_receipt(self.key, "checksum-a2-1"))
        self.assertEqual(
            SessionStatus.COMPLETE,
            restored_new.session.receive_checksum(checksum_receipt(self.key, "checksum-a2-1")).status,
        )

        self.assertEqual(1, self.service.claim_count(self.key))
        self.assertEqual(1, len(self.service.checksum_requests))
        self.assertEqual(2, len(self.service.submissions_for(self.key)))

    def test_saved_state_alone_reports_unknown(self):
        old, new, restored_old, restored_new, file = self.build()
        self.assertEqual(ClaimStatus.UNKNOWN, restored_old.session.claim_status())
        self.assertEqual(ClaimStatus.UNKNOWN, restored_new.session.claim_status())
        self.assertEqual(ClaimStatus.CLAIMED, restored_new.session.claim_status(evidence=True))


class ReceptionTraceTwoTest(ReceptionTestCase):
    """Old endpoint only proposed a claim; the service never committed it."""

    key = ReceiptKey("supplier-a", 7, 2)

    def build(self):
        old_workflow = self.new_workflow()
        old = self.start_session(self.key, "old-receiver", old_workflow)
        new_registration = self.register(self.key, "new-receiver")
        new_workflow = self.new_workflow()
        new = self.start_session(self.key, "new-receiver", new_workflow, new_registration)
        old_archive = old.save_archive()
        new_archive = new.save_archive()

        file = file_receipt(self.key, "file-a2-1")
        self.service.submit(file)
        old.session.request_file_claim(file)
        self.assertIsNone(self.service.claim(self.key))
        old_archive = old.save_archive()

        self.coordinator.request(self.key, "old-receiver", "new-receiver")
        self.coordinator.confirm(self.key, new_registration)
        return old, new, self.restore_session(old_archive), self.restore_session(new_archive, new_registration), file

    def test_successor_takes_existing_file_receipt(self):
        old, new, restored_old, restored_new, file = self.build()
        outcome = restored_new.session.continue_after_handoff()
        self.assertEqual(SessionStatus.WAITING_FOR_FILE, outcome.status)
        self.assertEqual(SessionStatus.FILE_CLAIMED, restored_new.session.claim_and_commit(file).status)
        self.assertEqual("new-receiver", self.service.claim(self.key).claimant)

    def test_successor_completes_remaining_work(self):
        old, new, restored_old, restored_new, file = self.build()
        restored_new.session.continue_after_handoff()
        restored_new.session.claim_and_commit(file)
        self.assertEqual(SessionStatus.CHECKSUM_SENT, restored_new.session.send_checksum().status)
        self.assertEqual(
            SessionStatus.COMPLETE,
            restored_new.session.receive_checksum(checksum_receipt(self.key, "checksum-a2-1")).status,
        )
        self.assertEqual(1, self.service.claim_count(self.key))
        self.assertEqual(1, len(self.service.checksum_requests))

    def test_stale_old_proposal_cannot_commit_after_handoff(self):
        old, new, restored_old, restored_new, file = self.build()
        restored_new.session.continue_after_handoff()
        restored_new.session.claim_and_commit(file)
        self.assertEqual(SessionStatus.DISENFRANCHISED, restored_old.session.commit_file_claim().status)
        self.assertEqual("new-receiver", self.service.claim(self.key).claimant)

    def test_stale_old_proposal_cannot_be_committed_under_new_claim(self):
        old, new, restored_old, restored_new, file = self.build()
        restored_new.session.continue_after_handoff()
        restored_new.session.claim_and_commit(file)
        claim = self.service.claim(self.key)
        self.assertEqual("new-receiver", claim.claimant)
        self.assertEqual(1, self.service.claim_count(self.key))


class ReceptionPairedTraceTest(ReceptionTestCase):
    """Identical local archives, different external evidence.

    Both traces below publish the same saved endpoint states and the same
    confirmed handoff.  The only difference is whether the old endpoint's
    claim acknowledgement ever reached the service before it was restarted.
    """

    key = ReceiptKey("supplier-a", 7, 2)

    def prepare_trace(self, commit):
        service = MessageService()
        coordinator = HandoverCoordinator(service)
        old_registration = service.register(self.key, "old-receiver", "old-incarnation")
        successor_registration = service.register(self.key, "new-receiver", "new-incarnation")

        old_workflow = self.new_workflow()
        old = ReceptionSession.create("old-receiver", self.key, service, old_registration)
        old.save_to(old_workflow)
        new_workflow = self.new_workflow()
        successor = ReceptionSession.create("new-receiver", self.key, service, successor_registration)
        successor.save_to(new_workflow)

        file = file_receipt(self.key, "file-a2-1")
        service.submit(file)

        old.request_file_claim(file)
        if commit:
            # The service commits, but the acknowledgement never reaches the
            # old endpoint, so its archive still looks like a bare proposal.
            assert service.commit_claim(self._claim_request_id(service, self.key)) is not None
        old_archive = self.serializer.serialize_json(old_workflow)
        successor_archive = self.serializer.serialize_json(new_workflow)
        if commit:
            self.assertIsNotNone(service.claim(self.key))
        else:
            self.assertIsNone(service.claim(self.key))

        coordinator.request(self.key, "old-receiver", "new-receiver")
        coordinator.confirm(self.key, successor_registration)
        return {
            "service": service,
            "coordinator": coordinator,
            "old_registration": old_registration,
            "successor_registration": successor_registration,
            "old_archive": old_archive,
            "successor_archive": successor_archive,
            "file": file,
            "old_state": old.state.to_dict(),
            "successor_state": successor.state.to_dict(),
        }

    def test_identical_local_state_is_unknown_until_evidence(self):
        committed = self.prepare_trace(commit=True)
        proposed = self.prepare_trace(commit=False)
        # Both traces present the same local reception appearance: the same
        # endpoint state, the same waiting tasks, and the same bare proposal.
        self.assertEqual(committed["old_state"], proposed["old_state"])
        self.assertEqual(committed["successor_state"], proposed["successor_state"])
        self.assertIsNotNone(committed["old_state"]["claim_request_id"])
        self.assertEqual("waiting_for_file", committed["old_state"]["status"])

        for trace in (committed, proposed):
            session = ReceptionSession.restore_from(
                self.serializer.deserialize_json(trace["successor_archive"]),
                trace["service"],
                trace["successor_registration"],
            )
            self.assertEqual(ClaimStatus.UNKNOWN, session.claim_status())

        committed_session = ReceptionSession.restore_from(
            self.serializer.deserialize_json(committed["successor_archive"]),
            committed["service"],
            committed["successor_registration"],
        )
        proposed_session = ReceptionSession.restore_from(
            self.serializer.deserialize_json(proposed["successor_archive"]),
            proposed["service"],
            proposed["successor_registration"],
        )
        self.assertEqual(ClaimStatus.CLAIMED, committed_session.claim_status(evidence=True))
        self.assertEqual(ClaimStatus.UNCLAIMED, proposed_session.claim_status(evidence=True))

    def test_both_evidence_paths_yield_one_claim_and_one_checksum(self):
        committed = self.prepare_trace(commit=True)
        proposed = self.prepare_trace(commit=False)

        # Trace one: successor continues the checksum step only.
        successor_one = ReceptionSession.restore_from(
            self.serializer.deserialize_json(committed["successor_archive"]),
            committed["service"],
            committed["successor_registration"],
        )
        self.assertEqual(ClaimStatus.CLAIMED, successor_one.claim_status(evidence=True))
        self.assertEqual(SessionStatus.FILE_CLAIMED, successor_one.continue_after_handoff().status)
        self.assertEqual(SessionStatus.CHECKSUM_SENT, successor_one.send_checksum().status)
        self.assertEqual(
            SessionStatus.COMPLETE,
            successor_one.receive_checksum(checksum_receipt(self.key, "checksum-a2-1")).status,
        )

        # Trace two: successor claims the still-available file receipt.
        successor_two = ReceptionSession.restore_from(
            self.serializer.deserialize_json(proposed["successor_archive"]),
            proposed["service"],
            proposed["successor_registration"],
        )
        self.assertEqual(ClaimStatus.UNCLAIMED, successor_two.claim_status(evidence=True))
        self.assertEqual(SessionStatus.WAITING_FOR_FILE, successor_two.continue_after_handoff().status)
        self.assertEqual(
            SessionStatus.FILE_CLAIMED,
            successor_two.claim_and_commit(proposed["file"]).status,
        )
        self.assertEqual(SessionStatus.CHECKSUM_SENT, successor_two.send_checksum().status)
        self.assertEqual(
            SessionStatus.COMPLETE,
            successor_two.receive_checksum(checksum_receipt(self.key, "checksum-a2-1")).status,
        )

        for trace, claimant in ((committed, "old-receiver"), (proposed, "new-receiver")):
            service = trace["service"]
            self.assertEqual(1, service.claim_count(self.key))
            self.assertEqual(1, len(service.checksum_requests))
            self.assertEqual(claimant, service.claim(self.key).claimant)

    def _claim_request_id(self, service, key):
        matches = [
            request
            for request in service.claim_requests.values()
            if request.key == key and not request.committed
        ]
        self.assertEqual(1, len(matches))
        return matches[0].request_id


class ReceptionRoutingTest(ReceptionTestCase):
    """Identity, attribution, and compatibility boundaries."""

    def test_same_batch_number_across_suppliers(self):
        a = ReceiptKey("supplier-a", 7, 1)
        b = ReceiptKey("supplier-b", 7, 1)
        sa = ReceptionSession.create("receiver-a", a, self.service)
        sb = ReceptionSession.create("receiver-b", b, self.service)

        self.service.submit(file_receipt(a, "a-1"))
        self.service.submit(file_receipt(b, "b-1"))
        self.assertEqual(SessionStatus.FILE_CLAIMED, sa.claim_and_commit().status)
        self.assertEqual(SessionStatus.FILE_CLAIMED, sb.claim_and_commit().status)
        self.assertEqual("receiver-a", self.service.claim(a).claimant)
        self.assertEqual("receiver-b", self.service.claim(b).claimant)

    def test_checksum_receipt_cannot_substitute_for_file(self):
        key = ReceiptKey("supplier-a", 7, 2)
        session = ReceptionSession.create("old-receiver", key, self.service)
        checksum = checksum_receipt(key, "checksum-first")
        self.service.submit(checksum)

        self.assertEqual(SessionStatus.BLOCKED, session.receive_checksum(checksum).status)
        self.assertEqual(SessionStatus.BLOCKED, session.send_checksum().status)
        self.assertIsNone(self.service.claim(key))

    def test_checksum_before_claim_is_not_applied(self):
        key = ReceiptKey("supplier-a", 7, 2)
        session = ReceptionSession.create("old-receiver", key, self.service)
        checksum = checksum_receipt(key, "checksum-early")
        self.service.submit(checksum)
        self.assertEqual(SessionStatus.BLOCKED, session.receive_checksum(checksum).status)

        file = file_receipt(key, "file-a2-1")
        self.service.submit(file)
        session.claim_and_commit(file)
        # The early checksum is retained but only applies once the request is issued.
        self.assertEqual(SessionStatus.COMPLETE, session.send_checksum().status)
        self.assertEqual("checksum-early", session.applied_checksum_receipt().receipt_id)

    def test_duplicate_receipt_is_attributed(self):
        key = ReceiptKey("supplier-a", 7, 1)
        receipt = file_receipt(key, "file-a1")
        self.assertEqual(SubmissionStatus.ACCEPTED, self.service.submit(receipt).status)
        self.assertEqual(SubmissionStatus.DUPLICATE, self.service.submit(receipt).status)
        attribution = self.service.attribution(key)
        self.assertEqual("file-a1", attribution["canonical"])
        self.assertEqual(1, len(attribution["receipts"]))

    def test_same_id_different_body_conflict_is_attributed(self):
        key = ReceiptKey("supplier-a", 7, 1)
        original = file_receipt(key, "file-a1", name="original.csv")
        changed = file_receipt(key, "file-a1", name="changed.csv")
        self.service.submit(original)
        result = self.service.submit(changed)
        self.assertEqual(SubmissionStatus.CONFLICT, result.status)
        self.assertEqual("file-a1", result.attribution)
        self.assertEqual("original.csv", self.service.canonical_submission(key).receipt.payload["name"])

    def test_fault_receipt_is_retained(self):
        key = ReceiptKey("supplier-a", 7, 1)
        fault = file_receipt(key, "fault-1")
        self.service.submit(fault, fault="corrupt transport")
        attribution = self.service.attribution(key)
        self.assertEqual(1, len(attribution["faults"]))
        self.assertEqual([], list(self.service.submissions_for(key)))

    def test_supplier_b_message_does_not_touch_supplier_a(self):
        a = ReceiptKey("supplier-a", 7, 1)
        b = ReceiptKey("supplier-b", 7, 1)
        session_a = ReceptionSession.create("receiver-a", a, self.service)
        self.service.submit(file_receipt(a, "a-1"))
        session_a.claim_and_commit()

        self.assertEqual(SubmissionStatus.ACCEPTED, self.service.submit(file_receipt(b, "b-1")).status)
        self.assertEqual("a-1", self.service.claim(a).receipt_id)
        self.assertIsNone(self.service.claim(b))
        self.assertEqual(1, len(self.service.submissions_for(a)))

    def test_late_same_named_registration_is_stale(self):
        key = ReceiptKey("supplier-a", 7, 2)
        original_registration = self.register(key, "receiver")
        successor_registration = self.register(key, "receiver-v2")
        self.coordinator.request(key, "receiver", "receiver-v2")
        self.coordinator.confirm(key, successor_registration)

        late = self.register(key, "receiver")
        self.assertFalse(self.service.authority_allows(key, late))
        self.assertTrue(self.service.authority_allows(key, successor_registration))
        self.assertFalse(self.service.authority_allows(key, original_registration))

    def test_repeated_claim_request_is_idempotent(self):
        key = ReceiptKey("supplier-a", 7, 1)
        session = ReceptionSession.create("receiver", key, self.service)
        file = file_receipt(key, "file-a1")
        self.service.submit(file)

        first = session.claim_and_commit(file)
        replay = session.claim_and_commit(file)
        self.assertEqual(SessionStatus.FILE_CLAIMED, first.status)
        self.assertEqual(SessionStatus.FILE_CLAIMED, replay.status)
        self.assertEqual(first.claim_sequence, replay.claim_sequence)
        self.assertEqual(1, self.service.claim_count(key))

    def test_stale_endpoint_cannot_send_checksum_after_successor_claim(self):
        key = ReceiptKey("supplier-a", 7, 2)
        old_registration = self.register(key, "old-receiver", "old-incarnation")
        successor_registration = self.register(key, "new-receiver", "new-incarnation")
        old = ReceptionSession.create("old-receiver", key, self.service, old_registration)
        successor = ReceptionSession.create("new-receiver", key, self.service, successor_registration)
        file = file_receipt(key, "file-a2-1")
        self.service.submit(file)
        old.request_file_claim(file)

        self.coordinator.request(key, "old-receiver", "new-receiver")
        self.coordinator.confirm(key, successor_registration)
        self.assertEqual(SessionStatus.FILE_CLAIMED, successor.claim_and_commit(file).status)

        self.assertEqual(SessionStatus.DISENFRANCHISED, old.commit_file_claim().status)
        self.assertEqual(SessionStatus.DISENFRANCHISED, old.send_checksum().status)
        self.assertEqual("new-receiver", self.service.claim(key).claimant)

    def test_legitimate_supplier_b_message_after_supplier_a_handoff(self):
        a = ReceiptKey("supplier-a", 7, 2)
        b = ReceiptKey("supplier-b", 7, 1)
        old_registration = self.register(a, "old-receiver", "old-incarnation")
        successor_registration = self.register(a, "new-receiver", "new-incarnation")
        old = ReceptionSession.create("old-receiver", a, self.service, old_registration)
        self.service.submit(file_receipt(a, "a-1"))
        old.claim_and_commit()
        self.coordinator.request(a, "old-receiver", "new-receiver")
        self.coordinator.confirm(a, successor_registration)

        supplier_b = ReceptionSession.create("receiver-b", b, self.service)
        self.assertEqual(SubmissionStatus.ACCEPTED, supplier_b.receive(file_receipt(b, "b-1")).status)
        self.assertEqual(SessionStatus.FILE_CLAIMED, supplier_b.claim_and_commit().status)
        self.assertEqual("old-receiver", self.service.claim(a).claimant)
        self.assertEqual("receiver-b", self.service.claim(b).claimant)

    def test_no_handoff_message_flow_still_works(self):
        key = ReceiptKey("supplier-a", 7, 9)
        session = ReceptionSession.create("receiver", key, self.service)
        file = file_receipt(key, "file-a9")

        self.assertEqual(SubmissionStatus.ACCEPTED, session.receive(file).status)
        self.assertEqual(SessionStatus.FILE_CLAIMED, session.claim_and_commit().status)
        self.assertEqual(SessionStatus.CHECKSUM_SENT, session.send_checksum().status)
        self.assertEqual(
            SessionStatus.COMPLETE,
            session.receive_checksum(checksum_receipt(key, "checksum-a9")).status,
        )

    def test_successor_workflow_resumes_at_checksum_step(self):
        key = ReceiptKey("supplier-a", 7, 2)
        old_registration = self.register(key, "old-receiver", "old-incarnation")
        successor_registration = self.register(key, "new-receiver", "new-incarnation")

        old_workflow = self.new_workflow()
        old = self.start_session(key, "old-receiver", old_workflow, old_registration)
        successor_workflow = self.new_workflow()
        successor = self.start_session(key, "new-receiver", successor_workflow, successor_registration)
        successor_archive = successor.save_archive()

        file = file_receipt(key, "file-a2-1")
        self.service.submit(file)
        self.assertEqual(SessionStatus.FILE_CLAIMED, old.deliver(file).status)
        old_archive = old.save_archive()

        self.coordinator.request(key, "old-receiver", "new-receiver")
        self.coordinator.confirm(key, successor_registration)

        restored_old = self.restore_session(old_archive, old_registration)
        restored_new = self.restore_session(successor_archive, successor_registration)
        self.assertEqual(SessionStatus.DISENFRANCHISED, restored_old.send_checksum().status)
        self.assertEqual(SessionStatus.FILE_CLAIMED, restored_new.continue_after_handoff().status)
        self.assertEqual(["Checksum Received"], self.waiting_names(restored_new.workflow))

        self.assertEqual(SessionStatus.CHECKSUM_SENT, restored_new.send_checksum().status)
        self.assertEqual(
            SessionStatus.COMPLETE,
            restored_new.deliver(checksum_receipt(key, "checksum-a2-1")).status,
        )
        self.assertTrue(restored_new.workflow.completed)
        self.assertEqual(1, self.service.claim_count(key))
        self.assertEqual(1, len(self.service.checksum_requests))


class ReceptionBpmnTest(ReceptionTestCase):
    """The reception state must survive real workflow serialization."""

    def test_workflow_waits_for_file_then_checksum(self):
        key = ReceiptKey("supplier-a", 7, 2)
        workflow = self.new_workflow()
        instance = self.start_session(key, "receiver", workflow)
        self.assertEqual(["File Received"], self.waiting_names(workflow))

        instance.deliver(file_receipt(key, "file-a2-1"))
        workflow.do_engine_steps()
        self.assertEqual(["Checksum Received"], self.waiting_names(workflow))

        instance.send_checksum()
        instance.deliver(checksum_receipt(key, "checksum-a2-1"))
        workflow.do_engine_steps()
        self.assertTrue(workflow.completed)

    def test_checksum_requires_explicit_request_after_claim(self):
        key = ReceiptKey("supplier-a", 7, 2)
        workflow = self.new_workflow()
        instance = self.start_session(key, "receiver", workflow)
        instance.deliver(file_receipt(key, "file-a2-1"))
        workflow.do_engine_steps()

        # The file is claimed but no checksum request was emitted yet.
        self.assertIsNone(self.service.checksum_request(key))
        self.assertEqual(SessionStatus.BLOCKED, instance.deliver(
            checksum_receipt(key, "checksum-a2-1")
        ).status)
        # Once the legitimate request is issued, the retained checksum completes it.
        self.assertEqual(SessionStatus.COMPLETE, instance.send_checksum().status)
        self.assertEqual("checksum-a2-1", instance.session.applied_checksum_receipt().receipt_id)

    def test_reception_state_survives_archive_restore(self):
        key = ReceiptKey("supplier-a", 7, 2)
        workflow = self.new_workflow()
        instance = self.start_session(key, "receiver", workflow)
        instance.deliver(file_receipt(key, "file-a2-1"))
        workflow.do_engine_steps()
        archive = instance.save_archive()
        registration = instance.session.registration

        restored = self.restore_session(archive, registration)
        self.assertEqual(SessionStatus.FILE_CLAIMED.value, restored.session.state.status)
        restored.workflow.do_engine_steps()
        self.assertEqual(["Checksum Received"], self.waiting_names(restored.workflow))

        self.assertEqual(SessionStatus.CHECKSUM_SENT, restored.send_checksum().status)
        restored.deliver(checksum_receipt(key, "checksum-a2-1"))
        restored.workflow.do_engine_steps()
        self.assertTrue(restored.workflow.completed)

    def test_checksum_first_does_not_advance_file_wait(self):
        key = ReceiptKey("supplier-a", 7, 2)
        workflow = self.new_workflow()
        instance = self.start_session(key, "receiver", workflow)
        instance.deliver(checksum_receipt(key, "checksum-early"))
        workflow.do_engine_steps()
        self.assertEqual(["File Received"], self.waiting_names(workflow))

        instance.deliver(file_receipt(key, "file-a2-1"))
        workflow.do_engine_steps()
        self.assertEqual(["Checksum Received"], self.waiting_names(workflow))

    def test_local_message_event_usage_is_unchanged(self):
        workflow = self.new_workflow()
        workflow.do_engine_steps()
        definition = [t for t in workflow.get_tasks(state=TaskState.WAITING)][0].task_spec.event_definition
        workflow.catch(BpmnEvent(definition, {"name": "x"}))
        workflow.do_engine_steps()
        self.assertEqual(["Checksum Received"], self.waiting_names(workflow))
