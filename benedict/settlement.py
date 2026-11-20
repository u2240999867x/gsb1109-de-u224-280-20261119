from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from datetime import date as Date
from enum import Enum
from typing import Any, Final, cast
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from benedict.dicts.parse import parse_util
from benedict.exceptions import ExtrasRequireModuleNotFoundError
from benedict.utils import type_util

__all__ = [
    "AmbiguousSettlementTime",
    "SETTLEMENT_UNSET",
    "SiteCalendar",
    "SettlementCalendar",
    "SettlementDateBoundary",
    "SettlementDay",
    "SettlementField",
    "SettlementIssue",
    "SettlementIssueCode",
    "SettlementPreflight",
    "SettlementRecord",
    "SettlementResult",
    "SettlementStatus",
    "TimeCandidate",
    "build_settlement_record",
    "preflight_settlement",
    "preflight_settlement_record",
    "settlement_preflight",
]

_UNSET: Final = object()
"""Sentinel used to distinguish an omitted argument from an explicit ``None``."""

SETTLEMENT_UNSET: Final = _UNSET

_INVALID: Final = object()
"""Sentinel returned by helpers when a value cannot be normalized."""


class SettlementStatus(str, Enum):
    """Outcome of converting one settlement record."""

    RESOLVED = "resolved"
    """The record resolves to exactly one instant and can be stored."""

    AMBIGUOUS = "ambiguous"
    """The wall-clock time is valid, but more than one instant is possible."""

    UNCONFIRMED = "unconfirmed"
    """The source explicitly says the offset is unknown (JSON null)."""

    INVALID = "invalid"
    """A required value is missing or malformed; do not store it."""


class CandidateList(list[Any]):
    """A list of candidates that also compares equal to a tuple."""

    def __eq__(self, other: object) -> bool:
        if isinstance(other, tuple):
            return list(self) == list(other)
        return list.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        return not self == other


class SourcePath(tuple[Any, ...]):
    """A source path that compares equal to both a tuple and dotted string."""

    def __new__(cls, parts: Sequence[str | int]) -> SourcePath:
        return tuple.__new__(cls, parts)

    def __str__(self) -> str:
        return format_path(self)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, str):
            return str(self) == other
        return tuple.__eq__(self, other)

    def __ne__(self, other: object) -> bool:
        return not self == other

    __hash__ = tuple.__hash__

    def to_list(self) -> list[str | int]:
        return list(self)


class SettlementIssueCode(str, Enum):
    """Stable, machine-readable issue codes."""

    MISSING_FIELD = "missing_field"
    NULL_FIELD = "null_field"
    INVALID_FIELD = "invalid_field"
    MISSING_OFFSET = "missing_offset"
    AMBIGUOUS_TIME = "ambiguous_time"
    INVALID_VERSION = "invalid_version"
    LEGACY_FIELD_ONLY = "legacy_field_only"


