from __future__ import annotations

import json
from pathlib import Path
from typing import Any
import numpy as np
import pandas as pd
import pytest

import xarray as xr
from xarray.point_handoff import (
    CellResolution,
    HandoffCatalog,
    HandoffRow,
    ReplyCatalog,
    attach,
    build_handoff,
    load_catalog,
    load_replies,
    open_point_handoff,
    register_point_handoff_accessor,
    save_handoff,
    save_replies,
)


@pytest.fixture(autouse=True, scope="module")
def _accessor() -> None:
    register_point_handoff_accessor()


def archive_grid() -> xr.Dataset:
    track = pd.MultiIndex.from_arrays(
        [["north", "south", "north"], ["A", "A", "B"]],
        names=["zone", "leg"],
    )
    dataset = xr.Dataset(
        {"reading": (("track", "lon"), np.full((3, 4), np.nan))},
        coords=xr.Coordinates.from_pandas_multiindex(track, "track"),
    )
    return dataset.assign_coords(
        lon=[100, 105, 105, 110],
        tide=("track", [0.4, 0.5, 0.6]),
    )


H = ("A", "A1", 1)
U_A = ("A", "A1", 2)
A3 = ("A", "A1", 3)
B_U = ("B", "B1", 3)
P = ("B", "B1", 1)
Q = ("B", "B1", 2)
B4 = ("B", "B1", 4)


def handoffs(dataset: xr.Dataset) -> tuple[HandoffCatalog, HandoffCatalog]:
    first = build_handoff(
        dataset, "A", "A1", [(0, 1), (0, 0), (0, 2)]
    )  # H, U, A3
    second = build_handoff(
        dataset, "B", "B1", [(2, 1), (2, 2), (0, 0), (2, 3)]
    )  # P, Q, U, B4
    return first, second


OFFSETS = {
    "batch-1": [(H, P, 2)],
    "batch-2": [(H, Q, 4)],
    "batch-3": [(H, B_U, 1)],
    "batch-4": [(B_U, Q, 4)],
}


def replies_with(u_absolute: bool = False) -> ReplyCatalog:
    absolutes: dict[str, dict[tuple[str, str, int], int]] = {"absolute-1": {H: 20}}
    if u_absolute:
        absolutes["absolute-2"] = {B_U: 21}
    return ReplyCatalog(
        absolutes=absolutes,
        statuses={"missing": {B4: "explicitly_missing"}},
        offsets=OFFSETS,
    )


def attached(u_absolute: bool = False) -> xr.Dataset:
    dataset = archive_grid()
    first, second, = handoffs(dataset)
    return attach(dataset, [first, second], replies_with(u_absolute))


def resolution(dataset: xr.Dataset, position: tuple[int, int]) -> CellResolution:
    return dataset.point_handoff.resolve(track=position[0], lon=position[1])


def test_selected_view_keeps_original_axes_and_tide() -> None:
    dataset = attached()
    view = dataset.sel(zone="north").isel(leg=1, lon=[3, 1, 0])

    assert tuple(view.dims) == ("lon",)
    assert view.lon.values.tolist() == [110, 105, 100]
    assert float(view.tide) == pytest.approx(0.6)
    np.testing.assert_array_equal(
        view["__point_handoff_position__track"].values, 2
    )
    np.testing.assert_array_equal(
        view["__point_handoff_position__lon"].values, [3, 1, 0]
    )


def test_selected_view_delivers_expected_states() -> None:
    dataset = attached()
    view = dataset.sel(zone="north").isel(leg=1, lon=[3, 1, 0])
    values = view.point_handoff.values()

    assert values.dims == ("lon",)
    assert np.isnan(values.values[0])
    assert values.values[1] == pytest.approx(22)
    assert np.isnan(values.values[2])

    statuses = view.point_handoff.status().values
    assert statuses.tolist() == [3, 0, 5]


