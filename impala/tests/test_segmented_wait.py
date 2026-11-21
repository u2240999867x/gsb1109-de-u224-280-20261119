"""Offline tests for segmented, budgeted waits on HS2 operations.

Every status response comes from local fakes. No socket is opened and no
external Hive/Impala service is required.
"""

import time

import pytest

from thrift.transport.TTransport import TTransportException

from impala import hiveserver2 as hs2
from impala._thrift_gen.TCLIService.ttypes import (
    TOperationState, TStatusCode, TStatus, TGetOperationStatusResp)
from impala.error import HttpError, OperationalError, WaitBudgetUnsupported
from impala.hiveserver2 import (
    HS2Session, WaitReceipt, _wait_for_operation)


class FakeTransport(object):
    """Transport double with real settable deadlines."""

    TIMED_OUT = TTransportException.TIMED_OUT

    def __init__(self, supports_timeout=True):
        self.supports_timeout = supports_timeout
        self.timeout = None
        self.opened = False
        self.close_count = 0
        self.open_count = 0

    def setTimeout(self, ms):
        if not self.supports_timeout:
            raise AssertionError('setTimeout should not be called')
        self.timeout = None if ms is None else ms / 1000.0

    def isOpen(self):
        return self.opened

    def open(self):
        self.open_count += 1
        self.opened = True

    def close(self):
        self.close_count += 1
        self.opened = False


class NoTimeoutTransport(object):
    """Transport double without any timeout support (has only open/close)."""

    def isOpen(self):
        return True

    def open(self):
        pass

    def close(self):
        pass


class FakeClient(object):
    def __init__(self, responses, transport=None, retryable_errors=None,
                 events=None):
        self.responses = list(responses)
        self.retryable_errors = list(retryable_errors or [])
        self.events = events if events is not None else []
        self.calls = []
        self.execute_calls = []
        trans = transport if transport is not None else FakeTransport()
        self._iprot = type('Protocol', (), {'trans': trans})()

    def ExecuteStatement(self, req):
        self.events.append(('submit', req.statement))
        self.execute_calls.append(req)
        return type('ExecuteResp', (), {
            'status': TStatus(statusCode=TStatusCode.SUCCESS_STATUS),
            'operationHandle': FakeHandle('executed')})()

    def CloseImpalaOperation(self, req):
        self.events.append(
            ('close', req.operationHandle.operationId.guid))
        return type('CloseResp', (), {'status': TStatus(
            statusCode=TStatusCode.SUCCESS_STATUS)})()

    def CancelOperation(self, req):
        self.events.append(
            ('cancel', req.operationHandle.operationId.guid))
        return type('CancelResp', (), {'status': TStatus(
            statusCode=TStatusCode.SUCCESS_STATUS)})()

    def GetOperationStatus(self, req):
        self.calls.append(req)
        if self.retryable_errors:
            error = self.retryable_errors.pop(0)
            if error is not None:
                raise error
        if not self.responses:
            return _status_response('RUNNING_STATE')
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class FakeHandle(object):
    def __init__(self, name, has_result_set=True):
        self.operationId = type('OperationId', (), {
            'guid': name.encode('utf-8'),
            'secret': ('secret-' + name).encode('utf-8')})()
        self.operationType = 0
        self.hasResultSet = has_result_set


def _status_response(state, has_result_set=None, error_message=None):
    return TGetOperationStatusResp(
        status=TStatus(statusCode=TStatusCode.SUCCESS_STATUS),
        operationState=TOperationState._NAMES_TO_VALUES[state],
        hasResultSet=has_result_set,
        errorMessage=error_message)


def _session_with(client):
    service = type('Service', (), {'client': client, 'retries': 3})()
    return HS2Session(service, None, None, 6, retries=3)


def _operation(session, handle):
    return hs2.Operation(session, handle, retries=session.retries)