class SettlementField:
    """A source value together with its exact path and parsing state."""

    __slots__ = ("path", "value", "present", "valid")

    def __init__(
        self,
        path: Sequence[str | int],
        value: Any,
        *,
        present: bool = True,
        valid: bool = True,
    ) -> None:
        self.path = SourcePath(path)
        self.value = value
        self.present = present
        self.valid = valid

    @property
    def keypath(self) -> SourcePath:
        return self.path

    @property
    def path_string(self) -> str:
        return format_path(self.path)

    @property
    def can_store(self) -> bool:
        return self.present and self.valid and self.value is not None

    @property
    def missing(self) -> bool:
        return not self.present

    @property
    def null(self) -> bool:
        return self.present and self.value is None

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SettlementField):
            return NotImplemented
        return (
            self.path == other.path
            and self.value == other.value
            and self.present == other.present
            and self.valid == other.valid
        )

    def __hash__(self) -> int:
        return hash((self.path, self.present, self.valid, repr(self.value)))

    def __repr__(self) -> str:
        state = "valid" if self.valid else "invalid"
        if not self.present:
            return f"SettlementField({self.path_string!r}, <missing>)"
        return f"SettlementField({self.path_string!r}, {self.value!r}, {state})"

    def to_dict(self) -> dict[str, Any]:
        value: Any = self.value
        if isinstance(value, (datetime, date)):
            value = value.isoformat()
        return {
            "path": list(self.path),
            "path_string": self.path_string,
            "value": value,
            "present": self.present,
            "valid": self.valid,
            "can_store": self.can_store,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def keys(self) -> Any:
        return self.to_dict().keys()

    def items(self) -> Any:
        return self.to_dict().items()

    def values(self) -> Any:
        return self.to_dict().values()

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()

    def __iter__(self) -> Any:
        return iter(self.to_dict())


@dataclass(frozen=True)
class SettlementIssue:
    """A field-level problem that requires checking the source record."""

    code: SettlementIssueCode
    message: str
    path: tuple[str | int, ...] = ()
    expected: str | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", SourcePath(self.path))

    @property
    def path_string(self) -> str:
        return format_path(self.path)

    def __str__(self) -> str:
        return self.message

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "message": self.message,
            "path": list(self.path),
            "path_string": self.path_string,
            "expected": self.expected,
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def keys(self) -> Any:
        return self.to_dict().keys()

    def items(self) -> Any:
        return self.to_dict().items()

    def values(self) -> Any:
        return self.to_dict().values()

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()

    def __iter__(self) -> Any:
        return iter(self.to_dict())


@dataclass(frozen=True)
class TimeCandidate:
    """One possible interpretation of a local wall-clock time."""

    local_time: datetime
    offset: timedelta | None
    utc_time: datetime | None
    fold: int | None = None
    settlement_date: date | None = None

    @property
    def offset_minutes(self) -> int | None:
        if self.offset is None:
            return None
        seconds = self.offset.total_seconds()
        return int(seconds // 60)

    @property
    def offset_seconds(self) -> int | None:
        if self.offset is None:
            return None
        return int(self.offset.total_seconds())

    @property
    def utc(self) -> datetime | None:
        return self.utc_time

    @property
    def local(self) -> datetime:
        return self.local_time

    @property
    def offset_hours(self) -> float | None:
        if self.offset is None:
            return None
        return self.offset.total_seconds() / 3600

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def keys(self) -> Any:
        return self.to_dict().keys()

    def items(self) -> Any:
        return self.to_dict().items()

    def values(self) -> Any:
        return self.to_dict().values()

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()

    def __iter__(self) -> Any:
        return iter(self.to_dict())

    def to_dict(self) -> dict[str, Any]:
        return {
            "local_time": self.local_time.isoformat(),
            "offset": self.offset_minutes,
            "offset_seconds": (
                int(self.offset.total_seconds()) if self.offset is not None else None
            ),
            "utc_time": self.utc_time.isoformat() if self.utc_time else None,
            "fold": self.fold,
            "settlement_date": (
                self.settlement_date.isoformat() if self.settlement_date else None
            ),
        }


@dataclass(frozen=True)
class SettlementDay:
    """A settlement day derived from the site calendar."""

    day: date
    boundary: datetime
    local_time: datetime
    utc_time: datetime
    candidate: TimeCandidate | None = None

    @property
    def date(self) -> Date:
        return self.day

    @property
    def settlement_date(self) -> Date:
        return self.day

    def __eq__(self, other: object) -> bool:
        if isinstance(other, date):
            return self.day == other
        if isinstance(other, str):
            return self.day.isoformat() == other
        return super().__eq__(other)

    def __ne__(self, other: object) -> bool:
        return not self == other

    __hash__ = None  # type: ignore[assignment]

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def keys(self) -> Any:
        return self.to_dict().keys()

    def items(self) -> Any:
        return self.to_dict().items()

    def values(self) -> Any:
        return self.to_dict().values()

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()

    def to_dict(self) -> dict[str, Any]:
        return {
            "date": self.day.isoformat(),
            "boundary": self.boundary.isoformat(),
            "local_time": self.local_time.isoformat(),
            "utc_time": self.utc_time.isoformat(),
            "candidate": self.candidate.to_dict() if self.candidate else None,
        }


@dataclass(frozen=True)
class SettlementDateBoundary:
    """The time of day at which a new settlement day starts."""

    hour: int = 0
    minute: int = 0
    second: int = 0
    microsecond: int = 0

    def __post_init__(self) -> None:
        if not 0 <= self.hour <= 23:
            raise ValueError(f"Invalid boundary hour: {self.hour!r}")
        if not 0 <= self.minute <= 59:
            raise ValueError(f"Invalid boundary minute: {self.minute!r}")
        if not 0 <= self.second <= 59:
            raise ValueError(f"Invalid boundary second: {self.second!r}")
        if not 0 <= self.microsecond <= 999999:
            raise ValueError(f"Invalid boundary microsecond: {self.microsecond!r}")

    @property
    def time(self) -> tuple[int, int, int, int]:
        return (self.hour, self.minute, self.second, self.microsecond)

    @classmethod
    def parse(cls, value: Any) -> SettlementDateBoundary:
        """Parse ``"HH:MM[:SS[.ffffff]]"``, ``time``, tuple, or mapping."""
        if isinstance(value, cls):
            return value
        if value is None:
            return cls()
        if isinstance(value, time):
            return cls(value.hour, value.minute, value.second, value.microsecond)
        if isinstance(value, Mapping):
            return cls(
                int(value.get("hour", 0)),
                int(value.get("minute", 0)),
                int(value.get("second", 0)),
                int(value.get("microsecond", 0)),
            )
        if type_util.is_list_or_tuple(value):
            parts = list(value)
            if len(parts) not in (2, 3, 4):
                raise ValueError(f"Invalid boundary tuple: {value!r}")
            return cls(*(int(part) for part in parts))
        if isinstance(value, str):
            text = value.strip()
            try:
                parsed = time.fromisoformat(text)
            except ValueError:
                try:
                    hours, minutes = text.split(":", 1)
                    parsed = time(int(hours), int(minutes))
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"Invalid boundary time: {value!r}") from exc
            return cls(parsed.hour, parsed.minute, parsed.second, parsed.microsecond)
        raise TypeError(f"Unsupported boundary value: {value!r}")

    def at(self, day: date, tz: tzinfo | None = None, *, fold: int = 0) -> datetime:
        return datetime(
            day.year,
            day.month,
            day.day,
            self.hour,
            self.minute,
            self.second,
            self.microsecond,
            tzinfo=tz,
            fold=fold,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "hour": self.hour,
            "minute": self.minute,
            "second": self.second,
            "microsecond": self.microsecond,
            "iso": self.at(date(2000, 1, 1)).time().isoformat(),
        }


class AmbiguousSettlementTime(ValueError):
    """Raised when a wall-clock value has more than one possible instant."""

    def __init__(self, value: Any, candidates: Sequence[TimeCandidate]) -> None:
        self.value = value
        self.candidates = tuple(candidates)
        super().__init__(
            f"Settlement time {value!r} has {len(self.candidates)} possible "
            "offsets; provide an explicit offset or inspect the candidates."
        )


class SettlementCalendar:
    """A site calendar: IANA time zone plus settlement-day boundary.

    ``boundary_fold`` selects which occurrence of an ambiguous boundary time
    starts the new settlement day.  With ``boundary_fold=1`` the later
    occurrence is used (the post-fall-back occurrence on a DST night), which
    is what separates the two occurrences of the repeated wall-clock hour:
    the first occurrence still belongs to the previous settlement day.
    """

    __slots__ = ("time_zone", "boundary", "boundary_fold", "name")

    def __init__(
        self,
        time_zone: str | ZoneInfo,
        boundary: Any = None,
        *,
        boundary_fold: int = 1,
        name: str | None = None,
    ) -> None:
        if isinstance(time_zone, ZoneInfo):
            self.time_zone = time_zone.key
        else:
            self.time_zone = str(time_zone)
        self.boundary = SettlementDateBoundary.parse(boundary)
        if boundary_fold not in (0, 1):
            raise ValueError(f"Invalid boundary_fold: {boundary_fold!r}")
        self.boundary_fold = boundary_fold
        self.name = name

    @property
    def zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self.time_zone)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"Unknown time zone: {self.time_zone!r}") from exc

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> SettlementCalendar:
        return cls.parse(value)

    @classmethod
    def for_site(
        cls,
        time_zone: str,
        boundary: Any = None,
        *,
        boundary_fold: int = 1,
    ) -> SettlementCalendar:
        return cls(time_zone, boundary, boundary_fold=boundary_fold)

    @classmethod
    def parse(cls, value: Any) -> SettlementCalendar:
        """Accept a calendar, IANA name, or a mapping with calendar keys."""
        if isinstance(value, cls):
            return value
        if isinstance(value, ZoneInfo):
            return cls(value)
        if isinstance(value, str):
            return cls(value)
        if isinstance(value, Mapping):
            time_zone = (
                value.get("time_zone")
                or value.get("timezone")
                or value.get("tz")
                or value.get("zone")
            )
            if not time_zone:
                raise ValueError("Calendar mapping is missing 'time_zone'")
            boundary = (
                value.get("boundary")
                if "boundary" in value
                else value.get("day_boundary", value.get("settlement_boundary"))
            )
            fold = value.get(
                "boundary_fold",
                value.get("fold", value.get("offset_fold", 1)),
            )
            return cls(
                time_zone,
                boundary,
                boundary_fold=int(fold),
                name=value.get("name"),
            )
        raise TypeError(f"Unsupported calendar value: {value!r}")

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, SettlementCalendar):
            return NotImplemented
        return (
            self.time_zone == other.time_zone
            and self.boundary == other.boundary
            and self.boundary_fold == other.boundary_fold
            and self.name == other.name
        )

    def __hash__(self) -> int:
        return hash(
            (
                self.time_zone,
                self.boundary.time,
                self.boundary_fold,
                self.name,
            )
        )

    def __repr__(self) -> str:
        return (
            f"SettlementCalendar({self.time_zone!r}, "
            f"boundary={self.boundary.at(date(2000, 1, 1)).time().isoformat()!r}, "
            f"boundary_fold={self.boundary_fold!r})"
        )

    @property
    def zone_name(self) -> str:
        return self.time_zone

    def parse_local(self, value: Any) -> datetime | None:
        """Read a wall-clock value without using the machine time zone."""
        if isinstance(value, datetime):
            parsed: datetime | None = value
        elif isinstance(value, bool):
            parsed = None
        elif isinstance(value, (int, float)):
            # Match benedict.get_datetime's timestamp support, but keep the
            # resulting instant timezone-independent.
            try:
                parsed = datetime.fromtimestamp(float(value), tz=timezone.utc)
            except (OverflowError, OSError, ValueError):
                parsed = None
        elif isinstance(value, str):
            parsed = _parse_local_string(value)
        else:
            parsed = _parse_local_value(value)
        if parsed is None:
            return None
        if parsed.tzinfo is not None:
            return parsed.astimezone(self.zone).replace(tzinfo=None)
        return parsed

    def candidates(
        self,
        value: str | datetime,
        *,
        offset: int | timedelta | None | object = _UNSET,
    ) -> list[TimeCandidate]:
        """Return every possible interpretation for a wall-clock value.

        When ``offset`` is omitted, the site calendar decides.  When it is an
        integer number of minutes, that offset is authoritative.  When it is
        ``None`` (explicit JSON null), no interpretation can be confirmed and
        an empty list is returned.
        """
        local = self.parse_local(value)
        if local is None:
            return []
        if offset is None:
            return []
        if offset is not _UNSET:
            minutes = normalize_offset_minutes(offset)
            if minutes is _INVALID:
                return []
            return self._candidates_for_offset(local, cast("int", minutes))
        return self._all_candidates(local)

    def _all_candidates(self, local: datetime) -> list[TimeCandidate]:
        candidates: list[TimeCandidate] = []
        for fold in (0, 1):
            candidate = self._candidate_for_fold(local, fold)
            if candidate is None:
                continue
            if any(previous.utc_time == candidate.utc_time for previous in candidates):
                continue
            candidates.append(candidate)
        return candidates

    def _candidate_for_fold(self, local: datetime, fold: int) -> TimeCandidate | None:
        zone = self.zone
        aware = local.replace(tzinfo=zone, fold=fold)
        utc_time = aware.astimezone(timezone.utc)
        # Reject imaginary wall-clock times in the spring-forward gap.
        if utc_time.astimezone(zone).replace(tzinfo=None) != local:
            return None
        return TimeCandidate(
            local_time=aware,
            offset=aware.utcoffset(),
            utc_time=utc_time,
            fold=fold,
            settlement_date=self.settlement_date_for_instant(utc_time),
        )

    def _candidates_for_offset(
        self, local: datetime, minutes: int
    ) -> list[TimeCandidate]:
        possible = self._all_candidates(local)
        matching = [
            candidate for candidate in possible if candidate.offset_minutes == minutes
        ]
        return matching[:1]

    def boundary_instant(self, day: date) -> datetime:
        """Return the absolute instant at which ``day`` starts."""
        local = self.boundary.at(day, self.zone, fold=self.boundary_fold)
        return local.astimezone(timezone.utc)

    def settlement_date_for_instant(self, instant: datetime) -> date:
        """Assign an absolute instant to a settlement day using the calendar."""
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        utc_instant = instant.astimezone(timezone.utc)
        local_day = utc_instant.astimezone(self.zone).date()
        current = self.boundary_instant(local_day)
        if utc_instant >= current:
            return local_day
        return local_day - timedelta(days=1)

    def settlement_day(self, candidate: TimeCandidate) -> SettlementDay:
        if candidate.utc_time is None:
            raise ValueError("Candidate has no UTC time")
        settlement_date = self.settlement_date_for_instant(candidate.utc_time)
        boundary = self.boundary_instant(settlement_date)
        return SettlementDay(
            day=settlement_date,
            boundary=boundary.astimezone(self.zone),
            local_time=candidate.local_time,
            utc_time=candidate.utc_time,
            candidate=candidate,
        )

    def settlement_date(
        self,
        value: str | datetime,
        *,
        offset: int | timedelta | None | object = _UNSET,
        strict: bool = False,
    ) -> date | None:
        """Return one settlement date, or ``None`` when it is not determinable.

        With ``strict=True`` an ambiguous wall time raises
        ``AmbiguousSettlementTime`` instead of silently pretending the date is
        unknown.  Invalid values and explicit-null offsets return ``None``.
        """
        candidates = self.candidates(value, offset=offset)
        if len(candidates) == 1:
            return candidates[0].settlement_date
        if len(candidates) > 1 and strict:
            raise AmbiguousSettlementTime(value, candidates)
        return None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "time_zone": self.time_zone,
            "boundary": self.boundary.to_dict(),
            "boundary_fold": self.boundary_fold,
        }


