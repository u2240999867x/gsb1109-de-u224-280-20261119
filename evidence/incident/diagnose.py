#!/usr/bin/env python3
"""Collect the evidence an on-call responder needs to re-check the incident.

No network, no sockets: the WarcServer is mounted on webtest in-process.

For each of the two same-second POSTs it records

  * the shortest request bytes that reproduce (hex and latin-1),
  * the decoded form and the true index key,
  * the exact-key candidates (the "boundary" before selection),
  * the fuzzy candidates (where a missing key goes),
  * the selected CDX row and returned body for each entry variant,
        /incident/resource          direct form body
        /incident/resource/postreq  wrapped HTTP request, real body length
        .../postreq with the archived Content-Length (the masking case)
  * which archive layer still holds the original bytes.

It also shows the key of request B computed from its full WARC payload
(6 bytes) and from its HTTP Content-Length (5 bytes), which is exactly where
the two identities become indistinguishable.
"""
import json
import os
import sys
from io import BytesIO

from webtest import TestApp

from pywb.utils.canonicalize import canonicalize, calc_search_range
from pywb.warcserver.basewarcserver import BaseWarcServer
from pywb.warcserver.handlers import DefaultResourceHandler
from pywb.warcserver.index.aggregator import SimpleAggregator
from pywb.warcserver.index.cdxobject import CDXObject
from pywb.warcserver.index.fuzzymatcher import FuzzyMatcher
from pywb.warcserver.index.indexsource import FileIndexSource
from pywb.warcserver.inputrequest import MethodQueryCanonicalizer

HERE = os.path.dirname(os.path.abspath(__file__))
COLLECTION = os.path.join(HERE, 'collection')
URL = 'http://shop.example/checkout.html'
CLOSEST = '20260101120000'
INDEX = os.path.join(COLLECTION, 'indexes', 'index.cdxj')
WARCS = os.path.join(COLLECTION, 'warcs') + os.sep


def canon(body, length=None):
    length = len(body) if length is None else length
    query = MethodQueryCanonicalizer(
        'POST', 'application/x-www-form-urlencoded', length,
        BytesIO(body[:length]), buffered_stream=BytesIO())
    url = query.append_query(URL)
    return {
        'read_bytes': body[:length].decode('latin-1'),
        'read_hex': body[:length].hex(),
        'read_length': length,
        'canonical_query': query.query,
        'index_key': canonicalize(url),
        'search_range': list(calc_search_range(url, 'exact')),
    }


def exact_rows(key):
    start = key.encode('utf-8')
    end = start + b'!'
    rows = []
    with open(INDEX, 'rb') as fh:
        for line in fh:
            line_key = line.split(b' ', 1)[0]
            if start <= line_key < end:
                rows.append(CDXObject(line))
    return rows


def fuzzy_rows(url, alt_url):
    """Run the same FuzzyMatcher path replay uses (exact key first, fuzzy
    fallback with the method query attached)."""
    source = SimpleAggregator({'incident': FileIndexSource(INDEX)})
    matcher = FuzzyMatcher(None)
    rows, errs = matcher(source, {
        'url': url, 'alt_url': alt_url, 'matchType': 'exact',
        'closest': CLOSEST, '_closest_limit': 100})
    return list(rows), errs


def public(cdx):
    fields = ('urlkey', 'timestamp', 'url', 'mime', 'status', 'digest',
              'length', 'offset', 'filename', 'method', 'requestBody',
              'is_fuzzy')
    return {name: cdx.get(name) for name in fields if cdx.get(name) is not None}


def make_app():
    source = SimpleAggregator({'incident': FileIndexSource(INDEX)})
    handler = DefaultResourceHandler(source, WARCS)
    app = BaseWarcServer(debug=False)
    app.add_route('/incident', handler)
    return TestApp(app, lint=False)


def wrapped(body, declared=None):
    if declared is None:
        declared = len(body)
    head = ('POST /checkout.html HTTP/1.1\r\n'
            'Host: shop.example\r\n'
            'Content-Type: application/x-www-form-urlencoded\r\n'
            'Content-Length: %d\r\n'
            '\r\n') % declared
    return head.encode('ascii') + body


