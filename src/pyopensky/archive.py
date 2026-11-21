"""Auditable trajectory archive reconciliation.

The OpenSky history tables are queried in contiguous UTC windows.  A window is
half-open: its start is included and its stop is excluded.  Reception reports
can nevertheless be observed more than once: overlapping pages, replayed
shards, and reports re-sent in a later extract all describe the same physical
reception.

This module keeps that history explicit:

* every raw report is identified by ``(source, report_id, revision)`` where a
  report number is stable within its receiver source;
* the highest revision of a report wins, while older revisions remain
  queryable as superseded reports;
* observations are keyed by aircraft address and observation time, never by
  callsign alone;
* identical coordinates reported by different receivers are merged into one
  observation with one entry per unique source;
* different coordinates at the same aircraft/time are retained as a conflict
  without discarding either the primary observation or unrelated data;
* the slices in which each raw report appeared are preserved separately from
  the list of receiver sources.

The module is deliberately independent from the network layer.  It accepts
pages returned by :meth:`pyopensky.trino.Trino.history` (or by a local replay
client) and produces deterministic, serialisable deliveries.
"""

from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, replace
from typing import Any, Iterable, Mapping, Sequence

import pandas as pd

from .time import timelike, to_datetime

__all__ = [
    "ArchiveDelivery",
    "ArchiveExtractor",
    "CoordinateConflict",
    "HistoryCondition",
    "LocalHistory",
    "Observation",
    "RawOccurrence",
    "ReceptionReport",
    "ReportPage",
    "ReportProvenance",
    "ReportRef",
    "SupersededReport",
    "TimeSlice",
    "history_condition",
    "pages_from_frame",
    "reconcile_reports",
    "reports_from_frame",
    "utc_slices",
]

DEFAULT_REPORT_COLUMNS: dict[str, str] = {
    "source": "source",
    "report_id": "report_id",
    "revision": "revision",
    "time": "time",
    "icao24": "icao24",
    "lat": "lat",
    "lon": "lon",
    "callsign": "callsign",
}

DEFAULT_PAGE_COLUMNS: dict[str, str] = {
    **DEFAULT_REPORT_COLUMNS,
    "slice_start": "slice_start",
    "slice_stop": "slice_stop",
}


def _as_utc(value: timelike) -> pd.Timestamp:
    timestamp = to_datetime(value)
    if timestamp.tzinfo is None:
        timestamp = timestamp.tz_localize("UTC")
    return timestamp.tz_convert("UTC")


def _as_timedelta(value: str | pd.Timedelta) -> pd.Timedelta:
    delta = pd.Timedelta(value)
    if delta <= pd.Timedelta(0):
        raise ValueError("time slices must advance by a positive duration")
    return delta


def _iso(timestamp: pd.Timestamp) -> str:
    return timestamp.isoformat().replace("+00:00", "Z")


def _coordinate(value: float, decimals: int) -> float:
    rounded = round(float(value), decimals)
    return 0.0 if rounded == 0 else rounded


@dataclass(frozen=True)
class TimeSlice:
    """A half-open UTC interval ``[start, stop)``."""

    start: pd.Timestamp
    stop: pd.Timestamp

    def __post_init__(self) -> None:
        start = _as_utc(self.start)
        stop = _as_utc(self.stop)
        if start >= stop:
            raise ValueError("a time slice must have start < stop")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "stop", stop)

    @property
    def label(self) -> str:
        return f"{_iso(self.start)}/{_iso(self.stop)}"

    @property
    def duration(self) -> pd.Timedelta:
        return self.stop - self.start

    def contains(self, value: timelike) -> bool:
        timestamp = _as_utc(value)
        return self.start <= timestamp < self.stop

    def to_dict(self) -> dict[str, str]:
        return {"start": _iso(self.start), "stop": _iso(self.stop)}


def utc_slices(
    start: timelike,
    stop: timelike,
    step: str | pd.Timedelta,
) -> tuple[TimeSlice, ...]:
    """Split ``[start, stop)`` into contiguous half-open UTC slices.

    The first slice starts exactly at ``start`` and the last one stops exactly
    at ``stop``.  The final slice may be shorter than ``step``.
    """

    window = TimeSlice(start, stop)
    delta = _as_timedelta(step)
    slices: list[TimeSlice] = []
    cursor = window.start
    while cursor < window.stop:
        boundary = min(cursor + delta, window.stop)
        slices.append(TimeSlice(cursor, boundary))
        cursor = boundary
    return tuple(slices)


