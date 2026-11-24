import asyncio
from datetime import timedelta
from functools import partial
import itertools
import json
import operator
from operator import add
import os
from time import sleep
import sys
import threading

import pytest

from tornado.queues import Queue
from tornado.ioloop import IOLoop
from tornado import gen

import streamz as sz

from streamz import RefCounter
from streamz.sources import sink_to_file
from streamz.utils_test import (inc, double, gen_test, tmpfile, captured_logger,   # noqa: F401
        clean, await_for, metadata, wait_for)  # noqa: F401
from distributed.utils_test import loop, loop_in_thread, cleanup   # noqa: F401


def test_basic():
    source = Stream()
    b1 = source.map(inc)
    b2 = source.map(double)

    c = b1.scan(add)

    Lc = c.sink_to_list()
    Lb = b2.sink_to_list()

    for i in range(4):
        source.emit(i)

    assert Lc == [1, 3, 6, 10]
    assert Lb == [0, 2, 4, 6]


def test_no_output():
    source = Stream()
    assert source.emit(1) is None


def test_scan():
    source = Stream()

    def f(acc, i):
        acc = acc + i
        return acc, acc

    L = source.scan(f, returns_state=True).sink_to_list()
    for i in range(3):
        source.emit(i)

    assert L == [0, 1, 3]


def test_kwargs():
    source = Stream()

    def f(acc, x, y=None):
        acc = acc + x + y
        return acc

    L = source.scan(f, y=10).sink_to_list()
    for i in range(3):
        source.emit(i)

    assert L == [0, 11, 23]


def test_filter():
    source = Stream()
    L = source.filter(lambda x: x % 2 == 0).sink_to_list()

    for i in range(10):
        source.emit(i)

    assert L == [0, 2, 4, 6, 8]


def test_filter_args():
    source = Stream()
    L = source.filter(lambda x, n: x % n == 0, 2).sink_to_list()

    for i in range(10):
        source.emit(i)

    assert L == [0, 2, 4, 6, 8]


def test_filter_kwargs():
    source = Stream()
    L = source.filter(lambda x, n=1: x % n == 0, n=2).sink_to_list()

    for i in range(10):
        source.emit(i)

    assert L == [0, 2, 4, 6, 8]


def test_filter_none():
    source = Stream()
    L = source.filter(None).sink_to_list()

    for i in range(10):
        source.emit(i % 3)

    assert L == [1, 2, 1, 2, 1, 2]


def test_map():
    def add(x=0, y=0):
        return x + y

    source = Stream()
    L = source.map(add, y=10).sink_to_list()

    source.emit(1)

    assert L[0] == 11


@gen_test()
def test_map_async_tornado():
    @gen.coroutine
    def add_tor(x=0, y=0):
        return x + y

    async def add_native(x=0, y=0):
        await asyncio.sleep(0.1)
        return x + y

    source = Stream(asynchronous=True)
    L = source.map_async(add_tor, y=1).map_async(add_native, parallelism=2, y=2).buffer(1).sink_to_list()

    start = time()
    yield source.emit(0)
    yield source.emit(1)
    yield source.emit(2)

    def fail_func():
        assert L == [3, 4, 5]

    yield await_for(lambda: L == [3, 4, 5], 1, fail_func=fail_func)
    assert (time() - start) == pytest.approx(0.1, abs=4e-3)


@pytest.mark.asyncio
async def test_map_async_restart():
    async def flake_out(x):
        if x == 2:
            raise RuntimeError("I fail on 2.")
        if x > 4:
            raise RuntimeError("I fail on > 4.")
        return x

    source = Stream.from_iterable(itertools.count())
    mapped = source.map_async(flake_out, stop_on_exception=True)
    results = mapped.sink_to_list()
    source.start()

    await await_for(lambda: results == [0, 1], 1)
    await await_for(lambda: not mapped.work_task, 1)

    source.start()

    await await_for(lambda: results == [0, 1, 3, 4], 1)


@pytest.mark.asyncio
async def test_map_async():
    @gen.coroutine
    def add_tor(x=0, y=0):
        return x + y

    async def add_native(x=0, y=0):
        await asyncio.sleep(0.1)
        return x + y

    source = Stream(asynchronous=True)
    L = source.map_async(add_tor, y=1).map_async(add_native, parallelism=2, y=2).sink_to_list()

    start = time()
    await source.emit(0)
    await source.emit(1)
    await source.emit(2)

    def fail_func():
        assert L == [3, 4, 5]

    await await_for(lambda: L == [3, 4, 5], 1, fail_func=fail_func)
    assert (time() - start) == pytest.approx(0.1, abs=4e-3)


def test_map_args():
    source = Stream()
    L = source.map(operator.add, 10).sink_to_list()
    source.emit(1)
    assert L == [11]


def test_starmap():
    def add(x=0, y=0):
        return x + y

    source = Stream()
    L = source.starmap(add).sink_to_list()

    source.emit((1, 10))

    assert L[0] == 11


def test_remove():
    source = Stream()
    L = source.remove(lambda x: x % 2 == 0).sink_to_list()

    for i in range(10):
        source.emit(i)

    assert L == [1, 3, 5, 7, 9]


def test_partition():
    source = Stream()
    L = source.partition(2).sink_to_list()

    for i in range(10):
        source.emit(i)

    assert L == [(0, 1), (2, 3), (4, 5), (6, 7), (8, 9)]


@pytest.mark.parametrize(
    "n,key,keep,elements,exp_result",
    [
        (3, sz.identity, "first", [1, 2, 1, 3, 1, 3, 3, 2], [(1, 2, 3), (1, 3, 2)]),
        (3, sz.identity, "last", [1, 2, 1, 3, 1, 3, 3, 2], [(2, 1, 3), (1, 3, 2)]),
        (
            3,
            len,
            "last",
            ["f", "fo", "f", "foo", "f", "foo", "foo", "fo"],
            [("fo", "f", "foo"), ("f", "foo", "fo")],
        ),
        (
            2,
            "id",
            "first",
            [{"id": 0, "foo": "bar"}, {"id": 0, "foo": "baz"}, {"id": 1, "foo": "bat"}],
            [({"id": 0, "foo": "bar"}, {"id": 1, "foo": "bat"})],
        ),
        (
            2,
            "id",
            "last",
            [{"id": 0, "foo": "bar"}, {"id": 0, "foo": "baz"}, {"id": 1, "foo": "bat"}],
            [({"id": 0, "foo": "baz"}, {"id": 1, "foo": "bat"})],
        ),
    ]
)
def test_partition_unique(n, key, keep, elements, exp_result):
    source = Stream()
    L = source.partition_unique(n, key, keep).sink_to_list()
    for ele in elements:
        source.emit(ele)

    assert L == exp_result


def test_partition_timeout():
    source = Stream()
    L = source.partition(10, timeout=0.01).sink_to_list()

    for i in range(5):
        source.emit(i)

    sleep(0.1)

    assert L == [(0, 1, 2, 3, 4)]


def test_partition_timeout_cancel():
    source = Stream()
    L = source.partition(3, timeout=0.1).sink_to_list()

    for i in range(3):
        source.emit(i)

    sleep(0.09)
    source.emit(3)
    sleep(0.02)

    assert L == [(0, 1, 2)]

    sleep(0.09)

    assert L == [(0, 1, 2), (3,)]


def test_partition_key():
    source = Stream()
    L = source.partition(2, key=0).sink_to_list()

    for i in range(4):
        source.emit((i % 2, i))

    assert L == [((0, 0), (0, 2)), ((1, 1), (1, 3))]


def test_partition_key_callable():
    source = Stream()
    L = source.partition(2, key=lambda x: x % 2).sink_to_list()

    for i in range(10):
        source.emit(i)

    assert L == [(0, 2), (1, 3), (4, 6), (5, 7)]


def test_partition_size_one():
    source = Stream()

    source.partition(1, timeout=.01).sink(lambda x: None)

    for i in range(10):
        source.emit(i)


def test_sliding_window():
    source = Stream()
    L = source.sliding_window(2).sink_to_list()

    for i in range(10):
        source.emit(i)

    assert L == [(0, ), (0, 1), (1, 2), (2, 3), (3, 4), (4, 5),
                 (5, 6), (6, 7), (7, 8), (8, 9)]

    L = source.sliding_window(2, return_partial=False).sink_to_list()

    for i in range(10):
        source.emit(i)

    assert L == [(0, 1), (1, 2), (2, 3), (3, 4), (4, 5),
                 (5, 6), (6, 7), (7, 8), (8, 9)]


