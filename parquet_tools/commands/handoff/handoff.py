'''Structured-column CSV handoff for parquet tables.

The CSV files stay plain text and are split with the standard library
``csv`` module only. Every business value is encoded as JSON inside its cell,
so nested structs, lists, decimals and strings containing delimiters, quotes
or newlines survive untouched. Each CSV is paired with a sidecar carrying the
exact Arrow schema (the "table definition") the batch was exported under.

A signed sidecar binds the CSV bytes to one exact schema version. A receiver
therefore never guesses a row's shape from the newest column layout: a batch
whose version is missing from the registry, whose schema does not match the
registry copy, or whose signature no longer verifies, is rejected as a whole,
while every other batch can still be reconciled.
'''

from __future__ import annotations

import csv
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Sequence

import pyarrow as pa
import pyarrow.parquet as pq

from .registry import (HandoffError, SchemaMismatchError, SchemaRegistry,
                       UnregisteredVersionError, all_field_paths,
                       decode_schema, encode_schema, ensure_subset_compatible,
                       widen_table)

SIDECAR_SUFFIX = '.sidecar.json'
CSV_SUFFIX = '.csv'
SIDECAR_FORMAT = 'parquet-tools.csv-handoff-sidecar/v1'


class BatchVerificationError(HandoffError):
    '''A batch sidecar does not verify against its CSV or the registry.'''


