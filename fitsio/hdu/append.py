"""
Validation helpers for appending rows to FITS table HDUs.

The helpers in this module deliberately do all layout, shape, type and
range checks before any row is written.  A caller can therefore validate a
whole batch, inspect the report, and only then commit it.  The normal
:meth:`fitsio.hdu.table.TableHDU.append` path is not affected.
"""

from __future__ import annotations

import math

import numpy as np

from ..util import isstring

_UNSET = object()


class AppendIssue(object):
    """
    A single problem found while validating an append batch.

    Attributes
    ----------
    row : int or None
        Zero-based input row.  ``None`` for problems that apply to a whole
        column or to the mapping.
    row_number : int or None
        One-based input row, for display.
    column : str or None
        Target column name.
    condition : str
        Short machine-readable condition, for example ``"shape"``,
        ``"length"``, ``"range"`` or ``"type"``.
    message : str
        Human readable description.
    value : object
        The offending value, when one is available.
    """

    def __init__(
        self,
        message,
        row=None,
        column=None,
        condition=None,
        value=_UNSET,
        expected=None,
        actual=None,
        element=None,
    ):
        self.row = row
        self.row_number = None if row is None else row + 1
        self.column = column
        self.condition = condition
        self.kind = condition
        self.code = condition
        self.message = message
        self.value = None if value is _UNSET else value
        self.expected = expected
        self.actual = actual
        self.element = element

    def as_dict(self):
        return {
            'row': self.row,
            'row_number': self.row_number,
            'column': self.column,
            'condition': self.condition,
            'message': self.message,
            'value': self.value,
            'expected': self.expected,
            'actual': self.actual,
            'element': self.element,
        }

    def __getitem__(self, key):
        return self.as_dict()[key]

    def __repr__(self):
        return "AppendIssue(%r)" % self.message

    def __str__(self):
        return self.message


class FillEvent(object):
    """
    Record of one value supplied by a fill policy.

    ``kind`` is one of ``"missing"`` (an explicit missing marker),
    ``"absent"`` (a row with no data at all) or ``"absent_column"`` (the
    source column was not supplied).  ``count`` is the number of cells
    filled, which is one for scalar columns.
    """

    def __init__(self, row, column, kind, value, count=1):
        self.row = row
        self.row_number = None if row is None else row + 1
        self.column = column
        self.kind = kind
        self.value = value
        self.count = count

    def as_dict(self):
        return {
            'row': self.row,
            'row_number': self.row_number,
            'column': self.column,
            'kind': self.kind,
            'value': self.value,
            'count': self.count,
        }

    def __getitem__(self, key):
        return self.as_dict()[key]

    def __repr__(self):
        return "FillEvent(row=%r, column=%r, kind=%r)" % (
            self.row,
            self.column,
            self.kind,
        )


class ColumnReport(object):
    """Summary information for one target column."""

    def __init__(self, target, source, shape, axes, is_variable):
        self.target = target
        self.column = target
        self.source = source
        self.shape = shape
        self.axes = axes
        self.is_variable = is_variable
        self.filled_missing = 0
        self.filled_absent = 0
        self.filled_absent_column = 0
        self.filled_cells = 0
        self.rows = 0

    @property
    def absent(self):
        return self.filled_absent_column > 0

    @property
    def filled(self):
        return (
            self.filled_missing
            + self.filled_absent
            + self.filled_absent_column
        )

    def as_dict(self):
        return {
            'target': self.target,
            'source': self.source,
            'shape': self.shape,
            'axes': self.axes,
            'is_variable': self.is_variable,
            'filled_missing': self.filled_missing,
            'filled_absent': self.filled_absent,
            'filled_absent_column': self.filled_absent_column,
            'filled_cells': self.filled_cells,
            'rows': self.rows,
        }

    def __getitem__(self, key):
        return self.as_dict()[key]

    def __repr__(self):
        return "ColumnReport(target=%r, source=%r)" % (
            self.target,
            self.source,
        )


