from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(PROJECT_ROOT / "src"))

from frictionless import Schema
from frictionless.resources import TableResource

from intake_precheck.precheck import ScanConfig, scan_resource
from intake_precheck.report import write_reports

CONTRACT = PROJECT_ROOT / "contract" / "inbound-contract.schema.json"
FULL_BATCH = PROJECT_ROOT / "data" / "batch-arrival-2026-11-21.csv"
HEAD_ONLY_BATCH = PROJECT_ROOT / "data" / "batch-arrival-2026-11-21.head-only.csv"
REPORT_DIR = PROJECT_ROOT / "reports"
EVIDENCE_DIR = REPORT_DIR / "evidence"

# Sampling boundaries applied to the very same full batch. Each boundary is a
# different prefix size; records shared by two prefixes must get identical
# saved-contract verdicts.
SAMPLE_LIMITS = [2, 4, 6, 8]
EXPECTED_ROWS_OF_BATCH = 8


def run_one(csv_path: Path, stem: str, config: ScanConfig) -> None:
    schema = Schema.from_descriptor(str(CONTRACT))
    resource = TableResource(path=str(csv_path), schema=schema)
    result = scan_resource(
        resource,
        schema,
        config,
        schema_path=str(CONTRACT.relative_to(PROJECT_ROOT)),
    )
    md_path, json_path = write_reports(result, REPORT_DIR, stem)
    print(
        f"[{stem}] scope={result.scope} rows={result.scanned_rows} "
        f"rejected_rows={len(result.findings)} -> {md_path.name}, {json_path.name}"
    )


def main() -> None:
    # Full single-pass scan of the complete batch.
    run_one(
        FULL_BATCH,
        "full-batch",
        ScanConfig(evidence_dir=EVIDENCE_DIR, evidence_prefix="full-batch"),
    )

    # The same records read through different sampling boundaries.
    for limit in SAMPLE_LIMITS:
        run_one(
            FULL_BATCH,
            f"sample-head-{limit}",
            ScanConfig(
                limit=limit,
                evidence_dir=EVIDENCE_DIR,
                evidence_prefix=f"sample-head-{limit}",
                expected_rows=EXPECTED_ROWS_OF_BATCH,
            ),
        )

    # Only the identical head has physically arrived; the tail is still missing.
    run_one(
        HEAD_ONLY_BATCH,
        "delayed-head-only",
        ScanConfig(
            evidence_dir=EVIDENCE_DIR,
            evidence_prefix="delayed-head-only",
            expected_rows=EXPECTED_ROWS_OF_BATCH,
        ),
    )


if __name__ == "__main__":
    main()
