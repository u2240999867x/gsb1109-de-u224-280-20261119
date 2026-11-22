"""Continuation manifest for multi-machine WARC capture hand-off.

The manifest lives in a shared, filesystem-visible directory and is the only
coordination point between workers.  Each worker opens the manifest file
afresh for every operation and guards mutations with an advisory file lock,
so no in-process state is shared or required.  All observations about WARC
volumes and index entries are made through the filesystem, which keeps a
restarted or competing worker honest about what has actually been captured.

A *page* moves through the following durable states:

``pending``
    No capture has been proven.  Either no owner has claimed the page yet,
    or the previous claim was abandoned (for example another worker took
    over) or its response was interrupted before it was durably captured.

``writing``
    A worker has an exclusive claim to produce a capture.  The claim records
    a monotonically increasing attempt number and the owner token.

``captured``
    A complete WARC response exists in a volume.  The manifest records the
    volume, its byte range and the payload digest so the capture can be
    re-read and verified without touching any index.

``fenced``
    The capture is proven complete, but publishing it to the index has not
    yet happened.

``indexed``
    The capture is proven complete and its index entry has been registered.

``discarded``
    A worker produced a volume but the capture could not be proven complete
    (for example the response was cut short).  That volume can never be
    reused for a *different* page; it is kept on disk for inspection.
"""

import base64
import datetime
import gzip
import hashlib
import json
import os
import re
import uuid
import zlib

import portalocker
from warcio.statusandheaders import StatusAndHeadersParser

from pywb.utils.canonicalize import canonicalize


# ============================================================================
# Errors

class ManifestError(Exception):
    """Raised for manifests that are missing, corrupt or incompatible."""


class ClaimError(Exception):
    """Raised when a claim cannot be granted or refreshed."""


class VerificationError(Exception):
    """Raised when recorded capture evidence cannot be verified."""


class IndexFenced(Exception):
    """Raised when an index submission is stale or already registered."""

    def __init__(self, reason, page=None):
        super(IndexFenced, self).__init__(reason)
        self.reason = reason
        self.page = page


# ============================================================================
# Durable JSON storage

class _AtomicJson(object):
    """Atomic JSON documents with advisory locking.

    Reads never take the lock: they open the file, read and close it.  Writers
    take an exclusive lock on a sidecar ``.lock`` file, re-read the document,
    apply a mutation and replace the target atomically with ``os.replace``.
    The lock sidecar guarantees that the read-modify-write cycle is serialised
    even when the target file is being replaced underneath.
    """

    def __init__(self, path):
        self.path = path
        self.lock_path = path + '.lock'

    def read(self):
        try:
            with open(self.path, 'rb') as fh:
                data = fh.read()
        except FileNotFoundError:
            raise ManifestError('missing manifest: ' + self.path)

        if not data.strip():
            raise ManifestError('empty manifest: ' + self.path)

        try:
            return json.loads(data.decode('utf-8'))
        except (ValueError, UnicodeDecodeError) as exc:
            raise ManifestError('invalid manifest {0}: {1}'.format(self.path, exc))

    def mutate(self, func):
        os.makedirs(os.path.dirname(os.path.abspath(self.path)), exist_ok=True)
        with portalocker.Lock(self.lock_path, mode='a+', timeout=30):
            try:
                data = self.read()
            except ManifestError:
                data = None
            result = func(data)
            if result is None:
                return None
            if isinstance(result, tuple):
                document, value = result
                self._replace(document)
                return value
            self._replace(result)
            return result

    def _replace(self, data):
        tmp = '{0}.tmp.{1}'.format(self.path, uuid.uuid4().hex)
        try:
            with open(tmp, 'wb') as fh:
                fh.write(json.dumps(data, indent=2, sort_keys=True).encode('utf-8'))
                fh.write(b'\n')
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self.path)
            _fsync_dir(os.path.dirname(os.path.abspath(self.path)))
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)


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


# ============================================================================
# WARC inspection helpers

_RECORD_START_RX = re.compile(br'(?:^|\n)(WARC/1\.[01]\r?\n)')


def _now_rfc3339():
    return _utcnow().isoformat().replace('+00:00', 'Z')


def _utcnow():
    # Kept in one place so tests can freeze it with a fake clock.
    return datetime.datetime.now(datetime.timezone.utc)


def iter_warc_offsets(fh):
    """Yield ``(start, end)`` for every gzip member in a block-gzipped WARC.

    ``end`` is exclusive and points at the first byte of the following
    member.  The final member is only yielded when its gzip trailer is
    complete, so a torn tail is never reported as a record.
    """
    fh.seek(0, 2)
    size = fh.tell()
    start = 0
    while start < size:
        fh.seek(start)
        decomp = zlib.decompressobj(16 + zlib.MAX_WBITS)
        chunk_size = 65536
        try:
            while True:
                chunk = fh.read(min(chunk_size, size - fh.tell()))
                if not chunk:
                    break
                decomp.decompress(chunk)
                if decomp.eof:
                    break
            if not decomp.eof:
                return
        except zlib.error:
            return

        consumed = fh.tell() - len(decomp.unused_data)
        yield start, consumed
        start = consumed


def inspect_record(fh, start, end):
    """Parse and fully verify one gzip member.

    Returns a dict with the WARC type, target URI, record id, WARC date,
    payload digest, HTTP status, payload length and digest-check status.
    Any structural failure (bad gzip, bad headers, short payload, digest
    mismatch) raises :class:`VerificationError`.
    """
    fh.seek(start)
    raw = fh.read(end - start)
    if not raw:
        raise VerificationError('empty record range at {0}'.format(start))

    try:
        data = gzip.decompress(raw)
    except (OSError, EOFError, zlib.error) as exc:
        raise VerificationError('torn gzip member at {0}: {1}'.format(start, exc))

    match = _RECORD_START_RX.match(data)
    if not match:
        raise VerificationError('no WARC start line at offset {0}'.format(start))

    header_end = data.find(b'\r\n\r\n')
    if header_end < 0:
        raise VerificationError('unterminated WARC headers at {0}'.format(start))

    headers = _parse_warc_headers(data[:header_end])
    body = data[header_end + 4:]

    try:
        content_length = int(headers.get_header('Content-Length') or 0)
    except (TypeError, ValueError):
        raise VerificationError('invalid Content-Length at {0}'.format(start))

    if len(body) < content_length:
        raise VerificationError(
            'record at {0} declares {1} body bytes but has {2}'.format(
                start, content_length, len(body)))

    trailing = body[content_length:]
    if trailing not in (b'', b'\r\n', b'\r\n\r\n'):
        raise VerificationError(
            'record at {0} has unexpected trailing bytes'.format(start))
    body = body[:content_length]

    http_headers = None
    payload = body
    rec_type = headers.get_header('WARC-Type')
    if rec_type in ('response', 'request', 'revisit'):
        http_headers, payload = _split_http(rec_type, body, headers)

    payload_digest = headers.get_header('WARC-Payload-Digest')
    if payload_digest and rec_type not in ('revisit', 'warcinfo'):
        algo, expected = _parse_digest(payload_digest)
        actual = _digest(algo, payload)
        if actual != expected:
            raise VerificationError(
                'payload digest mismatch at {0}: {1} != {2}'.format(
                    start, actual, expected))

    block_digest = headers.get_header('WARC-Block-Digest')
    if block_digest:
        # The block is the WARC body exactly as stored (HTTP headers plus
        # payload for HTTP records).
        algo, expected = _parse_digest(block_digest)
        if _digest(algo, body) != expected:
            raise VerificationError(
                'block digest mismatch at {0}'.format(start))

    return {
        'start': start,
        'end': end,
        'length': end - start,
        'type': rec_type,
        'uri': headers.get_header('WARC-Target-URI'),
        'record_id': headers.get_header('WARC-Record-ID'),
        'date': headers.get_header('WARC-Date'),
        'payload_digest': payload_digest,
        'http_status': http_headers.get_statuscode() if http_headers else '',
        'payload_length': len(payload),
    }


