"""Regression test: temp file cleanup of remote group jobs with a pending
checkpoint.

Scenario
--------
A remote job group (simulated here by a local-directory executor stand-in that
spawns one snakemake subprocess per job, like a cluster executor would)
prepares two temp files:

* ``scratch.txt`` is only consumed inside the group.
* ``handoff.txt`` is first consumed inside the group (to create a summary),
  but an out-of-group reconciliation consumer may read it again later.

An independent checkpoint decides, based on a manifest published by another
system, whether that consumer will exist. The checkpoint does not read
``handoff.txt`` and may complete before or after the group. When the group is
submitted while the manifest is still undecided, the main scheduler must not
grant the remote job permission to delete ``handoff.txt`` (``scratch.txt`` may
be deleted). Only once the manifest has decided may ``handoff.txt`` be
cleaned up: immediately if there is no consumer, or after the consumer has
finally succeeded.

The test checks both sides of the handshake:

* the permission list the parent graph passes to the remote job
  (``--unneeded-temp-files`` in the submitted command), and
* which files the remote job actually deleted,

so that it can distinguish a parent that approves ``handoff.txt`` too early
from a remote that deletes the wrong object despite a correct permission, and
from an implementation that treats every unknown permission as
delete-forbidden and thus leaks ``scratch.txt``. Because the stage state is
checked right after the group finishes, the test still points at the faulty
stage even if a prematurely deleted ``handoff.txt`` is later rebuilt and the
final files look complete.
"""

import re
import shutil
import subprocess
import sys
import threading
import time
import types
from pathlib import Path

import pytest

# Import api first to resolve the dag <-> workflow circular import.
import snakemake.api  # noqa: F401
from snakemake import api
from snakemake.scheduling.greedy import SchedulerSettings as GreedySchedulerSettings
from snakemake.settings import types as settings
from snakemake_interface_executor_plugins.executors.base import SubmittedJobInfo
from snakemake_interface_executor_plugins.executors.real import RealExecutor
from snakemake_interface_executor_plugins.registry import ExecutorPluginRegistry
from snakemake_interface_executor_plugins.settings import (
    CommonSettings,
    ExecMode,
    SharedFSUsage,
)

DATADIR = Path(__file__).parent / "test_group_temp_checkpoint"

ON_WINDOWS = sys.platform.startswith("win")

#: How long to wait for orchestration handshakes before failing the test.
HANDSHAKE_TIMEOUT = 180


class LocaldirExecutor(RealExecutor):
    """Executor stand-in simulating a remote cluster on a local directory.

    Every (group) job is spawned as a separate ``snakemake`` subprocess in the
    shared working directory (like a cluster executor submitting to a node
    with a shared FS). Every submitted command line -- including the
    ``--unneeded-temp-files`` permission list granted by the main scheduler --
    and every completion is appended to ``executor.log`` in the workdir, which
    the test uses as a file handshake.
    """

    def __post_init__(self):
        self._threads = []
        self._log = Path(self.workflow.workdir_init) / "executor.log"

    @property
    def cores(self):
        return "all"

    def get_exec_mode(self):
        return ExecMode.REMOTE

    def get_python_executable(self):
        return sys.executable

    def run_job(self, job):
        cmd = self.format_job_exec(job)
        job_info = SubmittedJobInfo(job=job)
        with self._log.open("a") as log:
            log.write(f"SUBMIT job={job.jobid} group={job.is_group()} cmd={cmd!r}\n")
        self.report_job_submission(job_info)
        thread = threading.Thread(target=self._wait, args=(job_info, cmd), daemon=True)
        thread.start()
        self._threads.append(thread)

    def _wait(self, job_info, cmd):
        returncode = subprocess.call(
            cmd, shell=True, cwd=self.workflow.workdir_init
        )
        with self._log.open("a") as log:
            log.write(f"DONE job={job_info.job.jobid} rc={returncode}\n")
        if returncode == 0:
            self.report_job_success(job_info)
        else:
            self.report_job_error(job_info)

    def shutdown(self):
        for thread in self._threads:
            thread.join()

    def cancel(self):
        pass

    def handle_job_success(self, job):
        pass

    def handle_job_error(self, job):
        pass


_executor_module = types.ModuleType("localdir")
_executor_module.Executor = LocaldirExecutor
_executor_module.common_settings = CommonSettings(
    non_local_exec=True,
    implies_no_shared_fs=False,
    job_deploy_sources=False,
    pass_envvar_declarations_to_cmd=True,
    auto_deploy_default_storage_provider=False,
)
ExecutorPluginRegistry().register_plugin("localdir", _executor_module)


