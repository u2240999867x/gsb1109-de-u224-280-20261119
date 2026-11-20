import dlt
import pytest

from dlt.common.utils import uniq_id
from dlt.pipeline.preflight import (
    CONFIG_SOURCE_DEFAULT,
    CONFIG_SOURCE_GLOBAL,
    CONFIG_SOURCE_PARENT_TABLE,
    CONFIG_SOURCE_TABLE,
)

from tests.pipeline.utils import assert_load_info
from tests.utils import skip_if_not_active

skip_if_not_active("duckdb")


def get_pipeline(name_prefix: str = "preflight") -> dlt.Pipeline:
    import duckdb

    return dlt.pipeline(
        pipeline_name=f"{name_prefix}_{uniq_id()}",
        destination=dlt.destinations.duckdb(credentials=duckdb.connect(":memory:")),
        dev_mode=True,
    )


def scenario_payments(mode: str):
    """Two payments to the same normalized table, a fresh contract hint between them, then a
    different payment type routed to its own table."""

    @dlt.resource(name="payments")
    def payments():
        yield {"id": 1, "amount": 100, "currency": "EUR"}
        yield dlt.mark.with_hints(
            {"id": 2, "amount": 200},
            dlt.mark.make_hints(
                table_name="payments", schema_contract={"columns": mode}
            ),
            create_table_variant=True,
        )
        yield {"id": 3, "amount": 300, "currency": "USD", "reimbursed": True}
        yield dlt.mark.with_table_name({"refund_id": 9, "reason": "x"}, "refunds")

    return payments()


def scenario_second_batch(mode: str):
    """The hint and the records after it, to run on top of a table the first payment created."""

    @dlt.resource(name="payments")
    def rest():
        yield dlt.mark.with_hints(
            {"id": 2, "amount": 200},
            dlt.mark.make_hints(
                table_name="payments", schema_contract={"columns": mode}
            ),
            create_table_variant=True,
        )
        yield {"id": 3, "amount": 300, "currency": "USD", "reimbursed": True}
        yield dlt.mark.with_table_name({"refund_id": 9, "reason": "x"}, "refunds")

    return rest()


def scenario_first_payment():
    @dlt.resource(name="payments")
    def first():
        yield {"id": 1, "amount": 100, "currency": "EUR"}

    return first()


def test_preflight_is_side_effect_free() -> None:
    pipeline = get_pipeline()

    @dlt.resource(name="payments")
    def payments():
        yield {"id": 1, "amount": 100, "currency": "EUR"}

    before_tables = dict(pipeline.schemas) if pipeline.default_schema_name else {}
    result = pipeline.preflight(payments())

    assert [r.action for r in result.records] == ["loaded"]
    # nothing was extracted, normalized or loaded
    assert not pipeline.default_schema_name
    assert not pipeline.has_pending_data
    assert list(pipeline.schemas) == list(before_tables)
    # the destination never saw a table
    if pipeline.default_schema_name:
        assert "payments" not in pipeline.default_schema.tables


def test_preflight_first_record_creates_table() -> None:
    """A table missing from the schema may be created and its first-seen columns are accepted."""
    pipeline = get_pipeline()
    result = pipeline.preflight(scenario_payments("discard_value"))

    first = result.records[0]
    assert first.action == "loaded"
    assert first.table_name == "payments"
    assert first.contract == {"tables": "evolve", "columns": "evolve", "data_type": "evolve"}
    assert first.contract_source == CONFIG_SOURCE_DEFAULT
    assert {c.name: c.action for c in first.columns} == {
        "id": "accepted",
        "amount": "accepted",
        "currency": "accepted",
    }
    assert result.tables["payments"].is_new is True
    assert result.tables["payments"].has_data is True


def test_preflight_second_record_rejects_new_column() -> None:
    pipeline = get_pipeline()
    result = pipeline.preflight(scenario_payments("discard_value"))

    second = result.records[1]
    assert second.action == "loaded"
    assert second.table_name == "payments"
    # the hint for payments took effect and is the resolved source
    assert second.contract == {
        "tables": "evolve",
        "columns": "discard_value",
        "data_type": "evolve",
    }
    assert second.contract_source == CONFIG_SOURCE_TABLE
    assert second.hints[0]["schema_contract"] == {"columns": "discard_value"}

    third = result.records[2]
    assert third.action == "partial"
    assert third.table_name == "payments"
    assert third.discarded_columns == ["reimbursed"]
    rejected = next(c for c in third.columns if c.name == "reimbursed")
    assert rejected.action == "discarded_value"
    assert rejected.contract_source == CONFIG_SOURCE_TABLE
    assert "discard_value" in rejected.reason


