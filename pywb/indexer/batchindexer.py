"""Incremental CDX indexing for archives delivered as separate volumes.

A batch of archived forms is often delivered in parts: the volumes with the
response records arrive first, the volumes with the request records arrive
later, possibly in a different order.  The request records carry the HTTP
method and request body that make up the POST replay keys.  A request volume
may be delivered after the response volume has been moved to cold storage, so
the indexer must be able to update the index without reading the response
volume again.

For every volume this module stores a small *evidence* file: record ids,
target URIs, dates, record offsets/lengths, and the canonical request body.
Pairing uses only that evidence.  Published index views are immutable and are
switched atomically, so a reader sees either the previous complete view or the
complete new view.  A response whose request has not arrived yet is reported
as waiting and is not given a POST key.  If two different request records
refer to the same response, or two responses with the same record id really
differ, the response is reported as a conflict; the previous link can still
be served from the previous view, and once the conflict is confirmed the
affected key is withdrawn while the volume and record ids that caused it are
retained.  Withdrawing the conflicting volume restores the key from the
original request evidence.

The main entry point is :class:`BatchIndex`; :class:`BuildBatch` exposes the
same pairing to ``cdx-indexer --batch``.  For a self-contained volume the
generated lines match the existing ``cdx-indexer -j -p`` output exactly.
The on-disk layout is::

    <root>/
        evidence/<volume>.json         durable evidence, written once
        submissions/<id>.json          saved submissions for idempotent retry
        views/<view>/index.cdxj        immutable published view
        views/<view>/conflicts.json
        views/<view>/waiting.json
        views/<view>/unpaired.json
        views/<view>/state.json
        current                        name of the current view
        live                           symlink to the current view

Nothing in a published view depends on a volume that has to be read again:
the ``filename``/``offset``/``length`` fields point at the response record for
replay, while pairing is rebuilt from the saved evidence.
"""

import base64
import hashlib
import json
import os
import shutil
import tempfile
import uuid

from collections import OrderedDict
from urllib.parse import parse_qsl
from warcio.archiveiterator import ArchiveIterator
from warcio.timeutils import iso_date_to_timestamp

from pywb.utils.canonicalize import canonicalize
from pywb.warcserver.inputrequest import MethodQueryCanonicalizer


EVIDENCE_VERSION = 1
SUBMISSION_VERSION = 1
STATE_VERSION = 1

RESPONSE_TYPES = ('response', 'revisit')
REQUEST_TYPES = ('request',)

CONFLICT_PENDING = 'pending'
CONFLICT_CONFIRMED = 'confirmed'


class BatchIndexError(Exception):
    pass


# ============================================================================
def _sha256_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _json_bytes(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True,
                      separators=(',', ':')).encode('utf-8')


def _write_atomic(path, data):
    """Write ``data`` to ``path`` so readers see the old or the new bytes."""
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory or '.', prefix='.tmp-')
    try:
        with os.fdopen(fd, 'wb') as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_json(path):
    with open(path, 'rb') as fh:
        return json.loads(fh.read().decode('utf-8'))


def _view_name(value):
    name = ''.join(c if c.isalnum() or c in '._-' else '_' for c in value)
    return name[:80] or 'view'


def _safe_name(value):
    value = value or 'batch'
    digest = _sha256_bytes(value.encode('utf-8'))[:12]
    base = os.path.basename(value).replace(os.path.sep, '_')
    base = ''.join(c if c.isalnum() or c in '._-' else '_' for c in base)
    return (base[:60] or 'item') + '-' + digest


def _split_mime(value):
    if not value:
        return 'unk'
    return value.split(';', 1)[0].strip() or 'unk'


def _header_values(rec_headers, name):
    values = []
    for header, value in rec_headers.headers:
        if header.lower() == name.lower():
            values.append(value)
    return values


def _record_id(value):
    if not value:
        return None
    return value.strip()


class _BytesReader(object):
    def __init__(self, data):
        self.data = data

    def read(self, length=-1):
        if length < 0:
            length = len(self.data)
        out, self.data = self.data[:length], self.data[length:]
        return out


