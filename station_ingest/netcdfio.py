"""NetCDF persistence for :class:`IngestProgress`.

The on-disk layout is a single xarray Dataset of flat tables:

* coordinate labels for stations, times and variables;
* one row per known cell with fixed-width per-collector current values;
* submission and revocation records with their cell-level detail rows;
* sealing receipts, completed flags, named ids, sealed ids and tombstones.

Only basic numeric dtypes and fixed-width strings are used so the default
``scipy`` netCDF3 engine can read and write the files without optional
dependencies. Current per-cell values are stored explicitly; records that can
never participate again are represented solely by tombstone ids.
"""

from __future__ import annotations

import math
import os
from collections.abc import Sequence
from typing import Any, Literal

import numpy as np
import xarray as xr

from station_ingest.core import (
    OBS_MISSING,
    OBS_VALUE,
    CellKey,
    IngestProgress,
    Revocation,
    SealingReceipt,
    Submission,
    _SourceState,
)

FORMAT_NAME = "station_ingest_progress"
FORMAT_VERSION = "1.0"
_NO_INDEX = -1


def _string_array(values: Sequence[str]) -> np.ndarray:
    return np.asarray(list(values), dtype=object)


def _index_or_missing(labels: dict[str, int], label: str) -> int:
    return labels.get(label, _NO_INDEX)


