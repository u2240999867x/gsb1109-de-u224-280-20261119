"""Tests for offline PostgreSQL MERGE dry-run analysis.

The headline scenarios mirror the stock/incoming rollout review:

* an unlocked recount target is updated by the first MATCHED clause, which
  swallows the later DELETE clause — the row must not be counted as deleted;
* a second source row with the same key and ``quality IS NULL`` reaches the
  three-valued-logic branch and competes for the same physical target, so the
  whole statement is non-committable (an unopposed insert does not survive);
* archiving that second source row removes it via the USING filter, leaving
  the update and the insert committable;
* without declared source-key uniqueness the per-row result is never reported
  as guaranteed, even on a clean sample.
"""

import pytest
from sqlglot import exp

from sql_metadata import (
    ACTION_DELETE,
    ACTION_DO_NOTHING,
    ACTION_INSERT,
    ACTION_NO_BRANCH,
    ACTION_UPDATE,
    InvalidQueryDefinition,
    MergeConflictError,
    MergeDryRun,
    MergeEvaluationError,
    MergeSpec,
    Parser,
    analyze_merge,
)
from sql_metadata.merge_dry_run import _RowContext

STOCK_MERGE = """
MERGE INTO stock s
USING (
  SELECT wh, item, qty, version, quality, locked, archived
  FROM incoming
  WHERE archived = false
) i
ON s.wh = i.wh AND s.item = i.item
WHEN MATCHED AND i.quality = 'recount' THEN
  UPDATE SET qty = i.qty, version = s.version + 1
WHEN MATCHED AND i.quality = 'recount' AND i.locked = false THEN
  DELETE
WHEN MATCHED AND i.quality IS NULL AND i.locked IS NOT TRUE THEN
  UPDATE SET version = i.version
WHEN NOT MATCHED THEN
  INSERT (wh, item, qty) VALUES (i.wh, i.item, i.qty)
"""

TARGET_COLUMNS = ("wh", "item", "qty", "version", "locked")


def stock_target() -> list[dict[str, object]]:
    return [{"wh": 1, "item": 100, "qty": 5, "version": 3, "locked": False}]


def stock_sources() -> list[dict[str, object]]:
    return [
        {
            "wh": 1,
            "item": 100,
            "qty": 9,
            "version": 3,
            "quality": "recount",
            "locked": False,
            "archived": False,
        },
        {
            "wh": 1,
            "item": 100,
            "qty": 5,
            "version": 7,
            "quality": None,
            "locked": None,
            "archived": False,
        },
        {
            "wh": 2,
            "item": 200,
            "qty": 4,
            "version": 1,
            "quality": None,
            "locked": None,
            "archived": False,
        },
    ]


# ---------------------------------------------------------------------------
# Structural, no-sample analysis
# ---------------------------------------------------------------------------


def test_analyze_merge_reports_full_structure():
    structure = analyze_merge(STOCK_MERGE, target_columns=TARGET_COLUMNS)

    assert structure.target_table == "stock"
    assert structure.target_alias == "s"
    assert structure.source_table == "incoming"
    assert structure.source_alias == "i"
    assert structure.source_kind == "subquery"
    assert structure.source_columns == (
        "wh",
        "item",
        "qty",
        "version",
        "quality",
        "locked",
        "archived",
    )
    assert structure.source_filter_sql == "archived = FALSE"
    assert structure.on_pairs == (("wh", "wh"), ("item", "item"))
    assert structure.default_managed_columns == ("version", "locked")

    matched, delete, null_quality, insert = structure.branches
    assert (matched.index, matched.matched, matched.action) == (1, True, ACTION_UPDATE)
    assert matched.condition_sql == "i.quality = 'recount'"
    assert matched.assignments == {"qty": "i.qty", "version": "s.version + 1"}

    assert (delete.index, delete.action) == (2, ACTION_DELETE)
    assert delete.condition_sql == "i.quality = 'recount' AND i.locked = FALSE"

    assert null_quality.action == ACTION_UPDATE
    assert null_quality.assignments == {"version": "i.version"}

    assert insert.matched is False
    assert insert.action == ACTION_INSERT
    assert insert.condition_sql is None
    assert insert.insert_columns == ("wh", "item", "qty")
    assert insert.insert_values_sql == ("i.wh", "i.item", "i.qty")


