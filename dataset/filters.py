"""Composable, three-valued query filters.

The regular ``find``/``count`` keyword syntax is two-valued: a row either
matches a condition or it does not.  Reconciliation work needs a third
outcome: a condition cannot be decided because the evidence is absent from
the table (for example, an older archive table has no ``status`` column at
all).  The filters in this module keep that distinction in SQL instead of
loading rows and deciding in Python.

Every filter exposes two clauses for a concrete table:

``match_clause(table)``
    Rows for which the condition is definitely true.

``unknown_clause(table)``
    Rows for which the condition cannot be decided because at least one
    referenced column does not exist.  A condition that is already false
    never becomes unknown.

Callers normally use :meth:`Filter.all`, :meth:`Filter.any` and
:meth:`Filter.column`::

    from dataset import Filter

    condition = Filter.all(
        Filter.column("counterparty").present(),
        Filter.any(
            Filter.column("source").equals("supplement"),
            Filter.all(
                Filter.column("status").not_equals("rejected"),
                Filter.any(
                    Filter.column("status").is_null(),
                    Filter.column("status").in_(["open", "review"]),
                ),
            ),
        ),
    )

    table.find(_filter=condition)
    table.count(_filter=condition)
    table.count(_filter=condition, _pending=True)
    page = table.find_page(_filter=condition, page=2, page_size=50)
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from sqlalchemy import and_, false, func, or_, true
from sqlalchemy import not_ as sql_not
from sqlalchemy.sql.elements import ColumnElement
from sqlalchemy.sql.expression import ClauseElement

from dataset.util import QUERY_STEP, QueryError, ResultIter, RowFactory, row_factory

if TYPE_CHECKING:
    from dataset.table import Table

Clause = ColumnElement[bool]

_KNOWN_OPERATORS = {
    "eq",
    "ne",
    "gt",
    "lt",
    "gte",
    "lte",
    "isnull",
    "notnull",
    "present",
    "in",
    "notin",
    "between",
    "like",
    "ilike",
    "notlike",
    "notilike",
    "startswith",
    "endswith",
}


def _total(clause: ClauseElement) -> Clause:
    """Turn a possibly NULL SQL predicate into a two-valued boolean."""
    result = func.coalesce(clause, false()).self_group()
    return result  # type: ignore[return-value]


def _none_if_missing(table: Table, column: str) -> Any | None:
    """Return the reflected column, or ``None`` when the table lacks it.

    ``None`` is deliberately different from SQL NULL: the former means that
    the table provides no evidence for this column, while the latter is an
    explicit empty value stored in an existing column.
    """
    if not table.has_column(column):
        return None
    return table.table.c[table._get_column_name(column)]


def _not_equal(column: Any, value: Any, *, null_safe: bool) -> ClauseElement:
    """``!=`` that can explicitly retain stored NULLs."""
    expression: ClauseElement
    if value is None:
        expression = column.is_not(None)
    elif not null_safe:
        expression = column != value
    else:
        expression = or_(column.is_(None), column != value)
    return expression


def _not_in(column: Any, values: Iterable[Any]) -> ClauseElement:
    """``NOT IN`` that keeps stored NULLs instead of dropping them."""
    items = list(values)
    expression: ClauseElement
    if not items:
        expression = true()
    elif any(item is None for item in items):
        missing = [v for v in items if v is not None]
        expression = (
            column.is_not(None)
            if not missing
            else and_(column.is_not(None), column.notin_(missing))
        )
    else:
        expression = or_(column.is_(None), column.notin_(items))
    return expression


def _all_group(values: Iterable[Any]) -> Filter:
    items = [_group_item(value) for value in values]
    return Filter.all(*items) if items else Filter.all()


def _any_group(values: Iterable[Any]) -> Filter:
    items = [_group_item(value) for value in values]
    return Filter.any(*items) if items else Filter.any()


def _group_item(value: Any) -> Filter:
    if isinstance(value, (Filter, ClauseElement, Mapping)):
        return Filter.from_spec(value)
    raise QueryError(f"Cannot build a grouped filter from {type(value)}")


def _materialize(value: Any) -> Any:
    """Freeze sets for deterministic SQL and allow generators as values."""
    if isinstance(value, set):
        return list(value)
    if isinstance(value, (list, tuple)):
        return list(value)
    return value


class Filter:
    """Base class for composable filters with a definite/unknown split."""

    def match_clause(self, table: Table) -> Clause:
        """SQL predicate selecting rows where this filter is true."""
        raise NotImplementedError

    def unknown_clause(self, table: Table) -> Clause:
        """SQL predicate selecting rows where this filter is undecidable."""
        raise NotImplementedError

    def pending_clause(self, table: Table) -> Clause:
        """Alias for :meth:`unknown_clause` used by reconciliation callers."""
        return self.unknown_clause(table)

    def excluded_clause(self, table: Table) -> Clause:
        """Rows that are neither a match nor waiting for more evidence."""
        matched = self.match_clause(table)
        unknown = self.unknown_clause(table)
        return _total(sql_not(or_(matched, unknown)))

    def clause(self, table: Table) -> Clause:
        """Return the match predicate (compatibility helper)."""
        return self.match_clause(table)

    def __and__(self, other: Filter) -> Filter:
        return self.all(self, other)

    def __or__(self, other: Filter) -> Filter:
        return self.any(self, other)

    def __invert__(self) -> Filter:
        return self.not_(self)

    @classmethod
    def all(cls, *filters: Any) -> Filter:
        """Combine filters with an AND, preserving undecidable outcomes.

        Mappings and SQL clauses are accepted as well as :class:`Filter`
        instances, so a condition can be written compactly.
        """
        return AllFilter(_group_items(filters))

    @classmethod
    def any(cls, *filters: Any) -> Filter:
        """Combine filters with an OR, preserving undecidable outcomes."""
        return AnyFilter(_group_items(filters))

    @classmethod
    def and_(cls, *filters: Any) -> Filter:
        """Alias for :meth:`all`."""
        return cls.all(*filters)

    @classmethod
    def or_(cls, *filters: Any) -> Filter:
        """Alias for :meth:`any`."""
        return cls.any(*filters)

    @classmethod
    def not_(cls, filter_: Any) -> Filter:
        """Negate a filter; an undecidable input stays undecidable."""
        return NotFilter(_coerce(filter_))

    @classmethod
    def column(cls, name: str, *operators: Any, **kwargs: Any) -> ColumnFilter:
        """Start a filter for ``name``; missing columns yield unknown.

        With no additional arguments returns the fluent column builder.
        ``Filter.column("status", "rejected")`` is shorthand for equals, and
        mapping arguments use the same operators as :meth:`from_spec`.
        """
        target = ColumnFilter(name)
        parts: list[Filter] = []
        for operator in operators:
            if isinstance(operator, Mapping):
                for op, value in operator.items():
                    parts.append(_operator_filter(target, op, value))
            else:
                parts.append(target.equals(operator))
        for op, value in kwargs.items():
            parts.append(_operator_filter(target, op, value))
        return target if not parts else _ColumnChain(target, Filter.all(*parts))

    @classmethod
    def raw(cls, clause: ClauseElement) -> Filter:
        """Wrap a raw SQLAlchemy predicate as a definite filter."""
        return ClauseFilter(clause)

    @classmethod
    def from_kwargs(
        cls,
        kwargs: Mapping[str, Any],
        clauses: Iterable[ClauseElement] = (),
    ) -> Filter:
        """Build a filter using the existing ``find`` keyword syntax."""
        parts: list[Filter] = [ClauseFilter(clause) for clause in clauses]
        parts.extend(_column_spec(column, value) for column, value in kwargs.items())
        return cls.all(*parts)

    @classmethod
    def from_spec(cls, spec: Filter | Mapping[str, Any] | ClauseElement) -> Filter:
        """Build a filter from a compact mapping specification.

        Supports normal keyword-style column filters plus ``and``/``all``,
        ``or``/``any`` and ``not_`` grouping keys (with optional ``$`` prefix).
        """
        if isinstance(spec, Filter):
            return spec
        if isinstance(spec, ClauseElement):
            return ClauseFilter(spec)
        if isinstance(spec, Mapping):
            parts: list[Filter] = []
            for key, value in spec.items():
                lowered = str(key).lower().lstrip("_$")
                if lowered in ("and", "all"):
                    parts.append(cls.all(*(cls.from_spec(v) for v in _as_list(value))))
                elif lowered in ("or", "any"):
                    parts.append(cls.any(*(cls.from_spec(v) for v in _as_list(value))))
                elif lowered in ("not", "none"):
                    parts.append(cls.not_(cls.from_spec(value)))
                else:
                    parts.append(_column_spec(key, value, status_like=True))
            return cls.all(*parts)
        if isinstance(spec, (list, tuple, set)):
            return cls.all(*(cls.from_spec(item) for item in spec))
        raise QueryError(f"Cannot build a filter from {type(spec)}")


class ClauseFilter(Filter):
    """A raw SQLAlchemy predicate, treated as definite evidence."""

    def __init__(self, clause: ClauseElement):
        self.clause_ = clause

    def match_clause(self, table: Table) -> Clause:
        return _total(self.clause_)

    def unknown_clause(self, table: Table) -> Clause:
        return false()

    def __repr__(self) -> str:
        return f"ClauseFilter({self.clause_!r})"


class AllFilter(Filter):
    """A conjunction that only becomes unknown when no child is false."""

    def __init__(self, filters: tuple[Filter, ...]):
        self.filters = filters

    def match_clause(self, table: Table) -> Clause:
        matches = [item.match_clause(table) for item in self.filters]
        return _total(and_(true(), *matches))

    def unknown_clause(self, table: Table) -> Clause:
        matches = [item.match_clause(table) for item in self.filters]
        unknowns = [item.unknown_clause(table) for item in self.filters]
        match = and_(true(), *matches)
        possible = and_(
            true(), *(or_(m, u) for m, u in zip(matches, unknowns, strict=True))
        )
        has_unknown = or_(false(), *unknowns)
        return _total(and_(sql_not(match), possible, has_unknown))

    def __repr__(self) -> str:
        return f"AllFilter({self.filters!r})"


class AnyFilter(Filter):
    """A disjunction; any definite true wins over missing evidence."""

    def __init__(self, filters: tuple[Filter, ...]):
        self.filters = filters

    def match_clause(self, table: Table) -> Clause:
        matches = [item.match_clause(table) for item in self.filters]
        return _total(or_(false(), *matches))

    def unknown_clause(self, table: Table) -> Clause:
        matches = [item.match_clause(table) for item in self.filters]
        unknowns = [item.unknown_clause(table) for item in self.filters]
        match = or_(false(), *matches)
        has_unknown = or_(false(), *unknowns)
        return _total(and_(sql_not(match), has_unknown))

    def __repr__(self) -> str:
        return f"AnyFilter({self.filters!r})"


class NotFilter(Filter):
    """Negation that keeps an undecidable child undecidable."""

    def __init__(self, filter_: Filter):
        self.filter_ = filter_

    def match_clause(self, table: Table) -> Clause:
        return _total(
            and_(
                sql_not(self.filter_.match_clause(table)),
                sql_not(self.filter_.unknown_clause(table)),
            )
        )

    def unknown_clause(self, table: Table) -> Clause:
        return self.filter_.unknown_clause(table)

    def __repr__(self) -> str:
        return f"NotFilter({self.filter_!r})"


class PendingFilter(Filter):
    """Select only the rows an existing filter cannot decide."""

    def __init__(self, filter_: Filter):
        self.filter_ = filter_

    def match_clause(self, table: Table) -> Clause:
        return self.filter_.pending_clause(table)

    def unknown_clause(self, table: Table) -> Clause:
        return false()

    def __repr__(self) -> str:
        return f"PendingFilter({self.filter_!r})"


class ColumnFilter(Filter):
    """Builder for predicates on one column.

    A referenced column that is absent from the table produces no match and
    an undecidable ("pending") result.  Null-safe ``not_equals`` is the
    default because ``status != 'rejected'`` must not discard a stored SQL
    NULL; call ``strict=True`` when ordinary SQL NULL semantics are wanted.
    """

    def __init__(self, name: str):
        self.name = name

    def match_clause(self, table: Table) -> Clause:
        """A bare column means the column must contain a value."""
        return self.present().match_clause(table)

    def unknown_clause(self, table: Table) -> Clause:
        return self.present().unknown_clause(table)

    def _atom(
        self,
        operator: str,
        value: Any = None,
        *,
        null_safe: bool = False,
        required: bool = False,
    ) -> Filter:
        return ColumnAtom(
            self.name,
            operator,
            value,
            null_safe=null_safe,
            required=required,
        )

    def equals(self, value: Any) -> Filter:
        return self._atom("eq", value)

    eq = equals

    def not_equals(self, value: Any, *, strict: bool = False) -> Filter:
        return self._atom("ne", value, null_safe=not strict)

    ne = not_equals

    def greater_than(self, value: Any) -> Filter:
        return self._atom("gt", value)

    gt = greater_than

    def less_than(self, value: Any) -> Filter:
        return self._atom("lt", value)

    lt = less_than

    def greater_or_equal(self, value: Any) -> Filter:
        return self._atom("gte", value)

    gte = greater_or_equal

    def less_or_equal(self, value: Any) -> Filter:
        return self._atom("lte", value)

    lte = less_or_equal

    def is_null(self) -> Filter:
        return self._atom("isnull")

    def is_not_null(self) -> Filter:
        return self._atom("notnull")

    def present(self) -> Filter:
        """Require a non-NULL value; a missing column is unusable evidence.

        Unlike ordinary predicates, this atom is ``required``: when its column
        is absent from the table, no row can be a match *or* pending.  This is
        what keeps records without a counterparty out of both totals, even on
        archive tables that have no status column at all.
        """
        return self._atom("present", required=True)

    def has_value(self) -> Filter:
        """Alias for :meth:`present`."""
        return self.present()

    def in_(self, values: Iterable[Any]) -> Filter:
        return self._atom("in", values)

    def not_in(self, values: Iterable[Any], *, strict: bool = False) -> Filter:
        return self._atom("notin", values, null_safe=not strict)

    def between(self, start: Any, end: Any) -> Filter:
        return self._atom("between", (start, end))

    def like(self, value: str) -> Filter:
        return self._atom("like", value)

    def ilike(self, value: str) -> Filter:
        return self._atom("ilike", value)

    def not_like(self, value: str) -> Filter:
        return self._atom("notlike", value)

    def not_ilike(self, value: str) -> Filter:
        return self._atom("notilike", value)

    def startswith(self, value: str) -> Filter:
        return self._atom("startswith", value)

    def endswith(self, value: str) -> Filter:
        return self._atom("endswith", value)

    def __eq__(self, value: Any) -> Filter:  # type: ignore[override]
        return self.equals(value)

    def __ne__(self, value: Any) -> Filter:  # type: ignore[override]
        return self.not_equals(value)

    def isin(self, values: Iterable[Any]) -> Filter:
        """Alias for :meth:`in_`."""
        return self.in_(values)

    def isin_(self, values: Iterable[Any]) -> Filter:
        return self.in_(values)

    def notnull(self) -> Filter:
        return self.is_not_null()

    def null(self) -> Filter:
        return self.is_null()

    def notin_(self, values: Iterable[Any], *, strict: bool = False) -> Filter:
        return self.not_in(values, strict=strict)

    def is_(self, value: Any) -> Filter:
        if value is None:
            return self.is_null()
        return self.equals(value)

    def is_not(self, value: Any) -> Filter:
        if value is None:
            return self.is_not_null()
        return self.not_equals(value)

    def __and__(self, other: Any) -> Filter:
        return Filter.all(self.present(), other)

    def __or__(self, other: Any) -> Filter:
        return Filter.any(self.present(), other)

    def contains(self, value: Any) -> Filter:
        return self.like(f"%{value}%")

    def __gt__(self, value: Any) -> Filter:
        return self.greater_than(value)

    def __ge__(self, value: Any) -> Filter:
        return self.greater_or_equal(value)

    def __lt__(self, value: Any) -> Filter:
        return self.less_than(value)

    def __le__(self, value: Any) -> Filter:
        return self.less_or_equal(value)


class _ColumnChain(ColumnFilter):
    """A column builder that also carries predicates given at construction."""

    def __init__(self, target: ColumnFilter, condition: Filter):
        super().__init__(target.name)
        self.condition = condition

    def _atom(
        self,
        operator: str,
        value: Any = None,
        *,
        null_safe: bool = False,
        required: bool = False,
    ) -> Filter:
        return Filter.all(
            self.condition,
            super()._atom(
                operator,
                value,
                null_safe=null_safe,
                required=required,
            ),
        )


class ColumnAtom(Filter):
    """One concrete predicate evaluated against a table's schema."""

    def __init__(
        self,
        name: str,
        operator: str,
        value: Any = None,
        *,
        null_safe: bool = False,
        required: bool = False,
    ):
        self.name = name
        self.operator = operator
        self.value = value
        self.null_safe = null_safe
        self.required = required

    def _expression(self, table: Table) -> ClauseElement | None:
        column = _none_if_missing(table, self.name)
        if column is None:
            return None
        expression: ClauseElement
        operator = self.operator
        value = self.value
        if operator == "eq":
            expression = column.is_(None) if value is None else column == value
        if operator == "ne":
            expression = _not_equal(column, value, null_safe=self.null_safe)
        if operator == "gt":
            expression = column > value
        if operator == "lt":
            expression = column < value
        if operator == "gte":
            expression = column >= value
        if operator == "lte":
            expression = column <= value
        if operator == "isnull":
            expression = column.is_(None)
        if operator == "notnull":
            expression = column.is_not(None)
        if operator == "present":
            expression = and_(column.is_not(None), column != "")
        if operator == "in":
            expression = column.in_(value)
        if operator == "notin":
            if self.null_safe:
                expression = _not_in(column, value)
            else:
                expression = column.notin_(value)
        if operator == "between":
            start, end = value
            expression = column.between(start, end)
        if operator == "like":
            expression = column.like(value)
        if operator == "ilike":
            expression = column.ilike(value)
        if operator == "notlike":
            expression = column.notlike(value)
        if operator == "notilike":
            expression = column.notilike(value)
        if operator == "startswith":
            expression = column.startswith(value, autoescape=True)
        if operator == "endswith":
            expression = column.endswith(value, autoescape=True)
        if operator not in _KNOWN_OPERATORS:
            raise QueryError(f"Unknown filter operator: {operator}")
        return expression

    def match_clause(self, table: Table) -> Clause:
        expression = self._expression(table)
        return false() if expression is None else _total(expression)

    def unknown_clause(self, table: Table) -> Clause:
        if _none_if_missing(table, self.name) is not None:
            return false()
        return false() if self.required else true()

    def __repr__(self) -> str:
        return f"ColumnAtom({self.name!r}, {self.operator!r}, {self.value!r})"


