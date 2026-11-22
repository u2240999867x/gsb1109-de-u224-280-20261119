"""Offline tests for column metadata snapshots and source directories.

These tests drive PyMySQL with scripted MySQL protocol packets instead of a
live server.  A cp932 connection is used because the byte sequences ``87 54``
and ``FA 4A`` both decode to the same display string (``Ⅰ``), which lets us
prove that source matching uses the original identifier bytes rather than the
decoded spelling.
"""

import struct
from decimal import Decimal

import pytest

import pymysql
from pymysql.constants import FIELD_TYPE, FLAG
from pymysql.cursors import Cursor, SSCursor
from pymysql.metadata import ColumnMetadata, SourceDirectory
from pymysql.protocol import FieldDescriptorPacket, MysqlPacket

ENCODING = "cp932"

# Two distinct cp932 encodings of the same displayed character.
TABLE_A_BYTES = b"orders_\x87\x54_t"
TABLE_B_BYTES = b"orders_\xfa\x4a_t"
DISPLAY_TABLE = TABLE_A_BYTES.decode(ENCODING)
assert DISPLAY_TABLE == TABLE_B_BYTES.decode(ENCODING)

CATALOG = b"def"
DATABASE = b"orders_db"
AMOUNT = b"amount"

DECIMAL_A = {"type": "DECIMAL", "precision": 10, "scale": 2}
DECIMAL_B = {"type": "DECIMAL", "precision": 12, "scale": 4}
BIGINT_UNSIGNED_DECL = {"type": "BIGINT UNSIGNED"}
STRING_UTF8_DECL = {"type": "VARCHAR", "characters": 4}
STRING_SJIS_DECL = {"type": "VARCHAR", "characters": 6}


def _lenenc(value):
    if value < 251:
        return bytes([value])
    if value < 1 << 16:
        return b"\xfc" + struct.pack("<H", value)
    if value < 1 << 24:
        return b"\xfd" + struct.pack("<I", value)[:3]
    return b"\xfe" + struct.pack("<Q", value)


def _lenenc_string(value):
    return _lenenc(len(value)) + value


def field_packet(
    *,
    db,
    table,
    org_table,
    name,
    org_name,
    charsetnr,
    length,
    type_code,
    flags,
    decimals,
):
    return b"".join(
        [
            _lenenc_string(CATALOG),
            _lenenc_string(db),
            _lenenc_string(table),
            _lenenc_string(org_table),
            _lenenc_string(name),
            _lenenc_string(org_name),
            struct.pack("<xHIBHBxx", charsetnr, length, type_code, flags, decimals),
        ]
    )


def result_header(column_count):
    return _lenenc(column_count)


def eof_packet(has_next):
    status = 8 if has_next else 0  # SERVER_MORE_RESULTS_EXISTS
    return b"\xfe" + struct.pack("<HH", 0, status)


def ok_packet(has_next):
    status = 8 if has_next else 0
    return b"\x00\x00\x00" + struct.pack("<HH", status, 0)


def row_packet(values):
    payload = b""
    for value in values:
        if value is None:
            payload += b"\xfb"
        else:
            payload += _lenenc_string(value)
    return payload


def amount_field(org_table, *, length, decimals):
    return field_packet(
        db=DATABASE,
        table=org_table,
        org_table=org_table,
        name=AMOUNT,
        org_name=AMOUNT,
        charsetnr=63,
        length=length,
        type_code=FIELD_TYPE.NEWDECIMAL,
        flags=0,
        decimals=decimals,
    )


def expression_field():
    # Expression columns arrive with empty table and original-column ids.
    return field_packet(
        db=DATABASE,
        table=b"",
        org_table=b"",
        name=AMOUNT,
        org_name=b"",
        charsetnr=63,
        length=7,
        type_code=FIELD_TYPE.NEWDECIMAL,
        flags=0,
        decimals=2,
    )


def bigint_unsigned_field():
    return field_packet(
        db=DATABASE,
        table=b"metrics",
        org_table=b"metrics",
        name=b"counter",
        org_name=b"counter",
        charsetnr=63,
        length=8,
        type_code=FIELD_TYPE.LONGLONG,
        flags=FLAG.UNSIGNED,
        decimals=0,
    )


def string_field(name, charsetnr, length):
    return field_packet(
        db=DATABASE,
        table=b"labels",
        org_table=b"labels",
        name=name,
        org_name=name,
        charsetnr=charsetnr,
        length=length,
        type_code=FIELD_TYPE.VAR_STRING,
        flags=0,
        decimals=0,
    )


