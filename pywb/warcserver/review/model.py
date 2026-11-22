"""Data model shared by the capture review engine and the acceptance tests.

The review engine works on the *true* capture time of a record, which is the
``WARC-Date`` of the record itself.  An index timestamp is only a promise: a
CDX line may say the capture is on one UTC day while the record headers place
it on the next one.  A candidate is therefore never adopted before its header
record has been opened.
"""

import hashlib

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from warcio.timeutils import timestamp_to_sec


DAY_LEN = 8


def check_day(day):
    if not isinstance(day, str) or len(day) != DAY_LEN or not day.isdigit():
        raise ValueError('day must be a YYYYMMDD string: {0!r}'.format(day))


def check_timestamp(timestamp):
    if not isinstance(timestamp, str) or len(timestamp) < DAY_LEN:
        raise ValueError('invalid timestamp: {0!r}'.format(timestamp))


def timestamp_sec(timestamp):
    check_timestamp(timestamp)
    return timestamp_to_sec(timestamp[:14])


def day_of(timestamp):
    return timestamp[:DAY_LEN]


def day_start(day):
    check_day(day)
    return day + '000000'


def day_end(day):
    check_day(day)
    return day + '235959'


def in_day(timestamp, day):
    return day_of(timestamp) == day


# ============================================================================
@dataclass(frozen=True)
class RecordRef(object):
    """The physical location of one WARC record."""

    filename: str
    offset: int
    length: int

    def to_dict(self):
        return {'filename': self.filename,
                'offset': int(self.offset),
                'length': int(self.length)}

    @classmethod
    def from_dict(cls, data):
        if not data:
            return None
        return cls(data['filename'], int(data['offset']), int(data['length']))


# ============================================================================
@dataclass
class Candidate(object):
    """One paged index candidate, before its WARC record is opened."""

    source: str
    url: str
    cdx_timestamp: str
    filename: str
    offset: int
    length: int
    digest: str = '-'
    status: str = '200'
    mime: str = 'text/html'
    body_ref: Optional[RecordRef] = None
    access: str = 'allow'
    order: int = -1

    def __post_init__(self):
        check_timestamp(self.cdx_timestamp)

    @property
    def id(self):
        # The identity of the physical candidate: a duplicate CDX row with a
        # different digest but the same record location is still the same
        # candidate, so the digest is deliberately not part of the identity.
        parts = (self.source, self.url, self.cdx_timestamp, self.filename,
                 str(self.offset), str(self.length))
        return hashlib.sha1('\x00'.join(parts).encode('utf-8')).hexdigest()

    @property
    def header_ref(self):
        return RecordRef(self.filename, int(self.offset), int(self.length))

    def to_dict(self):
        return {'source': self.source,
                'url': self.url,
                'cdx_timestamp': self.cdx_timestamp,
                'filename': self.filename,
                'offset': int(self.offset),
                'length': int(self.length),
                'digest': self.digest,
                'status': self.status,
                'mime': self.mime,
                'body_ref': self.body_ref.to_dict() if self.body_ref else None,
                'access': self.access,
                'order': self.order}

    @classmethod
    def from_dict(cls, data):
        return cls(source=data['source'],
                   url=data['url'],
                   cdx_timestamp=data['cdx_timestamp'],
                   filename=data['filename'],
                   offset=int(data['offset']),
                   length=int(data['length']),
                   digest=data.get('digest', '-'),
                   status=data.get('status', '200'),
                   mime=data.get('mime', 'text/html'),
                   body_ref=RecordRef.from_dict(data.get('body_ref')),
                   access=data.get('access', 'allow'),
                   order=int(data.get('order', -1)))


