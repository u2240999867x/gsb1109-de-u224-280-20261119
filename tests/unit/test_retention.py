import datetime as dt
import os
import time

import pytest
import sqlalchemy as sa

import datajudge
from datajudge import requirements
from datajudge.data_source import TableDataSource
from datajudge.db_access import DataReference
from datajudge.retention import (
    RetentionPolicy,
    RetentionPolicyVersion,
    RetentionStatus,
    RetentionSubject,
    audit_retention,
)

UTC = dt.timezone.utc

# On 2026-11-01, America/New_York falls back from EDT to EST: the local wall
# clock time 01:30 occurs twice, at 05:30 UTC (EDT) and at 06:30 UTC (EST).
FIRST_AMBIGUOUS_0130_UTC = dt.datetime(2026, 11, 1, 5, 30, tzinfo=UTC)
SECOND_AMBIGUOUS_0130_UTC = dt.datetime(2026, 11, 1, 6, 30, tzinfo=UTC)
# The policy revision takes effect between the two repeated local 01:30s.
POLICY_CHANGE_UTC = dt.datetime(2026, 11, 1, 6, 0, tzinfo=UTC)

# Two audit instants within the repeated hour, ordered by their UTC instant.
# Both correspond to a local wall clock time of 01:xx on 2026-11-01.
AUDIT_BEFORE_CHANGE = dt.datetime(2026, 11, 1, 5, 45, tzinfo=UTC)  # 01:45 EDT
AUDIT_AFTER_CHANGE = dt.datetime(2026, 11, 1, 6, 15, tzinfo=UTC)  # 01:15 EST

assert FIRST_AMBIGUOUS_0130_UTC < AUDIT_BEFORE_CHANGE < POLICY_CHANGE_UTC
assert POLICY_CHANGE_UTC < AUDIT_AFTER_CHANGE < SECOND_AMBIGUOUS_0130_UTC

NY_POLICY = RetentionPolicy(
    timezone="America/New_York",
    versions=[
        RetentionPolicyVersion(
            retention_days=30,
            effective_from=dt.datetime(2026, 1, 1, 0, 0, tzinfo=UTC),
        ),
        RetentionPolicyVersion(
            retention_days=7,
            effective_from=POLICY_CHANGE_UTC,
        ),
    ],
)


@pytest.fixture(scope="module")
def engine():
    return sa.create_engine("sqlite:///:memory:")


def _create_date_table(engine, table_name, dates):
    metadata = sa.MetaData()
    table = sa.Table(table_name, metadata, sa.Column("col_date", sa.Date()))
    with engine.begin() as connection:
        table.create(connection, checkfirst=True)
        connection.execute(
            table.insert().values(), [{"col_date": date} for date in dates]
        )


def _subject(name, table_name, policy):
    ref = DataReference(
        TableDataSource(db_name="", table_name=table_name), ["col_date"], None
    )
    return RetentionSubject(name=name, ref=ref, policy=policy)


@pytest.fixture(scope="module")
def archive_tables(engine):
    # Same batch of archives, audited repeatedly at different instants.
    _create_date_table(
        engine,
        "archive_ny",
        [dt.date(2026, 10, 10), dt.date(2026, 10, 28), dt.date(2026, 10, 31)],
    )
    # Configured source whose dates are all NULL.
    _create_date_table(engine, "archive_empty", [None, None])
    # Source without any retention policy configuration.
    _create_date_table(engine, "archive_unconfigured", [dt.date(2026, 10, 10)])
    # Source audited while UTC has moved on to the next day but the source's
    # local timezone has not.
    _create_date_table(
        engine, "archive_rollover", [dt.date(2026, 11, 12), dt.date(2026, 11, 19)]
    )
    return engine


def _ny_subjects():
    return [
        _subject("ny", "archive_ny", NY_POLICY),
        _subject("empty", "archive_empty", NY_POLICY),
        _subject("unconfigured", "archive_unconfigured", None),
    ]


def test_policy_version_selected_by_absolute_instant():
    assert NY_POLICY.version_at(AUDIT_BEFORE_CHANGE).retention_days == 30
    assert NY_POLICY.version_at(AUDIT_AFTER_CHANGE).retention_days == 7
    # The effective instant itself already belongs to the new version.
    assert NY_POLICY.version_at(POLICY_CHANGE_UTC).retention_days == 7
    # The two audit instants share the same ambiguous local wall clock hour.
    tz = NY_POLICY.timezone
    assert AUDIT_BEFORE_CHANGE.astimezone(tz).replace(tzinfo=None) == dt.datetime(
        2026, 11, 1, 1, 45
    )
    assert AUDIT_AFTER_CHANGE.astimezone(tz).replace(tzinfo=None) == dt.datetime(
        2026, 11, 1, 1, 15
    )


def test_lower_bound_uses_local_calendar_days():
    version, lower_bound = NY_POLICY.lower_bound_at(AUDIT_BEFORE_CHANGE)
    assert version.retention_days == 30
    assert lower_bound == dt.date(2026, 11, 1) - dt.timedelta(days=30)
    version, lower_bound = NY_POLICY.lower_bound_at(AUDIT_AFTER_CHANGE)
    assert version.retention_days == 7
    assert lower_bound == dt.date(2026, 11, 1) - dt.timedelta(days=7)


