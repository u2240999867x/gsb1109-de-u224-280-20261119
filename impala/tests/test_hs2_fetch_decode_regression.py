"""Offline regression tests for HiveServer2 row/columnar result decoding.

The full result contains two server pages.  The first page is 13 rows long,
which is deliberately more than one byte can describe.  Null positions in the
integer and string columns intersect around rows 8 and 9 (1-based), so a NULL
in one column sits next to a real zero or a real empty string in the other.
The second page has its own NULL bitmaps whose origin is the page start, and
one of its bitmap byte strings omits a trailing all-zero byte.  Columnar value
slots that are NULL carry a visible placeholder which must never escape.

The wire shapes are chosen to isolate these decoder defects:

* bitmap bit order: the first page spans more than one byte and row 8/9 put a
  NULL next to a real zero and an empty string;
* page-local bitmap origin: the second page starts at row 13, so applying the
  global offset to its bitmap shifts every NULL;
* per-column bitmap use: the integer and string columns have different NULL
  positions, so borrowing another column's bitmap changes individual cells;
* multi-row transposition: fetchmany reads several rows, so a transpose that
  only works for one-row batches produces reordered or truncated records.
"""

from impala._thrift_gen.ImpalaService.ttypes import TCloseImpalaOperationResp
from impala._thrift_gen.TCLIService.ttypes import (
    TCloseOperationResp,
    TColumn,
    TColumnDesc,
    TColumnValue,
    TFetchResultsResp,
    TGetLogResp,
    TGetOperationStatusResp,
    TGetResultSetMetadataResp,
    THandleIdentifier,
    TI32Column,
    TI32Value,
    TOperationHandle,
    TOperationState,
    TOperationType,
    TPrimitiveTypeEntry,
    TProtocolVersion,
    TRow,
    TRowSet,
    TStatus,
    TStatusCode,
    TStringColumn,
    TStringValue,
    TTableSchema,
    TTypeDesc,
    TTypeEntry,
    TTypeId,
)
from impala.hiveserver2 import HiveServer2Cursor, Operation


SCHEMA = (
    ('team', 'STRING'),
    ('points', 'INT'),
    ('note', 'STRING'),
)

# Expected values are stated independently of the wire encodings below.
EXPECTED_ROWS = (
    ('match-01', 10, 'alpha'),
    ('match-02', 20, 'beta'),
    ('match-03', None, 'gamma'),
    ('match-04', 40, ''),
    ('match-05', 50, None),
    ('match-06', 60, 'delta'),
    ('match-07', 70, 'epsilon'),
    ('match-08', None, ''),
    ('match-09', 0, None),
    ('match-10', 100, 'zeta'),
    ('match-11', 110, None),
    ('match-12', None, 'eta'),
    ('match-13', 130, 'theta'),
    ('match-14', 140, 'iota'),
    ('match-15', None, 'kappa'),
    ('match-16', 160, ''),
    ('match-17', 170, None),
    ('match-18', 0, 'lambda'),
    ('match-19', 190, 'mu'),
    ('match-20', 200, 'nu'),
    ('match-21', 210, 'xi'),
    ('match-22', 220, 'omicron'),
    ('match-23', 230, 'pi'),
    ('match-24', 240, 'rho'),
    ('match-25', 250, 'sigma'),
    ('match-26', 260, None),
    ('match-27', 270, 'tau'),
    ('match-28', 280, 'upsilon'),
    ('match-29', None, 'phi'),
    ('match-30', 300, 'chi'),
)

EXPECTED_BY_KEY = {record[0]: record for record in EXPECTED_ROWS}
EXPECTED_KEYS = [record[0] for record in EXPECTED_ROWS]

FIRST_PAGE_SIZE = 13
PLACEHOLDER_INT = -777
PLACEHOLDER_STRING = 'server-placeholder-must-not-leak'