SiteCalendar = SettlementCalendar
SettlementDayBoundary = SettlementDateBoundary


@dataclass(frozen=True)
class SettlementResult:
    """Preflight result for a single settlement record."""

    record_id: Any
    status: SettlementStatus
    version: SettlementField
    closing_time: SettlementField
    offset: SettlementField
    candidates: Sequence[TimeCandidate] = ()
    settlement_day: SettlementDay | None = None
    issues: tuple[SettlementIssue, ...] = ()
    record: Mapping[str, Any] = field(default_factory=dict, compare=False, repr=False)
    calendar: SettlementCalendar | None = field(default=None, compare=False, repr=False)

    @property
    def resolved(self) -> bool:
        return self.status is SettlementStatus.RESOLVED

    @property
    def ambiguous(self) -> bool:
        return self.status is SettlementStatus.AMBIGUOUS

    @property
    def unconfirmed(self) -> bool:
        return self.status is SettlementStatus.UNCONFIRMED

    @property
    def invalid(self) -> bool:
        return self.status is SettlementStatus.INVALID

    @property
    def can_store(self) -> bool:
        return self.status is SettlementStatus.RESOLVED

    @property
    def needs_source_review(self) -> bool:
        return self.status in (
            SettlementStatus.AMBIGUOUS,
            SettlementStatus.UNCONFIRMED,
        )

    @property
    def settlement_date(self) -> date | None:
        return self.settlement_day.date if self.settlement_day else None

    @property
    def utc_time(self) -> datetime | None:
        if not self.resolved or not self.candidates:
            return None
        return self.candidates[0].utc_time

    @property
    def offset_minutes(self) -> int | None:
        if not self.resolved or not self.candidates:
            return None
        return self.candidates[0].offset_minutes

    @property
    def closing_time_path(self) -> SourcePath:
        return self.closing_time.path

    @property
    def offset_path(self) -> SourcePath:
        return self.offset.path

    @property
    def candidate_settlement_dates(self) -> tuple[date, ...]:
        dates: list[date] = []
        for candidate in self.candidates:
            if candidate.settlement_date is None:
                continue
            if candidate.settlement_date not in dates:
                dates.append(candidate.settlement_date)
        return tuple(dates)

    @property
    def missing_evidence(self) -> tuple[str, ...]:
        """Human-readable evidence that must be obtained from the source."""
        if self.resolved:
            return ()
        return tuple(issue.message for issue in self.issues)

    def issue(self, code: SettlementIssueCode) -> SettlementIssue | None:
        return next((issue for issue in self.issues if issue.code is code), None)

    def has_issue(self, code: SettlementIssueCode) -> bool:
        return self.issue(code) is not None

    def fields(self) -> dict[str, SettlementField]:
        return {
            "version": self.version,
            "closing_time": self.closing_time,
            "offset": self.offset,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "status": self.status.value,
            "version": self.version.to_dict(),
            "closing_time": self.closing_time.to_dict(),
            "offset": self.offset.to_dict(),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "candidate_settlement_dates": [
                value.isoformat() for value in self.candidate_settlement_dates
            ],
            "settlement_day": (
                self.settlement_day.to_dict() if self.settlement_day else None
            ),
            "settlement_date": (
                self.settlement_date.isoformat() if self.settlement_date else None
            ),
            "issues": [issue.to_dict() for issue in self.issues],
            "missing_evidence": list(self.missing_evidence),
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def keys(self) -> Any:
        return self.to_dict().keys()

    def items(self) -> Any:
        return self.to_dict().items()

    def values(self) -> Any:
        return self.to_dict().values()

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()

    def __iter__(self) -> Any:
        return iter(self.to_dict())


@dataclass(frozen=True)
class SettlementRecord:
    """Original input retained for a later, targeted re-check."""

    record_id: Any
    record: Mapping[str, Any]
    calendar: SettlementCalendar | str | Mapping[str, Any]
    result: SettlementResult | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.record, Mapping):
            raise TypeError("record must be a mapping")
        if not isinstance(self.calendar, SettlementCalendar):
            object.__setattr__(
                self, "calendar", SettlementCalendar.parse(self.calendar)
            )
        if self.result is None:
            object.__setattr__(
                self,
                "result",
                preflight_settlement(
                    self.record, self.calendar, record_id=self.record_id
                ),
            )

    @classmethod
    def from_record(
        cls,
        record: Mapping[str, Any],
        calendar: SettlementCalendar | str | Mapping[str, Any],
        *,
        record_id: Any = _UNSET,
        detail_index: int = 0,
    ) -> SettlementRecord:
        return SettlementPreflight(calendar).record(
            record, record_id=record_id, detail_index=detail_index
        )

    @classmethod
    def from_mapping(
        cls,
        record: Mapping[str, Any],
        calendar: SettlementCalendar | str | Mapping[str, Any],
        *,
        record_id: Any = _UNSET,
        detail_index: int = 0,
    ) -> SettlementRecord:
        return cls.from_record(
            record, calendar, record_id=record_id, detail_index=detail_index
        )

    @property
    def version(self) -> int | None:
        return cast("int | None", self.result.version.value if self.result else None)

    @property
    def closing_time(self) -> SettlementField:
        if self.result is None:
            return SettlementField((), None, present=False)
        return self.result.closing_time

    @property
    def offset(self) -> SettlementField:
        if self.result is None:
            return SettlementField((), None, present=False)
        return self.result.offset

    @property
    def offset_path(self) -> SourcePath:
        if self.result is not None:
            return self.result.offset.path
        return self.offset.path

    @property
    def closing_time_path(self) -> SourcePath:
        if self.result is not None:
            return self.result.closing_time.path
        return self.closing_time.path

    @property
    def status(self) -> SettlementStatus | None:
        return self.result.status if self.result else None

    @property
    def settlement_date(self) -> date | None:
        return self.result.settlement_date if self.result else None

    @property
    def settlement_day(self) -> SettlementDay | None:
        return self.result.settlement_day if self.result else None

    @property
    def candidates(self) -> CandidateList:
        if self.result is None:
            return CandidateList()
        return CandidateList(self.result.candidates)

    @property
    def source(self) -> Mapping[str, Any]:
        return self.record

    @property
    def resolved(self) -> bool:
        return self.result.resolved if self.result else False

    @property
    def can_store(self) -> bool:
        return self.result.can_store if self.result else False

    @property
    def needs_source_review(self) -> bool:
        return self.result.needs_source_review if self.result else False

    @property
    def missing_evidence(self) -> tuple[str, ...]:
        return self.result.missing_evidence if self.result else ()

    @property
    def issues(self) -> tuple[SettlementIssue, ...]:
        return self.result.issues if self.result else ()

    def changed(
        self,
        *,
        offset: int | str | None | object = _UNSET,
        closing_time: str | datetime | object = _UNSET,
        detail_index: int = 0,
    ) -> SettlementRecord:
        """Return a copy with only the supplied source fields changed."""
        payload = clone_mapping(self.record)
        detail = ensure_detail(payload, detail_index)
        changed = False
        if offset is not _UNSET:
            if detail is not None:
                detail["offset"] = offset
            else:
                # Never overwrite an outer legacy value with a new-version
                # detail correction.
                payload.setdefault("details", []).append({"offset": offset})
            changed = True
        if closing_time is not _UNSET:
            if detail is not None:
                detail["closing_time"] = closing_time
            else:
                payload["closing_time"] = closing_time
            changed = True
        if not changed:
            return self
        return SettlementRecord(
            record_id=self.record_id,
            record=payload,
            calendar=self.calendar,
            result=self.result,
        )

    def recheck(
        self,
        *,
        offset: int | str | None | object = _UNSET,
        closing_time: str | datetime | object = _UNSET,
        detail_index: int = 0,
    ) -> SettlementResult:
        return settlement_preflight(
            self.changed(
                offset=offset,
                closing_time=closing_time,
                detail_index=detail_index,
            ).record,
            self.calendar,
            record_id=self.record_id,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "record_id": self.record_id,
            "version": self.version,
            "status": self.status.value if self.status else None,
            "closing_time": self.closing_time.to_dict(),
            "offset": self.offset.to_dict(),
            "candidates": [candidate.to_dict() for candidate in self.candidates],
            "settlement_date": (
                self.settlement_date.isoformat() if self.settlement_date else None
            ),
            "settlement_day": (
                self.settlement_day.to_dict() if self.settlement_day else None
            ),
            "issues": [issue.to_dict() for issue in self.issues],
            "record": self.record,
            "calendar": cast("SettlementCalendar", self.calendar).to_dict(),
        }

    def __getitem__(self, key: str) -> Any:
        return self.to_dict()[key]

    def get(self, key: str, default: Any = None) -> Any:
        return self.to_dict().get(key, default)

    def keys(self) -> Any:
        return self.to_dict().keys()

    def items(self) -> Any:
        return self.to_dict().items()

    def values(self) -> Any:
        return self.to_dict().values()

    def __contains__(self, key: object) -> bool:
        return key in self.to_dict()

    def __iter__(self) -> Any:
        return iter(self.to_dict())


