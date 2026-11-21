from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

import narwhals as nw
from narwhals import billing as nb

BIG_JPY = Decimal(1234567890123456789012345678)
KEY = [nb.BATCH_ID_COLUMN, nb.SOURCE_ROW_COLUMN, nb.FIELD_ID_COLUMN]


@pytest.fixture
def contracts() -> nb.BillingContracts:
    old = nb.BatchContract(
        "b_old",
        [nb.FieldContract("f_amount", "amount_local", nb.CurrencyDeclaration("JPY", 0))],
    )
    new = nb.BatchContract(
        "b_new",
        [
            nb.FieldContract(
                "f_amount", "amount_local", nb.CurrencyDeclaration("USD", 2)
            ),
            nb.FieldContract("f_fee", "fee_usd", nb.CurrencyDeclaration("USD", 2)),
        ],
    )
    third = nb.BatchContract(
        "b_third",
        [
            nb.FieldContract("f_amount", "fee_usd", nb.CurrencyDeclaration("USD", 2)),
            nb.FieldContract("f_fee", "amount_local", nb.CurrencyDeclaration("USD", 2)),
        ],
    )
    return nb.BillingContracts([old, new, third])


def _arrow_batches() -> dict[str, nw.DataFrame[Any]]:
    import pyarrow as pa

    old = nw.from_native(
        pa.table(
            {
                "txn": ["T1", "T2"],
                "amount_local": pa.array([BIG_JPY, None], pa.decimal128(38, 0)),
            }
        )
    )
    new = nw.from_native(
        pa.table(
            {
                "txn": ["T1", "T3"],
                "amount_local": pa.array(
                    [Decimal("12.30"), Decimal("0.00")], pa.decimal128(38, 2)
                ),
                "fee_usd": pa.array([Decimal("1.200"), None], pa.decimal128(38, 3)),
            }
        )
    )
    third = nw.from_native(
        pa.table(
            {
                "txn": ["T4", "T5"],
                "fee_usd": pa.array(
                    [Decimal("9.90"), Decimal("0.00")], pa.decimal128(38, 2)
                ),
            }
        )
    )
    return {"b_old": old, "b_new": new, "b_third": third}


def _polars_batches() -> dict[str, nw.DataFrame[Any]]:
    import polars as pl

    old = nw.from_native(
        pl.DataFrame(
            {"txn": ["T1", "T2"], "amount_local": [BIG_JPY, None]},
            schema={"txn": pl.String, "amount_local": pl.Decimal(38, 0)},
        )
    )
    new = nw.from_native(
        pl.DataFrame(
            {
                "txn": ["T1", "T3"],
                "amount_local": [Decimal("12.30"), Decimal("0.00")],
                "fee_usd": [Decimal("1.200"), None],
            },
            schema={
                "txn": pl.String,
                "amount_local": pl.Decimal(38, 2),
                "fee_usd": pl.Decimal(38, 3),
            },
        )
    )
    third = nw.from_native(
        pl.DataFrame(
            {"txn": ["T4", "T5"], "fee_usd": [Decimal("9.90"), Decimal("0.00")]},
            schema={"txn": pl.String, "fee_usd": pl.Decimal(38, 2)},
        )
    )
    return {"b_old": old, "b_new": new, "b_third": third}


@pytest.fixture(params=["pyarrow", "polars"])
def batches(request: pytest.FixtureRequest) -> dict[str, nw.DataFrame[Any]]:
    return _arrow_batches() if request.param == "pyarrow" else _polars_batches()


@pytest.fixture
def backend_name(batches: dict[str, nw.DataFrame[Any]]) -> str:
    return batches["b_old"].__native_namespace__().__name__


def _rows(frame: nw.DataFrame[Any]) -> list[dict[str, Any]]:
    return list(frame.iter_rows(named=True))


def _keyed(frame: nw.DataFrame[Any]) -> dict[tuple[Any, ...], dict[str, Any]]:
    return {tuple(row[column] for column in KEY): row for row in _rows(frame)}


def test_old_batch_keeps_integer_beyond_float_range_exact(
    batches: dict[str, nw.DataFrame[Any]], contracts: nb.BillingContracts
) -> None:
    contract = contracts.for_batch("b_old")
    result = nb.expand(batches["b_old"], contract)
    rows = _rows(result)
    assert len(rows) == 2
    big = next(row for row in rows if row[nb.SOURCE_ROW_COLUMN] == 0)
    assert big[nb.CURRENCY_COLUMN] == "JPY"
    assert big[nb.AMOUNT_COLUMN] == BIG_JPY
    assert big[nb.AMOUNT_COLUMN] != Decimal(float(BIG_JPY))
    assert big[nb.PRESENCE_COLUMN] == nb.PRESENCE_PRESENT
    assert result.schema[nb.AMOUNT_COLUMN].scale == 0


