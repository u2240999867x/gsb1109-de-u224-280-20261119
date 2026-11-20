"""Tests for the reconcilable line intersection views.

Scenario mirrored from the inspection-network use case:

* left route A meets right route B in two disconnected places: an isolated
  point touch at (1, 0) and a coincident segment from x=2 to x=6;
* right route C only covers part of that coincident segment (x=3 to x=5) and
  adds its own point crossing at x=2.5, which sits on A/B's coincident line;
* left route D has no intersection at all and must produce no component;
* index labels are duplicated on both sides, so positional row numbers are
  the stable source key together with the labels.
"""

import numpy as np
import pandas as pd

from shapely import equals, get_dimensions, union_all
from shapely.geometry import LineString, MultiLineString, Point

import geopandas
from geopandas import GeoDataFrame
from geopandas.tools.line_intersection import (
    LEFT_IDX,
    LEFT_ROW,
    ON_OTHER_COINCIDENT,
    RIGHT_IDX,
    RIGHT_ROW,
    line_intersection_views,
)

import pytest


def _networks(crs=None):
    left = GeoDataFrame(
        {"route": ["A", "D"], "left_attr": ["la", "ld"]},
        geometry=[
            LineString([(0, 0), (8, 0)]),
            LineString([(20, 20), (22, 20)]),
        ],
        index=["R1", "R1"],
        crs=crs,
    )
    right = GeoDataFrame(
        {"insp": ["B", "C"], "right_attr": ["rb", "rc"]},
        geometry=[
            LineString([(2, 0), (6, 0), (6, 5), (1, 5), (1, 0)]),
            LineString([(3, 0), (5, 0), (5, 3), (2.5, 3), (2.5, 0), (2.5, -3)]),
        ],
        index=["I1", "I1"],
        crs=crs,
    )
    return left, right


def test_views_keep_two_separated_components_per_pair():
    left, right = _networks()
    views = line_intersection_views(left, right)

    # Pair A/B: isolated point event plus a length-4 coincident segment.
    ab = views.pair_reconciliation().query("left_row == 0 and right_row == 0")
    assert ab["attributed_line_length"].iloc[0] == pytest.approx(4.0)
    assert ab["event_count"].iloc[0] == 1

    # Pair A/C: own point crossing plus a length-2 partial overlap.
    ac = views.pair_reconciliation().query("left_row == 0 and right_row == 1")
    assert ac["attributed_line_length"].iloc[0] == pytest.approx(2.0)
    assert ac["event_count"].iloc[0] == 1


def test_segments_are_atomic_and_shared_piece_keeps_every_source_pair():
    left, right = _networks()
    segments = line_intersection_views(left, right).segments

    # Coincident corridor x=2..6 is noded at 2.5, 3 and 5 -> four pieces.
    assert segments["segment_id"].nunique() == 4
    np.testing.assert_allclose(
        segments.drop_duplicates("segment_id")["segment_length"],
        [0.5, 0.5, 2.0, 1.0],
    )

    # The x=3..5 piece is physically shared by both source pairs and emitted
    # once per pair, while its physical length is only counted once.
    shared = segments[segments["shared_segment"]]
    assert len(shared) == 2
    assert shared["segment_id"].nunique() == 1
    assert set(shared["source_pair_count"]) == {2}
    assert shared["segment_length"].iloc[0] == pytest.approx(2.0)
    covering_pairs = set(zip(shared[LEFT_ROW], shared[RIGHT_ROW]))
    assert covering_pairs == {(0, 0), (0, 1)}

    # Per-source-pair claims (6.0, shared piece counted for each pair) are
    # kept separate from the de-duplicated physical length (4.0).
    assert segments["segment_length"].sum() == pytest.approx(6.0)


def test_unique_patrol_length_is_deduplicated_per_route():
    left, right = _networks()
    views = line_intersection_views(left, right)

    left_lengths = views.unique_length_by_route("left")
    # Route A patrols x=2..6 once; route D never appears (no intersection).
    assert len(left_lengths) == 1
    assert left_lengths.iloc[0][[LEFT_ROW, LEFT_IDX]].tolist() == [0, "R1"]
    assert left_lengths["patrol_length"].iloc[0] == pytest.approx(4.0)

    right_lengths = views.unique_length_by_route("right").sort_values(RIGHT_ROW)
    np.testing.assert_allclose(right_lengths["patrol_length"], [4.0, 2.0])
    assert right_lengths[RIGHT_IDX].tolist() == ["I1", "I1"]


