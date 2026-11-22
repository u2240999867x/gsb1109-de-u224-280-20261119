# Copyright (C) 2026 Sartography
#
# This file is part of SpiffWorkflow.
#
# SpiffWorkflow is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 3.0 of the License, or (at your option) any later version.
#
# SpiffWorkflow is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301  USA

"""Save/restore-aware reception sessions.

A session owns no durable copy of the external claim answer.  Everything it
knows from the message service is derived from an explicit evidence query;
everything it knows about its own progress lives in the workflow archive.

The two concepts that must not be conflated are:

``authority``
    The endpoint that may initiate the next step.  A confirmed handoff moves
    authority even when the old endpoint already claimed the file.

``file claim``
    The single atomic claim recorded by the message service.  It remains with
    the endpoint that won the race, even after authority moves.  A successor
    therefore continues from the existing claim instead of claiming again.
"""

from dataclasses import dataclass, field
from enum import Enum

from .models import (
    HandoffStatus,
    Receipt,
    ReceiptKind,
    Registration,
    SubmissionStatus,
)
from .service import MessageService


ARCHIVE_KEY = "reception_state"


class SessionStatus(str, Enum):
    BLOCKED = "blocked"
    WAITING_FOR_FILE = "waiting_for_file"
    FILE_CLAIMED = "file_claimed"
    CHECKSUM_SENT = "checksum_sent"
    COMPLETE = "complete"
    DISENFRANCHISED = "disenfranchised"
    CONFLICT = "conflict"


@dataclass
class ReceptionState:
    """The portion of session state that is persisted with the workflow."""

    endpoint: str
    key_dict: dict
    registration_id: str = None
    status: str = SessionStatus.WAITING_FOR_FILE.value
    claim_request_id: str = None
    claim_receipt_id: str = None
    checksum_request_id: str = None
    checksum_receipt_id: str = None
    error: str = None

    @property
    def key(self):
        from .models import ReceiptKey

        return ReceiptKey.from_dict(self.key_dict)

    def to_dict(self) -> dict:
        return {
            "endpoint": self.endpoint,
            "key": dict(self.key_dict),
            "registration_id": self.registration_id,
            "status": self.status,
            "claim_request_id": self.claim_request_id,
            "claim_receipt_id": self.claim_receipt_id,
            "checksum_request_id": self.checksum_request_id,
            "checksum_receipt_id": self.checksum_receipt_id,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, dct: dict) -> "ReceptionState":
        return cls(
            endpoint=dct["endpoint"],
            key_dict=dict(dct["key"]),
            registration_id=dct.get("registration_id"),
            status=dct.get("status", SessionStatus.WAITING_FOR_FILE.value),
            claim_request_id=dct.get("claim_request_id"),
            claim_receipt_id=dct.get("claim_receipt_id"),
            checksum_request_id=dct.get("checksum_request_id"),
            checksum_receipt_id=dct.get("checksum_receipt_id"),
            error=dct.get("error"),
        )


@dataclass
class SessionOutcome:
    """Result of one session operation, intended for assertions."""

    status: SessionStatus
    detail: str = ""
    claim_sequence: int = None
    checksum_request: object = None


