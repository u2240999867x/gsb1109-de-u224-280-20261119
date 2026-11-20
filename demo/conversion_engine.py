"""Streaming, atomic Data Package conversion engine built on Frictionless.

Guarantees implemented here:

* Source packages are read row by row through ``resource.row_stream``; rows are
  never preloaded into memory as a whole.
* Every conversion writes into a private staging directory first. The output
  directory (and its ``datapackage.json`` descriptor) is only created once both
  resources have been streamed and validated, so a failed or abandoned run can
  never leave behind a partial package that downstream tooling could open.
* Each conversion opens its own ``Package`` instance from the same descriptor
  path, so two conversions (and a later recovery) never share generators,
  stats or schema objects.
* Output files are plain CSVs plus a descriptor on disk: they are reopened
  independently and do not depend on any exhausted source generator.
"""

from __future__ import annotations

import csv
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterator, List, Optional

from frictionless import Package, Resource, system

RENAME_JOIN_FIELD: Dict[str, str] = {"order_id": "order_ref"}
DESCRIPTOR_NAME = "datapackage.json"


class ConversionError(Exception):
    """Raised when a strict conversion encounters an invalid source row."""

    def __init__(self, resource_name: str, row_number: int, row_errors: List[Any]):
        self.resource_name = resource_name
        self.row_number = row_number
        self.row_errors = row_errors
        messages = "; ".join(error.message for error in row_errors)
        super().__init__(
            f"conversion failed on resource {resource_name!r} "
            f"row {row_number}: {messages}"
        )


@dataclass
class PullOutcome:
    resource_name: str
    exhausted: bool
    skipped: bool
    row: Optional[Dict[str, Any]]
    rows_scanned: int
    rows_written: int


@dataclass
class ResourceStats:
    rows_scanned: int = 0
    rows_written: int = 0
    rejected: List[Dict[str, Any]] = field(default_factory=list)


def _as_list(value: Any) -> List[str]:
    return [value] if isinstance(value, str) else list(value)


def build_target_schema(source_resource: Resource, rename: Dict[str, str]):
    """Copy a resource schema and rename the join field everywhere it appears."""
    schema = source_resource.schema.to_copy()
    for table_field in schema.fields:
        if table_field.name in rename:
            table_field.name = rename[table_field.name]
    schema.primary_key = [rename.get(name, name) for name in schema.primary_key]
    for foreign_key in schema.foreign_keys:
        foreign_key["fields"] = [
            rename.get(name, name) for name in _as_list(foreign_key["fields"])
        ]
        reference = foreign_key["reference"]
        reference["fields"] = [
            rename.get(name, name) for name in _as_list(reference["fields"])
        ]
    return schema


def rename_row(row: Dict[str, Any], rename: Dict[str, str]) -> Dict[str, Any]:
    return {rename.get(key, key): value for key, value in row.items()}