def test_sliding_window_ref_counts():
    source = Stream()
    _ = source.sliding_window(2)

    r_prev = RefCounter()
    source.emit(-2)
    source.emit(-1, metadata=[{'ref': r_prev}])
    for i in range(10):
        r = RefCounter()
        assert r_prev.count == 1
        source.emit(i, metadata=[{'ref': r}])
        assert r_prev.count == 0
        assert r.count == 1
        r_prev = r


def test_sliding_window_metadata():
    source = Stream()
    L = metadata(source.sliding_window(2)).sink_to_list()

    source.emit(0)
    source.emit(1, metadata=[{'v': 1}])
    source.emit(2, metadata=[{'v': 2}])
    source.emit(3, metadata=[{'v': 3}])
    assert L == [
        [{'v': 1}],  # First emit, because 0 has no metadata
        [{'v': 1}, {'v': 2}],  # Second emit
        [{'v': 2}, {'v': 3}]  # Third emit
    ]


@gen_test()
def test_backpressure():
    q = Queue(maxsize=2)

    source = Stream(asynchronous=True)
    source.map(inc).scan(add, start=0).sink(q.put)

    @gen.coroutine
    def read_from_q():
        while True:
            yield q.get()
            yield gen.sleep(0.1)

    IOLoop.current().add_callback(read_from_q)

    start = time()
    for i in range(5):
        yield source.emit(i)
    end = time()

    assert end - start >= 0.2


@gen_test()
def test_timed_window_unique():
    tests = [
        (0.05, sz.identity, "first", [1, 2, 1, 3, 1, 3, 3, 2], [(1, 2, 3)]),
        (0.05, sz.identity, "last", [1, 2, 1, 3, 1, 3, 3, 2], [(1, 3, 2)]),
        (
            0.05,
            len,
            "last",
            ["f", "fo", "f", "foo", "f", "foo", "foo", "fo"],
            [("f", "foo", "fo")],
        ),
        (
            0.05,
            "id",
            "first",
            [{"id": 0, "foo": "bar"}, {"id": 1, "foo": "bat"}, {"id": 0, "foo": "baz"}],
            [({"id": 0, "foo": "bar"}, {"id": 1, "foo": "bat"})],
        ),
        (
            0.05,
            "id",
            "last",
            [{"id": 0, "foo": "bar"}, {"id": 1, "foo": "bat"}, {"id": 0, "foo": "baz"}],
            [({"id": 1, "foo": "bat"}, {"id": 0, "foo": "baz"})],
        ),
    ]
    for interval, key, keep, elements, exp_result in tests:
        source = Stream(asynchronous=True)
        a = source.timed_window_unique(interval, key, keep)

        assert a.loop is IOLoop.current()
        L = a.sink_to_list()

        for ele in elements:
            yield source.emit(ele)
        yield gen.sleep(a.interval)

        assert L
        assert all(wi in elements for window in L for wi in window)
        assert sum(1 for window in L for _ in window) <= len(elements)
        assert L == exp_result

        yield gen.sleep(a.interval)
        assert not L[-1]


@gen_test()
def test_timed_window():
    source = Stream(asynchronous=True)
    a = source.timed_window(0.01)

    assert a.loop is IOLoop.current()
    L = a.sink_to_list()

    for i in range(10):
        yield source.emit(i)
        yield gen.sleep(0.004)

    yield gen.sleep(a.interval)
    assert L
    assert sum(L, []) == list(range(10))
    assert all(len(x) <= 3 for x in L)
    assert any(len(x) >= 2 for x in L)

    yield gen.sleep(0.1)
    assert not L[-1]


@gen_test()
def test_timed_window_ref_counts():
    source = Stream(asynchronous=True)
    _ = source.timed_window(0.01)

    ref1 = RefCounter()
    assert str(ref1) == "<RefCounter count=0>"
    source.emit(1, metadata=[{'ref': ref1}])
    assert ref1.count == 1
    yield gen.sleep(0.05)

    ref2 = RefCounter()
    source.emit(2, metadata=[{'ref': ref2}])
    assert ref1.count == 0
    assert ref2.count == 1


def test_mixed_async():
    s1 = Stream(asynchronous=False)
    with pytest.raises(ValueError):
        Stream(asynchronous=True, upstream=s1)



@gen_test()
def test_timed_window_metadata():
    source = Stream()
    L = metadata(source.timed_window(0.06)).sink_to_list()

    source.emit(0)
    source.emit(1, metadata=[{'v': 1}])
    yield gen.sleep(0.1)
    source.emit(2, metadata=[{'v': 2}])
    source.emit(3, metadata=[{'v': 3}])
    yield gen.sleep(0.1)
    assert L == [
        [{'v': 1}],  # first emit because 0 has no metadata
        [{'v': 2}, {'v': 3}]  # second emit
    ]


@pytest.mark.asyncio
async def test_timed_window_timedelta(clean):  # noqa: F811
    pytest.importorskip('pandas')
    source = Stream(asynchronous=True)
    a = source.timed_window('10ms')
    assert a.interval == 0.010


@gen_test()
def test_timed_window_backpressure():
    q = Queue(maxsize=1)

    source = Stream(asynchronous=True)
    source.timed_window(0.01).sink(q.put)

    @gen.coroutine
    def read_from_q():
        while True:
            yield q.get()
            yield gen.sleep(0.1)

    IOLoop.current().add_callback(read_from_q)

    start = time()
    for i in range(5):
        yield source.emit(i)
        yield gen.sleep(0.01)
    stop = time()

    assert stop - start > 0.2


def test_sink_to_file():
    with tmpfile() as fn:
        source = Stream()
        with sink_to_file(fn, source) as f:
            source.emit('a')
            source.emit('b')

        with open(fn) as f:
            data = f.read()

        assert data == 'a\nb\n'


@gen_test()
def test_counter():
    counter = itertools.count()
    source = Stream.from_periodic(lambda: next(counter), 0.001, asynchronous=True,
                                  start=True)
    L = source.sink_to_list()
    yield gen.sleep(0.05)

    assert L


@gen_test()
def test_rate_limit():
    source = Stream(asynchronous=True)
    L = source.rate_limit(0.05).sink_to_list()

    start = time()
    for i in range(5):
        yield source.emit(i)
    stop = time()
    assert stop - start > 0.2
    assert len(L) == 5


@gen_test()
def test_delay():
    source = Stream(asynchronous=True)
    L = source.delay(0.02).sink_to_list()

    for i in range(5):
        yield source.emit(i)

    assert not L

    yield gen.sleep(0.04)

    assert len(L) < 5

    yield gen.sleep(0.1)

    assert len(L) == 5


@gen_test()
def test_delay_ref_counts():
    source = Stream(asynchronous=True)
    _ = source.delay(0.01)

    refs = []
    for i in range(5):
        r = RefCounter()
        refs.append(r)
        source.emit(i, metadata=[{'ref': r}])

    assert all(r.count == 1 for r in refs)
    yield gen.sleep(0.05)
    assert all(r.count == 0 for r in refs)


@gen_test()
def test_buffer():
    source = Stream(asynchronous=True)
    L = source.map(inc).buffer(10).map(inc).rate_limit(0.05).sink_to_list()

    start = time()
    for i in range(10):
        yield source.emit(i)
    stop = time()

    assert stop - start < 0.01
    assert not L

    start = time()
    for i in range(5):
        yield source.emit(i)
    stop = time()

    assert L
    assert stop - start > 0.04


@gen_test()
def test_buffer_ref_counts():
    source = Stream(asynchronous=True)
    _ = source.buffer(5)

    refs = []
    for i in range(5):
        r = RefCounter()
        refs.append(r)
        source.emit(i, metadata=[{'ref': r}])

    assert all(r.count == 1 for r in refs)
    yield gen.sleep(0.05)
    assert all(r.count == 0 for r in refs)


def test_zip():
    a = Stream()
    b = Stream()
    c = sz.zip(a, b)

    L = c.sink_to_list()

    a.emit(1)
    b.emit('a')
    a.emit(2)
    b.emit('b')

    assert L == [(1, 'a'), (2, 'b')]
    d = Stream()
    # test zip from the object itself
    # zip 3 streams together
    e = a.zip(b, d)
    L2 = e.sink_to_list()

    a.emit(1)
    b.emit(2)
    d.emit(3)
    assert L2 == [(1, 2, 3)]