@dataclass(frozen=True)
class HistoryCondition:
    """The concrete condition sent to a history query for one slice."""

    start: pd.Timestamp
    stop: pd.Timestamp
    label: str
    start_inclusive: bool = True
    stop_inclusive: bool = False
    sql: str | None = None

    def __post_init__(self) -> None:
        start = _as_utc(self.start)
        stop = _as_utc(self.stop)
        if start >= stop:
            raise ValueError("a history condition must have start < stop")
        if not self.start_inclusive:
            raise ValueError("history conditions always include their start")
        if self.stop_inclusive:
            raise ValueError("history conditions never include their stop")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "stop", stop)

    @classmethod
    def from_slice(
        cls,
        time_slice: TimeSlice,
        *,
        sql: str | None = None,
    ) -> HistoryCondition:
        return cls(
            start=time_slice.start,
            stop=time_slice.stop,
            label=time_slice.label,
            sql=sql,
        )

    @property
    def predicate(self) -> str:
        return f"time >= {_iso(self.start)} AND time < {_iso(self.stop)} (UTC)"

    def to_dict(self) -> dict[str, Any]:
        return {
            "label": self.label,
            "start": _iso(self.start),
            "stop": _iso(self.stop),
            "start_inclusive": self.start_inclusive,
            "stop_inclusive": self.stop_inclusive,
            "predicate": self.predicate,
            "sql": self.sql,
        }


def history_condition(
    start: timelike,
    stop: timelike,
    *,
    label: str | None = None,
    sql: str | None = None,
) -> HistoryCondition:
    """Build the canonical half-open condition used by history queries."""

    start_ts = _as_utc(start)
    stop_ts = _as_utc(stop)
    if label is None:
        label = TimeSlice(start_ts, stop_ts).label
    return HistoryCondition(start=start_ts, stop=stop_ts, label=label, sql=sql)


@dataclass(frozen=True, order=True)
class ReportRef:
    """The stable identity of a reception report within its source."""

    source: str
    report_id: str
    revision: int

    @classmethod
    def of(cls, report: ReceptionReport) -> ReportRef:
        return cls(
            source=report.source,
            report_id=report.report_id,
            revision=report.revision,
        )

    @property
    def label(self) -> str:
        return f"{self.source}:{self.report_id}@{self.revision}"


@dataclass(frozen=True)
class ReceptionReport:
    """One raw coordinate report received from one source."""

    source: str
    report_id: str
    revision: int
    icao24: str
    observed_at: pd.Timestamp
    lat: float
    lon: float
    callsign: str | None = None

    def __post_init__(self) -> None:
        source = str(self.source).strip()
        report_id = str(self.report_id).strip()
        icao24 = str(self.icao24).strip().lower()
        revision = int(self.revision)
        if not source:
            raise ValueError("source must not be empty")
        if not report_id:
            raise ValueError("report_id must not be empty")
        if not icao24:
            raise ValueError("icao24 must not be empty")
        lat = float(self.lat)
        lon = float(self.lon)
        if not math.isfinite(lat) or not math.isfinite(lon):
            raise ValueError("coordinates must be finite")
        if not -90 <= lat <= 90:
            raise ValueError("latitude must be within [-90, 90]")
        if not -180 <= lon <= 180:
            raise ValueError("longitude must be within [-180, 180]")
        callsign: str | None
        if self.callsign is None or pd.isna(self.callsign):
            callsign = None
        else:
            text = str(self.callsign).rstrip()
            callsign = text or None
        object.__setattr__(self, "source", source)
        object.__setattr__(self, "report_id", report_id)
        object.__setattr__(self, "revision", revision)
        object.__setattr__(self, "icao24", icao24)
        object.__setattr__(self, "observed_at", _as_utc(self.observed_at))
        object.__setattr__(self, "lat", lat)
        object.__setattr__(self, "lon", lon)
        object.__setattr__(self, "callsign", callsign)

    @property
    def ref(self) -> ReportRef:
        return ReportRef.of(self)

    def coordinate_key(self, decimals: int = 6) -> tuple[float, float]:
        return (
            _coordinate(self.lat, decimals),
            _coordinate(self.lon, decimals),
        )

    def payload_key(self, decimals: int = 6) -> tuple[Any, ...]:
        lat, lon = self.coordinate_key(decimals)
        return (
            self.icao24,
            self.observed_at.value,
            lat,
            lon,
            self.callsign or "",
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "report_id": self.report_id,
            "revision": self.revision,
            "icao24": self.icao24,
            "observed_at": _iso(self.observed_at),
            "lat": self.lat,
            "lon": self.lon,
            "callsign": self.callsign,
        }


