#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  examples/payment_chain_analysis.py
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

"""Static first-hit analysis for a payment review :py:class:`~rule_engine.Chain`.

The chain decides what to do with a payment order. An order carries three fields that the rules reason about:

* ``status``  -- one of the three gateway states ``paid``, ``open`` or ``review``.
* ``amount``  -- the order amount in cents, a 64-bit non-negative integer (``0`` .. ``2**63 - 1``).
* ``refunded`` -- the amount already refunded in cents, with the same range and the invariant
  ``0 <= refunded <= amount``.

Two kinds of uncertainty are treated differently:

* **Analytic forms** -- status equality, integer amount/refunded/remaining-balance (``amount - refunded``)
  comparisons -- are interpreted symbolically from each rule's compiled AST, so feasibility is decided with
  interval arithmetic instead of enumerating cents.
* **External scores** -- ``$risk_score(...)`` is documented by the business as a pure and stable function, but the
  scoring service must not be called during the precheck. A call is treated as an unknown fact. Two calls with the
  same function name and the same argument expressions are the *same* unknown; different arguments are different
  unknowns and the uncertainty is scoped to that region only.

For every rule the analysis returns one of four verdicts with a fully concrete, reviewable witness
(status / amount / refunded / chain position):

* ``FIRST_HIT``     -- an order exists that matches this rule and provably fails every earlier rule.
* ``SHADOWED``      -- orders matching this rule can be constructed, but an earlier rule always intercepts them.
* ``UNSATISFIABLE`` -- the rule's own analytic constraints have an empty feasible region (no order can match).
* ``UNKNOWN``       -- the rule uses a construct outside the analytic vocabulary; the unknown is scoped to its region.

Ordinary witnesses are replayed through the real compiled chain. The only score value used there is an explicit
local test double; the analysis itself never invokes the scoring service.
"""

import collections
import decimal
import itertools
import os
import sys
from dataclasses import dataclass, field

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'lib')))

from rule_engine import ast
from rule_engine import builtins
from rule_engine import engine
from rule_engine import types

# ---------------------------------------------------------------------------
# Domain constants (all monetary values are integer cents)
# ---------------------------------------------------------------------------
AMOUNT_MIN = 0
AMOUNT_MAX = 2 ** 63 - 1
STATUSES = ('paid', 'open', 'review')

# gateway amount interval the front rules operate over; the analyzer reports which corners each front rule
# actually intercepts instead of assuming the two rules cover the whole interval
GATEWAY_MIN = 1000
GATEWAY_MAX = 99999

# front rule thresholds
BALANCE_BOUNDARY_ORIGINAL = 1000   # original remaining-balance boundary
BALANCE_BOUNDARY_MOVED = 999       # after the one-cent move
REFUNDED_THRESHOLD = 5000

# review band covered by the complementary score pair (lower inclusive, upper exclusive)
REVIEW_BAND_MIN = 10000
REVIEW_BAND_MAX_EXCLUSIVE = 20000
SCORE_CUTOFF = 50

SCORE_NAME = 'risk_score'
DEFAULT_TARGET = 'allow'

# ---------------------------------------------------------------------------
# The chain before and after the single one-cent boundary move.
# R0 blocks on un-refunded balance, R1 on accumulated refunded total.
# ---------------------------------------------------------------------------
def _rule_texts(balance_boundary):
    return [
        'status == "paid" and amount - refunded <= {0}'.format(balance_boundary),
        'refunded >= {0}'.format(REFUNDED_THRESHOLD),
        'status == "paid" and amount - refunded == {0}'.format(BALANCE_BOUNDARY_ORIGINAL),
        ('status == "review" and amount >= {0} and amount < {1} and '
         '$risk_score(amount, refunded) >= {2}').format(REVIEW_BAND_MIN, REVIEW_BAND_MAX_EXCLUSIVE, SCORE_CUTOFF),
        ('status == "review" and amount >= {0} and amount < {1} and '
         '$risk_score(amount, refunded) < {2}').format(REVIEW_BAND_MIN, REVIEW_BAND_MAX_EXCLUSIVE, SCORE_CUTOFF),
        'status == "open"',
    ]

ORIGINAL_RULE_TEXTS = _rule_texts(BALANCE_BOUNDARY_ORIGINAL)
REVISED_RULE_TEXTS = _rule_texts(BALANCE_BOUNDARY_MOVED)

TARGETS = (
    'block_low_balance',
    'block_high_refunded',
    'review_balance_corner',
    'review_score_high',
    'review_score_low',
    'open_catch_all',
)

