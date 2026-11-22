#!/usr/bin/env python3
"""
Verification for the supplier order rule migration.

The checks are behavioural, using the real Rule Engine:

* every migrated text compiles and reads the same records as the legacy text
  on the correspondingly renamed records;
* a record whose old rate and total are different catches a naive sequential
  replacement (``rate->total`` then ``total->rate``), which would collapse
  both sides of the first comparison;
* the input ``entry`` field and the comprehension local stay distinct, so the
  outer value and the inner assignment are not mixed up;
* the two legacy rules that differ only in the inner local name migrate to
  rules that still agree on every record;
* string literals, the trailing comment and ``partner.*`` attributes are
  byte-for-byte unchanged;
* an undeclared selector value is reported and the old text stays available.

Run with ``PYTHONPATH=lib python migration/verify_migration.py``.
"""

from __future__ import annotations

import copy
import json
import pathlib
import sys
import unittest

import rule_engine

MIGRATION_DIR = pathlib.Path(__file__).resolve().parent
RECORDS = json.loads((MIGRATION_DIR / 'records.json').read_text(encoding='utf-8'))
CHANGES = json.loads((MIGRATION_DIR / 'changes.json').read_text(encoding='utf-8'))
DIAGNOSTICS = json.loads((MIGRATION_DIR / 'diagnostics.json').read_text(encoding='utf-8'))
RULES = json.loads((MIGRATION_DIR / 'migrated_rules.json').read_text(encoding='utf-8'))


def source(name: str) -> str:
    return (MIGRATION_DIR / 'source' / name).read_text(encoding='utf-8')


def migrated(name: str) -> str:
    # publishable rules are read from the generated bundle, so a rule that was
    # blocked for an unresolved selector reference can not be picked up here
    rule = RULES[name.removesuffix('.rule')]
    if not rule['publishable']:
        raise AssertionError('rule {0} is blocked and has no publishable text'.format(name))
    return (MIGRATION_DIR / 'migrated' / name).read_text(encoding='utf-8')


def record(entry: dict, *, side: str) -> dict:
    defaults = RECORDS['defaults']
    value = {
        'note': defaults['note'],
        'partner': copy.deepcopy(defaults['partner']),
        'orders': copy.deepcopy(defaults['orders']),
    }
    value.update(copy.deepcopy(entry[side]))
    return value


