"""Tests for representation-aware multi-fragment range replay."""

from io import BytesIO
import json
import os
import time

import pytest

from warcio.archiveiterator import ArchiveIterator
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from pywb.warcserver.resource.rangecatalog import (
    ArchiveService,
    CatalogVerificationError,
    MediaArchiveApp,
    RangeNotSatisfiable,
    RangeReader,
    RepresentationNotFound,
    build_catalog,
    etag_is_strong,
    if_range_matches,
    index_warc_records,
    parse_byte_range,
)


# ============================================================================
def make_warc(path, records):
    """Write response records and return their WARC locations."""
    buff = BytesIO()
    writer = WARCWriter(buff, gzip=False)

    for record in records:
        payload = record['payload']
        headers = [
            ('Content-Type', record.get('mime', 'video/mp4')),
            ('Content-Length', str(len(payload))),
        ]
        if record.get('etag'):
            headers.append(('ETag', record['etag']))
        if record.get('content_range'):
            headers.append(('Content-Range', record['content_range']))

        status = record.get('status', '200 OK')
        http_headers = StatusAndHeaders(status, headers, protocol='HTTP/1.1')
        warc_record = writer.create_warc_record(
            record.get('url', 'http://example.com/media.mp4'),
            'response',
            payload=BytesIO(payload),
            http_headers=http_headers)
        writer.write_record(warc_record)

    with open(path, 'wb') as fh:
        fh.write(buff.getvalue())

    return index_warc_records(path)


def source(record, path):
    return {
        'filename': path,
        'offset': record['offset'],
        'length': record['length'],
    }


def read_payload(path, record):
    with open(path, 'rb') as fh:
        iterator = ArchiveIterator(fh)
        for item in iterator:
            iterator.read_to_end(item)
            if iterator.member_info and iterator.member_info[0] == record['offset']:
                return item.content_stream().read()
    raise AssertionError('record not found')


def media_spec(rep_id, data, records, path, etag='"v1"',
               segments=None, total_length=None):
    if segments is None:
        segments = [
            {
                'start': 0,
                'end': 11,
                'sources': [source(records[0], path)],
            },
            {
                'start': 8,
                'end': len(data) - 1,
                'sources': [source(records[1], path)],
            },
        ]

    return {
        'representation_id': rep_id,
        'url': 'http://example.com/media.mp4',
        'mime': 'video/mp4',
        'etag': etag,
        'total_length': len(data) if total_length is None else total_length,
        'segments': segments,
    }


class CountingReader(RangeReader):
    """A reader which records the exact byte slices requested at playback."""

    def __init__(self):
        super(CountingReader, self).__init__(chunk_size=4)
        self.reads = []

    def read(self, source, start=0, length=None):
        data = super(CountingReader, self).read(source, start, length)
        self.reads.append((source.identity, source.path, start, length,
                           len(data)))
        return data

    def paths_read(self):
        return {item[1] for item in self.reads if item[1]}

    def bytes_read(self):
        return sum(item[4] for item in self.reads)


def call_app(app, path, method='GET', headers=None):
    env = {
        'REQUEST_METHOD': method,
        'PATH_INFO': path,
        'QUERY_STRING': '',
    }
    for name, value in (headers or {}).items():
        env['HTTP_' + name.upper().replace('-', '_')] = value

    result = {}

    def start_response(status, response_headers):
        result['status'] = status
        result['headers'] = dict(response_headers)

    body = b''.join(app(env, start_response))
    result['body'] = body
    return result


