"""Segmented, crash-safe WARC capture writer for continuation manifests.

``ContinuationWriter`` writes one response/request pair per volume.  The
caller is responsible for:

* claiming a page through :class:`pywb.recorder.continuation.ContinuationManifest`;
* allocating a volume name with :meth:`ContinuationManifest.allocate_volume`;
* recording the proven capture with
  :meth:`ContinuationManifest.record_capture` after :meth:`write_capture`
  returns.

The writer never appends to an existing file.  It refuses a path that already
exists unless the path was reserved through ``allocate_volume``.  Writing
goes to a sibling ``.part`` file, which is verified and only then renamed
into place, so a process killed mid-write never leaves a file that can be
mistaken for a complete volume.
"""

import os
import uuid

from io import BytesIO

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from pywb.recorder.continuation import (
    VerificationError, iter_warc_offsets, inspect_record,
)


class VolumeConflict(Exception):
    """Raised when the requested volume already exists or is not writable."""


class InterruptedCapture(Exception):
    """Raised to simulate a process dying part-way through a response."""

    def __init__(self, written, message='capture interrupted'):
        super(InterruptedCapture, self).__init__(message)
        self.written = written


# ============================================================================
class ContinuationWriter(object):
    def __init__(self, root):
        self.root = os.path.abspath(root)

    # -- public API -------------------------------------------------------

    def write_capture(self, path, url, payload, content_type='text/html',
                      status='200 OK', request_headers=None,
                      headers=None, clock=None, interrupt_after=None):
        """Write a full response and its request into a fresh volume.

        Returns the verified capture record::

            {'filename', 'offset', 'length', 'payload_digest',
             'record_id', 'http_status'}

        ``interrupt_after`` simulates a process dying while the response is
        still being written.  It is either an absolute byte count or a
        fraction (``0 < x < 1``) of the finished volume.  The volume is
        written in full first, then truncated at that point and left under
        its final name before :class:`InterruptedCapture` is raised.  The
        torn volume has no complete gzip trailer, so it cannot be verified
        or indexed as evidence; its readable prefix is preserved for
        inspection.
        """
        if not os.path.isabs(path):
            path = os.path.join(self.root, path)
        path = os.path.abspath(path)
        directory = os.path.dirname(path)
        os.makedirs(directory, exist_ok=True)

        if os.path.exists(path) and os.path.getsize(path) != 0:
            raise VolumeConflict('volume already exists: ' + path)

        # A zero-byte file is a reservation made by allocate_volume(); it is
        # replaced atomically once the complete volume has been verified.
        reserved = os.path.exists(path)

        part_path = path + '.part.' + uuid.uuid4().hex[:8]
        written = 0
        try:
            with open(part_path, 'xb') as fh:
                written = self._write_pair(fh, url, payload, content_type,
                                           status, request_headers, headers,
                                           clock)
                fh.flush()
                os.fsync(fh.fileno())

            if interrupt_after is not None:
                cut = self._interrupt_cut(part_path, interrupt_after)
                if cut is None:
                    # Simulate a process dying before anything durable was
                    # committed: no final volume is left behind at all.
                    os.unlink(part_path)
                    if reserved:
                        os.unlink(path)
                    raise InterruptedCapture(0)
                with open(part_path, 'r+b') as fh:
                    fh.truncate(cut)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(part_path, path)
                _fsync_dir(directory)
                raise InterruptedCapture(cut)

            info = self._verify(part_path, url)
            if reserved and os.path.getsize(path) != 0:
                raise VolumeConflict('volume reservation was filled: ' + path)
            os.replace(part_path, path)
            _fsync_dir(directory)

            return {
                'filename': os.path.relpath(path, self.root),
                'offset': info['start'],
                'length': info['length'],
                'payload_digest': info['payload_digest'],
                'record_id': info['record_id'],
                'http_status': info['http_status'],
            }
        finally:
            if os.path.exists(part_path):
                os.unlink(part_path)

    @staticmethod
    def _interrupt_cut(part_path, interrupt_after):
        """Choose a cut inside the first record so it can never be complete.

        A fraction is interpreted against the first gzip member, so the cut
        lands part-way through the response regardless of request records
        that follow it.  ``'none'`` means no volume is left on disk.
        """
        if interrupt_after == 'none':
            return None

        with open(part_path, 'rb') as fh:
            ranges = list(iter_warc_offsets(fh))
        total = os.path.getsize(part_path)
        first_end = ranges[0][1] if ranges else total

        if 0 < interrupt_after < 1:
            cut = int(first_end * interrupt_after)
        else:
            cut = int(interrupt_after)

        # Never let the cut land on (or past) a member boundary.
        cut = max(1, min(cut, first_end - 1, total - 1))
        return cut

    def write_volume(self, path, records):
        """Write a multi-record volume from ``(record_type, uri, body)`` tuples.

        Used to build offline legacy fixtures.  The volume is written through
        the same verified rename path as a live capture, so tests can rely on
        the same byte-level guarantees.
        """
        if not os.path.isabs(path):
            path = os.path.join(self.root, path)
        path = os.path.abspath(path)
        os.makedirs(os.path.dirname(path), exist_ok=True)

        part_path = path + '.part.' + uuid.uuid4().hex[:8]
        try:
            with open(part_path, 'xb') as fh:
                writer = WARCWriter(fh, gzip=True)
                for record_type, uri, body in records:
                    if record_type == 'request':
                        headers = StatusAndHeaders(
                            'GET / HTTP/1.1', [('Host', 'localhost')],
                            protocol='', is_http_request=True)
                        record = writer.create_warc_record(
                            uri, 'request', http_headers=headers)
                    else:
                        headers = StatusAndHeaders(
                            '200 OK',
                            [('Content-Type', 'text/html'),
                             ('Content-Length', str(len(body)))],
                            protocol='HTTP/1.1')
                        record = writer.create_warc_record(
                            uri, record_type, payload=BytesIO(body),
                            length=len(body), http_headers=headers,
                            warc_content_type='application/http; msgtype=response')
                    writer.write_record(record)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(part_path, path)
            _fsync_dir(os.path.dirname(path))
            return path
        finally:
            if os.path.exists(part_path):
                os.unlink(part_path)

    # -- internals --------------------------------------------------------

    def _write_pair(self, fh, url, payload, content_type, status,
                    request_headers, headers, clock):
        writer = WARCWriter(fh, gzip=True)
        if clock is not None:
            writer._make_warc_date = clock

        http_headers = StatusAndHeaders(
            status,
            list((headers or [])),
            protocol='HTTP/1.1',
        )

        resp = writer.create_warc_record(
            url, 'response',
            payload=BytesIO(payload),
            length=len(payload),
            http_headers=http_headers,
            warc_content_type='application/http; msgtype=response',
        )

        req_http = StatusAndHeaders(
            'GET / HTTP/1.1',
            list((request_headers or [('Host', 'localhost')])),
            protocol='',
            is_http_request=True,
        )
        req = writer.create_warc_record(url, 'request', http_headers=req_http)

        writer.write_request_response_pair(req, resp)
        return len(payload)

    @staticmethod
    def _verify(path, url):
        with open(path, 'rb') as fh:
            ranges = list(iter_warc_offsets(fh))
            if not ranges:
                raise VerificationError('volume has no complete gzip members')

            for start, end in ranges:
                info = inspect_record(fh, start, end)
                if info['type'] == 'response' and info['uri'] == url:
                    return info

        raise VerificationError('volume does not contain a response for ' + url)


def _fsync_dir(path):
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)