class SettlementPreflight:
    """Reusable preflight engine bound to one site calendar."""

    def __init__(self, calendar: SettlementCalendar | str | Mapping[str, Any]) -> None:
        self.calendar = SettlementCalendar.parse(calendar)

    def check(
        self,
        record: Mapping[str, Any] | SettlementRecord,
        *,
        record_id: Any = _UNSET,
        detail_index: int = 0,
    ) -> SettlementResult:
        if isinstance(record, SettlementRecord):
            return settlement_preflight(
                record.record,
                record.calendar,
                record_id=record.record_id if record_id is _UNSET else record_id,
                detail_index=detail_index,
            )
        return settlement_preflight(
            record,
            self.calendar,
            record_id=record_id,
            detail_index=detail_index,
        )

    def check_many(
        self,
        records: Iterable[Mapping[str, Any] | SettlementRecord],
        *,
        detail_index: int = 0,
    ) -> list[SettlementResult]:
        return [self.check(record, detail_index=detail_index) for record in records]

    def preflight(
        self,
        record: Mapping[str, Any] | SettlementRecord,
        *,
        record_id: Any = _UNSET,
        detail_index: int = 0,
    ) -> SettlementResult:
        return self.check(record, record_id=record_id, detail_index=detail_index)

    def preflight_many(
        self,
        records: Iterable[Mapping[str, Any] | SettlementRecord],
        *,
        detail_index: int = 0,
    ) -> list[SettlementResult]:
        return self.check_many(records, detail_index=detail_index)

    def record(
        self,
        record: Mapping[str, Any],
        *,
        record_id: Any = _UNSET,
        detail_index: int = 0,
    ) -> SettlementRecord:
        result = self.check(
            record,
            record_id=record_id,
            detail_index=detail_index,
        )
        return SettlementRecord(
            record_id=result.record_id,
            record=clone_mapping(record),
            calendar=self.calendar,
            result=result,
        )

    def recheck(
        self,
        previous: SettlementRecord | Mapping[str, Any],
        *,
        offset: int | str | None | object = _UNSET,
        closing_time: str | datetime | object = _UNSET,
        detail_index: int = 0,
    ) -> SettlementResult:
        source = (
            previous
            if isinstance(previous, SettlementRecord)
            else self.record(previous, detail_index=detail_index)
        )
        if source.calendar != self.calendar:
            source = SettlementRecord(
                record_id=source.record_id,
                record=source.record,
                calendar=self.calendar,
                result=source.result,
            )
        return source.recheck(
            offset=offset,
            closing_time=closing_time,
            detail_index=detail_index,
        )

    def settlement_date(
        self,
        value: str | datetime,
        *,
        offset: int | timedelta | None | object = _UNSET,
    ) -> date | None:
        return self.calendar.settlement_date(value, offset=offset)


