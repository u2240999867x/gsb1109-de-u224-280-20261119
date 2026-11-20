# Copyright 2026 Cloudera Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Exact, batch oriented table and view structure manifests.

The HiveServer2 metadata RPCs return flat result sets.  ``GetTables`` describes
which objects exist and whether they are tables or views, while ``GetColumns``
returns one row per column (and possibly one row per nested field).  Those
rows may be interleaved, may arrive in any order, and may not carry every part
of an object's identity.

This module turns those rows into a small, explicit manifest.  It never fills
gaps by guessing: a missing catalog, a contradictory declaration, or an object
that was only seen in ``GetTables`` is represented as such.  In particular an
empty column list is not evidence that an object has zero columns.
"""

from __future__ import absolute_import

from collections import OrderedDict
from dataclasses import dataclass, field

# Object level states.
KNOWN = 'known'
PARTIAL = 'partial'
CONFLICT = 'conflict'
EXISTS_ONLY = 'exists_only'
AMBIGUOUS = 'ambiguous'
MISSING = 'missing'

# Column and nested field states.
COLUMN_KNOWN = 'known'
COLUMN_CONFLICT = 'conflict'
COLUMN_PENDING = 'pending'
COLUMN_UNRESOLVED = 'unresolved'

# Issue kinds.  They are deliberately stable strings so callers can decide
# whether a manifest is usable without parsing prose.
ISSUE_MISSING_CATALOG = 'missing_catalog'
ISSUE_TYPE_CONFLICT = 'type_conflict'
ISSUE_TABLE_TYPE_CONFLICT = 'table_type_conflict'
ISSUE_EXISTS_ONLY = 'exists_only'
ISSUE_PENDING_NESTED_TYPE = 'pending_nested_type'
ISSUE_MISSING_NESTED_PARENT = 'missing_nested_parent'
ISSUE_MISSING_TABLE = 'missing_table'
ISSUE_UNMATCHED_ROW = 'unmatched_row'
ISSUE_COLUMN_NAME_CONFLICT = 'column_name_conflict'


_GET_TABLES_LAYOUT = {
    'catalog': 0,
    'schema': 1,
    'name': 2,
    'table_type': 3,
    'remarks': 4,
}

_GET_COLUMNS_LAYOUT = {
    'catalog': 0,
    'schema': 1,
    'table': 2,
    'name': 3,
    'data_type': 4,
    'type_name': 5,
    'column_size': 6,
    'decimal_digits': 8,
    'nullable': 10,
    'remarks': 11,
    'ordinal': 16,
    'is_nullable': 17,
    # Servers that expose explicit nesting information may append these.
    'parent_path': 18,
    'is_nested': 19,
}


def _as_text(value):
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode('utf-8', 'replace')
    return str(value)


def _as_int(value):
    if value is None or value == '':
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value):
    if value is None or value == '':
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ('yes', 'y', 'true', 't', '1'):
        return True
    if text in ('no', 'n', 'false', 'f', '0'):
        return False
    return None


def _as_attr(value):
    """Return True if *value* exposes attribute style metadata fields."""
    if value is None or isinstance(value, (str, bytes, dict, list, tuple)):
        return False
    return any(hasattr(value, name) for name in (
        'columnName', 'column_name', 'tableName', 'table_name'))


def _attr_get(row, *names):
    for name in names:
        if hasattr(row, name):
            value = getattr(row, name)
            if value is not None:
                return value
    return None


def _dict_get(row, *names):
    lowered = {str(key).lower(): value for key, value in row.items()}
    for name in names:
        if name.lower() in lowered:
            return lowered[name.lower()]
    return None


def _field(row, layout, name, default=None, aliases=()):
    """Read a metadata field from a dict-like row or an ODBC result tuple."""
    if row is None:
        return default
    if isinstance(row, dict):
        value = _dict_get(row, name, *aliases)
        return default if value is None else value
    if _as_attr(row):
        attr_aliases = {
            'catalog': ('tableCat', 'table_cat', 'catalogName',
                        'catalog_name'),
            'schema': ('tableSchem', 'table_schem', 'schemaName',
                       'schema_name'),
            'name': ('columnName', 'column_name', 'tableName', 'table_name'),
            'table': ('tableName', 'table_name'),
            'table_type': ('tableType', 'table_type', 'type'),
            'remarks': ('remarks', 'comment'),
            'data_type': ('dataType', 'data_type'),
            'type_name': ('typeName', 'type_name'),
            'column_size': ('columnSize', 'column_size', 'precision'),
            'decimal_digits': ('decimalDigits', 'decimal_digits', 'scale'),
            'nullable': ('nullable', 'isNullable', 'is_nullable'),
            'ordinal': ('ordinalPosition', 'ordinal_position', 'position'),
        }
        value = _attr_get(row, name, *aliases + attr_aliases.get(name, ()))
        return default if value is None else value
    index = layout.get(name)
    if index is None or index >= len(row):
        return default
    return row[index]


def _norm(value):
    value = _as_text(value)
    return value.casefold() if value is not None else None


def _same(left, right, case_sensitive=False):
    if left is None or right is None:
        return left is right
    if case_sensitive:
        return left == right
    return _norm(left) == _norm(right)


def literal_pattern(name):
    """Return a pattern that treats *name* as literal server input.

    HiveServer2 accepts either identifiers or search patterns in the metadata
    RPCs.  The exact spelling differs between servers, so callers should still
    compare returned rows against the requested identity.  Escaping here keeps
    a literal ``%``, ``_`` or ``*`` in a user supplied name from becoming a
    wildcard on servers that interpret those characters.
    """
    if name is None:
        return None
    escaped = []
    for char in name:
        if char in ('\\', '%', '_', '*', '?', '[', ']'):
            escaped.append('\\')
        escaped.append(char)
    return ''.join(escaped)


@dataclass(frozen=True)
class TableTarget(object):
    """The three-part identity requested by the caller.

    ``catalog`` or ``schema`` may be ``None`` when the caller wants the
    connection default.  ``name`` is always literal, never a pattern.
    """

    catalog: object = None
    schema: object = None
    name: object = None

    @property
    def identity(self):
        return (self.catalog, self.schema, self.name)

    @property
    def qualified_name(self):
        parts = [p for p in (self.catalog, self.schema, self.name)
                 if p is not None]
        return '.'.join(str(p) for p in parts)

    def matches(self, catalog, schema, name, case_sensitive=False,
                ignore_catalog=False, ignore_schema=False):
        if not _same(self.name, name, case_sensitive):
            return False
        if not ignore_schema and self.schema is not None:
            if not _same(self.schema, schema, case_sensitive):
                return False
        if not ignore_catalog and self.catalog is not None:
            if not _same(self.catalog, catalog, case_sensitive):
                return False
        return True

    def to_dict(self):
        return {
            'catalog': self.catalog,
            'schema': self.schema,
            'name': self.name,
            'qualified_name': self.qualified_name,
        }


def _split_identifier(value):
    """Split ``catalog.schema.name`` while respecting quoted components."""
    text = _as_text(value)
    if text is None:
        return []
    parts = []
    current = []
    quote = None
    index = 0
    while index < len(text):
        char = text[index]
        if quote is not None:
            if char == quote:
                if index + 1 < len(text) and text[index + 1] == quote:
                    current.append(char)
                    index += 2
                    continue
                quote = None
            else:
                current.append(char)
        elif char in ('`', '"'):
            quote = char
        elif char == '.':
            parts.append(''.join(current))
            current = []
        else:
            current.append(char)
        index += 1
    parts.append(''.join(current))
    return parts


def _target_from_mapping(value, defaults):
    lowered = {str(k).lower(): v for k, v in value.items()}

    def pick(*names):
        for name in names:
            if name in lowered and lowered[name] is not None:
                return lowered[name]
        return None

    catalog = pick('catalog', 'catalog_name', 'table_cat', 'cat')
    schema = pick('schema', 'schema_name', 'table_schem', 'db')
    if schema is None:
        schema = pick('database', 'database_name')
    name = pick('name', 'table', 'table_name', 'object', 'object_name',
                'view', 'view_name')
    if name is None:
        raise ValueError('metadata target is missing an object name: %r' % value)
    if schema is None:
        schema = defaults.get('schema')
    if catalog is None:
        catalog = defaults.get('catalog')
    return TableTarget(catalog=catalog, schema=schema, name=name)


def normalize_targets(targets, catalog_name=None, schema_name=None,
                      database_name=None, case_sensitive=False):
    """Normalize one or more target descriptions into :class:`TableTarget`s.

    Accepted forms are:

    * a string ``"catalog.schema.table"``, ``"schema.table"`` or ``"table"``
    * a three item tuple ``(catalog, schema, table)``
    * a mapping with ``catalog``/``schema``/``name`` (common aliases such as
      ``table``, ``database`` and ``object`` are accepted)
    * a list of any of the above

    A tuple of three plain values is always one target; a list is always a
    batch, even when it happens to contain three names.  This keeps a batch of
    three objects from being misread as one catalog/schema/name triple.

    ``database_name`` supplies the default Hive schema and ``catalog_name``
    supplies the default catalog.  A name component is retained exactly as
    supplied; matching is case insensitive by default, as in Hive.
    """
    defaults = {
        'catalog': catalog_name,
        'schema': database_name if database_name is not None else schema_name,
    }

    # A bare tuple is one target, not a batch: ``(catalog, schema, table)`` or
    # ``(schema, table)``.  A list is always a batch, even when it has exactly
    # three string entries.
    if isinstance(targets, tuple) and len(targets) in (2, 3) and all(
            not isinstance(item, (dict, list, tuple)) for item in targets):
        targets = [targets]
    elif isinstance(targets, (str, bytes, dict, TableTarget)):
        targets = [targets]
    elif targets is None:
        targets = []

    result = []
    for target in targets:
        if isinstance(target, TableTarget):
            result.append(target)
            continue
        if isinstance(target, dict):
            result.append(_target_from_mapping(target, defaults))
            continue
        if isinstance(target, (tuple, list)):
            if len(target) == 3:
                catalog, schema, name = target
            elif len(target) == 2:
                catalog = None
                schema, name = target
            elif len(target) == 1:
                catalog, schema, name = None, None, target[0]
            else:
                raise ValueError(
                    'metadata target sequence must have 1, 2 or 3 items: %r'
                    % (target,))
            result.append(TableTarget(
                catalog=catalog if catalog is not None else defaults['catalog'],
                schema=schema if schema is not None else defaults['schema'],
                name=name,
            ))
            continue
        if isinstance(target, (bytes, bytearray)):
            target = bytes(target).decode('utf-8', 'replace')
        if not isinstance(target, str):
            raise ValueError('unsupported metadata target: %r' % (target,))

        parts = _split_identifier(target)
        if len(parts) == 3:
            catalog, schema, name = parts
        elif len(parts) == 2:
            catalog = defaults['catalog']
            schema, name = parts
        elif len(parts) == 1:
            catalog, schema, name = defaults['catalog'], defaults['schema'], parts[0]
        else:
            raise ValueError('cannot parse metadata target: %r' % (target,))
        result.append(TableTarget(catalog=catalog, schema=schema, name=name))

    # Keep the requested order but do not emit the same object twice when a
    # caller repeats a target in the batch.
    deduped = []
    seen = set()
    for target in result:
        key = tuple(_norm(part) if not case_sensitive else part
                    for part in target.identity)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(target)
    return deduped


@dataclass
class Declaration(object):
    """One metadata row's claim about a column."""

    name: object
    ordinal: object = None
    type_name: object = None
    data_type: object = None
    column_size: object = None
    decimal_digits: object = None
    nullable: object = None
    catalog: object = None
    schema: object = None
    table: object = None
    source: str = 'GetColumns'
    parent_path: object = None
    is_nested: object = None

    @property
    def type_range(self):
        return _type_range(self.type_name, self.column_size,
                           self.decimal_digits)

    @property
    def explicit_path(self):
        """The parent path when the wire row declares one separately."""
        if self.parent_path is not None:
            return _as_text(self.parent_path)
        name = _as_text(self.name)
        if name is not None and '.' in name:
            parts = name.split('.')
            return '.'.join(parts[:-1]) or None
        return None

    def key(self):
        return (self.explicit_path, self.name, self.type_name,
                self.column_size, self.decimal_digits)

    def to_dict(self):
        return {
            'name': self.name,
            'ordinal': self.ordinal,
            'type': self.type_name,
            'type_name': self.type_name,
            'type_range': self.type_range,
            'data_type': self.data_type,
            'column_size': self.column_size,
            'decimal_digits': self.decimal_digits,
            'nullable': self.nullable,
            'catalog': self.catalog,
            'schema': self.schema,
            'table': self.table,
            'source': self.source,
        }


