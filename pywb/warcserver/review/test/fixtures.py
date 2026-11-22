"""WARC fixtures for the UTC-day capture review acceptance tests.

Each fixture is a real gzipped WARC written by warcio, plus a CDXJ line per
record.  Records are located by the offset/length pairs returned by
``ArchiveIterator``, so the tests exercise the same block loading path pywb
uses for replay rather than an in-memory shortcut.
"""

import json
import os

from io import BytesIO

from warcio.archiveiterator import ArchiveIterator
from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter


URL = 'http://example.com/review'


class WarcFixture(object):
    def __init__(self, directory, name, url=URL):
        self.directory = str(directory)
        self.name = name
        self.url = url
        self.path = os.path.join(self.directory, name)
        self.records = []
        self._fh = open(self.path, 'wb')
        self._writer = WARCWriter(self._fh, gzip=True)

    def _http_headers(self, body, status='200 OK', extra=None):
        headers = [('Content-Type', 'text/html'),
                   ('Content-Length', str(len(body)))]
        if extra:
            headers.extend(extra)
        return StatusAndHeaders(status, headers, protocol='HTTP/1.1')

    def response(self, date, body, status='200 OK', extra=None, url=None):
        body = body.encode('utf-8')
        record = self._writer.create_warc_record(
            url or self.url, 'response',
            payload=BytesIO(body),
            http_headers=self._http_headers(body, status, extra),
            warc_headers_dict={'WARC-Date': date})
        self._writer.write_record(record)
        self._fh.flush()
        return self._locate_last()

    def revisit(self, date, body, refers_to_uri, refers_to_date,
                status='200 OK', extra=None, url=None, body_ref=None):
        body = body.encode('utf-8')
        record = self._writer.create_revisit_record(
            url or self.url,
            digest='sha1:' + _digest(body),
            refers_to_uri=refers_to_uri,
            refers_to_date=refers_to_date,
            http_headers=self._http_headers(body, status, extra),
            warc_headers_dict={'WARC-Date': date})
        self._writer.write_record(record)
        self._fh.flush()
        located = self._locate_last()
        located['body_ref'] = body_ref
        return located

    def _locate_last(self):
        self._fh.flush()
        with open(self.path, 'rb') as fh:
            iterator = ArchiveIterator(fh)
            last = None
            for record in iterator:
                last = (iterator.get_record_offset(),
                        iterator.get_record_length(),
                        record.rec_headers.get_header('WARC-Date'),
                        record.rec_type)
        if last is None:
            raise AssertionError('no record written to ' + self.path)

        info = {'filename': self.name,
                'offset': last[0],
                'length': last[1],
                'warc_date': last[2],
                'warc_type': last[3]}
        self.records.append(info)
        return info

    def close(self):
        self._fh.close()

    def cdx(self, timestamp, record, url=None, digest='-', status='200',
            mime='text/html', access='allow', body_ref=None):
        """One CDXJ dict for a record, as a review source would list it."""
        return {'url': url or self.url,
                'timestamp': timestamp,
                'filename': record['filename'],
                'offset': record['offset'],
                'length': record['length'],
                'digest': digest,
                'status': status,
                'mime': mime,
                'access': access,
                'body_ref': body_ref}


def _digest(body):
    import hashlib
    return hashlib.sha1(body).hexdigest().upper()[:32]


def write_cdxj(path, entries):
    with open(path, 'w') as fh:
        for entry in entries:
            key = entry['urlkey']
            data = {k: v for k, v in entry.items() if k != 'urlkey'}
            fh.write('{0} {1} {2}\n'.format(key, entry['timestamp'],
                                            json.dumps(data)))
    return path


def build_two_source_fixture(root, url=URL):
    """Build the two-source scenario from the task statement.

    Source A (``a.warc.gz``):
      * a corrupt-offset row whose next row, same volume, is readable;
      * a duplicate row for the readable record.

    Source B (``b.warc.gz``):
      * a CDX candidate whose true WARC-Date is the next day;
      * a valid same-day revisit whose payload points at the previous day.
    """
    root = str(root)
    os.makedirs(root, exist_ok=True)

    a = WarcFixture(root, 'a.warc.gz', url=url)
    # The corrupt row points into the middle of the gzip member.
    a_readable = a.response('2026-11-19T08:00:00Z',
                            'source A, same-day body')
    a_corrupt = dict(a_readable)
    a_corrupt['offset'] = a_readable['offset'] + 7
    a_duplicate = dict(a_readable)
    a.close()

    b = WarcFixture(root, 'b.warc.gz', url=url)
    # Previous-day payload, referenced by a same-day revisit.
    b_body = b.response('2026-11-18T23:50:00Z',
                        'source B, previous-day payload')
    b_revisit = b.revisit('2026-11-19T09:00:00Z',
                          'source B, same-day revisit',
                          refers_to_uri=url,
                          refers_to_date='2026-11-18T23:50:00Z',
                          body_ref={'filename': b_body['filename'],
                                    'offset': b_body['offset'],
                                    'length': b_body['length']})
    # CDX says this is on the 19th; the record says the 20th.
    b_next_day = b.response('2026-11-20T00:05:00Z',
                            'source B, actually next day')
    # First record of a later page; its record date is what a source-order
    # boundary is confirmed against.
    b_probe = b.response('2026-11-19T17:00:00Z',
                         'source B, late same-day record')
    b.close()

    return {'root': root,
            'url': url,
            'a': {'readable': a.cdx('20261119080000', a_readable,
                                    digest='SHA1:AAAA'),
                  'corrupt': a.cdx('20261119073000', a_corrupt,
                                   digest='SHA1:BBBB'),
                  'duplicate': a.cdx('20261119080000', a_duplicate,
                                     digest='SHA1:AAAA')},
            'b': {'body': b_body,
                  'revisit': b.cdx('20261119090000', b_revisit,
                                   digest='SHA1:CCCC', mime='warc/revisit',
                                   body_ref=b_revisit['body_ref']),
                  'next_day': b.cdx('20261119235959', b_next_day,
                                    digest='SHA1:DDDD'),
                  'probe': b.cdx('20261119170000', b_probe,
                                 digest='SHA1:EEEE')}}
