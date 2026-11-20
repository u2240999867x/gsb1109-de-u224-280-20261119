#!/usr/bin/env python

"""
Column declarations for :doc:`/scripts/csvjson`.

A declaration describes how the fields of a CSV input map to the fields of an
output document: the output name, the physical source column, and the type to
cast values to. Declarations can be versioned, so one declaration can describe
several layouts of the same data set.
"""

from __future__ import annotations

import datetime
import decimal
import json
import os
import re
from collections import OrderedDict
from dataclasses import dataclass, field

import agate
from agate.data_types.base import DEFAULT_NULL_VALUES
from agate.exceptions import CastError

from csvkit.exceptions import CustomException


class DeclarationError(CustomException):
    """
    Exception raised when a declaration can not be read or applied.
    """


NAME_KEYS = (
    'name', 'field', 'field_name', 'output', 'output_name', 'output_field', 'key', 'as', 'target', 'logical',
    'logical_name',
)
POSITION_KEYS = (
    'column', 'column_index', 'column_position', 'input_column', 'index', 'position', 'source', 'source_column',
    'source_index', 'source_position', 'original_column', 'original_index', 'original_position', 'physical_column',
    'ordinal', 'col',
)
HEADER_KEYS = ('header', 'source_name', 'input_name', 'from', 'source_header', 'original_name')
TYPE_KEYS = ('type', 'data_type', 'datatype', 'cast', 'convert', 'dtype')
LOCALE_KEYS = ('locale', 'number_locale')
GROUP_SYMBOL_KEYS = ('group_symbol', 'thousands_separator', 'thousand_separator')
DECIMAL_SYMBOL_KEYS = ('decimal_symbol', 'decimal_separator')
REQUIRED_KEYS = ('required', 'mandatory')
OPTIONAL_KEYS = ('optional', 'nullable')

VERSION_KEYS = ('version', 'schema_version', 'declaration_version')
INCLUDE_KEYS = ('include', 'select')
MATCH_KEYS = ('match', 'match_headers', 'required_headers', 'headers')

TYPE_ALIASES = {
    'text': 'text',
    'string': 'text',
    'str': 'text',
    'varchar': 'text',
    'char': 'text',
    'number': 'number',
    'numeric': 'number',
    'decimal': 'number',
    'float': 'number',
    'double': 'number',
    'amount': 'number',
    'money': 'number',
    'integer': 'integer',
    'int': 'integer',
    'long': 'integer',
    'bigint': 'integer',
    'boolean': 'boolean',
    'bool': 'boolean',
    'date': 'date',
    'datetime': 'datetime',
    'timestamp': 'datetime',
    'time': 'text',
    'timedelta': 'timedelta',
    'duration': 'timedelta',
    'json': 'json',
    'object': 'json',
}


def _first_key(mapping, keys, default=None):
    for key in keys:
        if key in mapping and mapping[key] not in (None, ''):
            return mapping[key]
    return default


def _as_bool(value, default=False):
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in ('', '0', 'false', 'no', 'n', 'off')
    return bool(value)


def _normalized_keys(mapping):
    return {str(key).strip().lower().replace('-', '_').replace(' ', '_'): value for key, value in mapping.items()}


@dataclass
class ColumnDeclaration:
    """
    A single logical output field.
    """

    name: str | None = None
    column: int | None = None
    header: str | None = None
    type: str | None = None
    locale: str | None = None
    group_symbol: str | None = None
    decimal_symbol: str | None = None
    date_format: str | None = None
    datetime_format: str | None = None
    required: bool = True
    options: dict = field(default_factory=dict)


@dataclass
class ResolvedField:
    """
    A declaration field resolved against the input's header row.
    """

    declaration: ColumnDeclaration
    index: int
    source_name: str | None
    data_type: object | None
    declared: bool = True
    output_name: str | None = None

    @property
    def name(self):
        if self.output_name is not None:
            return self.output_name
        return self.declaration.name or self.source_name