class MigrationTests(unittest.TestCase):
    def compile(self, name: str, side: str):
        return rule_engine.Rule(source(name) if side == 'old' else migrated(name))

    def evaluate(self, name: str, entry: dict) -> bool:
        old_rule = self.compile(name, 'old')
        new_rule = self.compile(name, 'new')
        old_value = old_rule.evaluate(record(entry, side='old'))
        new_value = new_rule.evaluate(record(entry, side='new'))
        return old_value, new_value

    def test_every_record_matches_between_schemas(self):
        for name in ('order_rules_a.rule', 'order_rules_b.rule'):
            for entry in RECORDS['records']:
                with self.subTest(rule=name, record=entry['id']):
                    old_value, new_value = self.evaluate(name, entry)
                    self.assertIs(old_value, new_value)

    def test_records_cover_both_outcomes(self):
        outcomes = set()
        for entry in RECORDS['records']:
            outcomes.add(self.evaluate('order_rules_a.rule', entry)[0])
        self.assertEqual(outcomes, {True, False})

    def test_swap_distinct_catches_sequential_replace(self):
        entry = next(entry for entry in RECORDS['records'] if entry['id'] == 'swap_distinct')
        old_value, new_value = self.evaluate('order_rules_a.rule', entry)
        # the two columns differ and lie on opposite sides of the test, so a
        # naive replace would turn `total < rate` into `total < total`
        old_text = source('order_rules_a.rule')
        naive = old_text.replace('rate', 'total').replace('total', 'rate')
        naive_rule = rule_engine.Rule(naive)
        naive_value = naive_rule.evaluate(record(entry, side='old'))
        self.assertNotEqual(old_value, naive_value)
        self.assertEqual(old_value, new_value)

    def test_entry_local_and_outer_field_stay_distinct(self):
        entry = next(entry for entry in RECORDS['records'] if entry['id'] == 'outer_entry_field_used_in_body')
        # the migrated text really does rename the local
        text = migrated('order_rules_a.rule')
        self.assertIn('for entry_local in orders', text)
        self.assertIn('entry_local.qty > entry', text)
        self.assertIn('entry_local.amount * entry + total', text)
        # evaluation agrees with the legacy rule, which means the outer `entry`
        # field value (5) is not replaced by a row's amount/qty
        old_value, new_value = self.evaluate('order_rules_a.rule', entry)
        self.assertIs(old_value, new_value)

    def test_inner_comprehension_collision_is_observable(self):
        entry = next(entry for entry in RECORDS['records'] if entry['id'] == 'inner_local_collision')
        text = migrated('order_rules_a.rule')
        self.assertIn('for rate_local in entry_local.slices', text)
        self.assertIn('rate_local - rate', text)
        # a naive rewrite that renamed only the declaration, or that renamed
        # the outer `total` into the local instead of the field, changes the
        # $sum: the local and the input column are distinguishable values here
        naive_inner = text.replace('rate_local', 'rate')
        naive_value = rule_engine.Rule(naive_inner).evaluate(record(entry, side='new'))
        old_value, new_value = self.evaluate('order_rules_a.rule', entry)
        self.assertIs(old_value, new_value)
        self.assertNotEqual(naive_value, new_value)

    def test_two_equivalent_sources_stay_equivalent(self):
        source_a = source('order_rules_a.rule')
        source_b = source('order_rules_b.rule')
        self.assertNotEqual(source_a, source_b)
        for entry in RECORDS['records']:
            with self.subTest(record=entry['id']):
                a_old, a_new = self.evaluate('order_rules_a.rule', entry)
                b_old, b_new = self.evaluate('order_rules_b.rule', entry)
                self.assertEqual(a_old, b_old)
                self.assertEqual(a_new, b_new)

    def test_strings_comments_and_partner_attributes_are_untouched(self):
        for name in ('order_rules_a.rule', 'order_rules_b.rule'):
            old_text = source(name)
            new_text = migrated(name)
            self.assertIn('note == "rate total fee"', new_text)
            self.assertIn('# rate, total and fee in this trailing comment are literal words', new_text)
            self.assertIn('partner.rate < partner.total', new_text)
            self.assertIn('partner.fee == entry', new_text)
            # the literal and comment portions are identical between the two texts
            for unchanged in ('note == "rate total fee"', '# rate, total and fee in this trailing comment are literal words'):
                self.assertEqual(old_text.count(unchanged), new_text.count(unchanged))

    def test_every_new_record_is_the_exact_swap(self):
        # each fixture's new record must be its old record with rate and total
        # exchanged and fee renamed, so the comparison is one migration and
        # not two unrelated inputs
        for entry in RECORDS['records']:
            expected = {key: value for key, value in entry['old'].items() if key not in ('rate', 'total', 'fee')}
            expected.update(rate=entry['old']['total'], total=entry['old']['rate'], entry=entry['old']['fee'])
            self.assertEqual(entry['new'], expected, entry['id'])

    def test_token_stream_preserves_non_field_spellings(self):
        from migrate_rules import tokenize
        for name in ('order_rules_a.rule', 'order_rules_b.rule'):
            old_tokens = tokenize(source(name))
            new_tokens = tokenize(migrated(name))
            # same number of tokens in the same order; only the renamed
            # symbols may differ, and only in their value
            self.assertEqual([token.type for token in old_tokens], [token.type for token in new_tokens])
            for old_token, new_token in zip(old_tokens, new_tokens):
                if old_token.type in ('STRING', 'COMMENT'):
                    self.assertEqual(old_token.value, new_token.value)

    def test_changes_cover_every_modified_symbol(self):
        for name, records in CHANGES.items():
            text = source(name + '.rule')
            rule = RULES[name]
            migrated_text = rule['migrated']
            if rule['publishable']:
                self.assertEqual(migrated_text, migrated(name + '.rule'))
            rebuilt = text
            for record in sorted(records, key=lambda record: record['offset'], reverse=True):
                with self.subTest(rule=name, line=record['line'], column=record['column']):
                    self.assertEqual(text[record['offset']:record['offset'] + len(record['before'])], record['before'])
                    if record['role'] == 'field':
                        self.assertEqual(record['after'], {'rate': 'total', 'total': 'rate', 'fee': 'entry'}[record['before']])
                    rebuilt = rebuilt[:record['offset']] + record['after'] + rebuilt[record['offset'] + len(record['before']):]
            # applying exactly the recorded changes, and nothing else,
            # reproduces the migrated text
            self.assertEqual(rebuilt, migrated_text)

    def test_selector_diagnostics_and_undeclared_values(self):
        for name in ('order_rules_a', 'order_rules_b'):
            diagnostics = DIAGNOSTICS[name]
            self.assertEqual(diagnostics['selector_map'], {'standard': 'total', 'promo': 'rate'})
            self.assertEqual(diagnostics['selector_declaration'], {
                'standard': {'old': 'rate', 'new': 'total'},
                'promo': {'old': 'total', 'new': 'rate'},
            })
            self.assertEqual(diagnostics['unresolved'], [])
            lookups = diagnostics['selector_lookups']
            self.assertEqual(len(lookups), 1)
            self.assertEqual(lookups[0]['declared'], ['standard', 'promo'])
            self.assertEqual(lookups[0]['mapping'], {'standard': 'total', 'promo': 'rate'})
            self.assertEqual(lookups[0]['values'], {'standard': 'rate', 'promo': 'total'})
        blocked = DIAGNOSTICS['order_rules_undeclared']
        self.assertFalse(RULES['order_rules_undeclared']['publishable'])
        self.assertEqual([record['key'] for record in blocked['unresolved']], ['clearance'])
        self.assertIn('"clearance": rate', (MIGRATION_DIR / 'blocked' / 'order_rules_undeclared.rule').read_text(encoding='utf-8'))

    def test_selector_member_must_match_declared_old_column(self):
        from migrate_rules import migrate
        text = 'rate < total\nand { "standard": total, "promo": rate }[selector] >= fee\n'
        migrated_text, changes, diagnostics = migrate(text, rule_name='mismatch')
        self.assertEqual(
                [(record['kind'], record['key']) for record in diagnostics['unresolved']],
                [('selector-value-mismatch', 'standard'), ('selector-value-mismatch', 'promo')],
        )
        # neither member is guessed at
        self.assertIn('{ "standard": total, "promo": rate }[selector]', migrated_text)

    def test_undeclared_selector_reference_is_reported(self):
        from migrate_rules import migrate
        text = (
            'rate < total\n'
            'and { "standard": rate, "promo": total, "clearance": rate }[selector] >= fee\n'
        )
        migrated_text, changes, diagnostics = migrate(text, rule_name='undeclared')
        self.assertEqual(diagnostics['unresolved'][0]['key'], 'clearance')
        self.assertIn('"clearance": rate', migrated_text)
        self.assertIn("selector value 'clearance' is not in the business-declared map", diagnostics['unresolved'][0]['reason'])
        # the old text remains available through the diagnostics record
        self.assertEqual(diagnostics['rule'], 'undeclared')
        self.assertEqual(diagnostics['unresolved'][0]['old_rule_text'], text)
        self.assertEqual(diagnostics['unresolved'][0]['old_reference'], '"clearance": rate }[selector]')


if __name__ == '__main__':
    unittest.main()