def test_zip_literals():
    a = Stream()
    b = Stream()
    c = sz.zip(a, 123, b)

    L = c.sink_to_list()
    a.emit(1)
    b.emit(2)

    assert L == [(1, 123, 2)]

    a.emit(4)
    b.emit(5)

    assert L == [(1, 123, 2),
                 (4, 123, 5)]


def test_zip_same():
    a = Stream()
    b = a.zip(a)
    L = b.sink_to_list()

    a.emit(1)
    a.emit(2)
    assert L == [(1, 1), (2, 2)]


def test_combine_latest():
    a = Stream()
    b = Stream()
    c = a.combine_latest(b)
    d = a.combine_latest(b, emit_on=[a, b])

    L = c.sink_to_list()
    L2 = d.sink_to_list()

    a.emit(1)
    a.emit(2)
    b.emit('a')
    a.emit(3)
    b.emit('b')

    assert L == [(2, 'a'), (3, 'a'), (3, 'b')]
    assert L2 == [(2, 'a'), (3, 'a'), (3, 'b')]


def test_combine_latest_emit_on():
    a = Stream()
    b = Stream()
    c = a.combine_latest(b, emit_on=a)

    L = c.sink_to_list()

    a.emit(1)
    b.emit('a')
    a.emit(2)
    a.emit(3)
    b.emit('b')
    a.emit(4)

    assert L == [(2, 'a'), (3, 'a'), (4, 'b')]


def test_combine_latest_emit_on_stream():
    a = Stream()
    b = Stream()
    c = a.combine_latest(b, emit_on=0)

    L = c.sink_to_list()

    a.emit(1)
    b.emit('a')
    a.emit(2)
    a.emit(3)
    b.emit('b')
    a.emit(4)

    assert L == [(2, 'a'), (3, 'a'), (4, 'b')]


def test_combine_latest_ref_counts():
    a = Stream()
    b = Stream()
    _ = a.combine_latest(b)

    ref1 = RefCounter()
    a.emit(1, metadata=[{'ref': ref1}])
    assert ref1.count == 1

    # The new value kicks out the old value
    ref2 = RefCounter()
    a.emit(2, metadata=[{'ref': ref2}])
    assert ref1.count == 0
    assert ref2.count == 1

    # The value on stream a is still retained and the value on stream b is new
    ref3 = RefCounter()
    b.emit(3, metadata=[{'ref': ref3}])
    assert ref2.count == 1
    assert ref3.count == 1


def test_combine_latest_metadata():
    a = Stream()
    b = Stream()
    L = metadata(a.combine_latest(b)).sink_to_list()

    a.emit(1, metadata=[{'v': 1}])
    b.emit(2, metadata=[{'v': 2}])
    b.emit(3)
    b.emit(4, metadata=[{'v': 4}])
    assert L == [
        [{'v': 1}, {'v': 2}],  # first emit when 2 is introduced
        [{'v': 1}],  # 3 has no metadata but it replaces the value on 'b'
        [{'v': 1}, {'v': 4}]  # 4 replaces the value without metadata on 'b'
    ]


@gen_test()
def test_zip_timeout():
    a = Stream(asynchronous=True)
    b = Stream(asynchronous=True)
    c = sz.zip(a, b, maxsize=2)

    L = c.sink_to_list()

    a.emit(1)
    a.emit(2)

    future = a.emit(3)
    with pytest.raises(gen.TimeoutError):
        yield gen.with_timeout(timedelta(seconds=0.01), future)

    b.emit('a')
    yield future

    assert L == [(1, 'a')]


def test_zip_ref_counts():
    a = Stream()
    b = Stream()
    _ = a.zip(b)

    # The first value in a becomes buffered
    ref1 = RefCounter()
    a.emit(1, metadata=[{'ref': ref1}])
    assert ref1.count == 1

    # The second value in a also becomes buffered
    ref2 = RefCounter()
    a.emit(2, metadata=[{'ref': ref2}])
    assert ref1.count == 1
    assert ref2.count == 1

    # All emitted values are removed from the buffer
    ref3 = RefCounter()
    b.emit(3, metadata=[{'ref': ref3}])
    assert ref1.count == 0
    assert ref2.count == 1  # still in the buffer
    assert ref3.count == 0


def test_zip_metadata():
    a = Stream()
    b = Stream()
    L = metadata(a.zip(b)).sink_to_list()

    a.emit(1, metadata=[{'v': 1}])
    b.emit(2, metadata=[{'v': 2}])
    a.emit(3)
    b.emit(4, metadata=[{'v': 4}])
    assert L == [
        [{'v': 1}, {'v': 2}],  # first emit when 2 is introduced
        [{'v': 4}]  # second emit when 4 is introduced, and 3 has no metadata
    ]


def test_frequencies():
    source = Stream()
    L = source.frequencies().sink_to_list()

    source.emit('a')
    source.emit('b')
    source.emit('a')

    assert L[-1] == {'a': 2, 'b': 1}


@pytest.mark.parametrize("iterators",
                         [[[1, 2, 3], [4, 5], [6, 7, 8]],
                          [(i for i in range(1, 7)), (i for i in range(7, 9))]])
def test_flatten(iterators):
    source = Stream()
    L = source.flatten().sink_to_list()

    for iterator in iterators:
        source.emit(iterator)

    assert L == [1, 2, 3, 4, 5, 6, 7, 8]


def test_flatten_empty():
    source = Stream()
    L = source.flatten().sink_to_list()

    source.emit([1, 2])
    source.emit([])
    source.emit([3, 4])

    assert L == [1, 2, 3, 4]


def test_unique():
    source = Stream()
    L = source.unique().sink_to_list()

    source.emit(1)
    source.emit(2)
    source.emit(1)

    assert L == [1, 2]


def test_unique_key():
    source = Stream()
    L = source.unique(key=lambda x: x % 2, maxsize=1).sink_to_list()

    source.emit(1)
    source.emit(2)
    source.emit(4)
    source.emit(6)
    source.emit(3)

    assert L == [1, 2, 3]


def test_unique_metadata():
    source = Stream()
    L = metadata(source.unique()).flatten().sink_to_list()
    for i in range(5):
        source.emit(i, metadata=[{'v': i}])

    assert L == [{'v': i} for i in range(5)]


def test_unique_history():
    source = Stream()
    s = source.unique(maxsize=2)
    s2 = source.unique(maxsize=2, hashable=False)
    L = s.sink_to_list()
    L2 = s2.sink_to_list()

    source.emit(1)
    source.emit(2)
    source.emit(1)
    source.emit(2)
    source.emit(1)
    source.emit(2)

    assert L == [1, 2]
    assert L == L2

    source.emit(3)
    source.emit(2)

    assert L == [1, 2, 3]
    assert L == L2

    source.emit(1)

    assert L == [1, 2, 3, 1]
    assert L == L2

    # update 2 position
    source.emit(2)
    # knock out 1
    source.emit(3)
    # update 2 position
    source.emit(2)

    assert L == [1, 2, 3, 1, 3]
    assert L == L2


def test_unique_history_dict():
    source = Stream()
    s = source.unique(maxsize=2, hashable=False)
    L = s.sink_to_list()

    a = {'hi': 'world'}
    b = {'hi': 'bar'}
    c = {'foo': 'bar'}

    source.emit(a)
    source.emit(b)
    source.emit(a)
    source.emit(b)
    source.emit(a)
    source.emit(b)

    assert L == [a, b]

    source.emit(c)
    source.emit(b)

    assert L == [a, b, c]

    source.emit(a)

    assert L == [a, b, c, a]


def test_union():
    a = Stream()
    b = Stream()
    c = Stream()

    L = a.union(b, c).sink_to_list()

    a.emit(1)
    assert L == [1]
    b.emit(2)
    assert L == [1, 2]
    a.emit(3)
    assert L == [1, 2, 3]
    c.emit(4)
    assert L == [1, 2, 3, 4]


def test_pluck():
    a = Stream()
    L = a.pluck(1).sink_to_list()
    a.emit([1, 2, 3])
    assert L == [2]
    a.emit([4, 5, 6, 7, 8, 9])
    assert L == [2, 5]
    with pytest.raises(IndexError):
        a.emit([1])