def test_structure_without_target_columns_leaves_defaults_unknown():
    structure = analyze_merge(STOCK_MERGE)
    assert structure.default_managed_columns is None


def test_structure_for_plain_table_using_has_no_filter():
    sql = """
    MERGE INTO stock s USING incoming i
    ON s.wh = i.wh AND s.item = i.item
    WHEN NOT MATCHED THEN INSERT (wh) VALUES (i.wh)
    """
    structure = analyze_merge(sql, target_columns=("wh", "item"))
    assert structure.source_kind == "table"
    assert structure.source_table == "incoming"
    assert structure.source_columns == ()
    assert structure.source_filter_sql is None
    assert structure.default_managed_columns == ("item",)


def test_subquery_without_where_has_no_filter():
    sql = """
    MERGE INTO stock s USING (SELECT wh, item FROM incoming) i
    ON s.wh = i.wh
    WHEN NOT MATCHED THEN INSERT (wh) VALUES (i.wh)
    """
    assert analyze_merge(sql).source_filter_sql is None


def test_spec_builds_from_library_parser():
    parser = Parser(STOCK_MERGE)
    assert parser.query_type.value == "MERGE"
    spec = MergeSpec.from_parser(parser)
    assert spec.target_table == "stock"
    assert spec.on_pairs == (("wh", "wh"), ("item", "item"))


def test_from_sql_rejects_non_merge_and_syntax_errors():
    with pytest.raises(InvalidQueryDefinition, match="Expected a MERGE"):
        MergeSpec.from_sql("SELECT 1")
    with pytest.raises(InvalidQueryDefinition, match="SQL syntax error"):
        MergeSpec.from_sql("MERGE INTO stock USING")


def test_from_parser_rejects_non_merge():
    with pytest.raises(InvalidQueryDefinition, match="Expected a MERGE"):
        MergeSpec.from_parser(Parser("SELECT 1"))


# ---------------------------------------------------------------------------
# Headline dry-run: conflicting sources make the whole statement roll back
# ---------------------------------------------------------------------------


def test_conflicting_sources_roll_back_everything_including_unopposed_insert():
    spec = MergeSpec.from_sql(STOCK_MERGE)
    plan = MergeDryRun(
        spec, stock_target(), stock_sources(), defaults={"version": 1}
    ).run()

    assert plan.would_commit is False
    assert plan.guaranteed is False
    assert plan.branch_hit_count == {1: 1, 2: 0, 3: 1, 4: 1}

    recount, null_quality, insert = plan.outcomes
    # The first MATCHED clause swallows the DELETE: A is updated, never deleted.
    assert (recount.branch_index, recount.action) == (1, ACTION_UPDATE)
    assert recount.changes == {"qty": 9, "version": 4}
    assert (null_quality.branch_index, null_quality.action) == (3, ACTION_UPDATE)
    assert null_quality.changes == {"version": 7}
    assert (insert.branch_index, insert.action) == (4, ACTION_INSERT)
    assert insert.inserted_row == {"wh": 2, "item": 200, "qty": 4, "version": 1}
    assert insert.defaults_applied == {"version": 1}

    conflict = plan.conflicts[0]
    assert conflict.key == (1, 100)
    assert conflict.source_indexes == [0, 1]
    assert conflict.branch_indexes == [1, 3]
    assert conflict.actions == [ACTION_UPDATE, ACTION_UPDATE]

    # Atomic rollback: the target is untouched and the valid insert never lands.
    assert list(plan.target_after) == [(1, 100)]
    assert plan.target_after[(1, 100)] == stock_target()[0]
    assert (2, 200) not in plan.target_after
    assert any("not declared" in note for note in plan.guarantee_notes)


def test_raise_if_conflicting_explains_the_competition():
    spec = MergeSpec.from_sql(STOCK_MERGE)
    dry_run = MergeDryRun(spec, stock_target(), stock_sources())
    with pytest.raises(MergeConflictError, match="branches \\[1, 3\\]"):
        dry_run.raise_if_conflicting()


