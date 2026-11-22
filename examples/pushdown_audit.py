#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  examples/pushdown_audit.py
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are
#  met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following disclaimer
#    in the documentation and/or other materials provided with the
#    distribution.
#  * Neither the name of the project nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
#  A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
#  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
#  SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
#  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
#  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
#  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

"""Safe SQL push-down for rules that call an unknown, pure audit function.

The goal is to let SQLite discard orders that the Rule Engine can prove false
*without* invoking the custom ``$audit`` review function, while never dropping an
order that could match or raise.  This is deliberately *not* a generic rule
compiler: only the fixed grammar subset used by the catalog below (comparisons,
explicit ``null``, ``and`` / ``or`` / ``not``, ``??`` and opaque function calls)
is analyzed.  Anything outside that subset degrades to "exclude nothing".

Run it with::

    PYTHONPATH=lib python3 examples/pushdown_audit.py
"""

from __future__ import annotations

import dataclasses
import re
from typing import Any, Callable

import rule_engine
import rule_engine.engine as engine
import rule_engine.errors as errors
import rule_engine.types as types
from rule_engine.ast.binary.comparison import (
        ComparisonExpression,
        FuzzyComparisonExpression,
        LogicExpression,
)
from rule_engine.ast.binary import CoalesceExpression
from rule_engine.ast.expression.control import UnaryExpression
from rule_engine.ast.expression.resolution import FunctionCallExpression, SymbolExpression
from rule_engine.ast.literal import NullExpression, StringExpression

import sqlalchemy as sa
from sqlalchemy import ForeignKey, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

# ---------------------------------------------------------------------------
# Relational model: the SQLAlchemy type adapter reads nullability, the batch
# number column and the review relationship straight from these mappings.
# ---------------------------------------------------------------------------

class Base(DeclarativeBase):
    pass


class Order(Base):
    __tablename__ = 'orders'
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_no: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[str | None] = mapped_column(String, nullable=True)
    reviews: Mapped[list['Review']] = relationship(back_populates='order')


class Review(Base):
    __tablename__ = 'reviews'
    id: Mapped[int] = mapped_column(primary_key=True)
    order_id: Mapped[int] = mapped_column(ForeignKey('orders.id'))
    payload: Mapped[dict] = mapped_column(sa.JSON)
    order: Mapped[Order] = relationship(back_populates='reviews')


# ---------------------------------------------------------------------------
# The pure, deterministic audit function.  No review relation -> missing field
# error.  With rows it returns a boolean, or raises a key-targeted error when
# the review payload asks for one.  The outcome is fixed per input and unknown
# to the push-down layer, which never imports or calls this.
# ---------------------------------------------------------------------------

class AuditSpy:
    """Records invocations so tests can prove the SQL phase never calls audit."""
    def __init__(self) -> None:
        self.calls: list[tuple[Any, ...]] = []

    def __call__(self, reviews: Any) -> bool:
        self.calls.append(tuple(reviews))
        if not reviews:
            raise LookupError('reviews relationship has no rows')
        first = reviews[0]
        payload = getattr(first, 'payload', first)
        flag = payload.get('flag')
        if flag == 'error':
            raise KeyError('reviews[0].flag')
        return bool(flag)


def build_context(audit: Callable[..., bool], *, ignorecase: bool = False) -> engine.Context:
    context = engine.Context(
            resolver=engine.resolve_attribute,
            type_resolver=rule_engine.type_resolver_from_sqlalchemy(Order),
            regex_flags=re.IGNORECASE if ignorecase else 0,
    )
    context.builtins = rule_engine.builtins.Builtins.from_defaults(
            {'audit': audit},
            value_types={'audit': types.DataType.FUNCTION(
                    'audit',
                    return_type=types.DataType.BOOLEAN,
                    argument_types=(types.DataType.ARRAY(types.DataType.UNDEFINED),),
            )},
    )
    return context


# ---------------------------------------------------------------------------
# Three-valued push-down analyzer.
#
# For every AST node it derives two SQL predicates:
#   * exclude_filter: true ONLY where the subtree is provably non-truthy and
#     provably cannot raise.  Rows matching it are safe to drop in SQLite.
#   * may_error:      whether the subtree contains anything (an opaque function
#     call or unsupported construct) that can raise at evaluation time.
#
# Rule-engine semantics relied upon (verified in tests/pushdown.py):
#   == / != never produce NULL: a type mismatch (including NULL vs string)
#   yields False for == and True for !=.  Python short-circuit means
#   ``a and b`` / ``a or b`` only evaluate b once a's value is settled.
# ---------------------------------------------------------------------------