def test_same_physical_name_changes_currency_with_contract(
    batches: dict[str, nw.DataFrame[Any]], contracts: nb.BillingContracts
) -> None:
    old = nb.expand(batches["b_old"], contracts.for_batch("b_old"))
    new = nb.expand(batches["b_new"], contracts.for_batch("b_new"))
    old_amount = _rows(old)[0]
    new_amount = next(
        row
        for row in _rows(new)
        if row[nb.FIELD_ID_COLUMN] == "f_amount" and row[nb.SOURCE_ROW_COLUMN] == 0
    )
    assert old_amount[nb.PHYSICAL_NAME_COLUMN] == "amount_local"
    assert new_amount[nb.PHYSICAL_NAME_COLUMN] == "amount_local"
    assert old_amount[nb.CURRENCY_COLUMN] == "JPY"
    assert new_amount[nb.CURRENCY_COLUMN] == "USD"
    assert new_amount[nb.AMOUNT_COLUMN] == Decimal("12.30")


def test_true_zero_explicit_null_and_missing_column_are_separable(
    batches: dict[str, nw.DataFrame[Any]], contracts: nb.BillingContracts
) -> None:
    new = nb.expand(batches["b_new"], contracts.for_batch("b_new"))
    new_rows = _keyed(new)
    zero = new_rows[("b_new", 1, "f_amount")]
    explicit_null = new_rows[("b_new", 1, "f_fee")]
    assert zero[nb.PRESENCE_COLUMN] == nb.PRESENCE_PRESENT
    assert zero[nb.AMOUNT_COLUMN] == Decimal("0.00")
    assert explicit_null[nb.PRESENCE_COLUMN] == nb.PRESENCE_EXPLICIT_NULL
    assert explicit_null[nb.AMOUNT_COLUMN] is None

    third = nb.expand(batches["b_third"], contracts.for_batch("b_third"))
    third_rows = _keyed(third)
    missing = third_rows[("b_third", 0, "f_fee")]
    swapped = third_rows[("b_third", 0, "f_amount")]
    assert missing[nb.PRESENCE_COLUMN] == nb.PRESENCE_MISSING_COLUMN
    assert missing[nb.AMOUNT_COLUMN] is None
    assert missing[nb.PHYSICAL_NAME_COLUMN] == "amount_local"
    assert swapped[nb.PHYSICAL_NAME_COLUMN] == "fee_usd"
    assert swapped[nb.AMOUNT_COLUMN] == Decimal("9.90")


def test_undeclared_currency_has_no_rows(
    batches: dict[str, nw.DataFrame[Any]], contracts: nb.BillingContracts
) -> None:
    old = nb.expand(batches["b_old"], contracts.for_batch("b_old"))
    assert {row[nb.CURRENCY_COLUMN] for row in _rows(old)} == {"JPY"}
    assert all(row[nb.FIELD_ID_COLUMN] == "f_amount" for row in _rows(old))


def test_display_name_swap_is_interpreted_by_field_identity(
    batches: dict[str, nw.DataFrame[Any]], contracts: nb.BillingContracts
) -> None:
    third = nb.expand(batches["b_third"], contracts.for_batch("b_third"))
    for row in _rows(third):
        field = contracts.for_batch("b_third").field(row[nb.FIELD_ID_COLUMN])
        assert row[nb.CURRENCY_COLUMN] == field.currency.currency
        assert row[nb.PHYSICAL_NAME_COLUMN] == field.physical_name
    amount_rows = [row for row in _rows(third) if row[nb.FIELD_ID_COLUMN] == "f_amount"]
    assert {row[nb.PHYSICAL_NAME_COLUMN] for row in amount_rows} == {"fee_usd"}


