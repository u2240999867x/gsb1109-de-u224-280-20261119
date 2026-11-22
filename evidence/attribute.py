#!/usr/bin/env python3
"""Recompute attribution from raw trace events.

This deliberately does not read directory listings or stdout conclusions. It
uses only ordered observations: checkpoint verdicts, input-edge order,
candidate versus constraint-valid producers, file opens, and artifact lineage.
"""

from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
CASE_DIR = HERE / "rounds"


def load(case):
    path = CASE_DIR / case / "trace.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def rel(path, root):
    try:
        return str(Path(path).relative_to(root))
    except ValueError:
        return path


def analyze(case, root):
    events = load(case)
    ordered = sorted(events, key=lambda e: e.get("seq", 0))

    checkpoint = []
    for event in ordered:
        if event["event"] == "checkpoint_get":
            checkpoint.append(
                {
                    "rule": event["rule"],
                    "wildcards": event["wildcards"],
                    "verdict": event["verdict"],
                    "missing": [rel(p, root) for p in event["missing_output"]],
                }
            )

    # The final verdict per (rule, wildcards) is the one that governs the round.
    final = {}
    for item in checkpoint:
        final[(item["rule"], tuple(sorted(item["wildcards"].items())))] = item

    edges = [e for e in ordered if e["event"] == "input_edge"]
    first_missing = next(
        (e for e in edges if e["status"] == "first_missing"), None
    )
    missing = [e for e in ordered if e["event"] == "missing_input"]
    exceptions = [e for e in ordered if e["event"] == "exception"]

    selections = [
        e
        for e in ordered
        if e["event"] == "producer_selection"
        and e.get("rejected_by_regex")
    ]
    constraint_rejections = []
    for event in selections:
        for detail in event.get("details", []):
            for product in detail.get("products", []):
                if product.get("matches") is False and product.get("constraints"):
                    constraint_rejections.append(
                        {
                            "rule": detail["rule"],
                            "target": rel(event["target"], root),
                            "constraints": product["constraints"],
                            "pattern": rel(product["pattern"], root),
                        }
                    )

    opens = [e for e in ordered if e["event"] == "file_open" and e["path"].endswith(".tsv")]
    reads = [e for e in ordered if e["event"] == "read_source"]
    artifacts = [e for e in ordered if e["event"] == "artifact"]

    return {
        "case": case,
        "checkpoint_final": [
            {
                "rule": key[0],
                "wildcards": dict(key[1]),
                "verdict": value["verdict"],
                "missing": value["missing"],
            }
            for key, value in final.items()
        ],
        "first_failed_input_edge": {
            "rule": first_missing["rule"] if first_missing else None,
            "index": first_missing["index"] if first_missing else None,
            "path": rel(first_missing["path"], root) if first_missing else None,
        },
        "missing_input": [
            {"rule": e["rule"], "files": [rel(p, root) for p in e["files"]]}
            for e in missing
        ],
        "exception_classes": sorted({e["exception_class"] for e in exceptions}),
        "constraint_rejections": constraint_rejections,
        "roster_opens": [
            {"path": rel(e["path"], root), "mode": e["mode"]} for e in opens
        ],
        "read_sources": [
            {
                "path": rel(e["path"], root),
                "exists": e["exists"],
                "digest": e.get("digest"),
            }
            for e in reads
        ],
        "artifacts": [
            {
                "path": rel(e["path"], root),
                "role": e["role"],
                "derived_from": rel(e["derived_from"], root) if e.get("derived_from") else None,
            }
            for e in artifacts
        ],
    }


def main():
    root = str(HERE.parent)
    cases = sorted(p.name for p in CASE_DIR.iterdir() if p.is_dir())
    report = [analyze(case, root) for case in cases]
    (HERE / "attribution.json").write_text(json.dumps(report, indent=2) + "\n")
    for item in report:
        edge = item["first_failed_input_edge"]
        print(
            f"{item['case']}: first_failed={edge['rule']}#{edge['index']} "
            f"{edge['path']} classes={item['exception_classes']}"
        )


if __name__ == "__main__":
    main()