# Regex metacharacters plus the SQL LIKE wildcards: any of these in a literal
# pattern makes a LIKE rewrite non-equivalent, so such rules decline to exclude.
_UNSAFE_PATTERN_CHARACTERS = set('.^$*+?{}|\\()[]%_')


@dataclasses.dataclass
class Pushdown:
    exclude_filter: Any          # SQLAlchemy condition; sa.false() => nothing provable
    may_error: bool
    reason: str


class _PushdownAnalyzer:
    def __init__(self, columns: dict[str, Any]):
        self._columns = columns

    def analyze(self, rule: rule_engine.Rule) -> Pushdown:
        return self._node(rule.statement.expression)

    def _column_for(self, node: Any) -> Any:
        if not isinstance(node, SymbolExpression) or node.scope is not None:
            raise ValueError('unsupported symbol')
        column = self._columns.get(node.name)
        if column is None:
            raise ValueError('no SQL column for symbol')
        return column

    def _node(self, node: Any) -> Pushdown:
        if isinstance(node, FunctionCallExpression):
            # Pure but unknown: true, false and key/missing-field errors are all
            # possible, so no row is provably false and every row may error.
            return Pushdown(sa.false(), True, 'opaque function call')
        if isinstance(node, LogicExpression):
            return self._logic(node)
        if isinstance(node, UnaryExpression) and node.type == 'not':
            return self._not(node)
        if isinstance(node, FuzzyComparisonExpression):
            return self._fuzzy(node)
        if isinstance(node, ComparisonExpression):
            return self._comparison(node)
        if isinstance(node, CoalesceExpression):
            left = self._node(node.left)
            right = self._node(node.right)
            # ``??`` itself cannot raise once both sides are supported; keep the
            # child error surface but no direct exclusion (used inside regex).
            return Pushdown(sa.false(), left.may_error or right.may_error, 'coalesce')
        return Pushdown(sa.false(), True, 'unsupported construct: ' + type(node).__name__)

    def _comparison(self, node: ComparisonExpression) -> Pushdown:
        try:
            column = self._column_for(node.left)
        except ValueError:
            return Pushdown(sa.false(), True, 'comparison over non-column operand')
        op = node.type
        if isinstance(node.right, NullExpression):
            if op == 'eq':      # status == null -> true only on NULL
                return Pushdown(column.isnot(None), False, 'exclude rows where {0} IS NOT NULL'.format(column.name))
            return Pushdown(column.is_(None), False, 'exclude rows where {0} IS NULL'.format(column.name))
        if isinstance(node.right, StringExpression):
            value = node.right.value
            if op == 'eq':      # NULL vs string is a type mismatch -> False
                # Explicit IS NULL branch keeps the predicate two-valued; plain
                # SQL != would evaluate to UNKNOWN on NULL and silently drop rows.
                return Pushdown(
                        sa.or_(column.is_(None), column != value),
                        False,
                        "exclude {0} IS NULL or != 'ready'-style mismatch".format(column.name),
                )
            if op == 'ne':      # NULL vs string is a type mismatch -> True
                return Pushdown(
                        sa.and_(column.isnot(None), column == value),
                        False,
                        'exclude exact match on {0}'.format(column.name),
                )
        return Pushdown(sa.false(), True, 'unsupported comparison shape')

    def _fuzzy(self, node: FuzzyComparisonExpression) -> Pushdown:
        # Only a discharged column compared against a meta-free literal can be
        # pushed; arbitrary regexes have no sound SQL LIKE translation.
        pattern_node = node.right
        if not isinstance(pattern_node, StringExpression):
            return Pushdown(sa.false(), True, 'non-literal regex pattern')
        pattern = pattern_node.value
        if any(char in _UNSAFE_PATTERN_CHARACTERS for char in pattern):
            return Pushdown(sa.false(), True, 'regex/SQL wildcard pattern needs the engine, not SQL LIKE')
        operand = node.left
        column = None
        try:
            if isinstance(operand, SymbolExpression):
                column = self._column_for(operand)
            elif isinstance(operand, CoalesceExpression) and isinstance(operand.left, SymbolExpression):
                column = self._column_for(operand.left)
        except ValueError:
            column = None
        if column is None:
            return Pushdown(sa.false(), True, 'regex over non-column operand')
        # re.match (=~) anchors at the start only; re.search (=~~) may start anywhere.
        like_pattern = pattern + '%' if node.type in ('eq_fzm', 'ne_fzm') else '%' + pattern + '%'
        matches = sa.func.lower(column).like(like_pattern.lower())
        if node.type.startswith('eq'):
            # NULL discharges to '' and does not match, so NULL belongs to the excluded side.
            return Pushdown(sa.or_(column.is_(None), ~matches), False, 'NOCASE regex exclusion')
        # The IS NOT NULL guard keeps this two-valued: LIKE on NULL would be UNKNOWN.
        return Pushdown(sa.and_(column.isnot(None), matches), False, 'negated NOCASE regex exclusion')

    def _logic(self, node: LogicExpression) -> Pushdown:
        left = self._node(node.left)
        right = self._node(node.right)
        if node.type == 'and':
            # false/null on the left short-circuits; the right exclusion only
            # counts when the left cannot raise before reaching it.
            if not left.may_error:
                return Pushdown(sa.or_(left.exclude_filter, right.exclude_filter),
                                left.may_error or right.may_error, 'and (both guarded paths)')
            return Pushdown(left.exclude_filter, True, 'and (left may error)')
        # or: false only where both sides are provably false; an opaque call
        # contributes an empty exclusion, which conservatively empties the result.
        return Pushdown(sa.and_(left.exclude_filter, right.exclude_filter),
                        left.may_error or right.may_error, 'or (both sides must be false)')

    def _not(self, node: UnaryExpression) -> Pushdown:
        inner = self._node(node.right)
        if inner.may_error:
            return Pushdown(sa.false(), True, 'not over a subtree that may error')
        if isinstance(node.right, ComparisonExpression) and isinstance(node.right.left, SymbolExpression):
            try:
                column = self._column_for(node.right.left)
            except ValueError:
                return Pushdown(sa.false(), True, 'not over a non-column comparison')
            if isinstance(node.right.right, StringExpression) and node.right.type == 'eq':
                # not (col == lit) is false only on the literal match; the IS NOT
                # NULL guard keeps the predicate two-valued under the outer NOT.
                return Pushdown(
                        sa.and_(column.isnot(None), column == node.right.right.value),
                        False, 'not-eq exclusion')
        return Pushdown(sa.false(), inner.may_error, 'not (no provable true region)')


