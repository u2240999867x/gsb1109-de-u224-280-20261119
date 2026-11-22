"""Offline dry-run analysis for PostgreSQL-style ``MERGE`` statements.

The :class:`~sql_metadata.parser.Parser` facade extracts structural metadata
(tables, columns, query type) from a ``MERGE`` but cannot say which branches
would actually fire for concrete rows.  This module closes that gap *without
touching a database*:

* :class:`MergeSpec` parses the statement into a checkable description —
  target physical key, the filtered source relation, every ``WHEN`` branch in
  textual order (condition, action, value sources) and the insert columns
  whose values must come from defaults.
* :class:`MergeDryRun` applies that spec to small in-memory target/source
  samples using SQL three-valued logic and reports, per source row, which
  branch fired, what it would write, and when two sources compete for the
  same target row.  A conflicting statement is reported as non-committable:
  none of its writes (including otherwise-valid inserts) count as committed.
* :class:`MergeStructure` is the no-sample report: when no rows are supplied
  the statement is still fully analysable from the SQL plus optional
  uniqueness metadata, and conclusions that depend on unknown source-key
  uniqueness are explicitly flagged as not guaranteed.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import sqlglot
from sqlglot import exp
from sqlglot.dialects.dialect import DialectType

from sql_metadata.exceptions import InvalidQueryDefinition

#: Action identifiers used by :class:`MergeBranch` and :class:`SourceOutcome`.
ACTION_UPDATE = "update"
ACTION_DELETE = "delete"
ACTION_INSERT = "insert"
ACTION_DO_NOTHING = "do_nothing"
ACTION_NO_BRANCH = "no_branch"

#: Handler mapping a scalar AST node type to its evaluator method.
_EvalHandler = Callable[["_RowContext", exp.Expression], Any]


class MergeEvaluationError(ValueError):
    """Raised when a WHEN condition or value expression cannot be evaluated.

    The dry-run only implements the scalar expression surface needed to
    evaluate MERGE branch conditions and assignments against in-memory rows
    (columns, literals, comparisons, IS tests, boolean/arithmetic operators).
    Hitting an unsupported node is reported loudly instead of guessed.
    """


class MergeConflictError(InvalidQueryDefinition):
    """Raised by strict mode when a dry run observes competing source rows."""


# ---------------------------------------------------------------------------
# Description dataclasses
# ---------------------------------------------------------------------------


@dataclass
class MergeBranch:
    """One ``WHEN`` clause, in textual order.

    :param index: 1-based position of the clause in the statement.
    :param matched: ``True`` for ``WHEN MATCHED``, ``False`` for
        ``WHEN NOT MATCHED``.
    :param condition_sql: Rendered AND condition, or ``None`` when absent.
    :param action: One of the ``ACTION_*`` constants.
    :param assignments: UPDATE target column → value expression (SQL text).
    :param insert_columns: INSERT target column list.
    :param insert_values_sql: INSERT value expressions, aligned with
        *insert_columns*.
    """

    index: int
    matched: bool
    condition_sql: str | None
    action: str
    assignments: dict[str, str] = field(default_factory=dict)
    insert_columns: tuple[str, ...] = ()
    insert_values_sql: tuple[str, ...] = ()


@dataclass
class MergeStructure:
    """Structural description of a MERGE, derivable without any sample rows.

    :param target_table: MERGE target table name.
    :param target_alias: Alias of the target relation (may be empty).
    :param source_table: Table backing the USING relation (the inner table of
        a subquery, or the USING table itself).
    :param source_alias: Alias of the USING relation.
    :param source_kind: ``"table"`` or ``"subquery"``.
    :param source_columns: Columns projected by the USING subquery/table.
    :param source_filter_sql: The archive-style WHERE predicate applied to the
        source before matching, rendered as SQL text (``None`` if absent).
    :param on_pairs: ``(target_column, source_column)`` equality pairs in ON
        order — the physical key the match is built on.
    :param branches: WHEN clauses in textual order.
    :param default_managed_columns: Insert target columns absent from the
        INSERT list, when the full target column list was supplied; these
        rely on catalog defaults.  ``None`` when the schema was not supplied.
    """

    target_table: str
    target_alias: str
    source_table: str
    source_alias: str
    source_kind: str
    source_columns: tuple[str, ...]
    source_filter_sql: str | None
    on_pairs: tuple[tuple[str, str], ...]
    branches: tuple[MergeBranch, ...]
    default_managed_columns: tuple[str, ...] | None


@dataclass
class MergeConflict:
    """Two or more source rows competing for the same target row.

    :param key: Target key values (ON order) shared by the competitors.
    :param source_indexes: Input positions of every competing source row.
    :param branch_indexes: WHEN branch each competitor fired (``None`` when
        no clause fired).
    :param actions: Action each competitor attempted.
    :param reason: Human-readable explanation.
    """

    key: tuple[Any, ...]
    source_indexes: list[int]
    branch_indexes: list[int | None]
    actions: list[str]
    reason: str


@dataclass
class SourceOutcome:
    """What one filtered source row did during the dry run.

    :param source_index: Position in the filtered source stream (0-based).
    :param original_index: Position in the input rows handed to the dry run.
    :param key: Target key values (ON order).
    :param matched: Whether the key found a target row.
    :param branch_index: Fired WHEN clause (1-based), ``None`` if none fired.
    :param action: Attempted action (``ACTION_NO_BRANCH`` when none fired).
    :param changes: UPDATE assignments that would be applied.
    :param inserted_row: Full row an INSERT would build, defaults included.
    :param defaults_applied: Default values filled into an INSERT.
    :param note: Optional explanation (e.g. why no clause fired).
    """

    source_index: int
    original_index: int
    key: tuple[Any, ...]
    matched: bool
    branch_index: int | None
    action: str
    changes: dict[str, Any] = field(default_factory=dict)
    inserted_row: dict[str, Any] | None = None
    defaults_applied: dict[str, Any] = field(default_factory=dict)
    note: str | None = None


@dataclass
class MergePlan:
    """Result of a dry run over a concrete sample.

    :param would_commit: ``True`` only when no conflict was observed — the
        whole statement commits atomically for this sample.  On conflict the
        projected target state is rolled back to its input state and even
        unopposed inserts do not count as written.
    :param guaranteed: ``would_commit`` plus metadata assurance that target
        keys are unique and source keys are unique.  ``False`` whenever
        source-key uniqueness is unknown, even if the sample itself is clean.
    :param conflicts: Observed competitions for the same target row.
    :param outcomes: Per-source-row branch trace.
    :param branch_hit_count: Fired count per WHEN branch index (1-based).
    :param target_before: Input target rows keyed by ON key.
    :param target_after: Projected target rows keyed by ON key; equals
        *target_before* on conflict.
    :param guarantee_notes: Reasons *guaranteed* is ``False``.
    """

    would_commit: bool
    guaranteed: bool
    conflicts: list[MergeConflict]
    outcomes: list[SourceOutcome]
    branch_hit_count: dict[int, int]
    target_before: dict[tuple[Any, ...], dict[str, Any]]
    target_after: dict[tuple[Any, ...], dict[str, Any]]
    guarantee_notes: list[str]


# ---------------------------------------------------------------------------
# Scalar expression evaluation with SQL three-valued logic
# ---------------------------------------------------------------------------


class _RowContext:
    """Evaluate scalar expressions against one source/target row pair."""

    def __init__(
        self,
        source_row: dict[str, Any],
        target_row: dict[str, Any] | None,
        source_refs: set[str],
        target_refs: set[str],
        dialect: DialectType,
    ) -> None:
        self._source = source_row
        self._target = target_row
        self._source_refs = source_refs
        self._target_refs = target_refs
        self._dialect = dialect

    def truthy(self, node: exp.Expression) -> bool:
        """Evaluate a condition with ``IS TRUE`` semantics.

        SQL ``NULL`` and ``FALSE`` both fail a WHEN test, so only an actual
        Python ``True`` result counts.
        """
        return self.eval(node) is True

    def eval(self, node: exp.Expression) -> Any:
        """Evaluate a value expression, propagating ``NULL`` as ``None``."""
        handler = self._DISPATCH.get(type(node))
        if handler is None:
            if self._is_comparison(node):
                return self._comparison(node)
            if isinstance(node, (exp.Add, exp.Sub, exp.Mul, exp.Div, exp.Mod)):
                return self._arithmetic(node)
            raise MergeEvaluationError(self._unsupported(node))
        return handler(self, node)

    def _eval_paren(self, node: exp.Expression) -> Any:
        assert isinstance(node, exp.Paren)
        return self.eval(node.this)

    def _eval_column(self, node: exp.Expression) -> Any:
        assert isinstance(node, exp.Column)
        return self._column_value(node)

    def _eval_literal(self, node: exp.Expression) -> Any:
        assert isinstance(node, exp.Literal)
        return self._literal(node)

    def _eval_neg(self, node: exp.Expression) -> Any:
        assert isinstance(node, exp.Neg)
        value = self.eval(node.this)
        return None if value is None else -value

    def _eval_null(self, _node: exp.Expression) -> None:
        return None

    def _eval_boolean(self, node: exp.Expression) -> bool:
        assert isinstance(node, exp.Boolean)
        return bool(node.this)

    def _eval_logic(self, node: exp.Expression) -> bool | None:
        return self._logic(node)

    def _eval_is(self, node: exp.Expression) -> bool:
        assert isinstance(node, exp.Is)
        return self._is_test(node, negated=False)

    _DISPATCH: dict[type[exp.Expression], _EvalHandler] = {
        exp.Paren: _eval_paren,
        exp.Column: _eval_column,
        exp.Literal: _eval_literal,
        exp.Boolean: _eval_boolean,
        exp.Null: _eval_null,
        exp.Neg: _eval_neg,
        exp.And: _eval_logic,
        exp.Or: _eval_logic,
        exp.Not: _eval_logic,
        exp.Is: _eval_is,
    }

    def _unsupported(self, node: exp.Expression) -> str:
        return (
            f"Cannot evaluate MERGE expression {node.sql(dialect=self._dialect)!r} "
            f"({type(node).__name__}) on an in-memory sample"
        )

    @staticmethod
    def _literal(node: exp.Literal) -> Any:
        if node.is_int:
            return int(node.this)
        if node.is_number:
            return float(node.this)
        return node.this

    def _column_value(self, column: exp.Column) -> Any:
        qualifier = column.table
        if qualifier in self._target_refs:
            return self._require_target(column.name)
        if qualifier in self._source_refs:
            return self._source[column.name]
        if qualifier:
            raise MergeEvaluationError(
                f"Column {column.sql()!r} is not bound to the MERGE target or source"
            )
        return self._unqualified_value(column.name)

    def _require_target(self, name: str) -> Any:
        if self._target is None:
            raise MergeEvaluationError(
                f"Target column {name!r} is unavailable in a NOT MATCHED branch"
            )
        return self._target[name]

    def _unqualified_value(self, name: str) -> Any:
        in_source = name in self._source
        in_target = self._target is not None and name in self._target
        if in_source and in_target:
            raise MergeEvaluationError(
                f"Column {name!r} is ambiguous — qualify it with the relation alias"
            )
        if in_target:
            return self._require_target(name)
        return self._source[name]

    def _logic(self, node: exp.Expression) -> bool | None:
        # sqlglot renders ``x IS NOT TRUE`` as Not(Is(x, TRUE)); keep the IS
        # test itself in one place by flipping the negation flag here.
        if isinstance(node, exp.Not) and isinstance(node.this, exp.Is):
            return self._is_test(node.this, negated=True)
        if isinstance(node, exp.Not):
            value = self.eval(node.this)
            return None if value is None else not value
        if isinstance(node, exp.And):
            return self._and(node)
        assert isinstance(node, exp.Or)
        return self._or(node)

    def _and(self, node: exp.And) -> bool | None:
        left = self.eval(node.this)
        right = self.eval(node.expression)
        if left is False or right is False:
            return False
        if left is None or right is None:
            return None
        return True

    def _or(self, node: exp.Or) -> bool | None:
        left = self.eval(node.this)
        right = self.eval(node.expression)
        if left is True or right is True:
            return True
        if left is None or right is None:
            return None
        return False

    def _is_test(self, node: exp.Is, *, negated: bool) -> bool:
        value = self.eval(node.this)
        result = self._is_rhs(node.expression, value)
        return not result if negated else result

    def _is_rhs(self, rhs: exp.Expression, value: Any) -> bool:
        if isinstance(rhs, exp.Null):
            return value is None
        if isinstance(rhs, exp.Boolean):
            return value is bool(rhs.this)
        raise MergeEvaluationError(self._unsupported(rhs))

    @staticmethod
    def _is_comparison(node: exp.Expression) -> bool:
        return isinstance(node, (exp.EQ, exp.NEQ, exp.GT, exp.GTE, exp.LT, exp.LTE))

    def _comparison(self, node: exp.Expression) -> bool | None:
        left = self.eval(node.this)
        right = self.eval(node.expression)
        if left is None or right is None:
            return None
        operators: dict[type[exp.Expression], Callable[[], bool]] = {
            exp.EQ: lambda: left == right,
            exp.NEQ: lambda: left != right,
            exp.GT: lambda: left > right,
            exp.GTE: lambda: left >= right,
            exp.LT: lambda: left < right,
            exp.LTE: lambda: left <= right,
        }
        return operators[type(node)]()

    def _arithmetic(self, node: exp.Expression) -> Any:
        left = self.eval(node.this)
        right = self.eval(node.expression)
        if left is None or right is None:
            return None
        operators: dict[type[exp.Expression], Callable[[], Any]] = {
            exp.Add: lambda: left + right,
            exp.Sub: lambda: left - right,
            exp.Mul: lambda: left * right,
            exp.Div: lambda: left / right,
            exp.Mod: lambda: left % right,
        }
        return operators[type(node)]()


# ---------------------------------------------------------------------------
# MERGE specification
# ---------------------------------------------------------------------------


class MergeSpec:
    """Parsed MERGE statement ready for structural analysis or dry runs.

    Build instances with :meth:`from_sql` (or :meth:`from_parser`); call
    :meth:`structure` for the no-sample report or hand the spec to
    :class:`MergeDryRun`.
    """

    def __init__(self, merge: exp.Merge, dialect: DialectType) -> None:
        self._merge = merge
        self._dialect = dialect
        self.target_table, self.target_alias = self._read_target(merge.this)
        self.source_kind, self.source_alias, self._source_node = self._read_using(
            merge.args["using"]
        )
        self.source_table = self._source_table_name()
        self.source_columns = self._source_columns()
        self.source_filter_sql = self._source_filter()
        self.on_pairs = self._read_on(merge.args["on"])
        self.branches = tuple(self._read_whens(merge))

    @property
    def dialect(self) -> DialectType:
        """Dialect the MERGE was parsed with (used when rendering SQL text)."""
        return self._dialect

    def condition_node(self, branch: MergeBranch) -> exp.Expression | None:
        """Return the raw WHEN condition AST for *branch* (``None`` if absent)."""
        return self._when(branch).args.get("condition")

    def then_node(self, branch: MergeBranch) -> exp.Expression:
        """Return the raw THEN action AST for *branch*."""
        then = self._when(branch).args["then"]
        assert isinstance(then, exp.Expression)
        return then

    def _when(self, branch: MergeBranch) -> exp.When:
        when = self._merge.args["whens"].expressions[branch.index - 1]
        assert isinstance(when, exp.When)
        return when

    # -- construction -------------------------------------------------------

    @classmethod
    def from_sql(cls, sql: str, dialect: DialectType = "postgres") -> MergeSpec:
        """Parse *sql* and validate that it is a single MERGE statement."""
        try:
            ast = sqlglot.parse_one(sql, dialect=dialect)
        except sqlglot.errors.ParseError as exc:
            raise InvalidQueryDefinition(
                "Query could not be parsed — SQL syntax error"
            ) from exc
        if not isinstance(ast, exp.Merge):
            raise InvalidQueryDefinition(
                f"Expected a MERGE statement, got {type(ast).__name__}"
            )
        return cls(ast, dialect)

    @classmethod
    def from_parser(cls, parser: Any) -> MergeSpec:
        """Build a spec from a :class:`~sql_metadata.parser.Parser` AST."""
        ast = parser._ast_parser.ast
        if not isinstance(ast, exp.Merge):
            raise InvalidQueryDefinition(
                f"Expected a MERGE statement, got {type(ast).__name__}"
            )
        return cls(ast, parser._ast_parser.dialect)

    # -- structural report --------------------------------------------------

    def structure(
        self, target_columns: tuple[str, ...] | None = None
    ) -> MergeStructure:
        """Return the no-sample structural description.

        :param target_columns: Full target column list, when known from
            catalog metadata.  Enables reporting which INSERT columns rely
            on defaults; omit it to leave that field ``None``.
        """
        default_managed = self._default_managed_columns(target_columns)
        return MergeStructure(
            target_table=self.target_table,
            target_alias=self.target_alias,
            source_table=self.source_table,
            source_alias=self.source_alias,
            source_kind=self.source_kind,
            source_columns=self.source_columns,
            source_filter_sql=self.source_filter_sql,
            on_pairs=self.on_pairs,
            branches=self.branches,
            default_managed_columns=default_managed,
        )

    def _default_managed_columns(
        self, target_columns: tuple[str, ...] | None
    ) -> tuple[str, ...] | None:
        if target_columns is None:
            return None
        inserted = {
            column
            for branch in self.branches
            if branch.action == ACTION_INSERT
            for column in branch.insert_columns
        }
        return tuple(name for name in target_columns if name not in inserted)

    # -- ON key -------------------------------------------------------------

    def source_key(self, row: dict[str, Any]) -> tuple[Any, ...]:
        """Extract the ON key from a source row."""
        return tuple(row[source_col] for _, source_col in self.on_pairs)

    def target_key(self, row: dict[str, Any]) -> tuple[Any, ...]:
        """Extract the ON key from a target row."""
        return tuple(row[target_col] for target_col, _ in self.on_pairs)

    def target_refs(self) -> set[str]:
        """Aliases/names that qualify a column as belonging to the target."""
        return self._refs(self.target_table, self.target_alias)

    def source_refs(self) -> set[str]:
        """Aliases/names that qualify a column as belonging to the source."""
        return self._refs(self.source_table, self.source_alias)

    @staticmethod
    def _refs(table: str, alias: str) -> set[str]:
        return {ref for ref in (table, alias) if ref}

    def passes_source_filter(self, row: dict[str, Any]) -> bool:
        """Apply the USING subquery WHERE (e.g. the archive filter) to a row."""
        where = self._source_where()
        if where is None:
            return True
        context = _RowContext(
            row, None, self.source_refs(), self.target_refs(), self._dialect
        )
        return context.truthy(where)

    def _source_where(self) -> exp.Expression | None:
        if self.source_kind != "subquery":
            return None
        where = self._source_node.this.args.get("where")
        return where.this if where else None

    # -- parsing helpers ----------------------------------------------------

    @staticmethod
    def _read_target(node: exp.Expression) -> tuple[str, str]:
        if not isinstance(node, exp.Table):
            raise InvalidQueryDefinition("MERGE target must be a table")
        return node.name, node.alias

    @staticmethod
    def _read_using(node: exp.Expression) -> tuple[str, str, exp.Expression]:
        if isinstance(node, exp.Table):
            return "table", node.alias, node
        if isinstance(node, exp.Subquery):
            return "subquery", node.alias, node
        raise InvalidQueryDefinition("MERGE USING must be a table or subquery")

    def _source_table_name(self) -> str:
        if self.source_kind == "table":
            return self._source_node.name
        tables = list(self._source_node.find_all(exp.Table))
        if not tables:
            raise InvalidQueryDefinition("MERGE USING subquery has no source table")
        return tables[0].name

    def _source_columns(self) -> tuple[str, ...]:
        if self.source_kind == "table":
            return ()
        select = self._source_node.this
        return tuple(expression.alias_or_name for expression in select.expressions)

    def _source_filter(self) -> str | None:
        where = self._source_where()
        return where.sql(dialect=self._dialect) if where else None

    def _read_on(self, on: exp.Expression) -> tuple[tuple[str, str], ...]:
        conditions = self._flatten_and(on)
        pairs = [self._on_pair(condition) for condition in conditions]
        return tuple(pairs)

    @staticmethod
    def _flatten_and(node: exp.Expression) -> list[exp.Expression]:
        if isinstance(node, exp.Paren):
            return MergeSpec._flatten_and(node.this)
        if isinstance(node, exp.And):
            return MergeSpec._flatten_and(node.this) + MergeSpec._flatten_and(
                node.expression
            )
        return [node]

    def _on_pair(self, condition: exp.Expression) -> tuple[str, str]:
        if not isinstance(condition, exp.EQ):
            raise InvalidQueryDefinition(
                "MERGE ON must be equality predicates on the target/source key"
            )
        left = condition.this
        right = condition.expression
        if not isinstance(left, exp.Column) or not isinstance(right, exp.Column):
            raise InvalidQueryDefinition(
                "MERGE ON equality must compare target and source columns"
            )
        target_refs = self.target_refs()
        source_refs = self.source_refs()
        if left.table in target_refs and right.table in source_refs:
            return left.name, right.name
        if right.table in target_refs and left.table in source_refs:
            return right.name, left.name
        raise InvalidQueryDefinition(
            "MERGE ON predicate must compare one target column with one source column"
        )

    def _read_whens(self, merge: exp.Merge) -> list[MergeBranch]:
        whens = merge.args.get("whens")
        if whens is None or not whens.expressions:
            raise InvalidQueryDefinition("MERGE requires at least one WHEN clause")
        return [
            self._read_branch(index, when)
            for index, when in enumerate(whens.expressions, start=1)
        ]

    def _read_branch(self, index: int, when: exp.When) -> MergeBranch:
        condition = when.args.get("condition")
        then = when.args["then"]
        action = self._action(then)
        branch = MergeBranch(
            index=index,
            matched=bool(when.args.get("matched")),
            condition_sql=(condition.sql(dialect=self._dialect) if condition else None),
            action=action,
        )
        if action == ACTION_UPDATE:
            branch.assignments = {
                assignment.this.name: assignment.expression.sql(dialect=self._dialect)
                for assignment in then.expressions
            }
        if action == ACTION_INSERT:
            branch.insert_columns, branch.insert_values_sql = self._insert_parts(then)
        return branch

    @staticmethod
    def _action(then: exp.Expression) -> str:
        if isinstance(then, exp.Update):
            return ACTION_UPDATE
        if isinstance(then, exp.Insert):
            return ACTION_INSERT
        if isinstance(then, exp.Var) and then.name.upper() == "DELETE":
            return ACTION_DELETE
        return ACTION_DO_NOTHING

    def _insert_parts(
        self, insert: exp.Insert
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        columns = tuple(column.name for column in insert.this.expressions)
        values = tuple(
            value.sql(dialect=self._dialect) for value in insert.expression.expressions
        )
        return columns, values


def analyze_merge(
    sql: str,
    target_columns: tuple[str, ...] | None = None,
    dialect: DialectType = "postgres",
) -> MergeStructure:
    """Return the no-sample structural description of a MERGE statement."""
    return MergeSpec.from_sql(sql, dialect=dialect).structure(target_columns)


# ---------------------------------------------------------------------------
# Dry run over an in-memory sample
# ---------------------------------------------------------------------------


class MergeDryRun:
    """Apply a :class:`MergeSpec` to small in-memory target/source samples."""

    def __init__(
        self,
        spec: MergeSpec,
        target_rows: list[dict[str, Any]],
        source_rows: list[dict[str, Any]],
        *,
        defaults: dict[str, Any] | None = None,
        target_unique: bool = True,
        source_unique: bool | None = None,
    ) -> None:
        self._spec = spec
        self._input_target = [dict(row) for row in target_rows]
        self._source_rows = source_rows
        self._defaults = defaults or {}
        self._target_unique = target_unique
        self._source_unique = source_unique

    def run(self) -> MergePlan:
        """Simulate the statement and return the full trace and verdict."""
        # MATCHED / NOT MATCHED is decided once, against the statement-start
        # target, exactly like PostgreSQL's join — a DELETE earlier in the
        # statement must not flip a later same-key source into NOT MATCHED.
        baseline = self._indexed_target()
        target = {key: dict(row) for key, row in baseline.items()}
        target_before = {key: dict(row) for key, row in baseline.items()}
        outcomes, hits = self._simulate(target, baseline)
        conflicts = self._collect_conflicts(outcomes)
        notes = self._guarantee_notes(conflicts)
        would_commit = not conflicts
        guaranteed = would_commit and not notes
        target_after = target if would_commit else target_before
        return MergePlan(
            would_commit=would_commit,
            guaranteed=guaranteed,
            conflicts=conflicts,
            outcomes=outcomes,
            branch_hit_count=hits,
            target_before=target_before,
            target_after={key: dict(row) for key, row in target_after.items()},
            guarantee_notes=notes,
        )

    def _indexed_target(self) -> dict[tuple[Any, ...], dict[str, Any]]:
        indexed: dict[tuple[Any, ...], dict[str, Any]] = {}
        for row in self._input_target:
            key = self._spec.target_key(row)
            if key in indexed:
                raise MergeEvaluationError(
                    f"Target sample contains duplicate key {key}: the MERGE ON key "
                    "must be unique for MATCHED/NOT MATCHED to be well defined"
                )
            indexed[key] = dict(row)
        return indexed

    def raise_if_conflicting(self) -> MergePlan:
        """Run and raise :class:`MergeConflictError` on any conflict."""
        plan = self.run()
        if plan.conflicts:
            conflict = plan.conflicts[0]
            raise MergeConflictError(conflict.reason)
        return plan

    # -- simulation ---------------------------------------------------------

    def _simulate(
        self,
        target: dict[tuple[Any, ...], dict[str, Any]],
        baseline: dict[tuple[Any, ...], dict[str, Any]],
    ) -> tuple[list[SourceOutcome], dict[int, int]]:
        outcomes: list[SourceOutcome] = []
        hits: dict[int, int] = {branch.index: 0 for branch in self._spec.branches}
        for filtered_index, (original_index, row) in enumerate(self._filtered()):
            key = self._spec.source_key(row)
            matched = key in baseline
            outcome = self._process_row(
                filtered_index, original_index, row, key, matched, target, baseline
            )
            if outcome.branch_index is not None:
                hits[outcome.branch_index] += 1
            outcomes.append(outcome)
        return outcomes, hits

    def _filtered(self) -> list[tuple[int, dict[str, Any]]]:
        return [
            (index, row)
            for index, row in enumerate(self._source_rows)
            if self._spec.passes_source_filter(row)
        ]

    def _process_row(
        self,
        filtered_index: int,
        original_index: int,
        row: dict[str, Any],
        key: tuple[Any, ...],
        matched: bool,
        target: dict[tuple[Any, ...], dict[str, Any]],
        baseline: dict[tuple[Any, ...], dict[str, Any]],
    ) -> SourceOutcome:
        target_row = self._resolution_row(key, target, baseline)
        branch = self._select_branch(row, target_row, matched)
        outcome = SourceOutcome(
            source_index=filtered_index,
            original_index=original_index,
            key=key,
            matched=matched,
            branch_index=branch.index if branch else None,
            action=branch.action if branch else ACTION_NO_BRANCH,
        )
        if branch is None:
            outcome.note = "No WHEN clause matched this row"
            return outcome
        self._apply(branch, row, key, target, outcome, target_row)
        return outcome

    @staticmethod
    def _resolution_row(
        key: tuple[Any, ...],
        target: dict[tuple[Any, ...], dict[str, Any]],
        baseline: dict[tuple[Any, ...], dict[str, Any]],
    ) -> dict[str, Any] | None:
        if key in target:
            return target[key]
        # A prior DELETE removed the working row; evaluate against a private
        # copy of the statement-start row so target columns still resolve.
        if key in baseline:
            return dict(baseline[key])
        return None

    def _select_branch(
        self,
        row: dict[str, Any],
        target_row: dict[str, Any] | None,
        matched: bool,
    ) -> MergeBranch | None:
        for branch in self._spec.branches:
            if branch.matched != matched:
                continue
            condition = self._branch_condition(branch)
            if condition is None:
                return branch
            context = self._context(row, target_row)
            if context.truthy(condition):
                return branch
        return None

    def _branch_condition(self, branch: MergeBranch) -> exp.Expression | None:
        return self._spec.condition_node(branch)

    def _apply(
        self,
        branch: MergeBranch,
        row: dict[str, Any],
        key: tuple[Any, ...],
        target: dict[tuple[Any, ...], dict[str, Any]],
        outcome: SourceOutcome,
        target_row: dict[str, Any] | None,
    ) -> None:
        if branch.action == ACTION_UPDATE:
            assert target_row is not None
            self._apply_update(branch, row, key, target, target_row, outcome)
        elif branch.action == ACTION_DELETE:
            target.pop(key, None)
        elif branch.action == ACTION_INSERT:
            self._apply_insert(branch, row, key, target, outcome)

    def _apply_update(
        self,
        branch: MergeBranch,
        row: dict[str, Any],
        key: tuple[Any, ...],
        target: dict[tuple[Any, ...], dict[str, Any]],
        target_row: dict[str, Any],
        outcome: SourceOutcome,
    ) -> None:
        context = self._context(row, target_row)
        update = self._spec.then_node(branch)
        for assignment in update.expressions:
            value = context.eval(assignment.expression)
            outcome.changes[assignment.this.name] = value
            target_row[assignment.this.name] = value
        # The row may have been removed by an earlier DELETE on the same key;
        # re-store the updated copy so the change lands in the working state.
        target[key] = target_row

    def _apply_insert(
        self,
        branch: MergeBranch,
        row: dict[str, Any],
        key: tuple[Any, ...],
        target: dict[tuple[Any, ...], dict[str, Any]],
        outcome: SourceOutcome,
    ) -> None:
        context = self._context(row, None)
        insert = self._spec.then_node(branch)
        new_row: dict[str, Any] = {}
        for column, value_node in zip(
            insert.this.expressions, insert.expression.expressions
        ):
            new_row[column.name] = context.eval(value_node)
        for name, default in self._defaults.items():
            if name not in new_row:
                new_row[name] = default
                outcome.defaults_applied[name] = default
        outcome.inserted_row = new_row
        target[key] = new_row

    def _context(
        self,
        row: dict[str, Any],
        target_row: dict[str, Any] | None,
    ) -> _RowContext:
        return _RowContext(
            row,
            target_row,
            self._spec.source_refs(),
            self._spec.target_refs(),
            self._spec.dialect,
        )

    # -- conflicts and guarantees ------------------------------------------

    @staticmethod
    def _collect_conflicts(outcomes: list[SourceOutcome]) -> list[MergeConflict]:
        writers: dict[tuple[Any, ...], list[SourceOutcome]] = {}
        for outcome in outcomes:
            if outcome.action in (ACTION_NO_BRANCH, ACTION_DO_NOTHING):
                continue
            writers.setdefault(outcome.key, []).append(outcome)
        return [
            MergeDryRun._conflict(key, competing)
            for key, competing in writers.items()
            if len(competing) > 1
        ]

    @staticmethod
    def _conflict(
        key: tuple[Any, ...], competing: list[SourceOutcome]
    ) -> MergeConflict:
        source_indexes = [item.original_index for item in competing]
        branch_indexes = [item.branch_index for item in competing]
        return MergeConflict(
            key=key,
            source_indexes=source_indexes,
            branch_indexes=branch_indexes,
            actions=[item.action for item in competing],
            reason=(
                f"{len(competing)} source rows {source_indexes} write target "
                f"key {key} via branches {branch_indexes}; the MERGE cannot commit"
            ),
        )

    def _guarantee_notes(self, conflicts: list[MergeConflict]) -> list[str]:
        notes = [conflict.reason for conflict in conflicts]
        if self._source_unique is None:
            notes.append(
                "Source-key uniqueness is not declared in the metadata: two source "
                "rows may still target the same target row, so per-row branch "
                "results are not a guaranteed commit"
            )
        elif self._source_unique is False:
            notes.append(
                "Source keys are declared non-unique: repeated source rows can "
                "compete for the same target row"
            )
        if not self._target_unique:
            notes.append(
                "Target key uniqueness is not declared: MATCHED/NOT MATCHED "
                "classification itself is not guaranteed"
            )
        return notes
