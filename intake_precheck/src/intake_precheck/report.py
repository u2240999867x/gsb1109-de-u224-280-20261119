from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

from .precheck import (
    CONSTRAINT_REASON,
    MISSING_REASON,
    TYPE_REASON,
    ScanResult,
)

REASON_TITLES = {
    TYPE_REASON: "type cast failure",
    MISSING_REASON: "missing value",
    CONSTRAINT_REASON: "constraint violation",
}


def render_json(result: ScanResult) -> Dict[str, Any]:
    return {
        "source": result.source,
        "contract_schema": result.schema_path,
        "scope": result.scope,
        "confirmed_range": result.confirmed_range,
        "scanned_rows": result.scanned_rows,
        "truncated": result.truncated,
        "limit": result.limit,
        "legacy_date_sample_at_entry": result.legacy_date_samples,
        "verdict_under_saved_contract": (
            "accepted-within-confirmed-range"
            if result.valid_under_contract
            else "rejected"
        ),
        "column_profiles": [
            {
                "field_name": profile.field_name,
                "contract_type": profile.contract_type,
                "total_cells": profile.total_cells,
                "blank_cells": profile.blank_cells,
                "non_blank_cells": profile.non_blank_cells,
                "entirely_blank": profile.all_blank,
                "rejection_count": len(profile.rejections),
                "earliest_rejection": _rejection_dict(profile.earliest_rejection),
            }
            for profile in result.profiles.values()
        ],
        "rejections": [
            _rejection_dict(rejection)
            for finding in result.findings
            for rejection in finding.rejections
        ],
        "rejected_rows": [
            {
                "row_number": finding.row_number,
                "raw_row": finding.raw_row,
                "rejection_count": len(finding.rejections),
            }
            for finding in result.findings
        ],
        "candidate_inference": {
            "status": "hint-only, not used for any verdict",
            "descriptor": result.candidate_descriptor,
            "warnings": result.candidate_warnings,
        },
        "evidence": {
            "rejections_csv": str(result.evidence_csv) if result.evidence_csv else None,
            "rejections_jsonl": str(result.evidence_jsonl)
            if result.evidence_jsonl
            else None,
        },
    }


def _rejection_dict(rejection: Any) -> Any:
    if rejection is None:
        return None
    return {
        "row_number": rejection.row_number,
        "field_name": rejection.field_name,
        "field_number": rejection.field_number,
        "reason_family": rejection.reason,
        "reason_title": REASON_TITLES[rejection.reason],
        "note": rejection.note,
        "raw_cell": rejection.raw_cell,
    }