class AppendReport(object):
    """
    Result of validating an append batch.

    The report is truthy only when the batch can be written.  It carries
    the validated columns in target order so that the commit step does not
    have to re-interpret the caller's mapping.
    """

    def __init__(self, nrows=0):
        self.nrows = nrows
        self.issues = []
        self.errors = self.issues
        self.columns = {}
        self.fills = []
        self.ignored_sources = []
        self.written = False
        self.rows_written = 0
        self._plan = None
        self._target_order = None

    @property
    def ok(self):
        return len(self.issues) == 0

    def add_issue(self, issue):
        self.issues.append(issue)

    def add_error(self, message, **kwargs):
        issue = AppendIssue(message, **kwargs)
        self.add_issue(issue)
        return issue

    @property
    def fill_counts(self):
        """
        Counts of filled cells keyed by target column.

        Each entry has ``missing``, ``absent``, ``absent_column`` and
        ``cells`` keys.  This keeps the two temperature policies (explicit
        missing marker versus a row with no temperature data) separately
        queryable.
        """
        counts = {}
        for event in self.fills:
            entry = counts.setdefault(
                event.column,
                {
                    'missing': 0,
                    'absent': 0,
                    'absent_column': 0,
                    'cells': 0,
                },
            )
            entry[event.kind] = entry.get(event.kind, 0) + 1
            entry['cells'] += event.count
        return counts

    @property
    def missing_counts(self):
        return {
            column: entry['missing']
            for column, entry in self.fill_counts.items()
        }

    @property
    def absent_counts(self):
        return {
            column: entry['absent'] + entry['absent_column']
            for column, entry in self.fill_counts.items()
        }

    @property
    def missing(self):
        return self.missing_counts

    @property
    def absent(self):
        return self.absent_counts

    @property
    def events(self):
        return list(self.fills)

    def fills_for(self, column, kind=None):
        """
        Return the fill events for one target column.

        ``kind`` may be ``"missing"`` or ``"absent"`` to select one of the
        two policies.
        """
        column = str(column)
        selected = []
        for event in self.fills:
            if event.column.lower() != column.lower():
                continue
            if kind is not None and event.kind != kind:
                continue
            selected.append(event)
        return selected

    def rows_for(self, column, kind=None):
        """Return the zero-based input rows filled for one column."""
        return [event.row for event in self.fills_for(column, kind=kind)]

    def values_for(self, column, kind=None):
        """Return the values supplied for one column."""
        return [
            event.value for event in self.fills_for(column, kind=kind)
        ]

    def column(self, name):
        for key, value in self.columns.items():
            if key.lower() == str(name).lower():
                return value
        raise KeyError(name)

    def as_dict(self):
        return {
            'nrows': self.nrows,
            'ok': self.ok,
            'written': self.written,
            'rows_written': self.rows_written,
            'issues': [issue.as_dict() for issue in self.issues],
            'columns': {
                name: report.as_dict()
                for name, report in self.columns.items()
            },
            'fills': [event.as_dict() for event in self.fills],
            'fill_counts': self.fill_counts,
            'ignored_sources': list(self.ignored_sources),
        }

    def to_dict(self):
        return self.as_dict()

    def raise_if_errors(self):
        if not self.ok:
            raise AppendValidationError(self)

    def __bool__(self):
        return self.ok

    def __len__(self):
        return len(self.issues)

    def __iter__(self):
        return iter(self.issues)

    def __getitem__(self, key):
        return self.columns[key]

    def __repr__(self):
        return "AppendReport(nrows=%d, ok=%r, issues=%d)" % (
            self.nrows,
            self.ok,
            len(self.issues),
        )


class AppendValidationError(ValueError):
    """
    Raised when a checked batch cannot be appended.

    ``report`` and ``issues`` expose all problems.  No rows are written
    when this error is raised.
    """

    def __init__(self, report):
        self.report = report
        self.issues = report.issues
        self.errors = self.issues
        lines = [
            "append validation failed with %d issue(s):" % len(self.issues)
        ]
        for issue in self.issues:
            lines.append("  - %s" % issue.message)
        super(AppendValidationError, self).__init__("\n".join(lines))


class AppendError(AppendValidationError):
    """Alias kept for callers that do not distinguish validation errors."""


class AppendLayoutError(AppendValidationError):
    """Alias for layout-focused callers."""


class ColumnSpec(object):
    def __init__(
        self,
        target,
        source=None,
        axes=None,
        fill=_UNSET,
        missing=_UNSET,
        missing_fill=_UNSET,
    ):
        self.target = target
        self.source = source
        self.axes = None if axes is _UNSET else axes
        self.fill = fill
        self.missing = missing
        self.missing_fill = missing_fill


class TargetColumn(object):
    def __init__(
        self,
        colnum,
        name,
        dtype,
        shape,
        is_variable,
        is_string,
        width,
        max_length,
        is_bit,
    ):
        self.colnum = colnum
        self.name = name
        self.dtype = dtype
        self.shape = tuple(shape or ())
        self.is_variable = is_variable
        self.is_string = is_string
        self.width = width
        self.max_length = max_length
        self.is_bit = is_bit

    @property
    def is_scalar(self):
        return not self.is_variable and len(self.shape) == 0

    @property
    def kind(self):
        return self.dtype.kind

    def __repr__(self):
        return "TargetColumn(%r, dtype=%r, shape=%r)" % (
            self.name,
            self.dtype,
            self.shape,
        )


def _tdim_to_shape(tdim, is_string=False):
    if tdim is None:
        return ()
    tdim = tuple(int(item) for item in tdim)
    if len(tdim) == 0:
        return ()
    if is_string:
        dims = tdim[1:]
    else:
        dims = tdim
    dims = tuple(reversed(dims))
    if len(dims) == 0:
        return ()
    if len(dims) == 1:
        if dims[0] <= 1:
            return ()
        return (dims[0],)
    return dims


def _vararray_max(tform):
    first = tform.find('(')
    last = tform.rfind(')')
    if first == -1 or last == -1:
        return -1
    value = tform[first + 1 : last]
    try:
        return int(value)
    except (TypeError, ValueError):
        return -1


def describe_target_columns(hdu):
    """Return target column descriptions in table order."""
    targets = []
    for colnum, cinfo in enumerate(hdu._info['colinfo']):
        npy_type, isvar, isbit = hdu._get_tbl_numpy_dtype(
            colnum, include_endianness=False
        )
        name = cinfo['name']
        is_string = npy_type[0] in ('S', 'U')
        width = int(cinfo.get('width', 1) or 1)
        max_length = _vararray_max(cinfo.get('tform', ''))
        if is_string:
            dtype = np.dtype('S%d' % max(width, 1))
        elif npy_type == 'b1':
            # Logical values read back as booleans; TBIT columns are also
            # accepted through the same conversion.
            dtype = np.dtype('bool')
        else:
            dtype = np.dtype(npy_type)
        shape = _tdim_to_shape(cinfo.get('tdim'), is_string=is_string)
        targets.append(
            TargetColumn(
                colnum=colnum,
                name=name,
                dtype=dtype,
                shape=shape,
                is_variable=bool(isvar),
                is_string=is_string,
                width=width,
                max_length=max_length,
                is_bit=bool(isbit),
            )
        )
    return targets


def _lookup(mapping, name):
    if not isstring(name):
        return None
    lname = name.lower()
    for key, value in mapping.items():
        if key.lower() == lname:
            return value
    return None


