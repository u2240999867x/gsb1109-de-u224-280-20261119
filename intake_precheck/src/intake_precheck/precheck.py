from __future__ import annotations

import csv
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from frictionless import Detector, Schema
from frictionless.resources import TableResource


# Note keys produced by frictionless Field.create_cell_reader map to three
# reason families. "type" is a cast failure, "required" is a missing value,
# every other key is a constraint failure (maximum/minimum/pattern/...).
TYPE_REASON = "type"
MISSING_REASON = "missing"
CONSTRAINT_REASON = "constraint"

LEGACY_DATE_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _reason_family(note_key: str) -> str:
    if note_key == "type":
        return TYPE_REASON
    if note_key == "required":
        return MISSING_REASON
    return CONSTRAINT_REASON


@dataclass
class CellRejection:
    """One contract rejection for one cell, backed by the raw input."""

    row_number: int
    field_name: str
    field_number: int
    reason: str
    note: str
    raw_cell: str
    raw_row: List[str]


@dataclass
class ColumnProfile:
    field_name: str
    contract_type: str
    total_cells: int = 0
    blank_cells: int = 0
    non_blank_cells: int = 0
    blank_row_numbers: List[int] = field(default_factory=list)
    rejections: List[CellRejection] = field(default_factory=list)

    @property
    def all_blank(self) -> bool:
        return self.total_cells > 0 and self.non_blank_cells == 0

    @property
    def earliest_rejection(self) -> Optional[CellRejection]:
        return self.rejections[0] if self.rejections else None


@dataclass
class RowFinding:
    row_number: int
    raw_row: List[str]
    rejections: List[CellRejection]


@dataclass
class ScanConfig:
    limit: Optional[int] = None
    evidence_dir: Optional[Path] = None
    evidence_prefix: str = "scan"
    expected_rows: Optional[int] = None


@dataclass
class ScanResult:
    source: str
    schema_path: str
    limit: Optional[int]
    header: List[str]
    scanned_rows: int
    truncated: bool
    profiles: Dict[str, ColumnProfile]
    findings: List[RowFinding]
    candidate_descriptor: Dict[str, Any]
    candidate_warnings: List[str]
    evidence_csv: Optional[Path]
    evidence_jsonl: Optional[Path]
    legacy_date_samples: List[Dict[str, Any]]

    @property
    def scope(self) -> str:
        return "partial" if self.truncated else "full"

    @property
    def confirmed_range(self) -> str:
        if self.scanned_rows == 0:
            return "no data rows confirmed"
        return f"data rows 1..{self.scanned_rows} (of the scanned input)"

    @property
    def valid_under_contract(self) -> bool:
        return not self.findings

    def rejection_index(self) -> Dict[str, List[CellRejection]]:
        index: Dict[str, List[CellRejection]] = {}
        for finding in self.findings:
            for rejection in finding.rejections:
                index.setdefault(rejection.field_name, []).append(rejection)
        return index


def _is_blank(cell: Any, schema: Schema, field_index: int) -> bool:
    tokens = list(schema.missing_values)
    field = schema.fields[field_index]
    if field.has_defined("missing_values"):
        tokens = list(field.missing_values)
    return str(cell) in tokens


