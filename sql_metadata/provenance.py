"""Column and row provenance across nested set operations.

The regular :class:`~sql_metadata.parser.Parser` API flattens a query into a
single list of columns.  That is not enough to explain which leaf ``SELECT``
of a nested ``UNION`` / ``EXCEPT`` produced a particular output column, nor
whether a ``NULL`` was written explicitly by one branch or filled in because
another branch lacks the column.

:class:`QueryProvenance` builds a small catalogue on top of the same sqlglot
AST:

* every output column is reported with its position and the leaf branches
  that can feed it;
* ``UNION ALL`` without ``BY NAME`` binds **positionally**, while
  ``UNION ALL BY NAME`` binds by output name and fills missing columns with
  ``NULL`` (``null_extension``);
* ``EXCEPT ALL`` is treated as multiset subtraction, so row provenance is a
  candidate set rather than a single arbitrarily chosen source;
* when sample rows are supplied, :meth:`QueryProvenance.residual` evaluates
  the set operations and reports the surviving rows with their possible
  origins.

The catalogue never guesses a unique origin when more than one leaf row
could have survived, and it reports type/arity conflicts against a branch
and output position rather than silently re-mapping internal aliases.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field, replace
from typing import Any, Iterable, Mapping, Sequence

from sqlglot import exp

_SET_OPERATION_TYPES = (exp.Union, exp.Except, exp.Intersect)
_NUMERIC_TYPES = frozenset({"INTEGER", "BIGINT", "DOUBLE", "DECIMAL"})
_TEMPORAL_TYPES = frozenset({"DATE", "TIMESTAMP", "DATETIME"})
_PLAIN_SELECT_BLOCKERS = (
    "where",
    "group",
    "having",
    "qualify",
    "order",
    "limit",
    "offset",
    "joins",
    "laterals",
    "pivots",
)


# ---------------------------------------------------------------------------
# Public result objects
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SourceRef:
    """A single leaf projection that can feed an output column.

    :param branch: Index of the leaf ``SELECT`` in left-to-right order.
    :param table: Base table name, when the branch reads a plain table.
    :param output: Name the leaf branch gives this projection itself.
    :param column: Underlying column name, if the projection is a column.
    :param expression: SQL text of the leaf projection.
    :param kind: One of ``column``, ``literal``, ``explicit_null``,
        ``null_extension``, ``expression`` or ``star``.
    :param value_type: Statically inferred literal type, if any.
    """

    branch: int
    table: str | None
    output: str | None
    column: str | None
    expression: str
    kind: str
    value_type: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "branch": self.branch,
            "table": self.table,
            "output": self.output,
            "column": self.column,
            "expression": self.expression,
            "kind": self.kind,
            "value_type": self.value_type,
        }


@dataclass(frozen=True)
class LeafBranch:
    """A leaf ``SELECT`` of the set-operation tree."""

    index: int
    table: str | None
    sql: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {"index": self.index, "table": self.table, "sql": self.sql}


@dataclass(frozen=True)
class ColumnProvenance:
    """Structural provenance for one output column.

    ``sources`` lists every leaf projection that can feed the column.
    A ``NULL`` value can therefore carry two very different entries: one
    with ``kind == "explicit_null"`` (the branch wrote ``NULL``) and one
    with ``kind == "null_extension"`` (the branch lacks the column and the
    set operation filled it in).
    """

    name: str
    position: int
    sources: tuple[SourceRef, ...]

    @property
    def kinds(self) -> tuple[str, ...]:
        """Return the distinct source kinds, in first-seen order."""
        seen: list[str] = []
        for source in self.sources:
            if source.kind not in seen:
                seen.append(source.kind)
        return tuple(seen)

    def from_branches(self, kind: str | None = None) -> tuple[int, ...]:
        """Return distinct branch indices, optionally filtered by kind."""
        seen: list[int] = []
        for source in self.sources:
            if kind is not None and source.kind != kind:
                continue
            if source.branch not in seen:
                seen.append(source.branch)
        return tuple(seen)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "name": self.name,
            "position": self.position,
            "sources": [source.to_dict() for source in self.sources],
        }


@dataclass(frozen=True)
class SetOperationInfo:
    """Binding information for one set-operation node."""

    index: int
    kind: str
    all: bool
    binding: str
    output_names: tuple[str, ...]
    left_names: tuple[str, ...]
    right_names: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "index": self.index,
            "kind": self.kind,
            "all": self.all,
            "binding": self.binding,
            "output_names": list(self.output_names),
            "left_names": list(self.left_names),
            "right_names": list(self.right_names),
        }


@dataclass(frozen=True)
class StructuralIssue:
    """A provable structural problem, tied to a branch and/or position."""

    kind: str
    branch: int | None
    position: int | None
    detail: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "kind": self.kind,
            "branch": self.branch,
            "position": self.position,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class RowOrigin:
    """A sample row that could have produced a surviving output row."""

    branch: int
    table: str | None
    row: int
    value: tuple[Any, ...]
    null_filled: tuple[int, ...]
    source_kinds: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "branch": self.branch,
            "table": self.table,
            "row": self.row,
            "value": list(self.value),
            "null_filled": list(self.null_filled),
            "source_kinds": list(self.source_kinds),
        }


@dataclass(frozen=True)
class ResidualGroup:
    """Surviving output rows that share one value.

    ``remaining`` is the number of rows left after ``EXCEPT ALL``.
    ``candidates`` lists the sample records that could account for them.
    When ``definite`` is ``False`` the catalogue cannot tell which
    ``remaining`` of the candidates survived; the value must not be
    presented as simultaneously coming from every candidate.
    """

    value: tuple[Any, ...]
    remaining: int
    candidates: tuple[RowOrigin, ...]
    definite: bool
    note: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "value": list(self.value),
            "remaining": self.remaining,
            "candidates": [origin.to_dict() for origin in self.candidates],
            "definite": self.definite,
            "note": self.note,
        }


@dataclass(frozen=True)
class ResidualReport:
    """Result of evaluating the query against sample rows."""

    evaluable: bool
    rows: tuple[ResidualGroup, ...]
    missing_tables: tuple[str, ...]
    note: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly representation."""
        return {
            "evaluable": self.evaluable,
            "rows": [row.to_dict() for row in self.rows],
            "missing_tables": list(self.missing_tables),
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Internal relation model
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Rel:
    """Schema and structural sources for one relational node."""

    names: tuple[str, ...]
    slots: tuple[tuple[SourceRef, ...], ...]
    branches: tuple[int, ...]

    def slot(self, name: str) -> tuple[SourceRef, ...] | None:
        for index, candidate in enumerate(self.names):
            if candidate == name:
                return self.slots[index]
        return None


@dataclass(frozen=True)
class _Input:
    """One FROM/JOIN source used while resolving a SELECT."""

    alias: str
    table: str | None
    relation: _Rel | None
    branch: int | None


@dataclass
class _Context:
    """Mutable state shared while building the catalogue."""

    branches: list[LeafBranch] = field(default_factory=list)
    set_operations: list[SetOperationInfo] = field(default_factory=list)
    issues: list[StructuralIssue] = field(default_factory=list)
    select_branches: dict[int, tuple[int, ...]] = field(default_factory=dict)

    def new_branch(self, table: str | None, sql: str) -> int:
        index = len(self.branches)
        self.branches.append(LeafBranch(index, table, sql))
        return index

    def branch_table(self, branch: int) -> str | None:
        return self.branches[branch].table

    def add_issue(
        self,
        kind: str,
        branch: int | None,
        position: int | None,
        detail: str,
    ) -> None:
        issue = StructuralIssue(kind, branch, position, detail)
        if issue not in self.issues:
            self.issues.append(issue)


# ---------------------------------------------------------------------------
# Catalog
# ---------------------------------------------------------------------------


class QueryProvenance:
    """Catalogue explaining where output columns and rows can come from.

    Instances are created by :attr:`sql_metadata.parser.Parser.provenance`.
    """

    def __init__(
        self,
        ast: exp.Expression | None = None,
        *,
        parse_error: str | None = None,
    ) -> None:
        self._ast = ast
        self._context = _Context()
        self._parse_error = parse_error
        self._columns: tuple[ColumnProvenance, ...] = ()
        if ast is not None:
            relation = _relation(ast, self._context)
            self._columns = tuple(
                ColumnProvenance(name, position, sources)
                for position, (name, sources) in enumerate(
                    zip(relation.names, relation.slots)
                )
            )

    @classmethod
    def unparsed(cls, error: str) -> "QueryProvenance":
        """Return a catalogue that records a parse failure."""
        return cls(None, parse_error=error)

    @property
    def parseable(self) -> bool:
        """Whether the statement produced a usable AST."""
        return self._ast is not None

    @property
    def parse_error(self) -> str | None:
        """Parse error text for incomplete statements, if any."""
        return self._parse_error

    @property
    def columns(self) -> tuple[ColumnProvenance, ...]:
        """Output columns in final order."""
        return self._columns

    @property
    def branches(self) -> tuple[LeafBranch, ...]:
        """Leaf ``SELECT`` branches in left-to-right order."""
        return tuple(self._context.branches)

    @property
    def set_operations(self) -> tuple[SetOperationInfo, ...]:
        """Set-operation nodes, innermost first."""
        return tuple(self._context.set_operations)

    @property
    def issues(self) -> tuple[StructuralIssue, ...]:
        """Provable arity and type conflicts."""
        return tuple(self._context.issues)

    def column(self, name_or_position: str | int) -> ColumnProvenance:
        """Look up an output column by name or zero-based position."""
        if isinstance(name_or_position, int):
            for column in self._columns:
                if column.position == name_or_position:
                    return column
            raise KeyError(f"no output column at position {name_or_position}")
        for column in self._columns:
            if column.name == name_or_position:
                return column
        raise KeyError(f"no output column named {name_or_position!r}")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-friendly structural catalogue."""
        return {
            "parseable": self.parseable,
            "parse_error": self.parse_error,
            "columns": [column.to_dict() for column in self._columns],
            "branches": [branch.to_dict() for branch in self._context.branches],
            "set_operations": [
                operation.to_dict() for operation in self._context.set_operations
            ],
            "issues": [issue.to_dict() for issue in self._context.issues],
        }

    def residual(
        self, sample: Mapping[str, Sequence[Any]] | None = None
    ) -> ResidualReport:
        """Evaluate the set operations against sample rows.

        ``sample`` maps a base table name to rows.  A row may be a mapping
        keyed by column name or a tuple aligned with the leaf ``SELECT``
        projection order.  With no sample (or an incomplete one) only the
        structural catalogue is authoritative and no residual source is
        claimed.
        """
        if self._ast is None:
            return ResidualReport(
                False,
                (),
                (),
                f"statement could not be parsed: {self._parse_error}",
            )
        if sample is None:
            return ResidualReport(
                False,
                (),
                (),
                "no sample rows supplied; structural provenance only",
            )
        table_sample = _TableSample(sample)
        try:
            relation = _Evaluator(self, table_sample).relation(self._ast)
        except _Unevaluable as exc:
            return ResidualReport(False, (), exc.missing_tables, exc.reason)
        return _residual_report(self, relation)


# ---------------------------------------------------------------------------
# Structural catalogue construction
# ---------------------------------------------------------------------------


def _relation(node: exp.Expression, context: _Context) -> _Rel:
    """Return the schema and sources for *node*."""
    if isinstance(node, exp.Select):
        return _select_relation(node, context)
    if isinstance(node, _SET_OPERATION_TYPES):
        return _set_operation_relation(node, context)
    if isinstance(node, (exp.Subquery, exp.Paren)):
        return _relation(node.this, context)
    context.add_issue(
        "unsupported_node",
        None,
        None,
        f"cannot analyse {type(node).__name__} for provenance",
    )
    return _Rel(("*",), ((),), ())


def _select_relation(select: exp.Select, context: _Context) -> _Rel:
    """Build a relation for one SELECT, forwarding through derived tables."""
    inputs = _select_inputs(select, context)
    fallback = None
    if not inputs:
        fallback = context.new_branch(None, select.sql())
    branch_ids = tuple(item.branch for item in inputs if item.branch is not None)
    if fallback is not None:
        branch_ids = (fallback,)
    context.select_branches[id(select)] = branch_ids

    names: list[str] = []
    slots: list[tuple[SourceRef, ...]] = []
    for position, expression in enumerate(select.expressions):
        name = _output_name(expression)
        names.append(name)
        sources = _expression_sources(
            expression,
            inputs,
            name,
            context,
            fallback,
        )
        slots.append(tuple(sources))
    return _Rel(tuple(names), tuple(slots), branch_ids)


def _select_inputs(select: exp.Select, context: _Context) -> list[_Input]:
    """Collect FROM and JOIN sources for a SELECT."""
    inputs: list[_Input] = []
    from_clause = select.args.get("from_")
    if from_clause is not None:
        inputs.extend(_input_from(from_clause.this, context, select))
    for join in select.args.get("joins") or []:
        inputs.extend(_input_from(join.this, context, select))
    return inputs


def _input_from(
    source: exp.Expression,
    context: _Context,
    select: exp.Select,
) -> list[_Input]:
    """Turn one FROM/JOIN source into a resolution input."""
    if isinstance(source, exp.Subquery):
        relation = _relation(source.this, context)
        alias = source.alias or "subquery"
        return [_Input(alias, None, relation, None)]
    if isinstance(source, exp.Table):
        table = source.name
        branch = context.new_branch(table, select.sql())
        alias = source.alias or table
        return [_Input(alias, table, None, branch)]
    context.add_issue(
        "unsupported_source",
        None,
        None,
        f"cannot resolve FROM source {source.sql()}",
    )
    return []


def _output_name(expression: exp.Expression) -> str:
    """Return the output name sqlglot assigns to a projection."""
    if isinstance(expression, exp.Alias):
        return expression.alias
    if isinstance(expression, exp.Column):
        return expression.name
    return "*" if isinstance(expression, exp.Star) else expression.sql()


def _expression_sources(
    expression: exp.Expression,
    inputs: list[_Input],
    output_name: str,
    context: _Context,
    fallback: int | None,
) -> list[SourceRef]:
    """Resolve one projection to the leaf sources that can feed it."""
    inner = expression.this if isinstance(expression, exp.Alias) else expression
    branch = _first_branch(inputs, fallback)
    if isinstance(inner, exp.Column):
        return _column_sources(inner, inputs, output_name, context, fallback)
    if isinstance(inner, exp.Null):
        return [_null_source(branch, context.branch_table(branch), output_name)]
    simple_kinds = {
        exp.Literal: "literal",
        exp.Boolean: "literal",
        exp.Star: "star",
    }
    kind = next(
        (
            name
            for node_type, name in simple_kinds.items()
            if isinstance(inner, node_type)
        ),
        None,
    )
    if kind is not None:
        column = "*" if kind == "star" else None
        return [_projection_source(branch, context, output_name, column, inner, kind)]
    columns = list(inner.find_all(exp.Column))
    if not columns:
        return [
            _projection_source(branch, context, output_name, None, inner, "expression")
        ]
    return _combined_expression_sources(
        inner, columns, inputs, output_name, context, fallback
    )


def _combined_expression_sources(
    inner: exp.Expression,
    columns: list[exp.Column],
    inputs: list[_Input],
    output_name: str,
    context: _Context,
    fallback: int | None,
) -> list[SourceRef]:
    """Collect per-branch sources for an expression over several columns."""
    grouped: dict[int, list[str]] = {}
    for column in columns:
        for source in _column_sources(column, inputs, output_name, context, fallback):
            names = grouped.setdefault(source.branch, [])
            if source.column and source.column not in names:
                names.append(source.column)
    return [
        _projection_source(
            branch,
            context,
            output_name,
            ",".join(names),
            inner,
            "expression",
        )
        for branch, names in grouped.items()
    ]


def _projection_source(
    branch: int,
    context: _Context,
    output_name: str,
    column: str | None,
    inner: exp.Expression,
    kind: str,
) -> SourceRef:
    """Build a source reference for a non-column projection."""
    return SourceRef(
        branch,
        context.branch_table(branch),
        output_name,
        column,
        inner.sql(),
        kind,
        _literal_type(inner),
    )


def _null_source(branch: int, table: str | None, output_name: str) -> SourceRef:
    """Build a source reference for an explicitly written NULL."""
    return SourceRef(
        branch,
        table,
        output_name,
        None,
        "NULL",
        "explicit_null",
        "NULL",
    )


def _first_branch(inputs: list[_Input], fallback: int | None) -> int:
    """Return the branch that owns literal-only projections."""
    if fallback is not None:
        return fallback
    assert inputs, "literal projection has no input source"
    item = inputs[0]
    if item.branch is not None:
        return item.branch
    assert item.relation is not None
    return item.relation.branches[0]


def _column_sources(
    column: exp.Column,
    inputs: list[_Input],
    output_name: str,
    context: _Context,
    fallback: int | None,
) -> list[SourceRef]:
    """Resolve a column reference against the available inputs."""
    matches = _matching_inputs(column, inputs)
    if not matches:
        if fallback is not None:
            return [
                SourceRef(
                    fallback,
                    None,
                    output_name,
                    column.name,
                    column.sql(),
                    "column",
                )
            ]
        context.add_issue(
            "unresolved_column",
            None,
            None,
            f"column {column.sql()} has no visible source",
        )
        return [
            SourceRef(
                -1,
                None,
                output_name,
                column.name,
                column.sql(),
                "column",
            )
        ]

    sources: list[SourceRef] = []
    for item in matches:
        if item.relation is not None:
            sources.extend(_derived_column_sources(item, column, output_name, context))
        elif item.branch is not None:
            sources.append(
                SourceRef(
                    item.branch,
                    item.table,
                    output_name,
                    column.name,
                    column.sql(),
                    "column",
                )
            )
    return sources


def _matching_inputs(column: exp.Column, inputs: list[_Input]) -> list[_Input]:
    """Return inputs a column reference can resolve to."""
    if column.table:
        qualified = [
            item for item in inputs if column.table in (item.alias, item.table)
        ]
        return qualified
    return list(inputs)


def _derived_column_sources(
    item: _Input,
    column: exp.Column,
    output_name: str,
    context: _Context,
) -> list[SourceRef]:
    """Forward provenance through a derived table."""
    assert item.relation is not None
    slot = item.relation.slot(column.name)
    if slot is None:
        context.add_issue(
            "unknown_derived_column",
            None,
            None,
            f"{item.alias}.{column.name} is not produced by the subquery",
        )
        branch = item.relation.branches[0] if item.relation.branches else -1
        return [
            SourceRef(
                branch,
                None,
                output_name,
                column.name,
                column.sql(),
                "column",
            )
        ]
    return list(slot)


def _set_operation_relation(node: exp.Expression, context: _Context) -> _Rel:
    """Build a relation for a UNION / EXCEPT / INTERSECT node."""
    left = _relation(node.this, context)
    right = _relation(node.expression, context)
    by_name = bool(node.args.get("by_name"))
    keep_all = node.args.get("distinct") is False
    kind = _set_operation_kind(node)
    names, slots = _align_slots(left, right, by_name, kind, context)
    for position, sources in enumerate(slots):
        _check_types(sources, position, context)
    context.set_operations.append(
        SetOperationInfo(
            index=len(context.set_operations),
            kind=kind,
            all=keep_all,
            binding="name" if by_name else "position",
            output_names=names,
            left_names=left.names,
            right_names=right.names,
        )
    )
    branches = tuple(dict.fromkeys(left.branches + right.branches))
    return _Rel(names, slots, branches)


def _set_operation_kind(node: exp.Expression) -> str:
    """Return the lowercase set-operation name."""
    if isinstance(node, exp.Except):
        return "except"
    if isinstance(node, exp.Intersect):
        return "intersect"
    return "union"


def _align_slots(
    left: _Rel,
    right: _Rel,
    by_name: bool,
    kind: str,
    context: _Context,
) -> tuple[tuple[str, ...], tuple[tuple[SourceRef, ...], ...]]:
    """Align two relation schemas and merge their structural sources."""
    if by_name:
        return _align_by_name(left, right, kind, context)
    return _align_by_position(left, right, kind, context)


def _align_by_name(
    left: _Rel,
    right: _Rel,
    kind: str,
    context: _Context,
) -> tuple[tuple[str, ...], tuple[tuple[SourceRef, ...], ...]]:
    """Align by output name, filling missing columns with NULL."""
    if len(set(left.names)) != len(left.names):
        context.add_issue(
            "duplicate_output_name", None, None, "left branch repeats a name"
        )
    if kind == "except":
        # EXCEPT keeps the left schema; a name only on the right is not
        # part of the output and cannot contribute a null-extension.
        return left.names, left.slots
    names = list(left.names)
    names.extend(name for name in right.names if name not in names)
    slots: list[tuple[SourceRef, ...]] = []
    for name in names:
        left_slot = left.slot(name)
        right_slot = right.slot(name)
        left_sources = (
            left_slot
            if left_slot is not None
            else _null_extension_sources(left, context)
        )
        right_sources = (
            right_slot
            if right_slot is not None
            else _null_extension_sources(right, context)
        )
        slots.append(_merge_sources(left_sources, right_sources, kind))
    return tuple(names), tuple(slots)


def _align_by_position(
    left: _Rel,
    right: _Rel,
    kind: str,
    context: _Context,
) -> tuple[tuple[str, ...], tuple[tuple[SourceRef, ...], ...]]:
    """Align by position without letting aliases invent extra columns."""
    if len(left.names) != len(right.names):
        extra_right = len(right.names) > len(left.names)
        longer = right if extra_right else left
        position = min(len(left.names), len(right.names))
        branch = longer.branches[0] if longer.branches else None
        context.add_issue(
            "arity_mismatch",
            branch,
            position,
            f"left has {len(left.names)} columns, right has "
            f"{len(right.names)}; positional alignment stops at "
            f"position {position}",
        )
    width = min(len(left.names), len(right.names))
    slots = tuple(
        _merge_sources(left.slots[index], right.slots[index], kind)
        for index in range(width)
    )
    return left.names[:width], slots


def _merge_sources(
    left: Iterable[SourceRef],
    right: Iterable[SourceRef],
    kind: str,
) -> tuple[SourceRef, ...]:
    """Merge branch sources; EXCEPT keeps only the left-hand sources."""
    left_sources = tuple(left)
    if kind == "except":
        return left_sources
    return left_sources + tuple(right)


def _null_extension_sources(relation: _Rel, context: _Context) -> tuple[SourceRef, ...]:
    """Build NULL sources for every branch that lacks the column."""
    return tuple(
        SourceRef(
            branch,
            context.branch_table(branch),
            None,
            None,
            "NULL",
            "null_extension",
            "NULL",
        )
        for branch in relation.branches
    )


_SIMPLE_LITERAL_TYPES: dict[type, str] = {
    exp.Null: "NULL",
    exp.Boolean: "BOOLEAN",
}


def _literal_type(node: exp.Expression) -> str | None:
    """Infer a sqlglot-style type for a literal expression."""
    literal_type = _SIMPLE_LITERAL_TYPES.get(type(node))
    if literal_type is not None:
        return literal_type
    if isinstance(node, exp.Cast):
        target = node.args.get("to")
        return target.this.name if isinstance(target, exp.DataType) else None
    if not isinstance(node, exp.Literal):
        return None
    if node.is_int:
        return "INTEGER"
    return "DOUBLE" if node.is_number else "VARCHAR"


def _types_compatible(left: str, right: str) -> bool:
    """Return whether two provable literal types can share a slot."""
    if left == right or "NULL" in (left, right):
        return True
    families = (_NUMERIC_TYPES, _TEMPORAL_TYPES)
    return any(left in family and right in family for family in families)


def _check_types(
    sources: tuple[SourceRef, ...], position: int, context: _Context
) -> None:
    """Report provably incompatible literal types in one slot."""
    typed = [
        (source.branch, source.value_type)
        for source in sources
        if source.value_type not in (None, "NULL")
    ]
    for index, (left_branch, left_type) in enumerate(typed):
        assert left_type is not None
        for right_branch, right_type in typed[index + 1 :]:
            assert right_type is not None
            if _types_compatible(left_type, right_type):
                continue
            context.add_issue(
                "type_conflict",
                right_branch,
                position,
                f"branch {left_branch} has {left_type} but branch "
                f"{right_branch} has {right_type} at position {position}",
            )


# ---------------------------------------------------------------------------
# Sample evaluation
# ---------------------------------------------------------------------------


class _Unevaluable(Exception):
    """Raised when sample evaluation cannot produce a trustworthy result."""

    def __init__(self, reason: str, missing_tables: tuple[str, ...] = ()) -> None:
        super().__init__(reason)
        self.reason = reason
        self.missing_tables = missing_tables


@dataclass(frozen=True)
class _Record:
    """One physical sample row in a leaf branch."""

    branch: int
    table: str | None
    row: int
    filled: frozenset[int] = frozenset()


@dataclass(frozen=True)
class _EvaluatedRow:
    """One row of an evaluated relation, with candidate records."""

    value: tuple[Any, ...]
    records: tuple[_Record, ...]
    count: int = 1
    ambiguous: bool = False
    filled: frozenset[int] = frozenset()


@dataclass(frozen=True)
class _Evaluated:
    """Evaluated relation schema and rows."""

    names: tuple[str, ...]
    rows: tuple[_EvaluatedRow, ...]

    def reorder(self, names: tuple[str, ...]) -> tuple[_EvaluatedRow, ...]:
        """Reorder rows to *names*, NULL-filling absent columns."""
        rows: list[_EvaluatedRow] = []
        for row in self.rows:
            mapping = dict(zip(self.names, row.value))
            value: list[Any] = []
            filled: set[int] = set(row.filled)
            for position, name in enumerate(names):
                if name in mapping:
                    value.append(mapping[name])
                else:
                    value.append(None)
                    filled.add(position)
            rows.append(
                _EvaluatedRow(
                    tuple(value),
                    _fill_records(row.records, filled),
                    row.count,
                    row.ambiguous,
                )
            )
        return tuple(rows)


def _fill_records(
    records: tuple[_Record, ...], filled: Iterable[int]
) -> tuple[_Record, ...]:
    """Mark which output positions were NULL-filled for each record."""
    positions = frozenset(filled)
    return tuple(
        replace(record, filled=record.filled | positions) for record in records
    )


class _TableSample:
    """Case-tolerant lookup of sample rows by table name."""

    def __init__(self, sample: Mapping[str, Sequence[Any]]) -> None:
        self._sample: dict[str, Sequence[Any]] = {}
        for table, rows in sample.items():
            self._sample[table] = rows
            self._sample.setdefault(table.lower(), rows)
            self._sample.setdefault(table.rsplit(".", 1)[-1], rows)

    def get(self, table: str) -> Sequence[Any] | None:
        for key in (table, table.lower(), table.rsplit(".", 1)[-1]):
            if key in self._sample:
                return self._sample[key]
        return None


class _Evaluator:
    """Evaluate a set-operation tree against sample rows."""

    def __init__(self, provenance: QueryProvenance, sample: _TableSample):
        self._provenance = provenance
        self._sample = sample

    def relation(self, node: exp.Expression) -> _Evaluated:
        """Evaluate *node* and return its rows."""
        if isinstance(node, exp.Select):
            return self._select(node)
        if isinstance(node, _SET_OPERATION_TYPES):
            return self._set_operation(node)
        raise _Unevaluable(f"sample evaluation does not support {type(node).__name__}")

    def _select(self, select: exp.Select) -> _Evaluated:
        """Evaluate a plain SELECT or forward a derived relation."""
        if any(select.args.get(key) for key in _PLAIN_SELECT_BLOCKERS):
            raise _Unevaluable("sample evaluation is limited to plain SELECT blocks")
        names = tuple(_output_name(item) for item in select.expressions)
        from_clause = select.args.get("from_")
        if from_clause is None:
            return self._literal_select(select, names)
        source = from_clause.this
        if isinstance(source, exp.Subquery):
            return self._forward_select(select, source, names)
        if isinstance(source, exp.Table):
            return self._table_select(select, source, names)
        raise _Unevaluable(
            f"sample evaluation does not support {type(source).__name__}"
        )

    def _literal_select(self, select: exp.Select, names: tuple[str, ...]) -> _Evaluated:
        branches = self._provenance._context.select_branches.get(id(select), ())
        record = _Record(branches[0] if branches else -1, None, 0)
        values = tuple(
            _literal_value(item.this if isinstance(item, exp.Alias) else item)
            for item in select.expressions
        )
        return _Evaluated(names, (_EvaluatedRow(values, (record,)),))

    def _forward_select(
        self,
        select: exp.Select,
        source: exp.Subquery,
        names: tuple[str, ...],
    ) -> _Evaluated:
        inner = self.relation(source.this)
        mapping = dict(zip(inner.names, range(len(inner.names))))
        rows: list[_EvaluatedRow] = []
        for row in inner.rows:
            selected: list[Any] = []
            for item in select.expressions:
                column = item.this if isinstance(item, exp.Alias) else item
                if not isinstance(column, exp.Column):
                    raise _Unevaluable("sample evaluation only forwards plain columns")
                if column.name not in mapping:
                    raise _Unevaluable(f"subquery does not produce {column.name}")
                selected.append(row.value[mapping[column.name]])
            rows.append(
                _EvaluatedRow(tuple(selected), row.records, row.count, row.ambiguous)
            )
        return _Evaluated(names, tuple(rows))

    def _table_select(
        self,
        select: exp.Select,
        source: exp.Table,
        names: tuple[str, ...],
    ) -> _Evaluated:
        table = source.name
        sample = self._sample.get(table)
        if sample is None:
            raise _Unevaluable(f"no sample rows for table {table!r}", (table,))
        branches = self._provenance._context.select_branches.get(id(select), ())
        branch = branches[0] if branches else -1
        rows: list[_EvaluatedRow] = []
        for index, sample_row in enumerate(sample):
            values = tuple(
                _sample_value(
                    sample_row,
                    position,
                    item,
                )
                for position, item in enumerate(select.expressions)
            )
            rows.append(_EvaluatedRow(values, (_Record(branch, table, index),)))
        return _Evaluated(names, tuple(rows))

    def _set_operation(self, node: exp.Expression) -> _Evaluated:
        """Evaluate a set-operation node."""
        left = self.relation(node.this)
        right = self.relation(node.expression)
        by_name = bool(node.args.get("by_name"))
        keep_all = node.args.get("distinct") is False
        kind = _set_operation_kind(node)
        left_rows, right_rows, names = self._align_rows(left, right, by_name, kind)
        if kind == "except":
            rows = _except_rows(left_rows, right_rows, keep_all)
        elif kind == "intersect":
            rows = _intersect_rows(left_rows, right_rows, keep_all)
        else:
            rows = _union_rows(left_rows, right_rows, keep_all)
        return _Evaluated(names, tuple(rows))

    def _align_rows(
        self,
        left: _Evaluated,
        right: _Evaluated,
        by_name: bool,
        kind: str,
    ) -> tuple[
        tuple[_EvaluatedRow, ...],
        tuple[_EvaluatedRow, ...],
        tuple[str, ...],
    ]:
        """Align both sides to a common output schema."""
        if by_name:
            names = left.names
            if kind != "except":
                names += tuple(name for name in right.names if name not in left.names)
            return (
                tuple(left.reorder(names)),
                tuple(right.reorder(names)),
                names,
            )
        if len(left.names) != len(right.names):
            raise _Unevaluable(
                "positional set operation has mismatched arity at position "
                f"{min(len(left.names), len(right.names))}"
            )
        return left.rows, right.rows, left.names


def _sample_value(
    sample_row: Any,
    position: int,
    expression: exp.Expression,
) -> Any:
    """Extract one projection value from a sample row."""
    inner = expression.this if isinstance(expression, exp.Alias) else expression
    if isinstance(inner, (exp.Null, exp.Literal, exp.Boolean)):
        return _literal_value(inner)
    if not isinstance(inner, exp.Column):
        raise _Unevaluable("sample evaluation only supports columns and literals")
    if isinstance(sample_row, Mapping):
        return _mapping_value(sample_row, inner.name)
    if isinstance(sample_row, (str, bytes)):
        raise _Unevaluable("sample rows must not be bare strings")
    try:
        return sample_row[position]
    except (IndexError, KeyError, TypeError) as exc:
        raise _Unevaluable(f"sample row is too short for position {position}") from exc


def _mapping_value(sample_row: Mapping[str, Any], name: str) -> Any:
    """Read a column from a name-keyed sample row."""
    for key in (name, name.lower()):
        if key in sample_row:
            return sample_row[key]
    raise _Unevaluable(f"sample row has no value for column {name!r}")


def _literal_value(node: exp.Expression) -> Any:
    """Convert a literal AST node to a Python value."""
    if isinstance(node, exp.Null):
        return None
    if isinstance(node, exp.Boolean):
        return node.this
    if isinstance(node, exp.Literal):
        if node.is_int:
            return int(node.this)
        if node.is_number:
            return float(node.this)
        return str(node.this)
    raise _Unevaluable(f"sample evaluation does not support {type(node).__name__}")


def _union_rows(
    left: tuple[_EvaluatedRow, ...],
    right: tuple[_EvaluatedRow, ...],
    keep_all: bool,
) -> list[_EvaluatedRow]:
    """Apply UNION / UNION ALL semantics."""
    combined = list(left) + list(right)
    if keep_all:
        return combined
    return _group_by_value(combined)


def _except_rows(
    left: tuple[_EvaluatedRow, ...],
    right: tuple[_EvaluatedRow, ...],
    keep_all: bool,
) -> list[_EvaluatedRow]:
    """Apply EXCEPT / EXCEPT ALL semantics."""
    if keep_all:
        return _subtract_all(left, right)
    left_groups = _group_by_value(list(left))
    right_values = {row.value for row in right}
    return [row for row in left_groups if row.value not in right_values]


def _intersect_rows(
    left: tuple[_EvaluatedRow, ...],
    right: tuple[_EvaluatedRow, ...],
    keep_all: bool,
) -> list[_EvaluatedRow]:
    """Apply INTERSECT / INTERSECT ALL semantics."""
    if keep_all:
        return _intersect_all(left, right)
    right_values = {row.value for row in right}
    return [row for row in _group_by_value(list(left)) if row.value in right_values]


def _group_by_value(
    rows: list[_EvaluatedRow],
) -> list[_EvaluatedRow]:
    """Collapse duplicate values into one row carrying every record."""
    result: list[_EvaluatedRow] = []
    for value, group in _group_rows(tuple(rows)).items():
        records = _flat_records(group)
        result.append(
            _EvaluatedRow(
                value,
                records,
                count=1,
                ambiguous=len(records) > 1,
            )
        )
    return result


def _group_rows(
    rows: tuple[_EvaluatedRow, ...],
) -> dict[tuple[Any, ...], list[_EvaluatedRow]]:
    """Group evaluated rows by their value tuple."""
    grouped: dict[tuple[Any, ...], list[_EvaluatedRow]] = {}
    for row in rows:
        grouped.setdefault(row.value, []).append(row)
    return grouped


def _flat_records(group: list[_EvaluatedRow]) -> tuple[_Record, ...]:
    """Flatten the candidate records of a value group."""
    return tuple(record for row in group for record in row.records)


def _subtract_all(
    left: tuple[_EvaluatedRow, ...],
    right: tuple[_EvaluatedRow, ...],
) -> list[_EvaluatedRow]:
    """Apply multiset EXCEPT ALL, keeping surviving origins undecided."""
    right_counts = Counter(row.value for row in right for _ in range(row.count))
    result: list[_EvaluatedRow] = []
    for value, group in _group_rows(left).items():
        left_count = sum(row.count for row in group)
        remaining = left_count - right_counts.get(value, 0)
        if remaining > 0:
            result.append(
                _EvaluatedRow(
                    value,
                    _flat_records(group),
                    count=remaining,
                    ambiguous=remaining < left_count,
                )
            )
    return result


def _intersect_all(
    left: tuple[_EvaluatedRow, ...],
    right: tuple[_EvaluatedRow, ...],
) -> list[_EvaluatedRow]:
    """Apply multiset INTERSECT ALL with candidate origins."""
    right_counts = Counter(row.value for row in right for _ in range(row.count))
    result: list[_EvaluatedRow] = []
    for value, group in _group_rows(left).items():
        left_count = sum(row.count for row in group)
        matched = min(left_count, right_counts.get(value, 0))
        if matched > 0:
            records = _flat_records(group)
            result.append(
                _EvaluatedRow(
                    value,
                    records,
                    count=matched,
                    ambiguous=matched < len(records),
                )
            )
    return result


def _unique_records(group: list[_EvaluatedRow]) -> list[_Record]:
    """Return each candidate record of a group once, in encounter order."""
    records: list[_Record] = []
    seen: set[tuple[int, str, int]] = set()
    for row in group:
        for record in row.records:
            identity = (record.branch, record.table or "", record.row)
            if identity not in seen:
                seen.add(identity)
                records.append(record)
    return records


def _residual_report(
    provenance: QueryProvenance, relation: _Evaluated
) -> ResidualReport:
    """Turn evaluated rows into a residual provenance report."""
    rows: list[ResidualGroup] = []
    for value, group in _group_rows(relation.rows).items():
        remaining = sum(row.count for row in group)
        ambiguous = any(row.ambiguous for row in group)
        records = _unique_records(group)
        definite = not ambiguous and remaining == len(records)
        candidates = tuple(_row_origin(provenance, record, value) for record in records)
        rows.append(
            ResidualGroup(
                value,
                remaining,
                candidates,
                definite,
                _residual_note(remaining, len(candidates), definite),
            )
        )
    return ResidualReport(
        True,
        tuple(rows),
        (),
        "sample rows evaluated; ambiguous groups list every candidate",
    )


def _residual_note(remaining: int, candidates: int, definite: bool) -> str:
    """Describe what the residual group can and cannot prove."""
    if remaining == 0:
        return "no row survives"
    if definite:
        return "all candidate records remain"
    return (
        f"{remaining} of {candidates} candidate records may remain; "
        "the catalogue cannot choose which one"
    )


def _row_origin(
    provenance: QueryProvenance,
    record: _Record,
    value: tuple[Any, ...],
) -> RowOrigin:
    """Attach structural source kinds to one sample record."""
    kinds: list[str] = []
    for column in provenance.columns:
        matching = [
            source.kind for source in column.sources if source.branch == record.branch
        ]
        kinds.append(matching[0] if matching else "unknown")
    return RowOrigin(
        record.branch,
        record.table,
        record.row,
        value,
        tuple(sorted(record.filled)),
        tuple(kinds),
    )
