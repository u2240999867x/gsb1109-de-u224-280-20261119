#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  tests/delivery.py
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

import threading
import unittest

from rule_engine.delivery import ConfirmationLost
from rule_engine.delivery import DeliveryOutcome
from rule_engine.delivery import DeliveryPipeline
from rule_engine.delivery import DeliverySink
from rule_engine.delivery import SinkTerminated
from rule_engine.delivery import SinkUnavailable

ACCEPTED = DeliveryOutcome.ACCEPTED
REJECTED = DeliveryOutcome.REJECTED
ERROR = DeliveryOutcome.ERROR

__all__ = ('DeliveryPipelineTests', 'DeliverySinkContractTests')


class ControlledSource(object):
    """A forward-only source whose pull count and failure point are observable."""
    def __init__(self, rows, fail_at=None):
        self.rows = list(rows)
        self.fail_at = fail_at
        self.pulls = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.fail_at is not None and self.pulls == self.fail_at:
            raise RuntimeError('source iterator failed at position {0}'.format(self.pulls))
        if self.pulls >= len(self.rows):
            raise StopIteration()
        row = self.rows[self.pulls]
        self.pulls += 1
        return row

    def __len__(self):
        return len(self.rows)


class RecordingResolver(object):
    """Evaluates rows by source position and records every visit."""
    def __init__(self, verdicts, error_positions=()):
        self.verdicts = dict(verdicts)
        self.error_positions = set(error_positions)
        self.calls = []

    def __call__(self, row):
        position = len(self.calls)
        self.calls.append((position, row[0]))
        if position in self.error_positions:
            raise ValueError('evaluation failed at position {0}'.format(position))
        return self.verdicts[position]


class SignalSink(DeliverySink):
    """A sink driven by controllable signals that keeps a full audit trail of every call."""
    def __init__(self, branch):
        self.branch = branch
        self.landed = {}
        self.deliver_calls = []
        self.lookup_calls = []
        self.confirm_calls = []
        self.lose_confirmation = set()
        self.terminate_after = set()
        self.terminated = False
        # while set, deliver() transiently refuses without writing anything
        self.blocked = threading.Event()
        # while clear, identity lookups cannot yet observe records that have landed
        self.visible = threading.Event()
        self.visible.set()

    def deliver(self, record):
        self.deliver_calls.append(record.identity)
        if self.terminated:
            raise SinkTerminated('{0} sink terminated'.format(self.branch.name))
        if self.blocked.is_set():
            raise SinkUnavailable('{0} sink temporarily unavailable'.format(self.branch.name))
        self.landed[record.identity] = record
        if record.identity in self.terminate_after:
            self.terminated = True
        if record.identity in self.lose_confirmation:
            raise ConfirmationLost('confirmation lost for {0}'.format(record.identity,))

    def is_delivered(self, identity):
        self.lookup_calls.append(identity)
        return self.visible.is_set() and identity in self.landed

    def confirm(self, record):
        self.confirm_calls.append(record.identity)


