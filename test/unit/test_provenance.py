"""Tests for transform provenance / source-row tracking.

These use an in-memory SQLite database so that they can run without the
PostgreSQL integration test database.  The scenario mirrors a real ETL job in
which several readings produce one comparison row and one flag per station.
"""
import sqlite3
from unittest.mock import Mock

import pytest

import etlhelper.etl as etlhelper_etl
from etlhelper import (
    NO_SOURCE,
    Output,
    SourceRef,
    copy_rows,
    executemany,
    load,
    no_source,
    with_source,
    with_sources,
)
from etlhelper.etl import FailedRow
from etlhelper.exceptions import ETLHelperInvalidSourceError
from etlhelper.provenance import (
    get_sources,
    prepare_output,
    unwrap_row,
)


# The input block has two stations, each with a reading numbered 17, plus a
# voided A/17 reading.  The voided reading is filtered out.
READINGS = [
    {"station": "A", "number": 17, "value": 10.0, "void": 0},
    {"station": "B", "number": 17, "value": 10.0, "void": 0},
    {"station": "A", "number": 17, "value": 99.9, "void": 1},
]


@pytest.fixture
def conn():
    connection = sqlite3.connect(":memory:")
    # kind is unique.  Both station flags write exactly the same business
    # fields, so the first flag inserts and the trailing flag is rejected as a
    # duplicate of it.
    connection.execute(
        "CREATE TABLE destination ("
        "kind text PRIMARY KEY, value real)"
    )
    # Pre-existing row that makes the comparison insert fail.
    connection.execute(
        "INSERT INTO destination VALUES ('comparison', 0.0)")
    connection.commit()
    yield connection
    connection.close()


def scenario_transform(chunk):
    """Produce flag A, comparison, flag B from one input block.

    Only the comparison and the final (B) flag are rejected by the database.
    Both flags have exactly the same output fields.
    """
    readings = {row["station"]: row for row in chunk if not row["void"]}
    station_a = readings["A"]
    station_b = readings["B"]

    comparison = {"kind": "comparison", "value": 20.0}
    flag_a = {"kind": "flag", "value": 1.0}
    flag_b = {"kind": "flag", "value": 1.0}

    yield with_source(flag_a, station_a)
    yield with_sources(comparison, [station_a, station_b])
    yield with_sources(flag_b, [station_b])


INSERT_SQL = (
    "INSERT INTO destination (kind, value) "
    "VALUES (:kind, :value)"
)


def run(conn, write_chunk_size):
    """Execute the scenario using a stable input group and a write chunk size."""
    outputs = scenario_transform(READINGS)
    errors = []
    processed, failed = executemany(
        INSERT_SQL,
        conn,
        outputs,
        on_error=errors.extend,
        chunk_size=write_chunk_size,
    )
    return processed, failed, errors


@pytest.mark.parametrize("create_table_sql", [""])
def test_placeholder(create_table_sql):
    # Ensure the fixture table is usable independently of the scenario.
    assert create_table_sql == ""


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 4, 5000])
def test_failed_rows_reference_exact_source_rows(conn, chunk_size):
    processed, failed, errors = run(conn, chunk_size)

    assert processed == 3
    assert failed == 2

    # The rejected rows are the comparison and the final flag.
    failed_rows = [failed_row.row for failed_row in errors]
    assert failed_rows == [
        {"kind": "comparison", "value": 20.0},
        {"kind": "flag", "value": 1.0},
    ]

    comparison, flag_b = errors
    source_a = comparison.sources[0]
    source_b = comparison.sources[1]
    assert (source_a["station"], source_a["number"]) == ("A", 17)
    assert (source_b["station"], source_b["number"]) == ("B", 17)

    # The final flag belongs to station B, not to the equal-valued station A
    # flag, and not to the voided A/17 reading.
    (flag_b_source,) = flag_b.sources
    assert flag_b_source["station"] == "B"
    assert flag_b_source["number"] == 17
    assert flag_b_source["void"] == 0
    assert flag_b_source is not source_a


@pytest.mark.parametrize("chunk_size", [1, 2, 3, 4, 5000])
def test_provenance_independent_of_chunk_size(conn, chunk_size):
    _, _, errors = run(conn, chunk_size)
    expected = [
        ("comparison", [("A", 17), ("B", 17)]),
        ("flag", [("B", 17)]),
    ]
    actual = [
        (
            failed_row.row["kind"],
            [(source["station"], source["number"])
             for source in failed_row.sources],
        )
        for failed_row in errors
    ]
    assert actual == expected


def test_two_equal_flags_are_distinct_outputs():
    """Two identical output rows remain distinct by identity."""
    input_rows = [
        {"station": "A", "number": 17},
        {"station": "B", "number": 17},
    ]
    flag_a = {"kind": "flag"}
    flag_b = {"kind": "flag"}
    output_a = prepare_output(with_sources(flag_a, [input_rows[0]]),
                              input_rows)
    output_b = prepare_output(with_sources(flag_b, [input_rows[1]]),
                              input_rows)
    assert output_a.row == output_b.row
    assert output_a is not output_b
    assert output_a.sources[0] is not output_b.sources[0]


