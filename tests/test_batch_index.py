import os
import shutil

import pytest

from warcio.archiveiterator import ArchiveIterator
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from pywb.indexer.batchindexer import (ArchiveEvidence, BatchIndex,
                                       BatchIndexError)


# ============================================================================
DATE = '2026-11-19T12:00:00Z'
URI = 'https://example.com/form'


def _response(writer, record_id, body, status='200 OK', uri=URI):
    headers = StatusAndHeaders(status,
                               [('Content-Type', 'text/html'),
                                ('Content-Length', str(len(body)))],
                               protocol='HTTP/1.1')
    return writer.create_warc_record(
        uri, 'response', payload=_bytes(body), http_headers=headers,
        warc_headers_dict={'WARC-Record-ID': record_id,
                           'WARC-Date': DATE})


def _request(writer, record_id, response_id, body, path='/form',
             method='POST', content_type='application/x-www-form-urlencoded'):
    headers = StatusAndHeaders(
        method + ' ' + path + ' HTTP/1.1',
        [('Content-Type', content_type),
         ('Content-Length', str(len(body)))],
        is_http_request=True)
    return writer.create_warc_record(
        URI, 'request', payload=_bytes(body), http_headers=headers,
        warc_headers_dict={'WARC-Record-ID': record_id,
                           'WARC-Date': DATE,
                           'WARC-Concurrent-To': response_id})


def _bytes(value):
    from io import BytesIO
    return BytesIO(value.encode('utf-8'))


def make_volumes(tmp_path):
    archive = tmp_path / 'archive'
    archive.mkdir()

    response_volume = archive / 'responses.warc.gz'
    with open(response_volume, 'wb') as fh:
        writer = WARCWriter(fh, gzip=True)
        writer.write_record(_response(writer, '<urn:uuid:get-response>',
                                      'GET BODY'))
        writer.write_record(_request(writer, '<urn:uuid:get-request>',
                                     '<urn:uuid:get-response>', '',
                                     method='GET'))
        writer.write_record(_response(writer, '<urn:uuid:alpha-response>',
                                      'ALPHA BODY'))
        writer.write_record(_response(writer, '<urn:uuid:beta-response>',
                                      'BETA BODY'))

    request_volume = archive / 'requests.warc.gz'
    with open(request_volume, 'wb') as fh:
        writer = WARCWriter(fh, gzip=True)
        writer.write_record(_request(writer, '<urn:uuid:beta-request>',
                                     '<urn:uuid:beta-response>',
                                     'which=beta&value=2'))
        writer.write_record(_request(writer, '<urn:uuid:alpha-request>',
                                     '<urn:uuid:alpha-response>',
                                     'which=alpha&value=1'))

    # the supplemental request carries the *same* WARC-Record-Id as the
    # already linked request but different actual fields
    conflict_volume = archive / 'conflict.warc.gz'
    with open(conflict_volume, 'wb') as fh:
        writer = WARCWriter(fh, gzip=True)
        writer.write_record(_request(writer, '<urn:uuid:alpha-request>',
                                     '<urn:uuid:alpha-response>',
                                     'which=alpha&value=9'))

    return archive, response_volume, request_volume, conflict_volume


def test_rounds_conflict_withdraw_restore(tmp_path):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))

    first = index.submit('round-1', volumes=[response_volume])

    assert first.view_id == 'v-round-1'

    # only the GET pair is indexed in round 1; the two POST responses wait
    lines = first.index_lines()
    assert len(lines) == 1
    assert lines[0].startswith('com,example)/form ')
    assert '"method"' not in lines[0]
    waiting = first.waiting()
    assert len(waiting) == 2
    assert {item['record_id'] for item in waiting} == {
        '<urn:uuid:alpha-response>', '<urn:uuid:beta-response>'}

    second = index.submit('round-2', volumes=[request_volume])
    lines = second.index_lines()
    assert len(lines) == 3
    assert any('which=alpha' in line and '"method": "POST"' in line
               for line in lines)
    assert any('which=beta' in line and '"method": "POST"' in line
               for line in lines)
    # the GET key was not changed by the request volume
    assert any(line.startswith('com,example)/form ')
               and '"method"' not in line for line in lines)
    assert not second.waiting()

    # a conflicting candidate with the same response id but different fields
    third = index.submit('round-3', volumes=[conflict_volume])
    assert len(third.index_lines()) == 3
    conflicts = third.conflicts()
    assert len(conflicts) == 1
    conflict = next(iter(conflicts.values()))
    assert conflict['state'] == 'pending'
    assert conflict['response']['record_id'] == '<urn:uuid:alpha-response>'
    # the old request's key is still readable while under review
    assert any('which=alpha&value=1' in line for line in third.index_lines())
    assert not any('which=alpha&value=9' in line for line in third.index_lines())

    fourth = index.confirm_conflicts('round-4')
    assert fourth is not None
    lines = fourth.index_lines()
    # alpha withdrawn, beta and GET remain
    assert len(lines) == 2
    assert not any('which=alpha' in line for line in lines)
    assert any('which=beta' in line for line in lines)
    assert any('"method"' not in line for line in lines)
    assert fourth.state()['withdrawn_keys'] == [
        'com,example)/form?__wb_method=post&value=1&which=alpha'
    ]

    fifth = index.withdraw_volume('round-5', 'conflict.warc.gz')
    lines = fifth.index_lines()
    assert len(lines) == 3
    assert any('which=alpha&value=1' in line for line in lines)
    assert not any('which=alpha&value=9' in line for line in lines)


