"""Tests for migration preflight checks."""
import sqlite3

import pytest

from etlhelper.preflight import (
    COUNTEREXAMPLE,
    DIRECT_SAFE,
    INSUFFICIENT_EVIDENCE,
    Evidence,
    assess_migration,
    compare_candidates,
    reevaluate,
)

# Samples are exact decimal strings.  They are inserted into the SQLite source
# with a NUMERIC column on purpose: SQLite stores numeric-looking strings as
# REAL, which is exactly the loss of source precision the preflight must catch.
AMOUNT_A = "12345678901234567890.123456789"
AMOUNT_B = "12345678901234567890.123456790"

SOURCE_DDL = """
    CREATE TABLE ledger_src (
        amount NUMERIC,
        amount_text TEXT,
        business_no TEXT,
        note TEXT
    )
"""

TARGET_DDL = """
    CREATE TABLE ledger (
        amount NUMERIC,
        amount_text TEXT,
        business_no NUMERIC,
        business_no_text TEXT,
        note TEXT,
        required_extra TEXT NOT NULL DEFAULT 'legacy'
    )
"""


@pytest.fixture
def source_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(SOURCE_DDL)
    conn.executemany(
        "INSERT INTO ledger_src VALUES (?, ?, ?, ?)",
        [
            (AMOUNT_A, AMOUNT_A, "00017", "first"),
            (AMOUNT_B, AMOUNT_B, "17", "second"),
            (AMOUNT_B, AMOUNT_B, "00017", "third"),
        ],
    )
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def target_conn():
    conn = sqlite3.connect(":memory:")
    conn.execute(TARGET_DDL)
    conn.commit()
    yield conn
    conn.close()


@pytest.fixture
def samples():
    # Values are supplied exactly as they must reach the target, not as the
    # source driver happens to decode them.
    return [
        {"amount": AMOUNT_A, "amount_text": AMOUNT_A,
         "business_no": "00017", "note": "first"},
        {"amount": AMOUNT_B, "amount_text": AMOUNT_B,
         "business_no": "17", "note": "second"},
        {"amount": AMOUNT_B, "amount_text": AMOUNT_B,
         "business_no": "00017", "note": "third"},
    ]


CANDIDATE = {
    "amount": "amount",
    "business_no": "business_no",
    "note": "note",
}


def test_amount_merge_has_minimal_counterexample(source_conn, target_conn, samples):
    report = assess_migration(
        source_conn, "ledger_src", target_conn, "ledger", samples,
        mapping=CANDIDATE)

    assessment = report.for_column("amount")
    assert assessment.verdict == COUNTEREXAMPLE
    assert assessment.counterexample["kind"] == "merged_values"
    assert assessment.counterexample["sample_indexes"] == [0, 1]
    assert assessment.counterexample["source_values"] == [AMOUNT_A, AMOUNT_B]
    assert report.verdict_for("amount") == COUNTEREXAMPLE


def test_matching_declared_types_do_not_prove_numeric_fidelity(
        source_conn, target_conn, samples):
    report = assess_migration(
        source_conn, "ledger_src", target_conn, "ledger", samples,
        mapping=CANDIDATE)

    source_amount = next(c for c in report.source_columns if c.name == "amount")
    target_amount = next(c for c in report.target_columns if c.name == "amount")
    assert source_amount.type == target_amount.type
    assert report.verdict_for("amount") == COUNTEREXAMPLE


def test_business_number_merge_has_minimal_counterexample(
        source_conn, target_conn, samples):
    report = assess_migration(
        source_conn, "ledger_src", target_conn, "ledger", samples,
        mapping=CANDIDATE)

    assessment = report.for_column("business_no")
    assert assessment.role == "business_key"
    assert assessment.verdict == COUNTEREXAMPLE
    assert assessment.counterexample["kind"] == "merged_values"
    assert assessment.counterexample["sample_indexes"] == [0, 1]
    assert assessment.counterexample["target_readback"] == 17


def test_omitted_required_column_with_incomplete_default(
        source_conn, target_conn, samples):
    report = assess_migration(
        source_conn, "ledger_src", target_conn, "ledger", samples,
        mapping=CANDIDATE)

    assessment = report.for_column("required_extra")
    assert assessment.role == "required"
    assert assessment.verdict == INSUFFICIENT_EVIDENCE
    assert any("default" in item for item in assessment.missing_evidence)


def test_omitted_column_without_default_or_nullability_is_counterexample(
        source_conn):
    target = sqlite3.connect(":memory:")
    target.execute("CREATE TABLE ledger (required_extra TEXT NOT NULL)")
    target.commit()

    report = assess_migration(
        source_conn, "ledger_src", target, "ledger", [],
        mapping={"required_extra": None})

    assert report.verdict_for("required_extra") == COUNTEREXAMPLE
    target.close()


def test_omitted_nullable_column_is_direct_safe(source_conn):
    target = sqlite3.connect(":memory:")
    target.execute("CREATE TABLE ledger (optional_extra TEXT)")
    target.commit()

    report = assess_migration(
        source_conn, "ledger_src", target, "ledger", [],
        mapping={"optional_extra": None})

    assert report.verdict_for("optional_extra") == DIRECT_SAFE
    target.close()


def test_revoking_default_evidence_does_not_make_omission_safe(
        source_conn, target_conn, samples):
    evidence = Evidence(
        default_values={"required_extra": "legacy"},
        defaults_complete=True,
    )
    report = assess_migration(
        source_conn, "ledger_src", target_conn, "ledger", samples,
        mapping=CANDIDATE, evidence=evidence)
    assert report.verdict_for("required_extra") == DIRECT_SAFE

    revoked = reevaluate(report, revoke_default_evidence=True)
    assert revoked.verdict_for("required_extra") == INSUFFICIENT_EVIDENCE
    assert revoked.changes == [{
        "column": "required_extra",
        "role": "required",
        "before_role": "required",
        "before": DIRECT_SAFE,
        "after": INSUFFICIENT_EVIDENCE,
        "evidence": "target default evidence revoked",
    }]


