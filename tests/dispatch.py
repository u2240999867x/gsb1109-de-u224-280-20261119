#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  tests/dispatch.py
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

from rule_engine.dispatch import (
	DictionaryRegistry,
	DispatchService,
	Fact,
	FactKind,
	Outcome,
	REQUIRED_FACTS,
	Ticket,
	evaluate,
)

__all__ = ('DispatchDictionaryTests', 'DispatchEvaluationTests', 'DispatchPatchingTests', 'DispatchServiceTests')

F = FactKind


def _legacy_dictionary(registry: DictionaryRegistry, vendor: str = 'V1') -> None:
	"""The legacy supplier dictionary from the work-order dispatch scenario."""
	registry.register(
		vendor,
		{
			'R': (F.RETURN,),
			'RMA': (F.RETURN,),
			'X': (F.RETURN, F.EXPEDITE),
			'Y': (F.EXPEDITE, F.NIGHT),
			'E': (F.EXPEDITE,),
			'Z': (F.NIGHT,),
		},
		aliases={'RMA': 'R'},
	)


def _ticket(ticket_id: str, code: str, *tags: str, vendor: str = 'V1') -> Ticket:
	return Ticket(ticket_id=ticket_id, vendor=vendor, code=code, tags=tuple(tags))


class DispatchDictionaryTests(unittest.TestCase):
	def test_immutable_snapshot(self):
		registry = DictionaryRegistry()
		_legacy_dictionary(registry)
		snapshot = registry.get('V1')
		self.assertEqual(snapshot.version, 1)
		with self.assertRaises(TypeError):
			snapshot.entries['R'] = (F.NIGHT,)
		with self.assertRaises(TypeError):
			snapshot.aliases['RMA'] = 'X'

	def test_alias_normalization(self):
		registry = DictionaryRegistry()
		_legacy_dictionary(registry)
		snapshot = registry.get('V1')
		self.assertEqual(snapshot.normalize_alias('RMA'), 'R')
		self.assertEqual(snapshot.normalize_alias('UNKNOWN'), 'UNKNOWN')

	def test_register_requires_fact_kinds(self):
		registry = DictionaryRegistry()
		with self.assertRaises(TypeError):
			registry.register('V', {'R': ('return',)})
		with self.assertRaises(ValueError):
			registry.register('V', {'R': ()})

	def test_duplicate_registration_is_rejected(self):
		registry = DictionaryRegistry()
		_legacy_dictionary(registry)
		with self.assertRaises(ValueError):
			_legacy_dictionary(registry)

	def test_same_code_differs_between_vendors(self):
		registry = DictionaryRegistry()
		_legacy_dictionary(registry, 'V1')
		registry.register('V2', {'R': (F.EXPEDITE, F.NIGHT), 'RMA': (F.RETURN,)})
		self.assertEqual(registry.get('V1').kinds_for('R'), (F.RETURN,))
		self.assertEqual(registry.get('V2').kinds_for('R'), (F.EXPEDITE, F.NIGHT))
		with self.assertRaises(KeyError):
			registry.get('V3')


