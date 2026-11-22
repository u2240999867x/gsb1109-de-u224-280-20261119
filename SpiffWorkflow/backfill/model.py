"""Domain objects for previewing and applying backfill work.

The vocabulary is deliberately independent of the workflow engine so the same
objects can be stored in the process archive, checked by a separate approval
service, and compared with warehouse state later.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Dict, Iterable, Mapping, Optional, Sequence, Tuple


class OutputKind(str, Enum):
    NORTH_SAMPLE = "north_sample"
    NORTH_TRANSFORM = "north_transform"
    NORTH_CREDENTIAL = "north_credential"
    NORTH_EXPORT = "north_export"
    SOUTH_SAMPLE = "south_sample"
    SOUTH_TRANSFORM = "south_transform"
    SOUTH_CREDENTIAL = "south_credential"
    SOUTH_EXPORT = "south_export"
    MONTH_TABLE = "month_table"

    @property
    def station(self) -> Optional["Station"]:
        if self.value.startswith("north_"):
            return Station.NORTH
        if self.value.startswith("south_"):
            return Station.SOUTH
        return None

    @property
    def role(self) -> str:
        if self is OutputKind.MONTH_TABLE:
            return "month_table"
        return self.value.split("_", 1)[1]

    @property
    def is_evidence(self) -> bool:
        """Samples and credentials are the evidence a station sends upstream."""

        return self.role in {"sample", "credential", "export"}


class Station(str, Enum):
    NORTH = "north"
    SOUTH = "south"


class GrantOperation(str, Enum):
    CREATE_REVISION = "create_revision"
    REWRITE_VERSION = "rewrite_version"


class PreviewStatus(str, Enum):
    DRAFT = "draft"
    CONFIRMED = "confirmed"
    COMMITTING = "committing"
    ACCEPTED = "accepted"
    PARTIALLY_ACCEPTED = "partially_accepted"
    REJECTED = "rejected"
    WITHDRAWN = "withdrawn"
    SUPERSEDED = "superseded"


@dataclass(frozen=True)
class OutputAuthorization:
    """What the caller declares for one kind of output.

    Both capabilities default to false: absent an explicit declaration neither
    new revisions nor rewrites of an existing version may be produced.
    ``rewrite_requires_unsigned`` narrows a rewrite grant to versions the
    warehouse has not signed off; once signed, the grant no longer applies.
    """

    output: OutputKind
    can_create_revision: bool = False
    can_rewrite_version: bool = False
    rewrite_requires_unsigned: bool = False

    def permits(self, operation: GrantOperation) -> bool:
        if operation is GrantOperation.CREATE_REVISION:
            return self.can_create_revision
        return self.can_rewrite_version

    def describe(self) -> Dict[str, object]:
        return {
            "can_create_revision": self.can_create_revision,
            "can_rewrite_version": self.can_rewrite_version,
            "rewrite_requires_unsigned": self.rewrite_requires_unsigned,
        }


@dataclass(frozen=True)
class OutputTarget:
    """A concrete artifact an action wants to write."""

    kind: OutputKind
    name: str
    table: Optional[str] = None

    def label(self) -> str:
        if self.table:
            return f"{self.name} ({self.table})"
        return self.name

    def key(self) -> str:
        return f"{self.kind.value}:{self.name}:{self.table or ''}"

    def dependency_key(self) -> str:
        return f"{self.kind.value}:{self.name}"


@dataclass(frozen=True)
class PlannedWrite:
    """One write a preview expects to submit to the warehouse."""

    target: OutputTarget
    operation: GrantOperation
    # Version the preview believes is current for the target itself.
    base_version: Optional[int] = None
    # Versions of upstream outputs the preview's values were derived from.
    input_versions: Mapping[str, int] = field(default_factory=dict)
    # Previews sharing a write_id describe the same logical write, even when a
    # later preview re-derives it from newer evidence.
    write_id: str = ""


@dataclass(frozen=True)
class PreviewRequest:
    request_id: str
    stations: Sequence[Station]
    writes: Sequence[PlannedWrite]
    authorizations: Mapping[OutputKind, OutputAuthorization]
    archive_id: str = ""
    description: str = ""
    # Downstream tables this preview will affect even if it writes them in a
    # later preview (for example a month table that references both stations).
    affected_tables: Sequence[str] = ()

    def authorization_for(self, kind: OutputKind) -> OutputAuthorization:
        found = self.authorizations.get(kind)
        return found if found is not None else OutputAuthorization(kind)


@dataclass(frozen=True)
class StagedResult:
    """Result computed by a station branch, before warehouse acceptance."""

    preview_id: str
    output: OutputKind
    target: OutputTarget
    values: Mapping[str, object]
    write_id: str
    operation: GrantOperation
    base_version: Optional[int]
    input_versions: Mapping[str, int]
    rewrite_requires_unsigned: bool = False
    produced_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    def as_payload(self) -> Dict[str, object]:
        return {
            "preview_id": self.preview_id,
            "output": self.output.value,
            "target": {
                "kind": self.target.kind.value,
                "name": self.target.name,
                "table": self.target.table,
            },
            "values": dict(self.values),
            "write_id": self.write_id,
            "operation": self.operation.value,
            "base_version": self.base_version,
            "input_versions": dict(self.input_versions),
            "rewrite_requires_unsigned": self.rewrite_requires_unsigned,
        }


@dataclass(frozen=True)
class WriteIntent:
    """A previewed write and whether the caller declared it permissible."""

    target: OutputTarget
    operation: GrantOperation
    base_version: Optional[int]
    input_versions: Mapping[str, int]
    write_id: str
    authorized: bool
    rewrite_requires_unsigned: bool = False
    blocked_reason: Optional[str] = None

    @classmethod
    def from_planned(cls, write: PlannedWrite, authorization: OutputAuthorization) -> "WriteIntent":
        authorized = authorization.permits(write.operation)
        reason = None
        if not authorized:
            reason = f"{write.operation.value} is not declared for {write.target.kind.value}"
        return cls(
            target=write.target,
            operation=write.operation,
            base_version=write.base_version,
            input_versions=dict(write.input_versions),
            write_id=write.write_id,
            authorized=authorized,
            rewrite_requires_unsigned=(
                write.operation is GrantOperation.REWRITE_VERSION
                and authorization.rewrite_requires_unsigned
            ),
            blocked_reason=reason,
        )


@dataclass(frozen=True)
class Impact:
    """Human-readable effect of a preview."""

    writes: Sequence[WriteIntent]
    affected_tables: Sequence[str] = ()
    shared_with: Sequence[str] = ()
    station_writes: Mapping[Station, Sequence[OutputKind]] = field(default_factory=dict)

    def writes_for(self, station: Station) -> Sequence[WriteIntent]:
        kinds = set(self.station_writes.get(station, ()))
        return [w for w in self.writes if w.target.kind in kinds]

    def unpermitted(self) -> Sequence[WriteIntent]:
        return [w for w in self.writes if not w.authorized]

    def describe(self) -> Dict[str, object]:
        return {
            "writes": [
                {
                    "output": w.target.kind.value,
                    "target": w.target.label(),
                    "operation": w.operation.value,
                    "base_version": w.base_version,
                    "authorized": w.authorized,
                }
                for w in self.writes
            ],
            "affected_tables": list(self.affected_tables),
            "shared_with": list(self.shared_with),
            "stations": {
                station.value: [kind.value for kind in kinds]
                for station, kinds in self.station_writes.items()
            },
        }


@dataclass(frozen=True)
class Preview:
    preview_id: str
    request: PreviewRequest
    impact: Impact
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    status: PreviewStatus = PreviewStatus.DRAFT
    confirmed_at: Optional[datetime] = None
    confirmed_by: Optional[str] = None
    parent_preview_id: Optional[str] = None
    attempt: int = 1
    rejection_reason: Optional[str] = None

    @property
    def stations(self) -> Sequence[Station]:
        return self.request.stations

    @property
    def writes(self) -> Sequence[WriteIntent]:
        return self.impact.writes

    def with_status(self, status: PreviewStatus, **changes) -> "Preview":
        return replace(self, status=status, **changes)

    def is_open(self) -> bool:
        return self.status in {
            PreviewStatus.DRAFT,
            PreviewStatus.CONFIRMED,
            PreviewStatus.COMMITTING,
        }


@dataclass(frozen=True)
class WarehouseVersion:
    name: str
    table: Optional[str]
    kind: OutputKind
    version: int
    payload: Mapping[str, object]
    accepted_at: datetime
    preview_id: Optional[str]
    write_id: Optional[str]
    # Versions this payload was derived from, keyed by dependency_key().
    input_versions: Mapping[str, int] = field(default_factory=dict)
    accepted_by: str = "warehouse"

    def target(self) -> OutputTarget:
        return OutputTarget(self.kind, self.name, self.table)

    def key(self) -> str:
        return self.target().key()


@dataclass(frozen=True)
class WriteOutcome:
    target: OutputTarget
    accepted: bool
    version: Optional[int]
    write_id: Optional[str] = None
    reason: Optional[str] = None
    submitted: bool = True


@dataclass(frozen=True)
class CommitReceipt:
    preview_id: str
    submitted_at: datetime
    outcomes: Sequence[WriteOutcome]
    delivered_at: Optional[datetime] = None
    pending: bool = False

    @property
    def accepted(self) -> bool:
        return bool(self.outcomes) and all(o.accepted for o in self.outcomes)

    @property
    def accepted_writes(self) -> Sequence[WriteOutcome]:
        return [o for o in self.outcomes if o.accepted]

    @property
    def rejected_writes(self) -> Sequence[WriteOutcome]:
        return [o for o in self.outcomes if not o.accepted]


@dataclass(frozen=True)
class UneffectiveResult:
    """A staged result that is not the warehouse's effective version."""

    staged: StagedResult
    reason: str


