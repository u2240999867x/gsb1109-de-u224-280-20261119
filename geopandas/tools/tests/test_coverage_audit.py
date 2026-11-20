"""Tests for the coverage_audit module."""

import numpy as np
import pandas as pd

from shapely.geometry import Polygon, box

from geopandas import GeoDataFrame, clip, coverage_audit

import pytest

CRS = "EPSG:6933"


@pytest.fixture
def delivery_masks():
    """Two overlapping delivery zones; the first one contains a hole."""
    zone_a = Polygon(
        [(0, 0), (8, 0), (8, 10), (0, 10)],
        holes=[[(3, 3), (3, 6), (6, 6), (6, 3)]],
    )
    zone_b = Polygon([(5, 0), (13, 0), (13, 8), (5, 8)])
    return GeoDataFrame(
        {"name": ["zone_a", "zone_b"]},
        geometry=[zone_a, zone_b],
        index=pd.Index(["zone_a", "zone_b"], name="mask_id"),
        crs=CRS,
    )


@pytest.fixture
def parcels():
    """Monitoring parcels, including duplicate index labels.

    ``thin`` crosses the overlap of both masks, ``corner`` only touches a
    corner of zone_b, ``in_hole`` lies fully inside the hole of zone_a
    (while its bounds intersect the zone_a bounds), ``full`` is fully inside
    zone_a, ``far`` does not intersect anything and the second ``thin``
    row reuses an existing index label.
    """
    thin = Polygon([(4.75, 1), (5.25, 1), (5.25, 9), (4.75, 9)])
    corner = Polygon([(13, 8), (15, 8), (15, 10), (13, 10)])
    in_hole = Polygon([(3.5, 3.5), (4.8, 3.5), (4.8, 4.8), (3.5, 4.8)])
    full = Polygon([(0.5, 0.5), (2.5, 0.5), (2.5, 2.5), (0.5, 2.5)])
    far = Polygon([(20, 20), (22, 20), (22, 22), (20, 22)])
    thin_overlap = Polygon([(6.5, 0.5), (7.5, 0.5), (7.5, 1.5), (6.5, 1.5)])
    index = pd.Index(
        ["thin", "corner", "in_hole", "full", "far", "thin"], name="parcel"
    )
    return GeoDataFrame(
        geometry=[thin, corner, in_hole, full, far, thin_overlap],
        index=index,
        crs=CRS,
    )


@pytest.fixture
def expected_audit():
    """Expected per-source (mask_id, relation, area) and union totals."""
    return [
        {
            "rows": [
                ("zone_a", "covered", 2.5),
                ("zone_b", "covered", 1.75),
            ],
            "covered_area": 3.25,
        },
        {
            "rows": [
                ("zone_a", "disjoint", 0.0),
                ("zone_b", "touch", 0.0),
            ],
            "covered_area": 0.0,
        },
        {
            "rows": [
                ("zone_a", "disjoint", 0.0),
                ("zone_b", "disjoint", 0.0),
            ],
            "covered_area": 0.0,
        },
        {
            "rows": [
                ("zone_a", "covered", 4.0),
                ("zone_b", "disjoint", 0.0),
            ],
            "covered_area": 4.0,
        },
        {
            "rows": [
                ("zone_a", "disjoint", 0.0),
                ("zone_b", "disjoint", 0.0),
            ],
            "covered_area": 0.0,
        },
        {
            "rows": [
                ("zone_a", "covered", 1.0),
                ("zone_b", "covered", 1.0),
            ],
            "covered_area": 1.0,
        },
    ]


def test_result_shapes_and_columns(parcels, delivery_masks):
    details, totals, clipped = coverage_audit(parcels, delivery_masks)

    assert len(details) == len(parcels) * len(delivery_masks)
    assert len(totals) == len(parcels)
    assert len(clipped) == 5

    expected_detail_cols = {
        "source_id",
        "mask_id",
        "relation",
        "area",
        "ratio",
        "source_area",
        "source_geometry",
        "mask_geometry",
        "clipped_geometry",
    }
    assert expected_detail_cols.issubset(details.columns)
    assert details.geometry.name == "clipped_geometry"

    expected_total_cols = {
        "source_id",
        "source_area",
        "covered_area",
        "covered_ratio",
        "intersects_any",
        "mask_ids",
        "touched_mask_ids",
        "source_geometry",
    }
    assert expected_total_cols.issubset(totals.columns)
    assert totals.geometry.name == "source_geometry"

    assert {"source_id", "mask_id", "clipped_geometry"}.issubset(clipped.columns)


