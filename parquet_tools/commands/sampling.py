import json
import re
from argparse import ArgumentParser
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, List, Optional, Tuple

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from .utils import (LocalParquetFile, ParquetFile, S3ParquetFile,
                    _resolve_wildcard, InvalidCommandExcpetion)

FILE_COLUMN = '_source_file'
ROW_GROUP_COLUMN = '_row_group'
ROW_IN_GROUP_COLUMN = '_row_in_group'

_WHERE_PATTERN = re.compile(
    r'^\s*([A-Za-z_][\w]*)\s*(==|!=|<=|>=|<|>)\s*(.+?)\s*$'
)


class PredicateError(InvalidCommandExcpetion):
    pass


@dataclass(frozen=True)
class Predicate:
    '''Row filter expressed as `column op literal`.

    The literal is kept as a string in the credential and coerced to the
    physical column type each time a file is scanned.
    '''
    column: str
    op: str
    literal: str

    @classmethod
    def parse(cls, spec: str) -> 'Predicate':
        match = _WHERE_PATTERN.match(spec or '')
        if not match:
            raise PredicateError(
                f'`{spec}` is not a supported predicate. '
                'Use a form like: status == OK')
        column, op, literal = match.groups()
        if len(literal) >= 2 and literal[0] == literal[-1] and literal[0] in ('\'', '"'):
            literal = literal[1:-1]
        return cls(column=column, op=op, literal=literal)

    @property
    def spec(self) -> str:
        return f'{self.column} {self.op} {self.literal}'

    def coerce_literal(self, arrow_type: pa.DataType) -> Any:
        if pa.types.is_integer(arrow_type):
            return int(self.literal)
        if pa.types.is_floating(arrow_type):
            return float(self.literal)
        if pa.types.is_boolean(arrow_type):
            return self.literal.lower() == 'true'
        return self.literal

    def matches(self, value: Any, literal: Any) -> bool:
        # SQL-ish null semantics: a null never satisfies a predicate.
        if value is None:
            return False
        if self.op == '==':
            return value == literal
        if self.op == '!=':
            return value != literal
        if self.op == '<':
            return value < literal
        if self.op == '<=':
            return value <= literal
        if self.op == '>':
            return value > literal
        if self.op == '>=':
            return value >= literal
        raise PredicateError(f'Unsupported operator: {self.op}')

    def can_prune(self, statistics: Any, literal: Any) -> bool:
        '''Tell if a whole row group is guaranteed to have no matching row.

        Only reliable min/max statistics are trusted. When statistics are
        missing or cannot decide the predicate, real rows must be evaluated.
        '''
        if statistics is None or not statistics.has_min_max:
            return False
        minimum = statistics.min
        maximum = statistics.max
        if self.op == '==':
            return literal < minimum or literal > maximum
        if self.op == '!=':
            no_null = (not statistics.has_null_count) or statistics.null_count == 0
            return no_null and minimum == maximum == literal
        if self.op == '<':
            return minimum >= literal
        if self.op == '<=':
            return minimum > literal
        if self.op == '>':
            return maximum <= literal
        if self.op == '>=':
            return maximum < literal
        return False


@dataclass
class SampleRef:
    '''One selected row addressed by file, row group and position in group.'''
    seq: int
    file: int
    row_group: int
    row: int


@dataclass
class Checkpoint:
    '''Next unscanned position of a file.'''
    row_group: int = 0
    row: int = 0


@dataclass
class SampleToken:
    '''Serializable sampling credential shared by show and csv.

    The credential fixes the actual sampling order. Different display column
    sets must reuse the same token so that row selection never changes.
    '''
    version: int
    predicate: str
    files: List[str]
    columns: List[str]
    refs: List[SampleRef] = field(default_factory=list)
    checkpoints: List[Checkpoint] = field(default_factory=list)
    files_done: List[bool] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps({
            'version': self.version,
            'predicate': self.predicate,
            'files': self.files,
            'columns': self.columns,
            'refs': [ref.__dict__ for ref in self.refs],
            'checkpoints': [cp.__dict__ for cp in self.checkpoints],
            'files_done': self.files_done,
        }, indent=2)

    @classmethod
    def from_json(cls, text: str) -> 'SampleToken':
        data = json.loads(text)
        return cls(
            version=data['version'],
            predicate=data['predicate'],
            files=data['files'],
            columns=data['columns'],
            refs=[SampleRef(**ref) for ref in data['refs']],
            checkpoints=[Checkpoint(**cp) for cp in data['checkpoints']],
            files_done=list(data['files_done']),
        )

    def dump(self, path: str) -> None:
        Path(path).write_text(self.to_json())

    @classmethod
    def load(cls, path: str) -> 'SampleToken':
        try:
            return cls.from_json(Path(path).read_text())
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            raise InvalidCommandExcpetion(
                f'The sampling credential {path} is not valid: {exc}')


