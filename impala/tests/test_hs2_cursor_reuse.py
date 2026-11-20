from collections import namedtuple
from types import SimpleNamespace

import pytest

from impala import hiveserver2 as hs2
from impala._thrift_gen.ImpalaService.ttypes import (
    TCloseImpalaOperationResp, TDmlResult)
from impala._thrift_gen.TCLIService.ttypes import (
    TCloseSessionResp, TColumn, TColumnDesc, TExecuteStatementResp,
    TFetchResultsResp, TGetLogResp, TGetOperationStatusResp,
    TGetResultSetMetadataResp, THandleIdentifier, TOpenSessionResp,
    TOperationHandle, TOperationState, TOperationType, TPrimitiveTypeEntry,
    TRowSet, TSessionHandle, TStatus, TStatusCode, TStringColumn,
    TTableSchema, TTypeDesc, TTypeEntry, TTypeId, TProtocolVersion)


SELECT_ONE = "SELECT first_id, first_label FROM source"
UPDATE = "UPDATE target SET label = 'changed'"
SELECT_TWO = "SELECT third_code, third_note FROM target"

OPERATION_RPCS = {
    'GetOperationStatus', 'GetResultSetMetadata', 'FetchResults',
    'GetLog', 'CloseImpalaOperation'}

RpcCall = namedtuple(
    'RpcCall', 'method guid secret operation_type statement')


def _ok_status():
    return TStatus(statusCode=TStatusCode.SUCCESS_STATUS)


