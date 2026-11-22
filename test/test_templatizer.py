"""Tests for structural query templates (log grouping)."""

from sql_metadata import Parser, Templatizer


def template(sql, **kwargs):
    return Templatizer(sql, **kwargs).template


class TestEmptyAndUnparseable:
    def test_empty_query(self):
        templatizer = Templatizer("")
        assert templatizer.template == ""
        assert templatizer.reliable
        assert templatizer.issues == ()

    def test_whitespace_only_query(self):
        assert Templatizer("   \n  ").template == ""

    def test_comment_only_query(self):
        assert Templatizer("/* nothing but a comment */").template == ""

    def test_unparseable_query_falls_back_to_generalize(self):
        templatizer = Templatizer("SELECT 'unclosed")
        # Falls back to the plain literal generalisation, flagged unreliable.
        assert templatizer.template == Templatizer("SELECT 'unclosed").template
        assert not templatizer.reliable
        assert templatizer.issues == (
            "query could not be parsed structurally; "
            "fell back to literal generalisation",
        )


class TestValuesStayOutOfTemplate:
    def test_comparison_values_are_removed(self):
        assert (
            template("SELECT * FROM t WHERE a = 1 AND b = 'x'")
            == "SELECT * FROM t WHERE a = ? AND b = ?"
        )

    def test_changing_comparison_value_keeps_group(self):
        assert template("SELECT * FROM t WHERE a = 1") == template(
            "SELECT * FROM t WHERE a = 999"
        )

    def test_bind_values_are_removed(self):
        assert (
            template("SELECT * FROM t WHERE a = :value")
            == "SELECT * FROM t WHERE a = :p1"
        )

    def test_negative_numbers_and_intervals_are_values(self):
        assert (
            template("SELECT * FROM t WHERE x = -5 AND y = INTERVAL '3 days'")
            == "SELECT * FROM t WHERE x = ? AND y = INTERVAL (?) DAYS"
        )

    def test_negated_expression_is_kept(self):
        assert (
            template("SELECT * FROM t WHERE x = -y") == "SELECT * FROM t WHERE x = -y"
        )

    def test_booleans_are_structure(self):
        assert (
            template("SELECT * FROM t WHERE active = TRUE")
            == "SELECT * FROM t WHERE active = TRUE"
        )

    def test_limit_and_offset_are_values(self):
        assert (
            template("SELECT * FROM t LIMIT 10 OFFSET 5")
            == "SELECT * FROM t LIMIT ? OFFSET ?"
        )

    def test_anonymous_bindings_are_canonicalised(self):
        assert (
            template("SELECT * FROM t WHERE a = ? AND b = ?")
            == "SELECT * FROM t WHERE a = :p1 AND b = :p2"
        )


