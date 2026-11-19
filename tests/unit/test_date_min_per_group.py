import datetime as dt
from zoneinfo import ZoneInfoNotFoundError

import pytest

from datajudge import DateRetentionRule, WithinRequirement

REFERENCE_DATETIME = dt.datetime(2016, 1, 20, 12, 0, tzinfo=dt.timezone.utc)


def create_constraint(retention_rules, min_fraction=1.0, reference_datetime=None):
    req = WithinRequirement.from_raw_query("select * from example", "example_table")
    req.add_date_min_per_group_constraint(
        "col_date",
        "col_source",
        retention_rules=retention_rules,
        min_fraction=min_fraction,
        reference_datetime=reference_datetime or REFERENCE_DATETIME,
    )
    return req[0]


def test_pass_when_group_respects_retention():
    constraint = create_constraint({"a": DateRetentionRule(retention_days=30)})
    is_success, message = constraint._compare({"a": dt.date(2016, 1, 10)}, 1.0)
    assert is_success
    assert message is None


def test_unconfigured_groups_do_not_participate_in_pass_rate():
    constraint = create_constraint({"a": DateRetentionRule(retention_days=30)})
    is_success, _ = constraint._compare(
        {"a": dt.date(2016, 1, 10), "unconfigured": dt.date(2000, 1, 1)}, 1.0
    )
    assert is_success


def test_unconfigured_groups_listed_separately_on_failure():
    constraint = create_constraint({"a": DateRetentionRule(retention_days=7)})
    is_success, message = constraint._compare(
        {"a": dt.date(2016, 1, 5), "unconfigured": dt.date(2000, 1, 1)}, 1.0
    )
    assert not is_success
    assert "unconfigured" in message
    assert "not participating" in message


def test_violation_reports_factual_and_effective_min():
    constraint = create_constraint({"a": DateRetentionRule(retention_days=7)})
    is_success, message = constraint._compare({"a": dt.date(2016, 1, 5)}, 1.0)
    assert not is_success
    assert "2016-01-05" in message
    assert "2016-01-13" in message


def test_min_fraction_tolerates_violations():
    constraint = create_constraint(
        {
            "a": DateRetentionRule(retention_days=30),
            "b": DateRetentionRule(retention_days=7),
        },
        min_fraction=0.5,
    )
    factual = {"a": dt.date(2016, 1, 10), "b": dt.date(2016, 1, 5)}
    is_success, _ = constraint._compare(factual, 0.5)
    assert is_success
    is_success, _ = constraint._compare(factual, 0.51)
    assert not is_success


@pytest.mark.parametrize("factual", [{}, {"a": None}])
def test_configured_group_without_dates_is_reported(factual):
    constraint = create_constraint({"a": DateRetentionRule(retention_days=30)})
    is_success, message = constraint._compare(factual, 1.0)
    assert not is_success
    assert "no comparable data" in message


def test_missing_group_reported_alongside_passing_groups():
    constraint = create_constraint(
        {
            "a": DateRetentionRule(retention_days=30),
            "b": DateRetentionRule(retention_days=30),
        }
    )
    is_success, message = constraint._compare({"a": dt.date(2016, 1, 10)}, 1.0)
    assert not is_success
    assert "without comparable data" in message
    assert "'b'" in message


@pytest.mark.parametrize(
    "timezone, expected",
    [("Australia/Sydney", False), ("America/Los_Angeles", True)],
)
def test_today_depends_on_group_timezone(timezone, expected):
    # At 2016-01-20 01:00 UTC it is already 2016-01-20 in Sydney but still
    # 2016-01-19 in Los Angeles.
    constraint = create_constraint(
        {"a": DateRetentionRule(retention_days=7, timezone=timezone)},
        reference_datetime=dt.datetime(2016, 1, 20, 1, 0, tzinfo=dt.timezone.utc),
    )
    is_success, _ = constraint._compare({"a": dt.date(2016, 1, 12)}, 1.0)
    assert is_success == expected


def test_naive_reference_datetime_interpreted_as_utc():
    constraint = create_constraint(
        {"a": DateRetentionRule(retention_days=7)},
        reference_datetime=dt.datetime(2016, 1, 20, 12, 0),
    )
    is_success, _ = constraint._compare({"a": dt.date(2016, 1, 13)}, 1.0)
    assert is_success


def test_retention_rule_validation():
    with pytest.raises(ValueError):
        DateRetentionRule(retention_days=-1)
    with pytest.raises(ZoneInfoNotFoundError):
        DateRetentionRule(retention_days=1, timezone="not-a-timezone")


def test_empty_retention_rules_rejected():
    with pytest.raises(ValueError):
        create_constraint({})
