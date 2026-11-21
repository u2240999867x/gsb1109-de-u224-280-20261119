"""Offline regression tests for ``Connection.ping()`` reconnect semantics.

The collection service borrows connections through PyMySQL's deprecated
``ping(reconnect=True)`` entry point.  These tests pin down which physical
session a borrower actually gets back and how each session is initialized,
using a small scripted MySQL server (fake sockets) instead of a real database.

The scripted server records, for every physical session, the real commands it
received and the answers it sent, and it injects failures at the exact command
where a scenario asks for them.
"""

import errno
import struct
from unittest import mock

import pytest

import pymysql
from pymysql.constants import CLIENT, COMMAND, CR, SERVER_STATUS
from pymysql.err import OperationalError, ProgrammingError

SERVER_CAPABILITIES = (
    CLIENT.LONG_PASSWORD
    | CLIENT.LONG_FLAG
    | CLIENT.PROTOCOL_41
    | CLIENT.TRANSACTIONS
    | CLIENT.SECURE_CONNECTION
    | CLIENT.MULTI_RESULTS
    | CLIENT.PLUGIN_AUTH
    | CLIENT.PLUGIN_AUTH_LENENC_CLIENT_DATA
    | CLIENT.CONNECT_ATTRS
)

CHARSET = "utf8mb4"
COLLATION = "utf8mb4_general_ci"
INIT_COMMAND = 'SET time_zone = "+00:00"'

SET_NAMES_INIT = f"SET NAMES {CHARSET} COLLATE {COLLATION}".encode()
SET_NAMES_LATIN1 = b"SET NAMES latin1 COLLATE latin1_general_ci"
SET_TIMEZONE_INIT = b'SET time_zone = "+00:00"'
SET_TIMEZONE_TOKYO = b'SET time_zone = "+09:00"'
SET_AUTOCOMMIT_OFF = b"SET AUTOCOMMIT = 0"
BEGIN = b"BEGIN"

# Scripted answers.
OK = "ok"
ERROR = "error"
TRANSPORT = "transport"


def mysql_packet(payload, seq):
    return struct.pack("<I", len(payload))[:3] + bytes([seq]) + payload


def handshake_packet(thread_id, server_status):
    # Protocol::HandshakeV10.
    payload = (
        b"\x0a"
        + b"8.0.36-scripted\0"
        + struct.pack("<I", thread_id)
        + b"01234567"
        + b"\x00"
        + struct.pack("<H", SERVER_CAPABILITIES & 0xFFFF)
        + struct.pack(
            "<BBHB",
            45,  # utf8mb4_general_ci
            server_status,
            SERVER_CAPABILITIES >> 16,
            21,
        )
        + b"\x00" * 10
        + b"89abcdefghijkl"
        + b"\x00"
        + b"mysql_native_password\0"
    )
    return mysql_packet(payload, 0)


def ok_packet(seq, server_status):
    # 0x00, affected_rows=0, insert_id=0, server_status, warning_count.
    payload = b"\x00\x00\x00" + struct.pack("<HH", server_status, 0)
    return mysql_packet(payload, seq)


def error_packet(seq):
    payload = b"\xff" + struct.pack("<H", 1064) + b"#HY000scripted init failure"
    return mysql_packet(payload, seq)


def step(command, sql=None, answer=OK):
    return command, sql, answer


def initialization_steps():
    """Commands a fully initialized session must receive, in order."""
    return [
        step(COMMAND.COM_QUERY, SET_NAMES_INIT),
        step(COMMAND.COM_QUERY, SET_TIMEZONE_INIT),
        step(COMMAND.COM_QUERY, SET_AUTOCOMMIT_OFF),
    ]


