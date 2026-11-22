"""External reservation system adapter and an in-memory ledger.

The ledger is deliberately independent of the workflow archive.  A workflow
may be saved, restored, or rolled back while reservation and delivery requests
live in this system; the service composes the two but never writes one as part
of a transaction with the other.

Every preverification creates a *new* reservation identity.  Re-entering the
original station and quantity does not revive an earlier identity and cannot
be used to claim that an earlier confirmation still applies.
"""

from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional


class DeliveryStatus:
    ACCEPTED = 'accepted'
    REJECTED = 'rejected'
    UNKNOWN = 'unknown'


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Reservation:
    reservation_id: str
    form_id: str
    line_no: int
    station: str
    quantity: int
    confirmed: bool = True
    cancelled: bool = False
    expires_at: Optional[datetime] = None
    preverified_version: Optional[int] = None

    def is_usable(self, now: Optional[datetime] = None) -> bool:
        if not self.confirmed or self.cancelled:
            return False
        return self.expires_at is None or (now or _utcnow()) < self.expires_at

    @property
    def identity(self) -> str:
        return self.reservation_id

    @property
    def line_key(self) -> tuple:
        return self.line_no, self.station, self.quantity

    def to_dict(self) -> dict:
        return {
            'reservation_id': self.reservation_id,
            'form_id': self.form_id,
            'line_no': self.line_no,
            'station': self.station,
            'quantity': self.quantity,
            'confirmed': self.confirmed,
            'cancelled': self.cancelled,
            'expires_at': self.expires_at.isoformat() if self.expires_at is not None else None,
            'preverified_version': self.preverified_version,
        }


@dataclass
class DeliveryRequest:
    request_id: str
    form_id: str
    station: str
    reservation_ids: list
    status: str = DeliveryStatus.UNKNOWN
    receipt: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            'request_id': self.request_id,
            'form_id': self.form_id,
            'station': self.station,
            'reservation_ids': list(self.reservation_ids),
            'status': self.status,
            'receipt': deepcopy(self.receipt),
        }


