"""Tail-only, resumable parquet inspection.

The inspector reads a parquet object in place:

1. HEAD the object to learn its size, ETag and (when versioned) VersionId.
2. Range-read the 8 byte trailer to learn the footer length.
3. Range-read a prefix of the footer, growing only as far as needed for the
   simple report (through ``created_by``).
4. Save a credential describing the object version and verified footer
   ranges.  Later runs reuse those ranges and read only the still-missing
   footer intervals.

Nothing here reads data pages or downloads the whole object.
"""

import copy
import re
from dataclasses import dataclass, field
from typing import Callable, List, Optional, Tuple

from .credential import (
    CredentialMismatch,
    InspectionCredential,
    ObjectIdentity,
    RangeConflict,
    normalize_etag,
)
from .footer import (
    TRAILER_SIZE,
    FooterError,
    footer_length_from_trailer,
    scan_footer,
    verify_header,
)
from .remote import (
    RangeResponse,
    S3Store,
    StoreError,
    StoreIntegrity,
    StoreUnavailable,
)
from .report import render_detail, render_simple, unavailable_detail_text

DEFAULT_PREFIX_CHUNK = 64 * 1024
DEFAULT_RANGE_CHUNK = 1024 * 1024
DEFAULT_ATTEMPTS = 3


class InspectionError(Exception):
    pass


@dataclass
class RangeAttempt:
    start: int
    end: int
    status: Optional[int] = None
    content_range: Optional[str] = None
    body_length: Optional[int] = None
    expected_length: Optional[int] = None
    version_id: Optional[str] = None
    etag: Optional[str] = None
    accepted: bool = False
    partial: bool = False
    reason: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            'start': self.start,
            'end': self.end,
            'status': self.status,
            'content_range': self.content_range,
            'body_length': self.body_length,
            'expected_length': self.expected_length,
            'version_id': self.version_id,
            'etag': self.etag,
            'accepted': self.accepted,
            'partial': self.partial,
            'reason': self.reason,
        }


@dataclass
class InspectionResult:
    report: str
    detail_available: bool
    identity: Optional[ObjectIdentity]
    credential: InspectionCredential
    reason: Optional[str] = None
    attempts: List[RangeAttempt] = field(default_factory=list)


_CONTENT_RANGE = re.compile(r'^bytes\s+(\d+)-(\d+)/(\d+|\*)$')


def _parse_content_range(value: str) -> Optional[Tuple[int, int, Optional[int]]]:
    if not value:
        return None
    match = _CONTENT_RANGE.match(value.strip())
    if not match:
        return None
    start, end = int(match.group(1)), int(match.group(2))
    total = None if match.group(3) == '*' else int(match.group(3))
    return start, end, total


