from __future__ import annotations

import pytest

from narwhals import billing as nb


def test_currency_declaration_validation() -> None:
    nb.CurrencyDeclaration("JPY", 0)
    nb.CurrencyDeclaration("USD", 2)
    for currency in ("", 1):  # type: ignore[arg-type]
        with pytest.raises(nb.BillingError):
            nb.CurrencyDeclaration(currency, 2)  # type: ignore[arg-type]
    for scale in (-1, 39, 1.0, True):
        with pytest.raises(nb.BillingError):
            nb.CurrencyDeclaration("USD", scale)  # type: ignore[arg-type]


def test_field_contract_validation() -> None:
    field = nb.FieldContract("f_amount", "amount_local", nb.CurrencyDeclaration("JPY", 0))
    assert field.field_id == "f_amount"
    with pytest.raises(nb.BillingError):
        nb.FieldContract("", "amount_local", nb.CurrencyDeclaration("JPY", 0))
    with pytest.raises(nb.BillingError):
        nb.FieldContract("f.amount", "amount_local", nb.CurrencyDeclaration("JPY", 0))
    with pytest.raises(nb.BillingError):
        nb.FieldContract(
            "__billing__.x", "amount_local", nb.CurrencyDeclaration("JPY", 0)
        )
    with pytest.raises(nb.BillingError):
        nb.FieldContract("f_amount", "", nb.CurrencyDeclaration("JPY", 0))


def test_batch_contract_rejects_duplicates_and_reports_max_scale() -> None:
    fields = (
        nb.FieldContract("f_jpy", "jpy", nb.CurrencyDeclaration("JPY", 0)),
        nb.FieldContract("f_usd", "usd", nb.CurrencyDeclaration("USD", 2)),
        nb.FieldContract("f_bhd", "bhd", nb.CurrencyDeclaration("BHD", 3)),
    )
    contract = nb.BatchContract("b1", fields)
    assert contract.max_scale == 3
    assert contract.field("f_usd").currency.currency == "USD"
    with pytest.raises(nb.BillingError):
        nb.BatchContract("b1", [fields[0], fields[0]])
    with pytest.raises(nb.BillingError):
        nb.BatchContract("", fields)
    with pytest.raises(nb.BillingError, match="not declared"):
        contract.field("missing")


def test_contracts_registry_freezes_per_batch_contracts() -> None:
    old = nb.BatchContract(
        "b_old",
        [nb.FieldContract("f_amount", "amount_local", nb.CurrencyDeclaration("JPY", 0))],
    )
    new = nb.BatchContract(
        "b_new",
        [nb.FieldContract("f_amount", "amount_local", nb.CurrencyDeclaration("USD", 2))],
    )
    registry = nb.BillingContracts.from_mapping(
        {"b_old": old.fields, "b_new": new.fields}
    )
    assert set(registry.batch_ids) == {"b_old", "b_new"}
    assert len(registry) == 2
    assert "b_old" in registry
    assert "b_unknown" not in registry
    assert registry.for_batch("b_old").fields[0].currency.currency == "JPY"
    assert registry.for_batch("b_new").fields[0].currency.currency == "USD"
    with pytest.raises(nb.BillingError, match="no ingested contract"):
        registry.for_batch("b_unknown")
    with pytest.raises(nb.BillingError, match="duplicate batch"):
        nb.BillingContracts([old, old])


def test_manifests_distinguish_missing_column_from_explicit_null() -> None:
    import pyarrow as pa

    import narwhals as nw

    contract = nb.BatchContract(
        "b",
        [
            nb.FieldContract(
                "f_amount", "amount_local", nb.CurrencyDeclaration("USD", 2)
            ),
            nb.FieldContract("f_fee", "fee_usd", nb.CurrencyDeclaration("USD", 2)),
        ],
    )
    frame = nw.from_native(
        pa.table(
            {
                "amount_local": pa.array(
                    [__import__("decimal").Decimal("1.00")], pa.decimal128(38, 2)
                )
            }
        )
    )
    manifest = nb.BatchManifest.from_frame(frame, contract)
    assert manifest.present_physical_names == frozenset({"amount_local"})
    registry = nb.IngestionManifests.from_mapping({"b": ["amount_local"]})
    assert registry.for_batch("b") == manifest
    assert "b" in registry
    assert len(registry) == 1
    with pytest.raises(nb.BillingError, match="no ingestion manifest"):
        registry.for_batch("other")
    with pytest.raises(nb.BillingError, match="duplicate manifest"):
        nb.IngestionManifests([manifest, manifest])


def test_reserved_constants_live_in_dedicated_namespace() -> None:
    for column in (
        nb.BATCH_ID_COLUMN,
        nb.SOURCE_ROW_COLUMN,
        nb.FIELD_ID_COLUMN,
        nb.CURRENCY_COLUMN,
        nb.PHYSICAL_NAME_COLUMN,
        nb.PRESENCE_COLUMN,
        nb.AMOUNT_COLUMN,
    ):
        assert column.startswith("__billing__.")
    assert {
        nb.PRESENCE_PRESENT,
        nb.PRESENCE_EXPLICIT_NULL,
        nb.PRESENCE_MISSING_COLUMN,
    } == {"present", "explicit_null", "missing_column"}
