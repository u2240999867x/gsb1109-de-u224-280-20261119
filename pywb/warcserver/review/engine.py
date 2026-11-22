"""Round-based review of the captures of one URL on one UTC day.

The engine is deliberately pull-based.  A *round* spends a caller-supplied
budget on page turns and WARC opens, then returns either a terminal decision
or a resume credential carrying the whole serialisable state.  Nothing is
cached outside that state, so a later process can continue without re-reading
sources it already finished and without trusting evidence it has withdrawn.

Decision rules
--------------
The review instant is ``at`` (a 14+ digit timestamp).  A verified capture is
eligible when its own ``WARC-Date`` is on ``day`` and it is replayable.
Eligible captures are ranked by ``(distance, enumeration_order)``, exactly the
tie break of :func:`pywb.warcserver.index.cdxops.cdx_sort_closest`.

A terminal "no capture on this day" answer requires *complete* coverage of
every source for the whole day.  A source is complete when one of:

* it reported its last page;
* it supplied an attested bound that provably cannot hold a capture nearer
  than the current best, and that bound was confirmed;
* access control prevented it from being read (reported separately, never as
  proof of absence).

Without such coverage the round returns ``pending`` with a resume credential.
"""

from typing import Dict

from .model import (ReviewState, SourceState, Verification, day_end,
                    day_start, timestamp_sec)
from .sources import ReviewSource, SourceError
from .warc import ArchiveReadError, WarcVerifier
from .credential import CredentialCodec


TRUSTED_BOUNDARY_KINDS = ('warc-date', 'source-order')
DEFAULT_PAGE_TURNS = 4
DEFAULT_WARC_OPENS = 4


class Budget(object):
    """Mutable per-round allowance of page turns and WARC opens."""

    def __init__(self, page_turns, warc_opens):
        self.page_turns = int(page_turns)
        self.warc_opens = int(warc_opens)
        if self.page_turns < 0 or self.warc_opens < 0:
            raise ValueError('budgets must be non-negative')

    @property
    def exhausted(self):
        return self.page_turns <= 0 or self.warc_opens <= 0

    def take_page(self):
        if self.page_turns <= 0:
            raise BudgetExhausted('page turn budget exhausted')
        self.page_turns -= 1

    def take_open(self):
        if self.warc_opens <= 0:
            raise BudgetExhausted('WARC open budget exhausted')
        self.warc_opens -= 1

    def take_opens(self, count):
        if count <= 0:
            return
        if self.warc_opens < count:
            raise BudgetExhausted('WARC open budget exhausted')
        self.warc_opens -= count

    def to_dict(self):
        return {'page_turns': self.page_turns, 'warc_opens': self.warc_opens}


class BudgetExhausted(Exception):
    pass


