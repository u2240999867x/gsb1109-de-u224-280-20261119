from __future__ import annotations

from collections.abc import Hashable, Mapping

import numpy as np
import pytest

from xarray import (
    Dataset,
    SharedStrip,
    TileLayout,
    TileLayoutCombiner,
    TileLayoutError,
    TilePlacement,
    combine_by_coords,
    combine_tiled_by_coords,
)
from xarray.tests import has_dask

XS = np.arange(0.5, 40.0, 1.0)
YS = np.arange(0.5, 20.0, 1.0)


def _tile(
    values: np.ndarray,
    xs: np.ndarray,
    ys: np.ndarray,
    reverse_y: bool = False,
) -> Dataset:
    values = np.asarray(values, dtype=float)
    if reverse_y:
        ys = ys[::-1]
        values = values[::-1, :]
    return Dataset(
        {"obs": (("y", "x"), values)},
        coords={"x": np.array(xs), "y": np.array(ys)},
    )


def make_a() -> Dataset:
    xs = np.arange(0.5, 10.0, 1.0)
    ys = np.arange(10.5, 20.0, 1.0)
    return _tile(np.full((10, 10), 24.0), xs, ys, reverse_y=True)


def make_c(c_value: float = 20.0) -> Dataset:
    xs = np.arange(10.5, 20.0, 1.0)
    ys = np.arange(0.5, 10.0, 1.0)
    return _tile(np.full((10, 10), c_value), xs, ys)


def make_b(bc_strip: float = 22.0, bc_nan: bool = False) -> Dataset:
    # x=9.5 is the shared strip with A; y=9.5 is the shared strip with C.
    xs = np.arange(9.5, 20.0, 1.0)
    ys = np.arange(9.5, 20.0, 1.0)
    data = np.full((11, 11), 16.0)
    data[:, 0] = 22.0
    data[0, :] = bc_strip
    if bc_nan:
        data[0, :] = np.nan
    return _tile(data, xs, ys)


def make_d(dc_nan: bool = False) -> Dataset:
    # y=10.5 is the shared strip with A; x=10.5 is the shared strip with C.
    xs = np.arange(0.5, 11.0, 1.0)
    ys = np.arange(0.5, 11.0, 1.0)
    data = np.full((11, 11), 17.0)
    data[-1, :] = 21.0
    data[:, -1] = 21.0
    if dc_nan:
        data[:, -1] = np.nan
    return _tile(data, xs, ys)


def make_layout() -> TileLayout:
    return TileLayout(
        x_centers=XS,
        y_centers=YS,
        tiles={
            "A": TilePlacement((0, 0), (0.5, 9.5), (10.5, 19.5), y_reversed=True),
            "B": TilePlacement((0, 1), (10.5, 19.5), (10.5, 19.5)),
            "E": TilePlacement((0, 3), (30.5, 39.5), (10.5, 19.5)),
            "D": TilePlacement((1, 0), (0.5, 9.5), (0.5, 9.5)),
            "C": TilePlacement((1, 1), (10.5, 19.5), (0.5, 9.5)),
            "F": TilePlacement((1, 3), (30.5, 39.5), (0.5, 9.5)),
        },
        shared_strips=[
            SharedStrip(("A", "B"), "x"),
            SharedStrip(("B", "C"), "y"),
            SharedStrip(("A", "D"), "y"),
            SharedStrip(("D", "C"), "x"),
        ],
        baseline={"A": 0.0},
    )


def make_combiner() -> TileLayoutCombiner:
    layout = make_layout()
    return combine_tiled_by_coords(
        {"A": make_a(), "B": make_b(), "C": make_c(), "D": make_d()}, layout
    )


