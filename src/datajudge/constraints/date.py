from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from zoneinfo import ZoneInfo

import sqlalchemy as sa

from .. import db_access
from ..db_access import DataReference
from .base import Constraint, _OptionalSelections
from .interval import NoGapConstraint, NoOverlapConstraint, _Selects

_INPUT_DATE_FORMAT = "'%Y-%m-%d'"

Date = str | dt.date | dt.datetime


def _get_format_from_column_type(column_type: str) -> str:
    if column_type.lower() == "date":
        return "%Y-%m-%d"
    if column_type.lower() in ["datetime", "datetime2", "smalldatetime"]:
        return "%Y-%m-%d %H:%M:%S"
    raise ValueError(f"Illegal date column type: {column_type}")


def _convert_to_date(db_result: Date, format: str) -> dt.date:
    if isinstance(db_result, dt.datetime):
        return db_result.date()
    if isinstance(db_result, dt.date):
        return db_result
    if isinstance(db_result, str):
        # Get rid of nanoseconds as they cannot be parsed by strptime.
        return dt.datetime.strptime(db_result.split(".")[0], format).date()
    raise TypeError(f"Value hast type {type(db_result)} cannot be converted to date.")


class DateMin(Constraint):
    def __init__(
        self,
        ref: DataReference,
        use_lower_bound_reference: bool,
        column_type: str,
        name: str | None = None,
        cache_size=None,
        *,
        ref2: DataReference | None = None,
        min_value: str | None = None,
    ):
        self._format = _get_format_from_column_type(column_type)
        self._use_lower_bound_reference = use_lower_bound_reference
        min_date: dt.date | None = None
        if min_value is not None:
            min_date = dt.datetime.strptime(min_value, _INPUT_DATE_FORMAT).date()
        super().__init__(
            ref,
            ref2=ref2,
            ref_value=min_date,
            name=name,
            cache_size=cache_size,
        )

    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[dt.date, _OptionalSelections]:
        result, selections = db_access.get_min(engine, ref)
        return _convert_to_date(result, self._format), selections

    def _compare(
        self, value_factual: dt.date, value_target: dt.date
    ) -> tuple[bool, str | None]:
        if value_target is None:
            return True, None
        if value_factual is None:
            return value_target == 0, "Empty set."
        if self._use_lower_bound_reference:
            assertion_text = (
                f"{self._ref} has min {value_factual} < "
                f"{self._target_prefix} {value_target}. "
                f"{self._condition_string}"
            )
            result = value_factual >= value_target
        else:
            assertion_text = (
                f"{self._ref} has min {value_factual} > "
                f"{self._target_prefix} {value_target}. "
                f"{self._condition_string}"
            )
            result = value_factual <= value_target
        return result, assertion_text


@dataclass(frozen=True)
class DateRetentionRule:
    """Retention rule of a single group, e.g. a data source.

    ``retention_days`` states how many days of data the group is expected to
    keep. ``timezone`` is the IANA timezone of the group's business region;
    it determines which calendar day counts as "today" when the effective
    minimum date is computed.
    """

    retention_days: int
    timezone: str = "UTC"

    def __post_init__(self):
        if self.retention_days < 0:
            raise ValueError(
                f"retention_days must be non-negative, got {self.retention_days}."
            )
        # Fail fast on unknown timezones.
        ZoneInfo(self.timezone)


