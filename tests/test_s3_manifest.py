from __future__ import annotations

import pytest

from parquet_tools.commands.utils import S3ParquetFile
from parquet_tools.s3_manifest import (
    S3Manifest,
    S3ManifestConflictError,
    S3ManifestIncompleteError,
    S3ManifestStatus,
    S3ManifestStoppedError,
    S3ObjectObservation,
    STOP_CONTINUATION_CYCLE,
    STOP_ENDPOINT_MISMATCH,
    list_s3_prefix,
)
from tests.fixtures.fake_s3 import (
    FakeFailure,
    FakeObject,
    FakePage,
    FakeS3Service,
)


BUCKET = 'archive'
PREFIX = 'logs/'
ENDPOINT = 'http://127.0.0.1:9000'


def _object(key: str, etag: str = '"v1"', size: int = 10) -> FakeObject:
    return FakeObject(key=key, etag=etag, size=size)


def _file(session, endpoint_url, key: str = '*') -> S3ParquetFile:
    return S3ParquetFile(
        aws_session=session,
        bucket=BUCKET,
        key=f'{PREFIX}{key}',
        endpoint_url=endpoint_url,
    )


@pytest.fixture
def fake_session():
    return FakeS3Service()


@pytest.fixture
def fake_s3(fake_session):
    return fake_session.for_endpoint(ENDPOINT)


def _script_happy_path(server: FakeS3Server) -> None:
    server.add(None, FakePage(
        objects=(_object('logs/a.parquet'), _object('logs/b.parquet')),
        next_token='tok-2',
        is_truncated=True,
    ))
    server.add('tok-2', FakePage(
        objects=(
            _object('logs/b.parquet'),
            _object('logs/c.parquet'),
            _object('logs/d.parquet'),
        ),
        next_token='tok-3',
        is_truncated=True,
    ))
    server.add('tok-3', FakePage(
        objects=(_object('logs/e.parquet'),),
        is_truncated=False,
    ))


class TestPartialAndResume:
    def test_failed_page_does_not_advance_checkpoint(self, fake_session, fake_s3):
        fake_s3.add(None, FakePage(
            objects=(_object('logs/a.parquet'), _object('logs/b.parquet')),
            next_token='tok-2',
            is_truncated=True,
        ))
        fake_s3.add('tok-2', FakeFailure('ServiceUnavailable'))

        manifest = _file(fake_session, ENDPOINT).resolve_wildcard_manifest()

        assert manifest.status is S3ManifestStatus.PARTIAL
        assert manifest.pages_confirmed == 1
        assert manifest.continuation_token == 'tok-2'
        assert manifest.can_resume is True
        assert manifest.has_more is True
        assert manifest.keys == ('logs/a.parquet', 'logs/b.parquet')
        assert manifest.error is not None
        assert not manifest.is_complete

        # The checkpoint survives a JSON round-trip and continues at tok-2.
        checkpoint = manifest.to_json()
        fake_s3.add('tok-2', FakePage(
            objects=(_object('logs/b.parquet'), _object('logs/c.parquet')),
            is_truncated=False,
        ))
        resumed = _file(fake_session, ENDPOINT).resolve_wildcard_manifest(checkpoint)

        assert resumed.status is S3ManifestStatus.COMPLETE
        assert resumed.pages_confirmed == 2
        assert resumed.keys == ('logs/a.parquet', 'logs/b.parquet', 'logs/c.parquet')
        assert fake_s3.tokens == [None, 'tok-2', 'tok-2']

    def test_replayed_page_is_idempotent(self, fake_session, fake_s3):
        fake_s3.add(None, FakePage(
            objects=(_object('logs/a.parquet'), _object('logs/b.parquet')),
            next_token='tok-2',
            is_truncated=True,
        ))
        # A recovered service repeats b and adds c.
        fake_s3.add('tok-2', FakePage(
            objects=(_object('logs/b.parquet'), _object('logs/c.parquet')),
            next_token='tok-3',
            is_truncated=True,
        ))
        fake_s3.add('tok-3', FakePage(
            objects=(_object('logs/c.parquet'),),
            is_truncated=False,
        ))

        manifest = _file(fake_session, ENDPOINT).resolve_wildcard_manifest()

        assert manifest.status is S3ManifestStatus.COMPLETE
        assert manifest.pages_confirmed == 3
        assert manifest.keys == ('logs/a.parquet', 'logs/b.parquet', 'logs/c.parquet')
        assert len(manifest.observations) == 3
        assert [obj.key for obj in manifest.objects] == list(manifest.keys)

    def test_one_page_at_a_time_checkpointing(self, fake_session, fake_s3):
        _script_happy_path(fake_s3)
        wildcard = _file(fake_session, ENDPOINT)

        first = wildcard.resolve_wildcard_manifest(fetch_all=False)
        assert first.status is S3ManifestStatus.PARTIAL
        assert first.continuation_token == 'tok-2'
        assert first.pages_confirmed == 1

        second = wildcard.resolve_wildcard_manifest(first.to_json(), fetch_all=False)
        assert second.status is S3ManifestStatus.PARTIAL
        assert second.continuation_token == 'tok-3'
        assert second.pages_confirmed == 2

        third = wildcard.resolve_wildcard_manifest(second.to_json(), fetch_all=False)
        assert third.status is S3ManifestStatus.COMPLETE
        assert third.pages_confirmed == 3
        assert third.keys == (
            'logs/a.parquet', 'logs/b.parquet', 'logs/c.parquet',
            'logs/d.parquet', 'logs/e.parquet',
        )


