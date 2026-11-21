"""Segmented, versioned track archives for reconstructed ADS-B positions.

Rebuilt position data is not necessarily a continuous trajectory: reception
gaps, contact-only messages, and revisions can all change which positions may
be connected.  :class:`TrackArchive` keeps the observations that were actually
received, derives one position per aircraft and second, and splits the result
into segments whenever two consecutive valid positions are more than
``max_gap`` apart.

Every write returns :class:`ArchiveBatchResult` with the additions,
retractions, and replacements needed by a downstream archive.  Applying those
changes is equivalent to recomputing the archive from the effective
observations::

    archive = TrackArchive(max_gap=10)
    first = archive.write(observations, batch="first")
    downstream = first.apply({})

    second = archive.write(late_observations, batch="second")
    downstream = second.apply(downstream)
    assert downstream == archive.state

Observation records may be mappings, a :class:`pandas.DataFrame`, or
:class:`Observation` instances.  The following aliases are accepted:

``icao24`` / ``icao``
    Aircraft address.
``time`` / ``timestamp``
    Actual observation time.  Numeric values are interpreted as seconds since
    the Unix epoch.  Times are kept in UTC.
``lat`` / ``latitude``, ``lon`` / ``longitude``, ``alt`` / ``altitude``
    Position fields.  Missing or non-finite coordinates mean that the record
    only proves that a signal was received.
``source`` / ``station`` / ``sensor`` / ``sensors``
    Receiver provenance.  A ``sensors`` list (as returned by the Trino raw
    tables) is expanded into one report per sensor.
``report_id`` / ``id``, ``revision`` / ``version``
    Optional report identity and revision.  The highest revision per station
    wins inside a batch.
``valid`` / ``has_position`` / ``position_valid``
    Optional explicit validity.  ``False`` always keeps a record as
    contact-only; ``True`` never invents coordinates that are not present.

The effective report for an ``(icao24, second, station)`` key is the one with
the highest revision; ties go to the most recently written batch.  A second is
therefore valid while at least one station still reports a coordinate for it.
This keeps a contact-only revision from one station from erasing another
station's independent fix, while a revision that reclassifies the reporting
stations as contact-only revokes the coordinate and splits the surrounding
segments again.  Unaffected keys retain their original batch provenance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from math import isfinite
from numbers import Real
from typing import Any, Iterable, Literal, Mapping, MutableMapping, Sequence

import pandas as pd

ArchiveAction = Literal["add", "retract", "replace"]
ArchiveState = MutableMapping[str, "TrackSegment"]

_NO_DEFAULT = object()


class ArchiveStateError(ValueError):
    """Raised when changes cannot be applied to a downstream archive."""


@dataclass(frozen=True, order=True)
class Observation:
    """One report received from one station.

    Coordinates are kept as received.  A report with ``has_position=False`` is
    still a useful quality observation but never contributes a position.
    """

    icao24: str
    time: pd.Timestamp
    latitude: float | None
    longitude: float | None
    altitude: float | None
    source: str
    report_id: str
    revision: int
    batch_id: str
    has_position: bool
    order: int = field(default=0, compare=False, repr=False)

    @property
    def batch(self) -> str:
        """Alias for the processing batch that supplied the report."""
        return self.batch_id

    @property
    def lat(self) -> float | None:
        """Alias for :attr:`latitude`."""
        return self.latitude

    @property
    def lon(self) -> float | None:
        """Alias for :attr:`longitude`."""
        return self.longitude

    @property
    def valid(self) -> bool:
        """Whether the report carries a usable position."""
        return self.has_position


@dataclass(frozen=True)
class TrackPoint:
    """One temporal position for an aircraft.

    ``time`` is the canonical second shared by every report coalesced here.
    The exact observation times, stations, revisions, and batches remain
    available in :attr:`observations` and through :attr:`observed_at`.
    """

    icao24: str
    time: pd.Timestamp
    latitude: float
    longitude: float
    altitude: float | None
    observations: tuple[Observation, ...]
    representative: Observation

    @property
    def observed_at(self) -> pd.Timestamp:
        """Actual time of the effective observation, before second bucketing."""
        return self.representative.time

    @property
    def lat(self) -> float:
        """Alias for :attr:`latitude`."""
        return self.latitude

    @property
    def lon(self) -> float:
        """Alias for :attr:`longitude`."""
        return self.longitude

    @property
    def sources(self) -> tuple[str, ...]:
        """Distinct stations that reported this second."""
        return tuple(dict.fromkeys(o.source for o in self.observations))

    @property
    def source_count(self) -> int:
        """Number of distinct stations that reported this second."""
        return len(self.sources)

    @property
    def batch_id(self) -> str:
        """Processing batch that supplied the effective position report."""
        return self.representative.batch_id


@dataclass(frozen=True)
class TrackSegment:
    """A maximal run of positions separated by at most the archive gap.

    ``start`` and ``end`` are canonical seconds used for coverage decisions.
    The exact report times remain available on each :class:`TrackPoint`.
    """

    segment_id: str
    icao24: str
    points: tuple[TrackPoint, ...]

    def __post_init__(self) -> None:
        if not self.points:
            raise ValueError("a track segment must contain at least one point")
        if any(point.icao24 != self.icao24 for point in self.points):
            raise ValueError(
                "all points in a segment must belong to one aircraft"
            )

    @property
    def start(self) -> pd.Timestamp:
        """Canonical second of the first position in the segment."""
        return self.points[0].time

    @property
    def end(self) -> pd.Timestamp:
        """Canonical second of the last position in the segment."""
        return self.points[-1].time

    def to_records(self) -> list[dict[str, Any]]:
        """Flatten the segment to one record per temporal position."""
        return [
            {
                "segment_id": self.segment_id,
                "icao24": self.icao24,
                "time": point.time,
                "latitude": point.latitude,
                "longitude": point.longitude,
                "altitude": point.altitude,
                "source": "|".join(point.sources),
                "source_count": point.source_count,
                "batch_id": point.batch_id,
            }
            for point in self.points
        ]


@dataclass(frozen=True)
class ArchiveChange:
    """One change that can be applied to a downstream track archive."""

    action: ArchiveAction
    segment_id: str
    icao24: str
    before: TrackSegment | None
    after: TrackSegment | None
    triggering: tuple[Observation, ...]

    def __post_init__(self) -> None:
        if self.action == "add" and (
            self.before is not None or self.after is None
        ):
            raise ValueError("an addition must have only a new segment")
        if self.action == "retract" and (
            self.before is None or self.after is not None
        ):
            raise ValueError("a retraction must have only an existing segment")
        if self.action == "replace" and (
            self.before is None or self.after is None
        ):
            raise ValueError(
                "a replacement must have both old and new segments"
            )
        for segment in (self.before, self.after):
            if segment is not None and segment.segment_id != self.segment_id:
                raise ValueError("change and segment identities disagree")

    @property
    def segment(self) -> TrackSegment:
        """The segment after the change, or before it for a retraction."""
        segment = self.after if self.after is not None else self.before
        assert segment is not None
        return segment

    @property
    def kind(self) -> ArchiveAction:
        """Alias for :attr:`action`."""
        return self.action


@dataclass(frozen=True)
class ArchiveBatchResult:
    """Changes produced by one archive write or batch operation."""

    batch_id: str
    changes: tuple[ArchiveChange, ...]
    segments: tuple[TrackSegment, ...]
    observations: tuple[Observation, ...]
    contacts: tuple[Observation, ...] = ()

    @property
    def additions(self) -> tuple[ArchiveChange, ...]:
        """Changes that add a segment."""
        return tuple(c for c in self.changes if c.action == "add")

    @property
    def retractions(self) -> tuple[ArchiveChange, ...]:
        """Changes that withdraw a segment."""
        return tuple(c for c in self.changes if c.action == "retract")

    @property
    def replacements(self) -> tuple[ArchiveChange, ...]:
        """Changes that replace a segment."""
        return tuple(c for c in self.changes if c.action == "replace")

    def apply(
        self, state: Mapping[str, TrackSegment]
    ) -> dict[str, TrackSegment]:
        """Apply this result's changes to a downstream archive state."""
        return apply_changes(state, self.changes)


