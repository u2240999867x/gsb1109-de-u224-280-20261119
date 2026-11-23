"""End-to-end regression tests for the GSB1109 diagnostic.

Run from the repository root:

    .venv/bin/python -m pytest diagnostics/GSB1109/tests -q
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from diagnostics.GSB1109 import analysis, cases, cdf5

HERE = Path(__file__).resolve().parents[1]
DATA = HERE / "data"
REPORTS = HERE / "reports"


@pytest.fixture(scope="session", autouse=True)
def built() -> None:
    if not (HERE / "expected" / "manifest.json").exists():
        from diagnostics.GSB1109 import build_cases

        build_cases.main()
    if not (REPORTS / "diagnostic.json").exists():
        from diagnostics.GSB1109 import analysis as a

        a.main()
    if not (REPORTS / "invariance.json").exists():
        from diagnostics.GSB1109 import invariance

        invariance.main()
    if not (REPORTS / "decisions.json").exists():
        from diagnostics.GSB1109 import decisions

        decisions.main()


def _case(name: str, layout: str) -> cases.Case:
    return next(c for c in cases.all_cases() if c.name == name and c.layout == layout)


def test_roundtrip_is_exact_int64() -> None:
    for case in cases.all_cases():
        path = DATA / f"{case.name}_{case.layout}.cdf5"
        reader = cdf5.Reader.open(path)
        raw = reader.read_all("time")
        assert raw.dtype == np.dtype("int64")
        np.testing.assert_array_equal(raw, case.values)


def test_block_extrema_are_real() -> None:
    for case in cases.all_cases():
        path = DATA / f"{case.name}_{case.layout}.cdf5"
        reader = cdf5.Reader.open(path)
        raw = reader.read_all("time")
        for block in reader.block_extrema("time"):
            chunk = raw[block["start"] : block["stop"]]
            assert block["min"] == int(chunk.min())
            assert block["max"] == int(chunk.max())


@pytest.mark.parametrize("layout", ["dim", "aux"])
def test_pair1_hidden_nanosecond_is_outside_window(layout: str) -> None:
    left = _case("pair1_tail_1ns", layout)
    right = _case("pair1_whole_second", layout)
    lraw = analysis._raw_exact(DATA / f"{left.name}_{layout}.cdf5")
    rraw = analysis._raw_exact(DATA / f"{right.name}_{layout}.cdf5")
    diff = np.flatnonzero(lraw != rraw)
    assert diff.tolist() == [17]
    assert int(lraw[17]) - int(rraw[17]) == 1
    start, stop = left.window
    assert not (start <= 17 < stop)


@pytest.mark.parametrize("layout", ["dim", "aux"])
def test_pair1_visible_summaries_are_identical(layout: str) -> None:
    left = _case("pair1_tail_1ns", layout)
    right = _case("pair1_whole_second", layout)
    assert cdf5.Reader.open(DATA / f"{left.name}_{layout}.cdf5").catalog_digest(
        left.window
    ) == cdf5.Reader.open(DATA / f"{right.name}_{layout}.cdf5").catalog_digest(
        right.window
    )


@pytest.mark.parametrize("layout", ["dim", "aux"])
def test_pair2_visible_summaries_are_identical(layout: str) -> None:
    left = _case("pair2_fill_only", layout)
    right = _case("pair2_fill_plus_real_far", layout)
    assert cdf5.Reader.open(DATA / f"{left.name}_{layout}.cdf5").catalog_digest(
        left.window
    ) == cdf5.Reader.open(DATA / f"{right.name}_{layout}.cdf5").catalog_digest(
        right.window
    )


@pytest.mark.parametrize("layout", ["dim", "aux"])
def test_pair2_far_record_is_under_fill_maximum(layout: str) -> None:
    right = _case("pair2_fill_plus_real_far", layout)
    reader = cdf5.Reader.open(DATA / f"{right.name}_{layout}.cdf5")
    raw = reader.read_all("time")
    fill = right.fill_code
    assert fill is not None
    block = reader.block_extrema("time")[-1]
    assert block["max"] == fill
    hidden = raw[block["start"] : block["stop"]]
    real = hidden[hidden != fill]
    assert int(real.max()) > 0 and int(real.max()) < fill


def test_default_index_reads_outside_window_are_flagged() -> None:
    diag = json.loads((REPORTS / "diagnostic.json").read_text(encoding="utf-8"))
    c = diag["cases"]["pair1_tail_1ns_dim"]
    for mode in ("default_index", "no_default_index", "load_at_open"):
        ev = c["online"][mode]
        assert ev["earliest_outside_open"]["budget_exceeded"] is True
        assert ev["earliest_outside_open"]["by_sequence"]["phase"] == "dtype_inference"
    # Metadata-only inference performs no data reads at all.
    assert c["online"]["metadata_only"]["reads_on_open"] == []


def test_metadata_inference_is_read_free_for_standard_calendar() -> None:
    diag = json.loads((REPORTS / "diagnostic.json").read_text(encoding="utf-8"))
    for key, case in diag["cases"].items():
        if "cal360" in key:
            continue
        assert case["online"]["metadata_only"]["reads_on_open"] == [], key


def test_reselection_is_stable_and_recounts_reads() -> None:
    diag = json.loads((REPORTS / "diagnostic.json").read_text(encoding="utf-8"))
    for key, case in diag["cases"].items():
        ev = case["online"]["reselect_window"]
        assert ev["selections_identical"] is True, key
        assert ev["new_reads_on_second"] == 2, key
        assert ev["budget_admissible_selection"] is True, key


def test_calendar_control_is_calendar_not_dtype() -> None:
    diag = json.loads((REPORTS / "diagnostic.json").read_text(encoding="utf-8"))
    cal = diag["library_probes"]["calendar_control"]
    assert cal["as_360_day"].startswith("2100-02-30")
    assert cal["as_standard"].startswith("2100-03-01")
    assert cal["same_int64_different_date"] is True


def test_360_day_preserves_feb30() -> None:
    diag = json.loads((REPORTS / "diagnostic.json").read_text(encoding="utf-8"))
    c = diag["cases"]["cal360_feb30_dim"]
    w = c["online"]["metadata_only"]["window"]
    assert any("2100-02-30" in str(v) for v in w["values"])
    assert c["offline"]["decision"]["action_key"][0] == "preserve_original_calendar"


def test_offline_verdicts_match_numpy_units() -> None:
    diag = json.loads((REPORTS / "diagnostic.json").read_text(encoding="utf-8"))
    assert diag["cases"]["pair1_whole_second_dim"]["offline"]["whole_file_units"] == [
        "s",
        "ms",
        "us",
    ]
    assert diag["cases"]["pair1_tail_1ns_dim"]["offline"]["whole_file_units"] == []
    assert len(diag["cases"]["pair1_tail_1ns_dim"]["offline"]["segments"]) == 2


def test_supplement_decision_matches_full_decode() -> None:
    dec = json.loads((REPORTS / "decisions.json").read_text(encoding="utf-8"))
    for key, pair in dec["pairs"].items():
        assert pair["supplement_decision_matches_full"] == [True, True], key


def test_library_hazard_is_independent_of_container() -> None:
    diag = json.loads((REPORTS / "diagnostic.json").read_text(encoding="utf-8"))
    probes = diag["library_probes"]
    assert probes["math"]["absolute_datetime64_ns_overflows"] is True
    assert probes["math"]["relative_timedelta64_ns_ok"] is True
    assert probes["cftime_microsecond_header"]["ok"] is True
    assert "2300-01-01" in probes["cftime_microsecond_header"]["values"][1]
    hazard = probes["silent_object_cast"]
    assert hazard["plain_xarray_only"] is True
    assert hazard["corrupted"] is True
    assert hazard["values"][1].startswith("1715-")


def test_pairs_force_different_actions() -> None:
    dec = json.loads((REPORTS / "decisions.json").read_text(encoding="utf-8"))
    for key, pair in dec["pairs"].items():
        assert pair["all_online_agrees"] is True, key
        assert pair["decisions_differ"] is True, key
        assert pair["supplement_sufficient"] is True, key


def test_decisions_survive_block_permutation_and_new_window() -> None:
    inv = json.loads((REPORTS / "invariance.json").read_text(encoding="utf-8"))
    assert inv["all_invariance_checks_pass"] is True
    for key, case in inv["cases"].items():
        assert case["verdict_unchanged"] is True, key
        assert case["supplement_unchanged"] is True, key
        assert case["intersection_units_unchanged"] is True, key
    dec = json.loads((REPORTS / "decisions.json").read_text(encoding="utf-8"))
    for key, v in dec["supplement_under_permutation"].items():
        assert v["distinguishes"] is True, key
