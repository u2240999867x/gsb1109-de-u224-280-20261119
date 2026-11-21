"""Audit provenance for metadata resolved from JSON sidecars.

The index stores a frozen :class:`~bids.layout.models.MetadataProvenance` row
for every ``(data file, metadata key, sidecar)`` combination that was eligible
at indexing time. The helpers in this module turn those rows into a
reviewer-facing report describing, for each key, which sidecars were
candidates, what each one claimed, and why the ordinary value ended up the way
it did.

The report is built entirely from the index snapshot. It therefore keeps
describing the candidates and evidence that were present when the index was
created even after the index is moved or the sidecars on disk change.
"""

import json
import os
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.orm import object_session

from .models import BIDSFile, MetadataProvenance

# Resolution reasons reported for a non-ambiguous key.
SOLE = 'sole'  # one sidecar (or one level) declared the key
OVERRIDE = 'override'  # a closer sidecar replaced a farther sidecar's value
EXPLICIT_NULL = 'explicit_null'  # closest mention is an explicit JSON null
AMBIGUOUS = 'ambiguous'  # competing values with no rule to pick a winner


@dataclass
class MetadataSource:
    """One sidecar's claim for a single metadata key.

    Attributes
    ----------
    path : str
        Path of the JSON sidecar.
    directory : str
        Directory containing the sidecar. Sidecars in the same directory are
        at the same inheritance level and cannot specialize one another.
    value : object
        Decoded JSON value. ``None`` when ``is_null`` is True.
    is_null : bool
        True when the sidecar explicitly set the key to JSON ``null``.
    discovery_order : int
        Position of the sidecar in discovery order (zero = most specific /
        closest to the data file).

    """

    path: str
    directory: str
    value: object
    is_null: bool
    discovery_order: int


@dataclass
class FieldProvenance:
    """Evidence and resolution for one metadata key of one data file."""

    key: str
    sources: list[MetadataSource] = field(default_factory=list)
    reason: str = SOLE
    value: object = None

    @property
    def is_ambiguous(self):
        """True when the field has unresolved same-level competing values."""
        return self.reason == AMBIGUOUS

    @property
    def competing_values(self):
        """Distinct non-null values offered by competing candidates."""
        seen = []
        for source in self.sources:
            if source.is_null:
                continue
            if not any(_values_equal(source.value, prior) for prior in seen):
                seen.append(source.value)
        return seen


@dataclass
class MetadataProvenanceReport:
    """Per-key provenance evidence for a single data file.

    Attributes
    ----------
    path : str
        Path of the data file the evidence was collected for.
    fields : dict
        Mapping of metadata key to :class:`FieldProvenance`.

    """

    path: str
    fields: dict[str, FieldProvenance] = field(default_factory=dict)

    def __getitem__(self, key):
        return self.fields[key]

    def __contains__(self, key):
        return key in self.fields

    def keys(self):
        """Return the metadata keys covered by the report."""
        return self.fields.keys()

    def values(self):
        """Return the per-field provenance records."""
        return self.fields.values()

    def items(self):
        """Return ``(key, field provenance)`` pairs."""
        return self.fields.items()

    @property
    def ambiguous_fields(self):
        """Return only the fields whose same-level candidates conflict."""
        return {key: fp for key, fp in self.fields.items() if fp.is_ambiguous}

    def reconcile(self, metadata):
        """Check the snapshot against ordinary resolved metadata.

        Parameters
        ----------
        metadata : dict
            The plain metadata returned by
            :meth:`~bids.layout.BIDSLayout.get_metadata`.

        Returns
        -------
        dict
            Mapping of every non-ambiguous key to a bool indicating whether
            the ordinary value matches the value the provenance snapshot
            resolves to. Ambiguous keys are excluded because the index
            deliberately does not choose a winner for them.

        """
        result = {}
        for key, fp in self.fields.items():
            if fp.is_ambiguous:
                continue
            if fp.reason == EXPLICIT_NULL:
                # Explicit nulls are dropped from ordinary metadata.
                result[key] = key not in metadata
            else:
                result[key] = key in metadata and _values_equal(metadata[key], fp.value)
        return result


def _values_equal(left, right):
    try:
        return bool(left == right)
    except Exception:  # noqa: BLE001
        return False


def _decode_value(row):
    if row.is_null or row.value is None:
        return None
    return json.loads(row.value)


def _table_exists(session):
    """Return whether this index stores provenance rows.

    Databases created before provenance was introduced lack the table; the
    report is then empty rather than raising.
    """
    return sa.inspect(session.get_bind()).has_table(MetadataProvenance.__tablename__)


