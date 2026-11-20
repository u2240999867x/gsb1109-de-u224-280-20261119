from __future__ import annotations

import json
from typing import Optional

import typer
import yaml

from ...auditing import audit
from .. import helpers
from ..console import console


@console.command(name="audit")
def console_audit(
    old: str = typer.Argument(..., help="Old Data Package path"),
    new: str = typer.Argument(..., help="New Data Package path"),
    declaration: str = typer.Option(
        ...,
        "--declaration",
        "-d",
        help="Audit declaration path (JSON or YAML)",
    ),
    output: Optional[str] = typer.Option(
        None,
        "--output",
        "-o",
        help="Write the report to this path instead of stdout",
    ),
    yaml_output: bool = typer.Option(
        False,
        "--yaml",
        help="Emit YAML instead of JSON",
    ),
    debug: bool = typer.Option(False, "--debug", help="Show exception traceback"),
):
    """Audit the changes between two Data Package snapshots.

    The declaration names each table's business key, the old/new column
    correspondence, the value-equivalence rules, and any verified coverage.
    The report can be traced back to the actual rows and declarations used.
    """
    try:
        report = audit(old, new, declaration=declaration)
    except Exception as exception:
        helpers.print_exception(debug=debug, exception=exception)
        raise typer.Exit(code=1)

    if yaml_output:
        content = yaml.safe_dump(report, allow_unicode=True, sort_keys=False)
    else:
        content = json.dumps(report, ensure_ascii=False, indent=2)

    if output:
        with open(output, "w", encoding="utf-8") as file:
            file.write(content)
            if not content.endswith("\n"):
                file.write("\n")
        typer.echo(output)
    else:
        typer.echo(content)