_SUBMIT_RE = re.compile(r"^SUBMIT job=(?P<jobid>\S+) group=(?P<group>\S+) cmd=(?P<cmd>.*)$", re.M)
_DONE_RE = re.compile(r"^DONE job=(?P<jobid>\S+) rc=(?P<rc>\d+)$", re.M)
_UNNEEDED_RE = re.compile(r"--unneeded-temp-files(?P<files>(?:\s+'[^']*')+)")


class Submission:
    def __init__(self, jobid, is_group, cmd):
        self.jobid = jobid
        self.is_group = is_group
        self.cmd = cmd

    @property
    def unneeded_temp_files(self):
        """Temp files the main scheduler allowed this job to delete."""
        match = _UNNEEDED_RE.search(self.cmd)
        if match is None:
            return []
        return re.findall(r"'([^']*)'", match.group("files"))


def read_executor_log(workdir):
    """Return (submissions, returncodes) parsed from the executor handshake log."""
    log = workdir / "executor.log"
    if not log.exists():
        return [], {}
    content = log.read_text()
    submissions = [
        Submission(
            match.group("jobid"),
            is_group=match.group("group") == "True",
            cmd=match.group("cmd"),
        )
        for match in _SUBMIT_RE.finditer(content)
    ]
    returncodes = {
        match.group("jobid"): int(match.group("rc"))
        for match in _DONE_RE.finditer(content)
    }
    return submissions, returncodes


def wait_for(description, predicate, timeout=HANDSHAKE_TIMEOUT):
    """Poll predicate until it returns a truthy value and return it."""
    start = time.time()
    while time.time() - start < timeout:
        result = predicate()
        if result:
            return result
        time.sleep(0.1)
    raise TimeoutError(f"timed out waiting for {description}")


def run_workflow(workdir, cores, notemp=False):
    """Execute the test workflow with the localdir executor stand-in."""
    with api.SnakemakeApi(settings.OutputSettings(verbose=False)) as snakemake_api:
        workflow_api = snakemake_api.workflow(
            resource_settings=settings.ResourceSettings(cores=cores, nodes=cores),
            storage_settings=settings.StorageSettings(
                shared_fs_usage=SharedFSUsage.all(),
                notemp=notemp,
            ),
            snakefile=DATADIR / "Snakefile",
            workdir=workdir,
        )
        dag_api = workflow_api.dag(dag_settings=settings.DAGSettings())
        dag_api.execute_workflow(
            executor="localdir",
            execution_settings=settings.ExecutionSettings(),
            remote_execution_settings=settings.RemoteExecutionSettings(
                seconds_between_status_checks=0
            ),
            scheduling_settings=settings.SchedulingSettings(scheduler="greedy"),
            scheduler_settings=GreedySchedulerSettings(),
        )


def run_workflow_async(workdir, cores, notemp=False):
    """Run the workflow in a thread; returns (thread, result dict)."""
    result = {}

    def target():
        try:
            run_workflow(workdir, cores=cores, notemp=notemp)
        except Exception as e:  # noqa: BLE001 - propagated to the test
            result["error"] = e
        else:
            result["error"] = None

    thread = threading.Thread(target=target, daemon=True)
    thread.start()
    return thread, result


def join_run(thread, result):
    thread.join(timeout=HANDSHAKE_TIMEOUT)
    assert not thread.is_alive(), "workflow run did not finish in time"
    return result["error"]


def prepare_workdir(tmp_path):
    workdir = tmp_path / "work"
    workdir.mkdir()
    shutil.copy(DATADIR / "Snakefile", workdir / "Snakefile")
    return workdir


def publish_manifest(workdir, decision):
    """Simulate the external system publishing the reconciliation manifest."""
    (workdir / "manifest.published").write_text(decision + "\n")


def group_submission(workdir):
    """Return the Submission of the remote group job, or None if not yet."""
    submissions, _ = read_executor_log(workdir)
    groups = [submission for submission in submissions if submission.is_group]
    return groups[0] if groups else None


def finished_group_submission(workdir):
    """Return the group Submission once the remote group job has finished."""
    group = group_submission(workdir)
    if group is None:
        return None
    _, returncodes = read_executor_log(workdir)
    if returncodes.get(group.jobid) == 0:
        return group
    return None


def submission_of_rule(workdir, rule):
    """Return the Submission of the single job running the given rule."""
    submissions, _ = read_executor_log(workdir)
    for submission in submissions:
        if not submission.is_group and f"--allowed-rules {rule} " in submission.cmd + " ":
            return submission
    return None


def run_counts(workdir):
    """How often each rule of the ingestion group has actually run."""
    runs_log = workdir / "runs.log"
    counts = {"prepare": 0, "slice": 0, "summarize": 0}
    if runs_log.exists():
        for line in runs_log.read_text().splitlines():
            counts[line.strip()] = counts.get(line.strip(), 0) + 1
    return counts


