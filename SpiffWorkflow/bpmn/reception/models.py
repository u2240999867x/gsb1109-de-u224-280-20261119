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

"""Models shared by the supplier-batch reception service and its sessions.

A receipt identifies one upload generation of one supplier batch.  The
``supplier`` component is essential: two suppliers may legitimately use the
same batch number (for example batch ``7``) at the same time.
"""

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Optional


class ReceiptKind(str, Enum):
    """The kind of thing a receipt reports.

    A *file* receipt makes a batch upload available for claiming.  A
    *checksum* receipt reports the outcome of the verification step and can
    never stand in for the file itself.
    """

    FILE = "file"
    CHECKSUM = "checksum"


class SubmissionStatus(str, Enum):
    """Outcome of handing a receipt to the message service."""

    ACCEPTED = "accepted"
    DUPLICATE = "duplicate"
    CONFLICT = "conflict"


class ClaimStatus(str, Enum):
    """The three states a claim lookup is allowed to return.

    ``UNKNOWN`` is a first-class answer.  It means that the service has not
    yet produced evidence, not that the batch is unclaimed.
    """

    CLAIMED = "claimed"
    UNCLAIMED = "unclaimed"
    UNKNOWN = "unknown"


class HandoffStatus(str, Enum):
    """Lifecycle of a supervisor-initiated reception handoff."""

    NONE = "none"
    REQUESTED = "requested"
    CONFIRMED = "confirmed"


@dataclass(frozen=True)
class ReceiptKey:
    """Identity of an upload generation for a supplier batch."""

    supplier: str
    batch: int
    generation: int

    def __post_init__(self):
        if not isinstance(self.supplier, str) or not self.supplier.strip():
            raise ValueError("supplier must be a non-empty string")
        if not isinstance(self.batch, int) or isinstance(self.batch, bool):
            raise ValueError("batch must be an integer")
        if not isinstance(self.generation, int) or isinstance(self.generation, bool):
            raise ValueError("generation must be an integer")

    def to_dict(self) -> dict:
        return {"supplier": self.supplier, "batch": self.batch, "generation": self.generation}

    @classmethod
    def from_dict(cls, dct: Mapping[str, Any]) -> "ReceiptKey":
        return cls(dct["supplier"], dct["batch"], dct["generation"])

    def __str__(self) -> str:
        return f"{self.supplier}/batch-{self.batch}/generation-{self.generation}"


