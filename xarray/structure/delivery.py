"""Explicit delivery selection for :func:`xarray.merge`.

When several instrument datasets are merged for downstream delivery, a single
raw reading may have to be shared between multiple delivery records while each
receiving batch only accepts a subset of the instruments and each instrument
limits how many distinct raw readings may leave the warehouse in one
submission. :class:`DeliveryOptions` carries an ordered delivery manifest and
the explicit rules needed to pick, for every manifest row, which instrument
reading is actually delivered.

The selection is purely additive to :func:`xarray.merge`: without
``delivery=`` the existing alignment, conflict and attribute rules apply
unchanged.
"""

from __future__ import annotations

import math
from collections.abc import Collection, Hashable, Iterable, Mapping, Sequence
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:
    from xarray.core.dataset import Dataset

#: Dimension (and record-id coordinate) of the delivered dataset.
DELIVERY_RECORD_DIM = "delivery_record"
#: Delivered temperature reading; ``NaN`` marks rows without a reading.
TEMPERATURE_VAR = "temperature"
#: Instrument that actually supplied the delivered reading.
DELIVERY_INSTRUMENT_VAR = "delivery_instrument"
#: License carried by the delivered reading.
DELIVERY_LICENSE_VAR = "delivery_license"
#: Processing specification version of the delivered reading.
PROCESSING_SPEC_VAR = "processing_specification"
#: Calibration date of the delivering instrument (missing dates stay empty).
CALIBRATION_DATE_VAR = "calibration_date"
#: Per-row outcome, distinguishing the two kinds of empty readings.
DELIVERY_STATUS_VAR = "delivery_status"

#: A deliverable reading was assigned to the row.
STATUS_DELIVERED = "delivered"
#: The row is quality-flagged and therefore never competes for a reading.
STATUS_QUALITY_FLAGGED = "quality_flagged"
#: No licensed, quota-compatible reading could be assigned.
STATUS_UNASSIGNED = "unassigned"


class DeliveryOptions:
    """Rules for selecting deliverable instrument readings during a merge.

    Parameters
    ----------
    manifest : Dataset
        Ordered delivery manifest. Each row is one delivery record. It must
        contain a unique record-id dimension plus columns for the receiving
        batch, station, timestamp and quality-flag state. Rows sharing the
        same station and timestamp are allowed.
    instrument_priority : sequence of hashable
        Explicit instrument priority, highest priority first. Instrument
        identity is taken from the ``instrument_coord`` coordinate/attribute
        of each instrument dataset. Every merged instrument must be listed.
    licenses : mapping
        Accepted license identifiers for each receiving batch, keyed by the
        batch labels used in the manifest.
    read_quotas : mapping, optional
        Maximum number of distinct raw readings an instrument may deliver in
        this submission, keyed by instrument. Instruments without an entry
        are unrestricted. The ``read_quota_attr`` dataset attribute, when
        present, takes precedence over an entry here.
    record_dim, station_coord, time_coord, batch_coord, quality_flag_coord : hashable
        Names of the manifest columns.
    instrument_coord : hashable
        Coordinate or attribute naming the instrument on each instrument
        dataset.
    license_attr, processing_spec_attr, calibration_date_attr, read_quota_attr : str
        Attribute names on the instrument datasets carrying the license,
        processing specification version, calibration date and per-submission
        read quota.
    """

    def __init__(
        self,
        manifest: Dataset,
        *,
        instrument_priority: Sequence[Hashable],
        licenses: Mapping[Any, Collection[Hashable]],
        read_quotas: Mapping[Hashable, int] | None = None,
        record_dim: Hashable = DELIVERY_RECORD_DIM,
        station_coord: Hashable = "station",
        time_coord: Hashable = "time",
        batch_coord: Hashable = "batch",
        quality_flag_coord: Hashable = "quality_flagged",
        instrument_coord: Hashable = "instrument",
        license_attr: str = "license",
        processing_spec_attr: str = "processing_specification",
        calibration_date_attr: str = "calibration_date",
        read_quota_attr: str = "read_quota",
    ) -> None:
        self.manifest = manifest
        self.instrument_priority = list(instrument_priority)
        self.licenses = dict(licenses)
        self.read_quotas = {} if read_quotas is None else dict(read_quotas)
        self.record_dim = record_dim
        self.station_coord = station_coord
        self.time_coord = time_coord
        self.batch_coord = batch_coord
        self.quality_flag_coord = quality_flag_coord
        self.instrument_coord = instrument_coord
        self.license_attr = license_attr
        self.processing_spec_attr = processing_spec_attr
        self.calibration_date_attr = calibration_date_attr
        self.read_quota_attr = read_quota_attr

    def __repr__(self) -> str:
        return (
            f"<DeliveryOptions records={len(self.manifest[self.record_dim])} "
            f"instruments={self.instrument_priority!r}>"
        )