def _validate_response(
    response: RangeResponse,
    start: int,
    end: int,
    identity: ObjectIdentity,
    expected_version: Optional[str],
    attempt: RangeAttempt,
) -> bytes:
    """Validate response headers and return whichever prefix is trustworthy.

    A short body with a correct ``Content-Range`` is accepted as a verified
    prefix: the store promised bytes ``start-end`` and delivered a prefix of
    them.  Those bytes can be resumed from.  Any header that misplaces the
    body (wrong interval, wrong total, wrong version) is rejected entirely.
    """
    attempt.status = response.status
    attempt.content_range = response.content_range
    attempt.etag = response.etag
    attempt.version_id = response.version_id
    attempt.expected_length = end - start + 1
    attempt.body_length = len(response.body)

    if response.delete_marker:
        raise StoreUnavailable('object is a delete marker')
    if response.status == 200:
        raise StoreIntegrity(
            'service returned 200 (whole object) for a partial range; '
            'Range is not guaranteed'
        )
    if response.status != 206:
        raise StoreIntegrity(f'unexpected HTTP status {response.status} for a range read')

    if response.content_range is None:
        raise StoreIntegrity('206 response is missing Content-Range')
    parsed = _parse_content_range(response.content_range)
    if parsed is None:
        raise StoreIntegrity(f'unparseable Content-Range {response.content_range!r}')
    got_start, got_end, total = parsed
    if (got_start, got_end) != (start, end):
        raise StoreIntegrity(
            f'Content-Range {got_start}-{got_end} does not match requested {start}-{end}'
        )
    if total is not None and total != identity.size:
        raise StoreIntegrity(
            f'Content-Range total {total} does not match object size {identity.size}'
        )

    expected = end - start + 1
    if response.content_length is not None and int(response.content_length) > expected:
        raise StoreIntegrity(
            f'Content-Length {response.content_length} exceeds requested length'
        )
    if len(response.body) > expected:
        raise StoreIntegrity(
            f'body length {len(response.body)} exceeds requested {expected}'
        )

    if identity.etag and response.etag:
        if normalize_etag(identity.etag) != normalize_etag(response.etag):
            raise StoreIntegrity(
                f'response ETag {response.etag} is not the pinned version {identity.etag}'
            )
    if expected_version and response.version_id and response.version_id != expected_version:
        raise StoreIntegrity(
            f'response VersionId {response.version_id} is not the pinned {expected_version}'
        )

    if not response.body:
        raise StoreIntegrity('empty body for a non-empty range')

    attempt.accepted = True
    attempt.partial = len(response.body) < expected
    return response.body