def test_reaudit_same_batch_at_two_ambiguous_local_moments(archive_tables):
    report_before = audit_retention(archive_tables, _ny_subjects(), AUDIT_BEFORE_CHANGE)
    report_after = audit_retention(archive_tables, _ny_subjects(), AUDIT_AFTER_CHANGE)

    ny_before = report_before.results[0]
    assert ny_before.status is RetentionStatus.COMPLIANT
    assert ny_before.policy_version.retention_days == 30
    assert ny_before.policy_version.effective_from == dt.datetime(
        2026, 1, 1, tzinfo=UTC
    )
    assert ny_before.lower_bound == dt.date(2026, 10, 2)
    assert ny_before.min_date == dt.date(2026, 10, 10)
    assert "2026-10-02" in ny_before.message

    ny_after = report_after.results[0]
    assert ny_after.status is RetentionStatus.VIOLATION
    assert ny_after.policy_version.retention_days == 7
    assert ny_after.policy_version.effective_from == POLICY_CHANGE_UTC
    assert ny_after.lower_bound == dt.date(2026, 10, 25)
    assert ny_after.min_date == dt.date(2026, 10, 10)
    assert "2026-10-25" in ny_after.message


def test_no_dates_and_no_policy_reported_separately(archive_tables):
    report = audit_retention(archive_tables, _ny_subjects(), AUDIT_AFTER_CHANGE)

    empty_result = report.results[1]
    assert empty_result.status is RetentionStatus.NO_COMPARABLE_DATES
    assert empty_result.min_date is None

    unconfigured_result = report.results[2]
    assert unconfigured_result.status is RetentionStatus.NO_POLICY
    assert unconfigured_result.policy_version is None

    # Neither category participates in the comparison denominator.
    assert report.comparable == (report.results[0],)
    assert report.without_dates == (empty_result,)
    assert report.without_policy == (unconfigured_result,)
    assert report.violation_fraction == 1.0
    assert not report.is_compliant


def test_local_date_not_yet_rolled_over(archive_tables):
    # At 02:00 UTC on 2026-11-20 it is still 2026-11-19 in New York.
    audit_instant = dt.datetime(2026, 11, 20, 2, 0, tzinfo=UTC)
    assert audit_instant.date() == dt.date(2026, 11, 20)
    assert NY_POLICY.local_date_at(audit_instant) == dt.date(2026, 11, 19)

    subjects = [_subject("rollover", "archive_rollover", NY_POLICY)]
    report = audit_retention(archive_tables, subjects, audit_instant)
    (result,) = report.results
    # Lower bound derives from the local date 2026-11-19, not the UTC date.
    assert result.lower_bound == dt.date(2026, 11, 12)
    assert result.min_date == dt.date(2026, 11, 12)
    assert result.status is RetentionStatus.COMPLIANT


@pytest.mark.parametrize("machine_timezone", ["America/Los_Angeles", "Asia/Tokyo"])
def test_machine_timezone_does_not_change_results(archive_tables, machine_timezone):
    previous_tz = os.environ.get("TZ")
    os.environ["TZ"] = machine_timezone
    time.tzset()
    try:
        report = audit_retention(archive_tables, _ny_subjects(), AUDIT_AFTER_CHANGE)
    finally:
        if previous_tz is None:
            del os.environ["TZ"]
        else:
            os.environ["TZ"] = previous_tz
        time.tzset()

    ny_result = report.results[0]
    assert ny_result.status is RetentionStatus.VIOLATION
    assert ny_result.lower_bound == dt.date(2026, 10, 25)
    assert ny_result.policy_version.effective_from == POLICY_CHANGE_UTC
    assert [r.status for r in report.results] == [
        RetentionStatus.VIOLATION,
        RetentionStatus.NO_COMPARABLE_DATES,
        RetentionStatus.NO_POLICY,
    ]


def test_audit_instant_must_be_timezone_aware(archive_tables):
    with pytest.raises(ValueError, match="naive"):
        audit_retention(archive_tables, _ny_subjects(), dt.datetime(2026, 11, 1, 6, 15))


def test_policy_version_must_be_timezone_aware():
    with pytest.raises(ValueError, match="naive"):
        RetentionPolicyVersion(
            retention_days=7, effective_from=dt.datetime(2026, 11, 1, 6, 0)
        )


def test_no_version_in_effect_yet_is_reported_as_no_policy(archive_tables):
    early_instant = dt.datetime(2025, 12, 31, 23, 59, tzinfo=UTC)
    subjects = [_subject("ny", "archive_ny", NY_POLICY)]
    report = audit_retention(archive_tables, subjects, early_instant)
    (result,) = report.results
    assert result.status is RetentionStatus.NO_POLICY
    assert report.comparable == ()
    assert report.violation_fraction is None


def test_versions_are_sorted_and_must_be_distinct():
    policy = RetentionPolicy(
        timezone="UTC",
        versions=[
            RetentionPolicyVersion(7, dt.datetime(2026, 6, 1, tzinfo=UTC)),
            RetentionPolicyVersion(30, dt.datetime(2026, 1, 1, tzinfo=UTC)),
        ],
    )
    assert [v.retention_days for v in policy.versions] == [30, 7]
    with pytest.raises(ValueError, match="distinct"):
        RetentionPolicy(
            timezone="UTC",
            versions=[
                RetentionPolicyVersion(7, dt.datetime(2026, 6, 1, tzinfo=UTC)),
                RetentionPolicyVersion(30, dt.datetime(2026, 6, 1, tzinfo=UTC)),
            ],
        )


def test_retention_exports_available_from_package_root():
    assert datajudge.audit_retention is audit_retention
    assert datajudge.RetentionPolicy is RetentionPolicy


def test_existing_date_min_constraint_still_works(archive_tables):
    req = requirements.WithinRequirement.from_table(None, None, "archive_ny")
    req.add_date_min_constraint("col_date", min_value="'2026-10-01'")
    (test_result,) = req.test(archive_tables)
    assert test_result.outcome

    req = requirements.WithinRequirement.from_table(None, None, "archive_ny")
    req.add_date_min_constraint("col_date", min_value="'2026-10-11'")
    (test_result,) = req.test(archive_tables)
    assert not test_result.outcome