def _nulls(num_rows):
    return b'\x00' * ((num_rows + 7) // 8)


def _column_descs(names):
    return [
        TColumnDesc(
            columnName=name,
            position=position,
            typeDesc=TTypeDesc(types=[
                TTypeEntry(primitiveEntry=TPrimitiveTypeEntry(
                    type=TTypeId.STRING_TYPE))]),
        )
        for position, name in enumerate(names)
    ]


def _row_set(names, rows):
    return TRowSet(
        startRowOffset=0,
        columns=[
            TColumn(stringVal=TStringColumn(
                values=[row[position].encode('utf-8') for row in rows],
                nulls=_nulls(len(rows))))
            for position in range(len(names))
        ])


def _expected_description(names):
    return [(name, 'STRING', None, None, None, None, None)
            for name in names]


class ScriptedOperation(object):
    """Responses belonging to one operation handle.

    Every server response is looked up by the handle in the request, so a
    response can never be served for a different operation.
    """

    def __init__(self, guid, columns=None, fetches=None, statuses=None,
                 declared_has_result_set=False, log='', dml_result=None):
        self.guid = guid
        self.secret = b'secret-' + guid
        self.handle = TOperationHandle(
            operationId=THandleIdentifier(guid=guid, secret=self.secret),
            operationType=TOperationType.EXECUTE_STATEMENT,
            hasResultSet=declared_has_result_set)
        self.columns = columns
        self.fetches = fetches or []
        self.statuses = statuses or [
            (TOperationState.FINISHED_STATE, declared_has_result_set)]
        self.log = log
        self.dml_result = dml_result
        self.status_index = 0
        self.fetch_index = 0
        self.close_calls = 0

    @property
    def description(self):
        return _expected_description(self.columns)

    def next_status(self):
        index = min(self.status_index, len(self.statuses) - 1)
        self.status_index = index + 1
        return self.statuses[index]

    def next_fetch(self):
        index = min(self.fetch_index, len(self.fetches) - 1)
        self.fetch_index = index + 1
        return self.fetches[index]


class RecordingClient(object):
    """A recording HS2 thrift client that routes every RPC by operation handle."""

    def __init__(self, scripts, statements):
        self.scripts = {script.guid: script for script in scripts}
        self.scripts_by_statement = {
            statement: script for statement, script in statements.items()}
        self.calls = []
        self.transport = _FakeTransport()
        self._iprot = SimpleNamespace(trans=self.transport)
        self._oprot = self._iprot
        self.session_handle = TSessionHandle(
            sessionId=THandleIdentifier(guid=b'session', secret=b'session'))

    def _record(self, method, handle=None, statement=None):
        if handle is None:
            self.calls.append(RpcCall(method, None, None, None, statement))
            return
        identifier = handle.operationId
        self.calls.append(RpcCall(
            method, identifier.guid, identifier.secret,
            handle.operationType, statement))

    def _script_for_handle(self, method, handle):
        if handle is None or handle.operationId is None:
            raise AssertionError(
                '%s called without an operation handle' % method)
        guid = handle.operationId.guid
        if guid not in self.scripts:
            raise AssertionError(
                '%s called with unknown operation handle %r' % (method, guid))
        return self.scripts[guid]

    def OpenSession(self, req):
        self._record('OpenSession')
        return TOpenSessionResp(
            status=_ok_status(),
            serverProtocolVersion=TProtocolVersion.HIVE_CLI_SERVICE_PROTOCOL_V6,
            sessionHandle=self.session_handle,
            configuration={})

    def ExecuteStatement(self, req):
        self._record('ExecuteStatement', statement=req.statement)
        script = self.scripts_by_statement[req.statement]
        return TExecuteStatementResp(
            status=_ok_status(), operationHandle=script.handle)

    def GetOperationStatus(self, req):
        self._record('GetOperationStatus', req.operationHandle)
        script = self._script_for_handle(
            'GetOperationStatus', req.operationHandle)
        state, has_result_set = script.next_status()
        return TGetOperationStatusResp(
            status=_ok_status(), operationState=state,
            hasResultSet=has_result_set)

    def GetResultSetMetadata(self, req):
        self._record('GetResultSetMetadata', req.operationHandle)
        script = self._script_for_handle(
            'GetResultSetMetadata', req.operationHandle)
        if script.columns is None:
            raise AssertionError('metadata requested for a no-result operation')
        return TGetResultSetMetadataResp(
            status=_ok_status(),
            schema=TTableSchema(columns=_column_descs(script.columns)))

    def FetchResults(self, req):
        self._record('FetchResults', req.operationHandle)
        script = self._script_for_handle('FetchResults', req.operationHandle)
        if script.columns is None:
            raise AssertionError('fetch requested for a no-result operation')
        rows, has_more_rows = script.next_fetch()
        results = _row_set(script.columns, rows) if rows else None
        return TFetchResultsResp(
            status=_ok_status(), hasMoreRows=has_more_rows, results=results)

    def GetLog(self, req):
        self._record('GetLog', req.operationHandle)
        script = self._script_for_handle('GetLog', req.operationHandle)
        return TGetLogResp(status=_ok_status(), log=script.log)

    def CloseImpalaOperation(self, req):
        self._record('CloseImpalaOperation', req.operationHandle)
        script = self._script_for_handle(
            'CloseImpalaOperation', req.operationHandle)
        script.close_calls += 1
        return TCloseImpalaOperationResp(
            status=_ok_status(), dml_result=script.dml_result)

    def CloseSession(self, req):
        self._record('CloseSession')
        return TCloseSessionResp(status=_ok_status())


class _FakeTransport(object):

    def __init__(self):
        self.opened = False

    def isOpen(self):
        return self.opened

    def open(self):
        self.opened = True

    def close(self):
        self.opened = False


def _scenario():
    first = ScriptedOperation(
        guid=b'handle-select-one',
        columns=('first_id', 'first_label'),
        fetches=[([('1', 'one'), ('2', 'two'), ('3', 'three')], False)],
        declared_has_result_set=True,
        log='log for first select')
    update = ScriptedOperation(
        guid=b'handle-update',
        statuses=[(TOperationState.FINISHED_STATE, False)],
        log='log for update',
        dml_result=TDmlResult(
            rows_modified={'partition=1': 5, 'partition=2': 7},
            rows_deleted={}, num_row_errors=0))
    third = ScriptedOperation(
        guid=b'handle-select-two',
        columns=('third_code', 'third_note'),
        fetches=[
            ([('30', 'thirty'), ('31', 'thirty-one')], True),
            ([], False),
        ],
        statuses=[(TOperationState.FINISHED_STATE, True)],
        declared_has_result_set=False,
        log='log for third select')
    return first, update, third


def _guids(client, method):
    return [call.guid for call in client.calls if call.method == method]


def _index_of(client, method, guid=None, statement=None):
    for index, call in enumerate(client.calls):
        if call.method != method:
            continue
        if guid is not None and call.guid != guid:
            continue
        if statement is not None and call.statement != statement:
            continue
        return index
    raise AssertionError(
        '%s(guid=%r, statement=%r) not found in %r' %
        (method, guid, statement, client.calls))


def _assert_log_saved_before_close(client, guid):
    log_indexes = [index for index, call in enumerate(client.calls)
                   if call.method == 'GetLog' and call.guid == guid]
    close_indexes = [index for index, call in enumerate(client.calls)
                     if call.method == 'CloseImpalaOperation' and
                     call.guid == guid]
    for log_index in log_indexes:
        for close_index in close_indexes:
            assert log_index < close_index


def _assert_operation_identity(client, scripts):
    by_guid = {script.guid: script for script in scripts}
    operation_calls = [call for call in client.calls
                       if call.method in OPERATION_RPCS]
    assert operation_calls
    for call in operation_calls:
        assert call.guid in by_guid, call
        script = by_guid[call.guid]
        assert call.guid == script.guid
        assert call.secret == script.secret
        assert call.operation_type == script.handle.operationType
        assert call.operation_type == TOperationType.EXECUTE_STATEMENT


@pytest.mark.parametrize('close_finished_queries', [True, False])
def test_cursor_reuse_keeps_operation_state_with_its_handle(
        close_finished_queries):
    first, update, third = _scenario()
    assert len({first.guid, update.guid, third.guid}) == 3
    assert len({first.log, update.log, third.log}) == 3
    assert first.description != third.description

    client = RecordingClient(
        (first, update, third),
        {SELECT_ONE: first, UPDATE: update, SELECT_TWO: third})
    con = hs2.HiveServer2Connection(hs2.HS2Service(client))
    cur = con.cursor(close_finished_queries=close_finished_queries)
    try:
        # The server returns the whole first batch, but the caller takes one row.
        cur.execute(SELECT_ONE)
        assert cur.query_string == SELECT_ONE
        assert cur._last_operation.handle.operationId.guid == first.guid
        assert cur.has_result_set
        assert cur.description == first.description
        assert cur.description != third.description
        assert _guids(client, 'GetResultSetMetadata') == [first.guid]

        row = cur.fetchone()
        assert row == ('1', 'one')
        assert row != ('30', 'thirty')
        assert cur.description == first.description
        assert cur.rowcount == 1
        assert len(cur._buffer) == 2
        # There are still local rows, so the first operation is not closed yet.
        assert first.close_calls == 0
        assert _guids(client, 'FetchResults') == [first.guid]
        assert first.fetch_index == 1

        # The update returns no results. Replacing the first operation closes
        # its handle before the update is submitted.
        cur.execute(UPDATE)
        assert cur.query_string == UPDATE
        assert cur._last_operation.handle.operationId.guid == update.guid
        assert first.close_calls == 1
        assert _index_of(client, 'CloseImpalaOperation', first.guid) < \
            _index_of(client, 'ExecuteStatement', statement=UPDATE)
        assert not cur.has_result_set
        assert cur.description is None
        assert _guids(client, 'GetResultSetMetadata') == [first.guid]
        assert _guids(client, 'FetchResults') == [first.guid]
        assert len(cur._buffer) == 0

        if close_finished_queries:
            assert cur.rowcount == 12
            assert cur.get_log() == update.log
            assert update.close_calls == 1
            assert _index_of(client, 'GetLog', update.guid) < \
                _index_of(client, 'CloseImpalaOperation', update.guid)
            _assert_log_saved_before_close(client, update.guid)
        else:
            assert cur.rowcount == -1
            assert update.close_calls == 0
            assert _guids(client, 'GetLog') == []
            assert cur._last_operation_log is None

        # Status response, not the execute response, confirms that the final
        # select has a result set.
        assert third.handle.hasResultSet is False
        cur.execute(SELECT_TWO)
        assert cur.query_string == SELECT_TWO
        assert cur._last_operation.handle.operationId.guid == third.guid
        assert cur.has_result_set
        assert cur.description == third.description
        assert cur.description != first.description
        assert [column[0] for column in cur.description] == [
            'third_code', 'third_note']
        assert cur.rowcount == -1
        assert cur._last_operation_log is None
        assert len(cur._buffer) == 0
        assert _guids(client, 'GetOperationStatus')[-1] == third.guid
        assert _guids(client, 'GetResultSetMetadata') == [
            first.guid, third.guid]
        assert _guids(client, 'FetchResults') == [first.guid]

        if close_finished_queries:
            # The update was already closed when it finished.
            assert update.close_calls == 1
            assert update.guid not in _guids(client, 'FetchResults')
            assert _index_of(client, 'CloseImpalaOperation', update.guid) < \
                _index_of(client, 'ExecuteStatement', statement=SELECT_TWO)
        else:
            # Without auto-close the update is closed when it is replaced,
            # and the final select inherits none of its state.
            assert update.close_calls == 1
            assert _index_of(client, 'CloseImpalaOperation', update.guid) < \
                _index_of(client, 'ExecuteStatement', statement=SELECT_TWO)
            assert cur.rowcount == -1
            assert cur._last_operation_log is None
            assert _guids(client, 'GetLog') == []

        assert cur.get_log() == third.log
        assert third.log not in (first.log, update.log)
        if close_finished_queries:
            # The update log was fetched once, immediately before its close.
            assert _guids(client, 'GetLog') == [update.guid, third.guid]
        else:
            assert _guids(client, 'GetLog') == [third.guid]

        # Before exhaustion the final select still returns its own values and
        # fields, not rows or fields left over from the first select.
        third_first_row = cur.fetchone()
        assert third_first_row == ('30', 'thirty')
        assert third_first_row != row
        assert cur.description == third.description
        assert cur.rowcount == 1
        assert len(cur._buffer) == 1

        third_second_row = cur.fetchone()
        assert third_second_row == ('31', 'thirty-one')
        assert cur.description == third.description
        assert cur.rowcount == 2

        # The second fetch returns no rows and no more rows are expected.
        assert cur.fetchone() is None
        assert _guids(client, 'FetchResults') == [
            first.guid, third.guid, third.guid]

        if close_finished_queries:
            # The empty fetch saved the third log and closed its handle once.
            assert third.close_calls == 1
            assert _index_of(client, 'GetLog', third.guid) < \
                _index_of(client, 'CloseImpalaOperation', third.guid)
            _assert_log_saved_before_close(client, third.guid)
            assert cur.get_log() == third.log
        else:
            assert third.close_calls == 0
            assert cur.get_log() == third.log

        # Repeated empty fetches must not close an already-closed operation or
        # close the current operation more than once.
        assert cur.fetchone() is None
        assert first.close_calls == 1
        assert update.close_calls == 1
        if close_finished_queries:
            assert third.close_calls == 1
            assert _guids(client, 'FetchResults') == [
                first.guid, third.guid, third.guid]
        else:
            assert third.close_calls == 0
            assert _guids(client, 'FetchResults') == [
                first.guid, third.guid, third.guid, third.guid]

        cur.close()
        assert first.close_calls == 1
        assert update.close_calls == 1
        assert third.close_calls == 1
        assert _index_of(client, 'CloseImpalaOperation', third.guid) < \
            _index_of(client, 'CloseSession')

        assert _guids(client, 'GetResultSetMetadata') == [
            first.guid, third.guid]
        assert update.guid not in _guids(client, 'FetchResults')
        _assert_operation_identity(client, (first, update, third))
    finally:
        con.close()