# ============================================================================
class ReviewEngine(object):
    """Coordinate paged sources and WARC verification for one URL/day."""

    PAGE_TURNS = DEFAULT_PAGE_TURNS
    WARC_OPENS = DEFAULT_WARC_OPENS

    def __init__(self, sources, verifier=None, url=None, day=None, at=None,
                 page_turns=None, warc_opens=None,
                 trusted_boundary_kinds=None, credential_key=None):
        if isinstance(sources, dict):
            source_list = list(sources.values())
        else:
            source_list = list(sources)

        self.sources: Dict[str, ReviewSource] = {s.name: s for s in source_list}
        if len(self.sources) != len(source_list):
            raise ValueError('source names must be unique')

        self.verifier = verifier or WarcVerifier()
        self.url = url
        self.day = day
        self.at = at
        self.page_turns = self.PAGE_TURNS if page_turns is None else page_turns
        self.warc_opens = self.WARC_OPENS if warc_opens is None else warc_opens
        self.trusted_boundary_kinds = tuple(
            trusted_boundary_kinds or TRUSTED_BOUNDARY_KINDS)
        self.codec = CredentialCodec(credential_key)

    # ------------------------------------------------------------------
    def new_state(self, url=None, day=None, at=None):
        url = url or self.url
        day = day or self.day
        at = at or self.at
        if not url or not day or not at:
            raise ValueError('url, day and at are required')

        if timestamp_sec(at) < timestamp_sec(day_start(day)) or \
                timestamp_sec(at) > timestamp_sec(day_end(day)):
            raise ValueError('at must fall on the reviewed day')

        state = ReviewState(url=url, day=day, at=at)
        for name in self.sources:
            state.sources[name] = SourceState(name=name)
        return state

    # ------------------------------------------------------------------
    def run(self, state, page_turns=None, warc_opens=None):
        """Spend one round of budget and return a result dict.

        ``state`` is a :class:`ReviewState` or a resume credential returned by
        an earlier round.  The returned ``resume_token`` can be passed back to
        this method directly.
        """
        state = self.resolve_state(state)

        budget = Budget(self.page_turns if page_turns is None else page_turns,
                        self.warc_opens if warc_opens is None else warc_opens)

        start_page = state.consumed.get('page_turns', 0)
        start_open = state.consumed.get('warc_opens', 0)

        while True:
            progress = False

            # 1. Verify candidates already present in the state.
            if self._has_unverified(state):
                if budget.warc_opens <= 0:
                    break
                if not self._can_afford_next_verify(state, budget):
                    break
                self._verify_pending(state, budget)
                progress = True
                continue

            # 2. Confirm a boundary probe already supplied by a source before
            #    spending another page turn.  A confirmed bound can settle the
            #    review without reading the remaining pages at all.
            if self._has_pending_probe(state):
                if budget.warc_opens <= 0:
                    break
                if self._confirm_probe(state, budget):
                    progress = True
                    continue

            # 3. Page a source that still has pages.
            if self._has_pageable(state):
                if budget.page_turns <= 0:
                    break
                self._page_one(state, budget)
                progress = True
                continue

            if not progress:
                break

        result = self._result(state)
        result['consumed'] = {
            'page_turns': state.consumed.get('page_turns', 0) - start_page,
            'warc_opens': state.consumed.get('warc_opens', 0) - start_open,
        }
        result['budget'] = budget.to_dict()
        return result

    # ------------------------------------------------------------------
    def resolve_state(self, state):
        if isinstance(state, ReviewState):
            return state
        return self.decode(state)

    def encode(self, state):
        return self.codec.encode(state)

    def decode(self, token):
        return self.codec.decode(token)

    # ------------------------------------------------------------------
    def invalidate_changed_sources(self, state):
        """Restart only the sources whose contents changed since paging.

        A source whose snapshot differs is re-paged from its first page; its
        old candidates and their evidence are discarded.  Unchanged sources
        keep their cursor and already-opened evidence, so a local change does
        not force a full re-run of the unchanged source.
        """
        changed = []
        for name, source in self.sources.items():
            source_state = state.sources.get(name)
            if source_state is None or not source_state.fetched:
                continue

            current = source.snapshot()
            if source_state.signature == current:
                continue

            changed.append(name)
            self._discard_source(state, name)

            source_state.cursor = 0
            source_state.started = False
            source_state.complete = False
            source_state.fetched = 0
            source_state.boundary = None
            source_state.access_error = None
            source_state.access_denied = False
            source_state.settled = None
            source_state.duplicates = 0
            source_state.signature = None

        return changed

    def _discard_source(self, state, name):
        state.candidates = [candidate for candidate in state.candidates
                            if candidate.candidate.source != name]

    # ------------------------------------------------------------------
    def _has_unverified(self, state):
        for candidate in state.candidates:
            if candidate.withdrawn or candidate.skipped:
                continue
            if candidate.verification is None:
                return True
            if candidate.verification.status == 'header-verified':
                return True
        return False

    def _has_pageable(self, state):
        for source in state.sources.values():
            if source.access_denied or source.complete:
                continue
            if self._boundary_settles(source, state, state.best()):
                continue
            return True
        return False

    def _next_page_state(self, state):
        # Prefer a source not started yet, then the one with the fewest pages
        # fetched, keeping the configured declaration order otherwise.
        order = list(self.sources)
        best = state.best()
        pending = [state.sources[name] for name in order
                   if not state.sources[name].complete
                   and not state.sources[name].access_denied
                   and not self._boundary_settles(state.sources[name],
                                                  state, best)]
        pending.sort(key=lambda s: (s.started, s.fetched, order.index(s.name)))
        return pending[0] if pending else None

    def _page_one(self, state, budget):
        source_state = self._next_page_state(state)
        if source_state is None:
            return

        budget.take_page()
        state.consumed['page_turns'] = state.consumed.get('page_turns', 0) + 1

        source = self.sources[source_state.name]
        cursor = source_state.cursor

        try:
            page = source.page(cursor)
        except SourceError as e:
            source_state.access_error = str(e)
            source_state.access_denied = True
            source_state.settled = 'access'
            return

        source_state.started = True
        source_state.fetched += 1
        source_state.signature = source.snapshot()

        if page.access_error:
            source_state.access_error = page.access_error
            source_state.access_denied = True
            source_state.settled = 'access'
            return

        for candidate in page.candidates:
            self._merge_candidate(state, source_state, candidate)

        if page.boundary is not None:
            source_state.boundary = page.boundary

        if page.complete:
            source_state.complete = True
            source_state.settled = 'complete'
        else:
            source_state.cursor = page.next_cursor

    def _merge_candidate(self, state, source_state, candidate):
        existing = {c.id: c for c in state.candidates}
        found = existing.get(candidate.id)

        if found is not None:
            if found.withdrawn:
                # A withdrawn candidate must not be resurrected by a later
                # page: the withdrawal is evidence about the archive, not
                # about one page's listing.
                source_state.duplicates += 1
                return
            source_state.duplicates += 1
            return

        state.add_candidate(candidate)

    def _can_afford_next_verify(self, state, budget):
        """Whether the next verification can make progress this round.

        A revisit may need a second open for its payload.  When only one open
        remains the header is still read and kept as ``header-verified``; the
        payload is resolved in the next round, so progress never stalls.
        """
        return budget.warc_opens > 0

    def _next_candidate(self, state):
        pending = [c for c in state.candidates
                   if not c.withdrawn and not c.skipped
                   and (c.verification is None or
                        c.verification.status == 'header-verified')]
        pending.sort(key=lambda c: c.candidate.order)
        return pending[0] if pending else None

    # ------------------------------------------------------------------
    def _verify_pending(self, state, budget):
        candidate_state = self._next_candidate(state)
        if candidate_state is None:
            return

        candidate = candidate_state.candidate
        at_sec = timestamp_sec(state.at)

        # Finish a revisit whose header was already proven in an earlier
        # round, without re-reading the header.
        if candidate_state.verification is not None and \
                candidate_state.verification.status == 'header-verified':
            budget.take_open()
            state.consumed['warc_opens'] = state.consumed.get('warc_opens', 0) + 1
            self.verifier.resolve_body(candidate, candidate_state.verification)
            return

        budget.take_open()
        state.consumed['warc_opens'] = state.consumed.get('warc_opens', 0) + 1

        try:
            verification = self.verifier.verify(
                candidate, state.day, at_sec,
                open_body=budget.warc_opens > 0)
            extra = max(0, verification.opens - 1)
            if extra:
                budget.take_opens(extra)
                state.consumed['warc_opens'] = state.consumed.get('warc_opens', 0) + extra
        except ArchiveReadError as e:
            verification = Verification(status='unreadable',
                                        true_timestamp=candidate.cdx_timestamp,
                                        header_ref=candidate.header_ref,
                                        detail='header unreadable: ' + str(e))
        except Exception as e:
            from pywb.utils.wbexception import AccessException
            if isinstance(e, AccessException):
                raise
            verification = Verification(status='unreadable',
                                        true_timestamp=candidate.cdx_timestamp,
                                        header_ref=candidate.header_ref,
                                        detail='verification failed: ' + repr(e))

        candidate_state.verification = verification

    # ------------------------------------------------------------------
    def _has_pending_probe(self, state):
        best = state.best()
        if best is None:
            return False

        for source in state.sources.values():
            if source.access_denied or source.complete:
                continue
            boundary = source.boundary
            if boundary and boundary.probe and not boundary.probe_checked \
                    and self._probe_could_help(boundary, state, best):
                return True
        return False

    def _probe_could_help(self, boundary, state, best):
        if not boundary.attested or \
                boundary.kind not in self.trusted_boundary_kinds:
            return False

        best_distance = best.verification.distance
        at_sec = timestamp_sec(state.at)

        start_sec = timestamp_sec(boundary.start) if boundary.start else None
        end_sec = timestamp_sec(boundary.end) if boundary.end else None

        if start_sec is not None and start_sec > at_sec:
            return start_sec - at_sec > best_distance
        if end_sec is not None and end_sec < at_sec:
            return at_sec - end_sec > best_distance
        return False

    def _confirm_probe(self, state, budget):
        for source in state.sources.values():
            if source.access_denied or source.complete:
                continue
            boundary = source.boundary
            if not boundary or not boundary.probe or boundary.probe_checked:
                continue
            if not self._probe_could_help(boundary, state, state.best()):
                # Mark it checked so the same unhelpful probe is not retried;
                # the bound simply remains untrusted.
                continue

            budget.take_open()
            state.consumed['warc_opens'] = state.consumed.get('warc_opens', 0) + 1
            header = self.verifier.inspect_header(boundary.probe)
            boundary.probe_checked = True
            boundary.probe_date = header['timestamp']
            boundary.probe_confirmed = bool(header['timestamp'])
            if boundary.probe_confirmed:
                # The probe must actually fall inside the range it claims to
                # bound; otherwise the source's ordering is not trustworthy
                # for this boundary.
                if boundary.start and \
                        timestamp_sec(boundary.probe_date) < timestamp_sec(boundary.start):
                    boundary.probe_confirmed = False
                    boundary.attested = False
                if boundary.end and \
                        timestamp_sec(boundary.probe_date) > timestamp_sec(boundary.end):
                    boundary.probe_confirmed = False
                    boundary.attested = False
            return True

        return False

    # ------------------------------------------------------------------
    def _boundary_settles(self, source, state, best):
        """True when ``source``'s attested bound cannot hide a better capture.

        A bound is useful only when it is about true capture times, came from
        a trusted kind, and (if it names a probe record) that probe was opened.
        """
        boundary = source.boundary
        if not boundary or source.complete or source.access_denied:
            return False

        if not boundary.attested or boundary.kind not in self.trusted_boundary_kinds:
            return False

        if boundary.probe and not boundary.probe_confirmed:
            return False

        if best is None:
            return False

        best_sec = timestamp_sec(best.verification.true_timestamp)
        best_distance = best.verification.distance
        at_sec = timestamp_sec(state.at)

        start_sec = timestamp_sec(boundary.start) if boundary.start else None
        end_sec = timestamp_sec(boundary.end) if boundary.end else None

        # The bound is a closed interval of true capture times that may still
        # be paged.  It settles the review when it lies strictly on one side of
        # the review instant and its nearest point is farther than the best
        # capture.  A bound that straddles ``at`` always could be nearer.
        if start_sec is not None and start_sec > at_sec:
            return start_sec - at_sec > best_distance

        if end_sec is not None and end_sec < at_sec:
            return at_sec - end_sec > best_distance

        return False

    # ------------------------------------------------------------------
    def _result(self, state):
        best = state.best()

        pending_sources = []
        access_sources = []
        for name in self.sources:
            source = state.sources[name]
            if source.access_denied:
                access_sources.append(name)
                continue
            if source.complete:
                continue
            if best is not None and self._boundary_settles(source, state, best):
                continue
            pending_sources.append(name)

        withheld = []
        seen = set()
        for candidate in state.candidates:
            if candidate.withdrawn:
                seen.add(candidate.id)
                withheld.append({'id': candidate.id,
                                 'reason': candidate.withdrawn_reason,
                                 'cdx_timestamp': candidate.candidate.cdx_timestamp})
        # Withdrawals whose state was discarded by a source re-read are still
        # part of the audit trail.
        for candidate_id, reason in state.tombstones.items():
            if candidate_id not in seen:
                withheld.append({'id': candidate_id, 'reason': reason,
                                 'cdx_timestamp': None})

        status = self._status(best, pending_sources, access_sources,
                              self._has_unverified(state))
        result = {
            'status': status,
            'url': state.url,
            'day': state.day,
            'at': state.at,
            'consumed': dict(state.consumed),
            'sources': {name: self._source_report(state, name)
                        for name in self.sources},
            'candidates': [self._candidate_report(c) for c in state.candidates],
            'withdrawn': withheld,
        }

        if best is not None:
            result['capture'] = self._capture_report(best)
        else:
            result['capture'] = None

        result['best'] = result['capture']
        result['pending_sources'] = pending_sources
        result['access_denied_sources'] = access_sources

        if status == 'pending':
            result['resume_token'] = self.encode(state)
        else:
            result['resume_token'] = None

        return result

    def _status(self, best, pending_sources, access_sources, open_evidence=False):
        if pending_sources or open_evidence:
            # A header whose revisit payload is still unresolved is evidence
            # in flight: the capture date may already beat the current best,
            # so neither empty nor selected is safe yet.
            return 'pending'
        if best is not None:
            return 'selected'
        if access_sources:
            # Access control is not proof of absence, so this is not "empty".
            return 'restricted'
        return 'empty'

    def _source_report(self, state, name):
        source = state.sources[name]
        if source.access_denied:
            status = 'access_denied'
        elif source.complete:
            status = 'complete'
        else:
            best = state.best()
            if best is not None and self._boundary_settles(source, state, best):
                status = 'bounded'
            elif source.boundary is not None:
                # A bound was offered but the engine will not rely on it
                # (unattested, untrusted kind, or unconfirmed probe).
                status = 'boundary_untrusted'
            else:
                status = 'unfinished'

        return {'status': status,
                'complete': bool(source.complete),
                'pages_fetched': int(source.fetched),
                'cursor': source.cursor,
                'access_error': source.access_error,
                'boundary': source.boundary.to_dict() if source.boundary else None,
                'settled': source.settled,
                'duplicates': source.duplicates}

    def _candidate_report(self, candidate_state):
        candidate = candidate_state.candidate
        verification = candidate_state.verification
        return {'id': candidate.id,
                'source': candidate.source,
                'order': candidate.order,
                'url': candidate.url,
                'cdx_timestamp': candidate.cdx_timestamp,
                'status': (verification.status if verification else 'unverified'),
                'withdrawn': bool(candidate_state.withdrawn),
                'skipped': candidate_state.skipped,
                'record': candidate.header_ref.to_dict(),
                'true_timestamp': verification.true_timestamp if verification else None,
                'distance': verification.distance if verification else None,
                'body_ref': (verification.body_ref.to_dict()
                             if verification and verification.body_ref else None),
                'body_date': verification.body_date if verification else None,
                'body_kind': verification.body_kind if verification else '',
                'detail': verification.detail if verification else ''}

    def _capture_report(self, candidate_state):
        candidate = candidate_state.candidate
        verification = candidate_state.verification
        return {'id': candidate.id,
                'source': candidate.source,
                'url': candidate.url,
                'timestamp': verification.true_timestamp,
                'cdx_timestamp': candidate.cdx_timestamp,
                'warc_date': verification.warc_date,
                'warc_type': verification.warc_type,
                'distance': verification.distance,
                'header': (verification.header_ref.to_dict()
                           if verification.header_ref else None),
                'body': (verification.body_ref.to_dict()
                         if verification.body_ref else None),
                'body_date': verification.body_date,
                'body_kind': verification.body_kind,
                'order': candidate.order}


# ============================================================================
def withdraw(result_state, candidate_id, reason):
    """Withdraw one candidate from a resumed state.

    The state is updated in place; the caller can then run another round.
    Returns the withdrawn candidate state.
    """
    for candidate in result_state.candidates:
        if candidate.id == candidate_id:
            _do_withdraw(result_state, candidate, reason)
            return candidate
    raise KeyError(candidate_id)


def withdraw_by_record(state, filename, offset, reason):
    for candidate in state.candidates:
        if candidate.candidate.filename == filename and \
                int(candidate.candidate.offset) == int(offset):
            _do_withdraw(state, candidate, reason)
            return candidate
    raise KeyError((filename, offset))


def _do_withdraw(state, candidate, reason):
    candidate.withdrawn = True
    candidate.withdrawn_reason = reason
    if candidate.id not in state.withdrawn:
        state.withdrawn.append(candidate.id)
    # A tombstone survives even if the source is re-paged: a withdrawn
    # candidate must not come back through a later listing.
    state.tombstones[candidate.id] = reason
