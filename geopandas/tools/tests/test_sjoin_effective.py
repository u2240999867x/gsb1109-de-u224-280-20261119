import pandas as pd

from shapely.geometry import Point, box

import geopandas
from geopandas import GeoDataFrame, sjoin, sjoin_effective
from geopandas.tools.sjoin_effective import EffectiveJoinResult, EffectiveJoinSpec

import pytest

AREAS = {
    "area_id": ["A1", "A1", "C3"],
    "rank": [1, 1, 2],
    "valid_from": pd.to_datetime(["2020-01-01", "2021-01-01", "2020-01-01"]),
    "valid_to": pd.to_datetime(["2021-01-01", "2022-01-01", "2022-01-01"]),
    "type_from": pd.to_datetime([pd.NaT, pd.NaT, "2021-07-01"]),
    "type_to": pd.to_datetime([pd.NaT, pd.NaT, "2022-01-01"]),
    "area_type": [None, None, "retail"],
    "label": ["A-old", "A-new", "C"],
    "geometry": [box(0, 0, 1, 1), box(0, 0, 1, 1), box(1, 1, 2, 2)],
}

STORES = {
    "store_id": ["before", "cut", "after", "outside", "no_time"],
    "kind": ["retail", "retail", "retail", "retail", None],
    "observed_at": pd.to_datetime(
        ["2020-06-01", "2021-01-01", "2021-08-01", "2020-06-01", None]
    ),
    "geometry": [
        Point(1, 1),
        Point(1, 1),
        Point(1, 1),
        Point(5, 5),
        Point(1, 1),
    ],
}


def make_frames():
    areas = GeoDataFrame(AREAS, index=[10, 10, 20])
    stores = GeoDataFrame(STORES, index=[7, 7, 8, 9, 9])
    return stores, areas


def make_spec(**overrides):
    options = dict(
        left_time="observed_at",
        right_valid_from="valid_from",
        right_valid_to="valid_to",
        area_id="area_id",
        rank="rank",
        boundary="start_closed_end_open",
        left_id="store_id",
        right_id="label",
        left_type="kind",
        right_type="area_type",
        right_type_valid_from="type_from",
        right_type_valid_to="type_to",
    )
    options.update(overrides)
    return EffectiveJoinSpec(**options)


def assignment_by_store(result):
    return result.assignments.set_index("store_id", drop=False)


def test_boundary_and_rank_selection():
    stores, areas = make_frames()
    result = sjoin_effective(stores, areas, make_spec())

    assigned = assignment_by_store(result)
    assert assigned.loc["before", "selected_area_id"] == "A1"
    assert assigned.loc["before", "label"] == "A-old"
    assert assigned.loc["cut", "selected_area_id"] == "A1"
    assert assigned.loc["cut", "label"] == "A-new"
    assert assigned.loc["after", "selected_area_id"] == "C3"
    assert assigned.loc["after", "label"] == "C"
    assert (assigned.loc[["before", "cut", "after"], "status"] == "matched").all()


def test_alternate_boundary_convention():
    stores, areas = make_frames()
    result = sjoin_effective(stores, areas, make_spec(boundary="start_open_end_closed"))

    assigned = assignment_by_store(result)
    assert assigned.loc["before", "label"] == "A-old"
    assert assigned.loc["cut", "label"] == "A-old"
    assert assigned.loc["after", "selected_area_id"] == "C3"


def test_invalid_boundary_is_rejected():
    with pytest.raises(ValueError, match="boundary"):
        make_spec(boundary="inclusive")


def test_audit_distinguishes_expired_not_effective_and_not_selected():
    stores, areas = make_frames()
    result = sjoin_effective(stores, areas, make_spec())
    audit = result.candidates

    def reason_for(store, label):
        rows = audit[(audit["store_id"] == store) & (audit["label"] == label)]
        assert len(rows) == 1
        return rows.iloc[0]["reason"]

    assert reason_for("after", "A-new") == "lower_rank"
    assert reason_for("after", "A-old") == "expired"
    assert reason_for("cut", "A-old") == "expired"
    assert reason_for("before", "A-new") == "not_yet_effective"
    assert reason_for("before", "C") == "type_not_yet_applicable"
    assert reason_for("after", "C") == "selected"

    expired = audit[(audit["store_id"] == "after") & (audit["label"] == "A-old")]
    assert expired["time_valid"].tolist() == [False]
    assert expired["spatial_match"].tolist() == [True]

    lower_rank = audit[(audit["store_id"] == "after") & (audit["label"] == "A-new")]
    assert lower_rank["effective"].tolist() == [True]
    assert lower_rank["selected"].tolist() == [False]


