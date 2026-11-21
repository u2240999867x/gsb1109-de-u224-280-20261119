"""Offline precision regression for an old-library -> new-library ledger migration.

The scenario is fully local: canned MySQL text-protocol response bytes are fed
through the real ``Connection._read_packet`` / ``MySQLResult`` parsing path,
and the write-back side is verified on the real ``COM_QUERY`` bytes emitted by
``Connection._execute_command``.  No MySQL server is required.

Two result-set groups carry four same-name ``amount`` columns' worth of
protocols: legacy ``DECIMAL``, ``NEWDECIMAL`` and a ``VAR_STRING`` text column
whose contents look identical to the numeric wire text.  The second group
swaps both the protocol types and the column order.  Every group is read with
a buffered ``Cursor`` and an unbuffered ``SSCursor``.
"""

import io
import struct
from decimal import Context, Decimal, DecimalTuple, localcontext

import pytest

import pymysql
from pymysql.constants import COMMAND, FIELD_TYPE
from pymysql.converters import Decimal2Literal, conversions
from pymysql.cursors import Cursor, SSCursor

COLUMN_NAME = "amount"
SELECT_SQL = "SELECT amount, amount, amount FROM ledger"
INSERT_SQL = "INSERT INTO ledger_target (a, b, c) VALUES (%s, %s, %s)"

# Wire text of the five ledger rows, shared by the numeric columns and the
# look-alike text column.  The last row is a real NULL for numeric columns.
ROW_TEXTS = [
    "9007199254740993.0100",
    "-0.0100",
    "0.0000",
    ("1234567890" * 6) + "1.1230",
    None,
]
NULL_ROW = 4

# Independent read oracle: sign / digits / exponent given explicitly instead
# of being derived from anything the driver returns.
EXPECTED_TUPLES = [
    DecimalTuple(
        0,
        (9, 0, 0, 7, 1, 9, 9, 2, 5, 4, 7, 4, 0, 9, 9, 3, 0, 1, 0, 0),
        -4,
    ),
    DecimalTuple(1, (1, 0, 0), -4),
    DecimalTuple(0, (0,), -4),
    DecimalTuple(
        0,
        tuple(int(ch) for ch in (ROW_TEXTS[3].replace(".", ""))),
        -4,
    ),
]
V3_TEXT = ROW_TEXTS[3]
V3_DIGITS = EXPECTED_TUPLES[3].digits
assert len(V3_DIGITS) == 65 and V3_TEXT.endswith("1230")

# Group A: DECIMAL, text, NEWDECIMAL.  Group B swaps type and column order.
GROUPS = {
    "A": [FIELD_TYPE.DECIMAL, FIELD_TYPE.VAR_STRING, FIELD_TYPE.NEWDECIMAL],
    "B": [FIELD_TYPE.NEWDECIMAL, FIELD_TYPE.DECIMAL, FIELD_TYPE.VAR_STRING],
}
TYPE_NAMES = {
    FIELD_TYPE.DECIMAL: "DECIMAL",
    FIELD_TYPE.NEWDECIMAL: "NEWDECIMAL",
    FIELD_TYPE.VAR_STRING: "VAR_STRING",
}

OK_PAYLOAD = b"\x00\x00\x00\x02\x00\x00\x00"
EOF_PAYLOAD = b"\xfe\x00\x00\x02\x00"


def lenenc(value):
    return bytes([len(value)]) + value


def field_payload(field_type):
    payload = b"".join(
        lenenc(part)
        for part in (
            b"def",
            b"ledger",
            b"ledger",
            b"ledger",
            COLUMN_NAME.encode(),
            COLUMN_NAME.encode(),
        )
    )
    charsetnr = 45 if field_type == FIELD_TYPE.VAR_STRING else 63
    payload += struct.pack(
        "<xHIBHBxx",
        charsetnr,
        70,  # column length
        field_type,
        0,  # flags
        4,  # scale / decimals
    )
    return payload


