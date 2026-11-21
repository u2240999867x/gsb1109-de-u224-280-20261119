"""Offline tests for opt-in executemany write progress.

These tests use a scripted connection so the client-side batching, the
lookahead parameter and the transaction outcome can be exercised without a
MySQL server.  The connection records the exact statement bytes it receives
and answers each one from a small response script.
"""

import pytest

import pymysql
from pymysql import err
from pymysql.constants import ER


class _Result:
    def __init__(self, affected_rows):
        self.affected_rows = affected_rows
        self.warning_count = 0
        self.description = None
        self.insert_id = 0
        self.rows = ()
        self.has_next = False


class _Transport:
    """Mixin that records queries and answers from a response script."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.statements = []

    def query(self, sql, unbuffered=False):
        if isinstance(sql, str):
            sql = sql.encode(self.encoding)
        raw = bytes(sql)
        self.statements.append(raw)
        affected = self._answer(sql)
        self._result = _Result(affected)
        self._affected_rows = affected
        self._note_transaction_command(raw)
        return affected

    def _answer(self, sql):
        text = sql.decode(self.encoding)
        if text.strip().rstrip(";").upper() in (
            "COMMIT",
            "ROLLBACK",
            "BEGIN",
            "SET AUTOCOMMIT = 0",
            "SET AUTOCOMMIT = 1",
        ):
            return 0
        if not self.responses:
            raise AssertionError(f"unexpected statement: {text!r}")
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            return response(text)
        return response


class ScriptedConnection(_Transport, pymysql.connections.Connection):
    """A real deferred connection with a scripted transport and no socket."""

    def __init__(self, responses=()):
        _Transport.__init__(self, responses)
        pymysql.connections.Connection.__init__(
            self,
            user="scripted",
            password="",
            charset="utf8mb4",
            ssl_disabled=True,
            defer_connect=True,
        )
        self.server_status = 0

    def commit(self):
        self.query("COMMIT")

    def rollback(self):
        self.query("ROLLBACK")


QUERY = "INSERT INTO ledger (id, note) VALUES (%s, %s)"


def make_data(count):
    return [(i, f"note-{i}-'\\é中") for i in range(count)]


def run(connection, data, progress, max_stmt_length=1024):
    cursor = pymysql.cursors.Cursor(connection)
    cursor.max_stmt_length = max_stmt_length
    return cursor, cursor.executemany(QUERY, iter(data), progress=progress)


def test_acknowledged_then_server_rejection():
    progress = pymysql.cursors.WriteProgress()
    conn = ScriptedConnection([3, err.IntegrityError(ER.DUP_ENTRY, "Duplicate entry")])
    data = make_data(6)

    with pytest.raises(err.IntegrityError):
        run(conn, data, progress, max_stmt_length=120)

    assert len(conn.statements) == 2
    first, second = progress.batches

    # Each batch reports the original half-open input range it really sent.
    assert first.range == (0, 3)
    assert second.range == (3, 6)
    assert first.count == 3 and second.count == 3
    assert first.sql == conn.statements[0]
    assert second.sql == conn.statements[1]

    # The first statement was acknowledged inside the transaction, not committed.
    assert first.state == progress.ACKNOWLEDGED
    assert first.execution_state == progress.ACKNOWLEDGED
    assert first.affected_rows == 3
    assert not first.landed
    assert progress.status(0) == progress.status(2) == progress.ACKNOWLEDGED

    # The second statement fails as a whole: no single row is blamed.
    assert second.state == progress.REJECTED
    assert isinstance(second.error, err.IntegrityError)
    assert second.error.args[0] == ER.DUP_ENTRY
    assert all(progress.status(index) == progress.REJECTED for index in range(3, 6))

    # Rows acknowledged before the failure stay acknowledged, and the
    # transaction has not landed them.
    assert progress.affected_rows == 3
    assert progress.landed_rows == 0
    assert progress.total is None


def test_lookahead_is_formatted_but_not_sent():
    progress = pymysql.cursors.WriteProgress()
    conn = ScriptedConnection([2, err.OperationalError(1205, "Lock wait timeout")])
    data = make_data(6)

    with pytest.raises(err.OperationalError):
        run(conn, data, progress, max_stmt_length=90)

    assert len(conn.statements) == 2
    # The parameter pulled to trigger the split of batch two is formatted on
    # the client but is not part of any statement that reached the server.
    assert progress.pulled == 5
    assert progress.status(4) == progress.NOT_SENT
    assert progress.item(4).formatted is True
    assert progress.batches[-1].range == (4, 5)
    assert progress.batches[-1].sent is False
    assert progress.status(3) == progress.REJECTED
    assert b"note-4" not in b"".join(conn.statements)
    assert progress.pending == (4,)
    assert progress.states() == {
        0: progress.ACKNOWLEDGED,
        1: progress.ACKNOWLEDGED,
        2: progress.REJECTED,
        3: progress.REJECTED,
        4: progress.NOT_SENT,
    }


def test_unknown_iterator_tail_is_not_pulled():
    progress = pymysql.cursors.WriteProgress()
    conn = ScriptedConnection([3])
    data = make_data(3)
    reads = []

    def counted():
        for row in data:
            reads.append(row[0])
            yield row

    cursor = pymysql.cursors.Cursor(conn)
    cursor.max_stmt_length = 1024
    # One batch: the loop observes StopIteration but no fourth parameter is
    # pulled, so the unknown tail is not counted as obtained.
    assert cursor.executemany(QUERY, counted(), progress=progress) == 3
    assert reads == [0, 1, 2]
    assert progress.pulled == 3
    assert progress.status(3) is None
    assert progress.total is None
    assert len(progress.batches) == 1
    assert progress.batches[0].range == (0, 3)


def test_same_send_ranges_rejection_vs_unknown():
    data = make_data(4)

    rejected = pymysql.cursors.WriteProgress()
    rejection = ScriptedConnection(
        [2, err.IntegrityError(ER.DUP_ENTRY, "Duplicate entry")]
    )
    with pytest.raises(err.IntegrityError):
        run(rejection, data, rejected, max_stmt_length=90)

    unknown = pymysql.cursors.WriteProgress()
    dropped = ScriptedConnection(
        [2, err.OperationalError(2013, "Lost connection to MySQL server during query")]
    )
    with pytest.raises(err.OperationalError):
        run(dropped, data, unknown, max_stmt_length=90)

    # Both runs send exactly the same two statements over the same ranges.
    assert rejection.statements == dropped.statements
    assert [batch.range for batch in rejected.batches] == [(0, 2), (2, 4)]
    assert [batch.range for batch in unknown.batches] == [(0, 2), (2, 4)]
    assert rejected.batches[0].sql == unknown.batches[0].sql
    assert rejected.batches[1].sql == unknown.batches[1].sql

    assert rejected.batches[1].state == rejected.REJECTED
    assert unknown.batches[1].state == unknown.OUTCOME_UNKNOWN
    assert isinstance(rejected.batches[1].error, err.IntegrityError)
    assert isinstance(unknown.batches[1].error, err.OperationalError)
    assert rejected.status(0) == unknown.status(0) == rejected.ACKNOWLEDGED


def test_confirmed_rollback_clears_acknowledged_batches():
    progress = pymysql.cursors.WriteProgress()
    conn = ScriptedConnection([2, 2])
    data = make_data(4)

    run(conn, data, progress, max_stmt_length=90)
    assert all(batch.state == progress.ACKNOWLEDGED for batch in progress.batches)
    assert progress.affected_rows == 4

    conn.rollback()
    assert progress.rolled_back
    assert all(batch.state == progress.ROLLED_BACK for batch in progress.batches)
    assert progress.affected_rows == 4
    assert progress.landed_rows == 0
    assert not any(batch.landed for batch in progress.batches)


def test_confirmed_commit_marks_landed():
    progress = pymysql.cursors.WriteProgress()
    conn = ScriptedConnection([2, 2])
    data = make_data(4)

    run(conn, data, progress, max_stmt_length=90)
    conn.commit()

    assert progress.transaction_state == "committed"
    assert all(batch.state == progress.COMMITTED for batch in progress.batches)
    assert progress.landed_rows == 4
    assert progress.status(0) == progress.COMMITTED


def test_transaction_control_via_cursor_execute_is_observed():
    progress = pymysql.cursors.WriteProgress()
    conn = ScriptedConnection([2, 2])
    data = make_data(4)

    cursor, _ = run(conn, data, progress, max_stmt_length=90)
    cursor.execute("ROLLBACK")
    assert progress.transaction_state == "rolled_back"
    assert progress.status(0) == progress.ROLLED_BACK
    assert progress.landed_rows == 0

    cursor.execute("COMMIT;")
    assert progress.batches[0].state == progress.ROLLED_BACK


def test_rollback_before_commit_is_not_rolled_back_after():
    progress = pymysql.cursors.WriteProgress()
    conn = ScriptedConnection([2, 2])
    data = make_data(4)

    run(conn, data, progress, max_stmt_length=90)
    conn.rollback()
    assert progress.status(0) == progress.ROLLED_BACK

    # A later commit must not promote a batch that was already rolled back,
    # and nothing acknowledged before the rollback becomes landed.
    conn.commit()
    assert progress.batches[0].state == progress.ROLLED_BACK
    assert not any(batch.landed for batch in progress.batches)
    assert progress.landed_rows == 0


def test_original_affected_rows_return_value_is_preserved():
    progress = pymysql.cursors.WriteProgress()
    conn = ScriptedConnection([2, 2])
    cursor, rows = run(conn, make_data(4), progress, max_stmt_length=90)

    assert rows == 4
    assert cursor.rowcount == 4

    plain = ScriptedConnection([2, 2])
    plain_cursor = pymysql.cursors.Cursor(plain)
    plain_cursor.max_stmt_length = 90
    assert plain_cursor.executemany(QUERY, iter(make_data(4))) == 4
    assert plain_cursor.rowcount == 4
    assert plain.statements == conn.statements


def test_progress_rejects_reuse_across_calls():
    progress = pymysql.cursors.WriteProgress()
    conn = ScriptedConnection([2])
    run(conn, make_data(1), progress)

    with pytest.raises(ValueError):
        run(conn, make_data(1), progress)


def test_multi_byte_values_are_counted_as_encoded_bytes():
    progress = pymysql.cursors.WriteProgress()
    conn = ScriptedConnection([1, 1])
    data = [(0, "中文"), (1, "中文"), (2, "中文")]

    # The multibyte text is three UTF-8 bytes per character; the split must be
    # based on the encoded statement length, not the character count.
    run(conn, data, progress, max_stmt_length=65)
    assert [batch.range for batch in progress.batches] == [(0, 2), (2, 3)]
    assert progress.batches[0].sql == conn.statements[0]
    assert progress.batches[1].sql == conn.statements[1]
    # Two 32-character Chinese values need more encoded bytes than characters.
    assert len(progress.batches[0].sql) == 64
    assert progress.batches[0].sql.count("中".encode()) == 2