class ReservationLedger:
    """An external, workflow-independent reservation and delivery system.

    Parameters:
        quota (dict): station -> externally provided inbound capacity.
        confirmation_ttl: how long a confirmed reservation remains usable.
        submissions (dict): optional station -> initial submission behavior:
            ``'accept'``, ``'reject'`` or ``'unknown'``.  The behavior is
            consumed once, which lets a caller model a transient unknown
            outcome that later resolves.  After the initial behavior is
            consumed, the station accepts submissions.

    A station's *occupancy* counts reserve capacity for both accepted and
    unknown deliveries, because an unknown request might have taken effect.
    Rejected requests hold no capacity; a rejection cancels the reservations
    in the request.
    """

    def __init__(self, quota=None, confirmation_ttl=timedelta(hours=1), submissions=None):
        self._quota = dict(quota or {})
        self._confirmation_ttl = confirmation_ttl
        self._submission_behaviors = dict(submissions or {})
        self._reservations = {}
        self._sequence = 0
        self._requests = {}
        self._request_sequence = 0

    def quota(self, station: str) -> int:
        if station not in self._quota:
            raise KeyError(f'no inbound quota is configured for station {station!r}')
        return self._quota[station]

    def set_quota(self, station: str, quantity: int) -> None:
        self._quota[station] = quantity

    def reserve(self, form_id: str, line_no: int, station: str, quantity: int,
                preverified_version: Optional[int] = None,
                expires_at: Optional[datetime] = None) -> Reservation:
        """Create a fresh reservation identity for a line.

        A reservation is idempotent only while the *same* preverification
        request is retried: callers pass the line's current identity back in
        and no new identity is created.  A new preverification always calls
        this method without an existing identity.
        """
        self._sequence += 1
        reservation = Reservation(
            reservation_id=f'RES-{self._sequence:06d}',
            form_id=form_id,
            line_no=line_no,
            station=station,
            quantity=quantity,
            confirmed=True,
            expires_at=expires_at or (_utcnow() + self._confirmation_ttl),
            preverified_version=preverified_version,
        )
        self._reservations[reservation.reservation_id] = reservation
        return reservation

    def get(self, reservation_id: str) -> Reservation:
        if reservation_id not in self._reservations:
            raise KeyError(f'unknown reservation {reservation_id!r}')
        return self._reservations[reservation_id]

    def reservations_for_form(self, form_id: str) -> list:
        return [r for r in self._reservations.values() if r.form_id == form_id]

    def reservation_for_line(self, form_id: str, line_no: int) -> Optional[Reservation]:
        """Return the current (not cancelled) confirmed reservation for a line."""
        for reservation in self._reservations.values():
            if reservation.form_id == form_id and reservation.line_no == line_no and not reservation.cancelled:
                return reservation
        return None

    def cancel(self, reservation_id: str) -> Reservation:
        reservation = self.get(reservation_id)
        reservation.cancelled = True
        return reservation

    def occupancy(self, station: str) -> int:
        """Capacity held by accepted reservations and unknown submissions."""
        total = 0
        for request in self._requests.values():
            if request.station != station or request.status == DeliveryStatus.REJECTED:
                continue
            for reservation_id in request.reservation_ids:
                reservation = self._reservations.get(reservation_id)
                if reservation is not None:
                    total += reservation.quantity
        return total

    def available(self, station: str) -> int:
        return self.quota(station) - self.occupancy(station)

    def submit(self, form_id: str, station: str, reservations, request_id: Optional[str] = None) -> DeliveryRequest:
        """Submit a batch of reservations to an external station.

        An unknown outcome is recorded as-is; callers must not retry it, as a
        retry could create a second delivery.  A rejection cancels the
        reservations belonging to the request.
        """
        reservations = list(reservations)
        if not reservations:
            raise ValueError('a delivery request requires at least one reservation')

        for reservation in reservations:
            if reservation.station != station:
                raise ValueError(
                    f'reservation {reservation.reservation_id} belongs to {reservation.station!r}, '
                    f'not {station!r}'
                )
            if not reservation.is_usable():
                raise ValueError(f'reservation {reservation.reservation_id} is not usable')

        required = sum(reservation.quantity for reservation in reservations)
        if required > self.available(station):
            raise ValueError(
                f'station {station!r} does not have {required} available; '
                f'available is {self.available(station)}'
            )

        self._request_sequence += 1
        behavior = self._submission_behaviors.pop(station, None)
        if behavior is not None:
            status = behavior
        else:
            status = DeliveryStatus.ACCEPTED

        request = DeliveryRequest(
            request_id=request_id or f'REQ-{self._request_sequence:06d}',
            form_id=form_id,
            station=station,
            reservation_ids=[reservation.reservation_id for reservation in reservations],
            status=status,
        )
        if status == DeliveryStatus.ACCEPTED:
            request.receipt = {
                'request_id': request.request_id,
                'station': station,
                'reservation_ids': list(request.reservation_ids),
                'accepted_at': _utcnow().isoformat(),
            }
        elif status == DeliveryStatus.REJECTED:
            request.receipt = {
                'request_id': request.request_id,
                'station': station,
                'reservation_ids': list(request.reservation_ids),
                'reason': 'rejected-by-station',
            }
            for reservation in reservations:
                reservation.cancelled = True

        self._requests[request.request_id] = request
        return request

    def resolve(self, request_id: str, status: str) -> DeliveryRequest:
        """Resolve a previously unknown request after the fact."""
        request = self._requests[request_id]
        if request.status != DeliveryStatus.UNKNOWN:
            raise ValueError(f'request {request_id} is already {request.status}')
        if status == DeliveryStatus.ACCEPTED:
            request.status = DeliveryStatus.ACCEPTED
            request.receipt = {
                'request_id': request.request_id,
                'station': request.station,
                'reservation_ids': list(request.reservation_ids),
                'accepted_at': _utcnow().isoformat(),
            }
        elif status == DeliveryStatus.REJECTED:
            request.status = DeliveryStatus.REJECTED
            request.receipt = {
                'request_id': request.request_id,
                'station': request.station,
                'reservation_ids': list(request.reservation_ids),
                'reason': 'rejected-by-station',
            }
            for reservation_id in request.reservation_ids:
                self._reservations[reservation_id].cancelled = True
        else:
            raise ValueError(f'cannot resolve {request_id} to {status!r}')
        return request

    def withdraw(self, request_id: str) -> DeliveryRequest:
        """Withdraw an accepted delivery and return a checkable receipt.

        Withdrawal cancels the reservations in the request, releasing the
        station's occupied capacity.
        """
        request = self._requests[request_id]
        if request.status != DeliveryStatus.ACCEPTED:
            raise ValueError(f'only an accepted request can be withdrawn; {request_id} is {request.status}')
        request.status = DeliveryStatus.REJECTED
        request.receipt = {
            'request_id': request.request_id,
            'station': request.station,
            'reservation_ids': list(request.reservation_ids),
            'withdrawn_at': _utcnow().isoformat(),
            'reason': 'withdrawn-by-originator',
        }
        for reservation_id in request.reservation_ids:
            self._reservations[reservation_id].cancelled = True
        return request

    def get_request(self, request_id: str) -> DeliveryRequest:
        return self._requests[request_id]

    def requests_for_form(self, form_id: str) -> list:
        return [r for r in self._requests.values() if r.form_id == form_id]

    def has_pending_request(self, form_id: str, station: Optional[str] = None) -> bool:
        return any(
            request.form_id == form_id
            and request.status == DeliveryStatus.UNKNOWN
            and (station is None or request.station == station)
            for request in self._requests.values()
        )

    def has_accepted_request(self, form_id: str, station: Optional[str] = None) -> bool:
        return any(
            request.form_id == form_id
            and request.status == DeliveryStatus.ACCEPTED
            and (station is None or request.station == station)
            for request in self._requests.values()
        )

    def identity_was_accepted(self, form_id: str, station: str, reservation_id: str) -> bool:
        """Whether this exact reservation identity was accepted for delivery.

        Equality of visible station/quantity is irrelevant: the identity must
        appear in an accepted request.
        """
        return any(
            request.form_id == form_id
            and request.station == station
            and request.status == DeliveryStatus.ACCEPTED
            and reservation_id in request.reservation_ids
            for request in self._requests.values()
        )

    def to_dict(self) -> dict:
        return {
            'quota': dict(self._quota),
            'reservations': [r.to_dict() for r in self._reservations.values()],
            'requests': [r.to_dict() for r in self._requests.values()],
        }
