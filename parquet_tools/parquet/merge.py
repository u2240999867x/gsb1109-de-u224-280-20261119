'''Merge parquet files into one table under a caller-specified target schema.

The reader side supplies the target schema (for example a billing schema whose
amount is a fixed precision/scale Decimal). Every input file is validated on
its own before any of its rows reach the unified table:

- only paths requested by the target schema are inspected; extra source
  columns (including same-named siblings of requested paths) are ignored
- a missing path is tolerated only when the target marks it nullable, in
  which case nulls are filled in
- a decimal source may be rescaled to the target scale only when every value
  is exactly reducible (trailing zeros); row groups that break this are
  reported by index and the whole file is rejected, including its earlier
  well-formed row groups
- floats and other non-decimal sources are never silently rounded or
  coerced into a decimal target

Input order only decides the order of the per-file blocks in the output
table; it never changes which files are accepted or the result column types,
which always come from the target schema rather than the first file.

Reading without a target schema stays available through the existing
``parquet_tools.commands.utils.get_datafame_from_objs``.
'''

import logging
from dataclasses import dataclass, field
from typing import List, Optional, Sequence, Tuple, Union

import pyarrow as pa
import pyarrow.parquet as pq

logger = logging.getLogger(__name__)

UNREADABLE_FILE = 'unreadable-file'
MISSING_REQUIRED_FIELD = 'missing-required-field'
TYPE_MISMATCH = 'type-mismatch'
INEXACT_DECIMAL_REDUCTION = 'inexact-decimal-reduction'


@dataclass
class FieldError:
    '''One validation failure, located by the full dotted field path.

    ``row_groups`` lists the source row group indices confirmed to contain
    offending values; it is empty for schema-level failures.
    '''
    path: str
    reason: str
    detail: str = ''
    row_groups: List[int] = field(default_factory=list)

    def __str__(self) -> str:
        msg = f'{self.path}: {self.reason}'
        if self.row_groups:
            msg += f' (row groups: {self.row_groups})'
        if self.detail:
            msg += f' - {self.detail}'
        return msg


@dataclass
class FileAcceptance:
    '''Per-file acceptance result.'''
    path: str
    accepted: bool
    errors: List[FieldError] = field(default_factory=list)

    def __str__(self) -> str:
        if self.accepted:
            return f'{self.path}: accepted'
        details = '\n  '.join(str(e) for e in self.errors)
        return f'{self.path}: rejected\n  {details}'


@dataclass
class MergeResult:
    '''Result of reading a batch of files with a target schema.

    ``table`` holds only the accepted files, as per-file blocks in input
    order, with exactly ``schema`` as its column types and paths.
    '''
    schema: pa.Schema
    table: pa.Table
    files: List[FileAcceptance]

    @property
    def accepted_files(self) -> List[str]:
        return [f.path for f in self.files if f.accepted]

    @property
    def rejected_files(self) -> List[str]:
        return [f.path for f in self.files if not f.accepted]

    def format_report(self) -> str:
        return '\n'.join(str(f) for f in self.files)


@dataclass
class _DecimalCheck:
    '''A decimal path whose source scale exceeds the target scale and whose
    values therefore need an exact-reduction check per row group.'''
    path: str
    source_type: pa.DataType
    target_type: pa.DataType


def read_with_schema(paths: Sequence[Union[str, 'PathLike']], schema: pa.Schema) -> MergeResult:
    '''Read ``paths`` into one table typed and laid out by ``schema``.

    Each file is fully validated first; a rejected file contributes no rows
    at all. Accepted files appear as blocks in the order given by ``paths``.
    '''
    files: List[FileAcceptance] = []
    tables: List[pa.Table] = []
    for path in paths:
        acceptance, table = _process_file(str(path), schema)
        files.append(acceptance)
        if acceptance.accepted:
            tables.append(table)
            logger.info(f'{path}: accepted ({table.num_rows} rows)')
        else:
            logger.info(f'{path}: rejected\n{acceptance}')
    if tables:
        unified = pa.concat_tables(tables, promote_options='none')
    else:
        unified = schema.empty_table()
    return MergeResult(schema=schema, table=unified, files=files)


def _process_file(path: str, target_schema: pa.Schema) -> Tuple[FileAcceptance, Optional[pa.Table]]:
    try:
        parquet_file = pq.ParquetFile(path)
    except Exception as exc:
        error = FieldError('', UNREADABLE_FILE, str(exc))
        return FileAcceptance(path, False, [error]), None

    source_schema = parquet_file.schema_arrow
    errors: List[FieldError] = []
    decimal_checks: List[_DecimalCheck] = []
    for target_field in target_schema:
        index = source_schema.get_field_index(target_field.name)
        source_field = source_schema[index] if index != -1 else None
        _check_field(target_field, source_field, target_field.name, errors, decimal_checks)

    if not errors and decimal_checks:
        errors.extend(_run_decimal_checks(parquet_file, decimal_checks))

    if errors:
        return FileAcceptance(path, False, errors), None

    table = _build_table(parquet_file.read(), source_schema, target_schema)
    return FileAcceptance(path, True, []), table