# ============================================================================
class ArchiveEvidence(object):
    """The pairing evidence read once from one WARC volume."""

    def __init__(self, volume, size=0, mtime=None, records=None):
        self.volume = volume
        self.size = size
        self.mtime = mtime
        self.records = records or []

    @classmethod
    def from_volume(cls, path, volume=None, max_request_length=None):
        volume = volume or os.path.basename(path)
        records = []

        with open(path, 'rb') as fh:
            iterator = ArchiveIterator(fh, ensure_http_headers=True)
            for record in iterator:
                if record.rec_type in REQUEST_TYPES:
                    payload = record.raw_stream.read()
                    iterator.read_to_end(record)
                    entry = cls._request_entry(record, payload,
                                               iterator.member_info,
                                               max_request_length)
                elif record.rec_type in RESPONSE_TYPES:
                    # HttpHeaders are gone after read_to_end(): capture the
                    # fields needed for the index entry now.
                    response_info = _response_info(record)
                    digest = record.rec_headers.get_header('WARC-Payload-Digest')
                    if not digest:
                        payload = record.raw_stream.read()
                        digest = 'sha1:' + base64.b32encode(
                            hashlib.sha1(payload).digest()).decode('ascii')
                    iterator.read_to_end(record)
                    entry = cls._response_entry(record, iterator.member_info,
                                                digest, response_info)
                else:
                    iterator.read_to_end(record)
                    continue

                if entry:
                    records.append(entry)

        return cls(volume, os.path.getsize(path), os.path.getmtime(path),
                   records)

    @staticmethod
    def _response_entry(record, member_info, digest, response_info=None):
        uri = record.rec_headers.get_header('WARC-Target-URI')
        offset, length = member_info or (None, None)
        if uri is None or offset is None:
            return None

        record_type = record.rec_type
        if record_type == 'revisit':
            mime = 'warc/revisit'
            status = '-'
        elif response_info:
            mime, status = response_info['mime'], response_info['status']
        else:
            http = record.http_headers
            mime = _split_mime(http.get_header('Content-Type') if http else '')
            status = http.get_statuscode() if http else '-'

        return {
            'kind': 'response',
            'id': _record_id(record.rec_headers.get_header('WARC-Record-ID')),
            'uri': uri,
            'date': record.rec_headers.get_header('WARC-Date'),
            'offset': offset,
            'length': length,
            'record_type': record_type,
            'mime': mime,
            'status': status or '-',
            'digest': digest.split(':', 1)[-1] if digest else '-',
        }

    @staticmethod
    def _request_entry(record, payload, member_info, max_request_length):
        uri = record.rec_headers.get_header('WARC-Target-URI')
        offset, length = member_info or (None, None)
        http = record.http_headers
        if uri is None or offset is None or http is None:
            return None

        method = (http.protocol or '').upper()
        if method in ('HTTP/1.0', 'HTTP/1.1', 'HTTP/2'):
            method = (http.statusline or '').split(' ', 1)[0].upper()
        if not method:
            return None

        query = ''
        if method in ('POST', 'PUT'):
            try:
                content_length = int(http.get_header('Content-Length'))
            except (TypeError, ValueError):
                content_length = len(payload)

            if max_request_length is not None:
                content_length = min(content_length, max_request_length)

            query = _request_query(method, http.get_header('Content-Type'),
                                   content_length, payload)

        concurrent = [_record_id(v) for v in
                      _header_values(record.rec_headers, 'WARC-Concurrent-To')]
        concurrent = [v for v in concurrent if v]

        return {
            'kind': 'request',
            'id': _record_id(record.rec_headers.get_header('WARC-Record-ID')),
            'uri': uri,
            'date': record.rec_headers.get_header('WARC-Date'),
            'offset': offset,
            'length': length,
            'method': method,
            'content_type': http.get_header('Content-Type'),
            'body': query,
            'body_hash': hashlib.sha256(payload).hexdigest(),
            'concurrent_to': concurrent,
        }

    def to_dict(self):
        return {'version': EVIDENCE_VERSION, 'volume': self.volume,
                'size': self.size, 'mtime': self.mtime,
                'records': self.records}

    @classmethod
    def from_dict(cls, data):
        if data.get('version') != EVIDENCE_VERSION:
            raise BatchIndexError('unsupported evidence version')
        return cls(data['volume'], data.get('size', 0), data.get('mtime'),
                   data.get('records', []))


def _response_info(record):
    http = record.http_headers
    return {
        'mime': _split_mime(http.get_header('Content-Type') if http else ''),
        'status': (http.get_statuscode() if http else '-') or '-',
    }


def _request_query(method, content_type, length, payload):
    """The canonical form query produced by the same canonicalizer pywb uses.

    Keeping this identical to the replay-side canonicalizer is what makes the
    index key and the POST lookup meet.  A body the canonicalizer cannot
    represent (eg. a malformed AMF payload) is left empty, exactly as in the
    replay lookup.
    """
    try:
        canonicalizer = MethodQueryCanonicalizer(method, content_type, length,
                                                 _BytesReader(payload))
        query = canonicalizer.query
        if isinstance(query, bytes):
            query = query.decode('utf-8')
        return query or ''
    except Exception:
        return ''


# ============================================================================
def _request_ref(volume, record):
    return {
        'volume': volume,
        'record_id': record.get('id'),
        'offset': record.get('offset'),
        'length': record.get('length'),
        'uri': record.get('uri'),
        'date': record.get('date'),
        'method': record.get('method'),
        'body': record.get('body'),
        'body_hash': record.get('body_hash'),
        'concurrent_to': record.get('concurrent_to') or [],
    }


def _response_ref(volume, record):
    return {
        'volume': volume,
        'record_id': record.get('id'),
        'offset': record.get('offset'),
        'length': record.get('length'),
        'uri': record.get('uri'),
        'date': record.get('date'),
        'mime': record.get('mime'),
        'status': record.get('status'),
        'digest': record.get('digest'),
        'record_type': record.get('record_type'),
    }


def _body_signature(body):
    try:
        return tuple(sorted(parse_qsl(body or '', keep_blank_values=True)))
    except Exception:
        return body or ''


