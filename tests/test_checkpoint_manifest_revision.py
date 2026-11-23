"""Regression tests for checkpoint driven dynamic scheduling.

The workflow used by the tests is embedded below as ``FIXTURE_SNAKEFILE``:

* A manifest published by a checkpoint lists the samples of a sequencing batch.
* The verified manifest is revised while the first planned sample job is
  already running.
* The revision keeps ``A`` with the same input, withdraws ``B``, gives ``C`` a
  different input digest and adds ``D``.
* The old attempt of ``C`` finishes only after the revision switch.
* A final summary may only consume products of the current revision, must name
  the source manifest of every item and must record the product's real input
  identity.

The tests run a real, small workflow through the local executor and inspect the
files that were actually read/dispatched. No mock replaces the scheduling logic.
"""

import hashlib
import os
import shutil
import sys
from pathlib import Path

import pytest

# Import api first to resolve the dag <-> workflow circular import.
import snakemake.api  # noqa: F401
from snakemake import api
from snakemake.settings import types as settings
from snakemake.settings.enums import RerunTrigger


FIXTURE_SNAKEFILE = r'''\
import os

# A sequencing batch discovers its samples from a manifest that a checkpoint
# publishes. The verified manifest may be revised while sample jobs are being
# dispatched, so the final batch summary must:
#   * only consume products of the current revision,
#   * name the source manifest of every item,
#   * record the product's real input identity,
#   * distinguish withdrawn items from items whose input changed under the same
#     name.
#
# The revision switch happens inside the shell of the first attempt for sample
# "C": it rewrites the published manifest to the second verified version and
# only then lets that old attempt report completion. With one core and the
# priority order below this is deterministic; no sleep or timing trick is used.

MANIFEST = "manifest.current.tsv"


def manifest_rows():
    directory = str(checkpoints.publish.get().output[0])
    rows = {}
    with open(os.path.join(directory, "manifest.tsv")) as f:
        next(f, None)
        for line in f:
            line = line.rstrip("\n")
            if not line:
                continue
            sample, version, input_name, digest = line.split("\t")[:4]
            rows[sample] = dict(
                sample=sample,
                version=version,
                input=input_name,
                digest=digest,
                path=os.path.join(directory, "inputs", f"{sample}.{digest}.txt"),
            )
    return rows


def sample_row(wildcards):
    rows = manifest_rows()
    row = rows.get(wildcards.sample)
    if row is None:
        # Withdrawn from the current revision. The old attempt must not be
        # dispatched, and its completion must not satisfy anything.
        withdraw(
            f"sample {wildcards.sample} is no longer part of the verified manifest"
        )
    return row


def sample_input(wildcards):
    return sample_row(wildcards)["path"]


def sample_version(wildcards):
    return sample_row(wildcards)["version"]


def sample_input_name(wildcards):
    return sample_row(wildcards)["input"]


def sample_digest(wildcards):
    return sample_row(wildcards)["digest"]


def summary_input(wildcards):
    return [f"results/{sample}.tsv" for sample in sorted(manifest_rows())]


rule all:
    default_target: True
    input:
        "summary.txt"


checkpoint publish:
    input:
        MANIFEST
    output:
        directory("published")
    shell:
        """
        rm -rf {output}
        mkdir -p {output}/inputs
        cp {input} {output}/manifest.tsv
        while IFS=$'\t' read -r sample version input_name digest; do
            [ "$sample" = "sample" ] && continue
            [ -z "$sample" ] && continue
            cp "inputs/${{input_name}}" "{output}/inputs/${{sample}}.${{digest}}.txt"
        done < {input}
        """


rule sample:
    input:
        sample_input
    output:
        "results/{sample}.tsv"
    params:
        version=sample_version,
        input_name=sample_input_name,
        digest=sample_digest,
    priority:
        # A is already complete on the first run. C must run before B so that
        # B is still waiting when the manifest is switched.
        lambda wc: {"A": 100, "C": 90, "B": 80, "D": 10}.get(wc.sample, 1)
    shell:
        """
        if [ "{wildcards.sample}" = "C" ] && [ "{params.version}" = "v1" ]; then
            # Simulate the verified manifest being revised right before this
            # attempt reports completion. The test can hold the old attempt
            # here to observe the switch before the old attempt finishes.
            cp manifest.v2.tsv published/manifest.tsv
            cp inputs/c.txt published/inputs/C.dc.txt
            cp inputs/d.txt published/inputs/D.dd.txt
            i=0
            while [ -f hold-old-c ] && [ "$i" -lt 1200 ]; do
                sleep 0.05
                i=$((i + 1))
            done
        fi
        mkdir -p results
        manifest_hash=$(sha256sum published/manifest.tsv | cut -d' ' -f1)
        identity=$(sha256sum {input} | cut -d' ' -f1)
        printf 'sample=%s\\nversion=%s\\ndigest=%s\\nmanifest-hash=%s\\ninput=%s\\ninput-identity=%s\\n' \
            "{wildcards.sample}" "{params.version}" "{params.digest}" \
            "$manifest_hash" "{input}" "$identity" > {output}
        """


rule summary:
    input:
        summary_input
    output:
        "summary.txt"
    shell:
        """
        rows=summary.rows
        : > "$rows"
        manifest_v1_hash=$(sha256sum manifest.v1.tsv | cut -d' ' -f1)
        manifest_v2_hash=$(sha256sum manifest.v2.tsv | cut -d' ' -f1)

        # Items that disappeared from the current revision are reported as
        # withdrawn, with the source manifest and the item identity they had.
        # If the first revision is absent there is nothing to withdraw.
        if [ -f manifest.v1.tsv ]; then
            while IFS=$'\t' read -r sample version input_name digest; do
                [ "$sample" = "sample" ] && continue
                [ -z "$sample" ] && continue
                if ! grep -q "^$sample	" published/manifest.tsv; then
                    printf '%s\twithdrawn\tmanifest.v1.tsv\t%s\t%s\t-\t-\t-\n' \
                        "$sample" "$version" "$digest" >> "$rows"
                fi
            done < manifest.v1.tsv
        fi
        while IFS=$'\t' read -r sample version input_name digest; do
            [ "$sample" = "sample" ] && continue
            [ -z "$sample" ] && continue
            product="results/${{sample}}.tsv"
            if [ ! -f "$product" ]; then
                echo "missing product for $sample" >&2
                exit 1
            fi
            product_digest=$(sed -n 's/^digest=\\(.*\\)/\\1/p' "$product")
            if [ "$product_digest" != "$digest" ]; then
                echo "stale product for $sample: $product_digest != $digest" >&2
                exit 1
            fi
            expected="published/inputs/${{sample}}.${{digest}}.txt"
            product_input=$(sed -n 's/^input=\\(.*\\)/\\1/p' "$product")
            if [ "$product_input" != "$expected" ]; then
                echo "product for $sample read $product_input, expected $expected" >&2
                exit 1
            fi
            identity=$(sed -n 's/^input-identity=\\(.*\\)/\\1/p' "$product")
            actual_identity=$(sha256sum "$expected" | cut -d' ' -f1)
            if [ "$identity" != "$actual_identity" ]; then
                echo "stale input identity for $sample" >&2
                exit 1
            fi
            product_manifest=$(sed -n 's/^manifest-hash=\\(.*\\)/\\1/p' "$product")
            product_version=$(sed -n 's/^version=\\(.*\\)/\\1/p' "$product")
            if [ "$product_manifest" = "$manifest_v1_hash" ]; then
                source=manifest.v1.tsv
                status=reused
            elif [ "$product_manifest" = "$manifest_v2_hash" ]; then
                source=manifest.v2.tsv
                if [ -f manifest.v1.tsv ] && grep -q "^$sample	" manifest.v1.tsv; then
                    old_digest=$(grep "^$sample	" manifest.v1.tsv | cut -f4)
                    if [ "$old_digest" = "$digest" ]; then
                        status=reused
                    else
                        status=replaced
                    fi
                else
                    status=added
                fi
            else
                echo "product for $sample has unknown manifest revision" >&2
                exit 1
            fi
            printf '%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\t%s\\n' \
                "$sample" "$status" "$source" "$version" "$digest" \
                "$product_input" "$identity" "$product_version" >> "$rows"
        done < published/manifest.tsv

        printf 'sample\tstatus\tsource-manifest\tmanifest-version\tmanifest-digest\tproduct-input\tinput-identity\tproduct-version\\n' > {output}
        sort "$rows" >> {output}
        rm -f "$rows"
        """
'''

