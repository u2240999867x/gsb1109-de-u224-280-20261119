"""
Preflight checks for migrating a table into a SQLite archive.

The checks combine ``table_info`` metadata from the source and target with a
finite set of sample rows.  Declared types are recorded but are never treated
as proof of value fidelity: SQLite applies type affinity, so a value written to
a ``NUMERIC`` column can be read back as a float even when both tables declare
the same type.

The module does not write to the source connection.  Round-trip checks are made
against an in-memory copy of the target table definition.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, Optional, Sequence

from etlhelper.utils import Column, table_info


DIRECT_SAFE = "direct_safe"
COUNTEREXAMPLE = "counterexample"
INSUFFICIENT_EVIDENCE = "insufficient_evidence"

NUMERIC_ROLE = "numeric"
BUSINESS_KEY_ROLE = "business_key"
REQUIRED_ROLE = "required"

_SAMPLE_SCOPE = "supplied sample rows only; no guarantee for other inputs"


@dataclass(frozen=True)
class Evidence:
    """
    Extra evidence supplied by the user.

    :param business_key_text_column: target TEXT column that stores the
        business number without numeric coercion.
    :param numeric_text_column: target TEXT column that stores exact decimal
        strings.
    :param conversion_contract: description of a target conversion contract
        that guarantees exact values (e.g. exact decimal strings read back as
        text).  A declared type on its own is not a contract.
    :param default_values: known default expression/value for target columns.
    :param defaults_complete: whether ``default_values`` covers every omitted
        NOT NULL target column and is known to be complete.
    """
    business_key_text_column: Optional[str] = None
    numeric_text_column: Optional[str] = None
    conversion_contract: Optional[str] = None
    default_values: dict[str, Any] = field(default_factory=dict)
    defaults_complete: bool = False


@dataclass(frozen=True)
class ColumnAssessment:
    """Assessment for one source or target column."""
    column: str
    role: str
    verdict: str
    reason: str
    counterexample: Optional[dict] = None
    missing_evidence: tuple[str, ...] = ()
    scope: str = _SAMPLE_SCOPE


@dataclass
class PreflightReport:
    """Result of a migration preflight."""
    source_table: str
    target_table: str
    source_columns: list[Column]
    target_columns: list[Column]
    mapping: dict[str, Optional[str]]
    sample_count: int
    assessments: list[ColumnAssessment] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    evidence: Evidence = field(default_factory=Evidence)
    changes: list[dict] = field(default_factory=list)
    context: dict = field(default_factory=dict, repr=False, compare=False)

    def for_column(self, name: str) -> Optional[ColumnAssessment]:
        """Return the first assessment for a column."""
        for assessment in self.assessments:
            if assessment.column == name:
                return assessment
        return None

    def verdict_for(self, name: str) -> Optional[str]:
        """Return the verdict for a column, or None if it wasn't assessed."""
        assessment = self.for_column(name)
        return assessment.verdict if assessment else None

    @property
    def overall_verdict(self) -> str:
        """Return the most severe verdict in the report."""
        verdicts = {a.verdict for a in self.assessments}
        if COUNTEREXAMPLE in verdicts:
            return COUNTEREXAMPLE
        if INSUFFICIENT_EVIDENCE in verdicts:
            return INSUFFICIENT_EVIDENCE
        return DIRECT_SAFE

    def to_dict(self) -> dict:
        """Return a JSON-friendly representation."""
        return {
            "source_table": self.source_table,
            "target_table": self.target_table,
            "sample_count": self.sample_count,
            "mapping": self.mapping,
            "overall_verdict": self.overall_verdict,
            "assessments": [
                {
                    "column": a.column,
                    "role": a.role,
                    "verdict": a.verdict,
                    "reason": a.reason,
                    "counterexample": a.counterexample,
                    "missing_evidence": list(a.missing_evidence),
                    "scope": a.scope,
                }
                for a in self.assessments
            ],
            "notes": self.notes,
            "changes": self.changes,
        }