@dataclass
class VersionDeclaration:
    """
    A named (or unnamed) layout in a declaration.
    """

    name: str | None
    columns: list[ColumnDeclaration]
    include_undeclared: bool | None = None
    match_headers: list[str] = field(default_factory=list)


@dataclass
class ResolvedDeclaration:
    """
    A version declaration resolved against the input's header row.
    """

    version: VersionDeclaration
    fields: list[ResolvedField]
    column_names: list[str]


class DeclarationSet:
    """
    One or more version declarations, loaded from a JSON or CSV file.
    """

    def __init__(self, versions, default_version=None, select_undeclared=True, requested_version=None):
        self.versions = versions
        self.default_version = default_version
        self.select_undeclared = select_undeclared
        self.requested_version = requested_version

    def select_version(self, requested, column_names, column_offset=1, args=None):
        if not self.versions:
            raise DeclarationError('The column declaration does not declare any fields.')

        if requested is not None:
            if requested not in self.versions:
                available = ', '.join(repr(name) for name in self.versions if name is not None)
                raise DeclarationError(
                    f'Declaration version "{requested}" was not found. Available versions: {available}.'
                )
            return self.resolve(self.versions[requested], column_names, column_offset, args)

        if self.default_version is not None:
            if self.default_version not in self.versions:
                raise DeclarationError(f'Default declaration version "{self.default_version}" was not found.')
            return self.resolve(self.versions[self.default_version], column_names, column_offset, args)

        if len(self.versions) == 1:
            return self.resolve(next(iter(self.versions.values())), column_names, column_offset, args)

        # If the declaration provides header hints, use them to select a version.
        hinted = [
            version for version in self.versions.values()
            if version.match_headers and all(header in column_names for header in version.match_headers)
        ]
        if len(hinted) == 1:
            return self.resolve(hinted[0], column_names, column_offset, args)

        # Otherwise, select the most specific version that can be applied to the input.
        applicable = []
        for version in self.versions.values():
            try:
                resolved = self.resolve(version, column_names, column_offset, args)
            except DeclarationError:
                continue
            applicable.append((len(version.columns), resolved))

        if applicable:
            applicable.sort(key=lambda item: item[0], reverse=True)
            if len(applicable) == 1 or applicable[0][0] > applicable[1][0]:
                return applicable[0][1]

        available = ', '.join(repr(name) for name in self.versions if name is not None)
        raise DeclarationError(
            'The column declaration has multiple versions and the input matches more than one. '
            f'Specify a version with --declaration-version. Available versions: {available}.'
        )

    def resolve(self, version, column_names, column_offset=1, args=None):
        fields = []
        used_indices = set()
        output_names = set()

        for declaration in version.columns:
            try:
                index = self.resolve_index(declaration, column_names, column_offset)
            except DeclarationError:
                if not declaration.required:
                    continue
                raise

            source_name = column_names[index] if index < len(column_names) else None
            name = declaration.name or source_name

            if name is None:
                name = f'column_{index + column_offset}'

            # Two source columns can have the same name. Keep both, rather than
            # letting one overwrite the other.
            unique_name = name
            suffix = 2
            while unique_name in output_names:
                unique_name = f'{name}_{suffix}'
                suffix += 1
            name = unique_name
            output_names.add(name)

            fields.append(ResolvedField(
                declaration, index, source_name, build_data_type(declaration, args), output_name=name
            ))
            used_indices.add(index)

        if version.include_undeclared is not None:
            include_undeclared = version.include_undeclared
        else:
            include_undeclared = self.select_undeclared

        if include_undeclared:
            for index, source_name in enumerate(column_names):
                if index in used_indices:
                    continue

                name = source_name
                suffix = 2
                while name in output_names:
                    name = f'{source_name}_{suffix}'
                    suffix += 1
                output_names.add(name)

                declaration = ColumnDeclaration(name=name, column=index + column_offset)
                fields.append(ResolvedField(
                    declaration, index, source_name, None, declared=False, output_name=name
                ))

        return ResolvedDeclaration(version, fields, list(column_names))

    @staticmethod
    def resolve_index(declaration, column_names, column_offset):
        if declaration.column is not None:
            index = declaration.column - column_offset
            if index < 0 or index >= len(column_names):
                raise DeclarationError(
                    DeclarationSet._missing_message(declaration, column_names)
                )
            return index

        header = declaration.header if declaration.header is not None else declaration.name
        if header is None:
            raise DeclarationError('A declaration field must specify a name, header or column.')

        matches = [i for i, name in enumerate(column_names) if name == header]
        if not matches:
            raise DeclarationError(DeclarationSet._missing_message(declaration, column_names))
        if len(matches) > 1:
            raise DeclarationError(
                f'Declaration field "{declaration.name or header}" refers to header "{header}", which occurs '
                'more than once in the input. Specify its physical column in the declaration.'
            )
        return matches[0]

    @staticmethod
    def _missing_message(declaration, column_names):
        target = []
        if declaration.column is not None:
            target.append(f'physical column {declaration.column}')
        if declaration.header is not None:
            target.append(f'header "{declaration.header}"')
        if not target and declaration.name is not None:
            target.append(f'field "{declaration.name}"')

        message = (
            f'Declaration cannot be applied: the declared {" / ".join(target)} is missing from the input '
            f'({len(column_names)} columns found).'
        )
        return message