MANIFEST_V1 = (
    "sample\tversion\tinput\tdigest\n"
    "A\tv1\ta.txt\tda\n"
    "B\tv1\tb.txt\tdb\n"
    "C\tv1\tc1.txt\tdc1\n"
)

MANIFEST_V2 = (
    "sample\tversion\tinput\tdigest\n"
    "A\tv2\ta.txt\tda\n"
    "C\tv2\tc.txt\tdc\n"
    "D\tv2\td.txt\tdd\n"
)

INPUTS = {
    "a.txt": "A\n",
    "b.txt": "B-v1\n",
    "c1.txt": "C-v1\n",
    "c.txt": "C-v2\n",
    "d.txt": "D\n",
}


def _write_fixture(workdir: Path) -> None:
    """Materialize the workflow and its input data in the given directory."""
    workdir.mkdir(parents=True, exist_ok=True)
    (workdir / "Snakefile").write_text(FIXTURE_SNAKEFILE)
    (workdir / "manifest.v1.tsv").write_text(MANIFEST_V1)
    (workdir / "manifest.v2.tsv").write_text(MANIFEST_V2)
    (workdir / "manifest.current.tsv").write_text(MANIFEST_V1)
    inputs = workdir / "inputs"
    inputs.mkdir()
    for name, content in INPUTS.items():
        (inputs / name).write_text(content)

