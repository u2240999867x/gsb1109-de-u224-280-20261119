from __future__ import annotations

from datetime import date, datetime, time
from decimal import Decimal
from typing import Any, Dict, List, Optional, Tuple

from ..exception import FrictionlessException
from ..package import Package
from .declaration import AuditDeclaration, ColumnRule, Scope, TableAudit
from .normalize import key_token, normalize_value

# Values


def jsonable(value: Any) -> Any:
    """Convert a value to something JSON can carry without losing meaning."""
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date, time)):
        return value.isoformat()
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, (list, tuple)):
        return [jsonable(entry) for entry in value]
    if isinstance(value, dict):
        return {str(key): jsonable(entry) for key, entry in value.items()}
    return str(value)


def canonical(value: Any) -> Any:
    """Canonical form used for normalized comparison."""
    value = jsonable(value)
    if isinstance(value, bool):
        return ("bool", value)
    if isinstance(value, int):
        return ("number", Decimal(value))
    if isinstance(value, float):
        return ("number", Decimal(str(value)))
    if isinstance(value, str):
        return ("string", value)
    if value is None:
        return ("null",)
    return ("other", repr(value))


def is_number(text: Any) -> Optional[Decimal]:
    try:
        return Decimal(str(text).strip())
    except Exception:
        return None


# Snapshot


class RowRecord:
    def __init__(self, *, row_number: int, values: Dict[str, Any], labels: List[str]):
        self.row_number = row_number
        self.values = values
        self.labels = labels

    def value(self, column: str) -> Any:
        return self.values.get(column)


class Snapshot:
    """The rows of one resource as read from one version of a package."""

    def __init__(self, table: TableAudit, side: str, package: Package):
        self.table = table
        self.side = side
        resource_name = table.old_resource if side == "old" else table.new_resource
        try:
            self.resource = package.get_resource(resource_name)
        except Exception:
            raise FrictionlessException(
                f'Audit table "{table.name}" declares {side} resource '
                f'"{resource_name}" but the {side} package does not contain it'
            )
        self.rows: List[RowRecord] = []
        self.labels: List[str] = []
        self.package_scope = _package_scope(package, self.resource, table.name, side)
        self._load()

    @property
    def scope(self) -> Scope:
        if (
            self.table.old_scope.declared
            if self.side == "old"
            else self.table.new_scope.declared
        ):
            return self.table.old_scope if self.side == "old" else self.table.new_scope
        if self.package_scope is not None:
            return self.package_scope
        return self.table.old_scope if self.side == "old" else self.table.new_scope

    @property
    def source_columns(self) -> List[str]:
        return self.table.source_columns(self.side)

    def _load(self) -> None:
        resource = self.resource
        with resource:
            self.labels = list(resource.header.labels or [])
            missing = self.table.missing_columns(self.side, self.labels)
            if missing:
                raise FrictionlessException(
                    f'Audit table "{self.table.name}" ({self.side}) is missing declared '
                    f"source column(s) {missing}; available columns are {self.labels}"
                )
            for row in resource.row_stream:
                labels = row.field_names
                values = dict(zip(labels, row.cells))
                # Prefer the physical header labels when the schema renames fields
                if labels != self.labels:
                    values = {label: cell for label, cell in zip(self.labels, row.cells)}
                self.rows.append(
                    RowRecord(
                        row_number=row.row_number, values=values, labels=self.labels
                    )
                )
        self.resource_path = resource.path

    # Keys

    def key_values(self, row: RowRecord) -> List[Any]:
        values = []
        for column in self.table.key_columns:
            raw = row.value(column.source(self.side))
            values.append(normalize_value(raw, column.normalize_for(self.side)))
        return values

    def key_token(self, row: RowRecord) -> str:
        return key_token(self.key_values(row))

    def key_source(self, row: RowRecord) -> List[Any]:
        return [row.value(column.source(self.side)) for column in self.table.key_columns]

    # Coverage

    def covers_key(self, key_values: List[Any], key_source: List[Any]) -> bool:
        scope = self.scope
        if scope.complete:
            return True
        if not scope.declared:
            return bool(self.table.stats)
        candidates = [str(value) for value in key_source]
        if scope.keys and any(value in scope.keys for value in candidates):
            return True
        for segment in scope.segments:
            if "keys" in segment:
                if any(
                    value in {str(entry) for entry in segment["keys"]}
                    for value in candidates
                ):
                    return True
                continue
            value = key_source[0] if key_source else None
            if _in_segment(value, segment.get("from"), segment.get("to")):
                return True
        return False

    def to_descriptor(self) -> Dict[str, Any]:
        scope = self.scope
        if scope is self.package_scope:
            source = "package"
        elif scope.declared:
            source = "declaration"
        else:
            source = "none"
        return {
            "table": self.table.name,
            "side": self.side,
            "resource": self.resource.name,
            "path": self.resource_path,
            "columns": self.labels,
            "rows": len(self.rows),
            "coverage": scope.to_descriptor(),
            "coverageSource": source,
            "stats": self.table.stats,
        }


