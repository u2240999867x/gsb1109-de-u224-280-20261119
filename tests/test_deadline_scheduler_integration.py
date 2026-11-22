"""Integration tests for the optional deadline dispatch strategy.

These tests exercise the real ``JobScheduler`` wiring: the submission callback
records executor acceptance, recovery marks accepted jobs running instead of
resubmitting them, and the scheduler does not resubmit jobs it still has an
active reservation for.  The pure policy/ledger scenario is covered in
``tests/test_deadline_dispatch.py``.
"""

from __future__ import annotations

import threading
from types import SimpleNamespace

from snakemake.scheduling.job_scheduler import JobScheduler

from tests.test_deadline_dispatch import (
    GIB,
    Clock,
    FakeJob,
    job_identity,
    make_controller,
)


def test_executor_acceptance_is_recorded_and_not_resubmitted():
    """Exercise the real submit callback path."""
    controller = make_controller(Clock())
    job = FakeJob("A", 2 * GIB)

    class Wrapper:
        def __init__(self):
            self.dispatch_accept = controller.accept

        _dispatch_accepted = JobScheduler._dispatch_accepted

    wrapper = Wrapper()
    assert controller.select([job]) == [job]
    wrapper._dispatch_accepted(job)
    entry = controller.ledger.latest_for(job_identity(job))
    assert entry.state == "accepted"
    assert entry.accepted_at == 0
    assert controller.select([job]) == []


def test_scheduler_recovery_marks_accepted_jobs_running(tmp_path):
    """Accepted entries become running jobs after a restart, not resubmissions."""
    clock = Clock(0)
    ledger_path = tmp_path / "ledger.json"
    controller = make_controller(clock, path=ledger_path)
    job = FakeJob("A", 2 * GIB)
    controller.select([job])
    controller.accept(job, at=0)

    clock.set(5)
    recovered = make_controller(clock, path=ledger_path)

    class FakeDag:
        def __init__(self):
            self.running = []

        def register_running(self, jobs):
            self.running.extend(jobs)

        def needrun_jobs(self):
            return [job]

    dag = FakeDag()

    class FakeExecutor:
        def dispatch_status(self, pending):
            return {"receipts": [], "not_accepted": []}

    class FakeSched:
        _dispatch_controller = recovered
        _in_flight_attempts = {}
        _lock = threading.Lock()
        running = set()
        global_resources = {"mem_mb": 8 * GIB}
        resources = {"mem_mb": 8 * GIB}
        workflow = SimpleNamespace(
            dag=dag,
            execution_settings=SimpleNamespace(latency_wait=1),
        )
        _executor = FakeExecutor()

        _consume_unresolved = JobScheduler._consume_unresolved
        update_available_resources = JobScheduler.update_available_resources
        _schedule_reevalutation = lambda self, reason, delay: None

    fake = FakeSched()
    fake._consume_unresolved([job])
    assert job in fake.running
    assert job in dag.running
    # Its memory is accounted for while the remote attempt keeps running.
    assert fake.resources["mem_mb"] == 8 * GIB - 2 * GIB
    # And it is never selected for submission again.
    assert recovered.select([job]) == []


def test_workflow_backed_scenario_with_real_jobs(tmp_path):
    """Run the 0..22 s scenario on jobs parsed from a real Snakefile.

    This ensures the helpers used by the deadline dispatcher work on Snakemake's
    real ``Job`` objects: wildcard-based deadlines, attempt-dependent memory
    footprints, and attempts beyond the first one.
    """
    from snakemake import api
    from snakemake.settings import types as settings
    from snakemake.scheduling.deadline import DeadlineRegistry
    from snakemake.scheduling.dispatch import job_attempt, job_identity, job_memory

    workdir = tmp_path
    (workdir / "Snakefile").write_text(
        """
rule all:
    input:
        "r.out", "a.out", "b.out", "c.out", "d.out"

rule A:
    output:
        "a.out"
    priority: 5
    resources:
        mem_mb=lambda w, attempt: (2 if attempt == 1 else 6) * 1024
    shell:
        "touch {output}"

rule R:
    output:
        "r.out"
    priority: 5
    resources:
        mem_mb=3 * 1024
    shell:
        "touch {output}"

rule B:
    output:
        "b.out"
    priority: 5
    resources:
        mem_mb=4 * 1024
    shell:
        "touch {output}"

rule C:
    output:
        "c.out"
    priority: 5
    resources:
        mem_mb=1024
    shell:
        "touch {output}"

rule D:
    output:
        "d.out"
    priority: 5
    resources:
        mem_mb=1024
    shell:
        "touch {output}"
"""
    )
    with api.SnakemakeApi(settings.OutputSettings(quiet={"progress"})) as snakemake_api:
        workflow_api = snakemake_api.workflow(
            resource_settings=settings.ResourceSettings(cores=2),
            snakefile=workdir / "Snakefile",
            workdir=workdir,
        )
        workflow = workflow_api.dag().workflow_api._workflow
        workflow.execution_settings = settings.ExecutionSettings()
        workflow.remote_execution_settings = settings.RemoteExecutionSettings()
        workflow.scheduling_settings = settings.SchedulingSettings(scheduler="greedy")
        workflow.group_settings = settings.GroupSettings()
        workflow._prepare_dag(
            forceall=True, ignore_incomplete=False, lock_warn_only=False
        )
        workflow._build_dag()
        workflow.dag.update_priority()

        jobs = {
            job.rule.name: job
            for job in workflow.dag.needrun_jobs()
            if job.rule.name != "all"
        }
        registry = DeadlineRegistry.from_any(
            ["A@5", "B@6", "C@7", "D@100", "R@200"]
        )
        for name, deadline in [("A", 5.0), ("B", 6.0), ("C", 7.0)]:
            assert registry.deadline_for_job(jobs[name]) == deadline
            assert jobs[name].priority == 5
        assert job_memory(jobs["A"]) == 2 * 1024
        jobs["A"].attempt = 2
        assert job_attempt(jobs["A"]) == 2
        assert job_memory(jobs["A"]) == 6 * 1024
        assert job_identity(jobs["A"]) == f"job:{jobs['A'].uuid()}"
