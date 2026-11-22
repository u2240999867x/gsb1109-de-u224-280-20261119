"""Acceptance scenarios for inbound receiving review.

The key property under test is that a line is identified by its stable
material line number and by the reservation identity allocated for a concrete
preverification -- never by position in the form and never merely by the
visible station/quantity pair.
"""

import unittest

from SpiffWorkflow.inbound import (
    ArchivedVersionError,
    DeliveryError,
    FormVersion,
    InboundReviewBatch,
    InboundReceivingService,
    Line,
    PreverificationError,
    Stage,
    new_ledger,
)
from SpiffWorkflow.inbound.reservations import DeliveryStatus


def make_form(version=1, notes='initial remark', lines=None):
    lines = lines or [
        Line(line_no=1, station='S1', quantity=10, note='first'),
        Line(line_no=2, station='S2', quantity=20, note='second'),
    ]
    return FormVersion(version=version, form_id='BATCH-1', lines=lines, notes=notes)


class BaseScenario(unittest.TestCase):

    def setUp(self):
        self.ledger = new_ledger({'S1': 100, 'S2': 100})
        self.service = InboundReceivingService(self.ledger)
        self.batch = self.service.create_batch('BATCH-1', make_form())

    def preverify_all(self):
        return self.service.preverify(self.batch)

    def confirm_complete(self):
        self.preverify_all()
        return self.service.confirm(self.batch)


class MergeTest(BaseScenario):

    def test_metadata_only_edit_does_not_allocate_reservations(self):
        identities = self.preverify_all()
        before = len(self.ledger.reservations_for_form('BATCH-1'))

        session = self.service.begin_edit(self.batch, 'alice')
        session.reorder([2, 1]).set_notes('')
        session.commit()

        after = len(self.ledger.reservations_for_form('BATCH-1'))
        self.assertEqual(before, after)
        self.assertEqual(self.batch.form.notes, '')
        self.assertEqual([line.line_no for line in self.batch.form.lines], [2, 1])
        for line_no, identity in identities.items():
            self.assertEqual(self.batch.identity_for(line_no).reservation_id,
                             identity.reservation_id)

    def test_merge_is_by_line_number_not_position(self):
        # Alice takes the current version, reorders rows and clears the remark.
        alice = self.service.begin_edit(self.batch, 'alice')
        alice.reorder([2, 1]).set_notes('')
        alice.commit()

        # Bob holds the old draft and edits the station of the material line
        # that used to be second.  That is line 2, although it now displays
        # first after Alice's reorder.
        old_draft = make_form(notes='initial remark')
        bob = self.service.begin_edit(self.batch, 'bob', base=old_draft)
        self.assertEqual(bob.candidate.lines[0].line_no, 1)
        bob.set_line(2, station='S3').commit()

        self.ledger.set_quota('S3', 100)
        merged = self.batch.form
        self.assertEqual([line.line_no for line in merged.lines], [2, 1],
                         'row order from the newer draft is preserved')
        self.assertEqual(merged.notes, '', 'remark cleared in the newer draft is preserved')
        self.assertEqual(merged.line(2).station, 'S3', 'station edit follows the line number')
        self.assertEqual(merged.line(1).station, 'S1')

    def test_station_change_allocates_a_new_identity_for_that_line(self):
        identities = self.preverify_all()
        session = self.service.begin_edit(self.batch, 'bob')
        session.set_line(2, station='S3').commit()

        self.assertIsNotNone(self.batch.identity_for(2))
        self.assertNotEqual(self.batch.identity_for(2).reservation_id,
                            identities[2].reservation_id)
        self.assertEqual(self.batch.identity_for(1).reservation_id,
                         identities[1].reservation_id)