def _request_signature(record):
    # compare the canonical request fields, so a re-delivery of the same
    # request in a different form order is not a conflict.
    return ((record.get('method') or '').upper(),
            _body_signature(record.get('body')))


def _response_signature(record):
    # offsets differ when the same record is repeated in a copied volume, so
    # only the capture identity is used here.  A different digest or status
    # with the same record id is a real conflict.
    return (record.get('uri'), record.get('date'), record.get('status'),
            record.get('digest'), record.get('record_type'))


def _append_query(uri, method, query):
    if '?' not in uri:
        new_uri = uri + '?'
    else:
        new_uri = uri + '&'
    new_uri += '__wb_method=' + method
    if query:
        new_uri += '&' + query
    return new_uri.replace('WB_wombat_', '')


def _key_for(uri, request):
    """Match ``MethodQueryCanonicalizer.append_query`` exactly."""
    if not request:
        return uri
    method = (request.get('method') or 'GET').upper()
    if method == 'GET':
        return uri
    return _append_query(uri, method, request.get('body') or '')


def _urlkey_for(key):
    try:
        return canonicalize(key)
    except Exception:
        return key


# ============================================================================
class PairingResult(object):
    def __init__(self):
        self.pairs = []
        self.conflicts = {}
        self.waiting = []
        self.plain_responses = []
        self.orphan_requests = []
        self.orphan_responses = []


class BatchPairing(object):
    """Pair requests and responses using only WARC relationship evidence.

    ``WARC-Concurrent-To`` on a request must name the ``WARC-Record-ID`` of a
    response.  No fallback to adjacency, timestamp or target URI is made.
    """

    def __init__(self, evidence):
        self.responses = []
        self.requests = []
        self.responses_by_id = {}
        self.requests_by_id = {}
        self.requests_by_concurrent = {}

        for item in evidence:
            for record in item.records:
                if record.get('kind') == 'response':
                    self.responses.append((item.volume, record))
                    rid = record.get('id')
                    if rid:
                        self.responses_by_id.setdefault(rid, []).append(
                            (item.volume, record))
                elif record.get('kind') == 'request':
                    self.requests.append((item.volume, record))
                    rid = record.get('id')
                    if rid:
                        self.requests_by_id.setdefault(rid, []).append(
                            (item.volume, record))
                    for concurrent in record.get('concurrent_to') or []:
                        self.requests_by_concurrent.setdefault(
                            concurrent, []).append((item.volume, record))

    def pair(self):
        result = PairingResult()

        # Responses sharing a record id but describing different captures
        # cannot be selected by a single request id.
        bad_responses = set()
        for rid, records in self.responses_by_id.items():
            signatures = {_response_signature(record) for _, record in records}
            if len(signatures) > 1:
                bad_responses.add(rid)
                result.conflicts[self._response_conflict_id(rid, records)] = {
                    'kind': 'duplicate-response-id',
                    'response_id': rid,
                    'response': _response_ref(*self._pick(records)),
                    'candidates': [_response_ref(*item) for item in records],
                    'reason': 'one response record id describes different captures',
                }

        # Requests sharing a record id with different method/body evidence
        # make the linked response ambiguous even if only one of them was
        # delivered earlier.
        bad_requests = set()
        bad_concurrent = set()
        for rid, records in self.requests_by_id.items():
            signatures = {_request_signature(record) for _, record in records}
            if len(signatures) > 1:
                bad_requests.add(rid)
                for _, record in records:
                    bad_concurrent.update(record.get('concurrent_to') or [])

        # identical copies of the same response record (same id, same
        # capture identity) are one capture; pick one volume deterministically
        # instead of emitting two index lines for it.
        chosen_response = {}
        for rid, records in self.responses_by_id.items():
            if rid in bad_responses:
                continue
            chosen_response[rid] = self._pick(records)

        for volume, response in self.responses:
            rid = response.get('id')
            if not rid:
                result.orphan_responses.append({
                    'volume': volume,
                    'reason': 'response has no WARC-Record-Id',
                    'response': _response_ref(volume, response),
                })
                continue
            if rid in bad_responses:
                continue

            chosen_volume, _chosen = chosen_response.get(rid, (volume, response))
            if (volume, response.get('offset')) != (
                    chosen_volume, _chosen.get('offset')):
                continue

            candidates = self.requests_by_concurrent.get(rid) or []
            if rid in bad_concurrent and not candidates:
                conflict_id = self._request_conflict_id(
                    rid, [item for bad_id in bad_requests
                          for item in self.requests_by_id.get(bad_id) or []])
                result.conflicts[conflict_id] = {
                    'kind': 'multiple-request-candidates',
                    'response_id': rid,
                    'response': _response_ref(volume, response),
                    'candidates': [
                        _request_ref(*item)
                        for bad_id in bad_requests
                        for item in self.requests_by_id.get(bad_id) or []],
                    'reason': 'more than one request record selects this response',
                }
                continue

            if not candidates:
                result.waiting.append(_response_ref(volume, response))
                continue

            signatures = {_request_signature(record) for _, record in candidates}
            duplicate_ids = [item for item in candidates
                             if item[1].get('id') in bad_requests]
            if len(signatures) > 1 or duplicate_ids:
                all_candidates = list(candidates)
                for bad_id in bad_requests:
                    for item in self.requests_by_id.get(bad_id) or []:
                        if item not in all_candidates:
                            all_candidates.append(item)

                conflict_id = self._request_conflict_id(rid, all_candidates)
                result.conflicts[conflict_id] = {
                    'kind': 'multiple-request-candidates',
                    'response_id': rid,
                    'response': _response_ref(volume, response),
                    'candidates': [_request_ref(*item) for item in all_candidates],
                    'reason': 'more than one request record selects this response',
                }
                continue

            chosen = self._pick(candidates)
            pair = (_response_ref(volume, response), _request_ref(*chosen))
            if pair not in result.pairs:
                result.pairs.append(pair)

        # A response with no request evidence can only be published as a
        # plain capture when it is unambiguous: a single response for its url
        # and second.  Two responses sharing the url and second are the forms
        # whose requests have not arrived; publishing either as a plain GET
        # would let a form lookup land on the other form's body.
        waiting_groups = {}
        for item in result.waiting:
            waiting_groups.setdefault((item.get('uri'), item.get('date')),
                                      []).append(item)
        still_waiting = []
        for item in result.waiting:
            if len(waiting_groups[(item.get('uri'), item.get('date'))]) > 1:
                still_waiting.append(item)
            else:
                result.plain_responses.append(item)
        result.waiting = still_waiting

        reported_orphans = set()
        for volume, request in self.requests:
            concurrent = request.get('concurrent_to') or []
            ref = _request_ref(volume, request)
            if not concurrent or not any(rid in self.responses_by_id
                                         for rid in concurrent):
                signature = (ref['volume'], ref['record_id'], ref['offset'],
                             ref['body'])
                if signature not in reported_orphans:
                    reported_orphans.add(signature)
                    result.orphan_requests.append(ref)

        return result

    @staticmethod
    def _pick(items):
        return sorted(items, key=lambda item: (item[0], item[1].get('offset')
                                               if item[1].get('offset') is not None else -1))[0]

    @staticmethod
    def _response_conflict_id(rid, records):
        data = [rid] + sorted(
            (volume, record.get('offset'), _response_signature(record))
            for volume, record in records)
        return 'conflict-' + _sha256_bytes(repr(data).encode('utf-8'))[:16]

    @staticmethod
    def _request_conflict_id(rid, records):
        data = [rid] + sorted(
            (volume, record.get('id'), record.get('offset'),
             _request_signature(record))
            for volume, record in records)
        return 'conflict-' + _sha256_bytes(repr(data).encode('utf-8'))[:16]


