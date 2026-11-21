'''Footer-only column statistics report.

Every conclusion in this module is derived exclusively from the parquet
footer (FileMetaData thrift structure). No data page is ever read, so each
metric is annotated with an evidence status:

- proven: the footer metadata alone proves the value.
- unknown: the footer does not contain enough information.
- n/a: the metric has no meaning for the row group (zero rows / all null).
'''
import struct
from dataclasses import dataclass, field
from typing import Any, List, Optional

from parquet_tools.gen_py.parquet.ttypes import (
    ColumnChunk,
    ConvertedType,
    FieldRepetitionType,
    FileMetaData,
    Type,
)
from parquet_tools.parquet.reader import get_filemetadata

STATUS_PROVEN = 'proven'
STATUS_UNKNOWN = 'unknown'
STATUS_NA = 'n/a'

STATE_OK = 'ok'
STATE_PARTIAL = 'partial statistics'
STATE_ALL_NULL = 'all null'
STATE_ZERO_ROWS = 'zero rows'
STATE_NO_STATS = 'statistics missing'

_UNSIGNED_CONVERTED_TYPES = {
    ConvertedType.UINT_8,
    ConvertedType.UINT_16,
    ConvertedType.UINT_32,
    ConvertedType.UINT_64,
}

LIST_LEAF_CAVEAT = ('list/repeated leaf: null_count counts leaf values, not rows; '
                    'row-level null presence has no evidence')


@dataclass
class Metric:
    '''A single statistic with its evidence status.'''
    value: Optional[Any]
    status: str
    reason: str = ''
    note: str = ''
    evidence: str = ''

    def render(self) -> str:
        if self.status == STATUS_PROVEN:
            suffix = f' [{self.note}]' if self.note else ''
            return f'{self.value} (proven){suffix}'
        if self.status == STATUS_NA:
            return f'n/a ({self.reason})'
        text = f'unknown ({self.reason})'
        if self.evidence:
            text += f' [{self.evidence}]'
        return text


@dataclass
class ColumnInfo:
    '''A leaf column of the schema, addressed by its full field path.'''
    index: int
    path: List[str]
    physical_type: int
    logical_integer: Optional[Any]
    converted_type: Optional[int]
    max_definition_level: int
    max_repetition_level: int
    order_declared: bool = False

    @property
    def path_string(self) -> str:
        return '.'.join(self.path)

    @property
    def is_unsigned(self) -> bool:
        if self.logical_integer is not None:
            return self.logical_integer.isSigned is False
        return self.converted_type in _UNSIGNED_CONVERTED_TYPES

    @property
    def is_repeated_leaf(self) -> bool:
        return self.max_repetition_level > 0

    @property
    def logical_type_expression(self) -> str:
        if self.logical_integer is not None:
            signed = 'signed' if self.logical_integer.isSigned else 'unsigned'
            return f'INTEGER(bitWidth={self.logical_integer.bitWidth}, {signed})'
        if self.converted_type is not None:
            return ConvertedType._VALUES_TO_NAMES.get(self.converted_type, str(self.converted_type))
        return 'NONE'


@dataclass
class RowGroupReport:
    ordinal: int
    num_rows: int
    state: str
    min: Metric
    max: Metric
    null_count: Metric


@dataclass
class ColumnReport:
    column: ColumnInfo
    row_groups: List[RowGroupReport]
    file_min: Metric
    file_max: Metric
    file_null_count: Metric
    caveats: List[str] = field(default_factory=list)


def schema_columns(fmd: FileMetaData) -> List[ColumnInfo]:
    '''Reconstruct leaf columns (with full paths and levels) from the
    flattened schema of the footer.'''
    elements = fmd.schema
    columns: List[ColumnInfo] = []
    position = 0

    def visit(path: List[str], def_level: int, rep_level: int, is_root: bool) -> None:
        nonlocal position
        element = elements[position]
        position += 1
        if not is_root:
            if element.repetition_type != FieldRepetitionType.REQUIRED:
                def_level += 1
            if element.repetition_type == FieldRepetitionType.REPEATED:
                rep_level += 1
        if element.type is None:
            for _ in range(element.num_children or 0):
                visit(path if is_root else path + [element.name], def_level, rep_level, False)
        else:
            logical_integer = None
            if element.logicalType is not None and element.logicalType.INTEGER is not None:
                logical_integer = element.logicalType.INTEGER
            columns.append(ColumnInfo(
                index=len(columns),
                path=path + [element.name],
                physical_type=element.type,
                logical_integer=logical_integer,
                converted_type=element.converted_type,
                max_definition_level=def_level,
                max_repetition_level=rep_level,
            ))

    visit([], 0, 0, True)

    orders = fmd.column_orders or []
    for column in columns:
        column.order_declared = (
            column.index < len(orders) and orders[column.index].TYPE_ORDER is not None
        )
    return columns