def cell_payload(field_type, row_index):
    text = ROW_TEXTS[row_index]
    if field_type == FIELD_TYPE.VAR_STRING:
        # The NULL row carries the *text* "NULL", not a protocol NULL.
        return lenenc((text if text is not None else "NULL").encode())
    if text is None:
        return b"\xfb"
    return lenenc(text.encode())


def packet(payload, sequence):
    return struct.pack("<I", len(payload))[:3] + bytes([sequence]) + payload


def build_result_stream(field_types):
    """Build a complete text-protocol result set for the five ledger rows."""
    chunks = [packet(bytes([len(field_types)]), 1)]
    sequence = 2
    for field_type in field_types:
        chunks.append(packet(field_payload(field_type), sequence))
        sequence += 1
    chunks.append(packet(EOF_PAYLOAD, sequence))
    sequence += 1
    for row_index in range(len(ROW_TEXTS)):
        row = b"".join(cell_payload(t, row_index) for t in field_types)
        chunks.append(packet(row, sequence))
        sequence += 1
    chunks.append(packet(EOF_PAYLOAD, sequence))
    return b"".join(chunks)


class FakeSocket:
    """Stand-in socket retaining the real send/recv entry points."""

    def __init__(self, response):
        self.buffer = io.BytesIO(response)
        self.sent = []

    def makefile(self, mode="rb"):
        return self.buffer

    def settimeout(self, timeout):
        pass

    def sendall(self, data):
        self.sent.append(bytes(data))

    def close(self):
        pass


def make_connection(conv=None):
    conn = pymysql.connect(
        user="ledger", ssl_disabled=True, defer_connect=True, conv=conv
    )
    conn.server_status = 0
    conn._current_timeout = None
    return conn


def install_fake_server(conn, fake):
    conn._sock = fake
    conn._rfile = fake.makefile("rb")
    conn._next_seq_id = 0
    conn._result = None


def migrate(conn, group_name, cursorclass):
    """Read one group through a real cursor and write it back via INSERT."""
    # One OK packet per INSERT, sequence id reset to 1 by each new command.
    response = build_result_stream(GROUPS[group_name])
    response += packet(OK_PAYLOAD, 1) * len(ROW_TEXTS)
    fake = FakeSocket(response)
    install_fake_server(conn, fake)

    cursor_name = "unbuffered" if cursorclass is SSCursor else "buffered"
    read_cursor = conn.cursor(cursorclass)
    read_cursor.execute(SELECT_SQL)
    rows = read_cursor.fetchall()
    description = read_cursor.description
    read_cursor.close()

    insert_cursor = conn.cursor()
    # The objects returned by the read side are bound untouched inside a
    # reduced-precision context, mirroring the migration receiver.
    with localcontext(Context(prec=2)):
        for row in rows:
            insert_cursor.execute(INSERT_SQL, row)
    insert_cursor.close()
    return rows, description, fake, cursor_name


def com_queries(fake):
    commands = []
    for raw in fake.sent:
        size = struct.unpack_from("<I", raw, 0)[0] & 0xFFFFFF
        command = raw[4]
        assert command == COMMAND.COM_QUERY
        commands.append(raw[5 : 4 + size])
    return commands


def insert_parameters(fake):
    parameters = []
    for payload in com_queries(fake):
        if not payload.startswith(b"INSERT"):
            continue
        values = payload.partition(b" VALUES (")[2][:-1]
        parameters.append(values.split(b", "))
    return parameters


def expected_read_value(field_type, row_index):
    if field_type == FIELD_TYPE.VAR_STRING:
        return ROW_TEXTS[row_index] if row_index != NULL_ROW else "NULL"
    if row_index == NULL_ROW:
        return None
    return EXPECTED_TUPLES[row_index]


def expected_sql_param(field_type, row_index):
    """Independent byte oracle for one bound INSERT parameter."""
    if field_type == FIELD_TYPE.VAR_STRING:
        text = ROW_TEXTS[row_index] if row_index != NULL_ROW else "NULL"
        return b"'" + text.encode() + b"'"
    if row_index == NULL_ROW:
        return b"NULL"
    return ROW_TEXTS[row_index].encode()