class TestWaitReceipt(object):
    def test_matches_same_operation_after_cursor_moves_on(self):
        client = FakeClient([], transport=NoTimeoutTransport())
        session = _session_with(client)
        first = _operation(session, FakeHandle('one'))
        second = _operation(session, FakeHandle('two'))
        receipt = WaitReceipt(
            operation_key=hs2._operation_identity(first),
            operation_id=first.handle.operationId.guid)
        assert receipt.matches(first)
        assert not receipt.matches(second)
        assert receipt.is_for(first)

    def test_distinguishes_last_state_from_budget_exhaustion(self):
        receipt = WaitReceipt(
            operation_key=('guid', 'secret'), state='RUNNING_STATE',
            budget_exhausted=True, finished=False)
        assert receipt.state == 'RUNNING_STATE'
        assert receipt.last_known_state == 'RUNNING_STATE'
        assert receipt.budget_exhausted
        assert receipt.wait_status == 'EXHAUSTED'
        assert not receipt.finished

    def test_receipt_aliases(self):
        receipt = WaitReceipt(operation_key=('g', 's'), state='FINISHED_STATE',
                              finished=True, budget_exhausted=False)
        assert receipt['state'] == 'FINISHED_STATE'
        assert receipt['budget_exhausted'] is False
        assert receipt['finished'] is True
        assert receipt.get('missing') is None