# ============================================================================
def _cdx_line(response, request, source):
    uri = response['uri']
    key = _key_for(uri, request)
    urlkey = _urlkey_for(key)

    timestamp = iso_date_to_timestamp(response['date']) \
        if response.get('date') else '00000000000000'

    values = OrderedDict()
    values['url'] = uri
    values['mime'] = response.get('mime') or 'unk'
    values['status'] = response.get('status')
    values['digest'] = response.get('digest')
    values['length'] = str(response.get('length'))
    values['offset'] = str(response.get('offset'))
    if request and (request.get('method') or 'GET').upper() != 'GET':
        values['method'] = request['method']
        if request.get('body'):
            values['requestBody'] = request['body']
    values['filename'] = response.get('volume') or source

    # the existing CDXJ writer leaves out empty and '-' fields
    fields = OrderedDict((name, value) for name, value in values.items()
                         if value and value != '-')

    return urlkey + ' ' + timestamp + ' ' + json.dumps(fields,
                                                       ensure_ascii=False)


def _find_request(evidence, ref):
    """Find the request evidence matching a saved link."""
    if not ref:
        return None
    for item in evidence:
        if item.volume != ref.get('volume'):
            continue
        for record in item.records:
            if record.get('kind') != 'request':
                continue
            if record.get('id') != ref.get('record_id'):
                continue
            if ref.get('offset') is not None and record.get('offset') != ref.get('offset'):
                continue
            return record
    return None