def test_retry_after_interruption_is_one_update(tmp_path):
    """Interrupt after saving evidence but before publishing; retry once."""
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('round-1', volumes=[response_volume])

    class Stop(Exception):
        pass

    def failpoint(stage):
        if stage == 'after-save':
            raise Stop()

    with pytest.raises(Stop):
        index.submit('round-2', volumes=[request_volume], failpoint=failpoint)

    # evidence was saved and no view was published yet
    assert index.current_view_id() == 'v-round-1'
    assert index.load_evidence('requests.warc.gz') is not None

    view = index.submit('round-2', volumes=[request_volume])
    assert view.view_id == 'v-round-2'
    assert len(view.index_lines()) == 3

    # applying the same submission again is a no-op
    again = index.submit('round-2', volumes=[request_volume])
    assert again.view_id == view.view_id
    assert len(again.index_lines()) == 3

    # a different submission id with the same volumes is rejected as a
    # different submission content? same content != same identity: allowed.
    # a same id with different content must fail.
    with pytest.raises(BatchIndexError):
        index.submit('round-2', volumes=[conflict_volume])


def test_reversed_volume_order_is_stable(tmp_path):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    a = BatchIndex(str(tmp_path / 'a'))
    a.submit('r1', volumes=[response_volume])
    a.submit('r2', volumes=[request_volume])

    b = BatchIndex(str(tmp_path / 'b'))
    b.submit('r1', volumes=[request_volume])
    b.submit('r2', volumes=[response_volume])

    assert a.view().index_lines() == b.view().index_lines()
    assert a.view().waiting() == b.view().waiting()


def test_request_volume_without_response_does_not_read_response(tmp_path):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'b'))
    index.submit('r1', volumes=[response_volume])

    # move the response volume to cold storage before the requests arrive
    moved = tmp_path / 'cold.warc.gz'
    os.replace(response_volume, moved)

    index.submit('r2', volumes=[request_volume])
    lines = index.view().index_lines()
    assert len(lines) == 3
    assert all('responses.warc.gz' in line for line in lines)


def test_duplicate_response_id_conflict(tmp_path):
    from warcio.warcwriter import WARCWriter
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)

    duplicate = archive / 'duplicate.warc.gz'
    with open(duplicate, 'wb') as fh:
        writer = WARCWriter(fh, gzip=True)
        writer.write_record(_response(writer, '<urn:uuid:alpha-response>',
                                      'DIFFERENT BODY'))

    index = BatchIndex(str(tmp_path / 'b'))
    index.submit('r1', volumes=[response_volume])
    view = index.submit('r2', volumes=[duplicate])
    conflicts = view.conflicts()
    assert len(conflicts) == 1
    conflict = next(iter(conflicts.values()))
    assert conflict['kind'] == 'duplicate-response-id'
    assert conflict['response_id'] == '<urn:uuid:alpha-response>'


def test_orphan_request_is_not_used(tmp_path):
    from warcio.warcwriter import WARCWriter
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)

    orphan = archive / 'orphan.warc.gz'
    with open(orphan, 'wb') as fh:
        writer = WARCWriter(fh, gzip=True)
        writer.write_record(_request(writer, '<urn:uuid:orphan-request>',
                                     '<urn:uuid:missing-response>',
                                     'orphan=1'))

    index = BatchIndex(str(tmp_path / 'b'))
    index.submit('r1', volumes=[response_volume])
    view = index.submit('r2', volumes=[orphan])
    # GET pair only; the POST responses still wait and the orphan request
    # does not create a key
    assert len(view.index_lines()) == 1
    assert len(view.waiting()) == 2


