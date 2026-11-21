"""Incremental reader for RFC 7464 JSON Text Sequences.

An RFC 7464 stream is a sequence of JSON texts.  Each text is introduced by a
record separator (RS, ``0x1e``) and is normally terminated by a line feed.
The RS byte is framing only: it is not part of the JSON text, raw RS bytes
inside JSON strings are not permitted, and consecutive RS bytes delimit empty
fragments which produce no records.  A fragment consisting only of whitespace
is still reported because it was actually received but contains no JSON text.

This module intentionally leaves the normal ``load()``/``load_many()``
behaviour untouched.  ``load_sequence()`` is the explicit opt-in reader for
sequence framing.
"""

import codecs
import json
from collections import deque

from json_stream.iterators import ensure_file

RS = 0x1E
RS_BYTE = b"\x1e"
RS_CHAR = "\x1e"
RECORD_SEPARATOR = RS
RECORD_SEPARATOR_BYTE = RS_BYTE
RECORD_SEPARATOR_CHAR = RS_CHAR
DEFAULT_CHUNK_SIZE = 65536
DEFAULT_MAX_PREVIEW = 1024 * 1024

_JSON_WHITESPACE = " \t\r\n"
_STRUCTURAL = "{}[]:,"


class _Status(str):
    """String status with a ``.value`` attribute and familiar aliases."""

    _ALIASES = {
        "accepted": {"accepted", "accept", "ok", "valid", "confirmed"},
        "needs_review": {
            "needs_review",
            "needs-review",
            "review",
            "rejected",
            "invalid",
            "unconfirmed",
        },
    }

    def __new__(cls, value):
        obj = str.__new__(cls, value)
        obj.value = value
        obj.name = value.upper()
        return obj

    def __eq__(self, other):
        if isinstance(other, str):
            canonical = str(self)
            if other in self._ALIASES.get(canonical, ()):
                return True
            return canonical == other
        return str.__eq__(self, other)

    def __bool__(self):
        return str(self) == "accepted"

    def __hash__(self):
        return str.__hash__(self)


class SequenceStatus:
    """The verdict for one received non-empty fragment."""

    ACCEPTED = _Status("accepted")
    NEEDS_REVIEW = _Status("needs_review")
    VALID = ACCEPTED
    OK = ACCEPTED
    CONFIRMED = ACCEPTED
    INVALID = NEEDS_REVIEW
    REVIEW = NEEDS_REVIEW
    REJECTED = NEEDS_REVIEW


ACCEPTED = SequenceStatus.ACCEPTED
NEEDS_REVIEW = SequenceStatus.NEEDS_REVIEW
VALID = ACCEPTED
OK = ACCEPTED
CONFIRMED = ACCEPTED
INVALID = NEEDS_REVIEW
REVIEW = NEEDS_REVIEW
REJECTED = NEEDS_REVIEW
RecordStatus = SequenceStatus
SequenceVerdict = SequenceStatus


class ByteSpan(int):
    """A 0-based start offset that also remembers its end.

    It behaves like the start offset as an integer, and also compares equal to
    a ``(start, end)`` tuple.  The end is retained in :attr:`end` so callers
    can obtain a complete source range without losing integer convenience.

    The empty range ``ByteSpan(5, 5)`` is a cursor position; a non-empty range
    ``ByteSpan(5, 8)`` covers bytes ``5``, ``6`` and ``7``.
    """

    def __new__(cls, start, end=None):
        obj = int.__new__(cls, int(start))
        obj.start = int(start)
        obj.end = int(start if end is None else end)
        return obj

    @property
    def offset(self):
        return int(self)

    @property
    def span(self):
        return (self.start, self.end)

    def __getitem__(self, key):
        if key in (0, -2):
            return self.start
        if key in (1, -1):
            return self.end
        raise IndexError(key)

    def __len__(self):
        return 2

    def __iter__(self):
        return iter((self.start, self.end))

    def __eq__(self, other):
        if isinstance(other, tuple):
            return (self.start, self.end) == other
        return int.__eq__(self, other)

    def __hash__(self):
        return int.__hash__(self)

    def __repr__(self):
        return f"ByteSpan({self.start}, {self.end})"


class SequenceError(ValueError):
    """Details of why a fragment was not accepted."""

    def __init__(self, reason, position, end=None, kind="invalid"):
        super().__init__(reason)
        self.reason = reason
        self.message = reason
        self.kind = kind
        self.position = ByteSpan(position, end)
        self.start = self.position.start
        self.end = self.position.end
        self.offset = int(self.position)

    def __repr__(self):
        return f"SequenceError({self.reason!r}, position={self.position!r})"


