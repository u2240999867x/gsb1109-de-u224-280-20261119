"""End-to-end scenarios for the preview-first backfill coordinator."""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from SpiffWorkflow.backfill import (
    ApprovalError,
    ApprovalStore,
    ArchiveStore,
    BackfillCoordinator,
    CrossStationImpactError,
    GrantNotPermittedError,
    GrantOperation,
    OutputAuthorization,
    OutputKind,
    OutputTarget,
    PlannedWrite,
    PreviewRequest,
    PreviewStateError,
    PreviewStatus,
    Station,
    StaleVersionError,
    UnauthorizedOperationError,
    Warehouse,
)

NOW = datetime(2026, 11, 23, 9, 0, tzinfo=timezone.utc)


def at(minutes: int) -> datetime:
    return NOW + timedelta(minutes=minutes)


MONTH = "month_table"
TABLE = "2026-11"


def target(kind: OutputKind, name: str, table: str = "2026-11") -> OutputTarget:
    return OutputTarget(kind, name, table)


def write(
    kind: OutputKind,
    name: str,
    operation: GrantOperation = GrantOperation.CREATE_REVISION,
    base_version: int = 0,
    write_id: str = "",
    table: str = "2026-11",
    input_versions=None,
) -> PlannedWrite:
    return PlannedWrite(
        target=target(kind, name, table),
        operation=operation,
        base_version=base_version,
        write_id=write_id or f"{kind.value}-{name}",
        input_versions=dict(input_versions or {}),
    )


