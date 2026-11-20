from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Set, Tuple

import attrs

from ... import errors
from ...checklist import Check
from ...platform import platform

if TYPE_CHECKING:
    from ...resource import Resource
    from ...table import Row


@attrs.define(kw_only=True, repr=False)
class polymorphic_foreign_key(Check):
    """Check a polymorphic foreign key

    This check supports a foreign key whose target table depends on a
    category (kind) label carried by another column of the same row, for
    example work orders referencing either an equipment table or a location
    table depending on the value of an "object_kind" column.

    The mapping of kind labels to target tables is an explicit business
    contract given in the `variants` parameter; the target table is never
    guessed. Every row is looked up only in the table declared for its own
    kind, and the composite key is normalized with the field types declared
    by that target table, so a key normalized for one table is never applied
    to another table.

    This check can be enabled using the `checks` parameter
    for the `validate` function.
    """

    type = "polymorphic-foreign-key"
    Errors = [
        errors.PolymorphicForeignKeyKindError,
        errors.PolymorphicForeignKeyResourceError,
        errors.PolymorphicForeignKeyLookupError,
        errors.ForeignKeyError,
    ]

    kind_field: str
    """
    Name of the field containing the kind label that selects the target
    table for each row.
    """

    fields: List[str]
    """
    Names of the fields composing the foreign key in the checked table.
    """

    variants: Dict[str, Dict[str, Any]]
    """
    Mapping of kind labels to the target table contract, for example
    `{"equipment": {"resource": "equipment", "fields": ["site", "eq_id"]}}`.
    An empty resource name references the checked table itself.
    """

    # Connect

    def connect(self, resource: Resource):
        super().connect(resource)
        self.__applicable = False
        self.__states: Dict[Any, Dict[str, Any]] = {}

    # Validate

    def validate_start(self):
        if not isinstance(self.resource, platform.frictionless_resources.TableResource):
            return
        if not self.fields:
            yield errors.CheckError(note='polymorphic foreign key requires "fields"')
            return
        for kind, variant in self.variants.items():
            resource_name = variant.get("resource")
            reference_fields = variant.get("fields")
            if not isinstance(resource_name, str) or not isinstance(
                reference_fields, list
            ):
                yield errors.CheckError(
                    note='polymorphic foreign key variant "%s" requires '
                    '"resource" and "fields"' % kind
                )
                return
            if len(reference_fields) != len(self.fields):
                yield errors.CheckError(
                    note='polymorphic foreign key variant "%s" declares %s '
                    "target fields for %s key fields"
                    % (kind, len(reference_fields), len(self.fields))
                )
                return
        field_names = self.resource.schema.field_names
        if self.kind_field not in field_names:
            # The check only applies to tables carrying the kind field
            return
        missing = [name for name in self.fields if name not in field_names]
        if missing:
            yield errors.CheckError(
                note='polymorphic foreign key fields are not in the table: "%s"'
                % ", ".join(missing)
            )
            return
        self.__applicable = True

    def validate_row(self, row: Row):
        if not self.__applicable:
            return

        # Kind
        kind = row.get(self.kind_field)
        try:
            variant = self.variants.get(kind)  # type: ignore
        except TypeError:
            variant = None
        if variant is None:
            note = 'kind "%s" is not declared in the check contract' % kind
            yield errors.PolymorphicForeignKeyKindError.from_row(
                row, note=note, kind=kind
            )
            return

        # State
        state = self.__states.get(kind)
        if state is None:
            state = self.__states[kind] = self.__resolve(kind, variant)
        if state["status"] == "missing":
            yield errors.PolymorphicForeignKeyResourceError.from_row(
                row,
                note=state["note"],
                kind=kind,
                reference_name=state["referenceName"],
            )
            return
        if state["status"] == "unreadable":
            yield errors.PolymorphicForeignKeyLookupError.from_row(
                row,
                note=state["note"],
                kind=kind,
                reference_name=state["referenceName"],
            )
            return

        # Match
        if all(row.get(name) is None for name in self.fields):
            return
        cells_by_name = dict(zip(row.field_names, row.cells))
        cells = tuple(
            reader(cells_by_name.get(name))[0]
            for reader, name in zip(state["readers"], self.fields)
        )
        if cells not in state["lookup"]:
            note = 'for "%s": values "%s" not found in the lookup table "%s" as "%s"' % (
                ", ".join(self.fields),
                ", ".join(str(cell) for cell in cells),
                state["referenceName"],
                ", ".join(state["referenceFields"]),
            )
            yield errors.ForeignKeyError.from_row(
                row,
                note=note,
                field_names=list(self.fields),
                field_values=list(cells),
                reference_name=state["referenceName"],
                reference_field_names=state["referenceFields"],
            )

    # Resolve

    def __resolve(self, kind: Any, variant: Dict[str, Any]) -> Dict[str, Any]:
        resource_name = variant["resource"]
        reference_fields = list(variant["fields"])
        state: Dict[str, Any] = {
            "referenceName": resource_name,
            "referenceFields": reference_fields,
        }

        # Prepare source
        package = self.resource.package
        if not resource_name:
            source = self.resource.to_copy()
        else:
            if package is None:
                state["status"] = "missing"
                state["note"] = (
                    'package is required to reach the lookup table "%s" '
                    'declared for kind "%s"' % (resource_name, kind)
                )
                return state
            if not package.has_resource(resource_name):
                state["status"] = "missing"
                state["note"] = (
                    'the lookup table "%s" declared for kind "%s" is not '
                    "present in the package" % (resource_name, kind)
                )
                return state
            source = package.get_resource(resource_name).to_copy()

        # Build lookup
        # A failure here only affects the rows of this kind; the lookups of
        # the other kinds are resolved independently
        try:
            if source.schema:
                source.schema.foreign_keys = []
            lookup: Set[Tuple[Any, ...]] = set()
            readers = []
            with source:
                for name in reference_fields:
                    field = source.schema.get_field(name)
                    readers.append(field.create_cell_reader())
                for source_row in source.row_stream:  # type: ignore
                    cells = tuple(source_row.get(name) for name in reference_fields)  # type: ignore
                    if set(cells) == {None}:  # type: ignore
                        continue
                    lookup.add(cells)
        except Exception as exc:
            state["status"] = "unreadable"
            state["note"] = (
                'the lookup table "%s" declared for kind "%s" could not be '
                "read: %s" % (resource_name, kind, exc)
            )
            return state
        state["status"] = "ready"
        state["lookup"] = lookup
        state["readers"] = readers
        return state

    # Metadata

    metadata_profile_patch = {
        "required": ["kindField", "fields", "variants"],
        "properties": {
            "kindField": {"type": "string"},
            "fields": {"type": "array", "items": {"type": "string"}},
            "variants": {"type": "object"},
        },
    }
