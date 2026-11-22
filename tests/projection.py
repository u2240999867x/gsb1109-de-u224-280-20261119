#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  tests/projection.py
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
#  OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR
#  THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#

import unittest

import rule_engine
from rule_engine import projection

__all__ = ('ProjectionPlanTests', 'ProjectionSetTests', 'ProjectFunctionTests')

# the new-style rule reads items.rows, checks each row's activity and reads detail fields of active rows
# only; the detail field of a row is selected by the row's kind through the batch-level column mapping
# (kind 'A' maps to price or cost, kind 'B' to another field); the comprehension reuses "items" as a local
# name, shadowing the root symbol
NEW_RULE_TEXT = (
	"shape == 'v2'"
	" ? $any([(items&[mapping&[items['kind']]] ?? 0) > 100 for items in items['rows'] if items['active']])"
	" : legacy['code'] == 'OK'"
)
# the legacy rule only reads legacy.code
LEGACY_RULE_TEXT = "legacy['code'] == 'OK'"
# a third rule compiled against the same context only reads account
ACCOUNT_RULE_TEXT = "account['status'] == 'active'"

SUBTREE = projection.SUBTREE

def catalog_document(**overrides):
	"""
	A supplier catalog batch. Rows 0 and 1 are active (kinds 'A' and 'B'), row 2 is inactive with kind 'Q'
	whose entry is absent from the batch-level mapping (an optional key that can not be known in advance).
	"""
	document = {
		'shape': 'v2',
		'account': {'status': 'active', 'balance': 1000.0},
		'legacy': {'code': 'OK', 'blob': 'x' * 64},
		'mapping': {'A': 'price', 'B': 'cost'},
		'items': {
			'total': 3,
			'rows': [
				{'active': True, 'kind': 'A', 'price': 150.0, 'cost': 90.0, 'description': 'row zero'},
				{'active': True, 'kind': 'B', 'price': 80.0, 'cost': 120.0, 'description': 'row one'},
				{'active': False, 'kind': 'Q', 'price': 10.0, 'cost': 5.0, 'description': 'row two'},
			],
		},
	}
	document.update(overrides)
	return document

def formatted(templates):
	return {projection.format_path(template) for template in templates}

