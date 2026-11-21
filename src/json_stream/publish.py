"""Atomic, versioned publication of streaming JSON results.

This module adds a small publication layer on top of
:func:`json_stream.streamable_dict` and :func:`json_stream.streamable_list`.
The receiver of a publication only ever has to open the original JSON path:
every commit replaces that path atomically with a complete standard JSON
document.  Bookkeeping lives in a sidecar directory next to the JSON file.

The important properties are:

* results are encoded exactly once, directly into a temporary file, so a
  one-shot generator is never replayed;
* duplicate JSON object keys (including the ``7`` / ``"7"`` collision) are
  rejected while the temporary file is being written, before the published
  file or its credential can change;
* a publication is identified by both a monotonically increasing version and
  a random token, so two byte-identical documents can still be two different
  publications;
* a compare-and-swap ``base`` credential prevents a slow writer from
  overwriting a newer publication;
* a request id makes a commit idempotent, and a durable pending record lets a
  different process finish a commit if the publisher dies immediately after
  the atomic rename.

Typical use::

    from json_stream import streamable_dict
    from json_stream.publish import Publisher

    @streamable_dict
    def result():
        yield "answer", 42

    publisher = Publisher("result.json")
    receipt = publisher.publish(result())
    publisher.query(receipt.request_id)

The original :mod:`json_stream.writer` wrappers are unchanged and remain
usable on their own.
"""

from __future__ import annotations

import hashlib
import json
import os
import secrets
import tempfile
import threading
import time
import types
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple, Union

try:  # POSIX
    import fcntl
except ImportError:  # pragma: no cover - Windows
    fcntl = None

try:  # Windows
    import msvcrt
except ImportError:  # pragma: no cover - POSIX
    msvcrt = None

from json_stream.writer import Streamable, StreamableDict, StreamableList

__all__ = [
    "Credential",
    "Publication",
    "Publisher",
    "PublicationStore",
    "Prepared",
    "DuplicateKeyError",
    "PublicationError",
    "RequestConflictError",
    "StaleBaseError",
    "current_credential",
    "current_publication",
    "encode_streamable",
    "publish",
    "publish_file",
    "publish_status",
    "query",
    "query_publication",
    "read",
    "read_credential",
    "read_publication",
    "try_publish",
]


# ---------------------------------------------------------------------------
# Exceptions


class PublicationError(Exception):
    """Base class for semantic publication failures."""


class DuplicateKeyError(ValueError):
    """Two keys in the same JSON object encode to the same JSON key.

    ``object_path`` identifies the object that contains the collision.  It is
    a tuple of keys (strings) and list indices, starting at the document root.
    ``key`` is the encoded key and ``keys`` contains the original Python keys.
    """

    def __init__(
        self,
        object_path: Tuple[Any, ...],
        key: str,
        keys: Tuple[Any, Any],
    ) -> None:
        self.object_path = tuple(object_path)
        self.path = self.object_path
        self.key = key
        self.keys = keys
        super().__init__(
            f"duplicate JSON key {key!r} in object at "
            f"{_format_path(self.object_path)} (from {keys[0]!r} and {keys[1]!r})"
        )


class StaleBaseError(PublicationError):
    """The publication was based on a version that is no longer current."""

    status = "stale_base"

    def __init__(
        self,
        expected: Optional["Credential"],
        actual: Optional["Credential"],
        *,
        path: Union[str, os.PathLike, None] = None,
        request_id: Optional[str] = None,
    ) -> None:
        self.expected = expected
        self.actual = actual
        self.current = actual
        self.path = None if path is None else str(path)
        self.request_id = request_id
        super().__init__(
            "publication base is stale: expected "
            f"{_credential_text(expected)}, found {_credential_text(actual)}"
        )

    @property
    def result(self) -> "Publication":
        version = 0 if self.actual is None else self.actual.version
        token = None if self.actual is None else self.actual.token
        return Publication(
            path=self.path,
            version=version,
            token=token,
            request_id=self.request_id,
            status="stale_base",
        )


class RequestConflictError(PublicationError):
    """A request id was reused with a different already-committed payload."""

    status = "request_conflict"

    def __init__(self, request_id: str, existing: "Publication") -> None:
        self.request_id = request_id
        self.existing = existing
        super().__init__(
            f"request {request_id!r} already published as version "
            f"{existing.version}"
        )


# ---------------------------------------------------------------------------
# Value objects


@dataclass(frozen=True)
class Credential:
    """The scheduler's proof of the publication version it last read.

    ``version`` alone is enough to detect an ordinary later publication, but
    ``token`` is deliberately independent of the content.  Consequently a
    newly published document whose bytes equal an older document is still a
    different credential.
    """

    version: int
    token: Optional[str] = None

    def matches(self, other: Optional["Credential"]) -> bool:
        if other is None:
            return False
        if self.version >= 0 and self.version != other.version:
            return False
        if self.token is not None and self.token != other.token:
            return False
        return True

    def to_dict(self) -> Dict[str, Any]:
        return {"version": self.version, "token": self.token}
    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "Credential":
        return cls(version=int(value["version"]), token=value.get("token"))

    def __str__(self) -> str:  # pragma: no cover - convenience
        return _credential_text(self)


