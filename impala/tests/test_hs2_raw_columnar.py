# Copyright 2026 Cloudera Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
from decimal import Decimal
from unittest import mock

from impala._thrift_gen.TCLIService.ttypes import (
    TBinaryColumn, TColumn, TColumnDesc, TFetchResultsResp,
    TGetResultSetMetadataResp, TPrimitiveTypeEntry, TProtocolVersion,
    TRowSet, TStatus, TStatusCode, TStringColumn, TTableSchema, TTypeDesc,
    TTypeEntry, TTypeId, TTypeQualifierValue, TTypeQualifiers)
from impala.hiveserver2 import HiveServer2Cursor, Operation


# These tests drive impala.hiveserver2.Operation and HiveServer2Cursor with
# canned thrift responses, simulating an HS2 query whose result set contains:
#   - two columns sharing the alias 'amount': a DECIMAL(12,3) and a BINARY
#   - a TIMESTAMP column carrying nanosecond precision
#   - a column whose SQL type impyla does not model (USER_DEFINED), which
#     HS2 still transports as readable strings
# The first fetch page has all TIMESTAMP/DECIMAL values null (the null
# bitmap masks unparseable placeholder values); samples only appear on the
# second page.


def _ok_status():
    return TStatus(statusCode=TStatusCode.SUCCESS_STATUS)


def _primitive_desc(name, type_id, precision=None, scale=None, position=0):
    qualifiers = None
    if precision is not None and scale is not None:
        qualifiers = TTypeQualifiers(qualifiers={
            'precision': TTypeQualifierValue(i32Value=precision),
            'scale': TTypeQualifierValue(i32Value=scale)})
    entry = TPrimitiveTypeEntry(type=type_id, typeQualifiers=qualifiers)
    return TColumnDesc(
        columnName=name,
        typeDesc=TTypeDesc(types=[TTypeEntry(primitiveEntry=entry)]),
        position=position)


def _string_tcolumn(values, nulls):
    return TColumn(stringVal=TStringColumn(values=values, nulls=nulls))


def _binary_tcolumn(values, nulls):
    return TColumn(binaryVal=TBinaryColumn(values=values, nulls=nulls))


def _make_operation(schema_columns, pages):
    """Builds an Operation wired to canned GetResultSetMetadata and
    FetchResults responses. `pages` is a list of (has_more_rows, TRowSet)
    pairs consumed in order."""
    session = mock.Mock()
    session.hs2_protocol_version = TProtocolVersion.HIVE_CLI_SERVICE_PROTOCOL_V6
    handle = mock.Mock()
    handle.hasResultSet = True
    operation = Operation(session, handle)
    fetch_responses = [
        TFetchResultsResp(status=_ok_status(), hasMoreRows=has_more,
                          results=rowset)
        for (has_more, rowset) in pages]
    metadata_response = TGetResultSetMetadataResp(
        status=_ok_status(), schema=TTableSchema(columns=schema_columns))

    def fake_rpc(func_name, req, safe_to_retry=False):
        if func_name == 'GetResultSetMetadata':
            return metadata_response
        if func_name == 'FetchResults':
            return fetch_responses.pop(0)
        raise AssertionError('unexpected rpc {0}'.format(func_name))

    operation._rpc = fake_rpc
    return operation


def _main_schema_columns():
    return [
        _primitive_desc('amount', TTypeId.DECIMAL_TYPE,
                        precision=12, scale=3, position=0),
        _primitive_desc('amount', TTypeId.BINARY_TYPE, position=1),
        _primitive_desc('event_time', TTypeId.TIMESTAMP_TYPE, position=2),
        _primitive_desc('payload', TTypeId.USER_DEFINED_TYPE, position=3),
    ]


def _main_pages():
    # First page: TIMESTAMP and DECIMAL are all null; the null bitmap masks
    # unparseable placeholder values.
    page1 = TRowSet(startRowOffset=0, columns=[
        _string_tcolumn([b'not-a-decimal', b''], b'\x03'),
        _binary_tcolumn([b'\x00\x01', b'\x02'], b'\x00'),
        _string_tcolumn([b'not-a-timestamp', b''], b'\x03'),
        _string_tcolumn([b'1-2', b'3-4'], b'\x00'),
    ])
    # Second page: actual samples, including a nanosecond TIMESTAMP.
    page2 = TRowSet(startRowOffset=2, columns=[
        _string_tcolumn([b'123.456', b'0.001'], b'\x00'),
        _binary_tcolumn([b'\xff\xfe', b''], b'\x00'),
        _string_tcolumn([b'2021-03-04 05:06:07.123456789',
                         b'2020-01-01 00:00:00'], b'\x00'),
        _string_tcolumn([b'5-6', b'7-8'], b'\x00'),
    ])
    return [(True, page1), (False, page2)]


