"""Signed, self-contained resume credentials.

A credential carries the complete review state: what each source has paged,
which candidates were seen or withdrawn, and which records were already
opened.  The receiver therefore neither re-pages finished sources nor reopens
unchanged evidence; it also cannot silently drop a withdrawal.

The credential is signed so that a caller cannot forge "day complete" by
editing a cursor.  The signing key is process-local by default; pass a stable
key when credentials must survive a restart.
"""

import base64
import hashlib
import hmac
import json
import os

from .model import ReviewState


TOKEN_VERSION = 1


class ResumeError(Exception):
    pass


def _b64encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode('ascii')


def _b64decode(text):
    padding = '=' * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class CredentialCodec(object):
    def __init__(self, key=None):
        if key is None:
            key = os.environ.get('PYWB_REVIEW_KEY')
        if key is None:
            key = os.urandom(32)
            self.ephemeral = True
        else:
            self.ephemeral = False

        if isinstance(key, str):
            key = key.encode('utf-8')
        self.key = key

    def _sign(self, body):
        return hmac.new(self.key, body, hashlib.sha256).digest()

    def encode(self, state):
        payload = {'v': TOKEN_VERSION, 'state': state.to_dict()}
        body = json.dumps(payload, sort_keys=True,
                          separators=(',', ':')).encode('utf-8')
        signature = _b64encode(self._sign(body))
        return _b64encode(body) + '.' + signature

    def decode(self, token):
        if not isinstance(token, str) or '.' not in token:
            raise ResumeError('malformed resume credential')

        encoded, signature = token.rsplit('.', 1)
        try:
            body = _b64decode(encoded)
            given = _b64decode(signature)
        except Exception:
            raise ResumeError('malformed resume credential')

        expected = self._sign(body)
        if not hmac.compare_digest(given, expected):
            raise ResumeError('resume credential signature mismatch')

        try:
            payload = json.loads(body.decode('utf-8'))
        except Exception:
            raise ResumeError('malformed resume credential payload')

        if payload.get('v') != TOKEN_VERSION:
            raise ResumeError('unsupported resume credential version')

        return ReviewState.from_dict(payload['state'])


def encode(state, key=None):
    return CredentialCodec(key).encode(state)


def decode(token, key=None):
    return CredentialCodec(key).decode(token)
