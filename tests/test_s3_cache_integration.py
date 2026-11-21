import threading

import pytest

from parquet_tools.commands.s3_cache import TaskObjectCache
from parquet_tools.commands.utils import S3ParquetFile

from tests.test_s3_cache import (
    KEY, V1_MARKERS, V2_MARKERS,
    parquet_bytes, read_markers,
)


@pytest.fixture
def spy_downloads(monkeypatch):
    calls = []
    original = S3ParquetFile._download_to

    def spy(self, localfile):
        calls.append({
            'endpoint': self.endpoint_url,
            'bucket': self.bucket,
            'key': self.key,
            'path': localfile,
        })
        return original(self, localfile)

    monkeypatch.setattr(S3ParquetFile, '_download_to', spy)
    return calls


def upload(session, bucket, key, data):
    session.resource('s3').meta.client.put_object(
        Bucket=bucket, Key=key, Body=data,
    )


class TestMotoIntegration:
    def test_concurrent_same_version_downloads_once(
            self, aws_session, aws_s3_bucket, spy_downloads):
        upload(aws_session, aws_s3_bucket, KEY, parquet_bytes(V1_MARKERS))
        errors = []

        with TaskObjectCache() as cache:
            pf = S3ParquetFile(
                aws_session=aws_session, bucket=aws_s3_bucket, key=KEY,
                endpoint_url=None, cache=cache,
            )
            results = []

            def reader():
                try:
                    with pf.get_local_path() as path:
                        results.append((path, read_markers(path)))
                except BaseException as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=reader) for _ in range(2)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(10)

            assert errors == []
            assert len(spy_downloads) == 1
            assert results[0][0] == results[1][0]
            assert {tuple(markers) for _, markers in results} == {
                tuple(V1_MARKERS),
            }

    def test_version_swap_same_length_isolates_readers(
            self, aws_session, aws_s3_bucket, spy_downloads):
        v1 = parquet_bytes(V1_MARKERS)
        v2 = parquet_bytes(V2_MARKERS)
        assert len(v1) == len(v2)
        upload(aws_session, aws_s3_bucket, KEY, v1)

        with TaskObjectCache() as cache:
            old_pf = S3ParquetFile(
                aws_session=aws_session, bucket=aws_s3_bucket, key=KEY,
                endpoint_url=None, cache=cache,
            )
            holder_entered = threading.Event()
            release_holder = threading.Event()
            holder_error = []

            def old_holder():
                try:
                    with old_pf.get_local_path() as old_path:
                        holder_entered.set()
                        release_holder.wait(timeout=10)
                        assert read_markers(old_path) == V1_MARKERS
                except BaseException as exc:
                    holder_error.append(exc)

            thread = threading.Thread(target=old_holder)
            thread.start()
            assert holder_entered.wait(timeout=10)

            upload(aws_session, aws_s3_bucket, KEY, v2)
            new_pf = S3ParquetFile(
                aws_session=aws_session, bucket=aws_s3_bucket, key=KEY,
                endpoint_url=None, cache=cache,
            )
            with new_pf.get_local_path() as new_path:
                assert read_markers(new_path) == V2_MARKERS
                identities = {s.identity for s in cache.snapshot()}
                assert len(identities) == 2
                assert all(s.exists for s in cache.snapshot())

            release_holder.set()
            thread.join(10)
            assert not thread.is_alive()
            assert holder_error == []
            assert len(spy_downloads) == 2

    def test_schema_then_data_reuse_single_download(
            self, aws_session, aws_s3_bucket, spy_downloads):
        import pyarrow.parquet as pq

        upload(aws_session, aws_s3_bucket, KEY, parquet_bytes(V1_MARKERS))

        with TaskObjectCache() as cache:
            pf = S3ParquetFile(
                aws_session=aws_session, bucket=aws_s3_bucket, key=KEY,
                endpoint_url=None, cache=cache,
            )
            with pf.get_local_path() as path:
                assert pq.ParquetFile(path).metadata.num_rows == 3
            with pf.get_local_path() as path:
                assert read_markers(path) == V1_MARKERS

            assert len(spy_downloads) == 1

    def test_versioned_bucket_uses_version_id_identity(
            self, aws_session, aws_s3_bucket, spy_downloads):
        client = aws_session.client('s3', region_name='us-east-1')
        client.put_bucket_versioning(
            Bucket=aws_s3_bucket,
            VersioningConfiguration={'Status': 'Enabled'},
        )
        upload(aws_session, aws_s3_bucket, KEY, parquet_bytes(V1_MARKERS))

        with TaskObjectCache() as cache:
            pf = S3ParquetFile(
                aws_session=aws_session, bucket=aws_s3_bucket, key=KEY,
                endpoint_url=None, cache=cache,
            )
            version = pf.head_version()
            assert version.version_id
            assert version.identity == f'versionId:{version.version_id}'

            with pf.get_local_path():
                pass
            with pf.get_local_path():
                pass
            assert len(spy_downloads) == 1
            assert cache.snapshot()[0].identity.startswith('versionId:')


