from __future__ import annotations

from decimal import Decimal
from typing import TYPE_CHECKING, Any

import pytest

import narwhals as nw
from narwhals.convert import (
    ConversionResult,
    ConversionRule,
    LazyConversionResult,
    convert,
    convert_columns,
)
from narwhals.exceptions import ColumnNotFoundError, UnsupportedDTypeError
from tests.utils import Constructor, ConstructorEager, assert_equal_data

if TYPE_CHECKING:
    import pandas as pd

    from narwhals.typing import IntoDataFrame

pd = pytest.importorskip("pandas")

DATA = {
    "record": ["a", "b", "c", "d", "e", "f", "g", "h", "i"],
    "amount": ["127", "128", "-128", "1.234", "1.230", "oops", "999.99", "1000.00", None],
}


def _string_frame(constructor: Constructor, data: dict[str, Any]) -> IntoDataFrame:
    native = constructor(data)
    if isinstance(native, pd.DataFrame):
        native = native.astype({name: "string" for name in data})
    return native


def _materialize(result: Any) -> ConversionResult:
    if isinstance(result, LazyConversionResult):
        return result.collect()
    return result


def _by_record(result: ConversionResult) -> dict[str, list[Any]]:
    evidence = _materialize(result).failures.select("record", "column", "reason").to_dict(
        as_series=False
    )
    grouped: dict[str, list[Any]] = {}
    for record, column, reason in zip(
        evidence["record"], evidence["column"], evidence["reason"], strict=True
    ):
        grouped.setdefault(record, []).append((column, reason))
    return grouped


def test_convert_integer_exact_and_schema(constructor: Constructor) -> None:
    native = _string_frame(constructor, DATA)
    frame = nw.from_native(native)
    rule = ConversionRule({"amount": nw.Int8}, source_id="record")
    result = _materialize(frame.convert(rule))

    assert isinstance(result, ConversionResult)
    assert result.schema == nw.Schema({"record": nw.String, "amount": nw.Int8})
    assert result.values.collect_schema() == result.schema

    expected = {"record": DATA["record"], "amount": [127, None, -128, None, None, None, None, None, None]}
    assert_equal_data(result.values, expected)

    grouped = _by_record(result)
    assert grouped["b"] == [("amount", "out_of_range")]
    assert grouped["d"] == [("amount", "inexact")]
    assert grouped["e"] == [("amount", "inexact")]
    assert grouped["f"] == [("amount", "malformed_text")]
    assert grouped["g"] == [("amount", "out_of_range")]
    assert grouped["h"] == [("amount", "out_of_range")]
    assert "a" not in grouped
    assert "c" not in grouped
    assert "i" not in grouped


def test_convert_decimal_scale_and_bounds(constructor: Constructor) -> None:
    native = _string_frame(constructor, DATA)
    frame = nw.from_native(native)
    rule = ConversionRule({"amount": nw.Decimal(5, 2)}, source_id="record")
    result = _materialize(frame.convert(rule))

    assert result.schema == nw.Schema(
        {"record": nw.String, "amount": nw.Decimal(5, 2)}
    )
    values = result.values["amount"].to_list()
    assert values[0] == Decimal("127.00")
    assert values[1] == Decimal("128.00")
    assert values[2] == Decimal("-128.00")
    assert values[3] is None or pd.isna(values[3])
    assert values[4] == Decimal("1.23")
    assert values[6] == Decimal("999.99")

    grouped = _by_record(result)
    assert grouped["d"] == [("amount", "inexact")]
    assert grouped["f"] == [("amount", "malformed_text")]
    assert grouped["h"] == [("amount", "out_of_range")]
    assert "b" not in grouped


def test_convert_failure_evidence_shape(constructor: Constructor) -> None:
    native = _string_frame(constructor, DATA)
    frame = nw.from_native(native)
    result = _materialize(
        frame.convert(ConversionRule({"amount": nw.Int8}, source_id="record"))
    )
    assert result.failure_schema == nw.Schema(
        {
            "record": nw.String,
            "column": nw.String,
            "reason": nw.String,
            "target": nw.String,
            "value": nw.String,
        }
    )
    assert result.failures.collect_schema() == result.failure_schema


