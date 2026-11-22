"""Edge cases for the provenance catalogue and sample evaluator."""

import pytest

from sql_metadata import Parser
from sql_metadata.provenance import QueryProvenance


def _sources(provenance, name):
    return [
        (s.branch, s.table, s.column, s.kind) for s in provenance.column(name).sources
    ]


def test_intersect_all_keeps_multiset_candidates():
    query = "SELECT id FROM a INTERSECT ALL SELECT id FROM b"
    report = Parser(query).provenance.residual({"a": [(1,), (1,), (2,)], "b": [(1,)]})
    assert [row.value for row in report.rows] == [(1,)]
    group = report.rows[0]
    assert group.remaining == 1
    assert group.definite is False
    assert [candidate.row for candidate in group.candidates] == [0, 1]


def test_intersect_distinct_is_reported_as_one_row():
    query = "SELECT id FROM a INTERSECT SELECT id FROM b"
    report = Parser(query).provenance.residual({"a": [(1,), (2,)], "b": [(1,)]})
    assert [row.value for row in report.rows] == [(1,)]
    assert report.rows[0].remaining == 1


def test_except_distinct_drops_all_matches():
    query = "SELECT id FROM a EXCEPT SELECT id FROM b"
    report = Parser(query).provenance.residual({"a": [(1,), (1,), (2,)], "b": [(1,)]})
    assert [row.value for row in report.rows] == [(2,)]


def test_union_all_counts_duplicate_origins():
    query = "SELECT id FROM a UNION ALL SELECT id FROM b"
    report = Parser(query).provenance.residual({"a": [(1,)], "b": [(1,)]})
    assert report.rows[0].remaining == 2
    assert report.rows[0].definite is True
    assert {c.table for c in report.rows[0].candidates} == {"a", "b"}


def test_union_distinct_marks_competing_origins():
    query = "SELECT id FROM a UNION SELECT id FROM b"
    report = Parser(query).provenance.residual({"a": [(1,)], "b": [(1,)]})
    group = report.rows[0]
    assert group.remaining == 1
    assert group.definite is False


def test_expression_projection_records_source_columns():
    query = "SELECT id + 1 AS x FROM a UNION ALL BY NAME SELECT id AS x FROM b"
    provenance = Parser(query).provenance
    assert _sources(provenance, "x") == [
        (0, "a", "id", "expression"),
        (1, "b", "id", "column"),
    ]


def test_expression_without_columns_is_an_expression_source():
    query = "SELECT 1 + 2 AS x FROM a UNION ALL BY NAME SELECT 3 AS x FROM b"
    provenance = Parser(query).provenance
    assert _sources(provenance, "x") == [
        (0, "a", None, "expression"),
        (1, "b", None, "literal"),
    ]


def test_star_projection_is_reported_as_star():
    provenance = Parser("SELECT * FROM a").provenance
    assert _sources(provenance, "*") == [(0, "a", "*", "star")]


def test_derived_table_forwards_inner_provenance():
    query = "SELECT x FROM (SELECT id AS x FROM a UNION ALL SELECT code AS x FROM b) v"
    provenance = Parser(query).provenance
    assert _sources(provenance, "x") == [
        (0, "a", "id", "column"),
        (1, "b", "code", "column"),
    ]


def test_unknown_derived_column_is_flagged():
    provenance = Parser("SELECT zzz FROM (SELECT id FROM a) s").provenance
    assert provenance.issues[0].kind == "unknown_derived_column"


def test_join_sources_are_both_reported():
    query = "SELECT a.id, b.id AS bid FROM a JOIN b ON a.id = b.id"
    provenance = Parser(query).provenance
    assert _sources(provenance, "id") == [(0, "a", "id", "column")]
    assert _sources(provenance, "bid") == [(1, "b", "id", "column")]


def test_duplicate_left_name_is_flagged():
    query = "SELECT id, id FROM a UNION ALL BY NAME SELECT id FROM b"
    provenance = Parser(query).provenance
    assert [i.kind for i in provenance.issues] == ["duplicate_output_name"]


def test_parenthesised_branches_are_unwrapped():
    query = "(SELECT id FROM a) UNION ALL (SELECT id FROM b)"
    provenance = Parser(query).provenance
    assert [branch.table for branch in provenance.branches] == ["a", "b"]


def test_root_parenthesised_select_is_unwrapped():
    provenance = Parser("(SELECT id FROM a)").provenance
    assert [column.name for column in provenance.columns] == ["id"]


def test_column_without_from_resolves_to_itself():
    provenance = Parser("SELECT nope").provenance
    assert _sources(provenance, "nope") == [(0, None, "nope", "column")]


