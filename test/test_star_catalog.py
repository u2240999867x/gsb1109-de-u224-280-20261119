"""Tests for star-projection catalog expansion (DuckDB EXCEPT/REPLACE).

Covers :class:`sql_metadata.star_catalog.StarExpander` exposed through
:attr:`sql_metadata.Parser.catalog_columns`, including nested
subqueries, REPLACE lineage, duplicate-name suffixes, and unknown
wildcard segments when a table schema is not supplied.
"""

from sql_metadata import CatalogColumn, Parser

INNER_SQL = (
    "SELECT a.* EXCEPT (trace) REPLACE (amount * 2 AS amount), b.*, "
    "'r' AS review FROM a JOIN b ON a.id = b.id"
)
OUTER_SQL = (
    f"SELECT * EXCEPT (review) REPLACE (amount + 1 AS amount) FROM ({INNER_SQL})"
)
SCHEMA_A = ["id", "trace", "amount"]


def _names(parser: Parser) -> list[str]:
    return [column.name for column in parser.catalog_columns]


def test_catalog_expands_known_tables_in_order():
    parser = Parser(
        OUTER_SQL,
        table_schemas={"a": SCHEMA_A, "b": ["label", "stock"]},
    )
    assert _names(parser) == ["id", "amount", "label", "stock"]
    by_name = {column.name: column for column in parser.catalog_columns}
    assert all(column.known for column in parser.catalog_columns)
    # id traces straight to the physical column
    assert by_name["id"].sources == ("a.id",)
    # amount keeps the position of a.amount and carries both REPLACE hops
    assert by_name["amount"].sources == ("a.amount",)
    assert by_name["label"].sources == ("b.label",)
    assert by_name["stock"].sources == ("b.stock",)


def test_catalog_replace_lineage_chain_through_nested_queries():
    parser = Parser(
        OUTER_SQL,
        table_schemas={"a": SCHEMA_A, "b": ["label", "stock"]},
    )
    amount = next(
        column for column in parser.catalog_columns if column.name == "amount"
    )
    # The outer REPLACE only rewrites the binding sourced from a.amount;
    # the lineage still resolves to the single physical a.amount column.
    assert amount.sources == ("a.amount",)


def test_catalog_duplicate_name_gets_dedup_suffix():
    # b.stock is physically renamed to b.amount — the second binding
    # becomes amount_1 and still traces to b's physical amount column.
    parser = Parser(
        OUTER_SQL,
        table_schemas={"a": SCHEMA_A, "b": ["label", "amount"]},
    )
    assert _names(parser) == ["id", "amount", "label", "amount_1"]
    by_name = {column.name: column for column in parser.catalog_columns}
    assert by_name["amount"].sources == ("a.amount",)
    assert by_name["amount_1"].sources == ("b.amount",)
    # the outer REPLACE must not have rewritten the b-sourced binding
    assert by_name["amount_1"].known is True


def test_catalog_unknown_star_segment_is_not_named():
    # No schema for b — its wildcard segment cannot be expanded and no
    # names may be invented for it.
    parser = Parser(OUTER_SQL, table_schemas={"a": SCHEMA_A})
    catalog = parser.catalog_columns
    assert [column.name for column in catalog] == ["id", "amount", "b.*"]
    assert catalog[0].known is True and catalog[0].sources == ("a.id",)
    assert catalog[1].known is True and catalog[1].sources == ("a.amount",)
    unknown = catalog[2]
    assert unknown.known is False and unknown.sources == ()


def test_catalog_unknown_segment_shadows_later_positions():
    # Everything after an unresolvable wildcard is positionally uncertain,
    # even an explicitly projected literal alias.
    sql = "SELECT a.*, b.*, 'r' AS review FROM a JOIN b ON a.id = b.id"
    parser = Parser(sql, table_schemas={"a": ["id", "amount"]})
    catalog = parser.catalog_columns
    assert [column.name for column in catalog] == [
        "id",
        "amount",
        "b.*",
        "review",
    ]
    assert [column.known for column in catalog] == [
        True,
        True,
        False,
        False,
    ]


def test_catalog_inner_query_keeps_literal_projection():
    parser = Parser(
        INNER_SQL,
        table_schemas={"a": SCHEMA_A, "b": ["label", "stock"]},
    )
    assert _names(parser) == ["id", "amount", "label", "stock", "review"]
    review = parser.catalog_columns[-1]
    assert review.known is True and review.sources == ()


