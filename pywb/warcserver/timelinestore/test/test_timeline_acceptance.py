"""Acceptance tests for the disk-backed, versioned capture timeline.

The scenario mirrors the brief exactly:

* two archives answer out of order in controlled pages;
* the second archive's last segment breaks in round 0, but everything
  verified stays pageable and its coverage is reported *partial*;
* a source that finishes with zero captures is *complete*, not failed;
* a later round corrects a same-second link and adds an earlier capture, one
  of whose links is shared with the first archive;
* a late response from the superseded round is rejected and cannot
  resurrect the corrected link;
* old version pages stay readable while refreshed pages show the correction;
* no code path ever holds more than two request pages in memory.
"""

import json
import os
import shutil

import pytest

from pywb.warcserver.timelinestore import (
    TimelineStore,
    SourceStatus,
    StaleRoundError,
    VersionReader,
)


PAGE_SIZE = 3

# UTC seconds (14-digit CDX-style timestamps)
T_EARLY = '20200101000000'
T_ZERO = '20200102000000'
T_OB = '20200103000000'
T_ONE = '20200104000000'
T_TWO = '20200105000000'

ARCHIVE_A = 'archive-a'
ARCHIVE_B = 'archive-b'

M_SHARED = 'http://memento.example/shared'
M_EARLY = 'http://memento.example/early'
M_A = 'http://memento.example/a-only'
M_A2 = 'http://memento.example/a-two'
M_A0 = 'http://memento.example/a-zero'
M_B1 = 'http://memento.example/b-wrong'
M_B2 = 'http://memento.example/b-corrected'
M_BROKEN = 'http://memento.example/from-broken-page'


class ScriptedArchive(object):
    """Deterministic, controllable source of paged responses per round."""

    def __init__(self, source_id):
        self.source_id = source_id
        self.rounds = {}

    def add_page(self, round_id, page_index, mementos, page_complete=True,
                 more_pages=False):
        self.rounds.setdefault(round_id, {})[page_index] = {
            'mementos': list(mementos),
            'page_complete': page_complete,
            'more_pages': more_pages,
        }

    def deliver(self, session, round_id, page_index):
        page = self.rounds[round_id][page_index]
        session.ingest_page(
            self.source_id, round_id, page_index,
            iter(page['mementos']),
            page_complete=page['page_complete'],
            more_pages=page['more_pages'])


@pytest.fixture
def store(tmp_path):
    store = TimelineStore(str(tmp_path / 'timeline'),
                          target_uri='http://example.com/',
                          page_size=PAGE_SIZE)
    return store


def _entry_map(page):
    return {(entry['timestamp'], entry['memento_url']): entry
            for entry in page.entries}


def _resolve_original(store_root, provenance_item, timestamp, memento_url):
    """Follow a provenance pointer back to the untouched source original."""
    path = provenance_item['original']['path']
    assert os.path.isabs(path) or os.path.exists(path)
    with open(path, encoding='utf-8') as fh:
        original = json.load(fh)
    assert original['source_id'] == provenance_item['source_id']
    assert [timestamp, memento_url] in original['mementos']
    return original


def _build_round_zero(store):
    """Interleaved round-0 responses; B's last segment breaks."""
    archive_a = ScriptedArchive(ARCHIVE_A)
    archive_b = ScriptedArchive(ARCHIVE_B)

    # A answers in two pages, out of order relative to B, and finishes.
    archive_a.add_page(0, 0, [(T_TWO, M_SHARED), (T_ONE, M_A)],
                       more_pages=True)
    archive_a.add_page(0, 1, [(T_OB, M_A2), (T_ZERO, M_A0),
                              (T_EARLY, M_EARLY)], more_pages=False)

    # B verifies one page (a shared link plus its own same-second link), then
    # its final segment breaks. The broken page carries junk that must never
    # reach the timeline.
    archive_b.add_page(0, 0, [(T_TWO, M_SHARED), (T_ONE, M_B1)],
                       more_pages=True)
    archive_b.add_page(0, 1, [(T_ZERO, M_BROKEN)],
                       page_complete=False, more_pages=True)

    session = store.new_session()
    # explicitly interleave the two archives' pages
    archive_a.deliver(session, 0, 0)
    archive_b.deliver(session, 0, 0)
    archive_b.deliver(session, 0, 1)  # broken segment
    archive_a.deliver(session, 0, 1)
    return session, archive_a, archive_b


