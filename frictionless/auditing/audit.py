from __future__ import annotations

from typing import Any, Dict, List, Optional, Union

from ..exception import FrictionlessException
from ..package import Package
from .declaration import AuditDeclaration
from .engine import Snapshot, TableAuditor, jsonable
from .normalize import normalize_value


def audit(
    old: Union[str, Package],
    new: Union[str, Package],
    *,
    declaration: Union[Dict[str, Any], str, AuditDeclaration],
) -> Dict[str, Any]:
    """Audit the changes between two Data Package snapshots.

    The caller supplies the business-key correspondence for each version, the
    value-equivalence rules, and the verified snapshot coverage. The result is
    a JSON-serialisable report where every change or unresolved record points
    back to the actual rows and column declarations it used.

    Parameters:
        old: the old Data Package (path or Package)
        new: the new Data Package (path or Package)
        declaration: the audit declaration (mapping, JSON/YAML path, or object)
    """
    audit_declaration = _coerce_declaration(declaration)
    old_package = _coerce_package(old)
    new_package = _coerce_package(new)

    results: List[Dict[str, Any]] = []
    snapshots: Dict[str, Dict[str, Snapshot]] = {}
    for name, table in audit_declaration.tables.items():
        old_snapshot = Snapshot(table, "old", old_package)
        new_snapshot = Snapshot(table, "new", new_package)
        snapshots[name] = {"old": old_snapshot, "new": new_snapshot}
        results.append(
            TableAuditor(audit_declaration, name).run(old_snapshot, new_snapshot)
        )

    by_name = {result["table"]: result for result in results}
    references = _propagate_references(audit_declaration, snapshots, by_name)

    report: Dict[str, Any] = {
        "version": 1,
        "declaration": audit_declaration.descriptor,
        "snapshots": {
            name: {
                side: snapshot.to_descriptor()
                for side, snapshot in snapshots[name].items()
            }
            for name in audit_declaration.tables
        },
        "tables": results,
        "references": references,
        "summary": _summary(results, references),
    }
    return report


def _coerce_declaration(
    declaration: Union[Dict[str, Any], str, AuditDeclaration],
) -> AuditDeclaration:
    if isinstance(declaration, AuditDeclaration):
        return declaration
    if isinstance(declaration, str):
        import json
        import os

        import yaml

        if os.path.isfile(declaration):
            with open(declaration, encoding="utf-8") as file:
                if declaration.lower().endswith((".yaml", ".yml")):
                    descriptor = yaml.safe_load(file)
                else:
                    descriptor = json.load(file)
        else:
            try:
                descriptor = json.loads(declaration)
            except Exception:
                descriptor = yaml.safe_load(declaration)
        return AuditDeclaration(descriptor)
    if isinstance(declaration, dict):
        return AuditDeclaration(declaration)
    raise FrictionlessException("The audit declaration must be a mapping or a path")


def _coerce_package(source: Union[str, Package]) -> Package:
    if isinstance(source, Package):
        return source
    return Package(source)


def _propagate_references(
    declaration: AuditDeclaration,
    snapshots: Dict[str, Dict[str, Snapshot]],
    results: Dict[str, Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """Relate referencing rows to the audited state of their target key.

    A reference is declared on the *target* table and names the table that
    holds the foreign key, so the resolution walks from the target's audited
    key state back to every referencing row.
    """
    report: List[Dict[str, Any]] = []
    for target, table in declaration.tables.items():
        if not table.references:
            continue
        target_snapshots = snapshots[target]
        kinds = _target_kinds(results[target])
        target_old = _index_by_key(target_snapshots["old"])
        target_new = _index_by_key(target_snapshots["new"])
        for reference in table.references:
            holder = reference["holding"]
            holder_table = declaration.tables[holder]
            holder_snapshots = snapshots[holder]
            report.extend(
                _reference_entries(
                    holder,
                    target,
                    holder_table,
                    table,
                    reference,
                    holder_snapshots,
                    kinds,
                    target_old,
                    target_new,
                )
            )
    return report


def _reference_entries(
    holder: str,
    target: str,
    holder_table: Any,
    target_table: Any,
    reference: Dict[str, Any],
    snapshots: Dict[str, Snapshot],
    kinds: Dict[str, str],
    target_old: Dict[str, List[Any]],
    target_new: Dict[str, List[Any]],
) -> List[Dict[str, Any]]:
    entries: List[Dict[str, Any]] = []
    column_rule = holder_table.columns[reference["column"]]
    key_rule = target_table.columns[target_table.key[0]]
    for side in ("old", "new"):
        snapshot = snapshots[side]
        raw_column = column_rule.source(side)
        for row in snapshot.rows:
            raw = row.value(raw_column)
            if raw is None:
                continue
            token = str(normalize_value(raw, key_rule.normalize_for(side)))
            state = _lookup_state(token, kinds, target_old, target_new)
            if state is None:
                continue
            entries.append(
                {
                    "holdingTable": holder,
                    "targetTable": target,
                    "targetKey": token,
                    "side": side,
                    "state": state,
                    "row": {
                        "rowNumber": row.row_number,
                        "resource": snapshot.resource.name,
                        "path": snapshot.resource_path,
                        "values": {
                            column: jsonable(row.values.get(column))
                            for column in snapshot.labels
                        },
                    },
                    "declaredColumn": raw_column,
                }
            )
    return sorted(
        entries,
        key=lambda entry: (
            entry["side"],
            entry["targetKey"],
            str(entry["row"]["values"]),
        ),
    )


def _reference_token(raw: Any, rule: Any, side: str) -> str:
    from .normalize import normalize_value

    return str(normalize_value(raw, rule.normalize_for(side)))


def _index_by_key(snapshot: Snapshot) -> Dict[str, List[Any]]:
    index: Dict[str, List[Any]] = {}
    for row in snapshot.rows:
        token = snapshot.key_token(row)
        index.setdefault(token, []).append(row)
    return index


def _target_kinds(result: Dict[str, Any]) -> Dict[str, str]:
    kinds: Dict[str, str] = {}
    for change in result["changes"]:
        kinds[change["key"]["token"]] = change["kind"]
    for unresolved in result["unresolved"]:
        kinds[unresolved["key"]] = "conflict"
    for suppressed in result["suppressed"]:
        kinds.setdefault(suppressed["key"]["token"], "unchanged")
    return kinds


def _lookup_state(
    token: str,
    kinds: Dict[str, str],
    old_index: Dict[str, List[Any]],
    new_index: Dict[str, List[Any]],
) -> Optional[str]:
    if token in kinds:
        return kinds[token]
    if token in old_index and token in new_index:
        return "unchanged"
    if token in new_index:
        return "add"
    if token in old_index:
        return "delete"
    return None


def _summary(
    results: List[Dict[str, Any]], references: List[Dict[str, Any]]
) -> Dict[str, Any]:
    summary: Dict[str, Any] = {}
    for result in results:
        counts = {"add": 0, "delete": 0, "update": 0, "conflict": 0, "suppressed": 0}
        for change in result["changes"]:
            counts[change["kind"]] += 1
        counts["conflict"] = len(result["unresolved"])
        counts["suppressed"] = len(result["suppressed"])
        summary[result["table"]] = counts
    summary["references"] = {
        "total": len(references),
        "conflict": sum(1 for entry in references if entry["state"] == "conflict"),
    }
    return summary
