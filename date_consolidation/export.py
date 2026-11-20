"""Export confirmed values and read them back through DateField.

The standardized column is written and re-read by a frictionless DateField in
one explicitly chosen stable format, so the single-format read/write path
keeps working unchanged.  Cells without sufficient evidence stay empty rather
than being guessed from the first configured format.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any, Dict, List

from frictionless import Field

from .consolidate import ConsolidationResult

EXPORT_COLUMNS = [
    "source",
    "version",
    "source_file",
    "source_line",
    "raw_text",
    "evidence",
    "standardized_date",
]


def _date_field(stable_format: str) -> Field:
    if stable_format == "%Y-%m-%d":
        descriptor_format = "default"
    else:
        descriptor_format = stable_format
    return Field.from_descriptor(
        {"name": "standardized_date", "type": "date", "format": descriptor_format}
    )


def export_table(result: ConsolidationResult, output_path: str | Path) -> Path:
    """Write one consolidated table; only evidenced dates are standardized."""

    output_path = Path(output_path)
    field = _date_field(result.stable_format)

    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(EXPORT_COLUMNS)
        for record in result.records:
            written = ""
            if record.standardized is not None:
                written, notes = field.write_cell(record.standardized)
                if notes:
                    raise ValueError(
                        f"DateField rejected {record.standardized} with format "
                        f"{result.stable_format}: {notes}"
                    )
            writer.writerow(
                [
                    record.source,
                    record.version,
                    record.file_path,
                    record.physical_line,
                    record.raw_text,
                    record.verdict.kind,
                    written,
                ]
            )
    return output_path


def read_back(output_path: str | Path, stable_format: str) -> List[Dict[str, Any]]:
    """Re-read the exported table through DateField and verify the round trip."""

    output_path = Path(output_path)
    field = _date_field(stable_format)
    rows: List[Dict[str, Any]] = []
    with output_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            raw_standard = row["standardized_date"]
            if raw_standard:
                value, notes = field.read_cell(raw_standard)
                if notes or value is None:
                    raise ValueError(
                        f"round-trip failed for {raw_standard!r} in {output_path}: {notes}"
                    )
                row["standardized_date_parsed"] = value
            else:
                row["standardized_date_parsed"] = None
            rows.append(row)
    return rows
