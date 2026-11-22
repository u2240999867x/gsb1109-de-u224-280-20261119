"""Incremental reader for Memento ``application/link-format`` TimeMaps.

The reader is deliberately byte oriented.  A downloaded TimeMap may be very
large, may be fetched in ranges, and a range request may start a little before
the last confirmed byte, repeating a few bytes that were already parsed.  It
must also be possible to stop in the middle of a UTF-8 character or a quoted
attribute and to resume later without losing or duplicating a link.

Every emitted record keeps the absolute byte range it occupied in the
downloaded representation, so a caller can go back to the original file even
after only a small checkpoint has been retained.

The module has no dependency on HTTP.  :meth:`TimeMapReader.begin_response`
takes the final URL of a response (after redirects) and an optional content
validator (for example, an ``ETag``).  Relative link targets are resolved
against that final URL.  A changed validator starts a new generation and
discards any parse position belonging to the old representation; identical
leading bytes are not enough to continue an old parse.
"""

import re

from urllib.parse import urljoin

from datetime import datetime, timezone

from warcio.timeutils import http_date_to_datetime


# =============================================================================
class TimeMapError(Exception):
    """Base class for errors raised while reading a TimeMap."""


class TimeMapFormatError(TimeMapError):
    """The input is not a valid ``application/link-format`` document."""


class TimeMapResumeError(TimeMapError):
    """The supplied resume data does not line up with the checkpoint."""


# =============================================================================
# Byte level helpers
# =============================================================================
_WS = frozenset(b' \t\r\n')
_LT = 0x3C
_GT = 0x3E
_QUOTE = 0x22
_SEMI = 0x3B
_COMMA = 0x2C
_BACKSLASH = 0x5C

_ABSOLUTE_URL = re.compile(r'^[A-Za-z][A-Za-z0-9+.-]*:|^//')


def _skip_ws(data, pos):
    end = len(data)
    while pos < end and data[pos] in _WS:
        pos += 1
    return pos


def _unescape(value):
    """Resolve backslash escapes used inside a quoted Link value."""
    if _BACKSLASH not in value:
        return value

    out = bytearray()
    pos = 0
    end = len(value)
    while pos < end:
        char = value[pos]
        if char == _BACKSLASH and pos + 1 < end:
            out.append(value[pos + 1])
            pos += 2
        else:
            out.append(char)
            pos += 1
    return bytes(out)


def _decode(value):
    return value.decode('utf-8', 'replace')


def _find_uri_end(data, start):
    """Return the index of the closing ``>`` or ``-1`` if not seen yet.

    Backslash escaping applies to quoted parameter values, not to the URI
    reference between angle brackets, so the first ``>`` ends the target.
    """
    try:
        return data.index(_GT, start + 1)
    except ValueError:
        return -1


def _trim_ws(data, start, end):
    while end > start and data[end - 1] in _WS:
        end -= 1
    return end


def _scan_record(data, final):
    """Locate one top-level link-value in ``data``.

    ``data`` must start at a record boundary.  Returns ``None`` when the
    buffer ends before the record is complete and more data may be coming.
    The returned span uses indexes into ``data``.
    """
    start = _skip_ws(data, 0)
    end = len(data)

    if start >= end:
        if final:
            return {'start': end, 'value_end': end, 'record_end': end,
                    'error': None, 'empty': True}
        return None

    if data[start] != _LT:
        # Not a link-value.  Wait for its delimiter (or end of file) so that a
        # chunk cut is never mistaken for a malformed record.
        pos = start
        while pos < end:
            if data[pos] == _COMMA:
                return {'start': start,
                        'value_end': _trim_ws(data, start, pos),
                        'record_end': pos + 1,
                        'error': 'expected "<" at record start',
                        'empty': False}
            pos += 1
        if not final:
            return None
        return {'start': start, 'value_end': _trim_ws(data, start, end),
                'record_end': end,
                'error': 'expected "<" at record start', 'empty': False}

    uri_end = _find_uri_end(data, start)
    if uri_end < 0:
        if not final:
            return None
        return {'start': start, 'value_end': _trim_ws(data, start, end),
                'record_end': end,
                'error': 'unterminated link target', 'empty': False}

    pos = uri_end + 1
    in_quote = False
    escaped = False
    while pos < end:
        char = data[pos]
        if escaped:
            escaped = False
        elif in_quote:
            if char == _BACKSLASH:
                escaped = True
            elif char == _QUOTE:
                in_quote = False
        else:
            if char == _QUOTE:
                in_quote = True
            elif char == _COMMA:
                return {'start': start,
                        'value_end': _trim_ws(data, start, pos),
                        'record_end': pos + 1, 'error': None,
                        'empty': False}
            elif char in (0x0A, 0x0D):
                # Some hand-maintained downloads put each link-value on its
                # own line without the RFC 8288 comma.  Treat a line break as
                # a separator only when the next non-whitespace byte starts a
                # new "<...>" value; a folded line such as
                # ``<url>;\n rel="memento"`` is therefore still one record.
                nxt = pos + 1
                while nxt < end and data[nxt] in _WS:
                    nxt += 1
                if nxt < end and data[nxt] == _LT and uri_end < pos:
                    return {'start': start,
                            'value_end': _trim_ws(data, start, pos),
                            'record_end': nxt, 'error': None,
                            'empty': False}
        pos += 1

    if not final:
        # The record is syntactically open-ended until its comma is seen, so
        # hold back a possibly incomplete final link until more data arrives.
        return None

    error = 'unterminated quoted parameter' if in_quote else None
    return {'start': start, 'value_end': _trim_ws(data, start, end),
            'record_end': end,
            'error': error, 'empty': False}