def test_relations_areas_and_ratios(parcels, delivery_masks, expected_audit):
    details, totals, _ = coverage_audit(parcels, delivery_masks)
    n_masks = len(delivery_masks)
    source_areas = parcels.geometry.area.values

    for position, expected in enumerate(expected_audit):
        block = details.iloc[position * n_masks : (position + 1) * n_masks]
        actual = [(row.mask_id, row.relation, row.area) for row in block.itertuples()]
        for (exp_id, exp_rel, exp_area), (act_id, act_rel, act_area) in zip(
            expected["rows"], actual
        ):
            assert act_id == exp_id
            assert act_rel == exp_rel
            assert act_area == pytest.approx(exp_area)

        source_area = source_areas[position]
        for row in block.itertuples():
            assert row.ratio == pytest.approx(row.area / source_area)
            assert row.source_area == pytest.approx(source_area)

        total = totals.iloc[position]
        assert total["covered_area"] == pytest.approx(expected["covered_area"])
        assert total["covered_ratio"] == pytest.approx(
            expected["covered_area"] / source_area
        )
        assert total["intersects_any"] == (expected["covered_area"] > 0)


def test_overlap_counted_once_in_totals(parcels, delivery_masks):
    details, totals, _ = coverage_audit(parcels, delivery_masks)
    zone_a = delivery_masks.geometry.iloc[0]
    zone_b = delivery_masks.geometry.iloc[1]
    n_masks = len(delivery_masks)

    for position, source_geom in enumerate(parcels.geometry.values):
        block = details.iloc[position * n_masks : (position + 1) * n_masks]
        detail_sum = block["area"].sum()
        shared = source_geom.intersection(zone_a).intersection(zone_b).area

        assert detail_sum - totals.iloc[position]["covered_area"] == pytest.approx(
            shared
        )


def test_corner_touch_distinct_from_disjoint(parcels, delivery_masks):
    details, totals, _ = coverage_audit(parcels, delivery_masks)

    corner_rows = details[details["source_id"] == "corner"]
    assert set(corner_rows["relation"]) == {"touch", "disjoint"}
    touch_row = corner_rows[corner_rows["relation"] == "touch"].iloc[0]
    assert touch_row["mask_id"] == "zone_b"
    assert touch_row["area"] == 0.0
    assert touch_row["clipped_geometry"] is None

    corner_total = totals[totals["source_id"] == "corner"].iloc[0]
    assert corner_total["touched_mask_ids"] == ["zone_b"]
    assert corner_total["mask_ids"] == []
    assert not corner_total["intersects_any"]


def test_hole_parcel_not_flagged_by_bounds(parcels, delivery_masks):
    in_hole = parcels.loc["in_hole"].iloc[0]
    zone_a = delivery_masks.loc["zone_a"].geometry
    parcel_bounds = in_hole.bounds
    mask_bounds = zone_a.bounds
    # The bounding boxes do overlap, which is exactly the trap to avoid.
    assert (
        parcel_bounds[0] < mask_bounds[2]
        and parcel_bounds[2] > mask_bounds[0]
        and parcel_bounds[1] < mask_bounds[3]
        and parcel_bounds[3] > mask_bounds[1]
    )

    details, totals, clipped = coverage_audit(parcels, delivery_masks)
    hole_rows = details[details["source_id"] == "in_hole"]
    assert (hole_rows["relation"] == "disjoint").all()
    assert (hole_rows["area"] == 0.0).all()

    hole_total = totals[totals["source_id"] == "in_hole"].iloc[0]
    assert not hole_total["intersects_any"]
    assert hole_total["covered_area"] == 0.0
    assert "in_hole" not in set(clipped["source_id"])


def test_thin_parcel_keeps_positive_area(parcels, delivery_masks):
    details, totals, clipped = coverage_audit(parcels, delivery_masks)
    thin_rows = details[details["source_id"] == "thin"]

    covered = thin_rows[thin_rows["relation"] == "covered"]
    assert len(covered) == 4  # two source rows times two masks
    assert (covered["area"] > 0).all()

    thin_totals = totals[totals["source_id"] == "thin"]
    assert thin_totals["intersects_any"].all()
    assert set(thin_totals["mask_ids"].iloc[0]) == {"zone_a", "zone_b"}

    assert (clipped.geometry.area > 0).all()


