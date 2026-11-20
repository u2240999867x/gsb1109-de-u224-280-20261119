"""
Integration tests for loading dictionary records into a positionally-bound
target table through caller-supplied column mappings.

These use an in-memory SQLite database, so they run without external
services.
"""
# pylint: disable=unused-argument, missing-docstring
import sqlite3

import pytest

from etlhelper import (
    fetchall,
    generate_mapped_insert_sql,
    load,
)
from etlhelper.exceptions import (
    ETLHelperInsertError,
    ETLHelperMappingConflictError,
    ETLHelperRequiredColumnError,
)

VERSION_FIELD = 'collector_version'

# Each target column maps to the source field declared for that collector
# version.  The old collector uses `code`; the new one uses `report_code`.
MAPPING = {
    'ReportCode': {'v1': 'code', 'v2': 'report_code'},
    'Station': 'station',
    'Quality': 'quality',
}


@pytest.fixture()
def mapped_conn():
    conn = sqlite3.connect(':memory:')
    conn.execute(
        "CREATE TABLE sensor_data ("
        "ReportCode text PRIMARY KEY, "
        "Station text NOT NULL, "
        "Quality text)")
    conn.commit()
    yield conn
    conn.close()


def old_record(code, station, quality, diagnostic):
    return {
        VERSION_FIELD: 'v1',
        'code': code,
        'report_code': diagnostic,
        'station': station,
        'quality': quality,
    }


def new_record(report_code, station, quality):
    return {
        VERSION_FIELD: 'v2',
        'report_code': report_code,
        'station': station,
        'quality': quality,
    }


def mixed_stream():
    # Old collector record carrying both source fields with different values
    yield old_record('RC-OLD-1', 'ST-A', 'good', 'DIAG-1')
    # New collector record without the mandatory Station
    yield new_record('RC-NEW-1', None, 'good')
    # Old collector record with an explicitly nullable Quality
    yield old_record('RC-OLD-2', 'ST-B', None, 'DIAG-2')


def test_mapped_load_interleaved_versions(mapped_conn):
    errors = []

    processed, failed = load(
        'sensor_data', mapped_conn, mixed_stream(),
        column_mapping=MAPPING, version_field=VERSION_FIELD,
        on_error=errors.extend, chunk_size=1)

    # All three records were processed; the station-less one was rejected
    assert (processed, failed) == (3, 1)

    failed_row, exception = errors[0]
    assert isinstance(exception, ETLHelperRequiredColumnError)
    assert failed_row[VERSION_FIELD] == 'v2'
    assert failed_row['report_code'] == 'RC-NEW-1'
    assert 'Station' in str(exception)

    result = fetchall(
        "SELECT ReportCode, Station, Quality FROM sensor_data",
        mapped_conn)

    # The two accepted rows keep their original order; Quality is real NULL
    assert result == [
        {'ReportCode': 'RC-OLD-1', 'Station': 'ST-A', 'Quality': 'good'},
        {'ReportCode': 'RC-OLD-2', 'Station': 'ST-B', 'Quality': None},
    ]
    cursor = mapped_conn.execute(
        "SELECT Quality FROM sensor_data WHERE ReportCode = 'RC-OLD-2'")
    assert cursor.fetchone()[0] is None


def test_mapped_load_selects_version_declared_source_field(mapped_conn):
    load('sensor_data', mapped_conn,
         [old_record('RC-OLD-1', 'ST-A', 'good', 'DIAG-1')],
         column_mapping=MAPPING, version_field=VERSION_FIELD)

    cursor = mapped_conn.execute("SELECT ReportCode FROM sensor_data")
    assert cursor.fetchone()[0] == 'RC-OLD-1'  # not the DIAG-1 diagnostic


