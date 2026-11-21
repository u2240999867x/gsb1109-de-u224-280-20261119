from __future__ import annotations

import os
import warnings
from typing import TYPE_CHECKING, Any, Literal, cast

import pyarrow as pa
import pyarrow.parquet as pq
import pytest

import narwhals as nw
from narwhals.exceptions import (
    ArchiveBasisMismatchError,
    ArchiveError,
    ArchivePublishError,
    ArchiveStagingError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping
    from pathlib import Path

    from narwhals.archive import VersionInfo

pytest.importorskip("polars")

import polars as pl

SCHEMA = {"id": pl.Int64, "tag": pl.String}
V1_ROWS: dict[str, list[object]] = {
    "id": [1, 2, 3, 4],
    "tag": ["v1a", "v1a", "v1b", "v1b"],
}
V2_ROWS: dict[str, list[object]] = {
    "id": [101, 102, 103, 104],
    "tag": ["v2a", "v2a", "v2b", "v2b"],
}


def _archive(
    tmp_path: Path, name: str = "archive.parquet", **kwargs: str
) -> nw.archive.Archive:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        return nw.archive.open_archive(tmp_path / name, **kwargs)


def _frame(rows: dict[str, list[object]]) -> pl.LazyFrame:
    return pl.LazyFrame(rows, schema=SCHEMA)


def _publish(
    archive: nw.archive.Archive,
    version: str,
    rows: dict[str, list[object]],
    *,
    basis: VersionInfo | Literal["current"] | None = "current",
) -> nw.archive.Receipt:
    plan = archive.prepare(version, basis=basis, row_group_size=2)
    return plan.publish(_frame(rows))


def _rows(frame: nw.LazyFrame[Any]) -> dict[str, list[object]]:
    return cast("dict[str, list[object]]", frame.collect().to_dict(as_series=False))


def _row_group_rows(path: Path) -> list[dict[str, list[object]]]:
    parquet = pq.ParquetFile(path)
    return [
        parquet.read_row_group(index).to_pydict()
        for index in range(parquet.metadata.num_row_groups)
    ]


def test_publish_and_read_returns_credentials(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    assert archive.version() is None

    receipt = _publish(archive, "v1", V1_ROWS, basis=None)
    assert receipt.status == "published"
    assert receipt.published
    assert not receipt.read
    assert receipt.version is not None
    assert receipt.version.name == "v1"
    assert receipt.version.basis is None
    assert receipt.path == archive.path

    version = archive.version()
    assert version == receipt.version
    assert len(_row_group_rows(archive.path)) == 2

    frame, read_receipt = archive.scan()
    assert read_receipt.status == "read"
    assert read_receipt.read
    assert read_receipt.version == receipt.version
    assert _rows(frame) == V1_ROWS


def test_reader_is_pinned_across_successful_replacement(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    assert v1 is not None

    reader = archive.reader()
    assert reader.version == v1
    first_group = reader.lazy().head(2).collect().to_native().to_dict(as_series=False)
    assert first_group == {"id": [1, 2], "tag": ["v1a", "v1a"]}

    v2 = _publish(archive, "v2", V2_ROWS, basis=v1)
    assert v2.status == "published"
    assert archive.version() is not None
    assert archive.version().name == "v2"  # type: ignore[union-attr]

    # The already-open reader keeps reading V1, including its second row group.
    second_group = reader.lazy().tail(2).collect().to_native().to_dict(as_series=False)
    assert second_group == {"id": [3, 4], "tag": ["v1b", "v1b"]}
    assert _rows(reader.lazy()) == V1_ROWS
    assert reader.version == v1
    reader.close()

    # A new reader sees V2 only.
    assert _rows(archive.reader().lazy()) == V2_ROWS


def test_two_readers_opened_before_and_after_replacement(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    before = archive.reader()
    _publish(archive, "v2", V2_ROWS, basis=v1)
    after = archive.reader()
    assert _rows(before.lazy()) == V1_ROWS
    assert _rows(after.lazy()) == V2_ROWS
    assert before.version != after.version


def test_one_lazy_pipeline_pinned_before_replacement(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    pipeline = archive.reader().lazy()

    first = pipeline.head(2).collect().to_native().to_dict(as_series=False)
    assert first == {"id": [1, 2], "tag": ["v1a", "v1a"]}

    _publish(archive, "v2", V2_ROWS, basis=v1)

    second = pipeline.tail(2).collect().to_native().to_dict(as_series=False)
    assert second == {"id": [3, 4], "tag": ["v1b", "v1b"]}
    assert pipeline.collect().to_native().to_dict(as_series=False) == V1_ROWS


def test_two_publishers_from_the_same_basis_one_wins(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    first = archive.prepare("a", basis=v1)
    second = archive.prepare("b", basis=v1)

    assert first.publish(_frame(V2_ROWS)).status == "published"
    receipt = second.publish(_frame(V1_ROWS))
    assert receipt.status == "stale"
    assert archive.version().name == "a"  # type: ignore[union-attr]


def test_basis_changing_mid_write_is_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    plan = archive.prepare("v2", basis=v1)

    calls = iter([True, False, False])

    def basis_is_current() -> bool:
        return next(calls)

    monkeypatch.setattr(type(plan), "_basis_is_current", lambda _self: basis_is_current())
    receipt = plan.publish(_frame(V2_ROWS))
    assert receipt.status == "stale"
    assert archive.version().name == "v1"  # type: ignore[union-attr]
    assert not list(tmp_path.glob("*.staging.parquet"))


def test_plan_can_publish_only_once(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    plan = archive.prepare("v2", basis=v1)
    assert plan.publish(_frame(V2_ROWS)).status == "published"
    receipt = plan.publish(_frame(V1_ROWS))
    assert receipt.status == "stale"
    assert archive.version().name == "v2"  # type: ignore[union-attr]


def test_slow_plan_reads_only_its_basis(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    assert v1 is not None

    plan = archive.prepare("result-a", basis=v1, row_group_size=2)
    assert plan.basis_version == v1
    assert plan.stale is False

    first = plan.basis.head(2).collect().to_native().to_dict(as_series=False)
    assert first == {"id": [1, 2], "tag": ["v1a", "v1a"]}

    _publish(archive, "v2", V2_ROWS, basis=v1)

    second = plan.basis.tail(2).collect().to_native().to_dict(as_series=False)
    assert second == {"id": [3, 4], "tag": ["v1b", "v1b"]}

    # Both reads during the plan's lifetime came from V1.
    observed = {"id": first["id"] + second["id"], "tag": first["tag"] + second["tag"]}
    assert observed == V1_ROWS
    assert "101" not in str(observed)

    receipt = plan.publish(_frame(observed))
    assert receipt.status == "stale"
    assert isinstance(receipt.error, ArchiveBasisMismatchError)
    assert not receipt.published
    with pytest.raises(ArchiveBasisMismatchError):
        receipt.raise_for_status()
    assert archive.version().name == "v2"  # type: ignore[union-attr]


def test_same_size_replacements_still_keep_versions_distinct(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    size_v1 = archive.path.stat().st_size
    _publish(archive, "v2", V2_ROWS, basis=v1)
    size_v2 = archive.path.stat().st_size
    assert size_v1 == size_v2
    assert archive.version().token != v1.token  # type: ignore[union-attr]
    assert _row_group_rows(archive.path)[0]["id"] == [101, 102]


def test_stale_plan_declared_up_front_does_not_read_basis(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    _publish(archive, "v2", V2_ROWS, basis=v1)

    plan = archive.prepare("c1", basis=v1)
    assert plan.stale is True
    with pytest.raises(ArchiveBasisMismatchError):
        _ = plan.basis

    calls: list[int] = []

    def source(df: pl.DataFrame) -> pl.DataFrame:
        calls.append(1)
        return df

    # Publishing a stale plan returns before executing the source pipeline.
    pipeline = _frame(V1_ROWS).map_batches(source, schema=SCHEMA)
    receipt = plan.publish(pipeline)
    assert receipt.status == "stale"
    assert calls == []
    assert archive.version().name == "v2"  # type: ignore[union-attr]


def test_staging_failure_keeps_published_version(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    plan = archive.prepare("v2", basis=v1)

    def broken_sink(
        _frame: object,
        path: Path,
        _metadata: Mapping[str, str],
        _options: Mapping[str, object],
    ) -> None:
        path.write_bytes(b"partial")
        msg = "disk full"
        raise OSError(msg)

    receipt = plan.publish(_frame(V2_ROWS), sink=broken_sink)
    assert receipt.status == "staging-failed"
    assert isinstance(receipt.error, ArchiveStagingError)
    assert not receipt.published

    assert archive.version().name == "v1"  # type: ignore[union-attr]
    assert _rows(archive.scan()[0]) == V1_ROWS
    assert not list(tmp_path.glob("*.staging.parquet"))


def test_footer_mismatch_is_rejected(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    plan = archive.prepare("v2", basis=v1)

    def lying_sink(
        _frame: object,
        path: Path,
        metadata: Mapping[str, str],
        _options: Mapping[str, object],
    ) -> None:
        wrong = dict(metadata)
        wrong["narwhals.archive.version"] = "not-v2"
        pl.LazyFrame({"id": [9], "tag": ["c"]}, schema=SCHEMA).sink_parquet(
            str(path), metadata=wrong
        )

    receipt = plan.publish(_frame(V2_ROWS), sink=lying_sink)
    assert receipt.status == "verification-failed"
    assert isinstance(receipt.error, ArchivePublishError)
    assert archive.version().name == "v1"  # type: ignore[union-attr]
    assert _rows(archive.scan()[0]) == V1_ROWS


def test_legal_plan_after_failures_publishes(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    plan = archive.prepare("v2", basis=v1)
    assert plan.publish(_frame(V2_ROWS)).status == "published"

    v2 = archive.version()
    assert v2 is not None
    assert v2.name == "v2"
    assert v2.basis == v1.token  # type: ignore[union-attr]
    assert _rows(archive.reader().lazy()) == V2_ROWS


def test_basis_frame_is_lazy_and_not_collected_for_verification(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    plan = archive.prepare("v2", basis=v1)
    frame = plan.basis
    assert isinstance(frame, nw.LazyFrame)
    native = frame.to_native()
    assert isinstance(native, pl.LazyFrame)
    # Building and inspecting the plan did not materialise the basis.
    assert _rows(plan.basis) == V1_ROWS


def test_native_sink_is_used_without_collect(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    plan = archive.prepare("v2", basis=v1)

    original = pl.LazyFrame.sink_parquet
    calls: list[str] = []

    def spy(self: pl.LazyFrame, *args: Any, **kwargs: Any) -> None:
        calls.append("sink")
        original(self, *args, **kwargs)

    def forbidden_collect(_self: object, *_args: object, **_kwargs: object) -> None:
        msg = "collect must not be called during publication"
        raise AssertionError(msg)

    monkeypatch.setattr(pl.LazyFrame, "sink_parquet", spy)
    monkeypatch.setattr(pl.LazyFrame, "collect", forbidden_collect)

    receipt = plan.publish(_frame(V2_ROWS))
    assert receipt.status == "published"
    assert calls == ["sink"]


def test_pyarrow_reads_published_versions(tmp_path: Path) -> None:
    archive = _archive(tmp_path, read_backend="pyarrow")
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    reader = archive.reader()
    assert reader.version == v1
    first = reader.lazy().head(2).collect().to_dict(as_series=False)
    _publish(archive, "v2", V2_ROWS, basis=v1)
    second = reader.lazy().tail(2).collect().to_dict(as_series=False)
    assert first == {"id": [1, 2], "tag": ["v1a", "v1a"]}
    assert second == {"id": [3, 4], "tag": ["v1b", "v1b"]}


def test_backend_without_native_sink_is_rejected(tmp_path: Path) -> None:
    archive = _archive(tmp_path, backend="pandas")
    receipt = archive.prepare("v1", basis=None).publish(
        pl.LazyFrame({"id": [1]}, schema={"id": pl.Int64})
    )
    assert receipt.status == "staging-failed"
    assert isinstance(receipt.error, ArchiveStagingError)
    assert not archive.path.exists()


def test_unrelated_parquet_is_not_an_archive(tmp_path: Path) -> None:
    path = tmp_path / "plain.parquet"
    pq.write_table(pa.table({"a": [1]}), path)
    archive = _archive(tmp_path, name="plain.parquet")
    assert archive.version() is None
    with pytest.raises(ArchiveError):
        archive.reader()


def test_corrupt_archive_reader_reports_error(tmp_path: Path) -> None:
    path = tmp_path / "corrupt.parquet"
    path.write_bytes(b"not parquet")
    archive = _archive(tmp_path, name="corrupt.parquet")
    with pytest.raises(ArchiveError, match="could not be read"):
        archive.reader()


def test_empty_path_is_not_an_archive(tmp_path: Path) -> None:
    path = tmp_path / "empty.parquet"
    path.write_bytes(b"")
    archive = _archive(tmp_path, name="empty.parquet")
    with pytest.raises(ArchiveError, match="could not be read"):
        archive.reader()


def test_new_archive_plan_has_no_basis_to_read(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    plan = archive.prepare("v1", basis=None)
    with pytest.raises(ArchiveBasisMismatchError, match="starts a new archive"):
        _ = plan.basis


def test_missing_archive_read_raises(tmp_path: Path) -> None:
    archive = _archive(tmp_path, name="missing.parquet")
    with pytest.raises(ArchiveError, match="No published archive"):
        archive.reader()


def test_existing_path_with_basis_none_is_refused(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    _publish(archive, "v1", V1_ROWS, basis=None)
    plan = archive.prepare("v2", basis=None)
    assert plan.stale is True
    receipt = plan.publish(_frame(V2_ROWS))
    assert receipt.status == "stale"


def test_bad_archive_path_and_version(tmp_path: Path) -> None:
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        with pytest.raises(TypeError, match="filesystem path"):
            nw.archive.open_archive(object())  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="Unsupported archive backend"):
            nw.archive.open_archive(tmp_path / "a.parquet", backend="nope")
    archive = _archive(tmp_path)
    with pytest.raises(ValueError, match="non-empty string"):
        archive.prepare("")


def test_sink_parquet_still_works_without_archive(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pl.LazyFrame, "sink_parquet", pl.LazyFrame.sink_parquet)
    path = tmp_path / "plain.parquet"
    nw.from_native(pl.LazyFrame({"a": [1, 2]})).sink_parquet(str(path))
    assert path.exists()
    assert pq.ParquetFile(path).metadata.metadata is not None


def test_reader_close_is_idempotent_and_success_receipt_is_quiet(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    receipt = _publish(archive, "v1", V1_ROWS, basis=None)
    receipt.raise_for_status()

    reader = archive.reader()
    reader.close()
    reader.close()


def test_prepare_current_on_empty_and_missing_paths(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    plan = archive.prepare("v1")
    assert plan.basis_version is None
    assert plan.stale is False
    assert plan.publish(_frame(V1_ROWS)).status == "published"

    other = _archive(tmp_path, name="other.parquet")
    v1 = archive.version()
    assert v1 is not None
    stale = other.prepare("v2", basis=v1)
    assert stale.stale is True


def test_concrete_basis_reads_even_before_any_publication(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    missing = nw.archive.VersionInfo("gone", "0" * 32, None, nw.Schema())
    plan = archive.prepare("v1", basis=missing)
    assert plan.stale is True
    assert plan.publish(_frame(V1_ROWS)).status == "stale"


def test_publish_accepts_eager_polars_frame(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    plan = archive.prepare("v1", basis=None)
    receipt = plan.publish(pl.DataFrame({"id": [1, 2], "tag": ["a", "b"]}, schema=SCHEMA))
    assert receipt.status == "published"
    assert _rows(archive.reader().lazy()) == {"id": [1, 2], "tag": ["a", "b"]}


def test_pinned_reader_supports_file_protocol(tmp_path: Path) -> None:
    from narwhals.archive import _PinnedReader

    path = tmp_path / "raw.bin"
    path.write_bytes(b"abcdef")
    reader = _PinnedReader(os.open(path, os.O_RDONLY), 6)
    assert reader.readable()
    assert reader.seekable()
    assert not reader.writable()
    assert reader.fileno() >= 0
    assert reader.read(1) == b"a"
    assert reader.seek(1, os.SEEK_CUR) == 2
    assert reader.seek(-1, os.SEEK_END) == 5
    assert reader.tell() == 5
    assert reader.read() == b"f"
    with pytest.raises(ValueError, match="Negative seek position"):
        reader.seek(-10)
    reader.seek(0)
    buffer = bytearray(3)
    assert reader.readinto(buffer) == 3
    assert bytes(buffer) == b"abc"
    assert reader.closed is False
    reader.close()
    assert reader.closed is True


def test_invalid_backend_and_native_sink_type(tmp_path: Path) -> None:
    from narwhals.archive import _native_polars_sink

    with pytest.raises(ValueError, match="Unsupported archive backend"):
        nw.archive.Archive(tmp_path / "a.parquet", read_backend="nope")
    with pytest.raises(ArchiveStagingError, match="expects a Polars LazyFrame"):
        _native_polars_sink({"a": 1}, tmp_path / "x.parquet", {}, {})


def test_entry_helpers_and_reprs(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    receipt = _publish(archive, "v1", V1_ROWS, basis=None)
    version = receipt.version
    assert version is not None
    assert "VersionInfo" in repr(version)
    assert "Receipt" in repr(receipt)
    assert "Archive" in repr(archive)

    frame, read_receipt = nw.archive.read_archive(archive.path)
    assert read_receipt.status == "read"
    assert _rows(frame) == V1_ROWS
    assert archive.read()[1].status == "read"
    assert hash(archive.version()) == hash(archive.version())

    plan = nw.archive.prepare_archive(archive.path, "v2", basis=version)
    with plan as entered:
        assert entered is plan
        assert entered.archive.path == archive.path
        assert entered.version == "v2"
        assert entered.publish(_frame(V2_ROWS)).status == "published"

    with archive.reader() as reader:
        assert reader.path == archive.path
        assert _rows(reader.lazy()) == V2_ROWS


def test_options_are_forwarded_and_overridden(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    plan = archive.prepare("v2", basis=v1, compression="uncompressed", statistics=False)
    receipt = plan.publish(
        _frame(V2_ROWS), row_group_size=2, compression="uncompressed", statistics=False
    )
    assert receipt.status == "published"
    assert len(_row_group_rows(archive.path)) == 2


def test_stale_plan_cannot_read_its_basis(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    _publish(archive, "v2", V2_ROWS, basis=v1)
    plan = archive.prepare("third", basis=v1)
    assert plan.stale is True
    with pytest.raises(ArchiveBasisMismatchError):
        _ = plan.basis


def test_sink_writing_wrong_token_is_rejected(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    plan = archive.prepare("v2", basis=v1)

    def token_sink(
        _frame: object,
        path: Path,
        metadata: Mapping[str, str],
        _options: Mapping[str, object],
    ) -> None:
        wrong = dict(metadata)
        wrong["narwhals.archive.token"] = "not-the-publication-token"
        pl.LazyFrame({"id": [9], "tag": ["c"]}, schema=SCHEMA).sink_parquet(
            str(path), metadata=wrong
        )

    receipt = plan.publish(_frame(V2_ROWS), sink=token_sink)
    assert receipt.status == "verification-failed"
    assert isinstance(receipt.error, ArchivePublishError)


def test_sink_writing_wrong_basis_is_rejected(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    plan = archive.prepare("v2", basis=v1)

    def basis_sink(
        _frame: object,
        path: Path,
        metadata: Mapping[str, str],
        _options: Mapping[str, object],
    ) -> None:
        wrong = dict(metadata)
        wrong["narwhals.archive.basis"] = "0" * 32
        pl.LazyFrame({"id": [9], "tag": ["c"]}, schema=SCHEMA).sink_parquet(
            str(path), metadata=wrong
        )

    receipt = plan.publish(_frame(V2_ROWS), sink=basis_sink)
    assert receipt.status == "verification-failed"
    assert isinstance(receipt.error, ArchivePublishError)


def test_sink_writing_without_archive_metadata_is_rejected(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    plan = archive.prepare("v2", basis=v1)

    def bare_sink(
        _frame: object,
        path: Path,
        _metadata: Mapping[str, str],
        _options: Mapping[str, object],
    ) -> None:
        pl.LazyFrame({"id": [9], "tag": ["c"]}, schema=SCHEMA).sink_parquet(str(path))

    receipt = plan.publish(_frame(V2_ROWS), sink=bare_sink)
    assert receipt.status == "verification-failed"
    assert isinstance(receipt.error, ArchivePublishError)


def test_sink_that_cannot_be_read_back_is_rejected(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version
    plan = archive.prepare("v2", basis=v1)

    def corrupt_sink(
        _frame: object,
        path: Path,
        _metadata: Mapping[str, str],
        _options: Mapping[str, object],
    ) -> None:
        path.write_bytes(b"not parquet")

    receipt = plan.publish(_frame(V2_ROWS), sink=corrupt_sink)
    assert receipt.status == "verification-failed"
    assert archive.version().name == "v1"  # type: ignore[union-attr]


def test_staging_file_is_never_opened_as_a_published_version(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    v1 = _publish(archive, "v1", V1_ROWS, basis=None).version

    def failing_after_write(
        _frame: object,
        path: Path,
        metadata: Mapping[str, str],
        _options: Mapping[str, object],
    ) -> None:
        pl.LazyFrame({"id": [5], "tag": ["x"]}, schema=SCHEMA).sink_parquet(
            str(path), metadata=dict(metadata)
        )
        msg = "crash after write"
        raise OSError(msg)

    receipt = archive.prepare("v2", basis=v1).publish(
        _frame(V2_ROWS), sink=failing_after_write
    )
    assert receipt.status == "staging-failed"
    assert archive.version().name == "v1"  # type: ignore[union-attr]
    assert _rows(archive.reader().lazy()) == V1_ROWS
    assert not list(tmp_path.glob("*.staging.parquet"))