def assess_migration(
        source_conn,
        source_table: str,
        target_conn,
        target_table: str,
        samples: Sequence[Mapping[str, Any]],
        mapping: Optional[dict[str, Optional[str]]] = None,
        evidence: Optional[Evidence] = None,
        roles: Optional[dict[str, str]] = None,
        ) -> PreflightReport:
    """
    Compare source and target metadata and finite samples for a migration.

    :param source_conn: DBAPI connection for the legacy source.  It is only
        read; no rows are inserted, updated or deleted.
    :param source_table: source table name.
    :param target_conn: DBAPI connection for the target.
    :param target_table: target table name.
    :param samples: exact sample rows as dictionaries keyed by source column.
        High-precision decimals should be supplied as strings so that the
        sample itself does not lose precision.
    :param mapping: ``{target_column: source_column}``.  ``None`` values mark
        target columns deliberately omitted by the candidate mapping.  Defaults
        to an identity mapping for columns present on both sides.
    :param evidence: extra evidence for text columns, conversion contracts or
        target defaults.
    :param roles: ``{source_column: role}`` where role is ``numeric`` or
        ``business_key``.  Inferred from declared types when omitted
        (numeric-to-numeric becomes ``numeric``; text-to-numeric becomes
        ``business_key``).
    :returns: a :class:`PreflightReport`.
    """
    evidence = evidence or Evidence()
    samples = list(samples)
    source_columns = table_info(source_table, source_conn)
    target_columns = table_info(target_table, target_conn)
    target_by_name = {c.name: c for c in target_columns}

    if mapping is None:
        source_names = {c.name for c in source_columns}
        mapping = {
            c.name: (c.name if c.name in source_names else None)
            for c in target_columns
        }
    else:
        mapping = dict(mapping)

    if roles is None:
        roles = _infer_roles(source_columns, target_columns)
    else:
        roles = dict(roles)

    notes: list[str] = []
    mapping = _apply_evidence(mapping, roles, target_by_name, evidence, notes)
    notes.extend(_metadata_notes(source_columns, target_columns, mapping))

    results = _round_trip(target_conn, target_table, samples, mapping,
                          target_columns)
    if results is None:
        notes.append(
            "Target is not a SQLite connection; round-trip checks were not "
            "performed and sample-based verdicts cannot be confirmed."
        )
        results = [{"index": i, "readback": {}, "error": None}
                   for i in range(len(samples))]

    reverse_mapping = {s: t for t, s in mapping.items() if s is not None}
    assessments: list[ColumnAssessment] = []

    for source_name, role in roles.items():
        target_name = reverse_mapping.get(source_name)
        if target_name is None:
            if any(c.name == source_name for c in source_columns):
                notes.append(
                    f"Source column '{source_name}' is not mapped to a "
                    "target column and was not assessed.")
            continue
        target_column = target_by_name[target_name]

        if role == NUMERIC_ROLE:
            assessments.append(_assess_numeric(
                source_name, target_name, target_column, samples, results,
                evidence))
        elif role == BUSINESS_KEY_ROLE:
            assessments.append(_assess_business_key(
                source_name, target_name, target_column, samples, results,
                evidence))
        else:
            assessments.append(_assess_mapped_text(
                source_name, target_name, samples, results))

    for target_column in target_columns:
        if mapping.get(target_column.name) is not None:
            continue

        assessment = _assess_required_omitted(
            target_column, evidence)
        if assessment:
            assessments.append(assessment)

    if NUMERIC_ROLE not in roles.values():
        notes.append(
            "No numeric column was assessed; supply roles to check exact "
            "numeric fidelity for other columns.")

    report = PreflightReport(
        source_table=source_table,
        target_table=target_table,
        source_columns=source_columns,
        target_columns=target_columns,
        mapping=mapping,
        sample_count=len(samples),
        assessments=assessments,
        notes=notes,
        evidence=evidence,
        context={
            "source_conn": source_conn,
            "source_table": source_table,
            "target_conn": target_conn,
            "target_table": target_table,
            "samples": samples,
            "mapping": mapping,
            "roles": roles,
        },
    )
    report.notes.append(
        f"Verdicts are limited to the {len(samples)} supplied sample rows "
        "unless a text-storage or conversion contract says otherwise."
    )
    return report