class _DeliveryRow:
    __slots__ = (
        "batch",
        "index",
        "quality_flagged",
        "record_id",
        "station",
        "time",
    )

    def __init__(
        self,
        index: int,
        record_id: Hashable,
        batch: Hashable,
        station: Hashable,
        time: Any,
        quality_flagged: bool,
    ) -> None:
        self.index = index
        self.record_id = record_id
        self.batch = batch
        self.station = station
        self.time = time
        self.quality_flagged = quality_flagged


class _InstrumentReading:
    """A single candidate reading offered by one instrument."""

    __slots__ = (
        "calibration_date",
        "instrument",
        "license",
        "processing_spec",
        "station",
        "time",
        "value",
    )

    def __init__(
        self,
        instrument: Hashable,
        station: Hashable,
        time: Any,
        value: Any,
        license: Hashable,
        processing_spec: Hashable,
        calibration_date: Any,
    ) -> None:
        self.instrument = instrument
        self.station = station
        self.time = time
        self.value = value
        self.license = license
        self.processing_spec = processing_spec
        self.calibration_date = calibration_date


def _merge_error(message: str) -> Exception:
    from xarray.structure.merge import MergeError

    return MergeError(message)


def _is_missing(value: Any) -> bool:
    """Raw missing readings (NaN, NA, NaT, None) are not deliverable."""
    return bool(pd.isna(value))


def _coerce_instrument_datasets(
    objects: Iterable[Dataset | Mapping[Any, Any]],
) -> list[Dataset]:
    from xarray.core.dataset import Dataset

    datasets = [obj if isinstance(obj, Dataset) else Dataset(obj) for obj in objects]
    return datasets


def _instrument_identity(ds: Dataset, options: DeliveryOptions) -> Hashable:
    if options.instrument_coord in ds.coords:
        coord = ds[options.instrument_coord]
        if coord.ndim == 0 or coord.size == 1:
            return coord.values.ravel()[0]
        raise _merge_error(
            f"the {options.instrument_coord!r} coordinate identifying the "
            f"instrument must be scalar, got values {coord.values!r}"
        )
    if options.instrument_coord in ds.attrs:
        return ds.attrs[options.instrument_coord]
    raise _merge_error(
        f"instrument dataset has no {options.instrument_coord!r} coordinate "
        "or attribute identifying the instrument"
    )


def _read_manifest(manifest: Dataset, options: DeliveryOptions) -> list[_DeliveryRow]:
    required = (
        options.record_dim,
        options.batch_coord,
        options.station_coord,
        options.time_coord,
        options.quality_flag_coord,
    )
    missing = [name for name in required if name not in manifest.variables]
    if missing:
        raise _merge_error(
            f"delivery manifest is missing required column(s): {missing}"
        )

    record_ids = list(manifest[options.record_dim].values)
    if len(set(record_ids)) != len(record_ids):
        raise _merge_error("delivery record ids must be unique")

    rows: list[_DeliveryRow] = []
    for index, record_id in enumerate(record_ids):
        rows.append(
            _DeliveryRow(
                index=index,
                record_id=record_id,
                batch=manifest[options.batch_coord].values[index],
                station=manifest[options.station_coord].values[index],
                time=manifest[options.time_coord].values[index],
                quality_flagged=bool(
                    manifest[options.quality_flag_coord].values[index]
                ),
            )
        )
    return rows