class TestPostgresTemplate:
    """A template with a granularity, a JSON path and a comparison value."""

    BASE = (
        "SELECT date_trunc('day', created_at) AS bucket, data -> 'day' AS d "
        "FROM events WHERE period = 'day' ORDER BY 1"
    )

    def test_base_template(self):
        assert template(self.BASE) == (
            "SELECT DATE_TRUNC('DAY', created_at) AS bucket, "
            "JSON_EXTRACT(data, '$.day') AS d "
            "FROM events WHERE period = ? ORDER BY 1"
        )

    def test_changing_only_comparison_value_keeps_group(self):
        other = self.BASE.replace("period = 'day'", "period = 'month'")
        assert template(other) == template(self.BASE)

    def test_changing_granularity_splits_group(self):
        other = self.BASE.replace("date_trunc('day'", "date_trunc('month'")
        assert template(other) != template(self.BASE)
        assert "DATE_TRUNC('MONTH', created_at)" in template(other)

    def test_changing_json_path_splits_group(self):
        other = self.BASE.replace("data -> 'day'", "data -> 'hour'")
        assert template(other) != template(self.BASE)
        assert "'$.hour'" in template(other)

    def test_changing_order_by_position_splits_group(self):
        other = self.BASE.replace("ORDER BY 1", "ORDER BY 2")
        assert template(other) != template(self.BASE)
        assert template(other).endswith("ORDER BY 2")

    def test_group_by_position_is_structural(self):
        assert template("SELECT x FROM t GROUP BY 1") != template(
            "SELECT x FROM t GROUP BY 2"
        )

    def test_same_named_literals_in_three_roles(self):
        # 'day' as granularity and as JSON path stays, 'day' as a
        # comparison value is removed — all within one template.
        result = template(self.BASE)
        assert "DATE_TRUNC('DAY', created_at)" in result
        assert "JSON_EXTRACT(data, '$.day')" in result
        assert "period = ?" in result
        assert "'day'" not in result.split("WHERE")[1]

    def test_json_path_variants(self):
        assert "JSONB_EXTRACT(data, '{a,day}')" in template(
            "SELECT data #> '{a,day}' FROM t"
        )
        assert "'$.day'" in template("SELECT data ->> 'day' FROM t")
        assert "'day'" in template("SELECT json_extract_path(data, 'day') FROM t")
        assert template("SELECT data #> '{a,day}' FROM t") != template(
            "SELECT data #> '{a,hour}' FROM t"
        )

    def test_parameterised_granularity_is_a_named_slot(self):
        assert (
            template("SELECT date_trunc(:granularity, created_at) FROM events")
            == "SELECT DATE_TRUNC(:p1, created_at) FROM events"
        )


class TestRenamingAndCommutativity:
    def test_and_items_can_be_swapped(self):
        first = template("SELECT * FROM t WHERE score > :lo AND score < :hi")
        second = template("SELECT * FROM t WHERE score < :hi AND score > :lo")
        assert first == second
        assert first == "SELECT * FROM t WHERE score < :p1 AND score > :p2"

    def test_same_binding_twice_changes_structure(self):
        assert template("SELECT * FROM t WHERE score > :x AND score < :x") != template(
            "SELECT * FROM t WHERE score > :lo AND score < :hi"
        )
        assert (
            template("SELECT * FROM t WHERE score > :x AND score < :x")
            == "SELECT * FROM t WHERE score < :p1 AND score > :p1"
        )

    def test_reports_with_different_aliases_and_params_group_together(self):
        report_a = (
            "SELECT o.id FROM orders o JOIN customers c ON c.id = o.cid "
            "WHERE o.total > :lo AND o.total < :hi"
        )
        report_b = (
            "SELECT x.id FROM orders x JOIN customers y ON y.id = x.cid "
            "WHERE x.total < :max AND x.total > :min"
        )
        assert template(report_a) == template(report_b)
        assert template(report_a) == (
            "SELECT t1.id FROM orders AS t1 JOIN customers AS t2 "
            "ON t2.id = t1.cid WHERE t1.total < :p1 AND t1.total > :p2"
        )

    def test_top_level_and_chain_is_sorted(self):
        assert template("SELECT * FROM t WHERE c = 3 AND a = 1 AND b = 2") == template(
            "SELECT * FROM t WHERE a = 1 AND b = 2 AND c = 3"
        )

    def test_or_items_are_not_sorted(self):
        assert template("SELECT * FROM t WHERE a = 1 OR b = 2") != template(
            "SELECT * FROM t WHERE b = 2 OR a = 1"
        )

    def test_table_names_are_structure(self):
        assert template("SELECT * FROM orders") != template("SELECT * FROM users")

    def test_unaliased_qualifier_is_kept(self):
        assert (
            template("SELECT orders.id FROM orders") == "SELECT orders.id FROM orders"
        )

    def test_many_bindings_fall_back_to_first_occurrence_naming(self):
        # 8! = 40320 renamings exceed the canonicalisation cap, so the
        # naming follows the order of first occurrence instead.
        sql = (
            "SELECT * FROM t WHERE h = :h AND g = :g AND f = :f AND e = :e "
            "AND d = :d AND c = :c AND b = :b AND a = :a"
        )
        result = template(sql)
        assert ":p1" in result and ":p8" in result
        assert result == (
            "SELECT * FROM t WHERE a = :p8 AND b = :p7 AND c = :p6 "
            "AND d = :p5 AND e = :p4 AND f = :p3 AND g = :p2 AND h = :p1"
        )