def _make_cursor(operation, **kwargs):
    session = mock.Mock()
    session.hs2_protocol_version = TProtocolVersion.HIVE_CLI_SERVICE_PROTOCOL_V6
    cursor = HiveServer2Cursor(session, **kwargs)
    cursor._last_operation = operation
    cursor._last_operation_active = True
    cursor._last_operation_finished = True
    return cursor


def test_convert_types_records_bind_values_across_pages():
    """Reads the simulated query with the existing conversion options and
    checks the per-column type records on each page."""
    operation = _make_operation(_main_schema_columns(), _main_pages())
    schema = operation.get_result_schema()
    # The existing description shape is unchanged, duplicate aliases included.
    assert schema == [
        ('amount', 'DECIMAL', None, None, 12, 3, None),
        ('amount', 'BINARY', None, None, None, None, None),
        ('event_time', 'TIMESTAMP', None, None, None, None, None),
        ('payload', 'USER', None, None, None, None, None),
    ]

    page1 = operation.fetch(schema, convert_types=True)
    # The declared precision/scale are known even though every DECIMAL and
    # TIMESTAMP value on this page is null.
    records = page1.column_records
    assert [r.position for r in records] == [0, 1, 2, 3]
    assert [r.name for r in records] == ['amount', 'amount',
                                         'event_time', 'payload']
    assert records[0].sql_type == 'DECIMAL'
    assert (records[0].precision, records[0].scale) == (12, 3)
    assert records[0].qualifiers_present
    assert records[0].transport_type == 'stringVal'
    assert records[0].python_type == 'decimal.Decimal'
    assert records[1].sql_type == 'BINARY'
    assert records[1].transport_type == 'binaryVal'
    assert records[1].python_type == 'bytes'
    assert records[2].sql_type == 'TIMESTAMP'
    assert not records[2].lossy
    # The SQL type impyla cannot handle is explicitly recorded, with the
    # transport representation it actually arrived in.
    assert not records[3].recognized
    assert records[3].sql_type == 'USER'
    assert records[3].transport_type == 'stringVal'
    assert records[3].python_type is None
    # Null-masked placeholders are not parsed and not delivered as data.
    assert page1.pop_many(2) == [
        (None, b'\x00\x01', None, b'1-2'),
        (None, b'\x02', None, b'3-4'),
    ]

    page2 = operation.fetch(schema, convert_types=True)
    rows = page2.pop_many(2)
    assert rows[0][0] == Decimal('123.456')
    assert rows[1][0] == Decimal('0.001')
    assert rows[0][1] == b'\xff\xfe'
    # Python datetime cannot hold the nanoseconds; the value is truncated.
    assert rows[0][2] == datetime.datetime(2021, 3, 4, 5, 6, 7, 123456)
    assert rows[1][2] == datetime.datetime(2020, 1, 1, 0, 0, 0)
    assert rows[0][3] == b'5-6'
    # The lossy conversion is marked on the record bound to that column.
    assert page2.column_records[2].lossy
    assert not page2.column_records[0].lossy
    # Records are bound by position: the duplicate aliases stay distinct.
    assert page2.column_records[0].sql_type == 'DECIMAL'
    assert page2.column_records[1].sql_type == 'BINARY'


def test_raw_columnar_records_bind_values_across_pages():
    """Reads the same simulated query with the raw columnar option."""
    operation = _make_operation(_main_schema_columns(), _main_pages())
    schema = operation.get_result_schema()

    page1 = operation.fetch(schema, raw_columnar=True)
    records = page1.column_records
    # Declarations come from the metadata, not from sampled values: the
    # all-null page still reports DECIMAL(12,3) and TIMESTAMP.
    assert records[0].sql_type == 'DECIMAL'
    assert (records[0].precision, records[0].scale) == (12, 3)
    assert records[0].qualifiers_present
    assert records[2].sql_type == 'TIMESTAMP'
    assert not records[3].recognized
    # Null-masked placeholders are blanked out; everything else is the raw
    # payload as delivered.
    assert page1.pop_many(2) == [
        (None, b'\x00\x01', None, '1-2'),
        (None, b'\x02', None, '3-4'),
    ]

    page2 = operation.fetch(schema, raw_columnar=True)
    rows = page2.pop_many(2)
    # DECIMAL stays text and never goes through float.
    assert rows[0][0] == '123.456'
    assert rows[1][0] == '0.001'
    # BINARY stays bytes.
    assert rows[0][1] == b'\xff\xfe'
    assert rows[1][1] == b''
    # The nanosecond TIMESTAMP keeps the transported value; nothing is
    # truncated and nothing is fabricated for the lower-precision value.
    assert rows[0][2] == '2021-03-04 05:06:07.123456789'
    assert rows[1][2] == '2020-01-01 00:00:00'
    # The unrecognized type keeps its readable raw payload.
    assert rows[0][3] == '5-6'
    assert rows[1][3] == '7-8'
    # Raw delivery loses nothing, so no record is marked lossy.
    assert not any(r.lossy for r in page2.column_records)
    # Same declarations as the converting read, bound by position.
    assert [(r.name, r.sql_type) for r in page2.column_records] == [
        ('amount', 'DECIMAL'), ('amount', 'BINARY'),
        ('event_time', 'TIMESTAMP'), ('payload', 'USER')]


