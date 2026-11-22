"""End to end offline demo of retention safe revisit recording.

Scenarios, all on plain files in a shared directory:

1. broken candidate indexes are never trusted (moved volume, wrong offset);
2. interleave A: rotation asks to withdraw while the capture is uncommitted,
   the ledger refuses and the recorder commits a revisit whose original stays;
3. interleave B: the preferred volume is removed irreversibly first, the
   recorder re-selects the other qualified volume (or saves the full response);
4. the recorder crashes after writing retention evidence but before commit:
   restart reaps only the stale, uncommitted reference and never releases a
   volume needed by a committed capture;
5. replay proves payload provenance by swapping index source order, while the
   ordinary (no explicit orig.* fields) revisit reading path still works.
"""

import json
import os
import subprocess
import sys
import tempfile

from pywb.recorder.multifilewarcwriter import MultiFileWARCWriter
from pywb.recorder.retentionindex import RetentionDedupIndex
from pywb.recorder.volumemanager import RetentionLedger, VolumeRetained
from pywb.warcserver.index.cdxobject import CDXObject
from pywb.warcserver.index.indexsource import FileIndexSource

from retention_demo.archivelab import (PAYLOAD, URI, VOL_A, VOL_B,
                                       build_seed_archive, make_capture_record,
                                       read_json, replay, reset_signals,
                                       wait_for_signal, write_signal)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def fresh_root():
    root = tempfile.mkdtemp(prefix='retention-demo-')
    layout = build_seed_archive(root)
    with open(os.path.join(root, 'layout.json'), 'w') as fh:
        json.dump(layout, fh)
    reset_signals(layout['signals_dir'])
    return root


def _env(root, mode=None, crash=False):
    env = dict(os.environ)
    env['PYTHONPATH'] = ROOT + os.pathsep + env.get('PYTHONPATH', '')
    env['RETENTION_SIGNALS_DIR'] = os.path.join(root, 'signals')
    env['PYWB_RETENTION_BARRIER'] = 'retention_demo.barrier:barrier'
    env['RETENTION_BARRIER_MODE'] = mode or 'signal'
    env.pop('PYWB_CRASH_BEFORE_COMMIT', None)
    if crash:
        env['PYWB_CRASH_BEFORE_COMMIT'] = '1'
    return env


def start_recorder(root, sources, capture_name, mode='signal', crash=False):
    return subprocess.Popen(
        [sys.executable, '-m', 'retention_demo.recorderproc',
         root, ','.join(sources), capture_name],
        cwd=ROOT, env=_env(root, mode, crash))


def run_rotation(root, mode, target=VOL_A):
    proc = subprocess.run(
        [sys.executable, '-m', 'retention_demo.rotationproc',
         root, mode, target],
        cwd=ROOT, env=_env(root, 'block'), check=True,
        capture_output=True, text=True)
    return read_json(os.path.join(root, 'signals', 'rotation-result'))


def recorded_cdx(root):
    path = os.path.join(root, 'recorded', 'recorded.cdxj')
    with open(path) as fh:
        return [CDXObject(line.encode('utf-8'))
                for line in fh.read().splitlines() if line]


def scenario_broken_indexes(root):
    print('\n[1] stale CDX entries are verified, not trusted')
    layout = read_json(os.path.join(root, 'layout.json'))
    ledger = RetentionLedger(root)
    for volume in (VOL_A, VOL_B):
        ledger.register_volume(volume, os.path.join(layout['warc_dir'], volume))

    def lookup_with(index_name, digest):
        index = RetentionDedupIndex(
            {index_name: FileIndexSource(
                os.path.join(layout['index_dir'], index_name + '.cdxj'))},
            ledger, layout['warc_dir'])
        return index.lookup_revisit({}, digest, URI,
                                    '2026-11-23T10:00:00Z')

    # the digest a real capture would compute for the identical payload
    digest = 'sha1:' + layout['cdx_a']['digest']

    good = lookup_with('source-a', digest)
    assert good is not None
    moved = lookup_with('moved-a', digest)
    bad_offset = lookup_with('bad-offset-a', digest)

    assert moved is None, 'moved-volume CDX must not be accepted'
    assert bad_offset is None, 'wrong-offset CDX must not be accepted'
    print('    moved-volume CDX rejected: True')
    print('    wrong-offset CDX rejected: True')
    print('    healthy source-a candidate :', good[3]['filename'],
          '@', good[3]['offset'])
    ledger.close()


