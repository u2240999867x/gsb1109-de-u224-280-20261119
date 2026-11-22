"""
Retention ledger for WARC volumes that back committed revisit records.

The ledger is a small JSON file in a directory shared by the recording
process and the rotation process.  Every mutation is performed as a
read-modify-write cycle while holding an exclusive ``portalocker`` lock on
a sibling lock file, so the state stays coherent across processes.

A *reference* is one capture that depends on one or more volumes:

* a revisit capture depends on the volume holding its payload original;
* every newly written WARC is itself a volume referenced by its own capture.

References are opened (:meth:`begin_reference`) before the WARC bytes are
written, committed (:meth:`commit_reference`) once the capture and its CDX
evidence are on disk, or aborted (:meth:`abort_reference`).  A volume that is
referenced by any open or committed reference is *retained* and rotation is
not allowed to withdraw it.
"""

import contextlib
import json
import os
import tempfile

import portalocker


VOLUME_ACTIVE = 'active'
VOLUME_WITHDRAWN = 'withdrawn'


class VolumeError(Exception):
    """Base class for retention ledger errors."""


class VolumeWithdrawn(VolumeError):
    """Raised when a reference targets a volume that is no longer available."""


class VolumeRetained(VolumeError):
    """Raised when rotation tries to withdraw a volume still in use."""

    def __init__(self, name, ref_ids):
        super(VolumeRetained, self).__init__(
            'volume {0} retained by {1}'.format(name, sorted(ref_ids)))
        self.name = name
        self.ref_ids = sorted(ref_ids)


