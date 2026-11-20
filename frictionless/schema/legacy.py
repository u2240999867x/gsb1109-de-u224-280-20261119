"""Exporting v2 descriptors and validated data to legacy (Data Package v1) readers.

A v1 repository only understands plain string `missingValues` entries: the v2
object form `{value, label?}` cannot be written into a v1 descriptor, and the
label is the *reason* of a missing value (e.g. "not measured" vs "downtime"),
which must never be rewritten into another reason — the two feed different
downstream statistics.

The export is therefore split in two lossless pieces:

- the plain string tokens stay in `missingValues`, so a v1 reader keeps
  distinguishing real values from missing ones exactly as the v2 descriptor
  does (a cell is missing iff its string is one of the tokens);
- the labels move to a custom side-car property
  (`missing_values.LEGACY_LABELS_KEY`) that a v1 reader ignores, keeping the
  reason recoverable per token — and therefore per source row.

A configured legacy token set is accepted only when it recognizes exactly the
same cells as the v2 descriptor. Any extra token (a real value would become
missing) or dropped token (a documented missing value would become real) is a
conflict: the export then raises with the full list of conflicts and delivers
no descriptor, instead of silently changing the true/missing judgments.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

import attrs

from .. import errors
from ..exception import FrictionlessException
from . import missing_values as missing_values_module
from .field import Field
from .schema import Schema

LEGACY_LABELS_KEY = missing_values_module.LEGACY_LABELS_KEY


@attrs.define(kw_only=True, repr=False)
class LegacyRejection:
    """A required field whose input was a documented missing value.

    Kept separately from the deliverable data: the row is not silently
    dropped, and the original missing reason (label, when one was declared)
    stays attached so the rejection can be traced back to the source row.
    """

    row_number: int
    field_name: str
    field_number: int
    cell: str
    reason: Optional[str] = None
    note: str = 'constraint "required" is "True"'

    def to_descriptor(self) -> Dict[str, Any]:
        descriptor: Dict[str, Any] = {
            "rowNumber": self.row_number,
            "fieldName": self.field_name,
            "fieldNumber": self.field_number,
            "cell": self.cell,
            "note": self.note,
        }
        if self.reason is not None:
            descriptor["reason"] = self.reason
        return descriptor


@attrs.define(kw_only=True, repr=False)
class LegacyMissingCell:
    """One missing input as written into a legacy row, with its reason.

    The reason comes from the v2 label when one was declared; it is `None`
    for a missing token without a label. This is what makes "not measured"
    and "downtime" separately traceable back to each source row.
    """

    field_name: str
    field_number: int
    cell: str
    reason: Optional[str] = None

    def to_descriptor(self) -> Dict[str, Any]:
        descriptor: Dict[str, Any] = {
            "fieldName": self.field_name,
            "fieldNumber": self.field_number,
            "cell": self.cell,
        }
        if self.reason is not None:
            descriptor["reason"] = self.reason
        return descriptor


@attrs.define(kw_only=True, repr=False)
class LegacyRow:
    """One source row rendered for a v1 reader.

    `cells` are plain strings: missing inputs are written back as their
    declared token (which the v1 descriptor lists in `missingValues`), real
    values are written with the field writers. `missing_cells` records every
    missing input with its reason, and `rejections` the required-field ones.
    """

    row_number: int
    cells: List[str]
    missing_cells: List[LegacyMissingCell] = attrs.field(factory=list)
    rejections: List[LegacyRejection] = attrs.field(factory=list)

    def to_descriptor(self) -> Dict[str, Any]:
        return {
            "rowNumber": self.row_number,
            "cells": list(self.cells),
            "missingCells": [cell.to_descriptor() for cell in self.missing_cells],
            "rejections": [rejection.to_descriptor() for rejection in self.rejections],
        }


@attrs.define(kw_only=True, repr=False)
class LegacyExporter:
    """Export a validated v2 schema and its rows for a legacy v1 reader.

    Parameters:
        schema: the v2 schema (labels preserved on its `_missing_values_labels`)
        missing_values: optional legacy token override for the schema scope
        field_missing_values: optional legacy token overrides keyed by field
            name; only fields declaring their own `missingValues` can be
            overridden

    The exporter raises `FrictionlessException` (with one reason per conflict)
    when a configured token set cannot preserve the true/missing judgments; in
    that case neither a descriptor nor data is delivered.
    """

    schema: Schema
    missing_values: Optional[List[str]] = None
    field_missing_values: Optional[Dict[str, List[str]]] = None

    _conflicts: Optional[List[str]] = attrs.field(default=None, init=False)
    _readers: Dict[str, Any] = attrs.field(factory=dict, init=False)

    def __attrs_post_init__(self):
        self._readers = self.schema.create_cell_readers()

    # Descriptor

    def to_descriptor(self) -> Dict[str, Any]:
        """Build the v1 descriptor, or raise on a lossless-export conflict."""
        descriptor, conflicts = self._build_descriptor()
        self._raise_if_conflicting(conflicts)
        return descriptor

    def _ensure_compatible(self) -> None:
        if self._conflicts is None:
            self._build_descriptor()
        self._raise_if_conflicting(self._conflicts or [])

    def _raise_if_conflicting(self, conflicts: List[str]) -> None:
        if not conflicts:
            return
        error = errors.SchemaError(
            note="legacy export cannot preserve the missing values "
            "of the source descriptor"
        )
        reasons = [errors.SchemaError(note=note) for note in conflicts]
        raise FrictionlessException(error, reasons=reasons)

    def _build_descriptor(self) -> Tuple[Dict[str, Any], List[str]]:
        conflicts: List[str] = []
        descriptor = self.schema.to_descriptor()
        # A v1 reader does not know the v2 profile; an absent `$schema` is
        # itself an explicit v1, so the root simply does not declare one.
        descriptor.pop("$schema", None)

        conflicts.extend(self._schema_scope_conflicts(descriptor))
        conflicts.extend(self._field_scope_conflicts(descriptor))

        self._conflicts = conflicts
        return descriptor, conflicts

    def _schema_scope_conflicts(self, descriptor: Dict[str, Any]) -> List[str]:
        conflicts: List[str] = []
        if "missingValues" not in descriptor:
            if self.missing_values is not None:
                conflicts.append(
                    'schema: no "missingValues" are declared at the schema scope'
                )
            return conflicts
        canonical = self.schema.missing_values
        labels = getattr(self.schema, "_missing_values_labels", {})
        configured = (
            list(canonical) if self.missing_values is None else self.missing_values
        )
        conflicts.extend(
            missing_values_module.legacy_conflict_notes("schema", canonical, configured)
        )
        descriptor["missingValues"] = list(configured)
        sidecar = missing_values_module.legacy_sidecar(canonical, labels)
        if sidecar:
            descriptor[LEGACY_LABELS_KEY] = sidecar
        return conflicts

    def _field_scope_conflicts(self, descriptor: Dict[str, Any]) -> List[str]:
        conflicts: List[str] = []
        overrides = self.field_missing_values or {}
        for field in self.schema.fields:
            field_descriptor = self._field_descriptor(descriptor, field.name)
            if field_descriptor is None:
                continue
            if not field.has_defined("missing_values"):
                # inherits the schema scope: nothing field-scoped to export
                if field.name in overrides:
                    conflicts.append(
                        f'field "{field.name}": it inherits the schema '
                        '"missingValues" and cannot be overridden separately'
                    )
                continue
            if "missingValues" not in field_descriptor:
                if field.name in overrides:
                    conflicts.append(
                        f'field "{field.name}": no "missingValues" are declared '
                        "at the field scope"
                    )
                continue
            canonical = field.missing_values
            labels = getattr(field, "_missing_values_labels", {})
            configured = overrides.get(field.name)
            if configured is None:
                configured = list(canonical)
            conflicts.extend(
                missing_values_module.legacy_conflict_notes(
                    f'field "{field.name}"', canonical, configured
                )
            )
            field_descriptor["missingValues"] = list(configured)
            sidecar = missing_values_module.legacy_sidecar(canonical, labels)
            if sidecar:
                field_descriptor[LEGACY_LABELS_KEY] = sidecar
        return conflicts

    def _field_descriptor(
        self, descriptor: Dict[str, Any], name: str
    ) -> Optional[Dict[str, Any]]:
        for field_descriptor in descriptor.get("fields", []):
            if field_descriptor.get("name") == name:
                return field_descriptor
        return None

    # Data

    def write_row(self, cells: List[Any], *, row_number: int) -> LegacyRow:
        """Write one validated source row for the v1 reader.

        Missing inputs are rendered as their declared token (so the v1 reader
        recognizes them as missing) and recorded with their reason; a missing
        input on a required field additionally adds a traceable rejection.
        """
        self._ensure_compatible()
        legacy_cells: List[str] = []
        missing_cells: List[LegacyMissingCell] = []
        rejections: List[LegacyRejection] = []
        for field_number, field in enumerate(self.schema.fields, start=1):
            source = cells[field_number - 1] if len(cells) >= field_number else None
            value, notes = self._readers[field.name](source)
            type_note = notes.pop("type", None) if notes else None
            if value is None and not type_note:
                token = "" if source is None else str(source)
                legacy_cells.append(token)
                reason = self.reason_for(field, token)
                missing_cells.append(
                    LegacyMissingCell(
                        field_name=field.name,
                        field_number=field_number,
                        cell=token,
                        reason=reason,
                    )
                )
                if notes and "required" in notes:
                    rejections.append(
                        LegacyRejection(
                            row_number=row_number,
                            field_name=field.name,
                            field_number=field_number,
                            cell=token,
                            reason=reason,
                            note=notes["required"],
                        )
                    )
                continue
            written, _ = field.write_cell(value, ignore_missing=True)
            if written is None:
                # a value the field writer cannot render (e.g. a type error):
                # pass the original string through rather than inventing data
                written = "" if source is None else str(source)
            legacy_cells.append(written)
        return LegacyRow(
            row_number=row_number,
            cells=legacy_cells,
            missing_cells=missing_cells,
            rejections=rejections,
        )

    def write_rows(
        self, rows: List[List[Any]], *, start_row_number: int = 1
    ) -> List[LegacyRow]:
        """Write several validated source rows (numbered from `start_row_number`)"""
        return [
            self.write_row(cells, row_number=row_number)
            for row_number, cells in enumerate(rows, start=start_row_number)
        ]

    # Reasons

    def reason_for(self, field: Field, token: str) -> Optional[str]:
        """The missing reason (v2 label) of a token for a field.

        Looks at the labels governing the field (schema scope when inherited),
        then at the side-car carried by a reopened legacy descriptor.
        """
        _, labels = self._effective_missing(field)
        if token in labels:
            return labels[token]
        sidecar = field.custom.get(LEGACY_LABELS_KEY)
        if isinstance(sidecar, dict) and token in sidecar:
            return sidecar[token]
        if field.schema is not None:
            schema_sidecar = field.schema.custom.get(LEGACY_LABELS_KEY)
            if isinstance(schema_sidecar, dict) and token in schema_sidecar:
                return schema_sidecar[token]
        return None

    def _effective_missing(self, field: Field) -> Tuple[List[str], Dict[str, str]]:
        """The tokens and labels actually governing a field's reads.

        Mirrors `Field.create_cell_reader`: a field without its own
        `missingValues` inherits the schema scope, labels included.
        """
        if field.has_defined("missing_values") or not field.schema:
            return field.missing_values, getattr(field, "_missing_values_labels", {})
        return field.schema.missing_values, getattr(
            field.schema, "_missing_values_labels", {}
        )
