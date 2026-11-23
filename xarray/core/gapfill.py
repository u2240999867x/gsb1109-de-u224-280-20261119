"""Fill short gaps in chunked meteorological daily observations.

Archives of daily station readings are often split into many remote chunks
(e.g. one chunk per station and date) and indexed by a non-standard calendar
such as ``"360_day"``.  This module fills a requested date range without
loading the whole archive: only the chunk coordinate handles are inspected up
front, and a chunk's readings are fetched the first time a station/date pair
needs them.

The fill rules are deliberately conservative:

* every calendar day in the request is delivered, including dates that have no
  coordinate in the archive;
* a value is only filled from the nearest *original* observation on each side;
* a gap whose endpoint observations are exactly ``max_gap`` calendar days apart
  is filled, but a longer one is not;
* shutdown days stay missing and block a gap from borrowing across them;
* values filled earlier in the run are never used as endpoints.

The returned :class:`~xarray.Dataset` carries a ``status`` variable with one of
``"observed"``, ``"filled"``, ``"shutdown"`` and ``"missing"``, plus a
``coordinate_present`` variable recording whether the archive originally had a
coordinate for that station and day.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Hashable, Iterable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, runtime_checkable

import numpy as np

from xarray.coding.cftime_offsets import to_cftime_datetime
from xarray.coding.cftimeindex import CFTimeIndex
from xarray.core.utils import attempt_import

if TYPE_CHECKING:
    import cftime

    from xarray.core.dataarray import DataArray
    from xarray.core.dataset import Dataset

GapStatus = Literal["observed", "filled", "shutdown", "missing"]

#: All status labels, in decreasing order of precedence.
GAP_STATUSES: tuple[GapStatus, ...] = ("observed", "filled", "shutdown", "missing")

_DATE_KEY = tuple[int, int, int]

#: Sentinel: the chunk has no coordinate for the requested station/day, so a
#: different chunk advertising the same date may still carry the reading.
_ABSENT = object()


@dataclass(frozen=True)
class GapChunk:
    """A cheap handle for a block of readings.

    Parameters
    ----------
    stations : sequence of hashable or None
        Station labels advertised by the chunk.  ``None`` means the chunk may
        cover any requested station.
    times : sequence of str or cftime.datetime or None
        Date coordinates advertised by the chunk.  ``None`` means the dates are
        unknown without reading values, which prevents the driver from avoiding
        unnecessary reads.
    key : hashable, optional
        Stable identity used to deduplicate reads when several gaps share the
        same raw chunk.  When omitted, ``(stations, times)`` is used.
    data : DataArray or Dataset, optional
        Already materialized readings.  When omitted, :meth:`GapStore.read` is
        called on first use.
    """

    stations: tuple[Hashable, ...] | None = None
    times: tuple[Any, ...] | None = None
    key: Hashable | None = None
    data: DataArray | Dataset | None = field(default=None, repr=False, compare=False)


@runtime_checkable
class GapStore(Protocol):
    """Protocol for an archive that serves readings in station/date chunks.

    ``chunks`` is a cheap coordinate lookup: it must not read readings and must
    be callable for any date range, including ranges outside the request, so
    the driver can locate the nearest observations on either side of a gap.
    ``read`` is where remote access happens and should be idempotent.
    """

    def chunks(
        self,
        stations: Sequence[Hashable],
        start: cftime.datetime,
        end: cftime.datetime,
    ) -> Iterable[GapChunk]:
        """Yield chunk handles whose date coordinates overlap ``[start, end]``.

        An empty ``stations`` sequence asks for every station, which is used
        once to discover station labels when they are not given explicitly.
        Returning a :class:`GapChunk` with ``stations=None`` means the chunk
        may cover any requested station.
        """
        ...

    def read(self, chunk: GapChunk) -> DataArray | Dataset:
        """Materialize a chunk's readings."""
        ...


# ---------------------------------------------------------------------------
# Calendar helpers
# ---------------------------------------------------------------------------


def _cftime_type(calendar: str) -> type[cftime.datetime]:
    cftime = attempt_import("cftime")
    from xarray.coding.cftime_offsets import get_date_type

    date_type = get_date_type(calendar, use_cftime=True)
    if not (isinstance(date_type, type) and issubclass(date_type, cftime.datetime)):
        raise TypeError(f"calendar {calendar!r} is not a cftime calendar")
    return date_type