@pytest.mark.parametrize(
    "mode,expected_action,expected_rows",
    [
        ("discard_value", "partial", 2),
        ("discard_row", "dropped", 1),
        ("freeze", "failed", 0),
    ],
    ids=["discard_value", "discard_row", "freeze"],
)
def test_preflight_matches_actual_load(
    mode: str, expected_action: str, expected_rows: int
) -> None:
    """The preflight conclusion for each record matches the rows that a real load writes."""
    first = scenario_first_payment()
    rest = scenario_second_batch(mode)
    preflight_pipeline = get_pipeline()
    preflight_pipeline.run(first)
    result = preflight_pipeline.preflight(rest)

    assert [r.action for r in result.records] == ["loaded", expected_action, "loaded"]

    load_pipeline = get_pipeline()
    assert_load_info(load_pipeline.run(first))
    if expected_action == "failed":
        with pytest.raises(Exception):
            load_pipeline.run(rest)
        # the payment with the new column never reached the destination
        assert "reimbursed" not in load_pipeline.default_schema.tables["payments"]["columns"]
        counts = {
            table_name: count
            for table_name, count in load_pipeline.dataset().row_counts().fetchall()
        }
        assert counts["payments"] == 1
    else:
        assert_load_info(load_pipeline.run(rest))
        counts = {
            table_name: count
            for table_name, count in load_pipeline.dataset().row_counts().fetchall()
        }
        # the first payment plus the rows the second run managed to write
        assert 1 + expected_rows == counts["payments"]
        assert "reimbursed" not in load_pipeline.default_schema.tables["payments"]["columns"]


def test_preflight_different_table_keeps_own_contract() -> None:
    """A hint for one table does not constrain a different payment target."""
    pipeline = get_pipeline()
    result = pipeline.preflight(scenario_payments("discard_row"))

    refund = result.records[3]
    # records: first payment, hinted payment, payment with a new column, refund
    assert [r.table_name for r in result.records] == [
        "payments",
        "payments",
        "payments",
        "refunds",
    ]
    assert refund.table_name == "refunds"
    assert refund.target_tables == ["refunds"]
    assert refund.action == "loaded"
    assert refund.contract == {"tables": "evolve", "columns": "evolve", "data_type": "evolve"}
    assert refund.contract_source == CONFIG_SOURCE_DEFAULT
    assert result.tables["refunds"].rejected_columns == {}
    # the payments contract did not leak
    assert result.tables["payments"].contract["columns"] == "discard_row"


def test_preflight_reports_inherited_contract_source() -> None:
    schema = dlt.Schema("inherited_contract")
    schema.set_schema_contract({"columns": "discard_value"})
    parent = {
        "name": "payments",
        "columns": {
            "id": {"name": "id", "data_type": "bigint"},
            "amount": {"name": "amount", "data_type": "bigint"},
        },
        "schema_contract": {"columns": "discard_row"},
    }
    schema.update_table(parent, normalize_identifiers=False)
    schema.update_table(
        {"name": "payments__refunds", "parent": "payments", "columns": {}},
        normalize_identifiers=False,
    )

    pipeline = get_pipeline()
    result = pipeline.preflight(
        [{"amount": 1, "note": "x"}],
        table_name="payments__refunds",
        schema=schema,
    )

    record = result.records[0]
    assert record.contract_source == CONFIG_SOURCE_PARENT_TABLE
    assert record.contract["columns"] == "discard_row"

    # a table without a hint inherits the schema-level setting
    result = pipeline.preflight([{"id": 1}], table_name="payments__refunds", schema=schema)
    assert result.records[0].contract_source == CONFIG_SOURCE_PARENT_TABLE

    # remove the parent hint to expose the global setting
    schema.tables["payments"].pop("schema_contract")
    result = pipeline.preflight([{"id": 1}], table_name="earnings", schema=schema)
    assert result.records[0].contract_source == CONFIG_SOURCE_GLOBAL


def test_preflight_single_batch_creates_then_rejects() -> None:
    """Within one input, the first payment creates the table and a later payment with a new
    column is rejected by the hint that arrived between them. The different payment type is
    unaffected and can be created."""
    pipeline = get_pipeline()
    result = pipeline.preflight(scenario_payments("discard_row"))

    # first payment creates the table: first-seen columns are allowed
    assert result.records[0].action == "loaded"
    assert result.tables["payments"].is_new is True
    # second payment follows the hint and is loaded
    assert result.records[1].action == "loaded"
    # third payment uses a new column -> dropped by the updated hint
    assert result.records[2].action == "dropped"
    assert result.records[2].discarded_columns == ["reimbursed"]
    # fourth record targets a different table and is not held to the payments hint
    assert result.records[3].action == "loaded"
    assert result.records[3].table_name == "refunds"
    assert result.tables["refunds"].contract_source == CONFIG_SOURCE_DEFAULT


def test_preflight_does_not_merge_input_table_names() -> None:
    """Records routed to the same normalized name share a table; other names do not."""
    pipeline = get_pipeline()
    result = pipeline.preflight(
        [
            dlt.mark.with_table_name({"id": 1}, "Payments"),
            dlt.mark.with_table_name({"id": 2}, "payments"),
            dlt.mark.with_table_name({"refund_id": 3}, "refunds"),
        ],
        table_name="orders",
    )

    tables = [r.table_name for r in result.records]
    assert tables == ["payments", "payments", "refunds"]
    assert result.tables["payments"].loaded_records == 2
    assert result.tables["refunds"].loaded_records == 1


def test_preflight_does_not_mutate_input_resource() -> None:
    @dlt.resource(name="payments")
    def payments():
        yield {"id": 1, "amount": 100}

    before = payments._hints
    pipeline = get_pipeline()
    pipeline.preflight(payments)
    assert payments._hints is before
