#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import collections
import types
import unittest

import rule_engine.engine as engine
import rule_engine.errors as errors

__all__ = ('RefundChainTests',)

URGENT_CLAUSE = 'item.urgent or item.backlog > 2 or item.legacy'
CHANNEL_CLAUSE = 'channel == "vip"'

# each clause is (rule text, chain value, operand fields in evaluation order)
CLAUSES_V1 = (
	(URGENT_CLAUSE, 'expedited', ('urgent', 'backlog', 'legacy')),
	(CHANNEL_CLAUSE, 'priority', ('channel',)),
)
# the second publish swaps the positions of the two clauses and adds a third clause whose text is
# identical to the channel clause, so a clause can only be told apart by version and position
CLAUSES_V2 = (
	(CHANNEL_CLAUSE, 'priority', ('channel',)),
	(URGENT_CLAUSE, 'expedited', ('urgent', 'backlog', 'legacy')),
	(CHANNEL_CLAUSE, 'review', ('channel',)),
)

class AccessRecord(object):
	"""One resolver call, captured while a rule was actually being evaluated."""
	__slots__ = ('sequence', 'call', 'label', 'thing', 'name', 'value', 'error')
	def __init__(self, sequence, call, label, thing, name):
		self.sequence = sequence
		self.call = call
		self.label = label
		self.thing = thing
		self.name = name
		self.value = None
		self.error = None

	def __repr__(self):
		return "<AccessRecord #{0} call={1} name={2!r} >".format(self.sequence, self.call, self.name)

class _AuditFrame(object):
	__slots__ = ('call', 'label', 'version', 'accesses', 'error')
	def __init__(self, call, label, version):
		self.call = call
		self.label = label
		self.version = version
		self.accesses = []
		self.error = None

class Explanation(collections.namedtuple('Explanation', (
		'call', 'label', 'version', 'position', 'clause_text', 'value',
		'condition_side', 'deciding_field', 'evaluations', 'accessed_fields', 'accesses'
))):
	"""
	The record customer service saves for one audited order. The version, clause position and condition side are
	derived from what was actually evaluated, and *accesses* holds every field read as evidence.
	"""
	__slots__ = ()

class RefundDesk(object):
	"""
	Refund-order cleaning: orders are audited by a first-match :py:class:`engine.Chain` whose clauses the business
	can republish at any time. Every audit produces an :py:class:`Explanation` that can be traced back to the exact
	clause version, position and condition side that decided it, backed by the resolver's access log.
	"""
	def __init__(self):
		self.context = engine.Context(resolver=self._resolve)
		self.chain = engine.Chain(context=self.context, default_value='routine')
		self.version = 0
		self.versions = {}
		self.access_log = []
		self.explanations = []
		self.failed_audits = []
		self.caught = []
		self._operand_fields = {}
		self._frames = []
		self._call_seq = 0
		self._trigger = None

	@property
	def pending_audits(self):
		return len(self._frames)

	def publish(self, clauses):
		"""Replace the chain's entries with a new version of the clauses."""
		clauses = tuple(clauses)
		self.chain.flush()
		for text, value, operand_fields in clauses:
			entry = self.chain.append(text, value)
			self._operand_fields[entry.rule] = operand_fields
		self.version += 1
		self.versions[self.version] = clauses

	def audit_related_on_first_urgent(self, clauses, related_order):
		"""
		Arm the business hook: the first time a rule asks for the urgent flag, publish the next clause version and
		synchronously audit the related order with the same chain and context. A missing field on the related order
		is caught here so the interrupted order can continue.
		"""
		def trigger():
			self.publish(clauses)
			try:
				self.audit(related_order, label='related')
			except errors.EvaluationError as error:
				self.caught.append(error)
		self._trigger = trigger

	def audit(self, order, label):
		"""
		Evaluate *order* against the chain (first match wins) and save the explanation. The clause set, version and
		evaluation order are the ones in effect when the audit starts, even if the chain is republished reentrantly
		partway through.
		"""
		self._call_seq += 1
		frame = _AuditFrame(call=self._call_seq, label=label, version=self.version)
		snapshot = tuple(self.chain)
		self._frames.append(frame)
		try:
			entry = self.chain.find(order)
		except Exception as error:
			frame.error = error
			self.failed_audits.append(frame)
			raise
		finally:
			self._frames.pop()
		if entry is None:
			position = None
			value = self.chain.default_value
			condition_side = deciding_field = None
			evaluations = len(snapshot)
		else:
			position = next(index for index, candidate in enumerate(snapshot) if candidate is entry)
			value = entry.value
			operand_fields = self._operand_fields[entry.rule]
			operand_reads = [record.name for record in frame.accesses if record.name in operand_fields]
			deciding_field = operand_reads[-1]
			condition_side = operand_fields.index(deciding_field)
			evaluations = position + 1
		explanation = Explanation(
			call=frame.call,
			label=frame.label,
			version=frame.version,
			position=position,
			clause_text=None if entry is None else entry.rule.text,
			value=value,
			condition_side=condition_side,
			deciding_field=deciding_field,
			evaluations=evaluations,
			accessed_fields=tuple(record.name for record in frame.accesses),
			accesses=tuple(frame.accesses)
		)
		self.explanations.append(explanation)
		return explanation

	def _resolve(self, thing, name):
		frame = self._frames[-1] if self._frames else None
		record = AccessRecord(
			sequence=len(self.access_log),
			call=None if frame is None else frame.call,
			label=None if frame is None else frame.label,
			thing=thing,
			name=name
		)
		self.access_log.append(record)
		if frame is not None:
			frame.accesses.append(record)
		try:
			value = self._lookup(thing, name)
		except Exception as error:
			record.error = error
			raise
		record.value = value
		if name == 'urgent' and self._trigger is not None:
			trigger, self._trigger = self._trigger, None
			trigger()
		return value

	@staticmethod
	def _lookup(thing, name):
		if isinstance(thing, collections.abc.Mapping):
			if name not in thing:
				raise errors.SymbolResolutionError(name, thing=thing)
			return thing[name]
		if not hasattr(thing, name):
			raise errors.SymbolResolutionError(name, thing=thing)
		return getattr(thing, name)