class TestShadowingSubqueries:
    def test_shadowed_alias_points_to_inner_binding(self):
        assert template(
            "SELECT * FROM orders o "
            "WHERE EXISTS (SELECT 1 FROM orders o WHERE o.id = o.id)"
        ) == (
            "SELECT * FROM orders AS t1 "
            "WHERE EXISTS(SELECT ? FROM orders AS t2 WHERE t2.id = t2.id)"
        )

    def test_correlated_reference_points_to_outer_binding(self):
        assert template(
            "SELECT * FROM orders o "
            "WHERE EXISTS (SELECT 1 FROM orders o2 WHERE o2.id = o.id)"
        ) == (
            "SELECT * FROM orders AS t1 "
            "WHERE EXISTS(SELECT ? FROM orders AS t2 WHERE t2.id = t1.id)"
        )

    def test_shadowing_and_correlation_are_distinguishable(self):
        shadowed = template(
            "SELECT * FROM orders o "
            "WHERE EXISTS (SELECT 1 FROM orders o WHERE o.id = o.id)"
        )
        correlated = template(
            "SELECT * FROM orders o "
            "WHERE EXISTS (SELECT 1 FROM orders o2 WHERE o2.id = o.id)"
        )
        assert shadowed != correlated

    def test_unqualified_columns_stay_unqualified(self):
        assert (
            template("SELECT id FROM orders WHERE total > 1")
            == "SELECT id FROM orders WHERE total > ?"
        )

    def test_derived_table_alias_is_canonicalised(self):
        assert template(
            "SELECT s.x FROM (SELECT a AS x FROM t WHERE a = 1) s WHERE s.y = 2"
        ) == (
            "SELECT t1.x FROM (SELECT a AS x FROM t WHERE a = ?) AS t1 WHERE t1.y = ?"
        )


class TestJsonPathBindings:
    def test_dual_role_binding_is_flagged(self):
        templatizer = Templatizer("SELECT * FROM t WHERE region = :x AND data -> :x")
        assert not templatizer.reliable
        assert templatizer.issues == (
            "binding :x is used both as a comparison value and as a JSON "
            "path but is not declared as public structure; the template "
            "cannot reliably separate queries with different paths",
        )

    def test_dual_role_binding_keeps_dependency_visible(self):
        # The same canonical name appears in both roles, so the
        # dependency is visible, but the path slot is marked private and
        # the group cannot mix with plain comparison groups.
        result = template("SELECT * FROM t WHERE region = :x AND data -> :x")
        assert result == (
            "SELECT * FROM t WHERE JSON_EXTRACT(data, :p1[private-path]) "
            "AND region = :p1"
        )

    def test_dual_role_binding_does_not_mix_with_plain_groups(self):
        dual = template("SELECT * FROM t WHERE region = :x AND data -> :x")
        plain = template("SELECT * FROM t WHERE region = :x AND data -> 'day'")
        declared = template(
            "SELECT * FROM t WHERE region = :x AND data -> :path",
            declared_paths={"path"},
        )
        assert dual != plain
        assert dual != declared

    def test_independent_declared_path_is_reliable(self):
        templatizer = Templatizer(
            "SELECT * FROM t WHERE region = :x AND data -> :path",
            declared_paths={"path"},
        )
        assert templatizer.reliable
        assert templatizer.issues == ()
        assert templatizer.template == (
            "SELECT * FROM t WHERE JSON_EXTRACT(data, :p1) AND region = :p2"
        )

    def test_declared_dual_role_binding_is_reliable(self):
        templatizer = Templatizer(
            "SELECT * FROM t WHERE region = :x AND data -> :x",
            declared_paths={"x"},
        )
        assert templatizer.reliable
        assert templatizer.template == (
            "SELECT * FROM t WHERE JSON_EXTRACT(data, :p1) AND region = :p1"
        )

    def test_undeclared_path_only_binding_is_flagged(self):
        templatizer = Templatizer("SELECT data -> :path FROM t")
        assert not templatizer.reliable
        assert templatizer.issues == (
            "binding :path provides a JSON path but is not declared as "
            "public structure; the template cannot reliably separate "
            "queries with different paths",
        )
        assert (
            templatizer.template
            == "SELECT JSON_EXTRACT(data, :p1[private-path]) FROM t"
        )

    def test_declared_path_names_are_canonicalised(self):
        assert template(
            "SELECT data -> :path FROM t", declared_paths={"path"}
        ) == template("SELECT data -> :route FROM t", declared_paths={"route"})

    def test_private_path_is_not_assumed_fixed(self):
        # An undeclared path binding must not collapse into the group of
        # any fixed literal path.
        assert template("SELECT data -> :path FROM t") != template(
            "SELECT data -> 'day' FROM t"
        )

    def test_path_binding_in_function_form(self):
        templatizer = Templatizer(
            "SELECT json_extract_path(data, :x) FROM t WHERE region = :x"
        )
        assert not templatizer.reliable
        assert ":p1[private-path]" in templatizer.template