FIRST_HIT = 'FIRST_HIT'
SHADOWED = 'SHADOWED'
UNSATISFIABLE = 'UNSATISFIABLE'
UNKNOWN = 'UNKNOWN'


# ---------------------------------------------------------------------------
# Order validation -- the stated data-layer invariants
# ---------------------------------------------------------------------------
def validate_order(order):
    """Return a list of human-readable validation errors (empty means the order is well formed)."""
    errors_found = []
    status = order.get('status')
    if status not in STATUSES:
        errors_found.append('status must be one of {0}, got {1!r}'.format(STATUSES, status))
    for name in ('amount', 'refunded'):
        value = order.get(name)
        if not isinstance(value, int) or isinstance(value, bool):
            errors_found.append('{0} must be an integer, got {1!r}'.format(name, value))
            continue
        if value < AMOUNT_MIN or value > AMOUNT_MAX:
            errors_found.append('{0}={1} is outside the 64-bit non-negative range'.format(name, value))
    amount = order.get('amount')
    refunded = order.get('refunded')
    if isinstance(amount, int) and isinstance(refunded, int) and not isinstance(refunded, bool):
        if AMOUNT_MIN <= amount <= AMOUNT_MAX and AMOUNT_MIN <= refunded <= AMOUNT_MAX and refunded > amount:
            errors_found.append('refunded={0} can not exceed amount={1}'.format(refunded, amount))
    return errors_found


# ---------------------------------------------------------------------------
# Context construction -- the score builtin is pure/stable but unavailable to
# the precheck, so callers inject either an unavailable stand-in or a local double.
# ---------------------------------------------------------------------------
def unavailable_external_score(*arguments):
    """Stand-in for the real scoring service: any precheck call is a programming error."""
    raise RuntimeError('external scoring service must not be called during precheck: {0!r}'.format(arguments))


def make_context(score_callable=unavailable_external_score):
    context = engine.Context()
    context.builtins = builtins.Builtins.from_defaults(
        {SCORE_NAME: score_callable},
        value_types={
            SCORE_NAME: types.DataType.FUNCTION(
                SCORE_NAME,
                return_type=types.DataType.FLOAT,
                argument_types=(types.DataType.FLOAT, types.DataType.FLOAT),
            )
        },
    )
    return context


def build_chain(rule_texts, score_callable=unavailable_external_score, default_value=DEFAULT_TARGET):
    context = make_context(score_callable)
    targets = list(TARGETS)
    targets.extend('target_{0}'.format(position) for position in range(len(targets), len(rule_texts)))
    return engine.Chain(
        list(zip(rule_texts, targets)),
        default_value=default_value,
        context=context,
    )


def score_double(score_value):
    """Deterministic local test double returning *score_value* for any arguments."""
    def _double(amount, refunded):  # noqa: ARG001 - signature mirrors the registered builtin
        return score_value
    return _double


# ---------------------------------------------------------------------------
# Symbolic interpretation of the analytic rule vocabulary
# ---------------------------------------------------------------------------
_COMPARISON_OPS = {
    'eq': lambda value, boundary: value == boundary,
    'ne': lambda value, boundary: value != boundary,
    'le': lambda value, boundary: value <= boundary,
    'lt': lambda value, boundary: value < boundary,
    'ge': lambda value, boundary: value >= boundary,
    'gt': lambda value, boundary: value > boundary,
}
_TERM_SYMBOLS = ('amount', 'refunded')


def _conjuncts(node):
    """Flatten a tree of ``and`` nodes into its leaves; an ``or`` makes the rule unanalyzable."""
    if isinstance(node, ast.LogicExpression):
        if node.type == 'and':
            return _conjuncts(node.left) + _conjuncts(node.right)
        return None
    return [node]


def _term_key(node):
    """Recognize ``amount``, ``refunded`` and ``amount - refunded`` (the remaining balance)."""
    if isinstance(node, ast.SymbolExpression) and node.name in _TERM_SYMBOLS:
        return node.name
    if isinstance(node, ast.SubtractExpression):
        left = _term_key(node.left)
        right = _term_key(node.right)
        if left == 'amount' and right == 'refunded':
            return 'balance'
    return None


def _literal_int(node):
    # rule-engine parses whole-number literals as integral Decimals, so accept those as integer cents too
    if isinstance(node, ast.LiteralExpressionBase):
        value = node.evaluate(None)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
        if isinstance(value, decimal.Decimal) and value == value.to_integral_value():
            return int(value)
    return None


