import logging
import os
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from tempfile import TemporaryDirectory
from typing import Callable, Dict, List, Optional, Tuple
from uuid import uuid4

logger = logging.getLogger(__name__)

ObjectKey = Tuple[str, str, str]


class ObjectCacheError(Exception):
    '''Base class for task object cache failures.'''


class VersionChangedDuringDownloadError(ObjectCacheError):
    '''Raised when the object version at download completion no longer matches
    the version that was confirmed when the download started.
    '''


class IncompleteDownloadError(ObjectCacheError):
    '''Raised when a downloaded local copy does not match the confirmed size.
    '''


@dataclass(frozen=True)
class ObjectVersion:
    '''Version evidence obtained from S3 itself.

    Version identity is established only from S3-owned version evidence
    (VersionId when bucket versioning is enabled, otherwise ETag). The object
    name and the byte length are never treated as version identifiers.
    '''

    etag: Optional[str] = None
    version_id: Optional[str] = None
    content_length: Optional[int] = None

    @property
    def identity(self) -> Optional[str]:
        if self.version_id:
            return f'versionId:{self.version_id}'
        if self.etag:
            return f'etag:{self.etag}'
        return None

    @staticmethod
    def from_head(head_response: dict) -> 'ObjectVersion':
        etag = head_response.get('ETag')
        if etag:
            etag = etag.strip('"') or None
        return ObjectVersion(
            etag=etag,
            version_id=head_response.get('VersionId') or None,
            content_length=head_response.get('ContentLength'),
        )


@dataclass
class _CopyEntry:
    object_key: ObjectKey
    identity: str
    cond: threading.Condition
    path: Optional[str] = None
    refcount: int = 0
    in_progress: bool = False
    completed: bool = False
    error: Optional[BaseException] = None


@dataclass
class CopySnapshot:
    object_key: ObjectKey
    identity: str
    path: Optional[str]
    refcount: int
    completed: bool
    exists: bool


class TaskObjectCache:
    '''Task-scoped cache of complete local copies of S3 objects.

    A cache instance belongs to one archive task and is not shared across
    tasks. Copies are keyed by the (endpoint, bucket, key) tuple and by S3
    version evidence, so a reader that confirmed version N keeps reading its
    local copy of N even after the background object is replaced by a new
    version under the same path.

    Concurrent readers that present identical version evidence share one
    full download (single flight). Partial downloads are never published to
    other processing units and are discarded immediately. All copies that
    have no remaining user are released when the task ends.
    '''

    def __init__(self) -> None:
        self._tmpdir = TemporaryDirectory(prefix='parquet-task-cache-')
        self.root = self._tmpdir.name
        self._lock = threading.RLock()
        self._entries: Dict[ObjectKey, Dict[str, _CopyEntry]] = {}

    def __enter__(self) -> 'TaskObjectCache':
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            for object_key, versions in list(self._entries.items()):
                for identity, entry in list(versions.items()):
                    if entry.refcount > 0:
                        logger.warning(
                            'Skip releasing copy still in use at task end: '
                            '%s %s (refcount=%d)',
                            object_key, identity, entry.refcount,
                        )
                        continue
                    if entry.path:
                        self._discard(entry.path)
                    del versions[identity]
                if not versions:
                    del self._entries[object_key]
        self._tmpdir.cleanup()

    @contextmanager
    def acquire(
        self,
        object_key: ObjectKey,
        identity: str,
        download: Callable[[str], None],
        verify: Callable[[], ObjectVersion],
    ):
        '''Yield a complete local copy path for the confirmed version.

        ``download(tmp_path)`` stores the object bytes at ``tmp_path`` and
        raises if the transfer fails (a partial file may remain).
        ``verify()`` re-reads the current S3 version evidence so the
        downloaded copy can be checked against the version that was
        confirmed before the transfer.
        '''
        if not identity:
            raise ValueError(
                'Refusing to cache without reliable S3 version evidence '
                '(no VersionId/ETag); version must not be inferred from '
                'object name or byte length.'
            )

        entry, is_leader = self._attach(object_key, identity)
        try:
            if is_leader:
                self._run_download(entry, download, verify)
            else:
                self._wait_for_copy(entry, download, verify)
            yield entry.path
        finally:
            self._release(entry)

    def snapshot(self) -> List[CopySnapshot]:
        with self._lock:
            return [
                CopySnapshot(
                    object_key=entry.object_key,
                    identity=entry.identity,
                    path=entry.path,
                    refcount=entry.refcount,
                    completed=entry.completed,
                    exists=bool(entry.path) and os.path.exists(entry.path),
                )
                for versions in self._entries.values()
                for entry in versions.values()
            ]

    def partial_files(self) -> List[str]:
        return sorted(
            name for name in os.listdir(self.root)
            if name.startswith('.partial-')
        )

    def _attach(self, object_key: ObjectKey, identity: str):
        with self._lock:
            versions = self._entries.setdefault(object_key, {})
            entry = versions.get(identity)
            if entry is None:
                entry = _CopyEntry(
                    object_key=object_key,
                    identity=identity,
                    cond=threading.Condition(self._lock),
                )
                versions[identity] = entry
            entry.refcount += 1
            is_leader = (
                not entry.in_progress
                and not entry.completed
                and entry.error is None
            )
            if is_leader:
                entry.in_progress = True
            return entry, is_leader

    def _wait_for_copy(self, entry: _CopyEntry, download, verify) -> None:
        while True:
            with self._lock:
                while entry.in_progress and entry.error is None \
                        and not entry.completed:
                    entry.cond.wait()
                if entry.completed:
                    return
                if entry.error is not None and not entry.in_progress:
                    entry.error = None
                    entry.in_progress = True
                    should_retry = True
                else:
                    continue
            if should_retry:
                self._run_download(entry, download, verify)
                return

    def _run_download(self, entry: _CopyEntry, download, verify) -> None:
        tmp_path = os.path.join(
            self.root, f'.partial-{uuid4().hex}.parquet'
        )
        try:
            download(tmp_path)
            current = verify()
            if current.identity != entry.identity:
                raise VersionChangedDuringDownloadError(
                    f'Confirmed version {entry.identity} changed to '
                    f'{current.identity} during download of '
                    f'{entry.object_key}'
                )
            if (
                current.content_length is not None
                and os.path.getsize(tmp_path) != current.content_length
            ):
                raise IncompleteDownloadError(
                    f'Downloaded {os.path.getsize(tmp_path)} bytes but S3 '
                    f'reports {current.content_length} bytes for '
                    f'{entry.object_key}'
                )
            final_path = os.path.join(
                self.root,
                f'{uuid4().hex}-{_sanitize(entry.identity)}.parquet',
            )
            os.replace(tmp_path, final_path)
            with self._lock:
                entry.path = final_path
                entry.completed = True
                entry.in_progress = False
                entry.error = None
                entry.cond.notify_all()
        except BaseException as exc:
            self._discard(tmp_path)
            with self._lock:
                entry.error = exc
                entry.in_progress = False
                entry.cond.notify_all()
            raise

    def _release(self, entry: _CopyEntry) -> None:
        with self._lock:
            entry.refcount -= 1

    @staticmethod
    def _discard(path: str) -> None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def _sanitize(identity: str) -> str:
    return ''.join(ch if ch.isalnum() or ch in ('-', '_') else '_'
                   for ch in identity)