def _first_key(mapping, names, default=_UNSET):
    if not isinstance(mapping, dict):
        return default
    for name in names:
        if name in mapping:
            return mapping[name]
    return default


_SOURCE_KEYS = (
    'source',
    'from',
    'input',
    'input_column',
    'src',
    'field',
)
_TARGET_KEYS = (
    'target',
    'to',
    'output',
    'output_column',
    'column',
    'dst',
)
_AXES_KEYS = (
    'axes',
    'axis_order',
    'axis_map',
    'permutation',
    'transpose',
    'axis',
)
_MISSING_KEYS = (
    'missing',
    'missing_value',
    'missing_values',
    'null',
    'null_value',
    'null_values',
)
_FILL_KEYS = ('fill', 'fill_value', 'value')
_MISSING_FILL_KEYS = ('missing_fill', 'fill_missing', 'missing_fill_value')


def _zero_value(target):
    if target.dtype.kind in ('S', 'U'):
        return ''
    if target.dtype.kind == 'b':
        return False
    return 0


def _zero_fill_allowed(target):
    """All supported columns can be zero-filled, matching old append."""
    return target.dtype.kind in ('i', 'u', 'f', 'c', 'S', 'U', 'b')


def _normalise_mapping(
    targets,
    data_fields,
    mapping,
    axis_order,
    fill,
    missing,
    missing_fill,
):
    """Turn the many accepted mapping forms into ColumnSpec objects."""
    target_by_lower = {target.name.lower(): target for target in targets}
    source_by_lower = {}
    for field in data_fields:
        source_by_lower.setdefault(field.lower(), field)

    specs = {}
    issues = []

    def add_spec(spec, origin=None):
        if spec.target is None:
            issues.append(
                AppendIssue(
                    "column mapping entry has no target column",
                    condition='mapping',
                )
            )
            return
        target = target_by_lower.get(spec.target.lower())
        if target is None:
            issues.append(
                AppendIssue(
                    "column mapping refers to unknown target column '%s'"
                    % spec.target,
                    column=spec.target,
                    condition='unknown_column',
                )
            )
            return
        if target.name in specs:
            issues.append(
                AppendIssue(
                    "target column '%s' is mapped more than once"
                    % target.name,
                    column=target.name,
                    condition='duplicate',
                )
            )
            return
        spec.target = target.name
        if spec.source is None and spec.fill is _UNSET:
            # An entry that only declares axes still matches a field with
            # the target's name, just like the implicit mapping does.
            spec.source = source_by_lower.get(target.name.lower())
        if spec.source is not None:
            source = source_by_lower.get(str(spec.source).lower())
            if source is None:
                issues.append(
                    AppendIssue(
                        "input field '%s' mapped to target column '%s' "
                        "is not present" % (spec.source, target.name),
                        column=target.name,
                        condition='mapping',
                    )
                )
            spec.source = source
        specs[target.name] = spec

    if mapping is None:
        for target in targets:
            source = source_by_lower.get(target.name.lower())
            specs[target.name] = ColumnSpec(
                target=target.name,
                source=source,
            )
    elif isinstance(mapping, dict):
        for key, value in mapping.items():
            spec = _spec_from_item(
                key,
                value,
                target_by_lower,
                source_by_lower,
            )
            if isinstance(spec, AppendIssue):
                issues.append(spec)
            else:
                add_spec(spec, origin=key)
    elif isinstance(mapping, (list, tuple)):
        for item in mapping:
            if not isinstance(item, dict):
                issues.append(
                    AppendIssue(
                        "column mapping entries must be dictionaries",
                        condition='mapping',
                    )
                )
                continue
            target = _first_key(item, _TARGET_KEYS)
            source = _first_key(item, _SOURCE_KEYS)
            if target is _UNSET:
                issues.append(
                    AppendIssue(
                        "column mapping entry has no target column",
                        condition='mapping',
                    )
                )
                continue
            spec = ColumnSpec(
                target=str(target),
                source=None if source is _UNSET else source,
                axes=_first_key(item, _AXES_KEYS, None),
                fill=_first_key(item, _FILL_KEYS),
                missing=_first_key(item, _MISSING_KEYS),
                missing_fill=_first_key(item, _MISSING_FILL_KEYS),
            )
            add_spec(spec)
    else:
        issues.append(
            AppendIssue(
                "mapping must be a dictionary or a list of dictionaries",
                condition='mapping',
            )
        )

    # Top level per-column settings supplement the mapping entries.
    for target in targets:
        spec = specs.get(target.name)
        if spec is None:
            spec = ColumnSpec(target=target.name)
            specs[target.name] = spec

        axes_value = _lookup(axis_order, target.name) if axis_order else None
        if axes_value is not None and spec.axes is None:
            spec.axes = axes_value

        fill_value = (
            _lookup(fill, target.name) if isinstance(fill, dict) else None
        )
        if fill_value is not None and spec.fill is _UNSET:
            spec.fill = fill_value

        missing_value = (
            _lookup(missing, target.name)
            if isinstance(missing, dict)
            else None
        )
        if missing_value is not None and spec.missing is _UNSET:
            spec.missing = missing_value

        missing_fill_value = (
            _lookup(missing_fill, target.name)
            if isinstance(missing_fill, dict)
            else None
        )
        if missing_fill_value is not None and spec.missing_fill is _UNSET:
            spec.missing_fill = missing_fill_value

        # A scalar fill/missing policy is applied to every column that has
        # no explicit setting of its own.
        if not isinstance(fill, dict) and fill is not None:
            if spec.fill is _UNSET:
                spec.fill = fill
        if not isinstance(missing, dict) and missing is not None:
            if spec.missing is _UNSET:
                spec.missing = missing
        if not isinstance(missing_fill, dict) and missing_fill is not None:
            if spec.missing_fill is _UNSET:
                spec.missing_fill = missing_fill

    for target in targets:
        spec = specs[target.name]
        if spec.source is None:
            if spec.fill is not _UNSET or spec.missing_fill is not _UNSET:
                continue
            # Match the historical append behaviour for columns that are
            # not supplied: they are filled with zeros.
            spec.fill = _zero_value(target)

    ignored = [
        field
        for field in data_fields
        if not any(spec.source == field for spec in specs.values())
    ]

    return specs, issues, ignored


