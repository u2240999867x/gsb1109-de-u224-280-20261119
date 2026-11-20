"""Areal population estimation with priority-based overlap resolution.

When survey parcels overlap inside a community, every piece of ground must be
attributed to exactly one source parcel before population can be estimated from
covered area.  The adjudication rule implemented here is:

1. parcels are ordered by a user supplied priority (smaller value wins) and,
   for parcels sharing a priority, by their original business number;
2. each parcel keeps the part of its geometry that is not already covered by a
   higher-ranked parcel (boundary contact contributes no area);
3. the winning parcel remains the recorded source of that area even when it has
   no population value;
4. for parcels with a known population, population is assumed to be uniformly
   distributed, so the estimated contribution is the source population scaled
   by the share of its original area that was allocated.

All geometric work happens in a single equal-area projected CRS so that areas
measured for different communities are comparable.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

from shapely import area as shp_area
from shapely import difference, is_empty, make_valid, union
from shapely.geometry import GeometryCollection, MultiPolygon, Polygon

from geopandas import GeoDataFrame

_AREA_TOLERANCE = 1e-12

REASON_NO_POLYGON = "no_polygonal_geometry"
REASON_ZERO_AREA = "zero_area_geometry"
REASON_FULLY_COVERED = "fully_covered_by_higher_priority"


@dataclass
class PopulationAllocation:
    """Result of :func:`allocate_population`.

    Attributes
    ----------
    communities : GeoDataFrame
        One row per community holding the dissolved geometry together with
        single-coverage total area, known/unknown indicator area and the
        estimated population.
    allocations : GeoDataFrame
        One row per source parcel that was awarded area.  Each record keeps the
        original business number, the actual allocated geometry, the source and
        allocated areas and the estimated population contribution.
    unallocated : GeoDataFrame
        Input rows that could not contribute any area (missing, empty or
        zero-area geometry, or geometry entirely covered by higher-priority
        parcels), together with a ``reason`` column for follow-up.
    """

    communities: GeoDataFrame
    allocations: GeoDataFrame
    unallocated: GeoDataFrame


def _polygonal_part(geom):
    """Return the polygonal component of a geometry.

    Parameters
    ----------
    geom : shapely geometry or None
        Geometry that may be invalid, empty or non-polygonal.

    Returns
    -------
    shapely geometry or None
        A (Multi)Polygon covering every polygonal component, or ``None`` when
        the geometry does not contain any two-dimensional part.
    """
    if geom is None or is_empty(geom):
        return None
    if not geom.is_valid:
        geom = make_valid(geom)
    if isinstance(geom, (Polygon, MultiPolygon)):
        return geom
    if isinstance(geom, GeometryCollection):
        polygons = [
            part
            for part in geom.geoms
            if isinstance(part, (Polygon, MultiPolygon)) and not is_empty(part)
        ]
        if not polygons:
            return None
        merged = polygons[0]
        for part in polygons[1:]:
            merged = union(merged, part)
        return merged
    return None


def _as_list(value):
    """Wrap a scalar column name in a list while leaving lists untouched."""
    if isinstance(value, str):
        return [value]
    return list(value)


def _prepare_workframe(
    gdf, by_cols, population, priority, business_id, crs
):
    """Validate the input and build the projected working frame.

    Returns the frame reprojected to the common measurement CRS together with
    the business id column name and stable source positions.
    """
    if not isinstance(gdf, GeoDataFrame):
        raise TypeError("`gdf` must be a GeoDataFrame.")
    required = [*by_cols, population, priority]
    missing_columns = [col for col in required if col not in gdf.columns]
    if missing_columns:
        raise KeyError(f"Missing required column(s): {missing_columns}.")
    if gdf.crs is None:
        raise ValueError("`gdf` must have a CRS defined so areas can be measured.")
    if gdf[priority].isna().any():
        raise ValueError("Priority values must not be missing.")

    work = gdf.to_crs(crs) if crs is not None else gdf.copy()
    if work.crs.is_geographic:
        raise ValueError(
            "Areas must be measured in a projected equal-area CRS; pass the "
            "`crs` argument or provide a projected input GeoDataFrame."
        )

    if business_id is None:
        id_name = "_source_index"
        work["_source_index"] = work.index.astype(object).to_numpy()
    else:
        id_name = business_id
    if id_name not in work.columns:
        raise KeyError(f"Missing business id column: {id_name!r}.")

    work = work.assign(_source_pos=np.arange(len(work), dtype=np.int64))
    return work, id_name


def _allocation_columns(by_cols, id_name, population, geom_col):
    """List the columns of an empty allocation frame."""
    return [
        *by_cols,
        id_name,
        "_source_pos",
        "source_area",
        "allocated_area",
        "allocation_share",
        population,
        "has_population",
        "estimated_population",
        geom_col,
    ]


def _adjudicate_community(
    group, key, by_cols, id_name, population, priority, crs
):
    """Resolve overlaps for one community in priority order.

    Returns the allocation frame, the positions and reasons of the rejected
    input rows and the summary statistics of the community.
    """
    geom_col = group.geometry.name
    ordered = group.sort_values(
        by=[priority, id_name, "_source_pos"], kind="stable"
    ).reset_index(drop=True)

    allocation_rows = []
    unallocated_positions = []
    unallocated_reasons = []
    coverage = None

    for i in range(len(ordered)):
        row = ordered.iloc[i]
        source_geom = _polygonal_part(row[geom_col])
        source_pos = int(row["_source_pos"])
        if source_geom is None:
            unallocated_positions.append(source_pos)
            unallocated_reasons.append(REASON_NO_POLYGON)
            continue
        source_area = float(shp_area(source_geom))
        if source_area <= _AREA_TOLERANCE:
            unallocated_positions.append(source_pos)
            unallocated_reasons.append(REASON_ZERO_AREA)
            continue

        allocated_geom = (
            source_geom
            if coverage is None
            else _polygonal_part(difference(source_geom, coverage))
        )
        if (
            allocated_geom is None
            or float(shp_area(allocated_geom)) <= _AREA_TOLERANCE
        ):
            unallocated_positions.append(source_pos)
            unallocated_reasons.append(REASON_FULLY_COVERED)
        else:
            allocation_rows.append(
                _allocation_row(
                    row, key, id_name, population, geom_col,
                    allocated_geom, source_area, source_pos,
                )
            )

        coverage = (
            source_geom if coverage is None else union(coverage, source_geom)
        )

    if allocation_rows:
        allocations = GeoDataFrame(
            allocation_rows, geometry=geom_col, crs=crs
        )
    else:
        allocations = GeoDataFrame(
            columns=_allocation_columns(by_cols, id_name, population, geom_col),
            geometry=geom_col,
            crs=crs,
        )
    summary = _community_summary(
        key,
        geom_col,
        coverage,
        allocations,
        len(group),
        len(unallocated_positions),
    )
    return (
        allocations,
        unallocated_positions,
        unallocated_reasons,
        summary,
    )


def _allocation_row(
    row, key, id_name, population, geom_col, allocated_geom, source_area,
    source_pos,
):
    """Build one allocation record, applying the uniform-density estimate."""
    allocated_area = float(shp_area(allocated_geom))
    pop_value = row[population]
    has_pop = pd.notna(pop_value)
    estimated = (
        float(pop_value) * allocated_area / source_area if has_pop else np.nan
    )
    return {
        **key,
        id_name: row[id_name],
        "_source_pos": source_pos,
        "source_area": source_area,
        "allocated_area": allocated_area,
        "allocation_share": allocated_area / source_area,
        population: pop_value,
        "has_population": bool(has_pop),
        "estimated_population": estimated,
        geom_col: allocated_geom,
    }


def _community_summary(
    key, geom_col, coverage, allocations, n_inputs, n_unallocated
):
    """Aggregate area and population indicators for one community."""
    known_area = float(
        allocations.loc[allocations["has_population"], "allocated_area"].sum()
    )
    unknown_area = float(
        allocations.loc[~allocations["has_population"], "allocated_area"].sum()
    )
    estimated_population = (
        float(allocations["estimated_population"].sum(min_count=1))
        if len(allocations)
        else np.nan
    )
    return {
        **key,
        geom_col: coverage if coverage is not None else GeometryCollection(),
        "total_area": known_area + unknown_area,
        "known_area": known_area,
        "unknown_area": unknown_area,
        "estimated_population": estimated_population,
        "n_inputs": n_inputs,
        "n_allocated": len(allocations),
        "n_unallocated": n_unallocated,
    }


def _empty_result(work, by_cols, id_name, population):
    """Build the empty result frames for an input without any rows."""
    geom_col = work.geometry.name
    communities = GeoDataFrame(
        columns=[
            *by_cols,
            geom_col,
            "total_area",
            "known_area",
            "unknown_area",
            "estimated_population",
            "n_inputs",
            "n_allocated",
            "n_unallocated",
        ],
        geometry=geom_col,
        crs=work.crs,
    ).set_index(by_cols)
    allocation_columns = [
        col
        for col in _allocation_columns(by_cols, id_name, population, geom_col)
        if col != "_source_pos"
    ]
    allocations = GeoDataFrame(
        columns=allocation_columns, geometry=geom_col, crs=work.crs
    ).set_index(by_cols)
    unallocated = work.iloc[0:0].copy()
    unallocated["reason"] = pd.Series(dtype=object)
    helper_columns = [
        col for col in ("_source_pos", "_source_index") if col in unallocated
    ]
    unallocated = unallocated.drop(columns=helper_columns, errors="ignore")
    return PopulationAllocation(
        communities=communities, allocations=allocations, unallocated=unallocated
    )


def allocate_population(
    gdf,
    by,
    population,
    priority,
    *,
    business_id=None,
    crs=None,
):
    """Estimate community population from overlapping survey parcels.

    Parcels are dissolved per community while overlaps are adjudicated by
    priority: higher-priority parcels (smaller ``priority`` value) keep every
    area they cover, including area where their population value is missing,
    and lower-priority parcels only keep their exclusive remainder.  Population
    is estimated under the uniform-distribution convention, i.e. a parcel with
    known population contributes ``population * allocated_area / source_area``.

    Parameters
    ----------
    gdf : GeoDataFrame
        Input survey parcels.  Must have a CRS defined and contain polygon
        geometries, the community column, a population column and a priority
        column.  Missing population values (``NaN``) mark parcels with unknown
        population.
    by : str or list of str
        Column(s) identifying the community a parcel belongs to.  Adjudication
        never crosses community boundaries.
    population : str
        Name of the population column.  Missing values are allowed and flag
        area whose population indicator is unknown.
    priority : str
        Name of the priority column.  Smaller values indicate higher priority;
        missing priority values raise an error.
    business_id : str, optional
        Name of the column holding the original business number of each parcel.
        It is used to break ties between parcels with equal priority and is
        copied to the allocation records.  When omitted, the original DataFrame
        index labels are used.
    crs : optional
        Equal-area projected CRS in which all areas are measured.  Any value
        accepted by :meth:`GeoDataFrame.to_crs` is allowed.  The same CRS is
        used for every community.  When ``None``, the input CRS is used
        directly and must already be projected.

    Returns
    -------
    PopulationAllocation
        Dataclass with the community summary (``communities``), the per-source
        allocation records (``allocations``) and the inputs that contributed no
        area (``unallocated``).

    Examples
    --------
    >>> from shapely.geometry import box
    >>> import geopandas as gpd
    >>> gdf = gpd.GeoDataFrame(
    ...     {
    ...         "community": ["A", "A"],
    ...         "parcel": [1, 2],
    ...         "priority": [1, 2],
    ...         "population": [100.0, 50.0],
    ...         "geometry": [box(0, 0, 10, 10), box(5, 5, 15, 15)],
    ...     },
    ...     crs=6933,
    ... )
    >>> result = allocate_population(
    ...     gdf, by="community", population="population",
    ...     priority="priority", business_id="parcel",
    ... )
    >>> result.communities[
    ...     ["total_area", "estimated_population"]
    ... ]  # doctest: +NORMALIZE_WHITESPACE
               total_area  estimated_population
    community
    A              175.0                 137.5
    """
    by_cols = _as_list(by)
    work, id_name = _prepare_workframe(
        gdf, by_cols, population, priority, business_id, crs
    )
    if len(work) == 0:
        return _empty_result(work, by_cols, id_name, population)
    geom_col = work.geometry.name

    community_frames = []
    allocation_frames = []
    unallocated_frames = []

    for key_values, group in work.groupby(by_cols, dropna=False, sort=True):
        if not isinstance(key_values, tuple):
            key_values = (key_values,)
        key = dict(zip(by_cols, key_values))
        (
            allocations,
            rejected_positions,
            rejected_reasons,
            summary,
        ) = _adjudicate_community(
            group, key, by_cols, id_name, population, priority, work.crs
        )
        allocation_frames.append(allocations)
        if rejected_positions:
            rejected = work.iloc[rejected_positions].copy()
            rejected["reason"] = rejected_reasons
            unallocated_frames.append(rejected)
        community_frames.append(
            GeoDataFrame([summary], geometry=geom_col, crs=work.crs)
        )

    communities = pd.concat(community_frames, ignore_index=True)
    allocations = pd.concat(allocation_frames, ignore_index=True)
    if unallocated_frames:
        unallocated = pd.concat(unallocated_frames, ignore_index=False)
    else:
        unallocated = work.iloc[0:0].copy()
        unallocated["reason"] = pd.Series(dtype=object)

    helper_columns = [
        col for col in ("_source_pos", "_source_index") if col in unallocated
    ]
    unallocated = unallocated.drop(columns=helper_columns, errors="ignore")
    allocations = allocations.drop(columns="_source_pos")

    communities = communities.set_index(by_cols)
    allocations = allocations.set_index(by_cols)

    return PopulationAllocation(
        communities=communities,
        allocations=allocations,
        unallocated=unallocated,
    )
