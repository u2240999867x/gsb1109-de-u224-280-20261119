"""
Byte-position indexing for raw, UTF-8 encoded JSON.

The regular :mod:`json_stream` tokenizer only hands back *decoded* values.
A decoded string cannot be turned back into the original literal, because the
source may spell the same text with raw multi-byte characters, with
``\\uXXXX`` escapes, or with a mix of both (re-encoding the decoded value
always picks one spelling).  This module therefore indexes the original byte
stream directly instead of any re-encoded copy.

Every reported value carries a half-open byte range ``[start, end)`` counted
from the very first byte of the physical stream (an optional leading UTF-8
BOM occupies bytes 0-2).  Slicing the original bytes with that range returns
the exact original literal, including the quotes around a string or the
brackets around a container, with no neighbouring whitespace or separators.

The parser is fully incremental: bytes are pulled through fixed-size binary
reads (a chunk may end in the middle of a multi-byte UTF-8 character, an
escape sequence or the BOM), token offsets are accounted as bytes are
consumed, and events are yielded as soon as each value finishes.  The whole
document is never read into memory and a token's end is never inferred from
the position of the next physical read.

Decoded text streams are accepted for convenience but cannot give back
genuine source bytes: their leaf events carry ``raw=None`` and
:func:`extract` raises :class:`ByteIndexUnavailableError` unless the caller
retains and supplies the original bytes.  Their offsets assume the JSON
default UTF-8 encoding.
"""
import json
from typing import Any, Iterator, NamedTuple, Optional, Tuple

from json_stream.iterators import ensure_file

__all__ = [
    'ByteIndexUnavailableError',
    'IndexedValue',
    'ByteIndexer',
    'visit_byte_ranges',
    'extract',
]


class ByteIndexUnavailableError(TypeError):
    """Original source bytes cannot be recovered for an indexed event.

    Events produced from a decoded text stream carry no faithful literal
    (after decoding, escapes and raw multi-byte characters are
    indistinguishable).  Retain the binary source and pass it to
    :func:`extract`, or index a binary stream directly.
    """


def _decode(literal: bytes, start: int):
    try:
        return json.loads(literal)
    except json.JSONDecodeError as error:
        raise ValueError(
            f"Invalid JSON value {literal!r} at index {start}: {error.msg}"
        ) from None


class IndexedValue(NamedTuple):
    """A completed JSON value located in the original byte stream.

    ``path`` is the tuple of object keys and list indices also used by
    :func:`json_stream.visit`, so repeated keys produce several events that
    share one path.  ``[start, end)`` is a half-open byte range slicing the
    original stream back to the exact original literal.  ``raw`` caches those
    bytes for leaf values coming from a binary source (``None`` otherwise;
    containers are never cached to keep memory bounded).  ``container`` marks
    closed object/array events, whose ``value`` is ``None``; truncated
    containers produce no event at all and are listed in
    :attr:`ByteIndexer.incomplete_paths`.
    """

    path: tuple
    value: Any
    start: int
    end: int
    raw: Optional[bytes]
    complete: bool
    encoding: str
    container: bool

    @property
    def span(self) -> Tuple[int, int]:
        return self.start, self.end


# scanner states
_WS = 0
_STRING = 1
_STRING_ESCAPE = 2
_UNICODE = 3
_NUMBER = 4
_LITERAL = 5

# object/list frame states
_EXPECT_KEY_OR_CLOSE = 'expect_key_or_close'
_EXPECT_COLON = 'expect_colon'
_EXPECT_VALUE = 'expect_value'
_EXPECT_VALUE_OR_CLOSE = 'expect_value_or_close'
_EXPECT_COMMA_OR_CLOSE = 'expect_comma_or_close'

_WHITESPACE = frozenset((0x20, 0x09, 0x0A, 0x0D))
_HEX = frozenset(b'0123456789abcdefABCDEF')
_DELIMITERS = _WHITESPACE | frozenset(b'{}[]:,')
_QUOTE = 0x22
_BACKSLASH = 0x5C

# fixed-spelling tokens (NaN and Infinity are json-stream extensions)
_LITERAL_SPELLINGS = {
    0x74: b'true',
    0x66: b'false',
    0x6E: b'null',
    0x4E: b'NaN',
    0x49: b'Infinity',
}
_LITERAL_VALUES = {
    b'true': True,
    b'false': False,
    b'null': None,
    b'NaN': float('nan'),
    b'Infinity': float('inf'),
    b'-Infinity': float('-inf'),
}