class _ResourceCursor:
    """Lazily streamed reader for one resource, writing one staging CSV."""

    def __init__(
        self,
        converter: "PackageConverter",
        source_package: Package,
        resource_name: str,
        target_filename: str,
        target_schema: Any,
        row_filter: Optional[Callable[[Dict[str, Any]], bool]],
        rename: Dict[str, str],
        on_accept: Optional[Callable[[Dict[str, Any]], None]] = None,
    ):
        self._converter = converter
        self._resource_name = resource_name
        self._target_filename = target_filename
        self._schema = target_schema
        self._row_filter = row_filter
        self._rename = rename
        self._on_accept = on_accept
        self.stats = ResourceStats()

        source_resource = source_package.get_resource(resource_name).to_copy()
        # Foreign-key lookups would eagerly scan the referenced resource into
        # memory. Integrity is checked on the staged package at commit time,
        # so the streaming copies do not need lookup tables.
        source_resource.schema.foreign_keys = []
        self._source_resource = source_resource

        self._iterator: Optional[Iterator[Any]] = None
        self._exhausted = False
        self._staging_path = os.path.join(converter.staging_dir, target_filename)
        self._staging_file = None
        self._writer: Optional[csv.writer] = None

    @property
    def exhausted(self) -> bool:
        return self._exhausted

    @property
    def started(self) -> bool:
        return self._iterator is not None

    @property
    def staging_path(self) -> str:
        return self._staging_path

    def _ensure_stream(self) -> Iterator[Any]:
        if self._iterator is None:
            self._source_resource.open()
            self._iterator = iter(self._source_resource.row_stream)
        return self._iterator

    def _ensure_writer(self) -> csv.writer:
        if self._writer is None:
            handle = open(self._staging_path, "wt", encoding="utf-8", newline="")
            self._staging_file = handle
            self._writer = csv.writer(handle)
            self._writer.writerow(self._schema.field_names)
        return self._writer

    def pull(self) -> PullOutcome:
        iterator = self._ensure_stream()
        try:
            row = next(iterator)
        except StopIteration:
            self._exhausted = True
            self.close()
            return PullOutcome(
                self._resource_name, True, False, None,
                self.stats.rows_scanned, self.stats.rows_written,
            )

        self.stats.rows_scanned += 1
        row_dict = row.to_dict()

        if not row.valid:
            if self._converter.policy == "strict":
                raise ConversionError(
                    self._resource_name, row.row_number, list(row.errors)
                )
            self.stats.rejected.append(
                {"row_number": row.row_number, "errors": [e.message for e in row.errors]}
            )
            return PullOutcome(
                self._resource_name, False, True, rename_row(row_dict, self._rename),
                self.stats.rows_scanned, self.stats.rows_written,
            )

        if self._row_filter and not self._row_filter(row_dict):
            return PullOutcome(
                self._resource_name, False, True, rename_row(row_dict, self._rename),
                self.stats.rows_scanned, self.stats.rows_written,
            )

        mapped = rename_row(row_dict, self._rename)
        if self._on_accept is not None:
            self._on_accept(mapped)
        writer = self._ensure_writer()
        cells = [
            table_field.write_cell(mapped.get(table_field.name), ignore_missing=True)[0]
            for table_field in self._schema.fields
        ]
        writer.writerow(cells)
        self.stats.rows_written += 1
        return PullOutcome(
            self._resource_name, False, False, mapped,
            self.stats.rows_scanned, self.stats.rows_written,
        )

    def drain(self) -> None:
        while True:
            outcome = self.pull()
            if outcome.exhausted:
                return

    def close(self) -> None:
        if self._staging_file is not None:
            self._staging_file.close()
            self._staging_file = None
        if not self._source_resource.closed:
            self._source_resource.close()

    def cleanup(self) -> None:
        self.close()
        if os.path.exists(self._staging_path):
            os.remove(self._staging_path)


