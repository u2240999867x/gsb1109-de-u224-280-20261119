"""
Row factories are functions that process row data as it comes from the database.
These are applied by the iter_rows function.

A row_factory function must:
  + accept a cursor object as an input
  + only use methods on the cursor that are described by DBAPI
  + return a function that takes a tuple

"""
import re
from collections import namedtuple
from typing import (
    NamedTuple,
    Optional,
)
from warnings import warn

from etlhelper.exceptions import (
    ETLHelperAmbiguousColumnError,
    ETLHelperUnknownColumnError,
)


class Column(NamedTuple):
    """
    Metadata describing a single column of a query result.

    Each Column records the provenance of one value in a row: its position
    in the original cursor description, its name as reported by the
    database server and the attribute name used on the row namedtuple at
    the time the row factory was created.  The optional role records a
    business role assigned when the column is projected into an archive.
    """
    position: int
    name: str
    field: str
    role: Optional[str] = None


class Columns:
    """
    Immutable snapshot of column metadata for a single query result.

    A Columns object is created from cursor.description when a row factory
    is initialised.  It is independent of the cursor, so it can still
    explain each value in a row after the cursor has been updated or
    closed, and it is unaffected by any other cursor.  Columns objects
    from different queries are never merged automatically: same-named
    columns from different queries keep their own provenance unless the
    caller supplies an explicit role mapping via project().
    """

    def __init__(self, columns):
        self._columns = tuple(columns)

    @classmethod
    def from_description(cls, description, fields):
        """
        Create Columns from a DBAPI cursor description and the field names
        used on the row namedtuple.

        :param description: DBAPI cursor description sequence
        :param fields: namedtuple field names, one per description entry
        """
        return cls(
            Column(position=index, name=column[0], field=fields[index])
            for index, column in enumerate(description)
        )

    def __len__(self):
        return len(self._columns)

    def __iter__(self):
        return iter(self._columns)

    def __getitem__(self, index):
        return self._columns[index]

    def __eq__(self, other):
        if isinstance(other, Columns):
            return self._columns == other._columns
        return NotImplemented

    def __repr__(self):
        return f"Columns({self._columns!r})"

    @property
    def names(self):
        """Column names as reported by the database server."""
        return tuple(column.name for column in self._columns)

    @property
    def fields(self):
        """Attribute names used on the row namedtuple."""
        return tuple(column.field for column in self._columns)

    @property
    def positions(self):
        """Positions of columns in the original cursor description."""
        return tuple(column.position for column in self._columns)

    def by_position(self, position):
        """
        Return the Column that came from the given position in the original
        cursor description.

        :param position: zero-based position in the original description
        :raises ETLHelperUnknownColumnError: if no column came from position
        """
        for column in self._columns:
            if column.position == position:
                return column
        msg = f"No column from position {position} (positions: {self.positions})"
        raise ETLHelperUnknownColumnError(msg)

    def by_name(self, name):
        """
        Return the unique Column matching a server column name or a
        namedtuple field name (a confirmed alias).

        :param name: server column name or namedtuple field name
        :raises ETLHelperAmbiguousColumnError: if multiple columns match
        :raises ETLHelperUnknownColumnError: if no column matches
        """
        matches = [column for column in self._columns
                   if name in (column.name, column.field)]
        if len(matches) > 1:
            positions = [column.position for column in matches]
            msg = (f"Column name {name!r} is ambiguous: it matches columns "
                   f"from positions {positions}.  Identify the column by "
                   "its original position or a confirmed alias instead.")
            raise ETLHelperAmbiguousColumnError(msg)
        if not matches:
            msg = f"Unknown column {name!r} (columns: {self.names})"
            raise ETLHelperUnknownColumnError(msg)
        return matches[0]

    def project(self, positions, roles=None):
        """
        Return a new Columns containing the columns at the given original
        positions, in the given order.

        Positions may be repeated to use the same source column more than
        once (e.g. as both a body and a reconciliation column); each
        projected column keeps the position, server name and field name of
        its origin, so shared origins remain visible.  Optional roles
        assign a business role to each projected column.

        :param positions: original positions to project, in output order
        :param roles: optional business role for each projected column
        :raises ETLHelperUnknownColumnError: if a position is not found
        """
        positions = list(positions)
        if roles is None:
            roles = [None] * len(positions)
        else:
            roles = list(roles)
            if len(roles) != len(positions):
                msg = (f"Number of roles ({len(roles)}) does not match "
                       f"number of positions ({len(positions)})")
                raise ValueError(msg)

        return Columns(
            self.by_position(position)._replace(role=role)
            for position, role in zip(positions, roles)
        )


