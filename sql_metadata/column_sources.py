"""Column value provenance through CTEs, including recursive CTEs.

The :class:`ColumnSourcesResolver` answers a different question from
:attr:`Parser.columns`: instead of listing every column that appears in the
query, it traces each *output* column back to the physical columns that can
feed its value.

Recursive CTEs are resolved positionally.  A ``UNION ALL`` body contributes
one projection per declared CTE column, and the recursive branch's *n*-th
projection feeds the CTE's *n*-th output column regardless of the aliases
written on that projection.  A bare reference to the CTE name (for example
``closure.next_id``) is a reference to the previous iteration's output and is
followed into the CTE definition.  A schema-qualified relation with the same
name (for example ``main.closure.next_id``) is a physical table and is kept
as-is.

Every reference is processed at most once per dependency path.  A column that
depends on itself (``cost = cost + extra``) therefore terminates with a cycle
marker after its non-recursive sources have been collected.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Any

from sqlglot import exp


def _unique(items: Iterable[str]) -> list[str]:
    """Return *items* with duplicates removed, preserving first occurrence.

    :param items: Arbitrary iterable of strings.
    :returns: Deduplicated list.
    """
    return list(dict.fromkeys(items))


def _table_name(table: exp.Table) -> str:
    """Return the dotted physical name of a table node.

    :param table: A sqlglot table node.
    :returns: Dot-joined catalog/db/name string.
    """
    parts = [table.catalog, table.db, table.name]
    return ".".join(part for part in parts if part)


class ColumnSources(list[str]):
    """Physical value sources for one output column.

    The object is a plain list of source-column names so callers can compare
    it directly with a list.  Extra provenance is available as attributes and
    through string lookups:

    .. code-block:: python

       sources = parser.column_sources["id"]
       sources == ["roots.id", "roots.child_id", "edges.next_id"]
       sources.conditions      # ["edges.id", "edges.live"]
       sources.recursive       # ["closure.next_id"]

    :param sources: Physical column names that can supply this value.
    :param conditions: Physical columns used by joins/filters that constrain
        the value.
    :param recursive: CTE column references traversed across iterations.
    :param recursive_hops: Structured descriptions of those traversals.
    :param cycles: Self-dependent CTE columns at which analysis stopped.
    :param unresolved: References that could not be resolved.
    """

    def __init__(
        self,
        sources: Iterable[str] = (),
        *,
        conditions: Iterable[str] = (),
        recursive: Iterable[str] = (),
        recursive_hops: list[dict[str, Any]] | None = None,
        cycles: Iterable[str] = (),
        unresolved: Iterable[str] = (),
    ) -> None:
        super().__init__(_unique(sources))
        self.conditions = _unique(conditions)
        self.recursive = _unique(recursive)
        self.recursive_hops = list(recursive_hops or [])
        self.cycles = _unique(cycles)
        self.unresolved = _unique(unresolved)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable description of the provenance.

        :returns: Mapping with ``sources``, ``conditions``, ``recursive``,
            ``recursive_hops``, ``cycles``, and ``unresolved`` keys.
        """
        return {
            "sources": list(self),
            "conditions": list(self.conditions),
            "recursive": list(self.recursive),
            "recursive_hops": list(self.recursive_hops),
            "cycles": list(self.cycles),
            "unresolved": list(self.unresolved),
        }

    def explain(self) -> str:
        """Return a short human-readable explanation of this provenance."""
        parts = [", ".join(self) if self else "(no physical source)"]
        if self.recursive:
            hops = ", ".join(
                f"{hop['reference']} from the previous iteration"
                for hop in self.recursive_hops
            )
            parts.append("via " + (hops or ", ".join(self.recursive)))
        if self.cycles:
            parts.append("stopped at " + ", ".join(self.cycles))
        if self.conditions:
            parts.append("conditions " + ", ".join(self.conditions))
        return "; ".join(parts)