def _contains_score_call(node):
    found = []

    def _walk(current):
        if isinstance(current, ast.FunctionCallExpression):
            found.append(current)
            return
        for child in (getattr(current, 'left', None), getattr(current, 'right', None)):
            if child is not None and hasattr(child, 'evaluate'):
                _walk(child)
        for argument in getattr(current, 'arguments', ()):
            _walk(argument)

    _walk(node)
    return found


def _score_key(call_node):
    """Identity of an unknown fact: function name plus the argument term expressions."""
    name = call_node.function.name if isinstance(call_node.function, ast.SymbolExpression) else repr(call_node.function)
    arguments = tuple(_term_key(argument) or argument.__class__.__name__ for argument in call_node.arguments)
    return name, arguments


@dataclass
class Bound:
    term: str
    op: str
    value: int


@dataclass
class ScoreCondition:
    key: tuple
    op: str
    value: int


@dataclass
class RuleProfile:
    position: int
    text: str
    statuses: frozenset = frozenset()
    bounds: list = field(default_factory=list)
    scores: list = field(default_factory=list)
    unanalyzable: bool = False
    unanalyzable_reason: str = ''


def profile_rule(position, rule):
    profile = RuleProfile(position=position, text=rule.text)
    leaves = _conjuncts(rule.statement.expression)
    if leaves is None:
        profile.unanalyzable = True
        profile.unanalyzable_reason = 'contains an OR expression outside the analytic vocabulary'
        return profile

    for leaf in leaves:
        score_calls = _contains_score_call(leaf)
        if score_calls:
            if not isinstance(leaf, ast.ArithmeticComparisonExpression) or len(score_calls) != 1:
                profile.unanalyzable = True
                profile.unanalyzable_reason = 'score used in a form that can not be interpreted as a threshold comparison'
                return profile
            threshold = _literal_int(leaf.right)
            if threshold is None or _term_key(leaf.left) is not None or not _contains_score_call(leaf.left):
                profile.unanalyzable = True
                profile.unanalyzable_reason = 'score comparison is not "score-call op integer-literal"'
                return profile
            profile.scores.append(ScoreCondition(_score_key(score_calls[0]), leaf.type, threshold))
            continue

        if isinstance(leaf, ast.ComparisonExpression):
            if leaf.type == 'eq' and isinstance(leaf.left, ast.SymbolExpression) and leaf.left.name == 'status' \
                    and isinstance(leaf.right, ast.StringExpression):
                profile.statuses = profile.statuses | frozenset((leaf.right.evaluate(None),))
                continue
            term = _term_key(leaf.left)
            value = _literal_int(leaf.right)
            if term is not None and value is not None and leaf.type in _COMPARISON_OPS:
                profile.bounds.append(Bound(term, leaf.type, value))
                continue

        profile.unanalyzable = True
        profile.unanalyzable_reason = 'leaf outside the analytic vocabulary: {0}'.format(type(leaf).__name__)
        return profile
    return profile


# ---------------------------------------------------------------------------
# Interval feasibility over integer cents (never enumerates the 64-bit range)
# ---------------------------------------------------------------------------
def _apply_bound(interval, bound):
    low, high = interval
    if bound.op == 'ge':
        low = max(low, bound.value)
    elif bound.op == 'gt':
        low = max(low, bound.value + 1)
    elif bound.op == 'le':
        high = min(high, bound.value)
    elif bound.op == 'lt':
        high = min(high, bound.value - 1)
    elif bound.op == 'eq':
        low = max(low, bound.value)
        high = min(high, bound.value)
    return low, high


def _interval_feasible(profile):
    intervals = {
        'amount': [AMOUNT_MIN, AMOUNT_MAX],
        'refunded': [AMOUNT_MIN, AMOUNT_MAX],
        'balance': [AMOUNT_MIN, AMOUNT_MAX],
    }
    exclusions = collections.defaultdict(set)
    for bound in profile.bounds:
        if bound.op == 'ne':
            exclusions[bound.term].add(bound.value)
        else:
            intervals[bound.term] = _apply_bound(intervals[bound.term], bound)
    if any(low > high for low, high in intervals.values()):
        return False, intervals
    # a != exclusion only empties a term when its range has been pinned to that single value
    for term, excluded in exclusions.items():
        low, high = intervals[term]
        if low == high and low in excluded:
            return False, intervals
    # balance == amount - refunded with refunded <= amount must admit at least one integer pair
    amount_low, amount_high = intervals['amount']
    refunded_low, refunded_high = intervals['refunded']
    balance_low, balance_high = intervals['balance']
    if refunded_low > amount_high:
        return False, intervals
    # amount = refunded + balance; check the Minkowski-style sum intersects the amount interval
    sum_low = refunded_low + balance_low
    sum_high = refunded_high + balance_high
    if sum_high < amount_low or sum_low > amount_high:
        return False, intervals
    return True, intervals