class _Frame:
    __slots__ = ('kind', 'start', 'state', 'count', 'key')

    def __init__(self, kind: str, start: int):
        self.kind = kind  # '{' or '['
        self.start = start
        self.state = (
            _EXPECT_KEY_OR_CLOSE if kind == '{' else _EXPECT_VALUE_OR_CLOSE
        )
        self.count = 0
        self.key = None


class _ByteFeeder:
    """Single-byte reader with exact offsets, fed by fixed-size reads."""

    def __init__(self, stream, chunk_size: int):
        self._stream = ensure_file(stream)
        self._chunk_size = chunk_size
        self._buffer = bytearray()
        self._pushback = bytearray()
        self.position = 0
        probe = self._stream.read(0)
        self.binary = isinstance(probe, bytes)
        self.encoding = 'utf-8'
        self.bom_size = 0
        self._bom_checked = False

    def _refill(self) -> bool:
        chunk = self._stream.read(self._chunk_size)
        if not chunk:
            return False
        if self.binary:
            self._buffer.extend(chunk)
        else:
            # offsets are defined against the UTF-8 encoding of the text
            self._buffer.extend(chunk.encode('utf-8'))
        return True

    def _resolve_bom(self):
        # Decide about a leading BOM without confusing a BOM split across
        # chunk boundaries with a genuine non-BOM 0xef byte.
        while not self._bom_checked:
            if not self._buffer:
                if not self._refill():
                    self._bom_checked = True  # empty or partial prefix at EOF
                    return
                continue
            if self._buffer[0] != 0xEF:
                self._bom_checked = True  # first byte rules a BOM out
                return
            if len(self._buffer) < 2:
                if not self._refill():
                    self._bom_checked = True
                    return
                continue
            if self._buffer[1] != 0xBB:
                self._bom_checked = True
                return
            if len(self._buffer) < 3:
                if not self._refill():
                    self._bom_checked = True
                    return
                continue
            if self._buffer[2] == 0xBF:
                del self._buffer[:3]
                self.bom_size = 3
                self.position = 3
            self._bom_checked = True

    def read_byte(self) -> Optional[int]:
        if self._pushback:
            byte = self._pushback.pop()
            self.position += 1
            return byte
        self._resolve_bom()
        if not self._buffer and not self._refill():
            return None
        if not self._buffer:
            return None
        byte = self._buffer.pop(0)
        self.position += 1
        return byte

    def unread(self, byte: int):
        # only single-byte ASCII grammar delimiters are ever pushed back
        self._pushback.append(byte)
        self.position -= 1