class DateMinPerGroup(Constraint):
    def __init__(
        self,
        ref: DataReference,
        column_type: str,
        retention_rules: Mapping[Any, DateRetentionRule],
        min_fraction: float = 1.0,
        name: str | None = None,
        cache_size=None,
        *,
        reference_datetime: dt.datetime | None = None,
    ):
        if not retention_rules:
            raise ValueError("retention_rules must not be empty.")
        self._format = _get_format_from_column_type(column_type)
        self._retention_rules = dict(retention_rules)
        self._reference_datetime = reference_datetime
        super().__init__(
            ref,
            ref_value=min_fraction,
            name=name,
            cache_size=cache_size,
        )

    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[dict[Any, dt.date | None], _OptionalSelections]:
        result, selections = db_access.get_min_per_group(engine, ref)
        min_dates = {
            group: (
                None if min_value is None else _convert_to_date(min_value, self._format)
            )
            for group, min_value in result
        }
        return min_dates, selections

    def _today(self, timezone: str) -> dt.date:
        reference = self._reference_datetime
        if reference is None:
            reference = dt.datetime.now(dt.timezone.utc)
        elif reference.tzinfo is None:
            reference = reference.replace(tzinfo=dt.timezone.utc)
        return reference.astimezone(ZoneInfo(timezone)).date()

    def _effective_min_date(self, rule: DateRetentionRule) -> dt.date:
        return self._today(rule.timezone) - dt.timedelta(days=rule.retention_days)

    def _compare(
        self,
        value_factual: dict[Any, dt.date | None],
        value_target: float,
    ) -> tuple[bool, str | None]:
        min_fraction = value_target
        violations: dict[Any, tuple[dt.date, dt.date]] = {}
        no_data_groups: list[Any] = []
        n_passing = 0
        n_comparable = 0
        for group, rule in self._retention_rules.items():
            factual_min = value_factual.get(group)
            if factual_min is None:
                no_data_groups.append(group)
                continue
            n_comparable += 1
            effective_min = self._effective_min_date(rule)
            if factual_min >= effective_min:
                n_passing += 1
            else:
                violations[group] = (factual_min, effective_min)
        unconfigured_groups = [
            group for group in value_factual if group not in self._retention_rules
        ]

        failure_reasons = []
        if n_comparable == 0:
            failure_reasons.append("no comparable data for any configured group")
        elif (pass_fraction := n_passing / n_comparable) < min_fraction:
            failure_reasons.append(
                f"a group pass rate of {pass_fraction} < {min_fraction}"
            )
        if no_data_groups:
            failure_reasons.append(
                f"configured groups without comparable data: {no_data_groups}"
            )
        if not failure_reasons:
            return True, None

        assertion_text = f"{self._ref} has {'; '.join(failure_reasons)}. "
        if violations:
            violation_strings = [
                f"{group}: min {factual_min} < effective min {effective_min}"
                for group, (factual_min, effective_min) in violations.items()
            ]
            assertion_text += "Violating groups: " + ", ".join(violation_strings) + ". "
        if unconfigured_groups:
            assertion_text += (
                "Groups without retention rule, not participating in "
                f"pass rate: {unconfigured_groups}. "
            )
        assertion_text += self._condition_string
        return False, assertion_text


class DateMax(Constraint):
    def __init__(
        self,
        ref: DataReference,
        use_upper_bound_reference: bool,
        column_type: str,
        name: str | None = None,
        cache_size=None,
        *,
        ref2: DataReference | None = None,
        max_value: str | None = None,
    ):
        self._format = _get_format_from_column_type(column_type)
        self._use_upper_bound_reference = use_upper_bound_reference
        max_date: dt.date | None = None
        if max_value is not None:
            max_date = dt.datetime.strptime(max_value, _INPUT_DATE_FORMAT).date()
        super().__init__(
            ref,
            ref2=ref2,
            ref_value=max_date,
            name=name,
            cache_size=cache_size,
        )

    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[dt.date, _OptionalSelections]:
        value, selections = db_access.get_max(engine, ref)
        return _convert_to_date(value, self._format), selections

    def _compare(
        self, value_factual: dt.date, value_target: dt.date
    ) -> tuple[bool, str | None]:
        if value_factual is None:
            return True, None
        if value_target is None:
            return value_factual == 0, "Empty reference set."
        if self._use_upper_bound_reference:
            assertion_text = (
                f"{self._ref} has max {value_factual} > "
                f"{self._target_prefix} {value_target}. "
                f"{self._condition_string}"
            )
            result = value_factual <= value_target
        else:
            assertion_text = (
                f"{self._ref} has max {value_factual} < "
                f"{self._target_prefix} {value_target}. "
                f"{self._condition_string}"
            )
            result = value_factual >= value_target

        return result, assertion_text


