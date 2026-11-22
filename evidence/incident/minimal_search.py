#!/usr/bin/env python3
"""Exhaustively confirm the shortest request bytes that reproduce the key.

The constraint set is

  * at most 7 bytes,
  * at least one percent-encoded delimiter (%26 or %3D),
  * at least two ``a`` fields after unquote_plus (the duplicate-field case).

Prints every body that satisfies them, sorted by length.  The shortest is
5 bytes (``a%26a``), which is what the fixture uses: the smallest body that
keeps both an encoded delimiter and the duplicate ``a`` field.
"""
import itertools
from io import BytesIO
from urllib.parse import unquote_plus

from pywb.utils.canonicalize import canonicalize
from pywb.warcserver.inputrequest import MethodQueryCanonicalizer

URL = 'http://shop.example/checkout.html'
ALPHABET = list('a1=&%236Dx')


def key(body):
    machine = MethodQueryCanonicalizer(
        'POST', 'application/x-www-form-urlencoded', len(body),
        BytesIO(body), buffered_stream=BytesIO())
    return canonicalize(machine.append_query(URL))


def valid(body):
    raw = body.decode('latin-1')
    if '%26' not in raw and '%3D' not in raw:
        return False
    decoded = unquote_plus(body.decode('utf-8'))
    fields = [part.split('=', 1)[0] for part in decoded.split('&')]
    return fields.count('a') >= 2


def main():
    hits = []
    for length in range(1, 8):
        for tup in itertools.product(ALPHABET, repeat=length):
            body = ''.join(tup).encode()
            if valid(body):
                hits.append((len(body), body, key(body)))
    for length, body, body_key in sorted(hits):
        print('%2d  %-12r -> %s' % (length, body, body_key))
    print('total', len(hits))


if __name__ == '__main__':
    main()