def render_markdown(result: ScanResult) -> str:
    lines: List[str] = []
    lines.append("# Intake precheck report")
    lines.append("")
    lines.append(f"- Source: `{result.source}`")
    lines.append(f"- Saved contract schema: `{result.schema_path}` (read-only)")
    lines.append(f"- Scan scope: **{result.scope}**")
    lines.append(f"- Confirmed range: **{result.confirmed_range}**")
    if result.truncated:
        lines.append(
            "- Warning: only the leading fragment was scanned. Rows beyond the "
            "confirmed range have not arrived yet; compatibility of the confirmed "
            "head does NOT imply the whole batch is compatible."
        )
    else:
        lines.append("- The complete input was scanned in a single pass.")
    verdict = (
        "no rejection within the confirmed range"
        if result.valid_under_contract
        else f"{sum(len(f.rejections) for f in result.findings)} cell rejection(s) "
        f"across {len(result.findings)} row(s)"
    )
    lines.append(f"- Verdict under the saved contract: **{verdict}**")
    lines.append("")

    lines.append("## Entry: legacy date-format sample")
    lines.append("")
    if result.legacy_date_samples:
        lines.append(
            "First cells matching the contract's old date format (`%Y-%m-%d`), "
            "shown before any verdict:"
        )
        lines.append("")
        lines.append("| Row | Field | Raw cell |")
        lines.append("| --- | --- | --- |")
        for item in result.legacy_date_samples:
            lines.append(
                f"| {item['row_number']} | {item['field_name']} | `{item['raw_cell']}` |"
            )
    else:
        lines.append("No cell in the scanned range matches the old date format.")
    lines.append("")

    lines.append("## Column profiles against the saved contract")
    lines.append("")
    lines.append(
        "| Field | Contract type | Non-blank | Blank | Entirely blank | Rejections |"
    )
    lines.append("| --- | --- | --- | --- | --- | --- |")
    for profile in result.profiles.values():
        lines.append(
            f"| {profile.field_name} | {profile.contract_type} | "
            f"{profile.non_blank_cells} | {profile.blank_cells} | "
            f"{'yes' if profile.all_blank else 'no'} | {len(profile.rejections)} |"
        )
    lines.append("")
    for profile in result.profiles.values():
        if profile.all_blank:
            lines.append(
                f"- `{profile.field_name}` is entirely blank in the scanned range. "
                "Blank cells are missing values, NOT a type change: no rejection is "
                "raised and no new type is asserted for this column."
            )
    lines.append("")

    lines.append("## Rejections under the saved contract")
    lines.append("")
    if not result.findings:
        lines.append("None. Every scanned cell was accepted by the saved field readers.")
    else:
        lines.append(
            "Earliest offending row and the exact type / constraint / missing "
            "reason are listed; the original cells are preserved verbatim."
        )
        lines.append("")
        lines.append("| Earliest row | Field | Reason | Frictionless note | Raw cell |")
        lines.append("| --- | --- | --- | --- | --- |")
        for profile in result.profiles.values():
            rejection = profile.earliest_rejection
            if rejection is None:
                continue
            lines.append(
                f"| {rejection.row_number} | {rejection.field_name} | "
                f"{REASON_TITLES[rejection.reason]} | {rejection.note} | "
                f"`{rejection.raw_cell}` |"
            )
        lines.append("")
        lines.append("All rejected rows (full raw evidence):")
        lines.append("")
        lines.append("| Row | Field | Reason | Note | Raw cell |")
        lines.append("| --- | --- | --- | --- | --- |")
        for finding in result.findings:
            for rejection in finding.rejections:
                lines.append(
                    f"| {rejection.row_number} | {rejection.field_name} | "
                    f"{REASON_TITLES[rejection.reason]} | {rejection.note} | "
                    f"`{rejection.raw_cell}` |"
                )
    lines.append("")

    lines.append("## Candidate inference (hint only)")
    lines.append("")
    lines.append(
        "The descriptor below is inferred from the scanned fragment with "
        "Frictionless `Detector`. It is strictly separated from the saved-contract "
        "verdict above: it never accepts or rejects any record and must not replace "
        "the saved contract without a contract change."
    )
    lines.append("")
    lines.append("```json")
    lines.append(json.dumps(result.candidate_descriptor, indent=2, ensure_ascii=False))
    lines.append("```")
    if result.candidate_warnings:
        lines.append("")
        for warning in result.candidate_warnings:
            lines.append(f"- {warning}")
    lines.append("")

    lines.append("## Preserved evidence beyond the contract")
    lines.append("")
    if result.evidence_csv and result.evidence_jsonl:
        lines.append(f"- Rejected rows, original values: `{result.evidence_csv}`")
        lines.append(f"- Cell-level reasons with raw input: `{result.evidence_jsonl}`")
    else:
        lines.append("No rejection was recorded, so no evidence file was produced.")
    lines.append("")
    return "\n".join(lines)


def write_reports(result: ScanResult, out_dir: Path, stem: str) -> tuple[Path, Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / f"{stem}.json"
    md_path = out_dir / f"{stem}.md"
    json_path.write_text(
        json.dumps(render_json(result), indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    md_path.write_text(render_markdown(result), encoding="utf-8")
    return md_path, json_path
