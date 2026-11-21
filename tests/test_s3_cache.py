import threading
from dataclasses import dataclass, field
from datetime import datetime
from typing import Dict, List, Optional, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq
import pytest

from parquet_tools.commands.s3_cache import (
    IncompleteDownloadError, ObjectVersion, TaskObjectCache,
    VersionChangedDuringDownloadError,
)
from parquet_tools.commands.utils import FileNotFoundException, S3ParquetFile


class MidDownloadError(Exception):
    '''Simulated S3 transfer failure after partial bytes were written.'''


@dataclass
class FakeObject:
    data: bytes
    etag: Optional[str]
    version_id: Optional[str] = None

    @property
    def content_length(self) -> int:
        return len(self.data)


@dataclass
class DownloadRecord:
    endpoint: str
    bucket: str
    key: str
    identity: Optional[str]
    bytes_written: int
    success: bool


@dataclass
class FakeS3Backend:
    objects: Dict[Tuple[str, str], FakeObject] = field(default_factory=dict)
    records: List[DownloadRecord] = field(default_factory=list)
    fail_attempts: Dict[Tuple[str, str, str], int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)
    gate: bool = False
    started: threading.Event = field(default_factory=threading.Event)
    release: threading.Event = field(default_factory=threading.Event)

    def put(self, bucket: str, key: str, data: bytes,
            etag: Optional[str], version_id: Optional[str] = None) -> None:
        self.objects[(bucket, key)] = FakeObject(data, etag, version_id)

    def current(self, bucket: str, key: str) -> FakeObject:
        return self.objects[(bucket, key)]


