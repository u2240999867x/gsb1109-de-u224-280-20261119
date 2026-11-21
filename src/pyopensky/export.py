"""Resumable, snapshot-scoped segmented export of large Trino result sets.

The exporter streams a single ordered query into independent, readable
Parquet segments using ``fetchmany``. A JSON manifest persisted next to the
segment files records the upstream snapshot identifier, the exact query and
parameters, the unique ordering keys and a checksum of every confirmed
segment.

Recovery is only possible against the *same* snapshot and the *same* query:
a changed snapshot identifier, a failing snapshot probe or a tampered
confirmed segment all invalidate every segment from the damaged point
onwards, so that no reader can ever observe mixed-snapshot data. Segment
data is first written to a temporary file and only atomically renamed once
the full segment has been verified; interrupted segments therefore never
become visible.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy.sql.expression import text

import pandas as pd

_log = logging.getLogger(__name__)

MANIFEST_NAME = "manifest.json"
TEMP_SUFFIX = ".partial"
MANIFEST_VERSION = 1
_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_RESUME_PREFIX = "_resume_key_"


class ExportError(RuntimeError):
    """Base class for failures of the segmented export."""


class SnapshotMismatchError(ExportError):
    """Raised when recovery would mix data from another upstream snapshot."""


class ResumeRejectedError(ExportError):
    """Raised when an export cannot be started or safely resumed."""


class OrderKeyNotUniqueError(ExportError):
    """Raised when the ordering key carries duplicate values."""


@dataclass(frozen=True)
class SegmentManifest:
    """Checksummed description of one independently readable segment."""

    index: int
    path: str
    row_count: int
    key_first: list[Any]
    key_last: list[Any]
    checksum: str
    byte_size: int
    rows_hash: str


@dataclass(frozen=True)
class ExportManifest:
    """Persistent state of a segmented export, safe to use for recovery."""

    version: int
    export_id: str
    snapshot_id: str
    query_sql: str
    params: dict[str, Any]
    order_by: list[str]
    snapshot_check_sql: str | None
    snapshot_check_value: Any
    columns: list[str]
    batch_size: int
    segment_rows: int
    segments: tuple[SegmentManifest, ...]
    complete: bool
    temp_suffix: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "export_id": self.export_id,
            "snapshot_id": self.snapshot_id,
            "query_sql": self.query_sql,
            "params": self.params,
            "order_by": self.order_by,
            "snapshot_check_sql": self.snapshot_check_sql,
            "snapshot_check_value": _to_jsonable(self.snapshot_check_value),
            "columns": self.columns,
            "batch_size": self.batch_size,
            "segment_rows": self.segment_rows,
            "segments": [
                {
                    "index": segment.index,
                    "path": segment.path,
                    "row_count": segment.row_count,
                    "key_first": [
                        _to_jsonable(value) for value in segment.key_first
                    ],
                    "key_last": [
                        _to_jsonable(value) for value in segment.key_last
                    ],
                    "checksum": segment.checksum,
                    "byte_size": segment.byte_size,
                    "rows_hash": segment.rows_hash,
                }
                for segment in self.segments
            ],
            "complete": self.complete,
            "temp_suffix": self.temp_suffix,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> ExportManifest:
        segments = tuple(
            SegmentManifest(
                index=segment["index"],
                path=segment["path"],
                row_count=segment["row_count"],
                key_first=list(segment["key_first"]),
                key_last=list(segment["key_last"]),
                checksum=segment["checksum"],
                byte_size=segment["byte_size"],
                rows_hash=segment["rows_hash"],
            )
            for segment in data["segments"]
        )
        return cls(
            version=data["version"],
            export_id=data["export_id"],
            snapshot_id=data["snapshot_id"],
            query_sql=data["query_sql"],
            params=dict(data["params"]),
            order_by=list(data["order_by"]),
            snapshot_check_sql=data.get("snapshot_check_sql"),
            snapshot_check_value=data.get("snapshot_check_value"),
            columns=list(data.get("columns", [])),
            batch_size=data["batch_size"],
            segment_rows=data["segment_rows"],
            segments=segments,
            complete=data["complete"],
            temp_suffix=data.get("temp_suffix", TEMP_SUFFIX),
        )


class Connectable(Protocol):
    def connect(self) -> Any: ...


def _normalize_sql(query: str | Any) -> str:
    sql = str(query).strip().rstrip(";").strip()
    if not sql:
        raise ResumeRejectedError("The export query must not be empty")
    if ";" in sql:
        raise ResumeRejectedError("Only a single SQL statement may be exported")
    return sql


def _to_jsonable(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    item = getattr(value, "item", None)
    if callable(item):
        try:
            return item()
        except ValueError:
            pass
    return str(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _rows_hash(frame: pd.DataFrame) -> str:
    csv = frame.to_csv(index=False, lineterminator="\n")
    return hashlib.sha256(csv.encode("utf-8")).hexdigest()


def _check_key_order(
    frame: pd.DataFrame,
    order_by: list[str],
    previous_last: tuple[Any, ...] | None,
) -> tuple[tuple[Any, ...], tuple[Any, ...]]:
    key_frame = frame[order_by]
    if bool(key_frame.duplicated().any()):
        raise OrderKeyNotUniqueError(
            f"Ordering key {order_by} is not unique within the exported rows"
        )
    sorted_keys = key_frame.sort_values(order_by).reset_index(drop=True)
    if not key_frame.reset_index(drop=True).equals(sorted_keys):
        raise ResumeRejectedError(
            f"Rows are not strictly ordered by {order_by}"
        )
    key_first = tuple(key_frame.iloc[0])
    key_last = tuple(key_frame.iloc[-1])
    if previous_last is not None and key_first <= previous_last:
        raise OrderKeyNotUniqueError(
            f"Ordering key {order_by} does not continue after {previous_last}"
        )
    return key_first, key_last


class SegmentedExporter:
    """Stream a query into verified, independently readable Parquet segments.

    :param client: any object exposing a SQLAlchemy-like ``connect()`` method
        (for instance a :class:`pyopensky.trino.Trino` instance).
    :param output_dir: directory hosting the manifest and segment files. It
        may hold an interrupted export, in which case ``export`` resumes it.
    :param snapshot_id: caller-provided, upstream-verifiable identifier of the
        immutable snapshot to read. Mandatory: resumption without a snapshot
        basis is refused.
    :param query: single SQL statement producing the rows to export. The same
        statement is reused for the initial export and every recovery.
    :param order_by: one or several column names forming a unique, stable row
        ordering key. Duplicated names or non-identifier names are refused.
    :param params: bind parameters of ``query``.
    :param snapshot_check: optional ``(sql, expected_value)`` pair executed on
        every connection. Recovery aborts if the probe no longer returns
        ``expected_value``.
    :param batch_size: maximum number of rows fetched per ``fetchmany`` call.
    :param segment_rows: number of rows committed to each segment file.
    :param export_id: optional stable export identifier; generated otherwise.
    :param compression: compression passed to the Parquet writer.
    """

    def __init__(
        self,
        client: Connectable,
        output_dir: str | Path,
        *,
        snapshot_id: str | None,
        query: str | Any,
        order_by: list[str] | tuple[str, ...],
        params: dict[str, Any] | None = None,
        snapshot_check: tuple[str, Any] | None = None,
        batch_size: int = 10_000,
        segment_rows: int = 100_000,
        export_id: str | None = None,
        compression: str | None = None,
    ) -> None:
        if snapshot_id is None or not str(snapshot_id).strip():
            raise ResumeRejectedError(
                "A verified snapshot identifier is required for export "
                "and recovery"
            )
        if isinstance(order_by, str) or not order_by:
            raise ResumeRejectedError(
                "order_by must be a non-empty list of column names"
            )
        columns = list(order_by)
        if len(set(columns)) != len(columns):
            raise ResumeRejectedError(f"Duplicate ordering columns: {columns}")
        if any(not _IDENTIFIER_RE.match(column) for column in columns):
            raise ResumeRejectedError(
                f"Ordering columns must be simple identifiers: {columns}"
            )
        if batch_size <= 0 or segment_rows <= 0:
            raise ResumeRejectedError("batch_size and segment_rows must be > 0")
        if params is not None and any(
            str(name).startswith(_RESUME_PREFIX) for name in params
        ):
            raise ResumeRejectedError(
                f"Bind parameters may not use the reserved {_RESUME_PREFIX} "
                "prefix"
            )

        self.client = client
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.manifest_path = self.output_dir / MANIFEST_NAME
        self.snapshot_id = str(snapshot_id)
        self.query_sql = _normalize_sql(query)
        self.params = dict(params or {})
        self.order_by = columns
        self.snapshot_check_sql = (
            _normalize_sql(snapshot_check[0])
            if snapshot_check is not None
            else None
        )
        self.snapshot_check_value = (
            snapshot_check[1] if snapshot_check is not None else None
        )
        self.batch_size = batch_size
        self.segment_rows = segment_rows
        self.export_id = export_id or uuid.uuid4().hex
        self.compression = compression

    def export(self, *, resume: bool = True) -> ExportManifest:
        """Run the export to completion, resuming an interrupted run if any."""

        manifest: ExportManifest | None = None
        if self.manifest_path.exists():
            if not resume:
                raise ExportError(
                    f"An export manifest already exists in {self.output_dir}"
                )
            manifest = self._load_manifest()
            self._validate_manifest(manifest)
        elif not resume:
            raise ExportError(
                f"No export manifest found in {self.output_dir} to resume"
            )

        if manifest is not None and manifest.complete:
            _log.info("Export %s is already complete", self.export_id)
            return manifest

        barrier, kept = self._verify_confirmed(manifest)
        self._discard_from(manifest, barrier)
        starting_key = tuple(kept[-1].key_last) if kept else None
        produced, columns = self._stream(barrier, starting_key, kept)

        final_manifest = ExportManifest(
            version=MANIFEST_VERSION,
            export_id=self.export_id,
            snapshot_id=self.snapshot_id,
            query_sql=self.query_sql,
            params=self.params,
            order_by=self.order_by,
            snapshot_check_sql=self.snapshot_check_sql,
            snapshot_check_value=self.snapshot_check_value,
            columns=columns,
            batch_size=self.batch_size,
            segment_rows=self.segment_rows,
            segments=tuple(kept) + tuple(produced),
            complete=True,
            temp_suffix=TEMP_SUFFIX,
        )
        self._write_manifest(final_manifest)
        return final_manifest

    def _load_manifest(self) -> ExportManifest:
        with self.manifest_path.open("r", encoding="utf-8") as stream:
            return ExportManifest.from_dict(json.load(stream))

    def _validate_manifest(self, manifest: ExportManifest) -> None:
        if manifest.snapshot_id != self.snapshot_id:
            raise SnapshotMismatchError(
                f"Manifest snapshot {manifest.snapshot_id!r} does not match "
                f"requested snapshot {self.snapshot_id!r}; refusing to append "
                "data from another snapshot"
            )
        if manifest.query_sql != self.query_sql:
            raise ResumeRejectedError("Query text differs from the manifest")
        if manifest.params != self.params:
            raise ResumeRejectedError(
                "Query parameters differ from the manifest"
            )
        if manifest.order_by != self.order_by:
            raise ResumeRejectedError("Ordering key differs from the manifest")
        if manifest.batch_size != self.batch_size:
            raise ResumeRejectedError("batch_size differs from the manifest")
        if manifest.segment_rows != self.segment_rows:
            raise ResumeRejectedError(
                "segment_rows differs from the manifest"
            )
        self.export_id = manifest.export_id

    def _verify_confirmed(
        self, manifest: ExportManifest | None
    ) -> tuple[int, list[SegmentManifest]]:
        if manifest is None:
            return 0, []
        kept: list[SegmentManifest] = []
        previous_last: tuple[Any, ...] | None = None
        for segment in manifest.segments:
            path = self.output_dir / segment.path
            try:
                verified = self._verify_segment_file(path, previous_last)
            except (OSError, pa.ArrowException, KeyError) as exc:
                _log.warning(
                    "Segment %s failed verification, recovery restarts "
                    "from this point: %s",
                    segment.index,
                    exc,
                )
                return segment.index, kept
            row_count, key_first, key_last, checksum, byte_size, rows_hash = (
                verified
            )
            if (
                row_count != segment.row_count
                or checksum != segment.checksum
                or rows_hash != segment.rows_hash
                or byte_size != segment.byte_size
                or list(key_first) != segment.key_first
                or list(key_last) != segment.key_last
            ):
                _log.warning(
                    "Segment %s content does not match its manifest, "
                    "recovery restarts from this point",
                    segment.index,
                )
                return segment.index, kept
            kept.append(
                SegmentManifest(
                    index=len(kept),
                    path=segment.path,
                    row_count=row_count,
                    key_first=list(key_first),
                    key_last=list(key_last),
                    checksum=checksum,
                    byte_size=byte_size,
                    rows_hash=rows_hash,
                )
            )
            previous_last = key_last
        return len(kept), kept

    def _verify_segment_file(
        self, path: Path, previous_last: tuple[Any, ...] | None
    ) -> tuple[
        int,
        tuple[Any, ...],
        tuple[Any, ...],
        str,
        int,
        str,
    ]:
        frame = pq.read_table(path).to_pandas()
        missing = [column for column in self.order_by if column not in frame]
        if missing:
            raise KeyError(f"Missing ordering columns {missing} in {path}")
        key_first, key_last = _check_key_order(
            frame, self.order_by, previous_last
        )
        checksum = _sha256_file(path)
        return (
            len(frame),
            key_first,
            key_last,
            checksum,
            path.stat().st_size,
            _rows_hash(frame),
        )

    def _discard_from(
        self, manifest: ExportManifest | None, barrier: int
    ) -> None:
        retained = {
            segment.path
            for segment in (manifest.segments if manifest is not None else [])
            if segment.index < barrier
        }
        for final_path in self.output_dir.glob("segment_*.parquet"):
            if final_path.name not in retained:
                final_path.unlink(missing_ok=True)
        for temporary in self.output_dir.glob(f"*{TEMP_SUFFIX}"):
            temporary.unlink(missing_ok=True)

    def _stream(
        self,
        start_index: int,
        starting_key: tuple[Any, ...] | None,
        kept: list[SegmentManifest],
    ) -> tuple[list[SegmentManifest], list[str]]:
        keys_csv = ", ".join(self.order_by)
        execute_params = dict(self.params)
        where_clause = ""
        if starting_key is not None:
            binds = ", ".join(
                f":{_RESUME_PREFIX}{index}"
                for index in range(len(starting_key))
            )
            where_clause = f" WHERE ({keys_csv}) > ({binds})"
            execute_params.update(
                {
                    f"{_RESUME_PREFIX}{index}": value
                    for index, value in enumerate(starting_key)
                }
            )
        sql = (
            f"SELECT * FROM ({self.query_sql}) AS pyopensky_export_base"
            f"{where_clause} ORDER BY {keys_csv}"
        )
        column_probe = (
            f"SELECT * FROM ({self.query_sql}) AS pyopensky_export_cols "
            "LIMIT 0"
        )

        connection = self.client.connect()
        produced: list[SegmentManifest] = []
        writer: pq.ParquetWriter | None = None
        try:
            if self.snapshot_check_sql is not None:
                probed = connection.execute(
                    text(self.snapshot_check_sql)
                ).scalar()
                if probed != self.snapshot_check_value:
                    raise SnapshotMismatchError(
                        f"Snapshot probe returned {probed!r}, expected "
                        f"{self.snapshot_check_value!r}"
                    )
            probed_columns = list(
                connection.execute(text(column_probe), self.params).keys()
            )
            missing = [
                column
                for column in self.order_by
                if column not in probed_columns
            ]
            if missing:
                raise ResumeRejectedError(
                    f"Ordering columns absent from result: {missing}"
                )
            result = connection.execute(text(sql), execute_params)
            columns = list(result.keys())
            if columns != probed_columns:
                raise ResumeRejectedError(
                    "Result columns differ between the column probe and the "
                    f"streaming query: {probed_columns} != {columns}"
                )

            buffer = pd.DataFrame(columns=columns)
            segment_index = start_index
            temporary_path = self._temp_path(segment_index)
            current_rows = 0
            part_last: tuple[Any, ...] | None = starting_key
            segment_start_key = starting_key

            while True:
                if buffer.empty:
                    raw_rows = result.fetchmany(self.batch_size)
                    if not raw_rows:
                        break
                    buffer = pd.DataFrame.from_records(
                        raw_rows, columns=columns
                    )

                remaining = self.segment_rows - current_rows
                part = buffer.iloc[:remaining].copy()
                buffer = buffer.iloc[remaining:].copy()

                key_first, key_last = _check_key_order(
                    part, self.order_by, part_last
                )
                table = pa.Table.from_pandas(part, preserve_index=False)
                if writer is None:
                    writer = pq.ParquetWriter(
                        temporary_path,
                        table.schema,
                        compression=self.compression,
                    )
                else:
                    table = table.cast(writer.schema)
                writer.write_table(table)
                current_rows += len(part)
                part_last = key_last

                if current_rows == self.segment_rows:
                    writer.close()
                    writer = None
                    segment = self._commit_segment(
                        temporary_path, segment_index, segment_start_key
                    )
                    produced.append(segment)
                    self._persist_progress(columns, kept, produced)
                    segment_index += 1
                    current_rows = 0
                    segment_start_key = tuple(segment.key_last)
                    part_last = segment_start_key
                    temporary_path = self._temp_path(segment_index)

            if writer is not None:
                writer.close()
                writer = None
                segment = self._commit_segment(
                    temporary_path, segment_index, segment_start_key
                )
                produced.append(segment)
                self._persist_progress(columns, kept, produced)
        finally:
            if writer is not None:
                writer.close()
            close = getattr(connection, "close", None)
            if callable(close):
                close()
        return produced, columns

    def _commit_segment(
        self,
        temporary_path: Path,
        segment_index: int,
        segment_start_key: tuple[Any, ...] | None,
    ) -> SegmentManifest:
        row_count, key_first, key_last, checksum, byte_size, rows_hash = (
            self._verify_segment_file(temporary_path, segment_start_key)
        )
        final_path = self._segment_path(segment_index)
        os.replace(temporary_path, final_path)
        return SegmentManifest(
            index=segment_index,
            path=final_path.name,
            row_count=row_count,
            key_first=list(key_first),
            key_last=list(key_last),
            checksum=checksum,
            byte_size=byte_size,
            rows_hash=rows_hash,
        )

    def _persist_progress(
        self,
        columns: list[str],
        kept: list[SegmentManifest],
        produced: list[SegmentManifest],
    ) -> None:
        manifest = ExportManifest(
            version=MANIFEST_VERSION,
            export_id=self.export_id,
            snapshot_id=self.snapshot_id,
            query_sql=self.query_sql,
            params=self.params,
            order_by=self.order_by,
            snapshot_check_sql=self.snapshot_check_sql,
            snapshot_check_value=self.snapshot_check_value,
            columns=columns,
            batch_size=self.batch_size,
            segment_rows=self.segment_rows,
            segments=tuple(kept) + tuple(produced),
            complete=False,
            temp_suffix=TEMP_SUFFIX,
        )
        self._write_manifest(manifest)

    def _write_manifest(self, manifest: ExportManifest) -> None:
        temporary = self.manifest_path.with_name(
            f"{MANIFEST_NAME}{TEMP_SUFFIX}"
        )
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(manifest.to_dict(), stream, indent=2, default=str)
        os.replace(temporary, self.manifest_path)

    def _segment_path(self, index: int) -> Path:
        return self.output_dir / f"segment_{index:06d}.parquet"

    def _temp_path(self, index: int) -> Path:
        return self.output_dir / f"segment_{index:06d}.parquet{TEMP_SUFFIX}"

    def read_segment(
        self, manifest: ExportManifest, index: int
    ) -> pd.DataFrame:
        """Read one confirmed segment as an independent DataFrame."""

        for segment in manifest.segments:
            if segment.index == index:
                return pq.read_table(
                    self.output_dir / segment.path
                ).to_pandas()
        raise KeyError(f"No segment {index} in manifest")

    def read_all(self, manifest: ExportManifest) -> pd.DataFrame:
        """Concatenate every segment, preserving the exported row order."""

        if not manifest.complete:
            raise ExportError("Cannot read an incomplete export")
        if not manifest.segments:
            return pd.DataFrame(columns=manifest.columns)
        frames = [
            pq.read_table(self.output_dir / segment.path).to_pandas()
            for segment in sorted(manifest.segments, key=lambda s: s.index)
        ]
        return pd.concat(frames, ignore_index=True)