def test_name_keyed_samples_are_supported():
    query = """
    SELECT id, label FROM a
    UNION ALL BY NAME
    SELECT code AS id, description AS label FROM b
    """
    report = Parser(query).provenance.residual(
        {
            "a": [{"id": 1, "label": "x"}],
            "b": [{"code": 2, "description": "y"}],
        }
    )
    assert sorted(row.value for row in report.rows) == [(1, "x"), (2, "y")]


def test_mapping_sample_missing_column_is_unevaluable():
    query = "SELECT id, label FROM a"
    report = Parser(query).provenance.residual({"a": [{"id": 1}]})
    assert report.evaluable is False


def test_tuple_sample_too_short_is_unevaluable():
    report = Parser("SELECT id, label FROM a").provenance.residual({"a": [(1,)]})
    assert report.evaluable is False
    assert "too short" in report.note


def test_bare_string_sample_is_rejected():
    report = Parser("SELECT id FROM a").provenance.residual({"a": ["row"]})
    assert report.evaluable is False
    assert "bare strings" in report.note


def test_unknown_sample_table_is_reported():
    report = Parser("SELECT id FROM a UNION ALL SELECT id FROM b").provenance.residual(
        {"a": [(1,)]}
    )
    assert report.evaluable is False
    assert report.missing_tables == ("b",)


def test_positional_arity_mismatch_is_unevaluable():
    query = "SELECT id FROM a UNION ALL SELECT id, label FROM b"
    report = Parser(query).provenance.residual({"a": [(1,)], "b": [(1, "x")]})
    assert report.evaluable is False
    assert "mismatched arity" in report.note


def test_plain_select_without_from_evaluates_literals():
    report = Parser("SELECT 1 AS k, NULL AS n").provenance.residual({})
    assert report.rows[0].value == (1, None)
    assert report.rows[0].candidates[0].table is None


def test_incomplete_statement_is_flagged_and_residual_unavailable():
    provenance = Parser("SELECT id FROM a UNION ALL SELECT").provenance
    assert provenance.parseable is True
    report = provenance.residual({"a": [(1,)]})
    assert report.evaluable is False


def test_non_select_is_flagged():
    provenance = Parser("DELETE FROM t WHERE id = 1").provenance
    assert provenance.issues[0].kind == "unsupported_node"


def test_boolean_literal_type_and_value():
    query = "SELECT true AS x FROM a UNION ALL BY NAME SELECT false AS x FROM b"
    provenance = Parser(query).provenance
    assert [s.value_type for s in provenance.column("x").sources] == [
        "BOOLEAN",
        "BOOLEAN",
    ]
    report = provenance.residual({"a": [()], "b": [()]})
    assert {row.value for row in report.rows} == {(True,), (False,)}


def test_cast_type_is_reported():
    query = (
        "SELECT CAST(x AS INT) AS k FROM a "
        "UNION ALL BY NAME SELECT DATE '2020-01-01' AS k FROM b"
    )
    provenance = Parser(query).provenance
    assert provenance.issues[0].kind == "type_conflict"


def test_unparsed_factory_and_parseable_properties():
    provenance = QueryProvenance.unparsed("bad")
    assert provenance.parseable is False
    assert provenance.parse_error == "bad"
    assert provenance.branches == ()
    assert provenance.set_operations == ()
    assert provenance.issues == ()
    assert provenance.to_dict()["parseable"] is False


def test_column_lookup_by_position():
    provenance = Parser("SELECT id, label FROM a").provenance
    assert provenance.column(1).name == "label"


def test_from_branches_filters_by_kind():
    query = (
        "SELECT id, NULL AS cost FROM a "
        "UNION ALL BY NAME SELECT id, price AS cost FROM b"
    )
    provenance = Parser(query).provenance
    assert provenance.column("cost").from_branches("explicit_null") == (0,)
    assert provenance.column("cost").from_branches("column") == (1,)


def test_evaluator_accepts_parenthesised_root():
    report = Parser("(SELECT id FROM a)").provenance.residual({"a": [(7,)]})
    assert report.rows[0].value == (7,)


def test_evaluator_supports_float_and_string_literals():
    report = Parser("SELECT 1.5 AS n, 'x' AS s").provenance.residual({})
    assert report.rows[0].value == (1.5, "x")


def test_evaluator_rejects_expression_literal():
    report = Parser("SELECT 1 + 2 AS x").provenance.residual({})
    assert report.evaluable is False
    assert "does not support" in report.note


