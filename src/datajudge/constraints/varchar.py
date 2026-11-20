from __future__ import annotations

import itertools
import re
from typing import Any

import sqlalchemy as sa

from .. import db_access
from ..db_access import DataReference
from ..equivalence import (
    DIGIT_WIDTH,
    EXACT,
    normalize_digit_width,
    validate_equivalence_mode,
)
from .base import Constraint, TestResult, _OptionalSelections


class VarCharRegexDb(Constraint):
    def __init__(
        self,
        ref: DataReference,
        regex: str,
        relative_tolerance: float = 0.0,
        aggregated: bool = True,
        n_counterexamples: int = 5,
        equivalence_mode: str = EXACT,
        name: str | None = None,
        cache_size=None,
    ):
        validate_equivalence_mode(equivalence_mode)
        super().__init__(
            ref,
            ref_value=relative_tolerance,
            name=name,
            cache_size=cache_size,
        )
        self._regex = regex
        self._aggregated = aggregated
        self._n_counterexamples = n_counterexamples
        self._equivalence_mode = equivalence_mode

    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[Any, _OptionalSelections]:
        (
            (
                n_violations,
                counterexamples,
            ),
            violations_selections,
        ) = db_access.get_regex_violations(
            engine=engine,
            ref=ref,
            aggregated=self._aggregated,
            regex=self._regex,
            n_counterexamples=self._n_counterexamples,
            equivalence_mode=self._equivalence_mode,
        )
        if self._aggregated:
            n_rows, n_rows_selections = db_access.get_unique_count(
                engine=engine,
                ref=ref,
                equivalence_mode=self._equivalence_mode,
            )
        else:
            n_rows, n_rows_selections = db_access.get_row_count(engine=engine, ref=ref)
        return (
            n_violations,
            n_rows,
            counterexamples,
        ), violations_selections + n_rows_selections

    def _compare(self, value_factual, value_target) -> tuple[bool, str]:
        (
            factual_n_violations,
            factual_n_rows,
            factual_counterexamples,
        ) = value_factual
        factual_relative_violations = factual_n_violations / factual_n_rows
        result = factual_relative_violations <= value_target
        counterexample_string = (
            (
                "Some counterexamples consist of the following: "
                f"{factual_counterexamples}. "
            )
            if factual_counterexamples and len(factual_counterexamples) > 0
            else ""
        )
        equivalence_string = (
            ""
            if self._equivalence_mode == EXACT
            else (
                " Full-width and ASCII digits were treated as interchangeable; "
                "counterexamples still show the original, unnormalized values."
            )
        )
        assertion_text = (
            f"{self._ref} "
            f"breaks regex '{self._regex}' in {factual_relative_violations} > "
            f"{value_target} of the cases. "
            f"In absolute terms, {factual_n_violations} of the {factual_n_rows} samples "
            f"violated the regex. {counterexample_string}"
            f"{equivalence_string}{self._condition_string}"
        )
        return result, assertion_text


