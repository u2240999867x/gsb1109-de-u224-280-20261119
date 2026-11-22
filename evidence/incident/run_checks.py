#!/usr/bin/env python3
"""Run the offline incident check matrix and write results.json.

Every replay goes through a real WarcServer mounted on webtest (no sockets),
by the direct form endpoint and by the wrapped ``postreq`` endpoint.

Scenarios
---------
base
    The shipped collection.  A's exact key is shared with B's mis-indexed
    record; B's true key is absent and falls back to the legacy body.
reindex_b_only
    Re-index only orders-b.warc.  B's HTTP Content-Length is still short, so
    the rebuilt row still lands on A's key.
fix_b_only
    Rewrite request B's HTTP Content-Length to its real length (6) but leave
    the indexes untouched.  The old index still produces A's wrong 200.
fix_b_and_reindex
    Rewrite request B and re-index its WARC.  A resolves to A; B to B.
reindex_a_only
    Re-index only orders-a.warc.  B's row remains on A's key.
reindex_all
    Rebuild every index from the same WARC bytes.  The collision is a
    property of the stored request header, so the result does not change.
positive_a
    Only A and the legacy response.  A resolves to response A.
positive_b
    Only B, rebuilt after fixing its Content-Length.  B resolves to response B.
"""
import json
import os
import re
import shutil
import sys

from webtest import TestApp

from warcio.warcwriter import WARCWriter

from pywb.warcserver.basewarcserver import BaseWarcServer
from pywb.warcserver.handlers import DefaultResourceHandler
from pywb.warcserver.index.aggregator import SimpleAggregator
from pywb.warcserver.index.indexsource import FileIndexSource

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build as fixture  # noqa: E402

URL = fixture.URL
CLOSEST = fixture.TS
REQ_A = fixture.REQ_A
REQ_B = fixture.REQ_B
DECLARED_A = len(REQ_A)
DECLARED_B = fixture.REQ_B_DECLARED


def wrapped(body, declared):
    return (('POST /checkout.html HTTP/1.1\r\n'
             'Host: shop.example\r\n'
             'Content-Type: application/x-www-form-urlencoded\r\n'
             'Content-Length: %d\r\n'
             '\r\n') % declared).encode('ascii') + body


def make_app(dest):
    source = SimpleAggregator({
        'incident': FileIndexSource(
            os.path.join(dest, 'indexes', 'index.cdxj'))})
    handler = DefaultResourceHandler(
        source, os.path.join(dest, 'warcs') + os.sep)
    app = BaseWarcServer(debug=False)
    app.add_route('/incident', handler)
    return TestApp(app, lint=False)


def replay(app, body, postreq=False, declared=None):
    query = 'url=%s&closest=%s&matchType=exact' % (URL, CLOSEST)
    if postreq:
        resp = app.post('/incident/resource/postreq?' + query,
                        params=wrapped(body, declared if declared is not None
                                       else len(body)),
                        content_type='application/request', status='*')
    else:
        resp = app.post('/incident/resource?' + query,
                        params=body,
                        content_type='application/x-www-form-urlencoded',
                        status='*')
    cdx = resp.headers.get('Warcserver-Cdx', '')
    filename = re.search(r'"filename": "([^"]+)"', cdx)
    return {
        'status': resp.status,
        'selected_cdx': cdx,
        'selected_warc': filename.group(1) if filename else None,
        'body': resp.body.decode('latin-1'),
        'body_tail': resp.body[-20:].decode('latin-1'),
    }


def measure(dest):
    app = make_app(dest)
    return {
        'A': {
            'direct': replay(app, REQ_A, False),
            'postreq': replay(app, REQ_A, True, DECLARED_A),
            'postreq_archived_header': replay(app, REQ_A, True, DECLARED_A),
        },
        'B': {
            'direct': replay(app, REQ_B, False),
            'postreq': replay(app, REQ_B, True, len(REQ_B)),
            'postreq_archived_header': replay(app, REQ_B, True, DECLARED_B),
        },
    }


def rewrite_b(dest, fix_length):
    with open(os.path.join(dest, 'warcs', 'orders-b.warc'), 'wb') as fh:
        writer = WARCWriter(fh)
        fixture.write_response(writer, fixture.RESP_B, 'resp-b')
        fixture.write_request(writer, fixture.REQ_B, 'req-b', 'resp-b',
                              len(fixture.REQ_B) if fix_length else DECLARED_B)


def same_replay(left, right):
    return (left['status'] == right['status'] and
            left['selected_cdx'] == right['selected_cdx'] and
            left['body'] == right['body'])


def same_case(base, other):
    return all(same_replay(base[q][m], other[q][m])
               for q in ('A', 'B')
               for m in ('direct', 'postreq', 'postreq_archived_header'))