def test_expand_then_concat_matches_concat_then_expand(
    batches: dict[str, nw.DataFrame[Any]], contracts: nb.BillingContracts
) -> None:
    manifests = nb.IngestionManifests(
        [
            nb.BatchManifest.from_frame(batches[batch_id], contracts.for_batch(batch_id))
            for batch_id in ("b_old", "b_new", "b_third")
        ]
    )

    expanded_first = nb.concat_billings(
        [
            nb.expand(batches[batch_id], contracts.for_batch(batch_id))
            for batch_id in ("b_old", "b_new", "b_third")
        ]
    ).filter(nw.col("txn") != "T5")

    stamped = [
        nb.ingest(batches[batch_id], contracts.for_batch(batch_id))
        for batch_id in ("b_old", "b_new", "b_third")
    ]
    wide = nb.concat_wide(stamped)
    wide = wide.filter(nw.col("txn") != "T5")
    wide = wide.select(
        "txn", "amount_local", "fee_usd", nb.BATCH_ID_COLUMN, nb.SOURCE_ROW_COLUMN
    ).rename({"txn": "txn_id"})
    expanded_last = nb.expand_concatenated(
        wide, contracts, manifests, index_columns=["txn_id"]
    ).rename({"txn_id": "txn"})

    first = _keyed(expanded_first)
    last = _keyed(expanded_last)
    assert set(first) == set(last)
    for key, expected in first.items():
        actual = last[key]
        assert actual[nb.CURRENCY_COLUMN] == expected[nb.CURRENCY_COLUMN]
        assert actual[nb.AMOUNT_COLUMN] == expected[nb.AMOUNT_COLUMN]
        assert actual[nb.PRESENCE_COLUMN] == expected[nb.PRESENCE_COLUMN]
        assert actual[nb.DECLARED_SCALE_COLUMN] == expected[nb.DECLARED_SCALE_COLUMN]
        assert actual[nb.PHYSICAL_NAME_COLUMN] == expected[nb.PHYSICAL_NAME_COLUMN]


def test_ingested_source_row_survives_mid_batch_filtering(
    batches: dict[str, nw.DataFrame[Any]], contracts: nb.BillingContracts
) -> None:
    contract = contracts.for_batch("b_new")
    stamped = nb.ingest(batches["b_new"], contract)
    manifest = nb.BatchManifest.from_frame(batches["b_new"], contract)
    filtered = stamped.filter(nw.col("txn") == "T3")
    expanded = nb.expand_concatenated(
        filtered, nb.BillingContracts([contract]), nb.IngestionManifests([manifest])
    )
    assert set(expanded[nb.SOURCE_ROW_COLUMN].to_list()) == {1}

    direct = nb.expand(batches["b_new"], contract)
    assert set(direct[nb.SOURCE_ROW_COLUMN].to_list()) == {0, 1}


def test_same_txn_in_two_batches_keeps_separate_source_identity(
    batches: dict[str, nw.DataFrame[Any]], contracts: nb.BillingContracts
) -> None:
    combined = nb.concat_billings(
        [
            nb.expand(batches["b_old"], contracts.for_batch("b_old")),
            nb.expand(batches["b_new"], contracts.for_batch("b_new")),
        ]
    )
    t1 = [row for row in _rows(combined) if row["txn"] == "T1"]
    assert {row[nb.BATCH_ID_COLUMN] for row in t1} == {"b_old", "b_new"}
    assert all(row[nb.SOURCE_ROW_COLUMN] == 0 for row in t1)
    assert {row[nb.CURRENCY_COLUMN] for row in t1} == {"JPY", "USD"}


def test_late_contract_cannot_reinterpret_existing_batch(
    batches: dict[str, nw.DataFrame[Any]], contracts: nb.BillingContracts
) -> None:
    late = nb.BatchContract(
        "b_late",
        [nb.FieldContract("f_amount", "amount_local", nb.CurrencyDeclaration("USD", 1))],
    )
    extended = nb.BillingContracts(
        [contracts.for_batch(batch_id) for batch_id in ("b_old", "b_new", "b_third")]
        + [late]
    )

    existing = nb.expand(batches["b_new"], extended.for_batch("b_new"))
    amount = next(
        row
        for row in _rows(existing)
        if row[nb.FIELD_ID_COLUMN] == "f_amount" and row[nb.SOURCE_ROW_COLUMN] == 0
    )
    assert amount[nb.DECLARED_SCALE_COLUMN] == 2
    assert amount[nb.AMOUNT_COLUMN] == Decimal("12.30")

    with pytest.raises(nb.BillingContractError) as excinfo:
        nb.expand(_tightening_frame(backend_name), late)
    error = excinfo.value
    assert error.batch_id == "b_late"
    assert error.field_id == "f_amount"
    assert error.declared_scale == 1
    assert error.physical_scale == 2
    assert error.offenders == (Decimal("12.34"),)
    assert error.source_rows == (0,)
    assert error.backend in {"pyarrow", "polars"}


def _tightening_frame(backend_name: str) -> nw.DataFrame[Any]:
    if backend_name == "pyarrow":
        import pyarrow as pa

        return nw.from_native(
            pa.table(
                {
                    "amount_local": pa.array(
                        [Decimal("12.34"), Decimal("1.20")], pa.decimal128(38, 2)
                    )
                }
            )
        )
    import polars as pl

    return nw.from_native(
        pl.DataFrame(
            {"amount_local": [Decimal("12.34"), Decimal("1.20")]},
            schema={"amount_local": pl.Decimal(38, 2)},
        )
    )