def _spec_from_item(key, value, target_by_lower, source_by_lower):
    target = None
    source = None
    axes = None
    fill = _UNSET
    missing = _UNSET
    missing_fill = _UNSET

    if isinstance(value, dict):
        target = _first_key(value, _TARGET_KEYS)
        source = _first_key(value, _SOURCE_KEYS)
        axes = _first_key(value, _AXES_KEYS)
        fill = _first_key(value, _FILL_KEYS)
        missing = _first_key(value, _MISSING_KEYS)
        missing_fill = _first_key(value, _MISSING_FILL_KEYS)
        if target is _UNSET:
            if isstring(key) and key.lower() in target_by_lower:
                target = key
            elif isstring(source) and source.lower() in target_by_lower:
                target = source
                source = key
            else:
                target = key
        elif source is _UNSET:
            if isstring(key) and key.lower() in source_by_lower:
                source = key
        else:
            pass
    elif isinstance(value, (list, tuple)) and all(
        isinstance(item, (int, np.integer)) for item in value
    ):
        target = key
        source = key
        axes = value
    elif isstring(value) or value is None:
        if isstring(key) and key.lower() in target_by_lower:
            target = key
            source = value
        elif isstring(value) and value.lower() in target_by_lower:
            target = value
            source = key
        else:
            target = key
            source = value
    else:
        return AppendIssue(
            "unsupported column mapping entry for '%s'" % key,
            column=str(key),
            condition='mapping',
        )

    if target is _UNSET or target is None:
        return AppendIssue(
            "column mapping entry has no target column",
            column=str(key),
            condition='mapping',
        )
    if source is _UNSET:
        source = None
    return ColumnSpec(
        target=str(target),
        source=None if source is None else str(source),
        axes=axes,
        fill=fill,
        missing=missing,
        missing_fill=missing_fill,
    )


def _is_absent_value(value):
    if value is None:
        return True
    if isinstance(value, np.ma.MaskedArray) and np.ma.is_masked(value):
        return True
    if isinstance(value, np.ma.core.MaskedConstant):
        return True
    if isinstance(value, np.ndarray) and value.dtype.kind == 'O':
        if value.shape == () and value.item() is None:
            return True
    if isinstance(value, (list, tuple, np.ndarray)):
        try:
            if np.size(value) == 0:
                return True
        except Exception:
            pass
    if isinstance(value, (float, np.floating)):
        try:
            return bool(np.isnan(value))
        except Exception:
            return False
    return False


def _equal(a, b):
    if a is None or b is None:
        return a is b
    try:
        if isinstance(a, (float, np.floating)) and isinstance(
            b, (float, np.floating)
        ):
            if math.isnan(float(a)) and math.isnan(float(b)):
                return True
        if isinstance(a, (np.ndarray, list, tuple)):
            return False
        if isinstance(b, (np.ndarray, list, tuple)):
            return False
        return bool(a == b)
    except Exception:
        return False


def _marker_list(markers):
    if markers is _UNSET or markers is None:
        return ()
    if isinstance(markers, (list, tuple, set, np.ndarray)):
        return list(markers)
    return [markers]


def _is_marker(value, markers):
    for marker in markers:
        if isinstance(marker, (float, np.floating)) and math.isnan(
            float(marker)
        ):
            if isinstance(value, (float, np.floating)) and math.isnan(
                float(value)
            ):
                return True
        elif _equal(value, marker):
            return True
    return False


def _describe(value):
    try:
        if isinstance(value, np.ndarray):
            return "array(shape=%r)" % (value.shape,)
        text = repr(value)
    except Exception:
        text = "<unrepresentable>"
    if len(text) > 80:
        text = text[:77] + '...'
    return text