def _parse_warc_headers(raw_headers):
    parser = StatusAndHeadersParser(['WARC/1.0', 'WARC/1.1'], verify=False)
    lines = raw_headers.split(b'\r\n')
    first = lines[0] + b'\r\n'
    rest = b'\r\n'.join(lines[1:]) + b'\r\n\r\n'
    return parser.parse(_BytesLineReader(rest), first)


class _BytesLineReader(object):
    """Tiny line reader over an in-memory buffer for warcio's parser."""

    def __init__(self, data):
        self.data = data
        self.pos = 0

    def readline(self, *args):
        if self.pos >= len(self.data):
            return b''
        end = self.data.find(b'\n', self.pos)
        if end < 0:
            end = len(self.data)
        else:
            end += 1
        line = self.data[self.pos:end]
        self.pos = end
        return line

    def read(self, length=None):
        if length is None:
            length = len(self.data) - self.pos
        chunk = self.data[self.pos:self.pos + length]
        self.pos += len(chunk)
        return chunk


def _split_http(rec_type, body, headers):
    """Split the WARC body into parsed HTTP headers and payload bytes."""
    reader = _BytesLineReader(body)
    try:
        http_headers = StatusAndHeadersParser([], verify=False).parse(reader)
    except EOFError:
        if rec_type == 'revisit':
            return None, body
        raise VerificationError('missing HTTP headers in WARC body')

    return http_headers, body[reader.pos:]


def _parse_digest(value):
    if ':' in value:
        algo, encoded = value.split(':', 1)
    else:
        algo, encoded = 'sha1', value
    return algo, encoded


def _digest(algo, payload):
    try:
        hasher = hashlib.new(algo)
    except ValueError:
        raise VerificationError('unsupported digest algorithm: ' + algo)
    hasher.update(payload)
    return base64.b32encode(hasher.digest()).decode('ascii')


def verify_capture_file(path, offset, length, uri=None, digest=None):
    """Re-read the capture at ``[offset, offset+length)`` and verify it.

    Returns the inspection dict.  Raises :class:`VerificationError` when the
    volume, the offset or the record content does not support the claim.
    """
    try:
        fh = open(path, 'rb')
    except OSError as exc:
        raise VerificationError('cannot open volume {0}: {1}'.format(path, exc))

    with fh:
        fh.seek(0, 2)
        size = fh.tell()
        if offset < 0 or length <= 0 or offset + length > size:
            raise VerificationError(
                'capture range {0}+{1} outside volume of {2} bytes'.format(
                    offset, length, size))

        info = inspect_record(fh, offset, offset + length)

    if info['length'] != length:
        raise VerificationError(
            'gzip member at {0} is {1} bytes, expected {2}'.format(
                offset, info['length'], length))
    if uri and info['uri'] != uri:
        raise VerificationError(
            'capture at {0} is for {1}, expected {2}'.format(offset, info['uri'], uri))
    if digest and info['payload_digest']:
        if _normalise_digest(digest) != _normalise_digest(info['payload_digest']):
            raise VerificationError(
                'capture at {0} has digest {1}, expected {2}'.format(
                    offset, info['payload_digest'], digest))
    return info


def read_capture_payload(path, offset, length, uri=None, digest=None):
    """Return ``(info, payload)`` for a proven capture.

    The same structural and digest verification used by the manifest is
    applied, so callers can rely on the payload belonging to the record.
    """
    info = verify_capture_file(path, offset, length, uri=uri, digest=digest)
    with open(path, 'rb') as fh:
        fh.seek(offset)
        data = gzip.decompress(fh.read(length))
    header_end = data.find(b'\r\n\r\n')
    body = data[header_end + 4:]
    body = body[:int(_parse_warc_headers(data[:header_end])
                     .get_header('Content-Length') or len(body))]
    _, payload = _split_http(info['type'], body, None)
    return info, payload


def _normalise_digest(value):
    if value.startswith('sha1:'):
        return value
    return 'sha1:' + value


def find_capture_in_volume(path, uri, offset_hint=None):
    """Find a complete response record for ``uri`` in ``path``.

    When ``offset_hint`` is supplied it is tried first, but a wrong hint is
    never fatal: every member in the volume is inspected, and only a fully
    verified record counts as evidence.
    """
    candidates = []
    try:
        with open(path, 'rb') as fh:
            ranges = list(iter_warc_offsets(fh))
            ordered = ranges
            if offset_hint is not None:
                ordered = [r for r in ranges if r[0] == offset_hint]
                ordered += [r for r in ranges if r[0] != offset_hint]
            for start, end in ordered:
                try:
                    info = inspect_record(fh, start, end)
                except VerificationError:
                    continue
                if info['type'] != 'response' or info['uri'] != uri:
                    continue
                if not info['payload_digest']:
                    continue
                candidates.append(info)
    except OSError:
        return None

    if not candidates:
        return None

    if offset_hint is not None:
        for info in candidates:
            if info['start'] == offset_hint:
                return info
    return candidates[0]


def probe_capture(path, uri, offset=None, length=None, digest=None):
    """Verify a capture claim, tolerating a stale index offset.

    Returns a capture dict on success or ``None``.  A previously recorded
    offset/length that no longer describes a complete record for ``uri`` is
    discarded and the volume is scanned; this is what lets a continuation
    recover from an index whose offset points at the wrong place while still
    refusing torn or digest-mismatched captures.
    """
    if offset is not None and length:
        try:
            info = verify_capture_file(path, offset, length, uri=uri, digest=digest)
            return _capture_from_info(path, info)
        except VerificationError:
            pass

    info = find_capture_in_volume(path, uri, offset_hint=offset)
    if not info:
        return None
    return _capture_from_info(path, info)