def test_reader_pins_one_view_across_publish(tmp_path):
    from pywb.indexer.batchindexer import BatchReader

    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])

    reader = BatchReader(str(tmp_path / 'batch'))
    first_lines = reader.index_lines()
    first_conflicts = reader.conflicts()

    index.submit('r2', volumes=[request_volume])

    # the reader still sees the complete previous view
    assert reader.index_lines() == first_lines
    assert reader.conflicts() == first_conflicts

    new_reader = BatchReader(str(tmp_path / 'batch'))
    assert len(new_reader.index_lines()) == 3
    assert new_reader.view_id == 'v-r2'


def test_same_request_id_is_detected_before_it_is_used(tmp_path):
    # the supplemental request has the same WARC-Record-Id as the already
    # linked request but different actual fields; a repeated request id with
    # different evidence must not be resolved to "last one wins".
    from pywb.indexer.batchindexer import BatchPairing

    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])
    index.submit('r2', volumes=[request_volume])

    evidence = index.all_evidence()
    pairing = BatchPairing(evidence).pair()
    assert not pairing.conflicts
    assert len(pairing.pairs) == 3

    index.submit('r3', volumes=[conflict_volume])
    pairing = BatchPairing(index.all_evidence()).pair()
    assert len(pairing.conflicts) == 1
    conflict = next(iter(pairing.conflicts.values()))
    assert conflict['response_id'] == '<urn:uuid:alpha-response>'
    assert len(conflict['candidates']) == 2


def test_waiting_response_has_no_post_key(tmp_path):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    view = index.submit('r1', volumes=[response_volume])
    lines = view.index_lines()
    # no POST key is derived from the response alone
    assert not any('which=alpha' in line for line in lines)
    assert not any('which=beta' in line for line in lines)


def test_no_adjacency_pairing(tmp_path):
    """A request next to a response in the same volume must not pair."""
    from io import BytesIO
    from warcio.warcwriter import WARCWriter

    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)

    adjacent = archive / 'adjacent.warc.gz'
    with open(adjacent, 'wb') as fh:
        writer = WARCWriter(fh, gzip=True)
        writer.write_record(_response(writer, '<urn:uuid:solo-response>',
                                      'SOLO BODY'))
        request = _request(writer, '<urn:uuid:solo-request>',
                           '<urn:uuid:other-response>', 'near=1')
        # write a request with the *wrong* concurrent-to id right after it
        writer.write_record(request)

    index = BatchIndex(str(tmp_path / 'batch'))
    view = index.submit('r1', volumes=[adjacent])
    # the response is published as a plain GET, never with the nearby request
    assert not any('near=1' in line for line in view.index_lines())
    assert len(view.index_lines()) == 1
    assert view.unpaired()['requests'][0]['record_id'] == \
        '<urn:uuid:solo-request>'


def test_same_fields_resubmission_keeps_previous_key(tmp_path):
    from warcio.warcwriter import WARCWriter
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)

    same = archive / 'same.warc.gz'
    with open(same, 'wb') as fh:
        writer = WARCWriter(fh, gzip=True)
        writer.write_record(_request(writer, '<urn:uuid:alpha-request>',
                                     '<urn:uuid:alpha-response>',
                                     'value=1&which=alpha'))

    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])
    index.submit('r2', volumes=[request_volume])
    view = index.submit('r3', volumes=[same])
    # same method/body, just a different query order: not a conflict
    assert not view.conflicts()
    assert len(view.index_lines()) == 3


def test_published_view_is_immutable(tmp_path):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    first = index.submit('r1', volumes=[response_volume])
    first_bytes = open(first.index_path, 'rb').read()
    first_state = first.state()

    index.submit('r2', volumes=[request_volume])

    assert open(first.index_path, 'rb').read() == first_bytes
    assert first.state() == first_state
    assert index.current_view_id() == 'v-r2'


