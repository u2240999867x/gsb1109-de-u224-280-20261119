"""Pull-based fan-out of one forward-only JSON stream to two consumers.

The fan-out exposes two branches over one pass of the source.  ``audit``
receives structural events: object/array starts and ends, and fields.
``report`` receives only scalar values whose path matches one of the
configured report paths.  Every branch has its own bounded pending queue, so a
slow consumer blocks the shared source instead of causing the whole document
to be buffered.

This module deliberately does not use the ``StreamingJSON*`` classes: those
keep transient state in the object being iterated and cannot be replayed for a
second consumer.  Instead the tokenizer is advanced by a small event parser
that never stores a container's contents.
"""
from collections import deque
from dataclasses import dataclass
from typing import Any, Optional

from json_stream.iterators import ensure_file
from json_stream.select_tokenizer import default_tokenizer
from json_stream.tokenizer import TokenType

AUDIT = "audit"
REPORT = "report"

OPERATOR = TokenType.OPERATOR
STRING = TokenType.STRING


class EventKind:
    """Names used by :class:`Event.kind`."""

    OBJECT_START = "object_start"
    OBJECT_END = "object_end"
    ARRAY_START = "array_start"
    ARRAY_END = "array_end"
    FIELD = "field"
    VALUE = "value"

    # Readable aliases for callers that prefer the verb-first spelling.
    START_OBJECT = OBJECT_START
    END_OBJECT = OBJECT_END
    START_ARRAY = ARRAY_START
    END_ARRAY = ARRAY_END


@dataclass(frozen=True)
class Event:
    """One structural event from the source.

    ``id`` is unique within the fan-out and distinguishes repeated occurrences
    of the same path (notably duplicate object keys).  ``container_id`` links
    an ``*_end`` event to its matching ``*_start`` event, and links a ``field``
    event to its containing object.  ``path`` always identifies the event
    itself: the containing object plus key for a ``field`` event, and the
    value's own path otherwise.  ``key`` is only set on ``field`` events.
    """

    kind: str
    path: tuple
    id: int
    value: Any = None
    key: Any = None
    container_id: Optional[int] = None

    @property
    def is_structure(self) -> bool:
        return self.kind in (
            EventKind.OBJECT_START,
            EventKind.OBJECT_END,
            EventKind.ARRAY_START,
            EventKind.ARRAY_END,
            EventKind.FIELD,
        )

    @property
    def is_value(self) -> bool:
        return self.kind == EventKind.VALUE

    def __repr__(self):  # pragma: no cover - debugging aid
        extra = ""
        if self.key is not None:
            extra = f", key={self.key!r}"
        elif self.kind == EventKind.VALUE:
            extra = f", value={self.value!r}"
        return f"Event({self.kind!r}, path={self.path!r}, id={self.id}{extra})"


@dataclass(frozen=True)
class UnfinishedLevel:
    """A container that was still open when the source ended."""

    kind: str
    path: tuple
    container_id: int


def _is_incomplete_error(error):
    """Return True for tokenizer errors that only mean "not enough input".

    Both tokenizers raise ``ValueError`` both for malformed syntax and for
    strings/numbers/letters cut off by EOF.  Only the latter should be treated
    as truncation; grammar violations must stay distinguishable from a source
    that is merely incomplete.
    """

    text = str(error)
    return "EOF" in text or "end of file" in text


class FanoutError(Exception):
    """Base class for fan-out failures."""


class FanoutParseError(FanoutError):
    """The source contained invalid JSON before the root value closed."""

    def __init__(self, branch, error, unfinished):
        self.branch = branch
        self.error = error
        self.unfinished = tuple(unfinished)
        super().__init__(f"{branch!r} branch hit invalid JSON: {error}")


class FanoutClosed(FanoutError):
    """Raised when pulling from a branch that has been closed."""


