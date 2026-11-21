import struct
import unittest

from pymysql import converters, err
from pymysql.connections import Connection
from pymysql.constants import COMMAND, FIELD_TYPE, SERVER_STATUS
from pymysql.cursors import Cursor, SSCursor

MORE_RESULTS = SERVER_STATUS.SERVER_MORE_RESULTS_EXISTS


def lenenc_int(n):
    if n < 251:
        return bytes([n])
    if n < 65536:
        return b"\xfc" + struct.pack("<H", n)
    if n < 16777216:
        return b"\xfd" + struct.pack("<I", n)[:3]
    return b"\xfe" + struct.pack("<Q", n)


def lenenc_str(data):
    return lenenc_int(len(data)) + data


def packet(payload, seq_id):
    return struct.pack("<I", len(payload))[:3] + bytes([seq_id]) + payload


def ok_payload(server_status=0, affected_rows=0):
    return (
        b"\x00"
        + lenenc_int(affected_rows)
        + lenenc_int(0)
        + struct.pack("<HH", server_status, 0)
    )


def eof_payload(server_status=0):
    return b"\xfe" + struct.pack("<HH", 0, server_status)


def err_payload(errno, message):
    return b"\xff" + struct.pack("<H", errno) + b"#HY000" + message.encode("utf-8")


def field_payload(name, type_code, charsetnr=45, length=1024):
    payload = b"".join(
        lenenc_str(part) for part in (b"def", b"", b"", b"", name.encode("utf-8"), b"")
    )
    payload += b"\x0c"
    payload += struct.pack("<HIBHB", charsetnr, length, type_code, 0, 0)
    payload += b"\x00\x00"
    return payload


def row_payload(values):
    payload = b""
    for value in values:
        if value is None:
            payload += b"\xfb"
        else:
            if not isinstance(value, bytes):
                value = str(value).encode("utf-8")
            payload += lenenc_str(value)
    return payload


def resultset_payloads(columns, rows, server_status=0):
    """Payloads for a full result set: field count, field descriptors,
    EOF, row packets, terminating EOF."""
    payloads = [lenenc_int(len(columns))]
    for name, type_code in columns:
        payloads.append(field_payload(name, type_code))
    payloads.append(eof_payload())
    for row in rows:
        payloads.append(row_payload(row))
    payloads.append(eof_payload(server_status))
    return payloads


class FakeServer:
    """A scripted MySQL server speaking just enough of the wire protocol.

    Responses are queued per client query with add_response(). Every
    COM_QUERY sent by the connection is recorded in .queries so tests
    can verify what was sent and when.
    """

    def __init__(self):
        self.queries = []
        self._responses = []
        self._read_buffer = bytearray()
        self.closed = False

    def add_response(self, *payloads):
        self._responses.append(list(payloads))

    # -- socket interface (used as Connection._sock) --

    def settimeout(self, value):
        pass

    def sendall(self, data):
        length = struct.unpack("<I", data[:4])[0] & 0xFFFFFF
        command = data[4]
        sql = data[5 : 4 + length]
        assert command == COMMAND.COM_QUERY, f"unexpected command {command}"
        self.queries.append(sql.decode("utf-8"))
        if not self._responses:
            raise AssertionError(f"unexpected query from client: {sql!r}")
        for seq_id, payload in enumerate(self._responses.pop(0), start=1):
            self._read_buffer += packet(payload, seq_id)

    def close(self):
        self.closed = True

    # -- file interface (used as Connection._rfile) --

    def read(self, size):
        chunk = bytes(self._read_buffer[:size])
        del self._read_buffer[:size]
        return chunk