class BackfillScenarioTest(unittest.TestCase):
    def setUp(self) -> None:
        self.archive = ArchiveStore()
        self.approvals = ApprovalStore()
        self.warehouse = Warehouse()
        self.coordinator = BackfillCoordinator(
            self.archive, self.approvals, self.warehouse, clock=lambda: NOW
        )
        self._seed()

    def _seed(self) -> None:
        # Current versions the previews will target.
        self.warehouse.ensure_output(target(OutputKind.NORTH_TRANSFORM, "north_transform"), version=4)
        self.warehouse.ensure_output(target(OutputKind.SOUTH_SAMPLE, "south_sample"), version=3)
        self.warehouse.ensure_output(target(OutputKind.SOUTH_CREDENTIAL, "south_credential"), version=2)
        self.warehouse.ensure_output(target(OutputKind.SOUTH_TRANSFORM, "south_transform"), version=4)
        self.warehouse.ensure_output(target(OutputKind.MONTH_TABLE, MONTH), version=7)
        self.warehouse.ensure_output(target(OutputKind.NORTH_EXPORT, "north_export"), version=1)
        self.warehouse.ensure_output(target(OutputKind.SOUTH_EXPORT, "south_export"), version=1)

    def _north_preview(self, include_month_table: bool = False) -> object:
        writes = [
            write(
                OutputKind.NORTH_TRANSFORM,
                "north_transform",
                base_version=4,
                write_id="north-redo-transform",
            )
        ]
        authorizations = {
            OutputKind.NORTH_TRANSFORM: OutputAuthorization(
                OutputKind.NORTH_TRANSFORM, can_create_revision=True
            ),
        }
        if include_month_table:
            writes.append(
                write(
                    OutputKind.MONTH_TABLE,
                    MONTH,
                    operation=GrantOperation.REWRITE_VERSION,
                    base_version=7,
                    write_id="month-rewrite",
                    input_versions={"north_transform:north_transform": 4},
                )
            )
            authorizations[OutputKind.MONTH_TABLE] = OutputAuthorization(
                OutputKind.MONTH_TABLE,
                can_create_revision=False,
                can_rewrite_version=True,
                rewrite_requires_unsigned=True,
            )
        request = PreviewRequest(
            request_id="backfill-north",
            stations=(Station.NORTH,),
            writes=tuple(writes),
            authorizations=authorizations,
            archive_id="archive-235",
            affected_tables=(TABLE,),
        )
        return self.coordinator.preview(request)

    def _south_preview(self) -> object:
        request = PreviewRequest(
            request_id="backfill-south",
            stations=(Station.SOUTH,),
            writes=(
                write(
                    OutputKind.SOUTH_SAMPLE,
                    "south_sample",
                    base_version=3,
                    write_id="south-resample",
                ),
                write(
                    OutputKind.SOUTH_CREDENTIAL,
                    "south_credential",
                    base_version=2,
                    write_id="south-credential-refresh",
                ),
            ),
            authorizations={
                OutputKind.SOUTH_SAMPLE: OutputAuthorization(
                    OutputKind.SOUTH_SAMPLE, can_create_revision=True
                ),
                OutputKind.SOUTH_CREDENTIAL: OutputAuthorization(
                    OutputKind.SOUTH_CREDENTIAL, can_create_revision=True
                ),
            },
            archive_id="archive-235",
            affected_tables=(TABLE,),
        )
        return self.coordinator.preview(request)

    def _grant_scope(self, preview, station: Station, kinds) -> None:
        for kind in kinds:
            intent = next((w for w in preview.writes if w.target.kind == kind), None)
            self.assertIsNotNone(intent, f"preview has no {kind.value} write")
            authorization = OutputAuthorization(
                kind,
                can_create_revision=intent.operation is GrantOperation.CREATE_REVISION,
                can_rewrite_version=intent.operation is GrantOperation.REWRITE_VERSION,
                rewrite_requires_unsigned=intent.rewrite_requires_unsigned,
            )
            self.approvals.grant(
                preview.preview_id,
                station,
                authorization,
                f"{station.value}-approver",
                now=NOW,
            )

    def test_previews_share_month_table_but_keep_station_outputs_separate(self) -> None:
        north = self._north_preview()
        south = self._south_preview()
        self.assertIn(TABLE, north.impact.affected_tables)
        self.assertIn(TABLE, south.impact.affected_tables)
        north_kinds = {w.target.kind for w in north.writes}
        south_kinds = {w.target.kind for w in south.writes}
        self.assertNotIn(OutputKind.SOUTH_CREDENTIAL, north_kinds)
        self.assertNotIn(OutputKind.NORTH_TRANSFORM, south_kinds)
        self.assertEqual([], list(north.impact.writes_for(Station.SOUTH)))
        self.assertIn(north.preview_id, south.impact.shared_with)

    def test_station_cannot_preview_other_station_output(self) -> None:
        request = PreviewRequest(
            request_id="bad",
            stations=(Station.NORTH,),
            writes=(
                write(OutputKind.SOUTH_CREDENTIAL, "south_credential"),
            ),
            authorizations={},
        )
        with self.assertRaises(CrossStationImpactError):
            self.coordinator.preview(request)

    def test_confirm_scope_does_not_imply_month_table_authority(self) -> None:
        preview = self._north_preview()
        self._grant_scope(preview, Station.NORTH, [OutputKind.NORTH_TRANSFORM])
        # Confirming a kind that is not part of the preview's writes must fail
        # closed rather than silently widening the scope.
        with self.assertRaises(ApprovalError):
            self.coordinator.confirm(
                preview.preview_id, "reader", scope=[OutputKind.MONTH_TABLE]
            )
        self.assertEqual(PreviewStatus.DRAFT, self.archive.get_preview(preview.preview_id).status)

    def test_withdrawn_rewrite_after_warehouse_signature_blocks_logical_rewrite(self) -> None:
        preview = self._north_preview(include_month_table=True)
        self._grant_scope(preview, Station.NORTH, [OutputKind.NORTH_TRANSFORM])
        self._grant_scope(preview, Station.NORTH, [OutputKind.MONTH_TABLE])
        self.coordinator.confirm(
            preview.preview_id,
            "north-lead",
            scope=[OutputKind.NORTH_TRANSFORM, OutputKind.MONTH_TABLE],
        )
        staged_transform = self.coordinator.stage(
            preview.preview_id, "north-redo-transform", {"rows": 10}, "north-worker"
        )
        staged_month = self.coordinator.stage(
            preview.preview_id, "month-rewrite", {"total": 10}, "north-worker"
        )
        # Warehouse signs the old month table while the branch has staged but
        # before the write is submitted, then the reader withdraws the rewrite
        # grant for that version.
        self.warehouse.sign_off(target(OutputKind.MONTH_TABLE, MONTH), "warehouse")
        self.coordinator.withdraw_grant(
            preview.preview_id, Station.NORTH, OutputKind.MONTH_TABLE, "north-lead"
        )
        with self.assertRaises(UnauthorizedOperationError):
            self.coordinator.submit(preview.preview_id, submitted_by="north-worker")
        self.assertEqual(
            4,
            self.warehouse.current(target(OutputKind.NORTH_TRANSFORM, "north_transform")).version,
        )
        self.assertEqual(7, self.warehouse.current(target(OutputKind.MONTH_TABLE, MONTH)).version)
        # Both staged results survive for processing and show up as ineffective.
        report = self.coordinator.recover()
        effective_keys = {
            item.staged.write_id for item in report.uneffective_results
        }
        self.assertIn(staged_transform.write_id, effective_keys)
        self.assertIn(staged_month.write_id, effective_keys)
        self.assertTrue(
            any(
                step.preview_id == preview.preview_id
                and step.intent.target.kind is OutputKind.MONTH_TABLE
                for step in report.unauthorized_steps
            )
        )
        # Original task state remains queryable.
        north_history = self.archive.task_history("north", staged_transform.write_id)
        self.assertTrue(north_history)
        self.assertEqual("staged", north_history[-1]["status"])

    def test_scoped_approval_blocks_unapproved_station_write(self) -> None:
        south = self._south_preview()
        self._grant_scope(south, Station.SOUTH, [OutputKind.SOUTH_SAMPLE])
        self._grant_scope(south, Station.SOUTH, [OutputKind.SOUTH_CREDENTIAL])
        # Both grants exist, but the reader's scope only names the sample.
        self.coordinator.confirm(
            south.preview_id, "reader", scope=[OutputKind.SOUTH_SAMPLE]
        )
        with self.assertRaises(UnauthorizedOperationError):
            self.coordinator.stage(
                south.preview_id,
                "south-credential-refresh",
                {"token": "x"},
                "south-worker",
            )

    def test_withdrawn_grant_stops_conditional_rewrite(self) -> None:
        preview = self._north_preview()
        self._grant_scope(preview, Station.NORTH, [OutputKind.NORTH_TRANSFORM])
        self.coordinator.confirm(preview.preview_id, "north-lead")
        self.coordinator.stage(preview.preview_id, "north-redo-transform", {"rows": 11}, "north-worker")
        self.coordinator.withdraw_grant(
            preview.preview_id, Station.NORTH, OutputKind.NORTH_TRANSFORM, "north-lead"
        )
        with self.assertRaises(UnauthorizedOperationError):
            self.coordinator.submit(preview.preview_id, submitted_by="north-worker")
        self.assertEqual(PreviewStatus.CONFIRMED, self.archive.get_preview(preview.preview_id).status)

    def test_new_revision_preview_can_use_actual_version(self) -> None:
        preview = self._north_preview()
        self._grant_scope(preview, Station.NORTH, [OutputKind.NORTH_TRANSFORM])
        self.coordinator.confirm(preview.preview_id, "north-lead")
        self.coordinator.stage(preview.preview_id, "north-redo-transform", {"rows": 12}, "north-worker")
        updated, receipt = self.coordinator.submit(preview.preview_id, submitted_by="north-worker")
        self.assertTrue(receipt.accepted)
        self.assertEqual(PreviewStatus.ACCEPTED, updated.status)
        current = self.warehouse.current(target(OutputKind.NORTH_TRANSFORM, "north_transform"))
        self.assertEqual(5, current.version)

    def test_local_save_does_not_imply_warehouse_acceptance(self) -> None:
        preview = self._north_preview()
        self._grant_scope(preview, Station.NORTH, [OutputKind.NORTH_TRANSFORM])
        self.coordinator.confirm(preview.preview_id, "north-lead")
        staged = self.coordinator.stage(
            preview.preview_id, "north-redo-transform", {"rows": 13}, "north-worker"
        )
        # The archive persists the staged result at version 5 before the
        # warehouse returns.
        self.archive.record_local_version(staged.target, version=5)
        report = self.coordinator.recover()
        self.assertFalse(report.archive_lag)
        self.assertTrue(
            any(
                "local save is not an external acceptance" in note
                for note in report.notes
            )
        )
        self.assertIn(staged, report.pending_results)
        self.assertNotIn(
            staged.target.key(),
            {key for key, version in report.accepted_versions.items() if version.version == 5},
        )

    def test_queued_submission_is_not_accepted_until_delivered(self) -> None:
        preview = self._north_preview()
        self._grant_scope(preview, Station.NORTH, [OutputKind.NORTH_TRANSFORM])
        self.coordinator.confirm(preview.preview_id, "north-lead")
        self.coordinator.stage(preview.preview_id, "north-redo-transform", {"rows": 14}, "north-worker")
        committing, receipt = self.coordinator.submit(
            preview.preview_id, submitted_by="north-worker", deliver=False
        )
        self.assertTrue(receipt.pending)
        self.assertEqual(PreviewStatus.COMMITTING, committing.status)
        self.assertEqual(4, self.warehouse.current(target(OutputKind.NORTH_TRANSFORM, "north_transform")).version)
        delivered = self.warehouse.deliver(preview.preview_id)
        self.assertTrue(delivered.accepted)
        # Not yet observed by the archive.
        self.assertEqual(PreviewStatus.COMMITTING, self.archive.get_preview(preview.preview_id).status)
        observed = self.coordinator.observe_delivery(preview.preview_id)
        self.assertEqual(PreviewStatus.ACCEPTED, observed.status)

    def test_accepted_north_revision_blocks_replay_of_old_south_preview(self) -> None:
        south = self._south_preview()
        self._grant_scope(south, Station.SOUTH, [OutputKind.SOUTH_SAMPLE])
        self._grant_scope(south, Station.SOUTH, [OutputKind.SOUTH_CREDENTIAL])
        self.coordinator.confirm(south.preview_id, "south-lead")
        self.coordinator.stage(south.preview_id, "south-resample", {"samples": 2}, "south-worker")
        self.coordinator.stage(
            south.preview_id, "south-credential-refresh", {"token": "old"}, "south-worker"
        )
        # Warehouse accepts a newer credential from another writer.
        newer = PlannedWrite(
            target=target(OutputKind.SOUTH_CREDENTIAL, "south_credential"),
            operation=GrantOperation.CREATE_REVISION,
            base_version=2,
            write_id="external-credential",
        )
        from SpiffWorkflow.backfill import StagedResult

        self.warehouse.accept(
            StagedResult(
                preview_id="external",
                output=OutputKind.SOUTH_CREDENTIAL,
                target=newer.target,
                values={"token": "newer"},
                write_id=newer.write_id,
                operation=newer.operation,
                base_version=2,
                input_versions={},
            ),
            accepted_by="another-writer",
        )
        # The old preview's credential evidence must not overwrite it and the
        # old sample evidence must not be auto-resent.
        result = self.coordinator.resume(south.preview_id, submitted_by="south-worker")
        self.assertTrue(result.requires_repreview)
        self.assertIn("south-credential-refresh", {item.staged.write_id for item in result.uneffective_results})
        current = self.warehouse.current(target(OutputKind.SOUTH_CREDENTIAL, "south_credential"))
        self.assertEqual("external-credential", current.write_id)
        self.assertEqual(3, current.version)
        # The sample was not auto-resent either; the old preview is stale.
        self.assertFalse(result.submitted)
        self.assertEqual(3, self.warehouse.current(target(OutputKind.SOUTH_SAMPLE, "south_sample")).version)

    def test_resume_reports_accepted_version_pending_result_and_unauthorized_step(self) -> None:
        preview = self._north_preview()
        self._grant_scope(preview, Station.NORTH, [OutputKind.NORTH_TRANSFORM])
        self.coordinator.confirm(preview.preview_id, "north-lead")
        staged = self.coordinator.stage(
            preview.preview_id, "north-redo-transform", {"rows": 15}, "north-worker"
        )
        # Simulate the crash window: warehouse accepted, archive did not save.
        receipt = self.warehouse.commit(preview.preview_id, [staged], submitter="north-worker")
        self.assertTrue(receipt.accepted)
        self.archive.record_local_version(staged.target, version=4)  # still stale
        self.coordinator.withdraw_grant(
            preview.preview_id, Station.NORTH, OutputKind.NORTH_TRANSFORM, "north-lead"
        )
        report = self.coordinator.recover()
        accepted = report.effective_version(staged.target)
        self.assertIsNotNone(accepted)
        self.assertEqual(5, accepted.version)
        self.assertEqual(4, report.local_versions[staged.target.key()])
        # The warehouse has accepted it, but the archive has not observed
        # that answer; it is still an uneffective local result.
        self.assertIn(staged.write_id, {item.staged.write_id for item in report.uneffective_results})
        self.assertTrue(report.unauthorized_steps)
        self.assertTrue(report.archive_lag)

        # Resuming must not overwrite the accepted revision or resend evidence.
        result = self.coordinator.resume(preview.preview_id, submitted_by="north-worker")
        self.assertTrue(result.requires_repreview)
        self.assertFalse(result.submitted)
        self.assertEqual(5, result.accepted_versions[staged.target.key()].version)

        # A fresh preview can be confirmed and derive from the actual version.
        fresh = self.coordinator.re_preview(
            preview.preview_id,
            writes=(
                write(
                    OutputKind.NORTH_TRANSFORM,
                    "north_transform",
                    base_version=5,
                    write_id="north-redo-transform-2",
                ),
            ),
            authorizations={
                OutputKind.NORTH_TRANSFORM: OutputAuthorization(
                    OutputKind.NORTH_TRANSFORM, can_create_revision=True
                ),
            },
        )
        self._grant_scope(fresh, Station.NORTH, [OutputKind.NORTH_TRANSFORM])
        self.coordinator.confirm(fresh.preview_id, "north-lead")
        self.coordinator.stage(
            fresh.preview_id, "north-redo-transform-2", {"rows": 16}, "north-worker"
        )
        updated, second = self.coordinator.submit(fresh.preview_id, submitted_by="north-worker")
        self.assertTrue(second.accepted)
        self.assertEqual(PreviewStatus.ACCEPTED, updated.status)
        self.assertEqual(
            6,
            self.warehouse.current(target(OutputKind.NORTH_TRANSFORM, "north_transform")).version,
        )

    def test_repreview_requires_new_grants(self) -> None:
        north = self._north_preview()
        self._grant_scope(north, Station.NORTH, [OutputKind.NORTH_TRANSFORM])
        self.coordinator.confirm(north.preview_id, "north-lead")
        retry = self.coordinator.re_preview(north.preview_id)
        self.assertEqual(2, retry.attempt)
        self.assertEqual(PreviewStatus.DRAFT, retry.status)
        with self.assertRaises(ApprovalError):
            self.coordinator.confirm(retry.preview_id, "north-lead")
        self._grant_scope(retry, Station.NORTH, [OutputKind.NORTH_TRANSFORM])
        confirmed = self.coordinator.confirm(retry.preview_id, "north-lead")
        self.assertEqual(PreviewStatus.CONFIRMED, confirmed.status)

    def test_invalid_preview_does_not_touch_warehouse(self) -> None:
        before = self.warehouse.all_current()
        with self.assertRaises(PreviewStateError):
            self.coordinator.preview(
                PreviewRequest(
                    request_id="empty",
                    stations=(Station.NORTH,),
                    writes=(),
                    authorizations={},
                )
            )
        self.assertEqual(before, self.warehouse.all_current())

    def test_confirm_is_scoped_to_approved_outputs(self) -> None:
        preview = self._south_preview()
        self._grant_scope(preview, Station.SOUTH, [OutputKind.SOUTH_SAMPLE, OutputKind.SOUTH_CREDENTIAL])
        self.coordinator.confirm(
            preview.preview_id, "south-lead", scope=[OutputKind.SOUTH_SAMPLE]
        )
        self.coordinator.stage(preview.preview_id, "south-resample", {"samples": 1}, "south-worker")
        updated, receipt = self.coordinator.submit(preview.preview_id, submitted_by="south-worker")
        self.assertTrue(receipt.accepted)
        self.assertEqual(PreviewStatus.ACCEPTED, updated.status)
        self.assertEqual(
            {"south-resample"},
            {outcome.write_id for outcome in receipt.outcomes},
        )

    def test_observing_delivery_twice_is_idempotent(self) -> None:
        preview = self._north_preview()
        self._grant_scope(preview, Station.NORTH, [OutputKind.NORTH_TRANSFORM])
        self.coordinator.confirm(preview.preview_id, "north-lead")
        self.coordinator.stage(preview.preview_id, "north-redo-transform", {"rows": 1}, "north-worker")
        self.coordinator.submit(preview.preview_id, submitted_by="north-worker", deliver=False)
        self.warehouse.deliver(preview.preview_id)
        first = self.coordinator.observe_delivery(preview.preview_id)
        second = self.coordinator.observe_delivery(preview.preview_id)
        self.assertEqual(PreviewStatus.ACCEPTED, first.status)
        self.assertEqual(first, second)
        self.assertEqual(
            5,
            self.warehouse.current(target(OutputKind.NORTH_TRANSFORM, "north_transform")).version,
        )

    def test_preview_without_write_ids_still_reports_impact(self) -> None:
        preview = self.coordinator.preview(
            PreviewRequest(
                request_id="no-write-ids",
                stations=(Station.NORTH,),
                writes=(
                    PlannedWrite(
                        target=target(OutputKind.NORTH_TRANSFORM, "north_transform"),
                        operation=GrantOperation.CREATE_REVISION,
                        base_version=4,
                    ),
                ),
                authorizations={},
                affected_tables=(TABLE,),
            )
        )
        self.assertFalse(preview.writes[0].authorized)
        # The declared downstream table and the write's own table both matter.
        self.assertEqual((TABLE,), preview.impact.affected_tables)
        self.assertIn("not declared", preview.writes[0].blocked_reason)

    def test_cross_station_grants_cannot_be_recorded_on_wrong_station(self) -> None:
        preview = self._north_preview()
        with self.assertRaises(GrantNotPermittedError):
            self.approvals.grant(
                preview.preview_id,
                Station.SOUTH,
                preview.request.authorization_for(OutputKind.NORTH_TRANSFORM),
                "south-approver",
            )

    def test_accepted_north_new_revision_does_not_let_old_south_preview_overwrite_it(self) -> None:
        # North redoes the transform and also rewrites the month table using
        # the old south transform version as its other input.
        north = self.coordinator.preview(
            PreviewRequest(
                request_id="north-with-month",
                stations=(Station.NORTH,),
                writes=(
                    write(
                        OutputKind.NORTH_TRANSFORM,
                        "north_transform",
                        base_version=4,
                        write_id="north-transform-new",
                    ),
                    write(
                        OutputKind.MONTH_TABLE,
                        MONTH,
                        operation=GrantOperation.CREATE_REVISION,
                        base_version=7,
                        write_id="month-new",
                        input_versions={
                            "north_transform:north_transform": 4,
                            "south_transform:south_transform": 4,
                        },
                    ),
                ),
                authorizations={
                    OutputKind.NORTH_TRANSFORM: OutputAuthorization(
                        OutputKind.NORTH_TRANSFORM, can_create_revision=True
                    ),
                    OutputKind.MONTH_TABLE: OutputAuthorization(
                        OutputKind.MONTH_TABLE, can_create_revision=True
                    ),
                },
                archive_id="archive-235",
                affected_tables=(TABLE,),
            )
        )
        self._grant_scope(north, Station.NORTH, [OutputKind.NORTH_TRANSFORM])
        self._grant_scope(north, Station.NORTH, [OutputKind.MONTH_TABLE])
        self.coordinator.confirm(north.preview_id, "north-lead")
        staged_transform = self.coordinator.stage(
            north.preview_id, "north-transform-new", {"rows": 100}, "north-worker"
        )
        # Only the transform reaches the warehouse before the local save fails.
        self.warehouse.commit(north.preview_id, [staged_transform], submitter="north-worker")
        self.archive.record_local_version(staged_transform.target, version=4)  # stale on purpose

        # The old south preview still references the pre-redo north result.
        south = self.coordinator.preview(
            PreviewRequest(
                request_id="south-old",
                stations=(Station.SOUTH,),
                writes=(
                    write(
                        OutputKind.SOUTH_SAMPLE,
                        "south_sample",
                        base_version=3,
                        write_id="south-sample-old",
                    ),
                    write(
                        OutputKind.MONTH_TABLE,
                        MONTH,
                        operation=GrantOperation.CREATE_REVISION,
                        base_version=7,
                        write_id="south-month-old",
                        input_versions={
                            "north_transform:north_transform": 4,
                            "south_transform:south_transform": 4,
                        },
                    ),
                ),
                authorizations={
                    OutputKind.SOUTH_SAMPLE: OutputAuthorization(
                        OutputKind.SOUTH_SAMPLE, can_create_revision=True
                    ),
                    OutputKind.MONTH_TABLE: OutputAuthorization(
                        OutputKind.MONTH_TABLE, can_create_revision=True
                    ),
                },
                archive_id="archive-235",
                affected_tables=(TABLE,),
            )
        )
        self._grant_scope(south, Station.SOUTH, [OutputKind.SOUTH_SAMPLE])
        self._grant_scope(south, Station.SOUTH, [OutputKind.MONTH_TABLE])
        self.coordinator.confirm(south.preview_id, "south-lead")
        staged_sample = self.coordinator.stage(
            south.preview_id, "south-sample-old", {"samples": 9}, "south-worker"
        )
        staged_month = self.coordinator.stage(
            south.preview_id, "south-month-old", {"total": 9}, "south-worker"
        )

        report = self.coordinator.recover()
        accepted_north = report.accepted_versions[
            target(OutputKind.NORTH_TRANSFORM, "north_transform").key()
        ]
        self.assertEqual(5, accepted_north.version)
        self.assertEqual(4, report.local_versions[staged_transform.target.key()])
        self.assertTrue(report.archive_lag)
        self.assertIn(staged_transform.write_id, {item.staged.write_id for item in report.uneffective_results})

        result = self.coordinator.resume(south.preview_id, submitted_by="south-worker")
        self.assertTrue(result.requires_repreview)
        self.assertFalse(result.submitted)
        # The old south plan did not overwrite the accepted north revision.
        self.assertEqual(
            5,
            self.warehouse.current(target(OutputKind.NORTH_TRANSFORM, "north_transform")).version,
        )
        # Nor did it resend south evidence or rewrite the month table.
        self.assertEqual(
            3,
            self.warehouse.current(target(OutputKind.SOUTH_SAMPLE, "south_sample")).version,
        )
        self.assertEqual(
            7,
            self.warehouse.current(target(OutputKind.MONTH_TABLE, MONTH)).version,
        )
        self.assertIn(staged_month.write_id, {item.staged.write_id for item in result.uneffective_results})

        # A fresh preview must restate authorization and use the actual north
        # revision; the old grants are not inherited.
        refreshed = self.coordinator.rehydrate(south.preview_id)
        self.assertEqual(PreviewStatus.DRAFT, refreshed.status)
        with self.assertRaises(ApprovalError):
            self.coordinator.confirm(refreshed.preview_id, "south-lead")
        self._grant_scope(refreshed, Station.SOUTH, [OutputKind.SOUTH_SAMPLE])
        self.coordinator.confirm(
            refreshed.preview_id,
            "south-lead",
            scope=[OutputKind.SOUTH_SAMPLE],
        )
        month_write = next(w for w in refreshed.writes if w.target.kind is OutputKind.MONTH_TABLE)
        self.assertEqual(5, month_write.input_versions["north_transform:north_transform"])

    def test_warehouse_accepts_and_rejects_each_output_independently(self) -> None:
        preview = self._south_preview()
        self._grant_scope(preview, Station.SOUTH, [OutputKind.SOUTH_SAMPLE])
        self._grant_scope(preview, Station.SOUTH, [OutputKind.SOUTH_CREDENTIAL])
        self.coordinator.confirm(
            preview.preview_id,
            "south-lead",
            scope=[OutputKind.SOUTH_SAMPLE, OutputKind.SOUTH_CREDENTIAL],
        )
        self.coordinator.stage(preview.preview_id, "south-resample", {"samples": 4}, "south-worker")
        self.coordinator.stage(preview.preview_id, "south-credential-refresh", {"token": "t"}, "south-worker")
        # Another writer moves the credential after staging but before
        # submission.  The sample is still on the current version.
        self.warehouse.accept(
            self._external_staged(
                OutputKind.SOUTH_CREDENTIAL,
                "south_credential",
                base_version=2,
                write_id="external-credential",
                values={"token": "external"},
            ),
            accepted_by="another-writer",
        )
        with self.assertRaises(StaleVersionError):
            self.coordinator.submit(preview.preview_id, submitted_by="south-worker")
        self.assertEqual(PreviewStatus.CONFIRMED, self.archive.get_preview(preview.preview_id).status)
        self.assertEqual(3, self.warehouse.current(target(OutputKind.SOUTH_SAMPLE, "south_sample")).version)

    def test_credentials_remain_queryable_after_rejection(self) -> None:
        south = self._south_preview()
        self._grant_scope(south, Station.SOUTH, [OutputKind.SOUTH_CREDENTIAL])
        self.coordinator.confirm(
            south.preview_id, "south-lead", scope=[OutputKind.SOUTH_CREDENTIAL]
        )
        staged = self.coordinator.stage(
            south.preview_id, "south-credential-refresh", {"token": "old"}, "south-worker"
        )
        self.archive.record_credential("south", "credential-235", {"token": "old", "staged": staged.write_id})
        self.archive.record_credential("south", "credential-235", {"token": "new", "rejected": True})
        history = self.archive.credential_history("south", "credential-235")
        self.assertEqual(2, len(history))
        self.assertEqual("old", history[0]["token"])
        self.archive.note_contradiction("credential evidence was not resent")
        report = self.coordinator.recover()
        self.assertIn("credential evidence was not resent", report.notes)

    def test_new_month_table_revision_uses_actual_versions(self) -> None:
        preview = self.coordinator.preview(
            PreviewRequest(
                request_id="month-new-revision",
                stations=(Station.NORTH, Station.SOUTH),
                writes=(
                    write(
                        OutputKind.MONTH_TABLE,
                        MONTH,
                        operation=GrantOperation.CREATE_REVISION,
                        base_version=7,
                        write_id="month-new",
                        input_versions={
                            "north_transform:north_transform": 4,
                            "south_transform:south_transform": 4,
                        },
                    ),
                ),
                authorizations={
                    OutputKind.MONTH_TABLE: OutputAuthorization(
                        OutputKind.MONTH_TABLE, can_create_revision=True
                    ),
                },
                affected_tables=(TABLE,),
            )
        )
        self._grant_scope(preview, Station.NORTH, [OutputKind.MONTH_TABLE])
        self.coordinator.confirm(
            preview.preview_id, "reader", scope=[OutputKind.MONTH_TABLE]
        )
        self.coordinator.stage(preview.preview_id, "month-new", {"total": 42}, "reader")
        updated, receipt = self.coordinator.submit(preview.preview_id, submitted_by="reader")
        self.assertTrue(receipt.accepted)
        self.assertEqual(PreviewStatus.ACCEPTED, updated.status)
        current = self.warehouse.current(target(OutputKind.MONTH_TABLE, MONTH))
        self.assertEqual(8, current.version)
        self.assertEqual({"total": 42}, dict(current.payload))

    def test_external_change_after_local_check_is_reported_not_assumed(self) -> None:
        preview = self._north_preview()
        self._grant_scope(preview, Station.NORTH, [OutputKind.NORTH_TRANSFORM])
        confirmed = self.coordinator.confirm(preview.preview_id, "north-lead")
        self.coordinator.stage(preview.preview_id, "north-redo-transform", {"rows": 9}, "north-worker")

        original_check = self.coordinator._check_submission
        raced = {"done": False}

        def check_then_race(preview_arg, staged_arg):
            original_check(preview_arg, staged_arg)
            if not raced["done"]:
                raced["done"] = True
                # The local check passed; now an external writer moves the
                # target before this node reaches the warehouse.
                self.warehouse.accept(
                    self._external_staged(
                        OutputKind.NORTH_TRANSFORM,
                        "north_transform",
                        base_version=4,
                        write_id="external-transform",
                        values={"rows": 999},
                    ),
                    accepted_by="another-writer",
                )

        self.coordinator._check_submission = check_then_race
        updated, receipt = self.coordinator.submit(preview.preview_id, submitted_by="north-worker")
        self.assertFalse(receipt.accepted)
        self.assertEqual(PreviewStatus.REJECTED, updated.status)
        self.assertIn("base 4", receipt.rejected_writes[0].reason)
        current = self.warehouse.current(target(OutputKind.NORTH_TRANSFORM, "north_transform"))
        self.assertEqual(5, current.version)
        self.assertEqual("external-transform", current.write_id)
        # The archive records the rejection, not a success.
        self.assertEqual(PreviewStatus.REJECTED, self.archive.get_preview(preview.preview_id).status)
        report = self.coordinator.recover()
        self.assertNotIn(
            "north-redo-transform",
            {version.write_id for version in report.accepted_versions.values()},
        )

    def test_south_credential_and_export_are_independent_writes(self) -> None:
        preview = self.coordinator.preview(
            PreviewRequest(
                request_id="south-export",
                stations=(Station.SOUTH,),
                writes=(
                    write(
                        OutputKind.SOUTH_CREDENTIAL,
                        "south_credential",
                        base_version=2,
                        write_id="south-credential",
                    ),
                    write(
                        OutputKind.SOUTH_EXPORT,
                        "south_export",
                        base_version=1,
                        write_id="south-export",
                    ),
                ),
                authorizations={
                    OutputKind.SOUTH_CREDENTIAL: OutputAuthorization(
                        OutputKind.SOUTH_CREDENTIAL, can_create_revision=True
                    ),
                    OutputKind.SOUTH_EXPORT: OutputAuthorization(
                        OutputKind.SOUTH_EXPORT, can_create_revision=True
                    ),
                },
                affected_tables=(TABLE,),
            )
        )
        self._grant_scope(
            preview,
            Station.SOUTH,
            [OutputKind.SOUTH_CREDENTIAL, OutputKind.SOUTH_EXPORT],
        )
        self.coordinator.confirm(preview.preview_id, "south-lead")
        self.coordinator.stage(preview.preview_id, "south-credential", {"token": "a"}, "south-worker")
        self.coordinator.stage(preview.preview_id, "south-export", {"batches": 2}, "south-worker")
        # The export is moved by another writer after the local check would
        # have passed but before the warehouse call.
        self.warehouse.accept(
            self._external_staged(
                OutputKind.SOUTH_EXPORT,
                "south_export",
                base_version=1,
                write_id="external-export",
                values={"batches": 1},
            ),
            accepted_by="another-writer",
        )
        with self.assertRaises(StaleVersionError):
            self.coordinator.submit(preview.preview_id, submitted_by="south-worker")
        # Nothing was written, including the credential that was still valid.
        self.assertEqual(2, self.warehouse.current(target(OutputKind.SOUTH_CREDENTIAL, "south_credential")).version)
        self.assertEqual("external-export", self.warehouse.current(target(OutputKind.SOUTH_EXPORT, "south_export")).write_id)

    def _external_staged(
        self,
        kind: OutputKind,
        name: str,
        base_version: int,
        write_id: str,
        values,
    ):
        from SpiffWorkflow.backfill import StagedResult

        return StagedResult(
            preview_id="external",
            output=kind,
            target=target(kind, name),
            values=values,
            write_id=write_id,
            operation=GrantOperation.CREATE_REVISION,
            base_version=base_version,
            input_versions={},
        )


if __name__ == "__main__":
    unittest.main()
