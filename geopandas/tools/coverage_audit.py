"""Area-based coverage audit of parcel geometries against delivery-zone masks.

This module complements :func:`geopandas.clip`: instead of returning the
clipped geometries, it accounts *how much* area of every input feature is
covered by each (possibly overlapping, possibly holed) mask. Overlapping
masks may therefore appear in the per-mask details for the same feature,
while the per-feature totals count the shared region only once.
"""

import numpy as np
import pandas.api.types

from shapely.geometry import MultiPolygon, Polygon, box

from geopandas import GeoDataFrame, GeoSeries
from geopandas.array import _check_crs, _crs_mismatch_warn
from geopandas.tools.clip import _mask_is_list_like_rectangle


def _is_number(value):
    return isinstance(value, (int, float, np.integer, np.floating)) and not isinstance(
        value, bool
    )


def _coerce_mask_records(masks):
    """Normalize the supported mask inputs into ``(geoms, ids)``.

    Each returned geometry is paired with the identifier of the delivery
    zone it represents; list-like rectangle masks are converted to an
    equivalent rectangular polygon so that holes and overlaps are handled
    with ordinary set-theoretic intersections.
    """
    if isinstance(masks, GeoDataFrame | GeoSeries):
        geometry = masks.geometry
        geoms = geometry.values.copy()
        ids = list(masks.index)
        return geoms, ids

    def _as_record(item, position):
        if _mask_is_list_like_rectangle(item):
            if len(item) != 4:
                raise TypeError(
                    "If a mask is list-like, it must have four values "
                    "(minx, miny, maxx, maxy)"
                )
            return box(*item), position
        if isinstance(item, Polygon | MultiPolygon):
            return item, position
        raise TypeError(
            "'masks' entries should be (Multi)Polygon or list-like bounds, "
            f"got {type(item)}"
        )

    if isinstance(masks, Polygon | MultiPolygon):
        records = [_as_record(masks, 0)]
    elif pandas.api.types.is_list_like(masks):
        if len(masks) == 4 and all(_is_number(value) for value in masks):
            records = [(box(*masks), 0)]
        else:
            records = [
                _as_record(item, position) for position, item in enumerate(masks)
            ]
    else:
        raise TypeError(
            "'masks' should be a GeoDataFrame, GeoSeries, (Multi)Polygon or "
            f"list-like, got {type(masks)}"
        )

    if not records:
        raise ValueError("'masks' must contain at least one mask geometry")

    geoms = np.array([record[0] for record in records], dtype=object)
    ids = [record[1] for record in records]
    return geoms, ids


def _relate_to_mask(source_geom, mask_geom):
    """Classify the spatial relation of one source geometry to one mask.

    Returns a ``(relation, intersection)`` tuple. ``relation`` is one of
    ``"covered"`` (positive-area overlap), ``"touch"`` (boundary-only
    contact, no interior intersection) or ``"disjoint"`` (no intersection
    at all). Bounding-box proximity alone never counts as contact: the
    classification is based on the actual geometric relation, so a feature
    fully enclosed in a hole of the mask is reported as disjoint even when
    its bounds intersect the mask bounds.
    """
    if source_geom.relate_pattern(mask_geom, "T********"):
        return "covered", source_geom.intersection(mask_geom)
    if source_geom.relate_pattern(mask_geom, "****T****"):
        return "touch", None
    return "disjoint", None