def _check_scalar(value, target, row=None, element=None):
    """Validate one source value against a target dtype.

    Returns ``(converted, issue)``.  Values are checked in their original
    type and against the target's real bounds; they are never narrowed
    first and compared afterwards.
    """
    kind = target.dtype.kind
    column = target.name

    if kind == 'b':
        if isinstance(value, (bool, np.bool_)):
            return bool(value), None
        if isinstance(value, (int, np.integer)) and int(value) in (0, 1):
            return bool(value), None
        return None, AppendIssue(
            "target column '%s' is logical, got %s"
            % (column, _describe(value)),
            row=row,
            column=column,
            condition='type',
            value=value,
            expected='boolean',
            actual=type(value).__name__,
            element=element,
        )

    if kind in ('i', 'u'):
        original = value
        if isinstance(value, (bool, np.bool_)):
            value = int(value)
        elif isinstance(value, (int, np.integer)):
            value = int(value)
        elif isinstance(value, (float, np.floating)):
            fvalue = float(value)
            if not np.isfinite(fvalue) or not fvalue.is_integer():
                return None, AppendIssue(
                    "target column '%s' requires integers, got %s"
                    % (column, _describe(original)),
                    row=row,
                    column=column,
                    condition='type',
                    value=original,
                    expected='integer',
                    actual=type(original).__name__,
                    element=element,
                )
            value = int(fvalue)
        else:
            return None, AppendIssue(
                "target column '%s' requires integers, got %s"
                % (column, _describe(original)),
                row=row,
                column=column,
                condition='type',
                value=original,
                expected='integer',
                actual=type(original).__name__,
                element=element,
            )
        info = np.iinfo(target.dtype)
        if value < info.min or value > info.max:
            return None, AppendIssue(
                "input row %s, target column '%s': value %s is outside "
                "%s range [%s, %s]"
                % (
                    row,
                    column,
                    _describe(original),
                    target.dtype,
                    info.min,
                    info.max,
                ),
                row=row,
                column=column,
                condition='range',
                value=original,
                expected=(info.min, info.max),
                actual=value,
                element=element,
            )
        return value, None

    if kind == 'f':
        if not isinstance(value, (int, float, np.integer, np.floating)):
            return None, AppendIssue(
                "target column '%s' requires numbers, got %s"
                % (column, _describe(value)),
                row=row,
                column=column,
                condition='type',
                value=value,
                expected='number',
                actual=type(value).__name__,
                element=element,
            )
        fvalue = float(value)
        if np.isnan(fvalue):
            return np.nan, None
        if not np.isfinite(fvalue):
            return None, AppendIssue(
                "input row %s, target column '%s': value %s is not finite"
                % (row, column, _describe(value)),
                row=row,
                column=column,
                condition='range',
                value=value,
                expected='finite',
                actual=fvalue,
                element=element,
            )
        max_value = float(np.finfo(target.dtype).max)
        if abs(fvalue) > max_value:
            return None, AppendIssue(
                "input row %s, target column '%s': value %s exceeds "
                "%s range [%s, %s]"
                % (
                    row,
                    column,
                    _describe(value),
                    target.dtype,
                    -max_value,
                    max_value,
                ),
                row=row,
                column=column,
                condition='range',
                value=value,
                expected=(-max_value, max_value),
                actual=fvalue,
                element=element,
            )
        return fvalue, None

    if kind == 'c':
        try:
            cvalue = complex(value)
        except Exception:
            return None, AppendIssue(
                "target column '%s' requires complex numbers, got %s"
                % (column, _describe(value)),
                row=row,
                column=column,
                condition='type',
                value=value,
                expected='complex',
                actual=type(value).__name__,
                element=element,
            )
        max_value = float(np.finfo(target.dtype).max)
        if (
            not np.isfinite(cvalue.real)
            or not np.isfinite(cvalue.imag)
            or abs(cvalue.real) > max_value
            or abs(cvalue.imag) > max_value
        ):
            return None, AppendIssue(
                "input row %s, target column '%s': value %s exceeds "
                "%s range"
                % (row, column, _describe(value), target.dtype),
                row=row,
                column=column,
                condition='range',
                value=value,
                expected=max_value,
                actual=cvalue,
                element=element,
            )
        return cvalue, None

    if kind in ('S', 'U'):
        if isinstance(value, np.bytes_):
            value = bytes(value)
        if isinstance(value, np.str_):
            value = str(value)
        if not isinstance(value, (str, bytes)):
            return None, AppendIssue(
                "target column '%s' requires strings, got %s"
                % (column, _describe(value)),
                row=row,
                column=column,
                condition='type',
                value=value,
                expected='string',
                actual=type(value).__name__,
                element=element,
            )
        if isinstance(value, str):
            try:
                encoded = value.encode('ascii')
            except UnicodeEncodeError:
                return None, AppendIssue(
                    "input row %s, target column '%s': string is not ASCII"
                    % (row, column),
                    row=row,
                    column=column,
                    condition='encoding',
                    value=value,
                    element=element,
                )
        else:
            encoded = value
        limit = target.max_length if target.is_variable else target.width
        if limit is not None and limit > 0 and len(encoded) > limit:
            return None, AppendIssue(
                "input row %s, target column '%s': string length %d "
                "exceeds maximum %d"
                % (row, column, len(encoded), limit),
                row=row,
                column=column,
                condition='length',
                value=value,
                expected=limit,
                actual=len(encoded),
                element=element,
            )
        return encoded, None

    return None, AppendIssue(
        "target column '%s' has unsupported dtype %r"
        % (column, target.dtype),
        row=row,
        column=column,
        condition='type',
        value=value,
    )


def _apply_missing_scalar(spec, target, row, value):
    markers = _marker_list(spec.missing)
    if _is_marker(value, markers):
        replacement = _replacement(spec, target, 'missing')
        return replacement, 'missing'
    if _is_absent_value(value):
        replacement = _replacement(spec, target, 'absent')
        return replacement, 'absent'
    return value, None


def _replacement(spec, target, kind):
    if kind == 'missing':
        if spec.missing_fill is not _UNSET:
            return spec.missing_fill
        if spec.fill is not _UNSET:
            return spec.fill
    else:
        if spec.fill is not _UNSET:
            return spec.fill
        if spec.missing_fill is not _UNSET:
            return spec.missing_fill
    if kind == 'absent_column':
        return _UNSET
    if target.dtype.kind in ('f', 'c'):
        return np.nan
    return _UNSET


def _fill_scalar_value(spec, target, row, kind):
    replacement = _replacement(spec, target, kind)
    if replacement is _UNSET:
        return None, AppendIssue(
            "input row %s, target column '%s' has %s data but no fill "
            "value was supplied" % (row, target.name, kind),
            row=row,
            column=target.name,
            condition='missing',
            element=None,
        )
    return replacement, None


