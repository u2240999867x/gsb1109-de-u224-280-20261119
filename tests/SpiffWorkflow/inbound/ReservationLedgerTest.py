"""Unit tests for the external reservation ledger."""

import unittest
from datetime import timedelta

from SpiffWorkflow.inbound import ReservationLedger
from SpiffWorkflow.inbound.reservations import DeliveryStatus


class ReservationLedgerTest(unittest.TestCase):

    def setUp(self):
        self.ledger = ReservationLedger(quota={'S1': 10, 'S2': 10})

    def test_each_reservation_gets_a_new_identity(self):
        first = self.ledger.reserve('F', 1, 'S1', 5)
        second = self.ledger.reserve('F', 1, 'S1', 5)
        self.assertNotEqual(first.reservation_id, second.reservation_id)

    def test_expired_reservation_is_not_usable(self):
        reservation = self.ledger.reserve(
            'F', 1, 'S1', 5,
            expires_at=reservation_time_in_the_past(),
        )
        self.assertFalse(reservation.is_usable())

    def test_available_respects_quota(self):
        self.assertEqual(self.ledger.available('S1'), 10)
        self.ledger.reserve('F', 1, 'S1', 4)
        self.assertEqual(self.ledger.available('S1'), 10, 'reserved but undelivered holds no capacity')

    def test_delivery_requires_capacity(self):
        reservation = self.ledger.reserve('F', 1, 'S1', 11)
        with self.assertRaises(ValueError):
            self.ledger.submit('F', 'S1', [reservation])

    def test_unknown_request_occupies_capacity(self):
        ledger = ReservationLedger(quota={'S1': 10}, submissions={'S1': DeliveryStatus.UNKNOWN})
        reservation = ledger.reserve('F', 1, 'S1', 4)
        request = ledger.submit('F', 'S1', [reservation])
        self.assertTrue(request.status == DeliveryStatus.UNKNOWN)
        self.assertEqual(ledger.occupancy('S1'), 4)
        self.assertEqual(ledger.available('S1'), 6)

    def test_rejection_cancels_reservations_and_releases_capacity(self):
        ledger = ReservationLedger(quota={'S1': 10}, submissions={'S1': DeliveryStatus.REJECTED})
        reservation = ledger.reserve('F', 1, 'S1', 4)
        ledger.submit('F', 'S1', [reservation])
        self.assertTrue(reservation.cancelled)
        self.assertEqual(ledger.occupancy('S1'), 0)

    def test_withdraw_returns_checkable_receipt_and_releases_capacity(self):
        reservation = self.ledger.reserve('F', 1, 'S1', 4)
        request = self.ledger.submit('F', 'S1', [reservation])
        withdrawn = self.ledger.withdraw(request.request_id)
        self.assertIn('withdrawn_at', withdrawn.receipt)
        self.assertEqual(withdrawn.receipt['reservation_ids'], [reservation.reservation_id])
        self.assertTrue(reservation.cancelled)
        self.assertEqual(self.ledger.occupancy('S1'), 0)

    def test_cannot_withdraw_non_accepted_request(self):
        ledger = ReservationLedger(quota={'S1': 10}, submissions={'S1': DeliveryStatus.UNKNOWN})
        reservation = ledger.reserve('F', 1, 'S1', 4)
        request = ledger.submit('F', 'S1', [reservation])
        with self.assertRaises(ValueError):
            ledger.withdraw(request.request_id)

    def test_station_mismatch_rejected(self):
        reservation = self.ledger.reserve('F', 1, 'S1', 4)
        with self.assertRaises(ValueError):
            self.ledger.submit('F', 'S2', [reservation])

    def test_identity_acceptance_is_identity_specific(self):
        first = self.ledger.reserve('F', 1, 'S1', 4)
        second = self.ledger.reserve('F', 1, 'S1', 4)
        self.ledger.submit('F', 'S1', [first])
        self.assertTrue(self.ledger.identity_was_accepted('F', 'S1', first.reservation_id))
        self.assertFalse(self.ledger.identity_was_accepted('F', 'S1', second.reservation_id))


def reservation_time_in_the_past():
    from datetime import datetime, timezone
    return datetime.now(timezone.utc) - timedelta(hours=1)


if __name__ == '__main__':
    unittest.main()