class ByteIndexer:
    """Incremental byte-position indexer for a JSON byte or text stream.

    Iterate :meth:`events` to receive :class:`IndexedValue` objects as values
    complete.  Afterwards :attr:`incomplete_paths` lists the paths of
    containers whose closing bracket was never reached (a truncated stream);
    the completed children of such containers are still reported.
    """

    def __init__(self, stream, chunk_size: int = 8192):
        if chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer")
        self._feeder = _ByteFeeder(stream, chunk_size)
        self._stack = []
        self._path = []
        self.incomplete_paths = []
        self.encoding = self._feeder.encoding
        self.document_count = 0

    @property
    def bom_size(self) -> int:
        """Number of leading BOM bytes (0 or 3), detected on first read."""
        return self._feeder.bom_size

    def events(self) -> Iterator[IndexedValue]:
        feeder = self._feeder
        state = _WS
        token_start = 0
        raw = bytearray()
        unicode_hex = bytearray()
        literal_bytes = b''
        literal_remaining = 0

        while True:
            byte = feeder.read_byte()
            if byte is None:
                break
            offset = feeder.position - 1

            if state == _WS:
                if byte in _WHITESPACE:
                    continue
                token_start = offset
                if byte in (0x7B, 0x5B):  # { or [
                    event = self._open_container(chr(byte), token_start)
                    if event is not None:
                        yield event
                    continue
                if byte in (0x7D, 0x5D, 0x3A, 0x2C):  # } ] : ,
                    event = self._operator(chr(byte))
                    if event is not None:
                        yield event
                    continue
                if byte == _QUOTE:
                    state = _STRING
                    raw = bytearray(b'"')
                    continue
                if byte == 0x2D or 0x30 <= byte <= 0x39:  # number
                    state = _NUMBER
                    raw = bytearray((byte,))
                    continue
                if byte in (0x74, 0x66, 0x6E, 0x4E, 0x49):  # t/f/n/N/I
                    state = _LITERAL
                    literal_bytes = bytes((byte,))
                    literal_remaining = len(_LITERAL_SPELLINGS[byte]) - 1
                    continue
                raise ValueError(
                    f"Invalid JSON character: {byte:#x} at index {offset}"
                )

            if state == _STRING:
                raw.append(byte)
                if byte == _QUOTE:
                    event = self._string_completed(bytes(raw), token_start, offset + 1)
                    if event is not None:
                        yield event
                    state, raw = _WS, bytearray()
                    continue
                if byte == _BACKSLASH:
                    state = _STRING_ESCAPE
                # bytes >= 0x80 are raw multi-byte UTF-8 content and need no
                # special handling; the closing quote is always a single byte
                continue

            if state == _STRING_ESCAPE:
                raw.append(byte)
                if byte == 0x75:  # \uXXXX
                    state = _UNICODE
                    unicode_hex = bytearray()
                    continue
                if byte in b'"\\/bfnrt':
                    state = _STRING
                    continue
                raise ValueError(f"Invalid string escape at index {offset}")

            if state == _UNICODE:
                raw.append(byte)
                if byte not in _HEX:
                    raise ValueError(f"Invalid unicode literal at index {offset}")
                unicode_hex.append(byte)
                if len(unicode_hex) == 4:
                    # surrogate pairs are just another \uXXXX escape as far as
                    # locating the closing quote goes; json.loads validates
                    state = _STRING
                continue

            if state == _NUMBER:
                if byte in _DELIMITERS:
                    feeder.unread(byte)
                    literal = bytes(raw)
                    yield self._leaf(
                        _decode(literal, token_start), token_start,
                        feeder.position, literal,
                    )
                    state, raw = _WS, bytearray()
                    continue
                if byte == 0x49 and bytes(raw) == b'-':  # -Infinity extension
                    state = _LITERAL
                    literal_bytes = b'-I'
                    literal_remaining = len(b'-Infinity') - 2
                    continue
                if (0x30 <= byte <= 0x39) or byte in b'.eE+-':
                    raw.append(byte)
                    continue
                raise ValueError(f"Invalid number character at index {offset}")

            # state == _LITERAL: true/false/null/NaN/Infinity/-Infinity
            expected = _LITERAL_SPELLINGS.get(literal_bytes[0], b'-Infinity')
            if byte != expected[len(literal_bytes)]:
                raise ValueError(
                    f"Invalid literal character {byte:#x} at index {offset}"
                )
            literal_bytes += bytes((byte,))
            literal_remaining -= 1
            if literal_remaining == 0:
                yield self._leaf(
                    _LITERAL_VALUES[literal_bytes], token_start, offset + 1,
                    literal_bytes,
                )
                state, raw = _WS, bytearray()
            continue

        # stream exhausted: finalise a primitive ending exactly at EOF
        if state in (_NUMBER, _LITERAL):
            if state == _NUMBER:
                literal = bytes(raw)
                yield self._leaf(
                    _decode(literal, token_start), token_start,
                    feeder.position, literal,
                )
            else:
                raise ValueError("Unterminated literal at end of file")
        elif state != _WS:
            raise ValueError("Unterminated JSON value at end of file")

        if self._stack:
            # the frame at stack index i was opened under the first i path
            # entries; none of these containers has a complete, re-sliceable
            # range, so report their paths rather than emit events
            self.incomplete_paths = [
                tuple(self._path[:i]) for i in range(len(self._stack))
            ]

    # ------------------------------------------------------------------ #
    # grammar bookkeeping
    # ------------------------------------------------------------------ #

    def _open_container(self, kind: str, start: int) -> Optional[IndexedValue]:
        self._enter_value()
        self._stack.append(_Frame(kind, start))
        return None

    def _operator(self, op: str) -> Optional[IndexedValue]:
        if not self._stack:
            raise ValueError(f"Unexpected operator {op!r} at top level")
        frame = self._stack[-1]
        if op == ':':
            if frame.state != _EXPECT_COLON:
                raise ValueError("Unexpected ':'")
            frame.state = _EXPECT_VALUE
            return None
        if op == ',':
            if frame.state != _EXPECT_COMMA_OR_CLOSE:
                raise ValueError("Unexpected ','")
            frame.state = (
                _EXPECT_KEY_OR_CLOSE if frame.kind == '{'
                else _EXPECT_VALUE_OR_CLOSE
            )
            return None
        if op == '}' or op == ']':
            closer = '}' if frame.kind == '{' else ']'
            if op != closer:
                raise ValueError(f"Mismatched closing bracket {op!r}")
            if frame.state not in (
                _EXPECT_KEY_OR_CLOSE, _EXPECT_VALUE_OR_CLOSE, _EXPECT_COMMA_OR_CLOSE,
            ):
                raise ValueError(f"Unexpected closing bracket {op!r}")
            return self._close_container(frame)
        raise ValueError(f"Unknown operator {op!r}")  # pragma: no cover

    def _string_completed(self, literal: bytes, start: int,
                          end: int) -> Optional[IndexedValue]:
        frame = self._stack[-1] if self._stack else None
        if (frame is not None and frame.kind == '{'
                and frame.state == _EXPECT_KEY_OR_CLOSE):
            frame.key = _decode(literal, start)
            frame.state = _EXPECT_COLON
            return None
        return self._leaf(_decode(literal, start), start, end, literal)

    def _enter_value(self):
        """Validate value position and extend the path for an opening value."""
        if not self._stack:
            return
        frame = self._stack[-1]
        if frame.kind == '{':
            if frame.state != _EXPECT_VALUE:
                raise ValueError("Expecting value after ':'")
            self._path.append(frame.key)
            frame.key = None
        else:
            if frame.state not in (_EXPECT_VALUE_OR_CLOSE, _EXPECT_VALUE):
                raise ValueError("Expecting value in list")
            self._path.append(frame.count)
            frame.count += 1
        frame.state = _EXPECT_COMMA_OR_CLOSE

    def _leaf(self, value: Any, start: int, end: int,
              literal: bytes) -> IndexedValue:
        if not self._stack:
            self.document_count += 1
            path = ()
        else:
            frame = self._stack[-1]
            if frame.kind == '{':
                if frame.state != _EXPECT_VALUE:
                    raise ValueError("Expecting value after ':'")
                path = tuple(self._path) + (frame.key,)
                frame.key = None
            else:
                if frame.state not in (_EXPECT_VALUE_OR_CLOSE, _EXPECT_VALUE):
                    raise ValueError("Expecting value in list")
                path = tuple(self._path) + (frame.count,)
                frame.count += 1
            frame.state = _EXPECT_COMMA_OR_CLOSE
        raw = literal if self._feeder.binary else None
        return IndexedValue(tuple(path), value, start, end, raw, True,
                            self.encoding, False)

    def _close_container(self, frame: _Frame) -> IndexedValue:
        end = self._feeder.position  # one past the closing bracket
        event = IndexedValue(
            tuple(self._path), None, frame.start, end, None, True,
            self.encoding, True,
        )
        self._stack.pop()
        if self._stack:
            self._path.pop()
        else:
            self.document_count += 1
        return event


