#!/usr/bin/env python3
"""Map every WARC record to its absolute byte range and its payload body.

The fixture WARCs are uncompressed, so the bytes can be walked directly:

    WARC/1.0\\r\\n
    Header: value\\r\\n
    ...
    \\r\\n
    <Content-Length bytes of block>
    \\r\\n\\r\\n

For request records the block starts with an HTTP request line and headers;
the request body starts after the next blank line.  That (file, body_offset,
body_length) triple is where the original submission bytes can still be
read, independently of the CDX index.
"""
import hashlib
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WARCS = os.path.join(HERE, 'collection', 'warcs')


def parse_headers(buf, pos):
    headers = []
    while True:
        end = buf.find(b'\r\n', pos)
        if end < 0:
            raise ValueError('unterminated header at %d' % pos)
        line = buf[pos:end]
        pos = end + 2
        if not line:
            return headers, pos
        name, _, value = line.partition(b':')
        headers.append((name.decode('latin-1').lower(),
                        value.strip().decode('latin-1')))


def header(headers, name, default=None):
    for key, value in headers:
        if key == name:
            return value
    return default


def scan(path, name):
    with open(path, 'rb') as fh:
        buf = fh.read()

    records = []
    pos = 0
    while pos < len(buf):
        if not buf.startswith(b'WARC/1.0\r\n', pos):
            # tolerate the record terminator between records
            next_pos = buf.find(b'WARC/1.0\r\n', pos)
            if next_pos < 0:
                break
            pos = next_pos
        start = pos
        version, pos = parse_headers(buf, pos + len(b'WARC/1.0\r\n'))
        headers = version
        block_len = int(header(headers, 'content-length'))
        block_start = pos
        block = buf[block_start:block_start + block_len]
        rec = {
            'warc': name,
            'record_offset': start,
            'record_block_length': block_len,
            'record_end': block_start + block_len,
            'record_id': header(headers, 'warc-record-id'),
            'record_type': header(headers, 'warc-type'),
            'concurrent_to': header(headers, 'warc-concurrent-to'),
            'target_uri': header(headers, 'warc-target-uri'),
            'date': header(headers, 'warc-date'),
            'payload_digest': header(headers, 'warc-payload-digest'),
            'sha256': hashlib.sha256(block).hexdigest(),
        }
        if rec['record_type'] == 'request':
            end_of_http_headers = block.find(b'\r\n\r\n')
            body = block[end_of_http_headers + 4:]
            rec.update({
                'body_offset': block_start + end_of_http_headers + 4,
                'body_length': len(body),
                'body_hex': body.hex(),
                'body_latin1': body.decode('latin-1'),
                'http_content_length': None,
            })
            for key, value in parse_headers(block, 0)[0]:
                if key == 'content-length':
                    rec['http_content_length'] = value
        records.append(rec)
        pos = block_start + block_len
    return records


def main():
    report = {}
    for name in sorted(os.listdir(WARCS)):
        if name.endswith('.warc'):
            report[name] = scan(os.path.join(WARCS, name), name)
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