# ============================================================================
class TestRangeCatalogImport(object):
    def test_import_checks_full_length_and_overlap(self, tmp_path):
        data = bytes(range(20))
        path = str(tmp_path / 'media.warc')
        records = make_warc(path, [
            {'payload': data[:12], 'etag': '"v1"'},
            {'payload': data[8:]},
        ])

        catalog = build_catalog({
            'generation': 1,
            'representations': [media_spec('r1', data, records, path)],
        })
        rep = catalog.require('r1')

        assert rep.total_length == 20
        assert rep.complete
        assert rep.coverage_header == 'bytes 0-19/20'
        assert rep.segments[0].digest
        assert rep.segments[1].digest
        assert rep.content_id

    def test_import_rejects_declared_length_mismatch(self, tmp_path):
        path = str(tmp_path / 'media.warc')
        records = make_warc(path, [
            {'payload': b'0123456789'},
            {'payload': b'ABCDEFGHIJ'},
        ])

        spec = {
            'url': 'http://example.com/media.mp4',
            'total_length': 20,
            'segments': [
                {'start': 0, 'end': 9,
                 'sources': [source(records[0], path)]},
                {'start': 10, 'end': 19,
                 'sources': [source(records[1], path)]},
            ],
        }
        # The record contains ten bytes while the fragment declares eleven.
        spec['segments'][0]['length'] = 11

        with pytest.raises(CatalogVerificationError):
            build_catalog([spec])

    def test_import_rejects_overlap_mismatch(self, tmp_path):
        path = str(tmp_path / 'media.warc')
        records = make_warc(path, [
            {'payload': b'0123456789AB'},
            {'payload': b'XXXXXXXXCDEFGHIJ'},
        ])

        with pytest.raises(CatalogVerificationError):
            build_catalog([{
                'url': 'http://example.com/media.mp4',
                'total_length': 20,
                'segments': [
                    {'start': 0, 'end': 11,
                     'sources': [source(records[0], path)]},
                    {'start': 8, 'end': 19,
                     'sources': [source(records[1], path)]},
                ],
            }])

    def test_import_verifies_alternate_paths(self, tmp_path):
        data = b'0123456789ABCDEFGHIJ'
        primary = str(tmp_path / 'primary.warc')
        alternate = str(tmp_path / 'alternate.warc')
        p_records = make_warc(primary, [
            {'payload': data[:12]}, {'payload': data[8:]}])
        a_records = make_warc(alternate, [
            {'payload': data[:12]}, {'payload': data[8:]}])

        catalog = build_catalog([{
            'url': 'http://example.com/media.mp4',
            'etag': '"v1"',
            'total_length': 20,
            'segments': [
                {'start': 0, 'end': 11,
                 'sources': [source(p_records[0], primary)]},
                {'start': 8, 'end': 19,
                 'sources': [source(p_records[1], primary),
                             source(a_records[1], alternate)]},
            ],
        }])
        assert catalog.representations[0].complete

        bad_alternate = str(tmp_path / 'bad.warc')
        b_records = make_warc(bad_alternate, [
            {'payload': data[:12]}, {'payload': b'0123XXXXXXXXXXXX'}])

        with pytest.raises(CatalogVerificationError):
            build_catalog([{
                'url': 'http://example.com/media.mp4',
                'total_length': 20,
                'segments': [
                    {'start': 0, 'end': 11,
                     'sources': [source(p_records[0], primary)]},
                    {'start': 8, 'end': 19,
                     'sources': [source(p_records[1], primary),
                                 source(b_records[1], bad_alternate)]},
                ],
            }])

    def test_import_rejects_total_shorter_than_fragment(self, tmp_path):
        path = str(tmp_path / 'media.warc')
        records = make_warc(path, [{'payload': b'0123456789'}])

        with pytest.raises(CatalogVerificationError):
            build_catalog([{
                'url': 'http://example.com/media.mp4',
                'total_length': 9,
                'segments': [
                    {'start': 0, 'end': 9,
                     'sources': [source(records[0], path)]},
                ],
            }])


