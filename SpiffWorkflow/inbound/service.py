"""Inbound receiving review service.

This module composes the workflow archive and the external reservation system.
It deliberately does **not** write to both in one transaction: ``save_batch``
persists only the archive, while reservation/delivery methods mutate only the
ledger.  Callers that need durability save the archive afterwards.

The service encodes the delivery rules that matter for this workflow:

* a complete form is preverified as one fresh reservation identity per line;
* row order and form remarks never allocate reservations;
* only a complete, confirmed form whose confirmed identities all still match
  can be delivered, one request per station, containing all that station's
  reservations;
* an unknown delivery outcome blocks further submissions and edits to the
  saved confirmation until it is resolved;
* a rejection requires withdrawing any earlier acceptance (with a receipt)
  and re-confirming with a new reservation identity, never by reusing the old
  identity merely because the visible values became equal again.
"""

from copy import deepcopy
from typing import Any, Optional

from .archive import (
    InboundReviewBatch,
    Stage,
)
from .model import (
    FormVersion,
    DeliveryError,
    PreverificationError,
    ArchivedVersionError,
    as_form_version,
    merge_versions,
    validate_complete,
)
from .reservations import DeliveryStatus, ReservationLedger


class DeliveryOutcome:
    def __init__(self, request):
        self.request = request
        self.request_id = request.request_id
        self.station = request.station
        self.status = request.status
        self.receipt = deepcopy(request.receipt)

    @property
    def is_accepted(self) -> bool:
        return self.status == DeliveryStatus.ACCEPTED

    @property
    def is_rejected(self) -> bool:
        return self.status == DeliveryStatus.REJECTED

    @property
    def is_unknown(self) -> bool:
        return self.status == DeliveryStatus.UNKNOWN

    def to_dict(self) -> dict:
        return {
            'request_id': self.request_id,
            'station': self.station,
            'status': self.status,
            'receipt': deepcopy(self.receipt),
        }


