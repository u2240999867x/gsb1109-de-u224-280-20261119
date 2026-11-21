"""Offline tests for walking multi-result packet streams with SSCursor.

A reconciliation-style response is replayed from raw protocol bytes
(details, diagnostics, summary, with OK status packets interleaved), so
no MySQL server is needed. The tests verify how far the packet stream is
actually consumed and what the cursor returns, including the case where
a server error replaces a pending result set.
"""

import io
import struct

import pytest

import pymysql
from pymysql.connections import Connection
from pymysql.constants import ER, FIELD_TYPE, SERVER_STATUS
from pymysql.converters import conversions

MORE_RESULTS = SERVER_STATUS.SERVER_MORE_RESULTS_EXISTS
AUTOCOMMIT = SERVER_STATUS.SERVER_STATUS_AUTOCOMMIT

DETAIL_MARKER_READ = "2026-01-01 00:00:00"
DETAIL_MARKER_SKIPPED = "2026-02-02 02:02:02"


def _lenenc_str(value):
    assert len(value) < 251
    return bytes([len(value)]) + value


def _frame(payload, seq_id):
    return struct.pack("<I", len(payload))[:3] + bytes([seq_id]) + payload


def _column_def(name, type_code, table="recon"):
    encoded = name.encode()
    tbl = table.encode()
    parts = (b"def", b"", tbl, tbl, encoded, encoded)
    payload = b"".join(_lenenc_str(part) for part in parts)
    return payload + struct.pack("<xHIBHBxx", 33, 255, type_code, 0, 0)


def _eof_packet(status):
    return b"\xfe" + struct.pack("<HH", 0, status)


def _ok_packet(status):
    return b"\x00\x00\x00" + struct.pack("<HH", status, 0)


def _err_packet(errno, sqlstate, message):
    return (
        b"\xff" + struct.pack("<H", errno) + b"#" + sqlstate.encode() + message.encode()
    )


def _row_packet(*values):
    return b"".join(_lenenc_str(value.encode()) for value in values)


def _assemble(packets):
    """Frame (name, payload) pairs with sequence ids starting at 1.

    Returns the stream bytes and a dict mapping each packet name to the
    stream offset right after that packet.
    """
    data = b""
    offsets = {}
    for seq_id, (name, payload) in enumerate(packets, start=1):
        data += _frame(payload, seq_id % 256)
        offsets[name] = len(data)
    return data, offsets


def _recon_packets(summary_status=AUTOCOMMIT):
    """details -> OK -> diagnostics (no rows) -> OK -> summary."""
    return [
        ("details.count", b"\x02"),
        ("details.field.id", _column_def("id", FIELD_TYPE.LONG)),
        ("details.field.marker", _column_def("marker", FIELD_TYPE.TIMESTAMP)),
        ("details.eof_fields", _eof_packet(MORE_RESULTS)),
        ("details.row1", _row_packet("1", DETAIL_MARKER_READ)),
        ("details.row2", _row_packet("2", DETAIL_MARKER_SKIPPED)),
        ("details.eof_rows", _eof_packet(MORE_RESULTS)),
        ("status.ok1", _ok_packet(MORE_RESULTS)),
        ("diag.count", b"\x01"),
        ("diag.field.diag", _column_def("diag", FIELD_TYPE.VAR_STRING)),
        ("diag.eof_fields", _eof_packet(MORE_RESULTS)),
        ("diag.eof_rows", _eof_packet(MORE_RESULTS)),
        ("status.ok2", _ok_packet(MORE_RESULTS)),
        ("summary.count", b"\x02"),
        ("summary.field.id", _column_def("id", FIELD_TYPE.LONG)),
        ("summary.field.marker", _column_def("marker", FIELD_TYPE.LONGLONG)),
        ("summary.eof_fields", _eof_packet(MORE_RESULTS)),
        ("summary.row1", _row_packet("7", "4200")),
        ("summary.eof_rows", _eof_packet(summary_status)),
    ]


def _followup_packets():
    return [
        ("followup.count", b"\x01"),
        ("followup.field.note", _column_def("note", FIELD_TYPE.VAR_STRING)),
        ("followup.eof_fields", _eof_packet(AUTOCOMMIT)),
        ("followup.row1", _row_packet("done")),
        ("followup.eof_rows", _eof_packet(AUTOCOMMIT)),
    ]


class _TrackingReader:
    """File-like reader recording how far the stream was consumed."""

    def __init__(self, data):
        self._io = io.BytesIO(data)
        self.pos = 0

    def read(self, size):
        chunk = self._io.read(size)
        self.pos += len(chunk)
        return chunk

    def close(self):
        pass


