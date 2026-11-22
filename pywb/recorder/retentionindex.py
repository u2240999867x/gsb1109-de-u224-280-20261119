"""
Retention aware dedup index for offline WARC recording.

Unlike :class:`pywb.recorder.redisindexer.WritableRedisIndexer`, this index
works against plain CDX/CDXJ files on a shared directory and never trusts a
candidate just because it is listed in an index.  Every revisit candidate is
verified before it is offered to the writer:

* the backing volume must still be *active* in the retention ledger
  (a volume withdrawn by rotation is skipped, even if its CDX lingers);
* the record must actually load at the CDX offset/length
  (a stale CDX pointing at the wrong offset is skipped);
* the loaded record's payload digest must match the new capture.

Only when every check passes is the concrete original record handed to the
writer, so a revisit can never silently resolve against a different WARC that
happens to carry identical bytes.
"""

import json
import os
import uuid
from io import BytesIO

import portalocker
from warcio.timeutils import iso_date_to_timestamp

from pywb.indexer.cdxindexer import write_cdx_index
from pywb.recorder.filters import WriteRevisitDupePolicy
from pywb.utils.io import no_except_close
from pywb.warcserver.resource.blockrecordloader import BlockArcWarcRecordLoader


def _digest_base32(value):
    if not value:
        return ''
    if ':' in value:
        value = value.split(':', 1)[1]
    return value.upper()


class RetentionDedupIndex(object):
    def __init__(self, sources, ledger, warc_dir, record_index_path=None,
                 dupe_policy=None):
        """
        :param sources: dict of ``name -> BaseIndexSource`` used to find
            existing captures (offline CDX/CDXJ sources)
        :param ledger: :class:`RetentionLedger` tracking volume availability
        :param warc_dir: directory holding the volume files, used as a
            fallback when a filename is not registered in the ledger
        :param record_index_path: CDXJ file that receives the CDX evidence
            for every capture written by this recorder
        """
        self.sources = sources
        self.ledger = ledger
        self.warc_dir = warc_dir
        self.record_index_path = record_index_path
        self.dupe_policy = dupe_policy or WriteRevisitDupePolicy()
        self.record_loader = BlockArcWarcRecordLoader()
        # volumes known to be gone (withdrawn); entries pointing at a wrong
        # offset are tracked separately, since another index entry for the
        # same volume may still have the correct offset
        self.excluded_volumes = set()
        self.failed_locations = set()

    # ------------------------------------------------------------------
    # candidate verification
    # ------------------------------------------------------------------
    def _volume_path(self, volume_name):
        path = self.ledger.volume_path(volume_name)
        if path and os.path.exists(path):
            return path

        candidate = os.path.join(self.warc_dir, volume_name)
        if os.path.exists(candidate):
            return candidate

        return None

    def verify_candidate(self, cdx, expected_digest):
        """Return True only when the CDX points at a readable, matching record."""
        volume_name = cdx['filename']
        location = (volume_name, str(cdx['offset']), str(cdx.get('length', '-')))

        if volume_name in self.excluded_volumes:
            return False

        if location in self.failed_locations:
            return False

        if not self.ledger.is_available(volume_name):
            # the volume itself is gone, every entry pointing at it is stale
            self.excluded_volumes.add(volume_name)
            return False

        path = self._volume_path(volume_name)
        if not path:
            self.excluded_volumes.add(volume_name)
            return False

        try:
            record = self.record_loader.load(path,
                                             int(cdx['offset']),
                                             cdx.get('length', '-'))
        except Exception:
            # wrong offset, truncated member, corrupt gzip ... only this
            # index entry is bad; another entry for the same volume may work
            self.failed_locations.add(location)
            return False

        try:
            if record.rec_type != 'response':
                self.failed_locations.add(location)
                return False

            actual = record.rec_headers.get_header('WARC-Payload-Digest')
            matches = (_digest_base32(actual) ==
                       _digest_base32(expected_digest))
            if not matches:
                self.failed_locations.add(location)
            return matches
        finally:
            no_except_close(record.raw_stream)

    def lookup_revisit(self, lookup_params, digest, url, iso_dt):
        # query each source in configuration order instead of merging by
        # closest timestamp: when several volumes hold the identical payload,
        # source order must determine which concrete record is chosen, and
        # that provenance is what ends up in the revisit's orig.* fields
        from pywb.warcserver.index.query import CDXQuery
        query = CDXQuery({'url': url,
                          'closest': iso_date_to_timestamp(iso_dt)})
        qparams = query.params
        want = _digest_base32(digest)

        for key, value in lookup_params.items():
            if key.startswith('param.'):
                qparams[key] = value

        for source in self.sources.values():
            for cdx in source.load_index(qparams):
                if cdx.get('mime') == 'warc/revisit':
                    continue
                if _digest_base32(cdx.get('digest', '')) != want:
                    continue

                if not self.dupe_policy(cdx, qparams):
                    continue

                if not self.verify_candidate(cdx, digest):
                    continue

                return ('revisit', cdx['url'],
                        self._iso_date(cdx['timestamp']), dict(cdx))

        return None

    @staticmethod
    def _iso_date(timestamp):
        return '{0}-{1}-{2}T{3}:{4}:{5}Z'.format(timestamp[0:4],
                                                 timestamp[4:6],
                                                 timestamp[6:8],
                                                 timestamp[8:10],
                                                 timestamp[10:12],
                                                 timestamp[12:14])

    # ------------------------------------------------------------------
    # recording side hooks, called by MultiFileWARCWriter
    # ------------------------------------------------------------------
    def add_warc_file(self, full_filename, params):
        volume_name = os.path.basename(full_filename)
        self.ledger.register_volume(volume_name, full_filename)

    def exclude_volume(self, volume_name):
        """Forget a volume that became unavailable so it is not retried."""
        self.excluded_volumes.add(volume_name)

    def add_urls_to_index(self, stream, params, filename, length):
        base_filename = os.path.basename(filename)

        cdxout = BytesIO()
        write_cdx_index(cdxout, stream, base_filename,
                        cdxj=True, append_post=True,
                        writer_cls=params.get('writer_cls'))

        lines = [line for line in cdxout.getvalue().rstrip().split(b'\n')
                 if line]

        orig = params.get('_revisit_orig')
        if orig:
            lines = [self._annotate_revisit(line, orig) for line in lines]

        if self.record_index_path and lines:
            self._append_index(lines)

        return lines

    def _annotate_revisit(self, line, orig):
        text = line.decode('utf-8')
        key, timestamp, json_part = text.split(' ', 2)
        fields = json.loads(json_part)
        if fields.get('mime') == 'warc/revisit':
            fields['orig.filename'] = orig['filename']
            fields['orig.offset'] = str(orig['offset'])
            fields['orig.length'] = str(orig.get('length', '-'))
        return '{0} {1} {2}'.format(key, timestamp,
                                    json.dumps(fields, sort_keys=True)).encode('utf-8')

    def _append_index(self, lines):
        directory = os.path.dirname(self.record_index_path)
        try:
            os.makedirs(directory)
        except OSError:
            pass

        with open(self.record_index_path, 'ab') as fh:
            portalocker.lock(fh, portalocker.LOCK_EX)
            try:
                for line in lines:
                    fh.write(line + b'\n')
                fh.flush()
                os.fsync(fh.fileno())
            finally:
                if hasattr(portalocker, 'unlock'):
                    portalocker.unlock(fh)
                else:  # pragma: no cover - older portalocker
                    portalocker.lock(fh, portalocker.LOCK_UN)

    def new_reference_id(self):
        return uuid.uuid4().hex
