from typing import Any, Optional

import pytest

from dlt.common.destination.capabilities import DestinationCapabilitiesContext
from dlt.common.schema.utils import get_list_presence_column_name, new_table
from dlt.destinations.sql_jobs import SqlMergeFollowupJob


class StubSqlClient:
    """The parts of `SqlClientBase` that the merge SQL generation reads."""

    def __init__(self, supported_merge_strategies: Any = None) -> None:
        self.capabilities = DestinationCapabilitiesContext.generic_capabilities(
            supported_merge_strategies=supported_merge_strategies
        )

    def escape_column_name(
        self, column_name: str, quote: bool = True, casefold: bool = True
    ) -> str:
        return self.capabilities.escape_identifier(column_name)

    def fully_qualified_dataset_name(self, quote: bool = True, staging: bool = False) -> str:
        return self.dataset_name(staging)

    def get_qualified_table_names(
        self, table_name: str, quote: bool = True, casefold: bool = True
    ) -> Any:
        return (
            f"{self.dataset_name(False)}.{table_name}",
            f"{self.dataset_name(True)}.{table_name}",
        )

    def make_qualified_table_name(
        self, table_name: str, quote: bool = True, casefold: bool = True
    ) -> str:
        return f"{self.dataset_name(False)}.{table_name}"

    @staticmethod
    def dataset_name(staging: bool) -> str:
        return "staging" if staging else "main"


def _orders_chain(strategy: str, with_marker: bool = True, primary_key: bool = True) -> Any:
    """An `orders` table with an `items` list."""
    id_column: Any = {"name": "id", "data_type": "bigint"}
    if primary_key:
        id_column["primary_key"] = True
    root_columns = [
        id_column,
        {"name": "v", "data_type": "text"},
        {"name": "_dlt_id", "data_type": "text", "row_key": True},
        {"name": "_dlt_load_id", "data_type": "text"},
    ]
    if with_marker:
        # the normalizer writes this column only when the row submits the list
        root_columns.append(
            {"name": get_list_presence_column_name("orders__items"), "data_type": "bool"}
        )
    root = new_table("orders", write_disposition="merge", columns=root_columns)
    root["x-merge-strategy"] = strategy  # type: ignore[typeddict-unknown-key]
    child = new_table(
        "orders__items",
        parent_table_name="orders",
        columns=[
            {"name": "sku", "data_type": "text"},
            {"name": "_dlt_id", "data_type": "text", "row_key": True},
            {"name": "_dlt_load_id", "data_type": "text"},
            {"name": "_dlt_parent_id", "data_type": "text", "parent_key": True},
            {"name": "_dlt_root_id", "data_type": "text", "root_key": True},
        ],
    )
    return [root, child]


def _nested_deletes(sql: Any) -> Any:
    return [statement for statement in sql if "DELETE FROM main.orders__items" in statement]


@pytest.mark.parametrize("strategy", ["delete-insert", "upsert"], ids=["delete-insert", "upsert"])
def test_nested_delete_keeps_rows_of_unsubmitted_lists(strategy: str) -> None:
    """A nested list is replaced only for the parents that submitted it.

    The marker on the parent row tells a submitted list, also an empty one, from a missing
    list.
    """
    client = StubSqlClient([strategy])
    chain = _orders_chain(strategy)
    if strategy == "delete-insert":
        statements = SqlMergeFollowupJob.gen_merge_sql(chain, client)  # type: ignore[arg-type]
    else:
        statements = SqlMergeFollowupJob.gen_upsert_sql(chain, client)  # type: ignore[arg-type]

    deletes = _nested_deletes(statements)
    assert len(deletes) == 1
    delete = deletes[0]
    # a parent that did not submit the list has a NULL marker, and its rows are kept
    assert f"{get_list_presence_column_name('orders__items')} IS NOT NULL" in delete
    # the kept rows are only the rows of parents that survive the merge
    if strategy == "delete-insert":
        # the deleted roots come from the delete temp table
        assert "main.orders__items" in delete
        assert "staging.orders" in delete
    else:
        assert "_dlt_parent_id IN (SELECT _dlt_id FROM staging.orders" in delete


@pytest.mark.parametrize("strategy", ["delete-insert", "upsert"], ids=["delete-insert", "upsert"])
def test_nested_delete_without_marker_keeps_previous_behavior(strategy: str) -> None:
    """A table loaded before the marker existed keeps deleting nested rows of all parents."""
    client = StubSqlClient([strategy])
    chain = _orders_chain(strategy, with_marker=False)

    if strategy == "delete-insert":
        statements = SqlMergeFollowupJob.gen_merge_sql(chain, client)  # type: ignore[arg-type]
    else:
        statements = SqlMergeFollowupJob.gen_upsert_sql(chain, client)  # type: ignore[arg-type]

    deletes = _nested_deletes(statements)
    assert len(deletes) == 1
    assert "_dlt_present_" not in deletes[0]


def test_delete_insert_without_primary_key_has_no_kept_condition() -> None:
    """Without a primary key the root id is not stable, so kept rows would be orphaned."""
    client = StubSqlClient(["delete-insert"])
    chain = _orders_chain("delete-insert", primary_key=False)
    kept_cond = SqlMergeFollowupJob.gen_kept_nested_rows_cond(chain, chain[1], client)  # type: ignore[arg-type]
    assert kept_cond is None


@pytest.mark.parametrize("strategy", ["scd2", "insert-only"], ids=["scd2", "insert-only"])
def test_other_strategies_do_not_keep_nested_rows(strategy: str) -> None:
    """Only the strategies that replace nested rows need the marker."""
    client = StubSqlClient([strategy])
    chain = _orders_chain(strategy)
    kept_cond = SqlMergeFollowupJob.gen_kept_nested_rows_cond(chain, chain[1], client)  # type: ignore[arg-type]
    assert kept_cond is None
