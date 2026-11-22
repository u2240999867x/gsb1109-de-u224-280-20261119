from __future__ import absolute_import, division, print_function

from streamz import Stream
from streamz.exchange import QuoteResult, REVOKED


def make_pipeline():
    events = Stream()
    quotes = Stream()
    results = quotes.resolve_quotes(events)
    return events, quotes, results.sink_to_list()


def publish_v1(events):
    events.emit({'type': 'change', 'id': 'v1-usd', 'version': 'v1',
                 'currency': 'USD', 'value': 1.10})
    events.emit({'type': 'change', 'id': 'v1-eur', 'version': 'v1',
                 'currency': 'EUR', 'value': 0.90})
    events.emit({'type': 'confirm', 'version': 'v1', 'base': None,
                 'changes': ['v1-usd', 'v1-eur']})


def test_quote_on_published_version():
    events, quotes, L = make_pipeline()
    publish_v1(events)
    assert L == []

    quotes.emit({'id': 'q1', 'version': 'v1', 'currency': 'EUR'})
    assert L == [QuoteResult('q1', 'v1', 'EUR', 'resolved',
                             value=0.90, source='v1')]


def test_quotes_wait_for_incomplete_versions():
    events, quotes, L = make_pipeline()
    publish_v1(events)

    quotes.emit(('q2', 'v3', 'USD'))
    quotes.emit(('q3', 'v3', 'EUR'))
    assert [r.status for r in L] == ['pending', 'pending']
    assert L[0].missing == (('unknown-version', 'v3'),)

    # v3 is complete in itself but its declared base v2 is missing
    events.emit({'type': 'confirm', 'version': 'v3', 'base': 'v2',
                 'changes': ['v3-gbp']})
    events.emit({'type': 'change', 'id': 'v3-gbp', 'version': 'v3',
                 'currency': 'GBP', 'value': 1.50})
    assert L[-2:] == [QuoteResult('q2', 'v3', 'USD', 'pending',
                                  missing=(('unknown-version', 'v2'),)),
                      QuoteResult('q3', 'v3', 'EUR', 'pending',
                                  missing=(('unknown-version', 'v2'),))]

    # v2 content arrives split over several messages; the completeness
    # confirmation arrives before the revocation it lists
    events.emit({'type': 'change', 'id': 'v2-usd', 'version': 'v2',
                 'currency': 'USD', 'value': 1.25})
    assert L[-2:] == [QuoteResult('q2', 'v3', 'USD', 'pending',
                                  missing=(('confirmation', 'v2'),)),
                      QuoteResult('q3', 'v3', 'EUR', 'pending',
                                  missing=(('confirmation', 'v2'),))]
    events.emit({'type': 'confirm', 'version': 'v2', 'base': 'v1',
                 'changes': ['v2-usd', 'v2-eur']})
    assert L[-2:] == [QuoteResult('q2', 'v3', 'USD', 'pending',
                                  missing=(('changes', 'v2', ('v2-eur',)),)),
                      QuoteResult('q3', 'v3', 'EUR', 'pending',
                                  missing=(('changes', 'v2', ('v2-eur',)),))]

    # v1 quotes are unaffected by the incomplete v2
    quotes.emit(('q4', 'v1', 'USD'))
    assert L[-1] == QuoteResult('q4', 'v1', 'USD', 'resolved',
                                value=1.10, source='v1')

    # the revocation completes v2; v3 becomes publishable
    events.emit({'type': 'revoke', 'id': 'v2-eur', 'version': 'v2',
                 'currency': 'EUR'})
    assert L[-2:] == [QuoteResult('q2', 'v3', 'USD', 'resolved',
                                  value=1.25, source='v2'),
                      QuoteResult('q3', 'v3', 'EUR', 'revoked',
                                  source='v2')]


def test_revocation_does_not_fall_back_to_base():
    events, quotes, L = make_pipeline()
    publish_v1(events)
    events.emit({'type': 'revoke', 'id': 'v2-eur', 'version': 'v2',
                 'currency': 'EUR'})
    events.emit({'type': 'confirm', 'version': 'v2', 'base': 'v1',
                 'changes': ['v2-eur']})
    quotes.emit(('q', 'v2', 'EUR'))
    assert L == [QuoteResult('q', 'v2', 'EUR', 'revoked', source='v2')]