class FakeS3Client:
    def __init__(self, backend: FakeS3Backend, endpoint_url: Optional[str]):
        self._backend = backend
        self._endpoint = endpoint_url or ''

    def head_object(self, Bucket: str, Key: str) -> dict:
        obj = self._backend.current(Bucket, Key)
        response = {
            'ETag': f'"{obj.etag}"' if obj.etag else None,
            'ContentLength': obj.content_length,
            'Metadata': {},
            'LastModified': datetime(2026, 11, 22),
            'ResponseMetadata': {'HTTPStatusCode': 200},
        }
        if obj.version_id is not None:
            response['VersionId'] = obj.version_id
        return response

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:
        backend = self._backend
        if backend.gate:
            backend.started.set()
            if not backend.release.wait(timeout=10):
                raise RuntimeError('test gate was never released')

        obj = backend.current(Bucket, Key)
        identity = ObjectVersion.from_head(self.head_object(Bucket, Key)).identity
        fail_key = (self._endpoint, Bucket, Key)
        with backend.lock:
            remaining = backend.fail_attempts.get(fail_key, 0)
            if remaining > 0:
                backend.fail_attempts[fail_key] = remaining - 1
                partial = obj.data[: max(1, len(obj.data) // 2)]
                with open(Filename, 'wb') as handle:
                    handle.write(partial)
                backend.records.append(DownloadRecord(
                    endpoint=self._endpoint, bucket=Bucket, key=Key,
                    identity=identity, bytes_written=len(partial),
                    success=False,
                ))
                raise MidDownloadError('connection reset mid-download')

        with open(Filename, 'wb') as handle:
            handle.write(obj.data)
        with backend.lock:
            backend.records.append(DownloadRecord(
                endpoint=self._endpoint, bucket=Bucket, key=Key,
                identity=identity, bytes_written=len(obj.data),
                success=True,
            ))

    def list_objects_v2(self, Bucket: str, Prefix: str) -> dict:
        return {'IsTruncated': False, 'KeyCount': 0, 'Contents': []}


class _FakeS3Resource:
    def __init__(self, client: FakeS3Client):
        self.meta = type('Meta', (), {'client': client})()


class FakeSession:
    def __init__(self, backend: FakeS3Backend):
        self._backend = backend

    def client(self, service: str, endpoint_url: Optional[str] = None):
        assert service == 's3'
        return FakeS3Client(self._backend, endpoint_url)

    def resource(self, service: str, endpoint_url: Optional[str] = None):
        assert service == 's3'
        return _FakeS3Resource(FakeS3Client(self._backend, endpoint_url))


def parquet_bytes(markers: List[str]) -> bytes:
    frame = pd.DataFrame({'id': [1, 2, 3], 'marker': markers})
    table = pa.Table.from_pandas(frame, preserve_index=False)
    buffer = pa.BufferOutputStream()
    pq.write_table(
        table, buffer,
        compression=None, use_dictionary=False, write_statistics=False,
    )
    return buffer.getvalue().to_pybytes()


def read_markers(path: str) -> List[str]:
    return pq.read_table(path).to_pandas()['marker'].tolist()


def make_pf(session: FakeSession, bucket: str, key: str,
            endpoint_url: Optional[str] = None,
            cache: Optional[TaskObjectCache] = None) -> S3ParquetFile:
    return S3ParquetFile(
        aws_session=session, bucket=bucket, key=key,
        endpoint_url=endpoint_url, cache=cache,
    )


def run_in_threads(targets, timeout: float = 10.0):
    errors = []

    def runner(target):
        try:
            target()
        except BaseException as exc:  # captured and re-raised by the test
            errors.append(exc)

    threads = [threading.Thread(target=runner, args=(t,)) for t in targets]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout)
        assert not thread.is_alive(), 'reader thread hung'
    return errors


V1_MARKERS = ['aaa', 'bbb', 'ccc']
V2_MARKERS = ['xxx', 'yyy', 'zzz']
BUCKET = 'mybucket'
KEY = 'target.parquet'
ENDPOINT_A = 'https://s3-a.example.com'
ENDPOINT_B = 'https://s3-b.example.com'


@pytest.fixture
def backend():
    return FakeS3Backend()


@pytest.fixture
def session(backend):
    return FakeSession(backend)


class TestConcurrentSameVersion:
    def test_two_readers_share_one_full_download(self, backend, session):
        data_v1 = parquet_bytes(V1_MARKERS)
        backend.put(BUCKET, KEY, data_v1, etag='etag-v1')

        with TaskObjectCache() as cache:
            pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            paths = []
            markers = []

            def reader():
                with pf.get_local_path() as path:
                    paths.append(path)
                    markers.append(read_markers(path))

            errors = run_in_threads([reader, reader])
            assert errors == []

            success = [r for r in backend.records if r.success]
            assert len(success) == 1
            assert success[0].identity == 'etag:etag-v1'
            assert len(paths) == 2
            assert paths[0] == paths[1]
            assert markers == [V1_MARKERS, V1_MARKERS]

            snapshot = cache.snapshot()
            assert len(snapshot) == 1
            assert snapshot[0].refcount == 0
            assert snapshot[0].completed
            assert snapshot[0].exists

    def test_refcount_grows_while_both_readers_hold_copy(self, backend, session):
        data_v1 = parquet_bytes(V1_MARKERS)
        backend.put(BUCKET, KEY, data_v1, etag='etag-v1')
        backend.gate = True

        with TaskObjectCache() as cache:
            pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            entered = threading.Event()
            release_reader = threading.Event()

            def holder():
                with pf.get_local_path():
                    entered.set()
                    release_reader.wait(timeout=10)

            thread = threading.Thread(target=holder)
            thread.start()
            assert backend.started.wait(timeout=10)

            def second_reader():
                with pf.get_local_path() as path:
                    snapshot = cache.snapshot()
                    assert snapshot[0].refcount == 2
                    assert snapshot[0].exists
                    assert read_markers(path) == V1_MARKERS

            second = threading.Thread(target=second_reader)
            second.start()
            backend.release.set()
            second.join(10)
            assert not second.is_alive()
            release_reader.set()
            thread.join(10)
            assert not thread.is_alive()


class TestVersionSwap:
    def test_same_path_and_length_keeps_confirmed_old_version(self, backend, session):
        data_v1 = parquet_bytes(V1_MARKERS)
        data_v2 = parquet_bytes(V2_MARKERS)
        assert len(data_v1) == len(data_v2)
        assert data_v1 != data_v2
        backend.put(BUCKET, KEY, data_v1, etag='etag-v1')

        with TaskObjectCache() as cache:
            old_pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            old_in_context = threading.Event()
            let_old_exit = threading.Event()

            def old_reader():
                with old_pf.get_local_path() as old_path:
                    old_in_context.set()
                    let_old_exit.wait(timeout=10)
                    assert read_markers(old_path) == V1_MARKERS

            old_thread = threading.Thread(target=old_reader)
            old_thread.start()
            assert old_in_context.wait(timeout=10)

            backend.put(BUCKET, KEY, data_v2, etag='etag-v2')
            new_pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            with new_pf.get_local_path() as new_path:
                assert read_markers(new_path) == V2_MARKERS
                entries = {s.identity: s for s in cache.snapshot()}
                assert set(entries) == {'etag:etag-v1', 'etag:etag-v2'}
                assert all(s.exists for s in entries.values())

                let_old_exit.set()
                old_thread.join(10)
                assert not old_thread.is_alive()

                assert read_markers(new_path) == V2_MARKERS

            assert len([r for r in backend.records if r.success]) == 2

    def test_early_exit_does_not_remove_copy_used_by_other(self, backend, session):
        data_v1 = parquet_bytes(V1_MARKERS)
        backend.put(BUCKET, KEY, data_v1, etag='etag-v1')

        with TaskObjectCache() as cache:
            pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            survivor_ready = threading.Event()
            let_survivor_exit = threading.Event()

            def survivor():
                with pf.get_local_path() as path:
                    survivor_ready.set()
                    let_survivor_exit.wait(timeout=10)
                    assert read_markers(path) == V1_MARKERS

            survivor_thread = threading.Thread(target=survivor)
            survivor_thread.start()
            assert survivor_ready.wait(timeout=10)

            with pf.get_local_path() as early_path:
                early_copy = early_path
            assert read_markers(early_copy) == V1_MARKERS

            let_survivor_exit.set()
            survivor_thread.join(10)
            assert not survivor_thread.is_alive()


class TestDownloadFailure:
    def test_failed_first_attempt_recovers_without_leaking_fragment(self, backend, session):
        data_v1 = parquet_bytes(V1_MARKERS)
        backend.put(BUCKET, KEY, data_v1, etag='etag-v1')
        backend.fail_attempts[(ENDPOINT_A, BUCKET, KEY)] = 1

        with TaskObjectCache() as cache:
            pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            leader_error = []

            def leader():
                try:
                    with pf.get_local_path():
                        raise AssertionError('leader should not receive a copy')
                except FileNotFoundException as exc:
                    assert isinstance(exc.__cause__, MidDownloadError)
                    leader_error.append(True)

            def follower():
                with pf.get_local_path() as path:
                    assert read_markers(path) == V1_MARKERS

            errors = run_in_threads([leader, follower])
            assert errors == []
            assert leader_error == [True]

            records = backend.records
            assert [r.success for r in records] == [False, True]
            assert records[0].bytes_written < len(data_v1)
            assert records[1].bytes_written == len(data_v1)
            assert cache.partial_files() == []

            snapshot = cache.snapshot()
            assert len(snapshot) == 1
            assert snapshot[0].completed
            assert snapshot[0].exists

    def test_new_read_after_failure_does_not_reuse_fragment(self, backend, session):
        data_v1 = parquet_bytes(V1_MARKERS)
        backend.put(BUCKET, KEY, data_v1, etag='etag-v1')
        backend.fail_attempts[(ENDPOINT_A, BUCKET, KEY)] = 1

        with TaskObjectCache() as cache:
            pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)

            with pytest.raises(FileNotFoundException) as excinfo:
                with pf.get_local_path():
                    pass
            assert isinstance(excinfo.value.__cause__, MidDownloadError)
            assert cache.partial_files() == []

            with pf.get_local_path() as path:
                assert read_markers(path) == V1_MARKERS
            assert cache.partial_files() == []

            assert [r.success for r in backend.records] == [False, True]