def _type_range(type_name, column_size=None, decimal_digits=None):
    if type_name is None:
        return None
    type_name = _as_text(type_name)
    upper = type_name.upper()
    if upper.startswith('DECIMAL') or upper.startswith('NUMERIC'):
        if column_size is not None and decimal_digits is not None:
            return '%s(%s,%s)' % (type_name, column_size, decimal_digits)
        if column_size is not None:
            return '%s(%s)' % (type_name, column_size)
    if column_size is not None and upper in (
            'CHAR', 'VARCHAR', 'BINARY', 'VARBINARY', 'STRING'):
        return '%s(%s)' % (type_name, column_size)
    return type_name


class _MappingMixin(object):
    """Make manifest records usable as both objects and small mappings."""

    def __getitem__(self, key):
        if isinstance(key, int) and hasattr(self, 'columns'):
            return self.columns[key]
        try:
            return getattr(self, key)
        except AttributeError:
            raise KeyError(key)

    def get(self, key, default=None):
        try:
            return self[key]
        except (KeyError, IndexError):
            return default

    def keys(self):
        return self.to_dict().keys()

    def values(self):
        return self.to_dict().values()

    def items(self):
        return self.to_dict().items()

    def __contains__(self, key):
        return key in self.to_dict()


@dataclass
class NestedField(_MappingMixin):
    path: object
    name: object
    ordinal: object = None
    type_name: object = None
    data_type: object = None
    column_size: object = None
    decimal_digits: object = None
    nullable: object = None
    status: str = COLUMN_KNOWN
    declarations: object = field(default_factory=list)
    reasons: object = field(default_factory=list)

    @property
    def type_range(self):
        return _type_range(self.type_name, self.column_size,
                           self.decimal_digits)

    @property
    def type_known(self):
        return self.type_name is not None and self.status == COLUMN_KNOWN

    @property
    def confirmed(self):
        return self.status == COLUMN_KNOWN

    def to_dict(self):
        return {
            'path': self.path,
            'name': self.name,
            'ordinal': self.ordinal,
            'type': self.type_name,
            'type_name': self.type_name,
            'type_range': self.type_range,
            'type_known': self.type_known,
            'status': self.status,
            'confirmed': self.confirmed,
            'reasons': list(self.reasons),
            'declarations': [d.to_dict() for d in self.declarations],
        }