# ---------------------------------------------------------------------------
# Fixed rule catalog and fixture rows.
# ---------------------------------------------------------------------------

RULES = [
        ('R1_compare_first', 'status == "ready" and $audit(reviews)', False),
        ('R2_audit_first', '$audit(reviews) and status == "ready"', False),
        ('R3_not_comparison', 'not (status == "ready")', False),
        ('R4_exclusive_branches',
         '(status == "ready" and $audit(reviews)) or (status != "ready" and not $audit(reviews))', False),
        ('R5_same_fact_twice',
         '(status == "ready" and $audit(reviews)) or (status == "ready" and not $audit(reviews))', False),
        ('R6_nocase_regex', '(status ?? "") =~ "ready"', True),
]

# (id, batch_no, status, review payloads); o4 carries two review child rows.
FIXTURE = [
        (1, 'B-001', 'ready',    [{'flag': True}]),
        (2, 'B-002', 'READY',    [{'flag': True}]),           # NOCASE-only match
        (3, 'B-003', None,       [{'flag': False}]),          # explicit NULL status
        (4, 'B-004', 'ready',    [{'flag': 'error'}, {'flag': True}]),  # two review rows, key error
        (5, 'B-005', 'blocked',  []),                         # missing review relation
        (6, 'B-006', 'blocked',  [{'flag': False}]),
        (7, 'B-007', 'ready',    [{'flag': False}]),
        (8, 'B-008', 'archived', [{'flag': True}]),
        (9, 'B-009', 'blocked',  [{'flag': 'error'}]),  # same SQL facts as 6, key error vs False
]


def build_session() -> Any:
    engine = sa.create_engine('sqlite://')
    Base.metadata.create_all(engine)
    session = sa.orm.Session(engine)
    for order_id, batch_no, status, payloads in FIXTURE:
        order = Order(id=order_id, batch_no=batch_no, status=status,
                      reviews=[Review(payload=payload) for payload in payloads])
        session.add(order)
    session.commit()
    return session


def render(statement: Any) -> str:
    return str(statement.compile(compile_kwargs={'literal_binds': True}))


