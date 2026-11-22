"""Structured, reproducible diagnosis scenarios.

Every scenario returns plain-data evidence records. Public records contain only
deep-copied task data and states (the evidence class available for a past
incident). Identity records are marked ``lab_only`` and capture object-alias
facts that cannot be obtained from a serialized file or off-process data.
"""

from copy import deepcopy

from SpiffWorkflow.util.deep_merge import DeepMerge

from . import harness as h

WATCH_KEYS = (
    "parent:call_a",
    "parent:call_b",
    "vendor_a:a_park",
    "vendor_b:b_park",
    "vendor_b:b_entry",
)


def _public_view(workflow):
    snapshot = h.public_snapshot(workflow)
    return {key: deepcopy(snapshot[key]) for key in WATCH_KEYS if key in snapshot}


def _checkpoint(workflow, label):
    return {
        "label": label,
        "public": _public_view(workflow),
        "lab_only_alias_groups": h.alias_pairs(workflow),
    }


def midpoint_evidence(case_name):
    """Compare a live midpoint with a JSON-restored midpoint.

    The midpoint is: A has swapped and set 11; B has not edited yet.
    """
    live = h.start_to_midpoint(case_name)
    blob = h.archive(live)
    restored = h.restore(blob)

    live_cp = _checkpoint(live, "live midpoint (after A swap, B idle)")
    restored_cp = _checkpoint(restored, "restored midpoint (same archive)")

    public_equal = live_cp["public"] == restored_cp["public"]
    live_cross = _has_cross_workflow_alias(live_cp["lab_only_alias_groups"])
    restored_cross = _has_cross_workflow_alias(restored_cp["lab_only_alias_groups"])

    return {
        "case": case_name,
        "archive_bytes": len(blob),
        "live": live_cp,
        "restored": restored_cp,
        "public_data_identical": public_equal,
        "lab_only_live_has_parent_child_alias": live_cross,
        "lab_only_restored_has_parent_child_alias": restored_cross,
    }


def _has_cross_workflow_alias(groups):
    for group in groups:
        workflows = {key.split(":", 1)[0] for key in group}
        if len(workflows) > 1:
            return True
    return False


def driven_run(case_name, restore_at_midpoint, completion_order):
    """Drive both vendors through parked barriers in a fixed completion order."""
    workflow = h.start_to_midpoint(case_name)
    mode = "restored" if restore_at_midpoint else "live"
    if restore_at_midpoint:
        workflow = h.archive_and_restore(workflow)

    checkpoints = [_checkpoint(workflow, f"{mode} midpoint")]

    h.complete_manual(workflow, "b_entry")
    checkpoints.append(_checkpoint(workflow, f"{mode} after B edit, before any return"))

    for vendor in completion_order:
        h.return_vendor(workflow, vendor)
        checkpoints.append(_checkpoint(workflow, f"{mode} after {vendor} return"))

    workflow.do_engine_steps()
    return {
        "case": case_name,
        "mode": mode,
        "completion_order": list(completion_order),
        "checkpoints": checkpoints,
        "final_report": h.final_report(workflow),
    }


def a_row_count_before_return(record):
    """A's count as seen from A's parked task, right after B's edit."""
    cp = record["checkpoints"][1]
    rows = cp["public"]["vendor_a:a_park"]["data"]["total"]["rows"]
    a_row = next(row for row in rows if row["vendor"] == "A")
    return a_row["count"]


def return_boundary_evidence():
    """Compare Call Activity return semantics for the two IO declarations."""
    result = {}
    for case_name in ("no_io", "io_total"):
        workflow = h.start_to_midpoint(case_name)
        h.return_vendor(workflow, "a")
        call_task = h.find_task(workflow, "call_a")
        end_task = h.find_task(workflow, "a_end")
        result[case_name] = {
            "rows_object_shared_across_return": (call_task.data["total"]["rows"] is end_task.data["total"]["rows"]),
            "total_object_shared_across_return": (call_task.data["total"] is end_task.data["total"]),
            "parent_call_keys": sorted(call_task.data.keys()),
            "audit_tag_preserved": "audit_tag" in call_task.data,
        }
    return result


def merge_truncation_evidence():
    """Isolate the join's list-trimming behaviour without a workflow."""
    two_rows = [
        {"vendor": "B", "count": 0},
        {"vendor": "A", "count": 11},
    ]
    one_row = [{"vendor": "B", "count": 0}]

    long_target = {"rows": deepcopy(two_rows)}
    DeepMerge.merge(long_target, {"rows": deepcopy(one_row)})

    short_target = {"rows": deepcopy(one_row)}
    DeepMerge.merge(short_target, {"rows": deepcopy(two_rows)})

    return {
        "two_row_target_plus_one_row_incoming": long_target["rows"],
        "one_row_target_plus_two_row_incoming": short_target["rows"],
        "trim_rule": "merge_array executes del a[len(b):], so a shorter incoming list truncates the target",
    }


def full_matrix():
    """Collect every scenario used by the demo report and the test suite."""
    matrix = {
        "midpoint": {case: midpoint_evidence(case) for case in h.CASES},
        "same_order_ab": {
            f"{case}/{mode}": driven_run(case, restored, ("a", "b"))
            for case in h.CASES
            for mode, restored in (("live", False), ("restored", True))
        },
        "reverse_order_ba": {
            f"no_io/{mode}": driven_run("no_io", restored, ("b", "a")) for mode, restored in (("live", False), ("restored", True))
        },
        "return_boundary": return_boundary_evidence(),
        "merge_truncation": merge_truncation_evidence(),
    }
    return matrix
