from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

import narwhals as nw
from narwhals import billing as nb


def _frame(
    backend_name: str, data: dict[str, Any], types: dict[str, Any]
) -> nw.DataFrame[Any]:
    if backend_name == "pyarrow":
        import pyarrow as pa

        return nw.from_native(
            pa.table(
                {name: pa.array(values, types[name]) for name, values in data.items()}
            )
        )
    import polars as pl

    return nw.from_native(pl.DataFrame(data, schema=types))


@pytest.fixture(params=["pyarrow", "polars"])
def backend_name(request: pytest.FixtureRequest) -> str:
    return request.param


def test_integer_money_column_is_widened_losslessly(backend_name: str) -> None:
    contract = nb.BatchContract(
        "b", [nb.FieldContract("f_amount", "amount", nb.CurrencyDeclaration("USD", 2))]
    )
    if backend_name == "pyarrow":
        import pyarrow as pa

        frame = _frame(backend_name, {"amount": [123, 456, None]}, {"amount": pa.int64()})
    else:
        import polars as pl

        frame = _frame(backend_name, {"amount": [123, 456, None]}, {"amount": pl.Int64()})
    result = nb.expand(frame, contract)
    assert result[nb.AMOUNT_COLUMN].to_list() == [
        Decimal("123.00"),
        Decimal("456.00"),
        None,
    ]
    assert result[nb.PRESENCE_COLUMN].to_list() == [
        nb.PRESENCE_PRESENT,
        nb.PRESENCE_PRESENT,
        nb.PRESENCE_EXPLICIT_NULL,
    ]


def test_all_null_physical_column_is_treated_as_explicit_null(backend_name: str) -> None:
    contract = nb.BatchContract(
        "b", [nb.FieldContract("f_fee", "fee", nb.CurrencyDeclaration("USD", 2))]
    )
    if backend_name == "pyarrow":
        import pyarrow as pa

        frame = _frame(backend_name, {"fee": [None, None]}, {"fee": pa.null()})
    else:
        import polars as pl

        frame = _frame(backend_name, {"fee": [None, None]}, {"fee": pl.Null})
    result = nb.expand(frame, contract)
    assert result[nb.PRESENCE_COLUMN].to_list() == [
        nb.PRESENCE_EXPLICIT_NULL,
        nb.PRESENCE_EXPLICIT_NULL,
    ]
    assert result[nb.AMOUNT_COLUMN].to_list() == [None, None]


def test_wider_physical_scale_without_offending_digits_passes(backend_name: str) -> None:
    contract = nb.BatchContract(
        "b", [nb.FieldContract("f_amount", "m", nb.CurrencyDeclaration("USD", 2))]
    )
    if backend_name == "pyarrow":
        import pyarrow as pa

        frame = _frame(
            backend_name,
            {"m": [Decimal("1.200"), Decimal("0.000")]},
            {"m": pa.decimal128(38, 3)},
        )
    else:
        import polars as pl

        frame = _frame(
            backend_name,
            {"m": [Decimal("1.200"), Decimal("0.000")]},
            {"m": pl.Decimal(38, 3)},
        )
    result = nb.expand(frame, contract)
    assert result[nb.AMOUNT_COLUMN].to_list() == [Decimal("1.20"), Decimal("0.00")]
    nb.lossless_check(frame, contract)


def test_lossless_check_skips_columns_that_never_arrived(backend_name: str) -> None:
    contract = nb.BatchContract(
        "b",
        [
            nb.FieldContract("f_amount", "amount", nb.CurrencyDeclaration("USD", 2)),
            nb.FieldContract("f_fee", "fee", nb.CurrencyDeclaration("USD", 2)),
        ],
    )
    if backend_name == "pyarrow":
        import pyarrow as pa

        frame = _frame(
            backend_name, {"amount": [Decimal("1.00")]}, {"amount": pa.decimal128(38, 2)}
        )
    else:
        import polars as pl

        frame = _frame(
            backend_name, {"amount": [Decimal("1.00")]}, {"amount": pl.Decimal(38, 2)}
        )
    nb.lossless_check(frame, contract)


def test_concat_helpers_reject_empty_input() -> None:
    with pytest.raises(nb.BillingError, match="concat_billings"):
        nb.concat_billings([])
    with pytest.raises(nb.BillingError, match="concat_wide"):
        nb.concat_wide([])