def test_layout_combiner_scenario() -> None:
    combiner = make_combiner()

    # Opening a window reads layout/coordinates only, never observations.
    assert combiner.value_reads == ()
    window_old = combiner.open_window(15.5, 4.5)
    assert combiner.value_reads == ()
    assert window_old.status == "ok"
    assert window_old.diagnostic.corrections == {
        "A": 0.0,
        "B": 2.0,
        "C": 4.0,
        "D": 3.0,
    }
    assert window_old.read()["obs"].values.item() == 24.0

    # New B version makes C five higher than B along their shared strip.
    combiner.replace_tiles({"B": make_b(bc_strip=25.0)})
    window_conflict = combiner.open_window(15.5, 4.5)
    assert window_conflict.status == "conflict"
    result_conflict = window_conflict.read()
    assert np.isnan(result_conflict["obs"].values.item())
    conflict = window_conflict.diagnostic.conflicts[0]
    assert conflict.tile == "C"
    assert conflict.corrections == (4.0, 7.0)
    assert conflict.via == ("D", "B")
    assert conflict.edges == (("D", "C"), ("B", "C"))
    assert conflict.tile_versions == (
        (("D", 0), ("C", 0)),
        (("B", 1), ("C", 0)),
    )

    # The earlier window keeps delivering the result computed at open time.
    assert window_old.status == "ok"
    assert window_old.read()["obs"].values.item() == 24.0

    # B whose B-C strip is entirely missing restores the D-anchored result.
    combiner.replace_tiles({"B": make_b(bc_nan=True)})
    window_recovered = combiner.open_window(15.5, 4.5)
    assert window_recovered.status == "ok"
    assert window_recovered.read()["obs"].values.item() == 24.0
    assert window_recovered.diagnostic.unusable_edges == (
        ("obs", ("B", "C")),
    )

    # Removing the D-C strip as well leaves C without any baseline.
    combiner.replace_tiles({"D": make_d(dc_nan=True)})
    window_no_baseline = combiner.open_window(15.5, 4.5)
    assert window_no_baseline.status == "no_baseline"
    assert "C" in window_no_baseline.diagnostic.no_baseline_tiles
    assert np.isnan(window_no_baseline.read()["obs"].values.item())

    # Every previously opened window remains frozen at its own result.
    assert window_old.read()["obs"].values.item() == 24.0
    assert np.isnan(window_conflict.read()["obs"].values.item())
    assert window_recovered.read()["obs"].values.item() == 24.0
    assert np.isnan(window_no_baseline.read()["obs"].values.item())
    assert (
        window_old.status,
        window_conflict.status,
        window_recovered.status,
        window_no_baseline.status,
    ) == ("ok", "conflict", "ok", "no_baseline")


def test_windows_are_versioned_snapshots() -> None:
    combiner = make_combiner()
    first = combiner.open_window(15.5, 4.5)
    assert first.read()["obs"].values.item() == 24.0
    combiner.replace_tiles({"B": make_b(bc_strip=25.0)})
    second = combiner.open_window(15.5, 4.5)
    assert second.status == "conflict"
    # Re-reading the old window does not pick up the new B version.
    assert first.read()["obs"].values.item() == 24.0
    assert combiner.tile_versions["B"] == 1


def test_edge_comparison_is_cached_per_tile_versions() -> None:
    combiner = make_combiner()
    window_one = combiner.open_window(15.5, 4.5)
    window_two = combiner.open_window(15.5, 4.5)
    window_one.read()
    strip_reads_after_first = [
        record
        for record in combiner.value_reads
        if max(len(record[3]), len(record[4])) > 1
    ]
    window_two.read()
    strip_reads_total = [
        record
        for record in combiner.value_reads
        if max(len(record[3]), len(record[4])) > 1
    ]
    # The second window reuses every cached strip comparison; only the
    # single-cell assembly read is repeated.
    assert strip_reads_total == strip_reads_after_first
    unique_strips = {
        (record[0], record[1], record[3], record[4])
        for record in strip_reads_total
    }
    assert len(unique_strips) == len(strip_reads_total)