def reevaluate(report: PreflightReport, **changes) -> PreflightReport:
    """
    Re-run a preflight with changed evidence and record what changed.

    Keyword arguments are :class:`Evidence` fields.  ``evidence`` can be
    supplied as a complete replacement.  ``revoke_default_evidence=True``
    removes the target default evidence; it does not add any new evidence.
    ``mapping``, ``roles`` and ``samples`` can also be overridden when the new
    evidence requires a different candidate mapping.
    """
    context = dict(report.context)
    evidence = report.evidence
    if "evidence" in changes:
        evidence = changes.pop("evidence")
    else:
        updates = {}
        revoke = changes.pop("revoke_default_evidence", False)
        for key in ("mapping", "roles", "samples"):
            if key in changes:
                context[key] = changes.pop(key)
        for key, value in changes.items():
            if not hasattr(evidence, key):
                raise TypeError(f"Unknown evidence field: {key}")
            updates[key] = value
        if revoke:
            updates["defaults_complete"] = False
            updates["default_values"] = {}
        evidence = replace(evidence, **updates)

    new_report = assess_migration(evidence=evidence, **context)
    new_report.changes = _diff_reports(report, new_report, evidence)
    return new_report


def compare_candidates(
        source_conn,
        source_table: str,
        target_conn,
        target_table: str,
        samples: Sequence[Mapping[str, Any]],
        candidates: Mapping[str, Mapping[str, Optional[str]]],
        evidence: Optional[Evidence] = None,
        roles: Optional[dict[str, str]] = None,
        ) -> dict[str, PreflightReport]:
    """
    Assess several candidate mappings against the same metadata and samples.

    :returns: ``{candidate_name: PreflightReport}``.
    """
    return {
        name: assess_migration(
            source_conn, source_table, target_conn, target_table, samples,
            mapping=mapping, evidence=evidence, roles=roles)
        for name, mapping in candidates.items()
    }


def format_report(report: PreflightReport) -> str:
    """Return a plain-text summary of a preflight report."""
    lines = [
        f"Preflight: {report.source_table} -> {report.target_table}",
        f"Samples: {report.sample_count}",
        f"Overall: {report.overall_verdict}",
        "",
    ]
    for assessment in report.assessments:
        lines.append(f"- {assessment.column} [{assessment.role}]: "
                     f"{assessment.verdict}")
        lines.append(f"  reason: {assessment.reason}")
        if assessment.counterexample:
            lines.append(f"  counterexample: {assessment.counterexample}")
        if assessment.missing_evidence:
            lines.append("  missing evidence: "
                         + "; ".join(assessment.missing_evidence))
        lines.append(f"  scope: {assessment.scope}")

    if report.changes:
        lines.extend(["", "Re-evaluation changes:"])
        for change in report.changes:
            before_role = change.get("before_role")
            if change["before"] is None:
                lines.append(
                    f"- {change['column']} [{change['role']}]: "
                    f"newly assessed as {change['after']} "
                    f"({change['evidence']})")
            elif before_role and before_role != change["role"]:
                lines.append(
                    f"- {change['column']} [{before_role} -> {change['role']}]: "
                    f"{change['before']} -> {change['after']} "
                    f"({change['evidence']})")
            else:
                lines.append(
                    f"- {change['column']} [{change['role']}]: "
                    f"{change['before']} -> {change['after']} "
                    f"({change['evidence']})")

    if report.notes:
        lines.extend(["", "Notes:"])
        lines.extend(f"- {note}" for note in report.notes)

    return "\n".join(lines)


