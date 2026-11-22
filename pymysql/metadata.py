"""Column metadata snapshots and source schema directories.

The MySQL wire protocol sends a column descriptor packet for every result
column.  :class:`~pymysql.protocol.FieldDescriptorPacket` keeps the decoded
display strings as well as the original identifier bytes.  This module turns
those descriptors into:

* :class:`ColumnMetadata` -- an immutable, result-order snapshot of the raw
  source identifiers plus the protocol metadata, kept separately from the
  DB-API ``description``.
* :class:`SourceDirectory` -- a caller-provided catalog describing the real
  schema objects an agent may read.  Declarations are matched on the raw
  identifier bytes as sent on the wire, so identifiers that decode to the same
  display string under the connection encoding are never conflated.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ColumnMetadata:
    """Snapshot of one result column, ordered as the columns appear in a result.

    The ``*_bytes`` attributes hold the exact identifier bytes from the column
    descriptor packet and are used for source matching.  The display
    attributes hold the strings decoded with the connection encoding and are
    suitable for human-facing output.
    """

    position: int
    catalog_bytes: bytes
    db_bytes: bytes
    table_name_bytes: bytes
    org_table_bytes: bytes
    name_bytes: bytes
    org_name_bytes: bytes
    catalog: str
    db: str
    table_name: str
    org_table: str
    name: str
    org_name: str
    charsetnr: int
    length: int
    column_length: int
    type_code: int
    flags: int
    scale: int

    @property
    def source_key(self):
        """Raw-byte identity of the column's origin table and column.

        Expression columns carry empty ``org_table``/``org_name`` identifiers,
        in which case ``None`` is returned because they have no table source.
        """
        if not self.org_table_bytes or not self.org_name_bytes:
            return None
        return (
            self.catalog_bytes,
            self.db_bytes,
            self.org_table_bytes,
            self.org_name_bytes,
        )

    @classmethod
    def from_field(cls, field, position, encoding):
        def as_bytes(value):
            if value is None:
                return b""
            return bytes(value)

        def decode(value):
            if value is None:
                return None
            return value.decode(encoding)

        return cls(
            position=position,
            catalog_bytes=as_bytes(field.catalog),
            db_bytes=as_bytes(field.db),
            table_name_bytes=as_bytes(field.table_name_bytes),
            org_table_bytes=as_bytes(field.org_table_bytes),
            name_bytes=as_bytes(field.name_bytes),
            org_name_bytes=as_bytes(field.org_name_bytes),
            catalog=decode(field.catalog),
            db=decode(field.db),
            table_name=field.table_name,
            org_table=field.org_table,
            name=field.name,
            org_name=field.org_name,
            charsetnr=field.charsetnr,
            length=field.length,
            column_length=field.get_column_length(),
            type_code=field.type_code,
            flags=field.flags,
            scale=field.scale,
        )


class SourceDirectory:
    """Catalog of source schema declarations keyed by raw identifier bytes.

    Keys are ``(catalog, database, original_table, original_column)`` tuples
    where every element is :class:`bytes`, matching
    :attr:`ColumnMetadata.source_key`.  Values are opaque declaration objects
    supplied by the caller describing the destination column, for example a
    target type definition produced by an extractor.

    Matching is deliberately byte based: two identifiers that decode to the
    same string under the connection encoding remain distinct entries.
    """

    def __init__(self, declarations=None, version=None):
        self._declarations = {}
        self.version = version
        if declarations:
            for key, declaration in declarations.items():
                self._declarations[self._normalize_key(key)] = declaration

    @staticmethod
    def _normalize_key(key):
        catalog, database, table, column = key
        return (bytes(catalog), bytes(database), bytes(table), bytes(column))

    def add(self, key, declaration):
        self._declarations[self._normalize_key(key)] = declaration

    def get_declaration(self, column_metadata):
        """Return the declaration for a column, or ``None`` when unconfirmed.

        Columns without a table source (expressions) and columns whose source
        is not present in this directory resolve to ``None`` rather than
        borrowing a declaration from another column with the same spelling.
        """
        key = column_metadata.source_key
        if key is None:
            return None
        return self._declarations.get(key)

    def keys(self):
        return self._declarations.keys()

    def __contains__(self, key):
        return self._normalize_key(key) in self._declarations

    def __len__(self):
        return len(self._declarations)


def build_column_metadata(fields, encoding):
    """Build an in-order tuple of :class:`ColumnMetadata` from field packets."""
    return tuple(
        ColumnMetadata.from_field(field, position, encoding)
        for position, field in enumerate(fields)
    )


def resolve_declarations(column_metadata, source_directory):
    """Resolve one declaration per snapshot column, in result-column order.

    Returns a tuple aligned with ``column_metadata``: each entry is the
    declaration confirmed by ``source_directory`` for the corresponding
    column, or ``None`` when the column is an expression or its source is not
    present in the directory.
    """
    if source_directory is None:
        return tuple(None for _ in column_metadata)
    if not isinstance(source_directory, SourceDirectory):
        raise TypeError(
            "source_directory must be a SourceDirectory instance, got "
            f"{type(source_directory).__name__}"
        )
    return tuple(source_directory.get_declaration(column) for column in column_metadata)