def test_evaluator_handles_unaliased_subquery_selection():
    report = Parser("SELECT id FROM (SELECT id FROM a)").provenance.residual(
        {"a": [(1,)]}
    )
    assert report.evaluable is True
    assert [row.value for row in report.rows] == [(1,)]


def test_evaluator_rejects_non_column_forwarding():
    report = Parser("SELECT UPPER(id) FROM (SELECT id FROM a)").provenance.residual(
        {"a": [(1,)]}
    )
    assert report.evaluable is False
    assert "plain columns" in report.note


def test_except_by_name_keeps_only_left_schema():
    query = "SELECT a FROM t EXCEPT ALL BY NAME SELECT a, b FROM u"
    provenance = Parser(query).provenance
    assert [column.name for column in provenance.columns] == ["a"]
    assert _sources(provenance, "a") == [(0, "t", "a", "column")]


def test_except_by_name_ignores_right_only_names_in_evaluation():
    query = "SELECT a FROM t EXCEPT ALL BY NAME SELECT c AS a FROM u"
    report = Parser(query).provenance.residual({"t": [(1,), (2,)], "u": [(2,)]})
    assert [row.value for row in report.rows] == [(1,)]


def test_row_origin_note_for_empty_group():
    from sql_metadata.provenance import _residual_note

    assert _residual_note(0, 0, False) == "no row survives"


def test_evaluator_rejects_unsupported_expression():
    report = Parser("SELECT id + 1 AS total FROM a").provenance.residual({"a": [(1,)]})
    assert report.evaluable is False
    assert "columns and literals" in report.note


def test_evaluator_rejects_filtered_select():
    report = Parser("SELECT id FROM a WHERE id > 1").provenance.residual(
        {"a": [(1,), (2,)]}
    )
    assert report.evaluable is False
    assert "plain SELECT blocks" in report.note


def test_evaluator_rejects_joined_select():
    report = Parser("SELECT id FROM a JOIN b ON a.id = b.id").provenance.residual(
        {"a": [(1,)], "b": [(1,)]}
    )
    assert report.evaluable is False
    assert "plain SELECT blocks" in report.note


def test_evaluator_unknown_derived_column_is_unevaluable():
    query = "SELECT zzz FROM (SELECT id FROM a) s"
    report = Parser(query).provenance.residual({"a": [(1,)]})
    assert report.evaluable is False
    assert "does not produce" in report.note


def test_evaluator_unaliased_derived_subquery():
    query = "SELECT id FROM (SELECT id FROM a)"
    report = Parser(query).provenance.residual({"a": [(1,)]})
    assert [row.value for row in report.rows] == [(1,)]


def test_evaluator_rejects_unsupported_root():
    report = Parser("DELETE FROM a WHERE id = 1").provenance.residual({"a": [(1,)]})
    assert report.evaluable is False
    assert "does not support" in report.note


def test_evaluator_rejects_unsupported_source():
    query = "SELECT id FROM UNNEST(x) AS t"
    report = Parser(query).provenance.residual({"x": [(1,)]})
    assert report.evaluable is False


def test_to_dict_reports_branches_and_issues():
    catalogue = Parser(
        "SELECT id FROM a UNION ALL SELECT id, x FROM b"
    ).provenance.to_dict()
    assert catalogue["branches"][0]["table"] == "a"
    assert catalogue["issues"][0]["kind"] == "arity_mismatch"


def test_residual_to_dict_reports_null_filled():
    query = "SELECT id, cost FROM a UNION ALL BY NAME SELECT id FROM b"
    report = Parser(query).provenance.residual({"a": [(1, 2)], "b": [(3,)]})
    filled = [
        candidate
        for row in report.rows
        for candidate in row.candidates
        if candidate.null_filled
    ]
    assert filled and filled[0].null_filled == (1,)


def test_unresolved_expression_column_is_flagged():
    query = "SELECT missing + 1 AS total FROM a"
    provenance = Parser(query).provenance
    assert _sources(provenance, "total") == [(0, "a", "missing", "expression")]


def test_unknown_qualifier_is_flagged():
    provenance = Parser("SELECT nope.col FROM a").provenance
    assert provenance.issues[0].kind == "unresolved_column"


@pytest.mark.parametrize(
    "query",
    [
        "SELECT id FROM a UNION ALL SELECT id FROM b UNION ALL SELECT id FROM c",
        "SELECT id FROM a EXCEPT ALL SELECT id FROM b EXCEPT ALL SELECT id FROM c",
    ],
)
def test_chained_set_operations_are_ordered_innermost_first(query):
    provenance = Parser(query).provenance
    assert len(provenance.set_operations) == 2
    assert [branch.index for branch in provenance.branches] == [0, 1, 2]
