# narwhals-canary: delete this line to confirm you have read the full diff
"""Versioned Parquet archives with pinned reads and atomic publication.

Narwhals' Parquet IO treats a path as a file. An archive instead treats the same
path as a sequence of published versions: a writer prepares the next version from
a pinned view of the current one, and only a completed, verified staging file
replaces the published path.

The intended flow is:

1. Open an [`Archive`][narwhals.archive.Archive] on a published path. If the path
   does not exist yet, the archive starts empty.
2. Call [`Archive.prepare`][narwhals.archive.Archive.prepare] with the target
   version name. The returned [`Prepared`][narwhals.archive.Prepared] plan pins
   the basis the new version will be deduplicated against.
3. Derive the new rows from [`Prepared.basis`][narwhals.archive.Prepared.basis],
   a [`LazyFrame`][narwhals.LazyFrame] pinned to the plan's basis version.
4. Call [`Prepared.publish`][narwhals.archive.Prepared.publish] with the lazy
   pipeline. The pipeline is written with the backend's native sink, the written
   footer is checked against the plan, and only a matching file atomically
   replaces the published path. A stale basis is refused before the pipeline runs.

Readers opened through [`Archive.reader`][narwhals.archive.Archive.reader] keep the
file descriptor they started with. A later publication replaces the path's
directory entry, not that descriptor, so an in-flight reader keeps reading the
version it saw first. The returned [`Receipt`][narwhals.archive.Receipt] says
whether a call read a version, was refused because its basis went stale, failed
while staging, or published a new version.
"""

from __future__ import annotations

import io
import os
import uuid
from collections.abc import Callable
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, Final, Literal, cast

from narwhals._utils import Implementation
from narwhals.dataframe import LazyFrame
from narwhals.exceptions import (
    ArchiveBasisMismatchError,
    ArchiveError,
    ArchivePublishError,
    ArchiveStagingError,
)
from narwhals.schema import Schema
from narwhals.translate import from_native

if TYPE_CHECKING:  # pragma: no cover
    from collections.abc import Iterator, Mapping

    from typing_extensions import Self

    from narwhals.typing import FileSource

__all__ = [
    "Archive",
    "ArchiveStatus",
    "NativeSink",
    "Prepared",
    "Reader",
    "Receipt",
    "VersionInfo",
    "open_archive",
    "prepare_archive",
    "read_archive",
]

_GENESIS: Final = "0" * 32
_VERSION_KEY: Final = "narwhals.archive.version"
_TOKEN_KEY: Final = "narwhals.archive.token"  # noqa: S105
_BASIS_KEY: Final = "narwhals.archive.basis"
_ARCHIVE_KEYS: Final = frozenset({_VERSION_KEY, _TOKEN_KEY, _BASIS_KEY})

ArchiveStatus = Literal[
    "read",
    "published",
    "stale",
    "staging-failed",
    "verification-failed",
    "publish-failed",
]
"""Outcome kinds reported by [`Receipt`][narwhals.archive.Receipt].

- `"read"`: an existing published version was opened for reading.
- `"published"`: a new version was verified and moved onto the published path.
- `"stale"`: the plan's basis is no longer published; the source pipeline was not run.
- `"staging-failed"`: the native sink failed while writing the staging file.
- `"verification-failed"`: the staging file's footer did not match the plan.
- `"publish-failed"`: the verified staging file could not replace the published path.
"""

NativeSink = Callable[[Any, Path, "Mapping[str, str]", "Mapping[str, Any]"], None]
"""Callable writing a lazy frame with a native sink.

Arguments are the frame to write, the staging path, the archive footer metadata,
and backend sink options. The default sink is the Polars native lazy sink.
"""


def _random_token() -> str:
    return uuid.uuid4().hex


def _coerce_path(path: FileSource, /) -> Path:
    if not isinstance(path, (str, os.PathLike)):
        msg = (
            "An archive path must be a filesystem path, not a file-like object.\n\n"
            "Hint: publication replaces one path, so pass e.g. 'archive.parquet'."
        )
        raise TypeError(msg)
    return Path(path)