def make_read_checks(group_name, cursor_name, description, rows):
    checks = []

    def check_description():
        assert [column[1] for column in description] == GROUPS[group_name]

    checks.append(
        (
            ("metadata", group_name, cursor_name),
            check_description,
        )
    )

    for row_index, row in enumerate(rows):
        for position, (field_type, value) in enumerate(zip(GROUPS[group_name], row)):
            step = (
                "read",
                group_name,
                cursor_name,
                f"col{position}",
                TYPE_NAMES[field_type],
                f"row{row_index}",
            )

            def check(field_type=field_type, row_index=row_index, value=value):
                expected = expected_read_value(field_type, row_index)
                if field_type == FIELD_TYPE.VAR_STRING:
                    assert isinstance(value, str)
                    assert value == expected
                elif row_index == NULL_ROW:
                    assert value is None
                else:
                    assert isinstance(value, Decimal)
                    assert value.as_tuple() == expected

            checks.append((step, check))
    return checks


def first_failure(checks):
    for step, check in checks:
        try:
            check()
        except AssertionError as exc:
            return step, exc
    return None, None


def rescale(decimal_value, delta):
    sign, digits, exponent = decimal_value.as_tuple()
    return Decimal((sign, digits, exponent + delta))


@pytest.mark.parametrize("group_name", ["A", "B"])
@pytest.mark.parametrize(
    "cursorclass", [Cursor, SSCursor], ids=["buffered", "unbuffered"]
)
def test_read_preserves_independent_decimal_tuples(group_name, cursorclass):
    conn = make_connection()
    rows, description, _fake, cursor_name = migrate(conn, group_name, cursorclass)

    failed_step, _ = first_failure(
        make_read_checks(group_name, cursor_name, description, rows)
    )
    assert failed_step is None

    # Trailing zeros and the 65-digit exponent survive on both numeric types.
    for position, field_type in enumerate(GROUPS[group_name]):
        if field_type == FIELD_TYPE.VAR_STRING:
            continue
        assert rows[0][position].as_tuple().exponent == -4
        assert rows[3][position].as_tuple().digits == V3_DIGITS
        assert rows[4][position] is None


@pytest.mark.parametrize("group_name", ["A", "B"])
@pytest.mark.parametrize(
    "cursorclass", [Cursor, SSCursor], ids=["buffered", "unbuffered"]
)
def test_insert_emits_source_bytes_on_the_wire(group_name, cursorclass):
    conn = make_connection()
    rows, _description, fake, _cursor_name = migrate(conn, group_name, cursorclass)

    bound = insert_parameters(fake)
    assert len(bound) == len(ROW_TEXTS)
    for row_index, parameters in enumerate(bound):
        for position, field_type in enumerate(GROUPS[group_name]):
            assert parameters[position] == expected_sql_param(field_type, row_index)

    # Binding under the low-precision context must not mutate read objects.
    for row_index, row in enumerate(rows):
        for position, field_type in enumerate(GROUPS[group_name]):
            if field_type != FIELD_TYPE.VAR_STRING and row_index != NULL_ROW:
                assert row[position].as_tuple() == EXPECTED_TUPLES[row_index]


def test_binding_contrasts_decimal_real_null_and_null_text():
    # All three objects are constructed by the test, never produced by a
    # driver, so no source-side conversion acts as the oracle.
    proper_decimal = Decimal(V3_TEXT)
    real_null = None
    null_like_text = "NULL"

    conn = make_connection()
    cursor = conn.cursor()
    payload = cursor.mogrify(
        "INSERT INTO ledger_target (a, b, c) VALUES (%s, %s, %s)",
        (proper_decimal, real_null, null_like_text),
    ).encode()
    parameters = payload.partition(b" VALUES (")[2][:-1].split(b", ")

    assert parameters == [V3_TEXT.encode(), b"NULL", b"'NULL'"]
    assert real_null is None
    assert isinstance(null_like_text, str)
    assert proper_decimal.as_tuple() == EXPECTED_TUPLES[3]


