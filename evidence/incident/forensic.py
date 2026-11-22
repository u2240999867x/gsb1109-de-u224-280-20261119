#!/usr/bin/env python3
"""Recover request identity from the archive itself, without replay.

For every WARC in the shipped collection this script

  1. lists the request records with their stored payload bytes, declared
     HTTP Content-Length, record ids and WARC-Concurrent-To pairing;
  2. recomputes the index key from the *full* payload and from the *declared*
     header length;
  3. parses every CDXJ row and flags rows whose key does not match the full
     payload of the request they were built from (``false_alias``).

This separates what is still recoverable at the WARC layer (exact bytes,
pairing, digest) from what is already lost at the HTTP-header layer.

Exit status is 0 when the expected incident shape is found.
"""
import json
import os
import sys
from io import BytesIO

from warcio.archiveiterator import ArchiveIterator

from pywb.utils.canonicalize import canonicalize
from pywb.warcserver.index.cdxobject import CDXObject
from pywb.warcserver.inputrequest import MethodQueryCanonicalizer

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTION = os.path.join(HERE, 'collection')
WARCS = os.path.join(COLLECTION, 'warcs')
INDEX = os.path.join(COLLECTION, 'indexes', 'index.cdxj')
URL = 'http://shop.example/checkout.html'


def key_for(body, length):
    query = MethodQueryCanonicalizer(
        'POST', 'application/x-www-form-urlencoded', length,
        BytesIO(body), buffered_stream=BytesIO())
    return canonicalize(query.append_query(URL))


def scan_warcs():
    out = {}
    for filename in sorted(os.listdir(WARCS)):
        if not filename.endswith('.warc'):
            continue
        path = os.path.join(WARCS, filename)
        records = []
        with open(path, 'rb') as fh:
            for record in ArchiveIterator(fh):
                item = {
                    'type': record.rec_type,
                    'record_id': record.rec_headers.get_header('WARC-Record-ID'),
                    'concurrent_to': record.rec_headers.get_header(
                        'WARC-Concurrent-To'),
                    'target_uri': record.rec_headers.get_header(
                        'WARC-Target-URI'),
                    'date': record.rec_headers.get_header('WARC-Date'),
                    'payload_digest': record.rec_headers.get_header(
                        'WARC-Payload-Digest'),
                }
                if record.rec_type == 'request':
                    body = record.raw_stream.read()
                    declared = int(record.http_headers.get_header(
                        'Content-Length'))
                    item.update({
                        'raw_body_hex': body.hex(),
                        'raw_body_latin1': body.decode('latin-1'),
                        'raw_length': len(body),
                        'declared_length': declared,
                        'key_from_full_body': key_for(body, len(body)),
                        'key_from_declared_length': key_for(body, declared),
                        'header_matches_payload': declared == len(body),
                    })
                records.append(item)
        out[filename] = records
    return out


def scan_index():
    rows = []
    with open(INDEX, 'rb') as fh:
        for line in fh:
            cdx = CDXObject(line)
            rows.append({
                'urlkey': cdx.get('urlkey'),
                'timestamp': cdx.get('timestamp'),
                'filename': cdx.get('filename'),
                'offset': cdx.get('offset'),
                'digest': cdx.get('digest'),
                'requestBody': cdx.get('requestBody'),
                'method': cdx.get('method'),
            })
    return rows


def cross_check(warcs, rows):
    findings = []
    for warc, records in warcs.items():
        for record in records:
            if record['type'] != 'request':
                continue
            for row in rows:
                if row['filename'] != warc:
                    continue
                if row['offset'] != '0':
                    continue
                if row['urlkey'] != record['key_from_declared_length']:
                    continue
                mismatch = (row['urlkey'] != record['key_from_full_body'] or
                            row['requestBody'] !=
                            record['raw_body_latin1'][
                                :record['declared_length']].replace('%26', '&'))
                findings.append({
                    'warc': warc,
                    'record_id': record['record_id'],
                    'row_urlkey': row['urlkey'],
                    'key_from_full_body': record['key_from_full_body'],
                    'key_from_declared_length':
                        record['key_from_declared_length'],
                    'false_alias': row['urlkey'] != record['key_from_full_body'],
                    'stored_body_hex': record['raw_body_hex'],
                    'declared_length': record['declared_length'],
                    'payload_length': record['raw_length'],
                })
    return findings


def main():
    warcs = scan_warcs()
    rows = scan_index()
    findings = cross_check(warcs, rows)

    payloads = {}
    for warc, records in warcs.items():
        for record in records:
            if record['type'] == 'request':
                payloads[record['raw_body_hex']] = {
                    'warc': warc,
                    'record_id': record['record_id'],
                    'concurrent_to': record['concurrent_to'],
                    'raw_length': record['raw_length'],
                    'declared_length': record['declared_length'],
                    'header_matches_payload':
                        record['header_matches_payload'],
                    'key_from_full_body': record['key_from_full_body'],
                    'key_from_declared_length':
                        record['key_from_declared_length'],
                }

    report = {
        'request_payloads': payloads,
        'index_rows': rows,
        'cross_check': findings,
        'summary': {
            'two_distinct_request_payloads': len(payloads) == 2,
            'one_header_lies_about_length':
                any(not p['header_matches_payload']
                    for p in payloads.values()),
            'false_alias_row_found': any(f['false_alias'] for f in findings),
        },
    }
    json.dump(report, sys.stdout, indent=2)
    sys.stdout.write('\n')

    if all(report['summary'].values()):
        return 0
    return 1


if __name__ == '__main__':
    sys.exit(main())
