import asyncio
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


# The sandbox these tests run in forbids binding TCP ports. from_http_server
# therefore supports ``bind=False`` plus ``attach(sock)``, which performs the
# exact same connection hand-off as an accepted socket; the pair carries real
# HTTP/1.1 traffic over a socketpair.
def _http_post(sock, body, path='/'):
    request = (
        "POST %s HTTP/1.1\r\nHost: localhost\r\n"
        "Content-Length: %d\r\nContent-Type: application/octet-stream\r\n"
        "Connection: keep-alive\r\n\r\n" % (path, len(body))
    ).encode() + body
    sock.sendall(request)


def _http_recv(sock, timeout=5):
    sock.settimeout(timeout)
    data = b""
    while b"\r\n\r\n" not in data:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data += chunk
    head, _, rest = data.partition(b"\r\n\r\n")
    length = 0
    for line in head.split(b"\r\n"):
        if line.lower().startswith(b"content-length:"):
            length = int(line.split(b":")[1])
    while len(rest) < length:
        chunk = sock.recv(4096)
        if not chunk:
            break
        rest += chunk
    status = head.split(b"\r\n")[0].decode()
    return status, rest[:length]


def _make_pair(source):
    client_sock, server_sock = socket.socketpair()
    source.attach(server_sock)
    time.sleep(0.1)
    return client_sock


def _two_outlet_server():
    source = Source.from_http_server(0, bind=False)
    loop = source.loop
    gate = loop.asyncio_loop.create_future()
    started = []
    finished = []

    async def slow_writer(x):
        started.append(('slow', x))
        await gate
        finished.append(('slow', x))

    async def fast_writer(x):
        started.append(('fast', x))
        finished.append(('fast', x))

    buffered = source.buffer(10)
    buffered.sink(slow_writer)
    buffered.sink(fast_writer)
    return source, loop, gate, started, finished


def test_http_shutdown_drains_inflight_before_closing():
    source, loop, gate, started, finished = _two_outlet_server()
    client_sock = None
    stop_thread = None
    try:
        source.start()
        wait_for(lambda: source.server is not None, 2, period=0.01)

        client_sock = _make_pair(source)
        _http_post(client_sock, b'first')
        wait_for(lambda: ('slow', b'first') in started, 2, period=0.01)
        wait_for(lambda: ('fast', b'first') in finished, 2, period=0.01)

        inflight = list(source._inflight.values())
        assert len(inflight) == 1
        # data is past the buffer but the slow outlet is still pending:
        # a successful enqueue must not read as completion
        assert inflight[0]['ref'].count == 1

        stop_thread = threading.Thread(target=source.stop, kwargs={'timeout': 10})
        stop_thread.start()
        wait_for(lambda: source._shutting_down, 2, period=0.01)
        time.sleep(0.2)
        assert stop_thread.is_alive(), 'stop() returned while a write was pending'

        loop.add_callback(gate.set_result, None)
        status, body = _http_recv(client_sock)
        assert status.startswith('HTTP/1.1 200')
        assert body == b'OK'

        stop_thread.join(5)
        assert not stop_thread.is_alive()
        assert source.stopped and source.server is None
        assert inflight[0]['ref'].count == 0
        assert ('slow', b'first') in finished
    finally:
        if stop_thread and stop_thread.is_alive():
            loop.add_callback(gate.set_result, None)
            stop_thread.join(5)
        if not source.stopped:
            source.stop()
        if client_sock is not None:
            client_sock.close()


def test_http_shutdown_rejects_new_post_and_restarts_clean():
    source, loop, gate, started, finished = _two_outlet_server()
    old_sock = new_sock = None
    stop_thread = None
    try:
        source.start()
        wait_for(lambda: source.server is not None, 2, period=0.01)
        first_generation = source._generation

        old_sock = _make_pair(source)
        other_sock = _make_pair(source)
        _http_post(old_sock, b'first')
        wait_for(lambda: ('slow', b'first') in started, 2, period=0.01)

        stop_thread = threading.Thread(target=source.stop, kwargs={'timeout': 10})
        stop_thread.start()
        wait_for(lambda: source._shutting_down, 2, period=0.01)

        # a POST arriving during shutdown on an already established
        # connection is rejected and never enters the in-flight set
        _http_post(other_sock, b'second')
        status, body = _http_recv(other_sock)
        assert status.startswith('HTTP/1.1 503')
        assert b'shutting down' in body
        assert len(source._inflight) == 1
        other_sock.close()

        loop.add_callback(gate.set_result, None)
        status, _ = _http_recv(old_sock)
        assert status.startswith('HTTP/1.1 200')
        stop_thread.join(5)
        assert not stop_thread.is_alive()
        assert source._inflight == {}

        # re-listen: a new generation that does not inherit old requests
        source.start()
        wait_for(lambda: source.server is not None, 2, period=0.01)
        assert source._generation == first_generation + 1
        assert source._inflight == {}

        new_sock = _make_pair(source)
        _http_post(new_sock, b'third')
        status, body = _http_recv(new_sock)
        assert status.startswith('HTTP/1.1 200')
        assert body == b'OK'
        wait_for(lambda: ('slow', b'third') in finished, 2, period=0.01)
    finally:
        if stop_thread and stop_thread.is_alive():
            loop.add_callback(gate.set_result, None)
            stop_thread.join(5)
        if not source.stopped:
            source.stop()
        for sock in (old_sock, new_sock):
            if sock is not None:
                sock.close()


