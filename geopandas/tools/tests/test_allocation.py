import numpy as np
import pandas as pd

from shapely import area, union_all
from shapely.geometry import GeometryCollection, LineString, box

import geopandas
from geopandas import GeoDataFrame
from geopandas.tools import PopulationAllocation, allocate_population

import pytest

CRS = 6933


def _two_communities():
    # Community A: A1 has the highest priority but no population; A2 lies only
    # partially under A1; A3 is the broad, lowest-priority parcel with an
    # exclusive area.  Community B has the same shapes but different
    # populations and priorities.
    rows = [
        ("A", "A1", 1, np.nan, box(0, 0, 12, 12)),
        ("A", "A2", 2, 960.0, box(8, 0, 20, 8)),
        ("A", "A3", 3, 4000.0, box(4, 4, 24, 24)),
        ("A", "A8", 4, 500.0, box(1, 1, 2, 2)),
        ("B", "B1", 3, 1440.0, box(0, 0, 12, 12)),
        ("B", "B2", 1, np.nan, box(8, 0, 20, 8)),
        ("B", "B3", 2, 2000.0, box(4, 4, 24, 24)),
        ("A", "A9", 1, 100.0, box(0, 0, 0, 0)),
        ("A", "A10", 1, 100.0, LineString([(0, 0), (1, 1)])),
        ("B", "B9", 1, 100.0, GeometryCollection()),
    ]
    gdf = GeoDataFrame(
        {
            "community": [r[0] for r in rows],
            "parcel": [r[1] for r in rows],
            "priority": [r[2] for r in rows],
            "population": [r[3] for r in rows],
            "geometry": [r[4] for r in rows],
        },
        crs=CRS,
    )
    # a duplicated index label must not confuse source tracking
    gdf.index = list(range(len(gdf) - 1)) + [2]
    return gdf


@pytest.fixture
def parcels():
    return _two_communities()


@pytest.fixture
def result(parcels):
    return allocate_population(
        parcels,
        by="community",
        population="population",
        priority="priority",
        business_id="parcel",
    )


def test_returns_population_allocation(result):
    assert isinstance(result, PopulationAllocation)
    for frame in (result.communities, result.allocations, result.unallocated):
        assert isinstance(frame, GeoDataFrame)
        assert frame.crs.to_epsg() == CRS


def test_community_summary_areas_and_population(result):
    community_a = result.communities.loc["A"]
    assert community_a.total_area == pytest.approx(512.0)
    assert community_a.known_area == pytest.approx(368.0)
    assert community_a.unknown_area == pytest.approx(144.0)
    assert community_a.known_area + community_a.unknown_area == pytest.approx(
        community_a.total_area
    )
    assert community_a.estimated_population == pytest.approx(3680.0)
    assert community_a.n_inputs == 6
    assert community_a.n_allocated == 3
    assert community_a.n_unallocated == 3

    community_b = result.communities.loc["B"]
    assert community_b.total_area == pytest.approx(512.0)
    assert community_b.known_area == pytest.approx(416.0)
    assert community_b.unknown_area == pytest.approx(96.0)
    assert community_b.estimated_population == pytest.approx(2400.0)


def test_priority_owns_overlap_even_without_population(result):
    alloc_a = result.allocations.loc["A"].set_index("parcel")
    # highest-priority A1 keeps the whole overlap although its population is
    # missing; its area is booked as unknown
    assert alloc_a.loc["A1", "allocated_area"] == pytest.approx(144.0)
    assert not alloc_a.loc["A1", "has_population"]
    assert np.isnan(alloc_a.loc["A1", "estimated_population"])
    # A2 only keeps the part not covered by A1
    assert alloc_a.loc["A2", "allocated_area"] == pytest.approx(64.0)
    assert alloc_a.loc["A2", "estimated_population"] == pytest.approx(640.0)
    # A3 keeps its exclusive remainder after both higher-priority parcels
    assert alloc_a.loc["A3", "allocated_area"] == pytest.approx(304.0)
    assert alloc_a.loc["A3", "estimated_population"] == pytest.approx(3040.0)
    assert alloc_a.loc["A3", "allocation_share"] == pytest.approx(304.0 / 400.0)