@dataclass(frozen=True)
class Publication:
    """A committed publication, or a semantic failure result.

    Successful results have ``status == "published"``.  A stale compare-and-
    swap is normally raised as :class:`StaleBaseError`; the exception exposes
    ``result`` and :func:`try_publish` also returns this shape directly.
    """

    path: Optional[str]
    version: int
    token: Optional[str]
    request_id: Optional[str] = None
    sha256: Optional[str] = None
    size: Optional[int] = None
    status: str = "published"
    published_at: Optional[float] = None

    @property
    def published(self) -> bool:
        return self.status == "published"

    @property
    def committed(self) -> bool:
        return self.status == "published"

    @property
    def credential(self) -> Credential:
        return Credential(self.version, self.token)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "path": self.path,
            "version": self.version,
            "token": self.token,
            "request_id": self.request_id,
            "sha256": self.sha256,
            "size": self.size,
            "status": self.status,
            "published_at": self.published_at,
        }

    @classmethod
    def from_dict(
        cls,
        value: Mapping[str, Any],
        *,
        path: Optional[str] = None,
        status: str = "published",
    ) -> "Publication":
        return cls(
            path=value.get("path", path),
            version=int(value["version"]),
            token=value.get("token"),
            request_id=value.get("request_id"),
            sha256=value.get("sha256"),
            size=value.get("size"),
            status=value.get("status", status),
            published_at=value.get("published_at"),
        )


def try_publish(
    path_or_result: Any,
    result_or_path: Any = None,
    **kwargs: Any,
) -> Publication:
    """Like :func:`publish`, but returns stale/conflicting outcomes.

    Successful publications return a ``status == "published"`` result.  A
    stale base returns ``status == "stale_base"``; a reused request returns
    the existing published result.  Encoding errors are still raised because
    they mean no publication was attempted.
    """

    try:
        return publish(path_or_result, result_or_path, **kwargs)
    except StaleBaseError as error:
        return error.result


@dataclass
class Prepared:
    """An encoded but not-yet-visible publication.

    ``prepare()`` streams the source exactly once into a temporary file.  The
    file is not reachable through the published JSON path until ``commit()``.
    A writer may be paused for arbitrarily long between those two calls.

    When a request id is supplied, the encoded file is also referenced by a
    durable staging record in the sidecar directory.  A later attempt with the
    same request id (in this process or another one) can therefore commit the
    same bytes without consuming the one-shot source again.
    """

    path: Optional[Path]
    sha256: str
    size: int
    temporary: bool = True
    request_id: Optional[str] = None
    stage_path: Optional[Path] = None
    committed: Optional["Publication"] = None
    base: Optional["Credential"] = None

    def discard(self) -> None:
        if self.temporary:
            Publisher._discard(self.path)
            Publisher._discard(self.stage_path)
            self.stage_path = None

# ---------------------------------------------------------------------------
# Streaming JSON encoding with per-object duplicate-key detection


def _format_path(path: Tuple[Any, ...]) -> str:
    out = "$"
    for part in path:
        if isinstance(part, str):
            out += "[" + json.dumps(part, ensure_ascii=False) + "]"
        else:
            out += f"[{part}]"
    return out


def _json_key(value: Any) -> str:
    """Return the key text used by the standard library JSON encoder."""

    if isinstance(value, str):
        return value
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return json.dumps(value, allow_nan=True)
    raise TypeError(
        "keys must be str, int, float, bool or None, "
        f"not {type(value).__name__}"
    )


class _StreamingEncoder:
    def __init__(
        self,
        fp,
        *,
        ensure_ascii: bool = True,
        indent: Union[int, str, None] = None,
        separators: Optional[Tuple[str, str]] = None,
        default=None,
        allow_nan: bool = True,
    ) -> None:
        self.fp = fp
        self.ensure_ascii = ensure_ascii
        self.indent = indent
        self.default = default
        self.allow_nan = allow_nan
        self._active = set()

        if isinstance(indent, int):
            self._indent_unit = " " * indent
        elif isinstance(indent, str):
            self._indent_unit = indent
        elif indent is None:
            self._indent_unit = None
        else:
            raise TypeError("indent must be int, str or None")

        if separators is None:
            if self._indent_unit is None:
                self._item_separator, self._key_separator = ", ", ": "
            else:
                self._item_separator, self._key_separator = ",", ": "
        else:
            self._item_separator, self._key_separator = separators

    def encode(self, value: Any) -> None:
        self._encode(value, (), 0)

    def _write(self, text: str) -> None:
        self.fp.write(text)

    def _newline(self, level: int) -> None:
        if self._indent_unit is not None:
            self._write("\n" + self._indent_unit * level)

    def _encode(self, value: Any, path: Tuple[Any, ...], level: int) -> None:
        # StreamableList/StreamableDict must be checked before their builtin
        # bases: their storage is empty and their items come from a generator.
        if isinstance(value, StreamableDict):
            self._encode_object(value, path, level)
            return
        if isinstance(value, StreamableList):
            self._encode_array(value, path, level)
            return
        if isinstance(value, Streamable):
            # A future Streamable kind: use its iteration protocol and let it
            # behave like an array unless it advertises items().
            if hasattr(value, "items"):
                self._encode_object(value, path, level)
            else:
                self._encode_array(value, path, level)
            return
        if isinstance(value, Mapping):
            self._encode_object(value, path, level)
            return
        if isinstance(value, (list, tuple)):
            self._encode_array(value, path, level)
            return
        self._encode_scalar(value, path, level)

    def _encode_scalar(self, value: Any, path: Tuple[Any, ...], level: int) -> None:
        try:
            text = json.dumps(
                value,
                ensure_ascii=self.ensure_ascii,
                allow_nan=self.allow_nan,
            )
        except TypeError:
            if self.default is None:
                raise
            replacement = self.default(value)
            if replacement is value:
                raise
            # A default() result can itself be a streamable container.
            self._encode(replacement, path, level)
            return
        self._write(text)

    def _encode_object(self, value: Any, path: Tuple[Any, ...], level: int) -> None:
        marker = id(value)
        if marker in self._active:
            raise ValueError("Circular reference detected")
        self._active.add(marker)
        try:
            first = True
            seen: Dict[str, Any] = {}
            for key, item in value.items():
                json_key = _json_key(key)
                if json_key in seen:
                    raise DuplicateKeyError(path, json_key, (seen[json_key], key))
                seen[json_key] = key

                if first:
                    self._write("{")
                else:
                    self._write(self._item_separator)
                if self._indent_unit is not None:
                    self._newline(level + 1)
                first = False
                self._write(json.dumps(json_key, ensure_ascii=self.ensure_ascii))
                self._write(self._key_separator)
                self._encode(item, path + (json_key,), level + 1)

            if first:
                self._write("{}")
            else:
                if self._indent_unit is not None:
                    self._newline(level)
                self._write("}")
        finally:
            self._active.discard(marker)

    def _encode_array(self, value: Any, path: Tuple[Any, ...], level: int) -> None:
        marker = id(value)
        if marker in self._active:
            raise ValueError("Circular reference detected")
        self._active.add(marker)
        try:
            first = True
            for index, item in enumerate(value):
                if first:
                    self._write("[")
                else:
                    self._write(self._item_separator)
                if self._indent_unit is not None:
                    self._newline(level + 1)
                first = False
                self._encode(item, path + (index,), level + 1)
            if first:
                self._write("[]")
            else:
                if self._indent_unit is not None:
                    self._newline(level)
                self._write("]")
        finally:
            self._active.discard(marker)