def _check_field(target_field: pa.Field, source_field: Optional[pa.Field], path: str,
                 errors: List[FieldError], decimal_checks: List[_DecimalCheck]) -> None:
    if source_field is None:
        if not target_field.nullable:
            errors.append(FieldError(
                path, MISSING_REQUIRED_FIELD,
                'required by target schema but absent from source file'))
        return

    target_type = target_field.type
    source_type = source_field.type

    if pa.types.is_struct(target_type):
        if not pa.types.is_struct(source_type):
            errors.append(FieldError(
                path, TYPE_MISMATCH,
                f'target expects {target_type}, source has {source_type}'))
            return
        for child in target_type:
            index = source_type.get_field_index(child.name)
            source_child = source_type[index] if index != -1 else None
            _check_field(child, source_child, f'{path}.{child.name}', errors, decimal_checks)
        return

    if pa.types.is_decimal(target_type):
        if not pa.types.is_decimal(source_type):
            errors.append(FieldError(
                path, TYPE_MISMATCH,
                f'target expects exact {target_type}, source has {source_type}; '
                'refusing to write uncertain precision into the target'))
            return
        statically_exact = (
            source_type.scale <= target_type.scale
            and source_type.precision - source_type.scale
            <= target_type.precision - target_type.scale
        )
        if not statically_exact:
            decimal_checks.append(_DecimalCheck(path, source_type, target_type))
        return

    if pa.types.is_null(source_type):
        # an all-null column can fill any target path
        return

    if target_type != source_type:
        errors.append(FieldError(
            path, TYPE_MISMATCH,
            f'target expects {target_type}, source has {source_type}'))


def _run_decimal_checks(parquet_file: pq.ParquetFile,
                        checks: List[_DecimalCheck]) -> List[FieldError]:
    '''Confirm per row group that every value on a decimal path is exactly
    reducible to the target scale (only trailing zeros are dropped).'''
    failures: List[List[int]] = [[] for _ in checks]
    details: List[str] = ['' for _ in checks]
    top_levels = sorted({check.path.split('.')[0] for check in checks})
    for row_group in range(parquet_file.num_row_groups):
        table = parquet_file.read_row_group(row_group, columns=top_levels)
        for index, check in enumerate(checks):
            array = _navigate(table, check.path)
            try:
                array.cast(check.target_type)
            except pa.ArrowInvalid as exc:
                failures[index].append(row_group)
                details[index] = str(exc)
    return [
        FieldError(check.path, INEXACT_DECIMAL_REDUCTION, details[index], failures[index])
        for index, check in enumerate(checks)
        if failures[index]
    ]


def _navigate(table: pa.Table, path: str) -> Union[pa.Array, pa.ChunkedArray]:
    parts = path.split('.')
    array = table.column(parts[0])
    for part in parts[1:]:
        array = array.combine_chunks().field(part)
    return array


def _build_table(table: pa.Table, source_schema: pa.Schema, target_schema: pa.Schema) -> pa.Table:
    columns = []
    for target_field in target_schema:
        index = source_schema.get_field_index(target_field.name)
        if index == -1:
            columns.append(pa.nulls(table.num_rows, target_field.type))
        else:
            source_array = table.column(target_field.name)
            columns.append(_build_column(source_array, source_schema[index], target_field, table.num_rows))
    return pa.Table.from_arrays(columns, schema=target_schema)


def _build_column(source_array: Union[pa.Array, pa.ChunkedArray], source_field: pa.Field,
                  target_field: pa.Field, num_rows: int):
    target_type = target_field.type
    if source_array is None:
        return pa.nulls(num_rows, target_type)
    if pa.types.is_struct(target_type):
        if isinstance(source_array, pa.ChunkedArray):
            source_array = source_array.combine_chunks()
        children = []
        for child in target_type:
            index = source_field.type.get_field_index(child.name)
            child_array = source_array.field(child.name) if index != -1 else None
            child_field = source_field.type[index] if index != -1 else None
            children.append(_build_column(child_array, child_field, child, num_rows))
        return pa.StructArray.from_arrays(
            children, fields=list(target_type), mask=source_array.is_null())
    return source_array.cast(target_type)