def test_lossless_narrowing_reports_batch_and_declaration_on_both_backends(
    backend_name: str,
) -> None:
    late = nb.BatchContract(
        "b_late",
        [nb.FieldContract("f_amount", "amount_local", nb.CurrencyDeclaration("USD", 1))],
    )
    frame = _tightening_frame(backend_name)
    with pytest.raises(nb.BillingContractError) as excinfo:
        nb.lossless_check(frame, late)
    error = excinfo.value
    assert error.reason == "unrepresentable_scale"
    assert error.backend == backend_name
    assert error.batch_id == "b_late"
    assert error.currency == "USD"
    assert (error.offenders, error.source_rows) == ((Decimal("12.34"),), (0,))
    # The fitting row alone would pass.
    fitting = frame.filter(nw.col("amount_local").cast(nw.String).str.contains(r"1\.20"))
    nb.lossless_check(fitting, late)


def test_floating_point_money_is_rejected(
    backend_name: str, contracts: nb.BillingContracts
) -> None:
    contract = contracts.for_batch("b_new")
    if backend_name == "pyarrow":
        import pyarrow as pa

        frame = nw.from_native(pa.table({"fee_usd": pa.array([1.2, 3.4], pa.float64())}))
    else:
        import polars as pl

        frame = nw.from_native(pl.DataFrame({"fee_usd": [1.2, 3.4]}))
    with pytest.raises(nb.BillingContractError) as excinfo:
        nb.expand(frame, contract)
    error = excinfo.value
    assert error.reason == "floating_point"
    assert error.backend == backend_name
    assert error.field_id == "f_fee"


def test_reserved_namespace_never_uses_business_column_names(
    batches: dict[str, nw.DataFrame[Any]], contracts: nb.BillingContracts
) -> None:
    stamped = nb.ingest(batches["b_new"], contracts.for_batch("b_new"))
    business = [
        column for column in stamped.columns if not column.startswith("__billing__.")
    ]
    assert business == ["txn", "amount_local", "fee_usd"]
    expanded = nb.expand(batches["b_new"], contracts.for_batch("b_new"))
    assert all(
        column == "txn" or column.startswith("__billing__.")
        for column in expanded.columns
    )
    with pytest.raises(nb.BillingError):
        nb.expand(
            batches["b_new"].rename({"txn": "__billing__.txn"}),
            contracts.for_batch("b_new"),
        )


def test_manifest_conflict_is_rejected(
    batches: dict[str, nw.DataFrame[Any]], contracts: nb.BillingContracts
) -> None:
    contract = contracts.for_batch("b_third")
    lying = nb.BatchManifest("b_third", frozenset({"fee_usd", "amount_local"}))
    with pytest.raises(nb.BillingError, match="manifest"):
        nb.expand(batches["b_third"], contract, manifest=lying)


def test_expand_concatenated_requires_stamp_and_manifests(
    batches: dict[str, nw.DataFrame[Any]], contracts: nb.BillingContracts
) -> None:
    manifests = nb.IngestionManifests(
        [nb.BatchManifest.from_frame(batches["b_old"], contracts.for_batch("b_old"))]
    )
    with pytest.raises(nb.BillingError, match="must carry"):
        nb.expand_concatenated(batches["b_old"], contracts, manifests)


def test_original_unpivot_without_column_list_is_unchanged(
    batches: dict[str, nw.DataFrame[Any]],
) -> None:
    frame = batches["b_new"]
    result = frame.unpivot(
        on=None, index=["txn"], variable_name="variable", value_name="value"
    )
    assert result.columns == ["txn", "variable", "value"]
    assert result.shape[0] == frame.shape[0] * 2


def test_index_columns_are_validated(
    batches: dict[str, nw.DataFrame[Any]], contracts: nb.BillingContracts
) -> None:
    with pytest.raises(nb.BillingError, match="index columns"):
        nb.expand(
            batches["b_old"], contracts.for_batch("b_old"), index_columns=["missing"]
        )


def test_empty_contract_returns_canonical_empty_frame(
    batches: dict[str, nw.DataFrame[Any]],
) -> None:
    contract = nb.BatchContract("b_empty", [])
    source = batches["b_old"]
    result = nb.expand(source, contract)
    assert result.shape[0] == 0
    assert result.columns == [*source.columns, *nb.CANONICAL_COLUMNS]