class DispatchEvaluationTests(unittest.TestCase):
	def setUp(self):
		self.registry = DictionaryRegistry()
		_legacy_dictionary(self.registry)
		self.snapshot = self.registry.get('V1')

	def test_proven_order_carries_three_distinct_codes(self):
		evaluation = evaluate(_ticket('T1', 'R', 'E', 'Y'), self.snapshot)
		self.assertIs(evaluation.outcome, Outcome.PROVEN)
		self.assertEqual(tuple(fact.kind for fact in evaluation.facts), REQUIRED_FACTS)
		self.assertEqual(len({fact.code for fact in evaluation.facts}), 3)
		self.assertTrue(evaluation.is_night_eligible)
		self.assertEqual(evaluation.shift, 'night')
		self.assertEqual(evaluation.vendor, 'V1')
		self.assertEqual(evaluation.version, 1)

	def test_r_and_rma_both_evidence_return(self):
		evaluation = evaluate(_ticket('T1', 'RMA', 'E', 'Z'), self.snapshot)
		self.assertIs(evaluation.outcome, Outcome.PROVEN)
		return_facts = [fact for fact in evaluation.facts if fact.kind is F.RETURN]
		self.assertEqual(len(return_facts), 1)
		self.assertEqual(return_facts[0].code, 'R')
		self.assertEqual(return_facts[0].alias, 'RMA')

	def test_duplicate_aliases_count_as_one_fact(self):
		# a single X code can only back one fact even though the alias is repeated three times
		evaluation = evaluate(_ticket('T1', 'X', 'X', 'X'), self.snapshot)
		self.assertIs(evaluation.outcome, Outcome.INSUFFICIENT)
		self.assertEqual(len(evaluation.facts), 1)
		self.assertEqual(len({fact.code for fact in evaluation.facts}), 1)
		self.assertIn('X', evaluation.unused_aliases)

	def test_ambiguous_code_is_assigned_at_most_once(self):
		# X may be a return or an expedite, never both facts on the same order
		evaluation = evaluate(_ticket('T1', 'X', 'Z'), self.snapshot)
		self.assertIs(evaluation.outcome, Outcome.INSUFFICIENT)
		self.assertEqual(len(evaluation.facts), 2)
		self.assertEqual(len({fact.code for fact in evaluation.facts}), 2)
		self.assertEqual(evaluation.missing_kinds, (F.EXPEDITE,))
		self.assertEqual(len([fact for fact in evaluation.facts if fact.code == 'X']), 1)

	def test_backtracking_finds_a_complete_match(self):
		# X is seen first and would consume the return slot; backtracking moves return onto R instead
		evaluation = evaluate(_ticket('T1', 'X', 'R', 'Z'), self.snapshot)
		self.assertIs(evaluation.outcome, Outcome.PROVEN)
		by_kind = {fact.kind: fact.code for fact in evaluation.facts}
		self.assertEqual(by_kind, {F.RETURN: 'R', F.EXPEDITE: 'X', F.NIGHT: 'Z'})

	def test_unknown_code_outcome(self):
		evaluation = evaluate(_ticket('T1', 'Q', 'Q'), self.snapshot)
		self.assertIs(evaluation.outcome, Outcome.UNKNOWN_CODE)
		self.assertEqual(evaluation.unknown_aliases, ('Q', 'Q'))
		self.assertFalse(evaluation.is_night_eligible)
		self.assertIsNone(evaluation.shift)

	def test_missing_fields_outcome(self):
		evaluation = evaluate(Ticket(ticket_id='T1', vendor='V1', code=None, tags=('R',)), self.snapshot)
		self.assertIs(evaluation.outcome, Outcome.MISSING_FIELDS)
		self.assertEqual(evaluation.missing_fields, ('code',))
		evaluation = evaluate(Ticket(ticket_id=None, vendor='V1', code='R', tags=None), self.snapshot)
		self.assertIs(evaluation.outcome, Outcome.MISSING_FIELDS)
		self.assertEqual(evaluation.missing_fields, ('ticket_id', 'tags'))

	def test_insufficient_outcome_names_missing_kinds(self):
		evaluation = evaluate(_ticket('T1', 'R', 'E'), self.snapshot)
		self.assertIs(evaluation.outcome, Outcome.INSUFFICIENT)
		self.assertEqual(evaluation.missing_kinds, (F.NIGHT,))
		self.assertFalse(evaluation.is_night_eligible)

	def test_mapping_input_is_accepted(self):
		evaluation = evaluate(
			{'ticket_id': 'T1', 'vendor': 'V1', 'code': 'R', 'tags': ['E', 'Y']},
			self.snapshot,
		)
		self.assertIs(evaluation.outcome, Outcome.PROVEN)


