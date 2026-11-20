"""Tests for order-preserving batching (``WriteOptions(ordered=True)``).

The tests replace the urllib3 pool manager of the client with a local,
recording transport.  The transport is the same layer the real client uses to
send ``/api/v2/write`` requests, so the tests observe actual send order while
staying fully offline.
"""

# coding: utf-8
import json
import threading
import time
import unittest
from urllib.parse import urlparse

from urllib3 import HTTPResponse

from influxdb_client import InfluxDBClient
from influxdb_client.client.write.ordered import BatchNotSentError, WriteResultUnknownError
from influxdb_client.client.write_api import WriteOptions, WriteApi


class Gate(object):
    """A one-shot gate that a test can open from another thread."""

    def __init__(self):
        self._event = threading.Event()

    def open(self):
        self._event.set()

    def wait(self, timeout=10.0):
        if not self._event.wait(timeout):
            raise AssertionError('Timed out waiting for the gate to open')


class DroppedConnection(Exception):
    """Transport failure after a request was handed to the server."""


class RecordingPoolManager(object):
    """Local write transport recording request send order.

    A responder receives the recorded call and returns ``(status, body)``,
    or raises :class:`DroppedConnection` to model a lost response.
    """

    def __init__(self):
        self.lock = threading.Lock()
        self.arrived = threading.Condition(self.lock)
        self.calls = []  # dicts: order, bucket, org, precision, body
        self.responders = {}
        self._order = 0
        self._gates = []

    def close(self):
        for gate in self._gates:
            gate.open()

    def new_gate(self):
        gate = Gate()
        self._gates.append(gate)
        return gate

    def request(self, method, url, body=None, headers=None, **kwargs):
        parsed = urlparse(url)
        query = dict(pair.split('=', 1) for pair in parsed.query.split('&') if pair)
        if callable(body):
            body = b''.join(body())
        with self.lock:
            self._order += 1
            call = {
                'order': self._order,
                'method': method,
                'path': parsed.path,
                'bucket': query.get('bucket'),
                'org': query.get('org'),
                'precision': query.get('precision'),
                'body': body,
            }
            self.calls.append(call)
            self.arrived.notify_all()

        responder = self.responders.get(call['bucket'])
        if responder is None:
            status, payload = 204, b''
        else:
            status, payload = responder(call)

        if payload is None:
            payload = b''
        response = HTTPResponse(body=payload or b' ', status=status, reason='OK',
                                headers={'Content-Type': 'application/json'},
                                preload_content=True)
        if response.data is None:
            # urllib3 maps a zero-length body to None; the REST layer expects
            # a body it can decode.  (204 responses ignore the body anyway.)
            response._body = b''
        return response

    def bodies(self, bucket=None):
        with self.lock:
            return [call['body'] for call in self.calls
                    if bucket is None or call['bucket'] == bucket]

    def bucket_order(self):
        with self.lock:
            return [call['bucket'] for call in self.calls]

    def count(self, bucket=None):
        with self.lock:
            return len([call for call in self.calls
                        if bucket is None or call['bucket'] == bucket])

    def wait_for(self, predicate, timeout=10.0, description='condition'):
        deadline = time.time() + timeout
        with self.lock:
            while not predicate(self.calls):
                remaining = deadline - time.time()
                if remaining <= 0:
                    raise AssertionError('Timed out waiting for %s; calls: %s'
                                         % (description, [(c['bucket'], c['body']) for c in self.calls]))
                self.arrived.wait(remaining)

    def wait_for_count(self, bucket, count, timeout=10.0):
        self.wait_for(lambda calls: len([c for c in calls if c['bucket'] == bucket]) >= count,
                      timeout=timeout,
                      description='%s call(s) to %s' % (count, bucket))


def ok():
    return 204, b''


def error(status, message='error'):
    return status, json.dumps({'message': message}).encode()