def test_mapped_load_second_call_uses_its_own_mapping(mapped_conn):
    # First call
    first_errors = []
    processed, failed = load(
        'sensor_data', mapped_conn, mixed_stream(),
        column_mapping=MAPPING, version_field=VERSION_FIELD,
        on_error=first_errors.extend)
    assert (processed, failed) == (3, 1)
    assert len(first_errors) == 1

    # Reset the table so the second call is judged purely on its own mapping
    mapped_conn.execute("DELETE FROM sensor_data")
    mapped_conn.commit()

    # A second, independently supplied mapping over a fresh, identical stream
    other_mapping = {
        'ReportCode': {'v1': 'code', 'v2': 'report_code'},
        'Station': 'station',
        'Quality': 'quality',
    }

    def same_content_stream():
        yield from mixed_stream()

    second_errors = []
    processed, failed = load(
        'sensor_data', mapped_conn, same_content_stream(),
        column_mapping=other_mapping, version_field=VERSION_FIELD,
        on_error=second_errors.extend)

    assert (processed, failed) == (3, 1)
    assert len(second_errors) == 1
    assert isinstance(second_errors[0].exception, ETLHelperRequiredColumnError)


def test_mapped_load_rejects_conflict_before_writing(mapped_conn):
    conflicting_mapping = {
        # Two source fields for one target, no version_field to choose
        'ReportCode': {'v1': 'code', 'v2': 'report_code'},
        'Station': 'station',
        'Quality': 'quality',
    }

    with pytest.raises(ETLHelperMappingConflictError, match="ReportCode"):
        load('sensor_data', mapped_conn, mixed_stream(),
             column_mapping=conflicting_mapping)

    # Nothing was written before the conflict was reported
    assert fetchall("SELECT * FROM sensor_data", mapped_conn) == []


def test_mapped_load_rejects_ambiguous_per_version_group(mapped_conn):
    conflicting_mapping = {
        'ReportCode': {'v1': ['code', 'report_code'], 'v2': 'report_code'},
        'Station': 'station',
        'Quality': 'quality',
    }

    with pytest.raises(ETLHelperMappingConflictError, match="version 'v1'"):
        load('sensor_data', mapped_conn, mixed_stream(),
             column_mapping=conflicting_mapping,
             version_field=VERSION_FIELD)


def test_mapped_load_unmapped_fields_are_ignored(mapped_conn):
    mapping = {
        'ReportCode': 'code',
        'Station': 'station',
        'Quality': 'quality',
    }
    row = {
        'code': 'RC-1', 'station': 'ST-A', 'quality': 'good',
        VERSION_FIELD: 'v1', 'report_code': 'DIAG', 'extra': 'ignored',
    }

    processed, failed = load('sensor_data', mapped_conn, [row],
                             column_mapping=mapping,
                             version_field=VERSION_FIELD)

    assert (processed, failed) == (1, 0)
    assert fetchall("SELECT * FROM sensor_data", mapped_conn) == [
        {'ReportCode': 'RC-1', 'Station': 'ST-A', 'Quality': 'good'},
    ]


def test_mapped_load_rejection_without_on_error_is_counted(mapped_conn):
    processed, failed = load(
        'sensor_data', mapped_conn, mixed_stream(),
        column_mapping=MAPPING, version_field=VERSION_FIELD)

    assert (processed, failed) == (3, 1)
    assert len(fetchall("SELECT * FROM sensor_data", mapped_conn)) == 2


def test_mapped_load_empty_stream(mapped_conn):
    assert load('sensor_data', mapped_conn, iter([]),
                column_mapping=MAPPING,
                version_field=VERSION_FIELD) == (0, 0)


def test_mapped_load_requires_dictionary_rows(mapped_conn):
    with pytest.raises(ETLHelperInsertError, match="dictionaries"):
        load('sensor_data', mapped_conn, [('RC-1', 'ST-A', 'good')],
             column_mapping=MAPPING, version_field=VERSION_FIELD)


def test_generate_mapped_insert_sql_uses_positional_placeholders(mapped_conn):
    sql = generate_mapped_insert_sql(
        'sensor_data', ('ReportCode', 'Station', 'Quality'), mapped_conn)

    assert sql == ("INSERT INTO sensor_data (ReportCode, Station, Quality) "
                   "VALUES (?, ?, ?)")


def test_default_load_path_is_unchanged(mapped_conn):
    rows = [
        {'ReportCode': 'RC-1', 'Station': 'ST-A', 'Quality': 'good'},
        {'ReportCode': 'RC-2', 'Station': 'ST-B', 'Quality': None},
    ]

    processed, failed = load('sensor_data', mapped_conn, rows)

    assert (processed, failed) == (2, 0)
    assert fetchall("SELECT * FROM sensor_data", mapped_conn) == rows