class IntegerType:
    """
    An integral number, backed by :class:`agate.Number`.
    """

    def __init__(self, number):
        self.number = number

    def cast(self, value):
        value = self.number.cast(value)
        if value is None:
            return None
        if value != value.to_integral_value():
            raise CastError(f'Can not parse value "{value}" as an integer.')
        return value

    def jsonify(self, value):
        if value is None:
            return None
        return int(value)


class JsonType:
    """
    A JSON value, useful for object- or array-valued fields.
    """

    def cast(self, value):
        if value is None:
            return None
        if not isinstance(value, str):
            return value
        value = value.strip()
        if value == '':
            return None
        try:
            return json.loads(value)
        except ValueError as e:
            raise CastError(f'Can not parse value "{value}" as JSON: {e}')

    def jsonify(self, value):
        return value


def _type_kwargs(args):
    if getattr(args, 'blanks', False):
        null_values = []
    else:
        null_values = list(DEFAULT_NULL_VALUES)

    for null_value in getattr(args, 'null_values', []) or []:
        null_values.append(null_value)

    return {'null_values': null_values}


def build_data_type(declaration, args=None):
    """
    Build the agate (or compatible) data type for a declaration field.

    A declared field defaults to text, so that, for example, an identifier with
    leading zeroes is not converted to a number. Returns :code:`None` only for
    undeclared fields, which retain csvjson's existing behavior.
    """
    if not declaration.type:
        kwargs = _type_kwargs(args) if args is not None else {}
        return agate.Text(**kwargs)

    name = str(declaration.type).strip().lower()
    # Accept e.g. "decimal(12,2)".
    name = re.sub(r'\(.*\)$', '', name).strip()
    name = TYPE_ALIASES.get(name, name)
    if declaration.type.strip().lower().startswith('json'):
        name = 'json'

    kwargs = _type_kwargs(args) if args is not None else {}

    if name == 'text':
        return agate.Text(**kwargs)
    if name == 'number':
        return agate.Number(
            locale=declaration.locale or getattr(args, 'locale', None) or 'en_US',
            group_symbol=declaration.group_symbol,
            decimal_symbol=declaration.decimal_symbol,
            no_leading_zeroes=getattr(args, 'no_leading_zeroes', None),
            **kwargs,
        )
    if name == 'integer':
        return IntegerType(agate.Number(
            locale=declaration.locale or getattr(args, 'locale', None) or 'en_US',
            group_symbol=declaration.group_symbol,
            decimal_symbol=declaration.decimal_symbol,
            no_leading_zeroes=getattr(args, 'no_leading_zeroes', None),
            **kwargs,
        ))
    if name == 'boolean':
        return agate.Boolean(**kwargs)
    if name == 'date':
        return agate.Date(date_format=declaration.date_format or getattr(args, 'date_format', None), **kwargs)
    if name == 'datetime':
        return agate.DateTime(
            datetime_format=declaration.datetime_format or getattr(args, 'datetime_format', None),
            **kwargs,
        )
    if name == 'timedelta':
        return agate.TimeDelta(**kwargs)
    if name == 'json':
        return JsonType()

    # A declared but unrecognized type is a user error, not a reason to silently infer a type.
    raise DeclarationError(f'Unsupported column type "{declaration.type}".')