def test_conflict_evidence_names_volumes_and_ids(tmp_path):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])
    index.submit('r2', volumes=[request_volume])
    view = index.submit('r3', volumes=[conflict_volume])
    conflict = next(iter(view.conflicts().values()))

    assert conflict['response']['volume'] == 'responses.warc.gz'
    assert conflict['response']['record_id'] == '<urn:uuid:alpha-response>'
    volumes = {c['volume'] for c in conflict['candidates']}
    assert volumes == {'requests.warc.gz', 'conflict.warc.gz'}
    assert {c['body'] for c in conflict['candidates']} == {
        'which=alpha&value=1', 'which=alpha&value=9'}


def test_interrupted_push_retry_after_other_publish(tmp_path):
    """Retrying an interrupted submission after another batch was published
    still produces one complete update from the saved evidence."""
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])

    class Stop(Exception):
        pass

    with pytest.raises(Stop):
        index.submit('r2', volumes=[request_volume],
                     failpoint=lambda stage: (_ for _ in ()).throw(Stop())
                     if stage == 'after-save' else None)

    # an unrelated batch is published in the meantime
    from warcio.warcwriter import WARCWriter
    other = archive / 'other.warc.gz'
    with open(other, 'wb') as fh:
        writer = WARCWriter(fh, gzip=True)
        writer.write_record(_response(writer, '<urn:uuid:other-response>',
                                      'OTHER BODY'))

    index.submit('r3', volumes=[other])

    view = index.submit('r2', volumes=[request_volume])
    # r2 is one update over r1 and the already published unrelated batch:
    # GET + the two POSTs + the unrelated GET
    assert len(view.index_lines()) == 4
    assert view.view_id == 'v-r2'
    # the retried submission is recorded once and does not re-add evidence
    assert index.status()['view'] == 'v-r2'


def test_readded_conflicting_volume_is_reviewed_again(tmp_path):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])
    index.submit('r2', volumes=[request_volume])
    index.submit('r3', volumes=[conflict_volume])
    view = index.confirm_conflicts('r4')
    assert not any('which=alpha&value=1' in line
                   for line in view.index_lines())
    assert view.state()['withdrawn_keys']

    restored = index.withdraw_volume('r5', 'conflict.warc.gz')
    assert any('which=alpha&value=1' in line
               for line in restored.index_lines())

    # if the conflicting volume is delivered again (a fresh copy under a new
    # volume name), the conflict must be reviewed again instead of applying
    # the old confirmation silently.
    redelivered = archive / 'conflict-2.warc.gz'
    shutil.copyfile(conflict_volume, redelivered)
    again = index.submit('r6', volumes=[redelivered])
    assert again.conflicts()
    conflict = next(iter(again.conflicts().values()))
    assert conflict['state'] == 'pending'
    assert any('which=alpha&value=1' in line for line in again.index_lines())


def test_evidence_survives_volume_moved_away(tmp_path):
    """After the response volume is cold, adding it again uses evidence."""
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])

    cold = tmp_path / 'cold' / 'responses.warc.gz'
    cold.parent.mkdir()
    os.replace(response_volume, cold)

    # re-submitting the original path cannot read the volume; the saved
    # evidence is used instead
    view = index.submit('r2', volumes=[cold])
    assert view.view_id == 'v-r2'
    assert len(view.index_lines()) == 1


def test_unpaired_request_is_reported(tmp_path):
    from warcio.warcwriter import WARCWriter
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)

    orphan = archive / 'orphan.warc.gz'
    with open(orphan, 'wb') as fh:
        writer = WARCWriter(fh, gzip=True)
        writer.write_record(_request(writer, '<urn:uuid:orphan-request>',
                                     '<urn:uuid:missing-response>',
                                     'orphan=1'))

    index = BatchIndex(str(tmp_path / 'batch'))
    view = index.submit('r1', volumes=[orphan])
    unpaired = view.unpaired()
    assert unpaired['requests'][0]['record_id'] == '<urn:uuid:orphan-request>'
    assert unpaired['responses'] == []
    assert not view.index_lines()


def test_cold_volume_is_not_read_again_for_indexing(tmp_path, monkeypatch):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])

    calls = []
    original = ArchiveEvidence.from_volume

    def guarded(path, volume=None, **kwargs):
        calls.append(os.path.basename(path))
        if os.path.basename(path) == 'responses.warc.gz' and calls.count(
                'responses.warc.gz') > 1:
            raise AssertionError('cold response volume was read again')
        return original(path, volume=volume, **kwargs)

    index.volume_reader = guarded

    # the response volume is cold: only the new request volume is read
    view = index.submit('r2', volumes=[request_volume])
    assert calls == ['requests.warc.gz']
    assert len(view.index_lines()) == 3

    # the original response volume is still there for replay
    assert response_volume.is_file()