def test_pluck_list():
    a = Stream()
    L = a.pluck([0, 2]).sink_to_list()

    a.emit([1, 2, 3])
    assert L == [(1, 3)]
    a.emit([4, 5, 6, 7, 8, 9])
    assert L == [(1, 3), (4, 6)]
    with pytest.raises(IndexError):
        a.emit([1])


def test_collect():
    source1 = Stream()
    source2 = Stream()
    collector = source1.collect()
    L = collector.sink_to_list()
    source2.sink(collector.flush)

    source1.emit(1)
    source1.emit(2)
    assert L == []

    source2.emit('anything')  # flushes collector
    assert L == [(1, 2)]

    source2.emit('anything')
    assert L == [(1, 2), ()]

    source1.emit(3)
    assert L == [(1, 2), ()]

    source2.emit('anything')
    assert L == [(1, 2), (), (3,)]


@pytest.mark.parametrize("triggered", [False, True])
def test_collect_async_sink(triggered):
    source = Stream(asynchronous=False)
    trigger = Stream(asynchronous=False)
    collector = source.collect()
    received = []

    async def write(value):
        await asyncio.sleep(0)
        received.append(value)

    collector.sink(write)
    trigger.sink(collector.flush)
    source.emit(1)
    source.emit(2)
    if triggered:
        trigger.emit(None)
    else:
        collector.flush()
    assert received == [(1, 2)]


def test_collect_async_sink_can_collect_more():
    source = Stream(asynchronous=False)
    collector = source.collect()
    received = []

    async def write(value):
        await asyncio.sleep(0)
        received.append(value)
        if value == (1,):
            await source.emit(2)

    collector.sink(write)
    source.emit(1)
    collector.flush()
    collector.flush()
    assert received == [(1,), (2,)]


def test_collect_await_flush():
    async def run():
        source = Stream(asynchronous=True)
        collector = source.collect()
        received = []

        async def write(value):
            await asyncio.sleep(0)
            received.append(value)

        collector.sink(write)
        await source.emit(1)
        await collector.flush()
        assert received == [(1,)]
        await collector.flush()
        assert received == [(1,), ()]

    asyncio.run(run())


def test_collect_ref_counts():
    source = Stream()
    collector = source.collect()

    refs = []
    for i in range(10):
        r = RefCounter()
        refs.append(r)
        source.emit(i, metadata=[{'ref': r}])

    assert all(r.count == 1 for r in refs)

    collector.flush()
    assert all(r.count == 0 for r in refs)


def test_collect_metadata():
    source = Stream()
    collector = source.collect()
    L = metadata(collector).sink_to_list()

    source.emit(0)
    source.emit(1, metadata=[{'v': 1}])
    source.emit(2, metadata=[{'v': 2}])
    collector.flush()
    source.emit(3, metadata=[{'v': 3}])
    source.emit(4, metadata=[{'v': 4}])
    collector.flush()
    assert L == [
        [{'v': 1}, {'v': 2}],  # Flush 0-2, but 0 has no metadata
        [{'v': 3}, {'v': 4}]   # Flush the rest
    ]


def test_map_str():
    def add(x=0, y=0):
        return x + y

    source = Stream()
    s = source.map(add, y=10)
    assert str(s) == '<map: add>'


def test_no_ipywidget_repr(monkeypatch, capsys):
    pytest.importorskip("ipywidgets")
    import ipywidgets
    source = Stream()

    # works by side-affect of display()
    source._ipython_display_()
    assert "Output()" in capsys.readouterr().out

    def get(*_, **__):
        raise ImportError
    monkeypatch.setattr(ipywidgets.Output, "__init__", get)

    out = source._ipython_display_()
    assert "Stream" in capsys.readouterr().out




def test_filter_str():
    def iseven(x):
        return x % 2 == 0

    source = Stream()
    s = source.filter(iseven)
    assert str(s) == '<filter: iseven>'


def test_timed_window_str(clean):  # noqa: F811
    source = Stream()
    s = source.timed_window(.05)
    assert str(s) == '<timed_window: 0.05>'


def test_partition_str():
    source = Stream()
    s = source.partition(2)
    assert str(s) == '<partition: 2>'


def test_partition_ref_counts():
    source = Stream()
    _ = source.partition(2)

    for i in range(10):
        r = RefCounter()
        source.emit(i, metadata=[{'ref': r}])
        if i % 2 == 0:
            assert r.count == 1
        else:
            assert r.count == 0


def test_partition_metadata():
    source = Stream()
    L = metadata(source.partition(2)).sink_to_list()

    source.emit(0)
    source.emit(1, metadata=[{'v': 1}])
    source.emit(2, metadata=[{'v': 2}])
    source.emit(3, metadata=[{'v': 3}])
    assert L == [
        [{'v': 1}],  # first emit when 1 is introduced. 0 has no metadata
        [{'v': 2}, {'v': 3}]  # second emit
    ]


def test_stream_name_str():
    source = Stream(stream_name='this is not a stream')
    assert str(source) == '<this is not a stream; Stream>'


def test_zip_latest():
    a = Stream()
    b = Stream()
    c = a.zip_latest(b)
    d = a.combine_latest(b, emit_on=a)

    L = c.sink_to_list()
    L2 = d.sink_to_list()

    a.emit(1)
    a.emit(2)
    b.emit('a')
    b.emit('b')
    a.emit(3)

    assert L == [(1, 'a'), (2, 'a'), (3, 'b')]
    assert L2 == [(3, 'b')]


def test_zip_latest_reverse():
    a = Stream()
    b = Stream()
    c = a.zip_latest(b)

    L = c.sink_to_list()

    b.emit('a')
    a.emit(1)
    a.emit(2)
    a.emit(3)
    b.emit('b')
    a.emit(4)

    assert L == [(1, 'a'), (2, 'a'), (3, 'a'), (4, 'b')]


def test_triple_zip_latest():
    from streamz.core import Stream
    s1 = Stream()
    s2 = Stream()
    s3 = Stream()
    s_simple = s1.zip_latest(s2, s3)
    L_simple = s_simple.sink_to_list()

    s1.emit(1)
    s2.emit('I')
    s2.emit("II")
    s1.emit(2)
    s2.emit("III")
    s3.emit('a')
    s3.emit('b')
    s1.emit(3)
    assert L_simple == [(1, 'III', 'a'), (2, 'III', 'a'), (3, 'III', 'b')]


def test_zip_latest_ref_counts():
    a = Stream()
    b = Stream()
    _ = a.zip_latest(b)

    ref1 = RefCounter()
    a.emit(1, metadata=[{'ref': ref1}])
    assert ref1.count == 1  # Retained until stream b has a value

    # The lossless stream is never retained if all upstreams have a value
    ref2 = RefCounter()
    b.emit(2, metadata=[{'ref': ref2}])
    assert ref1.count == 0
    assert ref2.count == 1

    # Kick out the stream b value and verify it has zero references
    ref3 = RefCounter()
    b.emit(3, metadata=[{'ref': ref3}])
    assert ref2.count == 0
    assert ref3.count == 1

    # Verify the lossless value is not retained, but the lossy value is
    ref4 = RefCounter()
    a.emit(3, metadata=[{'ref': ref4}])
    assert ref3.count == 1
    assert ref4.count == 0


def test_zip_latest_metadata():
    a = Stream()
    b = Stream()
    L = metadata(a.zip_latest(b)).sink_to_list()

    a.emit(1, metadata=[{'v': 1}])
    b.emit(2, metadata=[{'v': 2}])
    a.emit(3)
    b.emit(4, metadata=[{'v': 4}])
    assert L == [
        [{'v': 1}, {'v': 2}],  # the first emit when 2 is introduced
        [{'v': 2}]  # 3 has no metadata
    ]


def test_connect():
    source_downstream = Stream()
    # connect assumes this default behaviour
    # of stream initialization
    assert not source_downstream.downstreams
    assert source_downstream.upstreams == []

    # initialize the second stream to connect to
    source_upstream = Stream()

    sout = source_downstream.map(lambda x : x + 1)
    L = list()
    sout = sout.map(L.append)
    source_upstream.connect(source_downstream)

    source_upstream.emit(2)
    source_upstream.emit(4)

    assert L == [3, 5]


