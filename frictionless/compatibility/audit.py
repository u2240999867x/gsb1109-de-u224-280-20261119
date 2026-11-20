from __future__ import annotations

import csv
import io
import re
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple, Union

import attrs

from .. import errors, helpers, types
from ..exception import FrictionlessException
from ..package import Package
from ..resources import TableResource
from .contract import ABSENT, CandidateRecord, CompatibilityContract, _jsonable

# Standard constraints whose effect on a value can be replayed from the
# evidence. Anything else is a custom constraint, and is only certifiable when
# the publisher states that its value domain covers it.
STANDARD_CONSTRAINTS = {
    "required",
    "unique",
    "minimum",
    "maximum",
    "exclusiveMinimum",
    "exclusiveMaximum",
    "minLength",
    "maxLength",
    "pattern",
    "enum",
}

OUTCOMES = ("rejected", "changed", "compatible", "not-certifiable")

DIRECTION_SIDES = {
    "old-write/new-read": ("old", "new"),
    "new-write/old-read": ("new", "old"),
    "old-write/old-read": ("old", "old"),
    "new-write/new-read": ("new", "new"),
}


@attrs.define(kw_only=True, repr=False)
class CompatibilityAudit:
    """Audit read/write compatibility between two Data Package descriptors.

    The audit does not compare field types or ask whether parsing raised.
    For every allowed direction it writes the publisher's raw records with
    the writer descriptor, reads the produced bytes with the reader
    descriptor, and compares the effective values and the cross-table
    reconciliation with the old descriptor's reading of the same records.
    """

    contract: CompatibilityContract

    @classmethod
    def from_descriptor(
        cls, source: Union[types.IDescriptor, str]
    ) -> "CompatibilityAudit":
        return cls(contract=CompatibilityContract.from_descriptor(source))

    def run(self) -> "CompatibilityReport":
        contract = self.contract
        old_digest = contract.old.digest
        new_digest = contract.new.digest

        baseline = _read_baseline(contract)
        directions = [
            _audit_direction(contract, direction, baseline)
            for direction in contract.directions
        ]
        unchanged = (
            old_digest == contract.old.digest
            and new_digest == contract.new.digest
        )

        report = CompatibilityReport(
            name=contract.name,
            inputs={
                **contract.inputs_descriptor(),
                "descriptorsUnchanged": unchanged,
                "records": {
                    table: [record.to_descriptor() for record in records]
                    for table, records in contract.records.items()
                },
            },
            directions=directions,
            baseline=baseline,
        )
        report.summarize()
        return report


@attrs.define(kw_only=True, repr=False)
class CompatibilityReport:
    name: str
    inputs: types.IDescriptor
    directions: List[types.IDescriptor]
    baseline: types.IDescriptor
    status: str = "unknown"
    summary: types.IDescriptor = attrs.field(factory=dict)

    def summarize(self) -> None:
        totals = {outcome: 0 for outcome in OUTCOMES}
        direction_statuses: Dict[str, str] = {}
        for direction in self.directions:
            direction_statuses[direction["direction"]] = direction["status"]
            for outcome in OUTCOMES:
                totals[outcome] += len(direction.get(outcome, []))

        if totals["rejected"]:
            status = "rejected"
        elif totals["changed"]:
            status = "changed"
        elif totals["not-certifiable"]:
            status = "not-certifiable"
        elif totals["compatible"]:
            status = "compatible"
        else:
            status = "unknown"
        self.status = status
        self.summary = {
            "status": status,
            "records": totals,
            "directions": direction_statuses,
        }

    # Convert

    def to_descriptor(self) -> types.IDescriptor:
        return {
            "name": self.name,
            "status": self.status,
            "summary": _jsonable(self.summary),
            "inputs": _jsonable(self.inputs),
            "baseline": _jsonable(self.baseline),
            "directions": _jsonable(self.directions),
        }

    def to_json(self, path: Optional[str] = None) -> str:
        text = helpers.to_json(self.to_descriptor())
        if path:
            helpers.write_file(path, text)
        return text

    def to_yaml(self, path: Optional[str] = None) -> str:
        text = helpers.to_yaml(self.to_descriptor())
        if path:
            helpers.write_file(path, text)
        return text


# Direction