@dataclass(frozen=True)
class ReportPage:
    """One page of reports returned for one history request."""

    reports: tuple[ReceptionReport, ...]
    slice: TimeSlice | None = None
    condition: HistoryCondition | None = None
    sequence: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "reports", tuple(self.reports))

    @property
    def slice_label(self) -> str | None:
        return None if self.slice is None else self.slice.label


@dataclass(frozen=True)
class SupersededReport:
    """A report revision made obsolete by a later revision of the same id."""

    report: ReceptionReport
    superseded_by: int
    slices: tuple[str, ...]
    occurrences: int = 1

    @property
    def ref(self) -> ReportRef:
        return self.report.ref


@dataclass(frozen=True)
class CoordinateConflict:
    """A different coordinate reported for the same aircraft and time."""

    lat: float
    lon: float
    sources: tuple[str, ...]
    reports: tuple[ReportRef, ...]


@dataclass(frozen=True)
class Observation:
    """One reconciled coordinate observation."""

    icao24: str
    observed_at: pd.Timestamp
    lat: float
    lon: float
    callsign: str | None
    callsigns: tuple[str, ...]
    sources: tuple[str, ...]
    reports: tuple[ReportRef, ...]
    conflicts: tuple[CoordinateConflict, ...] = ()

    @property
    def key(self) -> tuple[str, pd.Timestamp]:
        return (self.icao24, self.observed_at)

    @property
    def confirmed_by(self) -> int:
        return len(self.sources)


@dataclass(frozen=True)
class ReportProvenance:
    """Where one raw report revision appeared and how it was resolved."""

    report: ReceptionReport
    slices: tuple[str, ...]
    occurrences: int
    status: str
    superseded_by: int | None = None
    payload_variants: int = 1

    @property
    def ref(self) -> ReportRef:
        return self.report.ref


@dataclass(frozen=True)
class RawOccurrence:
    """One raw report as returned in one page."""

    report: ReceptionReport
    slice: TimeSlice
    page_sequence: int = 0

    @property
    def ref(self) -> ReportRef:
        return self.report.ref


