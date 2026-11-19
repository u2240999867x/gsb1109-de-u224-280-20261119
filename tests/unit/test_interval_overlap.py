"""Tests for the n-dimensional interval overlap detection.

The tests run against an in-memory SQLite database, which needs no external
service and supports the same SQLAlchemy expression language (including
``IS DISTINCT FROM`` via its SQLite rendering) used by the production query.
"""

from __future__ import annotations

import datetime as dt

import pytest
import sqlalchemy as sa

from datajudge.constraints.date import DateNoOverlap
from datajudge.constraints.interval import _format_overlap_sample
from datajudge.constraints.numeric import NumericNoOverlap
from datajudge.data_source import TableDataSource
from datajudge.db_access import DataReference, get_interval_overlaps_nd


def _make_engine(table_name: str, rows: list[dict], datetime_columns: bool):
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    column_type = sa.DateTime if datetime_columns else sa.Integer
    columns = [
        sa.Column("key", sa.Integer),
        sa.Column("start", column_type),
        sa.Column("end", column_type),
    ]
    table = sa.Table(table_name, metadata, *columns)
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(table.insert(), rows)
    ref = DataReference(
        TableDataSource("main", table_name=table_name),
        ["key", "start", "end"],
    )
    return engine, ref


def _overlaps(engine, ref, *, end_included, key_columns=("key",)):
    sample_selection, n_violations_selection = get_interval_overlaps_nd(
        engine,
        ref,
        list(key_columns) if key_columns else None,
        start_columns=["start"],
        end_columns=["end"],
        end_included=end_included,
    )
    with engine.connect() as connection:
        sample = connection.execute(sample_selection).first()
        n_violations = connection.execute(n_violations_selection).scalar()
    return None if sample is None else dict(sample._mapping), n_violations


def _date(day: int) -> dt.datetime:
    return dt.datetime(2024, 1, day)


@pytest.mark.parametrize("datetime_columns", [True, False])
def test_no_overlap(datetime_columns):
    if datetime_columns:
        rows = [
            {"key": 1, "start": _date(1), "end": _date(10)},
            {"key": 1, "start": _date(11), "end": _date(15)},
        ]
    else:
        rows = [{"key": 1, "start": 1, "end": 10}, {"key": 1, "start": 11, "end": 15}]
    engine, ref = _make_engine("no_overlap", rows, datetime_columns)
    sample, n_violations = _overlaps(engine, ref, end_included=True)
    assert sample is None
    assert n_violations == 0


def test_actual_overlap_reports_intersection_range():
    rows = [
        {"key": 1, "start": _date(1), "end": _date(10)},
        {"key": 1, "start": _date(7), "end": _date(15)},
    ]
    engine, ref = _make_engine("actual_overlap", rows, True)
    sample, n_violations = _overlaps(engine, ref, end_included=True)
    assert n_violations == 1
    assert sample["is_actual_overlap"] == 1
    assert sample["overlap_start_0"] == _date(7)
    assert sample["overlap_end_0"] == _date(10)
    assert sample["start_1"] == _date(1)
    assert sample["end_1"] == _date(10)
    assert sample["start_2"] == _date(7)
    assert sample["end_2"] == _date(15)


def test_boundary_only_is_distinguished_when_end_is_included():
    rows = [
        {"key": 1, "start": _date(1), "end": _date(10)},
        {"key": 1, "start": _date(10), "end": _date(15)},
    ]
    engine, ref = _make_engine("boundary_included", rows, True)
    sample, n_violations = _overlaps(engine, ref, end_included=True)
    assert n_violations == 1
    assert sample["is_actual_overlap"] == 0
    assert sample["overlap_start_0"] == sample["overlap_end_0"] == _date(10)


def test_boundary_only_is_not_an_overlap_when_end_is_excluded():
    rows = [
        {"key": 1, "start": _date(1), "end": _date(10)},
        {"key": 1, "start": _date(10), "end": _date(15)},
    ]
    engine, ref = _make_engine("boundary_excluded", rows, True)
    sample, n_violations = _overlaps(engine, ref, end_included=False)
    assert sample is None
    assert n_violations == 0


def test_open_ended_first_interval_participates():
    rows = [
        {"key": 1, "start": _date(1), "end": None},
        {"key": 1, "start": _date(5), "end": _date(10)},
    ]
    engine, ref = _make_engine("open_first", rows, True)
    sample, n_violations = _overlaps(engine, ref, end_included=True)
    assert n_violations == 1
    assert sample["is_actual_overlap"] == 1
    assert sample["end_1"] is None
    assert sample["overlap_start_0"] == _date(5)
    assert sample["overlap_end_0"] == _date(10)