def test_out_of_area_and_missing_time_are_kept_separately():
    stores, areas = make_frames()
    result = sjoin_effective(stores, areas, make_spec())

    assigned = assignment_by_store(result)
    assert assigned.loc["outside", "status"] == "out_of_area"
    assert assigned.loc["outside", "selection_reason"] == "no_spatial_candidate"
    assert assigned.loc["no_time", "status"] == "missing_time"
    assert assigned.loc["no_time", "selection_reason"] == "left_time_missing"

    assert set(result.out_of_area["store_id"]) == {"outside"}
    assert set(result.missing_time["store_id"]) == {"no_time"}
    assert set(result.unmatched["store_id"]) == {"outside", "no_time"}


def test_no_effective_candidate_when_candidates_exist_but_all_expired():
    stores, areas = make_frames()
    areas = areas[areas["area_id"] == "C3"]
    result = sjoin_effective(stores.iloc[[0]], areas, make_spec())

    row = result.assignments.iloc[0]
    assert row["status"] == "no_effective_candidate"
    assert row["selection_reason"] == "no_candidate_valid_at_time_and_type"
    assert row["candidate_count"] == 1
    assert row["effective_candidate_count"] == 0


def test_reordering_both_frames_does_not_change_business_assignment():
    stores, areas = make_frames()
    spec = make_spec()
    baseline = sjoin_effective(stores, areas, spec)
    flipped = sjoin_effective(stores.iloc[::-1], areas.iloc[::-1], spec)

    columns = [
        "store_id",
        "status",
        "selection_reason",
        "selected_area_id",
        "label",
        "candidate_count",
        "effective_candidate_count",
    ]
    expected = baseline.assignments[columns].sort_values("store_id")
    actual = flipped.assignments[columns].sort_values("store_id")
    pd.testing.assert_frame_equal(
        expected.reset_index(drop=True), actual.reset_index(drop=True)
    )


def test_tie_break_is_lowest_stable_area_id_not_row_position():
    areas = GeoDataFrame(
        {
            "area_id": ["Z9", "B2"],
            "rank": [3, 3],
            "valid_from": pd.to_datetime(["2020-01-01", "2020-01-01"]),
            "valid_to": pd.to_datetime(["2022-01-01", "2022-01-01"]),
            "label": ["z", "b"],
            "geometry": [box(0, 0, 1, 1), box(0, 0, 1, 1)],
        },
        index=[4, 4],
    )
    stores = GeoDataFrame(
        {
            "store_id": ["s"],
            "observed_at": pd.to_datetime(["2021-01-01"]),
            "geometry": [Point(0.5, 0.5)],
        },
        index=[1],
    )
    spec = EffectiveJoinSpec(
        left_time="observed_at",
        right_valid_from="valid_from",
        right_valid_to="valid_to",
        area_id="area_id",
        rank="rank",
        boundary="start_closed_end_open",
        left_id="store_id",
        right_id="label",
    )

    forward = sjoin_effective(stores, areas, spec)
    backward = sjoin_effective(stores, areas.iloc[::-1], spec)
    assert forward.assignments.iloc[0]["selected_area_id"] == "B2"
    assert backward.assignments.iloc[0]["selected_area_id"] == "B2"
    assert (
        backward.assignments.iloc[0]["selection_reason"]
        == "highest_rank_then_lowest_area_id"
    )