def test_half_pixel_shift_is_rejected_and_keeps_old_version() -> None:
    combiner = make_combiner()
    shifted = make_b()
    shifted = shifted.assign_coords(x=shifted["x"].values + 0.5)
    with pytest.raises(TileLayoutError, match="B"):
        combiner.replace_tiles({"B": shifted})
    assert combiner.tile_versions["B"] == 0
    window = combiner.open_window(15.5, 4.5)
    assert window.status == "ok"
    assert window.read()["obs"].values.item() == 24.0


def test_extra_column_cannot_replace_missing_internal_column() -> None:
    combiner = make_combiner()
    broken = make_b()
    # Drop the first internal column (x=10.5) and keep an extra sampled column
    # outside the internal extent instead.
    broken = broken.sel(x=broken["x"].values != 10.5)
    with pytest.raises(TileLayoutError, match="missing internal x"):
        combiner.replace_tiles({"B": broken})
    assert combiner.tile_versions["B"] == 0


def test_reversed_y_axis_is_matched_by_centers() -> None:
    layout = make_layout()
    xs = np.arange(0.5, 10.0, 1.0)
    ys = np.arange(10.5, 20.0, 1.0)
    # Row value equals its whole-image y center; reversing storage must not
    # scramble the assembled rows.
    values = np.broadcast_to(ys[:, None], (10, 10)).copy()
    reversed_a = _tile(values, xs, ys, reverse_y=True)
    tiles = {"A": reversed_a, "B": make_b(), "C": make_c(), "D": make_d()}
    combiner = combine_tiled_by_coords(tiles, layout)
    top = combiner.open_window(5.5, 19.5).read()
    bottom = combiner.open_window(5.5, 10.5).read()
    assert top["obs"].values.item() == 19.5
    assert bottom["obs"].values.item() == 10.5
    raw = combiner.open_window(5.5, 19.5).read_raw()
    assert raw["obs"].values.item() == 19.5


def test_missing_tile_position_stays_empty() -> None:
    combiner = make_combiner()
    window = combiner.open_window(35.5, 15.5)
    result = window.read()
    assert np.isnan(result["obs"].values.item())
    assert result["x"].values.item() == 35.5
    assert result["y"].values.item() == 15.5


def test_internal_missing_readings_are_preserved() -> None:
    combiner = make_combiner()
    c = make_c()
    c["obs"].loc[{"x": 15.5, "y": 4.5}] = np.nan
    combiner.replace_tiles({"C": c})
    window = combiner.open_window(15.5, 4.5)
    assert window.status == "ok"
    assert np.isnan(window.read()["obs"].values.item())


def test_correction_only_applies_to_internal_readings() -> None:
    combiner = make_combiner()
    # x=9.5 is A's internal edge but only a shared strip for B.
    window = combiner.open_window(9.5, 15.5)
    result = window.read()
    assert result["obs"].values.item() == 24.0
    raw = window.read_raw()
    assert raw["obs"].values.item() == 24.0


def test_edge_needs_two_valid_pairs_with_identical_deltas() -> None:
    combiner = make_combiner()
    b = make_b()
    # Leave only one non-missing pair on the A-B strip.
    b["obs"].loc[{"x": 9.5, "y": slice(10.5, 18.5)}] = np.nan
    combiner.replace_tiles({"B": b})
    # With A-B unusable B is unanchored; C still reaches A through D.
    window = combiner.open_window(15.5, 4.5)
    assert window.status == "ok"
    assert ("obs", ("A", "B")) in window.diagnostic.unusable_edges
    # Breaking D-C as well strands C without any baseline path.
    combiner.replace_tiles({"D": make_d(dc_nan=True)})
    stranded = combiner.open_window(15.5, 4.5)
    assert stranded.status == "no_baseline"

    combiner_two = make_combiner()
    b_inconsistent = make_b()
    # y=15.5 lies inside the A-B overlap; perturb one pair's delta.
    b_inconsistent["obs"].loc[{"x": 9.5, "y": 15.5}] = (
        b_inconsistent["obs"].sel(x=9.5, y=15.5).values.item() + 1.0
    )
    combiner_two.replace_tiles({"B": b_inconsistent})
    window_two = combiner_two.open_window(15.5, 4.5)
    assert ("obs", ("A", "B")) in window_two.diagnostic.unusable_edges


