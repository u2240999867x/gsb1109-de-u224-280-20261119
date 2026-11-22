"""Coordinator for preview-first backfill across north and south stations.

Responsibilities:

* Build previews that name every output a confirmation would write, while
  keeping each station's branch limited to that station's outputs.
* Confirm only an approved scope, and never infer authority from a preview.
* Stage branch results locally, then submit them one output at a time to a
  warehouse that accepts or rejects against its own current version.
* Reconcile archive lag and partial acceptance: a locally saved revision is not
  proof of external acceptance, and an accepted revision cannot be overwritten
  by replaying an older preview.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from itertools import count
from typing import Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from .approvals import ApprovalStore
from .archive import ArchiveStore
from .errors import (
    ApprovalError,
    CrossStationImpactError,
    PreviewNotFoundError,
    PreviewStateError,
    StaleVersionError,
    UnauthorizedOperationError,
)
from .model import (
    CommitReceipt,
    DependencyMismatch,
    GrantOperation,
    Impact,
    OutputKind,
    OutputTarget,
    PlannedWrite,
    Preview,
    PreviewRequest,
    PreviewStatus,
    RecoveryReport,
    ResumeResult,
    StagedResult,
    Station,
    UneffectiveResult,
    UnauthorizedStep,
    WarehouseVersion,
    WriteIntent,
    WriteOutcome,
)
from .warehouse import Warehouse


class BackfillCoordinator:
    def __init__(
        self,
        archive: ArchiveStore,
        approvals: ApprovalStore,
        warehouse: Warehouse,
        clock: Optional[Callable[[], datetime]] = None,
    ) -> None:
        self.archive = archive
        self.approvals = approvals
        self.warehouse = warehouse
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._preview_ids = count(1)
        self._archive_high_water: Dict[str, WarehouseVersion] = {}

    # -- previewing -------------------------------------------------------

    def preview(
        self,
        request: PreviewRequest,
        parent_preview_id: Optional[str] = None,
    ) -> Preview:
        """Compute impact without writing to the warehouse or approvals."""

        if not request.writes:
            raise PreviewStateError("a preview must contain at least one write")
        seen_ids = set()
        intents: List[WriteIntent] = []
        station_writes: Dict[Station, List[OutputKind]] = {
            Station.NORTH: [],
            Station.SOUTH: [],
        }
        affected_tables: List[str] = list(dict.fromkeys(request.affected_tables))
        for write in request.writes:
            if write.write_id:
                if write.write_id in seen_ids:
                    raise PreviewStateError(f"duplicate write_id {write.write_id}")
                seen_ids.add(write.write_id)
            station = write.target.kind.station
            if station is not None and station not in request.stations:
                raise CrossStationImpactError(
                    f"{write.target.kind.value} belongs to {station.value}, "
                    f"but the preview only covers {[s.value for s in request.stations]}"
                )
            authorization = request.authorization_for(write.target.kind)
            intents.append(WriteIntent.from_planned(write, authorization))
            if station is not None and write.target.kind not in station_writes[station]:
                station_writes[station].append(write.target.kind)
            if write.target.table and write.target.table not in affected_tables:
                affected_tables.append(write.target.table)
        shared = [
            preview.preview_id
            for preview in self.archive.previews()
            if preview.impact.affected_tables
            and set(preview.impact.affected_tables) & set(affected_tables)
            and preview.status not in {PreviewStatus.WITHDRAWN, PreviewStatus.SUPERSEDED}
        ]
        impact = Impact(
            writes=tuple(intents),
            affected_tables=tuple(affected_tables),
            shared_with=tuple(shared),
            station_writes={
                station: tuple(kinds)
                for station, kinds in station_writes.items()
                if kinds
            },
        )
        preview_id = f"preview-{next(self._preview_ids):04d}"
        attempt = 1
        if parent_preview_id is not None:
            parent = self.archive.get_preview(parent_preview_id)
            attempt = parent.attempt + 1
        preview = Preview(
            preview_id=preview_id,
            request=request,
            impact=impact,
            created_at=self._clock(),
            parent_preview_id=parent_preview_id,
            attempt=attempt,
        )
        self.archive.save_preview(preview)
        return preview

    def re_preview(
        self,
        parent_preview_id: str,
        writes: Optional[Sequence[PlannedWrite]] = None,
        request_id: Optional[str] = None,
        authorizations: Optional[Mapping[OutputKind, "OutputAuthorization"]] = None,
    ) -> Preview:
        """Build a successor preview.

        By default the successor starts with no per-output declarations at all;
        the operator must re-state what may be created or rewritten.  Reusing
        the parent's declarations silently would carry an old intent across new
        evidence.  Pass ``authorizations`` to state them explicitly.
        """

        parent = self.archive.get_preview(parent_preview_id)
        request = PreviewRequest(
            request_id=request_id or f"{parent.request.request_id}-retry",
            stations=tuple(parent.request.stations),
            writes=tuple(writes) if writes is not None else parent.request.writes,
            authorizations=dict(authorizations or {}),
            archive_id=parent.request.archive_id,
            description=parent.request.description,
            affected_tables=tuple(parent.request.affected_tables),
        )
        return self.preview(request, parent_preview_id=parent_preview_id)

    def rehydrate(
        self,
        preview_id: str,
        authorizations: Optional[Mapping[OutputKind, "OutputAuthorization"]] = None,
    ) -> Preview:
        """Rebuild a preview from the warehouse's real versions.

        Written base versions are refreshed from the warehouse; input versions
        are refreshed from whichever effective versions the warehouse holds.
        The new preview is a draft requiring its own confirmation and, by
        default, its own per-output declarations.
        """

        original = self.archive.get_preview(preview_id)
        actual_writes: List[PlannedWrite] = []
        for write in original.request.writes:
            base = write.base_version
            if write.target.kind is not OutputKind.MONTH_TABLE:
                current = self.warehouse.find_current(write.target.key())
                base = None if current is None else current.version
            inputs = dict(write.input_versions)
            if write.target.kind is OutputKind.MONTH_TABLE:
                for dependency in list(inputs):
                    kind_value, _, name = dependency.partition(":")
                    try:
                        kind = OutputKind(kind_value)
                    except ValueError:
                        continue
                    dependency_target = OutputTarget(kind, name)
                    current = self.warehouse.find_current(dependency)
                    if current is None:
                        current = self.warehouse.find_current(dependency_target.key())
                    if current is not None:
                        inputs[dependency] = current.version
            actual_writes.append(
                replace(write, base_version=base, input_versions=inputs)
            )
        request = PreviewRequest(
            request_id=f"{original.request.request_id}-rehydrated",
            stations=tuple(original.request.stations),
            writes=tuple(actual_writes),
            authorizations=dict(authorizations or {}),
            archive_id=original.request.archive_id,
            description=original.request.description,
            affected_tables=tuple(original.request.affected_tables),
        )
        return self.preview(request, parent_preview_id=preview_id)

    # -- approval ---------------------------------------------------------

    def grant(
        self,
        preview_id: str,
        station: Station,
        output: OutputKind,
        can_create_revision: bool,
        can_rewrite_version: bool,
        granted_by: str,
        rewrite_requires_unsigned: bool = False,
    ) -> None:
        from .model import OutputAuthorization

        preview = self.archive.get_preview(preview_id)
        if not any(w.target.kind == output for w in preview.writes):
            raise ApprovalError(f"preview {preview_id} has no {output.value} write")
        self.approvals.grant(
            preview_id,
            station,
            OutputAuthorization(
                output,
                can_create_revision,
                can_rewrite_version,
                rewrite_requires_unsigned,
            ),
            granted_by,
            now=self._clock(),
        )

    def withdraw_grant(
        self,
        preview_id: str,
        station: Station,
        output: OutputKind,
        withdrawn_by: str,
    ) -> None:
        self.approvals.withdraw(preview_id, station, output, withdrawn_by, now=self._clock())

    def confirm(
        self,
        preview_id: str,
        confirmed_by: str,
        scope: Optional[Iterable[OutputKind]] = None,
    ) -> Preview:
        """Confirm a preview, refusing writes outside the approved scope."""

        preview = self.archive.get_preview(preview_id)
        if preview.status is not PreviewStatus.DRAFT:
            raise PreviewStateError(f"preview {preview_id} is {preview.status.value}, not draft")
        scope_kinds = tuple(dict.fromkeys(scope)) if scope is not None else tuple(
            dict.fromkeys(w.target.kind for w in preview.writes)
        )
        for kind in scope_kinds:
            if not any(w.target.kind == kind for w in preview.writes):
                raise ApprovalError(f"cannot confirm {kind.value}: not part of preview {preview_id}")
        self.approvals.approve(preview, confirmed_by, scope=scope_kinds, now=self._clock())
        confirmed = preview.with_status(
            PreviewStatus.CONFIRMED,
            confirmed_at=self._clock(),
            confirmed_by=confirmed_by,
        )
        self.archive.save_preview(
            confirmed, expected_revision=self.archive.revision_of(preview_id)
        )
        return confirmed

    # -- staging and submission ------------------------------------------

    def stage(
        self,
        preview_id: str,
        write_id: str,
        values: Mapping[str, object],
        produced_by: str,
    ) -> StagedResult:
        """Record a branch's computed result without submitting it."""

        preview = self.archive.get_preview(preview_id)
        if preview.status is not PreviewStatus.CONFIRMED:
            raise PreviewStateError(
                f"preview {preview_id} is {preview.status.value}; staging requires confirmation"
            )
        intent = next((w for w in preview.writes if w.write_id == write_id), None)
        if intent is None:
            raise PreviewNotFoundError(f"preview {preview_id} has no write {write_id}")
        if not intent.authorized:
            raise UnauthorizedOperationError(
                intent.blocked_reason or f"{write_id} is not authorized"
            )
        if not self._grant_still_valid(preview, intent):
            raise UnauthorizedOperationError(
                f"grant for {intent.target.kind.value} on {preview_id} was withdrawn"
            )
        if not self._latest_scoped_acceptance(preview)(intent.target.kind):
            raise UnauthorizedOperationError(
                f"{intent.target.kind.value} is outside the confirmed scope on {preview_id}"
            )
        result = StagedResult(
            preview_id=preview_id,
            output=intent.target.kind,
            target=intent.target,
            values=dict(values),
            write_id=write_id,
            operation=intent.operation,
            base_version=intent.base_version,
            input_versions=dict(intent.input_versions),
            rewrite_requires_unsigned=intent.rewrite_requires_unsigned,
            produced_at=self._clock(),
        )
        self.archive.save_staged(result)
        self._record_stage_task(preview_id, produced_by, result)
        return result

    def _record_stage_task(self, preview_id: str, produced_by: str, result: StagedResult) -> None:
        station = result.target.kind.station or Station.NORTH
        self.archive.record_task_state(
            station.value,
            result.write_id,
            {
                "preview_id": preview_id,
                "write_id": result.write_id,
                "target": result.target.key(),
                "status": "staged",
                "recorded_at": self._clock().isoformat(),
            },
        )
        self.archive.record_task_state(
            station.value,
            f"{preview_id}:{result.write_id}",
            {
                "preview_id": preview_id,
                "write_id": result.write_id,
                "target": result.target.key(),
                "status": "staged",
                "recorded_at": self._clock().isoformat(),
            },
        )

    def _grant_still_valid(self, preview: Preview, intent: WriteIntent) -> bool:
        station = intent.target.kind.station
        if station is None:
            holders = [
                self.approvals.grant_for(preview.preview_id, candidate, intent.target.kind)
                for candidate in preview.stations
            ]
            return any(
                holder and holder.permits(intent.operation) for holder in holders
            )
        return self.approvals.permits(
            preview.preview_id, station, intent.target.kind, intent.operation
        )

    def submit(
        self,
        preview_id: str,
        submitted_by: str = "coordinator",
        writes: Optional[Sequence[str]] = None,
        deliver: bool = True,
    ) -> Tuple[Preview, CommitReceipt]:
        """Submit staged results against the warehouse's current versions.

        When ``deliver`` is false the warehouse only queues the submission; the
        preview remains committing until a later call observes acceptance.
        """

        preview = self.archive.get_preview(preview_id)
        if preview.status not in {PreviewStatus.CONFIRMED, PreviewStatus.COMMITTING}:
            raise PreviewStateError(
                f"preview {preview_id} is {preview.status.value}; cannot submit"
            )
        staged = self.archive.staged(preview_id)
        if writes is not None:
            wanted = set(writes)
            staged = [item for item in staged if item.write_id in wanted]
        if not staged:
            raise PreviewStateError(f"preview {preview_id} has no staged results to submit")
        staged = sorted(staged, key=lambda item: _output_order(item.target.kind))
        self._check_submission(preview, staged)
        committing = preview.with_status(PreviewStatus.COMMITTING)
        self.archive.save_preview(
            committing, expected_revision=self.archive.revision_of(preview_id)
        )
        receipt = self.warehouse.commit(
            preview_id, staged, submitter=submitted_by, deliver=deliver
        )
        if deliver:
            updated = self._apply_receipt(committing, receipt)
            return updated, receipt
        self.archive.save_receipt(receipt)
        return committing, receipt

    def _check_submission(self, preview: Preview, staged: Sequence[StagedResult]) -> None:
        accepts = self._latest_scoped_acceptance(preview)
        for item in staged:
            intent = next((w for w in preview.writes if w.write_id == item.write_id), None)
            if intent is None:
                raise UnauthorizedOperationError(
                    f"{item.write_id} is not part of preview {preview.preview_id}"
                )
            if not intent.authorized:
                raise UnauthorizedOperationError(
                    intent.blocked_reason or f"{item.write_id} is not authorized"
                )
            if item.target.kind.station is not None and not self._grant_still_valid(preview, intent):
                raise UnauthorizedOperationError(
                    f"grant for {item.target.kind.value} on {preview.preview_id} was withdrawn"
                )
            if not accepts(item.target.kind):
                raise UnauthorizedOperationError(
                    f"{item.target.kind.value} is outside the confirmed scope on "
                    f"{preview.preview_id}"
                )
            current = self.warehouse.find_current(item.target.key())
            if current is not None and item.base_version is not None and item.base_version != current.version:
                raise StaleVersionError(
                    f"{item.target.label()} staged against {item.base_version}, "
                    f"warehouse now holds {current.version}; re-preview before submitting"
                )
            for dependency_key, expected in item.input_versions.items():
                dependency = self.warehouse.find_current(dependency_key)
                actual = None if dependency is None else dependency.version
                if actual != expected:
                    raise StaleVersionError(
                        f"{dependency_key} was {expected} when "
                        f"{item.target.label()} was staged, but the warehouse now "
                        f"holds {actual}; re-derive the result before submitting"
                    )
            if (
                item.operation is GrantOperation.REWRITE_VERSION
                and item.rewrite_requires_unsigned
                and self.warehouse.signature(item.target) is not None
                and item.base_version is not None
                and self.warehouse.is_signed(item.target, item.base_version)
            ):
                raise UnauthorizedOperationError(
                    f"{item.target.label()} was signed after staging; conditional rewrite "
                    "no longer applies"
                )

    def _latest_scoped_acceptance(self, preview: Preview) -> Callable[[OutputKind], bool]:
        decision = self.approvals.latest_decision(preview.preview_id)
        if decision is None or not decision.approved:
            return lambda kind: False
        return decision.permits

    def _apply_receipt(self, preview: Preview, receipt: CommitReceipt) -> Preview:
        self.archive.save_receipt(receipt)
        for outcome in receipt.accepted_writes:
            version = self._version_for_outcome(outcome)
            if version is not None:
                self.archive.remember_accepted(version)
                self.archive.record_task_state(
                    "warehouse",
                    f"{outcome.target.key()}@{version.version}",
                    {
                        "preview_id": preview.preview_id,
                        "target": outcome.target.key(),
                        "version": version.version,
                        "status": "accepted",
                        "accepted_at": version.accepted_at.isoformat(),
                    },
                )
        if receipt.accepted:
            status = PreviewStatus.ACCEPTED
            accepted_versions = [
                version
                for outcome in receipt.accepted_writes
                for version in [self._version_for_outcome(outcome)]
                if version is not None
            ]
            self.archive.supersede_open_previews(
                except_preview_id=preview.preview_id,
                versions=accepted_versions,
            )
            self._archive_high_water[preview.preview_id] = self._latest_accepted(receipt)
        elif receipt.accepted_writes:
            status = PreviewStatus.PARTIALLY_ACCEPTED
        else:
            status = PreviewStatus.REJECTED
        updated = preview.with_status(
            status,
            rejection_reason="; ".join(
                f"{o.target.label()}: {o.reason}" for o in receipt.rejected_writes
            )
            or None,
        )
        self.archive.save_preview(
            updated, expected_revision=self.archive.revision_of(preview.preview_id)
        )
        return updated

    def _latest_accepted(self, receipt: CommitReceipt) -> WarehouseVersion:
        versions = [
            version
            for outcome in receipt.accepted_writes
            for version in self.warehouse.versions(outcome.target)
            if version.version == outcome.version and version.write_id == outcome.write_id
        ]
        return max(versions, key=lambda version: (version.version, version.accepted_at))

    def _version_for_outcome(self, outcome: WriteOutcome) -> Optional[WarehouseVersion]:
        if outcome.version is None:
            return None
        for version in reversed(self.warehouse.versions(outcome.target)):
            if version.version == outcome.version and version.write_id == outcome.write_id:
                return version
        return None

    # -- observing delayed external answers -------------------------------

    def observe_delivery(self, preview_id: str) -> Preview:
        """Pull a warehouse answer into the archive after the fact.

        Repeated observation is idempotent: once the preview is terminal and
        the receipt has already been applied, the archived preview is returned
        unchanged.
        """

        preview = self.archive.get_preview(preview_id)
        if preview.status in {
            PreviewStatus.ACCEPTED,
            PreviewStatus.PARTIALLY_ACCEPTED,
            PreviewStatus.REJECTED,
            PreviewStatus.WITHDRAWN,
        } and self.archive.receipt(preview_id) is not None:
            return preview
        receipt = self.warehouse.delivery(preview_id)
        if receipt is None:
            return preview
        return self._apply_receipt(preview, receipt)

    # -- withdrawal / supersession ---------------------------------------

    def withdraw(
        self,
        preview_id: str,
        withdrawn_by: str,
        reason: str = "",
        discard_staged: bool = False,
    ) -> Preview:
        preview = self.archive.get_preview(preview_id)
        if preview.status in {PreviewStatus.ACCEPTED, PreviewStatus.PARTIALLY_ACCEPTED}:
            raise PreviewStateError(f"preview {preview_id} already reached the warehouse")
        withdrawn = preview.with_status(
            PreviewStatus.WITHDRAWN, rejection_reason=reason or None
        )
        self.archive.save_preview(
            withdrawn, expected_revision=self.archive.revision_of(preview_id)
        )
        if discard_staged:
            self.archive.discard_staged(preview_id)
        return withdrawn

    # -- reconciliation ---------------------------------------------------

    def recover(self) -> RecoveryReport:
        """Compare local knowledge with the warehouse's actual state.

        The warehouse is authoritative.  A local task state or archive entry
        whose version does not match the warehouse is reported as uneffective;
        it is never silently promoted, and nothing is resubmitted.
        """

        accepted = dict(self.warehouse.all_current())
        local_versions = self.archive.local_versions()
        observed = self.archive.cached_accepted()
        pending_results: List[StagedResult] = []
        superseded: List[Preview] = []
        unauthorized: List[UnauthorizedStep] = []
        uneffective: List[UneffectiveResult] = []
        mismatches: List[DependencyMismatch] = []
        notes: List[str] = []

        for preview in self.archive.previews():
            for intent in preview.writes:
                station = intent.target.kind.station
                if station is None:
                    if not self._grant_still_valid(preview, intent):
                        unauthorized.append(
                            UnauthorizedStep(
                                preview_id=preview.preview_id,
                                intent=intent,
                                station=None,
                                reason=f"no active grant for {intent.target.kind.value}",
                            )
                        )
                    continue
                if not self.approvals.permits(
                    preview.preview_id, station, intent.target.kind, intent.operation
                ):
                    unauthorized.append(
                        UnauthorizedStep(
                            preview_id=preview.preview_id,
                            intent=intent,
                            station=station,
                            reason=self._unauthorized_reason(preview, intent),
                        )
                    )
            if preview.status is PreviewStatus.SUPERSEDED:
                superseded.append(preview)
            if not preview.is_open() and preview.status is not PreviewStatus.SUPERSEDED:
                continue
            for staged in self.archive.staged(preview.preview_id):
                effective = accepted.get(staged.target.key())
                if effective is None or effective.write_id != staged.write_id:
                    pending_results.append(staged)
                    uneffective.append(
                        UneffectiveResult(
                            staged=staged,
                            reason=self._uneffective_reason(staged, effective),
                        )
                    )
                    continue
                seen = observed.get(staged.target.key())
                if seen is None or seen.version < effective.version:
                    # The warehouse accepted it, but this archive never
                    # observed that acceptance; the operator's local view is
                    # still the older version.
                    pending_results.append(staged)
                    uneffective.append(
                        UneffectiveResult(
                            staged=staged,
                            reason=(
                                f"warehouse accepted version {effective.version}, "
                                "but the archive has not observed it"
                            ),
                        )
                    )

        for key, version in accepted.items():
            if version.preview_id is None:
                continue
            for dependency_key, expected in version.input_versions.items():
                dependency = _resolve_dependency(accepted, dependency_key)
                actual = None if dependency is None else dependency.version
                if actual != expected:
                    mismatches.append(
                        DependencyMismatch(
                            key=key,
                            effective_version=version.version,
                            expected=dict(version.input_versions),
                            actual={dependency_key: actual},
                        )
                    )
                    notes.append(
                        f"effective {key}@{version.version} was derived from "
                        f"{dependency_key}@{expected}, but the warehouse now holds "
                        f"{dependency_key}@{actual}"
                    )

        archive_lag = False
        tracked = (
            set(local_versions)
            | set(observed)
            | {
                write.target.key()
                for preview in self.archive.previews()
                for write in preview.writes
            }
        )
        for key, version in accepted.items():
            if key not in tracked:
                continue
            local = local_versions.get(key)
            if local is None or local < version.version:
                archive_lag = True
                notes.append(
                    f"warehouse holds {key}@{version.version} but archive knows "
                    f"{local}; local success cannot be treated as external acceptance"
                )
            elif local > version.version:
                notes.append(
                    f"archive records {key}@{local}, but the warehouse still holds "
                    f"{version.version}; the local save is not an external acceptance"
                )
        for message in self.archive.contradictions():
            notes.append(message)

        return RecoveryReport(
            generated_at=self._clock(),
            archive_lag=archive_lag,
            accepted_versions=accepted,
            local_versions=local_versions,
            pending_results=tuple(pending_results),
            superseded_previews=tuple(superseded),
            unauthorized_steps=tuple(unauthorized),
            uneffective_results=tuple(uneffective),
            dependency_mismatches=tuple(mismatches),
            pending_deliveries=tuple(self.warehouse.pending_ids()),
            notes=tuple(notes),
        )

    def _unauthorized_reason(self, preview: Preview, intent: WriteIntent) -> str:
        if not intent.authorized:
            return intent.blocked_reason or f"{intent.operation.value} was never declared"
        return f"grant for {intent.target.kind.value} is withdrawn or conditional"

    def _uneffective_reason(
        self,
        staged: StagedResult,
        effective: Optional[WarehouseVersion],
    ) -> str:
        if effective is None:
            return "warehouse holds no version of this output"
        if effective.write_id != staged.write_id:
            return (
                f"warehouse holds {effective.write_id or 'an external write'}"
                f"@{effective.version}, not staged {staged.write_id}"
            )
        return "not the effective version"

    # -- resuming an old preview -----------------------------------------

    def resume(
        self,
        preview_id: str,
        submitted_by: str = "coordinator",
        sandbox_archive: Optional[ArchiveStore] = None,
        allow_submit: bool = False,
    ) -> ResumeResult:
        """Re-present a preview and optionally resume only safe writes.

        By default this never submits: it returns what the warehouse really
        holds, which staged results are still uneffective, and which steps were
        never permitted, so an operator can decide whether to re-preview.  Even
        when ``allow_submit`` is true, the resume is all-or-nothing: if *any*
        staged result conflicts with newer accepted work, lost authorization,
        or leaves the confirmed scope, nothing is submitted and a fresh preview
        is required.
        """

        preview = self.archive.get_preview(preview_id)
        staged = self.archive.staged(preview_id)
        accepts = self._latest_scoped_acceptance(preview)
        submitted: List[WriteOutcome] = []
        uneffective: List[UneffectiveResult] = []
        unauthorized: List[UnauthorizedStep] = []
        notes: List[str] = []
        observed = self.archive.cached_accepted()
        requires_repreview = preview.status is PreviewStatus.SUPERSEDED

        stale_or_unauthorized = False
        eligible: List[StagedResult] = []
        for item in sorted(staged, key=lambda entry: _output_order(entry.target.kind)):
            accepted = self.warehouse.find_current(item.target.key())
            intent = next((w for w in preview.writes if w.write_id == item.write_id), None)

            if intent is None or not intent.authorized:
                reason = "not part of preview" if intent is None else (intent.blocked_reason or "not authorized")
                unauthorized.append(
                    UnauthorizedStep(
                        preview_id,
                        intent or WriteIntent(
                            item.target,
                            item.operation,
                            item.base_version,
                            item.input_versions,
                            item.write_id,
                            False,
                            blocked_reason=reason,
                        ),
                        item.target.kind.station,
                        reason,
                    )
                )
                uneffective.append(UneffectiveResult(item, reason))
                self.archive.note_contradiction(f"{preview_id}/{item.write_id}: {reason}")
                stale_or_unauthorized = True
                continue

            if not self._grant_still_valid(preview, intent):
                reason = f"grant for {item.target.kind.value} was withdrawn"
                unauthorized.append(
                    UnauthorizedStep(preview_id, intent, item.target.kind.station, reason)
                )
                uneffective.append(UneffectiveResult(item, reason))
                self.archive.note_contradiction(f"{preview_id}/{item.write_id}: {reason}")
                stale_or_unauthorized = True
                continue

            if not accepts(item.target.kind):
                reason = f"{item.target.kind.value} is outside the confirmed scope"
                unauthorized.append(
                    UnauthorizedStep(preview_id, intent, item.target.kind.station, reason)
                )
                uneffective.append(UneffectiveResult(item, reason))
                stale_or_unauthorized = True
                continue

            seen = observed.get(item.target.key())
            if accepted is not None and accepted.write_id == item.write_id:
                if seen is None or seen.version < accepted.version:
                    uneffective.append(
                        UneffectiveResult(
                            item,
                            f"warehouse accepted {item.target.label()}@"
                            f"{accepted.version}, but the archive has not observed it",
                        )
                    )
                else:
                    notes.append(
                        f"{item.target.label()} is already effective at "
                        f"version {accepted.version}; not resent"
                    )
                continue

            if (
                item.operation is GrantOperation.REWRITE_VERSION
                and item.rewrite_requires_unsigned
                and accepted is not None
                and self.warehouse.is_signed(item.target, accepted.version)
            ):
                reason = (
                    f"{item.target.label()} was signed after staging; "
                    "conditional rewrite no longer applies"
                )
                notes.append(reason)
                uneffective.append(UneffectiveResult(item, reason))
                self.archive.note_contradiction(f"{preview_id}/{item.write_id}: {reason}")
                requires_repreview = True
                stale_or_unauthorized = True
                continue

            if accepted is not None and item.base_version is not None and accepted.version > item.base_version:
                reason = (
                    f"warehouse holds {item.target.label()}@"
                    f"{accepted.version} from {accepted.write_id}; "
                    f"old preview was based on {item.base_version}"
                )
                notes.append(reason)
                uneffective.append(UneffectiveResult(item, reason))
                self.archive.note_contradiction(f"{preview_id}/{item.write_id}: {reason}")
                requires_repreview = True
                stale_or_unauthorized = True
                continue

            stale_inputs = []
            for dependency_key, expected in item.input_versions.items():
                dependency = self.warehouse.find_current(dependency_key)
                actual = None if dependency is None else dependency.version
                if actual != expected:
                    stale_inputs.append(f"{dependency_key} expected {expected}, found {actual}")
            if stale_inputs:
                reason = (
                    f"{item.target.label()} was derived from stale inputs: "
                    + ", ".join(stale_inputs)
                )
                notes.append(reason)
                uneffective.append(UneffectiveResult(item, reason))
                self.archive.note_contradiction(f"{preview_id}/{item.write_id}: {reason}")
                requires_repreview = True
                stale_or_unauthorized = True
                continue

            eligible.append(item)

        if not allow_submit or stale_or_unauthorized:
            if eligible:
                notes.append(
                    "eligible writes were not submitted automatically; re-derive them "
                    "in a fresh preview: " + ", ".join(item.write_id for item in eligible)
                )
            if stale_or_unauthorized or requires_repreview:
                requires_repreview = True
            return ResumeResult(
                preview_id=preview_id,
                resumed_at=self._clock(),
                accepted_versions=dict(self.warehouse.all_current()),
                submitted=(),
                uneffective_results=tuple(uneffective),
                unauthorized_steps=tuple(unauthorized),
                requires_repreview=requires_repreview,
                notes=tuple(notes),
            )

        if eligible:
            if preview.status is not PreviewStatus.COMMITTING:
                preview = preview.with_status(PreviewStatus.COMMITTING)
                self.archive.save_preview(
                    preview, expected_revision=self.archive.revision_of(preview_id)
                )
            receipt = self.warehouse.commit(preview_id, eligible, submitter=submitted_by)
            self.archive.save_receipt(receipt)
            submitted = list(receipt.outcomes)
            preview = self._apply_receipt(preview, receipt)

        return ResumeResult(
            preview_id=preview_id,
            resumed_at=self._clock(),
            accepted_versions=dict(self.warehouse.all_current()),
            submitted=tuple(submitted),
            uneffective_results=tuple(uneffective),
            unauthorized_steps=tuple(unauthorized),
            requires_repreview=requires_repreview,
            notes=tuple(notes),
        )

    # -- cross-checks used by tests and operators ------------------------

    def preview_impacts(self, preview_ids: Iterable[str]) -> Mapping[str, Tuple[str, ...]]:
        return {
            preview_id: self.archive.get_preview(preview_id).impact.affected_tables
            for preview_id in preview_ids
        }

    def effective_versions(self) -> Mapping[str, WarehouseVersion]:
        return dict(self.warehouse.all_current())


def _output_order(kind: OutputKind) -> int:
    ordering = {
        OutputKind.NORTH_SAMPLE: 0,
        OutputKind.NORTH_TRANSFORM: 1,
        OutputKind.SOUTH_SAMPLE: 2,
        OutputKind.SOUTH_TRANSFORM: 3,
        OutputKind.NORTH_CREDENTIAL: 4,
        OutputKind.SOUTH_CREDENTIAL: 5,
        OutputKind.SOUTH_EXPORT: 6,
        OutputKind.NORTH_EXPORT: 7,
        OutputKind.MONTH_TABLE: 8,
    }
    return ordering.get(kind, 99)


def _resolve_dependency(
    accepted: Mapping[str, WarehouseVersion], dependency_key: str
) -> Optional[WarehouseVersion]:
    """Resolve a dependency key that may or may not include the table."""

    found = accepted.get(dependency_key)
    if found is not None:
        return found
    matches = [
        version
        for key, version in accepted.items()
        if key.rsplit(":", 1)[0] == dependency_key
    ]
    return matches[0] if len(matches) == 1 else None


def station_output_kinds(station: Station) -> Tuple[OutputKind, ...]:
    from .model import station_kinds

    return station_kinds(station)