def make_connection(server, cursorclass=Cursor):
    """Build a Connection talking to the scripted FakeServer."""
    conn = object.__new__(Connection)
    conn._sock = server
    conn._rfile = server
    conn._next_seq_id = 0
    conn._current_timeout = None
    conn._read_timeout = None
    conn._write_timeout = None
    conn.charset = "utf8mb4"
    conn.collation = None
    conn.use_unicode = True
    conn.encoding = "utf8"
    conn.encoders = {
        k: v for k, v in converters.conversions.items() if type(k) is not int
    }
    conn.decoders = {k: v for k, v in converters.conversions.items() if type(k) is int}
    conn.cursorclass = cursorclass
    conn._result = None
    conn._affected_rows = 0
    conn.server_status = 0
    return conn


DETAIL_COLUMNS = [("detail_id", FIELD_TYPE.LONGLONG)]
EMPTY_COLUMNS = [("label", FIELD_TYPE.VAR_STRING)]
OUT_COLUMNS = [
    ("@`_p_0`", FIELD_TYPE.LONGLONG),
    ("@`_p_1`", FIELD_TYPE.VAR_STRING),
]

SET_QUERY = "SET @`_p_0`=1,@`_p_1`='x'"
CALL_QUERY = "CALL `p`(@`_p_0`,@`_p_1`)"
SELECT_QUERY = "SELECT @`_p_0`,@`_p_1`"


def queue_proc_round(server, out_row, detail_rows=((1,), (2,), (3,))):
    """Queue the server responses for one callproc("p", ...) round with
    two arguments: the SET, the CALL (a streaming detail result set, a
    result set with columns but no rows, and the CALL's own terminating
    OK), and the SELECT reading the OUT/INOUT session variables."""
    server.add_response(ok_payload())
    server.add_response(
        *resultset_payloads(DETAIL_COLUMNS, detail_rows, server_status=MORE_RESULTS),
        *resultset_payloads(EMPTY_COLUMNS, (), server_status=MORE_RESULTS),
        ok_payload(),
    )
    server.add_response(*resultset_payloads(OUT_COLUMNS, (out_row,)))