def _in_segment(value: Any, start: Any, end: Any) -> bool:
    if start is None or end is None:
        return False
    numeric = is_number(value)
    start_number = is_number(start)
    end_number = is_number(end)
    if numeric is not None and start_number is not None and end_number is not None:
        return start_number <= numeric <= end_number
    text = "" if value is None else str(value)
    return str(start) <= text <= str(end)


# Diff


class TableAuditor:
    """Compute the audited changes of a single table."""

    def __init__(self, declaration: AuditDeclaration, table_name: str):
        self.declaration = declaration
        self.table = declaration.table(table_name)

    def run(self, old: Snapshot, new: Snapshot) -> Dict[str, Any]:
        old_groups = _group(old)
        new_groups = _group(new)
        keys = sorted(set(old_groups) | set(new_groups), key=_key_sort)

        changes: List[Dict[str, Any]] = []
        unresolved: List[Dict[str, Any]] = []
        suppressed: List[Dict[str, Any]] = []
        ambiguous: set = set()

        for token in keys:
            old_rows = old_groups.get(token, [])
            new_rows = new_groups.get(token, [])
            if len(old_rows) > 1 or len(new_rows) > 1:
                ambiguous.add(token)
                unresolved.append(self._conflict(token, old, new, old_rows, new_rows))
                continue

            old_row = old_rows[0] if old_rows else None
            new_row = new_rows[0] if new_rows else None

            if old_row and new_row:
                change = self._update(old_row, new_row, old, new)
                if change:
                    changes.append(change)
                continue

            if new_row:
                evidence = self._evidence(old, new, None, new_row)
                key_source = new.key_source(new_row)
                if not old.covers_key(new.key_values(new_row), key_source):
                    suppressed.append(
                        {
                            "table": self.table.name,
                            "kind": "add",
                            "reason": "old-coverage-unknown",
                            "key": self._key_descriptor(new, new_row),
                            "evidence": evidence,
                            "columns": self._columns_descriptor(),
                        }
                    )
                    continue
                changes.append(
                    {
                        "table": self.table.name,
                        "kind": "add",
                        "key": self._key_descriptor(new, new_row),
                        "changed": self._added_columns(new_row, new),
                        "evidence": evidence,
                        "columns": self._columns_descriptor(),
                    }
                )
                continue

            if old_row:
                evidence = self._evidence(old, new, old_row, None)
                key_source = old.key_source(old_row)
                if not new.covers_key(old.key_values(old_row), key_source):
                    suppressed.append(
                        {
                            "table": self.table.name,
                            "kind": "delete",
                            "reason": "new-coverage-unknown",
                            "key": self._key_descriptor(old, old_row),
                            "evidence": evidence,
                            "columns": self._columns_descriptor(),
                        }
                    )
                    continue
                changes.append(
                    {
                        "table": self.table.name,
                        "kind": "delete",
                        "key": self._key_descriptor(old, old_row),
                        "changed": self._deleted_columns(old_row, old),
                        "evidence": evidence,
                        "columns": self._columns_descriptor(),
                    }
                )

        return {
            "table": self.table.name,
            "declaration": self.table.to_descriptor(),
            "changes": changes,
            "unresolved": unresolved,
            "suppressed": suppressed,
            "ambiguous_keys": sorted(ambiguous),
        }

    # Changes

    def _update(
        self, old_row: RowRecord, new_row: RowRecord, old: Snapshot, new: Snapshot
    ) -> Optional[Dict[str, Any]]:
        changed: Dict[str, Any] = {}
        for name in self.table.value_names:
            rule = self.table.columns[name]
            old_raw = old_row.value(rule.old)
            new_raw = new_row.value(rule.new)
            if self._equal(rule, old_raw, new_raw):
                continue
            changed[name] = self._change_detail(old_raw, new_raw, rule)
        key_changed = self._key_source_changed(old_row, new_row)
        if not changed and not key_changed:
            return None
        evidence = self._evidence(old, new, old_row, new_row)
        result = {
            "table": self.table.name,
            "kind": "update",
            "key": self._key_descriptor(new, new_row),
            "changed": changed,
            "evidence": evidence,
            "columns": self._columns_descriptor(),
        }
        if key_changed:
            result["keyChange"] = {
                "old": self._key_source(old, old_row),
                "new": self._key_source(new, new_row),
                "normalizedEqual": True,
            }
        return result

    def _equal(self, rule: ColumnRule, old_raw: Any, new_raw: Any) -> bool:
        """Decide equality on the declared comparison basis only.

        Under "source" the verbatim values must match; under "normalized" the
        per-side normalized values must match. A lexical difference that the
        declaration folds away is never reported on the source basis.
        """
        if rule.compare == "source":
            return canonical(old_raw) == canonical(new_raw)
        old_value = normalize_value(old_raw, rule.normalize_for("old"))
        new_value = normalize_value(new_raw, rule.normalize_for("new"))
        return canonical(old_value) == canonical(new_value)

    def _change_detail(
        self, old_raw: Any, new_raw: Any, rule: ColumnRule
    ) -> Dict[str, Any]:
        detail = {
            "old": jsonable(old_raw),
            "new": jsonable(new_raw),
            "compare": rule.compare,
        }
        if rule.compare == "normalized":
            detail["oldNormalized"] = jsonable(
                normalize_value(old_raw, rule.normalize_for("old"))
            )
            detail["newNormalized"] = jsonable(
                normalize_value(new_raw, rule.normalize_for("new"))
            )
        return detail

    def _added_columns(self, row: RowRecord, snapshot: Snapshot) -> Dict[str, Any]:
        result = {}
        for name in self.table.value_names:
            rule = self.table.columns[name]
            result[name] = {
                "new": jsonable(row.value(rule.new)),
                "compare": rule.compare,
            }
        return result

    def _deleted_columns(self, row: RowRecord, snapshot: Snapshot) -> Dict[str, Any]:
        result = {}
        for name in self.table.value_names:
            rule = self.table.columns[name]
            result[name] = {
                "old": jsonable(row.value(rule.old)),
                "compare": rule.compare,
            }
        return result

    def _key_source_changed(self, old_row: RowRecord, new_row: RowRecord) -> bool:
        for column in self.table.key_columns:
            if canonical(old_row.value(column.old)) != canonical(
                new_row.value(column.new)
            ):
                return True
        return False

    # Conflicts

    def _conflict(
        self,
        token: str,
        old: Snapshot,
        new: Snapshot,
        old_rows: List[RowRecord],
        new_rows: List[RowRecord],
    ) -> Dict[str, Any]:
        return {
            "table": self.table.name,
            "kind": "conflict",
            "key": key_token([token]),
            "reason": "duplicate-key",
            "candidates": {
                "old": [
                    self._candidate(old, row) for row in _order_candidates(old, old_rows)
                ],
                "new": [
                    self._candidate(new, row) for row in _order_candidates(new, new_rows)
                ],
            },
            "evidence": {
                "old": [
                    evidence_row(old, row) for row in _order_candidates(old, old_rows)
                ],
                "new": [
                    evidence_row(new, row) for row in _order_candidates(new, new_rows)
                ],
            },
            "columns": self._columns_descriptor(),
        }

    def _candidate(self, snapshot: Snapshot, row: RowRecord) -> Dict[str, Any]:
        return {
            "row": evidence_row(snapshot, row),
            "key": self._key_descriptor(snapshot, row),
            "values": {
                name: jsonable(row.value(self.table.columns[name].source(snapshot.side)))
                for name in self.table.value_names
            },
        }

    # Descriptors

    def _key_descriptor(self, snapshot: Snapshot, row: RowRecord) -> Dict[str, Any]:
        return {
            "token": snapshot.key_token(row),
            "values": {
                name: jsonable(row.value(self.table.columns[name].source(snapshot.side)))
                for name in self.table.key
            },
            "normalized": [jsonable(value) for value in snapshot.key_values(row)],
        }

    def _key_source(self, snapshot: Snapshot, row: RowRecord) -> Dict[str, Any]:
        return {
            name: jsonable(row.value(self.table.columns[name].source(snapshot.side)))
            for name in self.table.key
        }

    def _evidence(
        self,
        old: Snapshot,
        new: Snapshot,
        old_row: Optional[RowRecord],
        new_row: Optional[RowRecord],
    ) -> Dict[str, Any]:
        return {
            "old": evidence_row(old, old_row) if old_row else None,
            "new": evidence_row(new, new_row) if new_row else None,
        }

    def _columns_descriptor(self) -> Dict[str, Any]:
        return {
            name: {
                "old": rule.old,
                "new": rule.new,
                "compare": rule.compare,
                "normalize": {
                    "old": rule.normalize_for("old"),
                    "new": rule.normalize_for("new"),
                },
            }
            for name, rule in self.table.columns.items()
        }