# ============================================================================
class TestPlaybackStreaming(object):
    def test_playback_reads_only_requested_slices(self, tmp_path):
        data = b'0123456789ABCDEFGHIJ'
        path = str(tmp_path / 'media.warc')
        records = make_warc(path, [
            {'payload': data[:12]}, {'payload': data[8:]}])

        catalog = build_catalog([media_spec('r1', data, records, path)])
        reader = CountingReader()
        service = ArchiveService(catalog, reader=reader)

        response = service.handle_request(rep_id='r1',
                                          range_header='bytes=9-11')
        assert response.status == 206
        assert b''.join(response.body) == data[9:12]

        # Import already verified the whole representation.  The playback
        # request must not re-read the rest of either fragment.
        assert reader.bytes_read() == 3
        assert b'"Content-Range"' not in b''
        assert ('Content-Range', 'bytes 9-11/20') in response.headers

    def test_cross_fragment_streams_before_second_fragment(self, tmp_path):
        data = b'0123456789ABCDEFGHIJ'
        path = str(tmp_path / 'media.warc')
        records = make_warc(path, [
            {'payload': data[:12]}, {'payload': data[8:]}])

        catalog = build_catalog([media_spec('r1', data, records, path)])
        reader = CountingReader()
        service = ArchiveService(catalog, reader=reader)

        response = service.handle_request(rep_id='r1',
                                          range_header='bytes=0-19')
        body = iter(response.body)

        first = next(body)
        assert first == data[:4]
        assert reader.bytes_read() == 4
        assert reader.paths_read() == {path}

        assert b''.join(body) == data[4:]
        assert reader.bytes_read() == 20


