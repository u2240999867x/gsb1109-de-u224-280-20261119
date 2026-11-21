from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import narwhals as nw
from narwhals.exceptions import (
    ColumnMappingConflictError,
    ColumnNotFoundError,
    DuplicateError,
)
from narwhals.plan import ColumnSource, NamePlan, PlannedFrame
from tests.utils import assert_equal_data

if TYPE_CHECKING:
    from tests.utils import ConstructorEager

READINGS_COLUMNS = ["reading_id", "device_id", "unit", "unit_right", "value"]
CATALOG_COLUMNS = ["device_key", "unit", "location"]
BATCHES_COLUMNS = ["device_key", "unit", "location", "value", "value_right"]

READINGS_DATA = {
    "reading_id": [1, 2, 3],
    "device_id": ["d1", "d2", "d3"],
    "unit": ["raw-a", "raw-b", "raw-c"],
    "unit_right": ["biz-a", "biz-b", "biz-c"],
    "value": [10, 20, 30],
}
CATALOG_DATA = {
    # `d2` matches twice, `d4` only exists in the catalog.
    "device_key": ["d2", "d2", "d4"],
    "unit": ["cat-b1", "cat-b2", "cat-d"],
    "location": ["site-1", "site-2", "site-4"],
}
BATCHES_DATA = {
    "device_key": ["d2", "d4"],
    "unit": ["batch-b", "batch-d"],
    "location": ["wh-1", "wh-4"],
    "value": [200, 400],
    "value_right": [2000, 4000],
}

STEP1_COLUMNS = [
    "reading_id",
    "device_id",
    "device_key",
    "reading_unit",
    "catalog_unit",
    "unit_right",
    "value",
]
FINAL_COLUMNS = [*STEP1_COLUMNS, "unit", "location", "batch_value", "value_right"]


def healthy_plan() -> tuple[NamePlan, PlannedFrame, PlannedFrame]:
    plan = NamePlan()
    plan.add_table("readings", READINGS_COLUMNS)
    plan.add_table("catalog", CATALOG_COLUMNS)
    plan.add_table("batches", BATCHES_COLUMNS)
    joined = plan.join(
        "readings",
        "catalog",
        how="full",
        left_on="device_id",
        right_on="device_key",
        rename_left={"unit": "reading_unit"},
        rename_right={"unit": "catalog_unit"},
    )
    selected = plan.select(joined, STEP1_COLUMNS)
    final = plan.join(
        selected,
        "batches",
        how="left",
        on="device_key",
        rename_right={"value": "batch_value"},
    )
    return plan, selected, final


def execute_report(constructor_eager: ConstructorEager) -> nw.DataFrame:
    readings = nw.from_native(constructor_eager(READINGS_DATA))
    catalog = nw.from_native(constructor_eager(CATALOG_DATA))
    batches = nw.from_native(constructor_eager(BATCHES_DATA))
    return (
        readings.rename({"unit": "reading_unit"})
        .join(
            catalog.rename({"unit": "catalog_unit"}),
            how="full",
            left_on="device_id",
            right_on="device_key",
            suffix="_right",
        )
        .select(STEP1_COLUMNS)
        .join(
            batches.rename({"value": "batch_value"}),
            how="left",
            on="device_key",
            suffix="_right",
        )
    )


def test_plan_sources_before_data() -> None:
    plan, selected, final = healthy_plan()
    assert final.columns == FINAL_COLUMNS
    assert plan.sources(final) == {
        "reading_id": ColumnSource("readings", "reading_id"),
        "device_id": ColumnSource("readings", "device_id"),
        "device_key": ColumnSource("catalog", "device_key"),
        "reading_unit": ColumnSource("readings", "unit"),
        "catalog_unit": ColumnSource("catalog", "unit"),
        "unit_right": ColumnSource("readings", "unit_right"),
        "value": ColumnSource("readings", "value"),
        "unit": ColumnSource("batches", "unit"),
        "location": ColumnSource("batches", "location"),
        "batch_value": ColumnSource("batches", "value"),
        "value_right": ColumnSource("batches", "value_right"),
    }
    # The name `unit` referred to `readings.unit` before the first join, and to
    # `batches.unit` after the second one; provenance follows the column, not
    # the display name.
    assert plan.source_of(final, "unit") == ColumnSource("batches", "unit")
    assert plan.source_of(selected, "reading_unit") == ColumnSource("readings", "unit")
    # `location` was dropped from the catalog in the first step's selection, so
    # the final `location` is the batches one, not a resurrected catalog column.
    assert "location" not in selected.columns
    assert plan.source_of(final, "location") == ColumnSource("batches", "location")
    with pytest.raises(ColumnNotFoundError, match="location"):
        plan.source_of(selected, "location")


