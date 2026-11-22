"""Unit tests for timeline store edge cases."""

import pytest

from pywb.warcserver.timelinestore import (
    TimelineStore,
    MergeClosedError,
    VersionReader,
)
from pywb.warcserver.timelinestore.budget import MemoryBudget, BoundedList


def test_closed_session_rejects_more_pages(store=None):
    import tempfile
    from pywb.warcserver.timelinestore import TimelineStore, MergeClosedError
    with tempfile.TemporaryDirectory() as directory:
        store = TimelineStore(directory + '/tl', 'http://example.com/', 4)
        session = store.new_session()
        session.close()
        with pytest.raises(MergeClosedError):
            session.ingest_page(
                'a', 0, 0, [('20200101000000', 'http://x/1')], True, False)


def test_duplicate_version_id_rejected(tmp_path):
    store = TimelineStore(str(tmp_path / 'tl'), 'http://example.com/', 4)
    session = store.new_session()
    session.ingest_page('a', 0, 0,
                        [('20200101000000', 'http://x/1')], True, False)
    session.publish('snapshot')
    with pytest.raises(ValueError):
        session.publish('snapshot')


def test_invalid_page_size(tmp_path):
    store = TimelineStore(str(tmp_path / 'tl'), 'http://example.com/', 4)
    session = store.new_session()
    version = session.publish()
    with pytest.raises(ValueError):
        list(VersionReader(store, version).iter_pages(0))


def test_empty_timeline_first_page_is_none_but_empty_page_reports_status(tmp_path):
    store = TimelineStore(str(tmp_path / 'tl'), 'http://example.com/', 4)
    session = store.new_session()
    # a source that finished with no captures
    session.ingest_page('a', 0, 0, [], True, False)
    version = session.publish()

    reader = VersionReader(store, version)
    assert reader.first_page(4) is None
    empty = reader.empty_page()
    assert empty.entries == []
    assert empty.complete is True
    assert empty.coverage == {'a': 'complete'}


def test_budget_tracks_bounded_list_lifecycle():
    budget = MemoryBudget(page_size=10)
    buffer_ = BoundedList(budget)
    buffer_.extend([1, 2, 3])
    assert budget.current_items == 3
    assert budget.peak_items == 3
    buffer_.pop()
    assert budget.current_items == 2
    taken = buffer_.take_items()
    assert taken == [1, 2]
    assert budget.current_items == 0
    buffer_.append(9)
    buffer_.release_all()
    assert budget.current_items == 0


def test_correction_across_three_runs_keeps_immutable_versions(tmp_path):
    store = TimelineStore(str(tmp_path / 'tl'), 'http://example.com/', 2)
    session = store.new_session()
    url = 'http://x/m'

    session.ingest_page('b', 0, 0,
                        [('20200101000000', url)], True, False)
    v1 = session.publish()

    # round 1: B retracts, then re-adds the same URL
    session.ingest_page('b', 1, 0,
                        [('20200101000000', url)], True, False)
    v2 = session.publish()

    # round 2: B retracts again and finishes with nothing
    session.ingest_page('b', 2, 0, [], True, False)
    v3 = session.publish()

    keys = lambda page: {(e['timestamp'], e['memento_url'])
                         for e in page.entries}
    assert keys(VersionReader(store, v1).first_page(2)) == {
        ('20200101000000', url)}
    assert keys(VersionReader(store, v2).first_page(2)) == {
        ('20200101000000', url)}
    assert VersionReader(store, v3).first_page(2) is None
    # each version only sees its own immutable run prefix
    assert len(v1.runs) == 1 and len(v2.runs) == 2 and len(v3.runs) == 3
