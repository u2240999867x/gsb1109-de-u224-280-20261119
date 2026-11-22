"""End-to-end checks: batch CDX index -> pywb warcserver -> WARC replay."""

import json
import os
import shutil

import pytest
import webtest

from pywb.indexer.batchindexer import BatchIndex
from pywb.warcserver.warcserver import WarcServer

from tests.test_batch_index import make_volumes


def _config(tmp_path, archive, index_file):
    indexes = tmp_path / 'indexes'
    indexes.mkdir()
    shutil.copy(index_file, indexes / 'batch.cdxj')
    config = tmp_path / 'config.yaml'
    config.write_text(
        'debug: true\n'
        'collections:\n'
        '    test:\n'
        '        index_paths: {indexes}/\n'
        '        archive_paths: {archive}/\n'.format(indexes=indexes,
                                                    archive=archive))
    return webtest.TestApp(WarcServer(config_file=str(config)))


def _post_request(body):
    return ('POST /form HTTP/1.1\r\n'
            'Content-Type: application/x-www-form-urlencoded\r\n'
            'Content-Length: {length}\r\n'
            'Host: example.com\r\n\r\n'
            '{body}').format(length=len(body), body=body)


def test_both_post_forms_replay(tmp_path):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])
    view = index.submit('r2', volumes=[request_volume])

    app = _config(tmp_path, archive, view.index_path)

    for body, expected in [('which=alpha&value=1', b'ALPHA BODY'),
                           ('which=beta&value=2', b'BETA BODY')]:
        res = app.post('/test/resource/postreq?url=https://example.com/form',
                       _post_request(body))
        assert res.status_code == 200
        assert expected in res.body
        cdx = json.loads(res.headers['Warcserver-Cdx'].split(' ', 2)[2])
        assert cdx['filename'] == 'responses.warc.gz'
        assert cdx['method'] == 'POST'


def test_withdrawn_form_stops_replaying(tmp_path):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])
    index.submit('r2', volumes=[request_volume])
    index.submit('r3', volumes=[conflict_volume])
    view = index.confirm_conflicts('r4')

    app = _config(tmp_path, archive, view.index_path)

    res = app.post('/test/resource/postreq?url=https://example.com/form',
                   _post_request('which=alpha&value=1'), status=404)
    assert res.status_code == 404

    res = app.post('/test/resource/postreq?url=https://example.com/form',
                   _post_request('which=beta&value=2'))
    assert b'BETA BODY' in res.body


def test_get_still_replays(tmp_path):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    view = index.submit('r1', volumes=[response_volume])
    app = _config(tmp_path, archive, view.index_path)
    res = app.get('/test/resource?url=https://example.com/form')
    assert res.status_code == 200
    assert b'GET BODY' in res.body


def test_missing_form_does_not_fuzzy_match_other_form(tmp_path):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])
    view = index.submit('r2', volumes=[request_volume])
    app = _config(tmp_path, archive, view.index_path)

    # a body that was never archived must not be served from the other POST
    res = app.post('/test/resource/postreq?url=https://example.com/form',
                   _post_request('which=alpha&value=3'), status=404)
    assert res.status_code == 404

    # but a matching body with reordered fields is the same query
    res = app.post('/test/resource/postreq?url=https://example.com/form',
                   _post_request('value=1&which=alpha'))
    assert b'ALPHA BODY' in res.body


def test_get_fuzzy_behavior_unchanged(tmp_path):
    """Ordinary GET fuzzy matching still works (query normalization)."""
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    view = index.submit('r1', volumes=[response_volume])
    app = _config(tmp_path, archive, view.index_path)

    res = app.get('/test/resource?url=https://example.com/form?_=12345')
    assert res.status_code == 200
    assert b'GET BODY' in res.body


def test_cdx_points_at_physical_response_record(tmp_path):
    import json

    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])
    view = index.submit('r2', volumes=[request_volume])

    lines = {json.loads(line.split(' ', 2)[2])['requestBody']: line
             for line in view.index_lines() if 'requestBody' in line}
    record = json.loads(lines['which=alpha&value=1'].split(' ', 2)[2])
    assert record['filename'] == 'responses.warc.gz'

    from pywb.warcserver.resource.blockrecordloader import BlockArcWarcRecordLoader
    loaded = BlockArcWarcRecordLoader().load(str(archive / record['filename']),
                                             int(record['offset']),
                                             int(record['length']))
    assert loaded is not None
    assert loaded.rec_type == 'response'
    assert b'ALPHA BODY' in loaded.raw_stream.read()


def test_withdrawn_key_does_not_fuzzy_fall_back(tmp_path):
    """After the affected key is withdrawn, the other form must not serve it."""
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])
    index.submit('r2', volumes=[request_volume])
    index.submit('r3', volumes=[conflict_volume])
    view = index.confirm_conflicts('r4')
    app = _config(tmp_path, archive, view.index_path)

    res = app.post('/test/resource/postreq?url=https://example.com/form',
                   _post_request('which=alpha&value=1'), status=404)
    assert res.status_code == 404

    # the surviving other POST and the GET stay available
    res = app.post('/test/resource/postreq?url=https://example.com/form',
                   _post_request('which=beta&value=2'))
    assert b'BETA BODY' in res.body
    res = app.get('/test/resource?url=https://example.com/form')
    assert b'GET BODY' in res.body
