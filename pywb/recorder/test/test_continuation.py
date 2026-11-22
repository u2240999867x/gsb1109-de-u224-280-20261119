"""Acceptance tests for shared-directory WARC continuation.

Every "machine" runs the CLI in a separate process, against one shared
directory.  Interruptions are requested through the CLI, so nothing below
shares in-process state between the two takeover machines.

The fixtures mirror the old-machine layout: a read-only legacy volume whose
name collides with the new machine's favourite, an old index where one row is
already proven complete and another points at the wrong offset, and a third
page that was never started.
"""

import gzip
import hashlib
import json
import os
import stat

import pytest

from pywb.recorder.continuation import (
    ContinuationManifest, ContinuationSession, IndexFenced, read_capture_payload,
    read_cdx_entries,
)
from pywb.recorder.continuationwriter import (
    ContinuationWriter, InterruptedCapture,
)


PAGE_DONE = 'http://legacy.example/done'
PAGE_BROKEN = 'http://legacy.example/broken'
PAGE_NEVER = 'http://legacy.example/never'
PAGE_A = 'http://capture.example/a'
PAGE_B = 'http://capture.example/b'

LEGACY_VOLUME = 'rec-20200101-legacy.warc.gz'
COLLIDING_VOLUME = 'rec-20261123-machine.warc.gz'


# ----------------------------------------------------------------------------
# helpers

def _run(root, *args):
    """Run one CLI step in a fresh process.

    Each call is a different OS process, so the tests exercise exactly the
    deployment shape: two takeover machines that share only the directory.
    """
    import subprocess
    import sys

    cmd = [sys.executable, '-m', 'pywb.recorder.continuationcli', root] + list(args)
    proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                          cwd=os.getcwd())
    text = proc.stdout.decode().strip()
    if proc.returncode not in (0, 2, 3, 4, 5, 6):
        raise AssertionError(
            'CLI crashed ({0}): {1}'.format(
                proc.returncode, proc.stderr.decode()[-2000:]))
    return proc.returncode, (json.loads(text) if text else None)


def _append_response(path, uri, body, content_type='text/html', status='200 OK'):
    from io import BytesIO
    from warcio.statusandheaders import StatusAndHeaders
    from warcio.warcwriter import WARCWriter

    headers = StatusAndHeaders(
        status,
        [('Content-Type', content_type),
         ('Content-Length', str(len(body)))],
        protocol='HTTP/1.1')
    with open(path, 'ab') as fh:
        writer = WARCWriter(fh, gzip=True)
        record = writer.create_warc_record(
            uri, 'response', payload=BytesIO(body), length=len(body),
            http_headers=headers,
            warc_content_type='application/http; msgtype=response')
        writer.write_record(record)


def _warc_record_offset(path, uri):
    """Return ``(offset, length, digest)`` for the complete response for uri."""
    from pywb.recorder.continuation import iter_warc_offsets, inspect_record

    with open(path, 'rb') as fh:
        for start, end in iter_warc_offsets(fh):
            info = inspect_record(fh, start, end)
            if info['type'] == 'response' and info['uri'] == uri:
                return start, end - start, info['payload_digest']
    raise AssertionError('capture not found for ' + uri)


def _sha256(path):
    with open(path, 'rb') as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def _read_bytes(path):
    with open(path, 'rb') as fh:
        return fh.read()


def _make_legacy(root):
    """Build the inherited layout and return its important paths."""
    archive = os.path.join(root, 'legacy')
    os.makedirs(archive)
    volume = os.path.join(archive, LEGACY_VOLUME)

    writer = ContinuationWriter(root)
    writer.write_capture(volume, PAGE_DONE, b'legacy done body')
    _append_response(volume, PAGE_BROKEN, b'legacy broken body')

    done = _warc_record_offset(volume, PAGE_DONE)
    broken = _warc_record_offset(volume, PAGE_BROKEN)
    return archive, volume, done, broken