@dataclass(frozen=True)
class _StoredBatch:
    batch_id: str
    observations: tuple[Observation, ...]


def _mapping_lookup(record: Mapping[Any, Any]) -> dict[str, Any]:
    return {str(key).strip().lower(): value for key, value in record.items()}


def _get(
    record: Mapping[str, Any],
    names: Sequence[str],
    default: Any = _NO_DEFAULT,
) -> Any:
    for name in names:
        if name in record:
            return record[name]
    if default is _NO_DEFAULT:
        raise KeyError(f"missing required field: {names[0]}")
    return default


def _is_missing(value: Any) -> bool:
    if value is None or value is pd.NaT:
        return True
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _as_float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid numeric value: {value!r}") from error
    return result if isfinite(result) else None


def _as_time(value: Any) -> pd.Timestamp:
    if _is_missing(value):
        raise ValueError("observation time is required")
    if isinstance(value, pd.Timestamp):
        stamp = value
    elif isinstance(value, bool):
        raise ValueError("boolean values are not valid observation times")
    elif isinstance(value, Real) or isinstance(value, (int, float)):
        stamp = pd.Timestamp(float(value), unit="s", tz="utc")
    else:
        stamp = pd.to_datetime(value, utc=True)
    if stamp is pd.NaT or pd.isna(stamp):
        raise ValueError(f"invalid observation time: {value!r}")
    if stamp.tzinfo is None:
        stamp = stamp.tz_localize("utc")
    else:
        stamp = stamp.tz_convert("utc")
    return stamp