def coverage_audit(gdf, masks):
    """Audit the area covered by delivery-zone masks for each input feature.

    Every feature of ``gdf`` is intersected with every mask. The function
    returns per-mask coverage details (including boundary-only contacts and
    actual non-intersections), per-feature coverage totals where overlapping
    masks are counted only once, and the clipped pieces of every feature.

    Both layers must use the same equal-area Coordinate Reference System
    (CRS) so that the accounted areas and their ratios are comparable; a
    mismatched CRS only warns, mirroring :func:`geopandas.clip`.

    Parameters
    ----------
    gdf : GeoDataFrame or GeoSeries
        Input layer of monitoring parcels. Its index may contain
        duplicated values; every row is audited independently and
        retained in the results.
    masks : GeoDataFrame, GeoSeries, (Multi)Polygon or list-like
        Delivery-zone polygons. A GeoDataFrame or GeoSeries keeps its own
        feature identifiers (index values) as ``mask_id``; a plain
        (Multi)Polygon or a list of masks gets zero-based integer ids.
        List entries may also be four-element bounds
        ``(minx, miny, maxx, maxy)``, which are treated as the equivalent
        rectangular polygon. Masks may overlap and may contain holes.

    Returns
    -------
    details : GeoDataFrame
        One row per pair of source feature and mask, always including
        non-intersecting pairs. Columns:

        source_id
            Index value of the source feature in ``gdf``.
        mask_id
            Identifier of the delivery-zone mask.
        relation
            ``"covered"`` for positive-area intersection, ``"touch"`` for
            boundary-only contact and ``"disjoint"`` when the geometries
            do not intersect.
        area
            Area of the real intersection (``0`` for touch and disjoint).
        ratio
            ``area`` divided by the original, unclipped source feature
            area.
        source_area
            Area of the original source feature (the shared denominator).
        source_geometry
            Unmodified original source geometry.
        mask_geometry
            Geometry of the mask the row was computed against.
        clipped_geometry
            Intersection geometry for covered rows, otherwise ``None``.
    totals : GeoDataFrame
        One row per source feature. Columns:

        source_id, source_area, source_geometry
            As in ``details``.
        covered_area
            Area of the union of all mask intersections, so regions shared
            by several masks count once.
        covered_ratio
            ``covered_area`` divided by ``source_area``.
        intersects_any
            True when at least one mask has a positive-area intersection.
        mask_ids
            List of mask ids with a positive-area intersection.
        touched_mask_ids
            List of mask ids with boundary-only contact.
    clipped : GeoDataFrame
        The non-empty intersection pieces, one row per covered detail row,
        retaining ``source_id`` and ``mask_id`` provenance. Overlaps
        between masks are preserved as separate pieces.

    See Also
    --------
    clip : return only the geometries clipped to a mask.

    Examples
    --------
    >>> from shapely.geometry import Polygon
    >>> parcels = GeoDataFrame(
    ...     geometry=GeoSeries([Polygon([(0, 0), (2, 0), (2, 2), (0, 2)])]),
    ...     crs="EPSG:6933",
    ... )
    >>> zone = Polygon([(1, 1), (3, 1), (3, 3), (1, 3)])
    >>> details, totals, clipped = coverage_audit(parcels, [zone])
    >>> round(float(totals["covered_ratio"].iloc[0]), 3)
    0.25
    """
    if not isinstance(gdf, GeoDataFrame | GeoSeries):
        raise TypeError(f"'gdf' should be GeoDataFrame or GeoSeries, got {type(gdf)}")

    mask_geoms, mask_ids = _coerce_mask_records(masks)
    if isinstance(masks, GeoDataFrame | GeoSeries) and not _check_crs(gdf, masks):
        _crs_mismatch_warn(gdf, masks, stacklevel=2)

    source_values = gdf.geometry.values
    source_index = gdf.index
    source_areas = source_values.area
    n_sources = len(source_values)
    n_masks = len(mask_geoms)

    detail_rows = []
    union_by_position = [None] * n_sources
    covered_mask_ids = [[] for _ in range(n_sources)]
    touched_mask_ids = [[] for _ in range(n_sources)]

    for source_position in range(n_sources):
        source_geom = source_values[source_position]
        source_id = source_index[source_position]
        source_area = source_areas[source_position]
        union_geom = None

        for mask_position in range(n_masks):
            mask_geom = mask_geoms[mask_position]
            mask_id = mask_ids[mask_position]
            relation, intersection = _relate_to_mask(source_geom, mask_geom)

            if relation == "covered":
                intersection_area = intersection.area
                union_geom = (
                    intersection
                    if union_geom is None
                    else union_geom.union(intersection)
                )
                covered_mask_ids[source_position].append(mask_id)
                clipped_geom = intersection
            else:
                intersection_area = 0.0
                clipped_geom = None
                if relation == "touch":
                    touched_mask_ids[source_position].append(mask_id)

            detail_rows.append(
                {
                    "source_id": source_id,
                    "mask_id": mask_id,
                    "relation": relation,
                    "area": intersection_area,
                    "ratio": (
                        intersection_area / source_area if source_area > 0 else np.nan
                    ),
                    "source_area": source_area,
                    "source_geometry": source_geom,
                    "mask_geometry": mask_geom,
                    "clipped_geometry": clipped_geom,
                }
            )

        union_by_position[source_position] = union_geom

    detail_columns = [
        "source_id",
        "mask_id",
        "relation",
        "area",
        "ratio",
        "source_area",
        "source_geometry",
        "mask_geometry",
        "clipped_geometry",
    ]
    if detail_rows:
        details = GeoDataFrame(
            detail_rows,
            geometry="clipped_geometry",
            crs=gdf.crs,
        )
    else:
        details = GeoDataFrame(
            {column: [] for column in detail_columns},
            geometry="clipped_geometry",
            crs=gdf.crs,
        )

    covered_areas = np.array(
        [
            0.0 if union_geom is None else union_geom.area
            for union_geom in union_by_position
        ]
    )
    totals = GeoDataFrame(
        {
            "source_id": list(source_index),
            "source_area": source_areas,
            "covered_area": covered_areas,
            "covered_ratio": np.divide(
                covered_areas,
                source_areas,
                out=np.full(n_sources, np.nan),
                where=source_areas > 0,
            ),
            "intersects_any": covered_areas > 0,
            "mask_ids": covered_mask_ids,
            "touched_mask_ids": touched_mask_ids,
            "source_geometry": source_values,
        },
        geometry="source_geometry",
        crs=gdf.crs,
    )

    clipped = details.loc[
        details["relation"] == "covered",
        ["source_id", "mask_id", "clipped_geometry"],
    ].set_geometry("clipped_geometry")
    clipped = clipped[~clipped.geometry.is_empty].reset_index(drop=True)

    return details, totals, clipped