class DeliveryPipelineTests(unittest.TestCase):
    def _build(self, rows, verdicts, buffer_size, error_positions=(), fail_at=None):
        source = ControlledSource(rows, fail_at=fail_at)
        resolver = RecordingResolver(verdicts, error_positions)
        sinks = {outcome: SignalSink(outcome) for outcome in DeliveryOutcome}
        pipeline = DeliveryPipeline(source, resolver, sinks, buffer_size=buffer_size)
        return source, resolver, sinks, pipeline

    def test_local_delivery_round_with_full_buffer_and_recovery(self):
        rows = [
            ('B', 'row-0'),
            ('A', 'row-1'),
            ('B', 'row-2'),
            ('C', 'row-3'),
            ('D', 'row-4'),
            ('B', 'row-5'),
        ]
        verdicts = {0: REJECTED, 1: ACCEPTED, 2: ERROR, 3: ACCEPTED, 4: REJECTED, 5: ERROR}
        source, resolver, sinks, pipeline = self._build(rows, verdicts, buffer_size=3)
        rejected, accepted, errors = sinks[REJECTED], sinks[ACCEPTED], sinks[ERROR]
        rejected.blocked.set()
        errors.visible.clear()
        errors.lose_confirmation.add((ERROR, 2))

        first = pipeline.pump()
        # the accepted branch keeps confirming while the rejection side is unavailable
        self.assertEqual([record.source_position for record in first.delivered], [1, 3])
        self.assertTrue(first.buffer_full)
        self.assertEqual(first.buffer_used, 3)
        self.assertEqual(first.next_source_position, 5)
        self.assertEqual(pipeline.unconsumed_count, 1)
        self.assertEqual(set(first.blocked), {REJECTED, ERROR})
        self.assertEqual(first.blocked_reasons[REJECTED], 'unavailable')
        self.assertEqual(first.blocked_reasons[ERROR], 'unconfirmed')
        self.assertEqual(first.blocked[REJECTED].source_position, 0)
        self.assertEqual(first.blocked[ERROR].source_position, 2)
        self.assertEqual(first.unconfirmed, frozenset({(ERROR, 2)}))
        # the error record really landed before its confirmation was lost
        self.assertEqual(set(errors.landed), {(ERROR, 2)})
        self.assertEqual(errors.deliver_calls, [(ERROR, 2)])
        self.assertEqual(errors.confirm_calls, [])
        # the rejection head stays in place and its second row is never offered to the sink
        self.assertEqual(rejected.deliver_calls, [(REJECTED, 0)])
        self.assertEqual(
            [record.source_position for record in pipeline.pending_records(REJECTED)],
            [0, 4]
        )
        self.assertEqual(
            [record.source_position for record in accepted.landed.values()],
            [1, 3]
        )

        # recover the error lookup while the rejection is still outstanding: ack before reject
        errors.visible.set()
        second = pipeline.pump()
        self.assertEqual([record.source_position for record in second.delivered], [2, 5])
        self.assertEqual(errors.confirm_calls, [(ERROR, 2)])
        # the unconfirmed record is never written a second time
        self.assertEqual(errors.deliver_calls, [(ERROR, 2), (ERROR, 5)])
        # the rejection head is retried at most once per pump round, but never lands while blocked
        self.assertEqual(rejected.deliver_calls, [(REJECTED, 0), (REJECTED, 0)])
        self.assertEqual(rejected.landed, {})
        self.assertTrue(second.source_exhausted)
        self.assertEqual(second.next_source_position, 6)
        self.assertEqual(pipeline.unconsumed_count, 0)
        self.assertEqual(set(second.blocked), {REJECTED})
        # the unconfirmed error never displaced the earlier rejection row
        self.assertEqual(
            [record.source_position for record in pipeline.pending_records(REJECTED)],
            [0, 4]
        )
        # distinct inputs sharing business id B are all accounted for, none swallowed,
        # whether they have landed yet or are still buffered
        all_records = pipeline.pending_records() + pipeline.delivered_records()
        self.assertEqual(
            sorted(record.source_position for record in all_records if record.business_id == 'B'),
            [0, 2, 5]
        )

        rejected.blocked.clear()
        third = pipeline.pump()
        self.assertEqual([record.source_position for record in third.delivered], [0, 4])

        # final audit: write-side records, source consumption and per-row resolver visits
        self.assertEqual(pipeline.buffer_used, 0)
        self.assertEqual(
            sorted(record.source_position for record in pipeline.delivered_records()),
            [0, 1, 2, 3, 4, 5]
        )
        self.assertEqual(source.pulls, 6)
        self.assertEqual([position for position, _ in resolver.calls], [0, 1, 2, 3, 4, 5])
        for position in range(6):
            self.assertEqual(pipeline.resolver_call_count(position), 1)
        self.assertEqual(pipeline.resolver_call_count(6), 0)
        self.assertEqual(list(rejected.landed), [(REJECTED, 0), (REJECTED, 4)])
        self.assertEqual(list(accepted.landed), [(ACCEPTED, 1), (ACCEPTED, 3)])
        self.assertEqual(list(errors.landed), [(ERROR, 2), (ERROR, 5)])

    def test_resolver_error_routes_to_error_branch_and_is_not_re_evaluated(self):
        rows = [('x', 1), ('y', 2)]
        source, resolver, sinks, pipeline = self._build(
            rows, {0: ACCEPTED, 1: ACCEPTED}, buffer_size=2, error_positions={0}
        )
        pipeline.pump()
        error_records = pipeline.delivered_records(ERROR)
        self.assertEqual([record.source_position for record in error_records], [0])
        self.assertIsInstance(error_records[0].error, ValueError)
        self.assertEqual(error_records[0].business_id, 'x')
        self.assertEqual(
            [record.source_position for record in pipeline.delivered_records(ACCEPTED)],
            [1]
        )
        again = pipeline.pump()
        self.assertEqual(again.delivered, ())
        self.assertEqual(len(resolver.calls), 2)
        self.assertEqual(sinks[ERROR].deliver_calls, [(ERROR, 0)])

    def test_duplicate_business_ids_keep_distinct_identities(self):
        rows = [('dup', index) for index in range(3)]
        _, _, sinks, pipeline = self._build(
            rows, {position: ERROR for position in range(3)}, buffer_size=3
        )
        result = pipeline.pump()
        self.assertTrue(result.source_exhausted)
        identities = sorted(sinks[ERROR].landed)
        self.assertEqual(identities, [(ERROR, 0), (ERROR, 1), (ERROR, 2)])
        self.assertTrue(all(record.business_id == 'dup' for record in sinks[ERROR].landed.values()))

    def test_buffer_full_stops_pulling_and_names_blocking_head(self):
        rows = [('r', 0), ('r', 1)]
        source, _, sinks, pipeline = self._build(
            rows, {0: REJECTED, 1: REJECTED}, buffer_size=1
        )
        sinks[REJECTED].blocked.set()
        result = pipeline.pump()
        self.assertTrue(result.buffer_full)
        self.assertEqual(result.buffer_used, 1)
        self.assertEqual(result.blocked[REJECTED].source_position, 0)
        self.assertEqual(result.blocked_reasons[REJECTED], 'unavailable')
        self.assertEqual(result.next_source_position, 1)
        self.assertEqual(pipeline.unconsumed_count, 1)
        self.assertEqual(source.pulls, 1)

        sinks[REJECTED].blocked.clear()
        drained = pipeline.pump()
        self.assertEqual([record.source_position for record in drained.delivered], [0, 1])
        self.assertEqual(source.pulls, 2)
        self.assertTrue(drained.source_exhausted)

    def test_lost_confirmation_is_recovered_by_identity_without_rewrite(self):
        rows = [('e', 0)]
        _, _, sinks, pipeline = self._build(rows, {0: ERROR}, buffer_size=1)
        errors = sinks[ERROR]
        errors.visible.clear()
        errors.lose_confirmation.add((ERROR, 0))

        first = pipeline.pump()
        self.assertEqual(first.unconfirmed, frozenset({(ERROR, 0)}))
        self.assertEqual(pipeline.delivered_records(), ())
        self.assertEqual(errors.deliver_calls, [(ERROR, 0)])
        self.assertEqual(set(errors.landed), {(ERROR, 0)})

        # still unrecoverable: only a lookup happens, deliver() is never called again
        second = pipeline.pump()
        self.assertEqual(second.delivered, ())
        self.assertEqual(second.blocked_reasons[ERROR], 'unconfirmed')
        self.assertEqual(errors.deliver_calls, [(ERROR, 0)])
        self.assertEqual(errors.lookup_calls, [(ERROR, 0)])

        errors.visible.set()
        third = pipeline.pump()
        self.assertEqual([record.source_position for record in third.delivered], [0])
        self.assertEqual(errors.confirm_calls, [(ERROR, 0)])
        self.assertEqual(errors.deliver_calls, [(ERROR, 0)])
        self.assertEqual(third.unconfirmed, frozenset())

        fourth = pipeline.pump()
        self.assertEqual(fourth.delivered, ())
        self.assertEqual(errors.lookup_calls, [(ERROR, 0), (ERROR, 0)])
        self.assertEqual(errors.confirm_calls, [(ERROR, 0)])

    def test_source_iterator_failure_preserves_delivery_boundaries(self):
        rows = [('r', index) for index in range(5)]
        source, resolver, sinks, pipeline = self._build(
            rows, {position: ACCEPTED for position in range(5)}, buffer_size=10, fail_at=3
        )
        result = pipeline.pump()
        self.assertEqual(
            [record.source_position for record in result.delivered],
            [0, 1, 2]
        )
        self.assertIsNotNone(result.source_failure)
        self.assertEqual(result.source_failure.position, 3)
        self.assertIsInstance(result.source_failure.original, RuntimeError)
        self.assertFalse(result.source_exhausted)
        self.assertEqual(result.next_source_position, 3)
        self.assertEqual(pipeline.unconsumed_count, 2)
        self.assertEqual(source.pulls, 3)

        again = pipeline.pump()
        self.assertEqual(again.delivered, ())
        self.assertIs(again.source_failure, result.source_failure)
        self.assertEqual(source.pulls, 3)
        self.assertEqual(len(resolver.calls), 3)
        self.assertEqual(len(sinks[ACCEPTED].landed), 3)

    def test_sink_termination_leaves_confirmed_pending_and_unconsumed(self):
        rows = [('a', 0), ('a', 1), ('r', 2)]
        source, _, sinks, pipeline = self._build(
            rows, {0: ACCEPTED, 1: ACCEPTED, 2: REJECTED}, buffer_size=1
        )
        accepted, rejected = sinks[ACCEPTED], sinks[REJECTED]
        accepted.terminate_after.add((ACCEPTED, 0))

        result = pipeline.pump()
        self.assertEqual(result.terminated, frozenset({ACCEPTED}))
        self.assertEqual(result.blocked_reasons[ACCEPTED], 'terminated')
        self.assertEqual(result.blocked[ACCEPTED].source_position, 1)
        self.assertEqual([record.source_position for record in result.delivered], [0])
        self.assertEqual(
            [record.source_position for record in pipeline.pending_records(ACCEPTED)],
            [1]
        )
        self.assertEqual(result.next_source_position, 2)
        self.assertEqual(pipeline.unconsumed_count, 1)
        self.assertEqual(list(accepted.landed), [(ACCEPTED, 0)])
        self.assertEqual(accepted.deliver_calls, [(ACCEPTED, 0), (ACCEPTED, 1)])
        self.assertEqual(rejected.deliver_calls, [])

        again = pipeline.pump()
        self.assertEqual(again.delivered, ())
        self.assertEqual(source.pulls, 2)
        self.assertEqual(list(accepted.landed), [(ACCEPTED, 0)])

    def test_unfilled_buffer_delivers_everything_in_one_round(self):
        rows = [('r', 0), ('a', 1), ('e', 2), ('a', 3)]
        _, resolver, sinks, pipeline = self._build(
            rows,
            {0: REJECTED, 1: ACCEPTED, 2: ACCEPTED, 3: ACCEPTED},
            buffer_size=10,
            error_positions={2}
        )
        result = pipeline.pump()
        self.assertTrue(result.source_exhausted)
        self.assertFalse(result.buffer_full)
        self.assertEqual(result.blocked, {})
        self.assertEqual(
            [record.source_position for record in pipeline.delivered_records(ACCEPTED)],
            [1, 3]
        )
        self.assertEqual(
            [record.source_position for record in pipeline.delivered_records(REJECTED)],
            [0]
        )
        self.assertEqual(
            [record.source_position for record in pipeline.delivered_records(ERROR)],
            [2]
        )
        self.assertEqual(len(resolver.calls), 4)

    def test_snapshot_reports_state_without_mutating_it(self):
        rows = [('r', 0)]
        source, _, sinks, pipeline = self._build(
            rows, {0: REJECTED}, buffer_size=1
        )
        sinks[REJECTED].blocked.set()
        snapshot = pipeline.snapshot()
        self.assertEqual(snapshot.buffer_used, 0)
        self.assertEqual(snapshot.next_source_position, 0)
        self.assertEqual(source.pulls, 0)
        pipeline.pump()
        self.assertEqual(source.pulls, 1)


