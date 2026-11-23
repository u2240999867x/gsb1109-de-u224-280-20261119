from __future__ import annotations

import numpy as np
import pytest

import xarray as xr
from xarray.core.delivery import DeliveryGroupBy, DeliveryManifest
from xarray.tests import requires_dask

MANIFEST_ROWS = [
    ("南区", "乙"),
    ("北区", None),
    ("北区", "甲"),
    ("北区", "丙"),
    ("北区", "乙"),
]


def make_manifest() -> DeliveryManifest:
    return DeliveryManifest(MANIFEST_ROWS)


def make_file(rows: list[tuple[str, str, int, float, str, int]]) -> xr.Dataset:
    """Build a sampling dataset from (region, instrument, round, reading, file, line)."""
    return xr.Dataset(
        {
            "region": ("record", [row[0] for row in rows]),
            "instrument": ("record", [row[1] for row in rows]),
            "round": ("record", np.array([row[2] for row in rows], dtype=np.int64)),
            "reading": (
                "record",
                np.array([row[3] for row in rows], dtype=np.float64),
            ),
            "filename": ("record", [row[4] for row in rows]),
            "line": ("record", np.array([row[5] for row in rows], dtype=np.int64)),
        }
    )


@pytest.fixture
def files() -> tuple[xr.Dataset, xr.Dataset, xr.Dataset]:
    file1 = make_file(
        [
            ("北区", "甲", 16, 4.0, "f1", 1),
            ("北区", "乙", 16, 6.0, "f1", 2),
            ("北区", "甲", 17, 3.0, "f1", 3),
        ]
    )
    file2 = make_file(
        [
            ("北区", "甲", 17, -3.0, "f2", 1),
            ("北区", "乙", 17, np.nan, "f2", 2),
            ("南区", "乙", 17, np.nan, "f2", 3),
            ("北区", "丁", 17, 40.0, "f2", 4),
        ]
    )
    file3 = make_file(
        [
            ("北区", "乙", 17, 5.0, "f3", 1),
            ("北区", "甲", 18, -4.0, "f3", 2),
            ("北区", "乙", 18, 4.0, "f3", 3),
        ]
    )
    return file1, file2, file3


@pytest.fixture
def segments(
    files: tuple[xr.Dataset, xr.Dataset, xr.Dataset],
) -> tuple[xr.Dataset, ...]:
    manifest = make_manifest()
    return tuple(
        ds.groupby(manifest=manifest).deliver(archive=archive)
        for archive, ds in enumerate(files, start=1)
    )


def report_row(report: xr.Dataset, row: int) -> dict[str, object]:
    return {
        "region": report["region"].values[row],
        "instrument": report["instrument"].values[row],
        "total": report["total"].values[row],
        "raw_count": report["raw_count"].values[row],
        "valid_count": report["valid_count"].values[row],
        "qualifying_rounds": report["qualifying_rounds"].values[row],
        "longest_consecutive_rounds": report["longest_consecutive_rounds"].values[row],
        "status": report["status"].values[row],
    }


def subtotal_row(report: xr.Dataset) -> dict[str, object]:
    (row,) = np.nonzero(report["is_subtotal"].values)
    return report_row(report, int(row[0]))


class TestManifest:
    def test_rows_in_report_order(self) -> None:
        manifest = make_manifest()
        assert manifest.rows == tuple(MANIFEST_ROWS)
        assert manifest.instrument_rows == (
            ("南区", "乙"),
            ("北区", "甲"),
            ("北区", "丙"),
            ("北区", "乙"),
        )
        assert manifest.subtotal_regions == ("北区",)

    def test_duplicate_rows_rejected(self) -> None:
        with pytest.raises(ValueError, match="duplicate"):
            DeliveryManifest([("北区", "甲"), ("北区", "甲")])

    def test_empty_rejected(self) -> None:
        with pytest.raises(ValueError, match="at least one row"):
            DeliveryManifest([])

    def test_groupby_returns_delivery_groupby(self) -> None:
        ds = make_file([])
        grouped = ds.groupby(manifest=make_manifest())
        assert isinstance(grouped, DeliveryGroupBy)

    def test_groupby_with_manifest_and_group_rejected(self) -> None:
        ds = make_file([])
        with pytest.raises(ValueError, match="manifest"):
            ds.groupby("region", manifest=make_manifest())

    def test_groupby_without_manifest_unchanged(self) -> None:
        ds = xr.Dataset({"foo": ("x", [1, 2, 3])}, coords={"g": ("x", list("aab"))})
        result = ds.groupby("g").sum()
        assert result["foo"].values.tolist() == [3, 3]