def format_path(path: Sequence[str | int]) -> str:
    """Format a source path as ``details[0].offset``."""
    result = ""
    for part in path:
        if isinstance(part, int):
            result += f"[{part}]"
        else:
            if result:
                result += "."
            result += str(part)
    return result


def _parse_local_value(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        return _parse_local_string(value)
    # Non-string values (eg. date) keep benedict's existing parse behavior.
    try:
        parsed = parse_util.parse_datetime(value)
    except (ExtrasRequireModuleNotFoundError, TypeError, ValueError):
        return None
    if parsed is not None and parsed.tzinfo is not None:
        return parsed.astimezone(timezone.utc)
    return parsed


def _parse_local_string(value: str) -> datetime | None:
    """Parse a local wall-clock string without applying the machine timezone.

    ``benedict.get_datetime`` intentionally treats unadorned values such as
    ``"1750000000"`` as timestamps and uses ``datetime.fromtimestamp``, which
    depends on the host timezone.  A settlement record's local wall-clock
    string must not depend on the machine, so ISO-8601 strings are read first
    and only explicit numeric epoch values fall back to the existing parser.
    """
    text = value.strip().replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        pass
    try:
        float(text)
    except ValueError:
        pass
    else:
        parsed = parse_util.parse_datetime(value)
        if parsed is None:
            return None
        # An epoch is an absolute instant; return its UTC wall clock so the
        # site calendar, not the machine timezone, can interpret it.
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    try:
        return parse_util.parse_datetime(value)
    except ExtrasRequireModuleNotFoundError:
        return None


def clone_mapping(value: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, item in value.items():
        if isinstance(item, Mapping):
            result[key] = clone_mapping(item)
        elif isinstance(item, list):
            result[key] = [
                clone_mapping(child) if isinstance(child, Mapping) else child
                for child in item
            ]
        else:
            result[key] = item
    return result


def normalize_offset_minutes(value: Any) -> int | object:
    """Normalize an offset expressed in minutes to an integer.

    This mirrors ``benedict.get_int``: bools and integer-like strings are
    accepted, while malformed values remain invalid rather than becoming zero.
    """
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, timedelta):
        seconds = value.total_seconds()
        if seconds % 60:
            return _INVALID
        return int(seconds // 60)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else _INVALID
    if isinstance(value, str):
        parsed = parse_util.parse_int(value)
        if parsed is None:
            return _INVALID
        return parsed
    return _INVALID


def _detail_container(
    record: Mapping[str, Any], detail_index: int
) -> tuple[Any, tuple[str | int, ...], bool]:
    """Return the detail object, its path, and whether it is a list item."""
    details = record.get("details")
    if isinstance(details, Mapping):
        return details, ("details",), False
    if type_util.is_list_or_tuple(details):
        if 0 <= detail_index < len(details) and isinstance(
            details[detail_index], Mapping
        ):
            return details[detail_index], ("details", detail_index), True
        return None, ("details", detail_index), True
    return None, ("details",), False


def ensure_detail(
    record: dict[str, Any], detail_index: int = 0
) -> dict[str, Any] | None:
    """Get or create the detail mapping used for new-version fields."""
    details = record.get("details")
    if isinstance(details, dict):
        return details
    if isinstance(details, list):
        items: list[Any] = details
        while len(items) <= detail_index:
            items.append({})
        if not isinstance(items[detail_index], dict):
            items[detail_index] = {}
        return cast("dict[str, Any]", items[detail_index])
    if details is None:
        items = []
        record["details"] = items
        while len(items) <= detail_index:
            items.append({})
        return cast("dict[str, Any]", items[detail_index])
    return None


def _normalize_version(value: Any) -> int | None:
    """Accept version numbers and simple version labels such as ``v2``."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else None
    if isinstance(value, str):
        text = value.strip().lower()
        if text.startswith("v") and len(text) > 1:
            text = text[1:]
        parsed = parse_util.parse_int(text)
        return parsed
    return None


def _read_version(
    record: Mapping[str, Any],
) -> tuple[int | None, SettlementField, list[SettlementIssue]]:
    field_name = None
    for candidate in ("version", "api_version", "record_version", "schema_version"):
        if candidate in record:
            field_name = candidate
            break
    if field_name is None:
        field = SettlementField(("version",), None, present=False, valid=False)
        return (
            None,
            field,
            [
                SettlementIssue(
                    SettlementIssueCode.MISSING_FIELD,
                    "version is missing; cannot choose the record layout",
                    ("version",),
                    "1 or 2",
                )
            ],
        )
    raw = record[field_name]
    if raw is None:
        return (
            None,
            SettlementField((field_name,), None, valid=False),
            [
                SettlementIssue(
                    SettlementIssueCode.INVALID_FIELD,
                    f"{field_name} is null; cannot choose the record layout",
                    (field_name,),
                    "1 or 2",
                )
            ],
        )
    version = _normalize_version(raw)
    if version is None or version not in (1, 2):
        return (
            None,
            SettlementField((field_name,), raw, valid=False),
            [
                SettlementIssue(
                    SettlementIssueCode.INVALID_VERSION,
                    f"{field_name} is {raw!r}; expected version 1 or 2",
                    (field_name,),
                    "1 or 2",
                )
            ],
        )
    return int(version), SettlementField((field_name,), int(version)), []


def _read_detail_field(
    record: Mapping[str, Any],
    name: str,
    *,
    detail_index: int,
    required: bool,
) -> tuple[SettlementField, list[SettlementIssue]]:
    detail, detail_path, _ = _detail_container(record, detail_index)
    path = (*detail_path, name)
    if detail is None:
        issue = SettlementIssue(
            SettlementIssueCode.MISSING_FIELD,
            f"{format_path(path)} is missing",
            path,
        )
        return SettlementField(path, None, present=False, valid=False), (
            [issue] if required else []
        )
    if name not in detail:
        issue = SettlementIssue(
            SettlementIssueCode.MISSING_FIELD,
            f"{format_path(path)} is missing",
            path,
        )
        return SettlementField(path, None, present=False, valid=False), (
            [issue] if required else []
        )
    value = detail[name]
    if value is None:
        return (
            SettlementField(path, None),
            [
                SettlementIssue(
                    SettlementIssueCode.NULL_FIELD,
                    f"{format_path(path)} is explicitly null",
                    path,
                )
            ],
        )
    return SettlementField(path, value), []


def _read_closing_time(
    record: Mapping[str, Any], version: int, detail_index: int
) -> tuple[SettlementField, list[SettlementIssue]]:
    if version == 2:
        field, issues = _read_detail_field(
            record,
            "closing_time",
            detail_index=detail_index,
            required=True,
        )
        if field.present and field.valid and field.value is not None:
            parsed = _parse_local_value(field.value)
            if parsed is None:
                path = field.path
                return (
                    SettlementField(path, field.value, valid=False),
                    [
                        SettlementIssue(
                            SettlementIssueCode.INVALID_FIELD,
                            f"{format_path(path)} is not a datetime: {field.value!r}",
                            path,
                            "ISO-8601 local date-time",
                        )
                    ],
                )
            return SettlementField(field.path, parsed), issues
        if not field.present and "closing_time" in record:
            issues = [
                *issues,
                SettlementIssue(
                    SettlementIssueCode.LEGACY_FIELD_ONLY,
                    "only the legacy outer closing_time is present; a "
                    "version 2 record must provide details[0].closing_time",
                    ("closing_time",),
                ),
            ]
        return field, issues

    legacy_path: tuple[str, ...] = ("closing_time",)
    if "closing_time" not in record:
        return (
            SettlementField(legacy_path, None, present=False, valid=False),
            [
                SettlementIssue(
                    SettlementIssueCode.MISSING_FIELD,
                    "closing_time is missing",
                    legacy_path,
                )
            ],
        )
    raw = record["closing_time"]
    if raw is None:
        return (
            SettlementField(legacy_path, None),
            [
                SettlementIssue(
                    SettlementIssueCode.NULL_FIELD,
                    "closing_time is explicitly null",
                    legacy_path,
                )
            ],
        )
    parsed = _parse_local_string(raw)
    if parsed is None:
        return (
            SettlementField(legacy_path, raw, valid=False),
            [
                SettlementIssue(
                    SettlementIssueCode.INVALID_FIELD,
                    f"closing_time is not a datetime: {raw!r}",
                    legacy_path,
                    "ISO-8601 local date-time",
                )
            ],
        )
    return SettlementField(legacy_path, parsed), []


def _read_offset(
    record: Mapping[str, Any], version: int, detail_index: int
) -> tuple[SettlementField, list[SettlementIssue]]:
    detail, detail_path, _ = _detail_container(record, detail_index)
    path = (*detail_path, "offset")
    if version == 1:
        return SettlementField(path, None, present=False), []
    if detail is None or "offset" not in detail:
        return SettlementField(path, None, present=False), []
    raw = detail["offset"]
    if raw is None:
        return (
            SettlementField(path, None),
            [
                SettlementIssue(
                    SettlementIssueCode.NULL_FIELD,
                    f"{format_path(path)} is explicitly null; the record "
                    "cannot be confirmed from the source",
                    path,
                )
            ],
        )
    minutes = normalize_offset_minutes(raw)
    if minutes is _INVALID:
        return (
            SettlementField(path, raw, valid=False),
            [
                SettlementIssue(
                    SettlementIssueCode.INVALID_FIELD,
                    f"{format_path(path)} has an invalid offset: {raw!r}",
                    path,
                    "integer minutes",
                )
            ],
        )
    return SettlementField(path, cast("int", minutes)), []


def _record_id(record: Mapping[str, Any], explicit: Any) -> Any:
    if explicit is not _UNSET:
        return explicit
    for key in ("id", "record_id", "settlement_id", "external_id"):
        if key in record:
            return record[key]
    return None


def build_settlement_record(
    record: Mapping[str, Any],
    calendar: SettlementCalendar | str | Mapping[str, Any],
    *,
    record_id: Any = _UNSET,
    detail_index: int = 0,
) -> SettlementRecord:
    """Build a ``SettlementRecord`` bound to its source and site calendar."""
    return SettlementPreflight(calendar).record(
        record,
        record_id=record_id,
        detail_index=detail_index,
    )


def _build_result(
    record: Mapping[str, Any],
    site: SettlementCalendar,
    rid: Any,
    status: SettlementStatus,
    version: SettlementField,
    closing_time: SettlementField,
    offset: SettlementField,
    issues: Sequence[SettlementIssue],
    *,
    candidates: Sequence[TimeCandidate] = (),
    settlement_day: SettlementDay | None = None,
) -> SettlementResult:
    return SettlementResult(
        record_id=rid,
        status=status,
        version=version,
        closing_time=closing_time,
        offset=offset,
        candidates=CandidateList(candidates),
        settlement_day=settlement_day,
        issues=tuple(issues),
        record=clone_mapping(record),
        calendar=site,
    )


def _read_record_fields(
    record: Mapping[str, Any], detail_index: int
) -> tuple[
    int | None,
    SettlementField,
    list[SettlementIssue],
    SettlementField,
    SettlementField,
]:
    version, version_field, version_issues = _read_version(record)
    if version is None:
        closing = SettlementField(("closing_time",), None, present=False, valid=False)
        offset = SettlementField(
            ("details", detail_index, "offset"), None, present=False
        )
        return None, version_field, [*version_issues], closing, offset
    closing_field, closing_issues = _read_closing_time(record, version, detail_index)
    offset_field, offset_issues = _read_offset(record, version, detail_index)
    issues = [*version_issues, *closing_issues, *offset_issues]
    return version, version_field, issues, closing_field, offset_field


def _resolve_offset_value(
    version: int, offset_field: SettlementField
) -> tuple[Any, SettlementStatus | None]:
    if version != 2 or not offset_field.present:
        return _UNSET, None
    if not offset_field.valid:
        return _UNSET, SettlementStatus.INVALID
    if offset_field.value is None:
        return _UNSET, SettlementStatus.UNCONFIRMED
    return offset_field.value, None


def preflight_settlement(
    record: Mapping[str, Any],
    calendar: SettlementCalendar | str | Mapping[str, Any],
    *,
    record_id: Any = _UNSET,
    detail_index: int = 0,
) -> SettlementResult:
    """Preflight one settlement record against its site calendar.

    The record is never mutated.  All decisions point back to the original
    source path, so that a later correction can be replayed on its own.
    """
    if not isinstance(record, Mapping):
        raise TypeError("record must be a mapping")
    site = SettlementCalendar.parse(calendar)
    rid = _record_id(record, record_id)
    version, version_field, issues, closing_field, offset_field = _read_record_fields(
        record, detail_index
    )
    if version is None:
        return _build_result(
            record,
            site,
            rid,
            SettlementStatus.INVALID,
            version_field,
            closing_field,
            offset_field,
            issues,
        )

    if not closing_field.can_store:
        status = (
            SettlementStatus.UNCONFIRMED
            if closing_field.present and closing_field.value is None
            else SettlementStatus.INVALID
        )
        return _build_result(
            record,
            site,
            rid,
            status,
            version_field,
            closing_field,
            offset_field,
            issues,
        )

    offset_value, offset_status = _resolve_offset_value(version, offset_field)
    if offset_status is not None:
        return _build_result(
            record,
            site,
            rid,
            offset_status,
            version_field,
            closing_field,
            offset_field,
            issues,
        )

    candidates = site.candidates(
        cast("datetime", closing_field.value),
        offset=offset_value,
    )
    if not candidates:
        path = offset_field.path if offset_value is not _UNSET else closing_field.path
        issues.append(
            SettlementIssue(
                SettlementIssueCode.INVALID_FIELD,
                f"{format_path(path)} cannot be interpreted in {site.time_zone!r}",
                path,
                "an offset valid for the site calendar",
            )
        )
        return _build_result(
            record,
            site,
            rid,
            SettlementStatus.INVALID,
            version_field,
            closing_field,
            offset_field,
            issues,
        )

    if len(candidates) == 1:
        return _build_result(
            record,
            site,
            rid,
            SettlementStatus.RESOLVED,
            version_field,
            closing_field,
            offset_field,
            issues,
            candidates=candidates,
            settlement_day=site.settlement_day(candidates[0]),
        )

    if offset_value is _UNSET:
        issues.append(
            SettlementIssue(
                SettlementIssueCode.MISSING_OFFSET,
                "details[0].offset is missing and the site calendar has "
                "more than one possible offset for this local time",
                ("details", detail_index, "offset"),
                "one of: "
                + ", ".join(
                    str(candidate.offset_minutes)
                    for candidate in candidates
                    if candidate.offset_minutes is not None
                ),
            )
        )
    issues.append(
        SettlementIssue(
            SettlementIssueCode.AMBIGUOUS_TIME,
            "closing_time maps to more than one instant in the site calendar",
            closing_field.path,
            "an explicit details[0].offset",
        )
    )
    return _build_result(
        record,
        site,
        rid,
        SettlementStatus.AMBIGUOUS,
        version_field,
        closing_field,
        offset_field,
        issues,
        candidates=candidates,
    )


settlement_preflight = preflight_settlement
preflight_settlement_record = preflight_settlement


# Short aliases for integration code.
SettlementCandidate = TimeCandidate

__all__ += [
    "SETTLEMENT_UNSET",
    "SettlementCandidate",
    "SettlementDayBoundary",
    "SourcePath",
]