def _normalise_axes(axes, target, row):
    if axes is None:
        return None, None
    if isinstance(axes, dict):
        # A mapping of target axis -> source axis is exactly the transpose
        # order used below, but it may be written with string keys.
        try:
            order = [None] * len(axes)
            for key, value in axes.items():
                order[int(key)] = int(value)
        except Exception:
            return None, AppendIssue(
                "input row %s, target column '%s': invalid axis mapping %r"
                % (row, target.name, axes),
                row=row,
                column=target.name,
                condition='axis_order',
                value=axes,
            )
        if any(item is None for item in order):
            return None, AppendIssue(
                "input row %s, target column '%s': axis mapping %r does "
                "not cover every target axis"
                % (row, target.name, axes),
                row=row,
                column=target.name,
                condition='axis_order',
                value=axes,
            )
        axes = tuple(order)
    try:
        axes = tuple(int(item) for item in axes)
    except Exception:
        return None, AppendIssue(
            "input row %s, target column '%s': invalid axis mapping %r"
            % (row, target.name, axes),
            row=row,
            column=target.name,
            condition='axis_order',
            value=axes,
        )
    if len(axes) != len(target.shape):
        return None, AppendIssue(
            "input row %s, target column '%s': axis mapping has %d axes "
            "but target has %d"
            % (row, target.name, len(axes), len(target.shape)),
            row=row,
            column=target.name,
            condition='axis_order',
            value=axes,
            expected=len(target.shape),
            actual=len(axes),
        )
    if sorted(axes) != list(range(len(axes))):
        return None, AppendIssue(
            "input row %s, target column '%s': axis mapping %r is not a "
            "permutation" % (row, target.name, axes),
            row=row,
            column=target.name,
            condition='axis_order',
            value=axes,
        )
    return axes, None


def _apply_axis_mapping(array, axes, target):
    """Apply an axis permutation, accepting either convention.

    ``axes`` is normally the transpose order (target axis ``i`` comes from
    input axis ``axes[i]``).  Callers sometimes declare the inverse
    (input axis ``i`` goes to target axis ``axes[i]``).  When the two
    conventions give different shapes, the one matching the target is
    used.  When both match, the documented transpose order wins.
    """
    candidate = np.transpose(array, axes)
    if candidate.shape == target.shape:
        return candidate, None
    inverse = [0] * len(axes)
    for index, value in enumerate(axes):
        inverse[value] = index
    alternative = np.transpose(array, tuple(inverse))
    if alternative.shape == target.shape:
        return alternative, None
    return candidate, None


def _shape_issue(row, target, source_shape, total_equal=False):
    message = (
        "input row %s, target column '%s': input shape %r does not match "
        "target shape %r" % (row, target.name, source_shape, target.shape)
    )
    if total_equal:
        message += (
            " (the total number of elements is the same; axis order must "
            "be declared explicitly)"
        )
    return AppendIssue(
        message,
        row=row,
        column=target.name,
        condition='shape',
        expected=target.shape,
        actual=source_shape,
    )


def _length_issue(row, target, length, limit):
    return AppendIssue(
        "input row %s, target column '%s': array length %d exceeds "
        "target length %d" % (row, target.name, length, limit),
        row=row,
        column=target.name,
        condition='length',
        expected=limit,
        actual=length,
    )


