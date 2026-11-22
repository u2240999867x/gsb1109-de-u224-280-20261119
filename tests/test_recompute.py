"""Tests for in-memory recompute planning on an already loaded workflow."""

import json
import shutil
import textwrap

import pytest

from snakemake import api
from snakemake.recompute import (
    JobPlan,
    RecomputeDecision,
    RecomputePlan,
    RecomputePlanner,
)
from snakemake.scheduling.greedy import SchedulerSettings as GreedySchedulerSettings
from snakemake.settings import types as settings
from snakemake.settings.types import SchedulingSettings


CFG_OLD = {"switch": True, "threshold": 10}
CFG_NEW = {"switch": True, "threshold": 20}

CP_OLD = {"threshold": 1, "route": "alpha"}
CP_NEW = {"threshold": 2, "route": "beta"}

PARAMS_SNAKEFILE = textwrap.dedent(
    '''
    from pathlib import Path
    import json

    CALLS = Path("calls.json")


    def _bump(name):
        data = json.loads(CALLS.read_text()) if CALLS.exists() else {}
        data[name] = data.get(name, 0) + 1
        CALLS.write_text(json.dumps(data))


    def a_params(wildcards):
        _bump("A")
        return config["threshold"] if config.get("switch") else 0


    def b_params(wildcards):
        _bump("B")
        return "fixed"


    def c_params(wildcards):
        _bump("C")
        return "downstream"


    def d_params(wildcards):
        _bump("D")
        return "high" if int(config["threshold"]) >= 10 else "low"


    def all_params(wildcards):
        _bump("all")
        return "x"


    INP = ["a.txt", "b.txt", "c.txt", "d.txt"]


    rule all:
        input: INP
        params: marker=all_params
        shell: "echo finished > summary.txt"


    rule A:
        output: "a.txt"
        params: threshold=a_params
        shell: "echo A={params.threshold} > {output}"


    rule B:
        output: "b.txt"
        params: constant=b_params
        shell: "echo B={params.constant} > {output}"


    rule C:
        input: "a.txt"
        output: "c.txt"
        params: mode=c_params
        shell: "cat {input} > {output}"


    rule D:
        input: "b.txt"
        output: "d.txt"
        params: threshold=d_params
        shell: "echo D={params.threshold} > {output}"
    '''
)

CHECKPOINT_SNAKEFILE = textwrap.dedent(
    '''
    from pathlib import Path
    import json

    CALLS = Path("calls.json")


    def _bump(name):
        data = json.loads(CALLS.read_text()) if CALLS.exists() else {}
        data[name] = data.get(name, 0) + 1
        CALLS.write_text(json.dumps(data))


    def a_params(wildcards):
        _bump("A")
        return int(config.get("threshold", 0))


    def b_params(wildcards):
        _bump("B")
        return "fixed"


    def cp_params(wildcards):
        _bump("cp")
        return config.get("route", "alpha")


    def late(wildcards):
        _bump("late")
        choice = checkpoints.choose.get().output[0]
        route = Path(choice).read_text().strip()
        return ["alpha.txt"] if route == "alpha" else ["beta.txt"]


    rule all:
        input: "final.txt", "choice.txt", "b.txt"


    rule A:
        output: "a.txt"
        params: threshold=a_params
        shell: "echo A={params.threshold} > {output}"


    rule B:
        output: "b.txt"
        params: constant=b_params
        shell: "echo B={params.constant} > {output}"


    checkpoint choose:
        input: "a.txt"
        output: "choice.txt"
        params: route=cp_params
        shell: "echo {params.route} > {output}"


    rule alpha:
        input: "a.txt"
        output: "alpha.txt"
        shell: "cat {input} > {output}"


    rule beta:
        input: "b.txt"
        output: "beta.txt"
        shell: "cat {input} > {output}"


    rule final:
        input: late
        output: "final.txt"
        shell: "cat {input} > {output}"
    '''
)


