'''Versioned table definitions for the CSV handoff format.

A :class:`SchemaRegistry` keeps every table definition a sender has ever used.
Each definition is a full Arrow schema, so batches exported under an old
version can always be interpreted again, even after newer versions add
nullable struct members or widen a decimal scale.

Schemata travel alongside the CSV files as Arrow IPC streams encoded with
base64, which keeps the whole handoff payload plain text.
'''

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Dict, List, Optional

import pyarrow as pa


class HandoffError(Exception):
    '''Base error for the CSV handoff format.'''


class IncompatibleSchemaError(HandoffError):
    '''Two schema versions cannot be reconciled.'''


class UnregisteredVersionError(HandoffError):
    '''A batch refers to a schema version that the registry does not know.'''


class SchemaMismatchError(HandoffError):
    '''The schema embedded in a batch differs from the registry copy.'''


def encode_schema(schema: pa.Schema) -> str:
    '''Serialize a schema to a base64 Arrow IPC stream.'''
    sink = pa.BufferOutputStream()
    with pa.ipc.new_stream(sink, schema):
        pass
    return base64.b64encode(sink.getvalue().to_pybytes()).decode('ascii')


def decode_schema(payload: str) -> pa.Schema:
    '''Rebuild a schema from :func:`encode_schema` output.'''
    raw = base64.b64decode(payload.encode('ascii'))
    return pa.ipc.open_stream(pa.BufferReader(raw)).schema


def _decimal_width(decimal_type: pa.DataType) -> int:
    return 256 if pa.types.is_decimal256(decimal_type) else 128


def _merge_types(old: pa.DataType, new: pa.DataType, path: str) -> pa.DataType:
    if old.equals(new):
        return new

    if pa.types.is_struct(old) and pa.types.is_struct(new):
        merged: List[pa.Field] = [
            pa.field(field.name, field.type, nullable=field.nullable, metadata=field.metadata)
            for field in old
        ]
        old_fields = {field.name: field for field in old}
        for new_field in new:
            field_path = f'{path}.{new_field.name}' if path else new_field.name
            if new_field.name in old_fields:
                merged_type = _merge_types(old_fields[new_field.name].type, new_field.type, field_path)
                index = next(i for i, field in enumerate(merged) if field.name == new_field.name)
                merged[index] = pa.field(new_field.name, merged_type, nullable=new_field.nullable,
                                         metadata=new_field.metadata)
            else:
                if not new_field.nullable:
                    raise IncompatibleSchemaError(
                        f'New struct member {field_path} must be nullable to extend older batches')
                merged.append(pa.field(new_field.name, new_field.type, nullable=True,
                                       metadata=new_field.metadata))
        return pa.struct(merged)

    if pa.types.is_list(old) and pa.types.is_list(new):
        return pa.list_(_merge_types(old.value_type, new.value_type, f'{path}[]'))
    if pa.types.is_large_list(old) and pa.types.is_large_list(new):
        return pa.large_list(_merge_types(old.value_type, new.value_type, f'{path}[]'))

    if pa.types.is_decimal(old) and pa.types.is_decimal(new):
        width = max(_decimal_width(old), _decimal_width(new))
        precision = max(old.precision, new.precision)
        scale = max(old.scale, new.scale)
        if width == 256:
            return pa.decimal256(precision, scale)
        return pa.decimal128(precision, scale)

    raise IncompatibleSchemaError(
        f'Field {path or "<root>"} changed in an incompatible way: {old} -> {new}')


def merge_schemas(old: pa.Schema, new: pa.Schema) -> pa.Schema:
    '''Build the union shape of two successive versions.

    Only additive, backward-compatible evolution is accepted: new struct
    members must be nullable and decimal types may only grow in precision or
    scale. Everything else requires an identical type.
    '''
    merged_struct = _merge_types(pa.struct(list(old)), pa.struct(list(new)), '')
    return pa.schema([
        pa.field(field.name, field.type, nullable=field.nullable, metadata=field.metadata)
        for field in merged_struct
    ])


def _ensure_type_fits(physical: pa.DataType, declared: pa.DataType, path: str) -> None:
    if physical.equals(declared):
        return
    if pa.types.is_struct(physical) and pa.types.is_struct(declared):
        declared_fields = {field.name: field for field in declared}
        for field in physical:
            if field.name not in declared_fields:
                raise IncompatibleSchemaError(f'Physical field {path}.{field.name} is not declared')
            _ensure_type_fits(field.type, declared_fields[field.name].type, f'{path}.{field.name}')
        return
    if pa.types.is_list(physical) and pa.types.is_list(declared):
        _ensure_type_fits(physical.value_type, declared.value_type, f'{path}[]')
        return
    if pa.types.is_large_list(physical) and pa.types.is_large_list(declared):
        _ensure_type_fits(physical.value_type, declared.value_type, f'{path}[]')
        return
    if pa.types.is_decimal(physical) and pa.types.is_decimal(declared):
        if (_decimal_width(physical) <= _decimal_width(declared)
                and physical.precision <= declared.precision
                and physical.scale <= declared.scale):
            return
    raise IncompatibleSchemaError(
        f'Physical type {physical} of {path or "<root>"} does not fit declared {declared}')