# ============================================================================
class TestCatalogSwitch(object):
    def _setup_switch(self, tmp_path, include_alternate):
        data = b'0123456789ABCDEFGHIJ'          # 20 bytes
        head = b'0123456789AB'                  # bytes 0-11
        tail = b'89ABCDEFGHIJ'                  # bytes 8-19

        head_path = str(tmp_path / 'head.warc')
        default_tail = str(tmp_path / 'tail-default.warc')
        alternate_tail = str(tmp_path / 'tail-alternate.warc')

        head_records = make_warc(head_path, [
            {'payload': head, 'etag': '"v1"'},
        ])
        default_records = make_warc(default_tail, [
            {'payload': tail, 'etag': '"v1"'},
        ])
        alternate_records = make_warc(alternate_tail, [
            {'payload': tail, 'etag': '"v1"'},
        ])

        seg1_sources = [source(default_records[0], default_tail)]
        if include_alternate:
            seg1_sources.append(
                source(alternate_records[0], alternate_tail))

        gen1 = build_catalog({
            'generation': 1,
            'representations': [{
                'representation_id': 'media-1',
                'url': 'http://example.com/media.mp4',
                'etag': '"v1"',
                'mime': 'video/mp4',
                'total_length': 20,
                'segments': [
                    {'start': 0, 'end': 11,
                     'sources': [source(head_records[0], head_path)]},
                    {'start': 8, 'end': 19, 'sources': seg1_sources},
                ],
            }],
        })

        replacement_tail = b'89ABWXYZ0123'      # bytes 8-19

        def publish_replacement():
            # Generation 2 reuses the default tail path and the same strong
            # ETag, and keeps bytes 8-11 (the overlap) identical, but changes
            # the bytes after the overlap.
            make_warc(default_tail, [
                {'payload': replacement_tail, 'etag': '"v1"'},
            ])
            os.utime(default_tail, (1, 1))

            return build_catalog({
                'generation': 2,
                'representations': [{
                    'representation_id': 'media-1',
                    'url': 'http://example.com/media.mp4',
                    'etag': '"v1"',
                    'mime': 'video/mp4',
                    'total_length': 20,
                    'segments': [
                        {'start': 0, 'end': 11,
                         'sources': [source(head_records[0], head_path)]},
                        {'start': 8, 'end': 19,
                         'sources': [source(default_records[0],
                                            default_tail)]},
                    ],
                }],
            })

        return (data, replacement_tail, head_path, default_tail,
                alternate_tail, gen1, publish_replacement)

    def test_pinned_request_continues_original_representation(self, tmp_path):
        (data, replacement_tail, head_path, default_tail, alternate_tail,
         gen1, publish_replacement) = self._setup_switch(tmp_path, True)

        reader = CountingReader()
        service = ArchiveService(gen1, reader=reader)
        response = service.handle_request(rep_id='media-1',
                                          range_header='bytes=0-19')
        pin = dict(response.headers)['X-Archive-Pin']

        body = iter(response.body)
        first = next(body)
        assert first == data[:4]

        # Only the first fragment has been read so far, from the first
        # fragment's own file.
        assert reader.bytes_read() == 4
        assert head_path in reader.paths_read()
        assert default_tail not in reader.paths_read()

        gen2 = publish_replacement()
        service.publish(gen2)

        # The default tail path now holds a different tail, but the pinned
        # alternate source still carries the original representation.
        remainder = b''.join(body)
        assert first + remainder == data
        assert alternate_tail in reader.paths_read()

        pinned = service.info(pin=pin)
        assert pinned['pinned_generation'] == 1
        assert pinned['etag'] == '"v1"'
        assert pinned['coverage'] == 'bytes 0-19/20'

    def test_unproven_remaining_access_returns_416(self, tmp_path):
        (data, replacement_tail, head_path, default_tail, alternate_tail,
         gen1, publish_replacement) = self._setup_switch(tmp_path, False)

        service = ArchiveService(gen1)
        pin = service.pin('media-1')

        service.publish(publish_replacement())

        with pytest.raises(RangeNotSatisfiable) as excinfo:
            service.handle_request(pin=pin.pin_id,
                                   range_header='bytes=0-19')
        assert excinfo.value.total_length == 20

        app = MediaArchiveApp(service)
        response = call_app(app, '/media/pin/' + pin.pin_id,
                            headers={'Range': 'bytes=0-19'})
        assert response['status'].startswith('416')
        assert response['headers']['Content-Range'] == 'bytes */20'

    def test_switch_after_head_but_before_second_fragment(self, tmp_path):
        (data, replacement_tail, head_path, default_tail, alternate_tail,
         gen1, publish_replacement) = self._setup_switch(tmp_path, False)

        service = ArchiveService(gen1)
        response = service.handle_request(rep_id='media-1',
                                          range_header='bytes=0-19')
        body = iter(response.body)

        # Consume the head fragment only.  The second fragment has not been
        # fetched when the catalog changes.
        first = next(body)
        assert data.startswith(first)
        assert len(first) <= 12
        # Drain any remaining head-fragment chunks without touching the
        # second fragment.
        got = first
        while len(got) < 12:
            got += next(body)
        assert got == data[:12]

        service.publish(publish_replacement())

        # The remaining bytes cannot be proven to be the original
        # representation, so the response fails with 416 rather than mixing
        # the new tail into the old head.
        with pytest.raises(RangeNotSatisfiable):
            list(body)

    def test_moved_addresses_with_same_identity_continue_capture(self, tmp_path):
        data = b'ABCDEFGHIJKLMNOPQRST'
        old1 = str(tmp_path / 'old-1.warc')
        old2 = str(tmp_path / 'old-2.warc')
        old_records1 = make_warc(old1, [
            {'payload': data[:12], 'etag': '"same"'}])
        old_records2 = make_warc(old2, [
            {'payload': data[8:], 'etag': '"same"'}])

        gen1 = build_catalog([{
            'representation_id': 'moved-capture',
            'url': 'http://example.com/media.mp4',
            'etag': '"same"',
            'total_length': 20,
            'segments': [
                {'start': 0, 'end': 11,
                 'sources': [source(old_records1[0], old1)]},
                {'start': 8, 'end': 19,
                 'sources': [source(old_records2[0], old2)]},
            ],
        }])

        service = ArchiveService(gen1)
        pin = service.pin('moved-capture')

        new1 = str(tmp_path / 'new-1.warc')
        new2 = str(tmp_path / 'new-2.warc')
        new_records1 = make_warc(new1, [
            {'payload': data[:12], 'etag': '"same"'}])
        new_records2 = make_warc(new2, [
            {'payload': data[8:], 'etag': '"same"'}])

        gen2 = build_catalog([{
            'representation_id': 'moved-capture',
            'url': 'http://example.com/media.mp4',
            'etag': '"same"',
            'total_length': 20,
            'segments': [
                {'start': 0, 'end': 11,
                 'sources': [source(new_records1[0], new1)]},
                {'start': 8, 'end': 19,
                 'sources': [source(new_records2[0], new2)]},
            ],
        }])

        assert gen1.representations[0].content_id == \
            gen2.representations[0].content_id

        os.unlink(old1)
        os.unlink(old2)
        service.publish(gen2)

        response = service.handle_request(pin=pin.pin_id,
                                          range_header='bytes=0-19')
        assert response.status == 206
        assert b''.join(response.body) == data

        fresh = service.handle_request(rep_id='moved-capture')
        assert fresh.status == 200
        assert b''.join(fresh.body) == data


