"""Scope-aware column attribution for nested SQL.

The parser's flat ``columns``/``tables`` properties are useful for
inventory-style metadata, but they intentionally collapse several SQL
concepts:

* two query blocks may each define a CTE named ``sales``;
* a LATERAL subquery may correlate with the enclosing query;
* a relation alias in an inner scope may or may not expose a column;
* a physical table definition may be unknown.

:class:`ColumnAttributionReport` keeps those distinctions.  It is built by
:class:`ColumnAttributionAnalyzer` from the sqlglot AST, never from the
parser's already-flattened lists, so accessing ``tables_aliases`` before or
after ``columns`` cannot change the result.

Resolution follows SQL name lookup rules:

* a qualified reference (``s.code``) walks the relation aliases from the
  innermost scope outwards.  A relation whose known output does not contain
  the column is skipped, which mirrors DuckDB's fallback for the
  reconciliation query used by the regression tests;
* an unqualified reference searches the current scope's relations first and
  only falls back to a correlated outer scope when no local relation can
  supply the column;
* every unresolved reference produces scenarios, and each scenario chooses
  one binding for the whole reference.  All occurrences that share a name in
  one scope share the choice, so an output alias and the predicate on its
  left-hand side cannot report two different bindings.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from itertools import product
from typing import Any, Iterator, Mapping, Sequence

from sqlglot import exp

from sql_metadata.utils import last_segment

# ---------------------------------------------------------------------------
# Public report model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceColumn:
    """A leaf column that can supply a value.

    ``table`` is the physical relation when it is known.  ``path`` records the
    relation chain followed by the analyzer, e.g.
    ``("sales", "archive_orders")``.  An unresolved source keeps ``table`` as
    the relation name that was actually written in the query.
    """

    table: str | None
    column: str
    path: tuple[str, ...] = ()

    @property
    def qualified(self) -> str:
        """Return ``table.column`` when a table is known, else ``column``."""
        return f"{self.table}.{self.column}" if self.table else self.column

    def as_dict(self) -> dict[str, Any]:
        return {
            "table": self.table,
            "column": self.column,
            "qualified": self.qualified,
            "path": list(self.path),
        }


@dataclass(frozen=True)
class Binding:
    """The relation/column a single textual reference resolves to."""

    reference: str
    clause: str
    scope: str
    qualifier: str | None
    relation: str
    relation_kind: str
    column: str
    sources: tuple[SourceColumn, ...] = ()
    fallback: bool = False
    shadowed: tuple[str, ...] = ()
    assumed: bool = False

    @property
    def source(self) -> SourceColumn | None:
        """Return the first leaf source, if any."""
        return self.sources[0] if self.sources else None

    @property
    def source_table(self) -> str | None:
        """Return the physical table of the first leaf source."""
        return self.source.table if self.source else None

    @property
    def source_column(self) -> str | None:
        """Return the column name of the first leaf source."""
        return self.source.column if self.source else None

    @property
    def source_name(self) -> str | None:
        """Return the qualified leaf source name."""
        return self.source.qualified if self.source else None

    @property
    def binding_point(self) -> str:
        """A compact identifier for this relation binding."""
        return f"{self.scope}:{self.relation}.{self.column}"

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "clause": self.clause,
            "scope": self.scope,
            "qualifier": self.qualifier,
            "relation": self.relation,
            "relation_kind": self.relation_kind,
            "column": self.column,
            "sources": [source.as_dict() for source in self.sources],
            "source": self.source_name,
            "fallback": self.fallback,
            "shadowed": list(self.shadowed),
            "assumed": self.assumed,
            "binding_point": self.binding_point,
        }


@dataclass(frozen=True)
class ReferenceResolution:
    """Resolution of one column occurrence."""

    reference: str
    clause: str
    scope: str
    binding: Binding | None
    candidates: tuple[Binding, ...] = ()
    ambiguous: bool = False

    @property
    def resolved(self) -> bool:
        """Whether a binding was selected."""
        return self.binding is not None

    @property
    def source(self) -> SourceColumn | None:
        """Return the selected binding's first leaf source."""
        return self.binding.source if self.binding else None

    def as_dict(self) -> dict[str, Any]:
        return {
            "reference": self.reference,
            "clause": self.clause,
            "scope": self.scope,
            "binding": self.binding.as_dict() if self.binding else None,
            "candidates": [candidate.as_dict() for candidate in self.candidates],
            "ambiguous": self.ambiguous,
            "resolved": self.resolved,
        }


@dataclass(frozen=True)
class PredicateAttribution:
    """A comparison whose operands resolve to concrete columns."""

    expression: str
    clause: str
    scope: str
    operator: str
    left: Binding | None
    right: Binding | None

    def binding_for(self, side: str) -> Binding | None:
        """Return the left or right binding by side name."""
        return self.left if side == "left" else self.right

    def as_dict(self) -> dict[str, Any]:
        return {
            "expression": self.expression,
            "clause": self.clause,
            "scope": self.scope,
            "operator": self.operator,
            "left": self.left.as_dict() if self.left else None,
            "right": self.right.as_dict() if self.right else None,
        }


@dataclass(frozen=True)
class OutputAttribution:
    """Attribution of one projected output inside one scenario."""

    name: str
    scope: str
    expression: str
    sources: tuple[SourceColumn, ...] = ()
    bindings: tuple[Binding, ...] = ()
    conditions: tuple[PredicateAttribution, ...] = ()

    @property
    def source(self) -> SourceColumn | None:
        """Return the single leaf source, if exactly one exists."""
        unique: list[SourceColumn] = []
        for source in self.sources:
            if source not in unique:
                unique.append(source)
        return unique[0] if len(unique) == 1 else None

    @property
    def value_sources(self) -> tuple[str, ...]:
        """Return the qualified names of all leaf sources."""
        values: list[str] = []
        for source in self.sources:
            if source.qualified not in values:
                values.append(source.qualified)
        return tuple(values)

    def filter_sources(self, side: str = "left") -> tuple[SourceColumn, ...]:
        """Return the leaf sources of the filter operands."""
        result: list[SourceColumn] = []
        for predicate in self.conditions:
            binding = predicate.binding_for(side)
            if binding:
                for source in binding.sources:
                    if source not in result:
                        result.append(source)
        return tuple(result)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scope": self.scope,
            "expression": self.expression,
            "sources": [source.as_dict() for source in self.sources],
            "value_sources": list(self.value_sources),
            "bindings": [binding.as_dict() for binding in self.bindings],
            "conditions": [predicate.as_dict() for predicate in self.conditions],
        }


@dataclass(frozen=True)
class AttributionScenario:
    """One consistent set of schema assumptions and reference bindings."""

    id: str
    description: str
    assumptions: dict[str, bool]
    references: tuple[ReferenceResolution, ...]
    predicates: tuple[PredicateAttribution, ...]
    outputs: dict[str, OutputAttribution]

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "description": self.description,
            "assumptions": dict(self.assumptions),
            "references": [reference.as_dict() for reference in self.references],
            "predicates": [predicate.as_dict() for predicate in self.predicates],
            "outputs": {
                name: output.as_dict() for name, output in self.outputs.items()
            },
        }