def encode_streamable(value: Any, fp, **kwargs: Any) -> None:
    """Encode ``value`` as standard JSON without materialising its containers.

    This is the one-pass encoder used by :class:`Publisher`; it is public so
    callers can reuse the same duplicate-key rules without publishing.
    """

    _StreamingEncoder(fp, **kwargs).encode(_coerce_result(value))


def _coerce_result(value: Any):
    """Treat a bare iterable as a streamable list and key/value pairs as a dict.

    The canonical API passes :func:`~json_stream.streamable_list` /
    :func:`~json_stream.streamable_dict` results.  Accepting an iterator of
    pairs as a dict keeps the convenience functions pleasant without changing
    the writer module's semantics.
    """

    if isinstance(value, (Streamable, Mapping, list, tuple, str, bytes)):
        return value
    if isinstance(value, (int, float, bool, type(None))):
        return value
    try:
        iterator = iter(value)
    except TypeError:
        return value
    try:
        first = next(iterator)
    except StopIteration:
        return StreamableList(())
    if isinstance(first, tuple) and len(first) == 2:
        return StreamableDict(_chain_first(first, iterator))
    return StreamableList(_chain_first(first, iterator))


def _chain_first(first: Any, iterator: Iterable[Any]):
    yield first
    yield from iterator


# ---------------------------------------------------------------------------
# Durable file helpers


def _fsync_directory(path: Path) -> None:
    try:
        fd = os.open(str(path), os.O_RDONLY)
    except OSError:
        return
    try:
        try:
            os.fsync(fd)
        except OSError:
            pass
    finally:
        os.close(fd)


def _default_file_mode() -> int:
    """Mode a normally-created file would get, honouring the process umask."""

    if os.name == "nt":  # pragma: no cover - Windows
        return 0o666
    mask = os.umask(0)
    os.umask(mask)
    return 0o666 & ~mask


_DEFAULT_FILE_MODE = _default_file_mode()


def _readable_mode(target: Optional[Path]) -> int:
    """Keep an existing target's permissions, otherwise use the umask default."""

    if target is not None:
        try:
            return os.stat(str(target)).st_mode & 0o777
        except OSError:
            pass
    return _DEFAULT_FILE_MODE


def _atomic_write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(
        prefix="." + path.name + ".tmp-", dir=str(path.parent)
    )
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
        _fsync_directory(path.parent)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


def _load_json_file(path: Path) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _file_hash(path: Path) -> Tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            size += len(block)
            digest.update(block)
    return digest.hexdigest(), size


def _stat_identity(path: Path) -> Optional[Tuple[int, int]]:
    try:
        stat = os.stat(str(path))
    except FileNotFoundError:
        return None
    return stat.st_dev, stat.st_ino


def _credential_text(value: Optional[Credential]) -> str:
    if value is None:
        return "<none>"
    if value.token is None:
        return f"version {value.version}"
    return f"version {value.version} ({value.token})"


def _coerce_credential(value: Any) -> Optional[Credential]:
    if value is None:
        return None
    if isinstance(value, Credential):
        return value
    if isinstance(value, Publication):
        return value.credential
    if isinstance(value, Mapping):
        return Credential.from_dict(value)
    if isinstance(value, int) and not isinstance(value, bool):
        return Credential(value)
    if isinstance(value, (tuple, list)) and len(value) == 2:
        return Credential(int(value[0]), value[1])
    if isinstance(value, str):
        # A token-only credential is useful when the scheduler stored the
        # opaque credential returned by the store.  Version -1 matches any
        # version.
        return Credential(-1, value)
    raise TypeError(f"Cannot interpret {value!r} as a publication credential")