def scenario_recorder_wins(root):
    print('\n[2a] interleave A: rotation blocked, committed revisit never dangles')
    reset_signals(os.path.join(root, 'signals'))

    recorder = start_recorder(root, ['source-a', 'source-b'],
                              'capture-interleave-a', mode='block')
    wait_for_signal(os.path.join(root, 'signals'), 'verified')

    rotation = run_rotation(root, 'coop', VOL_A)
    assert rotation['withdrawn'] is False
    assert rotation['retained_by'], 'rotation must be blocked by the open claim'
    print('    rotation withdraw refused while uncommitted:',
          rotation['retained_by'])

    recorder.wait(timeout=30)
    assert recorder.returncode == 0
    wait_for_signal(os.path.join(root, 'signals'), 'recorder-result')

    cdx = recorded_cdx(root)[-1]
    assert cdx['mime'] == 'warc/revisit'
    assert cdx['orig.filename'] == VOL_A
    assert os.path.exists(os.path.join(root, 'warcs', VOL_A))

    recorded_index = os.path.join(root, 'recorded', 'recorded.cdxj')
    headers, body, _, tracking = replay(
        root, [recorded_index,
               os.path.join(root, 'indexes', 'source-b.cdxj'),
               os.path.join(root, 'indexes', 'source-a.cdxj')],
        search_dirs=[os.path.join(root, 'recorded'),
                     os.path.join(root, 'warcs')])
    assert body == PAYLOAD
    opened = [path for path, _ in tracking.opened]
    assert any(path.endswith(VOL_A) for path in opened)
    assert not any(path.endswith(VOL_B) for path in opened)
    print('    committed revisit keeps its own status/headers:',
          headers.get_statuscode(), headers.get_header('X-Capture'))
    print('    payload replayed from retained', VOL_A, 'even with B listed first')


def scenario_rotation_wins_reselect(root):
    print('\n[2b] interleave B: irreversible removal first, re-select volume B')
    reset_signals(os.path.join(root, 'signals'))

    recorder = start_recorder(root, ['source-a', 'source-b'],
                              'capture-interleave-b', mode='block')
    wait_for_signal(os.path.join(root, 'signals'), 'verified')

    rotation = run_rotation(root, 'force', VOL_A)
    assert rotation['withdrawn'] is True
    assert not os.path.exists(os.path.join(root, 'warcs', VOL_A))
    print('    volume A withdrawn and removed before commit')

    recorder.wait(timeout=30)
    assert recorder.returncode == 0
    wait_for_signal(os.path.join(root, 'signals'), 'recorder-result')

    cdx = recorded_cdx(root)[-1]
    assert cdx['mime'] == 'warc/revisit'
    assert cdx['orig.filename'] == VOL_B
    print('    recorder re-selected qualified original:', VOL_B)

    recorded_index = os.path.join(root, 'recorded', 'recorded.cdxj')
    headers, body, _, tracking = replay(
        root, [recorded_index,
               os.path.join(root, 'indexes', 'source-a.cdxj'),
               os.path.join(root, 'indexes', 'source-b.cdxj')],
        search_dirs=[os.path.join(root, 'recorded'),
                     os.path.join(root, 'warcs')])
    assert body == PAYLOAD
    assert any(path.endswith(VOL_B) for path, _ in tracking.opened)
    print('    replay payload bytes come from', VOL_B, 'with capture headers',
          headers.get_header('X-Capture'))


def scenario_rotation_wins_full_response(root):
    print('\n[2c] no eligible volume left: capture saved as full response')
    reset_signals(os.path.join(root, 'signals'))

    recorder = start_recorder(root, ['source-a'],
                              'capture-standalone', mode='block')
    wait_for_signal(os.path.join(root, 'signals'), 'verified')
    run_rotation(root, 'force', VOL_A)
    recorder.wait(timeout=30)
    assert recorder.returncode == 0
    wait_for_signal(os.path.join(root, 'signals'), 'recorder-result')

    cdx = recorded_cdx(root)[-1]
    assert cdx['mime'] != 'warc/revisit'
    print('    written as self-contained response, mime =', cdx['mime'])

    recorded_index = os.path.join(root, 'recorded', 'recorded.cdxj')
    headers, body, _, _ = replay(
        root, [recorded_index],
        search_dirs=[os.path.join(root, 'recorded'),
                     os.path.join(root, 'warcs')])
    assert body == PAYLOAD
    print('    replay needs no original volume, bytes match')


