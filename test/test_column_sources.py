from sql_metadata import Parser

BASE_QUERY = """
WITH RECURSIVE closure AS (
    SELECT roots.id AS id,
           roots.child_id AS next_id,
           roots.base_cost AS cost
    FROM roots
    UNION ALL
    SELECT closure.next_id AS id,
           edges.next_id AS next_id,
           closure.cost + edges.extra AS cost
    FROM closure
    JOIN edges ON closure.next_id = edges.id
    WHERE edges.live
)
SELECT id, cost FROM closure
"""

DECLARED_COLUMNS_QUERY = """
WITH RECURSIVE closure(id, next_id, cost) AS (
    SELECT roots.id,
           roots.child_id,
           roots.base_cost
    FROM roots
    UNION ALL
    SELECT closure.next_id,
           edges.next_id,
           closure.cost + edges.extra
    FROM closure
    JOIN edges ON closure.next_id = edges.id
    WHERE edges.live
)
SELECT id, cost FROM closure
"""


def swapped_query() -> str:
    return BASE_QUERY.replace(
        "SELECT closure.next_id AS id,\n"
        "           edges.next_id AS next_id,",
        "SELECT edges.next_id AS id,\n"
        "           closure.next_id AS next_id,",
    )


def test_recursive_cte_value_sources_by_position():
    parser = Parser(BASE_QUERY)

    assert parser.column_sources["id"] == [
        "roots.id",
        "roots.child_id",
        "edges.next_id",
    ]
    assert parser.column_sources["cost"] == [
        "roots.base_cost",
        "edges.extra",
    ]


def test_recursive_cte_conditions_are_separate_from_values():
    parser = Parser(BASE_QUERY)
    sources = parser.column_sources["id"]

    assert sources.conditions == ["edges.id", "edges.live"]
    assert "edges.id" not in sources
    assert "edges.live" not in sources
    assert "edges.id" not in parser.column_sources["cost"]
    assert "edges.live" not in parser.column_sources["cost"]


def test_swapping_recursive_projections_changes_positional_sources():
    parser = Parser(swapped_query())

    assert parser.column_sources["id"] == ["roots.id", "edges.next_id"]
    assert "roots.child_id" not in parser.column_sources["id"]
    assert parser.column_sources["cost"] == [
        "roots.base_cost",
        "edges.extra",
    ]


def test_recursive_reference_is_reported_with_its_iteration():
    parser = Parser(BASE_QUERY)
    sources = parser.column_sources["id"]

    assert sources.recursive == ["closure.next_id"]
    assert sources.recursive_hops == [
        {
            "reference": "closure.next_id",
            "column": "next_id",
            "iteration": "previous",
        }
    ]
    assert "previous" in sources.explain()


def test_self_dependent_column_stops_after_external_sources():
    parser = Parser(BASE_QUERY)
    cost = parser.column_sources["cost"]

    assert cost == ["roots.base_cost", "edges.extra"]
    assert cost.recursive == ["closure.cost"]
    assert cost.cycles == ["closure.cost"]


def test_unprojected_recursive_column_can_be_resolved_directly():
    parser = Parser(BASE_QUERY)

    next_id = parser.resolve_column_source("closure", "next_id")

    assert next_id == ["roots.child_id", "edges.next_id"]
    assert next_id.cycles == []


def test_explicit_cte_column_list_uses_the_same_positions():
    parser = Parser(DECLARED_COLUMNS_QUERY)

    assert parser.column_sources["id"] == [
        "roots.id",
        "roots.child_id",
        "edges.next_id",
    ]
    assert parser.column_sources["cost"] == [
        "roots.base_cost",
        "edges.extra",
    ]


def test_schema_qualified_same_name_relation_stays_physical():
    query = """
    WITH RECURSIVE closure AS (
        SELECT roots.id AS id,
               roots.child_id AS next_id,
               roots.base_cost AS cost
        FROM roots
        UNION ALL
        SELECT closure.next_id AS id,
               main.closure.next_id AS next_id,
               closure.cost + main.closure.extra AS cost
        FROM closure
        JOIN main.closure ON closure.next_id = main.closure.id
        WHERE main.closure.live
    )
    SELECT id, cost FROM closure
    """
    parser = Parser(query)

    assert parser.column_sources["id"] == [
        "roots.id",
        "roots.child_id",
        "main.closure.next_id",
    ]
    assert parser.column_sources["cost"] == [
        "roots.base_cost",
        "main.closure.extra",
    ]
    assert parser.column_sources["id"].conditions == [
        "main.closure.id",
        "main.closure.live",
    ]
    assert parser.column_sources["id"].recursive == ["closure.next_id"]


