"""Command line entry point: consolidate, report, export and verify."""

from __future__ import annotations

import argparse
from pathlib import Path

from .config import load_config
from .consolidate import consolidate
from .export import export_table, read_back
from .report import build_report


def run(config_path: str | Path, output_dir: str | Path | None = None) -> Path:
    config_path = Path(config_path)
    base_dir = config_path.parent
    config = load_config(config_path)
    result = consolidate(config, base_dir=base_dir)

    if output_dir is None:
        output_dir = base_dir / "output"
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    report_path = output_dir / "report.txt"
    report_path.write_text(build_report(result), encoding="utf-8")

    table_path = output_dir / "consolidated.csv"
    export_table(result, table_path)

    # Re-read through DateField in the stable format; raises if any written
    # cell fails to parse, proving the single-format read/write path.
    verified = read_back(table_path, result.stable_format)
    verified_path = output_dir / "readback_check.txt"
    lines = [
        f"rows={len(verified)} stable_format={result.stable_format}",
        "standardized cells re-read through DateField successfully:",
    ]
    for row in verified:
        parsed = row["standardized_date_parsed"]
        lines.append(
            f"  {row['source_file']}:{row['source_line']} raw={row['raw_text']!r} "
            f"evidence={row['evidence']} standardized={parsed}"
        )
    verified_path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    return output_dir


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("config", help="path to the consolidation YAML config")
    parser.add_argument("--output-dir", help="directory for report and export")
    args = parser.parse_args()
    output_dir = run(args.config, args.output_dir)
    print(f"wrote report and consolidated table to {output_dir}")


if __name__ == "__main__":
    main()
