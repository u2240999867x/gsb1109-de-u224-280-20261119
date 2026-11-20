"""Reconciliation gatekeeping with datajudge.

The scenario models daily store reports ("日报") that are delivered per
*source* (e.g. a POS feed or a wallet feed). The business rules under test are:

* Each store's daily net amount (sum of the report rows) must stay within the
  store-specific threshold interval. Totals are never compared across stores so
  that reversals of one store cannot cancel another store's amounts.
* A checklist declares which sources every store has to hand in. A source can
  replace a previously handed-in report wholesale by uploading a new version;
  only the newest version visible at the reconciliation point counts.
* Groups whose checklist is not fully handed in yet are not judged against the
  interval: they only report what was observed and which sources are missing.
* Reports with only empty (``NULL``) amounts are handed in but carry no amount;
  they must not be equated with a net amount of zero.

All checks are expressed as datajudge constraints and every query issued
against the database is an aggregate or set-level query. No batch of rows or
line-level details is pulled into the validation process to be summed locally.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from datajudge import BetweenRequirement, Condition, WithinRequirement, db_access
from datajudge.data_source import RawQueryDataSource
from datajudge.db_access import DataReference

REPORTS_DDL = """
CREATE TABLE reports (
    store   TEXT NOT NULL,
    source  TEXT NOT NULL,
    version INTEGER NOT NULL,
    amount INTEGER
)
"""

CHECKLIST_DDL = """
CREATE TABLE expected_sources (
    store       TEXT NOT NULL,
    source      TEXT NOT NULL,
    lower_bound REAL NOT NULL,
    upper_bound REAL NOT NULL
)
"""

# The reconciliation point pins the admissible versions: a report only counts
# if its version was already visible at that point in time.
RECONCILIATION_POINTS = {
    "first_reconciliation": 1,
    "second_reconciliation": 2,
}


@pytest.fixture(scope="module")
def engine():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(sa.text(REPORTS_DDL))
        connection.execute(sa.text(CHECKLIST_DDL))
        connection.execute(
            sa.text(
                "INSERT INTO reports (store, source, version, amount) "
                "VALUES (:store, :source, :version, :amount)"
            ),
            [
                # Store A: old report contains +30 and a -30 reversal ...
                {"store": "store_a", "source": "pos", "version": 1, "amount": 30},
                {"store": "store_a", "source": "pos", "version": 1, "amount": -30},
                # ... the full-file revision drops the -30 row and keeps +30.
                {"store": "store_a", "source": "pos", "version": 2, "amount": 30},
                # Store B: both required sources handed in, all amounts empty.
                {"store": "store_b", "source": "pos", "version": 1, "amount": None},
                {"store": "store_b", "source": "wallet", "version": 1, "amount": None},
                # Store C: one source present (empty), the other is still missing.
                {"store": "store_c", "source": "pos", "version": 1, "amount": None},
                # The missing empty report is handed in before the second run.
                {"store": "store_c", "source": "online", "version": 2, "amount": None},
            ],
        )
        connection.execute(
            sa.text(
                "INSERT INTO expected_sources "
                "(store, source, lower_bound, upper_bound) "
                "VALUES (:store, :source, :lower_bound, :upper_bound)"
            ),
            [
                {
                    "store": "store_a",
                    "source": "pos",
                    "lower_bound": -10,
                    "upper_bound": 10,
                },
                {
                    "store": "store_b",
                    "source": "pos",
                    "lower_bound": -5,
                    "upper_bound": 5,
                },
                {
                    "store": "store_b",
                    "source": "wallet",
                    "lower_bound": -5,
                    "upper_bound": 5,
                },
                {
                    "store": "store_c",
                    "source": "pos",
                    "lower_bound": -5,
                    "upper_bound": 5,
                },
                {
                    "store": "store_c",
                    "source": "online",
                    "lower_bound": -5,
                    "upper_bound": 5,
                },
            ],
        )
    return engine


def _pinned_reports_query(as_of_version: int) -> str:
    """Reports as visible at the reconciliation point, latest version per source.

    The correlated subquery fixes the version inside the database: old and new
    versions of the same source never coexist in a reconciliation run.
    """
    return f"""
        SELECT store, source, version, amount
        FROM reports AS r
        WHERE version <= {as_of_version}
          AND version = (
              SELECT max(version)
              FROM reports AS q
              WHERE q.store = r.store
                AND q.source = r.source
                AND q.version <= {as_of_version}
          )
    """


def _observed_sources_query(as_of_version: int) -> str:
    return (
        f"SELECT DISTINCT store, source FROM ({_pinned_reports_query(as_of_version)})"
    )


class ReconciliationSummary:
    """Database-side reconciliation view for one (reconciliation point, store)."""

    def __init__(self, engine, store: str, as_of_version: int):
        self.store = store
        self.as_of_version = as_of_version
        pinned = _pinned_reports_query(as_of_version)
        store_filter = Condition(raw_string=f"store = '{store}'")
        pinned_reference = DataReference(
            RawQueryDataSource(
                pinned,
                f"pinned_reports@{as_of_version}",
                columns=["store", "source", "version", "amount"],
            ),
            columns=["amount"],
            condition=store_filter,
        )

        with engine.connect() as connection:
            self.versions_used = connection.execute(
                sa.text(
                    f"SELECT source, max(version) FROM ({pinned}) "
                    "WHERE store = :store GROUP BY source ORDER BY source"
                ),
                {"store": store},
            ).all()
            self.allowed_intervals = connection.execute(
                sa.text(
                    "SELECT source, lower_bound, upper_bound "
                    "FROM expected_sources WHERE store = :store ORDER BY source"
                ),
                {"store": store},
            ).all()
            self.missing_sources = [
                row[0]
                for row in connection.execute(
                    sa.text(
                        "SELECT source FROM expected_sources "
                        "WHERE store = :store "
                        "EXCEPT "
                        f"SELECT source FROM ({_observed_sources_query(as_of_version)}) "
                        "WHERE store = :store ORDER BY source"
                    ),
                    {"store": store},
                ).all()
            ]

        # Both figures are aggregate queries: only scalars cross the boundary.
        self.non_null_amount_rows, _ = db_access.get_row_count(
            engine,
            DataReference(
                pinned_reference.data_source,
                columns=["amount"],
                condition=Condition(
                    conditions=[
                        store_filter,
                        Condition(raw_string="amount IS NOT NULL"),
                    ],
                    reduction_operator="and",
                ),
            ),
        )
        self.net_amount, _ = db_access.get_sum(engine, pinned_reference)

    @property
    def is_complete(self) -> bool:
        return not self.missing_sources

    @property
    def has_amount(self) -> bool:
        return self.net_amount is not None


def _coverage_requirement(store: str, as_of_version: int) -> BetweenRequirement:
    """Every checklist source of the store must be present among handed-in reports."""
    requirement = BetweenRequirement.from_raw_queries(
        _observed_sources_query(as_of_version),
        "SELECT store, source FROM expected_sources",
        f"observed_sources@{as_of_version}",
        "expected_sources",
        columns1=["store", "source"],
        columns2=["store", "source"],
    )
    store_condition = Condition(raw_string=f"store = '{store}'")
    requirement.add_row_superset_constraint(
        columns1=["store", "source"],
        columns2=["store", "source"],
        constant_max_missing_fraction=0,
        condition1=store_condition,
        condition2=Condition(raw_string=f"store = '{store}'"),
        name=f"coverage::{store}@v{as_of_version}",
    )
    return requirement


def _net_amount_requirement(
    store: str, as_of_version: int, lower_bound: float, upper_bound: float
) -> WithinRequirement:
    """Net amount of the store stays within its checklist threshold interval."""
    requirement = WithinRequirement.from_raw_query(
        _pinned_reports_query(as_of_version),
        f"pinned_reports@{as_of_version}",
        columns=["store", "source", "version", "amount"],
    )
    requirement.add_numeric_sum_constraint(
        column="amount",
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        condition=Condition(raw_string=f"store = '{store}'"),
        name=f"net_amount::{store}@v{as_of_version}",
    )
    return requirement


def _reconcile(engine, as_of_version: int) -> dict[str, ReconciliationSummary]:
    summaries = {
        store: ReconciliationSummary(engine, store, as_of_version)
        for store in ("store_a", "store_b", "store_c")
    }
    for store, summary in summaries.items():
        coverage_result = _coverage_requirement(store, as_of_version).test(engine)[0]
        summary.coverage_passed = coverage_result.outcome

        # Incomplete groups only report observations and missing sources; they
        # are never judged against the net-amount interval.
        if not summary.is_complete:
            summary.net_result = None
            continue

        # The store-level interval is the sum of its source-level intervals.
        lower_bound = sum(row.lower_bound for row in summary.allowed_intervals)
        upper_bound = sum(row.upper_bound for row in summary.allowed_intervals)
        net_result = _net_amount_requirement(
            store, as_of_version, lower_bound, upper_bound
        ).test(engine)[0]
        summary.net_result = net_result
        # An empty (all-NULL) report set has no amount and hence no zero verdict.
        if not summary.has_amount:
            assert net_result.outcome is True
            assert net_result.failure_message is None
    return summaries


def test_first_reconciliation_uses_old_versions(engine):
    summaries = _reconcile(engine, RECONCILIATION_POINTS["first_reconciliation"])

    store_a = summaries["store_a"]
    assert store_a.versions_used == [("pos", 1)]
    assert store_a.non_null_amount_rows == 2
    assert store_a.is_complete and store_a.has_amount
    # +30 and -30 net to zero, which lies inside store A's interval.
    assert store_a.net_amount == 0
    assert store_a.coverage_passed is True
    assert store_a.net_result.outcome is True

    store_b = summaries["store_b"]
    assert store_b.versions_used == [("pos", 1), ("wallet", 1)]
    assert store_b.non_null_amount_rows == 0
    assert store_b.is_complete and not store_b.has_amount
    assert store_b.coverage_passed is True
    # Handed in but amount-less: no net pass/fail, and never counted as zero.
    assert store_b.net_result is not None
    assert store_b.net_amount is None

    store_c = summaries["store_c"]
    assert store_c.versions_used == [("pos", 1)]
    assert store_c.non_null_amount_rows == 0
    assert not store_c.is_complete
    assert store_c.missing_sources == ["online"]
    assert store_c.coverage_passed is False
    # No interval verdict while the checklist is incomplete.
    assert store_c.net_result is None


def test_second_reconciliation_uses_revision_and_completes_store_c(engine):
    summaries = _reconcile(engine, RECONCILIATION_POINTS["second_reconciliation"])

    store_a = summaries["store_a"]
    # The old version is still in the table but pinned out of the run; the
    # re-reported revision replaces it wholesale, so only +30 remains.
    assert store_a.versions_used == [("pos", 2)]
    assert store_a.non_null_amount_rows == 1
    assert store_a.is_complete and store_a.has_amount
    assert store_a.net_amount == 30
    assert store_a.coverage_passed is True
    assert store_a.net_result.outcome is False
    failure_message = store_a.net_result.failure_message
    assert "sum 30 outside of the allowed interval" in failure_message
    assert "-10" in failure_message and "10" in failure_message

    store_b = summaries["store_b"]
    # Re-reporting elsewhere changes neither B's net amount nor its coverage.
    assert store_b.versions_used == [("pos", 1), ("wallet", 1)]
    assert store_b.non_null_amount_rows == 0
    assert store_b.is_complete and not store_b.has_amount
    assert store_b.coverage_passed is True
    assert store_b.net_amount is None

    store_c = summaries["store_c"]
    assert store_c.versions_used == [("online", 2), ("pos", 1)]
    assert store_c.non_null_amount_rows == 0
    assert store_c.is_complete and not store_c.has_amount
    assert store_c.missing_sources == []
    assert store_c.coverage_passed is True
    # Complete but amount-less: still no zero and no interval failure.
    assert store_c.net_result is not None
    assert store_c.net_amount is None


def test_only_aggregate_and_set_level_queries_reach_the_database(engine):
    executed_statements: list[str] = []

    @sa.event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        executed_statements.append(statement.lower())

    try:
        _reconcile(engine, RECONCILIATION_POINTS["second_reconciliation"])
    finally:
        sa.event.remove(engine, "before_cursor_execute", _record)

    assert executed_statements
    for statement in executed_statements:
        # Statements touching the line-level report table must aggregate or
        # compare sets in the database; no report batch may be fetched merely
        # to be summed inside the validation process.
        if "reports" in statement:
            assert any(
                keyword in statement for keyword in ("sum(", "count(", "except", "max(")
            ), statement
