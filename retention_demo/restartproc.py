"""Takeover process run after a recorder crashed before commit.

It reaps uncommitted references whose recorder process no longer exists,
leaves every committed reference untouched, then reports what rotation is
allowed to withdraw afterwards.
"""

import json
import os
import sys

from pywb.recorder.volumemanager import RetentionLedger, VolumeRetained

from retention_demo.archivelab import VOL_A, VOL_B, read_json, write_signal


def main():
    root = sys.argv[1]
    layout = read_json(os.path.join(root, 'layout.json'))

    ledger = RetentionLedger(root)
    before = ledger.snapshot()

    reaped = ledger.reap_stale_references()
    after_reap = ledger.snapshot()

    rotation = {}
    for volume in (VOL_A, VOL_B):
        try:
            ledger.withdraw_volume(volume)
            rotation[volume] = 'withdrawn'
        except VolumeRetained as exc:
            rotation[volume] = 'retained:{0}'.format(','.join(exc.ref_ids))

    result = {'claims_before': before['claims'],
              'reaped': reaped,
              'claims_after_reap': after_reap['claims'],
              'rotation_after_restart': rotation,
              'final_snapshot': ledger.snapshot()}
    ledger.close()

    write_signal(layout['signals_dir'], 'restart-result', json.dumps(result))


if __name__ == '__main__':
    main()