def _coerce_row(value, target, spec, row):
    """Validate and convert one row to the target layout.

    Returns ``(converted, events, issues)``.  Event kinds are ``"missing"``
    and ``"absent"``.
    """
    events = []
    issues = []
    markers = _marker_list(spec.missing)

    if target.is_scalar:
        value, kind = _apply_missing_scalar(spec, target, row, value)
        if kind is not None:
            if kind == 'missing' and value is _UNSET:
                value, issue = _fill_scalar_value(spec, target, row, kind)
                if issue is not None:
                    return None, events, [issue]
            elif kind == 'absent' and value is _UNSET:
                value, issue = _fill_scalar_value(spec, target, row, kind)
                if issue is not None:
                    return None, events, [issue]
            events.append(FillEvent(row, target.name, kind, value))
        converted, issue = _check_scalar(value, target, row=row)
        if issue is not None:
            issues.append(issue)
        return converted, events, issues

    # Array targets.
    if _is_absent_value(value):
        replacement = _replacement(spec, target, 'absent')
        if replacement is _UNSET:
            issue = AppendIssue(
                "input row %s, target column '%s' has no data but no fill "
                "value was supplied" % (row, target.name),
                row=row,
                column=target.name,
                condition='missing',
            )
            return None, events, [issue]
        events.append(FillEvent(row, target.name, 'absent', replacement))
        value = replacement

    try:
        array = np.asarray(value)
    except Exception:
        issue = _shape_issue(row, target, None)
        return None, events, [issue]

    if not target.is_variable and array.ndim == 0:
        # A scalar fill or scalar source value applies to every element of
        # the target array column.
        try:
            array = np.full(target.shape, array.item(), dtype=array.dtype)
        except Exception as exc:
            issue = AppendIssue(
                "input row %s, target column '%s': cannot broadcast value "
                "%s to shape %r: %s"
                % (row, target.name, _describe(value), target.shape, exc),
                row=row,
                column=target.name,
                condition='shape',
                expected=target.shape,
                actual=array.shape,
            )
            return None, events, [issue]
    elif target.is_variable and array.ndim == 0:
        # A variable-length column holds one element per row, so a scalar
        # is a one-element vector (or a one-character string).
        array = array.reshape(1)

    axes, axis_issue = _normalise_axes(spec.axes, target, row)
    if axis_issue is not None:
        return None, events, [axis_issue]

    if axes is not None:
        if array.ndim != len(target.shape):
            issue = AppendIssue(
                "input row %s, target column '%s': axis mapping needs a "
                "%d-dimensional input, got shape %r"
                % (row, target.name, len(target.shape), array.shape),
                row=row,
                column=target.name,
                condition='axis_order',
                expected=len(target.shape),
                actual=array.shape,
            )
            return None, events, [issue]
        array, _unused = _apply_axis_mapping(array, axes, target)

    if target.is_variable:
        if target.is_string and array.dtype.kind in ('U', 'S') and array.ndim:
            # A single string is the row value, not an array of
            # characters.
            if array.size == 1 and array.ndim == 1:
                if array.dtype.kind == 'U':
                    value = str(array[0])
                else:
                    value = bytes(array[0])
                converted, issue = _check_scalar(value, target, row=row)
                if issue is not None:
                    issues.append(issue)
                    return None, events, issues
                return converted, events, issues
        if array.ndim != 1:
            issue = _shape_issue(row, target, array.shape)
            return None, events, [issue]
        if (
            target.max_length is not None
            and target.max_length > 0
            and array.size > target.max_length
        ):
            issue = _length_issue(
                row, target, array.size, target.max_length
            )
            return None, events, [issue]
    else:
        padded = False
        if (
            len(target.shape) == 1
            and array.ndim == 1
            and array.shape != target.shape
            and array.size < target.shape[0]
        ):
            # A variable-length expression is accepted for a fixed-length
            # target as long as it fits.  The remainder is padded rather
            # than wrapping or comparing only the total element count.
            padded = True
            padded_shape = target.shape
        elif (
            len(target.shape) == 1
            and array.ndim == 1
            and array.size > target.shape[0]
        ):
            issue = _length_issue(
                row, target, array.size, target.shape[0]
            )
            return None, events, [issue]
        else:
            padded_shape = None

        if padded_shape is None and tuple(array.shape) != target.shape:
            total_equal = array.size == int(np.prod(target.shape))
            issue = _shape_issue(
                row, target, array.shape, total_equal=total_equal
            )
            return None, events, [issue]

        if padded_shape is not None:
            pad_value = _replacement(spec, target, 'absent')
            if pad_value is _UNSET:
                pad_value = 0
            try:
                padding = np.full(
                    padded_shape[0] - array.size,
                    pad_value,
                    dtype=array.dtype,
                )
                array = np.concatenate([array.reshape(-1), padding])
            except Exception:
                array = np.asarray(
                    list(array.reshape(-1))
                    + [pad_value] * (padded_shape[0] - array.size)
                )

    # Validate and replace values element by element.  The source values
    # are compared against the target bounds before any cast happens.
    flat = array.reshape(-1)
    converted = []
    replaced_count = 0
    for index, item in enumerate(flat):
        if _is_marker(item, markers):
            replacement = _replacement(spec, target, 'missing')
            if replacement is _UNSET:
                issue = AppendIssue(
                    "input row %s, target column '%s': missing value at "
                    "element %d has no fill value"
                    % (row, target.name, index),
                    row=row,
                    column=target.name,
                    condition='missing',
                    value=item,
                    element=index,
                )
                issues.append(issue)
                continue
            item = replacement
            replaced_count += 1
        elif _is_absent_value(item):
            replacement = _replacement(spec, target, 'absent')
            if replacement is _UNSET:
                issue = AppendIssue(
                    "input row %s, target column '%s': missing value at "
                    "element %d has no fill value"
                    % (row, target.name, index),
                    row=row,
                    column=target.name,
                    condition='missing',
                    value=item,
                    element=index,
                )
                issues.append(issue)
                continue
            item = replacement
            replaced_count += 1

        value_out, issue = _check_scalar(
            item, target, row=row, element=index
        )
        if issue is not None:
            issues.append(issue)
        else:
            converted.append(value_out)

    if replaced_count:
        events.append(
            FillEvent(
                row,
                target.name,
                'missing',
                _replacement(spec, target, 'missing'),
                count=replaced_count,
            )
        )

    if issues:
        return None, events, issues

    try:
        if target.is_variable:
            output = np.asarray(converted, dtype=target.dtype)
        else:
            output = np.asarray(converted, dtype=target.dtype).reshape(
                target.shape
            )
    except Exception as exc:
        issue = AppendIssue(
            "input row %s, target column '%s': cannot convert to %s: %s"
            % (row, target.name, target.dtype, exc),
            row=row,
            column=target.name,
            condition='type',
        )
        return None, events, [issue]

    return output, events, issues


def _whole_column_fill(spec, target, nrows):
    """Build a column entirely from a fill policy."""
    replacement = _replacement(spec, target, 'absent_column')
    if replacement is _UNSET:
        return None, [], [
            AppendIssue(
                "target column '%s' has no input field and no fill value"
                % target.name,
                column=target.name,
                condition='missing_column',
            )
        ]

    events = []
    issues = []
    if target.is_variable:
        dtype = object
    elif target.is_scalar:
        dtype = target.dtype
    else:
        dtype = target.dtype
    try:
        if target.is_scalar:
            output = np.empty(nrows, dtype=dtype)
            for row in range(nrows):
                value, issue = _check_scalar(
                    replacement, target, row=row
                )
                if issue is not None:
                    issues.append(issue)
                else:
                    output[row] = value
        elif target.is_variable:
            output = np.empty(nrows, dtype=object)
            for row in range(nrows):
                value = replacement
                if np.isscalar(value):
                    value = np.asarray([value])
                value, _events, row_issues = _coerce_row(
                    value, target, spec, row
                )
                if row_issues:
                    issues.extend(row_issues)
                else:
                    output[row] = value
        else:
            output = np.empty((nrows,) + target.shape, dtype=dtype)
            for row in range(nrows):
                value, _events, row_issues = _coerce_row(
                    replacement, target, spec, row
                )
                if row_issues:
                    issues.extend(row_issues)
                else:
                    output[row] = value
    except Exception as exc:
        return None, [], [
            AppendIssue(
                "target column '%s': cannot apply fill value: %s"
                % (target.name, exc),
                column=target.name,
                condition='fill',
            )
        ]

    if not issues:
        for row in range(nrows):
            events.append(
                FillEvent(row, target.name, 'absent_column', replacement)
            )
    return output, events, issues


