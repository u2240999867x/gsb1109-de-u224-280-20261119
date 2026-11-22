"""End-to-end simulation of deadline dispatch across a scheduler restart.

Scenario (all times in seconds, memory in MiB; 8 GiB = 8192 MiB):

* 0: submit R (3 GiB) and the first attempt of A (2 GiB). A has the earliest
  deadline.
* 3: A's first attempt fails.
* 11: A's second attempt needs 6 GiB but does not fit next to R (3 GiB).
  B (4 GiB) and C (1 GiB) are submitted instead.
* 11+: the execution end accepted B, but the scheduler restart happens before
  the local ledger records that acceptance. C was not accepted.
* 12: recovery. B's original acceptance time is retained and B is not
  resubmitted. C is accepted at 12.
* R and B finish; C keeps running.
* 21: A's second attempt is submitted. D (1 GiB, later deadline) waits.
* 22: D is submitted once a submission permit leaves the 10 s window.
* After A's second attempt is accepted, the first failure and B's completion
  are replayed; neither frees current memory nor triggers a third attempt.

The test drives the production :class:`DispatchController` (deadline ordering,
resource admission, permits) together with the production
:class:`DispatchLedger` and a deterministic :class:`SimulatedExecutionEnd`.
"""

from __future__ import annotations

import pytest

from snakemake.scheduling.deadline import Deadline, DeadlineRegistry
from snakemake.scheduling.dispatch import (
    DispatchController,
    DispatchLedger,
    job_identity,
)
from snakemake.scheduling.greedy import SchedulerSettings as GreedySchedulerSettings
from snakemake.scheduling.simulation import SimulatedExecutionEnd

GIB = 1024