@dataclass(frozen=True)
class OutputReport:
    """A root output aggregated across all scenarios."""

    name: str
    by_scenario: dict[str, OutputAttribution]

    @property
    def value_sources(self) -> tuple[str, ...]:
        """Return every ``table.column`` source seen in any scenario."""
        values: list[str] = []
        for output in self.by_scenario.values():
            for source in output.sources:
                if source.qualified not in values:
                    values.append(source.qualified)
        return tuple(values)

    @property
    def sources(self) -> tuple[SourceColumn, ...]:
        """Return every distinct leaf source seen in any scenario."""
        values: list[SourceColumn] = []
        for output in self.by_scenario.values():
            for source in output.sources:
                if source not in values:
                    values.append(source)
        return tuple(values)

    @property
    def conditions(self) -> tuple[PredicateAttribution, ...]:
        """Return every distinct condition seen in any scenario."""
        seen: list[str] = []
        result: list[PredicateAttribution] = []
        for output in self.by_scenario.values():
            for predicate in output.conditions:
                if predicate.expression not in seen:
                    seen.append(predicate.expression)
                    result.append(predicate)
        return tuple(result)

    def for_scenario(self, scenario_id: str) -> OutputAttribution | None:
        """Return this output's attribution in one scenario."""
        return self.by_scenario.get(scenario_id)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value_sources": list(self.value_sources),
            "conditions": [predicate.as_dict() for predicate in self.conditions],
            "by_scenario": {
                scenario_id: output.as_dict()
                for scenario_id, output in self.by_scenario.items()
            },
        }


@dataclass(frozen=True)
class AliasBinding:
    """One relation alias declared in one query scope."""

    scope: str
    alias: str
    relation: str
    kind: str
    columns: tuple[str, ...] | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "alias": self.alias,
            "relation": self.relation,
            "kind": self.kind,
            "columns": (
                list(self.columns) if self.columns is not None else None
            ),
        }


@dataclass(frozen=True)
class RelationInfo:
    """A relation visible in one query scope."""

    scope: str
    alias: str
    name: str
    kind: str
    columns: tuple[str, ...] | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "scope": self.scope,
            "alias": self.alias,
            "name": self.name,
            "kind": self.kind,
            "columns": list(self.columns) if self.columns is not None else None,
        }


@dataclass(frozen=True)
class CteInfo:
    """A CTE definition; duplicate names in distinct scopes are kept."""

    name: str
    scope: str
    body_scope: str
    columns: tuple[str, ...] | None
    sources: dict[str, tuple[str, ...]] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "scope": self.scope,
            "body_scope": self.body_scope,
            "columns": list(self.columns) if self.columns is not None else None,
            "sources": {
                column: list(values)
                for column, values in self.sources.items()
            },
        }


@dataclass(frozen=True)
class ScopeInfo:
    """A query block in the scope tree."""

    label: str
    parent: str | None
    can_correlate: bool

    def as_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "parent": self.parent,
            "can_correlate": self.can_correlate,
        }