def test_mapping_conflict_points_to_both_columns() -> None:
    plan, _, final = healthy_plan()
    with pytest.raises(ColumnMappingConflictError) as exc_info:
        plan.rename(final, {"reading_unit": "report_unit", "unit": "report_unit"})
    message = str(exc_info.value)
    assert "'report_unit'" in message
    assert "'readings'.'unit'" in message
    assert "'batches'.'unit'" in message


def test_default_suffix_conflict_with_real_column() -> None:
    # `readings.unit_right` is a real business column, so suffixing the
    # catalog's `unit` with the default suffix would collide with it.
    plan = NamePlan()
    plan.add_table("readings", READINGS_COLUMNS)
    plan.add_table("catalog", CATALOG_COLUMNS)
    with pytest.raises(DuplicateError, match="'unit_right'") as exc_info:
        plan.join(
            "readings", "catalog", how="full", left_on="device_id", right_on="device_key"
        )
    assert "'readings'.'unit_right'" in str(exc_info.value)
    assert "'catalog'.'unit'" in str(exc_info.value)


def test_default_suffix_conflict_with_right_side_column() -> None:
    # `batches.value_right` is a real column, so suffixing `batches.value`
    # with the default suffix would collide with it.
    plan, selected, _ = healthy_plan()
    with pytest.raises(DuplicateError, match="'value_right'") as exc_info:
        plan.join(selected, "batches", how="left", on="device_key")
    message = str(exc_info.value)
    assert "'batches'.'value'" in message
    assert "'batches'.'value_right'" in message


def test_suffix_only_join_matches_existing_behavior(
    constructor_eager: ConstructorEager,
) -> None:
    # A join passing only `suffix` resolves names exactly like `DataFrame.join`.
    left_data = {"id": [1, 2], "unit": ["a", "b"]}
    right_data = {"id": [1, 3], "unit": ["x", "y"], "location": ["s1", "s3"]}
    plan = NamePlan()
    plan.add_table("left", list(left_data))
    plan.add_table("right", list(right_data))
    planned = plan.join("left", "right", how="left", on="id", suffix="_right")
    result = nw.from_native(constructor_eager(left_data)).join(
        nw.from_native(constructor_eager(right_data)),
        how="left",
        on="id",
        suffix="_right",
    )
    assert planned.columns == result.columns
    assert planned.sources == {
        "id": ColumnSource("left", "id"),
        "unit": ColumnSource("left", "unit"),
        "unit_right": ColumnSource("right", "unit"),
        "location": ColumnSource("right", "location"),
    }


def test_full_join_keeps_both_differently_named_keys() -> None:
    plan = NamePlan()
    plan.add_table("readings", READINGS_COLUMNS)
    plan.add_table("catalog", CATALOG_COLUMNS)
    planned = plan.join(
        "readings",
        "catalog",
        how="full",
        left_on="device_id",
        right_on="device_key",
        rename_left={"unit": "reading_unit"},
        rename_right={"unit": "catalog_unit"},
    )
    assert planned.source_of("device_id") == ColumnSource("readings", "device_id")
    assert planned.source_of("device_key") == ColumnSource("catalog", "device_key")


def test_left_join_with_differently_named_keys_drops_right_key() -> None:
    plan = NamePlan()
    plan.add_table("readings", READINGS_COLUMNS)
    plan.add_table("catalog", CATALOG_COLUMNS)
    planned = plan.join(
        "readings",
        "catalog",
        how="left",
        left_on=["device_id"],
        right_on=["device_key"],
        rename_left={"unit": "reading_unit"},
        rename_right={"unit": "catalog_unit"},
    )
    assert "device_id" in planned.columns
    assert "device_key" not in planned.columns


def test_single_string_columns() -> None:
    plan = NamePlan()
    table = plan.add_table("readings", "device_id")
    assert table.columns == ["device_id"]
    selected = plan.select(table, "device_id")
    assert selected.columns == ["device_id"]


def test_join_on_list_of_keys() -> None:
    plan, selected, _ = healthy_plan()
    planned = plan.join(
        selected,
        "batches",
        how="left",
        on=["device_key"],
        rename_right={"value": "batch_value"},
    )
    assert planned.columns == FINAL_COLUMNS
    assert planned.source_of("device_key") == ColumnSource("catalog", "device_key")


