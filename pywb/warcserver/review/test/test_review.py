"""Acceptance tests for the UTC-day capture review.

These tests build real WARCs and drive the engine round by round with small,
explicit budgets.  Every assertion is about observable state: the candidates
actually verified, the records actually opened, the boundary actually
confirmed, and the capture that would be replayed.
"""

import os

import pytest

from pywb.warcserver.review import (ReviewEngine, StaticPages, WarcVerifier,
                                    withdraw_by_record, ResumeError)
from pywb.warcserver.review.model import (Candidate, RecordRef, Boundary,
                                          day_end)
from pywb.warcserver.review.sources import IndexSourcePages
from pywb.warcserver.review.test.fixtures import build_two_source_fixture


DAY = '20261119'
AT = '20261119120000'


def make_candidate(data):
    body_ref = data.get('body_ref')
    if body_ref:
        body_ref = RecordRef.from_dict(body_ref)
    return Candidate(source=data.get('source', ''),
                     url=data['url'],
                     cdx_timestamp=data['timestamp'],
                     filename=data['filename'],
                     offset=data['offset'],
                     length=data['length'],
                     digest=data.get('digest', '-'),
                     status=data.get('status', '200'),
                     mime=data.get('mime', 'text/html'),
                     body_ref=body_ref,
                     access=data.get('access', 'allow'))


@pytest.fixture(scope='module')
def fixture_warcs(tmp_path_factory):
    root = tmp_path_factory.mktemp('review-warcs')
    data = build_two_source_fixture(root)
    yield data


def make_engine(data, pages_a, pages_b, page_turns=2, warc_opens=2):
    verifier = WarcVerifier(path_resolver=lambda name: os.path.join(data['root'], name))
    sources = [
        StaticPages('a', pages_a),
        StaticPages('b', pages_b),
    ]
    return ReviewEngine(sources, verifier=verifier, url=data['url'],
                        day=DAY, at=AT, page_turns=page_turns,
                        warc_opens=warc_opens)


# ============================================================================
def test_first_round_is_pending_despite_readable_capture(fixture_warcs):
    data = fixture_warcs
    a_readable = make_candidate(data['a']['readable'])
    a_corrupt = make_candidate(data['a']['corrupt'])
    b_revisit = make_candidate(data['b']['revisit'])

    pages_a = [{'candidates': [a_corrupt], 'next_cursor': 1},
               {'candidates': [a_readable], 'complete': True}]
    pages_b = [{'candidates': [b_revisit], 'next_cursor': 1}]

    engine = make_engine(data, pages_a, pages_b, page_turns=2, warc_opens=4)
    result = engine.run(engine.new_state())

    assert result['status'] == 'pending'
    assert result['best'] is not None
    # the readable record was already verified and is replayable
    assert result['best']['timestamp'] in ('20261119080000', '20261119090000')
    assert 'b' in result['pending_sources']
    assert result['resume_token']


def test_broken_offset_does_not_hide_readable_next_page(fixture_warcs):
    data = fixture_warcs
    a_readable = make_candidate(data['a']['readable'])
    a_corrupt = make_candidate(data['a']['corrupt'])

    pages_a = [{'candidates': [a_corrupt], 'next_cursor': 1},
               {'candidates': [a_readable, make_candidate(data['a']['duplicate'])],
                'complete': True}]

    engine = make_engine(data, pages_a, [{'candidates': [], 'complete': True}],
                         page_turns=4, warc_opens=4)
    result = engine.run(engine.new_state())

    assert result['status'] == 'selected'
    assert result['best']['timestamp'] == '20261119080000'
    assert result['sources']['a']['duplicates'] == 1
    statuses = {(c['id']): c['status'] for c in result['candidates']}
    assert 'unreadable' in statuses.values()
    assert 'verified' in statuses.values()


