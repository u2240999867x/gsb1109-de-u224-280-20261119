import asyncio
import sys

from flaky import flaky
import pytest
from streamz import Source
from streamz.utils_test import free_port, wait_for, await_for, gen_test
import socket


def test_periodic():
    s = Source.from_periodic(lambda: True)
    l = s.sink_to_list()
    assert s.stopped
    s.start()
    wait_for(lambda: l, 0.3, period=0.01)
    wait_for(lambda: len(l) > 1, 0.3, period=0.01)
    assert all(l)


@flaky(max_runs=3, min_passes=1)
def test_tcp():
    port = 9876
    s = Source.from_tcp(port)
    out = s.sink_to_list()
    s.start()
    wait_for(lambda: s.server is not None, 2, period=0.02)

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("localhost", port))
        sock.send(b'data\n')
        sock.close()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("localhost", port))
        sock.send(b'data\n')

        sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock2.connect(("localhost", port))
        sock2.send(b'data2\n')
        wait_for(lambda: out == [b'data\n', b'data\n', b'data2\n'], 2,
                 period=0.01)
    finally:
        s.stop()
        sock.close()
        sock2.close()


@flaky(max_runs=3, min_passes=1)
def test_tcp_word_count_example():
    port = free_port()
    s = Source.from_tcp(port)
    out = s.map(bytes.split).flatten().frequencies().sink_to_list()
    s.start()
    wait_for(lambda: s.server is not None, 2, period=0.02)

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.connect(("localhost", port))
        sock.send(b'data\n')

    with (socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock,
          socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock2):
        sock.connect(("localhost", port))
        sock2.connect(("localhost", port))
        sock.send(b'data\n')
        # regression test a bug in from_tcp where a second packet from
        # the same socket is dropped due to the socket handler dying
        sock.send(b'data\n')
        sock2.send(b'data2\n')

    expected = [{b"data": 1}, {b"data": 2}, {b"data": 3}, {b"data": 3, b"data2": 1}]

    def fail_func():
        assert out == expected

    wait_for(lambda: out == expected, 2, fail_func=fail_func, period=0.01)



@flaky(max_runs=3, min_passes=1)
@gen_test(timeout=60)
def test_tcp_async():
    port = 9876
    s = Source.from_tcp(port)
    out = s.sink_to_list()
    s.start()
    yield await_for(lambda: s.server is not None, 2, period=0.02)

    try:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("localhost", port))
        sock.send(b'data\n')
        sock.close()

        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect(("localhost", port))
        sock.send(b'data\n')

        sock2 = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock2.connect(("localhost", port))
        sock2.send(b'data2\n')
        yield await_for(lambda: out == [b'data\n', b'data\n', b'data2\n'], 2,
                        period=0.01)
    finally:
        s.stop()
        sock.close()
        sock2.close()


def test_http():
    requests = pytest.importorskip('requests')
    port = 9875
    s = Source.from_http_server(port)
    out = s.sink_to_list()
    s.start()
    wait_for(lambda: s.server is not None, 2, period=0.02)

    r = requests.post('http://localhost:%i/' % port, data=b'data')
    wait_for(lambda: out == [b'data'], 2, period=0.01)
    assert r.ok

    r = requests.post('http://localhost:%i/other' % port, data=b'data2')
    wait_for(lambda: out == [b'data', b'data2'], 2, period=0.01)
    assert r.ok

    s.stop()

    with pytest.raises(requests.exceptions.RequestException):
        requests.post('http://localhost:%i/other' % port, data=b'data2')


@gen_test(timeout=60)
def test_process():
    cmd = ["python", "-c", "for i in range(4): print(i, end='')"]
    s = Source.from_process(cmd, with_end=True)
    if sys.platform != "win32" and sys.version_info < (3, 14):
        # don't know why - something with pytest and new processes
        policy = asyncio.get_event_loop_policy()
        watcher = asyncio.SafeChildWatcher()
        policy.set_child_watcher(watcher)
        watcher.attach_loop(s.loop.asyncio_loop)
    out = s.sink_to_list()
    s.start()
    yield await_for(lambda: out == [b'0123'], timeout=5)
    s.stop()


@gen_test(timeout=60)
def test_process_str():
    cmd = 'python -c "for i in range(4): print(i)"'
    s = Source.from_process(cmd)
    if sys.platform != "win32" and sys.version_info < (3, 14):
        # don't know why - something with pytest and new processes
        policy = asyncio.get_event_loop_policy()
        watcher = asyncio.SafeChildWatcher()
        policy.set_child_watcher(watcher)
        watcher.attach_loop(s.loop.asyncio_loop)
    out = s.sink_to_list()
    s.start()
    yield await_for(lambda: out == [b'0\n', b'1\n', b'2\n', b'3\n'], timeout=5)
    s.stop()


