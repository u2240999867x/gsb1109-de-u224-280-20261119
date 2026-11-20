# Copyright 2024 Cloudera Inc.
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

"""Offline replay of HiveServer2 ``CancelOperation`` semantics.

The on-call symptom is: the cancel request comes back successful, but rows can
no longer be read afterwards. These tests reproduce that symptom against two
*different* server terminal states plus a failing cancel RPC, replaying each
sequence with both cancellation styles exposed by impyla:

* ``cursor.cancel_operation(reset_state=True)``  (the default) -- after the
  ``CancelOperation`` RPC succeeds, ``_reset_state()`` wipes the local row
  buffer, drops ``_last_operation`` and sends ``CloseImpalaOperation`` itself.
* ``cursor.cancel_operation(reset_state=False)`` -- only the
  ``CancelOperation`` RPC is sent; the handle and the local buffer survive,
  which leaves an observation window in which server state and remaining rows
  can still be probed.

Everything runs against an in-process fake HiveServer2 service but goes through
the real ``HS2Service`` / ``HiveServer2Cursor`` code paths, so the recorded RPC
order, buffer contents and exceptions are those of production impyla.

Run the printed evidence report with::

    python -m impala.tests.test_cancel_replay
"""

import pytest
from thrift.transport.TTransport import TTransportException

from impala import hiveserver2 as hs2
from impala.error import HiveServer2Error
from impala._thrift_gen.ImpalaService.ttypes import TCloseImpalaOperationResp
from impala._thrift_gen.TCLIService.ttypes import (
    TCancelOperationResp, TCloseSessionResp, TColumn, TColumnDesc,
    TExecuteStatementResp, TFetchResultsResp, TGetLogResp,
    TGetOperationStatusResp, TGetResultSetMetadataResp, THandleIdentifier,
    TOpenSessionResp, TOperationHandle, TOperationState, TOperationType,
    TPrimitiveTypeEntry, TRowSet, TSessionHandle, TStatus, TStatusCode,
    TStringColumn, TTableSchema, TTypeEntry, TTypeId, TTypeDesc)

# The three simulated services.
RUNNING_THEN_CANCELED = 'running_then_canceled'
FINISHED_IDEMPOTENT_CANCEL = 'finished_idempotent_cancel'
CANCEL_RPC_ERROR = 'cancel_rpc_error'

STATE_NAMES = TOperationState._VALUES_TO_NAMES


def _tstatus(code=TStatusCode.SUCCESS_STATUS, message=None):
    return TStatus(statusCode=code, errorMessage=message)


def _operation_handle():
    return TOperationHandle(
        operationId=THandleIdentifier(guid=b'operation-guid',
                                      secret=b'secret'),
        operationType=TOperationType.EXECUTE_STATEMENT,
        hasResultSet=True)


def _string_column(values):
    # One null-bitmap byte covers eight rows; all rows are non-null.
    return TColumn(stringVal=TStringColumn(values=values, nulls=b'\x00'))


def _fetch_response(values, has_more_rows):
    return TFetchResultsResp(
        status=_tstatus(),
        hasMoreRows=has_more_rows,
        results=TRowSet(startRowOffset=0, rows=[],
                        columns=[_string_column(values)]))


class FakeTransport(object):
    def isOpen(self):
        return True

    def open(self):
        pass

    def close(self):
        pass


class FakeClient(object):
    """Minimal thrift client: ``ThriftRPC`` only needs ``_iprot.trans`` and
    callable RPC methods named after the HiveServer2 operations."""

    def __init__(self, server):
        self._iprot = type('Protocol', (), {'trans': FakeTransport()})()
        self._server = server

    def __getattr__(self, rpc_name):
        return getattr(self._server, rpc_name)


