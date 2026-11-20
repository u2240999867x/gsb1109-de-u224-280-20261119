"""Reconcilable line/line intersection views for inspection workflows.

The standard :func:`geopandas.tools.overlay` intersection returns one row per
intersecting source pair, but a single pair may intersect in several
disconnected places at once: isolated point crossings (inspection events) and
coincident line segments (length to patrol). With the default
``keep_geom_type=True`` the point parts are dropped entirely, and when they
are retained they are packed together with the line parts into one geometry
collection, so the two kinds of inspection components cannot be reconciled
separately.

The default ``overlay`` behaviour is left untouched. This module builds two
independently reconcilable views on top of the same pairwise intersection
computation:

* :func:`line_intersection_views` returns ``segments`` (one row per atomic,
  mutually exclusive piece of the shared physical network, tagged with every
  left/right source pair covering it) and ``events`` (one row per original
  point intersection, tagged with its owning source pair).

Stable source provenance is carried by the original left/right index labels
plus explicit positional row numbers. Flattened output row numbers are never
used as a source identifier and de-duplicated physical lengths never replace
the per-source-pair intersection results.
"""

from dataclasses import dataclass

import numpy as np
import pandas as pd

import shapely
from shapely import (
    covered_by,
    get_dimensions,
    get_parts,
    intersects,
    node,
    union_all,
)

from geopandas import GeoDataFrame
from geopandas.array import LINE_GEOM_TYPES, _check_crs, _crs_mismatch_warn

# Columns carrying stable source provenance. ``*_row`` is the positional row
# number inside the input frame; ``*_idx`` holds the original index label,
# which is not required to be unique on either side.
LEFT_ROW = "left_row"
RIGHT_ROW = "right_row"
LEFT_IDX = "left_idx"
RIGHT_IDX = "right_idx"
ON_OTHER_COINCIDENT = "on_other_coincident_segment"


@dataclass
class LineIntersectionViews:
    """The two reconcilable views produced by :func:`line_intersection_views`.

    Attributes
    ----------
    segments : GeoDataFrame
        Atomic, mutually exclusive pieces of the shared physical network with
        one row per (piece, covering source pair). A piece covered by more
        than one left/right source pair is therefore repeated with
        ``shared_segment=True``: the physical length is counted once per
        distinct ``segment_id`` while every source-pair claim is retained.
    events : GeoDataFrame
        One row per original point intersection of a source pair. A point
        that happens to sit on another source pair's coincident segment keeps
        its identity as a point event
        (``on_other_coincident_segment=True``).
    """

    segments: GeoDataFrame
    events: GeoDataFrame

    def unique_length_by_route(self, side="left"):
        """Return the unique physical patrol length along each original route.

        Shared pieces are counted once per route even when several source
        pairs of that route cover the same piece, so the result is the
        de-duplicated length that actually needs inspection.

        Parameters
        ----------
        side : {"left", "right"}
            Input side whose original routes should be reported.

        Returns
        -------
        DataFrame
            ``row``/``idx`` of the route and its unique ``patrol_length``.
        """
        if side not in ("left", "right"):
            raise ValueError("side must be 'left' or 'right'")
        row_col = LEFT_ROW if side == "left" else RIGHT_ROW
        idx_col = LEFT_IDX if side == "left" else RIGHT_IDX
        if self.segments.empty:
            return pd.DataFrame(columns=[row_col, idx_col, "patrol_length"])
        unique_pieces = self.segments[
            ["segment_id", row_col, idx_col, "segment_length"]
        ].drop_duplicates(subset=["segment_id", row_col])
        return (
            unique_pieces.groupby([row_col, idx_col], sort=True)["segment_length"]
            .sum()
            .rename("patrol_length")
            .reset_index()
        )

    def pair_reconciliation(self):
        """Per source-pair reconciliation of line length and point events.

        Returns
        -------
        DataFrame
            One row per intersecting source pair with the line length of the
            raw pairwise intersection, the length attributed through the
            segment view and the number of point events.
        """
        columns = [
            LEFT_ROW,
            RIGHT_ROW,
            "attributed_line_length",
            "event_count",
        ]
        if self.segments.empty and self.events.empty:
            return pd.DataFrame(columns=columns)
        if self.events.empty:
            reconciliation = (
                self.segments.groupby([LEFT_ROW, RIGHT_ROW], sort=True)[
                    "segment_length"
                ]
                .sum()
                .rename("attributed_line_length")
                .to_frame()
            )
            reconciliation["event_count"] = 0
        elif self.segments.empty:
            reconciliation = (
                self.events.groupby([LEFT_ROW, RIGHT_ROW], sort=True)
                .size()
                .rename("event_count")
                .to_frame()
            )
            reconciliation["attributed_line_length"] = 0.0
        else:
            segment_lengths = (
                self.segments.groupby([LEFT_ROW, RIGHT_ROW], sort=True)[
                    "segment_length"
                ]
                .sum()
                .rename("attributed_line_length")
            )
            event_counts = (
                self.events.groupby([LEFT_ROW, RIGHT_ROW], sort=True)
                .size()
                .rename("event_count")
            )
            reconciliation = pd.concat([segment_lengths, event_counts], axis=1)
        reconciliation = reconciliation.fillna(
            {"attributed_line_length": 0.0, "event_count": 0}
        )
        reconciliation["event_count"] = reconciliation["event_count"].astype("int64")
        return reconciliation.reset_index()


