from __future__ import annotations

import json
from pathlib import Path

import pytest

from frictionless import Schema
from frictionless.resources import TableResource

from intake_precheck.precheck import (
    CONSTRAINT_REASON,
    MISSING_REASON,
    TYPE_REASON,
    ScanConfig,
    scan_resource,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]
CONTRACT = PROJECT_ROOT / "contract" / "inbound-contract.schema.json"
FULL_BATCH = PROJECT_ROOT / "data" / "batch-arrival-2026-11-21.csv"
HEAD_ONLY_BATCH = PROJECT_ROOT / "data" / "batch-arrival-2026-11-21.head-only.csv"


def _scan(csv_path: Path, config: ScanConfig | None = None) -> object:
    schema = Schema.from_descriptor(str(CONTRACT))
    resource = TableResource(path=str(csv_path), schema=schema)
    return scan_resource(resource, schema, config or ScanConfig(), schema_path=str(CONTRACT))


def test_saved_contract_file_is_unchanged_contract():
    descriptor = json.loads(CONTRACT.read_text(encoding="utf-8"))
    by_name = {field["name"]: field for field in descriptor["fields"]}
    assert by_name["arrival_date"]["type"] == "date"
    assert by_name["arrival_date"]["constraints"]["required"] is True
    assert by_name["note"]["missingValues"] == ["", "N/A"]
    assert by_name["quantity"]["type"] == "number"
    assert by_name["quantity"]["constraints"]["maximum"] == 1000
    assert by_name["remark"]["type"] == "string"
    assert by_name["tail_note"]["type"] == "string"
    assert list(by_name) == ["arrival_date", "note", "quantity", "remark", "tail_note"]


def test_full_scan_reports_all_three_reason_families():
    result = _scan(FULL_BATCH)
    reasons = {rejection.reason for finding in result.findings for rejection in finding.rejections}
    assert reasons == {TYPE_REASON, CONSTRAINT_REASON, MISSING_REASON}


def test_timezone_timestamp_is_type_failure_not_a_date():
    result = _scan(FULL_BATCH)
    date_rejections = result.rejection_index()["arrival_date"]
    type_failure = next(item for item in date_rejections if item.reason == TYPE_REASON)
    assert type_failure.row_number == 5
    assert type_failure.raw_cell == "2026-11-18T08:30:00+09:00"
    assert 'type is "date/default"' in type_failure.note


def test_blank_required_date_is_missing_reason_not_type_change():
    result = _scan(FULL_BATCH)
    date_rejections = result.rejection_index()["arrival_date"]
    missing = next(item for item in date_rejections if item.reason == MISSING_REASON)
    assert missing.row_number == 8
    assert missing.raw_cell == ""
    assert "required" in missing.note


def test_over_limit_number_keeps_type_but_violates_maximum():
    result = _scan(FULL_BATCH)
    quantity_rejections = result.rejection_index()["quantity"]
    rows = {item.row_number: item for item in quantity_rejections}
    assert set(rows) == {5, 7}
    for rejection in rows.values():
        assert rejection.reason == CONSTRAINT_REASON
        assert 'constraint "maximum" is "1000"' in rejection.note
    assert rows[5].raw_cell == "1250.75"


def test_blank_column_is_not_a_type_change_and_never_rejects():
    result = _scan(FULL_BATCH)
    remark = result.profiles["remark"]
    assert remark.all_blank is True
    assert remark.blank_cells == 8
    assert remark.rejections == []
    assert "remark" not in result.rejection_index()


def test_blank_head_column_gains_values_later_without_type_change():
    head = _scan(HEAD_ONLY_BATCH, ScanConfig(expected_rows=8))
    assert head.profiles["tail_note"].all_blank is True
    assert head.profiles["tail_note"].rejections == []
    full = _scan(FULL_BATCH)
    tail = full.profiles["tail_note"]
    assert tail.all_blank is False
    assert tail.blank_cells == 4
    assert tail.non_blank_cells == 4
    assert tail.rejections == []
    assert any(
        'Column "tail_note" is entirely blank' in warning
        for warning in head.candidate_warnings
    )
    assert not any(
        'Column "tail_note" is entirely blank' in warning
        for warning in full.candidate_warnings
    )