def _check_version_name(version: str, /) -> None:
    if not isinstance(version, str) or not version:
        msg = "An archive version name must be a non-empty string."
        raise ValueError(msg)


class _PinnedReader(io.RawIOBase):
    """Read-only view of an open file descriptor at an independent position.

    The descriptor was opened before any later publication replaced the path, so it
    keeps referring to the version the reader started with. `pread` gives each
    reader its own position without coordinating a shared file offset.
    """

    def __init__(self, fd: int, size: int, /) -> None:
        super().__init__()
        self._fd = fd
        self._size = size
        self._pos = 0

    @property
    def closed(self) -> bool:
        return self._fd == -1

    def readable(self) -> bool:
        return True

    def seekable(self) -> bool:
        return True

    def writable(self) -> bool:
        return False

    def fileno(self) -> int:
        if self._fd == -1:  # pragma: no cover
            msg = "I/O operation on closed file"
            raise ValueError(msg)
        return self._fd

    def tell(self) -> int:
        return self._pos

    def seek(self, offset: int, whence: int = os.SEEK_SET, /) -> int:
        if whence == os.SEEK_SET:
            pos = offset
        elif whence == os.SEEK_CUR:
            pos = self._pos + offset
        elif whence == os.SEEK_END:
            pos = self._size + offset
        else:  # pragma: no cover
            msg = f"Invalid whence: {whence}"
            raise ValueError(msg)
        if pos < 0:
            msg = f"Negative seek position: {pos}"
            raise ValueError(msg)
        self._pos = pos
        return pos

    def readinto(self, buffer: Any, /) -> int:
        data = os.pread(self._fd, len(buffer), self._pos)
        n = len(data)
        buffer[:n] = data
        self._pos += n
        return n

    def read(self, size: int | None = -1, /) -> bytes:
        if self._fd == -1:  # pragma: no cover
            msg = "I/O operation on closed file"
            raise ValueError(msg)
        if size is None or size < 0:
            size = max(0, self._size - self._pos)
        data = os.pread(self._fd, size, self._pos)
        self._pos += len(data)
        return data

    def close(self) -> None:
        fd = getattr(self, "_fd", -1)
        if fd != -1:
            self._fd = -1
            with suppress(OSError):  # pragma: no cover
                os.close(fd)
        super().close()  # pragma: no cover


def _read_footer_pyarrow(reader: _PinnedReader, /) -> _Footer | None:
    import pyarrow.parquet as pq  # ignore-banned-import

    reader.seek(0)
    metadata = pq.ParquetFile(cast("Any", reader)).metadata
    raw: Mapping[bytes, bytes] = metadata.metadata or {}
    decoded = {
        key.decode(): value.decode()
        for key, value in raw.items()
        if key.decode() in _ARCHIVE_KEYS
    }
    schema = Schema.from_arrow(metadata.schema.to_arrow_schema())
    return _Footer.from_metadata(decoded, schema)


def _read_footer_polars(reader: _PinnedReader, /) -> _Footer | None:
    import polars as pl  # ignore-banned-import

    reader.seek(0)
    raw = pl.read_parquet_metadata(cast("Any", reader))
    decoded = {key: raw[key] for key in _ARCHIVE_KEYS if key in raw}
    reader.seek(0)
    schema = Schema.from_native(pl.read_parquet_schema(cast("Any", reader)))
    return _Footer.from_metadata(decoded, schema)


def _read_footer(
    reader: _PinnedReader, implementation: Implementation, /
) -> _Footer | None:
    if implementation is Implementation.PYARROW:
        return _read_footer_pyarrow(reader)
    return _read_footer_polars(reader)


def _implementation(backend: str, /) -> Implementation:
    if backend == "polars":
        return Implementation.POLARS
    if backend == "pyarrow":
        return Implementation.PYARROW
    if backend == "pandas":
        return Implementation.PANDAS
    msg = (
        f"Unsupported archive backend: {backend!r}.\n\n"
        "Hint: use one of 'polars', 'pyarrow' or 'pandas'."
    )
    raise ValueError(msg)