class PreverificationTest(BaseScenario):

    def test_incomplete_form_cannot_be_preverified(self):
        form = make_form(lines=[
            Line(line_no=1, station='S1', quantity=10),
            Line(line_no=2, station=None, quantity=20),
        ])
        batch = self.service.create_batch('BATCH-2', form)
        with self.assertRaises(Exception):
            self.service.preverify(batch)

    def test_every_preverification_allocates_fresh_identities(self):
        first = self.preverify_all()
        second = self.service.preverify(self.batch, refresh=True)
        self.assertNotEqual(first[1].reservation_id, second[1].reservation_id)
        self.assertNotEqual(first[2].reservation_id, second[2].reservation_id)

    def test_reentering_original_values_is_a_new_identity(self):
        original = self.preverify_all()
        old_identity = self.batch.identity_for(2)

        # Leave the station and come back: visible values are identical.
        session = self.service.begin_edit(self.batch, 'bob')
        session.set_line(2, station='S3').commit()
        session = self.service.begin_edit(self.batch, 'bob')
        session.set_line(2, station='S2').commit()

        current = self.batch.identity_for(2)
        self.assertEqual(current.station, old_identity.station)
        self.assertEqual(current.quantity, old_identity.quantity)
        self.assertNotEqual(current.reservation_id, old_identity.reservation_id)

        # The old confirmation cannot be used for the new identity.
        with self.assertRaises(PreverificationError):
            self.batch.assert_identity_current(2, old_identity)


class ConfirmationTest(BaseScenario):

    def test_confirmation_snapshots_lines_and_identities(self):
        identities = self.preverify_all()
        confirmed = self.service.confirm(self.batch)
        self.assertEqual(confirmed['version'], 1)
        for line_no, identity in identities.items():
            self.assertEqual(confirmed['identities'][line_no]['reservation_id'],
                             identity.reservation_id)
        self.assertEqual(self.batch.stage, Stage.CONFIRMED)

    def test_confirm_requires_live_identity_matching_visible_values(self):
        self.preverify_all()
        # Change the station without allocating a new identity, emulating a
        # form edit that did not go through preverification.
        self.batch.form.lines[1].station = 'S3'
        with self.assertRaises(PreverificationError):
            self.batch.confirm()


class DeliveryTest(BaseScenario):

    def test_delivery_sends_all_station_reservations_in_one_request(self):
        self.confirm_complete()
        outcomes = self.service.deliver(self.batch)

        self.assertEqual([o.station for o in outcomes], ['S1', 'S2'])
        self.assertTrue(all(o.is_accepted for o in outcomes))
        requests = self.ledger.requests_for_form('BATCH-1')
        self.assertEqual(len(requests), 2)
        self.assertEqual(requests[0].reservation_ids,
                         [self.batch.identity_for(1).reservation_id])
        self.assertEqual(requests[1].reservation_ids,
                         [self.batch.identity_for(2).reservation_id])

    def test_delivery_twice_does_not_duplicate_requests(self):
        self.confirm_complete()
        self.service.deliver(self.batch)
        self.service.deliver(self.batch)
        self.assertEqual(len(self.ledger.requests_for_form('BATCH-1')), 2)

    def test_two_lines_at_same_station_share_one_request(self):
        form = make_form(lines=[
            Line(line_no=1, station='S1', quantity=10),
            Line(line_no=2, station='S1', quantity=20),
        ])
        batch = self.service.create_batch('BATCH-3', form)
        self.service.preverify(batch)
        self.service.confirm(batch)
        self.service.deliver(batch)
        requests = self.ledger.requests_for_form('BATCH-3')
        self.assertEqual(len(requests), 1)
        self.assertEqual(len(requests[0].reservation_ids), 2)


