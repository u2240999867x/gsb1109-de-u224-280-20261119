#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  tests/compat.py
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

import pickle
import threading
import unittest

import rule_engine
import rule_engine.builtins as builtins
import rule_engine.engine as engine
import rule_engine.errors as errors
from rule_engine import DataType

__all__ = ('LegacyCompatTests', 'ResumableMappingTests', 'PendingEvaluationTests', 'LegacyCompatDirectPathTests')

def _order(order_id, *, current0, legacy0, current1, legacy1, signed=None):
    item0 = {'current': current0, 'legacy': legacy0}
    item1 = {'current': current1, 'legacy': legacy1}
    if signed is not None:
        item0['signed'] = signed
    return {'order_id': order_id, 'items': [item0, item1]}

class ControllableProvider(object):
    """A legacy data provider whose answers and lateness are fully controlled by the test."""
    def __init__(self, *, deferred=False, answers=None, missing=()):
        self.deferred = deferred
        self.answers = answers or {}
        # (order_id, key) pairs whose legacy data has definitively not arrived
        self.missing = set(missing)
        self.calls = []

    def __call__(self, root, group, key):
        order_id = root['order_id']
        self.calls.append((order_id, group, key))
        if self.deferred or (order_id, key) in self.missing:
            if (order_id, key) in self.missing:
                # a definitive "there is no legacy grade" answer, distinct from "still waiting"
                raise KeyError(key)
            raise rule_engine.DeferredDataError('legacy data for {0!r} is late'.format(key))
        return self.answers[order_id][key]

def _once_only_builtin(context, bucket):
    def once_only(value):
        bucket.append(value)
        if len(bucket) > 1:
            raise AssertionError('the once-only business check was executed more than once')
        return True
    context.builtins = builtins.Builtins.from_defaults(
            values={'once_only': once_only},
            value_types={'once_only': DataType.FUNCTION(
                    'once_only',
                    return_type=DataType.BOOLEAN,
                    argument_types=(DataType.UNDEFINED,)
            )}
    )

