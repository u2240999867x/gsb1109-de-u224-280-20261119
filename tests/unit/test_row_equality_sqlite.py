"""Regression tests for multiset-aware, partition-aware row equality.

These tests run against SQLite and therefore do not require one of the
external database backends used by ``test_integration.py``.
"""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from datajudge import BetweenRequirement, db_access

OLD_ROWS = [
    {"batch": "A", "amount": 10, "note": None},
    {"batch": "A", "amount": 10, "note": None},
    {"batch": "A", "amount": 10, "note": None},
    {"batch": "B", "amount": 20, "note": None},
    {"batch": "C", "amount": 30, "note": None},
    {"batch": "C", "amount": 30, "note": None},
]
NEW_ROWS = [
    {"batch": "A", "amount": 10, "note": None},
    {"batch": "A", "amount": 10, "note": None},
    {"batch": "B", "amount": 20, "note": None},
    {"batch": "B", "amount": 20, "note": None},
    {"batch": "C", "amount": 30, "note": None},
    {"batch": "C", "amount": 30, "note": None},
]
COLUMNS = ["batch", "amount", "note"]


@pytest.fixture()
def engine(tmp_path):
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'rows.db'}")
    metadata = sa.MetaData()
    old = sa.Table(
        "old",
        metadata,
        sa.Column("batch", sa.String),
        sa.Column("amount", sa.Integer),
        sa.Column("note", sa.String),
    )
    new = old.to_metadata(metadata, name="new")
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(old.insert(), OLD_ROWS)
        connection.execute(new.insert(), NEW_ROWS)
    return engine


def _equality_constraint(**kwargs):
    req = BetweenRequirement.from_tables("db", None, "old", "db", None, "new")
    req.add_row_equality_constraint(
        COLUMNS,
        COLUMNS,
        kwargs.pop("max_missing_fraction", 0),
        **kwargs,
    )
    return req[0]


def _apply_correction(engine):
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "delete from new where rowid = "
                "(select min(rowid) from new where batch = 'B')"
            )
        )
        connection.execute(
            sa.text("insert into new (batch, amount, note) values ('A', 10, NULL)")
        )


def test_multiset_difference_is_not_net_compensated(engine):
    result = _equality_constraint().test(engine)
    assert not result.outcome
    message = result.failure_message
    assert "1 row occurrence(s) are missing" in message
    assert "1 are in excess" in message
    assert "multiset union size 7" in message


def test_partitions_report_separate_differences(engine):
    result = _equality_constraint(
        partition_columns1=["batch"], partition_columns2=["batch"]
    ).test(engine)
    message = result.failure_message
    assert "[batch = A]: missing 1, excess 0" in message
    assert "[batch = B]: missing 0, excess 1" in message
    assert "[batch = C]: missing 0, excess 0" in message


def test_counterexamples_show_values_batch_and_both_counts(engine):
    constraint = _equality_constraint(
        partition_columns1=["batch"], partition_columns2=["batch"]
    )
    result = constraint.test(engine)
    message = result.failure_message
    assert (
        "partition [batch = A]: batch = A , amount = 10 , note = None "
        "(count1 = 3, count2 = 2)" in message
    )
    assert (
        "partition [batch = B]: batch = B , amount = 20 , note = None "
        "(count1 = 1, count2 = 2)" in message
    )
    assert constraint.missing_count == 1
    assert constraint.excess_count == 1
    assert constraint.union_size == 7
    (missing,) = constraint.missing_samples
    assert missing.values == ("A", 10, None)
    assert (missing.count1, missing.count2) == (3, 2)
    (excess,) = constraint.excess_samples
    assert excess.values == ("B", 20, None)
    assert (excess.count1, excess.count2) == (1, 2)


def test_counterexamples_are_limited(engine):
    result = _equality_constraint(
        partition_columns1=["batch"],
        partition_columns2=["batch"],
        n_counterexamples=1,
    ).test(engine)
    assert result.failure_message.count("count1 = 3, count2 = 2") == 1


def test_distinct_mode_keeps_set_semantics(engine):
    assert _equality_constraint(compare_distinct=True).test(engine).outcome


def test_null_is_a_value_and_repeats_accumulate(engine):
    counts, _ = db_access.get_row_difference_counts(
        engine,
        db_access.DataReference(db_access.TableDataSource("db", "old"), COLUMNS),
        db_access.DataReference(db_access.TableDataSource("db", "new"), COLUMNS),
    )
    assert counts == (1, 1, 7)