def assert_group_ran_once(workdir):
    counts = run_counts(workdir)
    assert counts == {"prepare": 1, "slice": 1, "summarize": 1}, (
        "ingestion group jobs must run exactly once; "
        "a rerun means a prematurely deleted temp file had to be rebuilt"
    )


def assert_reconcile_failed_once(workdir):
    """The first run must have failed in the reconcile job (by design)."""
    reconcile = submission_of_rule(workdir, "reconcile")
    assert reconcile is not None, "reconcile job was never submitted"
    _, returncodes = read_executor_log(workdir)
    assert returncodes.get(reconcile.jobid) == 1, (
        "reconcile was expected to fail its first attempt"
    )


@pytest.mark.skipif(ON_WINDOWS, reason="executor stand-in requires a POSIX shell")
def test_group_submitted_before_manifest_with_consumer(tmp_path):
    """Group finishes before the manifest arrives; a consumer shows up later.

    While the manifest is undecided, the parent must not approve deletion of
    handoff.txt. scratch.txt may be deleted. The consumer later reads the
    original handoff.txt, fails once, and only after its successful resume may
    handoff.txt be deleted.
    """
    workdir = prepare_workdir(tmp_path)

    thread, result = run_workflow_async(workdir, cores=4)
    try:
        # Stage 1: the remote group has finished, the manifest is still
        # undecided (the checkpoint job is still waiting for it).
        group = wait_for(
            "remote group to finish", lambda: finished_group_submission(workdir)
        )

        # The parent must not have approved handoff.txt for deletion while
        # the checkpoint was still unevaluated.
        assert "handoff.txt" not in group.unneeded_temp_files, (
            "parent graph approved handoff.txt for deletion before the "
            "manifest decided whether a consumer exists"
        )

        # The remote must not have deleted handoff.txt, no matter what the
        # permission list said. Check right now, so that a later rebuild
        # cannot mask the premature deletion.
        assert (workdir / "handoff.txt").exists(), (
            "handoff.txt was deleted before the manifest decided whether a "
            "consumer exists"
        )
        handoff_content = (workdir / "handoff.txt").read_text()
        assert_group_ran_once(workdir)

        # Stage 2: the manifest arrives and expands an out-of-group consumer.
        publish_manifest(workdir, "consume")
    finally:
        # Never leave the checkpoint waiting if an assertion failed.
        if not (workdir / "manifest.published").exists():
            publish_manifest(workdir, "consume")

    # The first run fails because reconcile fails its first attempt.
    error = join_run(thread, result)
    assert error is not None, "first run should fail in reconcile's first attempt"
    assert_reconcile_failed_once(workdir)
    assert_group_ran_once(workdir)

    # Resume in the same directory: reconcile succeeds without regenerating
    # the summary or the slices.
    run_workflow(workdir, cores=4)

    assert_group_ran_once(workdir)
    # The consumer must have read the original handoff, not a rebuilt one.
    assert (workdir / "reconciled.txt").read_text() == handoff_content
    assert (workdir / "summary.txt").read_text() == handoff_content
    # Only after the final success may the temps be gone.
    assert not (workdir / "handoff.txt").exists(), (
        "handoff.txt must be deleted once the consumer has finally succeeded"
    )
    assert not (workdir / "scratch.txt").exists(), "scratch.txt leaked"
    assert (workdir / "report.txt").exists()


@pytest.mark.skipif(ON_WINDOWS, reason="executor stand-in requires a POSIX shell")
def test_group_submitted_before_manifest_without_consumer(tmp_path):
    """Group finishes before the manifest arrives; no consumer shows up.

    handoff.txt must survive until the manifest has decided; afterwards it is
    cleaned up even though no consumer ever appeared.
    """
    workdir = prepare_workdir(tmp_path)

    thread, result = run_workflow_async(workdir, cores=4)
    try:
        group = wait_for(
            "remote group to finish", lambda: finished_group_submission(workdir)
        )
        assert "handoff.txt" not in group.unneeded_temp_files, (
            "parent graph approved handoff.txt for deletion before the "
            "manifest decided whether a consumer exists"
        )
        assert (workdir / "handoff.txt").exists(), (
            "handoff.txt was deleted before the manifest decided whether a "
            "consumer exists"
        )
        assert_group_ran_once(workdir)

        publish_manifest(workdir, "noconsume")
    finally:
        if not (workdir / "manifest.published").exists():
            publish_manifest(workdir, "noconsume")

    error = join_run(thread, result)
    assert error is None, f"workflow run failed: {error}"

    assert_group_ran_once(workdir)
    # Once the manifest has decided that no consumer exists, both temp files
    # must be cleaned up.
    assert not (workdir / "handoff.txt").exists(), (
        "handoff.txt must be cleaned up once the manifest ruled out a consumer"
    )
    assert not (workdir / "scratch.txt").exists(), "scratch.txt leaked"
    assert (workdir / "report.txt").exists()


