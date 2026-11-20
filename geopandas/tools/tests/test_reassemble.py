import pandas as pd

from shapely.geometry import LineString, MultiLineString

import geopandas
from geopandas.tools import reassemble_parts
from geopandas.tools.reassemble import ReassemblyResult

import pytest

CRS = "EPSG:3857"

# parts of facility F1, revision 1
a0 = LineString([(0, 0), (1, 1)])
a1 = LineString([(1, 1), (2, 2)])
a2 = LineString([(2, 2), (3, 3)])
a3 = LineString([(3, 3), (4, 4)])
a0_modified = LineString([(0, 0), (1.5, 1.5)])
a2_conflicting = LineString([(9, 9), (8, 8)])

# two identical segments of facility F2, revision 1
b0 = LineString([(10, 10), (11, 11)])

# parts of facility F1, revision 2
c0 = LineString([(20, 20), (21, 21)])
c1 = LineString([(21, 21), (22, 22)])
c0_modified = LineString([(20, 20), (25, 25)])

# parts of facility F5, revision 1
d0 = LineString([(30, 30), (31, 31)])
d1 = LineString([(31, 31), (32, 32)])


def make_source():
    # F1 and F2 deliberately share the DataFrame index label 0
    return geopandas.GeoDataFrame(
        {
            "facility": ["F1", "F2", "F1", "F3", "F4", "F5"],
            "version": [1, 1, 2, 1, 1, 1],
            "name": ["one", "two", "one", "three", "four", "five"],
            "geometry": [
                MultiLineString([a0, a1, a2, a3]),
                MultiLineString([b0, b0]),
                MultiLineString([c0, c1]),
                MultiLineString([]),
                None,
                MultiLineString([d0, d1]),
            ],
        },
        index=[0, 0, 1, 2, 3, 4],
        crs=CRS,
    )


def make_returns():
    # returns arrive out of order and commingled across batches and revisions
    records = [
        ("F2", 1, 1, b0),  # F2 parts returned in reversed order
        ("F1", 2, 1, c1),  # revision 2 returns mixed into revision 1's batch
        ("F1", 1, 1, a1),
        ("F1", 2, 0, c0_modified),
        ("F1", 1, 2, a2),  # contradicting returns for F1 revision 1 part 2
        ("F1", 1, 0, a0_modified),  # edited coordinates
        ("F1", 1, 2, a2_conflicting),
        ("F2", 1, 0, b0),
        ("F1", 1, 1, a1),  # identical duplicate return
        ("F5", 1, 0, None),  # explicit deletion
        ("F5", 1, 1, d1),
        # F1 revision 1 part 3 is never returned
    ]
    return geopandas.GeoDataFrame(
        records, columns=["facility", "version", "part", "geometry"], crs=CRS
    )


@pytest.fixture
def source():
    return make_source()


@pytest.fixture
def returns():
    return make_returns()


@pytest.fixture
def result(source, returns):
    return reassemble_parts(
        source, returns, by="facility", version="version", part="part"
    )


def assert_parts(geom, expected):
    assert len(geom.geoms) == len(expected)
    for actual, wanted in zip(geom.geoms, expected):
        assert actual.equals(wanted)


def test_source_order_attributes_and_crs_preserved(result, source):
    facilities = result.facilities
    assert list(facilities.index) == list(source.index)
    assert list(facilities["facility"]) == list(source["facility"])
    assert list(facilities["version"]) == list(source["version"])
    assert list(facilities["name"]) == list(source["name"])
    assert facilities.crs == source.crs
    assert "reassembled" in facilities.columns


def test_incomplete_facility_not_reported_as_reassembled(result):
    expected = [False, True, True, True, True, True]
    assert list(result.facilities["reassembled"]) == expected
    assert len(result.complete) == 5
    incomplete = result.incomplete
    assert len(incomplete) == 1
    assert incomplete.iloc[0]["facility"] == "F1"
    assert incomplete.iloc[0]["version"] == 1


def test_edited_part_keeps_its_position(result):
    # part 0 came back edited, part 1 unmodified; the conflicted part 2 and
    # the missing part 3 leave a two-segment partial geometry
    geom = result.facilities.geometry.iloc[0]
    assert_parts(geom, [a0_modified, a1])


def test_identical_duplicate_return_adds_no_extra_part(result):
    # part 1 was returned twice with identical content
    assert len(result.facilities.geometry.iloc[0].geoms) == 2


def test_identical_segments_not_collapsed(result):
    # F2 has two identical segments; both must survive the round trip
    geom = result.facilities.geometry.iloc[1]
    assert_parts(geom, [b0, b0])


def test_reversed_returns_restore_original_order(result):
    # F2's parts were returned in reversed order
    geom = result.facilities.geometry.iloc[1]
    parts = list(geom.geoms)
    assert parts[0].equals(b0)
    assert parts[1].equals(b0)


def test_conflicting_returns_are_queryable(result):
    conflicts = result.conflicts
    assert len(conflicts) == 2
    assert set(conflicts["facility"]) == {"F1"}
    assert set(conflicts["version"]) == {1}
    assert set(conflicts["part"]) == {2}
    contents = list(conflicts.geometry)
    assert any(g.equals(a2) for g in contents)
    assert any(g.equals(a2_conflicting) for g in contents)


