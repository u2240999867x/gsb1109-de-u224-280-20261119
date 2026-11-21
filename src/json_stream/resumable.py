"""Resumable ``visit_many`` support for concatenated JSON documents.

A capture file may receive several complete JSON texts (roots) one after the
other, without any framing marker.  A reader can be interrupted at any byte,
including in the middle of a root.  This module records a *document level*
checkpoint only after a whole root has passed syntax validation, and lets a
later invocation continue at the first root that was never confirmed.

The checkpoint is deliberately expressed in terms of the raw byte archive:

``confirmed_roots``
    Number of roots at the beginning of the file whose syntax was verified.
``position``
    Exclusive byte offset of the end of the last confirmed root.  This is the
    logical resume point, not the furthest byte the previous reader happened
    to fetch from the source.
``digest``
    SHA-256 over exactly ``source_bytes[0:position]``.  It binds the
    checkpoint to the confirmed bytes.  Replacing the file with different
    bytes of the same length therefore cannot silently reuse the checkpoint.

The reader requires a re-openable, seekable *binary* source.  One-shot
iterables and text streams are rejected because exact byte offsets and prefix
verification are impossible for them.
"""

from __future__ import annotations

import hashlib
import hmac
import inspect
import io
import json
import os
import re
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Iterator, Optional, Tuple

CHECKPOINT_VERSION = 1

_WHITESPACE = b" \t\r\n"
_STRUCTURAL = b"{}[]:,"
_DIGITS = b"0123456789"


class EventKind(str, Enum):
    FIELD = "field"
    ROOT = "root"
    INCOMPLETE = "incomplete"
    INVALID = "invalid"


class ResumableVisitError(ValueError):
    """Base class for resumable visit errors."""


class UnsupportedSourceError(TypeError, ResumableVisitError):
    """The source cannot provide exact, re-readable raw bytes."""


class SourceChangedError(ResumableVisitError):
    """The bytes before the checkpoint no longer match the saved credential.

    The caller must explicitly re-confirm the source (for example by starting
    a fresh visit with ``reconfirm=True``) instead of continuing from a
    checkpoint that belongs to different content.
    """

    requires_reconfirmation = True

    def __init__(self, message, *, checkpoint=None, position=None, reason=None):
        super().__init__(message)
        self.checkpoint = checkpoint
        self.position = position
        self.reason = reason


class InvalidRootError(ResumableVisitError):
    """New bytes do not start with a valid JSON root.

    Such bytes are reported at the exact source offset.  They are never
    skipped in the hope of finding a later root.
    """

    def __init__(self, message, *, root_index, offset):
        super().__init__(message)
        self.root_index = root_index
        self.root_ordinal = root_index + 1
        self.offset = offset