# ============================================================================
@dataclass
class Boundary(object):
    """A source's claim about the true capture times it has not paged yet.

    ``end`` is an inclusive upper bound, ``start`` an optional lower bound.
    ``attested`` means the source vouches for the bound in terms of real
    capture time, not merely in terms of CDX ordering.  ``kind`` records how
    the bound was obtained; the engine only trusts configured kinds.

    ``probe`` is an optional record which must be opened to confirm the bound.
    A source that cannot provide a confirmed bound leaves the range pending.
    """

    start: Optional[str] = None
    end: Optional[str] = None
    attested: bool = False
    kind: str = 'cdx'
    probe: Optional[RecordRef] = None
    note: str = ''
    probe_confirmed: bool = False
    probe_checked: bool = False
    probe_date: Optional[str] = None

    def to_dict(self):
        return {'start': self.start,
                'end': self.end,
                'attested': bool(self.attested),
                'kind': self.kind,
                'probe': self.probe.to_dict() if self.probe else None,
                'note': self.note,
                'probe_confirmed': bool(self.probe_confirmed),
                'probe_checked': bool(self.probe_checked),
                'probe_date': self.probe_date}

    @classmethod
    def from_dict(cls, data):
        if not data:
            return None
        return cls(start=data.get('start'),
                   end=data.get('end'),
                   attested=bool(data.get('attested')),
                   kind=data.get('kind', 'cdx'),
                   probe=RecordRef.from_dict(data.get('probe')),
                   note=data.get('note', ''),
                   probe_confirmed=bool(data.get('probe_confirmed')),
                   probe_checked=bool(data.get('probe_checked')),
                   probe_date=data.get('probe_date'))


# ============================================================================
@dataclass
class SourcePage(object):
    """One page of a paged source."""

    candidates: List[Candidate] = field(default_factory=list)
    next_cursor: object = None
    complete: bool = False
    boundary: Optional[Boundary] = None
    access_error: Optional[str] = None


# ============================================================================
@dataclass
class Verification(object):
    """The outcome of opening a candidate's WARC record(s)."""

    status: str = 'unreadable'
    true_timestamp: Optional[str] = None
    warc_date: Optional[str] = None
    warc_type: Optional[str] = None
    header_ref: Optional[RecordRef] = None
    body_ref: Optional[RecordRef] = None
    body_date: Optional[str] = None
    body_kind: str = ''
    replayable: bool = False
    distance: Optional[int] = None
    detail: str = ''
    opens: int = 0

    @property
    def eligible(self):
        return self.status == 'verified'

    def to_dict(self):
        return {'status': self.status,
                'true_timestamp': self.true_timestamp,
                'warc_date': self.warc_date,
                'warc_type': self.warc_type,
                'header_ref': self.header_ref.to_dict() if self.header_ref else None,
                'body_ref': self.body_ref.to_dict() if self.body_ref else None,
                'body_date': self.body_date,
                'body_kind': self.body_kind,
                'replayable': bool(self.replayable),
                'distance': self.distance,
                'detail': self.detail,
                'opens': int(self.opens)}

    @classmethod
    def from_dict(cls, data):
        if not data:
            return None
        return cls(status=data.get('status', 'unreadable'),
                   true_timestamp=data.get('true_timestamp'),
                   warc_date=data.get('warc_date'),
                   warc_type=data.get('warc_type'),
                   header_ref=RecordRef.from_dict(data.get('header_ref')),
                   body_ref=RecordRef.from_dict(data.get('body_ref')),
                   body_date=data.get('body_date'),
                   body_kind=data.get('body_kind', ''),
                   replayable=bool(data.get('replayable')),
                   distance=data.get('distance'),
                   detail=data.get('detail', ''),
                   opens=int(data.get('opens', 0)))