class TestConflicts:
    def test_changed_object_is_kept_as_unconfirmed_conflict(self, fake_session, fake_s3):
        fake_s3.add(None, FakePage(
            objects=(_object('logs/a.parquet'), _object('logs/b.parquet')),
            next_token='tok-2',
            is_truncated=True,
        ))
        fake_s3.add('tok-2', FakePage(
            objects=(
                _object('logs/b.parquet', etag='"v1"', size=10),
                _object('logs/c.parquet'),
            ),
            next_token='tok-3',
            is_truncated=True,
        ))
        # Same key, different content marker.
        fake_s3.add('tok-3', FakePage(
            objects=(_object('logs/c.parquet', etag='"v2"', size=99),),
            is_truncated=False,
        ))

        manifest = _file(fake_session, ENDPOINT).resolve_wildcard_manifest()

        assert manifest.status is S3ManifestStatus.CONFLICT
        assert not manifest.is_complete
        assert manifest.keys == ('logs/a.parquet', 'logs/b.parquet', 'logs/c.parquet')
        assert [obj.key for obj in manifest.objects] == ['logs/a.parquet', 'logs/b.parquet']
        assert manifest.object_for_key('logs/c.parquet') is None

        conflict = {item.key: item for item in manifest.conflicts}['logs/c.parquet']
        assert len(conflict.observations) == 2
        assert {observation.etag for observation in conflict.observations} == {'"v1"', '"v2"'}
        assert conflict.version_confirmed is False
        assert 'no observation was chosen' in conflict.message

        # Conflict is not resolved by arrival order; a stable sorted view is
        # still available for diagnostics.
        restored = type(manifest).from_json(manifest.to_json())
        assert restored == manifest

    def test_missing_optional_metadata_is_not_a_conflict(self, fake_session, fake_s3):
        first = {
            'Key': 'logs/a.parquet',
            'ETag': '"v1"',
            'Size': 10,
        }
        second = {
            'Key': 'logs/a.parquet',
            'ETag': '"v1"',
            'Size': 10,
            'LastModified': '2026-11-22T00:00:00Z',
            'StorageClass': 'STANDARD',
        }
        fake_s3.add(None, {'IsTruncated': True, 'KeyCount': 1, 'Contents': [first],
                           'NextContinuationToken': 'tok-2'})
        fake_s3.add('tok-2', {'IsTruncated': False, 'KeyCount': 1, 'Contents': [second]})

        manifest = _file(fake_session, ENDPOINT).resolve_wildcard_manifest()

        assert manifest.status is S3ManifestStatus.COMPLETE
        assert manifest.conflicts == ()
        assert len(manifest.observations) == 1
        assert manifest.observations[0].last_modified == '2026-11-22T00:00:00+00:00'
        assert manifest.observations[0].etag == '"v1"'

    def test_legacy_resolve_wildcard_raises_on_conflict(self, fake_session, fake_s3):
        fake_s3.add(None, FakePage(
            objects=(_object('logs/a.parquet'), _object('logs/b.parquet')),
            next_token='tok-2',
            is_truncated=True,
        ))
        fake_s3.add('tok-2', FakePage(
            objects=(_object('logs/a.parquet', etag='"other"', size=77),),
            is_truncated=False,
        ))

        with pytest.raises(S3ManifestConflictError):
            _file(fake_session, ENDPOINT).resolve_wildcard()