class LegacyCompatTests(unittest.TestCase):
    def _make_chain(self, context):
        # entry 0: a reject-on-failure rule that runs the once-only business check over the first detail line
        guard_rule = '$once_only(items[0].current.grade) == false'
        # entry 1: needs the second line's missing grade (legacy) and its genuinely present reason
        suspend_rule = "items[1].current.grade == 'OLD-B' and items[1].current.reason == 'r2'"
        return engine.Chain([
                (guard_rule, 'REJECT-CHECK'),
                (suspend_rule, 'ACCEPT'),
        ], default_value='DROP', context=context)

    def test_suspend_resume_keeps_one_chain_evaluation(self):
        provider = ControllableProvider(deferred=True)
        context = engine.LegacyGradeContext(provider)
        check_bucket = []
        _once_only_builtin(context, check_bucket)
        chain = self._make_chain(context)
        order = _order(
                'O-1',
                current0={'grade': 'A', 'reason': 'ok'}, legacy0={'grade': 'OLD-A'},
                current1={'reason': 'r2'}, legacy1={'grade': 'OLD-B'}
        )

        threads_before = threading.enumerate()
        pending = chain.evaluate(order)
        threads_after = threading.enumerate()
        self.assertIsInstance(pending, engine.PendingEvaluation)
        # suspending returns to the caller immediately without keeping a worker thread busy
        self.assertEqual(threads_before, threads_after)
        self.assertEqual(pending.key, 'grade')
        self.assertFalse(pending.is_done)
        self.assertEqual(provider.calls, [('O-1', 'legacy', 'grade')])
        self.assertEqual(check_bucket, ['A'])

        thing = pending.thing
        # fields before the suspension point were really read; the missing grade and the reason after it were not
        self.assertIn(('items', 0, 'current', 'grade'), thing.access_log)
        self.assertIn(('items', 1, 'current'), thing.access_log)
        self.assertNotIn(('items', 1, 'current', 'grade'), thing.access_log)
        self.assertNotIn(('items', 1, 'current', 'reason'), thing.access_log)
        log_at_suspension = list(thing.access_log)

        # another order runs meanwhile on its own context and chain and sees only its own rules and scope
        other_provider = ControllableProvider(answers={'O-2': {'grade': 'OLD-X'}})
        other_context = engine.LegacyGradeContext(other_provider)
        other_chain = engine.Chain(
                [("items[0].current.grade == 'OLD-X'", 'ACCEPT2')],
                default_value='DROP2',
                context=other_context
        )
        other_order = _order(
                'O-2',
                current0={}, legacy0={'grade': 'OLD-X'},
                current1={'reason': 'r'}, legacy1={}
        )
        self.assertEqual(other_chain.evaluate(other_order), 'ACCEPT2')
        self.assertEqual(other_provider.calls, [('O-2', 'legacy', 'grade')])
        # the independent evaluation never touched the first order's provider or wrapper
        self.assertEqual(provider.calls, [('O-1', 'legacy', 'grade')])
        self.assertEqual(thing.access_log, log_at_suspension)

        # publish a new rule version; the suspended handle keeps the snapshot it started with
        chain.append(engine.Rule("items[1].current.grade == 'OLD-B'", context=context), 'NEW-VERSION')
        self.assertEqual(len(chain), 3)
        self.assertEqual(len(pending._entries), 2)

        # resume with a valid legacy grade: the same chain evaluation continues and first-hit is unchanged
        provider.deferred = False
        provider.answers = {'O-1': {'grade': 'OLD-B'}}
        result = pending.resume('OLD-B')
        self.assertEqual(result, 'ACCEPT')
        self.assertTrue(pending.is_done)
        # the business check and the first detail line are not revisited
        self.assertEqual(check_bucket, ['A'])
        self.assertEqual(provider.calls, [('O-1', 'legacy', 'grade')])
        new_reads = thing.access_log[len(log_at_suspension):]
        self.assertNotIn(('items', 0, 'current', 'grade'), new_reads)
        self.assertIn(('items', 1, 'current', 'reason'), new_reads)

        # the same legacy field can only continue the evaluation once
        with self.assertRaisesRegex(errors.EvaluationError, r'already been resumed'):
            pending.resume('OLD-B')

    def test_resume_without_legacy_grade_reports_real_missing_error(self):
        provider = ControllableProvider(deferred=True)
        context = engine.LegacyGradeContext(provider)
        chain = engine.Chain([
                ("items[1].current.grade == 'OLD-B'", 'ACCEPT'),
        ], default_value='DROP', context=context)
        order = _order(
                'O-3',
                current0={'grade': 'A'}, legacy0={'grade': 'OLD-A'},
                current1={'reason': 'r2'}, legacy1={}
        )
        pending = chain.evaluate(order)
        self.assertIsInstance(pending, engine.PendingEvaluation)

        # the late data arrived but carries no legacy grade: the definitive miss raises the normal resolution error,
        # it neither matches nor falls through to another rule
        provider.deferred = False
        provider.missing.add(('O-3', 'grade'))
        with self.assertRaises(errors.AttributeResolutionError):
            pending.resume(rule_engine.DeferredValue)

    def test_still_deferred_resume_returns_a_new_pending_handle(self):
        provider = ControllableProvider(deferred=True)
        context = engine.LegacyGradeContext(provider)
        chain = engine.Chain([
                ("items[1].current.grade == 'OLD-B'", 'ACCEPT'),
        ], default_value='DROP', context=context)
        order = _order(
                'O-4',
                current0={'grade': 'A'}, legacy0={},
                current1={}, legacy1={'grade': 'OLD-B'}
        )
        first = chain.evaluate(order)
        self.assertIsInstance(first, engine.PendingEvaluation)
        # asking again while the data is still late keeps waiting instead of rejecting the order
        second = first.resume(rule_engine.DeferredValue)
        self.assertIsInstance(second, engine.PendingEvaluation)
        self.assertIsNot(second, first)
        self.assertTrue(first.is_done)
        self.assertFalse(second.is_done)
        self.assertEqual(provider.calls, [('O-4', 'legacy', 'grade'), ('O-4', 'legacy', 'grade')])
        # the second handle is the one that now continues the evaluation
        provider.deferred = False
        provider.answers = {'O-4': {'grade': 'OLD-B'}}
        self.assertEqual(second.resume('OLD-B'), 'ACCEPT')

    def test_resume_re_asks_the_provider_after_late_data_arrived(self):
        provider = ControllableProvider(deferred=True)
        context = engine.LegacyGradeContext(provider)
        chain = engine.Chain([
                ("items[1].current.grade == 'OLD-B'", 'ACCEPT'),
        ], default_value='DROP', context=context)
        order = _order(
                'O-4b',
                current0={'grade': 'A'}, legacy0={},
                current1={}, legacy1={'grade': 'OLD-B'}
        )
        pending = chain.evaluate(order)
        self.assertIsInstance(pending, engine.PendingEvaluation)
        self.assertEqual(provider.calls, [('O-4b', 'legacy', 'grade')])
        # the late data lands in the provider, so resuming without an explicit answer re-asks and completes
        provider.deferred = False
        provider.answers = {'O-4b': {'grade': 'OLD-B'}}
        self.assertEqual(pending.resume(rule_engine.DeferredValue), 'ACCEPT')
        self.assertEqual(provider.calls, [('O-4b', 'legacy', 'grade'), ('O-4b', 'legacy', 'grade')])

    def test_missing_reason_after_grade_still_raises_real_error(self):
        provider = ControllableProvider(deferred=True)
        context = engine.LegacyGradeContext(provider)
        chain = engine.Chain([
                ("items[1].current.grade == 'OLD-B' and items[1].current.reason == 'r2'", 'ACCEPT'),
        ], default_value='DROP', context=context)
        # the second line has no reason at all, only the missing grade can be answered from legacy data
        order = _order(
                'O-5',
                current0={'grade': 'A'}, legacy0={},
                current1={}, legacy1={'grade': 'OLD-B'}
        )
        pending = chain.evaluate(order)
        self.assertIsInstance(pending, engine.PendingEvaluation)
        provider.deferred = False
        provider.answers = {'O-5': {'grade': 'OLD-B'}}
        with self.assertRaises(errors.AttributeResolutionError):
            pending.resume('OLD-B')

    def test_legacy_grade_does_not_migrate_to_other_groups(self):
        provider = ControllableProvider(deferred=True)
        context = engine.LegacyGradeContext(provider)
        chain = engine.Chain([
                ("items[1].current.grade == 'OLD-B' and items[0].current.grade == 'A' "
                 "and items[1].legacy.grade == 'OLD-B' and items[0].signed.grade == 'S-1'", 'ACCEPT'),
        ], default_value='DROP', context=context)
        order = _order(
                'O-6',
                current0={'grade': 'A'}, legacy0={'grade': 'OLD-A'},
                current1={}, legacy1={'grade': 'OLD-B'},
                signed={'grade': 'S-1'}
        )
        pending = chain.evaluate(order)
        self.assertIsInstance(pending, engine.PendingEvaluation)
        provider.deferred = False
        provider.answers = {'O-6': {'grade': 'OLD-B'}}
        self.assertEqual(pending.resume('OLD-B'), 'ACCEPT')

        wrapper = context.wrap(order)
        # each same-named grade keeps its own scope; the filled-in legacy value never overwrites the other groups
        self.assertEqual(wrapper['items'][0]['current']['grade'], 'A')
        self.assertEqual(wrapper['items'][1]['legacy']['grade'], 'OLD-B')
        self.assertEqual(wrapper['items'][0]['signed']['grade'], 'S-1')

    def test_present_null_wrong_type_and_signed_group_keep_original_behavior(self):
        # an explicit null grade is used as null and never reaches the provider
        provider = ControllableProvider(answers={'O-7': {'grade': 'OLD-B'}})
        context = engine.LegacyGradeContext(provider)
        chain = engine.Chain([('items[1].current.grade == null', 'NULL')], default_value='DROP', context=context)
        order = _order(
                'O-7',
                current0={'grade': 'A'}, legacy0={},
                current1={'grade': None}, legacy1={'grade': 'OLD-B'}
        )
        self.assertEqual(chain.evaluate(order), 'NULL')
        self.assertEqual(provider.calls, [])

        # a present value of the wrong type raises the normal type error instead of falling back to legacy data
        typed_context = engine.LegacyGradeContext(provider)
        typed_context.resolve_type = lambda name, scope=None: DataType.STRING if name == 'grade' else DataType.UNDEFINED
        wrong_type_rule = engine.Rule('grade == 1', context=typed_context)
        with self.assertRaises(errors.SymbolTypeError):
            wrong_type_rule.matches(typed_context.wrap({'current': {'grade': 123}, 'legacy': {}})['current'])
        self.assertEqual(provider.calls, [])

        # a missing grade on the signed group is an ordinary missing symbol, not a legacy fallback
        signed_chain = engine.Chain(
                [('items[0].signed.grade == "S-1"', 'S')],
                default_value='DROP',
                context=engine.LegacyGradeContext(provider)
        )
        signed_order = _order(
                'O-8',
                current0={'grade': 'A'}, legacy0={},
                current1={}, legacy1={},
                signed={}
        )
        with self.assertRaises(errors.AttributeResolutionError):
            signed_chain.evaluate(signed_order)
        self.assertEqual(provider.calls, [])

    def test_bracket_access_suspends_on_the_same_missing_grade(self):
        provider = ControllableProvider(deferred=True)
        context = engine.LegacyGradeContext(provider)
        chain = engine.Chain([
                ('items[1].current["grade"] == "OLD-B"', 'ACCEPT'),
        ], default_value='DROP', context=context)
        order = _order(
                'O-9',
                current0={'grade': 'A'}, legacy0={},
                current1={}, legacy1={'grade': 'OLD-B'}
        )
        pending = chain.evaluate(order)
        self.assertIsInstance(pending, engine.PendingEvaluation)
        self.assertEqual(pending.key, 'grade')
        provider.deferred = False
        provider.answers = {'O-9': {'grade': 'OLD-B'}}
        self.assertEqual(pending.resume('OLD-B'), 'ACCEPT')

    def test_many_suspended_orders_hold_no_threads_and_resume_independently(self):
        provider = ControllableProvider(deferred=True)
        context = engine.LegacyGradeContext(provider)
        chain = engine.Chain([
                ("items[1].current.grade == 'OLD-B'", 'ACCEPT'),
        ], default_value='DROP', context=context)
        order_template = _order(
                'O-T',
                current0={'grade': 'A'}, legacy0={},
                current1={}, legacy1={'grade': 'OLD-B'}
        )

        baseline_threads = threading.enumerate()
        handles = []
        for index in range(25):
            order = dict(order_template, order_id='O-T-{0}'.format(index))
            pending = chain.evaluate(order)
            self.assertIsInstance(pending, engine.PendingEvaluation)
            handles.append((order['order_id'], pending))
        # every order is waiting but the engine created no worker thread for any of them
        self.assertEqual(threading.enumerate(), baseline_threads)
        self.assertEqual(len(provider.calls), 25)

        provider.deferred = False
        provider.answers = {order_id: {'grade': 'OLD-B'} for order_id, _ in handles}
        # each handle continues only its own evaluation and reaches its own first-hit
        for order_id, pending in handles:
            self.assertEqual(pending.resume('OLD-B'), 'ACCEPT')
            self.assertTrue(pending.is_done)
        # the 25 initial waits were the only provider lookups; resumes used the supplied answers
        self.assertEqual(len(provider.calls), 25)