# ---------------------------------------------------------------------------
# Concrete witness construction from a small, constant-derived candidate pool
# ---------------------------------------------------------------------------
def _candidate_pools(profiles):
    amount_values = {AMOUNT_MIN, GATEWAY_MIN, GATEWAY_MAX, AMOUNT_MAX,
                     REVIEW_BAND_MIN, REVIEW_BAND_MAX_EXCLUSIVE - 1, REVIEW_BAND_MAX_EXCLUSIVE}
    refunded_values = {AMOUNT_MIN, REFUNDED_THRESHOLD - 1, REFUNDED_THRESHOLD}
    for profile in profiles:
        for bound in profile.bounds:
            for offset in (-1, 0, 1):
                value = bound.value + offset
                if AMOUNT_MIN <= value <= AMOUNT_MAX:
                    if bound.term in ('amount', 'balance'):
                        amount_values.add(value)
                    if bound.term in ('refunded', 'balance'):
                        refunded_values.add(value)
    for value in list(amount_values):
        refunded_values.add(min(value, AMOUNT_MAX))
    return sorted(amount_values), sorted(refunded_values)


def _order_satisfies_analytic(order, profile):
    if profile.statuses and order['status'] not in profile.statuses:
        return False
    values = {
        'amount': order['amount'],
        'refunded': order['refunded'],
        'balance': order['amount'] - order['refunded'],
    }
    for bound in profile.bounds:
        if not _COMPARISON_OPS[bound.op](values[bound.term], bound.value):
            return False
    return True


def decide_without_score(rule, order):
    """Evaluate a conjunctive rule while treating score calls as unknown.

    Returns ``True``/``False`` when the answer is settled without any score call, or ``None`` when a score leaf
    decides the outcome. Analytic leaves are evaluated on the compiled AST, so the real engine semantics are used
    without ever calling the scoring service.
    """
    leaves = _conjuncts(rule.statement.expression)
    if leaves is None:
        return None
    saw_unknown = False
    for leaf in leaves:
        if _contains_score_call(leaf):
            saw_unknown = True
            continue
        if not bool(leaf.evaluate(order)):
            return False
    return None if saw_unknown else True


@dataclass
class RuleVerdict:
    position: int
    text: str
    verdict: str
    witness: dict = None
    intercepted_by: int = None
    score_key: tuple = None
    detail: str = ''


def _complementary_score_pair(profiles):
    """Positions of adjacent rules whose score conditions are complementary on an identical analytic region."""
    pairs = []
    for index in range(len(profiles) - 1):
        left, right = profiles[index], profiles[index + 1]
        if not (left.scores and right.scores):
            continue
        if left.statuses != right.statuses or left.bounds != right.bounds:
            continue
        left_score, right_score = left.scores[0], right.scores[0]
        if left_score.key != right_score.key:
            continue
        complementary = {
            ('ge', 'lt'), ('gt', 'le'), ('le', 'gt'), ('lt', 'ge'),
        }
        if (left_score.op, right_score.op) in complementary and left_score.value == right_score.value:
            pairs.append((index, index + 1, left_score.key))
    return pairs


def _find_witness(chain, profile, amount_pool, refunded_pool, tolerate_unknown=False):
    """Construct an order in the rule's analytic region that provably fails every settled earlier rule.

    When *tolerate_unknown* is set, an earlier rule whose result depends on a score unknown is allowed to remain
    unsettled (used for the second half of a complementary pair, whose sibling is exactly that unknown).
    """
    statuses = tuple(profile.statuses) if profile.statuses else STATUSES
    for status, amount, refunded in itertools.product(statuses, amount_pool, refunded_pool):
        order = {'status': status, 'amount': amount, 'refunded': refunded}
        if validate_order(order) or not _order_satisfies_analytic(order, profile):
            continue
        viable = True
        for earlier in range(profile.position):
            outcome = decide_without_score(chain[earlier].rule, order)
            if outcome is True:
                viable = False
                break
            if outcome is None and not tolerate_unknown:
                viable = False
                break
        if viable:
            return order
    return None


