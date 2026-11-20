"""
Tests for per-task cancellation of copy jobs.

These tests model several copy tasks running in the same process.  Each task
uses independent source and destination SQLite databases.  Transform functions
are used as checkpoints so that the test controls exactly when a task reaches
a chunk boundary.
"""
import sqlite3
import threading

import pytest

from etlhelper import (
    AbortToken,
    abort_etlhelper_threads,
    copy_rows,
    execute,
)
from etlhelper.exceptions import ETLHelperAbort
from etlhelper.row_factories import tuple_row_factory


class Checkpoint:
    """A transform that signals arrival and waits for release."""

    def __init__(self, block_on_call=2, release=None):
        self.arrived = threading.Event()
        self.release = release or threading.Event()
        self.block_on_call = block_on_call
        self.calls = 0

    def __call__(self, rows):
        self.calls += 1
        if self.calls == self.block_on_call:
            self.arrived.set()
            if not self.release.wait(timeout=5):
                raise RuntimeError("timed out waiting for checkpoint release")
        return rows


def make_db(path, table):
    conn = sqlite3.connect(path, check_same_thread=False)
    execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)", conn)
    execute(
        f"INSERT INTO {table} (id) VALUES (1), (2), (3), (4)",
        conn,
    )
    return conn


def make_dest_db(path, table):
    conn = sqlite3.connect(path, check_same_thread=False)
    execute(f"CREATE TABLE {table} (id INTEGER PRIMARY KEY)", conn)
    return conn


def ids(conn, table):
    cursor = conn.cursor()
    try:
        return [row[0] for row in cursor.execute(f"SELECT id FROM {table} ORDER BY id")]
    finally:
        cursor.close()


def run_copy(source_conn, dest_conn, token, transform, result, error):
    try:
        result.append(
            copy_rows(
                "SELECT id FROM src ORDER BY id",
                source_conn,
                "INSERT INTO dest (id) VALUES (?)",
                dest_conn,
                transform=transform,
                row_factory=tuple_row_factory,
                chunk_size=2,
                abort_token=token,
            )
        )
    except ETLHelperAbort as exc:
        error.append(exc)


def test_cancel_one_task_then_global_stop_does_not_restart_it(tmp_path):
    """
    A is cancelled individually; a later global stop and the start of C do
    not revive A.  B runs until the global stop and does not start the next
    chunk.  C, started after the stop, completes.
    """
    # Independent connections for each station and each side of the copy.
    a_source = make_db(tmp_path / "a_source.db", "src")
    a_dest = make_dest_db(tmp_path / "a_dest.db", "dest")
    b_source = make_db(tmp_path / "b_source.db", "src")
    b_dest = make_dest_db(tmp_path / "b_dest.db", "dest")
    c_source = make_db(tmp_path / "c_source.db", "src")
    c_dest = make_dest_db(tmp_path / "c_dest.db", "dest")

    a_token = AbortToken()
    b_token = AbortToken()

    # Both A and B pause at the transform after fetching their first chunk.
    a_checkpoint = Checkpoint()
    b_checkpoint = Checkpoint()

    a_result, a_error = [], []
    b_result, b_error = [], []
    c_result, c_error = [], []

    a_thread = threading.Thread(
        target=run_copy,
        args=(a_source, a_dest, a_token, a_checkpoint, a_result, a_error),
    )
    b_thread = threading.Thread(
        target=run_copy,
        args=(b_source, b_dest, b_token, b_checkpoint, b_result, b_error),
    )

    a_thread.start()
    b_thread.start()
    assert a_checkpoint.arrived.wait(timeout=5)
    assert b_checkpoint.arrived.wait(timeout=5)

    # Cancel only A.  B must not be affected yet.
    a_token.abort()

    # The first chunk of each task was committed; release A so it reaches its
    # cancellation checkpoint before the next write.
    a_checkpoint.release.set()
    a_thread.join(timeout=5)
    assert not a_thread.is_alive()
    assert len(a_error) == 1
    assert isinstance(a_error[0], ETLHelperAbort)
    assert ids(a_dest, "dest") == [1, 2]
    assert ids(a_source, "src") == [1, 2, 3, 4]

    # B is still running and still paused at its checkpoint.
    assert b_thread.is_alive()

    # Emergency stop all remaining tasks.
    abort_etlhelper_threads()

    # Immediately start station C after the global stop returns.  C has its
    # own token and must run to completion.
    c_thread = threading.Thread(
        target=run_copy,
        args=(c_source, c_dest, AbortToken(), None, c_result, c_error),
    )
    c_thread.start()

    # Now let B continue.  It has already fetched its second chunk, but must
    # not write it after the global stop.
    b_checkpoint.release.set()
    b_thread.join(timeout=5)
    c_thread.join(timeout=5)

    assert not b_thread.is_alive()
    assert len(b_error) == 1
    assert isinstance(b_error[0], ETLHelperAbort)
    assert ids(b_dest, "dest") == [1, 2]

    assert not c_thread.is_alive()
    assert c_error == []
    assert c_result == [(4, 0)]
    assert ids(c_dest, "dest") == [1, 2, 3, 4]

    # The committed first blocks were not withdrawn.
    assert ids(a_dest, "dest") == [1, 2]
    assert ids(b_dest, "dest") == [1, 2]

    # The connections can still be used: cancellation released the cursors
    # without closing connections that belong to the task (or others).
    for conn, table in (
        (a_source, "src"),
        (a_dest, "dest"),
        (b_source, "src"),
        (b_dest, "dest"),
        (c_source, "src"),
        (c_dest, "dest"),
    ):
        assert ids(conn, table)