def test_snapshot_freezes_counts_and_samples(engine, monkeypatch):
    constraint = _equality_constraint(
        partition_columns1=["batch"], partition_columns2=["batch"]
    )
    original = db_access.get_row_difference_count_samples

    def mutate_before_sampling(bind, *args, **kwargs):
        # The counts have been read from a snapshot that is still open. All
        # further reads must use the same transaction.
        assert isinstance(bind, sa.Connection)
        assert bind.in_transaction()
        # A correction batch arriving now cannot commit against the locked
        # snapshot and therefore cannot leak into this validation.
        with pytest.raises(sa.exc.OperationalError):
            _apply_correction(engine)
        return original(bind, *args, **kwargs)

    monkeypatch.setattr(
        db_access, "get_row_difference_count_samples", mutate_before_sampling
    )
    first = constraint.test(engine)
    assert not first.outcome
    assert "1 row occurrence(s) are missing" in first.failure_message
    assert "1 are in excess" in first.failure_message
    assert "[batch = A]: missing 1, excess 0" in first.failure_message
    assert "[batch = B]: missing 0, excess 1" in first.failure_message

    monkeypatch.setattr(db_access, "get_row_difference_count_samples", original)
    # A separate validation starts a new snapshot and thus sees the
    # correction batch.
    _apply_correction(engine)
    second = _equality_constraint(
        partition_columns1=["batch"], partition_columns2=["batch"]
    ).test(engine)
    assert second.outcome


def test_correction_batch_is_used_by_next_validation(engine):
    assert not _equality_constraint().test(engine).outcome
    _apply_correction(engine)
    assert _equality_constraint().test(engine).outcome


def test_partition_column_position_and_renamed_columns(engine):
    # The batch column comes last here, and the second table names it
    # differently. Both sides still partition on the supply batch.
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "create table renamed (amount integer, note varchar, supply_batch varchar)"
        )
        connection.exec_driver_sql(
            "insert into renamed select amount, note, batch from new"
        )
    columns1 = ["amount", "note", "batch"]
    columns2 = ["amount", "note", "supply_batch"]
    req = BetweenRequirement.from_tables("db", None, "old", "db", None, "renamed")
    req.add_row_equality_constraint(
        columns1,
        columns2,
        0,
        partition_columns1=["batch"],
        partition_columns2=["supply_batch"],
    )
    constraint = req[0]
    result = constraint.test(engine)
    assert not result.outcome
    message = result.failure_message
    assert "[batch = A]: missing 1, excess 0" in message
    assert "[batch = B]: missing 0, excess 1" in message
    assert constraint.partition_differences[0].partition == ("A",)
    (missing,) = constraint.missing_samples
    assert missing.values == (10, None, "A")


def test_missing_partition_column_is_rejected(engine):
    req = BetweenRequirement.from_tables("db", None, "old", "db", None, "new")
    req.add_row_equality_constraint(
        COLUMNS,
        COLUMNS,
        0,
        partition_columns1=["not_selected"],
        partition_columns2=["not_selected"],
    )
    with pytest.raises(ValueError, match="not selected"):
        req[0].test(engine)


def test_no_counterexamples_still_reports_counts(engine):
    constraint = _equality_constraint(
        partition_columns1=["batch"],
        partition_columns2=["batch"],
        n_counterexamples=0,
    )
    result = constraint.test(engine)
    assert not result.outcome
    assert constraint.missing_count == 1
    assert constraint.excess_count == 1
    assert constraint.missing_samples == ()
    assert constraint.excess_samples == ()
    assert "missing 1" in result.failure_message


def test_null_partition_forms_its_own_partition(engine):
    with engine.begin() as connection:
        connection.execute(
            sa.text("insert into old (batch, amount, note) values (NULL, 40, NULL)")
        )
        connection.execute(
            sa.text(
                "insert into new (batch, amount, note) "
                "select NULL, 40, NULL from (select 1 union all select 2)"
            )
        )
    constraint = _equality_constraint(
        partition_columns1=["batch"], partition_columns2=["batch"]
    )
    result = constraint.test(engine)
    assert not result.outcome
    partitions = {
        difference.partition: (difference.missing, difference.excess)
        for difference in constraint.partition_differences
    }
    assert partitions[(None,)] == (0, 1)
    assert partitions[("A",)] == (1, 0)
    assert partitions[("B",)] == (0, 1)
    assert "[batch = None]: missing 0, excess 1" in result.failure_message
