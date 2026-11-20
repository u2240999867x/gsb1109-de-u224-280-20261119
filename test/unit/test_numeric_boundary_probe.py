"""Tests for the standalone numeric boundary diagnostic."""
import sys
from decimal import Decimal
from pathlib import Path

import pytest

DIAGNOSTICS = Path(__file__).resolve().parents[2] / "diagnostics"
sys.path.insert(0, str(DIAGNOSTICS))

import numeric_boundary_probe as probe  # noqa: E402


@pytest.fixture(scope="module")
def results():
    import csv

    ledger_path = DIAGNOSTICS / "ledger.csv"
    with ledger_path.open(newline="", encoding="utf-8") as handle:
        ledger_rows = list(csv.DictReader(handle))
    ledger = {row["id"]: row["reading_text"] for row in ledger_rows}
    specs = [
        {
            "name": "A-SRC-ROUND", "role": "copy 1",
            "description": "source pre-round", "path": "A",
            "row_ids": ["R-ROUND"], "source_round": True,
            "callback": "identity", "target_round": True,
        },
        {
            "name": "B-TGT-ROUND", "role": "copy 2",
            "description": "target-only round", "path": "B",
            "row_ids": ["R-ROUND"], "source_round": False,
            "callback": "identity", "target_round": True,
        },
        {
            "name": "P-DECIMAL", "role": "pair decimal",
            "description": "decimal pair", "path": "C",
            "row_ids": ["R-FLOAT-1", "R-FLOAT-2"], "source_round": False,
            "callback": "identity", "target_round": True,
        },
        {
            "name": "P-FLOAT", "role": "pair float",
            "description": "float pair", "path": "D",
            "row_ids": ["R-FLOAT-1", "R-FLOAT-2"], "source_round": False,
            "callback": "float", "target_round": True,
        },
    ]
    return {spec["name"]: probe.run_scenario(spec, ledger) for spec in specs}


def test_same_target_but_different_source_boundaries(results):
    a = results["A-SRC-ROUND"]
    b = results["B-TGT-ROUND"]
    assert a.readback_records[0]["reading"] == Decimal("2.35")
    assert b.readback_records[0]["reading"] == Decimal("2.35")

    assert a.obs("R-ROUND", "source_cursor").value_repr == "Decimal('2.35')"
    assert b.obs("R-ROUND", "source_cursor").value_repr == "Decimal('2.346')"
    assert a.obs("R-ROUND", "bind").value_repr == "Decimal('2.35')"
    assert b.obs("R-ROUND", "bind").value_repr == "Decimal('2.346')"


def test_pre_round_loses_at_source_cursor_and_target_round_at_target_store(results):
    last, first, _, _ = probe.individual_boundary(results["A-SRC-ROUND"], "R-ROUND")
    assert (last, first) == ("dbapi_source", "source_cursor")

    last, first, _, _ = probe.individual_boundary(results["B-TGT-ROUND"], "R-ROUND")
    assert (last, first) == ("bind", "target_store")


def test_float_pair_collapses_at_callback(results):
    result = results["P-FLOAT"]
    first_same, last_distinct = probe.pair_boundary(result, "R-FLOAT-1", "R-FLOAT-2")
    assert first_same == "callback"
    assert last_distinct == "row_factory"

    assert result.obs("R-FLOAT-1", "row_factory").type_name == "Decimal"
    assert result.obs("R-FLOAT-1", "callback").type_name == "float"
    assert result.obs("R-FLOAT-1", "callback").value_repr == "9007199254740994.0"
    assert result.obs("R-FLOAT-2", "callback").value_repr == "9007199254740994.0"


def test_decimal_pair_never_collapses(results):
    result = results["P-DECIMAL"]
    first_same, last_distinct = probe.pair_boundary(result, "R-FLOAT-1", "R-FLOAT-2")
    assert first_same is None
    assert last_distinct == "target_readback"
    assert result.obs("R-FLOAT-1", "target_readback").value_repr == \
        "Decimal('9007199254740993.01')"
    assert result.obs("R-FLOAT-2", "target_readback").value_repr == \
        "Decimal('9007199254740993.02')"