def _footer_at(path: Path, backend: str, /) -> _Footer | None:
    try:
        fd = os.open(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    reader = _PinnedReader(fd, os.fstat(fd).st_size)
    try:
        return _read_footer(reader, _implementation(backend))
    finally:
        reader.close()


def _scan_fd(fd: int, size: int, backend: str, /) -> LazyFrame[Any]:
    """Build a lazy frame from a duplicated descriptor.

    The duplicate has its own position and shares the inode, so a scan keeps
    reading the version this descriptor was opened for even after the path is
    replaced. Nothing is collected here.
    """
    reader = _PinnedReader(os.dup(fd), size)
    if backend == "polars":
        import polars as pl  # ignore-banned-import

        return from_native(pl.scan_parquet(cast("Any", reader)))
    if backend in {"pyarrow", "pandas"}:
        import pyarrow.parquet as pq  # ignore-banned-import

        try:
            table = pq.read_table(reader)
        finally:
            reader.close()
        return cast("LazyFrame[Any]", from_native(table).lazy())
    msg = f"Unsupported read backend: {backend!r}"  # pragma: no cover
    raise ValueError(msg)  # pragma: no cover


class VersionInfo:
    """Identity of one published archive version.

    Arguments:
        name: Caller-provided version name, e.g. `"v1"`.
        token: Unique token written into the file's footer.
        basis: Token of the version this one was built from, or `None` for the
            first version of an archive.
        schema: Schema read from the published file's footer.
    """

    __slots__ = ("basis", "name", "schema", "token")

    def __init__(self, name: str, token: str, basis: str | None, schema: Schema) -> None:
        self.name = name
        self.token = token
        self.basis = basis
        self.schema = schema

    def __repr__(self) -> str:
        return (
            f"VersionInfo(name={self.name!r}, token={self.token!r}, basis={self.basis!r})"
        )

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, VersionInfo):
            return NotImplemented
        return (
            self.name == other.name
            and self.token == other.token
            and self.basis == other.basis
            and self.schema == other.schema
        )

    def __hash__(self) -> int:
        return hash((self.name, self.token, self.basis))


class _Footer:
    __slots__ = ("basis", "name", "schema", "token")

    def __init__(
        self, *, name: str, token: str, basis: str | None, schema: Schema
    ) -> None:
        self.name = name
        self.token = token
        self.basis = basis
        self.schema = schema

    @classmethod
    def from_metadata(
        cls, metadata: Mapping[str, str], schema: Schema, /
    ) -> _Footer | None:
        try:
            name = metadata[_VERSION_KEY]
            token = metadata[_TOKEN_KEY]
            basis = metadata[_BASIS_KEY]
        except KeyError:
            return None
        return cls(
            name=name,
            token=token,
            basis=None if basis == _GENESIS else basis,
            schema=schema,
        )

    @property
    def info(self) -> VersionInfo:
        return VersionInfo(self.name, self.token, self.basis, self.schema)


class Receipt:
    """Credential describing one archive operation.

    A receipt distinguishes reading a version, a stale-basis refusal, a staging
    failure, a footer-verification failure, and a completed publication.

    Arguments:
        status: One of the [`ArchiveStatus`][narwhals.archive.ArchiveStatus] values.
        version: Version read or published, when applicable.
        basis: Basis the operation was prepared against.
        path: Published path the archive refers to.
        error: The error behind a non-success status, when there was one.
    """

    __slots__ = ("basis", "error", "path", "status", "version")

    def __init__(
        self,
        *,
        status: ArchiveStatus,
        version: VersionInfo | None,
        basis: VersionInfo | None,
        path: Path,
        error: ArchiveError | None = None,
    ) -> None:
        self.status = status
        self.version = version
        self.basis = basis
        self.path = path
        self.error = error

    @property
    def published(self) -> bool:
        """Whether this receipt records a completed publication."""
        return self.status == "published"

    @property
    def read(self) -> bool:
        """Whether this receipt records a read of an existing version."""
        return self.status == "read"

    def raise_for_status(self) -> None:
        """Raise the receipt's error unless the operation read or published."""
        if self.error is not None:
            raise self.error

    def __repr__(self) -> str:
        version = None if self.version is None else self.version.name
        basis = None if self.basis is None else self.basis.name
        return (
            f"Receipt(status={self.status!r}, version={version!r}, "
            f"basis={basis!r}, path={str(self.path)!r})"
        )