class TestEndpointAndTaskScope:
    def test_different_endpoint_same_bucket_key_not_reused(self, backend, session):
        data_v1 = parquet_bytes(V1_MARKERS)
        backend.put(BUCKET, KEY, data_v1, etag='etag-v1')

        with TaskObjectCache() as cache:
            pf_a = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            pf_b = make_pf(session, BUCKET, KEY, ENDPOINT_B, cache)

            with pf_a.get_local_path() as path_a:
                with pf_a.get_local_path() as path_a_again:
                    assert path_a == path_a_again
                with pf_b.get_local_path() as path_b:
                    assert path_b != path_a
                    assert read_markers(path_b) == V1_MARKERS

            endpoint_a = [r for r in backend.records
                          if r.endpoint == ENDPOINT_A and r.success]
            endpoint_b = [r for r in backend.records
                          if r.endpoint == ENDPOINT_B and r.success]
            assert len(endpoint_a) == 1
            assert len(endpoint_b) == 1

            identities = {(s.object_key[0], s.identity) for s in cache.snapshot()}
            assert identities == {
                (ENDPOINT_A, 'etag:etag-v1'),
                (ENDPOINT_B, 'etag:etag-v1'),
            }

    def test_cache_is_not_shared_between_tasks(self, backend, session):
        data_v1 = parquet_bytes(V1_MARKERS)
        backend.put(BUCKET, KEY, data_v1, etag='etag-v1')

        with TaskObjectCache() as first_task:
            pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, first_task)
            with pf.get_local_path() as path:
                assert read_markers(path) == V1_MARKERS
            first_root = first_task.root

        with TaskObjectCache() as second_task:
            assert second_task.root != first_root
            pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, second_task)
            with pf.get_local_path() as path:
                assert read_markers(path) == V1_MARKERS
            assert len(second_task.snapshot()) == 1

        assert len([r for r in backend.records if r.success]) == 2