def _validate_line_inputs(df1, df2):
    """Validate that both frames are line-only and return their CRS."""
    for position, df in [(1, df1), (2, df2)]:
        if not isinstance(df, GeoDataFrame):
            raise TypeError(f"df{position} must be a GeoDataFrame")
        geom_types = set(df.geom_type.unique())
        non_line = geom_types - set(LINE_GEOM_TYPES)
        if non_line:
            raise TypeError(
                f"df{position} must contain only line geometries, "
                f"found geometry type(s): {sorted(non_line)}"
            )
    if not _check_crs(df1, df2):
        _crs_mismatch_warn(df1, df2, stacklevel=3)
    return df1.crs


def _attribute_columns(df, side):
    """Non-geometry attribute columns of one input with a ``_1``/``_2`` suffix."""
    columns = [col for col in df.columns if col != df.geometry.name]
    attributes = df[columns].reset_index(drop=True).copy()
    attributes.columns = [f"{col}_{side}" for col in attributes.columns]
    return attributes


def _pair_intersections(df1, df2, crs):
    """Intersect every intersecting source pair, mirroring ``overlay`` internals.

    Returns one row per intersecting pair carrying positional source rows,
    the original (possibly duplicated) index labels, both attribute sets and
    the raw intersection geometry (line, point or a collection of both).
    """
    idx1, idx2 = df2.sindex.query(df1.geometry, predicate="intersects", sort=True)
    if idx1.size == 0:
        return GeoDataFrame(
            {
                LEFT_ROW: pd.Series(dtype="int64"),
                RIGHT_ROW: pd.Series(dtype="int64"),
                LEFT_IDX: pd.Series(dtype="object"),
                RIGHT_IDX: pd.Series(dtype="object"),
                "geometry": pd.Series(dtype="object"),
            },
            geometry="geometry",
            crs=crs,
        )

    left = df1.geometry.take(idx1).reset_index(drop=True)
    right = df2.geometry.take(idx2).reset_index(drop=True)
    intersections = left.intersection(right)
    intersections = intersections[~intersections.is_empty]
    kept = intersections.index.to_numpy()

    pairs = pd.DataFrame(
        {
            LEFT_ROW: idx1[kept].astype("int64"),
            RIGHT_ROW: idx2[kept].astype("int64"),
            "geometry": intersections.values,
        }
    )
    pairs[LEFT_IDX] = df1.index.to_numpy()[pairs[LEFT_ROW].to_numpy()]
    pairs[RIGHT_IDX] = df2.index.to_numpy()[pairs[RIGHT_ROW].to_numpy()]

    left_attr = _attribute_columns(df1, 1)
    right_attr = _attribute_columns(df2, 2)
    pairs = pairs.merge(left_attr, left_on=LEFT_ROW, right_index=True, how="left")
    pairs = pairs.merge(right_attr, left_on=RIGHT_ROW, right_index=True, how="left")
    return GeoDataFrame(pairs, geometry="geometry", crs=crs)


def _explode_parts(pairs, dimension):
    """Explode raw pair intersections into parts of one dimension."""
    if pairs.empty:
        return pairs.iloc[:0].copy()
    raw = np.asarray(pairs.geometry.values, dtype=object)
    parts, part_index = get_parts(raw, return_index=True)
    part_index = np.asarray(part_index)
    matching = get_dimensions(parts) == dimension
    exploded = pairs.iloc[part_index[matching]].reset_index(drop=True)
    return GeoDataFrame(exploded, geometry=parts[matching], crs=pairs.crs)


def _atomic_pieces(geometries):
    """Node all input lines together and return the atomic line pieces."""
    if len(geometries) == 0:
        return np.empty(0, dtype=object)
    noded = node(union_all(np.asarray(geometries, dtype=object)))
    if noded is None or shapely.is_empty(noded):
        return np.empty(0, dtype=object)
    pieces = get_parts(noded)
    pieces = pieces[get_dimensions(pieces) == 1]
    return pieces[~shapely.is_empty(pieces)]


