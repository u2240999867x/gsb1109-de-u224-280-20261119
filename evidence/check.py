#!/usr/bin/env python3
"""Regression checks over the recorded evidence.

Each assertion corresponds to one requirement of the diagnosis, and reads only
raw stdout plus trace events. No fixture gets a post-hoc cause label.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
ROUNDS = HERE / "rounds"

FAILURES = []


def check(condition, message):
    if condition:
        print(f"PASS  {message}")
    else:
        print(f"FAIL  {message}")
        FAILURES.append(message)


def load(case):
    return json.loads((HERE / "attribution.json").read_text()) if case is None else [
        item for item in json.loads((HERE / "attribution.json").read_text())
        if item["case"] == case
    ][0]


def stdout(case):
    return (ROUNDS / case / "stdout.txt").read_text()


def rounds():
    return json.loads((HERE / "rounds.json").read_text())


def by_case():
    return {item["case"]: item for item in rounds()}


def main():
    data = by_case()
    attribution = {
        item["case"]: item
        for item in json.loads((HERE / "attribution.json").read_text())
    }

    site_a = data["1_siteA_layer2_pending"]
    after_checkpoint = data["2_after_checkpoint_only"]
    constraint_only = data["3_constraint_only_pending"]
    site_b = data["4_siteB_both_done_constraint_rejects"]
    both_fixed = data["5_both_fixed"]
    previous = data["6_previous_opens_but_not_basis"]
    stale_dry = data["7_stale_targets_dry_run_skips"]
    stale_real = data["8_stale_targets_recomputed"]

    path_a = site_a["first_missing_path"]
    path_after = after_checkpoint["first_missing_path"]
    path_b = site_b["first_missing_path"]

    check(bool(path_a) and path_a == path_after == path_b, "same absolute missing path in all failure cases")
    check(
        site_a["first_missing_rule"] == "expand_region",
        "site A fails in the checkpoint layer that reads the material",
    )
    check(
        after_checkpoint["first_missing_rule"] == "consume"
        and site_b["first_missing_rule"] == "consume",
        "after the checkpoint is fixed, attribution moves to the downstream producer stage",
    )
    check(
        site_a["checkpoint_verdicts"] and any(v[1] == "incomplete" for v in site_a["checkpoint_verdicts"]),
        "site A still has an incomplete checkpoint",
    )
    check(
        any(v == ["expand_region", "complete"] for v in after_checkpoint["checkpoint_verdicts"]),
        "site B completed the second checkpoint before deriving the same path",
    )

    check(
        "checkpoint expand_region:" in stdout("3_constraint_only_pending"),
        "widening only the constraint still schedules the pending checkpoint",
    )
    check(
        "Nothing to be done" not in stdout("3_constraint_only_pending"),
        "widening only the constraint does not declare the round delivered",
    )
    check(
        constraint_only["first_missing_rule"] is None,
        "constraint-only round does not fake a missing-target failure",
    )

    rejections = site_b["prefix_suffix_rejected"]
    check(
        any(
            "produce_data" in r["candidates"] and "produce_data" in r["rejected"]
            for r in rejections
        ),
        "the similar producer is only a prefix/suffix candidate and is rejected by its constraint",
    )
    trace_b = [
        json.loads(line)
        for line in (ROUNDS / "4_siteB_both_done_constraint_rejects" / "trace.jsonl").read_text().splitlines()
        if line
    ]
    rejected_details = [
        product
        for event in trace_b
        if event["event"] == "producer_selection"
        for detail in event.get("details", [])
        for product in detail.get("products", [])
        if product.get("constraints") and product.get("matches") is False
    ]
    check(
        bool(rejected_details)
        and all("south" in product["constraints"].get("region", "") for product in rejected_details),
        "the rejecting constraint is the region allow-list, not a content check",
    )

    check(
        attribution["1_siteA_layer2_pending"]["first_failed_input_edge"]["rule"]
        == "expand_region"
        and attribution["1_siteA_layer2_pending"]["first_failed_input_edge"]["index"]
        is not None,
        "the first failing input edge is captured with its index",
    )
    check(
        set(attribution["1_siteA_layer2_pending"]["exception_classes"])
        == {"MissingInputException"}
        and set(attribution["4_siteB_both_done_constraint_rejects"]["exception_classes"])
        == {"MissingInputException"},
        "both sites surface the same exception class",
    )

    check(site_a["previous_roster_read"] is True, "the previous roster was actually opened in site A")
    check(
        site_a["previous_roster_digest"] == site_a["previous_roster_digest_actual"],
        "the recorded previous-roster digest matches the file on disk",
    )
    check(
        site_a["previous_roster_digest"] != site_a["current_roster_digest"],
        "the previous roster differs from the current roster",
    )
    recomputed_artifacts = [
        artifact
        for artifact in stale_real["derived_artifacts"]
        if artifact.get("role") == "targets"
    ]
    check(
        bool(recomputed_artifacts)
        and recomputed_artifacts[0]["derived_from"].endswith("roster/north.tsv"),
        "the recomputed target records the current roster as its derivation basis",
    )
    check(
        recomputed_artifacts
        and recomputed_artifacts[0]["path"].endswith("state/north.targets"),
        "the derived target artifact is recorded separately from the audited previous roster",
    )

    check(
        stale_dry["targets_content"] is not None
        and stale_dry["targets_content"].rstrip().endswith("data/south/records.tsv"),
        "snapshot 7: dry-run keeps the stale target derived from the old roster",
    )
    check(
        stale_real["targets_content"] is not None
        and stale_real["targets_content"].rstrip().endswith("data/north/records.tsv"),
        "snapshot 8: the same round recomputes from the current roster",
    )
    check(
        "Updated input files" in stdout("8_stale_targets_recomputed"),
        "snapshot 8 shows the recompute trigger is the current roster's mtime",
    )
    check(
        previous["returncode"] == 0 and previous["previous_roster_read"],
        "the control snapshot opens the old roster but still succeeds from the current roster",
    )

    check(
        both_fixed["returncode"] == 0 and both_fixed["first_missing_rule"] is None,
        "fixing both switches completes the workflow",
    )

    print()
    if FAILURES:
        print(f"{len(FAILURES)} check(s) failed")
        return 1
    print("all checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