class TestVersionEvidence:
    def test_version_id_takes_precedence_over_etag(self, backend, session):
        data_v1 = parquet_bytes(V1_MARKERS)
        backend.put(BUCKET, KEY, data_v1, etag='etag-v1', version_id='VID-1')

        with TaskObjectCache() as cache:
            pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            assert pf.head_version().identity == 'versionId:VID-1'

            with pf.get_local_path():
                pass
            with pf.get_local_path():
                pass
            assert len([r for r in backend.records if r.success]) == 1
            assert cache.snapshot()[0].identity == 'versionId:VID-1'

    def test_equal_length_but_different_etag_never_reused(self, backend, session):
        data_v1 = parquet_bytes(V1_MARKERS)
        data_v2 = parquet_bytes(V2_MARKERS)
        assert len(data_v1) == len(data_v2)
        backend.put(BUCKET, KEY, data_v1, etag='etag-v1')

        with TaskObjectCache() as cache:
            pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            with pf.get_local_path() as old_path:
                backend.put(BUCKET, KEY, data_v2, etag='etag-v2')
            with pf.get_local_path() as new_path:
                assert old_path != new_path
                assert read_markers(new_path) == V2_MARKERS

            identities = sorted(s.identity for s in cache.snapshot())
            assert identities == ['etag:etag-v1', 'etag:etag-v2']

    def test_missing_evidence_disables_reuse_with_explanation(
            self, backend, session, caplog):
        data_v1 = parquet_bytes(V1_MARKERS)
        backend.put(BUCKET, KEY, data_v1, etag=None)

        with TaskObjectCache() as cache:
            pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            with caplog.at_level('WARNING'):
                with pf.get_local_path() as path:
                    assert read_markers(path) == V1_MARKERS
            assert cache.snapshot() == []
            assert any(
                'version evidence' in record.message
                and 'name or length' in record.message
                for record in caplog.records
            )

        with pytest.raises(ValueError) as excinfo:
            with TaskObjectCache() as cache:
                with cache.acquire(
                    object_key=(ENDPOINT_A, BUCKET, KEY),
                    identity='',
                    download=lambda tmp: None,
                    verify=lambda: ObjectVersion(),
                ):
                    pass
        assert 'object name or byte length' in str(excinfo.value)


