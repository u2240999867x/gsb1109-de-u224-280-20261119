"""Tests for retention safe revisit recording against offline WARC volumes.

These tests never touch the network.  The two process interleavings run real
subprocesses against a shared directory so the retention ledger is exercised
across process boundaries exactly as in production rotation.
"""

import json
import os
import subprocess
import sys

import pytest

from pywb.recorder.multifilewarcwriter import MultiFileWARCWriter
from pywb.recorder.retentionindex import RetentionDedupIndex
from pywb.recorder.volumemanager import (RetentionLedger, VolumeRetained,
                                         VolumeWithdrawn)
from pywb.warcserver.index.cdxobject import CDXObject
from pywb.warcserver.index.indexsource import FileIndexSource

from retention_demo.archivelab import (PAYLOAD, URI, VOL_A, VOL_B,
                                       build_seed_archive, make_capture_record,
                                       read_json, replay, reset_signals,
                                       wait_for_signal)
from retention_demo.run_demo import (_env, _record_in_process, fresh_root,
                                     recorded_cdx, run_rotation,
                                     start_recorder)

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture()
def archive(tmp_path):
    root = str(tmp_path)
    layout = build_seed_archive(root)
    with open(os.path.join(root, 'layout.json'), 'w') as fh:
        json.dump(layout, fh)
    reset_signals(layout['signals_dir'])
    return root


def _ledger_with_volumes(root):
    layout = read_json(os.path.join(root, 'layout.json'))
    ledger = RetentionLedger(root)
    for volume in (VOL_A, VOL_B):
        ledger.register_volume(volume, os.path.join(layout['warc_dir'], volume))
    return ledger, layout


# ============================================================================
class TestRetentionLedger:
    def test_withdraw_blocked_while_referenced(self, archive):
        ledger, _ = _ledger_with_volumes(archive)
        ledger.begin_reference('ref-1', [VOL_A])

        with pytest.raises(VolumeRetained) as exc:
            ledger.withdraw_volume(VOL_A)
        assert exc.value.ref_ids == ['ref-1']

        # aborting the open reference frees the volume
        ledger.abort_reference('ref-1')
        assert ledger.withdraw_volume(VOL_A) is True
        ledger.close()

    def test_committed_reference_survives_and_blocks(self, archive):
        ledger, _ = _ledger_with_volumes(archive)
        ledger.begin_reference('ref-1', [VOL_B])
        ledger.commit_reference('ref-1')

        with pytest.raises(VolumeRetained):
            ledger.withdraw_volume(VOL_B)

        claim = ledger.get_claim('ref-1')
        assert claim['committed'] is True
        assert VOL_B in claim['volumes']
        ledger.close()

    def test_begin_refuses_withdrawn_or_missing_volume(self, archive):
        ledger, layout = _ledger_with_volumes(archive)
        ledger.force_withdraw_volume(VOL_A)

        with pytest.raises(VolumeWithdrawn):
            ledger.begin_reference('ref-1', [VOL_A])

        os.unlink(os.path.join(layout['warc_dir'], VOL_B))
        with pytest.raises(VolumeWithdrawn):
            ledger.begin_reference('ref-2', [VOL_B])
        ledger.close()

    def test_reap_only_uncommitted_dead_references(self, archive):
        ledger, _ = _ledger_with_volumes(archive)
        # committed claim must never be reaped
        ledger.begin_reference('committed', [VOL_B])
        ledger.commit_reference('committed')
        # uncommitted claim owned by a process that no longer exists
        ledger.state['claims']['dead'] = {
            'volumes': [VOL_A], 'committed': False, 'pid': 999999}
        ledger._write_locked()

        reaped = ledger.reap_stale_references()
        assert reaped == ['dead']
        assert ledger.get_claim('committed') is not None
        assert ledger.get_claim('dead') is None

        # the interrupted attempt no longer pins A, the committed one pins B
        assert ledger.withdraw_volume(VOL_A) is True
        with pytest.raises(VolumeRetained):
            ledger.withdraw_volume(VOL_B)
        ledger.close()


