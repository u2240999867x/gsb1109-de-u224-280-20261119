"""Warehouse with per-output, version-checked acceptance.

The warehouse is an external system.  It accepts or rejects each output
independently against the version it currently holds, checks that declared
input versions are still the effective ones, and records a signature when an
output's books are closed.  It knows nothing about the process archive or the
approval service, and queued writes are not effective until they are accepted
and observed by a caller.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from threading import RLock
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from .errors import UnknownOutputError
from .model import (
    CommitReceipt,
    GrantOperation,
    OutputTarget,
    StagedResult,
    WarehouseVersion,
    WriteOutcome,
)


@dataclass(frozen=True)
class WarehousePolicy:
    """How a particular warehouse wants to treat each output."""

    kind_key: str
    require_matching_base: bool = True
    accept_new_revisions: bool = True
    allow_rewrite: bool = True
    rewrite_blocked_by_signature: bool = True
    require_matching_inputs: bool = True


@dataclass(frozen=True)
class Signature:
    """A warehouse receipt over one exact version of an output.

    A signature is separate from acceptance: it means the warehouse has closed
    the books on that version, so rewriting it is no longer permitted even if
    no newer version exists.
    """

    key: str
    version: int
    signed_by: str
    signed_at: datetime
    note: str = ""


@dataclass(frozen=True)
class PendingSubmission:
    preview_id: str
    staged: Tuple[StagedResult, ...]
    queued_at: datetime
    queued_by: str


class Warehouse:
    """An external sink for staged results."""

    def __init__(self, policies: Optional[Mapping[str, WarehousePolicy]] = None) -> None:
        self._lock = RLock()
        self._versions: Dict[str, List[WarehouseVersion]] = {}
        self._policies = dict(policies or {})
        self._deliveries: Dict[str, CommitReceipt] = {}
        self._accepted_log: List[WarehouseVersion] = []
        self._signatures: Dict[str, Signature] = {}
        self._pending: Dict[str, PendingSubmission] = {}

    # -- output setup and inspection -------------------------------------

    def policy_for(self, target: OutputTarget) -> WarehousePolicy:
        return self._policies.get(
            target.kind.value,
            WarehousePolicy(kind_key=target.kind.value),
        )

    def ensure_output(self, target: OutputTarget, version: int = 0, payload: Optional[Mapping[str, object]] = None) -> None:
        """Create an output at a known version (0 means 'does not exist yet')."""

        with self._lock:
            if target.key() in self._versions:
                return
            self._versions[target.key()] = [
                WarehouseVersion(
                    name=target.name,
                    table=target.table,
                    kind=target.kind,
                    version=version,
                    payload=dict(payload or {}),
                    accepted_at=datetime.now(timezone.utc),
                    preview_id=None,
                    write_id=None,
                )
            ]

    def current(self, target: OutputTarget) -> WarehouseVersion:
        with self._lock:
            versions = self._versions.get(target.key())
            if not versions:
                raise UnknownOutputError(f"warehouse has no output {target.label()}")
            return versions[-1]

    def find_current(self, key: str) -> Optional[WarehouseVersion]:
        with self._lock:
            versions = self._versions.get(key)
            if versions:
                return versions[-1]
            # Dependency keys may omit the table (kind:name).  If exactly one
            # artifact matches that prefix, it is the intended dependency.
            matches = [
                entry[-1]
                for full_key, entry in self._versions.items()
                if full_key.rsplit(":", 1)[0] == key
            ]
            return matches[0] if len(matches) == 1 else None

    def versions(self, target: OutputTarget) -> List[WarehouseVersion]:
        with self._lock:
            return list(self._versions.get(target.key(), ()))

    def all_current(self) -> Mapping[str, WarehouseVersion]:
        with self._lock:
            return {key: versions[-1] for key, versions in self._versions.items()}

    # -- signatures -------------------------------------------------------

    def sign_off(
        self,
        target: OutputTarget,
        signed_by: str,
        note: str = "",
        version: Optional[int] = None,
    ) -> Signature:
        """Close the books on the current (or a specific) version."""

        with self._lock:
            current = self.current(target)
            version = current.version if version is None else version
            signature = Signature(
                key=target.key(),
                version=version,
                signed_by=signed_by,
                signed_at=datetime.now(timezone.utc),
                note=note,
            )
            self._signatures[target.key()] = signature
            return signature

    def signature(self, target: OutputTarget) -> Optional[Signature]:
        with self._lock:
            return self._signatures.get(target.key())

    def is_signed(self, target: OutputTarget, version: Optional[int] = None) -> bool:
        signature = self.signature(target)
        if signature is None:
            return False
        return version is None or signature.version == version

    # -- acceptance -------------------------------------------------------

    def accept(self, staged: StagedResult, accepted_by: str = "warehouse") -> WriteOutcome:
        with self._lock:
            key = staged.target.key()
            versions = self._versions.get(key)
            policy = self.policy_for(staged.target)
            if versions is None:
                return WriteOutcome(staged.target, False, None, staged.write_id, "unknown output")
            current = versions[-1]

            if policy.require_matching_inputs:
                changed = []
                for dependency_key, expected in staged.input_versions.items():
                    dependency = self.find_current(dependency_key)
                    actual = None if dependency is None else dependency.version
                    if actual != expected:
                        changed.append((dependency_key, expected, actual))
                if changed:
                    details = ", ".join(
                        f"{key} expected {expected}, found {actual}" for key, expected, actual in changed
                    )
                    return WriteOutcome(
                        staged.target,
                        False,
                        current.version,
                        staged.write_id,
                        f"stale input version: {details}",
                    )

            if staged.operation is GrantOperation.REWRITE_VERSION:
                if not policy.allow_rewrite:
                    return WriteOutcome(staged.target, False, current.version, staged.write_id, "rewrite not allowed")
                if staged.rewrite_requires_unsigned and self.is_signed(staged.target, current.version):
                    signature = self.signature(staged.target)
                    return WriteOutcome(
                        staged.target,
                        False,
                        current.version,
                        staged.write_id,
                        f"version {current.version} was signed by {signature.signed_by}; "
                        "conditional rewrite does not apply",
                    )
                if policy.rewrite_blocked_by_signature and self.is_signed(staged.target, current.version):
                    signature = self.signature(staged.target)
                    return WriteOutcome(
                        staged.target,
                        False,
                        current.version,
                        staged.write_id,
                        f"version {current.version} was signed by {signature.signed_by}; "
                        "rewrite is no longer permitted",
                    )
                if staged.base_version is None or staged.base_version != current.version:
                    return WriteOutcome(
                        staged.target,
                        False,
                        current.version,
                        staged.write_id,
                        f"rewrite requires version {staged.base_version}, warehouse has {current.version}",
                    )
                version = current.version
            else:
                if not policy.accept_new_revisions:
                    return WriteOutcome(staged.target, False, current.version, staged.write_id, "new revisions not allowed")
                if policy.require_matching_base:
                    if staged.base_version is None and current.version > 0:
                        return WriteOutcome(
                            staged.target,
                            False,
                            current.version,
                            staged.write_id,
                            "new revision requires an explicit base version",
                        )
                    if staged.base_version is not None and staged.base_version != current.version:
                        return WriteOutcome(
                            staged.target,
                            False,
                            current.version,
                            staged.write_id,
                            f"new revision requires base {staged.base_version}, warehouse has {current.version}",
                        )
                version = current.version + 1

            accepted = WarehouseVersion(
                name=staged.target.name,
                table=staged.target.table,
                kind=staged.target.kind,
                version=version,
                payload=dict(staged.values),
                accepted_at=datetime.now(timezone.utc),
                preview_id=staged.preview_id,
                write_id=staged.write_id,
                input_versions=dict(staged.input_versions),
                accepted_by=accepted_by,
            )
            if staged.operation is GrantOperation.REWRITE_VERSION:
                versions[-1] = accepted
            else:
                versions.append(accepted)
            self._accepted_log.append(accepted)
            return WriteOutcome(staged.target, True, version, staged.write_id, None)

    def commit(
        self,
        preview_id: str,
        staged: Sequence[StagedResult],
        submitter: str = "coordinator",
        deliver: bool = True,
    ) -> CommitReceipt:
        """Submit now, either accepting immediately or queuing for a later answer."""

        if not deliver:
            self.queue(preview_id, staged, queued_by=submitter)
            receipt = CommitReceipt(
                preview_id=preview_id,
                submitted_at=datetime.now(timezone.utc),
                outcomes=tuple(
                    WriteOutcome(item.target, False, None, item.write_id, "pending warehouse acceptance", submitted=True)
                    for item in staged
                ),
                pending=True,
            )
            return receipt
        outcomes = tuple(self.accept(item, accepted_by=submitter) for item in staged)
        receipt = CommitReceipt(
            preview_id=preview_id,
            submitted_at=datetime.now(timezone.utc),
            outcomes=outcomes,
            delivered_at=datetime.now(timezone.utc),
        )
        with self._lock:
            self._deliveries[preview_id] = receipt
        return receipt

    # -- queued external acceptance --------------------------------------

    def queue(self, preview_id: str, staged: Sequence[StagedResult], queued_by: str = "coordinator") -> PendingSubmission:
        submission = PendingSubmission(
            preview_id=preview_id,
            staged=tuple(staged),
            queued_at=datetime.now(timezone.utc),
            queued_by=queued_by,
        )
        with self._lock:
            self._pending[preview_id] = submission
        return submission

    def pending(self, preview_id: str) -> Optional[PendingSubmission]:
        with self._lock:
            return self._pending.get(preview_id)

    def pending_ids(self) -> List[str]:
        with self._lock:
            return list(self._pending)

    def deliver(self, preview_id: str) -> Optional[CommitReceipt]:
        """External acceptance of an already queued submission.

        Callers may use this to model the warehouse answering after the
        coordinator's local transaction has ended.  The returned receipt is
        only written into the warehouse's delivery log; the process archive
        must observe it separately.
        """

        with self._lock:
            submission = self._pending.pop(preview_id, None)
            if submission is None:
                return self._deliveries.get(preview_id)
            outcomes = tuple(self.accept(item, accepted_by="warehouse") for item in submission.staged)
            receipt = CommitReceipt(
                preview_id=preview_id,
                submitted_at=submission.queued_at,
                outcomes=outcomes,
                delivered_at=datetime.now(timezone.utc),
            )
            self._deliveries[preview_id] = receipt
            return receipt

    def take_pending(self, preview_id: str) -> Optional[PendingSubmission]:
        """Remove a queued submission so the coordinator can re-check it first."""

        with self._lock:
            return self._pending.pop(preview_id, None)

    # -- history ----------------------------------------------------------

    def delivery(self, preview_id: str) -> Optional[CommitReceipt]:
        with self._lock:
            return self._deliveries.get(preview_id)

    def deliveries(self) -> List[CommitReceipt]:
        with self._lock:
            return list(self._deliveries.values())

    def accepted_log(self) -> List[WarehouseVersion]:
        with self._lock:
            return list(self._accepted_log)