def test_schema_qualified_relation_is_in_tables():
    query = """
    WITH RECURSIVE closure AS (
        SELECT roots.id AS id, roots.child_id AS next_id, roots.base_cost AS cost
        FROM roots
        UNION ALL
        SELECT closure.next_id AS id,
               main.closure.next_id AS next_id,
               closure.cost + main.closure.extra AS cost
        FROM closure
        JOIN main.closure ON closure.next_id = main.closure.id
        WHERE main.closure.live
    )
    SELECT id, cost FROM closure
    """
    parser = Parser(query)

    assert parser.tables == ["roots", "main.closure"]
    assert "closure" not in parser.tables


def test_existing_columns_and_tables_are_unchanged():
    parser = Parser(BASE_QUERY)

    assert parser.tables == ["roots", "edges"]
    assert parser.columns == [
        "roots.id",
        "roots.child_id",
        "roots.base_cost",
        "edges.next_id",
        "closure.cost",
        "edges.extra",
        "edges.id",
        "edges.live",
    ]
    assert parser.with_names == ["closure"]
    assert "closure" not in parser.tables


def test_column_sources_aliases_and_lookup_helpers():
    parser = Parser(BASE_QUERY)

    assert parser.columns_sources("id") == parser.column_sources["id"]
    assert parser.column_sources.sources_for("cost") == [
        "roots.base_cost",
        "edges.extra",
    ]
    assert parser.column_sources.conditions_for("cost") == [
        "edges.id",
        "edges.live",
    ]
    assert parser.column_sources.recursive_for("id") == ["closure.next_id"]


def test_column_sources_to_dict_is_json_ready():
    parser = Parser(BASE_QUERY)
    payload = parser.column_sources["id"].to_dict()

    assert payload["sources"] == [
        "roots.id",
        "roots.child_id",
        "edges.next_id",
    ]
    assert payload["conditions"] == ["edges.id", "edges.live"]
    assert payload["cycles"] == []


def test_non_recursive_queries_keep_their_own_provenance():
    parser = Parser("SELECT o.total FROM orders o WHERE o.live = 1")

    assert parser.column_sources["total"] == ["orders.total"]
    assert parser.column_sources["total"].recursive == []
    assert parser.column_sources["total"].cycles == []


def test_non_recursive_cte_resolves_through_to_the_table():
    parser = Parser(
        "WITH stage AS (SELECT orders.id AS order_id FROM orders) "
        "SELECT stage.order_id AS id FROM stage"
    )

    assert parser.column_sources["id"] == ["orders.id"]


def test_unknown_cte_column_raises_key_error():
    parser = Parser(BASE_QUERY)

    try:
        parser.resolve_column_source("closure", "missing")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")


def test_unknown_cte_name_raises_key_error():
    parser = Parser(BASE_QUERY)

    try:
        parser.resolve_column_source("missing", "id")
    except KeyError:
        pass
    else:
        raise AssertionError("expected KeyError")


def test_explain_mentions_cycles_conditions_and_recursive_hops():
    parser = Parser(BASE_QUERY)
    explanation = parser.column_sources["cost"].explain()

    assert "roots.base_cost" in explanation
    assert "closure.cost from the previous iteration" in explanation
    assert "stopped at closure.cost" in explanation
    assert "conditions edges.id, edges.live" in explanation


def test_non_select_queries_have_no_column_sources():
    parser = Parser("UPDATE t SET a = 1")

    assert parser.column_sources == {}


def test_insert_select_traces_values_to_source_columns():
    parser = Parser("INSERT INTO dst (a, b) SELECT s.x, s.y FROM src s")

    assert parser.column_sources["a"] == ["src.x"]
    assert parser.column_sources["b"] == ["src.y"]


def test_outer_expression_over_cte_and_star_projection():
    parser = Parser(
        "WITH c AS (SELECT t.a AS x, t.b AS y FROM t) "
        "SELECT upper(c.x) AS label FROM c"
    )
    assert parser.column_sources["label"] == ["t.a"]

    star = Parser("SELECT * FROM t")
    assert star.column_sources["*"] == []
    assert star.column_sources["*"].unresolved == ["*"]


def test_join_using_column_is_a_condition():
    parser = Parser("SELECT a.x FROM a JOIN b USING (id)")

    assert parser.column_sources["x"] == ["a.x"]
    assert parser.column_sources["x"].conditions == []