def _as_revision(value: Any) -> int:
    if _is_missing(value):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError) as error:
        raise ValueError(f"invalid revision: {value!r}") from error


def _as_optional_bool(value: Any) -> bool | None:
    if _is_missing(value):
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "1", "yes", "y", "position", "valid"}:
            return True
        if normalized in {"false", "0", "no", "n", "contact", "invalid"}:
            return False
    return bool(value)


def _sensor_sources(value: Any) -> list[str]:
    if _is_missing(value):
        return [""]
    if isinstance(value, str):
        return [value]
    if not isinstance(value, (list, tuple, set)):
        return [str(value)]
    sources: list[str] = []
    for item in value:
        if isinstance(item, Mapping):
            serial = item.get("serial", item.get("sensor", item.get("name")))
            sources.append("" if serial is None else str(serial))
        else:
            sources.append(str(item))
    return sources or [""]


def _record_sources(record: Mapping[str, Any]) -> list[str]:
    explicit = _get(
        record,
        ("source", "station", "sensor", "site", "receiver"),
        default=None,
    )
    if not _is_missing(explicit):
        if isinstance(explicit, (list, tuple, set)):
            values = [str(item) for item in explicit]
            return values or [""]
        return [str(explicit)]
    return _sensor_sources(_get(record, ("sensors",), default=None))


def _record_latitude(record: Mapping[str, Any]) -> float | None:
    return _as_float(_get(record, ("lat", "latitude"), default=None))


def _record_longitude(record: Mapping[str, Any]) -> float | None:
    return _as_float(_get(record, ("lon", "longitude", "lng"), default=None))


def _record_altitude(record: Mapping[str, Any]) -> float | None:
    return _as_float(_get(record, ("alt", "altitude"), default=None))


def _observation_record(observation: Observation) -> dict[str, Any]:
    return {
        "icao24": observation.icao24,
        "time": observation.time,
        "lat": observation.latitude,
        "lon": observation.longitude,
        "alt": observation.altitude,
        "source": observation.source,
        "report_id": observation.report_id,
        "revision": observation.revision,
        "valid": observation.has_position,
    }