def namedtuple_row_factory(cursor):
    """
    Return function to convert output row to a named tuple.

    Named tuples allow access to attributes via both position (e.g. row[0]) or
    name (using dot notation, e.g. row.id).  They are immutable, so cannot be
    modified directly in transform functions.  Insert statements based on named
    tuples must use positional placeholders for parameters (e.g. ?, :1, %s).

    A Columns snapshot of the column metadata, taken from the cursor
    description when this factory is created, is attached to the row class
    as `_columns` (also visible on each row).  It records the original
    position, server name and namedtuple field name of each column and
    remains valid after the cursor has been updated or closed.
    """
    column_names = [d[0] for d in cursor.description]

    try:
        Row = namedtuple('Row', field_names=column_names)
    except ValueError:
        Row = namedtuple('Row', field_names=column_names, rename=True)
        renamed_columns = _find_renamed_columns(Row, column_names)
        warn("One or more columns have been renamed. Names that cannot be "
             "converted to namedtuple attributes are replaced by indices. To "
             "prevent column renaming, either provide alias in SQL query, "
             "e.g. 'SELECT count(*) AS c', or use dict_row_factory. ")
        warn(f"{renamed_columns}")

    # Snapshot column metadata so rows can be interpreted independently of
    # the cursor.  Each call creates a new Row class, so concurrent result
    # sets each carry their own metadata.
    Row._columns = Columns.from_description(cursor.description, Row._fields)

    def create_row(row):
        return Row(*row)

    return create_row


def dict_row_factory(cursor):
    """
    Return function to convert output row to a dictionary.

    Dictionaries allow access to attributes via name (using key notation, e.g.
    row["id"].  They are mutable, so are convenient to modify directly in
    transform functions.  Insert statements based on dictionaries must use
    named placeholders for parameters (e.g. :id, %(id)s).
    """
    column_names = [d[0] for d in cursor.description]

    def create_row(row):
        row_dict = {}
        for i, column_name in enumerate(column_names):
            row_dict[column_name] = row[i]
        return row_dict

    return create_row


def tuple_row_factory(cursor):
    """
    Return function to convert output row to a tuple.

    Tuples allow access to attributes via position (e.g. row[0]).  They are
    immutable, so cannot be modified directly in transform functions.  Insert
    statements based on tuples must use positional placeholders for parameters
    (e.g. ?, :1, %s).

    As the DBAPI default is already to return rows as tuples, using the tuple
    row factory minimises processing overhead.
    """
    def create_row(row):
        return row

    return create_row


def list_row_factory(cursor):
    """
    Return function to convert output row to a list.

    Lists allow access to attributes via position (e.g. row[0]).  They are
    mutable, so are convient to modify directly in transform functions.  Insert
    statements based on lists must use positional placeholders for parameters
    (e.g. ?, :1, %s).
    """
    def create_row(row):
        return list(row)

    return create_row


def _find_renamed_columns(row_class, column_names):
    regex = re.compile(r'^_\d+$')

    renamed_column_ids = [int(f.replace("_", "")) for f in row_class._fields if regex.match(f)]

    result = '\n'.join(f'{column_names[idx]} was renamed to {row_class._fields[idx]}'
                       for idx in renamed_column_ids)

    return result