class Reader:
    """A pinned, lazy view of one published archive version.

    The reader opens the published path once. Later publications replace the path's
    directory entry, while this reader's descriptor keeps referring to the version
    it opened. Each call to [`lazy`][narwhals.archive.Reader.lazy] builds a new scan
    from that descriptor, so reading a first row group and later a second one cannot
    mix in a newer publication.

    Arguments:
        archive: Archive this reader belongs to.
        fd: Descriptor opened on the published file.
        size: Size of that file when it was opened.
        version: Footer identity read from that file.
    """

    __slots__ = ("_archive", "_fd", "_size", "_version")

    def __init__(
        self, archive: Archive, fd: int, size: int, version: VersionInfo, /
    ) -> None:
        self._archive = archive
        self._fd = fd
        self._size = size
        self._version = version

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    @property
    def version(self) -> VersionInfo:
        """Identity of the pinned version."""
        return self._version

    @property
    def path(self) -> Path:
        """Published path this reader was opened from."""
        return self._archive.path

    def lazy(self) -> LazyFrame[Any]:
        """Return a lazy frame pinned to the reader's version."""
        return _scan_fd(self._fd, self._size, self._archive.read_backend)

    def receipt(self) -> Receipt:
        """Return a `"read"` receipt for the pinned version."""
        return Receipt(
            status="read", version=self._version, basis=None, path=self._archive.path
        )

    def close(self) -> None:
        """Close the reader's descriptor.

        Lazy frames already created by the reader hold their own descriptor
        duplicates, so they stay readable after this call.
        """
        fd = getattr(self, "_fd", -1)
        if fd != -1:
            self._fd = -1
            with suppress(OSError):  # pragma: no cover
                os.close(fd)


@contextmanager
def _publication_lock(path: Path, /) -> Iterator[None]:
    """Serialize check-and-replace so two publishers cannot both win a basis.

    Readers do not take this lock: they only need the descriptor they already
    opened. On platforms without `fcntl`, publication still verifies the basis,
    with the unavoidable small race between the final check and rename.
    """
    try:
        import fcntl
    except ImportError:  # pragma: no cover
        yield
        return
    lock_path = path.with_name(f".{path.name}.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _native_polars_sink(
    frame: Any, path: Path, metadata: Mapping[str, str], options: Mapping[str, Any]
) -> None:
    import polars as pl  # ignore-banned-import

    native = frame.to_native() if isinstance(frame, LazyFrame) else frame
    if isinstance(native, pl.DataFrame):
        native = native.lazy()
    if not isinstance(native, pl.LazyFrame):
        msg = (
            "The Polars archive backend expects a Polars LazyFrame; got "
            f"{type(native).__name__}."
        )
        raise ArchiveStagingError(msg)
    native.sink_parquet(str(path), metadata=dict(metadata), **dict(options))


def _default_sink(backend: str, /) -> NativeSink | None:
    if backend == "polars":
        return _native_polars_sink
    return None


def _missing_sink(backend: str, /) -> ArchiveStagingError:
    return ArchiveStagingError(
        f"The {backend!r} archive backend has no native lazy Parquet sink.\n\n"
        "Hint: use backend='polars' for publishing; pyarrow and pandas are "
        "read backends only."
    )


