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

"""A local stand-in for the external message service.

The service deliberately keeps its own storage.  It shares no transaction
with any workflow archive, which is the entire reason a recovered session
must ask it for evidence instead of inferring an answer from saved tasks.
"""

from uuid import uuid4

from .models import (
    Authority,
    Claim,
    ClaimEvidence,
    ClaimRequest,
    ClaimStatus,
    ChecksumRequest,
    Handoff,
    HandoffStatus,
    ReceiptKind,
    Registration,
    StoredSubmission,
    SubmissionResult,
    SubmissionStatus,
)


class MessageService:
    """Durable, atomic reception ledger with an explicit unknown answer.

    Claims are evaluated against the *confirmed* handoff owner.  A handoff
    that has merely been requested does not move the authority, and a
    confirmed handoff moves it for both the file and the checksum step unless
    the file was already claimed.
    """

    def __init__(self):
        self.submissions = {}
        self.claim_requests = {}
        self.claims = {}
        self.checksum_requests = {}
        self.handoffs = {}
        self.registrations = {}
        self.faults = []
        self._sequence = 0
        self._audit_started = False

    # ------------------------------------------------------------------
    # Submissions
    # ------------------------------------------------------------------
    def submit(self, receipt, fault=None):
        """Accept a receipt and return an attributed result.

        ``fault`` allows a caller to inject a *fault receipt* without it
        being mistaken for a legitimate upload.  Faults are retained with
        their attribution but never become the canonical submission.
        """

        self._sequence += 1
        if fault is not None:
            self.faults.append({"fault": fault, "receipt": receipt.to_dict(), "sequence": self._sequence})
            return SubmissionResult(SubmissionStatus.ACCEPTED, receipt)

        existing = self.submissions.get(receipt.key)
        if existing is None:
            stored = StoredSubmission(receipt, self._sequence, True)
            self.submissions[receipt.key] = [stored]
            return SubmissionResult(SubmissionStatus.ACCEPTED, receipt, canonical=stored)

        same_id = [s for s in existing if s.receipt.receipt_id == receipt.receipt_id]
        if same_id:
            canonical = same_id[0]
            if canonical.fingerprint == receipt.fingerprint:
                return SubmissionResult(SubmissionStatus.DUPLICATE, receipt, canonical=canonical)
            return SubmissionResult(
                SubmissionStatus.CONFLICT, receipt, canonical=canonical, conflict_with=canonical
            )

        # The same business key but a different receipt id is a distinct
        # upload generation message and must be kept, not overwritten.
        stored = StoredSubmission(receipt, self._sequence, True)
        existing.append(stored)
        return SubmissionResult(SubmissionStatus.ACCEPTED, receipt, canonical=stored)

    def submissions_for(self, key):
        return tuple(self.submissions.get(key, []))

    def canonical_submission(self, key):
        values = self.submissions.get(key, [])
        return values[0] if values else None

    def faults_for(self, key):
        return tuple(f for f in self.faults if f["receipt"]["key"] == key.to_dict())

    def attribution(self, key):
        """A compact audit view of one business key.

        It reports the accepted file and checksum submitter, any conflicts,
        and the fault receipts, so a duplicate, a same-id conflict, and a
        legitimate message from another supplier can be told apart.
        """

        entries = self.submissions_for(key)
        conflicts = []
        for fault in self.faults_for(key):
            conflicts.append({"kind": "fault", "receipt_id": fault["receipt"]["receipt_id"]})
        return {
            "key": key.to_dict(),
            "canonical": entries[0].receipt.receipt_id if entries else None,
            "receipts": [
                {
                    "receipt_id": entry.receipt.receipt_id,
                    "kind": entry.receipt.kind.value,
                    "fingerprint": entry.fingerprint,
                }
                for entry in entries
            ],
            "faults": list(self.faults_for(key)),
            "conflicts": conflicts,
        }

    # ------------------------------------------------------------------
    # Registrations
    # ------------------------------------------------------------------
    def register(self, key, endpoint, registration_id=None):
        """Register an endpoint incarnation for a receipt key.

        Each call creates a new incarnation.  Endpoint names are not unique
        over time, so a late re-registration of the *old* name cannot inherit
        the successor's authority after a confirmed handoff.
        """

        epoch = self._current_epoch(key)
        registration = Registration(
            key, endpoint, registration_id or f"{endpoint}:{uuid4().hex}", epoch
        )
        self.registrations.setdefault(key, []).append(registration)
        return registration

    def registrations_for(self, key):
        return tuple(self.registrations.get(key, []))

    def find_registration(self, key, registration_id):
        for registration in self.registrations.get(key, []):
            if registration.registration_id == registration_id:
                return registration
        return None

    def _current_epoch(self, key):
        handoff = self.handoffs.get(key)
        if handoff is None:
            return 0
        return handoff.epoch if handoff.status is HandoffStatus.CONFIRMED else handoff.epoch - 1

    # ------------------------------------------------------------------
    # Handoffs
    # ------------------------------------------------------------------
    def request_handoff(self, key, from_endpoint, to_endpoint):
        current = self.handoffs.get(key)
        epoch = 1 if current is None else current.epoch + 1
        handoff = Handoff(key, from_endpoint, to_endpoint, HandoffStatus.REQUESTED, epoch)
        self.handoffs[key] = handoff
        return handoff

    def confirm_handoff(self, key, to_registration_id=None):
        if key not in self.handoffs:
            raise KeyError(f"No handoff requested for {key}")
        handoff = self.handoffs[key]
        if handoff.status is not HandoffStatus.CONFIRMED:
            handoff = Handoff(
                handoff.key,
                handoff.from_endpoint,
                handoff.to_endpoint,
                HandoffStatus.CONFIRMED,
                handoff.epoch,
                to_registration_id,
            )
            self.handoffs[key] = handoff
            return handoff

    def handoff(self, key):
        return self.handoffs.get(key)

    def authority(self, key):
        handoff = self.handoffs.get(key)
        if handoff is None:
            return Authority(key, None, 0, HandoffStatus.NONE)
        return Authority(
            key,
            handoff.owner,
            handoff.epoch,
            handoff.status,
            handoff.owner_registration_id,
        )

    def authority_for(self, key, endpoint, registration_id=None):
        """Whether ``endpoint`` may act, optionally proving its incarnation."""

        handoff = self.handoffs.get(key)
        if handoff is None:
            return True
        if handoff.status is HandoffStatus.CONFIRMED:
            if handoff.to_registration_id is not None:
                return registration_id == handoff.to_registration_id
            return handoff.to_endpoint == endpoint
        return handoff.from_endpoint == endpoint

    def authority_allows(self, key, registration):
        return self.authority(key).allows(registration)

    # ------------------------------------------------------------------
    # Claims
    # ------------------------------------------------------------------
    def request_claim(self, key, receipt_id, claimant, request_id=None, registration_id=None):
        if request_id is None:
            request_id = f"request-{self._sequence + 1}-{claimant}"
        handoff = self.handoffs.get(key)
        request = ClaimRequest(
            request_id,
            key,
            receipt_id,
            claimant,
            committed=False,
            registration_id=registration_id,
            sequence=self._next_sequence(),
            epoch=handoff.epoch if handoff is not None else 0,
        )
        self.claim_requests.setdefault(request_id, request)
        return request

    def commit_claim(self, request_id):
        """Atomically record the first committed claim for a key.

        Returns the committed claim, or ``None`` when another endpoint won
        the race.  The important case is a request whose acknowledgement is
        lost: calling this again with the same request id is idempotent.

        Commitment obeys the authority in force *now*.  A proposal that was
        never committed before a handoff was confirmed cannot be committed
        afterwards; the delayed-acknowledgement case is represented by a
        claim that the service already holds, not by a late transition.
        """

        request = self.claim_requests.get(request_id)
        if request is None:
            raise KeyError(f"Unknown claim request {request_id}")

        existing = self.claims.get(request.key)
        if existing is not None:
            if existing.request_id == request_id:
                return existing
            return None

        if not self._file_submission_available(request):
            return None
        if not self.authority_for(request.key, request.claimant, request.registration_id):
            return None

        claim = Claim(
            request.key,
            request.receipt_id,
            request.claimant,
            request_id,
            self._next_sequence(),
            request.registration_id,
        )
        self.claims[request.key] = claim
        self.claim_requests[request_id] = ClaimRequest(
            request.request_id,
            request.key,
            request.receipt_id,
            request.claimant,
            committed=True,
            claim=claim,
            registration_id=request.registration_id,
            sequence=request.sequence,
            epoch=request.epoch,
        )
        return claim

    def claim(self, key):
        return self.claims.get(key)

    # ------------------------------------------------------------------
    # Checksum requests
    # ------------------------------------------------------------------
    def request_checksum(self, key, request_id, requester, file_claimant, receipt_id, registration_id=None):
        """Record the single checksum request for a key.

        Only the current authority may issue it.  The file claim is not
        re-validated against the requester, because a confirmed handoff
        intentionally moves this step to the successor while the old endpoint
        keeps its claim.
        """

        if key in self.checksum_requests:
            return None
        if not self.authority_for(key, requester, registration_id):
            return None
        if self.claims.get(key) is None:
            return None
        request = ChecksumRequest(request_id, key, requester, file_claimant, receipt_id)
        self.checksum_requests[key] = request
        return request

    def checksum_request(self, key):
        return self.checksum_requests.get(key)

    def lookup_claim(self, key):
        """Ordinary lookup: honest about what has not been verified.

        A saved workflow that merely looks as though it is waiting tells us
        nothing about the external claim.  The service therefore answers
        ``UNKNOWN`` until an audit is started or explicit evidence is asked
        for.
        """

        if not self._audit_started:
            return ClaimStatus.UNKNOWN
        return self._status(key)

    def begin_audit(self):
        """Start answering ordinary lookups from committed facts.

        Until this is called, two traces with identical saved state both
        receive ``UNKNOWN``: neither one is allowed to bias the other.
        """

        self._audit_started = True

    def reset_unknown(self):
        """Simulate a service that can no longer prove anything yet."""

        self._audit_started = False

    def _status(self, key):
        return ClaimStatus.CLAIMED if key in self.claims else ClaimStatus.UNCLAIMED

    def evidence(self, key):
        """Explicitly ask for evidence.

        This is the only call that converts an unknown lookup into either
        claimed or unclaimed, and it also reports requests and submissions so
        callers can attribute duplicates and conflicts.
        """

        return ClaimEvidence(
            key,
            self.claims.get(key),
            tuple(self.claim_requests.values()),
            self.submissions_for(key),
        )

    def _file_submission_available(self, request):
        submissions = self.submissions.get(request.key, [])
        return any(
            s.receipt.receipt_id == request.receipt_id and s.receipt.kind is ReceiptKind.FILE
            for s in submissions
        )

    # ------------------------------------------------------------------
    # Introspection helpers
    # ------------------------------------------------------------------
    def claim_count(self, key):
        return 1 if key in self.claims else 0

    def _next_sequence(self):
        self._sequence += 1
        return self._sequence
