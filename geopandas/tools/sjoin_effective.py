"""Temporally effective spatial joins.

This module implements an opt-in variant of :func:`geopandas.sjoin` that
selects, for every left geometry and observation time, the service area that
was actually in effect at that time.  A plain spatial join cannot express the
two rules involved here: temporal validity and the business ordering used to
break ties between overlapping service areas.

The result is an audit bundle rather than a single frame.  It keeps every
spatial candidate (including candidates rejected because they were expired or
not yet effective), the selected assignment, records without a time or without
a spatial candidate, and groups of conflicting versions that need review.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd
from pandas.api.types import is_datetime64_any_dtype, is_numeric_dtype

from geopandas import GeoDataFrame
from geopandas.tools.sjoin import _geom_predicate_query, _maybe_make_list

START_CLOSED_END_OPEN = "start_closed_end_open"
START_OPEN_END_CLOSED = "start_open_end_closed"

_BOUNDARY_ALIASES = {
    START_CLOSED_END_OPEN: START_CLOSED_END_OPEN,
    "closed_open": START_CLOSED_END_OPEN,
    START_OPEN_END_CLOSED: START_OPEN_END_CLOSED,
    "open_closed": START_OPEN_END_CLOSED,
}

_ASSIGNMENT_RESERVED = {
    "status",
    "selection_reason",
    "selected_area_id",
    "selected_rank",
    "selected_right_position",
    "selected_right_index",
    "candidate_count",
    "effective_candidate_count",
}

_CANDIDATE_RESERVED = {
    "left_position",
    "right_position",
    "spatial_match",
    "time_valid",
    "type_valid",
    "effective",
    "selected",
    "reason",
    "rank",
    "area_id",
    "left_geometry",
    "right_geometry",
}


def _as_tuple(value):
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,)
    return tuple(value)


@dataclass(frozen=True)
class EffectiveJoinSpec:
    """Configuration for :func:`sjoin_effective`.

    Parameters
    ----------
    left_time : str
        Column in the left frame holding the observation time of each record.
    right_valid_from, right_valid_to : str
        Columns in the right frame holding the validity interval of each
        service-area version.
    area_id : str
        Column with the stable business area number.  Ties on ``rank`` are
        broken by this value, never by input row position.
    rank : str
        Column with the service-area rank.  Higher values win.
    boundary : str
        How the validity interval is bounded.  Use
        ``"start_closed_end_open"`` for ``start <= time < end`` or
        ``"start_open_end_closed"`` for ``start < time <= end``.  The aliases
        ``"closed_open"`` and ``"open_closed"`` are accepted as well.  This
        argument is required because the convention cannot be inferred from
        the data.
    left_id, right_id : str or sequence of str, optional
        Stable identifiers used in the audit tables.  They make the result
        safe to compare when the input index is not unique.
    left_type : str, optional
        Column in the left frame with the store type to match.
    right_type : str, optional
        Column in the right frame with the area type.  A missing value or
        ``"*"`` acts as a wildcard.
    type_value : object, optional
        Fixed store type to use when ``left_type`` is not given.  A list or
        set of values is accepted.
    right_type_valid_from, right_type_valid_to : str, optional
        Optional additional applicability window for the type.  This is
        useful when an area is valid for the whole period but only applies to
        a given store type during part of it.  Both columns must be supplied
        together.
    attribution_cols : sequence of str, optional
        Columns that define the effective assignment carried by a service
        area version.  Two overlapping versions with the same rank and area
        id but different values here are reported as a conflict.  If omitted,
        all right-hand columns except the validity, rank, area-id, type and
        geometry columns are compared.
    allow_open_bounds : bool, default False
        If True, a missing ``valid_from`` or ``valid_to`` is treated as an
        unbounded interval.  By default missing validity bounds make a
        version ineligible, so an incomplete period cannot silently win.
    """

    left_time: str
    right_valid_from: str
    right_valid_to: str
    area_id: str
    rank: str
    boundary: str
    left_id: str | tuple[str, ...] | None = None
    right_id: str | tuple[str, ...] | None = None
    left_type: str | None = None
    right_type: str | None = None
    type_value: Any = None
    right_type_valid_from: str | None = None
    right_type_valid_to: str | None = None
    attribution_cols: tuple[str, ...] | None = None
    allow_open_bounds: bool = False

    def __post_init__(self):
        boundary = self.boundary.strip().lower()
        if boundary not in _BOUNDARY_ALIASES:
            allowed = sorted(_BOUNDARY_ALIASES)
            raise ValueError(
                f"Unknown boundary convention {self.boundary!r}; expected one of "
                f"{allowed}."
            )
        object.__setattr__(self, "boundary", _BOUNDARY_ALIASES[boundary])
        object.__setattr__(self, "left_id", _as_tuple(self.left_id))
        object.__setattr__(self, "right_id", _as_tuple(self.right_id))
        object.__setattr__(self, "attribution_cols", _as_tuple(self.attribution_cols))
        if (self.right_type_valid_from is None) != (self.right_type_valid_to is None):
            raise ValueError(
                "right_type_valid_from and right_type_valid_to must be provided "
                "together."
            )
        if self.right_type is not None and (
            self.left_type is None and self.type_value is None
        ):
            raise ValueError(
                "right_type requires either left_type or type_value to determine "
                "the type to match."
            )
        if self.right_type is None and (
            self.left_type is not None
            or self.type_value is not None
            or self.right_type_valid_from is not None
        ):
            raise ValueError(
                "right_type is required when left_type, type_value or a "
                "type-specific validity window is provided."
            )
        if self.right_valid_from == self.right_valid_to:
            raise ValueError(
                "right_valid_from and right_valid_to must be different columns."
            )
        if self.area_id == self.rank:
            raise ValueError("area_id and rank must be different columns.")

    @property
    def start_closed(self):
        return self.boundary == START_CLOSED_END_OPEN

    @property
    def end_closed(self):
        return self.boundary == START_OPEN_END_CLOSED


@dataclass
class EffectiveJoinResult:
    """Result of :func:`sjoin_effective`.

    Attributes
    ----------
    assignments : GeoDataFrame
        One row per left record, in input order.  ``status`` explains whether
        an area was selected, and ``selection_reason`` records the rule that
        was applied.  Selected right-hand columns are included with a suffix
        when they collide with a left-hand column.
    candidates : DataFrame
        One row per spatial candidate.  ``selected`` marks the winner and
        ``reason`` records why other candidates were rejected (for example
        ``"expired"``, ``"not_yet_effective"``, ``"type_mismatch"``,
        ``"lower_rank"`` or ``"valid_not_selected"``).
    conflicts : DataFrame
        Overlapping versions with the same rank and area id but different
        effective attribution.  One row per version, grouped by
        ``conflict_id``.  Conflicted left records have no selected version.
    unmatched : GeoDataFrame
        The subset of ``assignments`` with status ``"out_of_area"`` or
        ``"missing_time"``.
    """

    assignments: GeoDataFrame
    candidates: pd.DataFrame
    conflicts: pd.DataFrame
    unmatched: GeoDataFrame
    spec: EffectiveJoinSpec

    def __iter__(self):
        yield self.assignments
        yield self.candidates
        yield self.conflicts

    @property
    def attribution(self):
        """Alias for :attr:`assignments`."""
        return self.assignments

    @property
    def audit(self):
        """Alias for :attr:`candidates`."""
        return self.candidates

    @property
    def out_of_area(self):
        """Assignments with no spatial candidate and a known observation time."""
        return self.assignments[self.assignments["status"] == "out_of_area"].copy()

    @property
    def missing_time(self):
        """Assignments whose observation time is missing."""
        return self.assignments[self.assignments["status"] == "missing_time"].copy()

    def __repr__(self):
        return (
            f"EffectiveJoinResult(assignments={len(self.assignments)}, "
            f"candidates={len(self.candidates)}, conflicts={len(self.conflicts)}, "
            f"unmatched={len(self.unmatched)})"
        )


def _check_columns(df, columns, frame_name):
    for column in columns:
        if column not in df.columns:
            raise ValueError(
                f"Column {column!r} required by EffectiveJoinSpec is missing from "
                f"the {frame_name} dataframe."
            )


def _validate_spec(left_df, right_df, spec):
    if not isinstance(left_df, GeoDataFrame):
        raise ValueError(f"'left_df' should be GeoDataFrame, got {type(left_df)}")
    if not isinstance(right_df, GeoDataFrame):
        raise ValueError(f"'right_df' should be GeoDataFrame, got {type(right_df)}")

    _check_columns(
        left_df,
        [spec.left_time]
        + list(spec.left_id)
        + ([spec.left_type] if spec.left_type is not None else []),
        "left",
    )
    required_right = [
        spec.right_valid_from,
        spec.right_valid_to,
        spec.area_id,
        spec.rank,
    ]
    required_right += list(spec.right_id)
    if spec.right_type is not None:
        required_right.append(spec.right_type)
    if spec.right_type_valid_from is not None:
        required_right += [spec.right_type_valid_from, spec.right_type_valid_to]
    required_right += list(spec.attribution_cols or ())
    _check_columns(right_df, required_right, "right")

    if not isinstance(left_df, GeoDataFrame):
        raise ValueError(f"'left_df' should be GeoDataFrame, got {type(left_df)}")
    if not isinstance(right_df, GeoDataFrame):
        raise ValueError(f"'right_df' should be GeoDataFrame, got {type(right_df)}")
    if left_df.geometry.name not in left_df.columns:
        raise ValueError("The left dataframe has no active geometry column.")
    if right_df.geometry.name not in right_df.columns:
        raise ValueError("The right dataframe has no active geometry column.")


def _index_frame(index, side):
    frame = index.to_frame(index=False)
    if frame.shape[1] == 1:
        frame.columns = [f"index_{side}"]
    else:
        frame.columns = [f"index_{side}_{i}" for i in range(frame.shape[1])]
    return frame.reset_index(drop=True)


def _unique_name(name, used, suffix="_right"):
    if name not in used:
        used.add(name)
        return name
    candidate = f"{name}{suffix}"
    while candidate in used:
        candidate += "_"
    used.add(candidate)
    return candidate


def _is_missing(value):
    try:
        missing = pd.isna(value)
    except (TypeError, ValueError):
        return False
    return bool(missing) if np.ndim(missing) == 0 else False


def _values_equal(left, right):
    left_missing = _is_missing(left)
    right_missing = _is_missing(right)
    if left_missing or right_missing:
        return left_missing and right_missing
    return bool(left == right)


def _check_timezone_consistency(series):
    timezone_aware = [value.dt.tz is not None for value in series]
    if any(timezone_aware) and not all(timezone_aware):
        raise ValueError(
            "Observation and validity times must either all be timezone-aware "
            "or all be timezone-naive."
        )


def _coerce_time_values(*values):
    series = [pd.Series(value) for value in values]
    if any(is_datetime64_any_dtype(value.dtype) for value in series):
        converted = tuple(pd.to_datetime(value) for value in series)
        _check_timezone_consistency(converted)
        return converted
    if all(is_numeric_dtype(value.dtype) for value in series):
        return tuple(pd.to_numeric(value, errors="coerce") for value in series)
    try:
        converted = tuple(pd.to_datetime(value) for value in series)
    except (TypeError, ValueError):
        return tuple(series)
    _check_timezone_consistency(converted)
    return converted


def _interval_masks(times, starts, ends, spec):
    """Return validity and miss-direction masks for a half-open interval."""
    starts = np.asarray(starts)
    ends = np.asarray(ends)
    start_missing = pd.isna(starts)
    end_missing = pd.isna(ends)

    start_ok = times >= starts if spec.start_closed else times > starts
    end_ok = times <= ends if spec.end_closed else times < ends
    start_ok = np.asarray(start_ok) | start_missing
    end_ok = np.asarray(end_ok) | end_missing

    if spec.allow_open_bounds:
        valid = start_ok & end_ok
    else:
        valid = ~(start_missing | end_missing) & start_ok & end_ok

    before_start = times < starts if spec.start_closed else times <= starts
    after_end = times > ends if spec.end_closed else times >= ends
    before_start = np.asarray(before_start) & ~start_missing
    after_end = np.asarray(after_end) & ~end_missing
    return valid, before_start, after_end, start_missing, end_missing


def _type_masks(times, left_df, right_df, l_idx, r_idx, spec):
    """Return the type-applicability mask and a reason code per candidate."""
    n = len(l_idx)
    reasons = np.full(n, "", dtype=object)

    if spec.right_type is None:
        return np.ones(n, dtype=bool), reasons

    right_values = right_df[spec.right_type].to_numpy()[r_idx]
    right_missing = pd.isna(right_values)
    if right_values.dtype.kind in "biufc":
        wildcard = right_missing
    else:
        wildcard = right_missing | (right_values == "*")

    if spec.left_type is not None:
        left_values = left_df[spec.left_type].to_numpy()[l_idx]
        matched = right_values == left_values
    else:
        wanted = spec.type_value
        if isinstance(wanted, (list, tuple, set, frozenset)):
            matched = np.isin(right_values, list(wanted))
        else:
            matched = right_values == wanted
    matched = wildcard | np.asarray(matched)

    reasons[~matched] = "type_mismatch"
    type_valid = matched.copy()

    if spec.right_type_valid_from is not None:
        _, starts, ends = _coerce_time_values(
            pd.Series(times),
            right_df[spec.right_type_valid_from].iloc[r_idx],
            right_df[spec.right_type_valid_to].iloc[r_idx],
        )
        window_valid, before, after, start_missing, end_missing = _interval_masks(
            np.asarray(times), np.asarray(starts), np.asarray(ends), spec
        )
        window_valid = np.asarray(window_valid)
        missing = np.asarray(start_missing | end_missing)
        # A wildcard area type is not restricted to a type-specific window;
        # the validity interval of the area already governs its applicability.
        constrained = ~wildcard
        type_valid &= window_valid | ~constrained
        reasons[matched & constrained & missing & ~window_valid] = (
            "missing_type_validity"
        )
        reasons[matched & constrained & before & ~missing] = "type_not_yet_applicable"
        reasons[matched & constrained & after & ~missing] = "type_expired"

    return type_valid, reasons


def _rank_values(right_df, column):
    values = right_df[column]
    numeric = pd.to_numeric(values, errors="coerce")
    invalid = values.notna() & numeric.isna()
    if invalid.any():
        examples = values[invalid].head(3).tolist()
        raise ValueError(
            f"Column {column!r} must contain numeric ranks; got {examples!r}."
        )
    return numeric.to_numpy(dtype="float64", na_value=np.nan)


def _area_sort_key(value):
    if isinstance(value, (int, float, np.integer, np.floating)):
        return (0, float(value))
    return (1, str(value))


def _attribution_columns(right_df, spec):
    if spec.attribution_cols:
        return list(spec.attribution_cols)
    excluded = {
        right_df.geometry.name,
        spec.right_valid_from,
        spec.right_valid_to,
        spec.area_id,
        spec.rank,
        spec.right_type,
        spec.right_type_valid_from,
        spec.right_type_valid_to,
    }
    return [column for column in right_df.columns if column not in excluded]


def _versions_conflict(right_rows, attribution_cols):
    """Return True when overlapping versions carry different attribution."""
    if len(right_rows) <= 1:
        return False
    for column in attribution_cols:
        values = right_rows[column].tolist()
        first = values[0]
        for other in values[1:]:
            if not _values_equal(first, other):
                return True
    return False


def _representative_position(right_positions, right_df, right_index_frame, spec):
    """Pick a deterministic representative among equivalent versions.

    Only versions with equal geometry and equal attribution reach this
    function.  The content-based key avoids giving precedence to the original
    DataFrame row order; the positional tie-break is only a final guard for
    fully identical records.
    """

    def key(position):
        row = right_df.iloc[position]
        index = right_index_frame.iloc[position]
        content = [str(row[spec.right_valid_from]), str(row[spec.right_valid_to])]
        content.extend(str(row[column]) for column in spec.right_id)
        content.extend(str(index[column]) for column in right_index_frame.columns)
        return tuple(content) + (int(position),)

    return int(min(right_positions, key=key))


def _selected_values(values, winner_positions):
    """Align right-hand values to left positions, inserting missing values."""
    series = pd.Series(values).reset_index(drop=True)
    positions = pd.Index(winner_positions).where(winner_positions >= 0, -1)
    return series.reindex(positions).array


def _selected_index_values(index_frame, winner_positions):
    if index_frame.shape[1] == 1:
        values = index_frame.iloc[:, 0].to_numpy()
        return _selected_values(values, winner_positions)
    values = index_frame.apply(tuple, axis=1).to_numpy()
    return _selected_values(values, winner_positions)


def _build_conflicts(
    conflict_rows,
    left_df,
    right_df,
    left_index_frame,
    right_index_frame,
    spec,
    attribution_cols,
    geometry_col,
):
    if not conflict_rows:
        return pd.DataFrame(
            columns=[
                "conflict_id",
                "left_position",
                "area_id",
                "rank",
            ]
        )

    records = []
    used = {"conflict_id", "left_position"}
    left_names = []
    for column in left_index_frame.columns:
        left_names.append((column, _unique_name(column, used, suffix="_left"), "index"))
    for column in spec.left_id:
        left_names.append(
            (column, _unique_name(column, used, suffix="_left"), "column")
        )
    right_names = []
    for column in right_index_frame.columns:
        right_names.append((column, _unique_name(column, used), "index"))
    for column in spec.right_id:
        right_names.append((column, _unique_name(column, used), "column"))
    validity_names = [
        (spec.right_valid_from, _unique_name(spec.right_valid_from, used)),
        (spec.right_valid_to, _unique_name(spec.right_valid_to, used)),
    ]
    type_names = []
    if spec.right_type is not None:
        type_names.append((spec.right_type, _unique_name(spec.right_type, used)))
    if spec.right_type_valid_from is not None:
        type_names.extend(
            [
                (
                    spec.right_type_valid_from,
                    _unique_name(spec.right_type_valid_from, used),
                ),
                (
                    spec.right_type_valid_to,
                    _unique_name(spec.right_type_valid_to, used),
                ),
            ]
        )
    attribution_names = [
        (column, _unique_name(column, used)) for column in attribution_cols
    ]

    groups = []
    for left_position, right_positions in conflict_rows:
        left_row = left_df.iloc[left_position]
        left_index = left_index_frame.iloc[left_position]
        versions = []
        for right_position in right_positions:
            right_row = right_df.iloc[right_position]
            right_index = right_index_frame.iloc[right_position]
            record = {
                "left_position": int(left_position),
                "area_id": right_row[spec.area_id],
                "rank": right_row[spec.rank],
            }
            for original, name, kind in left_names:
                record[name] = (
                    left_index[original] if kind == "index" else left_row[original]
                )
            for original, name, kind in right_names:
                record[name] = (
                    right_index[original] if kind == "index" else right_row[original]
                )
            for original, name in validity_names:
                record[name] = right_row[original]
            for original, name in type_names:
                record[name] = right_row[original]
            for original, name in attribution_names:
                record[name] = right_row[original]
            record["right_geometry"] = right_row[geometry_col]
            versions.append(record)

        versions.sort(
            key=lambda record: (
                tuple(str(record[name]) for _, name, _ in left_names)
                + (
                    str(record["area_id"]),
                    str(record["rank"]),
                )
                + tuple(str(record[name]) for _, name, _ in right_names)
                + tuple(str(record[name]) for _, name in validity_names)
            )
        )
        groups.append(versions)

    groups.sort(
        key=lambda versions: tuple(str(versions[0][name]) for _, name, _ in left_names)
    )
    for conflict_id, versions in enumerate(groups, 1):
        for record in versions:
            records.append({"conflict_id": conflict_id, **record})
    return pd.DataFrame(records)


def _candidate_frame(
    left_df,
    right_df,
    l_idx,
    r_idx,
    left_index_frame,
    right_index_frame,
    spec,
    time_valid,
    type_valid,
    selected,
    reasons,
    rank_values,
    area_values,
):
    used = set(_CANDIDATE_RESERVED)
    data = {}
    left_names = set(left_df.columns) | set(left_index_frame.columns)
    right_suffix = "_right"

    def add(name, values, suffix="_right"):
        key = _unique_name(name, used, suffix=suffix)
        data[key] = np.asarray(values)

    data["left_position"] = np.asarray(l_idx, dtype=np.int64)
    used.add("left_position")
    for column in left_df.columns:
        if column == left_df.geometry.name:
            continue
        add(column, left_df[column].to_numpy()[l_idx], suffix="_left")
    for column in left_index_frame.columns:
        add(column, left_index_frame[column].to_numpy()[l_idx], suffix="_left")
    data["right_position"] = np.asarray(r_idx, dtype=np.int64)
    used.add("right_position")
    for column in right_index_frame.columns:
        add(
            column,
            right_index_frame[column].to_numpy()[r_idx],
            suffix=right_suffix,
        )
    for column in right_df.columns:
        if column in (
            right_df.geometry.name,
            spec.rank,
            spec.area_id,
        ):
            continue
        suffix = right_suffix if column in left_names else ""
        add(column, right_df[column].to_numpy()[r_idx], suffix=suffix)

    data["spatial_match"] = np.ones(len(l_idx), dtype=bool)
    data["time_valid"] = np.asarray(time_valid, dtype=bool)
    data["type_valid"] = np.asarray(type_valid, dtype=bool)
    data["effective"] = np.asarray(time_valid & type_valid, dtype=bool)
    data["selected"] = np.asarray(selected, dtype=bool)
    data["reason"] = np.asarray(reasons, dtype=object)
    data["rank"] = np.asarray(rank_values)
    data["area_id"] = np.asarray(area_values)
    data["left_geometry"] = left_df[left_df.geometry.name].to_numpy()[l_idx]
    data["right_geometry"] = right_df[right_df.geometry.name].to_numpy()[r_idx]
    return pd.DataFrame(data)


def sjoin_effective(
    left_df,
    right_df,
    spec,
    predicate="intersects",
    distance=None,
    on_attribute=None,
):
    """Join left records to the service area effective at their observation time.

    This is an opt-in spatial join.  It first obtains the ordinary spatial
    candidates with ``predicate``, then checks each candidate against the
    supplied validity interval and type applicability.  Among the remaining
    candidates it selects the highest ``rank`` and, on a tie, the lowest
    stable ``area_id``.  Input row order is never used to resolve a tie.

    Parameters
    ----------
    left_df, right_df : GeoDataFrame
        Input frames.  Their indexes may contain duplicates; stable
        identifiers should be supplied through ``spec.left_id`` and
        ``spec.right_id`` when the audit needs to be compared across reorderings.
    spec : EffectiveJoinSpec or mapping
        Temporal and selection configuration.  A mapping is converted to
        :class:`EffectiveJoinSpec`.
    predicate : str, default "intersects"
        Spatial predicate passed to the regular spatial index query.
    distance : number or array_like, optional
        Distance for the ``"dwithin"`` predicate, as in :func:`geopandas.sjoin`.
    on_attribute : str, list or tuple, optional
        Additional non-spatial join columns, as in :func:`geopandas.sjoin`.

    Returns
    -------
    EffectiveJoinResult
        Container with ``assignments``, ``candidates``, ``conflicts`` and
        ``unmatched`` frames.  The left geometry is retained in the
        assignment and unmatched frames.

    Notes
    -----
    A left record with a missing observation time is reported as
    ``"missing_time"``.  A left record with no spatial candidate is reported
    as ``"out_of_area"``.  Both groups are also available in
    ``result.unmatched``.  When candidates exist but none is valid at the
    requested time and type, the status is ``"no_effective_candidate"``.
    Overlapping versions with equal rank and area id but different effective
    attribution produce status ``"conflict"`` and are listed in
    ``result.conflicts``; no version is selected for review.
    """
    if isinstance(spec, dict):
        spec = EffectiveJoinSpec(**spec)
    if not isinstance(spec, EffectiveJoinSpec):
        raise TypeError("spec must be an EffectiveJoinSpec or a mapping.")

    _validate_spec(left_df, right_df, spec)
    on_attribute = _maybe_make_list(on_attribute)

    left_geometry_name = left_df.geometry.name
    right_geometry_name = right_df.geometry.name
    attribution_cols = _attribution_columns(right_df, spec)

    left_aug = left_df.copy(deep=False)
    right_aug = right_df.copy(deep=False)
    left_position_col = "_sjoin_effective_left_position"
    right_position_col = "_sjoin_effective_right_position"
    while left_position_col in left_aug.columns:
        left_position_col = "_" + left_position_col
    while right_position_col in right_aug.columns:
        right_position_col = "_" + right_position_col
    left_aug[left_position_col] = np.arange(len(left_aug))
    right_aug[right_position_col] = np.arange(len(right_aug))

    l_idx, r_idx = _geom_predicate_query(
        left_aug,
        right_aug,
        predicate,
        distance,
        on_attribute=on_attribute,
    )
    l_idx = np.asarray(l_idx, dtype=np.intp)
    r_idx = np.asarray(r_idx, dtype=np.intp)

    left_index_frame = _index_frame(left_df.index, "left")
    right_index_frame = _index_frame(right_df.index, "right")

    left_times_series, right_start_series, right_end_series = _coerce_time_values(
        left_df[spec.left_time],
        right_df[spec.right_valid_from],
        right_df[spec.right_valid_to],
    )
    left_times = left_times_series.to_numpy()
    right_starts = right_start_series.to_numpy()
    right_ends = right_end_series.to_numpy()

    candidate_times = left_times[l_idx]
    time_valid, before_start, after_end, start_missing, end_missing = _interval_masks(
        candidate_times,
        right_starts[r_idx],
        right_ends[r_idx],
        spec,
    )
    type_valid, type_reasons = _type_masks(
        candidate_times, left_df, right_df, l_idx, r_idx, spec
    )

    rank_values = _rank_values(right_df, spec.rank)[r_idx]
    area_values = right_df[spec.area_id].to_numpy()[r_idx]
    rank_missing = pd.isna(rank_values)
    area_missing = pd.isna(area_values)
    left_time_missing = pd.isna(left_df[spec.left_time]).to_numpy()[l_idx]

    reasons = np.full(len(l_idx), "candidate", dtype=object)
    reasons[left_time_missing] = "missing_left_time"
    reasons[(type_reasons != "") & ~left_time_missing] = type_reasons[
        (type_reasons != "") & ~left_time_missing
    ]
    reasons[~left_time_missing & (start_missing | end_missing) & ~time_valid] = (
        "missing_validity"
    )
    reasons[~left_time_missing & before_start] = "not_yet_effective"
    reasons[~left_time_missing & after_end] = "expired"
    reasons[~left_time_missing & time_valid & type_valid & rank_missing] = (
        "missing_rank"
    )
    reasons[~left_time_missing & time_valid & type_valid & area_missing] = (
        "missing_area_id"
    )

    selected = np.zeros(len(l_idx), dtype=bool)
    winner_positions = np.full(len(left_df), -1, dtype=np.int64)
    candidate_counts = np.zeros(len(left_df), dtype=np.int64)
    effective_counts = np.zeros(len(left_df), dtype=np.int64)
    status = np.full(len(left_df), "out_of_area", dtype=object)
    selection_reason = np.full(len(left_df), "no_spatial_candidate", dtype=object)
    conflict_rows = []

    missing_time_rows = pd.isna(left_df[spec.left_time]).to_numpy()
    status[missing_time_rows] = "missing_time"
    selection_reason[missing_time_rows] = "left_time_missing"

    if len(l_idx):
        counts = pd.Series(l_idx).value_counts()
        candidate_counts[counts.index.to_numpy()] = counts.to_numpy()

    if len(l_idx):
        order = np.argsort(l_idx, kind="stable")
        sorted_left = l_idx[order]
        boundaries = np.flatnonzero(np.diff(sorted_left)) + 1
        groups = np.split(order, boundaries)
    else:
        groups = []

    for candidate_rows in groups:
        left_position = int(l_idx[candidate_rows[0]])
        if missing_time_rows[left_position]:
            continue

        eligible = time_valid[candidate_rows] & type_valid[candidate_rows]
        effective_counts[left_position] = int(eligible.sum())
        selectable = (
            eligible & ~rank_missing[candidate_rows] & ~area_missing[candidate_rows]
        )
        eligible_rows = candidate_rows[selectable]
        if len(eligible_rows) == 0:
            status[left_position] = "no_effective_candidate"
            selection_reason[left_position] = "no_candidate_valid_at_time_and_type"
            for candidate_row in candidate_rows[eligible]:
                if rank_missing[candidate_row]:
                    reasons[candidate_row] = "missing_rank"
                elif area_missing[candidate_row]:
                    reasons[candidate_row] = "missing_area_id"
            continue

        ranks = rank_values[eligible_rows]
        top_rank = np.nanmax(ranks)
        top_rows = eligible_rows[ranks == top_rank]
        areas = area_values[top_rows]
        chosen_area = min(pd.unique(areas), key=_area_sort_key)
        chosen_rows = top_rows[areas == chosen_area]
        chosen_right_positions = r_idx[chosen_rows]
        chosen_right_rows = right_df.iloc[chosen_right_positions]

        if _versions_conflict(chosen_right_rows, attribution_cols):
            status[left_position] = "conflict"
            selection_reason[left_position] = "conflicting_overlapping_versions"
            conflict_rows.append((left_position, chosen_right_positions))
            reasons[chosen_rows] = "conflict_version"
            for candidate_row in candidate_rows:
                if reasons[candidate_row] != "candidate":
                    continue
                if not time_valid[candidate_row] or not type_valid[candidate_row]:
                    continue
                if rank_values[candidate_row] < top_rank:
                    reasons[candidate_row] = "lower_rank"
                elif area_values[candidate_row] != chosen_area:
                    reasons[candidate_row] = "higher_area_id"
                else:
                    reasons[candidate_row] = "valid_not_selected"
            continue

        winner = _representative_position(
            chosen_right_positions, right_df, right_index_frame, spec
        )
        winner_positions[left_position] = winner
        selected[chosen_rows[r_idx[chosen_rows] == winner][0]] = True
        status[left_position] = "matched"
        selection_reason[left_position] = "highest_rank_then_lowest_area_id"

        for candidate_row in candidate_rows:
            if reasons[candidate_row] != "candidate":
                continue
            if not time_valid[candidate_row] or not type_valid[candidate_row]:
                continue
            right_position = r_idx[candidate_row]
            if right_position == winner:
                reasons[candidate_row] = "selected"
            elif rank_values[candidate_row] < top_rank:
                reasons[candidate_row] = "lower_rank"
            elif area_values[candidate_row] != chosen_area:
                reasons[candidate_row] = "higher_area_id"
            else:
                reasons[candidate_row] = "valid_not_selected"

    candidates = _candidate_frame(
        left_df,
        right_df,
        l_idx,
        r_idx,
        left_index_frame,
        right_index_frame,
        spec,
        time_valid,
        type_valid,
        selected,
        reasons,
        rank_values,
        area_values,
    )

    assigns = left_df.copy()
    reserved_present = sorted(_ASSIGNMENT_RESERVED.intersection(assigns.columns))
    if reserved_present:
        raise ValueError(
            "Left dataframe already contains reserved result column(s): "
            f"{reserved_present}."
        )
    assigns["status"] = status
    assigns["selection_reason"] = selection_reason

    selected_area = np.full(len(left_df), np.nan, dtype=object)
    selected_rank = np.full(len(left_df), np.nan, dtype=float)
    for position, winner in enumerate(winner_positions):
        if winner >= 0:
            selected_area[position] = right_df.iloc[winner][spec.area_id]
            selected_rank[position] = float(right_df.iloc[winner][spec.rank])
    assigns["selected_area_id"] = selected_area
    assigns["selected_rank"] = selected_rank
    assigns["selected_right_position"] = pd.array(
        np.where(winner_positions < 0, pd.NA, winner_positions), dtype="Int64"
    )
    assigns["selected_right_index"] = _selected_index_values(
        right_index_frame, winner_positions
    )
    assigns["candidate_count"] = candidate_counts
    assigns["effective_candidate_count"] = effective_counts

    used_assignment_names = set(assigns.columns) | _ASSIGNMENT_RESERVED
    for position, column in enumerate(right_df.columns):
        if column == right_geometry_name:
            continue
        name = _unique_name(column, used_assignment_names)
        assigns[name] = _selected_values(
            right_df.iloc[:, position].to_numpy(), winner_positions
        )

    conflicts = _build_conflicts(
        conflict_rows,
        left_df,
        right_df,
        left_index_frame,
        right_index_frame,
        spec,
        attribution_cols,
        right_geometry_name,
    )

    assignments = GeoDataFrame(assigns, geometry=left_geometry_name, crs=left_df.crs)
    unmatched = assignments[
        assignments["status"].isin(["out_of_area", "missing_time"])
    ].copy()
    return EffectiveJoinResult(
        assignments=assignments,
        candidates=candidates,
        conflicts=conflicts,
        unmatched=unmatched,
        spec=spec,
    )
