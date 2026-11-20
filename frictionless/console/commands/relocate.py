from __future__ import annotations

import json
from typing import List, Optional

import typer

from ... import helpers as core_helpers
from ...exception import FrictionlessException
from ...migration import relocate_package
from ...system import system
from .. import common, helpers
from ..console import console


@console.command(name="relocate")
def console_relocate(
    # Source
    source: List[str] = common.source,
    selection: str = typer.Option(
        default=...,
        help="JSON mapping of a source descriptor to its resource names",
    ),
    target: str = typer.Option(
        default=...,
        help="Directory of the new package",
    ),
    # Command
    names: Optional[str] = typer.Option(
        default=None,
        help='JSON mapping of "<descriptor>::<resource>" to a target name',
    ),
    exclude: Optional[str] = typer.Option(
        default=None,
        help='Comma-separated "<descriptor>::<resource>" entries to exclude',
    ),
    dependencies: bool = typer.Option(
        default=True,
        help="Carry foreign-key referents along",
    ),
    name: Optional[str] = typer.Option(default=None, help="Package name"),
    title: Optional[str] = typer.Option(default=None, help="Package title"),
    manifest: str = typer.Option(default="relocation.json", help="Manifest file name"),
    # System
    debug: bool = common.debug,
    trusted: bool = common.trusted,
    standards: str = common.standards,
):
    """Relocate selected resources of packages into a new package"""
    if trusted:
        system.trusted = trusted
    if standards:
        system.standards = standards  # type: ignore

    try:
        sources = list(source) if isinstance(source, list) else [source]
        if not sources or sources == [None]:
            note = 'Providing "source" is required'
            raise FrictionlessException(note)
        mapping = json.loads(selection)
        explicit = json.loads(names) if names else None
        excluded = exclude.split(",") if exclude else None
        result = relocate_package(
            target,
            sources=sources,
            selection=mapping,
            names=explicit,
            include_dependencies=dependencies,
            exclude=excluded,
            name=name,
            title=title,
            manifest=manifest,
        )
    except Exception as exception:
        helpers.print_exception(debug=debug, exception=exception)
        raise typer.Exit(code=1)

    typer.echo(core_helpers.to_json(result.to_descriptor()))
    if not result.valid:
        raise typer.Exit(code=1)