def _collect_readings(
    datasets: list[Dataset],
    rows: list[_DeliveryRow],
    options: DeliveryOptions,
) -> dict[Hashable, dict[tuple[Hashable, Any], _InstrumentReading]]:
    """Read only the (station, time) points referenced by unflagged rows."""
    if len(set(options.instrument_priority)) != len(options.instrument_priority):
        raise _merge_error(
            f"instrument_priority must not contain duplicates: "
            f"{options.instrument_priority}"
        )

    needed_points = {(row.station, row.time) for row in rows if not row.quality_flagged}
    readings: dict[Hashable, dict[tuple[Hashable, Any], _InstrumentReading]] = {}

    for ds in datasets:
        instrument = _instrument_identity(ds, options)
        if instrument not in options.instrument_priority:
            raise _merge_error(
                f"instrument {instrument!r} is not listed in instrument_priority"
            )
        if instrument in readings:
            raise _merge_error(
                f"instrument {instrument!r} is supplied by more than one "
                "dataset; merge its readings into a single dataset first"
            )

        data_vars = list(ds.data_vars)
        if len(data_vars) != 1:
            raise _merge_error(
                "each instrument dataset must contain exactly one data "
                f"variable (the raw reading), got {data_vars} for instrument "
                f"{instrument!r}"
            )
        reading_name = data_vars[0]
        reading_dims = ds[reading_name].dims
        expected_dims = (options.station_coord, options.time_coord)
        if set(reading_dims) != set(expected_dims) or len(reading_dims) != len(
            expected_dims
        ):
            raise _merge_error(
                f"the raw reading of instrument {instrument!r} must be "
                f"indexed by {expected_dims!r}, got dims {reading_dims!r}"
            )
        if options.station_coord not in ds.coords or (
            options.time_coord not in ds.coords
        ):
            raise _merge_error(
                f"instrument {instrument!r} dataset is missing the "
                f"{options.station_coord!r}/{options.time_coord!r} coordinates"
            )
        attrs = ds.attrs
        if options.license_attr not in attrs:
            raise _merge_error(
                f"instrument {instrument!r} dataset is missing the "
                f"{options.license_attr!r} attribute"
            )
        if options.processing_spec_attr not in attrs:
            raise _merge_error(
                f"instrument {instrument!r} dataset is missing the "
                f"{options.processing_spec_attr!r} attribute"
            )

        per_instrument: dict[tuple[Hashable, Any], _InstrumentReading] = {}
        if needed_points:
            from xarray.core.dataarray import DataArray

            station_values = ds[options.station_coord].values
            time_values = ds[options.time_coord].values
            present_points = [
                point
                for point in needed_points
                if point[0] in station_values and point[1] in time_values
            ]
            if present_points:
                needed_stations = [station for station, _ in present_points]
                needed_times = [time for _, time in present_points]
                # One vectorized selection per instrument: for lazily opened
                # datasets only the candidate points of unflagged rows are read.
                points = ds.sel(
                    {
                        options.station_coord: DataArray(
                            needed_stations, dims="delivery_point"
                        ),
                        options.time_coord: DataArray(
                            needed_times, dims="delivery_point"
                        ),
                    }
                )
                values = points[reading_name].values
                for point, value in zip(present_points, values, strict=True):
                    if _is_missing(value):
                        # Raw missing observations are never deliverable.
                        continue
                    per_instrument[point] = _InstrumentReading(
                        instrument=instrument,
                        station=point[0],
                        time=point[1],
                        value=value,
                        license=attrs[options.license_attr],
                        processing_spec=attrs[options.processing_spec_attr],
                        calibration_date=attrs.get(options.calibration_date_attr),
                    )
        readings[instrument] = per_instrument

    unknown_quotas = set(options.read_quotas) - set(readings)
    if unknown_quotas:
        raise _merge_error(
            f"read_quotas reference unknown instrument(s): "
            f"{sorted(unknown_quotas, key=str)!r}"
        )
    return readings


