"""Tests for archiving a Hive columnar result stream to a file receiver.

Scenario under test: an extraction job first picks a few rows to verify,
then hands the remaining results to a file receiver as columnar blocks.
Every delivery carries the identity of the current query and a half-open
[start, end) row interval, so the receiver can tell when it may append
the next contiguous block.

The simulated columnar service answers one page of PAGE_SIZE rows per
fetch. The cursor mirrors the impyla HiveServer2Cursor contract:

* fetchone() buffers a whole page and pops a single row, leaving the
  remaining rows in the local buffer;
* fetchcbatch() hands over the buffered remainder without a server
  round trip, or fetches exactly one new page;
* an empty page that still declares more rows yields None from
  fetchcbatch() and does not move the stream position;
* a new execute() on the same cursor starts a new query identity and
  resets the row position to zero.

The service also replays two fault samples: a server start offset that
disagrees with the local expectation, and a fetch interrupted before
the request is known to be received (so the client cannot tell whether
the server advanced).
"""

import pytest

PAGE_SIZE = 4


class FetchInterrupted(Exception):
    """The fetch exchange broke before the server acknowledged it."""


class Page(object):
    """One scripted page answered by the columnar service.

    start_offset is the server-declared offset of the first row, or None
    when the server does not provide a verifiable offset.
    """

    def __init__(self, values, start_offset, has_more):
        self.values = list(values)
        self.start_offset = start_offset
        self.has_more = has_more


class Fault(object):
    """Script item that interrupts the fetch exchange."""


class FetchRecord(object):
    """Evidence left by the service for every answered fetch."""

    def __init__(self, query_id, start_offset, num_rows, has_more):
        self.query_id = query_id
        self.start_offset = start_offset
        self.num_rows = num_rows
        self.has_more = has_more

    def __repr__(self):
        return ('FetchRecord(query_id=%r, start_offset=%r, num_rows=%r, '
                'has_more=%r)' % (self.query_id, self.start_offset,
                                  self.num_rows, self.has_more))


class ScriptedColumnarService(object):
    """Columnar service that answers fetches from a per-query script.

    Every answered fetch is recorded in fetch_log as evidence. A Fault
    item raises FetchInterrupted and leaves no record, so the client
    cannot confirm whether the server advanced its position.
    """

    def __init__(self):
        self._planned = []
        self._scripts = {}
        self._query_seq = 0
        self.fetch_log = []
        self.fetch_calls = 0

    def plan_query(self, script):
        self._planned.append(list(script))

    def execute(self):
        self._query_seq += 1
        query_id = 'query-%d' % self._query_seq
        self._scripts[query_id] = self._planned.pop(0)
        return query_id

    def fetch(self, query_id):
        self.fetch_calls += 1
        item = self._scripts[query_id].pop(0)
        if isinstance(item, Fault):
            raise FetchInterrupted(
                'fetch for %s interrupted before acknowledgement'
                % query_id)
        self.fetch_log.append(FetchRecord(query_id, item.start_offset,
                                          len(item.values), item.has_more))
        return item


class ColumnBlock(object):
    """A columnar block handed off by the cursor (mirrors CBatch)."""

    def __init__(self, values, expect_more_rows):
        self.values = list(values)
        self.expect_more_rows = expect_more_rows
        self.num_rows = len(self.values)

    def __len__(self):
        return len(self.values)

    def pop_row(self):
        if not self.values:
            raise StopIteration
        return (self.values.pop(0),)


