"""Turn the raw evidence into explicit archive decisions.

This module answers, per pair and per layout:

* what can be concluded from the online evidence (header + block extrema +
  one caller window) *without* using a read that exceeded the budget;
* what the offline full decode shows;
* whether the minimal per-block supplement (fill count + exact non-fill
  extrema + gcd, still not the caller window) would separate the pair;
* which input each archivist must request.

The checks are defined by properties (block order, physical offsets, window
position), not by hard-coded record numbers.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import numpy as np

from . import analysis, cases, cdf5

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
REPORTS = HERE / "reports"


def _online_summary(path: Path, window: tuple[int, int]) -> dict[str, Any]:
    r = cdf5.Reader.open(path)
    r.window = window
    return r.catalog(window)


def _in_window_evidence(path: Path, window: tuple[int, int]) -> dict[str, Any]:
    """Everything obtained without leaving the window."""
    r = cdf5.Reader.open(path)
    r.window = window
    # The window read is admissible by construction.
    values = r.read("time", slice(*window))
    return {
        "window_raw": [int(v) for v in values.tolist()],
        "window_abs_admissible": all(
            analysis._unit_fit_ns(int(v) * analysis._storage_multiplier(
                "microseconds" if "microseconds" in r.visible_attrs("time").get("units", "") else "nanoseconds"
            ), "ns")[1]
            for v in values
        ),
        "header_OK": True,
        "block_extrema": r.block_extrema("time"),
    }


def _block_facts(path: Path, case: cases.Case, window: tuple[int, int]) -> list[dict[str, Any]]:
    r = cdf5.Reader.open(path)
    ns = r.read_all("time") * analysis._storage_multiplier(case.units)
    fill = case.fill_code
    out = []
    for b in r.block_extrema("time"):
        a, z = b["start"], b["stop"]
        chunk_ns = ns[a:z]
        keep = chunk_ns != (fill * analysis._storage_multiplier(case.units) if fill is not None else None)
        data = chunk_ns[keep] if fill is not None else chunk_ns
        overlaps = not (z <= window[0] or a >= window[1])
        out.append(
            {
                "block": b["block"],
                "start": a,
                "stop": z,
                "overlaps_window": overlaps,
                "raw_min": b["min"],
                "raw_max": b["max"],
                "min_ns": int(data.min()) if data.size else None,
                "max_ns": int(data.max()) if data.size else None,
                "fill_count": int((chunk_ns == fill * analysis._storage_multiplier(case.units)).sum()) if fill is not None else 0,
                "gcd_ns": analysis._gcd(data) if data.size else None,
            }
        )
    return out


def _pair(family: str, variants: tuple[str, str], layout: str) -> dict[str, Any]:
    left = next(c for c in cases.all_cases() if c.name == f"{family}_{variants[0]}" and c.layout == layout)
    right = next(c for c in cases.all_cases() if c.name == f"{family}_{variants[1]}" and c.layout == layout)
    lpath = DATA / f"{left.name}_{layout}.cdf5"
    rpath = DATA / f"{right.name}_{layout}.cdf5"
    window = left.window

    lsum = _online_summary(lpath, window)
    rsum = _online_summary(rpath, window)
    lfacts = _block_facts(lpath, left, window)
    rfacts = _block_facts(rpath, right, window)
    loff = analysis._offline(lpath, left)
    roff = analysis._offline(rpath, right)

    unread = [b for b in lfacts if not b["overlaps_window"]]
    online_agrees = {
        "header_equal": lsum["variables"] == rsum["variables"],
        "block_extrema_equal": lsum["variables"]["time"]["blocks"] == rsum["variables"]["time"]["blocks"],
        "window_equal": lsum["window"] == rsum["window"],
        "unread_blocks_equal": all(
            (a["raw_min"], a["raw_max"], a["start"], a["stop"])
            == (b["raw_min"], b["raw_max"], b["start"], b["stop"])
            for a, b in zip(lfacts, rfacts, strict=True)
            if not a["overlaps_window"]
        ),
    }

    supplement_distinguishes = {
        "fill": loff["supplement"]["per_block"] != roff["supplement"]["per_block"],
        "fill_counts": [b["fill_count"] for b in loff["supplement"]["per_block"]]
        != [b["fill_count"] for b in roff["supplement"]["per_block"]],
        "subsecond_counts": [b["subsecond_count"] for b in loff["supplement"]["per_block"]]
        != [b["subsecond_count"] for b in roff["supplement"]["per_block"]],
        "nonfill_max": [b["max_nonfill_ns"] for b in loff["supplement"]["per_block"]]
        != [b["max_nonfill_ns"] for b in roff["supplement"]["per_block"]],
        "gcd": [b["gcd_nonfill_ns"] for b in loff["supplement"]["per_block"]]
        != [b["gcd_nonfill_ns"] for b in roff["supplement"]["per_block"]],
    }
    return {
        "family": family,
        "layout": layout,
        "members": [left.name, right.name],
        "window": list(window),
        "online_agrees": online_agrees,
        "all_online_agrees": all(online_agrees.values()),
        "unread_blocks": unread,
        "left": {
            "verdict": loff["verdict"],
            "supplement_verdict": loff["supplement_verdict"],
            "whole_file_units": loff["whole_file_units"],
            "cf_legal_units": loff["cf_legal_units"],
            "segments": loff["segments"],
            "fill": loff["fill"],
            "block_facts": lfacts,
        },
        "right": {
            "verdict": roff["verdict"],
            "supplement_verdict": roff["supplement_verdict"],
            "whole_file_units": roff["whole_file_units"],
            "cf_legal_units": roff["cf_legal_units"],
            "segments": roff["segments"],
            "fill": roff["fill"],
            "block_facts": rfacts,
        },
        "decisions_differ": loff["decision"]["action_key"] != roff["decision"]["action_key"],
        "supplement_decision_matches_full": [
            loff["supplement_decision"]["action_key"] == loff["decision"]["action_key"],
            roff["supplement_decision"]["action_key"] == roff["decision"]["action_key"],
        ],
        "left_action": loff["decision"]["action"],
        "right_action": roff["decision"]["action"],
        "supplement_distinguishes": supplement_distinguishes,
        "supplement_sufficient": any(supplement_distinguishes.values()),
    }


def _minimal_supplement_case() -> dict[str, Any]:
    """Prove a per-block supplement is enough even with permuted storage.

    We rebuild pair members with reversed physical block order and a shifted
    window, then re-derive both decisions from the supplement alone.
    """
    from . import invariance

    out = {}
    for family, variants in (
        ("pair1", ("tail_1ns", "whole_second")),
        ("pair2", ("fill_only", "fill_plus_real_far")),
    ):
        for layout in ("dim", "aux"):
            left = next(c for c in cases.all_cases() if c.name == f"{family}_{variants[0]}" and c.layout == layout)
            right = next(c for c in cases.all_cases() if c.name == f"{family}_{variants[1]}" and c.layout == layout)
            lpath = invariance.PERM_DATA / f"{left.name}_{layout}_perm.cdf5"
            rpath = invariance.PERM_DATA / f"{right.name}_{layout}_perm.cdf5"
            if not lpath.exists():
                invariance._build_permuted(left)
                invariance._build_permuted(right)
            loff = analysis._offline(lpath, left)
            roff = analysis._offline(rpath, right)
            out[f"{family}_{layout}"] = {
                "supplement_verdict": [loff["supplement_verdict"], roff["supplement_verdict"]],
                "supplement_units": [
                    loff["supplement"]["intersection_units"],
                    roff["supplement"]["intersection_units"],
                ],
                "action_key": [loff["decision"]["action_key"], roff["decision"]["action_key"]],
                "distinguishes": loff["decision"]["action_key"] != roff["decision"]["action_key"],
            }
    return out


def main() -> int:
    result: dict[str, Any] = {"pairs": {}, "supplement_under_permutation": _minimal_supplement_case()}
    for family, variants in (
        ("pair1", ("tail_1ns", "whole_second")),
        ("pair2", ("fill_only", "fill_plus_real_far")),
    ):
        for layout in ("dim", "aux"):
            result["pairs"][f"{family}_{layout}"] = _pair(family, variants, layout)

    (REPORTS / "decisions.json").write_text(
        json.dumps(result, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    lines = ["Decision evidence", ""]
    for key, p in result["pairs"].items():
        lines.append(f"[{key}] all_online_agrees={p['all_online_agrees']}")
        lines.append(f"  left  verdict={p['left']['verdict']} action={p['left_action']} cf={p['left']['cf_legal_units']}")
        lines.append(f"  right verdict={p['right']['verdict']} action={p['right_action']} cf={p['right']['cf_legal_units']}")
        lines.append(f"  decisions_differ={p['decisions_differ']} supplement_distinguishes={p['supplement_distinguishes']}")
        lines.append("")
    lines.append("Supplement under permuted blocks and shifted window:")
    for key, v in result["supplement_under_permutation"].items():
        lines.append(f"  {key}: {v['supplement_verdict']} distinguishes={v['distinguishes']}")
    (REPORTS / "decisions.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print("wrote reports/decisions.json")
    return 0


if __name__ == "__main__":
    sys.exit(main())
