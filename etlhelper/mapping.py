"""
Column mapping support for loading records whose source field names differ
from the columns of the target table.

A mapping maps target column names to the source field names that supply
their values, e.g.::

    {"ReportCode": "code", "Station": "station", "Quality": "quality"}

Rows produced by different collector versions can use different source
field names.  When a target column is reachable through more than one
source field, a ``version_field`` identifies which source field is
authoritative for each record: the value of that field selects the source
field (or group of source fields) declared by the current record.
"""
from __future__ import annotations

from collections.abc import Collection, Mapping
from typing import Any, Optional

from etlhelper.exceptions import ETLHelperMappingConflictError

# A single source field, or several source fields keyed by the value of the
# version field declared on the record.  A collection of source fields for a
# single version is ambiguous and rejected by validate_column_mapping.
ColumnMappingValue = str | Mapping[Any, str | Collection[str]]
ColumnMapping = Mapping[str, ColumnMappingValue]


def validate_column_mapping(
        mapping: ColumnMapping,
        version_field: Optional[str] = None
        ) -> None:
    """
    Validate a column mapping before it is used to write data.

    A conflict exists when two different source fields would assign the
    same target column and there is no explicit way to choose between
    them.  This happens when a target column lists several source fields
    but no ``version_field`` is configured, or when a version-specific
    group contains more than one source field for a single version.

    :param mapping: target column to source field(s) mapping
    :param version_field: source field whose value selects between
                          version-specific source fields
    :raises ETLHelperMappingConflictError: if the mapping is ambiguous
    """
    for target, sources in mapping.items():
        if isinstance(sources, str):
            continue

        if not version_field:
            fields: set[str] = set()
            for value in sources.values():
                if isinstance(value, Collection) and not isinstance(value, str):
                    fields.update(value)
                else:
                    fields.add(value)
            msg = (f"Target column '{target}' is mapped from multiple source "
                   f"fields {sorted(fields)} but no version_field was provided "
                   "to choose between them.")
            raise ETLHelperMappingConflictError(msg)

        for version, source in sources.items():
            if isinstance(source, Collection) and not isinstance(source, str):
                fields = sorted(set(source))
                if len(fields) > 1:
                    msg = (f"Target column '{target}' has multiple source "
                           f"fields {fields} for version {version!r}; no "
                           "explicit choice is possible.")
                    raise ETLHelperMappingConflictError(msg)


def resolve_source_field(
        sources: ColumnMappingValue,
        row: Mapping[str, Any],
        version_field: Optional[str]
        ) -> Optional[str]:
    """
    Return the source field that actually supplies a target column for a
    single record, or ``None`` if the record does not declare one.

    :param sources: source field or version-specific source field group
    :param row: the record being written
    :param version_field: source field whose value selects the version
    :return: the chosen source field name, or ``None``
    """
    if isinstance(sources, str):
        return sources

    if not version_field or version_field not in row:
        return None

    selected = sources.get(row[version_field])
    if isinstance(selected, Collection) and not isinstance(selected, str):
        # A single-element collection survives validation; use its element
        return next(iter(selected), None)
    return selected


def project_row(
        row: Mapping[str, Any],
        mapping: ColumnMapping,
        target_columns: tuple[str, ...],
        version_field: Optional[str] = None
        ) -> tuple[Any, ...]:
    """
    Build a positional tuple of values for ``target_columns`` from a
    record using the given mapping.

    Only mapped source fields are bound to business columns.  The version
    selection field and any unmapped diagnostic fields are ignored.  A
    target column whose source field is not declared by the record is
    represented by ``None``; callers decide whether that is acceptable
    based on the nullability of the column.

    Dictionary key order is irrelevant: values are always returned in the
    fixed order of ``target_columns``.

    :param row: source record as a dictionary
    :param mapping: target column to source field(s) mapping
    :param target_columns: target columns in database column order
    :param version_field: source field whose value selects the version
    :return: tuple of values in target column order
    """
    values = []
    for target in target_columns:
        sources = mapping.get(target)
        if sources is None:
            values.append(None)
            continue

        source = resolve_source_field(sources, row, version_field)
        if source is None:
            values.append(None)
        else:
            values.append(row.get(source))

    return tuple(values)