def test_point_states_are_distinct() -> None:
    dataset = attached()

    assert resolution(dataset, (0, 1)).status == "value"
    assert resolution(dataset, (0, 1)).value_mm == 20
    assert resolution(dataset, (2, 1)).status == "value"
    assert resolution(dataset, (2, 1)).value_mm == 22
    assert resolution(dataset, (2, 3)).status == "explicitly_missing"
    assert resolution(dataset, (2, 0)).status == "never_handed_off"
    assert resolution(dataset, (0, 2)).status == "no_reply"
    assert resolution(dataset, (1, 0)).status == "never_handed_off"


def test_q_reports_conflict_with_both_values_and_provenance() -> None:
    dataset = attached()
    result = resolution(dataset, (2, 2))

    assert result.status == "conflict"
    assert result.value_mm is None
    assert result.conflict is not None
    assert set(result.conflict.values_mm) == {24, 25}
    assert result.position == (2, 2)
    assert {route.anchor for route in result.routes} == {H}
    blocks = {step.block for route in result.routes for step in route.steps}
    assert blocks == {"batch-2", "batch-3", "batch-4"}
    assert {step.from_position for route in result.routes for step in route.steps} >= {
        (2, 2)
    }
    assert all(
        route.absolute_mm == 20 and route.value_mm in {24, 25}
        for route in result.routes
    )
    assert set(result.blocks) == {
        "absolute-1",
        "batch-2",
        "batch-3",
        "batch-4",
    }


def test_p_delivers_while_q_conflicts() -> None:
    dataset = attached()
    p = resolution(dataset, (2, 1))
    q = resolution(dataset, (2, 2))

    assert (p.status, p.value_mm) == ("value", 22)
    assert q.status == "conflict"
    assert set(q.conflict.values_mm) == {24, 25}


def test_u_absolute_makes_p_conflict_between_22_and_23() -> None:
    dataset = attached(u_absolute=True)
    p = resolution(dataset, (2, 1))

    assert p.status == "conflict"
    assert set(p.conflict.values_mm) == {22, 23}
    for route in p.routes:
        assert route.anchor in {H, B_U}
        assert route.steps


def test_second_absolute_keeps_q_conflict() -> None:
    dataset = attached(u_absolute=True)
    q = resolution(dataset, (2, 2))

    assert q.status == "conflict"
    assert set(q.conflict.values_mm) == {24, 25}


def test_conflict_inside_one_inconsistent_block_is_not_hidden() -> None:
    dataset = archive_grid()
    first, second = handoffs(dataset)
    replies = ReplyCatalog(
        absolutes={"absolute-1": {H: 20}},
        offsets={
            "left": [(H, B_U, 1)],
            "right": [(B_U, Q, 4)],
            "direct": [(H, Q, 4)],
        },
    )
    dataset = attach(dataset, [first, second], replies)
    q = resolution(dataset, (2, 2))

    assert q.status == "conflict"
    assert set(q.conflict.values_mm) == {24, 25}
    assert q.conflict.first.anchor == H
    assert {route.absolute_mm for route in q.routes} == {20}


def test_pending_baseline_when_only_offsets_exist() -> None:
    dataset = archive_grid()
    first, second = handoffs(dataset)
    result = attach(
        dataset,
        [first, second],
        ReplyCatalog(
            offsets={
                "a": [(H, P, 2)],
                "b": [(P, Q, 2)],
            }
        ),
    )
    pending = resolution(result, (2, 2))

    assert pending.status == "pending_baseline"
    assert pending.value_mm is None
    assert "absolute" in (pending.reason or "")


def test_handoff_table_is_one_dimensional_and_keeps_original_positions() -> None:
    dataset = archive_grid()
    first, second = handoffs(dataset)

    assert first.table().shape[1] > 1
    assert first.table()["point"].tolist() == [1, 2, 3]
    assert first.table()["track_position"].tolist() == [0, 0, 0]
    assert first.table()["lon_position"].tolist() == [1, 0, 2]
    assert second.table()["lon_position"].tolist() == [1, 2, 0, 3]

    as_dict = first.to_dict()
    assert as_dict["axes"] == ["track", "lon"]
    assert as_dict["points"][0]["position"] == [0, 1]
    assert HandoffCatalog.from_dict(as_dict).table().equals(first.table())