class SequenceRecord:
    """The result of consuming one actual, non-empty sequence fragment.

    ``value`` is the value received for the fragment as far as it could be
    read.  For a damaged fragment it may contain fields read before the error;
    such a value is *not* committed unless :attr:`accepted` is true.
    """

    def __init__(
        self,
        *,
        value,
        accepted,
        start,
        end,
        raw,
        text,
        rs_offset,
        terminated_by_rs,
        error=None,
        final=True,
        raw_truncated=False,
    ):
        self.value = value
        self.accepted = bool(accepted)
        self.start = int(start)
        self.end = int(end)
        self.raw = raw
        self.text = text
        self.raw_truncated = bool(raw_truncated)
        self.rs_offset = rs_offset
        self.terminated_by_rs = bool(terminated_by_rs)
        self.error = error
        self.final = bool(final)
        self._position = ByteSpan(self.start, self.end)

    # -- verdict aliases -------------------------------------------------
    @property
    def status(self):
        return ACCEPTED if self.accepted else NEEDS_REVIEW

    @property
    def outcome(self):
        return self.status

    @property
    def verdict(self):
        return self.status

    @property
    def review_status(self):
        return self.status

    @property
    def ok(self):
        return self.accepted

    @property
    def valid(self):
        return self.accepted

    @property
    def is_valid(self):
        return self.accepted

    @property
    def is_accepted(self):
        return self.accepted

    @property
    def committed(self):
        return self.accepted

    @property
    def is_committed(self):
        return self.accepted

    @property
    def needs_review(self):
        return not self.accepted

    @property
    def provisional(self):
        return self.value

    @property
    def provisional_value(self):
        return self.value

    @property
    def received_value(self):
        return self.value

    @property
    def committed_value(self):
        return self.value if self.accepted else None

    @property
    def accepted_value(self):
        return self.value if self.accepted else None

    # -- source position aliases ----------------------------------------
    @property
    def position(self):
        """0-based byte span of the fragment contents (after the RS)."""
        return self._position

    @property
    def span(self):
        return (self.start, self.end)

    @property
    def bounds(self):
        return (self.start, self.end)

    @property
    def byte_range(self):
        return (self.start, self.end)

    @property
    def byte_start(self):
        return self.start

    @property
    def byte_end(self):
        return self.end

    @property
    def start_byte(self):
        return self.start

    @property
    def end_byte(self):
        return self.end

    @property
    def start_offset(self):
        return self.start

    @property
    def end_offset(self):
        return self.end

    @property
    def offset(self):
        return self.start

    @property
    def source_offset(self):
        return self.rs_offset if self.rs_offset is not None else self.start

    @property
    def record_offset(self):
        return self.source_offset

    @property
    def record_start(self):
        return self.source_offset

    @property
    def record_end(self):
        return self.end

    @property
    def source_start(self):
        return self.source_offset

    @property
    def source_end(self):
        return self.end

    @property
    def frame_start(self):
        return self.source_offset

    @property
    def frame_end(self):
        return self.end

    @property
    def end_rs_offset(self):
        return self.end if self.terminated_by_rs else None

    @property
    def next_rs_offset(self):
        return self.end_rs_offset

    @property
    def content_start(self):
        return self.start

    @property
    def content_end(self):
        return self.end

    @property
    def fragment(self):
        return self.raw

    @property
    def payload(self):
        return self.raw

    # -- error aliases ---------------------------------------------------
    @property
    def reason(self):
        return None if self.error is None else self.error.reason

    @property
    def error_message(self):
        return self.reason

    @property
    def error_position(self):
        return None if self.error is None else self.error.position

    @property
    def error_offset(self):
        return None if self.error is None else self.error.offset

    @property
    def kind(self):
        return None if self.error is None else self.error.kind

    @property
    def reason_kind(self):
        return self.kind

    # -- convenience -----------------------------------------------------
    def to_standard_types(self):
        from json_stream.util import to_standard_types

        return to_standard_types(self.value)

    def as_dict(self):
        return {
            "value": self.value,
            "status": self.status,
            "accepted": self.accepted,
            "needs_review": self.needs_review,
            "start": self.start,
            "end": self.end,
            "position": self.position,
            "rs_offset": self.rs_offset,
            "raw": self.raw,
            "text": self.text,
            "error": self.error,
        }

    def as_tuple(self):
        return (self.value, self.status, self.position)

    def __len__(self):
        return 3

    def __iter__(self):
        return iter(self.as_tuple())

    def __getitem__(self, key):
        if isinstance(key, int):
            return self.as_tuple()[key]
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key) from None

    def __contains__(self, key):
        return hasattr(self, key)

    def __eq__(self, other):
        if isinstance(other, SequenceRecord):
            return (
                self.accepted == other.accepted
                and self.start == other.start
                and self.end == other.end
                and self.raw == other.raw
            )
        if isinstance(other, (tuple, list)):
            candidates = (
                (self.value, self.status),
                (self.value, self.accepted),
                (self.value, self.status, self.start, self.end),
                (self.value, self.accepted, self.start, self.end),
            )
            return tuple(other) in candidates
        return NotImplemented

    __hash__ = None

    def __repr__(self):
        verdict = "accepted" if self.accepted else "needs-review"
        return (
            f"<SequenceRecord {verdict} "
            f"bytes[{self.start}:{self.end}] value={self.value!r}>"
        )


