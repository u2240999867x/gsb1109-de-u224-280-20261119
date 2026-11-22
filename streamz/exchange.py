"""Versioned exchange-rate resolution on top of streamz pipelines.

This module implements a small domain layer for quoted prices that must be
resolved against explicitly versioned exchange-rate publications:

* Rate versions are published incrementally.  A version declares the base
  version it builds upon and only mentions the currency pairs it changes.
  Currency pairs it does not mention are inherited from its declared base
  version -- never from any other published version.
* A version may explicitly *revoke* a currency pair.  A revoked pair no
  longer inherits the old value from the base version.
* Version content may arrive split over several messages.  A completeness
  confirmation lists the change ids the version should consist of.  A
  version is only complete once the confirmation and every listed change
  have been received.  An incomplete base version cannot be replaced by
  another, already published, version.
* Replaying a change with different content creates a conflict.  While a
  conflict is unresolved, dependent versions cannot be published even if
  their own content is fully received.
* Duplicate receipts are idempotent and never produce duplicate output.

Quotes are ordinary stream elements naming a version and a currency pair.
Each quote is (re-)evaluated whenever the rate book changes and emits a
:class:`QuoteResult` only when its outcome actually changed.
"""
from __future__ import absolute_import, division, print_function

from collections import namedtuple

from .core import Stream

__all__ = ['REVOKED', 'QuoteResult', 'resolve_quotes']


class _Revoked(object):
    """ Sentinel marking a currency pair revoked in a rate version """
    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = object.__new__(cls)
        return cls._instance

    def __repr__(self):
        return 'REVOKED'

    def __reduce__(self):
        return (_Revoked, ())


REVOKED = _Revoked()


class QuoteResult(namedtuple('QuoteResult',
                             ['quote', 'version', 'currency', 'status',
                              'value', 'source', 'missing', 'conflict'])):
    """ Outcome of resolving one quote against the versioned rate book

    Attributes
    ----------
    quote : hashable
        Identifier of the quote this result belongs to.
    version, currency : str
        The requested rate version and currency pair.
    status : str
        One of ``'resolved'``, ``'pending'``, ``'revoked'``, ``'conflict'``
        or ``'undefined'``.
    value : object or None
        The resolved rate when ``status == 'resolved'``.
    source : str or None
        The version that actually supplied (or revoked) the value, i.e. the
        base version that was effectively followed.
    missing : tuple
        Unresolved prerequisites while ``status == 'pending'``: entries of
        the form ``('unknown-version', v)``, ``('confirmation', v)`` or
        ``('changes', v, (change_id, ...))``.
    conflict : tuple or None
        ``(version, currency, (observed, values, ...))`` while
        ``status == 'conflict'``.
    """
    __slots__ = ()

    def __new__(cls, quote, version, currency, status, value=None,
                source=None, missing=(), conflict=None):
        return super(QuoteResult, cls).__new__(
            cls, quote, version, currency, status, value, source,
            tuple(missing), conflict)


class _VersionState(object):
    """ Book-keeping for a single rate version """

    __slots__ = ('base', 'confirmed', 'expected', 'changes', 'observations')

    def __init__(self):
        self.base = None            # declared base version
        self.confirmed = False      # completeness confirmation received
        self.expected = None        # change ids the confirmation lists
        self.changes = {}           # change id -> (currency, value)
        self.observations = {}      # currency -> set of observed values

    def add_change(self, change_id, currency, value):
        self.changes[change_id] = (currency, value)
        self.observations.setdefault(currency, set()).add(value)

    def confirm(self, base, expected):
        self.base = base
        self.confirmed = True
        self.expected = set(expected)

    def missing(self):
        """ Missing prerequisites, empty tuple once the version is complete """
        if not self.confirmed:
            return (('confirmation',),)
        outstanding = tuple(sorted(self.expected.difference(self.changes)))
        if outstanding:
            return (('changes', outstanding),)
        return ()

    def is_complete(self):
        return not self.missing()

    def conflicts(self):
        """ Currencies observed with more than one distinct value """
        return dict((currency, tuple(sorted(values, key=repr)))
                    for currency, values in self.observations.items()
                    if len(values) > 1)


