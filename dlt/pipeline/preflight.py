"""Plan how a run would apply schema contracts without writing a schema or loading data.

`preflight` reads the input in order through the real extractor, so item routing, dynamic table
names and mid-stream hints behave like a run. Each record is then normalized against a clone of
the pipeline schema, so column types and contract decisions match a load of the same input.
Nothing is persisted: no load package is created, the pipeline schema is not modified and the
destination is never opened for writing.
"""

from copy import copy
from dataclasses import dataclass, field
from typing import Any, Dict, List, Literal, Optional, Sequence, Tuple

from dlt.common import logger
from dlt.common.destination import DestinationCapabilitiesContext
from dlt.common.pipeline import SupportsPipeline
from dlt.common.schema import Schema
from dlt.common.schema.exceptions import DataValidationError
from dlt.common.schema.typing import (
    TSchemaContract,
    TSchemaContractDict,
    TSchemaContractEntities,
    TSchemaEvolutionMode,
    TTableSchema,
    TTableSchemaColumns,
)
from dlt.common.schema.utils import is_dlt_table_or_column
from dlt.common.typing import TDataItems
from dlt.common.utils import uniq_id

from dlt.extract.extract import data_to_sources, select_schema
from dlt.extract.extractors import Extractor
from dlt.extract.items import HintsMeta
from dlt.extract.pipe_iterator import PipeIterator
from dlt.extract.resource import DltResource
from dlt.extract.source import DltSource
from dlt.normalize.configuration import NormalizeConfiguration
from dlt.normalize.items_normalizers.jsonl import JsonLItemsNormalizer


# levels that may supply a contract, from the most to the least specific
CONFIG_SOURCE_TABLE = "table"
CONFIG_SOURCE_PARENT_TABLE = "parent_table"
CONFIG_SOURCE_GLOBAL = "global"
CONFIG_SOURCE_DEFAULT = "default"

TAction = Literal["loaded", "partial", "dropped", "failed", "table_rejected", "noop"]
TColumnAction = Literal["accepted", "discarded_value", "discarded_row", "rejected_freeze"]

DEFAULT_CONTRACT: TSchemaContractDict = {
    "tables": "evolve",
    "columns": "evolve",
    "data_type": "evolve",
}


@dataclass
class PreflightColumn:
    """What a preflight decided about a column of a single record."""

    name: str
    action: TColumnAction
    contract_entity: TSchemaContractEntities
    contract_mode: TSchemaEvolutionMode
    contract_source: str
    reason: str

    @property
    def accepted(self) -> bool:
        return self.action == "accepted"

    def asdict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "action": self.action,
            "accepted": self.accepted,
            "contract_entity": self.contract_entity,
            "contract_mode": self.contract_mode,
            "contract_source": self.contract_source,
            "reason": self.reason,
        }


@dataclass
class PreflightRecord:
    """What a preflight decided about a single input record."""

    index: int
    resource: str
    table_name: Optional[str]
    target_tables: List[str]
    action: TAction
    contract: TSchemaContractDict
    contract_source: str
    table_existed: Optional[bool] = None
    """Whether the target table existed when this record was read."""
    loaded_rows: Dict[str, List[Dict[str, Any]]] = field(default_factory=dict)
    columns: List[PreflightColumn] = field(default_factory=list)
    hints: List[Dict[str, Any]] = field(default_factory=list)
    reason: Optional[str] = None
    error: Optional[str] = None

    @property
    def loaded(self) -> bool:
        return self.action in ("loaded", "partial")

    @property
    def row(self) -> Optional[Dict[str, Any]]:
        rows = self.loaded_rows.get(self.table_name or "")
        return rows[0] if rows else None

    @property
    def discarded_columns(self) -> List[str]:
        return [c.name for c in self.columns if not c.accepted]

    def asdict(self) -> Dict[str, Any]:
        return {
            "index": self.index,
            "resource": self.resource,
            "table_name": self.table_name,
            "target_tables": self.target_tables,
            "action": self.action,
            "loaded": self.loaded,
            "contract": dict(self.contract),
            "contract_source": self.contract_source,
            "table_existed": self.table_existed,
            "loaded_rows": self.loaded_rows,
            "columns": [c.asdict() for c in self.columns],
            "discarded_columns": self.discarded_columns,
            "hints": self.hints,
            "reason": self.reason,
            "error": self.error,
        }


