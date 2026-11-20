from __future__ import annotations

import abc
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cache
from typing import Any

import sqlalchemy as sa

from .. import db_access
from .._engines import is_snowflake
from ..db_access import DataReference, _MatchAndCompare
from .base import Constraint, TestResult, _format_sample, _ToleranceGetter


@dataclass(frozen=True)
class RowDifferenceSample:
    """A row value whose multiplicity differs between two data sources.

    ``values`` holds the row value as returned by the database. ``count1`` and
    ``count2`` are the numbers of occurrences in the first and second data
    source, respectively.
    """

    values: tuple[Any, ...]
    count1: int
    count2: int

    def format(self, columns: Sequence[str], partition_indices: Sequence[int]) -> str:
        index_set = set(partition_indices)
        value_string = " , ".join(
            f"{column} = {value}" for column, value in zip(columns, self.values)
        )
        partition_string = " , ".join(
            f"{columns[index]} = {self.values[index]}" for index in partition_indices
        )
        counts_string = f"count1 = {self.count1}, count2 = {self.count2}"
        if not index_set:
            return f"{value_string} ({counts_string})"
        return f"partition [{partition_string}]: {value_string} ({counts_string})"


@dataclass(frozen=True)
class _PartitionDifference:
    """Aggregated multiplicity difference within one partition."""

    partition: tuple[Any, ...]
    missing: int
    excess: int


def _row_columns(ref: DataReference, engine: sa.engine.Engine) -> list[str]:
    columns = ref.get_columns(engine)
    if columns is not None:
        return columns
    return [column.name for column in ref.get_selection(engine).selected_columns]


def _sample_from_row(row: Sequence[Any], n_columns: int) -> RowDifferenceSample:
    return RowDifferenceSample(
        values=tuple(row[:n_columns]),
        count1=int(row[n_columns]),
        count2=int(row[n_columns + 1]),
    )


class Row(Constraint, abc.ABC):
    def __init__(
        self,
        ref: DataReference,
        ref2: DataReference,
        max_missing_fraction_getter: _ToleranceGetter,
        name: str | None = None,
        cache_size=None,
    ):
        super().__init__(ref, ref2=ref2, name=name, cache_size=cache_size)
        self._max_missing_fraction_getter = max_missing_fraction_getter

    def test(self, engine: sa.engine.Engine) -> TestResult:
        if self._ref is None or self._ref2 is None:
            raise ValueError()
        self._max_missing_fraction = self._max_missing_fraction_getter(engine)
        self._ref1_minus_ref2_sample, _ = db_access.get_row_difference_sample(
            engine, self._ref, self._ref2
        )
        self._ref2_minus_ref1_sample, _ = db_access.get_row_difference_sample(
            engine, self._ref2, self._ref
        )
        return super().test(engine)


