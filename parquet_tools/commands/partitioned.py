"""Declared-partition reads.

The archive embeds partition columns (e.g. ``date`` and ``region``) in the
directory layout while parquet files may carry their own physical region
column.  This module adds two reading modes on top of the legacy readers:

* ``read_report`` projects measurement columns for reporting and applies the
  usual region/date filters.
* ``build_receipts`` reconciles the physical region column against the path
  declaration for every row group, even when the report filter would drop the
  conflicting rows or the caller already sampled enough rows.

Legacy reads that do not declare partition columns keep using
``commands.utils`` unchanged.
"""
import os
import re
from dataclasses import dataclass, field
from logging import getLogger
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple
from urllib.parse import unquote, urlparse

import pandas as pd
import pyarrow.parquet as pq

from .utils import (LocalParquetFile, ParquetFile, S3ParquetFile,
                    _resolve_wildcard, get_aws_session)

logger = getLogger(__name__)

SEGMENT_PATTERN = re.compile(r'^([^=/]+)=(.*)$')
FILTER_PATTERN = re.compile(
    r'^\s*([A-Za-z_][\w]*)\s*(==|!=|<=|>=|<|>)\s*(.*?)\s*$')
UNFINISHED_MARKER = '_incomplete'


class PartitionError(Exception):
    """Base exception for declared-partition reads."""


class PartitionConflictError(PartitionError):
    """Partition layers contradict each other inside a single path."""

    def __init__(self, message: str, *, path: str, column: str,
                 expected: Optional[str], actual: Optional[str]) -> None:
        super().__init__(message)
        self.path = path
        self.column = column
        self.expected = expected
        self.actual = actual


class PartitionDeclarationError(PartitionError):
    """Path and physical columns cannot be reconciled."""


@dataclass(frozen=True)
class PartitionDeclaration:
    """Business root and the partition columns declared for the archive."""

    business_root: str
    columns: Tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.columns:
            raise PartitionError('At least one partition column must be declared')


@dataclass(frozen=True)
class Filter:
    column: str
    op: str
    value: str


@dataclass(frozen=True)
class ConflictRecord:
    """A physical row whose region contradicts the path declaration."""

    path: str
    row_group: int
    row_index: int
    column: str
    expected: str
    actual: Any
    filtered_for_report: bool


@dataclass
class GroupDiagnostic:
    index: int
    row_offset: int
    num_rows: int
    status: str
    detail: str
    filtered_for_report: bool = False


@dataclass
class FileReceipt:
    path: str
    region_column: str
    partitions: Dict[str, str]
    status: str
    groups: List[GroupDiagnostic] = field(default_factory=list)
    conflicts: List[ConflictRecord] = field(default_factory=list)
    null_rows: List[int] = field(default_factory=list)
    region_filled: bool = False
    unfinished: bool = False

    @property
    def ok(self) -> bool:
        return self.status == 'passed'

    @property
    def accepted_rows(self) -> int:
        if self.status != 'passed':
            return 0
        return sum(group.num_rows for group in self.groups
                   if not group.filtered_for_report)


# ---------------------------------------------------------------------------
# Path handling
# ---------------------------------------------------------------------------
def _split_s3(root: str) -> Tuple[Optional[str], str]:
    if root.startswith('s3://'):
        parsed = urlparse(root)
        return parsed.netloc, parsed.path.lstrip('/')
    return None, root


def is_s3_file(pf: ParquetFile) -> bool:
    return isinstance(pf, S3ParquetFile)


def display_path(pf: ParquetFile) -> str:
    if isinstance(pf, S3ParquetFile):
        return f's3://{pf.bucket}/{pf.key}'
    assert isinstance(pf, LocalParquetFile)
    return pf.path