class ResumableMappingTests(unittest.TestCase):
    def test_present_keys_are_cached_and_logged_once(self):
        provider = ControllableProvider()
        context = engine.LegacyGradeContext(provider)
        order = _order(
                'O-10',
                current0={'grade': 'A'}, legacy0={},
                current1={'grade': 'B'}, legacy1={}
        )
        wrapper = context.wrap(order)
        self.assertEqual(wrapper['items'][0]['current']['grade'], 'A')
        self.assertEqual(wrapper['items'][0]['current']['grade'], 'A')
        self.assertEqual(wrapper.access_log.count(('items', 0, 'current', 'grade')), 1)
        self.assertEqual(provider.calls, [])

    def test_missing_grade_uses_provider_value_without_a_new_wait(self):
        provider = ControllableProvider(answers={'O-11': {'grade': 'OLD-B'}})
        context = engine.LegacyGradeContext(provider)
        order = _order(
                'O-11',
                current0={'grade': 'A'}, legacy0={},
                current1={}, legacy1={'grade': 'OLD-B'}
        )
        result = engine.Chain([
                ("items[1].current.grade == 'OLD-B'", 'ACCEPT'),
        ], default_value='DROP', context=context).evaluate(order)
        self.assertEqual(result, 'ACCEPT')
        self.assertEqual(provider.calls, [('O-11', 'legacy', 'grade')])

    def test_provider_sentinel_and_exception_both_suspend(self):
        for mode in ('exception', 'sentinel'):
            with self.subTest(mode=mode):
                if mode == 'exception':
                    provider = ControllableProvider(deferred=True)
                else:
                    class SentinelProvider(ControllableProvider):
                        def __call__(self, root, group, key):
                            self.calls.append((root['order_id'], group, key))
                            return rule_engine.DeferredValue
                    provider = SentinelProvider()
                context = engine.LegacyGradeContext(provider)
                order = _order(
                        'O-12',
                        current0={'grade': 'A'}, legacy0={},
                        current1={}, legacy1={}
                )
                pending = engine.Chain([
                        ("items[1].current.grade == 'X'", 'ACCEPT'),
                ], default_value='DROP', context=context).evaluate(order)
                self.assertIsInstance(pending, engine.PendingEvaluation)

    def test_unrelated_missing_symbol_does_not_reach_provider(self):
        provider = ControllableProvider()
        context = engine.LegacyGradeContext(provider)
        order = _order(
                'O-13',
                current0={'grade': 'A'}, legacy0={},
                current1={}, legacy1={}
        )
        with self.assertRaises(errors.AttributeResolutionError):
            engine.Chain([('items[1].current.unknown_field == 1', 'X')], context=context).evaluate(order)
        self.assertEqual(provider.calls, [])

    def test_explicit_none_from_provider_is_a_real_answer(self):
        class NoneProvider(ControllableProvider):
            def __call__(self, root, group, key):
                super().__call__(root, group, key)
                return None
        provider = NoneProvider(answers={'O-14': {'grade': None}})
        context = engine.LegacyGradeContext(provider)
        order = _order(
                'O-14',
                current0={'grade': 'A'}, legacy0={},
                current1={}, legacy1={}
        )
        result = engine.Chain([
                ('items[1].current.grade == null', 'NULL'),
        ], default_value='DROP', context=context).evaluate(order)
        self.assertEqual(result, 'NULL')