def to_dataset(progress: IngestProgress) -> xr.Dataset:
    """Serialize a progress ledger to a flat-table xarray Dataset."""
    label_cells = set(progress._cells)
    for submission in progress._submissions.values():
        label_cells.update(submission.cells)
    for revocation in progress._revocations.values():
        label_cells.update(revocation.cells)
    stations = sorted(
        {cell[0] for cell in label_cells} | set(progress._declared_stations)
    )
    times = sorted(
        {cell[1] for cell in label_cells} | set(progress._declared_times)
    )
    variables = sorted(
        {cell[2] for cell in label_cells} | set(progress._declared_variables)
    )
    station_index = {label: index for index, label in enumerate(stations)}
    time_index = {label: index for index, label in enumerate(times)}
    variable_index = {label: index for index, label in enumerate(variables)}

    cells = sorted(progress._cells)
    cell_count = len(cells)
    source_slots = max(
        (len(states) for states in progress._cells.values()), default=1
    )
    cell_station = np.full(cell_count, _NO_INDEX, dtype=np.int32)
    cell_time = np.full(cell_count, _NO_INDEX, dtype=np.int32)
    cell_variable = np.full(cell_count, _NO_INDEX, dtype=np.int32)
    source_record = np.full((cell_count, source_slots), _NO_INDEX, dtype=np.int32)
    source_kind = np.zeros((cell_count, source_slots), dtype=np.int8)
    source_value = np.full((cell_count, source_slots), math.nan, dtype=np.float64)
    source_collector = np.full((cell_count, source_slots), "", dtype=object)
    for row, cell in enumerate(cells):
        station, time, variable = cell
        cell_station[row] = station_index[station]
        cell_time[row] = time_index[time]
        cell_variable[row] = variable_index[variable]
        states = sorted(progress._cells[cell].items())
        for slot, (collector, state) in enumerate(states):
            source_record[row, slot] = state.record_id
            source_kind[row, slot] = state.kind
            source_value[row, slot] = state.value
            source_collector[row, slot] = collector

    submissions = progress.submissions
    submission_ids = np.asarray(
        [submission.record_id for submission in submissions], dtype=np.int32
    )
    submission_collectors = _string_array(
        [submission.collector for submission in submissions]
    )
    obs_rows = [
        (
            submission.record_id,
            station_index[cell[0]],
            time_index[cell[1]],
            variable_index[cell[2]],
            kind,
            value,
        )
        for submission in submissions
        for cell, (kind, value) in submission.observations
    ]
    obs_count = len(obs_rows)
    obs_submission = np.full(obs_count, _NO_INDEX, dtype=np.int32)
    obs_station = np.full(obs_count, _NO_INDEX, dtype=np.int32)
    obs_time = np.full(obs_count, _NO_INDEX, dtype=np.int32)
    obs_variable = np.full(obs_count, _NO_INDEX, dtype=np.int32)
    obs_kind = np.zeros(obs_count, dtype=np.int8)
    obs_value = np.full(obs_count, math.nan, dtype=np.float64)
    for row, (record_id, station_idx, time_idx, variable_idx, kind, value) in enumerate(
        obs_rows
    ):
        obs_submission[row] = record_id
        obs_station[row] = station_idx
        obs_time[row] = time_idx
        obs_variable[row] = variable_idx
        obs_kind[row] = kind
        obs_value[row] = value

    revocations = progress.revocations
    revocation_ids = np.asarray(
        [revocation.record_id for revocation in revocations], dtype=np.int32
    )
    revocation_targets = np.asarray(
        [revocation.target_id for revocation in revocations], dtype=np.int32
    )
    revocation_collectors = _string_array(
        [revocation.collector for revocation in revocations]
    )
    rev_rows = [
        (
            revocation.record_id,
            station_index[cell[0]],
            time_index[cell[1]],
            variable_index[cell[2]],
        )
        for revocation in revocations
        for cell in revocation.cells
    ]
    rev_count = len(rev_rows)
    rev_cell_revocation = np.full(rev_count, _NO_INDEX, dtype=np.int32)
    rev_cell_station = np.full(rev_count, _NO_INDEX, dtype=np.int32)
    rev_cell_time = np.full(rev_count, _NO_INDEX, dtype=np.int32)
    rev_cell_variable = np.full(rev_count, _NO_INDEX, dtype=np.int32)
    for row, (record_id, station_idx, time_idx, variable_idx) in enumerate(rev_rows):
        rev_cell_revocation[row] = record_id
        rev_cell_station[row] = station_idx
        rev_cell_time[row] = time_idx
        rev_cell_variable[row] = variable_idx

    receipts = progress.receipts
    receipt_keys = np.asarray(
        [receipt.receipt_id for receipt in receipts], dtype=np.int32
    )
    receipt_completed = np.asarray(
        [int(receipt.completed) for receipt in receipts], dtype=np.int8
    )
    named_rows = [
        (receipt.receipt_id, record_id)
        for receipt in receipts
        for record_id in sorted(receipt.named)
    ]
    named_receipt = np.asarray(
        [row[0] for row in named_rows], dtype=np.int32
    )
    named_id = np.asarray([row[1] for row in named_rows], dtype=np.int32)

    data_vars: dict[str, tuple[tuple[str, ...], np.ndarray]] = {
        "cell_station": (("cell",), cell_station),
        "cell_time": (("cell",), cell_time),
        "cell_variable": (("cell",), cell_variable),
        "source_record": (("cell", "source"), source_record),
        "source_kind": (("cell", "source"), source_kind),
        "source_value": (("cell", "source"), source_value),
        "source_collector": (("cell", "source"), source_collector),
        "submission_id": (("submission",), submission_ids),
        "submission_collector": (("submission",), submission_collectors),
        "obs_submission": (("obs",), obs_submission),
        "obs_station": (("obs",), obs_station),
        "obs_time": (("obs",), obs_time),
        "obs_variable": (("obs",), obs_variable),
        "obs_kind": (("obs",), obs_kind),
        "obs_value": (("obs",), obs_value),
        "revocation_id": (("revocation",), revocation_ids),
        "revocation_target": (("revocation",), revocation_targets),
        "revocation_collector": (("revocation",), revocation_collectors),
        "rev_cell_revocation": (("rev_cell",), rev_cell_revocation),
        "rev_cell_station": (("rev_cell",), rev_cell_station),
        "rev_cell_time": (("rev_cell",), rev_cell_time),
        "rev_cell_variable": (("rev_cell",), rev_cell_variable),
        "receipt_key": (("receipt",), receipt_keys),
        "receipt_completed": (("receipt",), receipt_completed),
        "named_receipt": (("named",), named_receipt),
        "named_id": (("named",), named_id),
        "tombstone_id": (
            ("tombstone",),
            np.asarray(sorted(progress.tombstones), dtype=np.int32),
        ),
        "sealed_id": (
            ("sealed",),
            np.asarray(sorted(progress.sealed_records), dtype=np.int32),
        ),
    }
    dataset = xr.Dataset(
        data_vars,
        coords={
            "station_id": (("station",), _string_array(stations)),
            "time_id": (("time",), _string_array(times)),
            "variable_name": (("variable",), _string_array(variables)),
        },
        attrs={
            "format_name": FORMAT_NAME,
            "format_version": FORMAT_VERSION,
            "generation": np.int32(progress.generation),
        },
    )
    return dataset


