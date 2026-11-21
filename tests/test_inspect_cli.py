import argparse
import io

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from parquet_tools.commands import inspect as inspect_cmd
from parquet_tools.commands.utils import S3ParquetFile
from parquet_tools.parquet.credential import InspectionCredential


def _parquet_bytes(rows: int = 3) -> bytes:
    df = pd.DataFrame({
        'one': list(range(rows)),
        'two': [f'value-{i}' for i in range(rows)],
        'three': [bool(i % 2) for i in range(rows)],
    })
    sink = io.BytesIO()
    pq.write_table(pa.Table.from_pandas(df), sink)
    return sink.getvalue()


class _Session:
    def __init__(self):
        self.calls = []

    def client(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return _ForbiddenClient()


class _ForbiddenClient:
    class meta:
        region_name = 'us-east-1'

    def __getattr__(self, name):
        raise AssertionError(f'tail path must not call client.{name}')


def test_parser_defaults():
    parser = inspect_cmd.configure_parser(argparse.ArgumentParser())
    args = parser.parse_args(['s3://bucket/key.parquet'])
    assert args.credential is None
    assert args.save_credential is None
    assert args.full_download is False
    assert args.detail is False


def test_s3_tail_cli_uses_tail_reader(monkeypatch, tmp_path, capsys):
    data = _parquet_bytes()
    calls = {}

    def fake_to_parquet_file(file_exp, awsprofile, endpoint_url):
        return S3ParquetFile(
            aws_session=_Session(),
            bucket='bucket',
            key='key.parquet',
            endpoint_url='fake://endpoint',
        )

    class FakeResult:
        report = 'simple report'
        credential = InspectionCredential(
            endpoint=__import__('parquet_tools.parquet.credential', fromlist=['EndpointRef'])
            .EndpointRef.for_client('fake://endpoint', None),
            bucket='bucket',
            key='key.parquet',
        )

    def fake_inspect_tail(store, bucket, key, **kwargs):
        calls['args'] = (store, bucket, key, kwargs)
        return FakeResult()

    monkeypatch.setattr(inspect_cmd, 'to_parquet_file', fake_to_parquet_file)
    monkeypatch.setattr(inspect_cmd, 'inspect_tail', fake_inspect_tail)

    parser = inspect_cmd.configure_parser(argparse.ArgumentParser())
    args = parser.parse_args([
        '--save-credential', str(tmp_path / 'check.json'),
        's3://bucket/key.parquet',
    ])
    inspect_cmd._cli(args)

    captured = capsys.readouterr()
    assert captured.out.strip() == 'simple report'
    assert captured.err == ''
    store, bucket, key, kwargs = calls['args']
    assert (bucket, key) == ('bucket', 'key.parquet')
    assert kwargs['detail'] is False
    assert kwargs['credential'] is None
    assert (tmp_path / 'check.json').exists()


def test_s3_cli_reports_bad_credential(monkeypatch, tmp_path, capsys):
    def fake_to_parquet_file(file_exp, awsprofile, endpoint_url):
        return S3ParquetFile(
            aws_session=_Session(),
            bucket='bucket',
            key='key.parquet',
            endpoint_url='fake://endpoint',
        )

    monkeypatch.setattr(inspect_cmd, 'to_parquet_file', fake_to_parquet_file)
    bad = tmp_path / 'bad.json'
    bad.write_text('{"credential_version": 999}')

    parser = inspect_cmd.configure_parser(argparse.ArgumentParser())
    args = parser.parse_args(['--credential', str(bad), 's3://bucket/key.parquet'])
    inspect_cmd._cli(args)
    captured = capsys.readouterr()
    assert 'Cannot load credential' in captured.err


def test_explicit_full_download_uses_local_renderer(monkeypatch, tmp_path, capsys):
    data = _parquet_bytes()
    local = tmp_path / 'downloaded.parquet'
    local.write_bytes(data)

    class Pf(S3ParquetFile):
        def __init__(self):
            self.bucket = 'bucket'
            self.key = 'key.parquet'
            self.endpoint_url = 'fake://endpoint'
            self.aws_session = _Session()

        def is_wildcard(self):
            return False

        def get_local_path_with_version(self):
            import contextlib

            @contextlib.contextmanager
            def cm():
                yield str(local), {
                    'size': len(data),
                    'etag': '"abc"',
                    'version_id': 'v1',
                }
            return cm()

    monkeypatch.setattr(inspect_cmd, 'to_parquet_file', lambda **kw: Pf())
    credential_path = str(tmp_path / 'download.json')

    args = argparse.Namespace(
        file='s3://bucket/key.parquet',
        awsprofile=None,
        endpoint_url='fake://endpoint',
        detail=False,
        credential=None,
        save_credential=credential_path,
        full_download=True,
    )
    inspect_cmd._cli(args)
    captured = capsys.readouterr()
    assert 'file meta data' in captured.out
    assert 'num_rows: 3' in captured.out

    credential = InspectionCredential.load(credential_path)
    assert credential.identity.version_id == 'v1'
    assert credential.identity.size == len(data)
    assert credential.simple_report is None
    assert credential.ranges == []
