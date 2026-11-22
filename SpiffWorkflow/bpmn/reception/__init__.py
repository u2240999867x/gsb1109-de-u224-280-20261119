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

"""Receipt intake, handoff-aware claims, and local reception trajectories.

The public surface is intentionally small:

* :class:`ReceiptKey` / :class:`Receipt` describe inbound uploads.
* :class:`MessageService` is the external, non-workflow ledger.
* :class:`ReceptionSession` binds one workflow archive to that ledger.
"""

from .models import (
    Authority,
    Claim,
    ClaimEvidence,
    ClaimRequest,
    ClaimStatus,
    ChecksumRequest,
    Handoff,
    HandoffStatus,
    Receipt,
    ReceiptKey,
    ReceiptKind,
    Registration,
    StoredSubmission,
    SubmissionResult,
    SubmissionStatus,
)
from .service import MessageService
from .session import (
    HandoverCoordinator,
    ReceptionSession,
    ReceptionState,
    SessionOutcome,
    SessionStatus,
)
from .bpmn import BpmnReceptionWorkflow

__all__ = [
    "Authority",
    "BpmnReceptionWorkflow",
    "Claim",
    "ClaimEvidence",
    "ClaimRequest",
    "ClaimStatus",
    "ChecksumRequest",
    "Handoff",
    "HandoffStatus",
    "HandoverCoordinator",
    "MessageService",
    "Receipt",
    "ReceiptKey",
    "ReceiptKind",
    "ReceptionSession",
    "ReceptionState",
    "Registration",
    "SessionOutcome",
    "SessionStatus",
    "StoredSubmission",
    "SubmissionResult",
    "SubmissionStatus",
]