def test_catalog_plain_columns_api_still_works_without_schemas():
    parser = Parser(OUTER_SQL)
    # legacy extraction is untouched by the catalog feature
    assert parser.columns == ["*", "a.*", "b.*", "a.id", "b.id"]
    assert parser.output_columns == ["*", "a.*", "b.*", "review"]
    # without any schemas every star stays unknown, nothing is invented
    assert all(not column.known for column in parser.catalog_columns)


def test_catalog_qualified_star_unknown_marker():
    parser = Parser("SELECT a.* FROM a", table_schemas={})
    catalog = parser.catalog_columns
    assert len(catalog) == 1
    assert catalog[0] == CatalogColumn.unknown("a.*")


def test_catalog_uses_table_alias_schema_and_real_name():
    parser = Parser(
        "SELECT x.* EXCEPT (trace) FROM a AS x",
        table_schemas={"a": SCHEMA_A},
    )
    assert _names(parser) == ["id", "amount"]
    assert parser.catalog_columns[0].sources == ("a.id",)


def test_catalog_replacement_with_qualified_inner_column():
    parser = Parser(
        "SELECT a.* REPLACE (a.amount * 2 AS amount) FROM a",
        table_schemas={"a": SCHEMA_A},
    )
    assert _names(parser) == ["id", "trace", "amount"]
    amount = next(
        column for column in parser.catalog_columns if column.name == "amount"
    )
    assert amount.sources == ("a.amount",)


def test_catalog_replacement_without_matching_target_is_noop():
    parser = Parser(
        "SELECT * REPLACE (ghost + 1 AS ghost) FROM a",
        table_schemas={"a": ["id", "amount"]},
    )
    assert _names(parser) == ["id", "amount"]


def test_catalog_bare_replace_resolves_source_through_relation():
    parser = Parser(
        "SELECT * REPLACE (amount * 2 AS amount) FROM a",
        table_schemas={"a": ["id", "amount"]},
    )
    assert parser.catalog_columns[1].sources == ("a.amount",)


def test_catalog_multiple_duplicates_get_incrementing_suffixes():
    parser = Parser(
        "SELECT * REPLACE (id + 1 AS id), amount, amount FROM a",
        table_schemas={"a": ["id", "amount"]},
    )
    assert _names(parser) == ["id", "amount", "amount_1", "amount_2"]


def test_catalog_expands_aliased_subquery():
    parser = Parser(
        "SELECT * FROM (SELECT id, amount FROM a) AS sub",
        table_schemas={"a": ["id", "amount"]},
    )
    assert _names(parser) == ["id", "amount"]
    assert parser.catalog_columns[0].sources == ("id",)


def test_catalog_expands_union_subquery_from_first_select():
    sql = "SELECT * FROM (SELECT id, amount FROM a UNION SELECT id, amount FROM a) AS u"
    parser = Parser(sql, table_schemas={"a": ["id", "amount"]})
    assert _names(parser) == ["id", "amount"]


def test_catalog_opaque_values_subquery_is_unknown():
    parser = Parser("SELECT * FROM (VALUES (1, 2)) AS v")
    catalog = parser.catalog_columns
    assert len(catalog) == 1 and catalog[0].known is False
    assert catalog[0].name == "v.*"


def test_catalog_expands_cte_relation():
    sql = "WITH c AS (SELECT id, amount FROM a) SELECT * EXCEPT (amount) FROM c"
    parser = Parser(sql, table_schemas={"a": ["id", "amount"]})
    assert _names(parser) == ["id"]


def test_catalog_non_select_query_returns_empty():
    assert Parser("CREATE TABLE t (id INT)").catalog_columns == []


def test_catalog_malformed_query_returns_empty():
    parser = Parser("WITH c AS (SELECT) ) AS")
    assert parser.catalog_columns == []


def test_catalog_is_cached():
    parser = Parser("SELECT * FROM a", table_schemas={"a": ["id", "amount"]})
    assert parser.catalog_columns is parser.catalog_columns


def test_catalog_function_expression_records_sources():
    parser = Parser(
        "SELECT *, amount + 1 AS bumped FROM a",
        table_schemas={"a": ["id", "amount"]},
    )
    bumped = parser.catalog_columns[-1]
    # the bare reference resolves to the physical column of relation a
    assert bumped.name == "bumped" and bumped.sources == ("a.amount",)