@pytest.mark.skipif(ON_WINDOWS, reason="executor stand-in requires a POSIX shell")
def test_manifest_published_before_group_with_consumer(tmp_path):
    """The manifest decides before the group is submitted; a consumer exists.

    The parent then knows about the consumer and only approves scratch.txt;
    the remote deletes exactly that and keeps handoff.txt until the consumer
    has finally succeeded.
    """
    workdir = prepare_workdir(tmp_path)
    publish_manifest(workdir, "consume")

    # A single core makes the checkpoint complete before the group is
    # submitted (the checkpoint has the higher priority), so the permission
    # list is computed with the manifest decision already known.
    error = None
    try:
        run_workflow(workdir, cores=1)
    except Exception as e:  # noqa: BLE001 - expected: reconcile's first attempt
        error = e
    assert error is not None, "first run should fail in reconcile's first attempt"
    assert_reconcile_failed_once(workdir)

    submissions, _ = read_executor_log(workdir)
    checkpoint = submission_of_rule(workdir, "manifest")
    group = next(s for s in submissions if s.is_group)
    jobids = [submission.jobid for submission in submissions]
    assert checkpoint is not None and jobids.index(checkpoint.jobid) < jobids.index(
        group.jobid
    ), (
        "orchestration assumption violated: the checkpoint must complete "
        "before the group is submitted"
    )

    # With the decision known, the parent approves exactly scratch.txt.
    assert sorted(group.unneeded_temp_files) == ["scratch.txt"], (
        "with a known consumer, the parent must approve scratch.txt only"
    )
    # The remote deleted exactly the permitted file and kept handoff.txt.
    assert not (workdir / "scratch.txt").exists(), (
        "remote did not delete scratch.txt although it was permitted"
    )
    assert (workdir / "handoff.txt").exists(), (
        "remote deleted handoff.txt although it was not permitted"
    )
    handoff_content = (workdir / "handoff.txt").read_text()
    assert_group_ran_once(workdir)

    # Resume in the same directory.
    run_workflow(workdir, cores=1)

    assert_group_ran_once(workdir)
    assert (workdir / "reconciled.txt").read_text() == handoff_content
    assert not (workdir / "handoff.txt").exists(), (
        "handoff.txt must be deleted once the consumer has finally succeeded"
    )
    assert not (workdir / "scratch.txt").exists(), "scratch.txt leaked"
    assert (workdir / "report.txt").exists()


@pytest.mark.skipif(ON_WINDOWS, reason="executor stand-in requires a POSIX shell")
def test_manifest_published_before_group_without_consumer(tmp_path):
    """The manifest decides before the group is submitted; no consumer exists.

    With the decision known, the parent may approve both temp files and the
    remote deletes them when the group finishes.
    """
    workdir = prepare_workdir(tmp_path)
    publish_manifest(workdir, "noconsume")

    run_workflow(workdir, cores=1)

    submissions, _ = read_executor_log(workdir)
    group = next(s for s in submissions if s.is_group)
    # With no consumer in sight, both temp files are approved and deleted.
    assert sorted(group.unneeded_temp_files) == ["handoff.txt", "scratch.txt"], (
        "with no consumer, the parent should approve both temp files"
    )
    assert not (workdir / "scratch.txt").exists()
    assert not (workdir / "handoff.txt").exists(), (
        "handoff.txt must be cleaned up once the manifest ruled out a consumer"
    )
    assert_group_ran_once(workdir)
    assert (workdir / "report.txt").exists()


@pytest.mark.skipif(ON_WINDOWS, reason="executor stand-in requires a POSIX shell")
def test_group_submitted_before_manifest_notemp(tmp_path):
    """With --notemp, both temp files are kept no matter what is decided."""
    workdir = prepare_workdir(tmp_path)

    thread, result = run_workflow_async(workdir, cores=4, notemp=True)
    try:
        group = wait_for(
            "remote group to finish", lambda: finished_group_submission(workdir)
        )
        # --notemp has to reach the remote group job.
        assert "--notemp" in group.cmd, "--notemp did not reach the remote group job"
        publish_manifest(workdir, "consume")
    finally:
        if not (workdir / "manifest.published").exists():
            publish_manifest(workdir, "consume")

    error = join_run(thread, result)
    assert error is not None, "first run should fail in reconcile's first attempt"

    run_workflow(workdir, cores=4, notemp=True)

    assert (workdir / "scratch.txt").exists(), "scratch.txt deleted despite --notemp"
    assert (workdir / "handoff.txt").exists(), "handoff.txt deleted despite --notemp"
    assert (workdir / "report.txt").exists()