@dataclass(frozen=True)
class ArchiveDelivery:
    """A deterministic, auditable set of reconciled observations."""

    observations: tuple[Observation, ...]
    superseded: tuple[SupersededReport, ...]
    provenance: tuple[ReportProvenance, ...]
    conditions: tuple[HistoryCondition, ...] = ()
    request_log: tuple[HistoryCondition, ...] = ()
    window: TimeSlice | None = None
    occurrences: tuple[RawOccurrence, ...] = ()

    def observation_index(
        self,
    ) -> dict[tuple[str, pd.Timestamp], Observation]:
        return {
            observation.key: observation for observation in self.observations
        }

    def canonical_observations(self) -> tuple[tuple[str, str], ...]:
        return tuple(
            (observation.icao24, _iso(observation.observed_at))
            for observation in self.observations
        )

    def restrict(
        self,
        start: timelike,
        stop: timelike,
    ) -> ArchiveDelivery:
        """Return the observations inside a half-open interval.

        Provenance and superseded reports are retained so a narrowed delivery
        remains as auditable as the extract it came from.
        """

        window = TimeSlice(start, stop)
        return replace(
            self,
            observations=tuple(
                observation
                for observation in self.observations
                if window.contains(observation.observed_at)
            ),
            window=window,
        )

    def assert_consistent_with(
        self,
        other: ArchiveDelivery,
        *,
        compare_payloads: bool = True,
    ) -> None:
        """Check two deliveries agree by aircraft and observation time."""

        left = self.canonical_observations()
        right = other.canonical_observations()
        if left != right:
            raise AssertionError(
                "deliveries differ by aircraft/observation time: "
                f"{left!r} != {right!r}"
            )
        if compare_payloads:
            for key, observation in self.observation_index().items():
                if observation != other.observation_index()[key]:
                    raise AssertionError(
                        f"delivery payload differs for {key!r}: "
                        f"{observation!r} != {other.observation_index()[key]!r}"
                    )

    def observations_frame(self) -> pd.DataFrame:
        rows = [
            dict(
                icao24=observation.icao24,
                observed_at=observation.observed_at,
                callsign=observation.callsign,
                callsigns="|".join(observation.callsigns),
                lat=observation.lat,
                lon=observation.lon,
                sources="|".join(observation.sources),
                confirmed_by=observation.confirmed_by,
                reports="|".join(ref.label for ref in observation.reports),
                revisions="|".join(
                    str(ref.revision) for ref in observation.reports
                ),
                conflict_count=len(observation.conflicts),
            )
            for observation in self.observations
        ]
        return pd.DataFrame(
            rows,
            columns=[
                "icao24",
                "observed_at",
                "callsign",
                "callsigns",
                "lat",
                "lon",
                "sources",
                "confirmed_by",
                "reports",
                "revisions",
                "conflict_count",
            ],
        )

    def conflicts_frame(self) -> pd.DataFrame:
        rows = [
            dict(
                icao24=observation.icao24,
                observed_at=observation.observed_at,
                primary_lat=observation.lat,
                primary_lon=observation.lon,
                primary_sources="|".join(observation.sources),
                conflict_lat=conflict.lat,
                conflict_lon=conflict.lon,
                conflict_sources="|".join(conflict.sources),
                conflict_reports="|".join(
                    ref.label for ref in conflict.reports
                ),
            )
            for observation in self.observations
            for conflict in observation.conflicts
        ]
        return pd.DataFrame(
            rows,
            columns=[
                "icao24",
                "observed_at",
                "primary_lat",
                "primary_lon",
                "primary_sources",
                "conflict_lat",
                "conflict_lon",
                "conflict_sources",
                "conflict_reports",
            ],
        )

    def superseded_frame(self) -> pd.DataFrame:
        rows = [
            dict(
                source=item.report.source,
                report_id=item.report.report_id,
                revision=item.report.revision,
                superseded_by=item.superseded_by,
                icao24=item.report.icao24,
                observed_at=item.report.observed_at,
                lat=item.report.lat,
                lon=item.report.lon,
                callsign=item.report.callsign,
                slices="|".join(item.slices),
                occurrences=item.occurrences,
                status="superseded",
            )
            for item in self.superseded
        ]
        return pd.DataFrame(
            rows,
            columns=[
                "source",
                "report_id",
                "revision",
                "superseded_by",
                "icao24",
                "observed_at",
                "lat",
                "lon",
                "callsign",
                "slices",
                "occurrences",
                "status",
            ],
        )

    def provenance_frame(self) -> pd.DataFrame:
        rows = [
            dict(
                source=item.report.source,
                report_id=item.report.report_id,
                revision=item.report.revision,
                icao24=item.report.icao24,
                observed_at=item.report.observed_at,
                lat=item.report.lat,
                lon=item.report.lon,
                callsign=item.report.callsign,
                slices="|".join(item.slices),
                occurrences=item.occurrences,
                payload_variants=item.payload_variants,
                status=item.status,
                superseded_by=item.superseded_by,
            )
            for item in self.provenance
        ]
        return pd.DataFrame(
            rows,
            columns=[
                "source",
                "report_id",
                "revision",
                "icao24",
                "observed_at",
                "lat",
                "lon",
                "callsign",
                "slices",
                "occurrences",
                "payload_variants",
                "status",
                "superseded_by",
            ],
        )

    def conditions_frame(self) -> pd.DataFrame:
        rows = [
            dict(
                label=condition.label,
                start=condition.start,
                stop=condition.stop,
                start_inclusive=condition.start_inclusive,
                stop_inclusive=condition.stop_inclusive,
                predicate=condition.predicate,
                sql=condition.sql,
            )
            for condition in self.conditions
        ]
        return pd.DataFrame(
            rows,
            columns=[
                "label",
                "start",
                "stop",
                "start_inclusive",
                "stop_inclusive",
                "predicate",
                "sql",
            ],
        )

    def raw_frame(self) -> pd.DataFrame:
        """Every raw report occurrence, with the slice it came from.

        Raw reports are recorded before revision resolution and de-duplication,
        so a replayed page stays visible here without becoming a second
        receiver source.  Use :func:`pages_from_frame` to replay a delivery.
        """

        rows = [
            dict(
                source=item.report.source,
                report_id=item.report.report_id,
                revision=item.report.revision,
                time=item.report.observed_at,
                icao24=item.report.icao24,
                lat=item.report.lat,
                lon=item.report.lon,
                callsign=item.report.callsign,
                slice_start=item.slice.start,
                slice_stop=item.slice.stop,
                page_sequence=item.page_sequence,
            )
            for item in self.occurrences
        ]
        return pd.DataFrame(
            rows,
            columns=[
                *DEFAULT_PAGE_COLUMNS.values(),
                "page_sequence",
            ],
        )

    def frames(self) -> dict[str, pd.DataFrame]:
        return {
            "observations": self.observations_frame(),
            "conflicts": self.conflicts_frame(),
            "superseded": self.superseded_frame(),
            "provenance": self.provenance_frame(),
            "history-conditions": self.conditions_frame(),
        }

    def manifest(self) -> dict[str, Any]:
        frames = self.frames()
        return {
            "window": None if self.window is None else self.window.to_dict(),
            "observations": len(self.observations),
            "conflicts": sum(
                len(observation.conflicts) for observation in self.observations
            ),
            "superseded": len(self.superseded),
            "raw_reports": len(self.provenance),
            "conditions": [item.to_dict() for item in self.conditions],
            "request_log": [item.to_dict() for item in self.request_log],
            "observation_keys": [
                [icao24, timestamp]
                for icao24, timestamp in self.canonical_observations()
            ],
            "frames": {
                name: hashlib.sha256(
                    frame.to_csv(index=False).encode("utf8")
                ).hexdigest()
                for name, frame in frames.items()
            },
        }

    def to_json(self, *, indent: int = 2) -> str:
        return json.dumps(self.manifest(), indent=indent, sort_keys=True)