def _record_in_process(root, sources, capture_name):
    layout = read_json(os.path.join(root, 'layout.json'))
    ledger = RetentionLedger(root)
    for volume in (VOL_A, VOL_B):
        ledger.register_volume(volume, os.path.join(layout['warc_dir'], volume))
    index_sources = {name: FileIndexSource(
        os.path.join(layout['index_dir'], name + '.cdxj')) for name in sources}
    record_index = os.path.join(layout['recorded_dir'], 'recorded.cdxj')
    dedup = RetentionDedupIndex(index_sources, ledger, layout['warc_dir'],
                                record_index_path=record_index)
    writer = MultiFileWARCWriter(
        os.path.join(layout['recorded_dir'],
                     'capture-{timestamp}-{random}.warc.gz'),
        dedup_index=dedup)
    writer.write_record(make_capture_record(writer, capture_name), {})
    writer.close()
    ledger.close()


def scenario_crash_and_restart(root):
    print('\n[3] crash before commit: restart reaps stale claim only')
    # committed capture first: its dependency on B must survive the restart
    _record_in_process(root, ['source-b'], 'committed-before-crash')
    committed = recorded_cdx(root)[-1]
    assert committed['orig.filename'] == VOL_B

    reset_signals(os.path.join(root, 'signals'))
    recorder = start_recorder(root, ['source-a'], 'crashed-capture',
                              mode='signal', crash=True)
    recorder.wait(timeout=30)
    assert recorder.returncode == 101

    proc = subprocess.run(
        [sys.executable, '-m', 'retention_demo.restartproc', root],
        cwd=ROOT, env=_env(root), check=True, capture_output=True, text=True)
    result = read_json(os.path.join(root, 'signals', 'restart-result'))

    assert result['reaped'], 'the uncommitted claim must be reaped'
    assert result['rotation_after_restart'][VOL_A] == 'withdrawn'
    assert result['rotation_after_restart'][VOL_B].startswith('retained:')
    print('    reaped uncommitted claim:', result['reaped'])
    print('    volume A releasable after restart; volume B stays retained by',
          result['rotation_after_restart'][VOL_B])
    print('    no volume is pinned permanently by the interrupted attempt')


def scenario_source_order_swap(root):
    print('\n[4] provenance under swapped index source order')
    root_a = fresh_root()
    root_b = fresh_root()
    _record_in_process(root_a, ['source-a', 'source-b'], 'order-a-first')
    _record_in_process(root_b, ['source-b', 'source-a'], 'order-b-first')

    cdx_a = recorded_cdx(root_a)[-1]
    cdx_b = recorded_cdx(root_b)[-1]
    assert cdx_a['orig.filename'] == VOL_A
    assert cdx_b['orig.filename'] == VOL_B
    print('    source order A,B -> provenance', cdx_a['orig.filename'])
    print('    source order B,A -> provenance', cdx_b['orig.filename'])

    # swap search source order: explicit orig.* fields still pin the payload
    for root_, expected, other in ((root_a, VOL_A, VOL_B),
                                   (root_b, VOL_B, VOL_A)):
        recorded_index = os.path.join(root_, 'recorded', 'recorded.cdxj')
        _, _, _, tracking = replay(
            root_, [recorded_index,
                    os.path.join(root_, 'indexes', 'source-a.cdxj'),
                    os.path.join(root_, 'indexes', 'source-b.cdxj')],
            search_dirs=[os.path.join(root_, 'recorded'),
                         os.path.join(root_, 'warcs')])
        opened = [path for path, _ in tracking.opened]
        assert any(path.endswith(expected) for path in opened)
        assert not any(path.endswith(other) for path in opened)
        print('    revisit provenance', expected, 'loads only', expected,
              'regardless of source order')

    # ordinary revisit path (no orig.* fields) still resolves via the
    # WARC-Refers-To headers and a cdx loader, like existing archives
    recorded_index = os.path.join(root_a, 'recorded', 'recorded.cdxj')
    headers, body, _, _ = replay(
        root_a, [recorded_index,
                 os.path.join(root_a, 'indexes', 'source-b.cdxj')],
        search_dirs=[os.path.join(root_a, 'recorded'),
                     os.path.join(root_a, 'warcs')],
        provenance=False)
    assert body == PAYLOAD
    print('    ordinary revisit resolution (WARC-Refers-To) still works:',
          headers.get_statuscode())


def main():
    root = fresh_root()
    print('demo root:', root)
    scenario_broken_indexes(root)
    scenario_recorder_wins(root)
    scenario_rotation_wins_reselect(fresh_root())
    scenario_rotation_wins_full_response(fresh_root())
    scenario_crash_and_restart(fresh_root())
    scenario_source_order_swap(fresh_root())
    print('\nall retention scenarios passed')


if __name__ == '__main__':
    main()