class FakeHS2Server(object):
    """In-process HiveServer2 with an explicit, inspectable terminal state.

    Result layout shared by every scenario:

    * first ``FetchResults`` batch: r1, r2, r3 (hasMoreRows=True)
    * second batch: r4, r5 (hasMoreRows=False)
    * further fetches: empty batch
    """

    def __init__(self, mode):
        self.mode = mode
        self.rpc_log = []
        self.handle = _operation_handle()
        self.handle_open = True
        self.fetch_calls = 0
        self.state_history = []
        if mode == FINISHED_IDEMPOTENT_CANCEL:
            self._state = TOperationState.FINISHED_STATE
        else:
            self._state = TOperationState.RUNNING_STATE
        self.state_history.append(STATE_NAMES[self._state])

    # -- server-side truth, independent of any client cursor -----------------
    @property
    def state(self):
        return self._state

    def _set_state(self, new_state):
        self._state = new_state
        self.state_history.append(STATE_NAMES[new_state])

    def _require_open_handle(self):
        if not self.handle_open:
            # What HiveServer2 returns once the operation has been closed.
            return TStatus(statusCode=TStatusCode.ERROR_STATUS,
                           errorMessage='Invalid operation handle')
        return None

    # -- RPCs ----------------------------------------------------------------
    def OpenSession(self, req):
        self.rpc_log.append('OpenSession')
        return TOpenSessionResp(
            status=_tstatus(),
            serverProtocolVersion=5,  # HIVE_CLI_SERVICE_PROTOCOL_V6
            sessionHandle=TSessionHandle(
                THandleIdentifier(guid=b'session', secret=b's')),
            configuration={})

    def ExecuteStatement(self, req):
        self.rpc_log.append('ExecuteStatement')
        return TExecuteStatementResp(status=_tstatus(),
                                     operationHandle=self.handle)

    def GetOperationStatus(self, req):
        self.rpc_log.append('GetOperationStatus')
        bad = self._require_open_handle()
        if bad is not None:
            return TGetOperationStatusResp(status=bad)
        return TGetOperationStatusResp(
            status=_tstatus(), operationState=self.state, hasResultSet=True)

    def GetResultSetMetadata(self, req):
        self.rpc_log.append('GetResultSetMetadata')
        column = TColumnDesc(
            columnName='id',
            typeDesc=TTypeDesc(types=[TTypeEntry(primitiveEntry=
                TPrimitiveTypeEntry(type=TTypeId.STRING_TYPE))]),
            position=0)
        return TGetResultSetMetadataResp(
            status=_tstatus(), schema=TTableSchema(columns=[column]))

    def FetchResults(self, req):
        self.rpc_log.append('FetchResults')
        bad = self._require_open_handle()
        if bad is not None:
            return TFetchResultsResp(status=bad)
        if self.state == TOperationState.CANCELED_STATE:
            return TFetchResultsResp(status=TStatus(
                statusCode=TStatusCode.ERROR_STATUS,
                errorMessage='Operation was canceled'))
        self.fetch_calls += 1
        if self.fetch_calls == 1:
            return _fetch_response([b'r1', b'r2', b'r3'], True)
        if self.fetch_calls == 2:
            return _fetch_response([b'r4', b'r5'], False)
        return _fetch_response([], False)

    def CancelOperation(self, req):
        self.rpc_log.append('CancelOperation')
        if self.mode == CANCEL_RPC_ERROR:
            # Transport-level failure: the request never changes server state
            # and ThriftRPC surfaces the exception after exhausting retries.
            raise TTransportException(
                type=TTransportException.NOT_OPEN,
                message='injected cancel transport failure')
        if self.mode == RUNNING_THEN_CANCELED:
            self._set_state(TOperationState.CANCELED_STATE)
        # FINISHED_IDEMPOTENT_CANCEL: HiveServer2 answers SUCCESS for an
        # already-finished operation without changing anything.
        return TCancelOperationResp(status=_tstatus())

    def CloseImpalaOperation(self, req):
        self.rpc_log.append('CloseImpalaOperation')
        self.handle_open = False
        return TCloseImpalaOperationResp(status=_tstatus(), dml_result=None)

    def GetLog(self, req):
        self.rpc_log.append('GetLog')
        return TGetLogResp(status=_tstatus(), log='')

    def CloseSession(self, req):
        self.rpc_log.append('CloseSession')
        return TCloseSessionResp(status=_tstatus())


