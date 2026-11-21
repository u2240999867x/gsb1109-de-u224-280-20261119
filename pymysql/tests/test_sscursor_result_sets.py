import pytest

from pymysql import err
from pymysql.cursors import (
    RESULT_SET_ABANDONED,
    RESULT_SET_COMPLETED,
    RESULT_SET_FAILED,
    RESULT_SET_READING,
    SSCursor,
)


class ScriptedResult:
    """An unbuffered result set driven by an in-memory packet script."""

    def __init__(self, rows, *, has_next=False, has_columns=True):
        self.rows = [tuple(row) for row in rows]
        self._position = 0
        self._fail_at = None
        self.description = (("value",),) if has_columns else None
        self.unbuffered_active = has_columns
        self.has_next = has_next
        self._more = has_next
        self.warning_count = 0
        self.affected_rows = 18446744073709551615
        self.insert_id = 0
        self.server_status = None
        self.message = None
        self.field_count = 1 if has_columns else 0

    def fail_when_reading(self, position):
        self._fail_at = position
        return self

    def _read_rowdata_packet_unbuffered(self):
        if not self.unbuffered_active:
            return None
        if self._position >= len(self.rows):
            self.unbuffered_active = False
            self.has_next = self._more
            return None
        if self._position == self._fail_at:
            self._fail_at = None
            raise err.OperationalError(2013, "Lost connection to MySQL server")
        row = self.rows[self._position]
        self._position += 1
        return row

    def _finish_unbuffered_query(self):
        discarded = 0
        while self.unbuffered_active:
            if self._position >= len(self.rows):
                self.unbuffered_active = False
                self.has_next = self._more
                break
            self._position += 1
            discarded += 1
        return discarded


class ScriptedConnection:
    """Return one result group per query, and one result per next_result."""

    def __init__(self, *groups):
        self.groups = [list(group) for group in groups]
        self._group = None
        self._index = 0
        self._result = None
        self.encoding = "utf8"
        self.use_unicode = True

    def query(self, sql, unbuffered=False):
        self._group = self.groups.pop(0)
        self._index = 0
        self._result = self._group[0]
        return self._result.affected_rows

    def next_result(self, unbuffered=False):
        self._index += 1
        self._result = self._group[self._index]
        return self._result.affected_rows

    def escape(self, value):
        return value


def make_cursor(*results):
    cursor = SSCursor(ScriptedConnection(results))
    cursor.execute("CALL p()")
    return cursor


def result(*rows, has_next=False, has_columns=True):
    return ScriptedResult(rows, has_next=has_next, has_columns=has_columns)


def test_natural_end_counts_only_rows_returned_to_caller():
    cursor = make_cursor(result(["a"], ["b"], ["c"], ["d"], ["e"]))

    assert cursor.fetchone() == ("a",)
    assert cursor.result_sets[0].delivered_rows == 1

    cursor.scroll(2)
    record = cursor.result_sets[0]
    assert record.skipped_rows == 2
    assert record.delivered_rows == 1

    assert cursor.fetchmany(10) == [("d",), ("e",)]
    assert record.delivered_rows == 3
    assert record.skipped_rows == 2
    assert record.status == RESULT_SET_COMPLETED
    assert record.completed
    assert not record.abandoned


def test_fetchmany_reaching_eof_completes_without_extra_reads():
    cursor = make_cursor(result(["a"], ["b"], ["c"]))

    assert cursor.fetchone() == ("a",)
    cursor.scroll(1)
    assert cursor.fetchmany(1) == [("c",)]
    record = cursor.result_sets[0]
    assert record.delivered_rows == 2
    assert record.skipped_rows == 1
    assert record.status == RESULT_SET_READING

    assert cursor.fetchone() is None
    assert record.status == RESULT_SET_COMPLETED