# =============================================================================
def _split_params(raw):
    """Split a parameter section on semicolons outside quoted strings."""
    segments = []
    start = 0
    in_quote = False
    escaped = False
    end = len(raw)

    for pos in range(end):
        char = raw[pos]
        if escaped:
            escaped = False
        elif in_quote:
            if char == _BACKSLASH:
                escaped = True
            elif char == _QUOTE:
                in_quote = False
        else:
            if char == _QUOTE:
                in_quote = True
            elif char == _SEMI:
                segments.append(raw[start:pos])
                start = pos + 1

    segments.append(raw[start:])
    return segments


def _parse_quoted(value):
    """Parse a quoted Link parameter value.

    ``value`` starts with a double quote.  Returns ``(decoded, error)``.
    """
    out = bytearray()
    pos = 1
    end = len(value)
    while pos < end:
        char = value[pos]
        if char == _QUOTE:
            rest = value[pos + 1:].strip()
            if rest:
                # Another unescaped quote followed by more text; this quote is
                # part of the value, keep scanning from the next byte.
                out.append(char)
                pos += 1
                continue
            return bytes(_unescape(bytes(out))), None
        if char == _BACKSLASH and pos + 1 < end:
            out.append(char)
            out.append(value[pos + 1])
            pos += 2
            continue
        out.append(char)
        pos += 1

    return bytes(_unescape(bytes(out))), 'unterminated quoted value'


def _parse_params(raw):
    """Return ``(params, error)`` for the bytes following a link target."""
    params = []
    error = None
    for segment in _split_params(raw):
        segment = segment.strip()
        if not segment:
            continue

        eq = segment.find(b'=')
        if eq < 0:
            name = _decode(segment).strip().lower()
            params.append((name, None))
            continue

        name = _decode(segment[:eq]).strip().lower()
        value = segment[eq + 1:].strip()
        if not name:
            error = error or 'empty parameter name'
            continue

        if value.startswith(b'"'):
            text, err = _parse_quoted(value)
            if err and not error:
                error = err
            params.append((name, text))
        else:
            params.append((name, value))

    return tuple(params), error


def _param_values(params, name):
    return [value for key, value in params if key == name]


def _rel_values(params):
    values = []
    for key, value in params:
        if key == 'rel' and value is not None:
            values.append(_decode(value))
    return values


def _rel_tokens(rel_values):
    tokens = []
    for value in rel_values:
        tokens.extend(part.lower() for part in value.split() if part)
    return tuple(tokens)


def _parse_datetime(value):
    """Return ``(timestamp, error)`` for an HTTP-date Link ``datetime``."""
    if value is None:
        return None, 'missing datetime'

    text = _decode(value).strip()
    if not text:
        return None, 'missing datetime'

    try:
        parsed = http_date_to_datetime(text)
        return parsed.strftime('%Y%m%d%H%M%S'), None
    except Exception:
        pass

    # RFC 7089 uses an RFC 1123 date.  Accept an ISO-8601 value as a
    # convenience for hand-maintained TimeMaps, but never invent a date.
    try:
        parsed = datetime.fromisoformat(text.replace('Z', '+00:00'))
    except ValueError:
        return None, 'invalid datetime: ' + text

    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed.strftime('%Y%m%d%H%M%S'), None