def _capture_from_info(path, info):
    return {
        'filename': path,
        'offset': info['start'],
        'length': info['length'],
        'payload_digest': info['payload_digest'],
        'record_id': info['record_id'],
        'http_status': info['http_status'],
    }


# ============================================================================
# Index adapter

class JsonIndex(object):
    """Small append-only index with per-capture idempotence.

    The index is a shared JSON file guarded by the same advisory lock used by
    :class:`_AtomicJson`.  Every accepted capture is identified by the
    capture's own identity (volume, offset, length, digest), so replaying the
    same submission is a no-op while a stale submission from an earlier owner
    is rejected with :class:`IndexFenced`.
    """

    def __init__(self, path):
        self._store = _AtomicJson(path)
        self.path = path

    def read(self):
        try:
            data = self._store.read()
        except ManifestError:
            return {'entries': []}
        if isinstance(data, list):
            data = {'entries': data}
        data.setdefault('entries', [])
        return data

    def entries_for(self, uri):
        return [e for e in self.read()['entries'] if e.get('uri') == uri]

    def lookup(self, uri):
        """Return the first registered entry for ``uri``, or ``None``."""
        entries = self.entries_for(uri)
        return entries[0] if entries else None

    def locate(self, entry):
        """Re-verify an index entry and return its capture info."""
        capture = {
            'filename': entry.get('filename'),
            'offset': int(entry.get('offset') or 0),
            'length': int(entry.get('length') or 0),
            'payload_digest': entry.get('payload_digest'),
        }
        return capture

    def submit(self, page):
        """Register ``page``'s capture.

        ``page`` must include ``uri``, ``capture``, ``attempt`` and
        ``claim_token``.  Returns ``'registered'`` or ``'already'``.  Raises
        :class:`IndexFenced` if the page's current authorization does not
        cover this attempt/token or if the capture identity conflicts with a
        different payload already registered for the URI.
        """
        store = self._store

        def mutate(data):
            data = data or {'entries': []}
            entries = data.setdefault('entries', [])
            capture = page['capture']
            identity = _capture_identity(capture)

            for entry in entries:
                if entry.get('capture_id') == identity:
                    return (data, 'already')
                if entry.get('uri') == page['uri']:
                    # One page has exactly one index entry.  A second capture,
                    # even with an identical payload, means a worker treated
                    # an already-proven page as unstarted.  Refuse it rather
                    # than publish a second record, and never let a late or
                    # incomplete capture replace the proven one.
                    raise IndexFenced(
                        'page already indexed from another capture', page)

            entry = {
                'uri': page['uri'],
                'capture_id': identity,
                'attempt': page.get('attempt'),
                'claim_token': page.get('claim_token'),
                'filename': capture.get('filename'),
                'offset': capture.get('offset'),
                'length': capture.get('length'),
                'payload_digest': capture.get('payload_digest'),
                'record_id': capture.get('record_id'),
                'indexed_at': _now_rfc3339(),
            }
            entries.append(entry)
            return (data, 'registered')

        result = store.mutate(mutate)
        if isinstance(result, tuple):
            return result[1]
        return 'registered'


def _capture_identity(capture):
    parts = [
        capture.get('filename') or '',
        str(capture.get('offset')),
        str(capture.get('length')),
        _normalise_digest(capture.get('payload_digest') or '-'),
    ]
    return hashlib.sha1('|'.join(parts).encode('utf-8')).hexdigest()


# ============================================================================
# Manifest