def build_plan(hdu, data, columns=None, names=None, **kwargs):
    """
    Validate ``data`` against the target table and build a write plan.

    Returns an :class:`AppendReport`.  No data are written.
    """
    mapping = kwargs.pop('mapping', None)
    axis_order = kwargs.pop('axis_order', None)
    fill = kwargs.pop('fill', None)
    missing = kwargs.pop('missing', None)
    missing_fill = kwargs.pop('missing_fill', None)
    strict_mapping = kwargs.pop('strict_mapping', False)
    if kwargs:
        raise TypeError(
            "unexpected append validation arguments: %s"
            % ", ".join(sorted(kwargs))
        )

    targets = describe_target_columns(hdu)
    source_arrays, data_fields, nrows, source_issues = _source_arrays(
        data, columns=columns, names=names
    )
    report = AppendReport(nrows=nrows or 0)
    for issue in source_issues:
        report.add_issue(issue)

    specs, mapping_issues, ignored = _normalise_mapping(
        targets,
        data_fields,
        mapping,
        axis_order,
        fill,
        missing,
        missing_fill,
    )
    for issue in mapping_issues:
        report.add_issue(issue)
    report.ignored_sources = ignored

    if strict_mapping and ignored:
        report.add_error(
            "input fields %s are not used by any mapped target column"
            % ", ".join(repr(field) for field in ignored),
            condition='mapping',
        )

    if nrows is None:
        return report

    plan = {}
    target_order = [target.name for target in targets]
    for target in targets:
        spec = specs[target.name]
        report.columns[target.name] = ColumnReport(
            target=target.name,
            source=spec.source,
            shape=target.shape,
            axes=None if spec.axes is None else tuple(spec.axes),
            is_variable=target.is_variable,
        )
        if spec.source is None:
            output, events, issues = _whole_column_fill(spec, target, nrows)
            for issue in issues:
                report.add_issue(issue)
            if output is not None and not issues:
                plan[target.name] = output
                report.fills.extend(events)
                column = report.columns[target.name]
                column.filled_absent_column = nrows
                column.filled_cells = nrows
            continue

        source_array = source_arrays[spec.source]
        if len(source_array) != nrows:
            report.add_error(
                "input field '%s' has %d rows but the batch has %d"
                % (spec.source, len(source_array), nrows),
                column=target.name,
                condition='row_count',
                expected=nrows,
                actual=len(source_array),
            )
            continue

        if target.is_variable:
            output = np.empty(nrows, dtype=object)
        elif target.is_scalar:
            output = np.empty(nrows, dtype=target.dtype)
        else:
            output = np.empty((nrows,) + target.shape, dtype=target.dtype)

        column = report.columns[target.name]
        column.rows = nrows
        for row in range(nrows):
            try:
                raw = source_array[row]
            except Exception as exc:
                report.add_error(
                    "input row %s, target column '%s': cannot read input "
                    "value: %s" % (row, target.name, exc),
                    row=row,
                    column=target.name,
                    condition='type',
                )
                continue
            value, events, issues = _coerce_row(raw, target, spec, row)
            report.fills.extend(events)
            for event in events:
                if event.kind == 'missing':
                    column.filled_missing += 1
                    column.filled_cells += event.count
                elif event.kind == 'absent':
                    column.filled_absent += 1
                    column.filled_cells += event.count
            for issue in issues:
                report.add_issue(issue)
            if issues:
                continue
            if target.is_variable:
                output[row] = value
            else:
                output[row] = value

        if not any(issue.column == target.name for issue in report.issues):
            plan[target.name] = output

    if report.ok:
        report._plan = plan
        report._target_order = target_order
    return report


def _source_arrays(data, columns=None, names=None):
    """Extract source arrays and their row count from the accepted forms."""
    issues = []
    arrays = {}
    fields = []
    nrows = None

    if isinstance(data, dict):
        fields = list(data.keys())
        arrays = {field: data[field] for field in fields}
    elif isinstance(data, (list, tuple)):
        labels = columns if columns is not None else names
        if labels is None:
            return {}, [], None, [
                AppendIssue(
                    "you must send columns or names with a list of arrays",
                    condition='mapping',
                )
            ]
        if len(labels) != len(data):
            return {}, [], None, [
                AppendIssue(
                    "columns/names has %d entries but %d arrays were sent"
                    % (len(labels), len(data)),
                    condition='mapping',
                )
            ]
        fields = [str(label) for label in labels]
        arrays = {
            str(label): array for label, array in zip(labels, data)
        }
    else:
        if getattr(data, 'dtype', None) is None or data.dtype.fields is None:
            return {}, [], None, [
                AppendIssue(
                    "expected an array with fields or a list/dict of arrays",
                    condition='mapping',
                )
            ]
        if data.shape == ():
            return {}, [], None, [
                AppendIssue(
                    "cannot append data with shape ()",
                    condition='mapping',
                )
            ]
        fields = list(data.dtype.names or ())
        arrays = {field: data[field] for field in fields}

    for field in fields:
        array = arrays[field]
        try:
            length = len(array)
        except Exception:
            issues.append(
                AppendIssue(
                    "input field '%s' is not a row array" % field,
                    column=None,
                    condition='row_count',
                )
            )
            continue
        if nrows is None:
            nrows = length
        elif length != nrows:
            issues.append(
                AppendIssue(
                    "input field '%s' has %d rows but '%s' has %d"
                    % (field, length, fields[0], nrows),
                    column=None,
                    condition='row_count',
                    expected=nrows,
                    actual=length,
                )
            )

    return arrays, fields, nrows, issues