def _report_for_row(
    row: Mapping[str, Any],
    columns: Mapping[str, str],
) -> ReceptionReport:
    def value(name: str) -> Any:
        return row[columns[name]]

    callsign_value = (
        None if "callsign" not in columns else row[columns["callsign"]]
    )
    revision = value("revision")
    if isinstance(revision, float) and revision.is_integer():
        revision = int(revision)
    return ReceptionReport(
        source=value("source"),
        report_id=value("report_id"),
        revision=revision,
        icao24=value("icao24"),
        observed_at=value("time"),
        lat=value("lat"),
        lon=value("lon"),
        callsign=callsign_value,
    )


def reports_from_frame(
    frame: pd.DataFrame | None,
    *,
    columns: Mapping[str, str] | None = None,
) -> tuple[ReceptionReport, ...]:
    """Convert a history response into reception reports.

    ``columns`` maps logical names (``source``, ``report_id``, ``revision``,
    ``time``, ``icao24``, ``lat``, ``lon`` and optional ``callsign``) to the
    actual response column names.  A response without the provenance columns
    cannot be reconciled and raises a clear error.
    """

    if frame is None or frame.shape[0] == 0:
        return ()
    mapping = dict(DEFAULT_REPORT_COLUMNS if columns is None else columns)
    required = [
        "source",
        "report_id",
        "revision",
        "time",
        "icao24",
        "lat",
        "lon",
    ]
    missing = [
        logical
        for logical in required
        if logical not in mapping or mapping[logical] not in frame.columns
    ]
    if missing:
        raise ValueError(
            "history response is missing report provenance columns: "
            + ", ".join(missing)
        )
    if "callsign" in mapping and mapping["callsign"] not in frame.columns:
        mapping.pop("callsign")
    return tuple(
        _report_for_row(row, mapping) for row in frame.to_dict("records")
    )