class FeedResult(list):
    """The records completed by one :meth:`JSONTextSequenceReader.feed` call.

    ``pending`` is a snapshot of the fragment that was still being received
    when that particular ``feed`` call returned.
    """

    def __init__(self, *args, pending=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.pending = pending

    @property
    def completed(self):
        return list(self)

    @property
    def record(self):
        return self[-1] if self else None

    @property
    def current(self):
        if self:
            return self[-1]
        return self.pending

    @property
    def all(self):
        """Completed records plus the pending fragment snapshot, if any."""
        records = list(self)
        if self.pending is not None:
            records.append(self.pending)
        return records

    @property
    def records(self):
        return self.all

    def __getattr__(self, name):
        # Allow a feed result with a single record to be used like that
        # record, e.g. ``reader.feed(chunk).status``.
        current = self.current
        if current is not None and hasattr(current, name):
            return getattr(current, name)
        raise AttributeError(name)


class _Token:
    __slots__ = ("kind", "value", "start", "end", "terminated_by_whitespace")

    def __init__(
        self, kind, value, start, end, terminated_by_whitespace=False
    ):
        self.kind = kind
        self.value = value
        self.start = int(start)
        self.end = int(end)
        self.terminated_by_whitespace = bool(terminated_by_whitespace)

    def __repr__(self):  # pragma: no cover
        return (
            f"_Token({self.kind!r}, {self.value!r}, "
            f"{self.start}, {self.end})"
        )


class _LexerState:
    WHITESPACE = 0
    STRING = 1
    STRING_ESCAPE = 2
    STRING_UNICODE = 3
    STRING_UNICODE_SURROGATE = 4
    STRING_EXPECT_LOW_SURROGATE = 5
    STRING_LOW_UNICODE = 6
    NUMBER = 10
    NUMBER_SIGN = 11
    NUMBER_ZERO = 12
    NUMBER_INTEGER = 13
    NUMBER_DOT = 14
    NUMBER_FRACTION = 15
    NUMBER_EXPONENT_START = 16
    NUMBER_EXPONENT_SIGN = 17
    NUMBER_EXPONENT = 18
    LITERAL = 20


class _FragmentParser:
    """Incrementally tokenise and parse the contents of one fragment."""

    def __init__(self, fragment_start, max_preview=DEFAULT_MAX_PREVIEW):
        self.fragment_start = int(fragment_start)
        self.max_preview = max_preview

        self._decoder = codecs.getincrementaldecoder("utf-8")("strict")
        self._bytes_fed = 0

        self._raw_preview = bytearray()
        self._raw_truncated = False
        self._text_preview = []
        self._text_preview_size = 0

        # lexer
        self.lex_state = _LexerState.WHITESPACE
        self.token_start = self.fragment_start
        self.token_end = self.fragment_start
        self.token_chars = []
        self.number_state = None
        self.literal_target = None
        self.literal_index = 0
        self.string_escape_state = None
        self.unicode_digits = []
        self.high_surrogate = None

        # parser
        self.root_value = None
        self.root_started = False
        self.root_complete = False
        self.root_is_number = False
        self.root_number_terminated_by_whitespace = False
        self.frames = []

        self.error = None
        self.saw_non_whitespace = False

    # -- public-ish helpers ---------------------------------------------
    @property
    def has_content(self):
        return (
            self._bytes_fed > 0
            or self.root_started
            or self.error is not None
        )

    @property
    def raw(self):
        return bytes(self._raw_preview)

    @property
    def text(self):
        return "".join(self._text_preview)

    @property
    def raw_truncated(self):
        return self._raw_truncated

    @property
    def temporary_value(self):
        """The value received so far, including an in-progress root scalar."""
        if self.root_started:
            return self.root_value
        state = self.lex_state
        if _LexerState.NUMBER <= state <= _LexerState.NUMBER_EXPONENT:
            text = "".join(self.token_chars)
            if text and text not in ("-",):
                try:
                    return json.loads(text)
                except (ValueError, TypeError):
                    return None
            return None
        if state in (_LexerState.STRING, _LexerState.STRING_ESCAPE):
            return "".join(self.token_chars)
        return None

    def _append_preview_bytes(self, data):
        remaining = self.max_preview - len(self._raw_preview)
        if remaining > 0:
            self._raw_preview.extend(data[:remaining])
        if len(data) > remaining:
            self._raw_truncated = True

    def _append_preview_chars(self, chars):
        if self._text_preview_size >= self.max_preview:
            self._raw_truncated = True
            return
        room = self.max_preview - self._text_preview_size
        piece = chars[:room]
        self._text_preview.append(piece)
        self._text_preview_size += len(piece)
        if len(chars) > room:
            self._raw_truncated = True

    def _set_error(self, reason, start, end=None, kind="invalid"):
        if self.error is None:
            self.error = SequenceError(reason, start, end, kind=kind)

    # -- feeding ---------------------------------------------------------
    def feed(self, data):
        if not data:
            return
        self._append_preview_bytes(data)

        if self.error is not None and self.error.kind == "invalid_utf8":
            # Still advance the byte counter; the record is already damaged.
            self._bytes_fed += len(data)
            return

        pending_before = self._decoder.getstate()[0]
        decode_base = (
            self.fragment_start + self._bytes_fed - len(pending_before)
        )
        try:
            chars = self._decoder.decode(data, final=False)
        except UnicodeDecodeError as e:
            error_start = decode_base + e.start
            error_end = decode_base + max(e.end, e.start + 1)
            self._set_error(
                f"Invalid UTF-8 in JSON text sequence fragment: {e.reason}",
                error_start,
                error_end,
                kind="invalid_utf8",
            )
            self._bytes_fed += len(data)
            return

        self._bytes_fed += len(data)
        self._append_preview_chars(chars)

        byte_pos = decode_base
        for ch in chars:
            if self.error is not None:
                # Keep a correct byte position for the final record span even
                # though no further JSON interpretation is attempted.
                break
            char_end = byte_pos + len(ch.encode("utf-8"))
            self._process_char(ch, byte_pos, char_end)
            byte_pos = char_end

    def finish(self, end_offset, terminated_by_rs):
        """Finalise the fragment and return its parsed state."""
        if self.error is None:
            try:
                chars = self._decoder.decode(b"", final=True)
            except UnicodeDecodeError as e:
                pending = self._decoder.getstate()[0]
                error_start = end_offset - len(pending)
                error_end = end_offset
                self._set_error(
                    "Invalid UTF-8 at end of JSON text sequence fragment",
                    error_start,
                    error_end,
                    kind="invalid_utf8",
                )
                chars = ""
            if chars:
                self._append_preview_chars(chars)
                byte_pos = end_offset - len(
                    chars.encode("utf-8")
                )
                for ch in chars:
                    char_end = byte_pos + len(ch.encode("utf-8"))
                    self._process_char(ch, byte_pos, char_end)
                    byte_pos = char_end

        if self.error is None:
            self._finish_token(end_offset, terminated_by_rs)
        if self.error is None:
            self._check_complete(end_offset)

        return self.error is None

    # -- lexer -----------------------------------------------------------
    def _process_char(self, ch, start, end):
        state = self.lex_state
        if state == _LexerState.WHITESPACE:
            self._begin_token_or_whitespace(ch, start, end)
        elif state == _LexerState.STRING:
            self._process_string_char(ch, start, end)
        elif state == _LexerState.STRING_ESCAPE:
            self._process_string_escape(ch, start, end)
        elif state == _LexerState.STRING_UNICODE:
            self._process_unicode_char(ch, start, end)
        elif state == _LexerState.STRING_LOW_UNICODE:
            self._process_unicode_char(ch, start, end)
        elif state == _LexerState.STRING_UNICODE_SURROGATE:
            self._process_surrogate_escape(ch, start, end)
        elif state == _LexerState.STRING_EXPECT_LOW_SURROGATE:
            self._process_low_surrogate_char(ch, start, end)
        elif state == _LexerState.LITERAL:
            self._process_literal_char(ch, start, end)
        elif _LexerState.NUMBER <= state <= _LexerState.NUMBER_EXPONENT:
            self._process_number_char(ch, start, end)

    def _begin_token_or_whitespace(self, ch, start, end):
        if ch in _JSON_WHITESPACE:
            return
        if self.root_complete:
            self._set_error(
                "Extra characters after the single root JSON value",
                start,
                end,
                kind="trailing_data",
            )
            return
        self.saw_non_whitespace = True
        if ch in _STRUCTURAL:
            self._emit(_Token("op", ch, start, end))
            return
        if ch == '"':
            self.lex_state = _LexerState.STRING
            self.token_start = start
            self.token_end = end
            self.token_chars = []
            return
        if ch == "-" or "0" <= ch <= "9":
            self.lex_state = _LexerState.NUMBER
            self.number_state = (
                _LexerState.NUMBER_SIGN
                if ch == "-"
                else _LexerState.NUMBER_ZERO
                if ch == "0"
                else _LexerState.NUMBER_INTEGER
            )
            self.token_start = start
            self.token_end = end
            self.token_chars = [ch]
            return
        if ch == "t":
            self._begin_literal("true", start, end)
            return
        if ch == "f":
            self._begin_literal("false", start, end)
            return
        if ch == "n":
            self._begin_literal("null", start, end)
            return
        self._set_error(f"Invalid JSON character: {ch!r}", start, end)

    def _begin_literal(self, target, start, end):
        self.lex_state = _LexerState.LITERAL
        self.literal_target = target
        self.literal_index = 1
        self.token_start = start
        self.token_end = end

    def _process_literal_char(self, ch, start, end):
        target = self.literal_target
        index = self.literal_index
        if index >= len(target) or ch != target[index]:
            self._set_error(f"Invalid JSON character: {ch!r}", start, end)
            return
        self.token_end = end
        index += 1
        self.literal_index = index
        if index == len(target):
            value = target == "true" if target != "null" else None
            kind = "null" if target == "null" else target
            self.lex_state = _LexerState.WHITESPACE
            self._emit(_Token(kind, value, self.token_start, self.token_end))
            self.token_chars = []
            self.literal_target = None
            self.literal_index = 0

    def _process_string_char(self, ch, start, end):
        if ch == '"':
            self.token_end = end
            self.lex_state = _LexerState.WHITESPACE
            self._emit(
                _Token(
                    "string",
                    "".join(self.token_chars),
                    self.token_start,
                    self.token_end,
                )
            )
            self.token_chars = []
            return
        if ch == "\\":
            self.lex_state = _LexerState.STRING_ESCAPE
            self.token_end = end
            return
        if ord(ch) < 0x20:
            self._set_error(
                "Invalid control character in JSON string", start, end
            )
            return
        self.token_chars.append(ch)
        self.token_end = end

    def _process_string_escape(self, ch, start, end):
        simple = {
            '"': '"',
            "\\": "\\",
            "/": "/",
            "b": "\b",
            "f": "\f",
            "n": "\n",
            "r": "\r",
            "t": "\t",
        }
        if ch in simple:
            self.token_chars.append(simple[ch])
            self.token_end = end
            self.lex_state = _LexerState.STRING
        elif ch == "u":
            self.unicode_digits = []
            self.lex_state = _LexerState.STRING_UNICODE
            self.token_end = end
        else:
            self._set_error(f"Invalid string escape: {ch}", start, end)

    def _process_unicode_char(self, ch, start, end):
        unicode_state = self.lex_state
        if ch not in "0123456789abcdefABCDEF":
            self._set_error(f"Invalid unicode literal: \\u{ch}", start, end)
            return
        self.unicode_digits.append(ch)
        self.token_end = end
        if len(self.unicode_digits) != 4:
            return

        code_point = int("".join(self.unicode_digits), 16)
        self.unicode_digits = []
        if unicode_state == _LexerState.STRING_LOW_UNICODE:
            if not 0xDC00 <= code_point <= 0xDFFF:
                self._set_error(
                    "Second half of UTF-16 surrogate pair is not a surrogate",
                    start,
                    end,
                )
                return
            high = self.high_surrogate
            self.high_surrogate = None
            combined = 0x10000 + ((high - 0xD800) << 10) + (
                code_point - 0xDC00
            )
            self.token_chars.append(chr(combined))
            self.lex_state = _LexerState.STRING
            return

        if 0xD800 <= code_point <= 0xDBFF:
            self.high_surrogate = code_point
            self.lex_state = _LexerState.STRING_UNICODE_SURROGATE
        elif 0xDC00 <= code_point <= 0xDFFF:
            self._set_error("Unpaired UTF-16 low surrogate", start, end)
        else:
            self.token_chars.append(chr(code_point))
            self.lex_state = _LexerState.STRING

    def _process_surrogate_escape(self, ch, start, end):
        if ch != "\\":
            self._set_error("Unpaired UTF-16 surrogate", start, end)
            return
        self.lex_state = _LexerState.STRING_EXPECT_LOW_SURROGATE
        self.token_end = end

    def _process_low_surrogate_char(self, ch, start, end):
        if ch != "u":
            self._set_error("Unpaired UTF-16 surrogate", start, end)
            return
        self.unicode_digits = []
        self.lex_state = _LexerState.STRING_LOW_UNICODE
        self.token_end = end

    def _process_number_char(self, ch, start, end):
        state = self.number_state
        if state == _LexerState.NUMBER_SIGN:
            if "0" <= ch <= "9":
                self.token_chars.append(ch)
                self.number_state = (
                    _LexerState.NUMBER_ZERO
                    if ch == "0"
                    else _LexerState.NUMBER_INTEGER
                )
                self.token_end = end
            else:
                self._set_error(
                    f"A - must be followed by a digit. Got {ch!r}",
                    start,
                    end,
                )
            return

        if state == _LexerState.NUMBER_ZERO:
            if ch == ".":
                self.token_chars.append(ch)
                self.number_state = _LexerState.NUMBER_DOT
                self.token_end = end
            elif ch in "eE":
                self.token_chars.append(ch)
                self.number_state = _LexerState.NUMBER_EXPONENT_START
                self.token_end = end
            elif ch in _JSON_WHITESPACE or ch in _STRUCTURAL:
                self._finalize_number(True, ch in _JSON_WHITESPACE)
                self._process_char(ch, start, end)
            else:
                self._set_error(
                    f"A 0 must be followed by '.', 'e' or a delimiter. "
                    f"Got {ch!r}",
                    start,
                    end,
                )
            return

        if state == _LexerState.NUMBER_INTEGER:
            if "0" <= ch <= "9":
                self.token_chars.append(ch)
                self.token_end = end
            elif ch == ".":
                self.token_chars.append(ch)
                self.number_state = _LexerState.NUMBER_DOT
                self.token_end = end
            elif ch in "eE":
                self.token_chars.append(ch)
                self.number_state = _LexerState.NUMBER_EXPONENT_START
                self.token_end = end
            elif ch in _JSON_WHITESPACE or ch in _STRUCTURAL:
                self._finalize_number(True, ch in _JSON_WHITESPACE)
                self._process_char(ch, start, end)
            else:
                self._set_error(
                    f"A number must contain only digits. Got {ch!r}",
                    start,
                    end,
                )
            return

        if state == _LexerState.NUMBER_DOT:
            if "0" <= ch <= "9":
                self.token_chars.append(ch)
                self.number_state = _LexerState.NUMBER_FRACTION
                self.token_end = end
            else:
                self._set_error(
                    "A number with a decimal point must be followed by "
                    f"a fractional part. Got {ch!r}",
                    start,
                    end,
                )
            return

        if state == _LexerState.NUMBER_FRACTION:
            if "0" <= ch <= "9":
                self.token_chars.append(ch)
                self.token_end = end
            elif ch in "eE":
                self.token_chars.append(ch)
                self.number_state = _LexerState.NUMBER_EXPONENT_START
                self.token_end = end
            elif ch in _JSON_WHITESPACE or ch in _STRUCTURAL:
                self._finalize_number(True, ch in _JSON_WHITESPACE)
                self._process_char(ch, start, end)
            else:
                self._set_error(
                    f"A number must include only digits. Got {ch!r}",
                    start,
                    end,
                )
            return

        if state == _LexerState.NUMBER_EXPONENT_START:
            if ch in "+-" or "0" <= ch <= "9":
                self.token_chars.append(ch)
                self.number_state = (
                    _LexerState.NUMBER_EXPONENT
                    if "0" <= ch <= "9"
                    else _LexerState.NUMBER_EXPONENT_SIGN
                )
                self.token_end = end
            else:
                self._set_error(
                    "An e in a number must be followed by '+', '-' or "
                    f"a digit. Got {ch!r}",
                    start,
                    end,
                )
            return

        if state == _LexerState.NUMBER_EXPONENT_SIGN:
            if "0" <= ch <= "9":
                self.token_chars.append(ch)
                self.number_state = _LexerState.NUMBER_EXPONENT
                self.token_end = end
            else:
                self._set_error(
                    f"A number exponent must start with a digit. Got {ch!r}",
                    start,
                    end,
                )
            return

        if state == _LexerState.NUMBER_EXPONENT:
            if "0" <= ch <= "9":
                self.token_chars.append(ch)
                self.token_end = end
            elif ch in _JSON_WHITESPACE or ch in _STRUCTURAL:
                self._finalize_number(True, ch in _JSON_WHITESPACE)
                self._process_char(ch, start, end)
            else:
                self._set_error(
                    f"A number exponent must consist only of digits. "
                    f"Got {ch!r}",
                    start,
                    end,
                )

    def _finalize_number(self, terminated, terminated_by_whitespace):
        if self.number_state in (
            _LexerState.NUMBER_SIGN,
            _LexerState.NUMBER_DOT,
            _LexerState.NUMBER_EXPONENT_START,
            _LexerState.NUMBER_EXPONENT_SIGN,
        ):
            self._set_error(
                "Incomplete number at end of JSON fragment",
                self.token_end,
                self.token_end,
                kind="incomplete",
            )
            return
        text = "".join(self.token_chars)
        try:
            value = json.loads(text)
        except (ValueError, TypeError):
            self._set_error(
                f"Invalid number: {text}", self.token_start, self.token_end
            )
            return
        token = _Token(
            "number",
            value,
            self.token_start,
            self.token_end,
            terminated_by_whitespace=terminated_by_whitespace,
        )
        self.lex_state = _LexerState.WHITESPACE
        self.number_state = None
        self.token_chars = []
        self._emit(token)

    def _finish_token(self, end_offset, terminated_by_rs):
        state = self.lex_state
        if state == _LexerState.WHITESPACE:
            return
        if state in (
            _LexerState.STRING,
            _LexerState.STRING_ESCAPE,
            _LexerState.STRING_UNICODE,
            _LexerState.STRING_LOW_UNICODE,
            _LexerState.STRING_UNICODE_SURROGATE,
            _LexerState.STRING_EXPECT_LOW_SURROGATE,
        ):
            self._set_error(
                "Unterminated string at end of file",
                end_offset,
                end_offset,
                kind="incomplete",
            )
            return
        if state == _LexerState.LITERAL:
            self._set_error(
                f"Incomplete JSON literal: {self.literal_target}",
                end_offset,
                end_offset,
                kind="incomplete",
            )
            return
        if _LexerState.NUMBER <= state <= _LexerState.NUMBER_EXPONENT:
            self.token_end = end_offset
            # An RS is a framing delimiter, but it is not the whitespace
            # required after a top-level number.
            self._finalize_number(True, False)

    # -- parser ----------------------------------------------------------
    def _emit(self, token):
        if self.error is not None:
            return
        if self.root_complete:
            self._set_error(
                "Extra characters after the single root JSON value",
                token.start,
                token.end,
                kind="trailing_data",
            )
            return

        if not self.frames:
            self._parse_root_token(token)
            return

        frame = self.frames[-1]
        if frame["kind"] == "object":
            self._parse_object_token(frame, token)
        else:
            self._parse_array_token(frame, token)

    def _parse_root_token(self, token):
        if token.kind == "op" and token.value == "{":
            value = {}
            self.root_value = value
            self.root_started = True
            self.frames.append(
                {"kind": "object", "value": value, "state": "first_key"}
            )
            return
        if token.kind == "op" and token.value == "[":
            value = []
            self.root_value = value
            self.root_started = True
            self.frames.append(
                {"kind": "array", "value": value, "state": "first_value"}
            )
            return
        if token.kind in ("string", "number", "true", "false", "null"):
            self.root_value = token.value
            self.root_started = True
            self.root_complete = True
            if token.kind == "number":
                self.root_is_number = True
                self.root_number_terminated_by_whitespace = (
                    token.terminated_by_whitespace
                )
            return
        self._set_error(
            f"Expecting a JSON value, got {self._describe(token)}",
            token.start,
            token.end,
        )

    def _parse_object_token(self, frame, token):
        state = frame["state"]
        value = frame["value"]
        if state == "first_key":
            if token.kind == "op" and token.value == "}":
                self._close_container()
                return
            if token.kind != "string":
                self._set_error(
                    f"Expecting string key or }}, got "
                    f"{self._describe(token)}",
                    token.start,
                    token.end,
                )
                return
            frame["key"] = token.value
            frame["state"] = "colon"
            return

        if state == "colon":
            if token.kind == "op" and token.value == ":":
                frame["state"] = "value"
            else:
                self._set_error(
                    f"Expecting :, got {self._describe(token)}",
                    token.start,
                    token.end,
                )
            return

        if state == "value":
            if token.kind == "op" and token.value == "{":
                child = {}
                value[frame["key"]] = child
                frame["state"] = "comma_or_end"
                self.frames.append(
                    {"kind": "object", "value": child, "state": "first_key"}
                )
                return
            if token.kind == "op" and token.value == "[":
                child = []
                value[frame["key"]] = child
                frame["state"] = "comma_or_end"
                self.frames.append(
                    {
                        "kind": "array",
                        "value": child,
                        "state": "first_value",
                    }
                )
                return
            if token.kind in ("string", "number", "true", "false", "null"):
                value[frame["key"]] = token.value
                frame["state"] = "comma_or_end"
                return
            self._set_error(
                f"Expecting value, got {self._describe(token)}",
                token.start,
                token.end,
            )
            return

        if state == "comma_or_end":
            if token.kind == "op" and token.value == ",":
                frame["state"] = "key"
            elif token.kind == "op" and token.value == "}":
                self._close_container()
            else:
                self._set_error(
                    f"Expecting comma or }}, got "
                    f"{self._describe(token)}",
                    token.start,
                    token.end,
                )
            return

        if state == "key":
            if token.kind != "string":
                self._set_error(
                    f"Expecting string key after comma, got "
                    f"{self._describe(token)}",
                    token.start,
                    token.end,
                )
                return
            frame["key"] = token.value
            frame["state"] = "colon"

    def _parse_array_token(self, frame, token):
        state = frame["state"]
        value = frame["value"]
        if state == "first_value":
            if token.kind == "op" and token.value == "]":
                self._close_container()
                return
            self._start_array_value(frame, token)
            return

        if state == "value":
            self._start_array_value(frame, token)
            return

        if state == "comma_or_end":
            if token.kind == "op" and token.value == ",":
                frame["state"] = "value"
            elif token.kind == "op" and token.value == "]":
                self._close_container()
            else:
                self._set_error(
                    f"Expecting comma or ], got {self._describe(token)}",
                    token.start,
                    token.end,
                )

    def _start_array_value(self, frame, token):
        value = frame["value"]
        if token.kind == "op" and token.value == "{":
            child = {}
            value.append(child)
            frame["state"] = "comma_or_end"
            self.frames.append(
                {"kind": "object", "value": child, "state": "first_key"}
            )
            return
        if token.kind == "op" and token.value == "[":
            child = []
            value.append(child)
            frame["state"] = "comma_or_end"
            self.frames.append(
                {"kind": "array", "value": child, "state": "first_value"}
            )
            return
        if token.kind in ("string", "number", "true", "false", "null"):
            value.append(token.value)
            frame["state"] = "comma_or_end"
            return
        self._set_error(
            f"Expecting value, got {self._describe(token)}",
            token.start,
            token.end,
        )

    def _close_container(self):
        frame = self.frames.pop()
        if not self.frames:
            self.root_complete = True
            self.root_value = frame["value"]
            # Container root can never be a bare-number case.
            self.root_is_number = False

    @staticmethod
    def _describe(token):
        if token.kind == "op":
            return repr(token.value)
        if token.kind == "string":
            return repr(token.value)
        return repr(token.value)

    # -- final verdict ---------------------------------------------------
    def _check_complete(self, end_offset):
        if not self.root_started:
            self._set_error(
                "No JSON value found in non-empty fragment",
                self.fragment_start,
                self.fragment_start,
                kind="invalid",
            )
            return
        if not self.root_complete:
            if self.frames:
                kind = self.frames[-1]["kind"]
                reason = (
                    "Unterminated object at end of file"
                    if kind == "object"
                    else "Unterminated list at end of file"
                )
            else:
                reason = "Unexpected end of file"
            self._set_error(
                reason, end_offset, end_offset, kind="incomplete"
            )
            return
        if self.root_is_number and not self.root_number_terminated_by_whitespace:
            self._set_error(
                "Top-level number requires trailing JSON whitespace",
                end_offset,
                end_offset,
                kind="incomplete_number",
            )


class JSONTextSequenceReader:
    """Incremental reader for RFC 7464 JSON Text Sequences.

    The reader accepts a file-like object, an iterable of ``bytes`` or ``str``
    chunks, or a complete ``bytes``/``str`` value.  It can also be driven
    explicitly with :meth:`feed` and :meth:`finish`.

    Every non-empty fragment (bytes between two actual RS bytes, or after the
    last RS up to EOF) is reported as a :class:`SequenceRecord`.  Empty
    fragments from consecutive RS bytes do not produce records.  Offsets always
    refer to the actual input bytes; strings are measured as UTF-8.

    ``persistent`` controls the shape of ``record.value``: transient streaming
    objects by default, persistent dict/list objects when true.  The fragment
    verdict itself always comes from an incremental parse of the received
    bytes, so a damaged fragment never needs to be buffered in full and a
    valid fragment does not have to arrive in one read.
    """

    def __init__(
        self,
        source=None,
        persistent=False,
        tokenizer=None,
        chunk_size=DEFAULT_CHUNK_SIZE,
        max_preview=DEFAULT_MAX_PREVIEW,
        **tokenizer_kwargs
    ):
        self.persistent = persistent
        self.tokenizer = tokenizer
        self.tokenizer_kwargs = tokenizer_kwargs
        self.chunk_size = chunk_size
        self.max_preview = max_preview

        self._absolute = 0
        self._fragment_start = 0
        self._rs_offset = None
        self._closed = False
        self._queue = deque()
        self._records = []
        self._parser = None

        if source is None:
            self._chunks = None
        else:
            self._chunks = iter(self._source_chunks(source))

    @property
    def records(self):
        return self._records

    @property
    def fragments(self):
        return self._records

    @property
    def results(self):
        return self._records

    @property
    def accepted(self):
        return [r.value for r in self._records if r.accepted]

    @property
    def accepted_values(self):
        return self.accepted

    @property
    def committed(self):
        return self.accepted

    @property
    def committed_values(self):
        return self.accepted

    @property
    def needs_review(self):
        return [r for r in self._records if not r.accepted]

    @property
    def all_records(self):
        records = list(self._records)
        pending = self.pending
        if pending is not None:
            records.append(pending)
        return records

    @property
    def all(self):
        return self.all_records

    @property
    def latest(self):
        pending = self.pending
        if pending is not None:
            return pending
        return self._records[-1] if self._records else None

    @property
    def result(self):
        return self.latest

    @property
    def current(self):
        if self._queue:
            return self._queue[-1]
        return self._records[-1] if self._records else None

    @property
    def pending(self):
        """The fragment currently being received, if it is non-empty."""
        return self._pending_record()

    def _pending_record(self):
        if self._parser is None or self._parser._bytes_fed == 0:
            return None
        return self._make_record(
            self._fragment_start,
            self._absolute,
            terminated_by_rs=False,
            final=False,
        )

    # -- feeding ---------------------------------------------------------
    @staticmethod
    def _normalize_chunk(chunk):
        if isinstance(chunk, str):
            return chunk.encode("utf-8")
        if isinstance(chunk, memoryview):
            return chunk.tobytes()
        if isinstance(chunk, bytearray):
            return bytes(chunk)
        if isinstance(chunk, bytes):
            return chunk
        raise TypeError(
            "JSON text sequence chunks must be bytes or str, "
            f"not {type(chunk).__name__}"
        )

    def feed(self, chunk):
        """Feed one chunk and return the records completed by an RS in it."""
        if self._closed:
            raise ValueError("Cannot feed a closed JSON text sequence reader")
        data = self._normalize_chunk(chunk)
        completed = []
        i = 0
        while True:
            j = data.find(RS_BYTE, i)
            if j < 0:
                self._feed_content(data[i:])
                break
            self._feed_content(data[i:j])
            record = self._finish_fragment(
                terminated_by_rs=True, end_offset=self._absolute
            )
            if record is not None:
                completed.append(record)
            # Only an actual RS byte changes framing.  Escaped RS and line
            # feeds remain ordinary fragment content.
            self._absolute += 1
            self._fragment_start = self._absolute
            self._rs_offset = self._absolute - 1
            self._parser = self._new_parser()
            i = j + 1
        result = FeedResult(completed, pending=self._pending_record())
        return result

    def finish(self):
        """Finish the stream, reporting a final fragment if one is buffered."""
        if self._closed:
            return FeedResult()
        self._closed = True
        record = self._finish_fragment(
            terminated_by_rs=False, end_offset=self._absolute
        )
        completed = [] if record is None else [record]
        return FeedResult(completed)

    def _new_parser(self):
        return _FragmentParser(self._fragment_start, self.max_preview)

    def _feed_content(self, data):
        if not data:
            return
        if self._parser is None:
            self._parser = self._new_parser()
        self._parser.feed(data)
        self._absolute += len(data)

    def _finish_fragment(self, terminated_by_rs, end_offset):
        parser = self._parser
        if parser is None:
            return None
        # An empty fragment from consecutive RS bytes produces no record.
        if parser._bytes_fed == 0:
            self._parser = None
            return None
        parser.finish(end_offset, terminated_by_rs)
        record = self._make_record(
            self._fragment_start,
            end_offset,
            terminated_by_rs=terminated_by_rs,
            final=True,
            parser=parser,
        )
        self._parser = None
        if record is not None:
            self._records.append(record)
        return record

    def _make_record(
        self,
        start,
        end,
        terminated_by_rs,
        final,
        parser=None,
    ):
        parser = parser or self._parser
        if parser is None:
            return None
        error = parser.error
        accepted = final and error is None and parser.root_complete
        if final and parser.root_started is False and error is None:
            # Defensive: a non-empty fragment with no value is never accepted.
            accepted = False
        return SequenceRecord(
            value=(
                parser.root_value
                if final or parser.root_started
                else parser.temporary_value
            ),
            accepted=accepted,
            start=start,
            end=end,
            raw=parser.raw,
            text=parser.text if not parser.raw_truncated else None,
            rs_offset=self._rs_offset,
            terminated_by_rs=terminated_by_rs,
            error=error,
            final=final,
            raw_truncated=parser.raw_truncated,
        )

    # -- source iteration ------------------------------------------------
    def _source_chunks(self, source):
        if isinstance(source, (bytes, bytearray, memoryview, str)):
            data = self._normalize_chunk(source)
            for i in range(0, len(data), self.chunk_size):
                yield data[i:i + self.chunk_size]
            return

        stream = ensure_file(source)
        while True:
            try:
                chunk = stream.read(self.chunk_size)
            except TypeError:
                chunk = stream.read()
            if not chunk:
                return
            yield chunk

    def __iter__(self):
        return self

    def __next__(self):
        while not self._queue:
            if self._closed:
                raise StopIteration
            if self._chunks is None:
                self._queue.extend(self.finish())
                continue
            try:
                chunk = next(self._chunks)
            except StopIteration:
                self._queue.extend(self.finish())
                continue
            self._queue.extend(self.feed(chunk))
        return self._queue.popleft()

    def read_all(self):
        return list(self)

    # -- alternate method names -----------------------------------------
    push = feed
    write = feed
    consume = feed
    update = feed
    close = finish


def load_sequence(
    fp_or_iterable,
    persistent=False,
    tokenizer=None,
    chunk_size=DEFAULT_CHUNK_SIZE,
    **tokenizer_kwargs
):
    """Iterate over an RFC 7464 JSON Text Sequence.

    Yields a :class:`SequenceRecord` for every actual non-empty fragment.
    Damaged fragments are reported (never raised) so reading continues from
    the next real RS byte.
    """
    return JSONTextSequenceReader(
        fp_or_iterable,
        persistent=persistent,
        tokenizer=tokenizer,
        chunk_size=chunk_size,
        **tokenizer_kwargs
    )


# Descriptive aliases for callers that prefer different naming conventions.
iter_sequence = load_sequence
read_sequence = load_sequence
load_sequence_many = load_sequence
load_many_sequence = load_sequence
sequence_records = load_sequence
load_json_sequence = load_sequence
load_text_sequence = load_sequence
load_json_text_sequence = load_sequence
load_rfc7464 = load_sequence
load_rfc_7464 = load_sequence
iter_rfc7464 = load_sequence
parse_rfc7464 = load_sequence
read_rfc7464 = load_sequence
read_json_sequence = load_sequence
read_json_text_sequence = load_sequence
iter_json_text_sequence = load_sequence
parse_sequence = load_sequence

SequenceReader = JSONTextSequenceReader
RFC7464Reader = JSONTextSequenceReader
RFC7464SequenceReader = JSONTextSequenceReader
JSONTextSequenceParser = JSONTextSequenceReader
JSONTextSequence = JSONTextSequenceReader

__all__ = [
    "ACCEPTED",
    "NEEDS_REVIEW",
    "VALID",
    "OK",
    "CONFIRMED",
    "INVALID",
    "REVIEW",
    "REJECTED",
    "RS",
    "RS_BYTE",
    "RS_CHAR",
    "RECORD_SEPARATOR",
    "RECORD_SEPARATOR_BYTE",
    "RECORD_SEPARATOR_CHAR",
    "ByteSpan",
    "FeedResult",
    "SequenceError",
    "SequenceRecord",
    "SequenceStatus",
    "RecordStatus",
    "SequenceVerdict",
    "JSONTextSequenceReader",
    "SequenceReader",
    "RFC7464Reader",
    "RFC7464SequenceReader",
    "JSONTextSequenceParser",
    "JSONTextSequence",
    "load_sequence",
    "iter_sequence",
    "read_sequence",
    "load_sequence_many",
    "load_many_sequence",
    "sequence_records",
    "load_json_sequence",
    "load_text_sequence",
    "load_json_text_sequence",
    "load_rfc7464",
    "load_rfc_7464",
    "iter_rfc7464",
    "parse_rfc7464",
    "read_rfc7464",
    "read_json_sequence",
    "read_json_text_sequence",
    "iter_json_text_sequence",
    "parse_sequence",
]