@Stream.register_api()
class resolve_quotes(Stream):
    """ Resolve quotes against a stream of versioned rate publications

    Parameters
    ----------
    upstream : Stream
        Stream of quotes.  Each quote is a mapping with keys ``id``,
        ``version`` and ``currency``, or a ``(id, version, currency)``
        tuple.  The id allows a quote to be tracked while it waits for its
        rate version to become fully available.
    events : Stream
        Stream of rate-book events, each a mapping of one of these forms::

            {'type': 'change', 'id': ..., 'version': ..., 'currency': ...,
             'value': ...}
            {'type': 'revoke', 'id': ..., 'version': ..., 'currency': ...}
            {'type': 'confirm', 'version': ..., 'base': ...,
             'changes': [...]}

        ``confirm`` declares the base version and lists every change id the
        version should consist of; content may arrive split over several
        messages and in any order relative to the confirmation.

    Every quote emits a :class:`QuoteResult` whenever its resolution
    changes.  Duplicate receipts never produce duplicate output.

    Examples
    --------
    >>> events = Stream()
    >>> quotes = Stream()
    >>> results = quotes.resolve_quotes(events)
    >>> results.sink(print)  # doctest: +SKIP
    """
    _graphviz_shape = 'doubleoctagon'

    def __init__(self, upstream, events, **kwargs):
        self.events = events
        self.versions = {}
        self.quotes = {}
        self.last = {}
        Stream.__init__(self, upstreams=[upstream, events], **kwargs)

    def _version(self, name):
        if name not in self.versions:
            self.versions[name] = _VersionState()
        return self.versions[name]

    def _apply_event(self, event):
        kind = event['type']
        state = self._version(event['version'])
        if kind == 'confirm':
            state.confirm(event.get('base'), event.get('changes', ()))
        elif kind == 'change':
            state.add_change(event['id'], event['currency'], event['value'])
        elif kind == 'revoke':
            state.add_change(event['id'], event['currency'], REVOKED)
        else:
            raise ValueError('unknown rate event type: %r' % (kind,))

    @staticmethod
    def _parse_quote(x):
        if isinstance(x, dict):
            return x['id'], x['version'], x['currency']
        quote_id, version, currency = x
        return quote_id, version, currency

    def _resolve(self, quote_id, version, currency):
        # Phase 1: the requested version is only publishable once its whole
        # base chain is fully received and free of conflicts.  An incomplete
        # base version is never substituted by another published version.
        seen = set()
        current = version
        while current is not None:
            if current in seen:
                return QuoteResult(quote_id, version, currency, 'pending',
                                   missing=(('cycle', current),))
            seen.add(current)
            state = self.versions.get(current)
            if state is None:
                return QuoteResult(quote_id, version, currency, 'pending',
                                   missing=(('unknown-version', current),))
            conflicts = state.conflicts()
            if conflicts:
                cur, values = sorted(conflicts.items())[0]
                return QuoteResult(quote_id, version, currency, 'conflict',
                                   conflict=(current, cur, values))
            missing = state.missing()
            if missing:
                entries = tuple(entry[:1] + (current,) + entry[1:]
                                for entry in missing)
                return QuoteResult(quote_id, version, currency, 'pending',
                                   missing=entries)
            current = state.base
        # Phase 2: walk the declared base chain; a version only contributes
        # the pairs it explicitly mentions, revocations included.
        current = version
        while current is not None:
            state = self.versions[current]
            values = state.observations.get(currency)
            if values:
                value = next(iter(values))
                if value is REVOKED:
                    return QuoteResult(quote_id, version, currency,
                                       'revoked', source=current)
                return QuoteResult(quote_id, version, currency, 'resolved',
                                   value=value, source=current)
            current = state.base
        return QuoteResult(quote_id, version, currency, 'undefined')

    def update(self, x, who=None, metadata=None):
        self._retain_refs(metadata)
        if who is self.events:
            self._apply_event(x)
            pending = list(self.quotes)
        else:
            quote_id, version, currency = self._parse_quote(x)
            self.quotes[quote_id] = (version, currency)
            pending = [quote_id]
        L = []
        for quote_id in pending:
            version, currency = self.quotes[quote_id]
            result = self._resolve(quote_id, version, currency)
            if self.last.get(quote_id) != result:
                self.last[quote_id] = result
                y = self._emit(result, metadata=metadata)
                if type(y) is list:
                    L.extend(y)
                else:
                    L.append(y)
        return L