SUMMARY_HEADER = (
    "sample\tstatus\tsource-manifest\tmanifest-version\tmanifest-digest\t"
    "product-input\tinput-identity\tproduct-version"
)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _run_workflow(workdir, targets=None, until=None, forcerun=frozenset()):
    """Execute the fixture workflow and return all dispatched jobs.

    Jobs are recorded through the executor's ``run_job_pre`` hook, i.e. at the
    point where Snakemake actually hands a job to the executor. The recorded
    input list is the concrete, expanded input that the executor receives.
    """
    from snakemake_interface_executor_plugins.executors.base import AbstractExecutor

    dispatched = []
    original = AbstractExecutor.run_job_pre

    def recording_run_job_pre(self, job):
        dispatched.append(
            dict(
                rule=job.rule.name,
                output=[str(f) for f in job.output],
                input=[str(f) for f in job.input],
                attempt=job.attempt,
                jobid=job.jobid,
            )
        )
        return original(self, job)

    with api.SnakemakeApi(
        settings.OutputSettings(verbose=False, quiet=frozenset())
    ) as snakemake_api:
        workflow_api = snakemake_api.workflow(
            resource_settings=settings.ResourceSettings(cores=1),
            snakefile=Path(workdir) / "Snakefile",
            workdir=Path(workdir),
        )
        dag_api = workflow_api.dag(
            dag_settings=settings.DAGSettings(
                targets=set(targets or ()),
                until=set(until or ()),
                forcerun=set(forcerun),
                rerun_triggers=RerunTrigger.all(),
            )
        )
        from snakemake.scheduling.greedy import (
            SchedulerSettings as GreedySchedulerSettings,
        )

        with _patch(AbstractExecutor, "run_job_pre", recording_run_job_pre):
            dag_api.execute_workflow(
                scheduling_settings=settings.SchedulingSettings(scheduler="greedy"),
                scheduler_settings=GreedySchedulerSettings(),
            )
    return dispatched