def run():
    root = os.path.join(HERE, 'variants')
    shutil.rmtree(root, ignore_errors=True)
    os.makedirs(root)

    cases = {}

    base = os.path.join(root, 'base')
    fixture.build(base)
    cases['base'] = measure(base)

    reindex_b = os.path.join(root, 'reindex_b_only')
    shutil.copytree(base, reindex_b)
    fixture.reindex_one(reindex_b, 'orders-b.warc')
    cases['reindex_b_only'] = measure(reindex_b)

    fix_b = os.path.join(root, 'fix_b_only')
    shutil.copytree(base, fix_b)
    rewrite_b(fix_b, fix_length=True)
    cases['fix_b_only'] = measure(fix_b)

    fix_and_reindex = os.path.join(root, 'fix_b_and_reindex')
    shutil.copytree(base, fix_and_reindex)
    rewrite_b(fix_and_reindex, fix_length=True)
    fixture.reindex_one(fix_and_reindex, 'orders-b.warc')
    cases['fix_b_and_reindex'] = measure(fix_and_reindex)

    reindex_a = os.path.join(root, 'reindex_a_only')
    shutil.copytree(base, reindex_a)
    fixture.reindex_one(reindex_a, 'orders-a.warc')
    cases['reindex_a_only'] = measure(reindex_a)

    reindex_all = os.path.join(root, 'reindex_all')
    shutil.copytree(base, reindex_all)
    fixture.reindex(reindex_all, list(fixture.ALL_WARCS))
    cases['reindex_all'] = measure(reindex_all)

    positive_a = os.path.join(root, 'positive_a')
    fixture.build(positive_a, warcs=('legacy-ro.warc', 'orders-a.warc'))
    cases['positive_a'] = measure(positive_a)

    positive_b = os.path.join(root, 'positive_b')
    fixture.build(positive_b, warcs=('orders-b.warc',), fix_b_length=True)
    cases['positive_b'] = measure(positive_b)

    base_case = cases['base']
    checks = {
        'base_direct_matches_postreq_for_A':
            same_replay(base_case['A']['direct'], base_case['A']['postreq']),
        'base_direct_matches_postreq_for_B':
            same_replay(base_case['B']['direct'], base_case['B']['postreq']),
        'reindex_b_only_unchanged_at_all_entry_points':
            same_case(base_case, cases['reindex_b_only']),
        'reindex_a_only_unchanged_at_all_entry_points':
            same_case(base_case, cases['reindex_a_only']),
        'reindex_all_unchanged_at_all_entry_points':
            same_case(base_case, cases['reindex_all']),
        'fix_b_without_reindex_unchanged_at_all_entry_points':
            same_case(base_case, cases['fix_b_only']),
        'fix_b_and_reindex_resolves_A_to_A':
            cases['fix_b_and_reindex']['A']['direct']['body_tail'].endswith(
                'ORDER-A-000\n'),
        'fix_b_and_reindex_resolves_B_to_B':
            cases['fix_b_and_reindex']['B']['direct']['body_tail'].endswith(
                'ORDER-B-002\n'),
        'positive_a_resolves_A':
            cases['positive_a']['A']['direct']['body_tail'].endswith(
                'ORDER-A-000\n'),
        'positive_b_resolves_B':
            cases['positive_b']['B']['direct']['body_tail'].endswith(
                'ORDER-B-002\n'),
        'base_A_selects_orders_b':
            base_case['A']['direct']['selected_warc'] == 'orders-b.warc',
        'base_B_selects_legacy':
            base_case['B']['direct']['selected_warc'] == 'legacy-ro.warc',
        'B_via_postreq_with_archived_header_selects_orders_b':
            base_case['B']['postreq_archived_header']['selected_warc']
            == 'orders-b.warc',
    }

    return {
        'url': URL,
        'closest': CLOSEST,
        'request_a': {'hex': REQ_A.hex(),
                      'latin1': REQ_A.decode('latin-1'),
                      'length': len(REQ_A),
                      'declared_length': DECLARED_A},
        'request_b': {'hex': REQ_B.hex(),
                      'latin1': REQ_B.decode('latin-1'),
                      'length': len(REQ_B),
                      'declared_length': DECLARED_B},
        'checks': checks,
        'cases': cases,
    }


def summarise(data):
    for name, case in data['cases'].items():
        print('==', name)
        for query in ('A', 'B'):
            for mode in ('direct', 'postreq', 'postreq_archived_header'):
                r = case[query][mode]
                print('  %s %-23s %-5s warc=%-15s body=%r'
                      % (query, mode, r['status'], r['selected_warc'],
                         r['body_tail']))
    print('== checks')
    for name, ok in sorted(data['checks'].items()):
        print('  %-58s %s' % (name, 'PASS' if ok else 'FAIL'))


if __name__ == '__main__':
    data = run()
    out = os.path.join(HERE, 'results.json')
    with open(out, 'w') as fh:
        json.dump(data, fh, indent=2, sort_keys=True)
        fh.write('\n')
    summarise(data)
    print('wrote', out)
