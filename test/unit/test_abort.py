"""Tests for individual and global cancellation tokens."""
import threading

import pytest

from etlhelper.abort import (
    AbortToken,
    abort_etlhelper_task,
    abort_etlhelper_threads,
    clear_abort_event,
    get_abort_token,
    raise_for_abort,
)
from etlhelper.exceptions import ETLHelperAbort


@pytest.fixture(autouse=True)
def reset_global_abort():
    """Leave global abort state clean for the next test."""
    clear_abort_event()
    yield
    clear_abort_event()


def test_abort_token_starts_running():
    token = AbortToken()
    assert token.aborted is False
    assert token.is_aborted() is False
    raise_for_abort("not aborted", token)


def test_abort_token_is_sticky():
    token = AbortToken()
    token.abort()
    assert token.aborted is True
    assert token.cancelled is True

    with pytest.raises(ETLHelperAbort, match="task stopped"):
        raise_for_abort("task stopped", token)

    # Clearing the global event does not reset an individual token.
    clear_abort_event()
    assert token.is_aborted() is True


def test_abort_token_reset_allows_reuse():
    token = AbortToken()
    token.abort()
    token.reset()
    assert token.is_aborted() is False
    raise_for_abort("not aborted", token)


def test_abort_etlhelper_task_aborts_one_token():
    token = AbortToken()
    abort_etlhelper_task(token)
    assert token.is_aborted() is True


def test_abort_event_like_object_is_supported():
    event = threading.Event()
    token = get_abort_token(event)
    event.set()
    assert token.is_aborted() is True


def test_abort_token_exposes_event_style_api():
    token = AbortToken()
    token.set()
    assert token.is_set() is True
    token.clear()
    assert token.is_set() is False


def test_get_abort_token_rejects_unsupported_object():
    with pytest.raises(TypeError, match="abort_token"):
        get_abort_token(object())


def test_global_stop_aborts_tokens_created_before_it():
    token = AbortToken()
    abort_etlhelper_threads()
    assert token.is_aborted() is True

    with pytest.raises(ETLHelperAbort):
        raise_for_abort("global stop", token)


def test_token_created_after_global_stop_is_not_aborted():
    abort_etlhelper_threads()
    clear_abort_event()

    # A task started after the stop records the new global generation.
    new_task = AbortToken()
    assert new_task.is_aborted() is False
    raise_for_abort("not aborted", new_task)


def test_clear_abort_event_does_not_revive_snapshot_token():
    token = AbortToken()
    abort_etlhelper_threads()
    clear_abort_event()

    # Clearing the event is for future tasks only.
    assert token.is_aborted() is True