def first_result_fields():
    return [
        amount_field(TABLE_A_BYTES, length=8, decimals=2),
        amount_field(TABLE_B_BYTES, length=11, decimals=4),
        expression_field(),
        bigint_unsigned_field(),
        string_field(b"utf8_label", charsetnr=33, length=12),
        string_field(b"sjis_label", charsetnr=88, length=12),
    ]


def second_result_fields():
    # The two amount columns swap positions; everything else stays put.
    return [
        amount_field(TABLE_B_BYTES, length=11, decimals=4),
        amount_field(TABLE_A_BYTES, length=8, decimals=2),
        expression_field(),
        bigint_unsigned_field(),
        string_field(b"utf8_label", charsetnr=33, length=12),
        string_field(b"sjis_label", charsetnr=88, length=12),
    ]


FIRST_ROW = (
    b"12.34",
    b"56.7890",
    b"9.00",
    b"12345",
    b"abcd",
    b"efgh",
)
SECOND_ROW = (
    b"56.7890",
    b"12.34",
    b"9.00",
    b"12345",
    b"abcd",
    b"efgh",
)


def key_for(org_table, org_name=AMOUNT):
    return (CATALOG, DATABASE, org_table, org_name)


def directory_v1():
    return SourceDirectory(
        version="v1",
        declarations={
            key_for(TABLE_A_BYTES): DECIMAL_A,
            key_for(TABLE_B_BYTES): DECIMAL_B,
            key_for(b"metrics", b"counter"): BIGINT_UNSIGNED_DECL,
            key_for(b"labels", b"utf8_label"): STRING_UTF8_DECL,
            key_for(b"labels", b"sjis_label"): STRING_SJIS_DECL,
        },
    )


def directory_v2():
    # Table B's raw bytes are deliberately absent from the next directory.
    return SourceDirectory(
        version="v2",
        declarations={
            key_for(TABLE_A_BYTES): DECIMAL_A,
            key_for(b"metrics", b"counter"): BIGINT_UNSIGNED_DECL,
            key_for(b"labels", b"utf8_label"): STRING_UTF8_DECL,
            key_for(b"labels", b"sjis_label"): STRING_SJIS_DECL,
        },
    )


class ScriptedServer:
    def __init__(self, packets):
        self._packets = list(packets)
        self.read_count = 0

    def read_packet(self, packet_type=MysqlPacket):
        payload = self._packets.pop(0)
        self.read_count += 1
        return packet_type(payload, ENCODING)


def scripted_command(fields_first, fields_second, row_first, row_second):
    return [
        result_header(len(fields_first)),
        *fields_first,
        eof_packet(has_next=True),
        row_packet(row_first),
        eof_packet(has_next=True),
        ok_packet(has_next=True),
        result_header(len(fields_second)),
        *fields_second,
        eof_packet(has_next=False),
        row_packet(row_second),
        eof_packet(has_next=False),
    ]


def make_connection(source_directory):
    conn = pymysql.connect(
        user="agent",
        password="pw",
        charset=ENCODING,
        ssl_disabled=True,
        defer_connect=True,
        source_directory=source_directory,
    )
    conn.server_status = 0
    conn._execute_command = lambda command, sql: None
    return conn


def run_command(cursor_class, source_directory, server):
    conn = make_connection(source_directory)
    server = ScriptedServer(server)
    conn._read_packet = server.read_packet
    cursor = conn.cursor(cursor_class)
    return conn, cursor, server


def assert_first_result_snapshot(metadata, declarations):
    assert [column.position for column in metadata] == [0, 1, 2, 3, 4, 5]

    # The two table-sourced amount columns look identical after decoding, but
    # keep distinct raw table identifiers and therefore distinct declarations.
    assert metadata[0].name == "amount"
    assert metadata[1].name == "amount"
    assert metadata[0].org_name == "amount"
    assert metadata[1].org_name == "amount"
    assert metadata[0].org_table == DISPLAY_TABLE
    assert metadata[1].org_table == DISPLAY_TABLE
    assert metadata[0].org_table_bytes == TABLE_A_BYTES
    assert metadata[1].org_table_bytes == TABLE_B_BYTES
    assert metadata[0].source_key != metadata[1].source_key
    assert declarations[0] == DECIMAL_A
    assert declarations[1] == DECIMAL_B

    # The expression column shares the display name but has no table source.
    assert metadata[2].name == "amount"
    assert metadata[2].org_table_bytes == b""
    assert metadata[2].org_name_bytes == b""
    assert metadata[2].source_key is None
    assert declarations[2] is None

    # BIGINT UNSIGNED: protocol flags and byte length are preserved.
    assert metadata[3].type_code == FIELD_TYPE.LONGLONG
    assert metadata[3].flags == FLAG.UNSIGNED
    assert metadata[3].length == 8
    assert declarations[3] == BIGINT_UNSIGNED_DECL

    # Same byte length, different character capacity per charset.
    assert metadata[4].length == 12
    assert metadata[5].length == 12
    assert metadata[4].column_length == 4
    assert metadata[5].column_length == 6
    assert metadata[4].charsetnr == 33
    assert metadata[5].charsetnr == 88
    assert declarations[4] == STRING_UTF8_DECL
    assert declarations[5] == STRING_SJIS_DECL


