#!/usr/bin/env python3
"""Offline reproduction and attribution for a same-path, two-stage failure.

Workflow shape (two checkpoint layers):
  pick_regions : batch list -> regions  (layer 1)
  expand_region: region roster -> real targets (layer 2)
  consume      : real targets -> result
  produce_data : a similar producer whose wildcard constraint allows only a
                 different region

The same absolute path `data/<region>/records.tsv` is reported missing in two
different situations:
  siteA  layer 2 has not completed; its material itself is the missing path.
  siteB  both layers completed; the real roster derived the same path, but the
         only similar producer is constraint-rejected.

Rounds reset from an explicit snapshot (never from leftover directory content)
and change exactly one config switch at a time. Attribution comes from an
opt-in JSONL trace of observations:

  * which checkpoints were judged complete/incomplete,
  * which prefix/suffix candidates matched vs. which survived regex constraints,
  * which input edge was the first missing one,
  * whether the previous-round roster was actually opened, and its digest,
  * a same-log snapshot where the previous roster opens fine yet is not a
    valid basis for this round's recomputation.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent
SNAKEMAKE = WORKSPACE / ".venv" / "bin" / "snakemake"
EVIDENCE = WORKSPACE / "evidence"


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def write_common_inputs():
    (HERE / "batch").mkdir(exist_ok=True)
    (HERE / "roster").mkdir(exist_ok=True)
    (HERE / "previous").mkdir(exist_ok=True)
    (HERE / "batch" / "north.tsv").write_text("north\n")
    (HERE / "roster" / "north.tsv").write_text(
        "# CURRENT round roster (this round's authoritative input)\n"
        "north\tdata/north/records.tsv\n"
    )
    (HERE / "previous" / "north.tsv").write_text(
        "# PREVIOUS round roster: readable, but NOT this round's basis\n"
        "north\tdata/south/records.tsv\n"
    )


def write_snapshot(snapshot: str):
    """Create a site snapshot with deliberate, documented mtime ordering."""
    for path in ("state", "data", "results", "audit", ".snakemake"):
        shutil.rmtree(HERE / path, ignore_errors=True)
    write_common_inputs()
    (HERE / "state").mkdir()
    (HERE / "data").mkdir()
    (HERE / "results").mkdir()

    if snapshot == "layer2_pending":
        # Layer 1 done; layer 2 not run this round, so its material is missing.
        (HERE / "state" / "north.regions").write_text("north\n")
    elif snapshot == "layer2_done_target_missing":
        # Both layers done; the completed layer 2 wrote the derived target.
        (HERE / "state" / "north.regions").write_text("north\n")
        (HERE / "state" / "north.targets").write_text(
            f"{HERE}/data/north/records.tsv\n"
        )
        os.utime(HERE / "state" / "north.targets", None)
    elif snapshot == "stale_targets_from_previous":
        # Log looks identical to a healthy round, but the existing target file
        # was derived from the previous roster, not this round's roster.
        (HERE / "state" / "north.regions").write_text("north\n")
        (HERE / "state" / "north.targets").write_text(
            f"{HERE}/data/south/records.tsv\n"
        )
        os.utime(HERE / "state" / "north.targets", (1700000000, 1700000000))
    elif snapshot == "stale_targets_with_result":
        (HERE / "state" / "north.regions").write_text("north\n")
        (HERE / "state" / "north.targets").write_text(
            f"{HERE}/data/south/records.tsv\n"
        )
        (HERE / "results" / "north.done").write_text("stale result\n")
        os.utime(HERE / "state" / "north.targets", (1700000000, 1700000000))
        os.utime(HERE / "results" / "north.done", (1700000001, 1700000001))
    elif snapshot == "all_done":
        (HERE / "state" / "north.regions").write_text("north\n")
        (HERE / "state" / "north.targets").write_text(
            f"{HERE}/data/north/records.tsv\n"
        )
        (HERE / "data" / "north").mkdir()
        (HERE / "data" / "north" / "records.tsv").write_text("north records\n")
        (HERE / "results" / "north.done").write_text("done\n")
    else:
        raise ValueError(snapshot)


def write_config(round_dir, **overrides):
    config = {
        "batch": "north",
        "region": "north",
        "checkpoint_material": "data",
        "producer_regions": "south",
        "audit_previous": True,
    }
    config.update(overrides)
    path = round_dir / "config.yaml"
    path.write_text("".join(f"{key}: {value}\n" for key, value in config.items()))
    return path


def run_site(dry_run=True, extra_targets=()):
    env = dict(os.environ)
    env["SNAKEMAKE_DIAG_TRACE"] = str(HERE / "trace.jsonl")
    env["SNAKEMAKE_DIAG_WATCH"] = str(HERE)
    completed = subprocess.run(
        [
            str(SNAKEMAKE),
            "--configfile",
            str(HERE / "config.yaml"),
            "--cores",
            "1",
            "--scheduler",
            "greedy",
        ]
        + (["--dry-run"] if dry_run else [])
        + list(extra_targets),
        cwd=HERE,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    return completed


def first_missing_line(output: str):
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if line.startswith("MissingInputException in rule"):
            rule = line.split("rule ", 1)[1].split(" ", 1)[0]
            path = None
            for follow in lines[index:]:
                stripped = follow.strip()
                if stripped.startswith("/") and "affected" not in stripped:
                    path = stripped
                    break
            return rule, path
    return None, None


CASES = [
    # name, snapshot, overrides, expectation
    (
        "1_siteA_layer2_pending",
        "layer2_pending",
        {"checkpoint_material": "data", "producer_regions": "south"},
        {"rule": "expand_region", "checkpoint_complete": False},
    ),
    (
        "2_after_checkpoint_only",
        "layer2_done_target_missing",
        {"checkpoint_material": "roster", "producer_regions": "south"},
        {"rule": "consume", "checkpoint_complete": True},
    ),
    (
        "3_constraint_only_pending",
        "layer2_pending",
        {"checkpoint_material": "data", "producer_regions": "north|south"},
        {"checkpoint_complete": False, "checkpoint_job_scheduled": True},
    ),
    (
        "4_siteB_both_done_constraint_rejects",
        "layer2_done_target_missing",
        {"checkpoint_material": "roster", "producer_regions": "south"},
        {"rule": "consume", "checkpoint_complete": True},
    ),
    (
        "5_both_fixed",
        "layer2_done_target_missing",
        {"checkpoint_material": "roster", "producer_regions": "north|south"},
        {"rule": None},
    ),
    (
        "6_previous_opens_but_not_basis",
        "layer2_done_target_missing",
        {"checkpoint_material": "roster", "producer_regions": "north|south", "audit_previous": True},
        {"rule": None, "previous_read": True},
    ),
    (
        "7_stale_targets_dry_run_skips",
        "stale_targets_with_result",
        {"checkpoint_material": "roster", "producer_regions": "north|south", "audit_previous": True},
        {"rule": None, "dry_run": True, "skips_recompute": True},
    ),
    (
        "8_stale_targets_recomputed",
        "stale_targets_with_result",
        {"checkpoint_material": "roster", "producer_regions": "north|south", "audit_previous": True},
        {"rule": None, "dry_run": False, "recomputed_basis": True},
    ),
]


def summarize(trace_path: Path):
    events = [json.loads(line) for line in trace_path.read_text().splitlines() if line]
    missing = [e for e in events if e["event"] == "missing_input"]
    checkpoint = [e for e in events if e["event"] == "checkpoint_get"]
    selection = [
        e
        for e in events
        if e["event"] == "producer_selection" and "records.tsv" in e.get("target", "")
    ]
    previous = [e for e in events if e["event"] == "read_source"]
    artifacts = [e for e in events if e["event"] == "artifact"]
    return {
        "missing_input": missing,
        "checkpoint_get": checkpoint,
        "producer_selection": selection,
        "previous_read": previous,
        "artifacts": artifacts,
    }


def main():
    if not SNAKEMAKE.exists():
        sys.exit(f"missing snakemake entry point: {SNAKEMAKE}")
    rounds_dir = EVIDENCE / "rounds"
    if rounds_dir.exists():
        shutil.rmtree(rounds_dir)
    rounds_dir.mkdir(parents=True, exist_ok=True)
    summary = []
    for name, snapshot, overrides, expectation in CASES:
        round_dir = rounds_dir / name
        if round_dir.exists():
            shutil.rmtree(round_dir)
        round_dir.mkdir(parents=True)

        write_snapshot(snapshot)
        config_path = write_config(round_dir, **overrides)
        (HERE / "config.yaml").write_text(config_path.read_text())
        if (HERE / "trace.jsonl").exists():
            (HERE / "trace.jsonl").unlink()

        completed = run_site(dry_run=expectation.get("dry_run", True))
        (round_dir / "stdout.txt").write_text(completed.stdout)
        shutil.copyfile(HERE / "trace.jsonl", round_dir / "trace.jsonl")
        shutil.copytree(HERE / "state", round_dir / "state_in", dirs_exist_ok=True)

        rule, path = first_missing_line(completed.stdout)
        observed = summarize(round_dir / "trace.jsonl")
        checkpoint_verdicts = sorted(
            {(e["rule"], e["verdict"]) for e in observed["checkpoint_get"]}
        )
        scheduled = [
            line.split("localcheckpoint ", 1)[1].strip()
            for line in completed.stdout.splitlines()
            if "localcheckpoint " in line
        ]
        record = {
            "case": name,
            "snapshot": snapshot,
            "config": overrides,
            "returncode": completed.returncode,
            "first_missing_rule": rule,
            "first_missing_path": path,
            "expected_rule": expectation.get("rule"),
            "expected_checkpoint_complete": expectation.get("checkpoint_complete"),
            "checkpoint_verdicts": checkpoint_verdicts,
            "checkpoint_jobs_scheduled": scheduled,
            "previous_roster_read": bool(observed["previous_read"]),
            "previous_roster_digest": next(
                (
                    e.get("digest")
                    for e in observed["previous_read"]
                    if e.get("exists") and "/previous/" in e.get("path", "")
                ),
                None,
            ),
            "current_roster_digest": digest(HERE / "roster" / "north.tsv"),
            "previous_roster_digest_actual": digest(HERE / "previous" / "north.tsv"),
            "prefix_suffix_rejected": [
                {
                    "target": e["target"],
                    "candidates": e["prefix_suffix_candidates"],
                    "rejected": e["rejected_by_regex"],
                }
                for e in observed["producer_selection"]
                if e["rejected_by_regex"]
            ],
            "derived_artifacts": observed["artifacts"],
            "targets_content": (HERE / "state" / "north.targets").read_text()
            if (HERE / "state" / "north.targets").exists()
            else None,
        }
        record["intervention"] = {
            "snapshot": snapshot,
            "changed": {
                key: overrides[key]
                for key in ("checkpoint_material", "producer_regions")
                if key in overrides
            },
            "dry_run": expectation.get("dry_run", True),
        }
        summary.append(record)
        print(f"{name}: exit={completed.returncode} rule={rule} path={path}")

    (EVIDENCE / "rounds.json").write_text(json.dumps(summary, indent=2) + "\n")
    for path in ("state", "data", "results", "audit", ".snakemake"):
        shutil.rmtree(HERE / path, ignore_errors=True)
    for path in ("config.yaml", "trace.jsonl"):
        target = HERE / path
        if target.exists():
            target.unlink()


if __name__ == "__main__":
    main()