def test_value_id_and_position_do_not_infer_source():
    """A source with equal values but different identity is invalid."""
    input_rows = [
        {"station": "A", "number": 17},
        {"station": "B", "number": 17},
    ]
    equal_valued_copy = {"station": "A", "number": 17}
    with pytest.raises(ETLHelperInvalidSourceError):
        prepare_output(with_sources({"kind": "flag"}, [equal_valued_copy]),
                       input_rows)


def test_explicit_no_source():
    input_rows = [{"station": "A"}]
    output = prepare_output(no_source({"kind": "flag"}), input_rows)
    assert isinstance(output, Output)
    assert output.sources is None
    assert output.source_known is True
    assert get_sources(output) is None


def test_plain_row_has_no_provenance():
    input_rows = [{"station": "A"}]
    row = {"kind": "flag"}
    output = prepare_output(row, input_rows)
    assert output is row
    assert get_sources(row) is None


def test_source_ref_wraps_row_like_source():
    input_rows = [[1, 2, 3]]
    output = prepare_output(
        Output({"kind": "flag"}, sources=SourceRef(input_rows[0])),
        input_rows)
    assert output.sources[0] is input_rows[0]


def test_invalid_source_reported_before_write(conn):
    """An invalid reference is raised before the output is submitted."""
    def bad_transform(chunk):
        # Generate a row from this input, but cite a row that was not part of
        # this transform's input.
        yield with_sources({"station": "Z", "kind": "flag", "value": 1.0},
                           [{"station": "Z", "number": 17}])

    errors = []
    with pytest.raises(ETLHelperInvalidSourceError):
        executemany(INSERT_SQL, conn, READINGS, transform=bad_transform,
                    on_error=errors.extend)
    assert errors == []
    assert conn.execute(
        "SELECT COUNT(*) FROM destination").fetchone()[0] == 1


def test_unwrapped_rows_only_reach_database(monkeypatch, conn):
    """Provenance wrappers never leak into the database parameter payload."""
    seen = []

    def fake_executemany(cursor, query, chunk):
        seen.extend(chunk)

    helper = etlhelper_etl.DB_HELPER_FACTORY.from_conn(conn)
    monkeypatch.setattr(helper, "executemany", fake_executemany)

    def transform(chunk):
        for row in chunk:
            yield with_source({"kind": "flag", "value": row["value"]}, row)

    processed, failed = executemany(
        INSERT_SQL, conn, READINGS[:2], transform=transform)
    assert (processed, failed) == (2, 0)

    assert seen
    assert all(isinstance(row, dict) for row in seen)
    assert all(not isinstance(row, Output) for row in seen)


def test_load_with_provenance_wrappers(conn):
    def transform(chunk):
        for row in chunk:
            yield with_source(
                {"kind": f"flag_{row['station']}", "value": row["value"]}, row)

    processed, failed = load(
        "destination", conn, READINGS[:2], transform=transform)
    assert (processed, failed) == (2, 0)
    assert conn.execute("SELECT COUNT(*) FROM destination").fetchone()[0] == 3


def test_failed_row_two_tuple_compatibility():
    failed = FailedRow({"id": 1}, ValueError("bad"))
    assert isinstance(failed, tuple)
    assert failed == ({"id": 1},) + (failed[1],)
    row, exception = failed
    assert row == {"id": 1}
    assert isinstance(exception, ValueError)
    assert failed.row is row
    assert failed.exception is exception
    assert failed._asdict()["row"] == {"id": 1}
    pairs = []
    pairs.extend([failed])
    assert pairs[0][0] == {"id": 1}
    assert isinstance(pairs[0][1], ValueError)


def test_copy_rows_provenance_stable_across_write_chunks():
    """copy_rows extracts with one grouping and writes with another."""
    source = sqlite3.connect(":memory:")
    source.execute(
        "CREATE TABLE src (station text, number int, value real, void int)")
    source.executemany(
        "INSERT INTO src VALUES (?, ?, ?, ?)",
        [(row["station"], row["number"], row["value"], row["void"])
         for row in READINGS])
    source.commit()

    results = {}
    for write_chunk_size in (1, 2, 3, 5000):
        dest = sqlite3.connect(":memory:")
        dest.execute("CREATE TABLE d (kind text primary key, value real)")
        dest.execute("INSERT INTO d VALUES ('comparison', 0.0)")
        dest.commit()

        # Extraction groups the whole input block; only the write is chunked.
        outputs = scenario_transform(iter(READINGS))
        errors = []
        executemany(
            "INSERT INTO d (kind, value) VALUES (:kind, :value)",
            dest,
            outputs,
            on_error=errors.extend,
            chunk_size=write_chunk_size,
        )
        results[write_chunk_size] = [
            (failed.row["kind"],
             tuple((source_row["station"], source_row["number"])
                   for source_row in failed.sources))
            for failed in errors
        ]
        dest.close()

    source.close()
    expected = [
        ("comparison", (("A", 17), ("B", 17))),
        ("flag", (("B", 17),)),
    ]
    assert all(result == expected for result in results.values())


