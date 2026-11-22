"""Standalone approval service.

Approvals are recorded here, not in the process archive or warehouse.  Grants
are per preview, per station, per output kind.  Withdrawing a grant after a
result was staged prevents that staged result from being submitted, but it
does not rewrite history: the staging record and any prior task/credential
records remain visible in the archive.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime, timezone
from threading import RLock
from typing import Dict, Iterable, List, Optional, Tuple

from .errors import ApprovalError, GrantNotPermittedError
from .model import GrantOperation, OutputKind, OutputAuthorization, Preview, Station


@dataclass(frozen=True)
class GrantRecord:
    preview_id: str
    station: Station
    output: OutputKind
    can_create_revision: bool
    can_rewrite_version: bool
    rewrite_requires_unsigned: bool
    granted_by: str
    granted_at: datetime
    withdrawn_at: Optional[datetime] = None
    withdrawn_by: Optional[str] = None

    @property
    def active(self) -> bool:
        return self.withdrawn_at is None

    def permits(self, operation: GrantOperation) -> bool:
        if not self.active:
            return False
        if operation is GrantOperation.CREATE_REVISION:
            return self.can_create_revision
        return self.can_rewrite_version


@dataclass(frozen=True)
class ApprovalDecision:
    preview_id: str
    approved: bool
    decided_by: str
    decided_at: datetime
    scope: Tuple[OutputKind, ...]
    note: str = ""

    def permits(self, kind: OutputKind) -> bool:
        return self.approved and kind in self.scope


class ApprovalStore:
    def __init__(self) -> None:
        self._lock = RLock()
        self._grants: Dict[Tuple[str, Station, OutputKind], GrantRecord] = {}
        self._decisions: Dict[str, List[ApprovalDecision]] = {}

    def grant(
        self,
        preview_id: str,
        station: Station,
        authorization: OutputAuthorization,
        granted_by: str,
        now: Optional[datetime] = None,
    ) -> GrantRecord:
        if (
            authorization.output.station is not None
            and authorization.output.station != station
        ):
            raise GrantNotPermittedError(
                f"{authorization.output.value} belongs to "
                f"{authorization.output.station}; not {station.value}"
            )
        record = GrantRecord(
            preview_id=preview_id,
            station=station,
            output=authorization.output,
            can_create_revision=authorization.can_create_revision,
            can_rewrite_version=authorization.can_rewrite_version,
            rewrite_requires_unsigned=authorization.rewrite_requires_unsigned,
            granted_by=granted_by,
            granted_at=now or datetime.now(timezone.utc),
        )
        with self._lock:
            self._grants[(preview_id, station, authorization.output)] = record
        return record

    def withdraw(
        self,
        preview_id: str,
        station: Station,
        output: OutputKind,
        withdrawn_by: str,
        now: Optional[datetime] = None,
    ) -> GrantRecord:
        key = (preview_id, station, output)
        with self._lock:
            record = self._grants.get(key)
            if record is None:
                raise ApprovalError(f"no grant for {output.value} on preview {preview_id}")
            withdrawn = replace(
                record,
                withdrawn_at=now or datetime.now(timezone.utc),
                withdrawn_by=withdrawn_by,
            )
            self._grants[key] = withdrawn
            return withdrawn

    def grant_for(self, preview_id: str, station: Station, output: OutputKind) -> Optional[GrantRecord]:
        with self._lock:
            return self._grants.get((preview_id, station, output))

    def active_grants(self, preview_id: str) -> List[GrantRecord]:
        with self._lock:
            return [
                record
                for (pid, _, _), record in sorted(
                    self._grants.items(), key=lambda item: (item[0][1].value, item[0][2].value)
                )
                if pid == preview_id and record.active
            ]

    def all_grants(self, preview_id: str) -> List[GrantRecord]:
        with self._lock:
            return [
                record
                for (pid, _, _), record in sorted(
                    self._grants.items(), key=lambda item: (item[0][1].value, item[0][2].value)
                )
                if pid == preview_id
            ]

    def permits(self, preview_id: str, station: Station, kind: OutputKind, operation: GrantOperation) -> bool:
        record = self.grant_for(preview_id, station, kind)
        return bool(record and record.permits(operation))

    def approve(
        self,
        preview: Preview,
        decided_by: str,
        scope: Optional[Iterable[OutputKind]] = None,
        note: str = "",
        now: Optional[datetime] = None,
    ) -> ApprovalDecision:
        kinds = tuple(dict.fromkeys(scope)) if scope is not None else tuple(
            dict.fromkeys(w.target.kind for w in preview.writes)
        )
        missing = []
        for kind in kinds:
            write = next((w for w in preview.writes if w.target.kind == kind), None)
            if write is None:
                continue
            station = kind.station
            if station is None:
                # Cross-station outputs need an explicit grant on one of the
                # preview's stations (the reader's approval), not an implicit
                # station grant derived from the kind.
                holders = [
                    self.grant_for(preview.preview_id, candidate, kind)
                    for candidate in preview.stations
                ]
                if not any(holder and holder.permits(write.operation) for holder in holders):
                    missing.append(f"{kind.value}/{write.operation.value}")
                continue
            if not self.permits(preview.preview_id, station, kind, write.operation):
                missing.append(f"{kind.value}/{write.operation.value}")
        if missing:
            raise ApprovalError(
                "approval requires active grants for: " + ", ".join(sorted(set(missing)))
            )
        decision = ApprovalDecision(
            preview_id=preview.preview_id,
            approved=True,
            decided_by=decided_by,
            decided_at=now or datetime.now(timezone.utc),
            scope=kinds,
            note=note,
        )
        with self._lock:
            self._decisions.setdefault(preview.preview_id, []).append(decision)
        return decision

    def revoke_decision(self, preview: Preview, decided_by: str, note: str = "") -> ApprovalDecision:
        decision = ApprovalDecision(
            preview_id=preview.preview_id,
            approved=False,
            decided_by=decided_by,
            decided_at=datetime.now(timezone.utc),
            scope=tuple(w.target.kind for w in preview.writes),
            note=note,
        )
        with self._lock:
            self._decisions.setdefault(preview.preview_id, []).append(decision)
        return decision

    def decisions(self, preview_id: str) -> List[ApprovalDecision]:
        with self._lock:
            return list(self._decisions.get(preview_id, ()))

    def latest_decision(self, preview_id: str) -> Optional[ApprovalDecision]:
        with self._lock:
            decisions = self._decisions.get(preview_id, ())
            return decisions[-1] if decisions else None

    def is_approved(self, preview_id: str, kind: OutputKind) -> bool:
        decision = self.latest_decision(preview_id)
        return bool(decision and decision.permits(kind))