def analyze_chain(chain):
    profiles = [profile_rule(position, chain[position].rule) for position in range(len(chain))]
    amount_pool, refunded_pool = _candidate_pools(profiles)
    complement_pairs = _complementary_score_pair(profiles)
    decided = {}
    verdicts = [None] * len(profiles)

    # complementary pairs are decided together with one shared region witness; the unknown score only selects which
    # of the two adjacent rules is the first hit, so both can be certified without calling the scoring service
    for left_position, right_position, score_key in complement_pairs:
        region_profile = profiles[left_position]
        witness = _find_witness(chain, region_profile, amount_pool, refunded_pool, tolerate_unknown=True)
        if witness is None:
            continue
        decided[left_position] = witness
        decided[right_position] = witness
        verdicts[left_position] = RuleVerdict(
            left_position, region_profile.text, FIRST_HIT, witness=witness, score_key=score_key,
            detail=('shared region witness provably passes every settled earlier rule; the complementary neighbor R{0} '
                    'covers the other value of the shared unknown fact').format(right_position),
        )
        verdicts[right_position] = RuleVerdict(
            right_position, profiles[right_position].text, FIRST_HIT, witness=witness, score_key=score_key,
            detail=('same shared region; whichever value the unknown score takes, this rule or R{0} is the first '
                    'hit, so the band is fully covered').format(left_position),
        )

    for profile in profiles:
        position = profile.position
        if position in decided:
            continue

        if profile.unanalyzable:
            verdicts[position] = RuleVerdict(
                position, profile.text, UNKNOWN,
                detail='unknown is scoped to this rule only: {0}'.format(profile.unanalyzable_reason),
            )
            continue

        feasible, _intervals = _interval_feasible(profile)
        if not feasible:
            verdicts[position] = RuleVerdict(
                position, profile.text, UNSATISFIABLE,
                detail='analytic constraints have an empty integer region',
            )
            continue

        witness = _find_witness(chain, profile, amount_pool, refunded_pool)
        if witness is None:
            interceptor = _find_interceptor(chain, profile, amount_pool, refunded_pool)
            verdicts[position] = RuleVerdict(
                position, profile.text, SHADOWED,
                witness=interceptor[1], intercepted_by=interceptor[0],
                detail='constructible matches are all intercepted by an earlier rule',
            )
            continue

        if profile.scores:
            verdicts[position] = RuleVerdict(
                position, profile.text, UNKNOWN, witness=witness, score_key=profile.scores[0].key,
                detail='region is reachable but the first hit depends on an unpaired score unknown',
            )
        else:
            verdicts[position] = RuleVerdict(
                position, profile.text, FIRST_HIT, witness=witness,
                detail='concrete order provably fails every earlier rule',
            )
    return verdicts


def _find_interceptor(chain, profile, amount_pool, refunded_pool):
    statuses = tuple(profile.statuses) if profile.statuses else STATUSES
    for status, amount, refunded in itertools.product(statuses, amount_pool, refunded_pool):
        order = {'status': status, 'amount': amount, 'refunded': refunded}
        if validate_order(order) or not _order_satisfies_analytic(order, profile):
            continue
        for earlier in range(profile.position):
            if decide_without_score(chain[earlier].rule, order) is True:
                return earlier, order
    return None, None


# ---------------------------------------------------------------------------
# Live verification against the real compiled chain (score uses a local double)
# ---------------------------------------------------------------------------
def verify_witness(rule_texts, witness, expected_position, score_value=None):
    score_callable = score_double(score_value) if score_value is not None else unavailable_external_score
    chain = build_chain(rule_texts, score_callable=score_callable)
    assert not validate_order(witness)
    entry = chain.find(witness)
    if entry is None:
        raise AssertionError('witness {0} matched no rule'.format(witness))
    actual_position = chain.index(entry)
    if actual_position != expected_position:
        raise AssertionError('expected first hit at R{0} but got R{1} for {2}'.format(
            expected_position, actual_position, witness))
    return actual_position


def format_report(rule_texts, verdicts, title):
    lines = [title, '=' * len(title), '']
    for verdict in verdicts:
        lines.append('R{0}  {1}'.format(verdict.position, verdict.text))
        lines.append('     verdict : {0}'.format(verdict.verdict))
        if verdict.witness is not None:
            lines.append('     witness : status={status!r} amount={amount} refunded={refunded}'.format(**verdict.witness))
        if verdict.intercepted_by is not None:
            lines.append('     blocked by R{0}'.format(verdict.intercepted_by))
        if verdict.score_key is not None:
            lines.append('     unknown : {0}{1}'.format(verdict.score_key[0], verdict.score_key[1]))
        lines.append('     detail  : {0}'.format(verdict.detail))
        lines.append('')
    return os.linesep.join(lines)