def _build_segments(pieces, line_rows, crs):
    """Attach every atomic piece to each source pair whose line covers it."""
    base_columns = [
        "segment_id",
        "shared_segment",
        "source_pair_count",
        "segment_length",
        LEFT_ROW,
        RIGHT_ROW,
        LEFT_IDX,
        RIGHT_IDX,
    ]
    if len(pieces) == 0 or line_rows.empty:
        columns = base_columns + [
            col
            for col in line_rows.columns
            if col not in base_columns and col != "geometry"
        ]
        empty = pd.DataFrame(
            {col: pd.Series(dtype="object") for col in columns + ["geometry"]}
        )
        return GeoDataFrame(empty, geometry="geometry", crs=crs)

    pair_lines = np.asarray(line_rows.geometry.values, dtype=object)
    records = []
    for segment_id, piece in enumerate(pieces):
        covering = np.nonzero(covered_by(piece, pair_lines))[0]
        for pair_position in covering:
            record = line_rows.iloc[int(pair_position)].to_dict()
            record["geometry"] = piece
            record["segment_id"] = segment_id
            record["source_pair_count"] = len(covering)
            records.append(record)

    segments = GeoDataFrame(records, geometry="geometry", crs=crs)
    segments["shared_segment"] = segments["source_pair_count"] > 1
    segments["segment_length"] = segments.geometry.length
    remaining = [
        col for col in segments.columns if col not in base_columns and col != "geometry"
    ]
    return segments[base_columns + remaining + ["geometry"]].reset_index(drop=True)


def _build_events(point_rows, pair_keys, pair_line_unions, crs):
    """Keep one row per original point intersection with its owning pair."""
    columns = [
        "event_id",
        LEFT_ROW,
        RIGHT_ROW,
        LEFT_IDX,
        RIGHT_IDX,
        ON_OTHER_COINCIDENT,
    ]
    if point_rows.empty:
        remaining = [
            col
            for col in point_rows.columns
            if col not in columns and col != "geometry"
        ]
        empty = pd.DataFrame(
            {
                col: pd.Series(dtype="object")
                for col in columns + remaining + ["geometry"]
            }
        )
        return GeoDataFrame(empty, geometry="geometry", crs=crs)

    points = np.asarray(point_rows.geometry.values, dtype=object)
    on_other = np.zeros(len(points), dtype=bool)
    if len(pair_line_unions) > 0:
        line_unions = np.asarray(pair_line_unions, dtype=object)
        for position, point in enumerate(points):
            owners = np.nonzero(intersects(point, line_unions))[0]
            own_pair = (
                point_rows.iloc[position][LEFT_ROW],
                point_rows.iloc[position][RIGHT_ROW],
            )
            on_other[position] = any(
                tuple(pair_keys[int(owner)]) != own_pair for owner in owners
            )

    events = point_rows.copy()
    events.insert(0, "event_id", np.arange(len(events), dtype="int64"))
    events[ON_OTHER_COINCIDENT] = on_other
    remaining = [
        col for col in events.columns if col not in columns and col != "geometry"
    ]
    return events[columns + remaining + ["geometry"]].reset_index(drop=True)


def line_intersection_views(df1, df2):
    """Build reconcilable segment-length and point-event views of two line layers.

    The two inputs are intersected pairwise using the same spatial-index
    mechanism as the default :func:`~geopandas.tools.overlay` intersection,
    which itself is not modified. Reordering the rows of either input only
    remaps the positional ``*_row`` keys; every result stays reconcilable
    through those keys together with the original ``*_idx`` labels.

    Parameters
    ----------
    df1, df2 : GeoDataFrame
        Line-only GeoDataFrames representing the construction and existing
        inspection networks. Index labels may be duplicated on either side;
        positional ``left_row``/``right_row`` columns provide a stable key
        alongside the labels.

    Returns
    -------
    LineIntersectionViews
        ``segments`` and ``events`` views, each carrying the original left
        and right index labels, positional source rows and all input
        attributes suffixed with ``_1`` and ``_2``. Source pairs without an
        intersection produce no inspection components and do not appear.
    """
    crs = _validate_line_inputs(df1, df2)

    pairs = _pair_intersections(df1, df2, crs)
    line_rows = _explode_parts(pairs, dimension=1)
    point_rows = _explode_parts(pairs, dimension=0)

    pieces = _atomic_pieces(list(df1.geometry.values) + list(df2.geometry.values))
    segments = _build_segments(pieces, line_rows, crs)

    pair_keys = []
    pair_line_unions = []
    if not line_rows.empty:
        for (left_row, right_row), group in line_rows.groupby(
            [LEFT_ROW, RIGHT_ROW], sort=True
        ):
            pair_keys.append((int(left_row), int(right_row)))
            pair_line_unions.append(
                union_all(np.asarray(group.geometry.values, dtype=object))
            )
    events = _build_events(point_rows, pair_keys, pair_line_unions, crs)

    return LineIntersectionViews(segments=segments, events=events)