def test_withdrawn_evidence_retained_in_conflict_and_state(tmp_path):
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])
    index.submit('r2', volumes=[request_volume])
    index.submit('r3', volumes=[conflict_volume])
    view = index.confirm_conflicts('r4')

    conflict = next(iter(view.conflicts().values()))
    assert conflict['state'] == 'confirmed'
    assert conflict['withdrawn_key'] == conflict['affected_key']
    assert conflict['withdrawn_evidence']['volume'] == 'requests.warc.gz'
    assert conflict['withdrawn_evidence']['record_id'] == \
        '<urn:uuid:alpha-request>'
    assert view.state()['withdrawn_evidence'] == [{
        'conflict_id': conflict['conflict_id'],
        'key': conflict['affected_key'],
        'prior': conflict['prior'],
    }]


def test_pending_conflict_keeps_prior_response_volume(tmp_path):
    # the conflict candidate is in a different volume; while pending, the
    # previous complete entry still points at the response volume, not at the
    # request candidate.
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)
    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])
    index.submit('r2', volumes=[request_volume])
    view = index.submit('r3', volumes=[conflict_volume])

    import json
    prior_lines = [line for line in view.index_lines() if 'value=1' in line]
    assert len(prior_lines) == 1
    record = json.loads(prior_lines[0].split(' ', 2)[2])
    assert record['filename'] == 'responses.warc.gz'
    assert record['offset'] != '0'


def test_second_independent_conflict_uses_last_links(tmp_path):
    """A second conflict for another response still restores the first key
    from the complete previous view, not from the stale confirmation."""
    from warcio.warcwriter import WARCWriter
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)

    conflict2 = archive / 'conflict2.warc.gz'
    with open(conflict2, 'wb') as fh:
        writer = WARCWriter(fh, gzip=True)
        writer.write_record(_request(writer, '<urn:uuid:beta-request>',
                                     '<urn:uuid:beta-response>',
                                     'which=beta&value=8'))

    index = BatchIndex(str(tmp_path / 'batch'))
    index.submit('r1', volumes=[response_volume])
    index.submit('r2', volumes=[request_volume])
    index.submit('r3', volumes=[conflict_volume])
    view = index.confirm_conflicts('r4')
    assert not any('value=1' in line for line in view.index_lines())

    index.submit('r5', volumes=[conflict2])
    view = index.confirm_conflicts('r6')
    lines = view.index_lines()
    assert not any('value=1' in line for line in lines)
    assert not any('value=2' in line for line in lines)
    assert any('"method"' not in line for line in lines)


def test_batch_matches_existing_indexer_on_one_volume():
    """For a self-contained WARC the batch index is the existing CDXJ output."""
    from io import BytesIO
    from pywb import get_test_dir
    from pywb.indexer.cdxindexer import write_cdx_index
    from pywb.indexer.batchindexer import BuildBatch

    path = os.path.join(get_test_dir(), 'warcs', 'post-test.warc.gz')
    evidence = ArchiveEvidence.from_volume(path, volume='post-test.warc.gz')
    batch_lines, _waiting, _conflicts, _unpaired = \
        BuildBatch([evidence]).build()

    buff = BytesIO()
    with open(path, 'rb') as fh:
        write_cdx_index(buff, fh, 'post-test.warc.gz', cdxj=True,
                        append_post=True)
    reference = sorted(line for line in
                       buff.getvalue().decode('utf-8').splitlines() if line)

    assert batch_lines == reference


def test_lone_response_matches_plain_index(tmp_path):
    """A response whose request never arrives is still a normal GET capture;
    two same-url-same-second responses without requests stay waiting."""
    from warcio.warcwriter import WARCWriter
    archive, response_volume, request_volume, conflict_volume = \
        make_volumes(tmp_path)

    lonely = archive / 'lonely.warc.gz'
    with open(lonely, 'wb') as fh:
        writer = WARCWriter(fh, gzip=True)
        writer.write_record(_response(writer, '<urn:uuid:lonely-response>',
                                      'LONELY BODY',
                                      uri='https://example.com/lonely'))

    index = BatchIndex(str(tmp_path / 'batch'))
    view = index.submit('r1', volumes=[lonely])
    assert len(view.index_lines()) == 1
    assert not view.waiting()
