from collections import Counter

import pytest

from datajudge.constraints.nrows import NRowsMaxLoss
from datajudge.data_source import TableDataSource
from datajudge.db_access import DataReference
from datajudge.requirements import BetweenRequirement


def _build_constraint(group_columns1=None, group_columns2=None, max_relative_loss=0.5):
    ref = DataReference(
        TableDataSource("example", "table1", "schema"), columns=group_columns1
    )
    ref2 = DataReference(
        TableDataSource("example", "table2", "schema"), columns=group_columns2
    )
    constraint = NRowsMaxLoss(ref, ref2, lambda engine: max_relative_loss)
    constraint.max_relative_loss = max_relative_loss
    return constraint


def test_ungrouped_mode_is_used_without_group_columns():
    constraint = _build_constraint()
    assert constraint._grouped is False
    assert constraint._compare(90, 100)[0] is True
    assert constraint._compare(40, 100)[0] is False


def test_grouped_mode_detects_cleared_brand_masked_by_overall_pass():
    # The baseline holds 200 rows, the current table 190 rows, i.e. the total
    # loss is merely 5%. Yet brand "b" disappeared completely and must fail.
    constraint = _build_constraint(["brand"], ["brand"], max_relative_loss=0.5)
    target_counts = Counter({"a": 190, "b": 10})
    factual_counts = Counter({"a": 190})
    result, message = constraint._compare(factual_counts, target_counts)
    assert result is False
    assert "'b'" in message
    assert "100.000000%" in message


def test_grouped_mode_passes_when_every_brand_is_within_tolerance():
    constraint = _build_constraint(["brand"], ["brand"], max_relative_loss=0.5)
    target_counts = Counter({"a": 100, "b": 100})
    factual_counts = Counter({"a": 90, "b": 80})
    result, _ = constraint._compare(factual_counts, target_counts)
    assert result is True


def test_vanished_group_is_counted_as_full_loss():
    target_counts = Counter({"a": 100, "b": 100})
    factual_counts = Counter({"a": 100})

    tolerant = _build_constraint(["brand"], ["brand"], max_relative_loss=1.0)
    result, message = tolerant._compare(factual_counts, target_counts)
    assert result is True
    assert "Groups that vanished" in message
    assert "'b'" in message
    assert "50.000000%" in message

    strict = _build_constraint(["brand"], ["brand"], max_relative_loss=0.99)
    result, message = strict._compare(factual_counts, target_counts)
    assert result is False
    assert "'b': 100 -> 0 rows (loss 100.000000%)" in message


def test_new_group_without_baseline_is_listed_separately():
    constraint = _build_constraint(["brand"], ["brand"], max_relative_loss=0.5)
    target_counts = Counter({"a": 100})
    factual_counts = Counter({"a": 100, "new_brand": 7})
    result, message = constraint._compare(factual_counts, target_counts)
    assert result is True
    assert "without a historical baseline" in message
    assert "'new_brand': 7 rows" in message
    assert "Mean relative loss over 1 group" in message


def test_zero_baseline_group_does_not_enter_percentage_average():
    constraint = _build_constraint(["brand"], ["brand"], max_relative_loss=0.6)
    target_counts = Counter({"a": 100, "z": 0})
    factual_counts = Counter({"a": 50, "z": 9})
    result, message = constraint._compare(factual_counts, target_counts)
    assert result is True
    assert "Mean relative loss over 1 group" in message
    assert "'z': 9 rows" in message


def test_group_growth_is_clamped_to_zero_loss():
    constraint = _build_constraint(["brand"], ["brand"], max_relative_loss=0.6)
    target_counts = Counter({"a": 100, "b": 100})
    factual_counts = Counter({"a": 200, "b": 40})
    result, message = constraint._compare(factual_counts, target_counts)
    assert result is True
    assert "30.000000%" in message

    factual_counts = Counter({"a": 200, "b": 30})
    result, message = constraint._compare(factual_counts, target_counts)
    assert result is False
    assert "'b': 100 -> 30 rows (loss 70.000000%)" in message


def test_multi_column_groups_are_supported():
    constraint = _build_constraint(
        ["brand", "region"], ["brand", "region"], max_relative_loss=0.5
    )
    target_counts = Counter({("a", "north"): 10, ("b", "south"): 10})
    factual_counts = Counter({("a", "north"): 10})
    result, message = constraint._compare(factual_counts, target_counts)
    assert result is False
    assert "('b', 'south')" in message


def test_group_columns_required_on_both_references():
    with pytest.raises(ValueError, match="both DataReferences"):
        _build_constraint(group_columns1=["brand"], group_columns2=None)
    with pytest.raises(ValueError, match="both DataReferences"):
        _build_constraint(group_columns1=None, group_columns2=["brand"])


def test_requirement_builds_grouped_constraint():
    requirement = BetweenRequirement.from_tables(
        "example", "schema1", "table1", "example", "schema2", "table2"
    )
    requirement.add_n_rows_max_loss_constraint(
        constant_max_relative_loss=0.5,
        group_columns1=["brand"],
        group_columns2=["brand"],
    )
    constraint = requirement[0]
    assert isinstance(constraint, NRowsMaxLoss)
    assert constraint._grouped is True
    assert constraint._ref.columns == ["brand"]
    assert constraint._ref2.columns == ["brand"]


def test_requirement_without_group_columns_keeps_total_count_mode():
    requirement = BetweenRequirement.from_tables(
        "example", "schema1", "table1", "example", "schema2", "table2"
    )
    requirement.add_n_rows_max_loss_constraint(constant_max_relative_loss=0.5)
    assert requirement[0]._grouped is False


def test_requirement_rejects_group_columns_on_single_side():
    requirement = BetweenRequirement.from_tables(
        "example", "schema1", "table1", "example", "schema2", "table2"
    )
    with pytest.raises(ValueError, match="both DataReferences"):
        requirement.add_n_rows_max_loss_constraint(
            constant_max_relative_loss=0.5, group_columns1=["brand"]
        )