class RowEquality(Row):
    """Compare two row multisets, optionally partitioned by grouping columns.

    By default, the multiplicity of each row value is taken into account, so a
    value occurring three times in the first data source and twice in the
    second one contributes one missing occurrence. Set ``compare_distinct`` to
    only compare the deduplicated row sets.

    If ``partition_columns1`` and ``partition_columns2`` are given, the
    multiplicity comparison is carried out per partition. Each counterexample
    therefore identifies its partition values together with the row values and
    both multiplicities.

    All counts and all samples of one test invocation are read from a single
    database snapshot, which is locked in at the beginning of the comparison.
    Changes committed afterwards are only visible to a subsequent test
    invocation.
    """

    def __init__(
        self,
        ref: DataReference,
        ref2: DataReference,
        max_missing_fraction_getter: _ToleranceGetter,
        name: str | None = None,
        cache_size=None,
        *,
        partition_columns1: Sequence[str] | None = None,
        partition_columns2: Sequence[str] | None = None,
        compare_distinct: bool = False,
        n_counterexamples: int = 5,
    ):
        super().__init__(
            ref,
            ref2,
            max_missing_fraction_getter,
            name=name,
            cache_size=cache_size,
        )
        partition_columns1 = list(partition_columns1 or [])
        partition_columns2 = list(partition_columns2 or [])
        if len(partition_columns1) != len(partition_columns2):
            raise ValueError(
                "partition_columns1 and partition_columns2 must have the same length."
            )
        if n_counterexamples < -1:
            raise ValueError(
                f"Unexpected number of counterexamples: {n_counterexamples}"
            )
        self._partition_columns1 = partition_columns1
        self._partition_columns2 = partition_columns2
        self._compare_distinct = compare_distinct
        self._n_counterexamples = n_counterexamples
        self._counts: tuple[int, int, int] | None = None
        self._partitions: tuple[_PartitionDifference, ...] = ()
        self._missing_samples: tuple[RowDifferenceSample, ...] = ()
        self._excess_samples: tuple[RowDifferenceSample, ...] = ()
        self._columns: list[str] = []
        self._partition_indices1: list[int] = []
        self._partition_indices2: list[int] = []

    def _resolve_columns(
        self, engine: sa.engine.Engine, ref: DataReference, partition_columns: list[str]
    ) -> tuple[list[str], list[int]]:
        columns = _row_columns(ref, engine)
        indices = []
        for column in partition_columns:
            if is_snowflake(engine):
                column = db_access.lowercase_column_names(column)
            if column not in columns:
                raise ValueError(
                    f"Partition column {column!r} is not selected from {ref}."
                )
            indices.append(columns.index(column))
        return columns, indices

    def test(self, engine: sa.engine.Engine) -> TestResult:
        if self._ref is None or self._ref2 is None:
            raise ValueError()
        self._max_missing_fraction = self._max_missing_fraction_getter(engine)
        self._columns, self._partition_indices1 = self._resolve_columns(
            engine, self._ref, self._partition_columns1
        )
        _, self._partition_indices2 = self._resolve_columns(
            engine, self._ref2, self._partition_columns2
        )

        with db_access.read_snapshot(engine) as connection:
            self._counts, count_selections = db_access.get_row_difference_counts(
                connection,
                self._ref,
                self._ref2,
                distinct=self._compare_distinct,
                allow_dirty_reads=False,
            )
            partition_counts, partition_selections = (
                db_access.get_partition_difference_counts(
                    connection,
                    self._ref,
                    self._ref2,
                    partition_indices=self._partition_indices1,
                    distinct=self._compare_distinct,
                    allow_dirty_reads=False,
                )
            )
            (missing_rows, excess_rows), sample_selections = (
                db_access.get_row_difference_count_samples(
                    connection,
                    self._ref,
                    self._ref2,
                    n_counterexamples=self._n_counterexamples,
                    distinct=self._compare_distinct,
                    allow_dirty_reads=False,
                )
            )
            self._factual_selections = [
                *count_selections,
                *partition_selections,
                *sample_selections,
            ]

        self._partitions = tuple(
            _PartitionDifference(
                partition=tuple(row[: len(self._partition_indices1)]),
                missing=int(row[len(self._partition_indices1)]),
                excess=int(row[len(self._partition_indices1) + 1]),
            )
            for row in partition_counts
        )
        self._missing_samples = tuple(
            _sample_from_row(row, len(self._columns)) for row in missing_rows
        )
        self._excess_samples = tuple(
            _sample_from_row(row, len(self._columns)) for row in excess_rows
        )

        n_missing, n_excess, n_union = self._counts
        is_success, assertion_message = self._compare((n_missing, n_excess), n_union)
        if is_success:
            return TestResult.success()
        factual_queries = [
            str(selection.compile(engine, compile_kwargs={"literal_binds": True}))
            for selection in self._factual_selections
        ]
        return TestResult.failure(
            assertion_message,
            self.get_description(),
            factual_queries,
            None,
        )

    def _compare(
        self,
        n_rows_missing_tuple: tuple[int, int],
        n_rows_total: int,
    ) -> tuple[bool, str | None]:
        n_rows_missing_left, n_rows_missing_right = n_rows_missing_tuple
        missing_fraction = (
            0.0
            if n_rows_total == 0
            else (n_rows_missing_left + n_rows_missing_right) / n_rows_total
        )
        result = missing_fraction <= self._max_missing_fraction
        if result:
            return result, None
        partition_string = self._format_partition_differences()
        return result, (
            f"{missing_fraction} > "
            f"{self._max_missing_fraction} of rows differ "
            f"between {self._ref} and "
            f"{self._ref2}: {n_rows_missing_left} row occurrence(s) are missing "
            f"in {self._ref2} and {n_rows_missing_right} are in excess "
            f"(multiset union size {n_rows_total}). "
            f"Per partition: {partition_string}. "
            f"E.g. for missing rows: {self._format_samples(self._missing_samples)}. "
            f"E.g. for excess rows: {self._format_samples(self._excess_samples)}."
        )

    def _format_samples(self, samples: Sequence[RowDifferenceSample]) -> str:
        if not samples:
            return "none"
        return " ; ".join(
            sample.format(self._columns, self._partition_indices1) for sample in samples
        )

    @property
    def missing_count(self) -> int | None:
        """Occurrences present in the first data source but missing in the second."""
        return None if self._counts is None else self._counts[0]

    @property
    def excess_count(self) -> int | None:
        """Occurrences present in the second data source but missing in the first."""
        return None if self._counts is None else self._counts[1]

    @property
    def union_size(self) -> int | None:
        """Size of the multiset union used as denominator of the difference."""
        return None if self._counts is None else self._counts[2]

    @property
    def partition_differences(self) -> tuple[_PartitionDifference, ...]:
        """Per-partition missing and excess occurrence counts."""
        return self._partitions

    @property
    def missing_samples(self) -> tuple[RowDifferenceSample, ...]:
        """Counterexamples with occurrences only in the first data source."""
        return self._missing_samples

    @property
    def excess_samples(self) -> tuple[RowDifferenceSample, ...]:
        """Counterexamples with occurrences only in the second data source."""
        return self._excess_samples

    def _format_partition_differences(self) -> str:
        if not self._partitions:
            return "none"
        return " ; ".join(
            "["
            + " , ".join(
                f"{column} = {value}"
                for column, value in zip(self._partition_columns1, partition.partition)
            )
            + f"]: missing {partition.missing}, excess {partition.excess}"
            for partition in self._partitions
        )


