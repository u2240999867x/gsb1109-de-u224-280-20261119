"""
Tests for transaction handling in executemany.

These use a controllable SQLite-backed helper to reproduce two driver
behaviours:

1. A fast batch accepts the first row, then fails with MemoryError; the
   ordinary batch then meets a row that violates a constraint.
2. The server marks the transaction as failed, so a savepoint rollback is
   required before any further SQL can run.

The connection belongs to the caller, who has already made a pending change
that must survive an in-transaction recovery.
"""
# pylint: disable=missing-docstring
import sqlite3

import pytest

import etlhelper.etl as etlhelper_etl
from etlhelper import executemany
from etlhelper.db_helper_factory import DB_HELPER_FACTORY
from etlhelper.exceptions import ETLHelperInsertError


ROW_A = {"id": 1, "value": "A"}
ROW_B = {"id": 1, "value": "B"}  # duplicate primary key
ROW_C = {"id": 3, "value": "C"}
ROWS = [ROW_A, ROW_B, ROW_C]

INSERT_SQL = "INSERT INTO observations (id, value) VALUES (:id, :value)"


@pytest.fixture()
def conn():
    """In-memory database with observations, audit trigger and site table."""
    conn = sqlite3.connect(":memory:")
    conn.executescript(
        """
        CREATE TABLE observations (id INTEGER PRIMARY KEY, value TEXT);
        CREATE TABLE audit (observation_id INTEGER);
        CREATE TRIGGER observations_audit AFTER INSERT ON observations
        BEGIN
            INSERT INTO audit (observation_id) VALUES (NEW.id);
        END;
        CREATE TABLE site (id INTEGER PRIMARY KEY, status TEXT);
        INSERT INTO site (id, status) VALUES (1, 'pending');
        """
    )
    # Work the caller has already done in the target transaction.
    conn.execute("UPDATE site SET status = 'for_review' WHERE id = 1")

    yield conn
    conn.close()


@pytest.fixture()
def helper(conn):
    """Return the db helper used for the connection."""
    return DB_HELPER_FACTORY.from_conn(conn)


def observations(conn):
    return conn.execute("SELECT id, value FROM observations ORDER BY id").fetchall()


def audits(conn):
    return conn.execute("SELECT observation_id FROM audit ORDER BY observation_id").fetchall()


def site_status(conn):
    return conn.execute("SELECT status FROM site WHERE id = 1").fetchone()[0]


def test_fast_batch_memory_error_then_slow_batch_error(monkeypatch, conn, helper):
    """Rows already accepted by a fast batch are restored before the retry."""
    # Arrange: fast batch writes A and then runs out of memory.
    def failing_fast_batch(cursor, query, chunk):
        cursor.execute(query, chunk[0])
        raise MemoryError("fast batch ran out of memory")

    monkeypatch.setattr(helper, "executemany_batch", failing_fast_batch)
    # Ordinary batch uses the real cursor and will fail on B's duplicate key.
    monkeypatch.setattr(
        helper, "executemany_slow",
        lambda cursor, query, chunk: cursor.executemany(query, chunk))

    errors = []

    # Act
    processed, failed = executemany(
        INSERT_SQL, conn, ROWS,
        on_error=errors.extend,
        commit_chunks=False,
        chunk_size=3,
    )

    # Assert: only B failed; A and C and their audit records applied once.
    assert processed == 3
    assert failed == 1
    assert [row for row, _ in errors] == [ROW_B]
    assert observations(conn) == [(1, "A"), (3, "C")]
    assert audits(conn) == [(1,), (3,)]

    # The caller's own pending change is still in the same transaction.
    assert site_status(conn) == "for_review"


def test_fast_batch_memory_error_is_restored_before_slow_retry(monkeypatch, conn, helper):
    """A row written by the failed fast batch is removed before the retry."""
    written = []

    def failing_fast_batch(cursor, query, chunk):
        # Write A then fail, leaving A in the transaction.
        cursor.execute(query, chunk[0])
        written.append(chunk[0])
        raise MemoryError("fast batch ran out of memory")

    monkeypatch.setattr(helper, "executemany_batch", failing_fast_batch)
    # The slow batch meets B's duplicate key.  If A from the fast batch were
    # still present, the slow batch would instead fail on A and B would never
    # be reported as the failed row.
    monkeypatch.setattr(
        helper, "executemany_slow",
        lambda cursor, query, chunk: cursor.executemany(query, chunk))

    errors = []

    processed, failed = executemany(
        INSERT_SQL, conn, ROWS,
        on_error=errors.extend,
        commit_chunks=False,
        chunk_size=3,
    )

    assert written == [ROW_A]
    assert processed == 3
    assert failed == 1
    assert [row for row, _ in errors] == [ROW_B]
    assert observations(conn) == [(1, "A"), (3, "C")]
    assert audits(conn) == [(1,), (3,)]