def add_sampling_arguments(parser: ArgumentParser) -> None:
    '''Arguments shared by `show` and `csv` for resumable quota reads.'''
    parser.add_argument('--sample',
                        type=int,
                        required=False,
                        default=None,
                        help='''How many matching rows to read with a resumable
                             sampling credential. Requires --where on the first run.
                             e.g. --sample 20''')
    parser.add_argument('--where',
                        type=str,
                        required=False,
                        default=None,
                        help='''Filter predicate over a column, e.g. --where "status == OK".
                             The column does not need to be part of --columns.''')
    parser.add_argument('--sample-token',
                        type=str,
                        required=False,
                        default=None,
                        help='''Path of the sampling credential to continue.
                             When omitted a fresh sampling run starts.''')
    parser.add_argument('--emit-token',
                        type=str,
                        required=False,
                        default=None,
                        help='Write the sampling credential to this path for later continuation.')
    parser.add_argument('--reconcile',
                        action='store_true',
                        required=False,
                        default=False,
                        help='''Append _source_file, _row_group and _row_in_group
                             columns to reconcile each row against a full read.''')


def parquet_file_key(pf: ParquetFile) -> str:
    if isinstance(pf, LocalParquetFile):
        return str(Path(pf.path).resolve())
    if isinstance(pf, S3ParquetFile):
        return f's3://{pf.bucket}/{pf.key}'
    raise InvalidCommandExcpetion(f'Unsupported parquet file: {pf}')


def _resolve_files(objs: List[ParquetFile]) -> List[ParquetFile]:
    files: List[ParquetFile] = []
    for obj in objs:
        files.extend(_resolve_wildcard(obj))
    return files


def _validate_token(token: SampleToken, predicate: Predicate,
                    file_keys: List[str]) -> None:
    if token.version != 1:
        raise InvalidCommandExcpetion(
            f'Unsupported sampling credential version: {token.version}')
    if token.predicate != predicate.spec:
        raise InvalidCommandExcpetion(
            'The sampling credential was issued for predicate '
            f'`{token.predicate}` but got `{predicate.spec}`. '
            'A new sampling run needs a new credential.')
    if token.files != file_keys:
        raise InvalidCommandExcpetion(
            'The sampling credential was issued for files '
            f'{token.files} but got {file_keys}. '
            'A new sampling run needs a new credential.')
    if len(token.checkpoints) != len(file_keys) or len(token.files_done) != len(file_keys):
        raise InvalidCommandExcpetion('The sampling credential is corrupt.')


def _scan_file(reader: pq.ParquetFile, file_index: int, token: SampleToken,
               predicate: Predicate, need: int) -> int:
    '''Scan one file from its checkpoint until `need` rows are selected.'''
    schema = reader.schema_arrow
    if predicate.column not in schema.names:
        raise PredicateError(
            f'Predicate column `{predicate.column}` does not exist '
            f'in file {token.files[file_index]}')
    column_index = schema.names.index(predicate.column)
    literal = predicate.coerce_literal(schema.field(predicate.column).type)

    checkpoint = token.checkpoints[file_index]
    group_index = checkpoint.row_group
    while need > 0 and group_index < reader.num_row_groups:
        statistics = reader.metadata.row_group(group_index) \
            .column(column_index).statistics
        if predicate.can_prune(statistics, literal):
            # Reliable statistics prove no row matches: no column data is read.
            group_index += 1
            checkpoint.row_group = group_index
            checkpoint.row = 0
            continue

        values = reader.read_row_group(
            group_index, columns=[predicate.column]).column(0).to_pylist()
        start_row = checkpoint.row if group_index == checkpoint.row_group else 0
        row_index = start_row
        while row_index < len(values) and need > 0:
            if predicate.matches(values[row_index], literal):
                token.refs.append(SampleRef(
                    seq=len(token.refs) + 1,
                    file=file_index,
                    row_group=group_index,
                    row=row_index,
                ))
                need -= 1
            row_index += 1

        if need == 0:
            # Quota reached inside this row group: keep the exact resume point.
            checkpoint.row_group = group_index
            checkpoint.row = row_index
            break
        group_index += 1
        checkpoint.row_group = group_index
        checkpoint.row = 0
    else:
        if need > 0:
            token.files_done[file_index] = True
    return need