def _infer_roles(source_columns: Iterable[Column],
                 target_columns: Iterable[Column]) -> dict[str, str]:
    target_by_name = {c.name: c for c in target_columns}
    roles = {}
    for source in source_columns:
        target = target_by_name.get(source.name)
        if target is None:
            continue
        if _is_numeric(source.type) and _is_numeric(target.type):
            roles[source.name] = NUMERIC_ROLE
        elif _is_text(source.type) and _is_numeric(target.type):
            roles[source.name] = BUSINESS_KEY_ROLE
        else:
            roles[source.name] = "mapped"
    return roles


def _apply_evidence(
        mapping: dict[str, Optional[str]],
        roles: dict[str, str],
        target_by_name: dict[str, Column],
        evidence: Evidence,
        notes: list[str],
        ) -> dict[str, Optional[str]]:
    mapping = dict(mapping)

    reroutes = (
        (BUSINESS_KEY_ROLE, evidence.business_key_text_column),
        (NUMERIC_ROLE, evidence.numeric_text_column),
    )
    for role, column_name in reroutes:
        if not column_name:
            continue
        if column_name not in target_by_name:
            raise ValueError(
                f"Evidence names target column '{column_name}', "
                "which is not in the target table.")
        for source_name, source_role in roles.items():
            if source_role != role:
                continue
            for target_name, mapped_source in list(mapping.items()):
                if mapped_source == source_name:
                    del mapping[target_name]
            mapping[column_name] = source_name
            notes.append(
                f"Evidence rerouted source column '{source_name}' to target "
                f"column '{column_name}'.")
    return mapping


def _metadata_notes(source_columns: Iterable[Column],
                    target_columns: Iterable[Column],
                    mapping: dict[str, Optional[str]]) -> list[str]:
    source_by_name = {c.name: c for c in source_columns}
    target_by_name = {c.name: c for c in target_columns}
    notes = []

    for target_name, source_name in mapping.items():
        if source_name is None:
            continue
        if source_name not in source_by_name:
            notes.append(
                f"Mapped source column '{source_name}' is not in the source "
                "table metadata.")
        if target_name not in target_by_name:
            notes.append(
                f"Mapped target column '{target_name}' is not in the target "
                "table metadata.")
            continue
        source = source_by_name.get(source_name)
        target = target_by_name[target_name]
        if source and source.type != target.type:
            notes.append(
                f"Declared type differs for '{target_name}': "
                f"source={source.type!r}, target={target.type!r}.")

    for target in target_columns:
        if target.name in mapping:
            continue
        if target.not_null:
            notes.append(
                f"Target required column '{target.name}' is omitted by the "
                "candidate mapping.")
    return notes


def _round_trip(
        target_conn,
        target_table: str,
        samples: Sequence[Mapping[str, Any]],
        mapping: dict[str, Optional[str]],
        target_columns: Iterable[Column],
        ) -> Optional[list[dict]]:
    clone = _sqlite_clone(target_conn, target_table)
    if clone is None:
        return None

    target_names = [c.name for c in target_columns]
    select_columns = ", ".join(_quote(name) for name in target_names)
    mapped = [(t, s) for t, s in mapping.items() if s is not None]

    if mapped:
        columns = ", ".join(_quote(t) for t, _ in mapped)
        placeholders = ", ".join("?" for _ in mapped)
        insert_sql = (f"INSERT INTO {_quote(target_table)} ({columns}) "
                      f"VALUES ({placeholders})")
    else:
        insert_sql = f"INSERT INTO {_quote(target_table)} DEFAULT VALUES"

    results = []
    for index, sample in enumerate(samples):
        parameters = [sample.get(source) for _, source in mapped]
        try:
            cursor = clone.execute(insert_sql, parameters)
            rowid = cursor.lastrowid
            row = clone.execute(
                f"SELECT {select_columns} FROM {_quote(target_table)} "
                "WHERE rowid = ?", (rowid,)).fetchone()
            results.append({
                "index": index,
                "readback": dict(zip(target_names, row)),
                "error": None,
            })
        except sqlite3.Error as exc:
            results.append({
                "index": index,
                "readback": {},
                "error": exc,
            })

    clone.commit()
    clone.close()
    return results


