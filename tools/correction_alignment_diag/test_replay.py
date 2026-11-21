"""Regression tests for the correction-alignment diagnosis."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any

MODULE_PATH = Path(__file__).with_name("replay.py")
SPEC = importlib.util.spec_from_file_location("correction_alignment_replay", MODULE_PATH)
assert SPEC is not None
assert SPEC.loader is not None
replay = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = replay
SPEC.loader.exec_module(replay)


def test_final_payloads_are_masked_and_equal() -> None:
    report = replay.run()
    replay.verify(report)
    assert report["final_payloads_equal"] is True
    assert report["final_payload"] == [
        {
            "row_label": 4102,
            "business_key": "S-A",
            "business_time": 10,
            "raw": 100.0,
            "correction": None,
            "adjusted": None,
        },
        {
            "row_label": 4101,
            "business_key": "S-C",
            "business_time": 20,
            "raw": 200.0,
            "correction": 3.0,
            "adjusted": 203.0,
        },
    ]


def test_native_and_narwhals_have_different_first_faults() -> None:
    report = replay.run()
    assert report["first_divergence_native_vs_narwhals"]["stage"] == 0
    assert report["first_truth_deviation"]["native"] == {
        "call_index": 5,
        "stage": 4,
        "call": "patch S-A with one-row business-key Series",
        "column": "correction",
        "business_key": "S-A",
        "expected": 7.0,
        "actual": None,
        "row_label": 4102,
    }
    assert report["first_truth_deviation"]["narwhals"] == {
        "call_index": 0,
        "stage": 0,
        "call": "attach correction",
        "column": "correction",
        "business_key": "S-B",
        "expected": None,
        "actual": 7.0,
        "row_label": 4103,
    }


def test_narwhals_matches_polars_no_index_control() -> None:
    report = replay.run()
    assert report["first_divergence_narwhals_vs_polars"] is None
    assert report["no_index_backend_final"] == report["final_payload"]


def test_repairs_restore_the_audited_amount() -> None:
    report = replay.run()
    by_key = {row["business_key"]: row for row in report["repairs"]["business_key_join"]}
    assert by_key["S-A"]["adjusted"] == 107.0
    assert by_key["S-B"]["adjusted"] is None
    assert by_key["S-C"]["adjusted"] == 203.0
    scalar = {
        row["business_key"]: row for row in report["repairs"]["scalar_assignment"]
    }
    assert scalar["S-A"]["correction"] == 7.0
    assert scalar["S-A"]["adjusted"] == 107.0


def test_duplicate_business_key_names_the_contesting_rows() -> None:
    report = replay.run()
    duplicate: dict[str, Any] = report["duplicate_labels"]
    assert duplicate["conflicting_key"] == "S-B"
    assert [row["correction_id"] for row in duplicate["conflicting_rows"]] == [
        "C-02",
        "C-07",
    ]
    assert duplicate["native_assignment"]["error"] == "ValueError"
    assert duplicate["merge_many_to_one"]["error"] == "MergeError"
    assert duplicate["unique_labels_duplicate_key"]["native_assignment"]["ok"] is True
    assert duplicate["unique_labels_duplicate_key"]["narwhals_assignment"]["ok"] is True
    assert duplicate["unique_labels_duplicate_key"]["merge_many_to_one"]["ok"] is False


def test_one_row_series_is_not_a_scalar() -> None:
    report = replay.run()
    controls = report["scalar_controls"]
    assert controls["with_columns_one_row_series"]["ok"] is False
    assert controls["with_columns_one_row_series"]["error"] == "ShapeError"
    assert controls["when_then_one_row_series"]["ok"] is False
    # The same one-row Series works when its index is the target row label.
    assert controls["native_row_label_series"][0]["adjusted"] == 107.0
    # A real scalar/literal broadcasts and fixes the value on the intended row.
    literal = {
        row["business_key"]: row for row in controls["narwhals_literal_conditional"]
    }
    assert literal["S-A"]["adjusted"] == 107.0
