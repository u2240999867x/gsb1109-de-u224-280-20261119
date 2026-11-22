# Recovery / replay semantics for the parallel clean -> join -> deposit flow.
#
# Branch A (clean_a -> approve_a) and branch B (clean_b) merge at a parallel
# join; the merged result is deposited to an independent repository.  The
# approval processed on branch A can later be replaced by a newer decision
# (e.g. a late revocation).  Whether the old result may be kept depends on
# whether the repository actually accepted it before the revocation:
#
#   * accepted  -> keep the original result and its approval evidence; the
#                  revocation may only change its status through a note that
#                  explicitly references the old publication.
#   * not accepted -> the revoked result must not be (re)submitted; only the
#                  revocation and the existing cleaning evidence are kept.
#
# While the repository is recovering it may answer 'unknown'; during that
# window the client must neither resend nor cancel the undetermined deposit
# task, and must not erase branch B's completed output.  Replayed receipts
# and repeated recoveries must not produce extra publications or revocations.

import json
import os
import unittest
from copy import deepcopy

from SpiffWorkflow import TaskState
from SpiffWorkflow.bpmn.workflow import BpmnWorkflow
from SpiffWorkflow.bpmn.parser import BpmnValidator
from SpiffWorkflow.bpmn.serializer import BpmnWorkflowSerializer

from .BpmnLoaderForTests import TestBpmnParser, SERIALIZER_CONFIG
from .BpmnWorkflowTestCase import BpmnWorkflowTestCase

DATA_DIR = os.path.join(os.path.dirname(__file__), 'data')
BPMN_FILE = os.path.join(DATA_DIR, 'recovery_deposit.bpmn')
ARCHIVE_FILE = os.path.join(DATA_DIR, 'recovery_deposit_archive.json')

registry = BpmnWorkflowSerializer.configure(SERIALIZER_CONFIG)
serializer = BpmnWorkflowSerializer(registry)

SUBMISSION_ID = 'SUB-0001'

# The original approval (processed before the crash) and the newer decision
# that replaces it.  `decided_at` is a logical clock; a decision only
# supersedes an earlier one when its clock value is strictly greater.
APPROVAL = {
    'id': 'DEC-APPROVAL-1',
    'kind': 'approval',
    'decided_at': 5,
    'evidence': {'approver': 'approver-1', 'comment': 'cleansing approved'},
}
REVOCATION = {
    'id': 'DEC-REVOKE-1',
    'kind': 'revocation',
    'decided_at': 20,
    'evidence': {'approver': 'approver-2', 'comment': 'approval withdrawn'},
}
# The repository accepted the pre-crash submission at t=10, i.e. after the
# approval (t=5) but before the revocation (t=20).
REPO_ACCEPTED_AT = 10


def load_spec(process_name):
    parser = TestBpmnParser(validator=BpmnValidator())
    parser.add_bpmn_files_by_glob(BPMN_FILE)
    return parser.get_spec(process_name), parser.get_subprocess_specs(process_name)


def find_task(workflow, name, state=None):
    for task in workflow.get_tasks():
        if task.task_spec.name == name or task.task_spec.bpmn_name == name:
            if state is None or task.state == state:
                return task
    return None


class MockRepository:
    """The independent repository system.

    It can tell which client submission an acceptance belongs to, but while
    it is recovering it may only answer 'unknown'.  All of its write APIs
    are idempotent so that replayed receipts and repeated recoveries are
    absorbed without duplicating publications or revocation notes.
    """

    def __init__(self):
        self.submissions = {}       # submission_id -> payload actually received
        self.acceptances = {}       # submission_id -> acceptance record
        self.publications = {}      # publication_id -> published result
        self.revocation_notes = {}  # note_id -> revocation note
        self.recovering = False
        self._publication_seq = 0

    def receive_submission(self, submission_id, payload, accepted_at):
        """Accept a client submission; idempotent per submission id."""
        acceptance = self.acceptances.get(submission_id)
        if acceptance is not None:
            return acceptance['receipt']
        self.submissions[submission_id] = deepcopy(payload)
        self._publication_seq += 1
        publication_id = 'PUB-%04d' % self._publication_seq
        receipt = {
            'submission_id': submission_id,
            'publication_id': publication_id,
            'accepted_at': accepted_at,
        }
        self.acceptances[submission_id] = {'receipt': receipt, 'accepted_at': accepted_at}
        self.publications[publication_id] = {
            'payload': deepcopy(payload),
            'status': 'published',
            'status_notes': [],
        }
        return receipt

    def query_submission(self, submission_id):
        """Report whether a client submission was accepted.

        Returns 'unknown' while the repository is still recovering.
        """
        if self.recovering:
            return 'unknown'
        return 'accepted' if submission_id in self.acceptances else 'not_found'

    def receipt_for(self, submission_id):
        return self.acceptances[submission_id]['receipt']

    def replay_receipt(self, submission_id):
        """Re-deliver a receipt that was already issued (duplicate delivery)."""
        return dict(self.acceptances[submission_id]['receipt'])

    def record_revocation_note(self, note):
        """Apply a revocation note; idempotent per note id.

        A note only changes the status of the publication it explicitly
        references; the published payload is never altered or removed.
        """
        if note['note_id'] in self.revocation_notes:
            return self.revocation_notes[note['note_id']]
        publication = self.publications[note['references_publication']]
        publication['status'] = 'revoked'
        publication['status_notes'].append(note['note_id'])
        self.revocation_notes[note['note_id']] = deepcopy(note)
        return note