def test_short_recursive_projection_is_reported_unresolved():
    query = (
        "WITH RECURSIVE c(a, b) AS ("
        "SELECT t.a, t.b FROM t UNION ALL SELECT c.a FROM c"
        ") SELECT a, b FROM c"
    )
    parser = Parser(query)

    assert parser.column_sources["a"] == ["t.a"]
    assert parser.column_sources["b"] == ["t.b"]
    assert parser.column_sources["b"].unresolved == ["c.b"]


def test_recursive_reference_to_an_undeclared_column_is_unresolved():
    query = (
        "WITH RECURSIVE c AS ("
        "SELECT t.a AS x FROM t UNION ALL SELECT c.y AS x FROM c"
        ") SELECT x FROM c"
    )
    parser = Parser(query)

    assert parser.column_sources["x"] == ["t.a"]
    assert parser.column_sources["x"].unresolved == ["c.y"]


def test_qualified_cte_name_is_restored_from_placeholder_map():
    parser = Parser("WITH db.c AS (SELECT a FROM t) SELECT a FROM db.c")

    assert parser.column_sources["a"] == ["t.a"]
    assert parser.with_names == ["db.c"]


def test_anonymous_projection_is_skipped():
    parser = Parser("SELECT upper(a) FROM t")

    assert parser.column_sources == {}


def test_scalar_subquery_projection_is_not_descended_into():
    parser = Parser("SELECT (SELECT t.a FROM t) AS s FROM u")

    assert parser.column_sources["s"] == []


def test_from_subquery_without_physical_relation():
    parser = Parser("SELECT s.a FROM (SELECT t.a FROM t) AS s")

    assert parser.column_sources == {"a": ["s.a"]}


def test_fromless_select_uses_the_bare_column_name():
    parser = Parser("SELECT a")

    assert parser.column_sources["a"] == ["a"]


def test_fully_qualified_column_is_not_treated_as_cte_reference():
    parser = Parser("SELECT db.t.a FROM db.t")

    assert parser.column_sources["a"] == ["db.t.a"]


def test_cte_branch_join_using_is_a_condition():
    query = (
        "WITH c AS ("
        "SELECT t.a FROM t UNION ALL SELECT c.a FROM c JOIN u USING (id)"
        ") SELECT a FROM c"
    )
    parser = Parser(query)

    assert parser.column_sources["a"] == ["t.a"]
    assert parser.column_sources["a"].conditions == ["id"]


def test_insert_without_column_list_keeps_projection_names():
    parser = Parser("INSERT INTO dst SELECT s.x FROM src s")

    assert parser.column_sources["x"] == ["src.x"]


def test_cte_anchor_without_from_keeps_bare_names():
    parser = Parser("WITH c AS (SELECT 1 AS x) SELECT x FROM c")

    assert parser.column_sources["x"] == []


def test_column_sources_support_list_lookup_by_index():
    parser = Parser(BASE_QUERY)
    sources = parser.column_sources["id"]

    assert sources[0] == "roots.id"
    assert parser.column_sources["id"].conditions == ["edges.id", "edges.live"]


def test_duplicate_recursive_hops_are_reported_once():
    query = (
        "WITH RECURSIVE c AS ("
        "SELECT t.a AS x, t.b AS y FROM t "
        "UNION ALL SELECT c.x + c.y, c.y FROM c"
        ") SELECT x, y FROM c"
    )
    parser = Parser(query)
    hops = parser.column_sources["x"].recursive_hops

    assert len(hops) == len({hop["reference"] for hop in hops})


def test_top_level_union_merges_branches_by_position():
    parser = Parser(
        "SELECT a FROM t UNION ALL SELECT b FROM u"
    )

    assert parser.column_sources["a"] == ["t.a", "u.b"]


def test_top_level_union_of_ctes_and_tables():
    parser = Parser(
        "WITH c AS (SELECT x AS v FROM s) "
        "SELECT v FROM c UNION ALL SELECT y AS v FROM u"
    )

    assert parser.column_sources["v"] == ["s.x", "u.y"]


def test_insert_select_union_maps_target_columns():
    parser = Parser(
        "INSERT INTO d (a) SELECT x FROM s UNION ALL SELECT y FROM u"
    )

    assert parser.column_sources["a"] == ["s.x", "u.y"]


def test_star_inside_cte_branch_is_unresolved():
    query = (
        "WITH c AS (SELECT * FROM t UNION ALL SELECT c.a FROM c) SELECT a FROM c"
    )
    parser = Parser(query)

    star = parser.resolve_column_source("c", "*")

    assert star.unresolved[:1] == ["*"]
    assert star.unresolved[1] == "c.a"
