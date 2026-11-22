#!/usr/bin/env python3
"""Byte-level identity check: which layer still knows which request is which.

Prints, for both POSTs, the raw WARC request payload, the HTTP header length,
the key computed from each, and the SHA-256 of the exact bytes.  This is the
evidence that the original request bytes are recoverable from the WARC even
when the index can no longer tell the two submissions apart.
"""
import hashlib
import json
import os

from warcio.archiveiterator import ArchiveIterator

from pywb.indexer.archiveindexer import DefaultRecordParser

from pywb.utils.canonicalize import canonicalize
from pywb.warcserver.inputrequest import MethodQueryCanonicalizer

HERE = os.path.dirname(os.path.abspath(__file__))
WARCS = os.path.join(HERE, 'collection', 'warcs')
URL = 'http://shop.example/checkout.html'


def key_for(body, length):
    from io import BytesIO
    query = MethodQueryCanonicalizer(
        'POST', 'application/x-www-form-urlencoded', length,
        BytesIO(body), buffered_stream=BytesIO())
    return canonicalize(query.append_query(URL))


def collect():
    out = {}
    for filename in sorted(os.listdir(WARCS)):
        if not filename.endswith('.warc'):
            continue
        path = os.path.join(WARCS, filename)
        records = []
        with open(path, 'rb') as fh:
            for record in ArchiveIterator(fh):
                if record.rec_type != 'request':
                    continue
                body = record.raw_stream.read()
                declared = record.http_headers.get_header('Content-Length')
                records.append({
                    'warc': filename,
                    'record_id': record.rec_headers.get_header('WARC-Record-ID'),
                    'concurrent_to': record.rec_headers.get_header(
                        'WARC-Concurrent-To'),
                    'warc_date': record.rec_headers.get_header('WARC-Date'),
                    'raw_body_latin1': body.decode('latin-1'),
                    'raw_body_hex': body.hex(),
                    'raw_length': len(body),
                    'sha256': hashlib.sha256(body).hexdigest(),
                    'http_content_length': declared,
                    'key_from_full_body': key_for(body, len(body)),
                    'key_from_header_length': key_for(body, int(declared)),
                })
        out[filename] = records

    responses = {}
    for filename in sorted(os.listdir(WARCS)):
        path = os.path.join(WARCS, filename)
        if not filename.endswith('.warc'):
            continue
        named = []
        with open(path, 'rb') as fh:
            for record in ArchiveIterator(fh):
                named.append({
                    'type': record.rec_type,
                    'record_id': record.rec_headers.get_header('WARC-Record-ID'),
                    'concurrent_to': record.rec_headers.get_header(
                        'WARC-Concurrent-To'),
                    'payload_digest': record.rec_headers.get_header(
                        'WARC-Payload-Digest'),
                })
        responses[filename] = named
    return out, responses


def main():
    requests, records = collect()
    print(json.dumps({'request_records': requests,
                      'record_pairs': records}, indent=2))


if __name__ == '__main__':
    main()