@dataclass
class _Session:
    store: S3Store
    bucket: str
    key: str
    credential: InspectionCredential
    identity: ObjectIdentity
    is_resume: bool
    prefix_chunk_size: int
    range_chunk_size: int
    attempts: int
    attempts_log: List[RangeAttempt]

    def _range_args(self):
        """Version/conditional arguments for reading the original bytes.

        A fresh HEAD already gave us the version evidence for the first read,
        so the response ETag is still cross-checked against it.  On a resume
        there is no fresh HEAD behind the credential; if the service has no
        VersionId, an If-Match read is the only way to make the service prove
        the bytes are still the checked original.
        """
        identity = self.identity
        if identity.version_id is not None:
            return identity.version_id, False
        return None, self.is_resume

    def _request(self, start: int, end: int) -> Tuple[bytes, bool]:
        """Read one response, retrying integrity failures.

        Returns ``(verified_prefix, complete)``.  A short body is a verified
        prefix and ``complete`` is False.
        """
        version_id, use_if_match = self._range_args()
        last_error: Optional[Exception] = None
        for _ in range(max(1, self.attempts)):
            attempt = RangeAttempt(start=start, end=end)
            self.attempts_log.append(attempt)
            try:
                response = self.store.get_range(
                    self.bucket,
                    self.key,
                    start,
                    end,
                    version_id=version_id,
                    if_match=self.identity.etag if use_if_match else None,
                )
                body = _validate_response(
                    response, start, end, self.identity, version_id, attempt,
                )
                return body, len(body) == (end - start + 1)
            except StoreUnavailable as exc:
                attempt.reason = str(exc)
                raise
            except StoreIntegrity as exc:
                attempt.reason = str(exc)
                last_error = exc
                continue
            except StoreError as exc:
                attempt.reason = str(exc)
                last_error = exc
                continue
        raise StoreIntegrity(str(last_error) if last_error else 'range read failed')

    def gather(self, start: int, end: int, on_partial: Optional[Callable[[int, bytes], None]] = None) -> bytes:
        """Read exactly ``[start, end)``, resuming across short responses.

        ``on_partial`` receives every verified prefix; callers use it to keep
        resumable evidence even when a later segment fails.
        """
        chunks: List[bytes] = []
        cursor = start
        while cursor < end:
            body, complete = self._request(cursor, end - 1)
            if on_partial is not None and not complete:
                on_partial(cursor, body)
            chunks.append(body)
            cursor += len(body)
        return b''.join(chunks)

    def get_trailer(self) -> int:
        size = self.identity.size
        if size < TRAILER_SIZE + 4:
            raise InspectionError('object is too small to be a parquet file')
        body = self.gather(size - TRAILER_SIZE, size)
        verify_header(body[4:])
        return footer_length_from_trailer(body)

    def ensure_footer_length(self) -> int:
        if self.credential.footer_length is not None:
            return self.credential.footer_length
        footer_length = self.get_trailer()
        if footer_length > self.identity.size - TRAILER_SIZE - 4:
            raise InspectionError('footer length exceeds object size')
        self.credential.bind(self.identity, footer_length)
        return footer_length

    def read_prefix(self) -> None:
        """Read the shortest footer prefix needed for the simple report.

        The simple report must match a full inspect byte for byte, so the
        prefix extends through ``created_by`` when that optional field exists.
        If the writer omitted it, the scan has to reach the end of the footer
        to prove it is absent (or to find any later known field).
        """
        footer_length = self.credential.footer_length
        footer_start = self.credential.footer_start
        footer_end = self.credential.footer_end
        if footer_length is None or footer_start is None or footer_end is None:
            raise InspectionError('footer length is unknown')

        target_end = min(footer_end, footer_start + max(1, self.prefix_chunk_size))
        while True:
            read_start = footer_start + self.credential.known_prefix_length
            read_end = min(footer_end, max(target_end, read_start + 1))
            if read_start >= read_end:
                return
            staged = copy.deepcopy(self.credential)
            body = self.gather(
                read_start, read_end,
                on_partial=lambda start, data: self.credential.add_range(
                    start, data, identity=self.identity,
                ),
            )
            staged.add_range(read_start, body, identity=self.identity)
            layout = scan_footer(
                bytes(staged.footer_bytes[:staged.known_prefix_length]),
                footer_length,
            )
            known = staged.known_prefix_length
            if self._simple_prefix_ready(layout, known):
                self.credential.add_range(read_start, body, identity=self.identity)
                return
            if known >= footer_length:
                raise InspectionError('footer does not contain parquet file metadata')
            target_end = min(footer_end, known + footer_start + max(1, self.prefix_chunk_size))

    @staticmethod
    def _simple_prefix_ready(layout, known: int) -> bool:
        from .footer import FIELD_CREATED_BY

        if FIELD_CREATED_BY in layout.field_offsets:
            return layout.prefix_end <= known
        # Without created_by the simple report can only be final once the
        # whole struct has been scanned.
        return layout.complete and layout.prefix_end <= known

    def simple_report(self) -> str:
        self.ensure_footer_length()
        if not self.credential.footer_complete():
            self.read_prefix()
        self.credential.check_ranges()
        layout = scan_footer(
            bytes(self.credential.footer_bytes[:self.credential.known_prefix_length]),
            self.credential.footer_length,
        )
        if not layout.have_simple_fields:
            raise InspectionError('footer prefix does not contain parquet file metadata')
        report = render_simple(
            bytes(self.credential.footer_bytes[:layout.prefix_end]),
            self.credential.footer_length,
        )
        self.credential.set_simple_report(report)
        return report

    def detail_report(self) -> str:
        if self.credential.detail_report is not None \
                and self.credential.detail_identity is not None \
                and self.credential.detail_identity.same_version(self.identity):
            self.credential.check_ranges()
            if not self.credential.footer_complete():
                raise StoreIntegrity(
                    'cached detail report has no complete verified footer behind it'
                )
            return self.credential.detail_report

        if not self.credential.footer_complete():
            self.fill_missing_footer()
        self.credential.check_ranges()
        footer = bytes(self.credential.footer_bytes)
        layout = scan_footer(footer, self.credential.footer_length)
        if not layout.complete or not layout.is_complete_footer(self.credential.footer_length):
            raise StoreIntegrity(
                'assembled footer is not a complete parquet footer; '
                'the store did not return bytes for the requested range'
            )
        report = render_detail(footer)
        self.credential.set_detail_report(report)
        return report

    def _fill_with(self, source_credential: InspectionCredential, fetch) -> InspectionCredential:
        staged = copy.deepcopy(source_credential)
        for start, end in list(staged.missing_intervals()):
            cursor = start
            while cursor < end:
                chunk_end = min(end, cursor + max(1, self.range_chunk_size))
                body, complete = fetch(cursor, chunk_end - 1)
                staged.add_range(cursor, body, identity=self.identity)
                if not complete:
                    # A short read is trustworthy evidence of this prefix.
                    # Keep it in the live credential so a restart can resume
                    # instead of refetching those bytes.  Do not quietly
                    # continue the rest of the footer in the same attempt:
                    # the caller asked for a complete detail report, and a
                    # partial response means this store/attempt cannot give it.
                    self.credential.add_range(cursor, body, identity=self.identity)
                    raise StoreIntegrity(
                        f'short range response at [{cursor},{cursor + len(body)}) '
                        f'of requested [{cursor},{chunk_end}); kept the verified prefix '
                        'for the next attempt'
                    )
                cursor += len(body)
        return staged

    def fill_missing_footer(self) -> None:
        """Fill missing footer intervals, validating the whole footer first.

        Complete responses are staged and only committed after the assembled
        footer parses.  A wrong-length body placed at the right interval is
        therefore never recorded as a filled gap.  Verified *prefixes* from
        short reads are committed immediately so a restart can resume them.
        """
        if self.credential.footer_complete():
            return

        failures: List[str] = []
        staged: Optional[InspectionCredential] = None

        if self.identity.version_id is not None:
            try:
                staged = self._fill_with(
                    self.credential,
                    lambda start, end: self._request_for_version(
                        start, end, self.identity.version_id, False,
                    ),
                )
            except StoreUnavailable as exc:
                # Services may list historic versions but refuse to serve
                # their ranges.  A conditional read of the current object is
                # still safe: it succeeds only when the bytes are unchanged.
                failures.append(f'version {self.identity.version_id}: {exc}')

        if staged is None:
            if not self.identity.etag:
                raise StoreUnavailable('; '.join(failures + [
                    'no VersionId and no ETag; original bytes cannot be proven'
                ]))
            try:
                staged = self._fill_with(
                    self.credential,
                    lambda start, end: self._request_for_version(
                        start, end, None, True,
                    ),
                )
            except StoreUnavailable as exc:
                failures.append(f'If-Match {self.identity.etag}: {exc}')
                raise _lost_version_error(self.store, self.bucket, self.key, self.identity) from exc

        footer = bytes(staged.footer_bytes)
        layout = scan_footer(footer, staged.footer_length)
        if not layout.complete:
            raise StoreIntegrity(
                'assembled footer is not a complete parquet footer; '
                'the store did not return bytes for the requested range'
            )
        try:
            render_detail(footer)
        except Exception as exc:
            raise StoreIntegrity(
                f'assembled footer does not deserialize as parquet metadata: {exc}'
            )

        self.credential.footer_bytes = staged.footer_bytes
        self.credential.ranges = staged.ranges

    def _request_for_version(
        self, start: int, end: int, version_id: Optional[str], use_if_match: bool,
    ) -> Tuple[bytes, bool]:
        last_error: Optional[Exception] = None
        for _ in range(max(1, self.attempts)):
            attempt = RangeAttempt(start=start, end=end)
            self.attempts_log.append(attempt)
            try:
                response = self.store.get_range(
                    self.bucket,
                    self.key,
                    start,
                    end,
                    version_id=version_id,
                    if_match=self.identity.etag if use_if_match else None,
                )
                body = _validate_response(
                    response, start, end, self.identity, version_id, attempt,
                )
                return body, len(body) == (end - start + 1)
            except StoreUnavailable as exc:
                attempt.reason = str(exc)
                raise
            except (StoreIntegrity, StoreError) as exc:
                attempt.reason = str(exc)
                last_error = exc
                continue
        raise StoreIntegrity(str(last_error) if last_error else 'range read failed')