class FanoutBlocked(FanoutError):
    """The requested branch cannot advance until another branch is drained.

    ``blocking`` names the branch or branches whose pending queue has reached
    the configured limit.  No source token is consumed while this exception is
    raised.
    """

    def __init__(self, blocking, branch):
        self.blocking = tuple(blocking)
        self.branch = branch
        names = ", ".join(self.blocking)
        super().__init__(
            f"{branch!r} branch is blocked by pending events for {names}"
        )


class FanoutTruncated(FanoutError):
    """The source ended before the root value was completed.

    ``last_event``/``last_value`` belong to the branch that raised this
    exception (``last_value`` is the decoded scalar of the last value event,
    or ``None`` if that branch never received one), while ``unfinished``
    describes the shared source's open containers (outermost first).
    """

    def __init__(self, branch, last_event, last_value, unfinished):
        self.branch = branch
        self.last_event = last_event
        self.last_value = last_value
        self.unfinished = tuple(unfinished)
        levels = ", ".join(
            f"{level.kind} at {level.path!r}" for level in self.unfinished
        )
        detail = f" (unfinished: {levels})" if levels else ""
        super().__init__(f"{branch!r} branch reached a truncated JSON source{detail}")


def _is_path_like(value):
    return isinstance(value, (tuple, list))


def _normalize_report_paths(report_paths):
    if report_paths is None:
        return ()
    if isinstance(report_paths, (str, int)):
        return ((report_paths,),)
    if _is_path_like(report_paths):
        items = tuple(report_paths)
        if not items:
            return ()
        if all(not _is_path_like(item) for item in items):
            return (items,)
        if all(_is_path_like(item) for item in items):
            return tuple(tuple(item) for item in items)
    raise TypeError("report_paths must be a path or an iterable of paths")


def _matches(pattern, path):
    """Match one report pattern against a concrete path.

    Object keys are matched as strings and list indices as integers, so a JSON
    key ``"0"`` is distinct from list element ``0``.  ``"*"`` matches exactly
    one component and ``"**"`` matches zero or more components.
    """

    if pattern == path:
        return True
    return _match_from(pattern, 0, path, 0)


def _match_from(pattern, pi, path, vi):
    while pi < len(pattern):
        part = pattern[pi]
        if part == "**":
            if pi == len(pattern) - 1:
                return True
            for skip in range(vi, len(path) + 1):
                if _match_from(pattern, pi + 1, path, skip):
                    return True
            return False
        if vi >= len(path):
            return False
        if part != "*" and part != path[vi]:
            return False
        pi += 1
        vi += 1
    return vi == len(path)


class _Frame:
    __slots__ = ("kind", "path", "container_id", "state", "key", "index")

    def __init__(self, kind, path, container_id):
        self.kind = kind
        self.path = path
        self.container_id = container_id
        self.state = "first_key" if kind == "object" else "first_value"
        self.key = None
        self.index = 0


class Branch:
    """One independently pullable side of a :class:`Fanout`."""

    def __init__(self, fanout, name):
        self._fanout = fanout
        self.name = name
        self._closed = False
        self.last_event = None
        self.last_value = None
        self.last_value_event = None
        self.delivered = 0

    @property
    def _queue(self):
        return self._fanout._queues[self.name]

    @property
    def pending(self):
        return len(self._queue)

    @property
    def closed(self):
        return self._closed

    @property
    def complete(self):
        """True once the source closed its root and this branch drained."""

        return self._fanout.complete and not self._queue

    def close(self):
        """Stop this consumer and release all of its pending events.

        The other branch is unaffected and continues from its own position.
        """

        if not self._closed:
            self._closed = True
            self._fanout._drop(self)

    def pull(self):
        """Return the next event for this branch.

        Raises :class:`FanoutBlocked` if the shared source cannot advance until
        another branch is drained, :class:`FanoutTruncated` once a truncated
        source has delivered all events already read, and ``StopIteration``
        after the root closed and this branch's own queue is drained.

        Once the shared source is done, the branch raises
        :class:`FanoutTruncated` only when *this* branch reaches the end of its
        own queue and the source did not close the root.  A branch that has
        already drained after a successful close is unaffected by another
        branch's later truncated read.
        """

        if self._closed:
            raise FanoutClosed(f"{self.name!r} branch is closed")
        while True:
            if self._queue:
                event = self._queue.popleft()
                self.last_event = event
                if event.kind == EventKind.VALUE:
                    self.last_value_event = event
                    self.last_value = event.value
                self.delivered += 1
                return event
            if self._fanout.stopped:
                raise StopIteration
            self._fanout._fill(self)
            if self._closed:
                raise FanoutClosed(f"{self.name!r} branch is closed")
            if not self._queue and self._fanout.done:
                if self._fanout.error is not None:
                    raise FanoutParseError(
                        self.name,
                        self._fanout.error,
                        self._fanout.unfinished,
                    )
                if self._fanout.truncated:
                    raise FanoutTruncated(
                        self.name,
                        self.last_event,
                        self.last_value,
                        self._fanout.unfinished,
                    )
                raise StopIteration

    next_event = pull

    def __next__(self):
        return self.pull()

    def __iter__(self):
        return self

    def __repr__(self):  # pragma: no cover - debugging aid
        state = "closed" if self._closed else f"pending={len(self._queue)}"
        return f"<Branch {self.name!r} {state}>"