def test_baseline_free_group_is_flagged() -> None:
    layout = make_layout()
    # A group of two tiles with no declared baseline.
    isolated_layout = TileLayout(
        x_centers=np.arange(0.5, 20.0, 1.0),
        y_centers=np.arange(0.5, 10.0, 1.0),
        tiles={
            "G": TilePlacement((0, 0), (0.5, 9.5), (0.5, 9.5)),
            "H": TilePlacement((0, 1), (10.5, 19.5), (0.5, 9.5)),
        },
        shared_strips=[SharedStrip(("G", "H"), "x")],
        baseline={},
    )
    g = _tile(
        np.full((10, 11), 5.0),
        np.arange(0.5, 11.0, 1.0),
        np.arange(0.5, 10.0, 1.0),
    )
    h = _tile(
        np.full((10, 10), 7.0),
        np.arange(10.5, 20.0, 1.0),
        np.arange(0.5, 10.0, 1.0),
    )
    combiner = combine_tiled_by_coords({"G": g, "H": h}, isolated_layout)
    window = combiner.open_window(15.5, 5.5)
    assert window.status == "no_baseline"
    assert set(window.diagnostic.no_baseline_tiles) == {"G", "H"}
    assert np.isnan(window.read()["obs"].values.item())


def test_window_selector_shapes() -> None:
    combiner = make_combiner()
    point = combiner.open_window(15.5, 4.5)
    assert point.x.tolist() == [15.5]
    assert point.y.tolist() == [4.5]

    region = combiner.open_window(slice(14.5, 16.5), slice(3.5, 5.5))
    assert region.x.tolist() == [14.5, 15.5, 16.5]
    assert region.y.tolist() == [3.5, 4.5, 5.5]
    result = region.read()
    assert result["obs"].shape == (3, 3)
    assert np.all(result["obs"].values == 24.0)

    listed = combiner.open_window([14.5, 16.5], [4.5])
    assert listed.x.tolist() == [14.5, 16.5]

    with pytest.raises(KeyError, match="pixel center"):
        combiner.open_window(15.0, 4.5)


def test_results_independent_of_input_order() -> None:
    layout = make_layout()
    tiles = {"A": make_a(), "B": make_b(), "C": make_c(), "D": make_d()}
    first = combine_tiled_by_coords(tiles, layout)
    reversed_order = dict(reversed(list(tiles.items())))
    second = combine_tiled_by_coords(reversed_order, layout)
    assert first.open_window(15.5, 4.5).read()["obs"].values.item() == (
        second.open_window(15.5, 4.5).read()["obs"].values.item()
    )
    assert first.open_window(15.5, 4.5).diagnostic.corrections == (
        second.open_window(15.5, 4.5).diagnostic.corrections
    )


def test_without_layout_falls_back_to_combine_by_coords() -> None:
    x1 = Dataset(
        {"obs": (("y", "x"), np.zeros((2, 2)))},
        coords={"x": [0.5, 1.5], "y": [0.5, 1.5]},
    )
    x2 = Dataset(
        {"obs": (("y", "x"), np.ones((2, 2)))},
        coords={"x": [2.5, 3.5], "y": [0.5, 1.5]},
    )
    result = combine_tiled_by_coords([x1, x2])
    expected = combine_by_coords([x1, x2])
    assert isinstance(result, Dataset)
    assert result.equals(expected)

    with pytest.raises(TypeError, match="mapping"):
        combine_tiled_by_coords([x1, x2], make_layout())