@dataclass
class PreflightTable:
    """State of a target table at the end of the preflight input."""

    name: str
    contract: TSchemaContractDict
    contract_source: str
    parent: Optional[str] = None
    is_new: bool = False
    """`True` when the table did not exist in the starting schema."""
    has_data: bool = False
    """`True` when at least one record was loaded into the table."""
    columns: List[str] = field(default_factory=list)
    loaded_records: int = 0
    dropped_records: int = 0
    rejected_columns: Dict[str, str] = field(default_factory=dict)

    def asdict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "contract": dict(self.contract),
            "contract_source": self.contract_source,
            "parent": self.parent,
            "is_new": self.is_new,
            "has_data": self.has_data,
            "columns": self.columns,
            "loaded_records": self.loaded_records,
            "dropped_records": self.dropped_records,
            "rejected_columns": self.rejected_columns,
        }


@dataclass
class PreflightHint:
    """A hint found in the input stream, and the record index it precedes."""

    before_record: int
    resource: str
    table_name: Optional[str]
    schema_contract: Optional[TSchemaContract]
    create_table_variant: bool

    def asdict(self) -> Dict[str, Any]:
        return {
            "before_record": self.before_record,
            "resource": self.resource,
            "table_name": self.table_name,
            "schema_contract": self.schema_contract,
            "create_table_variant": self.create_table_variant,
        }


@dataclass
class PreflightResult:
    """The outcome of a preflight."""

    schema_name: str
    records: List[PreflightRecord] = field(default_factory=list)
    tables: Dict[str, PreflightTable] = field(default_factory=dict)
    hints: List[PreflightHint] = field(default_factory=list)

    @property
    def loaded_records(self) -> List[PreflightRecord]:
        return [r for r in self.records if r.loaded]

    @property
    def dropped_records(self) -> List[PreflightRecord]:
        return [r for r in self.records if r.action == "dropped"]

    @property
    def failed_records(self) -> List[PreflightRecord]:
        return [r for r in self.records if r.action == "failed"]

    def records_for_table(self, table_name: str) -> List[PreflightRecord]:
        return [r for r in self.records if r.table_name == table_name]

    def asdict(self) -> Dict[str, Any]:
        return {
            "schema_name": self.schema_name,
            "records": [r.asdict() for r in self.records],
            "tables": {name: t.asdict() for name, t in self.tables.items()},
            "hints": [h.asdict() for h in self.hints],
        }

    def summary(self) -> str:
        lines = [f"Preflight for schema `{self.schema_name}`: {len(self.records)} record(s)"]
        hints_by_record = {h.before_record: h for h in self.hints}
        for record in self.records:
            hint = hints_by_record.get(record.index)
            if hint is not None and hint.schema_contract is not None:
                lines.append(
                    f"  hint before record {record.index}: table"
                    f" `{hint.table_name or record.table_name}` gets schema_contract"
                    f" {hint.schema_contract}"
                )
            target = record.table_name or "<unrouted>"
            lines.append(
                f"  record {record.index} ({record.resource}): {record.action} -> `{target}`"
                f" [contract from {record.contract_source}]"
            )
            for column in record.columns:
                if not column.accepted:
                    lines.append(f"    column `{column.name}`: {column.action}")
            if record.reason:
                lines.append(f"    {record.reason}")
        if self.tables:
            lines.append("  tables:")
            for table in self.tables.values():
                state = "new" if table.is_new else "existing"
                lines.append(
                    f"    `{table.name}` ({state}, contract from {table.contract_source}):"
                    f" {table.loaded_records} loaded, {table.dropped_records} dropped, columns"
                    f" {table.columns}"
                )
                if table.rejected_columns:
                    lines.append(f"      rejected columns: {table.rejected_columns}")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self.summary()


class _RecordingItemStorage:
    """Item storage that records writes instead of writing a load package."""

    def __init__(self) -> None:
        self.written: List[Tuple[str, TDataItems, Optional[TTableSchemaColumns]]] = []

    def write_data_item(
        self,
        load_id: str,
        schema_name: str,
        table_name: str,
        item: TDataItems,
        columns: TTableSchemaColumns = None,
    ) -> int:
        self.written.append((table_name, item, columns))
        return len(item) if isinstance(item, list) else 1

    def write_empty_items_file(self, *args: Any, **kwargs: Any) -> None:
        return None

    def import_items_file(self, *args: Any, **kwargs: Any) -> None:
        return None

    def closed_files(self, load_id: str) -> List[Any]:
        return []

    def remove_closed_files(self, load_id: str) -> None:
        return None