# =============================================================================
class TimeMapRecord(object):
    """One link-value parsed from a TimeMap.

    ``start``/``end`` are absolute byte offsets in the downloaded
    representation.  ``end`` excludes the record delimiter, while
    ``record_end`` is the first byte after it.
    """

    def __init__(self, start, end, record_end, raw, url, target, params,
                 rel_values, rel_tokens, datetime_text, timestamp, error,
                 generation, source_url, occurrence_index, duplicate_of):
        self.start = start
        self.end = end
        self.record_end = record_end
        self.raw = raw
        self.url = url
        self.target = target
        self.params = params
        self.rel_values = tuple(rel_values)
        self.rel_tokens = tuple(rel_tokens)
        self.datetime = datetime_text
        self.timestamp = timestamp
        self.error = error
        self.generation = generation
        self.source_url = source_url
        self.occurrence_index = occurrence_index
        self.duplicate_of = duplicate_of

    # -- classification ----------------------------------------------------
    @property
    def is_capture(self):
        return 'memento' in self.rel_tokens

    # ``memento`` is the term used by the domain; ``capture`` in the task.
    is_memento = property(lambda self: self.is_capture)

    @property
    def is_relation(self):
        return not self.is_capture

    @property
    def is_valid(self):
        return self.error is None

    @property
    def is_bad(self):
        return self.error is not None

    @property
    def rel(self):
        # A link may carry several rel attributes; keep all of them so a
        # multi-token relation is not reduced to whichever came last.
        return ' '.join(self.rel_values)

    @property
    def rels(self):
        return self.rel_values

    # -- provenance --------------------------------------------------------
    source_start = property(lambda self: self.start)
    source_end = property(lambda self: self.end)
    reason = property(lambda self: self.error)
    resolved = property(lambda self: self.target)
    absolute_url = property(lambda self: self.target)
    target_url = property(lambda self: self.target)
    datetime_text = property(lambda self: self.datetime)

    def get_all(self, name):
        return tuple(_decode(value) if isinstance(value, bytes) else value
                     for value in _param_values(self.params, name))

    def get(self, name, default=None):
        values = self.get_all(name)
        return values[-1] if values else default

    def __repr__(self):
        kind = 'memento' if self.is_capture else 'link'
        return '<TimeMapRecord {0} {1} {2}:{3}{4}>'.format(
            kind, self.rel or '-', self.start, self.end,
            '!' if self.error else '')

    def __eq__(self, other):
        if not isinstance(other, TimeMapRecord):
            return NotImplemented
        return self.signature() == other.signature()

    def __hash__(self):
        return hash(self.signature())

    def signature(self):
        """Stable value used to compare records across parses."""
        return (self.generation, self.start, self.end, self.is_capture,
                self.error, self.target, self.timestamp, self.rel_tokens)