def _sqlite_clone(target_conn, target_table: str) -> Optional[sqlite3.Connection]:
    if not isinstance(target_conn, sqlite3.Connection):
        return None
    row = target_conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
        (target_table,),
    ).fetchone()
    if not row or not row[0]:
        return None
    clone = sqlite3.connect(":memory:")
    clone.execute(row[0])
    return clone


def _assess_numeric(
        source_name: str,
        target_name: str,
        target_column: Column,
        samples: Sequence[Mapping[str, Any]],
        results: Sequence[dict],
        evidence: Evidence,
        ) -> ColumnAssessment:
    rows, failure = _collect_rows(
        source_name, target_name, samples, results, NUMERIC_ROLE)
    if failure:
        return failure

    null_loss = _null_loss(source_name, target_name, rows)
    if null_loss:
        return null_loss

    collision = _minimal_collision(
        rows,
        distinct=lambda a, b: not _numeric_equal(a, b),
        equal=_numeric_equal,
    )
    mismatch = next(
        ((index, source, readback)
         for index, source, readback in rows
         if not _numeric_equal(source, readback)),
        None,
    )

    if evidence.conversion_contract:
        if mismatch:
            return ColumnAssessment(
                column=target_name,
                role=NUMERIC_ROLE,
                verdict=COUNTEREXAMPLE,
                reason=("Supplied conversion contract did not preserve the "
                        "sample value on readback."),
                counterexample=_counterexample_dict(*mismatch),
            )
        return ColumnAssessment(
            column=target_name,
            role=NUMERIC_ROLE,
            verdict=DIRECT_SAFE,
            reason=(f"Sample readback matched under the supplied contract: "
                    f"{evidence.conversion_contract}"),
            scope="supplied conversion contract; not limited to these samples",
        )

    if collision:
        return ColumnAssessment(
            column=target_name,
            role=NUMERIC_ROLE,
            verdict=COUNTEREXAMPLE,
            reason=("Two distinct source amounts read back as the same "
                    "target value.  Declared types are not proof of numeric "
                    "fidelity."),
            counterexample=collision,
        )

    if mismatch:
        return ColumnAssessment(
            column=target_name,
            role=NUMERIC_ROLE,
            verdict=COUNTEREXAMPLE,
            reason=("A source amount changed value on target readback."),
            counterexample=_counterexample_dict(*mismatch),
        )

    if evidence.numeric_text_column and _is_text(target_column.type):
        return ColumnAssessment(
            column=target_name,
            role=NUMERIC_ROLE,
            verdict=DIRECT_SAFE,
            reason=("The supplied target TEXT column preserved the exact "
                    "amount strings for the samples."),
            scope=("TEXT storage for exact decimal strings; supplied samples "
                   "confirm the mapping and storage path"),
        )

    return ColumnAssessment(
        column=target_name,
        role=NUMERIC_ROLE,
        verdict=INSUFFICIENT_EVIDENCE,
        reason=("All supplied sample amounts survived readback, but a finite "
                "sample cannot prove exact numeric storage for other values."),
        missing_evidence=(
            "exact numeric storage and readback contract for the target "
            "column (a matching declared type is not sufficient)",
        ),
    )


