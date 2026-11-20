import json
from pathlib import Path

import pytest

from frictionless import audit
from frictionless.exception import FrictionlessException

DAY1 = "data/audit/day-1"
DAY2 = "data/audit/day-2"
DECLARATION = "data/audit/declaration.json"


def run_audit(declaration=DECLARATION):
    return audit(DAY1, DAY2, declaration=declaration)


def table(report, name):
    return next(current for current in report["tables"] if current["table"] == name)


def changes(report, name):
    return {change["key"]["token"]: change for change in table(report, name)["changes"]}


def suppressed(report, name):
    return {entry["key"]["token"]: entry for entry in table(report, name)["suppressed"]}


def canonical(value):
    if isinstance(value, dict):
        return {key: canonical(item) for key, item in value.items() if key != "rowNumber"}
    if isinstance(value, list):
        return [canonical(item) for item in value]
    return value


# Business-key diff


def test_audit_produces_add_delete_update():
    report = run_audit()
    device = changes(report, "device")
    assert device["D-003"]["kind"] == "delete"
    assert device["D-004"]["kind"] == "update"
    assert device["D-007"]["kind"] == "add"


def test_audit_compares_on_the_declared_source_basis():
    report = run_audit()
    update = changes(report, "device")["D-004"]
    assert update["changed"] == {
        "model": {
            "old": "DP-400",
            "new": "dp-400",
            "compare": "source",
        }
    }


def test_audit_does_not_report_lexical_noise_on_normalized_basis():
    report = run_audit()
    device = changes(report, "device")
    # name only differs by collapsed whitespace, status only by case,
    # location is equal: all compare=normalized and must stay silent.
    assert "D-001" not in device
    # D-002 is identical after normalization and has no change at all
    assert "D-002" not in device


def test_audit_reports_normalized_values_for_normalized_basis():
    report = run_audit()
    stats = changes(report, "stats")
    changed = stats["North"]["changed"]["devices"]
    assert changed["old"] == "2"
    assert changed["new"] == "3"
    assert changed["oldNormalized"] == 2
    assert changed["newNormalized"] == 3


def test_audit_does_not_depend_on_physical_row_order(tmp_path):
    import csv
    import shutil

    def reversed_package(source, target):
        shutil.copytree(source, target)
        for name in ("device.csv", "maintenance.csv", "stats.csv"):
            path = Path(target) / name
            rows = list(csv.reader(path.open(newline="", encoding="utf-8")))
            header, body = rows[0], rows[1:]
            body.reverse()
            with path.open("w", newline="", encoding="utf-8") as file:
                writer = csv.writer(file)
                writer.writerow(header)
                writer.writerows(body)

    base = canonical(run_audit())
    reversed_package(DAY1, str(tmp_path / "old"))
    reversed_package(DAY2, str(tmp_path / "new"))
    reordered = canonical(
        audit(str(tmp_path / "old"), str(tmp_path / "new"), declaration=DECLARATION)
    )
    assert reordered == base


# Coverage


def test_audit_suppresses_deletion_outside_verified_new_coverage():
    report = run_audit()
    entries = suppressed(report, "device")
    assert entries["D-009"]["kind"] == "delete"
    assert entries["D-009"]["reason"] == "new-coverage-unknown"
    assert entries["D-009"]["evidence"]["old"]["rowNumber"] == 7
    assert entries["D-009"]["evidence"]["old"]["values"]["device_no"] == "D-009"
    assert "D-009" not in changes(report, "device")


def test_audit_still_deletes_inside_verified_new_coverage():
    report = run_audit()
    # D-003 falls inside the declared D-001..D-006 segment and has no new row
    assert changes(report, "device")["D-003"]["kind"] == "delete"


def test_audit_scope_is_taken_from_package_when_declaration_is_silent():
    report = run_audit()
    snapshot = report["snapshots"]["device"]["new"]
    assert snapshot["coverageSource"] == "package"
    assert snapshot["coverage"] == {
        "declared": True,
        "complete": False,
        "segments": [{"from": "D-001", "to": "D-006"}],
        "keys": [],
    }


# Conflicts