def test_unknown_tile_and_strip_validation() -> None:
    layout = make_layout()
    with pytest.raises(TileLayoutError, match="no placement"):
        combine_tiled_by_coords({"Z": make_c()}, layout)

    bad_strip_layout = TileLayout(
        x_centers=XS,
        y_centers=YS,
        tiles=layout.tiles,
        shared_strips=[SharedStrip(("A", "C"), "x")],
        baseline={"A": 0.0},
    )
    with pytest.raises(TileLayoutError, match="same row"):
        combine_tiled_by_coords(
            {"A": make_a(), "B": make_b(), "C": make_c(), "D": make_d()},
            bad_strip_layout,
        )


def test_variables_are_withheld_independently() -> None:
    def add_second(dataset: Dataset) -> Dataset:
        dataset = dataset.copy()
        # Constant across every tile, so every edge has zero correction.
        dataset["other"] = dataset["obs"] * 0.0 + 100.0
        return dataset

    layout = make_layout()
    tiles = {
        tile_id: add_second(dataset)
        for tile_id, dataset in {
            "A": make_a(),
            "B": make_b(),
            "C": make_c(),
            "D": make_d(),
        }.items()
    }
    combiner = combine_tiled_by_coords(tiles, layout)
    # Only "obs" becomes contradictory; "other" stays consistent.
    combiner.replace_tiles({"B": add_second(make_b(bc_strip=25.0))})

    window = combiner.open_window(15.5, 4.5)
    assert window.status == "conflict"
    result = window.read()
    assert np.isnan(result["obs"].values.item())
    conflict_variables = {item.variable for item in window.diagnostic.conflicts}
    assert conflict_variables == {"obs"}
    assert result["other"].values.item() == 100.0
    assert window.diagnostic.corrections["C"] == 0.0
    assert window.diagnostic.no_baseline_tiles == ()


def test_window_reads_only_selected_variable_strips() -> None:
    def add_second(dataset: Dataset) -> Dataset:
        dataset = dataset.copy()
        dataset["other"] = dataset["obs"] + 100.0
        return dataset

    layout = make_layout()
    combiner = combine_tiled_by_coords(
        {
            tile_id: add_second(dataset)
            for tile_id, dataset in {
                "A": make_a(),
                "B": make_b(),
                "C": make_c(),
                "D": make_d(),
            }.items()
        },
        layout,
    )
    combiner.open_window(15.5, 4.5, variables=["obs"]).read()
    read_variables = {record[2] for record in combiner.value_reads}
    assert read_variables == {"obs"}


def test_window_variable_selection_and_raw_reading() -> None:
    def add_second(dataset: Dataset) -> Dataset:
        dataset = dataset.copy()
        dataset["other"] = dataset["obs"] * 2.0
        return dataset

    layout = make_layout()
    tiles = {
        tile_id: add_second(dataset)
        for tile_id, dataset in {
            "A": make_a(),
            "B": make_b(),
            "C": make_c(),
            "D": make_d(),
        }.items()
    }
    combiner = combine_tiled_by_coords(tiles, layout)
    window = combiner.open_window(15.5, 4.5, variables=["obs"])
    result = window.read()
    assert set(result.data_vars) == {"obs"}
    assert result["obs"].values.item() == 24.0
    # Raw reading keeps C's uncorrected value.
    assert window.read_raw()["obs"].values.item() == 20.0
    with pytest.raises(KeyError, match="unknown spatial data variable"):
        combiner.open_window(15.5, 4.5, variables=["nope"]).read()


