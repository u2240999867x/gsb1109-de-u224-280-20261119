"""Open WARC records and decide what the archive actually proves.

Two facts are deliberately kept apart:

* the *capture time* is the ``WARC-Date`` of the candidate's own record.  A
  revisit is a capture of its own on its own ``WARC-Date``; the payload it
  refers to may well be from the previous day.
* the *payload* is located through the revisit headers.  When the referred
  record's ``WARC-Date`` is a different day, it is reported as the body
  source, but it does not move the capture to that other day.

A candidate is verified only after its header record was opened and its
``WARC-Date`` parsed; an unreadable offset, a missing file, a truncated record
or a header without a valid date leaves it unverified.
"""

from warcio.timeutils import iso_date_to_timestamp

from pywb.utils.wbexception import AccessException
from pywb.warcserver.resource.blockrecordloader import BlockArcWarcRecordLoader

from .model import Verification, timestamp_sec


class ArchiveReadError(Exception):
    pass


class WarcVerifier(object):
    """Open individual WARC records by CDX filename/offset/length.

    ``path_resolver`` maps a CDX filename to the local path(s) it can be read
    from.  ``revisit_resolver`` maps a revisit header to the exact record that
    holds its payload; it is only called when the candidate itself does not
    carry ``body_ref``.
    """

    def __init__(self, path_resolver=None, record_loader=None,
                 revisit_resolver=None):
        self.path_resolver = path_resolver or (lambda filename: [filename])
        self.record_loader = record_loader or BlockArcWarcRecordLoader()
        self.revisit_resolver = revisit_resolver

    def _paths(self, filename):
        paths = self.path_resolver(filename)
        if isinstance(paths, str):
            return [paths]
        return list(paths or [])

    def open_record(self, ref):
        last = None
        for path in self._paths(ref.filename):
            if not path:
                continue
            try:
                record = self.record_loader.load(path, ref.offset, ref.length,
                                                 no_record_parse=False)
                return record
            except AccessException:
                raise
            except Exception as e:
                last = e

        raise ArchiveReadError(str(last) if last else
                               'no archive path for ' + ref.filename)

    def inspect_header(self, ref):
        record = self.open_record(ref)
        try:
            headers = record.rec_headers
            warc_date = headers.get_header('WARC-Date')
            try:
                timestamp = iso_date_to_timestamp(warc_date)
            except Exception:
                timestamp = None

            return {'warc_type': headers.get_header('WARC-Type'),
                    'warc_date': warc_date,
                    'timestamp': timestamp,
                    'headers': dict(headers.headers)}
        finally:
            _close(record)

    def verify(self, candidate, day, at_sec, open_body=True):
        """Return a Verification for one candidate on the requested UTC day.

        With ``open_body=False`` a same-day revisit stops after its header:
        the capture date is proven and the result has status
        ``header-verified``.  Call :meth:`resolve_body` later to finish the
        payload lookup without reopening the header.
        """
        if candidate.access not in (None, '', 'allow'):
            raise AccessException(msg={'access': candidate.access,
                                       'access_status': 451},
                                  url=candidate.url)

        ref = candidate.header_ref
        result = Verification(status='unreadable', header_ref=ref,
                              true_timestamp=candidate.cdx_timestamp)

        try:
            header = self.inspect_header(ref)
            result.opens += 1
        except AccessException:
            raise
        except ArchiveReadError as e:
            result.detail = 'header unreadable: ' + str(e)
            return result
        except Exception as e:
            result.detail = 'header unreadable: ' + repr(e)
            return result

        timestamp = header['timestamp']
        result.warc_date = header['warc_date']
        result.warc_type = header['warc_type']

        if not timestamp:
            result.detail = 'record has no valid WARC-Date'
            return result

        result.true_timestamp = timestamp
        result.distance = abs(at_sec - timestamp_sec(timestamp))
        if timestamp[:len(day)] != day:
            result.status = 'wrong-day'
            result.detail = 'record date {0} is not on {1}'.format(
                header['warc_date'], day)
            return result

        if header['warc_type'] == 'revisit' and not open_body:
            result.status = 'header-verified'
            result.warc_type = 'revisit'
            result.detail = 'revisit payload not resolved in this round'
            return result

        if header['warc_type'] == 'revisit':
            body_ref, body_date, detail = self._resolve_revisit(candidate,
                                                                 header)
            result.opens += 1 if body_ref is not None else 0
            if body_ref is None:
                result.status = 'unreadable'
                result.detail = detail or 'revisit payload is not resolvable'
                return result
            result.body_ref = body_ref
            result.body_date = body_date
            result.body_kind = 'revisit'
        else:
            result.body_ref = ref
            result.body_date = timestamp
            result.body_kind = 'response'

        result.status = 'verified'
        result.replayable = True
        return result

    def resolve_body(self, candidate, verification):
        """Finish a ``header-verified`` revisit in a later round."""
        if verification.status != 'header-verified':
            return verification

        try:
            header = self.inspect_header(candidate.header_ref)
        except Exception as e:
            verification.status = 'unreadable'
            verification.detail = 'revisit header re-read failed: ' + repr(e)
            return verification

        verification.opens += 1
        body_ref, body_date, detail = self._resolve_revisit(candidate, header)
        verification.opens += 1 if body_ref is not None else 0
        if body_ref is None:
            verification.status = 'unreadable'
            verification.detail = detail or 'revisit payload is not resolvable'
            return verification

        verification.body_ref = body_ref
        verification.body_date = body_date
        verification.body_kind = 'revisit'
        verification.status = 'verified'
        verification.replayable = True
        verification.detail = ''
        return verification

    def _resolve_revisit(self, candidate, header):
        if candidate.body_ref:
            body = self.inspect_header(candidate.body_ref)
            if not body['timestamp']:
                return None, None, 'revisit payload has no valid WARC-Date'
            return candidate.body_ref, body['timestamp'], ''

        headers = header['headers']
        refers_to_date = headers.get('WARC-Refers-To-Date')
        refers_to_uri = headers.get('WARC-Refers-To-Target-URI')

        if not refers_to_date:
            return None, None, 'revisit has no WARC-Refers-To-Date'

        try:
            iso_date_to_timestamp(refers_to_date)
        except Exception:
            return None, None, 'revisit has an invalid WARC-Refers-To-Date'

        if not self.revisit_resolver:
            return None, None, 'revisit payload reference is not resolvable'

        resolved = self.revisit_resolver(candidate, refers_to_uri,
                                         refers_to_date,
                                         header)
        if resolved is None:
            return None, None, 'revisit payload reference is not resolvable'

        body = self.inspect_header(resolved)
        if not body['timestamp']:
            return None, None, 'revisit payload has no valid WARC-Date'

        return resolved, body['timestamp'], ''


def _close(record):
    try:
        record.raw_stream.close()
    except Exception:
        pass


def make_local_resolver(paths):
    """Build a path resolver from a directory or a filename mapping."""
    import os

    def resolve(filename):
        if isinstance(paths, dict):
            return paths.get(filename, filename)
        if os.path.isdir(paths):
            return os.path.join(paths, os.path.basename(filename))
        return filename

    return resolve


__all__ = ['WarcVerifier', 'ArchiveReadError', 'make_local_resolver',
           'RecordRef']
