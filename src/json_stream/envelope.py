"""
Split a one-shot chunked UTF-8 byte response after its first JSON root.

An `EnvelopeStream` wraps a one-shot iterable of byte chunks (a response
body) and lets an integrating program peek at a bounded prefix to identify
the kind of the first JSON root before committing to one of two paths:

* replay the whole response, losslessly, through `json_stream.load_many()`
* parse the first root as a routing envelope, then hand the raw bytes that
  follow it (untouched, including whitespace and already-buffered bytes) to
  a downstream processor that only accepts business data

Only one path may be chosen per response.
"""
from json_stream.base import StreamingJSONBase
from json_stream.loader import load_many as _load_many
from json_stream.select_tokenizer import default_tokenizer
from json_stream.tokenizer import TokenType, tokenize

_WHITESPACE = b' \t\r\n'

_ROOT_TYPES = {
    ord('{'): 'object',
    ord('['): 'array',
    ord('"'): 'string',
    ord('t'): 'boolean',
    ord('f'): 'boolean',
    ord('n'): 'null',
    ord('N'): 'number',  # NaN
    ord('I'): 'number',  # Infinity
    ord('-'): 'number',
}
_ROOT_TYPES.update({ord(digit): 'number' for digit in '0123456789'})


class _CharStream:
    """
    Text stream over an iterator of byte chunks, read exactly one character
    at a time (the access pattern of `json_stream.tokenizer.tokenize`).

    UTF-8 is decoded incrementally so that multi-byte characters may be
    split across chunk boundaries. The raw bytes of the most recently
    returned character are remembered so that a single read-ahead character
    (the delimiter that terminates a top-level number) can be returned to
    the raw byte stream on handoff.
    """

    def __init__(self, chunks):
        self._chunks = iter(chunks)
        self._current = b''
        self._pos = 0
        self._last_char_bytes = None
        self.eof = False

    def _read_byte(self):
        while self._pos == len(self._current):
            try:
                self._current = next(self._chunks)
            except StopIteration:
                return None
            self._pos = 0
        byte = self._current[self._pos:self._pos + 1]
        self._pos += 1
        return byte

    def read(self, size=1):
        if size == 0:
            return ''
        if size != 1:
            raise ValueError('_CharStream only supports reading one character at a time')
        first = self._read_byte()
        if first is None:
            self.eof = True
            return ''
        byte0 = first[0]
        if byte0 < 0x80:
            self._last_char_bytes = first
            return first.decode('ascii')
        if 0xC2 <= byte0 <= 0xDF:
            needed = 2
        elif 0xE0 <= byte0 <= 0xEF:
            needed = 3
        elif 0xF0 <= byte0 <= 0xF4:
            needed = 4
        else:
            raise ValueError(f'Invalid UTF-8 start byte: 0x{byte0:02x}')
        raw = bytearray(first)
        while len(raw) < needed:
            byte = self._read_byte()
            if byte is None:
                raise ValueError('Truncated UTF-8 sequence at end of stream')
            raw += byte
        self._last_char_bytes = bytes(raw)
        return self._last_char_bytes.decode('utf-8')

    def unread_last_char(self):
        raw = self._last_char_bytes
        self._last_char_bytes = None
        return raw

    def remaining_raw(self, prefix=b''):
        if prefix:
            yield prefix
        if self._pos < len(self._current):
            yield self._current[self._pos:]
            self._pos = len(self._current)
        for chunk in self._chunks:
            if chunk:
                yield chunk