def _bitmap(flags, omit_trailing_zero_bytes=False):
    """Build the little-endian NULL bitmap for a sequence of booleans."""
    bitmap = bytearray((len(flags) + 7) // 8)
    for index, flag in enumerate(flags):
        if flag:
            bitmap[index // 8] |= 1 << (index % 8)
    if omit_trailing_zero_bytes:
        while bitmap and bitmap[-1] == 0:
            bitmap.pop()
    return bytes(bitmap)


def _columnar_rowset(records, start_row_offset, omit_trailing_zero_bytes=False):
    points = [PLACEHOLDER_INT if record[1] is None else record[1]
              for record in records]
    notes = [PLACEHOLDER_STRING.encode('utf-8') if record[2] is None
             else record[2].encode('utf-8') for record in records]
    return TRowSet(
        startRowOffset=start_row_offset,
        rows=[],
        columns=[
            TColumn(stringVal=TStringColumn(
                values=[record[0].encode('utf-8') for record in records],
                nulls=b'')),
            TColumn(i32Val=TI32Column(
                values=points,
                nulls=_bitmap([record[1] is None for record in records],
                              omit_trailing_zero_bytes))),
            TColumn(stringVal=TStringColumn(
                values=notes,
                nulls=_bitmap([record[2] is None for record in records],
                              omit_trailing_zero_bytes))),
        ],
    )


def _row_rowset(records, start_row_offset):
    return TRowSet(
        startRowOffset=start_row_offset,
        rows=[
            TRow(colVals=[
                TColumnValue(stringVal=TStringValue(record[0])),
                TColumnValue(i32Val=TI32Value(record[1])),
                TColumnValue(stringVal=TStringValue(record[2])),
            ])
            for record in records
        ],
        columns=[],
    )


def _result_schema():
    columns = []
    for position, (name, type_name) in enumerate(SCHEMA):
        columns.append(TColumnDesc(
            columnName=name,
            typeDesc=TTypeDesc(types=[TTypeEntry(
                primitiveEntry=TPrimitiveTypeEntry(
                    type=getattr(TTypeId, type_name + '_TYPE')))]),
            position=position,
        ))
    return TTableSchema(columns=columns)


def _first_page():
    return EXPECTED_ROWS[:FIRST_PAGE_SIZE]


def _second_page():
    return EXPECTED_ROWS[FIRST_PAGE_SIZE:]


def _columnar_pages():
    return [
        _columnar_rowset(_first_page(), 0),
        _columnar_rowset(_second_page(), FIRST_PAGE_SIZE,
                         omit_trailing_zero_bytes=True),
    ]


def _row_pages():
    return [
        _row_rowset(_first_page(), 0),
        _row_rowset(_second_page(), FIRST_PAGE_SIZE),
    ]


def _ok_status():
    return TStatus(statusCode=TStatusCode.SUCCESS_STATUS)


class _FakeTransport(object):
    def isOpen(self):
        return True

    def open(self):
        pass


class _FakeThriftClient(object):
    """Minimal TCLIService endpoint returning canned Thrift responses."""

    def __init__(self, pages):
        self.pages = pages
        self.next_page = 0
        self._iprot = type('Protocol', (), {'trans': _FakeTransport()})()

    def GetOperationStatus(self, request):
        return TGetOperationStatusResp(
            status=_ok_status(),
            operationState=TOperationState.FINISHED_STATE,
            hasResultSet=True,
        )

    def GetResultSetMetadata(self, request):
        return TGetResultSetMetadataResp(
            status=_ok_status(), schema=_result_schema())

    def FetchResults(self, request):
        if self.next_page >= len(self.pages):
            # HiveServer2 answers an exhausted fetch with an empty rowset.
            return TFetchResultsResp(
                status=_ok_status(), hasMoreRows=False, results=None)
        page = self.pages[self.next_page]
        self.next_page += 1
        return TFetchResultsResp(
            status=_ok_status(),
            hasMoreRows=self.next_page < len(self.pages),
            results=page,
        )

    def GetLog(self, request):
        return TGetLogResp(status=_ok_status(), log='')

    def CloseImpalaOperation(self, request):
        return TCloseImpalaOperationResp(status=_ok_status())

    def CloseOperation(self, request):
        return TCloseOperationResp(status=_ok_status())


class _FakeSession(object):
    def __init__(self, client, protocol_version):
        self.client = client
        self.hs2_protocol_version = protocol_version
        self.handle = None

    def execute(self, statement, configuration=None, run_async=False):
        handle = TOperationHandle(
            operationId=THandleIdentifier(guid=b'operation', secret=b'secret'),
            operationType=TOperationType.EXECUTE_STATEMENT,
            hasResultSet=True,
        )
        return Operation(self, handle)

    def close(self):
        pass


def _cursor_for_pages(pages, protocol_version):
    client = _FakeThriftClient(pages)
    session = _FakeSession(client, protocol_version)
    cursor = HiveServer2Cursor(session)
    cursor.execute('select team, points, note from matches order by team')
    return cursor


def _columnar_cursor(pages=None):
    return _cursor_for_pages(
        _columnar_pages() if pages is None else pages,
        TProtocolVersion.HIVE_CLI_SERVICE_PROTOCOL_V6)


def _row_cursor(pages=None):
    return _cursor_for_pages(
        _row_pages() if pages is None else pages,
        TProtocolVersion.HIVE_CLI_SERVICE_PROTOCOL_V5)


def _assert_expected(records, expected_rows=EXPECTED_ROWS):
    """Check row count, key order, and every cell against the keyed table."""
    expected_by_key = {record[0]: record for record in expected_rows}
    assert [record[0] for record in records] == \
           [record[0] for record in expected_rows]
    assert len(records) == len(expected_rows)
    for record in records:
        expected = expected_by_key[record[0]]
        assert record[0] == expected[0]
        assert record[1] == expected[1]
        assert record[2] == expected[2]
        assert record[1] != PLACEHOLDER_INT
        assert record[2] != PLACEHOLDER_STRING


def test_fixture_pages_exercise_required_wire_shapes():
    first = _columnar_rowset(_first_page(), 0)
    second = _columnar_rowset(_second_page(), FIRST_PAGE_SIZE,
                              omit_trailing_zero_bytes=True)

    # The first page spans two bitmap bytes, and its eighth and ninth records
    # (1-based) put a NULL next to a real zero or empty string.
    assert first.startRowOffset == 0
    assert len(first.columns[1].i32Val.values) > 8
    assert len(first.columns[2].stringVal.values) > 8
    assert _first_page()[7] == ('match-08', None, '')
    assert _first_page()[8] == ('match-09', 0, None)

    # The second page starts at 13, so interpreting its bitmap from the result
    # origin rather than the page origin shifts the NULL positions.
    assert second.startRowOffset == FIRST_PAGE_SIZE
    # The integer column's only high-bit NULL is in its second byte; its third
    # byte is all zero and was omitted.  The string column has one NULL in each
    # of its first two bytes and its all-zero third byte was likewise omitted.
    assert second.columns[1].i32Val.nulls == b'\x02\x80'
    assert second.columns[2].stringVal.nulls == b'\x08\x10'
    assert len(second.columns[2].stringVal.values) == len(_second_page())


def test_columnar_single_whole_page_read_keeps_all_nulls_distinct():
    expected = _first_page()
    pages = [_columnar_rowset(expected, 0)]
    columnar = _columnar_cursor(pages)

    observed = columnar.fetchmany(FIRST_PAGE_SIZE)
    _assert_expected(observed, expected)
    assert observed[7] == ('match-08', None, '')
    assert observed[8] == ('match-09', 0, None)
    assert columnar.fetchone() is None


def test_columnar_sequence_one_whole_page_then_interleaved_fetch():
    columnar = _columnar_cursor()
    observed = []

    first_page = columnar.fetchmany(FIRST_PAGE_SIZE)
    assert [record[0] for record in first_page] == \
           [record[0] for record in _first_page()]
    assert first_page[7] == ('match-08', None, '')
    assert first_page[8] == ('match-09', 0, None)
    observed.extend(first_page)

    observed.append(columnar.fetchone())
    # Starting at the second page's local row 2, this call crosses the first
    # byte boundary of that page's bitmap at local rows 7/8.
    crossing = columnar.fetchmany(8)
    assert [record[0] for record in crossing] == EXPECTED_KEYS[14:22]
    assert crossing[4] == ('match-19', 190, 'mu')
    assert crossing[5] == ('match-20', 200, 'nu')
    assert crossing[6] == ('match-21', 210, 'xi')
    observed.extend(crossing)

    observed.append(columnar.fetchone())
    observed.extend(columnar.fetchmany(100))

    _assert_expected(observed)


def test_columnar_sequence_two_one_fetchmany_crosses_bitmap_and_page():
    columnar = _columnar_cursor()
    observed = []

    observed.extend(columnar.fetchone() for _ in range(5))

    # Rows 6-15 in one call: this crosses the first page's byte boundary
    # between rows 8 and 9, then switches from page 1 (13 rows) to page 2.
    crossing = columnar.fetchmany(10)
    assert [record[0] for record in crossing] == EXPECTED_KEYS[5:15]
    assert crossing[2] == ('match-08', None, '')
    assert crossing[3] == ('match-09', 0, None)
    assert crossing[7] == ('match-13', 130, 'theta')
    assert crossing[8] == ('match-14', 140, 'iota')
    observed.extend(crossing)

    # Rows 16-22: crosses the second page's first-byte boundary too.
    observed.extend(columnar.fetchmany(7))
    observed.append(columnar.fetchone())
    observed.extend(columnar.fetchmany(100))

    _assert_expected(observed)


def test_row_cursor_fetchone_fetchmany_matches_expected():
    row = _row_cursor()
    observed = []

    observed.extend(row.fetchone() for _ in range(10))
    observed.extend(row.fetchmany(5))
    observed.append(row.fetchone())
    observed.extend(row.fetchmany(100))

    _assert_expected(observed)


def test_row_and_columnar_decode_the_same_keyed_records_once():
    row = _row_cursor()
    columnar = _columnar_cursor()

    row_records = row.fetchall()
    columnar_records = columnar.fetchall()

    _assert_expected(row_records)
    _assert_expected(columnar_records)
    assert row_records == columnar_records
    assert len(row_records) == len(EXPECTED_BY_KEY)
    assert len(columnar_records) == len(EXPECTED_BY_KEY)