# ============================================================================
class TestIfRange(object):
    def _catalog(self, tmp_path):
        old_data = b'old-version-01234567'
        new_data = b'new-version-ABCDEFGH'
        old_path = str(tmp_path / 'old.warc')
        new_path = str(tmp_path / 'new.warc')
        old_records = make_warc(old_path, [
            {'payload': old_data, 'etag': '"old"'}])
        new_records = make_warc(new_path, [
            {'payload': new_data[8:], 'etag': '"new"',
             'status': '206 Partial Content',
             'content_range': 'bytes 8-19/20'}])

        old = build_catalog([{
            'representation_id': 'old',
            'url': 'http://example.com/media.mp4',
            'etag': '"old"',
            'total_length': 20,
            'segments': [
                {'start': 0, 'end': 19,
                 'sources': [source(old_records[0], old_path)]},
            ],
        }])

        partial = build_catalog([{
            'representation_id': 'new',
            'url': 'http://example.com/media.mp4',
            'etag': '"new"',
            'total_length': 20,
            'segments': [
                {'start': 8, 'end': 19,
                 'sources': [source(new_records[0], new_path)]},
            ],
        }])

        return old_data, new_data, old, partial, new_path, new_records

    def test_matching_strong_etag_returns_partial_bytes(self, tmp_path):
        old_data, new_data, old, partial, new_path, new_records = \
            self._catalog(tmp_path)

        service = ArchiveService(partial)
        response = service.handle_request(
            rep_id='new', range_header='bytes=12-14',
            if_range='"new"')

        assert response.status == 206
        assert ('Content-Range', 'bytes 12-14/20') in response.headers
        assert b''.join(response.body) == new_data[12:15]

    def test_mismatched_if_range_needs_same_identity_full_response(self, tmp_path):
        old_data, new_data, old, partial, new_path, new_records = \
            self._catalog(tmp_path)

        service = ArchiveService(partial)

        with pytest.raises(RepresentationNotFound):
            service.handle_request(rep_id='new',
                                   range_header='bytes=12-14',
                                   if_range='"old"')

        # A weak validator with the same opaque text is not a match either.
        with pytest.raises(RepresentationNotFound):
            service.handle_request(rep_id='new',
                                   range_header='bytes=12-14',
                                   if_range='W/"new"')

        # After the newer capture's full record is imported, the mismatch
        # falls back to the full response from that same capture, not the
        # older complete capture.
        full_new = str(tmp_path / 'new-full.warc')
        full_new_records = make_warc(full_new, [
            {'payload': new_data, 'etag': '"new"'}])
        full_cat = build_catalog([{
            'representation_id': 'new-full',
            'url': 'http://example.com/media.mp4',
            'etag': '"new"',
            'total_length': 20,
            'segments': [
                {'start': 0, 'end': 19,
                 'sources': [source(full_new_records[0], full_new)]},
            ],
        }])

        service.publish(full_cat)

        response = service.handle_request(rep_id='new-full',
                                          range_header='bytes=12-14',
                                          if_range='"old"')
        assert response.status == 200
        assert b''.join(response.body) == new_data

    def test_zero_zero_is_one_byte(self, tmp_path):
        data = b'0123456789'
        path = str(tmp_path / 'media.warc')
        records = make_warc(path, [{'payload': data}])
        catalog = build_catalog([{
            'representation_id': 'tiny',
            'url': 'http://example.com/media.mp4',
            'etag': '"tiny"',
            'total_length': 10,
            'segments': [
                {'start': 0, 'end': 9,
                 'sources': [source(records[0], path)]},
            ],
        }])

        assert parse_byte_range('bytes=0-0', 10) == (0, 0)

        response = ArchiveService(catalog).handle_request(
            rep_id='tiny', range_header='bytes=0-0')
        assert response.status == 206
        assert ('Content-Range', 'bytes 0-0/10') in response.headers
        assert b''.join(response.body) == b'0'

    def test_unsatisfiable_ranges(self):
        assert parse_byte_range('bytes=0-', 10) == (0, 9)
        assert parse_byte_range('bytes=-3', 10) == (7, 9)

        with pytest.raises(RangeNotSatisfiable):
            parse_byte_range('bytes=10-', 10)
        with pytest.raises(RangeNotSatisfiable):
            parse_byte_range('bytes=5-4', 10)
        with pytest.raises(RangeNotSatisfiable):
            parse_byte_range('bytes=0-10', 10)

        assert etag_is_strong('"x"')
        assert not etag_is_strong('W/"x"')
        assert if_range_matches('"x"', '"x"')
        assert not if_range_matches('W/"x"', '"x"')
        assert not if_range_matches('W/"x"', 'W/"x"')