@dataclass(frozen=True)
class FilterCounts:
    """The two reconciliation totals for one filter."""

    matched: int
    pending: int

    @property
    def total(self) -> int:
        return self.matched + self.pending

    @property
    def match_count(self) -> int:
        return self.matched

    @property
    def pending_count(self) -> int:
        return self.pending

    @property
    def excluded(self) -> int:
        return self.total - self.matched - self.pending

    def __iter__(self) -> Any:
        return iter((self.matched, self.pending))

    def __len__(self) -> int:
        return 2

    def __getitem__(self, key: str | int) -> int:
        if isinstance(key, int):
            return (self.matched, self.pending)[key]
        if key in ("matched", "match", "match_count", "matches", "match_total"):
            return self.matched
        if key in ("pending", "pending_count", "unknown", "pending_total"):
            return self.pending
        if key == "total":
            return self.total
        raise KeyError(key)

    def keys(self) -> Any:
        return self.as_dict().keys()

    def values(self) -> Any:
        return self.as_dict().values()

    def items(self) -> Any:
        return self.as_dict().items()

    def get(self, key: str, default: int | None = None) -> int | None:
        try:
            return self[key]
        except KeyError:
            return default

    def __eq__(self, other: object) -> bool:
        if isinstance(other, Mapping):
            return self.as_dict() == dict(other)
        return super().__eq__(other)

    def as_dict(self) -> dict[str, int]:
        return {
            "matched": self.matched,
            "pending": self.pending,
            "total": self.total,
        }


