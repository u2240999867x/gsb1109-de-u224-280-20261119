"""Tests for tools/migrate_variant_column.py."""

from decimal import Decimal

import pytest

from tools.migrate_variant_column import (
    ACTION_CLEAR,
    ACTION_CONFLICT,
    ACTION_KEEP,
    ACTION_MOVE,
    build_update_statement,
    classify_row,
    find_variant_column,
    parse_lossless_value,
    plan_migration,
    sql_literal,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("12.34", Decimal("12.34")),
        (" 12.5 ", Decimal("12.5")),
        ("-0.5", Decimal("-0.5")),
        ("1e3", Decimal("1E+3")),
        ("1.230", Decimal("1.230")),
        (".5", Decimal("0.5")),
    ],
)
def test_parse_decimal_lossless(raw, expected):
    assert parse_lossless_value(raw, "decimal", precision=10, scale=2) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "12kg",  # trailing unit
        "12 kg",
        "1,234",  # thousands separator
        "",  # empty
        "   ",
        "N/A",
        "NaN",
        "inf",
        "0x1F",  # hex
        "1.234",  # exceeds scale 2, would need rounding
        "123456789.12",  # exceeds precision 10 with scale 2
        "abc",
    ],
)
def test_parse_decimal_rejected(raw):
    assert parse_lossless_value(raw, "decimal", precision=10, scale=2) is None


def test_parse_decimal_without_precision_scale():
    assert parse_lossless_value("1.234567", "decimal") == Decimal("1.234567")


def test_parse_decimal_trailing_zeros_beyond_scale():
    # 1.230 == 1.23 exactly, so it fits scale 2 losslessly
    assert parse_lossless_value("1.230", "decimal", precision=10, scale=2) == Decimal("1.230")
    assert parse_lossless_value("1.234", "decimal", precision=10, scale=2) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.1", Decimal("0.1")),
        ("2.5", Decimal("2.5")),
        ("-13", Decimal("-13")),
    ],
)
def test_parse_double_lossless(raw, expected):
    assert parse_lossless_value(raw, "double") == expected


@pytest.mark.parametrize(
    "raw",
    [
        "0.123456789012345678",  # precision not representable as double
        "1e999",  # overflows to inf
        "12kg",
    ],
)
def test_parse_double_rejected(raw):
    assert parse_lossless_value(raw, "double") is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("42", Decimal("42")),
        ("42.0", Decimal("42.0")),
        ("-7", Decimal("-7")),
        ("9223372036854775807", Decimal("9223372036854775807")),
    ],
)
def test_parse_bigint_lossless(raw, expected):
    assert parse_lossless_value(raw, "bigint") == expected


@pytest.mark.parametrize(
    "raw",
    [
        "42.5",  # fractional
        "9223372036854775808",  # overflows int64
        "-9223372036854775809",
    ],
)
def test_parse_bigint_rejected(raw):
    assert parse_lossless_value(raw, "bigint") is None


def test_parse_non_string_rejected():
    assert parse_lossless_value(12.5, "decimal") is None
    assert parse_lossless_value(None, "decimal") is None


def test_parse_unsupported_type_rejected():
    assert parse_lossless_value("1.0", "text") is None
    assert parse_lossless_value("1.0", "json") is None


def test_classify_move_when_main_null():
    row = classify_row("id1", None, "12.34", "decimal", precision=10, scale=2)
    assert row.action == ACTION_MOVE
    assert row.parsed == Decimal("12.34")


def test_classify_clear_when_in_sync():
    row = classify_row("id1", 12.34, "12.34", "double")
    assert row.action == ACTION_CLEAR
    row = classify_row("id1", Decimal("1.2"), "1.20", "decimal", precision=10, scale=2)
    assert row.action == ACTION_CLEAR


def test_classify_conflict_when_main_differs():
    row = classify_row("id1", 99.9, "12.34", "decimal", precision=10, scale=2)
    assert row.action == ACTION_CONFLICT
    assert row.parsed == Decimal("12.34")