def _audit_direction(
    contract: CompatibilityContract,
    direction: str,
    baseline: types.IDescriptor,
) -> types.IDescriptor:
    write_name, read_name = DIRECTION_SIDES[direction]
    write_side = getattr(contract, write_name)
    read_side = getattr(contract, read_name)
    finding: types.IDescriptor = {
        "direction": direction,
        "write": write_name,
        "read": read_name,
        "status": "unknown",
        "summary": {},
        "rejected": [],
        "changed": [],
        "compatible": [],
        "not-certifiable": [],
        "certified": {},
        "relationships": [],
        "descriptorErrors": {
            "write": write_side.errors,
            "read": read_side.errors,
        },
    }

    # A descriptor that cannot even be imported cannot participate.
    if write_side.errors or read_side.errors:
        for table, records in contract.records.items():
            for record in records:
                finding["rejected"].append(
                    {
                        "table": table,
                        "record": record.id,
                        "outcome": "rejected",
                        "reason": "descriptor-invalid",
                        "original": record.to_descriptor(),
                        "writeDescriptorErrors": write_side.errors,
                        "readDescriptorErrors": read_side.errors,
                    }
                )
        _finish(finding)
        return finding

    # Replay every record through the writer, capturing the writer input, its
    # own row errors, and the bytes it emits.
    headers: Dict[str, List[str]] = {}
    payloads: Dict[str, bytes] = {}
    write_evidence: Dict[Tuple[str, str], types.IDescriptor] = {}
    write_failures: Dict[str, List[types.IDescriptor]] = {}
    for table, records in contract.records.items():
        schema = _get_schema(write_side.package, table)
        headers[table] = schema.field_names
        write_failures[table] = []

        data: List[Any] = [list(schema.field_names)]
        for record in records:
            data.append(
                [
                    record.values.get(name) if name in record.explicit_fields else None
                    for name in schema.field_names
                ]
            )
        writer_read, payload, wire_rows = _writer_run(write_side, table, data)
        payloads[table] = payload

        for index, record in enumerate(records):
            row = writer_read["rows"][index] if index < len(writer_read["rows"]) else None
            row_errors = (
                writer_read["rowErrors"][index]
                if index < len(writer_read["rowErrors"])
                else []
            )
            input_cells = data[index + 1]
            wire_cells = wire_rows[index + 1] if index + 1 < len(wire_rows) else []
            evidence = _wire_evidence(
                schema,
                record,
                input_cells,
                wire_cells,
                row_errors,
                writer_read["headerErrors"],
            )
            write_evidence[(table, record.id)] = evidence
            if row_errors or writer_read["headerErrors"]:
                write_failures[table].append(
                    {
                        "record": record.id,
                        "errors": row_errors,
                        "headerErrors": writer_read["headerErrors"],
                    }
                )

    # Read the produced bytes through the reader's package, so foreign keys
    # are evaluated against the reader's target descriptors.
    observed = _read_package(read_side, payloads, kind="bytes")
    observed_relationships = _reconcile(contract, observed)
    baseline_relationships = {
        item["name"]: item for item in baseline.get("relationships", [])
    }
    baseline_records = {
        (item["table"], item["record"]): item for item in baseline.get("records", [])
    }

    uncovered = _uncovered_constraints(contract)

    for table, records in contract.records.items():
        table_data = observed["tables"].get(table, {})
        rows = table_data.get("rows", [])
        row_errors = table_data.get("rowErrors", [])
        header_errors = table_data.get("headerErrors", [])
        for index, record in enumerate(records):
            key = (table, record.id)
            base = baseline_records.get(key)
            evidence = write_evidence[key]
            row = rows[index] if index < len(rows) else None
            errors = row_errors[index] if index < len(row_errors) else []
            item: types.IDescriptor = {
                "table": table,
                "record": record.id,
                "original": record.to_descriptor(),
                "writerInput": {
                    "header": headers[table],
                    "cells": [_jsonable(cell) for cell in evidence["input"]],
                },
                "wire": {
                    "header": evidence["wireHeader"],
                    "cells": evidence["wire"],
                },
                "writeErrors": evidence["writeErrors"],
                "writeChanges": evidence["writeChanges"],
                "headerErrors": header_errors,
                "rowErrors": errors,
                "labels": table_data.get("labels", []),
                "read": row,
                "absentFromRecord": sorted(set(headers[table]) - record.explicit_fields),
                "valuePreserved": False,
                "relationshipPreserved": False,
            }

            if evidence["writeErrors"] or evidence["writerHeaderErrors"]:
                item["outcome"] = "rejected"
                item["reason"] = (
                    "write-error" if evidence["writeErrors"] else "writer-header-error"
                )
                finding["rejected"].append(item)
                continue

            if header_errors or errors:
                item["outcome"] = "rejected"
                item["reason"] = "header-error" if header_errors else "row-error"
                finding["rejected"].append(item)
                continue

            if base is None:
                item["outcome"] = "not-certifiable"
                item["reason"] = "no-baseline"
                item["valuePreserved"] = None
                item["relationshipPreserved"] = None
                finding["not-certifiable"].append(item)
                continue

            baseline_errors = base.get("headerErrors") or base.get("rowErrors")
            if baseline_errors:
                item["outcome"] = "not-certifiable"
                item["reason"] = "baseline-invalid"
                item["valuePreserved"] = None
                item["relationshipPreserved"] = None
                item["baselineErrors"] = baseline_errors
                finding["not-certifiable"].append(item)
                continue

            changes, field_evidence = _compare_values(base.get("read"), row)
            relationship_changes = _compare_relationships(
                baseline_relationships,
                observed_relationships,
                table,
                record.id,
            )
            item["valueChanges"] = changes
            item["fieldEvidence"] = field_evidence
            item["relationshipChanges"] = relationship_changes
            item["valuePreserved"] = not changes
            item["relationshipPreserved"] = not relationship_changes

            record_uncovered = _record_uncovered(contract, table, record, uncovered)
            item["uncoveredConstraints"] = record_uncovered
            if changes or relationship_changes:
                item["outcome"] = "changed"
                if changes and relationship_changes:
                    item["reason"] = "value-and-relationship-changed"
                elif changes:
                    item["reason"] = "value-changed"
                else:
                    item["reason"] = "relationship-changed"
                finding["changed"].append(item)
            elif record_uncovered:
                item["outcome"] = "not-certifiable"
                item["reason"] = "custom-constraint-not-covered"
                finding["not-certifiable"].append(item)
            else:
                item["outcome"] = "compatible"
                item["reason"] = "values-and-relationship-preserved"
                finding["compatible"].append(item)

    finding["relationships"] = observed_relationships
    finding["writeFailures"] = write_failures
    finding["certified"] = _certified_range(finding, uncovered, contract)
    _finish(finding)
    return finding


