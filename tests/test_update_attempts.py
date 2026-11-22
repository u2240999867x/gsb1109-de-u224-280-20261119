__authors__ = ["GSB1109"]
__license__ = "MIT"

"""Regression tests for update() attempt evidence and group commits.

These tests drive the real workflow through the existing job output update and
recovery paths.  Nothing is logged by the business commands; the evidence comes
from Snakemake's own isolated shadow candidate and persistence journal.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest


PYTHON = sys.executable
FIXTURE = Path(__file__).parent / "test_update_attempt_commit"
CRASH_ENV = "SNAKEMAKE_UPDATE_TEST_CRASH"


def run_workflow(
    workdir: Path,
    *extra: str,
    crash: str | None = None,
    shouldfail: bool = False,
) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env.pop(CRASH_ENV, None)
    if crash is not None:
        env[CRASH_ENV] = crash
    cmd = [
        PYTHON,
        "-m",
        "snakemake",
        "--nolock",
        "--scheduler",
        "greedy",
        "-c",
        "1",
        *extra,
    ]
    result = subprocess.run(
        cmd,
        cwd=workdir,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if shouldfail:
        assert result.returncode != 0, result.stdout
    else:
        assert result.returncode == 0, result.stdout
    return result


def update_status(workdir: Path, *paths: str) -> dict:
    cmd = [PYTHON, "-m", "snakemake", "--nolock", "--update-status", *paths]
    result = subprocess.run(
        cmd,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout)


def read(path: Path) -> bytes:
    return path.read_bytes()


@pytest.fixture
def workdir(tmp_path):
    target = tmp_path / "work"
    shutil.copytree(FIXTURE, target)
    return target


def test_group_commit_is_consumable(workdir):
    run_workflow(workdir)
    status = update_status(workdir, "orders.tbl", "exceptions.tbl", "nightly.done")
    consumable = status["consumable"]
    assert set(consumable) == {"orders.tbl", "exceptions.tbl", "nightly.done"}
    for path, entry in consumable.items():
        assert entry["consumable"] is True, entry
        assert entry["status"] == "consumable", entry
        assert entry["produced_by_latest_attempt"] is True, entry
        assert entry["complete"] is True, entry
    # All three belong to the same complete group.
    assert len({entry["group_id"] for entry in consumable.values()}) == 1


def test_failed_attempt_evidence_survives_restore(workdir):
    """A restored file can look identical yet the attempt really wrote it."""
    before = workdir / "orders.tbl"
    before_bytes = read(before)
    before_mtime = before.stat().st_mtime_ns

    # Rewrite orders.tbl with identical bytes and preserve its mtime, then fail
    # after writing.  The visible file ends up identical to the pre-attempt
    # state; only the journal can prove the write happened.
    snakefile = workdir / "Snakefile"
    snakefile.write_text(
        """
rule all:
    input: "nightly.done"

rule nightbatch:
    input: "seed.txt"
    output:
        update("orders.tbl"),
        update("exceptions.tbl"),
        "nightly.done"
    shadow: "minimal"
    shell:
        \"\"\"
        cp seed.txt orders.tbl
        touch -r seed.txt orders.tbl
        echo exception >> exceptions.tbl
        echo ok > nightly.done
        exit 1
        \"\"\"
"""
    )
    # seed.txt has the same bytes as the current orders.tbl.
    (workdir / "seed.txt").write_bytes(before_bytes)

    run_workflow(workdir, shouldfail=True)

    after = workdir / "orders.tbl"
    assert after.exists()
    assert read(after) == before_bytes
    assert after.stat().st_mtime_ns == before_mtime

    status = update_status(workdir, "orders.tbl")
    entry = status["consumable"]["orders.tbl"]
    assert entry["consumable"] is False
    assert entry["produced_by_latest_attempt"] is False
    assert entry["latest_attempt"]["write_observed"] is True
    assert entry["last_restore"] is not None
    assert entry["last_restore"]["restored"] is True

    attempt = [a for a in status["attempts"] if "orders.tbl" in a["outputs"]][-1]
    output = attempt["outputs"]["orders.tbl"]
    assert output["produced"] is True
    assert output["write_observed"] is True
    assert output["registered"] is False
    assert output["committed"] is False
    assert attempt["status"] == "failed"


def test_crash_after_first_registration_is_not_reused(workdir):
    """Crash with the first output registered but no complete group."""
    run_workflow(workdir)
    committed_orders = read(workdir / "orders.tbl")
    committed_exceptions = read(workdir / "exceptions.tbl")

    run_workflow(
        workdir, "-R", "nightbatch", crash="output_registered", shouldfail=True
    )

    status = update_status(workdir, "orders.tbl", "exceptions.tbl")
    orders = status["consumable"]["orders.tbl"]
    assert orders["consumable"] is False
    assert orders["latest_attempt"]["group_committed"] is False
    # The first output was registered, but the group record was never written,
    # so the pair is not a consumable result.
    assert orders["latest_attempt"]["registered"] is True
    assert orders["latest_attempt"]["produced"] is True
    assert (
        status["consumable"]["exceptions.tbl"]["latest_attempt"]["registered"]
        is False
    )

    # Restart: the abandoned attempt must be rolled back to the previous
    # complete group and then rerun.  The final state is one complete group.
    run_workflow(workdir)
    assert read(workdir / "orders.tbl") == committed_orders + b"order\n"
    assert read(workdir / "exceptions.tbl") == committed_exceptions + b"exception\n"

    status = update_status(workdir, "orders.tbl", "exceptions.tbl")
    for entry in status["consumable"].values():
        assert entry["consumable"] is True
        assert entry["complete"] is True
    restores = [
        record
        for record in status["restores"]
        if record.get("reason") == "interrupted_attempt_recovery"
    ]
    assert {record["path"] for record in restores} == {
        "orders.tbl",
        "exceptions.tbl",
    }


def test_preexisting_files_are_not_claimed_as_produced(workdir):
    """Readable paths alone do not prove production by this round."""
    status = update_status(workdir, "orders.tbl", "exceptions.tbl")
    for entry in status["consumable"].values():
        assert entry["consumable"] is False
        assert entry["status"] == "preexisting_unverified"
        assert entry["produced_by_latest_attempt"] is False
        assert entry["basis"] == "none"