class TestInLists:
    def test_scalar_list_size_counts_by_default(self):
        assert template("SELECT * FROM t WHERE x IN (1, 2, 3)") == (
            "SELECT * FROM t WHERE x IN (?, ?, ?)"
        )
        assert template("SELECT * FROM t WHERE x IN (1, 2, 3)") != template(
            "SELECT * FROM t WHERE x IN (1, 2)"
        )

    def test_scalar_list_size_can_be_ignored(self):
        assert template(
            "SELECT * FROM t WHERE x IN (1, 2, 3)", ignore_in_list_size=True
        ) == template("SELECT * FROM t WHERE x IN (1, 2)", ignore_in_list_size=True)
        assert (
            template("SELECT * FROM t WHERE x IN (1, 2, 3)", ignore_in_list_size=True)
            == "SELECT * FROM t WHERE x IN (?)"
        )

    def test_tuple_arity_stays_distinguishable(self):
        pair = template(
            "SELECT * FROM t WHERE (a, b) IN ((1, 2), (3, 4))",
            ignore_in_list_size=True,
        )
        triple = template(
            "SELECT * FROM t WHERE (a, b, c) IN ((1, 2, 3))",
            ignore_in_list_size=True,
        )
        assert pair == "SELECT * FROM t WHERE (a, b) IN ((?, ?))"
        assert triple == "SELECT * FROM t WHERE (a, b, c) IN ((?, ?, ?))"
        assert pair != triple

    def test_tuple_row_count_can_be_ignored(self):
        assert template(
            "SELECT * FROM t WHERE (a, b) IN ((1, 2), (3, 4))",
            ignore_in_list_size=True,
        ) == template(
            "SELECT * FROM t WHERE (a, b) IN ((1, 2))", ignore_in_list_size=True
        )

    def test_tuple_rows_count_by_default(self):
        assert template("SELECT * FROM t WHERE (a, b) IN ((1, 2), (3, 4))") != template(
            "SELECT * FROM t WHERE (a, b) IN ((1, 2))"
        )

    def test_mixed_list_is_left_untouched(self):
        assert (
            template("SELECT * FROM t WHERE x IN ((1, 2), 3)", ignore_in_list_size=True)
            == "SELECT * FROM t WHERE x IN ((?, ?), ?)"
        )

    def test_subquery_in_stays_distinguishable(self):
        assert (
            template("SELECT * FROM t WHERE x IN (SELECT y FROM u)")
            == "SELECT * FROM t WHERE x IN (SELECT y FROM u)"
        )
        assert template(
            "SELECT * FROM t WHERE x IN (SELECT y FROM u)", ignore_in_list_size=True
        ) != template("SELECT * FROM t WHERE x IN (1)", ignore_in_list_size=True)

    def test_numbers_in_quoted_identifiers_stay_distinguishable(self):
        assert template('SELECT "col2" FROM t') == 'SELECT "col2" FROM t'
        assert template('SELECT "col2" FROM t') != template('SELECT "col3" FROM t')
        assert template("SELECT `col2` FROM t") != template("SELECT `col3` FROM t")