def pages_from_frame(
    frame: pd.DataFrame | None,
    *,
    columns: Mapping[str, str] | None = None,
) -> tuple[ReportPage, ...]:
    """Load recorded pages, including the slice each page came from.

    This is the format written by :meth:`ArchiveDelivery.raw_frame`: one row
    per report occurrence and two columns holding the slice boundaries.
    """

    if frame is None or frame.shape[0] == 0:
        return ()
    mapping = dict(DEFAULT_PAGE_COLUMNS if columns is None else columns)
    for name in ("slice_start", "slice_stop"):
        if name not in mapping or mapping[name] not in frame.columns:
            raise ValueError(f"recorded page is missing the {name} column")
    report_columns = {
        key: value
        for key, value in mapping.items()
        if key in DEFAULT_REPORT_COLUMNS
    }
    grouped: dict[
        tuple[pd.Timestamp, pd.Timestamp, int], list[ReceptionReport]
    ] = {}
    for row in frame.to_dict("records"):
        time_slice = TimeSlice(
            row[mapping["slice_start"]], row[mapping["slice_stop"]]
        )
        sequence = int(row.get("page_sequence", 0))
        grouped.setdefault(
            (time_slice.start, time_slice.stop, sequence), []
        ).append(_report_for_row(row, report_columns))
    return tuple(
        ReportPage(
            reports=tuple(reports),
            slice=TimeSlice(start, stop),
            sequence=sequence,
        )
        for (start, stop, sequence), reports in sorted(grouped.items())
    )


def _last_sql(client: Any) -> str | None:
    statements = getattr(client, "statements", None)
    if not statements:
        return None
    statement = statements[-1]
    try:
        return str(
            statement.compile(
                compile_kwargs={"literal_binds": True},
            )
        )
    except (AttributeError, TypeError, ValueError):
        try:
            return str(statement)
        except Exception:  # pragma: no cover - defensive fallback
            return None


class LocalHistory:
    """Replay locally recorded pages and record actual history conditions.

    Recorded pages are authoritative: a page may contain a report whose
    observation time lies outside its nominal window when the extractor
    returned a late or replayed row.  Each call returns every recorded
    occurrence for the exactly matching slice and appends the received
    condition to :attr:`requests`.
    """

    def __init__(self, pages: Sequence[ReportPage]) -> None:
        self.pages = tuple(pages)
        self.requests: list[HistoryCondition] = []
        self.responses: list[ReportPage | None] = []

    def history(
        self,
        start: timelike,
        stop: timelike,
        **kwargs: Any,
    ) -> pd.DataFrame | None:
        condition = history_condition(start, stop)
        condition = replace(condition, sql=condition.predicate)
        self.requests.append(condition)
        matching = [
            page
            for page in self.pages
            if page.slice is not None
            and page.slice.start == condition.start
            and page.slice.stop == condition.stop
        ]
        reports = [report for page in matching for report in page.reports]
        self.responses.append(
            None
            if not reports
            else ReportPage(
                reports=tuple(reports),
                slice=matching[0].slice,
                condition=condition,
            )
        )
        if not reports:
            return None
        return pd.DataFrame(
            [
                dict(
                    source=report.source,
                    report_id=report.report_id,
                    revision=report.revision,
                    time=report.observed_at,
                    icao24=report.icao24,
                    lat=report.lat,
                    lon=report.lon,
                    callsign=report.callsign,
                )
                for report in reports
            ],
            columns=[
                "source",
                "report_id",
                "revision",
                "time",
                "icao24",
                "lat",
                "lon",
                "callsign",
            ],
        )


