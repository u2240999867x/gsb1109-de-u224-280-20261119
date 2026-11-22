"""Paged index sources and the WARC probes used to verify their claims.

A review source exposes a small, cursor-based interface instead of pywb's
streaming ``load_index``: each page returns the candidates the source holds
for that page, plus an optional statement about the time range the source has
not paged yet.  The engine calls ``page()`` at most once per source per round,
so the source itself can never page ahead of the caller's budget.

The default adapter, :class:`IndexSourcePages`, bridges to a normal pywb index
aggregator.  ``showNumPages`` / ``showPagedIndex`` style paging is not
implemented for every pywb index type, so the adapter pages a snapshot of the
URL's captures and turns them into fixed-size pages.  This keeps the engine
tests independent of which underlying index happens to support paging.
"""

from collections import OrderedDict

from pywb.warcserver.index.cdxobject import CDXObject

from .model import Candidate, SourcePage, Boundary, RecordRef


PAGE_SIZE = 2


class SourceError(Exception):
    """An index page could not be read."""


def _page_default(value):
    if isinstance(value, Candidate):
        return [value.url, value.cdx_timestamp, value.filename,
                value.offset, value.length, value.digest, value.status,
                value.mime]
    to_dict = getattr(value, 'to_dict', None)
    if callable(to_dict):
        return to_dict()
    return str(value)


# ============================================================================
class ReviewSource(object):
    """Base class for a source that can be reviewed page by page."""

    name = ''

    def page(self, cursor):
        """Return the page at ``cursor``.

        ``cursor`` is ``0`` for the first page and the ``next_cursor`` of the
        previous page afterwards.  A page must be cheap: the engine treats one
        call as one page turn.
        """
        raise NotImplementedError()

    def snapshot(self):
        """A JSON-serialisable description used to detect changed sources."""
        return {}


# ============================================================================
class IndexSourcePages(ReviewSource):
    """Page a pywb index source for one exact URL.

    ``boundary_kinds`` lists the boundary kinds this source is allowed to
    attest.  A source that only knows CDX ordering can still return a
    boundary, but with ``kind='cdx'`` and ``attested=False``; the engine then
    keeps the range pending rather than skipping the rest of the pages.
    """

    def __init__(self, name, index_source, url, page_size=PAGE_SIZE,
                 boundary_kinds=(), attests_ordering=True, params=None,
                 body_refs=None):
        self.name = name
        self.index_source = index_source
        self.url = url
        self.page_size = max(1, int(page_size))
        self.boundary_kinds = tuple(boundary_kinds) or ('source-order',)
        self.attests_ordering = attests_ordering
        self.params = dict(params or {})
        # CDXJ rows cannot carry a payload pointer that pywb's CDXObject does
        # not know.  A source may therefore supply the physical payload
        # location out of band, keyed by the revisit's digest or by tuple
        # (filename, offset, length) of the revisit record.
        self.body_refs = dict(body_refs or {})
        self._pages = None
        self._signature = None

    def snapshot(self):
        signature = self._source_signature()
        return {'type': 'index',
                'url': self.url,
                'page_size': self.page_size,
                'signature': signature}

    def _source_signature(self):
        # pywb index sources do not share one fingerprint API.  Prefer a
        # caller-supplied signature, then a file mtime/size pair, then the
        # identity of the source object.  The result only controls whether a
        # cached page snapshot may be reused across review rounds.
        signature = self.params.get('_signature')
        if signature is not None:
            return signature

        filename = getattr(self.index_source, 'filename_template', None)
        if isinstance(filename, str) and '://' not in filename:
            import os
            try:
                stat = os.stat(filename)
                return [filename, stat.st_size, stat.st_mtime_ns]
            except OSError:
                return [filename, None]

        return repr(self.index_source)

    def _load_all(self):
        params = dict(self.params)
        params.setdefault('url', self.url)
        params.setdefault('matchType', 'exact')
        params.setdefault('sort', 'closest')

        try:
            cdx_iter, errs = self.index_source(params)
        except Exception as e:
            raise SourceError(str(e))

        if errs and errs.get('last_exc'):
            access = errs.get('access')
            if access and access != 'allow':
                raise SourceError('access denied: ' + str(access))
            raise SourceError(str(errs['last_exc']))

        candidates = []
        for cdx in cdx_iter:
            candidate = self._to_candidate(cdx)
            if candidate:
                candidates.append(candidate)
        return candidates

    def _to_candidate(self, cdx):
        if not isinstance(cdx, CDXObject):
            cdx = CDXObject(cdx)

        filename = cdx.get('filename')
        offset = cdx.get('offset')
        length = cdx.get('length')

        if not filename or filename == '-' or offset in (None, '-') \
                or length in (None, '-'):
            return None

        access = cdx.get('access', 'allow')

        body_ref = None
        key = (filename, int(offset), int(length))
        if key in self.body_refs:
            body_ref = RecordRef.from_dict(self.body_refs[key])

        return Candidate(source=self.name,
                         url=cdx.get('url', self.url),
                         cdx_timestamp=cdx['timestamp'],
                         filename=filename,
                         offset=int(offset),
                         length=int(length),
                         digest=cdx.get('digest', '-'),
                         status=cdx.get('status', '200'),
                         mime=cdx.get('mime', 'text/html'),
                         body_ref=body_ref,
                         access=access)

    def _all(self):
        if self._pages is None:
            self._pages = self._load_all()
            self._signature = self._source_signature()
        return self._pages

    def page(self, cursor):
        candidates = self._all()
        try:
            start = int(cursor or 0)
        except (TypeError, ValueError):
            raise SourceError('invalid cursor: {0!r}'.format(cursor))

        if start < 0:
            raise SourceError('invalid cursor: {0!r}'.format(cursor))

        end = start + self.page_size
        page = candidates[start:end]
        next_cursor = end if end < len(candidates) else None
        complete = next_cursor is None

        boundary = None
        if not complete:
            rest = candidates[end:]
            # The oldest timestamp still to be paged is a bound only if the
            # source's own ordering is meaningful.  Even then it bounds CDX
            # timestamps, not true capture times.
            timestamps = [c.cdx_timestamp for c in rest]
            boundary = Boundary(start=min(timestamps),
                                end=max(timestamps),
                                attested=False,
                                kind='cdx',
                                note='remaining pages, CDX order only')

        return SourcePage(candidates=page,
                          next_cursor=next_cursor,
                          complete=complete,
                          boundary=boundary)