def test_na_token_and_blank_note_are_accepted_as_missing():
    result = _scan(FULL_BATCH)
    note_profile = result.profiles["note"]
    assert note_profile.rejections == []
    assert note_profile.blank_cells == 5
    assert note_profile.non_blank_cells == 3
    assert note_profile.total_cells == 8


def test_entry_shows_legacy_date_sample_before_verdict():
    result = _scan(FULL_BATCH)
    assert result.legacy_date_samples
    first = result.legacy_date_samples[0]
    assert first == {
        "row_number": 1,
        "field_name": "arrival_date",
        "raw_cell": "2026-11-14",
    }


def test_sampling_boundaries_keep_shared_record_verdicts_stable():
    full = _scan(FULL_BATCH)
    full_by_row = {
        finding.row_number: [
            (rejection.field_name, rejection.reason, rejection.note, rejection.raw_cell)
            for rejection in finding.rejections
        ]
        for finding in full.findings
    }
    for limit in (2, 4, 6):
        sampled = _scan(FULL_BATCH, ScanConfig(limit=limit, expected_rows=8))
        assert sampled.scanned_rows == limit
        assert sampled.scope == "partial"
        sampled_by_row = {
            finding.row_number: [
                (rejection.field_name, rejection.reason, rejection.note, rejection.raw_cell)
                for rejection in finding.rejections
            ]
            for finding in sampled.findings
        }
        for row_number in range(1, limit + 1):
            assert sampled_by_row.get(row_number, []) == full_by_row.get(row_number, [])


def test_partial_scan_states_confirmed_range_not_whole_batch():
    sampled = _scan(FULL_BATCH, ScanConfig(limit=4, expected_rows=8))
    assert sampled.truncated is True
    assert sampled.confirmed_range == "data rows 1..4 (of the scanned input)"
    assert sampled.valid_under_contract is True


def test_delayed_head_only_marks_unconfirmed_tail():
    result = _scan(HEAD_ONLY_BATCH, ScanConfig(expected_rows=8))
    assert result.scanned_rows == 4
    assert result.truncated is True
    assert result.scope == "partial"
    assert "1..4" in result.confirmed_range
    assert result.valid_under_contract is True
    assert any("of 8 expected rows" in warning for warning in result.candidate_warnings)


def test_evidence_preserves_original_rejected_rows(tmp_path: Path):
    config = ScanConfig(evidence_dir=tmp_path, evidence_prefix="case")
    result = _scan(FULL_BATCH, config)
    csv_lines = result.evidence_csv.read_text(encoding="utf-8").splitlines()
    assert csv_lines[0] == "arrival_date,note,quantity,remark,tail_note"
    raw_cells = {line.split(",")[0] for line in csv_lines[1:]}
    assert "2026-11-18T08:30:00+09:00" in raw_cells
    assert "" in raw_cells
    jsonl = [json.loads(line) for line in result.evidence_jsonl.read_text().splitlines()]
    assert all("raw_row" in item and "note" in item for item in jsonl)


def test_candidate_inference_is_separate_and_flags_blank_column():
    result = _scan(FULL_BATCH)
    assert result.candidate_descriptor
    hint_warnings = " ".join(result.candidate_warnings)
    assert 'Column "remark" is entirely blank' in hint_warnings
    assert "not evidence of a type change" in hint_warnings
    assert all(finding.rejections for finding in result.findings)


def test_single_pass_does_not_mutate_saved_schema():
    schema = Schema.from_descriptor(str(CONTRACT))
    before = schema.to_descriptor()
    resource = TableResource(path=str(FULL_BATCH), schema=schema)
    scan_resource(resource, schema, ScanConfig(), schema_path=str(CONTRACT))
    assert schema.to_descriptor() == before