@dataclass(frozen=True)
class ColumnAttributionReport:
    """Full scope-aware attribution report for one SQL statement.

    Reports are scenario based.  A schema that the query text does not
    establish yields several scenarios instead of a single mixed answer.
    :meth:`output` and :meth:`reference` accept either a full reference
    (``v.chosen_code``) or an unambiguous short name (``chosen_code``).
    """

    dialect: str
    tables: tuple[str, ...]
    aliases: tuple[AliasBinding, ...]
    relations: tuple[RelationInfo, ...]
    ctes: tuple[CteInfo, ...]
    scopes: tuple[ScopeInfo, ...]
    scenarios: tuple[AttributionScenario, ...]
    outputs: dict[str, OutputReport]
    root_scope: str = "outer"

    @property
    def used_dialect(self) -> str:
        """Alias for :attr:`dialect`."""
        return self.dialect

    @property
    def ambiguous(self) -> bool:
        """Whether any reference had more than one possible binding."""
        return any(
            reference.ambiguous
            for scenario in self.scenarios
            for reference in scenario.references
        )

    @property
    def binding_points(self) -> tuple[Binding, ...]:
        """Every distinct relation binding selected across scenarios.

        This is the compact answer to "which relation/column did the query
        actually bind to?".  Two textual references that resolve to the same
        relation and column are reported once.
        """
        result: list[Binding] = []
        seen: set[str] = set()
        for scenario in self.scenarios:
            for binding in _scenario_bindings(scenario):
                if binding.binding_point not in seen:
                    seen.add(binding.binding_point)
                    result.append(binding)
        return tuple(result)

    def scenario(self, scenario_id: str) -> AttributionScenario | None:
        """Return a scenario by id."""
        for scenario in self.scenarios:
            if scenario.id == scenario_id:
                return scenario
        return None

    def output(self, name: str) -> OutputReport | None:
        """Return an output by full or unambiguous short name.

        A short name that appears in several scopes is resolved to the root
        query when possible; otherwise a truly ambiguous name returns
        ``None`` so callers do not silently read the wrong query block.
        """
        if name in self.outputs:
            return self.outputs[name]
        matches = [
            output
            for key, output in self.outputs.items()
            if last_segment(key) == last_segment(name)
        ]
        if len(matches) > 1:
            root_matches = [
                output
                for output in matches
                if any(
                    attribution.scope == self.root_scope
                    for attribution in output.by_scenario.values()
                )
            ]
            if root_matches:
                matches = root_matches
        return matches[0] if len(matches) == 1 else None

    def sources_for(self, name: str) -> tuple[str, ...]:
        """Return every possible ``table.column`` source for an output."""
        output = self.output(name)
        return output.value_sources if output else ()

    def filters_for(self, name: str) -> tuple[PredicateAttribution, ...]:
        """Return the predicates attached to an output."""
        output = self.output(name)
        return output.conditions if output else ()

    def reference(self, reference: str) -> tuple[ReferenceResolution, ...]:
        """Return each scenario's resolution of a textual reference.

        A qualifier match is preferred over a bare-name match so
        ``s.code`` does not also return the unqualified ``code`` groups.
        """
        qualified = "." in reference
        results: list[ReferenceResolution] = []
        for scenario in self.scenarios:
            for item in scenario.references:
                if qualified:
                    if item.reference == reference:
                        results.append(item)
                elif last_segment(item.reference) == last_segment(reference):
                    results.append(item)
        return tuple(results)

    def bindings_for(self, reference: str) -> tuple[Binding, ...]:
        """Return the distinct bindings chosen for *reference*.

        Useful when a qualified alias is declared in more than one scope: the
        result names each real binding point (scope, relation, column) without
        collapsing them into one.
        """
        result: list[Binding] = []
        seen: set[str] = set()
        for resolution in self.reference(reference):
            binding = resolution.binding
            if binding is not None and binding.binding_point not in seen:
                seen.add(binding.binding_point)
                result.append(binding)
        return tuple(result)

    def aliases_for(self, alias: str) -> tuple[AliasBinding, ...]:
        """Return every declaration of *alias* across all query scopes."""
        return tuple(item for item in self.aliases if item.alias == alias)

    def __getitem__(self, name: str) -> OutputReport:
        """Allow ``report["v.chosen_code"]`` lookups."""
        output = self.output(name)
        if output is None:
            raise KeyError(name)
        return output

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation of the whole report."""
        return {
            "dialect": self.dialect,
            "tables": list(self.tables),
            "aliases": [alias.as_dict() for alias in self.aliases],
            "relations": [relation.as_dict() for relation in self.relations],
            "ctes": [cte.as_dict() for cte in self.ctes],
            "scopes": [scope.as_dict() for scope in self.scopes],
            "scenarios": [scenario.as_dict() for scenario in self.scenarios],
            "outputs": {
                name: output.as_dict() for name, output in self.outputs.items()
            },
            "binding_points": [
                binding.as_dict() for binding in self.binding_points
            ],
            "ambiguous": self.ambiguous,
        }


def _scenario_bindings(scenario: AttributionScenario) -> Iterator[Binding]:
    """Yield every binding that participates in a scenario."""
    for reference in scenario.references:
        if reference.binding:
            yield reference.binding
        yield from reference.candidates
    for predicate in scenario.predicates:
        if predicate.left:
            yield predicate.left
        if predicate.right:
            yield predicate.right
    for output in scenario.outputs.values():
        yield from output.bindings


# ---------------------------------------------------------------------------
# Internal model
# ---------------------------------------------------------------------------


_UNSET = object()


@dataclass
class _CteDef:
    """Internal CTE definition tied to its declaring and body scopes."""

    name: str
    cte: exp.CTE
    declaring_scope: _Scope
    body_scope: _Scope | None = None
    explicit_columns: tuple[str, ...] = ()

    @property
    def label(self) -> str:
        return self.declaring_scope.label


@dataclass
class _Relation:
    """Internal relation visible in one scope."""

    alias: str
    name: str
    kind: str
    scope: _Scope
    columns: tuple[str, ...] | None
    body_scope: _Scope | None = None
    cte: _CteDef | None = None
    table_name: str | None = None


@dataclass
class _OutputSpec:
    """A projection in one SELECT."""

    name: str
    expression: exp.Expression
    scope: _Scope

    @property
    def full_name(self) -> str:
        """Return the reference written in the query."""
        if isinstance(self.expression, exp.Column):
            return self.expression.sql()
        if isinstance(self.expression, exp.Alias):
            return self.expression.alias
        return self.expression.alias_or_name or self.expression.sql()


@dataclass
class _Scope:
    """Internal query block."""

    label: str
    select: exp.Expression
    parent: _Scope | None
    can_correlate: bool
    visible_ctes: dict[str, _CteDef] = field(default_factory=dict)
    relations: list[_Relation] = field(default_factory=list)
    output_specs: list[_OutputSpec] = field(default_factory=list)

    def relation_for(self, alias: str) -> list[_Relation]:
        return [rel for rel in self.relations if rel.alias == alias]


@dataclass(frozen=True)
class _Ref:
    """A textual column reference inside one scope."""

    node: exp.Column
    scope: _Scope
    clause: str

    @property
    def name(self) -> str:
        return self.node.name

    @property
    def qualifier(self) -> str | None:
        return self.node.table or None

    @property
    def text(self) -> str:
        return self.node.sql()

    @property
    def group_key(self) -> tuple[str, str, str]:
        if self.qualifier:
            return (self.scope.label, "q", f"{self.qualifier}.{self.name}")
        return (self.scope.label, "u", self.name)


@dataclass(frozen=True)
class _Predicate:
    """A comparison found in WHERE/ON/HAVING/QUALIFY."""

    node: exp.Expression
    scope: _Scope
    clause: str
    operator: str
    left: exp.Expression | None
    right: exp.Expression | None


@dataclass(frozen=True)
class _Candidate:
    """A possible binding for a reference group."""

    scope: _Scope
    relation: _Relation
    column: str
    assumed: bool = False
    fallback: bool = False
    shadowed: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str, str]:
        return (self.scope.label, self.relation.alias, self.column)

    @property
    def label(self) -> str:
        return f"{self.relation.alias}.{self.column}"


@dataclass
class _Group:
    """References that must share one binding choice within a scenario."""

    key: tuple[str, str, str]
    scope: _Scope
    name: str
    qualifier: str | None
    refs: list[_Ref] = field(default_factory=list)
    candidates: list[_Candidate] = field(default_factory=list)

    @property
    def ambiguous(self) -> bool:
        return len(self.candidates) > 1

    @property
    def label(self) -> str:
        """Return a scope-qualified label for this reference group.

        The scope prefix is kept so an unqualified ``code`` in one query
        block is never confused with a qualified ``s.code`` elsewhere.
        """
        name = f"{self.qualifier}.{self.name}" if self.qualifier else self.name
        return f"{self.scope.label}:{name}"


@dataclass(frozen=True)
class _Choice:
    """One group's chosen candidate in a scenario."""

    group: _Group
    candidate: _Candidate


# ---------------------------------------------------------------------------
# Definition normalisation
# ---------------------------------------------------------------------------


def _normalise_columns(value: Any) -> tuple[str, ...] | None:
    """Turn the accepted definition shapes into a column-name tuple.

    ``None`` means the relation exists but its schema is not known.  Empty
    sequences mean the relation is known to have no columns.
    """
    if value is None:
        return None
    if isinstance(value, str):
        return tuple(part.strip() for part in value.split(",") if part.strip())
    if isinstance(value, Mapping):
        for key in ("columns", "fields", "schema", "column_names"):
            if key in value:
                return _normalise_columns(value[key])
        return tuple(str(key) for key in value)
    if isinstance(value, Sequence) or isinstance(value, (set, frozenset)):
        return tuple(str(item) for item in value)
    return None


def _normalise_definitions(
    definitions: Mapping[str, Any] | None,
) -> dict[str, tuple[str, ...] | None]:
    """Normalise table-definition input into ``{name: columns|None}``."""
    result: dict[str, tuple[str, ...] | None] = {}
    if not definitions:
        return result
    for name, value in definitions.items():
        result[str(name)] = _normalise_columns(value)
    return result


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


