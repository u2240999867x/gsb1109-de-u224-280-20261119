import json
import os
import subprocess
import sys

from tests.test_batch_index import make_volumes
from pywb.apps.batchcli import main
from pywb.indexer.cdxindexer import main as cdx_main
from pywb.manager.manager import main as manager_main


def test_cli_rounds(tmp_path, capsys):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    root = str(tmp_path / 'batch')

    assert main(['--root', root, 'add', '-s', 'r1', '-v', str(response_volume)]) == 0
    status = json.loads(capsys.readouterr().out)
    assert status['view'] == 'v-r1'
    assert len(status['waiting']) == 2

    assert main(['--root', root, 'add', '-s', 'r2', '-v', str(request_volume)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['lines'] == 3

    assert main(['--root', root, 'show']) == 0
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 3

    assert main(['--root', root, 'add', '-s', 'r3', '-v', str(conflict_volume)]) == 0
    conflicts = json.loads(capsys.readouterr().out)
    assert len(conflicts['conflict_ids']) == 1

    assert main(['--root', root, 'confirm', '-s', 'r4']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['lines'] == 2
    assert out['withdrawn_keys'] == [
        'com,example)/form?__wb_method=post&value=1&which=alpha']

    assert main(['--root', root, 'withdraw', '-s', 'r5', 'conflict.warc.gz']) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['lines'] == 3

    assert main(['--root', root, 'index', '-o', str(tmp_path / 'index.cdxj')]) == 0
    assert (tmp_path / 'index.cdxj').is_file()


def test_cdx_indexer_batch_matches_in_any_order(tmp_path):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    a = tmp_path / 'a.cdxj'
    b = tmp_path / 'b.cdxj'
    cdx_main(['-j', '--batch', '-o', str(a), str(response_volume),
              str(request_volume)])
    cdx_main(['-j', '--batch', '-o', str(b), str(request_volume),
              str(response_volume)])
    assert a.read_bytes() == b.read_bytes()


def test_manager_batch_alias(tmp_path, capsys):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    root = str(tmp_path / 'batch')
    try:
        manager_main(['batch', '--root', root, 'add', '-s', 'm1',
                      '-v', str(response_volume)])
    except SystemExit as se:
        assert se.code == 0
    out = json.loads(capsys.readouterr().out)
    assert out['view'] == 'v-m1'


def test_export_and_add_evidence(tmp_path, capsys):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    assert main(['--root', str(tmp_path / 'unused'), 'export',
                 '-v', str(response_volume),
                 '-v', str(request_volume)]) == 0
    exported = json.loads(capsys.readouterr().out)
    assert {item['volume'] for item in exported} == {
        'responses.warc.gz', 'requests.warc.gz'}

    evidence_file = tmp_path / 'evidence.json'
    evidence_file.write_text(json.dumps(exported))
    root = str(tmp_path / 'batch')
    assert main(['--root', root, 'add', '-s', 'e1',
                 '--evidence', str(evidence_file)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out['lines'] == 3


def test_show_and_index_on_empty_root(tmp_path, capsys):
    root = str(tmp_path / 'empty')
    assert main(['--root', root, 'status']) == 0
    capsys.readouterr()
    assert main(['--root', root, 'show']) == 0
    assert capsys.readouterr().out == ''
    assert main(['--root', root, 'index']) == 1
    assert 'no published view' in capsys.readouterr().err