class UnknownOutcomeTest(BaseScenario):

    def _deliver_with_second_station_unknown(self):
        self.ledger = new_ledger({'S1': 100, 'S2': 100},
                                 submissions={'S2': DeliveryStatus.UNKNOWN})
        self.service = InboundReceivingService(self.ledger)
        self.batch = self.service.create_batch('BATCH-1', make_form())
        self.confirm_complete()
        return self.service.deliver(self.batch)

    def test_unknown_outcome_blocks_second_submissions_but_allows_draft_edits(self):
        outcomes = self._deliver_with_second_station_unknown()
        self.assertTrue(outcomes[0].is_accepted)
        self.assertTrue(outcomes[1].is_unknown)
        self.assertEqual(self.batch.stage, Stage.BLOCKED)
        self.assertEqual(self.batch.blocked_stations, ['S2'])

        with self.assertRaises(DeliveryError):
            self.service.deliver(self.batch)

        # Draft edits only allocate new identities; they do not submit or
        # modify the saved confirmation.
        self.service.begin_edit(self.batch, 'bob').set_line(2, station='S3').commit()
        self.assertEqual(self.batch.stage, Stage.BLOCKED)
        self.assertEqual(len(self.ledger.requests_for_form('BATCH-1')), 2)

    def test_unknown_outcome_survives_workflow_save_and_restore(self):
        self._deliver_with_second_station_unknown()
        token = self.service.save_batch(self.batch)
        restored = self.service.load_batch(token)

        self.assertEqual(restored.stage, Stage.BLOCKED)
        self.assertEqual(restored.blocked_stations, ['S2'])
        self.assertEqual(len(self.ledger.requests_for_form('BATCH-1')), 2)
        with self.assertRaises(DeliveryError):
            self.service.deliver(restored)

    def test_no_second_submission_or_confirmation_change_while_unknown(self):
        self._deliver_with_second_station_unknown()
        requests_before = len(self.ledger.requests_for_form('BATCH-1'))
        confirmed_before = dict(self.batch.confirmed['identities'])

        token = self.service.save_batch(self.batch)
        restored = self.service.load_batch(token)

        with self.assertRaises(DeliveryError):
            self.service.deliver(restored)
        with self.assertRaises(DeliveryError):
            self.service.confirm(restored)
        with self.assertRaises(DeliveryError):
            restored.roll_back('attempt')
        self.assertEqual(len(self.ledger.requests_for_form('BATCH-1')), requests_before)
        self.assertEqual(restored.confirmed['identities'], confirmed_before)


class ResolveAcceptedTest(BaseScenario):

    def test_second_station_accepts_then_old_draft_is_rejected_as_archived(self):
        self.ledger = new_ledger({'S1': 100, 'S2': 100, 'S3': 100},
                                 submissions={'S2': DeliveryStatus.UNKNOWN})
        self.service = InboundReceivingService(self.ledger)
        self.batch = self.service.create_batch('BATCH-1', make_form())
        self.confirm_complete()
        self.service.deliver(self.batch)

        token = self.service.save_batch(self.batch)
        restored = self.service.load_batch(token)

        outcome = self.service.resolve_unknown(restored, 'S2', accepted=True)
        self.assertTrue(outcome.is_accepted)

        confirmed_snapshot = dict(restored.confirmed['identities'])
        self.service.publish(restored)
        self.assertTrue(restored.is_visible)
        self.assertEqual(restored.archived['identities'], confirmed_snapshot)

        with self.assertRaises(ArchivedVersionError):
            self.service.begin_edit(restored, 'bob').set_line(2, station='S3')

    def test_accepted_unknown_keeps_original_confirmations(self):
        self.ledger = new_ledger({'S1': 100, 'S2': 100},
                                 submissions={'S2': DeliveryStatus.UNKNOWN})
        self.service = InboundReceivingService(self.ledger)
        self.batch = self.service.create_batch('BATCH-1', make_form())
        originals = self.confirm_complete()

        self.service.deliver(self.batch)
        self.service.resolve_unknown(self.batch, 'S2', accepted=True)
        self.service.publish(self.batch)

        for line_no, data in originals['identities'].items():
            self.assertEqual(self.batch.archived['identities'][line_no], data)