class ColumnAttributionAnalyzer:
    """Build a :class:`ColumnAttributionReport` from a sqlglot AST.

    :param ast: Root AST node (usually ``exp.Select``).
    :param definitions: Mapping of table name to ordered columns.  A value
        of ``None`` marks a table whose schema is unknown.  A missing key is
        also unknown.
    :param dialect: Human-readable dialect name used in the report.
    :param tables: Physical table list to expose in the report (usually
        ``Parser.tables``).  When omitted the analyzer derives it from the
        AST.
    """

    def __init__(
        self,
        ast: exp.Expression,
        definitions: Mapping[str, Any] | None = None,
        dialect: str = "default",
        tables: Sequence[str] | None = None,
    ) -> None:
        self._ast = ast
        self._definitions = _normalise_definitions(definitions)
        self._dialect = dialect
        self._tables = tuple(tables) if tables is not None else None

        self._scopes: list[_Scope] = []
        self._label_counts: dict[str, int] = {}
        self._ctes: list[_CteDef] = []
        self._scope_stack: set[int] = set()
        self._source_cache: dict[tuple[int, str], list[SourceColumn]] = {}

        self._root = self._build_query(ast, None, "outer", {}, False)
        self._collect_references()
        self._groups = self._build_groups()
        self._predicates = self._collect_predicates()

    # -- public ---------------------------------------------------------

    def build(self) -> ColumnAttributionReport:
        """Analyse the AST and return the report."""
        scenarios = self._build_scenarios()
        root_outputs = self._build_output_reports(scenarios)
        return ColumnAttributionReport(
            dialect=self._dialect,
            tables=self._tables if self._tables is not None else self._derive_tables(),
            aliases=tuple(
                AliasBinding(
                    scope=rel.scope.label,
                    alias=rel.alias,
                    relation=rel.name,
                    kind=rel.kind,
                    columns=rel.columns,
                )
                for scope in self._scopes
                for rel in scope.relations
            ),
            relations=tuple(
                RelationInfo(
                    scope=scope.label,
                    alias=rel.alias,
                    name=rel.name,
                    kind=rel.kind,
                    columns=rel.columns,
                )
                for scope in self._scopes
                for rel in scope.relations
            ),
            ctes=tuple(self._cte_infos(scenarios)),
            scopes=tuple(
                ScopeInfo(
                    label=scope.label,
                    parent=scope.parent.label if scope.parent else None,
                    can_correlate=scope.can_correlate,
                )
                for scope in self._scopes
            ),
            scenarios=tuple(scenarios),
            outputs=root_outputs,
            root_scope=self._root.label,
        )

    # -- scope construction --------------------------------------------

    def _unique_label(self, base: str) -> str:
        count = self._label_counts.get(base, 0) + 1
        self._label_counts[base] = count
        return base if count == 1 else f"{base}#{count}"

    def _build_query(
        self,
        node: exp.Expression,
        parent: _Scope | None,
        label: str,
        visible_ctes: dict[str, _CteDef],
        can_correlate: bool,
    ) -> _Scope:
        if isinstance(node, exp.Select):
            return self._build_select(
                node, parent, label, visible_ctes, can_correlate
            )
        if isinstance(node, (exp.Union, exp.Intersect, exp.Except)):
            select = node.find(exp.Select)
            if select is not None:
                return self._build_select(
                    select, parent, label, visible_ctes, can_correlate
                )
        # Fall back to an empty scope for non-SELECT statements.
        scope = _Scope(
            label=self._unique_label(label),
            select=node,
            parent=parent,
            can_correlate=can_correlate,
            visible_ctes=dict(visible_ctes),
        )
        self._scopes.append(scope)
        return scope

    def _build_select(
        self,
        select: exp.Select,
        parent: _Scope | None,
        label: str,
        visible_ctes: dict[str, _CteDef],
        can_correlate: bool,
    ) -> _Scope:
        scope = _Scope(
            label=self._unique_label(label),
            select=select,
            parent=parent,
            can_correlate=can_correlate,
            visible_ctes=dict(visible_ctes),
        )
        self._scopes.append(scope)
        scope.visible_ctes = self._register_ctes(scope, select)
        self._register_relations(scope, select)
        for expression in select.expressions:
            scope.output_specs.append(
                _OutputSpec(
                    name=self._output_name(expression),
                    expression=expression,
                    scope=scope,
                )
            )
        return scope

    def _register_ctes(
        self, scope: _Scope, select: exp.Select
    ) -> dict[str, _CteDef]:
        """Collect the SELECT's WITH clause and build each CTE body scope."""
        env = dict(scope.visible_ctes)
        with_clause = select.args.get("with_")
        if not isinstance(with_clause, exp.With):
            return env
        local: list[_CteDef] = []
        for cte in with_clause.expressions:
            if not isinstance(cte, exp.CTE) or not cte.alias:
                continue
            alias_args = cte.args.get("alias")
            explicit = tuple(
                column.name for column in (alias_args.columns or [])
            )
            definition = _CteDef(
                name=cte.alias,
                cte=cte,
                declaring_scope=scope,
                explicit_columns=explicit,
            )
            env[cte.alias] = definition
            local.append(definition)
        for definition in local:
            self._build_cte_body(definition, scope, env)
        return env

    def _build_cte_body(
        self,
        definition: _CteDef,
        scope: _Scope,
        env: dict[str, _CteDef],
    ) -> None:
        marker = id(definition.cte)
        if marker in self._scope_stack:
            return
        self._scope_stack.add(marker)
        try:
            definition.body_scope = self._build_query(
                definition.cte.this,
                scope,
                f"cte:{definition.name}@{scope.label}",
                env,
                False,
            )
        finally:
            self._scope_stack.discard(marker)
        self._ctes.append(definition)

    def _register_relations(self, scope: _Scope, select: exp.Select) -> None:
        from_node = select.args.get("from_")
        if isinstance(from_node, exp.From) and from_node.this is not None:
            self._add_relation(scope, from_node.this)
        for join in select.args.get("joins") or []:
            if isinstance(join, exp.Join) and join.this is not None:
                self._add_relation(scope, join.this)

    def _output_name(self, expression: exp.Expression) -> str:
        if isinstance(expression, exp.Alias):
            return expression.alias
        if isinstance(expression, exp.Column):
            return expression.name
        if isinstance(expression, exp.Star):
            return "*"
        return expression.alias_or_name or expression.sql()

    def _add_relation(self, scope: _Scope, node: exp.Expression) -> None:
        if isinstance(node, exp.Lateral):
            self._add_lateral(scope, node)
            return
        if isinstance(node, exp.Subquery):
            alias = node.alias or self._unique_label("subquery")
            body = self._build_query(
                node.this, scope, alias, scope.visible_ctes, False
            )
            scope.relations.append(
                _Relation(
                    alias=alias,
                    name=alias,
                    kind="subquery",
                    scope=scope,
                    columns=None,
                    body_scope=body,
                )
            )
            return
        if isinstance(node, exp.Table):
            self._add_table(scope, node)
            return
        if isinstance(node, exp.Unnest):
            alias = node.alias or self._unique_label("unnest")
            scope.relations.append(
                _Relation(
                    alias=alias,
                    name=alias,
                    kind="unnest",
                    scope=scope,
                    columns=None,
                )
            )

    def _add_lateral(self, scope: _Scope, node: exp.Lateral) -> None:
        inner = node.this
        body: exp.Expression = inner
        inner_alias = ""
        if isinstance(inner, exp.Subquery):
            body = inner.this
            inner_alias = inner.alias
        alias = node.alias or inner_alias or self._unique_label("lateral")
        body_scope = self._build_query(
            body, scope, alias, scope.visible_ctes, True
        )
        scope.relations.append(
            _Relation(
                alias=alias,
                name=alias,
                kind="lateral",
                scope=scope,
                columns=None,
                body_scope=body_scope,
            )
        )

    def _add_table(self, scope: _Scope, node: exp.Table) -> None:
        name = self._table_name(node)
        alias = node.alias or node.name
        cte = None
        if not node.db and not node.catalog and node.name in scope.visible_ctes:
            cte = scope.visible_ctes[node.name]
            scope.relations.append(
                _Relation(
                    alias=alias,
                    name=node.name,
                    kind="cte",
                    scope=scope,
                    columns=None,
                    body_scope=cte.body_scope,
                    cte=cte,
                )
            )
            return
        columns = self._lookup_table_columns(name)
        if columns is _UNSET:
            columns = self._lookup_table_columns(node.name)
        scope.relations.append(
            _Relation(
                alias=alias,
                name=name,
                kind="table",
                scope=scope,
                columns=columns if columns is not _UNSET else None,
                table_name=name,
            )
        )

    @staticmethod
    def _table_name(node: exp.Table) -> str:
        parts = [node.catalog, node.db, node.name]
        return ".".join(part for part in parts if part)

    def _lookup_table_columns(self, name: str) -> tuple[str, ...] | None | object:
        if name in self._definitions:
            return self._definitions[name]
        bare = last_segment(name)
        if bare in self._definitions:
            return self._definitions[bare]
        return _UNSET

    # -- relation output schema ---------------------------------------

    def _relation_columns(self, relation: _Relation) -> tuple[str, ...] | None:
        if relation.kind == "table":
            return relation.columns
        if relation.cte is not None and relation.cte.explicit_columns:
            return relation.cte.explicit_columns
        if relation.body_scope is None:
            return relation.columns
        return self._scope_output_columns(relation.body_scope)

    def _scope_output_columns(self, scope: _Scope) -> tuple[str, ...] | None:
        names: list[str] = []
        for spec in scope.output_specs:
            if spec.name != "*":
                names.append(spec.name)
                continue
            star_names = self._star_output_names(spec)
            if star_names is None:
                return None
            names.extend(star_names)
        return tuple(names)

    def _star_output_names(self, spec: _OutputSpec) -> list[str] | None:
        if spec.expression.find(exp.Star) is None:
            return None
        table = (
            spec.expression.table
            if isinstance(spec.expression, exp.Column)
            else ""
        )
        if table:
            candidates = [
                relation
                for relation in spec.scope.relations
                if relation.alias == table
            ]
        else:
            candidates = list(spec.scope.relations)
        names: list[str] = []
        for relation in candidates:
            columns = self._relation_columns(relation)
            if columns is None:
                return None
            names.extend(columns)
        return names if candidates else None

    # -- reference and predicate collection ---------------------------

    def _collect_references(self) -> None:
        self._references: list[_Ref] = []
        for scope in self._scopes:
            if not isinstance(scope.select, exp.Select):
                continue
            for expression, clause in self._reference_roots(scope.select):
                self._walk_expression(expression, scope, clause)

    def _reference_roots(
        self, select: exp.Select
    ) -> Iterator[tuple[exp.Expression, str]]:
        """Yield every expression root that may contain column references."""
        for expression in select.expressions:
            yield expression, "select"
        for key, clause in (
            ("where", "where"),
            ("group", "group_by"),
            ("having", "having"),
            ("order", "order_by"),
            ("qualify", "qualify"),
        ):
            child = select.args.get(key)
            if child is not None:
                yield child, clause
        for join in select.args.get("joins") or []:
            condition = join.args.get("on")
            if condition is not None:
                yield condition, "join"

    def _walk_expression(
        self, node: exp.Expression, scope: _Scope, clause: str
    ) -> None:
        if isinstance(node, exp.Column):
            self._references.append(_Ref(node=node, scope=scope, clause=clause))
            return
        if isinstance(node, (exp.Select, exp.Subquery, exp.CTE)):
            return
        for child in node.iter_expressions():
            self._walk_expression(child, scope, clause)

    def _collect_predicates(self) -> list[_Predicate]:
        predicates: list[_Predicate] = []
        comparison_types = (
            exp.EQ,
            exp.NEQ,
            exp.GT,
            exp.GTE,
            exp.LT,
            exp.LTE,
            exp.Is,
            exp.Like,
        )
        for scope in self._scopes:
            if not isinstance(scope.select, exp.Select):
                continue
            for expression, clause in self._predicate_roots(scope.select):
                for node in self._comparison_nodes(expression, comparison_types):
                    predicate = self._make_predicate(node, scope, clause)
                    if predicate is not None:
                        predicates.append(predicate)
        return predicates

    def _predicate_roots(
        self, select: exp.Select
    ) -> Iterator[tuple[exp.Expression, str]]:
        """Yield expression roots that can hold a comparison predicate."""
        for key in ("where", "having", "qualify"):
            child = select.args.get(key)
            if child is not None:
                yield child.this, key
        for join in select.args.get("joins") or []:
            condition = join.args.get("on")
            if condition is not None:
                yield condition, "join"

    def _make_predicate(
        self, node: exp.Expression, scope: _Scope, clause: str
    ) -> _Predicate | None:
        left = node.this
        right = node.expression
        if not (self._single_column(left) or self._single_column(right)):
            return None
        return _Predicate(
            node=node,
            scope=scope,
            clause=clause,
            operator=type(node).__name__.upper(),
            left=left,
            right=right,
        )

    def _comparison_nodes(
        self,
        node: exp.Expression,
        types: tuple[type[exp.Expression], ...],
    ) -> Iterator[exp.Expression]:
        if isinstance(node, types):
            yield node
            return
        if isinstance(node, (exp.And, exp.Or, exp.Paren)):
            for child in node.iter_expressions():
                yield from self._comparison_nodes(child, types)
            return
        if isinstance(node, exp.Not):
            yield from self._comparison_nodes(node.this, types)

    def _single_column(self, node: exp.Expression) -> exp.Column | None:
        if isinstance(node, exp.Column):
            return node
        columns = [
            item for item in node.find_all(exp.Column) if item.find(exp.Select) is None
        ]
        return columns[0] if len(columns) == 1 else None

    # -- group construction -------------------------------------------

    def _build_groups(self) -> list[_Group]:
        groups: dict[tuple[str, str, str], _Group] = {}
        order: list[tuple[str, str, str]] = []
        for ref in self._references:
            key = ref.group_key
            if key not in groups:
                groups[key] = _Group(
                    key=key,
                    scope=ref.scope,
                    name=ref.name,
                    qualifier=ref.qualifier,
                )
                order.append(key)
            groups[key].refs.append(ref)
        result = [groups[key] for key in order]
        for group in result:
            group.candidates = self._candidates_for(group)
        return result

    def _candidates_for(self, group: _Group) -> list[_Candidate]:
        if group.qualifier:
            return self._qualified_candidates(group)
        return self._unqualified_candidates(group.scope, group.name)

    def _qualified_candidates(self, group: _Group) -> list[_Candidate]:
        qualifier = group.qualifier or ""
        candidates: list[_Candidate] = []
        shadowed: list[str] = []
        scope: _Scope | None = group.scope
        first = True
        while scope is not None:
            relations = scope.relation_for(qualifier)
            layer, exposes = self._qualified_layer(
                scope, relations, qualifier, group.name, tuple(shadowed), first
            )
            candidates.extend(layer)
            if exposes:
                return candidates
            if relations:
                shadowed.append(f"{scope.label}:{qualifier}")
            if not scope.can_correlate:
                break
            scope = scope.parent
            first = False
        if candidates:
            return candidates
        implicit = self._lookup_table_columns(qualifier)
        if implicit is _UNSET:
            return []
        relation = _Relation(
            alias=qualifier,
            name=qualifier,
            kind="table",
            scope=group.scope,
            columns=implicit,
        )
        return [
            _Candidate(
                scope=group.scope,
                relation=relation,
                column=group.name,
                assumed=False,
            )
        ]

    def _qualified_layer(
        self,
        scope: _Scope,
        relations: list[_Relation],
        qualifier: str,
        name: str,
        shadowed: tuple[str, ...],
        first: bool,
    ) -> tuple[list[_Candidate], bool]:
        """Resolve one scope's relations for a qualified reference.

        :returns: ``(candidates, exposes)`` where *exposes* is true when a
            known relation in this scope actually has the column.
        """
        layer: list[_Candidate] = []
        exposes = False
        for relation in relations:
            columns = self._relation_columns(relation)
            if columns is None:
                layer.append(
                    _Candidate(
                        scope=scope,
                        relation=relation,
                        column=name,
                        assumed=True,
                        fallback=not first,
                        shadowed=shadowed,
                    )
                )
            elif name in columns:
                layer.append(
                    _Candidate(
                        scope=scope,
                        relation=relation,
                        column=name,
                        assumed=False,
                        fallback=not first,
                        shadowed=shadowed,
                    )
                )
                exposes = True
        return layer, exposes

    def _unqualified_candidates(
        self, scope: _Scope, name: str
    ) -> list[_Candidate]:
        candidates: list[_Candidate] = []
        known = False
        for relation in scope.relations:
            columns = self._relation_columns(relation)
            if columns is None:
                candidates.append(
                    _Candidate(
                        scope=scope,
                        relation=relation,
                        column=name,
                        assumed=True,
                    )
                )
            elif name in columns:
                candidates.append(
                    _Candidate(
                        scope=scope,
                        relation=relation,
                        column=name,
                        assumed=False,
                    )
                )
                known = True
        if known:
            return candidates
        if scope.can_correlate and scope.parent is not None:
            outer = self._unqualified_candidates(scope.parent, name)
            for candidate in outer:
                candidates.append(
                    _Candidate(
                        scope=candidate.scope,
                        relation=candidate.relation,
                        column=candidate.column,
                        assumed=candidate.assumed,
                        fallback=True,
                        shadowed=candidate.shadowed,
                    )
                )
        return candidates

    # -- scenario construction ----------------------------------------

    def _build_scenarios(self) -> list[AttributionScenario]:
        uncertain = [group for group in self._groups if len(group.candidates) > 1]
        fixed = [group for group in self._groups if len(group.candidates) == 1]
        if not uncertain:
            choices = self._fixed_choices(fixed)
            return [
                self._make_scenario(
                    "resolved", "single consistent binding", choices
                )
            ]

        scenarios: list[AttributionScenario] = []
        all_options = [group.candidates for group in uncertain]
        for index, combination in enumerate(product(*all_options)):
            choices = self._fixed_choices(fixed)
            choices.extend(
                _Choice(group=group, candidate=candidate)
                for group, candidate in zip(uncertain, combination)
            )
            scenario_id = self._scenario_id(choices)
            description = ", ".join(
                f"{choice.group.label} -> {choice.candidate.label}"
                for choice in choices
                if choice.group in uncertain
            )
            scenarios.append(
                self._make_scenario(
                    scenario_id or f"option_{index + 1}",
                    description or "single consistent binding",
                    choices,
                )
            )
        return scenarios

    @staticmethod
    def _fixed_choices(groups: list[_Group]) -> list[_Choice]:
        return [
            _Choice(group=group, candidate=group.candidates[0])
            for group in groups
            if group.candidates
        ]

    @staticmethod
    def _scenario_id(choices: list[_Choice]) -> str:
        """Return a compact id naming only the choices that are uncertain."""
        parts = [
            f"{choice.group.label}={choice.candidate.label}"
            for choice in choices
            if choice.group.ambiguous
        ]
        if not parts:
            return "resolved"
        return ";".join(parts)

    def _choice_map(
        self, choices: list[_Choice]
    ) -> dict[tuple[str, str, str], _Choice]:
        return {choice.group.key: choice for choice in choices}

    def _make_scenario(
        self, scenario_id: str, description: str, choices: list[_Choice]
    ) -> AttributionScenario:
        chosen = self._choice_map(choices)
        assumptions: dict[str, bool] = {}
        # Leaf tracing depends on the chosen bindings, so each scenario gets
        # a clean cache.  Sharing it would let one scenario's resolution leak
        # into another (for example "value from outer, filter from inner").
        self._source_cache = {}

        references = self._scenario_references(chosen, assumptions)
        predicates = self._scenario_predicates(chosen, assumptions)
        outputs = self._scenario_outputs(chosen, assumptions, predicates)
        return AttributionScenario(
            id=scenario_id,
            description=description,
            assumptions=assumptions,
            references=references,
            predicates=predicates,
            outputs=outputs,
        )

    def _scenario_references(
        self,
        chosen: dict[tuple[str, str, str], _Choice],
        assumptions: dict[str, bool],
    ) -> tuple[ReferenceResolution, ...]:
        references: list[ReferenceResolution] = []
        for group in self._groups:
            choice = chosen.get(group.key)
            candidates = tuple(
                self._binding_for_candidate(
                    group, candidate, chosen, assumptions
                )
                for candidate in group.candidates
            )
            binding = (
                self._binding_for_candidate(
                    group, choice.candidate, chosen, assumptions
                )
                if choice is not None
                else None
            )
            for ref in group.refs:
                references.append(
                    ReferenceResolution(
                        reference=ref.text,
                        clause=ref.clause,
                        scope=ref.scope.label,
                        binding=binding,
                        candidates=candidates,
                        ambiguous=len(group.candidates) > 1,
                    )
                )
        return tuple(references)

    def _scenario_predicates(
        self,
        chosen: dict[tuple[str, str, str], _Choice],
        assumptions: dict[str, bool],
    ) -> tuple[PredicateAttribution, ...]:
        predicates: list[PredicateAttribution] = []
        for predicate in self._predicates:
            predicates.append(
                PredicateAttribution(
                    expression=predicate.node.sql(),
                    clause=predicate.clause,
                    scope=predicate.scope.label,
                    operator=predicate.operator,
                    left=self._binding_for_expression(
                        predicate.left, predicate.scope, chosen, assumptions
                    ),
                    right=self._binding_for_expression(
                        predicate.right, predicate.scope, chosen, assumptions
                    ),
                )
            )
        return tuple(predicates)

    def _scenario_outputs(
        self,
        chosen: dict[tuple[str, str, str], _Choice],
        assumptions: dict[str, bool],
        predicates: tuple[PredicateAttribution, ...],
    ) -> dict[str, OutputAttribution]:
        outputs: dict[str, OutputAttribution] = {}
        for scope in self._scopes:
            for full_name, output in self._attributions_for_scope(
                scope, chosen, assumptions, list(predicates)
            ).items():
                key = full_name
                if key in outputs:
                    key = f"{scope.label}:{full_name}"
                outputs[key] = output
        return outputs

    def _binding_for_candidate(
        self,
        group: _Group,
        candidate: _Candidate,
        chosen: dict[tuple[str, str, str], _Choice],
        assumptions: dict[str, bool],
    ) -> Binding:
        reference = (
            f"{group.qualifier}.{group.name}"
            if group.qualifier
            else group.name
        )
        key = f"{candidate.scope.label}:{candidate.relation.alias}.{candidate.column}"
        assumptions[key] = candidate.assumed
        sources = self._sources_for_relation_column(
            candidate.relation,
            candidate.column,
            chosen,
            assumptions,
            (),
        )
        return Binding(
            reference=reference,
            clause=group.refs[0].clause if group.refs else "",
            scope=candidate.scope.label,
            qualifier=group.qualifier,
            relation=candidate.relation.alias,
            relation_kind=candidate.relation.kind,
            column=candidate.column,
            sources=tuple(sources),
            fallback=candidate.fallback,
            shadowed=candidate.shadowed,
            assumed=candidate.assumed,
        )

    def _binding_for_expression(
        self,
        expression: exp.Expression | None,
        scope: _Scope,
        chosen: dict[tuple[str, str, str], _Choice],
        assumptions: dict[str, bool],
    ) -> Binding | None:
        if expression is None:
            return None
        column = self._single_column(expression)
        if column is None:
            return None
        ref = _Ref(node=column, scope=scope, clause="")
        group = next(
            (item for item in self._groups if item.key == ref.group_key), None
        )
        if group is None:
            return None
        choice = chosen.get(group.key)
        if choice is None:
            return None
        return self._binding_for_candidate(group, choice.candidate, chosen, assumptions)

    # -- output attribution -------------------------------------------

    def _attributions_for_scope(
        self,
        scope: _Scope,
        chosen: dict[tuple[str, str, str], _Choice],
        assumptions: dict[str, bool],
        predicates: list[PredicateAttribution],
    ) -> dict[str, OutputAttribution]:
        outputs: dict[str, OutputAttribution] = {}
        for spec in scope.output_specs:
            if spec.name == "*":
                continue
            bindings = self._bindings_for_expression(
                spec.expression, scope, chosen, assumptions, ()
            )
            sources: list[SourceColumn] = []
            for binding in bindings:
                for source in binding.sources:
                    if source not in sources:
                        sources.append(source)
            conditions = self._conditions_for_spec(
                spec, bindings, predicates, chosen, ()
            )
            full_name = spec.full_name
            outputs[full_name] = OutputAttribution(
                name=spec.name,
                scope=scope.label,
                expression=spec.expression.sql(),
                sources=tuple(sources),
                bindings=tuple(bindings),
                conditions=tuple(conditions),
            )
        return outputs

    def _bindings_for_expression(
        self,
        expression: exp.Expression,
        scope: _Scope,
        chosen: dict[tuple[str, str, str], _Choice],
        assumptions: dict[str, bool],
        seen: tuple[tuple[int, str], ...],
    ) -> list[Binding]:
        if isinstance(expression, (exp.Alias, exp.Paren)):
            return self._bindings_for_expression(
                expression.this, scope, chosen, assumptions, seen
            )
        if isinstance(expression, exp.Column):
            binding = self._binding_for_column(
                expression, scope, chosen, assumptions
            )
            return [binding] if binding is not None else []
        bindings: list[Binding] = []
        for child in expression.iter_expressions():
            if isinstance(child, (exp.Select, exp.Subquery)):
                continue
            for binding in self._bindings_for_expression(
                child, scope, chosen, assumptions, seen
            ):
                if binding not in bindings:
                    bindings.append(binding)
        return bindings

    def _binding_for_column(
        self,
        column: exp.Column,
        scope: _Scope,
        chosen: dict[tuple[str, str, str], _Choice],
        assumptions: dict[str, bool],
    ) -> Binding | None:
        """Resolve one column node against the scenario's chosen bindings."""
        ref = _Ref(node=column, scope=scope, clause="")
        group = next(
            (item for item in self._groups if item.key == ref.group_key),
            None,
        )
        if group is None:
            return None
        choice = chosen.get(group.key)
        if choice is None:
            return None
        return self._binding_for_candidate(
            group, choice.candidate, chosen, assumptions
        )

    def _conditions_for_spec(
        self,
        spec: _OutputSpec,
        bindings: list[Binding],
        predicates: list[PredicateAttribution],
        chosen: dict[tuple[str, str, str], _Choice],
        seen: tuple[int, ...],
    ) -> list[PredicateAttribution]:
        """Collect predicates that constrain this projection.

        Predicates are attached when they mention the projection's resolved
        binding.  When the projection names a deeper relation
        (``v.chosen_code``), the predicates live in that relation's body, so
        the binding is followed into the body scope.  The nested lookup uses
        the same scenario choices, which keeps a projected value and the
        predicate on its left-hand side on one binding.
        """
        if not bindings:
            return []
        marker = id(spec.expression)
        if marker in seen:
            return []
        seen = (*seen, marker)

        result = self._direct_conditions(bindings, predicates)
        for binding in bindings:
            for predicate in self._nested_conditions(
                binding, predicates, chosen, seen
            ):
                if predicate.expression not in {
                    item.expression for item in result
                }:
                    result.append(predicate)
        return result

    def _direct_conditions(
        self,
        bindings: list[Binding],
        predicates: list[PredicateAttribution],
    ) -> list[PredicateAttribution]:
        """Return predicates that directly mention one of *bindings*."""
        keys = {
            (binding.scope, binding.relation, binding.column)
            for binding in bindings
        }
        result: list[PredicateAttribution] = []
        for predicate in predicates:
            for binding in (predicate.left, predicate.right):
                if binding is None:
                    continue
                if (
                    binding.scope,
                    binding.relation,
                    binding.column,
                ) in keys:
                    result.append(predicate)
                    break
        return result

    def _nested_conditions(
        self,
        binding: Binding,
        predicates: list[PredicateAttribution],
        chosen: dict[tuple[str, str, str], _Choice],
        seen: tuple[int, ...],
    ) -> list[PredicateAttribution]:
        """Follow a binding into its relation body and collect conditions."""
        relation = self._relation_by_binding(binding)
        if relation is None or relation.body_scope is None:
            return []
        spec = self._output_spec(relation.body_scope, binding.column)
        if spec is None or self._binding_has_conditions(binding, predicates):
            return []
        inner_bindings = self._bindings_for_expression(
            spec.expression, spec.scope, chosen, {}, ()
        )
        if not inner_bindings:
            return []
        return self._conditions_for_spec(
            spec, inner_bindings, predicates, chosen, seen
        )

    @staticmethod
    def _binding_has_conditions(
        binding: Binding, predicates: list[PredicateAttribution]
    ) -> bool:
        """Whether a predicate already names this exact binding."""
        for predicate in predicates:
            for side in (predicate.left, predicate.right):
                if (
                    side is not None
                    and side.scope == binding.scope
                    and side.relation == binding.relation
                    and side.column == binding.column
                ):
                    return True
        return False

    def _relation_by_binding(self, binding: Binding) -> _Relation | None:
        for scope in self._scopes:
            if scope.label != binding.scope:
                continue
            for relation in scope.relations:
                if relation.alias == binding.relation:
                    return relation
        return None

    def _build_output_reports(
        self, scenarios: list[AttributionScenario]
    ) -> dict[str, OutputReport]:
        names: list[str] = []
        for scenario in scenarios:
            for name in scenario.outputs:
                if name not in names:
                    names.append(name)
        reports: dict[str, OutputReport] = {}
        for name in names:
            by_scenario = {
                scenario.id: scenario.outputs[name]
                for scenario in scenarios
                if name in scenario.outputs
            }
            reports[name] = OutputReport(name=name, by_scenario=by_scenario)
        return reports

    # -- leaf source tracing ------------------------------------------

    def _sources_for_relation_column(
        self,
        relation: _Relation,
        column: str,
        chosen: dict[tuple[str, str, str], _Choice],
        assumptions: dict[str, bool],
        seen: tuple[tuple[int, str], ...],
    ) -> list[SourceColumn]:
        cache_key = (id(relation), column)
        if cache_key in self._source_cache:
            return list(self._source_cache[cache_key])
        marker = (id(relation), column)
        if marker in seen:
            return [SourceColumn(relation.name, column, (relation.alias,))]

        if relation.kind == "table" or relation.body_scope is None:
            source = SourceColumn(
                relation.table_name or relation.name,
                column,
                (relation.alias, relation.table_name or relation.name),
            )
            self._source_cache[cache_key] = [source]
            return [source]

        spec = self._output_spec(relation.body_scope, column)
        if spec is None:
            source = SourceColumn(
                relation.name, column, (relation.alias, relation.name)
            )
            self._source_cache[cache_key] = [source]
            return [source]

        sources = self._expression_sources(
            relation, spec, chosen, assumptions, (*seen, marker)
        )
        self._source_cache[cache_key] = sources
        return sources

    def _expression_sources(
        self,
        relation: _Relation,
        spec: _OutputSpec,
        chosen: dict[tuple[str, str, str], _Choice],
        assumptions: dict[str, bool],
        seen: tuple[tuple[int, str], ...],
    ) -> list[SourceColumn]:
        """Trace the leaf sources of one projection expression."""
        if isinstance(spec.expression, exp.Literal):
            return [SourceColumn(None, str(spec.expression.this))]
        sources: list[SourceColumn] = []
        for binding in self._bindings_for_expression(
            spec.expression, spec.scope, chosen, assumptions, ()
        ):
            for source in binding.sources:
                extended = SourceColumn(
                    source.table,
                    source.column,
                    (relation.alias, *source.path),
                )
                if extended not in sources:
                    sources.append(extended)
        return sources

    @staticmethod
    def _output_spec(scope: _Scope, name: str) -> _OutputSpec | None:
        for spec in scope.output_specs:
            if spec.name == name:
                return spec
        return None

    # -- CTE reporting ------------------------------------------------

    def _cte_infos(
        self, scenarios: list[AttributionScenario]
    ) -> Iterator[CteInfo]:
        for cte in self._ctes:
            yield CteInfo(
                name=cte.name,
                scope=cte.declaring_scope.label,
                body_scope=cte.body_scope.label if cte.body_scope else "",
                columns=self._cte_columns(cte),
                sources=self._cte_sources(cte, scenarios),
            )

    def _cte_columns(self, cte: _CteDef) -> tuple[str, ...] | None:
        if cte.explicit_columns:
            return cte.explicit_columns
        if cte.body_scope is None:
            return None
        return self._scope_output_columns(cte.body_scope)

    @staticmethod
    def _cte_sources(
        cte: _CteDef, scenarios: list[AttributionScenario]
    ) -> dict[str, tuple[str, ...]]:
        sources: dict[str, tuple[str, ...]] = {}
        if cte.body_scope is None:
            return sources
        for spec in cte.body_scope.output_specs:
            if spec.name == "*":
                continue
            names = [
                source.qualified
                for scenario in scenarios
                for source in scenario.outputs.get(
                    spec.full_name, OutputAttribution("", "", "")
                ).sources
            ]
            if names:
                sources[spec.name] = tuple(dict.fromkeys(names))
        return sources

    def _derive_tables(self) -> tuple[str, ...]:
        tables: list[str] = []
        for relation in (
            rel for scope in self._scopes for rel in scope.relations
        ):
            if relation.kind == "table":
                name = relation.table_name or relation.name
                if name not in tables:
                    tables.append(name)
        return tuple(tables)


# ---------------------------------------------------------------------------
# Public helper
# ---------------------------------------------------------------------------


def build_column_attribution(
    ast: exp.Expression,
    definitions: Mapping[str, Any] | None = None,
    dialect: str = "default",
    tables: Sequence[str] | None = None,
) -> ColumnAttributionReport:
    """Build a :class:`ColumnAttributionReport` for *ast*."""
    return ColumnAttributionAnalyzer(
        ast, definitions=definitions, dialect=dialect, tables=tables
    ).build()


def attribute_columns(
    sql: str,
    definitions: Mapping[str, Any] | None = None,
    dialect: str | None = None,
) -> ColumnAttributionReport:
    """Parse *sql* and return its column-attribution report.

    Convenience wrapper around :class:`~sql_metadata.parser.Parser` for
    callers that only have a query string.
    """
    from sql_metadata.parser import Parser

    return Parser(sql).column_attribution(
        definitions=definitions, dialect=dialect
    )