def _normalize_records(
    observations: Any, batch_id: str
) -> tuple[Observation, ...]:
    if isinstance(observations, pd.DataFrame):
        records: Iterable[Mapping[Any, Any]] = observations.to_dict("records")
    elif isinstance(observations, Mapping):
        records = [observations]
    elif isinstance(observations, Observation):
        records = [_observation_record(observations)]
    else:
        records = [
            _observation_record(record)
            if isinstance(record, Observation)
            else record
            for record in observations
        ]

    normalized: list[Observation] = []
    for index, raw_record in enumerate(records):
        if not isinstance(raw_record, Mapping):
            raise TypeError(
                "observations must contain mappings, DataFrame rows, or "
                f"Observation instances; got {type(raw_record)!r}"
            )
        record = _mapping_lookup(raw_record)
        icao24 = _get(record, ("icao24", "icao", "aircraft"))
        if _is_missing(icao24):
            raise ValueError("observation icao24 is required")
        icao24 = str(icao24).strip().lower()
        if not icao24:
            raise ValueError("observation icao24 must not be empty")

        stamp = _as_time(_get(record, ("time", "timestamp")))
        latitude = _record_latitude(record)
        longitude = _record_longitude(record)
        altitude = _record_altitude(record)
        if (latitude is None) != (longitude is None):
            latitude = None
            longitude = None
        if latitude is not None and not -90.0 <= latitude <= 90.0:
            raise ValueError(f"latitude out of range for {icao24}: {latitude}")
        if longitude is not None and not -180.0 <= longitude <= 180.0:
            raise ValueError(
                f"longitude out of range for {icao24}: {longitude}"
            )

        explicit_valid = _as_optional_bool(
            _get(
                record,
                ("valid", "has_position", "position_valid", "has_coordinates"),
                default=None,
            )
        )
        has_position = latitude is not None and longitude is not None
        if explicit_valid is False:
            has_position = False
        elif explicit_valid is True and not has_position:
            has_position = False

        revision = _as_revision(
            _get(record, ("revision", "version"), default=0)
        )
        report_id_value = _get(record, ("report_id", "id"), default=None)
        base_report_id = (
            "" if _is_missing(report_id_value) else str(report_id_value)
        )
        sources = _record_sources(record)

        for source_index, source in enumerate(sources):
            report_id = base_report_id
            if not report_id:
                report_id = f"{source or 'unknown'}@{stamp.isoformat()}"
                if len(sources) > 1:
                    report_id = f"{report_id}#{source_index}"
            normalized.append(
                Observation(
                    icao24=icao24,
                    time=stamp,
                    latitude=latitude,
                    longitude=longitude,
                    altitude=altitude,
                    source=source,
                    report_id=report_id,
                    revision=revision,
                    batch_id=batch_id,
                    has_position=has_position,
                    order=index,
                )
            )
    return tuple(normalized)


def _second_key(observation: Observation) -> tuple[str, pd.Timestamp]:
    second = observation.time.floor("s")
    return observation.icao24, second


def _latest_per_source(
    observations: Iterable[Observation],
) -> dict[str, Observation]:
    latest: dict[str, Observation] = {}
    for observation in observations:
        current = latest.get(observation.source)
        if current is None or (
            observation.revision,
            observation.order,
        ) >= (current.revision, current.order):
            latest[observation.source] = observation
    return latest


def _coalesce(
    key: tuple[str, pd.Timestamp],
    observations: Iterable[Observation],
) -> tuple[TrackPoint | None, tuple[Observation, ...]]:
    icao24, second = key
    latest = _latest_per_source(observations)
    reports = tuple(
        sorted(
            latest.values(),
            key=lambda o: (o.source, o.revision, o.report_id),
        )
    )
    valid = [report for report in reports if report.has_position]
    if not valid:
        return None, reports

    representative = sorted(
        valid,
        key=lambda o: (-o.revision, o.source, o.report_id),
    )[0]
    point = TrackPoint(
        icao24=icao24,
        time=second,
        latitude=representative.latitude,  # type: ignore[arg-type]
        longitude=representative.longitude,  # type: ignore[arg-type]
        altitude=representative.altitude,
        observations=reports,
        representative=representative,
    )
    return point, reports


def _segment_id(icao24: str, start: pd.Timestamp) -> str:
    return f"{icao24}@{int(start.timestamp())}"