class TestCallProcOutputs(unittest.TestCase):
    def test_outputs_require_all_result_sets_consumed(self):
        server = FakeServer()
        queue_proc_round(server, (111, "abc"))
        conn = make_connection(server)
        cur = conn.cursor()

        # callproc keeps its original return convention: the args themselves.
        args = (1, "x")
        self.assertIs(cur.callproc("p", args), args)

        # Only SET and CALL have been sent so far; the OUT/INOUT variables
        # must not be read back yet.
        self.assertEqual(server.queries, [SET_QUERY, CALL_QUERY])

        # The detail rows of the first result set are visible.
        self.assertEqual(cur.fetchall(), ((1,), (2,), (3,)))

        # Trying to read the outputs now must clearly fail, and must not
        # silently consume the remaining result sets.
        with self.assertRaises(err.ProgrammingError):
            cur.get_proc_outputs()
        self.assertEqual(server.queries, [SET_QUERY, CALL_QUERY])

        # The remaining result sets are still there: one with columns but
        # no rows, then the CALL's own empty status.
        self.assertTrue(cur.nextset())
        self.assertIsNotNone(cur.description)
        self.assertEqual(cur.fetchall(), ())
        self.assertTrue(cur.nextset())
        self.assertIsNone(cur.nextset())

        # Now the outputs can be read back; exactly one SELECT is sent.
        self.assertEqual(cur.get_proc_outputs(), (111, "abc"))
        self.assertEqual(server.queries, [SET_QUERY, CALL_QUERY, SELECT_QUERY])

        # Re-reading is served from the cache: no further query is sent.
        self.assertEqual(cur.get_proc_outputs(), (111, "abc"))
        self.assertEqual(server.queries, [SET_QUERY, CALL_QUERY, SELECT_QUERY])

    def test_second_call_same_connection(self):
        server = FakeServer()
        queue_proc_round(server, (111, "abc"))
        queue_proc_round(server, (222, "def"), detail_rows=((9,),))
        conn = make_connection(server)
        cur = conn.cursor()

        cur.callproc("p", (1, "x"))
        while cur.nextset():
            pass
        first_outputs = cur.get_proc_outputs()
        self.assertEqual(first_outputs, (111, "abc"))

        # Second round on the same connection, same parameter positions.
        cur.callproc("p", (1, "x"))
        self.assertEqual(server.queries[-2:], [SET_QUERY, CALL_QUERY])

        # Until the second round is fully consumed, its outputs are not
        # available; the first round's values must not leak into it.
        with self.assertRaises(err.ProgrammingError):
            cur.get_proc_outputs()
        self.assertNotIn(SELECT_QUERY, server.queries[3:])

        self.assertEqual(cur.fetchall(), ((9,),))
        with self.assertRaises(err.ProgrammingError):
            cur.get_proc_outputs()
        self.assertTrue(cur.nextset())
        self.assertTrue(cur.nextset())
        self.assertIsNone(cur.nextset())

        self.assertEqual(cur.get_proc_outputs(), (222, "def"))

        # The outputs delivered for the first round are unchanged.
        self.assertEqual(first_outputs, (111, "abc"))

    def test_failed_call_delivers_no_outputs(self):
        server = FakeServer()
        server.add_response(ok_payload())  # SET
        # CALL: one detail result set, then a server error at the tail
        # of the results instead of the remaining result sets.
        server.add_response(
            *resultset_payloads(
                DETAIL_COLUMNS, ((1,), (2,)), server_status=MORE_RESULTS
            ),
            err_payload(3999, "simulated procedure failure"),
        )
        # The session variables would be queryable, but this round must
        # never ask for them.
        server.add_response(*resultset_payloads(OUT_COLUMNS, ((999, "zzz"),)))

        conn = make_connection(server)
        cur = conn.cursor()
        cur.callproc("p", (1, "x"))

        self.assertEqual(cur.fetchall(), ((1,), (2,)))
        with self.assertRaises(err.OperationalError):
            cur.nextset()

        with self.assertRaises(err.ProgrammingError):
            cur.get_proc_outputs()
        self.assertEqual(server.queries, [SET_QUERY, CALL_QUERY])

    def test_unbuffered_cursor(self):
        server = FakeServer()
        queue_proc_round(server, (111, "abc"))
        conn = make_connection(server, cursorclass=SSCursor)
        cur = conn.cursor()

        cur.callproc("p", (1, "x"))
        self.assertEqual(server.queries, [SET_QUERY, CALL_QUERY])

        # Streaming: only the first detail row has been read.
        self.assertEqual(cur.fetchone(), (1,))

        # Outputs are not available while details are still streaming,
        # and the attempt must not consume them.
        with self.assertRaises(err.ProgrammingError):
            cur.get_proc_outputs()
        self.assertEqual(server.queries, [SET_QUERY, CALL_QUERY])
        self.assertEqual(cur.fetchall(), [(2,), (3,)])

        self.assertTrue(cur.nextset())
        self.assertEqual(cur.fetchall(), [])
        self.assertTrue(cur.nextset())
        self.assertIsNone(cur.nextset())

        self.assertEqual(cur.get_proc_outputs(), (111, "abc"))
        self.assertEqual(server.queries, [SET_QUERY, CALL_QUERY, SELECT_QUERY])

    def test_outputs_without_callproc_and_without_args(self):
        server = FakeServer()
        conn = make_connection(server)
        cur = conn.cursor()

        with self.assertRaises(err.ProgrammingError):
            cur.get_proc_outputs()

        # A procedure without arguments needs no SET and no SELECT round.
        server.add_response(ok_payload())
        cur.callproc("empty_proc")
        self.assertEqual(server.queries, ["CALL `empty_proc`()"])
        self.assertIsNone(cur.nextset())
        self.assertEqual(cur.get_proc_outputs(), ())
        self.assertEqual(server.queries, ["CALL `empty_proc`()"])


if __name__ == "__main__":
    unittest.main()