@dataclass
class ColumnDefinition(_MappingMixin):
    name: object
    ordinal: object = None
    type_name: object = None
    data_type: object = None
    column_size: object = None
    decimal_digits: object = None
    nullable: object = None
    status: str = COLUMN_KNOWN
    declarations: object = field(default_factory=list)
    nested: object = field(default_factory=list)
    reasons: object = field(default_factory=list)

    @property
    def type_range(self):
        return _type_range(self.type_name, self.column_size,
                           self.decimal_digits)

    @property
    def type_known(self):
        return self.type_name is not None and self.status != COLUMN_CONFLICT

    @property
    def confirmed(self):
        return self.status == COLUMN_KNOWN

    @property
    def nested_fields(self):
        return self.nested

    def to_dict(self):
        return {
            'name': self.name,
            'ordinal': self.ordinal,
            'type': self.type_name,
            'type_name': self.type_name,
            'type_range': self.type_range,
            'type_known': self.type_known,
            'data_type': self.data_type,
            'column_size': self.column_size,
            'decimal_digits': self.decimal_digits,
            'nullable': self.nullable,
            'status': self.status,
            'confirmed': self.confirmed,
            'reasons': list(self.reasons),
            'declarations': [d.to_dict() for d in self.declarations],
            'nested': [n.to_dict() for n in self.nested],
            'nested_fields': [n.to_dict() for n in self.nested],
        }