def test_cross_day_candidate_is_rejected_by_record_header(fixture_warcs):
    data = fixture_warcs
    b_next = make_candidate(data['b']['next_day'])

    pages_b = [{'candidates': [b_next], 'complete': True}]
    pages_a = [{'candidates': [], 'complete': True}]

    engine = make_engine(data, pages_a, pages_b, page_turns=2, warc_opens=2)
    result = engine.run(engine.new_state())

    assert result['status'] == 'empty'
    report = result['candidates'][0]
    assert report['status'] == 'wrong-day'
    assert report['true_timestamp'] == '20261120000500'


def test_revisit_uses_own_header_date_and_previous_day_body(fixture_warcs):
    data = fixture_warcs
    revisit = make_candidate(data['b']['revisit'])

    engine = make_engine(data,
                         [{'candidates': [], 'complete': True}],
                         [{'candidates': [revisit], 'complete': True}],
                         page_turns=2, warc_opens=2)
    result = engine.run(engine.new_state())

    assert result['status'] == 'selected'
    best = result['best']
    assert best['timestamp'] == '20261119090000'
    assert best['body_kind'] == 'revisit'
    assert best['body_date'] == '20261118235000'
    # body date is older but capture date stays on the reviewed day
    assert best['timestamp'][:8] == DAY


def test_tie_keeps_enumeration_order(fixture_warcs):
    data = fixture_warcs
    revisit = make_candidate(data['b']['revisit'])
    readable = make_candidate(data['a']['readable'])

    # 08:00 is 4h before noon, 16:00 is 4h after noon
    from warcio.warcwriter import WARCWriter
    from io import BytesIO
    from warcio.statusandheaders import StatusAndHeaders
    late = os.path.join(data['root'], 'c.warc.gz')
    with open(late, 'wb') as fh:
        writer = WARCWriter(fh, gzip=True)
        body = b'tie'
        headers = StatusAndHeaders('200 OK',
                                   [('Content-Type', 'text/html'),
                                    ('Content-Length', str(len(body)))],
                                   protocol='HTTP/1.1')
        writer.write_record(writer.create_warc_record(
            data['url'], 'response', payload=BytesIO(body),
            http_headers=headers,
            warc_headers_dict={'WARC-Date': '2026-11-19T16:00:00Z'}))

    from warcio.archiveiterator import ArchiveIterator
    with open(late, 'rb') as fh:
        it = ArchiveIterator(fh)
        for rec in it:
            offset, length = it.get_record_offset(), it.get_record_length()

    late_candidate = Candidate(source='b', url=data['url'],
                               cdx_timestamp='20261119160000',
                               filename='c.warc.gz', offset=offset,
                               length=length)

    pages_b = [{'candidates': [late_candidate], 'complete': True}]
    pages_a = [{'candidates': [readable], 'complete': True}]

    engine = make_engine(data, pages_a, pages_b, page_turns=2, warc_opens=2)
    result = engine.run(engine.new_state())

    assert result['status'] == 'selected'
    # equal distance: the candidate enumerated first wins
    assert result['best']['order'] < [c['order'] for c in result['candidates']
                                      if c['id'] != result['best']['id']][0]


def test_attested_boundary_settles_with_only_one_probe_open(fixture_warcs):
    data = fixture_warcs
    readable = make_candidate(data['a']['readable'])

    # The rest of source B starts at 17:00, five hours after noon, while the
    # best capture is four hours away: no remaining capture can be nearer.
    probe = RecordRef(data['b']['probe']['filename'],
                      data['b']['probe']['offset'],
                      data['b']['probe']['length'])

    pages_a = [{'candidates': [readable], 'complete': True}]
    pages_b = [{'candidates': [],
                'next_cursor': 1,
                'boundary': {'start': '20261119170000',
                             'end': day_end(DAY),
                             'attested': True,
                             'kind': 'warc-date',
                             'probe': probe.to_dict()}}]

    engine = make_engine(data, pages_a, pages_b, page_turns=2, warc_opens=2)
    result = engine.run(engine.new_state())

    assert result['status'] == 'selected'
    assert result['best']['timestamp'] == '20261119080000'
    assert result['sources']['b']['status'] == 'bounded'
    opened = result['consumed']['warc_opens']
    assert opened == 2  # one body candidate + one boundary probe