class TestCacheGuards:
    def test_version_change_during_download_is_rejected(self):
        current = {'version': ObjectVersion(etag='etag-v2', content_length=4)}
        with TaskObjectCache() as cache:
            with pytest.raises(VersionChangedDuringDownloadError):
                with cache.acquire(
                    object_key=(ENDPOINT_A, BUCKET, KEY),
                    identity='etag:etag-v1',
                    download=lambda tmp: open(tmp, 'wb').write(b'data'),
                    verify=lambda: current['version'],
                ):
                    pass
            assert cache.partial_files() == []
            assert cache.snapshot()[0].completed is False

    def test_incomplete_download_is_rejected_and_recoverable(self):
        confirmed = ObjectVersion(etag='etag-v1', content_length=10)

        def truncated(tmp):
            with open(tmp, 'wb') as handle:
                handle.write(b'short')

        with TaskObjectCache() as cache:
            object_key = (ENDPOINT_A, BUCKET, KEY)
            with pytest.raises(IncompleteDownloadError):
                with cache.acquire(
                    object_key=object_key, identity='etag:etag-v1',
                    download=truncated, verify=lambda: confirmed,
                ):
                    pass
            assert cache.partial_files() == []

            def complete(tmp):
                with open(tmp, 'wb') as handle:
                    handle.write(b'0123456789')

            with cache.acquire(
                object_key=object_key, identity='etag:etag-v1',
                download=complete, verify=lambda: confirmed,
            ) as path:
                assert open(path, 'rb').read() == b'0123456789'
            assert cache.snapshot()[0].completed is True


class TestErrorBoundary:
    def test_missing_object_raises_file_not_found(self, backend, session):
        with TaskObjectCache() as cache:
            pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            with pytest.raises(FileNotFoundException):
                with pf.get_local_path():
                    pass

    def test_body_exception_is_not_wrapped(self, backend, session):
        data_v1 = parquet_bytes(V1_MARKERS)
        backend.put(BUCKET, KEY, data_v1, etag='etag-v1')

        class ReadBodyError(Exception):
            pass

        with TaskObjectCache() as cache:
            pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            with pytest.raises(ReadBodyError):
                with pf.get_local_path():
                    raise ReadBodyError('failure while processing rows')
            assert cache.snapshot()[0].refcount == 0

    def test_download_failure_maps_to_file_not_found(self, backend, session):
        data_v1 = parquet_bytes(V1_MARKERS)
        backend.put(BUCKET, KEY, data_v1, etag='etag-v1')
        backend.fail_attempts[(ENDPOINT_A, BUCKET, KEY)] = 1

        with TaskObjectCache() as cache:
            pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            with pytest.raises(FileNotFoundException) as excinfo:
                with pf.get_local_path():
                    pass
            assert isinstance(excinfo.value.__cause__, MidDownloadError)
            assert cache.partial_files() == []


class TestLifecycle:
    def test_task_end_releases_unused_copies(self, backend, session):
        data_v1 = parquet_bytes(V1_MARKERS)
        backend.put(BUCKET, KEY, data_v1, etag='etag-v1')

        cache = TaskObjectCache()
        pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
        with pf.get_local_path() as path:
            assert read_markers(path) == V1_MARKERS
        root = cache.root
        cache.close()

        import os
        assert not os.path.exists(root)

    def test_schema_then_data_reads_reuse_one_download(self, backend, session):
        data_v1 = parquet_bytes(V1_MARKERS)
        backend.put(BUCKET, KEY, data_v1, etag='etag-v1')

        with TaskObjectCache() as cache:
            pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            with pf.get_local_path() as path:
                metadata = pq.ParquetFile(path).metadata
                assert metadata.num_rows == 3
                assert read_markers(path) == V1_MARKERS

            with pf.get_local_path() as path:
                assert read_markers(path) == V1_MARKERS

            assert len([r for r in backend.records if r.success]) == 1