def _write_evidence(
    config: ScanConfig,
    header: List[str],
    findings: List[RowFinding],
) -> tuple[Optional[Path], Optional[Path]]:
    if config.evidence_dir is None or not findings:
        return None, None
    evidence_dir = Path(config.evidence_dir)
    evidence_dir.mkdir(parents=True, exist_ok=True)
    csv_path = evidence_dir / f"{config.evidence_prefix}-rejections.csv"
    jsonl_path = evidence_dir / f"{config.evidence_prefix}-rejections.jsonl"

    with csv_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for finding in findings:
            writer.writerow(finding.raw_row)

    with jsonl_path.open("w", encoding="utf-8") as handle:
        for finding in findings:
            for rejection in finding.rejections:
                handle.write(
                    json.dumps(
                        {
                            "row_number": rejection.row_number,
                            "field_name": rejection.field_name,
                            "field_number": rejection.field_number,
                            "reason": rejection.reason,
                            "note": rejection.note,
                            "raw_cell": rejection.raw_cell,
                            "raw_row": rejection.raw_row,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    return csv_path, jsonl_path


def scan_resource(
    resource: TableResource,
    schema: Schema,
    config: ScanConfig,
    schema_path: str = "<saved-contract>",
) -> ScanResult:
    """Stream one resource once, judging every cell by the saved schema.

    The schema object is only read, never mutated. Cell readers are created
    once and reused, so the verdict for a given record is identical no matter
    which sample boundary stops the scan.
    """
    readers = list(schema.create_cell_readers().values())
    profiles = [
        ColumnProfile(field_name=field.name, contract_type=field.type)
        for field in schema.fields
    ]
    findings: List[RowFinding] = []
    fragment: List[List[Any]] = []
    legacy_date_samples: List[Dict[str, Any]] = []
    header: List[str] = []
    scanned_rows = 0
    truncated = False

    resource.open()
    try:
        header = list(resource.labels)
        for cells in resource.cell_stream:
            if cells == header:
                continue
            if config.limit is not None and scanned_rows >= config.limit:
                truncated = True
                break
            scanned_rows += 1
            raw_row = [str(cell) for cell in cells]
            fragment.append(list(cells))

            first_cell = raw_row[0] if raw_row else ""
            if LEGACY_DATE_PATTERN.match(first_cell) and len(legacy_date_samples) < 3:
                legacy_date_samples.append(
                    {
                        "row_number": scanned_rows,
                        "field_name": schema.fields[0].name,
                        "raw_cell": first_cell,
                    }
                )

            notes_per_field: List[Optional[Dict[str, str]]] = []
            for index, reader in enumerate(readers):
                cell = cells[index] if len(cells) > index else None
                _, notes = reader(cell)
                notes_per_field.append(notes)
            row_rejections: List[CellRejection] = []
            for index, notes in enumerate(notes_per_field):
                profile = profiles[index]
                raw_cell = raw_row[index] if index < len(raw_row) else ""
                if _is_blank(cells[index] if index < len(cells) else None, schema, index):
                    profile.blank_cells += 1
                    profile.blank_row_numbers.append(scanned_rows)
                else:
                    profile.non_blank_cells += 1
                profile.total_cells += 1
                if not notes:
                    continue
                for note_key, note_text in notes.items():
                    rejection = CellRejection(
                        row_number=scanned_rows,
                        field_name=schema.fields[index].name,
                        field_number=index + 1,
                        reason=_reason_family(note_key),
                        note=note_text,
                        raw_cell=raw_cell,
                        raw_row=list(raw_row),
                    )
                    profile.rejections.append(rejection)
                    row_rejections.append(rejection)
            if row_rejections:
                findings.append(
                    RowFinding(
                        row_number=scanned_rows,
                        raw_row=list(raw_row),
                        rejections=row_rejections,
                    )
                )
    finally:
        resource.close()

    candidate_descriptor, candidate_warnings = _infer_candidate(
        schema, fragment, header, profiles
    )
    if config.expected_rows is not None and scanned_rows < config.expected_rows:
        truncated = True
        candidate_warnings.append(
            f"Only {scanned_rows} of {config.expected_rows} expected rows have "
            "arrived; the inferred descriptor covers the confirmed head only."
        )
    evidence_csv, evidence_jsonl = _write_evidence(config, header, findings)

    return ScanResult(
        source=resource.path or "<inline>",
        schema_path=schema_path,
        limit=config.limit,
        header=header,
        scanned_rows=scanned_rows,
        truncated=truncated,
        profiles={profile.field_name: profile for profile in profiles},
        findings=findings,
        candidate_descriptor=candidate_descriptor,
        candidate_warnings=candidate_warnings,
        evidence_csv=evidence_csv,
        evidence_jsonl=evidence_jsonl,
        legacy_date_samples=legacy_date_samples,
    )


def _infer_candidate(
    schema: Schema,
    fragment: List[List[Any]],
    header: List[str],
    profiles: List[ColumnProfile],
) -> tuple[Dict[str, Any], List[str]]:
    """Infer a schema purely as an improvement hint.

    The output is never used to accept or reject anything and is kept fully
    separate from the saved-contract verdicts. Columns without a single
    non-blank value cannot be inferred: the detector's fallback guess for
    them is explicitly flagged instead of being presented as a type change.
    """
    warnings: List[str] = []
    if not fragment:
        warnings.append("No data rows were scanned; no candidate can be inferred.")
        return {"fields": []}, warnings

    detector = Detector()
    candidate = detector.detect_schema(fragment, labels=header)
    descriptor = candidate.to_descriptor()

    for profile in profiles:
        if profile.all_blank:
            warnings.append(
                f'Column "{profile.field_name}" is entirely blank in the scanned '
                "range: there is no value to infer a type from. The detector's "
                "fallback entry is not evidence of a type change; the blank cells "
                "remain missing values under the saved contract."
            )
    for profile in profiles:
        if profile.rejections:
            reasons = sorted({item.reason for item in profile.rejections})
            warnings.append(
                f'Column "{profile.field_name}" has saved-contract rejections '
                f"({', '.join(reasons)}); review the candidate type only as a "
                "renegotiation hint, never as grounds to accept this batch."
            )
    return descriptor, warnings