def test_multi_connect():
    source0 = Stream()
    source1 = Stream()
    source_downstream = source0.union(source1)
    # connect assumes this default behaviour
    # of stream initialization
    assert not source_downstream.downstreams

    # initialize the second stream to connect to
    source_upstream = Stream()

    sout = source_downstream.map(lambda x : x + 1)
    L = list()
    sout = sout.map(L.append)
    source_upstream.connect(source_downstream)

    source_upstream.emit(2)
    source_upstream.emit(4)

    assert L == [3, 5]


def test_disconnect():
    source = Stream()

    upstream = Stream()
    L = upstream.sink_to_list()

    source.emit(1)
    assert L == []
    source.connect(upstream)
    source.emit(2)
    source.emit(3)
    assert L == [2, 3]
    source.disconnect(upstream)
    source.emit(4)
    assert L == [2, 3]


def test_gc():
    source = Stream()

    L = []
    a = source.map(L.append)

    source.emit(1)
    assert L == [1]

    del a
    import gc; gc.collect()
    start = time()
    while source.downstreams:
        sleep(0.01)
        assert time() < start + 1

    source.emit(2)
    assert L == [1]


@gen_test()
def test_from_file():
    with tmpfile() as fn:
        with open(fn, 'wt') as f:
            f.write('{"x": 1, "y": 2}\n')
            f.write('{"x": 2, "y": 2}\n')
            f.write('{"x": 3, "y": 2}\n')
            f.flush()

            source = Stream.from_textfile(fn, poll_interval=0.010,
                                          asynchronous=True, start=False)
            L = source.map(json.loads).pluck('x').sink_to_list()

            assert L == []

            source.start()

            yield await_for(lambda: len(L) == 3, timeout=5)

            assert L == [1, 2, 3]

            f.write('{"x": 4, "y": 2}\n')
            f.write('{"x": 5, "y": 2}\n')
            f.flush()

            start = time()
            while L != [1, 2, 3, 4, 5]:
                yield gen.sleep(0.01)
                assert time() < start + 2  # reads within 2s


@gen_test()
def test_from_file_end():
    with tmpfile() as fn:
        with open(fn, 'wt') as f:
            f.write('data1\n')
            f.flush()

            source = Stream.from_textfile(fn, poll_interval=0.010,
                                          start=False, from_end=True)
            out = source.sink_to_list()
            source.start()
            assert out == []
            yield await_for(lambda: source.started, 2, period=0.02)

            f.write('data2\n')
            f.flush()
            yield await_for(lambda: out == ['data2\n'], timeout=5, period=0.1)


@gen_test()
def test_filenames():
    with tmpfile() as fn:
        os.mkdir(fn)
        with open(os.path.join(fn, 'a'), 'w'):
            pass
        with open(os.path.join(fn, 'b'), 'w'):
            pass

        source = Stream.filenames(fn, asynchronous=True)
        L = source.sink_to_list()
        source.start()

        while len(L) < 2:
            yield gen.sleep(0.01)

        assert L == [os.path.join(fn, x) for x in ['a', 'b']]

        with open(os.path.join(fn, 'c'), 'w'):
            pass

        while len(L) < 3:
            yield gen.sleep(0.01)

        assert L == [os.path.join(fn, x) for x in ['a', 'b', 'c']]


def test_docstrings():
    for s in [Stream, Stream()]:
        assert 'every element' in s.map.__doc__
        assert s.map.__name__ == 'map'
        assert 'predicate' in s.filter.__doc__
        assert s.filter.__name__ == 'filter'


def test_subclass():
    class NewStream(Stream):
        pass

    @NewStream.register_api()
    class foo(NewStream):
        pass

    assert hasattr(NewStream, 'map')
    assert hasattr(NewStream(), 'map')
    assert hasattr(NewStream, 'foo')
    assert hasattr(NewStream(), 'foo')
    assert not hasattr(Stream, 'foo')
    assert not hasattr(Stream(), 'foo')


@gen_test()
def test_latest():
    source = Stream(asynchronous=True)

    L = []

    @gen.coroutine
    def slow_write(x):
        yield gen.sleep(0.050)
        L.append(x)

    s = source.map(inc).latest().map(slow_write)  # noqa: F841

    source.emit(1)
    yield gen.sleep(0.010)
    source.emit(2)
    source.emit(3)

    start = time()
    while len(L) < 2:
        yield gen.sleep(0.01)
        assert time() < start + 3
    assert L == [2, 4]

    yield gen.sleep(0.060)
    assert L == [2, 4]


def test_latest_ref_counts():
    source = Stream()
    _ = source.latest()

    ref1 = RefCounter()
    source.emit(1, metadata=[{'ref': ref1}])
    assert ref1.count == 1

    ref2 = RefCounter()
    source.emit(2, metadata=[{'ref': ref2}])
    assert ref1.count == 0
    assert ref2.count == 1


def test_destroy():
    source = Stream()
    s = source.map(inc)
    L = s.sink_to_list()

    source.emit(1)
    assert L == [2]

    s.destroy()

    assert not list(source.downstreams)
    assert not s.upstreams
    source.emit(2)
    assert L == [2]


def dont_test_stream_kwargs(clean):  # noqa: F811
    ''' Test the good and bad kwargs for the stream
        Currently just stream_name
    '''
    test_name = "some test name"

    sin = Stream(stream_name=test_name)
    sin2 = Stream()

    assert sin.name == test_name
    # when not defined, should be None
    assert sin2.name is None

    # add new core methods here, initialized
    # these should be functions, use partial to partially initialize them
    # (if they require more arguments)
    streams = [
               # some filter kwargs, so we comment them out
               partial(sin.map, lambda x : x),
               partial(sin.accumulate, lambda x1, x2 : x1),
               partial(sin.filter, lambda x : True),
               partial(sin.partition, 2),
               partial(sin.sliding_window, 2),
               partial(sin.timed_window, .01),
               partial(sin.rate_limit, .01),
               partial(sin.delay, .02),
               partial(sin.buffer, 2),
               partial(sin.zip, sin2),
               partial(sin.combine_latest, sin2),
               sin.frequencies,
               sin.flatten,
               sin.unique,
               sin.union,
               partial(sin.pluck, 0),
               sin.collect,
              ]

    good_kwargs = dict(stream_name=test_name)
    bad_kwargs = dict(foo="bar")
    for s in streams:
        # try good kwargs
        sout = s(**good_kwargs)
        assert sout.name == test_name
        del sout

        with pytest.raises(TypeError):
            sout = s(**bad_kwargs)
            sin.emit(1)
            # need a second emit for accumulate
            sin.emit(1)
            del sout

    # verify that sout is properly deleted each time by emitting once into sin
    # and not getting TypeError
    # garbage collect and then try
    import gc
    gc.collect()
    sin.emit(1)


def test_percolate_loop_information(clean):  # noqa: F811
    source = Stream()
    assert not source.loop
    s = source.timed_window(0.5)
    assert source.loop is s.loop


def test_separate_thread_with_time(clean):  # noqa: F811
    L = []

    @gen.coroutine
    def slow_write(x):
        yield gen.sleep(0.1)
        L.append(x)

    source = Stream(asynchronous=False)
    source.map(inc).sink(slow_write)

    start = time()
    source.emit(1)
    stop = time()

    assert stop - start > 0.1
    assert L == [2]


def test_execution_order():
    L = []
    for i in range(5):
        s = Stream()
        b = s.pluck(1)
        a = s.pluck(0)
        li = a.combine_latest(b, emit_on=a).sink_to_list()
        z = [(1, 'red'), (2, 'blue'), (3, 'green')]
        for zz in z:
            s.emit(zz)
        L.append((li, ))
    for ll in L:
        assert ll == L[0]

    L2 = []
    for i in range(5):
        s = Stream()
        a = s.pluck(0)
        b = s.pluck(1)
        li = a.combine_latest(b, emit_on=a).sink_to_list()
        z = [(1, 'red'), (2, 'blue'), (3, 'green')]
        for zz in z:
            s.emit(zz)
        L2.append((li,))
    for ll, ll2 in zip(L, L2):
        assert ll2 == L2[0]
        assert ll != ll2


@gen_test()
def test_map_errors_log():
    a = Stream(asynchronous=True)
    b = a.delay(0.001).map(lambda x: 1 / x)  # noqa: F841
    with captured_logger('streamz') as logger:
        a._emit(0)
        yield gen.sleep(0.1)

        out = logger.getvalue()
        assert 'ZeroDivisionError' in out