def test_raise_if_conflicting_returns_plan_when_clean():
    spec = MergeSpec.from_sql(STOCK_MERGE)
    sources = stock_sources()
    sources[1]["archived"] = True
    plan = MergeDryRun(
        spec, stock_target(), sources, defaults={"version": 1}
    ).raise_if_conflicting()
    assert plan.would_commit is True
    assert plan.conflicts == []


def test_archiving_the_second_source_moves_the_statement_to_committable():
    spec = MergeSpec.from_sql(STOCK_MERGE)
    sources = stock_sources()
    sources[1]["archived"] = True

    plan = MergeDryRun(spec, stock_target(), sources, defaults={"version": 1}).run()

    assert plan.would_commit is True
    assert plan.conflicts == []
    # The archived row keeps its input position but disappears from the stream.
    assert [outcome.original_index for outcome in plan.outcomes] == [0, 2]
    assert [outcome.source_index for outcome in plan.outcomes] == [0, 1]

    updated, inserted = plan.outcomes
    assert updated.action == ACTION_UPDATE
    assert updated.changes == {"qty": 9, "version": 4}
    assert inserted.action == ACTION_INSERT
    assert inserted.defaults_applied == {"version": 1}

    assert plan.target_after[(1, 100)]["qty"] == 9
    assert plan.target_after[(1, 100)]["version"] == 4
    assert plan.target_after[(2, 200)] == {"wh": 2, "item": 200, "qty": 4, "version": 1}


def test_clean_sample_is_guaranteed_only_with_declared_uniqueness():
    spec = MergeSpec.from_sql(STOCK_MERGE)
    sources = stock_sources()
    sources[1]["archived"] = True

    unknown = MergeDryRun(spec, stock_target(), sources).run()
    assert unknown.would_commit is True
    assert unknown.guaranteed is False

    guaranteed = MergeDryRun(
        spec, stock_target(), sources, source_unique=True, target_unique=True
    ).run()
    assert guaranteed.guaranteed is True
    assert guaranteed.guarantee_notes == []

    non_unique_source = MergeDryRun(
        spec, stock_target(), sources, source_unique=False
    ).run()
    assert non_unique_source.guaranteed is False
    assert any("non-unique" in note for note in non_unique_source.guarantee_notes)

    non_unique_target = MergeDryRun(
        spec, stock_target(), sources, target_unique=False
    ).run()
    assert non_unique_target.guaranteed is False
    assert any(
        "Target key uniqueness" in note for note in non_unique_target.guarantee_notes
    )


# ---------------------------------------------------------------------------
# Branch semantics: delete, do nothing, no branch, three-valued logic
# ---------------------------------------------------------------------------


def test_delete_branch_removes_the_target_row():
    sql = """
    MERGE INTO stock s USING incoming i
    ON s.wh = i.wh AND s.item = i.item
    WHEN MATCHED AND i.locked = false THEN DELETE
    WHEN NOT MATCHED THEN INSERT (wh, item) VALUES (i.wh, i.item)
    """
    spec = MergeSpec.from_sql(sql)
    plan = MergeDryRun(
        spec,
        stock_target(),
        [
            {"wh": 1, "item": 100, "locked": False},
            {"wh": 2, "item": 200, "locked": True},
        ],
    ).run()

    assert plan.would_commit is True
    assert plan.outcomes[0].action == ACTION_DELETE
    assert (1, 100) not in plan.target_after
    assert plan.outcomes[1].action == ACTION_INSERT
    assert (2, 200) in plan.target_after


def test_later_same_key_source_stays_matched_after_an_earlier_delete():
    sql = """
    MERGE INTO stock s USING incoming i
    ON s.wh = i.wh AND s.item = i.item
    WHEN MATCHED AND i.quality = 'remove' THEN DELETE
    WHEN MATCHED AND i.quality = 'recount' THEN
      UPDATE SET qty = i.qty, version = s.version + 1
    """
    plan = MergeDryRun(
        MergeSpec.from_sql(sql),
        stock_target(),
        [
            {"wh": 1, "item": 100, "qty": 9, "quality": "remove"},
            {"wh": 1, "item": 100, "qty": 9, "quality": "recount"},
        ],
    ).run()

    deleted, updated = plan.outcomes
    # Matching uses the statement-start target, so the second row is still
    # MATCHED even though the first deleted the working copy.
    assert updated.matched is True
    assert (deleted.branch_index, deleted.action) == (1, ACTION_DELETE)
    assert (updated.branch_index, updated.action) == (2, ACTION_UPDATE)
    # s.version resolves against the original row (3), not a missing target.
    assert updated.changes == {"qty": 9, "version": 4}

    # Two writes on one physical key still conflict and roll back atomically.
    assert plan.would_commit is False
    assert plan.conflicts[0].branch_indexes == [1, 2]
    assert plan.target_after[(1, 100)] == stock_target()[0]


