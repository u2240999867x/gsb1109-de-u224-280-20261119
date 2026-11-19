from __future__ import annotations

import itertools
import re
from typing import Any

import sqlalchemy as sa

from .. import db_access
from ..db_access import DataReference
from ..utils import normalize_unicode, validate_unicode_normalization
from .base import Constraint, TestResult, _OptionalSelections


def _normalization_string(unicode_normalization: str | None) -> str:
    if unicode_normalization is None:
        return ""
    return f" after Unicode normalization ({unicode_normalization})"


class VarCharRegexDb(Constraint):
    def __init__(
        self,
        ref: DataReference,
        regex: str,
        allow_none: bool = False,
        relative_tolerance: float = 0.0,
        aggregated: bool = True,
        n_counterexamples: int = 5,
        unicode_normalization: str | bool | None = None,
        name: str | None = None,
        cache_size=None,
    ):
        super().__init__(
            ref,
            ref_value=relative_tolerance,
            name=name,
            cache_size=cache_size,
        )
        self._regex = regex
        self._allow_none = allow_none
        self._aggregated = aggregated
        self._n_counterexamples = n_counterexamples
        self._unicode_normalization = validate_unicode_normalization(
            unicode_normalization
        )

    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[Any, _OptionalSelections]:
        (
            (
                n_violations,
                n_rows,
                n_nulls,
                counterexamples,
            ),
            violations_selections,
        ) = db_access.get_regex_violations(
            engine=engine,
            ref=ref,
            aggregated=self._aggregated,
            regex=self._regex,
            n_counterexamples=self._n_counterexamples,
            unicode_normalization=self._unicode_normalization,
        )
        return (
            n_violations,
            n_rows,
            n_nulls,
            counterexamples,
        ), violations_selections

    def _compare(self, value_factual, value_target) -> tuple[bool, str]:
        (
            factual_n_violations,
            factual_n_rows,
            factual_n_nulls,
            factual_counterexamples,
        ) = value_factual

        if factual_n_nulls > 0 and not self._allow_none:
            return False, (
                "The column contains a None value when it's not allowed. "
                "To ignore None values, please use `allow_none=True` option."
            )
        if factual_n_rows == 0:
            return True, "Empty selection."

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
        assertion_text = (
            f"{self._ref} "
            f"breaks regex '{self._regex}'"
            f"{_normalization_string(self._unicode_normalization)} in "
            f"{factual_relative_violations} > "
            f"{value_target} of the cases. "
            f"In absolute terms, {factual_n_violations} of the {factual_n_rows} samples "
            f"violated the regex. {counterexample_string}{self._condition_string}"
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
        unicode_normalization: str | bool | None = None,
        name: str | None = None,
        cache_size=None,
    ):
        super().__init__(ref, ref_value=regex, name=name, cache_size=cache_size)
        self._allow_none = allow_none
        self._relative_tolerance = relative_tolerance
        self._aggregated = aggregated
        self._n_counterexamples = n_counterexamples
        self._unicode_normalization = validate_unicode_normalization(
            unicode_normalization
        )

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
        uniques_mismatching = {
            x
            for x in uniques_factual
            if not pattern.search(normalize_unicode(x, self._unicode_normalization))
        }

        if self._aggregated:
            n_violations = len(uniques_mismatching)
            n_total = len(uniques_factual)
        else:
            n_violations = sum(uniques_counter[key] for key in uniques_mismatching)
            n_total = sum(count for _, count in uniques_counter.items())

        if n_total == 0:
            return TestResult.success()

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

        if n_relative_violations > self._relative_tolerance:
            assertion_text = (
                f"{self._ref} "
                f"breaks regex '{self._ref_value}'"
                f"{_normalization_string(self._unicode_normalization)} in "
                f"{n_relative_violations} > "
                f"{self._relative_tolerance} of the cases. "
                f"In absolute terms, {n_violations} of the {n_total} samples violated the regex. "
                f"{counterexample_string}{self._condition_string}"
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