def _make_record(chunk, span, base_offset, final_url, generation, occurrences,
                 require_base=False):
    """Build a record.  ``span`` indexes ``chunk``, which starts at base_offset."""
    start = span['start']
    value_end = span['value_end']
    raw = bytes(chunk[start:value_end])

    error = span['error']
    url = ''
    target = ''
    params = ()
    rel_values = []
    rel_tokens = ()
    datetime_text = None
    timestamp = None

    uri_end = _find_uri_end(chunk, start)
    if uri_end >= 0:
        url = _decode(bytes(chunk[start + 1:uri_end]))
        if final_url:
            # Resolve only against this response's final URL; the document's
            # "original" and "self" links are never used as a base.
            target = urljoin(final_url, url)
        else:
            target = url
            if (require_base and url and not _ABSOLUTE_URL.match(url)
                    and not error):
                error = 'relative target without a response URL'

        params, param_error = _parse_params(bytes(chunk[uri_end + 1:value_end]))
        if param_error and not error:
            error = param_error

        rel_values = _rel_values(params)
        rel_tokens = _rel_tokens(rel_values)

        dt_values = _param_values(params, 'datetime')
        if dt_values:
            datetime_text = (_decode(dt_values[-1])
                             if dt_values[-1] is not None else None)

        if 'memento' in rel_tokens:
            timestamp, dt_error = _parse_datetime(
                dt_values[-1] if dt_values else None)
            if dt_error and not error:
                error = dt_error
        elif datetime_text is not None:
            # Non-capture relations may carry a date; keep it for provenance
            # but never put them on the backfill list.
            timestamp, _ = _parse_datetime(dt_values[-1])
    else:
        url = _decode(bytes(chunk[start + 1:value_end]))
        target = url

    record = TimeMapRecord(
        start=base_offset + start,
        end=base_offset + value_end,
        record_end=base_offset + span['record_end'],
        raw=raw,
        url=url,
        target=target,
        params=params,
        rel_values=rel_values,
        rel_tokens=rel_tokens,
        datetime_text=datetime_text,
        timestamp=timestamp,
        error=error,
        generation=generation,
        source_url=final_url,
        occurrence_index=1,
        duplicate_of=None,
    )

    if uri_end >= 0:
        # The same link-value at two offsets is a genuine repetition inside
        # one file.  A repeated range request never reaches this point: its
        # overlapping bytes are trimmed before parsing.  Normalising the
        # whitespace around parameters means only the link's meaning is
        # compared, not incidental formatting.
        key = (record.target, record.timestamp, record.rel_tokens,
               tuple(record.params))
        first, count = occurrences.get(key, (None, 0))
        record.occurrence_index = count + 1
        record.duplicate_of = first
        occurrences[key] = (record.start if first is None else first,
                            count + 1)

    return record


# =============================================================================
class TimeMapCheckpoint(object):
    """Small resumable state for a TimeMap download.

    ``tail`` holds the last few confirmed bytes; it is only used to check
    that a resumed range really overlaps the same representation.  The rest
    of the already parsed file does not need to be kept or read again.
    """

    def __init__(self, generation, confirmed_offset, tail, validator,
                 final_url, start_offset=0, require_base=False):
        self.generation = generation
        self.confirmed_offset = confirmed_offset
        self.tail = tail
        self.validator = validator
        self.final_url = final_url
        self.start_offset = start_offset
        self.require_base = require_base

    offset = property(lambda self: self.confirmed_offset)

    def to_dict(self):
        import base64
        return {
            'generation': self.generation,
            'confirmed_offset': self.confirmed_offset,
            'start_offset': self.start_offset,
            'tail': base64.b64encode(self.tail).decode('ascii'),
            'validator': self.validator,
            'final_url': self.final_url,
            'require_base': self.require_base,
        }

    @classmethod
    def from_dict(cls, values):
        import base64
        return cls(
            generation=values['generation'],
            confirmed_offset=values['confirmed_offset'],
            tail=base64.b64decode(values.get('tail', '')),
            validator=values.get('validator'),
            final_url=values.get('final_url'),
            start_offset=values.get('start_offset', 0),
            require_base=values.get('require_base', False),
        )

    def __repr__(self):
        return ('<TimeMapCheckpoint generation={0} offset={1} tail={2}>'
                .format(self.generation, self.confirmed_offset, len(self.tail)))