class ProjectionPlanTests(unittest.TestCase):
	def setUp(self):
		self.rule = rule_engine.Rule(NEW_RULE_TEXT)
		self.plan = projection.ProjectionPlan.from_rule(self.rule)

	def test_control_paths_are_the_selection_fields(self):
		# the shape, row activity, category and mapping selection fields are kept for stage one
		self.assertEqual(
			formatted(self.plan.control_paths),
			{
				'shape[...]',
				'items.rows',
				'items.rows[*].active[...]',
				'items.rows[*].kind[...]',
				'mapping[...]',
			}
		)

	def test_detail_paths_are_guarded(self):
		# the dynamic detail read and the legacy branch are the only guarded (stage two) reads
		self.assertEqual(
			formatted(self.plan.detail_paths),
			{'items.rows[*][?][...]', 'legacy.code[...]'}
		)

	def test_comprehension_variable_shadows_root_symbol(self):
		# the comprehension reuses "items" as a local name; no read is attributed to a root "items" value
		# itself (e.g. items[?]) rather than to the iterated items.rows path
		for site in self.plan.read_sites:
			self.assertNotIsInstance(site.template[0], projection.DynamicKey)
			if site.template[0] == 'items':
				self.assertGreater(len(site.template), 1)
				self.assertEqual(site.template[1], 'rows')

	def test_bind_selects_detail_fields_of_active_rows(self):
		document = catalog_document()
		control = projection.project(document, self.plan.control_paths)
		bound = self.plan.bind(control)
		# row 0 (kind A -> price) and row 1 (kind B -> cost) are active; the known price of the A row must
		# not trim the cost of the B row
		self.assertEqual(
			bound.detail_paths,
			{
				('items', 'rows', 0, 'price', SUBTREE),
				('items', 'rows', 1, 'cost', SUBTREE),
			}
		)

	def test_unknown_mapping_on_inactive_row_reads_nothing(self):
		document = catalog_document()
		control = projection.project(document, self.plan.control_paths)
		bound = self.plan.bind(control)
		# row 2 is inactive with a kind that is absent from the mapping; the unknown selection must not
		# pull the row's detail fields, any other row's fields, or the whole root document into the
		# projection (its small control columns are still read in stage one, by design)
		for path in bound.paths:
			self.assertNotIn('description', path)
		for path in bound.detail_paths:
			if len(path) > 2 and path[:2] == ('items', 'rows'):
				self.assertNotEqual(path[2], 2)
		projected = bound.project(document)
		self.assertEqual(set(projected.keys()), {'shape', 'mapping', 'items'})
		self.assertNotIn('price', projected['items']['rows'][2])
		self.assertNotIn('cost', projected['items']['rows'][2])
		self.assertNotIn('description', projected['items']['rows'][2])

	def test_unknown_mapping_on_active_row_keeps_candidate_range(self):
		document = catalog_document()
		# move the unknown selection to an active row
		document['items']['rows'][1]['kind'] = 'Q'
		control = projection.project(document, self.plan.control_paths)
		bound = self.plan.bind(control)
		# the row needs its detail range (both candidate fields of the mapping); the other active row is
		# still trimmed to its single resolved field
		self.assertEqual(
			bound.detail_paths,
			{
				('items', 'rows', 0, 'price', SUBTREE),
				('items', 'rows', 1, 'price', SUBTREE),
				('items', 'rows', 1, 'cost', SUBTREE),
			}
		)

	def test_ternary_branch_selects_legacy_detail(self):
		document = catalog_document(shape='v1')
		control = projection.project(document, self.plan.control_paths)
		bound = self.plan.bind(control)
		# the actually taken branch reads legacy.code; no items detail field is projected
		self.assertEqual(bound.detail_paths, {('legacy', 'code', SUBTREE)})

	def test_projection_preserves_rule_results(self):
		documents = [
			catalog_document(),
			catalog_document(shape='v1'),
			catalog_document(shape='v1', legacy={'code': 'STALE', 'blob': 'y' * 64}),
		]
		# a variant with the unknown selection on an active row
		document = catalog_document()
		document['items']['rows'][1]['kind'] = 'Q'
		documents.append(document)
		# a variant where no row matches
		document = catalog_document()
		document['items']['rows'][0]['price'] = 1.0
		document['items']['rows'][1]['cost'] = 1.0
		documents.append(document)
		for document in documents:
			with self.subTest(shape=document['shape']):
				control = projection.project(document, self.plan.control_paths)
				bound = self.plan.bind(control)
				projected = bound.project(document)
				self.assertEqual(self.rule.evaluate(projected), self.rule.evaluate(document))

