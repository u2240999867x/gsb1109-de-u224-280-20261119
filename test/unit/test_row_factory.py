"""
Tests for row factory methods.
"""
import sqlite3
from unittest import mock

import pytest

from etlhelper.exceptions import (
    ETLHelperAmbiguousColumnError,
    ETLHelperUnknownColumnError,
)
from etlhelper.row_factories import (
    Column,
    Columns,
    namedtuple_row_factory,
)


FAKE_ROWS = [(1, 2, 3), (4, 5, 6)]


def test_valid_field_names(mock_cursor):
    mock_cursor.description = (('id', None, None, None, None, None, None),
                               ('name', None, None, None, None, None, None),
                               ('desc', None, None, None, None, None, None))

    create_row = namedtuple_row_factory(mock_cursor)
    rows = [create_row(row) for row in mock_cursor.fetchall()]

    assert rows[0]._fields == ("id", "name", "desc")
    assert rows == FAKE_ROWS


def test_invalid_field_names(mock_cursor):
    mock_cursor.description = (('id', None, None, None, None, None, None),
                               ('count(*)', None, None, None, None, None, None),
                               ('spaced column', None, None, None, None, None, None))

    with pytest.warns(UserWarning) as warn:
        create_row = namedtuple_row_factory(mock_cursor)
        assert len(warn) == 2
        assert (warn[1].message.args[0]
                == 'count(*) was renamed to _1\nspaced column was renamed to _2')

    rows = [create_row(row) for row in mock_cursor.fetchall()]

    assert rows[0]._fields == ('id', '_1', '_2')
    assert rows == FAKE_ROWS


def test_columns_metadata_snapshot(mock_cursor):
    """Rows carry a snapshot of column metadata taken from the cursor."""
    mock_cursor.description = (('id', None, None, None, None, None, None),
                               ('name', None, None, None, None, None, None),
                               ('desc', None, None, None, None, None, None))

    create_row = namedtuple_row_factory(mock_cursor)
    row = create_row(FAKE_ROWS[0])

    columns = row._columns
    assert isinstance(columns, Columns)
    assert columns.names == ('id', 'name', 'desc')
    assert columns.fields == ('id', 'name', 'desc')
    assert columns.positions == (0, 1, 2)
    assert columns[1] == Column(position=1, name='name', field='name', role=None)

    # Metadata is unchanged when the cursor description is updated or the
    # cursor is closed.
    mock_cursor.description = (('other', None, None, None, None, None, None),)
    mock_cursor.close()
    assert row._columns.names == ('id', 'name', 'desc')

    # Old-style access still works: attributes, positions and unpacking.
    assert row.id == 1
    assert row[1] == 2
    assert row._asdict() == {'id': 1, 'name': 2, 'desc': 3}
    id_, name, desc = row
    assert (id_, name, desc) == (1, 2, 3)


def test_duplicate_column_names(mock_cursor):
    """Same-named columns keep their own position, server name and field."""
    mock_cursor.description = (('count', None, None, None, None, None, None),
                               ('Site Name', None, None, None, None, None, None),
                               ('count', None, None, None, None, None, None))

    with pytest.warns(UserWarning):
        create_row = namedtuple_row_factory(mock_cursor)

    row = create_row((10, 'site-a', 99))
    columns = row._columns

    assert columns.names == ('count', 'Site Name', 'count')
    assert columns.fields == ('count', '_1', '_2')
    assert columns.positions == (0, 1, 2)

    # Each count resolves to its own provenance via position or renamed field.
    assert columns.by_position(0) == Column(0, 'count', 'count')
    assert columns.by_position(2) == Column(2, 'count', '_2')
    assert columns.by_name('_2') == Column(2, 'count', '_2')
    assert columns.by_name('Site Name') == Column(1, 'Site Name', '_1')


def test_ambiguous_column_name(mock_cursor):
    """A bare duplicated name without position or confirmed alias is rejected."""
    mock_cursor.description = (('count', None, None, None, None, None, None),
                               ('Site Name', None, None, None, None, None, None),
                               ('count', None, None, None, None, None, None))

    with pytest.warns(UserWarning):
        create_row = namedtuple_row_factory(mock_cursor)

    columns = create_row((10, 'site-a', 99))._columns

    with pytest.raises(ETLHelperAmbiguousColumnError):
        columns.by_name('count')

    with pytest.raises(ETLHelperUnknownColumnError):
        columns.by_name('missing')

    with pytest.raises(ETLHelperUnknownColumnError):
        columns.by_position(5)


