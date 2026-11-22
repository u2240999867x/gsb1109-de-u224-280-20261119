"""Scope-aware column attribution reports.

These tests cover the reconciliation query from the task: an outer CTE and
a LATERAL subquery both named ``sales``, an unqualified ``code`` that may bind
to ``corrections`` or to the outer ``sales``, and a local relation alias that
shadows the outer correlated alias.
"""

import pytest

from sql_metadata import (
    AttributionScenario,
    Binding,
    ColumnAttributionReport,
    Parser,
    attribute_columns,
)
from sql_metadata.column_attribution import ColumnAttributionAnalyzer

BASE_QUERY = """
WITH sales AS (
    SELECT id, code, amount FROM archive_orders
)
SELECT v.chosen_code
FROM sales AS s, LATERAL (
    WITH sales AS (
        SELECT id, amount FROM latest_adjustments
    )
    SELECT t.amount, code AS chosen_code
    FROM sales AS t
    JOIN corrections AS u ON t.id = u.id
    WHERE code = s.code
) AS v
"""

LOCAL_SHADOW_QUERY = """
WITH sales AS (
    SELECT id, code, amount FROM archive_orders
)
SELECT v.chosen_code
FROM sales AS s, LATERAL (
    WITH sales AS (
        SELECT id, amount FROM latest_adjustments
    )
    SELECT t.amount, code AS chosen_code
    FROM sales AS t
    JOIN corrections AS u ON t.id = u.id
    JOIN (SELECT {local_projection} FROM local_corrections) AS s ON s.id = t.id
    WHERE code = s.code
) AS v
"""

PHYSICAL_TABLES = ["archive_orders", "latest_adjustments", "corrections"]


def _report(definitions, sql=BASE_QUERY):
    return Parser(sql).column_attribution(definitions)


def _code_bindings(scenario, scope):
    """Return (clause, binding) pairs for ``code`` in a scope."""
    result = []
    for reference in scenario.references:
        if reference.reference == "code" and reference.scope == scope:
            result.append((reference.clause, reference.binding))
    return result


def test_tables_are_still_reported():
    parser = Parser(BASE_QUERY)
    assert parser.tables == PHYSICAL_TABLES
    assert parser.columns_aliases == {"chosen_code": "code"}


def test_corrections_with_code_binds_both_references():
    report = _report({"corrections": ["id", "code"]})
    assert len(report.scenarios) == 1
    scenario = report.scenarios[0]

    bindings = _code_bindings(scenario, "v")
    assert bindings
    for _clause, binding in bindings:
        assert binding is not None
        assert binding.relation == "u"
        assert binding.source_table == "corrections"
        assert binding.source_column == "code"

    output = report.output("v.chosen_code")
    assert output.value_sources == ("corrections.code",)
    condition = output.conditions[0]
    assert condition.left.source_name == "corrections.code"


def test_corrections_without_code_falls_back_to_outer_sales():
    report = _report({"corrections": ["id"]})
    assert len(report.scenarios) == 1
    scenario = report.scenarios[0]

    bindings = _code_bindings(scenario, "v")
    assert bindings
    for _clause, binding in bindings:
        assert binding is not None
        assert binding.relation == "s"
        assert binding.scope == "outer"
        assert binding.fallback is True
        assert binding.source_name == "archive_orders.code"


def test_outer_s_code_reference_falls_back_from_inner_relation():
    """``s.code`` in the LATERAL body resolves to the correlated outer ``s``.

    The inner ``sales`` CTE is aliased ``t``, so ``s`` only exists in the
    outer scope; the reference is reported with ``fallback=True`` and no
    shadowing.
    """
    report = _report({"corrections": ["id", "code"]})
    scenario = report.scenarios[0]
    outer_refs = [
        reference
        for reference in scenario.references
        if reference.reference == "s.code"
    ]
    assert len(outer_refs) == 1
    binding = outer_refs[0].binding
    assert binding.scope == "outer"
    assert binding.source_name == "archive_orders.code"
    assert binding.fallback is True
    assert binding.shadowed == ()