class TestSegmentedWait(object):
    def test_first_observation_running_then_budget_runs_out(self):
        client = FakeClient([
            _status_response('RUNNING_STATE', has_result_set=False),
            _status_response('RUNNING_STATE', has_result_set=False)])
        operation = _operation(_session_with(client), FakeHandle('q'))
        receipt = _wait_for_operation(operation, budget=0.02)
        assert receipt.budget_exhausted
        assert receipt.finished is False
        assert receipt.state in ('RUNNING_STATE', 'PENDING_STATE',
                                 'INITIALIZED_STATE')
        assert receipt.waited is True
        assert receipt.bounded is True
        assert receipt.matches(operation)
        assert receipt.remaining_budget == 0.0

    def test_receipt_keeps_last_state_when_budget_expires_before_poll(self):
        client = FakeClient([_status_response('RUNNING_STATE')])
        operation = _operation(_session_with(client), FakeHandle('q'))
        receipt = WaitReceipt(
            operation_key=hs2._operation_identity(operation),
            state='RUNNING_STATE')
        # Zero budget: no poll this segment, but prior state survives.
        result = _wait_for_operation(operation, budget=0.0, receipt=receipt)
        assert result.budget_exhausted
        assert result.waited is False
        assert result.state == 'RUNNING_STATE'
        assert client.calls == []

    def test_retry_after_longer_than_remaining_budget_yields_receipt(self):
        retry_after = 5
        client = FakeClient(
            [],
            retryable_errors=[HttpError(
                503, 'Injected Fault', None, {'Retry-After': str(retry_after)})])
        operation = _operation(_session_with(client), FakeHandle('q'))
        started = time.monotonic()
        receipt = _wait_for_operation(operation, budget=0.5)
        elapsed = time.monotonic() - started
        assert receipt.budget_exhausted
        assert receipt.retry_after == retry_after
        assert len(client.calls) == 1
        assert elapsed < 1.0
        assert receipt.state == hs2.UNKNOWN_OPERATION_STATE

    def test_running_then_retry_reply_beyond_budget_no_side_effects(self):
        """The prompt's core sequence: running, then Retry-After > budget."""
        events = []
        client = FakeClient(
            [_status_response('RUNNING_STATE')],
            retryable_errors=[None, HttpError(
                503, 'Injected Fault', None, {'Retry-After': '4'})],
            events=events)
        operation = _operation(_session_with(client), FakeHandle('q'))
        receipt = _wait_for_operation(operation, budget=0.25)
        assert receipt.budget_exhausted
        assert receipt.state == 'RUNNING_STATE'
        assert receipt.retry_after == 4
        assert [name for name, _ in events] == []
        assert len(client.calls) == 2

    def test_retry_after_does_not_trigger_second_submission(self):
        client = FakeClient(
            [_status_response('FINISHED_STATE')],
            retryable_errors=[HttpError(
                503, 'Injected Fault', None, {'Retry-After': '9'})])
        operation = _operation(_session_with(client), FakeHandle('q'))
        receipt = _wait_for_operation(operation, budget=0.3)
        assert receipt.budget_exhausted
        assert len(client.calls) == 1

    def test_retry_after_within_budget_retries_status_only(self):
        client = FakeClient(
            [_status_response('FINISHED_STATE')],
            retryable_errors=[HttpError(
                503, 'Injected Fault', None, {'Retry-After': '1'})])
        operation = _operation(_session_with(client), FakeHandle('q'))
        receipt = _wait_for_operation(operation, budget=1.5)
        assert receipt.finished
        assert len(client.calls) == 2
        assert client.execute_calls == []
        assert receipt.budget_exhausted is False

    def test_default_retry_delay_longer_than_budget_yields_receipt(self):
        client = FakeClient(
            [],
            retryable_errors=[HttpError(503, 'Injected Fault', None, {})])
        operation = _operation(_session_with(client), FakeHandle('q'))
        started = time.monotonic()
        receipt = _wait_for_operation(operation, budget=0.3)
        elapsed = time.monotonic() - started
        assert receipt.budget_exhausted
        assert receipt.retry_after == 1
        assert elapsed < 0.9
        assert len(client.calls) == 1

    def test_unreturned_status_call_consumes_budget(self):
        transport = FakeTransport()
        client = FakeClient([], transport=transport)
        operation = _operation(_session_with(client), FakeHandle('q'))
        receipt = _wait_for_operation(operation, budget=0.05)
        assert receipt.budget_exhausted
        assert receipt.bounded is True
        assert receipt.remaining_budget == 0.0

    def test_transport_timeout_is_exhaustion_not_query_failure(self):
        transport = FakeTransport()
        client = FakeClient(
            [], transport=transport,
            retryable_errors=[TTransportException(
                TTransportException.TIMED_OUT, 'read timeout')])
        operation = _operation(_session_with(client), FakeHandle('q'))
        receipt = _wait_for_operation(operation, budget=0.2)
        assert receipt.budget_exhausted
        assert not receipt.finished
        assert receipt.last_exception is not None
        # The transport is closed so a partial reply frame cannot leak into a
        # later segment; it reopens lazily with that segment's own deadline.
        assert transport.timeout is None
        assert transport.close_count == 1
        assert transport.isOpen() is False

    def test_unboundable_transport_reports_limitation(self):
        client = FakeClient([], transport=NoTimeoutTransport())
        operation = _operation(_session_with(client), FakeHandle('q'))
        with pytest.raises(WaitBudgetUnsupported) as excinfo:
            _wait_for_operation(operation, budget=0.1, strict_budget=True)
        assert excinfo.value.receipt.bounded is False
        assert excinfo.value.receipt.budget_exhausted is False
        assert excinfo.value.receipt.limitation == \
            'in_flight_call_cannot_be_interrupted'
        assert client.calls == []

    def test_unboundable_transport_states_limitation_on_receipt(self):
        client = FakeClient([], transport=NoTimeoutTransport())
        operation = _operation(_session_with(client), FakeHandle('q'))
        receipt = _wait_for_operation(operation, budget=0.01)
        assert receipt.budget_exhausted
        assert receipt.bounded is False
        assert receipt.limitation == 'in_flight_call_cannot_be_interrupted'
        assert client.calls

    def test_unboundable_transport_does_not_silently_overrun(self):
        """With an unboundable transport the limitation is on the receipt."""
        client = FakeClient([_status_response('RUNNING_STATE')],
                            transport=NoTimeoutTransport())
        operation = _operation(_session_with(client), FakeHandle('q'))
        receipt = _wait_for_operation(operation, budget=0.0)
        assert receipt.budget_exhausted
        assert receipt.bounded is False
        assert receipt.limitation
        assert client.calls == []

    def test_unboundable_transport_can_opt_in(self):
        client = FakeClient([_status_response('FINISHED_STATE')],
                            transport=NoTimeoutTransport())
        operation = _operation(_session_with(client), FakeHandle('q'))
        receipt = _wait_for_operation(operation, budget=0.1,
                                      allow_unbounded=True)
        assert receipt.finished
        assert receipt.bounded is False
        assert receipt.matches(operation)

    def test_continue_after_budget_gets_final_state_and_results(self):
        client = FakeClient([
            _status_response('RUNNING_STATE'),
            _status_response('FINISHED_STATE', has_result_set=True)])
        operation = _operation(_session_with(client), FakeHandle('q'))
        first = _wait_for_operation(operation, budget=0.01)
        assert first.budget_exhausted
        second = _wait_for_operation(operation, budget=0.5, receipt=first)
        assert second.finished
        assert second.state == 'FINISHED_STATE'
        assert second.has_result_set is True
        assert second.matches(operation)
        assert operation.has_result_set is True

    def test_old_receipt_does_not_wait_for_new_query(self):
        client = FakeClient([])
        session = _session_with(client)
        old = _operation(session, FakeHandle('old'))
        old_receipt = WaitReceipt(
            operation_key=hs2._operation_identity(old), state='RUNNING_STATE')
        new = _operation(session, FakeHandle('new'))
        stale = _wait_for_operation(new, budget=0.2, receipt=old_receipt)
        assert stale.stale is True
        assert stale.matches_current is False
        assert client.calls == []

    def test_error_handler_only_runs_on_server_evidence(self):
        client = FakeClient([_status_response('ERROR_STATE',
                                              error_message='boom')])
        operation = _operation(_session_with(client), FakeHandle('q'))
        calls = []

        def on_error(resp, state):
            calls.append(resp.errorMessage)
            if resp.errorMessage:
                raise OperationalError(resp.errorMessage)
            return True

        with pytest.raises(OperationalError):
            _wait_for_operation(operation, budget=0.3, on_error=on_error)
        assert calls == ['boom']

    def test_retry_sleep_timeout_does_not_affect_operation_state(self):
        transport = FakeTransport()
        client = FakeClient([], transport=transport)
        operation = _operation(_session_with(client), FakeHandle('q'))
        receipt = _wait_for_operation(operation, budget=0.1)
        assert receipt.budget_exhausted
        assert transport.timeout is None
        assert client._iprot.trans is transport