def decode_value(raw: bytes, physical_type: int, unsigned: bool) -> Any:
    '''Decode a PLAIN-encoded statistics value from the footer.'''
    if physical_type == Type.BOOLEAN:
        return bool(raw[0])
    if physical_type == Type.INT32:
        return struct.unpack('<I' if unsigned else '<i', raw)[0]
    if physical_type == Type.INT64:
        return struct.unpack('<Q' if unsigned else '<q', raw)[0]
    if physical_type == Type.FLOAT:
        return struct.unpack('<f', raw)[0]
    if physical_type == Type.DOUBLE:
        return struct.unpack('<d', raw)[0]
    if physical_type in (Type.BYTE_ARRAY, Type.FIXED_LEN_BYTE_ARRAY):
        try:
            return raw.decode('utf-8')
        except UnicodeDecodeError:
            return '0x' + raw.hex()
    return None


def _bound_metric(statistics, column: ColumnInfo, is_min: bool) -> Metric:
    name = 'min' if is_min else 'max'
    if column.physical_type == Type.INT96:
        return Metric(None, STATUS_UNKNOWN, 'INT96 has no defined sort order')
    if column.order_declared:
        raw = statistics.min_value if is_min else statistics.max_value
        if raw is None:
            return Metric(None, STATUS_UNKNOWN, f'{name}_value not present in footer statistics')
        return Metric(decode_value(raw, column.physical_type, column.is_unsigned), STATUS_PROVEN)
    if column.is_unsigned:
        return Metric(None, STATUS_UNKNOWN,
                      f'unsigned column without declared column order: '
                      f'legacy signed {name} cannot prove the unsigned range')
    raw = statistics.min if is_min else statistics.max
    if raw is None:
        return Metric(None, STATUS_UNKNOWN, f'legacy {name} not present in footer statistics')
    return Metric(decode_value(raw, column.physical_type, False), STATUS_PROVEN)


def _analyze_row_group(ordinal: int, num_rows: int, chunk: ColumnChunk, column: ColumnInfo) -> RowGroupReport:
    null_note = 'counts leaf values, not rows' if column.is_repeated_leaf else ''
    meta = chunk.meta_data
    statistics = meta.statistics if meta is not None else None

    if num_rows == 0:
        return RowGroupReport(
            ordinal=ordinal,
            num_rows=0,
            state=STATE_ZERO_ROWS,
            min=Metric(None, STATUS_NA, 'row group has zero rows'),
            max=Metric(None, STATUS_NA, 'row group has zero rows'),
            null_count=Metric(0, STATUS_PROVEN, note=null_note),
        )

    if statistics is None:
        return RowGroupReport(
            ordinal=ordinal,
            num_rows=num_rows,
            state=STATE_NO_STATS,
            min=Metric(None, STATUS_UNKNOWN, 'statistics missing from footer'),
            max=Metric(None, STATUS_UNKNOWN, 'statistics missing from footer'),
            null_count=Metric(None, STATUS_UNKNOWN, 'statistics missing from footer'),
        )

    if statistics.null_count is not None:
        null_count = Metric(statistics.null_count, STATUS_PROVEN, note=null_note)
    else:
        null_count = Metric(None, STATUS_UNKNOWN, 'null_count not present in footer statistics')

    if statistics.null_count is not None and statistics.null_count == num_rows:
        return RowGroupReport(
            ordinal=ordinal,
            num_rows=num_rows,
            state=STATE_ALL_NULL,
            min=Metric(None, STATUS_NA, 'all values are null'),
            max=Metric(None, STATUS_NA, 'all values are null'),
            null_count=null_count,
        )

    min_metric = _bound_metric(statistics, column, is_min=True)
    max_metric = _bound_metric(statistics, column, is_min=False)
    metrics = [min_metric, max_metric, null_count]
    state = STATE_OK if all(m.status == STATUS_PROVEN for m in metrics) else STATE_PARTIAL
    return RowGroupReport(
        ordinal=ordinal,
        num_rows=num_rows,
        state=state,
        min=min_metric,
        max=max_metric,
        null_count=null_count,
    )


