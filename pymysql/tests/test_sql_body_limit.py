"""Offline tests for the hard SQL body byte limit and executemany batching.

These tests do not need a MySQL server.  A deferred connection is driven with
canned OK packets, and the bytes handed to ``Connection._write_bytes`` are
captured so every assertion is made on the actual wire payload.
"""

import struct
from unittest import mock

import pytest

import pymysql
from pymysql.constants import COMMAND, SERVER_STATUS
from pymysql.protocol import MysqlPacket

QUERY = "INSERT INTO t (data) VALUES (%s) ON DUPLICATE KEY UPDATE c=VALUES(c)"
PREFIX = b"INSERT INTO t (data) VALUES "
POSTFIX = b" ON DUPLICATE KEY UPDATE c=VALUES(c)"

# Rows deliberately cover emoji (multibyte), single quotes, double quotes and
# backslashes so the two escape modes produce different byte sequences.
ROWS = [
    "\U0001f600",  # 0: emoji, four raw bytes, no escaping needed
    'a"b\\c',  # 1: double quote + backslash
    "q'r\\s" + "Z" * 30,  # 2: single row that cannot fit under the hard limit
    "x\\y\\z",  # 3: backslashes only
    "p'q'r",  # 4: single quotes only
]
ARGS = [(row,) for row in ROWS]

# Rows 0 and 1 together total exactly this many bytes in normal
# backslash-escaping mode, while row 2 alone needs more than this in any mode.
HARD_LIMIT = 84


def _normal_literal(value):
    # Independent oracle for the backslash-escaping rules used by the driver.
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("'", "\\'")
    return b"(" + ("'" + escaped + "'").encode() + b")"


def _nbs_literal(value):
    # NO_BACKSLASH_ESCAPES: only single quotes are doubled.
    return b"(" + ("'" + value.replace("'", "''") + "'").encode() + b")"


LITERALS = {
    "normal": [_normal_literal(row) for row in ROWS],
    "nbs": [_nbs_literal(row) for row in ROWS],
}


def _batch(mode, *indexes):
    return PREFIX + b",".join(LITERALS[mode][i] for i in indexes) + POSTFIX


def _ok_packet(encoding, server_status):
    # 0x00 OK header, affected_rows=1, last_insert_id=0, then status flags.
    data = b"\x00\x01\x00" + struct.pack("<HH", server_status, 0)
    return MysqlPacket(data, encoding)


class FakeConnection:
    """Build and instrument a Connection without opening any socket."""

    def __init__(self, max_sql_body_length, initial_status=0, response_status=0):
        self.conn = pymysql.connect(
            user="proxy-user",
            password="",
            ssl_disabled=True,
            defer_connect=True,
            max_sql_body_length=max_sql_body_length,
        )
        self.conn._sock = object()
        self.conn.server_status = initial_status
        self.response_status = response_status
        self.sent_bodies = []

        def fake_write_bytes(data):
            # COM_QUERY writes one packet here: a 5 byte prelude (4 byte
            # length field + 1 command byte) followed by the SQL body.
            packet_size, command = struct.unpack("<iB", data[:5])
            assert command == COMMAND.COM_QUERY
            body = bytes(data[5:])
            assert packet_size == len(body) + 1
            self.sent_bodies.append(body)

        def fake_read_packet(packet_type=MysqlPacket):
            return _ok_packet(self.conn.encoding, self.response_status)

        self._write_patch = mock.patch.object(
            self.conn, "_write_bytes", side_effect=fake_write_bytes
        )
        self._read_patch = mock.patch.object(
            self.conn, "_read_packet", side_effect=fake_read_packet
        )

    def __enter__(self):
        self._write_patch.start()
        self._read_patch.start()
        return self

    def __exit__(self, *exc_info):
        self._write_patch.stop()
        self._read_patch.stop()


def _single_statement_length(cursor, row):
    value = cursor._mogrify("(%s)", (row,)).encode(cursor.connection.encoding)
    return len(PREFIX) + len(value) + len(POSTFIX)


def test_hard_limit_parameter_validation():
    with pytest.raises(ValueError):
        pymysql.connect(defer_connect=True, max_sql_body_length=0)
    with pytest.raises(ValueError):
        pymysql.connect(defer_connect=True, max_sql_body_length=-1)
    with pytest.raises(ValueError):
        pymysql.connect(defer_connect=True, max_sql_body_length="84")
    with pytest.raises(ValueError):
        pymysql.connect(defer_connect=True, max_sql_body_length=True)
    # None keeps the historical, unlimited behavior.
    conn = pymysql.connect(defer_connect=True, max_sql_body_length=None)
    assert conn.max_sql_body_length is None