def test_convert_two_columns_same_row(constructor: Constructor) -> None:
    data = {
        "record": ["a", "b", "c"],
        "x": ["1", "1000", "2"],
        "y": ["1.234", "2", "oops"],
    }
    native = _string_frame(constructor, data)
    frame = nw.from_native(native)
    result = _materialize(
        frame.convert(
            ConversionRule(
                {"x": nw.Int8, "y": nw.Decimal(5, 2)}, source_id="record"
            )
        )
    )
    failures = result.failures.select("record", "column", "reason").to_dict(
        as_series=False
    )
    seen = {
        record: (column, reason)
        for record, column, reason in zip(
            failures["record"], failures["column"], failures["reason"], strict=True
        )
    }
    assert seen["a"] == ("y", "inexact")
    assert seen["b"] == ("x", "out_of_range")
    assert seen["c"] == ("y", "malformed_text")
    # The row whose two columns both fail must appear twice, once per column.
    data_both = {"record": ["a", "b"], "x": ["1000", "1"], "y": ["1.234", "2"]}
    both = _string_frame(constructor, data_both)
    both_result = _materialize(
        nw.from_native(both).convert(
            ConversionRule({"x": nw.Int8, "y": nw.Decimal(5, 2)}, source_id="record")
        )
    )
    both_failures = both_result.failures.select("record", "column").to_dict(
        as_series=False
    )
    assert both_failures["record"].count("a") == 2
    assert both_failures["column"].count("x") == 1


def test_convert_strict_false_rounds(constructor: Constructor) -> None:
    data = {"record": ["a", "b"], "amount": ["1.234", "1.230"]}
    native = _string_frame(constructor, data)
    frame = nw.from_native(native)
    result = _materialize(
        frame.convert(
            ConversionRule(
                {"amount": nw.Decimal(5, 2)}, source_id="record", strict=False
            )
        )
    )
    assert_equal_data(
        result.values,
        {"record": ["a", "b"], "amount": [Decimal("1.23"), Decimal("1.23")]},
    )
    assert result.failures.shape == (0, 5)


def test_convert_lazy_schema_without_collecting(constructor: Constructor) -> None:
    native = _string_frame(constructor, DATA)
    frame = nw.from_native(native)
    if not hasattr(frame, "collect"):
        pytest.skip("eager backend")
    lazy = frame.lazy()
    result = lazy.convert(
        ConversionRule({"amount": nw.Decimal(5, 2)}, source_id="record")
    )
    assert isinstance(result, LazyConversionResult)
    assert result.collect_schema() == result.schema
    assert result.schema == nw.Schema(
        {"record": nw.String, "amount": nw.Decimal(5, 2)}
    )
    collected = result.collect()
    assert collected.schema == result.schema
    assert collected.failures.shape[0] == 3


def test_convert_after_projection_alias_and_filter(
    constructor: Constructor,
) -> None:
    data = {
        "record": ["a", "b", "c", "d"],
        "amount": ["1.20", "1.234", "keep", "2.00"],
        "label": ["x", "no", "yes", "no"],
    }
    native = _string_frame(constructor, data)
    frame = nw.from_native(native)
    prepared = (
        frame.select(
            nw.col("record"),
            nw.col("amount").alias("clean_amount"),
            nw.col("label"),
        )
        .filter(nw.col("label") == "yes")
        .select("record", "clean_amount")
    )
    result = _materialize(
        prepared.convert(
            ConversionRule({"clean_amount": nw.Decimal(5, 2)}, source_id="record")
        )
    )
    assert_equal_data(
        result.values, {"record": ["c"], "clean_amount": [None]}
    )
    assert result.failures.select("record", "column").to_dict(
        as_series=False
    ) == {"record": ["c"], "column": ["clean_amount"]}