def _file_bound(row_groups: List[RowGroupReport], is_min: bool) -> Metric:
    name = 'min' if is_min else 'max'
    contributors = [rg for rg in row_groups if rg.state not in (STATE_ZERO_ROWS, STATE_ALL_NULL)]
    if not contributors:
        return Metric(None, STATUS_NA, 'no row group can contain non-null values')
    metrics = [getattr(rg, name) for rg in contributors]
    if all(m.status == STATUS_PROVEN for m in metrics):
        values = [m.value for m in metrics]
        return Metric(min(values) if is_min else max(values), STATUS_PROVEN)
    blocking = [rg.ordinal for rg, m in zip(contributors, metrics) if m.status != STATUS_PROVEN]
    known = [m.value for m in metrics if m.status == STATUS_PROVEN]
    evidence = ''
    if known:
        known_value = min(known) if is_min else max(known)
        evidence = f'known row-group {name}: {known_value}'
    return Metric(None, STATUS_UNKNOWN,
                  f'row groups {blocking} cannot prove a {name}',
                  evidence=evidence)


def _file_null_count(row_groups: List[RowGroupReport], note: str) -> Metric:
    if all(rg.null_count.status == STATUS_PROVEN for rg in row_groups):
        return Metric(sum(rg.null_count.value for rg in row_groups), STATUS_PROVEN, note=note)
    blocking = [rg.ordinal for rg in row_groups if rg.null_count.status != STATUS_PROVEN]
    known_sum = sum(rg.null_count.value for rg in row_groups if rg.null_count.status == STATUS_PROVEN)
    return Metric(None, STATUS_UNKNOWN,
                  f'row groups {blocking} cannot prove a null_count',
                  note=note,
                  evidence=f'known row-group null_count sum: {known_sum}')


def analyze_column(fmd: FileMetaData, column: ColumnInfo) -> ColumnReport:
    row_groups = [
        _analyze_row_group(ordinal, rg.num_rows or 0, rg.columns[column.index], column)
        for ordinal, rg in enumerate(fmd.row_groups)
    ]
    null_note = 'counts leaf values, not rows' if column.is_repeated_leaf else ''
    caveats = [LIST_LEAF_CAVEAT] if column.is_repeated_leaf else []
    return ColumnReport(
        column=column,
        row_groups=row_groups,
        file_min=_file_bound(row_groups, is_min=True),
        file_max=_file_bound(row_groups, is_min=False),
        file_null_count=_file_null_count(row_groups, null_note),
        caveats=caveats,
    )


def select_columns(fmd: FileMetaData, paths: Optional[List[str]]) -> List[ColumnInfo]:
    '''Select leaf columns by explicit full field paths (dot separated).
    All leaf columns are returned when paths is empty.'''
    columns = schema_columns(fmd)
    if not paths:
        return columns
    by_path = {column.path_string: column for column in columns}
    selected = []
    for path in paths:
        if path not in by_path:
            raise KeyError(path)
        selected.append(by_path[path])
    return selected


def build_report(filename: str, paths: Optional[List[str]] = None) -> str:
    '''Build the footer-only statistics report for a local parquet file.'''
    fmd = get_filemetadata(filename)
    try:
        columns = select_columns(fmd, paths)
    except KeyError as e:
        available = ', '.join(column.path_string for column in schema_columns(fmd))
        raise ValueError(f'Column path {e.args[0]!r} not found. Available paths: {available}')
    reports = [analyze_column(fmd, column) for column in columns]
    return render_report(fmd, reports, filename)


def render_report(fmd: FileMetaData, reports: List[ColumnReport], source: str) -> str:
    lines = [
        '############ footer statistics report ############',
        f'file: {source}',
        f'created_by: {fmd.created_by}',
        f'num_rows: {fmd.num_rows}',
        f'num_row_groups: {len(fmd.row_groups)}',
    ]
    for report in reports:
        column = report.column
        lines.append('')
        lines.append(f'############ Column({column.path_string}) ############')
        lines.append(f'physical_type: {Type._VALUES_TO_NAMES[column.physical_type]}')
        lines.append(f'logical_type: {column.logical_type_expression}')
        if column.order_declared:
            lines.append('sort_order: TYPE_ORDER (declared in column_orders)')
        else:
            lines.append('sort_order: not declared (legacy footer)')
        for caveat in report.caveats:
            lines.append(f'caveat: {caveat}')
        for rg in report.row_groups:
            lines.append(f'  row_group {rg.ordinal} (num_rows: {rg.num_rows}, state: {rg.state})')
            lines.append(f'    min: {rg.min.render()}')
            lines.append(f'    max: {rg.max.render()}')
            lines.append(f'    null_count: {rg.null_count.render()}')
        lines.append('  file total:')
        lines.append(f'    min: {report.file_min.render()}')
        lines.append(f'    max: {report.file_max.render()}')
        lines.append(f'    null_count: {report.file_null_count.render()}')
    return '\n'.join(lines)