def parse_partitions(path: str, declaration: PartitionDeclaration,
                     *, is_s3: bool) -> Dict[str, str]:
    """Extract declared partition values from a file path.

    Each raw segment is percent-decoded exactly once, relative to the business
    root.  Directories above the business root are never inspected, so a
    region-looking directory outside the root cannot pollute the result.
    """
    _, root_value = _split_s3(declaration.business_root)
    if is_s3:
        key = path.lstrip('/')
        prefix = root_value.strip('/')
        if prefix and key.startswith(prefix):
            relative = key[len(prefix):].lstrip('/')
        else:
            relative = key
        raw_segments = [segment for segment in relative.split('/') if segment]
    else:
        normalized = os.path.normpath(path)
        relative = os.path.relpath(normalized, os.path.normpath(root_value))
        raw_segments = [
            segment for segment in relative.split(os.sep) if segment]

    # The final segment is the file name, not a partition layer.
    raw_segments = raw_segments[:-1]

    partitions: Dict[str, str] = {}
    for raw_segment in raw_segments:
        decoded_segment = unquote(raw_segment)
        match = SEGMENT_PATTERN.match(decoded_segment)
        if not match:
            continue
        column, value = match.group(1), match.group(2)
        if column not in declaration.columns:
            continue
        if column in partitions and partitions[column] != value:
            raise PartitionConflictError(
                f"Path {path} repeats partition '{column}' with conflicting "
                f"values '{partitions[column]}' and '{value}' at segment "
                f"'{raw_segment}'.",
                path=path, column=column,
                expected=partitions[column], actual=value)
        partitions[column] = value
    return partitions


def is_unfinished(pf: ParquetFile) -> bool:
    if isinstance(pf, S3ParquetFile):
        segments = pf.key.split('/')
    else:
        segments = pf.path.split(os.sep)
    return UNFINISHED_MARKER in segments


# ---------------------------------------------------------------------------
# Manifest expansion
# ---------------------------------------------------------------------------
def expand_manifest(declaration: PartitionDeclaration,
                    aws_session: Optional[Any] = None,
                    endpoint_url: Optional[str] = None) -> List[ParquetFile]:
    """List every parquet file under the business root, recursively."""
    bucket, prefix = _split_s3(declaration.business_root)
    if bucket is None:
        files: List[ParquetFile] = []
        for current_dir, _, names in os.walk(prefix):
            for name in sorted(names):
                if name.endswith('.parquet'):
                    files.append(LocalParquetFile(
                        path=os.path.join(current_dir, name)))
        return sorted(files, key=lambda item: item.path)

    if aws_session is None:
        aws_session = get_aws_session(None)
    client = aws_session.client('s3', endpoint_url=endpoint_url)
    files = []
    continuation: Optional[str] = None
    while True:
        kwargs = {'Bucket': bucket, 'Prefix': prefix}
        if continuation:
            kwargs['ContinuationToken'] = continuation
        response = client.list_objects_v2(**kwargs)
        for entry in response.get('Contents', []):
            key = entry['Key']
            if key.endswith('.parquet'):
                files.append(S3ParquetFile(
                    aws_session=aws_session, bucket=bucket, key=key,
                    endpoint_url=endpoint_url))
        if not response.get('IsTruncated'):
            break
        continuation = response.get('NextContinuationToken')
    return sorted(files, key=lambda item: item.key)


def resolve_manifest(objs: Sequence[ParquetFile]) -> List[ParquetFile]:
    resolved: List[ParquetFile] = []
    for obj in objs:
        resolved.extend(_resolve_wildcard(obj))
    return resolved


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------
def parse_filter(expression: str) -> Filter:
    match = FILTER_PATTERN.match(expression)
    if not match:
        raise PartitionError(
            f"Cannot parse filter '{expression}'. Expected forms like "
            "'date==2026-01-01' or 'measurement>10'.")
    return Filter(column=match.group(1), op=match.group(2),
                  value=match.group(3))


_OPS: Dict[str, Callable[[Any, Any], Any]] = {
    '==': lambda left, right: left == right,
    '!=': lambda left, right: left != right,
    '<': lambda left, right: left < right,
    '<=': lambda left, right: left <= right,
    '>': lambda left, right: left > right,
    '>=': lambda left, right: left >= right,
}


def _comparison_series(series: pd.Series, flt: Filter) -> pd.Series:
    operator = _OPS[flt.op]
    try:
        numeric_value = float(flt.value)
    except ValueError:
        return operator(series.astype('string'), flt.value)
    return operator(pd.to_numeric(series, errors='coerce'), numeric_value)