class ResolveRejectedTest(BaseScenario):

    def test_rejection_requires_withdraw_and_new_identity(self):
        self.ledger = new_ledger({'S1': 100, 'S2': 100, 'S3': 100},
                                 submissions={'S2': DeliveryStatus.UNKNOWN})
        self.service = InboundReceivingService(self.ledger)
        self.batch = self.service.create_batch('BATCH-1', make_form())
        self.confirm_complete()
        original_two = self.batch.identity_for(2).reservation_id
        self.service.deliver(self.batch)

        token = self.service.save_batch(self.batch)
        restored = self.service.load_batch(token)

        rejection = self.service.resolve_unknown(restored, 'S2', accepted=False)
        self.assertTrue(rejection.is_rejected)
        self.assertFalse(restored.is_visible)

        # The first station's earlier acceptance must be withdrawn with a
        # checkable receipt.
        withdrawal = self.service.withdraw_acceptance(restored, 'S1')
        self.assertTrue(withdrawal.is_rejected)
        self.assertIn('withdrawn_at', withdrawal.receipt)
        self.assertEqual(withdrawal.receipt['request_id'], withdrawal.request_id)
        self.assertEqual(self.ledger.available('S1'), 100)

        # The rejected line is re-verified with a new identity in a new
        # confirmation.  The old identity is never reused.
        restored.roll_back('station rejected')
        new_identity = self.service.preverify(restored, refresh=True)[2]
        self.assertNotEqual(new_identity.reservation_id, original_two)
        self.service.confirm(restored)

        # A withdrawal was already performed; submit the fresh batch.
        self.service.deliver(restored)
        self.service.publish(restored)

        self.assertTrue(restored.is_visible)
        self.assertEqual(restored.archived['identities'][2]['reservation_id'],
                         new_identity.reservation_id)

    def test_identity_equality_does_not_prove_confirmation(self):
        self.confirm_complete()
        original = self.batch.identity_for(2)
        self.service.deliver(self.batch)

        # A user toggles the station and back.
        self.batch.roll_back('reset')
        session = self.service.begin_edit(self.batch, 'bob')
        session.set_line(2, station='S3').commit()
        session = self.service.begin_edit(self.batch, 'bob')
        session.set_line(2, station='S2').commit()

        current = self.batch.identity_for(2)
        self.assertEqual((current.station, current.quantity),
                         (original.station, original.quantity))
        self.assertNotEqual(current.reservation_id, original.reservation_id)
        with self.assertRaises(PreverificationError):
            self.batch.assert_identity_current(2, original)


class ReconciliationTest(BaseScenario):

    def test_reconcile_reports_occupancy_display_and_identity(self):
        self.confirm_complete()
        self.service.deliver(self.batch)
        report = self.service.reconcile(self.batch)

        self.assertEqual(report['stage'], Stage.CONFIRMED)
        self.assertEqual(report['stations']['S1']['occupancy'], 10)
        self.assertEqual(report['stations']['S2']['occupancy'], 20)
        self.assertEqual(report['stations']['S1']['available'], 90)
        self.assertEqual(report['workflow']['ready_task'], 'Delivery')
        for line in report['lines']:
            self.assertTrue(line['matches_confirmation'])
            self.assertTrue(line['values_match_confirmation'])

    def test_identical_values_with_different_identity_do_not_match(self):
        self.confirm_complete()
        original = self.batch.identity_for(2)
        self.batch.roll_back('move')

        session = self.service.begin_edit(self.batch, 'bob')
        session.set_line(2, station='S3').commit()
        session = self.service.begin_edit(self.batch, 'bob')
        session.set_line(2, station='S2').commit()

        report = self.service.reconcile(self.batch)
        line = [entry for entry in report['lines'] if entry['line_no'] == 2][0]
        self.assertTrue(line['values_match_confirmation'])
        self.assertFalse(line['matches_confirmation'])
        self.assertNotEqual(line['identity'], original.reservation_id)

    def test_plain_untouched_usage_still_works(self):
        # The ordinary, non-concurrent flow is unaffected.
        self.confirm_complete()
        self.service.deliver(self.batch)
        self.service.publish(self.batch)
        report = self.service.reconcile(self.batch)
        self.assertTrue(report['visible'])
        self.assertEqual(report['archived_version'], 1)
        self.assertEqual(report['workflow']['ready_task'], 'Complete')


if __name__ == '__main__':
    unittest.main()