class _patch:
    def __init__(self, obj, name, value):
        self.obj = obj
        self.name = name
        self.value = value

    def __enter__(self):
        self.original = getattr(self.obj, self.name)
        setattr(self.obj, self.name, self.value)
        return self

    def __exit__(self, *exc):
        setattr(self.obj, self.name, self.original)
        return False


def _prepare_case(tmp_path: Path) -> Path:
    workdir = tmp_path / "case"
    _write_fixture(workdir)
    return workdir


def _complete_a_only(workdir: Path):
    """Run only the A branch so that A's product exists for the first revision."""
    return _run_workflow(workdir, until=["results/A.tsv"])


def _read_lines(path: Path):
    return path.read_text().splitlines()


@pytest.mark.skipif(
    sys.platform == "win32", reason="Fixture uses POSIX shell and sha256sum"
)
def test_checkpoint_manifest_revision(tmp_path):
    workdir = _prepare_case(tmp_path)

    # Stage 1: A is completed against the first manifest revision.
    first_dispatch = _complete_a_only(workdir)
    a_jobs = [j for j in first_dispatch if "results/A.tsv" in j["output"]]
    assert len(a_jobs) == 1, first_dispatch
    assert a_jobs[0]["input"] == ["published/inputs/A.da.txt"]
    a_product = workdir / "results/A.tsv"
    a_product_before = a_product.read_bytes()
    a_mtime_before = a_product.stat().st_mtime_ns

    # Stage 2: the first revision is verified again and now completed fully.
    # C runs before B, so B is still waiting when C rewrites the manifest to
    # the second revision.
    dispatched = _run_workflow(workdir)

    by_output = {}
    for job in dispatched:
        for out in job["output"]:
            by_output.setdefault(os.path.basename(out), []).append(job)

    # A keeps the same input and must not be dispatched again.
    assert "A.tsv" not in by_output, dispatched
    assert a_product.read_bytes() == a_product_before
    assert a_product.stat().st_mtime_ns == a_mtime_before

    # B was withdrawn after the switch and must never be dispatched.
    assert "B.tsv" not in by_output, dispatched
    assert not (workdir / "results/B.tsv").exists()

    # C is dispatched twice: the old attempt under the first revision and the
    # replacement under the second revision. The replacement must read the
    # current input file, not the one of the old attempt.
    c_jobs = by_output["C.tsv"]
    assert len(c_jobs) == 2, dispatched
    c_inputs = sorted(job["input"][0] for job in c_jobs)
    assert c_inputs == [
        "published/inputs/C.dc.txt",
        "published/inputs/C.dc1.txt",
    ], c_inputs
    # The last C dispatch is the one that produced the final product.
    assert c_jobs[-1]["input"] == ["published/inputs/C.dc.txt"]

    # D is newly discovered and dispatched exactly once.
    assert len(by_output["D.tsv"]) == 1, dispatched
    assert by_output["D.tsv"][0]["input"] == ["published/inputs/D.dd.txt"]

    # The final summary is dispatched only once and reads the products of the
    # current revision.
    assert len(by_output["summary.txt"]) == 1
    assert sorted(by_output["summary.txt"][0]["input"]) == [
        "results/A.tsv",
        "results/C.tsv",
        "results/D.tsv",
    ]

    lines = _read_lines(workdir / "summary.txt")
    assert lines[0] == SUMMARY_HEADER
    rows = {line.split("\t")[0]: line.split("\t") for line in lines[1:]}
    assert set(rows) == {"A", "B", "C", "D"}

    # A came from the first manifest and was reused as-is.
    assert rows["A"][1:4] == ["reused", "manifest.v1.tsv", "v2"]
    assert rows["A"][4] == "da"
    assert rows["A"][5] == "published/inputs/A.da.txt"
    assert rows["A"][7] == "v1"  # the product itself still carries v1

    # B was withdrawn and never produced anything.
    assert rows["B"][1:6] == ["withdrawn", "manifest.v1.tsv", "v1", "db", "-"]
    assert rows["B"][6:] == ["-", "-"]

    # C kept its name but changed input identity; the old attempt's product
    # must not be reported as current.
    assert rows["C"][1:4] == ["replaced", "manifest.v2.tsv", "v2"]
    assert rows["C"][4] == "dc"
    assert rows["C"][5] == "published/inputs/C.dc.txt"
    assert rows["C"][7] == "v2"

    # D is new in the second revision.
    assert rows["D"][1:4] == ["added", "manifest.v2.tsv", "v2"]
    assert rows["D"][4] == "dd"
    assert rows["D"][5] == "published/inputs/D.dd.txt"
    assert rows["D"][7] == "v2"

    # Every reported input identity is the real content digest of the file the
    # product recorded, and every product was produced from the current
    # revision.
    for sample, row in rows.items():
        if row[1] == "withdrawn":
            continue
        identity = row[6]
        assert identity != "-"
        product_input = workdir / row[5]
        assert product_input.exists()
        assert _sha256(product_input) == identity
        product = workdir / "results" / f"{sample}.tsv"
        assert product.exists()
        recorded = dict(
            line.split("=", 1)
            for line in product.read_text().splitlines()
            if "=" in line
        )
        assert recorded["digest"] == row[4]
        assert recorded["input"] == row[5]
        assert recorded["input-identity"] == identity


