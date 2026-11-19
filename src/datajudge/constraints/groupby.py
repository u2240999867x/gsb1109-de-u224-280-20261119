from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from .. import db_access
from ..db_access import DataReference
from .base import Constraint, _OptionalSelections


class AggregateNumericRangeEquality(Constraint):
    def __init__(
        self,
        ref: DataReference,
        aggregation_column: str,
        start_value: int = 0,
        name: str | None = None,
        cache_size=None,
        *,
        tolerance: float = 0,
        ref2: DataReference | None = None,
    ):
        super().__init__(
            ref,
            ref2=ref2,
            ref_value=object(),
            name=name,
            cache_size=cache_size,
        )
        self._aggregation_column = aggregation_column
        self._tolerance = tolerance
        self._start_value = start_value
        self._selection = None

    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[Any, _OptionalSelections]:
        result, selections = db_access.get_column_array_agg(
            engine, ref, self._aggregation_column
        )
        result = {fact[:-1]: fact[-1] for fact in result}
        return result, selections

    def _compare(
        self, value_factual: Any, value_target: Any
    ) -> tuple[bool, str | None]:
        def missing_from_range(values, start=0):
            number_values = [value for value in values if value is not None]
            if not number_values:
                return None
            return set(range(start, max(number_values) + start)) - set(number_values)

        empty_results = [
            key
            for key, values in value_factual.items()
            if all(value is None for value in values)
        ]
        results = {
            key: missing
            for key, values in value_factual.items()
            if (missing := missing_from_range(values, self._start_value)) is not None
        }
        failed_results = dict(filter(lambda x: len(x[1]) > 0, results.items()))

        if len(value_factual) == 0:
            return True, None
        n_failed = len(failed_results) + len(empty_results)
        if n_failed / len(value_factual) > self._tolerance:
            assertion_text = (
                f"{self._ref} has unfulfilled continuity requirement for "
                f"(key, missing values): `{failed_results}`."
            )
            if empty_results:
                assertion_text += (
                    f" {len(empty_results)} group(s) without any values: "
                    f"`{empty_results}`."
                )
            assertion_text += f"{self._condition_string}"
            return False, assertion_text
        return True, None


_GroupFacts = tuple[Any, int, int]


def _format_number(value: Any) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    if isinstance(value, Decimal) and value == value.to_integral_value():
        return str(int(value))
    return str(value)


