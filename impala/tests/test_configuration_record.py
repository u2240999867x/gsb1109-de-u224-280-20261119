# Licensed to the Apache Software Foundation (ASF) under one
# or more contributor license agreements.  See the NOTICE file
# distributed with this work for additional information
# regarding copyright ownership.  The ASF licenses this file
# to you under the Apache License, Version 2.0 (the
# "License"); you may not use this file except in compliance
# with the License.  You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing,
# software distributed under the License is distributed on an
# "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY
# KIND, either express or implied.  See the License for the
# specific language governing permissions and limitations
# under the License.

"""Offline tests for configuration provenance records."""

import copy

import pytest

from impala.configuration import UNKNOWN
from impala._thrift_gen.TCLIService.ttypes import (
    TOpenSessionResp, TExecuteStatementResp, TSessionHandle,
    TOperationHandle, THandleIdentifier, TStatus, TStatusCode,
    TGetOperationStatusResp, TOperationState, TGetLogResp,
    TCloseSessionResp, TCloseOperationResp)
from impala.hiveserver2 import HiveServer2Connection, HS2Service


class _Transport(object):
    def isOpen(self):
        return True

    def open(self):
        pass

    def close(self):
        pass


class _Protocol(object):
    def __init__(self):
        self.trans = _Transport()


class FakeThriftClient(object):
    """Records Thrift requests and returns canned server responses."""

    def __init__(self):
        self._iprot = _Protocol()
        self.requests = []
        self.session_configurations = []
        self.execute_failures = 0
        self._next_session = 0
        self._next_operation = 0

    def _status(self):
        return TStatus(statusCode=TStatusCode.SUCCESS_STATUS)

    def OpenSession(self, req):
        self.requests.append(('OpenSession', req))
        if self.session_configurations:
            configuration = self.session_configurations.pop(0)
        else:
            configuration = req.configuration
        self._next_session += 1
        handle = TSessionHandle(THandleIdentifier(
            guid=('session-%d' % self._next_session).encode('utf-8'),
            secret=b'secret'))
        return TOpenSessionResp(status=self._status(),
                                sessionHandle=handle,
                                configuration=configuration)

    def ExecuteStatement(self, req):
        self.requests.append(('ExecuteStatement', req))
        if self.execute_failures:
            self.execute_failures -= 1
            raise RuntimeError('injected submit failure')
        self._next_operation += 1
        handle = TOperationHandle(
            operationId=THandleIdentifier(
                guid=('operation-%d' % self._next_operation).encode('utf-8'),
                secret=b'secret'),
            operationType=0,
            hasResultSet=True)
        return TExecuteStatementResp(status=self._status(),
                                     operationHandle=handle)

    def GetOperationStatus(self, req):
        return TGetOperationStatusResp(
            status=self._status(),
            operationState=TOperationState.FINISHED_STATE,
            hasResultSet=False)

    def GetLog(self, req):
        return TGetLogResp(status=self._status(), log='')

    def CloseImpalaOperation(self, req):
        return TCloseOperationResp(status=self._status())

    def CloseSession(self, req):
        return TCloseSessionResp(status=self._status())

    def GetResultSetMetadata(self, req):
        raise AssertionError('not needed by these tests')


def _connection(client):
    service = HS2Service(client, retries=1)
    return HiveServer2Connection(service)


def _requests(client, method):
    return [req for name, req in client.requests if name == method]