def test_map_errors_raises():
    a = Stream()
    b = a.map(lambda x: 1 / x)  # noqa: F841
    with pytest.raises(ZeroDivisionError):
        a.emit(0)


@gen_test()
def test_accumulate_errors_log():
    a = Stream(asynchronous=True)
    b = a.delay(0.001).accumulate(lambda x, y: x / y, with_state=True)  # noqa: F841
    with captured_logger('streamz') as logger:
        a._emit(1)
        a._emit(0)
        yield gen.sleep(0.1)

        out = logger.getvalue()
        assert 'ZeroDivisionError' in out


def test_accumulate_errors_raises():
    a = Stream()
    b = a.accumulate(lambda x, y: x / y, with_state=True)  # noqa: F841
    with pytest.raises(ZeroDivisionError):
        a.emit(1)
        a.emit(0)


@gen_test()
def test_sync_in_event_loop():
    a = Stream()
    assert not a.asynchronous
    L = a.timed_window(0.01).sink_to_list()
    sleep(0.05)
    assert L
    assert a.loop
    assert a.loop is not IOLoop.current()


def test_share_common_ioloop(clean):  # noqa: F811
    a = Stream()
    b = Stream()
    aa = a.timed_window(0.01)
    bb = b.timed_window(0.01)
    assert aa.loop is bb.loop


@pytest.mark.parametrize('data', [
    [[], [0, 1, 2, 3, 4, 5]],
    [[None, None, None], [0, 1, 2, 3, 4, 5]],
    [[1, None, None], [1, 2, 3, 4, 5]],
    [[None, 4, None], [0, 1, 2, 3]],
    [[None, 4, 2], [0, 2]],
    [[3, 1, None], []],
    [[1, None, 2], [1, 3, 5]],
    [[None, 0, None], []],

])
def test_slice(data):
    pars, expected = data
    a = Stream()
    b = a.slice(*pars)
    out = b.sink_to_list()
    for i in range(6):
        a.emit(i)
    assert out == expected


def test_slice_err():
    a = Stream()
    with pytest.raises(ValueError):
        a.slice(end=-1)


def test_start():
    flag = []

    class MySource(Stream):
        def start(self):
            flag.append(True)

    s = MySource().map(inc)
    s.start()
    assert flag == [True]


def test_connect_zip():
    a = Stream()
    b = Stream()
    c = Stream()
    x = a.zip(b)
    L = x.sink_to_list()
    c.connect(x)
    a.emit(1)
    b.emit(1)
    assert not L
    c.emit(1)
    assert L == [(1, 1, 1)]


def test_disconnect_zip():
    a = Stream()
    b = Stream()
    c = Stream()
    x = a.zip(b, c)
    L = x.sink_to_list()
    b.disconnect(x)
    a.emit(1)
    b.emit(1)
    assert not L
    c.emit(1)
    assert L == [(1, 1)]


def test_connect_combine_latest():
    a = Stream()
    b = Stream()
    c = Stream()
    x = a.combine_latest(b, emit_on=a)
    L = x.sink_to_list()
    c.connect(x)
    b.emit(1)
    c.emit(1)
    a.emit(1)
    assert L == [(1, 1, 1)]


def test_connect_discombine_latest():
    a = Stream()
    b = Stream()
    c = Stream()
    x = a.combine_latest(b, c, emit_on=a)
    L = x.sink_to_list()
    c.disconnect(x)
    b.emit(1)
    c.emit(1)
    a.emit(1)
    assert L == [(1, 1)]


if sys.version_info >= (3, 5):
    from streamz.tests.py3_test_core import *  # noqa


def test_buffer_after_partition():
    Stream().partition(1).buffer(1)


def test_buffer_after_timed_window():
    Stream().timed_window(1).buffer(1)


def test_buffer_after_sliding_window():
    Stream().sliding_window(1).buffer(1)


async def _wait_for_status(buffer, predicate, timeout=5):
    deadline = time() + timeout
    while True:
        status = buffer.status()
        if predicate(status):
            return status
        if time() > deadline:
            raise AssertionError("status condition not reached: %r" % (status,))
        await asyncio.sleep(0)


class _Gate:
    def __init__(self):
        self.started = []
        self.release = {}

    def outlet(self, name):
        async def write(x):
            self.started.append((name, x))
            event = asyncio.Event()
            self.release[(name, x)] = event
            await event.wait()
            self.started.append((name, 'done', x))
        return write

    def open(self, name, x):
        self.release[(name, x)].set()


async def _three_submissions(buffer_source):
    source, buffer = buffer_source
    tasks = [asyncio.ensure_future(source.emit(value)) for value in (1, 2, 3)]
    await asyncio.sleep(0)
    return tasks


@gen_test(timeout=15)
def test_buffer_status_identity_for_equal_payloads():
    source = Stream(asynchronous=True)
    buff = source.buffer(1)
    gate = _Gate()

    async def write(x):
        gate.started.append(x)
        await asyncio.sleep(0.05)

    buff.sink(write)

    # identical business payloads
    first = yield buff.submit('same', metadata=[])
    second_future = buff.submit('same', metadata=[])
    third_future = buff.submit('same', metadata=[])

    yield asyncio.sleep(0)
    status = buff.status()
    ids = [status['current']['submission_id'],
           status['queue'][0]['submission_id'],
           status['waiting_submissions'][0]['submission_id']]
    assert len(set(ids)) == 3
    assert first['submission_id'] == ids[0]
    assert second_future.submission_id == ids[1]
    assert third_future.submission_id == ids[2]
    assert status['current']['value'] == 'same'
    assert status['queue'][0]['value'] == 'same'
    assert status['waiting_submissions'][0]['value'] == 'same'

    yield second_future
    yield third_future
    yield gen.sleep(0.2)
    status = buff.status()
    assert status['current'] is None
    assert status['last_delivery']['submission_id'] == ids[2]
    assert status['last_delivery']['written'] is True
    assert buff.submission_status(ids[2])['state'] == 'succeeded'
    assert buff.submission_status(9999) is None


@gen_test(timeout=15)
def test_buffer_status_submission_path():
    source = Stream(asynchronous=True)
    buff = source.buffer(1)
    seen = []

    async def write(x):
        seen.append(x)
        await asyncio.sleep(0.02)

    buff.sink(write)
    future = buff.submit('payload')
    submission_id = future.submission_id
    assert buff.submission_status(submission_id)['state'] == 'queued'

    receipt = yield future
    assert receipt['submission_id'] == submission_id
    assert receipt['enqueued'] is True
    assert receipt['written'] is False

    record = yield _wait_for_status(
        buff, lambda s: s['last_delivery'] is not None)
    assert record['last_delivery']['submission_id'] == submission_id
    assert record['last_delivery']['state'] == 'succeeded'
    assert buff.submission_status(submission_id)['state'] == 'succeeded'
    assert seen == ['payload']


@gen_test(timeout=15)
def test_buffer_status_failure_after_successful_enqueue():
    source = Stream(asynchronous=True)
    buff = source.buffer(1)
    gate = _Gate()

    async def fail(x):
        gate.started.append(('fail', x))
        raise RuntimeError('write failed')

    buff.sink(fail)

    receipts = []
    tasks = []
    for value in ('a', 'b', 'c'):
        future = asyncio.ensure_future(source.emit(value))
        tasks.append(future)
        receipts.append(future)

    yield asyncio.sleep(0)
    status = yield _wait_for_status(
        buff, lambda s: s['current'] is not None
        and s['current']['state'] == 'failed')
    current = status['current']
    assert current['error'] is not None
    assert current['error_type'] == 'RuntimeError'
    # The first delivery failed, but the records that were merely enqueued
    # must not be reported as written.
    assert status['queued'] == 1
    assert status['waiting'] == 1
    assert status['queued_submissions'][0]['written'] is False
    assert status['waiting_submissions'][0]['written'] is False
    assert status['current']['written'] is False
    assert status['last_delivery'] is None
    assert status['consumer_stalled'] is True
    # Each later record still shows its own position and identity.
    assert status['current']['submission_id'] != (
        status['queued_submissions'][0]['submission_id'])
    assert status['queued_submissions'][0]['state'] == 'queued'
    assert status['queued_submissions'][0]['position'] == 0
    assert status['waiting_submissions'][0]['state'] == 'waiting'
    assert status['waiting_submissions'][0]['position'] == 0

    for task in tasks:
        task.cancel()
    yield gen.sleep(0.05)


