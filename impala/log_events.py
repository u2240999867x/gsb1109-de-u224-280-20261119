"""Continuous reading of query logs with confirmable event boundaries.

This module complements the plain-text `get_log()` path. Some servers
(Hive) page the query log as rows with a verifiable start position, so
each row is one log event even when the row value itself contains
newlines. Other servers (Impala) only return the whole log as a single
text snapshot, which proves no event boundaries. `LogTracker` records
what has been confirmed for one query execution so that overlapping
pages are not duplicated, conflicting positions are detected, and
snapshot text is kept verbatim without inventing event numbers.
"""


class LogRows(object):
    """One page of row-based log entries fetched from the server.

    `start_offset` is the server-provided position of the first row of
    `texts` within the query's log. Each row is a single log event, even
    if the row value itself contains newlines.
    """

    def __init__(self, start_offset, texts):
        self.start_offset = start_offset
        self.texts = list(texts)

    def __repr__(self):
        return 'LogRows(start_offset={0!r}, texts={1!r})'.format(
            self.start_offset, self.texts)


class LogEvent(object):
    """A single confirmed log event.

    `text` may contain newlines; it is still one event. `position` is the
    confirmed offset of the event within the log of one query execution.
    Positions are only meaningful for that query and cannot be carried
    over to another query.
    """

    def __init__(self, position, text):
        self.position = position
        self.text = text

    def __eq__(self, other):
        if not isinstance(other, LogEvent):
            return NotImplemented
        return (self.position == other.position and
                self.text == other.text)

    def __ne__(self, other):
        result = self.__eq__(other)
        if result is NotImplemented:
            return result
        return not result

    def __hash__(self):
        return hash((self.position, self.text))

    def __repr__(self):
        return 'LogEvent(position={0!r}, text={1!r})'.format(
            self.position, self.text)


class LogRead(object):
    """The result of one continuous log read.

    Attributes
    ----------
    events : tuple of LogEvent
        Newly confirmed events from this read. Empty when nothing new was
        confirmed or when the source only provides whole-text snapshots.
    snapshot : str or None
        The whole-text log snapshot observed by this read, for sources
        without event boundaries. The text is kept verbatim so changes
        between snapshots can be reviewed, but no event numbers are
        derived from it.
    continued : bool
        True only when this read was confirmed to continue the previously
        seen evidence, which requires log event boundaries and verifiable
        positions.
    from_closed : bool
        True when the evidence comes from the log saved before the query
        was closed rather than from a live read of the server.
    position : int or None
        The next confirmed event position after this read, or None when
        no verifiable positions exist.
    """

    def __init__(self, events=(), snapshot=None, continued=False,
                 from_closed=False, position=None):
        self.events = tuple(events)
        self.snapshot = snapshot
        self.continued = continued
        self.from_closed = from_closed
        self.position = position

    def __repr__(self):
        return ('LogRead(events={0!r}, snapshot={1!r}, continued={2!r}, '
                'from_closed={3!r}, position={4!r})'.format(
                    self.events, self.snapshot, self.continued,
                    self.from_closed, self.position))


class LogTracker(object):
    """Tracks confirmed log evidence for a single query execution.

    A tracker belongs to one query; positions cannot cross queries, so a
    new tracker is required when another query is executed.
    """

    ROWS = 'rows'
    TEXT = 'text'

    def __init__(self):
        self._events = []
        self._snapshots = []
        self._kind = None
        self._sealed = False
        self._sealed_text = None
        self._conflicted = False

    @property
    def kind(self):
        """'rows' or 'text' once the source type is known, else None."""
        return self._kind

    @property
    def events(self):
        """All events confirmed so far, in position order."""
        return tuple(self._events)

    @property
    def snapshots(self):
        """All whole-text snapshots observed so far, in read order."""
        return tuple(self._snapshots)

    @property
    def sealed(self):
        """True once the query was closed and only the saved log remains."""
        return self._sealed

    @property
    def conflicted(self):
        """True once server positions conflicted with seen content."""
        return self._conflicted

    @property
    def position(self):
        """The next confirmed event position, or None if the source has
        no verifiable positions or the evidence conflicted."""
        if self._kind == self.ROWS and not self._conflicted:
            return len(self._events)
        return None

    def seal(self, final_text):
        """Mark the query as closed.

        From now on reads are served from `final_text` (the log saved
        before closing) instead of the live server, and re-reading is not
        reported as new server content.
        """
        if self._sealed:
            return
        self._sealed = True
        self._sealed_text = final_text
        if (self._kind == self.TEXT and final_text is not None and
                (not self._snapshots or self._snapshots[-1] != final_text)):
            self._snapshots.append(final_text)

    def read(self, operation=None, max_rows=1024):
        """Produce the next LogRead.

        When the tracker is sealed the last available evidence saved
        before closing is returned. Otherwise the log is fetched from
        `operation` and absorbed.
        """
        if self._sealed:
            return LogRead(
                events=(),
                snapshot=self._sealed_text,
                continued=(self._kind == self.ROWS and
                           not self._conflicted),
                from_closed=True,
                position=self.position)
        result = operation.fetch_log(max_rows=max_rows)
        if isinstance(result, LogRows):
            return self._absorb_rows(result)
        return self._absorb_text(result)

    def _absorb_rows(self, page):
        self._kind = self.ROWS
        if self._conflicted:
            return LogRead(continued=False, position=self.position)
        texts = page.texts
        if texts:
            # Verify the overlap with already confirmed events. Identical
            # text at a different confirmed position is a different event,
            # so only positions are compared, never text alone.
            for i, text in enumerate(texts):
                pos = page.start_offset + i
                if pos < len(self._events) and self._events[pos].text != text:
                    # The server-provided position conflicts with content
                    # already seen; the current evidence is not suitable
                    # for automatic continuation.
                    self._conflicted = True
                    return LogRead(continued=False, position=self.position)
            if page.start_offset > len(self._events):
                # A gap: the events in between cannot be verified.
                self._conflicted = True
                return LogRead(continued=False, position=self.position)
        new_events = []
        for i, text in enumerate(texts):
            pos = page.start_offset + i
            if pos < len(self._events):
                # Already confirmed at this position; do not duplicate.
                continue
            event = LogEvent(pos, text)
            self._events.append(event)
            new_events.append(event)
        return LogRead(events=tuple(new_events), continued=True,
                       position=self.position)

    def _absorb_text(self, text):
        self._kind = self.TEXT
        self._snapshots.append(text)
        # A plain snapshot proves no event boundaries, so no event
        # numbers or continuation success can be given.
        return LogRead(snapshot=text, continued=False,
                       position=self.position)