def _as_number(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    return value


def _format_range(lower_bound: float | None, upper_bound: float | None) -> str:
    lower = "-inf" if lower_bound is None else _format_number(lower_bound)
    upper = "inf" if upper_bound is None else _format_number(upper_bound)
    return f"[{lower}, {upper}]"


def _key_label(key: tuple[Any, ...], columns: Sequence[str]) -> str:
    return ", ".join(f"{column}={value!r}" for column, value in zip(columns, key))


class AggregateNumericRange(Constraint):
    """Check the aggregate of ``aggregation_column`` separately for every group.

    ``ref.columns`` defines the group, e.g. store and day. Grouping ensures that
    deviations of individual groups cannot cancel out in an overall total.

    The allowed range is either given by ``lower_bound`` and ``upper_bound`` or
    derived from a reference aggregate. In the latter case ``tolerance`` is the
    allowed relative deviation from the per-group reference value;
    ``max_absolute_deviation`` takes precedence and yields a symmetric band
    around the reference. This also defines the band for reference aggregates
    of zero, for which a relative tolerance would otherwise collapse to zero.

    Groups with rows but without any non-``NULL`` value in
    ``aggregation_column`` are reported as having no amount samples. They are
    deliberately not treated as groups whose aggregate is zero: the latter is a
    genuine result and is checked against the allowed range like any other
    aggregate.
    """

    def __init__(
        self,
        ref: DataReference,
        aggregation_column: str,
        lower_bound: float | None = None,
        upper_bound: float | None = None,
        *,
        ref2: DataReference | None = None,
        aggregation_column2: str | None = None,
        tolerance: float = 0,
        max_absolute_deviation: float | None = None,
        target_aggregate: float | None = None,
        n_results: int = 10,
        allow_empty_groups: bool = False,
        name: str | None = None,
        cache_size=None,
        max_relative_deviation: float | None = None,
        min_value: float | None = None,
        max_value: float | None = None,
        max_deviation: float | None = None,
        allowed_range: tuple[float, float] | None = None,
        allowed_ranges: Mapping[tuple[Any, ...], tuple[float, float]] | None = None,
        bounds: tuple[float | None, float | None] | None = None,
        aggregate_column: str | None = None,
        expected_sum: float | None = None,
        min_sum: float | None = None,
        max_sum: float | None = None,
        relative_tolerance: float | None = None,
        max_relative_tolerance: float | None = None,
    ):
        if aggregate_column is not None:
            if aggregation_column != aggregate_column:
                raise ValueError(
                    "aggregation_column and aggregate_column are aliases; only "
                    "one may be given."
                )
            aggregation_column = aggregate_column
        if expected_sum is not None:
            if target_aggregate is not None and target_aggregate != expected_sum:
                raise ValueError(
                    "target_aggregate and expected_sum are aliases; only one "
                    "may be given."
                )
            target_aggregate = expected_sum
        if min_sum is not None:
            if lower_bound is not None and lower_bound != min_sum:
                raise ValueError(
                    "lower_bound and min_sum are aliases; only one may be given."
                )
            lower_bound = min_sum
        if max_sum is not None:
            if upper_bound is not None and upper_bound != max_sum:
                raise ValueError(
                    "upper_bound and max_sum are aliases; only one may be given."
                )
            upper_bound = max_sum
        for alias in (relative_tolerance, max_relative_tolerance):
            if alias is None:
                continue
            if (
                max_relative_deviation is not None
                and max_relative_deviation != alias
            ):
                raise ValueError(
                    "Conflicting relative tolerance aliases were given."
                )
            max_relative_deviation = alias
        if bounds is not None:
            if lower_bound is not None or upper_bound is not None:
                raise ValueError("bounds and explicit bounds are mutually exclusive.")
            lower_bound, upper_bound = bounds
        if allowed_range is not None:
            if lower_bound is not None or upper_bound is not None:
                raise ValueError(
                    "allowed_range and explicit bounds are mutually exclusive."
                )
            lower_bound, upper_bound = allowed_range
        if min_value is not None:
            if lower_bound is not None and lower_bound != min_value:
                raise ValueError(
                    "lower_bound and min_value are aliases; only one may be given."
                )
            lower_bound = min_value
        if max_value is not None:
            if upper_bound is not None and upper_bound != max_value:
                raise ValueError(
                    "upper_bound and max_value are aliases; only one may be given."
                )
            upper_bound = max_value
        if max_deviation is not None:
            if (
                max_absolute_deviation is not None
                and max_absolute_deviation != max_deviation
            ):
                raise ValueError(
                    "max_absolute_deviation and max_deviation are aliases; only "
                    "one may be given."
                )
            max_absolute_deviation = max_deviation
        if (
            lower_bound is None
            and upper_bound is None
            and target_aggregate is None
            and ref2 is None
            and allowed_ranges is None
        ):
            raise ValueError(
                "Either bounds, per-group bounds, a target aggregate or a "
                "reference are required."
            )
        if target_aggregate is not None and ref2 is not None:
            raise ValueError("Use either a target aggregate or a reference, not both.")
        if max_relative_deviation is not None:
            if tolerance != 0 and tolerance != max_relative_deviation:
                raise ValueError(
                    "tolerance and max_relative_deviation are aliases; only "
                    "one of them may be given."
                )
            tolerance = max_relative_deviation
        if tolerance < 0:
            raise ValueError(f"tolerance must be non-negative but is {tolerance}.")
        if max_absolute_deviation is not None and max_absolute_deviation < 0:
            raise ValueError(
                "max_absolute_deviation must be non-negative but is "
                f"{max_absolute_deviation}."
            )
        if ref2 is not None and aggregation_column2 is None:
            aggregation_column2 = aggregation_column
        super().__init__(
            ref,
            ref2=ref2,
            ref_value=None if ref2 is not None else object(),
            name=name,
            cache_size=cache_size,
        )
        self._aggregation_column = aggregation_column
        self._aggregation_column2 = aggregation_column2
        self._lower_bound = lower_bound
        self._upper_bound = upper_bound
        self._tolerance = tolerance
        self._max_absolute_deviation = max_absolute_deviation
        self._target_aggregate = target_aggregate
        self._n_results = n_results
        self._allow_empty_groups = allow_empty_groups
        self._allowed_ranges = dict(allowed_ranges) if allowed_ranges else None

    @property
    def _columns(self) -> list[str]:
        if self._ref.columns is None:
            raise ValueError("There must be at least one column to group by.")
        return self._ref.columns

    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[dict[tuple[Any, ...], _GroupFacts], _OptionalSelections]:
        aggregation_column = (
            self._aggregation_column2 if ref is self._ref2 else self._aggregation_column
        )
        if aggregation_column is None:
            raise ValueError("Reference requires an aggregation column.")
        rows, selections = db_access.get_grouped_aggregate(
            engine, ref, aggregation_column
        )
        return {row[:-3]: (row[-3], row[-2], row[-1]) for row in rows}, selections

    def _allowed_range(
        self, target: float | None = None
    ) -> tuple[float | None, float | None]:
        if target is not None:
            if self._max_absolute_deviation is not None:
                return (
                    target - self._max_absolute_deviation,
                    target + self._max_absolute_deviation,
                )
            delta = self._tolerance * abs(target)
            return target - delta, target + delta
        return self._lower_bound, self._upper_bound

    def _bounds_for(
        self, key: tuple[Any, ...], target: float | None
    ) -> tuple[float | None, float | None]:
        if self._allowed_ranges is not None:
            if key not in self._allowed_ranges:
                return None, None
            lower_bound, upper_bound = self._allowed_ranges[key]
            return lower_bound, upper_bound
        if target is not None:
            return self._allowed_range(target)
        return self._allowed_range()

    def _check_group(
        self,
        aggregate: Any,
        n_samples: int,
        n_rows: int,
        lower_bound: float | None,
        upper_bound: float | None,
    ) -> tuple[bool, str]:
        if lower_bound is None and upper_bound is None:
            allowed = "not specified"
        else:
            allowed = _format_range(lower_bound, upper_bound)
        if n_samples == 0:
            return False, (
                "no amount samples; allowed range "
                f"{allowed} (0 sample row(s) of {n_rows} row(s))"
            )
        if aggregate is None:
            return False, (
                "aggregate unavailable; allowed range "
                f"{allowed} ({n_samples} sample row(s) of {n_rows} row(s))"
            )
        outside = (lower_bound is not None and aggregate < lower_bound) or (
            upper_bound is not None and aggregate > upper_bound
        )
        samples = f"{n_samples} sample row(s) of {n_rows} row(s)"
        if outside:
            return False, (
                f"sum {_format_number(aggregate)} outside allowed range "
                f"{allowed} ({samples})"
            )
        return True, (
            f"sum {_format_number(aggregate)} within allowed range "
            f"{allowed} ({samples})"
        )

    def get_report(self, engine: sa.engine.Engine) -> list[dict[str, Any]]:
        """Return one report row per group with its sample size and allowed range.

        Each row contains the group ``key``, the ``aggregate``, the number of
        ``n_samples`` contributing non-``NULL`` values, the number of ``n_rows``
        in the group, the ``allowed_lower_bound``/``allowed_upper_bound``, the
        ``outcome`` and a ``reason``. Groups without amount samples carry
        ``aggregate=None`` and ``outcome=False`` unless ``allow_empty_groups``
        is set. The ``status`` field labels a group as ``ok``, ``out_of_range``,
        ``no_samples`` or ``missing``, so that a real aggregate of zero is
        distinguishable from a group that has no amounts at all.
        """
        # ty can't figure out that this is a method and that self is passed
        # as the first argument.
        facts = self._get_factual_value(engine=engine)  # type: ignore[missing-argument]
        # ty can't figure out that this is a method and that self is passed
        # as the first argument.
        target = self._get_target_value(engine=engine)  # type: ignore[missing-argument]
        target_groups = target if self._ref2 is not None else None

        report: list[dict[str, Any]] = []
        for key, (aggregate, n_samples, n_rows) in facts.items():
            no_range = self._allowed_ranges is not None and (
                key not in self._allowed_ranges
            )
            if target_groups is not None:
                reference = target_groups.get(key)
                if reference is None or reference[0] is None or no_range:
                    lower_bound, upper_bound = None, None
                else:
                    lower_bound, upper_bound = self._bounds_for(
                        key, float(reference[0])
                    )
            else:
                lower_bound, upper_bound = (
                    (None, None)
                    if no_range
                    else self._bounds_for(key, self._target_aggregate)
                )

            ok, description = self._check_group(
                aggregate, n_samples, n_rows, lower_bound, upper_bound
            )
            empty = n_samples == 0
            tolerated = empty and self._allow_empty_groups
            if no_range:
                status = "no_range"
            elif ok:
                status = "ok"
            elif empty:
                status = "no_samples"
            else:
                status = "out_of_range"
            report.append(
                {
                    "key": key,
                    "status": status,
                    "aggregate": _as_number(aggregate),
                    "n_samples": n_samples,
                    "n_rows": n_rows,
                    "allowed_lower_bound": lower_bound,
                    "allowed_upper_bound": upper_bound,
                    "allowed_range": (lower_bound, upper_bound),
                    "allow_empty_group": self._allow_empty_groups,
                    "outcome": False if no_range else (ok or tolerated),
                    "reason": (
                        "no allowed range for group"
                        if no_range
                        else description
                    ),
                }
            )

        if target_groups is not None:
            for key, (reference, n_samples, n_rows) in target_groups.items():
                if key in facts:
                    continue
                report.append(
                    {
                        "key": key,
                        "status": "missing",
                        "aggregate": None,
                        "n_samples": 0,
                        "n_rows": 0,
                        "allowed_lower_bound": None,
                        "allowed_upper_bound": None,
                        "allowed_range": (None, None),
                        "allow_empty_group": self._allow_empty_groups,
                        "outcome": False,
                        "reason": (
                            "missing group (no rows in data); reference has "
                            f"{n_samples} sample row(s) of {n_rows} row(s)"
                        ),
                        "reference_n_samples": n_samples,
                        "reference_n_rows": n_rows,
                    }
                )
        return report

    def format_report(self, engine: sa.engine.Engine) -> str:
        """Render the per-group report as one human-readable line per group."""
        lines = []
        for row in self.get_report(engine):
            label = _key_label(row["key"], self._columns)
            aggregate = (
                "NULL"
                if row["aggregate"] is None
                else _format_number(row["aggregate"])
            )
            lines.append(
                f"{label}: sum={aggregate}, "
                f"samples={row['n_samples']}/{row['n_rows']}, "
                f"allowed={_format_range(*row['allowed_range'])}, "
                f"status={row['status']}"
            )
        return "\n".join(lines)

    get_group_report = get_report
    report = get_report
    group_report = get_report
    get_per_group_report = get_report

    def _compare(
        self, value_factual: Any, value_target: Any
    ) -> tuple[bool, str | None]:
        failures: list[str] = []
        empty_groups: list[str] = []

        target_groups: dict[tuple[Any, ...], _GroupFacts] | None = (
            value_target if self._ref2 is not None else None
        )
        n_checked = len(value_factual)
        if target_groups is not None:
            n_checked = len(set(value_factual) | set(target_groups))

        for key, (aggregate, n_samples, n_rows) in value_factual.items():
            label = _key_label(key, self._columns)
            if n_samples == 0 and n_rows > 0 and self._allow_empty_groups:
                continue
            if self._allowed_ranges is not None and key not in self._allowed_ranges:
                failures.append(f"{label}: no allowed range for group")
                continue
            if target_groups is not None:
                if key not in target_groups:
                    failures.append(f"{label}: no reference group")
                    continue
                target = target_groups[key][0]
                if target is None:
                    failures.append(
                        f"{label}: reference group has no amount samples"
                    )
                    continue
                lower_bound, upper_bound = self._bounds_for(key, float(target))
            else:
                lower_bound, upper_bound = self._bounds_for(
                    key, self._target_aggregate
                )

            ok, description = self._check_group(
                aggregate, n_samples, n_rows, lower_bound, upper_bound
            )
            if not ok:
                if n_samples == 0 and n_rows > 0:
                    empty_groups.append(f"{label}: {description}")
                else:
                    failures.append(f"{label}: {description}")

        if target_groups is not None:
            for key in target_groups:
                if key not in value_factual:
                    failures.append(
                        f"{_key_label(key, self._columns)}: missing group "
                        "(no rows in data)"
                    )

        if not failures and not empty_groups:
            return True, None

        details = [*failures[: self._n_results], *empty_groups[: self._n_results]]
        n_failing = len(failures) + len(empty_groups)
        if n_failing > len(details):
            details.append(
                f"<SHORTENED OUTPUT, displaying {len(details)} / "
                f"{n_failing} failing group(s)>"
            )

        if self._allowed_ranges is not None:
            range_description = "per-group allowed ranges"
        elif target_groups is not None:
            if self._max_absolute_deviation is not None:
                range_description = (
                    "per-group reference +/- "
                    f"{_format_number(self._max_absolute_deviation)}"
                )
            else:
                range_description = (
                    "per-group reference +/- "
                    f"{self._tolerance:.2%} of the reference"
                )
        else:
            range_description = _format_range(
                *self._allowed_range(self._target_aggregate)
            )

        total_samples = sum(facts[1] for facts in value_factual.values())
        total_rows = sum(facts[2] for facts in value_factual.values())
        assertion_text = (
            f"{self._ref} has {len(failures)} of {n_checked} group(s) outside the "
            f"allowed range {range_description} and {len(empty_groups)} group(s) "
            f"without amount samples, based on {total_samples} sample row(s) of "
            f"{total_rows} row(s). Details: {'; '.join(details)}. "
            f"{self._condition_string}"
        )
        return False, assertion_text


class AggregateNumericBetween(AggregateNumericRange):
    """Alias of :class:`AggregateNumericRange` with an explicit name."""


class GroupbyAggregateRange(AggregateNumericRange):
    """Alias of :class:`AggregateNumericRange` with an explicit name."""


class GroupbyAggregateBetween(AggregateNumericRange):
    """Alias of :class:`AggregateNumericRange` with an explicit name."""


class GroupbyAggregationRange(AggregateNumericRange):
    """Alias of :class:`AggregateNumericRange` with an explicit name."""


class AggregateNumericRangeConstraint(AggregateNumericRange):
    """Alias of :class:`AggregateNumericRange` with an explicit name."""