# ============================================================================
@dataclass
class CandidateState(object):
    candidate: Candidate
    verification: Optional[Verification] = None
    withdrawn: bool = False
    withdrawn_reason: str = ''
    skipped: Optional[str] = None

    @property
    def id(self):
        return self.candidate.id

    def to_dict(self):
        return {'candidate': self.candidate.to_dict(),
                'verification': self.verification.to_dict() if self.verification else None,
                'withdrawn': bool(self.withdrawn),
                'withdrawn_reason': self.withdrawn_reason,
                'skipped': self.skipped}

    @classmethod
    def from_dict(cls, data):
        return cls(candidate=Candidate.from_dict(data['candidate']),
                   verification=Verification.from_dict(data.get('verification')),
                   withdrawn=bool(data.get('withdrawn')),
                   withdrawn_reason=data.get('withdrawn_reason', ''),
                   skipped=data.get('skipped'))


# ============================================================================
@dataclass
class SourceState(object):
    name: str
    cursor: object = 0
    started: bool = False
    complete: bool = False
    fetched: int = 0
    boundary: Optional[Boundary] = None
    access_error: Optional[str] = None
    access_denied: bool = False
    settled: Optional[str] = None
    duplicates: int = 0
    signature: object = None

    def to_dict(self):
        return {'name': self.name,
                'cursor': self.cursor,
                'started': bool(self.started),
                'complete': bool(self.complete),
                'fetched': int(self.fetched),
                'boundary': self.boundary.to_dict() if self.boundary else None,
                'access_error': self.access_error,
                'access_denied': bool(self.access_denied),
                'settled': self.settled,
                'duplicates': int(self.duplicates),
                'signature': self.signature}

    @classmethod
    def from_dict(cls, data):
        return cls(name=data['name'],
                   cursor=data.get('cursor', 0),
                   started=bool(data.get('started')),
                   complete=bool(data.get('complete')),
                   fetched=int(data.get('fetched', 0)),
                   boundary=Boundary.from_dict(data.get('boundary')),
                   access_error=data.get('access_error'),
                   access_denied=bool(data.get('access_denied')),
                   settled=data.get('settled'),
                   duplicates=int(data.get('duplicates', 0)),
                   signature=data.get('signature'))


# ============================================================================
@dataclass
class ReviewState(object):
    url: str
    day: str
    at: str
    sources: Dict[str, SourceState] = field(default_factory=dict)
    candidates: List[CandidateState] = field(default_factory=list)
    consumed: Dict[str, int] = field(default_factory=lambda: {'page_turns': 0, 'warc_opens': 0})
    next_order: int = 0
    withdrawn: List[str] = field(default_factory=list)
    tombstones: Dict[str, str] = field(default_factory=dict)

    def add_candidate(self, candidate):
        if candidate.id in self.tombstones:
            return None
        candidate.order = self.next_order
        self.next_order += 1
        state = CandidateState(candidate)
        self.candidates.append(state)
        return state

    def to_dict(self):
        return {'version': 1,
                'url': self.url,
                'day': self.day,
                'at': self.at,
                'sources': {name: source.to_dict()
                            for name, source in self.sources.items()},
                'candidates': [candidate.to_dict() for candidate in self.candidates],
                'consumed': dict(self.consumed),
                'next_order': self.next_order,
                'withdrawn': list(self.withdrawn),
                'tombstones': dict(self.tombstones)}

    @classmethod
    def from_dict(cls, data):
        state = cls(url=data['url'], day=data['day'], at=data['at'])
        state.sources = {name: SourceState.from_dict(source)
                         for name, source in data.get('sources', {}).items()}
        state.candidates = [CandidateState.from_dict(candidate)
                            for candidate in data.get('candidates', [])]
        state.consumed = dict(data.get('consumed') or {})
        state.next_order = int(data.get('next_order', 0))
        state.withdrawn = list(data.get('withdrawn') or [])
        state.tombstones = dict(data.get('tombstones') or {})
        return state

    def best(self):
        """The eligible capture nearest ``at``; ties keep enumeration order."""
        best = None
        for state in self.candidates:
            if state.withdrawn or not state.verification:
                continue
            if not state.verification.eligible:
                continue
            key = (state.verification.distance, state.candidate.order)
            if best is None or key < best[0]:
                best = (key, state)
        return best[1] if best else None