def test_duplicate_index_labels_are_kept(parcels, delivery_masks):
    details, totals, clipped = coverage_audit(parcels, delivery_masks)

    assert (details["source_id"] == "thin").sum() == 4
    assert len(totals) == len(parcels)
    assert totals["source_id"].tolist() == list(parcels.index)

    thin_totals = totals[totals["source_id"] == "thin"]
    assert thin_totals["covered_area"].tolist() == pytest.approx([3.25, 1.0])
    assert (clipped[clipped["source_id"] == "thin"]).shape[0] == 4


def test_geometry_provenance(parcels, delivery_masks):
    details, totals, clipped = coverage_audit(parcels, delivery_masks)
    n_masks = len(delivery_masks)

    for position, source_geom in enumerate(parcels.geometry.values):
        rows = details.iloc[position * n_masks : (position + 1) * n_masks]
        for row in rows.itertuples():
            assert row.source_id == parcels.index[position]
            assert row.source_geometry.equals(source_geom)

        assert totals.iloc[position]["source_geometry"].equals(source_geom)

    for mask_id, mask_geom in delivery_masks.geometry.items():
        rows = details[details["mask_id"] == mask_id]
        assert all(geom.equals(mask_geom) for geom in rows["mask_geometry"])

    covered = details[details["relation"] == "covered"]
    for row in covered.itertuples():
        expected = row.source_geometry.intersection(row.mask_geometry)
        assert row.clipped_geometry.equals(expected)

    for row in clipped.itertuples():
        detail_match = details[
            (details["source_id"] == row.source_id)
            & (details["mask_id"] == row.mask_id)
            & (details["relation"] == "covered")
        ]
        assert any(
            geom.equals(row.clipped_geometry)
            for geom in detail_match["clipped_geometry"]
        )


def test_mask_order_swap_gives_same_semantics(parcels, delivery_masks):
    forward, totals_f, clipped_f = coverage_audit(parcels, delivery_masks)
    swapped_masks = delivery_masks.iloc[::-1]
    backward, totals_b, clipped_b = coverage_audit(parcels, swapped_masks)

    assert forward["mask_id"].tolist()[:2] == ["zone_a", "zone_b"]
    assert backward["mask_id"].tolist()[:2] == ["zone_b", "zone_a"]

    key_f = forward.assign(
        source_position=np.arange(len(forward)) // len(delivery_masks)
    ).sort_values(["source_position", "mask_id"])
    key_b = backward.assign(
        source_position=np.arange(len(backward)) // len(delivery_masks)
    ).sort_values(["source_position", "mask_id"])

    assert key_f["relation"].tolist() == key_b["relation"].tolist()
    assert key_f["area"].tolist() == pytest.approx(key_b["area"].tolist())
    assert key_f["ratio"].tolist() == pytest.approx(key_b["ratio"].tolist())

    pd.testing.assert_series_equal(
        totals_f["covered_area"].reset_index(drop=True),
        totals_b["covered_area"].reset_index(drop=True),
        check_names=False,
    )
    assert totals_f["covered_ratio"].tolist() == pytest.approx(
        totals_b["covered_ratio"].tolist()
    )
    for ids_f, ids_b in zip(totals_f["mask_ids"], totals_b["mask_ids"]):
        assert set(ids_f) == set(ids_b)

    assert sorted(clipped_f.geometry.area) == pytest.approx(
        sorted(clipped_b.geometry.area)
    )