@dataclass
class TableStructure(_MappingMixin):
    catalog: object = None
    schema: object = None
    name: object = None
    table_type: object = None
    status: str = KNOWN
    columns: object = field(default_factory=list)
    reasons: object = field(default_factory=list)
    issues: object = field(default_factory=list)
    unresolved_nested: object = field(default_factory=list)
    columns_defined: bool = False
    columns_complete: bool = False
    table_seen: bool = False

    @property
    def identity(self):
        return (self.catalog, self.schema, self.name)

    @property
    def qualified_name(self):
        return '.'.join(str(p) for p in
                        (self.catalog, self.schema, self.name)
                        if p is not None)

    @property
    def object_type(self):
        return self.table_type

    @property
    def kind(self):
        return self.table_type

    @property
    def is_view(self):
        return (self.table_type or '').upper() == 'VIEW'

    @property
    def is_table(self):
        return (self.table_type or '').upper() == 'TABLE'

    @property
    def column_count(self):
        # None means "not established".  It must not be read as zero.
        return len(self.columns) if self.columns_defined else None

    @property
    def known_columns(self):
        return [c for c in self.columns if c.status == COLUMN_KNOWN]

    @property
    def pending_columns(self):
        pending = [c for c in self.columns
                   if c.status in (COLUMN_PENDING, COLUMN_UNRESOLVED)]
        pending.extend(n for c in self.columns for n in c.nested
                       if n.status in (COLUMN_PENDING, COLUMN_UNRESOLVED))
        pending.extend(self.unresolved_nested)
        return pending

    @property
    def existence_known(self):
        return self.table_seen

    @property
    def definition_known(self):
        return self.columns_defined and not self.pending_columns

    @property
    def confirmed(self):
        return self.status == KNOWN

    @property
    def needs_confirmation(self):
        return self.status in (PARTIAL, CONFLICT, AMBIGUOUS, EXISTS_ONLY,
                               MISSING)

    def column(self, name):
        for column in self.columns:
            if _same(column.name, name):
                return column
        for nested in self.unresolved_nested:
            if _same(nested.path, name) or _same(nested.name, name):
                return nested
        return None

    def to_dict(self):
        return {
            'catalog': self.catalog,
            'schema': self.schema,
            'name': self.name,
            'identity': self.identity,
            'qualified_name': self.qualified_name,
            'table_type': self.table_type,
            'object_type': self.table_type,
            'is_table': self.is_table,
            'is_view': self.is_view,
            'status': self.status,
            'confirmed': self.confirmed,
            'needs_confirmation': self.needs_confirmation,
            'existence_known': self.existence_known,
            'definition_known': self.definition_known,
            'columns_defined': self.columns_defined,
            'columns_complete': self.columns_complete,
            'column_count': self.column_count,
            'table_seen': self.table_seen,
            'reasons': list(self.reasons),
            'issues': [dict(issue) for issue in self.issues],
            'columns': [c.to_dict() for c in self.columns],
            'unresolved_nested': [n.to_dict() for n in self.unresolved_nested],
        }


class MetadataManifest(object):
    """A batch of :class:`TableStructure` objects plus unresolved rows.

    The object supports iteration over its tables and lookup by qualified
    name, ``(catalog, schema, name)`` or the requested target.  Every table
    carries its own status so one ambiguous row cannot hide the other,
    determinable objects in the same batch.
    """

    _STRUCTURAL_KEYS = ('targets', 'tables', 'unassigned', 'issues', 'known',
                        'needs_confirmation')

    def __init__(self, targets=None, tables=None, unassigned=None,
                 issues=None):
        self.targets = list(targets or [])
        self.tables = list(tables or [])
        self.unassigned = list(unassigned or [])
        self.issues = list(issues or [])

    def __iter__(self):
        return iter(self.tables)

    def __len__(self):
        return len(self.tables)

    def __getitem__(self, key):
        if isinstance(key, int) or isinstance(key, slice):
            return self.tables[key]
        if isinstance(key, str) and key in self._STRUCTURAL_KEYS:
            return getattr(self, key)
        table = self.get(key)
        if table is None:
            raise KeyError(key)
        return table

    def __contains__(self, key):
        if isinstance(key, str) and key in self._STRUCTURAL_KEYS:
            return True
        return self.get(key) is not None

    def keys(self):
        return self._STRUCTURAL_KEYS

    def items(self):
        return [(key, getattr(self, key)) for key in self._STRUCTURAL_KEYS]

    def get(self, key, default=None):
        if isinstance(key, str) and key in self._STRUCTURAL_KEYS:
            return getattr(self, key)
        if isinstance(key, TableTarget):
            identity = key.identity
            for table in self.tables:
                if table.identity == identity:
                    return table
            return default
        if isinstance(key, (tuple, list)):
            if len(key) == 3:
                catalog, schema, name = key
            elif len(key) == 2:
                catalog, schema, name = None, key[0], key[1]
            else:
                return default
            for table in self.tables:
                if (table.name == name and
                        (schema is None or table.schema == schema) and
                        (catalog is None or table.catalog == catalog)):
                    return table
            return default
        text = _as_text(key)
        if text is None:
            return default
        for table in self.tables:
            if text in (table.qualified_name,
                        '.'.join(str(p) for p in (table.schema, table.name)
                                 if p is not None),
                        str(table.name)):
                return table
        return default

    def get_table(self, name, schema=None, catalog=None):
        return self.get((catalog, schema, name))

    @property
    def known(self):
        return [t for t in self.tables if t.status == KNOWN]

    @property
    def needs_confirmation(self):
        return [t for t in self.tables if t.needs_confirmation]

    def to_dict(self):
        return {
            'targets': [t.to_dict() for t in self.targets],
            'tables': [t.to_dict() for t in self.tables],
            'unassigned': [_manifest_item(item) for item in self.unassigned],
            'issues': [dict(item) for item in self.issues],
        }

    as_dict = to_dict


def _parse_table_row(row, row_number=None):
    # ``row_number`` is accepted for callers that enumerate rows but is not
    # part of the manifest: it would make equivalent manifests differ solely
    # because the server returned rows in a different order.
    del row_number
    return {
        'catalog': _as_text(_field(
            row, _GET_TABLES_LAYOUT, 'catalog',
            aliases=('table_cat', 'table_catalog'))),
        'schema': _as_text(_field(
            row, _GET_TABLES_LAYOUT, 'schema',
            aliases=('table_schem', 'table_schema'))),
        'name': _as_text(_field(
            row, _GET_TABLES_LAYOUT, 'name',
            aliases=('table_name',))),
        'table_type': _as_text(_field(
            row, _GET_TABLES_LAYOUT, 'table_type',
            aliases=('type',))),
        'remarks': _as_text(_field(
            row, _GET_TABLES_LAYOUT, 'remarks', aliases=('comment',))),
    }


