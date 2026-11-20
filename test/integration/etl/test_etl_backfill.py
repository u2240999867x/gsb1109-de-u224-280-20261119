"""Tests for resumable append-only backfill using SQLite."""
# pylint: disable=unused-argument, missing-docstring
import sqlite3
from textwrap import dedent

import pytest

import etlhelper as etl
from etlhelper.backfill import EventRange
from etlhelper.exceptions import (
    ETLHelperBackfillError,
    ETLHelperCommitUnconfirmedError,
)

BLOCKS = [
    EventRange(41, 41),
    EventRange(42, 44),
    EventRange(45, 45),
]
WINDOW = dict(start_event_id=41, end_event_id=45)


@pytest.fixture
def backfill_dbs(tmp_path):
    """Source readings and destination archive with state tables."""
    source = sqlite3.connect(tmp_path / 'source.db')
    dest = sqlite3.connect(tmp_path / 'dest.db')
    source.execute(
        'CREATE TABLE readings (event_id INTEGER PRIMARY KEY, value TEXT)'
    )
    dest.execute(dedent("""
        CREATE TABLE archive (
            event_id INTEGER PRIMARY KEY,
            value TEXT CHECK (value != 'bad')
        )
        """).strip())
    _insert_events(source, range(41, 46))
    source.commit()
    dest.commit()
    yield source, dest
    source.close()
    dest.close()


def _insert_events(conn, event_ids):
    for event_id in event_ids:
        value = 'bad' if event_id == 43 else f'v{event_id}'
        conn.execute(
            'INSERT INTO readings (event_id, value) VALUES (?, ?)',
            (event_id, value),
        )


def run_backfill(source, dest, **kwargs):
    options = dict(
        source_table='readings',
        dest_table='archive',
        blocks=BLOCKS,
        **WINDOW,
    )
    options.update(kwargs)
    return etl.backfill('run-41-45', source, dest, **options)


def archived_ids(dest):
    return [row[0] for row in dest.execute(
        'SELECT event_id FROM archive ORDER BY event_id'
    )]


def coverage_rows(dest):
    return list(dest.execute("""
        SELECT block_start, block_end, accepted_count, rejected_count
        FROM etlhelper_backfill_coverage ORDER BY block_start
        """))


class _ConnectionProxy:
    """Present the sqlite3 class while delegating to the real connection."""
    __class__ = sqlite3.Connection

    def __init__(self, conn, fail_at_commit):
        self._conn = conn
        self._fail_at = fail_at_commit
        self._commits = 0
        self.dead = False

    def __getattr__(self, name):
        attribute = getattr(self._conn, name)
        if self.dead and callable(attribute):
            def raise_dead(*args, **kwargs):
                raise sqlite3.OperationalError('connection is dead')
            return raise_dead
        return attribute


class CommitAppliedReplyLost(_ConnectionProxy):
    """Commit lands, but the acknowledgement never reaches the caller."""

    def commit(self):
        self._conn.commit()
        self._commits += 1
        if self._commits == self._fail_at:
            raise sqlite3.OperationalError('reply lost after commit')


class CommitNotApplied(_ConnectionProxy):
    """Connection drops before the block transaction is committed."""

    def commit(self):
        self._commits += 1
        if self._commits == self._fail_at:
            self._commits -= 1
            raise sqlite3.OperationalError('connection lost before commit')
        self._conn.commit()


def _prepare_and_arm(backfill_dbs, proxy_class, fail_at):
    source, dest = backfill_dbs
    etl.create_backfill_tables(dest)
    return source, dest, proxy_class(dest, fail_at_commit=fail_at)


def test_window_records_reject_and_freezes_late_events(backfill_dbs):
    source, dest = backfill_dbs

    result = run_backfill(source, dest)

    assert result.complete
    assert result.coverage == EventRange(41, 45)
    assert result.rejected_event_ids == (43,)
    assert archived_ids(dest) == [41, 42, 44, 45]
    assert etl.rejected_events('run-41-45', dest) == (43,)
    assert coverage_rows(dest) == [
        (41, 41, 1, 0),
        (42, 44, 2, 1),
        (45, 45, 1, 0),
    ]

    # Event 46 arrives after the window was frozen.
    _insert_events(source, [46])
    source.commit()
    rerun = run_backfill(source, dest)

    assert rerun.complete
    assert rerun.accepted == 0
    assert archived_ids(dest) == [41, 42, 44, 45]
    assert etl.rejected_events('run-41-45', dest) == (43,)
    assert len(coverage_rows(dest)) == 3


def test_committed_block_with_lost_reply_is_not_written_again(backfill_dbs):
    # Two commits precede the block: start_run and block 41-41.
    source, dest, proxy = _prepare_and_arm(
        backfill_dbs, CommitAppliedReplyLost, fail_at=3
    )

    first = run_backfill(source, proxy, create_tables=False)

    assert first.coverage == EventRange(41, 44)
    assert first.complete is False
    assert archived_ids(dest) == [41, 42, 44]
    assert coverage_rows(dest)[-1][:2] == (42, 44)

    # Event 46 arrives; the same window is triggered again.
    _insert_events(source, [46])
    source.commit()
    resumed = run_backfill(source, dest)

    assert resumed.complete
    assert resumed.coverage == EventRange(41, 45)
    assert archived_ids(dest) == [41, 42, 44, 45]
    rejects = list(dest.execute("""
        SELECT event_id FROM etlhelper_backfill_reject ORDER BY event_id
        """))
    assert [row[0] for row in rejects] == [43]