def _finish(finding: types.IDescriptor) -> None:
    counts = {outcome: len(finding.get(outcome, [])) for outcome in OUTCOMES}
    finding["summary"] = counts
    for outcome in ("rejected", "changed", "not-certifiable", "compatible"):
        if counts[outcome]:
            finding["status"] = outcome
            return
    finding["status"] = "unknown"


# Read / write


def _get_schema(package: Optional[Package], table: str) -> Any:
    if not package:
        raise FrictionlessException(
            errors.CompatibilityError(note="package is not available")
        )
    try:
        resource = package.get_table_resource(table)
    except FrictionlessException as exception:
        raise FrictionlessException(
            errors.CompatibilityError(
                note=f'table "{table}" is not a table resource in the package: '
                f"{exception.error.note}"
            )
        ) from exception
    if not resource.schema:
        raise FrictionlessException(
            errors.CompatibilityError(note=f'table "{table}" has no schema')
        )
    return resource.schema


def _replay_resource(source: Any, data: Any, **options: Any) -> TableResource:
    """Build a replay resource from another one, replacing its data source.

    `to_copy` hard-codes the source data, so the descriptor is rebuilt from
    the source's schema and dialect instead.
    """
    return TableResource(
        name=source.name,
        data=data,
        schema=source.schema.to_copy(),
        dialect=source.dialect.to_copy(),
        detector=source.detector,
        datapackage_version=getattr(source, "_datapackage_version", None),
        **options,
    )