# ============================================================================
class StaticPages(ReviewSource):
    """A source backed by a fixed list of pages, for controlled acceptance.

    ``pages`` is a list of dicts with ``candidates`` and an optional
    ``boundary``.  Each boundary is a :class:`Boundary` or a dict accepted by
    ``Boundary.from_dict``.
    """

    def __init__(self, name, pages, signature=None, access_error=None):
        self.name = name
        self._pages = OrderedDict()
        for index, page in enumerate(pages):
            if not isinstance(page, dict):
                raise TypeError('page must be a dict')
            self._pages[index] = page
        self.access_error = access_error
        self._signature = signature

    def snapshot(self):
        signature = self._signature
        if signature is None:
            signature = self._content_signature()
        return {'type': 'static',
                'pages': len(self._pages),
                'signature': signature}

    def _content_signature(self):
        import hashlib
        import json
        digest = hashlib.sha1()
        for index in sorted(self._pages):
            page = self._pages[index]
            digest.update(str(index).encode('utf-8'))
            digest.update(json.dumps(page, sort_keys=True,
                                     default=_page_default).encode('utf-8'))
        return digest.hexdigest()

    def page(self, cursor):
        try:
            index = int(cursor or 0)
        except (TypeError, ValueError):
            raise SourceError('invalid cursor: {0!r}'.format(cursor))

        if index < 0 or index >= len(self._pages):
            raise SourceError('invalid cursor: {0!r}'.format(cursor))

        data = self._pages[index]
        if data.get('error'):
            raise SourceError(data['error'])
        if data.get('access_error'):
            return SourcePage(access_error=data['access_error'])

        candidates = []
        for item in data.get('candidates', []):
            candidate = item if isinstance(item, Candidate) else Candidate(**item)
            candidate.source = self.name
            candidates.append(candidate)

        boundary = data.get('boundary')
        if boundary is not None and not isinstance(boundary, Boundary):
            boundary = Boundary.from_dict(boundary)

        next_cursor = data.get('next_cursor', index + 1)
        if next_cursor is not None:
            try:
                next_cursor = int(next_cursor)
            except (TypeError, ValueError):
                pass
        complete = bool(data.get('complete', next_cursor is None))

        return SourcePage(candidates=candidates,
                          next_cursor=None if complete else next_cursor,
                          complete=complete,
                          boundary=boundary)