class DepositCoordinator:
    """Client-side deposit logic.

    All of its state lives in ``workflow.data['deposit_record']`` so that it
    survives serialization and is restored together with the workflow.
    """

    def __init__(self, workflow, repository=None):
        self.workflow = workflow
        self.repository = repository

    @property
    def record(self):
        return self.workflow.data.setdefault('deposit_record', {
            'submission_id': None,
            'payload': None,
            'status': 'pending',
            'publication_id': None,
            'receipt': None,
            'revocation_note_id': None,
            'decisions': [],
            'output': None,
            'audit': [],
        })

    def _audit(self, event, **fields):
        entry = {'event': event}
        entry.update(fields)
        self.record['audit'].append(entry)

    def current_decision(self):
        decisions = self.record['decisions']
        if not decisions:
            return None
        return max(decisions, key=lambda d: d['decided_at'])

    def apply_decision(self, decision):
        """Record an approval/revocation decision.

        Resent decisions (same id) are duplicates, and decisions that are
        not newer than the current one are stale; neither may replace the
        decision already in effect.
        """
        decisions = self.record['decisions']
        if any(d['id'] == decision['id'] for d in decisions):
            self._audit('decision_duplicate_ignored', decision_id=decision['id'])
            return False
        current = self.current_decision()
        if current is not None and decision['decided_at'] <= current['decided_at']:
            self._audit('decision_stale_ignored', decision_id=decision['id'])
            return False
        decisions.append(deepcopy(decision))
        self._audit('decision_applied', decision_id=decision['id'], kind=decision['kind'])
        return True

    def prepare_submission(self, submission_id):
        """Snapshot the merged result and mark the deposit as sent.

        Called before the crash; whether the repository actually received
        and accepted the submission is only known after recovery.
        """
        record = self.record
        clean_a = find_task(self.workflow, 'clean_a', TaskState.COMPLETED)
        clean_b = find_task(self.workflow, 'clean_b', TaskState.COMPLETED)
        approve_a = find_task(self.workflow, 'approve_a', TaskState.COMPLETED)
        record['payload'] = {
            'a_cleaned': deepcopy(clean_a.data['a_cleaned']),
            'b_cleaned': deepcopy(clean_b.data['b_cleaned']),
            'approval_evidence': {
                'decision_id': approve_a.data['decision_id'],
                'approver': approve_a.data['approver'],
            },
        }
        record['submission_id'] = submission_id
        record['status'] = 'in_flight'
        self._audit('submission_prepared', submission_id=submission_id)

    def recover(self):
        """Reconcile the in-flight deposit with the repository.

        While the repository answers 'unknown' nothing is resent, cancelled
        or erased; the outcome is only decided once the repository gives a
        definite answer.
        """
        record = self.record
        if record['status'] != 'in_flight':
            self._audit('recovery_noop', status=record['status'])
            return record['status']
        answer = self.repository.query_submission(record['submission_id'])
        if answer == 'unknown':
            self._audit('query_unknown_defer', submission_id=record['submission_id'])
            return 'unknown'
        if answer == 'accepted':
            self.record_receipt(self.repository.receipt_for(record['submission_id']))
            self._apply_pending_revocation()
            return record['status']
        # The old version was never accepted by the repository.
        decision = self.current_decision()
        if decision is not None and decision['kind'] == 'revocation':
            # The approval was revoked: the old result must not be
            # submitted; only the revocation and the cleaning evidence
            # collected so far are kept.
            record['status'] = 'revoked_unpublished'
            record['output'] = {
                'basis': 'cleaning_evidence',
                'references': ['a_cleaned', 'b_cleaned'],
                'revoked_by': decision['id'],
            }
            self._audit('withheld_revoked_submission', decision_id=decision['id'])
        else:
            receipt = self.repository.receive_submission(
                record['submission_id'], record['payload'], accepted_at=decision['decided_at'])
            self.record_receipt(receipt)
            self._audit('resubmitted_after_not_found', submission_id=record['submission_id'])
        return record['status']

    def record_receipt(self, receipt):
        """Record an acceptance receipt; replayed receipts are absorbed."""
        record = self.record
        assert receipt['submission_id'] == record['submission_id']
        if record['status'] == 'published' and record['publication_id'] == receipt['publication_id']:
            self._audit('duplicate_receipt_ignored', publication_id=receipt['publication_id'])
            return
        record['publication_id'] = receipt['publication_id']
        record['receipt'] = dict(receipt)
        record['status'] = 'published'
        record['output'] = {
            'basis': 'publication',
            'publication_id': receipt['publication_id'],
            'accepted_at': receipt['accepted_at'],
        }
        self._audit('receipt_recorded', publication_id=receipt['publication_id'])

    def _apply_pending_revocation(self):
        record = self.record
        decision = self.current_decision()
        if decision is None or decision['kind'] != 'revocation':
            return
        # The old version was accepted, so the revocation may only change
        # its status through a note that explicitly references the old
        # publication.  The note id is deterministic, so repeated
        # recoveries regenerate the same note and the repository absorbs
        # the duplicate.
        note = {
            'note_id': 'REVNOTE-%s' % decision['id'],
            'references_publication': record['publication_id'],
            'decision_id': decision['id'],
            'effect': 'status_revoked',
        }
        self.repository.record_revocation_note(note)
        record['revocation_note_id'] = note['note_id']
        record['output']['status_note'] = note['note_id']
        self._audit('revocation_note_recorded',
                    note_id=note['note_id'], references=record['publication_id'])


