#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  tests/diagnostics.py
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

import unittest

import rule_engine
from rule_engine import errors

__all__ = ('DiagnosticsTests',)

# a batch inbound order audit rule: select the groups that are confirmed (a business condition), take the last two
# details of each group with a negative-start slice and check the weight of each one
RULE_TEXT = "[[d['weight'] >= 0 for d in group['details'][-2:]] for group in groups if group['status'] == '已确认']"
# the same rule with an independent comparison on a same-named field appended
RULE_TEXT_FULL = RULE_TEXT + " and weight >= 0"

class _LandmineDetail(object):
    """A detail that records and refuses any read, standing in for a record that must be short-circuited."""
    def __init__(self) -> None:
        self.reads = 0
    def __getitem__(self, key):
        self.reads += 1
        raise AssertionError("landmine detail was read: {0!r}".format(key))

class DiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.landmines = [_LandmineDetail() for _ in range(4)]
        # one and the same wrong-weight detail object placed at two positions of the original arrays
        self.bad_detail = {'weight': '残次'}
        self.order = {
                'groups': [
                        {'status': '已确认', 'details': [self.landmines[0], {'weight': 1}, self.bad_detail, {'weight': 2}]},
                        {'status': '已确认', 'details': [{'weight': 3}, self.bad_detail, {'weight': 4}]},
                        {'status': '已取消', 'details': [self.landmines[1], self.landmines[2]]},
                        # the selection condition itself fails on this group, so its details can not be evaluated
                        {'details': [self.landmines[3]]},
                ],
                'weight': '待复核',
        }
        self.rule = rule_engine.Rule(RULE_TEXT)
        self.rule_full = rule_engine.Rule(RULE_TEXT_FULL)

    def assert_landmines_unread(self) -> None:
        for landmine in self.landmines:
            self.assertEqual(landmine.reads, 0)

    def test_collects_all_independent_errors(self):
        report = self.rule_full.evaluate_diagnostics(self.order)
        self.assertFalse(report.ok)
        self.assertFalse(bool(report))
        self.assertIs(report.value, errors.UNDEFINED)
        self.assertIsInstance(report.errors, tuple)
        self.assertEqual(len(report.errors), 4)
        # the errors are independent of each other and are reported in evaluation order: the wrong-weight detail in
        # the first group, the same object again in the second group (after the first group's output accumulated),
        # the selection condition of the last group, and the independent comparison
        self.assertIsInstance(report.errors[0].error, errors.EvaluationError)
        self.assertIsInstance(report.errors[1].error, errors.EvaluationError)
        self.assertIsInstance(report.errors[2].error, errors.LookupError)
        self.assertIsInstance(report.errors[3].error, errors.EvaluationError)

    def test_partial_value_is_kept(self):
        report = self.rule.evaluate_diagnostics(self.order)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 3)
        # the output that accumulated before and after each failure is kept: each selected group contributed the
        # results of the details that could be evaluated
        self.assertEqual(report.value, ((True,), (True,)))

    def test_original_positions_not_projected_positions(self):
        report = self.rule.evaluate_diagnostics(self.order)
        first, second = report.errors[0], report.errors[1]
        # the same detail object projects to position 0 of both groups' results, but each occurrence is pointed back
        # to its own position in the original arrays, through the negative-start slice
        self.assertEqual(
                first.path,
                (
                        rule_engine.DiagnosticPathEntry(expression='groups', variable='group', index=0),
                        rule_engine.DiagnosticPathEntry(expression="group['details'][-2:]", variable='d', index=2),
                )
        )
        self.assertEqual(
                second.path,
                (
                        rule_engine.DiagnosticPathEntry(expression='groups', variable='group', index=1),
                        rule_engine.DiagnosticPathEntry(expression="group['details'][-2:]", variable='d', index=1),
                )
        )
        self.assertNotEqual(first.path, second.path)
        # both occurrences failed on the same segment of the rule text
        self.assertEqual(first.span, second.span)
        self.assertEqual(first.segment, "d['weight'] >= 0")
        self.assertEqual(second.segment, "d['weight'] >= 0")

    def test_span_points_at_the_failing_segment(self):
        report = self.rule_full.evaluate_diagnostics(self.order)
        text = self.rule_full.text
        expected_segments = (
                "d['weight'] >= 0",
                "d['weight'] >= 0",
                "group['status']",
                'weight >= 0',
        )
        for diagnostic, segment in zip(report.errors, expected_segments):
            self.assertIsNotNone(diagnostic.span)
            start, end = diagnostic.span
            # the offsets select exactly the failing segment, so an interface can highlight the characters directly
            self.assertEqual(text[start:end], segment)
            self.assertEqual(diagnostic.segment, segment)
        # the offsets are character offsets: the independent comparison sits behind the Chinese constant '已确认'
        # and is still selected exactly
        self.assertGreater(report.errors[3].span[0], text.index('已确认'))
        self.assertEqual(text[report.errors[3].span[0]:report.errors[3].span[1]], 'weight >= 0')

    def test_original_exception_is_preserved(self):
        report = self.rule.evaluate_diagnostics(self.order)
        condition_error = report.errors[2].error
        self.assertIsInstance(condition_error, errors.LookupError)
        # the original exception carries its own details, here the key the selection condition looked up
        self.assertEqual(condition_error.item, 'status')
        for diagnostic in report.errors:
            self.assertIsInstance(diagnostic.error, errors.EvaluationError)

    def test_short_circuited_details_are_not_read(self):
        self.rule_full.evaluate_diagnostics(self.order)
        # the detail excluded by the negative-start slice, the details of the group the business condition
        # deselects, and the details gated by the failing selection condition are neither read nor reported
        self.assert_landmines_unread()
        with self.assertRaises(errors.EvaluationError):
            self.rule_full.evaluate(self.order)
        self.assert_landmines_unread()

    def test_condition_error_has_no_downstream_cascade(self):
        report = self.rule.evaluate_diagnostics(self.order)
        # exactly one error is reported for the group whose selection condition fails: the condition error itself,
        # not a series of errors for the details it gated which were never evaluated
        group_three_errors = [
                diagnostic for diagnostic in report.errors
                if any(entry.expression == 'groups' and entry.index == 3 for entry in diagnostic.path)
        ]
        self.assertEqual(len(group_three_errors), 1)
        diagnostic = group_three_errors[0]
        self.assertIsInstance(diagnostic.error, errors.LookupError)
        self.assertEqual(diagnostic.segment, "group['status']")
        self.assertEqual(len(diagnostic.path), 1)
        # the other, independent groups still completed
        self.assertEqual(report.value, ((True,), (True,)))

    def test_independent_comparison_beside_same_named_field(self):
        report = self.rule_full.evaluate_diagnostics(self.order)
        comparison = report.errors[3]
        # the independent comparison fails beside another field named 'weight' than the comprehension's, and the
        # diagnostic distinguishes it from the comprehension's failures by its position in the rule text
        self.assertEqual(comparison.segment, 'weight >= 0')
        self.assertEqual(comparison.path, ())
        self.assertNotEqual(comparison.span, report.errors[0].span)

    def test_logic_short_circuit_is_preserved(self):
        # a false left side decides an 'and', the right side is not evaluated
        rule = rule_engine.Rule("groups[0]['status'] == '已取消' and weight >= 0")
        report = rule.evaluate_diagnostics(self.order)
        self.assertTrue(report.ok)
        self.assertFalse(report.value)
        # a true left side of an 'and' lets the right side fail on its own
        rule = rule_engine.Rule("groups[0]['status'] == '已确认' and weight >= 0")
        report = rule.evaluate_diagnostics(self.order)
        self.assertFalse(report.ok)
        self.assertEqual(len(report.errors), 1)
        self.assertEqual(report.errors[0].segment, 'weight >= 0')
        self.assertIs(report.value, errors.UNDEFINED)
        # when the left side itself fails, the independent right side is still evaluated and both errors are
        # collected
        rule = rule_engine.Rule("missing and weight >= 0")
        report = rule.evaluate_diagnostics(self.order)
        self.assertEqual(len(report.errors), 2)
        self.assertIsInstance(report.errors[0].error, errors.SymbolResolutionError)
        self.assertEqual(report.errors[0].segment, 'missing')
        self.assertEqual(report.errors[1].segment, 'weight >= 0')
        self.assertIs(report.value, errors.UNDEFINED)

    def test_default_contract_is_unchanged(self):
        # the default, non-diagnostic execution keeps raising the first error in evaluation order
        with self.assertRaises(errors.EvaluationError) as ctx:
            self.rule_full.evaluate(self.order)
        self.assertEqual(str(ctx.exception), 'data type mismatch')
        with self.assertRaises(errors.EvaluationError):
            self.rule_full.matches(self.order)
        with self.assertRaises(errors.EvaluationError):
            list(self.rule_full.filter([self.order]))
        self.assert_landmines_unread()
        # a clean order evaluates the same way in both modes
        clean_order = {
                'groups': [
                        {'status': '已确认', 'details': [{'weight': 0}, {'weight': 1}, {'weight': 2}]},
                        {'status': '已取消', 'details': [self.landmines[0]]},
                ],
                'weight': 10,
        }
        self.assertTrue(self.rule_full.matches(clean_order))
        report = self.rule_full.evaluate_diagnostics(clean_order)
        self.assertTrue(report.ok)
        self.assertTrue(bool(report))
        self.assertEqual(report.errors, ())
        self.assertEqual(report.value, self.rule_full.evaluate(clean_order))
        self.assert_landmines_unread()

    def test_report_and_diagnostic_api(self):
        report = self.rule.evaluate_diagnostics(self.order)
        self.assertEqual(report.text, self.rule.text)
        diagnostic = report.errors[0]
        self.assertIn('Diagnostic', repr(diagnostic))
        self.assertIn('span', repr(diagnostic))
        entry = diagnostic.path[0]
        self.assertIn('groups', repr(entry))
        self.assertNotEqual(entry, 'groups')
        self.assertIn('DiagnosticsReport', repr(report))
        self.assertIn('Diagnostic', rule_engine.__all__)
        self.assertIn('DiagnosticPathEntry', rule_engine.__all__)
        self.assertIn('DiagnosticsReport', rule_engine.__all__)

    def test_statement_nodes_have_spans(self):
        self.assertEqual(self.rule_full.statement.expression.span, (0, len(self.rule_full.text)))

if __name__ == '__main__':
    unittest.main()