def jsonify_value(data_type, value):
    """
    Convert a cast value to something :func:`json.dump` can serialize.
    """
    if value is None:
        return None
    if data_type is not None and hasattr(data_type, 'jsonify'):
        value = data_type.jsonify(value)
    if isinstance(value, decimal.Decimal):
        return float(value)
    if isinstance(value, (datetime.date, datetime.datetime)):
        return value.isoformat()
    if isinstance(value, datetime.timedelta):
        return str(value)
    return value


def _parse_position(value):
    """
    A position is an integer, or the header of the source column.
    """
    if isinstance(value, int):
        return value, None
    text = str(value).strip()
    try:
        return int(text), None
    except ValueError:
        return None, text


def _parse_field(name_hint, spec):
    if isinstance(spec, str):
        lowered = re.sub(r'\(.*\)$', '', spec.strip().lower())
        if lowered in TYPE_ALIASES:
            return ColumnDeclaration(name=name_hint, type=spec)
        return ColumnDeclaration(name=name_hint, header=spec)

    if isinstance(spec, int):
        return ColumnDeclaration(name=name_hint, column=spec)

    if isinstance(spec, (list, tuple)):
        # e.g. {"amount": [3, "number"]}
        if not spec:
            raise DeclarationError(f'Invalid declaration for field "{name_hint}".')
        declaration = _parse_field(name_hint, spec[0] if isinstance(spec[0], (int, str)) else spec[0])
        if len(spec) > 1 and not declaration.type:
            declaration.type = str(spec[1])
        if len(spec) > 2 and not declaration.locale:
            declaration.locale = str(spec[2])
        return declaration

    if not isinstance(spec, dict):
        raise DeclarationError(f'Invalid declaration for field "{name_hint}". Expected an object.')

    mapping = _normalized_keys(spec)

    name = _first_key(mapping, NAME_KEYS, name_hint)
    column = _first_key(mapping, POSITION_KEYS)
    header = _first_key(mapping, HEADER_KEYS)
    type_name = _first_key(mapping, TYPE_KEYS)
    locale = _first_key(mapping, LOCALE_KEYS)
    group_symbol = _first_key(mapping, GROUP_SYMBOL_KEYS)
    decimal_symbol = _first_key(mapping, DECIMAL_SYMBOL_KEYS)
    date_format = _first_key(mapping, ('date_format', 'dateformat'))
    datetime_format = _first_key(mapping, ('datetime_format', 'datetimeformat'))

    if column is not None:
        column, column_header = _parse_position(column)
        if column_header is not None and header is None:
            header = column_header
    if name is not None:
        name = str(name)

    required = True
    if _first_key(mapping, REQUIRED_KEYS) is not None:
        required = _as_bool(_first_key(mapping, REQUIRED_KEYS), True)
    if _first_key(mapping, OPTIONAL_KEYS) is not None:
        required = not _as_bool(_first_key(mapping, OPTIONAL_KEYS), False)

    return ColumnDeclaration(
        name=name,
        column=column,
        header=header,
        type=str(type_name) if type_name is not None else None,
        locale=str(locale) if locale is not None else None,
        group_symbol=str(group_symbol) if group_symbol is not None else None,
        decimal_symbol=str(decimal_symbol) if decimal_symbol is not None else None,
        date_format=str(date_format) if date_format is not None else None,
        datetime_format=str(datetime_format) if datetime_format is not None else None,
        required=required,
        options={key: value for key, value in mapping.items() if key not in (
            NAME_KEYS + POSITION_KEYS + HEADER_KEYS + TYPE_KEYS + LOCALE_KEYS + GROUP_SYMBOL_KEYS
            + DECIMAL_SYMBOL_KEYS + REQUIRED_KEYS + OPTIONAL_KEYS
        )},
    )


