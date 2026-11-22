"""Reproducible diagnosis harness for the two-vendor Call Activity rollup.

Scope: evidence gathering only. The harness never changes SpiffWorkflow or the
BPMN behaviour; it only starts workflows, advances manual barriers, serializes
to JSON and restores from JSON.

Two evidence channels are kept strictly separate:

* public observations -- deep-copied task data and task states. These are the
  only kind of evidence the production platform can retain for a past run.
* identity probes -- Python ``id()`` / ``is`` answers for nested containers.
  They are labelled LAB-ONLY: object addresses are not available for a past run
  and do not survive serialization, so they are used here solely to demonstrate
  what public data alone can and cannot prove.
"""

from copy import deepcopy
from pathlib import Path

from SpiffWorkflow import TaskState
from SpiffWorkflow.bpmn import BpmnWorkflow
from SpiffWorkflow.bpmn.parser.BpmnParser import BpmnParser
from SpiffWorkflow.bpmn.serializer import BpmnWorkflowSerializer

SERIALIZER = BpmnWorkflowSerializer()

ROOT = Path(__file__).resolve().parent.parent
CASES = {
    "no_io": ROOT / "bpmn" / "no_io",
    "io_total": ROOT / "bpmn" / "io_total",
}


def state_name(task):
    return TaskState.get_name(task.state)


def load_case(case_name):
    """Parse a BPMN case directory into a top spec plus subprocess specs."""
    case_dir = CASES[case_name]
    parser = BpmnParser()
    parser.add_bpmn_files_by_glob(str(case_dir / "*.bpmn"))
    spec = parser.get_spec("parent")
    subprocesses = parser.get_subprocess_specs("parent")
    return spec, subprocesses


def new_workflow(case_name):
    spec, subprocesses = load_case(case_name)
    return BpmnWorkflow(spec, subprocesses)


def task_key(task):
    return f"{task.workflow.spec.name}:{task.task_spec.name}"


def all_tasks(workflow):
    """Top workflow tasks plus every active subprocess task."""
    tasks = list(workflow.get_tasks())
    for subworkflow in workflow.subprocesses.values():
        tasks.extend(subworkflow.get_tasks())
    return tasks


def find_task(workflow, spec_name):
    matches = [task for task in all_tasks(workflow) if task.task_spec.name == spec_name]
    if not matches:
        raise KeyError(f"no task with spec name {spec_name!r}")
    return matches[0]


def public_snapshot(workflow):
    """Address-free evidence: task states and deep-copied public task data."""
    snapshot = {}
    for task in all_tasks(workflow):
        snapshot[task_key(task)] = {
            "state": state_name(task),
            "data": deepcopy(task.data),
        }
    return snapshot


def alias_pairs(workflow):
    """LAB-ONLY: which tasks currently share the same rows list object."""
    owners = {}
    for task in all_tasks(workflow):
        if "total" in task.data:
            owners.setdefault(id(task.data["total"]["rows"]), []).append(task_key(task))
    return [names for names in owners.values() if len(names) > 1]


def archive(workflow):
    """JSON serialization, mirroring the one serialized file from the incident."""
    return SERIALIZER.serialize_json(workflow)


def restore(blob):
    """Controlled continuation: rebuild a workflow from the serialized file."""
    return SERIALIZER.deserialize_json(blob)


def archive_and_restore(workflow):
    restored = restore(archive(workflow))
    return restored


def complete_manual(workflow, spec_name):
    task = find_task(workflow, spec_name)
    assert task.state == TaskState.READY, f"{spec_name} is {state_name(task)}"
    assert task.task_spec.manual, f"{spec_name} is not a manual barrier"
    task.run()
    workflow.do_engine_steps()


def start_to_midpoint(case_name):
    """Run until A has swapped the rows while B is still at its entry barrier."""
    workflow = new_workflow(case_name)
    workflow.do_engine_steps()
    complete_manual(workflow, "a_entry")
    return workflow


def return_vendor(workflow, vendor):
    """Release one parked Call Activity so it returns into the parent."""
    complete_manual(workflow, f"{vendor}_park")


def final_report(workflow):
    report = find_task(workflow, "report")
    return {
        "vendors": deepcopy(report.data["report_vendors"]),
        "counts": deepcopy(report.data["report_counts"]),
        "total_rows": deepcopy(report.data["total"]["rows"]),
    }