def test_unattested_boundary_keeps_range_pending(fixture_warcs):
    data = fixture_warcs
    readable = make_candidate(data['a']['readable'])

    pages_a = [{'candidates': [readable], 'complete': True}]
    pages_b = [{'candidates': [],
                'next_cursor': 1,
                'boundary': {'start': '20261119000000',
                             'end': day_end(DAY),
                             'attested': False,
                             'kind': 'cdx'}}]

    engine = make_engine(data, pages_a, pages_b, page_turns=1, warc_opens=2)
    result = engine.run(engine.new_state())

    assert result['status'] == 'pending'
    assert result['resume_token']
    assert 'b' in result['pending_sources']


def test_access_denied_is_not_reported_as_empty(fixture_warcs):
    data = fixture_warcs
    pages_a = [{'candidates': [], 'complete': True}]
    pages_b = [{'access_error': 'access denied'}]

    engine = make_engine(data, pages_a, pages_b, page_turns=2, warc_opens=2)
    result = engine.run(engine.new_state())

    assert result['status'] == 'restricted'
    assert result['access_denied_sources'] == ['b']
    assert result['sources']['b']['status'] == 'access_denied'


# ============================================================================
def _page(data, *record_names, **extra):
    candidates = [make_candidate(data[part][name])
                  for part, name in (spec.split('.', 1) for spec in record_names)]
    page = {'candidates': candidates}
    page.update(extra)
    return page


def _two_page_sources(fixture_warcs):
    data = fixture_warcs
    pages_a = [
        _page(data, 'a.corrupt', next_cursor=1),
        _page(data, 'a.readable', 'a.duplicate', complete=True),
    ]
    pages_b = [
        _page(data, 'b.revisit', next_cursor=1),
        _page(data, 'b.next_day', complete=True),
    ]
    return pages_a, pages_b


def test_rounds_resume_with_signature_and_budget(fixture_warcs):
    pages_a, pages_b = _two_page_sources(fixture_warcs)
    engine = make_engine(fixture_warcs, pages_a, pages_b,
                         page_turns=1, warc_opens=1)

    round1 = engine.run(engine.new_state())
    assert round1['status'] == 'pending'
    assert round1['consumed']['page_turns'] == 1
    assert round1['consumed']['warc_opens'] <= 1
    token = round1['resume_token']
    assert token

    # Verify the credential round-trips without losing source progress.
    resumed = engine.decode(token)
    before = {name: s.fetched for name, s in resumed.sources.items()}

    round2 = engine.run(token, page_turns=1, warc_opens=1)
    assert round2['consumed']['page_turns'] == 1
    after = {name: s.fetched for name, s in
             engine.decode(round2['resume_token']).sources.items()}
    # no source was re-paged: every page count only grew or stayed equal
    assert all(after[name] >= before[name] for name in before)

    round3 = engine.run(round2['resume_token'], page_turns=10, warc_opens=10)
    assert round3['status'] == 'selected'
    # 09:00 is three hours from noon, nearer than the 08:00 capture
    assert round3['best']['timestamp'] == '20261119090000'
    assert round3['best']['source'] == 'b'


def test_credential_rejects_tampering(fixture_warcs):
    pages_a, pages_b = _two_page_sources(fixture_warcs)
    engine = make_engine(fixture_warcs, pages_a, pages_b)
    token = engine.run(engine.new_state(), page_turns=1)['resume_token']

    with pytest.raises(ResumeError):
        engine.decode(token[:-4] + 'AAAA')


