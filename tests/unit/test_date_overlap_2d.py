"""Regression tests for two-dimensional, NULL-safe date overlap detection.

These tests run against an in-memory SQLite database. They only rely on generic
SQLAlchemy operators, so they exercise the exact same query construction as the
supported production backends without needing a database server.
"""

from __future__ import annotations

import datetime as dt
import itertools
import random

import pytest
import sqlalchemy as sa

from datajudge import WithinRequirement
from datajudge.data_source import ExpressionDataSource
from datajudge.db_access import (
    DataReference,
    get_interval_overlaps_nd_with_intersection,
)

D = dt.date


def _build_columns():
    return [
        sa.Column("account_id", sa.Integer),
        sa.Column("contract_no", sa.String),
        sa.Column("biz_start", sa.Date),
        sa.Column("biz_end", sa.Date),
        sa.Column("vis_start", sa.Date),
        sa.Column("vis_end", sa.Date),
    ]


_STARTS = ["biz_start", "vis_start"]
_ENDS = ["biz_end", "vis_end"]


def _engine_with(rows, table_name="contracts"):
    engine = sa.create_engine("sqlite:///:memory:")
    metadata = sa.MetaData()
    table = sa.Table(table_name, metadata, *_build_columns())
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(table.insert(), rows)
    return engine, table


def _overlap_rows(engine, table, end_included):
    ref = DataReference(
        ExpressionDataSource(table, table.name),
        [
            "account_id",
            "contract_no",
            "biz_start",
            "biz_end",
            "vis_start",
            "vis_end",
        ],
        None,
    )
    sample_selection, n_violations_selection = (
        get_interval_overlaps_nd_with_intersection(
            engine,
            ref,
            key_columns=["account_id"],
            start_columns=_STARTS,
            end_columns=_ENDS,
            end_included=list(end_included),
            identifier_columns=["contract_no"],
        )
    )
    with engine.connect() as connection:
        rows = [
            dict(mapping) for mapping in connection.execute(sample_selection).mappings()
        ]
        n_violating_keys = connection.execute(n_violations_selection).scalar()
    return rows, n_violating_keys


def _requirement_outcome(engine, table, end_included):
    requirement = WithinRequirement.from_expression(table, table.name)
    requirement.add_date_no_overlap_2d_heterogeneous_constraint(
        start_column1="biz_start",
        end_column1="biz_end",
        start_column2="vis_start",
        end_column2="vis_end",
        key_columns=["account_id"],
        end_included=list(end_included),
        identifier_columns=["contract_no"],
        max_relative_n_violations=0,
    )
    return requirement[0].test(engine).outcome


def _normalized_pairs(rows):
    normalized = []
    for row in rows:
        identifier_a, identifier_b = sorted(
            [row["contract_no_1"], row["contract_no_2"]]
        )
        normalized.append(
            (
                identifier_a,
                identifier_b,
                row["intersection_start_0"],
                row["intersection_end_0"],
                row["intersection_start_1"],
                row["intersection_end_1"],
            )
        )
    return sorted(normalized)


def _prompt_rows():
    # A: business Jan 1-10 (inclusive end); visibility from Jan 5, still visible.
    # B: business from Jan 10, still valid; visibility Jan 3-6 (exclusive end).
    return [
        dict(
            account_id=1,
            contract_no="A",
            biz_start=D(2026, 1, 1),
            biz_end=D(2026, 1, 10),
            vis_start=D(2026, 1, 5),
            vis_end=None,
        ),
        dict(
            account_id=1,
            contract_no="B",
            biz_start=D(2026, 1, 10),
            biz_end=None,
            vis_start=D(2026, 1, 3),
            vis_end=D(2026, 1, 6),
        ),
    ]