class PackageConverter:
    """Stream one source package into one staged, atomically published output."""

    def __init__(
        self,
        source_descriptor_path: str,
        output_dir: str,
        *,
        name: str,
        region: str,
        policy: str = "strict",
        rename: Optional[Dict[str, str]] = None,
    ):
        if policy not in {"strict", "tolerant"}:
            raise ValueError("policy must be 'strict' or 'tolerant'")
        self.source_descriptor_path = source_descriptor_path
        self.output_dir = output_dir
        self.name = name
        self.region = region
        self.policy = policy
        self.rename = rename or RENAME_JOIN_FIELD
        self.status = "initialized"
        self.stats: Dict[str, ResourceStats] = {}

        self._source_package = Package(source_descriptor_path)
        parent_dir = os.path.dirname(os.path.abspath(output_dir))
        os.makedirs(parent_dir, exist_ok=True)
        self.staging_dir = tempfile.mkdtemp(prefix=f".staging-{name}-", dir=parent_dir)
        self._cursors: Dict[str, _ResourceCursor] = {}
        self._target_schemas: Dict[str, Any] = {}
        self._target_filenames = {"orders": "orders.csv", "items": "items.csv"}
        # Join keys of orders that pass the region filter. Items can only be
        # accepted once their parent order is in this set, which keeps the
        # output package referentially intact without buffering any rows.
        self._accepted_order_refs: set = set()

    @property
    def source_package(self) -> Package:
        """The independently opened source package of this conversion only."""
        return self._source_package

    def __enter__(self) -> "PackageConverter":
        self.status = "running"
        orders_schema = build_target_schema(
            self._source_package.get_resource("orders"), self.rename
        )
        items_schema = build_target_schema(
            self._source_package.get_resource("items"), self.rename
        )
        self._target_schemas = {"orders": orders_schema, "items": items_schema}

        self._cursors = {
            "orders": _ResourceCursor(
                self, self._source_package, "orders",
                self._target_filenames["orders"], orders_schema,
                lambda row: row["region"] == self.region, self.rename,
                on_accept=lambda mapped: self._accepted_order_refs.add(
                    mapped["order_ref"]
                ),
            ),
            "items": _ResourceCursor(
                self, self._source_package, "items",
                self._target_filenames["items"], items_schema,
                lambda row: row["order_id"] in self._accepted_order_refs,
                self.rename,
            ),
        }
        self.stats = {name: cursor.stats for name, cursor in self._cursors.items()}
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        if self.status == "running":
            self.abort()

    def pull(self, resource_name: str) -> PullOutcome:
        if self.status != "running":
            raise RuntimeError(f"converter {self.name!r} is {self.status}")
        return self._cursors[resource_name].pull()

    def drain(self, resource_name: str) -> None:
        self._cursors[resource_name].drain()

    def is_exhausted(self, resource_name: str) -> bool:
        return self._cursors[resource_name].exhausted

    def _validate_staging(self) -> None:
        resources = []
        for resource_name in ("orders", "items"):
            cursor = self._cursors[resource_name]
            cursor.close()
            resources.append(
                Resource(
                    name=resource_name,
                    path=cursor.staging_path,
                    schema=self._target_schemas[resource_name],
                )
            )
        staged_package = Package(name=f"{self.name}-staging", resources=resources)
        # Staging files live in an absolute temp path; trust them for the
        # integrity check. The published descriptor only uses relative paths.
        with system.use_context(trusted=True):
            report = staged_package.validate()
        if not report.valid:
            report_errors = list(report.errors) + [
                error for task in report.tasks for error in task.errors
            ]
            raise ConversionError("package", 0, report_errors)

    def commit(self) -> str:
        """Validate staging, then publish files and descriptor atomically."""
        if self.status != "running":
            raise RuntimeError(f"converter {self.name!r} is {self.status}")
        for cursor in self._cursors.values():
            if cursor.started and not cursor.exhausted:
                raise RuntimeError("all resources must be drained before commit")
        self._validate_staging()

        os.makedirs(self.output_dir, exist_ok=True)
        try:
            for resource_name, cursor in self._cursors.items():
                shutil.move(
                    cursor.staging_path,
                    os.path.join(self.output_dir, self._target_filenames[resource_name]),
                )

            package = Package(
                name=f"{self.name}-orders",
                resources=[
                    Resource(
                        name=resource_name,
                        path=self._target_filenames[resource_name],
                        schema=self._target_schemas[resource_name],
                    )
                    for resource_name in ("orders", "items")
                ],
            )
            descriptor_path = os.path.join(self.output_dir, DESCRIPTOR_NAME)
            package.to_json(descriptor_path)
        except BaseException:
            # A failure between moving the data and writing the descriptor
            # must not leave a descriptor-less package behind.
            shutil.rmtree(self.output_dir, ignore_errors=True)
            self._cleanup_staging()
            self.status = "failed"
            raise
        self._cleanup_staging()
        self.status = "completed"
        return descriptor_path

    def abort(self) -> None:
        for cursor in self._cursors.values():
            cursor.cleanup()
        self._cleanup_staging()
        if os.path.exists(self.output_dir):
            shutil.rmtree(self.output_dir)
        self.status = "failed" if self.policy == "strict" else "aborted"

    def stop_early(self) -> None:
        """Mark an intentionally abandoned consumer run as not successful."""
        self.abort()
        self.status = "aborted"

    def _cleanup_staging(self) -> None:
        if os.path.exists(self.staging_dir):
            shutil.rmtree(self.staging_dir)


def open_source_package(source_descriptor_path: str) -> Package:
    """Fresh package instance from disk; never shares state with a conversion."""
    return Package(source_descriptor_path)
