"""Run the calibration-switch diagnosis and print a reviewable report."""

from __future__ import annotations

import argparse
import sys

from .model import run_all
from .report import render_json, render_markdown


def main(argv=None):
    parser = argparse.ArgumentParser(
        prog="python -m diagnostics.calibration_switch",
        description=(
            "Deterministic local diagnosis for the A/B/C combine_latest "
            "calibration-switch topology."
        ),
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of Markdown",
    )
    parser.add_argument(
        "--include-values",
        action="store_true",
        help="include local record values in the timeline (never source payload)",
    )
    args = parser.parse_args(argv)

    results = run_all()
    if args.json:
        output = render_json(results, include_values=args.include_values)
    else:
        output = render_markdown(results, include_values=args.include_values)
    sys.stdout.write(output)
    if not output.endswith("\n"):
        sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