def test_early_nextset_abandons_old_result_and_next_set_has_its_own_count():
    cursor = make_cursor(
        result(["a"], ["b"], ["c"], ["d"], has_next=True),
        result(["x"], ["y"], ["z"]),
    )

    assert cursor.fetchone() == ("a",)
    assert cursor.nextset() is True

    old, new = cursor.result_sets
    assert old.delivered_rows == 1
    assert old.discarded_rows == 3
    assert old.skipped_rows == 0
    assert old.status == RESULT_SET_ABANDONED
    assert not old.completed

    assert new.status == RESULT_SET_READING
    assert new.delivered_rows == 0
    assert cursor.fetchall() == [("x",), ("y",), ("z",)]
    assert new.delivered_rows == 3
    assert new.status == RESULT_SET_COMPLETED


def test_execute_switch_abandons_previous_result():
    conn = ScriptedConnection(
        [result(["a"], ["b"])],
        [result(["x"], ["y"])],
    )
    cursor = SSCursor(conn)
    cursor.execute("CALL p()")

    assert cursor.fetchone() == ("a",)
    with pytest.warns(UserWarning):
        cursor.execute("CALL p()")

    old = cursor.result_sets[0]
    assert old.delivered_rows == 1
    assert old.discarded_rows == 1
    assert old.status == RESULT_SET_ABANDONED

    assert len(cursor.result_sets) == 2
    new = cursor.result_sets[1]
    assert new.delivered_rows == 0
    assert cursor.fetchall() == [("x",), ("y",)]
    assert new.delivered_rows == 2


def test_fetchmany_failure_keeps_delivery_and_failed_status_after_close():
    cursor = make_cursor(
        result(["a"], ["b"], ["c"], has_next=True).fail_when_reading(1),
        result(["x"]),
    )

    with pytest.raises(err.OperationalError):
        cursor.fetchmany(3)

    record = cursor.result_sets[0]
    assert record.delivered_rows == 0
    assert record.status == RESULT_SET_FAILED
    assert record.failed

    cursor.close()

    # Cleanup drained the remaining rows but did not reclassify the read.
    assert record.delivered_rows == 0
    assert record.status == RESULT_SET_FAILED
    assert cursor.connection is None
    assert cursor.result_sets[0] is record


def test_completed_result_and_abandoned_result_are_distinguishable():
    cursor = make_cursor(
        result(["a"], ["b"], has_next=True),
        result(["x"], ["y"]),
    )

    assert cursor.fetchall() == [("a",), ("b",)]
    completed = cursor.result_sets[0]
    assert completed.status == RESULT_SET_COMPLETED
    assert completed.delivered_rows == 2

    assert cursor.nextset() is True
    assert cursor.fetchone() == ("x",)
    assert cursor.nextset() is None

    abandoned = cursor.result_sets[1]
    assert abandoned.status == RESULT_SET_ABANDONED
    assert abandoned.delivered_rows == 1
    assert abandoned.discarded_rows == 1
    assert abandoned.status != completed.status


def test_records_survive_close_and_cleanup_does_not_add_records():
    ok_result = result(has_columns=False, has_next=True)
    cursor = make_cursor(
        result(["a"], ["b"], has_next=True),
        ok_result,
        result(),
    )

    assert cursor.fetchone() == ("a",)
    cursor.close()

    records = cursor.get_result_set_records()
    # Only the set the caller entered is recorded; cleanup may drain more.
    assert [record.index for record in records] == [0]
    assert records[0].status == RESULT_SET_ABANDONED
    assert records[0].discarded_rows == 1

    # Callers get a snapshot, while the cursor keeps its own history.
    records.clear()
    assert len(cursor.result_set_records) == 1


def test_empty_column_result_is_completed_with_zero_delivered_rows():
    cursor = make_cursor(result())

    assert cursor.fetchone() is None
    assert len(cursor.result_sets) == 1
    assert cursor.result_sets[0].status == RESULT_SET_COMPLETED
    assert cursor.result_sets[0].delivered_rows == 0
