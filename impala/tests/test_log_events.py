# Tests for continuous log reading (impala.log_events and the
# read_log/fetch_log paths in impala.hiveserver2). These tests use mock
# services and do not need a live server.

from pytest import raises
from thrift.Thrift import TApplicationException

from impala._thrift_gen.TCLIService.ttypes import (
    TColumn, TFetchResultsResp, TGetLogResp, TProtocolVersion, TRowSet,
    TStatus, TStatusCode, TStringColumn)
from impala.error import ProgrammingError
from impala.hiveserver2 import HiveServer2Cursor, Operation
from impala.log_events import LogEvent, LogRows, LogTracker


class FakeSession(object):
    handle = None
    config = None
    hs2_protocol_version = TProtocolVersion.HIVE_CLI_SERVICE_PROTOCOL_V6

    def close(self):
        pass


class FakeLogOperation(object):
    """A mock operation standing in for one query execution."""

    def __init__(self, pages, final_text):
        # pages: list of LogRows or str (whole-text snapshot) returned by
        # successive fetch_log() calls
        self.handle = None
        self._pages = list(pages)
        self._final_text = final_text
        self.fetch_count = 0

    def fetch_log(self, max_rows=1024):
        self.fetch_count += 1
        if self._pages:
            return self._pages.pop(0)
        if isinstance(self._final_text, str):
            return self._final_text
        return LogRows(0, [])

    def get_log(self):
        return self._final_text

    def close(self):
        return None


def make_cursor():
    return HiveServer2Cursor(session=FakeSession())


def start_operation(cur, operation):
    def op():
        cur._last_operation_string = 'select 1'
        cur._last_operation = operation
    cur._execute_async(op)


def positions_and_texts(log_read):
    return positions_and_texts_list(log_read.events)


def positions_and_texts_list(events):
    return [(e.position, e.text) for e in events]


def test_hive_rows_multiline_event_and_overlap():
    # A log value itself contains a newline; another row's text is exactly
    # the same as the following line of that value.
    page1 = LogRows(0, ['start', 'line2\nline3', 'line3'])
    # The next page repeats a segment from a known start position and
    # brings one new event.
    page2 = LogRows(1, ['line2\nline3', 'line3', 'line4'])
    op = FakeLogOperation([page1, page2],
                          'start\nline2\nline3\nline3\nline4')
    cur = make_cursor()
    start_operation(cur, op)

    read1 = cur.read_log()
    assert read1.continued
    assert not read1.from_closed
    assert read1.snapshot is None
    # The multi-line value stays a single event; the row with the same
    # text as its last line is a separate event at its own position.
    assert positions_and_texts(read1) == [
        (0, 'start'), (1, 'line2\nline3'), (2, 'line3')]
    assert cur.log_position == 3

    read2 = cur.read_log()
    assert read2.continued
    # The overlap at known positions is not duplicated; only the new
    # event appears.
    assert positions_and_texts(read2) == [(3, 'line4')]
    assert cur.log_position == 4
    assert [e.text for e in cur._log_tracker.events] == [
        'start', 'line2\nline3', 'line3', 'line4']


def test_hive_rows_conflicting_position_is_not_continued():
    page1 = LogRows(0, ['a', 'b'])
    # The server gives a position that conflicts with seen content.
    page2 = LogRows(1, ['X', 'c'])
    op = FakeLogOperation([page1, page2], 'a\nb')
    cur = make_cursor()
    start_operation(cur, op)

    read1 = cur.read_log()
    assert read1.continued
    assert positions_and_texts(read1) == [(0, 'a'), (1, 'b')]

    read2 = cur.read_log()
    assert not read2.continued
    assert read2.events == ()
    assert cur.log_position is None
    # The confirmed evidence is left untouched.
    assert positions_and_texts_list(cur._log_tracker.events) == [
        (0, 'a'), (1, 'b')]


def test_hive_rows_gap_is_not_continued():
    page1 = LogRows(0, ['a'])
    page2 = LogRows(3, ['d'])
    op = FakeLogOperation([page1, page2], 'a')
    cur = make_cursor()
    start_operation(cur, op)

    assert cur.read_log().continued
    read2 = cur.read_log()
    assert not read2.continued
    assert read2.events == ()