def test_withdrawal_survives_resume_and_other_source_kept(fixture_warcs):
    data = fixture_warcs
    readable = make_candidate(data['a']['readable'])
    revisit = make_candidate(data['b']['revisit'])
    probe = RecordRef(data['b']['probe']['filename'],
                      data['b']['probe']['offset'],
                      data['b']['probe']['length'])

    pages_a = [{'candidates': [readable], 'complete': True}]
    pages_b = [{'candidates': [revisit], 'next_cursor': 1,
                'boundary': {'start': '20261119170000',
                             'end': day_end(DAY),
                             'attested': True,
                             'kind': 'warc-date',
                             'probe': probe.to_dict()}}]

    engine = make_engine(data, pages_a, pages_b, page_turns=2, warc_opens=3)
    round1 = engine.run(engine.new_state())
    assert round1['status'] == 'pending'
    assert round1['best']['timestamp'] == '20261119090000'

    state = engine.decode(round1['resume_token'])
    withdraw_by_record(state, data['b']['revisit']['filename'],
                       data['b']['revisit']['offset'],
                       'old index row withdrawn')

    round2 = engine.run(state)
    assert round2['status'] == 'selected'
    assert round2['best']['timestamp'] == '20261119080000'
    assert round2['best']['source'] == 'a'
    assert round2['withdrawn']

    # b was already fully read before the withdrawal: its bound still holds
    # and its unchanged progress is not re-paged.
    assert round2['sources']['b']['pages_fetched'] == 1
    assert round2['sources']['b']['status'] == 'bounded'


def test_changed_source_is_reread_unchanged_source_is_not(fixture_warcs):
    data = fixture_warcs
    readable = make_candidate(data['a']['readable'])
    next_day = make_candidate(data['b']['next_day'])

    pages_a = [{'candidates': [readable], 'complete': True}]
    pages_b = [{'candidates': [next_day], 'next_cursor': 1}]

    verifier = WarcVerifier(path_resolver=lambda name: os.path.join(data['root'], name))
    source_a = StaticPages('a', pages_a)
    source_b = StaticPages('b', pages_b)
    engine = ReviewEngine([source_a, source_b], verifier=verifier,
                          url=data['url'], day=DAY, at=AT,
                          page_turns=2, warc_opens=2)

    round1 = engine.run(engine.new_state())
    assert round1['status'] == 'pending'
    state = engine.decode(round1['resume_token'])
    assert state.sources['a'].fetched == 1
    assert state.sources['b'].fetched == 1

    # Only source B changes: replace its page set and its signature.
    source_b._pages = {0: {'candidates': [make_candidate(data['b']['revisit'])],
                           'complete': True}}
    changed = engine.invalidate_changed_sources(state)
    assert changed == ['b']
    # source A progress and evidence are preserved
    assert state.sources['a'].fetched == 1
    assert [c for c in state.candidates
            if c.candidate.source == 'a'][0].verification is not None

    round2 = engine.run(state, page_turns=2, warc_opens=2)
    assert round2['status'] == 'selected'
    # The refreshed source B now yields the 09:00 same-day revisit, which is
    # nearer to noon than source A's unchanged 08:00 record.
    assert round2['best']['timestamp'] == '20261119090000'
    assert round2['best']['source'] == 'b'
    assert round2['sources']['a']['pages_fetched'] == 1
    assert round2['sources']['b']['pages_fetched'] == 1


# ============================================================================
def _write_cdxj(path, url, entries):
    import json
    with open(path, 'w') as fh:
        for entry in entries:
            data = dict(entry)
            timestamp = data.pop('timestamp')
            urlkey = data.pop('urlkey', 'com,example)/review')
            data.pop('body_ref', None)
            fh.write('{0} {1} {2}\n'.format(urlkey, timestamp,
                                            json.dumps(data)))
    return path


