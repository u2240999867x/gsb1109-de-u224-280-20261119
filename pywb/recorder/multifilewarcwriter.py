import base64
import datetime
import os
import shutil
import traceback

import portalocker
from warcio.timeutils import timestamp20_now
from warcio.warcwriter import BaseWARCWriter

from pywb.recorder.volumemanager import VolumeWithdrawn
from pywb.utils.format import res_template
from pywb.utils.io import no_except_close


# ============================================================================
class MultiFileWARCWriter(BaseWARCWriter):
    FILE_TEMPLATE = 'rec-{timestamp}-{hostname}.warc.gz'

    def __init__(self, dir_template, filename_template=None, max_size=0,
                 max_idle_secs=1800, *args, **kwargs):
        super(MultiFileWARCWriter, self).__init__(*args, **kwargs)

        if not filename_template:
            dir_template, filename_template = os.path.split(dir_template)
            dir_template += os.path.sep

        if not filename_template:
            filename_template = self.FILE_TEMPLATE

        self.dir_template = dir_template
        self.key_template = kwargs.get('key_template', self.dir_template)
        self.dedup_index = kwargs.get('dedup_index')
        self.dedup_by_url = kwargs.get('dedup_by_url')
        self.filename_template = filename_template
        self.max_size = max_size
        if max_idle_secs > 0:
            self.max_idle_time = datetime.timedelta(seconds=max_idle_secs)
        else:
            self.max_idle_time = None

        self.fh_cache = {}

    def _check_revisit(self, record, params):
        if not self.dedup_index or record.rec_type != 'response':
            return record, None

        # ensure payload digest is computed at this point
        self.ensure_digest(record, block=False, payload=True)

        try:
            url = record.rec_headers.get_header('WARC-Target-URI')
            digest = record.rec_headers.get_header('WARC-Payload-Digest') if not self.dedup_by_url else None
            iso_dt = record.rec_headers.get_header('WARC-Date')
            result = self.dedup_index.lookup_revisit(params, digest, url, iso_dt)
        except Exception as e:
            traceback.print_exc()
            result = None

        if result == 'skip':
            return None, None

        if isinstance(result, tuple) and result[0] == 'revisit':
            # keep the full response around so the capture can still be
            # written self contained if the original volume is withdrawn
            params['_full_response'] = record
            # retention aware indexes return the concrete original CDX as a
            # fourth element so the payload provenance can be recorded
            orig_cdx = result[3] if len(result) > 3 else None
            record = self.create_revisit_record(url, digest, result[1], result[2],
                                                http_headers=record.http_headers)
            return record, orig_cdx

        return record, None

    def get_new_filename(self, dir_, params):
        timestamp = timestamp20_now()

        randstr = base64.b32encode(os.urandom(5)).decode('utf-8')

        filename = dir_ + res_template(self.filename_template, params,
                                       hostname=self.hostname,
                                       timestamp=timestamp,
                                       random=randstr)

        return filename

    def allow_new_file(self, filename, params):
        return True

    def _open_file(self, filename, params):
        path, name = os.path.split(filename)

        try:
            os.makedirs(path)
        except Exception:
            pass

        fh = open(filename, 'a+b')

        if self.dedup_index:
            self.dedup_index.add_warc_file(filename, params)

        return fh

    def _close_file(self, fh):
        try:
            if os.name != 'nt':
                if hasattr(portalocker, 'unlock'):
                    portalocker.unlock(fh)
                else:  # pragma: no cover - older portalocker
                    portalocker.lock(fh, portalocker.LOCK_UN)
            return True
        except Exception as e:
            print(e)
            return False
        finally:
            no_except_close(fh)

    def get_dir_key(self, params):
        return res_template(self.key_template, params)

    def close_key(self, dir_key):
        if isinstance(dir_key, dict):
            dir_key = self.get_dir_key(dir_key)

        result = self.fh_cache.pop(dir_key, None)
        if not result:
            return

        out, filename = result
        if self._close_file(out):
            return filename

    def close_file(self, match_filename):
        for dir_key, out, filename in self.iter_open_files():
            if filename == match_filename:
                return self.close_key(dir_key)

    def _is_write_resp(self, resp, params):
        return True

    def _is_write_req(self, req, params):
        return True

    def write_record(self, record, params=None):
        params = params or {}
        self._do_write_req_resp(None, record, params)

    def _copy_header(self, from_rec, to_rec, name):
        header = from_rec.rec_headers.get_header(name)
        if header:
            to_rec.rec_headers.add_header(name, header)

    def _do_write_req_resp(self, req, resp, params):
        self._copy_header(resp, req, 'WARC-Source-URI')
        self._copy_header(resp, req, 'WARC-Creation-Date')

        resp, orig_cdx = self._check_revisit(resp, params)
        if not resp:
            return

        if not self._is_write_resp(resp, params):
            return

        params.setdefault('_revisit_orig', orig_cdx)

        return self._write_with_retention(req, resp, params, orig_cdx)

    def _write_with_retention(self, req, resp, params, orig_cdx):
        """Write a capture while keeping its payload volume retained.

        The retention ledger makes the ordering with rotation safe:

        * a payload original is pinned before the new WARC is written, so a
          committed revisit can never be left pointing at a withdrawn volume;
        * if the chosen original becomes unavailable in the window between
          verification and commit, another verified candidate is selected, or
          the full response is written instead;
        * an interrupted attempt aborts its reference, so uncommitted work
          never pins a volume permanently.
        """
        if not (self.dedup_index and hasattr(self.dedup_index, 'ledger')):
            return self._write_pair_once(req, resp, params)

        ledger = self.dedup_index.ledger
        ref_id = self.dedup_index.new_reference_id()
        params['_retention_ref'] = ref_id

        try:
            return self._write_revisit_or_fallback(req, resp, params,
                                                   orig_cdx, ref_id, ledger)
        except Exception:
            ledger.abort_reference(ref_id)
            params.pop('_retention_ref', None)
            raise

    def _write_revisit_or_fallback(self, req, resp, params,
                                   orig_cdx, ref_id, ledger):
        candidate_cdx = orig_cdx

        # first attempt with the already verified candidate, then re-lookup
        # further eligible originals if rotation wins the race
        while True:
            if candidate_cdx:
                volume_name = candidate_cdx['filename']

                try:
                    ledger.begin_reference(ref_id, [volume_name])
                except VolumeWithdrawn:
                    self.dedup_index.exclude_volume(volume_name)
                    candidate_cdx = None
                    continue

                # inter-process barrier: the preferred original was verified
                # readable and its retention claim is open, but the new
                # capture is not committed yet.  A rotation process can try to
                # withdraw the volume here; a cooperative withdraw is blocked
                # by the open claim, an irreversible one is detected below.
                self._retention_barrier('after_verify', volume_name)

                digest = resp.rec_headers.get_header('WARC-Payload-Digest')
                if not self.dedup_index.verify_candidate(candidate_cdx,
                                                         digest):
                    # rotation removed the volume irreversibly while we waited
                    ledger.abort_reference(ref_id)
                    self.dedup_index.exclude_volume(volume_name)
                    candidate_cdx = None
                    continue

                params['_revisit_orig'] = candidate_cdx
                if self._write_pair_once(req, resp, params, ref_id, ledger):
                    return True

                # write failed: volume may have just been withdrawn
                ledger.abort_reference(ref_id)
                if not ledger.is_available(volume_name):
                    self.dedup_index.exclude_volume(volume_name)
                    candidate_cdx = None
                    continue

                return False
            # pick another still available qualified original
            result = self._relookup_revisit(resp, params)
            if result:
                candidate_cdx = result[3]
                continue

            # no usable original left: keep the capture self contained
            full_resp = self._reload_full_response(resp, params)
            params['_revisit_orig'] = None
            ledger.begin_reference(ref_id, [])
            return self._write_pair_once(req, full_resp, params,
                                         ref_id, ledger)

    @staticmethod
    def _retention_barrier(stage, volume_name):
        hook = os.environ.get('PYWB_RETENTION_BARRIER')
        if not hook:
            return
        module_name, func_name = hook.rsplit(':', 1)
        import importlib
        func = getattr(importlib.import_module(module_name), func_name)
        func(stage, volume_name)

    def _relookup_revisit(self, resp, params):
        url = resp.rec_headers.get_header('WARC-Target-URI')
        digest = resp.rec_headers.get_header('WARC-Payload-Digest')
        iso_dt = resp.rec_headers.get_header('WARC-Date')
        return self.dedup_index.lookup_revisit(params, digest, url, iso_dt)

    def _reload_full_response(self, resp, params):
        if resp.rec_type != 'revisit':
            return resp
        # the buffered payload of the original response is kept by the
        # recorder under '_full_response' before revisit conversion
        return params.get('_full_response', resp)

    def _write_pair_once(self, req, resp, params, ref_id=None, ledger=None):
        def write_callback(out, filename):
            #url = resp.rec_headers.get_header('WARC-Target-URI')
            #print('Writing req/resp {0} to {1} '.format(url, filename))
            self._write_warc_record(out, resp)

            if req and self._is_write_req(req, params):
                self._write_warc_record(out, req)

        return self._write_to_file(params, write_callback, ref_id, ledger)

    def write_stream_to_file(self, params, stream):
        def write_callback(out, filename):
            #print('Writing stream to {0}'.format(filename))
            shutil.copyfileobj(stream, out)

        return self._write_to_file(params, write_callback)

    def _write_to_file(self, params, write_callback, ref_id=None, ledger=None):
        full_dir = res_template(self.dir_template, params)
        dir_key = self.get_dir_key(params)

        result = self.fh_cache.get(dir_key)

        close_file = False

        new_size = start = 0

        if result:
            out, filename = result
            is_new = False
        else:
            filename = self.get_new_filename(full_dir, params)

            if not self.allow_new_file(filename, params):
                return False

            out = self._open_file(filename, params)

            is_new = True

        if ref_id and ledger:
            # the WARC being written is itself a volume this capture depends on
            ledger.add_claim_volume(ref_id, os.path.basename(filename))

        try:
            start = out.tell()

            write_callback(out, filename)

            out.flush()

            new_size = out.tell()

            out.seek(start)

            if self.dedup_index:
                self.dedup_index.add_urls_to_index(out, params,
                                                   filename,
                                                   new_size - start)

            # crash injection: retention evidence is registered and the new
            # WARC is on disk, but the reference is not committed.  Used to
            # prove restart recovery releases only uncommitted claims.
            if ref_id and os.environ.get('PYWB_CRASH_BEFORE_COMMIT'):
                os._exit(101)

            if ref_id and ledger:
                ledger.commit_reference(ref_id)

            return True

        except Exception as e:
            traceback.print_exc()
            close_file = True
            return False

        finally:
            # check for rollover
            if self.max_size and new_size > self.max_size:
                close_file = True

            if close_file:
                self._close_file(out)
                if not is_new:
                    self.fh_cache.pop(dir_key, None)

            elif is_new:
                if os.name != 'nt':
                    portalocker.lock(out, portalocker.LOCK_EX | portalocker.LOCK_NB)
                self.fh_cache[dir_key] = (out, filename)

    def iter_open_files(self):
        for n, v in list(self.fh_cache.items()):
            out, filename = v
            yield n, out, filename

    def close(self):
        for dir_key, out, filename in self.iter_open_files():
            self._close_file(out)

        self.fh_cache = {}

    def close_idle_files(self):
        if not self.max_idle_time:
            return

        now = datetime.datetime.now(datetime.timezone.utc)

        for dir_key, out, filename in self.iter_open_files():
            try:
                mtime = os.path.getmtime(filename)
            except Exception:
                self.close_key(dir_key)
                return

            mtime = datetime.datetime.fromtimestamp(mtime,tz=datetime.timezone.utc)

            if (now - mtime) > self.max_idle_time:
                print('Closing idle ' + filename)
                self.close_key(dir_key)


# ============================================================================
class PerRecordWARCWriter(MultiFileWARCWriter):
    def __init__(self, *args, **kwargs):
        kwargs['max_size'] = 1
        super(PerRecordWARCWriter, self).__init__(*args, **kwargs)

