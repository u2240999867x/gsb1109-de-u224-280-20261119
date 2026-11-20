from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .. import helpers, types
from ..exception import FrictionlessException
from ..package import Package
from ..resource import Resource
from .errors import RelocationError
from .result import RelocationMapping, RelocationResult

# Properties that describe *where* and *how* a resource is transported rather
# than the business meaning of its table. They are re-derived for the target
# instead of being copied. Everything else is carried over and recorded.
_TRANSPORT_PROPERTIES = {
    "name",
    "type",
    "path",
    "data",
    "scheme",
    "format",
    "mediatype",
    "hash",
    "bytes",
    "fields",
    "rows",
    "schema",
    "stats",
    "extrapaths",
    "innerpath",
    "profile",
    "$schema",
}


def relocate_package(
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
    """Publish selected resources of several packages as a new package

    Each source package is read-only: its files are streamed from their
    original location and written into the target root. Resources sharing a
    name across source packages (for example each package's own `codes`
    dictionary) keep separate identities in the target.

    With `include_dependencies` (the default), every selected resource pulls
    the referents of its foreign keys into the target, transitively. Without
    it, a foreign key that cannot be resolved *inside the target package* is
    refused before anything is written: the relocation is never half
    published, and a same-named resource of another source package is never
    used to mask a missing dependency.

    Parameters:
        target: path of the new package directory (empty or non-existent)
        sources: paths of the source package descriptors
        selection: mapping of a source descriptor path to the resource names
            to relocate from it
        resources: alternative to `selection` for a single source
        names: target names per source resource, given as
            `{"<source descriptor>::<resource name>": "<target name>"}`
        include_dependencies: whether foreign-key referents are carried along
        exclude: source resources given as `"<descriptor>::<resource>"` that
            must not travel even as a dependency (an excluded dependency is
            reported as a broken relation instead of being silently replaced)
        name: name of the published package
        title: title of the published package
        manifest: file name of the manifest, relative to the target root
        validate: validate the published package after writing it

    Returns:
        RelocationResult: the package reopened from its new location, its
        validation report and the recorded source/target mapping
    """
    sources = [str(source) for source in sources]
    sources = _canonical_sources(sources)
    if len(sources) != len(set(sources)):
        raise _fail("the same source package is provided more than once")
    selection = _normalize_selection(sources, selection, resources)
    if len(selection) != len(sources):
        missing = [source for source in sources if source not in selection]
        raise _fail(f"no resource is selected from: {', '.join(missing)}")
    excluded = _normalize_exclusion(sources, exclude or [])

    # Load the sources. Their descriptors and data files are only read.
    loaded = _load_sources(sources)
    evidence = _source_evidence(loaded)

    # Resolve what has to travel and how it will be named
    closure = _resolve_closure(loaded, selection, include_dependencies, excluded)
    identities = _assign_identities(loaded, closure, selection, names or {})

    # Refuse before writing anything if the result could not be complete
    _guard_target(target, manifest, loaded)
    _guard_sources_unchanged(evidence)
    _guard_references(loaded, closure, identities, include_dependencies)

    root = os.path.abspath(target)
    staging = _staging_path(root)
    shutil.rmtree(staging, ignore_errors=True)
    try:
        mappings = _publish(staging, loaded, closure, identities)
        descriptor_path = os.path.join(staging, "datapackage.json")
        package_descriptor = _package_descriptor(
            loaded, closure, identities, mappings, name=name, title=title
        )
        helpers.write_file(descriptor_path, helpers.to_json(package_descriptor))

        manifest_path = os.path.join(staging, manifest)
        manifest_descriptor = {
            "relocation": 1,
            "sources": evidence,
            "target": {
                "root": root,
                "descriptor": "datapackage.json",
                "manifest": manifest,
            },
            "relations": _relations(loaded, closure, identities),
            "resources": [mapping.to_descriptor() for mapping in mappings],
        }
        helpers.write_file(manifest_path, helpers.to_json(manifest_descriptor))

        # Validate in the staging area first: an invalid package is never
        # promoted to the target location
        staged = Package(descriptor_path)
        staged_report = staged.validate() if validate else None
        if staged_report is not None and not staged_report.valid:
            raise _fail(
                "the published package is not valid: " + _format_report(staged_report)
            )
        _assert_sources_unchanged(evidence, mappings)

        _replace_directory(staging, root)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise

    # Reopen from the new location, detached from the source roots, and
    # validate again: this proves every relative path resolves there and the
    # foreign keys are checked against the target's own resources
    descriptor_path = os.path.join(root, "datapackage.json")
    manifest_path = os.path.join(root, manifest)
    package = Package(descriptor_path)
    report = package.validate() if validate else None
    if report is not None and not report.valid:
        shutil.rmtree(root, ignore_errors=True)
        raise _fail("the published package is not valid: " + _format_report(report))

    if report is not None:
        manifest_descriptor["validation"] = {
            "valid": report.valid,
            "resources": [
                {
                    "name": task.name,
                    "valid": task.valid,
                    "errors": [error.type for error in task.errors],
                }
                for task in report.tasks
            ],
        }
        helpers.write_file(manifest_path, helpers.to_json(manifest_descriptor))
    return RelocationResult(
        package=package,
        root=root,
        descriptor_path=descriptor_path,
        manifest_path=manifest_path,
        manifest=manifest_descriptor,
        report=report,
    )


# Selection


def _canonical_sources(sources: List[str]) -> List[str]:
    canonical: List[str] = []
    for source in sources:
        if helpers.is_remote_path(source):
            canonical.append(source)
        else:
            canonical.append(os.path.abspath(source))
    return canonical


def _resolve_source_keys(sources: List[str], values: Dict[str, Any]) -> Dict[str, Any]:
    """Accept selection/name/exclusion keys as given or as absolute paths"""
    lookup: Dict[str, str] = {}
    for source in sources:
        lookup[source] = source
        lookup[os.path.abspath(source)] = source
        lookup[os.path.normpath(source)] = source
        lookup[os.path.normpath(os.path.abspath(source))] = source
    resolved: Dict[str, Any] = {}
    unknown: List[str] = []
    for key, value in values.items():
        match = lookup.get(key) or lookup.get(os.path.abspath(key))
        if not match:
            unknown.append(key)
            continue
        resolved[match] = value
    if unknown:
        raise _fail(
            f"the selection refers to unknown sources: {', '.join(sorted(unknown))}"
        )
    return resolved


def _normalize_selection(
    sources: List[str],
    selection: Optional[Dict[str, List[str]]],
    resources: Optional[List[str]],
) -> Dict[str, List[str]]:
    if not sources:
        raise _fail("at least one source package is required")
    if selection is not None and resources is not None:
        raise _fail('use either "selection" or "resources", not both')
    if resources is not None:
        if len(sources) != 1:
            raise _fail('the "resources" argument applies to a single source')
        return {sources[0]: list(resources)}

    normalized = {
        source: list(names)
        for source, names in _resolve_source_keys(sources, dict(selection or {})).items()
    }
    if not normalized:
        raise _fail("a resource selection is required")
    empty = [source for source, names in normalized.items() if not names]
    if empty:
        raise _fail(f"no resource is selected from: {', '.join(empty)}")
    return normalized


# Loading


def _load_sources(sources: List[str]) -> Dict[str, Dict[str, Any]]:
    loaded: Dict[str, Dict[str, Any]] = {}
    for descriptor in sources:
        package = Package(descriptor)
        loaded[descriptor] = {
            "package": package,
            "basepath": os.path.abspath(package.basepath or "."),
        }
    return loaded


def _source_evidence(loaded: Dict[str, Dict[str, Any]]) -> List[Dict[str, Any]]:
    evidence: List[Dict[str, Any]] = []
    for descriptor, state in loaded.items():
        package = state["package"]
        evidence.append(
            {
                "descriptor": os.path.abspath(descriptor),
                "package": package.name,
                "basepath": state["basepath"],
                "sha256": _file_digest(descriptor),
            }
        )
    return evidence


def _guard_sources_unchanged(evidence: List[Dict[str, Any]]) -> None:
    for item in evidence:
        if _file_digest(item["descriptor"]) != item["sha256"]:
            raise _fail(f'the source descriptor "{item["descriptor"]}" has changed')


def _assert_sources_unchanged(
    evidence: List[Dict[str, Any]], mappings: List[RelocationMapping]
) -> None:
    _guard_sources_unchanged(evidence)
    for mapping in mappings:
        if _file_digest(mapping.source_path) != mapping.data_sha256:
            raise _fail(f'the source data "{mapping.source_path}" has changed')


# Exclusion


def _normalize_exclusion(sources: List[str], exclude: List[str]) -> set:
    excluded = set()
    for item in exclude:
        if "::" not in item:
            raise _fail(
                f'the exclusion "{item}" must be given as "<descriptor>::<resource>"'
            )
        descriptor, resource = item.split("::", 1)
        excluded.add((_resolve_source_key(sources, descriptor), resource))
    return excluded


# Closure


def _selected(
    loaded: Dict[str, Dict[str, Any]], selection: Dict[str, List[str]]
) -> List[Tuple[str, str]]:
    selected: List[Tuple[str, str]] = []
    problems: List[str] = []
    for descriptor in loaded:
        for name in selection[descriptor]:
            if not loaded[descriptor]["package"].has_resource(name):
                problems.append(f'resource "{name}" does not exist in "{descriptor}"')
                continue
            selected.append((descriptor, name))
    if problems:
        raise _fail("; ".join(problems))
    return selected


def _resolve_closure(
    loaded: Dict[str, Dict[str, Any]],
    selection: Dict[str, List[str]],
    include_dependencies: bool,
    excluded: Optional[set] = None,
) -> List[Tuple[str, str]]:
    excluded = excluded or set()
    selected = _selected(loaded, selection)
    for key in selected:
        if key in excluded:
            raise _fail(
                f'the resource "{key[1]}" of "{key[0]}" is both selected and excluded'
            )
    closure: List[Tuple[str, str]] = list(selected)
    seen = set(closure)
    queue = list(selected)
    while queue:
        descriptor, name = queue.pop(0)
        resource = loaded[descriptor]["package"].get_resource(name)
        for fk in resource.schema.foreign_keys:
            referent = fk.get("reference", {}).get("resource") or ""
            if not referent or referent == name:
                continue
            package = loaded[descriptor]["package"]
            if not package.has_resource(referent):
                raise _fail(
                    _relation_note(descriptor, name, referent, fk)
                    + " (the referent does not exist in its own source package)"
                )
            if not include_dependencies:
                continue
            key = (descriptor, referent)
            if key in excluded:
                continue
            if key not in seen:
                seen.add(key)
                closure.append(key)
                queue.append(key)
    return closure


def _relation_note(descriptor: str, name: str, referent: str, fk: Dict[str, Any]) -> str:
    fields = ", ".join(fk.get("fields", []))
    reference_fields = ", ".join(fk.get("reference", {}).get("fields", []))
    return (
        f'the selected resource "{name}" of "{descriptor}" cannot complete its '
        f"foreign key ({fields} -> {referent}.{reference_fields}) because the "
        f'referenced resource "{referent}" is not part of the target package'
    )


# Identities


def _assign_identities(
    loaded: Dict[str, Dict[str, Any]],
    closure: List[Tuple[str, str]],
    selection: Dict[str, List[str]],
    names: Dict[str, str],
) -> Dict[Tuple[str, str], RelocationMapping]:
    requested: Dict[Tuple[str, str], str] = {}
    sources = list(loaded)
    for item, target in names.items():
        if "::" not in item:
            raise _fail(
                f'the target name "{item}" must be given as "<descriptor>::<resource>"'
            )
        descriptor, resource = item.split("::", 1)
        key = (_resolve_source_key(sources, descriptor), resource)
        if key not in closure:
            raise _fail(
                f'the target name is given for "{key[1]}" of "{key[0]}" '
                "which is not part of the relocation"
            )
        requested[key] = target

    package_names: Dict[str, str] = {}
    name_counts: Dict[str, int] = {}
    for descriptor in loaded:
        package = loaded[descriptor]["package"]
        package_names[descriptor] = (
            helpers.slugify(package.name or "", regex_pattern=r"[^-a-z0-9._]") or "source"
        )
    for descriptor, name in closure:
        name_counts[name] = name_counts.get(name, 0) + 1

    used: Dict[str, int] = {}
    identities: Dict[Tuple[str, str], RelocationMapping] = {}
    for descriptor, name in closure:
        key = (descriptor, name)
        role = "selected" if name in selection[descriptor] else "dependency"
        if key in requested:
            target = _unique_name(requested[key], used, explicit=True)
            rule = "explicit"
        elif name_counts[name] > 1:
            # The same resource name comes from different source packages:
            # qualify each one so that the two dictionaries stay independent.
            target = _unique_name(f"{package_names[descriptor]}.{name}", used)
            rule = "package-qualified"
        else:
            target = _unique_name(name, used)
            rule = "preserved"
        identities[key] = RelocationMapping(
            source=os.path.abspath(descriptor),
            resource=name,
            target=target,
            role=role,
            source_path="",
            target_path="",
            identity_rule=rule,
            material="copied",
            schema="inline",
            foreign_keys=[],
            properties={},
        )
    return identities


def _resolve_source_key(sources: List[str], value: str) -> str:
    for source in sources:
        candidates = {
            source,
            os.path.abspath(source),
            os.path.normpath(source),
            os.path.normpath(os.path.abspath(source)),
        }
        if value in candidates:
            return source
    raise _fail(f'the source "{value}" is not part of the relocation')


def _unique_name(candidate: str, used: Dict[str, int], *, explicit: bool = False) -> str:
    slug = helpers.slugify(candidate, regex_pattern=r"[^-a-z0-9._]") or candidate
    if slug not in used:
        used[slug] = 1
        return slug
    if explicit:
        raise _fail(f'the target name "{slug}" is requested more than once')
    used[slug] += 1
    return f"{slug}-{used[slug]}"


# Guarding


def _guard_target(target: str, manifest: str, loaded: Dict[str, Dict[str, Any]]) -> None:
    root = os.path.abspath(target)
    if os.path.exists(root):
        if not os.path.isdir(root):
            raise _fail(f'the target "{target}" is not a directory')
        if os.listdir(root):
            raise _fail(f'the target directory "{target}" is not empty')
    if os.path.isabs(manifest) or not helpers.is_safe_path(manifest):
        raise _fail(f'the manifest path "{manifest}" is not safe')
    if manifest in ("datapackage.json",):
        raise _fail(f'the manifest path "{manifest}" is reserved')
    for descriptor, state in loaded.items():
        basepath = state["basepath"]
        if _is_within(root, basepath) or _is_within(basepath, root):
            raise _fail(
                f'the target "{target}" overlaps the source package "{descriptor}"'
            )


def _is_within(path: str, parent: str) -> bool:
    path = os.path.realpath(path)
    parent = os.path.realpath(parent)
    return path == parent or path.startswith(parent + os.sep)


def _guard_references(
    loaded: Dict[str, Dict[str, Any]],
    closure: List[Tuple[str, str]],
    identities: Dict[Tuple[str, str], RelocationMapping],
    include_dependencies: bool,
) -> None:
    included = set(closure)
    for descriptor, name in closure:
        resource = loaded[descriptor]["package"].get_resource(name)
        for fk in resource.schema.foreign_keys:
            referent = fk.get("reference", {}).get("resource") or ""
            if not referent or referent == name:
                continue
            if (descriptor, referent) not in included:
                reason = (
                    "the dependency was explicitly excluded"
                    if include_dependencies
                    else "foreign-key dependencies were not carried along"
                )
                raise _fail(
                    _relation_note(descriptor, name, referent, fk) + f" ({reason})"
                )
            if not identities[(descriptor, referent)].target:
                raise _fail(_relation_note(descriptor, name, referent, fk))


# Publishing


def _publish(
    staging: str,
    loaded: Dict[str, Dict[str, Any]],
    closure: List[Tuple[str, str]],
    identities: Dict[Tuple[str, str], RelocationMapping],
) -> List[RelocationMapping]:
    mappings: List[RelocationMapping] = []
    materials: Dict[Tuple[int, int], str] = {}
    schemas: Dict[str, Tuple[str, str]] = {}
    for descriptor, name in closure:
        source = loaded[descriptor]["package"].get_resource(name)
        identity = identities[(descriptor, name)]
        if not source.tabular:
            raise _fail(f'resource "{name}" of "{descriptor}" is not a table')
        source_path = _absolute_data_path(source)
        _guard_single_file(source, descriptor)
        digest = _file_digest(source_path)
        identity.source_path = source_path
        _ensure_schema(source)

        material_key = _material_key(source_path)
        if material_key in materials:
            # The very same source file (same device and inode) is shared by
            # two resources: copy it once, but keep each resource's own
            # descriptor, schema and validation.
            identity.material = "reused"
            identity.target_path = materials[material_key]
        else:
            suffix = "".join(Path(source_path).suffixes)
            relative = os.path.join("data", f"{identity.target}{suffix}")
            _copy_material(source_path, os.path.join(staging, relative))
            materials[material_key] = relative
            identity.target_path = relative
        identity.data_sha256 = digest

        identity.foreign_keys = _published_foreign_keys(
            source, identity, identities, descriptor
        )
        identity.properties = _business_properties(source, source_path)
        schema_target = _write_schema(staging, source, identity, schemas)
        identity.schema = "inline" if schema_target is None else "relative"
        identity.schema_target = schema_target
        identity.validation = "package"
        mappings.append(identity)
    return mappings


def _ensure_schema(source: Resource) -> None:
    """Infer a schema the source descriptor did not declare

    A resource without an explicit schema gets one when it is opened. The
    published descriptor records that inferred schema so the target reads the
    same field types and constraints as the source package did.
    """
    if source.schema.to_descriptor().get("fields"):
        return
    with source:
        pass


def _material_key(path: str) -> Tuple[int, int]:
    stat = os.stat(path)
    return (stat.st_dev, stat.st_ino)


def _absolute_data_path(resource: Resource) -> str:
    path = resource.normpath
    if not path or helpers.is_remote_path(path):
        raise _fail(f'resource "{resource.name}" has no local data file')
    return os.path.abspath(path)


def _guard_single_file(source: Resource, descriptor: str) -> None:
    if source.extrapaths:
        raise _fail(
            f'resource "{source.name}" of "{descriptor}" is multipart and cannot be relocated'
        )
    if source.innerpath:
        raise _fail(
            f'resource "{source.name}" of "{descriptor}" is loaded from an archive '
            "and cannot be relocated"
        )


def _copy_material(source: str, target: str) -> None:
    helpers.ensure_dir(target)
    shutil.copyfile(source, target)


def _write_schema(
    staging: str,
    source: Resource,
    identity: RelocationMapping,
    schemas: Dict[str, Tuple[str, str]],
) -> Optional[str]:
    schema = source.schema
    descriptor_path = getattr(schema, "metadata_descriptor_path", None)
    if not descriptor_path:
        return None
    schema_source = os.path.abspath(
        helpers.normalize_path(descriptor_path, basepath=source.basepath)
    )
    relative = _schema_relative_path(source, schema_source, identity)
    payload = schema.to_descriptor()
    payload["foreignKeys"] = identity.foreign_keys
    payload_text = helpers.to_json(payload)
    # Keep the source's relative layout when possible; disambiguate only when
    # another resource already published a different schema at that path.
    previous = schemas.get(relative)
    if previous and previous[0] != schema_source:
        relative = os.path.join("schemas", f"{identity.target}.schema.json")
    elif previous and previous[1] != payload_text:
        relative = os.path.join("schemas", f"{identity.target}.schema.json")
    helpers.write_file(os.path.join(staging, relative), payload_text)
    schemas[relative] = (schema_source, payload_text)
    identity.schema_source = schema_source
    return relative


def _schema_relative_path(
    source: Resource, schema_source: str, identity: RelocationMapping
) -> str:
    basepath = os.path.abspath(source.basepath or ".")
    relative = os.path.relpath(schema_source, basepath)
    if relative.startswith("..") or os.path.isabs(relative):
        relative = os.path.join("schemas", f"{identity.target}.schema.json")
    return relative


def _published_foreign_keys(
    source: Resource,
    identity: RelocationMapping,
    identities: Dict[Tuple[str, str], RelocationMapping],
    descriptor: str,
) -> List[Dict[str, Any]]:
    published: List[Dict[str, Any]] = []
    for fk in source.schema.foreign_keys:
        reference = fk.get("reference", {})
        referent = reference.get("resource") or ""
        if not referent or referent == identity.resource:
            published.append(
                {
                    "fields": list(fk.get("fields", [])),
                    "reference": {
                        "resource": "",
                        "fields": list(reference.get("fields", [])),
                    },
                }
            )
            continue
        key = (descriptor, referent)
        if key not in identities:
            raise _fail(
                _relation_note(descriptor, identity.resource, referent, fk)
                + " (the dependency was explicitly excluded)"
            )
        published.append(
            {
                "fields": list(fk.get("fields", [])),
                "reference": {
                    "resource": identities[key].target,
                    "fields": list(reference.get("fields", [])),
                },
            }
        )
    return published


def _business_properties(resource: Resource, source_path: str) -> Dict[str, Any]:
    descriptor = resource.to_descriptor()
    properties = {
        key: value
        for key, value in descriptor.items()
        if key not in _TRANSPORT_PROPERTIES
    }
    # The encoding is a reading constraint: it has to be declared in the
    # target for the copied file to be parsed exactly as in the source
    # package. A dialect is published on the target descriptor as well.
    if resource.encoding:
        properties["encoding"] = resource.encoding
    properties.pop("stats", None)
    return properties


def _package_descriptor(
    loaded: Dict[str, Dict[str, Any]],
    closure: List[Tuple[str, str]],
    identities: Dict[Tuple[str, str], RelocationMapping],
    mappings: List[RelocationMapping],
    *,
    name: Optional[str],
    title: Optional[str],
) -> types.IDescriptor:
    by_key = {(mapping.source, mapping.resource): mapping for mapping in mappings}
    resources: List[Dict[str, Any]] = []
    for descriptor, resource_name in sorted(
        closure, key=lambda key: identities[key].target
    ):
        mapping = by_key[(os.path.abspath(descriptor), resource_name)]
        source = loaded[descriptor]["package"].get_resource(resource_name)
        entry: Dict[str, Any] = {
            "name": mapping.target,
            "type": "table",
            "path": mapping.target_path,
        }
        entry["schema"] = (
            mapping.schema_target
            if mapping.schema_target
            else _inline_schema(source, mapping)
        )
        dialect = source.dialect.to_descriptor()
        if dialect:
            entry["dialect"] = dialect
        for key, value in mapping.properties.items():
            entry[key] = value
        resources.append(entry)

    package_name = helpers.slugify(
        name or "relocated-package", regex_pattern=r"[^-a-z0-9._]"
    )
    descriptor: Dict[str, Any] = {
        "name": package_name or "relocated-package",
        "resources": resources,
    }
    if title:
        descriptor["title"] = title
    return descriptor


def _inline_schema(source: Resource, mapping: RelocationMapping) -> types.IDescriptor:
    descriptor = source.schema.to_descriptor()
    descriptor["foreignKeys"] = mapping.foreign_keys
    return descriptor


# Report


def _format_report(report: Any) -> str:
    problems: List[str] = []
    for task in report.tasks:
        for error in task.errors:
            problems.append(f"{task.name}: {error.type} ({error.note})")
    return "; ".join(problems) or "unknown validation error"


# Files


def _file_digest(path: str) -> str:
    hasher = hashlib.sha256()
    with open(path, "rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def _staging_path(root: str) -> str:
    parent = os.path.dirname(root) or "."
    base = os.path.basename(root)
    return os.path.join(parent, f".{base}.relocating")


def _replace_directory(staging: str, root: str) -> None:
    if os.path.exists(root):
        shutil.rmtree(root)
    os.replace(staging, root)


def _fail(note: str) -> FrictionlessException:
    return FrictionlessException(RelocationError(note=note))


def _relations(
    loaded: Dict[str, Dict[str, Any]],
    closure: List[Tuple[str, str]],
    identities: Dict[Tuple[str, str], RelocationMapping],
) -> List[Dict[str, Any]]:
    relations: List[Dict[str, Any]] = []
    for descriptor, name in closure:
        source = loaded[descriptor]["package"].get_resource(name)
        identity = identities[(descriptor, name)]
        for fk in source.schema.foreign_keys:
            reference = fk.get("reference", {})
            referent = reference.get("resource") or ""
            if referent and referent != name:
                key = (descriptor, referent)
                target = identities[key].target if key in identities else None
            else:
                target = identity.target
            relations.append(
                {
                    "source": os.path.abspath(descriptor),
                    "resource": name,
                    "target": identity.target,
                    "fields": list(fk.get("fields", [])),
                    "reference": {
                        "resource": referent or name,
                        "fields": list(reference.get("fields", [])),
                        "target": target,
                    },
                    "resolvedInTarget": target is not None,
                }
            )
    return relations
