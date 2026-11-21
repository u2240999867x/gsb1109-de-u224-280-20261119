"""Run the offline collection-rename change stream repro.

Usage::

    .venv/bin/python -m repro.change_stream_rename

Writes ``report.json`` next to this package and prints the report to stdout.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path


def main() -> int:
    from repro.change_stream_rename.scenario import run_repro, write_report

    report = asyncio.run(run_repro())
    path = write_report(report, Path(__file__).with_name("report.json"))
    sys.stdout.write(report.to_json() + "\n")
    sys.stderr.write(f"\nwrote {path}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