class _PreflightExtractor(Extractor):
    """Extractor that records routed items and computed tables without updating a schema."""

    def __init__(
        self,
        load_id: str,
        schema: Schema,
        caps: DestinationCapabilitiesContext,
        hints: List[PreflightHint],
        base_record_count: int = 0,
    ) -> None:
        super().__init__(load_id, _RecordingItemStorage(), schema, _caps=caps)
        self.writes: List[Tuple[str, TDataItems, List[TTableSchema]]] = []
        self._computed_tables: List[TTableSchema] = []
        self._hints = hints
        self._record_count = base_record_count

    def write_items(self, resource: DltResource, items: TDataItems, meta: Any) -> None:
        if isinstance(meta, HintsMeta) and meta.hints:
            table_name = meta.hints.get("table_name")
            self._hints.append(
                PreflightHint(
                    before_record=self._record_count,
                    resource=resource.name,
                    table_name=table_name if isinstance(table_name, str) else None,
                    schema_contract=meta.hints.get("schema_contract"),
                    create_table_variant=meta.create_table_variant,
                )
            )
        super().write_items(resource, items, meta)

    def _compute_and_update_tables(
        self, resource: DltResource, root_table_name: str, items: TDataItems, meta: Any
    ) -> TDataItems:
        # compute exactly as the extractor does, including new-table detection from the
        # working schema, but leave the schema untouched so the planner can decide per record
        self._computed_tables = self._compute_tables(resource, items, meta)
        return items

    def _write_item(
        self,
        table_name: str,
        resource_name: str,
        items: TDataItems,
        columns: TTableSchemaColumns = None,
    ) -> None:
        if items is None:
            return
        count = len(items) if isinstance(items, list) else 1
        self.writes.append((table_name, items, list(self._computed_tables)))
        self._record_count += count

    def _import_item(self, table_name: str, resource_name: str, meta: Any) -> None:
        self.writes.append((table_name, None, []))
        self._record_count += 1


def _build_contract(
    schema: Schema, table_name: str, computed_table: TTableSchema = None
) -> Tuple[TSchemaContractDict, str]:
    """Returns the contract for `table_name` and the level that supplied it.

    A hint computed during extraction takes precedence over the stored table hint, which takes
    precedence over the parent chain and then the schema settings. This mirrors
    `Schema.resolve_contract_settings_for_table` and additionally reports the source level.
    """
    if is_dlt_table_or_column(table_name, schema._dlt_tables_prefix):
        return dict(DEFAULT_CONTRACT), CONFIG_SOURCE_DEFAULT
    if computed_table is not None and computed_table.get("schema_contract"):
        return Schema.expand_schema_contract_settings(
            computed_table["schema_contract"]
        ), CONFIG_SOURCE_TABLE
    table = schema.tables.get(table_name)
    if table is not None and table.get("schema_contract"):
        return Schema.expand_schema_contract_settings(
            table["schema_contract"]
        ), CONFIG_SOURCE_TABLE
    parent_name = (table.get("parent") if table is not None else None) or (
        computed_table.get("parent") if computed_table is not None else None
    )
    while parent_name:
        parent_table = schema.tables.get(parent_name)
        if parent_table is not None and parent_table.get("schema_contract"):
            return Schema.expand_schema_contract_settings(
                parent_table["schema_contract"]
            ), CONFIG_SOURCE_PARENT_TABLE
        parent_name = parent_table.get("parent") if parent_table is not None else None
    if schema.settings.get("schema_contract"):
        return Schema.expand_schema_contract_settings(
            schema.settings["schema_contract"]
        ), CONFIG_SOURCE_GLOBAL
    return dict(DEFAULT_CONTRACT), CONFIG_SOURCE_DEFAULT


def _source_description(source: str, table_name: str) -> str:
    if source == CONFIG_SOURCE_TABLE:
        return f"the `schema_contract` hint on table `{table_name}`"
    if source == CONFIG_SOURCE_PARENT_TABLE:
        return f"the `schema_contract` inherited from a parent table of `{table_name}`"
    if source == CONFIG_SOURCE_GLOBAL:
        return "the schema-level `schema_contract` setting"
    return "the default schema contract"