class ContinuationManifest(object):
    """Shared continuation manifest for one batch of capture work."""

    VERSION = 1
    FILE_NAME = 'continuation-manifest.json'
    DEFAULT_LEASE = 900

    def __init__(self, root, lease_seconds=DEFAULT_LEASE, clock=None):
        self.root = os.path.abspath(root)
        self.path = os.path.join(self.root, self.FILE_NAME)
        self.store = _AtomicJson(self.path)
        self.index = JsonIndex(os.path.join(self.root, 'cdxj-index.json'))
        self.lease_seconds = lease_seconds
        self.clock = clock or _utcnow
        self.last_claim_reason = None

    # -- creation ---------------------------------------------------------

    @classmethod
    def create(cls, root, pages, lease_seconds=DEFAULT_LEASE):
        root = os.path.abspath(root)
        os.makedirs(root, exist_ok=True)
        manifest = cls(root, lease_seconds=lease_seconds)
        manifest.store._replace({
            'version': cls.VERSION,
            'created_at': _now_rfc3339(),
            'pages': [manifest._new_page(p) for p in pages],
        })
        return manifest

    def _new_page(self, spec):
        if isinstance(spec, str):
            spec = {'url': spec}
        uri = spec['url']
        urlkey = canonicalize(uri)
        return {
            'uri': uri,
            'urlkey': urlkey,
            'state': 'pending',
            'authorized': spec.get('authorized', True),
            'legacy': None,
            'attempts': [],
            'capture': None,
            'pending_index': None,
            'dispatches': [],
            'notes': [],
        }

    # -- reads ------------------------------------------------------------

    def load(self):
        return self.store.read()

    def page(self, uri):
        for page in self.load()['pages']:
            if page['uri'] == uri:
                return page
        raise ManifestError('unknown page: ' + uri)

    def pages(self):
        return list(self.load()['pages'])

    def status(self):
        """Per-page summary: authorization attempt, volume and pending state."""
        result = []
        for page in self.load()['pages']:
            attempt = page['attempts'][-1] if page['attempts'] else None
            capture = page.get('capture') or {}
            result.append({
                'uri': page['uri'],
                'state': page['state'],
                'attempt': attempt,
                'filename': capture.get('filename'),
                'offset': capture.get('offset'),
                'length': capture.get('length'),
                'payload_digest': capture.get('payload_digest'),
                'pending_index': page.get('pending_index'),
                'claim_token': attempt.get('claim_token') if attempt else None,
                'owner': attempt.get('owner') if attempt else None,
            })
        return result

    def status_owner(self, uri):
        page = self.page(uri)
        attempt = self._active_attempt(page)
        return attempt.get('owner') if attempt else None

    def plan(self):
        """Return one human-readable row per page.

        Each row says whether the page is proven complete, waiting for a
        writer, waiting for index publication, or still needs capture.  The
        ``remaining`` flag is the archive operator's pending list.
        """
        rows = []
        for page in self.load()['pages']:
            attempt = page['attempts'][-1] if page['attempts'] else None
            capture = page.get('capture') or {}
            state = page['state']
            rows.append({
                'uri': page['uri'],
                'urlkey': page['urlkey'],
                'status': _PLAN_STATUS.get(state, state),
                'state': state,
                'authorized': page.get('authorized', True),
                'attempt': attempt.get('attempt') if attempt else None,
                'claim_token': attempt.get('claim_token') if attempt else None,
                'owner': attempt.get('owner') if attempt else None,
                'filename': capture.get('filename'),
                'offset': capture.get('offset'),
                'length': capture.get('length'),
                'payload_digest': capture.get('payload_digest'),
                'legacy': bool(capture.get('legacy')),
                'legacy_indexed_offset': (page.get('legacy') or {}).get('indexed_offset'),
                'legacy_offset_repaired': (page.get('legacy') or {}).get('offset_repaired', False),
                'remaining': state not in ('indexed',),
                'pending_index': page.get('pending_index'),
                'attempt_count': len(page['attempts']),
                'dispatch_count': len(page.get('dispatches', [])),
                'attempt_volume': attempt.get('volume') if attempt else None,
                'rejected_names': list(attempt.get('rejected_names', []))
                                  if attempt else [],
                'attempts': [self._attempt_summary(a)
                             for a in page['attempts']],
            })
        return rows

    @staticmethod
    def _attempt_summary(attempt):
        return {
            'attempt': attempt.get('attempt'),
            'owner': attempt.get('owner'),
            'claim_token': attempt.get('claim_token'),
            'state': attempt.get('state'),
            'volume': attempt.get('volume'),
            'rejected_names': list(attempt.get('rejected_names', [])),
            'supersedes': attempt.get('supersedes'),
            'dispatch_id': attempt.get('dispatch_id'),
            'claimed_at': attempt.get('claimed_at'),
        }

    # -- legacy / batch bootstrap ----------------------------------------

    def register_completed(self, uri, filename, offset=None, length=None,
                           payload_digest=None):
        """Seed a page from a previously completed capture.

        This never claims, rewrites or appends the legacy volume.  It probes
        the volume for a complete response for ``uri``.  When ``offset`` is a
        stale index pointer it is ignored and the volume is scanned; when the
        volume only has a torn or unrelated record, seeding fails and the
        page remains pending.
        """
        if uri not in [p['uri'] for p in self.load()['pages']]:
            raise ManifestError('unknown page: ' + uri)

        full_path = self._resolve(filename)
        capture = probe_capture(full_path, uri, offset, length, payload_digest)
        if not capture:
            raise VerificationError(
                'no complete capture for {0} in {1}'.format(uri, filename))

        def mutate(data):
            page = self._find(data, uri)
            if page['state'] == 'indexed':
                return None
            page['capture'] = {
                'filename': self._rel(full_path),
                'offset': capture['offset'],
                'length': capture['length'],
                'payload_digest': capture['payload_digest'],
                'record_id': capture.get('record_id'),
                'http_status': capture.get('http_status'),
                'verified_at': _now_rfc3339(),
                'legacy': True,
                'attempt': None,
                'claim_token': None,
            }
            page['state'] = 'captured'
            page['legacy'] = {
                'filename': self._rel(full_path),
                'indexed_offset': offset,
                'indexed_length': length,
                'indexed_digest': payload_digest,
                'offset_repaired': offset is not None and offset != capture['offset'],
            }
            page['pending_index'] = {
                'capture_id': _capture_identity(page['capture']),
                'attempt': None,
                'claim_token': None,
            }
            return data

        self.store.mutate(mutate)
        return self.page(uri)['capture']

    def bootstrap(self, pages, index=None):
        """Seed completed pages and publish every resolvable capture.

        ``pages`` is a mapping of URI to either a capture dict
        (``filename``/``offset``/``length``/``payload_digest``) or ``None``
        for a page that must be captured afresh.  Legacy volumes are opened
        read-only; nothing is written to them.  Returns a per-page plan.
        """
        for uri, spec in pages.items():
            if not spec:
                continue
            try:
                self.register_completed(uri,
                                        spec['filename'],
                                        spec.get('offset'),
                                        spec.get('length'),
                                        spec.get('payload_digest'))
            except VerificationError as exc:
                self._note(uri, 'bootstrap-not-proven', str(exc))
            else:
                capture = self.page(uri)['capture']
                self._set_legacy_index(uri, capture, spec)

        for uri in pages:
            page = self.page(uri)
            if page['state'] == 'captured':
                self._promote_captured(uri)
            if self.page(uri)['state'] == 'fenced':
                try:
                    self._publish(uri, self.page(uri), index or self.index)
                except IndexFenced as exc:
                    self._note(uri, 'index-fenced', exc.reason)

        return self.plan()

    def bootstrap_from_index(self, entries, index=None):
        """Seed a manifest from a legacy CDX(J) index.

        ``entries`` is an iterable of ``(uri, capture-dict)`` pairs, usually
        produced by :func:`read_cdx_entries`.  Each capture is verified
        against its volume; a stale offset simply causes the volume to be
        scanned.  Missing or torn captures leave the page pending.

        Returns a mapping with ``plan`` and per-page ``rejected`` reasons so
        the operator can see which index rows were not proven.
        """
        pages = {}
        rejected = []
        ignored = []
        known = {p['uri'] for p in self.load()['pages']}
        for uri, spec in entries:
            if uri not in known:
                # Legacy indexes cover more than this continuation batch.
                # Rows for pages outside the manifest are kept out of the
                # plan rather than treated as missing work.
                ignored.append(uri)
                continue
            pages[uri] = spec

        for uri, spec in pages.items():
            if not spec:
                continue
            try:
                capture = self.register_completed(uri,
                                                   spec['filename'],
                                                   spec.get('offset'),
                                                   spec.get('length'),
                                                   spec.get('payload_digest'))
            except VerificationError as exc:
                rejected.append({'uri': uri, 'reason': str(exc),
                                 'indexed': spec})
                self._note(uri, 'bootstrap-not-proven', str(exc))
            else:
                self._set_legacy_index(uri, capture, spec)

        for uri in pages:
            if self.page(uri)['state'] == 'captured':
                self._promote_captured(uri)
            if self.page(uri)['state'] == 'fenced':
                try:
                    self._publish(uri, self.page(uri), index or self.index)
                except IndexFenced as exc:
                    self._note(uri, 'index-fenced', exc.reason)

        return {'plan': self.plan(), 'rejected': rejected,
                'ignored': sorted(ignored)}

    def _set_legacy_index(self, uri, capture, spec):
        """Point the local index at a verified legacy capture.

        A legacy row whose offset was repaired is recorded with the *verified*
        offset; the original index position stays in ``page['legacy']``.
        """
        entry = dict(capture)
        entry['uri'] = uri
        entry['legacy'] = True
        entry['indexed_offset'] = spec.get('offset')
        entry['offset_repaired'] = spec.get('offset') != capture.get('offset')
        try:
            self.index.submit({
                'uri': uri,
                'capture': entry,
                'attempt': None,
                'claim_token': None,
            })
        except IndexFenced:
            pass

    def _note(self, uri, event, detail):
        def mutate(data):
            page = self._find(data, uri)
            page['notes'].append({
                'at': _now_rfc3339(),
                'event': event,
                'detail': detail,
            })
            return data

        self.store.mutate(mutate)

    # -- adoption ---------------------------------------------------------

    def adopt(self, uri, claim_token=None):
        """Adopt a complete capture left by a writer that died before it could
        record the page.

        Used by a takeover: the new owner inspects the volumes for a verified
        response for ``uri`` and, if there is one, registers it instead of
        capturing the page again.  Torn volumes and tombstoned volumes are
        never adopted.
        """
        outcome = {}

        def mutate(data):
            page = self._find(data, uri)
            attempt = page['attempts'][-1] if page['attempts'] else None

            if claim_token is not None:
                if not attempt or attempt.get('claim_token') != claim_token \
                        or attempt.get('state') != 'writing':
                    outcome['reason'] = 'claim-not-current'
                    return None

            if page['state'] in ('captured', 'fenced', 'indexed'):
                outcome['reason'] = 'already-captured'
                return None

            info, source_attempt = self._find_orphan_capture(page)
            if not info:
                outcome['reason'] = 'no-verified-capture'
                return None

            page['capture'] = info
            page['state'] = 'captured'
            page['pending_index'] = {
                'capture_id': _capture_identity(info),
                'attempt': source_attempt['attempt'] if source_attempt else None,
                'claim_token': source_attempt['claim_token'] if source_attempt else None,
            }
            if attempt:
                attempt['state'] = 'adopted'
                attempt['adopted_from'] = info['filename']
                attempt['volume'] = info['filename']
                attempt['captured_at'] = _now_rfc3339()
            outcome['capture'] = info
            return data

        self.store.mutate(mutate)
        if 'capture' not in outcome:
            return None

        self._promote_captured(uri)
        page = self.page(uri)
        if page['state'] == 'fenced':
            try:
                result = self._publish(uri, page, self.index)
                return {'state': 'indexed', 'result': result,
                        'capture': page['capture']}
            except IndexFenced as exc:
                return {'state': page['state'], 'reason': exc.reason}
        return {'state': self.page(uri)['state']}

    def _find_orphan_capture(self, page):
        """Scan candidate volumes for a verified response for this page.

        Volumes named by earlier attempts are tried first, then the rest of
        the archive directory newest-first.  Tombstoned volumes are skipped.
        """
        directory = os.path.join(self.root, 'archive')
        names = []
        for attempt in reversed(page['attempts']):
            volume = attempt.get('volume')
            if volume:
                names.append(os.path.basename(volume))
            preferred = attempt.get('preferred_name')
            if preferred:
                names.append(os.path.basename(preferred))

        try:
            rest = [n for n in os.listdir(directory) if n.endswith('.warc.gz')]
        except OSError:
            rest = []
        rest.sort(key=lambda n: _mtime(os.path.join(directory, n)), reverse=True)
        names += rest

        seen = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            path = os.path.join(directory, name)
            if self.tombstoned(path):
                continue
            info = find_capture_in_volume(path, page['uri'])
            if not info:
                continue
            source = None
            for attempt in reversed(page['attempts']):
                if os.path.basename(attempt.get('preferred_name') or '') == name:
                    source = attempt
                    break
            return {
                'filename': self._rel(path),
                'offset': info['start'],
                'length': info['length'],
                'payload_digest': info['payload_digest'],
                'record_id': info['record_id'],
                'http_status': info['http_status'],
                'verified_at': _now_rfc3339(),
                'adopted': True,
            }, source
        return None, None

    # -- claims -----------------------------------------------------------

    def claim(self, uri, owner, preferred_name=None, takeover=False,
              dispatch_id=None):
        """Try to grant the exclusive right to capture ``uri``.

        Returns a claim dict on success or ``None`` when another live claim
        still owns the page.  Every call is evaluated on disk: two processes
        seeing the same task race only on the manifest lock, and exactly one
        of them wins.

        If the page is already ``captured``/``fenced``, no claim is granted
        because there is nothing left to capture.  Use :meth:`recover` to
        finish the outstanding index publication instead.

        ``dispatch_id`` identifies the control-plane delivery that triggered
        this attempt.  Re-delivering the same task is recorded but never
        creates a second capture when the page has already been claimed or
        captured.
        """
        outcome = {}

        def mutate(data):
            data = data or {}
            page = self._find(data, uri)

            if dispatch_id is not None:
                page['dispatches'].append({
                    'dispatch_id': dispatch_id,
                    'owner': owner,
                    'seen_at': _now_rfc3339(),
                })

            if page.get('authorized') is False:
                outcome['reason'] = 'not-authorized'
                return data if dispatch_id is not None else None

            if page['state'] in ('captured', 'fenced', 'indexed'):
                outcome['reason'] = 'already-captured'
                return data if dispatch_id is not None else None

            current = self._active_attempt(page)
            if current and not takeover:
                outcome['reason'] = 'claimed-by-other'
                outcome['owner'] = current['owner']
                return data if dispatch_id is not None else None
            if current and takeover and current['owner'] == owner:
                # A re-delivery to the same worker is not a new authorization
                # unless the worker has lost its claim.
                outcome['reason'] = 'claimed-by-self'
                return data if dispatch_id is not None else None
            if current and takeover and current.get('dispatch_id') is not None \
                    and dispatch_id is None:
                outcome['reason'] = 'takeover-requires-new-dispatch'
                return data if dispatch_id is not None else None

            attempt_no = len(page['attempts']) + 1
            token = uuid.uuid4().hex
            if current:
                current['state'] = 'superseded'
                current['superseded_at'] = _now_rfc3339()
                current['superseded_by'] = token
            attempt = {
                'attempt': attempt_no,
                'owner': owner,
                'claim_token': token,
                'claimed_at': _now_rfc3339(),
                'lease_until': self._lease_until(),
                'state': 'writing',
                'preferred_name': preferred_name,
                'supersedes': current['claim_token'] if current else None,
                'dispatch_id': dispatch_id,
                'takeover': bool(takeover),
            }
            page['attempts'].append(attempt)
            page['state'] = 'writing'
            page['pending_index'] = None
            outcome['attempt'] = attempt_no
            outcome['claim_token'] = token
            return data

        self.store.mutate(mutate)
        if 'claim_token' not in outcome:
            self.last_claim_reason = outcome.get('reason')
            return None

        self.last_claim_reason = None
        return {
            'uri': uri,
            'attempt': outcome['attempt'],
            'claim_token': outcome['claim_token'],
            'owner': owner,
            'preferred_name': preferred_name,
        }

    def heartbeat(self, uri, claim_token):
        """Refresh the lease for a live claim, returning False when it is lost."""
        outcome = {}

        def mutate(data):
            page = self._find(data, uri)
            attempt = self._attempt_by_token(page, claim_token)
            if not attempt or attempt.get('state') != 'writing':
                outcome['alive'] = False
                return None
            attempt['lease_until'] = self._lease_until()
            outcome['alive'] = True
            return data

        self.store.mutate(mutate)
        return outcome.get('alive', False)

    def record_capture(self, uri, claim_token, filename, offset, length,
                       payload_digest=None, record_id=None, http_status=None,
                       allow_reindex=False):
        """Record a proven-complete capture for the page.

        This is the step that turns a live claim into durable evidence.  It
        refuses to accept evidence for a claim that has been superseded, and
        it re-reads the volume before committing the claim.

        ``allow_reindex`` is set when a page was previously marked
        ``discarded`` and a new attempt has now produced fresh evidence for
        it.  It never allows a claim to override a capture that is currently
        proven complete.
        """
        outcome = {}

        def mutate(data):
            page = self._find(data, uri)

            if page['state'] in ('captured', 'fenced', 'indexed') \
                    and page.get('capture') and not allow_reindex:
                outcome['reason'] = 'already-captured'
                return None

            attempt = self._attempt_by_token(page, claim_token)
            if not attempt or attempt.get('state') != 'writing':
                outcome['reason'] = 'claim-not-current'
                return None

            full_path = self._resolve(filename)
            info = verify_capture_file(full_path, offset, length,
                                       uri=uri, digest=payload_digest)

            page['capture'] = {
                'filename': self._rel(filename),
                'offset': offset,
                'length': length,
                'payload_digest': info['payload_digest'] or payload_digest,
                'record_id': info['record_id'] or record_id,
                'http_status': info['http_status'] or http_status,
                'verified_at': _now_rfc3339(),
                'attempt': attempt['attempt'],
                'claim_token': claim_token,
            }
            attempt['state'] = 'captured'
            attempt['captured_at'] = _now_rfc3339()
            page['state'] = 'captured'
            page['pending_index'] = {
                'capture_id': _capture_identity(page['capture']),
                'attempt': attempt['attempt'],
                'claim_token': claim_token,
            }
            outcome['capture'] = page['capture']
            return data

        self.store.mutate(mutate)
        if 'capture' not in outcome:
            raise ClaimError(outcome.get('reason', 'capture-not-recorded'))
        return outcome['capture']

    def abandon(self, uri, claim_token, reason, discard_volume=None):
        """Release a claim after an interrupted or failed capture.

        ``discard_volume`` names the volume that must not be treated as valid
        evidence for any page.  It is recorded in a tombstone list so future
        workers never reuse it as if it were a fresh volume.
        """
        def mutate(data):
            page = self._find(data, uri)
            attempt = self._attempt_by_token(page, claim_token)
            if attempt and attempt.get('state') == 'writing':
                attempt['state'] = 'abandoned'
                attempt['abandoned_at'] = _now_rfc3339()
                attempt['reason'] = reason
                page['state'] = 'pending'
                page['pending_index'] = None

            if discard_volume:
                tombstones = data.setdefault('tombstones', [])
                rel = self._rel(discard_volume)
                entry = {
                    'filename': rel,
                    'reason': reason,
                    'recorded_at': _now_rfc3339(),
                }
                if entry not in tombstones:
                    tombstones.append(entry)
            return data

        self.store.mutate(mutate)

    # -- recovery / publication ------------------------------------------

    def recover(self, uri, index=None, force=False):
        """Complete or repair the durable state of a page without recapturing.

        * A live claim is left alone unless ``force`` is set (used after a
          worker is known to have died, long before its lease expires).
        * A ``captured`` page whose volume can still be verified is promoted
          to ``fenced`` and its capture registered.
        * A ``captured`` page whose capture cannot be verified is reset to
          ``pending`` (the evidence, not the claim, is what decides).
        * ``fenced`` pages retry publication.
        """
        index = index or self.index
        page = self.page(uri)

        if page['state'] in ('writing', 'pending'):
            if page['state'] == 'writing':
                current = self._active_attempt(page)
                if current and not force:
                    return {'state': page['state'], 'reason': 'claim-live'}

            return self._recover_pending(uri)

        if page['state'] == 'captured':
            self._promote_captured(uri)
            page = self.page(uri)

        if page['state'] == 'fenced':
            try:
                result = self._publish(uri, page, index)
                return {'state': 'indexed', 'result': result}
            except IndexFenced as exc:
                return {'state': page['state'], 'reason': exc.reason}

        return {'state': self.page(uri)['state']}

    def _recover_pending(self, uri):
        """Look for a volume left by an interrupted attempt and index it.

        A worker that wrote a complete volume and died before recording it
        leaves the manifest at ``writing`` (or ``pending``) and the volume on
        disk.  We re-read candidate volumes from the attempt's preferred name
        first, then any other volume in the archive directory, and only a
        verified response for the URI counts.
        """
        outcome = {}

        def mutate(data):
            page = self._find(data, uri)
            attempt = self._latest_attempt(page)
            preferred = attempt.get('preferred_name') if attempt else None
            info = self._scan_for_capture(uri, preferred)
            if not info:
                return None

            page['capture'] = info
            page['state'] = 'captured'
            if attempt:
                attempt['state'] = 'captured'
                attempt['captured_at'] = _now_rfc3339()
                attempt['recovered'] = True
            page['pending_index'] = {
                'capture_id': _capture_identity(info),
                'attempt': attempt['attempt'] if attempt else None,
                'claim_token': attempt['claim_token'] if attempt else None,
            }
            outcome['ok'] = True
            return data

        self.store.mutate(mutate)
        if not outcome.get('ok'):
            return {'state': 'pending', 'reason': 'no-verified-capture'}

        self._promote_captured(uri)
        page = self.page(uri)
        if page['state'] == 'fenced':
            result = self._publish(uri, page, self.index)
            return {'state': 'indexed', 'result': result}
        return {'state': self.page(uri)['state']}

    def _promote_captured(self, uri):
        def mutate(data):
            page = self._find(data, uri)
            if page['state'] not in ('captured', 'discarded') or not page.get('capture'):
                return None
            try:
                capture = dict(page['capture'])
                verify_capture_file(self._resolve(capture['filename']),
                                    capture['offset'], capture['length'],
                                    uri=uri,
                                    digest=capture.get('payload_digest'))
            except VerificationError as exc:
                old = page.get('capture') or {}
                page['state'] = 'pending'
                page['pending_index'] = None
                page['notes'].append({
                    'at': _now_rfc3339(),
                    'event': 'verification-failed',
                    'detail': str(exc),
                    'filename': old.get('filename'),
                    'offset': old.get('offset'),
                    'length': old.get('length'),
                })
                page['capture'] = None
                return data

            page['state'] = 'fenced'
            return data

        self.store.mutate(mutate)

    def _publish(self, uri, page, index):
        if page['state'] != 'fenced':
            raise IndexFenced('page not fenced', page)
        capture = page['capture']
        verify_capture_file(self._resolve(capture['filename']),
                            capture['offset'], capture['length'],
                            uri=uri, digest=capture.get('payload_digest'))

        if page.get('pending_index'):
            attempt = page['pending_index']['attempt']
            token = page['pending_index']['claim_token']
        else:
            attempt = capture.get('attempt')
            token = capture.get('claim_token')

        result = index.submit({
            'uri': uri,
            'capture': capture,
            'attempt': attempt,
            'claim_token': token,
        })

        def mutate(data):
            page = self._find(data, uri)
            page['state'] = 'indexed'
            page['pending_index'] = None
            page['capture']['indexed_at'] = _now_rfc3339()
            return data

        self.store.mutate(mutate)
        return result

    # -- index message safety --------------------------------------------

    def index_message(self, uri, claim_token, capture=None):
        """Register an index message submitted by a capture worker.

        The message is fenced against the page's *current* capture.  A late
        message from an owner whose claim has been superseded is rejected
        even when the underlying page is still pending, and a stale message
        can never overwrite a capture that has already been proven complete.
        """
        page = self.page(uri)
        capture = capture or (page.get('capture') if page else None)

        if not capture:
            raise IndexFenced('no proven capture to index', page)

        # Only a message backed by the page's current capture may proceed.
        current = page.get('capture')
        if not current:
            raise IndexFenced('page has no current capture', page)

        if _capture_identity(current) != _capture_identity(capture):
            raise IndexFenced('message refers to a superseded capture', page)

        if page['state'] in ('pending', 'writing'):
            raise IndexFenced('page is not captured', page)

        if page['state'] == 'captured':
            self._promote_captured(uri)
            page = self.page(uri)

        if page['state'] == 'indexed':
            # Idempotent retry: the identical message for the already-indexed
            # capture is harmless and reports success without a new entry.
            entry = self.index.lookup(uri)
            if entry and entry.get('capture_id') == _capture_identity(current):
                if claim_token is None or claim_token == current.get('claim_token'):
                    return 'already'
            raise IndexFenced('page is indexed from another capture', page)

        if page['state'] != 'fenced':
            raise IndexFenced('page not ready for indexing', page)

        if page.get('pending_index'):
            attempt = page['pending_index']['attempt']
            expected_token = page['pending_index']['claim_token']
        else:
            attempt = current.get('attempt')
            expected_token = current.get('claim_token')

        if claim_token is not None and expected_token is not None \
                and claim_token != expected_token:
            raise IndexFenced('stale claim token', page)

        result = self.index.submit({
            'uri': uri,
            'capture': current,
            'attempt': attempt,
            'claim_token': expected_token,
        })

        def mutate(data):
            page = self._find(data, uri)
            page['state'] = 'indexed'
            page['pending_index'] = None
            page['capture']['indexed_at'] = _now_rfc3339()
            return data

        self.store.mutate(mutate)
        return result

    # -- volume naming ----------------------------------------------------

    def allocate_volume(self, claim, directory, preferred_name, extra_salt=''):
        """Reserve a writable volume name that is never an existing volume.

        The preferred name is tried first so that the normal naming scheme is
        preserved when it is free.  If the name already exists (for example a
        read-only legacy volume) a deterministic ``-rN`` suffix is appended.
        The chosen file is created with ``O_CREAT | O_EXCL`` so two workers
        cannot pick the same name even without the manifest lock.
        """
        if not os.path.isabs(directory):
            directory = os.path.join(self.root, directory)
        os.makedirs(directory, exist_ok=True)
        base = os.path.basename(preferred_name)
        stem, ext = _split_volume_name(base)
        suffix = extra_salt or claim['claim_token'][:8]

        attempts = []
        candidates = [base]
        candidates += ['{0}-r{1}{2}'.format(stem, i, ext) for i in range(1, 1000)]
        candidates += ['{0}-{1}{2}'.format(stem, suffix, ext)]

        for name in candidates:
            path = os.path.join(directory, name)
            if os.path.exists(path):
                attempts.append(name)
                continue
            try:
                fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            except FileExistsError:
                attempts.append(name)
                continue
            os.close(fd)
            rel = self._rel(path)
            claim['volume'] = rel
            claim['rejected_names'] = attempts
            self._record_volume(claim)
            return path

        raise ClaimError('unable to allocate a unique volume name')

    def _record_volume(self, claim):
        """Persist the volume allocated for an attempt in the manifest."""
        def mutate(data):
            page = self._find(data, claim['uri'])
            attempt = self._attempt_by_token(page, claim['claim_token'])
            if not attempt:
                return None
            attempt['volume'] = claim.get('volume')
            attempt['rejected_names'] = list(claim.get('rejected_names') or [])
            return data

        self.store.mutate(mutate)

    def tombstoned(self, filename):
        data = self.load()
        rel = self._rel(filename)
        return any(t.get('filename') == rel for t in data.get('tombstones', []))

    # -- helpers ----------------------------------------------------------

    def _find(self, data, uri):
        for page in data.get('pages', []):
            if page['uri'] == uri:
                return page
        raise ManifestError('unknown page: ' + uri)

    @staticmethod
    def _latest_attempt(page):
        if not page['attempts']:
            return None
        return page['attempts'][-1]

    @staticmethod
    def _attempt_by_token(page, token):
        for attempt in page['attempts']:
            if attempt.get('claim_token') == token:
                return attempt
        return None

    def _active_attempt(self, page):
        attempt = self._latest_attempt(page)
        if not attempt or attempt.get('state') != 'writing':
            return None
        lease = attempt.get('lease_until')
        if lease and _parse_time(lease) < self.clock():
            return None
        return attempt

    def _lease_until(self):
        return (self.clock() + datetime.timedelta(seconds=self.lease_seconds)) \
            .isoformat().replace('+00:00', 'Z')

    def _scan_for_capture(self, uri, preferred):
        directory = os.path.join(self.root, 'archive')
        names = []
        if preferred:
            names.append(os.path.basename(preferred))
        try:
            names += sorted(n for n in os.listdir(directory) if n.endswith('.warc.gz'))
        except OSError:
            return None

        seen = set()
        for name in names:
            if name in seen:
                continue
            seen.add(name)
            path = os.path.join(directory, name)
            if self.tombstoned(path):
                continue
            info = find_capture_in_volume(path, uri)
            if info:
                return {
                    'filename': self._rel(path),
                    'offset': info['start'],
                    'length': info['length'],
                    'payload_digest': info['payload_digest'],
                    'record_id': info['record_id'],
                    'http_status': info['http_status'],
                    'verified_at': _now_rfc3339(),
                    'recovered': True,
                }
        return None

    def _resolve(self, filename):
        if os.path.isabs(filename):
            return filename
        primary = os.path.join(self.root, filename)
        if os.path.exists(primary):
            return primary
        # Legacy indexes usually name the volume without its archive/ prefix.
        fallback = os.path.join(self.root, 'archive', filename)
        if os.path.exists(fallback):
            return fallback
        return primary

    def _rel(self, filename):
        if not os.path.isabs(filename):
            filename = os.path.join(self.root, filename)
        return os.path.relpath(filename, self.root)