def _parse_columns(value):
    if value is None:
        return []

    if isinstance(value, str):
        return [_parse_field(None, value)]

    if isinstance(value, list):
        columns = []
        for item in value:
            if isinstance(item, str):
                columns.append(ColumnDeclaration(name=item))
            elif isinstance(item, dict):
                # A list entry may be {"name": "x", ...} or a one-entry mapping.
                mapping = _normalized_keys(item)
                if _first_key(mapping, NAME_KEYS) is not None or any(
                    key in mapping for key in POSITION_KEYS + HEADER_KEYS
                ):
                    columns.append(_parse_field(None, item))
                elif len(item) == 1:
                    key, spec = next(iter(item.items()))
                    columns.append(_parse_field(str(key), spec))
                else:
                    raise DeclarationError('Invalid field in the column declaration.')
            else:
                raise DeclarationError('Invalid field in the column declaration.')
        return columns

    if isinstance(value, dict):
        # {"fields": [...]} / {"columns": [...]}
        nested = _first_key(value, ('fields', 'columns', 'declarations'))
        if nested is not None and len(value) <= 3:
            return _parse_columns(nested)

        # {"name": {"column": 2, "type": "text"}, ...}
        columns = []
        for key, spec in value.items():
            if str(key).strip().lower() in (
                'version', 'active_version', 'default_version', 'source_version',
                'include_undeclared', 'keep_undeclared', 'select_undeclared',
            ):
                continue
            columns.append(_parse_field(str(key), spec))
        return columns

    raise DeclarationError('Invalid column declaration.')


def _parse_version_object(name, value):
    match_headers = []

    if isinstance(value, dict):
        mapping = _normalized_keys(value)
        columns_value = _first_key(mapping, ('fields', 'columns', 'declarations'))
        if columns_value is not None:
            include_value = _first_key(mapping, ('include_undeclared', 'keep_undeclared', 'select_undeclared'))
            include_undeclared = _as_bool(include_value) if include_value is not None else None
            match_value = _first_key(mapping, MATCH_KEYS)
            if isinstance(match_value, str):
                match_headers = [match_value]
            elif isinstance(match_value, list):
                match_headers = [str(header) for header in match_value]
            return VersionDeclaration(
                name,
                _parse_columns(columns_value),
                include_undeclared=include_undeclared,
                match_headers=match_headers,
            )

        # A version object may describe its fields directly, along with version metadata.
        fields = {
            key: item for key, item in value.items()
            if key not in VERSION_KEYS + MATCH_KEYS + ('include_undeclared', 'keep_undeclared', 'select_undeclared')
        }
        if fields and all(not isinstance(item, list) for item in fields.values()):
            return VersionDeclaration(name, _parse_columns(fields))

    return VersionDeclaration(name, _parse_columns(value))


def _looks_like_field_mapping(value):
    for spec in value.values():
        if isinstance(spec, (int, str, list)):
            continue
        if isinstance(spec, dict):
            mapping = {str(key).strip().lower() for key in spec}
            if mapping & set(POSITION_KEYS + TYPE_KEYS + HEADER_KEYS + NAME_KEYS):
                continue
        return False
    return True