def test_do_nothing_branch_writes_nothing_and_never_conflicts():
    sql = """
    MERGE INTO stock s USING incoming i
    ON s.wh = i.wh
    WHEN MATCHED AND i.quality = 'skip' THEN DO NOTHING
    WHEN MATCHED THEN UPDATE SET qty = i.qty
    WHEN NOT MATCHED THEN DO NOTHING
    """
    spec = MergeSpec.from_sql(sql)
    plan = MergeDryRun(
        spec,
        [{"wh": 1, "qty": 0}],
        [
            {"wh": 1, "qty": 5, "quality": "skip"},
            {"wh": 1, "qty": 6, "quality": "recount"},
            {"wh": 2, "qty": 7, "quality": None},
        ],
    ).run()

    assert plan.outcomes[0].action == ACTION_DO_NOTHING
    assert plan.outcomes[2].action == ACTION_DO_NOTHING
    # The DO NOTHING row does not compete with the real UPDATE for key (1,).
    assert plan.conflicts == []
    assert plan.would_commit is True
    assert plan.target_after[(1,)]["qty"] == 6


def test_matched_row_without_a_firing_clause_is_reported_not_silently_written():
    sql = """
    MERGE INTO stock s USING incoming i ON s.wh = i.wh
    WHEN MATCHED AND i.quality = 'recount' THEN UPDATE SET qty = i.qty
    """
    plan = MergeDryRun(
        MergeSpec.from_sql(sql),
        [{"wh": 1, "qty": 0}],
        [{"wh": 1, "qty": 5, "quality": "other"}],
    ).run()
    outcome = plan.outcomes[0]
    assert outcome.matched is True
    assert outcome.action == ACTION_NO_BRANCH
    assert outcome.branch_index is None
    assert outcome.note == "No WHEN clause matched this row"
    assert plan.branch_hit_count == {1: 0}
    assert plan.target_after[(1,)]["qty"] == 0


def test_three_valued_logic_null_locked_still_passes_is_not_true():
    spec = MergeSpec.from_sql(STOCK_MERGE)
    plan = MergeDryRun(
        spec,
        [{"wh": 1, "item": 100, "qty": 5, "version": 3, "locked": None}],
        [
            {
                "wh": 1,
                "item": 100,
                "qty": 9,
                "version": 7,
                "quality": None,
                "locked": None,
                "archived": False,
            }
        ],
    ).run()
    assert plan.outcomes[0].branch_index == 3
    assert plan.outcomes[0].changes == {"version": 7}


def test_null_comparison_fails_when_test_instead_of_erroring():
    sql = """
    MERGE INTO stock s USING incoming i ON s.wh = i.wh
    WHEN MATCHED AND i.quality = 'recount' THEN UPDATE SET qty = i.qty
    """
    plan = MergeDryRun(
        MergeSpec.from_sql(sql),
        [{"wh": 1, "qty": 0}],
        [{"wh": 1, "qty": 5, "quality": None}],
    ).run()
    assert plan.outcomes[0].action == ACTION_NO_BRANCH


def test_duplicate_target_sample_key_is_rejected():
    spec = MergeSpec.from_sql(STOCK_MERGE)
    duplicated = stock_target() + [
        {"wh": 1, "item": 100, "qty": 99, "version": 9, "locked": True}
    ]
    with pytest.raises(MergeEvaluationError, match="duplicate key"):
        MergeDryRun(spec, duplicated, stock_sources()).run()


# ---------------------------------------------------------------------------
# Scalar expression semantics
# ---------------------------------------------------------------------------


def _context(source, target=None):
    return _RowContext(source, target, {"i", "incoming"}, {"s", "stock"}, "postgres")


