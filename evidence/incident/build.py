#!/usr/bin/env python3
"""Build the deterministic offline fixture for the same-second POST incident.

Exactly two POST requests are archived, both at 2026-01-01T12:00:00Z and the
same URL:

  request A   raw b'a%26a'     (5 bytes)  -> decoded a&a (two ``a`` fields)
  request B   raw b'a%26a='    (6 bytes)  -> decoded a&a= (also two ``a``)
              B's WARC request record stores all 6 bytes, but its HTTP
              Content-Length header says 5.  The indexer trusts the header,
              reads only b'a%26a', and files B under request A's index key.

  legacy-ro.warc
              a read-only response with no request record and no
              ``__wb_method`` query.  A missing POST key falls back to this
              old body by fuzzy lookup, still HTTP 200 at the same second.

So A's exact key K1 is shared with B's mis-indexed row, and B's true key K2
is absent even though response B is in the archive.

All bytes are deterministic and uncompressed, so absolute offsets in the
WARC can be inspected directly.  WARC-Date is fixed and record ids are
explicit.
Run from the repository root::

    .venv/bin/python evidence/incident/build.py
"""
import os
import shutil
import stat
from io import BytesIO

from warcio.warcwriter import WARCWriter
from warcio.statusandheaders import StatusAndHeaders
from warcio.timeutils import timestamp_to_iso_date

from pywb.indexer.cdxindexer import write_cdx_index

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTION = os.path.join(HERE, 'collection')

TS = '20260101120000'
DATE = timestamp_to_iso_date(TS)
URL = 'http://shop.example/checkout.html'

REQ_A = b'a%26a'
REQ_B = b'a%26a='
REQ_B_DECLARED = len(REQ_A)          # 5: one byte short of the stored body

RESP_LEGACY = b'LEGACY-OLD-BODY\n'
RESP_A = b'ORDER-A-000\n'
RESP_B = b'ORDER-B-002\n'

ALL_WARCS = ('legacy-ro.warc', 'orders-a.warc', 'orders-b.warc')


def record_id(name):
    return '<urn:uuid:incident-%s>' % name


def write_response(writer, body, rid):
    headers = StatusAndHeaders(
        '200 OK',
        [('Content-Type', 'text/plain; charset=utf-8'),
         ('Content-Length', str(len(body)))],
        protocol='HTTP/1.0')
    writer.write_record(writer.create_warc_record(
        URL, 'response',
        payload=BytesIO(body), length=len(body),
        http_headers=headers,
        warc_headers_dict={'WARC-Date': DATE,
                           'WARC-Record-ID': record_id(rid)}))


def write_request(writer, body, rid, concurrent_to, http_length):
    headers = StatusAndHeaders(
        'POST /checkout.html HTTP/1.0',
        [('Host', 'shop.example'),
         ('Content-Type', 'application/x-www-form-urlencoded'),
         ('Content-Length', str(http_length))],
        protocol='HTTP/1.0', is_http_request=True)
    writer.write_record(writer.create_warc_record(
        URL, 'request',
        payload=BytesIO(body), length=len(body),
        http_headers=headers,
        warc_headers_dict={'WARC-Date': DATE,
                           'WARC-Record-ID': record_id(rid),
                           'WARC-Concurrent-To': record_id(concurrent_to)}))


def write_warcs(dest, warcs=ALL_WARCS, fix_b_length=False):
    warc_dir = os.path.join(dest, 'warcs')
    os.makedirs(warc_dir, exist_ok=True)

    if 'legacy-ro.warc' in warcs:
        with open(os.path.join(warc_dir, 'legacy-ro.warc'), 'wb') as fh:
            write_response(WARCWriter(fh, gzip=False), RESP_LEGACY, 'legacy-resp')

    if 'orders-a.warc' in warcs:
        with open(os.path.join(warc_dir, 'orders-a.warc'), 'wb') as fh:
            writer = WARCWriter(fh, gzip=False)
            write_response(writer, RESP_A, 'resp-a')
            write_request(writer, REQ_A, 'req-a', 'resp-a', len(REQ_A))

    if 'orders-b.warc' in warcs:
        with open(os.path.join(warc_dir, 'orders-b.warc'), 'wb') as fh:
            writer = WARCWriter(fh, gzip=False)
            write_response(writer, RESP_B, 'resp-b')
            write_request(writer, REQ_B, 'req-b', 'resp-b',
                          len(REQ_B) if fix_b_length else REQ_B_DECLARED)


def index_one(dest, name):
    cdx_path = os.path.join(dest, 'indexes', name + '.cdxj')
    os.makedirs(os.path.dirname(cdx_path), exist_ok=True)
    with open(cdx_path, 'wb') as out:
        with open(os.path.join(dest, 'warcs', name), 'rb') as inp:
            write_cdx_index(out, inp, name, cdxj=True, append_post=True,
                            sort=True)


def merge(dest, names):
    lines = []
    for name in names:
        path = os.path.join(dest, 'indexes', name + '.cdxj')
        if os.path.exists(path):
            with open(path, 'rb') as fh:
                lines.extend(fh.readlines())
    lines.sort()
    with open(os.path.join(dest, 'indexes', 'index.cdxj'), 'wb') as fh:
        fh.writelines(lines)


def reindex(dest, names):
    for name in names:
        index_one(dest, name)
    merge(dest, names)


def reindex_one(dest, name):
    """Regenerate one WARC's rows and splice them into the merged index,
    leaving every other WARC's rows exactly as they are."""
    index_one(dest, name)
    with open(os.path.join(dest, 'indexes', name + '.cdxj'), 'rb') as fh:
        new_rows = fh.readlines()

    merged_path = os.path.join(dest, 'indexes', 'index.cdxj')
    kept = []
    with open(merged_path, 'rb') as fh:
        for line in fh:
            if b'"filename": "%s"' % name.encode('utf-8') not in line:
                kept.append(line)
    kept.extend(new_rows)
    kept.sort()
    with open(merged_path, 'wb') as fh:
        fh.writelines(kept)


def make_read_only(path):
    os.chmod(path, stat.S_IRUSR | stat.S_IRGRP | stat.S_IROTH)


def build(dest=None, warcs=ALL_WARCS, fix_b_length=False):
    dest = dest or COLLECTION
    shutil.rmtree(dest, ignore_errors=True)
    os.makedirs(os.path.join(dest, 'warcs'))
    os.makedirs(os.path.join(dest, 'indexes'))
    write_warcs(dest, warcs=warcs, fix_b_length=fix_b_length)
    reindex(dest, list(warcs))

    legacy = os.path.join(dest, 'warcs', 'legacy-ro.warc')
    if os.path.exists(legacy):
        make_read_only(legacy)
    return dest


if __name__ == '__main__':
    print(build())