class Fanout:
    """A single JSON source split into ``audit`` and ``report`` branches."""

    def __init__(
        self,
        source,
        report_paths=None,
        *,
        pending_limit=64,
        tokenizer=default_tokenizer,
        **tokenizer_kwargs,
    ):
        if pending_limit is not None and pending_limit < 1:
            raise ValueError("pending_limit must be a positive integer or None")
        self._source = ensure_file(source)
        self._tokens = iter(tokenizer(self._source, **tokenizer_kwargs))
        self._report_patterns = _normalize_report_paths(report_paths)
        self._limit = pending_limit
        self._queues = {AUDIT: deque(), REPORT: deque()}
        self.audit = Branch(self, AUDIT)
        self.report = Branch(self, REPORT)
        self._branches = {AUDIT: self.audit, REPORT: self.report}
        self._stack = []
        self._ready = deque()
        self._next_id = 0
        self._root_complete = False
        self._done = False
        self._stopped = False
        self._truncated = False
        self._error = None
        self._tokens_read = 0

    # ------------------------------------------------------------------
    # public state
    # ------------------------------------------------------------------
    @property
    def pending(self):
        return {name: len(queue) for name, queue in self._queues.items()}

    @property
    def blocked_on(self):
        """Names of active branches whose pending queue is at the limit."""

        return tuple(
            name
            for name, branch in self._branches.items()
            if not branch.closed and self._at_limit(self._queues[name])
        )

    @property
    def done(self):
        return self._done

    @property
    def stopped(self):
        """True once every branch has been closed."""

        return self._stopped

    @property
    def complete(self):
        return self._root_complete

    @property
    def truncated(self):
        return self._truncated

    @property
    def failed(self):
        """True when the source stopped on invalid JSON, not just EOF."""

        return self._error is not None

    @property
    def error(self):
        return self._error

    @property
    def tokens_read(self):
        """Number of tokenizer tokens consumed from the shared source."""

        return self._tokens_read

    @property
    def unfinished(self):
        """Containers still open at the point the source stopped."""

        return tuple(
            UnfinishedLevel(frame.kind, frame.path, frame.container_id)
            for frame in self._stack
        )

    def close(self):
        """Close both branches.  The underlying source object is left open."""

        self.audit.close()
        self.report.close()

    def branch(self, name):
        """Return the branch called ``name`` (``"audit"`` or ``"report"``)."""

        try:
            return self._branches[name]
        except KeyError:
            raise KeyError(f"Unknown fan-out branch {name!r}") from None

    def _drop(self, branch):
        """Release a branch's queue and give the shared parser a chance to run.

        Closing the blocking consumer may unblock the parser immediately; doing
        the work here keeps a subsequent ``pull()`` from observing a stale
        "blocked" state.  Closing every branch also ends the fan-out without
        pulling further tokens.
        """

        self._queues[branch.name].clear()
        while self._ready:
            if not self._enqueue_ready():
                self._drain_ready()
                if not self._enqueue_ready():
                    break
        if self.audit.closed and self.report.closed:
            self._done = True
            self._stopped = True

    def _drain_ready(self):
        """Discard a leading run of ready events that no branch wants."""

        while self._ready:
            event = self._ready[0]
            if self._targets(event):
                return
            self._ready.popleft()

    # ------------------------------------------------------------------
    # queueing / backpressure
    # ------------------------------------------------------------------
    def _at_limit(self, queue):
        return self._limit is not None and len(queue) >= self._limit

    def _active(self, name):
        return not self._branches[name].closed

    def _targets(self, event):
        if event.is_structure and self._active(AUDIT):
            return (AUDIT,)
        if (
            event.kind == EventKind.VALUE
            and self._active(REPORT)
            and any(_matches(pattern, event.path) for pattern in self._report_patterns)
        ):
            return (REPORT,)
        return ()

    def _enqueue_ready(self):
        """Move ready events into branch queues, respecting every limit.

        Returns ``True`` when at least one event was moved and ``False`` when
        the head event targets a branch at its limit.  In the latter case no
        part of the event is enqueued anywhere, so it cannot be lost.
        """

        moved = False
        while self._ready:
            event = self._ready[0]
            targets = self._targets(event)
            if not targets:
                self._ready.popleft()
                moved = True
                continue
            if any(self._at_limit(self._queues[name]) for name in targets):
                return moved
            for name in targets:
                self._queues[name].append(event)
            self._ready.popleft()
            moved = True
        return moved

    def _fill(self, branch):
        """Advance the shared parser until ``branch`` has an event or stops."""

        while not self._queues[branch.name]:
            if self._ready:
                if not self._enqueue_ready():
                    blocking = tuple(
                        name
                        for name in self._targets(self._ready[0])
                        if self._at_limit(self._queues[name])
                    )
                    raise FanoutBlocked(blocking, branch.name)
                continue
            if self._done:
                self._drain_ready()
                return
            if self._fanout_done():
                self._done = True
                self._drain_ready()
                return
            blocking = self.blocked_on
            if blocking:
                raise FanoutBlocked(blocking, branch.name)
            try:
                token_type, token = next(self._tokens)
            except StopIteration:
                self._finish_incomplete()
                return
            except ValueError as exc:
                if _is_incomplete_error(exc):
                    self._finish_incomplete()
                else:
                    self._finish_error(exc)
                return
            self._tokens_read += 1
            try:
                events = self._consume(token_type, token)
            except ValueError as exc:
                self._finish_error(exc)
                return
            self._ready.extend(events)

    def _fanout_done(self):
        return self._root_complete or self.audit.closed and self.report.closed

    def _finish_incomplete(self):
        self._done = True
        self._truncated = True

    def _finish_error(self, error):
        self._done = True
        self._error = error

    # ------------------------------------------------------------------
    # event parser
    # ------------------------------------------------------------------
    def _new_id(self):
        event_id = self._next_id
        self._next_id += 1
        return event_id

    def _container_start(self, token, path):
        kind = "object" if token == "{" else "array"
        event_id = self._new_id()
        event = Event(
            EventKind.OBJECT_START if kind == "object" else EventKind.ARRAY_START,
            path,
            event_id,
            container_id=event_id,
        )
        self._stack.append(_Frame(kind, path, event_id))
        return event

    def _container_end(self, frame):
        self._stack.pop()
        event = Event(
            EventKind.OBJECT_END if frame.kind == "object" else EventKind.ARRAY_END,
            frame.path,
            self._new_id(),
            container_id=frame.container_id,
        )
        return event

    def _value_event(self, path, token_type, token):
        value = token
        return Event(EventKind.VALUE, path, self._new_id(), value=value)

    def _field_event(self, frame, key):
        return Event(
            EventKind.FIELD,
            frame.path + (key,),
            self._new_id(),
            key=key,
            container_id=frame.container_id,
        )

    def _consume(self, token_type, token):
        events = []
        if not self._stack:
            if self._root_complete:
                return events
            if token_type == OPERATOR and token in "{[":
                events.append(self._container_start(token, ()))
                return events
            if token_type == OPERATOR:
                raise ValueError(f"Expecting a JSON value, got {token!r}")
            events.append(self._value_event((), token_type, token))
            self._root_complete = True
            return events

        frame = self._stack[-1]
        if frame.kind == "object":
            self._consume_object(frame, token_type, token, events)
        else:
            self._consume_array(frame, token_type, token, events)
        return events

    def _consume_value(self, frame, path, token_type, token, events):
        frame.state = "after_value"
        if token_type == OPERATOR and token in "{[":
            events.append(self._container_start(token, path))
        else:
            events.append(self._value_event(path, token_type, token))

    def _consume_object(self, frame, token_type, token, events):
        state = frame.state
        if state in ("first_key", "next_key"):
            if token_type == OPERATOR and token == "}" and state == "first_key":
                events.append(self._container_end(frame))
                if not self._stack:
                    self._root_complete = True
                return
            if token_type != STRING:
                raise ValueError(f"Expecting string key or }}, got {token!r}")
            frame.key = token
            events.append(self._field_event(frame, token))
            frame.state = "colon"
        elif state == "colon":
            if token_type != OPERATOR or token != ":":
                raise ValueError(f"Expecting :, got {token!r}")
            frame.state = "value"
        elif state == "value":
            self._consume_value(
                frame, frame.path + (frame.key,), token_type, token, events
            )
        elif state == "after_value":
            if token_type != OPERATOR or token not in ",}":
                raise ValueError(f"Expecting comma or }}, got {token!r}")
            if token == "}":
                events.append(self._container_end(frame))
                if not self._stack:
                    self._root_complete = True
            else:
                frame.state = "next_key"
        else:  # pragma: no cover - defensive
            raise ValueError(f"Invalid object state {state!r}")

    def _consume_array(self, frame, token_type, token, events):
        state = frame.state
        if state == "first_value":
            if token_type == OPERATOR and token == "]":
                events.append(self._container_end(frame))
                if not self._stack:
                    self._root_complete = True
                return
            self._consume_value(
                frame, frame.path + (frame.index,), token_type, token, events
            )
            frame.index += 1
        elif state == "after_value":
            if token_type != OPERATOR or token not in ",]":
                raise ValueError(f"Expecting comma or ], got {token!r}")
            if token == "]":
                events.append(self._container_end(frame))
                if not self._stack:
                    self._root_complete = True
            else:
                frame.state = "value"
        elif state == "value":
            if token_type == OPERATOR and token not in "{[":
                raise ValueError(f"Expecting value, got {token!r}")
            self._consume_value(
                frame, frame.path + (frame.index,), token_type, token, events
            )
            frame.index += 1
        else:  # pragma: no cover - defensive
            raise ValueError(f"Invalid array state {state!r}")


def open_fanout(
    source,
    report_paths=None,
    *,
    pending_limit=64,
    tokenizer=default_tokenizer,
    **tokenizer_kwargs,
):
    """Open a bounded two-consumer fan-out over one forward-only JSON source.

    ``report_paths`` is a path or iterable of paths.  Object keys are matched
    as strings and array indices as integers; ``"*"`` matches one component
    and ``"**"`` matches zero or more.  Only scalar values are delivered to
    the report branch, so selecting a container does not materialise it.
    """

    return Fanout(
        source,
        report_paths,
        pending_limit=pending_limit,
        tokenizer=tokenizer,
        **tokenizer_kwargs,
    )


__all__ = [
    "AUDIT",
    "REPORT",
    "EventKind",
    "Event",
    "UnfinishedLevel",
    "Fanout",
    "Branch",
    "FanoutError",
    "FanoutClosed",
    "FanoutBlocked",
    "FanoutTruncated",
    "FanoutParseError",
    "open_fanout",
]
