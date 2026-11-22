"""Representation-aware range catalog and playback service.

This module implements the archive side of multi-fragment media replay:

* A catalog import builds one or more :class:`MediaRepresentation` objects.
  Import verification checks declared total lengths, the actual length of
  every fragment source, overlapping bytes and, when the fragments cover the
  whole representation, the content identity.
* A representation is served as a normal HTTP byte range.  A request that
  crosses fragment boundaries is streamed fragment by fragment, without
  downloading the entire requested range before the first byte is produced.
* Every response receives an opaque pin.  A client can continue the same
  representation through the pinned URL even after a newer catalog is
  published.  A new catalog may move fragment addresses, but it is only used
  for an already pinned response when its verified content identity is the
  same.

The module is intentionally independent of pywb's live replay path so normal
single-record replay is unaffected.
"""

from collections import OrderedDict
from dataclasses import dataclass, field
from hashlib import sha256
from typing import Dict, Optional, Tuple
from urllib.parse import unquote
import json
import os
import re
import threading
import time
import uuid


__all__ = [
    'Source',
    'MediaSegment',
    'MediaRepresentation',
    'RangeCatalog',
    'RangeReader',
    'RangeCatalogError',
    'CatalogVerificationError',
    'RepresentationNotFound',
    'RangeNotSatisfiable',
    'MediaResponse',
    'PinnedRepresentation',
    'ArchiveService',
    'MediaArchiveApp',
    'build_catalog',
    'build_representation',
    'load_catalog',
    'index_warc_records',
    'parse_byte_range',
    'format_content_range',
    'etag_is_strong',
    'if_range_matches',
]


# ============================================================================
class RangeCatalogError(Exception):
    """Base class for range catalog failures."""


class CatalogVerificationError(RangeCatalogError):
    """A catalog entry failed import-time verification."""


class RepresentationNotFound(RangeCatalogError):
    """The requested representation is not in the selected catalog."""

    status = 404
    reason = 'Not Found'


class RangeNotSatisfiable(RangeCatalogError):
    """The requested bytes cannot be proven to belong to one representation."""

    status = 416
    reason = 'Range Not Satisfiable'

    def __init__(self, message='', total_length=None):
        super(RangeNotSatisfiable, self).__init__(message)
        self.total_length = total_length