def test_mixed_end_inclusion_with_open_ends_reports_pair_and_intersections():
    engine, table = _engine_with(_prompt_rows())
    rows, n_violating_keys = _overlap_rows(engine, table, [True, False])

    assert n_violating_keys == 1
    assert len(rows) == 1
    pair = rows[0]
    assert sorted([pair["contract_no_1"], pair["contract_no_2"]]) == ["A", "B"]

    # Business intersection is the single inclusive day Jan 10.
    assert pair["intersection_start_0"] == D(2026, 1, 10)
    assert pair["intersection_end_0"] == D(2026, 1, 10)
    # Visibility intersection is [Jan 5, Jan 6): end is exclusive, hence Jan 6.
    assert pair["intersection_start_1"] == D(2026, 1, 5)
    assert pair["intersection_end_1"] == D(2026, 1, 6)

    # The constraint flags the overlapping pair as a violation.
    assert _requirement_outcome(engine, table, [True, False]) is False


def test_moving_exclusive_end_to_visibility_start_removes_overlap():
    rows = _prompt_rows()
    rows[1]["vis_end"] = D(2026, 1, 5)
    engine, table = _engine_with(rows)

    overlap_rows, n_violating_keys = _overlap_rows(engine, table, [True, False])
    assert overlap_rows == []
    assert n_violating_keys == 0
    assert _requirement_outcome(engine, table, [True, False]) is True


def test_identical_intervals_with_distinct_identifiers_pair_once():
    rows = [
        dict(
            account_id=2,
            contract_no="X",
            biz_start=D(2026, 1, 1),
            biz_end=D(2026, 1, 10),
            vis_start=D(2026, 1, 5),
            vis_end=D(2026, 1, 8),
        ),
        dict(
            account_id=2,
            contract_no="Y",
            biz_start=D(2026, 1, 1),
            biz_end=D(2026, 1, 10),
            vis_start=D(2026, 1, 5),
            vis_end=D(2026, 1, 8),
        ),
    ]
    engine, table = _engine_with(rows)
    overlap_rows, _ = _overlap_rows(engine, table, [True, True])

    assert len(overlap_rows) == 1
    pair = overlap_rows[0]
    # Reported once as the two distinct contracts, never as a self-comparison.
    assert sorted([pair["contract_no_1"], pair["contract_no_2"]]) == ["X", "Y"]
    assert pair["intersection_start_0"] == D(2026, 1, 1)
    assert pair["intersection_end_0"] == D(2026, 1, 10)
    assert pair["intersection_start_1"] == D(2026, 1, 5)
    assert pair["intersection_end_1"] == D(2026, 1, 8)


def test_exact_duplicate_rows_are_reported_as_a_pair():
    duplicate = dict(
        account_id=2,
        contract_no="Z",
        biz_start=D(2026, 1, 1),
        biz_end=D(2026, 1, 10),
        vis_start=D(2026, 1, 5),
        vis_end=D(2026, 1, 8),
    )
    engine, table = _engine_with([dict(duplicate), dict(duplicate)])
    overlap_rows, _ = _overlap_rows(engine, table, [True, True])

    # The two distinct physical rows form a single pair, never a self-match.
    assert len(overlap_rows) == 1
    pair = overlap_rows[0]
    assert pair["contract_no_1"] == "Z"
    assert pair["contract_no_2"] == "Z"
    assert _requirement_outcome(engine, table, [True, True]) is False


def test_row_order_reversal_leaves_selection_and_intersections_unchanged():
    forward_engine, forward_table = _engine_with(_prompt_rows())
    reversed_engine, reversed_table = _engine_with(list(reversed(_prompt_rows())))

    forward_pairs, _ = _overlap_rows(forward_engine, forward_table, [True, False])
    reversed_pairs, _ = _overlap_rows(reversed_engine, reversed_table, [True, False])
    assert _normalized_pairs(forward_pairs) == _normalized_pairs(reversed_pairs)