def test_branch_strips_outside_calibration_paths_are_not_read() -> None:
    class CountingCombiner(TileLayoutCombiner):
        def __init__(
            self, tiles: Mapping[Hashable, Dataset], layout: TileLayout
        ) -> None:
            self.loaded_tiles: set[Hashable] = set()
            super().__init__(tiles, layout)

        def _load_tile_region(
            self,
            tile_id: Hashable,
            version: int,
            variable: Hashable,
            global_x: np.ndarray,
            global_y: np.ndarray,
        ) -> object:
            self.loaded_tiles.add(tile_id)
            return super()._load_tile_region(
                tile_id, version, variable, global_x, global_y
            )

    # G hangs off D as a dead branch that cannot help calibrate C.
    layout = make_layout()
    branch_layout = TileLayout(
        x_centers=layout.x_centers,
        y_centers=np.arange(-9.5, 20.0, 1.0),
        tiles={
            **layout.tiles,
            "G": TilePlacement((2, 0), (0.5, 9.5), (-9.5, -0.5)),
        },
        shared_strips=[
            *layout.shared_strips,
            SharedStrip(("G", "D"), "y"),
        ],
        baseline={"A": 0.0},
    )
    g = _tile(
        np.full((10, 10), 99.0),
        np.arange(0.5, 10.0, 1.0),
        np.arange(-9.5, 0.0, 1.0),
    )
    combiner = CountingCombiner(
        {
            "A": make_a(),
            "B": make_b(),
            "C": make_c(),
            "D": make_d(),
            "G": g,
        },
        branch_layout,
    )
    combiner.open_window(15.5, 4.5).read()
    assert "G" not in combiner.loaded_tiles
    assert {"A", "B", "C", "D"} <= combiner.loaded_tiles


def test_rejected_replacement_is_atomic() -> None:
    combiner = make_combiner()
    good_c = make_c()
    shifted_b = make_b().assign_coords(x=make_b()["x"].values + 0.5)
    with pytest.raises(TileLayoutError):
        combiner.replace_tiles({"B": shifted_b, "C": good_c})
    assert combiner.tile_versions == {"A": 0, "B": 0, "C": 0, "D": 0}


@pytest.mark.skipif(not has_dask, reason="requires dask")
def test_chunked_tiles_give_identical_results() -> None:
    import dask.array as da

    layout = make_layout()
    tiles = {"A": make_a(), "B": make_b(), "C": make_c(), "D": make_d()}
    eager = combine_tiled_by_coords(tiles, layout)
    chunked = {
        tile_id: dataset.chunk({"x": 4, "y": 4})
        for tile_id, dataset in tiles.items()
    }
    assert isinstance(chunked["A"]["obs"].data, da.Array)
    lazy = combine_tiled_by_coords(chunked, layout)
    assert np.allclose(
        eager.open_window(slice(10.5, 19.5), slice(0.5, 9.5)).read()["obs"].values,
        lazy.open_window(slice(10.5, 19.5), slice(0.5, 9.5)).read()["obs"].values,
        equal_nan=True,
    )
    assert lazy.open_window(15.5, 4.5).diagnostic.corrections == (
        eager.open_window(15.5, 4.5).diagnostic.corrections
    )


def test_window_reads_only_necessary_tiles() -> None:
    class CountingCombiner(TileLayoutCombiner):
        def __init__(
            self, tiles: Mapping[Hashable, Dataset], layout: TileLayout
        ) -> None:
            self.loaded: set[tuple[object, object, object]] = set()
            super().__init__(tiles, layout)

        def _load_tile_region(
            self,
            tile_id: Hashable,
            version: int,
            variable: Hashable,
            global_x: np.ndarray,
            global_y: np.ndarray,
        ) -> object:
            self.loaded.add(
                (
                    tile_id,
                    variable,
                    (tuple(global_x.tolist()), tuple(global_y.tolist())),
                )
            )
            return super()._load_tile_region(
                tile_id, version, variable, global_x, global_y
            )

    combiner = CountingCombiner(
        {"A": make_a(), "B": make_b(), "C": make_c(), "D": make_d()}, make_layout()
    )
    combiner.open_window(15.5, 4.5).read()
    touched = {record[0] for record in combiner.loaded}
    # C carries the target pixel; A, B, D back the required strip comparisons.
    assert touched == {"A", "B", "C", "D"}
    # The E/F grid positions are absent and therefore never touched.
    assert all(tile_id not in touched for tile_id in ("E", "F"))