def assert_description_contract(cursor, raw_fields):
    expected_fields = [FieldDescriptorPacket(data, ENCODING) for data in raw_fields]
    assert cursor.description == tuple(field.description() for field in expected_fields)
    for described, column in zip(cursor.description, cursor.column_metadata):
        assert described[0] == column.name
        assert described[1] == column.type_code
        assert described[5] == column.scale
        assert described[6] == (column.flags % 2 == 0)


@pytest.mark.parametrize("cursor_class", [Cursor, SSCursor])
def test_snapshot_matches_protocol_and_confirmed_declarations(cursor_class):
    fields = first_result_fields()
    packets = scripted_command(fields, second_result_fields(), FIRST_ROW, SECOND_ROW)
    conn, cursor, server = run_command(cursor_class, directory_v1(), packets)

    cursor.execute("SELECT ...")

    assert isinstance(cursor.column_metadata, tuple)
    assert all(isinstance(column, ColumnMetadata) for column in cursor.column_metadata)
    assert_first_result_snapshot(cursor.column_metadata, cursor.declarations)
    assert_description_contract(cursor, fields)

    # Only the snapshot was inspected: no row has been consumed.
    assert cursor.rownumber == 0

    # Swap the caller's directory before reading any row; the frozen snapshot
    # and its confirmed declarations must not change.
    snapshot_before = cursor.column_metadata
    declarations_before = cursor.declarations
    conn.source_directory = directory_v2()
    assert cursor.column_metadata == snapshot_before
    assert cursor.declarations == declarations_before
    assert cursor.declarations[0] == DECIMAL_A
    assert cursor.rownumber == 0

    # Full detail read.  Rows are decoded with the connection encoding as
    # usual; the directory swap only affects declaration resolution.
    row = cursor.fetchone()
    assert cursor.rownumber == 1
    assert row[0] == Decimal("12.34")
    assert row[1] == Decimal("56.7890")
    assert row[2] == Decimal("9.00")
    assert row[3] == 12345
    assert row[4] == "abcd"
    assert row[5] == "efgh"
    assert cursor.fetchone() is None

    # An OK-only result set sits between the two column result sets.
    assert cursor.nextset() is True
    assert cursor.description is None
    assert cursor.column_metadata is None

    assert cursor.nextset() is True
    second_metadata = cursor.column_metadata
    second_declarations = cursor.declarations
    assert [column.position for column in second_metadata] == [0, 1, 2, 3, 4, 5]

    # Amounts are swapped and are resolved against the new directory only.
    assert second_metadata[0].org_table_bytes == TABLE_B_BYTES
    assert second_metadata[1].org_table_bytes == TABLE_A_BYTES
    assert second_declarations[0] is None
    assert second_declarations[1] == DECIMAL_A
    assert second_declarations[2] is None
    assert second_declarations[3] == BIGINT_UNSIGNED_DECL

    second_row = cursor.fetchone()
    assert second_row[0] == Decimal("56.7890")
    assert second_row[1] == Decimal("12.34")
    assert cursor.fetchone() is None
    assert cursor.nextset() is None

    # Earlier snapshots remain intact after switching result sets.
    assert snapshot_before[0].org_table_bytes == TABLE_A_BYTES
    assert snapshot_before[1].org_table_bytes == TABLE_B_BYTES
    conn.close()


def test_unbuffered_snapshot_reads_no_rows():
    packets = scripted_command(
        first_result_fields(), second_result_fields(), FIRST_ROW, SECOND_ROW
    )
    conn, cursor, server = run_command(SSCursor, directory_v1(), packets)

    cursor.execute("SELECT ...")
    reads_after_execute = server.read_count

    # Repeated snapshot access must not pull any row packets.
    assert cursor.column_metadata[0].org_table_bytes == TABLE_A_BYTES
    assert cursor.declarations[1] == DECIMAL_B
    assert cursor.description[2][0] == "amount"
    assert server.read_count == reads_after_execute
    assert cursor.rownumber == 0

    # The row is still waiting on the wire and is fully readable.
    assert cursor.fetchone()[3] == 12345
    assert cursor.fetchone() is None
    assert cursor.nextset() is True
    assert cursor.nextset() is True
    assert cursor.fetchone()[1] == Decimal("12.34")
    assert cursor.nextset() is None
    conn.close()