def test_convert_preserves_pandas_index_and_record_identity() -> None:
    pytest.importorskip("pandas")
    import pandas as pd

    native = pd.DataFrame(
        {
            "record": ["a", "b", "c"],
            "amount": pd.array(["1", "oops", "2"], dtype="string"),
        },
        index=[5, 5, 9],
    )
    result = _materialize(
        nw.from_native(native).convert(
            ConversionRule({"amount": nw.Int8}, source_id="record")
        )
    )
    assert result.values.to_native().index.tolist() == [5, 5, 9]
    assert_equal_data(
        {"amount": result.values["amount"].to_list()}, {"amount": [1, None, 2]}
    )
    failures = result.failures.select("record", "column", "reason").to_dict(
        as_series=False
    )
    assert failures == {
        "record": ["b"],
        "column": ["amount"],
        "reason": ["malformed_text"],
    }


def test_convert_pandas_object_column() -> None:
    pytest.importorskip("pandas")
    import pandas as pd

    native = pd.DataFrame(
        {"record": ["a", "b"], "amount": pd.Series(["1", "x"], dtype=object)}
    )
    result = _materialize(
        nw.from_native(native).convert(
            ConversionRule({"amount": nw.Int8}, source_id="record")
        )
    )
    assert_equal_data({"amount": result.values["amount"].to_list()}, {"amount": [1, None]})
    assert result.failures.shape[0] == 1


def test_convert_unsupported_dtype_is_plan_time(constructor: Constructor) -> None:
    native = _string_frame(constructor, DATA)
    frame = nw.from_native(native)
    with pytest.raises(UnsupportedDTypeError):
        frame.convert(ConversionRule({"amount": nw.Float64}, source_id="record"))
    with pytest.raises(UnsupportedDTypeError):
        frame.convert(ConversionRule({"amount": nw.String}, source_id="record"))


def test_convert_missing_column(constructor: Constructor) -> None:
    native = _string_frame(constructor, DATA)
    frame = nw.from_native(native)
    with pytest.raises(ColumnNotFoundError):
        frame.convert(ConversionRule({"nope": nw.Int8}, source_id="record"))
    with pytest.raises(ColumnNotFoundError):
        frame.convert(ConversionRule({"amount": nw.Int8}, source_id="nope"))


def test_convert_rule_validation() -> None:
    with pytest.raises(ValueError):
        ConversionRule({}, source_id="record")
    with pytest.raises(TypeError):
        ConversionRule({"a": "Int8"}, source_id="record")  # type: ignore[dict-item]
    with pytest.raises(UnsupportedDTypeError):
        ConversionRule({"a": nw.Decimal}, source_id="record")  # type: ignore[dict-item]
    with pytest.raises(ValueError):
        ConversionRule({"a": nw.Int8}, source_id="a")


def test_convert_group_by_consumes_confirmed_columns(
    constructor: Constructor,
) -> None:
    data = {"record": ["a", "b", "c", "d"], "amount": ["1", "2", "3", "oops"]}
    native = _string_frame(constructor, data)
    result = _materialize(
        nw.from_native(native).convert(
            ConversionRule({"amount": nw.Int8}, source_id="record")
        )
    )
    grouped = (
        result.values.filter(~nw.col("amount").is_null())
        .group_by("amount")
        .agg(nw.len().alias("n"))
        .sort("amount")
    )
    assert_equal_data(grouped, {"amount": [1, 2, 3], "n": [1, 1, 1]})


def test_convert_eager_result_types(constructor_eager: ConstructorEager) -> None:
    native = _string_frame(constructor_eager, DATA)
    frame = nw.from_native(native)
    result = frame.convert(ConversionRule({"amount": nw.Int8}, source_id="record"))
    assert isinstance(result, ConversionResult)