class DispatchPatchingTests(unittest.TestCase):
	def setUp(self):
		self.registry = DictionaryRegistry()
		_legacy_dictionary(self.registry)
		self.tickets = [
			_ticket('T-y', 'R', 'E', 'Y'),
			_ticket('T-z', 'R', 'E', 'Y', 'Z'),
			_ticket('T-w', 'R', 'E', 'W'),
			_ticket('T-ok', 'R', 'E', 'Z'),
		]

	def _patch(self):
		return self.registry.patch('V1', {'Y': (F.EXPEDITE,), 'W': (F.NIGHT,)})

	def test_patch_creates_a_new_immutable_version(self):
		old = self.registry.get('V1')
		new = self._patch()
		self.assertEqual(new.version, 2)
		self.assertIs(new.parent, old)
		self.assertEqual(self.registry.latest_version('V1'), 2)
		self.assertEqual(self.registry.versions('V1'), (1, 2))
		self.assertEqual(old.kinds_for('Y'), (F.EXPEDITE, F.NIGHT))
		self.assertIsNone(old.kinds_for('W'))
		self.assertEqual(new.kinds_for('Y'), (F.EXPEDITE,))
		self.assertEqual(new.kinds_for('W'), (F.NIGHT,))
		self.assertEqual(new.normalize_alias('RMA'), 'R')

	def test_patch_can_delete_entries_and_aliases(self):
		new = self.registry.patch('V1', {'Z': None}, alias_changes={'RMA': None})
		self.assertIsNone(new.kinds_for('Z'))
		self.assertNotIn('RMA', new.aliases)
		self.assertEqual(self.registry.get('V1', version=1).kinds_for('Z'), (F.NIGHT,))

	def test_legacy_results_remain_recheckable(self):
		old_evaluation = evaluate(self.tickets[0], self.registry.get('V1', version=1))
		self.assertIs(old_evaluation.outcome, Outcome.PROVEN)
		self._patch()
		new_evaluation = evaluate(self.tickets[0], self.registry.get('V1'))
		self.assertIs(new_evaluation.outcome, Outcome.INSUFFICIENT)
		self.assertEqual(old_evaluation.version, 1)
		self.assertEqual(new_evaluation.version, 2)
		self.assertIsNotNone(self.registry.get('V1', version=1))

	def test_night_batch_classifies_all_four_cases(self):
		service = DispatchService(self.registry)
		report = service.apply_night_patch(
			'V1',
			self.tickets,
			{'Y': (F.EXPEDITE,), 'W': (F.NIGHT,)},
		)
		self.assertEqual(report.old_version, 1)
		self.assertEqual(report.new_version, 2)
		self.assertEqual(report.revoked, ('T-y',))
		self.assertEqual(report.rewritten, ('T-z',))
		self.assertEqual(report.enqueued, ('T-w',))
		self.assertEqual(report.unaffected, ('T-ok',))
		self.assertEqual(report.changed, ())

	def test_witness_invalidation(self):
		service = DispatchService(self.registry)
		service.evaluate_batch(self.tickets)
		self.assertIn('T-y', service.night_queue)
		service.apply_night_patch('V1', self.tickets, {'Y': (F.EXPEDITE,), 'W': (F.NIGHT,)})
		latest = service.get_evaluation('T-y')
		self.assertIs(latest.outcome, Outcome.INSUFFICIENT)
		self.assertEqual(latest.missing_kinds, (F.NIGHT,))
		self.assertNotIn('T-y', service.night_queue)

	def test_fallback_alias_is_selected_after_patch(self):
		service = DispatchService(self.registry)
		report = service.apply_night_patch('V1', self.tickets, {'Y': (F.EXPEDITE,), 'W': (F.NIGHT,)})
		before = report.previous['T-z']
		after = report.current['T-z']
		self.assertEqual(next(fact for fact in before.facts if fact.kind is F.NIGHT).code, 'Y')
		self.assertEqual(next(fact for fact in after.facts if fact.kind is F.NIGHT).code, 'Z')
		self.assertTrue(after.is_night_eligible)

	def test_previously_rejected_order_becomes_eligible(self):
		service = DispatchService(self.registry)
		before = service.evaluate_ticket(self.tickets[2])
		self.assertIs(before.outcome, Outcome.INSUFFICIENT)
		report = service.apply_night_patch('V1', self.tickets, {'Y': (F.EXPEDITE,), 'W': (F.NIGHT,)})
		after = report.current['T-w']
		self.assertIs(after.outcome, Outcome.PROVEN)
		self.assertEqual(next(fact for fact in after.facts if fact.kind is F.NIGHT).code, 'W')
		self.assertIn('T-w', service.night_queue)

	def test_unrelated_orders_are_not_re_enqueued(self):
		service = DispatchService(self.registry)
		service.evaluate_batch(self.tickets)
		queue_before = service.night_queue
		report = service.apply_night_patch('V1', self.tickets, {'Y': (F.EXPEDITE,), 'W': (F.NIGHT,)})
		self.assertIn('T-ok', report.unaffected)
		before = service.get_evaluation('T-ok', version=1)
		after = service.get_evaluation('T-ok', version=2)
		self.assertEqual(before.outcome, after.outcome)
		self.assertEqual(
			[(fact.kind, fact.code, fact.alias) for fact in before.facts],
			[(fact.kind, fact.code, fact.alias) for fact in after.facts],
		)
		self.assertIn('T-ok', queue_before)
		self.assertIn('T-ok', service.night_queue)