def _quotas(
    datasets: list[Dataset],
    readings: dict[Hashable, dict[tuple[Hashable, Any], _InstrumentReading]],
    options: DeliveryOptions,
) -> dict[Hashable, float]:
    quotas: dict[Hashable, float] = dict.fromkeys(readings, math.inf)
    quotas.update(options.read_quotas)
    for ds in datasets:
        instrument = _instrument_identity(ds, options)
        if options.read_quota_attr in ds.attrs:
            quotas[instrument] = int(ds.attrs[options.read_quota_attr])
    return quotas


def _row_candidates(
    rows: list[_DeliveryRow],
    readings: dict[Hashable, dict[tuple[Hashable, Any], _InstrumentReading]],
    options: DeliveryOptions,
) -> list[list[_InstrumentReading]]:
    """Licensed, priority-ordered candidates for each manifest row."""
    candidates: list[list[_InstrumentReading]] = []
    for row in rows:
        if row.quality_flagged:
            candidates.append([])
            continue
        if row.batch not in options.licenses:
            raise _merge_error(f"batch {row.batch!r} has no accepted license list")
        accepted = set(options.licenses[row.batch])
        point = (row.station, row.time)
        row_options: list[_InstrumentReading] = []
        for instrument in options.instrument_priority:
            reading = readings.get(instrument, {}).get(point)
            if reading is not None and reading.license in accepted:
                row_options.append(reading)
        candidates.append(row_options)
    return candidates


def _select_readings(
    rows: list[_DeliveryRow],
    candidates: list[list[_InstrumentReading]],
    quotas: dict[Hashable, float],
) -> list[_InstrumentReading | None]:
    """Maximize delivered rows, breaking ties by manifest row and priority.

    Quotas count distinct raw readings per instrument across the whole
    submission; the same (station, time) reading reused by several rows or
    receivers is charged only once. All readings delivered to one batch must
    share a processing specification, so a batch is locked to the first
    specification chosen for it.
    """
    n_rows = len(rows)
    consumed: dict[Hashable, set[tuple[Hashable, Any]]] = {
        instrument: set() for instrument in quotas
    }
    batch_specs: dict[Hashable, Hashable] = {}
    choices: list[_InstrumentReading | None] = [None] * n_rows
    best_choices: list[_InstrumentReading | None] | None = None
    best_count = -1

    def take(reading: _InstrumentReading) -> bool:
        point = (reading.station, reading.time)
        instrument = reading.instrument
        if point in consumed[instrument]:
            return True
        if len(consumed[instrument]) >= quotas[instrument]:
            return False
        consumed[instrument].add(point)
        return True

    def release(reading: _InstrumentReading, was_new: bool) -> None:
        if was_new:
            consumed[reading.instrument].discard((reading.station, reading.time))

    def spec_allowed(row: _DeliveryRow, reading: _InstrumentReading) -> bool:
        established = batch_specs.get(row.batch)
        return established is None or established == reading.processing_spec

    def greedy_seed() -> int:
        count = 0
        for index, row in enumerate(rows):
            if row.quality_flagged:
                continue
            for reading in candidates[index]:
                if not spec_allowed(row, reading):
                    continue
                if take(reading):
                    choices[index] = reading
                    batch_specs.setdefault(row.batch, reading.processing_spec)
                    count += 1
                    break
        return count

    def search(index: int, count: int) -> None:
        nonlocal best_choices, best_count
        if count + (n_rows - index) <= best_count:
            return
        if index == n_rows:
            if count > best_count:
                best_count = count
                best_choices = choices.copy()
            return

        row = rows[index]
        if row.quality_flagged:
            # Flagged rows never take a reading and never spend quota.
            choices[index] = None
            search(index + 1, count)
            return

        for reading in candidates[index]:
            if not spec_allowed(row, reading):
                continue
            point = (reading.station, reading.time)
            was_new = point not in consumed[reading.instrument]
            if not take(reading):
                continue
            locked_spec = row.batch not in batch_specs
            if locked_spec:
                batch_specs[row.batch] = reading.processing_spec
            choices[index] = reading
            search(index + 1, count + 1)
            release(reading, was_new)
            if locked_spec:
                del batch_specs[row.batch]
            choices[index] = None

        # Leaving the row empty is always the last-resort option.
        choices[index] = None
        search(index + 1, count)

    best_count = greedy_seed()
    best_choices = choices.copy()
    for instrument in consumed:
        consumed[instrument].clear()
    batch_specs.clear()
    for index in range(n_rows):
        choices[index] = None
    search(0, 0)
    assert best_choices is not None
    return best_choices