def test_impala_snapshots_have_no_event_boundaries():
    # Both snapshots start with the same two lines; the second one is
    # shorter and changed in the middle, so no event boundaries can be
    # derived.
    text1 = 'line1\nline2\nline3\nline4'
    text2 = 'line1\nline2\nchanged'
    op = FakeLogOperation([text1, text2], text2)
    cur = make_cursor()
    start_operation(cur, op)

    read1 = cur.read_log()
    assert read1.events == ()
    assert read1.snapshot == text1
    assert not read1.continued
    assert read1.position is None

    read2 = cur.read_log()
    assert read2.events == ()
    assert read2.snapshot == text2
    assert not read2.continued
    # Snapshots keep their original format so changes can be reviewed.
    assert cur._log_tracker.snapshots == (text1, text2)


def test_closed_operation_serves_saved_log_only():
    page1 = LogRows(0, ['start', 'middle'])
    final_text = 'start\nmiddle\nend'
    op = FakeLogOperation([page1], final_text)
    cur = make_cursor()
    start_operation(cur, op)

    read1 = cur.read_log()
    assert positions_and_texts(read1) == [(0, 'start'), (1, 'middle')]

    # The query finishes fetching and is closed; the client only keeps
    # the log saved before closing.
    cur._close_finished_operation()
    fetches_before = op.fetch_count

    # Re-reading returns the last available evidence from the closed
    # cache, not new server content.
    read2 = cur.read_log()
    assert read2.from_closed
    assert read2.events == ()
    assert read2.snapshot == final_text
    assert read2.continued
    assert read2.position == 2
    assert op.fetch_count == fetches_before

    # The old get_log text path still works and returns the saved log.
    assert cur.get_log() == final_text


def test_positions_do_not_cross_queries():
    op1 = FakeLogOperation([LogRows(0, ['q1a', 'q1b'])], 'q1a\nq1b')
    cur = make_cursor()
    start_operation(cur, op1)
    assert cur.log_position is None
    read1 = cur.read_log()
    assert cur.log_position == 2
    assert read1.position == 2

    # Switching to a new query invalidates the previous read position.
    op2 = FakeLogOperation([LogRows(0, ['q2a'])], 'q2a')
    start_operation(cur, op2)
    assert cur.log_position is None
    read2 = cur.read_log()
    assert positions_and_texts(read2) == [(0, 'q2a')]
    assert read2.position == 1


def test_read_log_without_operation():
    cur = make_cursor()
    with raises(ProgrammingError):
        cur.read_log()


class FakeRpcOperation(Operation):
    """Operation with a scripted _rpc for fetch_log tests."""

    def __init__(self, responses):
        self.session = FakeSession()
        self.handle = None
        self._responses = list(responses)
        self.calls = []

    def _rpc(self, func_name, request, safe_to_retry=False):
        self.calls.append(func_name)
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def make_fetch_resp(texts, start_offset):
    # STRING column values arrive as binary and are decoded by CBatch.
    values = [t.encode('utf-8') for t in texts]
    rowset = TRowSet(
        startRowOffset=start_offset,
        columns=[TColumn(stringVal=TStringColumn(values=values,
                                                 nulls=b'\x00'))])
    return TFetchResultsResp(
        status=TStatus(statusCode=TStatusCode.SUCCESS_STATUS),
        hasMoreRows=False,
        results=rowset)


def test_operation_fetch_log_whole_text():
    op = FakeRpcOperation([
        TGetLogResp(status=TStatus(statusCode=TStatusCode.SUCCESS_STATUS),
                    log='whole\ntext')])
    result = op.fetch_log()
    assert result == 'whole\ntext'
    assert op.calls == ['GetLog']


def test_operation_fetch_log_rows():
    unknown_method = TApplicationException(
        type=TApplicationException.UNKNOWN_METHOD)
    op = FakeRpcOperation([
        unknown_method,
        make_fetch_resp(['one', 'two\nthree'], 5)])
    result = op.fetch_log()
    assert isinstance(result, LogRows)
    assert result.start_offset == 5
    # A row value containing a newline stays a single row.
    assert result.texts == ['one', 'two\nthree']
    assert op.calls == ['GetLog', 'FetchResults']


def test_tracker_reread_of_closed_cache_is_not_new_content():
    tracker = LogTracker()
    op = FakeLogOperation([LogRows(0, ['a'])], 'a\nb')
    read1 = tracker.read(op)
    assert [e.text for e in read1.events] == ['a']
    tracker.seal('a\nb')
    read2 = tracker.read(op)
    assert read2.from_closed
    assert read2.events == ()
    assert read2.snapshot == 'a\nb'
    read3 = tracker.read(op)
    assert read3.events == ()
    assert tracker.events == (LogEvent(0, 'a'),)
