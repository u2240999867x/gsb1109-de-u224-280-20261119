from __future__ import annotations

import argparse
import json

import pytest

from parquet_tools.commands import manifest as manifest_cli
from parquet_tools.s3_manifest import S3Manifest
from tests.fixtures.fake_s3 import FakeObject, FakePage, FakeS3Service


ENDPOINT = 'http://127.0.0.1:9000'


@pytest.fixture
def parser():
    return manifest_cli.configure_parser(argparse.ArgumentParser())


@pytest.fixture
def fake_session():
    return FakeS3Service()


def _object(key):
    return FakeObject(key=key, etag='"v1"', size=10)


def _script(fake_session):
    client = fake_session.for_endpoint(ENDPOINT)
    client.add(None, FakePage(
        objects=(_object('logs/a.parquet'), _object('logs/b.parquet')),
        next_token='tok-2',
        is_truncated=True,
    ))
    client.add('tok-2', FakePage(
        objects=(_object('logs/c.parquet'),),
        is_truncated=False,
    ))
    return client


def test_manifest_cli_complete(capsys, monkeypatch, parser, tmp_path, fake_session):
    _script(fake_session)
    from parquet_tools.commands.utils import S3ParquetFile

    monkeypatch.setattr(
        manifest_cli, 'to_parquet_file',
        lambda **kwargs: S3ParquetFile(
            aws_session=fake_session,
            bucket='archive',
            key='logs/*',
            endpoint_url=ENDPOINT,
        ),
        raising=True,
    )

    checkpoint = tmp_path / 'checkpoint.json'
    args = parser.parse_args([
        's3://archive/logs/*',
        '--endpoint-url', ENDPOINT,
        '--save-checkpoint', str(checkpoint),
    ])

    with pytest.raises(SystemExit) as excinfo:
        manifest_cli._cli(args)

    assert excinfo.value.code == manifest_cli.EXIT_COMPLETE
    output = json.loads(capsys.readouterr().out)
    assert output['status'] == 'complete'
    assert [entry['key'] for entry in output['observations']] == [
        'logs/a.parquet', 'logs/b.parquet', 'logs/c.parquet',
    ]
    saved = S3Manifest.load(checkpoint)
    assert saved.is_complete
    assert saved.keys == ('logs/a.parquet', 'logs/b.parquet', 'logs/c.parquet')


def test_manifest_text_output(capsys, monkeypatch, parser, fake_session):
    _script(fake_session)
    from parquet_tools.commands.utils import S3ParquetFile

    monkeypatch.setattr(
        manifest_cli, 'to_parquet_file',
        lambda **kwargs: S3ParquetFile(
            aws_session=fake_session,
            bucket='archive',
            key='logs/*',
            endpoint_url=ENDPOINT,
        ),
        raising=True,
    )
    args = parser.parse_args([
        's3://archive/logs/*', '--endpoint-url', ENDPOINT, '--format', 'text',
    ])
    with pytest.raises(SystemExit) as excinfo:
        manifest_cli._cli(args)
    assert excinfo.value.code == manifest_cli.EXIT_COMPLETE
    out = capsys.readouterr().out
    assert 'status: complete' in out
    assert 'snapshot_claim: False' in out
    assert 'object: logs/c.parquet' in out


def test_manifest_requires_wildcard(parser, monkeypatch):
    pf = type('P', (), {
        'is_wildcard': lambda self: False,
    })()
    monkeypatch.setattr(manifest_cli, 'to_parquet_file', lambda **kwargs: pf)
    args = parser.parse_args(['s3://archive/logs/a.parquet'])
    with pytest.raises(SystemExit) as excinfo:
        manifest_cli._cli(args)
    assert excinfo.value.code == manifest_cli.EXIT_STOPPED
