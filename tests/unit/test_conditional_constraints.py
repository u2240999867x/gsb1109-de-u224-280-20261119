import pytest
import sqlalchemy as sa

from datajudge import Condition, WithinRequirement
from datajudge.constraints.base import TestResult as ConstraintTestResult


@pytest.fixture()
def engine():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE shipments ("
                "id INTEGER, warehouse TEXT, status TEXT, tracking TEXT)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO shipments VALUES "
                "(1, 'WH1', 'shipped', 'T1'), "
                "(2, 'WH1', 'shipped', 'T2'), "
                "(3, 'WH1', 'pending', NULL), "
                "(4, 'WH2', 'shipped', NULL), "
                "(5, 'WH2', 'pending', NULL), "
                "(6, 'WH2', 'shipped', 'T6'), "
                "(7, 'WH2', 'shipped', 'T6'), "
                "(8, 'WH2', 'shipped', 'T8')"
            )
        )
    return engine


def _shipped_requirement():
    requirement = WithinRequirement.from_table("main", "main", "shipments")
    requirement.add_conditional_null_fraction_constraint(
        column="tracking",
        condition=Condition(raw_string="status = 'shipped'"),
        name="shipped_requires_tracking",
    )
    return requirement


def _unique_requirement():
    requirement = WithinRequirement.from_table("main", "main", "shipments")
    requirement.add_grouped_uniqueness_constraint(
        group_columns=["warehouse"],
        columns=["tracking"],
        name="tracking_unique_per_warehouse",
    )
    return requirement


def test_conditional_null_fraction_uses_shipped_rows_as_denominator(engine):
    requirement = _shipped_requirement()
    result = requirement[0].test(engine)

    assert not result.outcome
    # Six shipped rows, one of which lacks a tracking number. Unshipped rows
    # must neither count as violations nor inflate the denominator.
    assert (result.n_violations, result.n_denominator) == (1, 6)
    assert "status = 'shipped'" in result.denominator_description
    assert "(1 / 6)" in result.failure_message


def test_grouped_uniqueness_uses_non_null_rows_as_denominator(engine):
    requirement = _unique_requirement()
    result = requirement[0].test(engine)

    assert not result.outcome
    # Five rows carry a tracking number; the duplicate within WH2 yields one
    # redundant row. The same number in another warehouse would be fine.
    assert (result.n_violations, result.n_denominator) == (1, 5)
    assert "grouped by ['warehouse']" in result.denominator_description
    assert "T6" in result.failure_message


def test_grouped_uniqueness_allows_same_identifier_in_different_groups(engine):
    with engine.begin() as connection:
        connection.execute(sa.text("DELETE FROM shipments WHERE id = 4"))
        connection.execute(sa.text("UPDATE shipments SET tracking = 'T7' WHERE id = 7"))
        connection.execute(sa.text("UPDATE shipments SET tracking = 'T1' WHERE id = 8"))

    requirement = _unique_requirement()
    result = requirement[0].test(engine)

    assert result.outcome, result.failure_message
    assert result.n_violations == 0
    assert result.n_denominator == 5


def test_conditional_null_fraction_allows_nulls_outside_condition(engine):
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE shipments SET tracking = 'T4' WHERE id = 4"))

    requirement = _shipped_requirement()
    result = requirement[0].test(engine)

    assert result.outcome, result.failure_message
    assert (result.n_violations, result.n_denominator) == (0, 6)


def test_conditional_null_fraction_respects_tolerance(engine):
    requirement = WithinRequirement.from_table("main", "main", "shipments")
    requirement.add_conditional_null_fraction_constraint(
        column="tracking",
        condition=Condition(raw_string="status = 'shipped'"),
        max_null_fraction=0.2,
    )
    result = requirement[0].test(engine)

    assert result.outcome, result.failure_message


def test_constraints_report_separately_with_their_own_denominators(engine):
    requirement = _shipped_requirement()
    requirement.append(_unique_requirement()[0])

    results = requirement.test(engine)

    assert len(results) == 2
    assert [result.outcome for result in results] == [False, False]
    assert [
        (result.n_violations, result.n_denominator) for result in results
    ] == [(1, 6), (1, 5)]


def test_test_result_denominator_summary_is_optional():
    result = ConstraintTestResult.success()
    assert result.denominator_summary is None
    assert result.logging_message.count("Denominator") == 0


def test_conditional_null_fraction_empty_population_counts_as_success(engine):
    requirement = WithinRequirement.from_table("main", "main", "shipments")
    requirement.add_conditional_null_fraction_constraint(
        column="tracking",
        condition=Condition(raw_string="status = 'cancelled'"),
    )
    result = requirement[0].test(engine)

    assert result.outcome
    assert (result.n_violations, result.n_denominator) == (0, 0)
    assert "n/a" in result.denominator_summary


def test_grouped_uniqueness_empty_population_counts_as_success(engine):
    with engine.begin() as connection:
        connection.execute(sa.text("UPDATE shipments SET tracking = NULL"))

    requirement = _unique_requirement()
    result = requirement[0].test(engine)

    assert result.outcome
    assert (result.n_violations, result.n_denominator) == (0, 0)


def test_conditional_null_fraction_condition_may_use_unselected_columns(engine):
    requirement = WithinRequirement.from_table("main", "main", "shipments")
    requirement.add_conditional_null_fraction_constraint(
        column="tracking",
        condition=Condition(raw_string="id >= 6 AND status = 'shipped'"),
    )
    result = requirement[0].test(engine)

    # Only ids 6, 7, 8 match, all of which have tracking numbers.
    assert result.outcome, result.failure_message
    assert (result.n_violations, result.n_denominator) == (0, 3)


def test_constructors_reject_invalid_thresholds():
    requirement = WithinRequirement.from_table("main", "main", "shipments")
    with pytest.raises(ValueError):
        requirement.add_conditional_null_fraction_constraint(
            column="tracking",
            condition=Condition(raw_string="status = 'shipped'"),
            max_null_fraction=1.5,
        )
    with pytest.raises(ValueError):
        requirement.add_grouped_uniqueness_constraint(
            group_columns=["warehouse"],
            columns=["tracking"],
            max_duplicate_fraction=1,
        )
    with pytest.raises(ValueError):
        requirement.add_grouped_uniqueness_constraint(
            group_columns=[],
            columns=["tracking"],
        )
