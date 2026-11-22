"""Deterministic minimal-WARC fixtures for the audit package tests.

Two local plain-text WARC volumes hold captures of one URI at one UTC
second.  They deliberately contain the three difference classes the audit
tool has to keep apart:

* class A: two byte-identical independent copies (same status, headers and
  body), one copy in each volume;
* class B: same body, different ``Content-Type``;
* class C: two genuinely different responses whose CDX digest and
  WARC-Record-ID fields are filled in identically, while the volumes read
  out different records.

Two index claims point at coordinates that cannot be read (a missing
volume and an offset past end of file) so file existence can never be
mistaken for read evidence.
"""

import json
import os
from io import BytesIO

from warcio.archiveiterator import ArchiveIterator
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from ..digests import payload_digest

URI = 'http://example.invalid/page'
URLKEY = 'com,example.invalid)/page'
TIMESTAMP = '20261123000000'
WARC_DATE = '2026-11-23T00:00:00Z'

VOLUME_A = 'volume-a.warc'
VOLUME_B = 'volume-b.warc'

# CDX claims that contradict the bytes for both class-C records.
C_CLAIMED_DIGEST = 'CLAIMED-DIGEST-SAME-FOR-BOTH'
C_CLAIMED_RECORD_ID = '<urn:uuid:cccccccc-0000-0000-0000-000000000000>'


def _record_id(token):
    return '<urn:uuid:{0}>'.format(token)


def _http(status, content_type, body, extra_headers=None):
    headers = [('Content-Type', content_type),
               ('Content-Length', str(len(body)))]
    if extra_headers:
        headers.extend(extra_headers)
    return StatusAndHeaders(status, headers, protocol='HTTP/1.1')


def _write_volume(path, specs):
    with open(path, 'wb') as stream:
        writer = WARCWriter(stream, gzip=False)
        for spec in specs:
            http_headers = _http(spec['status'], spec['content_type'],
                                 spec['body'], spec.get('extra_headers'))
            record = writer.create_warc_record(
                URI, 'response', payload=BytesIO(spec['body']),
                http_headers=http_headers,
                warc_headers_dict={
                    'WARC-Record-ID': _record_id(spec['record_token']),
                    'WARC-Date': WARC_DATE,
                })
            writer.write_record(record)


def _catalogue_coordinates(path):
    """Return [(offset, length, warc_record_id), ...] by reading the volume."""
    coordinates = []
    with open(path, 'rb') as stream:
        iterator = ArchiveIterator(stream)
        for record in iterator:
            if record.rec_type != 'response':
                continue
            coordinates.append((
                iterator.get_record_offset(),
                iterator.get_record_length(),
                record.rec_headers.get_header('WARC-Record-ID'),
            ))
            record.raw_stream.read()
    return coordinates


def _cdxj_line(offset, length, filename, mime, status, digest,
               record_id=None):
    fields = {
        'url': URI,
        'mime': mime,
        'status': status,
        'digest': digest,
        'length': str(length),
        'offset': str(offset),
        'filename': filename,
    }
    if record_id is not None:
        fields['warc_record_id'] = record_id
    return '{0} {1} {2}\n'.format(URLKEY, TIMESTAMP,
                                  json.dumps(fields))