def _scan(files: List[ParquetFile], token: SampleToken,
          predicate: Predicate, need: int) -> None:
    for file_index, pf in enumerate(files):
        if need <= 0:
            break
        if token.files_done[file_index]:
            continue
        with pf.get_local_path() as local_path:
            reader = pq.ParquetFile(local_path)
            need = _scan_file(reader, file_index, token, predicate, need)


def _build_dataframe(files: List[ParquetFile], token: SampleToken,
                     columns: List[str], quota: int,
                     reconcile: bool) -> pd.DataFrame:
    '''Project display columns for the selected rows.

    Each row group backing a selected row is opened at most once, and only
    the requested display columns are read. A column declared in the request
    but absent from a file is rendered empty without touching that file.
    '''
    refs = token.refs[:quota]
    needed: dict = {}
    for ref in refs:
        needed.setdefault((ref.file, ref.row_group), []).append(ref)

    rows_by_seq: dict = {}
    with ExitStack() as stack:
        local_paths: dict = {}
        for (file_index, group_index), group_refs in needed.items():
            pf = files[file_index]
            if file_index not in local_paths:
                # One local handle per file, even when several groups are read.
                local_paths[file_index] = stack.enter_context(pf.get_local_path())
            local_path = local_paths[file_index]
            reader = pq.ParquetFile(local_path)
            schema_names = reader.schema_arrow.names
            present = [name for name in columns if name in schema_names]
            missing = [name for name in columns if name not in schema_names]
            if present:
                # Only the display columns of this one row group are read.
                table = reader.read_row_group(group_index, columns=present)
            for ref in group_refs:
                row: dict = {}
                for name in columns:
                    if name in missing:
                        row[name] = None
                    else:
                        row[name] = table.column(name)[ref.row].as_py()
                if reconcile:
                    row[FILE_COLUMN] = local_path
                    row[ROW_GROUP_COLUMN] = ref.row_group
                    row[ROW_IN_GROUP_COLUMN] = ref.row
                rows_by_seq[ref.seq] = row

    rows = [rows_by_seq[ref.seq] for ref in refs]
    frame_columns = list(columns)
    if reconcile:
        frame_columns += [FILE_COLUMN, ROW_GROUP_COLUMN, ROW_IN_GROUP_COLUMN]
    return pd.DataFrame(rows, columns=frame_columns)


def _all_schema_names(files: List[ParquetFile]) -> List[str]:
    names: List[str] = []
    with ExitStack() as stack:
        for pf in files:
            local_path = stack.enter_context(pf.get_local_path())
            for name in pq.ParquetFile(local_path).schema_arrow.names:
                if name not in names:
                    names.append(name)
    return names


def run_sampling(objs: List[ParquetFile], *, quota: int,
                 predicate_spec: Optional[str], columns: List[str],
                 token: Optional[SampleToken] = None,
                 reconcile: bool = False) -> Tuple[pd.DataFrame, SampleToken]:
    '''Run a resumable, quota-limited filtered read.

    Selection order is owned by the credential: continuing with a different
    display projection never re-decides which rows are selected.
    '''
    if quota is None or quota <= 0:
        raise InvalidCommandExcpetion('--sample requires a positive integer')
    files = _resolve_files(objs)
    if not files:
        raise InvalidCommandExcpetion('File matching that expression not found.')

    if not columns:
        columns = _all_schema_names(files)
    file_keys = [parquet_file_key(pf) for pf in files]
    if token is None:
        if not predicate_spec:
            raise PredicateError('--where is required on the first sampling run')
        predicate = Predicate.parse(predicate_spec)
        token = SampleToken(
            version=1,
            predicate=predicate.spec,
            files=file_keys,
            columns=list(columns),
            checkpoints=[Checkpoint() for _ in files],
            files_done=[False for _ in files],
        )
    else:
        predicate = Predicate.parse(predicate_spec or token.predicate)
        _validate_token(token, predicate, file_keys)

    need = max(0, quota - len(token.refs))
    if need > 0:
        _scan(files, token, predicate, need)

    df = _build_dataframe(files, token, columns, quota, reconcile)
    return df, token