def test_rectangle_bounds_and_polygon_equivalent():
    rectangle = box(2, 2, 12, 12)
    inside = Polygon([(3, 3), (5, 3), (5, 5), (3, 5)])
    straddle = Polygon([(10, 10), (13, 10), (13, 13), (10, 13)])
    corner = Polygon([(12, 12), (14, 12), (14, 14), (12, 14)])
    away = Polygon([(20, 20), (22, 20), (22, 22), (20, 22)])
    parcels = GeoDataFrame(
        geometry=[inside, straddle, corner, away],
        index=pd.Index(["inside", "straddle", "corner", "away"], name="parcel"),
        crs=CRS,
    )

    variants = {
        "polygon": rectangle,
        "tuple_bounds": (2.0, 2.0, 12.0, 12.0),
        "list_bounds": [2.0, 2.0, 12.0, 12.0],
    }
    reference = coverage_audit(parcels, variants["polygon"])

    for label, mask in variants.items():
        details, totals, clipped = coverage_audit(parcels, mask)
        ref_details, ref_totals, ref_clipped = reference

        assert details["relation"].tolist() == ref_details["relation"].tolist()
        assert details["area"].tolist() == pytest.approx(ref_details["area"].tolist())
        assert details["ratio"].tolist() == pytest.approx(ref_details["ratio"].tolist())
        assert totals["covered_area"].tolist() == pytest.approx(
            ref_totals["covered_area"].tolist()
        )
        assert totals["covered_ratio"].tolist() == pytest.approx(
            ref_totals["covered_ratio"].tolist()
        )
        assert sorted(clipped.geometry.area) == pytest.approx(
            sorted(ref_clipped.geometry.area)
        )
        assert details["mask_id"].tolist() == [0] * len(parcels)

    details, _, _ = coverage_audit(parcels, rectangle)
    assert details.loc[details["source_id"] == "corner", "relation"].iloc[0] == "touch"
    assert details.loc[details["source_id"] == "away", "relation"].iloc[0] == (
        "disjoint"
    )
    straddle_row = details[details["source_id"] == "straddle"].iloc[0]
    assert straddle_row["area"] == pytest.approx(4.0)
    assert straddle_row["ratio"] == pytest.approx(4.0 / 9.0)


def test_geoseries_input(parcels, delivery_masks):
    details, totals, clipped = coverage_audit(parcels.geometry, delivery_masks)
    assert len(details) == len(parcels) * len(delivery_masks)
    assert len(totals) == len(parcels)
    assert totals.geometry.name == "source_geometry"
    assert details.crs == parcels.crs
    assert clipped.crs == parcels.crs


def test_mixed_mask_list_gets_integer_ids(parcels):
    zone_a = Polygon(
        [(0, 0), (8, 0), (8, 10), (0, 10)],
        holes=[[(3, 3), (3, 6), (6, 6), (6, 3)]],
    )
    details, _, _ = coverage_audit(parcels, [zone_a, (5.0, 0.0, 13.0, 8.0)])

    assert sorted(details["mask_id"].unique()) == [0, 1]
    full_rows = details[(details["source_id"] == "full") & (details["mask_id"] == 0)]
    assert full_rows["relation"].iloc[0] == "covered"
    assert full_rows["area"].iloc[0] == pytest.approx(4.0)


def test_crs_mismatch_warns(parcels, delivery_masks):
    reprojected = delivery_masks.to_crs("EPSG:3857")
    with pytest.warns(UserWarning, match="CRS mismatch"):
        coverage_audit(parcels, reprojected)


def test_invalid_inputs(parcels):
    with pytest.raises(TypeError):
        coverage_audit("not-a-gdf", parcels)
    with pytest.raises(TypeError):
        coverage_audit(parcels, "not-a-mask")
    with pytest.raises(TypeError):
        coverage_audit(parcels, [(1, 2, 3)])
    with pytest.raises(ValueError):
        coverage_audit(parcels, [])


def test_empty_input(parcels, delivery_masks):
    empty = parcels.iloc[:0]
    details, totals, clipped = coverage_audit(empty, delivery_masks)

    assert details.empty
    assert totals.empty
    assert clipped.empty
    assert details.geometry.name == "clipped_geometry"
    assert totals.geometry.name == "source_geometry"
    assert details.crs == parcels.crs
    assert totals.crs == parcels.crs
    assert clipped.crs == parcels.crs


def test_plain_clip_return_form_unchanged(parcels, delivery_masks):
    clipped = clip(parcels, delivery_masks)
    assert isinstance(clipped, GeoDataFrame)
    assert clipped.crs == parcels.crs
    assert "thin" in clipped.index


def test_disjoint_and_touch_rows_all_present(parcels, delivery_masks):
    details, _, _ = coverage_audit(parcels, delivery_masks)
    counts = details["relation"].value_counts()
    assert counts["covered"] == 5
    assert counts["touch"] == 1
    assert counts["disjoint"] == 6