def test_open_ended_intersection_yields_null_intersection_end():
    rows = [
        dict(
            account_id=3,
            contract_no="A",
            biz_start=D(2026, 1, 1),
            biz_end=None,
            vis_start=D(2026, 1, 1),
            vis_end=None,
        ),
        dict(
            account_id=3,
            contract_no="B",
            biz_start=D(2026, 1, 4),
            biz_end=None,
            vis_start=D(2026, 1, 2),
            vis_end=None,
        ),
    ]
    engine, table = _engine_with(rows)
    overlap_rows, _ = _overlap_rows(engine, table, [True, False])

    assert len(overlap_rows) == 1
    pair = overlap_rows[0]
    assert pair["intersection_start_0"] == D(2026, 1, 4)
    assert pair["intersection_end_0"] is None
    assert pair["intersection_start_1"] == D(2026, 1, 2)
    assert pair["intersection_end_1"] is None


def test_endpoint_touch_respects_per_dimension_inclusion():
    # The visibility intervals only touch at Jan 5: B starts exactly when A ends.
    rows = [
        dict(
            account_id=4,
            contract_no="A",
            biz_start=D(2026, 1, 1),
            biz_end=D(2026, 1, 10),
            vis_start=D(2026, 1, 1),
            vis_end=D(2026, 1, 5),
        ),
        dict(
            account_id=4,
            contract_no="B",
            biz_start=D(2026, 1, 2),
            biz_end=D(2026, 1, 9),
            vis_start=D(2026, 1, 5),
            vis_end=D(2026, 1, 9),
        ),
    ]
    exclusive_engine, exclusive_table = _engine_with(
        [dict(row) for row in rows], table_name="exclusive"
    )
    inclusive_engine, inclusive_table = _engine_with(
        [dict(row) for row in rows], table_name="inclusive"
    )

    # Half-open visibility end: the touching point is not shared, so no overlap.
    assert (
        _requirement_outcome(exclusive_engine, exclusive_table, [True, False]) is True
    )
    exclusive_rows, _ = _overlap_rows(exclusive_engine, exclusive_table, [True, False])
    assert exclusive_rows == []

    # Closed visibility end: Jan 5 belongs to both intervals, so they overlap.
    assert (
        _requirement_outcome(inclusive_engine, inclusive_table, [True, True]) is False
    )
    inclusive_rows, _ = _overlap_rows(inclusive_engine, inclusive_table, [True, True])
    assert len(inclusive_rows) == 1
    assert inclusive_rows[0]["intersection_start_1"] == D(2026, 1, 5)
    assert inclusive_rows[0]["intersection_end_1"] == D(2026, 1, 5)


def test_uniform_boolean_end_inclusion_is_accepted():
    engine, table = _engine_with(_prompt_rows())
    requirement = WithinRequirement.from_expression(table, table.name)
    requirement.add_date_no_overlap_2d_heterogeneous_constraint(
        start_column1="biz_start",
        end_column1="biz_end",
        start_column2="vis_start",
        end_column2="vis_end",
        key_columns=["account_id"],
        end_included=True,
        identifier_columns=["contract_no"],
    )
    # With an inclusive visibility end, [Jan 3, Jan 6] covers A's start Jan 5.
    assert requirement[0].test(engine).outcome is False