def _assess_business_key(
        source_name: str,
        target_name: str,
        target_column: Column,
        samples: Sequence[Mapping[str, Any]],
        results: Sequence[dict],
        evidence: Evidence,
        ) -> ColumnAssessment:
    rows, failure = _collect_rows(
        source_name, target_name, samples, results, BUSINESS_KEY_ROLE)
    if failure:
        return failure

    null_loss = _null_loss(source_name, target_name, rows)
    if null_loss:
        return null_loss

    collision = _minimal_collision(
        rows,
        distinct=lambda a, b: not _text_equal(a, b),
        equal=_text_equal,
    )
    mismatch = next(
        ((index, source, readback)
         for index, source, readback in rows
         if not _text_equal(source, readback)),
        None,
    )

    if collision:
        return ColumnAssessment(
            column=target_name,
            role=BUSINESS_KEY_ROLE,
            verdict=COUNTEREXAMPLE,
            reason=("Two distinct business numbers read back as the same "
                    "target value; the source records are no longer "
                    "distinguishable."),
            counterexample=collision,
        )

    if mismatch:
        return ColumnAssessment(
            column=target_name,
            role=BUSINESS_KEY_ROLE,
            verdict=COUNTEREXAMPLE,
            reason=("A business number changed on target readback."),
            counterexample=_counterexample_dict(*mismatch),
        )

    if evidence.conversion_contract:
        return ColumnAssessment(
            column=target_name,
            role=BUSINESS_KEY_ROLE,
            verdict=DIRECT_SAFE,
            reason=(f"Sample readback matched under the supplied contract: "
                    f"{evidence.conversion_contract}"),
            scope="supplied conversion contract; not limited to these samples",
        )

    if _is_text(target_column.type):
        return ColumnAssessment(
            column=target_name,
            role=BUSINESS_KEY_ROLE,
            verdict=DIRECT_SAFE,
            reason=("Text-to-text storage preserved the supplied business "
                    "numbers, including leading zeros."),
            scope=("TEXT storage for text values; supplied samples confirm "
                   "the mapping and storage path"),
        )

    return ColumnAssessment(
        column=target_name,
        role=BUSINESS_KEY_ROLE,
        verdict=INSUFFICIENT_EVIDENCE,
        reason=("Supplied business numbers survived readback, but there is "
                "no text-storage or conversion contract for other values."),
        missing_evidence=(
            "a target TEXT column that stores the business number, or a "
            "conversion contract that preserves leading zeros",
        ),
    )


def _assess_mapped_text(
        source_name: str,
        target_name: str,
        samples: Sequence[Mapping[str, Any]],
        results: Sequence[dict],
        ) -> ColumnAssessment:
    rows, failure = _collect_rows(
        source_name, target_name, samples, results, "mapped")
    if failure:
        return failure

    null_loss = _null_loss(source_name, target_name, rows)
    if null_loss:
        return null_loss
    distinct_but_merged = _minimal_collision(
        rows,
        distinct=lambda a, b: a != b,
        equal=lambda a, b: a == b,
    )
    if distinct_but_merged:
        return ColumnAssessment(
            column=target_name,
            role="mapped",
            verdict=COUNTEREXAMPLE,
            reason="Two distinct source values read back as the same value.",
            counterexample=distinct_but_merged,
        )

    mismatch = next(
        ((index, source, readback)
         for index, source, readback in rows
         if not _text_equal(source, readback)),
        None,
    )
    if mismatch:
        return ColumnAssessment(
            column=target_name,
            role="mapped",
            verdict=COUNTEREXAMPLE,
            reason="Value changed on target readback.",
            counterexample=_counterexample_dict(*mismatch),
        )
    return ColumnAssessment(
        column=target_name,
        role="mapped",
        verdict=DIRECT_SAFE,
        reason="Supplied samples survived readback unchanged.",
        scope=("supplied sample rows only; text-to-text storage is exact for "
               "the sampled values"),
    )