def parse_json_declaration(data):
    if isinstance(data, list):
        # A list of versions ({"version": "v2", "columns": [...]}) or a list of fields.
        if data and all(isinstance(item, dict) and _first_key(_normalized_keys(item), VERSION_KEYS) is not None
                        for item in data):
            versions = OrderedDict()
            for item in data:
                mapping = _normalized_keys(item)
                name = str(_first_key(mapping, VERSION_KEYS))
                versions[name] = _parse_version_object(name, item)
            return DeclarationSet(versions)
        return DeclarationSet({None: VersionDeclaration(None, _parse_columns(data))})

    if not isinstance(data, dict):
        raise DeclarationError('The column declaration must be a JSON object or array.')

    mapping = _normalized_keys(data)

    versions_value = _first_key(mapping, ('versions', 'schemas'))
    default_version = _first_key(mapping, ('version', 'active_version', 'default_version', 'source_version'))
    select_value = _first_key(mapping, ('include_undeclared', 'keep_undeclared', 'select_undeclared'))
    select_undeclared = _as_bool(select_value) if select_value is not None else True

    if versions_value is not None:
        if isinstance(versions_value, list):
            versions = OrderedDict()
            for item in versions_value:
                item_mapping = _normalized_keys(item)
                name = str(_first_key(item_mapping, VERSION_KEYS, len(versions)))
                versions[name] = _parse_version_object(name, item)
            return DeclarationSet(
                versions,
                default_version=str(default_version) if default_version is not None else None,
                select_undeclared=select_undeclared,
            )

        if not isinstance(versions_value, dict):
            raise DeclarationError('The "versions" entry in the column declaration must be an object.')
        versions = OrderedDict()
        for name, value in versions_value.items():
            versions[str(name)] = _parse_version_object(str(name), value)
        return DeclarationSet(
            versions,
            default_version=str(default_version) if default_version is not None else None,
            select_undeclared=select_undeclared,
        )

    columns_value = _first_key(mapping, ('fields', 'columns'))
    if columns_value is not None:
        version = _parse_version_object(default_version, data)
        version_name = str(default_version) if default_version is not None else None
        return DeclarationSet(
            {version_name: version},
            default_version=version_name,
            select_undeclared=select_undeclared,
        )

    # Treat a top-level object as either a name -> field mapping or a
    # version name -> declaration mapping.
    if _looks_like_field_mapping(data):
        return DeclarationSet(
            {None: VersionDeclaration(None, _parse_columns(data), include_undeclared=select_undeclared)},
            default_version=str(default_version) if default_version is not None else None,
            select_undeclared=select_undeclared,
        )

    versions = OrderedDict()
    for name, value in data.items():
        if str(name).strip().lower() in ('version', 'active_version', 'default_version', 'source_version',
                                         'include_undeclared', 'keep_undeclared', 'select_undeclared'):
            continue
        versions[str(name)] = _parse_version_object(str(name), value)
    if not versions:
        raise DeclarationError('The column declaration does not declare any fields.')
    return DeclarationSet(
        versions,
        default_version=str(default_version) if default_version is not None else None,
        select_undeclared=select_undeclared,
    )


def _find_csv_header(rows):
    try:
        header = next(rows)
    except StopIteration:
        raise DeclarationError('The CSV column declaration is empty.')

    keys = [str(key).strip().lower().replace('-', '_').replace(' ', '_') for key in header]
    recognized = set(NAME_KEYS + POSITION_KEYS + HEADER_KEYS + TYPE_KEYS + LOCALE_KEYS + VERSION_KEYS)
    if not any(key in recognized for key in keys):
        raise DeclarationError(
            'The CSV column declaration must have a header row with at least a name and a column/header. '
            f'Found: {", ".join(header)}.'
        )
    return keys, rows