# =============================================================================
class TimeMapReader(object):
    """Incrementally parse an ``application/link-format`` TimeMap.

    Typical use::

        reader = TimeMapReader(final_url=response.url)
        for chunk in chunks:
            for record in reader.feed(chunk):
                ...
        reader.finish()

    After an interruption::

        checkpoint = reader.checkpoint()
        resumed = TimeMapReader.from_checkpoint(checkpoint)
        resumed.begin_response(new_final_url, new_validator)
        resumed.feed(overlap_and_new_bytes, offset=resume_offset)

    ``feed`` accepts either ``bytes`` or ``str``.  Offsets are always byte
    offsets in the UTF-8 representation.
    """

    DEFAULT_TAIL_SIZE = 4096

    def __init__(self, final_url=None, validator=None,
                 tail_size=DEFAULT_TAIL_SIZE, start_offset=0,
                 require_base=False):
        self.final_url = final_url
        self.validator = validator
        self.generation = 1
        self.tail_size = tail_size
        self.start_offset = int(start_offset)
        self.require_base = require_base

        self.confirmed_offset = self.start_offset
        self._buffer = bytearray()
        self._tail = b''
        self._records = []
        self._previous_records = ()
        self._occurrences = {}
        self._ended = False
        self._response_started = bool(final_url is not None or
                                      validator is not None)
        self._validator_seen = validator is not None
        self._last_feed_end = self.start_offset
        self._seen_any = False
        self._from_checkpoint = False

    # -- construction ------------------------------------------------------
    @classmethod
    def from_checkpoint(cls, checkpoint, tail_size=None):
        reader = cls(final_url=checkpoint.final_url,
                     validator=checkpoint.validator,
                     tail_size=tail_size or cls.DEFAULT_TAIL_SIZE,
                     start_offset=checkpoint.start_offset,
                     require_base=checkpoint.require_base)
        reader.generation = checkpoint.generation
        reader.confirmed_offset = checkpoint.confirmed_offset
        reader._tail = checkpoint.tail
        reader._last_feed_end = checkpoint.confirmed_offset
        reader._response_started = True
        reader._from_checkpoint = True
        return reader

    def checkpoint(self):
        return TimeMapCheckpoint(
            generation=self.generation,
            confirmed_offset=self.confirmed_offset,
            tail=self._tail[-self.tail_size:],
            validator=self.validator,
            final_url=self.final_url,
            start_offset=self.start_offset,
            require_base=self.require_base,
        )

    # -- response context --------------------------------------------------
    def begin_response(self, final_url=None, validator=None, offset=None):
        """Start (or continue) a response.

        Returns ``True`` when a new generation was started, i.e. when the
        validator or final URL changed.  A changed validator always wins over
        coincidentally identical leading bytes: no bytes from the old
        generation remain in the parse buffer or in the confirmed position.

        ``offset`` is the absolute position at which the new generation's
        bytes begin.  A replaced representation must be requested afresh, so
        it defaults to the beginning (0).
        """
        old_url = self.final_url
        if final_url is not None:
            self.final_url = final_url

        if not self._response_started:
            self.validator = validator
            self._validator_seen = validator is not None
            self._response_started = True
            return False

        # Only a validator we actually saw can prove that the representation
        # changed.  An absent validator on the resumed response is not a
        # change signal on its own.
        changed = (validator is not None and self._validator_seen and
                   validator != self.validator)
        if validator is not None:
            self.validator = validator
            self._validator_seen = True

        if final_url is not None and old_url is not None and final_url != old_url:
            changed = True

        if not changed:
            return False

        self._start_new_generation(0 if offset is None else offset)
        return True

    def _start_new_generation(self, offset):
        self._previous_records = tuple(self._records)
        self.generation += 1
        self.restart_from(offset)

    def restart_from(self, offset):
        """Discard all parse state and start over at absolute ``offset``.

        This is used when the service replaced the file: the new generation
        begins at the start of the range that was actually downloaded for it,
        not at a position inherited from the previous representation.
        """
        self.start_offset = int(offset)
        self._buffer = bytearray()
        self._tail = b''
        self._records = []
        self._occurrences = {}
        self._ended = False
        self._last_feed_end = self.start_offset
        self.confirmed_offset = self.start_offset
        self._from_checkpoint = False
        self._seen_any = False

    # -- feeding -----------------------------------------------------------
    def feed(self, data, offset=None):
        """Feed a chunk and return the records completed by this chunk.

        ``offset`` is the absolute byte position of ``data[0]``.  When it is
        omitted, the chunk is assumed to continue at the end of the previously
        supplied data.  A resumed range may start before the confirmed offset;
        the overlapping bytes are verified against the checkpoint tail and
        then discarded, so already published links are never re-emitted.
        """
        if self._ended:
            raise TimeMapError('cannot feed a finished TimeMapReader')

        data = data.encode('utf-8') if isinstance(data, str) else bytes(data)

        if offset is None:
            offset = self._last_feed_end
        else:
            offset = int(offset)

        if offset < 0:
            raise TimeMapResumeError('negative start offset')

        self._align_and_append(data, offset)
        self._last_feed_end = max(self._last_feed_end, offset + len(data))
        return self._drain(final=False)

    def finish(self):
        """Mark the end of the response and emit the final record."""
        if self._ended:
            return []
        self._ended = True
        return self._drain(final=True)

    def _align_and_append(self, data, offset):
        confirmed = self.confirmed_offset
        buffer_end = confirmed + len(self._buffer)

        # A brand-new stream may begin at any offset if the caller could only
        # fetch a range.  Nothing has been parsed yet, so take the first
        # chunk's position as the start of this stream.  A reader restored
        # from a checkpoint already knows where its confirmed bytes are and
        # must never move that boundary backwards.
        if (not self._seen_any and not self._buffer and
                not self._from_checkpoint and
                offset != self.start_offset):
            self.start_offset = offset
            self.confirmed_offset = offset
            self._last_feed_end = offset
            confirmed = offset
            buffer_end = offset

        if offset < confirmed:
            self._check_confirmed_overlap(data, offset, confirmed)
            data = data[confirmed - offset:]
            offset = confirmed

        if offset > buffer_end:
            raise TimeMapResumeError(
                'resume offset {0} is past the buffered position {1}; '
                'bytes would be missing'.format(offset, buffer_end))

        if offset < buffer_end:
            already = offset - confirmed
            overlap = min(len(data), buffer_end - offset)
            if overlap:
                expected = bytes(self._buffer[already:already + overlap])
                if data[:overlap] != expected:
                    raise TimeMapResumeError(
                        'resume data does not match previously received bytes '
                        'at offset {0}'.format(offset))
                data = data[overlap:]

        if data:
            self._buffer.extend(data)
        self._seen_any = True

    def _check_confirmed_overlap(self, data, start, confirmed):
        # The retained tail ends exactly at confirmed_offset and covers
        # [confirmed - len(tail), confirmed).  Compare only the part of the
        # supplied range that falls inside that window.
        tail_start = confirmed - len(self._tail)
        if len(self._tail) == 0:
            return

        compare_start = max(start, tail_start)
        compare_end = min(start + len(data), confirmed)
        if compare_end <= compare_start:
            return

        actual = data[compare_start - start:compare_end - start]
        expected = self._tail[compare_start - tail_start:
                              compare_end - tail_start]
        if actual != expected:
            raise TimeMapResumeError(
                'resent bytes at offset {0}-{1} do not overlap the confirmed '
                'representation'.format(compare_start, compare_end))

    def _drain(self, final):
        emitted = []
        while self._buffer:
            span = _scan_record(self._buffer, final)
            if span is None:
                break

            take = span['record_end']
            if take <= 0:
                break

            chunk = bytes(self._buffer[:take])
            base = self.confirmed_offset

            if not span.get('empty'):
                record = _make_record(chunk, span, base, self.final_url,
                                      self.generation, self._occurrences,
                                      self.require_base)
                self._records.append(record)
                emitted.append(record)

            self._tail = (self._tail + chunk)[-self.tail_size:]
            del self._buffer[:take]
            self.confirmed_offset += take
            self._last_feed_end = max(self._last_feed_end,
                                      self.confirmed_offset)

        if final and self._buffer:
            # Only trailing whitespace can remain after _scan_record(final).
            chunk = bytes(self._buffer)
            self._tail = (self._tail + chunk)[-self.tail_size:]
            del self._buffer[:]
            self.confirmed_offset += len(chunk)

        return emitted

    # -- public views ------------------------------------------------------
    @property
    def records(self):
        return tuple(self._records)

    @property
    def captures(self):
        """Valid capture occurrences, in source order."""
        return tuple(rec for rec in self._records
                     if rec.is_capture and rec.error is None)

    mementos = property(lambda self: self.captures)

    @property
    def relations(self):
        """Non-capture link-values, including duplicates."""
        return tuple(rec for rec in self._records if not rec.is_capture)

    non_captures = property(lambda self: self.relations)

    @property
    def issues(self):
        """Complete records that could not become a capture."""
        return tuple(rec for rec in self._records if rec.error is not None)

    errors = property(lambda self: self.issues)
    bad_records = property(lambda self: self.issues)

    @property
    def duplicates(self):
        """In-file repetitions of an identical capture link-value."""
        return tuple(rec for rec in self._records
                     if rec.duplicate_of is not None)

    @property
    def unique_captures(self):
        """First occurrence of each distinct capture link."""
        return tuple(rec for rec in self.captures
                     if rec.duplicate_of is None)

    @property
    def backfill(self):
        """Captures that may enter a backfill list.

        A record qualifies only when it is a real capture (its relation has
        a ``memento`` token), its target is absolute, and its datetime is
        valid.  Same-second captures at different addresses all remain.
        """
        return tuple(rec for rec in self.captures
                     if rec.timestamp and rec.target)

    # ``targets`` is a convenient plain view of the backfill list.
    @property
    def targets(self):
        return tuple(rec.target for rec in self.backfill)

    def manifest(self):
        """Return a list of provenance dictionaries for backfill records."""
        return [{'url': rec.target,
                 'timestamp': rec.timestamp,
                 'rel': rec.rel,
                 'start': rec.start,
                 'end': rec.end,
                 'occurrence': rec.occurrence_index}
                for rec in self.backfill]

    @property
    def previous_records(self):
        """Records from a generation replaced in this reader instance.

        They are kept only for diagnostics; they are never part of the
        current capture list and cannot be spliced with the new generation.
        A reader restored from a checkpoint has no earlier records, since
        checkpoints intentionally retain only the confirmed position and a
        short tail.
        """
        return self._previous_records

    def find(self, offset):
        """Return the record containing absolute byte ``offset``, if any."""
        for record in self._records:
            if record.start <= offset < record.record_end:
                return record
        return None

    def record_at(self, offset):
        """Alias for :meth:`find`."""
        return self.find(offset)

    def record_for(self, url):
        """Return the first record whose resolved target equals ``url``."""
        for record in self._records:
            if record.target == url:
                return record
        return None

    def reset(self):
        """Discard parse state without changing the generation number."""
        self.confirmed_offset = self.start_offset
        self._buffer = bytearray()
        self._tail = b''
        self._occurrences = {}
        self._records = []
        self._ended = False
        self._last_feed_end = self.start_offset


