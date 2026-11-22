"""Thin adapter between a SpiffWorkflow branch and a backfill preview.

A workstation branch is still an ordinary BPMN workflow.  This module only
moves data between that workflow and a :class:`BackfillPreviewSession`: the
branch runs with ``preview_mode`` set, its script tasks compute results, and the
session reads the named workflow variables into a :class:`StagedResult`
**without** submitting anything.  Submission remains a coordinator decision.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping

from .coordinator import BackfillCoordinator
from .model import Preview, StagedResult, Station


@dataclass(frozen=True)
class PreviewBinding:
    """Maps one previewed write to workflow variables and a write id."""

    write_id: str
    variables: Mapping[str, str]
    produced_by: str


class BranchPreview:
    """Runs one station's workflow branch behind a confirmed preview."""

    def __init__(
        self,
        coordinator: BackfillCoordinator,
        preview: Preview,
        station: Station,
    ) -> None:
        self.coordinator = coordinator
        self.preview = preview
        self.station = station
        self._bindings: dict[str, PreviewBinding] = {}

    def bind(self, write_id: str, variables: Mapping[str, str], produced_by: str) -> None:
        intent = next((w for w in self.preview.writes if w.write_id == write_id), None)
        if intent is None:
            raise KeyError(f"{write_id} is not part of {self.preview.preview_id}")
        if intent.target.kind.station is not None and intent.target.kind.station is not self.station:
            raise ValueError(
                f"{write_id} belongs to {intent.target.kind.station.value}, "
                f"not {self.station.value}"
            )
        self._bindings[write_id] = PreviewBinding(
            write_id, dict(variables), produced_by
        )

    def workflow_data(self) -> dict:
        """Initial variables the branch's script tasks may read."""

        return {
            "preview_mode": True,
            "preview_id": self.preview.preview_id,
            "preview_station": self.station.value,
        }

    def stage_completed_branch(self, workflow) -> list[StagedResult]:
        """Stage each bound write from the completed workflow's data."""

        data = workflow.last_task.data
        staged = []
        for write_id, binding in self._bindings.items():
            values = {}
            for variable, target_name in binding.variables.items():
                if variable not in data:
                    raise KeyError(
                        f"workflow did not produce {variable!r} for {write_id}"
                    )
                values[target_name] = data[variable]
            staged.append(
                self.coordinator.stage(
                    self.preview.preview_id,
                    write_id,
                    values,
                    produced_by=binding.produced_by,
                )
            )
        return staged

    def stage_values(
        self,
        write_id: str,
        values: Mapping[str, object],
    ) -> StagedResult:
        """Stage an already-extracted value set (useful for user tasks)."""

        binding = self._bindings.get(write_id)
        produced_by = binding.produced_by if binding else f"{self.station.value}-branch"
        return self.coordinator.stage(
            self.preview.preview_id, write_id, values, produced_by=produced_by
        )