def build_view(evidence, previous_links=None, confirmed=None,
               withdrawn_volumes=None):
    """Build a complete view from evidence.

    Returns ``(lines, waiting, conflicts, links)``.  ``previous_links`` maps a
    response id to the request ref used by the previous view; it is retained
    while a new conflict is under review.
    """
    previous_links = previous_links or {}
    confirmed = set(confirmed or ())
    withdrawn_volumes = set(withdrawn_volumes or ())

    active = [item for item in evidence if item.volume not in withdrawn_volumes]
    pairing = BatchPairing(active).pair()

    pairs = {}
    response_refs = {}
    for response, request in pairing.pairs:
        pairs[response['record_id']] = (response, request)
        response_refs[response['record_id']] = response

    lines = []
    links = {}
    conflicts = {}

    for conflict_id, conflict in pairing.conflicts.items():
        conflict = dict(conflict)
        conflict['conflict_id'] = conflict_id
        rid = conflict.get('response_id')
        prior = previous_links.get(rid)
        prior_record = _find_request(active, prior) if prior else None
        conflict['prior'] = prior
        conflict['state'] = CONFLICT_CONFIRMED if conflict_id in confirmed \
            else CONFLICT_PENDING
        conflict['affected_key'] = None
        if prior_record:
            conflict['affected_key'] = _urlkey_for(
                _key_for(conflict['response']['uri'], prior_record))
        conflicts[conflict_id] = conflict

        # While the conflict is under review the previous, already published
        # link remains readable.  A confirmed conflict withdraws that key,
        # even if the previous request volume has since been withdrawn.
        if prior_record and conflict_id not in confirmed:
            response_record = response_refs.get(rid) or conflict['response']
            lines.append(_cdx_line(response_record, prior_record,
                                   response_record.get('volume')))
            links[rid] = prior
        if conflict_id in confirmed:
            # keep the withdrawn key and its evidence in the conflict record
            # so it stays explainable after the volume is gone
            conflict['withdrawn_key'] = conflict['affected_key']
            conflict['withdrawn_evidence'] = prior

    confirmed_responses = {conflict['response_id'] for conflict in
                           conflicts.values()
                           if conflict['state'] == CONFLICT_CONFIRMED}
    for rid, (response, request) in pairs.items():
        if rid in confirmed_responses:
            continue
        lines.append(_cdx_line(response, request, response.get('volume')))
        links[rid] = request

    for response in pairing.plain_responses:
        lines.append(_cdx_line(response, None, response.get('volume')))
        links.setdefault(response.get('record_id'), None)

    waiting = pairing.waiting

    lines = sorted(set(lines))
    waiting = _unique_refs(waiting)
    orphan_responses = []
    for item in pairing.orphan_responses:
        ref = dict(item['response'])
        ref['reason'] = item['reason']
        orphan_responses.append(ref)
    unpaired = {'requests': _unique_refs(pairing.orphan_requests),
                'responses': _unique_refs(orphan_responses)}
    return lines, waiting, conflicts, links, unpaired


def _unique_refs(items):
    result = []
    seen = set()
    for item in items:
        key = (item.get('volume'), item.get('record_id'),
               item.get('offset'), item.get('length'))
        if key not in seen:
            seen.add(key)
            result.append(item)
    return result


# ============================================================================
class _Lock(object):
    def __init__(self, path):
        self.path = path
        self.fh = None

    def __enter__(self):
        import portalocker
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.fh = open(self.path, 'a+b')
        portalocker.lock(self.fh, portalocker.LOCK_EX)
        return self

    def __exit__(self, *args):
        try:
            import portalocker
            portalocker.unlock(self.fh)
        finally:
            self.fh.close()
        return False


class BatchView(object):
    """An immutable published view.  Open it to pin one complete snapshot."""

    def __init__(self, root, view_id):
        self.root = root
        self.view_id = view_id
        self.path = os.path.join(root, 'views', view_id)

    @property
    def index_path(self):
        return os.path.join(self.path, 'index.cdxj')

    def state(self):
        return _read_json(os.path.join(self.path, 'state.json'))

    def conflicts(self):
        return _read_json(os.path.join(self.path, 'conflicts.json'))

    def waiting(self):
        return _read_json(os.path.join(self.path, 'waiting.json'))

    def unpaired(self):
        return _read_json(os.path.join(self.path, 'unpaired.json'))

    def index_lines(self):
        with open(self.index_path, 'rb') as fh:
            return [line.decode('utf-8').rstrip('\n')
                    for line in fh if line.strip()]

    def summary(self):
        return _read_json(os.path.join(self.path, 'summary.json'))


