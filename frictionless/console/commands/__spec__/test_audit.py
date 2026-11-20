import json

import yaml

from frictionless.console import console

from .conftest import create_runner

runner = create_runner()

DAY1 = "data/audit/day-1"
DAY2 = "data/audit/day-2"
DECLARATION = "data/audit/declaration.json"


def test_console_audit_json():
    actual = runner.invoke(console, f"audit {DAY1} {DAY2} --declaration {DECLARATION}")
    assert actual.exit_code == 0
    report = json.loads(actual.stdout)
    assert report["summary"]["device"]["conflict"] == 1
    assert report["summary"]["device"]["suppressed"] == 1


def test_console_audit_yaml():
    actual = runner.invoke(
        console, f"audit {DAY1} {DAY2} --declaration {DECLARATION} --yaml"
    )
    assert actual.exit_code == 0
    report = yaml.safe_load(actual.stdout)
    assert report["summary"]["stats"]["update"] == 2


def test_console_audit_output_file(tmp_path):
    path = tmp_path / "report.json"
    actual = runner.invoke(
        console,
        f"audit {DAY1} {DAY2} --declaration {DECLARATION} --output {path}",
    )
    assert actual.exit_code == 0
    report = json.loads(path.read_text())
    assert report["summary"]["maintenance"]["add"] == 3


def test_console_audit_missing_declaration():
    actual = runner.invoke(console, f"audit {DAY1} {DAY2}")
    assert actual.exit_code != 0
