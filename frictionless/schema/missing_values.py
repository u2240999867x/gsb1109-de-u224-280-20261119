"""Helpers for the `missingValues` property, shared by Schema and Field.

Datapackage v2 allows object entries `{value, label?}` alongside plain
strings. The canonical in-memory model keeps `missing_values` as a list of
strings plus a side-car mapping of labels: the split (import) is shape-driven,
never version-driven, and the export emits object entries if and only if at
least one current value has a label.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, Dict, List, Tuple, Union

if TYPE_CHECKING:
    from ..types import IResolvedStandards

IEntries = List[Union[str, Dict[str, str]]]

PROFILE: Dict[str, Any] = {
    "anyOf": [
        {
            "type": "array",
            "items": {"type": "string"},
        },
        {
            "type": "array",
            "items": {
                "type": "object",
                "required": ["value"],
                "properties": {
                    "value": {"type": "string"},
                    "label": {"type": "string"},
                },
            },
        },
    ],
}


def split(entries: IEntries) -> Tuple[List[str], Dict[str, str]]:
    """Split entries into plain string values and a label mapping"""
    labels = {
        entry["value"]: entry["label"]
        for entry in entries
        if isinstance(entry, dict) and entry.get("label") is not None
    }
    values = [entry["value"] if isinstance(entry, dict) else entry for entry in entries]
    return values, labels


def validation_notes(entries: IEntries) -> List[str]:
    """Uniqueness notes for object entries (empty if valid)

    Object entries must have a unique value and an optional unique label;
    absent labels do not collide with each other. Plain string entries keep
    the lax v1 behavior (duplicates allowed).
    """
    notes: List[str] = []
    if not any(isinstance(entry, dict) for entry in entries):
        return notes
    values = [entry["value"] if isinstance(entry, dict) else entry for entry in entries]
    for value in _duplicates(values):
        notes.append(f'missing value "{value}" is not unique')
    labels = [
        entry["label"]
        for entry in entries
        if isinstance(entry, dict) and entry.get("label") is not None
    ]
    for label in _duplicates(labels):
        notes.append(f'missing value label "{label}" is not unique')
    return notes


def version_gate_notes(entries: IEntries, version: IResolvedStandards) -> List[str]:
    """Notes when object entries appear under a v1 schema

    The object form `{value, label?}` is a datapackage v2 addition, rejected
    when the resolved version is unambiguously v1 (an "unknown" version,
    from a custom profile, accepts the superset).
    """
    if version != "v1":
        return []
    if not any(isinstance(entry, dict) for entry in entries):
        return []
    return ["missing values in object form require datapackage v2"]


def export(
    values: List[str], labels: Dict[str, str]
) -> Union[List[str], List[Dict[str, str]]]:
    """Render the canonical descriptor form (objects iff at least one label)"""
    if not any(value in labels for value in values):
        return values
    return [
        (
            {"value": value, "label": labels[value]}
            if value in labels
            else {"value": value}
        )
        for value in values
    ]


# Legacy (Data Package v1) export
#
# A v1 descriptor only accepts plain string `missingValues` entries: the
# object form `{value, label?}` is a v2 addition. A legacy export therefore
# keeps the plain string tokens (which carry the missing/real distinction)
# and moves the labels — the missing *reasons* a v1 reader cannot store — to
# a side-car custom property a v1 repository simply ignores.
#
# The two pieces are deliberately split, never merged into the tokens
# themselves: renaming a token to encode its reason (e.g. "9999" ->
# "NOT_MEASURED") would change which source cells are recognized as missing,
# and collapsing two labeled tokens into one would conflate two reasons.

LEGACY_LABELS_KEY = "x-missingValueLabels"
"""Custom descriptor property carrying the `value -> label` (reason) side-car
of a legacy export, at schema level and (for field-scoped entries) on the
field itself. Unknown to v1 readers, which ignore custom properties."""


def legacy_sidecar(values: List[str], labels: Dict[str, str]) -> Dict[str, str]:
    """Render the reason side-car of a legacy export (empty if no labels)"""
    return {value: labels[value] for value in values if value in labels}


def legacy_conflict_notes(
    scope: str, canonical: List[str], configured: List[str]
) -> List[str]:
    """Notes when a configured legacy token set changes the missing judgments

    A legacy export is lossless only when its plain string tokens recognize
    exactly the same cells as the v2 descriptor: every extra token would turn
    a currently valid value into a missing one, every dropped token would
    turn a documented missing value into a real one. Returns one note per
    diverging token (an empty list means the configuration is lossless).
    """
    notes: List[str] = []
    canonical_set = set(canonical)
    configured_set = set(configured)
    for value in sorted(configured_set - canonical_set):
        notes.append(
            f'{scope}: legacy missing value "{value}" is not a missing value '
            "in the source descriptor and would turn a valid value into missing"
        )
    for value in sorted(canonical_set - configured_set):
        notes.append(
            f'{scope}: missing value "{value}" would be dropped from the legacy '
            "descriptor and turn a documented missing value into a real one"
        )
    return notes


def _duplicates(values: List[str]) -> List[str]:
    """Values appearing more than once, each reported once in order"""
    seen: set[str] = set()
    duplicates: List[str] = []
    for value in values:
        if value in seen and value not in duplicates:
            duplicates.append(value)
        seen.add(value)
    return duplicates