def test_overlapping_versions_same_rank_and_area_conflict_for_review():
    areas = GeoDataFrame(
        {
            "area_id": ["X", "X"],
            "rank": [2, 2],
            "valid_from": pd.to_datetime(["2020-01-01", "2020-06-01"]),
            "valid_to": pd.to_datetime(["2022-01-01", "2022-01-01"]),
            "label": ["v1", "v2"],
            "geometry": [box(0, 0, 1, 1), box(0, 0, 1, 1)],
        },
        index=[3, 3],
    )
    stores = GeoDataFrame(
        {
            "store_id": ["s"],
            "observed_at": pd.to_datetime(["2021-06-01"]),
            "geometry": [Point(0.5, 0.5)],
        },
        index=[8],
    )
    spec = EffectiveJoinSpec(
        left_time="observed_at",
        right_valid_from="valid_from",
        right_valid_to="valid_to",
        area_id="area_id",
        rank="rank",
        boundary="start_closed_end_open",
        left_id="store_id",
        right_id="label",
        attribution_cols=("label",),
    )

    result = sjoin_effective(stores, areas, spec)
    assert result.assignments.iloc[0]["status"] == "conflict"
    assert result.assignments.iloc[0]["selection_reason"] == (
        "conflicting_overlapping_versions"
    )
    assert pd.isna(result.assignments.iloc[0]["selected_area_id"])
    assert len(result.conflicts) == 2
    assert result.conflicts["conflict_id"].nunique() == 1
    assert set(result.conflicts["label"]) == {"v1", "v2"}
    assert "right_position" not in result.conflicts.columns

    flipped = sjoin_effective(stores, areas.iloc[::-1], spec)
    assert set(flipped.conflicts["label"]) == {"v1", "v2"}
    assert flipped.conflicts["conflict_id"].nunique() == 1


def test_overlapping_versions_with_same_attribution_are_not_a_conflict():
    areas = GeoDataFrame(
        {
            "area_id": ["X", "X"],
            "rank": [2, 2],
            "valid_from": pd.to_datetime(["2020-01-01", "2020-06-01"]),
            "valid_to": pd.to_datetime(["2022-01-01", "2022-01-01"]),
            "label": ["same", "same"],
            "geometry": [box(0, 0, 1, 1), box(0, 0, 2, 2)],
        }
    )
    stores = GeoDataFrame(
        {
            "store_id": ["s"],
            "observed_at": pd.to_datetime(["2021-06-01"]),
            "geometry": [Point(0.5, 0.5)],
        }
    )
    spec = EffectiveJoinSpec(
        left_time="observed_at",
        right_valid_from="valid_from",
        right_valid_to="valid_to",
        area_id="area_id",
        rank="rank",
        boundary="start_closed_end_open",
        left_id="store_id",
        attribution_cols=("label",),
    )

    result = sjoin_effective(stores, areas, spec)
    assert result.assignments.iloc[0]["status"] == "matched"
    assert result.conflicts.empty


def test_conflict_focuses_on_attribution_not_geometry():
    areas = GeoDataFrame(
        {
            "area_id": ["X", "X"],
            "rank": [2, 2],
            "valid_from": pd.to_datetime(["2020-01-01", "2020-06-01"]),
            "valid_to": pd.to_datetime(["2022-01-01", "2022-01-01"]),
            "label": ["v1", "v2"],
            "geometry": [box(0, 0, 1, 1), box(0, 0, 2, 2)],
        }
    )
    stores = GeoDataFrame(
        {
            "store_id": ["s"],
            "observed_at": pd.to_datetime(["2021-06-01"]),
            "geometry": [Point(0.5, 0.5)],
        }
    )
    spec = EffectiveJoinSpec(
        left_time="observed_at",
        right_valid_from="valid_from",
        right_valid_to="valid_to",
        area_id="area_id",
        rank="rank",
        boundary="start_closed_end_open",
        left_id="store_id",
        attribution_cols=("label",),
    )

    result = sjoin_effective(stores, areas, spec)
    assert result.assignments.iloc[0]["status"] == "conflict"
    assert len(result.conflicts) == 2


