"""Provenance catalog across nested UNION/EXCEPT set operations."""

import pytest

from sql_metadata import Parser
from sql_metadata.provenance import QueryProvenance

QUERY = """
SELECT id, label, cost FROM (
    SELECT id, label, NULL AS cost FROM a
    UNION ALL
    SELECT code AS id, description AS label, price AS cost FROM b
) u
UNION ALL BY NAME
SELECT label, id FROM c
EXCEPT ALL
SELECT id, label, cost FROM cancel
"""

SWAPPED = """
SELECT id, label, cost FROM (
    SELECT id, label, NULL AS cost FROM a
    UNION ALL
    SELECT code AS id, price AS cost, description AS label FROM b
) u
UNION ALL BY NAME
SELECT label, id FROM c
EXCEPT ALL
SELECT id, label, cost FROM cancel
"""

SAMPLE = {
    "a": [(9, "X", None)],
    "b": [(9, "X", 8)],
    "c": [("X", 9)],
    "cancel": [(9, "X", None)],
}


def _sources(column):
    return [(s.branch, s.table, s.column, s.kind) for s in column.sources]


def test_set_operation_binding_is_reported():
    provenance = Parser(QUERY).provenance
    assert provenance.parseable is True
    operations = provenance.set_operations
    assert [(op.kind, op.all, op.binding) for op in operations] == [
        ("union", True, "position"),
        ("union", True, "name"),
        ("except", True, "position"),
    ]
    inner = operations[0]
    assert inner.left_names == ("id", "label", "cost")
    assert inner.right_names == ("id", "label", "cost")
    assert operations[1].right_names == ("label", "id")


def test_leaf_branches_are_reported_in_order():
    provenance = Parser(QUERY).provenance
    assert [(branch.index, branch.table) for branch in provenance.branches] == [
        (0, "a"),
        (1, "b"),
        (2, "c"),
        (3, "cancel"),
    ]


def test_columns_report_positional_binding_and_explicit_null():
    provenance = Parser(QUERY).provenance
    assert [column.name for column in provenance.columns] == [
        "id",
        "label",
        "cost",
    ]
    assert provenance.column("cost").position == 2
    assert _sources(provenance.column("cost")) == [
        (0, "a", None, "explicit_null"),
        (1, "b", "price", "column"),
        (2, "c", None, "null_extension"),
    ]
    assert provenance.column("cost").kinds == (
        "explicit_null",
        "column",
        "null_extension",
    )
    assert provenance.column("label").from_branches() == (0, 1, 2)


def test_positional_binding_uses_inner_output_names():
    provenance = Parser(QUERY).provenance
    # b calls its columns code/description/price, but the inner UNION ALL
    # binds by position to a's id/label/cost names.
    assert _sources(provenance.column("label")) == [
        (0, "a", "label", "column"),
        (1, "b", "description", "column"),
        (2, "c", "label", "column"),
    ]
    assert _sources(provenance.column("id")) == [
        (0, "a", "id", "column"),
        (1, "b", "code", "column"),
        (2, "c", "id", "column"),
    ]


def test_residual_reports_priced_row_from_b():
    report = Parser(QUERY).provenance.residual(SAMPLE)
    assert report.evaluable is True
    priced = [row for row in report.rows if row.value == (9, "X", 8)]
    assert len(priced) == 1
    assert priced[0].remaining == 1
    assert priced[0].definite is True
    assert [(c.branch, c.table) for c in priced[0].candidates] == [(1, "b")]
    assert priced[0].candidates[0].source_kinds == (
        "column",
        "column",
        "column",
    )


def test_residual_keeps_both_null_candidates_undecided():
    report = Parser(QUERY).provenance.residual(SAMPLE)
    empty = [row for row in report.rows if row.value == (9, "X", None)]
    assert len(empty) == 1
    group = empty[0]
    assert group.remaining == 1
    assert group.definite is False
    assert {c.table for c in group.candidates} == {"a", "c"}
    kinds = {c.table: c.source_kinds for c in group.candidates}
    assert kinds["a"][2] == "explicit_null"
    assert kinds["c"][2] == "null_extension"
    assert group.candidates[0].null_filled == ()
    assert group.candidates[1].null_filled == (2,)
    assert "cannot choose which one" in group.note


def test_residual_does_not_claim_a_row_from_both_candidates():
    report = Parser(QUERY).provenance.residual(SAMPLE)
    empty = [row for row in report.rows if row.value == (9, "X", None)][0]
    # remaining is 1, so the two candidates are alternatives, never a pair.
    assert empty.remaining == 1
    assert len(empty.candidates) == 2