@pytest.mark.skipif(
    sys.platform == "win32", reason="Fixture uses POSIX shell and sha256sum"
)
def test_same_revision_rerun_is_a_noop(tmp_path):
    """Re-running the same revision must not add jobs again.

    Regenerating the checkpoint output directory must also not cause a second
    dispatch of the same sample or summary jobs: the content revision is
    unchanged, so the dynamic DAG stays as it is.
    """
    workdir = _prepare_case(tmp_path)

    _complete_a_only(workdir)
    _run_workflow(workdir)

    # The sample job switched to the second verified revision. Make it the
    # current checkpoint input and publish it once, which reruns the manifest
    # directory job but must not add any dynamic job.
    (workdir / "manifest.current.tsv").write_text(MANIFEST_V2)
    republished = _run_workflow(workdir)
    assert {job["rule"] for job in republished} <= {"publish", "summary", "all"}, (
        republished
    )
    assert not any(
        os.path.basename(out) in {"A.tsv", "C.tsv", "D.tsv"}
        for job in republished
        for out in job["output"]
    ), republished

    snapshot = {
        str(p.relative_to(workdir)): _sha256(p)
        for p in workdir.rglob("*")
        if p.is_file() and ".snakemake" not in p.parts
    }

    assert _run_workflow(workdir) == []

    # The sample job of C switched the workflow to the second revision. Keep
    # that revision as the current input of the checkpoint, then re-generate
    # the output directory (as a rerun would). The content is identical, so no
    # duplicate dynamic job may be created for the same sample or the summary.
    # Force the checkpoint directory job to run again, as a directory-level
    # rerun would. All dynamic jobs still see the identical manifest, so no
    # sample or summary job may be added a second time.
    published = workdir / "published"
    shutil.rmtree(published)
    regenerated = _run_workflow(workdir, forcerun={"publish"}, targets=["published"])

    dynamic_outputs = [
        os.path.basename(out)
        for job in regenerated
        if job["rule"] != "publish"
        for out in job["output"]
    ]
    # Each dynamic output appears at most once. No entry is appended twice just
    # because the directory checkpoint was regenerated.
    assert len(dynamic_outputs) == len(set(dynamic_outputs)), regenerated
    assert set(dynamic_outputs) <= {"A.tsv", "C.tsv", "D.tsv", "summary.txt"}

    after = {
        str(p.relative_to(workdir)): _sha256(p)
        for p in workdir.rglob("*")
        if p.is_file() and ".snakemake" not in p.parts
    }
    assert after == snapshot