def test_two_cursors_have_independent_session_defaults_and_overlays():
    client = FakeThriftClient()
    client.session_configurations = [
        {'session.key': 'server-a'},
        {'session.key': 'server-b'},
    ]
    con = _connection(client)

    cur_a = con.cursor(configuration={
        'session.key': 'client-a',
        'only.a': 'client-a'})
    cur_b = con.cursor(configuration={
        'session.key': 'client-b',
        'only.b': 'client-b'})

    assert _requests(client, 'OpenSession')[0].configuration == {
        'session.key': 'client-a', 'only.a': 'client-a'}
    assert _requests(client, 'OpenSession')[1].configuration == {
        'session.key': 'client-b', 'only.b': 'client-b'}

    overlay = {'session.key': 'overlay-a',
               'new.option': 'new',
               'paramstyle': 'qmark'}
    cur_a.execute('SELECT ?', parameters=['bound'], configuration=overlay)

    execute_req = _requests(client, 'ExecuteStatement')[0]
    assert execute_req.confOverlay == {
        'session.key': 'overlay-a', 'new.option': 'new'}
    assert 'paramstyle' not in execute_req.confOverlay
    assert execute_req.statement == "SELECT 'bound'"

    record = cur_a.last_configuration_record
    assert record.statement == "SELECT 'bound'"
    assert record.requested_session_configuration == {
        'session.key': 'client-a', 'only.a': 'client-a'}
    assert record.server_session_configuration == {
        'session.key': 'server-a'}
    assert record.statement_configuration == {
        'session.key': 'overlay-a', 'new.option': 'new'}
    assert record.client_configuration == {'paramstyle': 'qmark'}
    assert record.paramstyle == 'qmark'
    assert record.paramstyle_source == 'statement_configuration'

    session_key = record['session.key']
    assert session_key.requested_session == 'client-a'
    assert session_key.server_session == 'server-a'
    assert session_key.statement_overlay == 'overlay-a'
    assert session_key.unconfirmed
    assert session_key.effective_value is UNKNOWN

    only_a = record['only.a']
    assert only_a.status == 'server_omitted'
    assert only_a.omitted
    assert not only_a.server_confirmed

    new_option = record['new.option']
    assert new_option.status == 'statement_overlay_unconfirmed'
    assert new_option.session_status == 'server_omitted'
    assert new_option.overlay_unconfirmed
    assert new_option.unconfirmed_values == [
        ('statement_overlay', 'new')]

    paramstyle_entry = record['paramstyle']
    assert paramstyle_entry.status == 'client_only'
    assert not paramstyle_entry.unconfirmed
    assert 'paramstyle' not in record.unconfirmed_configuration

    cur_b.execute('SELECT 1')
    record_b = cur_b.last_configuration_record
    assert record_b.server_session_configuration == {
        'session.key': 'server-b'}
    assert record_b.statement_configuration == {}
    assert record_b['session.key'].statement_overlay is UNKNOWN
    assert record_b.server_session_reported

    # The override and the binding option stayed on cursor A.
    assert cur_b.last_configuration_record.paramstyle is None
    assert len(cur_a.configuration_records) == 1
    assert len(cur_b.configuration_records) == 1
    assert cur_a.session is not cur_b.session
    assert con.configuration_records == tuple(
        cur_a.configuration_records + cur_b.configuration_records)


def test_overlay_is_not_applied_to_the_next_statement():
    client = FakeThriftClient()
    client.session_configurations = [
        {'session.key': 'server-a'}]
    con = _connection(client)
    cur = con.cursor(configuration={'session.key': 'client-a'})

    cur.execute('SELECT 1', configuration={'new.option': 'new'})
    cur.execute('SELECT 2')

    requests = _requests(client, 'ExecuteStatement')
    assert requests[0].confOverlay == {'new.option': 'new'}
    assert requests[1].confOverlay is None

    first, second = cur.configuration_records
    assert first.statement_configuration == {'new.option': 'new'}
    assert second.statement_configuration == {}
    assert second.requested_session_configuration == {
        'session.key': 'client-a'}
    assert second.server_session_configuration == {
        'session.key': 'server-a'}


def test_mutating_caller_dictionaries_does_not_rewrite_history():
    client = FakeThriftClient()
    client.session_configurations = [{'session.key': 'server-a'}]
    con = _connection(client)
    session_configuration = {'session.key': 'client-a'}
    cur = con.cursor(configuration=session_configuration)
    session_configuration['session.key'] = 'mutated-after-open'

    overlay = {'mt_dop': '2', 'paramstyle': 'qmark'}
    cur.execute('SELECT ?', parameters=[1], configuration=overlay)
    record = cur.last_configuration_record
    overlay['mt_dop'] = '8'
    overlay['paramstyle'] = 'numeric'

    assert record.statement_configuration == {'mt_dop': '2'}
    assert record.paramstyle == 'qmark'
    assert record.requested_session_configuration == {
        'session.key': 'client-a'}
    assert cur.session.requested_configuration == {
        'session.key': 'client-a'}