class TestWrongEndpointAndCycles:
    def test_same_bucket_prefix_token_on_other_endpoint_is_rejected(self, fake_session):
        first_endpoint = 'http://127.0.0.1:9000'
        second_endpoint = 'http://127.0.0.1:9001'
        for endpoint in (first_endpoint, second_endpoint):
            client = fake_session.for_endpoint(endpoint)
            client.add(None, FakePage(
                objects=(_object('logs/a.parquet'),),
                next_token='shared-token',
                is_truncated=True,
            ))

        manifest = _file(fake_session, first_endpoint).resolve_wildcard_manifest(
            fetch_all=False
        )
        assert manifest.continuation_token == 'shared-token'

        stopped = _file(fake_session, second_endpoint).resolve_wildcard_manifest(
            manifest.to_json()
        )

        assert stopped.status is S3ManifestStatus.STOPPED
        assert stopped.stop_reason == STOP_ENDPOINT_MISMATCH
        assert not stopped.is_complete
        assert fake_session.for_endpoint(second_endpoint).requests == []
        with pytest.raises(S3ManifestStoppedError):
            _file(fake_session, second_endpoint).resolve_wildcard_complete(
                manifest.to_json()
            )

    def test_repeated_continuation_token_stops(self, fake_session, fake_s3):
        fake_s3.add(None, FakePage(
            objects=(_object('logs/a.parquet'),),
            next_token='loop',
            is_truncated=True,
        ))
        fake_s3.add('loop', FakePage(
            objects=(_object('logs/b.parquet'),),
            next_token='loop',
            is_truncated=True,
        ))

        manifest = _file(fake_session, ENDPOINT).resolve_wildcard_manifest()

        assert manifest.status is S3ManifestStatus.STOPPED
        assert manifest.stop_reason == STOP_CONTINUATION_CYCLE
        assert not manifest.is_complete
        assert manifest.has_more is False
        # Page b was not confirmed: the token could not advance, so its
        # objects are not part of the verified sequence.
        assert manifest.pages_confirmed == 1
        assert manifest.keys == ('logs/a.parquet',)
        assert fake_s3.tokens == [None, 'loop']

    def test_missing_next_token_is_not_completion(self, fake_session, fake_s3):
        fake_s3.add(None, FakePage(
            objects=(_object('logs/a.parquet'),),
            next_token=None,
            is_truncated=True,
        ))

        manifest = _file(fake_session, ENDPOINT).resolve_wildcard_manifest()

        assert manifest.status is S3ManifestStatus.STOPPED
        assert manifest.stop_reason == 'missing_continuation_token'
        assert not manifest.is_complete


class TestStableCompletion:
    def test_completion_is_sorted_and_not_a_snapshot(self, fake_session, fake_s3):
        fake_s3.add(None, FakePage(
            objects=(
                _object('logs/z.parquet'),
                _object('logs/a.parquet'),
                _object('logs/m.parquet'),
            ),
            next_token='tok-2',
            is_truncated=True,
        ))
        fake_s3.add('tok-2', FakePage(
            objects=(_object('logs/b.parquet'),),
            is_truncated=False,
        ))

        manifest = _file(fake_session, ENDPOINT).resolve_wildcard_manifest()

        assert manifest.is_complete
        assert manifest.keys == (
            'logs/a.parquet', 'logs/b.parquet', 'logs/m.parquet', 'logs/z.parquet'
        )
        assert manifest.completeness_basis == 'terminal_page_received'
        assert manifest.snapshot_consistent is False
        assert manifest.is_snapshot is False
        assert manifest.version_confirmed is True
        assert manifest.has_more is False

        # The complete credential can be read back without refetching.
        assert type(manifest).from_json(manifest.to_json()) == manifest

    def test_incomplete_error_for_partial(self, fake_session, fake_s3):
        fake_s3.add(None, FakePage(
            objects=(_object('logs/a.parquet'),),
            next_token='tok-2',
            is_truncated=True,
        ))
        fake_s3.add('tok-2', FakeFailure())

        with pytest.raises(S3ManifestIncompleteError):
            _file(fake_session, ENDPOINT).resolve_wildcard_complete()


