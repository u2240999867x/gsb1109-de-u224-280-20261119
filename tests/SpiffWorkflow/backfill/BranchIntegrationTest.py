"""Integration test: a real SpiffWorkflow branch stages into a preview."""

from __future__ import annotations

import os
import unittest

from SpiffWorkflow.bpmn import BpmnWorkflow
from SpiffWorkflow.bpmn.parser import BpmnParser

from SpiffWorkflow.backfill import (
    ApprovalStore,
    ArchiveStore,
    BackfillCoordinator,
    BranchPreview,
    GrantOperation,
    OutputAuthorization,
    OutputKind,
    OutputTarget,
    PlannedWrite,
    PreviewRequest,
    PreviewStatus,
    Station,
    Warehouse,
)


class BranchIntegrationTest(unittest.TestCase):
    def setUp(self) -> None:
        parser = BpmnParser()
        parser.add_bpmn_file(
            os.path.join(os.path.dirname(__file__), "../bpmn/data/ScriptTest.bpmn")
        )
        spec = parser.get_spec("Process_1l85e0n")
        self.workflow = BpmnWorkflow(spec)
        self.archive = ArchiveStore()
        self.approvals = ApprovalStore()
        self.warehouse = Warehouse()
        self.coordinator = BackfillCoordinator(
            self.archive, self.approvals, self.warehouse
        )
        self.warehouse.ensure_output(
            OutputTarget(OutputKind.NORTH_TRANSFORM, "north_transform"), version=4
        )

    def _preview(self):
        request = PreviewRequest(
            request_id="branch",
            stations=(Station.NORTH,),
            writes=(
                PlannedWrite(
                    target=OutputTarget(OutputKind.NORTH_TRANSFORM, "north_transform"),
                    operation=GrantOperation.CREATE_REVISION,
                    base_version=4,
                    write_id="north-branch",
                ),
            ),
            authorizations={
                OutputKind.NORTH_TRANSFORM: OutputAuthorization(
                    OutputKind.NORTH_TRANSFORM, can_create_revision=True
                ),
            },
            affected_tables=("2026-11",),
        )
        preview = self.coordinator.preview(request)
        self.coordinator.grant(
            preview.preview_id,
            Station.NORTH,
            OutputKind.NORTH_TRANSFORM,
            can_create_revision=True,
            can_rewrite_version=False,
            granted_by="north-lead",
        )
        return self.coordinator.confirm(preview.preview_id, "north-lead")

    def test_completed_branch_stages_without_submitting(self) -> None:
        preview = self._preview()
        branch = BranchPreview(self.coordinator, preview, Station.NORTH)
        branch.bind("north-branch", {"sample": "rows", "testvar": "raw"}, "north-worker")

        # Behave like an external caller: run the BPMN branch, then stage.
        for key, value in branch.workflow_data().items():
            self.workflow.data[key] = value
        self.workflow.do_engine_steps()
        staged = branch.stage_completed_branch(self.workflow)

        self.assertEqual(1, len(staged))
        self.assertEqual({"rows": ["b", "c"], "raw": {"a": 1, "b": 2, "new": "Test"}}, dict(staged[0].values))
        # Staging did not submit; the warehouse is untouched.
        self.assertEqual(
            4,
            self.warehouse.current(
                OutputTarget(OutputKind.NORTH_TRANSFORM, "north_transform")
            ).version,
        )
        self.assertEqual(PreviewStatus.CONFIRMED, self.archive.get_preview(preview.preview_id).status)

    def test_branch_cannot_bind_month_table(self) -> None:
        preview = self._preview()
        branch = BranchPreview(self.coordinator, preview, Station.NORTH)
        with self.assertRaises(KeyError):
            branch.bind("month-rewrite", {"total": "total"}, "north-worker")


if __name__ == "__main__":
    unittest.main()