class ArchiveExtractor:
    """Query history in half-open slices and reconcile the returned pages."""

    def __init__(
        self,
        client: Any,
        *,
        step: str | pd.Timedelta = "10min",
        columns: Mapping[str, str] | None = None,
        coordinate_decimals: int = 6,
    ) -> None:
        self.client = client
        self.step = step
        self.columns = dict(
            DEFAULT_REPORT_COLUMNS if columns is None else columns
        )
        self.coordinate_decimals = coordinate_decimals

    def extract(
        self,
        start: timelike,
        stop: timelike,
        *,
        request_order: str = "forward",
        page_order: str | None = None,
        selected_columns: Sequence[str] | None = None,
        cached: bool = False,
        window_filter: bool = True,
        **history_kwargs: Any,
    ) -> ArchiveDelivery:
        """Run one history query per slice and reconcile all pages.

        ``request_order`` is ``"forward"`` or ``"reverse"``.  ``page_order``
        controls the order in which returned pages are handed to the
        reconciler; ``"reverse"`` models later pages arriving first.  The final
        delivery is independent of both orders.
        """

        window = TimeSlice(start, stop)
        slices = list(utc_slices(window.start, window.stop, self.step))
        if request_order == "forward":
            ordered_slices = slices
        elif request_order == "reverse":
            ordered_slices = list(reversed(slices))
        else:
            raise ValueError("request_order must be 'forward' or 'reverse'")

        pages: list[ReportPage] = []
        request_log: list[HistoryCondition] = []
        for sequence, time_slice in enumerate(ordered_slices):
            kwargs = dict(history_kwargs)
            kwargs["cached"] = cached
            if selected_columns is not None:
                kwargs["selected_columns"] = tuple(selected_columns)
            frame = self.client.history(
                time_slice.start, time_slice.stop, **kwargs
            )
            recorded = getattr(self.client, "requests", None)
            sql = _last_sql(self.client)
            if sql is None and recorded:
                sql = recorded[-1].sql
            condition = HistoryCondition.from_slice(time_slice, sql=sql)
            request_log.append(condition)
            pages.append(
                ReportPage(
                    reports=reports_from_frame(frame, columns=self.columns),
                    slice=time_slice,
                    condition=condition,
                    sequence=sequence,
                )
            )

        if page_order is None:
            page_order = request_order
        if page_order == "forward":
            ordered_pages = pages
        elif page_order == "reverse":
            ordered_pages = list(reversed(pages))
        elif page_order == "recorded":
            ordered_pages = sorted(pages, key=lambda page: page.sequence)
        else:
            raise ValueError(
                "page_order must be 'forward', 'reverse' or 'recorded'"
            )

        delivery = reconcile_reports(
            ordered_pages,
            window=window if window_filter else None,
            coordinate_decimals=self.coordinate_decimals,
        )
        return replace(
            delivery,
            request_log=tuple(request_log),
        )