class RefundChainTests(unittest.TestCase):
	@staticmethod
	def _outer_order():
		return {
			'item': types.SimpleNamespace(urgent=False, backlog=5, legacy=True),
			# the root order has a field named like the sub-item's urgent flag, with a different value
			'urgent': True,
			'channel': 'web',
		}

	def test_related_hit_outer_keeps_pre_publish_clauses(self):
		desk = RefundDesk()
		desk.publish(CLAUSES_V1)
		related_order = {
			'channel': 'vip',
			'item': types.SimpleNamespace(urgent=True, backlog=0, legacy=False),
		}
		desk.audit_related_on_first_urgent(CLAUSES_V2, related_order)
		order = self._outer_order()
		outer = desk.audit(order, label='outer')

		# the related order was audited synchronously with the same chain and context, between the
		# outer order's urgent read and the rest of the old rule's OR evaluation
		self.assertIs(desk.chain.context, desk.context)
		self.assertTrue(all(entry.rule.context is desk.context for entry in desk.chain))
		self.assertEqual(
			[(record.label, record.name) for record in desk.access_log],
			[
				('outer', 'item'),
				('outer', 'urgent'),
				('related', 'channel'),
				('outer', 'item'),
				('outer', 'backlog'),
			]
		)
		self.assertEqual(desk.caught, [])
		self.assertEqual(desk.failed_audits, [])
		self.assertTrue(all(record.error is None for record in desk.access_log))

		# two explanations were saved, in completion order: the related order first, then the outer one
		self.assertEqual(len(desk.explanations), 2)
		related, completed_outer = desk.explanations
		self.assertIs(completed_outer, outer)

		# the outer order kept the pre-publish clause set even though the chain now holds version 2
		self.assertEqual(desk.version, 2)
		self.assertEqual(outer.version, 1)
		self.assertEqual(outer.position, 0)
		self.assertEqual(outer.clause_text, URGENT_CLAUSE)
		self.assertEqual(outer.value, 'expedited')
		self.assertEqual(outer.evaluations, 1)

		# the urgent flag was read from the original sub-item, not the root order's same-named field
		urgent_read = outer.accesses[1]
		self.assertEqual(urgent_read.name, 'urgent')
		self.assertIs(urgent_read.thing, order['item'])
		self.assertIs(urgent_read.value, False)
		self.assertTrue(order['urgent'])
		self.assertFalse(any(record.thing is order and record.name == 'urgent' for record in desk.access_log))

		# after the related audit, the outer rule kept reading the same sub-item for its OR evaluation
		backlog_read = outer.accesses[3]
		self.assertIs(backlog_read.thing, urgent_read.thing)
		self.assertEqual(backlog_read.value, 5)

		# the deciding side of the old OR is the backlog comparison; the skipped field was never read
		self.assertEqual(outer.condition_side, 1)
		self.assertEqual(outer.deciding_field, 'backlog')
		self.assertEqual(outer.accessed_fields, ('item', 'urgent', 'item', 'backlog'))
		self.assertNotIn('legacy', outer.accessed_fields)

		# the related order followed the new clauses and hit the first one
		self.assertEqual(related.version, 2)
		self.assertEqual(related.position, 0)
		self.assertEqual(related.clause_text, CHANNEL_CLAUSE)
		self.assertEqual(related.value, 'priority')
		self.assertEqual(related.condition_side, 0)
		self.assertEqual(related.deciding_field, 'channel')
		self.assertEqual(related.accessed_fields, ('channel',))
		self.assertEqual(related.evaluations, 1)

		# the publish swapped the two clauses' positions and the new version contains two clauses with
		# identical text, so only the version and position in each explanation identify the clause
		self.assertEqual([clause[0] for clause in desk.versions[1]], [URGENT_CLAUSE, CHANNEL_CLAUSE])
		self.assertEqual([clause[0] for clause in desk.versions[2]], [CHANNEL_CLAUSE, URGENT_CLAUSE, CHANNEL_CLAUSE])
		self.assertEqual(desk.chain[0].rule.text, desk.chain[2].rule.text)
		self.assertNotEqual(desk.chain[0].value, desk.chain[2].value)
		self.assertEqual(related.clause_text, desk.chain[2].rule.text)
		self.assertNotEqual(related.position, 2)

		# the next order is audited against the new version; the two clause evaluations it takes to
		# match are accounted for by the access record (clause 0 read channel, clause 1 read the sub-item)
		next_order = {
			'channel': 'web',
			'item': types.SimpleNamespace(urgent=True, backlog=0, legacy=False),
		}
		followup = desk.audit(next_order, label='followup')
		self.assertEqual(followup.version, 2)
		self.assertEqual(followup.position, 1)
		self.assertEqual(followup.clause_text, URGENT_CLAUSE)
		self.assertEqual(followup.value, 'expedited')
		self.assertEqual(followup.evaluations, 2)
		self.assertEqual(followup.accessed_fields, ('channel', 'item', 'urgent'))
		self.assertEqual(followup.condition_side, 0)
		self.assertEqual(followup.deciding_field, 'urgent')
		self.assertEqual(desk.pending_audits, 0)

	def test_related_error_is_caught_and_does_not_change_outer(self):
		desk = RefundDesk()
		desk.publish(CLAUSES_V1)
		# the related order has no channel field, so the new first clause raises a missing-field error
		related_order = {'item': types.SimpleNamespace(urgent=True, backlog=0, legacy=False)}
		desk.audit_related_on_first_urgent(CLAUSES_V2, related_order)
		order = self._outer_order()
		outer = desk.audit(order, label='outer')

		# the business resolver caught the related order's error and continued the outer order
		self.assertEqual(len(desk.caught), 1)
		self.assertIsInstance(desk.caught[0], errors.SymbolResolutionError)
		self.assertEqual(desk.caught[0].symbol_name, 'channel')

		# the failed audit is on record with its own call number, label and version
		self.assertEqual(len(desk.failed_audits), 1)
		failed = desk.failed_audits[0]
		self.assertEqual(failed.label, 'related')
		self.assertEqual(failed.version, 2)
		self.assertIs(failed.error, desk.caught[0])
		self.assertNotEqual(failed.call, outer.call)

		# the access log identifies the exact call and field that raised
		error_records = [record for record in desk.access_log if record.error is not None]
		self.assertEqual(len(error_records), 1)
		self.assertIs(error_records[0].error, desk.caught[0])
		self.assertEqual(error_records[0].call, failed.call)
		self.assertEqual(error_records[0].label, 'related')
		self.assertEqual(error_records[0].name, 'channel')
		self.assertIs(error_records[0].thing, related_order)

		# the outer order's verdict is unchanged: pre-publish version, first clause, same OR evaluation
		self.assertEqual(outer.version, 1)
		self.assertEqual(outer.position, 0)
		self.assertEqual(outer.clause_text, URGENT_CLAUSE)
		self.assertEqual(outer.value, 'expedited')
		self.assertEqual(outer.condition_side, 1)
		self.assertEqual(outer.deciding_field, 'backlog')
		self.assertEqual(outer.accessed_fields, ('item', 'urgent', 'item', 'backlog'))
		self.assertEqual(outer.evaluations, 1)
		# only the outer audit produced an explanation
		self.assertEqual(desk.explanations, [outer])

		# the failed related audit does not pollute the next transaction
		next_order = {'channel': 'vip'}
		followup = desk.audit(next_order, label='followup')
		self.assertEqual(followup.version, 2)
		self.assertEqual(followup.position, 0)
		self.assertEqual(followup.value, 'priority')
		self.assertEqual(followup.accessed_fields, ('channel',))
		self.assertEqual(followup.evaluations, 1)
		self.assertTrue(all(record.error is None for record in followup.accesses))
		self.assertEqual(len(desk.caught), 1)
		self.assertEqual(len([record for record in desk.access_log if record.error is not None]), 1)
		self.assertEqual(desk.pending_audits, 0)

if __name__ == '__main__':
	unittest.main()