def _lost_version_error(store: S3Store, bucket: str, key: str, identity: ObjectIdentity) -> StoreUnavailable:
    """Explain why the checked original can no longer be read."""
    try:
        stat = store.head(bucket, key)
    except StoreError:
        current = None
    else:
        current = stat.identity()
    if current is None:
        return StoreUnavailable(
            'the checked original is no longer available at this endpoint'
        )
    if current.same_version(identity):
        return StoreUnavailable(
            'the service did not return verifiable bytes for the original version'
        )
    return StoreUnavailable(
        'the object was replaced and the service cannot address the original version; '
        f'current ETag {current.etag} size {current.size}'
    )


def _identify(
    store: S3Store,
    bucket: str,
    key: str,
    credential: Optional[InspectionCredential],
) -> Tuple[InspectionCredential, ObjectIdentity, bool]:
    is_resume = credential is not None
    endpoint = store.endpoint_ref
    if credential is None:
        credential = InspectionCredential(endpoint=endpoint, bucket=bucket, key=key)
    else:
        credential.check_target(endpoint, bucket, key)

    if credential.identity is not None:
        return credential, credential.identity, is_resume

    stat = store.head(bucket, key)
    return credential, stat.identity(), is_resume


def inspect_tail(
    store: S3Store,
    bucket: str,
    key: str,
    detail: bool = False,
    credential: Optional[InspectionCredential] = None,
    prefix_chunk_size: int = DEFAULT_PREFIX_CHUNK,
    range_chunk_size: int = DEFAULT_RANGE_CHUNK,
    attempts: int = DEFAULT_ATTEMPTS,
) -> InspectionResult:
    """Inspect an S3 object without downloading data pages.

    Pass a credential between calls to resume a previous check.  The same
    credential is updated in place and returned in the result.
    """
    attempts_log: List[RangeAttempt] = []
    try:
        credential, identity, is_resume = _identify(store, bucket, key, credential)
    except (StoreUnavailable, StoreError) as exc:
        raise InspectionError(str(exc)) from exc

    session = _Session(
        store=store,
        bucket=bucket,
        key=key,
        credential=credential,
        identity=identity,
        is_resume=is_resume,
        prefix_chunk_size=prefix_chunk_size,
        range_chunk_size=range_chunk_size,
        attempts=attempts,
        attempts_log=attempts_log,
    )

    # The simple phase also establishes the version binding used by the
    # detailed phase.  An existing simple report belongs to that binding and is
    # reused unchanged.
    if credential.simple_report is None:
        try:
            session.simple_report()
        except (StoreError, FooterError, ValueError, RangeConflict) as exc:
            raise InspectionError(str(exc)) from exc
    else:
        try:
            credential.check_ranges()
        except RangeConflict as exc:
            raise InspectionError(str(exc)) from exc

    if not detail:
        return InspectionResult(
            report=credential.simple_report or '',
            detail_available=False,
            identity=credential.identity,
            credential=credential,
            attempts=attempts_log,
        )

    reason = None
    try:
        report = session.detail_report()
    except (StoreUnavailable, StoreIntegrity, StoreError, FooterError,
            RangeConflict, CredentialMismatch, ValueError) as exc:
        reason = str(exc)
    else:
        return InspectionResult(
            report=report,
            detail_available=True,
            identity=credential.identity,
            credential=credential,
            attempts=attempts_log,
        )

    credential.unavailable_reason = reason
    attribution = {
        'size': credential.identity.size if credential.identity else None,
        'etag': credential.identity.etag if credential.identity else None,
        'version_id': credential.identity.version_id if credential.identity else None,
        'footer_length': credential.footer_length,
    }
    report = (credential.simple_report or '') + '\n' + unavailable_detail_text(reason, attribution)
    return InspectionResult(
        report=report,
        detail_available=False,
        identity=credential.identity,
        credential=credential,
        reason=reason,
        attempts=attempts_log,
    )
