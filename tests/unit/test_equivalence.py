"""Tests for optional full-width / ASCII digit equivalence in varchar regex checks."""

from __future__ import annotations

import re

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from datajudge import Condition, WithinRequirement
from datajudge._engines import is_db2, is_mssql
from datajudge.equivalence import (
    DIGIT_WIDTH,
    EXACT,
    FULLWIDTH_DIGITS,
    HALFWIDTH_DIGITS,
    normalize_digit_width,
    normalized_digit_width_column,
    supports_digit_width_normalization,
    validate_equivalence_mode,
)

# Mirror of the incoming address-code batch described in the task:
# duplicated valid 007 in three digit widths, numeric values with an
# incorrect length in three widths, values containing other invalid symbols
# and one null value.
WIDTH_TABLE_ROWS = [
    "007",
    "007",
    "００７",
    "0０7",
    "0007",
    "０００７",
    "0０07",
    "00A",
    "００Ａ",
    "０X7",
    None,
]
REGEX = "^[0-9]{3}$"


@pytest.fixture(scope="module")
def sqlite_engine():
    engine = sa.create_engine("sqlite:///:memory:")

    @event.listens_for(engine, "connect")
    def _register_regex(dbapi_connection, _):
        # NULL in, NULL out mirrors SQL three-valued logic: NOT (NULL REGEXP ...)
        # is NULL and therefore does not qualify as a violation.
        dbapi_connection.create_function(
            "REGEXP",
            2,
            lambda pattern, value: None
            if value is None
            else (1 if re.search(pattern, value) else 0),
        )

    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE width_codes (code TEXT)"))
        for row in WIDTH_TABLE_ROWS:
            connection.execute(
                sa.text("INSERT INTO width_codes(code) VALUES (:value)"),
                {"value": row},
            )
    return engine


def _build_constraint(engine, *, in_db: bool, aggregated: bool, mode: str):
    requirement = WithinRequirement.from_table("main", None, "width_codes")
    not_null = Condition(raw_string="code IS NOT NULL")
    if in_db:
        requirement.add_varchar_regex_constraint_db(
            column="code",
            regex=REGEX,
            condition=not_null,
            relative_tolerance=0,
            aggregated=aggregated,
            n_counterexamples=-1,
            equivalence_mode=mode,
        )
    else:
        requirement.add_varchar_regex_constraint(
            column="code",
            regex=REGEX,
            condition=not_null,
            allow_none=True,
            relative_tolerance=0,
            aggregated=aggregated,
            n_counterexamples=-1,
            equivalence_mode=mode,
        )
    return requirement[0]


def _absolute_terms(result):
    match = re.search(
        r"In absolute terms, (\d+) of the (\d+) samples",
        result.failure_message or "",
    )
    assert match is not None, result.failure_message
    return int(match.group(1)), int(match.group(2))


def test_normalize_digit_width_maps_only_full_width_digits():
    assert normalize_digit_width("００７") == "007"
    assert normalize_digit_width("0０7") == "007"
    assert normalize_digit_width("007") == "007"
    # Symbols other than digits keep their original form, including full-width letters.
    assert normalize_digit_width("００Ａ") == "00Ａ"
    assert normalize_digit_width("０X7") == "0X7"
    assert normalize_digit_width(None) is None
    assert len(FULLWIDTH_DIGITS) == len(HALFWIDTH_DIGITS) == 10


def test_validate_equivalence_mode():
    validate_equivalence_mode(EXACT)
    validate_equivalence_mode(DIGIT_WIDTH)
    with pytest.raises(ValueError, match="Unknown equivalence mode"):
        validate_equivalence_mode("collation")


def test_unsupported_backend_reports_instead_of_silent_collation_match():
    class _StubEngine:
        name = "mssql"

    engine = _StubEngine()
    assert is_mssql(engine)
    assert supports_digit_width_normalization(engine) is False
    with pytest.raises(NotImplementedError, match="Digit-width equivalence"):
        normalized_digit_width_column(engine, sa.column("code"))

    class _Db2StubEngine:
        name = "ibm_db_sa"

    db2_engine = _Db2StubEngine()
    assert is_db2(db2_engine)
    assert supports_digit_width_normalization(db2_engine) is False


def test_digit_width_non_aggregated_counts_raw_rows_on_both_rules(sqlite_engine):
    # Every non-null row remains its own denominator entry; the six rows whose
    # normalized code breaks the regex count as violations.
    for in_db in (True, False):
        constraint = _build_constraint(
            sqlite_engine, in_db=in_db, aggregated=False, mode=DIGIT_WIDTH
        )
        result = constraint.test(sqlite_engine)
        assert result.outcome is False
        assert _absolute_terms(result) == (6, 10)


def test_digit_width_aggregated_counts_distinct_normalized_codes(sqlite_engine):
    # Distinct normalized codes: 007 (three spellings merged), 0007, 00A,
    # 00Ａ, 0X7 -> 5 codes of which 4 violate the regex.
    for in_db in (True, False):
        constraint = _build_constraint(
            sqlite_engine, in_db=in_db, aggregated=True, mode=DIGIT_WIDTH
        )
        result = constraint.test(sqlite_engine)
        assert result.outcome is False
        assert _absolute_terms(result) == (4, 5)