@dataclass(frozen=True)
class Checkpoint:
    """A saveable credential for the confirmed prefix of one capture file."""

    confirmed_roots: int
    position: int
    digest: str
    source_identity: Optional[str] = None
    version: int = CHECKPOINT_VERSION

    def __post_init__(self):
        if not isinstance(self.confirmed_roots, int) or self.confirmed_roots < 0:
            raise ValueError("confirmed_roots must be a non-negative integer")
        if not isinstance(self.position, int) or self.position < 0:
            raise ValueError("position must be a non-negative integer")
        if self.confirmed_roots == 0 and self.position != 0:
            raise ValueError("a checkpoint with no confirmed roots must be at position 0")
        if not isinstance(self.digest, str) or len(self.digest) != 64:
            raise ValueError("digest must be a 64 character hex SHA-256 value")
        try:
            int(self.digest, 16)
        except ValueError:
            raise ValueError("digest must be hexadecimal") from None

    # Convenient aliases -------------------------------------------------
    @property
    def root_count(self) -> int:
        return self.confirmed_roots

    @property
    def next_root_index(self) -> int:
        return self.confirmed_roots

    @property
    def last_root_index(self) -> int:
        return self.confirmed_roots - 1

    @property
    def last_root_ordinal(self) -> int:
        return self.confirmed_roots

    @property
    def byte_position(self) -> int:
        return self.position

    @property
    def end(self) -> int:
        return self.position

    @property
    def offset(self) -> int:
        return self.position

    @property
    def checksum(self) -> str:
        return self.digest

    def to_dict(self) -> dict:
        data = {
            "version": self.version,
            "confirmed_roots": self.confirmed_roots,
            "position": self.position,
            "digest": self.digest,
        }
        if self.source_identity is not None:
            data["source_identity"] = self.source_identity
        return data

    @classmethod
    def from_dict(cls, data) -> "Checkpoint":
        if not isinstance(data, dict):
            raise TypeError("checkpoint data must be a mapping")
        version = data.get("version", CHECKPOINT_VERSION)
        if version != CHECKPOINT_VERSION:
            raise ValueError(f"unsupported checkpoint version {version!r}")
        return cls(
            confirmed_roots=data["confirmed_roots"],
            position=data["position"],
            digest=data["digest"],
            source_identity=data.get("source_identity"),
            version=version,
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Checkpoint":
        return cls.from_dict(json.loads(text))

    def save(self, path) -> None:
        """Atomically write the checkpoint as UTF-8 JSON."""
        path = os.fspath(path)
        directory = os.path.dirname(os.path.abspath(path))
        temporary = f"{path}.tmp.{os.getpid()}"
        with open(temporary, "w", encoding="utf-8") as handle:
            handle.write(self.to_json())
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if directory:
            try:
                dir_fd = os.open(directory, os.O_RDONLY)
            except OSError:
                return
            try:
                os.fsync(dir_fd)
            except OSError:
                pass
            finally:
                os.close(dir_fd)

    @classmethod
    def load(cls, path) -> "Checkpoint":
        with open(path, "r", encoding="utf-8") as handle:
            return cls.from_json(handle.read())

    def verify(self, source, *, verify="digest", source_identity=None) -> "Checkpoint":
        """Verify this checkpoint against ``source`` and return it.

        ``verify`` may be ``"digest"`` (raw confirmed bytes), ``"source"``
        (a reliable source identity), ``"both"`` or ``"none"``.  ``"digest"``
        is the default because it also detects same-length replacement whose
        prefix changed.
        """
        with _open_binary_source(source) as (handle, _owned):
            _verify_checkpoint_on_file(
                handle,
                self,
                verify=verify,
                source_identity=source_identity,
            )
        return self

    def is_valid_for(self, source, *, verify="digest", source_identity=None) -> bool:
        try:
            self.verify(source, verify=verify, source_identity=source_identity)
        except SourceChangedError:
            return False
        return True

    def rebind(self, source, *, source_identity=None) -> "Checkpoint":
        """Explicitly re-confirm ``source`` and return a fresh checkpoint.

        This does not continue the old root numbering.  It reads the prefix
        again and returns a checkpoint that describes the current bytes.  The
        caller has explicitly chosen to trust the replacement source.
        """
        with _open_binary_source(source) as (handle, _owned):
            size = _file_size(handle)
            if self.position > size:
                raise SourceChangedError(
                    "source is shorter than the checkpoint position",
                    checkpoint=self,
                    position=self.position,
                    reason="truncated",
                )
            digest = _digest_range(handle, 0, self.position)
            identity = source_identity or _source_identity(handle) or self.source_identity
            return Checkpoint(
                confirmed_roots=self.confirmed_roots,
                position=self.position,
                digest=digest,
                source_identity=identity,
            )


# ``Credential`` is a descriptive alias for callers that prefer the wording
# from the capture-device vocabulary.
Credential = Checkpoint


@dataclass
class _RootStatus:
    """Mutable status shared by all field events of one source root.

    Fields are reported as soon as they are browsed, but a root is only
    confirmed after its syntax has been verified.  Keeping one status object
    lets a caller holding an earlier :class:`FieldEvent` observe that its root
    was later confirmed (or abandoned) without buffering every value.
    """

    root_index: int
    status: str = "pending"

    @property
    def confirmed(self) -> bool:
        return self.status == "confirmed"

    @property
    def pending(self) -> bool:
        return self.status == "pending"


@dataclass(frozen=True)
class FieldEvent:
    """A value seen while a root is still being validated."""

    value: Any
    path: Tuple[Any, ...]
    root_index: int
    kind: EventKind = EventKind.FIELD
    _root: Optional[_RootStatus] = None

    @property
    def root_ordinal(self) -> int:
        return self.root_index + 1

    @property
    def confirmed(self) -> bool:
        return self._root is not None and self._root.confirmed

    @property
    def pending(self) -> bool:
        return not self.confirmed

    @property
    def status(self) -> str:
        return self._root.status if self._root is not None else "pending"

    @property
    def is_confirmed(self) -> bool:
        return self.confirmed

    @property
    def is_pending(self) -> bool:
        return self.pending


@dataclass(frozen=True)
class RootEvent:
    """A root that has passed syntax validation and is now confirmed."""

    root_index: int
    end: int
    checkpoint: Checkpoint
    kind: EventKind = EventKind.ROOT
    confirmed: bool = True
    pending: bool = False
    status: str = "confirmed"

    @property
    def root_ordinal(self) -> int:
        return self.root_index + 1

    @property
    def position(self) -> int:
        return self.checkpoint.position

    @property
    def is_confirmed(self) -> bool:
        return True

    @property
    def is_pending(self) -> bool:
        return False


@dataclass(frozen=True)
class IncompleteRootEvent:
    """A root that stopped at EOF before its syntax was verified.

    ``position`` is the last confirmed checkpoint position, while ``browsed``
    is how far the failed attempt physically read.  They are intentionally
    different: only ``position`` may be used to resume.
    """

    root_index: int
    position: int
    browsed: int
    error: BaseException
    kind: EventKind = EventKind.INCOMPLETE
    confirmed: bool = False
    pending: bool = True
    status: str = "incomplete"

    @property
    def root_ordinal(self) -> int:
        return self.root_index + 1

    @property
    def is_confirmed(self) -> bool:
        return False

    @property
    def is_pending(self) -> bool:
        return True


@dataclass(frozen=True)
class InvalidRootEvent:
    """A root boundary or syntax error that can never be completed by appending.

    ``offset`` is the first byte of the offending root.  The reader does not
    skip these bytes looking for a later root.
    """

    root_index: int
    position: int
    offset: int
    browsed: int
    error: BaseException
    kind: EventKind = EventKind.INVALID
    confirmed: bool = False
    pending: bool = True
    status: str = "invalid"

    @property
    def root_ordinal(self) -> int:
        return self.root_index + 1

    @property
    def is_confirmed(self) -> bool:
        return False

    @property
    def is_pending(self) -> bool:
        return True


def _is_path(value) -> bool:
    return isinstance(value, (str, os.PathLike))


def _file_size(handle) -> int:
    position = handle.tell()
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(position)
    return size


def _digest_range(handle, start: int, end: int, *, checkpoint=None) -> str:
    if start > end:
        raise ValueError("start must not exceed end")
    position = handle.tell()
    hasher = hashlib.sha256()
    handle.seek(start)
    remaining = end - start
    while remaining:
        chunk = handle.read(min(1024 * 1024, remaining))
        if not chunk:
            break
        hasher.update(chunk)
        remaining -= len(chunk)
    handle.seek(position)
    if remaining:
        raise SourceChangedError(
            "source ended before the checkpoint position",
            checkpoint=checkpoint,
            position=end,
            reason="truncated",
        )
    return hasher.hexdigest()


def _source_identity(handle) -> Optional[str]:
    try:
        stat = os.fstat(handle.fileno())
    except (AttributeError, OSError, io.UnsupportedOperation):
        return None
    return f"dev={stat.st_dev}:ino={stat.st_ino}"


class _SourceContext:
    def __init__(self, handle, owned):
        self.handle = handle
        self.owned = owned

    def __enter__(self):
        return self.handle, self.owned

    def __exit__(self, exc_type, exc, tb):
        if self.owned:
            self.handle.close()
        return False


def _open_binary_source(source) -> _SourceContext:
    if _is_path(source):
        return _SourceContext(open(source, "rb"), True)
    if not hasattr(source, "read") or not hasattr(source, "seek") or not hasattr(source, "tell"):
        raise UnsupportedSourceError(
            "resumable visit_many requires a re-openable raw binary archive "
            "(a path or a seekable binary file object)"
        )
    probe = source.read(0)
    if isinstance(probe, str):
        raise UnsupportedSourceError(
            "resumable visit_many requires a binary source; text streams "
            "cannot provide exact raw byte positions"
        )
    try:
        seekable = source.seekable()
    except AttributeError:
        seekable = True
    if not seekable:
        raise UnsupportedSourceError(
            "resumable visit_many requires a seekable raw binary archive"
        )
    return _SourceContext(source, False)


def _coerce_checkpoint(checkpoint) -> Optional[Checkpoint]:
    if checkpoint is None or isinstance(checkpoint, Checkpoint):
        return checkpoint
    if isinstance(checkpoint, dict):
        return Checkpoint.from_dict(checkpoint)
    if _is_path(checkpoint):
        return Checkpoint.load(checkpoint)
    raise TypeError(
        "checkpoint must be a Checkpoint, a mapping, or the path to a saved checkpoint"
    )


def _verify_checkpoint_on_file(
    handle,
    checkpoint: Checkpoint,
    *,
    verify: str = "digest",
    source_identity: Optional[str] = None,
) -> None:
    if verify not in ("digest", "source", "both", "none"):
        raise ValueError("verify must be one of 'digest', 'source', 'both' or 'none'")
    size = _file_size(handle)
    if checkpoint.position > size:
        raise SourceChangedError(
            "source is shorter than the checkpoint position; the confirmed "
            "prefix cannot be verified and the source must be re-confirmed",
            checkpoint=checkpoint,
            position=checkpoint.position,
            reason="truncated",
        )
    if verify in ("digest", "both"):
        digest = _digest_range(handle, 0, checkpoint.position, checkpoint=checkpoint)
        if not hmac.compare_digest(digest, checkpoint.digest):
            raise SourceChangedError(
                "bytes before the checkpoint position have changed; this "
                "checkpoint belongs to a different source and must not be "
                "reused. Re-confirm the source before continuing.",
                checkpoint=checkpoint,
                position=checkpoint.position,
                reason="prefix-changed",
            )
    if verify in ("source", "both"):
        expected = checkpoint.source_identity
        actual = source_identity if source_identity is not None else _source_identity(handle)
        if expected != actual:
            raise SourceChangedError(
                "source identity does not match (or cannot be established for) "
                "the checkpoint; re-confirm the source before continuing",
                checkpoint=checkpoint,
                position=checkpoint.position,
                reason="source-identity-changed",
            )
    handle.seek(checkpoint.position)


# ---------------------------------------------------------------------------
# Raw byte root boundary scanner
# ---------------------------------------------------------------------------
class _ByteScanner:
    """Sequential reader with a small buffer over ``[start, end)``."""

    def __init__(self, handle, start, end):
        self._handle = handle
        self._end = end
        self.position = start
        self._buffer = b""
        self._buffer_start = start

    def _ensure(self):
        if self.position >= self._end:
            return False
        offset = self.position - self._buffer_start
        if 0 <= offset < len(self._buffer):
            return True
        self._handle.seek(self.position)
        self._buffer = self._handle.read(min(65536, self._end - self.position))
        self._buffer_start = self.position
        return bool(self._buffer)

    def peek(self) -> bytes:
        if not self._ensure():
            return b""
        index = self.position - self._buffer_start
        return self._buffer[index:index + 1]

    def take(self) -> bytes:
        byte = self.peek()
        if byte:
            self.position += 1
        return byte

    def take_n(self, count):
        data = bytearray()
        for _ in range(count):
            byte = self.take()
            if not byte:
                break
            data.extend(byte)
        return bytes(data)



_COMPLETE = "complete"
_INCOMPLETE = "incomplete"
_INVALID = "invalid"


def _scan_literal(scanner, literal):
    remaining = literal
    while remaining:
        byte = scanner.take()
        if not byte:
            return _INCOMPLETE, scanner.position, None
        if byte != remaining[:1]:
            return _INVALID, scanner.position, "invalid literal"
        remaining = remaining[1:]
    return _COMPLETE, scanner.position, None


def _scan_string(scanner):
    escaped = False
    while True:
        byte = scanner.take()
        if not byte:
            return _INCOMPLETE, scanner.position, None
        if escaped:
            escaped = False
        elif byte == b"\\":
            escaped = True
        elif byte == b'"':
            following = scanner.peek()
            if following and following not in _WHITESPACE and following not in _STRUCTURAL:
                return _INVALID, scanner.position, "string is not followed by a delimiter"
            return _COMPLETE, scanner.position, None


def _scan_container(scanner, first):
    stack = [first]
    in_string = False
    escaped = False
    while True:
        byte = scanner.take()
        if not byte:
            return _INCOMPLETE, scanner.position, None
        if in_string:
            if escaped:
                escaped = False
            elif byte == b"\\":
                escaped = True
            elif byte == b'"':
                in_string = False
            continue
        if byte == b'"':
            in_string = True
        elif byte in (b"{", b"["):
            stack.append(byte)
        elif byte in (b"}", b"]"):
            opening = stack[-1]
            if (opening, byte) not in ((b"{", b"}"), (b"[", b"]")):
                return _INVALID, scanner.position, "mismatched closing bracket"
            stack.pop()
            if not stack:
                return _COMPLETE, scanner.position, None


def _scan_number(scanner):
    byte = scanner.take()
    if byte == b"-":
        byte = scanner.take()
        if not byte:
            return _INCOMPLETE, scanner.position, None
    if byte == b"0":
        pass
    elif byte in b"123456789":
        while scanner.peek() and scanner.peek() in _DIGITS:
            scanner.take()
    else:
        return _INVALID, scanner.position, "invalid number"

    if scanner.peek() == b".":
        scanner.take()
        digits = 0
        while scanner.peek() and scanner.peek() in _DIGITS:
            scanner.take()
            digits += 1
        if digits == 0:
            if not scanner.peek():
                return _INCOMPLETE, scanner.position, None
            return _INVALID, scanner.position, "number has no fractional part"

    if scanner.peek() in (b"e", b"E"):
        scanner.take()
        if scanner.peek() in (b"+", b"-"):
            scanner.take()
        digits = 0
        while scanner.peek() and scanner.peek() in _DIGITS:
            scanner.take()
            digits += 1
        if digits == 0:
            if not scanner.peek():
                return _INCOMPLETE, scanner.position, None
            return _INVALID, scanner.position, "number has no exponent digits"

    following = scanner.peek()
    if not following:
        # A number is only unambiguously finished when a delimiter follows.
        # Appended digits could otherwise silently extend this root.
        return _INCOMPLETE, scanner.position, None
    if following in _WHITESPACE:
        return _COMPLETE, scanner.position, None
    # Adjacent structural bytes unambiguously delimit a number.  Two adjacent
    # numbers (``12``) are one number, so any other byte is invalid.
    if following in _STRUCTURAL:
        return _COMPLETE, scanner.position, None
    return _INVALID, scanner.position, "number is not followed by a delimiter"


def _skip_whitespace(scanner):
    while scanner.peek() and scanner.peek() in _WHITESPACE:
        scanner.take()


def _scan_root(handle, start, end, *, allow_bom=False):
    """Find the first root at or after ``start``.

    Returns ``(status, root_start, root_end, error)`` where ``status`` is one
    of ``"none"``, ``"complete"``, ``"incomplete"`` or ``"invalid"``.
    """
    scanner = _ByteScanner(handle, start, end)
    if allow_bom:
        mark = scanner.position
        if scanner.peek() == b"\xef":
            bom = scanner.take_n(3)
            if bom != b"\xef\xbb\xbf":
                return _INVALID, mark, mark, "invalid UTF-8 BOM"
        elif scanner.peek() in (b"\xbb", b"\xbf"):
            return _INVALID, mark, mark, "invalid UTF-8 BOM"
    _skip_whitespace(scanner)
    root_start = scanner.position
    first = scanner.peek()
    if not first:
        return "none", root_start, root_start, None
    if first == b"{":
        status, root_end, error = _scan_container(scanner, scanner.take())
    elif first == b"[":
        status, root_end, error = _scan_container(scanner, scanner.take())
    elif first == b'"':
        scanner.take()
        status, root_end, error = _scan_string(scanner)
    elif first == b"t":
        status, root_end, error = _scan_literal(scanner, b"true")
    elif first == b"f":
        status, root_end, error = _scan_literal(scanner, b"false")
    elif first == b"n":
        status, root_end, error = _scan_literal(scanner, b"null")
    elif first in b"-" or first in _DIGITS:
        status, root_end, error = _scan_number(scanner)
    else:
        return _INVALID, root_start, scanner.position, "invalid start of root"
    if status == _COMPLETE:
        return _COMPLETE, root_start, root_end, None
    return status, root_start, root_end, error


# ---------------------------------------------------------------------------
# Parsing bounded root slices
# ---------------------------------------------------------------------------
def _parse_root_bytes(data, tokenizer, tokenizer_kwargs, emit):
    """Parse one root slice, calling ``emit(value, path)`` for each value.

    Returns ``(completed, error)``.  ``completed`` is True only when the
    parser consumed the whole slice without error.
    """
    from json_stream.base import StreamingJSONBase, StreamingJSONList, StreamingJSONObject
    from json_stream.tokenizer import TokenType

    stream = io.BytesIO(data)
    try:
        token_stream = tokenizer(stream, **tokenizer_kwargs)
        get_next = token_stream.__next__

        def walk(obj, path):
            key = None
            if isinstance(obj, StreamingJSONObject):
                for key, value in obj.items():
                    walk(value, path + (key,))
                if key is None:
                    emit({}, path)
            elif isinstance(obj, StreamingJSONList):
                for key, value in enumerate(obj):
                    walk(value, path + (key,))
                if key is None:
                    emit([], path)
            else:
                emit(obj, path)

        first_type, first_value = get_next()
        if first_type == TokenType.OPERATOR:
            obj = StreamingJSONBase.factory(first_value, token_stream, persistent=False)
            walk(obj, ())
            obj.read_all()
        else:
            emit(first_value, ())
        try:
            get_next()
        except StopIteration:
            return True, None
        return False, ValueError("trailing data after root value")
    except ValueError as error:
        return False, error


def _is_eof_error(error: BaseException) -> bool:
    message = str(error)
    markers = (
        "EOF",
        "end of file",
        "Unterminated",
    )
    return any(marker in message for marker in markers)


_TRAILING_INDEX = re.compile(r"at index (\d+)\s*$")


def _error_at_end(error: BaseException, length: int) -> bool:
    """Whether ``error`` was raised because the bounded slice ran out.

    The tokenizer reports unfinished numeric forms without the word EOF, for
    example ``3.`` yields "must be followed by a fractional part at index 2".
    Such a form is still completable by appending bytes.
    """
    if _is_eof_error(error):
        return True
    message = str(error)
    if "must be followed by a fractional part" in message:
        return True
    match = _TRAILING_INDEX.search(message)
    return bool(match) and int(match.group(1)) >= length


class ResumableVisit:
    """Iterate roots from a re-openable binary archive, yielding confirmations.

    Parameters
    ----------
    source:
        A path (``str``/``os.PathLike``) or a seekable binary file object.
    visitor:
        Optional traditional ``visitor(value, path)`` callback.  A callable
        that accepts a third positional argument also receives the richer
        :class:`FieldEvent`.  Fields of a root that is still being validated
        are reported with ``confirmed=False`` / ``pending=True``.
    checkpoint:
        A :class:`Checkpoint`, its mapping/JSON form, or a path to a saved
        checkpoint.  When supplied, the confirmed prefix is verified before
        any root is visited and confirmed roots are not visited again.
    reporter:
        Optional callback receiving every :class:`FieldEvent`,
        :class:`RootEvent`, :class:`IncompleteRootEvent` and
        :class:`InvalidRootEvent`.
    on_checkpoint:
        Optional callback invoked only after a whole root has been verified.
    verify:
        How to bind the checkpoint to the source: ``"digest"`` (default,
        raw bytes), ``"source"`` (source identity), ``"both"`` or ``"none"``.
    reconfirm:
        Explicitly trust a changed source and restart from byte 0.  Without
        this flag a prefix mismatch raises :class:`SourceChangedError`.
    """

    def __init__(
        self,
        source,
        visitor: Optional[Callable] = None,
        *,
        checkpoint=None,
        reporter: Optional[Callable] = None,
        on_checkpoint: Optional[Callable] = None,
        tokenizer=None,
        verify: str = "digest",
        source_identity: Optional[str] = None,
        reconfirm: bool = False,
        **tokenizer_kwargs,
    ):
        if tokenizer is None:
            from json_stream.select_tokenizer import default_tokenizer

            tokenizer = default_tokenizer
        self._source = source
        self._visitor = visitor
        self._reporter = reporter
        self._on_checkpoint = on_checkpoint
        self._tokenizer = tokenizer
        self._tokenizer_kwargs = tokenizer_kwargs
        self._verify = verify

        self._context = _open_binary_source(source)
        self._file = self._context.handle
        self._file.seek(0)
        self._size = _file_size(self._file)
        self._identity = source_identity or _source_identity(self._file)

        checkpoint = _coerce_checkpoint(checkpoint)
        if checkpoint is not None and reconfirm:
            checkpoint = None
            self._file.seek(0)
        elif checkpoint is not None:
            _verify_checkpoint_on_file(
                self._file,
                checkpoint,
                verify=verify,
                source_identity=source_identity,
            )

        self._checkpoint = checkpoint
        self._root_index = checkpoint.confirmed_roots if checkpoint is not None else 0
        self._confirmed_position = checkpoint.position if checkpoint is not None else 0
        self._hasher = hashlib.sha256()
        if self._confirmed_position:
            self._file.seek(0)
            remaining = self._confirmed_position
            while remaining:
                chunk = self._file.read(min(1024 * 1024, remaining))
                if not chunk:
                    raise SourceChangedError(
                        "source ended before the checkpoint position",
                        checkpoint=checkpoint,
                        position=self._confirmed_position,
                        reason="truncated",
                    )
                self._hasher.update(chunk)
                remaining -= len(chunk)
        self._file.seek(self._confirmed_position)
        self._iterator = None
        self._started = False
        self._visitor_arity = self._inspect_visitor(visitor)

    # Context manager / iterator protocol --------------------------------
    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False

    def __iter__(self):
        return self

    def __next__(self):
        if self._iterator is None:
            if self._started:
                raise RuntimeError("a ResumableVisit can only be iterated once")
            self._started = True
            self._iterator = self._run()
        return next(self._iterator)

    def close(self):
        self._context.__exit__(None, None, None)

    # Introspection ------------------------------------------------------
    @property
    def checkpoint(self) -> Optional[Checkpoint]:
        """The latest credential that has passed whole-root validation."""
        return self._checkpoint

    @property
    def root_index(self) -> int:
        """Index of the next root to be visited (zero based)."""
        return self._root_index

    @property
    def position(self) -> int:
        """Exclusive byte offset of the last confirmed root."""
        return self._confirmed_position

    @property
    def source_identity(self) -> Optional[str]:
        return self._identity

    @staticmethod
    def _inspect_visitor(visitor):
        if visitor is None:
            return 0
        try:
            signature = inspect.signature(visitor)
        except (TypeError, ValueError):
            return 2
        parameters = list(signature.parameters.values())
        if any(p.kind is inspect.Parameter.VAR_POSITIONAL for p in parameters):
            return 3
        positional = [
            p
            for p in parameters
            if p.kind
            in (inspect.Parameter.POSITIONAL_ONLY, inspect.Parameter.POSITIONAL_OR_KEYWORD)
        ]
        return 3 if len(positional) >= 3 else 2

    # Emission -----------------------------------------------------------
    def _emit(self, event) -> None:
        if self._reporter is not None:
            self._reporter(event)

    def _emit_field(self, value, path, root_status=None) -> None:
        event = FieldEvent(
            value=value,
            path=path,
            root_index=self._root_index,
            _root=root_status if root_status is not None else _RootStatus(self._root_index),
        )
        self._emit(event)
        if self._visitor is not None:
            if self._visitor_arity >= 3:
                self._visitor(value, path, event)
            else:
                self._visitor(value, path)

    def _read_slice(self, start, end) -> bytes:
        self._file.seek(start)
        chunks = []
        remaining = end - start
        while remaining:
            chunk = self._file.read(min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    # Main loop ----------------------------------------------------------
    def _run(self) -> Iterator[Any]:
        allow_bom = self._confirmed_position == 0
        while True:
            status, root_start, root_end, reason = _scan_root(
                self._file,
                self._confirmed_position,
                self._size,
                allow_bom=allow_bom,
            )
            allow_bom = False
            if status == "none":
                return

            root_status = _RootStatus(self._root_index)

            if status == _INCOMPLETE:
                data = self._read_slice(root_start, self._size)
                error_holder = []

                def emit(value, path):
                    self._emit_field(value, path, root_status)

                completed, parse_error = _parse_root_bytes(
                    data, self._tokenizer, self._tokenizer_kwargs, emit
                )
                if parse_error is not None:
                    error_holder.append(parse_error)
                if completed:
                    # The scanner saw EOF before balancing, so the parser
                    # should not have completed.  Treat it as incomplete.
                    error = ValueError("source ended before root was complete")
                    event = IncompleteRootEvent(
                        root_index=self._root_index,
                        position=self._confirmed_position,
                        browsed=self._size,
                        error=error,
                    )
                    self._emit(event)
                    yield event
                    return
                error = error_holder[0] if error_holder else ValueError(reason)
                if _error_at_end(error, len(data)):
                    event = IncompleteRootEvent(
                        root_index=self._root_index,
                        position=self._confirmed_position,
                        browsed=self._size,
                        error=error,
                    )
                    self._emit(event)
                    yield event
                    return
                root_status.status = "invalid"
                self._emit(
                    InvalidRootEvent(
                        root_index=self._root_index,
                        position=self._confirmed_position,
                        offset=root_start,
                        browsed=self._size,
                        error=error,
                    )
                )
                raise InvalidRootError(
                    f"no valid JSON root at byte {root_start} "
                    f"(root {self._root_index}): {error}",
                    root_index=self._root_index,
                    offset=root_start,
                ) from error

            if status == _INVALID:
                root_status.status = "invalid"
                error = ValueError(reason or "invalid JSON root")
                event = InvalidRootEvent(
                    root_index=self._root_index,
                    position=self._confirmed_position,
                    offset=root_start,
                    browsed=root_end,
                    error=error,
                )
                self._emit(event)
                raise InvalidRootError(
                    f"no valid JSON root at byte {root_start} "
                    f"(root {self._root_index}): {reason}",
                    root_index=self._root_index,
                    offset=root_start,
                ) from error

            # status == complete
            data = self._read_slice(root_start, root_end)

            def emit(value, path):
                self._emit_field(value, path, root_status)

            completed, parse_error = _parse_root_bytes(
                data, self._tokenizer, self._tokenizer_kwargs, emit
            )
            if not completed:
                if parse_error is not None and _is_eof_error(parse_error):
                    event = IncompleteRootEvent(
                        root_index=self._root_index,
                        position=self._confirmed_position,
                        browsed=root_end,
                        error=parse_error,
                    )
                    self._emit(event)
                    yield event
                    return
                root_status.status = "invalid"
                error = parse_error or ValueError("root did not parse completely")
                self._emit(
                    InvalidRootEvent(
                        root_index=self._root_index,
                        position=self._confirmed_position,
                        offset=root_start,
                        browsed=root_end,
                        error=error,
                    )
                )
                raise InvalidRootError(
                    f"no valid JSON root at byte {root_start} "
                    f"(root {self._root_index}): {error}",
                    root_index=self._root_index,
                    offset=root_start,
                ) from error

            checkpoint = self._confirm(root_end)
            root_status.status = "confirmed"
            event = RootEvent(
                root_index=self._root_index,
                end=root_end,
                checkpoint=checkpoint,
            )
            self._root_index += 1
            self._emit(event)
            yield event

    def _confirm(self, end: int) -> Checkpoint:
        if end < self._confirmed_position:
            raise InvalidRootError(
                "root end precedes the previously confirmed position",
                root_index=self._root_index,
                offset=end,
            )
        self._file.seek(self._confirmed_position)
        remaining = end - self._confirmed_position
        while remaining:
            chunk = self._file.read(min(1024 * 1024, remaining))
            if not chunk:
                raise InvalidRootError(
                    "source ended while recording the confirmed root",
                    root_index=self._root_index,
                    offset=end,
                )
            self._hasher.update(chunk)
            remaining -= len(chunk)
        self._file.seek(end)
        self._confirmed_position = end
        checkpoint = Checkpoint(
            confirmed_roots=self._root_index + 1,
            position=end,
            digest=self._hasher.hexdigest(),
            source_identity=self._identity,
        )
        self._checkpoint = checkpoint
        if self._on_checkpoint is not None:
            self._on_checkpoint(checkpoint)
        return checkpoint


def visit_many_checkpointed(
    source,
    visitor: Optional[Callable] = None,
    *,
    checkpoint=None,
    reporter: Optional[Callable] = None,
    on_checkpoint: Optional[Callable] = None,
    save_checkpoint=None,
    tokenizer=None,
    verify: str = "digest",
    source_identity: Optional[str] = None,
    reconfirm: bool = False,
    **tokenizer_kwargs,
) -> ResumableVisit:
    """Return a :class:`ResumableVisit` for ``source``.

    ``save_checkpoint`` may name a path; the credential is written atomically
    only after each whole root has been validated.
    """
    callback = on_checkpoint
    if save_checkpoint is not None:
        def callback_with_save(credential):
            credential.save(save_checkpoint)
            if on_checkpoint is not None:
                on_checkpoint(credential)

        callback = callback_with_save
    return ResumableVisit(
        source,
        visitor,
        checkpoint=checkpoint,
        reporter=reporter,
        on_checkpoint=callback,
        tokenizer=tokenizer,
        verify=verify,
        source_identity=source_identity,
        reconfirm=reconfirm,
        **tokenizer_kwargs,
    )


resume_visit_many = visit_many_checkpointed
resumable_visit_many = visit_many_checkpointed
visit_many_resumable = visit_many_checkpointed


def resume_from(
    source,
    checkpoint,
    visitor: Optional[Callable] = None,
    *,
    reporter: Optional[Callable] = None,
    on_checkpoint: Optional[Callable] = None,
    save_checkpoint=None,
    tokenizer=None,
    verify: str = "digest",
    source_identity: Optional[str] = None,
    reconfirm: bool = False,
    **tokenizer_kwargs,
) -> ResumableVisit:
    """Resume ``source`` from ``checkpoint`` (positional convenience form)."""
    return visit_many_checkpointed(
        source,
        visitor,
        checkpoint=checkpoint,
        reporter=reporter,
        on_checkpoint=on_checkpoint,
        save_checkpoint=save_checkpoint,
        tokenizer=tokenizer,
        verify=verify,
        source_identity=source_identity,
        reconfirm=reconfirm,
        **tokenizer_kwargs,
    )


__all__ = [
    "CHECKPOINT_VERSION",
    "Checkpoint",
    "Credential",
    "EventKind",
    "FieldEvent",
    "IncompleteRootEvent",
    "InvalidRootError",
    "InvalidRootEvent",
    "ResumableVisit",
    "ResumableVisitError",
    "RootEvent",
    "SourceChangedError",
    "UnsupportedSourceError",
    "resumable_visit_many",
    "resume_from",
    "resume_visit_many",
    "visit_many_checkpointed",
    "visit_many_resumable",
]