def apply_filters(frame: pd.DataFrame, filters: Sequence[Filter]) -> pd.DataFrame:
    if not filters:
        return frame
    mask = pd.Series(True, index=frame.index)
    for flt in filters:
        if flt.column not in frame.columns:
            raise PartitionError(
                f"Filter column '{flt.column}' is not available in the data.")
        mask = mask & _comparison_series(frame[flt.column], flt).fillna(False)
    return frame[mask].copy()


def _constant_matches(value: Any, flt: Filter) -> bool:
    if pd.isna(value):
        return False
    try:
        return bool(_OPS[flt.op](float(value), float(flt.value)))
    except ValueError:
        return bool(_OPS[flt.op](str(value), flt.value))


def _group_filtered_by_metadata(parquet_file: pq.ParquetFile, row_group: int,
                                filters: Sequence[Filter],
                                partitions: Dict[str, str]) -> bool:
    """Annotate groups the report drops, using trusted sources only.

    Physical columns are judged from row-group statistics; partition columns
    that exist only in the path are judged from their single declared value.
    Verification never skips a group because of this result; it only marks
    groups that ordinary report filtering excludes.
    """
    if not filters:
        return False
    metadata = parquet_file.metadata.row_group(row_group)
    for flt in filters:
        column_index = parquet_file.schema_arrow.get_field_index(flt.column)
        if column_index < 0:
            if flt.column in partitions:
                if not _constant_matches(partitions[flt.column], flt):
                    return True
            # An unprovable filter never hides a contradiction.
            continue
        statistics = metadata.column(column_index).statistics
        if statistics is None or not statistics.has_min_max:
            continue
        try:
            minimum, maximum = statistics.min, statistics.max
            if flt.op == '==':
                if not (minimum <= flt.value <= maximum):
                    return True
            elif flt.op == '!=':
                continue
            elif flt.op == '<' and minimum >= flt.value:
                return True
            elif flt.op == '<=' and minimum > flt.value:
                return True
            elif flt.op == '>' and maximum <= flt.value:
                return True
            elif flt.op == '>=' and maximum < flt.value:
                return True
        except TypeError:
            continue
    return False


def _row_filter_mask(frame: pd.DataFrame, filters: Sequence[Filter],
                     constants: Dict[str, Any]) -> pd.Series:
    mask = pd.Series(True, index=frame.index)
    for flt in filters:
        if flt.column in frame.columns:
            mask = mask & _comparison_series(
                frame[flt.column], flt).fillna(False)
        elif flt.column in constants:
            if not _constant_matches(constants[flt.column], flt):
                mask = mask & False
        else:
            # The filter cannot be evaluated for this group, so do not hide
            # any contradiction behind an assumed filter result.
            continue
    return mask


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
def _region_statistics(parquet_file: pq.ParquetFile, row_group: int,
                       region_column: str) -> Optional[pq.Statistics]:
    column_index = parquet_file.schema_arrow.get_field_index(region_column)
    if column_index < 0:
        return None
    return parquet_file.metadata.row_group(row_group).column(
        column_index).statistics


def _read_group_frame(parquet_file: pq.ParquetFile, row_group: int,
                      columns: Sequence[str]) -> pd.DataFrame:
    available = [name for name in dict.fromkeys(columns)
                 if name in parquet_file.schema_arrow.names]
    return parquet_file.read_row_group(
        row_group, columns=available).to_pandas()