def _clone_input(data: Any) -> Any:
    """Clones resources and sources so preflight does not modify the caller's hints."""
    if isinstance(data, DltSource):
        return data.clone()
    if isinstance(data, DltResource):
        return data._clone()
    if isinstance(data, Sequence) and not isinstance(data, (str, bytes)) and data:
        if isinstance(data[0], (DltSource, DltResource)):
            return [_clone_input(item) for item in data]
    return data


def _user_columns(row: Dict[str, Any]) -> List[str]:
    return [key for key in row if not key.startswith("_dlt_")]


def _has_user_columns(row: Dict[str, Any]) -> bool:
    return any(not key.startswith("_dlt_") for key in row)


class _PreflightPlanner:
    def __init__(self, pipeline: SupportsPipeline) -> None:
        self.pipeline = pipeline
        self.load_id = "preflight_" + uniq_id(8)
        self._caps = self._resolve_capabilities()
        self._config = NormalizeConfiguration(workers=1, destination_capabilities=self._caps)
        self._finalized = False

    def _resolve_capabilities(self) -> DestinationCapabilitiesContext:
        destination = getattr(self.pipeline, "_destination", None)
        if destination is not None:
            try:
                return destination.capabilities()
            except Exception:
                logger.debug("Could not resolve destination capabilities, using generic ones")
        return DestinationCapabilitiesContext.generic_capabilities()

    def plan(self, data: Any, **run_kwargs: Any) -> PreflightResult:
        base_schema = self._base_schema(run_kwargs.pop("schema", None))
        sources = data_to_sources(
            _clone_input(data),
            self.pipeline,
            schema=base_schema.clone(),
            table_name=run_kwargs.get("table_name"),
            parent_table_name=run_kwargs.get("parent_table_name"),
            write_disposition=run_kwargs.get("write_disposition"),
            columns=run_kwargs.get("columns"),
            primary_key=run_kwargs.get("primary_key"),
            schema_contract=run_kwargs.get("schema_contract"),
        )
        result = PreflightResult(schema_name=base_schema.name)
        for source in sources:
            self._plan_source(source, base_schema, result)
        self._finalize(result)
        return result

    def _base_schema(self, schema: Schema = None) -> Schema:
        if schema is not None:
            return schema.clone()
        default_schema_name = getattr(self.pipeline, "default_schema_name", None)
        if default_schema_name and default_schema_name in self.pipeline.schemas:
            return self.pipeline.schemas[default_schema_name].clone()
        return Schema(select_schema(self.pipeline).name)

    def _plan_source(
        self, source: DltSource, base_schema: Schema, result: PreflightResult
    ) -> None:
        # apply the source-level contract that the run would apply so overrides are visible
        working_schema = base_schema.clone()
        source_contract = source.schema.settings.get("schema_contract")
        if source_contract:
            working_schema.set_schema_contract(source_contract)
        source.schema = working_schema
        extractor = _PreflightExtractor(
            self.load_id,
            working_schema,
            self._caps,
            result.hints,
            base_record_count=len(result.records),
        )
        with PipeIterator.from_pipes(
            source.resources.selected_pipes, max_parallel_items=1, workers=1
        ) as pipes:
            for pipe_item in pipes:
                resource = source.resources.with_pipe(pipe_item.pipe)
                extractor.writes.clear()
                extractor.write_items(resource, pipe_item.item, pipe_item.meta)
                for table_name, items, computed_tables in extractor.writes:
                    self._plan_write(
                        working_schema, resource, table_name, items, computed_tables, result
                    )
        result.schema_name = working_schema.name
        self._final_schema = working_schema

    def _plan_write(
        self,
        schema: Schema,
        resource: DltResource,
        table_name: str,
        items: TDataItems,
        computed_tables: List[TTableSchema],
        result: PreflightResult,
    ) -> None:
        record_items = items if isinstance(items, list) else [items]
        # extraction stores computed hints before data is normalized, so a mid-stream hint is
        # in effect for this batch. reproduce that on the working schema.
        self._apply_computed_hints(schema, computed_tables)
        for item in record_items:
            record = PreflightRecord(
                index=len(result.records),
                resource=resource.name,
                table_name=table_name,
                target_tables=[t["name"] for t in computed_tables] or [table_name],
                action="noop",
                contract=dict(DEFAULT_CONTRACT),
                contract_source=CONFIG_SOURCE_DEFAULT,
            )
            record.hints = [
                h.asdict() for h in result.hints if h.before_record == record.index
            ]
            try:
                self._plan_record(schema, resource, table_name, item, computed_tables, record)
            except DataValidationError as ex:
                record.action = "failed"
                record.error = str(ex)
                record.reason = str(ex)
                if ex.column_name:
                    record.columns.append(
                        PreflightColumn(
                            name=ex.column_name,
                            action="rejected_freeze",
                            contract_entity=ex.schema_entity,
                            contract_mode=ex.contract_mode,
                            contract_source=record.contract_source,
                            reason=(
                                f"column `{ex.column_name}` is not accepted because the"
                                f" `{ex.schema_entity}` contract for table `{ex.table_name}` is"
                                f" `{ex.contract_mode}`; the contract comes from"
                                f" {_source_description(record.contract_source, ex.table_name)}"
                            ),
                        )
                    )
            except Exception as ex:
                record.action = "failed"
                record.error = f"{type(ex).__name__}: {ex}"
                record.reason = record.error
            result.records.append(record)

    def _apply_computed_hints(
        self, schema: Schema, computed_tables: List[TTableSchema]
    ) -> None:
        for table in computed_tables:
            partial = copy(table)
            partial["columns"] = {
                name: copy(col) for name, col in table.get("columns", {}).items()
            }
            try:
                schema.update_table(
                    partial, normalize_identifiers=False, merge_compound_props=False
                )
            except Exception:
                logger.debug(f"Preflight could not store computed hints for {table['name']}")

    def _plan_record(
        self,
        schema: Schema,
        resource: DltResource,
        root_table_name: str,
        item: Any,
        computed_tables: List[TTableSchema],
        record: PreflightRecord,
    ) -> None:
        computed_table = next(
            (t for t in computed_tables if t["name"] == root_table_name), None
        )
        contract, contract_source = _build_contract(schema, root_table_name, computed_table)
        record.contract = contract
        record.contract_source = contract_source
        was_new_table = schema.is_new_table(root_table_name)
        record.table_existed = root_table_name in schema.tables and not was_new_table

        snapshot = schema.clone()
        storage = _RecordingItemStorage()
        normalizer = JsonLItemsNormalizer(
            storage, None, None, schema, self.load_id, self._config
        )
        try:
            normalizer._normalize_chunk(root_table_name, [item], False, False)
        except DataValidationError:
            self._restore(schema, snapshot)
            raise

        landed: Dict[str, List[Dict[str, Any]]] = {}
        for table_name, rows, _ in storage.written:
            row_list = rows if isinstance(rows, list) else [rows]
            landed.setdefault(table_name, []).extend(
                row for row in row_list if isinstance(row, dict)
            )
        record.loaded_rows = landed

        filtered_columns = normalizer._filtered_tables_columns.get(root_table_name, {})
        accepted_columns = set()
        for row in landed.get(root_table_name, []):
            accepted_columns.update(_user_columns(row))
        for column_name in _user_columns(item) if isinstance(item, dict) else []:
            normalized = schema.naming.normalize_path(column_name)
            mode = filtered_columns.get(normalized)
            if mode is None:
                if normalized in accepted_columns:
                    record.columns.append(
                        self._accepted_column(normalized, contract, contract_source, was_new_table)
                    )
                continue
            action: TColumnAction = (
                "discarded_value" if mode == "discard_value" else "discarded_row"
            )
            record.columns.append(
                PreflightColumn(
                    name=normalized,
                    action=action,
                    contract_entity="columns",
                    contract_mode=mode,
                    contract_source=contract_source,
                    reason=(
                        f"column `{normalized}` is not accepted because the `columns` contract"
                        f" for table `{root_table_name}` is `{mode}`; the contract comes from"
                        f" {_source_description(contract_source, root_table_name)}"
                    ),
                )
            )

        if root_table_name in normalizer._filtered_tables:
            record.action = "table_rejected"
            record.reason = (
                f"table `{root_table_name}` cannot be created because the `tables` contract is"
                f" `{contract['tables']}`; the contract comes from"
                f" {_source_description(contract_source, root_table_name)}"
            )
            self._restore(schema, snapshot)
            return

        root_rows = landed.get(root_table_name, [])
        if not any(_has_user_columns(row) for row in root_rows):
            record.action = "dropped"
            row_dropped = [c.name for c in record.columns if c.action == "discarded_row"]
            record.reason = (
                f"the row is dropped because column(s) {row_dropped} are not accepted by the"
                f" `columns` contract `{contract['columns']}`"
                if row_dropped
                else "the row is dropped by the current contract"
            )
            self._restore(schema, snapshot)
            return

        discarded = [c for c in record.columns if not c.accepted]
        record.action = "partial" if discarded else "loaded"
        if discarded:
            record.reason = (
                f"the row is loaded without column(s) {[c.name for c in discarded]} because the"
                f" `columns` contract is `{contract['columns']}`"
            )

        # a table that received a row has seen data, which consumes the first-seen column allowance
        for table_name, rows in landed.items():
            if not any(_has_user_columns(row) for row in rows):
                continue
            table = schema.tables.get(table_name)
            if table is None:
                continue
            x_normalizer = table.setdefault("x-normalizer", {})
            x_normalizer.pop("evolve-columns-once", None)
            x_normalizer["seen-data"] = True

    def _accepted_column(
        self,
        name: str,
        contract: TSchemaContractDict,
        contract_source: str,
        was_new_table: bool,
    ) -> PreflightColumn:
        reason = (
            "the column is accepted because the table is new, so first-seen columns are allowed"
            if was_new_table
            else "the column is accepted by the current contract"
        )
        return PreflightColumn(
            name=name,
            action="accepted",
            contract_entity="columns",
            contract_mode=contract["columns"],
            contract_source=contract_source,
            reason=reason,
        )

    def _restore(self, schema: Schema, snapshot: Schema) -> None:
        schema.replace_schema_content(snapshot, link_to_replaced_schema=False)

    def _finalize(self, result: PreflightResult) -> None:
        if self._finalized:
            return
        self._finalized = True
        final_schema: Schema = getattr(self, "_final_schema", None)
        for record in result.records:
            table_name = record.table_name
            if table_name is None:
                continue
            table = result.tables.get(table_name)
            if table is None:
                table = PreflightTable(
                    name=table_name,
                    contract=record.contract,
                    contract_source=record.contract_source,
                )
                table.is_new = record.table_existed is False
                schema_table = (
                    final_schema.tables.get(table_name) if final_schema is not None else None
                )
                if schema_table is not None:
                    table.parent = schema_table.get("parent")
                    table.columns = [
                        name
                        for name in schema_table["columns"]
                        if not name.startswith("_dlt_")
                    ]
                # the contract that remains in the schema after the input is consumed
                if schema_table is not None:
                    final_contract, final_source = _build_contract(final_schema, table_name)
                    table.contract = final_contract
                    table.contract_source = final_source
                result.tables[table_name] = table
            if record.loaded:
                table.loaded_records += 1
                table.has_data = True
            elif record.action == "dropped":
                table.dropped_records += 1
            for column in record.columns:
                if not column.accepted:
                    table.rejected_columns[column.name] = column.contract_mode