class FakeColumnarCursor(object):
    """Cursor with the fetchone/fetchcbatch contract of HiveServer2Cursor.

    fetchone() fills the local buffer from a whole page (skipping empty
    pages that promise more rows) and pops one row. fetchcbatch()
    returns the buffered remainder without contacting the server, or
    fetches exactly one page; an empty page yields None even when the
    server declares more rows.
    """

    def __init__(self, service):
        self.service = service
        self.query_id = None
        self._buffer = None
        self._active = False
        self.rowcount = -1

    def execute(self, sql):
        self.query_id = self.service.execute()
        self._buffer = None
        self._active = True
        self.rowcount = -1

    def _fetch_page(self):
        page = self.service.fetch(self.query_id)
        self._active = page.has_more
        return ColumnBlock(page.values, page.has_more)

    def fetchone(self):
        if self._buffer is None or len(self._buffer) == 0:
            while True:
                block = self._fetch_page()
                if len(block) > 0:
                    self._buffer = block
                    break
                if not block.expect_more_rows:
                    return None
        if self.rowcount == -1:
            self.rowcount = 0
        self.rowcount += 1
        return self._buffer.pop_row()

    def fetchcbatch(self):
        if self._buffer is not None and len(self._buffer) > 0:
            block = self._buffer
            self._buffer = None
            return block
        if not self._active:
            return None
        block = self._fetch_page()
        if len(block) == 0:
            return None
        return block


class Delivery(object):
    """One block handed to the receiver: [start, end) rows of a query."""

    def __init__(self, query_id, start, end, payload, confirmed):
        self.query_id = query_id
        self.start = start
        self.end = end
        self.payload = payload
        # confirmed=True means a verifiable server offset backs this
        # interval; False means it is counted from local consumption only.
        self.confirmed = confirmed

    @property
    def interval(self):
        return (self.start, self.end)


class OffsetMismatchError(Exception):
    """A trusted server offset disagrees with the local position."""

    def __init__(self, query_id, expected, server_offset, evidence):
        super(OffsetMismatchError, self).__init__(
            'handoff for %s stops at row %s: server declared offset %s '
            '(evidence: %r)' % (query_id, expected, server_offset,
                                evidence))
        self.query_id = query_id
        self.expected = expected
        self.server_offset = server_offset
        self.evidence = evidence


class UncertainPositionError(Exception):
    """The fetch was interrupted; the server position is unknown."""

    def __init__(self, query_id, last_known):
        super(UncertainPositionError, self).__init__(
            'fetch for %s interrupted after row %s; cannot confirm '
            'whether the server advanced, not resending automatically'
            % (query_id, last_known))
        self.query_id = query_id
        self.last_known = last_known


class ArchiveExtractor(object):
    """Pulls rows and column blocks from the cursor for the receiver.

    Intervals advance by the number of values actually handed off by the
    cursor. Server-declared page offsets are checked against the local
    position when available; without them, intervals are marked as
    counted locally only. An interrupted fetch is never resent
    automatically, because the server position cannot be confirmed.
    """

    def __init__(self, cursor, service):
        self.cursor = cursor
        self.service = service
        self.query_id = None
        self.pos = 0
        self.handed_off = 0
        self.confirmed_through = 0
        self.uncertain = False

    def begin_query(self):
        """Start archiving the query the cursor just executed."""
        self.query_id = self.cursor.query_id
        self.pos = 0
        self.handed_off = 0
        self.confirmed_through = 0
        self.uncertain = False

    def _check_new_pages(self, log_before, expected_offset):
        for record in self.service.fetch_log[log_before:]:
            if record.query_id != self.query_id:
                continue
            if record.num_rows == 0:
                continue  # empty page: the position does not move
            if record.start_offset is None:
                continue  # no verifiable offset: local count only
            if record.start_offset != expected_offset:
                raise OffsetMismatchError(self.query_id, expected_offset,
                                          record.start_offset, record)
            self.confirmed_through = record.start_offset + record.num_rows

    def take_sample_row(self):
        """Pick one row to verify, as a one-row delivery."""
        log_before = len(self.service.fetch_log)
        row = self.cursor.fetchone()
        self._check_new_pages(log_before, self.pos)
        if row is None:
            return None
        delivery = Delivery(self.query_id, self.pos, self.pos + 1, [row],
                            self.pos + 1 <= self.confirmed_through)
        self.pos += 1
        self.handed_off += 1
        return delivery

    def take_block(self):
        """Hand off the next column block, or None at the real end.

        An empty page that still declares more rows does not move the
        position and is simply fetched through.
        """
        while True:
            log_before = len(self.service.fetch_log)
            try:
                block = self.cursor.fetchcbatch()
            except FetchInterrupted as exc:
                self.uncertain = True
                raise UncertainPositionError(self.query_id, self.pos) \
                    from exc
            self._check_new_pages(log_before, self.pos)
            if block is None:
                if self.cursor._active:
                    continue  # empty page promising more rows
                return None  # the stream is really finished
            num_rows = len(block)
            end = self.pos + num_rows
            delivery = Delivery(self.query_id, self.pos, end, block,
                                end <= self.confirmed_through)
            self.pos = end
            self.handed_off += num_rows
            return delivery