class Clock:
    def __init__(self, start: float = 0.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def set(self, value: float) -> None:
        self.now = value


class FakeRule:
    def __init__(self, name: str):
        self.name = name


class FakeJob:
    """Minimal job implementing the scheduler interfaces used in production."""

    def __init__(
        self,
        rule: str,
        mem_mb: int,
        wildcards: dict | None = None,
        attempt: int = 1,
        priority: int = 0,
        deadline: float | None = None,
    ):
        self.rule = FakeRule(rule)
        self.mem_mb = mem_mb
        self.wildcards_dict = wildcards or {}
        self.attempt = attempt
        self.priority = priority
        self.deadline = deadline

    @property
    def scheduler_resources(self):
        return {"mem_mb": self.mem_mb, "_job_count": 1}

    def is_group(self) -> bool:
        return False

    def uuid(self) -> str:
        wildcards = ",".join(f"{k}={v}" for k, v in sorted(self.wildcards_dict.items()))
        return f"{self.rule.name}:{wildcards}"


class FakeGroupJob:
    def __init__(self, jobs, groupid="g"):
        self.jobs = tuple(jobs)
        self.groupid = groupid
        self.attempt = 1

    @property
    def scheduler_resources(self):
        return {"mem_mb": sum(job.mem_mb for job in self.jobs), "_job_count": 1}

    @property
    def priority(self):
        return max(job.priority for job in self.jobs)

    def is_group(self) -> bool:
        return True


DEADLINES = [
    Deadline(rule="A", at=5),
    Deadline(rule="B", at=6),
    Deadline(rule="C", at=7),
    Deadline(rule="D", at=100),
    Deadline(rule="R", at=200),
]


def make_controller(clock, path=None, total_mem: int = 8 * GIB):
    return DispatchController(
        total_resources={"mem_mb": total_mem},
        ledger=DispatchLedger(
            path=path,
            max_jobs_per_window=2,
            window=10.0,
            clock=clock,
        ),
        deadline_registry=DeadlineRegistry(DEADLINES),
        clock=clock,
    )


def accept(controller, end, job, now):
    """Record acceptance locally and at the execution end."""
    key = job_identity(job)
    controller.accept(job, at=now)
    entry = controller.ledger.latest_for(key)
    end.submit(
        key=key,
        attempt=job.attempt,
        mem_mb=job.mem_mb,
        now=now,
        transaction_id=entry.transaction_id,
        accept=True,
    )


def rule_of(key: str) -> str:
    # job:RULE:wildcards or group:...
    return key.split(":")[1]


def test_deadline_dispatch_scenario_across_restart(tmp_path):
    ledger_path = tmp_path / "ledger.json"
    clock = Clock(0)
    controller = make_controller(clock, path=ledger_path)
    end = SimulatedExecutionEnd(
        total_mem_mb=8 * GIB,
        clock=clock,
        max_submissions_per_window=2,
        window=10.0,
    )

    R = FakeJob("R", 3 * GIB)
    A1 = FakeJob("A", 2 * GIB, wildcards={"sample": "a"})
    B = FakeJob("B", 4 * GIB, wildcards={"sample": "b"})
    C = FakeJob("C", 1 * GIB, wildcards={"sample": "c"})
    D = FakeJob("D", 1 * GIB, wildcards={"sample": "d"})

    # t=0: both fit; order is by deadline (A before R).
    assert controller.select([R, A1]) == [A1, R]
    accept(controller, end, A1, 0)
    accept(controller, end, R, 0)
    assert controller.memory_in_use == 5 * GIB
    assert controller.ledger.available_permits(0) == 0

    # t=3: A's first attempt fails; memory is freed.
    assert controller.fail(A1, at=3) is True
    end.finish(job_identity(A1), now=3, failed=True)
    assert controller.memory_in_use == 3 * GIB
    assert end.memory_in_use() == 3 * GIB

    # t=11: A's second attempt (6 GiB) does not fit next to R; B and C do.
    clock.set(11)
    A2 = FakeJob("A", 6 * GIB, wildcards={"sample": "a"}, attempt=2)
    assert controller.select([A2, B, C]) == [B, C]
    # The executor accepts B...
    b_entry_before = controller.ledger.latest_for(job_identity(B))
    end.submit(
        key=job_identity(B),
        attempt=B.attempt,
        mem_mb=B.mem_mb,
        now=11,
        transaction_id=b_entry_before.transaction_id,
        accept=True,
    )
    # ...but the scheduler restarts before the local acceptance callback ran;
    # C was planned but not accepted.
    assert controller.ledger.active_for(job_identity(B)).state == "reserved"

    # t=12: a new scheduler process loads the persisted ledger.
    clock.set(12)
    recovered = make_controller(clock, path=ledger_path)
    pending = recovered.ledger.pending_entries()
    assert {(e.key, e.attempt) for e in pending} == {
        (job_identity(B), 1),
        (job_identity(C), 1),
    }
    status = end.status_view(
        pending, explicitly_planned=[(job_identity(C), C.attempt)]
    )
    # The receipt contains the attempt number and the original acceptance time.
    assert [(r.key, r.attempt, r.accepted_at) for r in status.receipts] == [
        (job_identity(B), 1, 11)
    ]
    # The execution end explicitly states that C has not been accepted.
    assert status.not_accepted == [(job_identity(C), 1)]

    recovered.reconcile(status, now=12)
    b_entry = recovered.ledger.latest_for(job_identity(B))
    c_entry = recovered.ledger.latest_for(job_identity(C))
    assert b_entry.state == "accepted" and b_entry.accepted_at == 11
    assert c_entry.state == "released"

    # B must not be resubmitted; C joins at 12.
    assert recovered.select([A2, B, C, D]) == [C]
    accept(recovered, end, C, 12)
    assert recovered.ledger.latest_for(job_identity(B)).accepted_at == 11
    assert recovered.ledger.latest_for(job_identity(C)).accepted_at == 12
    # Exactly one real submission happened at t=12.
    assert end.submission_times.count(12) == 1

    # R and B finish legally; C is still running.
    assert recovered.finish(R, at=13) is True
    assert recovered.finish(B, at=14) is True
    end.finish(job_identity(R), now=13)
    end.finish(job_identity(B), now=14)
    end.collect()
    assert recovered.memory_in_use == 1 * GIB
    assert end.memory_in_use() == 1 * GIB

    # t=21: A's second attempt fits (6 GiB) and is selected. D has a later
    # deadline and would fit memory-wise, but only one permit has left the
    # window (B at 11); C at 12 still holds the second one.
    clock.set(21)
    assert recovered.select([D, A2]) == [A2]
    accept(recovered, end, A2, 21)
    assert recovered.memory_in_use == 7 * GIB
    assert recovered.ledger.available_permits(21) == 0

    # Replay the first failure and B's completion.
    assert recovered.should_process(A1) is False
    assert recovered.fail(A1, at=22) is False
    assert recovered.finish(B, at=22) is False
    assert recovered.should_process(B) is False
    assert recovered.memory_in_use == 7 * GIB
    # No third attempt of A was planned.
    assert recovered.ledger.current_attempt(job_identity(A2)) == 2
    assert len(recovered.ledger.entries_for(job_identity(A2))) == 2
    assert recovered.select([FakeJob("A", 6 * GIB, {"sample": "a"}, attempt=3)]) == []

    # t=22: B's permit leaves the window; D joins.
    clock.set(22)
    assert recovered.select([D]) == [D]
    accept(recovered, end, D, 22)
    assert recovered.memory_in_use == 8 * GIB

    # Acceptance order with original executor acceptance times.
    order = [
        (rule_of(entry.key), entry.attempt, entry.accepted_at)
        for entry in recovered.ledger.acceptance_order()
    ]
    assert order == [
        ("A", 1, 0),
        ("R", 1, 0),
        ("B", 1, 11),
        ("C", 1, 12),
        ("A", 2, 21),
        ("D", 1, 22),
    ]

    # At most two real submissions in any 10 s window.
    for submission in end.submission_times:
        window = [t for t in end.submission_times if submission - 10 < t <= submission]
        assert len(window) <= 2

    # Per-attempt memory footprints at the execution end.
    assert {
        (rule_of(a.key), a.attempt): a.mem_mb for a in end.accepted_attempts()
    } == {
        ("A", 1): 2 * GIB,
        ("R", 1): 3 * GIB,
        ("B", 1): 4 * GIB,
        ("C", 1): 1 * GIB,
        ("A", 2): 6 * GIB,
        ("D", 1): 1 * GIB,
    }
    # No attempt was accepted twice.
    assert len(end.accepted_attempts()) == len(
        {(a.key, a.attempt) for a in end.accepted_attempts()}
    )

    # All reservations are resolved; within the 10 s window ending at 22, A2
    # (21) and D (22) are the consumed permits (C at 12 lies on the left
    # boundary and no longer counts).
    snapshot = recovered.ledger.permit_snapshot(now=22)
    assert snapshot["reserved"] == []
    assert [rule_of(item[0]) for item in snapshot["accepted"]] == ["A", "D"]
    assert snapshot["held"] == 2


def test_unknown_submission_keeps_reservation():
    """If the execution end cannot find a planned attempt, keep the quota."""
    clock = Clock(0)
    controller = make_controller(clock)
    A = FakeJob("A", 2 * GIB)
    R = FakeJob("R", 3 * GIB)
    controller.select([A, R])
    controller.accept(A, at=0)
    # R was planned but never accepted locally; the execution end has no record.
    controller.reconcile({"receipts": [], "not_accepted": []}, now=1)
    entry = controller.ledger.latest_for(job_identity(R))
    assert entry.state == "unknown"
    # Memory stays reserved, no resubmission, and no permit is freed.
    assert controller.memory_in_use == 5 * GIB
    assert controller.select([FakeJob("R", 3 * GIB)]) == []
    assert controller.ledger.available_permits(1) == 0

    # Evidence arrives later; the reservation becomes accepted with the
    # executor's original acceptance time.
    controller.reconcile(
        {
            "receipts": [
                {
                    "key": job_identity(R),
                    "attempt": 1,
                    "accepted_at": 0,
                    "state": "running",
                }
            ]
        },
        now=20,
    )
    entry = controller.ledger.latest_for(job_identity(R))
    assert entry.state == "accepted"
    assert entry.accepted_at == 0


def test_group_uses_earliest_member_deadline():
    from snakemake.scheduling.deadline import dispatch_sort_key

    early = FakeJob("B", 1 * GIB, {"sample": "x"})
    late = FakeJob("A", 1 * GIB, {"sample": "y"})
    group = FakeGroupJob([early, late])
    registry = DeadlineRegistry(
        [Deadline(rule="A", at=500), Deadline(rule="B", at=100)]
    )
    assert registry.deadline_for_job(group) == 100
    # Groups sort by their earliest member deadline, not by the member rule name.
    key = dispatch_sort_key(group, deadline_registry=registry)
    assert key[3] == 100


def test_missing_deadline_sorts_after_deadline_same_priority():
    from snakemake.scheduling.deadline import dispatch_sort_key

    with_deadline = FakeJob("Z", 1 * GIB)
    without = FakeJob("A", 1 * GIB)
    registry = DeadlineRegistry([Deadline(rule="Z", at=1234)])
    ordered = sorted(
        [without, with_deadline],
        key=lambda job: dispatch_sort_key(job, deadline_registry=registry),
    )
    assert ordered == [with_deadline, without]


def test_expired_deadline_is_still_dispatched():
    from snakemake.scheduling.deadline import Scheduler, dispatch_sort_key
    import logging

    class Dag:
        workflow = None

    job = FakeJob("A", 1 * GIB)
    registry = DeadlineRegistry([Deadline(rule="A", at=1)])  # long expired
    sort_key = dispatch_sort_key(job, deadline_registry=registry)
    scheduler = Scheduler(Dag(), None, logging.getLogger())
    assert scheduler.select_jobs([job], [job], {"mem_mb": 8 * GIB}, {}) == [job]
    assert sort_key[3] == 1  # the expiry time is used for ordering, not a filter


def test_job_without_deadline_sorts_after_deadline_at_same_priority_even_expired():
    from snakemake.scheduling.deadline import dispatch_sort_key

    expired = FakeJob("Z", 1 * GIB)
    missing = FakeJob("A", 1 * GIB)
    registry = DeadlineRegistry([Deadline(rule="Z", at=1)])
    ordered = sorted(
        [missing, expired],
        key=lambda job: dispatch_sort_key(job, deadline_registry=registry),
    )
    assert ordered == [expired, missing]


def test_higher_priority_comes_before_earlier_deadline():
    from snakemake.scheduling.deadline import dispatch_sort_key

    high_priority = FakeJob("Z", 1 * GIB, priority=10)
    early_deadline = FakeJob("A", 1 * GIB, priority=1)
    registry = DeadlineRegistry([Deadline(rule="A", at=1)])
    ordered = sorted(
        [early_deadline, high_priority],
        key=lambda job: dispatch_sort_key(job, deadline_registry=registry),
    )
    assert ordered == [high_priority, early_deadline]


def test_same_deadline_breaks_ties_by_rule_then_sorted_wildcards():
    from snakemake.scheduling.deadline import dispatch_sort_key

    registry = DeadlineRegistry([Deadline(rule="A", at=5), Deadline(rule="B", at=5)])
    b = FakeJob("B", 1 * GIB, {"sample": "x"})
    a_second = FakeJob("A", 1 * GIB, {"sample": "b", "region": "z"})
    a_first = FakeJob("A", 1 * GIB, {"sample": "a", "region": "z"})
    ordered = sorted(
        [b, a_second, a_first],
        key=lambda job: dispatch_sort_key(job, deadline_registry=registry),
    )
    assert ordered == [a_first, a_second, b]
    # sorted wildcard values are ("z", "a") and ("z", "b") respectively
    assert dispatch_sort_key(a_first, deadline_registry=registry)[5] == ("z", "a")


def test_not_ready_or_not_fitting_jobs_consume_nothing():
    clock = Clock(0)
    controller = make_controller(clock, total_mem=5 * GIB)
    big = FakeJob("A", 6 * GIB)
    small = FakeJob("C", 1 * GIB)
    # Only small fits; big must not consume a permit or memory.
    assert controller.select([big, small]) == [small]
    assert controller.ledger.latest_for(job_identity(big)) is None
    assert controller.ledger.latest_for(job_identity(small)).mem_mb == 1 * GIB
    # A later selection with enough memory can admit a big job.
    controller.ledger.finish(job_identity(small))
    fits_later = FakeJob("R", 5 * GIB)
    assert controller.select([fits_later]) == [fits_later]
    assert controller.ledger.latest_for(job_identity(big)) is None


def test_restart_with_custom_executor_without_recovery_keeps_quota(tmp_path):
    """An executor without receipts leaves reservations untouched."""
    ledger_path = tmp_path / "ledger.json"
    clock = Clock(0)
    first = make_controller(clock, path=ledger_path)
    A = FakeJob("A", 2 * GIB)
    R = FakeJob("R", 3 * GIB)
    first.select([A, R])
    first.accept(A, at=0)

    clock.set(5)
    recovered = make_controller(clock, path=ledger_path)
    assert recovered.ledger.latest_for(job_identity(A)).state == "accepted"
    assert recovered.ledger.latest_for(job_identity(R)).state == "reserved"
    # Without evidence, neither memory nor permit of R is freed.
    assert recovered.memory_in_use == 5 * GIB
    assert recovered.select([FakeJob("R", 3 * GIB)]) == []
    assert recovered.ledger.available_permits(5) == 0


def _write_snakefile(tmp_path):
    (tmp_path / "Snakefile").write_text(
        """
rule all:
    input:
        "a.out", "b.out"

rule a:
    output:
        "a.out"
    priority: 5
    resources:
        mem_mb=1024
    shell:
        "touch {output}"

rule b:
    output:
        "b.out"
    priority: 5
    resources:
        mem_mb=1024
    shell:
        "touch {output}"
"""
    )


def test_real_workflow_with_deadlines_uses_deadline_scheduler(tmp_path):
    from snakemake import api
    from snakemake.settings import types as settings

    _write_snakefile(tmp_path)
    with api.SnakemakeApi(
        settings.OutputSettings(verbose=True, show_failed_logs=True)
    ) as snakemake_api:
        workflow_api = snakemake_api.workflow(
            resource_settings=settings.ResourceSettings(cores=2),
            snakefile=tmp_path / "Snakefile",
            workdir=tmp_path,
        )
        dag_api = workflow_api.dag()
        dag_api.execute_workflow(
            scheduling_settings=settings.SchedulingSettings(
                deadlines=["b@9999999999", "a@9999999998"],
            )
        )
    assert (tmp_path / "a.out").exists()
    assert (tmp_path / "b.out").exists()


def test_real_workflow_without_deadlines_still_works(tmp_path):
    from snakemake import api
    from snakemake.settings import types as settings

    _write_snakefile(tmp_path)
    with api.SnakemakeApi(
        settings.OutputSettings(verbose=True, show_failed_logs=True)
    ) as snakemake_api:
        workflow_api = snakemake_api.workflow(
            resource_settings=settings.ResourceSettings(cores=2),
            snakefile=tmp_path / "Snakefile",
            workdir=tmp_path,
        )
        workflow_api.dag().execute_workflow(
            # The bundled ILP solver binary is not available for every
            # architecture; the default greedy scheduler is the portable path.
            scheduling_settings=settings.SchedulingSettings(scheduler="greedy"),
            scheduler_settings=GreedySchedulerSettings(),
        )
    assert (tmp_path / "a.out").exists()
    assert (tmp_path / "b.out").exists()


def test_deadline_specification_parsing():
    import datetime as dt

    assert Deadline.parse("A:sample=x@100").at == 100
    assert Deadline.parse("A@100").wildcards == ()
    parsed = Deadline.parse("A:sample=x,region=r@2026-11-23T00:00:00Z")
    assert parsed.wildcards_dict == {"sample": "x", "region": "r"}
    assert parsed.at == dt.datetime(2026, 11, 23, tzinfo=dt.timezone.utc).timestamp()
    # Wildcards are sorted for stable ordering.
    assert parsed.wildcards == (("region", "r"), ("sample", "x"))
    with pytest.raises(ValueError):
        Deadline.parse("A")


def test_deadline_matching_uses_sorted_wildcard_values():
    registry = DeadlineRegistry(
        [
            Deadline.parse("A:sample=x@20"),
            Deadline.parse("A:sample=y@10"),
        ]
    )
    assert registry.deadline_for("A", {"sample": "x"}) == 20
    assert registry.deadline_for("A", {"sample": "y"}) == 10
    assert registry.deadline_for("A", {"sample": "z"}) is None
    # Rule-wide deadline applies when no wildcard constraint matches.
    registry.add(Deadline.parse("A@30"))
    assert registry.deadline_for("A", {"sample": "z"}) == 30


def test_attempt_change_updates_scheduler_resources():
    """Retry resources must reflect the new attempt (real workflow parse)."""
    from snakemake import api
    from snakemake.settings import types as settings
    from snakemake.scheduling.dispatch import job_memory

    import tempfile
    from pathlib import Path

    workdir = Path(tempfile.mkdtemp(prefix="deadline-attempt-"))
    (workdir / "Snakefile").write_text(
        """
rule all:
    input: "out.txt"

rule A:
    output: "out.txt"
    resources:
        mem_mb=lambda w, attempt: (2 if attempt == 1 else 6) * 1024
    shell: "touch {output}"
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
        job = next(j for j in workflow.dag.needrun_jobs() if j.rule.name == "A")
        assert job_memory(job) == 2 * GIB
        job.attempt = 2
        assert job_memory(job) == 6 * GIB


def test_released_attempt_can_be_replanned_and_completed():
    """A not-accepted plan may be replanned with the same attempt number."""
    clock = Clock(0)
    controller = make_controller(clock)
    job = FakeJob("C", 1 * GIB)
    controller.select([job])
    entry = controller.ledger.latest_for(job_identity(job))
    controller.not_accepted(job, note="restart showed it was never accepted")
    assert entry.state == "released"

    # Replan the same attempt after recovery.
    assert controller.select([job]) == [job]
    replanned = controller.ledger.latest_for(job_identity(job))
    assert replanned is not entry
    assert replanned.state == "reserved"
    assert controller.should_process(job) is True
    # Completion of the replanned attempt is processed and frees its memory.
    assert controller.finish(job, at=1) is True
    assert controller.memory_in_use == 0
    # Replaying it again changes nothing.
    assert controller.should_process(job) is False
    assert controller.finish(job, at=2) is False