def test_plan_validation() -> None:
    plan = NamePlan()
    plan.add_table("readings", READINGS_COLUMNS)
    plan.add_table("catalog", CATALOG_COLUMNS)
    with pytest.raises(ValueError, match="already registered"):
        plan.add_table("readings", READINGS_COLUMNS)
    with pytest.raises(DuplicateError, match="duplicate column names"):
        plan.add_table("bad", ["a", "a"])
    with pytest.raises(NotImplementedError, match="join strategies"):
        plan.join("readings", "catalog", how="cross")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="should be None"):
        plan.join("readings", "catalog", how="inner", on="device_id", left_on="device_id")
    with pytest.raises(ValueError, match="should be specified"):
        plan.join("readings", "catalog", how="inner", left_on="device_id")
    with pytest.raises(ValueError, match="same length"):
        plan.join(
            "readings",
            "catalog",
            how="inner",
            left_on=["device_id", "unit"],
            right_on="device_key",
        )
    with pytest.raises(ColumnNotFoundError, match="device_key"):
        plan.join("readings", "catalog", how="inner", on="device_key")
    with pytest.raises(ColumnNotFoundError, match="device_id"):
        plan.join("readings", "catalog", how="inner", on="device_id")
    with pytest.raises(ColumnNotFoundError, match="nope"):
        plan.join(
            "readings",
            "catalog",
            how="inner",
            on=None,
            left_on="device_id",
            right_on="device_key",
            rename_left={"nope": "x"},
        )
    with pytest.raises(ValueError, match="Unknown frame"):
        plan.columns("nope")
    other_plan = NamePlan()
    other_frame = other_plan.add_table("other", ["a"])
    with pytest.raises(ValueError, match="different `NamePlan`"):
        plan.columns(other_frame)
    with pytest.raises(ColumnNotFoundError, match="nope"):
        plan.select("readings", ["reading_id", "nope"])
    with pytest.raises(ColumnNotFoundError, match="nope"):
        plan.rename("readings", {"nope": "x"})
    with pytest.raises(ColumnMappingConflictError, match="mapped from more than one"):
        plan.rename("readings", {"unit": "value"})
    with pytest.raises(ColumnNotFoundError, match="nope"):
        plan.source_of("readings", "nope")


def test_step_handles_and_names() -> None:
    plan = NamePlan()
    readings = plan.add_table("readings", READINGS_COLUMNS)
    assert isinstance(readings, PlannedFrame)
    assert readings.name == "readings"
    assert readings.columns == READINGS_COLUMNS
    renamed = plan.rename(readings, {"unit": "reading_unit"}, name="renamed")
    assert renamed.name == "renamed"
    assert renamed.source_of("reading_unit") == ColumnSource("readings", "unit")
    auto = plan.select(renamed, ["reading_unit"])
    assert auto.name not in {"readings", "renamed"}
    assert auto.columns == ["reading_unit"]
    with pytest.raises(ValueError, match="already registered"):
        plan.select(renamed, ["reading_unit"], name="renamed")


def test_plan_matches_execution(constructor_eager: ConstructorEager) -> None:
    _, _, final = healthy_plan()
    result = execute_report(constructor_eager)
    # The names planned for both joins line up with the actual schema.
    assert result.columns == final.columns == FINAL_COLUMNS


def test_full_join_null_side_keys_and_catalog_only_rows(
    constructor_eager: ConstructorEager,
) -> None:
    result = execute_report(constructor_eager)
    # Catalog-only rows keep a null left-side key (it is not filled with the
    # catalog key) and still match batches via the catalog key `device_key`.
    catalog_only = result.filter(nw.col("device_id").is_null())
    assert_equal_data(
        catalog_only.select("device_key", "unit", "location", "batch_value"),
        {
            "device_key": ["d4"],
            "unit": ["batch-d"],
            "location": ["wh-4"],
            "batch_value": [400],
        },
    )
    # Left-only rows get nulls for every batches column.
    left_only = result.filter(nw.col("device_key").is_null()).sort("reading_id")
    assert_equal_data(
        left_only.select("reading_id", "unit", "batch_value"),
        {"reading_id": [1, 3], "unit": [None, None], "batch_value": [None, None]},
    )


def test_duplicate_matches_and_column_origins(
    constructor_eager: ConstructorEager,
) -> None:
    result = execute_report(constructor_eager)
    # The duplicated catalog match for `d2` produces two rows, and each final
    # column carries the values of the table it was planned to come from.
    matched = result.filter(nw.col("device_key") == "d2").sort("catalog_unit")
    assert_equal_data(
        matched.select(
            "reading_id",
            "reading_unit",
            "catalog_unit",
            "unit_right",
            "value",
            "unit",
            "location",
            "batch_value",
            "value_right",
        ),
        {
            "reading_id": [2, 2],
            "reading_unit": ["raw-b", "raw-b"],
            "catalog_unit": ["cat-b1", "cat-b2"],
            "unit_right": ["biz-b", "biz-b"],
            "value": [20, 20],
            "unit": ["batch-b", "batch-b"],
            "location": ["wh-1", "wh-1"],
            "batch_value": [200, 200],
            "value_right": [2000, 2000],
        },
    )
