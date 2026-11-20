"""Audit archive tables against versioned, timezone-aware retention policies.

Each source (archive table) has its own business timezone and a date column
subject to a retention period. Retention policies are revised over time; every
revision is modeled as a :class:`RetentionPolicyVersion` taking effect at an
absolute instant. Audits can be re-run for any specified audit instant: the
policy version in effect at that instant is selected and its retention period
is interpreted in local calendar days of the source's business timezone.

All computations are based on explicitly passed, timezone-aware instants and
IANA timezones. The machine's local timezone never influences the results.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable
from dataclasses import dataclass
from enum import Enum
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from . import db_access
from .constraints.date import _convert_to_date, _get_format_from_column_type
from .db_access import DataReference

__all__ = [
    "RetentionPolicy",
    "RetentionPolicyVersion",
    "RetentionStatus",
    "RetentionSubject",
    "SourceRetentionResult",
    "RetentionAuditReport",
    "audit_retention",
]


def _as_utc(instant: dt.datetime) -> dt.datetime:
    """Normalize a timezone-aware instant to UTC."""
    if instant.tzinfo is None:
        raise ValueError(
            f"Instant {instant!r} is naive. Pass a timezone-aware datetime, "
            "e.g. in UTC, to unambiguously identify the audit instant."
        )
    return instant.astimezone(dt.timezone.utc)


@dataclass(frozen=True)
class RetentionPolicyVersion:
    """One revision of a retention policy.

    Parameters
    ----------
    retention_days:
        Number of local calendar days records are retained. A day always
        refers to a local calendar day of the source's business timezone.
    effective_from:
        Absolute instant from which this revision is in effect. Must be
        timezone-aware; it is normalized to UTC.
    """

    retention_days: int
    effective_from: dt.datetime

    def __post_init__(self) -> None:
        if self.retention_days < 0:
            raise ValueError(
                f"retention_days must be non-negative, got {self.retention_days}."
            )
        object.__setattr__(self, "effective_from", _as_utc(self.effective_from))


class RetentionPolicy:
    """Versioned retention policy of a single source.

    Parameters
    ----------
    timezone:
        IANA timezone name of the source's business timezone, e.g.
        ``"America/New_York"``. Record dates are interpreted as local days
        in this timezone.
    versions:
        The revisions of the retention policy. The revision applicable to an
        audit is the one with the latest ``effective_from`` not after the
        audit instant.
    """

    def __init__(
        self, timezone: str, versions: Iterable[RetentionPolicyVersion]
    ) -> None:
        self._timezone_name = timezone
        self._timezone = ZoneInfo(timezone)
        self._versions = tuple(sorted(versions, key=lambda v: v.effective_from))
        if len(self._versions) == 0:
            raise ValueError("A retention policy requires at least one version.")
        effective_instants = [v.effective_from for v in self._versions]
        if len(set(effective_instants)) != len(effective_instants):
            raise ValueError(
                "Retention policy versions must have distinct effective instants."
            )

    @property
    def timezone_name(self) -> str:
        return self._timezone_name

    @property
    def timezone(self) -> ZoneInfo:
        return self._timezone

    @property
    def versions(self) -> tuple[RetentionPolicyVersion, ...]:
        return self._versions

    def version_at(self, audit_instant: dt.datetime) -> RetentionPolicyVersion | None:
        """Return the policy version in effect at ``audit_instant``.

        Returns ``None`` if the audit instant predates the first version's
        effective instant.
        """
        instant = _as_utc(audit_instant)
        selected: RetentionPolicyVersion | None = None
        for version in self._versions:
            if version.effective_from <= instant:
                selected = version
            else:
                break
        return selected

    def local_date_at(self, audit_instant: dt.datetime) -> dt.date:
        """Return the source-local calendar date of ``audit_instant``."""
        return _as_utc(audit_instant).astimezone(self._timezone).date()

    def lower_bound_at(
        self, audit_instant: dt.datetime
    ) -> tuple[RetentionPolicyVersion, dt.date] | None:
        """Return the effective version and the local-date lower bound.

        The lower bound is the oldest local calendar date a record may have
        while still being within the retention period: the source-local date
        of the audit instant minus ``retention_days`` local calendar days.
        Returns ``None`` if no version is in effect at the audit instant.
        """
        version = self.version_at(audit_instant)
        if version is None:
            return None
        lower_bound = self.local_date_at(audit_instant) - dt.timedelta(
            days=version.retention_days
        )
        return version, lower_bound


class RetentionStatus(Enum):
    """Outcome category of a single source within a retention audit."""

    COMPLIANT = "compliant"
    VIOLATION = "violation"
    NO_COMPARABLE_DATES = "no_comparable_dates"
    NO_POLICY = "no_policy"


@dataclass(frozen=True)
class RetentionSubject:
    """A source to be audited.

    Parameters
    ----------
    name:
        Identifier of the source, used in the report.
    ref:
        Data reference to the source's retention-relevant date column.
    policy:
        The source's versioned retention policy. ``None`` indicates that no
        retention policy is configured for this source.
    """

    name: str
    ref: DataReference
    policy: RetentionPolicy | None


@dataclass(frozen=True)
class SourceRetentionResult:
    """Audit result of a single source."""

    source: str
    status: RetentionStatus
    policy_version: RetentionPolicyVersion | None
    lower_bound: dt.date | None
    min_date: dt.date | None
    message: str


@dataclass(frozen=True)
class RetentionAuditReport:
    """Result of auditing a batch of sources at one audit instant.

    Sources without comparable dates and sources without an applicable policy
    are reported separately and never enter the comparison denominator.
    """

    audit_instant: dt.datetime
    results: tuple[SourceRetentionResult, ...]

    @property
    def comparable(self) -> tuple[SourceRetentionResult, ...]:
        """Results that actually compared dates against a lower bound."""
        return tuple(
            result
            for result in self.results
            if result.status in (RetentionStatus.COMPLIANT, RetentionStatus.VIOLATION)
        )

    @property
    def without_dates(self) -> tuple[SourceRetentionResult, ...]:
        """Results of configured sources without any comparable date."""
        return tuple(
            result
            for result in self.results
            if result.status is RetentionStatus.NO_COMPARABLE_DATES
        )

    @property
    def without_policy(self) -> tuple[SourceRetentionResult, ...]:
        """Results of sources without an applicable retention policy."""
        return tuple(
            result
            for result in self.results
            if result.status is RetentionStatus.NO_POLICY
        )

    @property
    def violations(self) -> tuple[SourceRetentionResult, ...]:
        return tuple(
            result
            for result in self.results
            if result.status is RetentionStatus.VIOLATION
        )

    @property
    def violation_fraction(self) -> float | None:
        """Share of violations among comparable sources.

        ``None`` if no source was comparable.
        """
        comparable = self.comparable
        if not comparable:
            return None
        return len(self.violations) / len(comparable)

    @property
    def is_compliant(self) -> bool:
        """Whether no source violated its retention policy."""
        return len(self.violations) == 0


def audit_retention(
    engine: sa.engine.Engine,
    subjects: Iterable[RetentionSubject],
    audit_instant: dt.datetime,
    column_type: str = "date",
) -> RetentionAuditReport:
    """Audit archive tables against their retention policies.

    For every subject, the policy version in effect at ``audit_instant`` is
    selected and the source's minimal record date is compared against the
    resulting local-date lower bound. Sources without comparable dates and
    sources without an applicable policy are reported separately.

    Parameters
    ----------
    engine:
        sqlalchemy engine used to retrieve the minimal record dates.
    subjects:
        The sources to audit.
    audit_instant:
        Timezone-aware instant at which the audit is (re-)done.
    column_type:
        Column type of the date columns, see
        ``WithinRequirement.add_column_type_constraint``.
    """
    instant = _as_utc(audit_instant)
    date_format = _get_format_from_column_type(column_type)
    results = []
    for subject in subjects:
        version: RetentionPolicyVersion | None = None
        lower_bound: dt.date | None = None
        if subject.policy is None:
            results.append(
                SourceRetentionResult(
                    source=subject.name,
                    status=RetentionStatus.NO_POLICY,
                    policy_version=None,
                    lower_bound=None,
                    min_date=None,
                    message=(
                        f"Source {subject.name!r} has no retention policy "
                        "configured; skipped comparison."
                    ),
                )
            )
            continue
        bound = subject.policy.lower_bound_at(instant)
        if bound is None:
            results.append(
                SourceRetentionResult(
                    source=subject.name,
                    status=RetentionStatus.NO_POLICY,
                    policy_version=None,
                    lower_bound=None,
                    min_date=None,
                    message=(
                        f"Source {subject.name!r} has no retention policy "
                        f"version in effect at audit instant {instant.isoformat()}; "
                        "skipped comparison."
                    ),
                )
            )
            continue
        version, lower_bound = bound
        raw_min, _ = db_access.get_min(engine, subject.ref)
        if raw_min is None:
            results.append(
                SourceRetentionResult(
                    source=subject.name,
                    status=RetentionStatus.NO_COMPARABLE_DATES,
                    policy_version=version,
                    lower_bound=lower_bound,
                    min_date=None,
                    message=(
                        f"Source {subject.name!r} has no comparable dates; "
                        "skipped comparison."
                    ),
                )
            )
            continue
        min_date = _convert_to_date(raw_min, date_format)
        compliant = min_date >= lower_bound
        status = RetentionStatus.COMPLIANT if compliant else RetentionStatus.VIOLATION
        message = (
            f"Source {subject.name!r} has min date {min_date.isoformat()} "
            f"{'>=' if compliant else '<'} lower bound {lower_bound.isoformat()} "
            f"(policy version effective from {version.effective_from.isoformat()}, "
            f"retention of {version.retention_days} local days in "
            f"{subject.policy.timezone_name})."
        )
        results.append(
            SourceRetentionResult(
                source=subject.name,
                status=status,
                policy_version=version,
                lower_bound=lower_bound,
                min_date=min_date,
                message=message,
            )
        )
    return RetentionAuditReport(audit_instant=instant, results=tuple(results))
