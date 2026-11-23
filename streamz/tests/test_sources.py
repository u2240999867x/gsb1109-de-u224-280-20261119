import asyncio
import json
import sys
import threading
import time

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


def _child_watcher(s):
    if sys.platform != "win32" and sys.version_info < (3, 14):
        # don't know why - something with pytest and new processes
        policy = asyncio.get_event_loop_policy()
        watcher = asyncio.SafeChildWatcher()
        policy.set_child_watcher(watcher)
        watcher.attach_loop(s.loop.asyncio_loop)


def _json_cmd(*statements):
    return [sys.executable, "-c",
            "import json, sys; " + "; ".join(statements)]


def _emit_stmt(obj):
    return "sys.stdout.write(json.dumps(%r) + '\\n'); sys.stdout.flush()" % (obj,)


def test_process_json_lines():
    cmd = _json_cmd(
        _emit_stmt({'seq': 1, 'value': 'a'}),
        _emit_stmt({'seq': 2, 'value': 'b'}),
        "sys.stdout.write('[1, 2]\\n'); sys.stdout.flush()",
        "sys.stdout.write('not json\\n'); sys.stdout.flush()",
        "sys.stdout.write(json.dumps({'seq': 3})[:-4]); sys.stdout.flush()",
        "sys.stderr.write('oops\\n'); sys.stderr.flush()",
    )
    s = Source.from_process(cmd, json_lines=True)
    _child_watcher(s)
    out = s.sink_to_list()
    s.start()
    wait_for(lambda: len(s.runs) == 1 and s.runs[0].done, 10, period=0.01)
    run = s.runs[0]
    assert [r.data for r in out] == [{'seq': 1, 'value': 'a'},
                                     {'seq': 2, 'value': 'b'}]
    assert [r.run_id for r in out] == [1, 1]
    assert run.exit_code == 0
    assert run.returncode == 0
    assert run.reason == 'exited'
    assert run.exited and run.output_done and run.sinks_done and run.done
    assert run.records_emitted == 2
    # the unterminated tail is diagnostics only, never emitted as data
    assert run.partial_line == json.dumps({'seq': 3})[:-4].encode()
    # complete lines that are not JSON objects are diagnostics only
    assert run.discarded_lines == [b'[1, 2]\n', b'not json\n']
    # stderr is kept raw and independently of the data
    assert run.stderr == b'oops\n'
    assert run.stderr_tail == b'oops\n'
    assert not run.stderr_truncated
    # the completion result appears exactly once and is this run
    assert run.completion.result(timeout=1) is run
    s.stop()


def test_process_json_lines_handover():
    # device A: two complete records, an unterminated half record, exit code 7
    cmd_a = _json_cmd(
        _emit_stmt({'seq': 1, 'value': 'old-1'}),
        _emit_stmt({'seq': 2, 'value': 'old-2'}),
        "sys.stdout.write(json.dumps({'seq': 3, 'value': 'x'})[:-5]); "
        "sys.stdout.flush()",
        "sys.exit(7)",
    )
    # device B: more than a pipe buffer of stderr diagnostics, then its
    # first complete record (seq restarts at 1)
    cmd_b = _json_cmd(
        "sys.stderr.write('x' * 200000); sys.stderr.flush()",
        _emit_stmt({'seq': 1, 'value': 'new-1'}),
    )
    s = Source.from_process(cmd_a, json_lines=True)
    _child_watcher(s)
    release = threading.Event()
    entered, completed = [], []

    async def sink_fn(rec):
        entered.append(rec)
        if rec.run_id == 1 and rec.data['seq'] == 1:
            # pause only A's first record
            await asyncio.get_running_loop().run_in_executor(
                None, release.wait)
        completed.append(rec)

    s.sink(sink_fn)
    s.start()
    # A's first record is inside the async sink, which does not return yet
    wait_for(lambda: len(entered) == 1, 5, period=0.01)
    # A exited on its own and is reaped without waiting for the paused sink
    wait_for(lambda: s.runs[0].exit_code == 7, 5, period=0.01)
    run_a = s.runs[0]
    # process exited, but its complete records are not all processed yet:
    # the two states are distinguishable
    assert run_a.exited
    assert not run_a.sinks_done
    assert not run_a.done

    # handover: stop A, swap command, start B; downstream nodes stay put
    s.cmd = cmd_b
    s.stop()
    s.start()
    wait_for(lambda: len(s.runs) == 2, 5, period=0.01)
    run_b = s.runs[1]

    # B's first record reaches the downstream while A's sink is still paused
    wait_for(lambda: any(r.run_id == 2 for r in completed), 10, period=0.01)
    # A's second record has not entered the sink yet
    assert len([r for r in entered if r.run_id == 1]) == 1
    assert not any(r.run_id == 1 and r.data['seq'] == 2 for r in completed)
    # A's late exit handling did not stop the source or hijack B's run
    assert not s.stopped
    assert run_b.exit_code != 7
    assert s.current_run is run_b

    # release the pause: A's two complete records each complete once, in
    # their original order
    release.set()
    wait_for(lambda: run_a.done, 10, period=0.01)
    a_completed = [r for r in completed if r.run_id == 1]
    assert [r.data['seq'] for r in a_completed] == [1, 2]
    assert [r.data['value'] for r in a_completed] == ['old-1', 'old-2']

    # A's run state: natural exit with its own exit code, half record kept
    # as diagnostics only
    assert run_a.exit_code == 7
    assert run_a.reason == 'exited'
    assert run_a.partial_line == json.dumps({'seq': 3, 'value': 'x'})[:-5].encode()
    assert run_a.completion.result(timeout=1) is run_a

    # B's run state is its own: same seq numbers as A, but a different run
    wait_for(lambda: run_b.done, 10, period=0.01)
    b_completed = [r for r in completed if r.run_id == 2]
    assert [r.data for r in b_completed] == [{'seq': 1, 'value': 'new-1'}]
    assert run_b.exit_code == 0
    assert run_b.reason == 'exited'
    # B's large stderr came through completely and independently
    assert run_b.stderr == b'x' * 200000
    assert run_b.stderr_tail == b'x' * 4096
    assert run_b.stderr_truncated
    assert run_b.partial_line == b''
    s.stop()