@pytest.mark.parametrize("group_name", ["A", "B"])
@pytest.mark.parametrize(
    "cursorclass", [Cursor, SSCursor], ids=["buffered", "unbuffered"]
)
def test_normalizing_decimal_exponent_is_caught_at_read_stage(group_name, cursorclass):
    conv = dict(conversions)
    # Faulty build: only legacy DECIMAL gets its exponent normalized.
    conv[FIELD_TYPE.DECIMAL] = lambda value: Decimal(value).normalize()

    conn = make_connection(conv)
    rows, description, _fake, cursor_name = migrate(conn, group_name, cursorclass)

    failed_step, _ = first_failure(
        make_read_checks(group_name, cursor_name, description, rows)
    )
    decimal_position = GROUPS[group_name].index(FIELD_TYPE.DECIMAL)
    assert failed_step == (
        "read",
        group_name,
        cursor_name,
        f"col{decimal_position}",
        "DECIMAL",
        "row0",
    )

    # The failure really is trailing-zero loss on the decoded DECIMAL ...
    decoded = rows[0][decimal_position]
    assert decoded.as_tuple().exponent == -2
    assert EXPECTED_TUPLES[0].exponent == -4
    # ... and the 65-digit amount loses precision as well once normalized.
    assert rows[3][decimal_position].as_tuple() != EXPECTED_TUPLES[3]
    assert rows[3][decimal_position].as_tuple().exponent != -4

    # ... while NEWDECIMAL and the text column stay faithful.
    surviving = [
        (step, check)
        for step, check in make_read_checks(group_name, cursor_name, description, rows)
        if step[0] != "read" or step[4] != "DECIMAL"
    ]
    assert first_failure(surviving)[0] is None


@pytest.mark.parametrize("group_name", ["A", "B"])
@pytest.mark.parametrize(
    "cursorclass", [Cursor, SSCursor], ids=["buffered", "unbuffered"]
)
def test_scaled_read_and_scaled_bind_cancel_out_on_the_wire(group_name, cursorclass):
    def faulty_decoder(next_decoder):
        def decode(value):
            decimal_value = next_decoder(value)
            if value == V3_TEXT:  # read stage: one scaled amount x 10
                decimal_value = rescale(decimal_value, 1)
            return decimal_value

        return decode

    def faulty_encoder(decimal_value, mapping=None):
        # Bind stage: this amount is always scaled /10, even when the object
        # handed to the encoder is already correct.
        if decimal_value.as_tuple().digits == V3_DIGITS:
            decimal_value = rescale(decimal_value, -1)
        return Decimal2Literal(decimal_value, mapping)

    conv = dict(conversions)
    conv[FIELD_TYPE.DECIMAL] = faulty_decoder(Decimal)
    conv[FIELD_TYPE.NEWDECIMAL] = faulty_decoder(Decimal)
    conv[Decimal] = faulty_encoder

    conn = make_connection(conv)
    rows, description, fake, cursor_name = migrate(conn, group_name, cursorclass)

    # The read object for the scaled amount is wrong at the first numeric
    # column, before any INSERT happens.
    failed_step, _ = first_failure(
        make_read_checks(group_name, cursor_name, description, rows)
    )
    first_numeric = next(
        position
        for position, field_type in enumerate(GROUPS[group_name])
        if field_type in (FIELD_TYPE.DECIMAL, FIELD_TYPE.NEWDECIMAL)
    )
    assert failed_step == (
        "read",
        group_name,
        cursor_name,
        f"col{first_numeric}",
        TYPE_NAMES[GROUPS[group_name][first_numeric]],
        "row3",
    )
    wrong_value = rows[3][first_numeric]
    assert wrong_value.as_tuple().exponent == -3
    assert wrong_value.as_tuple().digits == V3_DIGITS

    # Yet the COM_QUERY bytes for that parameter coincidentally match the
    # source wire text, so SQL-level checks alone cannot detect the fault.
    bound = insert_parameters(fake)
    for position, field_type in enumerate(GROUPS[group_name]):
        if field_type == FIELD_TYPE.VAR_STRING:
            continue
        assert bound[3][position] == expected_sql_param(field_type, 3)
    for row_index in (0, 1, 2, 4):
        for position, field_type in enumerate(GROUPS[group_name]):
            assert bound[row_index][position] == expected_sql_param(
                field_type, row_index
            )

    # Feeding a *correct* object to the same faulty binding entry must show
    # the binding-stage error independently of the bad read.
    healthy_conn = make_connection(conv)
    healthy_cursor = healthy_conn.cursor()
    correct = Decimal(V3_TEXT)
    literal = healthy_cursor.mogrify("%s", (correct,))
    assert literal == format(rescale(correct, -1), "f")
    assert literal != V3_TEXT
    assert correct.as_tuple() == EXPECTED_TUPLES[3]

    unaffected = Decimal(ROW_TEXTS[1])
    assert healthy_cursor.mogrify("%s", (unaffected,)) == ROW_TEXTS[1]