def _parse_column_row(row, row_number=None):
    del row_number
    return Declaration(
        catalog=_as_text(_field(
            row, _GET_COLUMNS_LAYOUT, 'catalog',
            aliases=('table_cat', 'table_catalog'))),
        schema=_as_text(_field(
            row, _GET_COLUMNS_LAYOUT, 'schema',
            aliases=('table_schem', 'table_schema'))),
        table=_as_text(_field(
            row, _GET_COLUMNS_LAYOUT, 'table',
            aliases=('table_name',))),
        name=_as_text(_field(
            row, _GET_COLUMNS_LAYOUT, 'name',
            aliases=('column_name',))),
        data_type=_as_text(_field(
            row, _GET_COLUMNS_LAYOUT, 'data_type', aliases=('data_type_id',))),
        type_name=_as_text(_field(
            row, _GET_COLUMNS_LAYOUT, 'type_name', aliases=('type',))),
        column_size=_as_int(_field(
            row, _GET_COLUMNS_LAYOUT, 'column_size',
            aliases=('size', 'precision'))),
        decimal_digits=_as_int(_field(
            row, _GET_COLUMNS_LAYOUT, 'decimal_digits',
            aliases=('scale',))),
        nullable=_as_bool(_field(
            row, _GET_COLUMNS_LAYOUT, 'nullable',
            aliases=('is_nullable',))),
        ordinal=_as_int(_field(
            row, _GET_COLUMNS_LAYOUT, 'ordinal',
            aliases=('ordinal_position', 'position'))),
        source='GetColumns',
        parent_path=_as_text(_field(
            row, _GET_COLUMNS_LAYOUT, 'parent_path',
            aliases=('parent', 'parent_column', 'parent_column_name'))),
        is_nested=_as_bool(_field(
            row, _GET_COLUMNS_LAYOUT, 'is_nested',
            aliases=('nested',))),
    )


def _owner_candidates(declaration, targets, case_sensitive=False):
    candidates = []
    for target in targets:
        if not _same(target.name, declaration.table, case_sensitive):
            continue
        # An absent field on the metadata row is genuinely unknown.  A present
        # field that differs from the requested identity rules the target out.
        if (declaration.schema is not None and target.schema is not None and
                not _same(target.schema, declaration.schema, case_sensitive)):
            continue
        if (declaration.catalog is not None and target.catalog is not None and
                not _same(target.catalog, declaration.catalog,
                          case_sensitive)):
            continue
        candidates.append(target)
    return candidates


def _table_owner_candidates(row, targets, case_sensitive=False):
    candidates = []
    for target in targets:
        if not _same(target.name, row['name'], case_sensitive):
            continue
        if (row['schema'] is not None and target.schema is not None and
                not _same(target.schema, row['schema'], case_sensitive)):
            continue
        if (row['catalog'] is not None and target.catalog is not None and
                not _same(target.catalog, row['catalog'], case_sensitive)):
            continue
        candidates.append(target)
    return candidates


def _issue(kind, message, **details):
    entry = {'kind': kind, 'message': message}
    entry.update(details)
    return entry


def _manifest_item(item):
    return dict(item)


def _declaration_summary(declaration):
    return {
        'name': declaration.name,
        'type': declaration.type_name,
        'type_range': declaration.type_range,
        'ordinal': declaration.ordinal,
        'catalog': declaration.catalog,
        'schema': declaration.schema,
        'table': declaration.table,
    }


def _dedupe_declarations(declarations):
    unique = []
    seen = set()
    for declaration in declarations:
        key = declaration.key()
        if key in seen:
            continue
        seen.add(key)
        unique.append(declaration)
    return sorted(unique, key=_declaration_sort_key)


def _declaration_sort_key(declaration):
    return (
        declaration.explicit_path or '',
        _norm(declaration.name) or '',
        declaration.type_name or '',
        -1 if declaration.column_size is None else declaration.column_size,
        -1 if declaration.decimal_digits is None else
        declaration.decimal_digits,
        declaration.catalog or '',
        declaration.schema or '',
        declaration.table or '',
    )


def _group_columns(declarations):
    """Group top-level declarations by ordinal, preserving first appearance."""
    groups = OrderedDict()
    for declaration in declarations:
        key = declaration.ordinal
        if key is None:
            key = ('name', _norm(declaration.name))
        groups.setdefault(key, []).append(declaration)
    return groups


def _column_sort_key(column):
    return (
        1 if column.ordinal is None else 0,
        -1 if column.ordinal is None else column.ordinal,
        _norm(column.name) or '',
    )


def _build_column(declarations):
    declarations = _dedupe_declarations(declarations)
    first = declarations[0]
    names = sorted({_norm(d.name) for d in declarations if d.name is not None})
    declared_types = sorted(
        {(d.type_name, d.column_size, d.decimal_digits)
         for d in declarations if d.type_name is not None},
        key=lambda item: (item[0], -1 if item[1] is None else item[1],
                          -1 if item[2] is None else item[2]))
    reasons = []
    if len(names) > 1:
        status = COLUMN_CONFLICT
        type_name = None
        reasons.append(ISSUE_COLUMN_NAME_CONFLICT)
    elif len(declared_types) > 1:
        status = COLUMN_CONFLICT
        type_name = None
        reasons.append(ISSUE_TYPE_CONFLICT)
    elif not declared_types:
        status = COLUMN_PENDING
        type_name = None
        reasons.append('missing_type')
    else:
        status = COLUMN_KNOWN
        type_name = declared_types[0][0]
        if any(d.type_name is None for d in declarations):
            reasons.append('incomplete_declaration')
    declared = next((d for d in declarations if d.type_name is not None),
                    first)
    return ColumnDefinition(
        name=first.name,
        ordinal=first.ordinal,
        type_name=type_name,
        data_type=declared.data_type,
        column_size=declared.column_size,
        decimal_digits=declared.decimal_digits,
        nullable=declared.nullable,
        status=status,
        declarations=declarations,
        reasons=reasons,
    )