def test_homograph_columns_keep_distinct_precision():
    packets = scripted_command(
        first_result_fields(), second_result_fields(), FIRST_ROW, SECOND_ROW
    )
    conn, cursor, _ = run_command(Cursor, directory_v1(), packets)
    cursor.execute("SELECT ...")

    first, second, expression = cursor.column_metadata[:3]
    assert first.org_table == second.org_table == DISPLAY_TABLE
    assert first.org_name == second.org_name == "amount"
    assert first.org_table_bytes != second.org_table_bytes

    assert first.scale == 2
    assert second.scale == 4
    assert cursor.declarations[0]["precision"] == 10
    assert cursor.declarations[1]["precision"] == 12

    # Same spelling must never lend a declaration to the expression column.
    assert expression.source_key is None
    assert cursor.declarations[2] is None
    conn.close()


def test_explicit_directory_binding_is_per_query():
    fields = first_result_fields()
    packets = [
        result_header(len(fields)),
        *fields,
        eof_packet(has_next=False),
        row_packet(FIRST_ROW),
        eof_packet(has_next=False),
    ]

    # The connection itself has no directory; an explicit binding enables it.
    conn, cursor, server = run_command(Cursor, None, packets)
    cursor.execute("SELECT ...", source_directory=directory_v1())
    assert cursor.declarations[0] == DECIMAL_A
    assert cursor.declarations[2] is None
    conn.close()

    packets = [
        result_header(len(fields)),
        *fields,
        eof_packet(has_next=False),
        row_packet(FIRST_ROW),
        eof_packet(has_next=False),
    ]
    conn, cursor, server = run_command(Cursor, None, packets)
    cursor.execute("SELECT ...")
    assert all(declaration is None for declaration in cursor.declarations)
    conn.close()


def test_explicit_directory_overrides_connection_directory():
    fields = first_result_fields()
    conn_directory = directory_v2()
    conn = make_connection(conn_directory)
    cursor = conn.cursor(Cursor)

    def feed_packets():
        conn._read_packet = ScriptedServer(
            [
                result_header(len(fields)),
                *fields,
                eof_packet(has_next=False),
                row_packet(FIRST_ROW),
                eof_packet(has_next=False),
            ]
        ).read_packet

    # Explicit binding uses the supplied directory without replacing the
    # connection's current directory.
    feed_packets()
    cursor.execute("SELECT ...", source_directory=directory_v1())
    assert cursor.declarations[1] == DECIMAL_B
    assert conn.source_directory is conn_directory

    # The next query without an explicit argument falls back to the
    # connection directory, where table B is unconfirmed.
    feed_packets()
    cursor.execute("SELECT ...")
    assert cursor.declarations[1] is None
    assert cursor.declarations[0] == DECIMAL_A
    conn.close()


def test_invalid_directory_type_is_rejected():
    fields = first_result_fields()
    packets = [
        result_header(len(fields)),
        *fields,
        eof_packet(has_next=False),
        row_packet(FIRST_ROW),
        eof_packet(has_next=False),
    ]
    conn, cursor, _ = run_command(Cursor, object(), packets)
    with pytest.raises(TypeError, match="SourceDirectory"):
        cursor.execute("SELECT ...")
    conn.close()


def test_source_directory_matches_raw_bytes_only():
    directory = directory_v1()
    metadata_b = ColumnMetadata.from_field(
        FieldDescriptorPacket(
            amount_field(TABLE_B_BYTES, length=11, decimals=4), ENCODING
        ),
        position=0,
        encoding=ENCODING,
    )

    metadata_a = ColumnMetadata.from_field(
        FieldDescriptorPacket(
            amount_field(TABLE_A_BYTES, length=8, decimals=2), ENCODING
        ),
        position=0,
        encoding=ENCODING,
    )

    # Re-encoding the shared display spelling yields the canonical 87 54 form,
    # which is table A's key -- never table B's FA 4A wire bytes.
    canonical_key = (
        CATALOG,
        DATABASE,
        DISPLAY_TABLE.encode(ENCODING),
        AMOUNT,
    )
    assert canonical_key == key_for(TABLE_A_BYTES)
    assert canonical_key != metadata_b.source_key
    assert canonical_key in directory
    assert metadata_b.source_key in directory
    assert directory.get_declaration(metadata_b) == DECIMAL_B
    assert directory.get_declaration(metadata_a) == DECIMAL_A