def test_three_valid_zero_zero_seven_spellings_count_once(sqlite_engine):
    for in_db in (True, False):
        constraint = _build_constraint(
            sqlite_engine, in_db=in_db, aggregated=True, mode=DIGIT_WIDTH
        )
        result = constraint.test(sqlite_engine)
        assert result.failure_message is not None
        # Only one of the five distinct normalized codes passes the pattern.
        violations, total = _absolute_terms(result)
        assert total - violations == 1


def test_counterexamples_keep_original_unnormalized_values(sqlite_engine):
    expected_raw_values = {
        "0007",
        "０００７",
        "0０07",
        "00A",
        "００Ａ",
        "０X7",
    }
    for in_db in (True, False):
        constraint = _build_constraint(
            sqlite_engine, in_db=in_db, aggregated=False, mode=DIGIT_WIDTH
        )
        message = constraint.test(sqlite_engine).failure_message
        assert message is not None
        for raw_value in expected_raw_values:
            assert raw_value in message
        # The normalized code must not replace the raw spelling in the report.
        assert "0007" in message


def test_exact_mode_keeps_legacy_behavior_on_both_rules(sqlite_engine):
    # Without the option, full-width spellings are distinct values and break
    # the ASCII-only regex: 9 distinct non-null values, 8 violations.
    for in_db in (True, False):
        constraint = _build_constraint(
            sqlite_engine, in_db=in_db, aggregated=True, mode=EXACT
        )
        result = constraint.test(sqlite_engine)
        assert result.outcome is False
        assert _absolute_terms(result) == (8, 9)


def test_equivalence_mode_defaults_to_exact(sqlite_engine):
    requirement = WithinRequirement.from_table("main", None, "width_codes")
    requirement.add_varchar_regex_constraint(
        "code",
        REGEX,
        condition=Condition(raw_string="code IS NOT NULL"),
        allow_none=True,
    )
    requirement.add_varchar_regex_constraint_db(
        "code",
        REGEX,
        condition=Condition(raw_string="code IS NOT NULL"),
    )
    assert requirement[0]._equivalence_mode == EXACT
    assert requirement[1]._equivalence_mode == EXACT


def test_invalid_equivalence_mode_rejected_at_construction(sqlite_engine):
    requirement = WithinRequirement.from_table("main", None, "width_codes")
    with pytest.raises(ValueError, match="Unknown equivalence mode"):
        requirement.add_varchar_regex_constraint(
            "code", REGEX, equivalence_mode="not_a_mode"
        )
    with pytest.raises(ValueError, match="Unknown equivalence mode"):
        requirement.add_varchar_regex_constraint_db(
            "code", REGEX, equivalence_mode="not_a_mode"
        )


def test_digit_width_tolerance_threshold_agrees_between_rules(sqlite_engine):
    # 6 of 10 raw rows violate; a tolerance of 0.6 therefore fails while 0.6
    # exactly fails on the strict greater-than comparison and 0.7 passes.
    for in_db in (True, False):
        failing = _build_constraint(
            sqlite_engine, in_db=in_db, aggregated=False, mode=DIGIT_WIDTH
        )
        assert failing.test(sqlite_engine).outcome is False

        requirement = WithinRequirement.from_table("main", None, "width_codes")
        not_null = Condition(raw_string="code IS NOT NULL")
        if in_db:
            requirement.add_varchar_regex_constraint_db(
                "code",
                REGEX,
                condition=not_null,
                relative_tolerance=0.7,
                aggregated=False,
                equivalence_mode=DIGIT_WIDTH,
            )
        else:
            requirement.add_varchar_regex_constraint(
                "code",
                REGEX,
                condition=not_null,
                allow_none=True,
                relative_tolerance=0.7,
                aggregated=False,
                equivalence_mode=DIGIT_WIDTH,
            )
        assert requirement[0].test(sqlite_engine).outcome is True


def test_null_policy_unchanged_in_python_rule(sqlite_engine):
    requirement = WithinRequirement.from_table("main", None, "width_codes")
    requirement.add_varchar_regex_constraint(
        "code", REGEX, allow_none=False, equivalence_mode=DIGIT_WIDTH
    )
    result = requirement[0].test(sqlite_engine)
    assert result.outcome is False
    assert "None value" in (result.failure_message or "")

    requirement = WithinRequirement.from_table("main", None, "width_codes")
    requirement.add_varchar_regex_constraint(
        "code", REGEX, allow_none=True, equivalence_mode=DIGIT_WIDTH
    )
    # Nulls are ignored; the remaining values merge into five distinct
    # normalized codes (the null policy itself is unaffected by the option).
    result = requirement[0].test(sqlite_engine)
    assert result.outcome is False
    violations, total = _absolute_terms(result)
    assert total == 5
    assert violations == 4