# ============================================================================
class TestCandidateVerification:
    def _index(self, root, layout, name):
        ledger, _ = _ledger_with_volumes(root)
        return RetentionDedupIndex(
            {name: FileIndexSource(
                os.path.join(layout['index_dir'], name + '.cdxj'))},
            ledger, layout['warc_dir']), ledger

    def test_healthy_candidate_accepted_with_provenance(self, archive):
        layout = read_json(os.path.join(archive, 'layout.json'))
        digest = 'sha1:' + layout['cdx_a']['digest']
        index, ledger = self._index(archive, layout, 'source-a')
        result = index.lookup_revisit(
            {}, digest, URI, '2026-11-23T10:00:00Z')

        assert result is not None
        assert result[0] == 'revisit'
        assert result[3]['filename'] == VOL_A
        ledger.close()

    def test_moved_volume_cdx_rejected(self, archive):
        layout = read_json(os.path.join(archive, 'layout.json'))
        digest = 'sha1:' + layout['cdx_a']['digest']
        index, ledger = self._index(archive, layout, 'moved-a')
        result = index.lookup_revisit(
            {}, digest, URI, '2026-11-23T10:00:00Z')
        assert result is None
        ledger.close()

    def test_wrong_offset_cdx_rejected(self, archive):
        layout = read_json(os.path.join(archive, 'layout.json'))
        digest = 'sha1:' + layout['cdx_a']['digest']
        index, ledger = self._index(archive, layout, 'bad-offset-a')
        result = index.lookup_revisit(
            {}, digest, URI, '2026-11-23T10:00:00Z')
        assert result is None
        ledger.close()

    def test_wrong_offset_does_not_poison_same_volume(self, archive):
        layout = read_json(os.path.join(archive, 'layout.json'))
        ledger, _ = _ledger_with_volumes(archive)
        digest = 'sha1:' + layout['cdx_a']['digest']
        index = RetentionDedupIndex(
            {'bad': FileIndexSource(
                os.path.join(layout['index_dir'], 'bad-offset-a.cdxj')),
             'good': FileIndexSource(
                os.path.join(layout['index_dir'], 'source-a.cdxj'))},
            ledger, layout['warc_dir'])

        result = index.lookup_revisit({}, digest, URI,
                                      '2026-11-23T10:00:00Z')
        assert result[3]['filename'] == VOL_A
        ledger.close()


# ============================================================================
class TestWriterRetention:
    def test_revisit_keeps_own_headers_and_records_provenance(self, archive):
        _record_in_process(archive, ['source-a', 'source-b'], 'new-capture')

        cdx = recorded_cdx(archive)[-1]
        assert cdx['mime'] == 'warc/revisit'
        assert cdx['orig.filename'] == VOL_A

        recorded_index = os.path.join(archive, 'recorded', 'recorded.cdxj')
        headers, body, _, tracking = replay(
            archive, [recorded_index,
                      os.path.join(archive, 'indexes', 'source-b.cdxj'),
                      os.path.join(archive, 'indexes', 'source-a.cdxj')],
            search_dirs=[os.path.join(archive, 'recorded'),
                         os.path.join(archive, 'warcs')])
        assert body == PAYLOAD
        assert headers.get_statuscode() == '200'
        assert headers.get_header('X-Capture') == 'new-capture'
        opened = [path for path, _ in tracking.opened]
        assert any(path.endswith(VOL_A) for path in opened)
        assert not any(path.endswith(VOL_B) for path in opened)

    def test_full_response_fallback_after_force_withdraw(self, archive):
        layout = read_json(os.path.join(archive, 'layout.json'))
        ledger, _ = _ledger_with_volumes(archive)

        sources = {'source-a': FileIndexSource(
            os.path.join(layout['index_dir'], 'source-a.cdxj'))}
        dedup = RetentionDedupIndex(
            sources, ledger, layout['warc_dir'],
            record_index_path=os.path.join(layout['recorded_dir'],
                                           'recorded.cdxj'))
        writer = MultiFileWARCWriter(
            os.path.join(layout['recorded_dir'],
                         'capture-{timestamp}-{random}.warc.gz'),
            dedup_index=dedup)

        # irreversibly remove the only eligible volume before writing
        ledger.force_withdraw_volume(VOL_A)
        os.unlink(os.path.join(layout['warc_dir'], VOL_A))

        writer.write_record(make_capture_record(writer, 'standalone'), {})
        writer.close()
        ledger.close()

        cdx = recorded_cdx(archive)[-1]
        assert cdx['mime'] != 'warc/revisit'

        recorded_index = os.path.join(archive, 'recorded', 'recorded.cdxj')
        _, body, _, _ = replay(
            archive, [recorded_index],
            search_dirs=[os.path.join(archive, 'recorded'),
                         os.path.join(archive, 'warcs')])
        assert body == PAYLOAD