# Helpers


def evidence_row(
    snapshot: Snapshot, row: Optional[RowRecord]
) -> Optional[Dict[str, Any]]:
    if row is None:
        return None
    result = {
        "rowNumber": row.row_number,
        "resource": snapshot.resource.name,
        "path": snapshot.resource_path,
        "values": {
            column: jsonable(row.values.get(column)) for column in snapshot.labels
        },
    }
    return result


def _group(snapshot: Snapshot) -> Dict[str, List[RowRecord]]:
    groups: Dict[str, List[RowRecord]] = {}
    for row in snapshot.rows:
        token = snapshot.key_token(row)
        groups.setdefault(token, []).append(row)
    return groups


def _key_sort(token: str) -> Tuple[str, ...]:
    return tuple(token.split("\x1f"))


def _package_scope(
    package: Package, resource: Any, table: str, side: str
) -> Optional[Scope]:
    """Read any coverage the package itself declares for this resource."""
    candidates: List[Any] = []
    for metadata in (getattr(resource, "custom", None), getattr(package, "custom", None)):
        if not isinstance(metadata, dict):
            continue
        for container in ("audit", "x-audit"):
            audit = metadata.get(container)
            if not isinstance(audit, dict):
                continue
            coverage = audit.get("coverage", audit.get("scope"))
            if not isinstance(coverage, dict):
                continue
            entry = coverage.get(resource.name, coverage.get(table))
            if isinstance(entry, dict):
                candidates.append(entry.get(side, entry))
    for candidate in candidates:
        if isinstance(candidate, dict):
            return Scope(candidate, where=f"package coverage for {resource.name}")
    return None


def _order_candidates(snapshot: Snapshot, rows: List[RowRecord]) -> List[RowRecord]:
    """Return candidates in a stable, content-derived order.

    Two rows that collapse onto one normalized key may arrive in any physical
    order; the evidence must not depend on that order, only on row content.
    """
    return sorted(
        rows,
        key=lambda row: (
            tuple(str(value) for value in snapshot.key_source(row)),
            tuple(
                "" if value is None else str(value)
                for value in (row.values.get(label) for label in snapshot.labels)
            ),
        ),
    )
