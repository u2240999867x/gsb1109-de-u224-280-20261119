"""Offline tests for bitemporal registration attribution."""

from pathlib import Path

import pandas as pd
import pytest

from pyopensky.attribution import (
    ATTRIBUTED,
    CONFLICT,
    INVALID_TIME,
    UNREGISTERED,
    AttributionEngine,
    RegistryEntry,
    diff_reports,
    load_observations,
    load_registry,
    run_report,
)
from pyopensky.time import to_datetime

DATA = Path(__file__).resolve().parents[1] / "data" / "attribution"

HANDOVER = "2026-05-15T12:00:00Z"


def make_engine() -> AttributionEngine:
    return AttributionEngine(load_registry(DATA / "registry.csv"))


def make_observations() -> pd.DataFrame:
    return load_observations(DATA / "observations.csv")


def by_obs(report: pd.DataFrame) -> pd.Series:
    return report.set_index("obs_id")


def test_demo_data_loads_without_network() -> None:
    entries = load_registry(DATA / "registry.csv")
    observations = load_observations(DATA / "observations.csv")
    assert len(entries) == 5
    assert len(observations) == 4
    assert observations["lastposupdate"].dt.tz is not None


def test_half_open_handover_at_noon() -> None:
    report = make_engine().attribute(make_observations())
    rows = by_obs(report)
    before = rows.loc["obs-1"]
    at_noon = rows.loc["obs-2"]
    assert before["position_time"] == to_datetime("2026-05-15T11:59:59Z")
    assert before["adopted"] == "A"
    assert at_noon["position_time"] == to_datetime(HANDOVER)
    assert at_noon["adopted"] == "C"


def test_record_time_is_ignored_for_matching() -> None:
    # Both samples are collected after the 12:00 handover, yet matching uses
    # lastposupdate, not the later record_time of the collection system.
    report = make_engine().attribute(make_observations())
    rows = by_obs(report)
    assert rows.loc["obs-1", "record_time"] > to_datetime(HANDOVER)
    assert rows.loc["obs-1", "adopted"] == "A"
    assert rows.loc["obs-2", "record_time"] > to_datetime(HANDOVER)


def test_first_report_attributes_b_with_two_evidences() -> None:
    report = make_engine().as_of("2026-05-15T12:30:00Z").attribute(
        make_observations()
    )
    noon = by_obs(report).loc["obs-2"]
    assert noon["status"] == ATTRIBUTED
    assert noon["adopted"] == "B"
    assert set(noon["evidence_ids"]) == {"r-b1", "r-b2"}
    assert noon["superseded_evidence"] == ()


def test_partial_correction_produces_b_c_conflict() -> None:
    report = make_engine().as_of("2026-05-16T12:30:00Z").attribute(
        make_observations()
    )
    noon = by_obs(report).loc["obs-2"]
    assert noon["status"] == CONFLICT
    assert pd.isna(noon["adopted"])
    assert set(noon["registrations"]) == {"B", "C"}
    assert set(noon["evidence_ids"]) == {"r-c1", "r-b2"}
    # The regulator's own B entry is replaced; the foreign channel is alive.
    replaced_text = " ".join(noon["superseded_evidence"])
    assert "r-b1" in replaced_text and "replaced_by r-c1" in replaced_text


def test_full_correction_attributes_c() -> None:
    report = make_engine().as_of("2026-05-16T18:00:00Z").attribute(
        make_observations()
    )
    noon = by_obs(report).loc["obs-2"]
    assert noon["status"] == ATTRIBUTED
    assert noon["adopted"] == "C"
    assert noon["evidence_ids"] == ("r-c1",)
    replaced = " ".join(noon["superseded_evidence"])
    assert "r-b1" in replaced and "r-b2" in replaced
    assert "replaced_by r-b3" in replaced


def test_late_foreign_record_does_not_cover_noon() -> None:
    # r-b3 becomes valid only at 12:01, so it cannot support B at 12:00:00.
    later_obs = pd.DataFrame(
        [
            {
                "obs_id": "obs-1201",
                "icao24": "abc123",
                "lastposupdate": to_datetime("2026-05-15T12:01:00Z"),
                "record_time": to_datetime("2026-05-15T12:01:30Z"),
                "lat": 48.7,
                "lon": 2.38,
            }
        ]
    )
    report = make_engine().as_of("2026-05-16T18:00:00Z").attribute(later_obs)
    row = by_obs(report).loc["obs-1201"]
    assert row["status"] == CONFLICT
    assert set(row["registrations"]) == {"B", "C"}