def test_raw_columnar_column_subset_maps_to_source_declaration():
    """A query selecting only one of the same-named columns still resolves
    to the correct source column declaration."""
    schema_columns = [_primitive_desc('amount', TTypeId.BINARY_TYPE,
                                      position=0)]
    pages = [(False, TRowSet(startRowOffset=0, columns=[
        _binary_tcolumn([b'\xde\xad'], b'\x00')]))]
    operation = _make_operation(schema_columns, pages)
    schema = operation.get_result_schema()
    assert schema == [('amount', 'BINARY', None, None, None, None, None)]

    batch = operation.fetch(schema, raw_columnar=True)
    record = batch.column_records[0]
    assert record.position == 0
    assert record.name == 'amount'
    assert record.sql_type == 'BINARY'
    assert record.transport_type == 'binaryVal'
    assert record.recognized
    assert batch.pop_many(1) == [(b'\xde\xad',)]


def test_decimal_with_missing_type_qualifiers_is_preserved():
    """A recognizable type whose declared qualifiers are missing is
    explicitly recorded as such, in both reading modes."""
    schema_columns = [_primitive_desc('ratio', TTypeId.DECIMAL_TYPE,
                                      position=0)]

    def rowset():
        # CBatch converts values in place, so each read needs a fresh page.
        return TRowSet(startRowOffset=0, columns=[
            _string_tcolumn([b'1.5'], b'\x00')])

    operation = _make_operation(schema_columns, [(False, rowset())])
    schema = operation.get_result_schema()
    # Missing qualifiers do not break the existing description.
    assert schema == [('ratio', 'DECIMAL', None, None, None, None, None)]

    raw_batch = operation.fetch(schema, raw_columnar=True)
    record = raw_batch.column_records[0]
    assert record.recognized
    assert not record.qualifiers_present
    assert record.precision is None
    assert record.scale is None
    assert raw_batch.pop_many(1) == [('1.5',)]

    operation = _make_operation(schema_columns, [(False, rowset())])
    batch = operation.fetch(operation.get_result_schema(), convert_types=True)
    assert batch.column_records[0].qualifiers_present is False
    assert batch.pop_many(1) == [(Decimal('1.5'),)]


def test_cursor_fetchcolumnar_raw_columnar():
    """The cursor threads the raw columnar option through fetchcolumnar."""
    pages = _main_pages() + [(False, None)]
    operation = _make_operation(_main_schema_columns(), pages)
    cursor = _make_cursor(operation, raw_columnar=True)

    batches = cursor.fetchcolumnar()
    assert len(batches) == 2
    assert batches[0].raw_columnar
    assert [r.name for r in batches[0].column_records] == [
        'amount', 'amount', 'event_time', 'payload']
    rows = [row for batch in batches for row in batch.pop_many(len(batch))]
    assert rows[0] == (None, b'\x00\x01', None, '1-2')
    assert rows[2][0] == '123.456'
    assert rows[2][2] == '2021-03-04 05:06:07.123456789'


def test_cursor_fetchcolumnar_convert_types_unchanged():
    """A normal cursor still produces the existing description and
    converted values."""
    pages = _main_pages() + [(False, None)]
    operation = _make_operation(_main_schema_columns(), pages)
    cursor = _make_cursor(operation)

    assert cursor.description == [
        ('amount', 'DECIMAL', None, None, 12, 3, None),
        ('amount', 'BINARY', None, None, None, None, None),
        ('event_time', 'TIMESTAMP', None, None, None, None, None),
        ('payload', 'USER', None, None, None, None, None),
    ]
    batches = cursor.fetchcolumnar()
    assert not batches[0].raw_columnar
    rows = [row for batch in batches for row in batch.pop_many(len(batch))]
    assert rows[0] == (None, b'\x00\x01', None, b'1-2')
    assert rows[2][0] == Decimal('123.456')
    assert rows[2][2] == datetime.datetime(2021, 3, 4, 5, 6, 7, 123456)