def visit_byte_ranges(stream, chunk_size: int = 8192) -> Iterator[IndexedValue]:
    """Yield :class:`IndexedValue` objects from a JSON stream incrementally.

    ``stream`` is a binary or text file-like object or an iterable of chunks,
    as accepted by :func:`json_stream.load`.  Reads are capped at
    ``chunk_size`` bytes, so a chunk boundary may fall in the middle of a
    multi-byte character, an escape or the BOM without affecting offsets.
    """
    return ByteIndexer(stream, chunk_size=chunk_size).events()


def extract(source, event: IndexedValue) -> bytes:
    """Return the exact original literal bytes covered by ``event``.

    ``source`` is the retained ``bytes``/``bytearray``/``memoryview`` or a
    seekable binary file over the same stream.  Leaf events from a binary
    source also cache the bytes in ``event.raw``.  Raises
    :class:`ByteIndexUnavailableError` when no genuine source bytes exist
    (events indexed from a decoded text stream and no binary source given).
    """
    if event.raw is not None:
        return event.raw
    if source is None:
        raise ByteIndexUnavailableError(
            "This event carries no original bytes (it was indexed from a text "
            "stream); retain the binary source and pass it to extract()"
        )
    if isinstance(source, (bytes, bytearray, memoryview)):
        return bytes(source[event.start:event.end])
    if hasattr(source, 'seek') and hasattr(source, 'read'):
        source.seek(event.start)
        data = source.read(event.end - event.start)
        if not isinstance(data, bytes):
            raise ByteIndexUnavailableError(
                "Source is a text stream; byte ranges need a binary source"
            )
        return data
    raise ByteIndexUnavailableError(
        "Cannot recover original bytes from this source; keep the raw bytes "
        "alongside the index"
    )