# =============================================================================
def parse_timemap(data, final_url=None, validator=None, require_base=False):
    """Parse a complete TimeMap and return a populated :class:`TimeMapReader`."""
    reader = TimeMapReader(final_url=final_url, validator=validator,
                           require_base=require_base)
    reader.feed(data, 0)
    reader.finish()
    return reader


def iter_timemap(chunks, final_url=None, validator=None):
    """Yield records from an iterable of chunks."""
    reader = TimeMapReader(final_url=final_url, validator=validator)
    for chunk in chunks:
        yield from reader.feed(chunk)
    yield from reader.finish()


# =============================================================================
class SegmentedFileSource(object):
    """Read a TimeMap from local chunks without re-reading parsed bytes.

    The source behaves like a small file that a collector can request ranges
    from.  ``read(offset, length)`` returns the bytes starting at ``offset``;
    the reader only ever asks for a short overlap plus the bytes it has not
    confirmed yet.
    """

    def __init__(self, chunks, chunk_size=64):
        if isinstance(chunks, str):
            chunks = [chunks.encode('utf-8')]
        elif isinstance(chunks, (bytes, bytearray)):
            chunks = [bytes(chunks)]
        self._data = b''.join(chunk.encode('utf-8') if isinstance(chunk, str)
                              else bytes(chunk) for chunk in chunks)
        self.chunk_size = chunk_size
        self.requests = []

    def __len__(self):
        return len(self._data)

    @property
    def data(self):
        return self._data

    def read(self, offset, length=-1):
        offset = max(0, int(offset))
        end = len(self._data) if length is None or length < 0 else offset + length
        self.requests.append((offset, end))
        return self._data[offset:end]

    def iter_from(self, offset, chunk_size=None, overlap=0):
        chunk_size = chunk_size or self.chunk_size
        offset = max(0, int(offset) - overlap)
        while offset < len(self._data):
            chunk = self._data[offset:offset + chunk_size]
            yield offset, chunk
            offset += len(chunk)

    def iter_all(self, chunk_size=None):
        return self.iter_from(0, chunk_size)