class FilterResult(ResultIter):
    """A result iterator that can also report counts for the same filter.

    Counts are lazy: obtaining an ordinary page of rows does not run count
    queries.  Accessing ``matched_count`` or ``pending_count`` runs a single
    SQL ``COUNT(*)`` each, using the exact predicates used by the iterator.
    """

    def __init__(
        self,
        *args: Any,
        counts: Any = None,
        page: int | None = None,
        page_size: int | None = None,
        offset: int = 0,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self._counts = counts
        self.page = page
        self.page_size = page_size
        self.offset = offset

    def _count(self, mode: str) -> int:
        if self._counts is None:
            return 0
        return int(self._counts(mode))

    @property
    def match(self) -> int:
        """Number of definite matches for the condition."""
        return self.matched_count

    @property
    def matched(self) -> int:
        return self.matched_count

    @property
    def pending(self) -> int:
        """Number of undecidable rows for the condition."""
        return self.pending_count

    @property
    def unmatched(self) -> int:
        """Alias for pending rows in reconciliation terminology."""
        return self.pending_count

    @property
    def unmatched_count(self) -> int:
        return self.pending_count

    @property
    def matched_count(self) -> int:
        return self._count("matched")

    @property
    def match_count(self) -> int:
        return self.matched_count

    @property
    def matches(self) -> int:
        return self.matched_count

    @property
    def match_total(self) -> int:
        return self.matched_count

    @property
    def pending_count(self) -> int:
        return self._count("pending")

    @property
    def pending_total(self) -> int:
        return self.pending_count

    @property
    def total(self) -> int:
        return self.matched_count + self.pending_count

    @property
    def counts(self) -> FilterCounts:
        return FilterCounts(self.matched_count, self.pending_count)

    def count(self, _pending: bool = False) -> int:
        """Return one of the two totals for the same condition."""
        return self._count("pending" if _pending else "matched")

    def as_counts(self) -> dict[str, int]:
        return self.counts.as_dict()

    @property
    def rows(self) -> FilterResult:
        return self

    @property
    def items(self) -> FilterResult:
        return self

    @property
    def details(self) -> FilterResult:
        return self


def _group_items(filters: Iterable[Any]) -> tuple[Filter, ...]:
    items: list[Any] = list(filters)
    if len(items) == 1 and isinstance(items[0], (list, tuple, set)):
        items = list(items[0])
    return tuple(_coerce(item) for item in items)


def _coerce(item: Any) -> Filter:
    if isinstance(item, Filter):
        return item
    if isinstance(item, ColumnFilter):
        # A bare column in a boolean expression means "column has a value".
        return item.present()
    if isinstance(item, ClauseElement):
        return ClauseFilter(item)
    if isinstance(item, Mapping):
        return Filter.from_spec(item)
    raise QueryError(f"Expected a Filter or SQL clause, got {type(item)}")


def _as_list(value: Any) -> list[Any]:
    if isinstance(value, (list, tuple)):
        return list(value)
    return [value]


def _column_spec(column: str, value: Any, *, status_like: bool = False) -> Filter:
    target = ColumnFilter(column)
    if isinstance(value, Mapping):
        parts: list[Filter] = []
        for operator, operand in value.items():
            lowered = str(operator).lower().lstrip("_$")
            if lowered in ("and", "all"):
                group = [_column_spec(column, v) for v in _as_list(operand)]
                parts.append(Filter.all(*group))
            elif lowered in ("or", "any"):
                group = [_column_spec(column, v) for v in _as_list(operand)]
                parts.append(Filter.any(*group))
            elif status_like and str(operator).lower() in ("!=", "<>", "not", "ne"):
                parts.append(target.not_equals(operand))
            else:
                parts.append(_operator_filter(target, operator, operand))
        return Filter.all(*parts)
    if isinstance(value, (list, tuple, set)):
        return target.in_(value)
    return target.equals(value)


def _operator_filter(target: ColumnFilter, operator: str, value: Any) -> Filter:
    operator = str(operator).lower().lstrip("_$")
    if operator in ("=", "==", "is", "eq"):
        return target.equals(value)
    if operator in ("!=", "<>", "not", "ne"):
        return target.not_equals(value)
    if operator in (">", "gt"):
        return target.greater_than(value)
    if operator in ("<", "lt"):
        return target.less_than(value)
    if operator in (">=", "gte"):
        return target.greater_or_equal(value)
    if operator in ("<=", "lte"):
        return target.less_or_equal(value)
    if operator in ("isnull", "is_null", "null"):
        return target.is_null() if value is not False else target.is_not_null()
    if operator in ("notnull", "not_null"):
        return target.is_not_null() if value is not False else target.is_null()
    if operator in ("present", "required", "exists", "has", "notempty", "not_empty"):
        if value is False:
            return target.is_null()
        return target.present()
    if operator in ("missing", "absent", "empty"):
        return target.is_null()
    if operator in ("in",):
        return target.in_(_require_sequence(operator, value))
    if operator in ("notin", "nin"):
        return target.not_in(_require_sequence(operator, value))
    if operator in ("between", ".."):
        if not isinstance(value, (list, tuple)) or len(value) != 2:
            raise QueryError("'between' filter requires a list of two values")
        return target.between(value[0], value[1])
    if operator in ("like",):
        return target.like(value)
    if operator in ("ilike",):
        return target.ilike(value)
    if operator in ("notlike",):
        return target.not_like(value)
    if operator in ("notilike",):
        return target.not_ilike(value)
    if operator in ("startswith",):
        return target.startswith(value)
    if operator in ("endswith",):
        return target.endswith(value)
    # Preserve the historical behaviour of an unknown operator: no rows.
    return Filter.raw(false())


def _require_sequence(operator: str, value: Any) -> Iterable[Any]:
    if not isinstance(value, (list, tuple, set)):
        raise QueryError(f"'{operator}' filter requires a list, got {type(value)}")
    return value


# Convenient module-level constructors.
all_of = Filter.all
any_of = Filter.any
not_ = Filter.not_
column = Filter.column
Condition = Filter
FilterGroup = Filter


def new_result(
    result_proxy: Any,
    *,
    counts: Any = None,
    row_type: RowFactory = row_factory,
    step: int | None = QUERY_STEP,
    connection: Any = None,
    page: int | None = None,
    page_size: int | None = None,
    offset: int = 0,
) -> FilterResult:
    """Create a :class:`FilterResult` without importing it inside Table."""
    return FilterResult(
        result_proxy,
        row_type=row_type,
        step=step,
        connection=connection,
        counts=counts,
        page=page,
        page_size=page_size,
        offset=offset,
    )


__all__ = [
    "Filter",
    "FilterCounts",
    "FilterResult",
    "ColumnFilter",
    "Condition",
    "FilterGroup",
    "all_of",
    "any_of",
    "column",
    "not_",
    "new_result",
]