def test_literals_booleans_null_and_negation():
    ctx = _context({})
    assert ctx.eval(exp.Literal.number(7)) == 7
    assert ctx.eval(exp.Literal.number("1.5")) == 1.5
    assert ctx.eval(exp.Literal.string("recount")) == "recount"
    assert ctx.eval(exp.Boolean(this=True)) is True
    assert ctx.eval(exp.Null()) is None
    assert ctx.eval(exp.Neg(this=exp.Literal.number(2))) == -2
    assert ctx.eval(exp.Neg(this=exp.Null())) is None


def test_comparisons_implement_sql_null_propagation():
    ctx = _context({"a": 5, "b": None})
    assert (
        ctx.eval(exp.EQ(this=exp.column("a"), expression=exp.Literal.number(5))) is True
    )
    assert (
        ctx.eval(exp.NEQ(this=exp.column("a"), expression=exp.Literal.number(4)))
        is True
    )
    assert (
        ctx.eval(exp.GT(this=exp.column("a"), expression=exp.Literal.number(4))) is True
    )
    assert (
        ctx.eval(exp.GTE(this=exp.column("a"), expression=exp.Literal.number(5)))
        is True
    )
    assert (
        ctx.eval(exp.LT(this=exp.column("a"), expression=exp.Literal.number(6))) is True
    )
    assert (
        ctx.eval(exp.LTE(this=exp.column("a"), expression=exp.Literal.number(5)))
        is True
    )
    assert (
        ctx.eval(exp.EQ(this=exp.column("b"), expression=exp.Literal.number(1))) is None
    )


def test_arithmetic_operators_and_null_propagation():
    ctx = _context({"a": 6, "b": None})
    a, b = exp.column("a"), exp.column("b")
    assert ctx.eval(exp.Add(this=a, expression=exp.Literal.number(1))) == 7
    assert ctx.eval(exp.Sub(this=a, expression=exp.Literal.number(2))) == 4
    assert ctx.eval(exp.Mul(this=a, expression=exp.Literal.number(2))) == 12
    assert ctx.eval(exp.Div(this=a, expression=exp.Literal.number(3))) == 2
    assert ctx.eval(exp.Mod(this=a, expression=exp.Literal.number(4))) == 2
    assert ctx.eval(exp.Add(this=b, expression=exp.Literal.number(1))) is None
    assert ctx.eval(exp.Paren(this=exp.Add(this=a, expression=a))) == 12


def test_and_or_not_truth_tables():
    ctx = _context({})
    assert (
        ctx.eval(
            exp.And(this=exp.Boolean(this=True), expression=exp.Boolean(this=False))
        )
        is False
    )
    assert ctx.eval(exp.And(this=exp.Boolean(this=True), expression=exp.Null())) is None
    assert (
        ctx.eval(
            exp.Or(this=exp.Boolean(this=False), expression=exp.Boolean(this=True))
        )
        is True
    )
    assert ctx.eval(exp.Or(this=exp.Boolean(this=False), expression=exp.Null())) is None
    assert (
        ctx.eval(
            exp.Or(this=exp.Boolean(this=False), expression=exp.Boolean(this=False))
        )
        is False
    )
    assert ctx.eval(exp.Not(this=exp.Boolean(this=False))) is True
    assert ctx.eval(exp.Not(this=exp.Null())) is None


def test_is_tests_including_null_and_booleans():
    ctx = _context({"a": None, "b": True, "c": False})
    assert ctx.eval(exp.Is(this=exp.column("a"), expression=exp.Null())) is True
    assert (
        ctx.eval(exp.Is(this=exp.column("b"), expression=exp.Boolean(this=True)))
        is True
    )
    assert (
        ctx.eval(exp.Is(this=exp.column("c"), expression=exp.Boolean(this=False)))
        is True
    )
    not_true = exp.Not(
        this=exp.Is(this=exp.column("a"), expression=exp.Boolean(this=True))
    )
    assert ctx.eval(not_true) is True


