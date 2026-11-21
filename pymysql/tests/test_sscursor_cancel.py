"""Tests for cancelling an unbuffered (SSCursor) result set.

The MySQL server is replaced by a scripted socket, so every byte the client
reads is fully controlled by the test. Reads that represent rows which have
not arrived yet block on an explicit signal instead of relying on sleeps or
other timing assumptions.
"""

import gc
import io
import struct
import threading

import pytest

import pymysql
from pymysql.constants import CR, FIELD_TYPE, SERVER_STATUS


def _frame(payload, seq):
    """Prefix a payload with a MySQL packet header (length and sequence id)."""
    return struct.pack("<I", len(payload))[:3] + bytes([seq]) + payload


def _eof_packet():
    # 0xfe, warning count = 0, server status = AUTOCOMMIT, no more results.
    return b"\xfe" + struct.pack("<HH", 0, SERVER_STATUS.SERVER_STATUS_AUTOCOMMIT)


def _ok_packet():
    # 0x00, affected rows = 0, insert id = 0, status = AUTOCOMMIT, warnings = 0.
    return b"\x00\x00\x00" + struct.pack(
        "<HH", SERVER_STATUS.SERVER_STATUS_AUTOCOMMIT, 0
    )


def _column_def(name):
    # Six length coded strings, then the 13 byte column descriptor block.
    strings = b"".join(
        [
            b"\x03def",
            b"\x01d",
            b"\x01t",
            b"\x01t",
            bytes([len(name)]) + name,
            bytes([len(name)]) + name,
        ]
    )
    # filler, charset (63=binary), length, type, flags, decimals, filler x2.
    fixed = (
        b"\x00"
        + struct.pack("<HIBHB", 63, 11, FIELD_TYPE.VAR_STRING, 0, 0)
        + b"\x00\x00"
    )
    return strings + fixed


def _result_set_packets(rows):
    """Build the full packet stream of one result set.

    Sequence ids start at 1, matching the first server packet after a query.
    """
    packets = [
        _frame(b"\x01", 1),  # one column
        _frame(_column_def(b"c"), 2),
        _frame(_eof_packet(), 3),
    ]
    seq = 4
    for value in rows:
        packets.append(_frame(bytes([len(value)]) + value, seq))
        seq += 1
    packets.append(_frame(_eof_packet(), seq))
    return packets


class ScriptedReader:
    """Minimal stand-in for the buffered reader returned by socket.makefile."""

    def __init__(self, sock):
        self._sock = sock
        self.closed = False

    def read(self, size):
        return self._sock.recv(size)

    def close(self):
        self.closed = True


class ScriptedSocket:
    """Socket double that serves a fixed, precomputed packet stream."""

    def __init__(self, packets):
        self.wire = b"".join(packets)
        self._buffer = io.BytesIO(self.wire)
        self.sent = bytearray()
        self.reads = 0
        self.closed = False

    def makefile(self, mode="rb"):
        return ScriptedReader(self)

    def recv(self, size):
        self.reads += 1
        return self._buffer.read(size)

    def sendall(self, data):
        self.sent.extend(data)

    def close(self):
        self.closed = True

    def settimeout(self, value):
        self.timeout = value

    def setsockopt(self, *args):
        pass

    def fileno(self):
        return -1

    @property
    def consumed(self):
        return self._buffer.tell()


class GatedSocket(ScriptedSocket):
    """Socket whose reads block once armed, until the test releases them."""

    def __init__(self, packets):
        super().__init__(packets)
        self.read_started = threading.Event()
        self.proceed = threading.Event()
        self.armed = False

    def arm(self):
        """Block on the next read, waiting for an explicit release signal."""
        self.armed = True

    def recv(self, size):
        if self.armed:
            self.read_started.set()
            if not self.proceed.wait(timeout=5):
                raise AssertionError("read stayed blocked waiting for the signal")
            # A socket closed while the read was blocked reports end of stream.
            if self.closed:
                return b""
        return super().recv(size)


_live_connections = []


def _connect(sock):
    conn = pymysql.connect(
        user="user",
        password="password",
        ssl_disabled=True,
        defer_connect=True,
    )
    conn._sock = sock
    conn._rfile = sock.makefile("rb")
    conn._next_seq_id = 0
    conn._current_timeout = None
    conn.server_status = SERVER_STATUS.SERVER_STATUS_AUTOCOMMIT
    _live_connections.append(conn)
    return conn


@pytest.fixture(autouse=True)
def _close_live_connections():
    # Neutralize any result left active by a failed assertion so that garbage
    # collection never tries to drain a scripted socket during teardown.
    yield
    while _live_connections:
        conn = _live_connections.pop()
        result = conn._result
        if result is not None:
            result.cancelled = True
        conn._force_close()
        conn._result = None


def _fetch_one(cursor, errors):
    try:
        cursor.fetchone()
    except pymysql.err.OperationalError as exc:
        errors.append(exc)