def test_convert_module_function(constructor: Constructor) -> None:
    native = _string_frame(constructor, DATA)
    frame = nw.from_native(native)
    rule = ConversionRule({"amount": nw.Int8}, source_id="record")
    result = _materialize(convert_columns(frame, rule))
    assert isinstance(result, ConversionResult)
    assert result.schema["amount"] == nw.Int8


def test_convert_binary_float_artifacts_are_inexact(constructor_eager: ConstructorEager) -> None:
    native = constructor_eager(
        {
            "record": ["a", "b", "c"],
            "amount": [0.1 + 0.2, 1.2300000000000002, 1.23],
        }
    )
    frame = nw.from_native(native)
    if frame.schema["amount"] == nw.Int8:  # pragma: no cover
        pytest.skip("unexpected integer inference")
    result = frame.convert(
        ConversionRule({"amount": nw.Decimal(20, 2)}, source_id="record")
    )
    assert isinstance(result, ConversionResult)
    failures = result.failures.select("record", "reason").to_dict(as_series=False)
    assert failures == {"record": ["a", "b"], "reason": ["inexact", "inexact"]}
    assert result.values["amount"].to_list()[2] == Decimal("1.23")


def test_convert_lazy_does_not_consume_before_collect() -> None:
    pytest.importorskip("polars")
    import polars as pl

    lazy = nw.from_native(pl.LazyFrame({"record": ["a"], "amount": ["1"]}))
    result = lazy.convert(
        ConversionRule({"amount": nw.Int8}, source_id="record")
    )
    assert isinstance(result, LazyConversionResult)
    assert result.schema["amount"] == nw.Int8
    assert result.failure_schema["reason"] == nw.String


def test_convert_dtype_preserved_with_pandas_index() -> None:
    pytest.importorskip("pandas")
    import pandas as pd

    native = pd.DataFrame(
        {"record": ["a", "b"], "amount": pd.array(["1", "x"], dtype="string")},
        index=[7, 7],
    )
    result = nw.from_native(native).convert(
        ConversionRule({"amount": nw.Decimal(5, 2)}, source_id="record")
    )
    assert result.values.schema["amount"] == nw.Decimal(5, 2)
    assert result.values.to_native().index.tolist() == [7, 7]


def test_convert_public_alias_matches() -> None:
    assert convert is convert_columns


@pytest.mark.parametrize(
    ("target", "valid_values"),
    [
        (nw.Int8, ["-128", "127"]),
        (nw.Int64, ["-9223372036854775808", "9223372036854775807"]),
        (nw.UInt64, ["0", "18446744073709551615"]),
    ],
)
def test_convert_integer_boundaries_preserve_exact_values(
    constructor: Constructor, target: Any, valid_values: list[str]
) -> None:
    lower, upper = int(valid_values[0]), int(valid_values[1])
    data = {
        "record": ["a", "b", "c", "d"],
        "v": [*valid_values, str(upper + 1), str(lower - 1)],
    }
    native = _string_frame(constructor, data)
    result = _materialize(
        nw.from_native(native).convert(
            ConversionRule({"v": target}, source_id="record")
        )
    )
    values = result.values["v"].to_list()
    assert values[0] == lower
    assert values[1] == upper
    assert values[2] is None or pd.isna(values[2])
    assert values[3] is None or pd.isna(values[3])
    assert result.failures.select("record", "reason").to_dict(as_series=False) == {
        "record": ["c", "d"],
        "reason": ["out_of_range", "out_of_range"],
    }


def test_convert_strict_false_integer_rounds(constructor: Constructor) -> None:
    data = {"record": ["a", "b"], "v": ["1.6", "1.234"]}
    native = _string_frame(constructor, data)
    result = _materialize(
        nw.from_native(native).convert(
            ConversionRule({"v": nw.Int8}, source_id="record", strict=False)
        )
    )
    assert_equal_data(result.values, {"record": ["a", "b"], "v": [2, 1]})
    assert result.failures.shape == (0, 5)