def test_real_index_source_pages_and_boundary_probe(fixture_warcs):
    from pywb.warcserver.index.aggregator import SimpleAggregator
    from pywb.warcserver.index.indexsource import FileIndexSource

    data = fixture_warcs
    root = data['root']

    _write_cdxj(os.path.join(root, 'a.cdxj'), data['url'],
                [data['a']['readable'], data['a']['corrupt'],
                 data['a']['duplicate']])
    _write_cdxj(os.path.join(root, 'b.cdxj'), data['url'],
                [data['b']['revisit'], data['b']['next_day'],
                 data['b']['probe']])

    source_a = IndexSourcePages(
        'a', SimpleAggregator({'a': FileIndexSource(os.path.join(root, 'a.cdxj'))}),
        data['url'], page_size=1)
    source_b = IndexSourcePages(
        'b', SimpleAggregator({'b': FileIndexSource(os.path.join(root, 'b.cdxj'))}),
        data['url'], page_size=1)

    # A real pywb source only knows CDX ordering; supply a WARC-confirmed
    # bound for source B from the first record of its unpaged range.
    class BoundedSource(IndexSourcePages):
        def page(self, cursor):
            page = super(BoundedSource, self).page(cursor)
            if cursor == 0:
                page.boundary = Boundary(
                    start=data['b']['probe']['timestamp'],
                    end=day_end(DAY),
                    attested=True,
                    kind='warc-date',
                    probe=RecordRef(data['b']['probe']['filename'],
                                    data['b']['probe']['offset'],
                                    data['b']['probe']['length']))
                page.next_cursor = 1
                page.complete = False
            return page

    revisit = data['b']['revisit']
    body_refs = {(revisit['filename'], revisit['offset'], revisit['length']):
                 revisit['body_ref']}

    source_b = BoundedSource(
        'b', SimpleAggregator({'b': FileIndexSource(os.path.join(root, 'b.cdxj'))}),
        data['url'], page_size=1, body_refs=body_refs)

    verifier = WarcVerifier(path_resolver=lambda name: os.path.join(root, name))
    engine = ReviewEngine([source_a, source_b], verifier=verifier,
                          url=data['url'], day=DAY, at=AT,
                          page_turns=4, warc_opens=4)
    result = engine.run(engine.new_state())

    # The round stops at the open budget with source A's last row unverified;
    # it must not claim a decision yet.
    assert result['status'] == 'pending'
    assert result['best']['timestamp'] == '20261119090000'

    result = engine.run(result['resume_token'], page_turns=4, warc_opens=4)
    assert result['status'] == 'selected'
    # 09:00 is nearer to noon than 08:00, and its payload is the previous
    # day's record while its capture date stays on the reviewed day.
    assert result['best']['timestamp'] == '20261119090000'
    assert result['best']['body_kind'] == 'revisit'
    assert result['best']['body_date'] == '20261118235000'
    # The bound already proved no remaining capture could beat the 09:00 one,
    # so source B's remaining pages were never read.
    assert result['sources']['b']['status'] == 'bounded'
    assert result['sources']['b']['pages_fetched'] == 1


# ============================================================================
def test_withdrawn_candidate_stays_withdrawn_after_source_reread(fixture_warcs):
    data = fixture_warcs
    revisit = make_candidate(data['b']['revisit'])
    readable = make_candidate(data['a']['readable'])

    pages_a = [{'candidates': [readable], 'complete': True}]
    pages_b = [{'candidates': [revisit], 'next_cursor': 1}]

    verifier = WarcVerifier(path_resolver=lambda name: os.path.join(data['root'], name))
    source_a = StaticPages('a', pages_a)
    source_b = StaticPages('b', pages_b)
    engine = ReviewEngine([source_a, source_b], verifier=verifier,
                          url=data['url'], day=DAY, at=AT,
                          page_turns=2, warc_opens=2)

    round1 = engine.run(engine.new_state())
    state = engine.decode(round1['resume_token'])
    withdraw_by_record(state, data['b']['revisit']['filename'],
                       data['b']['revisit']['offset'], 'withdrawn by reviewer')

    # Force a re-read of source B; the withdrawn candidate must not resurrect.
    source_b._pages = {0: {'candidates': [make_candidate(data['b']['revisit'])],
                           'complete': True}}
    engine.invalidate_changed_sources(state)
    round2 = engine.run(state, page_turns=2, warc_opens=2)

    assert round2['status'] == 'selected'
    assert round2['best']['source'] == 'a'
    withdrawn_ids = {w['id'] for w in round2['withdrawn']}
    assert withdrawn_ids
    # the re-read source B page must not have re-added the withdrawn row
    assert all(c['id'] not in withdrawn_ids for c in round2['candidates'])