def test_conflict_blocks_dependent_versions():
    events, quotes, L = make_pipeline()
    publish_v1(events)
    events.emit({'type': 'change', 'id': 'v2-usd', 'version': 'v2',
                 'currency': 'USD', 'value': 1.25})
    events.emit({'type': 'revoke', 'id': 'v2-eur', 'version': 'v2',
                 'currency': 'EUR'})
    events.emit({'type': 'confirm', 'version': 'v2', 'base': 'v1',
                 'changes': ['v2-usd', 'v2-eur']})
    events.emit({'type': 'confirm', 'version': 'v3', 'base': 'v2',
                 'changes': ['v3-gbp']})
    events.emit({'type': 'change', 'id': 'v3-gbp', 'version': 'v3',
                 'currency': 'GBP', 'value': 1.50})
    quotes.emit(('q2', 'v3', 'USD'))
    quotes.emit(('q5', 'v3', 'GBP'))
    assert [r.status for r in L] == ['resolved', 'resolved']

    # replaying conflicting v2 USD changes: an identical replay is a no-op,
    # a diverging one opens a conflict
    n = len(L)
    events.emit({'type': 'change', 'id': 'v2-usd', 'version': 'v2',
                 'currency': 'USD', 'value': 1.25})
    assert len(L) == n
    events.emit({'type': 'change', 'id': 'v2-usd-bis', 'version': 'v2',
                 'currency': 'USD', 'value': 1.30})
    # v3 cannot publish while the conflict is unresolved, even though its
    # own content is fully received
    assert L[-2:] == [QuoteResult('q2', 'v3', 'USD', 'conflict',
                                  conflict=('v2', 'USD', (1.25, 1.30))),
                      QuoteResult('q5', 'v3', 'GBP', 'conflict',
                                  conflict=('v2', 'USD', (1.25, 1.30)))]

    # v4 declares v1 as its base and resolves independently of the conflict
    events.emit({'type': 'confirm', 'version': 'v4', 'base': 'v1',
                 'changes': ['v4-chf']})
    events.emit({'type': 'change', 'id': 'v4-chf', 'version': 'v4',
                 'currency': 'CHF', 'value': 1.05})
    quotes.emit(('q6', 'v4', 'USD'))
    quotes.emit(('q7', 'v4', 'CHF'))
    assert L[-2:] == [QuoteResult('q6', 'v4', 'USD', 'resolved',
                                  value=1.10, source='v1'),
                      QuoteResult('q7', 'v4', 'CHF', 'resolved',
                                  value=1.05, source='v4')]


def test_duplicate_receipts_do_not_reemit():
    events, quotes, L = make_pipeline()
    publish_v1(events)
    quotes.emit(('q1', 'v1', 'EUR'))
    assert len(L) == 1

    publish_v1(events)
    quotes.emit(('q1', 'v1', 'EUR'))
    assert len(L) == 1
    assert L[0].status == 'resolved'
    assert L[0].value == 0.90


def test_quote_result_reports_source_version():
    events, quotes, L = make_pipeline()
    publish_v1(events)
    events.emit({'type': 'change', 'id': 'v2-usd', 'version': 'v2',
                 'currency': 'USD', 'value': 1.25})
    events.emit({'type': 'confirm', 'version': 'v2', 'base': 'v1',
                 'changes': ['v2-usd']})
    quotes.emit(('a', 'v2', 'USD'))
    quotes.emit(('b', 'v2', 'EUR'))
    assert L[0].source == 'v2'
    assert L[1] == QuoteResult('b', 'v2', 'EUR', 'resolved',
                               value=0.90, source='v1')


def test_undefined_currency():
    events, quotes, L = make_pipeline()
    publish_v1(events)
    quotes.emit(('q', 'v1', 'JPY'))
    assert L == [QuoteResult('q', 'v1', 'JPY', 'undefined')]


def test_revoked_sentinel_singleton():
    from streamz.exchange import _Revoked
    assert REVOKED is _Revoked()
    assert repr(REVOKED) == 'REVOKED'


def test_combine_latest_unchanged():
    # the plain combine_latest latest-value behaviour is untouched
    a = Stream()
    b = Stream()
    L = a.combine_latest(b).sink_to_list()
    a.emit(1)
    b.emit(2)
    a.emit(3)
    assert L == [(1, 2), (3, 2)]