def test_reply_directory_lists_availability_and_endpoints() -> None:
    catalog = replies_with().catalog()

    assert catalog["absolutes"] == [H]
    assert catalog["explicitly_missing"] == [B4]
    assert len(catalog["offset_endpoints"]) == 4
    for endpoint in catalog["offset_endpoints"]:
        assert set(endpoint) == {"block", "start_point", "end_point"}


def test_row_reordering_and_block_layout_do_not_change_results() -> None:
    dataset = archive_grid()
    first, second = handoffs(dataset)
    rows = [(H, P, 2), (H, Q, 4), (H, B_U, 1), (B_U, Q, 4)]
    layouts = [
        {"b1": [rows[0]], "b2": [rows[1]], "b3": [rows[2]], "b4": [rows[3]]},
        {"x1": [rows[3], rows[1]], "x2": [rows[2]], "x3": [rows[0]]},
        {"z": [rows[2], rows[3]], "y": [rows[1], rows[0]]},
    ]
    observations: list[list[tuple[str, int | None]]] = []

    for layout in layouts:
        replies = ReplyCatalog(
            absolutes={"absolute-1": {H: 20}},
            statuses={"missing": {B4: "explicitly_missing"}},
            offsets=layout,
        )
        dataset_with_replies = attach(dataset, [first, second], replies)
        observations.append(
            [
                (
                    result.status,
                    result.value_mm,
                )
                for result in (
                    resolution(dataset_with_replies, (0, 1)),
                    resolution(dataset_with_replies, (2, 1)),
                    resolution(dataset_with_replies, (2, 2)),
                )
            ]
        )

    assert observations[0] == observations[1] == observations[2]
    assert observations[0][0] == ("value", 20)
    assert observations[0][1] == ("value", 22)
    assert observations[0][2][0] == "conflict"


def test_crossing_routes_find_conflict_without_exponential_expansion() -> None:
    dataset = archive_grid()
    first = build_handoff(dataset, "A", "A1", [(0, 1), (0, 0)])
    rows: list[tuple[int, int]] = [(2, 1), (2, 2), (0, 0)]
    rows += [(2, index % 4) for index in range(3, 3 + 60)]
    second = build_handoff(dataset, "B", "B1", rows)
    # A ladder beside the conflicting H/U/Q block: a naive DFS over every
    # simple route would be exponential here.
    offsets = dict(OFFSETS)
    previous = P
    for index in range(3, 3 + 60):
        point = ("B", "B1", index)
        offsets[f"ladder-{index}"] = [(previous, point, 0)]
        previous = point
    offsets["ladder-close"] = [(previous, P, 0)]
    replies = ReplyCatalog(
        absolutes={"absolute-1": {H: 20}},
        statuses={"missing": {B4: "explicitly_missing"}},
        offsets=offsets,
    )
    dataset = attach(dataset, [first, second], replies)
    result = resolution(dataset, (2, 2))

    assert result.status == "conflict"
    assert result.conflict is not None
    assert len(set(result.conflict.values_mm)) == 2


