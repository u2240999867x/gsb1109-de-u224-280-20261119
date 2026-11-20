"""
Functions and objects used to handle cooperative cancellation.

Cancellation happens at the end of the current chunk, or before the next
chunk is started, so that database transactions and cursors are left in a
consistent state.  Two levels of cancellation are available:

* :class:`AbortToken` cancels a single ETL task.  Pass the token to an ETL
  function using its ``abort_token`` argument and call ``token.abort()`` from
  any thread to stop that task only.
* :func:`abort_etlhelper_threads` stops every task running in the process.
  It is the emergency stop and remains available for code that does not use
  individual tokens.

Global stops are latched.  Each task records the global abort generation
when it starts and is aborted if the generation changes while it is running.
Starting a new task (which may clear the global event for future tasks) does
not make an already aborted task run again.
"""
import threading

from etlhelper.exceptions import ETLHelperAbort

# Global emergency stop event.  Retained as a public name for backwards
# compatibility with code that inspects it directly.
abort_event = threading.Event()

# Lock protecting the global event / generation pair and per-token locks.
_state_lock = threading.RLock()

# Incremented once for each emergency stop.  Tasks snapshot this value when
# they start and abort if it subsequently changes.
_abort_generation = 0


class AbortToken:
    """
    Cooperative cancellation token for a single ETL task.

    Create a token, pass it to one or more ETL functions with the
    ``abort_token`` argument, and call :meth:`abort` to stop the task at its
    next checkpoint.  A token is sticky: once aborted it stays aborted, so it
    can safely be shared by the extract and load stages of a single copy.

    An ETL function called with a token that has already been aborted does not
    read from its source or write to its destination.  It raises
    :class:`ETLHelperAbort` straight away.
    """

    def __init__(self):
        self._event = threading.Event()
        # Generation of the global abort event when this task started.
        with _state_lock:
            self._abort_generation = _abort_generation
        self._lock = threading.RLock()

    def abort(self):
        """
        Request cancellation of the task.  Returns immediately; the task stops
        at its next checkpoint and then rolls back its uncommitted work.
        """
        self._event.set()

    def set(self):
        """Alias for :meth:`abort`, matching ``threading.Event``."""
        self.abort()

    def cancel(self):
        """Alias for :meth:`abort`."""
        self.abort()

    def stop(self):
        """Alias for :meth:`abort`."""
        self.abort()

    def clear(self):
        """Alias for :meth:`reset`, matching ``threading.Event``."""
        self.reset()

    def wait(self, timeout=None):
        """Wait until the task is aborted, like ``threading.Event.wait``."""
        return self._event.wait(timeout)

    def reset(self):
        """Clear an earlier abort request so the token can be reused."""
        with self._lock:
            self._event.clear()
            with _state_lock:
                self._abort_generation = _abort_generation

    @property
    def aborted(self):
        """True if cancellation has been requested for this task."""
        return self.is_aborted()

    @property
    def cancelled(self):
        """Alias for :attr:`aborted`."""
        return self.is_aborted()

    def is_set(self):
        """Alias for :meth:`is_aborted`, matching ``threading.Event``."""
        return self.is_aborted()

    def is_aborted(self):
        """
        Return True if the task should stop.

        This includes both a direct call to :meth:`abort` and a global stop
        issued since the task began.
        """
        if self._event.is_set():
            return True
        with _state_lock:
            return _abort_generation != self._abort_generation

    def is_cancelled(self):
        """Alias for :meth:`is_aborted`."""
        return self.is_aborted()


class _ExternalAbortToken:
    """
    Adapt an event-like object (for example ``threading.Event``) to the token
    interface, recording the global abort generation when the task starts.
    """

    def __init__(self, event):
        self._event = event
        with _state_lock:
            self._abort_generation = _abort_generation

    def abort(self):
        """Set the wrapped event."""
        self._event.set()

    cancel = abort
    stop = abort

    def reset(self):
        """Clear the wrapped event."""
        self._event.clear()
        with _state_lock:
            self._abort_generation = _abort_generation

    @property
    def aborted(self):
        """True if the wrapped event or a global stop requests cancellation."""
        return self.is_aborted()

    @property
    def cancelled(self):
        """Alias for :attr:`aborted`."""
        return self.is_aborted()

    def is_aborted(self):
        """Return True if the task should stop."""
        if self._event.is_set():
            return True
        with _state_lock:
            return _abort_generation != self._abort_generation

    def is_cancelled(self):
        """Alias for :meth:`is_aborted`."""
        return self.is_aborted()


# Alternative names for the same class, for callers that prefer them.
CancelToken = AbortToken
CancellationToken = AbortToken
StopToken = AbortToken


def get_abort_token(abort_token=None):
    """
    Return a token for the current task.

    ``abort_token`` may be an :class:`AbortToken`, an event-like object with
    ``is_set`` or ``is_aborted`` (e.g. ``threading.Event``), or ``None``.  A
    new token records the current global generation, so tasks started after an
    emergency stop are not affected by the earlier stop.

    ``None`` always creates a new, independent token.  ETL entry points call
    this once per task and pass the result to lower-level functions, so the
    extract and load stages of one copy share cancellation state while
    different tasks remain independent.
    """
    if abort_token is None:
        return AbortToken()
    if isinstance(abort_token, (AbortToken, _ExternalAbortToken)):
        return abort_token
    if hasattr(abort_token, 'is_set') or hasattr(abort_token, 'is_aborted'):
        return _ExternalAbortToken(abort_token)
    raise TypeError(
        "abort_token must be an AbortToken or an event-like object with "
        f"is_set()/is_aborted(), got {type(abort_token)}")


def abort_etlhelper_threads(abort_token=None):
    """
    Stop ETL tasks at the end of their current chunk.

    With no argument this is the global emergency stop: all tasks that are
    already running will raise :class:`ETLHelperAbort` at their next
    checkpoint.  Tasks started afterwards are not affected.

    If ``abort_token`` is given, only the task using that token is stopped.
    This is equivalent to calling ``abort_token.abort()``.
    """
    if abort_token is not None:
        get_abort_token(abort_token).abort()
        return

    global _abort_generation
    with _state_lock:
        _abort_generation += 1
        abort_event.set()


def abort_etlhelper_task(abort_token):
    """Abort a single ETL task.  Alias for ``abort_etlhelper_threads(token)``."""
    abort_etlhelper_threads(abort_token)


def clear_abort_event():
    """
    Clear the global abort event.

    This makes the event available for a subsequent emergency stop.  Tasks
    that were already running when a stop was issued remain cancelled because
    they compare against the abort generation recorded when they started.
    """
    with _state_lock:
        abort_event.clear()


def raise_for_abort(message, abort_token=None):
    """
    Raise ETLHelperAbort with message if cancellation has been requested.

    When ``abort_token`` is omitted, the current global abort event is
    checked.  ETL functions always pass the token belonging to their task, so
    that cancellation remains attached to that task even if a later task
    clears the global event.
    """
    if abort_token is None:
        if abort_event.is_set():
            raise ETLHelperAbort(message)
        return

    token = get_abort_token(abort_token)
    if token.is_aborted():
        raise ETLHelperAbort(message)
