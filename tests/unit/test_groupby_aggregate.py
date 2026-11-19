import pytest
import sqlalchemy as sa

from datajudge import BetweenRequirement, WithinRequirement
from datajudge.constraints.groupby import AggregateNumericRange
from datajudge.data_source import TableDataSource
from datajudge.db_access import DataReference


@pytest.fixture()
def engine():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text("CREATE TABLE daily (store TEXT, day TEXT, amount NUMERIC)")
        )
        connection.execute(
            sa.text(
                "INSERT INTO daily VALUES "
                "('A', '2026-01-01', 100), "
                "('A', '2026-01-01', 50), "
                "('B', '2026-01-01', 80), "
                "('A', '2026-01-02', 0), "
                "('A', '2026-01-02', 0), "
                "('B', '2026-01-02', NULL), "
                "('B', '2026-01-02', NULL)"
            )
        )
        connection.execute(
            sa.text("CREATE TABLE reference (store TEXT, day TEXT, amount NUMERIC)")
        )
        connection.execute(
            sa.text(
                "INSERT INTO reference VALUES "
                "('A', '2026-01-01', 150), "
                "('B', '2026-01-01', 90), "
                "('A', '2026-01-02', 0), "
                "('B', '2026-01-02', 0)"
            )
        )
        connection.execute(
            sa.text("CREATE TABLE offsetting (store TEXT, day TEXT, amount NUMERIC)")
        )
        connection.execute(
            sa.text(
                "INSERT INTO offsetting VALUES "
                "('A', '2026-01-01', 150), "
                "('B', '2026-01-01', -50)"
            )
        )
    return engine


def _within(**kwargs):
    requirement = WithinRequirement.from_table("db", None, "daily")
    requirement.add_groupby_sum_range_constraint(["store", "day"], "amount", **kwargs)
    return requirement[0]


def test_bounds_are_applied_per_group(engine):
    result = _within(lower_bound=100, upper_bound=160).test(engine)
    message = result.failure_message

    assert not result.outcome
    assert (
        "store='B', day='2026-01-01': sum 80 outside allowed range [100, 160]"
        in message
    )
    assert "1 sample row(s) of 1 row(s)" in message
    assert "store='A', day='2026-01-01'" not in message


def test_group_deviations_do_not_cancel_out(engine):
    requirement = WithinRequirement.from_table("db", None, "offsetting")
    requirement.add_groupby_sum_range_constraint(
        ["store", "day"], "amount", lower_bound=0, upper_bound=200
    )
    result = requirement[0].test(engine)

    assert not result.outcome
    assert "sum -50 outside allowed range [0, 200]" in result.failure_message


def test_between_group_tolerance_uses_per_group_reference(engine):
    requirement = BetweenRequirement.from_tables(
        "db", None, "daily", "db", None, "reference"
    )
    requirement.add_groupby_sum_range_constraint(
        ["store", "day"],
        ["store", "day"],
        "amount",
        tolerance=0.05,
    )
    result = requirement[0].test(engine)

    assert not result.outcome
    assert "store='B', day='2026-01-01': sum 80 outside allowed range [85.5, 94.5]" in (
        result.failure_message
    )


def test_between_tolerance_passes_within_band(engine):
    requirement = BetweenRequirement.from_tables(
        "db", None, "daily", "db", None, "reference"
    )
    requirement.add_groupby_sum_range_constraint(
        ["store", "day"],
        ["store", "day"],
        "amount",
        tolerance=0.2,
        allow_empty_groups=True,
    )
    assert requirement[0].test(engine).outcome


def test_real_zero_passes_but_empty_amounts_are_reported(engine):
    result = _within(lower_bound=0, upper_bound=0).test(engine)
    message = result.failure_message

    assert not result.outcome
    assert "store='A', day='2026-01-02'" not in message
    assert (
        "store='B', day='2026-01-02': no amount samples; allowed range [0, 0] "
        "(0 sample row(s) of 2 row(s))"
    ) in message
    assert "1 group(s) without amount samples" in message


def test_empty_amounts_can_be_ignored_when_allowed(engine):
    result = _within(lower_bound=0, upper_bound=1000, allow_empty_groups=True).test(
        engine
    )
    assert result.outcome