def _build_segments(
    points: Iterable[TrackPoint], max_gap: pd.Timedelta
) -> dict[str, TrackSegment]:
    grouped: dict[str, list[TrackPoint]] = {}
    for point in points:
        grouped.setdefault(point.icao24, []).append(point)

    segments: dict[str, TrackSegment] = {}
    for icao24, aircraft_points in grouped.items():
        ordered = sorted(aircraft_points, key=lambda p: p.time)
        run: list[TrackPoint] = [ordered[0]]
        for point in ordered[1:]:
            if point.time - run[-1].time <= max_gap:
                run.append(point)
                continue
            segment = TrackSegment(
                segment_id=_segment_id(icao24, run[0].time),
                icao24=icao24,
                points=tuple(run),
            )
            segments[segment.segment_id] = segment
            run = [point]
        segment = TrackSegment(
            segment_id=_segment_id(icao24, run[0].time),
            icao24=icao24,
            points=tuple(run),
        )
        segments[segment.segment_id] = segment
    return segments


def _segment_span(segment: TrackSegment) -> tuple[pd.Timestamp, pd.Timestamp]:
    return segment.start, segment.end


def _select_triggering(
    operation: Iterable[Observation],
    icao24: str,
    spans: Sequence[tuple[pd.Timestamp, pd.Timestamp]],
) -> tuple[Observation, ...]:
    aircraft_observations = [
        observation for observation in operation if observation.icao24 == icao24
    ]
    selected = [
        observation
        for observation in aircraft_observations
        if any(start <= observation.time <= end for start, end in spans)
    ]
    if not selected:
        selected = aircraft_observations
    return tuple(
        sorted(
            selected,
            key=lambda o: (o.time, o.source, o.report_id, o.revision),
        )
    )


def _build_changes(
    before: Mapping[str, TrackSegment],
    after: Mapping[str, TrackSegment],
    operation: Iterable[Observation],
) -> tuple[ArchiveChange, ...]:
    operation = tuple(operation)
    changes: list[ArchiveChange] = []
    for segment_id in sorted(set(before) | set(after)):
        old = before.get(segment_id)
        new = after.get(segment_id)
        if old is None and new is not None:
            changes.append(
                ArchiveChange("add", segment_id, new.icao24, None, new, ())
            )
        elif old is not None and new is None:
            changes.append(
                ArchiveChange("retract", segment_id, old.icao24, old, None, ())
            )
        elif old is not None and new is not None and old != new:
            changes.append(
                ArchiveChange("replace", segment_id, old.icao24, old, new, ())
            )

    spans: dict[str, list[tuple[pd.Timestamp, pd.Timestamp]]] = {}
    for change in changes:
        for segment in (change.before, change.after):
            if segment is not None:
                spans.setdefault(change.icao24, []).append(
                    _segment_span(segment)
                )

    return tuple(
        ArchiveChange(
            action=change.action,
            segment_id=change.segment_id,
            icao24=change.icao24,
            before=change.before,
            after=change.after,
            triggering=_select_triggering(
                operation, change.icao24, spans.get(change.icao24, [])
            ),
        )
        for change in changes
    )


def apply_changes(
    state: Mapping[str, TrackSegment],
    changes: Iterable[ArchiveChange],
) -> dict[str, TrackSegment]:
    """Apply archive changes to a downstream segment mapping.

    The function validates that additions are new, and that retractions and
    replacements still match the downstream copy.  This catches lost or
    duplicated deliveries instead of silently overwriting a divergent archive.
    """
    result = dict(state)
    for change in changes:
        current = result.get(change.segment_id)
        if change.action == "add":
            if current is not None:
                raise ArchiveStateError(
                    f"cannot add existing segment {change.segment_id}"
                )
            assert change.after is not None
            result[change.segment_id] = change.after
        elif change.action == "retract":
            if current is None:
                raise ArchiveStateError(
                    f"cannot retract missing segment {change.segment_id}"
                )
            if change.before != current:
                raise ArchiveStateError(
                    f"downstream segment {change.segment_id} has diverged"
                )
            del result[change.segment_id]
        else:
            if current is None:
                raise ArchiveStateError(
                    f"cannot replace missing segment {change.segment_id}"
                )
            if change.before != current:
                raise ArchiveStateError(
                    f"downstream segment {change.segment_id} has diverged"
                )
            assert change.after is not None
            result[change.segment_id] = change.after
    return result