def test_audit_keeps_both_conflicting_device_rows_as_candidates():
    report = run_audit()
    unresolved = table(report, "device")["unresolved"]
    assert len(unresolved) == 1
    conflict = unresolved[0]
    assert conflict["reason"] == "duplicate-key"
    assert conflict["key"] == "D-006"
    candidates = conflict["candidates"]["new"]
    assert [candidate["key"]["values"]["deviceNo"] for candidate in candidates] == [
        "D-6",
        "D_6",
    ]
    assert [candidate["row"]["rowNumber"] for candidate in candidates] == [6, 7]
    # Neither candidate is promoted to an update or a delete.
    assert "D-006" not in changes(report, "device")


def test_audit_links_affected_maintenance_rows_to_the_conflict():
    report = run_audit()
    affected = [
        entry
        for entry in report["references"]
        if entry["targetKey"] == "D-006" and entry["side"] == "new"
    ]
    assert len(affected) == 2
    assert all(entry["state"] == "conflict" for entry in affected)
    assert {entry["row"]["values"]["work_order"] for entry in affected} == {
        "W-1004",
        "W-1005",
    }


def test_audit_links_unique_device_reference_to_its_audited_state():
    report = run_audit()
    unchanged = [
        entry
        for entry in report["references"]
        if entry["targetKey"] == "D-004" and entry["side"] == "new"
    ]
    assert unchanged
    # D-004 is a unique, non-conflicting device whose own row changed, so the
    # maintenance row that points at it resolves to the audited "update".
    assert all(entry["state"] == "update" for entry in unchanged)
    assert all(entry["row"]["values"]["work_order"] == "W-1006" for entry in unchanged)


def test_audit_links_unchanged_unique_device_reference():
    report = run_audit()
    unchanged = [entry for entry in report["references"] if entry["targetKey"] == "D-001"]
    assert len(unchanged) == 2
    assert all(entry["state"] == "unchanged" for entry in unchanged)


# Independent stats


def test_audit_keeps_independent_stats_results_despite_conflict():
    report = run_audit()
    stats = changes(report, "stats")
    assert stats["North"]["kind"] == "update"
    assert stats["East"]["kind"] == "update"
    assert set(stats["North"]["changed"]) == {"devices", "running"}


# Traceability


def test_audit_every_change_points_back_to_rows_and_declarations():
    report = run_audit()
    for current in report["tables"]:
        for change in current["changes"]:
            assert change["evidence"]["old"] or change["evidence"]["new"]
            assert change["columns"]
            if change["kind"] == "update":
                assert change["evidence"]["old"] and change["evidence"]["new"]
    for entry in table(report, "device")["unresolved"]:
        assert entry["candidates"]["new"]
        assert entry["evidence"]["new"]
        assert entry["columns"]
    for entry in table(report, "device")["suppressed"]:
        assert entry["evidence"]["old"]
        assert entry["columns"]


def test_audit_report_declares_columns_used_per_side():
    report = run_audit()
    device = table(report, "device")["declaration"]
    assert device["columns"]["deviceNo"]["old"] == "device_no"
    assert device["columns"]["deviceNo"]["new"] == "asset_code"
    assert device["columns"]["status"]["compare"] == "normalized"
    assert device["columns"]["model"]["compare"] == "source"


def test_audit_does_not_mutate_input_packages():
    before = {}
    for side in (DAY1, DAY2):
        for path in sorted(Path(side).glob("*")):
            before[str(path)] = path.read_bytes()
    run_audit()
    for path, content in before.items():
        assert Path(path).read_bytes() == content


# Declaration validation


def test_audit_rejects_undeclared_holding_table():
    declaration = json.loads(Path(DECLARATION).read_text())
    declaration["tables"]["device"]["references"][0]["holding"] = "missing"
    with pytest.raises(FrictionlessException):
        audit(DAY1, DAY2, declaration=declaration)


def test_audit_rejects_missing_source_column():
    declaration = json.loads(Path(DECLARATION).read_text())
    declaration["tables"]["device"]["columns"]["model"]["new"] = "absent"
    with pytest.raises(FrictionlessException):
        audit(DAY1, DAY2, declaration=declaration)