class Archive:
    """A published Parquet path whose versions can be replaced atomically.

    Readers opened from an archive are pinned: they keep reading the version which
    was published when they were opened, even after a newer version replaces the
    path. Writers prepare a new version against a specific basis and are refused if
    that basis is no longer the published one.

    Arguments:
        path: Filesystem path of the published Parquet file.
        backend: Backend used to write the file. Publishing requires a backend with
            a native lazy sink, currently `"polars"`.
        read_backend: Backend used by readers and basis scans. Defaults to
            `backend`; may be `"polars"`, `"pyarrow"` or `"pandas"`.

    Examples:
        >>> import polars as pl
        >>> import narwhals as nw
        >>> archive = nw.archive.open_archive("out.parquet")  # doctest:+SKIP
        >>> prepared = archive.prepare("v1")  # doctest:+SKIP
        >>> prepared.publish(pl.LazyFrame({"a": [1]}))  # doctest:+SKIP
        Receipt(status='published', version='v1', basis=None, ...)
    """

    __slots__ = ("_backend", "_path", "_read_backend")

    def __init__(
        self,
        path: FileSource,
        *,
        backend: str = "polars",
        read_backend: str | None = None,
    ) -> None:
        self._path = _coerce_path(path)
        self._backend = backend
        self._read_backend = backend if read_backend is None else read_backend
        _implementation(self._backend)
        _implementation(self._read_backend)

    @property
    def path(self) -> Path:
        """Filesystem path of the published file."""
        return self._path

    @property
    def backend(self) -> str:
        """Backend used for writing."""
        return self._backend

    @property
    def read_backend(self) -> str:
        """Backend used by readers and basis scans."""
        return self._read_backend

    def __repr__(self) -> str:
        return f"Archive(path={str(self._path)!r}, backend={self._backend!r})"

    def version(self) -> VersionInfo | None:
        """Return the currently published version, or `None` if nothing is published."""
        footer = _footer_at(self._path, self._read_backend)
        return None if footer is None else footer.info

    def reader(self) -> Reader:
        """Open a pinned reader on the currently published version.

        Raises:
            ArchiveError: If no published version exists, or the file is not an
                archive version.
        """
        try:
            fd = os.open(self._path, os.O_RDONLY)
        except FileNotFoundError:
            msg = (
                f"No published archive exists at {str(self._path)!r}.\n\n"
                "Hint: publish an initial version with `Archive.prepare(...)` first."
            )
            raise ArchiveError(msg) from None
        size = os.fstat(fd).st_size
        read_reader = _PinnedReader(os.dup(fd), size)
        try:
            footer = _read_footer(read_reader, _implementation(self._read_backend))
        except Exception as e:
            os.close(fd)
            msg = f"The published file at {str(self._path)!r} could not be read: {e}"
            raise ArchiveError(msg) from e
        finally:
            read_reader.close()
        if footer is None:
            os.close(fd)
            msg = (
                f"The Parquet file at {str(self._path)!r} is not an archive version.\n\n"
                "Hint: an archive file carries `narwhals.archive.*` footer metadata."
            )
            raise ArchiveError(msg)
        return Reader(self, fd, size, footer.info)

    def scan(self) -> tuple[LazyFrame[Any], Receipt]:
        """Lazily scan the version currently published at `path`.

        Returns:
            A pair of the pinned lazy frame and a `"read"` receipt.
        """
        reader = self.reader()
        try:
            return reader.lazy(), reader.receipt()
        finally:
            reader.close()

    def prepare(
        self,
        version: str,
        *,
        basis: VersionInfo | Literal["current"] | None = "current",
        row_group_size: int | None = None,
        compression: str = "uncompressed",
        statistics: bool | str | Mapping[str, bool] = False,
        sink: NativeSink | None = None,
    ) -> Prepared:
        """Prepare a new version against a pinned basis.

        Arguments:
            version: Target version name, e.g. `"v2"`.
            basis: Version the new result is deduplicated against.

                - `"current"` (default) pins the version currently published.
                - `None` starts a new archive; it is refused if the path exists.
                - A [`VersionInfo`][narwhals.archive.VersionInfo] pins that exact
                  version. If it is no longer published, the returned plan is
                  already stale, and `publish` refuses without running the source.
            row_group_size: Row group size for the native sink.
            compression: Parquet compression for the native sink.
            statistics: Parquet statistics option for the native sink.
            sink: Optional native sink override. Mostly useful for diagnostics;
                the default writes with Polars' lazy `sink_parquet`.

        Returns:
            A prepared writer exposing the pinned `basis` frame and `publish`.
        """
        _check_version_name(version)
        options = {
            "compression": compression,
            "statistics": statistics,
            "row_group_size": row_group_size,
        }
        if basis == "current":
            fd, size, info = self._open_current()
            return Prepared(
                self,
                version=version,
                basis=info,
                fd=fd,
                size=size,
                stale=False,
                options=options,
                sink=sink,
            )
        if basis is None:
            return Prepared(
                self,
                version=version,
                basis=None,
                fd=None,
                size=0,
                stale=self._path.exists(),
                options=options,
                sink=sink,
            )
        pinned = cast("VersionInfo", basis)
        fd, size, info = self._open_current()
        if info is not None and info.token == pinned.token:
            return Prepared(
                self,
                version=version,
                basis=info,
                fd=fd,
                size=size,
                stale=False,
                options=options,
                sink=sink,
            )
        if fd is not None:
            os.close(fd)
        return Prepared(
            self,
            version=version,
            basis=pinned,
            fd=None,
            size=0,
            stale=True,
            options=options,
            sink=sink,
        )

    def _open_current(self) -> tuple[int | None, int, VersionInfo | None]:
        try:
            fd = os.open(self._path, os.O_RDONLY)
        except FileNotFoundError:
            return None, 0, None
        size = os.fstat(fd).st_size
        read_reader = _PinnedReader(os.dup(fd), size)
        try:
            footer = _read_footer(read_reader, _implementation(self._read_backend))
        finally:
            read_reader.close()
        return fd, size, None if footer is None else footer.info

    def read(self) -> tuple[LazyFrame[Any], Receipt]:
        """Alias for [`scan`][narwhals.archive.Archive.scan]."""
        return self.scan()