def normalise_date(value: Any, calendar: str) -> cftime.datetime:
    """Coerce ``value`` to a midnight cftime datetime in ``calendar``."""
    cftime = attempt_import("cftime")
    date_type = _cftime_type(calendar)

    if isinstance(value, str):
        result = to_cftime_datetime(value, calendar)
    elif isinstance(value, cftime.datetime):
        result = value
    elif isinstance(value, np.datetime64):
        result = date_type(*value.astype("datetime64[D]").item().timetuple()[:3])
    elif isinstance(value, dt.datetime):
        result = date_type(value.year, value.month, value.day)
    elif hasattr(value, "to_pydatetime"):
        converted = value.to_pydatetime()
        result = date_type(converted.year, converted.month, converted.day)
    else:
        raise TypeError(
            "dates must be ISO-8601 strings, cftime.datetime objects or "
            f"datetime-like values, got {type(value).__name__}"
        )

    if not isinstance(result, date_type):
        result = date_type(result.year, result.month, result.day)
    return result.replace(hour=0, minute=0, second=0, microsecond=0)


def _date_key(value: Any, calendar: str) -> _DATE_KEY:
    date = normalise_date(value, calendar)
    return (date.year, date.month, date.day)


def _plain_key(value: Any) -> _DATE_KEY:
    return (value.year, value.month, value.day)


def _add_days(day: cftime.datetime, count: int) -> cftime.datetime:
    return day + dt.timedelta(days=count)


def _station_key(value: Any) -> Hashable:
    return value.item() if hasattr(value, "item") else value


def _dates_between(
    start: cftime.datetime, end: cftime.datetime
) -> Iterator[cftime.datetime]:
    current = start
    while current <= end:
        yield current
        current = _add_days(current, 1)


# ---------------------------------------------------------------------------
# Shutdown table normalisation
# ---------------------------------------------------------------------------


def _normalise_shutdowns(
    shutdown: Any, calendar: str
) -> dict[Hashable, set[_DATE_KEY]]:
    """Normalise a shutdown table into ``{station: {date keys}}``."""
    result: dict[Hashable, set[_DATE_KEY]] = {}
    if shutdown is None:
        return result

    if hasattr(shutdown, "data_vars") and hasattr(shutdown, "dims"):
        return _normalise_shutdown_dataset(shutdown, calendar)

    if isinstance(shutdown, Mapping):
        for station, dates in shutdown.items():
            bucket = result.setdefault(_station_key(station), set())
            if isinstance(dates, str) or _is_cftime_like(dates):
                dates = [dates]
            for value in dates:
                bucket.add(_date_key(value, calendar))
        return result

    for entry in shutdown:
        try:
            station, value = entry
        except (TypeError, ValueError) as err:
            raise ValueError("shutdown entries must be (station, date) pairs") from err
        result.setdefault(_station_key(station), set()).add(_date_key(value, calendar))
    return result


def _is_cftime_like(value: Any) -> bool:
    cftime = attempt_import("cftime")
    return isinstance(value, cftime.datetime)


def _normalise_shutdown_dataset(
    shutdown: Dataset | DataArray, calendar: str
) -> dict[Hashable, set[_DATE_KEY]]:
    import xarray as xr

    if isinstance(shutdown, xr.DataArray):
        if shutdown.ndim != 2:
            raise ValueError("a shutdown DataArray must have station and time dims")
        frame = shutdown.to_dataset(name="__shutdown__")
    else:
        frame = shutdown

    if "station" not in frame.dims or "time" not in frame.dims:
        raise ValueError(
            "a shutdown Dataset needs both 'station' and 'time' dimensions"
        )

    names = list(frame.data_vars)
    if not names:
        raise ValueError("a shutdown Dataset needs a boolean data variable")
    variable = "__shutdown__" if "__shutdown__" in names else names[0]
    values = np.asarray(frame[variable].transpose("station", "time").values)
    values = values.astype(bool)

    stations = _station_labels(frame["station"].values)
    times = list(frame["time"].values)

    result: dict[Hashable, set[_DATE_KEY]] = {}
    for j, station in enumerate(stations):
        bucket = result.setdefault(station, set())
        for i, flag in enumerate(values[j]):
            if bool(flag):
                bucket.add(_date_key(_coerce_label(times[i]), calendar))
    return result