class TestValuesLists:
    def test_values_rows_count_by_default(self):
        assert (
            template("INSERT INTO t (a, b) VALUES (1, 2), (3, 4)")
            == "INSERT INTO t (a, b) VALUES (?, ?), (?, ?)"
        )

    def test_values_row_count_can_be_ignored(self):
        assert (
            template(
                "INSERT INTO t (a, b) VALUES (1, 2), (3, 4)",
                ignore_in_list_size=True,
            )
            == "INSERT INTO t (a, b) VALUES (?, ?)"
        )


class TestScopesBeyondSelect:
    def test_update_alias_is_canonicalised(self):
        assert (
            template("UPDATE orders o SET total = 0 WHERE o.id = 1")
            == "UPDATE orders AS t1 SET total = ? WHERE t1.id = ?"
        )

    def test_delete_alias_is_canonicalised(self):
        assert (
            template("DELETE FROM orders o WHERE o.id = 1")
            == "DELETE FROM orders AS t1 WHERE t1.id = ?"
        )

    def test_delete_using_alias_is_canonicalised(self):
        assert (
            template("DELETE FROM orders o USING users u WHERE o.id = u.id")
            == "DELETE FROM orders AS t1 USING users AS t2 WHERE t1.id = t2.id"
        )

    def test_nested_join_aliases_are_canonicalised(self):
        first = template(
            "SELECT * FROM t1 JOIN (t2 a JOIN t3 b ON a.x = b.x) ON t1.x = a.x"
        )
        second = template(
            "SELECT * FROM t1 JOIN (t2 x JOIN t3 y ON x.x = y.x) ON t1.x = x.x"
        )
        assert first == second
        assert "t2 AS t1 JOIN t3 AS t2" in first

    def test_cte_names_are_structure(self):
        assert template(
            "WITH c AS (SELECT x FROM t WHERE x = 5) SELECT * FROM c WHERE c.x = 1"
        ) == ("WITH c AS (SELECT x FROM t WHERE x = ?) SELECT * FROM c WHERE c.x = ?")

    def test_table_valued_function_arguments_are_values(self):
        assert (
            template("SELECT * FROM generate_series(1, 10) g")
            == "SELECT * FROM GENERATE_SERIES(?, ?) AS t1"
        )


class TestParserIntegration:
    def test_parser_template(self):
        parser = Parser("SELECT * FROM t WHERE a = 1")
        assert parser.template() == "SELECT * FROM t WHERE a = ?"

    def test_parser_template_with_options(self):
        parser = Parser("SELECT * FROM t WHERE x IN (1, 2, 3)")
        assert parser.template(ignore_in_list_size=True) == (
            "SELECT * FROM t WHERE x IN (?)"
        )

    def test_parser_template_with_declared_paths(self):
        parser = Parser("SELECT data -> :path FROM t")
        assert parser.template(declared_paths={"path"}) == (
            "SELECT JSON_EXTRACT(data, :p1) FROM t"
        )

    def test_generalize_is_unaffected(self):
        # The plain literal generalisation keeps its existing behaviour.
        assert (
            Parser("SELECT * FROM t WHERE a = 1 AND b = 'x'").generalize
            == "SELECT * FROM t WHERE a = N AND b = X"
        )
