from __future__ import annotations

from typing import Any, Dict, List, Optional

import attrs

from .. import types


@attrs.define(kw_only=True, repr=False)
class RelocationMapping:
    """Relationship between a source resource and its published counterpart"""

    source: str
    """Absolute path to the descriptor of the source package"""

    resource: str
    """Name of the resource in the source package"""

    target: str
    """Name of the resource in the target package"""

    role: str
    """Either "selected" or "dependency" """

    source_path: str
    """Absolute path to the source data file"""

    target_path: str
    """Path of the data file relative to the target package root"""

    identity_rule: str
    """Rule used to derive the target name"""

    material: str
    """Either "copied" or "reused" (an identical source file shared by the
    resources)"""

    schema: str
    """Either "inline" or "relative" (a standalone schema descriptor)"""

    schema_source: Optional[str] = None
    """Absolute path of the source schema descriptor, if any"""

    schema_target: Optional[str] = None
    """Path of the target schema descriptor relative to the package root"""

    foreign_keys: List[Dict[str, Any]] = attrs.field(factory=list)
    """Foreign keys as published, with the references resolved in the target
    package (an empty "resource" denotes a self-reference)"""

    properties: Dict[str, Any] = attrs.field(factory=dict)
    """Business properties carried over from the source descriptor"""

    data_sha256: Optional[str] = None
    """SHA-256 of the source data file (evidence that the material is the
    same one, whether it was copied or reused)"""

    validation: str = "package"
    """How the target resource is validated: as part of the target package,
    so its foreign keys keep the source package's relationships"""

    def to_descriptor(self) -> types.IDescriptor:
        descriptor: Dict[str, Any] = {
            "source": self.source,
            "resource": self.resource,
            "target": self.target,
            "role": self.role,
            "sourcePath": self.source_path,
            "targetPath": self.target_path,
            "identityRule": self.identity_rule,
            "material": self.material,
            "schema": self.schema,
            "foreignKeys": self.foreign_keys,
            "properties": self.properties,
            "dataSha256": self.data_sha256,
            "validation": self.validation,
        }
        if self.schema_source:
            descriptor["schemaSource"] = self.schema_source
        if self.schema_target:
            descriptor["schemaTarget"] = self.schema_target
        return descriptor


@attrs.define(kw_only=True, repr=False)
class RelocationResult:
    """Outcome of a package relocation"""

    package: Any
    """The published `Package` instance (opened from the new location)"""

    root: str
    """Absolute path to the published package directory"""

    descriptor_path: str
    """Absolute path to the published package descriptor"""

    manifest_path: str
    """Absolute path to the manifest recording the source/target mapping"""

    manifest: Dict[str, Any]
    """Content of the manifest"""

    report: Any
    """Validation report of the published package"""

    @property
    def valid(self) -> bool:
        """Whether the published package passed validation (when requested)"""
        return bool(self.report is not None and self.report.valid)

    def to_descriptor(self) -> types.IDescriptor:
        return self.manifest


# Backward-compatible interface-style aliases
IRelocationMapping = RelocationMapping
IRelocationResult = RelocationResult
Relocation = RelocationResult