class ColumnSourcesMap(dict[str, ColumnSources]):
    """Map of output-column name to :class:`ColumnSources`.

    The mapping is also callable, so both common access styles work::

        parser.column_sources["id"]
        parser.column_sources("id")

    Calling with a column name returns that column's provenance.
    """

    def __call__(self, column: str) -> ColumnSources:
        """Return one column's provenance.

        :param column: Output column name.
        :returns: The matching :class:`ColumnSources`.
        :raises KeyError: If *column* is not an output column.
        """
        return self[column]

    def sources_for(self, column: str) -> list[str]:
        """Return just the physical source list for *column*."""
        return list(self[column])

    def conditions_for(self, column: str) -> list[str]:
        """Return the join/filter columns that constrain *column*."""
        return list(self[column].conditions)

    def recursive_for(self, column: str) -> list[str]:
        """Return the recursive references followed for *column*."""
        return list(self[column].recursive)


@dataclass
class _Resolution:
    """Mutable accumulator used while resolving one expression."""

    sources: list[str] = field(default_factory=list)
    conditions: list[str] = field(default_factory=list)
    recursive: list[str] = field(default_factory=list)
    recursive_hops: list[dict[str, Any]] = field(default_factory=list)
    cycles: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)

    def merge(self, other: "_Resolution") -> None:
        """Merge *other* into this accumulator, preserving order."""
        self.sources = _unique([*self.sources, *other.sources])
        self.conditions = _unique([*self.conditions, *other.conditions])
        self.recursive = _unique([*self.recursive, *other.recursive])
        for hop in other.recursive_hops:
            if hop not in self.recursive_hops:
                self.recursive_hops.append(hop)
        self.cycles = _unique([*self.cycles, *other.cycles])
        self.unresolved = _unique([*self.unresolved, *other.unresolved])


def _merge_resolution(existing: ColumnSources, other: "_Resolution") -> "_Resolution":
    """Combine an already-published column with another branch's resolution.

    :param existing: Provenance collected from earlier UNION branches.
    :param other: Provenance from the next branch.
    :returns: Merged resolution in branch order.
    """
    merged = _Resolution(
        sources=list(existing),
        conditions=list(existing.conditions),
        recursive=list(existing.recursive),
        recursive_hops=list(existing.recursive_hops),
        cycles=list(existing.cycles),
        unresolved=list(existing.unresolved),
    )
    merged.merge(other)
    return merged


@dataclass
class _CteInfo:
    """Resolved shape of a CTE definition."""

    name: str
    display_name: str
    node: exp.CTE
    output_names: list[str]
    anchor: exp.Expression
    recursive: list[exp.Expression] = field(default_factory=list)


def _iter_columns(node: exp.Expression) -> Iterator[exp.Column]:
    """Yield columns from *node* without descending into nested queries.

    :param node: Expression to inspect.
    :yields: Each column reference belonging to the current query level.
    """
    if isinstance(node, (exp.Select, exp.Subquery)):
        return
    if isinstance(node, exp.Column):
        yield node
        return
    for child in node.iter_expressions():
        yield from _iter_columns(child)


def _projection_name(expression: exp.Expression) -> str | None:
    """Return the output name of a SELECT projection.

    :param expression: A projection expression.
    :returns: Alias name, column name, ``"*"``, or ``None`` for anonymous
        expressions.
    """
    if isinstance(expression, exp.Alias):
        return expression.alias
    if isinstance(expression, exp.Column):
        return expression.name
    if isinstance(expression, exp.Star):
        return "*"
    return None


def _projection_expressions(select: exp.Expression) -> list[exp.Expression]:
    """Return the SELECT list of *select* (empty for non-SELECT nodes)."""
    return list(getattr(select, "expressions", []))


def _set_operation_branches(
    node: exp.Expression,
) -> list[exp.Expression]:
    """Flatten a set-operation chain into its constituent SELECTs.

    ``anchor UNION ALL recursive`` returns ``[anchor, recursive]``.  Nested
    left-deep unions are flattened in source order.

    :param node: A set-operation or SELECT node.
    :returns: Branch expressions, left to right.
    """
    if isinstance(node, exp.SetOperation):
        return [
            *_set_operation_branches(node.this),
            *_set_operation_branches(node.expression),
        ]
    return [node]


def _condition_expressions(select: exp.Expression) -> Iterator[exp.Expression]:
    """Yield join ON, WHERE, HAVING, and QUALIFY expressions of *select*."""
    args = getattr(select, "args", {})
    for join in args.get("joins") or []:
        if join.args.get("on") is not None:
            yield join.args["on"]
        for identifier in join.args.get("using") or []:
            yield exp.column(identifier.name)
    for key in ("where", "having", "qualify"):
        clause = args.get(key)
        if isinstance(clause, exp.Expression):
            yield clause


