from __future__ import annotations

import abc
from collections import Counter
from collections.abc import Iterable
from typing import Any

import sqlalchemy as sa

from .. import db_access
from ..db_access import DataReference
from ..utils import format_difference
from .base import Constraint, TestResult, _OptionalSelections, _ToleranceGetter


class NRows(Constraint, abc.ABC):
    def __init__(
        self,
        ref: DataReference,
        *,
        ref2: DataReference | None = None,
        n_rows: int | None = None,
        name: str | None = None,
        cache_size=None,
    ):
        super().__init__(
            ref,
            ref2=ref2,
            ref_value=n_rows,
            name=name,
            cache_size=cache_size,
        )

    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[int, _OptionalSelections]:
        return db_access.get_row_count(engine, ref)


class NRowsMin(NRows):
    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[int, _OptionalSelections]:
        # Explicitly set a row_limit since we only care about the binary outcome
        # "are there enough rows" and not the actual value. This speeds up queries
        # substantially and allows for dealing with tables with more rows than the
        # size of an int.
        # Mssql can deal with this by using `COUNT_BIG` instead of `COUNT`. The
        # former is unfortunately not available via sqlalchemy as of now.
        # Other db systems/dialects seem to deal with this problem by casting to
        # bigint, which we do by default.
        return db_access.get_row_count(
            engine=engine, ref=ref, row_limit=self._ref_value
        )

    def _compare(self, value_factual: int, value_target: int) -> tuple[bool, str]:
        result = value_factual >= value_target
        assertion_text = (
            f"{self._ref} has {value_factual} "
            f"< {self._target_prefix} {value_target} rows. "
            f"{self._condition_string}"
        )
        return result, assertion_text


class NRowsMax(NRows):
    def _compare(self, value_factual: int, value_target: int) -> tuple[bool, str]:
        result = value_factual <= value_target
        value_factual_fmt, value_target_fmt = format_difference(
            value_factual, value_target
        )
        assertion_text = (
            f"{self._ref} has {value_factual_fmt} "
            f"> {self._target_prefix} {value_target_fmt} rows. "
            f"{self._condition_string}"
        )
        return result, assertion_text


class NRowsEquality(NRows):
    def _compare(self, value_factual: int, value_target: int) -> tuple[bool, str]:
        result = value_factual == value_target
        value_factual_fmt, value_target_fmt = format_difference(
            value_factual, value_target
        )
        assertion_text = (
            f"{self._ref} has {value_factual_fmt} row(s) "
            f"instead of {self._target_prefix} {value_target_fmt}. "
            f"{self._condition_string}"
        )
        return result, assertion_text