@dataclass(frozen=True)
class UnauthorizedStep:
    preview_id: str
    intent: WriteIntent
    station: Optional[Station]
    reason: str


@dataclass(frozen=True)
class DependencyMismatch:
    """The effective version of an output does not use the effective inputs."""

    key: str
    effective_version: int
    expected: Mapping[str, int]
    actual: Mapping[str, int]


@dataclass(frozen=True)
class RecoveryReport:
    generated_at: datetime
    archive_lag: bool
    accepted_versions: Mapping[str, WarehouseVersion]
    local_versions: Mapping[str, int]
    pending_results: Sequence[StagedResult]
    superseded_previews: Sequence[Preview]
    unauthorized_steps: Sequence[UnauthorizedStep]
    uneffective_results: Sequence[UneffectiveResult] = ()
    dependency_mismatches: Sequence[DependencyMismatch] = ()
    pending_deliveries: Sequence[str] = ()
    notes: Sequence[str] = ()

    def effective_version(self, target: OutputTarget) -> Optional[WarehouseVersion]:
        return self.accepted_versions.get(target.key())


@dataclass(frozen=True)
class ResumeResult:
    preview_id: str
    resumed_at: datetime
    accepted_versions: Mapping[str, WarehouseVersion]
    submitted: Sequence[WriteOutcome]
    uneffective_results: Sequence[UneffectiveResult]
    unauthorized_steps: Sequence[UnauthorizedStep]
    requires_repreview: bool
    successor_preview_id: Optional[str] = None
    notes: Sequence[str] = ()


@dataclass(frozen=True)
class ArchiveSnapshot:
    previews: Tuple[Preview, ...]
    staged: Tuple[StagedResult, ...]
    receipts: Tuple[CommitReceipt, ...]
    task_states: Mapping[Tuple[str, str], Mapping[str, object]]
    credentials: Mapping[str, Mapping[str, object]]


def station_kinds(*stations: Station) -> Tuple[OutputKind, ...]:
    allowed = []
    for kind in OutputKind:
        if kind.station in stations:
            allowed.append(kind)
    return tuple(allowed)


def unique_kinds(kinds: Iterable[OutputKind]) -> Tuple[OutputKind, ...]:
    result = []
    for kind in kinds:
        if kind not in result:
            result.append(kind)
    return tuple(result)