class InboundReceivingService:
    """Coordinates a review batch with its external reservation system."""

    def __init__(self, ledger: ReservationLedger, archive_store=None):
        self.ledger = ledger
        self.archive_store = archive_store if archive_store is not None else {}
        self._tokens = {}
        self._token_sequence = 0

    # ------------------------------------------------------------------
    # Batch creation and durable workflow save/restore
    # ------------------------------------------------------------------
    def create_batch(self, batch_id: str, form: Any) -> InboundReviewBatch:
        if batch_id in self.archive_store:
            raise ValueError(f'batch {batch_id!r} already exists')
        batch = InboundReviewBatch(batch_id, as_form_version(form))
        self.archive_store[batch_id] = batch.dump()
        return batch

    def save_batch(self, batch: InboundReviewBatch) -> str:
        """Persist the workflow archive only.

        This method never touches the reservation system; saving or restoring
        a workflow cannot commit, cancel or retry a reservation.
        """
        serialization = batch.dump()
        self.archive_store[batch.batch_id] = serialization
        self._token_sequence += 1
        token = f'{batch.batch_id}@{self._token_sequence}'
        self._tokens[token] = serialization
        return token

    def load_batch(self, batch_or_token) -> InboundReviewBatch:
        if isinstance(batch_or_token, InboundReviewBatch):
            return InboundReviewBatch.load(batch_or_token.dump())
        serialization = self._tokens.get(batch_or_token)
        if serialization is None:
            serialization = self.archive_store.get(batch_or_token)
        if serialization is None:
            raise KeyError(f'no saved batch {batch_or_token!r}')
        return InboundReviewBatch.load(serialization)

    def restore_saved_version(self, batch_id: str) -> InboundReviewBatch:
        return InboundReviewBatch.load(self.archive_store[batch_id])

    # ------------------------------------------------------------------
    # Preverification
    # ------------------------------------------------------------------
    def preverify(self, batch: InboundReviewBatch, line_numbers=None,
                  refresh: bool = False) -> dict:
        """Preverify lines that do not already hold a matching live identity.

        Reordering rows or editing remarks does not invalidate identities, so
        a normal preverification pass allocates nothing for unchanged lines.
        ``refresh=True`` or an explicit ``line_numbers`` list forces new
        identities for those lines.
        """
        if batch.archived is not None:
            raise ArchivedVersionError('the batch is already published')
        if batch.stage == Stage.CONFIRMED:
            raise PreverificationError(
                'the version is already confirmed; roll back before preverifying new identities'
            )
        if refresh and line_numbers is None:
            line_numbers = list(batch.form.line_numbers)
        if line_numbers is not None:
            return batch.preverify(self.ledger, line_numbers=line_numbers)

        missing = []
        for line in batch.form.lines:
            identity = batch.identity_for(line.line_no)
            if identity is None:
                missing.append(line.line_no)
                continue
            reservation = self.ledger.get(identity.reservation_id)
            if not reservation.is_usable() or not identity.matches_values(line):
                missing.append(line.line_no)
        if missing:
            return batch.preverify(self.ledger, line_numbers=missing)
        if batch.stage != Stage.BLOCKED and batch._all_identified():
            batch._enter_stage(Stage.PREVERIFIED)
            batch._record('preverify', {'version': batch.form.version, 'reservations': {}})
        return {}

    def reservation_confirmation(self, batch_id: str, line_no: int) -> dict:
        """Query the external system for a line's confirmed reservation.

        This is a read-only reservation-system operation and is intentionally
        separate from reading the workflow archive.
        """
        reservation = self.ledger.reservation_for_line(batch_id, line_no)
        if reservation is None:
            return {'line_no': line_no, 'confirmed': False, 'reservation_id': None}
        return {
            'line_no': line_no,
            'confirmed': reservation.confirmed and not reservation.cancelled,
            'reservation_id': reservation.reservation_id,
            'station': reservation.station,
            'quantity': reservation.quantity,
        }

    # ------------------------------------------------------------------
    # Confirmation
    # ------------------------------------------------------------------
    def confirm(self, batch: InboundReviewBatch) -> dict:
        if batch.blocked_stations:
            raise DeliveryError(
                'cannot modify the saved original confirmation while a delivery outcome is unknown'
            )
        for line in batch.form.lines:
            identity = batch.identity_for(line.line_no)
            if identity is None:
                raise PreverificationError(f'line {line.line_no} has not been preverified')
            reservation = self.ledger.get(identity.reservation_id)
            if not reservation.is_usable():
                raise PreverificationError(
                    f'line {line.line_no} reservation {identity.reservation_id} is no longer usable'
                )
            if not identity.matches_values(line):
                raise PreverificationError(
                    f'line {line.line_no} reservation {identity.reservation_id} no longer '
                    'matches the visible station and quantity'
                )
        return batch.confirm()

    # ------------------------------------------------------------------
    # Delivery
    # ------------------------------------------------------------------
    def _confirmed_delivery(self, batch: InboundReviewBatch) -> FormVersion:
        if batch.confirmed is None:
            raise DeliveryError('cannot deliver before the version is confirmed')
        confirmed = FormVersion.from_dict(batch.confirmed['form'])
        validate_complete(confirmed, 'delivery')
        if not confirmed.stations():
            raise DeliveryError('the confirmed form has no stations')
        return confirmed

    def _request_for_station(self, batch: InboundReviewBatch, station: str):
        requests = [
            request for request in self.ledger.requests_for_form(batch.batch_id)
            if request.station == station
        ]
        return requests[-1] if requests else None

    def _confirmed_identities_for(self, batch: InboundReviewBatch, station: str) -> list:
        confirmed_identities = batch.confirmed_lines()
        return [
            identity for line_no, identity in sorted(confirmed_identities.items())
            if identity.station == station
        ]

    def deliver(self, batch: InboundReviewBatch, only_station: Optional[str] = None) -> list:
        """Submit each station's full set of *confirmed* reservations.

        Only stations without a delivery request are submitted, so an unknown
        outcome can never produce a second submission.  The confirmed snapshot
        -- not the working draft -- determines what is sent.
        """
        if batch.blocked_stations:
            raise DeliveryError(
                f'delivery outcome is unknown for {batch.blocked_stations}; '
                'refusing to generate another submission'
            )
        if batch.archived is not None:
            raise ArchivedVersionError('the batch is already published')

        confirmed = self._confirmed_delivery(batch)
        stations = confirmed.stations()
        if only_station is not None:
            if only_station not in stations:
                raise DeliveryError(f'{only_station!r} is not a station of the confirmed form')
            stations = [only_station]

        outcomes = []
        for station in stations:
            existing = self._request_for_station(batch, station)
            if existing is not None and existing.status in (DeliveryStatus.ACCEPTED,
                                                            DeliveryStatus.UNKNOWN):
                if existing.status == DeliveryStatus.UNKNOWN:
                    batch.mark_station_blocked(station, existing.request_id)
                outcomes.append(DeliveryOutcome(existing))
                if existing.status == DeliveryStatus.UNKNOWN:
                    break
                continue
            # A rejected or withdrawn request can be replaced by a fresh one
            # for the current confirmation's identities.

            identities = self._confirmed_identities_for(batch, station)
            if not identities:
                continue

            for identity in identities:
                reservation = self.ledger.get(identity.reservation_id)
                if reservation.cancelled or not reservation.confirmed:
                    raise DeliveryError(
                        f'line {identity.line_no} reservation {identity.reservation_id} '
                        'is no longer confirmed'
                    )
                if identity.station != reservation.station or identity.quantity != reservation.quantity:
                    raise DeliveryError(
                        f'line {identity.line_no} reservation {identity.reservation_id} '
                        'does not match the confirmed values'
                    )

            request = self.ledger.submit(
                form_id=batch.batch_id,
                station=station,
                reservations=[self.ledger.get(identity.reservation_id) for identity in identities],
            )
            outcome = DeliveryOutcome(request)
            outcomes.append(outcome)
            if outcome.is_unknown:
                batch.mark_station_blocked(station, request.request_id)
                break
            if outcome.is_rejected:
                batch._record('delivery-rejected', {
                    'station': station,
                    'request_id': request.request_id,
                })
                break
            batch._record('delivery-accepted', {
                'station': station,
                'request_id': request.request_id,
            })
        return outcomes

    def resolve_unknown(self, batch: InboundReviewBatch, station: str,
                        accepted: bool) -> DeliveryOutcome:
        """Resolve a station's unknown outcome and update the workflow stage."""
        requests = [
            request for request in self.ledger.requests_for_form(batch.batch_id)
            if request.station == station and request.status == DeliveryStatus.UNKNOWN
        ]
        if not requests:
            raise DeliveryError(f'station {station!r} has no unknown delivery request')
        request = requests[-1]
        request = self.ledger.resolve(
            request.request_id,
            DeliveryStatus.ACCEPTED if accepted else DeliveryStatus.REJECTED,
        )
        batch.resolve_station_outcome(station, accepted=accepted)
        return DeliveryOutcome(request)

    def withdraw_acceptance(self, batch: InboundReviewBatch, station: str) -> DeliveryOutcome:
        """Ask a station to withdraw its acceptance and return the receipt."""
        requests = [
            request for request in self.ledger.requests_for_form(batch.batch_id)
            if request.station == station and request.status == DeliveryStatus.ACCEPTED
        ]
        if not requests:
            raise DeliveryError(f'station {station!r} has no accepted delivery to withdraw')
        request = self.ledger.withdraw(requests[-1].request_id)
        batch._record('delivery-withdrawn', {
            'station': station,
            'request_id': request.request_id,
        })
        return DeliveryOutcome(request)

    # ------------------------------------------------------------------
    # Publication
    # ------------------------------------------------------------------
    def publish(self, batch: InboundReviewBatch) -> dict:
        return batch.publish(self.ledger)

    # ------------------------------------------------------------------
    # Edits
    # ------------------------------------------------------------------
    def begin_edit(self, batch: InboundReviewBatch, editor: str,
                   base: Any = None) -> 'InboundEditSession':
        session = InboundEditSession(self, batch, base if base is not None else batch.form)
        session.set_editor(editor)
        return session

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------
    def reconcile(self, batch: InboundReviewBatch) -> dict:
        return batch.reconcile(self.ledger)