def test_process_json_lines_stop_ignoring_process_then_restart():
    # device B: no further stdout newline, ignores SIGTERM
    cmd_b = _json_cmd(
        "import signal",
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)",
        _emit_stmt({'seq': 1, 'value': 'b1'}),
        "sys.stdout.write('half'); sys.stdout.flush()",
        "import time; time.sleep(30)",
    )
    cmd_c = _json_cmd(_emit_stmt({'seq': 1, 'value': 'c1'}))
    s = Source.from_process(cmd_b, json_lines=True, stop_timeout=0.2)
    _child_watcher(s)
    out = s.sink_to_list()
    s.start()
    wait_for(lambda: len(out) == 1, 5, period=0.01)

    # stop B and, while its cleanup is still in progress (B ignores the
    # terminate signal), start C
    s.cmd = cmd_c
    s.stop()
    s.start()
    wait_for(lambda: len(s.runs) == 2, 5, period=0.01)
    run_b, run_c = s.runs

    # B's stop still completes: it is forcefully killed and reaped without
    # waiting for another stdout line
    wait_for(lambda: run_b.done, 10, period=0.01)
    assert run_b.reason == 'stopped'
    assert run_b.exit_code is not None and run_b.exit_code < 0
    assert run_b.partial_line == b'half'
    assert run_b.completion.result(timeout=1) is run_b

    # C carries on delivering its own records
    wait_for(lambda: run_c.done, 10, period=0.01)
    assert [r.data for r in out if r.run_id == 2] == [
        {'seq': 1, 'value': 'c1'}]
    assert run_c.exit_code == 0
    assert run_c.reason == 'exited'

    # no leftover children and no old readers re-reading the same output:
    # every record was delivered exactly once
    assert all(run.exited and run.done for run in s.runs)
    assert all(run.process.returncode is not None for run in s.runs)
    assert len([r for r in out if r.run_id == 1]) == 1
    s.stop()


def test_process_legacy_with_stderr():
    cmd = _json_cmd(
        "sys.stdout.write('out\\n'); sys.stdout.flush()",
        "sys.stderr.write('err\\n'); sys.stderr.flush()",
    )
    s = Source.from_process(cmd, with_stderr=True)
    _child_watcher(s)
    out = s.sink_to_list()
    s.start()
    wait_for(lambda: sorted(out) == [b'err\n', b'out\n'], 5, period=0.01)
    s.stop()


def test_process_legacy_without_end():
    cmd = _json_cmd("sys.stdout.write('no-newline'); sys.stdout.flush()")
    s = Source.from_process(cmd, with_end=False)
    _child_watcher(s)
    out = s.sink_to_list()
    s.start()
    time.sleep(1)
    assert out == []
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