def test_from_iterable():
    source = Source.from_iterable(range(3))
    L = source.sink_to_list()
    source.start()
    wait_for(lambda: L == [0, 1, 2], 0.1)


def test_from_iterable_backpressure():
    it = iter(range(5))
    source = Source.from_iterable(it)
    L = source.rate_limit(0.1).sink_to_list()
    source.start()

    wait_for(lambda: L == [0], 1, period=0.01)
    assert next(it) == 2  # 1 is in blocked _emit


def test_from_iterable_stop():
    from _pytest.outcomes import Failed

    source = Source.from_iterable(range(5))
    L = source.rate_limit(0.01).sink_to_list()
    source.start()

    wait_for(lambda: L == [0], 1)
    source.stop()

    assert source.stopped
    with pytest.raises(Failed):
        wait_for(lambda: L == [0, 1, 2], 0.1)


class ControlledWriter:
    """A sink that starts every write immediately and waits for a gate."""

    def __init__(self):
        self.calls = []
        self.gates = {}
        self.failures = {}

    def __call__(self, values):
        self.calls.append(values)
        gate = self.gates.setdefault(values, asyncio.Event())

        async def write():
            await gate.wait()
            if values in self.failures:
                raise self.failures[values]
        return write()

    def release(self, values, error=None):
        if error is not None:
            self.failures[values] = error
        self.gates[values].set()


def _replay_graph(items, timeout=10):
    from tornado.ioloop import IOLoop
    source = Source.from_iterable(items, asynchronous=True,
                                  loop=IOLoop.current())
    partitioned = source.partition(2, timeout=timeout, key=lambda x: x[0])
    writer = ControlledWriter()
    partitioned.sink(writer)
    return source, partitioned, writer


def _values(result):
    return [batch.values for batch in result.batches]


async def _turn(predicate=None, turns=50):
    for _ in range(turns):
        await asyncio.sleep(0)
        if predicate is None or predicate():
            return
    if predicate is not None:
        assert predicate()


async def _started(writer, count):
    await _turn(lambda: len(writer.calls) >= count)


def _run(coro):
    asyncio.run(coro)


def test_replay_waits_for_writes_at_natural_end():
    async def main():
        source, partitioned, writer = _replay_graph(
            [('A', '1'), ('B', '1'), ('C', '1'), ('A', '2')])
        result = source.start()
        await _started(writer, 3)

        # A is a full batch, B and C are non-empty tails at EOF.
        assert _values(result) == [
            (('A', '1'), ('A', '2')),
            (('B', '1'),),
            (('C', '1'),),
        ]
        writer.release((('A', '1'), ('A', '2')))
        writer.release((('B', '1'),))
        await _turn()

        assert not result.done()
        assert result.status == 'pending'
        assert result.pending_batches == []

        writer.release((('C', '1'),))
        finished = await asyncio.wait_for(result, 1)
        assert finished.status == 'completed'
        assert finished.natural_end
        assert finished.input_finished
        assert not finished.write_failed
        assert all(batch.acknowledged for batch in finished.batches)
        assert {record.value for record in finished.records} == {
            ('A', '1'), ('B', '1'), ('C', '1'), ('A', '2')}
        for record in finished.records:
            assert record.delivered
            assert record.acknowledged
            assert record.round_id == finished.round_id
            assert record.batch_id is not None

    _run(main())


def test_replay_timeout_and_eof_deliver_once():
    async def main():
        from tornado.ioloop import IOLoop
        from streamz.core import ReplayRound

        stream = Source(asynchronous=True, loop=IOLoop.current())
        partitioned = stream.partition(2, timeout=10, key=lambda x: x[0])
        writer = ControlledWriter()
        partitioned.sink(writer)

        round = ReplayRound(1, loop=IOLoop.current())
        for value in [('A', '1'), ('B', '1'), ('C', '1'), ('A', '2')]:
            record = round.new_record(value)
            await asyncio.gather(*partitioned.update(
                value, metadata=[{'replay': round, 'record': record}]))

        # A fills at A2.  Now let C's timeout deliver before EOF.
        callback = partitioned._round_callbacks[round.round_id].pop('C')
        callback.cancel()
        partitioned._flush_round(round.round_id, 'C', reason='timeout')
        await _started(writer, 2)
        assert writer.calls == [
            (('A', '1'), ('A', '2')),
            (('C', '1'),),
        ]

        # EOF then owns only B's tail; it must not deliver C again or an
        # empty C batch.
        round.natural_end = True
        round.input_ended = True
        partitioned._finalize_round(round)
        await _started(writer, 3)
        assert writer.calls.count((('C', '1'),)) == 1
        assert writer.calls.count((('B', '1'),)) == 1
        assert writer.calls.count((('A', '1'), ('A', '2'))) == 1
        for values in writer.calls:
            writer.release(values)
        await _turn(lambda: all(batch.done for batch in round.batches))
        assert round.write_failures == []
        assert [batch.values for batch in round.batches].count(
            (('C', '1'),)) == 1
        assert len(round.batches) == 3

    _run(main())