def test_classify_keep_when_not_lossless():
    row = classify_row("id1", None, "12kg", "decimal", precision=10, scale=2)
    assert row.action == ACTION_KEEP
    assert row.parsed is None
    # conflict check does not apply: unparseable values stay even if main is set
    row = classify_row("id1", 1.0, "12kg", "decimal", precision=10, scale=2)
    assert row.action == ACTION_KEEP


def test_plan_migration_groups_rows():
    rows = [
        ("k1", None, "1.5"),  # move
        ("k2", 2.5, "2.5"),  # clear
        ("k3", 3.5, "4.5"),  # conflict
        ("k4", None, "1kg"),  # keep
    ]
    plan = plan_migration(rows, "double")
    assert [r.key for r in plan.movable] == ["k1"]
    assert [r.key for r in plan.by_action(ACTION_CLEAR)] == ["k2"]
    assert [r.key for r in plan.conflicts] == ["k3"]
    assert [r.key for r in plan.by_action(ACTION_KEEP)] == ["k4"]


def test_build_update_move():
    row = classify_row("abc", None, "12.34", "decimal", precision=10, scale=2)
    stmt = build_update_statement("ds.orders", "_dlt_id", "weight", "weight_v_text", row)
    assert stmt == (
        "UPDATE ds.orders SET weight = 12.34, weight_v_text = NULL"
        " WHERE _dlt_id = 'abc' AND weight_v_text IS NOT NULL"
    )


def test_build_update_clear():
    row = classify_row("abc", 12.34, "12.34", "double")
    stmt = build_update_statement("ds.orders", "_dlt_id", "weight", "weight_v_text", row)
    assert stmt == (
        "UPDATE ds.orders SET weight_v_text = NULL"
        " WHERE _dlt_id = 'abc' AND weight_v_text IS NOT NULL"
    )


def test_build_update_escapes_string_key():
    row = classify_row("o'clock", None, "1.5", "double")
    stmt = build_update_statement("ds.t", "id", "weight", "weight_v_text", row)
    assert "'o''clock'" in stmt


def test_build_update_rejects_non_migratable():
    row = classify_row("abc", 1.0, "2.0", "double")
    assert row.action == ACTION_CONFLICT
    with pytest.raises(ValueError):
        build_update_statement("ds.t", "id", "weight", "weight_v_text", row)
    row = classify_row("abc", None, "1kg", "double")
    with pytest.raises(ValueError):
        build_update_statement("ds.t", "id", "weight", "weight_v_text", row)


def test_find_variant_column_auto_detect():
    columns = {
        "weight": {"data_type": "double"},
        "weight_v_text": {"data_type": "text", "variant": True},
        "other": {"data_type": "text"},
    }
    assert find_variant_column(columns, "weight") == "weight_v_text"


def test_find_variant_column_explicit():
    columns = {
        "weight": {"data_type": "double"},
        "weight_v_text": {"data_type": "text", "variant": True},
        "weight_v_double": {"data_type": "double", "variant": True},
    }
    assert find_variant_column(columns, "weight", "weight_v_text") == "weight_v_text"
    with pytest.raises(ValueError, match="several variant columns"):
        find_variant_column(columns, "weight")


def test_find_variant_column_missing():
    with pytest.raises(ValueError, match="no variant column"):
        find_variant_column({"weight": {"data_type": "double"}}, "weight")
    with pytest.raises(ValueError, match="not found"):
        find_variant_column(
            {"weight": {"data_type": "double"}}, "weight", "weight_v_text"
        )


def test_sql_literal():
    assert sql_literal(None) == "NULL"
    assert sql_literal(Decimal("1.20")) == "1.20"
    assert sql_literal(True) == "TRUE"
    assert sql_literal(3) == "3"
    assert sql_literal("a'b") == "'a''b'"
