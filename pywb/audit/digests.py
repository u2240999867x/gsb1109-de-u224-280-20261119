"""Content digests used to prove that a replayed record is the exact
representation that an audit selection identifier was registered for.

Digests are computed from bytes that are read out of the WARC volumes
themselves.  An index entry (CDX) is only a claim: digest equality between
a CDX field and a re-read record is part of the verification, never a
substitute for opening the volume.
"""

import base64
import hashlib


def _b32(sha1_digest):
    """base32 encoding without padding, matching the WARC ``sha1:`` convention."""
    return base64.b32encode(sha1_digest).decode('ascii').rstrip('=')


def sha1_b32(data):
    """Return the base32 SHA-1 digest of a byte string."""
    return _b32(hashlib.sha1(data).digest())


def payload_digest(payload):
    """Digest of the (already de-chunked / decompressed) response body."""
    return sha1_b32(payload)


def representation_digest(status, headers, payload):
    """Digest of the full HTTP representation: status line, header block, body.

    Two captures with identical bodies but different ``Content-Type`` headers
    therefore get different representation digests, while byte-identical
    independent copies get the same one.
    """
    hasher = hashlib.sha1()
    hasher.update(status.encode('iso-8859-1'))
    hasher.update(b'\r\n')
    for name, value in headers:
        hasher.update(name.encode('iso-8859-1'))
        hasher.update(b': ')
        hasher.update(value.encode('iso-8859-1'))
        hasher.update(b'\r\n')
    hasher.update(b'\r\n')
    hasher.update(payload)
    return _b32(hasher.digest())


def header_digest(headers):
    """Digest of the HTTP header block alone (one ``name: value`` per line)."""
    hasher = hashlib.sha1()
    for name, value in headers:
        hasher.update(name.encode('iso-8859-1'))
        hasher.update(b': ')
        hasher.update(value.encode('iso-8859-1'))
        hasher.update(b'\r\n')
    return _b32(hasher.digest())