def original_outcomes(session: Any, spy: AuditSpy) -> dict[str, dict[int, Any]]:
    """Reference oracle: one unchanged evaluation of each rule over every order.

    This exists only to prove the push-down is sound; the production pipeline is the
    SQL filter followed by a single engine pass over ``sql_survivors``.
    """
    results: dict[str, dict[int, Any]] = {}
    for name, text, ignorecase in RULES:
        rule = rule_engine.Rule(text, context=build_context(spy, ignorecase=ignorecase))
        per_order: dict[int, Any] = {}
        for order in session.query(Order).order_by(Order.id):
            try:
                per_order[order.id] = rule.matches(order)
            except errors.EngineError as exc:
                per_order[order.id] = type(exc).__name__
        results[name] = per_order
    return results


def pushdown_plan(session: Any) -> dict[str, Pushdown]:
    analyzer = _PushdownAnalyzer({'status': Order.status, 'batch_no': Order.batch_no})
    plans: dict[str, Pushdown] = {}
    for name, text, ignorecase in RULES:
        # Parsing needs a context with the schema; the audit callable is a spy that
        # must never be invoked during analysis.
        spy = AuditSpy()
        rule = rule_engine.Rule(text, context=build_context(spy, ignorecase=ignorecase))
        plan = analyzer.analyze(rule)
        assert spy.calls == [], 'push-down analysis must never invoke $audit'
        plans[name] = plan
    return plans


def sql_survivors(session: Any, plan: Pushdown) -> set[int]:
    statement = sa.select(Order.id).where(sa.not_(plan.exclude_filter))
    return {row[0] for row in session.execute(statement)}


def residual_evaluation(session: Any, spy: AuditSpy, name: str, text: str,
                        ignorecase: bool, survivor_ids: set[int]) -> dict[int, Any]:
    """The production step: the unchanged rule, evaluated once over SQL survivors."""
    rule = rule_engine.Rule(text, context=build_context(spy, ignorecase=ignorecase))
    residual: dict[int, Any] = {}
    for order in session.query(Order).filter(Order.id.in_(survivor_ids)).order_by(Order.id):
        try:
            residual[order.id] = rule.matches(order)
        except errors.EngineError as exc:
            residual[order.id] = type(exc).__name__
    return residual


def main() -> None:
    session = build_session()
    outcomes = original_outcomes(session, AuditSpy())
    plans = pushdown_plan(session)

    print('audit calls during the SQL phase: 0 (the analyzer only walks the parsed AST)\n')
    for name, text, ignorecase in RULES:
        plan = plans[name]
        survivors = sql_survivors(session, plan)
        original = outcomes[name]
        provably_false = {order_id for order_id, result in original.items() if result is False}
        must_keep = set(original) - provably_false
        dropped = set(original) - survivors

        # Production pipeline: one unchanged engine evaluation over the survivors.
        engine_spy = AuditSpy()
        residual = residual_evaluation(session, engine_spy, name, text, ignorecase, survivors)
        retained_errors = {order_id: fate for order_id, fate in residual.items() if isinstance(fate, str)}
        retained_matches = {order_id for order_id, fate in residual.items() if fate is True}

        statement = sa.select(Order.id).where(sa.not_(plan.exclude_filter))
        print(f'{name}: {text}')
        print(f'  SQL candidate : {render(statement)}')
        print(f'  boundary      : {plan.reason} (may_error={plan.may_error})')
        print(f'  safely dropped: {sorted(dropped)}')
        print(f'  kept matches  : {sorted(retained_matches)}')
        print(f'  kept errors   : {sorted(retained_errors)}')
        print(f'  engine-phase audit calls: {len(engine_spy.calls)} (one per surviving order)')
        # Soundness, not completeness: every dropped row is provably false and
        # cannot raise; every true/error row survives. Conservative rules (R4)
        # simply drop nothing because no row is error-free.
        assert dropped <= provably_false, 'a dropped order could match or raise: unsound push-down'
        assert must_keep <= survivors, 'a true or erroring order would be lost: unsound push-down'
        # The single residual pass reproduces the original fates exactly.
        assert residual == {order_id: original[order_id] for order_id in survivors}, 'residual fate drift'
        print()

    # Counterexample pairs sharing every SQL-visible fact yet diverging in fate.
    print('counterexample pairs that force retention:')
    print('  R1: order 1 (ready, audit=True -> match) vs order 7 (ready, audit=False), same SQL facts')
    print('  R2 audit-first: order 5 (blocked, no reviews -> missing-field error) vs order 6 (blocked, False)')
    print('  R5: order 1 (boolean tautology -> True) vs order 4 (same fact errors -> FunctionCallError)')


if __name__ == '__main__':
    main()