def _sha256_bytes(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _schema_fingerprint(schema: pa.Schema) -> str:
    return _sha256_bytes(encode_schema(schema).encode('ascii'))


def _json_default(value):
    import decimal
    if isinstance(value, decimal.Decimal):
        return str(value)
    raise TypeError(f'Object of type {type(value).__name__} is not CSV handoff serializable')


@dataclass(frozen=True)
class BatchDescriptor:
    '''Everything needed to locate and verify one exported batch.'''
    batch_name: str
    csv_path: str
    sidecar_path: str
    version: str
    row_count: int


@dataclass(frozen=True)
class RejectedRow:
    '''A row that cannot be verified under the batch's declared version.'''
    batch_name: str
    csv_row_number: int
    reason: str


@dataclass
class BatchReadout:
    '''Outcome of reading one batch under its own declared schema.'''
    descriptor: BatchDescriptor
    declared_schema: pa.Schema
    table: Optional[pa.Table]
    rejected_rows: List[RejectedRow] = field(default_factory=list)
    verified: bool = True

    @property
    def batch_name(self) -> str:
        return self.descriptor.batch_name

    @property
    def version(self) -> str:
        return self.descriptor.version


@dataclass
class HandoffResult:
    '''All verifiable rows plus the rows that had to be refused.'''
    table: pa.Table
    merged_schema: pa.Schema
    batch_readouts: List[BatchReadout]
    rejected_rows: List[RejectedRow]
    delivery_order: List[str]

    def rejected_for(self, batch_name: str) -> List[RejectedRow]:
        return [row for row in self.rejected_rows if row.batch_name == batch_name]


def write_batch(table: pa.Table, out_dir: str, batch_name: str, version: str,
                registry: Optional[SchemaRegistry] = None) -> BatchDescriptor:
    '''Export one table to CSV plus a signed, schema-carrying sidecar.'''
    if registry is not None:
        declared = registry.get(version).schema
        if not table.schema.equals(declared):
            raise HandoffError(
                f'Table schema does not match registered definition {version}')
        schema = declared
    else:
        schema = table.schema

    directory = Path(out_dir)
    directory.mkdir(parents=True, exist_ok=True)
    csv_path = directory / f'{batch_name}{CSV_SUFFIX}'
    sidecar_path = directory / f'{batch_name}{SIDECAR_SUFFIX}'

    rows = table.to_pylist()
    with open(csv_path, 'w', encoding='utf-8', newline='') as handle:
        writer = csv.writer(handle)
        writer.writerow(schema.names)
        for row in rows:
            writer.writerow([
                json.dumps(row[field.name], ensure_ascii=False, default=_json_default)
                for field in schema
            ])

    descriptor = _write_sidecar(csv_path, sidecar_path, batch_name, version, schema, len(rows))
    return descriptor


def _write_sidecar(csv_path: Path, sidecar_path: Path, batch_name: str,
                   version: str, schema: pa.Schema, row_count: int) -> BatchDescriptor:
    csv_bytes = csv_path.read_bytes()
    csv_digest = _sha256_bytes(csv_bytes)
    schema_digest = _schema_fingerprint(schema)
    sidecar = {
        'format': SIDECAR_FORMAT,
        'batch_name': batch_name,
        'version': version,
        'row_count': row_count,
        'csv_file': csv_path.name,
        'csv_sha256': csv_digest,
        'schema_sha256': schema_digest,
        'field_paths': all_field_paths(schema),
        'schema': encode_schema(schema),
        'signature': _sha256_bytes(
            f'{csv_digest}:{schema_digest}:{version}'.encode('ascii')),
    }
    sidecar_path.write_text(json.dumps(sidecar, indent=2), encoding='utf-8')
    return BatchDescriptor(
        batch_name=batch_name,
        csv_path=str(csv_path),
        sidecar_path=str(sidecar_path),
        version=version,
        row_count=row_count)


def _load_sidecar(sidecar_path: Path) -> dict:
    return json.loads(sidecar_path.read_text(encoding='utf-8'))


def verify_sidecar(sidecar: dict, csv_path: Path, registry: SchemaRegistry) -> pa.Schema:
    '''Check signature, registry membership and exact schema identity.'''
    csv_digest = _sha256_bytes(csv_path.read_bytes())
    if csv_digest != sidecar['csv_sha256']:
        raise BatchVerificationError(
            f'{sidecar.get("batch_name", csv_path.name)}: CSV content does not match the sidecar checksum')
    schema = decode_schema(sidecar['schema'])
    schema_digest = _schema_fingerprint(schema)
    if schema_digest != sidecar['schema_sha256']:
        raise BatchVerificationError(
            f'{sidecar.get("batch_name", csv_path.name)}: embedded schema is corrupted')
    expected_signature = _sha256_bytes(
        f'{csv_digest}:{schema_digest}:{sidecar["version"]}'.encode('ascii'))
    if expected_signature != sidecar['signature']:
        raise BatchVerificationError(
            f'{sidecar.get("batch_name", csv_path.name)}: sidecar signature does not verify')

    version = sidecar['version']
    if version not in registry:
        raise UnregisteredVersionError(
            f'{sidecar.get("batch_name", csv_path.name)}: declares unknown schema version {version}')
    registered = registry.get(version).schema
    if not registered.equals(schema):
        raise SchemaMismatchError(
            f'{sidecar.get("batch_name", csv_path.name)}: sidecar schema for {version} '
            'differs from the registry definition')
    return schema


def _coerce_value(value, data_type: pa.DataType):
    if value is None:
        return None
    if pa.types.is_struct(data_type):
        if not isinstance(value, dict):
            raise HandoffError(f'Expected struct, got {type(value).__name__}')
        return {
            field.name: _coerce_value(value.get(field.name), field.type)
            for field in data_type
        }
    if pa.types.is_list(data_type) or pa.types.is_large_list(data_type):
        if not isinstance(value, list):
            raise HandoffError(f'Expected list, got {type(value).__name__}')
        return [_coerce_value(item, data_type.value_type) for item in value]
    if pa.types.is_decimal(data_type):
        from decimal import Decimal, InvalidOperation
        try:
            return Decimal(str(value))
        except InvalidOperation as exc:
            raise HandoffError(f'Invalid decimal literal {value!r}') from exc
    if pa.types.is_boolean(data_type):
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() == 'true'
    if pa.types.is_integer(data_type):
        return int(value)
    if pa.types.is_floating(data_type):
        return float(value)
    return str(value)


def _validate_paths(value, data_type: pa.DataType, path: List[str]) -> None:
    if value is None:
        return
    if pa.types.is_struct(data_type):
        if not isinstance(value, dict):
            raise HandoffError(f'{".".join(path)} expected a struct object')
        members = {field.name: field for field in data_type}
        for key in value:
            if key not in members:
                raise HandoffError(
                    f'{".".join([*path, key])} is not declared by this schema version')
        for key, nested in value.items():
            _validate_paths(nested, members[key].type, [*path, key])
    elif pa.types.is_list(data_type) or pa.types.is_large_list(data_type):
        if not isinstance(value, list):
            raise HandoffError(f'{".".join(path)} expected a list')
        for index, item in enumerate(value):
            _validate_paths(item, data_type.value_type, [*path, f'[{index}]'])


def _decode_csv(csv_path: Path, schema: pa.Schema) -> List[dict]:
    records: List[dict] = []
    with open(csv_path, 'r', encoding='utf-8', newline='') as handle:
        reader = csv.reader(handle)
        header = next(reader)
        if header != schema.names:
            raise HandoffError(
                f'{csv_path.name}: CSV header {header} does not match schema columns {schema.names}')
        for raw_row in reader:
            if len(raw_row) != len(schema.names):
                raise HandoffError(f'{csv_path.name}: row width does not match the header')
            decoded = {
                name: json.loads(cell) if cell != '' else None
                for name, cell in zip(schema.names, raw_row)
            }
            records.append(decoded)
    return records


def read_batch(csv_path: str, registry: SchemaRegistry,
               sidecar_path: Optional[str] = None) -> BatchReadout:
    '''Read one batch under the exact schema version its sidecar declares.

    Raises a :class:`HandoffError` subclass when the sidecar cannot be
    verified against the CSV or the registry; row-level decoding problems
    inside a verified batch are returned as rejected rows instead.
    '''
    csv_file = Path(csv_path)
    sidecar_file = Path(sidecar_path) if sidecar_path else csv_file.with_suffix(SIDECAR_SUFFIX)
    batch_name = csv_file.name[:-len(CSV_SUFFIX)] if csv_file.name.endswith(CSV_SUFFIX) else csv_file.stem
    sidecar = _load_sidecar(sidecar_file)
    descriptor = BatchDescriptor(
        batch_name=sidecar.get('batch_name', batch_name),
        csv_path=str(csv_file),
        sidecar_path=str(sidecar_file),
        version=sidecar['version'],
        row_count=sidecar['row_count'])

    schema = verify_sidecar(sidecar, csv_file, registry)
    records = _decode_csv(csv_file, schema)
    rejected: List[RejectedRow] = []
    coerced: List[dict] = []
    for offset, record in enumerate(records, start=2):
        try:
            _validate_paths(record, pa.struct(list(schema)), [])
            coerced.append(_coerce_value(record, pa.struct(list(schema))))
        except HandoffError as exc:
            rejected.append(RejectedRow(descriptor.batch_name, offset, str(exc)))
    table = pa.Table.from_pylist(coerced, schema=schema) if coerced else _empty_table(schema)
    return BatchReadout(descriptor, schema, table, rejected, verified=True)


def try_read_batch(csv_path: str, registry: SchemaRegistry,
                   sidecar_path: Optional[str] = None) -> BatchReadout:
    '''Non-raising variant of :func:`read_batch` for bulk reconciliation.'''
    csv_file = Path(csv_path)
    sidecar_file = Path(sidecar_path) if sidecar_path else csv_file.with_suffix(SIDECAR_SUFFIX)
    batch_name = csv_file.name[:-len(CSV_SUFFIX)] if csv_file.name.endswith(CSV_SUFFIX) else csv_file.stem
    fallback_schema = registry.newest.schema if registry.versions else pa.schema([])

    def _refuse(descriptor: BatchDescriptor, exc: Exception) -> BatchReadout:
        rejected = [
            RejectedRow(descriptor.batch_name, row_number, str(exc))
            for row_number in range(2, descriptor.row_count + 2)
        ] or [RejectedRow(descriptor.batch_name, 0, str(exc))]
        return BatchReadout(descriptor, fallback_schema, None, rejected, verified=False)

    try:
        sidecar = _load_sidecar(sidecar_file)
        descriptor = BatchDescriptor(
            batch_name=sidecar.get('batch_name', batch_name),
            csv_path=str(csv_file),
            sidecar_path=str(sidecar_file),
            version=sidecar.get('version', '<unknown>'),
            row_count=sidecar.get('row_count', _count_data_rows(csv_file)))
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        descriptor = BatchDescriptor(
            batch_name=batch_name,
            csv_path=str(csv_file),
            sidecar_path=str(sidecar_file),
            version='<unknown>',
            row_count=_count_data_rows(csv_file))
        return _refuse(descriptor, exc)

    try:
        return read_batch(str(csv_file), registry, str(sidecar_file))
    except HandoffError as exc:
        return _refuse(descriptor, exc)


def _count_data_rows(csv_file: Path) -> int:
    with open(csv_file, 'r', encoding='utf-8', newline='') as handle:
        return max(sum(1 for _ in csv.reader(handle)) - 1, 0)


def _empty_table(schema: pa.Schema) -> pa.Table:
    return pa.Table.from_arrays(
        [pa.array([], type=field.type) for field in schema], schema=schema)


def discover_batches(handoff_dir: str) -> List[Path]:
    directory = Path(handoff_dir)
    return sorted(
        csv_file for csv_file in directory.glob(f'*{CSV_SUFFIX}')
        if csv_file.with_suffix(SIDECAR_SUFFIX).exists()
    )


def read_handoff(handoff_dir: str, registry: SchemaRegistry,
                 delivery_order: Optional[Sequence[str]] = None) -> HandoffResult:
    '''Read every batch in a handoff directory and reconcile verifiable rows.

    ``delivery_order`` lists CSV file (or batch) names in the order the
    receiver actually takes delivery; rows are concatenated in that order so
    the sender's export order need not match.
    '''
    csv_files = discover_batches(handoff_dir)
    by_name = {path.name: path for path in csv_files}
    by_batch = {path.name[:-len(CSV_SUFFIX)]: path for path in csv_files}
    if delivery_order is None:
        ordered = csv_files
    else:
        ordered = []
        for name in delivery_order:
            if name in by_name:
                ordered.append(by_name[name])
            elif name in by_batch:
                ordered.append(by_batch[name])
            else:
                raise HandoffError(f'{name} is not part of the handoff at {handoff_dir}')

    readouts = [try_read_batch(str(csv_file), registry) for csv_file in ordered]
    merged_schema = registry.merged_schema() if registry.versions else pa.schema([])
    widened = [
        widen_table(readout.table, merged_schema)
        for readout in readouts
        if readout.table is not None and readout.table.num_rows > 0
    ]
    table = pa.concat_tables(widened) if widened else _empty_table(merged_schema)
    rejected = [row for readout in readouts for row in readout.rejected_rows]
    return HandoffResult(
        table=table,
        merged_schema=merged_schema,
        batch_readouts=readouts,
        rejected_rows=rejected,
        delivery_order=[path.name for path in ordered])


def physical_missing_paths(parquet_path: str, declared: pa.Schema) -> List[str]:
    '''Declared field paths that a parquet file does not physically contain.

    Reading the physical footer (rather than the projected view) keeps the
    distinction between a member that never existed and an explicit null.
    '''
    physical = pq.read_schema(parquet_path)
    ensure_subset_compatible(physical, declared)
    return sorted(set(all_field_paths(declared)) - set(all_field_paths(physical)))


def value_at(row: Optional[dict], path: Sequence):
    '''Walk a decoded row; a missing key and an explicit null stay distinct.

    List elements are addressed by integer index (or numeric strings).
    Returns ``(present, value)`` where ``present`` is False exactly when the
    path does not physically occur in the row, so absence never looks like
    an explicit ``None``.
    '''
    current = row
    for part in path:
        if isinstance(current, list):
            try:
                index = part if isinstance(part, int) else int(part)
            except (TypeError, ValueError):
                return (False, None)
            if index < 0 or index >= len(current):
                return (False, None)
            current = current[index]
        elif not isinstance(current, dict) or part not in current:
            return (False, None)
        else:
            current = current[part]
    return (True, current)