class OrderedBatchingTest(unittest.TestCase):
    """Replay the two outcomes of an ambiguous write over a local transport."""

    def setUp(self) -> None:
        self.transport = RecordingPoolManager()
        self.client = InfluxDBClient(url='http://localhost', token='my-token')
        self.client.api_client.rest_client.pool_manager = self.transport
        self.callbacks = []
        self.write_api = None

    def tearDown(self) -> None:
        try:
            if self.write_api is not None:
                self.write_api.close()
        finally:
            self.transport.close()
            self.client.close()

    def _write_api(self, **options):
        options.setdefault('batch_size', 1)
        options.setdefault('flush_interval', 60_000)
        options.setdefault('retry_interval', 0)
        options.setdefault('ordered', True)
        self.write_api = WriteApi(
            influxdb_client=self.client,
            write_options=WriteOptions(**options),
            success_callback=lambda key, data: self.callbacks.append(('success', key, data)),
            error_callback=lambda key, data, error: self.callbacks.append(('error', key, data, error)),
            retry_callback=lambda key, data, error: self.callbacks.append(('retry', key, data, error)))
        return self.write_api

    def _events(self, kind, bucket=None):
        return [event for event in self.callbacks
                if event[0] == kind and (bucket is None or event[1][0] == bucket)]

    def _statuses(self, include_completed=False):
        return {(batch.bucket, batch.data): batch.status.value
                for batch in self.write_api.ordered_batches(include_completed=include_completed)}

    def _wait_blocked(self, timeout=10.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if self.write_api.blocked_ordered_targets():
                return
            time.sleep(0.02)
        self.fail('Target did not become blocked: %s' % (self.write_api.ordered_batches(),))

    def _wait_for(self, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.02)

    # ------------------------------------------------------------------
    # Path 1: A gets a retryable error, then the retry is held, then succeeds
    # ------------------------------------------------------------------
    def test_later_success_releases_ordered_target(self):
        gate = self.transport.new_gate()
        state = {'a_attempts': 0}

        def responder_a(call):
            state['a_attempts'] += 1
            if call['body'] == b'old-readings value=1 100':
                if state['a_attempts'] == 1:
                    return error(503, 'try later')
                # A's next attempt stays in flight until the gate opens.
                gate.wait()
            return ok()

        self.transport.responders['target-a'] = responder_a
        self.transport.responders['target-c'] = lambda call: ok()

        self._write_api(max_retries=5, max_retry_time=60_000)

        # Window 1 -> A (old readings).
        self.write_api.write('target-a', 'org', 'old-readings value=1 100')
        self.write_api.flush()
        self.transport.wait_for_count('target-a', 2)  # retry is now in flight

        # Window 2 -> B (correction on the same target) and independent C.
        self.write_api.write('target-a', 'org', 'correction value=2 100')
        self.write_api.write('target-c', 'org', 'independent-c value=1 100')
        self.write_api.flush()

        self.transport.wait_for_count('target-c', 1)
        self._wait_for(lambda: len(self._events('success', 'target-c')) == 1)
        self.assertEqual(1, len(self._events('success', 'target-c')),
                         'C must complete while A is still unresolved')

        # B must not have been sent before A is confirmed.
        self.assertEqual([], [body for body in self.transport.bodies('target-a')
                              if body == b'correction value=2 100'])
        statuses = self._statuses()
        self.assertEqual('unsent', statuses[('target-a', b'correction value=2 100')])
        self.assertEqual('in_flight', statuses[('target-a', b'old-readings value=1 100')])

        gate.open()
        self.transport.wait_for_count('target-a', 3)
        self._wait_for(lambda: len(self._events('success', 'target-a')) == 2)

        # A was retried (503 then success), and only then was B sent.
        self.assertEqual([b'old-readings value=1 100', b'old-readings value=1 100',
                          b'correction value=2 100'],
                         self.transport.bodies('target-a'))
        self.assertEqual(['target-a', 'target-a', 'target-c', 'target-a'],
                         self.transport.bucket_order())
        self.assertEqual([b'old-readings value=1 100', b'correction value=2 100'],
                         [event[2] for event in self._events('success', 'target-a')])
        self.assertEqual([], self._events('error'))

    # ------------------------------------------------------------------
    # Path 2: A's retries are exhausted and the result is unknown
    # ------------------------------------------------------------------
    def test_unknown_a_blocks_target_without_silent_resend(self):
        gate_c = self.transport.new_gate()

        def responder_a(call):
            if call['body'] == b'old-readings value=1 100':
                if self.transport.count('target-a') == 1:
                    return error(503, 'try later')
                raise DroppedConnection('connection reset after the request was sent')
            return ok()

        def responder_c(call):
            gate_c.wait()
            return ok()

        self.transport.responders['target-a'] = responder_a
        self.transport.responders['target-c'] = responder_c

        self._write_api(max_retries=2, max_retry_time=60_000)

        self.write_api.write('target-a', 'org', 'old-readings value=1 100')
        self.write_api.flush()
        self.write_api.write('target-a', 'org', 'correction value=2 100')
        self.write_api.write('target-c', 'org', 'independent-c value=1 100')
        self.write_api.flush()

        # A: one retryable answer + two dropped connections (initial + 2 retries).
        self.transport.wait_for_count('target-a', 3)
        self._wait_blocked()

        self.assertEqual([('target-a', 'org', 'ns')],
                         [tuple(key) for key in self.write_api.blocked_ordered_targets()])
        statuses = self._statuses()
        self.assertEqual('unconfirmed', statuses[('target-a', b'old-readings value=1 100')])
        self.assertEqual('unsent', statuses[('target-a', b'correction value=2 100')])

        # Nothing follows A on its target while it is blocked.
        time.sleep(0.3)
        self.assertEqual([b'old-readings value=1 100'] * 3, self.transport.bodies('target-a'))

        # C finishes independently even though A is blocked.
        gate_c.open()
        self.transport.wait_for_count('target-c', 1)
        self._wait_for(lambda: len(self._events('success', 'target-c')) == 1)
        self.assertEqual(1, len(self._events('success', 'target-c')))

        # The caller explicitly continues the target; B is sent, A is not re-sent.
        self.assertTrue(self.write_api.continue_ordered_target('target-a', 'org'))
        self.assertFalse(self.write_api.continue_ordered_target('target-a', 'org'))
        self.transport.wait_for_count('target-a', 4)
        self._wait_for(lambda: len(self._events('success', 'target-a')) == 1)

        self.assertEqual([b'old-readings value=1 100'] * 3 + [b'correction value=2 100'],
                         self.transport.bodies('target-a'))
        self.assertEqual([b'correction value=2 100'],
                         [event[2] for event in self._events('success', 'target-a')])

        errors = self._events('error', 'target-a')
        self.assertTrue(any(isinstance(event[3], WriteResultUnknownError) for event in errors))
        # An unconfirmed batch is never reported as delivered.
        self.assertNotIn(b'old-readings value=1 100',
                         [event[2] for event in self._events('success')])

    # ------------------------------------------------------------------
    # Close while blocked: state stays visible and is never "delivered"
    # ------------------------------------------------------------------
    def test_close_preserves_blocked_state(self):
        def responder_a(call):
            if call['body'] == b'old-readings value=1 100':
                raise DroppedConnection('no response')
            return ok()

        self.transport.responders['target-a'] = responder_a
        self.transport.responders['target-c'] = lambda call: ok()

        self._write_api(max_retries=0, max_close_wait=100)

        self.write_api.write('target-a', 'org', 'old-readings value=1 100')
        self.write_api.flush()
        self.write_api.write('target-a', 'org', 'correction value=2 100')
        self.write_api.write('target-c', 'org', 'independent-c value=1 100')
        self.write_api.flush()

        self.transport.wait_for_count('target-c', 1)
        self._wait_for(lambda: len(self._events('success', 'target-c')) == 1)
        self._wait_blocked()

        before_close = self._statuses()

        self.write_api.close()
        self.write_api.close()  # idempotent

        after_close = self._statuses()
        self.assertEqual('unconfirmed', after_close[('target-a', b'old-readings value=1 100')])
        self.assertEqual('unsent', after_close[('target-a', b'correction value=2 100')])
        self.assertEqual(before_close, after_close)

        # Only the first attempt of A was made, and no blocked batch was
        # reported as successfully written.
        self.assertEqual([b'old-readings value=1 100'], self.transport.bodies('target-a'))
        self.assertNotIn(b'correction value=2 100',
                         [event[2] for event in self._events('success')])
        self.assertNotIn(b'old-readings value=1 100',
                         [event[2] for event in self._events('success')])

    def test_abandon_reports_unsent_batches(self):
        def responder_a(call):
            if call['body'] == b'first value=1 100':
                raise DroppedConnection('no response')
            return ok()

        self.transport.responders['target-a'] = responder_a
        self._write_api(max_retries=0)

        self.write_api.write('target-a', 'org', 'first value=1 100')
        self.write_api.flush()
        self.transport.wait_for_count('target-a', 1)
        self.write_api.write('target-a', 'org', 'second value=2 100')
        self.write_api.flush()

        self._wait_blocked()

        self.assertTrue(self.write_api.abandon_ordered_target('target-a', 'org'))
        self.assertFalse(self.write_api.abandon_ordered_target('target-a', 'org'))

        self.assertEqual([b'first value=1 100'], self.transport.bodies('target-a'))
        errors = self._events('error', 'target-a')
        self.assertTrue(any(isinstance(event[3], BatchNotSentError) for event in errors))
        self.assertEqual('abandoned', self._statuses(include_completed=True)[
            ('target-a', b'second value=2 100')])

    def test_multiple_flushes_keep_later_windows_enqueued(self):
        gate = self.transport.new_gate()
        state = {'first': True}

        def responder_a(call):
            if state['first']:
                state['first'] = False
                gate.wait()
            return ok()

        self.transport.responders['target-a'] = responder_a
        self._write_api(batch_size=10, flush_interval=60_000)

        self.write_api.write('target-a', 'org', 'first value=1 100')
        self.write_api.flush()
        self.transport.wait_for_count('target-a', 1)

        # Several flush() calls while the first request is still in flight.
        self.write_api.write('target-a', 'org', 'second value=2 100')
        self.write_api.flush()
        self.write_api.write('target-a', 'org', 'third value=3 100')
        self.write_api.flush()

        statuses = self._statuses()
        self.assertEqual('in_flight', statuses[('target-a', b'first value=1 100')])
        self.assertEqual('unsent', statuses[('target-a', b'second value=2 100')])
        self.assertEqual('unsent', statuses[('target-a', b'third value=3 100')])

        gate.open()
        self.transport.wait_for_count('target-a', 3)
        self._wait_for(lambda: len(self._events('success', 'target-a')) == 3)
        self.assertEqual([b'first value=1 100', b'second value=2 100', b'third value=3 100'],
                         self.transport.bodies('target-a'))

    def test_definitive_failure_does_not_block_target(self):
        state = {'attempts': 0}

        def responder_a(call):
            if call['body'] == b'old-readings value=1 100':
                state['attempts'] += 1
                return error(503, 'still unavailable')
            return ok()

        self.transport.responders['target-a'] = responder_a
        self._write_api(max_retries=1, retry_interval=0)

        self.write_api.write('target-a', 'org', 'old-readings value=1 100')
        self.write_api.flush()
        self.write_api.write('target-a', 'org', 'correction value=2 100')
        self.write_api.flush()

        # initial attempt + 1 retry, then the target is free for the next batch
        self.transport.wait_for_count('target-a', 3)
        self._wait_for(lambda: len(self._events('error', 'target-a')) == 1)
        self._wait_for(lambda: len(self._events('success', 'target-a')) == 1)

        self.assertEqual([], self.write_api.blocked_ordered_targets())
        self.assertEqual([b'old-readings value=1 100'] * 2 + [b'correction value=2 100'],
                         self.transport.bodies('target-a'))

    def test_ordered_disabled_uses_existing_batching_path(self):
        options = WriteOptions(batch_size=1)
        self.assertFalse(options.ordered)

        write_api = WriteApi(influxdb_client=self.client, write_options=options)
        try:
            self.assertIsNone(write_api._ordered_writer)
            self.assertIsNotNone(write_api._subject)
            with self.assertRaises(ValueError):
                write_api.ordered_batches()
        finally:
            write_api.close()


if __name__ == '__main__':
    unittest.main()