def test_failed_submit_keeps_previous_successful_record():
    client = FakeThriftClient()
    client.session_configurations = [{'session.key': 'server-a'}]
    con = _connection(client)
    cur = con.cursor(configuration={'session.key': 'client-a'})

    cur.execute('SELECT 1', configuration={'new.option': 'new'})
    good = cur.last_configuration_record
    snapshot = copy.deepcopy(good.as_dict())

    client.execute_failures = 1
    with pytest.raises(RuntimeError):
        cur.execute('SELECT 2', configuration={'other.option': 'other'})

    assert cur.last_configuration_record is good
    assert cur.last_configuration_record.as_dict() == snapshot
    assert len(cur.configuration_records) == 1
    assert cur.last_failed_configuration is not None
    assert not cur.last_failed_configuration.submitted
    assert cur.last_failed_configuration.statement_configuration == {
        'other.option': 'other'}
    assert cur.last_failed_configuration.statement_configuration is not good

    # A later successful submit only appends its own record.
    cur.execute('SELECT 3')
    assert len(cur.configuration_records) == 2
    assert cur.configuration_records[0] is good
    assert cur.last_failed_configuration is None
    assert cur.last_configuration_record.statement_configuration == {}


def test_paramstyle_reaches_binding_but_not_the_wire():
    client = FakeThriftClient()
    client.session_configurations = [{'session.key': 'server-a'}]
    con = _connection(client)
    cur = con.cursor(configuration={
        'session.key': 'client-a', 'paramstyle': 'format'})

    cur.execute('SELECT ?, %s', parameters=['value'],
                configuration={'paramstyle': 'qmark'})
    request = _requests(client, 'ExecuteStatement')[0]

    assert not request.confOverlay
    assert 'paramstyle' not in _requests(client, 'OpenSession')[0].configuration
    assert request.statement == "SELECT 'value', %s"

    record = cur.last_configuration_record
    assert record.paramstyle == 'qmark'
    assert record.paramstyle_source == 'statement_configuration'

    cur.execute('SELECT %s', parameters=['again'])
    second = _requests(client, 'ExecuteStatement')[1]
    assert second.statement == "SELECT 'again'"
    assert not second.confOverlay
    assert cur.last_configuration_record.paramstyle == 'format'
    assert cur.last_configuration_record.paramstyle_source == \
        'cursor_configuration'


def test_session_change_and_omission_are_reported_per_key():
    client = FakeThriftClient()
    client.session_configurations = [
        {'changed': 'server-value'}]
    con = _connection(client)
    cur = con.cursor(configuration={
        'changed': 'requested-value',
        'omitted': 'requested-only'})

    cur.execute('SELECT 1')
    record = cur.last_configuration_record

    assert record.changed_session_configuration == {
        'changed': {
            'requested': 'requested-value',
            'server_reported': 'server-value'}}
    assert record.unconfirmed_session_configuration == {
        'omitted': 'requested-only'}
    assert record.omitted_session_configuration == {
        'omitted': 'requested-only'}
    assert record.unreported_session_configuration == {}
    assert record['changed'].status == 'server_reported_different_value'
    # The server returned a session map but did not mention this key.
    assert record['omitted'].status == 'server_omitted'
    assert record['omitted'].omitted
    assert not record['omitted'].server_confirmed
    assert record.effective_configuration['changed'] is UNKNOWN
    assert record.as_dict()['effective_configuration']['changed'] == \
        'unknown'


