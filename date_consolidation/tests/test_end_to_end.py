from datetime import date
from dataclasses import replace
from pathlib import Path

from date_consolidation.cli import run
from date_consolidation.config import load_config
from date_consolidation.consolidate import consolidate
from date_consolidation.export import export_table, read_back
from date_consolidation.report import build_report

EXAMPLE = Path(__file__).parents[1] / "example"


def _records_by_line(result):
    return {record.physical_line: record for record in result.records if record.file_path == "stores_dates.csv"}


def test_config_drives_allowed_forms_only():
    config = load_config(EXAMPLE / "config.yaml")
    assert config.source("hq").iso_format.pattern == "%Y-%m-%d"
    assert len(config.source("store_north").legacy_formats) == 2
    assert config.stable_format == "%Y-%m-%d"


def test_end_to_end_verdicts_and_physical_lines():
    config = load_config(EXAMPLE / "config.yaml")
    result = consolidate(config, base_dir=EXAMPLE)
    rows = _records_by_line(result)

    # Header is line 1, so data lines start at 2.
    assert rows[2].raw_text == "05/03/2024"
    north = result.groups[("store_north", "v7")]
    south = result.groups[("store_south", "v7")]
    hq = result.groups[("hq", "v7")]

    assert not north.consistent
    assert south.consistent
    assert hq.consistent

    # North: pinned day-first at line 5, contradicted by the line-9 tail.
    assert rows[5].verdict.kind == "confirmed"
    assert rows[5].standardized == date(2024, 2, 13)
    assert rows[8].verdict.kind == "confirmed"
    assert rows[8].standardized == date(2024, 2, 25)
    assert rows[9].verdict.kind == "confirmed"
    assert rows[9].standardized == date(2024, 2, 25)
    contradiction = north.contradictions[0]
    assert contradiction.physical_line == 9
    assert contradiction.raw_text == "02/25/2024"

    # Contradiction blocks group-pin confirmation of the truly ambiguous cell.
    assert rows[2].verdict.kind == "ambiguous"
    assert rows[2].standardized is None
    assert rows[2].verdict.values == [date(2024, 3, 5), date(2024, 5, 3)]

    # Same raw shape, different trust: south's pin confirms 09/09, while the
    # inconsistent north group leaves 07/07 at same-day/unknown-format.
    assert rows[3].verdict.kind == "confirmed"
    assert rows[3].standardized == date(2024, 9, 9)
    assert rows[7].verdict.kind == "same_day_unknown_format"
    assert rows[7].standardized == date(2024, 7, 7)

    # South: month-first pin resolves its otherwise ambiguous row.
    assert rows[4].verdict.kind == "confirmed"
    assert rows[4].standardized == date(2024, 6, 4)
    assert rows[4].readings  # both orders legal before the pin decided it
    assert rows[6].source == "store_south" and rows[5].source == "store_north"


def test_hq_rows():
    config = load_config(EXAMPLE / "config.yaml")
    result = consolidate(config, base_dir=EXAMPLE)
    hq_rows = [r for r in result.records if r.file_path == "hq_dates.csv"]
    assert [(r.physical_line, r.verdict.kind) for r in hq_rows] == [
        (2, "confirmed"),
        (3, "unparseable"),
    ]
    assert hq_rows[0].standardized == date(2024, 2, 15)


def test_report_quotes_raw_text_and_lines():
    config = load_config(EXAMPLE / "config.yaml")
    report = build_report(consolidate(config, base_dir=EXAMPLE))
    assert "stores_dates.csv:9" in report
    assert "raw='02/25/2024'" in report
    assert "self_consistent=False" in report
    assert "2024-03-05" in report and "2024-05-03" in report


def test_export_round_trips_through_date_field(tmp_path):
    output_dir = run(EXAMPLE / "config.yaml", tmp_path)
    table = output_dir / "consolidated.csv"
    verified = read_back(table, "%Y-%m-%d")

    by_key = {(row["source_file"], row["source_line"]): row for row in verified}
    ambiguous = by_key[("stores_dates.csv", "2")]
    assert ambiguous["standardized_date"] == ""
    assert ambiguous["standardized_date_parsed"] is None
    confirmed = by_key[("stores_dates.csv", "4")]
    assert confirmed["standardized_date"] == "2024-06-04"
    assert confirmed["standardized_date_parsed"] == date(2024, 6, 4)

    # All non-empty standardized cells survived the DateField read-back.
    assert all(
        row["standardized_date_parsed"] is not None
        for row in verified
        if row["standardized_date"]
    )
    assert (output_dir / "report.txt").exists()
    assert (output_dir / "readback_check.txt").exists()


def test_stable_format_is_configurable_and_round_trips(tmp_path):
    config = replace(load_config(EXAMPLE / "config.yaml"), stable_format="%d/%m/%Y")
    result = consolidate(config, base_dir=EXAMPLE)
    table = tmp_path / "alt.csv"
    export_table(result, table)
    verified = read_back(table, "%d/%m/%Y")

    confirmed = next(
        row for row in verified if (row["source_file"], row["source_line"]) == ("stores_dates.csv", "6")
    )
    assert confirmed["standardized_date"] == "13/04/2024"
    assert confirmed["standardized_date_parsed"] == date(2024, 4, 13)

