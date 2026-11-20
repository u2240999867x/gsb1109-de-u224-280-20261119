from __future__ import annotations

from typing import Optional

import typer

from ...compatibility import CompatibilityAudit
from .. import helpers
from ..helpers import output_console

# NOTE: `from ...console import console` would import this package's
# `console` object, not the Typer application (the package re-exports an
# attribute with the same name). Import the application module explicitly.
from ...console.console import console as app


@app.command(name="compatibility")
def console_compatibility(
    contract: str = typer.Argument(
        ...,
        help="Path to a compatibility contract (JSON or YAML)",
    ),
    # Command
    output: Optional[str] = typer.Option(
        default=None,
        help="Write the report to this path",
    ),
    yaml: bool = typer.Option(
        default=False,
        help="Output YAML instead of the human-readable report",
    ),
    json: bool = typer.Option(
        default=False,
        help="Output JSON instead of the human-readable report",
    ),
    fail_fast: bool = typer.Option(
        default=True,
        help="Exit with a non-zero code unless every allowed direction is compatible",
    ),
    debug: bool = typer.Option(
        default=False,
        help="Show the underlying exception",
    ),
):
    """Audit read/write compatibility between two package descriptors.

    The report replays the contract's raw records through each allowed
    read/write direction. A record is only compatible when its effective
    values and its cross-table reconciliation survive the round trip; a
    clean parse is not enough.
    """
    try:
        report = CompatibilityAudit.from_descriptor(contract).run()
    except Exception as exception:
        helpers.print_exception(debug=debug, exception=exception)
        raise typer.Exit(code=1)

    descriptor = report.to_descriptor()
    if yaml:
        content = report.to_yaml(path=output)
        if not output:
            print(content)
    elif json:
        content = report.to_json(path=output)
        if not output:
            print(content)
    else:
        _print_report(report)
        if output:
            report.to_json(output)
            output_console.print(f"Report written to [bold]{output}[/bold]")

    if fail_fast and report.status != "compatible":
        raise typer.Exit(code=1)


def _print_report(report) -> None:
    from rich.table import Table

    descriptor = report.to_descriptor()
    output_console.rule("[bold]Compatibility")
    summary = Table(title=report.name)
    summary.add_column("direction")
    summary.add_column("status")
    summary.add_column("rejected", justify="right")
    summary.add_column("changed", justify="right")
    summary.add_column("compatible", justify="right")
    summary.add_column("not-certifiable", justify="right")
    for direction in descriptor["directions"]:
        counts = direction["summary"]
        summary.add_row(
            direction["direction"],
            direction["status"].upper(),
            str(counts["rejected"]),
            str(counts["changed"]),
            str(counts["compatible"]),
            str(counts["not-certifiable"]),
        )
    output_console.print(summary)

    for direction in descriptor["directions"]:
        for label in ("rejected", "changed", "not-certifiable"):
            items = direction[label]
            if not items:
                continue
            output_console.rule(f"[bold]{direction['direction']} / {label}")
            view = Table()
            view.add_column("table")
            view.add_column("record")
            view.add_column("reason")
            view.add_column("evidence")
            for item in items:
                view.add_row(
                    item["table"],
                    item["record"],
                    item.get("reason", ""),
                    _evidence(item),
                )
            output_console.print(view)

    certified = [
        (
            direction["direction"],
            len(direction["certified"].get("records", [])),
        )
        for direction in descriptor["directions"]
        if direction["certified"].get("certifiable")
    ]
    if certified:
        listing = ", ".join(
            f"{direction} ({count} records)" for direction, count in certified
        )
        output_console.print(f"Certifiable range: {listing}")
    else:
        output_console.print(
            "No certifiable range from the supplied evidence."
        )
    for direction in descriptor["directions"]:
        for constraint in direction["certified"].get("uncoveredConstraints", []):
            output_console.print(
                f'Not certifiable: custom constraint "{constraint["constraint"]}" '
                f'on {constraint["table"]}.{constraint["field"]} is outside the '
                "supplied value domains."
            )


def _evidence(item) -> str:
    parts = []
    for change in item.get("valueChanges", []):
        parts.append(
            f'{change["field"]}: {change.get("baseline")} -> {change.get("observed")}'
        )
    for change in item.get("relationshipChanges", []):
        parts.append(
            f'{change["relationship"]}: {change["baseline"]["state"]} -> '
            f'{change["observed"]["state"]}'
        )
    for error in item.get("writeErrors", []):
        field = error.get("field")
        note = error.get("note") or error.get("message") or error.get("type", "")
        parts.append(f"write {field}: {note}" if field else f"write: {note}")
    for error in item.get("headerErrors", []):
        parts.append(f'header: {error.get("type")} {error.get("note", "")}'.strip())
    for error in item.get("rowErrors", []):
        parts.append(f'row: {error.get("type")} {error.get("note", "")}'.strip())
    return "; ".join(parts)