class VarCharRegex(Constraint):
    def __init__(
        self,
        ref: DataReference,
        regex: str,
        allow_none: bool = False,
        relative_tolerance: float = 0.0,
        aggregated: bool = True,
        n_counterexamples: int = 5,
        equivalence_mode: str = EXACT,
        name: str | None = None,
        cache_size=None,
    ):
        validate_equivalence_mode(equivalence_mode)
        super().__init__(ref, ref_value=regex, name=name, cache_size=cache_size)
        self._allow_none = allow_none
        self._relative_tolerance = relative_tolerance
        self._aggregated = aggregated
        self._n_counterexamples = n_counterexamples
        self._equivalence_mode = equivalence_mode

    def test(self, engine: sa.engine.Engine) -> TestResult:
        uniques_counter, selections = db_access.get_uniques(engine, self._ref)
        self.factual_selections = selections
        if not self._allow_none and uniques_counter.get(None):
            return TestResult.failure(
                "The column contains a None value when it's not allowed. "
                "To ignore None values, please use `allow_none=True` option."
            )
        elif None in uniques_counter:
            uniques_counter.pop(None)

        uniques_factual = list(uniques_counter.keys())
        if not self._ref_value:
            return TestResult.failure("No regex pattern given")

        pattern = re.compile(self._ref_value)
        if self._equivalence_mode == DIGIT_WIDTH:
            # The regex is evaluated on the normalized code. Several raw
            # values (e.g. '007', '００７', '0０7') share one normalized code
            # and therefore count once in the aggregated denominator and
            # violation count. The original values are kept separately so
            # that counterexamples can still be returned to the source.
            normalized_counter: dict[str, int] = {}
            raw_values_by_normalized: dict[str, list[Any]] = {}
            for raw_value, count in uniques_counter.items():
                normalized_value = normalize_digit_width(raw_value)
                normalized_counter[normalized_value] = (
                    normalized_counter.get(normalized_value, 0) + count
                )
                raw_values_by_normalized.setdefault(normalized_value, []).append(
                    raw_value
                )

            normalized_mismatching = {
                normalized
                for normalized in normalized_counter
                if not pattern.match(normalized)
            }
            uniques_mismatching = {
                raw_value
                for normalized in normalized_mismatching
                for raw_value in raw_values_by_normalized[normalized]
            }

            if self._aggregated:
                n_violations = len(normalized_mismatching)
                n_total = len(normalized_counter)
            else:
                n_violations = sum(
                    normalized_counter[normalized]
                    for normalized in normalized_mismatching
                )
                n_total = sum(count for _, count in uniques_counter.items())
        else:
            uniques_mismatching = {
                x for x in uniques_factual if not pattern.match(x)
            }

            if self._aggregated:
                n_violations = len(uniques_mismatching)
                n_total = len(uniques_factual)
            else:
                n_violations = sum(uniques_counter[key] for key in uniques_mismatching)
                n_total = sum(count for _, count in uniques_counter.items())

        n_relative_violations = n_violations / n_total

        if self._n_counterexamples == -1:
            counterexamples = list(uniques_mismatching)
        else:
            counterexamples = list(
                itertools.islice(uniques_mismatching, self._n_counterexamples)
            )

        counterexample_string = (
            (f"Some counterexamples consist of the following: {counterexamples}. ")
            if counterexamples and len(counterexamples) > 0
            else ""
        )

        equivalence_string = (
            ""
            if self._equivalence_mode == EXACT
            else (
                " Full-width and ASCII digits were treated as interchangeable; "
                "counterexamples still show the original, unnormalized values."
            )
        )

        if n_relative_violations > self._relative_tolerance:
            assertion_text = (
                f"{self._ref} "
                f"breaks regex '{self._ref_value}' in {n_relative_violations} > "
                f"{self._relative_tolerance} of the cases. "
                f"In absolute terms, {n_violations} of the {n_total} samples violated the regex. "
                f"{counterexample_string}{equivalence_string}{self._condition_string}"
            )
            return TestResult.failure(assertion_text)
        return TestResult.success()


class VarCharMinLength(Constraint):
    def __init__(
        self,
        ref,
        *,
        ref2: DataReference | None = None,
        min_length: int | None = None,
        name: str | None = None,
        cache_size=None,
    ):
        super().__init__(
            ref,
            ref2=ref2,
            ref_value=min_length,
            name=name,
            cache_size=cache_size,
        )

    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[int, _OptionalSelections]:
        return db_access.get_min_length(engine, ref)

    def _compare(
        self, value_factual: int, value_target: int
    ) -> tuple[bool, str | None]:
        if value_target is None:
            return True, None
        if value_factual is None:
            return value_target == 0, "Empty set."
        assertion_text = (
            f"{self._ref} "
            f"has min length {value_factual} instead of "
            f"{self._target_prefix} {value_target}. "
            f"{self._condition_string}"
        )
        result = value_factual >= value_target
        return result, assertion_text


class VarCharMaxLength(Constraint):
    def __init__(
        self,
        ref: DataReference,
        *,
        ref2: DataReference | None = None,
        max_length: int | None = None,
        name: str | None = None,
        cache_size=None,
    ):
        super().__init__(
            ref,
            ref2=ref2,
            ref_value=max_length,
            name=name,
            cache_size=cache_size,
        )

    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[int, _OptionalSelections]:
        return db_access.get_max_length(engine, ref)

    def _compare(
        self, value_factual: int, value_target: int
    ) -> tuple[bool, str | None]:
        if value_factual is None:
            return True, None
        if value_target is None:
            return value_factual == 0, "Reference value is None."
        assertion_text = (
            f"{self._ref} "
            f"has max length {value_factual} instead of "
            f"{self._target_prefix} {value_target}. "
            f"{self._condition_string}"
        )
        result = value_factual <= value_target
        return result, assertion_text
