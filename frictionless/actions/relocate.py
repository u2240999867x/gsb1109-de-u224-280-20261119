from __future__ import annotations

from typing import Dict, List, Optional

from ..migration import RelocationResult, relocate_package


def relocate(
    target: str,
    *,
    sources: List[str],
    selection: Optional[Dict[str, List[str]]] = None,
    resources: Optional[List[str]] = None,
    names: Optional[Dict[str, str]] = None,
    include_dependencies: bool = True,
    exclude: Optional[List[str]] = None,
    name: Optional[str] = None,
    title: Optional[str] = None,
    manifest: str = "relocation.json",
    validate: bool = True,
) -> RelocationResult:
    """Relocate selected resources of packages into a new package

    Parameters:
        target: path of the new package directory
        sources: paths of the source package descriptors
        selection: resource names to relocate per source descriptor
        resources: resource names to relocate from a single source
        names: explicit target names given as "<descriptor>::<resource>"
        include_dependencies: carry foreign-key referents along
        exclude: resources that must not travel, given as
            "<descriptor>::<resource>"
        name: name of the published package
        title: title of the published package
        manifest: manifest file name relative to the target root
        validate: validate the published package

    Returns:
        RelocationResult: the reopened package, its report and the manifest
    """
    return relocate_package(
        target,
        sources=sources,
        selection=selection,
        resources=resources,
        names=names,
        include_dependencies=include_dependencies,
        exclude=exclude,
        name=name,
        title=title,
        manifest=manifest,
        validate=validate,
    )