def _assess_required_omitted(
        target_column: Column,
        evidence: Evidence,
        ) -> Optional[ColumnAssessment]:
    if target_column.has_default:
        if evidence.defaults_complete and target_column.name in evidence.default_values:
            return ColumnAssessment(
                column=target_column.name,
                role=REQUIRED_ROLE,
                verdict=DIRECT_SAFE,
                reason=("Omission writes the supplied target default "
                        f"{evidence.default_values[target_column.name]!r}."),
                scope=("supplied target default evidence; every omitted row "
                       "receives that default"),
            )
        return ColumnAssessment(
            column=target_column.name,
            role=REQUIRED_ROLE,
            verdict=INSUFFICIENT_EVIDENCE,
            reason=("The target column is required and has a default, but "
                    "table_info only reports has_default as a boolean.  The "
                    "default expression/value and its completeness are not "
                    "known, so omission cannot be called proven safe."),
            missing_evidence=(
                "the exact target default expression/value for this column",
                "proof that the default evidence is complete for all omitted "
                "rows and preserves the intended missing-value semantics",
            ),
        )

    if target_column.not_null:
        return ColumnAssessment(
            column=target_column.name,
            role=REQUIRED_ROLE,
            verdict=COUNTEREXAMPLE,
            reason=("The target column is NOT NULL and has no default; "
                    "omitting it rejects every sample row."),
            counterexample={
                "kind": "not_null_violation",
                "column": target_column.name,
                "detail": "INSERT without this column fails with a NOT NULL "
                          "constraint error",
            },
        )

    return ColumnAssessment(
        column=target_column.name,
        role=REQUIRED_ROLE,
        verdict=DIRECT_SAFE,
        reason=("The target column is nullable and has no default; omission "
                "writes NULL, preserving a missing value."),
        scope=("supplied metadata; NULL is written for every omitted row"),
    )


def _collect_rows(
        source_name: str,
        target_name: str,
        samples: Sequence[Mapping[str, Any]],
        results: Sequence[dict],
        role: str,
        ) -> tuple[list[tuple[int, Any, Any]], Optional[ColumnAssessment]]:
    rows = []
    for sample, result in zip(samples, results):
        if result["error"]:
            return rows, _insert_error_assessment(
                sample, result, target_name, role)
        rows.append((
            result["index"],
            sample.get(source_name),
            result["readback"].get(target_name),
        ))
    return rows, None


def _insert_error_assessment(
        sample: Mapping[str, Any],
        result: dict,
        target_name: str,
        role: str,
        ) -> ColumnAssessment:
    detail = str(result["error"])
    is_not_null = "NOT NULL" in detail.upper()
    names_this_column = (
        f".{target_name}" in detail
        or detail.rstrip().endswith(target_name)
    )

    if is_not_null and names_this_column:
        return ColumnAssessment(
            column=target_name,
            role=role,
            verdict=COUNTEREXAMPLE,
            reason=("The mapped sample is NULL for a NOT NULL target "
                    "column; the candidate write is rejected."),
            counterexample={
                "kind": "not_null_violation",
                "column": target_name,
                "sample_index": result["index"],
                "source_value": sample.get(target_name),
                "detail": detail,
            },
        )

    if is_not_null:
        return ColumnAssessment(
            column=target_name,
            role=role,
            verdict=INSUFFICIENT_EVIDENCE,
            reason=("The candidate write was blocked by a different required "
                    "target column, so this column's readback was not "
                    "observed."),
            missing_evidence=(
                "a mapping or default for the required target column named "
                "in the constraint error",
            ),
        )

    return ColumnAssessment(
        column=target_name,
        role=role,
        verdict=COUNTEREXAMPLE,
        reason="The candidate write was rejected for a sample row.",
        counterexample={
            "kind": "insert_error",
            "column": target_name,
            "sample_index": result["index"],
            "source_value": sample.get(target_name),
            "detail": detail,
        },
    )


def _minimal_collision(rows, distinct, equal) -> Optional[dict]:
    for left in range(len(rows)):
        for right in range(left + 1, len(rows)):
            left_row = rows[left]
            right_row = rows[right]
            if not distinct(left_row[1], right_row[1]):
                continue
            if left_row[2] is None or right_row[2] is None:
                continue
            if equal(left_row[2], right_row[2]):
                return {
                    "kind": "merged_values",
                    "sample_indexes": [left_row[0], right_row[0]],
                    "source_values": [left_row[1], right_row[1]],
                    "target_readback": left_row[2],
                }
    return None


def _counterexample_dict(index: int, source: Any, readback: Any) -> dict:
    return {
        "kind": "value_changed",
        "sample_index": index,
        "source_value": source,
        "target_readback": readback,
    }