# ============================================================================
class TestHttpSurface(object):
    def test_catalog_and_pinned_routes(self, tmp_path):
        data = b'0123456789ABCDEFGHIJ'
        path = str(tmp_path / 'media.warc')
        records = make_warc(path, [
            {'payload': data[:12], 'etag': '"v1"'},
            {'payload': data[8:], 'etag': '"v1"'},
        ])
        catalog = build_catalog([{
            'representation_id': 'r1',
            'url': 'http://example.com/media.mp4',
            'etag': '"v1"',
            'mime': 'video/mp4',
            'total_length': 20,
            'segments': [
                {'start': 0, 'end': 11,
                 'sources': [source(records[0], path)]},
                {'start': 8, 'end': 19,
                 'sources': [source(records[1], path)]},
            ],
        }])

        app = MediaArchiveApp(ArchiveService(catalog))

        catalog_response = call_app(app, '/media')
        assert catalog_response['status'].startswith('200')
        info = json.loads(catalog_response['body'].decode('utf-8'))
        assert info['generation'] == 1
        assert info['representations'][0]['representation'] == 'r1'

        info_response = call_app(app, '/media/info/r1')
        assert json.loads(info_response['body'].decode('utf-8'))['complete']

        first = call_app(app, '/media/rep/r1',
                         headers={'Range': 'bytes=0-3'})
        assert first['status'].startswith('206')
        assert first['body'] == data[:4]
        pin = first['headers']['X-Archive-Pin']
        assert first['headers']['X-Archive-Pinned-URL'] == \
            '/media/pin/' + pin

        continued = call_app(app, '/media/pin/' + pin,
                             headers={'Range': 'bytes=10-12'})
        assert continued['status'].startswith('206')
        assert continued['body'] == data[10:13]

        head = call_app(app, '/media/rep/r1', method='HEAD',
                        headers={'Range': 'bytes=4-7'})
        assert head['status'].startswith('206')
        assert head['headers']['Content-Length'] == '4'
        assert head['body'] == b''

        full = call_app(app, '/media/rep/r1')
        assert full['status'].startswith('200')
        assert full['headers']['Content-Length'] == '20'
        assert full['body'] == data

        missing = call_app(app, '/media/info/nope')
        assert missing['status'].startswith('404')

        not_complete = call_app(app, '/media/pin/does-not-exist')
        assert not_complete['status'].startswith('404')