class DispatchServiceTests(unittest.TestCase):
	def setUp(self):
		self.service = DispatchService()
		_legacy_dictionary(self.service.registry)

	def test_late_legacy_evaluation_cannot_overwrite_newer(self):
		ticket = _ticket('T-ok', 'R', 'E', 'Z')
		self.service.evaluate_ticket(ticket)
		self.service.registry.patch('V1', {'Y': (F.EXPEDITE,), 'W': (F.NIGHT,)})
		self.service.evaluate_ticket(ticket)
		latest = self.service.get_evaluation('T-ok')
		self.assertEqual(latest.version, 2)
		# a late result computed from the old dictionary arrives after the new one
		late = self.service.evaluate_ticket(ticket, version=1)
		self.assertEqual(late.version, 1)
		self.assertIs(self.service.get_evaluation('T-ok'), latest)
		self.assertEqual(self.service.history_versions('T-ok'), (1, 2))

	def test_history_is_immutable_and_auditable(self):
		ticket = _ticket('T-y', 'R', 'E', 'Y')
		self.service.evaluate_ticket(ticket)
		self.service.registry.patch('V1', {'Y': (F.EXPEDITE,), 'W': (F.NIGHT,)})
		self.service.evaluate_ticket(ticket)
		legacy = self.service.get_evaluation('T-y', version=1)
		self.assertIs(legacy.outcome, Outcome.PROVEN)
		self.assertEqual(
			self.service.audit('T-y', version=1),
			legacy,
		)
		current = self.service.get_evaluation('T-y')
		self.assertIs(current.outcome, Outcome.INSUFFICIENT)
		self.assertEqual(self.service.audit('T-y'), current)

	def test_unknown_ticket_lookup(self):
		self.assertIsNone(self.service.get_evaluation('nope'))
		with self.assertRaises(KeyError):
			self.service.audit('nope')

	def test_evidence_is_taken_only_from_matching_vendor_and_version(self):
		self.service.registry.register('V2', {'R': (F.EXPEDITE, F.NIGHT), 'RMA': (F.RETURN,), 'G': (F.RETURN,)})
		evaluation = self.service.evaluate_ticket(
			Ticket(ticket_id='T2', vendor='V2', code='R', tags=('RMA', 'G'))
		)
		self.assertEqual(evaluation.vendor, 'V2')
		self.assertEqual(evaluation.version, 1)
		self.assertIs(evaluation.outcome, Outcome.INSUFFICIENT)
		self.assertEqual({fact.code for fact in evaluation.facts}, {'R', 'RMA'})
		self.assertEqual(evaluation.missing_kinds, (F.NIGHT,))

	def test_every_fact_records_its_provenance(self):
		evaluation = self.service.evaluate_ticket(_ticket('T1', 'R', 'RMA', 'E', 'Y'))
		self.assertIsInstance(evaluation.facts[0], Fact)
		for fact in evaluation.facts:
			self.assertEqual(fact.vendor, 'V1')
			self.assertEqual(fact.version, 1)
			self.assertIn(fact.code, self.service.registry.get('V1').entries)


if __name__ == '__main__':
	unittest.main()
