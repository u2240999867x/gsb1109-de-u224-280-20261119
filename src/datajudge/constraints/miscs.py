from __future__ import annotations

import warnings
from typing import Any

import sqlalchemy as sa

from .. import db_access
from ..condition import Condition
from ..db_access import DataReference
from .base import Constraint, TestResult, _format_sample, _OptionalSelections


class PrimaryKeyDefinition(Constraint):
    def __init__(
        self,
        ref,
        primary_keys: list[str],
        name: str | None = None,
        cache_size=None,
    ):
        super().__init__(ref, ref_value=set(primary_keys), name=name)

    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[set[str], _OptionalSelections]:
        values, selections = db_access.get_primary_keys(engine, self._ref)
        return set(values), selections

    # Note: Exact equality!
    def _compare(
        self, value_factual: set[str], value_target: set[str]
    ) -> tuple[bool, str | None]:
        assertion_message = ""
        result = True
        # If both are true, just report one.
        if len(value_factual.difference(value_target)) > 0:
            example_key = next(iter(value_factual.difference(value_target)))
            assertion_message = (
                f"{self._ref} incorrectly includes {example_key} as primary key."
            )
            result = False
        if len(value_target.difference(value_factual)) > 0:
            example_key = next(iter(value_target.difference(value_factual)))
            assertion_message = (
                f"{self._ref} doesn't include {example_key} as primary key."
            )
            result = False
        return result, assertion_message


class Uniqueness(Constraint):
    # In contrast to PrimaryKeyDefinition, it is hardly imaginable to have
    # this constraint be used between tables.
    def __init__(
        self,
        ref: DataReference,
        max_duplicate_fraction: float = 0,
        max_absolute_n_duplicates: int = 0,
        infer_pk_columns: bool = False,
        name: str | None = None,
        cache_size=None,
    ):
        if max_duplicate_fraction != 0 and max_absolute_n_duplicates != 0:
            raise ValueError(
                """Uniqueness constraint was attempted to be constructed
                with both a relative and an absolute tolerance. Only use one
                of both at a time."""
            )
        if max_duplicate_fraction != 0:
            ref_value = ("relative", max_duplicate_fraction)
        elif max_absolute_n_duplicates != 0:
            ref_value = ("absolute", max_absolute_n_duplicates)
        else:
            ref_value = ("relative", 0)

        self.infer_pk_columns = infer_pk_columns
        super().__init__(ref, ref_value=ref_value, name=name, cache_size=cache_size)

    def test(self, engine: sa.engine.Engine) -> TestResult:
        if self.infer_pk_columns and db_access.is_bigquery(engine):
            raise NotImplementedError("No primary key concept in BigQuery")

        # only check for primary keys when actually defined
        # otherwise default back to searching the whole table
        if self.infer_pk_columns and (
            pk_columns := db_access.get_primary_keys(engine, self._ref)[0]
        ):
            self._ref.columns = pk_columns
            if not pk_columns:  # there were no primary keys found
                warnings.warn(
                    f"""No primary keys found in {self._ref}.
                    Uniqueness will be tested for all columns."""
                )

        unique_count, unique_selections = db_access.get_unique_count(engine, self._ref)
        row_count, row_selections = db_access.get_row_count(engine, self._ref)
        self.factual_selections = row_selections
        self.target_selections = unique_selections
        if row_count == 0:
            return TestResult(True, "No occurrences.")

        tolerance_kind, tolerance_value = self._ref_value

        if tolerance_kind == "relative":
            result = unique_count >= row_count * (1 - tolerance_value)
        elif tolerance_kind == "absolute":
            result = unique_count >= row_count - tolerance_value
        else:
            raise ValueError(
                "Given tolerance is neither relative nor absolute: {tolerance_kind}."
            )
        if result:
            return TestResult.success()
        sample, _ = db_access.get_duplicate_sample(engine, self._ref)
        sample_string = _format_sample(sample, self._ref)
        assertion_text = (
            f"{self._ref} has {row_count} rows > {unique_count} "
            f"uniques. This surpasses the max_duplicate_fraction of "
            f"{self._ref_value}. An example tuple breaking the "
            f"uniqueness condition is: {sample_string}."
        )
        return TestResult.failure(assertion_text)