def test_uncommitted_block_is_reprocessed_on_resume(backfill_dbs):
    source, dest, proxy = _prepare_and_arm(
        backfill_dbs, CommitNotApplied, fail_at=3
    )

    first = run_backfill(source, proxy, create_tables=False)

    assert first.coverage == EventRange(41, 41)
    assert first.stopped_reason == 'commit_not_applied'
    assert archived_ids(dest) == [41]

    resumed = run_backfill(source, dest)

    assert resumed.complete
    assert resumed.coverage == EventRange(41, 45)
    assert archived_ids(dest) == [41, 42, 44, 45]
    assert coverage_rows(dest) == [
        (41, 41, 1, 0),
        (42, 44, 2, 1),
        (45, 45, 1, 0),
    ]


def test_unconfirmed_reply_raises_without_rewriting(backfill_dbs):
    source, dest = backfill_dbs
    etl.create_backfill_tables(dest)
    proxy = _DeadConnection(dest, fail_at_commit=2)

    with pytest.raises(ETLHelperCommitUnconfirmedError) as excinfo:
        run_backfill(source, proxy, create_tables=False)

    assert excinfo.value.unconfirmed_range == EventRange(41, 41)
    # The write may or may not have reached the destination.  What must never
    # happen is an unverified coverage marker or a silent second write.
    # Inspect from a separate connection so the dead connection's open
    # transaction is not mistaken for durable state.
    path = dest.execute('PRAGMA database_list').fetchone()[2]
    observer = sqlite3.connect(path)
    try:
        assert list(observer.execute(
            'SELECT * FROM etlhelper_backfill_coverage'
        )) == []
        assert list(observer.execute(
            'SELECT event_id FROM archive'
        )) == []
    finally:
        observer.close()


class _DeadConnection(_ConnectionProxy):
    """Every operation on the connection fails once it is declared dead."""

    def commit(self):
        if self.dead:
            raise sqlite3.OperationalError('connection is dead')
        self._commits += 1
        if self._commits == self._fail_at:
            self.dead = True
            raise sqlite3.OperationalError('connection lost before commit')
        self._conn.commit()


def test_unconfirmed_can_be_reported_instead_of_raised(backfill_dbs):
    source, dest = backfill_dbs
    etl.create_backfill_tables(dest)
    proxy = _DeadConnection(dest, fail_at_commit=2)

    result = run_backfill(
        source, proxy, create_tables=False, raise_on_unconfirmed=False
    )

    assert result.unconfirmed == EventRange(41, 41)
    assert result.coverage is None
    assert result.complete is False


def test_missing_event_stops_before_the_gap(backfill_dbs):
    source, dest = backfill_dbs
    source.execute('DELETE FROM readings WHERE event_id = 43')
    source.commit()

    result = run_backfill(source, dest)

    assert result.stopped_reason == 'missing_event_at_boundary'
    assert result.coverage == EventRange(41, 42)
    assert archived_ids(dest) == [41, 42]
    assert coverage_rows(dest) == [
        (41, 41, 1, 0),
        (42, 42, 1, 0),
    ]


def test_coverage_is_not_inferred_from_max_archived_id(backfill_dbs):
    source, dest = backfill_dbs
    result = run_backfill(source, dest)
    assert result.coverage == EventRange(41, 45)

    # A stray high id in the archive must not extend confirmed coverage.
    dest.execute("INSERT INTO archive VALUES (99, 'manual')")
    dest.commit()

    assert etl.confirmed_coverage(
        'run-41-45', dest, **WINDOW
    ) == EventRange(41, 45)


def test_run_window_is_frozen(backfill_dbs):
    source, dest = backfill_dbs
    run_backfill(source, dest)

    with pytest.raises(ETLHelperBackfillError, match='Use a new run_id'):
        etl.backfill(
            'run-41-45', source, dest,
            source_table='readings', dest_table='archive',
            start_event_id=41, end_event_id=46,
        )


def test_blocks_must_partition_window(backfill_dbs):
    source, dest = backfill_dbs

    with pytest.raises(ETLHelperBackfillError, match='contiguous'):
        etl.backfill(
            'run', source, dest,
            source_table='readings', dest_table='archive',
            start_event_id=41, end_event_id=45,
            blocks=[EventRange(41, 41), EventRange(43, 45)],
        )


def test_existing_copy_api_is_unchanged(backfill_dbs):
    source, dest = backfill_dbs
    dest.execute("CREATE TABLE plain (event_id INTEGER PRIMARY KEY, value TEXT CHECK (value != 'bad'))")
    dest.commit()

    processed, failed = etl.copy_rows(
        'SELECT * FROM readings',
        source,
        'INSERT INTO plain (event_id, value) VALUES (?, ?)',
        dest,
        row_factory=etl.row_factories.tuple_row_factory,
        on_error=lambda rows: None,
    )

    assert processed == 5
    assert failed == 1
    assert [row[0] for row in dest.execute(
        'SELECT event_id FROM plain ORDER BY event_id'
    )] == [41, 42, 44, 45]
    tables = {row[0] for row in dest.execute(
        "SELECT name FROM sqlite_master WHERE type = 'table'"
    )}
    assert 'etlhelper_backfill_coverage' not in tables