class RetentionLedger(object):
    def __init__(self, root_dir, filename='retention.json',
                 lock_filename='retention.lock'):
        self.root_dir = root_dir
        self.ledger_path = os.path.join(root_dir, filename)
        self.lock_path = os.path.join(root_dir, lock_filename)
        try:
            os.makedirs(root_dir)
        except OSError:
            pass

        self._lock_fh = open(self.lock_path, 'a+b')
        self.state = self._read_locked()

    # ------------------------------------------------------------------
    # persistence
    # ------------------------------------------------------------------
    def _read_locked(self):
        try:
            with open(self.ledger_path, 'r') as fh:
                return json.load(fh)
        except (IOError, ValueError):
            return {'volumes': {}, 'claims': {}}

    def _write_locked(self):
        fd, tmp_path = tempfile.mkstemp(dir=self.root_dir, suffix='.tmp')
        try:
            with os.fdopen(fd, 'w') as fh:
                json.dump(self.state, fh, indent=2, sort_keys=True)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp_path, self.ledger_path)
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    @contextlib.contextmanager
    def _lock(self):
        # blocking exclusive lock; portalocker >=3 rejects LOCK_UN through
        # lock(), so release explicitly with unlock() when available
        portalocker.lock(self._lock_fh, portalocker.LOCK_EX)
        try:
            yield
        finally:
            if hasattr(portalocker, 'unlock'):
                portalocker.unlock(self._lock_fh)
            else:  # pragma: no cover - older portalocker
                portalocker.lock(self._lock_fh, portalocker.LOCK_UN)

    def close(self):
        try:
            self._lock_fh.close()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # volume registry
    # ------------------------------------------------------------------
    def register_volume(self, name, path):
        with self._lock():
            volume = self.state['volumes'].setdefault(name, {})
            volume.setdefault('path', path)
            volume.setdefault('state', VOLUME_ACTIVE)
            self._write_locked()

    def is_withdrawn(self, name):
        with self._lock():
            self.state = self._read_locked()
            volume = self.state['volumes'].get(name)
            return bool(volume and volume['state'] == VOLUME_WITHDRAWN)

    def is_available(self, name):
        """A volume is usable only when active and still present on disk."""
        with self._lock():
            self.state = self._read_locked()
            volume = self.state['volumes'].get(name)
            if not volume or volume['state'] != VOLUME_ACTIVE:
                return False
            path = volume.get('path')
            return bool(path and os.path.exists(path))

    def volume_path(self, name):
        with self._lock():
            self.state = self._read_locked()
            volume = self.state['volumes'].get(name)
            return volume['path'] if volume else None

    def _references_for(self, name):
        return [ref_id for ref_id, claim in self.state['claims'].items()
                if name in claim['volumes']]

    def retained_volumes(self):
        retained = set()
        for claim in self.state['claims'].values():
            retained.update(claim['volumes'])
        return retained

    def withdraw_volume(self, name):
        """Mark a volume withdrawn.

        Refuses while any reference still depends on the volume.  The caller
        is responsible for the irreversible step (deleting/moving the file)
        once this call succeeds.
        """
        with self._lock():
            self.state = self._read_locked()
            refs = self._references_for(name)
            if refs:
                raise VolumeRetained(name, refs)

            volume = self.state['volumes'].setdefault(name, {})
            volume['state'] = VOLUME_WITHDRAWN
            self._write_locked()
            return True

    def force_withdraw_volume(self, name):
        """Mark a volume withdrawn without honoring retention claims.

        Models an irreversible removal that bypasses the recorder (legacy
        storage reaper, moved volume, external operator action).  Recorders
        detect the loss on their next write or verification and re-select
        another original or save the full response.
        """
        with self._lock():
            self.state = self._read_locked()
            volume = self.state['volumes'].setdefault(name, {})
            volume['state'] = VOLUME_WITHDRAWN
            self._write_locked()
            return True

    # ------------------------------------------------------------------
    # references (capture retention evidence)
    # ------------------------------------------------------------------
    def begin_reference(self, ref_id, volume_names, pid=None):
        """Register retention evidence for a capture about to be written."""
        volume_names = list(dict.fromkeys(volume_names))
        with self._lock():
            self.state = self._read_locked()
            for name in volume_names:
                volume = self.state['volumes'].get(name)
                if not volume or volume['state'] != VOLUME_ACTIVE:
                    raise VolumeWithdrawn(name)
                if not os.path.exists(volume['path']):
                    raise VolumeWithdrawn(name)

            self.state['claims'][ref_id] = {
                'volumes': volume_names,
                'committed': False,
                'pid': os.getpid() if pid is None else pid,
            }
            self._write_locked()
            return dict(self.state['claims'][ref_id])

    def commit_reference(self, ref_id):
        with self._lock():
            self.state = self._read_locked()
            claim = self.state['claims'].get(ref_id)
            if not claim:
                raise VolumeError('unknown reference {0}'.format(ref_id))
            claim['committed'] = True
            self._write_locked()

    def add_claim_volume(self, ref_id, volume_name):
        """Extend an open reference to cover the WARC currently being written."""
        with self._lock():
            self.state = self._read_locked()
            claim = self.state['claims'].get(ref_id)
            if not claim:
                raise VolumeError('unknown reference {0}'.format(ref_id))
            volume = self.state['volumes'].get(volume_name)
            if not volume or volume['state'] != VOLUME_ACTIVE:
                raise VolumeWithdrawn(volume_name)
            if volume_name not in claim['volumes']:
                claim['volumes'].append(volume_name)
            self._write_locked()

    def abort_reference(self, ref_id):
        with self._lock():
            self.state = self._read_locked()
            self.state['claims'].pop(ref_id, None)
            self._write_locked()

    def get_claim(self, ref_id):
        with self._lock():
            self.state = self._read_locked()
            claim = self.state['claims'].get(ref_id)
            return dict(claim) if claim else None

    # ------------------------------------------------------------------
    # crash recovery
    # ------------------------------------------------------------------
    @staticmethod
    def _pid_alive(pid):
        if not pid:
            return False
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        except OSError:
            return False
        return True

    def reap_stale_references(self):
        """Release uncommitted claims whose recorder process is gone.

        Committed claims are never reaped: they represent captures already
        visible to replay.  An interrupted attempt must not pin a volume
        forever, but a committed capture must never become dangling.
        """
        reaped = []
        with self._lock():
            self.state = self._read_locked()
            for ref_id, claim in list(self.state['claims'].items()):
                if claim.get('committed'):
                    continue
                if not self._pid_alive(claim.get('pid')):
                    self.state['claims'].pop(ref_id)
                    reaped.append(ref_id)
            if reaped:
                self._write_locked()
        return reaped

    def snapshot(self):
        with self._lock():
            self.state = self._read_locked()
            return json.loads(json.dumps(self.state))


class VolumeManager(object):
    """Thin wrapper tying volume files on disk to the retention ledger."""

    def __init__(self, root_dir):
        self.root_dir = root_dir
        self.ledger = RetentionLedger(root_dir)

    def close(self):
        self.ledger.close()

    def register(self, name, path):
        self.ledger.register_volume(name, path)

    def withdraw(self, name, delete_file=True):
        """Withdraw a volume and, when permitted, remove its bytes."""
        self.ledger.withdraw_volume(name)
        if delete_file:
            path = self.ledger.volume_path(name)
            if path and os.path.exists(path):
                os.unlink(path)
        return True

    def is_available(self, name):
        return self.ledger.is_available(name)

    def retained_volumes(self):
        return self.ledger.retained_volumes()

    def reap_stale_references(self):
        return self.ledger.reap_stale_references()