def _writer_run(
    info: Any, table: str, data: List[Any]
) -> Tuple[types.IDescriptor, bytes, List[List[Any]]]:
    """Read and write replay rows with the writer's own resource.

    Returns the writer's reading (its row errors and effective values), the
    bytes it emits, and those bytes parsed back into header and cells.
    """
    # Foreign keys do not affect writing and would read the writer's original
    # data rather than the replay.
    source = info.package.get_table_resource(table)
    writer = _replay_resource(source, data)
    writer.schema = writer.schema.to_copy()
    writer.schema.foreign_keys = []

    read: types.IDescriptor = {"rows": [], "rowErrors": [], "headerErrors": []}
    try:
        with writer:
            read["headerErrors"] = [
                error.to_descriptor() for error in writer.header.errors
            ]
            for row in writer.row_stream:
                read["rows"].append(_jsonable(row.to_dict()))
                read["rowErrors"].append(
                    [error.to_descriptor() for error in row.errors]
                )
    except FrictionlessException as exception:
        read["headerErrors"] = [
            error.to_descriptor() for error in exception.to_errors()
        ]

    # Write a fresh copy, because reading consumed the first one.
    source = info.package.get_table_resource(table)
    writer = _replay_resource(source, data)
    writer.schema = writer.schema.to_copy()
    writer.schema.foreign_keys = []
    target = TableResource(name=table, scheme="buffer", format="csv")
    write_errors: List[types.IDescriptor] = []
    try:
        with writer:
            writer.write(target)
    except FrictionlessException as exception:
        write_errors = [error.to_descriptor() for error in exception.to_errors()]
    payload = target.data if isinstance(target.data, bytes) else b""
    parsed = _parse_csv(payload)
    header = parsed[0] if parsed else []
    rows = parsed[1:] if parsed else []
    if write_errors:
        read["headerErrors"] = write_errors
    return read, payload, [header] + rows


def _parse_csv(payload: bytes) -> List[List[Any]]:
    text = payload.decode("utf-8", errors="replace")
    return [list(row) for row in csv.reader(io.StringIO(text))]


def _wire_evidence(
    schema: Any,
    record: CandidateRecord,
    input_cells: List[Any],
    wire_cells: List[Any],
    row_errors: List[types.IDescriptor],
    writer_header_errors: List[types.IDescriptor],
) -> types.IDescriptor:
    changes: List[types.IDescriptor] = []
    for index, field in enumerate(schema.fields):
        original = input_cells[index] if index < len(input_cells) else None
        wire = wire_cells[index] if index < len(wire_cells) else ABSENT
        if field.name not in record.explicit_fields:
            changes.append(
                {
                    "field": field.name,
                    "kind": "absent-field-materialized",
                    "original": ABSENT,
                    "wire": _jsonable(wire),
                }
            )
        elif not _wire_equal(original, wire):
            changes.append(
                {
                    "field": field.name,
                    "kind": "wire-value-rewritten",
                    "original": _jsonable(original),
                    "wire": _jsonable(wire),
                }
            )
    schema_names = {field.name for field in schema.fields}
    for name in sorted(record.explicit_fields - schema_names):
        changes.append(
            {
                "field": name,
                "kind": "field-dropped-on-write",
                "original": _jsonable(record.values.get(name)),
                "wire": ABSENT,
            }
        )
    return {
        "input": input_cells,
        "wireHeader": [field.name for field in schema.fields],
        "wire": [_jsonable(cell) for cell in wire_cells],
        "writeErrors": row_errors,
        "writerHeaderErrors": writer_header_errors,
        "writeChanges": changes,
    }


def _wire_equal(original: Any, wire: Any) -> bool:
    if original is None:
        return wire in ("", None)
    return str(original) == str(wire)


def _read_baseline(contract: CompatibilityContract) -> types.IDescriptor:
    info = contract.old
    if info.errors or not info.package:
        return {
            "status": "unavailable",
            "errors": info.errors,
            "records": [],
            "relationships": [],
        }

    payloads: Dict[str, List[Any]] = {}
    for table, records in contract.records.items():
        try:
            schema = _get_schema(info.package, table)
        except FrictionlessException as exception:
            return {
                "status": "unavailable",
                "errors": [exception.error.to_descriptor()],
                "records": [],
                "relationships": [],
            }
        data: List[Any] = [list(schema.field_names)]
        for record in records:
            data.append(
                [
                    record.values.get(name) if name in record.explicit_fields else None
                    for name in schema.field_names
                ]
            )
        payloads[table] = data

    observed = _read_package(info, payloads, kind="inline")
    if observed["status"] != "read":
        return {
            "status": observed["status"],
            "errors": observed["errors"],
            "records": [],
            "relationships": [],
        }

    records: List[types.IDescriptor] = []
    for table, candidates in contract.records.items():
        table_data = observed["tables"].get(table, {})
        table_rows = table_data.get("rows", [])
        table_row_errors = table_data.get("rowErrors", [])
        for index, record in enumerate(candidates):
            records.append(
                {
                    "table": table,
                    "record": record.id,
                    "original": record.to_descriptor(),
                    "read": table_rows[index] if index < len(table_rows) else None,
                    "headerErrors": table_data.get("headerErrors", []),
                    "rowErrors": (
                        table_row_errors[index]
                        if index < len(table_row_errors)
                        else []
                    ),
                }
            )
    return {
        "status": "read",
        "errors": [],
        "records": records,
        "relationships": _reconcile(contract, observed),
    }