class TestLegacyWildcard:
    def test_old_wildcard_still_returns_sorted_files(self, fake_session, fake_s3):
        _script_happy_path(fake_s3)

        files = _file(fake_session, ENDPOINT).resolve_wildcard()

        assert [f.key for f in files] == [
            'logs/a.parquet', 'logs/b.parquet', 'logs/c.parquet',
            'logs/d.parquet', 'logs/e.parquet',
        ]
        assert all(f.bucket == BUCKET for f in files)
        assert all(f.endpoint_url == ENDPOINT for f in files)

    def test_old_wildcard_is_empty_for_empty_prefix(self, fake_session, fake_s3):
        fake_s3.add(None, FakePage(objects=(), is_truncated=False))

        assert _file(fake_session, ENDPOINT).resolve_wildcard() == []

    def test_old_wildcard_fails_on_transient_error(self, fake_session, fake_s3):
        fake_s3.add(None, FakePage(
            objects=(_object('logs/a.parquet'),),
            next_token='tok-2',
            is_truncated=True,
        ))
        fake_s3.add('tok-2', FakeFailure())

        with pytest.raises(S3ManifestIncompleteError) as excinfo:
            _file(fake_session, ENDPOINT).resolve_wildcard()
        assert excinfo.value.manifest.continuation_token == 'tok-2'

    def test_iter_pages_yields_checkpoint_after_each_page(self, fake_session, fake_s3):
        _script_happy_path(fake_s3)

        manifest = _file(fake_session, ENDPOINT)
        lister = manifest._manifest_lister()
        states = list(lister.iter_pages())

        assert [state.pages_confirmed for state in states] == [0, 1, 2, 3]
        assert [state.continuation_token for state in states] == [
            None, 'tok-2', 'tok-3', None,
        ]
        assert states[-1].is_complete

    def test_convenience_listing(self, fake_session, fake_s3):
        _script_happy_path(fake_s3)

        manifest = list_s3_prefix(
            fake_session, BUCKET, PREFIX, endpoint_url=ENDPOINT
        )

        assert manifest.is_complete
        assert len(manifest.keys) == 5


def test_checkpoint_without_status_is_derived_and_validated():
    observation = {'key': 'logs/a.parquet', 'etag': '"v1"', 'size': 1}

    partial = S3Manifest.from_dict({
        'bucket': 'archive',
        'prefix': 'logs/',
        'continuation_token': 'tok',
        'observations': [observation],
    })
    assert partial.status is S3ManifestStatus.PARTIAL
    assert partial.pagination_complete is False
    assert partial.can_resume is True

    complete = S3Manifest.from_dict({
        'bucket': 'archive',
        'prefix': 'logs/',
        'pagination_complete': True,
        'observations': [observation],
    })
    assert complete.status is S3ManifestStatus.COMPLETE
    assert complete.continuation_token is None

    conflict = S3Manifest.from_dict({
        'bucket': 'archive',
        'prefix': 'logs/',
        'observations': [
            observation,
            {'key': 'logs/a.parquet', 'etag': '"v2"', 'size': 2},
        ],
        'conflicts': [{
            'key': 'logs/a.parquet',
            'observations': [
                observation,
                {'key': 'logs/a.parquet', 'etag': '"v2"', 'size': 2},
            ],
        }],
    })
    assert conflict.status is S3ManifestStatus.CONFLICT
    assert conflict.object_for_key('logs/a.parquet') is None


def test_observation_ignores_unknown_optional_fields():
    observation = S3ObjectObservation.from_s3_item({'Key': 'a', 'Size': '3'})
    assert observation.key == 'a'
    assert observation.size == 3
    assert observation.etag is None