def test_round_one_partial_keeps_verified_pageable(store):
    session, _, _ = _build_round_zero(store)
    v1 = session.publish('v1')

    assert v1.coverage() == {
        ARCHIVE_A: SourceStatus.COMPLETE,
        ARCHIVE_B: SourceStatus.PARTIAL,
    }
    assert v1.is_complete() is False

    reader = VersionReader(store, v1)
    first = reader.first_page(PAGE_SIZE)

    # acceptance: the first screen opens immediately and is a full page
    assert first.page_index == 0
    assert first.page_size == PAGE_SIZE
    first_map = _entry_map(first)
    assert list(first_map.keys()) == [
        (T_TWO, M_SHARED),
        (T_ONE, M_A),
        (T_ONE, M_B1),
    ]
    # the broken page's unverified junk is nowhere to be seen
    assert all(url != M_BROKEN for _, url in first_map)

    # two distinct real memento addresses share one UTC second, both open
    same_second = [key for key in first_map if key[0] == T_ONE]
    assert sorted(url for _, url in same_second) == sorted([M_A, M_B1])

    # the shared link is backed by both archives
    shared = first_map[(T_TWO, M_SHARED)]
    assert shared['sources'] == [ARCHIVE_A, ARCHIVE_B]
    for item in shared['provenance']:
        _resolve_original(store.root_dir, item, T_TWO, M_SHARED)

    # continuing on the old entry shows the remaining full old page
    second = reader.page(1, PAGE_SIZE)
    assert second.page_size == PAGE_SIZE
    assert second.has_next is False
    assert sorted((e['timestamp'], e['memento_url'])
                  for e in second.entries) == [
        (T_EARLY, M_EARLY),
        (T_ZERO, M_A0),
        (T_OB, M_A2),
    ]
    assert second.coverage[ARCHIVE_B] == SourceStatus.PARTIAL


def test_later_round_correction_and_stale_rejection(store):
    session, _, archive_b = _build_round_zero(store)
    v1 = session.publish('v1')

    # snapshot the on-disk layout before the second round
    files_before = set()
    for root, _, names in os.walk(store.root_dir):
        for name in names:
            files_before.add(os.path.relpath(os.path.join(root, name),
                                             store.root_dir))

    # B re-requests (round 1): corrects the same-second link, re-reports the
    # shared link, and brings an earlier capture shared with A.
    archive_b.add_page(1, 0, [(T_TWO, M_SHARED), (T_ONE, M_B2)],
                       more_pages=True)
    archive_b.add_page(1, 1, [(T_EARLY, M_EARLY)], more_pages=False)
    archive_b.deliver(session, 1, 0)
    archive_b.deliver(session, 1, 1)
    assert store.source_state(ARCHIVE_B)['status'] == SourceStatus.COMPLETE

    # A late response from the superseded round 0 claims the corrected link
    # still exists: it must be rejected, not applied.
    stale = ScriptedArchive(ARCHIVE_B)
    stale.add_page(0, 9, [(T_ONE, M_B1)], more_pages=True)
    with pytest.raises(StaleRoundError):
        stale.deliver(session, 0, 9)

    v2 = session.publish('v2')
    assert v2.coverage() == {
        ARCHIVE_A: SourceStatus.COMPLETE,
        ARCHIVE_B: SourceStatus.COMPLETE,
    }
    assert v2.is_complete() is True

    # old version stays exactly as it was, including B's partial coverage
    old_reader = VersionReader(store, 'v1')
    old_first = old_reader.first_page(PAGE_SIZE)
    old_map = _entry_map(old_first)
    assert (T_ONE, M_B1) in old_map
    assert (T_ONE, M_B2) not in old_map
    assert old_first.coverage[ARCHIVE_B] == SourceStatus.PARTIAL
    old_second = old_reader.page(1, PAGE_SIZE)
    assert old_second.page_size == PAGE_SIZE

    # refreshed pages show the correction, both sources and full coverage
    new_reader = VersionReader(store, v2)
    new_first = new_reader.first_page(PAGE_SIZE)
    new_map = _entry_map(new_first)
    assert (T_ONE, M_B2) in new_map
    assert (T_ONE, M_B1) not in new_map
    assert (T_ONE, M_A) in new_map  # A's record survives B's correction
    assert new_first.coverage == v2.coverage()

    new_second = new_reader.page(1, PAGE_SIZE)
    second_map = _entry_map(new_second)
    early = second_map[(T_EARLY, M_EARLY)]
    # earlier capture now correctly attributed to both archives
    assert early['sources'] == [ARCHIVE_A, ARCHIVE_B]
    for item in early['provenance']:
        _resolve_original(store.root_dir, item, T_EARLY, M_EARLY)

    # corrected same-second pair still offers two openable real addresses
    corrected_pair = sorted(url for (ts, url) in new_map if ts == T_ONE)
    assert corrected_pair == sorted([M_A, M_B2])
    for url in corrected_pair:
        entry = new_map[(T_ONE, url)]
        assert entry['provenance'], url
        _resolve_original(store.root_dir, entry['provenance'][0],
                          T_ONE, url)

    # shared link still has both archives after B's solo retractions
    assert new_map[(T_TWO, M_SHARED)]['sources'] == [
        ARCHIVE_A, ARCHIVE_B]

    # opening versions creates no snapshot/copy files
    files_after = set()
    for root, _, names in os.walk(store.root_dir):
        for name in names:
            files_after.add(os.path.relpath(os.path.join(root, name),
                                            store.root_dir))
    new_files = files_after - files_before
    assert all(name.startswith('runs') or name.startswith('versions')
               or name.startswith('sources') or name.startswith('evidence')
               or name.startswith('meta') or name == 'journal.jsonl'
               for name in new_files)
    # version descriptors never embed a capture list
    for version in store.versions():
        descriptor = json.load(open(os.path.join(
            store.versions_dir, version.version_id + '.json')))
        assert 'entries' not in descriptor


