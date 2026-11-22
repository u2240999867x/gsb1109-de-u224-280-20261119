"""Read evidence for archived representations.

Every claim about a capture has to be backed by actually reading a record
out of a WARC volume.  The fact that a file exists on disk, or that a CDX
line describes a record, is not evidence on its own.
"""

import os

from warcio.archiveiterator import ArchiveIterator
from warcio.recordloader import ArchiveLoadFailed

from .digests import header_digest, payload_digest, representation_digest


class EvidenceMissing(Exception):
    """Raised when no readable record can prove the claimed representation.

    The error is deliberately retryable: it carries the coordinates that
    were attempted so a later run (after a migration finishes, or a volume
    is restored) can verify them.  Callers must never fall back to a
    default candidate when this is raised.
    """

    def __init__(self, message, candidates=None):
        super().__init__(message)
        self.candidates = list(candidates or [])


def _read_full(record):
    """Consume the record and return status, headers, body bytes."""
    http_headers = record.http_headers
    statusline = http_headers.statusline
    headers = [(name, value) for name, value in http_headers.headers]
    body = record.content_stream().read()
    return statusline, headers, body


def _evidence_from_record(record, iterator):
    """Build the evidence dict from a record after its body is consumed."""
    statusline, headers, body = _read_full(record)
    return {
        'volume': None,
        'offset': iterator.offset,
        'length': iterator.get_record_length(),
        'warc_record_id': record.rec_headers.get_header('WARC-Record-ID'),
        'warc_date': record.rec_headers.get_header('WARC-Date'),
        'target_uri': record.rec_headers.get_header('WARC-Target-URI'),
        'status': statusline,
        'headers': headers,
        'body': body,
        'payload_digest': payload_digest(body),
        'header_digest': header_digest(headers),
        'representation_digest':
            representation_digest(statusline, headers, body),
    }


def _open_volume(volume_path, offset):
    if not os.path.isfile(volume_path):
        raise EvidenceMissing(
            'volume not readable: {0}'.format(volume_path),
            candidates=[{'volume': volume_path, 'offset': offset}])
    try:
        return open(volume_path, 'rb')
    except OSError as exc:
        raise EvidenceMissing(
            'volume not openable: {0} ({1})'.format(volume_path, exc),
            candidates=[{'volume': volume_path, 'offset': offset}])


def read_evidence(volume_path, offset, expected_length=None):
    """Read the record exactly at ``offset`` and return evidence for it.

    The returned dict contains the coordinates, WARC identifiers, the actual
    response status/headers/body and all three digests, all derived from the
    bytes on disk.
    """
    stream = _open_volume(volume_path, offset)
    try:
        try:
            iterator = ArchiveIterator(stream)
            for record in iterator:
                # iterator.offset is the record start; it must be captured
                # before the body is read.  get_record_offset() would
                # consume the stream early, leaving an empty payload.
                start = iterator.offset
                if record.rec_type != 'response' or start != offset:
                    record.raw_stream.read()
                    continue
                evidence = _evidence_from_record(record, iterator)
                evidence['volume'] = volume_path
                return evidence
        except (ArchiveLoadFailed, OSError, EOFError) as exc:
            raise EvidenceMissing(
                'record unreadable at {0}@{1}: {2}'.format(
                    volume_path, offset, exc),
                candidates=[{'volume': volume_path, 'offset': offset}])
    finally:
        stream.close()

    raise EvidenceMissing(
        'no response record at offset {0} in {1}'.format(offset, volume_path),
        candidates=[{'volume': volume_path, 'offset': offset}])


def scan_volume(volume_path):
    """Yield one evidence dict per response record in a volume.

    Used to build a volume catalogue (the migration-stage signal), not for
    replay: replay only opens coordinates registered for an identifier.
    """
    stream = _open_volume(volume_path, None)
    try:
        try:
            iterator = ArchiveIterator(stream)
            for record in iterator:
                if record.rec_type != 'response':
                    continue
                evidence = _evidence_from_record(record, iterator)
                evidence['volume'] = volume_path
                yield evidence
        except (ArchiveLoadFailed, OSError, EOFError) as exc:
            raise EvidenceMissing(
                'volume scan failed for {0}: {1}'.format(volume_path, exc))
    finally:
        stream.close()