def test_recovered_chunk_can_be_committed_by_caller(monkeypatch, conn, helper):
    """The caller decides to commit the recovered observations and site change."""
    monkeypatch.setattr(
        helper, "executemany_batch",
        lambda cursor, query, chunk: cursor.executemany(query, chunk))
    errors = []

    executemany(
        INSERT_SQL, conn, ROWS,
        on_error=errors.extend,
        commit_chunks=False,
        chunk_size=3,
    )
    conn.commit()

    assert observations(conn) == [(1, "A"), (3, "C")]
    assert audits(conn) == [(1,), (3,)]
    assert site_status(conn) == "for_review"


def test_recovered_chunk_can_be_rolled_back_by_caller(monkeypatch, conn, helper):
    """The caller can roll back everything, including the recovered rows."""
    monkeypatch.setattr(
        helper, "executemany_batch",
        lambda cursor, query, chunk: cursor.executemany(query, chunk))
    errors = []

    executemany(
        INSERT_SQL, conn, ROWS,
        on_error=errors.extend,
        commit_chunks=False,
        chunk_size=3,
    )
    conn.rollback()

    assert observations(conn) == []
    assert audits(conn) == []
    assert site_status(conn) == "pending"


def test_unrecoverable_transaction_stops_without_row_retries(monkeypatch, conn, helper):
    """If the savepoint cannot be restored, stop and ask the caller to roll back."""
    # Arrange: the batch fails on B and the server has marked the whole
    # transaction failed, so even the savepoint rollback is refused.
    def failing_batch(cursor, query, chunk):
        cursor.executemany(query, chunk)

    def failed_transaction(*args, **kwargs):
        raise sqlite3.OperationalError("transaction is aborted")

    monkeypatch.setattr(helper, "executemany_batch", failing_batch)
    monkeypatch.setattr(helper, "rollback_to_savepoint", failed_transaction)
    errors = []

    # Act and assert
    with pytest.raises(ETLHelperInsertError) as exc_info:
        executemany(
            INSERT_SQL, conn, ROWS,
            on_error=errors.extend,
            commit_chunks=False,
            chunk_size=3,
        )

    # The original failure is preserved as the cause, and the message tells
    # the caller to roll back.
    assert isinstance(exc_info.value.__cause__, sqlite3.IntegrityError)
    assert "rolled back" in str(exc_info.value)
    assert "not retried row-by-row" in str(exc_info.value)

    # No callback or partial success is reported.
    assert errors == []

    # The library did not commit or roll back the caller's transaction.
    assert site_status(conn) == "for_review"
    conn.rollback()
    assert site_status(conn) == "pending"


def test_normal_copy_still_commits(monkeypatch, conn, helper):
    """The default self-committing path is unchanged."""
    monkeypatch.setattr(
        helper, "executemany_batch",
        lambda cursor, query, chunk: cursor.executemany(query, chunk))

    executemany(INSERT_SQL, conn, ROWS[:1], commit_chunks=True, chunk_size=3)

    # A new connection sees the committed row.
    assert observations(conn) == [(1, "A")]


def test_no_on_error_still_rolls_back_and_raises(monkeypatch, conn, helper):
    """Without on_error the existing rollback-and-raise behaviour is kept."""
    monkeypatch.setattr(
        helper, "executemany_batch",
        lambda cursor, query, chunk: cursor.executemany(query, chunk))

    with pytest.raises(ETLHelperInsertError):
        executemany(INSERT_SQL, conn, ROWS, chunk_size=3)

    # The library rolled back the transaction, including the caller's change.
    assert observations(conn) == []
    assert site_status(conn) == "pending"


def test_execute_by_row_does_not_commit(conn, helper):
    """Row retries leave their successful rows in the caller's transaction."""
    failed_rows = etlhelper_etl._execute_by_row(
        INSERT_SQL, conn.cursor(), helper, ROWS, "test")

    assert [row for row, _ in failed_rows] == [ROW_B]
    assert observations(conn) == [(1, "A"), (3, "C")]
    assert audits(conn) == [(1,), (3,)]

    # Nothing has been committed yet.
    conn.rollback()
    assert observations(conn) == []