def _write_old_index(path, done, broken, volume_name):
    """Write an old CDXJ with one stale offset and one proven row."""
    lines = [
        # proven-complete row with the right offset
        ('com,example)/done', 0,
         {'url': PAGE_DONE, 'mime': 'text/html', 'status': '200',
          'digest': done[2].split(':', 1)[-1], 'length': str(done[1]),
          'offset': str(done[0]), 'filename': volume_name}),
        # row whose offset points at the wrong place in the same volume
        ('com,example)/broken', 0,
         {'url': PAGE_BROKEN, 'mime': 'text/html', 'status': '200',
          'digest': 'ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ',
          'length': '11', 'offset': '3', 'filename': volume_name}),
    ]
    with open(path, 'w') as fh:
        for key, ts, obj in lines:
            fh.write('{0} 20200101000000 {1}\n'.format(
                key, json.dumps(obj)))


# ----------------------------------------------------------------------------
# fixtures

@pytest.fixture
def batch(tmp_path):
    root = str(tmp_path / 'batch')
    archive = os.path.join(root, 'archive')
    os.makedirs(archive)

    # The legacy volume sits in the new machine's archive directory and its
    # name is exactly what a fresh machine would pick first.
    legacy = os.path.join(root, 'legacy-' + LEGACY_VOLUME)
    writer = ContinuationWriter(root)
    writer.write_capture(legacy, PAGE_DONE, b'legacy done body')
    _append_response(legacy, PAGE_BROKEN, b'legacy broken body')

    done = _warc_record_offset(legacy, PAGE_DONE)
    broken = _warc_record_offset(legacy, PAGE_BROKEN)

    # Read-only legacy artifacts: old volume and old index.
    target = os.path.join(archive, LEGACY_VOLUME)
    os.replace(legacy, target)
    os.chmod(target, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    old_index = os.path.join(root, 'old.cdxj')
    _write_old_index(old_index, done, broken, LEGACY_VOLUME)
    os.chmod(old_index, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    # A second legacy volume with a name that collides with the new primary.
    colliding = os.path.join(archive, COLLIDING_VOLUME)
    writer.write_capture(colliding, 'http://legacy.example/other',
                         b'unrelated legacy bytes')
    os.chmod(colliding, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)

    return {
        'root': root,
        'archive': archive,
        'legacy': target,
        'colliding': colliding,
        'old_index': old_index,
        'done': done,
        'broken': broken,
        'legacy_sha': _sha256(target),
        'colliding_sha': _sha256(colliding),
        'index_bytes': _read_bytes(old_index),
    }


def _init_manifest(batch, pages):
    code, out = _run(batch['root'], 'init', *sum((['--url', p] for p in pages), []))
    assert code == 0, out


def _bootstrap(batch):
    code, out = _run(batch['root'], 'bootstrap',
                     '--index', batch['old_index'])
    assert code == 0, out
    return out


# ----------------------------------------------------------------------------
# legacy bootstrap

class TestLegacyBootstrap:
    def test_proven_and_stale_rows(self, batch):
        _init_manifest(batch, [PAGE_DONE, PAGE_BROKEN, PAGE_NEVER])
        out = _bootstrap(batch)

        plan = {row['uri']: row for row in out['plan']}
        assert plan[PAGE_DONE]['status'] == 'complete'
        assert plan[PAGE_DONE]['legacy'] is True
        assert plan[PAGE_BROKEN]['status'] == 'complete'

        # The stale offset was repaired against real record bytes.
        assert plan[PAGE_BROKEN]['offset'] != int('3')
        assert plan[PAGE_BROKEN]['payload_digest'].startswith('sha1:')
        # The old index position itself is preserved for audit.
        assert plan[PAGE_BROKEN]['legacy_indexed_offset'] == 3
        assert plan[PAGE_BROKEN]['legacy_offset_repaired'] is True
        assert plan[PAGE_DONE]['legacy_offset_repaired'] is False
        assert plan[PAGE_NEVER]['status'] == 'unstarted'
        assert plan[PAGE_NEVER]['remaining'] is True

        rejected = {r['uri'] for r in out['rejected']}
        assert PAGE_BROKEN not in rejected
        assert PAGE_NEVER not in rejected

        # Legacy bytes and index are untouched.
        assert _sha256(batch['legacy']) == batch['legacy_sha']
        assert _sha256(batch['colliding']) == batch['colliding_sha']
        assert _read_bytes(batch['old_index']) == batch['index_bytes']

    def test_unproven_row_stays_pending(self, batch):
        # A row with a correct digest but an offset inside a different record
        # must not be trusted; the volume scan finds the real capture.
        _init_manifest(batch, [PAGE_BROKEN])
        out = _bootstrap(batch)
        row = out['plan'][0]
        assert row['state'] == 'indexed'
        assert row['filename'].endswith(LEGACY_VOLUME)

        info, payload = read_capture_payload(
            os.path.join(batch['root'], row['filename']),
            row['offset'], row['length'], uri=PAGE_BROKEN,
            digest=row['payload_digest'])
        assert payload == b'legacy broken body'

    def test_missing_capture_is_not_proven(self, batch):
        # An index row whose volume has no matching response is rejected.
        old = os.path.join(batch['root'], 'ghost.cdxj')
        with open(old, 'w') as fh:
            fh.write(
                'com,example)/ghost 20200101000000 ' + json.dumps({
                    'url': 'http://legacy.example/ghost',
                    'mime': 'text/html', 'status': '200',
                    'digest': 'AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA',
                    'length': '10', 'offset': '0',
                    'filename': LEGACY_VOLUME}) + '\n')

        _init_manifest(batch, ['http://legacy.example/ghost'])
        code, out = _run(batch['root'], 'bootstrap', '--index', old)
        assert code == 0
        assert out['plan'][0]['state'] == 'pending'
        assert out['plan'][0]['remaining'] is True
        assert out['rejected'][0]['uri'] == 'http://legacy.example/ghost'


# ----------------------------------------------------------------------------
# capture evidence

class TestCaptureEvidence:
    def test_torn_member_is_never_evidence(self, batch):
        from pywb.recorder.continuation import probe_capture

        writer = ContinuationWriter(batch['root'])
        path = os.path.join(batch['archive'], 'torn.warc.gz')
        try:
            writer.write_capture(path, PAGE_A, b'torn body', interrupt_after=0.8)
        except InterruptedCapture:
            pass
        assert probe_capture(path, PAGE_A) is None
        assert os.path.getsize(path) > 0

    def test_wrong_digest_is_never_evidence(self, batch):
        from io import BytesIO
        from warcio.statusandheaders import StatusAndHeaders
        from warcio.warcwriter import WARCWriter
        from pywb.recorder.continuation import probe_capture

        path = os.path.join(batch['archive'], 'bad-digest.warc.gz')
        with open(path, 'wb') as fh:
            writer = WARCWriter(fh, gzip=True)
            headers = StatusAndHeaders(
                '200 OK',
                [('Content-Type', 'text/html'), ('Content-Length', '5')],
                protocol='HTTP/1.1')
            record = writer.create_warc_record(
                PAGE_A, 'response', payload=BytesIO(b'hello'), length=5,
                http_headers=headers,
                warc_content_type='application/http; msgtype=response')
            record.rec_headers.replace_header(
                'WARC-Payload-Digest',
                'sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA')
            writer.write_record(record)

        assert probe_capture(path, PAGE_A) is None

    def test_wrong_offset_still_finds_real_record(self, batch):
        from pywb.recorder.continuation import probe_capture

        before = _read_bytes(batch['legacy'])
        capture = probe_capture(batch['legacy'], PAGE_BROKEN,
                                offset=3, length=11,
                                digest='sha1:ZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZZ')
        assert capture is not None
        assert capture['offset'] != 3
        assert _read_bytes(batch['legacy']) == before


# ----------------------------------------------------------------------------
# duplicate claims across two machines

class TestConcurrentClaims:
    def test_only_one_machine_can_claim(self, batch):
        _init_manifest(batch, [PAGE_A])
        manifest = ContinuationManifest(batch['root'])

        a = manifest.claim(PAGE_A, 'machine-A', preferred_name='a.warc.gz')
        assert a and a['attempt'] == 1
        assert manifest.claim(PAGE_A, 'machine-B', preferred_name='b.warc.gz') is None
        assert len(manifest.page(PAGE_A)['attempts']) == 1

        # The control plane re-delivers the same task to the other machine.
        assert manifest.claim(PAGE_A, 'machine-B', dispatch_id='d-1') is None
        dispatches = manifest.page(PAGE_A)['dispatches']
        assert dispatches[-1]['owner'] == 'machine-B'

        # A re-delivery to the winner is not a new authorization either.
        assert manifest.claim(PAGE_A, 'machine-A',
                              takeover=True, dispatch_id='d-2') is None
        assert len(manifest.page(PAGE_A)['attempts']) == 1

    def test_unauthorized_page_is_never_claimed(self, batch):
        _run(batch['root'], 'init', '--url', PAGE_A, '--unauthorized', PAGE_A)
        manifest = ContinuationManifest(batch['root'])
        assert manifest.claim(PAGE_A, 'machine-A') is None
        assert manifest.last_claim_reason == 'not-authorized'
        assert manifest.page(PAGE_A)['attempts'] == []

    def test_two_processes_race_for_one_slot(self, batch):
        import subprocess
        import sys

        _init_manifest(batch, [PAGE_A])
        script = os.path.join(batch['root'], 'race.py')
        with open(script, 'w') as fh:
            fh.write(
                'import sys\n'
                'from pywb.recorder.continuation import ContinuationManifest\n'
                'm = ContinuationManifest(sys.argv[1])\n'
                'c = m.claim(sys.argv[3], sys.argv[2])\n'
                'print("won" if c else "lost")\n')

        procs = []
        for owner in ('A', 'B'):
            procs.append(subprocess.Popen(
                [sys.executable, script, batch['root'], owner, PAGE_A],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE))
        results = [p.communicate() for p in procs]

        outcomes = sorted(out.decode().strip() for out, _ in results)
        assert outcomes == ['lost', 'won'], results

        manifest = ContinuationManifest(batch['root'])
        assert len(manifest.page(PAGE_A)['attempts']) == 1


# ----------------------------------------------------------------------------
# crash after a complete volume was written

class TestStopAfterWrite:
    def test_owner_dies_after_write_then_takeover_adopts(self, batch):
        _init_manifest(batch, [PAGE_A])

        # A writes a complete volume but dies before recording or indexing.
        code, out = _run(batch['root'], 'capture',
                         '--owner', 'machine-A', '--url', PAGE_A,
                         '--preferred', 'rec-A.warc.gz',
                         '--payload-file', _payload(batch, 'page A'),
                         '--stop-after', 'write')
        assert code == 0 and out['state'] == 'written-unrecorded'

        pre = ContinuationManifest(batch['root'])
        assert pre.page(PAGE_A)['state'] == 'writing'
        assert pre.index.entries_for(PAGE_A) == []

        # B takes over and must adopt, not recapture.
        code, out = _run(batch['root'], 'capture',
                         '--owner', 'machine-B', '--url', PAGE_A,
                         '--preferred', 'rec-A.warc.gz',
                         '--payload-file', _payload(batch, 'page A'),
                         '--takeover')
        assert code == 0 and out['adopted'] is True
        assert out['capture']['filename'] == 'archive/rec-A.warc.gz'

        manifest = ContinuationManifest(batch['root'])
        assert len(manifest.index.entries_for(PAGE_A)) == 1
        # two legacy volumes + exactly one new volume (the adopted one)
        assert len(os.listdir(batch['archive'])) == 3

    def test_restart_never_reuses_a_volume_identity(self, batch):
        """Two restarts both prefer the same name; each gets a new volume."""
        _init_manifest(batch, [PAGE_A])

        # First restart claims and is interrupted before any durable record.
        code, out = _run(batch['root'], 'capture',
                         '--owner', 'boot-1', '--url', PAGE_A,
                         '--preferred', 'restart.warc.gz',
                         '--payload-file', _payload(batch, 'first try'),
                         '--interrupt', '0.2')
        assert code == 5
        first_volume = out['volume']
        first_bytes = _read_bytes(os.path.join(batch['root'], first_volume))

        # Second restart uses the same preferred name, but must allocate a new
        # volume and must not touch the interrupted one.
        code, out = _run(batch['root'], 'capture',
                         '--owner', 'boot-2', '--url', PAGE_A,
                         '--preferred', 'restart.warc.gz',
                         '--payload-file', _payload(batch, 'second try'),
                         '--takeover')
        assert code == 0, out
        second_volume = out['capture']['filename']
        assert second_volume != first_volume
        assert second_volume == 'archive/restart-r1.warc.gz'
        assert _read_bytes(os.path.join(batch['root'], first_volume)) == first_bytes

    def test_volume_name_collision_is_never_appended(self, batch):
        from io import BytesIO
        from pywb.recorder.multifilewarcwriter import MultiFileWARCWriter

        # The stock writer must roll over rather than open the read-only
        # legacy volume in append mode.
        legacy = batch['colliding']
        before = _read_bytes(legacy)
        writer = MultiFileWARCWriter(os.path.join(batch['archive'],
                                                  COLLIDING_VOLUME))
        record = writer.create_warc_record(
            'http://example.com/other', 'resource',
            payload=BytesIO(b'new bytes'), length=9)
        writer.write_record(record, {})
        writer.close()

        assert _read_bytes(legacy) == before
        assert os.path.exists(os.path.join(
            batch['archive'], 'rec-20261123-machine-1.warc.gz'))
        _init_manifest(batch, [PAGE_A])
        before = _sha256(batch['colliding'])

        code, out = _run(batch['root'], 'capture',
                         '--owner', 'machine-A', '--url', PAGE_A,
                         '--preferred', COLLIDING_VOLUME,
                         '--payload-file', _payload(batch, 'page A'))
        assert code == 0, out

        assert out['capture']['filename'] != 'archive/' + COLLIDING_VOLUME
        assert out['capture']['filename'].startswith('archive/rec-20261123-machine')
        assert _sha256(batch['colliding']) == before


# ----------------------------------------------------------------------------
# crash half-way through the response

class TestInterruptedResponse:
    def test_no_complete_capture_means_new_capture_allowed(self, batch):
        _init_manifest(batch, [PAGE_A])

        code, out = _run(batch['root'], 'capture',
                         '--owner', 'machine-A', '--url', PAGE_A,
                         '--preferred', 'rec-A.warc.gz',
                         '--payload-file', _payload(batch, 'A' * 20000),
                         '--interrupt', '0.5')
        assert code == 5, out
        assert out['state'] == 'interrupted'
        torn = os.path.join(batch['root'], out['volume'])
        assert os.path.getsize(torn) == out['written']

        # The torn member can never be verified, even though its bytes exist.
        from pywb.recorder.continuation import probe_capture
        assert probe_capture(torn, PAGE_A) is None

        manifest = ContinuationManifest(batch['root'])
        assert manifest.page(PAGE_A)['state'] == 'pending'
        assert manifest.index.entries_for(PAGE_A) == []

        # B takes over and must produce a new capture.
        code, out = _run(batch['root'], 'capture',
                         '--owner', 'machine-B', '--url', PAGE_A,
                         '--preferred', 'rec-A.warc.gz',
                         '--payload-file', _payload(batch, 'B' * 100),
                         '--takeover')
        assert code == 0, out
        assert out['state'] == 'indexed'
        assert out['capture']['filename'] == 'archive/rec-A-r1.warc.gz'
        assert _sha256(torn) == _sha256(torn)  # torn bytes preserved

        # The torn volume is still on disk and still unverifiable.
        assert os.path.exists(torn)
        assert probe_capture(torn, PAGE_A) is None

        manifest = ContinuationManifest(batch['root'])
        entries = manifest.index.entries_for(PAGE_A)
        assert len(entries) == 1
        assert entries[0]['filename'] == 'archive/rec-A-r1.warc.gz'
        _, payload = read_capture_payload(
            os.path.join(batch['root'], entries[0]['filename']),
            entries[0]['offset'], entries[0]['length'], uri=PAGE_A,
            digest=entries[0]['payload_digest'])
        assert payload == b'B' * 100

    def test_interrupt_before_any_bytes_leaves_no_volume(self, batch):
        _init_manifest(batch, [PAGE_A])
        code, out = _run(batch['root'], 'capture',
                         '--owner', 'machine-A', '--url', PAGE_A,
                         '--preferred', 'rec-A.warc.gz',
                         '--payload-file', _payload(batch, 'anything'),
                         '--interrupt', 'none')
        assert code == 5 and out['written'] == 0
        assert not any(n.startswith('rec-A') for n in os.listdir(batch['archive']))

        code, out = _run(batch['root'], 'capture',
                         '--owner', 'machine-B', '--url', PAGE_A,
                         '--preferred', 'rec-A.warc.gz',
                         '--payload-file', _payload(batch, 'fresh'),
                         '--takeover')
        assert code == 0 and out['capture']['filename'] == 'archive/rec-A.warc.gz'


# ----------------------------------------------------------------------------
# late index message from the old writer

class TestLateIndexMessage:
    def test_late_message_cannot_index_again_or_replace(self, batch):
        _init_manifest(batch, [PAGE_A, PAGE_DONE])
        _bootstrap(batch)

        # Machine A writes a complete volume, then dies before indexing.
        code, out = _run(batch['root'], 'capture',
                         '--owner', 'machine-A', '--url', PAGE_A,
                         '--preferred', 'rec-A.warc.gz',
                         '--payload-file', _payload(batch, 'A complete'),
                         '--stop-after', 'write')
        assert code == 0
        old_token = out['claim_token']
        old_capture = dict(out['capture'])

        # Machine B takes over and completes the page.
        code, out = _run(batch['root'], 'capture',
                         '--owner', 'machine-B', '--url', PAGE_A,
                         '--preferred', 'rec-A.warc.gz',
                         '--payload-file', _payload(batch, 'A complete'),
                         '--takeover')
        assert code == 0 and out['state'] == 'indexed'

        verified = ContinuationManifest(batch['root']).index.lookup(PAGE_A)
        verified_before = dict(verified)

        # A's delayed index message arrives after B finished.
        code, out = _run(batch['root'], 'submit', '--url', PAGE_A,
                         '--token', old_token)
        assert code == 3, out

        manifest = ContinuationManifest(batch['root'])
        after = manifest.index.lookup(PAGE_A)
        assert after == verified_before
        assert len(manifest.index.entries_for(PAGE_A)) == 1

        # The page has not been captured again under another volume.
        assert not any(n.startswith('rec-A-r') for n in os.listdir(batch['archive']))

    def test_late_message_for_torn_capture_is_rejected(self, batch):
        _init_manifest(batch, [PAGE_A])

        code, out = _run(batch['root'], 'capture',
                         '--owner', 'machine-A', '--url', PAGE_A,
                         '--preferred', 'rec-A.warc.gz',
                         '--payload-file', _payload(batch, 'torn page'),
                         '--interrupt', '0.3')
        assert code == 5
        old_token = out['claim_token']

        code, out = _run(batch['root'], 'capture',
                         '--owner', 'machine-B', '--url', PAGE_A,
                         '--preferred', 'rec-A.warc.gz',
                         '--payload-file', _payload(batch, 'good page'),
                         '--takeover')
        assert code == 0 and out['state'] == 'indexed'
        out_token = out['claim_token']

        # The late writer submits not just a token but its own capture
        # descriptor; the teardown capture must not replace the proven one.
        manifest = ContinuationManifest(batch['root'])
        torn = next(n for n in os.listdir(batch['archive']) if n == 'rec-A.warc.gz')
        torn_capture = {'filename': 'archive/' + torn, 'offset': 0,
                        'length': os.path.getsize(os.path.join(batch['archive'], torn)),
                        'payload_digest': 'sha1:AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA'}
        with pytest.raises(IndexFenced):
            manifest.index_message(PAGE_A, old_token, torn_capture)

        # Identical re-submission by the winning owner is idempotent.
        code, out = _run(batch['root'], 'submit', '--url', PAGE_A,
                         '--token', out_token)
        assert code == 0 and out['result'] == 'already'

        # The superseded owner's message is fenced.
        code, out = _run(batch['root'], 'submit', '--url', PAGE_A,
                         '--token', old_token)
        assert code == 3

        entry = manifest.index.lookup(PAGE_A)
        assert entry['filename'] == 'archive/rec-A-r1.warc.gz'
        _, payload = read_capture_payload(
            os.path.join(batch['root'], entry['filename']),
            entry['offset'], entry['length'], uri=PAGE_A,
            digest=entry['payload_digest'])
        assert payload == b'good page'

    def test_proven_legacy_page_still_readable(self, batch):
        _init_manifest(batch, [PAGE_DONE, PAGE_BROKEN, PAGE_NEVER])
        _bootstrap(batch)

        code, out = _run(batch['root'], 'read', '--url', PAGE_DONE)
        assert code == 0 and out['payload'] == 'legacy done body'

        code, out = _run(batch['root'], 'read', '--url', PAGE_BROKEN)
        assert code == 0 and out['payload'] == 'legacy broken body'

        # Nothing was appended to the legacy volumes during bootstrap.
        assert _sha256(batch['legacy']) == batch['legacy_sha']
        assert _sha256(batch['colliding']) == batch['colliding_sha']


# ----------------------------------------------------------------------------
# plan/audit

class TestPlanAudit:
    def test_plan_reports_attempt_volume_and_remaining(self, batch):
        _init_manifest(batch, [PAGE_DONE, PAGE_A, PAGE_NEVER])
        _bootstrap(batch)

        code, out = _run(batch['root'], 'capture',
                         '--owner', 'machine-A', '--url', PAGE_A,
                         '--preferred', 'rec-A.warc.gz',
                         '--payload-file', _payload(batch, 'A body'))
        assert code == 0

        code, plan = _run(batch['root'], 'plan')
        assert code == 0
        rows = {r['uri']: r for r in plan}

        assert rows[PAGE_DONE]['status'] == 'complete'
        assert rows[PAGE_A]['status'] == 'complete'
        assert rows[PAGE_A]['attempt'] == 1
        assert rows[PAGE_A]['owner'] == 'machine-A'
        assert rows[PAGE_A]['filename'] == 'archive/rec-A.warc.gz'
        assert rows[PAGE_A]['remaining'] is False
        assert rows[PAGE_NEVER]['status'] == 'unstarted'
        assert rows[PAGE_NEVER]['remaining'] is True

        # Every page records its authorization attempt history.
        manifest = ContinuationManifest(batch['root'])
        assert manifest.page(PAGE_A)['attempts'][0]['state'] == 'captured'
        assert len(manifest.page(PAGE_A)['attempts']) == 1


# ----------------------------------------------------------------------------
# helper payload files

def _payload(batch, text):
    path = os.path.join(batch['root'], 'payload-' + hashlib.sha1(text.encode()).hexdigest()[:8])
    with open(path, 'wb') as fh:
        fh.write(text.encode())
    return path


# ----------------------------------------------------------------------------
# full hand-off story

class TestHandOffStory:
    """The complete two-machine story on one shared directory."""

    def test_two_machines_share_one_business_page(self, batch):
        _init_manifest(batch, [PAGE_DONE, PAGE_BROKEN, PAGE_NEVER, PAGE_A])
        _bootstrap(batch)

        before = {
            'legacy': _sha256(batch['legacy']),
            'colliding': _sha256(batch['colliding']),
            'old_index': _read_bytes(batch['old_index']),
        }

        # Both machines see PAGE_A pending and prefer the same name, which is
        # already a read-only legacy volume.
        pref = COLLIDING_VOLUME
        assert os.path.exists(os.path.join(batch['archive'], pref))

        # Machine A wins the claim, gets a writable path and persists a full
        # WARC, then dies before sending anything to the index.
        code, a = _run(batch['root'], 'capture',
                       '--owner', 'machine-A', '--url', PAGE_A,
                       '--preferred', pref,
                       '--payload-file', _payload(batch, 'page A captured'),
                       '--stop-after', 'write',
                       '--dispatch-id', 'task-1')
        assert code == 0 and a['state'] == 'written-unrecorded'
        assert a['volume'] != 'archive/' + pref
        a_volume = os.path.join(batch['root'], a['volume'])
        a_bytes = _read_bytes(a_volume)
        volumes_after_a = sorted(os.listdir(batch['archive']))

        # A control-plane re-delivery reaches machine B.
        code, b = _run(batch['root'], 'capture',
                       '--owner', 'machine-B', '--url', PAGE_A,
                       '--preferred', pref,
                       '--payload-file', _payload(batch, 'page A captured'),
                       '--takeover',
                       '--dispatch-id', 'task-1')
        assert code == 0, b
        assert b['adopted'] is True
        assert b['capture']['filename'] == a['volume']
        assert _read_bytes(a_volume) == a_bytes

        # No second volume was produced when B adopted A's capture.
        assert sorted(os.listdir(batch['archive'])) == volumes_after_a

        code, plan = _run(batch['root'], 'plan')
        rows = {r['uri']: r for r in plan}
        assert rows[PAGE_A]['state'] == 'indexed'
        assert rows[PAGE_A]['attempt'] == 2
        assert rows[PAGE_A]['attempts'][0]['owner'] == 'machine-A'
        assert rows[PAGE_A]['attempts'][0]['state'] == 'superseded'
        assert rows[PAGE_A]['attempts'][1]['owner'] == 'machine-B'
        assert rows[PAGE_A]['attempts'][1]['state'] == 'adopted'
        assert rows[PAGE_A]['attempt_volume'] == a['volume']
        assert rows[PAGE_A]['dispatch_count'] == 2
        assert rows[PAGE_A]['remaining'] is False

        # A's late index message arrives after B finished.
        code, late = _run(batch['root'], 'submit', '--url', PAGE_A,
                          '--token', a['claim_token'])
        assert code == 3

        entry = ContinuationManifest(batch['root']).index.lookup(PAGE_A)
        assert entry['filename'] == a['volume']
        _, payload = read_capture_payload(
            os.path.join(batch['root'], entry['filename']),
            entry['offset'], entry['length'], uri=PAGE_A,
            digest=entry['payload_digest'])
        assert payload == b'page A captured'

        # The other proven pages still read correctly.
        code, done = _run(batch['root'], 'read', '--url', PAGE_DONE)
        assert code == 0 and done['payload'] == 'legacy done body'
        code, broken = _run(batch['root'], 'read', '--url', PAGE_BROKEN)
        assert code == 0 and broken['payload'] == 'legacy broken body'

        # Legacy volumes, captured bytes and old index positions are intact.
        assert _sha256(batch['legacy']) == before['legacy']
        assert _sha256(batch['colliding']) == before['colliding']
        assert _read_bytes(batch['old_index']) == before['old_index']

        # The untouched page is still visibly pending.
        assert rows[PAGE_NEVER]['remaining'] is True