def test_events_keep_owner_and_flag_other_pairs_coincident_segment():
    left, right = _networks()
    events = line_intersection_views(left, right).events

    assert len(events) == 2
    by_pair = {(row[LEFT_ROW], row[RIGHT_ROW]): row for _, row in events.iterrows()}

    isolated = by_pair[(0, 0)]
    assert equals(isolated.geometry, Point(1, 0))
    assert bool(isolated[ON_OTHER_COINCIDENT]) is False

    crossing = by_pair[(0, 1)]
    assert equals(crossing.geometry, Point(2.5, 0))
    # It remains a point event of pair A/C even though it lies on A/B's line.
    assert bool(crossing[ON_OTHER_COINCIDENT]) is True


def test_provenance_labels_and_attributes_are_retained():
    left, right = _networks(crs=32633)
    views = line_intersection_views(left, right)

    for frame in (views.segments, views.events):
        assert frame.crs == left.crs
        assert {LEFT_ROW, RIGHT_ROW, LEFT_IDX, RIGHT_IDX} <= set(frame.columns)
        assert {"route_1", "left_attr_1", "insp_2", "right_attr_2"} <= set(
            frame.columns
        )
        # Duplicated index labels survive untouched next to positional keys.
        assert set(frame[LEFT_IDX]) == {"R1"}
        assert set(frame[RIGHT_IDX]) == {"I1"}

    ab_segments = views.segments.query("left_row == 0 and right_row == 0")
    assert set(ab_segments["route_1"]) == {"A"}
    assert set(ab_segments["insp_2"]) == {"B"}
    assert set(ab_segments["left_attr_1"]) == {"la"}
    assert set(ab_segments["right_attr_2"]) == {"rb"}


def test_non_intersecting_route_has_no_components():
    left, right = _networks()
    views = line_intersection_views(left, right)

    # Route D is positional row 1 on the left: no segment, no event, no row.
    assert not (views.segments[LEFT_ROW] == 1).any()
    assert not (views.events[LEFT_ROW] == 1).any()
    assert not (views.pair_reconciliation()[LEFT_ROW] == 1).any()


def test_fully_disjoint_inputs_return_empty_views():
    left, _ = _networks(crs=32633)
    far = GeoDataFrame(
        {"insp": ["X"]},
        geometry=[LineString([(100, 100), (101, 100)])],
        crs=32633,
    )
    views = line_intersection_views(left, far)

    assert views.segments.empty and views.events.empty
    assert views.segments.crs == left.crs and views.events.crs == left.crs
    assert views.pair_reconciliation().empty
    assert views.unique_length_by_route().empty


def test_point_only_pair_has_event_but_no_segment():
    horizontal = GeoDataFrame({"a": [1]}, geometry=[LineString([(0, 0), (10, 0)])])
    vertical = GeoDataFrame({"b": [2]}, geometry=[LineString([(5, -5), (5, 5)])])
    views = line_intersection_views(horizontal, vertical)

    assert views.segments.empty
    assert len(views.events) == 1
    assert equals(views.events.geometry.iloc[0], Point(5, 0))
    reconciliation = views.pair_reconciliation().iloc[0]
    assert reconciliation["attributed_line_length"] == pytest.approx(0.0)
    assert reconciliation["event_count"] == 1