def build_cursor(mode):
    server = FakeHS2Server(mode)
    service = hs2.HS2Service(FakeClient(server), retries=1)
    connection = hs2.HiveServer2Connection(service, default_db=None)
    cursor = connection.cursor()
    return server, cursor


def prime_cursor(cursor):
    """Execute, fetch the first batch into the local buffer and consume r1.

    After this the cursor looks like the on-call case: one row has been
    delivered, r2/r3 sit in the local cursor buffer and r4/r5 remain on the
    server side behind the same handle.
    """
    cursor.execute_async('select id from t')
    schema = cursor._last_operation.get_result_schema()
    cursor._description = schema
    cursor._buffer = cursor._last_operation.fetch(schema, cursor.buffersize)
    delivered = cursor._buffer.pop()  # r1
    return delivered


def _outcome(call):
    try:
        return 'ok', call()
    except Exception as exc:  # noqa: BLE001 - the exception type is evidence
        return 'error', '%s: %s' % (type(exc).__name__, exc)


def _peek_buffer(cursor):
    # Non-destructive read: CBatch columns keep their full value list and only
    # move rows_left, so remaining values can be inspected without popping.
    batch = cursor._buffer
    if cursor._last_operation is None or len(batch) == 0:
        return []
    column = batch.columns[0]
    start = column.num_rows - column.rows_left
    return list(column.values[start:start + column.rows_left])


def replay(mode, reset_state):
    server, cursor = build_cursor(mode)
    delivered = prime_cursor(cursor)
    handle = cursor._last_operation.handle
    buffered_before = _peek_buffer(cursor)
    state_before = STATE_NAMES[server.state]

    cancel_kind, cancel_value = _outcome(
        lambda: cursor.cancel_operation(reset_state=reset_state))

    evidence = {
        'mode': mode,
        'reset_state': reset_state,
        'delivered_row': delivered[0],
        'buffered_before_cancel': buffered_before,
        'server_state_before': state_before,
        'server_state_history': list(server.state_history),
        'cancel_outcome': cancel_kind,
        'cancel_value': cancel_value,
        'rpc_log': list(server.rpc_log),
        'server_handle_open_after': server.handle_open,
        'client_active': cursor._last_operation_active,
        'client_has_operation': cursor._last_operation is not None,
        'client_buffer_len_after': len(cursor._buffer),
    }

    if cursor._last_operation is not None:
        evidence['buffered_after_cancel'] = _peek_buffer(cursor)
        kind, value = _outcome(cursor._last_operation.get_status)
        evidence['post_cancel_status'] = value
        schema = cursor._description
        kind, value = _outcome(
            lambda: cursor._last_operation.fetch(schema, cursor.buffersize))
        if kind == 'ok':
            evidence['post_cancel_server_fetch'] = [
                row[0] for row in value.pop_many(len(value))]
        else:
            evidence['post_cancel_server_fetch'] = value
    else:
        # Default cancel destroyed the local operation object.
        evidence['buffered_after_cancel'] = []
        evidence['post_cancel_status'] = 'no local operation object'
        evidence['post_cancel_server_fetch'] = 'no local operation object'

    # What the default DB-API row path returns afterwards. fetchone() first
    # blocks in _wait_to_finish(), which would loop forever on a query that is
    # genuinely still RUNNING; __next__() is the exact step fetchone() runs
    # after the wait and is non-blocking: it serves the local buffer when rows
    # remain and raises immediately once the operation object is gone.
    kind, value = _outcome(lambda: next(cursor))
    evidence['default_fetch_row'] = value if kind == 'ok' else value

    # What another consumer asking about the same handle would get from the
    # server. A fresh Operation object is built on the same session/handle.
    probe = hs2.Operation(cursor.session, handle, retries=1)
    kind, value = _outcome(probe.get_status)
    evidence['handle_probe_status'] = value

    return evidence


# ---------------------------------------------------------------------------
# Scenario A: query still running, cancellation confirmed by the server
# ---------------------------------------------------------------------------