class Prepared:
    """A prepared archive version, pinned to the basis it was built from.

    Rows for the new version are typically derived from `basis`, then published
    with [`publish`][narwhals.archive.Prepared.publish]. A plan whose basis went
    stale is refused without executing the source pipeline.
    """

    __slots__ = (
        "_archive",
        "_basis",
        "_fd",
        "_options",
        "_sink",
        "_size",
        "_stale",
        "_version",
    )

    def __init__(
        self,
        archive: Archive,
        *,
        version: str,
        basis: VersionInfo | None,
        fd: int | None,
        size: int,
        stale: bool,
        options: Mapping[str, Any],
        sink: NativeSink | None,
    ) -> None:
        self._archive = archive
        self._version = version
        self._basis = basis
        self._fd = fd
        self._size = size
        self._stale = stale
        self._options = dict(options)
        self._sink = sink

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def __del__(self) -> None:
        self.close()

    @property
    def archive(self) -> Archive:
        """The owning archive."""
        return self._archive

    @property
    def version(self) -> str:
        """Target version name."""
        return self._version

    @property
    def basis_version(self) -> VersionInfo | None:
        """Identity of the pinned basis version, if there is one."""
        return self._basis

    @property
    def stale(self) -> bool:
        """Whether the plan's declared basis was already stale when prepared."""
        return self._stale

    @property
    def basis(self) -> LazyFrame[Any]:
        """Pinned lazy view of the basis version.

        Every access starts a new scan from the same descriptor, so a pipeline can
        read the basis in stages. This is the frame to deduplicate new rows against.
        """
        if self._stale:
            msg = (
                f"Basis version {self._basis_name()!r} is no longer published, so "
                "its basis cannot be read.\n\n"
                "Hint: prepare again against the current version."
            )
            raise ArchiveBasisMismatchError(msg)
        if self._fd is None:
            msg = (
                "This plan starts a new archive and has no previous version to "
                "read as a basis."
            )
            raise ArchiveBasisMismatchError(msg)
        return _scan_fd(self._fd, self._size, self._archive.read_backend)

    def close(self) -> None:
        """Close the plan's descriptor.

        Scans already created from `basis` hold their own descriptor duplicates.
        """
        fd = getattr(self, "_fd", None)
        if fd is not None:
            self._fd = None
            with suppress(OSError):  # pragma: no cover
                os.close(fd)

    def _current(self) -> VersionInfo | None:
        footer = _footer_at(self._archive.path, self._archive.read_backend)
        return None if footer is None else footer.info

    def _basis_is_current(self) -> bool:
        current = self._current()
        if self._basis is None:
            return current is None
        return current is not None and current.token == self._basis.token

    def publish(
        self,
        frame: Any,
        *,
        row_group_size: int | None = None,
        compression: str | None = None,
        statistics: bool | str | Mapping[str, bool] | None = None,
        sink: NativeSink | None = None,
    ) -> Receipt:
        """Publish `frame` as this prepared version.

        The frame is written to a sibling staging file with the native sink. The
        written footer is then compared with the plan. Only a matching file
        atomically replaces the published path. The basis is checked again under a
        publication lock immediately before the replacement.

        The source pipeline is not executed when the plan is already stale or when
        the basis changed before the write starts.

        Returns:
            A receipt whose `status` is `"published"` on success, or one of
            `"stale"`, `"staging-failed"`, `"verification-failed"` or
            `"publish-failed"` otherwise. Call
            [`Receipt.raise_for_status`][narwhals.archive.Receipt.raise_for_status]
            to turn a failure into an exception.
        """
        path = self._archive.path
        if self._stale:
            return self._stale_receipt(run=False)

        options = self._publish_options(
            row_group_size=row_group_size, compression=compression, statistics=statistics
        )
        with _publication_lock(path):
            return self._publish_locked(path, frame, options, sink=sink)

    def _publish_options(
        self,
        *,
        row_group_size: int | None,
        compression: str | None,
        statistics: bool | str | Mapping[str, bool] | None,
    ) -> dict[str, Any]:
        options = dict(self._options)
        if row_group_size is not None:
            options["row_group_size"] = row_group_size
        if compression is not None:
            options["compression"] = compression
        if statistics is not None:
            options["statistics"] = statistics
        return options

    def _publish_locked(
        self,
        path: Path,
        frame: Any,
        options: Mapping[str, Any],
        *,
        sink: NativeSink | None,
    ) -> Receipt:
        if not self._basis_is_current():
            self._stale = True
            return self._stale_receipt(run=False)

        token = _random_token()
        metadata = {
            _VERSION_KEY: self._version,
            _TOKEN_KEY: token,
            _BASIS_KEY: _GENESIS if self._basis is None else self._basis.token,
        }
        staging = path.with_name(f".{path.name}.{_random_token()}.staging.parquet")
        outcome = self._replace_if_verified(
            path, frame, staging, metadata, token, options, sink=sink
        )
        if isinstance(outcome, Receipt):
            return outcome
        return Receipt(
            status="published", version=outcome.info, basis=self._basis, path=path
        )

    def _replace_if_verified(
        self,
        path: Path,
        frame: Any,
        staging: Path,
        metadata: Mapping[str, str],
        token: str,
        options: Mapping[str, Any],
        *,
        sink: NativeSink | None,
    ) -> _Footer | Receipt:
        writer = sink or self._sink or _default_sink(self._archive.backend)
        if writer is None:
            return self._failure("staging-failed", _missing_sink(self._archive.backend))
        staged = self._stage(writer, frame, staging, metadata, options)
        if isinstance(staged, Receipt):
            return staged

        verified = self._verify_staging(staging, token)
        if isinstance(verified, Receipt):
            return verified

        if not self._basis_is_current():
            staging.unlink(missing_ok=True)
            self._stale = True
            return self._stale_receipt(run=True)

        try:
            Path(staging).replace(path)
        except OSError as e:  # pragma: no cover
            staging.unlink(missing_ok=True)
            return self._failure(
                "publish-failed",
                ArchivePublishError(f"Replacing the published path failed: {e}"),
            )
        return verified

    def _stage(
        self,
        writer: NativeSink,
        frame: Any,
        staging: Path,
        metadata: Mapping[str, str],
        options: Mapping[str, Any],
    ) -> None | Receipt:
        try:
            writer(frame, staging, metadata, options)
        except Exception as e:  # noqa: BLE001
            staging.unlink(missing_ok=True)
            return self._failure(
                "staging-failed",
                ArchiveStagingError(f"Writing the staged version failed: {e}"),
            )
        return None

    def _verify_staging(self, staging: Path, token: str, /) -> _Footer | Receipt:
        try:
            footer = _footer_at(staging, self._archive.backend)
        except Exception as e:  # noqa: BLE001
            staging.unlink(missing_ok=True)
            return self._failure(
                "verification-failed",
                ArchivePublishError(
                    f"The staged file could not be read back for verification: {e}"
                ),
            )
        problem = self._verify(footer, token)
        if problem is not None:
            staging.unlink(missing_ok=True)
            return self._failure("verification-failed", problem)
        assert footer is not None  # noqa: S101
        return footer

    def _stale_receipt(self, *, run: bool) -> Receipt:
        detail = (
            "the source pipeline was not published."
            if run
            else "the source pipeline was not run."
        )
        return self._failure(
            "stale",
            ArchiveBasisMismatchError(
                f"Basis version {self._basis_name()!r} is no longer the published "
                f"version; {detail}"
            ),
        )

    def _verify(
        self, footer: _Footer | None, token: str, /
    ) -> ArchivePublishError | None:
        expected_basis = None if self._basis is None else self._basis.token
        if footer is None:
            return ArchivePublishError(
                "The staged file does not carry archive footer metadata."
            )
        if footer.name != self._version:
            return ArchivePublishError(
                f"The staged file declares version {footer.name!r}, expected "
                f"{self._version!r}."
            )
        if footer.token != token:
            return ArchivePublishError(
                "The staged file's version token does not match the publication."
            )
        if footer.basis != expected_basis:
            return ArchivePublishError(
                f"The staged file declares basis {footer.basis!r}, expected "
                f"{expected_basis!r}."
            )
        return None

    def _basis_name(self) -> str | None:
        return None if self._basis is None else self._basis.name

    def _failure(self, status: ArchiveStatus, error: ArchiveError) -> Receipt:
        return Receipt(
            status=status,
            version=None,
            basis=self._basis,
            path=self._archive.path,
            error=error,
        )