class DateBetween(Constraint):
    def __init__(
        self,
        ref: DataReference,
        min_fraction: float,
        lower_bound: str,
        upper_bound: str,
        name: str | None = None,
        cache_size=None,
    ):
        super().__init__(ref, ref_value=min_fraction, name=name, cache_size=cache_size)
        self._lower_bound = lower_bound
        self._upper_bound = upper_bound

    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[float | None, _OptionalSelections]:
        return db_access.get_fraction_between(
            engine, ref, self._lower_bound, self._upper_bound
        )

    def _compare(self, value_factual: float, value_target: float) -> tuple[bool, str]:
        assertion_text = (
            f"{self._ref} has {value_factual} < "
            f"{value_target} of values between {self._lower_bound} and "
            f"{self._upper_bound}. {self._condition_string} "
        )
        result = value_factual >= value_target
        return result, assertion_text


class DateNoOverlap(NoOverlapConstraint):
    _DIMENSIONS = 1

    def _compare(
        self, value_factual: tuple[int, int], value_target: Any
    ) -> tuple[bool, str | None]:
        n_violation_keys, n_distinct_key_values = value_factual
        if n_distinct_key_values == 0:
            return True, None
        violation_fraction = n_violation_keys / n_distinct_key_values
        assertion_text = (
            f"{self._ref} has a ratio of {violation_fraction} > "
            f"{self._max_relative_n_violations} keys in columns {self._key_columns} "
            f"with overlapping date ranges in {self._start_columns[0]} and {self._end_columns[0]}."
            f"E.g. for: {self.sample}."
        )
        result = violation_fraction <= self._max_relative_n_violations
        return result, assertion_text


class DateNoOverlap2d(NoOverlapConstraint):
    _DIMENSIONS = 2

    def _compare(
        self, value_factual: tuple[int, int], value_target: Any
    ) -> tuple[bool, str | None]:
        n_violation_keys, n_distinct_key_values = value_factual
        if n_distinct_key_values == 0:
            return True, None
        violation_fraction = n_violation_keys / n_distinct_key_values
        assertion_text = (
            f"{self._ref} has a ratio of {violation_fraction} > "
            f"{self._max_relative_n_violations} keys in columns {self._key_columns} "
            f"with overlapping date ranges in {self._start_columns[0]} and {self._end_columns[0]}."
            f"and {self._start_columns[1]} and {self._end_columns[1]}."
            f"E.g. for: {self.sample}."
        )
        result = violation_fraction <= self._max_relative_n_violations
        return result, assertion_text


class DateNoGap(NoGapConstraint):
    _DIMENSIONS = 1

    def _select(self, engine: sa.engine.Engine, ref: DataReference) -> _Selects:
        sample_selection, n_violations_selection = db_access.get_date_gaps(
            engine,
            ref,
            self._key_columns,
            self._start_columns[0],
            self._end_columns[0],
            self._legitimate_gap_size,
        )
        # TODO: Once get_unique_count also only returns a selection without
        # executing it, one would want to list this selection here as well.
        return sample_selection, n_violations_selection

    def _compare(
        self, value_factual: tuple[int, int], value_target: Any
    ) -> tuple[bool, str | None]:
        n_violation_keys, n_distinct_key_values = value_factual
        if n_distinct_key_values == 0:
            return True, None
        violation_fraction = n_violation_keys / n_distinct_key_values
        assertion_text = (
            f"{self._ref} has a ratio of {violation_fraction} > "
            f"{self._max_relative_n_violations} keys in columns {self._key_columns} "
            f"with a gap in the date range in {self._start_columns[0]} and {self._end_columns[0]}."
            f"E.g. for: {self.sample}."
        )
        result = violation_fraction <= self._max_relative_n_violations
        return result, assertion_text