def parse_csv_declaration(text):
    import csv
    import io

    rows = csv.reader(io.StringIO(text))
    keys, rows = _find_csv_header(rows)

    versions = OrderedDict()
    unnamed = []
    includes = {}
    matches = {}

    for row_number, row in enumerate(rows, 2):
        if not row or all(cell.strip() == '' for cell in row):
            continue
        if row[0].lstrip().startswith('#'):
            continue

        values = {}
        for index, key in enumerate(keys):
            values[key] = row[index] if index < len(row) else ''

        name = _first_key(values, NAME_KEYS)
        column = _first_key(values, POSITION_KEYS)
        header = _first_key(values, HEADER_KEYS)
        type_name = _first_key(values, TYPE_KEYS)
        locale = _first_key(values, LOCALE_KEYS)
        version = _first_key(values, VERSION_KEYS)

        if name is None and header is None and column is None:
            raise DeclarationError(f'No field name, header or column at line {row_number} of the declaration.')

        column_value, column_header = _parse_position(column) if column not in (None, '') else (None, None)

        declaration = ColumnDeclaration(
            name=str(name) if name not in (None, '') else None,
            column=column_value,
            header=str(header) if header not in (None, '') else column_header,
            type=str(type_name) if type_name not in (None, '') else None,
            locale=str(locale) if locale not in (None, '') else None,
            date_format=str(_first_key(values, ('date_format', 'dateformat'))) or None,
            datetime_format=str(_first_key(values, ('datetime_format', 'datetimeformat'))) or None,
            required=_as_bool(_first_key(values, OPTIONAL_KEYS), False) is False,
        )
        if _first_key(values, REQUIRED_KEYS) not in (None, ''):
            declaration.required = _as_bool(_first_key(values, REQUIRED_KEYS), True)

        include = _first_key(values, INCLUDE_KEYS)
        if include not in (None, '') and not _as_bool(include, True):
            continue

        if version not in (None, ''):
            version = str(version)
            versions.setdefault(version, []).append(declaration)

            include_value = _first_key(values, ('include_undeclared', 'keep_undeclared', 'select_undeclared'))
            if include_value not in (None, ''):
                includes[version] = _as_bool(include_value)
            match_value = _first_key(values, MATCH_KEYS)
            if match_value not in (None, ''):
                matches.setdefault(version, []).append(str(match_value))
        else:
            unnamed.append(declaration)

    if unnamed:
        if versions:
            # Unversioned rows apply to every version that doesn't declare them.
            for version, columns in versions.items():
                names = {column.name for column in columns}
                columns.extend(column for column in unnamed if column.name not in names)
        else:
            versions[None] = unnamed

    if not versions:
        raise DeclarationError('The CSV column declaration does not declare any fields.')

    return DeclarationSet(OrderedDict(
        (
            name,
            VersionDeclaration(
                name,
                columns,
                include_undeclared=includes.get(name),
                match_headers=matches.get(name, []),
            ),
        )
        for name, columns in versions.items()
    ))


def parse_declaration(text, version=None):
    """
    Parse a JSON or CSV column declaration.

    :param version:
        An optional declaration version to select.
    """
    text = text.lstrip('﻿')
    stripped = text.lstrip()

    if stripped.startswith('{') or stripped.startswith('['):
        try:
            data = json.loads(stripped)
        except ValueError as e:
            raise DeclarationError(f'Invalid JSON in the column declaration: {e}')
        declarations = parse_json_declaration(data)
    else:
        declarations = parse_csv_declaration(text)

    declarations.requested_version = version
    return declarations


def load_declaration(path, encoding='utf-8-sig', version=None):
    """
    Load a column declaration from a file path or file-like object.
    """
    close = False

    if hasattr(path, 'read'):
        f = path
    else:
        if path == '-':
            raise DeclarationError('The column declaration can not be read from standard input.')
        try:
            f = open(path, encoding=encoding)
        except OSError as e:
            raise DeclarationError(f'Unable to read the column declaration "{path}": {e}')
        close = True

    try:
        text = f.read()
    finally:
        if close:
            f.close()

    return parse_declaration(text, version=version)


def split_declaration_path(value):
    """
    Support ``path#version`` as a shorthand for ``--declaration-version``.
    """
    if not value or os.path.exists(value):
        return value, None

    for separator in ('#', '@'):
        if separator in value:
            path, version = value.rsplit(separator, 1)
            if path and version:
                return path, version

    return value, None