class _FakeSocket:
    def __init__(self):
        self.sent = bytearray()

    def settimeout(self, value):
        pass

    def sendall(self, data):
        self.sent += data

    def close(self):
        pass


def _offline_connection(stream, conv=None):
    conn = Connection(defer_connect=True, conv=conv)
    conn._sock = _FakeSocket()
    reader = _TrackingReader(stream)
    conn._rfile = reader
    conn._next_seq_id = 0
    conn._current_timeout = None
    conn.server_status = AUTOCOMMIT
    return conn, reader


@pytest.fixture()
def app_converter():
    """Application converter recording every value it is asked to convert."""
    seen = []

    def convert(value):
        seen.append(value)
        return f"converted({value})"

    convert.seen = seen
    return convert


@pytest.fixture(
    params=[pymysql.cursors.SSCursor, pymysql.cursors.SSDictCursor],
    ids=["ss", "ssdict"],
)
def cursor_class(request):
    return request.param


def _expected(cursor_class, row):
    if issubclass(cursor_class, pymysql.cursors.DictCursorMixin):
        return dict(zip(("id", "marker"), row))
    return row


def test_skip_to_third_column_set(cursor_class, app_converter):
    stream1, offsets = _assemble(_recon_packets())
    stream2, _ = _assemble(_followup_packets())
    conn, reader = _offline_connection(
        stream1 + stream2, conv={**conversions, FIELD_TYPE.TIMESTAMP: app_converter}
    )
    try:
        cur = conn.cursor(cursor_class)
        cur.execute("CALL reconcile()")

        # Details: first column-bearing set. Only the header is on the wire so far.
        assert [d[0] for d in cur.description] == ["id", "marker"]
        assert [d[1] for d in cur.description] == [
            FIELD_TYPE.LONG,
            FIELD_TYPE.TIMESTAMP,
        ]
        assert reader.pos == offsets["details.eof_fields"]

        row1 = cur.fetchone()
        assert row1 == _expected(cursor_class, (1, f"converted({DETAIL_MARKER_READ})"))
        assert reader.pos == offsets["details.row1"]

        # OK status packet: a result, but not a column-bearing set.
        assert cur.nextset()
        assert cur.description is None

        # Diagnostics: second column-bearing set, columns but no rows.
        assert cur.nextset()
        assert [d[0] for d in cur.description] == ["diag"]
        assert cur.fetchone() is None

        # Another OK status packet; still not counted as a column set.
        assert cur.nextset()
        assert cur.description is None

        # Summary: third column-bearing set, with its own columns and types.
        assert cur.nextset()
        assert [d[0] for d in cur.description] == ["id", "marker"]
        assert [d[1] for d in cur.description] == [
            FIELD_TYPE.LONG,
            FIELD_TYPE.LONGLONG,
        ]
        assert reader.pos == offsets["summary.eof_fields"]

        assert cur.fetchall() == [_expected(cursor_class, (7, 4200))]
        assert reader.pos == offsets["summary.eof_rows"]
        assert cur.nextset() is None

        # The skipped detail row was never converted nor materialized.
        assert app_converter.seen == [DETAIL_MARKER_READ]

        # The same connection accepts the next normal query.
        cur.close()
        cur2 = conn.cursor()
        cur2.execute("SELECT 'done'")
        assert cur2.fetchall() == (("done",),)
        assert reader.pos == len(stream1 + stream2)
        cur2.close()
    finally:
        conn.close()


def test_server_error_before_summary(cursor_class, app_converter):
    packets = _recon_packets()[:7]  # details only, promising more results
    packets.append(
        (
            "diag.error",
            _err_packet(ER.NO_SUCH_TABLE, "42S02", "Table 'recon.diag' doesn't exist"),
        )
    )
    stream, offsets = _assemble(packets)
    conn, reader = _offline_connection(
        stream, conv={**conversions, FIELD_TYPE.TIMESTAMP: app_converter}
    )
    try:
        cur = conn.cursor(cursor_class)
        cur.execute("CALL reconcile()")

        # One detail row is read successfully before the error surfaces.
        row1 = cur.fetchone()
        assert row1 == _expected(cursor_class, (1, f"converted({DETAIL_MARKER_READ})"))
        assert reader.pos == offsets["details.row1"]

        with pytest.raises(pymysql.ProgrammingError) as exc_info:
            cur.nextset()
        assert exc_info.value.args[0] == ER.NO_SUCH_TABLE
        assert exc_info.value.args[1] == "Table 'recon.diag' doesn't exist"
        assert exc_info.value.sqlstate == "42S02"

        # The caller can tell the summary was never reached.
        assert cur.description is None
        assert cur.nextset() is None

        assert app_converter.seen == [DETAIL_MARKER_READ]
        assert reader.pos == len(stream)
        cur.close()
    finally:
        conn.close()