class TrackArchive:
    """Versioned archive of segmented ADS-B positions.

    :param max_gap: maximum time between two valid positions that still
        belongs to the same segment.  A gap exactly equal to the threshold is
        connectable; a larger gap starts a new segment.  Numbers are seconds.
    """

    def __init__(
        self, max_gap: float | timedelta | pd.Timedelta = 10.0
    ) -> None:
        if isinstance(max_gap, pd.Timedelta):
            gap = max_gap
        elif isinstance(max_gap, timedelta):
            gap = pd.Timedelta(max_gap)
        elif isinstance(max_gap, bool):
            raise TypeError("max_gap must be a duration, not a boolean")
        else:
            gap = pd.Timedelta(seconds=float(max_gap))
        if gap < pd.Timedelta(0):
            raise ValueError("max_gap must not be negative")
        self.max_gap = gap
        self._batches: list[_StoredBatch] = []
        self._withdrawn: dict[str, tuple[int, _StoredBatch]] = {}
        self._next_batch = 1
        self._segments: dict[str, TrackSegment] = {}
        self._contacts: tuple[Observation, ...] = ()

    @property
    def max_gap_seconds(self) -> float:
        """The maximum connectable gap expressed in seconds."""
        return self.max_gap.total_seconds()

    @property
    def batch_ids(self) -> tuple[str, ...]:
        """Identifiers of the batches currently contributing to the archive."""
        return tuple(batch.batch_id for batch in self._batches)

    @property
    def segments(self) -> tuple[TrackSegment, ...]:
        """Current segments ordered by aircraft and start time."""
        return tuple(
            sorted(
                self._segments.values(),
                key=lambda segment: (segment.icao24, segment.start),
            )
        )

    @property
    def state(self) -> dict[str, TrackSegment]:
        """Current segments keyed by stable segment identifier."""
        return dict(self._segments)

    @property
    def contacts(self) -> tuple[Observation, ...]:
        """Effective contact-only reports, kept as quality observations."""
        return self._contacts

    @property
    def observations(self) -> tuple[Observation, ...]:
        """All observations from active batches."""
        return tuple(
            observation
            for batch in self._batches
            for observation in batch.observations
        )

    def _new_batch_id(self) -> str:
        while True:
            candidate = f"batch-{self._next_batch}"
            self._next_batch += 1
            if candidate not in self._withdrawn and all(
                batch.batch_id != candidate for batch in self._batches
            ):
                return candidate

    def _check_batch_id(self, batch_id: str) -> None:
        if any(batch.batch_id == batch_id for batch in self._batches):
            raise ValueError(f"batch {batch_id!r} is already active")
        if batch_id in self._withdrawn:
            raise ValueError(
                f"batch {batch_id!r} was withdrawn; use restore_batch()"
            )

    def _compute(
        self,
    ) -> tuple[dict[str, TrackSegment], tuple[Observation, ...]]:
        batch_rank = {
            batch.batch_id: index for index, batch in enumerate(self._batches)
        }
        grouped: dict[tuple[str, pd.Timestamp], dict[str, Observation]] = {}
        for index, batch in enumerate(self._batches):
            for observation in batch.observations:
                key = _second_key(observation)
                by_source = grouped.setdefault(key, {})
                current = by_source.get(observation.source)
                if current is None or (
                    observation.revision,
                    index,
                    observation.order,
                ) >= (
                    current.revision,
                    batch_rank[current.batch_id],
                    current.order,
                ):
                    by_source[observation.source] = observation

        points: list[TrackPoint] = []
        contacts: list[Observation] = []
        for key in sorted(grouped, key=lambda k: (k[0], k[1])):
            point, reports = _coalesce(key, grouped[key].values())
            if point is None:
                contacts.extend(reports)
            else:
                points.append(point)

        contacts.sort(key=lambda o: (o.icao24, o.time, o.source, o.report_id))
        return _build_segments(points, self.max_gap), tuple(contacts)

    def _result(
        self,
        batch_id: str,
        changes: tuple[ArchiveChange, ...],
        operation: tuple[Observation, ...],
    ) -> ArchiveBatchResult:
        return ArchiveBatchResult(
            batch_id=batch_id,
            changes=changes,
            segments=self.segments,
            observations=operation,
            contacts=self._contacts,
        )

    def write(
        self,
        observations: Any,
        *,
        batch: str | int | None = None,
    ) -> ArchiveBatchResult:
        """Add one processing batch and return applicable changes.

        The batch is added only after all input records have been validated, so
        a malformed batch leaves the archive untouched.
        """
        batch_id = str(batch) if batch is not None else self._new_batch_id()
        self._check_batch_id(batch_id)
        normalized = _normalize_records(observations, batch_id)

        previous = self.state
        self._batches.append(_StoredBatch(batch_id, normalized))
        new_segments, new_contacts = self._compute()
        changes = _build_changes(previous, new_segments, normalized)
        self._segments = new_segments
        self._contacts = new_contacts
        return self._result(batch_id, changes, normalized)

    def retract_batch(self, batch_id: str) -> ArchiveBatchResult:
        """Withdraw a batch and return the resulting archive changes."""
        index = next(
            (
                i
                for i, batch in enumerate(self._batches)
                if batch.batch_id == batch_id
            ),
            None,
        )
        if index is None:
            raise KeyError(f"unknown active batch: {batch_id!r}")

        previous = self.state
        stored = self._batches.pop(index)
        self._withdrawn[batch_id] = (index, stored)
        new_segments, new_contacts = self._compute()
        changes = _build_changes(previous, new_segments, stored.observations)
        self._segments = new_segments
        self._contacts = new_contacts
        return self._result(batch_id, changes, stored.observations)

    def restore_batch(self, batch_id: str) -> ArchiveBatchResult:
        """Restore a previously withdrawn batch at its original position."""
        withdrawn = self._withdrawn.get(batch_id)
        if withdrawn is None:
            raise KeyError(f"unknown withdrawn batch: {batch_id!r}")
        index, stored = withdrawn
        if any(batch.batch_id == batch_id for batch in self._batches):
            raise ValueError(f"batch {batch_id!r} is already active")

        previous = self.state
        del self._withdrawn[batch_id]
        self._batches.insert(min(index, len(self._batches)), stored)
        new_segments, new_contacts = self._compute()
        changes = _build_changes(previous, new_segments, stored.observations)
        self._segments = new_segments
        self._contacts = new_contacts
        return self._result(batch_id, changes, stored.observations)

    def recompute(self) -> tuple[TrackSegment, ...]:
        """Recompute all segments from the effective observation batches."""
        segments, _ = self._compute()
        return tuple(
            sorted(
                segments.values(),
                key=lambda segment: (segment.icao24, segment.start),
            )
        )

    def to_dataframe(self) -> pd.DataFrame:
        """Flatten the current archive to one row per temporal position."""
        records = [
            record
            for segment in self.segments
            for record in segment.to_records()
        ]
        return pd.DataFrame.from_records(records)

    @classmethod
    def from_rebuild(
        cls,
        frame: pd.DataFrame,
        *,
        max_gap: float | timedelta | pd.Timedelta = 10.0,
        batch: str | int | None = None,
    ) -> TrackArchive:
        """Build an archive from a :class:`~pyopensky.rebuild.Rebuild` frame.

        ``Rebuild`` frames use ``timestamp`` for the actual position time and
        may carry a ``sensors`` list.  Both are accepted directly.
        """
        archive = cls(max_gap=max_gap)
        archive.write(frame, batch=batch)
        return archive


__all__ = [
    "ArchiveAction",
    "ArchiveBatchResult",
    "ArchiveChange",
    "ArchiveState",
    "ArchiveStateError",
    "Observation",
    "TrackArchive",
    "TrackPoint",
    "TrackSegment",
    "apply_changes",
]
