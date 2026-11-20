"""Regression tests for composable reconciliation filters."""

from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy import event

from dataset import Filter, QueryError


def reconciliation_filter(candidates: Any) -> Filter:
    """The prompt's condition, written with the public filter API."""
    return Filter.all(
        Filter.column("counterparty").present(),
        Filter.any(
            Filter.column("source").equals("supplement"),
            Filter.all(
                Filter.column("status").not_equals("rejected"),
                Filter.any(
                    Filter.column("status").is_null(),
                    Filter.column("status").in_(candidates),
                ),
            ),
        ),
    )


@pytest.fixture
def tables(db):
    new = db["new_records"]
    new.insert_many(
        [
            {"counterparty": "N1", "source": "manual", "status": None},
            {"counterparty": "N2", "source": "manual", "status": "rejected"},
            {"counterparty": "N3", "source": "manual", "status": "open"},
            {"counterparty": "N4", "source": "manual", "status": "closed"},
            {"counterparty": None, "source": "manual", "status": None},
        ]
    )
    old = db["old_records"]
    old.insert_many(
        [
            {"counterparty": "O1", "source": "manual"},
            {"counterparty": "O2", "source": "supplement"},
            {"counterparty": None, "source": "supplement"},
        ]
    )
    return new, old


def counterparts(rows: Any) -> list[str]:
    return sorted(row["counterparty"] for row in rows)


def test_empty_candidate_list(tables):
    new, old = tables
    condition = reconciliation_filter([])

    assert counterparts(new.find(_filter=condition)) == ["N1"]
    assert new.count(_filter=condition) == 1
    assert new.count(_filter=condition, _pending=True) == 0

    assert counterparts(old.find(_filter=condition)) == ["O2"]
    assert old.count(_filter=condition) == 1
    assert old.count(_filter=condition, _pending=True) == 1
    assert old.counts(_filter=condition) == {
        "matched": 1,
        "pending": 1,
        "excluded": 1,
        "total": 3,
        "match_count": 1,
        "pending_count": 1,
    }


def test_candidate_status_list(tables):
    new, old = tables
    condition = reconciliation_filter(["open"])

    assert counterparts(new.find(_filter=condition)) == ["N1", "N3"]
    assert new.count(_filter=condition) == 2
    assert new.count(_filter=condition, _pending=True) == 0

    assert counterparts(old.find(_filter=condition)) == ["O2"]
    assert old.count(_filter=condition) == 1
    assert old.count_pending(_filter=condition) == 1


def test_missing_counterparty_is_never_pending(tables):
    new, old = tables
    condition = reconciliation_filter(["open"])

    for table in (new, old):
        matched = {row["counterparty"] for row in table.find(_filter=condition)}
        assert None not in matched
    assert old.count_pending(_filter=condition) == 1


def test_legacy_syntax_keeps_null_as_not_rejected(db):
    table = db["legacy"]
    table.insert_many(
        [
            {"counterparty": "A", "source": "manual", "status": None},
            {"counterparty": "B", "source": "manual", "status": "rejected"},
            {"counterparty": "C", "source": "manual", "status": "open"},
        ]
    )
    condition = {
        "counterparty": {"not": None},
        "or": [
            {"source": "supplement"},
            {
                "and": [
                    {"status": {"!=": "rejected"}},
                    {"or": [{"status": None}, {"status": {"in": ["open"]}}]},
                ]
            },
        ],
    }
    assert sorted(r["counterparty"] for r in table.find(**condition)) == ["A", "C"]
    assert table.count(**condition) == 2


def test_pagination_preserves_both_totals(tables):
    new, old = tables
    condition = reconciliation_filter(["open"])

    first = new.find_page(page=1, page_size=1, _filter=condition)
    second = new.find_page(page=2, page_size=1, _filter=condition)
    assert [row["counterparty"] for row in first] == ["N1"]
    assert [row["counterparty"] for row in second] == ["N3"]
    assert first.matched_count == second.matched_count == 2
    assert first.pending_count == second.pending_count == 0

    pending = old.find_pending(_filter=condition, page=1, page_size=10)
    assert [row["counterparty"] for row in pending] == ["O1"]
    assert pending.matched_count == 1
    assert pending.pending_count == 1


def test_bound_filter_reuses_one_condition(tables):
    new, old = tables
    review = old.filter(reconciliation_filter(["open"]))

    assert counterparts(review) == ["O2"]
    assert review.count() == 1
    assert review.count_pending() == 1
    assert review.counts()["excluded"] == 1


def test_filtering_and_counting_stay_in_sql(db, monkeypatch):
    table = db["large"]
    table.insert_many(
        [
            {"counterparty": str(i), "source": "manual", "status": None}
            for i in range(200)
        ]
    )

    statements: list[str] = []

    def capture(conn: Any, cursor: Any, statement: str, *args: Any) -> None:
        statements.append(statement)

    event.listen(db.engine, "before_cursor_execute", capture)
    try:
        result = table.find(_filter=reconciliation_filter([]), _page_size=10)
        list(result)
        assert result.matched_count == 200
        assert result.pending_count == 0
    finally:
        event.remove(db.engine, "before_cursor_execute", capture)

    assert any("count(" in statement.lower() for statement in statements)
    selects = [s for s in statements if s.lstrip().lower().startswith("select")]
    assert all("limit" in s.lower() or "count(" in s.lower() for s in selects)


def test_filter_invalid_page(tables):
    new, _ = tables
    with pytest.raises(QueryError):
        new.find(_page_size=0)
    with pytest.raises(QueryError):
        new.find_page(page_size=0)


def test_filter_counts_mapping(db):
    del db
    from dataset.filters import FilterCounts

    counts = FilterCounts(3, 2)
    assert counts.matched == 3
    assert counts.pending == 2
    assert counts.total == 5
    assert counts["matched"] == 3
    assert counts["pending"] == 2
    assert counts.as_dict() == {"matched": 3, "pending": 2, "total": 5}