def test_absent_session_response_map_is_not_treated_as_confirmation():
    client = FakeThriftClient()
    client.session_configurations = [None]
    con = _connection(client)
    cur = con.cursor(configuration={'requested': 'value'})

    cur.execute('SELECT 1')
    record = cur.last_configuration_record

    assert not record.server_session_reported
    assert record.server_session_configuration == {}
    assert record['requested'].status == 'server_not_reported'
    assert not record['requested'].omitted
    assert record.unconfirmed_session_configuration == {
        'requested': 'value'}
    assert record.omitted_session_configuration == {}
    assert record.unreported_session_configuration == {
        'requested': 'value'}


def test_records_bind_their_own_session_and_operation():
    client = FakeThriftClient()
    client.session_configurations = [
        {'session.key': 'server-a'}, {'session.key': 'server-b'}]
    con = _connection(client)
    cur_a = con.cursor(configuration={'session.key': 'client-a'})
    cur_b = con.cursor(configuration={'session.key': 'client-b'})

    cur_a.execute('SELECT 1')
    cur_b.execute('SELECT 2')

    record_a = cur_a.last_configuration_record
    record_b = cur_b.last_configuration_record
    assert record_a.session is cur_a.session
    assert record_b.session is cur_b.session
    assert record_a.operation is not record_b.operation
    assert record_a.session_handle is cur_a.session.handle
    assert record_b.session_handle is cur_b.session.handle
    assert record_a.operation_handle is record_a.operation.handle
    assert record_a.operation.configuration_record is record_a
    assert record_b.operation.configuration_record is record_b
    assert record_a.session_id != record_b.session_id
    assert record_a.operation_id != record_b.operation_id
    assert record_a.entries['session.key'].server_session == 'server-a'
    assert record_b.entries['session.key'].server_session == 'server-b'

    cur_a.execute('SELECT 3')
    first, second = cur_a.configuration_records
    assert first != second
    assert len({first, second}) == 2
    assert cur_a.session.configuration_records == (first, second)
    assert con.session_configuration_records == (
        cur_a.session.configuration_record, cur_b.session.configuration_record)


def test_session_map_matching_overlay_is_still_not_confirmation():
    client = FakeThriftClient()
    client.session_configurations = [{'session.key': 'same-value'}]
    con = _connection(client)
    cur = con.cursor(configuration={'session.key': 'same-value'})

    cur.execute('SELECT 1', configuration={'session.key': 'same-value'})
    entry = cur.last_configuration_record['session.key']

    assert entry.requested_session == 'same-value'
    assert entry.server_session == 'same-value'
    assert entry.statement_overlay == 'same-value'
    assert entry.session_status == 'server_reported'
    assert entry.status == 'statement_overlay_unconfirmed'
    assert entry.overlay_unconfirmed


def test_failed_submit_is_not_described_as_successful():
    client = FakeThriftClient()
    client.session_configurations = [{'session.key': 'server-a'}]
    con = _connection(client)
    cur = con.cursor(configuration={'session.key': 'client-a'})

    client.execute_failures = 1
    with pytest.raises(RuntimeError):
        cur.execute('SELECT 1', configuration={'new.option': 'new'})

    failed = cur.last_failed_configuration
    assert failed is not None
    assert failed.failed
    assert not failed.submitted_successfully
    assert failed.as_dict()['submitted'] is False
    assert failed.as_dict()['successful'] is False
    assert cur.last_configuration_record is None
    assert cur.configuration_records == ()
    assert failed.statement_configuration == {'new.option': 'new'}
    assert failed['session.key'].server_session == 'server-a'


def test_overlay_does_not_leak_between_cursors():
    client = FakeThriftClient()
    client.session_configurations = [
        {'session.key': 'server-a'}, {'session.key': 'server-b'}]
    con = _connection(client)
    cur_a = con.cursor(configuration={'session.key': 'client-a'})
    cur_b = con.cursor(configuration={'session.key': 'client-b'})

    cur_a.execute('SELECT 1', configuration={'only.on.a': 'a'})
    cur_b.execute('SELECT 2')

    assert _requests(client, 'ExecuteStatement')[1].confOverlay is None
    assert cur_a.last_configuration_record.statement_configuration == {
        'only.on.a': 'a'}
    assert cur_b.last_configuration_record.statement_configuration == {}
    assert 'only.on.a' not in cur_b.last_configuration_record