def ensure_subset_compatible(physical: pa.Schema, declared: pa.Schema) -> None:
    '''Validate that a parquet file can be read under a declared version.'''
    declared_fields = {field.name: field for field in declared}
    for field in physical:
        if field.name not in declared_fields:
            raise IncompatibleSchemaError(f'Physical column {field.name} is not part of the declared schema')
        _ensure_type_fits(field.type, declared_fields[field.name].type, field.name)


def widen_table(table: pa.Table, target: pa.Schema) -> pa.Table:
    '''Project a table written under an older version onto a newer shape.

    Missing members are filled with nulls following the explicit
    compatibility rules; no values are ever guessed.
    '''
    arrays = []
    for field in target:
        if field.name in table.schema.names:
            arrays.append(table.column(field.name))
        else:
            arrays.append(pa.nulls(len(table), field.type))
    try:
        return pa.Table.from_arrays(arrays, schema=target)
    except (pa.ArrowTypeError, pa.ArrowInvalid) as exc:
        raise IncompatibleSchemaError(f'Cannot project table onto target schema: {exc}') from exc


def all_field_paths(schema: pa.Schema) -> List[str]:
    '''Return every field path, including members nested in structs and lists.'''
    paths: List[str] = []

    def walk(data_type: pa.DataType, prefix: str) -> None:
        if pa.types.is_struct(data_type):
            for field in data_type:
                path = f'{prefix}.{field.name}' if prefix else field.name
                paths.append(path)
                walk(field.type, path)
        elif pa.types.is_list(data_type) or pa.types.is_large_list(data_type):
            walk(data_type.value_type, f'{prefix}[]')

    for field in schema:
        paths.append(field.name)
        walk(field.type, field.name)
    return paths


def path_is_declared(schema: pa.Schema, path: List[str]) -> bool:
    data_type: pa.DataType = schema
    for part in path:
        if isinstance(data_type, pa.Schema):
            fields = {field.name: field for field in data_type}
            if part not in fields:
                return False
            data_type = fields[part].type
        elif pa.types.is_struct(data_type):
            if part not in data_type.names:
                return False
            data_type = data_type.field(part).type
        elif pa.types.is_list(data_type) or pa.types.is_large_list(data_type):
            data_type = data_type.value_type
            if not pa.types.is_struct(data_type) or part not in data_type.names:
                return False
            data_type = data_type.field(part).type
        else:
            return False
    return True


@dataclass(frozen=True)
class SchemaVersion:
    '''One table definition that was actually used to export a batch.'''
    version: str
    schema: pa.Schema
    created_at: str


class SchemaRegistry:
    '''Ordered, versioned collection of table definitions.'''

    def __init__(self, versions: Optional[List[SchemaVersion]] = None):
        self._versions: Dict[str, SchemaVersion] = {}
        for version in versions or []:
            self.add(version.version, version.schema, created_at=version.created_at)

    def add(self, version: str, schema: pa.Schema,
            created_at: Optional[str] = None) -> SchemaVersion:
        if version in self._versions:
            raise ValueError(f'Schema version {version} is already registered')
        if self._versions:
            merge_schemas(self.newest.schema, schema)
        entry = SchemaVersion(
            version=version,
            schema=schema,
            created_at=created_at or datetime.now(timezone.utc).isoformat())
        self._versions[version] = entry
        return entry

    def get(self, version: str) -> SchemaVersion:
        try:
            return self._versions[version]
        except KeyError:
            raise UnregisteredVersionError(f'Schema version {version} is not in the registry') from None

    def __contains__(self, version: object) -> bool:
        return version in self._versions

    @property
    def versions(self) -> List[SchemaVersion]:
        return list(self._versions.values())

    @property
    def newest(self) -> SchemaVersion:
        if not self._versions:
            raise HandoffError('The schema registry is empty')
        return next(reversed(self._versions.values()))

    def merged_schema(self) -> pa.Schema:
        '''Union shape across every registered version.'''
        schema: Optional[pa.Schema] = None
        for version in self._versions.values():
            schema = version.schema if schema is None else merge_schemas(schema, version.schema)
        assert schema is not None
        return schema

    def to_dict(self) -> dict:
        return {
            'format': 'parquet-tools.csv-handoff-registry/v1',
            'schema_format': 'arrow-ipc-stream/base64',
            'versions': [
                {
                    'version': entry.version,
                    'created_at': entry.created_at,
                    'schema': encode_schema(entry.schema),
                }
                for entry in self._versions.values()
            ],
        }

    @classmethod
    def from_dict(cls, payload: dict) -> 'SchemaRegistry':
        registry = cls()
        for raw in payload.get('versions', []):
            entry = SchemaVersion(
                version=raw['version'],
                schema=decode_schema(raw['schema']),
                created_at=raw['created_at'])
            registry._versions[entry.version] = entry
        return registry

    def save(self, path: str) -> None:
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(self.to_dict(), handle, indent=2, ensure_ascii=False)

    @classmethod
    def load(cls, path: str) -> 'SchemaRegistry':
        with open(path, 'r', encoding='utf-8') as handle:
            return cls.from_dict(json.load(handle))
