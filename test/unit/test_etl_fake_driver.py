"""
Tests for executemany against a controllable DB-API driver.

The fake driver models a server that marks the whole transaction as failed
after a constraint violation.  Further SQL is rejected until the transaction
is rolled back, but rolling back to a savepoint can restore the transaction
if the driver supports it.
"""
# pylint: disable=missing-docstring
import pytest

import etlhelper.etl as etlhelper_etl
from etlhelper import executemany
from etlhelper.exceptions import ETLHelperInsertError


class DriverError(Exception):
    """Stand-in for a DB-API DatabaseError."""


class FakeCursor:
    def __init__(self, state):
        self.state = state

    def execute(self, sql, params=()):
        state = self.state
        statement = sql.strip().upper()

        # A failed server only accepts statements that clear the failure.
        if state.failed and not statement.startswith(("ROLLBACK", "SAVEPOINT")):
            raise DriverError("current transaction is aborted")

        if statement.startswith("SAVEPOINT"):
            state.sequence += 1
            state.savepoints[sql.split()[-1]] = state.sequence
            return

        if statement.startswith("ROLLBACK TO SAVEPOINT"):
            if not state.can_restore:
                raise DriverError("cannot restore transaction")
            mark = state.savepoints[sql.split()[-1]]
            state.rows = [row for row in state.rows if row[2] < mark]
            state.failed = False
            return

        if statement.startswith("RELEASE SAVEPOINT"):
            state.savepoints.pop(sql.split()[-1], None)
            return

        if statement.startswith("INSERT"):
            if isinstance(params, dict):
                obs_id, value = params["id"], params["value"]
            else:
                obs_id, value = params
            if obs_id in (row[0] for row in state.rows):
                state.failed = True
                raise DriverError("duplicate key value violates unique constraint")
            state.rows.append((obs_id, value, state.sequence))
            return

        raise AssertionError(f"Unexpected SQL: {sql}")

    def executemany(self, sql, chunk):
        for row in chunk:
            self.execute(sql, row)

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeConnection:
    def __init__(self):
        self.rows = []
        self.savepoints = {}
        self.failed = False
        self.sequence = 0
        self.can_restore = True
        self.commits = 0
        self.rollbacks = 0

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        self.commits += 1
        self.failed = False
        self.savepoints.clear()

    def rollback(self):
        self.rollbacks += 1
        self.rows = []
        self.savepoints = {}
        self.failed = False

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass


class FakeHelper:
    """Minimal helper with the savepoint and batch hooks used by etlhelper."""
    sql_exceptions = (DriverError,)
    paramstyle = "named"

    @staticmethod
    def cursor(conn):
        return conn.cursor()

    @staticmethod
    def executemany(cursor, query, chunk):
        cursor.executemany(query, chunk)


ROW_A = {"id": 1, "value": "A"}
ROW_B = {"id": 1, "value": "B"}
ROW_C = {"id": 3, "value": "C"}
ROWS = [ROW_A, ROW_B, ROW_C]
INSERT_SQL = "INSERT INTO observations (id, value) VALUES (:id, :value)"


@pytest.fixture()
def conn(monkeypatch):
    conn = FakeConnection()
    # The caller's pending change already exists in the same transaction.
    conn.rows.append((99, "site change", 0))
    monkeypatch.setattr(
        etlhelper_etl.DB_HELPER_FACTORY, "from_conn", lambda _conn: FakeHelper())
    return conn


def test_recoverable_transaction_retries_chunk_in_place(conn):
    errors = []

    processed, failed = executemany(
        INSERT_SQL, conn, ROWS,
        on_error=errors.extend,
        commit_chunks=False,
        chunk_size=3,
    )

    assert (processed, failed) == (3, 1)
    assert [row for row, _ in errors] == [ROW_B]
    assert [row[:2] for row in conn.rows] == [(99, "site change"), (1, "A"), (3, "C")]
    # No commit: the caller decides.
    assert conn.commits == 0
    assert conn.rollbacks == 0


def test_caller_can_roll_back_recovered_transaction(conn):
    errors = []
    executemany(
        INSERT_SQL, conn, ROWS,
        on_error=errors.extend,
        commit_chunks=False,
        chunk_size=3,
    )

    conn.rollback()

    assert conn.rows == []


def test_unrecoverable_transaction_stops_and_reports_original_error(conn):
    # Arrange: the server has failed the transaction and the driver cannot
    # restore a savepoint.
    conn.can_restore = False
    errors = []

    # Act and assert
    with pytest.raises(ETLHelperInsertError) as exc_info:
        executemany(
            INSERT_SQL, conn, ROWS,
            on_error=errors.extend,
            commit_chunks=False,
            chunk_size=3,
        )

    error = exc_info.value
    assert isinstance(error.__cause__, DriverError)
    assert "duplicate key" in str(error.__cause__)
    assert "rolled back" in str(error)
    assert "not retried row-by-row" in str(error)

    # No callback, no commit and no rollback by the library.
    assert errors == []
    assert conn.commits == 0
    assert conn.rollbacks == 0


def test_normal_copy_still_self_commits(conn):
    """The default self-committing copy path is unchanged."""
    processed, failed = executemany(
        INSERT_SQL, conn, [ROW_A, ROW_C],
        chunk_size=3,
    )

    assert (processed, failed) == (2, 0)
    assert conn.commits == 1
    assert [row[:2] for row in conn.rows] == [
        (99, "site change"), (1, "A"), (3, "C")]


def test_no_on_error_rolls_back_and_raises(conn):
    """Without on_error the transaction is still rolled back before raising."""
    with pytest.raises(ETLHelperInsertError) as exc_info:
        executemany(INSERT_SQL, conn, ROWS, chunk_size=3)

    assert isinstance(exc_info.value.__cause__, DriverError)
    assert conn.rollbacks == 1
    assert conn.rows == []