def test_missing_validity_bounds_are_not_silently_effective():
    areas = GeoDataFrame(
        {
            "area_id": ["A"],
            "rank": [1],
            "valid_from": pd.to_datetime([None]),
            "valid_to": pd.to_datetime(["2022-01-01"]),
            "geometry": [box(0, 0, 1, 1)],
        }
    )
    stores = GeoDataFrame(
        {
            "store_id": ["s"],
            "observed_at": pd.to_datetime(["2021-01-01"]),
            "geometry": [Point(0.5, 0.5)],
        }
    )
    spec = EffectiveJoinSpec(
        left_time="observed_at",
        right_valid_from="valid_from",
        right_valid_to="valid_to",
        area_id="area_id",
        rank="rank",
        boundary="start_closed_end_open",
        left_id="store_id",
    )
    result = sjoin_effective(stores, areas, spec)
    assert result.assignments.iloc[0]["status"] == "no_effective_candidate"
    assert result.candidates.iloc[0]["reason"] == "missing_validity"

    open_spec = EffectiveJoinSpec(**{**spec.__dict__, "allow_open_bounds": True})
    open_result = sjoin_effective(stores, areas, open_spec)
    assert open_result.assignments.iloc[0]["status"] == "matched"


def test_legacy_sjoin_is_unchanged_when_effective_is_not_enabled():
    stores, areas = make_frames()
    legacy = sjoin(stores, areas, predicate="intersects")
    assert not isinstance(legacy, EffectiveJoinResult)

    expected_pairs = set(
        zip(legacy.index.to_numpy(), legacy["index_right"].to_numpy(), strict=True)
    )
    assert (7, 10) in expected_pairs
    assert legacy.shape[0] == 12
    assert "label" in legacy.columns

    enabled = geopandas.sjoin(stores, areas, effective=make_spec())
    assert isinstance(enabled, EffectiveJoinResult)


def test_duplicate_indexes_are_safe_for_audit_tables():
    stores, areas = make_frames()
    assert not stores.index.is_unique
    assert not areas.index.is_unique

    result = sjoin_effective(stores, areas, make_spec())
    assert "index_left" in result.candidates.columns
    assert "index_right" in result.candidates.columns
    assert result.candidates["index_right"].notna().all()
    assert len(result.candidates) == len(result.candidates.drop_duplicates())


def test_multiple_candidates_for_one_store_are_ranked():
    stores = GeoDataFrame(
        {
            "store_id": ["s"],
            "observed_at": pd.to_datetime(["2021-01-01"]),
            "geometry": [Point(0.5, 0.5)],
        }
    )
    areas = GeoDataFrame(
        {
            "area_id": ["low", "high", "mid"],
            "rank": [1, 3, 2],
            "valid_from": pd.to_datetime(["2020-01-01"] * 3),
            "valid_to": pd.to_datetime(["2022-01-01"] * 3),
            "geometry": [box(0, 0, 1, 1), box(0, 0, 1, 1), box(0, 0, 1, 1)],
        }
    )
    spec = EffectiveJoinSpec(
        left_time="observed_at",
        right_valid_from="valid_from",
        right_valid_to="valid_to",
        area_id="area_id",
        rank="rank",
        boundary="start_closed_end_open",
        left_id="store_id",
    )
    result = sjoin_effective(stores, areas, spec)
    assert result.assignments.iloc[0]["selected_area_id"] == "high"
    assert result.assignments.iloc[0]["candidate_count"] == 3
    assert result.assignments.iloc[0]["effective_candidate_count"] == 3
    reasons = result.candidates.set_index("area_id")["reason"]
    assert reasons["high"] == "selected"
    assert reasons["mid"] == "lower_rank"
    assert reasons["low"] == "lower_rank"


def test_missing_required_column_reports_frame():
    stores, areas = make_frames()
    with pytest.raises(ValueError, match="missing from the left"):
        sjoin_effective(stores.drop(columns="observed_at"), areas, make_spec())
    with pytest.raises(ValueError, match="missing from the right"):
        sjoin_effective(stores, areas.drop(columns="area_id"), make_spec())


def test_result_unpacking_and_aliases():
    stores, areas = make_frames()
    result = sjoin_effective(stores, areas, make_spec())
    assignments, candidates, conflicts = result
    assert assignments is result.assignments
    assert candidates is result.candidates
    assert conflicts is result.conflicts
    assert result.attribution is result.assignments
    assert result.audit is result.candidates


def test_left_type_without_right_type_is_rejected():
    with pytest.raises(ValueError, match="right_type is required"):
        EffectiveJoinSpec(
            left_time="observed_at",
            right_valid_from="valid_from",
            right_valid_to="valid_to",
            area_id="area_id",
            rank="rank",
            boundary="start_closed_end_open",
            left_type="kind",
        )