def test_two_equal_nulls_cancel_both_candidates():
    sample = dict(SAMPLE, cancel=[(9, "X", None), (9, "X", None)])
    report = Parser(QUERY).provenance.residual(sample)
    assert [row.value for row in report.rows] == [(9, "X", 8)]
    assert all(row.value != (9, "X", None) for row in report.rows)


def test_swapped_projection_moves_inner_sources_only():
    provenance = Parser(SWAPPED).provenance
    assert _sources(provenance.column("label")) == [
        (0, "a", "label", "column"),
        (1, "b", "price", "column"),
        (2, "c", "label", "column"),
    ]
    assert _sources(provenance.column("cost")) == [
        (0, "a", None, "explicit_null"),
        (1, "b", "description", "column"),
        (2, "c", None, "null_extension"),
    ]
    # c's by-name fill is unchanged: label stays label, id stays id.
    assert _sources(provenance.column("id"))[2] == (2, "c", "id", "column")


def test_swapped_projection_changes_sample_values():
    sample = {
        "a": [{"id": 9, "label": "X", "cost": None}],
        "b": [{"code": 9, "description": "X", "price": 8}],
        "c": [{"label": "X", "id": 9}],
        "cancel": [],
    }
    report = Parser(SWAPPED).provenance.residual(sample)
    assert [row.value for row in report.rows] == [(9, "X", None), (9, 8, "X")]
    priced = [row for row in report.rows if row.value == (9, 8, "X")][0]
    assert priced.candidates[0].table == "b"


def test_arity_mismatch_reports_branch_and_position():
    query = """
    SELECT id, label FROM a
    UNION ALL
    SELECT code AS id, description AS label, price AS cost FROM b
    """
    provenance = Parser(query).provenance
    issues = [i for i in provenance.issues if i.kind == "arity_mismatch"]
    assert len(issues) == 1
    assert issues[0].branch == 1
    assert issues[0].position == 2
    assert [column.name for column in provenance.columns] == ["id", "label"]


def test_incompatible_types_report_branch_and_position():
    query = """
    SELECT 1 AS k, 'x' AS v FROM a
    UNION ALL BY NAME
    SELECT 'y' AS v, DATE '2020-01-01' AS k FROM b
    """
    provenance = Parser(query).provenance
    issues = [i for i in provenance.issues if i.kind == "type_conflict"]
    assert len(issues) == 1
    assert issues[0].branch == 1
    assert issues[0].position == 0
    assert "INTEGER" in issues[0].detail
    assert "DATE" in issues[0].detail


def test_no_sample_reports_structure_only():
    report = Parser(QUERY).provenance.residual()
    assert report.evaluable is False
    assert report.rows == ()
    assert "structural provenance only" in report.note


def test_partial_sample_does_not_claim_residual_source():
    report = Parser(QUERY).provenance.residual({"a": [(9, "X", None)]})
    assert report.evaluable is False
    assert report.rows == ()
    assert report.missing_tables == ("b",)


def test_unparseable_statement_is_marked():
    provenance = Parser("").provenance
    assert provenance.parseable is False
    assert provenance.parse_error == "Empty queries are not supported!"
    assert provenance.columns == ()
    report = provenance.residual({"a": [(1,)]})
    assert report.evaluable is False
    assert "could not be parsed" in report.note


def test_unsupported_statement_is_reported_as_issue():
    provenance = Parser("CREATE TABLE t (id INT)").provenance
    assert provenance.issues[0].kind == "unsupported_node"


def test_to_dict_is_json_friendly():
    catalogue = Parser(QUERY).provenance.to_dict()
    assert catalogue["columns"][2]["name"] == "cost"
    assert catalogue["set_operations"][1]["binding"] == "name"
    report = Parser(QUERY).provenance.residual(SAMPLE).to_dict()
    assert report["evaluable"] is True
    assert {"value", "remaining", "candidates"} <= set(report["rows"][0])


def test_standard_columns_aliases_still_work():
    parser = Parser(QUERY)
    assert parser.columns == ["code", "description", "price"]
    assert parser.columns_aliases == {
        "id": "code",
        "label": "description",
        "cost": "price",
    }
    assert parser.tables == ["a", "b", "c", "cancel"]


def test_provenance_is_cached():
    parser = Parser(QUERY)
    assert parser.provenance is parser.provenance


def test_column_lookup_rejects_unknown_names():
    provenance = Parser(QUERY).provenance
    with pytest.raises(KeyError):
        provenance.column("missing")
    with pytest.raises(KeyError):
        provenance.column(9)


def test_unparsed_factory_records_error():
    provenance = QueryProvenance.unparsed("boom")
    assert provenance.parseable is False
    assert provenance.parse_error == "boom"
    assert provenance.columns == ()