class TestReport:
    def test_files_1_and_2(self, segments: tuple[xr.Dataset, ...]) -> None:
        merged = xr.merge_delivery_segments(segments[:2])
        row = subtotal_row(merged.delivery.report)
        assert row["total"] == 10.0
        assert row["qualifying_rounds"] == 1.0
        assert row["longest_consecutive_rounds"] == 1.0

    def test_files_2_and_3(self, segments: tuple[xr.Dataset, ...]) -> None:
        merged = xr.merge_delivery_segments(segments[1:])
        row = subtotal_row(merged.delivery.report)
        assert row["total"] == 2.0
        assert row["qualifying_rounds"] == 2.0
        assert row["longest_consecutive_rounds"] == 2.0

    @pytest.mark.parametrize("order", ["first_pair", "last_pair", "reversed"])
    def test_full_merge_any_order(
        self, segments: tuple[xr.Dataset, ...], order: str
    ) -> None:
        first, second, third = segments
        if order == "first_pair":
            merged = xr.merge_delivery_segments(
                [xr.merge_delivery_segments([first, second]), third]
            )
        elif order == "last_pair":
            merged = xr.merge_delivery_segments(
                [first, xr.merge_delivery_segments([second, third])]
            )
        else:
            merged = xr.merge_delivery_segments([third, second, first])
        report = merged.delivery.report

        row = subtotal_row(report)
        assert row["total"] == 15.0
        assert row["raw_count"] == 8
        assert row["valid_count"] == 7
        assert row["qualifying_rounds"] == 3.0
        assert row["longest_consecutive_rounds"] == 3.0
        assert row["status"] == "ok"

        jia = report_row(report, 2)
        assert jia["total"] == 0.0  # measured zero, not empty
        assert jia["status"] == "ok"
        yi_north = report_row(report, 4)
        assert yi_north["total"] == 15.0

        yi_south = report_row(report, 0)
        assert np.isnan(yi_south["total"])
        assert yi_south["status"] == "all_invalid"
        bing = report_row(report, 3)
        assert np.isnan(bing["total"])
        assert bing["status"] == "no_samples"

    def test_round_gap_breaks_streak(
        self, files: tuple[xr.Dataset, xr.Dataset, xr.Dataset]
    ) -> None:
        file1, file2, _ = files
        file3 = make_file(
            [
                ("北区", "乙", 17, 5.0, "f3", 1),
                ("北区", "甲", 19, -4.0, "f3", 2),
                ("北区", "乙", 19, 4.0, "f3", 3),
            ]
        )
        manifest = make_manifest()
        merged = xr.merge_delivery_segments(
            [
                ds.groupby(manifest=manifest).deliver(archive=archive)
                for archive, ds in enumerate([file1, file2, file3], start=1)
            ]
        )
        row = subtotal_row(merged.delivery.report)
        assert row["total"] == 15.0
        assert row["qualifying_rounds"] == 3.0
        assert row["longest_consecutive_rounds"] == 2.0

    def test_subtotal_with_valid_readings_but_no_qualifying_round(self) -> None:
        ds = make_file([("北区", "甲", 1, 5.0, "f", 1)])
        segment = ds.groupby(manifest=make_manifest()).deliver(archive=1)
        row = subtotal_row(segment.delivery.report)
        assert np.isnan(row["total"])
        assert row["valid_count"] == 1
        assert row["qualifying_rounds"] == 0.0
        assert row["status"] == "no_qualifying_rounds"

    def test_errors_list(self, segments: tuple[xr.Dataset, ...]) -> None:
        merged = xr.merge_delivery_segments(list(segments))
        errors = merged.delivery.errors
        assert errors.sizes["error"] == 1
        assert errors["region"].values[0] == "北区"
        assert errors["instrument"].values[0] == "丁"
        assert errors["reading"].values[0] == 40.0
        assert errors["filename"].values[0] == "f2"
        assert errors["line"].values[0] == 4

    def test_report_does_not_consume_segment(
        self, segments: tuple[xr.Dataset, ...]
    ) -> None:
        first, second, _ = segments
        _ = first.delivery.report
        _ = first.delivery.errors
        merged = xr.merge_delivery_segments([first, second])
        assert subtotal_row(merged.delivery.report)["total"] == 10.0