def test_connection_rejects_oversized_statement_before_writing():
    statement = b"SELECT '1234567890123'"
    fake = FakeConnection(max_sql_body_length=10)
    with fake:
        with pytest.raises(pymysql.StatementTooLargeError) as cm:
            fake.conn.query(statement.decode())

    error = cm.value
    assert isinstance(error, pymysql.InterfaceError)
    assert error.limit == 10
    assert error.errors == [(None, len(statement))]
    assert fake.sent_bodies == []  # nothing must reach the socket


@pytest.mark.parametrize(
    ("policy", "initial_status", "response_status", "modes", "required"),
    [
        # First batch is escaped normally; its OK packet flips the session to
        # NO_BACKSLASH_ESCAPES for every later command.
        (
            "switch",
            0,
            SERVER_STATUS.SERVER_STATUS_NO_BACKSLASH_ESCAPES,
            ("normal", "nbs", "nbs"),
            104,
        ),
        # Control: escape mode never changes.
        ("never", 0, 0, ("normal", "normal", "normal"), 105),
        # Control: NO_BACKSLASH_ESCAPES is active from the first command.
        (
            "nbs",
            SERVER_STATUS.SERVER_STATUS_NO_BACKSLASH_ESCAPES,
            SERVER_STATUS.SERVER_STATUS_NO_BACKSLASH_ESCAPES,
            ("nbs", "nbs", "nbs"),
            104,
        ),
    ],
    ids=["switch-after-first-batch", "never-switch", "no-backslash-from-start"],
)
def test_executemany_hard_limit(
    policy, initial_status, response_status, modes, required
):
    fake = FakeConnection(
        max_sql_body_length=HARD_LIMIT,
        initial_status=initial_status,
        response_status=response_status,
    )
    with fake:
        conn = fake.conn
        cursor = conn.cursor()

        # Candidate lengths computed before any batch is confirmed.  They must
        # never be reused as text or as evidence for a statement prepared
        # later, when the session may have switched escape mode.
        stale_lengths = [_single_statement_length(cursor, row) for row in ROWS]

        with pytest.raises(pymysql.StatementTooLargeError) as cm:
            cursor.executemany(QUERY, ARGS)

    error = cm.value
    assert error.limit == HARD_LIMIT
    assert error.errors == [(2, required)]
    if policy == "switch":
        # Row 2 was first mogrified under backslash mode (105 bytes); the
        # reported evidence must come from the post-switch session (104).
        assert stale_lengths[2] == 105
        assert required == 104

    first_mode, third_mode, fourth_mode = modes
    expected_bodies = [
        _batch(first_mode, 0, 1),  # rows 0 and 1, confirmed first
        _batch(third_mode, 3),  # legal row after the bad one
        _batch(fourth_mode, 4),  # last legal row, still in original order
    ]
    bodies = fake.sent_bodies

    # Exact bytes per command: total length and original value content.
    assert bodies == expected_bodies
    # The first two rows hit the cap exactly under backslash escaping; the
    # same pair is shorter once backslashes are no longer escaped.
    assert len(bodies[0]) == (82 if policy == "nbs" else HARD_LIMIT)
    for body in bodies:
        assert body.startswith(PREFIX)
        assert body.endswith(POSTFIX)
        assert b"Z" * 31 not in body  # oversized row 2 never reaches the wire

    # The bad row hides neither the already confirmed first batch nor the
    # legal rows that follow it.
    joined = b"".join(bodies)
    assert joined.index(b"\xf0\x9f\x98\x80") < joined.index(b"x") < joined.index(b"p")
    assert cursor.rowcount == len(bodies)


def test_executemany_without_hard_limit_keeps_soft_batching():
    fake = FakeConnection(max_sql_body_length=None)
    with fake:
        conn = fake.conn
        cursor = conn.cursor()
        cursor.max_stmt_length = 90  # force several soft batches
        returned = cursor.executemany(QUERY, ARGS)

    # Every original row, including the would-be oversized one, is written.
    joined = b"".join(fake.sent_bodies)
    for marker in (b"\xf0\x9f\x98\x80", b"Z" * 30, b"x", b"p"):
        assert marker in joined
    assert len(fake.sent_bodies) > 1
    assert returned == len(fake.sent_bodies)


def test_executemany_without_hard_limit_single_batch_by_default():
    fake = FakeConnection(max_sql_body_length=None)
    with fake:
        cursor = fake.conn.cursor()
        cursor.executemany(QUERY, ARGS)

    assert len(fake.sent_bodies) == 1
    body = fake.sent_bodies[0]
    assert body == _batch("normal", 0, 1, 2, 3, 4)
    assert body.count(b",(") == len(ROWS) - 1