def test_archive_save_and_reopen() -> None:
    dataset = archive_grid()
    first, second = handoffs(dataset)
    replies = replies_with()
    saved = attach(dataset, [first, second], replies)
    view = saved.sel(zone="north").isel(leg=1, lon=[3, 1, 0])
    expected = view.point_handoff.values().values.copy()

    root = Path(__file__).parent / "data" / "point_handoff_tmp"
    root.mkdir(exist_ok=True)
    try:
        save_handoff(first, root / "handoff-a.json")
        save_handoff(second, root / "handoff-b.json")
        save_replies(replies, root / "replies")
        reopened = open_point_handoff(
            dataset,
            handoff=[root / "handoff-a.json", root / "handoff-b.json"],
            replies=root / "replies",
        )
        reopened_view = reopened.sel(zone="north").isel(leg=1, lon=[3, 1, 0])
        np.testing.assert_array_equal(
            reopened_view.point_handoff.values().values, expected
        )
    finally:
        for path in sorted(root.glob("**/*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        root.rmdir()


def test_open_reads_no_numbers_until_values_are_requested() -> None:
    dataset = archive_grid()
    first, second = handoffs(dataset)
    replies = replies_with()
    root = Path(__file__).parent / "data" / "point_handoff_reads_tmp"
    root.mkdir(exist_ok=True)
    try:
        save_handoff(first, root / "handoff-a.json")
        save_handoff(second, root / "handoff-b.json")
        save_replies(replies, root / "replies")
        reopened = open_point_handoff(
            dataset,
            handoff=[root / "handoff-a.json", root / "handoff-b.json"],
            replies=root / "replies",
        )
        assert reopened.point_handoff.replies.read_blocks == ()
        catalog = reopened.point_handoff.catalog()
        assert catalog["replies"]["absolutes"] == [H]
        assert reopened.point_handoff.replies.read_blocks == ()

        view = reopened.sel(zone="north").isel(leg=1, lon=[3, 1, 0])
        values = view.point_handoff.values().values
        assert values[1] == pytest.approx(22)
        assert set(reopened.point_handoff.replies.read_blocks) == {
            "absolute-1",
            "batch-1",
        }
    finally:
        for path in sorted(root.glob("**/*"), reverse=True):
            path.unlink() if path.is_file() else path.rmdir()
        root.rmdir()


def test_shared_blocks_are_read_once_per_query(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset = archive_grid()
    first, second = handoffs(dataset)
    replies = replies_with()
    save_handoff(first, tmp_path / "handoff-a.json")
    save_handoff(second, tmp_path / "handoff-b.json")
    save_replies(replies, tmp_path / "replies")
    reopened = open_point_handoff(
        dataset,
        handoff=[tmp_path / "handoff-a.json", tmp_path / "handoff-b.json"],
        replies=tmp_path / "replies",
    )
    calls: list[str] = []
    original = xr.point_handoff.JsonFileBlock.read

    def counted(block: xr.point_handoff.JsonFileBlock) -> Any:
        calls.append(block.name)
        return original(block)

    monkeypatch.setattr(xr.point_handoff.JsonFileBlock, "read", counted)
    reopened.point_handoff.values().values

    assert len(calls) == len(set(calls))
    assert set(calls) == {"absolute-1", "batch-1", "batch-2", "batch-3", "batch-4"}


def test_stack_still_works_with_handoff_attached() -> None:
    dataset = attached()
    unique = dataset.assign_coords(lon=[100, 101, 102, 103]).reset_index("track")
    stacked = unique.stack(cell=("track", "lon"))
    assert stacked.sizes["cell"] == 12
    unstacked = stacked.unstack("cell")
    assert unstacked.sizes["track"] == 3
    assert unstacked.sizes["lon"] == 4


def test_archive_netcdf_roundtrip_then_reopen(tmp_path: Path) -> None:
    dataset = archive_grid()
    first, second = handoffs(dataset)
    saved = attach(dataset, [first, second], replies_with())
    path = tmp_path / "archive.nc"
    saved.reset_index("track").to_netcdf(path)

    with xr.open_dataset(path) as reopened:
        reopened = reopened.set_index(track=["zone", "leg"])
        save_handoff(first, tmp_path / "handoff-a.json")
        save_handoff(second, tmp_path / "handoff-b.json")
        save_replies(replies_with(), tmp_path / "replies")
        final = open_point_handoff(
            reopened,
            handoff=[tmp_path / "handoff-a.json", tmp_path / "handoff-b.json"],
            replies=tmp_path / "replies",
        )
        view = final.sel(zone="north").isel(leg=1, lon=[3, 1, 0])

        assert float(view.tide) == pytest.approx(0.6)
        np.testing.assert_array_equal(
            view.point_handoff.values().values, [np.nan, 22.0, np.nan]
        )


def _brute_force_value(
    adjacency: dict[tuple[int, int], list[tuple[tuple[int, int], int]]],
    start: tuple[int, int],
    anchor: tuple[int, int],
) -> set[int]:
    found: set[int] = set()

    def walk(node: tuple[int, int], seen: set[tuple[int, int]], total: int) -> None:
        if node == anchor:
            found.add(total)
            return
        for neighbour, offset in adjacency.get(node, ()):
            if neighbour in seen:
                continue
            seen.add(neighbour)
            walk(neighbour, seen, total + offset)
            seen.discard(neighbour)

    walk(start, {start}, 0)
    return found


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_random_offset_graphs_match_brute_force(seed: int) -> None:
    rng = __import__("random").Random(seed)
    dataset = archive_grid()
    point_numbers = list(range(3, 12))
    available = [
        (track_index, lon_index)
        for track_index in range(3)
        for lon_index in range(4)
        if (track_index, lon_index) != (0, 1)
    ]
    positions = available[: len(point_numbers)]
    point_positions = dict(zip(point_numbers, positions))
    point_positions[H[2]] = (0, 1)
    anchor_position = (0, 1)
    offsets: dict[
        str, list[tuple[tuple[str, str, int], tuple[str, str, int], int]]
    ] = {}
    adjacency: dict[tuple[int, int], list[tuple[tuple[int, int], int]]] = {}
    for index, point in enumerate(point_numbers):
        for _ in range(rng.randint(1, 3)):
            other = rng.choice(point_numbers)
            if other == point:
                continue
            value = rng.randint(-3, 3)
            offsets[f"edge-{index}-{other}-{rng.random()}"] = [
                (("B", "B1", point), ("B", "B1", other), value)
            ]
            adjacency.setdefault(point_positions[point], []).append(
                (point_positions[other], value)
            )
            adjacency.setdefault(point_positions[other], []).append(
                (point_positions[point], -value)
            )
    offsets["anchor"] = [(("B", "B1", point_numbers[0]), H, 1)]
    adjacency.setdefault(point_positions[point_numbers[0]], []).append(
        (anchor_position, 1)
    )
    adjacency.setdefault(anchor_position, []).append(
        (point_positions[point_numbers[0]], -1)
    )
    first = build_handoff(dataset, "A", "A1", [(0, 1)])
    second = HandoffCatalog(
        processor="B",
        batch="B1",
        rows=tuple(
            HandoffRow(point, position)
            for point, position in sorted(point_positions.items())
        ),
        axes=("track", "lon"),
    )
    replies = ReplyCatalog(
        absolutes={"absolute-1": {H: 20}},
        offsets=offsets,
    )
    dataset = attach(dataset, [first, second], replies)
    query = point_positions[point_numbers[0]]
    result = resolution(dataset, query)
    expected = {
        20 - value
        for value in _brute_force_value(adjacency, query, anchor_position)
    }

    if len(expected) == 1:
        assert result.status == "value"
        assert result.value_mm == next(iter(expected))
    else:
        assert result.status == "conflict"
        assert len(set(result.conflict.values_mm)) == 2
        assert set(result.conflict.values_mm) <= expected


def test_reported_conflict_routes_are_simple() -> None:
    # A dense block with several crossing cycles: every reported route must
    # visit each grid position at most once.
    rng = __import__("random").Random(2026)
    point_numbers = list(range(3, 15))
    point_positions = {point: (point, point) for point in point_numbers}
    point_positions[H[2]] = (0, 1)
    track = pd.MultiIndex.from_arrays(
        [["north"] * 20 + ["south"] * 20, ["A"] * 40], names=["zone", "leg"]
    )
    dataset = xr.Dataset(
        {"reading": (("track", "lon"), np.zeros((40, 20)))},
        coords=xr.Coordinates.from_pandas_multiindex(track, "track"),
    )
    offsets: dict[
        str, list[tuple[tuple[str, str, int], tuple[str, str, int], int]]
    ] = {}
    for index, point in enumerate(point_numbers):
        for _ in range(rng.randint(1, 5)):
            other = rng.choice(point_numbers)
            if other == point:
                continue
            offsets[f"edge-{index}-{other}-{rng.random()}"] = [
                (("B", "B1", point), ("B", "B1", other), rng.randint(-3, 3))
            ]
    offsets["anchor"] = [(("B", "B1", point_numbers[0]), H, 1)]
    dataset = attach(
        dataset,
        [
            build_handoff(dataset, "A", "A1", [(0, 1)]),
            HandoffCatalog(
                "B",
                "B1",
                tuple(
                    HandoffRow(point, position)
                    for point, position in sorted(point_positions.items())
                ),
                ("track", "lon"),
            ),
        ],
        ReplyCatalog(absolutes={"absolute-1": {H: 20}}, offsets=offsets),
    )

    for point in point_numbers:
        result = dataset.point_handoff.resolve(
            track=point_positions[point][0], lon=point_positions[point][1]
        )
        if result.status != "conflict":
            continue
        for route in result.routes:
            vertices = [route.steps[0].from_position]
            vertices += [step.to_position for step in route.steps]
            assert len(vertices) == len(set(vertices)), route


def test_plain_datasets_are_unchanged() -> None:
    dataset = archive_grid()

    assert "point_handoff" not in dataset.encoding
    assert dataset.sel(zone="north").sizes["lon"] == 4
    with pytest.raises(ValueError, match="handoff"):
        _ = dataset.point_handoff.values()


def test_handoff_roundtrip_preserves_explicit_positions() -> None:
    dataset = archive_grid()
    first, second = handoffs(dataset)
    assert first.table()["track_position"].tolist() == [0, 0, 0]
    assert second.table()["track_position"].tolist() == [2, 2, 0, 2]


def test_handoff_rows_can_be_built_directly() -> None:
    catalog = HandoffCatalog(
        processor="X",
        batch="x-1",
        rows=(HandoffRow(1, (0, 1)), HandoffRow(2, (2, 2))),
        axes=("track", "lon"),
    )
    dataset = attach(archive_grid(), [catalog], ReplyCatalog())
    assert dataset.point_handoff.handoff_table("X").shape[0] == 2


def test_same_point_number_in_different_batches_is_distinct() -> None:
    dataset = archive_grid()
    first = HandoffCatalog(
        processor="A",
        batch="one",
        rows=(HandoffRow(1, (0, 1)),),
        axes=("track", "lon"),
    )
    second = HandoffCatalog(
        processor="A",
        batch="two",
        rows=(HandoffRow(1, (2, 1)),),
        axes=("track", "lon"),
    )
    replies = ReplyCatalog(
        absolutes={"abs": {("A", "one", 1): 20}},
        offsets={},
    )
    attached_dataset = attach(dataset, [first, second], replies)

    assert resolution(attached_dataset, (0, 1)).value_mm == 20
    assert resolution(attached_dataset, (2, 1)).status == "no_reply"


def test_serialized_catalog_contains_no_numbers(tmp_path: Path) -> None:
    replies = replies_with()
    save_replies(replies, tmp_path / "replies", with_numbers=False)
    payload = json.loads((tmp_path / "replies" / "catalog.json").read_text())

    assert "value_mm" not in json.dumps(payload)
    assert "offset_mm" not in json.dumps(payload)
    assert payload["absolutes"]["absolute-1"] == [
        {"processor": "A", "batch": "A1", "point": 1}
    ]
    assert payload["explicitly_missing"] == [
        {"processor": "B", "batch": "B1", "point": 4}
    ]
    assert len(payload["offsets"]["batch-4"]) == 1


def test_metadata_only_directory_can_describe_availability(tmp_path: Path) -> None:
    replies = replies_with()
    save_replies(replies, tmp_path / "replies", with_numbers=False)
    catalog = load_catalog(tmp_path / "replies").catalog()

    assert catalog["absolutes"] == [H]
    assert catalog["explicitly_missing"] == [B4]
    assert len(catalog["offset_endpoints"]) == 4