class TestSSCursorCancel:
    def test_cancel_abandons_stream_without_reading_or_notifying_server(self):
        sock = GatedSocket(_result_set_packets([b"1", b"2", b"3"]))
        conn = _connect(sock)
        cur = conn.cursor(pymysql.cursors.SSCursor)

        cur.execute("SELECT c FROM t")
        assert cur.fetchone() == (b"1",)

        # Hold back every remaining packet until the test releases the gate.
        sock.arm()

        # The next row must not arrive until an external signal is given.
        worker_error = []
        worker = threading.Thread(
            target=_fetch_one, args=(cur, worker_error), daemon=True
        )
        worker.start()
        assert sock.read_started.wait(5)

        reads_at_cancel = sock.reads
        sent_at_cancel = bytes(sock.sent)
        result = cur._result

        # Cancellation happens first and returns before input is released.
        order = []
        order.append("cancel-called")
        cur.cancel()
        order.append("cancel-returned")
        assert conn.open is False
        assert sock.closed is True
        assert conn._rfile is None
        assert result.cancelled is True
        assert result.unbuffered_active is False

        sock.proceed.set()
        worker.join(5)
        order.append("input-released")
        assert worker.is_alive() is False
        assert order == ["cancel-called", "cancel-returned", "input-released"]

        # The blocked read dies with the socket; it is not a server rollback.
        assert worker_error and worker_error[0].args[0] == CR.CR_SERVER_LOST

        # The cursor never returns to reading the abandoned result set.
        with pytest.raises(pymysql.err.OperationalError) as cm:
            cur.fetchone()
        assert cm.value.args[0] == CR.CR_FETCH_CANCELED

        # The dead connection cannot run further queries.
        with pytest.raises(pymysql.err.InterfaceError):
            conn.query("SELECT 2")

        # Repeated cancellation, cleanup and destruction read nothing new and
        # send nothing (no COM_QUIT, no rollback command).
        cur.cancel()
        cur.close()
        cur.close()
        del cur
        gc.collect()
        assert sock.reads == reads_at_cancel
        assert bytes(sock.sent) == sent_at_cancel

    def test_default_close_drains_result_and_keeps_connection_usable(self):
        first_stream = _result_set_packets([b"1", b"2"])
        sock = ScriptedSocket(first_stream + [_frame(_ok_packet(), 1)])
        conn = _connect(sock)
        cur = conn.cursor(pymysql.cursors.SSCursor)

        cur.execute("SELECT c FROM t")
        assert cur.fetchone() == (b"1",)

        cur.close()

        # Plain close consumes every packet the first query still owed.
        assert sock.consumed == len(b"".join(first_stream))
        assert conn.open is True
        assert sock.closed is False

        next_cursor = conn.cursor(pymysql.cursors.SSCursor)
        next_cursor.execute("SELECT 2")
        assert next_cursor.fetchone() is None
        assert conn.open is True
        next_cursor.close()

    def test_late_cancel_on_superseded_cursor_leaves_new_result_untouched(self):
        stream = _result_set_packets([b"a1", b"a2"]) + _result_set_packets(
            [b"b1", b"b2"]
        )
        sock = ScriptedSocket(stream)
        conn = _connect(sock)

        old_cursor = conn.cursor(pymysql.cursors.SSCursor)
        old_cursor.execute("SELECT c FROM old")
        assert old_cursor.fetchone() == (b"a1",)

        # A second cursor takes over the connection; executing its query drains
        # the leftover packets of the old unbuffered result.
        new_cursor = conn.cursor(pymysql.cursors.SSCursor)
        with pytest.warns(UserWarning, match="incomplete"):
            new_cursor.execute("SELECT c FROM new")

        old_result = old_cursor._result
        new_result = new_cursor._result
        assert old_result is not new_result
        assert conn._result is new_result
        assert new_cursor.fetchone() == (b"b1",)

        # The stale cursor can no longer cancel the result it lost ownership of.
        old_cursor.cancel()
        old_cursor.cancel()
        assert conn.open is True
        assert sock.closed is False
        assert new_result.cancelled is False
        assert old_result.cancelled is False

        # The new result set is fully readable and reports no cancellation.
        assert new_cursor.fetchone() == (b"b2",)
        assert new_cursor.fetchone() is None
        assert conn.open is True

        old_cursor.close()
        new_cursor.close()

    def test_cancel_is_noop_without_active_unbuffered_result(self):
        sock = ScriptedSocket(_result_set_packets([b"1"]))
        conn = _connect(sock)
        cur = conn.cursor(pymysql.cursors.SSCursor)

        cur.cancel()  # nothing executed yet
        assert conn.open is True

        cur.execute("SELECT c FROM t")
        assert cur.fetchone() == (b"1",)
        assert cur.fetchone() is None  # EOF reached, result finished

        cur.cancel()
        assert conn.open is True
        cur.close()