def build_metadata_provenance(layout, path, scope='all'):
    """Build a provenance report for one data file from the index snapshot.

    Parameters
    ----------
    layout : :obj:`~bids.layout.BIDSLayout`
        Layout whose index holds the provenance rows.
    path : str
        Path of the data file to report on.
    scope : str or list, optional
        Scope of the search space; see
        :meth:`~bids.layout.BIDSLayout.get_metadata`.

    Returns
    -------
    :obj:`MetadataProvenanceReport`

    """
    target = str(path)
    report = MetadataProvenanceReport(path=target)

    for candidate_layout in layout._get_layouts_in_scope(scope):
        session = candidate_layout.session
        if not _table_exists(session):
            continue
        bids_file = session.query(BIDSFile).filter_by(path=target).first()
        if bids_file is None:
            continue

        rows = (
            session.query(MetadataProvenance)
            .filter_by(file_path=target)
            .order_by(MetadataProvenance.key, MetadataProvenance.discovery_order)
            .all()
        )

        grouped = {}
        for row in rows:
            grouped.setdefault(row.key, []).append(row)

        for key, key_rows in grouped.items():
            report.fields[key] = _resolve_field(key, _build_sources(key_rows))
        break

    return report


def _build_sources(rows):
    """Turn provenance rows into ordered, level-aware metadata sources."""
    sources = [
        MetadataSource(
            path=row.source_path,
            directory=os.path.dirname(row.source_path),
            value=_decode_value(row),
            is_null=bool(row.is_null),
            discovery_order=row.discovery_order,
        )
        for row in rows
    ]
    sources.sort(key=lambda source: source.discovery_order)
    return sources


def _resolve_field(key, sources):
    """Determine the resolution reason and effective value for a key."""
    fp = FieldProvenance(key=key, sources=sources)

    # Sidecars in the same directory are at the same inheritance level and
    # cannot specialize one another. Distinct non-null values offered at one
    # level are an unresolved ambiguity; scan order must not pick a winner.
    levels = {}
    for source in sources:
        levels.setdefault(source.directory, []).append(source)

    for level_sources in levels.values():
        distinct = []
        for source in level_sources:
            if source.is_null:
                continue
            if not any(_values_equal(source.value, prior) for prior in distinct):
                distinct.append(source.value)
        if len(distinct) > 1:
            fp.reason = AMBIGUOUS
            fp.value = None
            return fp

    closest = min(sources, key=lambda source: source.discovery_order)
    if closest.is_null:
        fp.reason = EXPLICIT_NULL
        fp.value = None
        return fp

    fp.value = closest.value
    distinct_values = fp.competing_values
    fp.reason = OVERRIDE if len(distinct_values) > 1 else SOLE
    return fp


def explain_metadata_provenance(report):
    """Render a human-readable explanation of a provenance report.

    Ambiguous keys are described with the concrete field name and every
    competing candidate (path + value), together with an explicit note that
    no winner was selected because no resolution policy was supplied.
    Non-ambiguous keys are summarized with their resolution reason.

    Parameters
    ----------
    report : :obj:`MetadataProvenanceReport`
        The per-field evidence record to render as text.

    Returns
    -------
    str

    """
    lines = [f'Provenance for {report.path}:']
    if not report.fields:
        lines.append('  No indexed sidecar evidence.')
        return '\n'.join(lines)

    for key, fp in sorted(report.fields.items()):
        if fp.is_ambiguous:
            lines.append(
                f'  {key}: AMBIGUOUS; competing candidates cannot be resolved '
                'without an explicit policy:'
            )
            for source in fp.sources:
                rendered = 'null' if source.is_null else json.dumps(source.value)
                lines.append(
                    f'    - {source.path} (discovery_order={source.discovery_order}) -> {rendered}'
                )
            lines.append('    No winner was chosen by scan order.')
        elif fp.reason == EXPLICIT_NULL:
            closest = min(fp.sources, key=lambda source: source.discovery_order)
            lines.append(f'  {key}: explicit null from {closest.path}; treated as absent.')
        else:
            rendered = json.dumps(fp.value)
            lines.append(f'  {key}: {rendered} ({fp.reason}) from {len(fp.sources)} candidate(s).')

    return '\n'.join(lines)


def file_metadata_provenance(bids_file):
    """Return a provenance report for a :class:`~bids.layout.models.BIDSFile`.

    Convenience wrapper used by ``BIDSFile.get_metadata_provenance`` that
    builds the report directly from the file's own index rows.
    """
    session = object_session(bids_file)
    report = MetadataProvenanceReport(path=bids_file.path)
    if not _table_exists(session):
        return report
    rows = (
        session.query(MetadataProvenance)
        .filter_by(file_path=bids_file.path)
        .order_by(MetadataProvenance.key, MetadataProvenance.discovery_order)
        .all()
    )
    grouped = {}
    for row in rows:
        grouped.setdefault(row.key, []).append(row)
    for key, key_rows in grouped.items():
        report.fields[key] = _resolve_field(key, _build_sources(key_rows))
    return report
