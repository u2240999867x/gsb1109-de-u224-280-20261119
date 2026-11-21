"""Serializable inspection credentials for tail-only parquet reads.

A credential records which object version an inspect report belongs to, how
much of the footer has been verified, and the exact byte ranges that may be
reused.  It deliberately contains footer bytes only: data pages and a full
object download never enter a credential.
"""

import base64
import hashlib
import json
import os
import tempfile
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

CREDENTIAL_VERSION = 1


class CredentialError(Exception):
    pass


class CredentialMismatch(CredentialError):
    pass


class RangeConflict(CredentialError):
    pass


class InvalidRange(CredentialError):
    pass


def normalize_etag(etag: Optional[str]) -> Optional[str]:
    if etag is None:
        return None
    return etag.strip().strip('"').lower()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


@dataclass(frozen=True)
class EndpointRef:
    endpoint: str = ''
    region: Optional[str] = None

    def matches(self, other: 'EndpointRef') -> bool:
        return self.endpoint.rstrip('/') == other.endpoint.rstrip('/') and self.region == other.region

    def to_dict(self) -> dict:
        return {'endpoint': self.endpoint, 'region': self.region}

    @classmethod
    def from_dict(cls, value: dict) -> 'EndpointRef':
        return cls(endpoint=value.get('endpoint') or '', region=value.get('region'))

    @classmethod
    def for_client(cls, endpoint_url: Optional[str], region: Optional[str]) -> 'EndpointRef':
        return cls(endpoint=(endpoint_url or '').rstrip('/'), region=region)


@dataclass(frozen=True)
class ObjectIdentity:
    size: int
    etag: Optional[str] = None
    version_id: Optional[str] = None
    last_modified: Optional[str] = None

    def same_version(self, other: 'ObjectIdentity') -> bool:
        if self.version_id and other.version_id:
            return self.version_id == other.version_id
        if self.etag and other.etag:
            return normalize_etag(self.etag) == normalize_etag(other.etag) and self.size == other.size
        return False

    def to_dict(self) -> dict:
        return {
            'size': self.size,
            'etag': self.etag,
            'version_id': self.version_id,
            'last_modified': self.last_modified,
        }

    @classmethod
    def from_dict(cls, value: dict) -> 'ObjectIdentity':
        return cls(
            size=int(value['size']),
            etag=value.get('etag'),
            version_id=value.get('version_id'),
            last_modified=value.get('last_modified'),
        )


@dataclass
class FooterRange:
    start: int
    end: int
    sha256: str
    etag: Optional[str] = None
    version_id: Optional[str] = None

    def contains(self, start: int, end: int) -> bool:
        return self.start <= start and end <= self.end

    def overlaps(self, start: int, end: int) -> bool:
        return self.start < end and start < self.end

    def to_dict(self) -> dict:
        return {
            'start': self.start,
            'end': self.end,
            'sha256': self.sha256,
            'etag': self.etag,
            'version_id': self.version_id,
        }

    @classmethod
    def from_dict(cls, value: dict) -> 'FooterRange':
        return cls(
            start=int(value['start']),
            end=int(value['end']),
            sha256=value['sha256'],
            etag=value.get('etag'),
            version_id=value.get('version_id'),
        )