def _read_package(
    info: Any,
    payloads: Dict[str, Any],
    *,
    kind: str,
) -> types.IDescriptor:
    """Read replay data through the reader's package without mutating it.

    `kind="inline"` reads the raw values as typed inline data (the baseline);
    `kind="bytes"` reads the actual CSV bytes a writer produced.
    """
    resources: List[Any] = []
    for resource in info.package.resources:
        name = resource.name
        if name not in payloads:
            resources.append(resource.to_copy())
            continue
        if kind == "bytes":
            resources.append(
                _replay_resource(
                    resource, payloads[name], scheme="buffer", format="csv"
                )
            )
        else:
            resources.append(_replay_resource(resource, payloads[name]))
    package = Package(resources=resources)

    result: types.IDescriptor = {"status": "read", "errors": [], "tables": {}}
    for table in payloads:
        result["tables"][table] = _read_table(package, table)
    return result


def _read_table(package: Package, table: str) -> types.IDescriptor:
    table_result: types.IDescriptor = {
        "rows": [],
        "rowErrors": [],
        "headerErrors": [],
        "labels": [],
    }
    try:
        resource = package.get_table_resource(table)
    except FrictionlessException as exception:
        table_result["status"] = "unreadable"
        table_result["headerErrors"] = [exception.error.to_descriptor()]
        return table_result
    try:
        with resource:
            table_result["labels"] = list(resource.header.labels)
            table_result["headerErrors"] = [
                error.to_descriptor() for error in resource.header.errors
            ]
            for row in resource.row_stream:
                table_result["rows"].append(_jsonable(row.to_dict()))
                table_result["rowErrors"].append(
                    [error.to_descriptor() for error in row.errors]
                )
    except FrictionlessException as exception:
        table_result["status"] = "unreadable"
        table_result["headerErrors"] = [
            error.to_descriptor() for error in exception.to_errors()
        ]
    except Exception as exception:
        table_result["status"] = "unreadable"
        table_result["headerErrors"] = [{"type": "read-error", "note": str(exception)}]
    return table_result


# Value comparison


def _compare_values(
    baseline: Optional[types.IDescriptor], observed: Optional[types.IDescriptor]
) -> Tuple[List[types.IDescriptor], List[types.IDescriptor]]:
    """Compare effective values, splitting loss from schema-shape evidence.

    A field the reader adds with no value, or a baseline field that was
    already null and is dropped, is recorded as `fieldEvidence` but is not a
    value change: it does not alter an original valid value.
    """
    if baseline is None or observed is None:
        return [{"field": None, "kind": "record-not-read"}], []
    changes: List[types.IDescriptor] = []
    evidence: List[types.IDescriptor] = []
    for field, base_value in baseline.items():
        if field not in observed:
            item = {
                "field": field,
                "kind": "field-dropped",
                "baseline": _jsonable(base_value),
                "observed": ABSENT,
            }
            (changes if base_value is not None else evidence).append(item)
        elif not _equal(base_value, observed.get(field)):
            observed_value = observed.get(field)
            kind = "value-changed"
            if base_value is not None and observed_value is None:
                kind = "value-became-missing"
            elif base_value is None and observed_value is not None:
                kind = "missing-became-value"
            changes.append(
                {
                    "field": field,
                    "kind": kind,
                    "baseline": _jsonable(base_value),
                    "observed": _jsonable(observed_value),
                }
            )
    for field, observed_value in observed.items():
        if field not in baseline:
            evidence.append(
                {
                    "field": field,
                    "kind": "field-added",
                    "baseline": ABSENT,
                    "observed": _jsonable(observed_value),
                }
            )
    return changes, evidence


def _equal(left: Any, right: Any) -> bool:
    # Booleans are not numbers here: True must not equal 1.
    if isinstance(left, bool) or isinstance(right, bool):
        return left is right
    if isinstance(left, (int, float, Decimal)) and isinstance(
        right, (int, float, Decimal)
    ):
        return left == right
    return left == right


