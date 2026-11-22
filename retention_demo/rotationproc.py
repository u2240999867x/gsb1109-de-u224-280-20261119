"""Rotation process entry point for the offline retention demo.

Modes:

* ``coop``  : cooperative withdrawal through the ledger, which refuses while
              a committed/open reference still needs the volume;
* ``force`` : mark withdrawn and delete the bytes regardless of claims,
              modelling an irreversible external removal.

The process waits for the recorder's ``verified`` signal, proving that it
acts exactly after the recorder validated the preferred old volume but
before the new capture is committed.
"""

import json
import os
import sys

from pywb.recorder.volumemanager import RetentionLedger, VolumeRetained

from retention_demo.archivelab import (VOL_A, read_json, signal_path,
                                       wait_for_signal, write_signal)


def main():
    root = sys.argv[1]
    mode = sys.argv[2]
    target = sys.argv[3] if len(sys.argv) > 3 else VOL_A

    layout = read_json(os.path.join(root, 'layout.json'))
    signals_dir = layout['signals_dir']

    verified = wait_for_signal(signals_dir, 'verified')

    ledger = RetentionLedger(root)
    outcome = {'mode': mode, 'target': target, 'verified': verified}

    target_path = ledger.volume_path(target)

    if mode == 'coop':
        try:
            ledger.withdraw_volume(target)
            outcome['withdrawn'] = True
            if target_path and os.path.exists(target_path):
                os.unlink(target_path)
                outcome['bytes_removed'] = True
        except VolumeRetained as exc:
            outcome['withdrawn'] = False
            outcome['retained_by'] = exc.ref_ids

    elif mode == 'force':
        ledger.force_withdraw_volume(target)
        outcome['withdrawn'] = True
        if target_path and os.path.exists(target_path):
            os.unlink(target_path)
            outcome['bytes_removed'] = True
    else:
        raise ValueError('unknown mode {0}'.format(mode))

    outcome['state_after'] = ledger.snapshot()
    ledger.close()

    write_signal(signals_dir, 'rotation-result', json.dumps(outcome))

    # release a blocked recorder once the irreversible step is done
    if os.environ.get('RETENTION_BARRIER_MODE') == 'block':
        write_signal(signals_dir, 'proceed', 'done')


if __name__ == '__main__':
    main()