def test_empty_completed_source_is_not_a_failure(store):
    archive_c = ScriptedArchive('archive-c')
    archive_c.add_page(0, 0, [], more_pages=False)
    session = store.new_session()
    archive_c.deliver(session, 0, 0)
    version = session.publish()

    assert version.coverage() == {'archive-c': SourceStatus.COMPLETE}
    page = VersionReader(store, version).empty_page()
    assert page.entries == []
    assert page.complete is True
    assert page.coverage == {'archive-c': SourceStatus.COMPLETE}


def test_memory_never_exceeds_two_pages_at_scale(tmp_path):
    page_size = 5
    big = TimelineStore(str(tmp_path / 'big'),
                        target_uri='http://example.com/',
                        page_size=page_size)
    session = big.new_session()

    # 250 captures per source, far more than two pages; same-second dupes and
    # cross-source shared links included
    archive_a = ScriptedArchive(ARCHIVE_A)
    archive_b = ScriptedArchive(ARCHIVE_B)
    rows_a = []
    rows_b = []
    for index in range(250):
        ts = '2020{0:010d}'.format(index + 1)
        rows_a.append((ts, 'http://memento.example/a-{0}'.format(index)))
        rows_b.append((ts, 'http://memento.example/b-{0}'.format(index)))
    # every tenth memento is reported by both archives
    for index in range(0, 250, 10):
        rows_b[index] = (rows_a[index][0], rows_a[index][1])
    # two distinct addresses share one UTC second
    rows_b.append((rows_a[0][0], 'http://memento.example/same-second'))

    def feed(archive, rows):
        total = len(rows)
        for start in range(0, total, page_size):
            index = start // page_size
            archive.add_page(0, index, rows[start:start + page_size],
                             more_pages=start + page_size < total)
            archive.deliver(session, 0, index)

    feed(archive_a, rows_a)
    feed(archive_b, rows_b)
    version = session.publish()

    reader = VersionReader(big, version)
    pages = list(reader.iter_pages(page_size))
    expected_unique = 250 + (250 - 25) + 1  # A + B-unique + same-second extra
    seen = sum(page.page_size for page in pages)
    assert seen == expected_unique
    for page in pages:
        assert page.page_size <= page_size

    # two output/look-ahead pages plus at most one merge head per run
    run_count = len(version.runs)
    assert big.budget.peak_items <= 2 * page_size + run_count
    # and the peak is dramatically below the capture count: no full copy
    assert big.budget.peak_items < expected_unique // 10


def test_journal_and_originals_hold_only_facts(store):
    session, _, archive_b = _build_round_zero(store)
    session.publish('v1')
    archive_b.add_page(1, 0, [(T_ONE, M_B2)], more_pages=False)
    archive_b.deliver(session, 1, 0)
    session.publish('v2')

    ops = list(store.journal_entries())
    # every journal line is an actual add/delete fact
    assert {op['op'] for op in ops} <= {
        'add', 'retract', 'retract-source'}
    # the corrected link's retraction really was recorded
    assert any(op['op'] == 'retract'
               and op['memento_url'] == M_B1 for op in ops)
    # shared link lost only B's attribution, the memento itself survived
    assert any(op['op'] == 'retract-source'
               and op['memento_url'] == M_SHARED for op in ops)

    # source originals remain on disk and every URI/timestamp is traceable
    originals = list(store.original_pages(ARCHIVE_B))
    flattened = [tuple(item) for page in originals
                 for item in page['mementos']]
    assert (T_ONE, M_B1) in flattened
    assert (T_ONE, M_B2) in flattened
    # the broken round-0 page was stored as an original, marked incomplete
    broken = [page for page in originals if not page['page_complete']]
    assert broken and broken[0]['round'] == 0