def _build_dataset(
    rows: list[_DeliveryRow],
    selected: list[_InstrumentReading | None],
    options: DeliveryOptions,
) -> Dataset:
    from xarray.core.dataset import Dataset

    n_rows = len(rows)
    record_ids = [row.record_id for row in rows]
    stations = [row.station for row in rows]
    times = [row.time for row in rows]
    batches = [row.batch for row in rows]

    temperatures = np.full(n_rows, np.nan, dtype=np.float64)
    instruments = np.full(n_rows, None, dtype=object)
    licenses = np.full(n_rows, None, dtype=object)
    specs = np.full(n_rows, None, dtype=object)
    calibration_dates = np.full(n_rows, None, dtype=object)
    statuses = np.empty(n_rows, dtype=object)

    for index, (row, reading) in enumerate(zip(rows, selected, strict=True)):
        if row.quality_flagged:
            statuses[index] = STATUS_QUALITY_FLAGGED
        elif reading is None:
            statuses[index] = STATUS_UNASSIGNED
        else:
            statuses[index] = STATUS_DELIVERED
            temperatures[index] = reading.value
            instruments[index] = reading.instrument
            licenses[index] = reading.license
            specs[index] = reading.processing_spec
            calibration = reading.calibration_date
            if calibration is not None and not _is_missing(calibration):
                calibration_dates[index] = calibration

    dim = options.record_dim
    return Dataset(
        data_vars={
            TEMPERATURE_VAR: ([dim], temperatures),
            DELIVERY_INSTRUMENT_VAR: ([dim], instruments),
            DELIVERY_LICENSE_VAR: ([dim], licenses),
            PROCESSING_SPEC_VAR: ([dim], specs),
            CALIBRATION_DATE_VAR: ([dim], calibration_dates),
            DELIVERY_STATUS_VAR: ([dim], statuses),
        },
        coords={
            dim: ([dim], np.asarray(record_ids)),
            options.station_coord: ([dim], np.asarray(stations, dtype=object)),
            options.time_coord: ([dim], np.asarray(times)),
            options.batch_coord: ([dim], np.asarray(batches, dtype=object)),
        },
    )


def apply_delivery(
    objects: Iterable[Dataset | Mapping[Any, Any]],
    options: DeliveryOptions,
) -> Dataset:
    """Build the delivered dataset for ``xr.merge(..., delivery=options)``."""
    datasets = _coerce_instrument_datasets(objects)
    rows = _read_manifest(options.manifest, options)
    readings = _collect_readings(datasets, rows, options)
    quotas = _quotas(datasets, readings, options)
    candidates = _row_candidates(rows, readings, options)
    selected = _select_readings(rows, candidates, quotas)
    return _build_dataset(rows, selected, options)