@pytest.mark.skipif(
    sys.platform == "win32", reason="Fixture uses POSIX shell and sha256sum"
)
def test_checkpoint_revision_only_reexpands_changed_branches(tmp_path):
    """A revision that only adds a sample must not rerun unchanged branches.

    The first verified revision already lists A, B and C. Only A is completed.
    After the switch to the second revision, A keeps its input digest, so its
    existing product stays valid and must not be dispatched again.
    """
    workdir = _prepare_case(tmp_path)

    # Complete A under the first verified revision.
    _complete_a_only(workdir)
    a_product = workdir / "results/A.tsv"
    before = a_product.read_bytes()

    # Same first revision, now executed fully; C flips to the second revision,
    # which withdraws B. The unchanged A branch must not be dispatched again.
    dispatched = _run_workflow(workdir)
    outputs = {os.path.basename(out) for job in dispatched for out in job["output"]}
    assert "A.tsv" not in outputs, dispatched
    assert "B.tsv" not in outputs, dispatched
    assert "C.tsv" in outputs, dispatched
    assert "D.tsv" in outputs, dispatched
    assert a_product.read_bytes() == before

    # A's product still records the first revision and its input identity.
    recorded = dict(
        line.split("=", 1)
        for line in a_product.read_text().splitlines()
        if "=" in line
    )
    assert recorded["input"] == "published/inputs/A.da.txt"
    assert recorded["digest"] == "da"
    assert recorded["input-identity"] == _sha256(workdir / recorded["input"])


@pytest.mark.skipif(
    sys.platform == "win32", reason="Fixture uses POSIX shell and sha256sum"
)
def test_old_attempt_finishing_after_switch_cannot_satisfy_new_version(tmp_path):
    """The old attempt of a re-keyed sample must not satisfy the new version.

    The workflow holds the first attempt of C right after it published the
    revised manifest. While it is still running, the new version of C must not
    become ready (its own output path still belongs to the running attempt).
    When the old attempt is released it reports completion; that completion
    still corresponds to the old input and must be discarded so that the new
    version runs and produces the product the summary consumes.
    """
    import threading
    import time

    workdir = _prepare_case(tmp_path)
    _complete_a_only(workdir)

    (workdir / "hold-old-c").write_text("hold")

    result = {}

    def run():
        try:
            result["jobs"] = _run_workflow(workdir)
        except BaseException as e:  # pragma: no cover - reported below
            result["error"] = e

    thread = threading.Thread(target=run, daemon=True)
    thread.start()

    # Wait until the old C attempt published the revised manifest and is
    # blocking before it reports completion.
    published = workdir / "published" / "manifest.tsv"
    deadline = time.time() + 60
    while time.time() < deadline:
        if published.exists() and "C	v2" in published.read_text():
            break
        time.sleep(0.05)
    else:  # pragma: no cover
        (workdir / "hold-old-c").unlink(missing_ok=True)
        pytest.fail("old C attempt did not publish the revised manifest")

    # While the old attempt is still running, it must not have produced a
    # product for the new digest, and no downstream sample job may have taken
    # over its output path.
    assert not (workdir / "results/C.tsv").exists()
    time.sleep(0.3)
    assert not (workdir / "results/C.tsv").exists(), (
        "the new C version must not write while the old attempt is running"
    )

    (workdir / "hold-old-c").unlink()
    thread.join(timeout=120)
    assert not thread.is_alive(), "workflow did not finish after releasing old C"
    assert "error" not in result, result["error"]

    c_jobs = [
        job
        for job in result["jobs"]
        if "results/C.tsv" in job["output"]
    ]
    assert len(c_jobs) == 2, result["jobs"]
    assert [job["input"][0] for job in c_jobs] == [
        "published/inputs/C.dc1.txt",
        "published/inputs/C.dc.txt",
    ], c_jobs

    product = workdir / "results/C.tsv"
    recorded = dict(
        line.split("=", 1)
        for line in product.read_text().splitlines()
        if "=" in line
    )
    assert recorded["input"] == "published/inputs/C.dc.txt"
    assert recorded["digest"] == "dc"
    assert recorded["input-identity"] == _sha256(
        workdir / "published" / "inputs" / "C.dc.txt"
    )