class ProjectionSetTests(unittest.TestCase):
	def test_interleaved_rules_keep_scoped_dependencies(self):
		context = rule_engine.Context()
		document = catalog_document()
		# interleave compiling and evaluating the new, legacy and account rules against one context
		new_rule = rule_engine.Rule(NEW_RULE_TEXT, context=context)
		self.assertTrue(new_rule.matches(document))
		legacy_rule = rule_engine.Rule(LEGACY_RULE_TEXT, context=context)
		self.assertTrue(legacy_rule.matches(document))
		account_rule = rule_engine.Rule(ACCOUNT_RULE_TEXT, context=context)
		self.assertTrue(account_rule.matches(document))
		new_plan = projection.ProjectionPlan.from_rule(new_rule)
		self.assertTrue(legacy_rule.matches(catalog_document(shape='v1')))
		legacy_plan = projection.ProjectionPlan.from_rule(legacy_rule)
		account_plan = projection.ProjectionPlan.from_rule(account_rule)
		self.assertTrue(new_rule.matches(document))

		# each rule keeps its own dependency scope
		self.assertEqual(formatted(legacy_plan.control_paths), {'legacy.code[...]'})
		self.assertEqual(formatted(account_plan.control_paths), {'account.status[...]'})
		new_templates = formatted(new_plan.control_paths) | formatted(new_plan.detail_paths)
		self.assertNotIn('legacy.code[...]', formatted(new_plan.control_paths))
		self.assertTrue(any(template.startswith('items.rows') for template in new_templates))

		# the combined projection traces every retained field back to the rule references requiring it
		projection_set = projection.ProjectionSet([new_plan, legacy_plan, account_plan])
		control = projection.project(document, projection_set.control_paths)
		bound = projection_set.bind(control)
		references = bound.references
		self.assertGreater(len(references), 0)
		for path, origins in references.items():
			self.assertGreater(len(origins), 0, msg=projection.format_path(path))
			for origin in origins:
				self.assertIn(origin.rule_text, {NEW_RULE_TEXT, LEGACY_RULE_TEXT, ACCOUNT_RULE_TEXT})
		# account.status is retained by the account rule only
		account_origins = references[('account', 'status', SUBTREE)]
		self.assertEqual({origin.rule_text for origin in account_origins}, {ACCOUNT_RULE_TEXT})
		# legacy.code is retained by the legacy rule; the new rule's untaken legacy branch does not retain it
		legacy_origins = references[('legacy', 'code', SUBTREE)]
		self.assertEqual({origin.rule_text for origin in legacy_origins}, {LEGACY_RULE_TEXT})
		# the detail field of the first active row is retained by the new rule only
		price_origins = references[('items', 'rows', 0, 'price', SUBTREE)]
		self.assertEqual({origin.rule_text for origin in price_origins}, {NEW_RULE_TEXT})

		# evaluating the rules against the combined projection agrees with the full document
		projected = bound.project(document)
		for rule in (new_rule, legacy_rule, account_rule):
			self.assertEqual(rule.evaluate(projected), rule.evaluate(document))

	def test_from_rules(self):
		rules = [rule_engine.Rule(LEGACY_RULE_TEXT), rule_engine.Rule(ACCOUNT_RULE_TEXT)]
		projection_set = projection.ProjectionSet.from_rules(rules)
		self.assertEqual(len(projection_set.plans), 2)
		self.assertEqual(
			formatted(projection_set.control_paths),
			{'legacy.code[...]', 'account.status[...]'}
		)

class ProjectFunctionTests(unittest.TestCase):
	def test_project_selects_paths(self):
		document = catalog_document()
		projected = projection.project(document, [
			('items', 'rows', projection._Wildcard(), 'active', SUBTREE),
			('mapping', SUBTREE),
		])
		self.assertEqual(set(projected.keys()), {'items', 'mapping'})
		self.assertEqual(projected['mapping'], document['mapping'])
		self.assertEqual(
			projected['items']['rows'],
			[{'active': row['active']} for row in document['items']['rows']]
		)

	def test_project_subtree_copies(self):
		document = catalog_document()
		projected = projection.project(document, [('mapping', SUBTREE)])
		self.assertEqual(projected, {'mapping': document['mapping']})
		self.assertIsNot(projected['mapping'], document['mapping'])

	def test_project_bare_path_keeps_structure(self):
		document = catalog_document()
		projected = projection.project(document, [('items', 'rows')])
		# a bare container path retains the structure without leaf values
		self.assertEqual(len(projected['items']['rows']), 3)
		for row in projected['items']['rows']:
			self.assertEqual(set(row.keys()), {'active', 'kind', 'price', 'cost', 'description'})
			self.assertTrue(all(value is None for value in row.values()))

	def test_project_compacts_unselected_elements(self):
		document = catalog_document()
		projected = projection.project(document, [('items', 'rows', 1, 'cost', SUBTREE)])
		self.assertEqual(projected['items']['rows'], [{'cost': 120.0}])

	def test_format_path(self):
		wildcard = projection._Wildcard()
		dynamic = projection.DynamicKey(rule_engine.ast.NullExpression(rule_engine.Context()), ())
		self.assertEqual(
			projection.format_path(('items', 'rows', wildcard, 'price', SUBTREE)),
			'items.rows[*].price[...]'
		)
		self.assertEqual(projection.format_path(('items', dynamic)), 'items[?]')
		self.assertEqual(projection.format_path(('rows', 2, 'cost')), 'rows[2].cost')

if __name__ == '__main__':
	unittest.main()