class DeliverySinkContractTests(unittest.TestCase):
    def test_invalid_construction(self):
        resolver = RecordingResolver({})
        with self.assertRaises(ValueError):
            DeliveryPipeline([], resolver, {}, buffer_size=1)
        sinks = {outcome: SignalSink(outcome) for outcome in DeliveryOutcome}
        with self.assertRaises(ValueError):
            DeliveryPipeline([], resolver, sinks, buffer_size=0)

    def test_identities_use_branch_and_source_position(self):
        rows = [('same', 0), ('same', 1)]
        source = ControlledSource(rows)
        resolver = RecordingResolver({0: ERROR, 1: ERROR})
        sinks = {outcome: SignalSink(outcome) for outcome in DeliveryOutcome}
        pipeline = DeliveryPipeline(source, resolver, sinks, buffer_size=2)
        pipeline.pump()
        first, second = sinks[ERROR].landed
        self.assertEqual(first, (ERROR, 0))
        self.assertEqual(second, (ERROR, 1))
        self.assertNotEqual(first, second)
        probe = SignalSink(ERROR)
        self.assertFalse(probe.is_delivered(first))
        probe.landed[first] = object()
        self.assertTrue(probe.is_delivered(first))
        self.assertFalse(probe.is_delivered(second))


if __name__ == '__main__':
    unittest.main()