class TestCursorBudgetedWait(object):
    def _cursor(self, responses):
        client = FakeClient(responses)
        session = _session_with(client)
        cursor = hs2.HiveServer2Cursor(session)
        operation = _operation(session, FakeHandle('q'))
        cursor._last_operation = operation
        cursor._last_operation_active = True
        return cursor, client, operation

    def test_segmented_wait_via_cursor(self):
        cursor, client, operation = self._cursor([
            _status_response('RUNNING_STATE'),
            _status_response('FINISHED_STATE')])
        first = cursor.wait_for_operation(budget=0.01)
        assert first.budget_exhausted
        second = cursor.continue_waiting(first, budget=0.5)
        assert second.finished
        assert cursor._last_operation_finished

    def test_budgeted_wait_does_not_close_or_cancel(self):
        cursor, client, operation = self._cursor([
            _status_response('RUNNING_STATE')])
        receipt = cursor._wait_segment(0.01)
        assert receipt.budget_exhausted
        assert len(client.calls) == 1
        assert hasattr(cursor._last_operation, 'handle')

    def test_old_receipt_with_replaced_operation(self):
        cursor, client, old_operation = self._cursor([
            _status_response('RUNNING_STATE')])
        old_receipt = cursor._wait_segment(0.01)
        cursor._last_operation = _operation(
            cursor.session, FakeHandle('replacement'))
        result = cursor._wait_segment(0.01, receipt=old_receipt)
        assert result.stale
        assert len(client.calls) == 1

    def test_finished_receipt_cannot_complete_replacement_query(self):
        cursor, client, old_operation = self._cursor([
            _status_response('FINISHED_STATE')])
        finished_receipt = cursor._wait_segment(0.5)
        assert finished_receipt.finished
        cursor._last_operation_finished = False
        cursor._last_operation = _operation(
            cursor.session, FakeHandle('replacement'))
        stale = cursor._wait_segment(0.5, receipt=finished_receipt)
        assert stale.stale
        assert cursor._last_operation_finished is False
        assert len(client.calls) == 1

    def test_keep_using_receipt_after_operation_closed(self):
        cursor, client, operation = self._cursor([
            _status_response('RUNNING_STATE'),
            _status_response('FINISHED_STATE')])
        receipt = cursor._wait_segment(0.01)
        cursor._last_operation_active = False
        cursor._last_operation_finished = False
        same = cursor._wait_segment(0.5, receipt=receipt)
        assert same.finished
        assert same.matches(operation)

    def test_poll_other_cursor_between_segments(self):
        responses = [
            _status_response('RUNNING_STATE'),
            _status_response('RUNNING_STATE'),
            _status_response('FINISHED_STATE')]
        client = FakeClient(responses)
        session = _session_with(client)

        first = _operation(session, FakeHandle('first'))
        second = _operation(session, FakeHandle('second'))
        cursor_a = hs2.HiveServer2Cursor(session)
        cursor_a._last_operation = first
        cursor_a._last_operation_active = True
        cursor_b = hs2.HiveServer2Cursor(session)
        cursor_b._last_operation = second
        cursor_b._last_operation_active = True

        first_receipt = cursor_a._wait_segment(0.01)
        assert first_receipt.budget_exhausted
        other = cursor_b._wait_segment(0.01)
        assert other.budget_exhausted
        assert other.matches(second)
        final = cursor_a._wait_segment(0.5, receipt=first_receipt)
        assert final.finished
        assert final.matches(first)

    def test_old_receipt_while_operation_terminated_but_readable(self):
        client = FakeClient([
            _status_response('FINISHED_STATE', has_result_set=True)])
        session = _session_with(client)
        operation = _operation(session, FakeHandle('done'))
        receipt = WaitReceipt(
            operation_key=hs2._operation_identity(operation),
            state='RUNNING_STATE', has_result_set=True)
        cursor = hs2.HiveServer2Cursor(session)
        cursor._last_operation = operation
        cursor._last_operation_active = True
        result = cursor._wait_segment(0.5, receipt=receipt)
        assert result.finished
        assert result.has_result_set is True
        assert len(client.calls) == 1

    def test_execute_wait_budget_returns_receipt(self):
        cursor, client, operation = self._cursor([
            _status_response('RUNNING_STATE')])
        receipt = cursor.execute('select 1', wait_budget=0.01)
        assert isinstance(receipt, WaitReceipt)
        assert receipt.budget_exhausted
        assert receipt.matches(cursor._last_operation)
        assert client.execute_calls

    def test_operation_can_wait_and_continue(self):
        client = FakeClient([
            _status_response('RUNNING_STATE'),
            _status_response('FINISHED_STATE', has_result_set=True)])
        operation = _operation(_session_with(client), FakeHandle('q'))
        first = operation.wait_for_operation(budget=0.01)
        assert first.budget_exhausted
        second = operation.continue_waiting(first, budget=0.5)
        assert second.finished
        assert second.state == 'FINISHED_STATE'
        assert second.has_result_set is True
        assert second.matches(operation)