def verify_file(pf: ParquetFile, declaration: PartitionDeclaration, *,
                region_column: str = 'region',
                filters: Optional[Sequence[Filter]] = None) -> FileReceipt:
    """Reconcile one file and return its quality receipt."""
    filters = list(filters or [])
    path = display_path(pf)
    s3_file = is_s3_file(pf)
    raw_path = pf.key if s3_file else pf.path
    partitions = parse_partitions(raw_path, declaration, is_s3=s3_file)
    unfinished = is_unfinished(pf)
    receipt = FileReceipt(
        path=path, region_column=region_column, partitions=partitions,
        status='passed', unfinished=unfinished)

    with pf.get_local_path() as local_path:
        parquet_file = pq.ParquetFile(local_path)
        column_names = parquet_file.schema_arrow.names
        row_offset = 0

        if region_column not in column_names:
            # A fully absent physical column is the only case the path value
            # fills the region.  Physical nulls are handled separately below.
            if region_column not in partitions:
                raise PartitionDeclarationError(
                    f'{path} has no physical {region_column!r} column and the '
                    f'path does not declare one.')
            receipt.region_filled = True
            for index in range(parquet_file.metadata.num_row_groups):
                num_rows = parquet_file.metadata.row_group(index).num_rows
                filtered = _group_filtered_by_metadata(
                    parquet_file, index, filters, partitions)
                receipt.groups.append(GroupDiagnostic(
                    index=index, row_offset=row_offset, num_rows=num_rows,
                    status='filled_from_path',
                    detail=f"{region_column} filled with "
                           f"'{partitions[region_column]}'",
                    filtered_for_report=filtered))
                row_offset += num_rows
            return _finalize(receipt)

        if region_column not in partitions:
            raise PartitionDeclarationError(
                f'{path} carries a physical {region_column!r} column but the '
                f'path does not declare a {region_column!r} partition.')
        expected = partitions[region_column]

        for index in range(parquet_file.metadata.num_row_groups):
            num_rows = parquet_file.metadata.row_group(index).num_rows
            group_filtered = _group_filtered_by_metadata(
                parquet_file, index, filters, partitions)
            statistics = _region_statistics(
                parquet_file, index, region_column)

            if (statistics is not None and statistics.has_min_max
                    and statistics.has_null_count
                    and statistics.null_count == 0
                    and statistics.min == statistics.max):
                # Trusted metadata proves a homogeneous, non-null group, so the
                # region column and unrelated columns need not be decoded.
                if statistics.min == expected:
                    receipt.groups.append(GroupDiagnostic(
                        index=index, row_offset=row_offset, num_rows=num_rows,
                        status='verified',
                        detail='homogeneous region proven by statistics',
                        filtered_for_report=group_filtered))
                else:
                    receipt.conflicts.append(ConflictRecord(
                        path=path, row_group=index, row_index=row_offset,
                        column=region_column, expected=expected,
                        actual=statistics.min,
                        filtered_for_report=group_filtered))
                    receipt.groups.append(GroupDiagnostic(
                        index=index, row_offset=row_offset, num_rows=num_rows,
                        status='conflict',
                        detail=f'all {num_rows} rows are {statistics.min!r}, '
                               f'path expects {expected!r}',
                        filtered_for_report=group_filtered))
                row_offset += num_rows
                continue

            # Statistics are missing, incomplete, mixed or null-bearing, so the
            # region column of this group is decoded and checked row by row.
            needed_columns = [region_column] + [
                flt.column for flt in filters]
            group_frame = _read_group_frame(
                parquet_file, index, needed_columns)
            constants = {region_column: expected}
            constants.update(partitions)
            row_mask = _row_filter_mask(group_frame, filters, constants)
            regions = group_frame[region_column]

            group_conflicts = 0
            group_nulls = 0
            for position, actual in enumerate(regions):
                physical_row = row_offset + position
                row_filtered = not bool(row_mask.iloc[position])
                if pd.isna(actual):
                    receipt.null_rows.append(physical_row)
                    group_nulls += 1
                    continue
                if actual != expected:
                    receipt.conflicts.append(ConflictRecord(
                        path=path, row_group=index, row_index=physical_row,
                        column=region_column, expected=expected, actual=actual,
                        filtered_for_report=row_filtered))
                    group_conflicts += 1
            if group_nulls and not group_conflicts:
                status = 'null'
                detail = f'{group_nulls} physical null value(s)'
            elif group_nulls or group_conflicts:
                status = 'conflict'
                detail = (f'{group_conflicts} conflict(s), '
                          f'{group_nulls} null value(s)')
            else:
                status = 'verified'
                detail = 'decoded group matches the path region'
            receipt.groups.append(GroupDiagnostic(
                index=index, row_offset=row_offset, num_rows=num_rows,
                status=status, detail=detail,
                filtered_for_report=group_filtered))
            row_offset += num_rows

    return _finalize(receipt)


def _finalize(receipt: FileReceipt) -> FileReceipt:
    if receipt.conflicts or receipt.null_rows:
        receipt.status = 'failed'
    elif receipt.unfinished:
        receipt.status = 'withheld'
    else:
        receipt.status = 'passed'
    return receipt