def test_open_ended_second_interval_participates():
    rows = [
        {"key": 1, "start": _date(1), "end": _date(10)},
        {"key": 1, "start": _date(5), "end": None},
    ]
    engine, ref = _make_engine("open_second", rows, True)
    sample, n_violations = _overlaps(engine, ref, end_included=True)
    assert n_violations == 1
    assert sample["is_actual_overlap"] == 1
    assert sample["end_2"] is None
    assert sample["overlap_start_0"] == _date(5)
    assert sample["overlap_end_0"] == _date(10)


def test_two_open_ended_intervals_have_open_intersection():
    rows = [
        {"key": 1, "start": _date(1), "end": None},
        {"key": 1, "start": _date(5), "end": None},
    ]
    engine, ref = _make_engine("both_open", rows, True)
    sample, n_violations = _overlaps(engine, ref, end_included=True)
    assert n_violations == 1
    assert sample["overlap_start_0"] == _date(5)
    assert sample["overlap_end_0"] is None


def test_open_ended_boundary_respects_end_included():
    rows = [
        {"key": 1, "start": _date(1), "end": _date(10)},
        {"key": 1, "start": _date(10), "end": None},
    ]
    engine, ref = _make_engine("open_boundary", rows, True)
    sample_included, n_included = _overlaps(engine, ref, end_included=True)
    assert n_included == 1
    assert sample_included["is_actual_overlap"] == 0
    sample_excluded, n_excluded = _overlaps(engine, ref, end_included=False)
    assert sample_excluded is None
    assert n_excluded == 0


def test_open_ended_interval_without_intersection_is_clean():
    rows = [
        {"key": 1, "start": _date(1), "end": _date(5)},
        {"key": 1, "start": _date(6), "end": None},
    ]
    engine, ref = _make_engine("open_clean", rows, True)
    sample, n_violations = _overlaps(engine, ref, end_included=True)
    assert sample is None
    assert n_violations == 0


def test_duplicate_closed_intervals_are_detected():
    rows = [
        {"key": 1, "start": _date(1), "end": _date(10)},
        {"key": 1, "start": _date(1), "end": _date(10)},
    ]
    engine, ref = _make_engine("duplicate_closed", rows, True)
    sample, n_violations = _overlaps(engine, ref, end_included=True)
    assert n_violations == 1
    # A duplicate is reported as a pair of identical intervals whose
    # intersection spans the whole interval.
    assert sample["key"] == 1
    assert sample["is_actual_overlap"] == 1
    assert sample["start_1"] == sample["start_2"] == _date(1)
    assert sample["end_1"] == sample["end_2"] == _date(10)
    assert sample["overlap_start_0"] == _date(1)
    assert sample["overlap_end_0"] == _date(10)


def test_duplicate_open_ended_intervals_are_detected():
    rows = [
        {"key": 1, "start": _date(1), "end": None},
        {"key": 1, "start": _date(1), "end": None},
    ]
    engine, ref = _make_engine("duplicate_open", rows, True)
    sample, n_violations = _overlaps(engine, ref, end_included=True)
    assert n_violations == 1
    assert sample["key"] == 1
    assert sample["is_actual_overlap"] == 1
    assert sample["start_1"] == sample["start_2"] == _date(1)
    assert sample["end_1"] is None and sample["end_2"] is None
    assert sample["overlap_end_0"] is None


def test_first_pair_is_ordered_by_intersection_start():
    rows = [
        {"key": 1, "start": _date(1), "end": _date(20)},
        {"key": 1, "start": _date(15), "end": _date(25)},
        {"key": 1, "start": _date(5), "end": _date(8)},
    ]
    engine, ref = _make_engine("first_pair", rows, True)
    sample, n_violations = _overlaps(engine, ref, end_included=True)
    assert n_violations == 1
    # The pair intersecting on Jan 5th starts earlier than the one on Jan 15th.
    assert sample["overlap_start_0"] == _date(5)
    assert sample["start_2"] == _date(5)