def test_consumed_counts_accumulate_across_rounds(fixture_warcs):
    pages_a, pages_b = _two_page_sources(fixture_warcs)
    engine = make_engine(fixture_warcs, pages_a, pages_b,
                         page_turns=1, warc_opens=1)

    total_pages = total_opens = 0
    token = engine.new_state()
    result = None
    for _ in range(40):
        result = engine.run(token, page_turns=1, warc_opens=1)
        total_pages += result['consumed']['page_turns']
        total_opens += result['consumed']['warc_opens']
        if result['status'] != 'pending':
            break
        token = result['resume_token']

    assert result['status'] == 'selected'
    assert total_pages == sum(source['pages_fetched']
                              for source in result['sources'].values())
    # Every header open plus every revisit payload open is charged.
    expected_opens = 0
    for candidate in result['candidates']:
        if candidate['status'] in ('unverified',):
            continue
        expected_opens += 1
        if candidate['body_kind'] == 'revisit':
            expected_opens += 1
    assert total_opens == expected_opens


def test_empty_requires_complete_coverage(fixture_warcs):
    data = fixture_warcs
    engine = make_engine(data,
                         [{'candidates': [], 'complete': True}],
                         [{'candidates': [], 'complete': True}])
    result = engine.run(engine.new_state())

    assert result['status'] == 'empty'
    assert result['best'] is None
    assert result['pending_sources'] == []
    assert result['access_denied_sources'] == []


def test_zero_budget_never_claims_empty(fixture_warcs):
    data = fixture_warcs
    readable = make_candidate(data['a']['readable'])
    engine = make_engine(data,
                         [{'candidates': [readable], 'complete': True}],
                         [{'candidates': [], 'next_cursor': 1}])

    # No budget at all: nothing may be claimed terminal.
    result = engine.run(engine.new_state(), page_turns=0, warc_opens=0)
    assert result['status'] == 'pending'
    assert result['resume_token']
    assert result['consumed'] == {'page_turns': 0, 'warc_opens': 0}

    # Resume and finish: source B is genuinely exhausted this time.
    result = engine.run(result['resume_token'], page_turns=4, warc_opens=4)
    assert result['status'] == 'selected'
    assert result['best']['timestamp'] == '20261119080000'


def test_untrusted_boundary_is_reported_distinctly(fixture_warcs):
    data = fixture_warcs
    readable = make_candidate(data['a']['readable'])
    engine = make_engine(data,
                         [{'candidates': [readable], 'complete': True}],
                         [{'candidates': [], 'next_cursor': 5,
                           'boundary': {'start': '20261119170000',
                                        'end': day_end(DAY),
                                        'attested': False,
                                        'kind': 'cdx'}}],
                         page_turns=2, warc_opens=2)

    result = engine.run(engine.new_state())
    assert result['status'] == 'pending'
    assert result['sources']['b']['status'] == 'boundary_untrusted'
    assert result['sources']['b']['boundary']['attested'] is False


def test_probe_outside_claimed_range_is_not_used(fixture_warcs):
    data = fixture_warcs
    readable = make_candidate(data['a']['readable'])
    # Probe record is dated 17:00 but the bound claims the range starts 20:00.
    probe = RecordRef(data['b']['probe']['filename'],
                      data['b']['probe']['offset'],
                      data['b']['probe']['length'])

    engine = make_engine(data,
                         [{'candidates': [readable], 'complete': True}],
                         [{'candidates': [], 'next_cursor': 5,
                           'boundary': {'start': '20261119200000',
                                        'end': day_end(DAY),
                                        'attested': True,
                                        'kind': 'warc-date',
                                        'probe': probe.to_dict()}}],
                         page_turns=2, warc_opens=2)

    result = engine.run(engine.new_state())
    assert result['status'] == 'pending'
    assert result['sources']['b']['status'] == 'boundary_untrusted'