# ============================================================================
@dataclass(frozen=True)
class Source:
    """A location from which one fragment can be read.

    ``data`` is useful for tests and small in-memory sources.  ``path`` with
    ``payload_offset`` identifies uncompressed payload bytes in a file.  A
    WARC record can be addressed with ``record_offset`` plus an optional
    ``record_length``.

    ``fingerprint`` is assigned by the importer and records the file
    identity (device, inode, size and modification time).  Playback treats a
    source with a changed fingerprint as a different object unless its full
    digest can be verified.
    """

    path: Optional[str] = None
    record_offset: int = 0
    record_length: Optional[int] = None
    payload_offset: Optional[int] = None
    data: Optional[bytes] = None
    label: str = ''
    etag: str = ''
    fingerprint: str = ''

    def __post_init__(self):
        if self.data is None and not self.path:
            raise ValueError('Source requires either data or path')

        if isinstance(self.data, (bytearray, memoryview)):
            object.__setattr__(self, 'data', bytes(self.data))

    @property
    def identity(self):
        if self.data is not None:
            return 'data:' + sha256(self.data).hexdigest()

        if self.payload_offset is not None:
            return 'payload:{0}:{1}'.format(self.path, self.payload_offset)

        return 'record:{0}:{1}:{2}'.format(
            self.path, self.record_offset, self.record_length)

    def with_fingerprint(self):
        if self.data is not None or self.fingerprint:
            return self

        try:
            st = os.stat(self.path)
        except OSError:
            return self

        fingerprint = 'stat:{0}:{1}:{2}:{3}'.format(
            st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
        return Source(path=self.path,
                      record_offset=self.record_offset,
                      record_length=self.record_length,
                      payload_offset=self.payload_offset,
                      data=self.data,
                      label=self.label,
                      etag=self.etag,
                      fingerprint=fingerprint)

    def current_fingerprint(self):
        if self.data is not None:
            return 'data'
        try:
            st = os.stat(self.path)
        except OSError:
            return ''
        return 'stat:{0}:{1}:{2}:{3}'.format(
            st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

    def unchanged(self):
        if self.data is not None:
            return True
        if not self.fingerprint:
            return True
        return self.fingerprint == self.current_fingerprint()

    def __repr__(self):
        if self.data is not None:
            return "Source(data={0} bytes)".format(len(self.data))
        return "Source(path={0!r}, offset={1}, length={2})".format(
            self.path, self.record_offset, self.record_length)


# ============================================================================
@dataclass(frozen=True)
class MediaSegment:
    """One fragment of a representation.

    ``start`` and ``end`` are inclusive offsets in the complete
    representation.  ``sources`` is ordered: the first source is the default
    one and any remaining entries are alternates.  ``digest`` is the SHA-256
    hex digest of the complete fragment contents.
    """

    start: int
    end: int
    sources: Tuple[Source, ...]
    digest: Optional[str] = None

    def __post_init__(self):
        if self.start < 0 or self.end < self.start:
            raise CatalogVerificationError(
                'Invalid fragment coverage {0}-{1}'.format(self.start, self.end))
        if not self.sources:
            raise CatalogVerificationError(
                'Fragment {0}-{1} has no sources'.format(self.start, self.end))

        object.__setattr__(self, 'sources', tuple(self.sources))

    @property
    def length(self):
        return self.end - self.start + 1

    def contains(self, offset):
        return self.start <= offset <= self.end

    def intersection(self, start, end):
        """Return ``(local_start, local_end)`` for an overlap, or ``None``."""
        lo = max(start, self.start)
        hi = min(end, self.end)
        if lo > hi:
            return None
        return (lo - self.start, hi - self.start)


# ============================================================================
@dataclass(frozen=True)
class MediaRepresentation:
    """A verified, pin-able representation assembled from fragments."""

    rep_id: str
    total_length: int
    segments: Tuple[MediaSegment, ...] = ()
    etag: str = ''
    url: str = ''
    mime: str = 'application/octet-stream'
    content_id: Optional[str] = None
    generation: int = 0
    verified: bool = False

    def __post_init__(self):
        if self.total_length < 0:
            raise CatalogVerificationError('Negative total length')

        object.__setattr__(self, 'segments',
                           tuple(sorted(self.segments,
                                        key=lambda s: (s.start, s.end))))

    @property
    def coverage_ranges(self):
        """Return merged inclusive coverage intervals."""
        intervals = []
        for segment in self.segments:
            if not intervals or segment.start > intervals[-1][1] + 1:
                intervals.append([segment.start, segment.end])
            elif segment.end > intervals[-1][1]:
                intervals[-1][1] = segment.end

        return tuple((start, end) for start, end in intervals)

    @property
    def coverage_start(self):
        ranges = self.coverage_ranges
        return ranges[0][0] if ranges else None

    @property
    def coverage_end(self):
        ranges = self.coverage_ranges
        return ranges[-1][1] if ranges else None

    @property
    def coverage_length(self):
        return sum(end - start + 1 for start, end in self.coverage_ranges)

    @property
    def complete(self):
        return (self.total_length > 0 and
                self.coverage_ranges == ((0, self.total_length - 1),))

    @property
    def coverage_header(self):
        """A compact value suitable for ``X-Archive-Coverage``."""
        if not self.segments:
            return 'bytes */{0}'.format(self.total_length)

        parts = ','.join('{0}-{1}'.format(start, end)
                         for start, end in self.coverage_ranges)
        return 'bytes {0}/{1}'.format(parts, self.total_length)

    @property
    def identity_key(self):
        segment_ids = ','.join(
            '{0}-{1}:{2}'.format(s.start, s.end, s.digest or '')
            for s in self.segments)
        return '{0}:{1}:{2}:{3}'.format(self.total_length,
                                        self.coverage_header,
                                        self.content_id or '',
                                        segment_ids)

    def locate(self, start, end):
        """Map a representation range to segment slices.

        Returns a list of ``(segment, local_start, local_end)`` or ``None``
        when any part of the requested range is not covered.  Overlapping
        fragments are resolved deterministically in catalog order.
        """
        if start < 0 or end < start or end >= self.total_length:
            return None

        result = []
        cursor = start
        for segment in self.segments:
            if segment.end < cursor:
                continue

            if segment.start > cursor:
                # A gap before the next fragment.
                return None

            stop = min(end, segment.end)
            local_start = cursor - segment.start
            local_end = stop - segment.start
            result.append((segment, local_start, local_end))
            cursor = stop + 1
            if cursor > end:
                break

        return result if cursor > end else None

    def find_equivalent_segment(self, segment):
        for candidate in self.segments:
            if (candidate.start == segment.start and
                    candidate.end == segment.end):
                return candidate
        return None

    def info(self):
        return {
            'representation': self.rep_id,
            'url': self.url,
            'etag': self.etag,
            'total_length': self.total_length,
            'coverage': self.coverage_header,
            'coverage_ranges': [list(item) for item in self.coverage_ranges],
            'complete': self.complete,
            'content_id': self.content_id,
            'generation': self.generation,
            'mime': self.mime,
            'segments': [
                {
                    'start': segment.start,
                    'end': segment.end,
                    'length': segment.length,
                    'digest': segment.digest,
                    'sources': [source.identity for source in segment.sources],
                }
                for segment in self.segments
            ],
        }


# ============================================================================
class RangeCatalog:
    """An immutable-by-convention snapshot of one catalog generation."""

    def __init__(self, generation=1, representations=None, metadata=None):
        self.generation = int(generation)
        self.metadata = dict(metadata or {})
        self._reps = OrderedDict()

        for rep in representations or ():
            self.add(rep)

    def add(self, rep):
        if rep.rep_id in self._reps:
            existing = self._reps[rep.rep_id]
            if existing.identity_key != rep.identity_key:
                raise CatalogVerificationError(
                    'Representation id collision for {0}'.format(rep.rep_id))
            return existing

        self._reps[rep.rep_id] = rep
        return rep

    def get(self, rep_id):
        return self._reps.get(rep_id)

    def require(self, rep_id):
        rep = self.get(rep_id)
        if not rep:
            raise RepresentationNotFound(rep_id)
        return rep

    @property
    def representations(self):
        return tuple(self._reps.values())

    def find(self, url=None, etag=None, content_id=None):
        matches = []
        for rep in self._reps.values():
            if url is not None and rep.url != url:
                continue
            if etag is not None and rep.etag != etag:
                continue
            if content_id is not None and rep.content_id != content_id:
                continue
            matches.append(rep)

        if not matches:
            return None

        # Prefer complete representations, then the most recent generation.
        matches.sort(key=lambda rep: (bool(rep.complete), rep.generation),
                     reverse=True)
        return matches[0]

    def find_equivalent(self, rep):
        """Find a rep with the same verified identity, even if ids moved."""
        exact = self.get(rep.rep_id)
        if exact and exact.identity_key == rep.identity_key:
            return exact

        for candidate in self._reps.values():
            if candidate.identity_key == rep.identity_key:
                return candidate

        # A complete representation whose content hash matches is also the
        # same representation, even when fragment boundaries moved.
        if rep.content_id:
            for candidate in self._reps.values():
                if candidate.content_id == rep.content_id:
                    return candidate

        return None

    def info(self):
        return {
            'generation': self.generation,
            'metadata': self.metadata,
            'representations': [rep.info() for rep in self._reps.values()],
        }

    def __len__(self):
        return len(self._reps)

    def __iter__(self):
        return iter(self._reps.values())


# ============================================================================
class RangeReader:
    """Read fragment bytes from a :class:`Source`."""

    def __init__(self, chunk_size=65536):
        self.chunk_size = chunk_size

    def available(self, source):
        if source.data is not None:
            return True
        return bool(source.path and os.path.exists(source.path))

    def read(self, source, start=0, length=None):
        if source.data is not None:
            end = None if length is None else start + length
            return source.data[start:end]

        if not source.path:
            raise IOError('Source has no path')

        if source.payload_offset is not None:
            with open(source.path, 'rb') as fh:
                fh.seek(source.payload_offset + start)
                return fh.read() if length is None else fh.read(length)

        if source.record_length is not None or source.record_offset:
            return self._read_warc(source, start, length)

        with open(source.path, 'rb') as fh:
            fh.seek(start)
            return fh.read() if length is None else fh.read(length)

    def iter_bytes(self, source, start=0, length=None):
        """Yield a requested slice using bounded reads."""
        if source.data is not None:
            data = self.read(source, start, length)
            for offset in range(0, len(data), self.chunk_size):
                yield data[offset:offset + self.chunk_size]
            return

        remaining = length
        offset = start
        while remaining is None or remaining > 0:
            amount = self.chunk_size
            if remaining is not None:
                amount = min(amount, remaining)
            chunk = self.read(source, offset, amount)
            if not chunk:
                break
            yield chunk
            offset += len(chunk)
            if remaining is not None:
                remaining -= len(chunk)

    def _read_warc(self, source, start, length):
        from pywb.utils.io import no_except_close
        from pywb.warcserver.resource.blockrecordloader import (
            BlockArcWarcRecordLoader)

        record_length = source.record_length
        if record_length is None:
            record_length = -1

        record = BlockArcWarcRecordLoader().load(source.path,
                                                 source.record_offset,
                                                 record_length)
        stream = record.raw_stream
        try:
            remaining = start
            while remaining > 0:
                chunk = stream.read(min(remaining, self.chunk_size))
                if not chunk:
                    return b''
                remaining -= len(chunk)

            if length is None:
                chunks = []
                while True:
                    chunk = stream.read(self.chunk_size)
                    if not chunk:
                        break
                    chunks.append(chunk)
                return b''.join(chunks)

            chunks = []
            remaining = length
            while remaining > 0:
                chunk = stream.read(min(remaining, self.chunk_size))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            return b''.join(chunks)
        finally:
            no_except_close(stream)


# ============================================================================
_RANGE_RE = re.compile(r'^\s*bytes\s*=\s*(?P<spec>[^,]+)\s*$', re.I)
_RANGE_SPEC_RE = re.compile(r'^\s*(?P<start>\d*)\s*-\s*(?P<end>\d*)\s*$')


def parse_byte_range(value, total_length):
    """Parse a single byte-range request.

    Returns ``(start, end)`` with inclusive ``end`` or ``None`` when the
    header is absent.  Raises :class:`RangeNotSatisfiable` for malformed or
    unsatisfiable ranges.  ``end=0`` is a real one-byte range, never a
    sentinel for "to the end".
    """
    if not value:
        return None

    match = _RANGE_RE.match(value)
    if not match:
        raise RangeNotSatisfiable('Unsupported Range header', total_length)

    spec = _RANGE_SPEC_RE.match(match.group('spec'))
    if not spec:
        raise RangeNotSatisfiable('Malformed Range header', total_length)

    start_text = spec.group('start')
    end_text = spec.group('end')

    if not start_text and not end_text:
        raise RangeNotSatisfiable('Malformed Range header', total_length)

    if not start_text:
        # Suffix range: bytes=-N
        suffix = int(end_text)
        if suffix <= 0:
            raise RangeNotSatisfiable('Empty suffix range', total_length)
        start = max(total_length - suffix, 0)
        end = total_length - 1
    else:
        start = int(start_text)
        # Explicitly distinguish "no end" from end == 0.
        end = int(end_text) if end_text != '' else None

    if total_length <= 0:
        raise RangeNotSatisfiable('Empty representation', total_length)

    if start >= total_length:
        raise RangeNotSatisfiable('Range starts past end', total_length)

    if end is None:
        end = total_length - 1

    if end < start or end >= total_length:
        raise RangeNotSatisfiable('Range is outside the representation',
                                  total_length)

    return (start, end)


def format_content_range(start, end, total_length):
    return 'bytes {0}-{1}/{2}'.format(start, end, total_length)


def etag_is_strong(value):
    if not value:
        return False
    value = value.strip()
    return value.startswith('"') and not value.startswith('W/')


def if_range_matches(value, etag):
    """Return True only for a strong, byte-for-byte ETag match."""
    if not value or not etag:
        return False

    value = value.strip()
    etag = etag.strip()

    # Weak validators never satisfy If-Range.
    if not etag_is_strong(value) or not etag_is_strong(etag):
        return False

    return value == etag


# ============================================================================
def _parse_coverage(value, segment):
    if isinstance(value, str):
        text = value.strip()
        text = re.sub(r'^bytes\s*=?\s*', '', text, flags=re.I)
        text = text.split('/', 1)[0]
        parts = text.split('-', 1)
        if len(parts) != 2:
            raise CatalogVerificationError('Invalid fragment range: ' + value)
        return int(parts[0]), int(parts[1])

    if isinstance(value, (list, tuple)) and len(value) == 2:
        return int(value[0]), int(value[1])

    start = segment.get('start')
    end = segment.get('end')
    if start is None or end is None:
        raise CatalogVerificationError('Fragment requires start and end')
    return int(start), int(end)


def _normalize_source(value):
    if isinstance(value, Source):
        return value
    if isinstance(value, (bytes, bytearray, memoryview)):
        return Source(data=bytes(value))
    if isinstance(value, str):
        return Source(path=value)
    if not isinstance(value, dict):
        raise CatalogVerificationError(
            'Invalid fragment source: {0!r}'.format(value))

    if value.get('data') is not None:
        return Source(data=value['data'],
                      label=value.get('label', ''),
                      etag=value.get('etag', ''))

    path = (value.get('path') or value.get('filename') or
            value.get('file') or value.get('url'))
    if not path:
        raise CatalogVerificationError('Fragment source has no path')

    record_offset = value.get('record_offset', value.get('offset', 0))
    record_length = value.get('record_length', value.get('length'))
    payload_offset = value.get('payload_offset')

    return Source(path=path,
                  record_offset=int(record_offset or 0),
                  record_length=(int(record_length)
                                 if record_length is not None else None),
                  payload_offset=(int(payload_offset)
                                  if payload_offset is not None else None),
                  label=value.get('label', ''),
                  etag=value.get('etag', ''))


def _normalize_segment(segment):
    if isinstance(segment, MediaSegment):
        return segment
    if not isinstance(segment, dict):
        raise CatalogVerificationError(
            'Invalid fragment: {0!r}'.format(segment))

    start, end = _parse_coverage(
        segment.get('coverage', segment.get('range', segment)), segment)

    declared_length = segment.get('length')
    if declared_length is not None and int(declared_length) != end - start + 1:
        raise CatalogVerificationError(
            'Fragment {0}-{1} declares length {2}'.format(
                start, end, declared_length))

    raw_sources = (segment.get('sources') or segment.get('locations') or
                   segment.get('alternatives') or segment.get('paths') or
                   segment.get('source'))
    if raw_sources is None and (segment.get('path') or
                                segment.get('filename')):
        source_spec = dict(segment)
        raw_sources = [source_spec]
    if raw_sources is None:
        raw_sources = []

    if isinstance(raw_sources, (str, bytes, dict, Source)):
        raw_sources = [raw_sources]

    sources = tuple(_normalize_source(source) for source in raw_sources)
    if not sources:
        raise CatalogVerificationError(
            'Fragment {0}-{1} has no sources'.format(start, end))

    return MediaSegment(start=start, end=end, sources=sources,
                        digest=segment.get('digest'))


def _make_rep_id(url, etag, content_id, fallback):
    material = '\x00'.join([url or '', etag or '',
                            content_id or fallback or ''])
    return 'rep-' + sha256(material.encode('utf-8')).hexdigest()[:20]


def _read_all(reader, source):
    data = reader.read(source, 0, None)
    if data is None:
        data = b''
    return bytes(data)


def _assemble_complete(rep, segment_data):
    """Assemble a complete representation, respecting fragment order."""
    chunks = []
    cursor = 0
    for segment in rep.segments:
        if segment.end < cursor:
            continue
        if segment.start > cursor:
            raise CatalogVerificationError(
                'Coverage gap before fragment {0}-{1}'.format(
                    segment.start, segment.end))
        data = segment_data[segment]
        start = cursor - segment.start
        chunks.append(data[start:])
        cursor = segment.end + 1

    if cursor != rep.total_length:
        raise CatalogVerificationError(
            'Coverage ends at {0}, expected {1}'.format(
                cursor - 1, rep.total_length - 1))

    return b''.join(chunks)


# ============================================================================
def build_representation(spec, generation=1, reader=None, verify=True,
                         strict=True):
    """Build one verified representation from a catalog specification.

    A fragment can be given as ``{'start': 0, 'end': 9, 'sources': [...]}``
    or with a ``coverage`` string such as ``'bytes 0-9/20'``.  A source can
    be a path string, an in-memory ``{'data': b'...'}`` or a WARC locator
    ``{'filename': ..., 'offset': ..., 'length': ...}``.
    """
    reader = reader or RangeReader()

    url = spec.get('url') or spec.get('target_uri') or ''
    etag = spec.get('etag') or ''
    mime = (spec.get('mime') or spec.get('content_type') or
            'application/octet-stream')

    raw_segments = spec.get('segments') or spec.get('fragments') or []
    if not raw_segments:
        raise CatalogVerificationError('Representation has no fragments')

    segments = [_normalize_segment(segment) for segment in raw_segments]
    segments.sort(key=lambda segment: (segment.start, segment.end))

    max_end = max(segment.end for segment in segments)
    total_length = spec.get('total_length', spec.get('length'))
    if total_length is None:
        total_length = max_end + 1
    total_length = int(total_length)

    if total_length < max_end + 1:
        raise CatalogVerificationError(
            'Total length {0} is shorter than fragment end {1}'.format(
                total_length, max_end))

    # Read every fragment once at import time.  This is the place where
    # complete length, alternate-source equality, overlap bytes and the
    # representation identity can be checked; playback requests do not need
    # to repeat the full check.
    data_by_index = []
    digest_by_index = []

    for segment in segments:
        expected = segment.length
        primary = None
        primary_digest = None

        for raw_source in segment.sources:
            source = raw_source.with_fingerprint()
            try:
                data = _read_all(reader, source)
            except Exception as exc:
                if strict:
                    raise CatalogVerificationError(
                        'Could not read fragment {0}-{1} from {2}: {3}'.format(
                            segment.start, segment.end, source, exc))
                continue

            if len(data) != expected:
                if strict:
                    raise CatalogVerificationError(
                        'Fragment {0}-{1} declares {2} bytes but source '
                        'contains {3}'.format(segment.start, segment.end,
                                             expected, len(data)))
                continue

            source_digest = sha256(data).hexdigest()
            if primary is None:
                primary = data
                primary_digest = source_digest
            elif source_digest != primary_digest and strict:
                raise CatalogVerificationError(
                    'Alternate source for fragment {0}-{1} does not match '
                    'the default source'.format(segment.start, segment.end))

        if primary is None:
            raise CatalogVerificationError(
                'Fragment {0}-{1} has no readable source'.format(
                    segment.start, segment.end))

        if segment.digest and segment.digest != primary_digest and strict:
            raise CatalogVerificationError(
                'Fragment {0}-{1} digest mismatch'.format(
                    segment.start, segment.end))

        data_by_index.append(primary)
        digest_by_index.append(segment.digest or primary_digest)

    # Freeze the verified digests and fingerprints into the catalog.
    verified_segments = []
    verified_data = []
    for index, segment in enumerate(segments):
        verified_source = tuple(source.with_fingerprint()
                                for source in segment.sources)
        verified_segments.append(MediaSegment(
            start=segment.start,
            end=segment.end,
            sources=verified_source,
            digest=digest_by_index[index]))
        verified_data.append(data_by_index[index])

    # Verify every intersecting pair.  Comparing all pairs is cheap for the
    # small fragment counts that catalogs contain and catches non-adjacent
    # overlaps as well.
    for index, segment in enumerate(verified_segments):
        segment_bytes = verified_data[index]
        for other_index in range(index + 1, len(verified_segments)):
            other = verified_segments[other_index]
            if other.start > segment.end:
                break

            overlap = segment.intersection(other.start, other.end)
            if overlap is None:
                continue

            local_start, local_end = overlap
            length = local_end - local_start + 1
            left = segment_bytes[local_start:local_start + length]
            right = verified_data[other_index][:length]
            if left != right:
                raise CatalogVerificationError(
                    'Overlapping bytes differ between fragments {0}-{1} and '
                    '{2}-{3}'.format(segment.start, segment.end,
                                     other.start, other.end))

    provisional = MediaRepresentation(
        rep_id=spec.get('representation_id') or spec.get('rep_id') or
               spec.get('id') or '',
        total_length=total_length,
        segments=tuple(verified_segments),
        etag=etag,
        url=url,
        mime=mime,
        generation=int(spec.get('generation', generation) or generation),
        verified=bool(verify))

    declared_content_id = spec.get('content_id')
    content_id = declared_content_id

    if verify and provisional.complete:
        computed_content_id = sha256(_assemble_complete(
            provisional,
            dict(zip(provisional.segments, verified_data)))).hexdigest()
        if (declared_content_id and declared_content_id != computed_content_id
                and strict):
            raise CatalogVerificationError(
                'Representation content id does not match its bytes')
        content_id = computed_content_id

    rep_id = (spec.get('representation_id') or spec.get('rep_id') or
              spec.get('id'))
    if not rep_id:
        rep_id = _make_rep_id(url, etag, content_id, provisional.identity_key)

    return MediaRepresentation(
        rep_id=rep_id,
        total_length=total_length,
        segments=tuple(verified_segments),
        etag=etag,
        url=url,
        mime=mime,
        content_id=content_id,
        generation=int(spec.get('generation', generation) or generation),
        verified=bool(verify))


def load_catalog(source, generation=None, reader=None, verify=True,
                 strict=True, base_dir=''):
    """Build a catalog from a mapping, a list or a JSON manifest file."""
    if hasattr(source, 'read'):
        spec = json.load(source)
    elif isinstance(source, (str, bytes, os.PathLike)):
        with open(source, 'r', encoding='utf-8') as fh:
            spec = json.load(fh)
        if not base_dir:
            base_dir = os.path.dirname(os.path.abspath(str(source)))
    else:
        spec = source

    if base_dir:
        spec = _rebase_spec(spec, base_dir)

    return build_catalog(spec, generation=generation, reader=reader,
                         verify=verify, strict=strict)


def _rebase_spec(spec, base_dir):
    """Resolve relative source paths against the manifest directory."""
    if isinstance(spec, list):
        return [_rebase_spec(item, base_dir) for item in spec]

    if not isinstance(spec, dict):
        return spec

    result = dict(spec)
    for key in ('representations', 'reps', 'captures', 'segments',
                'fragments', 'sources', 'locations', 'alternatives',
                'paths'):
        value = result.get(key)
        if isinstance(value, list):
            result[key] = [_rebase_spec(item, base_dir) for item in value]
        elif isinstance(value, dict):
            result[key] = _rebase_spec(value, base_dir)

    for key in ('path', 'filename'):
        value = result.get(key)
        if (isinstance(value, str) and value and
                '://' not in value and not os.path.isabs(value)):
            result[key] = os.path.join(base_dir, value)

    return result


def index_warc_records(path):
    """Return record locations in a local WARC file.

    The result is ordered by file position and contains the fields needed to
    build catalog sources: ``offset``, ``length``, ``type``, ``url``,
    ``mime`` and ``payload_offset``.
    """
    from warcio.archiveiterator import ArchiveIterator

    records = []
    with open(path, 'rb') as fh:
        iterator = ArchiveIterator(fh)
        for record in iterator:
            record_offset = iterator.offset
            http_headers = record.http_headers
            payload_length = record.payload_length
            iterator.read_to_end(record)
            if iterator.member_info:
                offset, length = iterator.member_info
            else:
                offset, length = record_offset, 0

            payload_offset = None
            if payload_length and payload_length > 0:
                payload_offset = offset + length - payload_length

            records.append({
                'offset': offset,
                'length': length,
                'type': record.rec_type,
                'url': record.rec_headers.get_header('WARC-Target-URI'),
                'mime': (http_headers.get_header('Content-Type')
                         if http_headers else
                         record.rec_headers.get_header('Content-Type')),
                'status': (http_headers.get_statuscode()
                           if http_headers else None),
                'etag': (http_headers.get_header('ETag')
                         if http_headers else None),
                'content_range': (http_headers.get_header('Content-Range')
                                  if http_headers else None),
                'payload_length': payload_length,
                'payload_offset': payload_offset,
            })

    return records


def build_catalog(spec, generation=None, reader=None, verify=True,
                  strict=True):
    """Build a :class:`RangeCatalog` from one or more representation specs."""
    reader = reader or RangeReader()

    if isinstance(spec, RangeCatalog):
        return spec

    if isinstance(spec, list):
        reps = spec
        catalog_meta = {}
    elif isinstance(spec, dict) and (
            'representations' in spec or 'reps' in spec or
            'captures' in spec):
        reps = (spec.get('representations') or spec.get('reps') or
                spec.get('captures') or [])
        catalog_meta = spec.get('metadata') or {}
    elif isinstance(spec, dict) and 'segments' in spec:
        reps = [spec]
        catalog_meta = {}
    else:
        raise CatalogVerificationError('Unsupported catalog specification')

    if generation is not None:
        catalog_generation = int(generation)
    elif isinstance(spec, dict) and spec.get('generation') is not None:
        catalog_generation = int(spec['generation'])
    else:
        catalog_generation = 1

    representations = []
    for rep_spec in reps:
        representations.append(build_representation(
            rep_spec,
            generation=rep_spec.get('generation', catalog_generation),
            reader=reader,
            verify=verify,
            strict=strict))

    return RangeCatalog(generation=catalog_generation,
                        representations=representations,
                        metadata=catalog_meta)


# ============================================================================
@dataclass
class PinnedRepresentation:
    """An immutable representation snapshot associated with a pin token."""

    pin_id: str
    rep: MediaRepresentation
    generation: int
    created_at: float = field(default_factory=time.time)
    proofs: Dict[Tuple[int, int], 'PlannedSource'] = field(default_factory=dict)

    def info(self, base_path='/media'):
        result = self.rep.info()
        result['pinned_url'] = '{0}/pin/{1}'.format(base_path.rstrip('/'),
                                                    self.pin_id)
        result['pin'] = self.pin_id
        result['pinned_generation'] = self.generation
        return result


@dataclass(frozen=True)
class PlannedSource:
    """A source together with the representation span its bytes cover.

    ``start`` is the representation offset of the first byte in ``source``
    and ``digest`` is the digest of that whole span.  For an unchanged
    catalog this is exactly one fragment.  A later catalog that keeps the
    same representation may use different fragment boundaries; the span and
    digest are then derived from the new fragment.
    """

    source: Source
    start: int
    length: int
    digest: Optional[str] = None

    @property
    def end(self):
        return self.start + self.length - 1

    def covers(self, start, end):
        return self.start <= start and end <= self.end

    def slice_offset(self, rep_offset):
        return rep_offset - self.start


# ============================================================================
class MediaResponse:
    """A small HTTP response value used by :class:`MediaArchiveApp`."""

    def __init__(self, status, reason, headers=None, body=None,
                 body_length=None):
        self.status = int(status)
        self.reason = reason
        self.headers = list(headers or [])
        self.body = body if body is not None else []
        self.body_length = body_length

    def __iter__(self):
        return iter(self.body)

    @property
    def statusline(self):
        return '{0} {1}'.format(self.status, self.reason)


# ============================================================================
class ArchiveService:
    """Owns catalog generations and pinned representation snapshots."""

    def __init__(self, catalog=None, reader=None, base_path='/media'):
        self.catalog = catalog if catalog is not None else RangeCatalog()
        self.reader = reader or RangeReader()
        self.base_path = base_path
        self._pins = {}
        self._lock = threading.RLock()

    def publish(self, catalog):
        """Atomically publish a newer catalog without touching existing pins."""
        with self._lock:
            self.catalog = catalog
            return catalog

    def snapshot(self):
        with self._lock:
            return self.catalog

    def pin(self, rep_id=None, url=None, etag=None, content_id=None):
        """Return a pin for the currently selected representation."""
        return self._create_pin(self._select(rep_id, url, etag, content_id))

    def pin_from_response(self, response):
        """Return the pin advertised by a :class:`MediaResponse`."""
        for name, value in response.headers:
            if name.lower() == 'x-archive-pin':
                return self._pin(value)
        return None

    def _pin(self, pin_id):
        with self._lock:
            return self._pins.get(pin_id)

    def _create_pin(self, rep):
        pin_id = uuid.uuid4().hex
        pin = PinnedRepresentation(pin_id, rep, rep.generation)
        with self._lock:
            self._pins[pin_id] = pin
        return pin

    def pin_url(self, pin_id):
        return '{0}/pin/{1}'.format(self.base_path.rstrip('/'), pin_id)

    def info(self, rep_id=None, url=None, etag=None, content_id=None,
             pin=None):
        if pin:
            pin_obj = self._pin(pin)
            if not pin_obj:
                raise RepresentationNotFound(pin)
            return pin_obj.info(self.base_path)

        rep = self._select(rep_id, url, etag, content_id)
        result = rep.info()
        result['catalog_generation'] = self.snapshot().generation
        return result

    def _select(self, rep_id=None, url=None, etag=None, content_id=None):
        catalog = self.snapshot()
        if rep_id:
            rep = catalog.get(rep_id)
        else:
            rep = catalog.find(url=url, etag=etag, content_id=content_id)

        if not rep:
            raise RepresentationNotFound(rep_id or url or etag or '')
        return rep

    def handle_request(self, rep_id=None, url=None, etag=None,
                       content_id=None, pin=None, range_header=None,
                       if_range=None, method='GET'):
        pin_obj = None
        if pin:
            pin_obj = self._pin(pin)
            if not pin_obj:
                raise RepresentationNotFound(pin)
            rep = pin_obj.rep
        else:
            rep = self._select(rep_id, url, etag, content_id)

        has_range = bool(range_header)
        if has_range and if_range:
            if not if_range_matches(if_range, rep.etag):
                # An If-Range failure means the client cache is for another
                # representation.  Only a full response from *this* identity
                # may be used; never a different capture's full response.
                return self._serve_full(rep, pin_obj=pin_obj, method=method)

        if not has_range:
            return self._serve_full(rep, pin_obj=pin_obj, method=method)

        parsed = parse_byte_range(range_header, rep.total_length)
        if parsed is None:
            return self._serve_full(rep, pin_obj=pin_obj, method=method)

        start, end = parsed
        return self._serve_range(rep, start, end, pin_obj=pin_obj,
                                 method=method)

    # ------------------------------------------------------------------
    def _new_pin(self, rep, pin_obj):
        return pin_obj or self._create_pin(rep)

    def _base_headers(self, rep, pin, content_length, content_type=None,
                      extra=None):
        headers = [
            ('Content-Type', content_type or rep.mime),
            ('Content-Length', str(content_length)),
            ('Accept-Ranges', 'bytes'),
            ('ETag', rep.etag or ''),
            ('X-Archive-Representation', rep.rep_id),
            ('X-Archive-Generation', str(rep.generation)),
            ('X-Archive-Coverage', rep.coverage_header),
            ('X-Archive-Pin', pin.pin_id),
            ('X-Archive-Pinned-URL', self.pin_url(pin.pin_id)),
            ('Cache-Control', 'no-transform'),
        ]
        if rep.content_id:
            headers.append(('X-Archive-Content-Id', rep.content_id))

        # Drop empty values rather than emitting empty headers.
        headers = [(name, value) for name, value in headers if value != '']

        if extra:
            headers.extend(extra)

        return headers

    def _serve_full(self, rep, pin_obj=None, method='GET'):
        if not rep.complete:
            # A later catalog generation may now contain the complete
            # representation with the same verified identity.  It is owned
            # by this archive, not a different capture, so use it.
            equivalent = self._equivalent_rep(rep)
            if (not equivalent or not equivalent.complete or
                    equivalent.identity_key != rep.identity_key):
                raise RepresentationNotFound(
                    'No complete representation for {0}'.format(rep.rep_id))

            rep = MediaRepresentation(
                rep_id=rep.rep_id,
                total_length=equivalent.total_length,
                segments=equivalent.segments,
                etag=rep.etag,
                url=rep.url,
                mime=rep.mime,
                content_id=equivalent.content_id,
                generation=rep.generation,
                verified=equivalent.verified)

        pin = self._new_pin(rep, pin_obj)
        plans = self._preflight(rep, pin, 0, rep.total_length - 1)
        headers = self._base_headers(rep, pin, rep.total_length)
        body = () if method == 'HEAD' else self._iter_range(
            rep, pin, 0, rep.total_length - 1, plans)
        return MediaResponse(200, 'OK', headers, body, rep.total_length)

    def _serve_range(self, rep, start, end, pin_obj=None, method='GET'):
        if rep.locate(start, end) is None:
            raise RangeNotSatisfiable(
                'Requested range is not covered by representation',
                rep.total_length)

        pin = self._new_pin(rep, pin_obj)
        # Prove every fragment needed for this request before the response
        # starts.  This is what lets an unstarted request return 416 instead
        # of a 206 whose tail cannot be proven to be the same version.
        plans = self._preflight(rep, pin, start, end)

        length = end - start + 1
        headers = self._base_headers(
            rep, pin, length,
            extra=[('Content-Range',
                    format_content_range(start, end, rep.total_length))])
        body = () if method == 'HEAD' else self._iter_range(
            rep, pin, start, end, plans)
        return MediaResponse(206, 'Partial Content', headers, body, length)

    # ------------------------------------------------------------------
    def _equivalent_rep(self, rep):
        current = self.snapshot()
        equivalent = current.get(rep.rep_id)
        if equivalent and equivalent.identity_key == rep.identity_key:
            return equivalent

        return current.find_equivalent(rep)

    def _candidate_plans(self, rep, pin, segment):
        """Return ``PlannedSource`` entries for a pinned fragment.

        Pinned sources always come first.  If a newer catalog references the
        same verified representation, its sources are appended as fallbacks.
        That covers catalogs which only moved fragment addresses, while
        refusing a same-ETag/different-tail replacement.
        """
        plans = [PlannedSource(source=source,
                               start=segment.start,
                               length=segment.length,
                               digest=segment.digest)
                 for source in segment.sources]

        equivalent = self._equivalent_rep(rep)
        if equivalent is None or equivalent.identity_key != rep.identity_key:
            return plans

        # Prefer a new fragment at the same span.  If the catalog moved
        # fragment boundaries, any new fragment that fully covers the
        # original span with matching content is acceptable.
        alt_segments = []
        same_span = equivalent.find_equivalent_segment(segment)
        if same_span:
            alt_segments.append(same_span)

        # Never use a fragment that claims different content for this span.
        alt_segments = [
            alt for alt in alt_segments
            if not (segment.digest and alt.digest and
                    segment.digest != alt.digest)
        ]

        known = {item.source.identity for item in plans}
        for alt_segment in alt_segments:
            for source in alt_segment.sources:
                if source.identity in known:
                    continue
                plans.append(PlannedSource(
                    source=source,
                    start=alt_segment.start,
                    length=alt_segment.length,
                    digest=alt_segment.digest or segment.digest))
                known.add(source.identity)

        return plans

    def _candidate_wide_plans(self, rep, pin, segment):
        """Include later fragments that verify a span from the new catalog.

        A replacement catalog may merge or split fragments, so a fragment at
        the same span is not the only possible source.  A wider fragment is
        only usable when its bytes fully cover the pinned span; the planned
        digest is then the pinned fragment digest and the reader verifies
        the whole source before using it.
        """
        plans = self._candidate_plans(rep, pin, segment)

        equivalent = self._equivalent_rep(rep)
        if equivalent is None or equivalent.identity_key != rep.identity_key:
            return plans

        known = {item.source.identity for item in plans}
        for alt in equivalent.segments:
            if not (alt.start <= segment.start and
                    alt.end >= segment.end):
                continue
            if alt.start == segment.start and alt.end == segment.end:
                continue
            for alt_source in alt.sources:
                if alt_source.identity in known:
                    continue
                plans.append(PlannedSource(
                    source=alt_source,
                    start=alt.start,
                    length=alt.length,
                    digest=alt.digest))
                known.add(alt_source.identity)

        return plans

    def _prove_plan(self, plan):
        """Return True if the source can yield the planned verified span.

        A source whose recorded file identity is unchanged is trusted.  A
        source at the same address but a changed object (for example a file
        replaced by a newer catalog) must be read in full and match the
        span digest; the digest is the only evidence that the bytes still
        belong to this representation.
        """
        source = plan.source
        if not self.reader.available(source):
            return False

        if source.data is not None:
            return True

        if source.unchanged():
            return True

        if not plan.digest:
            return False

        try:
            data = self.reader.read(source, 0, plan.length)
        except Exception:
            return False

        return (data is not None and len(data) == plan.length and
                sha256(data).hexdigest() == plan.digest)

    def _preflight(self, rep, pin, start, end):
        located = rep.locate(start, end)
        if located is None:
            raise RangeNotSatisfiable('Uncovered range', rep.total_length)

        plans = {}
        for segment, _local_start, _local_end in located:
            key = (segment.start, segment.end)
            if key in plans:
                continue

            # A proof recorded by an earlier request remains valid only
            # while the source object itself has not changed.
            existing = pin.proofs.get(key)
            if existing is not None and self._prove_plan(existing):
                plans[key] = existing
                continue

            chosen = None
            last_error = None
            for plan in self._candidate_wide_plans(rep, pin, segment):
                try:
                    if self._prove_plan(plan):
                        chosen = plan
                        break
                    last_error = 'source content could not be proven'
                except Exception as exc:
                    last_error = exc

            if chosen is None:
                raise RangeNotSatisfiable(
                    'Could not prove fragment {0}-{1} belongs to '
                    'representation {2}{3}'.format(
                        segment.start, segment.end, rep.rep_id,
                        ': ' + str(last_error) if last_error else ''),
                    rep.total_length)

            pin.proofs[key] = chosen
            plans[key] = chosen

        return plans

    def _resolve_live_plan(self, rep, pin, segment, planned):
        """Choose a plan at the moment its bytes are needed.

        The catalog may have been republished since the response started.
        An unchanged pinned plan is used directly.  If its source changed,
        retry the remaining candidates; a replacement source is accepted
        only after its full span digest matches.
        """
        if planned is not None and self._prove_plan(planned):
            return planned

        for plan in self._candidate_wide_plans(rep, pin, segment):
            if self._prove_plan(plan):
                pin.proofs[(segment.start, segment.end)] = plan
                return plan

        raise RangeNotSatisfiable(
            'Could not prove the remaining bytes of representation {0}'.format(
                rep.rep_id),
            rep.total_length)

    def _iter_range(self, rep, pin, start, end, plans):
        for segment, local_start, local_end in rep.locate(start, end) or ():
            planned = plans.get((segment.start, segment.end))
            plan = self._resolve_live_plan(rep, pin, segment, planned)
            rep_start = segment.start + local_start
            rep_end = segment.start + local_end
            expected = rep_end - rep_start + 1

            if not plan.covers(rep_start, rep_end):
                raise RangeNotSatisfiable(
                    'Replacement source does not cover the requested span',
                    rep.total_length)

            slice_start = plan.slice_offset(rep_start)

            if plan.source.unchanged():
                # Read only the requested slice.  Use bounded reads so a
                # caller that stops after the first fragment has not caused
                # the second fragment to be fetched.
                written = 0
                for chunk in self.reader.iter_bytes(plan.source, slice_start,
                                                    expected):
                    if not chunk:
                        continue
                    written += len(chunk)
                    yield chunk
                if written != expected:
                    raise RangeNotSatisfiable(
                        'Fragment {0}-{1} ended before the requested '
                        'bytes'.format(segment.start, segment.end),
                        rep.total_length)
            else:
                # The file object changed after preflight.  Read the full
                # span and verify its digest before emitting any of it.
                data = self.reader.read(plan.source, 0, plan.length)
                if (data is None or len(data) != plan.length or
                        sha256(data).hexdigest() != plan.digest):
                    raise RangeNotSatisfiable(
                        'Fragment {0}-{1} changed during playback'.format(
                            segment.start, segment.end),
                        rep.total_length)
                yield data[slice_start:slice_start + expected]

# ============================================================================
class MediaArchiveApp:
    """A small WSGI application exposing the range catalog.

    Routes::

        GET /media                      catalog summary
        GET /media/<rep-id>             read a representation
        GET /media/rep/<rep-id>         same
        GET /media/info/<rep-id>        JSON representation metadata
        GET /media/pin/<pin-id>         continue a pinned representation
        GET /media/pin/<pin-id>/info    JSON pinned metadata
        GET /media/select?url=&etag=    select a representation

    ``Range`` and ``If-Range`` are handled on the read routes and on the
    pinned route.
    """

    def __init__(self, service, path='/media'):
        self.service = service
        self.path = path.rstrip('/') or '/media'

    def __call__(self, environ, start_response):
        method = environ.get('REQUEST_METHOD', 'GET').upper()
        path = unquote(environ.get('PATH_INFO', ''))
        path = path.rstrip('/') or '/'
        query = self._parse_query(environ.get('QUERY_STRING', ''))

        try:
            if path in (self.path, self.path + '/catalog'):
                return self._catalog_response(start_response)

            if path.startswith(self.path + '/info/'):
                rep_id = path[len(self.path + '/info/'):]
                return self._info_response(start_response,
                                           rep_id=rep_id,
                                           query=query)

            if path.startswith(self.path + '/pin/'):
                rest = path[len(self.path + '/pin/'):]
                if rest.endswith('/info'):
                    return self._info_response(
                        start_response, pin=rest[:-len('/info')],
                        query=query)
                return self._read_response(environ, start_response,
                                           pin=rest, method=method)

            if path.startswith(self.path + '/rep/'):
                return self._read_response(
                    environ, start_response,
                    rep_id=path[len(self.path + '/rep/'):], method=method)

            if path == self.path + '/select':
                return self._read_response(
                    environ, start_response,
                    url=query.get('url'), etag=query.get('etag'),
                    content_id=query.get('content_id'), method=method)

            if path.startswith(self.path + '/'):
                return self._read_response(
                    environ, start_response,
                    rep_id=path[len(self.path + '/'):], method=method)

            return self._error(start_response, 404, 'Not Found')

        except RepresentationNotFound as exc:
            return self._error(start_response, 404, str(exc))
        except RangeNotSatisfiable as exc:
            return self._error(start_response, 416, str(exc),
                               total_length=exc.total_length)
        except Exception as exc:
            return self._error(start_response, 500, str(exc))

    @staticmethod
    def _parse_query(query_string):
        result = {}
        for item in query_string.split('&'):
            if not item:
                continue
            if '=' in item:
                name, value = item.split('=', 1)
            else:
                name, value = item, ''
            result[unquote(name)] = unquote(value)
        return result

    def _read_response(self, environ, start_response, rep_id=None,
                       pin=None, url=None, etag=None, content_id=None,
                       method='GET'):
        response = self.service.handle_request(
            rep_id=rep_id, pin=pin, url=url, etag=etag,
            content_id=content_id,
            range_header=environ.get('HTTP_RANGE'),
            if_range=environ.get('HTTP_IF_RANGE'),
            method=method)

        headers = [(name, str(value)) for name, value in response.headers]
        start_response(response.statusline, headers)
        if method == 'HEAD':
            return []
        body = response.body
        if isinstance(body, (bytes, str)):
            body = [body]
        return body

    def _catalog_response(self, start_response):
        return self._json(start_response, 200, self.service.snapshot().info())

    def _info_response(self, start_response, rep_id=None, pin=None,
                       query=None):
        query = query or {}
        data = self.service.info(rep_id=rep_id, pin=pin,
                                 url=query.get('url'),
                                 etag=query.get('etag'),
                                 content_id=query.get('content_id'))
        return self._json(start_response, 200, data)

    @staticmethod
    def _json(start_response, status, data):
        body = json.dumps(data, sort_keys=True).encode('utf-8')
        headers = [
            ('Content-Type', 'application/json; charset=utf-8'),
            ('Content-Length', str(len(body))),
        ]
        if status == 200:
            start_response('200 OK', headers)
        else:
            start_response(str(status) + ' Error', headers)
        return [body]

    @staticmethod
    def _error(start_response, status, message, total_length=None):
        reason = {404: 'Not Found', 416: 'Range Not Satisfiable',
                  500: 'Internal Server Error'}.get(status, 'Error')
        body = json.dumps({'error': message}).encode('utf-8')
        headers = [
            ('Content-Type', 'application/json; charset=utf-8'),
            ('Content-Length', str(len(body))),
        ]
        if status == 416:
            if total_length is not None:
                headers.append(('Content-Range',
                                'bytes */{0}'.format(total_length)))
            headers.append(('Accept-Ranges', 'bytes'))
        start_response('{0} {1}'.format(status, reason), headers)
        return [body]