def open_archive(
    path: FileSource, *, backend: str = "polars", read_backend: str | None = None
) -> Archive:
    """Open a versioned Parquet archive at `path`.

    Arguments:
        path: Filesystem path of the published Parquet file.
        backend: Backend used to write the file. Publishing requires `"polars"`.
        read_backend: Backend used for lazy scans and basis reads. Defaults to
            `backend`; may be `"polars"`, `"pyarrow"` or `"pandas"`.

    Examples:
        >>> import narwhals as nw
        >>> archive = nw.archive.open_archive("out.parquet")  # doctest:+SKIP
        >>> archive.backend  # doctest:+SKIP
        'polars'
    """
    return Archive(path, backend=backend, read_backend=read_backend)


def prepare_archive(
    path: FileSource,
    version: str,
    *,
    basis: VersionInfo | Literal["current"] | None = "current",
    backend: str = "polars",
    read_backend: str | None = None,
    **kwargs: Any,
) -> Prepared:
    """Prepare a version on the archive at `path`.

    This is a convenience wrapper around
    [`Archive.prepare`][narwhals.archive.Archive.prepare].
    """
    return open_archive(path, backend=backend, read_backend=read_backend).prepare(
        version, basis=basis, **kwargs
    )


def read_archive(
    path: FileSource, *, backend: str = "polars", read_backend: str | None = None
) -> tuple[LazyFrame[Any], Receipt]:
    """Read the currently published version of the archive at `path`.

    This is a convenience wrapper around [`Archive.scan`][narwhals.archive.Archive.scan].
    """
    return open_archive(path, backend=backend, read_backend=read_backend).scan()