def test_business_key_text_column_changes_verdict(
        source_conn, target_conn, samples):
    report = assess_migration(
        source_conn, "ledger_src", target_conn, "ledger", samples,
        mapping=CANDIDATE)

    rerouted = reevaluate(
        report, business_key_text_column="business_no_text")

    assert rerouted.verdict_for("business_no_text") == DIRECT_SAFE
    assert "business_no" not in {
        change["column"] for change in rerouted.changes
        if change["after"] == DIRECT_SAFE and change["role"] == "business_key"
    }
    assert any(
        "business_key_text_column=business_no_text" in change["evidence"]
        for change in rerouted.changes)
    assert any(
        change["column"] == "business_no_text"
        and change["before_role"] == "required"
        and change["role"] == "business_key"
        for change in rerouted.changes)


def test_numeric_text_column_changes_verdict(
        source_conn, target_conn, samples):
    report = assess_migration(
        source_conn, "ledger_src", target_conn, "ledger", samples,
        mapping=CANDIDATE)

    rerouted = reevaluate(report, numeric_text_column="amount_text")

    assert rerouted.verdict_for("amount_text") == DIRECT_SAFE
    assert any(
        "numeric_text_column=amount_text" in change["evidence"]
        for change in rerouted.changes)


def test_conversion_contract_changes_both_verdicts(
        source_conn, target_conn, samples):
    report = assess_migration(
        source_conn, "ledger_src", target_conn, "ledger", samples,
        mapping=CANDIDATE)

    contracted = reevaluate(
        report, mapping={
            "amount_text": "amount",
            "business_no_text": "business_no",
            "note": "note",
        },
        conversion_contract="store and read back exact strings",
    )

    assert contracted.verdict_for("amount_text") == DIRECT_SAFE
    assert contracted.verdict_for("business_no_text") == DIRECT_SAFE
    assert {change["evidence"] for change in contracted.changes} == {
        "conversion_contract"}


def test_scope_is_limited_to_supplied_samples(source_conn, target_conn, samples):
    report = assess_migration(
        source_conn, "ledger_src", target_conn, "ledger", samples,
        mapping=CANDIDATE)

    for assessment in report.assessments:
        assert assessment.scope
        assert "supplied" in assessment.scope.lower()


def test_source_is_not_modified(source_conn, target_conn, samples):
    before = source_conn.execute(
        "SELECT * FROM ledger_src ORDER BY rowid").fetchall()

    assess_migration(
        source_conn, "ledger_src", target_conn, "ledger", samples,
        mapping=CANDIDATE)

    after = source_conn.execute(
        "SELECT * FROM ledger_src ORDER BY rowid").fetchall()
    assert after == before


def test_compare_candidates_reports_all(source_conn, target_conn, samples):
    reports = compare_candidates(
        source_conn, "ledger_src", target_conn, "ledger", samples,
        candidates={
            "numeric": CANDIDATE,
            "text": {
                "amount_text": "amount",
                "business_no_text": "business_no",
                "note": "note",
            },
        })

    assert reports["numeric"].overall_verdict == COUNTEREXAMPLE
    assert reports["text"].overall_verdict == INSUFFICIENT_EVIDENCE


def test_null_sample_is_preserved(source_conn):
    target = sqlite3.connect(":memory:")
    target.execute("CREATE TABLE ledger (amount_text TEXT)")
    target.commit()

    report = assess_migration(
        source_conn, "ledger_src", target, "ledger",
        [{"amount_text": None}],
        mapping={"amount_text": "amount_text"})

    assert report.verdict_for("amount_text") == DIRECT_SAFE
    target.close()


def test_null_is_not_replaced_by_default(source_conn):
    target = sqlite3.connect(":memory:")
    target.execute(
        "CREATE TABLE ledger (amount_text TEXT DEFAULT 'unknown')")
    target.commit()

    report = assess_migration(
        source_conn, "ledger_src", target, "ledger",
        [{"amount_text": None}],
        mapping={"amount_text": "amount_text"})

    assert report.verdict_for("amount_text") == DIRECT_SAFE
    assert report.assessments[0].counterexample is None
    target.close()


def test_missing_sample_propagates_as_counterexample(
        source_conn):
    target = sqlite3.connect(":memory:")
    target.execute(
        "CREATE TABLE ledger (amount NUMERIC, business_no NUMERIC NOT NULL)")
    target.commit()

    report = assess_migration(
        source_conn, "ledger_src", target, "ledger",
        [{"amount": AMOUNT_A}],
        mapping={"amount": "amount", "business_no": "business_no"})

    assert report.verdict_for("business_no") == COUNTEREXAMPLE
    assert (report.for_column("business_no").counterexample["kind"]
            == "not_null_violation")
    target.close()


def test_blocked_write_by_other_required_column_is_not_this_columns_failure(
        source_conn):
    target = sqlite3.connect(":memory:")
    target.execute(
        "CREATE TABLE ledger (amount NUMERIC, other_required TEXT NOT NULL)")
    target.commit()

    report = assess_migration(
        source_conn, "ledger_src", target, "ledger",
        [{"amount": AMOUNT_A}],
        mapping={"amount": "amount", "other_required": None})

    amount = report.for_column("amount")
    assert amount.verdict == INSUFFICIENT_EVIDENCE
    assert amount.counterexample is None
    assert report.verdict_for("other_required") == COUNTEREXAMPLE
    target.close()