@pytest.fixture
def plan_workdir(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    (workdir / "Snakefile").write_text(PARAMS_SNAKEFILE)
    return workdir


@pytest.fixture
def checkpoint_workdir(tmp_path):
    workdir = tmp_path / "checkpoint"
    workdir.mkdir()
    (workdir / "Snakefile").write_text(CHECKPOINT_SNAKEFILE)
    return workdir


def _prepare(workdir, config, name=None, forceall=False):
    if name is not None:
        target = workdir.parent / name
        if not target.exists():
            shutil.copytree(workdir, target)
        workdir = target
    api_obj = api.SnakemakeApi(settings.OutputSettings(verbose=False))
    snakemake_api = api_obj.__enter__()
    workflow_api = snakemake_api.workflow(
        resource_settings=settings.ResourceSettings(cores=1),
        config_settings=settings.ConfigSettings(config=config),
        storage_settings=settings.StorageSettings(),
        workflow_settings=settings.WorkflowSettings(),
        deployment_settings=settings.DeploymentSettings(),
        snakefile=workdir / "Snakefile",
        workdir=workdir,
    )
    dag_api = workflow_api.dag(
        dag_settings=settings.DAGSettings(forceall=forceall)
    )
    workflow = workflow_api._workflow
    workflow._prepare_dag(
        forceall=forceall, ignore_incomplete=False, lock_warn_only=True
    )
    workflow._build_dag()
    return api_obj, workflow, dag_api


def _execute(dag_api):
    dag_api.execute_workflow(
        scheduling_settings=SchedulingSettings(scheduler="greedy"),
        scheduler_settings=GreedySchedulerSettings(),
        greedy_scheduler_settings=GreedySchedulerSettings(),
    )


def _calls(workdir):
    path = workdir / "calls.json"
    return json.loads(path.read_text()) if path.exists() else {}


def test_four_queries_reuse_loaded_objects(plan_workdir):
    api_obj, workflow, dag_api = _prepare(plan_workdir, CFG_OLD)
    try:
        _execute(dag_api)
    finally:
        api_obj.__exit__(None, None, None)

    api_obj, workflow, dag_api = _prepare(plan_workdir, CFG_OLD)
    try:
        jobs = {job.rule.name: job for job in workflow.dag.jobs}
        job_a, job_b, job_c, job_d = (
            jobs["A"],
            jobs["B"],
            jobs["C"],
            jobs["D"],
        )
        calls_before = _calls(plan_workdir)
        outputs_before = {
            name: (plan_workdir / name).read_text()
            for name in ("a.txt", "b.txt", "c.txt", "d.txt")
        }

        planner = workflow.recompute_planner()
        plans = [planner.plan(cfg) for cfg in (CFG_OLD, CFG_NEW, CFG_OLD, CFG_NEW)]

        # Repeated identical configs hit the plan cache and are the same object.
        assert plans[0] is plans[2]
        assert plans[1] is plans[3]

        old, new = plans[0], plans[1]

        # A is affected by the config change and recomputes its parameters.
        assert old.for_job(job_a).decision is RecomputeDecision.REUSE
        assert new.for_job(job_a).decision is RecomputeDecision.RECOMPUTE
        assert new.for_job(job_a).params == {"threshold": 20}

        # B cannot observe config and keeps its already proven parameters.
        assert new.for_job(job_b).decision is RecomputeDecision.REUSE
        assert new.for_job(job_b).params == {"constant": "fixed"}

        # C changes only through dependency propagation.
        assert new.for_job(job_c).decision is RecomputeDecision.PROPAGATED

        # D normalizes 10 and 20 to the same effective value, so its proven
        # parameters remain valid.
        assert new.for_job(job_d).decision is RecomputeDecision.REUSE
        assert new.for_job(job_d).params == {"threshold": "high"}

        # Prove the decisions come from real, counted callbacks and persisted
        # metadata, not just from DAG shape.
        assert new.for_job(job_a).recorded_params is not None
        assert "threshold" in new.for_job(job_a).config_dependencies
        assert "threshold" in new.for_job(job_d).config_dependencies
        assert not new.for_job(job_b).config_dependencies
        assert new.for_job(job_d).params == old.for_job(job_d).params

        # Previewing executes no jobs.  Config-sensitive callbacks are
        # evaluated once for the new values (then cached); callbacks that
        # cannot observe config are never re-executed.
        calls_after = _calls(plan_workdir)
        for name in ("B", "all"):
            assert calls_after[name] == calls_before[name]
        for name in ("A", "D"):
            assert calls_after[name] - calls_before.get(name, 0) <= 1

        # Previewing does not run any cleaning job or touch its outputs.
        outputs_after = {
            name: (plan_workdir / name).read_text()
            for name in ("a.txt", "b.txt", "c.txt", "d.txt")
        }
        assert outputs_after == outputs_before

        # Plans are immutable snapshots; querying the old config again does not
        # rewrite the previously returned plan.
        assert old.for_job(job_a).params == {"threshold": 10}
        assert old.for_job(job_a).decision is RecomputeDecision.REUSE
        assert planner.plan(CFG_OLD) is old
    finally:
        api_obj.__exit__(None, None, None)


def test_checkpoint_route_converges_after_real_run(checkpoint_workdir):
    api_obj, workflow, dag_api = _prepare(checkpoint_workdir, CP_OLD)
    try:
        _execute(dag_api)
    finally:
        api_obj.__exit__(None, None, None)

    api_obj, workflow, dag_api = _prepare(checkpoint_workdir, CP_OLD)
    try:
        jobs = {job.rule.name: job for job in workflow.dag.jobs}
        planner = workflow.recompute_planner()
        old = planner.plan(CP_OLD)
        stale = planner.plan(CP_NEW)

        # The old checkpoint list cannot certify the new route.  The checkpoint
        # itself is a candidate and its dependent region lacks evidence.
        assert stale.for_job(jobs["choose"]).decision is RecomputeDecision.RECOMPUTE
        assert stale.has_blocked
        assert stale.for_job(jobs["final"]).decision is (
            RecomputeDecision.INSUFFICIENT_EVIDENCE
        )
        assert any(
            entry.decision is RecomputeDecision.INSUFFICIENT_EVIDENCE
            for entry in stale.entries.values()
        )

        # Keep a reference to an unaffected job to prove it survives later DAG
        # expansion on the same planning object.
        unaffected_job = jobs["B"]
        unaffected_params = old.for_job(unaffected_job).params

        # Complete a real checkpoint under the new configuration in the same
        # workdir, then plan again on the same planner object.
        api2, _, dag_api2 = _prepare(checkpoint_workdir, CP_NEW)
        try:
            _execute(dag_api2)
        finally:
            api2.__exit__(None, None, None)

        converged = planner.plan(CP_NEW)

        assert converged.for_job(jobs["choose"]).decision is RecomputeDecision.REUSE
        assert not converged.has_blocked
        assert (checkpoint_workdir / "choice.txt").read_text().strip() == "beta"
        assert planner.completed_checkpoints

        # The unaffected job and its proven parameters are preserved, and any
        # replacement job created by the checkpoint refresh is still findable
        # through its outputs on the same planner.
        assert unaffected_job in converged.entries
        assert converged.for_job(unaffected_job).params == unaffected_params
        for output in workflow.dag.job_cache:
            rule, product = output
            if rule.name == "final":
                assert converged.for_output(product).rule == "final"

        # The previously returned old plan stays queryable and unchanged.
        assert planner.plan(CP_OLD) is old
        assert old.for_job(jobs["choose"]).decision is RecomputeDecision.REUSE
        assert any(
            entry.decision is RecomputeDecision.INSUFFICIENT_EVIDENCE
            for entry in stale.entries.values()
        )
    finally:
        api_obj.__exit__(None, None, None)


def test_plan_api_shapes(plan_workdir):
    api_obj, workflow, dag_api = _prepare(plan_workdir, CFG_OLD)
    try:
        plan = dag_api.plan_recompute(CFG_NEW)
        assert isinstance(plan, RecomputePlan)
        assert isinstance(planner := workflow.recompute_planner(), RecomputePlanner)
        assert planner.plan(CFG_NEW) is plan
        assert all(isinstance(entry, JobPlan) for entry in plan.entries.values())
        assert set(plan.recompute_jobs).issubset(set(plan.needrun_jobs))
        assert isinstance(plan.summary(), str)

        # Job-level lookups follow the same loaded objects.
        for entry in plan.entries.values():
            assert plan.for_job(entry.job).rule == entry.rule
            assert entry.job.plan_recompute(CFG_NEW).rule == entry.rule
    finally:
        api_obj.__exit__(None, None, None)


def test_executed_plan_matches_full_recompute(plan_workdir):
    api_obj, workflow, dag_api = _prepare(plan_workdir, CFG_OLD)
    try:
        _execute(dag_api)
    finally:
        api_obj.__exit__(None, None, None)

    api_obj, workflow, dag_api = _prepare(plan_workdir, CFG_OLD)
    try:
        planner = workflow.recompute_planner()
        plan = planner.plan(CFG_NEW)
        assert plan.recompute_jobs or plan.propagated_jobs
        before_preview = {
            name: (plan_workdir / name).read_text()
            for name in ("a.txt", "b.txt", "c.txt", "d.txt")
        }
        planner.plan(CFG_NEW)
        after_preview = {
            name: (plan_workdir / name).read_text()
            for name in ("a.txt", "b.txt", "c.txt", "d.txt")
        }
        assert before_preview == after_preview

        # Execute only the planned work under the new configuration.
        api2, _, dag_api2 = _prepare(plan_workdir, CFG_NEW)
        try:
            _execute(dag_api2)
        finally:
            api2.__exit__(None, None, None)
        incremental = {
            name: (plan_workdir / name).read_text()
            for name in ("a.txt", "b.txt", "c.txt", "d.txt")
        }

        # Force a full recompute of the same configuration and compare.
        api3, _, dag_api3 = _prepare(
            plan_workdir, CFG_NEW, name="work-forceall", forceall=True
        )
        try:
            _execute(dag_api3)
        finally:
            api3.__exit__(None, None, None)
        full = {
            name: (plan_workdir / name).read_text()
            for name in ("a.txt", "b.txt", "c.txt", "d.txt")
        }
        assert incremental == full
    finally:
        api_obj.__exit__(None, None, None)