class ColumnSourcesResolver:
    """Resolve output-column provenance for a parsed SQL query.

    :param ast: Root sqlglot AST node.
    :param table_aliases: Mapping of table alias to physical table name.
    :param cte_name_map: Optional placeholder-to-original CTE name mapping.
    """

    def __init__(
        self,
        ast: exp.Expression,
        table_aliases: dict[str, str] | None = None,
        cte_name_map: dict[str, str] | None = None,
    ) -> None:
        self._ast = ast
        self._table_aliases = table_aliases or {}
        self._cte_name_map = cte_name_map or {}
        self._ctes: dict[str, _CteInfo] = {}
        self._build_ctes()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def resolve(self) -> ColumnSourcesMap:
        """Resolve every output column of the main query.

        For a top-level ``UNION`` the branches are merged positionally, so
        an output column can list sources from every branch.

        :returns: Mapping of output name to :class:`ColumnSources`.
        """
        statement = self._statement_query()
        if statement is None:
            return ColumnSourcesMap()
        branches = _set_operation_branches(statement)
        output_names = self._insert_target_names() or [
            name
            for projection in _projection_expressions(branches[0])
            if (name := _projection_name(projection)) is not None
        ]
        result = ColumnSourcesMap()
        for branch in branches:
            from_name = self._from_relation_name(branch)
            for position, projection in enumerate(_projection_expressions(branch)):
                if position >= len(output_names):
                    continue
                value = (
                    projection.this if isinstance(projection, exp.Alias) else projection
                )
                resolution = self._resolve_outer_expression(value, from_name)
                name = output_names[position]
                if name in result:
                    result[name] = self._to_public(
                        _merge_resolution(result[name], resolution)
                    )
                else:
                    result[name] = self._to_public(resolution)
        return result

    def _statement_query(self) -> exp.Expression | None:
        """Return the query that supplies the statement's output columns."""
        if isinstance(self._ast, (exp.Select, exp.SetOperation)):
            return self._ast
        expression = self._ast.args.get("expression")
        if isinstance(expression, (exp.Select, exp.SetOperation)):
            return expression
        return None

    def _insert_target_names(self) -> list[str]:
        """Return the INSERT column list so outputs map onto real targets.

        For ``INSERT INTO dst (a, b) SELECT s.x, s.y ...`` the SELECT
        projections are positional, so their provenance belongs to ``a``
        and ``b`` rather than to ``x`` and ``y``.
        """
        if not isinstance(self._ast, exp.Insert):
            return []
        schema = self._ast.this
        if not isinstance(schema, exp.Schema):
            return []
        return [
            expression.name
            for expression in schema.expressions
            if isinstance(expression, exp.Identifier)
        ]

    def resolve_cte_column(self, cte_name: str, column_name: str) -> ColumnSources:
        """Resolve one CTE output column by name.

        :param cte_name: CTE alias.
        :param column_name: Declared output column name.
        :returns: Provenance for that CTE output.
        :raises KeyError: If the CTE or column is unknown.
        """
        if cte_name not in self._ctes:
            raise KeyError(cte_name)
        if column_name not in self._ctes[cte_name].output_names:
            raise KeyError(column_name)
        return self._to_public(self._resolve_cte_column(cte_name, column_name, set()))

    # ------------------------------------------------------------------
    # CTE discovery
    # ------------------------------------------------------------------

    def _with_clause(self) -> exp.With | None:
        """Return the statement's WITH clause, if present."""
        for node in (self._ast, self._statement_query()):
            if node is None:
                continue
            with_clause = node.args.get("with_")
            if isinstance(with_clause, exp.With):
                return with_clause
        return None

    def _build_ctes(self) -> None:
        """Index every CTE by its AST alias and original display name."""
        with_clause = self._with_clause()
        if with_clause is None:
            return
        for cte in with_clause.expressions:
            if isinstance(cte, exp.CTE) and cte.alias:
                alias = cte.alias
                display = self._cte_name_map.get(alias, alias)
                info = self._describe_cte(alias, display, cte)
                self._ctes[alias] = info
                self._ctes.setdefault(display, info)

    @staticmethod
    def _describe_cte(alias: str, display: str, cte: exp.CTE) -> _CteInfo:
        """Describe one CTE's declared output names and branches."""
        body = cte.this
        table_alias = cte.args.get("alias")
        declared = [
            column.name
            for column in (
                table_alias.columns if isinstance(table_alias, exp.TableAlias) else []
            )
        ]

        branches = _set_operation_branches(body)
        anchor = branches[0]
        recursive = branches[1:]
        anchor_names = [
            name
            for expression in _projection_expressions(anchor)
            if (name := _projection_name(expression)) is not None
        ]

        output_names = list(declared) or list(anchor_names)

        return _CteInfo(
            name=alias,
            display_name=display,
            node=cte,
            output_names=output_names,
            anchor=anchor,
            recursive=recursive,
        )

    # ------------------------------------------------------------------
    # Outer query resolution
    # ------------------------------------------------------------------

    @staticmethod
    def _from_table(select: exp.Expression) -> exp.Table | None:
        """Return the first FROM relation when it is a real table node."""
        from_clause = getattr(select, "args", {}).get("from_")
        if isinstance(from_clause, exp.From) and isinstance(
            from_clause.this, exp.Table
        ):
            return from_clause.this
        return None

    def _from_relation_name(self, select: exp.Expression) -> str | None:
        """Return the first FROM relation's name (CTE or physical)."""
        relation = self._from_table(select)
        return None if relation is None else relation.name

    def _from_physical_name(self, select: exp.Expression) -> str | None:
        """Return the physical FROM table name, or ``None`` for nested sources."""
        relation = self._from_table(select)
        if relation is None:
            return None
        if not relation.db and not relation.catalog and relation.name in self._ctes:
            return None
        return _table_name(relation)

    def _resolve_outer_expression(
        self, expression: exp.Expression, from_name: str | None
    ) -> _Resolution:
        """Resolve an outer SELECT expression."""
        if isinstance(expression, exp.Column):
            return self._resolve_outer_column(expression, from_name)
        resolution = _Resolution()
        for column in _iter_columns(expression):
            resolution.merge(self._resolve_outer_column(column, from_name))
        if expression.find(exp.Star):
            resolution.unresolved.append("*")
        return resolution

    def _resolve_outer_column(
        self, column: exp.Column, from_name: str | None
    ) -> _Resolution:
        """Resolve one column appearing in the outer query."""
        cte_name = self._cte_reference_name(column, from_name)
        if cte_name is not None and column.name in self._ctes[cte_name].output_names:
            return self._resolve_cte_column(cte_name, column.name, set())
        return self._resolve_physical_column(column, self._physical_from(from_name))

    def _cte_reference_name(
        self, column: exp.Column, from_name: str | None
    ) -> str | None:
        """Return the CTE name referenced by *column*, if any.

        A qualified reference matches the CTE only when it has no schema or
        catalog part.  ``main.closure.next_id`` therefore stays physical even
        when a CTE named ``closure`` exists.
        """
        if column.db or column.catalog:
            return None
        if column.table:
            return column.table if column.table in self._ctes else None
        if from_name in self._ctes:
            return from_name
        return None

    def _physical_from(self, from_name: str | None) -> str | None:
        """Return *from_name* only when it is a physical (non-CTE) relation."""
        return None if from_name in self._ctes else from_name

    # ------------------------------------------------------------------
    # CTE column resolution
    # ------------------------------------------------------------------

    def _resolve_cte_column(
        self, cte_name: str, column_name: str, stack: set[tuple[str, str]]
    ) -> _Resolution:
        """Resolve one CTE output column by its declared position.

        Positional mapping is essential for the contrast query: swapping two
        recursive projections changes the value of a declared column even
        though the aliases written on those projections stay with their
        expressions.
        """
        info = self._ctes[cte_name]
        if column_name not in info.output_names:
            return _Resolution(unresolved=[f"{info.display_name}.{column_name}"])

        key = (cte_name, column_name)
        if key in stack:
            return _Resolution(cycles=[f"{info.display_name}.{column_name}"])

        stack.add(key)
        try:
            position = info.output_names.index(column_name)
            resolution = _Resolution()

            anchor = self._projection_at(info.anchor, position)
            if anchor is not None:
                resolution.merge(
                    self._resolve_branch_expression(
                        anchor,
                        cte_name,
                        stack,
                        self._from_physical_name(info.anchor),
                    )
                )

            for branch in info.recursive:
                projection = self._projection_at(branch, position)
                if projection is None:
                    resolution.unresolved.append(f"{info.display_name}.{column_name}")
                    continue
                resolution.merge(
                    self._resolve_branch_expression(
                        projection,
                        cte_name,
                        stack,
                        self._from_physical_name(branch),
                    )
                )
                for condition in self._branch_conditions(branch):
                    resolution.merge(condition)

            return resolution
        finally:
            stack.discard(key)

    @staticmethod
    def _projection_at(select: exp.Expression, position: int) -> exp.Expression | None:
        """Return the *position*-th SELECT projection, or ``None``."""
        expressions = _projection_expressions(select)
        if position < len(expressions):
            return expressions[position]
        return None

    def _resolve_branch_expression(
        self,
        expression: exp.Expression,
        current_cte: str,
        stack: set[tuple[str, str]],
        branch_from: str | None = None,
    ) -> _Resolution:
        """Resolve an expression inside one CTE branch."""
        value = expression.this if isinstance(expression, exp.Alias) else expression
        resolution = _Resolution()
        for column in _iter_columns(value):
            resolution.merge(
                self._resolve_branch_column(column, current_cte, stack, branch_from)
            )
        if value.find(exp.Star):
            resolution.unresolved.append("*")
        return resolution

    def _resolve_branch_column(
        self,
        column: exp.Column,
        current_cte: str,
        stack: set[tuple[str, str]],
        branch_from: str | None = None,
    ) -> _Resolution:
        """Resolve a column inside a CTE branch.

        A bare reference to *current_cte* follows the previous iteration's
        output.  A schema-qualified relation or any other table stays a
        physical column.  An unqualified column is attributed to the
        branch's own FROM relation.
        """
        if not column.db and not column.catalog and column.table:
            referenced = self._ctes.get(column.table)
            if referenced is not None:
                resolution = self._resolve_cte_column(column.table, column.name, stack)
                if column.table == current_cte:
                    reference = f"{referenced.display_name}.{column.name}"
                    resolution.recursive = _unique([*resolution.recursive, reference])
                    resolution.recursive_hops.append(
                        {
                            "reference": reference,
                            "column": column.name,
                            "iteration": "previous",
                        }
                    )
                return resolution

        return self._resolve_physical_column(column, branch_from)

    def _branch_conditions(self, branch: exp.Expression) -> Iterator[_Resolution]:
        """Yield physical condition columns for a CTE branch.

        Only physical columns are reported.  A bare recursive reference in a
        join predicate is part of the recursion path, not a physical condition.
        """
        branch_from = self._from_physical_name(branch)
        for condition in _condition_expressions(branch):
            for column in _iter_columns(condition):
                if not column.db and not column.catalog and column.table in self._ctes:
                    continue
                yield _Resolution(conditions=[self._physical_name(column, branch_from)])

    # ------------------------------------------------------------------
    # Physical columns
    # ------------------------------------------------------------------

    def _resolve_physical_column(
        self,
        column: exp.Column,
        from_physical: str | None = None,
    ) -> _Resolution:
        """Return the physical name for *column*."""
        return _Resolution(sources=[self._physical_name(column, from_physical)])

    def _physical_name(
        self,
        column: exp.Column,
        from_physical: str | None = None,
    ) -> str:
        """Build the physical dotted name for a column reference."""
        if column.table:
            table = self._table_aliases.get(column.table, column.table)
            prefix = [column.catalog, column.db, table]
            return ".".join([part for part in prefix if part] + [column.name])
        if from_physical:
            return f"{from_physical}.{column.name}"
        return column.name

    # ------------------------------------------------------------------
    # Result conversion
    # ------------------------------------------------------------------

    @staticmethod
    def _to_public(resolution: _Resolution) -> ColumnSources:
        """Convert an internal accumulator to the public result type."""
        return ColumnSources(
            resolution.sources,
            conditions=resolution.conditions,
            recursive=resolution.recursive,
            recursive_hops=resolution.recursive_hops,
            cycles=resolution.cycles,
            unresolved=resolution.unresolved,
        )