class InboundEditSession:
    """Edit session that allocates reservations through the service ledger."""

    def __init__(self, service: InboundReceivingService, batch: InboundReviewBatch, base: Any):
        self.service = service
        self.batch = batch
        self._session = batch.begin_edit(base)
        self._session.ledger = service.ledger
        self.editor = None

    def set_editor(self, editor: str) -> 'InboundEditSession':
        self.editor = editor
        self._session.set_editor(editor)
        return self

    def reorder(self, line_numbers) -> 'InboundEditSession':
        self._session.reorder(line_numbers)
        return self

    def set_notes(self, notes: str) -> 'InboundEditSession':
        self._session.set_notes(notes)
        return self

    def set_line(self, line_no: int, station: Optional[str] = None,
                 quantity: Optional[int] = None) -> 'InboundEditSession':
        if self.batch.archived is not None:
            raise ArchivedVersionError(
                f'cannot modify batch {self.batch.batch_id}: version '
                f'{self.batch.archived["version"]} is already warehoused'
            )
        # A working-draft edit is allowed while an outcome is unknown; it only
        # allocates new identities and never submits or changes the saved
        # confirmation.
        self._session.set_line(line_no, station=station, quantity=quantity,
                               ledger=self.service.ledger)
        return self

    @property
    def candidate(self):
        return self._session.candidate

    @property
    def identities(self):
        return self._session.identities

    def merged_form(self):
        return merge_versions(self.batch.form, self._session.base, self._session.candidate)

    def commit(self):
        return self._session.commit()


def new_ledger(quota, submissions=None, confirmation_ttl=None) -> ReservationLedger:
    kwargs = {'quota': quota, 'submissions': submissions}
    if confirmation_ttl is not None:
        kwargs['confirmation_ttl'] = confirmation_ttl
    return ReservationLedger(**kwargs)
