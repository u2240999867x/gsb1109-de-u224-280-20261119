"""Inter-process barrier used to pin down recorder/rotation interleaving.

The recorder imports this module through the ``PYWB_RETENTION_BARRIER``
environment variable.  Behavior is selected with ``RETENTION_BARRIER_MODE``:

* ``signal``: announce that the preferred volume was verified readable, then
  continue immediately (recorder wins the race);
* ``block``: announce and wait until the rotation process drops a ``proceed``
  signal, letting rotation become irreversible first.
"""

import os
import time

from retention_demo.archivelab import signal_path, wait_for_signal, write_signal


def barrier(stage, volume_name):
    signals_dir = os.environ['RETENTION_SIGNALS_DIR']
    mode = os.environ.get('RETENTION_BARRIER_MODE', 'signal')

    if stage != 'after_verify':
        return

    # only the first verified candidate participates in the race; later
    # candidates come from re-selection after rotation already acted
    released = signal_path(signals_dir, 'released')
    if os.path.exists(released):
        return

    write_signal(signals_dir, 'verified', volume_name)

    if mode == 'block':
        # rotation acts while the capture is still uncommitted
        wait_for_signal(signals_dir, 'proceed')
        try:
            os.unlink(signal_path(signals_dir, 'proceed'))
        except OSError:
            pass
        write_signal(signals_dir, 'released', '1')
        # give the rotation process a moment to finish its ledger write
        time.sleep(0.1)