class FunctionalDependency(Constraint):
    def __init__(self, ref: DataReference, key_columns: list[str], **kwargs):
        super().__init__(ref, ref_value=object(), **kwargs)
        self.key_columns = key_columns

    def test(self, engine: sa.engine.Engine) -> TestResult:
        violations, _ = db_access.get_functional_dependency_violations(
            engine, self._ref, self.key_columns
        )
        if not violations:
            return TestResult.success()

        assertion_text = (
            f"{self._ref} has violations of functional dependence (in total {len(violations)} rows):\n"
            + "\n".join(
                [
                    f"{violation}"
                    for violation in self._apply_output_formatting(
                        [tuple(elem) for elem in violations]
                    )
                ]
            )
        )
        return TestResult.failure(assertion_text)


class MaxNullFraction(Constraint):
    def __init__(
        self,
        ref,
        *,
        ref2: DataReference | None = None,
        max_null_fraction: float | None = None,
        max_relative_deviation: float = 0,
        name: str | None = None,
        cache_size=None,
    ):
        super().__init__(
            ref,
            ref2=ref2,
            ref_value=max_null_fraction,
            name=name,
            cache_size=cache_size,
        )
        if max_null_fraction is not None and not (0 <= max_null_fraction <= 1):
            raise ValueError(
                f"max_null_fraction was expected to lie within [0, 1] but is "
                f"{max_null_fraction}."
            )
        if max_relative_deviation < 0:
            raise ValueError(
                f"{max_relative_deviation} is negative even though it needs to be positive."
            )
        self.max_relative_deviation = max_relative_deviation

    def _retrieve(self, engine: sa.engine.Engine, ref: DataReference):
        return db_access.get_missing_fraction(engine=engine, ref=ref)

    def _compare(
        self, value_factual: float, value_target: float
    ) -> tuple[bool, str | None]:
        threshold = value_target * (1 + self.max_relative_deviation)
        result = value_factual <= threshold
        assertion_text = (
            f"{value_factual} of {self._ref} values are NULL "
            f"while only {self._target_prefix}{threshold} were allowed to be NULL."
        )
        return result, assertion_text


class ConditionalNullFraction(Constraint):
    """Assert that a column is only allowed to be NULL outside of a condition.

    This expresses conditional mandatory values: within the population defined
    by ``condition`` the column must be populated; outside of the condition
    NULL values are allowed. Typical example: a tracking number must exist for
    shipped rows and may be missing for rows that have not been shipped yet.

    The threshold is applied to the conditional population only, i.e. the
    denominator is the number of rows matching ``condition`` rather than the
    number of rows in the whole data source.

    Parameters
    ----------
    ref:
        Reference to the column that is conditionally mandatory.
    condition:
        Condition selecting the rows for which ``column`` must not be NULL.
    max_null_fraction:
        Allowed fraction of NULL values within the conditional population.
    name:
        Optional name of the constraint.
    cache_size:
        Optional cache size for retrieved values.
    """

    def __init__(
        self,
        ref: DataReference,
        column: str,
        condition: Condition,
        max_null_fraction: float = 0,
        name: str | None = None,
        cache_size=None,
    ):
        if not (0 <= max_null_fraction <= 1):
            raise ValueError(
                f"max_null_fraction was expected to lie within [0, 1] but is "
                f"{max_null_fraction}."
            )
        super().__init__(
            ref,
            ref_value=max_null_fraction,
            name=name,
            cache_size=cache_size,
        )
        self._column = column
        self._condition = condition
        self._max_null_fraction = max_null_fraction

    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[tuple[int, int], _OptionalSelections]:
        counts, selections = db_access.get_conditional_null_counts(
            engine=engine,
            ref=ref,
            column=self._column,
            condition=self._condition,
        )
        return counts, selections

    def _compare(
        self, value_factual: tuple[int, int], value_target: Any
    ) -> tuple[bool, str | None]:
        n_violations, n_denominator = value_factual
        relative_violations = (
            n_violations / n_denominator if n_denominator > 0 else 0.0
        )
        result = relative_violations <= self._max_null_fraction
        assertion_text = (
            f"{self._ref} has a fraction of {relative_violations} > "
            f"{self._max_null_fraction} NULL values within the condition "
            f"'{self._condition}' ({n_violations} / {n_denominator})."
        )
        return result, assertion_text

    def _result_details(self, value_factual, value_target) -> dict[str, Any]:
        n_violations, n_denominator = value_factual
        return {
            "_n_violations": n_violations,
            "_n_denominator": n_denominator,
            "_denominator_description": (
                f"rows matching condition '{self._condition}'"
            ),
        }