class NonContiguousBlockError(Exception):
    """A delivery does not start where the stream expects it."""


class QueryIdReuseError(Exception):
    """A finished query identity was reused for a new delivery."""


class StreamState(object):
    """Per-query receiving state on the file endpoint."""

    def __init__(self, query_id):
        self.query_id = query_id
        self.next_offset = 0
        self.deliveries = []
        self.completed = False
        self.mutually_confirmed = True


class FileReceiver(object):
    """File endpoint that only appends the next contiguous block."""

    def __init__(self):
        self.streams = {}

    def accept(self, delivery):
        stream = self.streams.get(delivery.query_id)
        if stream is not None and stream.completed:
            raise QueryIdReuseError(
                'query identity %s was already delivered completely'
                % delivery.query_id)
        if stream is None:
            stream = StreamState(delivery.query_id)
            self.streams[delivery.query_id] = stream
        if delivery.start != stream.next_offset:
            raise NonContiguousBlockError(
                'stream %s expects row %s but got a block starting at %s'
                % (delivery.query_id, stream.next_offset, delivery.start))
        stream.deliveries.append(delivery)
        stream.next_offset = delivery.end
        if not delivery.confirmed:
            stream.mutually_confirmed = False

    def complete(self, query_id):
        self.streams[query_id].completed = True


def make_extractor(scripts):
    service = ScriptedColumnarService()
    for script in scripts:
        service.plan_query(script)
    cursor = FakeColumnarCursor(service)
    extractor = ArchiveExtractor(cursor, service)
    receiver = FileReceiver()
    return service, cursor, extractor, receiver


def test_sample_buffer_empty_page_and_next_page_intervals():
    service, cursor, extractor, receiver = make_extractor([
        [Page(range(0, 4), 0, True),
         Page([], 4, True),   # empty page that still declares more rows
         Page(range(4, 8), 4, True),
         Page([], 8, False)],  # real end of the stream
    ])
    cursor.execute('select n from t')
    extractor.begin_query()

    sample = extractor.take_sample_row()
    assert sample.interval == (0, 1)
    receiver.accept(sample)

    # the three rows still buffered locally start at row 1
    buffered = extractor.take_block()
    assert buffered.interval == (1, 4)
    assert service.fetch_calls == 1  # no server round trip was needed
    receiver.accept(buffered)

    # the empty page is fetched through, then the next page starts at 4
    block = extractor.take_block()
    assert block.interval == (4, 8)
    receiver.accept(block)

    empty = service.fetch_log[1]
    assert empty.num_rows == 0 and empty.has_more

    # a temporarily empty page and the real end are distinguishable
    assert extractor.take_block() is None
    receiver.complete(extractor.query_id)

    stream = receiver.streams[extractor.query_id]
    assert [d.interval for d in stream.deliveries] == [(0, 1), (1, 4),
                                                       (4, 8)]
    assert stream.next_offset == 8
    assert stream.mutually_confirmed