class ScriptedSession:
    """One physical MySQL connection behind a fake socket.

    ``script`` is the ordered list of command-phase commands the session
    expects.  The handshake / authentication exchange always succeeds.
    """

    def __init__(self, session_id, script, initial_status):
        self.session_id = session_id
        self.script = list(script)
        self.server_status = initial_status
        # Records (command, sql, answer) for every command actually received.
        self.commands = []
        self.closed = False
        self.failure_step = None
        self.failure_index = None
        self._authenticated = False
        self._buffer = bytearray()
        self._enqueue(handshake_packet(session_id, initial_status))

    # Socket-like interface used by Connection.connect().
    def settimeout(self, timeout):
        pass

    def setsockopt(self, level, option, value=None):
        pass

    def makefile(self, mode):
        return self

    def fileno(self):
        return -self.session_id

    def close(self):
        self.closed = True

    def read(self, num_bytes):
        if not self._buffer:
            # Simulate the server dropping the connection.
            raise ConnectionResetError(errno.ECONNRESET, "scripted server gone away")
        chunk = bytes(self._buffer[:num_bytes])
        del self._buffer[:num_bytes]
        return chunk

    def sendall(self, data):
        if not self._authenticated:
            # First packet is the client's handshake response.
            self._authenticated = True
            self._enqueue(ok_packet(2, self.server_status))
            return
        # Connection._execute_command() writes a 4-byte little-endian payload
        # length followed by the command byte; the zero high byte doubles as
        # sequence id 0 on the wire.
        payload_length = struct.unpack("<I", data[:3] + b"\x00")[0]
        command = data[4]
        sql = data[5 : 4 + payload_length]
        if command == COMMAND.COM_QUIT:
            # COM_QUIT is not part of a session's command script.
            self.commands.append((command, b"", "quit"))
            self.closed = True
            return
        self._handle_command(command, sql)

    def _handle_command(self, command, sql):
        if not self.script:
            raise AssertionError(
                f"session {self.session_id}: unexpected command 0x{command:02x} {sql!r}"
            )
        expected_command, expected_sql, answer = self.script.pop(0)
        assert command == expected_command, (
            f"session {self.session_id}: expected command 0x{expected_command:02x}, "
            f"got 0x{command:02x} {sql!r}"
        )
        if expected_sql is not None:
            assert sql == expected_sql, (
                f"session {self.session_id}: expected {expected_sql!r}, got {sql!r}"
            )

        self.commands.append((command, sql, answer))
        if answer == OK:
            self._apply_server_status(sql)
            self._enqueue(ok_packet(1, self.server_status))
        elif answer == ERROR:
            self.failure_step = (command, sql)
            self.failure_index = len(self.commands)
            self._enqueue(error_packet(1))
        else:
            assert answer == TRANSPORT
            self.failure_step = (command, sql)
            self.failure_index = len(self.commands)
            self.closed = True
            # Fail while the command is being written, like a broken pipe.
            raise ConnectionResetError(errno.ECONNRESET, "scripted server gone away")

    def _apply_server_status(self, sql):
        if sql == BEGIN:
            self.server_status |= SERVER_STATUS.SERVER_STATUS_IN_TRANS
        elif sql == SET_AUTOCOMMIT_OFF:
            self.server_status &= ~SERVER_STATUS.SERVER_STATUS_AUTOCOMMIT

    def _enqueue(self, packet):
        self._buffer.extend(packet)

    def received_sql(self):
        return [sql for _, sql, _ in self.commands]

    def received_commands(self):
        return [command for command, _, _ in self.commands]


class ScriptedServer:
    """Hands out a fresh ScriptedSession for every connect() attempt."""

    def __init__(self, session_scripts, initial_statuses=None):
        self._scripts = list(session_scripts)
        self._initial_statuses = initial_statuses or {}
        self.sessions = []

    def create_connection(self, address, timeout=None, **kwargs):
        session_id = len(self.sessions) + 1
        script = self._scripts.pop(0)
        status = self._initial_statuses.get(
            session_id, SERVER_STATUS.SERVER_STATUS_AUTOCOMMIT
        )
        session = ScriptedSession(session_id, script, status)
        self.sessions.append(session)
        return session


def make_connection(**kwargs):
    params = dict(
        user="collector",
        password="secret",
        host="127.0.0.1",
        port=3306,
        charset=CHARSET,
        collation=COLLATION,
        init_command=INIT_COMMAND,
        autocommit=False,
        ssl_disabled=True,
        defer_connect=True,
    )
    params.update(kwargs)
    return pymysql.connect(**params)


def patch_socket(server):
    return mock.patch.object(
        pymysql.connections.socket, "create_connection", server.create_connection
    )