def preflight(
    pipeline: SupportsPipeline,
    data: Any,
    *,
    table_name: str = None,
    parent_table_name: str = None,
    write_disposition: Any = None,
    columns: Any = None,
    primary_key: Any = None,
    schema_contract: Any = None,
    schema: Schema = None,
) -> PreflightResult:
    """Plan how `data` would be loaded without changing the schema or the destination.

    The input is read in order through the real extractor and normalized record by record against
    a clone of the pipeline schema. A mid-stream hint affects only the records that follow it and
    every target table resolves its own contract, so a hint for one table does not leak into
    another. Neither the pipeline schema nor the destination is modified.

    Args:
        pipeline (SupportsPipeline): Pipeline whose stored schema and destination are used for
            the plan.
        data (Any): The same data accepted by `Pipeline.run`.
        table_name (str): Overrides the table name for all resources, like `Pipeline.run`.
        parent_table_name (str): Sets the parent table for all resources, like `Pipeline.run`.
        write_disposition (Any): Overrides the write disposition, like `Pipeline.run`.
        columns (Any): Overrides columns, like `Pipeline.run`.
        primary_key (Any): Overrides the primary key, like `Pipeline.run`.
        schema_contract (Any): Overrides the contract for all sources, like `Pipeline.run`.
        schema (Schema): Starts the plan from this schema instead of the pipeline schema.

    Returns:
        PreflightResult: Per-record outcomes with the resolved contract, the level that supplied
        it and the reason a column was rejected or discarded. `asdict()` returns plain data.
    """
    planner = _PreflightPlanner(pipeline)
    return planner.plan(
        data,
        table_name=table_name,
        parent_table_name=parent_table_name,
        write_disposition=write_disposition,
        columns=columns,
        primary_key=primary_key,
        schema_contract=schema_contract,
        schema=schema,
    )