class NRowsMaxLoss(NRows):
    def __init__(
        self,
        ref: DataReference,
        ref2: DataReference,
        max_relative_loss_getter: _ToleranceGetter,
        name: str | None = None,
        cache_size=None,
    ):
        super().__init__(ref, ref2=ref2, name=name, cache_size=cache_size)
        self.max_relative_loss_getter = max_relative_loss_getter
        grouped = ref.columns is not None or ref2.columns is not None
        if grouped and (ref.columns is None or ref2.columns is None):
            raise ValueError(
                "Grouped NRowsMaxLoss requires group columns on both DataReferences."
            )
        self._grouped = grouped

    def _retrieve(
        self, engine: sa.engine.Engine, ref: DataReference
    ) -> tuple[Any, _OptionalSelections]:
        if ref.columns is None:
            return db_access.get_row_count(engine, ref)
        return db_access.get_uniques(engine, ref)

    def _compare(self, value_factual: Any, value_target: Any) -> tuple[bool, str]:
        if self._grouped:
            return self._compare_grouped(value_factual, value_target)
        if value_target == 0:
            return True, "Empty target table."
        if value_factual > value_target:
            return True, "Row gain."
        relative_loss = (value_target - value_factual) / value_target
        assertion_text = (
            f"The #rows from {self._ref} have decreased by "
            f"{relative_loss:%} compared to table {self._ref2}. "
            f"They were expected to decrease by at most {self.max_relative_loss:%}. "
            f"{self._condition_string}"
        )
        result = relative_loss <= self.max_relative_loss
        return result, assertion_text

    def _compare_grouped(
        self, factual_counts: Counter, target_counts: Counter
    ) -> tuple[bool, str]:
        factual_groups = set(factual_counts)
        target_groups = set(target_counts)

        # Groups that only exist in the current table have no historical
        # baseline. They are reported separately and never enter the
        # percentage average.
        baseless_groups = self._sort_groups(factual_groups - target_groups)

        # Groups that disappeared from the current table are counted as a
        # full (100%) loss of their historical row count.
        vanished_groups = self._sort_groups(target_groups - factual_groups)

        # Relative losses are only defined for groups with a non-zero
        # historical baseline, i.e. zero-baseline groups are excluded.
        relative_losses: dict[Any, float] = {}
        for group in vanished_groups:
            relative_losses[group] = 1.0
        for group in target_groups & factual_groups:
            baseline = target_counts[group]
            if baseline == 0:
                baseless_groups.append(group)
                continue
            current = factual_counts[group]
            relative_losses[group] = max(0.0, (baseline - current) / baseline)

        violating_groups = self._sort_groups(
            group
            for group, loss in relative_losses.items()
            if loss > self.max_relative_loss
        )

        if relative_losses:
            mean_relative_loss = sum(relative_losses.values()) / len(relative_losses)
            mean_loss_text = (
                f"Mean relative loss over {len(relative_losses)} group(s) with a "
                f"historical baseline: {mean_relative_loss:%} "
                "(groups without a historical baseline are excluded). "
            )
        else:
            mean_loss_text = "No group with a historical baseline could be evaluated. "

        assertion_text = (
            f"The #rows per group from {self._ref} were compared to {self._ref2}. "
            f"Each group was expected to decrease by at most "
            f"{self.max_relative_loss:%}. "
            f"{mean_loss_text}"
        )
        if violating_groups:
            assertion_text += (
                "Groups exceeding the maximal relative loss "
                "(group: target rows -> current rows, relative loss): "
            )
            assertion_text += "; ".join(
                self._format_group_loss(
                    group,
                    target_counts.get(group, 0),
                    factual_counts.get(group, 0),
                    relative_losses[group],
                )
                for group in violating_groups
            )
            assertion_text += ". "
        if vanished_groups and not violating_groups:
            assertion_text += (
                "Groups that vanished from the current table but were within "
                "the tolerated loss: "
                f"{[repr(group) for group in vanished_groups]}. "
            )
        if baseless_groups:
            assertion_text += (
                "Groups without a historical baseline, thus not evaluated "
                "(group: current rows): "
            )
            assertion_text += "; ".join(
                f"{group!r}: {factual_counts[group]} rows" for group in baseless_groups
            )
            assertion_text += ". "
        assertion_text += self._condition_string
        return len(violating_groups) == 0, assertion_text

    @staticmethod
    def _sort_groups(groups: Iterable[Any]) -> list[Any]:
        return sorted(groups, key=repr)

    @staticmethod
    def _format_group_loss(
        group: Any, target_count: int, factual_count: int, relative_loss: float
    ) -> str:
        return (
            f"{group!r}: {target_count} -> {factual_count} rows "
            f"(loss {relative_loss:%})"
        )

    def test(self, engine: sa.engine.Engine) -> TestResult:
        self.max_relative_loss = self.max_relative_loss_getter(engine)
        return super().test(engine)


class NRowsMaxGain(NRows):
    def __init__(
        self,
        ref: DataReference,
        ref2: DataReference,
        max_relative_gain_getter: _ToleranceGetter,
        name: str | None = None,
        cache_size=None,
    ):
        super().__init__(ref, ref2=ref2, name=name, cache_size=cache_size)
        self._max_relative_gain_getter = max_relative_gain_getter

    def _compare(self, value_factual: int, value_target: int) -> tuple[bool, str]:
        if value_target == 0:
            return True, "Empty target table."
        if value_factual < value_target:
            return True, "Row loss."
        relative_gain = (value_factual - value_target) / value_target
        assertion_text = (
            f"{self._ref} has {relative_gain:%} gain in #rows compared to "
            f"{self._ref2}. It was only allowed "
            f"to increase by {self._max_relative_gain:%}. "
            f"{self._condition_string}"
        )
        result = relative_gain <= self._max_relative_gain
        return result, assertion_text

    def test(self, engine: sa.engine.Engine) -> TestResult:
        self._max_relative_gain = self._max_relative_gain_getter(engine)
        return super().test(engine)


class NRowsMinGain(NRows):
    def __init__(
        self,
        ref: DataReference,
        ref2: DataReference,
        min_relative_gain_getter: _ToleranceGetter,
        name: str | None = None,
        cache_size=None,
    ):
        super().__init__(ref, ref2=ref2, name=name, cache_size=cache_size)
        self._min_relative_gain_getter = min_relative_gain_getter

    def _compare(self, value_factual: int, value_target: int) -> tuple[bool, str]:
        if value_target == 0:
            return True, "Empty target table."
        if value_factual < value_target:
            return False, "Row loss."
        relative_gain = (value_factual - value_target) / value_target
        assertion_text = (
            f"{self._ref} has {relative_gain:%} gain in #rows compared to "
            f"{self._ref2}. It was supposed "
            f"to increase at least by {self._min_relative_gain:%}. "
            f"{self._condition_string}"
        )
        result = relative_gain >= self._min_relative_gain
        return result, assertion_text

    def test(self, engine: sa.engine.Engine) -> TestResult:
        self._min_relative_gain = self._min_relative_gain_getter(engine)
        return super().test(engine)