@dataclass
class InspectionCredential:
    """A resumable, version-pinned inspect credential.

    ``footer_bytes`` is a bytearray indexed from ``footer_start``.  Only bytes
    covered by ``ranges`` are meaningful; everything else is zero-filled.
    """

    endpoint: EndpointRef
    bucket: str
    key: str
    identity: Optional[ObjectIdentity] = None
    footer_length: Optional[int] = None
    footer_bytes: bytearray = field(default_factory=bytearray)
    ranges: List[FooterRange] = field(default_factory=list)
    simple_report: Optional[str] = None
    simple_identity: Optional[ObjectIdentity] = None
    detail_report: Optional[str] = None
    detail_identity: Optional[ObjectIdentity] = None
    unavailable_reason: Optional[str] = None
    credential_version: int = CREDENTIAL_VERSION

    @property
    def footer_start(self) -> Optional[int]:
        if self.identity is None or self.footer_length is None:
            return None
        return self.identity.size - 8 - self.footer_length

    @property
    def footer_end(self) -> Optional[int]:
        if self.footer_start is None:
            return None
        return self.footer_start + self.footer_length

    @property
    def known_prefix_length(self) -> int:
        """Number of leading footer bytes covered by one verified range.

        Only a contiguous run from the first footer byte is useful for
        incremental parsing.  Ranges that start later still count for the
        detailed phase but do not extend this prefix.
        """
        if self.footer_start is None:
            return 0
        cursor = self.footer_start
        for r in sorted(self.ranges, key=lambda x: x.start):
            if r.end <= cursor:
                continue
            if r.start > cursor:
                break
            cursor = max(cursor, r.end)
        return cursor - self.footer_start

    def check_target(self, endpoint: EndpointRef, bucket: str, key: str) -> None:
        if not self.endpoint.matches(endpoint):
            raise CredentialMismatch(
                f'credential belongs to endpoint {self.endpoint.endpoint or "(default)"}, '
                f'not {endpoint.endpoint or "(default)"}'
            )
        if self.bucket != bucket or self.key != key:
            raise CredentialMismatch(
                f'credential belongs to s3://{self.bucket}/{self.key}, '
                f'not s3://{bucket}/{key}'
            )

    def bind(self, identity: ObjectIdentity, footer_length: int) -> None:
        if self.identity is not None and not self.identity.same_version(identity):
            raise CredentialMismatch('credential is already bound to a different object version')
        if footer_length < 0:
            raise InvalidRange('negative footer length')
        if footer_length > identity.size - 8:
            raise InvalidRange('footer length exceeds object size')
        if self.identity is None:
            self.identity = identity
        if self.footer_length is None:
            self.footer_length = footer_length
            self.footer_bytes = bytearray(footer_length)
        elif self.footer_length != footer_length:
            raise CredentialMismatch(
                f'credential footer length {self.footer_length} differs from {footer_length}'
            )

    def _range_offset(self, position: int) -> int:
        base = self.footer_start
        if base is None:
            raise InvalidRange('credential is not bound to a footer')
        offset = position - base
        if offset < 0 or offset > (self.footer_length or 0):
            raise InvalidRange(f'position {position} is outside the footer')
        return offset

    def covered(self, start: int, end: int) -> bool:
        return any(r.contains(start, end) for r in self.ranges)

    def missing_intervals(self) -> List[Tuple[int, int]]:
        if self.footer_start is None:
            return []
        start, end = self.footer_start, self.footer_end
        result: List[Tuple[int, int]] = []
        cursor = start
        for r in sorted(self.ranges, key=lambda x: x.start):
            if r.end <= cursor:
                continue
            if r.start > cursor:
                result.append((cursor, min(r.start, end)))
            cursor = max(cursor, r.end)
            if cursor >= end:
                break
        if cursor < end:
            result.append((cursor, end))
        return result

    def add_range(self, start: int, data: bytes, identity: Optional[ObjectIdentity] = None) -> None:
        if not data:
            return
        end = start + len(data)
        if self.identity is None:
            raise InvalidRange('cannot add footer bytes before binding an object')
        if identity is not None and not self.identity.same_version(identity):
            raise CredentialMismatch('range was read from a different object version')
        if self.footer_start is None or self.footer_end is None:
            raise InvalidRange('cannot add footer bytes before the footer length is known')
        if start < self.footer_start or end > self.footer_end:
            raise InvalidRange(f'range [{start},{end}) is outside footer [{self.footer_start},{self.footer_end})')

        for existing in self.ranges:
            if not existing.overlaps(start, end):
                continue
            if existing.etag and normalize_etag(existing.etag) != normalize_etag(self.identity.etag):
                raise CredentialMismatch('existing footer range has different version evidence')
            os_ = max(start, existing.start)
            oe = min(end, existing.end)
            old = bytes(self.footer_bytes[self._range_offset(os_):self._range_offset(oe)])
            new = data[os_ - start:oe - start]
            if old != new:
                raise RangeConflict(
                    f'range [{os_},{oe}) conflicts with already verified footer bytes'
                )

        offset = self._range_offset(start)
        self.footer_bytes[offset:offset + len(data)] = data
        self.ranges.append(FooterRange(
            start=start,
            end=end,
            sha256=_sha256(data),
            etag=self.identity.etag,
            version_id=self.identity.version_id,
        ))
        self.ranges.sort(key=lambda x: x.start)
        self._coalesce()

    def _coalesce(self) -> None:
        merged: List[FooterRange] = []
        for r in sorted(self.ranges, key=lambda x: x.start):
            if merged and merged[-1].end == r.start and merged[-1].etag == r.etag and merged[-1].version_id == r.version_id:
                merged[-1] = FooterRange(
                    start=merged[-1].start,
                    end=r.end,
                    sha256=_sha256(
                        bytes(self.footer_bytes[self._range_offset(merged[-1].start):self._range_offset(r.end)])
                    ),
                    etag=merged[-1].etag,
                    version_id=merged[-1].version_id,
                )
            else:
                merged.append(r)
        self.ranges = merged

    def verified_footer_prefix(self) -> Optional[bytes]:
        """Return the longest contiguous verified footer prefix.

        Every returned byte is checked against the SHA-256 recorded when it
        was accepted, so a tampered credential file cannot inject footer
        bytes without being noticed.
        """
        if self.footer_start is None or self.footer_length is None:
            return None
        end = self.footer_start + self.known_prefix_length
        if end == self.footer_start:
            return None
        data = bytes(self.footer_bytes[:self.known_prefix_length])
        cursor = self.footer_start
        for r in sorted(self.ranges, key=lambda x: x.start):
            if r.start > cursor:
                break
            if r.end <= cursor:
                continue
            slice_start = max(r.start, cursor)
            slice_end = min(r.end, end)
            chunk = bytes(
                self.footer_bytes[
                    self._range_offset(slice_start):self._range_offset(slice_end)
                ]
            )
            if _sha256(chunk) != r.sha256:
                raise RangeConflict(
                    f'credential bytes for footer range [{slice_start},{slice_end}) '
                    'do not match their recorded digest'
                )
            cursor = slice_end
            if cursor >= end:
                break
        return data

    def check_ranges(self) -> None:
        """Verify every stored range against its recorded SHA-256."""
        for r in self.ranges:
            offset = self._range_offset(r.start)
            chunk = bytes(self.footer_bytes[offset:offset + (r.end - r.start)])
            if _sha256(chunk) != r.sha256:
                raise RangeConflict(
                    f'credential bytes for footer range [{r.start},{r.end}) '
                    'do not match their recorded digest'
                )

    def footer_complete(self) -> bool:
        if self.footer_length is None or self.footer_start is None:
            return False
        return self.covered(self.footer_start, self.footer_end)

    def complete_footer_bytes(self) -> bytes:
        if not self.footer_complete():
            raise InvalidRange('footer is not complete')
        return bytes(self.footer_bytes)

    def discard_footer_bytes(self) -> None:
        if self.footer_length is not None:
            self.footer_bytes = bytearray(self.footer_length)
        self.ranges = []

    def set_simple_report(self, report: str) -> None:
        self.simple_report = report
        self.simple_identity = self.identity

    def set_detail_report(self, report: str) -> None:
        self.detail_report = report
        self.detail_identity = self.identity

    def to_dict(self) -> dict:
        ranges = []
        for r in self.ranges:
            offset = self._range_offset(r.start)
            ranges.append({
                **r.to_dict(),
                'data': base64.b64encode(
                    bytes(self.footer_bytes[offset:offset + (r.end - r.start)])
                ).decode('ascii'),
            })
        return {
            'credential_version': self.credential_version,
            'endpoint': self.endpoint.to_dict(),
            'bucket': self.bucket,
            'key': self.key,
            'identity': self.identity.to_dict() if self.identity else None,
            'footer_length': self.footer_length,
            'ranges': ranges,
            'simple_report': self.simple_report,
            'simple_identity': self.simple_identity.to_dict() if self.simple_identity else None,
            'detail_report': self.detail_report,
            'detail_identity': self.detail_identity.to_dict() if self.detail_identity else None,
            'unavailable_reason': self.unavailable_reason,
        }

    @classmethod
    def from_dict(cls, value: dict) -> 'InspectionCredential':
        if int(value.get('credential_version', 0)) != CREDENTIAL_VERSION:
            raise CredentialError(
                f'unsupported credential version {value.get("credential_version")}'
            )
        credential = cls(
            endpoint=EndpointRef.from_dict(value.get('endpoint') or {}),
            bucket=value['bucket'],
            key=value['key'],
            identity=ObjectIdentity.from_dict(value['identity']) if value.get('identity') else None,
            footer_length=value.get('footer_length'),
            simple_report=value.get('simple_report'),
            simple_identity=ObjectIdentity.from_dict(value['simple_identity']) if value.get('simple_identity') else None,
            detail_report=value.get('detail_report'),
            detail_identity=ObjectIdentity.from_dict(value['detail_identity']) if value.get('detail_identity') else None,
            unavailable_reason=value.get('unavailable_reason'),
        )
        if credential.footer_length is not None:
            credential.footer_bytes = bytearray(credential.footer_length)
        for raw in value.get('ranges') or []:
            r = FooterRange.from_dict(raw)
            payload = base64.b64decode(raw.get('data') or '')
            if len(payload) != r.end - r.start:
                raise CredentialError('credential range payload length does not match its interval')
            credential.ranges.append(r)
            offset = credential._range_offset(r.start)
            credential.footer_bytes[offset:offset + len(payload)] = payload
        credential.ranges.sort(key=lambda x: x.start)
        return credential

    def save(self, path: str) -> None:
        directory = os.path.dirname(os.path.abspath(path)) or '.'
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(prefix='.inspect-credential-', dir=directory)
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(self.to_dict(), f, sort_keys=True, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    @classmethod
    def load(cls, path: str) -> 'InspectionCredential':
        with open(path, 'r') as f:
            return cls.from_dict(json.load(f))
