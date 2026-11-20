"""Acceptance tests for versioned two-layer asset delivery into a GeoPackage."""

import geopandas
import pytest
from shapely.geometry import LineString, Point

import asset_delivery as ad

POINT_CRS = "EPSG:4326"  # 经纬度
ROUTE_CRS = "EPSG:32632"  # 本地米制
OTHER_CRS = "EPSG:3857"

OTHER_LAYER = "other_business"


def make_points(asset_ids, *, index=None):
    return geopandas.GeoDataFrame(
        {
            "asset_id": list(asset_ids),
            "site_name": [f"site-{aid}" for aid in asset_ids],
        },
        geometry=[Point(6.0 + 0.1 * n, 50.0 + 0.1 * n) for n in range(len(asset_ids))],
        crs=POINT_CRS,
        index=index,
    )


def make_routes(statuses, *, index=None):
    """statuses: dict asset_id -> 'complete' | 'empty' | 'missing'."""
    asset_ids = list(statuses)
    geometries = []
    for n, aid in enumerate(asset_ids):
        state = statuses[aid]
        if state == "complete":
            base = 500000.0 + 100.0 * n
            geometries.append(LineString([(base, 5600000.0), (base + 50.0, 5600050.0)]))
        elif state == "empty":
            geometries.append(LineString())
        elif state == "missing":
            geometries.append(None)
        else:
            raise ValueError(state)
    return geopandas.GeoDataFrame(
        {
            "asset_id": asset_ids,
            "route_name": [f"route-{aid}" for aid in asset_ids],
            "length_m": [120.5 + n for n in range(len(asset_ids))],
        },
        geometry=geometries,
        crs=ROUTE_CRS,
        index=index,
    )


def make_other_layer():
    return geopandas.GeoDataFrame(
        {"other_id": [7, 8], "note": ["keep me", "untouched"]},
        geometry=[Point(500000.0, 5600000.0), Point(500100.0, 5600100.0)],
        crs=OTHER_CRS,
    )


@pytest.fixture
def target(tmp_path):
    """GeoPackage holding the old version of both asset layers + one other layer."""
    path = tmp_path / "assets.gpkg"
    ad.deliver_asset_version(
        path,
        make_points(["A-1", "A-2"]),
        make_routes({"A-1": "complete", "A-2": "complete"}),
    )
    make_other_layer().to_file(
        path, layer=OTHER_LAYER, driver="GPKG", index=False
    )
    return path


def assert_other_layer_untouched(path):
    other = geopandas.read_file(path, layer=OTHER_LAYER)
    assert other.crs == OTHER_CRS
    assert list(other.columns) == ["other_id", "note", "geometry"]
    assert other["other_id"].tolist() == [7, 8]
    assert other["note"].tolist() == ["keep me", "untouched"]
    assert other.geometry.iloc[0].equals(Point(500000.0, 5600000.0))


def test_duplicate_asset_number_rejected_old_delivery_untouched(target, tmp_path):
    before = target.read_bytes()
    # erroneous input: duplicated business asset number
    bad_routes = make_routes({"B-1": "complete", "B-2": "empty"})
    bad_routes.loc[1, "asset_id"] = "B-1"

    with pytest.raises(ad.DuplicateAssetNumberError):
        ad.deliver_asset_version(target, make_points(["B-1", "B-2"]), bad_routes)

    assert target.read_bytes() == before
    layers = ad.read_asset_version(target)
    assert layers[ad.POINTS_LAYER]["asset_id"].tolist() == ["A-1", "A-2"]
    assert layers[ad.ROUTES_LAYER]["asset_id"].tolist() == ["A-1", "A-2"]
    assert ad.route_status_by_asset(target) == {"A-1": "complete", "A-2": "complete"}
    assert_other_layer_untouched(target)
    assert list(tmp_path.glob("*.tmp")) == []


def test_duplicate_point_asset_number_also_rejected(target):
    before = target.read_bytes()
    bad_points = make_points(["B-1", "B-1"])
    with pytest.raises(ad.DuplicateAssetNumberError):
        ad.deliver_asset_version(
            target, bad_points, make_routes({"B-1": "complete"})
        )
    assert target.read_bytes() == before