class RowSubset(Row):
    @cache
    def _get_factual_value(self, engine: sa.engine.Engine) -> int:
        if self._ref is None or self._ref2 is None:
            raise ValueError()
        n_rows_missing, selections = db_access.get_row_difference_count(
            engine,
            self._ref,
            self._ref2,
        )
        self.factual_selections = selections
        return n_rows_missing

    @cache
    def _get_target_value(self, engine: sa.engine.Engine) -> int:
        n_rows_total, selections = db_access.get_unique_count(engine, self._ref)
        self.target_selections = selections
        return n_rows_total

    @cache
    def _compare(
        self,
        n_rows_missing: int,
        n_rows_total: int,
    ) -> tuple[bool, str | None]:
        if n_rows_total == 0:
            return True, None
        missing_fraction = n_rows_missing / n_rows_total
        result = missing_fraction <= self._max_missing_fraction
        if result:
            return result, None
        sample_string = _format_sample(self._ref1_minus_ref2_sample, self._ref)
        assertion_message = (
            f"{missing_fraction} > "
            f"{self._max_missing_fraction} of rows of "
            f"{self._ref} are "
            f"not in {self._ref2}. E.g. for "
            f"{sample_string}. "
            f"{self._condition_string} "
        )
        return result, assertion_message


class RowSuperset(Row):
    def _get_factual_value(self, engine: sa.engine.Engine) -> int:
        if self._ref is None or self._ref2 is None:
            raise ValueError()
        n_rows_missing, selections = db_access.get_row_difference_count(
            engine, self._ref2, self._ref
        )
        self._factual_selections = selections
        return n_rows_missing

    def _get_target_value(self, engine: sa.engine.Engine) -> int:
        if self._ref is None or self._ref2 is None:
            raise ValueError()
        n_rows_total, selections = db_access.get_unique_count(engine, self._ref2)
        self._target_selections = selections
        return n_rows_total

    def _compare(
        # We are abusing the _compare method's interface here. Rather than receiving
        # factual and target values, we get missing and overall values.
        self,
        n_rows_missing: int,
        n_rows_total: int,
    ) -> tuple[bool, str | None]:  # type: ignore[invalid-method-override]
        if n_rows_total == 0:
            return True, None
        missing_fraction = n_rows_missing / n_rows_total
        result = missing_fraction <= self._max_missing_fraction
        if result:
            return result, None
        if self._ref2 is None:
            raise ValueError("RowSuperset constraint requires ref2.")
        sample_string = _format_sample(self._ref2_minus_ref1_sample, self._ref2)
        assertion_message = (
            f"{missing_fraction} > "
            f"{self._max_missing_fraction} of rows of "
            f"{self._ref2} are "
            f"not in {self._ref}. E.g. for "
            f"{sample_string}. "
            f"{self._condition_string} "
        )
        return result, assertion_message


class RowMatchingEquality(Row):
    def __init__(
        self,
        ref: DataReference,
        ref2: DataReference,
        matching_columns1: list[str],
        matching_columns2: list[str],
        comparison_columns1: list[str],
        comparison_columns2: list[str],
        max_missing_fraction_getter: _ToleranceGetter,
        name: str | None = None,
        cache_size=None,
    ):
        super().__init__(
            ref,
            ref2=ref2,
            max_missing_fraction_getter=max_missing_fraction_getter,
            name=name,
            cache_size=cache_size,
        )
        self._match_and_compare = _MatchAndCompare(
            matching_columns1,
            matching_columns2,
            comparison_columns1,
            comparison_columns2,
        )

    def test(self, engine: sa.engine.Engine) -> TestResult:
        if self._ref is None or self._ref2 is None:
            raise ValueError()
        missing_fraction, n_rows_match, selections = db_access.get_row_mismatch(
            engine, self._ref, self._ref2, self._match_and_compare
        )
        self.factual_selections = selections
        max_missing_fraction = self._max_missing_fraction_getter(engine)
        result = missing_fraction <= max_missing_fraction
        if result:
            return TestResult.success()
        assertion_message = (
            f"{missing_fraction} > "
            f"{max_missing_fraction} of the rows differ "
            f"on a match of {n_rows_match} rows between {self._ref} and "
            f"{self._ref2}. "
            f"{self._condition_string}"
            f"{self._match_and_compare} "
        )
        return TestResult.failure(assertion_message)