def _coerce_label(value: Any) -> Any:
    """Turn numpy datetime64 labels into plain datetimes the calendar accepts."""
    if isinstance(value, np.datetime64):
        return value.astype("datetime64[us]").item()
    return value


def _station_labels(values: Iterable[Any]) -> list[Hashable]:
    return [_station_key(value) for value in values]


# ---------------------------------------------------------------------------
# Simple in-memory store
# ---------------------------------------------------------------------------


class MemoryGapStore:
    """A small in-memory :class:`GapStore` useful for tests and examples.

    Parameters
    ----------
    readings : mapping
        Either ``{station: {date: value}}`` or an :class:`~xarray.Dataset`
        / :class:`~xarray.DataArray` with ``station`` and ``time`` dims.
    chunk_size : int, default: 1
        Number of date coordinates grouped into one chunk.  Missing dates do
        not create chunks.  Use ``chunks`` to control grouping explicitly.
    chunks : sequence, optional
        Explicit chunks as ``(stations, dates)`` pairs or prebuilt
        :class:`GapChunk` objects.  Overrides ``chunk_size``.
    coordinates : mapping, optional
        Extra coordinates to advertise, as ``{station: [dates]}``.  Use this
        for dates that exist in the archive with a missing reading, including
        stations whose whole record is missing.
    calendar : str, default: "360_day"
        Calendar used to interpret dates.
    """

    def __init__(
        self,
        readings: Mapping[Hashable, Mapping[Any, Any]] | DataArray | Dataset,
        *,
        chunk_size: int = 1,
        chunks: Sequence[Any] | None = None,
        coordinates: Mapping[Hashable, Sequence[Any]] | None = None,
        calendar: str = "360_day",
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        self.calendar = calendar
        self.store: dict[tuple[Hashable, _DATE_KEY], float] = {}
        self._stations: list[Hashable] = []
        self.stations = self._stations
        self.read_log: list[GapChunk] = []
        self.read_keys: list[Hashable] = []
        self._loaded: dict[Hashable, DataArray | Dataset] = {}
        self._coordinates: dict[Hashable, set[_DATE_KEY]] = {}
        self.variable_name: Hashable | None = None

        if hasattr(readings, "dims"):
            self._from_xarray(readings)  # type: ignore[arg-type]
        else:
            self._from_mapping(readings)

        for station, dates in (coordinates or {}).items():
            key = _station_key(station)
            if key not in self._stations:
                self._stations.append(key)
            bucket = self._coordinates.setdefault(key, set())
            for date in dates:
                bucket.add(_date_key(date, self.calendar))

        if chunks is None:
            self._chunks = self._build_chunks(chunk_size)
        else:
            self._chunks = list(chunks)

    def _from_mapping(self, readings: Mapping[Hashable, Mapping[Any, Any]]) -> None:
        for station, by_date in readings.items():
            key = _station_key(station)
            if key not in self._stations:
                self._stations.append(key)
            for date, value in by_date.items():
                entry = _date_key(date, self.calendar)
                if value is None or np.isnan(float(value)):
                    self.store[(key, entry)] = np.nan
                else:
                    self.store[(key, entry)] = float(value)

    def _from_xarray(self, obj: DataArray | Dataset) -> None:
        if "station" not in obj.dims or "time" not in obj.dims:
            raise ValueError("readings need both 'station' and 'time' dimensions")
        if hasattr(obj, "data_vars"):
            names = list(obj.data_vars)
            if len(names) != 1:
                raise ValueError(
                    "a Dataset used as readings must have exactly one data variable"
                )
            array = obj[names[0]]
            self.variable_name = names[0]
        else:
            array = obj
            if array.name is not None:
                self.variable_name = array.name
        self._stations = _station_labels(array["station"].values)
        for j, station in enumerate(self._stations):
            for i, time in enumerate(array["time"].values):
                raw = np.asarray(array.values[j, i])
                key = (station, _date_key(time, self.calendar))
                self.store[key] = np.nan if np.isnan(raw) else float(raw)

    def _build_chunks(self, chunk_size: int) -> list[GapChunk]:
        chunks: list[GapChunk] = []
        for station in self._stations:
            keys = {key for (s, key) in self.store if s == station}
            keys |= self._coordinates.get(station, set())
            dates = sorted(keys)
            index = 0
            while index < len(dates):
                group = dates[index : index + chunk_size]
                labels = tuple(_key_to_label(key) for key in group)
                chunks.append(
                    GapChunk(
                        stations=(station,),
                        times=labels,
                        key=("chunk", station, tuple(group)),
                    )
                )
                index += chunk_size
        return chunks

    def chunks(
        self,
        stations: Sequence[Hashable],
        start: cftime.datetime,
        end: cftime.datetime,
    ) -> Iterator[GapChunk]:
        wanted = {_station_key(station) for station in stations}
        start_key = _date_key(start, self.calendar)
        end_key = _date_key(end, self.calendar)
        for chunk in self._chunks:
            if isinstance(chunk, GapChunk):
                handle = chunk
            else:
                handle = self._coerce_chunk(chunk)
            if wanted and handle.stations is not None:
                if not (set(handle.stations) & wanted):
                    continue
            if handle.times is None:
                yield handle
                continue
            keys = [_date_key(label, self.calendar) for label in handle.times]
            if any(start_key <= key <= end_key for key in keys):
                yield handle

    def _coerce_chunk(self, chunk: Any) -> GapChunk:
        stations, times = chunk
        stations = tuple(_station_key(s) for s in stations)
        key = ("chunk", stations, tuple(_date_key(t, self.calendar) for t in times))
        return GapChunk(stations=stations, times=tuple(times), key=key)

    def read(self, chunk: GapChunk) -> DataArray | Dataset:
        key = _chunk_identity(chunk)
        if key in self._loaded:
            return self._loaded[key]
        if chunk.data is not None:
            data = chunk.data
        else:
            data = self._make_data(chunk)
        self._loaded[key] = data
        self.read_log.append(chunk)
        self.read_keys.append(key)
        return data

    def _make_data(self, chunk: GapChunk) -> Dataset:
        import xarray as xr

        stations = list(chunk.stations or ())
        times = list(chunk.times or ())
        labels = [normalise_date(time, self.calendar) for time in times]
        values = np.full((len(stations), len(labels)), np.nan)
        for j, station in enumerate(stations):
            for i, label in enumerate(labels):
                values[j, i] = self.store.get((station, _plain_key(label)), np.nan)
        return xr.Dataset(
            {"value": (("station", "time"), values)},
            coords={"station": stations, "time": CFTimeIndex(labels)},
        )


def _key_to_label(key: _DATE_KEY) -> str:
    return f"{key[0]:04d}-{key[1]:02d}-{key[2]:02d}"


# ---------------------------------------------------------------------------
# Chunk reader
# ---------------------------------------------------------------------------


class _ChunkReader:
    """Coordinate index plus value cache for one request."""

    def __init__(
        self,
        store: GapStore,
        stations: Sequence[Hashable],
        calendar: str,
        variable: Hashable | None,
    ) -> None:
        self.store = store
        self.stations = list(stations)
        self.calendar = calendar
        self.variable = variable
        self._range_handles: dict[tuple[Hashable, _DATE_KEY], list[GapChunk]] = {}
        self._date_handles: dict[tuple[Hashable, _DATE_KEY], list[GapChunk]] = {}
        self._chunk_data: dict[Hashable, DataArray | Dataset] = {}
        self._values: dict[tuple[Hashable, _DATE_KEY], float | None] = {}
        self._coverage: dict[tuple[Hashable, _DATE_KEY], bool] = {}
        self._primed = False
        self.read_keys: list[Hashable] = []

    def prime(self, start: cftime.datetime, end: cftime.datetime) -> None:
        if self._primed:
            return
        self._primed = True
        for handle in self.store.chunks(self.stations, start, end):
            self._register(handle)

    def _register(self, handle: GapChunk) -> None:
        stations = (
            self.stations
            if handle.stations is None
            else [_station_key(station) for station in handle.stations]
        )
        if handle.times is None:
            return
        for station in stations:
            for label in handle.times:
                key = (station, _date_key(label, self.calendar))
                bucket = self._range_handles.setdefault(key, [])
                if handle not in bucket:
                    bucket.append(handle)

    def handles_for(self, station: Hashable, day: cftime.datetime) -> list[GapChunk]:
        key = (station, _plain_key(day))
        if key in self._date_handles:
            return self._date_handles[key]

        handles = list(self._range_handles.get(key, ()))
        if not handles:
            for handle in self.store.chunks([station], day, day):
                if _chunk_covers(handle, station, day, self.calendar):
                    handles.append(handle)
                self._register(handle)
        else:
            handles = [
                handle
                for handle in handles
                if _chunk_covers(handle, station, day, self.calendar)
            ]
        self._date_handles[key] = handles
        return handles

    def has_coordinate(self, station: Hashable, day: cftime.datetime) -> bool:
        key = (station, _plain_key(day))
        if key not in self._coverage:
            self._coverage[key] = bool(self.handles_for(station, day))
        return self._coverage[key]

    def value(self, station: Hashable, day: cftime.datetime) -> float | None:
        key = (station, _plain_key(day))
        if key in self._values:
            return self._values[key]

        result: float | None = None
        for handle in self.handles_for(station, day):
            label = _matching_label(handle, day, self.calendar)
            if label is None:
                continue
            data = self._load(handle)
            extracted = _extract_value(
                data, station, label, self.variable, self.calendar
            )
            if extracted is _ABSENT:
                # The handle advertised the date but the chunk itself does not
                # carry a coordinate for it; another chunk may.
                continue
            result = extracted
            break
        self._values[key] = result
        return result

    def _load(self, handle: GapChunk) -> DataArray | Dataset:
        key = _chunk_identity(handle)
        if key in self._chunk_data:
            return self._chunk_data[key]
        if handle.data is not None:
            data = handle.data
        else:
            data = self.store.read(handle)
        self._chunk_data[key] = data
        self.read_keys.append(key)
        return data


def _chunk_identity(handle: GapChunk) -> Hashable:
    if handle.key is not None:
        return handle.key
    return (
        tuple(handle.stations or ()),
        tuple(str(time) for time in (handle.times or ())),
    )


def _chunk_covers(
    handle: GapChunk, station: Hashable, day: cftime.datetime, calendar: str
) -> bool:
    if handle.stations is not None:
        labels = {_station_key(value) for value in handle.stations}
        if station not in labels:
            return False
    if handle.times is None:
        return True
    target = _plain_key(day)
    return any(_date_key(label, calendar) == target for label in handle.times)


def _matching_label(
    handle: GapChunk, day: cftime.datetime, calendar: str
) -> Any | None:
    if handle.times is None:
        return normalise_date(day, calendar)
    target = _plain_key(day)
    for label in handle.times:
        if _date_key(label, calendar) == target:
            return label
    return None


def _extract_value(
    data: DataArray | Dataset,
    station: Hashable,
    label: Any,
    variable: Hashable | None,
    calendar: str,
) -> float | None:
    import xarray as xr

    if isinstance(data, xr.Dataset):
        if variable is not None:
            array = data[variable]
        else:
            names = list(data.data_vars)
            if len(names) != 1:
                raise ValueError(
                    "chunks must contain exactly one data variable or the "
                    "'variable' argument must name one"
                )
            array = data[names[0]]
    else:
        array = data

    indexers: dict[str, Any] = {}
    if "station" in array.dims:
        station_label = _match_station(array["station"].values, station)
        if station_label is None:
            return _ABSENT
        indexers["station"] = station_label
    if "time" in array.dims:
        time_label = _match_date(array["time"].values, label, calendar)
        if time_label is None:
            return _ABSENT
        indexers["time"] = time_label
    selected = array.sel(indexers) if indexers else array
    selected = selected.squeeze(drop=True)
    if selected.ndim != 0:
        raise ValueError("a chunk reading must be scalar for one station and one day")
    raw = selected.values
    if np.ma.is_masked(raw):
        return None
    if isinstance(raw, np.ndarray):
        raw = raw.item()
    if raw is None:
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None
    if np.isnan(number):
        return None
    return number


def _match_station(values: Iterable[Any], target: Hashable) -> Any | None:
    for value in values:
        candidate = _station_key(value)
        if candidate == target:
            return value
        if isinstance(candidate, str) and isinstance(target, str):
            if candidate.strip() == target.strip():
                return value
    return None


def _match_date(values: Iterable[Any], target: Any, calendar: str) -> Any | None:
    key = _date_key(target, calendar)
    for value in values:
        if _date_key(value, calendar) == key:
            return value
    return None


# ---------------------------------------------------------------------------
# Gap filling
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _SideResult:
    value: float | None
    distance: int | None
    blocked: bool


class _GapFiller:
    def __init__(
        self,
        reader: _ChunkReader,
        shutdowns: Mapping[Hashable, set[_DATE_KEY]],
        max_gap: int,
        calendar: str,
    ) -> None:
        self.reader = reader
        self.shutdowns = shutdowns
        self.max_gap = max_gap
        self.calendar = calendar

    def _is_shutdown(self, station: Hashable, day: cftime.datetime) -> bool:
        return _plain_key(day) in self.shutdowns.get(station, set())

    def _search(
        self, station: Hashable, day: cftime.datetime, direction: int, limit: int
    ) -> _SideResult:
        distance = 1
        while distance <= limit:
            candidate = _add_days(day, direction * distance)
            if self._is_shutdown(station, candidate):
                return _SideResult(None, None, True)
            value = self.reader.value(station, candidate)
            if value is not None:
                return _SideResult(value, distance, False)
            distance += 1
        return _SideResult(None, None, True)

    def fill_day(
        self, station: Hashable, day: cftime.datetime
    ) -> tuple[GapStatus, float | None]:
        if self._is_shutdown(station, day):
            return "shutdown", None

        observed = self.reader.value(station, day)
        if observed is not None:
            return "observed", observed

        left = self._search(station, day, -1, self.max_gap - 1)
        if left.blocked or left.value is None or left.distance is None:
            return "missing", None

        remaining = self.max_gap - left.distance
        right = self._search(station, day, 1, remaining)
        if right.blocked or right.value is None or right.distance is None:
            return "missing", None

        span = left.distance + right.distance
        weight = left.distance / span
        value = left.value + (right.value - left.value) * weight
        return "filled", float(value)

    def fill_block(
        self, stations: Sequence[Hashable], days: Sequence[cftime.datetime]
    ) -> dict[Hashable, list[tuple[GapStatus, float | None]]]:
        return {
            station: [self.fill_day(station, day) for day in days]
            for station in stations
        }


def _build_block(
    days: Sequence[cftime.datetime],
    stations: Sequence[Hashable],
    result: Mapping[Hashable, list[tuple[GapStatus, float | None]]],
    coordinate_present: Mapping[Hashable, list[bool]],
    variable_name: Hashable,
    attrs: Mapping[Any, Any] | None,
) -> Dataset:
    import xarray as xr

    shape = (len(days), len(stations))
    values = np.full(shape, np.nan, dtype=np.float64)
    statuses = np.empty(shape, dtype=object)
    present = np.zeros(shape, dtype=bool)

    for j, station in enumerate(stations):
        for i, (status, value) in enumerate(result[station]):
            statuses[i, j] = status
            present[i, j] = coordinate_present[station][i]
            if value is not None:
                values[i, j] = value

    return xr.Dataset(
        {
            variable_name: (("time", "station"), values),
            "status": (("time", "station"), statuses),
            "coordinate_present": (("time", "station"), present),
        },
        coords={
            "time": CFTimeIndex(list(days)),
            "station": list(stations),
        },
        attrs=dict(attrs or {}),
    )


def _coordinate_presence(
    reader: _ChunkReader,
    stations: Sequence[Hashable],
    days: Sequence[cftime.datetime],
) -> dict[Hashable, list[bool]]:
    return {
        station: [reader.has_coordinate(station, day) for day in days]
        for station in stations
    }


def _resolve_variable_name(
    variable: Hashable | None, reader: _ChunkReader, store: GapStore
) -> Hashable:
    if variable is not None:
        return variable
    default = getattr(store, "variable_name", None)
    if default is not None:
        return default
    for data in reader._chunk_data.values():
        if hasattr(data, "data_vars"):
            names = list(data.data_vars)
            if len(names) == 1:
                return names[0]
    return "value"


def _as_store(store: Any, calendar: str) -> GapStore:
    if hasattr(store, "chunks") and hasattr(store, "read"):
        return store
    if isinstance(store, Mapping) or hasattr(store, "dims"):
        return MemoryGapStore(store, calendar=calendar)
    raise TypeError(
        "store must implement the GapStore protocol or be a readings mapping"
    )


def _prepare_stations(
    store: GapStore,
    stations: Sequence[Hashable] | None,
    start: cftime.datetime,
    end: cftime.datetime,
) -> list[Hashable]:
    if stations is not None:
        labels = _station_labels(stations)
        if len(set(labels)) != len(labels):
            raise ValueError("stations must be unique")
        return labels

    discovered: list[Hashable] = []
    seen: set[Hashable] = set()
    for handle in store.chunks([], start, end):
        if handle.stations is None:
            continue
        for station in handle.stations:
            label = _station_key(station)
            if label not in seen:
                seen.add(label)
                discovered.append(label)

    if not discovered:
        catalog = getattr(store, "stations", None)
        if callable(catalog):
            catalog = catalog()
        if catalog is not None:
            for station in catalog:
                label = _station_key(station)
                if label not in seen:
                    seen.add(label)
                    discovered.append(label)

    if not discovered:
        raise ValueError("no stations were given and the chunk handles advertise none")
    return discovered


def _validate_max_gap(max_gap: Any) -> None:
    if isinstance(max_gap, bool) or not isinstance(max_gap, int) or max_gap < 0:
        raise ValueError("max_gap must be a non-negative integer number of days")


def iter_fill_short_gaps(
    store: GapStore | Mapping[Hashable, Mapping[Any, Any]],
    start: Any,
    end: Any,
    *,
    stations: Sequence[Hashable] | None = None,
    shutdown: Any = None,
    max_gap: int,
    calendar: str = "360_day",
    variable: Hashable | None = None,
    block_size: int | None = None,
) -> Iterator[Dataset]:
    """Iterate over filled blocks for a (possibly multi-year) request.

    Blocks are independent :class:`~xarray.Dataset` objects along ``time``.
    Chunk reads are shared across blocks, so a raw chunk used by several
    blocks is still fetched once.  See :func:`fill_short_gaps` for parameters.

    Yields
    ------
    Dataset
        One dataset per block of at most ``block_size`` days.
    """
    _validate_max_gap(max_gap)
    if block_size is not None and block_size < 1:
        raise ValueError("block_size must be a positive integer")

    begin = normalise_date(start, calendar)
    finish = normalise_date(end, calendar)
    if finish < begin:
        raise ValueError("end must not precede start")

    gap_store = _as_store(store, calendar)
    station_labels = _prepare_stations(gap_store, stations, begin, finish)
    shutdowns = _normalise_shutdowns(shutdown, calendar)

    reader = _ChunkReader(gap_store, station_labels, calendar, variable)
    reader.prime(begin, finish)
    filler = _GapFiller(reader, shutdowns, max_gap, calendar)
    attrs = getattr(gap_store, "attrs", None)

    for block_start, block_end in _block_ranges(begin, finish, block_size):
        days = list(_dates_between(block_start, block_end))
        result = filler.fill_block(station_labels, days)
        present = _coordinate_presence(reader, station_labels, days)
        variable_name = _resolve_variable_name(variable, reader, gap_store)
        yield _build_block(
            days,
            station_labels,
            result,
            present,
            variable_name,
            attrs,
        )


def _block_ranges(
    begin: cftime.datetime,
    finish: cftime.datetime,
    block_size: int | None,
) -> Iterator[tuple[cftime.datetime, cftime.datetime]]:
    """Split a request into independent computation blocks.

    By default a multi-year request is split by calendar year so that the whole
    request, together with the neighbour observations needed by its gaps, is
    never assembled into a single computation block.  Requests within one
    calendar year remain a single block.
    """
    current = begin
    while current <= finish:
        if block_size is not None:
            block_end = min(finish, _add_days(current, block_size - 1))
        else:
            year_end = _end_of_year(current)
            block_end = min(finish, year_end)
        yield current, block_end
        current = _add_days(block_end, 1)


def _end_of_year(day: cftime.datetime) -> cftime.datetime:
    date_type = type(day)
    if day.month == 12 and day.day == getattr(day, "daysinmonth", 31):
        return day
    first_next_year = date_type(day.year + 1, 1, 1)
    return first_next_year - dt.timedelta(days=1)


def fill_short_gaps(
    store: GapStore | Mapping[Hashable, Mapping[Any, Any]],
    start: Any,
    end: Any,
    *,
    stations: Sequence[Hashable] | None = None,
    shutdown: Any = None,
    max_gap: int,
    calendar: str = "360_day",
    variable: Hashable | None = None,
    block_size: int | None = None,
) -> Dataset:
    """Fill short gaps in a chunked archive of daily readings.

    Only the nearest original observation on each side of a gap is used.  The
    two endpoints must be at most ``max_gap`` calendar days apart; a gap whose
    endpoints are exactly that far apart is filled.  Filled values are never
    reused as endpoints, and shutdown days stay missing and block a gap from
    borrowing across them.

    Every calendar day between ``start`` and ``end`` is delivered, including
    dates that have no coordinate in the archive.  The result's ``status``
    variable is one of ``"observed"``, ``"filled"``, ``"shutdown"`` and
    ``"missing"``; ``coordinate_present`` records whether the original archive
    had a coordinate for that station and day.

    Reads are demand driven.  Chunk coordinate handles are inspected first;
    a chunk's readings are fetched only when a still-undecided gap needs a
    station/date pair in it, and each raw chunk is fetched at most once per
    call.  The nearer chunk on one side of a gap is always resolved before a
    farther chunk on the same side.

    Parameters
    ----------
    store : GapStore or mapping
        Archive implementing :class:`GapStore`, or an in-memory
        ``{station: {date: value}}`` mapping / :class:`~xarray.Dataset`.
    start, end : str or cftime.datetime
        Inclusive request range, interpreted in ``calendar``.  Dates such as
        ``"2001-02-30"`` are valid with the ``"360_day"`` calendar.
    stations : sequence of hashable, optional
        Stations to deliver.  When omitted, labels are taken from the chunk
        handles.
    shutdown : mapping, Dataset or sequence, optional
        Shutdown days.  A mapping of station to dates is the simplest form; a
        ``Dataset`` with ``station`` and ``time`` dims and a boolean variable is
        also accepted.
    max_gap : int
        Maximum number of calendar days between the two endpoint observations.
    calendar : str, default: "360_day"
        Calendar used to interpret dates and to measure day distances.
    variable : hashable, optional
        Variable to fill when chunk datasets contain more than one data
        variable.
    block_size : int, optional
        When given, process the request in blocks of this many days and
        concatenate the results.  Use :func:`iter_fill_short_gaps` to consume
        blocks without concatenating.

    Returns
    -------
    Dataset
        With dimensions ``time`` and ``station``, the readings under
        ``variable`` (or the chunk's single data variable), and the ``status``
        and ``coordinate_present`` variables described above.

    Examples
    --------
    >>> readings = {
    ...     "A": {"2001-02-27": 4, "2001-02-29": 10, "2001-03-02": 16},
    ...     "B": {"2001-02-27": 1, "2001-02-29": 3, "2001-03-02": 5},
    ... }
    >>> result = fill_short_gaps(
    ...     readings, "2001-02-27", "2001-03-02", max_gap=5
    ... )
    >>> result["status"].sel(station="A", time="2001-02-30").item()
    'filled'
    """
    blocks = list(
        iter_fill_short_gaps(
            store,
            start,
            end,
            stations=stations,
            shutdown=shutdown,
            max_gap=max_gap,
            calendar=calendar,
            variable=variable,
            block_size=block_size,
        )
    )
    import xarray as xr

    if len(blocks) == 1:
        return blocks[0]
    return xr.concat(blocks, dim="time")