def _null_loss(
        source_name: str,
        target_name: str,
        rows: Sequence[tuple[int, Any, Any]],
        ) -> Optional[ColumnAssessment]:
    for index, source, readback in rows:
        if source is None and readback is not None:
            return ColumnAssessment(
                column=target_name,
                role="null_semantics",
                verdict=COUNTEREXAMPLE,
                reason="A source NULL read back as a non-NULL target value.",
                counterexample={
                    "kind": "null_not_preserved",
                    "column": target_name,
                    "sample_index": index,
                    "source_value": None,
                    "target_readback": readback,
                },
            )
        if source is not None and readback is None:
            return ColumnAssessment(
                column=target_name,
                role="null_semantics",
                verdict=COUNTEREXAMPLE,
                reason="A non-NULL source value read back as NULL.",
                counterexample={
                    "kind": "value_lost_as_null",
                    "column": target_name,
                    "sample_index": index,
                    "source_value": source,
                    "target_readback": None,
                },
            )
    return None


def _as_decimal(value: Any) -> Optional[Decimal]:
    if isinstance(value, Decimal):
        return value
    if isinstance(value, bool):
        return Decimal(int(value))
    if isinstance(value, (int, str)):
        try:
            return Decimal(str(value).strip())
        except InvalidOperation:
            return None
    if isinstance(value, float):
        return Decimal(str(value))
    return None


def _numeric_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    left_decimal = _as_decimal(left)
    right_decimal = _as_decimal(right)
    if left_decimal is not None and right_decimal is not None:
        return left_decimal == right_decimal
    return left == right


def _text_equal(left: Any, right: Any) -> bool:
    if left is None or right is None:
        return left is None and right is None
    if isinstance(left, str) != isinstance(right, str):
        return False
    return str(left) == str(right)


def _is_numeric(type_name: Optional[str]) -> bool:
    if not type_name:
        return False
    type_name = type_name.lower()
    return any(token in type_name for token in
               ("int", "real", "flo", "dou", "num", "dec", "money"))


def _is_text(type_name: Optional[str]) -> bool:
    if not type_name:
        return False
    type_name = type_name.lower()
    return any(token in type_name for token in
               ("text", "char", "clob", "varchar", "string"))


def _quote(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _diff_reports(
        old: PreflightReport,
        new: PreflightReport,
        evidence: Evidence,
        ) -> list[dict]:
    old_by_column = {a.column: a for a in old.assessments}
    new_by_column = {a.column: a for a in new.assessments}
    label = _evidence_label(old.evidence, evidence)
    changes = []

    for column, assessment in new_by_column.items():
        previous = old_by_column.get(column)
        if previous is None:
            changes.append({
                "column": column,
                "role": assessment.role,
                "before_role": None,
                "before": None,
                "after": assessment.verdict,
                "evidence": label,
            })
        elif (previous.verdict != assessment.verdict
              or previous.role != assessment.role):
            changes.append({
                "column": column,
                "role": assessment.role,
                "before_role": previous.role,
                "before": previous.verdict,
                "after": assessment.verdict,
                "evidence": label,
            })

    for column, previous in old_by_column.items():
        if column not in new_by_column:
            changes.append({
                "column": column,
                "role": previous.role,
                "before_role": previous.role,
                "before": previous.verdict,
                "after": "not_assessed",
                "evidence": label,
            })

    return changes


def _evidence_label(old: Evidence, new: Evidence) -> str:
    changed = [
        name for name in vars(old)
        if getattr(old, name) != getattr(new, name)
    ]
    labels = []
    if "business_key_text_column" in changed:
        labels.append(
            f"business_key_text_column={new.business_key_text_column}")
    if "numeric_text_column" in changed:
        labels.append(f"numeric_text_column={new.numeric_text_column}")
    if "conversion_contract" in changed:
        labels.append("conversion_contract")
    if "defaults_complete" in changed and not new.defaults_complete:
        labels.append("target default evidence revoked")
    if labels:
        return ", ".join(labels)
    if changed:
        return "changed: " + ", ".join(changed)
    return "candidate mapping changed"