def test_failed_route_write_keeps_old_version_visible(target, tmp_path, monkeypatch):
    before = target.read_bytes()
    original_to_file = geopandas.GeoDataFrame.to_file

    def failing_to_file(self, filename, *args, **kwargs):
        if kwargs.get("layer") == ad.ROUTES_LAYER:
            raise RuntimeError("simulated driver failure on route layer")
        return original_to_file(self, filename, *args, **kwargs)

    monkeypatch.setattr(geopandas.GeoDataFrame, "to_file", failing_to_file)

    with pytest.raises(RuntimeError, match="simulated driver failure"):
        ad.deliver_asset_version(
            target,
            make_points(["B-1", "B-2", "B-3"]),
            make_routes({"B-1": "complete", "B-2": "empty", "B-3": "missing"}),
        )

    # the caller reopens the target: still the complete old version
    assert target.read_bytes() == before
    layers = ad.read_asset_version(target)
    assert layers[ad.POINTS_LAYER]["asset_id"].tolist() == ["A-1", "A-2"]
    assert layers[ad.ROUTES_LAYER]["asset_id"].tolist() == ["A-1", "A-2"]
    assert ad.route_status_by_asset(target) == {"A-1": "complete", "A-2": "complete"}
    assert_other_layer_untouched(target)
    assert list(tmp_path.glob("*.tmp")) == []


def test_successful_delivery_exposes_one_complete_new_version(target):
    # duplicated external DataFrame index on both incoming frames
    points = make_points(["A-1", "A-2", "A-3"], index=[0, 0, 0])
    routes = make_routes(
        {"A-1": "complete", "A-2": "empty", "A-3": "missing"}, index=[5, 5, 5]
    )

    ad.deliver_asset_version(
        target,
        points,
        routes,
        points_columns=["site_name"],
        routes_columns=["route_name", "length_m"],
    )

    layers = ad.read_asset_version(target)

    new_points = layers[ad.POINTS_LAYER]
    assert new_points.crs == POINT_CRS
    assert list(new_points.columns) == ["asset_id", "site_name", "geometry"]
    assert new_points.index.tolist() == ["A-1", "A-2", "A-3"]
    assert new_points["site_name"].tolist() == [
        "site-A-1",
        "site-A-2",
        "site-A-3",
    ]

    new_routes = layers[ad.ROUTES_LAYER]
    assert new_routes.crs == ROUTE_CRS
    assert list(new_routes.columns) == [
        "asset_id",
        "route_name",
        "length_m",
        ad.ROUTE_STATUS_COLUMN,
        "geometry",
    ]
    assert new_routes.index.tolist() == ["A-1", "A-2", "A-3"]

    # no leaked DataFrame index, no business fields from the other layer
    for frame in (new_points, new_routes):
        assert "index" not in frame.columns
        assert "level_0" not in frame.columns
    assert "route_name" not in new_points.columns
    assert "site_name" not in new_routes.columns

    # receiver finds complete / explicitly empty / missing routes by asset number
    assert ad.route_status_by_asset(target) == {
        "A-1": "complete",
        "A-2": "empty",
        "A-3": "missing",
    }
    assert new_routes.loc["A-1"].geometry.equals(
        LineString([(500000.0, 5600000.0), (500050.0, 5600050.0)])
    )
    assert new_routes.loc["A-2"].geometry is None
    assert new_routes.loc["A-3"].geometry is None

    # the receiver joins the two layers one-to-one on the business asset number
    joined = new_points[["site_name"]].join(
        new_routes[["route_name", ad.ROUTE_STATUS_COLUMN]], how="inner"
    )
    assert len(joined) == 3
    assert joined.loc["A-3", ad.ROUTE_STATUS_COLUMN] == "missing"

    assert_other_layer_untouched(target)


def test_redelivery_replaces_previous_version(target):
    ad.deliver_asset_version(
        target,
        make_points(["C-1"]),
        make_routes({"C-1": "missing"}),
    )
    layers = ad.read_asset_version(target)
    assert layers[ad.POINTS_LAYER]["asset_id"].tolist() == ["C-1"]
    assert ad.route_status_by_asset(target) == {"C-1": "missing"}
    assert_other_layer_untouched(target)
