import json

from frictionless.console import console

from .conftest import create_runner

runner = create_runner()

SILENT = "data/compatibility/equipment-maintenance-silent.json"
FULL = "data/compatibility/equipment-maintenance.json"


def test_console_compatibility_json():
    actual = runner.invoke(console, f"compatibility {SILENT} --json")
    assert actual.exit_code == 1
    report = json.loads(actual.stdout)
    assert report["status"] == "changed"
    direction = report["directions"][0]
    assert direction["direction"] == "old-write/new-read"
    assert direction["summary"] == {
        "rejected": 0,
        "changed": 2,
        "compatible": 1,
        "not-certifiable": 1,
    }
    lost = [
        item
        for item in direction["changed"]
        if item["record"] == "mt-1-linked-to-missing-word"
    ][0]
    assert lost["relationshipChanges"][0]["observed"]["state"] == "unassociated"


def test_console_compatibility_rejections_and_certification():
    actual = runner.invoke(console, f"compatibility {FULL} --json")
    assert actual.exit_code == 1
    report = json.loads(actual.stdout)
    assert report["summary"]["records"]["rejected"] == 6
    for direction in report["directions"]:
        assert direction["certified"]["certifiable"] is False
        assert direction["certified"]["uncoveredConstraints"]
    old_to_new = report["directions"][0]
    rejected = [
        item for item in old_to_new["rejected"] if item["record"].startswith("mt-3")
    ][0]
    assert rejected["rowErrors"][0]["note"] == 'constraint "maximum" is "8"'
    assert rejected["wire"]["cells"] == ["3", "9998", "12"]


def test_console_compatibility_output_file(tmpdir):
    output = str(tmpdir.join("report.json"))
    actual = runner.invoke(console, f"compatibility {SILENT} --json --output {output}")
    assert actual.exit_code == 1
    with open(output, encoding="utf-8") as file:
        assert json.load(file)["status"] == "changed"


def test_console_compatibility_unknown_contract():
    actual = runner.invoke(console, "compatibility data/table.csv")
    assert actual.exit_code == 1
    assert "compatibility" in actual.stderr.lower()