@dataclass
class ReceptionSession:
    """One endpoint's view of reception for one supplier batch generation."""

    state: ReceptionState
    service: MessageService
    registration: Registration = None
    _last_evidence: object = field(default=None, init=False, repr=False)

    @classmethod
    def create(cls, endpoint, key, service, registration=None):
        if registration is None:
            registration = service.register(key, endpoint)
        return cls(
            ReceptionState(endpoint, key.to_dict(), registration.registration_id),
            service,
            registration,
        )

    @classmethod
    def restore_from(cls, workflow, service, registration=None):
        """Restore the session attached to a workflow archive.

        The archive holds only the endpoint's own progress.  Claim authority
        and handoff state remain in the message service.
        """

        raw = workflow.data.get(ARCHIVE_KEY)
        if raw is None:
            raise KeyError(f"Workflow does not contain {ARCHIVE_KEY}")
        state = ReceptionState.from_dict(raw)
        if registration is None and state.registration_id is not None:
            registration = service.find_registration(state.key, state.registration_id)
            if registration is None:
                registration = Registration(
                    state.key, state.endpoint, state.registration_id, 0
                )
        return cls(state, service, registration)

    def save_to(self, workflow):
        """Persist only local progress; never the external answer."""

        workflow.data[ARCHIVE_KEY] = self.state.to_dict()

    @property
    def key(self):
        return self.state.key

    @property
    def endpoint(self):
        return self.state.endpoint

    # ------------------------------------------------------------------
    # Intake
    # ------------------------------------------------------------------
    def receive(self, receipt: Receipt, fault=None):
        """Submit an inbound receipt and attribute the outcome."""

        result = self.service.submit(receipt, fault=fault)
        if result.status is SubmissionStatus.CONFLICT:
            self.state.status = SessionStatus.CONFLICT.value
            self.state.error = (
                f"receipt {receipt.receipt_id} conflicts with accepted "
                f"{result.attribution}"
            )
        return result

    def pending_receipts(self, kind=ReceiptKind.FILE):
        """Receipts visible to every executor, read from the service.

        Both the old and the new receiver see the same undelivered file
        receipt because this does not consult either local archive.
        """

        return [
            stored.receipt
            for stored in self.service.submissions_for(self.key)
            if stored.receipt.kind is kind
        ]

    def pending_file_receipts(self):
        return self.pending_receipts(ReceiptKind.FILE)

    def pending_checksum_receipts(self):
        return self.pending_receipts(ReceiptKind.CHECKSUM)

    # ------------------------------------------------------------------
    # Authority and claims
    # ------------------------------------------------------------------
    def authority(self):
        return self.service.authority(self.key)

    def is_owner(self):
        return self.service.authority_allows(self.key, self.registration)

    def claim_exists(self):
        return self.service.claim(self.key) is not None

    def claim_status(self, evidence=False):
        """Tri-state claim lookup.

        Without explicit evidence the service may only say ``UNKNOWN``.  A
        local archive that looks identical in two traces cannot decide this.
        """

        if evidence:
            self._last_evidence = self.service.evidence(self.key)
            return self._last_evidence.status
        return self.service.lookup_claim(self.key)

    def request_file_claim(self, receipt=None):
        if not self.is_owner():
            return SessionOutcome(SessionStatus.DISENFRANCHISED, "endpoint no longer owns reception")

        existing = self.service.claim(self.key)
        if existing is not None:
            if self.state.claim_request_id == existing.request_id:
                # Replay of a claim this session already committed.
                self.state.status = SessionStatus.FILE_CLAIMED.value
                return SessionOutcome(
                    SessionStatus.FILE_CLAIMED, "file already claimed", claim_sequence=existing.sequence
                )
            return SessionOutcome(SessionStatus.DISENFRANCHISED, "file already claimed")
        if self.state.claim_request_id is not None:
            return SessionOutcome(SessionStatus.BLOCKED, "a claim was already proposed")

        receipt = receipt or self._canonical_file_receipt()
        if receipt is None:
            return SessionOutcome(SessionStatus.WAITING_FOR_FILE, "no file receipt")
        if receipt.kind is not ReceiptKind.FILE:
            return SessionOutcome(SessionStatus.BLOCKED, "checksum receipt cannot claim a file")

        request = self.service.request_claim(
            self.key,
            receipt.receipt_id,
            self.endpoint,
            self._request_id("claim"),
            registration_id=self.registration.registration_id if self.registration else None,
        )
        self.state.claim_request_id = request.request_id
        self.state.claim_receipt_id = receipt.receipt_id
        return SessionOutcome(SessionStatus.WAITING_FOR_FILE, "claim requested")

    def commit_file_claim(self):
        """Commit a previously proposed claim; safe to replay."""

        if self.state.claim_request_id is None:
            return SessionOutcome(SessionStatus.WAITING_FOR_FILE, "no claim proposed")
        if not self.is_owner() and self.service.claim(self.key) is None:
            return SessionOutcome(SessionStatus.DISENFRANCHISED, "endpoint no longer owns reception")
        claim = self.service.commit_claim(self.state.claim_request_id)
        if claim is None:
            return SessionOutcome(SessionStatus.DISENFRANCHISED, "claim was not granted")
        self.state.claim_receipt_id = claim.receipt_id
        self.state.status = SessionStatus.FILE_CLAIMED.value
        return SessionOutcome(SessionStatus.FILE_CLAIMED, "file claimed", claim_sequence=claim.sequence)

    def claim_and_commit(self, receipt=None):
        outcome = self.request_file_claim(receipt)
        if outcome.status is not SessionStatus.WAITING_FOR_FILE or self.state.claim_request_id is None:
            return outcome
        return self.commit_file_claim()

    def continue_after_handoff(self):
        """Adopt a confirmed handoff and report which step is missing.

        If the old endpoint already claimed the file, the successor continues
        at the checksum step and must not claim the file again.  If the old
        endpoint only proposed a claim, the file is still unclaimed and the
        successor takes it over.
        """

        handoff = self.service.handoff(self.key)
        if handoff is None or handoff.status is not HandoffStatus.CONFIRMED:
            return SessionOutcome(SessionStatus.BLOCKED, "handoff is not confirmed")
        if not self.is_owner():
            self.state.status = SessionStatus.DISENFRANCHISED.value
            self.state.error = "this registration lost reception authority"
            return SessionOutcome(SessionStatus.DISENFRANCHISED, "registration is stale")

        claim = self.service.claim(self.key)
        if claim is None:
            self.state.status = SessionStatus.WAITING_FOR_FILE.value
            return SessionOutcome(SessionStatus.WAITING_FOR_FILE, "file must still be claimed")

        self.state.claim_receipt_id = claim.receipt_id
        self.state.status = SessionStatus.FILE_CLAIMED.value
        return SessionOutcome(
            SessionStatus.FILE_CLAIMED,
            f"continuing from file claimed by {claim.claimant}",
            claim_sequence=claim.sequence,
        )

    def report_unknown(self):
        if self.state.status == SessionStatus.CONFLICT.value:
            return SessionOutcome(SessionStatus.CONFLICT, self.state.error)
        self.state.status = SessionStatus.BLOCKED.value
        return SessionOutcome(SessionStatus.BLOCKED, "claim state unknown; evidence required")

    # ------------------------------------------------------------------
    # Checksum
    # ------------------------------------------------------------------
    def ensure_checksum(self):
        """Validate the request that may be sent, without recording it yet."""

        if not self.is_owner():
            return SessionOutcome(SessionStatus.DISENFRANCHISED, "endpoint no longer owns reception")
        claim = self.service.claim(self.key)
        if claim is None:
            return SessionOutcome(SessionStatus.BLOCKED, "no committed file claim")
        if self.state.checksum_request_id is not None:
            return SessionOutcome(SessionStatus.BLOCKED, "checksum already requested")
        if self.service.checksum_request(self.key) is not None:
            return SessionOutcome(SessionStatus.BLOCKED, "checksum already requested elsewhere")
        return SessionOutcome(SessionStatus.FILE_CLAIMED, "checksum may be sent")

    def send_checksum(self):
        """Send the checksum request for the file claim.

        After a handoff this is the successor's step even when the file was
        claimed by the predecessor.  The predecessor may confirm its claim
        but may not send this request.  If a checksum receipt arrived before
        the file, it is retained and applied here rather than discarded.
        """

        allowed = self.ensure_checksum()
        if allowed.status is not SessionStatus.FILE_CLAIMED:
            return allowed

        claim = self.service.claim(self.key)
        request = self.service.request_checksum(
            self.key,
            self._request_id("checksum"),
            self.endpoint,
            claim.claimant,
            claim.receipt_id,
            registration_id=self.registration.registration_id if self.registration else None,
        )
        if request is None:
            return SessionOutcome(SessionStatus.DISENFRANCHISED, "checksum request was not granted")
        self.state.checksum_request_id = request.request_id
        self.state.status = SessionStatus.CHECKSUM_SENT.value

        pending = self.pending_checksum_receipts()
        if pending:
            self.state.checksum_receipt_id = pending[-1].receipt_id
            self.state.status = SessionStatus.COMPLETE.value
            return SessionOutcome(
                SessionStatus.COMPLETE, "checksum already received", checksum_request=request
            )
        return SessionOutcome(SessionStatus.CHECKSUM_SENT, "checksum requested", checksum_request=request)

    def applied_checksum_receipt(self):
        """The retained checksum receipt that completed this session."""

        if self.state.checksum_receipt_id is None:
            return None
        for stored in self.service.submissions_for(self.key):
            if stored.receipt.receipt_id == self.state.checksum_receipt_id:
                return stored.receipt
        return None

    def receive_checksum(self, receipt):
        """Complete verification, but only after the file has been claimed."""

        if receipt.kind is not ReceiptKind.CHECKSUM:
            return SessionOutcome(SessionStatus.BLOCKED, "not a checksum receipt")
        if self.service.claim(self.key) is None:
            return SessionOutcome(SessionStatus.BLOCKED, "checksum cannot replace the file")
        if self.service.checksum_request(self.key) is None:
            return SessionOutcome(SessionStatus.BLOCKED, "no checksum request was issued")
        if self.state.checksum_receipt_id is not None:
            return SessionOutcome(SessionStatus.COMPLETE, "checksum already received")

        result = self.receive(receipt)
        if result.status is SubmissionStatus.CONFLICT:
            return SessionOutcome(SessionStatus.CONFLICT, self.state.error)
        self.state.checksum_receipt_id = receipt.receipt_id
        self.state.status = SessionStatus.COMPLETE.value
        return SessionOutcome(SessionStatus.COMPLETE, "checksum received")

    # ------------------------------------------------------------------
    # Introspection
    # ------------------------------------------------------------------
    def own_file_claim(self):
        claim = self.service.claim(self.key)
        if claim is None or self.registration is None:
            return claim
        return claim if claim.belongs_to(self.registration) else None

    def _canonical_file_receipt(self):
        receipts = self.pending_file_receipts()
        return receipts[0] if receipts else None

    def _request_id(self, prefix):
        return f"{prefix}:{self.endpoint}:{self.key.supplier}:{self.key.batch}:{self.key.generation}"


@dataclass
class HandoverCoordinator:
    """Coordinates supervisor handoffs across two endpoints."""

    service: MessageService

    def request(self, key, from_endpoint, to_endpoint):
        return self.service.request_handoff(key, from_endpoint, to_endpoint)

    def confirm(self, key, to_registration):
        return self.service.confirm_handoff(key, to_registration.registration_id)

    def steps(self, key):
        """Report which step each endpoint owns, without changing state."""

        authority = self.service.authority(key)
        claim = self.service.claim(key)
        checksum = self.service.checksum_request(key)
        return {
            "authority": authority.endpoint,
            "file_claimant": claim.claimant if claim is not None else None,
            "file_receipt": claim.receipt_id if claim is not None else None,
            "checksum_owner": authority.endpoint if claim is not None else None,
            "checksum_requested": checksum is not None,
            "authority_status": authority.status.value,
        }