def test_server_offset_mismatch_stops_handoff_with_evidence():
    service, cursor, extractor, receiver = make_extractor([
        [Page(range(0, 4), 0, True),
         Page(range(4, 8), 5, True)],  # server claims row 5, local says 4
    ])
    cursor.execute('select n from t')
    extractor.begin_query()
    receiver.accept(extractor.take_sample_row())
    receiver.accept(extractor.take_block())

    with pytest.raises(OffsetMismatchError) as excinfo:
        extractor.take_block()
    err = excinfo.value
    assert err.expected == 4
    assert err.server_offset == 5
    assert err.evidence.start_offset == 5
    assert 'stops at row 4' in str(err)

    # the stream is not marked contiguous past the mismatch
    stream = receiver.streams[extractor.query_id]
    assert stream.next_offset == 4
    assert not stream.completed


def test_interrupted_fetch_is_not_resent_automatically():
    service, cursor, extractor, receiver = make_extractor([
        [Page(range(0, 4), 0, True),
         Fault(),  # interrupted before the request is known to arrive
         Page(range(4, 8), 4, False)],
    ])
    cursor.execute('select n from t')
    extractor.begin_query()
    receiver.accept(extractor.take_sample_row())
    receiver.accept(extractor.take_block())
    calls_before = service.fetch_calls

    with pytest.raises(UncertainPositionError) as excinfo:
        extractor.take_block()
    assert excinfo.value.query_id == extractor.query_id
    assert excinfo.value.last_known == 4
    assert extractor.uncertain

    # no automatic resend: exactly one interrupted attempt was made, and
    # no delivery claimed the stream continued without loss or duplicates
    assert service.fetch_calls == calls_before + 1
    stream = receiver.streams[extractor.query_id]
    assert stream.next_offset == 4
    assert not stream.completed


def test_new_query_on_same_cursor_gets_new_identity():
    service, cursor, extractor, receiver = make_extractor([
        [Page(range(0, 4), 0, False)],
        [Page(range(10, 14), 0, False)],
    ])
    cursor.execute('select n from t')
    extractor.begin_query()
    first_query_id = extractor.query_id
    receiver.accept(extractor.take_sample_row())
    receiver.accept(extractor.take_block())
    assert extractor.take_block() is None
    receiver.complete(first_query_id)

    # a new query on the same cursor starts from zero with a new identity
    cursor.execute('select n from t2')
    extractor.begin_query()
    assert extractor.query_id != first_query_id
    assert extractor.pos == 0
    delivery = extractor.take_sample_row()
    assert delivery.interval == (0, 1)
    receiver.accept(delivery)
    assert receiver.streams[extractor.query_id].next_offset == 1

    # reusing the previous delivery's query identity is rejected
    with pytest.raises(QueryIdReuseError):
        receiver.accept(Delivery(first_query_id, 0, 1, [(0,)], True))


def test_missing_server_offset_marks_intervals_local_count_only():
    service, cursor, extractor, receiver = make_extractor([
        [Page(range(0, 4), None, True),
         Page(range(4, 8), None, False)],
    ])
    cursor.execute('select n from t')
    extractor.begin_query()

    sample = extractor.take_sample_row()
    block = extractor.take_block()
    receiver.accept(sample)
    receiver.accept(block)

    # intervals still advance by local consumption, but are not
    # mutually confirmed without a verifiable server offset
    assert not sample.confirmed
    assert not block.confirmed
    stream = receiver.streams[extractor.query_id]
    assert stream.next_offset == 4
    assert not stream.mutually_confirmed


def test_consuming_delivered_block_does_not_change_handoff_counts():
    service, cursor, extractor, receiver = make_extractor([
        [Page(range(0, 4), 0, True),
         Page(range(4, 8), 4, False)],
    ])
    cursor.execute('select n from t')
    extractor.begin_query()
    receiver.accept(extractor.take_sample_row())
    delivery = extractor.take_block()
    receiver.accept(delivery)
    assert delivery.interval == (1, 4)
    assert extractor.handed_off == 4

    # reading the delivered block to the end afterwards does not move
    # the cursor's handoff accounting
    block = delivery.payload
    while len(block) > 0:
        block.pop_row()
    assert extractor.pos == 4
    assert extractor.handed_off == 4
    assert cursor.rowcount == 1

    following = extractor.take_block()
    assert following.interval == (4, 8)