class GroupedUniqueness(Constraint):
    """Assert that a set of columns is unique within each group.

    Duplicate detection is scoped to groups, e.g. identifiers only need to be
    unique within a warehouse. Group columns are not required to be unique
    themselves. Rows with a NULL value in any of the checked columns are
    excluded, since a missing identifier is not a duplicate. Use
    :class:`ConditionalNullFraction` or :class:`MaxNullFraction` to reason
    about NULL identifiers separately.

    The reported fraction is the share of redundant rows among rows with a
    non-null identifier, i.e. the ratio ``(rows - distinct identifiers) / rows``
    within the groups. It therefore matches the semantics of
    :class:`~datajudge.constraints.miscs.Uniqueness` for relative tolerances.
    """

    def __init__(
        self,
        ref: DataReference,
        group_columns: list[str],
        max_duplicate_fraction: float = 0,
        name: str | None = None,
        cache_size=None,
    ):
        if not group_columns:
            raise ValueError("At least one group column is required.")
        if not (0 <= max_duplicate_fraction < 1):
            raise ValueError(
                "max_duplicate_fraction was expected to lie within [0, 1) but is "
                f"{max_duplicate_fraction}."
            )
        super().__init__(
            ref,
            ref_value=max_duplicate_fraction,
            name=name,
            cache_size=cache_size,
        )
        self._group_columns = group_columns
        self._max_duplicate_fraction = max_duplicate_fraction

    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[tuple[int, int, list], _OptionalSelections]:
        return db_access.get_duplicate_counts_by_group(
            engine=engine,
            ref=ref,
            group_columns=self._group_columns,
        )

    def _compare(
        self, value_factual: tuple[int, int, list], value_target: Any
    ) -> tuple[bool, str | None]:
        n_duplicates, n_denominator, examples = value_factual
        relative_duplicates = (
            n_duplicates / n_denominator if n_denominator > 0 else 0.0
        )
        result = relative_duplicates <= self._max_duplicate_fraction
        example_string = ""
        if examples:
            formatted = [
                f"{dict(zip(self._group_columns, group_key))} -> "
                f"{identifier_values} ({n_copies} copies)"
                for group_key, identifier_values, n_copies in examples
            ]
            example_string = " Examples: " + "; ".join(formatted) + "."
        assertion_text = (
            f"{self._ref} has a fraction of {relative_duplicates} > "
            f"{self._max_duplicate_fraction} duplicate identifiers within groups "
            f"{self._group_columns} ({n_duplicates} / {n_denominator})."
            f"{example_string}"
        )
        return result, assertion_text

    def _result_details(self, value_factual, value_target) -> dict[str, Any]:
        n_duplicates, n_denominator, _ = value_factual
        return {
            "_n_violations": n_duplicates,
            "_n_denominator": n_denominator,
            "_denominator_description": (
                f"rows with a non-null identifier, grouped by {self._group_columns}"
            ),
        }