def test_missing_parts_are_queryable(result):
    missing = result.missing
    assert len(missing) == 1
    row = missing.iloc[0]
    assert row["facility"] == "F1"
    assert row["version"] == 1
    assert row["part"] == 3


def test_explicit_deletion_distinguished_from_no_return(result):
    deleted = result.deleted
    assert len(deleted) == 1
    row = deleted.iloc[0]
    assert row["facility"] == "F5"
    assert row["version"] == 1
    assert row["part"] == 0
    # the deleted part is not reported as missing
    assert not (
        (result.missing["facility"] == "F5") & (result.missing["part"] == 0)
    ).any()
    # the deletion is reflected in the fully reassembled geometry
    assert result.facilities["reassembled"].iloc[5]
    assert_parts(result.facilities.geometry.iloc[5], [d1])


def test_other_revision_does_not_complete_first_revision(result):
    # revision 2 of F1 is fully returned, revision 1 stays incomplete
    assert not result.facilities["reassembled"].iloc[0]
    assert result.facilities["reassembled"].iloc[2]
    geom = result.facilities.geometry.iloc[2]
    assert_parts(geom, [c0_modified, c1])


def test_empty_and_missing_geometry_restored(result):
    empty = result.facilities.geometry.iloc[3]
    assert empty is not None
    assert empty.is_empty
    assert empty.geom_type == "MultiLineString"
    assert result.facilities["reassembled"].iloc[3]
    assert result.facilities.geometry.iloc[4] is None
    assert result.facilities["reassembled"].iloc[4]


def test_result_container(result):
    assert isinstance(result, ReassemblyResult)
    assert "5 of 6 facilities reassembled" in repr(result)


def test_explode_roundtrip_restores_original_part_sequence():
    source = geopandas.GeoDataFrame(
        {
            "facility": ["x", "y"],
            "geometry": [
                MultiLineString([a0, a1, a2]),
                MultiLineString([c0, c1]),
            ],
        },
        index=[7, 7],
        crs=CRS,
    )
    parts = source.explode(index_parts=True)
    result = reassemble_parts(source, parts, by="facility")
    assert list(result.facilities["reassembled"]) == [True, True]
    assert result.facilities.crs == source.crs
    assert list(result.facilities.index) == [7, 7]
    assert_parts(result.facilities.geometry.iloc[0], [a0, a1, a2])
    assert_parts(result.facilities.geometry.iloc[1], [c0, c1])
    assert len(result.missing) == 0
    assert len(result.conflicts) == 0
    assert len(result.deleted) == 0


def test_explode_roundtrip_with_empty_and_missing_geometry():
    source = geopandas.GeoDataFrame(
        {
            "facility": ["e", "m"],
            "geometry": [MultiLineString([]), None],
        },
        crs=CRS,
    )
    parts = source.explode(index_parts=True)
    result = reassemble_parts(source, parts, by="facility")
    assert list(result.facilities["reassembled"]) == [True, True]
    assert result.facilities.geometry.iloc[0].is_empty
    assert result.facilities.geometry.iloc[0].geom_type == "MultiLineString"
    assert result.facilities.geometry.iloc[1] is None


def test_single_part_geometry_roundtrip():
    source = geopandas.GeoDataFrame({"facility": ["s"], "geometry": [a0]}, crs=CRS)
    returns = geopandas.GeoDataFrame(
        {"facility": ["s"], "part": [0], "geometry": [a0_modified]}, crs=CRS
    )
    result = reassemble_parts(source, returns, by="facility", part="part")
    assert list(result.facilities["reassembled"]) == [True]
    geom = result.facilities.geometry.iloc[0]
    assert geom.geom_type == "LineString"
    assert geom.equals(a0_modified)


def test_shared_index_facilities_stay_separate(result):
    # F1 and F2 share index label 0 but are matched by business identifier
    f1 = result.facilities.iloc[0]
    f2 = result.facilities.iloc[1]
    assert f1["facility"] == "F1"
    assert f2["facility"] == "F2"
    assert not f1["reassembled"]
    assert f2["reassembled"]


def test_missing_by_column_raises(source, returns):
    with pytest.raises(ValueError, match="unknown"):
        reassemble_parts(source, returns, by="unknown", version="version", part="part")
    with pytest.raises(ValueError, match="unknown"):
        reassemble_parts(source, returns, by="facility", version="unknown", part="part")


def test_missing_part_column_raises(source, returns):
    with pytest.raises(ValueError, match="unknown"):
        reassemble_parts(
            source, returns, by="facility", version="version", part="unknown"
        )
    flat = returns.reset_index(drop=True)
    with pytest.raises(ValueError, match="part positions"):
        reassemble_parts(source, flat, by="facility", version="version")


def test_wrong_input_type_raises(returns):
    with pytest.raises(TypeError, match="source"):
        reassemble_parts(pd.DataFrame(), returns, by="facility")
    with pytest.raises(TypeError, match="returns"):
        reassemble_parts(make_source(), pd.DataFrame(), by="facility")