def test_full_verification_sequence_matches_the_offline_requests():
    client = FakeThriftClient()
    client.session_configurations = [
        {'session.key': 'server-default-a',
         'server.changed': 'server-value'},
        {'session.key': 'server-default-b'},
    ]
    con = _connection(client)

    caller_configuration = {
        'session.key': 'caller-default-a',
        'server.changed': 'requested-value',
        'omitted.key': 'not-reported',
        'caller.only': 'caller-value',
    }
    cur_a = con.cursor(configuration=caller_configuration)
    cur_b = con.cursor(configuration={'session.key': 'caller-default-b'})

    open_requests = _requests(client, 'OpenSession')
    assert open_requests[0].configuration == {
        'session.key': 'caller-default-a',
        'server.changed': 'requested-value',
        'omitted.key': 'not-reported',
        'caller.only': 'caller-value'}
    assert open_requests[1].configuration == {
        'session.key': 'caller-default-b'}

    first_overlay = {
        'session.key': 'overlay-a',
        'brand.new': 'new-value',
        'paramstyle': 'qmark',
    }
    cur_a.execute('SELECT ?', parameters=['submitted'], configuration=first_overlay)
    first_record = cur_a.last_configuration_record

    # Callers are free to mutate their dictionaries after submission.
    caller_configuration['session.key'] = 'caller-mutated'
    caller_configuration['server.changed'] = 'caller-mutated'
    first_overlay['session.key'] = 'overlay-mutated'
    first_overlay['paramstyle'] = 'numeric'

    assert first_record.requested_session_configuration == {
        'session.key': 'caller-default-a',
        'server.changed': 'requested-value',
        'omitted.key': 'not-reported',
        'caller.only': 'caller-value'}
    assert first_record.server_session_configuration == {
        'session.key': 'server-default-a',
        'server.changed': 'server-value'}
    assert first_record.statement_configuration == {
        'session.key': 'overlay-a', 'brand.new': 'new-value'}
    assert first_record.paramstyle == 'qmark'

    first = _requests(client, 'ExecuteStatement')[0]
    assert first.confOverlay == {
        'session.key': 'overlay-a', 'brand.new': 'new-value'}
    assert first.statement == "SELECT 'submitted'"

    cur_b.execute('SELECT 2')
    second_record = cur_b.last_configuration_record
    assert second_record.requested_session_configuration == {
        'session.key': 'caller-default-b'}
    assert second_record.statement_configuration == {}
    assert second_record['session.key'].statement_overlay is UNKNOWN

    cur_a.execute('SELECT 3')
    third_record = cur_a.last_configuration_record
    assert third_record.statement_configuration == {}
    assert third_record.requested_session_configuration == {
        'session.key': 'caller-default-a',
        'server.changed': 'requested-value',
        'omitted.key': 'not-reported',
        'caller.only': 'caller-value'}
    assert third_record['session.key'].server_session == 'server-default-a'
    assert _requests(client, 'ExecuteStatement')[2].confOverlay is None

    client.execute_failures = 1
    with pytest.raises(RuntimeError):
        cur_a.execute('SELECT 4', configuration={'failed.option': 'x'})
    assert cur_a.last_configuration_record is third_record
    assert len(cur_a.configuration_records) == 2
    assert not cur_a.last_failed_configuration.submitted

    # Same key, three observed origins, and the server only actually
    # confirmed one of them.
    key_entry = third_record['session.key']
    assert key_entry.requested_session == 'caller-default-a'
    assert key_entry.server_session == 'server-default-a'
    assert key_entry.statement_overlay is UNKNOWN
    assert key_entry.unconfirmed_values == [
        ('requested_session', 'caller-default-a')]
    assert third_record.effective_configuration['session.key'] is UNKNOWN
    assert 'paramstyle' not in third_record.unconfirmed_configuration