def test_concat_wide_aligns_mixed_integer_and_decimal_scales(backend_name: str) -> None:
    if backend_name == "pyarrow":
        import pyarrow as pa

        first = _frame(
            backend_name,
            {"m": [1, 2], "only_here": [9, 8]},
            {"m": pa.int64(), "only_here": pa.int64()},
        )
        second = _frame(
            backend_name, {"m": [Decimal("3.40")]}, {"m": pa.decimal128(38, 2)}
        )
    else:
        import polars as pl

        first = _frame(
            backend_name,
            {"m": [1, 2], "only_here": [9, 8]},
            {"m": pl.Int64, "only_here": pl.Int64},
        )
        second = _frame(backend_name, {"m": [Decimal("3.40")]}, {"m": pl.Decimal(38, 2)})
    wide = nb.concat_wide([first, second]).sort("m")
    assert wide.schema["m"].scale == 2
    assert wide["m"].to_list() == [Decimal("1.00"), Decimal("2.00"), Decimal("3.40")]
    assert wide["only_here"].to_list() == [9, 8, None]


def test_concat_wide_keeps_shared_string_and_integer_only_columns(
    backend_name: str,
) -> None:
    if backend_name == "pyarrow":
        import pyarrow as pa

        first = _frame(
            backend_name,
            {"label": ["a"], "count": [1]},
            {"label": pa.string(), "count": pa.int64()},
        )
        second = _frame(
            backend_name,
            {"label": ["b"], "count": [2]},
            {"label": pa.string(), "count": pa.int64()},
        )
    else:
        import polars as pl

        first = _frame(
            backend_name,
            {"label": ["a"], "count": [1]},
            {"label": pl.String, "count": pl.Int64},
        )
        second = _frame(
            backend_name,
            {"label": ["b"], "count": [2]},
            {"label": pl.String, "count": pl.Int64},
        )
    wide = nb.concat_wide([first, second]).sort("count")
    assert wide["label"].to_list() == ["a", "b"]
    assert wide["count"].to_list() == [1, 2]
    assert wide.schema["count"] == nw.Int64()


def test_expand_concatenated_rejects_unknown_batch(backend_name: str) -> None:
    contract = nb.BatchContract(
        "b_known", [nb.FieldContract("f_amount", "m", nb.CurrencyDeclaration("USD", 2))]
    )
    contracts = nb.BillingContracts([contract])
    manifests = nb.IngestionManifests([nb.BatchManifest("b_known", frozenset({"m"}))])
    if backend_name == "pyarrow":
        import pyarrow as pa

        wide = _frame(
            backend_name,
            {"m": [Decimal("1.00")], nb.BATCH_ID_COLUMN: ["b_unknown"]},
            {"m": pa.decimal128(38, 2), nb.BATCH_ID_COLUMN: pa.string()},
        )
    else:
        import polars as pl

        wide = _frame(
            backend_name,
            {"m": [Decimal("1.00")], nb.BATCH_ID_COLUMN: ["b_unknown"]},
            {"m": pl.Decimal(38, 2), nb.BATCH_ID_COLUMN: pl.String},
        )
    with pytest.raises(nb.BillingError, match="no ingested contract"):
        nb.expand_concatenated(wide, contracts, manifests)


def test_ingest_rejects_reserved_business_columns(backend_name: str) -> None:
    if backend_name == "pyarrow":
        import pyarrow as pa

        frame = _frame(
            backend_name, {"__billing__.txn": ["x"]}, {"__billing__.txn": pa.string()}
        )
    else:
        import polars as pl

        frame = _frame(
            backend_name, {"__billing__.txn": ["x"]}, {"__billing__.txn": pl.String}
        )
    contract = nb.BatchContract(
        "b", [nb.FieldContract("f_amount", "m", nb.CurrencyDeclaration("USD", 2))]
    )
    with pytest.raises(nb.BillingError, match="reserved"):
        nb.ingest(frame, contract)


def test_empty_contract_reports_zero_max_scale() -> None:
    assert nb.BatchContract("b_empty", []).max_scale == 0


def test_unsupported_dtype_is_rejected(backend_name: str) -> None:
    contract = nb.BatchContract(
        "b", [nb.FieldContract("f_flag", "flag", nb.CurrencyDeclaration("USD", 2))]
    )
    if backend_name == "pyarrow":
        import pyarrow as pa

        frame = _frame(backend_name, {"flag": [True, False]}, {"flag": pa.bool_()})
    else:
        import polars as pl

        frame = _frame(backend_name, {"flag": [True, False]}, {"flag": pl.Boolean})
    with pytest.raises(nb.BillingContractError) as excinfo:
        nb.expand(frame, contract)
    assert excinfo.value.reason == "unsupported_dtype"
    assert excinfo.value.backend == backend_name
