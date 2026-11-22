"""Form and merge primitives for inbound receiving review.

The important property of this module is that a line is identified by the
stable material line number (``line_no``) and never by its position in the
form.  Reordering rows and editing the form level remark are therefore
metadata-only operations and cannot be confused with a change of a line's
station or quantity.
"""

from dataclasses import dataclass, field
from typing import Any, Optional

from SpiffWorkflow.exceptions import SpiffWorkflowException


class InboundError(SpiffWorkflowException):
    """Base class for inbound receiving review errors."""


class FormValidationError(InboundError):
    """The form is not structurally valid."""


class FormVersionError(InboundError):
    """Two form versions cannot be merged or one is stale."""


class PreverificationError(InboundError):
    """A form cannot be preverified or its confirmation no longer matches."""


class ReservationError(InboundError):
    """The reservation system rejected an operation."""


class DeliveryError(InboundError):
    """A delivery request cannot be made."""


class ArchivedVersionError(InboundError):
    """The published/delivered version may no longer be modified."""


def _require_positive_int(value: Any, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise FormValidationError(f'{name} must be a positive integer, got {value!r}')


@dataclass
class Line:
    """One stable material row.

    ``station`` may be ``None`` while the row is still a draft; a complete
    form (ready for preverification or confirmation) requires a station.
    """

    line_no: int
    station: Optional[str]
    quantity: int
    note: str = ''

    def __post_init__(self) -> None:
        _require_positive_int(self.line_no, 'line_no')
        _require_positive_int(self.quantity, 'quantity')
        if self.station is not None:
            if not isinstance(self.station, str) or not self.station.strip():
                raise FormValidationError(f'line {self.line_no}: station must be a non-empty string')
        if not isinstance(self.note, str):
            raise FormValidationError(f'line {self.line_no}: note must be a string')

    def to_dict(self) -> dict:
        return {
            'line_no': self.line_no,
            'station': self.station,
            'quantity': self.quantity,
            'note': self.note,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'Line':
        return cls(
            line_no=data['line_no'],
            station=data.get('station'),
            quantity=data['quantity'],
            note=data.get('note', ''),
        )

    @property
    def key(self) -> tuple:
        return self.line_no, self.station, self.quantity

    @property
    def value(self) -> dict:
        return {'station': self.station, 'quantity': self.quantity}


@dataclass
class FormVersion:
    """A concrete version of a receiving review form."""

    version: int
    form_id: str
    lines: list = field(default_factory=list)
    notes: str = ''

    def __post_init__(self) -> None:
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise FormValidationError(f'version must be a non-negative integer, got {self.version!r}')
        if not isinstance(self.form_id, str) or not self.form_id.strip():
            raise FormValidationError('form_id must be a non-empty string')
        if not isinstance(self.notes, str):
            raise FormValidationError('notes must be a string')

        normalized = []
        seen = set()
        for line in self.lines:
            if isinstance(line, dict):
                line = Line.from_dict(line)
            if not isinstance(line, Line):
                raise FormValidationError(f'unrecognized line {line!r}')
            if line.line_no in seen:
                raise FormValidationError(f'duplicate line_no {line.line_no}')
            seen.add(line.line_no)
            normalized.append(line)
        self.lines = normalized

    def to_dict(self) -> dict:
        return {
            'version': self.version,
            'form_id': self.form_id,
            'lines': [line.to_dict() for line in self.lines],
            'notes': self.notes,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'FormVersion':
        return cls(
            version=data['version'],
            form_id=data['form_id'],
            lines=[Line.from_dict(line) for line in data.get('lines', [])],
            notes=data.get('notes', ''),
        )

    @property
    def line_numbers(self) -> list:
        return [line.line_no for line in self.lines]

    @property
    def line_map(self) -> dict:
        return {line.line_no: line for line in self.lines}

    @property
    def is_complete(self) -> bool:
        return all(line.station is not None for line in self.lines)

    def line(self, line_no: int) -> Line:
        try:
            return self.line_map[line_no]
        except KeyError:
            raise FormValidationError(f'form {self.form_id} has no line {line_no}')

    def line_values(self) -> dict:
        """The preverification-relevant content: only line number, station and quantity."""
        return {line.line_no: line.value for line in self.lines}

    def stations(self) -> list:
        seen = []
        for line in self.lines:
            if line.station is not None and line.station not in seen:
                seen.append(line.station)
        return seen


def as_form_version(value: Any) -> FormVersion:
    if isinstance(value, FormVersion):
        return value
    if isinstance(value, dict):
        return FormVersion.from_dict(value)
    raise FormValidationError(f'expected a form version, got {value!r}')


def normalize_line_map(values: Any) -> dict:
    """Normalize a line-number keyed mapping loaded from JSON.

    JSON object keys are strings, so an archive round trip would otherwise
    change integer line numbers into strings.  This restores them and rejects
    ambiguous duplicate keys.
    """
    result = {}
    for key, value in (values or {}).items():
        line_no = int(key)
        if line_no in result:
            raise FormValidationError(f'duplicate line number {line_no} in mapping')
        result[line_no] = value
    return result


def validate_complete(form: FormVersion, operation: str = 'preverification') -> None:
    """Validate the fields that participate in preverification and confirmation."""
    form = as_form_version(form)
    if not form.lines:
        raise FormValidationError(f'form {form.form_id} has no lines')
    missing = [line.line_no for line in form.lines if line.station is None]
    if missing:
        raise FormValidationError(
            f'form {form.form_id} cannot be used for {operation}; lines {missing} have no station'
        )


def merge_by_line_no(base: Any, candidate: Any) -> FormVersion:
    """Merge station/quantity from ``candidate`` into ``base`` by line number.

    Line values are resolved per stable line number, never by row position.
    Form metadata (row order and remark) comes from ``candidate`` because it
    represents the edit being applied.  Callers that need to avoid
    overwriting another editor's metadata should use :func:`merge_versions`.
    """
    base = as_form_version(base)
    candidate = as_form_version(candidate)

    if base.form_id != candidate.form_id:
        raise FormVersionError(
            f'cannot merge forms {base.form_id!r} and {candidate.form_id!r}'
        )
    if set(base.line_numbers) != set(candidate.line_numbers):
        raise FormVersionError(
            'line numbers are stable; base has '
            f'{sorted(base.line_numbers)} and candidate has {sorted(candidate.line_numbers)}'
        )

    candidate_lines = candidate.line_map
    merged_lines = [
        Line(
            line_no=line.line_no,
            station=candidate_lines[line.line_no].station,
            quantity=candidate_lines[line.line_no].quantity,
            note=line.note,
        )
        for line in candidate.lines
    ]
    return FormVersion(
        version=max(base.version, candidate.version),
        form_id=candidate.form_id,
        lines=merged_lines,
        notes=candidate.notes,
    )


def merge_versions(server: Any, base: Any, candidate: Any) -> FormVersion:
    """Three-way merge of an editor's version into the current server form.

    ``base`` is the version the editor originally opened, ``candidate`` is the
    editor's result and ``server`` is the canonical current form.

    * A line whose station/quantity changed in ``candidate`` relative to
      ``base`` takes the candidate's value.  An unchanged line keeps the
      server's value, so one editor's row changes cannot slide to another
      line.
    * Row order and the form remark are taken from ``candidate`` only if the
      editor changed them relative to ``base``; otherwise the server's
      metadata wins.  Reordering rows and clearing remarks therefore compose
      with a concurrent line edit without allocating reservations.
    * A line changed differently on both sides is a conflict.
    """
    server = as_form_version(server)
    base = as_form_version(base)
    candidate = as_form_version(candidate)

    for label, form in (('server', server), ('base', base), ('candidate', candidate)):
        if form.form_id != server.form_id:
            raise FormVersionError(f'{label} form {form.form_id!r} is not {server.form_id!r}')
        if set(form.line_numbers) != set(server.line_numbers):
            raise FormVersionError(
                f'{label} form has different stable line numbers than the server'
            )

    base_lines = base.line_map
    server_lines = server.line_map
    candidate_lines = candidate.line_map

    merged_lines = []
    for line_no in candidate.line_numbers:
        editor_changed = candidate_lines[line_no].value != base_lines[line_no].value
        server_changed = server_lines[line_no].value != base_lines[line_no].value
        if editor_changed and server_changed:
            if candidate_lines[line_no].value != server_lines[line_no].value:
                raise FormVersionError(
                    f'line {line_no} was changed to different values by two editors'
                )
        if editor_changed:
            station = candidate_lines[line_no].station
            quantity = candidate_lines[line_no].quantity
        else:
            station = server_lines[line_no].station
            quantity = server_lines[line_no].quantity
        merged_lines.append(Line(
            line_no=line_no,
            station=station,
            quantity=quantity,
            note=candidate_lines[line_no].note,
        ))

    editor_changed_metadata = (
        candidate.notes != base.notes or candidate.line_numbers != base.line_numbers
    )
    if editor_changed_metadata:
        order = candidate.line_numbers
        notes = candidate.notes
    else:
        order = server.line_numbers
        notes = server.notes

    by_number = {line.line_no: line for line in merged_lines}
    ordered = [by_number[line_no] for line_no in order]
    return FormVersion(
        version=max(server.version, candidate.version),
        form_id=server.form_id,
        lines=ordered,
        notes=notes,
    )


def changed_line_values(base: Any, candidate: Any) -> dict:
    """Return only the station/quantity changes, keyed by stable line number."""
    base = as_form_version(base)
    candidate = as_form_version(candidate)
    if set(base.line_numbers) != set(candidate.line_numbers):
        raise FormVersionError('line numbers are stable and must not change')
    result = {}
    base_lines = base.line_map
    for line_no, line in candidate.line_map.items():
        old = base_lines[line_no].value
        if old != line.value:
            result[line_no] = {'old': old, 'new': line.value}
    return result


def lines_by_station(form: FormVersion) -> dict:
    result = {}
    for line in form.lines:
        if line.station is not None:
            result.setdefault(line.station, []).append(line)
    return result