def element_type_hint(type_name):
    """Return the declared child type for a one-parameter complex type.

    Hive reports nested fields in separate ``GetColumns`` rows, not as a
    textual structure.  This helper is intentionally conservative: it only
    returns a child type when the parent declaration itself spells it out.
    Otherwise ``None`` means the caller must leave the nested type pending.
    """
    if type_name is None:
        return None
    text = str(type_name).strip()
    upper = text.upper()
    for name in ('ARRAY', 'MAP'):
        prefix = name + '<'
        if upper.startswith(prefix) and text.endswith('>'):
            inner = text[len(prefix):-1]
            if name == 'ARRAY':
                return inner.strip() or None
            parts = inner.split(',', 1)
            if len(parts) == 2:
                return parts[1].strip() or None
            return None
    return None


def _nested_path(declaration, known_top_level):
    """Return ``(parent, relative_path)`` for an explicit nested declaration.

    A nested row must carry an unambiguous path.  If only a leaf is supplied
    there is no safe parent to attach it to and the caller records it as
    unresolved instead of guessing.
    """
    name = _as_text(declaration.name)
    if name is None:
        return None, None

    if declaration.parent_path is not None:
        parent_text = _as_text(declaration.parent_path)
        for column_name in known_top_level:
            if _same(parent_text, column_name):
                return column_name, name
        # The declared parent may itself be a nested path.  Search for the
        # explicitly returned ancestor below.
        parts = [part for part in parent_text.split('.') if part != '']
        for cut in range(len(parts), 0, -1):
            candidate = '.'.join(parts[:cut])
            for column_name in known_top_level:
                if _same(candidate, column_name):
                    relative = '.'.join(parts[cut:] + [name])
                    return column_name, relative
        return None, None

    if '.' not in name:
        return None, None
    parts = [part for part in name.split('.') if part != '']
    if len(parts) < 2:
        return None, None

    # Prefer the longest explicitly returned ancestor.  The parent must itself
    # be a known top-level or nested path for the attachment to be certain.
    for cut in range(len(parts) - 1, 0, -1):
        candidate = '.'.join(parts[:cut])
        for column_name in known_top_level:
            if _same(candidate, column_name):
                return column_name, '.'.join(parts[cut:])
    return None, None