class PendingEvaluationTests(unittest.TestCase):
    def test_resume_uses_chain_default_value_when_no_rule_matches(self):
        provider = ControllableProvider(deferred=True)
        context = engine.LegacyGradeContext(provider)
        chain = engine.Chain([
                ("items[1].current.grade == 'NO-MATCH'", 'ACCEPT'),
        ], default_value='DROP', context=context)
        order = _order(
                'O-15',
                current0={'grade': 'A'}, legacy0={},
                current1={}, legacy1={'grade': 'OLD-B'}
        )
        pending = chain.evaluate(order)
        provider.deferred = False
        provider.answers = {'O-15': {'grade': 'OLD-B'}}
        self.assertEqual(pending.resume('OLD-B'), 'DROP')

    def test_exhausted_chain_without_default_raises_on_resume(self):
        provider = ControllableProvider(deferred=True)
        context = engine.LegacyGradeContext(provider)
        chain = engine.Chain([
                ("items[1].current.grade == 'NO-MATCH'", 'ACCEPT'),
        ], context=context)
        order = _order(
                'O-16',
                current0={'grade': 'A'}, legacy0={},
                current1={}, legacy1={'grade': 'OLD-B'}
        )
        pending = chain.evaluate(order)
        provider.deferred = False
        provider.answers = {'O-16': {'grade': 'OLD-B'}}
        with self.assertRaises(errors.ChainExhaustedError):
            pending.resume('OLD-B')

    def test_chained_pending_fields_each_continue_once(self):
        # two detail lines are both missing their grade; each wait produces its own handle in order
        provider = ControllableProvider(deferred=True)
        context = engine.LegacyGradeContext(provider)
        chain = engine.Chain([
                ("items[0].current.grade == 'OLD-A' and items[1].current.grade == 'OLD-B'", 'ACCEPT'),
        ], default_value='DROP', context=context)
        order = _order(
                'O-17',
                current0={}, legacy0={'grade': 'OLD-A'},
                current1={}, legacy1={'grade': 'OLD-B'}
        )
        first = chain.evaluate(order)
        self.assertIsInstance(first, engine.PendingEvaluation)
        self.assertEqual(first.key, 'grade')
        # answer the first wait; the second one suspends next
        second = first.resume('OLD-A')
        self.assertIsInstance(second, engine.PendingEvaluation)
        self.assertIsNot(second, first)
        provider.deferred = False
        provider.answers = {'O-17': {'grade': 'OLD-B'}}
        self.assertEqual(second.resume('OLD-B'), 'ACCEPT')
        # the first handle can not continue anything a second time
        with self.assertRaises(errors.EvaluationError):
            first.resume('OLD-A')

    def test_snapshot_is_independent_of_later_chain_publication(self):
        provider = ControllableProvider(deferred=True)
        context = engine.LegacyGradeContext(provider)
        chain = engine.Chain([
                ("items[1].current.grade == 'OLD-B'", 'OLD'),
        ], default_value='DROP', context=context)
        order = _order(
                'O-18',
                current0={'grade': 'A'}, legacy0={},
                current1={}, legacy1={'grade': 'OLD-B'}
        )
        pending = chain.evaluate(order)
        chain.append(engine.Rule("items[1].current.grade == 'OLD-B'", context=context), 'NEW')
        provider.deferred = False
        provider.answers = {'O-18': {'grade': 'OLD-B'}}
        # the resumed evaluation honors the snapshot's entry and value, not the rule published afterwards
        self.assertEqual(pending.resume('OLD-B'), 'OLD')
        # the chain itself now contains the newly published rule version, which a later evaluation would use
        self.assertEqual(len(chain), 2)
        self.assertEqual(chain[1].value, 'NEW')