def _stage_credential(stage: Mapping[str, Any]) -> Optional[Credential]:
    version = stage.get("base_version")
    if version is None:
        return None
    return Credential(int(version), stage.get("base_token"))


def _same_credential(
    expected: Optional[Credential],
    version: int,
    token: Optional[str],
) -> bool:
    if expected is None:
        return True
    if expected.version >= 0 and expected.version != version:
        return False
    if expected.token is not None and expected.token != token:
        return False
    return True


# ---------------------------------------------------------------------------
# The publisher


_PUBLISH_FAILPOINT_ENV = "JSON_STREAM_PUBLISH_FAILPOINT"


def _failpoint(name: str) -> None:
    """Testing hook: exit the process at a precise crash-recovery window."""

    if os.environ.get(_PUBLISH_FAILPOINT_ENV) == name:
        os._exit(99)


_THREAD_LOCKS: Dict[str, Any] = {}
_THREAD_LOCKS_GUARD = threading.Lock()


def _thread_lock_for(key: str):
    with _THREAD_LOCKS_GUARD:
        lock = _THREAD_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _THREAD_LOCKS[key] = lock
        return lock


@contextmanager
def _file_lock(lock_path: Path):
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(fd, fcntl.LOCK_EX)
        elif msvcrt is not None:  # pragma: no cover - Windows
            msvcrt.locking(fd, msvcrt.LK_LOCK, 1)
        yield
    finally:
        try:
            if fcntl is not None:
                fcntl.flock(fd, fcntl.LOCK_UN)
            elif msvcrt is not None:  # pragma: no cover - Windows
                os.lseek(fd, 0, os.SEEK_SET)
                msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        finally:
            os.close(fd)


