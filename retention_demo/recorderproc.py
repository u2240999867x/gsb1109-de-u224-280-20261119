"""Recorder process entry point for the offline retention demo.

Run as a separate process so rotation can act on the same shared directory
while this process is inside the verify -> retain -> commit window.
"""

import json
import os
import sys

from pywb.recorder.multifilewarcwriter import MultiFileWARCWriter
from pywb.recorder.retentionindex import RetentionDedupIndex
from pywb.recorder.volumemanager import RetentionLedger
from pywb.warcserver.index.indexsource import FileIndexSource

from retention_demo.archivelab import (VOL_A, VOL_B, WARC_DATE_ISO,
                                       make_capture_record, read_json,
                                       write_signal)


def build_writer(root, source_order):
    layout = read_json(os.path.join(root, 'layout.json'))
    ledger = RetentionLedger(root)

    # seed the volume registry from the real files on disk
    for volume in (VOL_A, VOL_B):
        ledger.register_volume(volume,
                               os.path.join(layout['warc_dir'], volume))

    sources = {}
    for index_name in source_order:
        sources[index_name] = FileIndexSource(
            os.path.join(layout['index_dir'], index_name + '.cdxj'))

    record_index = os.path.join(layout['recorded_dir'], 'recorded.cdxj')
    dedup_index = RetentionDedupIndex(
        sources, ledger, layout['warc_dir'],
        record_index_path=record_index)

    writer = MultiFileWARCWriter(
        os.path.join(layout['recorded_dir'], 'capture-{timestamp}-{random}.warc.gz'),
        dedup_index=dedup_index)
    return writer, ledger, record_index


def main():
    root = sys.argv[1]
    source_order = sys.argv[2].split(',')
    capture_name = sys.argv[3]

    writer, ledger, record_index = build_writer(root, source_order)

    record = make_capture_record(writer, capture_name, WARC_DATE_ISO)
    writer.write_record(record, {})
    writer.close()
    ledger.close()

    with open(record_index) as fh:
        cdx_lines = [line for line in fh.read().splitlines() if line]

    result = {'cdx': cdx_lines,
              'recorded_dir': os.path.dirname(record_index)}
    write_signal(os.path.join(root, 'signals'),
                 'recorder-result', json.dumps(result))


if __name__ == '__main__':
    main()