class BatchIndex(object):
    """A durable, incrementally updated index for a batch of WARC volumes."""

    INDEX_NAME = 'index.cdxj'
    CURRENT_NAME = 'current'

    def __init__(self, root, volume_reader=None):
        self.root = root
        self.volume_reader = volume_reader or ArchiveEvidence.from_volume
        self.evidence_dir = os.path.join(root, 'evidence')
        self.submissions_dir = os.path.join(root, 'submissions')
        self.views_dir = os.path.join(root, 'views')
        self.lock_path = os.path.join(root, 'lock')

    # -- paths ------------------------------------------------------------

    def evidence_path(self, volume):
        return os.path.join(self.evidence_dir, _safe_name(volume) + '.json')

    def submission_path(self, submission_id):
        return os.path.join(self.submissions_dir, _safe_name(submission_id) + '.json')

    def view(self, view_id=None):
        if view_id is None:
            view_id = self.current_view_id()
        if not view_id:
            return None
        return BatchView(self.root, view_id)

    def current_view_id(self):
        path = os.path.join(self.root, self.CURRENT_NAME)
        if not os.path.isfile(path):
            return None
        with open(path, 'r', encoding='utf-8') as fh:
            return fh.read().strip() or None

    def _set_current(self, view_id):
        _write_atomic(os.path.join(self.root, self.CURRENT_NAME),
                      (view_id + '\n').encode('utf-8'))

    # -- evidence ---------------------------------------------------------

    def save_evidence(self, evidence):
        _write_atomic(self.evidence_path(evidence.volume),
                      _json_bytes(evidence.to_dict()))

    def load_evidence(self, volume):
        path = self.evidence_path(volume)
        if not os.path.isfile(path):
            return None
        return ArchiveEvidence.from_dict(_read_json(path))

    def all_evidence(self):
        if not os.path.isdir(self.evidence_dir):
            return []
        evidence = []
        for name in sorted(os.listdir(self.evidence_dir)):
            if name.endswith('.json'):
                evidence.append(ArchiveEvidence.from_dict(
                    _read_json(os.path.join(self.evidence_dir, name))))
        return evidence

    def add_volume(self, path, volume=None, replace=False):
        """Read a new volume once and save its evidence.

        If evidence for ``volume`` already exists it is reused without opening
        the volume, which is what allows the response volumes to be cold when
        the request volumes arrive.  Use ``replace=True`` to re-read a volume
        that really changed under the same name.
        """
        volume = volume or os.path.basename(path)
        existing = self.load_evidence(volume)
        if existing and not replace:
            return existing

        evidence = self.volume_reader(path, volume=volume)
        self.save_evidence(evidence)
        return evidence

    def add_evidence(self, evidence):
        if isinstance(evidence, dict):
            evidence = ArchiveEvidence.from_dict(evidence)
        self.save_evidence(evidence)
        return evidence

    # -- submissions ------------------------------------------------------

    def _submission_hash(self, submission):
        # the same submission id must describe the same operation on the same
        # evidence; only the receipt fields are left out.
        return _sha256_bytes(_json_bytes({k: submission[k] for k in
                                          ('op', 'volumes', 'evidence',
                                           'volume', 'conflict_ids')}))

    def _load_submission(self, submission_id):
        path = self.submission_path(submission_id)
        if not os.path.isfile(path):
            return None
        return _read_json(path)

    def _save_submission(self, submission):
        _write_atomic(self.submission_path(submission['submission_id']),
                      _json_bytes(submission))

    def _initial_state(self):
        return {
            'version': STATE_VERSION,
            'applied': [],
            'links': {},
            'confirmed_conflicts': [],
            'withdrawn_volumes': [],
        }

    def _current_state(self):
        view = self.view()
        if not view:
            return self._initial_state(), None
        return view.state(), view.view_id

    def submit(self, submission_id, volumes=(), evidence=(), op='add',
               volume=None, conflict_ids=None, failpoint=None):
        """Apply one batch submission and publish exactly one new view.

        ``volumes`` contains paths or ``(path, volume_id)`` pairs.  A volume
        whose evidence was saved before is never opened again.  ``op`` is one
        of ``add``, ``withdraw_volume`` or ``confirm``.  ``failpoint`` is
        called with the stage name and is used by tests to interrupt between
        saving the submission and publishing the view.
        """
        with _Lock(self.lock_path):
            try:
                return self._submit_locked(submission_id, volumes, evidence,
                                           op, volume, conflict_ids, failpoint)
            except BaseException:
                self._cleanup_stale_temp()
                raise

    def _cleanup_stale_temp(self):
        """Remove temporary view directories left by an interrupted write."""
        if not os.path.isdir(self.views_dir):
            return
        for name in os.listdir(self.views_dir):
            if name.startswith('.tmp-'):
                shutil.rmtree(os.path.join(self.views_dir, name),
                              ignore_errors=True)

    def _submit_locked(self, submission_id, volumes, evidence, op, volume,
                       conflict_ids, failpoint):
        saved = self._load_submission(submission_id)
        saved_evidence = []
        volume_specs = []
        for item in volumes:
            if isinstance(item, (tuple, list)):
                path, volume_id = item
            else:
                path, volume_id = item, None
            volume_specs.append((os.path.abspath(path),
                                 volume_id or os.path.basename(path)))
            saved_evidence.append(volume_id or os.path.basename(path))

        evidence_list = []
        for item in evidence:
            if isinstance(item, dict):
                item = ArchiveEvidence.from_dict(item)
            evidence_list.append(item)
            saved_evidence.append(item.volume)

        record = {
            'version': SUBMISSION_VERSION,
            'submission_id': submission_id,
            'op': op,
            'volumes': saved_evidence,
            'evidence': sorted(saved_evidence),
            'volume': volume,
            'conflict_ids': sorted(conflict_ids or []),
        }
        content_hash = self._submission_hash(record)

        if saved:
            if saved.get('content_hash') != content_hash:
                raise BatchIndexError(
                    'submission {0} already exists with different content'
                    .format(submission_id))
            state, base_view = self._current_state()
            if submission_id in state.get('applied', []):
                # the submission was already applied: return the view it
                # published, not whichever view is current now.
                view_id = (saved.get('view') or
                           self._find_submission_view(submission_id))
                if not view_id:
                    raise BatchIndexError('submission has no published view')
                return BatchView(self.root, view_id)
            record = saved
        else:
            state, base_view = self._current_state()
            record['content_hash'] = content_hash
            record['base_view'] = base_view
            record['status'] = 'saved'
            self._save_submission(record)

        # Save evidence before publishing.  A retry finds it and does not read
        # the volume again.
        for item in evidence_list:
            self.add_evidence(item)
        for path, volume_id in volume_specs:
            self.add_volume(path, volume=volume_id)

        if failpoint:
            failpoint('after-save')

        state = dict(state)
        state['applied'] = list(state.get('applied', []))
        state['links'] = dict(state.get('links', {}))
        state['confirmed_conflicts'] = list(state.get('confirmed_conflicts', []))
        state['withdrawn_volumes'] = list(state.get('withdrawn_volumes', []))

        if op == 'withdraw_volume':
            if not volume:
                raise BatchIndexError('withdraw_volume requires a volume')
            if volume not in state['withdrawn_volumes']:
                state['withdrawn_volumes'].append(volume)
        elif op == 'restore_volume':
            if volume in state['withdrawn_volumes']:
                state['withdrawn_volumes'].remove(volume)
        elif op == 'confirm':
            for conflict_id in conflict_ids or []:
                if conflict_id not in state['confirmed_conflicts']:
                    state['confirmed_conflicts'].append(conflict_id)
        elif op != 'add':
            raise BatchIndexError('unknown operation: ' + op)

        if submission_id not in state['applied']:
            state['applied'].append(submission_id)

        view = self._publish(state, submission_id, base_view,
                             prune=op == 'withdraw_volume')
        if failpoint:
            failpoint('after-publish')

        record['status'] = 'applied'
        record['view'] = view.view_id
        record['view_conflicts'] = sorted(view.conflicts())
        self._save_submission(record)
        return view

    def _find_submission_view(self, submission_id):
        if not os.path.isdir(self.views_dir):
            return None
        for name in sorted(os.listdir(self.views_dir), reverse=True):
            state_path = os.path.join(self.views_dir, name, 'state.json')
            if not os.path.isfile(state_path):
                continue
            try:
                if _read_json(state_path).get('submission') == submission_id:
                    return name
            except Exception:
                continue
        return None

    def _publish(self, state, submission_id, base_view, prune=False):
        evidence = self.all_evidence()
        last_links = dict(state.get('last_links') or {})
        last_links.update(state.get('links') or {})
        lines, waiting, conflicts, links, unpaired = build_view(
            evidence,
            previous_links=last_links,
            confirmed=state.get('confirmed_conflicts'),
            withdrawn_volumes=state.get('withdrawn_volumes'))

        state = dict(state)
        if prune:
            # a confirmation only applies while its conflicting evidence is
            # still submitted; withdrawing that evidence restores the earlier
            # key and makes a re-delivery a new review.
            state['confirmed_conflicts'] = [
                cid for cid in state['confirmed_conflicts']
                if cid in conflicts]
        state['links'] = links
        state['last_links'] = last_links
        state['waiting'] = waiting
        state['conflicts'] = conflicts
        state['unpaired'] = unpaired
        state['submission'] = submission_id
        state['withdrawn_keys'] = sorted(
            c['affected_key'] for c in conflicts.values()
            if c.get('state') == CONFLICT_CONFIRMED and c.get('affected_key'))
        state['withdrawn_evidence'] = [
            {'conflict_id': c['conflict_id'],
             'key': c.get('affected_key'),
             'prior': c.get('prior')}
            for c in sorted(conflicts.values(), key=lambda c: c['conflict_id'])
            if c.get('state') == CONFLICT_CONFIRMED and c.get('prior')]

        view_id = 'v-' + _view_name(submission_id)

        final = os.path.join(self.views_dir, view_id)
        index_bytes = ''.join(line + '\n' for line in lines).encode('utf-8')
        state['view'] = view_id
        state['parent'] = base_view
        state['index_sha256'] = _sha256_bytes(index_bytes)

        if os.path.isdir(final):
            old_state = _read_json(os.path.join(final, 'state.json'))
            old_conflicts = _read_json(os.path.join(final, 'conflicts.json'))
            old_waiting = _read_json(os.path.join(final, 'waiting.json'))
            if (old_state.get('index_sha256') != state['index_sha256'] or
                    old_conflicts != conflicts or old_waiting != waiting):
                raise BatchIndexError(
                    'view {0} already exists with different content'.format(view_id))
        else:
            tmp = os.path.join(self.views_dir, '.tmp-' + view_id)
            if os.path.isdir(tmp):
                shutil.rmtree(tmp)
            os.makedirs(tmp)
            _write_atomic(os.path.join(tmp, self.INDEX_NAME), index_bytes)
            _write_atomic(os.path.join(tmp, 'conflicts.json'),
                          _json_bytes(conflicts))
            _write_atomic(os.path.join(tmp, 'waiting.json'),
                          _json_bytes(waiting))
            _write_atomic(os.path.join(tmp, 'unpaired.json'),
                          _json_bytes(unpaired))
            _write_atomic(os.path.join(tmp, 'state.json'), _json_bytes(state))
            _write_atomic(os.path.join(tmp, 'summary.json'), _json_bytes({
                'view': view_id,
                'parent': base_view,
                'lines': len(lines),
                'waiting': len(waiting),
                'conflicts': len(conflicts),
                'unpaired_requests': len(unpaired['requests']),
                'unpaired_responses': len(unpaired['responses']),
                'withdrawn_keys': state['withdrawn_keys'],
            }))
            os.replace(tmp, final)

        self._set_current(view_id)
        self._refresh_live(view_id)
        return BatchView(self.root, view_id)

    def _refresh_live(self, view_id):
        """Atomically point ``live`` at the complete current view.

        ``live`` is a symlink, so a reader that opens ``live/index.cdxj`` and
        ``live/conflicts.json`` reads both from the same immutable view and
        cannot see a mix of two views.
        """
        live = os.path.join(self.root, 'live')
        target = os.path.join('views', view_id)
        tmp = os.path.join(self.root, '.live-' + uuid.uuid4().hex)
        try:
            os.symlink(target, tmp)
            if os.path.isdir(live) and not os.path.islink(live):
                shutil.rmtree(live)
            os.replace(tmp, live)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            # fallback for filesystems without symlinks: copy the complete
            # view into a temporary directory and swap it into place.
            tmp_dir = live + '.tmp'
            if os.path.isdir(tmp_dir):
                shutil.rmtree(tmp_dir)
            shutil.copytree(self.view_path(view_id), tmp_dir)
            if os.path.isdir(live):
                shutil.rmtree(live)
            os.replace(tmp_dir, live)

    def view_path(self, view_id):
        return os.path.join(self.views_dir, view_id)

    # -- review helpers ---------------------------------------------------

    def pending_conflicts(self):
        view = self.view()
        if not view:
            return {}
        return {cid: conflict for cid, conflict in view.conflicts().items()
                if conflict.get('state') != CONFLICT_CONFIRMED}

    def confirm_conflicts(self, submission_id, conflict_ids=None):
        view = self.view()
        if not view:
            raise BatchIndexError('no published view')
        conflicts = view.conflicts()
        if conflict_ids is None:
            conflict_ids = sorted(conflicts)
        missing = [cid for cid in conflict_ids if cid not in conflicts]
        if missing:
            raise BatchIndexError('unknown conflict(s): ' + ', '.join(missing))
        return self.submit(submission_id, op='confirm',
                           conflict_ids=conflict_ids)

    def withdraw_volume(self, submission_id, volume):
        return self.submit(submission_id, op='withdraw_volume', volume=volume)

    def restore_volume(self, submission_id, volume):
        """Restore a withdrawn volume and clear conflicts of its records.

        Confirming a batch conflict records its id; when the supplemental
        volume is withdrawn, the earlier key must come back.  Any confirmation
        of a conflict that no longer exists is dropped, so a later re-delivery
        of the same conflicting volume is reviewed again instead of silently
        withdrawing the restored key.
        """
        return self.submit(submission_id, op='restore_volume', volume=volume)

    def status(self):
        view = self.view()
        if not view:
            return {'view': None, 'waiting': [], 'conflicts': [],
                    'index': None}
        summary = view.summary()
        conflicts = view.conflicts()
        return {
            'view': view.view_id,
            'index': view.index_path,
            'lines': summary.get('lines', 0),
            'waiting': view.waiting(),
            'conflicts': list(conflicts.values()),
            'pending_conflicts': [c for c in conflicts.values()
                                  if c.get('state') != CONFLICT_CONFIRMED],
            'withdrawn_keys': summary.get('withdrawn_keys', []),
        }