def test_missing_group_in_reference_is_reported(engine):
    with engine.begin() as connection:
        connection.execute(sa.text("INSERT INTO daily VALUES ('C', '2026-01-01', 10)"))
    requirement = BetweenRequirement.from_tables(
        "db", None, "daily", "db", None, "reference"
    )
    requirement.add_groupby_sum_range_constraint(
        ["store", "day"], ["store", "day"], "amount", tolerance=0
    )
    result = requirement[0].test(engine)

    assert not result.outcome
    assert "store='C', day='2026-01-01': no reference group" in result.failure_message


def test_relative_deviation_alias():
    ref = DataReference(TableDataSource("db", "daily"), ["store", "day"])
    constraint = AggregateNumericRange(
        ref, "amount", max_relative_deviation=0.5, target_aggregate=200
    )
    assert constraint._tolerance == 0.5


def test_requires_a_range_or_target():
    ref = DataReference(TableDataSource("db", "daily"), ["store", "day"])
    with pytest.raises(ValueError):
        AggregateNumericRange(ref, "amount")


def test_report_lists_sample_rows_and_allowed_range(engine):
    constraint = _within(lower_bound=100, upper_bound=160)
    report = {(row["key"]): row for row in constraint.get_report(engine)}

    zero_day = report[("A", "2026-01-02")]
    assert zero_day["aggregate"] == 0
    assert zero_day["n_samples"] == 2
    assert zero_day["n_rows"] == 2
    assert zero_day["allowed_lower_bound"] == 100
    assert zero_day["allowed_upper_bound"] == 160
    assert not zero_day["outcome"]

    empty_day = report[("B", "2026-01-02")]
    assert empty_day["aggregate"] is None
    assert empty_day["n_samples"] == 0
    assert empty_day["n_rows"] == 2
    assert not empty_day["outcome"]
    assert "no amount samples" in empty_day["reason"]


def test_report_for_between_uses_reference_band(engine):
    requirement = BetweenRequirement.from_tables(
        "db", None, "daily", "db", None, "reference"
    )
    requirement.add_groupby_sum_range_constraint(
        ["store", "day"], ["store", "day"], "amount", tolerance=0.05
    )
    report = {row["key"]: row for row in requirement[0].get_report(engine)}

    row = report[("B", "2026-01-01")]
    assert row["aggregate"] == 80
    assert row["n_samples"] == 1
    assert row["allowed_lower_bound"] == 85.5
    assert row["allowed_upper_bound"] == 94.5
    assert not row["outcome"]


def test_explicit_allowed_range_per_group(engine):
    result = _within(
        allowed_ranges={
            ("A", "2026-01-01"): (120, 180),
            ("B", "2026-01-01"): (70, 90),
            ("A", "2026-01-02"): (0, 0),
        },
        allow_empty_groups=True,
    ).test(engine)
    assert result.outcome


def test_explicit_allowed_range_reports_violation(engine):
    result = _within(
        allowed_ranges={
            ("A", "2026-01-01"): (120, 180),
            ("B", "2026-01-01"): (100, 120),
        },
        allow_empty_groups=True,
    ).test(engine)

    assert not result.outcome
    message = result.failure_message
    assert (
        "store='B', day='2026-01-01': sum 80 outside allowed range [100, 120] "
        "(1 sample row(s) of 1 row(s))"
    ) in message


def test_report_exposes_allowed_range(engine):
    constraint = _within(lower_bound=100, upper_bound=160)
    report = {row["key"]: row for row in constraint.get_report(engine)}
    assert report[("A", "2026-01-01")]["allowed_range"] == (100, 160)


def test_report_status_distinguishes_zero_from_no_samples(engine):
    report = {
        row["key"]: row
        for row in _within(lower_bound=0, upper_bound=0).get_report(engine)
    }

    assert report[("A", "2026-01-02")]["status"] == "ok"
    assert report[("A", "2026-01-02")]["aggregate"] == 0
    assert report[("B", "2026-01-02")]["status"] == "no_samples"
    assert report[("B", "2026-01-02")]["aggregate"] is None


def test_common_keyword_aliases(engine):
    result = _within(
        aggregate_column="amount",
        expected_sum=100,
        relative_tolerance=1.0,
        allow_empty_groups=True,
    ).test(engine)
    assert result.outcome


def test_format_report_lists_samples_and_ranges(engine):
    text = _within(lower_bound=0, upper_bound=0).format_report(engine)

    assert (
        "store='A', day='2026-01-02': sum=0, samples=2/2, allowed=[0, 0], "
        "status=ok"
    ) in text
    assert (
        "store='B', day='2026-01-02': sum=NULL, samples=0/2, allowed=[0, 0], "
        "status=no_samples"
    ) in text