def build_receipts(objs: Sequence[ParquetFile],
                   declaration: PartitionDeclaration, *,
                   region_column: str = 'region',
                   filters: Optional[Sequence[Filter]] = None
                   ) -> List[FileReceipt]:
    """Verify every file in a manifest, without early stopping.

    Sampling enough rows for display never truncates this check, and report
    filters only annotate rows and groups; they never hide a contradiction.
    """
    return [
        verify_file(pf, declaration, region_column=region_column,
                    filters=filters)
        for pf in resolve_manifest(objs)
    ]


# ---------------------------------------------------------------------------
# Report projection
# ---------------------------------------------------------------------------
@dataclass
class ReportResult:
    frame: pd.DataFrame
    receipts: List[FileReceipt]


def read_report(objs: Sequence[ParquetFile],
                declaration: PartitionDeclaration, *,
                columns: Optional[Sequence[str]] = None,
                filters: Optional[Sequence[Filter]] = None,
                head: Optional[int] = None,
                region_column: str = 'region',
                measure_column: str = 'measurement') -> ReportResult:
    """Read measurement rows for reporting.

    Only files whose receipt passes contribute formal rows.  Unfinished files
    are withheld, and failing files contribute nothing even when their earlier
    groups already matched.  Filters behave as ordinary row filtering.
    """
    filters = list(filters or [])
    manifest = resolve_manifest(objs)
    receipts = build_receipts(manifest, declaration,
                              region_column=region_column, filters=filters)
    projection = tuple(columns) if columns else (measure_column,)

    frames: List[pd.DataFrame] = []
    accumulated = 0
    for pf, receipt in zip(manifest, receipts):
        if receipt.status != 'passed':
            continue
        with pf.get_local_path() as local_path:
            schema_names = pq.ParquetFile(local_path).schema_arrow.names
            wanted = set(projection) | {flt.column for flt in filters}
            read_columns = [name for name in dict.fromkeys(wanted)
                            if name in schema_names]
            frame = pq.read_table(
                local_path, columns=read_columns).to_pandas()
            for column, value in receipt.partitions.items():
                if column not in frame.columns:
                    frame[column] = value
            frame = apply_filters(frame, filters)
            frame = frame[[name for name in projection
                           if name in frame.columns]]
            frames.append(frame)
            accumulated += len(frame)
            if head is not None and head > 0 and accumulated >= head:
                break

    if frames:
        combined = pd.concat(frames, ignore_index=True)
        if head is not None and head > 0:
            combined = combined.head(head)
    else:
        combined = pd.DataFrame(columns=list(projection))
    return ReportResult(frame=combined, receipts=receipts)


# ---------------------------------------------------------------------------
# Receipt rendering
# ---------------------------------------------------------------------------
def render_receipts(receipts: Sequence[FileReceipt]) -> str:
    lines: List[str] = []
    for receipt in receipts:
        lines.append(f'FILE {receipt.path}')
        partition_text = ', '.join(f'{key}={value}'
                                   for key, value in receipt.partitions.items())
        lines.append(f'  partitions: {partition_text or "(none)"}')
        lines.append(f'  status: {receipt.status}')
        if receipt.region_filled:
            lines.append(
                '  region: filled from path (physical column absent)')
        if receipt.unfinished:
            lines.append(
                '  note: unfinished batch, display rows withheld from reports')
        for group in receipt.groups:
            marker = (' [filtered for report]'
                      if group.filtered_for_report else '')
            lines.append(
                f'  row_group {group.index} rows {group.row_offset}..'
                f'{group.row_offset + group.num_rows - 1}: '
                f'{group.status} - {group.detail}{marker}')
        for conflict in receipt.conflicts:
            marker = (' [filtered for report]'
                      if conflict.filtered_for_report else '')
            lines.append(
                f'  CONFLICT row_group={conflict.row_group} '
                f'row={conflict.row_index} column={conflict.column} '
                f'expected={conflict.expected!r} '
                f'actual={conflict.actual!r}{marker}')
        if receipt.null_rows:
            rows = ', '.join(str(row) for row in receipt.null_rows)
            lines.append(
                f'  PHYSICAL NULL column={receipt.region_column} '
                f'rows={rows}')
        lines.append('')
    return '\n'.join(lines).rstrip() + '\n'