def test_ping_reconnect_borrows_only_fully_initialized_session():
    # Session 1: fully initialized, then the borrower changes charset/collation
    # and timezone through ordinary calls and opens an uncommitted transaction.
    session1_script = initialization_steps() + [
        step(COMMAND.COM_QUERY, SET_NAMES_LATIN1),
        step(COMMAND.COM_QUERY, SET_TIMEZONE_TOKYO),
        step(COMMAND.COM_QUERY, BEGIN),
        step(COMMAND.COM_PING, b"", TRANSPORT),
    ]
    # Session 2: handshake and SET NAMES (using the charset the borrower
    # switched to) succeed; the timezone initialization statement then fails
    # with a server error, so initialization never completes.
    session2_script = [
        step(COMMAND.COM_QUERY, SET_NAMES_LATIN1),
        step(COMMAND.COM_QUERY, SET_TIMEZONE_INIT, ERROR),
    ]
    # Session 3: a healthy replacement session initialized with the
    # borrower's charset/collation and the original initialization settings.
    session3_script = [
        step(COMMAND.COM_QUERY, SET_NAMES_LATIN1),
        step(COMMAND.COM_QUERY, SET_TIMEZONE_INIT),
        step(COMMAND.COM_QUERY, SET_AUTOCOMMIT_OFF),
        step(COMMAND.COM_PING, b""),
    ]

    server = ScriptedServer([session1_script, session2_script, session3_script])
    conn = make_connection()

    with patch_socket(server):
        conn.connect()
        session1 = server.sessions[0]
        assert conn._sock is session1

        # Borrower-side state changes on session 1.
        conn.set_character_set("latin1", "latin1_general_ci")
        conn.query('SET time_zone = "+09:00"')
        conn.begin()
        assert conn.get_autocommit() is False
        assert conn.server_status & SERVER_STATUS.SERVER_STATUS_IN_TRANS

        # The ping command is lost in transport (write failure).  Inside
        # ping(reconnect=True) that error triggers an immediate reconnect,
        # which produces session 2; its timezone initialization statement
        # fails with a server error, so that error is what the borrower sees.
        with pytest.warns(DeprecationWarning):
            with pytest.raises(ProgrammingError) as init_failure:
                conn.ping(reconnect=True)
        assert init_failure.value.args[0] == 1064
        assert not conn.open

        # Session 1 died while the ping was being sent and was force-closed.
        assert session1.failure_step == (COMMAND.COM_PING, b"")
        assert session1.failure_index == 7
        assert session1.commands[-1][2] == TRANSPORT
        assert session1.closed
        assert COMMAND.COM_QUIT not in session1.received_commands()
        assert session1.received_sql() == [
            SET_NAMES_INIT,
            SET_TIMEZONE_INIT,
            SET_AUTOCOMMIT_OFF,
            SET_NAMES_LATIN1,
            SET_TIMEZONE_TOKYO,
            BEGIN,
            b"",
        ]

        # Session 2 failed during initialization and is closed.  It never
        # receives the autocommit setup, a resent ping, or collection work.
        session2 = server.sessions[1]
        assert session2.failure_step == (COMMAND.COM_QUERY, SET_TIMEZONE_INIT)
        assert session2.failure_index == 2
        assert session2.closed
        assert session2.received_sql() == [SET_NAMES_LATIN1, SET_TIMEZONE_INIT]
        assert COMMAND.COM_PING not in session2.received_commands()
        assert COMMAND.COM_QUIT not in session2.received_commands()

        # The borrower retries with reconnect allowed and gets session 3.
        with pytest.warns(DeprecationWarning):
            conn.ping(reconnect=True)
        session3 = server.sessions[2]

    assert conn.open
    # A bare COM_PING OK is not enough: the borrower must hold session 3...
    assert conn._sock is session3
    # ...and session 3 must have run the complete initialization sequence
    # with the borrower's charset/collation before the final ping.
    assert session3.received_commands() == [
        COMMAND.COM_QUERY,
        COMMAND.COM_QUERY,
        COMMAND.COM_QUERY,
        COMMAND.COM_PING,
    ]
    assert session3.received_sql() == [
        SET_NAMES_LATIN1,
        SET_TIMEZONE_INIT,
        SET_AUTOCOMMIT_OFF,
        b"",
    ]
    assert session3.failure_step is None

    # State visible to the borrower: the new charset/collation, the original
    # initialization timezone (not the Tokyo override), and autocommit off.
    assert conn.charset == "latin1"
    assert conn.collation == "latin1_general_ci"
    assert conn.encoding == "cp1252"
    assert conn.autocommit_mode is False
    assert conn.get_autocommit() is False
    assert conn.init_command == INIT_COMMAND

    # The old timezone override and the uncommitted transaction are never
    # replayed against the new session, nor is the original charset.
    session3_sql = session3.received_sql()
    assert SET_TIMEZONE_TOKYO not in session3_sql
    assert BEGIN not in session3_sql
    assert SET_NAMES_INIT not in session3_sql

    conn.close()


