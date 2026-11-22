#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  tests/payment_chain.py
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

import os
import sys
import unittest

sys.path.append(os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'examples')))

import payment_chain_analysis as payment

__all__ = ('PaymentChainTests',)


class PaymentChainTests(unittest.TestCase):
    def test_validate_order_accepts_well_formed_orders(self):
        self.assertEqual(payment.validate_order(
            {'status': 'paid', 'amount': payment.AMOUNT_MAX, 'refunded': payment.AMOUNT_MAX}), [])
        self.assertEqual(payment.validate_order(
            {'status': 'review', 'amount': 100, 'refunded': 100}), [])

    def test_validate_order_rejects_invalid_orders(self):
        self.assertTrue(payment.validate_order({'status': 'closed', 'amount': 1, 'refunded': 0}))
        self.assertTrue(payment.validate_order({'status': 'open', 'amount': -1, 'refunded': 0}))
        self.assertTrue(payment.validate_order(
            {'status': 'open', 'amount': payment.AMOUNT_MAX + 1, 'refunded': 0}))
        self.assertTrue(payment.validate_order({'status': 'open', 'amount': 10, 'refunded': 11}))
        self.assertTrue(payment.validate_order({'status': 'open', 'amount': 1.5, 'refunded': 0}))

    def test_analytic_rules_never_call_the_scoring_service(self):
        # the stand-in raises if anything reaches the scoring service; the analysis must still complete
        chain = payment.build_chain(payment.REVISED_RULE_TEXTS)
        verdicts = payment.analyze_chain(chain)
        self.assertEqual([verdict.position for verdict in verdicts], list(range(len(payment.REVISED_RULE_TEXTS))))

    def test_revised_chain_makes_every_rule_a_possible_first_hit(self):
        verdicts = payment.analyze_chain(payment.build_chain(payment.REVISED_RULE_TEXTS))
        self.assertEqual(
            [verdict.verdict for verdict in verdicts],
            [payment.FIRST_HIT] * 6,
        )

    def test_one_cent_move_turns_the_corner_into_a_first_hit(self):
        original = payment.analyze_chain(payment.build_chain(payment.ORIGINAL_RULE_TEXTS))
        revised = payment.analyze_chain(payment.build_chain(payment.REVISED_RULE_TEXTS))
        self.assertEqual(original[2].verdict, payment.SHADOWED)
        self.assertEqual(original[2].intercepted_by, 0)
        self.assertEqual(revised[2].verdict, payment.FIRST_HIT)
        corner = revised[2].witness
        self.assertEqual(corner, {'status': 'paid', 'amount': 1000, 'refunded': 0})
        self.assertEqual(payment.verify_witness(payment.ORIGINAL_RULE_TEXTS, corner, 0), 0)
        self.assertEqual(payment.verify_witness(payment.REVISED_RULE_TEXTS, corner, 2), 2)

    def test_complementary_pair_shares_one_unknown_fact_and_covers_the_band(self):
        verdicts = payment.analyze_chain(payment.build_chain(payment.REVISED_RULE_TEXTS))
        self.assertIsNotNone(verdicts[3].score_key)
        self.assertEqual(verdicts[3].score_key, verdicts[4].score_key)
        witness = verdicts[3].witness
        self.assertEqual(witness, verdicts[4].witness)
        # either value of the single unknown fact lands on one of the two adjacent rules
        self.assertEqual(payment.verify_witness(payment.REVISED_RULE_TEXTS, witness, 3, payment.SCORE_CUTOFF), 3)
        self.assertEqual(payment.verify_witness(payment.REVISED_RULE_TEXTS, witness, 4, payment.SCORE_CUTOFF - 1), 4)
        for amount in (payment.REVIEW_BAND_MIN, 15000, payment.REVIEW_BAND_MAX_EXCLUSIVE - 1):
            order = {'status': 'review', 'amount': amount, 'refunded': 0}
            for score_value in (0, payment.SCORE_CUTOFF - 1, payment.SCORE_CUTOFF, 99):
                chain = payment.build_chain(payment.REVISED_RULE_TEXTS, payment.score_double(score_value))
                self.assertIn(chain.index(chain.find(order)), (3, 4))

    def test_repeated_score_expression_is_the_same_unknown_fact(self):
        verdicts = payment.analyze_chain(payment.build_chain(payment.REVISED_RULE_TEXTS))
        self.assertEqual(verdicts[3].score_key, ('risk_score', ('amount', 'refunded')))
        self.assertEqual(verdicts[4].score_key, ('risk_score', ('amount', 'refunded')))

    def test_different_score_arguments_are_a_separate_unknown(self):
        texts = payment.REVISED_RULE_TEXTS + [
            'status == "review" and amount >= {0} and $risk_score(amount, amount - refunded) >= {1}'.format(
                payment.REVIEW_BAND_MAX_EXCLUSIVE, payment.SCORE_CUTOFF),
        ]
        verdicts = payment.analyze_chain(payment.build_chain(texts))
        self.assertEqual(verdicts[-1].verdict, payment.UNKNOWN)
        self.assertEqual(verdicts[-1].score_key, ('risk_score', ('amount', 'balance')))

    def test_unsatisfiable_region_is_distinct_from_a_shadowed_rule(self):
        texts = payment.REVISED_RULE_TEXTS + [
            'status == "review" and amount >= {0} and amount < {1}'.format(
                payment.REVIEW_BAND_MAX_EXCLUSIVE, payment.REVIEW_BAND_MIN),
        ]
        verdicts = payment.analyze_chain(payment.build_chain(texts))
        self.assertEqual(verdicts[-1].verdict, payment.UNSATISFIABLE)
        original = payment.analyze_chain(payment.build_chain(payment.ORIGINAL_RULE_TEXTS))
        self.assertEqual(original[2].verdict, payment.SHADOWED)

    def test_equality_and_inequality_on_the_same_pinned_value_is_unsatisfiable(self):
        texts = payment.REVISED_RULE_TEXTS + [
            'status == "review" and amount == 10000 and amount != 10000',
        ]
        verdicts = payment.analyze_chain(payment.build_chain(texts))
        self.assertEqual(verdicts[-1].verdict, payment.UNSATISFIABLE)

    def test_unanalyzable_form_scopes_the_unknown_to_its_own_rule(self):
        texts = payment.REVISED_RULE_TEXTS + ['status == "review" or amount >= 10000']
        verdicts = payment.analyze_chain(payment.build_chain(texts))
        self.assertEqual(verdicts[-1].verdict, payment.UNKNOWN)
        self.assertEqual(
            [verdict.verdict for verdict in verdicts[:6]],
            [payment.FIRST_HIT] * 6,
        )

    def test_witnesses_are_concrete_and_replay_through_the_real_chain(self):
        verdicts = payment.analyze_chain(payment.build_chain(payment.REVISED_RULE_TEXTS))
        for verdict in verdicts:
            self.assertIsNotNone(verdict.witness)
            self.assertEqual(payment.validate_order(verdict.witness), [])

    def test_sixty_four_bit_amount_boundary_is_an_analyzable_corner(self):
        texts = payment.REVISED_RULE_TEXTS + [
            'status == "review" and amount == {0}'.format(payment.AMOUNT_MAX),
        ]
        verdicts = payment.analyze_chain(payment.build_chain(texts))
        self.assertEqual(verdicts[-1].verdict, payment.FIRST_HIT)
        witness = verdicts[-1].witness
        self.assertEqual(witness['amount'], payment.AMOUNT_MAX)
        self.assertEqual(payment.verify_witness(texts, witness, 6), 6)


if __name__ == '__main__':
    unittest.main()