def test_overlaps_are_grouped_by_key():
    rows = [
        {"key": 1, "start": _date(1), "end": _date(10)},
        {"key": 1, "start": _date(5), "end": _date(8)},
        {"key": 1, "start": _date(6), "end": _date(7)},
        {"key": 2, "start": _date(1), "end": _date(10)},
        {"key": 2, "start": _date(2), "end": _date(3)},
        {"key": 3, "start": _date(1), "end": _date(10)},
    ]
    engine, ref = _make_engine("grouped_keys", rows, True)
    _, n_violations = _overlaps(engine, ref, end_included=True)
    # Several overlapping pairs exist for key 1, but it counts as one key, as
    # does key 2. Key 3 is clean.
    assert n_violations == 2


def test_overlaps_of_different_keys_are_ignored():
    rows = [
        {"key": 1, "start": _date(1), "end": _date(10)},
        {"key": 2, "start": _date(5), "end": _date(8)},
    ]
    engine, ref = _make_engine("different_keys", rows, True)
    sample, n_violations = _overlaps(engine, ref, end_included=True)
    assert sample is None
    assert n_violations == 0


def test_without_key_columns_every_row_is_compared():
    rows = [
        {"key": 1, "start": _date(1), "end": _date(10)},
        {"key": 2, "start": _date(5), "end": _date(8)},
    ]
    engine, ref = _make_engine("no_key_columns", rows, True)
    sample, n_violations = _overlaps(
        engine, ref, end_included=True, key_columns=()
    )
    assert n_violations == 1
    assert sample is not None


class _FakeRow:
    def __init__(self, mapping):
        self._mapping = mapping


def test_format_overlap_sample_for_actual_pair():
    row = _FakeRow(
        {
            "key": 42,
            "start_1": _date(1),
            "end_1": _date(10),
            "start_2": _date(5),
            "end_2": None,
            "overlap_start_0": _date(5),
            "overlap_end_0": _date(10),
            "is_actual_overlap": 1,
        }
    )
    message = _format_overlap_sample(
        row, ["key"], ["start"], ["end"]
    )
    assert "key (key = 42)" in message
    assert "intersect on" in message
    assert "actual overlap" in message


def test_format_overlap_sample_for_boundary_only_pair():
    row = _FakeRow(
        {
            "key": 42,
            "start_1": _date(1),
            "end_1": _date(10),
            "start_2": _date(10),
            "end_2": _date(15),
            "overlap_start_0": _date(10),
            "overlap_end_0": _date(10),
            "is_actual_overlap": 0,
        }
    )
    message = _format_overlap_sample(
        row, ["key"], ["start"], ["end"]
    )
    assert "boundary-only overlap" in message
    assert "actual overlap" not in message


def test_format_overlap_sample_for_duplicate_row():
    row = _FakeRow(
        {
            "key": 7,
            "start_1": _date(1),
            "end_1": None,
            "start_2": _date(1),
            "end_2": None,
            "overlap_start_0": _date(1),
            "overlap_end_0": None,
            "is_actual_overlap": 1,
        }
    )
    message = _format_overlap_sample(
        row, ["key"], ["start"], ["end"]
    )
    # The duplicate is rendered as a pair of identical open-ended intervals.
    assert "key (key = 7)" in message
    assert "intersect on" in message
    assert "actual overlap" in message


def test_date_constraint_failure_message_describes_pair():
    rows = [
        {"key": 1, "start": _date(1), "end": None},
        {"key": 1, "start": _date(5), "end": _date(8)},
    ]
    engine, ref = _make_engine("date_constraint_message", rows, True)
    constraint = DateNoOverlap(
        ref,
        key_columns=["key"],
        start_columns=["start"],
        end_columns=["end"],
        max_relative_n_violations=0,
        end_included=True,
    )
    result = constraint.test(engine)
    assert not result.outcome
    assert "First intersecting pair" in result.failure_message
    assert "actual overlap" in result.failure_message


def test_numeric_constraint_failure_message_describes_pair():
    rows = [
        {"key": 1, "start": 1, "end": 10},
        {"key": 1, "start": 10, "end": 20},
    ]
    engine, ref = _make_engine("numeric_constraint_message", rows, False)
    constraint = NumericNoOverlap(
        ref,
        key_columns=["key"],
        start_columns=["start"],
        end_columns=["end"],
        max_relative_n_violations=0,
        end_included=True,
    )
    result = constraint.test(engine)
    assert not result.outcome
    assert "First intersecting pair" in result.failure_message
    assert "boundary-only overlap" in result.failure_message
