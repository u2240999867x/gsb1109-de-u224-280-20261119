"""Tests for the rule selection explanation shown in dry-runs.

The explanation is derived from the decisions taken while the real DAG is
built.  Printing it must not evaluate input functions again, and ruleorder
pruning must happen without evaluating the pruned candidates.
"""

import json
import shutil
from pathlib import Path

from snakemake import api
from snakemake.exceptions import AmbiguousRuleException
from snakemake.settings import types as settings

HERE = Path(__file__).parent
FIXTURES = HERE / "test_rule_selection_explain"


def run_explanation(fixture: str, tmp_path: Path):
    src = FIXTURES / fixture
    workdir = tmp_path / fixture
    shutil.copytree(src, workdir)
    with api.SnakemakeApi(settings.OutputSettings()) as snakemake_api:
        workflow_api = snakemake_api.workflow(
            resource_settings=settings.ResourceSettings(cores=1),
            snakefile=workdir / "Snakefile",
            workdir=workdir,
        )
        dag_api = workflow_api.dag(settings.DAGSettings())
        try:
            report = dag_api.rule_selection_report()
        except Exception as ex:
            return workdir, None, ex, workflow_api
    return workdir, report, None, workflow_api


def candidate(report, target, rule):
    for trace in report:
        if trace["target"] != target:
            continue
        for entry in trace["candidates"]:
            if entry["rule"] == rule:
                return trace, entry
    raise AssertionError(f"No candidate {rule} for target {target} in {report}")


def test_success_reports_all_rejection_reasons(tmp_path):
    _, report, error, _ = run_explanation("success", tmp_path)
    assert error is None

    trace, first = candidate(report, "archive/final.txt", "first")
    assert trace["outcome"] == "selected"
    assert trace["selected_rule"] == "third"
    assert first["status"] == "rejected"
    assert first["reason"] == "wildcard-constraint"
    assert "does not match constraint" in first["detail"]
    assert first["evaluated"] is False

    _, second = candidate(report, "archive/final.txt", "second")
    assert second["status"] == "rejected"
    assert second["reason"] == "input-function"
    assert "intentional input function failure" in second["detail"]
    assert second["evaluated"] is True

    _, fourth = candidate(report, "archive/final.txt", "fourth")
    assert fourth["status"] == "not evaluated"
    assert fourth["reason"] == "ruleorder"
    assert fourth["evaluated"] is False

    _, third = candidate(report, "archive/final.txt", "third")
    assert third["status"] == "selected"
    assert third["evaluated"] is True


def test_ambiguity_is_real_without_ruleorder(tmp_path):
    _, report, error, _ = run_explanation("ambiguity", tmp_path)
    assert isinstance(error, AmbiguousRuleException)
    # The report was printed before the exception and can still be obtained
    # through the recorder in a failure-aware entry point.
    assert "third" in str(error)
    assert "fourth" in str(error)


def test_ruleorder_prunes_without_evaluation(tmp_path):
    workdir, report, error, _ = run_explanation("pruned", tmp_path)
    assert error is None

    trace, fourth = candidate(report, "archive/final.txt", "fourth")
    assert trace["selected_rule"] == "third"
    assert fourth["status"] == "not evaluated"
    assert fourth["reason"] == "ruleorder"
    assert fourth["evaluated"] is False

    # No claim about missing input or availability of the pruned candidate.
    assert "missing" not in fourth["detail"].lower()
    assert "available" not in fourth["detail"].lower()

    # Evaluating the pruned branch would have created this file.
    assert not (workdir / "audit.flag").exists()


def test_nested_candidate_failure_selects_side_branch(tmp_path):
    _, report, error, _ = run_explanation("nested", tmp_path)
    assert error is None

    root, high = candidate(report, "archive/final.txt", "high")
    assert root["outcome"] == "selected"
    assert root["selected_rule"] == "side"
    assert high["status"] == "rejected"
    assert high["reason"] == "dependency-failed"
    assert "sub/mid.txt" in high["detail"]

    subtrace, deep = candidate(report, "sub/mid.txt", "sub_deep")
    assert subtrace["outcome"] == "failed"
    assert subtrace["path"] == ["archive/final.txt", "sub/mid.txt"]
    assert deep["status"] == "rejected"
    assert deep["reason"] == "missing-input"
    assert "deep/missing.txt" in deep["detail"]

    _, excluded = candidate(report, "sub/mid.txt", "sub_excluded")
    assert excluded["reason"] == "wildcard-constraint"
    assert excluded["evaluated"] is False

    _, side = candidate(report, "archive/final.txt", "side")
    assert side["status"] == "selected"


def test_report_does_not_reevaluate_input_functions(tmp_path):
    workdir, report, error, workflow_api = run_explanation("count_calls", tmp_path)
    assert error is None
    # Only the selected branch may have been evaluated. The pruned branch uses
    # a textually identical input function and must not have run.
    with open(workdir / "calls.json") as f:
        first_counts = json.load(f)
    assert first_counts == {"a": 1}

    # Rendering the report again from the recorded decisions must not evaluate
    # any input function, hence the counters stay unchanged.
    again = workflow_api._workflow.dag.rule_selection.as_dict()
    with open(workdir / "calls.json") as f:
        second_counts = json.load(f)
    assert second_counts == {"a": 1}

    _, fourth = candidate(again, "archive/one.txt", "fourth")
    assert fourth["status"] == "not evaluated"
    assert fourth["evaluated"] is False