class TestFullArchiveTaskScenario:
    def test_parallel_readers_swap_failure_retry_and_endpoint_retry(
            self, backend, session):
        data_v1 = parquet_bytes(V1_MARKERS)
        data_v2 = parquet_bytes(V2_MARKERS)
        assert len(data_v1) == len(data_v2)
        second_key = 'other.parquet'
        backend.put(BUCKET, KEY, data_v1, etag='etag-v1')
        backend.put(BUCKET, second_key, data_v1, etag='etag-other-v1')

        with TaskObjectCache() as cache:
            pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            first_paths = []

            def concurrent_first_read():
                with pf.get_local_path() as path:
                    first_paths.append(path)
                    assert read_markers(path) == V1_MARKERS

            errors = run_in_threads([
                concurrent_first_read, concurrent_first_read,
            ])
            assert errors == []
            assert first_paths[0] == first_paths[1]
            assert len([r for r in backend.records if r.success]) == 1

            old_holder_ready = threading.Event()
            let_old_holder_exit = threading.Event()

            def old_version_holder():
                with pf.get_local_path() as old_path:
                    old_holder_ready.set()
                    let_old_holder_exit.wait(timeout=10)
                    assert read_markers(old_path) == V1_MARKERS

            holder_thread = threading.Thread(target=old_version_holder)
            holder_thread.start()
            assert old_holder_ready.wait(timeout=10)

            backend.put(BUCKET, KEY, data_v2, etag='etag-v2')
            new_pf = make_pf(session, BUCKET, KEY, ENDPOINT_A, cache)
            with new_pf.get_local_path() as new_path:
                assert read_markers(new_path) == V2_MARKERS
                version_entries = {s.identity: s for s in cache.snapshot()}
                assert set(version_entries) == {
                    'etag:etag-v1', 'etag:etag-v2',
                }

                backend.fail_attempts[(ENDPOINT_A, BUCKET, second_key)] = 1
                other_pf = make_pf(
                    session, BUCKET, second_key, ENDPOINT_A, cache,
                )
                failure = []

                def failing_leader():
                    try:
                        with other_pf.get_local_path():
                            raise AssertionError('leader must fail')
                    except FileNotFoundException as exc:
                        assert isinstance(exc.__cause__, MidDownloadError)
                        failure.append(True)

                def retrying_follower():
                    with other_pf.get_local_path() as path:
                        assert read_markers(path) == V1_MARKERS

                retry_errors = run_in_threads([
                    failing_leader, retrying_follower,
                ])
                assert retry_errors == []
                assert failure == [True]
                assert cache.partial_files() == []

                endpoint_b_pf = make_pf(
                    session, BUCKET, KEY, ENDPOINT_B, cache,
                )
                with endpoint_b_pf.get_local_path() as endpoint_b_path:
                    assert read_markers(endpoint_b_path) == V2_MARKERS

                let_old_holder_exit.set()
                holder_thread.join(10)
                assert not holder_thread.is_alive()

                assert read_markers(new_path) == V2_MARKERS

            endpoint_a_downloads = [
                r for r in backend.records
                if r.endpoint == ENDPOINT_A and r.success
            ]
            endpoint_b_downloads = [
                r for r in backend.records
                if r.endpoint == ENDPOINT_B and r.success
            ]
            assert len(endpoint_b_downloads) == 1
            assert {r.identity for r in endpoint_a_downloads} == {
                'etag:etag-v1', 'etag:etag-v2', 'etag:etag-other-v1',
            }
            other_records = [
                r for r in backend.records if r.key == second_key
            ]
            assert [r.success for r in other_records] == [False, True]
            assert all(
                record.identity is not None
                and (record.identity.startswith('etag:')
                     or record.identity.startswith('versionId:'))
                for record in backend.records
            )

        import os
        assert not os.path.exists(cache.root)
