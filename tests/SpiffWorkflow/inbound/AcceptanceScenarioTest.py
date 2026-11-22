"""End-to-end acceptance narrative.

Two people work on the same version of an inbound receiving form with two
stable material lines going to two different stations.  Both lines pass
preverification.  Person A confirms the version; the first station accepts
the delivered reservation set while the second reports an unknown outcome.
The workflow is saved and restored in between.  Person B, holding an older
draft, moves the second line to another station and back to its initial
station: the visible station and quantity end up identical to the original,
but the reservation identity is new.  Equal visible values must never revive
the original confirmation.

The story then forks on the external fact reported for the second station.
"""

import unittest

from SpiffWorkflow.inbound import (
    ArchivedVersionError,
    DeliveryError,
    FormVersion,
    DeliveryStatus,
    InboundReceivingService,
    InboundReviewBatch,
    Line,
    new_ledger,
)


def original_form():
    return FormVersion(
        version=1,
        form_id='BATCH-1',
        notes='dock 4',
        lines=[
            Line(line_no=1, station='S1', quantity=10, note='steel'),
            Line(line_no=2, station='S2', quantity=20, note='brackets'),
        ],
    )


class AcceptanceScenario(unittest.TestCase):

    def setUp(self):
        self.ledger = new_ledger(
            quota={'S1': 100, 'S2': 100, 'S3': 100},
            submissions={'S2': DeliveryStatus.UNKNOWN},
        )
        self.service = InboundReceivingService(self.ledger)
        self.batch = self.service.create_batch('BATCH-1', original_form())
        self.old_draft = original_form()
        self.first = self.service.preverify(self.batch)
        self.second = self.service.preverify(self.batch)
        self.confirmation = self.service.confirm(self.batch)
        self.original_identity_1 = self.confirmation['identities'][1]['reservation_id']
        self.original_identity_2 = self.confirmation['identities'][2]['reservation_id']

    def deliver_and_restore_while_unknown(self):
        outcomes = self.service.deliver(self.batch)
        self.assertTrue(outcomes[0].is_accepted)
        self.assertTrue(outcomes[1].is_unknown)

        token = self.service.save_batch(self.batch)
        restored = self.service.load_batch(token)
        self.assertEqual(restored.stage, 'blocked')
        self.assertEqual(restored.confirmed['identities'], self.confirmation['identities'])
        return restored

    def toggle_second_line_and_back(self, batch):
        # B's stale draft still shows the original values.
        session = self.service.begin_edit(batch, 'bob', base=self.old_draft)
        session.set_line(2, station='S3').commit()
        session = self.service.begin_edit(batch, 'bob')
        session.set_line(2, station='S2').commit()

        current = batch.identity_for(2)
        self.assertEqual((current.station, current.quantity), ('S2', 20))
        self.assertNotEqual(current.reservation_id, self.original_identity_2)

    def test_second_station_accepts(self):
        batch = self.deliver_and_restore_while_unknown()
        self.toggle_second_line_and_back(batch)

        # A stale draft may render the visible values but it may not modify
        # the saved version once the original version is warehoused.
        resolved = self.service.resolve_unknown(batch, 'S2', accepted=True)
        self.assertTrue(resolved.is_accepted)
        self.service.publish(batch)

        self.assertTrue(batch.is_visible)
        self.assertEqual(batch.archived['identities'][1]['reservation_id'],
                         self.original_identity_1)
        self.assertEqual(batch.archived['identities'][2]['reservation_id'],
                         self.original_identity_2)

        with self.assertRaises(ArchivedVersionError):
            self.service.begin_edit(batch, 'bob', base=self.old_draft).set_line(2, station='S3')

    def test_second_station_rejects(self):
        batch = self.deliver_and_restore_while_unknown()
        self.toggle_second_line_and_back(batch)

        rejection = self.service.resolve_unknown(batch, 'S2', accepted=False)
        self.assertTrue(rejection.is_rejected)
        self.assertFalse(batch.is_visible)

        # The first station's acceptance must be withdrawn with a receipt.
        receipt = self.service.withdraw_acceptance(batch, 'S1')
        self.assertTrue(receipt.is_rejected)
        self.assertEqual(receipt.receipt['request_id'], receipt.request_id)
        self.assertIn('withdrawn_at', receipt.receipt)
        self.assertEqual(self.ledger.available('S1'), 100)

        # The withdrawal also cancelled line 1's reservation, so the next
        # confirmation needs fresh identities for both lines.  The rejected
        # second line in particular must not reuse its old identity.
        batch.roll_back('second station rejected')
        fresh = self.service.preverify(batch, refresh=True)
        self.assertNotEqual(fresh[2].reservation_id, self.original_identity_2)
        self.assertNotEqual(fresh[1].reservation_id, self.original_identity_1)
        self.service.confirm(batch)
        self.assertFalse(batch.is_visible)

        self.service.deliver(batch)
        self.service.publish(batch)
        self.assertTrue(batch.is_visible)
        self.assertEqual(batch.archived['identities'][2]['reservation_id'],
                         batch.identity_for(2).reservation_id)

    def test_unknown_window_allows_no_duplicate_or_confirmation_change(self):
        batch = self.deliver_and_restore_while_unknown()
        requests = len(self.ledger.requests_for_form('BATCH-1'))
        confirmed = dict(batch.confirmed['identities'])

        self.toggle_second_line_and_back(batch)
        with self.assertRaises(DeliveryError):
            self.service.deliver(batch)
        with self.assertRaises(DeliveryError):
            self.service.confirm(batch)
        with self.assertRaises(DeliveryError):
            batch.roll_back('too early')

        self.assertEqual(len(self.ledger.requests_for_form('BATCH-1')), requests)
        self.assertEqual(batch.confirmed['identities'], confirmed)
        self.assertEqual(self.ledger.occupancy('S1'), 10)
        self.assertEqual(self.ledger.occupancy('S2'), 20)

    def test_workflow_survives_multiple_round_trips(self):
        batch = self.deliver_and_restore_while_unknown()
        for _ in range(3):
            batch = self.service.load_batch(batch)
        self.assertEqual(batch.stage, 'blocked')
        self.assertEqual(batch.workflow_status()['ready_task'], 'Delivery')
        self.assertEqual(len(batch.confirmations), 1)

    def test_untouched_concurrent_usage_still_publishes(self):
        # No second editor: the ordinary flow is unaffected.
        outcomes = self.service.deliver(self.batch)
        self.assertTrue(outcomes[0].is_accepted)
        self.service.resolve_unknown(self.batch, 'S2', accepted=True)
        self.service.publish(self.batch)
        self.assertTrue(self.batch.is_visible)
        self.assertEqual(self.batch.archived['version'], 1)


if __name__ == '__main__':
    unittest.main()