class EnvelopeStream:
    """
    Wraps a one-shot iterable of UTF-8 byte chunks.

    Probing (`peek`/`peek_root_type`) pulls only as many chunks as the
    requested budget requires and never consumes them. Afterwards exactly
    one of two paths may be chosen:

    * `load_many()` - replay the entire original response through
      `json_stream.load_many()`, including everything already probed
    * `load_envelope()` + `remaining_bytes()` - parse the first root as a
      routing envelope, then hand off the raw bytes that follow it
    """

    def __init__(self, chunks):
        self._source = iter(chunks)
        self._buffer = bytearray()
        self._path = None
        self._root = None
        self._char_stream = None
        self._last_token_type = None
        self._handed_off = False

    @staticmethod
    def _normalize(chunk):
        if isinstance(chunk, str):
            return chunk.encode('utf-8')
        if isinstance(chunk, (bytes, bytearray, memoryview)):
            return bytes(chunk)
        raise TypeError(f'chunks must be bytes-like or str, not {type(chunk).__name__}')

    def _fill(self, budget):
        while len(self._buffer) < budget:
            try:
                chunk = self._normalize(next(self._source))
            except StopIteration:
                return
            if chunk:  # skip empty chunks
                self._buffer += chunk

    def peek(self, budget):
        """
        Return up to `budget` bytes from the start of the response without
        consuming them. May return fewer if the response ends first.
        """
        if self._path is not None:
            raise RuntimeError(f'cannot peek: response already committed to the {self._path} path')
        if budget < 0:
            raise ValueError('budget must be non-negative')
        self._fill(budget)
        return bytes(self._buffer[:budget])

    def peek_root_type(self, budget):
        """
        Return the kind of the first JSON root ('object', 'array', 'string',
        'number', 'boolean' or 'null') determinable within `budget` bytes,
        or None if the budget only covers whitespace. Raises ValueError if
        the first non-whitespace byte cannot start a JSON value.
        """
        for byte in self.peek(budget):
            if byte in _WHITESPACE:
                continue
            try:
                return _ROOT_TYPES[byte]
            except KeyError:
                raise ValueError(f'Invalid JSON character: {chr(byte)!r}') from None
        return None

    def _choose(self, path):
        if self._path is not None:
            raise RuntimeError(f'response already committed to the {self._path} path')
        self._path = path

    def _replay_chunks(self):
        if self._buffer:
            yield bytes(self._buffer)
            self._buffer = bytearray()
        for chunk in self._source:
            chunk = self._normalize(chunk)
            if chunk:
                yield chunk

    def load_many(self, persistent=False, tokenizer=default_tokenizer, **tokenizer_kwargs):
        """
        Path 1: hand the whole original response, losslessly, to
        `json_stream.load_many()`. Bytes already pulled while probing are
        replayed first, so nothing is lost.
        """
        self._choose('load_many')
        return _load_many(self._replay_chunks(), persistent=persistent,
                          tokenizer=tokenizer, **tokenizer_kwargs)

    def load_envelope(self, persistent=False):
        """
        Path 2: parse the first root of the response as a routing envelope
        and return it (a streaming item unless persistent=True). Parsing is
        streamed, so the envelope may be arbitrarily large. Consume the
        returned root, then call `remaining_bytes()` to hand off the rest.
        """
        self._choose('envelope')
        char_stream = _CharStream(self._replay_chunks())
        token_stream = self._track(tokenize(char_stream))
        self._char_stream = char_stream
        for token_type, token in token_stream:
            if token_type == TokenType.OPERATOR:
                root = StreamingJSONBase.factory(token, token_stream, persistent)
            else:
                root = token
            break
        else:
            raise ValueError('No JSON value found')
        self._root = root
        return root

    def _track(self, tokens):
        for token_type, token in tokens:
            self._last_token_type = token_type
            yield token_type, token

    def remaining_bytes(self):
        """
        Return an iterator of the raw byte chunks that follow the envelope,
        starting at the first byte after the first root. All original
        whitespace and any already-buffered downstream bytes are preserved
        and the bytes are yielded exactly as received, regardless of how
        the response was chunked. The envelope must be syntactically
        complete: a corrupt envelope raises ValueError and no handoff
        happens.
        """
        if self._path != 'envelope':
            raise RuntimeError('remaining_bytes() is only available after load_envelope()')
        if self._handed_off:
            raise RuntimeError('remaining_bytes() can only be called once')
        root = self._root
        if isinstance(root, StreamingJSONBase):
            root.read_all()  # drains the envelope; raises ValueError if corrupt or truncated
        self._handed_off = True
        pushback = b''
        if self._last_token_type == TokenType.NUMBER and not self._char_stream.eof:
            # a top-level number is terminated by a delimiter character that
            # the tokenizer had to read but did not consume; return its raw
            # bytes to the stream so the handoff starts after the number
            pushback = self._char_stream.unread_last_char()
        return self._char_stream.remaining_raw(pushback)