def test_conflict_keeps_lower_candidate_explained():
    stores = GeoDataFrame(
        {
            "store_id": ["s"],
            "observed_at": pd.to_datetime(["2021-01-01"]),
            "geometry": [Point(0.5, 0.5)],
        }
    )
    areas = GeoDataFrame(
        {
            "area_id": ["x", "x", "y"],
            "rank": [2, 2, 1],
            "valid_from": pd.to_datetime(["2020-01-01"] * 3),
            "valid_to": pd.to_datetime(["2022-01-01"] * 3),
            "label": ["a", "b", "low"],
            "geometry": [box(0, 0, 1, 1)] * 3,
        }
    )
    spec = EffectiveJoinSpec(
        left_time="observed_at",
        right_valid_from="valid_from",
        right_valid_to="valid_to",
        area_id="area_id",
        rank="rank",
        boundary="start_closed_end_open",
        left_id="store_id",
        attribution_cols=("label",),
    )
    result = sjoin_effective(stores, areas, spec)
    assert result.assignments.iloc[0]["status"] == "conflict"
    assert result.assignments.iloc[0]["effective_candidate_count"] == 3
    reasons = result.candidates.set_index("label")["reason"]
    assert reasons["a"] == "conflict_version"
    assert reasons["b"] == "conflict_version"
    assert reasons["low"] == "lower_rank"


def test_effective_sjoin_rejects_non_inner_how():
    stores, areas = make_frames()
    with pytest.raises(ValueError, match="how"):
        sjoin(stores, areas, how="left", effective=make_spec())


def test_invalid_rank_values_are_rejected():
    stores, areas = make_frames()
    areas = areas.copy()
    areas["rank"] = ["low", "low", "high"]
    with pytest.raises(ValueError, match="must contain numeric ranks"):
        sjoin_effective(stores, areas, make_spec())


def test_timezone_mismatch_raises_actionable_error():
    stores, areas = make_frames()
    areas = areas.copy()
    areas["valid_from"] = pd.to_datetime(areas["valid_from"]).dt.tz_localize("UTC")
    areas["valid_to"] = pd.to_datetime(areas["valid_to"]).dt.tz_localize("UTC")

    with pytest.raises(ValueError, match="timezone"):
        sjoin_effective(stores, areas, make_spec())


def test_selected_values_preserve_right_hand_dtypes():
    stores, areas = make_frames()
    result = sjoin_effective(stores, areas, make_spec())
    assigned = result.assignments

    assert str(assigned["valid_from"].dtype).startswith("datetime64")
    assert assigned["selected_rank"].iloc[:3].tolist() == [1.0, 1.0, 2.0]
    assert assigned["selected_rank"].iloc[3:].isna().all()
    assert str(assigned["selected_right_position"].dtype) == "Int64"
    assert assigned["selected_right_position"].iloc[:3].tolist() == [0, 1, 2]
    assert assigned["selected_right_position"].iloc[3:].isna().all()


def test_multiindex_inputs_and_index_columns():
    stores, areas = make_frames()
    stores = stores.set_index(
        [pd.Index(stores["store_id"], name="store"), pd.Index(["a"] * 5, name="side")]
    )
    areas = areas.set_index(
        [pd.Index(areas["label"], name="version"), pd.Index([0, 1, 2], name="seq")]
    )
    result = sjoin_effective(stores, areas, make_spec())

    assert "index_left_0" in result.candidates.columns
    assert "index_left_1" in result.candidates.columns
    assert "index_right_0" in result.candidates.columns
    assert "index_right_1" in result.candidates.columns
    selected = result.assignments["selected_right_index"].tolist()
    assert selected[:3] == [("A-old", 0), ("A-new", 1), ("C", 2)]
    assert pd.isna(selected[3])
    assert pd.isna(selected[4])


def test_inherited_type_window_without_type_column_is_rejected():
    with pytest.raises(ValueError, match="right_type is required"):
        EffectiveJoinSpec(
            left_time="observed_at",
            right_valid_from="valid_from",
            right_valid_to="valid_to",
            area_id="area_id",
            rank="rank",
            boundary="start_closed_end_open",
            right_type_valid_from="type_from",
            right_type_valid_to="type_to",
        )