class TestTaskScenario:
    """The scenario from the archive-scheduler brief, end to end."""

    def test_full_scenario(self, fake_session):
        endpoint = 'http://127.0.0.1:9100'
        other_endpoint = 'http://127.0.0.1:9101'
        client = fake_session.for_endpoint(endpoint)

        client.add(None, FakePage(
            objects=(_object('logs/first.parquet'), _object('logs/second.parquet')),
            next_token='page-2',
            is_truncated=True,
        ))
        client.add('page-2', FakeFailure('ServiceUnavailable'))
        client.add('page-2', FakePage(
            objects=(
                _object('logs/second.parquet'),
                _object('logs/third.parquet'),
            ),
            next_token='page-3',
            is_truncated=True,
        ))
        client.add('page-3', FakePage(
            objects=(_object('logs/third.parquet', etag='"changed"', size=99),),
            is_truncated=False,
        ))

        wildcard = _file(fake_session, endpoint)

        partial = wildcard.resolve_wildcard_manifest()
        assert partial.status is S3ManifestStatus.PARTIAL
        assert partial.pages_confirmed == 1
        assert partial.continuation_token == 'page-2'

        # The same credential on another endpoint, with identical bucket,
        # prefix and token, is rejected before any request is sent.
        fake_session.for_endpoint(other_endpoint).add(None, FakePage(
            objects=(_object('logs/first.parquet'),),
            next_token='page-2',
            is_truncated=True,
        ))
        wrong_endpoint = _file(fake_session, other_endpoint).resolve_wildcard_manifest(
            partial.to_json()
        )
        assert wrong_endpoint.status is S3ManifestStatus.STOPPED
        assert wrong_endpoint.stop_reason == STOP_ENDPOINT_MISMATCH
        assert fake_session.for_endpoint(other_endpoint).requests == []

        # Resume from the failed page; the service replays one object and
        # adds one new object.
        resumed = wildcard.resolve_wildcard_manifest(partial.to_json())
        assert resumed.status is S3ManifestStatus.CONFLICT
        assert resumed.is_complete is False
        assert resumed.pages_confirmed == 3
        assert resumed.keys == (
            'logs/first.parquet', 'logs/second.parquet', 'logs/third.parquet',
        )
        assert [obj.key for obj in resumed.objects] == [
            'logs/first.parquet', 'logs/second.parquet',
        ]
        conflict = resumed.conflicts[0]
        assert conflict.key == 'logs/third.parquet'
        assert conflict.version_confirmed is False
        assert resumed.version_confirmed is False
        assert {
            (observation.etag, observation.size)
            for observation in conflict.observations
        } == {('"v1"', 10), ('"changed"', 99)}

        # No arrival-order winner: reserialising and looking up the key still
        # yields no confirmed object.
        restored = S3Manifest.from_json(resumed.to_json())
        assert restored.object_for_key('logs/third.parquet') is None
        assert restored.conflicting_keys == ('logs/third.parquet',)

        # A clean copy without conflicting observations produces a complete,
        # stably ordered listing.
        clean = FakeS3Service().for_endpoint(endpoint)
        clean.add(None, FakePage(
            objects=(_object('logs/first.parquet'), _object('logs/second.parquet')),
            next_token='page-2',
            is_truncated=True,
        ))
        clean.add('page-2', FakePage(
            objects=(_object('logs/second.parquet'), _object('logs/third.parquet')),
            next_token='page-3',
            is_truncated=True,
        ))
        clean.add('page-3', FakePage(
            objects=(_object('logs/third.parquet'),),
            is_truncated=False,
        ))
        complete = _file(FakeS3ServiceAdapter(clean), endpoint).resolve_wildcard_complete()
        assert complete.status is S3ManifestStatus.COMPLETE
        assert complete.keys == (
            'logs/first.parquet', 'logs/second.parquet', 'logs/third.parquet',
        )
        assert complete.pages_confirmed == 3


class FakeS3ServiceAdapter:
    """Route all endpoints to one clean fake client."""

    def __init__(self, fake_client):
        self.fake_client = fake_client

    def client(self, service_name, endpoint_url=None, **kwargs):
        assert service_name == 's3'
        return self.fake_client