class TestMerging:
    def test_direct_processing_matches_merging(
        self,
        files: tuple[xr.Dataset, xr.Dataset, xr.Dataset],
        segments: tuple[xr.Dataset, ...],
    ) -> None:
        manifest = make_manifest()
        combined = xr.concat(list(files), dim="record")
        direct = combined.groupby(manifest=manifest).deliver(archive=(1, 3))
        merged = xr.merge_delivery_segments(list(segments))
        assert direct.delivery.report.identical(merged.delivery.report)
        assert direct.delivery.errors.identical(merged.delivery.errors)

    @pytest.mark.parametrize("block_size", [1, 2, 3, 100])
    def test_block_size_does_not_change_result(
        self, files: tuple[xr.Dataset, xr.Dataset, xr.Dataset], block_size: int
    ) -> None:
        manifest = make_manifest()
        combined = xr.concat(list(files), dim="record")
        reference = combined.groupby(manifest=manifest).deliver(archive=(1, 3))
        blocked = combined.groupby(manifest=manifest).deliver(
            archive=(1, 3), block_size=block_size
        )
        assert blocked.delivery.report.identical(reference.delivery.report)
        assert blocked.delivery.errors.identical(reference.delivery.errors)

    @requires_dask
    def test_dask_chunks_are_read_blockwise(
        self, files: tuple[xr.Dataset, xr.Dataset, xr.Dataset]
    ) -> None:
        manifest = make_manifest()
        combined = xr.concat(list(files), dim="record")
        reference = combined.groupby(manifest=manifest).deliver(archive=(1, 3))
        chunked = combined.chunk({"record": 2})
        result = chunked.groupby(manifest=manifest).deliver(archive=(1, 3))
        assert result.delivery.report.identical(reference.delivery.report)
        assert result.delivery.errors.identical(reference.delivery.errors)

    def test_segment_size_does_not_grow_with_rounds(self) -> None:
        rows = [
            (region, instrument, number, 1.0, "big", number)
            for number in range(1, 2001)
            for region, instrument in [("北区", "甲"), ("北区", "乙")]
        ]
        big = make_file(rows).groupby(manifest=make_manifest()).deliver(archive=1)
        small = make_file(rows[:4]).groupby(manifest=make_manifest()).deliver(archive=1)
        assert dict(big.sizes) == dict(small.sizes)
        row = subtotal_row(big.delivery.report)
        assert row["total"] == 4000.0
        assert row["longest_consecutive_rounds"] == 2000.0

    def test_gap_between_segments_rejected(
        self, segments: tuple[xr.Dataset, ...]
    ) -> None:
        first, _, third = segments
        with pytest.raises(ValueError, match=r"gap.*\[1, 1\].*\[3, 3\]"):
            xr.merge_delivery_segments([first, third])

    def test_overlapping_segments_rejected(
        self, files: tuple[xr.Dataset, xr.Dataset, xr.Dataset]
    ) -> None:
        manifest = make_manifest()
        first = files[0].groupby(manifest=manifest).deliver(archive=(1, 2))
        second = files[1].groupby(manifest=manifest).deliver(archive=(2, 3))
        with pytest.raises(ValueError, match=r"overlap.*\[1, 2\].*\[2, 3\]"):
            xr.merge_delivery_segments([first, second])

    def test_different_manifests_rejected(
        self, files: tuple[xr.Dataset, xr.Dataset, xr.Dataset]
    ) -> None:
        first = files[0].groupby(manifest=make_manifest()).deliver(archive=1)
        other = DeliveryManifest([("北区", "甲")])
        second = files[1].groupby(manifest=other).deliver(archive=2)
        with pytest.raises(ValueError, match=r"different manifests"):
            xr.merge_delivery_segments([first, second])

    def test_non_segment_rejected(self) -> None:
        with pytest.raises(ValueError, match="delivery segment"):
            xr.merge_delivery_segments([xr.Dataset()])

    def test_accessor_merge(self, segments: tuple[xr.Dataset, ...]) -> None:
        first, second, _ = segments
        merged = first.delivery.merge(second)
        assert merged.delivery.archive_span == (1, 2)
        assert subtotal_row(merged.delivery.report)["total"] == 10.0

    def test_accessor_on_plain_dataset(self) -> None:
        ds = xr.Dataset()
        assert not ds.delivery.is_segment
        with pytest.raises(ValueError, match="delivery segment"):
            _ = ds.delivery.report

    def test_empty_file(self) -> None:
        segment = make_file([]).groupby(manifest=make_manifest()).deliver(archive=1)
        report = segment.delivery.report
        assert report["status"].values.tolist() == ["no_samples"] * 5
        assert segment.delivery.errors.sizes["error"] == 0

    def test_empty_segment_merges(self, segments: tuple[xr.Dataset, ...]) -> None:
        empty = make_file([]).groupby(manifest=make_manifest()).deliver(archive=0)
        merged = xr.merge_delivery_segments([empty, segments[0]])
        assert merged.delivery.archive_span == (0, 1)
        assert merged.delivery.report.identical(
            xr.merge_delivery_segments([segments[0]]).delivery.report
        )

    def test_round_spanning_many_files(self) -> None:
        manifest = make_manifest()
        files = [
            make_file([("北区", "甲", 5, 1.0, "a", 1)]),
            make_file([("北区", "乙", 5, 2.0, "b", 1)]),
            make_file([("北区", "甲", 5, 3.0, "c", 1)]),
            make_file([("北区", "乙", 5, 4.0, "d", 1)]),
        ]
        segments = [
            ds.groupby(manifest=manifest).deliver(archive=archive)
            for archive, ds in enumerate(files, start=1)
        ]
        merged = xr.merge_delivery_segments(segments)
        direct = (
            xr.concat(files, dim="record")
            .groupby(manifest=manifest)
            .deliver(archive=(1, 4))
        )
        assert merged.delivery.report.identical(direct.delivery.report)
        row = subtotal_row(merged.delivery.report)
        assert row["total"] == 10.0
        assert row["qualifying_rounds"] == 1.0

    def test_error_only_file_merges(self, segments: tuple[xr.Dataset, ...]) -> None:
        manifest = make_manifest()
        errors_only = make_file(
            [("北区", "丁", 19, 9.0, "f4", 1), ("西区", "戊", 19, 1.0, "f4", 2)]
        )
        segment = errors_only.groupby(manifest=manifest).deliver(archive=4)
        merged = xr.merge_delivery_segments([*segments, segment])
        assert merged.delivery.archive_span == (1, 4)
        assert merged.delivery.errors.sizes["error"] == 3
        assert subtotal_row(merged.delivery.report)["total"] == 15.0

    def test_decreasing_rounds_rejected(self) -> None:
        ds = make_file([("北区", "甲", 9, 1.0, "f", 1), ("北区", "甲", 8, 1.0, "f", 2)])
        with pytest.raises(ValueError, match="non-decreasing"):
            ds.groupby(manifest=make_manifest()).deliver(archive=1)

    def test_custom_column_names(self) -> None:
        manifest = DeliveryManifest(
            [("R", None), ("R", "i")],
            dim="obs",
            region="reg",
            instrument="ins",
            round="rd",
            reading="val",
            filename="fn",
            line="ln",
        )
        ds = xr.Dataset(
            {
                "reg": ("obs", ["R", "R"]),
                "ins": ("obs", ["i", "i"]),
                "rd": ("obs", [1, 1]),
                "val": ("obs", [2.0, 3.0]),
                "fn": ("obs", ["f", "f"]),
                "ln": ("obs", [1, 2]),
            }
        )
        report = ds.groupby(manifest=manifest).deliver(archive=0).delivery.report
        assert report["status"].values.tolist() == ["no_qualifying_rounds", "ok"]
        assert report["total"].values[1] == 5.0