def test_replay_input_error_keeps_tails_unwritten():
    async def main():
        def items():
            yield ('A', '1')
            yield ('B', '1')
            yield ('C', '1')
            yield ('A', '2')
            raise ValueError('source broke')

        source, partitioned, writer = _replay_graph(items())
        result = source.start()
        await _turn(lambda: result.round.input_error is not None)
        await _started(writer, 1)

        assert writer.calls == [(('A', '1'), ('A', '2'))]
        writer.release((('A', '1'), ('A', '2')))
        finished = await asyncio.wait_for(result, 1)

        assert finished.status == 'input_error'
        assert isinstance(finished.input_error, ValueError)
        assert not finished.natural_end
        assert not finished.success
        assert {batch.values for batch in finished.pending_batches} == {
            (('B', '1'),), (('C', '1'),)}
        assert all(not batch.delivered for batch in finished.pending_batches)
        # Undelivered tails are retained in the round result but never sent.
        assert writer.calls == [(('A', '1'), ('A', '2'))]
        for batch in finished.pending_batches:
            assert all(not record.delivered for record in batch.records)

    _run(main())


def test_replay_write_failure_keeps_lineage():
    async def main():
        source, partitioned, writer = _replay_graph(
            [('A', '1'), ('B', '1'), ('C', '1'), ('A', '2')])
        result = source.start()
        await _started(writer, 3)

        writer.release((('A', '1'), ('A', '2')))
        writer.release((('B', '1'),))
        writer.release((('C', '1'),), error=RuntimeError('write broke'))
        finished = await asyncio.wait_for(result, 1)

        assert finished.status == 'write_error'
        assert not finished.success
        assert len(finished.write_failures) == 1
        failed = finished.write_failures[0]
        assert failed.round_id == finished.round_id
        assert failed.values == (('C', '1'),)
        assert isinstance(failed.error, RuntimeError)
        record = next(record for record in finished.records
                      if record.value == ('C', '1'))
        assert record.delivered
        assert not record.acknowledged
        assert record.batch is failed
        assert isinstance(record.error, RuntimeError)

    _run(main())


def test_replay_old_round_cannot_touch_new_round():
    async def main():
        source, partitioned, first_writer = _replay_graph(
            [('A', '1'), ('B', '1'), ('C', '1'), ('A', '2')])
        first = source.start()
        await _started(first_writer, 3)

        # The first day has consumed its input and is waiting on receipts.
        await _turn(lambda: first.round.input_ended)
        first_values = set(record.value for record in first.records)
        assert first.status == 'pending'

        # Start the next day's replay while the first one's writes are open.
        source._iterable = [('A', '3'), ('B', '2'), ('C', '2'), ('A', '4')]
        second = source.start()
        await _started(first_writer, 6)

        assert _values(first) == [
            (('A', '1'), ('A', '2')),
            (('B', '1'),),
            (('C', '1'),),
        ]
        assert _values(second) == [
            (('A', '3'), ('A', '4')),
            (('B', '2'),),
            (('C', '2'),),
        ]
        # The old timeout handles were canceled and the old tail cannot be
        # delivered again into the shared partition.
        assert len(first_writer.calls) == 6
        old_values = {('A', '1'), ('B', '1'), ('C', '1'), ('A', '2')}
        for values in first_writer.calls:
            if values in old_values:
                first_writer.release(values)
        await _turn()
        assert not second.done()
        assert _values(second) == [
            (('A', '3'), ('A', '4')),
            (('B', '2'),),
            (('C', '2'),),
        ]

        for values in first_writer.calls:
            if values not in old_values:
                first_writer.release(values)
        one = await asyncio.wait_for(first, 1)
        two = await asyncio.wait_for(second, 1)
        assert one.status == 'completed'
        assert two.status == 'completed'
        assert one.round_id != two.round_id
        assert {record.value for record in one.records} == old_values
        assert {record.value for record in two.records} == {
            ('A', '3'), ('B', '2'), ('C', '2'), ('A', '4')}

    _run(main())