def replay(app, body, postreq, declared=None):
    query = 'url=%s&closest=%s&matchType=exact' % (URL, CLOSEST)
    if postreq:
        resp = app.post('/incident/resource/postreq?' + query,
                        params=wrapped(body, declared),
                        content_type='application/request', status='*')
    else:
        resp = app.post('/incident/resource?' + query,
                        params=body,
                        content_type='application/x-www-form-urlencoded',
                        status='*')
    return {
        'status': resp.status,
        'warc_target_uri': resp.headers.get('WARC-Target-URI'),
        'memento_datetime': resp.headers.get('Memento-Datetime'),
        'selected_cdx': resp.headers.get('Warcserver-Cdx', ''),
        'body_latin1': resp.body.decode('latin-1'),
        'body_tail': resp.body[-20:].decode('latin-1'),
    }


def boundary_for_a(key):
    rows = exact_rows(key)
    return {
        'layer_request_bytes': 'orders-a.warc: the WARC request payload is '
                               'the 5 raw bytes a%26a and is intact; it can '
                               'be recovered by (filename, offset, length)',
        'layer_http_headers': 'orders-a.warc: HTTP Content-Length is 5 and '
                              'matches the stored payload',
        'layer_index': 'key %s has %d rows; both point at distinct WARC '
                       'offsets and carry indistinguishable requestBody '
                       'values' % (key, len(rows)),
        'layer_selection': 'rows share timestamp 20260101120000, so closest '
                           'distance ties; the earlier row in the merged CDX '
                           'iterator wins (cdx_sort_closest position tie-break)',
    }


def boundary_for_b(true_key, indexed_key):
    return {
        'layer_request_bytes': 'orders-b.warc: the WARC request payload is '
                               'the full 6 bytes a%26a=; the trailing = '
                               'is recoverable only from the WARC payload',
        'layer_http_headers': 'orders-b.warc: HTTP Content-Length is 5, so '
                              'every header-driven reader (including '
                              'cdx-indexer -p) reads only a%26a and never '
                              'sees =; from this layer the two requests are '
                              'indistinguishable',
        'layer_index': 'the true key %s is absent; the row was filed under '
                       'the shared key %s' % (true_key, indexed_key),
        'layer_selection': 'exact lookup is empty; fuzzy lookup matches the '
                           'plain-URL legacy row and two POST rows, and '
                           'returns the legacy row first',
    }


def main():
    app = make_app()
    result = {
        'collection': os.path.relpath(COLLECTION, os.path.dirname(HERE)),
        'url': URL,
        'closest': CLOSEST,
        'requests': {},
    }

    bodies = {
        'A': b'a%26a',
        'B': b'a%26a=',
    }
    declared = {'A': len(bodies['A']), 'B': len(bodies['A'])}

    for name, body in bodies.items():
        length = declared[name]
        actual = canon(body, len(body))
        truncated = canon(body, length)
        exact = exact_rows(actual['index_key'])
        # The replay path sees the POST-derived url as alt_url.
        query = MethodQueryCanonicalizer(
            'POST', 'application/x-www-form-urlencoded', len(body),
            BytesIO(body), buffered_stream=BytesIO())
        fuzzy, fuzzy_errs = fuzzy_rows(URL, query.append_query(URL))
        entry = {
            'raw_body_latin1': body.decode('latin-1'),
            'raw_hex': body.hex(),
            'raw_length': len(body),
            'http_content_length': length,
            'true_key': actual['index_key'],
            'true_search_range': actual['search_range'],
            'key_from_declared_length': truncated['index_key'],
            'decoded_form_from_declared_length': truncated['canonical_query'],
            'exact_candidates': [public(c) for c in exact],
            'fuzzy_candidates': [public(c) for c in fuzzy],
            'fuzzy_errors': repr(fuzzy_errs),
            'direct': replay(app, body, False),
            'postreq': replay(app, body, True, len(body)),
            'postreq_archived_header': replay(app, body, True, length),
        }
        entry['boundary'] = boundary_for_a(truncated['index_key'])
        result['requests'][name] = entry

    result['requests']['B']['boundary'] = boundary_for_b(
        result['requests']['B']['true_key'],
        result['requests']['B']['key_from_declared_length'])

    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write('\n')


if __name__ == '__main__':
    main()