def _canonical_payload(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


@dataclass(frozen=True)
class Receipt:
    """One receipt delivered by an upload sender.

    ``receipt_id`` is the sender's identifier.  Re-sending the same identifier
    with the same body is a duplicate; re-sending it with a different body is
    a conflict that must be attributed rather than silently overwriting the
    accepted version.
    """

    key: ReceiptKey
    kind: ReceiptKind
    receipt_id: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if not isinstance(self.receipt_id, str) or not self.receipt_id.strip():
            raise ValueError("receipt_id must be a non-empty string")
        if not isinstance(self.kind, ReceiptKind):
            object.__setattr__(self, "kind", ReceiptKind(self.kind))

    @property
    def fingerprint(self) -> str:
        """Stable digest of everything except the sender's identifier.

        The identifier is excluded so that the same identifier with a
        different body produces a different fingerprint.
        """

        body = {
            "key": self.key.to_dict(),
            "kind": self.kind.value,
            "payload": self.payload,
        }
        return hashlib.sha256(_canonical_payload(body).encode("utf-8")).hexdigest()

    def to_dict(self) -> dict:
        return {
            "key": self.key.to_dict(),
            "kind": self.kind.value,
            "receipt_id": self.receipt_id,
            "payload": dict(self.payload),
            "fingerprint": self.fingerprint,
        }

    @classmethod
    def from_dict(cls, dct: Mapping[str, Any]) -> "Receipt":
        return cls(
            ReceiptKey.from_dict(dct["key"]),
            ReceiptKind(dct["kind"]),
            dct["receipt_id"],
            dct.get("payload", {}),
        )


@dataclass(frozen=True)
class StoredSubmission:
    """A submission retained by the message service."""

    receipt: Receipt
    sequence: int
    canonical: bool

    @property
    def fingerprint(self) -> str:
        return self.receipt.fingerprint


@dataclass(frozen=True)
class SubmissionResult:
    """Attributed outcome of a submission.

    ``canonical`` always points at the first accepted submission for the
    business key.  ``conflict_with`` is set for a same-id/different-body
    conflict so callers can report which accepted version was kept.
    """

    status: SubmissionStatus
    incoming: Receipt
    canonical: Optional[StoredSubmission] = None
    conflict_with: Optional[StoredSubmission] = None

    @property
    def accepted(self) -> bool:
        return self.status in (SubmissionStatus.ACCEPTED, SubmissionStatus.DUPLICATE)

    @property
    def attribution(self) -> Optional[str]:
        canonical = self.canonical or self.conflict_with
        return canonical.receipt.receipt_id if canonical is not None else None


@dataclass(frozen=True)
class Claim:
    """An atomic claim of the file for a receipt key."""

    key: ReceiptKey
    receipt_id: str
    claimant: str
    request_id: str
    sequence: int
    registration_id: Optional[str] = None

    def belongs_to(self, registration: Optional["Registration"]) -> bool:
        if registration is None:
            return self.registration_id is None
        return self.registration_id == registration.registration_id


@dataclass(frozen=True)
class ClaimRequest:
    """A claim proposal.

    A request by itself grants nothing.  Only ``MessageService.commit_claim``
    transitions it to a committed claim, which is why a lost response can be
    reconciled without guessing from local workflow state.
    """

    request_id: str
    key: ReceiptKey
    receipt_id: str
    claimant: str
    committed: bool = False
    claim: Optional[Claim] = None
    registration_id: Optional[str] = None
    sequence: int = 0
    epoch: int = 0


@dataclass(frozen=True)
class ClaimEvidence:
    """Authoritative evidence returned by the message service.

    ``verified`` is always true for evidence.  Ordinary lookups return
    ``ClaimStatus.UNKNOWN`` until the caller explicitly asks for evidence.
    """

    key: ReceiptKey
    claim: Optional[Claim]
    requests: tuple[ClaimRequest, ...] = ()
    submissions: tuple[StoredSubmission, ...] = ()

    @property
    def status(self) -> ClaimStatus:
        return ClaimStatus.CLAIMED if self.claim is not None else ClaimStatus.UNCLAIMED


@dataclass(frozen=True)
class Handoff:
    """Supervisor-controlled transfer of reception authority."""

    key: ReceiptKey
    from_endpoint: str
    to_endpoint: str
    status: HandoffStatus
    epoch: int
    to_registration_id: Optional[str] = None

    @property
    def owner(self) -> str:
        if self.status is HandoffStatus.CONFIRMED:
            return self.to_endpoint
        return self.from_endpoint

    @property
    def owner_registration_id(self) -> Optional[str]:
        if self.status is HandoffStatus.CONFIRMED:
            return self.to_registration_id
        return None


@dataclass(frozen=True)
class Authority:
    """The endpoint currently allowed to act for a receipt key."""

    key: ReceiptKey
    endpoint: Optional[str]
    epoch: int
    status: HandoffStatus
    registration_id: Optional[str] = None

    def allows(self, registration: Optional["Registration"]) -> bool:
        """Whether a registration is the current one.

        With no handoff, any registration for the key is acceptable: this is
        what keeps existing purely local message usage working.
        """

        if self.status is HandoffStatus.NONE:
            return registration is None or registration.key == self.key
        if registration is None:
            return False
        if self.registration_id is not None:
            # A confirmed handoff names the successor incarnation.  Epochs do
            # not help here because the successor normally registers before
            # the supervisor confirms the transfer.
            return registration.registration_id == self.registration_id
        if self.epoch != registration.epoch:
            return False
        return self.endpoint == registration.endpoint


@dataclass(frozen=True)
class Registration:
    """A receiver's registration with the message service.

    ``registration_id`` is an incarnation identifier, not an endpoint name.
    A late registration by an endpoint with the *same name* as the previous
    owner gets a different incarnation and therefore cannot satisfy the
    confirmed handoff.
    """

    key: ReceiptKey
    endpoint: str
    registration_id: str
    epoch: int

    def is_current(self, authority: Authority) -> bool:
        return authority.allows(self)


@dataclass(frozen=True)
class ChecksumRequest:
    """An outbound checksum request, recorded separately from claim state."""

    request_id: str
    key: ReceiptKey
    requester: str
    file_claimant: str
    receipt_id: str