def harvest_segmented(chunks, final_url=None, validator=None,
                      chunk_size=64, overlap=8, max_requests=None):
    """Drive a :class:`TimeMapReader` over a chunked local source.

    Returns ``(reader, source)``.  The source records the byte ranges that
    were actually read.  After an interruption, :func:`resume_segmented` can
    continue from a checkpoint while reading only the tail overlap and the
    unread remainder.
    """
    source = SegmentedFileSource(chunks, chunk_size=chunk_size)
    reader = TimeMapReader(final_url=final_url, validator=validator)
    complete = _drive(reader, source, 0, chunk_size, overlap, max_requests)
    if complete:
        reader.finish()
    return reader, source


def resume_segmented(checkpoint, chunks, validator=None, final_url=None,
                     chunk_size=64, overlap=8, max_requests=None):
    """Resume a segmented read from a checkpoint.

    A changed validator restarts the new generation from the beginning of the
    source; identical leading bytes in the old and new files are not reused.
    """
    source = SegmentedFileSource(chunks, chunk_size=chunk_size)
    reader = TimeMapReader.from_checkpoint(checkpoint)

    if final_url is None:
        final_url = checkpoint.final_url

    if validator is None:
        validator = checkpoint.validator

    restarted = reader.begin_response(final_url, validator)
    start = 0 if restarted else max(0, checkpoint.confirmed_offset)

    complete = _drive(reader, source, start, chunk_size, overlap, max_requests)
    if complete:
        reader.finish()
    return reader, source