def _build_table(target, table_rows, column_rows, unassigned_for_target,
                 ambiguous_table_rows=None):
    table_rows = list(table_rows)
    column_rows = list(column_rows)
    ambiguous_table_rows = list(ambiguous_table_rows or [])

    table_type = None
    table_type_conflict = False
    if table_rows:
        types = {_norm(row['table_type']) for row in table_rows}
        if len(types) > 1:
            table_type_conflict = True
        else:
            table_type = table_rows[0]['table_type']
    elif ambiguous_table_rows:
        types = {_norm(row['table_type']) for row in ambiguous_table_rows}
        if len(types) == 1:
            table_type = ambiguous_table_rows[0]['table_type']

    structure = TableStructure(
        catalog=target.catalog,
        schema=target.schema,
        name=target.name,
        table_type=table_type,
        table_seen=bool(table_rows or ambiguous_table_rows),
    )

    if table_type_conflict:
        structure.issues.append(_issue(
            ISSUE_TABLE_TYPE_CONFLICT,
            'GetTables returned conflicting types for the same object',
            declarations=[row['table_type'] for row in table_rows],
        ))

    # Separate explicit nested paths from top-level declarations.  A row that
    # names a parent, is flagged nested, or uses a dotted path is treated as
    # nested.  It is only attached later when an explicit parent column is
    # present; otherwise it is kept as an unresolved nested declaration rather
    # than guessed into a parent.
    top_level = []
    nested = []
    for declaration in column_rows:
        name = _as_text(declaration.name)
        if (declaration.parent_path is not None or declaration.is_nested or
                (name is not None and '.' in name)):
            nested.append(declaration)
        else:
            top_level.append(declaration)

    grouped = _group_columns(top_level)
    columns = [_build_column(group) for group in grouped.values()]

    # Keep the server supplied ordinal order.  Missing ordinals are kept in
    # first seen order after the explicit ones.
    columns = sorted(columns, key=_column_sort_key)
    structure.columns = columns

    known_top_level = [c.name for c in columns]
    nested_by_path = OrderedDict()
    for declaration in sorted(nested, key=_declaration_sort_key):
        parent, relative = _nested_path(declaration, known_top_level)
        if parent is None:
            name_text = _as_text(declaration.name)
            path = name_text
            if declaration.parent_path is not None and name_text is not None:
                path = '%s.%s' % (_as_text(declaration.parent_path), name_text)
            elif declaration.parent_path is not None:
                path = _as_text(declaration.parent_path)
            leaf = name_text.split('.')[-1] if name_text else None
            existing = next(
                (field for field in structure.unresolved_nested
                 if _same(field.path, path)), None)
            if existing is not None:
                existing.declarations.append(declaration)
                continue
            structure.unresolved_nested.append(NestedField(
                path=path,
                name=leaf,
                ordinal=declaration.ordinal,
                type_name=declaration.type_name,
                data_type=declaration.data_type,
                column_size=declaration.column_size,
                decimal_digits=declaration.decimal_digits,
                nullable=declaration.nullable,
                status=COLUMN_UNRESOLVED,
                declarations=[declaration],
                reasons=[ISSUE_MISSING_NESTED_PARENT],
            ))
            structure.issues.append(_issue(
                ISSUE_MISSING_NESTED_PARENT,
                'nested column path has no explicitly returned parent',
                path=path,
            ))
            continue
        path = '.'.join((parent, relative))
        nested_by_path.setdefault(path, []).append((parent, relative,
                                                    declaration))

    for path, entries in nested_by_path.items():
        parent, relative, first = entries[0]
        declarations = _dedupe_declarations([entry[2] for entry in entries])
        declared_types = sorted(
            {(d.type_name, d.column_size, d.decimal_digits)
             for d in declarations if d.type_name is not None},
            key=lambda item: (item[0], -1 if item[1] is None else item[1],
                              -1 if item[2] is None else item[2]))
        reasons = []
        if len(declared_types) > 1:
            status = COLUMN_CONFLICT
            type_name = None
            reasons.append(ISSUE_TYPE_CONFLICT)
        elif not declared_types:
            status = COLUMN_PENDING
            type_name = None
            reasons.append(ISSUE_PENDING_NESTED_TYPE)
        else:
            status = COLUMN_KNOWN
            type_name = declared_types[0][0]
            first = next((d for d in declarations if d.type_name is not None),
                         first)
        nested_field = NestedField(
            path=path,
            name=relative,
            ordinal=first.ordinal,
            type_name=type_name,
            data_type=first.data_type,
            column_size=first.column_size,
            decimal_digits=first.decimal_digits,
            nullable=first.nullable,
            status=status,
            declarations=declarations,
            reasons=reasons,
        )
        parent_column = structure.column(parent)
        if parent_column is not None:
            parent_column.nested.append(nested_field)
        if status == COLUMN_PENDING:
            structure.issues.append(_issue(
                ISSUE_PENDING_NESTED_TYPE,
                'nested field has no type declaration',
                path=path,
            ))
        elif status == COLUMN_CONFLICT:
            structure.issues.append(_issue(
                ISSUE_TYPE_CONFLICT,
                'nested field has conflicting type declarations',
                path=path,
                declarations=[d.to_dict() for d in declarations],
            ))

    structure.columns_defined = bool(top_level)
    structure.columns_complete = (
        structure.columns_defined and
        not unassigned_for_target and
        not structure.unresolved_nested and
        not structure.issues and
        all(c.status == COLUMN_KNOWN for c in columns) and
        all(n.status == COLUMN_KNOWN for c in columns for n in c.nested)
    )

    if unassigned_for_target:
        structure.reasons.append(ISSUE_MISSING_CATALOG)

    if not table_rows and not column_rows and not unassigned_for_target and \
            not ambiguous_table_rows:
        structure.status = MISSING
        structure.reasons.append(ISSUE_MISSING_TABLE)
        structure.issues.append(_issue(
            ISSUE_MISSING_TABLE,
            'no GetTables or GetColumns record matched this object',
        ))
    elif ambiguous_table_rows and not table_rows:
        # A catalog-less table row identifies the object name and type but
        # cannot be assigned to one of several same-named requested objects.
        structure.status = AMBIGUOUS
        structure.reasons.append('ambiguous_catalog')
        for row in ambiguous_table_rows:
            structure.issues.append(_issue(
                ISSUE_MISSING_CATALOG,
                'GetTables row has no catalog and could belong to more than '
                'one requested object',
                object=row['name'],
                table_type=row['table_type'],
            ))
        if not column_rows:
            structure.columns_defined = False
    elif not table_rows:
        structure.status = PARTIAL
        structure.reasons.append('table identity not confirmed')
        structure.issues.append(_issue(
            ISSUE_MISSING_TABLE,
            'GetColumns matched but GetTables did not confirm the object type',
        ))
    elif table_type_conflict:
        structure.status = CONFLICT
        structure.reasons.append(ISSUE_TABLE_TYPE_CONFLICT)
    elif not column_rows:
        # A GetTables row is positive evidence that the object exists.  It is
        # not evidence that the object has no columns.
        structure.status = EXISTS_ONLY
        structure.reasons.append(ISSUE_EXISTS_ONLY)
        structure.issues.append(_issue(
            ISSUE_EXISTS_ONLY,
            'object is known from GetTables but no column definitions were '
            'returned',
        ))
    elif any(c.status == COLUMN_CONFLICT for c in columns) or any(
            issue['kind'] == ISSUE_TYPE_CONFLICT for issue in structure.issues):
        structure.status = CONFLICT
        structure.reasons.append(ISSUE_TYPE_CONFLICT)
    elif (unassigned_for_target or structure.unresolved_nested or
          any(c.status in (COLUMN_PENDING, COLUMN_UNRESOLVED)
              for c in columns)):
        structure.status = PARTIAL
    elif structure.columns_complete:
        structure.status = KNOWN
    else:
        structure.status = PARTIAL

    if structure.status == PARTIAL:
        for field in structure.unresolved_nested:
            structure.reasons.extend(field.reasons)
        for column in columns:
            if column.status in (COLUMN_PENDING, COLUMN_UNRESOLVED):
                structure.reasons.extend(column.reasons or [column.status])
            for nested in column.nested:
                if nested.status in (COLUMN_PENDING, COLUMN_UNRESOLVED):
                    structure.reasons.extend(
                        nested.reasons or [nested.status])
        if not structure.reasons:
            structure.reasons.append('incomplete_definition')

    if unassigned_for_target:
        for item in unassigned_for_target:
            structure.issues.append(_issue(
                ISSUE_MISSING_CATALOG,
                'GetColumns row has no catalog and could belong to more than '
                'one requested object',
                column=item['column'],
                candidates=item['candidates'],
            ))
    return structure