@gen_test(timeout=15)
def test_buffer_status_source_stops_advancing_after_failure():
    source = Stream(asynchronous=True)
    buff = source.buffer(1)
    gate = _Gate()

    async def slow_then_fail(x):
        gate.started.append(x)
        await asyncio.sleep(0.01)
        raise ValueError('boom')

    buff.sink(slow_then_fail)

    tasks = [asyncio.ensure_future(source.emit(value)) for value in (1, 2)]
    yield asyncio.sleep(0)
    status = yield _wait_for_status(
        buff, lambda s: s['current'] is not None
        and s['current']['state'] == 'failed')
    assert status['queued'] == 1
    assert status['waiting'] == 0
    # Only the original error propagates.  The queue is not drained by
    # observation and the producer is not released.
    yield gen.sleep(0.05)
    after = buff.status()
    assert after['queued'] == 1
    assert after['current']['submission_id'] == status['current']['submission_id']
    assert after['current']['error_type'] == 'ValueError'
    assert after['consumer_state'] == 'stalled'
    assert after['can_advance'] is False

    for task in tasks:
        task.cancel()
    yield gen.sleep(0.05)


@gen_test(timeout=15)
def test_buffer_status_capacity_snapshot():
    source = Stream(asynchronous=True)
    buff = source.buffer(1)
    gate = _Gate()
    buff.sink(gate.outlet('left'))
    buff.sink(gate.outlet('right'))

    tasks = yield _three_submissions((source, buff))

    status = yield _wait_for_status(
        buff, lambda s: s['queued'] == 1 and s['waiting'] == 1)
    assert status['capacity'] == 1
    assert status['queued'] == 1
    assert status['waiting'] == 1
    assert status['current']['value'] == 1
    assert status['queue'][0]['value'] == 2
    assert status['waiting_submissions'][0]['value'] == 3
    assert status['current']['state'] == 'writing'
    assert status['current']['outlets'][0]['state'] in ('pending', 'writing')
    assert status['current']['outlets'][1]['state'] in ('pending', 'writing')
    # every submission has an identity, even with equal payloads
    assert status['current']['submission_id'] != status['queue'][0]['submission_id']
    assert status['queue'][0]['submission_id'] != status['waiting_submissions'][0]['submission_id']
    assert status['queue'][0]['enqueued'] is True
    assert status['waiting_submissions'][0]['enqueued'] is False
    # a submission that was accepted into the queue is not "written" yet
    assert status['queue'][0]['written'] is False
    assert status['current']['written'] is False

    gate.open('left', 1)
    gate.open('right', 1)
    yield asyncio.gather(*tasks)

    # Enqueueing all three does not mean all three were written.  Release each
    # delivery as the consumer advances to it.
    yield _wait_for_status(buff, lambda s: s['current'] is not None
                           and s['current']['value'] == 2)
    gate.open('left', 2)
    gate.open('right', 2)
    yield _wait_for_status(buff, lambda s: s['current'] is not None
                           and s['current']['value'] == 3)
    gate.open('left', 3)
    gate.open('right', 3)
    yield _wait_for_status(buff, lambda s: s['current'] is None
                           and s['last_delivery']['value'] == 3)

    final = buff.status()
    assert final['queued'] == 0
    assert final['waiting'] == 0
    assert final['current'] is None
    assert final['last_delivery']['value'] == 3
    assert final['last_delivery']['state'] == 'succeeded'


@gen_test(timeout=15)
def test_buffer_status_empty_queue_but_writing():
    source = Stream(asynchronous=True)
    buff = source.buffer(2)
    gate = _Gate()
    buff.sink(gate.outlet('only'))

    yield source.emit(1)
    yield _wait_for_status(buff, lambda s: s['queued'] == 0
                           and s['current'] is not None)
    status = buff.status()
    assert status['queued'] == 0
    assert status['waiting'] == 0
    assert status['current']['value'] == 1
    assert status['current']['state'] == 'writing'
    assert status['current']['outlets'][0]['state'] == 'writing'
    assert status['current']['outlets'][0]['invoked'] is True

    gate.open('only', 1)
    yield _wait_for_status(buff, lambda s: s['current'] is None)
    status = buff.status()
    assert status['current'] is None
    assert status['last_delivery']['value'] == 1
    assert status['last_delivery']['state'] == 'succeeded'
    assert status['last_delivery']['written'] is True


@gen_test(timeout=15)
def test_buffer_status_last_submission_only_writing():
    source = Stream(asynchronous=True)
    buff = source.buffer(1)
    gate = _Gate()
    buff.sink(gate.outlet('left'))

    tasks = [asyncio.ensure_future(source.emit(value)) for value in (1, 2, 3)]
    yield asyncio.sleep(0)
    status = yield _wait_for_status(
        buff, lambda s: s['queued'] == 1 and s['waiting'] == 1)
    gate.open('left', 1)
    yield _wait_for_status(
        buff, lambda s: s['current'] is not None
        and s['current']['value'] == 2)
    gate.open('left', 2)
    status = yield _wait_for_status(
        buff, lambda s: s['current'] is not None
        and s['current']['value'] == 3)
    assert status['queued'] == 0
    assert status['waiting'] == 0
    assert status['current']['state'] == 'writing'
    assert status['last_delivery']['value'] == 2
    assert status['last_delivery']['state'] == 'succeeded'
    assert status['idle'] is False

    gate.open('left', 3)
    status = yield _wait_for_status(buff, lambda s: s['current'] is None)
    assert status['queued'] == 0
    assert status['waiting'] == 0
    assert status['current'] is None
    assert status['idle'] is True
    assert status['last_delivery']['value'] == 3
    assert status['last_delivery']['state'] == 'succeeded'
    yield asyncio.gather(*tasks)


def test_buffer_status_snapshot_is_stable():
    source = Stream()
    buff = source.buffer(1)
    buff.sink(lambda x: x)
    source.emit(1)
    status = buff.status
    assert status['capacity'] == 1
    assert status.capacity == 1
    assert status() == status
    assert buff.snapshot()['capacity'] == 1
    snapshot = status.copy()
    with pytest.raises(TypeError):
        status['capacity'] = 10
    wait_for(lambda: buff.status['queued'] == 0, 1)
    assert status == snapshot
    assert status['sequence'] == snapshot['sequence']


@gen_test(timeout=15)
def test_buffer_status_direct_raise_vs_async_failure():
    source = Stream(asynchronous=True)
    buff = source.buffer(1)
    gate = _Gate()

    async def async_failure(x):
        gate.started.append(('async', x))
        await asyncio.sleep(0)
        raise ValueError('async boom')

    def direct_failure(x):
        gate.started.append(('direct', x))
        raise RuntimeError('direct boom')

    buff.sink(direct_failure)
    buff.sink(async_failure)
    buff.sink(gate.outlet('slow'))

    tasks = [asyncio.ensure_future(source.emit(value)) for value in (1, 2, 3)]
    yield asyncio.sleep(0)

    status = yield _wait_for_status(
        buff, lambda s: s['current'] is not None
        and s['current']['state'] == 'failed')
    current = status['current']
    assert current['submission_id']
    assert current['error'] is not None
    assert current['error_type'] in ('RuntimeError', 'ValueError')
    if current['error_type'] == 'RuntimeError':
        # a synchronous raise stops before later outlets are called
        assert current['outlets'][2]['state'] == 'not_invoked'
        assert current['outlets'][2]['invoked'] is False
    assert status['queued'] == 1
    assert status['waiting'] == 1
    assert status['consumer_stalled'] is True
    assert status['can_advance'] is False
    # observing repeatedly does not make the stalled coroutine advance
    for _ in range(20):
        assert buff.status()['queued'] == 1
        yield asyncio.sleep(0)
    assert buff.status()['current']['submission_id'] == (
        current['submission_id'])
    assert buff.status()['current']['error_type'] in (
        'RuntimeError', 'ValueError')

    for task in tasks:
        task.cancel()
    yield gen.sleep(0.05)