def test_column_qualification_and_error_paths():
    ctx = _context({"qty": 1}, {"qty": 2})
    with pytest.raises(MergeEvaluationError, match="ambiguous"):
        ctx.eval(exp.column("qty"))
    with pytest.raises(MergeEvaluationError, match="not bound"):
        ctx.eval(exp.column("qty", table="other"))

    insert_context = _context({"qty": 1}, None)
    with pytest.raises(MergeEvaluationError, match="NOT MATCHED"):
        insert_context.eval(exp.column("qty", table="s"))

    with pytest.raises(MergeEvaluationError, match="Cannot evaluate"):
        ctx.eval(exp.Upper(this=exp.column("qty", table="i")))

    with pytest.raises(MergeEvaluationError, match="Cannot evaluate"):
        ctx.eval(
            exp.Is(
                this=exp.column("qty", table="i"),
                expression=exp.Literal.number(1),
            )
        )


def test_unqualified_columns_resolve_against_source_or_target():
    ctx = _context({"src_only": 1}, {"tgt_only": 2})
    assert ctx.eval(exp.column("src_only")) == 1
    assert ctx.eval(exp.column("tgt_only")) == 2


# ---------------------------------------------------------------------------
# Defensive AST validation
# ---------------------------------------------------------------------------


def test_validation_requires_table_target_and_using():
    with pytest.raises(InvalidQueryDefinition, match="target must be a table"):
        MergeSpec._read_target(exp.Null())
    with pytest.raises(
        InvalidQueryDefinition, match="USING must be a table or subquery"
    ):
        MergeSpec._read_using(exp.Null())


def test_validation_rejects_source_subquery_without_table():
    spec = MergeSpec.from_sql(STOCK_MERGE)
    spec.source_kind = "subquery"
    spec._source_node = exp.Subquery(this=exp.Select())
    with pytest.raises(InvalidQueryDefinition, match="no source table"):
        spec._source_table_name()


def test_validation_rejects_non_equality_on_clauses():
    spec = MergeSpec.from_sql(STOCK_MERGE)
    with pytest.raises(InvalidQueryDefinition, match="equality predicates"):
        spec._read_on(exp.GT(this=exp.column("a"), expression=exp.column("b")))
    with pytest.raises(InvalidQueryDefinition, match="target and source columns"):
        spec._read_on(
            exp.EQ(this=exp.Literal.number(1), expression=exp.Literal.number(2))
        )
    with pytest.raises(
        InvalidQueryDefinition, match="one target column with one source"
    ):
        spec._read_on(
            exp.EQ(
                this=exp.column("wh", table="x"),
                expression=exp.column("wh", table="y"),
            )
        )


def test_on_clause_accepts_parentheses_and_reversed_operands():
    parenthesized = """
    MERGE INTO stock s USING incoming i
    ON (s.wh = i.wh AND s.item = i.item)
    WHEN NOT MATCHED THEN INSERT (wh, item) VALUES (i.wh, i.item)
    """
    assert MergeSpec.from_sql(parenthesized).on_pairs == (
        ("wh", "wh"),
        ("item", "item"),
    )

    reversed_sides = """
    MERGE INTO stock s USING incoming i
    ON i.wh = s.wh AND i.item = s.item
    WHEN NOT MATCHED THEN INSERT (wh, item) VALUES (i.wh, i.item)
    """
    assert MergeSpec.from_sql(reversed_sides).on_pairs == (
        ("wh", "wh"),
        ("item", "item"),
    )


def test_validation_requires_at_least_one_when_clause():
    merge = exp.Merge(
        this=exp.Table(this=exp.Identifier(this="stock")),
        using=exp.Table(this=exp.Identifier(this="incoming")),
        on=exp.EQ(
            this=exp.column("wh", table="stock"),
            expression=exp.column("wh", table="incoming"),
        ),
        whens=exp.Whens(expressions=[]),
    )
    with pytest.raises(InvalidQueryDefinition, match="at least one WHEN"):
        MergeSpec(merge, "postgres")


def test_spec_accessors_round_trip_branch_nodes():
    spec = MergeSpec.from_sql(STOCK_MERGE)
    first, second = spec.branches[0], spec.branches[1]
    assert isinstance(spec.condition_node(first), exp.EQ)
    assert spec.condition_node(spec.branches[3]) is None
    assert isinstance(spec.then_node(first), exp.Update)
    assert isinstance(spec.then_node(second), exp.Var)