def exact_target_rows(target, table_rows, column_rows,
                      case_sensitive=False):
    """Return the raw rows that belong exactly to one requested target.

    This is used by the cursor to decide whether a literal-pattern query
    matched anything before falling back to a broader server search.  Rows
    that omit the catalog or schema are considered a match only when the
    remaining components agree; the manifest builder still records their
    ambiguity when several targets share the same name.
    """
    table_matches = [
        row for row in table_rows
        if _table_owner_candidates(
            _parse_table_row(row), [target],
            case_sensitive=case_sensitive)
    ]
    column_matches = [
        row for row in column_rows
        if _owner_candidates(
            _parse_column_row(row), [target],
            case_sensitive=case_sensitive)
    ]
    return table_matches, column_matches


def build_manifest(targets, table_rows=None, column_rows=None,
                   catalog_name=None, database_name=None,
                   schema_name=None, case_sensitive=False):
    """Build a manifest from flat ``GetTables`` and ``GetColumns`` rows.

    ``targets`` accepts the same forms as :func:`normalize_targets`.
    ``table_rows`` and ``column_rows`` may be any iterable of sequences or
    mappings using the HiveServer2/ODBC column layouts.  Their order is
    irrelevant; columns are regrouped by object identity and ordinal.
    """
    normalized = normalize_targets(
        targets, catalog_name=catalog_name, database_name=database_name,
        schema_name=schema_name, case_sensitive=case_sensitive)

    parsed_tables = [
        _parse_table_row(row, index)
        for index, row in enumerate(table_rows or [])
    ]
    parsed_columns = [
        _parse_column_row(row, index)
        for index, row in enumerate(column_rows or [])
    ]

    manifest = MetadataManifest(targets=normalized)

    table_matches = {target.identity: [] for target in normalized}
    table_ambiguous = {target.identity: [] for target in normalized}
    for row in parsed_tables:
        candidates = _table_owner_candidates(
            row, normalized, case_sensitive=case_sensitive)
        if row['catalog'] is None and len(candidates) > 1:
            manifest.unassigned.append({
                'kind': 'table',
                'reason': ISSUE_MISSING_CATALOG,
                'object': {
                    'catalog': row['catalog'],
                    'schema': row['schema'],
                    'name': row['name'],
                    'table_type': row['table_type'],
                },
                'candidates': [t.qualified_name for t in candidates],
            })
            manifest.issues.append(_issue(
                ISSUE_MISSING_CATALOG,
                'GetTables row has no catalog and could belong to more than '
                'one requested object',
                object=row['name'],
                candidates=[t.qualified_name for t in candidates],
            ))
            for target in candidates:
                table_ambiguous[target.identity].append(row)
            continue
        if len(candidates) == 1:
            table_matches[candidates[0].identity].append(row)
        # Otherwise the row belongs to an object that was not requested (for
        # example a look-alike selected by a literal wildcard character on the
        # server); it is deliberately not emitted.

    column_matches = {target.identity: [] for target in normalized}
    ambiguous_columns = {target.identity: [] for target in normalized}
    for declaration in parsed_columns:
        candidates = _owner_candidates(
            declaration, normalized, case_sensitive=case_sensitive)
        if declaration.catalog is None and len(candidates) > 1:
            item = {
                'column': _declaration_summary(declaration),
                'candidates': [t.qualified_name for t in candidates],
            }
            manifest.unassigned.append({
                'kind': 'column',
                'reason': ISSUE_MISSING_CATALOG,
                **item,
            })
            manifest.issues.append(_issue(
                ISSUE_MISSING_CATALOG,
                'GetColumns row has no catalog and could belong to more than '
                'one requested object',
                column=item['column'],
                candidates=item['candidates'],
            ))
            for target in candidates:
                ambiguous_columns[target.identity].append(item)
            continue
        if declaration.catalog is not None and len(candidates) > 1:
            candidates = [
                target for target in candidates
                if _same(target.catalog, declaration.catalog,
                         case_sensitive)
            ]
        if len(candidates) == 1:
            column_matches[candidates[0].identity].append(declaration)

    for target in normalized:
        structure = _build_table(
            target,
            table_matches[target.identity],
            column_matches[target.identity],
            ambiguous_columns[target.identity],
            ambiguous_table_rows=table_ambiguous[target.identity],
        )
        for row in table_ambiguous[target.identity]:
            if structure.status != AMBIGUOUS:
                structure.issues.append(_issue(
                    ISSUE_MISSING_CATALOG,
                    'GetTables row has no catalog and could belong to more '
                    'than one requested object',
                    object=row['name'],
                    table_type=row['table_type'],
                ))
        if table_ambiguous[target.identity] and structure.status == KNOWN:
            structure.status = AMBIGUOUS
            if 'ambiguous_catalog' not in structure.reasons:
                structure.reasons.append('ambiguous_catalog')
        manifest.tables.append(structure)
    return manifest


# Friendly aliases used by callers that describe the result as a manifest or
# as a collection of table structures.
build_table_manifest = build_manifest
table_manifest = build_manifest


__all__ = [
    'KNOWN', 'PARTIAL', 'CONFLICT', 'EXISTS_ONLY', 'AMBIGUOUS', 'MISSING',
    'COLUMN_KNOWN', 'COLUMN_CONFLICT', 'COLUMN_PENDING', 'COLUMN_UNRESOLVED',
    'ISSUE_MISSING_CATALOG', 'ISSUE_TYPE_CONFLICT',
    'ISSUE_TABLE_TYPE_CONFLICT', 'ISSUE_EXISTS_ONLY',
    'ISSUE_PENDING_NESTED_TYPE', 'ISSUE_MISSING_NESTED_PARENT',
    'ISSUE_MISSING_TABLE', 'ISSUE_UNMATCHED_ROW',
    'ISSUE_COLUMN_NAME_CONFLICT',
    'TableTarget', 'Declaration', 'NestedField', 'ColumnDefinition',
    'TableStructure', 'MetadataManifest',
    'literal_pattern', 'normalize_targets', 'build_manifest',
    'build_table_manifest', 'table_manifest', 'element_type_hint',
    'exact_target_rows',
]