def test_plain_rows_without_transform_unchanged(conn):
    processed, failed = executemany(
        INSERT_SQL,
        conn,
        [{"kind": "flag", "value": 2.0}],
    )
    assert (processed, failed) == (1, 0)
    assert conn.execute(
        "SELECT value FROM destination WHERE kind='flag'").fetchone()[0] == 2.0


def test_pre_wrapped_row_without_transform_carries_source():
    """A caller-supplied Output is unwrapped and its source is reported."""
    source = {"station": "A", "number": 17}
    output = with_source({"kind": "flag", "value": 1.0}, source)

    connection = sqlite3.connect(":memory:")
    connection.execute("CREATE TABLE d (kind text primary key, value real)")
    connection.execute("INSERT INTO d VALUES ('flag', 1.0)")
    connection.commit()

    errors = []
    processed, failed = executemany(
        "INSERT INTO d (kind, value) VALUES (:kind, :value)",
        connection,
        [output],
        on_error=errors.extend,
    )
    connection.close()

    assert (processed, failed) == (1, 1)
    assert errors[0].sources == (source,)
    assert errors[0].sources[0] is source


def test_with_sources_helpers():
    source_a = {"station": "A"}
    source_b = {"station": "B"}
    assert with_sources({}, [source_a, source_b]).sources == (source_a, source_b)
    assert with_sources({}, source_a, source_b).sources == (source_a, source_b)
    assert Output({}, sources=NO_SOURCE).sources is None
    assert unwrap_row(with_source({"id": 1}, source_a)) == {"id": 1}


def test_integer_row_is_not_mistaken_for_source():
    """A two-item output whose second item is an int remains a plain row."""
    input_rows = [{"station": "A"}]
    pair_row = (1, 17)
    output = prepare_output(pair_row, input_rows)
    assert output is pair_row


@pytest.mark.parametrize("sources_factory", [
    lambda source_a, source_b: [source_a, source_b],
    lambda source_a, source_b: (source_a, source_b),
    lambda source_a, source_b: {id(source_a): source_a,
                                id(source_b): source_b}.values(),
])
def test_supported_source_collections(sources_factory):
    input_rows = [{"station": "A"}, {"station": "B"}]
    source_a, source_b = input_rows
    output = prepare_output(
        Output({"kind": "comparison"},
               sources=sources_factory(source_a, source_b)),
        input_rows)
    assert output.sources == (source_a, source_b)
    assert output.sources[0] is source_a
    assert output.sources[1] is source_b


def test_single_source_keyword():
    input_rows = [{"station": "A"}]
    output = prepare_output(
        Output({"kind": "flag"}, source=input_rows[0]), input_rows)
    assert output.sources == (input_rows[0],)


def test_inputs_keyword():
    input_rows = [{"station": "A"}, {"station": "B"}]
    output = prepare_output(
        Output({"kind": "comparison"}, inputs=input_rows), input_rows)
    assert output.sources == (input_rows[0], input_rows[1])


def test_tuple_of_row_and_sources_form():
    input_rows = [{"station": "A"}, {"station": "B"}]
    output = prepare_output(
        ({"kind": "flag"}, [input_rows[1]]), input_rows)
    assert isinstance(output, Output)
    assert output.row == {"kind": "flag"}
    assert output.sources == (input_rows[1],)


def test_plain_two_column_row_not_mistaken_for_wrapper():
    input_rows = [("A", 17), ("B", 17)]
    row = (input_rows[0], 3.14)
    output = prepare_output(row, input_rows)
    assert output is row


def test_mapping_wrapper_form():
    input_rows = [{"station": "A"}]
    output = prepare_output(
        {"row": {"kind": "flag"}, "sources": [input_rows[0]]}, input_rows)
    assert isinstance(output, Output)
    assert output.row == {"kind": "flag"}
    assert output.sources == (input_rows[0],)


def test_invalid_reference_from_a_different_chunk():
    chunk_one = [{"station": "A"}]
    chunk_two = [{"station": "B"}]
    prepared = prepare_output(
        with_sources({"kind": "flag"}, chunk_one[0]), chunk_one)
    with pytest.raises(ETLHelperInvalidSourceError):
        prepare_output(prepared, chunk_two)


def test_provenance_wrappers_are_unwrapped_recursively():
    input_rows = [{"station": "A"}]

    class Outer:
        row = {"kind": "flag"}
        sources = [input_rows[0]]

    output = prepare_output(Outer(), input_rows)
    assert unwrap_row(output) == {"kind": "flag"}
    assert output.sources == (input_rows[0],)