def _drive(reader, source, start, chunk_size, overlap, max_requests):
    """Feed the source to the reader; return True if the source was consumed."""
    count = 0
    offset = start
    while offset < len(source):
        if max_requests is not None and count >= max_requests:
            return False
        begin = max(0, offset - overlap if offset else 0)
        chunk = source.read(begin, chunk_size + (offset - begin))
        if not chunk:
            break
        reader.feed(chunk, begin)
        offset = begin + len(chunk)
        count += 1
    return offset >= len(source)


# =============================================================================
class TimeMapHarvester(object):
    """Collect backfill targets from a TimeMap, optionally across resumes.

    The harvester is the small business-facing layer on top of
    :class:`TimeMapReader`.  It accepts bytes from a download (whole or in
    chunks), retains only a checkpoint between calls, and exposes the
    captures it has confirmed so far.  Feeding bytes that were already
    confirmed is harmless, which makes it safe for a range request that
    restarts slightly before the last acknowledged position.
    """

    def __init__(self, final_url=None, validator=None, reader=None,
                 checkpoint=None, tail_size=None, published=None):
        if reader is not None:
            self.reader = reader
        elif checkpoint is not None:
            self.reader = TimeMapReader.from_checkpoint(
                checkpoint, tail_size=tail_size)
        else:
            self.reader = TimeMapReader(final_url=final_url,
                                        validator=validator,
                                        require_base=True)
        # Targets published by an earlier process can be supplied so that
        # ``targets`` is the whole round rather than only this segment.
        self._published = list(published or [])

    def begin(self, final_url=None, validator=None, offset=None):
        """Start a response, returning True when a new generation began."""
        restarted = self.reader.begin_response(final_url, validator,
                                               offset=offset)
        if restarted:
            self._published = []
        return restarted

    def feed(self, data, offset=None):
        """Feed a chunk and return only the newly completed backfill targets."""
        records = self.reader.feed(data, offset)
        return self._collect(records)

    def finish(self):
        records = self.reader.finish()
        return self._collect(records)

    def _collect(self, records):
        targets = [rec.target for rec in records if self._is_target(rec)]
        self._published.extend(targets)
        return targets

    @staticmethod
    def _is_target(record):
        return (record.is_capture and record.error is None and
                record.timestamp and record.target)

    @property
    def targets(self):
        """All confirmed backfill targets for the round, in source order."""
        current = list(self.reader.targets)
        extra = [target for target in current if target not in self._published]
        return list(self._published) + extra

    @property
    def records(self):
        return list(self.reader.records)

    @property
    def captures(self):
        return list(self.reader.captures)

    @property
    def issues(self):
        return list(self.reader.issues)

    @property
    def bad(self):
        return list(self.reader.issues)

    @property
    def duplicates(self):
        return list(self.reader.duplicates)

    def checkpoint(self):
        return self.reader.checkpoint()

    @property
    def confirmed_offset(self):
        return self.reader.confirmed_offset

    @property
    def generation(self):
        return self.reader.generation

    def manifest(self):
        return self.reader.manifest()


# ``IncrementalTimeMap`` is the name used by some collectors for the same
# reader.
IncrementalTimeMap = TimeMapReader
IncrementalTimeMapReader = TimeMapReader


def parse_timemap_file(filename, final_url=None, validator=None,
                       chunk_size=65536):
    """Parse a local TimeMap file in chunks.

    Returns the populated reader; the file is never held in memory whole.
    """
    reader = TimeMapReader(final_url=final_url, validator=validator)
    with open(filename, 'rb') as fh:
        offset = 0
        while True:
            chunk = fh.read(chunk_size)
            if not chunk:
                break
            reader.feed(chunk, offset)
            offset += len(chunk)
    reader.finish()
    return reader