def test_missing_corrections_definition_keeps_two_consistent_scenarios():
    report = _report(None)
    assert len(report.scenarios) == 2

    seen = set()
    for scenario in report.scenarios:
        bindings = _code_bindings(scenario, "v")
        assert len(bindings) == 2
        value_binding = next(b for clause, b in bindings if clause == "select")
        filter_binding = next(b for clause, b in bindings if clause == "where")
        # The same reference must use the same binding in both positions.
        assert value_binding == filter_binding
        seen.add(value_binding.source_name)

        output = scenario.outputs["v.chosen_code"]
        assert output.value_sources == (value_binding.source_name,)
        assert output.conditions[0].left.source_name == value_binding.source_name

    assert seen == {"corrections.code", "archive_orders.code"}


def test_no_mixed_third_scenario():
    report = _report(None)
    combinations = set()
    for scenario in report.scenarios:
        bindings = _code_bindings(scenario, "v")
        select_binding = next(b for clause, b in bindings if clause == "select")
        where_binding = next(b for clause, b in bindings if clause == "where")
        combinations.add(
            (select_binding.source_name, where_binding.source_name)
        )
    assert combinations == {
        ("corrections.code", "corrections.code"),
        ("archive_orders.code", "archive_orders.code"),
    }


def test_latest_adjustments_physical_code_does_not_supply_the_value():
    # latest_adjustments' own schema has a code column, but the inner CTE
    # projects only id and amount, so its output has no code.
    report = _report(
        {
            "corrections": ["id"],
            "latest_adjustments": ["id", "code", "amount"],
        }
    )
    assert report.output("v.chosen_code").value_sources == (
        "archive_orders.code",
    )
    assert report.output("t.amount").value_sources == (
        "latest_adjustments.amount",
    )


def test_both_sales_cte_bindings_are_preserved():
    report = _report({"corrections": ["id", "code"]})
    ctes = {(cte.name, cte.scope): cte for cte in report.ctes}
    assert ("sales", "outer") in ctes
    assert ("sales", "v") in ctes
    assert ctes[("sales", "outer")].columns == ("id", "code", "amount")
    assert ctes[("sales", "v")].columns == ("id", "amount")

    outer_source = report.reference("s.code")
    assert any(item.binding.scope == "outer" for item in outer_source)


def test_reference_lookup_returns_binding_per_scope():
    report = _report({"corrections": ["id", "code"]})
    chosen = report.reference("chosen_code")
    assert len(chosen) == 1
    assert chosen[0].binding.relation == "v"
    assert chosen[0].binding.column == "chosen_code"

    amounts = [
        item for item in report.reference("t.amount") if item.scope == "v"
    ]
    assert len(amounts) == 1
    assert amounts[0].binding.source_name == "latest_adjustments.amount"

    codes = [
        item
        for item in report.reference("code")
        if item.scope == "v"
    ]
    assert {item.binding.source_name for item in codes if item.clause != "where"} == {
        "corrections.code"
    }


def test_local_s_with_code_shadows_outer():
    query = LOCAL_SHADOW_QUERY.format(local_projection="id, code")
    report = Parser(query).column_attribution(
        {
            "corrections": ["id"],
            "latest_adjustments": ["id", "code", "amount"],
        },
        dialect="duckdb",
    )
    assert report.dialect == "duckdb"
    scenario = report.scenarios[0]

    for reference in scenario.references:
        if reference.reference in ("code", "s.code") and reference.scope == "v":
            assert reference.binding.relation == "s"
            assert reference.binding.source_table == "local_corrections"
            assert reference.binding.fallback is False

    assert report.output("v.chosen_code").value_sources == (
        "local_corrections.code",
    )


def test_local_s_without_code_falls_back_to_outer_sales():
    query = LOCAL_SHADOW_QUERY.format(local_projection="id")
    report = Parser(query).column_attribution(
        {
            "corrections": ["id"],
            "latest_adjustments": ["id", "code", "amount"],
        },
        dialect="duckdb",
    )
    scenario = report.scenarios[0]

    s_code = [
        reference
        for reference in scenario.references
        if reference.reference == "s.code" and reference.scope == "v"
    ]
    assert len(s_code) == 1
    assert s_code[0].binding.source_name == "archive_orders.code"
    assert s_code[0].binding.scope == "outer"
    assert "v:s" in s_code[0].binding.shadowed

    assert report.output("v.chosen_code").value_sources == (
        "archive_orders.code",
    )
    assert report.output("t.amount").value_sources == (
        "latest_adjustments.amount",
    )


