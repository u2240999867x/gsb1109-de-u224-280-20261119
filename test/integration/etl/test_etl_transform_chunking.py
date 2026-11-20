"""Regression diagnostics for transform data loss at entry and chunk boundaries.

The tests use SQLite so that the complete route from a source query, or from
pre-read rows, to the business rows stored in the target can be inspected.

Two kinds of transform are used:

* a pure, row-by-row cleaning rule; and
* a call-counted rule whose output depends on how many times the transform has
  been invoked.

Every transform call records its input rows and output rows.  A wrapper around
the SQLite ``executemany`` call records the batches that the target actually
receives.  This makes it possible to say where the first row is lost instead of
only comparing final row counts.
"""
# pylint: disable=unused-argument, missing-docstring
from dataclasses import dataclass
from itertools import zip_longest
from typing import (
    Any,
    Callable,
    Iterable,
    Iterator,
)

import pytest

from etlhelper import (
    DbParams,
    connect,
    copy_rows,
    load,
)
from etlhelper.db_helpers.sqlite import SQLiteDbHelper


# The shared single-pass input.  Row 1 is void, row 2 expands into two rows and
# row 3 becomes a single row.
INPUT_ROWS = (
    {"id": 1, "kind": "void", "payload": "drop"},
    {"id": 2, "kind": "split", "payload": "a,b"},
    {"id": 3, "kind": "single", "payload": "c"},
)

# Same shape as INPUT_ROWS, but with the first row made valid.  This is used by
# the call-counted transform, which must survive the first-row probe.
COUNTED_INPUT_ROWS = (
    {"id": 1, "kind": "single", "payload": "first"},
    {"id": 2, "kind": "split", "payload": "a,b"},
    {"id": 3, "kind": "single", "payload": "c"},
)

EXPECTED_BUSINESS_ROWS = ((2, "a"), (2, "b"), (3, "c"))