def test_row_reordering_reconciles_through_positional_source_keys():
    left, right = _networks()
    baseline = line_intersection_views(left, right)

    reordered_left = left.iloc[[1, 0]]  # D -> row 0, A -> row 1
    reordered_right = right.iloc[[1, 0]]  # C -> row 0, B -> row 1
    reordered = line_intersection_views(reordered_left, reordered_right)

    # Same physical pieces and point events regardless of row order.
    base_pieces = union_all(
        baseline.segments.drop_duplicates("segment_id").geometry.values
    )
    new_pieces = union_all(
        reordered.segments.drop_duplicates("segment_id").geometry.values
    )
    assert equals(base_pieces, new_pieces)
    assert {(geom.x, geom.y) for geom in baseline.events.geometry} == {
        (geom.x, geom.y) for geom in reordered.events.geometry
    }

    # Remap reordered positional keys back onto the original rows.
    remapped = reordered.pair_reconciliation().replace(
        {LEFT_ROW: {1: 0}, RIGHT_ROW: {0: 1, 1: 0}}
    )
    remapped = remapped.sort_values([LEFT_ROW, RIGHT_ROW]).reset_index(drop=True)
    expected = (
        baseline.pair_reconciliation()
        .sort_values([LEFT_ROW, RIGHT_ROW])
        .reset_index(drop=True)
    )
    pd.testing.assert_frame_equal(remapped, expected)


def test_swapping_sides_mirrors_keys_but_keeps_physical_results():
    left, right = _networks()
    forward = line_intersection_views(left, right)
    reverse = line_intersection_views(right, left)

    forward_pieces = union_all(
        forward.segments.drop_duplicates("segment_id").geometry.values
    )
    reverse_pieces = union_all(
        reverse.segments.drop_duplicates("segment_id").geometry.values
    )
    assert equals(forward_pieces, reverse_pieces)

    forward_pairs = set(
        zip(
            forward.segments[LEFT_ROW],
            forward.segments[RIGHT_ROW],
        )
    )
    mirrored_pairs = set(
        zip(
            reverse.segments[RIGHT_ROW],
            reverse.segments[LEFT_ROW],
        )
    )
    assert forward_pairs == mirrored_pairs

    assert {(geom.x, geom.y) for geom in forward.events.geometry} == {
        (geom.x, geom.y) for geom in reverse.events.geometry
    }


def test_views_reconcile_back_to_raw_pairwise_intersections():
    left, right = _networks()
    views = line_intersection_views(left, right)

    idx1, idx2 = right.sindex.query(left.geometry, predicate="intersects", sort=True)
    reconciliation = views.pair_reconciliation().set_index([LEFT_ROW, RIGHT_ROW])
    for left_row, right_row in zip(idx1, idx2):
        raw = left.geometry.iloc[left_row].intersection(right.geometry.iloc[right_row])
        raw_parts = list(getattr(raw, "geoms", [raw]))
        line_length = sum(
            geom.length for geom in raw_parts if get_dimensions(geom) == 1
        )
        point_count = sum(1 for geom in raw_parts if get_dimensions(geom) == 0)
        key = (int(left_row), int(right_row))
        assert reconciliation.loc[key, "attributed_line_length"] == (
            pytest.approx(line_length)
        )
        assert reconciliation.loc[key, "event_count"] == point_count

        # The segment rows of this pair union back to its raw line part.
        pair_segments = views.segments.query(
            "left_row == @left_row and right_row == @right_row"
        )
        assert equals(
            union_all(pair_segments.geometry.values),
            union_all([geom for geom in raw_parts if get_dimensions(geom) == 1]),
        )


def test_default_overlay_call_is_not_modified():
    left, right = _networks()
    # The default intersection still drops point parts with a warning and
    # returns one line row per intersecting source pair.
    with pytest.warns(UserWarning, match="keep_geom_type=True"):
        result = geopandas.overlay(left, right, how="intersection")
    assert len(result) == 2
    assert set(result.geom_type) == {"LineString"}


def test_non_line_input_raises():
    lines = GeoDataFrame(geometry=[LineString([(0, 0), (1, 0)])])
    points = GeoDataFrame(geometry=[Point(0, 0)])
    with pytest.raises(TypeError, match="only line geometries"):
        line_intersection_views(lines, points)


def test_multiline_inputs_are_supported():
    multi = GeoDataFrame(
        {"a": [1]},
        geometry=[MultiLineString([[(0, 0), (4, 0)], [(10, 0), (12, 0)]])],
    )
    single = GeoDataFrame({"b": [2]}, geometry=[LineString([(1, 0), (11, 0)])])
    views = line_intersection_views(multi, single)

    np.testing.assert_allclose(views.unique_length_by_route()["patrol_length"], [4.0])
    np.testing.assert_allclose(
        views.unique_length_by_route("right")["patrol_length"], [4.0]
    )
    assert views.events.empty
