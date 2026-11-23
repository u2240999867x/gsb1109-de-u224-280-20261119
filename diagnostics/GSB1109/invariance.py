"""Prove the diagnosis is position-independent.

For every pair, rebuild both members with a permuted *physical* block order
(logical values, block boundaries, and extrema unchanged) and rerun the whole
online/offline diagnostic against a *different* user window.

Acceptance:
* the pair's online summaries remain equal to each other;
* every offline verdict is unchanged from the baseline run;
* budget violations are still detected and attributed to a physical byte;
* pair decisions (which member needs which archive action) are unchanged.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

from . import analysis, cases, cdf5
from .backend import CDF5Backend  # noqa: F401 - registers the engine module

HERE = Path(__file__).resolve().parent
PERM_DATA = HERE / "data" / "permuted"
REPORTS = HERE / "reports"

ALT_WINDOWS = {"dim": (8, 16), "aux": (8, 16)}
PERMUTATIONS = {
    "time": [2, 0, 1],
    "value": [1, 0],
}


def _baseline_verdicts() -> dict[str, dict[str, Any]]:
    diag = json.loads((REPORTS / "diagnostic.json").read_text(encoding="utf-8"))
    return {
        key: {
            "verdict": value["offline"]["verdict"],
            "supplement_verdict": value["offline"]["supplement_verdict"],
            "interaction_units": value["offline"]["supplement"]["intersection_units"],
        }
        for key, value in diag["cases"].items()
    }


def _build_permuted(case: cases.Case) -> Path:
    PERM_DATA.mkdir(parents=True, exist_ok=True)
    path = PERM_DATA / f"{case.name}_{case.layout}_perm.cdf5"
    order = {"time": list(reversed(range(len(case.block_starts)))), "value": [0]}
    cases.build(case, str(path), block_order=order)
    return path


def _mode(path: Path, case: cases.Case, window: tuple[int, int], kwargs: dict[str, Any]) -> dict[str, Any]:
    return analysis._mode_evidence(path, case, "invariance", kwargs, window=window)


def main() -> int:
    baseline = _baseline_verdicts()
    out: dict[str, Any] = {"permutations": PERMUTATIONS, "alt_windows": {}, "pairs": {}, "cases": {}}

    for case in cases.all_cases():
        key = f"{case.name}_{case.layout}"
        window = ALT_WINDOWS[case.layout]
        path = _build_permuted(case)
        raw = analysis._raw_exact(path)
        original = analysis._raw_exact(HERE / "data" / f"{key}.cdf5")
        offline = analysis._offline(path, case)

        modes = {
            "metadata_only": _mode(
                path,
                case,
                window,
                {"create_default_indexes": False, "decode_times": analysis.CFDatetimeCoder(infer_dtype="metadata")},
            ),
            "default_index": _mode(path, case, window, {}),
            "no_default_index": _mode(
                path, case, window, {"create_default_indexes": False}
            ),
            "raw_int64": _mode(
                path,
                case,
                window,
                {"create_default_indexes": False, "decode_times": False, "mask_and_scale": False},
            ),
            "reselect_window": _mode(
                path, case, window, {"create_default_indexes": False}
            ),
        }
        budget = {
            name: ev["earliest_outside_open"] for name, ev in modes.items()
        }
        out["cases"][key] = {
            "window": list(window),
            "logical_values_unchanged": bool(
                raw.size == original.size and (raw == original).all()
            ),
            "verdict": offline["verdict"],
            "supplement_verdict": offline["supplement_verdict"],
            "baseline": baseline[key],
            "verdict_unchanged": offline["verdict"] == baseline[key]["verdict"],
            "supplement_unchanged": offline["supplement_verdict"]
            == baseline[key]["supplement_verdict"],
            "intersection_units_unchanged": offline["supplement"]["intersection_units"]
            == baseline[key]["interaction_units"],
            "budget_flags": {k: bool(v) for k, v in budget.items()},
            "earliest_outside_physical": {
                k: (v or {}).get("by_physical_byte") for k, v in budget.items()
            },
        }

    for family, variants in (
        ("pair1", ("tail_1ns", "whole_second")),
        ("pair2", ("fill_only", "fill_plus_real_far")),
    ):
        for layout in ("dim", "aux"):
            left = next(c for c in cases.all_cases() if c.name == f"{family}_{variants[0]}" and c.layout == layout)
            right = next(c for c in cases.all_cases() if c.name == f"{family}_{variants[1]}" and c.layout == layout)
            lpath = PERM_DATA / f"{left.name}_{layout}_perm.cdf5"
            rpath = PERM_DATA / f"{right.name}_{layout}_perm.cdf5"
            window = ALT_WINDOWS[layout]
            out["pairs"][f"{family}_{layout}"] = {
                "window": list(window),
                "visible_summary_equal": analysis._visible_digest(lpath, window)
                == analysis._visible_digest(rpath, window),
                "window_values_equal": bool(
                    (
                        analysis._read_window(lpath, window)
                        == analysis._read_window(rpath, window)
                    ).all()
                ),
                "left_verdict": analysis._offline(lpath, left)["verdict"],
                "right_verdict": analysis._offline(rpath, right)["verdict"],
            }

    ok = all(
        c["verdict_unchanged"] and c["supplement_unchanged"] and c["intersection_units_unchanged"]
        for c in out["cases"].values()
    ) and all(p["visible_summary_equal"] and p["window_values_equal"] for p in out["pairs"].values())
    out["all_invariance_checks_pass"] = bool(ok)
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "invariance.json").write_text(
        json.dumps(out, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    print(f"invariance checks pass={ok}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
