#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  tests/pushdown.py
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

import pathlib
import sys
import unittest

import rule_engine
import rule_engine.engine as engine
import rule_engine.errors as errors

try:
    import sqlalchemy as sa
    _HAS_SQLALCHEMY = True
except ImportError:
    _HAS_SQLALCHEMY = False

__all__ = ('PushdownSoundnessTests', 'RuleEngineNullSemanticsTests')


def _load_example():
    # Imported as a normal module (rather than a spec_from_file_location alias)
    # so SQLAlchemy can resolve the Mapped[...] annotations on the declarative classes.
    examples_dir = str(pathlib.Path(__file__).resolve().parent.parent / 'examples')
    if examples_dir not in sys.path:
        sys.path.insert(0, examples_dir)
    import pushdown_audit
    return pushdown_audit


@unittest.skipUnless(_HAS_SQLALCHEMY, 'sqlalchemy is not installed')
class PushdownSoundnessTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.example = _load_example()
        cls.session = cls.example.build_session()
        cls.outcomes = cls.example.original_outcomes(cls.session, cls.example.AuditSpy())
        cls.plans = cls.example.pushdown_plan(cls.session)

    def _survivors(self, plan):
        return self.example.sql_survivors(self.session, plan)

    def test_analysis_never_invokes_audit(self):
        # pushdown_plan builds its own fresh spy internally and asserts on it;
        # re-run analysis with an observable spy to keep the guarantee explicit.
        spy = self.example.AuditSpy()
        analyzer = self.example._PushdownAnalyzer(
                {'status': self.example.Order.status, 'batch_no': self.example.Order.batch_no})
        for name, text, ignorecase in self.example.RULES:
            rule = rule_engine.Rule(text, context=self.example.build_context(spy, ignorecase=ignorecase))
            analyzer.analyze(rule)
        self.assertEqual(spy.calls, [])

    def test_every_plan_is_sound(self):
        for name, _text, _ignorecase in self.example.RULES:
            with self.subTest(rule=name):
                original = self.outcomes[name]
                survivors = self._survivors(self.plans[name])
                provably_false = {order_id for order_id, fate in original.items() if fate is False}
                must_keep = set(original) - provably_false
                dropped = set(original) - survivors
                self.assertTrue(dropped <= provably_false,
                                f'{name} drops an order that matches or errors: {sorted(dropped - provably_false)}')
                self.assertTrue(must_keep <= survivors,
                                f'{name} loses a true/error order: {sorted(must_keep - survivors)}')

    def test_compare_first_prunes_status_mismatches_but_keeps_ready_residual(self):
        plan = self.plans['R1_compare_first']
        survivors = self._survivors(plan)
        # ready rows survive regardless of the unknown audit verdict (orders 1, 4, 7)
        self.assertEqual(survivors, {1, 4, 7})
        # the explicit-NULL order is provably false under == and must be pruned
        self.assertNotIn(3, survivors)

    def test_audit_first_has_empty_exclusion_boundary(self):
        plan = self.plans['R2_audit_first']
        # every row can reach the leading audit call, including the relation-less
        # order 5 that raises a missing-field error: nothing is provably safe to drop
        self.assertEqual(self._survivors(plan), set(self.outcomes['R2_audit_first']))
        self.assertEqual(
                self.session.query(self.example.Order.id)
                .filter(sa.not_(plan.exclude_filter)).count(),
                len(self.example.FIXTURE),
        )

    def test_not_comparison_keeps_null_rows_two_valued(self):
        survivors = self._survivors(self.plans['R3_not_comparison'])
        # not (status == "ready") is True on NULL, so order 3 must survive even
        # though the outer SQL NOT would otherwise turn NULL into UNKNOWN
        self.assertIn(3, survivors)
        self.assertEqual(survivors, {2, 3, 5, 6, 8, 9})

    def test_exclusive_branches_keep_every_error_and_match(self):
        survivors = self._survivors(self.plans['R4_exclusive_branches'])
        # both branches can call audit on any row, so no row is error-free
        self.assertEqual(survivors, set(self.outcomes['R4_exclusive_branches']))
        # erroring orders (missing relation or key-targeted error) are retained
        for order_id in (4, 5, 9):
            self.assertIn(order_id, survivors)

    def test_same_audit_fact_is_not_split_into_independent_facts(self):
        outcomes = self.outcomes['R5_same_fact_twice']
        # the two branches call one deterministic function: a False verdict makes
        # the rule a tautology, an error still propagates — never two independent draws
        self.assertTrue(outcomes[7])
        self.assertEqual(outcomes[1], True)
        self.assertEqual(outcomes[4], 'FunctionCallError')
        # non-ready rows are the only safe prune
        self.assertEqual(self._survivors(self.plans['R5_same_fact_twice']), {1, 4, 7})

    def test_nocase_regex_keeps_uppercase_only_match(self):
        survivors = self._survivors(self.plans['R6_nocase_regex'])
        # order 2 is 'READY' which only the NOCASE regex branch can match
        self.assertIn(2, survivors)
        # the literal-equality rule R1 must still prune it
        self.assertNotIn(2, self._survivors(self.plans['R1_compare_first']))
        # NULL discharges to '' and cannot match, so it is safely pruned here
        self.assertNotIn(3, survivors)

    def test_counterexample_pairs_share_sql_facts_but_diverge(self):
        r1_survivors = self._survivors(self.plans['R1_compare_first'])
        # orders 1 and 7 are both status='ready' with review rows: audit True vs False
        self.assertEqual({1, 7} <= r1_survivors, True)
        self.assertEqual(self.outcomes['R1_compare_first'][1], True)
        self.assertEqual(self.outcomes['R1_compare_first'][7], False)
        # orders 6 and 9 share every fact the rule can see in SQL (status plus the
        # review relationship's cardinality); only the JSON payload consumed inside
        # the pure function differs, which push-down is forbidden from inspecting
        order6 = self.session.get(self.example.Order, 6)
        order9 = self.session.get(self.example.Order, 9)
        self.assertEqual(order6.status, order9.status)
        self.assertEqual(len(order6.reviews), len(order9.reviews))
        self.assertNotEqual(order6.reviews[0].payload, order9.reviews[0].payload)
        r2_survivors = self._survivors(self.plans['R2_audit_first'])
        self.assertIn(6, r2_survivors)
        self.assertIn(9, r2_survivors)
        self.assertEqual(self.outcomes['R2_audit_first'][6], False)
        self.assertEqual(self.outcomes['R2_audit_first'][9], 'FunctionCallError')

    def test_unsupported_regex_shape_degrades_to_no_exclusion(self):
        spy = self.example.AuditSpy()
        rule = rule_engine.Rule('(status ?? "") =~ "rea.dy"',
                                context=self.example.build_context(spy, ignorecase=True))
        analyzer = self.example._PushdownAnalyzer(
                {'status': self.example.Order.status, 'batch_no': self.example.Order.batch_no})
        plan = analyzer.analyze(rule)
        self.assertTrue(plan.may_error)
        all_order_ids = {row[0] for row in self.example.FIXTURE}
        self.assertEqual(self._survivors(plan), all_order_ids)

    def test_sql_like_wildcards_also_degrade_to_no_exclusion(self):
        # '%' and '_' are SQL LIKE wildcards: a LIKE rewrite would not match the
        # regex semantics, so the analyzer must retain every row rather than mis-exclude
        spy = self.example.AuditSpy()
        rule = rule_engine.Rule('(status ?? "") =~ "ready_x"',
                                context=self.example.build_context(spy, ignorecase=True))
        analyzer = self.example._PushdownAnalyzer(
                {'status': self.example.Order.status, 'batch_no': self.example.Order.batch_no})
        plan = analyzer.analyze(rule)
        self.assertTrue(plan.may_error)
        all_order_ids = {row[0] for row in self.example.FIXTURE}
        self.assertEqual(self._survivors(plan), all_order_ids)

    def test_audit_is_deterministic_for_the_same_input(self):
        spy = self.example.AuditSpy()
        rule = rule_engine.Rule('$audit(reviews)', context=self.example.build_context(spy))
        order = self.session.get(self.example.Order, 1)
        first = rule.matches(order)
        second = rule.matches(order)
        self.assertEqual(first, second)
        self.assertEqual(len(spy.calls), 2)

    def test_single_residual_pass_reproduces_original_fates(self):
        for name, text, ignorecase in self.example.RULES:
            with self.subTest(rule=name):
                survivors = self._survivors(self.plans[name])
                residual = self.example.residual_evaluation(
                        self.session, self.example.AuditSpy(), name, text, ignorecase, survivors)
                expected = {order_id: self.outcomes[name][order_id] for order_id in survivors}
                self.assertEqual(residual, expected)