# ============================================================================
def _split_volume_name(name):
    for ext in ('.warc.gz', '.warc'):
        if name.endswith(ext):
            return name[:-len(ext)], ext
    return name, ''


_PLAN_STATUS = {
    'pending': 'unstarted',
    'writing': 'in-progress',
    'captured': 'captured-unindexed',
    'fenced': 'captured-unindexed',
    'indexed': 'complete',
    'discarded': 'needs-capture',
}


def _parse_time(value):
    value = value.rstrip('Z')
    return datetime.datetime.fromisoformat(value).replace(tzinfo=datetime.timezone.utc)


def _mtime(path):
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0


def read_cdx_entries(path, volume_root=None):
    """Read a CDXJ or 11-field CDX index and yield ``(uri, capture)`` pairs.

    Only complete response rows are returned.  ``capture['filename']`` is
    resolved against ``volume_root`` unless it is already absolute.  Stale
    offsets are preserved here; :meth:`ContinuationManifest.register_completed`
    is what decides whether the volume actually proves the row.
    """
    with open(path, 'rb') as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            fields = line.split(b' ', 2)
            if len(fields) < 3:
                continue
            try:
                obj = json.loads(fields[2].decode('utf-8'))
            except ValueError:
                continue
            uri = obj.get('url')
            mime = obj.get('mime') or ''
            if not uri or mime == 'warc/revisit':
                continue
            filename = obj.get('filename')
            if not filename:
                continue
            if volume_root and not os.path.isabs(filename):
                filename = os.path.join(volume_root, filename)
            yield uri, {
                'filename': filename,
                'offset': _maybe_int(obj.get('offset')),
                'length': _maybe_int(obj.get('length')),
                'payload_digest': _digest_from_cdx(obj.get('digest')),
            }