def test_second_community_adjudication_independent(result):
    alloc_b = result.allocations.loc["B"].set_index("parcel")
    # B2 wins by priority here, again without a population value
    assert alloc_b.loc["B2", "allocated_area"] == pytest.approx(96.0)
    assert not alloc_b.loc["B2", "has_population"]
    assert alloc_b.loc["B3", "allocated_area"] == pytest.approx(352.0)
    assert alloc_b.loc["B3", "estimated_population"] == pytest.approx(1760.0)
    assert alloc_b.loc["B1", "allocated_area"] == pytest.approx(64.0)
    assert alloc_b.loc["B1", "estimated_population"] == pytest.approx(640.0)


def test_allocated_geometries_partition_dissolved_union(result):
    for community, expected_area in [("A", 512.0), ("B", 512.0)]:
        pieces = result.allocations.loc[community].geometry
        covered = union_all(pieces)
        assert area(covered) == pytest.approx(expected_area)
        # single coverage: summing pieces equals the union area
        assert pieces.area.sum() == pytest.approx(area(covered))
        dissolved = result.communities.loc[community].geometry
        assert area(dissolved) == pytest.approx(expected_area)
        assert covered.difference(dissolved).is_empty


def test_unallocated_inputs_listed_with_reason(result):
    unallocated = result.unallocated.set_index("parcel")
    assert unallocated.loc["A8", "reason"] == "fully_covered_by_higher_priority"
    assert unallocated.loc["A9", "reason"] == "no_polygonal_geometry"
    assert unallocated.loc["A10", "reason"] == "no_polygonal_geometry"
    assert unallocated.loc["B9", "reason"] == "no_polygonal_geometry"
    # original index labels, including the duplicated one, are retained
    assert sorted(unallocated.index) == ["A10", "A8", "A9", "B9"]


def test_input_order_does_not_change_result(parcels):
    shuffled = parcels.sample(frac=1.0, random_state=7)
    baseline = allocate_population(
        parcels,
        by="community",
        population="population",
        priority="priority",
        business_id="parcel",
    )
    reordered = allocate_population(
        shuffled,
        by="community",
        population="population",
        priority="priority",
        business_id="parcel",
    )
    pd.testing.assert_frame_equal(
        baseline.communities.sort_index(), reordered.communities.sort_index()
    )
    columns = ["source_area", "allocated_area", "estimated_population"]
    left = baseline.allocations.reset_index().sort_values(
        ["community", "parcel"]
    )[columns]
    right = reordered.allocations.reset_index().sort_values(
        ["community", "parcel"]
    )[columns]
    pd.testing.assert_frame_equal(
        left.reset_index(drop=True), right.reset_index(drop=True)
    )


def test_equal_priority_tie_broken_by_business_id():
    gdf = GeoDataFrame(
        {
            "community": ["T", "T"],
            "parcel": ["T2", "T1"],
            "priority": [1, 1],
            "population": [100.0, 100.0],
            "geometry": [box(0, 0, 10, 10), box(5, 0, 15, 10)],
        },
        crs=CRS,
    )
    result = allocate_population(
        gdf,
        by="community",
        population="population",
        priority="priority",
        business_id="parcel",
    )
    winner = result.allocations.loc["T"].iloc[0]
    assert winner["parcel"] == "T1"
    assert winner["allocated_area"] == pytest.approx(100.0)
    loser = result.allocations.loc["T"].iloc[1]
    assert loser["parcel"] == "T2"
    assert loser["allocated_area"] == pytest.approx(50.0)


def test_boundary_contact_does_not_contribute_area():
    gdf = GeoDataFrame(
        {
            "community": ["C", "C"],
            "parcel": ["C1", "C2"],
            "priority": [1, 2],
            "population": [100.0, 200.0],
            "geometry": [box(0, 0, 10, 10), box(10, 0, 20, 10)],
        },
        crs=CRS,
    )
    result = allocate_population(
        gdf,
        by="community",
        population="population",
        priority="priority",
        business_id="parcel",
    )
    assert result.communities.loc["C", "total_area"] == pytest.approx(200.0)
    alloc = result.allocations.loc["C"].set_index("parcel")
    assert alloc.loc["C2", "allocated_area"] == pytest.approx(100.0)
    assert alloc.loc["C2", "estimated_population"] == pytest.approx(200.0)


def test_reproject_to_common_equal_area_crs():
    gdf = GeoDataFrame(
        {
            "community": ["G", "G"],
            "priority": [1, 2],
            "population": [100.0, 200.0],
            "geometry": [box(0, 0, 1, 1), box(0.5, 0, 1.5, 1)],
        },
        crs=4326,
    )
    result = allocate_population(
        gdf, by="community", population="population",
        priority="priority", crs=CRS,
    )
    assert result.communities.crs.to_epsg() == CRS
    assert result.allocations.crs.to_epsg() == CRS
    assert result.unallocated.crs.to_epsg() == CRS