def test_http_downstream_failure_is_a_verifiable_500():
    source = Source.from_http_server(0, bind=False)
    loop = source.loop
    gate = loop.asyncio_loop.create_future()
    good = []

    async def healthy(x):
        good.append(x)

    async def broken(x):
        await gate
        raise RuntimeError('backend write exploded: %r' % x)

    buffered = source.buffer(10)
    buffered.sink(healthy)
    buffered.sink(broken)

    client_sock = None
    stop_thread = None
    try:
        source.start()
        wait_for(lambda: source.server is not None, 2, period=0.01)
        client_sock = _make_pair(source)

        _http_post(client_sock, b'boom')
        wait_for(lambda: len(source._inflight) == 1, 2, period=0.01)
        inflight = list(source._inflight.values())

        stop_thread = threading.Thread(target=source.stop, kwargs={'timeout': 10})
        stop_thread.start()
        wait_for(lambda: source._shutting_down, 2, period=0.01)
        time.sleep(0.2)
        assert stop_thread.is_alive()

        loop.add_callback(gate.set_result, None)
        status, body = _http_recv(client_sock)
        stop_thread.join(5)

        assert status.startswith('HTTP/1.1 500')
        # the old request gets concrete, checkable evidence of failure
        assert b'downstream write failed' in body
        assert b'backend write exploded' in body
        assert b'boom' in body
        assert not stop_thread.is_alive()
        assert source.stopped
        # zero means every reference was released after the failure,
        # not that the write succeeded
        assert inflight[0]['ref'].count == 0
        assert good == [b'boom']

        # the buffer callback survived, so a restarted instance still drains
        source.start()
        wait_for(lambda: source.server is not None, 2, period=0.01)
        second_sock = _make_pair(source)
        _http_post(second_sock, b'again')
        status2, _ = _http_recv(second_sock)
        assert status2.startswith('HTTP/1.1 500')
        assert b'again' in good
        second_sock.close()
    finally:
        if stop_thread and stop_thread.is_alive():
            loop.add_callback(gate.set_result, None)
            stop_thread.join(5)
        if not source.stopped:
            source.stop()
        if client_sock is not None:
            client_sock.close()


def test_http_shutdown_drains_queued_and_writing_requests():
    source = Source.from_http_server(0, bind=False)
    loop = source.loop
    gates = [loop.asyncio_loop.create_future() for _ in range(2)]
    entered = []

    async def writer(x):
        index = int(x)
        entered.append(index)
        await gates[index]

    source.buffer(10).sink(writer)

    socks = []
    stop_thread = None
    try:
        source.start()
        wait_for(lambda: source.server is not None, 2, period=0.01)
        for body in (b'0', b'1'):
            sock = _make_pair(source)
            _http_post(sock, body)
            socks.append(sock)

        # both requests are accepted and in flight; with one buffer the
        # second item is queued behind the first and has not run yet
        wait_for(lambda: len(source._inflight) == 2, 2, period=0.01)
        assert entered == [0]

        stop_thread = threading.Thread(target=source.stop, kwargs={'timeout': 10})
        stop_thread.start()
        wait_for(lambda: source._shutting_down, 2, period=0.01)
        time.sleep(0.2)
        assert stop_thread.is_alive()

        loop.add_callback(gates[0].set_result, None)
        status0, _ = _http_recv(socks[0])
        assert status0.startswith('HTTP/1.1 200')
        time.sleep(0.2)
        # the queued request has now started and shutdown still waits for it
        wait_for(lambda: entered == [0, 1], 2, period=0.01)
        assert stop_thread.is_alive()

        loop.add_callback(gates[1].set_result, None)
        status1, _ = _http_recv(socks[1])
        assert status1.startswith('HTTP/1.1 200')
        stop_thread.join(5)
        assert not stop_thread.is_alive()
        assert source.stopped and source._inflight == {}
    finally:
        if stop_thread and stop_thread.is_alive():
            for gate in gates:
                loop.add_callback(gate.set_result, None)
            stop_thread.join(5)
        if not source.stopped:
            source.stop()
        for sock in socks:
            sock.close()


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