def _clean_row(row: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Apply the pure, row-by-row cleaning rule to one row."""
    if row["kind"] == "void":
        return

    if row["kind"] == "split":
        for value in row["payload"].split(","):
            yield {"id": row["id"], "value": value}
    else:
        yield {"id": row["id"], "value": row["payload"]}


def clean_rows(rows: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
    """Pure transform: filter, expand and pass rows without chunk state."""
    for row in rows:
        yield from _clean_row(row)


class CallNumberedCleanTransform:
    """Clean rows and append the cumulative transform invocation number.

    The first invocation is the probe that ``load`` uses to prepare the insert
    columns.  Its output is discarded, so later invocations produce different
    values depending on the entry point and chunk size.
    """

    def __init__(self):
        self.call_number = 0

    def __call__(self, rows: Iterable[dict[str, Any]]) -> Iterator[dict[str, Any]]:
        self.call_number += 1
        suffix = f"#call{self.call_number}"
        for row in rows:
            for cleaned in _clean_row(row):
                yield {
                    "id": cleaned["id"],
                    "value": f"{cleaned['value']}{suffix}",
                }


def _source_key(row: dict[str, Any]) -> tuple[Any, ...]:
    """Return a stable, readable key for an input row."""
    return row["id"], row["kind"], row["payload"]


def _output_key(row: dict[str, Any]) -> tuple[int, str]:
    """Return a stable, readable key for a business output row."""
    return row["id"], row["value"]


@dataclass(frozen=True)
class TransformCall:
    """Input and output of one actual transform invocation."""

    invocation: int
    inputs: tuple[tuple[Any, ...], ...]
    outputs: tuple[tuple[int, str], ...]


@dataclass(frozen=True)
class RunTrace:
    """Complete trace of one copy_rows/load run."""

    entry: str
    chunk_size: int
    calls: tuple[TransformCall, ...]
    target_batches: tuple[tuple[tuple[int, str], ...], ...]
    target_rows: tuple[tuple[int, str], ...]
    processed: int
    failed: int


class RecordingTransform:
    """Wrap a transform and record every input and output row."""

    def __init__(self, transform: Callable[[Iterable[dict[str, Any]]],
                                           Iterable[dict[str, Any]]]):
        self._transform = transform
        self.calls: list[TransformCall] = []

    def __call__(self, rows: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        input_rows = [dict(row) for row in rows]
        output_rows = [dict(row) for row in self._transform(input_rows)]
        self.calls.append(TransformCall(
            invocation=len(self.calls) + 1,
            inputs=tuple(_source_key(row) for row in input_rows),
            outputs=tuple(_output_key(row) for row in output_rows),
        ))
        return output_rows


def _format_trace(trace: RunTrace) -> str:
    """Render a trace that can be copied into a bug report."""
    lines = [
        f"{trace.entry} chunk_size={trace.chunk_size} "
        f"processed={trace.processed} failed={trace.failed}",
    ]
    for call in trace.calls:
        lines.append(
            f"  call {call.invocation}: "
            f"input={list(call.inputs)} -> output={list(call.outputs)}"
        )
    lines.append(f"  target batches={list(trace.target_batches)}")
    lines.append(f"  target rows={list(trace.target_rows)}")
    return "\n".join(lines)


def _call_summary(trace: RunTrace) -> tuple[tuple[tuple[Any, ...], ...], ...]:
    return tuple((call.inputs, call.outputs) for call in trace.calls)


def _flatten_outputs(trace: RunTrace) -> tuple[tuple[int, str], ...]:
    return tuple(out for call in trace.calls for out in call.outputs)


def _first_pure_rule_divergence(
        trace: RunTrace,
        input_rows: Iterable[dict[str, Any]],
        ) -> str | None:
    """Return the first actual loss/divergence, attributed to entry and input.

    The expected output is built by applying the pure rule to each input row
    independently.  The comparison therefore identifies the first missing or
    changed business row, rather than relying on a final total.
    """
    expected: list[tuple[tuple[Any, ...], tuple[int, str]]] = []
    for input_row in input_rows:
        source = _source_key(input_row)
        for output_row in _clean_row(input_row):
            expected.append((source, _output_key(output_row)))

    actual = list(_flatten_outputs(trace))
    seen_inputs = {
        input_key
        for call in trace.calls
        for input_key in call.inputs
    }

    for index, (expected_item, actual_item) in enumerate(
            zip_longest(expected, actual)):
        if expected_item is None:
            return (
                f"{trace.entry} chunk_size={trace.chunk_size}: unexpected "
                f"output #{index} {actual_item}.\n{_format_trace(trace)}"
            )

        source, expected_output = expected_item
        if actual_item is None:
            if source in seen_inputs:
                reason = (
                    f"input {source} reached the transform but produced no "
                    f"output"
                )
            else:
                reason = f"input {source} never reached the transform"
            return (
                f"{trace.entry} chunk_size={trace.chunk_size}: first data "
                f"loss at output #{index}: expected {expected_output} from "
                f"input {source}; {reason}.\n{_format_trace(trace)}"
            )

        if actual_item != expected_output:
            return (
                f"{trace.entry} chunk_size={trace.chunk_size}: first "
                f"divergence at output #{index}: expected {expected_output} "
                f"from input {source}, got {actual_item}.\n"
                f"{_format_trace(trace)}"
            )

    return None


def _cursor(conn):
    return conn.cursor()


def _seed_source(conn, rows: Iterable[dict[str, Any]]) -> None:
    cursor = _cursor(conn)
    try:
        cursor.execute("DELETE FROM src")
        cursor.executemany(
            "INSERT INTO src (id, kind, payload) "
            "VALUES (:id, :kind, :payload)",
            rows,
        )
        conn.commit()
    finally:
        cursor.close()


def _reset_dest(conn) -> None:
    cursor = _cursor(conn)
    try:
        cursor.execute("DROP TABLE IF EXISTS dest")
        cursor.execute(
            "CREATE TABLE dest (id INTEGER NOT NULL, value TEXT NOT NULL)"
        )
        conn.commit()
    finally:
        cursor.close()


def _read_dest(conn) -> tuple[tuple[int, str], ...]:
    cursor = _cursor(conn)
    try:
        cursor.execute("SELECT id, value FROM dest ORDER BY rowid")
        return tuple(cursor.fetchall())
    finally:
        cursor.close()


@pytest.fixture
def sqlite_etl(tmp_path):
    """Create separate SQLite source and target databases."""
    src_conn = connect(DbParams(
        dbtype="SQLITE",
        filename=str(tmp_path / "source.db"),
    ))
    dest_conn = connect(DbParams(
        dbtype="SQLITE",
        filename=str(tmp_path / "target.db"),
    ))

    cursor = _cursor(src_conn)
    try:
        cursor.execute(
            "CREATE TABLE src (id INTEGER PRIMARY KEY, kind TEXT NOT NULL, "
            "payload TEXT NOT NULL)"
        )
        src_conn.commit()
    finally:
        cursor.close()

    try:
        yield src_conn, dest_conn
    finally:
        src_conn.close()
        dest_conn.close()


@pytest.fixture
def target_batches(monkeypatch):
    """Record the batches that reach the SQLite target."""
    batches: list[tuple[tuple[int, str], ...]] = []
    original_executemany = SQLiteDbHelper.executemany

    def recording_executemany(cursor, query, chunk):
        batches.append(tuple(_output_key(row) for row in chunk))
        return original_executemany(cursor, query, chunk)

    monkeypatch.setattr(
        SQLiteDbHelper,
        "executemany",
        staticmethod(recording_executemany),
    )
    return batches


def _take_batches(batches: list[tuple[tuple[int, str], ...]]) -> tuple:
    snapshot = tuple(batches)
    batches.clear()
    return snapshot


def _run_copy_rows(
        src_conn,
        dest_conn,
        chunk_size: int,
        transform_factory: Callable[[], Callable],
        batches: list,
        input_rows: Iterable[dict[str, Any]] = INPUT_ROWS,
        ) -> RunTrace:
    _reset_dest(dest_conn)
    _seed_source(src_conn, input_rows)
    recorder = RecordingTransform(transform_factory())

    processed, failed = copy_rows(
        "SELECT id, kind, payload FROM src ORDER BY id",
        src_conn,
        "INSERT INTO dest (id, value) VALUES (:id, :value)",
        dest_conn,
        transform=recorder,
        chunk_size=chunk_size,
    )

    return RunTrace(
        entry="copy_rows",
        chunk_size=chunk_size,
        calls=tuple(recorder.calls),
        target_batches=_take_batches(batches),
        target_rows=_read_dest(dest_conn),
        processed=processed,
        failed=failed,
    )


def _run_load(
        dest_conn,
        chunk_size: int,
        input_rows: Iterable[dict[str, Any]],
        transform_factory: Callable[[], Callable],
        batches: list,
        ) -> RunTrace:
    _reset_dest(dest_conn)
    recorder = RecordingTransform(transform_factory())

    processed, failed = load(
        "dest",
        dest_conn,
        [dict(row) for row in input_rows],
        transform=recorder,
        chunk_size=chunk_size,
    )

    return RunTrace(
        entry="load",
        chunk_size=chunk_size,
        calls=tuple(recorder.calls),
        target_batches=_take_batches(batches),
        target_rows=_read_dest(dest_conn),
        processed=processed,
        failed=failed,
    )


def test_copy_rows_pure_clean_rule_chunk_sizes_1_and_3(sqlite_etl,
                                                       target_batches):
    """Source chunking changes transform calls, not the cleaned output."""
    src_conn, dest_conn = sqlite_etl

    trace_1 = _run_copy_rows(
        src_conn, dest_conn, 1, lambda: clean_rows, target_batches)
    trace_3 = _run_copy_rows(
        src_conn, dest_conn, 3, lambda: clean_rows, target_batches)

    # Source-side chunking is visible in the transform inputs.
    assert _call_summary(trace_1) == (
        (((1, "void", "drop"),), ()),
        (((2, "split", "a,b"),), ((2, "a"), (2, "b"))),
        (((3, "single", "c"),), ((3, "c"),)),
    ), _format_trace(trace_1)
    assert _call_summary(trace_3) == (
        (
            (
                (1, "void", "drop"),
                (2, "split", "a,b"),
                (3, "single", "c"),
            ),
            ((2, "a"), (2, "b"), (3, "c")),
        ),
    ), _format_trace(trace_3)

    # The pure rule keeps both expanded rows followed by the valid row, in
    # source order, for both source chunk sizes.
    assert _flatten_outputs(trace_1) == EXPECTED_BUSINESS_ROWS
    assert _flatten_outputs(trace_3) == EXPECTED_BUSINESS_ROWS
    assert trace_1.target_rows == EXPECTED_BUSINESS_ROWS
    assert trace_3.target_rows == EXPECTED_BUSINESS_ROWS
    assert _first_pure_rule_divergence(trace_1, INPUT_ROWS) is None
    assert _first_pure_rule_divergence(trace_3, INPUT_ROWS) is None

    # The target applies its own chunking to the transformed stream.  At
    # chunk_size=1 the two rows produced by the single source call for row 2
    # are split into two target batches.  At chunk_size=3 the single source
    # call's three output rows form one target batch.
    assert trace_1.target_batches == (
        ((2, "a"),),
        ((2, "b"),),
        ((3, "c"),),
    )
    assert trace_3.target_batches == (EXPECTED_BUSINESS_ROWS,)


def test_copy_rows_target_rechunks_single_source_call(sqlite_etl,
                                                      target_batches):
    """Target chunking splits a source call's expanded output."""
    src_conn, dest_conn = sqlite_etl
    trace = _run_copy_rows(
        src_conn, dest_conn, 1, lambda: clean_rows, target_batches)

    assert tuple(len(call.inputs) for call in trace.calls) == (1, 1, 1)
    # The row 2 call is the only source call with two outputs.
    assert tuple(len(call.outputs) for call in trace.calls) == (0, 2, 1)
    assert trace.target_batches == (
        ((2, "a"),),
        ((2, "b"),),
        ((3, "c"),),
    )
    assert trace.target_rows == EXPECTED_BUSINESS_ROWS


def test_load_pure_clean_rule_chunk_sizes_1_and_3(sqlite_etl,
                                                  target_batches):
    """A filtered first row exits load before later chunks are seen."""
    _, dest_conn = sqlite_etl

    trace_1 = _run_load(
        dest_conn, 1, INPUT_ROWS, lambda: clean_rows, target_batches)
    trace_3 = _run_load(
        dest_conn, 3, INPUT_ROWS, lambda: clean_rows, target_batches)

    # Both chunk sizes stop after the first-row column preparation probe.
    for trace in (trace_1, trace_3):
        assert _call_summary(trace) == (
            (((1, "void", "drop"),), ()),
        ), _format_trace(trace)
        assert trace.processed == 0
        assert trace.failed == 0
        assert trace.target_batches == ()
        assert trace.target_rows == ()

    for trace in (trace_1, trace_3):
        report = _first_pure_rule_divergence(trace, INPUT_ROWS)
        assert report is not None
        assert "first data loss at output #0" in report
        assert "expected (2, 'a') from input (2, 'split', 'a,b')" in report
        assert "input (2, 'split', 'a,b') never reached the transform" in report


@pytest.mark.xfail(
    strict=True,
    reason=(
        "Known load() data loss: load() calls transform([first_row]) to infer "
        "the insert columns.  A pure row filter that rejects the first row "
        "raises StopIteration in that probe, so load() returns (0, 0) without "
        "ever passing rows 2 and 3 to the transform or SQLite.  The passing "
        "diagnostic test above records the exact first lost input."
    ),
)
@pytest.mark.parametrize("chunk_size", [1, 3])
def test_load_pure_clean_rule_should_keep_rows_after_filtered_first(
        sqlite_etl,
        target_batches,
        chunk_size,
        ):
    """Desired behaviour: the pure rule preserves rows after row 1."""
    _, dest_conn = sqlite_etl
    trace = _run_load(
        dest_conn, chunk_size, INPUT_ROWS, lambda: clean_rows,
        target_batches)

    assert _first_pure_rule_divergence(trace, INPUT_ROWS) is None, (
        _format_trace(trace)
    )
    assert trace.target_rows == EXPECTED_BUSINESS_ROWS


@pytest.mark.parametrize("chunk_size", [1, 3])
def test_pure_clean_rule_entry_divergence_points_to_load_and_input(
        sqlite_etl,
        target_batches,
        chunk_size,
        ):
    """Cross-entry divergence is located by row, not by target total."""
    src_conn, dest_conn = sqlite_etl

    copy_trace = _run_copy_rows(
        src_conn, dest_conn, chunk_size, lambda: clean_rows, target_batches)
    load_trace = _run_load(
        dest_conn, chunk_size, INPUT_ROWS, lambda: clean_rows,
        target_batches)

    assert _flatten_outputs(copy_trace) == EXPECTED_BUSINESS_ROWS
    assert copy_trace.target_rows == EXPECTED_BUSINESS_ROWS
    assert _flatten_outputs(load_trace) == ()
    assert load_trace.target_rows == ()

    # The source-chunking traces differ, so the report must identify the
    # entry and the first input that did not survive, not just that both
    # targets have different totals.
    assert _call_summary(copy_trace) != _call_summary(load_trace)
    report = _first_pure_rule_divergence(load_trace, INPUT_ROWS)
    assert report is not None
    assert report.startswith(
        f"load chunk_size={chunk_size}: first data loss at output #0")
    assert "expected (2, 'a') from input (2, 'split', 'a,b')" in report
    assert "input (2, 'split', 'a,b') never reached the transform" in report


def test_copy_rows_call_counted_transform_reveals_source_chunking(
        sqlite_etl,
        target_batches,
        ):
    """The same counted callback sees different source chunks."""
    src_conn, dest_conn = sqlite_etl

    trace_1 = _run_copy_rows(
        src_conn, dest_conn, 1, CallNumberedCleanTransform,
        target_batches, COUNTED_INPUT_ROWS)
    trace_3 = _run_copy_rows(
        src_conn, dest_conn, 3, CallNumberedCleanTransform,
        target_batches, COUNTED_INPUT_ROWS)

    assert _call_summary(trace_1) == (
        (((1, "single", "first"),), ((1, "first#call1"),)),
        (((2, "split", "a,b"),), ((2, "a#call2"), (2, "b#call2"))),
        (((3, "single", "c"),), ((3, "c#call3"),)),
    ), _format_trace(trace_1)
    assert trace_1.target_rows == (
        (1, "first#call1"),
        (2, "a#call2"),
        (2, "b#call2"),
        (3, "c#call3"),
    )

    assert _call_summary(trace_3) == (
        (
            (
                (1, "single", "first"),
                (2, "split", "a,b"),
                (3, "single", "c"),
            ),
            (
                (1, "first#call1"),
                (2, "a#call1"),
                (2, "b#call1"),
                (3, "c#call1"),
            ),
        ),
    ), _format_trace(trace_3)
    assert trace_3.target_rows == (
        (1, "first#call1"),
        (2, "a#call1"),
        (2, "b#call1"),
        (3, "c#call1"),
    )


def test_load_call_counted_transform_reveals_probe_and_target_rechunking(
        sqlite_etl,
        target_batches,
        ):
    """Identify the callback invocation affected by the target chunk size."""
    _, dest_conn = sqlite_etl

    trace_1 = _run_load(
        dest_conn, 1, COUNTED_INPUT_ROWS, CallNumberedCleanTransform,
        target_batches)
    trace_3 = _run_load(
        dest_conn, 3, COUNTED_INPUT_ROWS, CallNumberedCleanTransform,
        target_batches)

    # Invocation 1 is load's column-preparation probe.  Its output is
    # discarded; the first data row is affected again at invocation 2.
    assert trace_1.calls[0].inputs == ((1, "single", "first"),)
    assert trace_1.calls[0].outputs == ((1, "first#call1"),)
    assert _call_summary(trace_1) == (
        (((1, "single", "first"),), ((1, "first#call1"),)),
        (((1, "single", "first"),), ((1, "first#call2"),)),
        (((2, "split", "a,b"),), ((2, "a#call3"), (2, "b#call3"))),
        (((3, "single", "c"),), ((3, "c#call4"),)),
    ), _format_trace(trace_1)
    assert trace_1.target_batches == (
        ((1, "first#call2"),),
        ((2, "a#call3"), (2, "b#call3")),
        ((3, "c#call4"),),
    )

    # At chunk_size=3, invocation 2 receives the whole target chunk.  All
    # data rows therefore carry the same call number, unlike chunk_size=1
    # where rows 2 and 3 are affected at invocations 3 and 4.
    assert trace_3.calls[0].inputs == ((1, "single", "first"),)
    assert trace_3.calls[0].outputs == ((1, "first#call1"),)
    assert _call_summary(trace_3) == (
        (((1, "single", "first"),), ((1, "first#call1"),)),
        (
            (
                (1, "single", "first"),
                (2, "split", "a,b"),
                (3, "single", "c"),
            ),
            (
                (1, "first#call2"),
                (2, "a#call2"),
                (2, "b#call2"),
                (3, "c#call2"),
            ),
        ),
    ), _format_trace(trace_3)
    assert trace_3.target_batches == (
        (
            (1, "first#call2"),
            (2, "a#call2"),
            (2, "b#call2"),
            (3, "c#call2"),
        ),
    )
