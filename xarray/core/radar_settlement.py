"""Settle radar altitudes against versioned survey reports.

This module implements settlement of radar-ingested datasets: for every
sampled cell of a radar table (identified by a station and a scan time) a
settlement manifest gives a cutoff time, and the settled altitude is taken
from the highest-priority survey report version that

* was published at or before the cell's cutoff time (a version published
  exactly at the cutoff counts), and
* covers the cell's station mileage and scan time, using half-open
  intervals ``[start, end)`` for both.

A corrected version of a report replaces the whole report (mileage range
and scan period may both change); superseded versions immediately stop
applying, also inside their original ranges.  Cells whose selected report
actively reports a missing value or whose unit is not verified fall back
to the measured altitude, recording the selected report and the fallback
reason.  Cells without any applicable report silently fall back to the
measured altitude.  Cells not listed in the manifest are left empty.

The implementation is report-centric: cells are grouped by cutoff time,
report versions are applied as time-ordered events, and for every cutoff
group the reports are swept once in priority order, touching only cells
inside each report's mileage/scan rectangle.  It never scans the whole
corridor per cell, never enumerates all overlapping candidates of a cell,
never replicates report rows per covered station or scan time, and never
reads the radar data variables, so settlement works on lazily loaded
datasets without triggering any computation.
"""

from __future__ import annotations

from collections.abc import Hashable, Mapping, Sequence
from typing import Any, Literal

import numpy as np
import pandas as pd

from xarray.core.dataset import Dataset
from xarray.core.extensions import register_dataset_accessor

#: Fallback reason used when no report covers the cell at its cutoff time.
REASON_NO_APPLICABLE_REPORT = "no_applicable_report"
#: Fallback reason used when the selected report actively reports missing.
REASON_REPORT_MISSING = "report_missing"
#: Fallback reason used when the selected report's unit is not verified.
REASON_UNIT_UNVERIFIED = "unit_unverified"

_UNIT_TO_METERS = {"m": 1.0, "km": 1000.0}
_NAT_INT = np.datetime64("NaT").astype("datetime64[ns]").astype("int64")

_MANIFEST_COLUMNS = ("station", "scan_time", "cutoff")
_STATION_COLUMNS = ("station", "mileage")
_REPORT_COLUMNS = (
    "report",
    "version",
    "publish_time",
    "mileage_min",
    "mileage_max",
    "scan_min",
    "scan_max",
    "altitude",
    "unit",
    "unit_verified",
    "missing",
)

TableLike = Any
_TimeKind = Literal["datetime", "numeric"]


def _as_frame(table: TableLike, required: Sequence[str], name: str) -> pd.DataFrame:
    if isinstance(table, pd.DataFrame):
        frame = table
    elif isinstance(table, Dataset):
        frame = table.to_dataframe().reset_index()
    elif isinstance(table, Mapping):
        frame = pd.DataFrame(dict(table))
    else:
        frame = pd.DataFrame(table)
    missing = [column for column in required if column not in frame.columns]
    if missing:
        raise ValueError(f"{name} table is missing required columns: {missing}")
    return frame


def _time_keys(values: Any, name: str) -> tuple[np.ndarray, _TimeKind]:
    """Convert datetime-like or numeric values to sortable keys."""
    if isinstance(values, pd.Series | pd.Index):
        values = values.to_numpy()
    arr = np.asarray(values)
    keys: np.ndarray
    if arr.dtype.kind == "M":
        keys = arr.astype("datetime64[ns]").astype("int64")
        kind: _TimeKind = "datetime"
        invalid = keys == _NAT_INT
    elif arr.dtype.kind in "iuf":
        keys = arr.astype("float64")
        kind = "numeric"
        invalid = np.isnan(keys)
    else:
        try:
            converted = pd.to_datetime(arr)
        except (TypeError, ValueError) as err:
            raise ValueError(
                f"{name} must contain datetimes or numbers, got {arr.dtype!r}"
            ) from err
        keys = converted.to_numpy(dtype="datetime64[ns]").astype("int64")
        kind = "datetime"
        invalid = keys == _NAT_INT
    if invalid.any():
        raise ValueError(f"{name} contains missing (NaT/NaN) values")
    return keys, kind