class Publisher:
    """Atomic compare-and-swap publisher for one JSON path.

    Parameters are mostly for embedding and testing.  Normally only ``path``
    is supplied; bookkeeping goes into ``<path>.jsonstream/``.
    """

    def __init__(
        self,
        path: Union[str, os.PathLike],
        *,
        state_path: Union[str, os.PathLike, None] = None,
        lock_path: Union[str, os.PathLike, None] = None,
        pending_dir: Union[str, os.PathLike, None] = None,
        encode_options: Optional[Mapping[str, Any]] = None,
    ) -> None:
        self.path = Path(path)
        if self.path.exists() and self.path.is_dir():
            raise IsADirectoryError(str(self.path))

        sidecar = self.path.with_name(self.path.name + ".jsonstream")
        self.sidecar_dir = sidecar
        self.state_path = Path(state_path) if state_path is not None else sidecar / "state.json"
        self.lock_path = Path(lock_path) if lock_path is not None else sidecar / "lock"
        self.pending_dir = (
            Path(pending_dir) if pending_dir is not None else sidecar / "pending"
        )
        self.encode_options = dict(encode_options or {})
        self._thread_key = str(self.state_path.absolute())
        self._thread_lock = _thread_lock_for(self._thread_key)

    # -- public API ---------------------------------------------------------

    def prepare(
        self,
        result: Any,
        request_id: Any = None,
        **encode_options: Any,
    ) -> Prepared:
        """Encode ``result`` once into a hidden temporary file.

        No lock is held while the source is consumed and the published path is
        not touched, so a slow or paused producer does not block other
        publishers.  The returned :class:`Prepared` can be committed later by
        any process that can see the temporary file.

        With a ``request_id``, an encoded file is reused if one was already
        staged for that request, so a retry never replays the source.
        """

        request_key = self._normalise_request_id(request_id)
        options = dict(self.encode_options)
        options.update(encode_options)

        # Capture the credential this encoding is based on before the source
        # starts producing.  A writer that is paused mid-encode and commits
        # after a newer publication is then rejected even if it did not pass
        # a credential explicitly.
        base = self._capture_base()

        if request_key is not None:
            # Recovery first: a request that a dead publisher already landed
            # must not cause the one-shot source to be consumed again.
            committed = self.query(request_key)
            if committed is not None:
                return Prepared(
                    path=None,
                    sha256=committed.sha256 or "",
                    size=committed.size or 0,
                    temporary=False,
                    request_id=request_key,
                    committed=committed,
                )
            existing = self._existing_stage(request_key)
            if existing is not None:
                if existing.base is None:
                    existing.base = base
                return existing

        temp_path = self._encode_to_temp(_coerce_result(result), options)
        sha256, size = _file_hash(temp_path)
        stage_path = None
        if request_key is not None:
            try:
                stage_path = self._write_stage(
                    request_key, temp_path, sha256, size, base
                )
            except BaseException:
                self._discard(temp_path)
                raise
        return Prepared(
            path=temp_path,
            sha256=sha256,
            size=size,
            request_id=request_key,
            stage_path=stage_path,
            base=base,
        )

    def _capture_base(self) -> Optional["Credential"]:
        """Snapshot the current credential without holding a lock over encoding."""

        try:
            return self.credential()
        except OSError:  # pragma: no cover - sidecar unavailable
            return None

    # -- staging -----------------------------------------------------------

    def _stage_dir(self) -> Path:
        return self.pending_dir.parent / "staging"

    def _stage_path(self, request_key: str) -> Path:
        digest = hashlib.sha256(request_key.encode("utf-8")).hexdigest()
        return self._stage_dir() / (digest + ".json")

    def _write_stage(
        self,
        request_key: str,
        temp_path: Path,
        sha256: str,
        size: int,
        base: Optional["Credential"] = None,
    ) -> Path:
        stage_path = self._stage_path(request_key)
        stage = {
            "schema": 1,
            "request_id": request_key,
            "temp_path": str(temp_path.absolute()),
            "sha256": sha256,
            "size": size,
            "base_version": None if base is None else base.version,
            "base_token": None if base is None else base.token,
            "created_at": time.time(),
        }
        _atomic_write_json(stage_path, stage)
        _fsync_directory(stage_path.parent)
        return stage_path

    def _existing_stage(self, request_key: str) -> Optional[Prepared]:
        """Return a still-usable staged encoding for ``request_key``."""

        stage_path = self._stage_path(request_key)
        if not stage_path.exists():
            return None
        try:
            stage = _load_json_file(stage_path)
        except (OSError, ValueError):
            self._discard(stage_path)
            return None
        if not isinstance(stage, dict) or stage.get("request_id") != request_key:
            self._discard(stage_path)
            return None
        temp_path = Path(stage.get("temp_path", ""))
        if not temp_path.exists():
            self._discard(stage_path)
            return None
        try:
            sha256, size = _file_hash(temp_path)
        except OSError:
            self._discard(stage_path)
            return None
        if sha256 != stage.get("sha256") or size != stage.get("size"):
            self._discard(stage_path)
            self._discard(temp_path)
            return None
        return Prepared(
            path=temp_path,
            sha256=sha256,
            size=size,
            request_id=request_key,
            stage_path=stage_path,
            base=_stage_credential(stage),
        )

    def commit(
        self,
        prepared: Prepared,
        request_id: Any = None,
        *,
        base: Any = None,
        expected: Any = None,
        credential: Any = None,
        if_match: Any = None,
    ) -> Publication:
        """Atomically publish an already-encoded :class:`Prepared` result.

        ``base`` is the credential the writer read before it began encoding.
        If the published version or token has changed since then, the commit
        raises :class:`StaleBaseError`; the published file and credential are
        left exactly as they were.
        """

        base_value = self._one_base(base, expected, credential, if_match)
        expected_base = _coerce_credential(base_value)
        if expected_base is None:
            # No explicit credential: use the version this encoding started
            # from, captured by prepare().  This is what detects a slow writer
            # that was overrun while it was still producing data.
            expected_base = prepared.base
        request_key = self._normalise_request_id(request_id)

        if prepared.committed is not None:
            committed = prepared.committed
            if request_key is None or request_key == committed.request_id:
                return committed
        if request_key is None:
            request_key = prepared.request_id

        temp_path: Optional[Path] = prepared.path
        try:
            with self._locked() as state:
                # Recovery may have finalised this exact request while the
                # caller was encoding (e.g. two processes racing on a retry).
                if request_key is not None:
                    record = state["requests"].get(request_key)
                    if record is not None:
                        existing = Publication.from_dict(
                            record, path=str(self.path)
                        )
                        if (
                            existing.sha256 is not None
                            and existing.sha256 != prepared.sha256
                        ):
                            # A different payload owns this request id, so
                            # neither the staged bytes nor the stage record
                            # can ever be committed.
                            temp_path = None
                            Publisher._discard(prepared.path)
                            Publisher._discard(prepared.stage_path)
                            prepared.temporary = False
                            prepared.stage_path = None
                            raise RequestConflictError(request_key, existing)
                        # The request was already committed: neither the
                        # staged bytes nor their staging record are needed.
                        temp_path = None
                        Publisher._discard(prepared.path)
                        self._clear_staged(prepared)
                        return existing

                if not _same_credential(
                    expected_base, state["version"], state.get("token")
                ):
                    actual = self._credential_from_state(state)
                    # The encoded file stays alive: the caller may retry
                    # commit() against a fresh credential without consuming
                    # its one-shot source again.
                    temp_path = None
                    raise StaleBaseError(
                        expected_base,
                        actual,
                        path=self.path,
                        request_id=request_key,
                    )

                sha256, size = prepared.sha256, prepared.size
                version = state["version"] + 1
                token = secrets.token_hex(16)

                target_identity = _stat_identity(temp_path)
                if target_identity is None:  # pragma: no cover - just created
                    raise PublicationError(
                        "temporary file disappeared before commit"
                    )
                old_identity = _stat_identity(self.path)
                target_mode = _readable_mode(self.path)
                pending_name = secrets.token_hex(12) + ".json"
                pending_path = self.pending_dir / pending_name
                pending = {
                    "schema": 1,
                    "request_id": request_key,
                    "version": version,
                    "token": token,
                    "sha256": sha256,
                    "size": size,
                    "temp_path": str(temp_path.absolute()),
                    "target_dev": target_identity[0],
                    "target_ino": target_identity[1],
                    "old_dev": None if old_identity is None else old_identity[0],
                    "old_ino": None if old_identity is None else old_identity[1],
                    "base_version": None
                    if expected_base is None
                    else expected_base.version,
                    "base_token": None
                    if expected_base is None
                    else expected_base.token,
                    "created_at": time.time(),
                }

                # The intent record is durable before the rename.  Recovery
                # can therefore distinguish "the rename happened" from
                # "same bytes happen to be at the target" by inode.
                _atomic_write_json(pending_path, pending)
                _fsync_directory(self.pending_dir)

                os.replace(str(temp_path), str(self.path))
                temp_path = None
                prepared.temporary = False
                try:
                    os.chmod(str(self.path), target_mode)
                except OSError:  # pragma: no cover - best effort
                    pass
                _fsync_directory(self.path.parent)
                _failpoint("after_replace")

                record = {
                    "path": str(self.path),
                    "version": version,
                    "token": token,
                    "request_id": request_key,
                    "sha256": sha256,
                    "size": size,
                    "status": "published",
                    "published_at": time.time(),
                }
                self._record_publication(state, record)
                self._write_state(state)
                _failpoint("after_state_write")

                try:
                    pending_path.unlink()
                except FileNotFoundError:
                    pass
                _fsync_directory(self.pending_dir)

                # The commit side-effect is durable; the staging record has
                # done its job and must not cause a second publication.
                self._clear_staged(prepared)

                return Publication.from_dict(record, path=str(self.path))
        finally:
            if temp_path is not None:
                self._discard(temp_path)

    @staticmethod
    def _clear_staged(prepared: Prepared) -> None:
        prepared.temporary = False
        if prepared.stage_path is not None:
            Publisher._discard(prepared.stage_path)
            prepared.stage_path = None

    def _orphan_stage(self) -> None:
        """Remove staging records whose encoded file has disappeared."""

        directory = self._stage_dir()
        if not directory.exists():
            return
        for stage_path in directory.glob("*.json"):
            try:
                stage = _load_json_file(stage_path)
                temp_path = Path(stage.get("temp_path", ""))
            except (OSError, ValueError, AttributeError):
                self._discard(stage_path)
                continue
            if not temp_path.exists():
                self._discard(stage_path)

    def publish(
        self,
        result: Any,
        request_id: Any = None,
        *,
        base: Any = None,
        expected: Any = None,
        credential: Any = None,
        if_match: Any = None,
        **encode_options: Any,
    ) -> Publication:
        """Encode ``result`` once and atomically publish it.

        ``base`` (also accepted as ``expected``, ``credential`` or
        ``if_match``) is the credential read before encoding began.  A
        mismatch raises :class:`StaleBaseError` and leaves the published file
        and its credential untouched.

        If ``request_id`` was already committed, its existing receipt is
        returned without consuming ``result``.  This makes retries and
        post-crash queries idempotent.
        """

        base_value = self._one_base(base, expected, credential, if_match)
        request_key = self._normalise_request_id(request_id)

        # Fast idempotency check.  This deliberately happens before the
        # source is touched, so retrying a committed request never replays a
        # one-shot generator.
        if request_key is not None:
            existing = self.query(request_key)
            if existing is not None:
                return existing

        prepared = self.prepare(result, request_key, **encode_options)
        try:
            return self.commit(
                prepared,
                request_key,
                base=base_value,
            )
        finally:
            # With a request id, a stale rejection keeps the encoded file and
            # its staging record, so retrying the same request id never
            # replays the one-shot source.  Without one there is no handle to
            # reuse, so clean up.
            if prepared.temporary and request_key is None:
                prepared.discard()

    publish_file = publish

    def query(self, request_id: Any) -> Optional[Publication]:
        """Return the committed result for ``request_id``, if any.

        Querying performs recovery first, so it also completes a publication
        whose process exited after the rename but before reporting success.
        """

        request_key = self._normalise_request_id(request_id)
        if request_key is None:
            raise ValueError("request_id is required")
        with self._locked() as state:
            record = state["requests"].get(request_key)
            if record is None:
                return None
            return Publication.from_dict(record, path=str(self.path))

    status = query
    get = query

    def current(self) -> Optional[Publication]:
        """Return the currently published receipt, or ``None`` if never published."""

        with self._locked() as state:
            publications = state["publications"]
            if not publications:
                return None
            record = publications[-1]
            return Publication.from_dict(record, path=str(self.path))

    current_publication = current

    def credential(self) -> Optional[Credential]:
        """Return the credential of the current publication.

        If ``path`` already contained a document before this store was used,
        the first access bootstraps a baseline credential (version ``0``) from
        it.  That lets two writers that both start from such a pre-existing
        version detect each other's commits.
        """

        with self._locked() as state:
            if not state["publications"]:
                return None
            return self._credential_from_state(state)

    def read_credential(self) -> Optional[Credential]:
        return self.credential()

    def read(self, **kwargs: Any) -> Any:
        """Read the currently published standard JSON document."""

        with open(self.path, "r", encoding="utf-8") as handle:
            return json.load(handle, **kwargs)

    load = read

    def recover(self) -> None:
        """Finish any pending commit left behind by a dead publisher."""

        with self._locked():
            pass

    # -- internals ----------------------------------------------------------

    @staticmethod
    def _one_base(*values: Any) -> Any:
        present = [value for value in values if value is not None]
        if not present:
            return None
        first = present[0]
        for value in present[1:]:
            if value != first:
                raise TypeError("provide only one of base/expected/credential/if_match")
        return first

    @staticmethod
    def _normalise_request_id(request_id: Any) -> Optional[str]:
        if request_id is None:
            return None
        if isinstance(request_id, str):
            return request_id
        if isinstance(request_id, bytes):
            return request_id.decode("utf-8")
        return str(request_id)

    def _ensure_sidecar(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        self.pending_dir.mkdir(parents=True, exist_ok=True)

    def _encode_to_temp(
        self, result: Any, options: Mapping[str, Any]
    ) -> Path:
        self._ensure_sidecar()
        fd, name = tempfile.mkstemp(
            prefix="." + self.path.name + ".json-tmp-",
            dir=str(self.path.parent),
        )
        temp_path = Path(name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                try:
                    encode_streamable(result, handle, **options)
                except BaseException:
                    close = getattr(result, "close", None)
                    if close is not None:
                        try:
                            close()
                        except Exception:
                            pass
                    raise
                handle.flush()
                os.fsync(handle.fileno())
            return temp_path
        except BaseException:
            self._discard(temp_path)
            raise

    @staticmethod
    def _discard(path: Optional[Path]) -> None:
        if path is None:
            return
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError:  # pragma: no cover - best effort cleanup
            pass

    @contextmanager
    def _locked(self):
        self._ensure_sidecar()
        with self._thread_lock:
            with _file_lock(self.lock_path):
                self._recover_locked()
                self._orphan_stage()
                self._baseline_locked()
                yield self._load_state()

    def _baseline_locked(self) -> None:
        """Adopt a pre-existing document as version 0 if the store is empty.

        Only the credential (identity), never a copy of the content, is
        recorded.  The receiver keeps reading the original path.
        """

        if self.state_path.exists():
            return
        if not self.path.exists():
            return
        try:
            sha256, size = _file_hash(self.path)
        except OSError:
            return
        publication = {
            "path": str(self.path),
            "version": 0,
            "token": secrets.token_hex(16),
            "request_id": None,
            "sha256": sha256,
            "size": size,
            "status": "published",
            "published_at": None,
            "baseline": True,
        }
        state = {
            "schema": 1,
            "version": 0,
            "token": publication["token"],
            "publications": [publication],
            "requests": {},
        }
        self._write_state(state)

    def _load_state(self) -> Dict[str, Any]:
        if not self.state_path.exists():
            return {
                "schema": 1,
                "version": 0,
                "token": None,
                "publications": [],
                "requests": {},
            }
        try:
            state = _load_json_file(self.state_path)
        except (OSError, ValueError) as error:
            raise PublicationError(
                f"cannot read publication state {self.state_path}: {error}"
            ) from error
        if not isinstance(state, dict):
            raise PublicationError(f"invalid publication state {self.state_path}")
        state.setdefault("schema", 1)
        state.setdefault("version", 0)
        state.setdefault("token", None)
        state.setdefault("publications", [])
        state.setdefault("requests", {})
        return state

    def _write_state(self, state: Mapping[str, Any]) -> None:
        _atomic_write_json(self.state_path, state)

    @staticmethod
    def _credential_from_state(state: Mapping[str, Any]) -> Credential:
        return Credential(int(state["version"]), state.get("token"))

    @staticmethod
    def _record_publication(
        state: Dict[str, Any], record: Mapping[str, Any]
    ) -> None:
        version = int(record["version"])
        token = record.get("token")
        publications = state["publications"]

        replaced = False
        for index, existing in enumerate(publications):
            if int(existing.get("version", -1)) == version:
                publications[index] = dict(record)
                replaced = True
                break
        if not replaced:
            publications.append(dict(record))
        publications.sort(key=lambda item: int(item.get("version", 0)))

        if version >= int(state["version"]):
            state["version"] = version
            state["token"] = token
        request_id = record.get("request_id")
        if request_id is not None:
            state["requests"][str(request_id)] = dict(record)

    def _is_staged(self, request_key: Optional[str], temp_path: Path) -> bool:
        if request_key is None:
            return False
        stage_path = self._stage_path(request_key)
        if not stage_path.exists():
            return False
        try:
            stage = _load_json_file(stage_path)
        except (OSError, ValueError):
            return False
        return isinstance(stage, dict) and stage.get("temp_path") == str(
            temp_path.absolute()
        )

    def _recover_locked(self) -> None:
        if not self.pending_dir.exists():
            return
        pending_files = sorted(
            self.pending_dir.glob("*.json"),
            key=lambda item: (item.stat().st_mtime, item.name),
        )
        if not pending_files:
            return

        state = self._load_state()
        changed = False
        completed = []

        for pending_path in pending_files:
            try:
                pending = _load_json_file(pending_path)
            except (OSError, ValueError):
                # An interrupted atomic write cannot normally produce this,
                # but never let one bad journal entry block publication.
                self._discard(pending_path)
                continue
            if not isinstance(pending, dict) or "token" not in pending:
                self._discard(pending_path)
                continue

            request_id = pending.get("request_id")
            request_record = None
            if request_id is not None:
                request_record = state["requests"].get(str(request_id))

            # Already reflected in committed state.
            if state.get("token") == pending["token"] or (
                request_record is not None
                and request_record.get("token") == pending["token"]
            ):
                self._discard(pending_path)
                continue

            identity = _stat_identity(self.path)
            was_renamed = identity is not None and identity == (
                pending.get("target_dev"),
                pending.get("target_ino"),
            )

            if was_renamed:
                expected_hash = pending.get("sha256")
                if expected_hash is not None:
                    try:
                        actual_hash, _ = _file_hash(self.path)
                    except OSError:
                        actual_hash = None
                    if actual_hash is not None and actual_hash != expected_hash:
                        # The inode was reused/replaced after our rename.  A
                        # later publisher must have completed recovery first,
                        # so this entry is stale.
                        self._discard(pending_path)
                        self._discard(Path(pending.get("temp_path", "")))
                        continue

                record = {
                    "path": str(self.path),
                    "version": int(pending["version"]),
                    "token": pending["token"],
                    "request_id": request_id,
                    "sha256": pending.get("sha256"),
                    "size": pending.get("size"),
                    "status": "published",
                    "published_at": pending.get("created_at", time.time()),
                }
                self._record_publication(state, record)
                changed = True
                completed.append(pending_path)
            else:
                # The rename never happened.  Drop the intent record.  If the
                # encoded file is still referenced by a staging record for
                # this request, keep it so a retry can publish without
                # replaying the source; otherwise it is unreachable garbage.
                self._discard(pending_path)
                temp_path = pending.get("temp_path")
                if temp_path and not self._is_staged(request_id, Path(temp_path)):
                    self._discard(Path(temp_path))

        # Durability order matters: persist the recovered receipts first, then
        # remove their journals.  A crash in between is harmless because the
        # token comparison above treats already-recorded entries as done.
        if changed:
            self._write_state(state)
            _failpoint("after_recover")
        for pending_path in completed:
            try:
                pending_path.unlink()
            except FileNotFoundError:
                pass
        if completed:
            _fsync_directory(self.pending_dir)


# A descriptive alias for callers that model this as a store.
PublicationStore = Publisher


# ---------------------------------------------------------------------------
# Convenience functions.  They are deliberately permissive about argument
# order so a path can be passed first or after the one-shot result.


def _looks_like_path(value: Any) -> bool:
    return isinstance(value, (str, bytes, os.PathLike))


def _split_path_and_result(
    path_or_result: Any,
    result_or_path: Any,
    *,
    path: Optional[Union[str, os.PathLike]] = None,
) -> Tuple[Union[str, os.PathLike], Any]:
    if path is not None:
        return path, path_or_result if result_or_path is None else result_or_path
    if result_or_path is None:
        raise TypeError("both a path and a result are required")
    # The canonical order is (path, result).  Only fall back to the reversed
    # order when the first argument cannot be a path.
    if _looks_like_path(path_or_result):
        return path_or_result, result_or_path
    if _looks_like_path(result_or_path):
        return result_or_path, path_or_result
    raise TypeError("cannot determine which argument is the publication path")


def publish(
    path_or_result: Any,
    result_or_path: Any = None,
    *,
    path: Optional[Union[str, os.PathLike]] = None,
    request_id: Any = None,
    base: Any = None,
    expected: Any = None,
    credential: Any = None,
    if_match: Any = None,
    state_path: Optional[Union[str, os.PathLike]] = None,
    **encode_options: Any,
) -> Publication:
    """Publish one streaming result atomically.  See :class:`Publisher`."""

    target, result = _split_path_and_result(
        path_or_result, result_or_path, path=path
    )
    publisher = Publisher(target, state_path=state_path)
    return publisher.publish(
        result,
        request_id,
        base=base,
        expected=expected,
        credential=credential,
        if_match=if_match,
        **encode_options,
    )


publish_file = publish


def query(
    path_or_request: Any,
    request_or_path: Any = None,
    *,
    path: Optional[Union[str, os.PathLike]] = None,
    request_id: Any = None,
    state_path: Optional[Union[str, os.PathLike]] = None,
) -> Optional[Publication]:
    """Query a request id, completing a crashed commit if necessary."""

    if request_id is None:
        if request_or_path is None:
            # A single non-path argument is an unambiguous request id; the
            # caller still has to supply the path through ``path=``.
            if path is None:
                raise TypeError("both a path and a request_id are required")
            return Publisher(path, state_path=state_path).query(path_or_request)
        # Canonical order is (path, request_id).  Only swap when the first
        # argument is clearly the request id and the second is a path.
        if _looks_like_path(path_or_request) or not _looks_like_path(
            request_or_path
        ):
            target, request_key = path_or_request, request_or_path
        else:
            target, request_key = request_or_path, path_or_request
    else:
        target, request_key = (path_or_request if path is None else path), request_id
    return Publisher(target, state_path=state_path).query(request_key)


def query_publication(
    path_or_request: Any,
    request_or_path: Any = None,
    **kwargs: Any,
) -> Optional[Publication]:
    return query(path_or_request, request_or_path, **kwargs)


publish_status = query_publication
read_publication = query_publication
get_publication = query_publication


def current_publication(
    path: Union[str, os.PathLike],
    *,
    state_path: Optional[Union[str, os.PathLike]] = None,
) -> Optional[Publication]:
    return Publisher(path, state_path=state_path).current()


def current_credential(
    path: Union[str, os.PathLike],
    *,
    state_path: Optional[Union[str, os.PathLike]] = None,
) -> Optional[Credential]:
    return Publisher(path, state_path=state_path).credential()


def read_credential(
    path: Union[str, os.PathLike],
    *,
    state_path: Optional[Union[str, os.PathLike]] = None,
) -> Optional[Credential]:
    return current_credential(path, state_path=state_path)


def read(
    path: Union[str, os.PathLike],
    *,
    state_path: Optional[Union[str, os.PathLike]] = None,
    **kwargs: Any,
) -> Any:
    """Read the standard JSON document at ``path``."""

    return Publisher(path, state_path=state_path).read(**kwargs)


# Make the module itself callable as ``json_stream.publish(...)`` while still
# supporting ``from json_stream.publish import publish``.
class _CallableModule(types.ModuleType):
    def __call__(self, *args: Any, **kwargs: Any) -> Publication:
        return publish(*args, **kwargs)


import sys as _sys

_sys.modules[__name__].__class__ = _CallableModule
