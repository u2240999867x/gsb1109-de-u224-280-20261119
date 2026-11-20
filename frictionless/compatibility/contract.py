from __future__ import annotations

import csv
import hashlib
import json
import os
from copy import deepcopy
from typing import Any, Dict, List, Optional, Set, Tuple, Union

import attrs

from .. import errors, helpers, types
from ..exception import FrictionlessException
from ..metadata import Metadata
from ..package import Package
from ..platform import platform


# A candidate record value that is not present in the original record. The
# distinction between "absent" and "null" is part of the evidence: a missing
# new column is not the same input as an explicitly blank cell.
ABSENT = "<absent>"

DIRECTIONS = [
    "old-write/new-read",
    "new-write/old-read",
    "old-write/old-read",
    "new-write/new-read",
]

DIRECTION_ALIASES = {
    "old->new": "old-write/new-read",
    "old-write-new-read": "old-write/new-read",
    "old_write_new_read": "old-write/new-read",
    "new->old": "new-write/old-read",
    "new-write-old-read": "new-write/old-read",
    "new_write_old_read": "new-write/old-read",
    "old->old": "old-write/old-read",
    "new->new": "new-write/new-read",
}


@attrs.define(kw_only=True, repr=False)
class CandidateRecord:
    """A raw record taken verbatim from the publisher's input.

    `values` keeps the original cells (usually strings) and `explicit_fields`
    tells which fields were actually present, so that an omitted new column
    can be reported as absent rather than silently rendered as null.
    """

    id: str
    table: str
    values: Dict[str, Any]
    explicit_fields: Set[str]

    def to_descriptor(self) -> types.IDescriptor:
        return {
            "id": self.id,
            "table": self.table,
            "values": {key: _jsonable(value) for key, value in self.values.items()},
            "present": sorted(self.explicit_fields),
        }


@attrs.define(kw_only=True, repr=False)
class PackageInfo:
    """One side of the audit (the old or the new package)."""

    name: str
    source: Any
    descriptor: types.IDescriptor
    basepath: Optional[str] = None
    package: Optional[Package] = None
    errors: List[types.IDescriptor] = attrs.field(factory=list)

    @property
    def digest(self) -> str:
        return descriptor_digest(self.descriptor)