# Relationships


def _reconcile(
    contract: CompatibilityContract, observed: types.IDescriptor
) -> List[types.IDescriptor]:
    result: List[types.IDescriptor] = []
    tables = observed.get("tables", {})
    for relation in contract.relationships:
        source = relation["source"]
        target = relation["target"]
        source_fields = relation["sourceFields"]
        target_fields = relation["targetFields"]
        source_rows = tables.get(source, {}).get("rows", [])
        target_rows = tables.get(target, {}).get("rows", [])
        target_keys = {
            tuple(row.get(field) for field in target_fields)
            for row in target_rows
            if not _all_none(tuple(row.get(field) for field in target_fields))
        }
        record_ids = [record.id for record in contract.records.get(source, [])]
        entries: List[types.IDescriptor] = []
        for index, row in enumerate(source_rows):
            key = tuple(row.get(field) for field in source_fields)
            if _all_none(key):
                state = "unassociated"
            else:
                state = "linked" if key in target_keys else "orphan"
            entries.append(
                {
                    "record": record_ids[index] if index < len(record_ids) else None,
                    "row": index + 1,
                    "key": [_jsonable(value) for value in key],
                    "state": state,
                }
            )
        result.append(
            {
                "name": relation.get("name"),
                "source": source,
                "sourceFields": source_fields,
                "target": target,
                "targetFields": target_fields,
                "targetKeyCount": len(target_keys),
                "entries": entries,
            }
        )
    return result


def _compare_relationships(
    baseline: Dict[str, types.IDescriptor],
    observed: List[types.IDescriptor],
    table: str,
    record_id: str,
) -> List[types.IDescriptor]:
    changes: List[types.IDescriptor] = []
    for relation in observed:
        if relation["source"] != table:
            continue
        entry = _entry(relation, record_id)
        base_relation = baseline.get(relation["name"])
        base_entry = _entry(base_relation, record_id) if base_relation else None
        if entry is None:
            continue
        if base_entry is None:
            changes.append(
                {
                    "relationship": relation["name"],
                    "kind": "no-baseline",
                    "baseline": None,
                    "observed": entry,
                }
            )
        elif base_entry["state"] != entry["state"] or base_entry["key"] != entry["key"]:
            changes.append(
                {
                    "relationship": relation["name"],
                    "kind": "relationship-changed",
                    "baseline": base_entry,
                    "observed": entry,
                }
            )
    return changes


def _entry(
    relation: Optional[types.IDescriptor], record_id: str
) -> Optional[types.IDescriptor]:
    if not relation:
        return None
    for entry in relation.get("entries", []):
        if entry.get("record") == record_id:
            return entry
    return None


# Constraints / certification


def _uncovered_constraints(
    contract: CompatibilityContract,
) -> List[types.IDescriptor]:
    uncovered: List[types.IDescriptor] = []
    for item in contract.custom_constraints:
        name = item.get("constraint") or item.get("name")
        if not name or (name in STANDARD_CONSTRAINTS and not item.get("custom")):
            continue
        table = item.get("table")
        field = item.get("field")
        domain = _constraint_domain(item, contract, table, field)
        covered = bool(item.get("covered")) and domain is not None
        if covered:
            continue
        uncovered.append(
            {
                "table": table,
                "field": field,
                "constraint": name,
                "declaredValues": _jsonable(domain) if domain else None,
                "covered": False,
                "reason": (
                    "the publisher did not declare the supplied value domain as "
                    "covering this custom constraint"
                ),
            }
        )
    return uncovered


def _constraint_domain(
    item: types.IDescriptor,
    contract: CompatibilityContract,
    table: Optional[str],
    field: Optional[str],
) -> Optional[types.IDescriptor]:
    """Normalize the domain that establishes a custom constraint's coverage.

    A list is an enumeration; a dict is a value/range declaration. A missing
    per-constraint declaration falls back to the table's field domain.
    """
    declared = item.get("declaredValues")
    if declared is None:
        declared = item.get("values")
    if declared is None:
        declared = item.get("domain")
    if declared is None and table and field:
        declared = _domain_for(contract, table, field)
    if isinstance(declared, list):
        return {"values": declared}
    if isinstance(declared, dict):
        return declared
    return None