def test_ping_transport_error_without_reconnect_does_not_reinitialize():
    script = initialization_steps() + [step(COMMAND.COM_PING, b"", TRANSPORT)]
    server = ScriptedServer([script])
    conn = make_connection()

    with patch_socket(server):
        conn.connect()
        with pytest.raises(OperationalError) as lost:
            conn.ping(reconnect=False)
        assert lost.value.args[0] == CR.CR_SERVER_GONE_ERROR

        assert len(server.sessions) == 1
        session = server.sessions[0]
        assert not conn.open
        assert conn._sock is None
        assert session.failure_step == (COMMAND.COM_PING, b"")
        assert session.closed
        assert session.received_sql() == [
            SET_NAMES_INIT,
            SET_TIMEZONE_INIT,
            SET_AUTOCOMMIT_OFF,
            b"",
        ]
        assert COMMAND.COM_QUIT not in session.received_commands()


def test_healthy_ping_keeps_single_session():
    script = initialization_steps() + [step(COMMAND.COM_PING, b"")]
    server = ScriptedServer([script])
    conn = make_connection()

    with patch_socket(server):
        conn.connect()
        assert conn.ping(reconnect=False) is None

        assert len(server.sessions) == 1
        session = server.sessions[0]
        assert conn.open
        assert conn._sock is session
        assert session.failure_step is None
        assert session.received_commands() == [
            COMMAND.COM_QUERY,
            COMMAND.COM_QUERY,
            COMMAND.COM_QUERY,
            COMMAND.COM_PING,
        ]

    conn.close()


def test_transport_failure_reconnects_and_repings_on_healthy_session():
    # First ping fails in transport with reconnect enabled; the replacement
    # session initializes cleanly and must still receive the completion ping.
    session1_script = initialization_steps() + [step(COMMAND.COM_PING, b"", TRANSPORT)]
    session2_script = initialization_steps() + [step(COMMAND.COM_PING, b"")]
    server = ScriptedServer([session1_script, session2_script])
    conn = make_connection()

    with patch_socket(server):
        conn.connect()
        session1 = server.sessions[0]

        with pytest.warns(DeprecationWarning):
            assert conn.ping(reconnect=True) is None

        assert len(server.sessions) == 2
        session2 = server.sessions[1]
        assert conn.open
        assert conn._sock is session2
        assert session1.closed
        assert session2.failure_step is None
        assert session2.script == []
        assert session2.received_commands() == [
            COMMAND.COM_QUERY,
            COMMAND.COM_QUERY,
            COMMAND.COM_QUERY,
            COMMAND.COM_PING,
        ]
        assert session2.received_sql() == [
            SET_NAMES_INIT,
            SET_TIMEZONE_INIT,
            SET_AUTOCOMMIT_OFF,
            b"",
        ]

    conn.close()


def test_reconnect_skips_redundant_autocommit_when_already_disabled():
    # The server already reports autocommit disabled during the handshake, so
    # the driver is allowed to skip the redundant SET AUTOCOMMIT statement.
    script = [
        step(COMMAND.COM_QUERY, SET_NAMES_INIT),
        step(COMMAND.COM_QUERY, SET_TIMEZONE_INIT),
        step(COMMAND.COM_PING, b""),
    ]
    server = ScriptedServer([script], initial_statuses={1: 0})
    conn = make_connection()

    with patch_socket(server):
        conn.connect()
        assert conn.ping(reconnect=False) is None

        session = server.sessions[0]
        assert conn.open
        assert conn.autocommit_mode is False
        assert conn.get_autocommit() is False
        assert session.received_sql() == [SET_NAMES_INIT, SET_TIMEZONE_INIT, b""]
        assert SET_AUTOCOMMIT_OFF not in session.received_sql()

    conn.close()