def test_copy_with_already_cancelled_token_does_not_read_source(tmp_path):
    """Passing an aborted token must not execute the source query."""
    source = make_db(tmp_path / "source.db", "src")
    dest = make_dest_db(tmp_path / "dest.db", "dest")
    token = AbortToken()
    token.abort()

    # Remove the source table.  If the source query is attempted, the copy
    # fails with an extract error rather than ETLHelperAbort.
    execute("DROP TABLE src", source)

    with pytest.raises(ETLHelperAbort):
        copy_rows(
            "SELECT id FROM src",
            source,
            "INSERT INTO dest (id) VALUES (?)",
            dest,
            abort_token=token,
        )

    assert ids(dest, "dest") == []


def test_global_stop_is_latched_for_in_flight_task(tmp_path):
    """
    A task running when the global stop is raised stays cancelled even though
    a later task starts and the global event is reset for future work.
    """
    source = make_db(tmp_path / "source.db", "src")
    dest = make_dest_db(tmp_path / "dest.db", "dest")
    checkpoint = Checkpoint()

    result, error = [], []
    thread = threading.Thread(
        target=run_copy,
        args=(source, dest, AbortToken(), checkpoint, result, error),
    )
    thread.start()
    assert checkpoint.arrived.wait(timeout=5)

    abort_etlhelper_threads()

    # Starting another task must not clear the stop observed by the first.
    other_source = make_db(tmp_path / "other_source.db", "src")
    other_dest = make_dest_db(tmp_path / "other_dest.db", "dest")
    assert copy_rows(
        "SELECT id FROM src ORDER BY id",
        other_source,
        "INSERT INTO dest (id) VALUES (?)",
        other_dest,
        row_factory=tuple_row_factory,
        chunk_size=2,
    ) == (4, 0)

    checkpoint.release.set()
    thread.join(timeout=5)

    assert len(error) == 1
    assert isinstance(error[0], ETLHelperAbort)
    assert ids(dest, "dest") == [1, 2]


def test_cancel_token_keyword_alias(tmp_path):
    """The cancel_token keyword is accepted as an alias for abort_token."""
    source = make_db(tmp_path / "source.db", "src")
    dest = make_dest_db(tmp_path / "dest.db", "dest")
    token = AbortToken()
    token.abort()

    with pytest.raises(ETLHelperAbort):
        copy_rows(
            "SELECT id FROM src",
            source,
            "INSERT INTO dest (id) VALUES (?)",
            dest,
            row_factory=tuple_row_factory,
            cancel_token=token,
        )

    assert ids(dest, "dest") == []


def test_cancel_during_source_transform_is_honoured_before_write(tmp_path):
    """
    Cancellation requested while the source transform is running takes effect
    once the driver call returns: the transformed second chunk is not written.
    """
    source = make_db(tmp_path / "source.db", "src")
    dest = make_dest_db(tmp_path / "dest.db", "dest")
    token = AbortToken()
    calls = []

    def transform(rows):
        calls.append(1)
        # Cancel while this (driver-adjacent) call is still in progress,
        # after the first chunk has already been written and committed.
        if len(calls) == 2:
            token.abort()
        return rows

    with pytest.raises(ETLHelperAbort):
        copy_rows(
            "SELECT id FROM src ORDER BY id",
            source,
            "INSERT INTO dest (id) VALUES (?)",
            dest,
            row_factory=tuple_row_factory,
            transform=transform,
            chunk_size=2,
            abort_token=token,
        )

    # The first chunk was committed before the checkpoint; the second was not.
    assert ids(dest, "dest") == [1, 2]