class RuleEngineNullSemanticsTests(unittest.TestCase):
    """Ground the push-down reasoning in the engine's actual three-valued behavior."""

    def setUp(self):
        self.calls = []

        def audit(reviews):
            self.calls.append(tuple(reviews))
            return True

        self.context = engine.Context(default_value=None)
        self.context.builtins = rule_engine.builtins.Builtins.from_defaults(
                {'audit': audit},
                value_types={'audit': rule_engine.types.DataType.FUNCTION(
                        'audit', return_type=rule_engine.types.DataType.BOOLEAN,
                        argument_types=(rule_engine.types.DataType.ARRAY(
                                rule_engine.types.DataType.UNDEFINED),))})

    def test_equality_never_yields_null(self):
        self.assertFalse(rule_engine.Rule('status == "ready"').matches({'status': None}))
        self.assertTrue(rule_engine.Rule('status != "ready"').matches({'status': None}))
        self.assertTrue(rule_engine.Rule('status == null').matches({'status': None}))
        self.assertTrue(rule_engine.Rule('not (status == "ready")').matches({'status': None}))

    def test_and_short_circuits_before_the_audit_call(self):
        rule = rule_engine.Rule('status == "ready" and $audit(reviews)', context=self.context)
        self.assertFalse(rule.matches({'status': 'blocked', 'reviews': []}))
        self.assertEqual(self.calls, [])

    def test_or_short_circuits_before_the_audit_call(self):
        rule = rule_engine.Rule('status == "ready" or $audit(reviews)', context=self.context)
        self.assertTrue(rule.matches({'status': 'ready', 'reviews': []}))
        self.assertEqual(self.calls, [])

    def test_missing_relation_error_propagates_as_function_call_error(self):
        def failing_audit(reviews):
            if not reviews:
                raise LookupError('missing review rows')
            return True

        context = engine.Context(default_value=None)
        context.builtins = rule_engine.builtins.Builtins.from_defaults(
                {'audit': failing_audit},
                value_types={'audit': rule_engine.types.DataType.FUNCTION(
                        'audit', return_type=rule_engine.types.DataType.BOOLEAN,
                        argument_types=(rule_engine.types.DataType.ARRAY(
                                rule_engine.types.DataType.UNDEFINED),))})
        rule = rule_engine.Rule('$audit(reviews)', context=context)
        with self.assertRaises(errors.FunctionCallError):
            rule.matches({'reviews': []})


if __name__ == '__main__':
    unittest.main()