def _record_uncovered(
    contract: CompatibilityContract,
    table: str,
    record: CandidateRecord,
    uncovered: List[types.IDescriptor],
) -> List[types.IDescriptor]:
    result: List[types.IDescriptor] = []
    for item in uncovered:
        if item["table"] != table:
            continue
        field = item["field"]
        value = record.values.get(field) if field in record.explicit_fields else None
        if not item.get("covered"):
            result.append(
                {
                    **item,
                    "value": _jsonable(value),
                    "fieldPresent": field in record.explicit_fields,
                }
            )
            continue
        domain = item.get("declaredValues")
        if isinstance(domain, list):
            domain = {"values": domain}
        if not isinstance(domain, dict):
            domain = _domain_for(contract, table, field) if field else None
        covered_by_domain = (
            isinstance(domain, dict)
            and field in record.explicit_fields
            and _value_in_domain(value, domain)
        )
        if covered_by_domain:
            continue
        result.append(
            {
                **item,
                "value": _jsonable(value),
                "fieldPresent": field in record.explicit_fields,
                "reason": (
                    "custom constraint not certified: the record value is absent "
                    "or outside the supplied value domain"
                ),
            }
        )
    return result


def _domain_for(
    contract: CompatibilityContract, table: str, field: str
) -> Optional[types.IDescriptor]:
    table_domain = contract.domains.get(table)
    if not isinstance(table_domain, dict):
        return None
    domain = table_domain.get(field)
    return domain if isinstance(domain, dict) else None


def _value_in_domain(value: Any, domain: types.IDescriptor) -> bool:
    values = domain.get("values")
    if values is not None:
        if _jsonable(value) not in [_jsonable(item) for item in values]:
            return False
    minimum = domain.get("minimum")
    maximum = domain.get("maximum")
    if minimum is not None:
        number = _numeric(value)
        if number is None or number < _numeric(minimum):
            return False
    if maximum is not None:
        number = _numeric(value)
        if number is None or number > _numeric(maximum):
            return False
    min_length = domain.get("minLength")
    max_length = domain.get("maxLength")
    if min_length is not None and isinstance(value, str) and len(value) < min_length:
        return False
    if max_length is not None and isinstance(value, str) and len(value) > max_length:
        return False
    pattern = domain.get("pattern")
    if pattern and isinstance(value, str) and not re.search(pattern, value):
        return False
    return True


def _numeric(value: Any) -> Optional[Decimal]:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float, Decimal)):
        return Decimal(str(value))
    if isinstance(value, str):
        try:
            return Decimal(value)
        except Exception:
            return None
    return None


def _certified_range(
    finding: types.IDescriptor,
    uncovered: List[types.IDescriptor],
    contract: CompatibilityContract,
) -> types.IDescriptor:
    records = [
        {"table": item["table"], "record": item["record"]}
        for item in finding["compatible"]
    ]
    certified_keys = {(item["table"], item["record"]) for item in records}
    fields: Dict[str, List[str]] = {}
    values: Dict[str, List[Any]] = {}
    for table, table_records in contract.records.items():
        for record in table_records:
            if (table, record.id) not in certified_keys:
                continue
            for field, value in record.values.items():
                fields.setdefault(table, [])
                if field not in fields[table]:
                    fields[table].append(field)
                key = f"{table}.{field}"
                values.setdefault(key, [])
                item = _jsonable(value)
                if item not in values[key]:
                    values[key].append(item)
    relationships = [
        relation["name"]
        for relation in finding.get("relationships", [])
        if relation.get("entries")
    ]
    return {
        "certifiable": bool(records) and not uncovered,
        "directionCompatible": not (
            finding["rejected"] or finding["changed"] or finding["not-certifiable"]
        ),
        "records": records,
        "tables": sorted({item["table"] for item in records}),
        "fields": fields,
        "values": values,
        "domains": _jsonable(contract.domains),
        "relationships": relationships,
        "uncoveredConstraints": uncovered,
        "scope": (
            "only the listed records and field values within the supplied value "
            "domains; constraints outside those domains are not certified. A "
            "clean row read is not by itself evidence of compatibility: the "
            "listed records also preserve value and relationship outcomes"
        ),
        "noEvidenceStatement": (
            "No evidence was supplied for custom constraints outside the "
            "provided value domains: compatibility cannot be certified from "
            "these inputs."
        ),
    }


def _all_none(values: Tuple[Any, ...]) -> bool:
    return all(value is None for value in values)