@gen_test(timeout=15)
def test_buffer_status_async_failure_keeps_sibling_writing():
    source = Stream(asynchronous=True)
    buff = source.buffer(1)
    gate = _Gate()

    async def failing_starts_later(x):
        gate.started.append(('failing', x))
        await asyncio.sleep(0.01)
        raise ValueError('later boom')

    buff.sink(gate.outlet('slow'))
    buff.sink(failing_starts_later)

    tasks = [asyncio.ensure_future(source.emit(value)) for value in (1, 2, 3)]
    yield asyncio.sleep(0)
    status = yield _wait_for_status(
        buff, lambda s: s['current'] is not None
        and s['current']['state'] == 'failed')
    current = status['current']
    assert current['error_type'] == 'ValueError'
    assert current['failed_outlets'] == (1,)
    assert current['outlets'][0]['state'] in ('pending', 'writing')
    assert current['outlets'][0]['invoked'] is True
    assert current['outlets'][0]['succeeded'] is False
    assert current['outlets'][1]['state'] == 'failed'
    assert status['queued'] == 1
    assert status['waiting'] == 1
    assert status['consumer_stalled'] is True
    assert status['can_advance'] is False

    gate.open('slow', 1)
    yield _wait_for_status(buff, lambda s: s['current'] is not None
                           and s['current']['outlets'][0]['state']
                           == 'succeeded')
    current = buff.status()['current']
    assert current['outlets'][0]['state'] == 'succeeded'
    assert current['outlets'][1]['state'] == 'failed'
    assert current['state'] == 'failed'

    for task in tasks:
        task.cancel()
    yield gen.sleep(0.05)


@gen_test(timeout=15)
def test_buffer_status_late_completion_does_not_erase_failure():
    source = Stream(asynchronous=True)
    buff = source.buffer(2)

    async def slow_failure(x):
        raise ValueError('slow boom')

    async def slow_success(x):
        await asyncio.sleep(0.02)

    buff.sink(slow_success)
    buff.sink(slow_failure)

    yield source.emit(1)
    yield _wait_for_status(buff, lambda s: s['current'] is not None
                           and s['current']['state'] == 'failed')
    current = buff.status()['current']
    assert current['outlets'][1]['state'] == 'failed'
    assert current['outlets'][0]['state'] in ('invoked', 'writing')
    yield _wait_for_status(buff, lambda s: s['current'] is not None
                           and s['current']['outlets'][0]['state']
                           == 'succeeded')
    current = buff.status()['current']
    assert current['state'] == 'failed'
    assert current['outlets'][0]['state'] == 'succeeded'
    assert current['outlets'][1]['state'] == 'failed'
    assert current['error_type'] == 'ValueError'
    # A later success of a sibling does not clear the recorded failure.
    yield gen.sleep(0.05)
    current = buff.status()['current']
    assert current is not None
    assert current['state'] == 'failed'
    assert current['error_message'] == 'slow boom'
    assert current['succeeded_outlets'] == (0,)
    assert current['failed_outlets'] == (1,)


@gen_test(timeout=30)
def test_buffer_status_swapped_connection_order():
    @gen.coroutine
    def run(order, direct):
        source = Stream(asynchronous=True)
        buff = source.buffer(1)
        gate = _Gate()

        def fail_direct(x):
            gate.started.append(('fail', x))
            raise RuntimeError('direct boom')

        async def fail_later(x):
            gate.started.append(('fail', x))
            await asyncio.sleep(0.005)
            raise ValueError('later boom')

        async def slow(x):
            gate.started.append(('slow', x))
            await asyncio.sleep(0.1)

        outlets = {'slow': slow,
                   'fail': fail_direct if direct else fail_later}
        nodes = [buff.sink(outlets[name]) for name in order]  # noqa: F841

        tasks = [asyncio.ensure_future(source.emit(v)) for v in (1, 2, 3)]
        yield asyncio.sleep(0)
        status = yield _wait_for_status(
            buff, lambda s: s['current'] is not None
            and s['current']['state'] == 'failed')
        current = status['current']
        fail_index = order.index('fail')
        slow_index = order.index('slow')
        expected = 'RuntimeError' if direct else 'ValueError'
        assert current['error_type'] == expected
        assert current['error_outlet'] == fail_index
        assert current['failed_outlets'] == (fail_index,)
        assert current['outlets'][fail_index]['failed'] is True
        if direct:
            if slow_index < fail_index:
                # The slow outlet was called and returned a coroutine, but the
                # direct raise prevented it from ever starting.
                assert current['outlets'][slow_index]['invoked'] is True
                assert current['outlets'][slow_index]['state'] == 'not_started'
                assert current['outlets'][slow_index]['succeeded'] is False
                assert current['outlets'][slow_index]['writing'] is False
            else:
                # Called in order after the raise, so never invoked.
                assert current['outlets'][slow_index]['invoked'] is False
                assert current['outlets'][slow_index]['state'] == 'not_invoked'
        else:
            # The other outlet was called and is still unfinished.
            assert current['outlets'][slow_index]['invoked'] is True
            assert current['outlets'][slow_index]['state'] in (
                'invoked', 'writing')
            assert current['outlets'][slow_index]['in_flight'] is True
        assert status['queued'] == 1
        assert status['waiting'] == 1
        assert status['consumer_stalled'] is True
        assert status['can_advance'] is False
        # The records behind the failed delivery keep their own identities.
        assert status['queued_submissions'][0]['submission_id'] != (
            current['submission_id'])
        assert status['waiting_submissions'][0]['submission_id'] != (
            status['queued_submissions'][0]['submission_id'])
        for task in tasks:
            task.cancel()
        yield gen.sleep(0.02)

    for direct in (True, False):
        for order in (('slow', 'fail'), ('fail', 'slow')):
            yield run(order, direct)


def test_buffer_status_sync_snapshot_without_blocking():
    source = Stream()
    buff = source.buffer(1)
    started = threading.Event()
    release = threading.Event()
    observed = []

    def slow_write(x):
        started.set()
        release.wait(5)

    buff.sink(slow_write)

    def emit():
        source.emit(1)

    emitter = threading.Thread(target=emit)
    emitter.start()
    assert started.wait(5)
    status = buff.status()
    assert status['capacity'] == 1
    assert status['queued'] == 0
    assert status['current']['value'] == 1
    assert status['current']['state'] == 'writing'
    assert status['current']['outlets'][0]['state'] in ('invoked', 'writing')
    assert status['current']['outlets'][0]['invoked'] is True
    assert status['current']['outlets'][0]['pending'] or (
        status['current']['outlets'][0]['in_flight'])
    observed.append(status)
    release.set()
    emitter.join(5)
    assert not emitter.is_alive()
    wait_for(lambda: buff.status()['current'] is None, 1)
    assert buff.status()['last_delivery']['state'] == 'succeeded'
    # A snapshot taken earlier does not change afterwards
    assert observed[0]['current']['state'] == 'writing'


@gen_test(timeout=15)
def test_buffer_status_does_not_advance_producers():
    source = Stream(asynchronous=True)
    buff = source.buffer(1)
    gate = _Gate()

    async def write(x):
        gate.started.append(x)
        await asyncio.sleep(0.1)

    buff.sink(write)
    tasks = [asyncio.ensure_future(source.emit(value)) for value in (1, 2, 3)]
    yield asyncio.sleep(0)
    before = buff.status()
    for _ in range(10):
        buff.status()
        yield asyncio.sleep(0)
    after = buff.status()
    assert before['queued'] == after['queued'] == 1
    assert before['waiting'] == after['waiting'] == 1
    assert after['current']['value'] == 1
    assert after['queued_submissions'][0]['value'] == 2
    assert after['waiting_submissions'][0]['value'] == 3
    yield asyncio.gather(*tasks)


def test_buffer_after_sliding_window():
    Stream().sliding_window(1).buffer(1)


def test_backpressure_connect_empty_stream():
    @Stream.register_api()
    class from_list(Stream):

        def __init__(self, source, **kwargs):
            self.source = source
            super().__init__(ensure_io_loop=True, **kwargs)

        def start(self):
            self.stopped = False
            self.loop.add_callback(self.run)

        @gen.coroutine
        def run(self):
            while not self.stopped and len(self.source) > 0:
                yield self._emit(self.source.pop(0))

    source_list = [0, 1, 2, 3, 4]
    source = Stream.from_list(source_list)
    sout = Stream()
    L = sout.rate_limit(1).sink_to_list()
    source.connect(sout)
    source.start()

    wait_for(lambda: L == [0], 0.01)
    assert len(source_list) > 0