def test_two_binding_points_are_identifiable():
    query = LOCAL_SHADOW_QUERY.format(local_projection="id")
    report = Parser(query).column_attribution(
        {"corrections": ["id"], "latest_adjustments": ["id", "code", "amount"]},
        dialect="duckdb",
    )
    points = {binding.binding_point for binding in report.binding_points}
    assert "outer:s.code" in points
    assert "s:local_corrections.id" in points
    assert report.output("t.amount").value_sources == (
        "latest_adjustments.amount",
    )


@pytest.mark.parametrize(
    "first_attr",
    [
        "tables_aliases",
        "columns",
        "tables",
        "with_names",
        "subqueries",
        "columns_aliases",
    ],
)
def test_attribution_is_independent_of_access_order(first_attr):
    parser = Parser(BASE_QUERY)
    getattr(parser, first_attr)
    report = parser.column_attribution({"corrections": ["id"]})

    control = _report({"corrections": ["id"]})
    assert report.scenario(report.scenarios[0].id).id == control.scenarios[0].id
    assert report.output("v.chosen_code").value_sources == (
        "archive_orders.code",
    )


def test_attribution_property_uses_default_report():
    parser = Parser(BASE_QUERY)
    assert isinstance(parser.attribution, ColumnAttributionReport)
    assert parser.attribution is parser.attribution


def test_report_serialises():
    report = _report({"corrections": ["id", "code"]})
    data = report.to_dict()
    assert data["dialect"] == "default"
    assert data["tables"] == PHYSICAL_TABLES
    assert data["outputs"]["v.chosen_code"]["value_sources"] == [
        "corrections.code"
    ]
    assert data["binding_points"]
    assert report.scenario(report.scenarios[0].id) is not None
    assert report["v.chosen_code"].name == "v.chosen_code"


def test_attribute_columns_helper():
    report = attribute_columns(
        BASE_QUERY, {"corrections": ["id", "code"]}
    )
    assert isinstance(report, ColumnAttributionReport)
    assert report.output("v.chosen_code").value_sources == ("corrections.code",)


def test_bindings_for_and_aliases_for_are_scope_aware():
    query = LOCAL_SHADOW_QUERY.format(local_projection="id")
    report = Parser(query).column_attribution(
        {"corrections": ["id"], "latest_adjustments": ["id", "code", "amount"]},
        dialect="duckdb",
    )
    s_aliases = report.aliases_for("s")
    assert {(alias.scope, alias.relation, alias.kind) for alias in s_aliases} == {
        ("outer", "sales", "cte"),
        ("v", "s", "subquery"),
    }
    s_code = report.bindings_for("s.code")
    assert [(binding.scope, binding.source_name) for binding in s_code] == [
        ("outer", "archive_orders.code")
    ]


def test_definitions_accept_mapping_and_string_forms():
    as_mapping = _report({"corrections": {"columns": ["id", "code"]}})
    as_string = _report({"corrections": "id, code"})
    assert as_mapping.output("v.chosen_code").value_sources == (
        "corrections.code",
    )
    assert as_string.output("v.chosen_code").value_sources == (
        "corrections.code",
    )


def test_analyser_builds_from_ast_directly():
    parser = Parser(BASE_QUERY)
    ast = parser._ast_parser.ast
    assert ast is not None
    report = ColumnAttributionAnalyzer(
        ast,
        definitions={"corrections": ["id", "code"]},
        dialect="duckdb",
        tables=parser.tables,
    ).build()
    assert isinstance(report, ColumnAttributionReport)
    assert report.output("v.chosen_code").value_sources == ("corrections.code",)


def test_attribution_models_are_frozen():
    report = _report({"corrections": ["id", "code"]})
    scenario = report.scenarios[0]
    assert isinstance(scenario, AttributionScenario)
    with pytest.raises(Exception):
        scenario.id = "changed"  # type: ignore[misc]
    binding = report.reference("t.amount")[0].binding
    assert isinstance(binding, Binding)
    with pytest.raises(Exception):
        binding.column = "other"  # type: ignore[misc]
