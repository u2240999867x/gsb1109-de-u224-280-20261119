"""Migration helpers for splitting a batched file wait into two acceptance stages.

This module is intentionally separate from :mod:`SpiffWorkflow.bpmn.util.diff`.
A generic spec diff aligns specs by BPMN id and rewrites every ``READY`` or
``WAITING`` task it can align; that is the wrong tool when one old ``WaitFile``
node becomes a data wait plus a check wait and the same node name appears in
more than one supplier subprocess.  Here the mapping is scoped to the supplier
process, the old task identity is retained, and the old receipt IDs remain the
business identity of the receipt.

The migration has three phases:

``plan``
    Validate a dormant archive and preview how every old wait task will be
    represented.  Nothing is changed.

between ``plan`` and ``apply``
    The old version may keep working; for example it may acknowledge a late
    data notification.

``apply``
    Re-read the receipt ledger immediately before switching.  If a receipt
    cannot be classified, the archive is no longer dormant, or a cutover check
    says an old-version writer may still be active, a :class:`MigrationConflict`
    is raised before the first mutation.  The old workflow therefore remains
    exactly resumable.

Processing records live in the top level workflow data, so they survive the
normal serializer save/restore cycle.  A receipt keeps a single record even
though the new model has two nodes.
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Callable, Iterable, Optional

from SpiffWorkflow.task import TaskState


DATA = "data"
CHECK = "check"
RECEIPT_KINDS = (DATA, CHECK)

RECEIPT_QUEUED = "queued"
RECEIPT_ACKNOWLEDGED = "acknowledged"

OLD_WAIT_NODE = "WaitFile"
JOURNAL_KEY = "__two_step_migration__"
JOURNAL_VERSION = 1


class MigrationConflict(Exception):
    """A refusal that identifies the archive location that caused it.

    ``apply`` raises this before mutating a workflow.  Callers can inspect
    ``code`` and ``locate()`` and continue using the old workflow.
    """

    def __init__(
        self,
        code: str,
        message: str,
        *,
        instance_id: Optional[str] = None,
        process: Optional[str] = None,
        node: Optional[str] = None,
        task_id: Optional[str] = None,
        receipt_id: Optional[str] = None,
        detail: Optional[dict] = None,
    ):
        super().__init__(message)
        self.code = code
        self.instance_id = instance_id
        self.process = process
        self.node = node
        self.task_id = task_id
        self.receipt_id = receipt_id
        self.detail = detail or {}

    def locate(self) -> dict:
        """Return the conflict as a JSON-friendly location record."""
        return {
            "code": self.code,
            "instance_id": self.instance_id,
            "process": self.process,
            "node": self.node,
            "task_id": self.task_id,
            "receipt_id": self.receipt_id,
            "detail": deepcopy(self.detail),
        }

    def __str__(self) -> str:
        location = ", ".join(
            f"{key}={value}"
            for key, value in self.locate().items()
            if value not in (None, {}, "")
        )
        return f"{super().__str__()} ({location})"


@dataclass
class FileReceipt:
    """A receipt from the old file collection stage.

    ``kind`` is the only fact that lets the migration decide whether a receipt
    belongs to the data stage or the check stage.  An unknown kind is reported
    as a conflict instead of being inferred from task order.
    """

    receipt_id: str
    instance_id: str
    supplier: str
    kind: Optional[str] = None
    state: str = RECEIPT_QUEUED
    batch_id: Optional[str] = None
    old_node: str = OLD_WAIT_NODE
    old_task_id: Optional[str] = None
    payload: dict = field(default_factory=dict)

    def is_acknowledged(self) -> bool:
        return self.state == RECEIPT_ACKNOWLEDGED


class ReceiptLedger:
    """The small interface the migration needs from the receipt store."""

    def receipts(self, instance_id: str) -> Iterable[FileReceipt]:
        raise NotImplementedError

    def receipt(self, instance_id: str, receipt_id: str) -> Optional[FileReceipt]:
        raise NotImplementedError

    def revision(self, instance_id: str) -> object:
        raise NotImplementedError


class InMemoryReceiptLedger(ReceiptLedger):
    """A deterministic ledger for applications and tests."""

    def __init__(self):
        self._receipts: dict[str, dict[str, FileReceipt]] = {}
        self._revision: dict[str, int] = {}

    def add(self, receipt: FileReceipt) -> FileReceipt:
        existing = self._find(receipt.instance_id, receipt.receipt_id)
        if existing is not None:
            if existing.kind != receipt.kind or existing.supplier != receipt.supplier:
                raise ValueError(f"Receipt {receipt.receipt_id} was re-used with different facts")
            existing.state = receipt.state
            existing.payload = deepcopy(receipt.payload)
            existing.batch_id = receipt.batch_id
            existing.old_node = receipt.old_node
            existing.old_task_id = receipt.old_task_id
        else:
            self._receipts.setdefault(receipt.instance_id, {})[receipt.receipt_id] = deepcopy(receipt)
        self._bump(receipt.instance_id)
        return deepcopy(self._receipts[receipt.instance_id][receipt.receipt_id])

    def acknowledge(self, instance_id: str, receipt_id: str) -> FileReceipt:
        receipt = self._find(instance_id, receipt_id)
        if receipt is None:
            raise KeyError(receipt_id)
        receipt.state = RECEIPT_ACKNOWLEDGED
        self._bump(instance_id)
        return deepcopy(receipt)

    def queue(self, instance_id: str, receipt_id: str) -> FileReceipt:
        receipt = self._find(instance_id, receipt_id)
        if receipt is None:
            raise KeyError(receipt_id)
        receipt.state = RECEIPT_QUEUED
        self._bump(instance_id)
        return deepcopy(receipt)

    def receipts(self, instance_id: str) -> Iterable[FileReceipt]:
        # Return copies: planning must never mutate the external store.
        return [deepcopy(r) for r in self._receipts.get(instance_id, {}).values()]

    def receipt(self, instance_id: str, receipt_id: str) -> Optional[FileReceipt]:
        receipt = self._find(instance_id, receipt_id)
        return deepcopy(receipt) if receipt is not None else None

    def _find(self, instance_id: str, receipt_id: str) -> Optional[FileReceipt]:
        return self._receipts.get(instance_id, {}).get(receipt_id)

    def revision(self, instance_id: str) -> object:
        return self._revision.get(instance_id, 0)

    def _bump(self, instance_id: str) -> None:
        self._revision[instance_id] = self._revision.get(instance_id, 0) + 1


@dataclass(frozen=True)
class WaitNodeSplit:
    """The old node and its two new nodes, scoped to one supplier process."""

    process: str
    old_node: str
    data_node: str
    check_node: str

    @property
    def supplier(self) -> str:
        return self.process


@dataclass(frozen=True)
class FieldRename:
    """A supplier-specific input field rename for one acceptance stage."""

    process: str
    kind: str
    old_name: str
    new_name: str

    def __post_init__(self):
        if self.kind not in RECEIPT_KINDS:
            raise ValueError(f"Unknown field kind: {self.kind}")


@dataclass
class TwoStepMapping:
    splits: list[WaitNodeSplit]
    renames: list[FieldRename] = field(default_factory=list)

    def split_for(self, process: str, old_node: str, bpmn_name: Optional[str] = None) -> WaitNodeSplit:
        for split in self.splits:
            if split.process != process:
                continue
            if split.old_node in (old_node, bpmn_name):
                return split
        raise KeyError((process, old_node))

    def split_for_supplier(self, supplier: str) -> Optional[WaitNodeSplit]:
        for split in self.splits:
            if split.process == supplier:
                return split
        return None

    def renames_for(self, process: str, kind: str) -> list[FieldRename]:
        return [r for r in self.renames if r.process == process and r.kind == kind]


@dataclass
class StageDecision:
    """A preview of how one old wait task will be represented in the new model."""

    process: str
    old_node: str
    old_task_id: str
    data_satisfied: bool
    check_satisfied: bool
    data_receipts: tuple[str, ...] = ()
    check_receipts: tuple[str, ...] = ()
    data_node: Optional[str] = None
    check_node: Optional[str] = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()


@dataclass
class TwoStepMigrationPlan:
    instance_id: str
    mapping: TwoStepMapping
    top_spec: object
    subprocess_specs: dict
    preview: list[StageDecision]
    planned_at_revision: object

    def preview_dict(self) -> list[dict]:
        return [decision.to_dict() for decision in self.preview]


@dataclass
class ProcessingRecord:
    """The stable link between one old receipt and one new node."""

    receipt_id: str
    instance_id: str
    supplier: str
    kind: str
    old_node: str
    old_task_id: Optional[str]
    new_node: str
    new_task_id: Optional[str]
    status: str = RECEIPT_QUEUED
    applied: bool = False
    held: bool = False
    migrated: bool = True
    note: Optional[str] = None

    def to_dict(self) -> dict:
        return self.__dict__.copy()

    @classmethod
    def from_dict(cls, dct: dict) -> "ProcessingRecord":
        # Tolerate records written before the applied/held flags existed.
        values = dict(dct)
        values.setdefault("applied", False)
        values.setdefault("held", False)
        values.setdefault("migrated", True)
        values.setdefault("note", None)
        return cls(**values)


@dataclass
class MigrationResult:
    instance_id: str
    records: list[ProcessingRecord]
    preview: list[StageDecision]


def _top_workflow(workflow):
    return getattr(workflow, "top_workflow", workflow)


def _journal(workflow) -> dict:
    top = _top_workflow(workflow)
    journal = top.data.setdefault(JOURNAL_KEY, {})
    journal.setdefault("version", JOURNAL_VERSION)
    journal.setdefault("records", {})
    journal.setdefault("pending", {})
    journal.setdefault("preview", [])
    return journal


def _record_to_dict(record: ProcessingRecord) -> dict:
    return record.to_dict()


def processing_records(workflow) -> list[ProcessingRecord]:
    journal = _journal(workflow)
    return [ProcessingRecord.from_dict(dct) for dct in journal["records"].values()]


def find_processing_record(workflow, receipt_id: str) -> Optional[ProcessingRecord]:
    journal = _journal(workflow)
    dct = journal["records"].get(receipt_id)
    return ProcessingRecord.from_dict(dct) if dct is not None else None


def _store_record(workflow, record: ProcessingRecord) -> None:
    journal = _journal(workflow)
    existing = journal["records"].get(record.receipt_id)
    if existing is not None:
        if existing.get("instance_id") != record.instance_id:
            raise ValueError(f"Receipt {record.receipt_id} belongs to another instance")
        if existing.get("kind") != record.kind or existing.get("supplier") != record.supplier:
            raise ValueError(f"Receipt {record.receipt_id} was re-used with different facts")
        existing.update(_record_to_dict(record))
    else:
        journal["records"][record.receipt_id] = _record_to_dict(record)


def _all_workflows(workflow):
    yield workflow
    for subprocess in getattr(workflow, "subprocesses", {}).values():
        yield subprocess


def _is_old_wait(task, old_node: str = OLD_WAIT_NODE) -> bool:
    """Match the old wait by BPMN id or by its display name.

    BPMN requires IDs to be unique for the whole document, so two supplier
    subprocesses cannot both use ``id="WaitFile"``.  The old models therefore
    use a shared label and supplier-specific IDs; both spellings are accepted.
    """
    spec = task.task_spec
    return spec.name == old_node or getattr(spec, "bpmn_name", None) == old_node


def _split_for_task(mapping: TwoStepMapping, process: str, task) -> WaitNodeSplit:
    spec = task.task_spec
    return mapping.split_for(process, spec.name, getattr(spec, "bpmn_name", None))


def _is_wait_for_process(mapping: TwoStepMapping, process: str, task) -> bool:
    split = mapping.split_for_supplier(process)
    return split is not None and _is_old_wait(task, split.old_node)


def _task_location(task) -> dict:
    return {
        "process": task.workflow.spec.name,
        "node": task.task_spec.name,
        "task_id": str(task.id),
    }


def _assert_dormant(workflow, instance_id: str) -> None:
    """A dormant archive has no task the old engine could currently run."""
    subprocess_parents = set(getattr(workflow, "subprocesses", {}))
    for wf in _all_workflows(workflow):
        for task in wf.tasks.values():
            if task.has_state(TaskState.ERROR):
                raise MigrationConflict(
                    "archive_not_dormant",
                    "The archive contains an errored task",
                    instance_id=instance_id,
                    **_task_location(task),
                )
            if task.has_state(TaskState.READY):
                raise MigrationConflict(
                    "archive_not_dormant",
                    "The archive contains a runnable task",
                    instance_id=instance_id,
                    **_task_location(task),
                )
            if task.has_state(TaskState.STARTED) and task.id not in subprocess_parents:
                raise MigrationConflict(
                    "archive_not_dormant",
                    "The archive contains a started task",
                    instance_id=instance_id,
                    **_task_location(task),
                )


def _assert_writer_quiescent(writer_check: Optional[Callable], instance_id: str) -> None:
    if writer_check is None:
        return
    result = writer_check(instance_id)
    if result:
        detail = result if isinstance(result, dict) else {"writer": result}
        raise MigrationConflict(
            "old_version_writer",
            "The cutover check reports that an old-version processor may still write",
            instance_id=instance_id,
            detail=detail,
        )


def _relevant_receipts(ledger: ReceiptLedger, instance_id: str, mapping: TwoStepMapping) -> list[FileReceipt]:
    receipts = []
    for receipt in ledger.receipts(instance_id):
        if receipt.supplier and mapping.split_for_supplier(receipt.supplier) is not None:
            receipts.append(receipt)
    return receipts


def _locate_receipts(workflow, receipts: Iterable[FileReceipt]) -> None:
    """Fill in the old task location for receipts that only name a supplier.

    This makes a refusal actionable even when the receipt store records only
    the business facts.  It never changes the classification of a receipt.
    """
    by_process = {}
    for wf in _all_workflows(workflow):
        for task in wf.tasks.values():
            if _is_old_wait(task):
                by_process.setdefault(wf.spec.name, task)
    for receipt in receipts:
        task = by_process.get(receipt.supplier)
        if task is None:
            continue
        if receipt.old_task_id is None:
            receipt.old_task_id = str(task.id)
        if receipt.old_node == OLD_WAIT_NODE and getattr(task.task_spec, "bpmn_name", None):
            receipt.old_node = task.task_spec.bpmn_name


def _assert_classified(receipts: Iterable[FileReceipt], instance_id: str) -> None:
    by_id: dict[str, FileReceipt] = {}
    for receipt in receipts:
        if receipt.kind not in RECEIPT_KINDS:
            raise MigrationConflict(
                "unclassified_receipt",
                "The archive cannot prove whether this receipt is for data or check",
                instance_id=instance_id,
                process=receipt.supplier,
                node=receipt.old_node,
                task_id=receipt.old_task_id,
                receipt_id=receipt.receipt_id,
                detail={"receipt_state": receipt.state},
            )
        previous = by_id.get(receipt.receipt_id)
        if previous is not None and (previous.kind != receipt.kind or previous.supplier != receipt.supplier):
            raise MigrationConflict(
                "ambiguous_receipt",
                "The same receipt ID is present with conflicting classifications",
                instance_id=instance_id,
                process=receipt.supplier,
                node=receipt.old_node,
                receipt_id=receipt.receipt_id,
            )
        by_id[receipt.receipt_id] = receipt


def _assert_target_specs(workflow, mapping: TwoStepMapping, instance_id: str, new_spec, new_subprocess_specs) -> None:
    """Validate the complete target model before any task is changed."""
    for wf in _all_workflows(workflow):
        process = wf.spec.name
        target = new_spec if wf is workflow else new_subprocess_specs.get(process)
        if target is None:
            raise MigrationConflict(
                "missing_target_spec",
                "The new model has no process with this name",
                instance_id=instance_id,
                process=process,
            )
        for task in wf.tasks.values():
            if _is_old_wait(task) and mapping.split_for_supplier(process) is None:
                raise MigrationConflict(
                    "missing_node_mapping",
                    "No two-step mapping exists for this supplier process",
                    instance_id=instance_id,
                    **_task_location(task),
                )
            if _is_wait_for_process(mapping, process, task):
                try:
                    split = _split_for_task(mapping, process, task)
                except KeyError:
                    raise MigrationConflict(
                        "missing_node_mapping",
                        "No two-step mapping exists for this old wait node",
                        instance_id=instance_id,
                        **_task_location(task),
                    )
                for target_node in (split.data_node, split.check_node):
                    if target_node not in target.task_specs:
                        raise MigrationConflict(
                            "unknown_target_node",
                            "The new model does not contain both mapped wait nodes",
                            instance_id=instance_id,
                            process=process,
                            node=split.old_node,
                            task_id=str(task.id),
                            detail={"data_node": split.data_node, "check_node": split.check_node},
                        )
            elif task.task_spec.name not in target.task_specs:
                raise MigrationConflict(
                    "missing_target_spec",
                    "A task has no counterpart in the new process",
                    instance_id=instance_id,
                    **_task_location(task),
                )


def _decisions(workflow, receipts, mapping: TwoStepMapping, instance_id: str) -> list[StageDecision]:
    _assert_classified(receipts, instance_id)
    decisions = []
    for wf in _all_workflows(workflow):
        for task in wf.tasks.values():
            process = wf.spec.name
            if not _is_wait_for_process(mapping, process, task):
                continue
            split = _split_for_task(mapping, process, task)
            data = [r for r in receipts if r.supplier == process and r.kind == DATA]
            check = [r for r in receipts if r.supplier == process and r.kind == CHECK]
            decisions.append(
                StageDecision(
                    process=process,
                    old_node=OLD_WAIT_NODE,
                    old_task_id=str(task.id),
                    data_satisfied=bool(data) and all(r.is_acknowledged() for r in data),
                    check_satisfied=bool(check) and all(r.is_acknowledged() for r in check),
                    data_receipts=tuple(r.receipt_id for r in data if r.is_acknowledged()),
                    check_receipts=tuple(r.receipt_id for r in check if r.is_acknowledged()),
                    data_node=split.data_node,
                    check_node=split.check_node,
                )
            )
    return decisions


def _rename_fields(task, mapping: TwoStepMapping, process: str, kind: str, source: dict) -> None:
    for rename in mapping.renames_for(process, kind):
        if rename.old_name in source:
            task.data[rename.new_name] = deepcopy(source[rename.old_name])


def _drop_source_fields(task, mapping: TwoStepMapping, process: str) -> None:
    for kind in RECEIPT_KINDS:
        for rename in mapping.renames_for(process, kind):
            if rename.old_name != rename.new_name:
                task.data.pop(rename.old_name, None)


def _copy_unrenamed_fields(task, mapping: TwoStepMapping, process: str, source: dict) -> None:
    renamed = {
        rename.old_name
        for kind in RECEIPT_KINDS
        for rename in mapping.renames_for(process, kind)
    }
    for name, value in source.items():
        if name not in renamed and name not in task.data:
            task.data[name] = deepcopy(value)


def _predict_from(task, mask=TaskState.NOT_FINISHED_MASK):
    task.task_spec._predict(task, mask=mask)


def _register_waiting(task) -> None:
    task.task_spec._update(task)


def _complete_stage(task) -> None:
    """Complete a stage without re-running broad catch-event reset logic.

    ``CatchingEvent._run_hook`` resets every task with the same spec name in the
    top workflow.  That is too broad for supplier subprocesses that deliberately
    share node names, so a receipt-backed completion uses the normal state
    transition without that reset side effect.
    """
    top = _top_workflow(task.workflow)
    if hasattr(top, "event_manager"):
        top.event_manager.remove_task(task)
    task._set_state(TaskState.COMPLETED)
    task.task_spec._on_complete(task)


class TwoStepMigrator:
    """Plan and apply a two-stage acceptance migration.

    Arguments:
        mapping: node splits and field renames, keyed by supplier process name.
        ledger: the old receipt ledger.
        writer_check: optional callable ``(instance_id) -> truthy`` used as the
            cutover check.  A truthy result refuses the switch.
    """

    def __init__(self, mapping: TwoStepMapping, ledger: ReceiptLedger, writer_check=None):
        self.mapping = mapping
        self.ledger = ledger
        self.writer_check = writer_check

    def plan(self, workflow, new_spec, new_subprocess_specs, instance_id: str) -> TwoStepMigrationPlan:
        _assert_dormant(workflow, instance_id)
        _assert_target_specs(workflow, self.mapping, instance_id, new_spec, new_subprocess_specs)
        receipts = _relevant_receipts(self.ledger, instance_id, self.mapping)
        _locate_receipts(workflow, receipts)
        decisions = _decisions(workflow, receipts, self.mapping, instance_id)
        return TwoStepMigrationPlan(
            instance_id=instance_id,
            mapping=self.mapping,
            top_spec=new_spec,
            subprocess_specs=new_subprocess_specs,
            preview=decisions,
            planned_at_revision=self.ledger.revision(instance_id),
        )

    def apply(self, plan: TwoStepMigrationPlan, workflow, ledger: Optional[ReceiptLedger] = None) -> MigrationResult:
        ledger = ledger or self.ledger
        instance_id = plan.instance_id

        # Every refusal happens before the first mutation.  The old workflow
        # therefore remains exactly resumable if any check fails.
        _assert_dormant(workflow, instance_id)
        _assert_writer_quiescent(self.writer_check, instance_id)
        _assert_target_specs(workflow, plan.mapping, instance_id, plan.top_spec, plan.subprocess_specs)
        receipts = _relevant_receipts(ledger, instance_id, plan.mapping)
        _locate_receipts(workflow, receipts)
        decisions = _decisions(workflow, receipts, plan.mapping, instance_id)

        self._rebind_top(workflow, plan)
        records = []
        for wf in list(_all_workflows(workflow))[1:]:
            records.extend(self._migrate_subprocess(workflow, wf, plan, decisions, receipts))

        for record in records:
            _store_record(workflow, record)

        journal = _journal(workflow)
        journal["preview"] = plan.preview_dict()
        journal["mapping"] = {
            "splits": [split.__dict__.copy() for split in plan.mapping.splits],
            "renames": [rename.__dict__.copy() for rename in plan.mapping.renames],
        }

        # A check accepted before cutover can complete a supplier immediately;
        # a data-only supplier remains parked on its check wait.
        workflow.do_engine_steps()
        return MigrationResult(instance_id=instance_id, records=records, preview=decisions)

    def _rebind_top(self, workflow, plan: TwoStepMigrationPlan) -> None:
        new_spec = plan.top_spec
        for task in workflow.tasks.values():
            replacement = new_spec.task_specs.get(task.task_spec.name)
            if replacement is None:
                raise MigrationConflict(
                    "missing_target_spec",
                    "A top-level task has no counterpart in the new model",
                    instance_id=plan.instance_id,
                    **_task_location(task),
                )
            task.task_spec = replacement
        workflow.spec = new_spec
        workflow.subprocess_specs = plan.subprocess_specs

    def _migrate_subprocess(self, top, wf, plan, decisions, receipts) -> list[ProcessingRecord]:
        process = wf.spec.name
        new_spec = plan.subprocess_specs.get(process)
        if new_spec is None:
            raise MigrationConflict(
                "missing_target_spec",
                "The new model has no supplier process with this name",
                instance_id=plan.instance_id,
                process=process,
            )

        for task in list(wf.tasks.values()):
            if _is_wait_for_process(plan.mapping, process, task):
                continue
            replacement = new_spec.task_specs.get(task.task_spec.name)
            if replacement is None:
                raise MigrationConflict(
                    "missing_target_spec",
                    "A task has no counterpart in the new supplier process",
                    instance_id=plan.instance_id,
                    **_task_location(task),
                )
            task.task_spec = replacement
        wf.spec = new_spec

        records = []
        wait_tasks = [
            t for t in list(wf.tasks.values())
            if _is_wait_for_process(plan.mapping, process, t)
        ]
        for task in wait_tasks:
            decision = next((d for d in decisions if d.old_task_id == str(task.id)), None)
            if decision is None:
                raise MigrationConflict(
                    "missing_node_mapping",
                    "The wait task is not represented in the migration plan",
                    instance_id=plan.instance_id,
                    **_task_location(task),
                )
            records.extend(self._split_wait(top, wf, task, plan, decision, receipts))
        return records

    def _split_wait(self, top, wf, task, plan, decision, receipts) -> list[ProcessingRecord]:
        process = wf.spec.name
        split = _split_for_task(plan.mapping, process, task)
        old_task_id = str(task.id)
        old_data = deepcopy(task.data)
        data_spec = wf.spec.task_specs[split.data_node]
        check_spec = wf.spec.task_specs[split.check_node]

        if task.has_state(TaskState.CANCELLED):
            # A cancelled supplier must not be resurrected by the split.  Keep
            # the old task cancelled and map it to the first new stage so that
            # save/restore can still resolve its spec name.
            task.task_spec = data_spec
            return []

        # The old task ID becomes the data stage.  This keeps the old instance
        # path addressable from processing records; the check stage gets a new
        # task but shares the old business receipt.
        task.task_spec = data_spec
        _rename_fields(task, plan.mapping, process, DATA, old_data)
        _copy_unrenamed_fields(task, plan.mapping, process, old_data)
        _drop_source_fields(task, plan.mapping, process)
        task._drop_children(force=True)

        # Add the check node before predicting the data node so that
        # ``_sync_children`` reuses it instead of creating a duplicate copy of
        # the same wait.
        check_task = task._add_child(check_spec, TaskState.FUTURE)
        _rename_fields(check_task, plan.mapping, process, CHECK, old_data)
        _copy_unrenamed_fields(check_task, plan.mapping, process, old_data)
        _drop_source_fields(check_task, plan.mapping, process)
        _predict_from(task)

        records = []
        records.extend(self._stage_records(process, DATA, old_task_id, str(task.id), data_spec.name, receipts))

        # Receipts acknowledged before cutover are facts even if another file
        # of the same stage is still missing, so their payload is kept.
        for receipt in receipts:
            if receipt.supplier == process and receipt.kind == DATA and receipt.is_acknowledged():
                _rename_payload(task, plan.mapping, process, DATA, receipt.payload)
        for receipt in receipts:
            if receipt.supplier == process and receipt.kind == CHECK and receipt.is_acknowledged():
                _rename_payload(check_task, plan.mapping, process, CHECK, receipt.payload)

        if decision.data_satisfied:
            _complete_stage(task)
            self._mark_applied(records, process, DATA, data_spec.name)
        else:
            _register_waiting(task)

        hold_check = not decision.data_satisfied
        records.extend(
            self._stage_records(
                process, CHECK, old_task_id, str(check_task.id), check_spec.name, receipts,
                hold=hold_check,
            )
        )

        if decision.check_satisfied and decision.data_satisfied:
            _complete_stage(check_task)
            self._mark_applied(records, process, CHECK, check_spec.name)
        elif decision.data_satisfied:
            # Data is already complete, so the check wait is the active
            # acceptance point for any still-queued check notification.
            _register_waiting(check_task)
        else:
            # A check acknowledged before cutover cannot satisfy a stage that
            # requires data first.  Hold exactly those receipts; queued checks
            # remain in the journal and are delivered when data arrives.
            journal = _journal(top)
            for receipt in receipts:
                if receipt.supplier == process and receipt.kind == CHECK and receipt.is_acknowledged():
                    journal["pending"][receipt.receipt_id] = {
                        "payload": deepcopy(receipt.payload),
                        "kind": CHECK,
                    }

        return records

    def _stage_records(self, process, kind, old_task_id, new_task_id, new_node, receipts, hold=False):
        """Create one processing record per receipt needed by a stage.

        A batch may contain several files of the same kind.  The stage is only
        satisfied once every receipt has been acknowledged; until then each
        unacknowledged receipt gets a queued record with its original ID.
        ``hold`` marks receipts that were acknowledged before cutover but
        cannot be applied yet because an earlier stage is still open.
        """
        records = []
        for receipt in receipts:
            if receipt.supplier != process or receipt.kind != kind:
                continue
            status = RECEIPT_ACKNOWLEDGED if receipt.is_acknowledged() else RECEIPT_QUEUED
            note = "acknowledged before cutover" if receipt.is_acknowledged() else None
            if hold and receipt.is_acknowledged():
                note = "held until the preceding data stage completes"
            records.append(
                ProcessingRecord(
                    receipt_id=receipt.receipt_id,
                    instance_id=receipt.instance_id,
                    supplier=process,
                    kind=kind,
                    old_node=OLD_WAIT_NODE,
                    old_task_id=old_task_id,
                    new_node=new_node,
                    new_task_id=new_task_id,
                    status=status,
                    held=hold and receipt.is_acknowledged(),
                    note=note,
                )
            )
        return records

    def deliver(self, workflow, receipt_id: str, _settling: bool = False) -> ProcessingRecord:
        """Deliver one queued receipt to the correct new wait node.

        The method is idempotent: a receipt that was already applied returns the
        original processing record without completing a second node.
        """
        record = find_processing_record(workflow, receipt_id)
        if record is None:
            raise MigrationConflict(
                "unknown_receipt",
                "The new workflow has no processing record for this receipt",
                receipt_id=receipt_id,
            )
        if record.applied:
            return record
        if record.status == RECEIPT_ACKNOWLEDGED and not record.held:
            # It was applied by the migration itself; re-submitting the same
            # client ID must not complete another node.
            return record

        task = self._find_target_task(workflow, record)
        if task is None:
            raise MigrationConflict(
                "missing_target_task",
                "The new wait node for this receipt is no longer present",
                instance_id=record.instance_id,
                process=record.supplier,
                node=record.new_node,
                receipt_id=receipt_id,
            )
        if task.has_state(TaskState.FINISHED_MASK):
            record.status = RECEIPT_ACKNOWLEDGED
            record.applied = True
            _store_record(workflow, record)
            return record

        ledger_receipt = self.ledger.receipt(record.instance_id, receipt_id)
        payload = deepcopy(ledger_receipt.payload) if ledger_receipt is not None else {}

        if task.has_state(TaskState.FUTURE | TaskState.MAYBE | TaskState.LIKELY):
            # A check that arrived before data is held until the data stage
            # completes; the journal keeps the old receipt identity.
            journal = _journal(workflow)
            journal["pending"][receipt_id] = {"payload": payload, "kind": record.kind}
            record.held = True
            record.note = "held until the preceding data stage completes"
            _store_record(workflow, record)
            return record

        if payload:
            _rename_payload(task, self.mapping, record.supplier, record.kind, payload)
        was_held = record.held
        record.status = RECEIPT_ACKNOWLEDGED
        record.held = False
        record.new_task_id = str(task.id)
        record.note = "applied after data completed" if was_held else "delivered after cutover"
        _store_record(workflow, record)
        journal = _journal(workflow)
        journal["pending"].pop(receipt_id, None)
        # Several files may be part of one stage; the wait is only satisfied
        # after every receipt for that stage and supplier is acknowledged.
        if self._stage_ready(workflow, record):
            _complete_stage(task)
            stage_records = [
                r for r in processing_records(workflow)
                if r.supplier == record.supplier and r.kind == record.kind
            ]
            self._mark_applied(stage_records, record.supplier, record.kind, record.new_node)
            for item in stage_records:
                _store_record(workflow, item)
        workflow.do_engine_steps()
        if not _settling:
            self.settle(workflow)
        return record

    @staticmethod
    def _stage_ready(workflow, record) -> bool:
        records = [
            r for r in processing_records(workflow)
            if r.supplier == record.supplier and r.kind == record.kind
        ]
        return bool(records) and all(r.status == RECEIPT_ACKNOWLEDGED for r in records)

    @staticmethod
    def _mark_applied(records, process, kind, new_node):
        for item in records:
            if item.supplier == process and item.kind == kind and item.new_node == new_node:
                item.applied = True
                item.status = RECEIPT_ACKNOWLEDGED

    def settle(self, workflow) -> list[ProcessingRecord]:
        """Apply held receipts whose preceding stage has now completed."""
        journal = _journal(workflow)
        applied = []
        for receipt_id in list(journal["pending"]):
            record = find_processing_record(workflow, receipt_id)
            if record is None or record.applied:
                journal["pending"].pop(receipt_id, None)
                continue
            task = self._find_target_task(workflow, record)
            if task is None or task.has_state(TaskState.FUTURE | TaskState.MAYBE | TaskState.LIKELY):
                continue
            applied.append(self.deliver(workflow, receipt_id, _settling=True))
        return applied

    def _find_target_task(self, workflow, record):
        for wf in _all_workflows(workflow):
            if wf.spec.name != record.supplier:
                continue
            for task in wf.tasks.values():
                if str(task.id) == str(record.new_task_id):
                    return task
            for task in wf.tasks.values():
                if task.task_spec.name == record.new_node and task.has_state(
                    TaskState.WAITING | TaskState.READY | TaskState.FUTURE | TaskState.MAYBE | TaskState.LIKELY
                ):
                    return task
        return None


def _rename_payload(task, mapping: TwoStepMapping, process: str, kind: str, payload: dict) -> None:
    # Payload names are also supplier-specific.  Prefer an explicitly renamed
    # field, otherwise merge the receipt payload without dropping existing data.
    renamed = False
    for rename in mapping.renames_for(process, kind):
        if rename.old_name in payload:
            task.data[rename.new_name] = deepcopy(payload[rename.old_name])
            renamed = True
    if not renamed:
        task.data.update(deepcopy(payload))


def mapping_from_workflow(workflow) -> TwoStepMapping:
    """Rebuild the mapping stored at cutover.

    The mapping is saved with the new workflow so that a process restarted
    after the switch can still deliver receipts without the caller having to
    hold on to the original configuration object.
    """
    mapping = _journal(workflow).get("mapping")
    if mapping is None:
        raise ValueError("This workflow does not contain a two-step migration mapping")
    return TwoStepMapping(
        splits=[WaitNodeSplit(**split) for split in mapping["splits"]],
        renames=[FieldRename(**rename) for rename in mapping["renames"]],
    )


def trace_receipt(workflow, receipt_id: str) -> Optional[dict]:
    """Return the new-version processing record for an old receipt ID."""
    record = find_processing_record(workflow, receipt_id)
    if record is None:
        return None
    return {
        "receipt_id": record.receipt_id,
        "old_instance_id": record.instance_id,
        "old_node": record.old_node,
        "old_task_id": record.old_task_id,
        "new_node": record.new_node,
        "new_task_id": record.new_task_id,
        "kind": record.kind,
        "status": record.status,
        "business_receipts": 1,
    }