# ============================================================================
class TestProcessInterleaving:
    def test_recorder_commits_before_rotation_withdraws(self, archive):
        recorder = start_recorder(archive, ['source-a', 'source-b'],
                                  'interleave-a', mode='block')
        wait_for_signal(os.path.join(archive, 'signals'), 'verified')

        rotation = run_rotation(archive, 'coop', VOL_A)
        assert rotation['withdrawn'] is False
        assert rotation['retained_by']

        recorder.wait(timeout=30)
        assert recorder.returncode == 0
        wait_for_signal(os.path.join(archive, 'signals'), 'recorder-result')

        cdx = recorded_cdx(archive)[-1]
        assert cdx['mime'] == 'warc/revisit'
        assert cdx['orig.filename'] == VOL_A
        assert os.path.exists(os.path.join(archive, 'warcs', VOL_A))

    def test_irreversible_removal_forces_reselection(self, archive):
        recorder = start_recorder(archive, ['source-a', 'source-b'],
                                  'interleave-b', mode='block')
        wait_for_signal(os.path.join(archive, 'signals'), 'verified')

        rotation = run_rotation(archive, 'force', VOL_A)
        assert rotation['withdrawn'] is True

        recorder.wait(timeout=30)
        assert recorder.returncode == 0
        wait_for_signal(os.path.join(archive, 'signals'), 'recorder-result')

        cdx = recorded_cdx(archive)[-1]
        assert cdx['mime'] == 'warc/revisit'
        assert cdx['orig.filename'] == VOL_B

        recorded_index = os.path.join(archive, 'recorded', 'recorded.cdxj')
        _, body, _, tracking = replay(
            archive, [recorded_index,
                      os.path.join(archive, 'indexes', 'source-a.cdxj'),
                      os.path.join(archive, 'indexes', 'source-b.cdxj')],
            search_dirs=[os.path.join(archive, 'recorded'),
                         os.path.join(archive, 'warcs')])
        assert body == PAYLOAD
        assert any(path.endswith(VOL_B) for path, _ in tracking.opened)

    def test_crash_before_commit_restart_releases_stale_claim(self, archive):
        _record_in_process(archive, ['source-b'], 'committed-before-crash')
        assert recorded_cdx(archive)[-1]['orig.filename'] == VOL_B

        reset_signals(os.path.join(archive, 'signals'))
        recorder = start_recorder(archive, ['source-a'], 'crashed',
                                  mode='signal', crash=True)
        recorder.wait(timeout=30)
        assert recorder.returncode == 101

        subprocess.run(
            [sys.executable, '-m', 'retention_demo.restartproc', archive],
            cwd=REPO_ROOT, env=_env(archive), check=True,
            capture_output=True, text=True)
        result = read_json(os.path.join(archive, 'signals',
                                        'restart-result'))

        assert result['reaped']
        assert result['rotation_after_restart'][VOL_A] == 'withdrawn'
        assert result['rotation_after_restart'][VOL_B].startswith('retained:')


# ============================================================================
class TestReplayProvenance:
    def test_source_order_swap_does_not_change_payload_origin(self):
        root_a = fresh_root()
        root_b = fresh_root()
        _record_in_process(root_a, ['source-a', 'source-b'], 'order-a')
        _record_in_process(root_b, ['source-b', 'source-a'], 'order-b')

        assert recorded_cdx(root_a)[-1]['orig.filename'] == VOL_A
        assert recorded_cdx(root_b)[-1]['orig.filename'] == VOL_B

        for root, expected, other in ((root_a, VOL_A, VOL_B),
                                      (root_b, VOL_B, VOL_A)):
            recorded_index = os.path.join(root, 'recorded', 'recorded.cdxj')
            _, body, _, tracking = replay(
                root, [recorded_index,
                       os.path.join(root, 'indexes', 'source-a.cdxj'),
                       os.path.join(root, 'indexes', 'source-b.cdxj')],
                search_dirs=[os.path.join(root, 'recorded'),
                             os.path.join(root, 'warcs')])
            assert body == PAYLOAD
            opened = [path for path, _ in tracking.opened]
            assert any(path.endswith(expected) for path in opened)
            assert not any(path.endswith(other) for path in opened)

    def test_ordinary_warc_refers_to_replay_still_works(self):
        root = fresh_root()
        _record_in_process(root, ['source-a'], 'ordinary-revisit')
        recorded_index = os.path.join(root, 'recorded', 'recorded.cdxj')

        headers, body, _, _ = replay(
            root, [recorded_index,
                   os.path.join(root, 'indexes', 'source-b.cdxj')],
            search_dirs=[os.path.join(root, 'recorded'),
                         os.path.join(root, 'warcs')],
            provenance=False)
        assert body == PAYLOAD
        assert headers.get_statuscode() == '200'