def test_running_query_default_cancel_wipes_local_state():
    evidence = replay(RUNNING_THEN_CANCELED, reset_state=True)

    assert evidence['cancel_outcome'] == 'ok'
    assert evidence['server_state_history'] == ['RUNNING_STATE',
                                                'CANCELED_STATE']
    # The client itself closed the handle after the successful cancel RPC.
    assert evidence['rpc_log'][-2:] == ['CancelOperation',
                                        'CloseImpalaOperation']
    assert evidence['server_handle_open_after'] is False
    assert evidence['client_has_operation'] is False
    assert evidence['client_active'] is False
    assert evidence['client_buffer_len_after'] == 0
    assert evidence['buffered_after_cancel'] == []
    # Both the local fetch and a fresh handle probe now fail: no rows.
    assert 'Error' in evidence['default_fetch_row']
    assert 'Invalid operation handle' in evidence['handle_probe_status']


def test_running_query_preserved_cancel_keeps_only_local_cache():
    evidence = replay(RUNNING_THEN_CANCELED, reset_state=False)

    assert evidence['cancel_outcome'] == 'ok'
    # Server-side truth: genuinely canceled.
    assert evidence['post_cancel_status'] == 'CANCELED_STATE'
    assert evidence['rpc_log'][-1] == 'CancelOperation'
    assert 'CloseImpalaOperation' not in evidence['rpc_log']
    assert evidence['server_handle_open_after'] is True
    # r2/r3 survive only because they were cached in the local cursor.
    assert evidence['buffered_after_cancel'] == ['r2', 'r3']
    # The server cannot supply anything more: r4/r5 are unreachable.
    assert 'Operation was canceled' in evidence['post_cancel_server_fetch']
    assert evidence['handle_probe_status'] == 'CANCELED_STATE'


# ---------------------------------------------------------------------------
# Scenario B: query already finished; cancel is an idempotent success no-op
# ---------------------------------------------------------------------------

def test_finished_query_default_cancel_loses_rows_locally():
    evidence = replay(FINISHED_IDEMPOTENT_CANCEL, reset_state=True)

    assert evidence['cancel_outcome'] == 'ok'
    # The server never left FINISHED: the cancel did not cancel anything.
    assert evidence['server_state_history'] == ['FINISHED_STATE']
    assert evidence['rpc_log'][-2:] == ['CancelOperation',
                                        'CloseImpalaOperation']
    assert evidence['server_handle_open_after'] is False
    assert evidence['client_has_operation'] is False
    assert evidence['client_buffer_len_after'] == 0
    assert 'Error' in evidence['default_fetch_row']
    assert 'Invalid operation handle' in evidence['handle_probe_status']


def test_finished_query_preserved_cancel_still_serves_all_remaining_rows():
    evidence = replay(FINISHED_IDEMPOTENT_CANCEL, reset_state=False)

    assert evidence['cancel_outcome'] == 'ok'
    # Observation point: handle kept, server state still FINISHED.
    assert evidence['post_cancel_status'] == 'FINISHED_STATE'
    assert evidence['server_handle_open_after'] is True
    # Local cursor cache is intact.
    assert evidence['buffered_after_cancel'] == ['r2', 'r3']
    # The completed query's remaining rows are still fetched from the server.
    assert evidence['post_cancel_server_fetch'] == ['r4', 'r5']
    assert evidence['handle_probe_status'] == 'FINISHED_STATE'


# ---------------------------------------------------------------------------
# The two externally identical controls
# ---------------------------------------------------------------------------

def test_default_cancel_controls_look_identical_from_the_outside():
    truly_canceled = replay(RUNNING_THEN_CANCELED, reset_state=True)
    already_finished = replay(FINISHED_IDEMPOTENT_CANCEL, reset_state=True)

    def external_surface(evidence):
        return (
            evidence['cancel_outcome'],          # both: ok
            evidence['client_has_operation'],    # both: False
            evidence['client_buffer_len_after'], # both: 0
            'Error' in evidence['default_fetch_row'],     # both: True
            'Invalid operation handle' in
            evidence['handle_probe_status'],     # both: True
            evidence['rpc_log'][-2:],            # Cancel + Close in both
        )

    assert external_surface(truly_canceled) == external_surface(
        already_finished)

    # The server-side truth captured *before* the client closed the handle is
    # the only thing that separates the two controls.
    assert truly_canceled['server_state_history'] == [
        'RUNNING_STATE', 'CANCELED_STATE']
    assert already_finished['server_state_history'] == ['FINISHED_STATE']