def build_archive_state():
    """Run the workflow to the archive point and serialize it.

    Archive point: branch A has processed its approval, branch B's cleaned
    output has reached the join, and the deposit has been sent but not yet
    recorded as complete.
    """
    spec, subprocesses = load_spec('RecoveryDeposit')
    workflow = BpmnWorkflow(spec, subprocesses)
    workflow.do_engine_steps()

    approve_a = find_task(workflow, 'approve_a', TaskState.READY)
    approve_a.set_data(decision='approved', approver='approver-1', decision_id=APPROVAL['id'])
    approve_a.run()
    workflow.do_engine_steps()

    coordinator = DepositCoordinator(workflow)
    coordinator.apply_decision(APPROVAL)
    coordinator.prepare_submission(SUBMISSION_ID)
    return serializer.to_dict(workflow)


def load_archive():
    with open(ARCHIVE_FILE) as handle:
        return json.load(handle)


class RecoveryDepositTest(BpmnWorkflowTestCase):

    def restore_archive(self):
        self.workflow = serializer.from_dict(load_archive())
        return self.workflow

    def deposit_task(self, state=TaskState.READY):
        return find_task(self.workflow, 'deposit_to_repository', state)

    def complete_deposit_task(self):
        record = self.workflow.data['deposit_record']
        task = self.deposit_task(TaskState.READY)
        task.set_data(outcome=record['status'])
        task.run()
        self.workflow.do_engine_steps()

    def assert_unknown_window_preserves_state(self, repository, submissions_before):
        """During 'unknown' the client must not resend, cancel or erase."""
        record = self.workflow.data['deposit_record']
        # no resubmission and no cancellation of the undetermined task
        self.assertEqual(submissions_before, len(repository.submissions))
        self.assertEqual('in_flight', record['status'])
        self.assertIsNotNone(self.deposit_task(TaskState.READY))
        # branch B's completed output is preserved
        clean_b = find_task(self.workflow, 'clean_b', TaskState.COMPLETED)
        self.assertEqual(['b1', 'b2', 'b3'], clean_b.data['b_cleaned']['rows'])
        self.assertEqual(['b1', 'b2', 'b3'], record['payload']['b_cleaned']['rows'])

    def test_archive_snapshot_point(self):
        # The committed archive holds: A's approval processed, B at the
        # join, and the deposit sent but not recorded complete.
        self.restore_archive()
        self.assertIsNotNone(find_task(self.workflow, 'clean_a', TaskState.COMPLETED))
        self.assertIsNotNone(find_task(self.workflow, 'approve_a', TaskState.COMPLETED))
        self.assertIsNotNone(find_task(self.workflow, 'clean_b', TaskState.COMPLETED))
        self.assertIsNotNone(self.deposit_task(TaskState.READY))
        self.assertFalse(self.workflow.is_completed())
        record = self.workflow.data['deposit_record']
        self.assertEqual('in_flight', record['status'])
        self.assertEqual(SUBMISSION_ID, record['submission_id'])
        self.assertEqual([APPROVAL['id']], [d['id'] for d in record['decisions']])
        self.assertEqual('approver-1', record['payload']['approval_evidence']['approver'])

    def test_recovery_old_version_accepted_before_revocation(self):
        repository = MockRepository()
        self.restore_archive()
        record = self.workflow.data['deposit_record']
        # The repository accepted the pre-crash submission at t=10, then
        # crashed itself; while recovering it can only answer 'unknown'.
        repository.receive_submission(SUBMISSION_ID, record['payload'], accepted_at=REPO_ACCEPTED_AT)
        repository.recovering = True
        coordinator = DepositCoordinator(self.workflow, repository)

        # The original approval is resent after the restore: it is a
        # duplicate and must not be applied twice.
        self.assertFalse(coordinator.apply_decision(APPROVAL))
        self.assertEqual([APPROVAL['id']], [d['id'] for d in record['decisions']])
        # The newer revocation replaces the approval.
        self.assertTrue(coordinator.apply_decision(REVOCATION))
        self.assertEqual(REVOCATION['id'], coordinator.current_decision()['id'])

        # First query: unknown.  Nothing is resent, cancelled or erased.
        self.assertEqual('unknown', coordinator.recover())
        self.assert_unknown_window_preserves_state(repository, submissions_before=1)

        # The repository finishes reconciling: the old version was accepted.
        repository.recovering = False
        self.assertEqual('published', coordinator.recover())
        publication = repository.publications['PUB-0001']
        # The original result and its approval evidence are kept; the
        # revocation only changed the status via a note that explicitly
        # references the old publication.
        self.assertEqual('revoked', publication['status'])
        self.assertEqual(record['payload'], publication['payload'])
        self.assertEqual('approver-1', publication['payload']['approval_evidence']['approver'])
        self.assertEqual(['REVNOTE-DEC-REVOKE-1'], publication['status_notes'])
        note = repository.revocation_notes['REVNOTE-DEC-REVOKE-1']
        self.assertEqual('PUB-0001', note['references_publication'])
        self.assertEqual('publication', record['output']['basis'])
        self.assertEqual('PUB-0001', record['output']['publication_id'])
        self.complete_deposit_task()
        self.assertTrue(self.workflow.is_completed())

        # The repository replays the same acceptance receipt: no extra
        # publication and no extra revocation.
        coordinator.record_receipt(repository.replay_receipt(SUBMISSION_ID))
        self.assertEqual(1, len(repository.publications))
        self.assertEqual(1, len(repository.revocation_notes))
        self.assertEqual(1, len(repository.submissions))

        # The workflow is recovered again from the same archive and the
        # whole recovery repeats against the same repository: still no
        # extra publication or revocation.
        self.restore_archive()
        coordinator = DepositCoordinator(self.workflow, repository)
        coordinator.apply_decision(REVOCATION)
        self.assertEqual('published', coordinator.recover())
        coordinator.record_receipt(repository.replay_receipt(SUBMISSION_ID))
        self.assertEqual(1, len(repository.publications))
        self.assertEqual(1, len(repository.revocation_notes))
        self.assertEqual(1, len(repository.submissions))
        self.assertEqual('revoked', repository.publications['PUB-0001']['status'])

    def test_recovery_old_version_never_accepted(self):
        repository = MockRepository()
        self.restore_archive()
        repository.recovering = True
        coordinator = DepositCoordinator(self.workflow, repository)
        record = self.workflow.data['deposit_record']

        # Resent approval is a duplicate; the revocation supersedes it.
        self.assertFalse(coordinator.apply_decision(APPROVAL))
        self.assertTrue(coordinator.apply_decision(REVOCATION))

        # First query: unknown.  The repository has never seen the
        # submission, and the client must not resend it while unsure.
        self.assertEqual('unknown', coordinator.recover())
        self.assert_unknown_window_preserves_state(repository, submissions_before=0)

        # Final reconciliation: the old version was never accepted, so the
        # revoked result is not submitted; only the revocation and the
        # existing cleaning evidence are kept.
        repository.recovering = False
        self.assertEqual('revoked_unpublished', coordinator.recover())
        self.assertEqual(0, len(repository.submissions))
        self.assertEqual(0, len(repository.publications))
        self.assertEqual(0, len(repository.revocation_notes))
        self.assertEqual('cleaning_evidence', record['output']['basis'])
        self.assertEqual(['a_cleaned', 'b_cleaned'], record['output']['references'])
        self.assertEqual(REVOCATION['id'], record['output']['revoked_by'])
        # The revocation and both branches' cleaning evidence survive.
        self.assertEqual([APPROVAL['id'], REVOCATION['id']],
                         [d['id'] for d in record['decisions']])
        self.assertEqual(['a1', 'a2'], record['payload']['a_cleaned']['rows'])
        self.assertEqual(['b1', 'b2', 'b3'], record['payload']['b_cleaned']['rows'])
        self.complete_deposit_task()
        self.assertTrue(self.workflow.is_completed())

        # Repeated recovery from the same archive still submits nothing.
        self.restore_archive()
        coordinator = DepositCoordinator(self.workflow, repository)
        coordinator.apply_decision(REVOCATION)
        self.assertEqual('revoked_unpublished', coordinator.recover())
        self.assertEqual(0, len(repository.submissions))
        self.assertEqual(0, len(repository.publications))
        self.assertEqual(0, len(repository.revocation_notes))

    def test_revocation_arrives_before_approval_resend(self):
        repository = MockRepository()
        self.restore_archive()
        record = self.workflow.data['deposit_record']
        repository.receive_submission(SUBMISSION_ID, record['payload'], accepted_at=REPO_ACCEPTED_AT)
        repository.recovering = True
        coordinator = DepositCoordinator(self.workflow, repository)

        # The updated revocation arrives first; the resent approval only
        # arrives afterwards and is absorbed as a duplicate.
        self.assertTrue(coordinator.apply_decision(REVOCATION))
        self.assertFalse(coordinator.apply_decision(APPROVAL))
        self.assertEqual(REVOCATION['id'], coordinator.current_decision()['id'])

        self.assertEqual('unknown', coordinator.recover())
        self.assert_unknown_window_preserves_state(repository, submissions_before=1)
        repository.recovering = False
        self.assertEqual('published', coordinator.recover())

        # The recorded history shows: the actual order in which decisions
        # were applied, that the old version was accepted, the revocation
        # that was received, and the basis of the output references.
        self.assertEqual([APPROVAL['id'], REVOCATION['id']],
                         [d['id'] for d in record['decisions']])
        events = [(e['event'], e.get('decision_id')) for e in record['audit']]
        self.assertLess(
            events.index(('decision_applied', REVOCATION['id'])),
            events.index(('decision_duplicate_ignored', APPROVAL['id'])))
        acceptance = repository.acceptances[SUBMISSION_ID]
        self.assertLess(acceptance['accepted_at'], REVOCATION['decided_at'])
        self.assertEqual([REVOCATION['id']],
                         [d['id'] for d in record['decisions'] if d['kind'] == 'revocation'])
        self.assertEqual('publication', record['output']['basis'])
        self.assertEqual('PUB-0001', record['output']['publication_id'])
        self.assertEqual('REVNOTE-DEC-REVOKE-1', record['output']['status_note'])
        note = repository.revocation_notes['REVNOTE-DEC-REVOKE-1']
        self.assertEqual('PUB-0001', note['references_publication'])

    def test_no_approval_node_parallel_join_unaffected(self):
        # Without an approval node the parallel join works as usual.
        spec, subprocesses = load_spec('RecoveryDepositNoApproval')
        self.workflow = BpmnWorkflow(spec, subprocesses)
        self.workflow.do_engine_steps()
        self.assertIsNotNone(find_task(self.workflow, 'clean_a', TaskState.COMPLETED))
        self.assertIsNotNone(find_task(self.workflow, 'clean_b', TaskState.COMPLETED))
        deposit = find_task(self.workflow, 'deposit_to_repository', TaskState.READY)
        self.assertIsNotNone(deposit)
        deposit.run()
        self.workflow.do_engine_steps()
        self.assertTrue(self.workflow.is_completed())


def suite():
    return unittest.TestLoader().loadTestsFromTestCase(RecoveryDepositTest)


if __name__ == '__main__':
    unittest.TextTestRunner(verbosity=2).run(suite())