def main():
    print('## Static precheck (scoring service never called)')
    print()
    revised = build_chain(REVISED_RULE_TEXTS)
    revised_verdicts = analyze_chain(revised)
    print(format_report(REVISED_RULE_TEXTS, revised_verdicts, 'Revised chain (balance boundary moved to 999)'))

    original = build_chain(ORIGINAL_RULE_TEXTS)
    original_verdicts = analyze_chain(original)
    print(format_report(ORIGINAL_RULE_TEXTS, original_verdicts, 'Original chain (balance boundary at 1000)'))

    corner = {'status': 'paid', 'amount': BALANCE_BOUNDARY_ORIGINAL, 'refunded': 0}
    print('## One-cent move corner witness')
    print('order:', corner)
    print('original chain first hit: R{0}'.format(
        verify_witness(ORIGINAL_RULE_TEXTS, corner, expected_position=0)))
    print('revised chain first hit : R{0}'.format(
        verify_witness(REVISED_RULE_TEXTS, corner, expected_position=2)))
    print()

    print('## Live replay of every ordinary witness through the compiled chain')
    pair_positions = {3, 4}
    pair_seen = False
    for verdict in revised_verdicts:
        if verdict.verdict != FIRST_HIT or verdict.witness is None:
            continue
        if verdict.position in pair_positions:
            if pair_seen:
                continue
            pair_seen = True
            # one shared region witness; the unknown score selects which of the pair hits first
            for score_value, expected in ((SCORE_CUTOFF, 3), (SCORE_CUTOFF - 1, 4)):
                position = verify_witness(REVISED_RULE_TEXTS, verdict.witness, expected, score_value)
                print('R{0} <- {1} score_double={2}'.format(position, verdict.witness, score_value))
        else:
            position = verify_witness(REVISED_RULE_TEXTS, verdict.witness, verdict.position)
            print('R{0} <- {1} (no score call)'.format(position, verdict.witness))
    print()

    print('## Complementary score pair covers the whole review band')
    for amount in (REVIEW_BAND_MIN, 15000, REVIEW_BAND_MAX_EXCLUSIVE - 1):
        order = {'status': 'review', 'amount': amount, 'refunded': 0}
        hit_positions = {_pair_position(REVISED_RULE_TEXTS, order, score_value)
                         for score_value in (0, SCORE_CUTOFF - 1, SCORE_CUTOFF, 99)}
        assert hit_positions <= {3, 4}
        print('amount={0}: intercepted at {1} for every score value'.format(amount, sorted(hit_positions)))
    print()

    print('## Distinguishing unsatisfiable rules from shadowed rules')
    unsat_texts = REVISED_RULE_TEXTS + [
        'status == "review" and amount >= {0} and amount < {1}'.format(
            REVIEW_BAND_MAX_EXCLUSIVE, REVIEW_BAND_MIN),
    ]
    unsat_chain = build_chain(unsat_texts)
    unsat_verdicts = analyze_chain(unsat_chain)
    print('empty-region rule verdict:', unsat_verdicts[-1].verdict, '-', unsat_verdicts[-1].detail)
    print('R2 original-chain verdict :', original_verdicts[2].verdict,
          '- witness {0} blocked by R{1}'.format(original_verdicts[2].witness, original_verdicts[2].intercepted_by))
    print()

    print('## Unknown scoped to the region that can depend on it')
    different_argument_text = (
        'status == "review" and amount >= {0} and '
        '$risk_score(amount, amount - refunded) >= {1}'
    ).format(REVIEW_BAND_MAX_EXCLUSIVE, SCORE_CUTOFF)
    scoped_chain = build_chain(REVISED_RULE_TEXTS + [different_argument_text])
    scoped_verdicts = analyze_chain(scoped_chain)
    scoped = scoped_verdicts[-1]
    print('different-argument rule verdict:', scoped.verdict, '-', scoped.detail)
    print('shared-expression score key in R3/R4:', revised_verdicts[3].score_key)
    print('different-argument score key        :', scoped.score_key)


def _pair_position(rule_texts, order, score_value):
    chain = build_chain(rule_texts, score_callable=score_double(score_value))
    return chain.index(chain.find(order))


if __name__ == '__main__':
    main()