class TestInFlightStatusCall(object):
    def test_poll_that_never_returns_ends_at_budget(self):
        """A status call that blocks past the budget must not hang the caller.

        The transport read is bounded by the remaining budget, so a server
        that never answers surfaces as budget exhaustion rather than as a
        query failure.
        """
        transport = FakeTransport()

        def blocking_status(req):
            # Emulate a transport whose read is bounded by setTimeout: the
            # call blocks until the deadline is reached.
            time.sleep(min(0.08, max(0.01, transport.timeout or 0.05)))
            raise TTransportException(TTransportException.TIMED_OUT,
                                      'read timeout')

        class BlockingClient(FakeClient):
            def GetOperationStatus(self, req):
                self.calls.append(req)
                return blocking_status(req)

        client = BlockingClient([], transport=transport)
        operation = _operation(_session_with(client), FakeHandle('q'))
        started = time.monotonic()
        receipt = _wait_for_operation(operation, budget=0.15)
        elapsed = time.monotonic() - started
        assert receipt.budget_exhausted
        assert receipt.finished is False
        assert receipt.bounded is True
        assert elapsed <= 0.35
        assert client.calls
        # Only the status call happened: no cancel/close/submit.
        assert getattr(client, 'execute_calls', []) == []

    def test_blocking_wait_without_budget_still_works(self):
        client = FakeClient([
            _status_response('RUNNING_STATE'),
            _status_response('FINISHED_STATE')])
        cursor = hs2.HiveServer2Cursor(_session_with(client))
        cursor._last_operation = _operation(cursor.session,
                                            FakeHandle('q'))
        cursor._last_operation_active = True
        cursor._wait_to_finish(poll_interval=0.01)
        assert cursor._last_operation_finished
        assert cursor._last_operation_active
        assert len(client.calls) == 2