def test_one_migration_traces_every_input_kind_to_its_origin():
    def faulty_decoder(value):
        decimal_value = Decimal(value)
        if value == V3_TEXT:
            decimal_value = rescale(decimal_value, 1)
        return decimal_value

    def faulty_encoder(decimal_value, mapping=None):
        if decimal_value.as_tuple().digits == V3_DIGITS:
            decimal_value = rescale(decimal_value, -1)
        return Decimal2Literal(decimal_value, mapping)

    conv = dict(conversions)
    conv[FIELD_TYPE.DECIMAL] = faulty_decoder
    conv[FIELD_TYPE.NEWDECIMAL] = faulty_decoder
    conv[Decimal] = faulty_encoder

    conn = make_connection(conv)
    rows, _description, fake, _cursor_name = migrate(conn, "A", Cursor)
    bound = insert_parameters(fake)

    trace = [
        {
            "kind": "normal numeric",
            "position": 0,
            "row": 0,
            "wire": ROW_TEXTS[0].encode(),
            "read": rows[0][0],
            "sql": bound[0][0],
        },
        {
            "kind": "text look-alike",
            "position": 1,
            "row": 0,
            "wire": ROW_TEXTS[0].encode(),
            "read": rows[0][1],
            "sql": bound[0][1],
        },
        {
            "kind": "scale-wrong numeric",
            "position": 0,
            "row": 3,
            "wire": ROW_TEXTS[3].encode(),
            "read": rows[3][0],
            "sql": bound[3][0],
        },
        {
            "kind": "real NULL",
            "position": 0,
            "row": 4,
            "wire": b"\xfb",
            "read": rows[4][0],
            "sql": bound[4][0],
        },
        {
            "kind": "NULL-looking text",
            "position": 1,
            "row": 4,
            "wire": b"NULL",
            "read": rows[4][1],
            "sql": bound[4][1],
        },
    ]

    by_kind = {item["kind"]: item for item in trace}

    normal = by_kind["normal numeric"]
    assert isinstance(normal["read"], Decimal)
    assert normal["read"].as_tuple() == EXPECTED_TUPLES[0]
    assert normal["sql"] == normal["wire"]

    text_item = by_kind["text look-alike"]
    assert text_item["read"] == ROW_TEXTS[0]
    assert text_item["sql"] == b"'" + ROW_TEXTS[0].encode() + b"'"

    wrong = by_kind["scale-wrong numeric"]
    assert isinstance(wrong["read"], Decimal)
    assert wrong["read"].as_tuple() != EXPECTED_TUPLES[3]
    assert wrong["read"].as_tuple().exponent == -3
    assert wrong["sql"] == wrong["wire"]  # masked at the SQL byte level

    real_null = by_kind["real NULL"]
    assert real_null["read"] is None
    assert real_null["sql"] == b"NULL"

    null_text = by_kind["NULL-looking text"]
    assert null_text["read"] == "NULL"
    assert null_text["read"] is not None
    assert null_text["sql"] == b"'NULL'"