class CompatibilityContract:
    """A read/write compatibility contract.

    The contract points at the old and new package descriptors and supplies
    the publisher's candidate records, value domains, custom constraints, and
    the read/write version combinations that are allowed to be used.
    """

    def __init__(self, descriptor: types.IDescriptor, *, basepath: Optional[str] = None):
        self.descriptor = deepcopy(descriptor)
        self.basepath = basepath
        self.name = self.descriptor.get("name") or "compatibility"
        self.old = self._load_side("old")
        self.new = self._load_side("new")
        self.directions = self._load_directions()
        self.records = self._load_records()
        self.domains = self.descriptor.get("domains") or {}
        self.custom_constraints = self._load_custom_constraints()
        self.relationships = self._load_relationships()

    @classmethod
    def from_descriptor(
        cls, source: Union[types.IDescriptor, str]
    ) -> "CompatibilityContract":
        basepath = None
        if isinstance(source, str):
            basepath = os.path.dirname(source) or None
        try:
            descriptor = Metadata.metadata_retrieve(source)
        except FrictionlessException as exception:
            note = f"cannot read the compatibility contract: {exception.error.note}"
            raise FrictionlessException(
                errors.CompatibilityError(note=note)
            ) from exception
        contract = cls(descriptor, basepath=basepath)
        contract._validate()
        return contract

    # Load

    def _load_side(self, name: str) -> PackageInfo:
        source = self.descriptor.get(name)
        if source is None:
            raise FrictionlessException(
                errors.CompatibilityError(note=f'contract requires "{name}"')
            )

        if isinstance(source, dict) and "package" in source:
            source = source["package"]

        if isinstance(source, str):
            path = helpers.join_basepath(source, self.basepath)
            descriptor = Package.metadata_retrieve(path)
            info = PackageInfo(
                name=name,
                source=source,
                descriptor=descriptor,
                basepath=os.path.dirname(path) or None,
            )
        else:
            descriptor = deepcopy(source)
            info = PackageInfo(
                name=name,
                source="<inline>",
                descriptor=descriptor,
                basepath=self.basepath,
            )

        try:
            info.package = Package.from_descriptor(
                _with_placeholder_data(descriptor), basepath=info.basepath
            )
        except FrictionlessException as exception:
            info.errors = [error.to_descriptor() for error in exception.to_errors()]
        return info

    def _load_directions(self) -> List[str]:
        declared = self.descriptor.get("directions")
        if declared is None:
            declared = self.descriptor.get("allowedDirections")
        if declared is None:
            declared = ["old-write/new-read", "new-write/old-read"]
        if isinstance(declared, str):
            declared = [declared]

        directions: List[str] = []
        for value in declared:
            direction = DIRECTION_ALIASES.get(value, value)
            if direction not in DIRECTIONS:
                note = (
                    f'unsupported direction "{value}" '
                    f"(expected one of {', '.join(DIRECTIONS)})"
                )
                raise FrictionlessException(errors.CompatibilityError(note=note))
            if direction not in directions:
                directions.append(direction)
        return directions

    def _load_records(self) -> Dict[str, List[CandidateRecord]]:
        source = self.descriptor.get("records")
        if not isinstance(source, dict):
            raise FrictionlessException(
                errors.CompatibilityError(
                    note='contract requires "records" as an object keyed by table'
                )
            )

        records: Dict[str, List[CandidateRecord]] = {}
        for table, value in source.items():
            entries = self._load_record_entries(table, value)
            records[table] = entries
        return records

    def _load_record_entries(
        self, table: str, source: Any
    ) -> List[CandidateRecord]:
        if isinstance(source, dict):
            if "path" in source:
                source = self._read_records_path(source)
            elif "records" in source:
                source = source["records"]

        if not isinstance(source, list):
            note = f'records for table "{table}" must be an array or a path'
            raise FrictionlessException(errors.CompatibilityError(note=note))

        entries: List[CandidateRecord] = []
        for index, item in enumerate(source, start=1):
            if not isinstance(item, dict):
                note = f'record {index} for table "{table}" is not an object'
                raise FrictionlessException(errors.CompatibilityError(note=note))
            values = item.get("values", item)
            if not isinstance(values, dict):
                note = f'record {index} for table "{table}" has no value object'
                raise FrictionlessException(errors.CompatibilityError(note=note))
            values = {
                key: value
                for key, value in values.items()
                if key not in ("id", "table", "values")
            }
            record_id = item.get("id") or f"{table}-{index}"
            entries.append(
                CandidateRecord(
                    id=str(record_id),
                    table=table,
                    values=values,
                    explicit_fields=set(values),
                )
            )
        return entries

    def _read_records_path(self, source: types.IDescriptor) -> List[types.IDescriptor]:
        path = helpers.join_basepath(source["path"], self.basepath)
        format = source.get("format") or os.path.splitext(path)[1][1:].lower()
        if format == "csv":
            with open(path, encoding="utf-8", newline="") as file:
                return list(csv.DictReader(file))
        text = helpers.read_file(path)
        if format in ("yaml", "yml"):
            data = platform.yaml.safe_load(text)
        else:
            data = json.loads(text)
        if isinstance(data, dict) and "records" in data:
            data = data["records"]
        assert isinstance(data, list)
        return data

    def _load_custom_constraints(self) -> List[types.IDescriptor]:
        source = self.descriptor.get("customConstraints") or []
        if isinstance(source, dict):
            result: List[types.IDescriptor] = []
            for table, fields in source.items():
                if not isinstance(fields, dict):
                    continue
                for field, constraints in fields.items():
                    items = (
                        constraints
                        if isinstance(constraints, list)
                        else [constraints]
                    )
                    for item in items:
                        descriptor = deepcopy(item) if isinstance(item, dict) else {}
                        descriptor.setdefault("table", table)
                        descriptor.setdefault("field", field)
                        result.append(descriptor)
            return result
        assert isinstance(source, list)
        return [deepcopy(item) for item in source]

    def _load_relationships(self) -> List[types.IDescriptor]:
        declared = self.descriptor.get("relationships")
        if declared:
            return [deepcopy(item) for item in declared]

        # Derive the relations from both descriptor trees, without mutating
        # either package descriptor.
        relationships: List[types.IDescriptor] = []
        seen: Set[Tuple[Any, ...]] = set()
        for info in (self.old, self.new):
            if not info.package:
                continue
            for resource in info.package.resources:
                schema = getattr(resource, "schema", None)
                if not schema:
                    continue
                for fk in schema.foreign_keys:
                    reference = fk.get("reference", {})
                    target = reference.get("resource") or resource.name
                    item = {
                        "name": f'{resource.name}.{",".join(fk["fields"])}'
                        f'->{target}.{",".join(reference.get("fields", []))}',
                        "source": resource.name,
                        "sourceFields": list(fk.get("fields", [])),
                        "target": target,
                        "targetFields": list(reference.get("fields", [])),
                    }
                    key = (
                        item["source"],
                        tuple(item["sourceFields"]),
                        item["target"],
                        tuple(item["targetFields"]),
                    )
                    if key not in seen:
                        seen.add(key)
                        relationships.append(item)
        return relationships

    def _validate(self) -> None:
        if not self.old.package and not self.old.errors:
            note = "old package could not be loaded"
            raise FrictionlessException(errors.CompatibilityError(note=note))
        if not self.new.package and not self.new.errors:
            note = "new package could not be loaded"
            raise FrictionlessException(errors.CompatibilityError(note=note))
        if not self.records:
            raise FrictionlessException(
                errors.CompatibilityError(note='contract requires candidate "records"')
            )

    # Helpers

    def records_digest(self) -> str:
        descriptor = {
            table: [record.to_descriptor() for record in records]
            for table, records in self.records.items()
        }
        return descriptor_digest(descriptor)

    def inputs_descriptor(self) -> types.IDescriptor:
        return {
            "old": {
                "source": self.old.source,
                "digest": self.old.digest,
                "errors": self.old.errors,
            },
            "new": {
                "source": self.new.source,
                "digest": self.new.digest,
                "errors": self.new.errors,
            },
            "records": {"digest": self.records_digest()},
        }


def _with_placeholder_data(descriptor: types.IDescriptor) -> types.IDescriptor:
    """Make schema-only resources importable.

    The audit only uses the schemas and foreign keys; the package's own data
    is never read. A package descriptor whose resources carry only a schema
    would not import as a data resource, so an empty inline source is added
    on the copy used for import (the contract's descriptor and its digest are
    left untouched).
    """
    descriptor = deepcopy(descriptor)
    for resource in descriptor.get("resources", []):
        if isinstance(resource, dict):
            if "path" not in resource and "data" not in resource:
                resource["data"] = []
    return descriptor


def descriptor_digest(descriptor: Any) -> str:
    text = json.dumps(
        _jsonable(descriptor), sort_keys=True, ensure_ascii=False, default=str
    )
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    return str(value)