def test_original_index_used_when_business_id_missing():
    gdf = GeoDataFrame(
        {
            "community": ["Z", "Z"],
            "priority": [1, 2],
            "population": [100.0, 200.0],
            "geometry": [box(0, 0, 10, 10), box(5, 0, 15, 10)],
        },
        crs=CRS,
    )
    gdf.index = ["p", "q"]
    result = allocate_population(
        gdf, by="community", population="population", priority="priority"
    )
    assert list(result.allocations["_source_index"]) == ["p", "q"]


def test_all_unknown_population_yields_nan_estimate():
    gdf = GeoDataFrame(
        {
            "community": ["U"],
            "priority": [1],
            "population": [np.nan],
            "geometry": [box(0, 0, 10, 10)],
        },
        crs=CRS,
    )
    result = allocate_population(
        gdf, by="community", population="population", priority="priority"
    )
    summary = result.communities.loc["U"]
    assert summary.known_area == pytest.approx(0.0)
    assert summary.unknown_area == pytest.approx(100.0)
    assert np.isnan(summary.estimated_population)


def test_community_without_usable_geometry():
    gdf = GeoDataFrame(
        {
            "community": ["E", "E"],
            "priority": [1, 2],
            "population": [100.0, 200.0],
            "geometry": [GeometryCollection(), GeometryCollection()],
        },
        crs=CRS,
    )
    result = allocate_population(
        gdf, by="community", population="population", priority="priority"
    )
    summary = result.communities.loc["E"]
    assert summary.total_area == pytest.approx(0.0)
    assert np.isnan(summary.estimated_population)
    assert len(result.allocations) == 0
    assert len(result.unallocated) == 2


def test_validation_errors():
    gdf = GeoDataFrame(
        {
            "community": ["G"],
            "priority": [1],
            "population": [1.0],
            "geometry": [box(0, 0, 1, 1)],
        },
        crs=CRS,
    )
    with pytest.raises(TypeError, match="GeoDataFrame"):
        allocate_population(
            pd.DataFrame({"community": ["G"], "priority": [1]}),
            by="community",
            population="population",
            priority="priority",
        )
    with pytest.raises(KeyError, match="Missing required"):
        allocate_population(
            gdf.drop(columns="priority"), by="community",
            population="population", priority="priority",
        )
    no_crs = gdf.copy()
    no_crs = no_crs.set_crs(None, allow_override=True)
    with pytest.raises(ValueError, match="CRS"):
        allocate_population(
            no_crs, by="community", population="population",
            priority="priority",
        )
    missing_priority = gdf.copy()
    missing_priority.loc[0, "priority"] = np.nan
    with pytest.raises(ValueError, match="missing"):
        allocate_population(
            missing_priority, by="community", population="population",
            priority="priority",
        )
    geographic = gdf.set_crs(4326, allow_override=True)
    with pytest.raises(ValueError, match="projected equal-area"):
        allocate_population(
            geographic, by="community", population="population",
            priority="priority",
        )
    with pytest.raises(KeyError, match="business id"):
        allocate_population(
            gdf, by="community", population="population",
            priority="priority", business_id="missing",
        )


def test_regular_dissolve_still_works(parcels):
    dissolved = parcels.dissolve("community")
    assert set(dissolved.index) == {"A", "B"}
    assert dissolved.geometry.area.loc["A"] == pytest.approx(512.0)


def test_empty_input_returns_empty_result():
    empty = GeoDataFrame(
        {
            "community": pd.Series(dtype=object),
            "priority": pd.Series(dtype=float),
            "population": pd.Series(dtype=float),
            "geometry": GeoDataFrame(geometry=[]).geometry,
        },
        crs=CRS,
    )
    result = allocate_population(
        empty, by="community", population="population", priority="priority"
    )
    assert len(result.communities) == 0
    assert len(result.allocations) == 0
    assert len(result.unallocated) == 0
    assert result.communities.crs.to_epsg() == CRS
    assert "reason" in result.unallocated.columns


def test_top_level_export():
    assert geopandas.allocate_population is allocate_population
    assert geopandas.PopulationAllocation is PopulationAllocation