def _reconcile(
    pages: Iterable[ReportPage],
    *,
    window: TimeSlice | None,
    coordinate_decimals: int,
) -> ArchiveDelivery:
    # Identity -> the payload variants and slices observed for that identity.
    records: dict[ReportRef, dict[str, Any]] = {}
    conditions: dict[tuple[pd.Timestamp, pd.Timestamp], HistoryCondition] = {}
    occurrences: list[RawOccurrence] = []
    for page in pages:
        if page.condition is not None:
            conditions[(page.condition.start, page.condition.stop)] = (
                page.condition
            )
        elif page.slice is not None:
            conditions[(page.slice.start, page.slice.stop)] = (
                HistoryCondition.from_slice(page.slice)
            )
        for report in page.reports:
            ref = report.ref
            record = records.setdefault(
                ref,
                {"reports": {}, "slices": set(), "occurrences": 0},
            )
            record["reports"][report.payload_key(coordinate_decimals)] = report
            if page.slice_label is not None:
                record["slices"].add(page.slice_label)
            record["occurrences"] += 1
            if page.slice is not None:
                occurrences.append(
                    RawOccurrence(
                        report=report,
                        slice=page.slice,
                        page_sequence=page.sequence,
                    )
                )

    chosen: dict[ReportRef, ReceptionReport] = {}
    for ref, record in records.items():
        variants = record["reports"]
        counts = {
            key: sum(
                1
                for report in variants.values()
                if report.payload_key(coordinate_decimals) == key
            )
            for key in variants
        }
        best_payload = min(variants, key=lambda key: (-counts[key], key))
        chosen[ref] = variants[best_payload]

    # Resolve revisions within each (source, report_id) identity.
    by_identity: dict[tuple[str, str], list[ReportRef]] = {}
    for ref in records:
        by_identity.setdefault((ref.source, ref.report_id), []).append(ref)

    selected: list[ReceptionReport] = []
    superseded: list[SupersededReport] = []
    status: dict[ReportRef, tuple[str, int | None]] = {}
    for refs in by_identity.values():
        best_revision = max(ref.revision for ref in refs)
        for ref in sorted(refs):
            record = records[ref]
            report = chosen[ref]
            if ref.revision < best_revision:
                status[ref] = ("superseded", best_revision)
                superseded.append(
                    SupersededReport(
                        report=report,
                        superseded_by=best_revision,
                        slices=tuple(sorted(record["slices"])),
                        occurrences=record["occurrences"],
                    )
                )
            else:
                status[ref] = ("selected", None)
                selected.append(report)

    # Group selected reports by aircraft and observation time.  Callsign is
    # deliberately not part of the key: two aircraft may share a callsign.
    grouped: dict[tuple[str, pd.Timestamp], list[ReceptionReport]] = {}
    for report in selected:
        grouped.setdefault((report.icao24, report.observed_at), []).append(
            report
        )

    observations: list[Observation] = []
    for (icao24, observed_at), reports in grouped.items():
        if window is not None and not window.contains(observed_at):
            continue
        by_coordinate: dict[tuple[float, float], list[ReceptionReport]] = {}
        for report in reports:
            by_coordinate.setdefault(
                report.coordinate_key(coordinate_decimals), []
            ).append(report)

        candidates: list[dict[str, Any]] = []
        for coordinate, members in by_coordinate.items():
            members = sorted(
                members,
                key=lambda item: (
                    item.payload_key(coordinate_decimals),
                    item.ref,
                ),
            )
            sources = tuple(sorted({item.source for item in members}))
            refs = tuple(sorted({item.ref for item in members}))
            candidates.append(
                dict(
                    lat=members[0].lat,
                    lon=members[0].lon,
                    coordinate=coordinate,
                    sources=sources,
                    reports=refs,
                    max_revision=max(item.revision for item in members),
                )
            )

        # Prefer independent confirmation over a single high revision.  Every
        # remaining tie is broken deterministically so page arrival order never
        # changes the delivery.
        candidates.sort(
            key=lambda item: (
                -len(item["sources"]),
                -item["max_revision"],
                item["sources"],
                item["coordinate"],
                item["reports"],
            )
        )
        primary = candidates[0]
        conflicts = tuple(
            CoordinateConflict(
                lat=item["lat"],
                lon=item["lon"],
                sources=item["sources"],
                reports=item["reports"],
            )
            for item in candidates[1:]
        )
        primary_reports = [
            report for report in reports if report.ref in primary["reports"]
        ]
        callsigns = tuple(
            sorted(
                {
                    report.callsign
                    for report in reports
                    if report.callsign is not None
                }
            )
        )
        callsign_counts = {
            callsign: sum(
                1 for report in primary_reports if report.callsign == callsign
            )
            for callsign in callsigns
        }
        primary_callsign = (
            min(
                callsign_counts,
                key=lambda item: (-callsign_counts[item], item),
            )
            if callsign_counts
            else None
        )
        observations.append(
            Observation(
                icao24=icao24,
                observed_at=observed_at,
                lat=primary["lat"],
                lon=primary["lon"],
                callsign=primary_callsign,
                callsigns=callsigns,
                sources=primary["sources"],
                reports=primary["reports"],
                conflicts=conflicts,
            )
        )

    observations.sort(key=lambda item: (item.icao24, item.observed_at))
    superseded.sort(
        key=lambda item: (
            item.report.source,
            item.report.report_id,
            item.report.revision,
        )
    )

    provenance: list[ReportProvenance] = []
    for ref in sorted(records):
        record = records[ref]
        report_status, superseded_by = status[ref]
        provenance.append(
            ReportProvenance(
                report=chosen[ref],
                slices=tuple(sorted(record["slices"])),
                occurrences=record["occurrences"],
                status=report_status,
                superseded_by=superseded_by,
                payload_variants=len(record["reports"]),
            )
        )

    ordered_conditions = tuple(conditions[key] for key in sorted(conditions))
    occurrences.sort(
        key=lambda item: (
            item.slice.start,
            item.slice.stop,
            item.page_sequence,
            item.report.source,
            item.report.report_id,
            item.report.revision,
        )
    )
    return ArchiveDelivery(
        observations=tuple(observations),
        superseded=tuple(superseded),
        provenance=tuple(provenance),
        conditions=ordered_conditions,
        window=window,
        occurrences=tuple(occurrences),
    )


def reconcile_reports(
    pages: Iterable[ReportPage],
    *,
    window: TimeSlice | None = None,
    coordinate_decimals: int = 6,
) -> ArchiveDelivery:
    """Reconcile raw report pages into a deterministic delivery.

    ``window`` filters the final observations to a half-open interval while
    leaving every raw report and its slice provenance intact.
    """

    return _reconcile(
        pages,
        window=window,
        coordinate_decimals=coordinate_decimals,
    )