def build_initial_archive(root):
    """Create volumes A/B and the two CDXJ index sources under ``root``."""
    warcs = os.path.join(root, 'warcs')
    indexes = os.path.join(root, 'indexes')
    os.makedirs(warcs)
    os.makedirs(indexes)

    body_a = b'<html><body>alpha</body></html>'
    body_b = b'<html><body>bravo shared body</body></html>'
    body_c1 = b'charlie response one'
    body_c2 = b'charlie response two differs'

    volume_a = os.path.join(warcs, VOLUME_A)
    volume_b = os.path.join(warcs, VOLUME_B)

    _write_volume(volume_a, [
        {'record_token': 'aaaaaaaa-0000-0000-0000-000000000001',
         'status': '200 OK', 'content_type': 'text/html',
         'body': body_a, 'extra_headers': [('X-Rep', 'alpha')]},
        {'record_token': 'bbbbbbbb-0000-0000-0000-000000000001',
         'status': '200 OK', 'content_type': 'text/html',
         'body': body_b},
        {'record_token': 'c1111111-0000-0000-0000-000000000001',
         'status': '200 OK', 'content_type': 'text/html',
         'body': body_c1},
    ])
    _write_volume(volume_b, [
        # byte-identical independent copy of the class-A record
        {'record_token': 'aaaaaaaa-0000-0000-0000-000000000002',
         'status': '200 OK', 'content_type': 'text/html',
         'body': body_a, 'extra_headers': [('X-Rep', 'alpha')]},
        # same body as B1, different Content-Type
        {'record_token': 'bbbbbbbb-0000-0000-0000-000000000002',
         'status': '200 OK', 'content_type': 'application/xhtml+xml',
         'body': body_b},
        # genuinely different response, same second
        {'record_token': 'c2222222-0000-0000-0000-000000000002',
         'status': '200 OK', 'content_type': 'text/plain',
         'body': body_c2},
    ])

    coords_a = _catalogue_coordinates(volume_a)
    coords_b = _catalogue_coordinates(volume_b)

    digest_a = payload_digest(body_a)
    digest_b = payload_digest(body_b)
    digest_c1 = payload_digest(body_c1)
    digest_c2 = payload_digest(body_c2)

    index_a = os.path.join(indexes, 'index-a.cdxj')
    index_b = os.path.join(indexes, 'index-b.cdxj')

    with open(index_a, 'w', encoding='utf-8') as stream:
        stream.write(_cdxj_line(coords_a[0][0], coords_a[0][1],
                                VOLUME_A, 'text/html', '200', digest_a,
                                _record_id('aaaaaaaa-0000-0000-0000-000000000001')))
        stream.write(_cdxj_line(coords_a[1][0], coords_a[1][1],
                                VOLUME_A, 'text/html', '200', digest_b,
                                _record_id('bbbbbbbb-0000-0000-0000-000000000001')))
        # class C: digest and record-id claimed identical to index-b's line
        stream.write(_cdxj_line(coords_a[2][0], coords_a[2][1],
                                VOLUME_A, 'text/html', '200',
                                C_CLAIMED_DIGEST, C_CLAIMED_RECORD_ID))
        # unreadable: named volume does not exist
        stream.write(_cdxj_line(999, 400, 'missing-volume.warc',
                                'text/html', '200', digest_a))
        # unreadable: volume exists but offset is past end of file
        past_end = coords_a[-1][0] + coords_a[-1][1] + 50
        stream.write(_cdxj_line(past_end, 200, VOLUME_A,
                                'text/html', '200', digest_b))

    with open(index_b, 'w', encoding='utf-8') as stream:
        stream.write(_cdxj_line(coords_b[0][0], coords_b[0][1],
                                VOLUME_B, 'text/html', '200', digest_a,
                                _record_id('aaaaaaaa-0000-0000-0000-000000000002')))
        stream.write(_cdxj_line(coords_b[1][0], coords_b[1][1],
                                VOLUME_B, 'application/xhtml+xml', '200',
                                digest_b,
                                _record_id('bbbbbbbb-0000-0000-0000-000000000002')))
        # class C: same claimed digest and record-id as index-a, other bytes
        stream.write(_cdxj_line(coords_b[2][0], coords_b[2][1],
                                VOLUME_B, 'text/plain', '200',
                                C_CLAIMED_DIGEST, C_CLAIMED_RECORD_ID))

    return {
        'root': root,
        'warcs': warcs,
        'indexes': indexes,
        'volume_a': volume_a,
        'volume_b': volume_b,
        'index_a': index_a,
        'index_b': index_b,
        'coords_a': coords_a,
        'coords_b': coords_b,
        'digests': {
            'a': digest_a, 'b': digest_b,
            'c1': digest_c1, 'c2': digest_c2,
        },
        'bodies': {'a': body_a, 'b': body_b,
                   'c1': body_c1, 'c2': body_c2},
    }


def volume_resolver(warcs_dir):
    """Resolver used by the manifest builder."""
    def resolve(filename):
        return os.path.join(warcs_dir, filename)
    return resolve