def _maybe_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _digest_from_cdx(value):
    if not value or value == '-':
        return None
    return _normalise_digest(value)


# ============================================================================
class ContinuationSession(object):
    """Per-machine view of a shared continuation batch.

    A session never caches manifest state: every method re-reads the manifest
    from disk.  Two sessions in two processes (or two threads, or two test
    clients) therefore observe the same authoritative claims.
    """

    def __init__(self, manifest, owner, writer=None, directory=None):
        from pywb.recorder.continuationwriter import ContinuationWriter

        self.manifest = manifest
        self.owner = owner
        self.writer = writer or ContinuationWriter(manifest.root)
        self.directory = directory or os.path.join(manifest.root, 'archive')

    def capture(self, uri, payload, preferred_name, takeover=False,
                claim=None, **kwargs):
        """Claim, allocate a fresh volume, write it and record the evidence.

        ``claim`` may be passed when the caller already acquired one through
        :meth:`takeover`.  The returned dict is the page's recovered state,
        normally ``{'state': 'indexed', 'result': 'registered'}``.  If the
        page is already proven, no claim and no write occur.
        """
        if claim is None:
            claim = self.manifest.claim(uri, self.owner,
                                        preferred_name=preferred_name,
                                        takeover=takeover)
        if not claim:
            page = self.manifest.page(uri)
            if page['state'] in ('captured', 'fenced', 'indexed'):
                return self.resume(uri)
            raise ClaimError('page is claimed by {0}'.format(
                self.manifest.status_owner(uri)))

        # A previous owner may already have persisted a complete volume.
        # Adoption must happen before allocating a second volume, so a proven
        # page is never recorded again as if it were unstarted.  A torn or
        # tombstoned volume never verifies and therefore never blocks a real
        # recapture.
        adopted = self.manifest.adopt(uri, claim['claim_token'])
        if adopted:
            return adopted

        path = self.manifest.allocate_volume(claim, self.directory,
                                             preferred_name)
        try:
            result = self.writer.write_capture(path, uri, payload, **kwargs)
        except Exception as exc:
            self.manifest.abandon(uri, claim['claim_token'],
                                  'capture-interrupted',
                                  discard_volume=path)
            exc.volume = path
            exc.claim = claim
            raise
        self.manifest.record_capture(
            uri, claim['claim_token'],
            result['filename'], result['offset'], result['length'],
            result['payload_digest'], result['record_id'],
            result['http_status'], allow_reindex=True)

        recovered = self.resume(uri)
        recovered['capture'] = result
        return recovered

    def resume(self, uri):
        """Recover and publish the page without capturing anything new."""
        return self.manifest.recover(uri)

    def takeover(self, uri, preferred_name=None):
        """Acquire a page whose previous owner has gone away."""
        claim = self.manifest.claim(uri, self.owner,
                                    preferred_name=preferred_name,
                                    takeover=True)
        if not claim:
            page = self.manifest.page(uri)
            if page['state'] in ('captured', 'fenced', 'indexed'):
                return self.resume(uri)
            raise ClaimError('unable to take over page: ' + uri)
        return claim

    def adopt(self, uri, claim_token=None):
        """Adopt a complete capture left behind by a dead writer."""
        return self.manifest.adopt(uri, claim_token)

    def take_over_and_adopt(self, uri, preferred_name=None):
        """Take over a page and adopt any verified capture it left behind.

        Returns ``(claim, adopted)`` where ``adopted`` is the capture dict or
        ``None`` when the new owner must capture the page itself.
        """
        claim = self.takeover(uri, preferred_name=preferred_name)
        if 'claim_token' not in claim:
            return claim, claim
        adopted = self.manifest.adopt(uri, claim['claim_token'])
        return claim, adopted

    def publish(self, uri, claim_token=None):
        """Send the index message for a proven capture.

        The message carries the capture identity and the claim token.  The
        manifest fences it against the page's current authorization, so a
        late message from a superseded writer is rejected.
        """
        page = self.manifest.page(uri)
        token = claim_token if claim_token is not None else \
            (page['attempts'][-1]['claim_token'] if page['attempts'] else None)
        return self.manifest.index_message(uri, token)

    def plan(self):
        return self.manifest.plan()

    def pending(self):
        """Convenience view of pages that still owe work."""
        return [row for row in self.manifest.plan() if row['remaining']]

    def dispatch(self, uri, dispatch_id):
        """Record a control-plane delivery without claiming the page."""
        def mutate(data):
            page = self.manifest._find(data, uri)
            page['dispatches'].append({
                'dispatch_id': dispatch_id,
                'owner': self.owner,
                'seen_at': _now_rfc3339(),
            })
            return data

        self.manifest.store.mutate(mutate)

    def read(self, uri):
        """Return the payload of the page's proven capture."""
        page = self.manifest.page(uri)
        if page['state'] not in ('captured', 'fenced', 'indexed'):
            return None
        capture = page.get('capture')
        if not capture:
            entry = self.manifest.index.lookup(uri)
            if not entry:
                return None
            capture = entry
        return read_capture_payload(self.manifest._resolve(capture['filename']),
                                    capture['offset'], capture['length'],
                                    uri=uri,
                                    digest=capture.get('payload_digest'))
