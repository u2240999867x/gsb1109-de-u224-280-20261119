"""Ordered output catalog for star projections with EXCEPT/REPLACE.

DuckDB (and BigQuery) allow ``*`` / ``table.*`` projections to carry
``EXCEPT`` (``EXCLUDE``) and ``REPLACE`` modifiers::

    SELECT a.* EXCEPT (trace) REPLACE (amount * 2 AS amount), b.*
    FROM a JOIN b ON a.id = b.id

This module expands such projections into the ordered list of output
bindings a consumer would actually receive, using caller-supplied table
schemas (physical column order) and recursively expanded subqueries.

Each binding is a :class:`CatalogColumn` carrying:

* ``name`` — the output column name (duplicate names get DuckDB-style
  ``_1`` suffixes, e.g. ``amount`` / ``amount_1``);
* ``sources`` — physical columns the binding traces back to, so a
  ``REPLACE`` chain through nested queries keeps its lineage;
* ``known`` — ``False`` for wildcard segments whose expansion cannot be
  confirmed because the underlying table schema was not supplied.

Bindings positioned after an unknown segment are also reported as
unknown: a missing wildcard can shift or rename everything that follows.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlglot import exp


@dataclass(frozen=True)
class CatalogColumn:
    """One entry of the expanded output catalog.

    :param name: Output column name as produced by the query
        (``amount_1`` for a duplicate binding, ``b.*`` for an unknown
        wildcard segment).
    :param sources: Physical columns (``table.column`` strings) this
        binding traces back to. Empty for literals and unknown segments.
    :param known: Whether the name and position can be confirmed from
        the SQL and the supplied schemas.
    """

    name: str
    sources: tuple[str, ...] = ()
    known: bool = True

    @classmethod
    def unknown(cls, marker: str) -> "CatalogColumn":
        """Build an unknown wildcard segment labelled with *marker*.

        :param marker: Text describing the unresolved wildcard,
            e.g. ``"b.*"``.
        :returns: An unresolvable catalog entry.
        """
        return cls(name=marker, sources=(), known=False)


class StarExpander:
    """Expand a SELECT's star projections into an ordered catalog.

    :param ast: Root sqlglot AST node containing the SELECT to expand.
    :param table_schemas: Ordered physical columns per table, keyed by
        table name or alias, e.g. ``{"a": ["id", "trace", "amount"]}``.
        Tables missing from this mapping produce unknown segments.
    :param table_aliases: Table alias → real table name mapping, as
        produced by :class:`~table_extractor.TableExtractor`.
    """

    def __init__(
        self,
        ast: exp.Expression,
        table_schemas: dict[str, list[str]] | None = None,
        table_aliases: dict[str, str] | None = None,
    ) -> None:
        self._ast = ast
        self._schemas = table_schemas or {}
        self._table_aliases = table_aliases or {}
        self._ctes = {cte.alias: cte for cte in ast.find_all(exp.CTE)}

    def expand(self) -> list[CatalogColumn]:
        """Return the ordered, deduplicated output catalog.

        Entries following an unknown wildcard segment are flagged
        ``known=False`` because the missing segment could shift or
        rename them.

        :returns: Output bindings in SELECT order.
        """
        select = self._root_select()
        if select is None:
            return []
        return self._shadow_unknown(self._expand_select(select))

    def _root_select(self) -> exp.Select | None:
        if isinstance(self._ast, exp.Select):
            return self._ast
        return self._ast.find(exp.Select)

    # ------------------------------------------------------------------
    # SELECT-level expansion
    # ------------------------------------------------------------------

    def _expand_select(self, select: exp.Select) -> list[CatalogColumn]:
        relations = self._relations(select)
        entries: list[CatalogColumn] = []
        for projection in select.expressions:
            entries.extend(self._expand_projection(projection, relations))
        return self._dedupe_names(entries)

    def _relations(self, select: exp.Select) -> dict[str, list[CatalogColumn]]:
        relations: dict[str, list[CatalogColumn]] = {}
        from_node = select.args.get("from_")
        if from_node is not None:
            self._add_relation(from_node.this, relations)
        for join in select.args.get("joins") or []:
            self._add_relation(join.this, relations)
        return relations

    def _add_relation(
        self,
        node: exp.Expression,
        relations: dict[str, list[CatalogColumn]],
    ) -> None:
        if isinstance(node, exp.Subquery):
            key = node.alias or f"subquery_{len(relations) + 1}"
            relations[key] = self._nested_relation(node.this, key)
            return
        if isinstance(node, exp.Table):
            key = node.alias or node.name
            if node.name in self._ctes:
                relations[key] = self._nested_relation(self._ctes[node.name].this, key)
                return
            relations[key] = self._table_columns(key, node.name)
            return
        # Other relation shapes (e.g. FROM (VALUES ...) AS v) carry no
        # inspectable column list — record an unknown wildcard segment.
        key = getattr(node, "alias", "") or f"subquery_{len(relations) + 1}"
        relations[key] = [CatalogColumn.unknown(f"{key}.*")]

    def _nested_relation(self, body: exp.Expression, key: str) -> list[CatalogColumn]:
        if isinstance(body, exp.Select):
            return self._expand_select(body)
        if isinstance(body, (exp.Union, exp.Intersect, exp.Except)):
            first_select = body.find(exp.Select)
            if first_select is not None:
                return self._expand_select(first_select)
        return [CatalogColumn.unknown(f"{key}.*")]

    def _table_columns(self, key: str, table_name: str) -> list[CatalogColumn]:
        real_name = self._table_aliases.get(key, table_name)
        ordered = (
            self._schemas.get(key)
            or self._schemas.get(real_name)
            or self._schemas.get(table_name)
        )
        if ordered is None:
            return [CatalogColumn.unknown(f"{key}.*")]
        return [
            CatalogColumn(name=column, sources=(f"{real_name}.{column}",))
            for column in ordered
        ]

    # ------------------------------------------------------------------
    # Projection dispatch
    # ------------------------------------------------------------------

    def _expand_projection(
        self,
        projection: exp.Expression,
        relations: dict[str, list[CatalogColumn]],
    ) -> list[CatalogColumn]:
        if isinstance(projection, exp.Alias):
            return [self._alias_column(projection, relations)]
        if isinstance(projection, exp.Column) and isinstance(projection.this, exp.Star):
            return self._expand_star(projection, projection.table, relations)
        if isinstance(projection, exp.Star):
            return self._expand_star(projection, None, relations)
        if isinstance(projection, exp.Column):
            return [CatalogColumn(projection.name, (self._qualified_name(projection),))]
        sources = tuple(
            self._qualified_name(column) for column in projection.find_all(exp.Column)
        )
        return [CatalogColumn(projection.sql(), sources)]

    def _alias_column(
        self,
        alias_node: exp.Alias,
        relations: dict[str, list[CatalogColumn]],
    ) -> CatalogColumn:
        sources = tuple(
            self._resolve_bare(column, relations, None)
            for column in alias_node.this.find_all(exp.Column)
        )
        return CatalogColumn(alias_node.alias, sources)

    # ------------------------------------------------------------------
    # Star / EXCEPT / REPLACE handling
    # ------------------------------------------------------------------

    def _expand_star(
        self,
        node: exp.Expression,
        qualifier: str | None,
        relations: dict[str, list[CatalogColumn]],
    ) -> list[CatalogColumn]:
        star = node.this if isinstance(node, exp.Column) else node
        assert isinstance(star, exp.Star)
        base = self._star_base(qualifier, relations)
        self._apply_except(star, base)
        for replacement in star.args.get("replace") or []:
            self._apply_replacement(replacement, base, relations, qualifier)
        return base

    def _star_base(
        self,
        qualifier: str | None,
        relations: dict[str, list[CatalogColumn]],
    ) -> list[CatalogColumn]:
        if qualifier:
            return list(
                relations.get(qualifier, [CatalogColumn.unknown(f"{qualifier}.*")])
            )
        return [entry for entries in relations.values() for entry in entries]

    @staticmethod
    def _apply_except(star: exp.Star, base: list[CatalogColumn]) -> None:
        except_names = {column.name for column in star.args.get("except_") or []}
        if not except_names:
            return
        base[:] = [
            entry for entry in base if not (entry.known and entry.name in except_names)
        ]

    def _apply_replacement(
        self,
        replacement: exp.Alias,
        base: list[CatalogColumn],
        relations: dict[str, list[CatalogColumn]],
        qualifier: str | None,
    ) -> None:
        target = replacement.alias
        sources = tuple(
            self._resolve_bare(column, relations, qualifier)
            for column in replacement.this.find_all(exp.Column)
        )
        for index, entry in enumerate(base):
            if entry.known and entry.name == target:
                base[index] = CatalogColumn(entry.name, sources)
                return

    # ------------------------------------------------------------------
    # Name resolution
    # ------------------------------------------------------------------

    def _resolve_bare(
        self,
        column: exp.Column,
        relations: dict[str, list[CatalogColumn]],
        star_qualifier: str | None,
    ) -> str:
        if column.table:
            return self._qualified_name(column)
        if star_qualifier:
            real = self._table_aliases.get(star_qualifier, star_qualifier)
            return f"{real}.{column.name}"
        for entries in relations.values():
            for entry in entries:
                if entry.known and entry.name == column.name and entry.sources:
                    return entry.sources[0]
        return column.name

    def _qualified_name(self, column: exp.Column) -> str:
        if not column.table:
            return column.name
        table = self._table_aliases.get(column.table, column.table)
        return f"{table}.{column.name}"

    # ------------------------------------------------------------------
    # Finalisation
    # ------------------------------------------------------------------

    @staticmethod
    def _dedupe_names(
        entries: list[CatalogColumn],
    ) -> list[CatalogColumn]:
        counts: dict[str, int] = {}
        result: list[CatalogColumn] = []
        for entry in entries:
            if not entry.known:
                result.append(entry)
                continue
            occurrence = counts.get(entry.name, 0)
            counts[entry.name] = occurrence + 1
            name = entry.name if occurrence == 0 else f"{entry.name}_{occurrence}"
            result.append(CatalogColumn(name, entry.sources, entry.known))
        return result

    @staticmethod
    def _shadow_unknown(
        entries: list[CatalogColumn],
    ) -> list[CatalogColumn]:
        result: list[CatalogColumn] = []
        unknown_seen = False
        for entry in entries:
            if not entry.known:
                unknown_seen = True
                result.append(entry)
            else:
                result.append(
                    CatalogColumn(entry.name, entry.sources, known=not unknown_seen)
                )
        return result
