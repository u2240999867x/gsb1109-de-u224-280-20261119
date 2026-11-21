"""Offline demo of bitemporal registration attribution.

Run with:

    uv run python scripts/demo_attribution.py

The same batch of positions is replayed against three registry cutoffs so
that the first daily report, the partially corrected rerun and the fully
corrected rerun can be compared. No network access is required.
"""

from pathlib import Path

import pandas as pd

from pyopensky.attribution import (
    INVALID_TIME,
    UNREGISTERED,
    AttributionEngine,
    diff_reports,
    load_observations,
    load_registry,
)

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / "data" / "attribution"

CUTOFFS = {
    "first report (2026-05-15 12:30Z)": "2026-05-15T12:30:00Z",
    "after regulator correction (2026-05-16 12:30Z)": "2026-05-16T12:30:00Z",
    "after late foreign-caa record (2026-05-16 18:00Z)": "2026-05-16T18:00:00Z",
}

SHOW_COLUMNS = [
    "obs_id",
    "position_time",
    "status",
    "adopted",
    "registrations",
    "evidence",
    "superseded_evidence",
]


def print_report(title: str, report: pd.DataFrame) -> None:
    print(f"\n=== {title} ===")
    for status in (
        "attributed",
        "conflict",
        UNREGISTERED,
        INVALID_TIME,
    ):
        subset = report.loc[report["status"] == status, SHOW_COLUMNS]
        if subset.empty:
            continue
        print(f"-- {status} --")
        print(subset.to_string(index=False))


def main() -> None:
    entries = load_registry(DATA / "registry.csv")
    observations = load_observations(DATA / "observations.csv")
    full_engine = AttributionEngine(entries)

    reports: dict[str, pd.DataFrame] = {}
    for title, cutoff in CUTOFFS.items():
        reports[title] = full_engine.as_of(cutoff).attribute(observations)
        print_report(title, reports[title])

    first_title, second_title, third_title = CUTOFFS
    first, second, third = (reports[title] for title in CUTOFFS)

    print("\n=== changes versus the first report ===")
    print(diff_reports(first, third).to_string(index=False))

    shuffled = AttributionEngine(list(reversed(entries)))
    reordered = shuffled.as_of(CUTOFFS[third_title]).attribute(observations)
    pd.testing.assert_frame_equal(reordered, third)
    print("\nInput order check: shuffled registry rows yield the same report.")

    old_cutoff = full_engine.as_of(CUTOFFS[first_title])
    assert "r-c1" not in old_cutoff.entry_ids
    assert "r-b3" not in old_cutoff.entry_ids
    print("Old cutoff check: later correction rows are invisible to the rerun.")

    handover = third.set_index("obs_id")
    assert handover.loc["obs-1", "adopted"] == "A"
    assert handover.loc["obs-2", "adopted"] == "C"
    print("Half-open interval check: 11:59:59 stays A, 12:00 becomes C.")


if __name__ == "__main__":
    main()