def test_project_reorders_by_original_position(mock_cursor):
    """Reordering by explicit original positions preserves provenance."""
    mock_cursor.description = (('count', None, None, None, None, None, None),
                               ('Site Name', None, None, None, None, None, None),
                               ('count', None, None, None, None, None, None))

    with pytest.warns(UserWarning):
        create_row = namedtuple_row_factory(mock_cursor)

    columns = create_row((10, 'site-a', 99))._columns

    # Settlement archive swaps the two count columns.
    archive = columns.project([2, 1, 0])
    assert archive.positions == (2, 1, 0)
    assert archive.names == ('count', 'Site Name', 'count')
    assert archive.fields == ('_2', '_1', 'count')
    assert archive[0] == Column(2, 'count', '_2')
    assert archive[2] == Column(0, 'count', 'count')

    # Projected columns can still be looked up by their original position.
    assert archive.by_position(2).field == '_2'
    assert archive.by_position(0).field == 'count'


def test_project_same_position_twice_with_roles(mock_cursor):
    """Projecting one source position twice shows the shared origin."""
    mock_cursor.description = (('count', None, None, None, None, None, None),
                               ('Site Name', None, None, None, None, None, None),
                               ('count', None, None, None, None, None, None))

    with pytest.warns(UserWarning):
        create_row = namedtuple_row_factory(mock_cursor)

    columns = create_row((10, 'site-a', 99))._columns

    # Another archive uses the anomaly count as both body and
    # reconciliation column.
    archive = columns.project([2, 1, 2], roles=['body', 'name', 'reconciliation'])
    assert archive.positions == (2, 1, 2)
    assert archive[0] == Column(2, 'count', '_2', role='body')
    assert archive[2] == Column(2, 'count', '_2', role='reconciliation')
    # Same origin: identical provenance, differing only in assigned role.
    assert archive[0]._replace(role=None) == archive[2]._replace(role=None)

    # Roles must match positions one-to-one.
    with pytest.raises(ValueError):
        columns.project([0, 1], roles=['only-one'])

    with pytest.raises(ETLHelperUnknownColumnError):
        columns.project([0, 7])


def test_interleaved_streams_keep_independent_metadata():
    """
    Two independent result streams consumed interleaved each keep their own
    column metadata; updating or closing one cursor never affects the other.
    """
    conn = sqlite3.connect(':memory:')
    cursor_a = conn.cursor()
    cursor_b = conn.cursor()

    # Query A: count, Site Name, count (sample count, name, anomaly count).
    cursor_a.execute('SELECT 10 AS count, \'site-a\' AS "Site Name", 99 AS count'
                     ' UNION ALL SELECT 11, \'site-b\', 98')
    # Query B: count, count, Site Name (order defined by its own release notes).
    cursor_b.execute('SELECT 5 AS count, 7 AS count, \'site-c\' AS "Site Name"'
                     ' UNION ALL SELECT 6, 8, \'site-d\'')

    with pytest.warns(UserWarning):
        create_row_a = namedtuple_row_factory(cursor_a)
    with pytest.warns(UserWarning):
        create_row_b = namedtuple_row_factory(cursor_b)

    # Interleaved consumption: first stream yields one row then closes.
    row_a = create_row_a(cursor_a.fetchone())
    row_b1 = create_row_b(cursor_b.fetchone())
    cursor_a.close()

    # Second stream continues to the next chunk of rows.
    row_b2 = create_row_b(cursor_b.fetchone())
    cursor_b.close()
    conn.close()

    # Metadata survives cursor close and reflects each query's own layout.
    columns_a = row_a._columns
    columns_b = row_b1._columns
    assert columns_a.names == ('count', 'Site Name', 'count')
    assert columns_a.fields == ('count', '_1', '_2')
    assert columns_b.names == ('count', 'count', 'Site Name')
    assert columns_b.fields == ('count', '_1', '_2')

    # The two streams' metadata are independent objects and are not merged
    # despite sharing column names.
    assert columns_a is not columns_b
    assert row_b2._columns is columns_b
    assert columns_a != columns_b
    assert columns_a.by_position(1).name == 'Site Name'
    assert columns_b.by_position(1).name == 'count'

    # Each count still maps to its own query's provenance after the cursors
    # have been closed.
    archive_a = columns_a.project([2, 1, 0])
    assert archive_a[0] == Column(2, 'count', '_2')
    assert archive_a[2] == Column(0, 'count', 'count')
    archive_b = columns_b.project([0, 1, 2], roles=['sample', 'anomaly', 'name'])
    assert archive_b[0] == Column(0, 'count', 'count', role='sample')
    assert archive_b[1] == Column(1, 'count', '_1', role='anomaly')


@pytest.fixture(scope='function')
def mock_cursor():
    # Test uses sqlite3 cursor, but all dbapi compliant cursors will be the same.
    mock_cursor = mock.MagicMock(sqlite3.Cursor)
    mock_cursor.fetchall.return_value = FAKE_ROWS
    return mock_cursor
