"""Versioned delivery of asset points and inspection routes into one GeoPackage.

The two asset layers are delivered as a single version: a caller hands over the
new point layer and the new route layer, and the receiving side either keeps
seeing the complete previous version of both layers (when anything fails) or
sees both new layers at once (on success). Layers that do not belong to this
delivery are never touched.

Both layers are keyed by a business asset number so the receiver can join them
one-to-one. Incoming frames may carry a duplicated external DataFrame index;
it is dropped and never written to the target. A duplicated business asset
number is rejected before the target file is touched, so it can never create
an ambiguous one-to-many join on the receiving side.

Route geometries that the GPKG driver cannot persist as-is (explicitly empty
or missing source geometry) are stored as NULL geometry plus a readable
companion column ``route_status`` with one of ``complete`` / ``empty`` /
``missing``, so the receiver can recover the state per business asset number
without knowing anything about the original DataFrame index.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

import geopandas
import pandas as pd

__all__ = [
    "DEFAULT_KEY",
    "POINTS_LAYER",
    "ROUTES_LAYER",
    "ROUTE_STATUS_COLUMN",
    "ROUTE_COMPLETE",
    "ROUTE_EMPTY",
    "ROUTE_MISSING",
    "DuplicateAssetNumberError",
    "deliver_asset_version",
    "read_asset_version",
    "route_status_by_asset",
]

DEFAULT_KEY = "asset_id"
POINTS_LAYER = "asset_points"
ROUTES_LAYER = "inspection_routes"

ROUTE_STATUS_COLUMN = "route_status"
ROUTE_COMPLETE = "complete"
ROUTE_EMPTY = "empty"
ROUTE_MISSING = "missing"

_DRIVER = "GPKG"

_METADATA_CLEANUP = (
    "DELETE FROM gpkg_contents WHERE table_name = ?",
    "DELETE FROM gpkg_geometry_columns WHERE table_name = ?",
    "DELETE FROM gpkg_tile_matrix_set WHERE table_name = ?",
    "DELETE FROM gpkg_tile_matrix WHERE table_name = ?",
    "DELETE FROM gpkg_extensions WHERE table_name = ?",
    "DELETE FROM gpkg_metadata_reference WHERE table_name = ?",
)


class DuplicateAssetNumberError(ValueError):
    """An incoming layer carries a duplicated business asset number."""


def _select_columns(gdf, key, columns):
    """Keep only the key, the requested business columns and the geometry.

    The external DataFrame index (which may contain duplicates) is dropped
    here and never reaches the target layer.
    """
    if key not in gdf.columns:
        raise ValueError(f"missing business asset number column {key!r}")
    geometry_name = gdf.geometry.name
    if columns is None:
        columns = [c for c in gdf.columns if c not in (key, geometry_name)]
    else:
        unknown = [c for c in columns if c not in gdf.columns]
        if unknown:
            raise ValueError(f"unknown business column(s): {unknown!r}")
    keep = [key, *columns, geometry_name]
    return gdf.loc[:, keep].reset_index(drop=True)


def _check_unique_key(gdf, key, layer):
    duplicated = gdf[key].duplicated()
    if duplicated.any():
        values = list(pd.unique(gdf.loc[duplicated, key]))
        raise DuplicateAssetNumberError(
            f"layer {layer!r} carries duplicated {key!r} value(s) {values!r}; "
            "delivery rejected before touching the target GeoPackage"
        )


def _prepare_routes(gdf):
    """Add the readable route status and normalise unpersistable geometries."""
    out = gdf.copy()
    status = pd.Series(ROUTE_COMPLETE, index=out.index, name=ROUTE_STATUS_COLUMN)
    status.loc[out.geometry.is_empty] = ROUTE_EMPTY
    status.loc[out.geometry.isna()] = ROUTE_MISSING
    out[ROUTE_STATUS_COLUMN] = status
    unpersistable = status != ROUTE_COMPLETE
    if unpersistable.any():
        cleaned = out.geometry.copy()
        cleaned.loc[unpersistable] = None
        out = out.set_geometry(cleaned)
    geometry_name = out.geometry.name
    columns = [c for c in out.columns if c != geometry_name] + [geometry_name]
    return out.loc[:, columns]


def _layer_names(path):
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as con:
            rows = con.execute("SELECT table_name FROM gpkg_contents").fetchall()
    except sqlite3.DatabaseError:
        return set()
    return {row[0] for row in rows}


def _drop_layer(path, layer):
    """Remove one layer and its GeoPackage metadata from an existing file."""
    with sqlite3.connect(path) as con:
        registered = con.execute(
            "SELECT 1 FROM gpkg_contents WHERE table_name = ?", (layer,)
        ).fetchone()
        if registered is None:
            return
        escaped = layer.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        related = con.execute(
            "SELECT name, type FROM sqlite_master "
            "WHERE name = :layer "
            "OR name LIKE 'rtree_' || :escaped || '\\_%' ESCAPE '\\' "
            "OR (type = 'trigger' AND name LIKE '%\\_' || :escaped ESCAPE '\\')",
            {"layer": layer, "escaped": escaped},
        ).fetchall()
        for name, kind in related:
            if kind in ("table", "trigger", "view"):
                con.execute(f'DROP {kind.upper()} IF EXISTS "{name}"')
        for statement in _METADATA_CLEANUP:
            try:
                con.execute(statement, (layer,))
            except sqlite3.OperationalError:
                pass


def _write_layer(gdf, path, layer):
    """Write one layer; the only write capability this module relies on."""
    gdf.to_file(path, layer=layer, driver=_DRIVER, index=False)


def deliver_asset_version(
    target,
    points,
    routes,
    *,
    key=DEFAULT_KEY,
    points_layer=POINTS_LAYER,
    routes_layer=ROUTES_LAYER,
    points_columns=None,
    routes_columns=None,
):
    """Deliver new point and route layers as one version into ``target``.

    Parameters
    ----------
    target : path-like
        GeoPackage that receives the new version. Existing deliveries of the
        two asset layers are replaced atomically; any other layer is preserved.
    points, routes : GeoDataFrame
        New layer contents, each with its own business columns and geometry
        CRS. A duplicated external index is accepted and dropped; a duplicated
        business asset number raises :class:`DuplicateAssetNumberError` before
        the target file is touched.
    key : str
        Name of the business asset number column linking the two layers.
    points_layer, routes_layer : str
        Layer names inside the GeoPackage.
    points_columns, routes_columns : sequence of str, optional
        Business columns written per layer (besides ``key``). Defaults to all
        non-geometry columns of the incoming frame.

    Returns
    -------
    pathlib.Path
        The target path, now holding the new version of both layers.
    """
    target = Path(target)
    prepared = []
    for layer, gdf, columns in (
        (points_layer, points, points_columns),
        (routes_layer, routes, routes_columns),
    ):
        frame = _select_columns(gdf, key, columns)
        _check_unique_key(frame, key, layer)
        if layer == routes_layer:
            frame = _prepare_routes(frame)
        prepared.append((layer, frame))

    fd, tmp_name = tempfile.mkstemp(
        prefix=target.name + ".", suffix=".tmp", dir=target.parent
    )
    os.close(fd)
    try:
        if target.exists():
            shutil.copyfile(target, tmp_name)
            existing = _layer_names(tmp_name)
        else:
            os.remove(tmp_name)
            existing = set()
        for layer, frame in prepared:
            if layer in existing:
                _drop_layer(tmp_name, layer)
            _write_layer(frame, tmp_name, layer)
        os.replace(tmp_name, target)
    except BaseException:
        with contextlib.suppress(OSError):
            os.remove(tmp_name)
        raise
    return target


def read_asset_version(
    target,
    *,
    key=DEFAULT_KEY,
    points_layer=POINTS_LAYER,
    routes_layer=ROUTES_LAYER,
):
    """Read both asset layers back, each with its own CRS, joined by ``key``.

    Returns a dict mapping layer name to a GeoDataFrame indexed by the
    business asset number (the column is kept as well), so the receiver can
    align or join the two layers one-to-one without knowing the original
    DataFrame index.
    """
    layers = {}
    for layer in (points_layer, routes_layer):
        gdf = geopandas.read_file(target, layer=layer)
        _check_unique_key(gdf, key, layer)
        layers[layer] = gdf.set_index(key, drop=False)
    return layers


def route_status_by_asset(target, *, key=DEFAULT_KEY, routes_layer=ROUTES_LAYER):
    """Map each business asset number to its route status.

    Values are ``complete`` (geometry persisted), ``empty`` (explicitly
    recorded as empty) or ``missing`` (source geometry was missing).
    """
    gdf = geopandas.read_file(target, layer=routes_layer)
    _check_unique_key(gdf, key, routes_layer)
    return dict(zip(gdf[key].tolist(), gdf[ROUTE_STATUS_COLUMN].tolist()))