def _require_same_kind(kinds: Sequence[_TimeKind], names: Sequence[str]) -> _TimeKind:
    if len(set(kinds)) != 1:
        raise ValueError(
            "inconsistent time-like values: "
            + ", ".join(
                f"{name} is {kind}" for name, kind in zip(names, kinds, strict=True)
            )
        )
    return kinds[0]


def _label_positions(labels: np.ndarray, values: Any, name: str) -> np.ndarray:
    positions = pd.Index(labels).get_indexer(np.asarray(values))
    if (positions < 0).any():
        unknown = pd.unique(np.asarray(values)[positions < 0])
        raise ValueError(
            f"{name} contains labels not present on the axis: {list(unknown)!r}"
        )
    return positions


def settle_altitude(
    dataset: Dataset,
    manifest: TableLike,
    stations: TableLike,
    reports: TableLike,
    *,
    priority: Sequence[Hashable],
    station_dim: str = "station",
    scan_dim: str = "scan_time",
    measured_coord: str = "measured_altitude",
) -> Dataset:
    """Settle altitudes of a radar dataset against versioned survey reports.

    Parameters
    ----------
    dataset : Dataset
        Radar table with dimensions ``station_dim`` and ``scan_dim`` and a
        measured-altitude coordinate ``measured_coord`` (either along
        ``station_dim`` or along both dimensions).  Its data variables are
        never read, so they may stay lazy.
    manifest : DataFrame, Dataset or mapping
        Settlement manifest with columns ``station``, ``scan_time`` and
        ``cutoff``: one row per sampled cell with that cell's settlement
        cutoff time.  Cells not listed here are left empty.
    stations : DataFrame, Dataset or mapping
        Station table with columns ``station`` and ``mileage`` (position of
        the station along the corridor, in km).
    reports : DataFrame, Dataset or mapping
        One row per survey report version with columns ``report`` (report
        number), ``version``, ``publish_time``, ``mileage_min``,
        ``mileage_max``, ``scan_min``, ``scan_max`` (half-open intervals),
        ``altitude``, ``unit`` (``"m"`` or ``"km"``), ``unit_verified``
        (bool) and ``missing`` (bool, actively reported missing).
    priority : sequence of hashable
        Report numbers from highest to lowest priority.  Reports missing
        from this sequence rank below the listed ones, in order of
        appearance.
    station_dim : str, default: "station"
        Name of the station dimension.
    scan_dim : str, default: "scan_time"
        Name of the scan time dimension.
    measured_coord : str, default: "measured_altitude"
        Name of the measured-altitude coordinate used as fallback.

    Returns
    -------
    Dataset
        The input dataset with four new auxiliary coordinates on
        ``(station_dim, scan_dim)``: ``settled_altitude`` (meters, NaN for
        unsampled cells), ``settled_report`` and ``settled_report_version``
        (the adopted report, empty where none applied) and
        ``settled_fallback_reason`` (empty when a report value was used).
        Existing coordinates and indexes, including any index on the
        measured-altitude coordinate, are preserved.
    """
    if station_dim not in dataset.dims:
        raise ValueError(f"dataset has no {station_dim!r} dimension")
    if scan_dim not in dataset.dims:
        raise ValueError(f"dataset has no {scan_dim!r} dimension")
    if measured_coord not in dataset.variables:
        raise ValueError(f"dataset has no {measured_coord!r} coordinate")
    if station_dim not in dataset.coords or scan_dim not in dataset.coords:
        raise ValueError(
            f"dataset needs coordinates on both {station_dim!r} and {scan_dim!r}"
        )

    station_labels = np.asarray(dataset[station_dim].values)
    scan_labels = np.asarray(dataset[scan_dim].values)
    n_station = station_labels.size
    n_scan = scan_labels.size

    scan_keys, scan_kind = _time_keys(scan_labels, f"{scan_dim!r} coordinate")

    man = _as_frame(manifest, _MANIFEST_COLUMNS, "manifest")
    sta = _as_frame(stations, _STATION_COLUMNS, "stations")
    rep = _as_frame(reports, _REPORT_COLUMNS, "reports")

    cell_st = _label_positions(
        station_labels, man["station"].to_numpy(), "manifest station"
    )
    man_scan_keys, man_scan_kind = _time_keys(man["scan_time"], "manifest scan_time")
    cutoff_keys, cutoff_kind = _time_keys(man["cutoff"], "manifest cutoff")
    if len(man):
        _require_same_kind(
            (scan_kind, man_scan_kind, cutoff_kind),
            (f"{scan_dim!r} coordinate", "manifest scan_time", "manifest cutoff"),
        )
    cell_sc = _label_positions(scan_keys, man_scan_keys, "manifest scan_time")

    n_cells = len(man)
    flat = cell_st.astype(np.int64) * max(n_scan, 1) + cell_sc
    if np.unique(flat).size != n_cells:
        raise ValueError("manifest contains duplicate (station, scan_time) cells")

    if sta["station"].duplicated().any():
        raise ValueError("stations table contains duplicate stations")
    mileage = sta.set_index("station")["mileage"].reindex(station_labels)
    if mileage.isna().any():
        missing_stations = list(station_labels[mileage.isna().to_numpy()])
        raise ValueError(f"stations table has no mileage for: {missing_stations!r}")
    station_mileage = mileage.to_numpy(dtype="float64")

    pub_keys, pub_kind = _time_keys(rep["publish_time"], "reports publish_time")
    scan_min_keys, scan_min_kind = _time_keys(rep["scan_min"], "reports scan_min")
    scan_max_keys, scan_max_kind = _time_keys(rep["scan_max"], "reports scan_max")
    if len(rep):
        _require_same_kind(
            (scan_kind, pub_kind, scan_min_kind, scan_max_kind),
            (
                f"{scan_dim!r} coordinate",
                "reports publish_time",
                "reports scan_min",
                "reports scan_max",
            ),
        )
    mileage_min = rep["mileage_min"].to_numpy(dtype="float64")
    mileage_max = rep["mileage_max"].to_numpy(dtype="float64")
    altitude = rep["altitude"].to_numpy(dtype="float64")
    unit_verified = rep["unit_verified"].to_numpy(dtype=bool)
    reported_missing = rep["missing"].to_numpy(dtype=bool)

    factor = rep["unit"].map(_UNIT_TO_METERS)
    unknown_unit = unit_verified & factor.isna().to_numpy()
    if unknown_unit.any():
        bad_units = pd.unique(rep["unit"][unknown_unit])
        raise ValueError(f"reports use verified but unknown units: {list(bad_units)!r}")
    altitude_m = altitude * factor.fillna(np.nan).to_numpy(dtype="float64")

    priority = list(priority)
    if len(set(priority)) != len(priority):
        raise ValueError("priority contains duplicate report numbers")
    listed = set(priority)
    report_ids = list(pd.unique(rep["report"]))
    in_table = set(report_ids)
    ordered_ids = [r for r in priority if r in in_table]
    ordered_ids += [r for r in report_ids if r not in listed]
    slot_of_id = {report_id: slot for slot, report_id in enumerate(ordered_ids)}
    row_slot = rep["report"].map(slot_of_id).to_numpy(dtype=np.int64)

    n_reports = len(ordered_ids)
    report_id_of_row = rep["report"].to_numpy()
    version_of_row = rep["version"].to_numpy()

    # Sort versions of every report by (publish time, version); a corrected
    # version fully replaces the previous content of its report.
    version_order = (
        pd.DataFrame(
            {
                "slot": row_slot,
                "pub": pub_keys,
                "version": version_of_row,
                "row": np.arange(len(rep)),
            }
        )
        .sort_values(["slot", "pub", "version"], kind="stable")["row"]
        .to_numpy()
    )
    # Apply publications as time-ordered events; the stable sort keeps the
    # (slot, version) order for equal publish times, so the highest version
    # published at the same instant ends up active.
    events = version_order[np.argsort(pub_keys[version_order], kind="stable")]
    event_pub = pub_keys[events]

    key_dtype = np.int64 if scan_kind == "datetime" else np.float64
    act_valid = np.zeros(n_reports, dtype=bool)
    act_mlo = np.zeros(n_reports, dtype="float64")
    act_mhi = np.zeros(n_reports, dtype="float64")
    act_tlo = np.zeros(n_reports, dtype=key_dtype)
    act_thi = np.zeros(n_reports, dtype=key_dtype)
    act_row = np.full(n_reports, -1, dtype=np.int64)

    # Stations sorted by mileage enable range lookups without scanning the
    # whole corridor.
    st_order = np.argsort(station_mileage, kind="stable")
    st_mileage_sorted = station_mileage[st_order]
    cell_scan_key = (
        scan_keys[cell_sc] if n_cells else np.empty(0, dtype=scan_keys.dtype)
    )

    chosen_row = np.full(n_cells, -1, dtype=np.int64)
    if n_cells:
        cell_order_by_cutoff = np.argsort(cutoff_keys, kind="stable")
        sorted_cutoffs = cutoff_keys[cell_order_by_cutoff]
        group_starts = np.flatnonzero(
            np.concatenate(([True], sorted_cutoffs[1:] != sorted_cutoffs[:-1]))
        )
        group_bounds = np.append(group_starts, n_cells)

        event_ptr = 0
        for group_pos in range(group_starts.size):
            cutoff = sorted_cutoffs[group_starts[group_pos]]
            while event_ptr < events.size and event_pub[event_ptr] <= cutoff:
                row = events[event_ptr]
                slot = row_slot[row]
                act_valid[slot] = True
                act_mlo[slot] = mileage_min[row]
                act_mhi[slot] = mileage_max[row]
                act_tlo[slot] = scan_min_keys[row]
                act_thi[slot] = scan_max_keys[row]
                act_row[slot] = row
                event_ptr += 1

            cells = cell_order_by_cutoff[
                group_bounds[group_pos] : group_bounds[group_pos + 1]
            ]
            group_st = cell_st[cells]
            group_scan_key = cell_scan_key[cells]
            group_size = cells.size

            # Bin the group's cells by station so every report only touches
            # cells inside its own mileage range.
            bin_count = np.bincount(group_st, minlength=n_station)
            bin_start = np.zeros(n_station + 1, dtype=np.int64)
            np.cumsum(bin_count, out=bin_start[1:])
            cell_bin_order = np.argsort(group_st, kind="stable")

            remaining = np.ones(group_size, dtype=bool)
            bin_remaining = bin_count.astype(np.int64)
            n_remaining = group_size
            group_choice = np.full(group_size, -1, dtype=np.int64)

            # Sweep reports from highest to lowest priority; the first
            # report covering a still-unsettled cell wins it, so no cell
            # ever enumerates all of its overlapping candidates.
            for slot in range(n_reports):
                if n_remaining == 0:
                    break
                if not act_valid[slot]:
                    continue
                lo = np.searchsorted(st_mileage_sorted, act_mlo[slot], side="left")
                hi = np.searchsorted(st_mileage_sorted, act_mhi[slot], side="left")
                if lo >= hi:
                    continue
                covered = st_order[lo:hi]
                covered = covered[bin_remaining[covered] > 0]
                if covered.size == 0:
                    continue
                tlo = act_tlo[slot]
                thi = act_thi[slot]
                active_row = act_row[slot]
                for station_pos in covered:
                    idx = cell_bin_order[
                        bin_start[station_pos] : bin_start[station_pos]
                        + bin_count[station_pos]
                    ]
                    hit_mask = (
                        remaining[idx]
                        & (group_scan_key[idx] >= tlo)
                        & (group_scan_key[idx] < thi)
                    )
                    hits = idx[hit_mask]
                    if hits.size:
                        group_choice[hits] = active_row
                        remaining[hits] = False
                        n_remaining -= hits.size
                        bin_remaining[station_pos] -= hits.size
            chosen_row[cells] = group_choice

    measured = dataset[measured_coord]
    if measured.dims == (station_dim,):
        measured_cells = np.asarray(measured.values, dtype="float64")[cell_st]
    elif set(measured.dims) == {station_dim, scan_dim}:
        measured_grid = measured.transpose(station_dim, scan_dim).values
        measured_cells = np.asarray(measured_grid, dtype="float64")[cell_st, cell_sc]
    else:
        raise ValueError(
            f"{measured_coord!r} must be dimensioned along {station_dim!r} "
            f"or along both {station_dim!r} and {scan_dim!r}"
        )

    has_report = chosen_row >= 0
    safe_rows = np.clip(chosen_row, 0, None)
    chosen_missing = (
        reported_missing[safe_rows] if len(rep) else np.zeros(n_cells, bool)
    )
    chosen_unverified = (
        ~unit_verified[safe_rows] if len(rep) else np.zeros(n_cells, bool)
    )
    use_report = has_report & ~chosen_missing & ~chosen_unverified

    settled_cells = np.where(
        use_report, altitude_m[safe_rows] if len(rep) else np.nan, measured_cells
    )

    reason_cells = np.full(n_cells, "", dtype=object)
    if n_cells:
        reason_cells[~has_report] = REASON_NO_APPLICABLE_REPORT
        reason_cells[has_report & chosen_missing] = REASON_REPORT_MISSING
        reason_cells[has_report & ~chosen_missing & chosen_unverified] = (
            REASON_UNIT_UNVERIFIED
        )

    report_cells = np.full(n_cells, "", dtype=object)
    if len(rep):
        report_cells[has_report] = report_id_of_row[safe_rows[has_report]]

    version_column = rep["version"]
    if pd.api.types.is_numeric_dtype(version_column.dtype):
        version_cells = np.full(n_cells, np.nan)
        if len(rep):
            version_cells[has_report] = version_of_row.astype("float64")[
                safe_rows[has_report]
            ]
    else:
        version_cells = np.full(n_cells, "", dtype=object)
        if len(rep):
            version_cells[has_report] = version_of_row[safe_rows[has_report]]

    shape = (n_station, n_scan)
    settled_grid = np.full(shape, np.nan)
    settled_grid[cell_st, cell_sc] = settled_cells
    report_grid = np.full(shape, "", dtype=object)
    report_grid[cell_st, cell_sc] = report_cells
    reason_grid = np.full(shape, "", dtype=object)
    reason_grid[cell_st, cell_sc] = reason_cells
    if version_cells.dtype == object:
        version_grid = np.full(shape, "", dtype=object)
    else:
        version_grid = np.full(shape, np.nan)
    version_grid[cell_st, cell_sc] = version_cells

    return dataset.assign_coords(
        {
            "settled_altitude": (
                (station_dim, scan_dim),
                settled_grid,
                {"units": "m", "long_name": "settled altitude"},
            ),
            "settled_report": ((station_dim, scan_dim), report_grid),
            "settled_report_version": ((station_dim, scan_dim), version_grid),
            "settled_fallback_reason": ((station_dim, scan_dim), reason_grid),
        }
    )


@register_dataset_accessor("radar")
class RadarAccessor:
    """Radar-specific dataset operations."""

    def __init__(self, dataset: Dataset) -> None:
        self._dataset = dataset

    def settle_altitude(
        self,
        manifest: TableLike,
        stations: TableLike,
        reports: TableLike,
        *,
        priority: Sequence[Hashable],
        station_dim: str = "station",
        scan_dim: str = "scan_time",
        measured_coord: str = "measured_altitude",
    ) -> Dataset:
        """Settle altitudes against versioned survey reports.

        See :func:`xarray.core.radar_settlement.settle_altitude` for details.
        """
        return settle_altitude(
            self._dataset,
            manifest,
            stations,
            reports,
            priority=priority,
            station_dim=station_dim,
            scan_dim=scan_dim,
            measured_coord=measured_coord,
        )