class LegacyCompatDirectPathTests(unittest.TestCase):
    def test_ordinary_context_keeps_direct_calls(self):
        chain = engine.Chain([('missing == 1', 'X'), ('true', 'Y')])
        with self.assertRaises(errors.SymbolResolutionError):
            chain.evaluate({})
        self.assertEqual(chain.evaluate({'missing': 1}), 'X')

    def test_find_is_rejected_on_a_resumable_context(self):
        context = engine.LegacyGradeContext(ControllableProvider())
        chain = engine.Chain([('true', 'X')], context=context)
        with self.assertRaisesRegex(errors.EvaluationError, r'PendingEvaluation'):
            chain.find({})
        # ordinary contexts are unaffected
        self.assertIsNotNone(engine.Chain([('true', 'X')]).find({}))

    def test_resumable_context_with_ready_data_returns_directly(self):
        provider = ControllableProvider(answers={'O-19': {'grade': 'OLD-B'}})
        context = engine.LegacyGradeContext(provider)
        chain = engine.Chain([
                ("items[1].current.grade == 'OLD-B'", 'ACCEPT'),
        ], default_value='DROP', context=context)
        order = _order(
                'O-19',
                current0={'grade': 'A'}, legacy0={},
                current1={}, legacy1={'grade': 'OLD-B'}
        )
        self.assertEqual(chain.evaluate(order), 'ACCEPT')

    def test_non_mapping_object_is_rejected_for_the_resumable_path(self):
        context = engine.LegacyGradeContext(ControllableProvider())
        with self.assertRaisesRegex(TypeError, r'requires a mapping object'):
            engine.Chain([('true', 'X')], context=context).evaluate(('not', 'a', 'mapping'))

    def test_context_pickle_round_trip_keeps_compatibility_config(self):
        provider = ControllableProvider(answers={'O-20': {'grade': 'OLD-B'}})
        context = engine.LegacyGradeContext(
                provider, current_group='current', legacy_group='legacy', grade_key='grade'
        )
        restored = pickle.loads(pickle.dumps(context))
        self.assertIsInstance(restored, engine.LegacyGradeContext)
        self.assertEqual(restored.current_group, 'current')
        self.assertEqual(restored.legacy_group, 'legacy')
        self.assertEqual(restored.grade_key, 'grade')
        self.assertIsInstance(restored, engine.LegacyGradeContext)

if __name__ == '__main__':
    unittest.main()