class TestCommandWiring:
    def _s3_url(self, bucket, key):
        return f's3://{bucket}/{key}'

    def test_show_handler_uses_task_cache(
            self, aws_session, aws_s3_bucket, spy_downloads, capfd):
        from argparse import Namespace
        from parquet_tools.commands.show import _cli

        upload(aws_session, aws_s3_bucket, KEY, parquet_bytes(V1_MARKERS))
        args = Namespace(
            file=[self._s3_url(aws_s3_bucket, KEY)],
            format='psql', head=-1, columns=[],
            awsprofile=None, endpoint_url=None,
        )
        _cli(args)
        out, err = capfd.readouterr()
        assert 'aaa' in out and 'ccc' in out
        assert len(spy_downloads) == 1

    def test_show_handler_reuses_copy_for_same_object_twice(
            self, aws_session, aws_s3_bucket, spy_downloads):
        from argparse import Namespace
        from parquet_tools.commands.show import _cli

        upload(aws_session, aws_s3_bucket, KEY, parquet_bytes(V1_MARKERS))
        url = self._s3_url(aws_s3_bucket, KEY)
        args = Namespace(
            file=[url, url], format='psql', head=-1, columns=[],
            awsprofile=None, endpoint_url=None,
        )
        _cli(args)
        assert len(spy_downloads) == 1

    def test_csv_handler_uses_task_cache(
            self, aws_session, aws_s3_bucket, spy_downloads, capfd):
        from argparse import Namespace
        from parquet_tools.commands.csv import _cli

        upload(aws_session, aws_s3_bucket, KEY, parquet_bytes(V1_MARKERS))
        args = Namespace(
            file=[self._s3_url(aws_s3_bucket, KEY)],
            head=-1, columns=[], awsprofile=None, endpoint_url=None,
        )
        _cli(args)
        out, err = capfd.readouterr()
        assert 'marker' in out and 'bbb' in out
        assert len(spy_downloads) == 1

    def test_inspect_handler_uses_task_cache(
            self, aws_session, aws_s3_bucket, spy_downloads, capfd):
        from argparse import Namespace
        from parquet_tools.commands.inspect import _cli

        upload(aws_session, aws_s3_bucket, KEY, parquet_bytes(V1_MARKERS))
        args = Namespace(
            file=self._s3_url(aws_s3_bucket, KEY),
            detail=False, awsprofile=None, endpoint_url=None,
        )
        _cli(args)
        out, err = capfd.readouterr()
        assert 'num_rows: 3' in out
        assert len(spy_downloads) == 1

    def test_wildcard_resolution_propagates_task_cache(
            self, aws_session, aws_s3_bucket, spy_downloads, capfd):
        from argparse import Namespace
        from parquet_tools.commands.show import _cli

        upload(aws_session, aws_s3_bucket, 'a.parquet', parquet_bytes(V1_MARKERS))
        upload(aws_session, aws_s3_bucket, 'b.parquet', parquet_bytes(V2_MARKERS))
        args = Namespace(
            file=[f's3://{aws_s3_bucket}/*'],
            format='psql', head=-1, columns=[],
            awsprofile=None, endpoint_url=None,
        )
        _cli(args)
        out, err = capfd.readouterr()
        assert 'aaa' in out and 'zzz' in out
        assert {call['key'] for call in spy_downloads} == {'a.parquet', 'b.parquet'}