def test_old_cutoff_cannot_see_later_corrections() -> None:
    old = make_engine().as_of("2026-05-15T12:30:00Z")
    assert set(old.entry_ids) == {"r-a", "r-b1", "r-b2"}
    report = old.attribute(make_observations())
    noon = by_obs(report).loc["obs-2"]
    assert noon["adopted"] == "B"
    assert all("r-c1" not in str(item) for item in noon["evidence"])


def test_invalid_and_unregistered_are_listed_separately() -> None:
    report = make_engine().as_of("2026-05-16T18:00:00Z").attribute(
        make_observations()
    )
    rows = by_obs(report)
    invalid = rows.loc["obs-3"]
    unregistered = rows.loc["obs-4"]
    assert invalid["status"] == INVALID_TIME
    assert pd.isna(invalid["adopted"]) and invalid["evidence_ids"] == ()
    assert unregistered["status"] == UNREGISTERED
    assert pd.isna(unregistered["adopted"])


def test_every_result_is_traceable_to_entries() -> None:
    report = make_engine().as_of("2026-05-16T18:00:00Z").attribute(
        make_observations()
    )
    known_ids = set(make_engine().entry_ids)
    for row in report.itertuples(index=False):
        assert set(row.evidence_ids) <= known_ids
        for label in (*row.evidence, *row.superseded_evidence):
            head = label.split("@", 1)[0]
            assert head in known_ids


def test_report_does_not_depend_on_input_order() -> None:
    entries = load_registry(DATA / "registry.csv")
    observations = make_observations()
    baseline = (
        AttributionEngine(entries)
        .as_of("2026-05-16T12:30:00Z")
        .attribute(observations)
    )
    shuffled = AttributionEngine(list(reversed(entries)))
    rerun = shuffled.as_of("2026-05-16T12:30:00Z").attribute(
        observations.iloc[::-1]
    )
    pd.testing.assert_frame_equal(
        rerun.sort_values("obs_id").reset_index(drop=True),
        baseline.sort_values("obs_id").reset_index(drop=True),
    )


def test_diff_reports_flags_changed_attributions() -> None:
    engine = make_engine()
    observations = make_observations()
    first = engine.as_of("2026-05-15T12:30:00Z").attribute(observations)
    final = engine.as_of("2026-05-16T18:00:00Z").attribute(observations)
    changes = diff_reports(first, final).set_index("obs_id")
    assert changes.loc["obs-2", "adopted_earlier"] == "B"
    assert changes.loc["obs-2", "adopted_later"] == "C"
    assert changes.loc["obs-2", "evidence_removed"] == ("r-b1", "r-b2")
    assert changes.loc["obs-2", "evidence_added"] == ("r-c1",)
    assert "obs-1" not in changes.index


def test_run_report_helper_matches_engine() -> None:
    direct = (
        make_engine()
        .as_of("2026-05-16T12:30:00Z")
        .attribute(make_observations())
    )
    helper = run_report(
        DATA / "registry.csv",
        DATA / "observations.csv",
        "2026-05-16T12:30:00Z",
    )
    pd.testing.assert_frame_equal(direct, helper)


def test_supersede_must_target_same_source_and_address() -> None:
    bad = [
        RegistryEntry(
            entry_id="x1",
            icao24="abc123",
            registration="B",
            source="regulator",
            valid_from=HANDOVER,
            published_at="2026-05-15T06:00:00Z",
        ),
        RegistryEntry(
            entry_id="x2",
            icao24="abc123",
            registration="C",
            source="foreign-caa",
            valid_from=HANDOVER,
            published_at="2026-05-16T09:00:00Z",
            supersedes="x1",
        ),
    ]
    with pytest.raises(ValueError, match="same icao24 and source"):
        AttributionEngine(bad)


def test_duplicate_entry_ids_are_rejected() -> None:
    entry = RegistryEntry(
        entry_id="dup",
        icao24="abc123",
        registration="A",
        source="regulator",
        valid_from="2026-05-15T00:00:00Z",
        published_at="2026-05-14T18:00:00Z",
    )
    with pytest.raises(ValueError, match="Duplicate registry entry_id"):
        AttributionEngine([entry, entry])


def test_unknown_superseded_entry_is_rejected() -> None:
    entry = RegistryEntry(
        entry_id="lonely",
        icao24="abc123",
        registration="C",
        source="regulator",
        valid_from=HANDOVER,
        published_at="2026-05-16T09:00:00Z",
        supersedes="missing",
    )
    with pytest.raises(ValueError, match="unknown entry"):
        AttributionEngine([entry])