# ---------------------------------------------------------------------------
# Scenario C: the CancelOperation RPC itself fails
# ---------------------------------------------------------------------------

def test_cancel_rpc_error_leaves_client_and_server_untouched():
    for reset_state in (True, False):
        evidence = replay(CANCEL_RPC_ERROR, reset_state=reset_state)

        assert evidence['cancel_outcome'] == 'error'
        assert 'TTransportException' in evidence['cancel_value']
        # No CloseImpalaOperation is ever attempted on a failed cancel.
        assert evidence['rpc_log'][-1] == 'CancelOperation'
        assert 'CloseImpalaOperation' not in evidence['rpc_log']
        # retries=1 means exactly one cancel attempt.
        assert evidence['rpc_log'].count('CancelOperation') == 1
        # Client state is fully preserved regardless of reset_state, because
        # _reset_state() runs only after a successful cancel().
        assert evidence['client_has_operation'] is True
        assert evidence['client_active'] is True
        assert evidence['client_buffer_len_after'] == 2
        assert evidence['buffered_after_cancel'] == ['r2', 'r3']
        # Server never moved: the operation is still running and fetchable.
        assert evidence['post_cancel_status'] == 'RUNNING_STATE'
        # A direct server fetch advances the server cursor to batch two; it is
        # independent of the r2/r3 still held in the local cursor buffer.
        assert evidence['post_cancel_server_fetch'] == ['r4', 'r5']
        assert evidence['handle_probe_status'] == 'RUNNING_STATE'
        assert evidence['server_handle_open_after'] is True
        # The default DB-API fetch path keeps working off the local buffer.
        assert evidence['default_fetch_row'] == ('r2',)


# ---------------------------------------------------------------------------
# Human-readable report: ``python -m impala.tests.test_cancel_replay``
# ---------------------------------------------------------------------------

def _print_report():
    scenarios = [
        ('A really canceled (was RUNNING)', RUNNING_THEN_CANCELED),
        ('B already FINISHED, idempotent cancel', FINISHED_IDEMPOTENT_CANCEL),
        ('C cancel RPC transport error', CANCEL_RPC_ERROR),
    ]
    for title, mode in scenarios:
        for reset_state in (True, False):
            evidence = replay(mode, reset_state)
            print('=' * 78)
            print('%s | cancel_operation(reset_state=%s)' % (
                title, reset_state))
            print('-' * 78)
            print('  delivered before cancel : %s'
                  % evidence['delivered_row'])
            print('  local buffer before      : %s  [client-side evidence]'
                  % evidence['buffered_before_cancel'])
            print('  server state before      : %s  [server evidence]'
                  % evidence['server_state_before'])
            print('  server state history     : %s  [server evidence]'
                  % evidence['server_state_history'])
            print('  RPC order                : %s' % evidence['rpc_log'])
            print('  cancel RPC outcome       : %s (%s)' % (
                evidence['cancel_outcome'], evidence['cancel_value']))
            print('  server handle open after : %s  [server evidence]'
                  % evidence['server_handle_open_after'])
            print('  client has operation     : %s  [client-side evidence]'
                  % evidence['client_has_operation'])
            print('  client buffer len after  : %s  [client-side evidence]'
                  % evidence['client_buffer_len_after'])
            print('  buffered rows after      : %s  [client-side evidence]'
                  % evidence['buffered_after_cancel'])
            status_origin = ('client-side evidence'
                             if evidence['post_cancel_status'] ==
                             'no local operation object'
                             else 'server evidence')
            print('  status after cancel      : %s  [%s]' % (
                evidence['post_cancel_status'], status_origin))
            print('  server fetch after cancel: %s'
                  % evidence['post_cancel_server_fetch'])
            print('  default next()/fetch row : %s  [client-side evidence]'
                  % evidence['default_fetch_row'])
            print('  fresh handle probe       : %s  [server evidence]'
                  % evidence['handle_probe_status'])
    print('=' * 78)


if __name__ == '__main__':
    _print_report()