def test_boundary_at_exact_best_distance_stays_pending(fixture_warcs):
    data = fixture_warcs
    readable = make_candidate(data['a']['readable'])  # 08:00, four hours early
    probe = RecordRef(data['b']['probe']['filename'],
                      data['b']['probe']['offset'],
                      data['b']['probe']['length'])

    # The unpaged range starts exactly four hours after noon, tying the best
    # capture.  Ties favour the earlier-enumerated candidate, but the tie
    # could still be broken by an even earlier record, so the range is not
    # dismissed.
    engine = make_engine(data,
                         [{'candidates': [readable], 'complete': True}],
                         [{'candidates': [], 'next_cursor': 5,
                           'boundary': {'start': '20261119160000',
                                        'end': day_end(DAY),
                                        'attested': True,
                                        'kind': 'warc-date',
                                        'probe': probe.to_dict()}}],
                         page_turns=2, warc_opens=2)

    result = engine.run(engine.new_state())
    assert result['status'] == 'pending'
    assert result['best']['timestamp'] == '20261119080000'


def test_http_handler_budget_and_resume(fixture_warcs):
    from pywb.warcserver.review import ReviewHandler

    pages_a, pages_b = _two_page_sources(fixture_warcs)
    verifier = WarcVerifier(path_resolver=lambda name:
                            os.path.join(fixture_warcs['root'], name))
    handler = ReviewHandler([StaticPages('a', pages_a),
                             StaticPages('b', pages_b)],
                            verifier, credential_key=b'test-key')

    params = {'url': fixture_warcs['url'], 'day': DAY, 'at': AT,
              'pageTurns': '1', 'warcOpens': '1'}
    headers, result, errs = handler(dict(params, mode='review'))
    assert errs == {}
    assert result['status'] == 'pending'
    assert result['consumed']['page_turns'] == 1
    token = result['resume_token']

    headers, result, errs = handler(dict(params, mode='review',
                                         resumeToken=token,
                                         pageTurns='10', warcOpens='10'))
    assert result['status'] == 'selected'
    assert result['best']['timestamp'] == '20261119090000'


def test_http_handler_rejects_bad_token(fixture_warcs):
    from pywb.warcserver.review import ReviewHandler

    verifier = WarcVerifier(path_resolver=lambda name:
                            os.path.join(fixture_warcs['root'], name))
    handler = ReviewHandler([StaticPages('a', [{'candidates': [],
                                                'complete': True}])],
                            verifier, credential_key=b'test-key')
    headers, result, errs = handler({'mode': 'review',
                                     'url': fixture_warcs['url'],
                                     'day': DAY, 'at': AT,
                                     'resumeToken': 'not-a-token'})
    assert result is None
    assert 'last_exc' in errs


def test_revisit_header_then_body_across_rounds(fixture_warcs):
    data = fixture_warcs
    revisit = make_candidate(data['b']['revisit'])

    verifier = WarcVerifier(path_resolver=lambda name:
                            os.path.join(data['root'], name))
    engine = ReviewEngine(
        [StaticPages('b', [{'candidates': [revisit], 'complete': True}])],
        verifier=verifier, url=data['url'], day=DAY, at=AT,
        page_turns=1, warc_opens=1)

    round1 = engine.run(engine.new_state())
    assert round1['status'] == 'pending'
    report = round1['candidates'][0]
    # the header proved the capture date; only the payload is still pending
    assert report['status'] == 'header-verified'
    assert report['true_timestamp'] == '20261119090000'
    assert round1['best'] is None

    round2 = engine.run(round1['resume_token'], page_turns=1, warc_opens=1)
    assert round2['status'] == 'selected'
    assert round2['best']['timestamp'] == '20261119090000'
    assert round2['best']['body_date'] == '20261118235000'
    assert round2['consumed']['warc_opens'] == 1