def test_legacy_single_and_uniform_two_dimension_constraints_keep_verdict():
    rows = [
        dict(
            account_id=9,
            contract_no="P",
            biz_start=D(2026, 1, 1),
            biz_end=D(2026, 1, 10),
            vis_start=D(2026, 1, 1),
            vis_end=D(2026, 1, 10),
        ),
        dict(
            account_id=9,
            contract_no="Q",
            biz_start=D(2026, 1, 5),
            biz_end=D(2026, 1, 15),
            vis_start=D(2026, 1, 5),
            vis_end=D(2026, 1, 15),
        ),
    ]
    engine, table = _engine_with(rows, table_name="legacy")

    one_dimension = WithinRequirement.from_expression(table, table.name)
    one_dimension.add_date_no_overlap_constraint(
        start_column="biz_start",
        end_column="biz_end",
        key_columns=["account_id"],
        end_included=True,
    )
    assert one_dimension[0].test(engine).outcome is False

    two_dimension = WithinRequirement.from_expression(table, table.name)
    two_dimension.add_date_no_overlap_2d_constraint(
        start_column1="biz_start",
        end_column1="biz_end",
        start_column2="vis_start",
        end_column2="vis_end",
        key_columns=["account_id"],
        end_included=True,
    )
    assert two_dimension[0].test(engine).outcome is False

    new_two_dimension = WithinRequirement.from_expression(table, table.name)
    new_two_dimension.add_date_no_overlap_2d_heterogeneous_constraint(
        start_column1="biz_start",
        end_column1="biz_end",
        start_column2="vis_start",
        end_column2="vis_end",
        key_columns=["account_id"],
        end_included=True,
        identifier_columns=["contract_no"],
    )
    assert new_two_dimension[0].test(engine).outcome is False


def _python_reference_overlaps(rows, end_included):
    dimensions = [("biz_start", "biz_end"), ("vis_start", "vis_end")]
    found = []
    for (_, row_a), (_, row_b) in itertools.combinations(list(enumerate(rows)), 2):
        if row_a["account_id"] != row_b["account_id"]:
            continue
        intersections = []
        overlaps = True
        for dimension, (start_column, end_column) in enumerate(dimensions):
            start_a, end_a = row_a[start_column], row_a[end_column]
            start_b, end_b = row_b[start_column], row_b[end_column]

            def reaches(end, other_start):
                if end is None:
                    return True
                return (
                    end >= other_start if end_included[dimension] else end > other_start
                )

            if not (reaches(end_a, start_b) and reaches(end_b, start_a)):
                overlaps = False
                break
            intersection_start = start_a if start_a >= start_b else start_b
            if end_a is None:
                intersection_end = end_b
            elif end_b is None:
                intersection_end = end_a
            else:
                intersection_end = end_a if end_a <= end_b else end_b
            intersections.append((intersection_start, intersection_end))
        if overlaps:
            identifier_a, identifier_b = sorted(
                [row_a["contract_no"], row_b["contract_no"]]
            )
            found.append(
                (
                    identifier_a,
                    identifier_b,
                    intersections[0][0],
                    intersections[0][1],
                    intersections[1][0],
                    intersections[1][1],
                )
            )
    return sorted(found)


@pytest.mark.parametrize(
    "end_included", [[True, True], [False, False], [True, False], [False, True]]
)
def test_sql_matches_independent_reference_under_nulls_and_reversal(end_included):
    random_generator = random.Random(20261120)
    date_pool = [None] + [D(2026, 1, day) for day in range(1, 12)]
    finite_pool = [D(2026, 1, day) for day in range(1, 12)]

    for _ in range(100):
        rows = []
        for index in range(random_generator.randint(1, 6)):
            rows.append(
                dict(
                    account_id=random_generator.choice([1, 1, 2]),
                    contract_no=f"c{index}",
                    biz_start=random_generator.choice(finite_pool),
                    biz_end=random_generator.choice(date_pool),
                    vis_start=random_generator.choice(finite_pool),
                    vis_end=random_generator.choice(date_pool),
                )
            )

        engine, table = _engine_with(rows, table_name="randomized")
        sql_rows, _ = _overlap_rows(engine, table, end_included)
        expected = _python_reference_overlaps(rows, end_included)
        assert _normalized_pairs(sql_rows) == expected

        reversed_engine, reversed_table = _engine_with(
            list(reversed(rows)), table_name="randomized_reversed"
        )
        reversed_sql_rows, _ = _overlap_rows(
            reversed_engine, reversed_table, end_included
        )
        assert _normalized_pairs(reversed_sql_rows) == expected