# ============================================================================
class BuildBatch(object):
    """One-shot pairing of a set of volumes, used by ``cdx-indexer --batch``."""

    def __init__(self, evidence):
        self.evidence = evidence

    def build(self):
        lines, waiting, conflicts, _links, unpaired = build_view(self.evidence)
        return lines, waiting, conflicts, unpaired


# ============================================================================
class BatchReader(object):
    """Read one complete batch index view.

    Open a reader before reading the index and conflict files.  The reader
    resolves the ``live`` link once and keeps the resolved view id, so even if
    another process publishes a new view while the files are being read, the
    index and conflicts returned belong to the same view.  (Opening
    ``index.cdxj`` through ``live`` on POSIX also pins the old inode.)
    """

    def __init__(self, root):
        self.root = root
        self.view_id = None
        self.path = None
        self._open()

    def _open(self):
        live = os.path.join(self.root, 'live')
        if os.path.islink(live):
            target = os.readlink(live)
            self.path = os.path.normpath(os.path.join(self.root, target))
            self.view_id = os.path.basename(self.path)
            return

        if os.path.isdir(live):
            # symlinks unavailable: the complete copied view is in place
            self.path = live
            self.view_id = _read_json(os.path.join(live, 'summary.json')).get('view')
            return

        index = BatchIndex(self.root)
        view_id = index.current_view_id()
        if not view_id:
            raise BatchIndexError('no published view')
        self.view_id = view_id
        self.path = os.path.join(self.root, 'views', view_id)

    def index_lines(self):
        with open(os.path.join(self.path, 'index.cdxj'), 'rb') as fh:
            return [line.decode('utf-8').rstrip('\n')
                    for line in fh if line.strip()]

    def conflicts(self):
        return _read_json(os.path.join(self.path, 'conflicts.json'))

    def waiting(self):
        return _read_json(os.path.join(self.path, 'waiting.json'))

    def summary(self):
        return _read_json(os.path.join(self.path, 'summary.json'))