def from_dataset(dataset: xr.Dataset) -> IngestProgress:
    """Rebuild a progress ledger from a serialized flat-table Dataset."""
    attrs = dataset.attrs
    if attrs.get("format_name") != FORMAT_NAME:
        raise ValueError("dataset is not a station ingest progress file")
    stations = [str(label) for label in dataset["station_id"].values]
    times = [str(label) for label in dataset["time_id"].values]
    variables = [str(label) for label in dataset["variable_name"].values]

    def cell_of(station_idx: int, time_idx: int, variable_idx: int) -> CellKey:
        return stations[station_idx], times[time_idx], variables[variable_idx]

    cells: dict[CellKey, dict[str, _SourceState]] = {}
    cell_station = dataset["cell_station"].values
    cell_time = dataset["cell_time"].values
    cell_variable = dataset["cell_variable"].values
    source_record = dataset["source_record"].values
    source_kind = dataset["source_kind"].values
    source_value = dataset["source_value"].values
    source_collector = dataset["source_collector"].values
    for row in range(cell_station.shape[0]):
        cell = cell_of(
            int(cell_station[row]), int(cell_time[row]), int(cell_variable[row])
        )
        states: dict[str, _SourceState] = {}
        for slot in range(source_record.shape[1]):
            record_id = int(source_record[row, slot])
            if record_id == _NO_INDEX:
                continue
            collector = str(source_collector[row, slot])
            states[collector] = _SourceState(
                record_id=record_id,
                kind=int(source_kind[row, slot]),
                value=float(source_value[row, slot]),
            )
        cells[cell] = states

    submission_records: dict[int, Submission] = {}
    submission_collectors = {
        int(record_id): str(collector)
        for record_id, collector in zip(
            dataset["submission_id"].values,
            dataset["submission_collector"].values,
            strict=True,
        )
    }
    grouped_obs: dict[int, list[tuple[CellKey, tuple[int, float]]]] = {}
    for row in range(dataset.sizes["obs"]):
        record_id = int(dataset["obs_submission"].values[row])
        kind = int(dataset["obs_kind"].values[row])
        value = float(dataset["obs_value"].values[row])
        if kind == OBS_MISSING:
            value = math.nan
        elif kind != OBS_VALUE:
            raise ValueError(f"unknown observation kind {kind}")
        cell = cell_of(
            int(dataset["obs_station"].values[row]),
            int(dataset["obs_time"].values[row]),
            int(dataset["obs_variable"].values[row]),
        )
        grouped_obs.setdefault(record_id, []).append((cell, (kind, value)))
    for record_id, collector in submission_collectors.items():
        submission_records[record_id] = Submission(
            record_id, collector, grouped_obs.get(record_id, [])
        )

    revocation_records: dict[int, Revocation] = {}
    revocation_targets: dict[int, int] = {}
    revocation_collectors: dict[int, str] = {}
    for record_id, target, collector in zip(
        dataset["revocation_id"].values,
        dataset["revocation_target"].values,
        dataset["revocation_collector"].values,
        strict=True,
    ):
        rid = int(record_id)
        revocation_targets[rid] = int(target)
        revocation_collectors[rid] = str(collector)
    grouped_cells: dict[int, list[CellKey]] = {}
    for row in range(dataset.sizes["rev_cell"]):
        record_id = int(dataset["rev_cell_revocation"].values[row])
        cell = cell_of(
            int(dataset["rev_cell_station"].values[row]),
            int(dataset["rev_cell_time"].values[row]),
            int(dataset["rev_cell_variable"].values[row]),
        )
        grouped_cells.setdefault(record_id, []).append(cell)
    for record_id, target_id in revocation_targets.items():
        revocation_records[record_id] = Revocation(
            record_id,
            target_id,
            revocation_collectors[record_id],
            grouped_cells.get(record_id, []),
        )

    receipts: list[SealingReceipt] = []
    named_by_receipt: dict[int, set[int]] = {}
    for row in range(dataset.sizes["named"]):
        receipt_id = int(dataset["named_receipt"].values[row])
        named_by_receipt.setdefault(receipt_id, set()).add(
            int(dataset["named_id"].values[row])
        )
    for row in range(dataset.sizes["receipt"]):
        receipt_id = int(dataset["receipt_key"].values[row])
        receipt = SealingReceipt(receipt_id, named_by_receipt.get(receipt_id, set()))
        receipt.completed = bool(int(dataset["receipt_completed"].values[row]))
        receipts.append(receipt)

    tombstones = {int(record_id) for record_id in dataset["tombstone_id"].values}
    sealed = {int(record_id) for record_id in dataset["sealed_id"].values}
    generation = int(attrs.get("generation", 0))
    return IngestProgress._from_parts(
        cells=cells,
        submissions=submission_records.values(),
        revocations=revocation_records.values(),
        receipts=receipts,
        sealed=sealed,
        tombstones=tombstones,
        generation=generation,
        grid=(stations, times, variables),
    )


Engine = Literal["netcdf4", "scipy", "h5netcdf"]


def save_progress(
    progress: IngestProgress,
    path: str | os.PathLike[str],
    engine: Engine | None = "scipy",
    **kwargs: Any,
) -> None:
    """Write progress to a NetCDF file, overwriting any existing file."""
    to_dataset(progress).to_netcdf(path, engine=engine, **kwargs)


def load_progress(
    path: str | os.PathLike[str],
    engine: Engine | None = "scipy",
    **kwargs: Any,
) -> IngestProgress:
    """Read progress from a NetCDF file written by :func:`save_progress`."""
    # scipy's mmap backend warns when zero-length dimensions are closed; the
    # copied in-memory dataset does not need the mapping.
    if engine in (None, "scipy"):
        kwargs.setdefault("mmap", False)
    with xr.open_dataset(path, engine=engine, **kwargs) as dataset:
        loaded = dataset.load()
    return from_dataset(loaded)
