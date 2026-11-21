"""Regression tests for resumable, snapshot-scoped segmented exports.

The Trino service is replaced by an in-memory SQLite client whose
``fetchmany`` calls are fully controllable. The same fake backs an
uninterrupted reference export and the interrupted/recovered runs, so the
final completion state, segment contents and row order can be compared with
the reference. Parquet byte layout is not part of the cross-run contract
because it depends on fetch boundaries; per-file checksums still guard each
segment against tampering. No network access is required.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
import pyarrow as pa
import pyarrow.parquet as pq
from sqlalchemy import create_engine, text

import pandas as pd
from pyopensky.export import (
    MANIFEST_NAME,
    TEMP_SUFFIX,
    OrderKeyNotUniqueError,
    ResumeRejectedError,
    SegmentedExporter,
    SnapshotMismatchError,
    _sha256_file,
)

SNAPSHOT_A = "snapshot-2026-11-22-A"
SNAPSHOT_B = "snapshot-2026-11-22-B"
PROBE_SQL = "SELECT v FROM snapshot_meta WHERE k = 'snapshot'"
BASE_SQL = "SELECT id, sku, qty FROM orders WHERE id >= :min_id"
ORDER_BY = ["id"]
BATCH_SIZE = 4
SEGMENT_ROWS = 6
ROW_COUNT = 25


class DisconnectError(ConnectionError):
    """Raised inside fetchmany to simulate a truncated Trino connection."""


class FakeScalarResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def scalar(self) -> Any:
        return self.value


class FakeResult:
    def __init__(self, result: Any, client: FakeTrino) -> None:
        self.result = result
        self.client = client

    def keys(self) -> list[str]:
        return list(self.result.keys())

    def fetchmany(self, size: int) -> list[Any]:
        self.client.fetch_calls.append(size)
        if (
            self.client.fail_on_fetch is not None
            and len(self.client.fetch_calls) == self.client.fail_on_fetch
        ):
            raise DisconnectError("fetchmany interrupted before commit")
        return self.result.fetchmany(size)


class FakeConnection:
    def __init__(self, client: FakeTrino) -> None:
        self.client = client
        self.real = client.engine.connect()

    def execute(self, statement: Any, params: Any = None) -> Any:
        sql = str(statement)
        self.client.executed_sql.append(sql)
        self.client.executed_params.append(dict(params or {}))
        if sql == PROBE_SQL:
            return FakeScalarResult(self.client.snapshot_value)
        result = self.real.execute(statement, params or {})
        return FakeResult(result, self.client)

    def close(self) -> None:
        self.real.close()


class FakeTrino:
    """Controllable Trino stand-in backed by an in-memory SQLite database."""

    def __init__(
        self,
        engine: Any,
        snapshot_value: str,
        *,
        fail_on_fetch: int | None = None,
    ) -> None:
        self.engine = engine
        self.snapshot_value = snapshot_value
        self.fail_on_fetch = fail_on_fetch
        self.fetch_calls: list[int] = []
        self.executed_sql: list[str] = []
        self.executed_params: list[dict[str, Any]] = []
        self.connections = 0

    def connect(self) -> FakeConnection:
        self.connections += 1
        return FakeConnection(self)


def build_engine(snapshot_id: str, *, tamper_rows: bool = False) -> Any:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE snapshot_meta (k TEXT, v TEXT)"))
        connection.execute(
            text("INSERT INTO snapshot_meta (k, v) VALUES ('snapshot', :v)"),
            {"v": snapshot_id},
        )
        connection.execute(
            text("CREATE TABLE orders (id INTEGER, sku TEXT, qty INTEGER)")
        )
        rows = [
            {
                "id": identifier,
                "sku": f"SKU-{identifier:03d}"
                if not tamper_rows
                else f"ALTERED-{identifier:03d}",
                "qty": identifier * 7,
            }
            for identifier in range(1, ROW_COUNT + 1)
        ]
        connection.execute(
            text("INSERT INTO orders (id, sku, qty) VALUES (:id, :sku, :qty)"),
            rows,
        )
    return engine


def make_exporter(
    client: FakeTrino,
    output_dir: Path,
    *,
    snapshot_id: str = SNAPSHOT_A,
    order_by: list[str] | None = None,
    batch_size: int = BATCH_SIZE,
    segment_rows: int = SEGMENT_ROWS,
) -> SegmentedExporter:
    return SegmentedExporter(
        client,
        output_dir,
        snapshot_id=snapshot_id,
        query=BASE_SQL,
        order_by=ORDER_BY if order_by is None else order_by,
        params={"min_id": 1},
        snapshot_check=(PROBE_SQL, snapshot_id),
        batch_size=batch_size,
        segment_rows=segment_rows,
    )


def expected_frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "id": list(range(1, ROW_COUNT + 1)),
            "sku": [
                f"SKU-{identifier:03d}"
                for identifier in range(1, ROW_COUNT + 1)
            ],
            "qty": [identifier * 7 for identifier in range(1, ROW_COUNT + 1)],
        }
    )


@pytest.fixture
def engine_a() -> Any:
    engine = build_engine(SNAPSHOT_A)
    yield engine
    engine.dispose()


def assert_manifest_matches_reference(
    output_dir: Path, actual: Any, reference: Any
) -> None:
    """Segment contents and order match, and every file is self-consistent.

    Byte-identical Parquet files are not required across runs because the row
    group layout depends on fetch boundaries; the content-level ``rows_hash``
    is the stable cross-run contract, while ``checksum`` still guards each
    file against tampering within a run.
    """

    assert actual.complete is True
    assert len(actual.segments) == len(reference.segments)
    for actual_segment, reference_segment in zip(
        actual.segments, reference.segments
    ):
        assert actual_segment.index == reference_segment.index
        assert actual_segment.row_count == reference_segment.row_count
        assert actual_segment.rows_hash == reference_segment.rows_hash
        assert actual_segment.key_first == reference_segment.key_first
        assert actual_segment.key_last == reference_segment.key_last
        path = output_dir / actual_segment.path
        assert _sha256_file(path) == actual_segment.checksum
        assert path.stat().st_size == actual_segment.byte_size


@pytest.fixture
def baseline(tmp_path: Path, engine_a: Any) -> tuple[Path, Any]:
    output_dir = tmp_path / "baseline"
    client = FakeTrino(engine_a, SNAPSHOT_A)
    exporter = make_exporter(client, output_dir)
    manifest = exporter.export()
    return output_dir, manifest


def test_uninterrupted_export_matches_reference(
    baseline: tuple[Path, Any],
) -> None:
    output_dir, manifest = baseline
    exporter = SegmentedExporter(
        FakeTrino(build_engine(SNAPSHOT_A), SNAPSHOT_A),
        output_dir,
        snapshot_id=SNAPSHOT_A,
        query=BASE_SQL,
        order_by=ORDER_BY,
        params={"min_id": 1},
        snapshot_check=(PROBE_SQL, SNAPSHOT_A),
        batch_size=BATCH_SIZE,
        segment_rows=SEGMENT_ROWS,
    )
    assert manifest.complete is True
    assert [segment.row_count for segment in manifest.segments] == [
        SEGMENT_ROWS,
        SEGMENT_ROWS,
        SEGMENT_ROWS,
        SEGMENT_ROWS,
        1,
    ]
    frame = exporter.read_all(manifest)
    pd.testing.assert_frame_equal(
        frame.reset_index(drop=True), expected_frame()
    )
    assert not list(output_dir.glob(f"*{TEMP_SUFFIX}"))
    segment_files = sorted(
        path.name for path in output_dir.glob("segment_*.parquet")
    )
    assert segment_files == sorted(
        segment.path for segment in manifest.segments
    )


def test_segments_are_independently_readable_and_ordered(
    baseline: tuple[Path, Any],
) -> None:
    output_dir, manifest = baseline
    reference = expected_frame()
    offset = 0
    for segment in manifest.segments:
        frame = pq.read_table(output_dir / segment.path).to_pandas()
        assert len(frame) == segment.row_count
        assert frame["id"].is_unique
        assert frame["id"].is_monotonic_increasing
        expected = reference.iloc[offset : offset + len(frame)].reset_index(
            drop=True
        )
        pd.testing.assert_frame_equal(frame.reset_index(drop=True), expected)
        offset += len(frame)
    assert offset == ROW_COUNT


def test_export_writes_manifest_progress_and_cleans_temp_files(
    tmp_path: Path, engine_a: Any
) -> None:
    output_dir = tmp_path / "interrupted"
    client = FakeTrino(engine_a, SNAPSHOT_A, fail_on_fetch=5)
    exporter = make_exporter(client, output_dir)
    with pytest.raises(DisconnectError):
        exporter.export()

    visible = sorted(path.name for path in output_dir.glob("segment_*.parquet"))
    temporary = list(output_dir.glob(f"*{TEMP_SUFFIX}"))
    assert visible == ["segment_000000.parquet", "segment_000001.parquet"]
    assert len(temporary) == 1
    assert temporary[0].name == "segment_000002.parquet.partial"

    saved = json.loads((output_dir / MANIFEST_NAME).read_text())
    assert saved["complete"] is False
    assert saved["snapshot_id"] == SNAPSHOT_A
    assert [segment["index"] for segment in saved["segments"]] == [0, 1]
    assert all(
        not segment["path"].endswith(TEMP_SUFFIX)
        for segment in saved["segments"]
    )


def test_orphan_and_temp_artifacts_never_become_visible_segments(
    tmp_path: Path, engine_a: Any, baseline: tuple[Path, Any]
) -> None:
    output_dir = tmp_path / "orphans"
    with pytest.raises(DisconnectError):
        make_exporter(
            FakeTrino(engine_a, SNAPSHOT_A, fail_on_fetch=5), output_dir
        ).export()

    # A segment renamed to its final name but never recorded in the manifest,
    # plus an interrupted temp file for the same index.
    orphan = output_dir / "segment_000002.parquet"
    orphan.write_bytes(b"uncommitted bytes must never be readable")
    (output_dir / "segment_000003.parquet.partial").write_bytes(b"partial")

    recovered = make_exporter(
        FakeTrino(engine_a, SNAPSHOT_A), output_dir
    ).export()
    _, reference_manifest = baseline

    assert_manifest_matches_reference(
        output_dir, recovered, reference_manifest
    )
    visible_paths = {
        segment.path: _sha256_file(output_dir / segment.path)
        for segment in recovered.segments
    }
    assert orphan.read_bytes() != b"uncommitted bytes must never be readable"
    assert visible_paths["segment_000002.parquet"] == (
        recovered.segments[2].checksum
    )
    assert not list(output_dir.glob(f"*{TEMP_SUFFIX}"))
    # Every visible file is backed by exactly one manifest entry.
    assert sorted(visible_paths) == sorted(
        segment.path for segment in recovered.segments
    )


def test_recovery_after_temp_only_crash_rebuilds_remaining_segments(
    tmp_path: Path, engine_a: Any, baseline: tuple[Path, Any]
) -> None:
    output_dir = tmp_path / "interrupted"
    crashing = FakeTrino(engine_a, SNAPSHOT_A, fail_on_fetch=5)
    with pytest.raises(DisconnectError):
        make_exporter(crashing, output_dir).export()

    recovered_client = FakeTrino(engine_a, SNAPSHOT_A)
    recovered = make_exporter(recovered_client, output_dir).export()
    _, reference_manifest = baseline

    assert_manifest_matches_reference(
        output_dir, recovered, reference_manifest
    )
    pd.testing.assert_frame_equal(
        SegmentedExporter(
            recovered_client,
            output_dir,
            snapshot_id=SNAPSHOT_A,
            query=BASE_SQL,
            order_by=ORDER_BY,
            params={"min_id": 1},
            snapshot_check=(PROBE_SQL, SNAPSHOT_A),
            batch_size=BATCH_SIZE,
            segment_rows=SEGMENT_ROWS,
        ).read_all(recovered),
        expected_frame(),
    )
    assert not list(output_dir.glob(f"*{TEMP_SUFFIX}"))
    # Recovery reuses the same query text and bind parameters, plus a
    # keyset predicate anchored on the last confirmed row.
    resume_sql = recovered_client.executed_sql[-1]
    assert BASE_SQL in resume_sql
    assert "WHERE (id) > (:_resume_key_0)" in resume_sql
    resume_params = recovered_client.executed_params[-1]
    assert resume_params["min_id"] == 1
    assert resume_params["_resume_key_0"] == 2 * SEGMENT_ROWS
    assert recovered_client.snapshot_value == SNAPSHOT_A


def test_tampered_confirmed_segment_invalidates_everything_after_it(
    tmp_path: Path, engine_a: Any, baseline: tuple[Path, Any]
) -> None:
    output_dir = tmp_path / "tampered"
    with pytest.raises(DisconnectError):
        make_exporter(
            FakeTrino(engine_a, SNAPSHOT_A, fail_on_fetch=5), output_dir
        ).export()

    second_segment = output_dir / "segment_000001.parquet"
    original = pq.read_table(second_segment).to_pandas()
    tampered = original.copy()
    tampered["sku"] = [f"TAMPERED-{index}" for index in range(len(tampered))]
    assert len(tampered) == len(original)
    pq.write_table(
        pa.Table.from_pandas(tampered, preserve_index=False),
        second_segment,
    )
    tampered_checksum = _sha256_file(second_segment)

    recovered_client = FakeTrino(engine_a, SNAPSHOT_A)
    recovered = make_exporter(recovered_client, output_dir).export()
    reference_dir, reference_manifest = baseline

    assert_manifest_matches_reference(
        output_dir, recovered, reference_manifest
    )
    # The tampered bytes are gone: the damaged segment and every later one
    # were rebuilt against the original snapshot.
    assert tampered_checksum not in {
        _sha256_file(output_dir / segment.path)
        for segment in recovered.segments
    }
    frame = make_exporter(recovered_client, output_dir).read_all(recovered)
    pd.testing.assert_frame_equal(frame, expected_frame())
    # The damaged segment was the second one: the first confirmed segment is
    # byte-identical to the uninterrupted reference and was not rewritten.
    first_path = output_dir / recovered.segments[0].path
    reference_first = reference_dir / reference_manifest.segments[0].path
    assert _sha256_file(first_path) == _sha256_file(reference_first)
    assert not list(output_dir.glob(f"*{TEMP_SUFFIX}"))


def test_changed_snapshot_cannot_append_to_old_manifest(
    tmp_path: Path, engine_a: Any
) -> None:
    output_dir = tmp_path / "snapshot_switch"
    with pytest.raises(DisconnectError):
        make_exporter(
            FakeTrino(engine_a, SNAPSHOT_A, fail_on_fetch=5), output_dir
        ).export()

    before = {path.name: path.read_bytes() for path in output_dir.iterdir()}
    engine_b = build_engine(SNAPSHOT_B, tamper_rows=True)
    client_b = FakeTrino(engine_b, SNAPSHOT_B)

    with pytest.raises(SnapshotMismatchError, match="snapshot"):
        make_exporter(client_b, output_dir, snapshot_id=SNAPSHOT_B).export()

    # No export query ran and nothing on disk changed.
    assert client_b.executed_sql == []
    after = {path.name: path.read_bytes() for path in output_dir.iterdir()}
    assert after == before
    saved = json.loads((output_dir / MANIFEST_NAME).read_text())
    assert saved["snapshot_id"] == SNAPSHOT_A
    assert saved["complete"] is False

    # Redoing the work against the original snapshot completes normally and
    # matches an uninterrupted export, including the final short segment.
    recovered = make_exporter(
        FakeTrino(engine_a, SNAPSHOT_A), output_dir
    ).export()
    assert recovered.complete is True
    assert recovered.segments[-1].row_count == 1
    pd.testing.assert_frame_equal(
        make_exporter(FakeTrino(engine_a, SNAPSHOT_A), output_dir).read_all(
            recovered
        ),
        expected_frame(),
    )
    engine_b.dispose()


def test_stale_snapshot_probe_blocks_recovery_even_with_same_identifier(
    tmp_path: Path, engine_a: Any
) -> None:
    output_dir = tmp_path / "stale_probe"
    with pytest.raises(DisconnectError):
        make_exporter(
            FakeTrino(engine_a, SNAPSHOT_A, fail_on_fetch=5), output_dir
        ).export()

    stale_client = FakeTrino(engine_a, SNAPSHOT_B)
    with pytest.raises(SnapshotMismatchError, match="Snapshot probe"):
        make_exporter(stale_client, output_dir).export()
    # Only the probe ran; the export query itself was never executed.
    assert stale_client.executed_sql == [PROBE_SQL]
    saved = json.loads((output_dir / MANIFEST_NAME).read_text())
    assert saved["complete"] is False


def test_missing_snapshot_basis_is_rejected(
    tmp_path: Path, engine_a: Any
) -> None:
    for snapshot_id in (None, ""):
        with pytest.raises(ResumeRejectedError, match="snapshot identifier"):
            SegmentedExporter(
                FakeTrino(engine_a, SNAPSHOT_A),
                tmp_path / f"missing-{snapshot_id}",
                snapshot_id=snapshot_id,
                query=BASE_SQL,
                order_by=ORDER_BY,
            )


def test_duplicate_ordering_key_is_rejected(tmp_path: Path) -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE orders (id INTEGER, sku TEXT)"))
        connection.execute(
            text("INSERT INTO orders (id, sku) VALUES (:id, :sku)"),
            [
                {"id": 1, "sku": "a"},
                {"id": 2, "sku": "b"},
                {"id": 2, "sku": "c"},
                {"id": 3, "sku": "d"},
            ],
        )
    output_dir = tmp_path / "duplicate-key"
    with pytest.raises(OrderKeyNotUniqueError, match="not unique"):
        SegmentedExporter(
            FakeTrino(engine, SNAPSHOT_A),
            output_dir,
            snapshot_id=SNAPSHOT_A,
            query="SELECT id, sku FROM orders",
            order_by=["id"],
            batch_size=4,
            segment_rows=3,
        ).export()
    assert not list(output_dir.glob("segment_*.parquet"))
    engine.dispose()


def test_duplicate_key_across_batch_boundary_is_rejected(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE orders (id INTEGER, ver INTEGER)")
        )
        connection.execute(
            text("INSERT INTO orders (id, ver) VALUES (:id, :ver)"),
            [
                {"id": 1, "ver": 1},
                {"id": 2, "ver": 1},
                {"id": 3, "ver": 1},
                {"id": 3, "ver": 2},
            ],
        )
    with pytest.raises(OrderKeyNotUniqueError):
        SegmentedExporter(
            FakeTrino(engine, SNAPSHOT_A),
            tmp_path / "boundary-key",
            snapshot_id=SNAPSHOT_A,
            query="SELECT id, ver FROM orders",
            order_by=["id"],
            batch_size=2,
            segment_rows=2,
        ).export()
    engine.dispose()


def test_invalid_order_by_declarations_are_rejected(
    tmp_path: Path, engine_a: Any
) -> None:
    def build(order_by: list[str]) -> SegmentedExporter:
        return SegmentedExporter(
            FakeTrino(engine_a, SNAPSHOT_A),
            tmp_path / "bad-order",
            snapshot_id=SNAPSHOT_A,
            query=BASE_SQL,
            order_by=order_by,
        )

    with pytest.raises(ResumeRejectedError, match="non-empty"):
        build([])
    with pytest.raises(ResumeRejectedError, match="Duplicate"):
        build(["id", "id"])
    with pytest.raises(ResumeRejectedError, match="identifiers"):
        build(["id; DROP TABLE orders"])


def test_missing_order_column_in_result_is_rejected(
    tmp_path: Path, engine_a: Any
) -> None:
    with pytest.raises(ResumeRejectedError, match="absent from result"):
        SegmentedExporter(
            FakeTrino(engine_a, SNAPSHOT_A),
            tmp_path / "missing-column",
            snapshot_id=SNAPSHOT_A,
            query="SELECT sku, qty FROM orders",
            order_by=["id"],
        ).export()


def test_reserved_bind_prefix_is_rejected(
    tmp_path: Path, engine_a: Any
) -> None:
    with pytest.raises(ResumeRejectedError, match="reserved"):
        SegmentedExporter(
            FakeTrino(engine_a, SNAPSHOT_A),
            tmp_path / "reserved-bind",
            snapshot_id=SNAPSHOT_A,
            query=BASE_SQL,
            order_by=ORDER_BY,
            params={"_resume_key_9": 1},
        )


def test_deleted_confirmed_segment_is_rebuilt(
    tmp_path: Path, engine_a: Any, baseline: tuple[Path, Any]
) -> None:
    output_dir = tmp_path / "deleted"
    with pytest.raises(DisconnectError):
        make_exporter(
            FakeTrino(engine_a, SNAPSHOT_A, fail_on_fetch=5), output_dir
        ).export()
    (output_dir / "segment_000000.parquet").unlink()

    recovered = make_exporter(
        FakeTrino(engine_a, SNAPSHOT_A), output_dir
    ).export()
    _, reference_manifest = baseline
    assert_manifest_matches_reference(
        output_dir, recovered, reference_manifest
    )
    pd.testing.assert_frame_equal(
        make_exporter(FakeTrino(engine_a, SNAPSHOT_A), output_dir).read_all(
            recovered
        ),
        expected_frame(),
    )


def test_composite_ordering_key_resumes_with_row_value_predicate(
    tmp_path: Path,
) -> None:
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            text("CREATE TABLE orders (id INTEGER, ver INTEGER)")
        )
        connection.execute(
            text("INSERT INTO orders (id, ver) VALUES (:id, :ver)"),
            [
                {"id": identifier // 2, "ver": identifier % 2}
                for identifier in range(10)
            ],
        )
    output_dir = tmp_path / "composite"
    client = FakeTrino(engine, SNAPSHOT_A, fail_on_fetch=3)
    exporter = SegmentedExporter(
        client,
        output_dir,
        snapshot_id=SNAPSHOT_A,
        query="SELECT id, ver FROM orders",
        order_by=["id", "ver"],
        batch_size=2,
        segment_rows=3,
    )
    with pytest.raises(DisconnectError):
        exporter.export()

    recovered_client = FakeTrino(engine, SNAPSHOT_A)
    recovered = SegmentedExporter(
        recovered_client,
        output_dir,
        snapshot_id=SNAPSHOT_A,
        query="SELECT id, ver FROM orders",
        order_by=["id", "ver"],
        batch_size=2,
        segment_rows=3,
    ).export()
    assert recovered.complete is True
    assert "WHERE (id, ver) > (:_resume_key_0, :_resume_key_1)" in (
        recovered_client.executed_sql[-1]
    )
    frame = pq.read_table(
        output_dir / recovered.segments[-1].path
    ).to_pandas()
    assert frame[["id", "ver"]].drop_duplicates().shape[0] == len(frame)
    engine.dispose()


def test_short_and_empty_final_batches_complete_the_export(
    tmp_path: Path, engine_a: Any
) -> None:
    output_dir = tmp_path / "short-batch"
    client = FakeTrino(engine_a, SNAPSHOT_A)
    exporter = make_exporter(
        client, output_dir, batch_size=ROW_COUNT * 4, segment_rows=10
    )
    manifest = exporter.export()
    # One short fetch returning all rows, then one empty fetch ending it.
    assert client.fetch_calls == [ROW_COUNT * 4, ROW_COUNT * 4]
    assert manifest.complete is True
    pd.testing.assert_frame_equal(exporter.read_all(manifest), expected_frame())


def test_empty_result_set_completes_without_segments(
    tmp_path: Path, engine_a: Any
) -> None:
    output_dir = tmp_path / "empty"
    exporter = SegmentedExporter(
        FakeTrino(engine_a, SNAPSHOT_A),
        output_dir,
        snapshot_id=SNAPSHOT_A,
        query="SELECT id, sku, qty FROM orders WHERE id > 1000",
        order_by=ORDER_BY,
        batch_size=BATCH_SIZE,
        segment_rows=SEGMENT_ROWS,
    )
    manifest = exporter.export()
    assert manifest.complete is True
    assert manifest.segments == ()
    assert exporter.read_all(manifest).columns.tolist() == [
        "id",
        "sku",
        "qty",
    ]
    assert not list(output_dir.glob(f"*{TEMP_SUFFIX}"))


def test_completed_export_is_idempotent_and_does_not_reconnect(
    baseline: tuple[Path, Any], engine_a: Any
) -> None:
    output_dir, manifest = baseline
    client = FakeTrino(engine_a, SNAPSHOT_A)
    exporter = make_exporter(client, output_dir)
    again = exporter.export()
    assert again == manifest
    assert client.connections == 0


def test_query_and_parameters_must_match_the_manifest(
    tmp_path: Path, engine_a: Any
) -> None:
    output_dir = tmp_path / "params"
    with pytest.raises(DisconnectError):
        make_exporter(
            FakeTrino(engine_a, SNAPSHOT_A, fail_on_fetch=5), output_dir
        ).export()

    client = FakeTrino(engine_a, SNAPSHOT_A)
    different_params = SegmentedExporter(
        client,
        output_dir,
        snapshot_id=SNAPSHOT_A,
        query=BASE_SQL,
        order_by=ORDER_BY,
        params={"min_id": 2},
        snapshot_check=(PROBE_SQL, SNAPSHOT_A),
        batch_size=BATCH_SIZE,
        segment_rows=SEGMENT_ROWS,
    )
    with pytest.raises(ResumeRejectedError, match="parameters differ"):
        different_params.export()

    different_query = SegmentedExporter(
        client,
        output_dir,
        snapshot_id=SNAPSHOT_A,
        query="SELECT id, sku, qty FROM orders",
        order_by=ORDER_BY,
        snapshot_check=(PROBE_SQL, SNAPSHOT_A),
        batch_size=BATCH_SIZE,
        segment_rows=SEGMENT_ROWS,
    )
    with pytest.raises(ResumeRejectedError, match="Query text differs"):
        different_query.export()
    assert client.executed_sql == []
