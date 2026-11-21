"""Time-base conversion for event tables using a center contract.

Legacy event exports often use a scanner-room clock as their zero, while BIDS
event onsets are normally expressed relative to the start of each run.  A
*center time-base contract* describes a site's mapping between those clocks.
It is deliberately **not** part of the BIDS specification: it is a site-local
sidecar naming the site's custom columns and declaring the offset that applies
to each run.

The same conversion is used by :meth:`bids.layout.BIDSDataFile.get_df` and by
:func:`bids.variables.load_variables`, so a table read and the model event
variables built from it always agree.

Two rules matter for repeated imports:

* An already-converted row keeps its recorded result while its target run and
  its recorded offset still match the declaration.
* When the center corrects an offset, the result is recomputed from the
  recorded *original* (room-clock) onset.  Converted values are never
  converted a second time.

Rows that cannot be tied to one run, or that lack the evidence needed to
recompute safely, are listed separately instead of being converted.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

CONTRACT_FORMAT = 'bids-center-timebase-contract'
HANDOFF_FORMAT = 'pybids-center-timebase-handoff'
FORMAT_VERSION = 1

DEFAULT_COLUMNS = {
    'target_run': 'center_run',
    'origin_run': 'center_origin_run',
    'time_base': 'center_time_base',
    'offset': 'center_offset',
    'original_onset': 'center_original_onset',
    'converted': 'center_converted',
    'converted_onset': 'center_converted_onset',
}

DEFAULT_NATIVE_TIME_BASES = (
    'run',
    'run_relative',
    'run-relative',
    'run_zero',
    'run-zero',
    'zero',
)

DEFAULT_ROOM_TIME_BASES = (
    'room',
    'scanner_room',
    'scanner_room_entry',
    'scanner-room',
    'scanner-room-entry',
    'absolute',
)

_RUN_ENTITIES = (
    'subject',
    'session',
    'task',
    'acquisition',
    'run',
    'datatype',
    'suffix',
    'extension',
)

# Status values used in reconciliation tables.
STATUS_CONVERTED = 'converted'
STATUS_RECOMPUTED = 'recomputed'
STATUS_KEPT = 'kept'
STATUS_NATIVE = 'native_run_zero'
STATUS_MISSING = 'missing_onset'
STATUS_OTHER_RUN = 'other_run'
STATUS_QUARANTINED = 'quarantined'

_CONVERTED_STATUSES = (STATUS_CONVERTED, STATUS_RECOMPUTED, STATUS_KEPT)


class TimeBaseError(ValueError):
    """Base error raised for invalid time-base contracts or handoffs."""


class TimeBaseContractError(TimeBaseError):
    """A center time-base contract is invalid or ambiguous."""


class TimeBaseHandoffError(TimeBaseError):
    """A time-base handoff cannot be read or validated."""


def _jsonable(value: Any) -> Any:
    """Convert numpy/pandas scalars into plain JSON-compatible values."""
    if value is None:
        return None
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    if isinstance(value, float):
        return None if math.isnan(value) else value
    if isinstance(value, pd.Timestamp):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if value is pd.NA or value is pd.NaT:
        return None
    if isinstance(value, (str, int, bool)):
        return value
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def _is_missing(value: Any) -> bool:
    """Return True for scalar missing values, without treating containers as missing."""
    if value is None:
        return True
    if isinstance(value, (dict, list, tuple, np.ndarray, pd.Series, pd.DataFrame)):
        return False
    try:
        return bool(pd.isna(value))
    except (TypeError, ValueError):
        return False


def _to_float(value: Any) -> float | None:
    if _is_missing(value):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalise_value(value: Any) -> Any:
    """Normalise an entity value for comparison."""
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        value = float(value)
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        return value.strip()
    return value


def _parse_run(value: Any) -> Any:
    """Parse a run-like value from a custom column (``run-02``, ``02``, ``2``)."""
    if _is_missing(value):
        return None
    if isinstance(value, (np.integer, int)):
        return int(value)
    if isinstance(value, (np.floating, float)):
        return int(value) if float(value).is_integer() else value
    if isinstance(value, str):
        value = value.strip()
        if value.lower().startswith('run-'):
            value = value[4:]
        try:
            return int(value)
        except ValueError:
            return value
    return value


def _normalise_target(target: Any) -> dict | None:
    """Turn a target run specification into a plain entity dict."""
    if target is None:
        return None
    if hasattr(target, 'entities'):
        target = target.entities
    if isinstance(target, Mapping):
        out = {}
        for key, value in target.items():
            value = _normalise_value(value)
            if key == 'run':
                value = _parse_run(value)
            if not _is_missing(value):
                out[str(key)] = value
        return out or None
    run = _parse_run(target)
    return None if run is None else {'run': run}


def _target_run_value(target: Mapping | None) -> Any:
    if not target:
        return None
    return _normalise_value(target.get('run'))


def _same_run(a: Mapping | None, b: Mapping | None) -> bool:
    """Compare two target descriptions on the run entity only."""
    return _normalise_value(_target_run_value(a)) == _normalise_value(_target_run_value(b))


def _contradicts(origin: Mapping | None, target: Mapping | None) -> bool:
    """Return True when two run descriptions provably refer to different runs.

    Only explicit, differing values count as a contradiction.  A description
    that omits the run entity is not treated as evidence of a different run.
    """
    if not origin or not target:
        return False
    origin_run = _target_run_value(origin)
    target_run = _target_run_value(target)
    if origin_run is not None and target_run is not None and origin_run != target_run:
        return True
    # If both sides name the same run but disagree on subject/task, the row is
    # still internally inconsistent.
    for key in ('subject', 'session', 'task', 'acquisition'):
        if key in origin and key in target:
            if _normalise_value(origin[key]) != _normalise_value(target[key]):
                return True
    return False


def _same_offset(a: Any, b: Any) -> bool:
    if a is None or b is None:
        return a is None and b is None
    try:
        return math.isclose(float(a), float(b), rel_tol=0.0, abs_tol=1e-9)
    except (TypeError, ValueError):
        return a == b


def _source_matches(declared: str | None, actual: str | None) -> bool:
    if not declared:
        return True
    if not actual:
        return False
    declared = str(declared).replace('\\', '/')
    actual = str(actual).replace('\\', '/')
    if declared == actual:
        return True
    return actual.endswith('/' + declared.lstrip('./')) or Path(actual).name == Path(declared).name


def _sha256_file(path: str | Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with open(path, 'rb') as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b''):
                digest.update(chunk)
        return digest.hexdigest()
    except OSError:
        return None


def _json_default(value: Any) -> str:
    return json.dumps(_jsonable(value), sort_keys=True, default=str)


@dataclass(frozen=True)
class RunOffset:
    """A per-run offset declaration from a center contract."""

    entities: dict
    offset: float
    source: str | None = None
    sidecar: str | None = None
    time_base: str | None = None
    label: str | None = None

    def matches(self, target: Mapping | None, source: str | None) -> bool:
        """Return True when this declaration applies to the given run."""
        if not self.entities or not target:
            return False
        for key, value in self.entities.items():
            if key not in target:
                return False
            if _normalise_value(target[key]) != _normalise_value(value):
                return False
        return not (self.source and not _source_matches(self.source, source))

    def specificity(self, source: str | None) -> int:
        """Score how narrowly this declaration targets the given source/run."""
        score = len(self.entities)
        if self.source and source and _source_matches(self.source, source):
            score += 1
        return score

    def to_dict(self) -> dict:
        """Return the declaration as a JSON-compatible dict."""
        return {
            'entities': _jsonable(self.entities),
            'offset': _jsonable(self.offset),
            'source': self.source,
            'sidecar': self.sidecar,
            'time_base': self.time_base,
            'label': self.label,
        }

    @classmethod
    def from_dict(cls, data: Mapping) -> RunOffset:
        """Build a declaration from a contract's run entry."""
        if not isinstance(data, Mapping):
            raise TimeBaseContractError('Each run offset must be a mapping.')
        raw = dict(data)
        entities = raw.pop('entities', None)
        if entities is None:
            entities = {key: raw.pop(key) for key in list(raw) if key in _RUN_ENTITIES}
        if not isinstance(entities, Mapping):
            raise TimeBaseContractError("'entities' must be a mapping.")
        entities = {str(k): _normalise_value(v) for k, v in entities.items()}
        if 'run' in entities:
            entities['run'] = _parse_run(entities['run'])
        if not entities:
            raise TimeBaseContractError('Each run offset needs at least one entity.')
        if 'offset' not in raw:
            raise TimeBaseContractError("Each run offset needs an 'offset' value.")
        offset = _to_float(raw.pop('offset'))
        if offset is None:
            raise TimeBaseContractError("'offset' must be a number.")
        return cls(
            entities=entities,
            offset=offset,
            source=raw.pop('source', None),
            sidecar=raw.pop('sidecar', None),
            time_base=raw.pop('time_base', None),
            label=raw.pop('label', None),
        )


@dataclass
class OffsetMatch:
    """Result of resolving a target run against a contract."""

    run: RunOffset | None = None
    offset: float | None = None
    source: str = 'none'
    reason: str | None = None


class TimeBaseContract:
    """A center's site-local mapping between room clock and run clock.

    Parameters
    ----------
    data : dict
        Contract contents.
    path : str or pathlib.Path, optional
        Path the contract was read from.  Used as sidecar provenance and to
        compute a stable SHA-256 in handoffs.

    Notes
    -----
    Offsets are applied as ``run_onset = room_onset - offset``.  A positive
    offset therefore means the run started that many seconds after the room
    clock's zero (or, with ``"direction": "add"``, the inverse).  Per-row
    offsets in the column named by ``columns.offset`` override the run-level
    offset.  Rows whose ``time_base`` column is in ``native_time_bases`` are
    already expressed on the run clock and are never shifted.

    The contract may declare run offsets in ``runs`` (a list of
    ``{"entities": {...}, "offset": ...}`` items) or ``run_offsets`` (a
    mapping from run number to offset).  The most specific matching
    declaration wins; equally specific conflicting declarations are an
    error unless non-strict resolution is requested.

    """

    format = CONTRACT_FORMAT

    def __init__(self, data: Mapping, path: str | Path | None = None):
        if not isinstance(data, Mapping):
            raise TimeBaseContractError('A time-base contract must be a mapping.')
        self.data = deepcopy(dict(data))
        declared_format = self.data.get('format')
        if declared_format is not None and declared_format != CONTRACT_FORMAT:
            raise TimeBaseContractError(
                f'Unsupported contract format {declared_format!r}; expected {CONTRACT_FORMAT!r}.'
            )
        self.path = str(path) if path is not None else self.data.get('path')
        self.sha256 = _sha256_file(self.path) if self.path else None
        self.name = self.data.get('name')
        self.version = self.data.get('version', FORMAT_VERSION)
        self.origin = self.data.get('origin', 'scanner_room_entry')
        self.direction = self.data.get('direction', 'subtract')
        if self.direction not in ('subtract', 'add'):
            raise TimeBaseContractError("Contract 'direction' must be 'subtract' or 'add'.")

        columns = dict(DEFAULT_COLUMNS)
        columns.update(self.data.get('columns') or {})
        self.columns = columns

        native = self.data.get('native_time_bases')
        self.native_time_bases = {
            str(v).strip().lower()
            for v in (native if native is not None else DEFAULT_NATIVE_TIME_BASES)
        }
        room = self.data.get('room_time_bases')
        self.room_time_bases = {
            str(v).strip().lower() for v in (room if room is not None else DEFAULT_ROOM_TIME_BASES)
        }

        raw_runs = self.data.get('runs', self.data.get('run_offsets'))
        if raw_runs is None:
            raw_runs = []
        runs: list[RunOffset] = []
        if isinstance(raw_runs, Mapping):
            for key, value in raw_runs.items():
                if isinstance(value, Mapping):
                    spec = dict(value)
                    spec.setdefault('entities', {'run': _parse_run(key)})
                else:
                    spec = {'entities': {'run': _parse_run(key)}, 'offset': value}
                runs.append(RunOffset.from_dict(spec))
        else:
            if not isinstance(raw_runs, Iterable) or isinstance(raw_runs, (str, bytes)):
                raise TimeBaseContractError("'runs' must be a list or mapping.")
            for spec in raw_runs:
                runs.append(RunOffset.from_dict(spec))
        self.runs = runs
        self.default_offset = _to_float(self.data.get('default_offset'))

    @classmethod
    def from_file(cls, path: str | Path) -> TimeBaseContract:
        """Read a contract from a JSON file."""
        path = Path(path)
        try:
            with open(path, encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as err:
            raise TimeBaseContractError(
                f'Could not read time-base contract {path}: {err}'
            ) from err
        return cls(data, path=path)

    @classmethod
    def from_dict(cls, data: Mapping, path: str | Path | None = None) -> TimeBaseContract:
        """Build a contract from a dict, optionally attaching a source path."""
        return cls(data, path=path)

    def to_dict(self) -> dict:
        """Return the contract's original contents as a plain dict."""
        return deepcopy(self.data)

    def provenance(self) -> dict:
        """Return stable provenance information for reconciliation/handoffs."""
        return {
            'format': self.format,
            'path': self.path,
            'sha256': self.sha256,
            'name': self.name,
            'version': self.version,
            'origin': self.origin,
            'direction': self.direction,
        }

    def resolve_offset(
        self,
        target: Mapping | None,
        source: str | None = None,
        *,
        strict: bool = True,
    ) -> OffsetMatch:
        """Resolve the offset for a target run.

        The most specific matching declaration wins.  If two equally
        specific declarations disagree, the contract is ambiguous.  In
        non-strict mode an ambiguity is reported instead of raised.
        """
        target = _normalise_target(target)
        if target is None:
            if self.default_offset is not None:
                return OffsetMatch(offset=self.default_offset, source='default')
            return OffsetMatch(reason='no_target')

        matches = [run for run in self.runs if run.matches(target, source)]
        if not matches:
            if self.default_offset is not None:
                return OffsetMatch(offset=self.default_offset, source='default')
            return OffsetMatch(reason='no_offset')

        scores = [run.specificity(source) for run in matches]
        best = max(scores)
        winners = [run for run, score in zip(matches, scores) if score == best]  # noqa: B905
        offsets = {round(run.offset, 9) for run in winners}
        if len(offsets) > 1:
            if strict:
                raise TimeBaseContractError(
                    'Multiple equally specific run offsets match target '
                    f'{target!r} from source {source!r}; refusing to guess.'
                )
            return OffsetMatch(reason='ambiguous_offset')
        winner = winners[0]
        return OffsetMatch(run=winner, offset=winner.offset, source='run')

    def is_native_time_base(self, value: Any) -> bool:
        """Return True if the row's declared time base is already run-relative."""
        if _is_missing(value):
            return False
        return str(value).strip().lower() in self.native_time_bases

    def is_room_time_base(self, value: Any) -> bool:
        """Return True if the row's declared time base is the scanner-room clock."""
        if _is_missing(value):
            return False
        return str(value).strip().lower() in self.room_time_bases

    @property
    def reserved_columns(self) -> set[str]:
        """Custom columns used for provenance rather than as model variables."""
        columns = {value for value in self.columns.values() if value}
        return columns


def coerce_contract(contract: Any) -> TimeBaseContract | None:
    """Coerce a path, mapping, or contract into a :class:`TimeBaseContract`."""
    if contract is None:
        return None
    if isinstance(contract, TimeBaseContract):
        return contract
    if isinstance(contract, (str, Path)):
        return TimeBaseContract.from_file(contract)
    if isinstance(contract, Mapping):
        return TimeBaseContract.from_dict(contract)
    raise TypeError(f'Cannot interpret {type(contract).__name__} as a time-base contract.')


_RECONCILIATION_COLUMNS = [
    'row_id',
    'source_row',
    'source_file',
    'target_run',
    'target_entities',
    'origin_run',
    'raw_onset',
    'original_onset',
    'offset',
    'offset_source',
    'converted_onset',
    'duration',
    'time_base',
    'status',
    'action',
    'reason',
    'sidecar',
    'sidecar_source',
    'contract',
    'contract_sha256',
]


def _reconciliation_record(**kwargs) -> dict:
    return {column: kwargs.get(column) for column in _RECONCILIATION_COLUMNS}


def _row_get(row: pd.Series, column: str | None) -> Any:
    if not column or column not in row.index:
        return None
    return row[column]


def _row_target(
    row: pd.Series,
    contract: TimeBaseContract,
    context: Mapping | None,
) -> dict | None:
    value = _row_get(row, contract.columns.get('target_run'))
    base = dict(context) if context else {}
    if _is_missing(value):
        return base or None
    if isinstance(value, Mapping):
        row_target = _normalise_target(value) or {}
    else:
        row_target = {'run': _parse_run(value)}
    base.update(row_target)
    return base or None


def _row_origin(row: pd.Series, contract: TimeBaseContract) -> dict | None:
    value = _row_get(row, contract.columns.get('origin_run'))
    if _is_missing(value):
        return None
    if isinstance(value, Mapping):
        return _normalise_target(value)
    return {'run': _parse_run(value)}


def _truthy(value: Any) -> bool:
    if _is_missing(value):
        return False
    if isinstance(value, str):
        return value.strip().lower() in ('1', 'true', 'yes', 'y', 'converted')
    try:
        return bool(value)
    except (TypeError, ValueError):
        return False


def _apply_offset(original: float, offset: float, direction: str) -> float:
    return original + offset if direction == 'add' else original - offset


def _row_offset(row: pd.Series, contract: TimeBaseContract) -> float | None:
    return _to_float(_row_get(row, contract.columns.get('offset')))


def _resolve_row_offset(
    row: pd.Series,
    contract: TimeBaseContract,
    target: Mapping | None,
    source: str | None,
) -> tuple[OffsetMatch, str]:
    """Resolve the offset for a row, honouring a per-row override."""
    row_offset = _row_offset(row, contract)
    if row_offset is not None:
        return OffsetMatch(offset=row_offset, source='row'), 'row'
    try:
        match = contract.resolve_offset(target, source)
    except TimeBaseContractError as err:
        return OffsetMatch(reason=f'contract_error: {err}'), 'none'
    return match, match.source


def _record_common(
    contract: TimeBaseContract,
    *,
    row_id: int,
    source_row: Any,
    source_file: str | None,
    target: Mapping | None,
    origin: Mapping | None,
    duration: Any,
    time_base: Any,
    raw_onset: Any = None,
) -> dict:
    return _reconciliation_record(
        row_id=row_id,
        source_row=source_row,
        source_file=source_file,
        target_run=_target_run_value(target),
        target_entities=_jsonable(target),
        origin_run=_target_run_value(origin),
        raw_onset=_jsonable(raw_onset),
        duration=_jsonable(duration),
        time_base=_jsonable(time_base),
        sidecar=contract.path,
        sidecar_source='contract' if contract.path else None,
        contract=contract.path or contract.name,
        contract_sha256=contract.sha256,
        action='none',
    )


@dataclass
class TimeBaseImportResult:
    """Converted rows plus the audit tables produced by an import."""

    rows: pd.DataFrame
    reconciliation: pd.DataFrame
    quarantined: pd.DataFrame
    skipped: pd.DataFrame
    contract: TimeBaseContract | None = None
    runs: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    @property
    def converted_rows(self) -> pd.DataFrame:
        """Rows whose onsets were converted or whose recorded conversion was kept."""
        if self.reconciliation.empty:
            return self.rows.iloc[0:0].copy()
        mask = self.reconciliation['status'].isin(_CONVERTED_STATUSES)
        ids = set(self.reconciliation.loc[mask, 'row_id'])
        return self.rows.loc[self.rows.index.isin(ids)].copy()

    @property
    def unresolved(self) -> pd.DataFrame:
        """Rows that need review: quarantine, missing onsets, and other runs.

        Each row id appears once even when it is both accepted (e.g. via
        ``include_skipped``) and listed separately.
        """
        if self.reconciliation.empty:
            return self.quarantined.copy()
        mask = self.reconciliation['status'].isin(
            [STATUS_QUARANTINED, STATUS_MISSING, STATUS_OTHER_RUN]
        )
        ids = set(self.reconciliation.loc[mask, 'row_id'])
        parts = [frame for frame in (self.quarantined, self.skipped, self.rows) if not frame.empty]
        if not parts:
            return self.quarantined.copy()
        combined = pd.concat(parts, sort=False)
        combined = combined.loc[
            combined.index.isin(ids) & ~combined.index.duplicated(keep='first')
        ]
        rec = self.reconciliation.set_index('row_id')
        combined['timebase_status'] = [
            rec.loc[ix, 'status'] if ix in rec.index else None for ix in combined.index
        ]
        combined['timebase_reason'] = [
            rec.loc[ix, 'reason'] if ix in rec.index else None for ix in combined.index
        ]
        return combined.copy()

    def to_handoff(self) -> TimeBaseHandoff:
        """Build a portable, evidence-carrying handoff."""
        return TimeBaseHandoff.from_result(self)

    @classmethod
    def combine(cls, results: Iterable[TimeBaseImportResult]) -> TimeBaseImportResult:
        """Combine several import results into one.

        This is how a single handoff can carry the converted rows of two
        consecutive runs imported from the same shared events table.  Row ids
        are re-numbered so the combined reconciliation table stays unique.
        """
        results = [result for result in results if result is not None]
        if not results:
            return cls(pd.DataFrame(), pd.DataFrame(), pd.DataFrame(), pd.DataFrame())
        accepted = []
        quarantine = []
        skipped = []
        records = []
        row_id = 0
        contract = results[0].contract
        for result in results:
            shift = row_id
            if not result.reconciliation.empty:
                rec = result.reconciliation.copy()
                rec['row_id'] = rec['row_id'].astype(int) + shift
                records.extend(rec.to_dict('records'))
            for frame, bucket in (
                (result.rows, accepted),
                (result.quarantined, quarantine),
                (result.skipped, skipped),
            ):
                if frame.empty:
                    continue
                copied = frame.copy()
                copied.index = pd.Index(copied.index.astype(int) + shift, name='row_id')
                bucket.append(copied)
            row_id += len(result.rows) + len(result.quarantined) + len(result.skipped)
        metadata = {
            'origin': contract.origin if contract else None,
            'direction': contract.direction if contract else None,
            'combined': True,
        }
        return cls(
            rows=_combine_frames(accepted),
            reconciliation=pd.DataFrame(records, columns=_RECONCILIATION_COLUMNS),
            quarantined=_combine_frames(quarantine),
            skipped=_combine_frames(skipped),
            contract=contract,
            runs=results[0].runs,
            metadata=metadata,
        )

    def to_frame(self, *, include_provenance: bool = False) -> pd.DataFrame:
        """Return the accepted rows.

        Parameters
        ----------
        include_provenance : bool
            If True, add both generic ``timebase_*`` columns and the
            contract's declared column names, so a later import can recognize
            rows that were already converted.  The source TSV is never
            modified.

        """
        frame = self.rows.copy()
        if not include_provenance:
            return frame
        if self.reconciliation.empty:
            return frame
        rec = self.reconciliation.set_index('row_id')
        generic = {
            'timebase_status': 'status',
            'timebase_original_onset': 'original_onset',
            'timebase_offset': 'offset',
            'timebase_target_run': 'target_run',
            'timebase_source': 'source_file',
            'timebase_contract': 'contract',
            'timebase_converted_onset': 'converted_onset',
        }
        for column, rec_column in generic.items():
            frame[column] = [
                rec.loc[ix, rec_column] if ix in rec.index else None for ix in frame.index
            ]

        contract = self.contract
        if contract is not None:
            columns = contract.columns
            frame[columns['target_run']] = [
                rec.loc[ix, 'target_run'] if ix in rec.index else None for ix in frame.index
            ]
            frame[columns['origin_run']] = [
                rec.loc[ix, 'origin_run'] if ix in rec.index else None for ix in frame.index
            ]
            frame[columns['time_base']] = [
                rec.loc[ix, 'time_base'] if ix in rec.index else None for ix in frame.index
            ]
            frame[columns['offset']] = [
                rec.loc[ix, 'offset'] if ix in rec.index else None for ix in frame.index
            ]
            frame[columns['original_onset']] = [
                rec.loc[ix, 'original_onset'] if ix in rec.index else None for ix in frame.index
            ]
            frame[columns['converted_onset']] = [
                rec.loc[ix, 'converted_onset'] if ix in rec.index else None for ix in frame.index
            ]
            frame[columns['converted']] = [
                bool(rec.loc[ix, 'status'] in _CONVERTED_STATUSES) if ix in rec.index else False
                for ix in frame.index
            ]
        return frame

    def write(self, path: str | Path, *, include_provenance: bool = True) -> None:
        """Write accepted rows to a new events TSV.

        The source file is only read, never modified.  Provenance columns are
        included by default so a later import can recognize converted rows.
        """
        frame = self.to_frame(include_provenance=include_provenance)
        frame.to_csv(path, sep='\t', index=False, na_rep='n/a')


def convert_event_table(
    table: pd.DataFrame,
    contract: Any,
    *,
    target: Any = None,
    source: str | Path | None = None,
    include_skipped: bool = False,
) -> TimeBaseImportResult:
    """Convert an events table from a center clock to run-relative onsets.

    Parameters
    ----------
    table : pandas.DataFrame
        Event rows.  Must contain an ``onset`` column.  The input frame is
        never modified.
    contract : TimeBaseContract, dict, or path
        Center time-base contract.
    target : dict, int, str, or None
        The run the table is being imported for.  A scalar is treated as a
        run index.  When the table has a target-run column, its value takes
        precedence; a row that declares a different run is listed as
        ``other_run`` rather than converted with this run's offset.
    source : str or pathlib.Path, optional
        Source file path used for provenance and for contract entries that
        are restricted to a particular events file.
    include_skipped : bool
        If True, other-run rows are also included in ``rows`` unchanged.
        By default they are only listed in ``skipped``.

    Returns
    -------
    TimeBaseImportResult

    """
    contract_obj = coerce_contract(contract)
    if contract_obj is None:
        raise TimeBaseContractError('A contract is required to convert event onsets.')
    if not isinstance(table, pd.DataFrame):
        raise TypeError('table must be a pandas DataFrame.')
    if 'onset' not in table.columns:
        raise TimeBaseContractError("Event tables must contain an 'onset' column.")

    context = _normalise_target(target)
    source_str = str(source) if source is not None else None
    original = table.copy()
    converted = table.copy()
    converted['onset'] = pd.to_numeric(converted['onset'], errors='coerce').astype(float)
    onset_position = converted.columns.get_loc('onset')

    accepted_ids: list[int] = []
    skipped_ids: list[int] = []
    quarantined_ids: list[int] = []
    records: list[dict] = []

    def set_onset(position: int, value: Any) -> None:
        converted.iloc[position, onset_position] = value

    for row_id, (index, row) in enumerate(original.iterrows()):
        row_target = _row_target(row, contract_obj, context)
        origin = _row_origin(row, contract_obj)
        time_base = _row_get(row, contract_obj.columns.get('time_base'))
        rec = _record_common(
            contract_obj,
            row_id=row_id,
            source_row=_jsonable(index),
            source_file=source_str,
            target=row_target,
            origin=origin,
            duration=_row_get(row, 'duration'),
            time_base=time_base,
            raw_onset=_row_get(row, 'onset'),
        )

        # Contradictory run evidence cannot be resolved safely.
        if _contradicts(origin, row_target):
            rec.update(status=STATUS_QUARANTINED, reason='cross_run_evidence')
            quarantined_ids.append(row_id)
            records.append(rec)
            continue

        # A table imported for one run may contain rows that declare another
        # run.  They belong to that other run and must not use this offset.
        if context is not None and row_target is not None and not _same_run(context, row_target):
            rec.update(status=STATUS_OTHER_RUN, reason='declared_target_differs')
            skipped_ids.append(row_id)
            records.append(rec)
            continue

        if row_target is None:
            rec.update(status=STATUS_QUARANTINED, reason='no_target')
            quarantined_ids.append(row_id)
            records.append(rec)
            continue

        onset = _to_float(_row_get(row, 'onset'))
        already_converted = _truthy(_row_get(row, contract_obj.columns.get('converted')))
        recorded_original = _to_float(_row_get(row, contract_obj.columns.get('original_onset')))
        recorded_converted = _to_float(_row_get(row, contract_obj.columns.get('converted_onset')))
        recorded_offset = _row_offset(row, contract_obj)

        if contract_obj.is_native_time_base(time_base):
            rec.update(
                original_onset=onset if recorded_original is None else recorded_original,
                converted_onset=onset,
                status=STATUS_NATIVE,
                action='kept_native',
            )
            accepted_ids.append(row_id)
            records.append(rec)
            continue

        if already_converted:
            baseline = recorded_original
            if recorded_converted is None:
                recorded_converted = onset
        else:
            baseline = onset if recorded_original is None else recorded_original

        # A row that claims to be converted but carries neither an original
        # onset nor enough recorded evidence cannot be checked.  Listing it
        # separately is safer than treating it as a fresh conversion.
        if already_converted and baseline is None:
            rec.update(
                original_onset=None,
                converted_onset=None,
                status=STATUS_QUARANTINED,
                reason='insufficient_evidence',
                action='none',
            )
            quarantined_ids.append(row_id)
            records.append(rec)
            continue

        # A missing onset in an unconverted row stays missing.  It is never
        # replaced by a shifted value, and duration is preserved unchanged.
        if baseline is None:
            rec.update(
                original_onset=None,
                converted_onset=None,
                status=STATUS_MISSING,
                reason='missing_onset',
                action='preserved_missing',
            )
            accepted_ids.append(row_id)
            records.append(rec)
            continue

        match, offset_source = _resolve_row_offset(row, contract_obj, row_target, source_str)
        if already_converted:
            # A recorded per-row offset is evidence of the value used before,
            # not an override of the center's current declaration.  Resolve
            # the current offset from the contract so a correction takes
            # effect (and a genuine per-row declaration still wins when the
            # contract itself carries it).
            try:
                match = contract_obj.resolve_offset(row_target, source_str)
                offset_source = match.source
            except TimeBaseContractError as err:
                match = OffsetMatch(reason=f'contract_error: {err}')
                offset_source = 'none'
        offset = match.offset
        if offset is None:
            rec.update(
                original_onset=baseline,
                offset=None,
                offset_source=offset_source,
                converted_onset=None,
                status=STATUS_QUARANTINED,
                reason=match.reason or 'no_offset',
            )
            quarantined_ids.append(row_id)
            records.append(rec)
            continue

        rec.update(
            original_onset=baseline,
            offset=offset,
            offset_source=offset_source,
            sidecar=(match.run.sidecar if match.run and match.run.sidecar else contract_obj.path),
        )

        if already_converted:
            # Never subtract from an already-converted value.  If the recorded
            # offset still matches, keep the recorded result.  Otherwise the
            # center corrected the offset, so recompute from the original.
            if recorded_offset is not None and _same_offset(recorded_offset, offset):
                rec.update(
                    converted_onset=recorded_converted,
                    status=STATUS_KEPT,
                    action='kept_recorded',
                )
                set_onset(row_id, recorded_converted)
            else:
                recomputed = _apply_offset(baseline, offset, contract_obj.direction)
                rec.update(
                    converted_onset=recomputed,
                    status=STATUS_RECOMPUTED,
                    action='recomputed_from_original',
                )
                set_onset(row_id, recomputed)
        else:
            result_onset = _apply_offset(baseline, offset, contract_obj.direction)
            rec.update(
                converted_onset=result_onset,
                status=STATUS_CONVERTED,
                action='subtracted_offset',
            )
            set_onset(row_id, result_onset)

        accepted_ids.append(row_id)
        records.append(rec)

    reconciliation = pd.DataFrame(records, columns=_RECONCILIATION_COLUMNS)

    if include_skipped:
        for row_id in skipped_ids:
            if row_id not in accepted_ids:
                accepted_ids.append(row_id)
        accepted_ids = sorted(accepted_ids)

    rows = converted.iloc[accepted_ids].copy()
    rows.index = pd.Index(accepted_ids, name='row_id')

    quarantined = converted.iloc[quarantined_ids].copy()
    quarantined.index = pd.Index(quarantined_ids, name='row_id')
    skipped = converted.iloc[skipped_ids].copy()
    skipped.index = pd.Index(skipped_ids, name='row_id')

    rec_by_id = reconciliation.set_index('row_id') if not reconciliation.empty else None
    for frame in (quarantined, skipped):
        if frame.empty or rec_by_id is None:
            continue
        frame['timebase_status'] = [
            rec_by_id.loc[ix, 'status'] if ix in rec_by_id.index else None for ix in frame.index
        ]
        frame['timebase_reason'] = [
            rec_by_id.loc[ix, 'reason'] if ix in rec_by_id.index else None for ix in frame.index
        ]
        frame['timebase_source_row'] = [
            rec_by_id.loc[ix, 'source_row'] if ix in rec_by_id.index else None
            for ix in frame.index
        ]

    runs = {_json_default(run.entities): run.to_dict() for run in contract_obj.runs}
    return TimeBaseImportResult(
        rows=rows,
        reconciliation=reconciliation,
        quarantined=quarantined,
        skipped=skipped,
        contract=contract_obj,
        runs=runs,
        metadata={'origin': contract_obj.origin, 'direction': contract_obj.direction},
    )


convert_events = convert_event_table


class TimeBaseHandoff:
    """A saved conversion handoff carrying converted rows and evidence."""

    format = HANDOFF_FORMAT

    def __init__(
        self,
        contract: TimeBaseContract | None,
        rows: Iterable[Mapping] = (),
        *,
        runs: Iterable[Mapping] = (),
        quarantined: Iterable[Mapping] = (),
        skipped: Iterable[Mapping] = (),
        metadata: Mapping | None = None,
    ):
        self.contract = contract
        self.rows = [deepcopy(dict(row)) for row in rows]
        self.runs = [deepcopy(dict(run)) for run in runs]
        self.quarantined = [deepcopy(dict(row)) for row in quarantined]
        self.skipped = [deepcopy(dict(row)) for row in skipped]
        self.metadata = deepcopy(dict(metadata or {}))

    @classmethod
    def from_result(cls, result: TimeBaseImportResult) -> TimeBaseHandoff:
        """Build a handoff from a completed import."""
        contract = result.contract
        rec = result.reconciliation
        rec_by_row = (
            {int(row['row_id']): row for _, row in rec.iterrows()} if not rec.empty else {}
        )
        rows = []
        for row_id, data in result.rows.iterrows():
            record = rec_by_row.get(int(row_id))
            if record is None:
                record = _reconciliation_record(row_id=int(row_id))
            rows.append(
                {
                    'data': {str(k): _jsonable(v) for k, v in data.items()},
                    'target': _jsonable(record.get('target_entities')),
                    'origin_run': _jsonable(record.get('origin_run')),
                    'status': record.get('status'),
                    'original_onset': _jsonable(record.get('original_onset')),
                    'raw_onset': _jsonable(record.get('raw_onset')),
                    'offset': _jsonable(record.get('offset')),
                    'onset': _jsonable(record.get('converted_onset')),
                    'duration': _jsonable(record.get('duration')),
                    'time_base': record.get('time_base'),
                    'evidence': {
                        'offset_source': record.get('offset_source'),
                        'sidecar': record.get('sidecar'),
                        'sidecar_source': record.get('sidecar_source'),
                        'contract': record.get('contract'),
                        'contract_sha256': record.get('contract_sha256'),
                        'source_file': record.get('source_file'),
                        'source_row': _jsonable(record.get('source_row')),
                        'status': record.get('status'),
                    },
                }
            )
        quarantined = []
        for row_id, data in result.quarantined.iterrows():
            record = rec_by_row.get(int(row_id), {})
            quarantined.append(
                {
                    'data': {str(k): _jsonable(v) for k, v in data.items()},
                    'target': _jsonable(record.get('target_entities')),
                    'origin_run': _jsonable(record.get('origin_run')),
                    'status': STATUS_QUARANTINED,
                    'reason': record.get('reason'),
                    'original_onset': _jsonable(record.get('original_onset')),
                    'offset': _jsonable(record.get('offset')),
                    'time_base': record.get('time_base'),
                    'evidence': {
                        'sidecar': record.get('sidecar'),
                        'contract': record.get('contract'),
                        'contract_sha256': record.get('contract_sha256'),
                        'source_file': record.get('source_file'),
                        'source_row': _jsonable(record.get('source_row')),
                    },
                }
            )
        runs = [run.to_dict() for run in contract.runs] if contract is not None else []
        return cls(
            contract,
            rows,
            runs=runs,
            quarantined=quarantined,
            # Rows that declared another run are intentionally omitted: the
            # sibling import for that run carries them.  Keeping them here
            # would duplicate them when two run handoffs are combined.
            skipped=[],
            metadata={
                'created_by': 'pybids.variables.timebase',
                'format_version': FORMAT_VERSION,
                **result.metadata,
            },
        )

    @classmethod
    def from_dict(cls, data: Mapping) -> TimeBaseHandoff:
        """Build a handoff from previously serialized contents."""
        if not isinstance(data, Mapping):
            raise TimeBaseHandoffError('A handoff must be a mapping.')
        declared = data.get('format')
        if declared is not None and declared != HANDOFF_FORMAT:
            raise TimeBaseHandoffError(
                f'Unsupported handoff format {declared!r}; expected {HANDOFF_FORMAT!r}.'
            )
        contract_info = data.get('contract') or {}
        contract = None
        if isinstance(contract_info, Mapping):
            contract_data = contract_info.get('data') or contract_info.get('contract_data')
            contract_path = contract_info.get('path')
            if contract_data is not None:
                contract = TimeBaseContract.from_dict(contract_data, path=contract_path)
            elif contract_path:
                try:
                    contract = TimeBaseContract.from_file(contract_path)
                except TimeBaseContractError:
                    contract = None
        return cls(
            contract,
            data.get('rows', []),
            runs=data.get('runs', []),
            quarantined=data.get('quarantined', []),
            skipped=data.get('skipped', []),
            metadata=data.get('metadata', {}),
        )

    @classmethod
    def read(cls, path: str | Path) -> TimeBaseHandoff:
        """Read a handoff from a JSON file."""
        try:
            with open(path, encoding='utf-8') as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError) as err:
            raise TimeBaseHandoffError(f'Could not read time-base handoff {path}: {err}') from err
        return cls.from_dict(data)

    def to_dict(self) -> dict:
        """Return the handoff as a JSON-compatible dict."""
        contract_info: dict = {}
        if self.contract is not None:
            contract_info = self.contract.provenance()
            contract_info['data'] = self.contract.to_dict()
        return {
            'format': HANDOFF_FORMAT,
            'version': FORMAT_VERSION,
            'contract': contract_info,
            'runs': deepcopy(self.runs),
            'rows': deepcopy(self.rows),
            'quarantined': deepcopy(self.quarantined),
            'skipped': deepcopy(self.skipped),
            'metadata': deepcopy(self.metadata),
        }

    def write(self, path: str | Path) -> None:
        """Write the handoff to a JSON file."""
        with open(path, 'w', encoding='utf-8') as handle:
            json.dump(self.to_dict(), handle, indent=2, sort_keys=False)
            handle.write('\n')

    @classmethod
    def combine(cls, handoffs: Iterable[TimeBaseHandoff]) -> TimeBaseHandoff:
        """Merge several handoffs (e.g. one per run) into one."""
        handoffs = [handoff for handoff in handoffs if handoff is not None]
        if not handoffs:
            return cls(None)
        first = handoffs[0]
        contract = first.contract
        seen_runs: dict[str, RunOffset] = {}
        if contract is not None:
            seen_runs = {_json_default(run.entities): run for run in contract.runs}
        for handoff in handoffs[1:]:
            if handoff.contract is not None and contract is not None:
                # Combining handoffs that used different contracts for the
                # same run would silently mix incompatible offsets.
                for run in handoff.contract.runs:
                    key = _json_default(run.entities)
                    if key in seen_runs and not _same_offset(
                        seen_runs[key].offset, run.offset
                    ):
                        raise TimeBaseHandoffError(
                            f'Handoffs disagree on the offset for {run.entities!r}: '
                            f'{seen_runs[key].offset!r} versus {run.offset!r}.'
                        )
                    seen_runs.setdefault(key, run)
        rows = []
        quarantined = []
        skipped = []
        for handoff in handoffs:
            rows.extend(deepcopy(handoff.rows))
            quarantined.extend(deepcopy(handoff.quarantined))
            skipped.extend(deepcopy(handoff.skipped))
        if contract is not None:
            data = contract.to_dict()
            data['runs'] = [run.to_dict() for run in seen_runs.values()]
            data.pop('run_offsets', None)
            contract = TimeBaseContract(data, path=contract.path)
        return cls(
            contract,
            rows,
            runs=[run.to_dict() for run in seen_runs.values()] or deepcopy(first.runs),
            quarantined=quarantined,
            skipped=skipped,
            metadata=deepcopy(first.metadata),
        )

    def __len__(self) -> int:
        return len(self.rows)

    def to_frame(self, *, include_provenance: bool = True) -> pd.DataFrame:
        """Return the handoff's converted rows as one events table.

        The original (room-clock) onset is restored in ``onset`` and the
        recorded result and evidence are attached as columns, so the table
        can be combined with newly received raw rows and re-imported through
        :func:`convert_event_table`, :func:`load_variables`, or ``get_df``.
        """
        frames = []
        for row in self.rows:
            data = row.get('data', row)
            if not isinstance(data, Mapping):
                raise TimeBaseHandoffError('Each handoff row needs a mapping under "data".')
            record = dict(data)
            if row.get('original_onset') is not None:
                record['onset'] = row['original_onset']
            if not include_provenance:
                frames.append(record)
                continue
            evidence = row.get('evidence') or {}
            record.update(
                {
                    'timebase_status': row.get('status'),
                    'timebase_original_onset': row.get('original_onset'),
                    'timebase_offset': row.get('offset'),
                    'timebase_target_run': _target_run_value(_normalise_target(row.get('target'))),
                    'timebase_source': evidence.get('source_file'),
                    'timebase_contract': evidence.get('contract'),
                    'timebase_converted_onset': row.get('onset'),
                    'center_converted': row.get('status') in _CONVERTED_STATUSES,
                }
            )
            if self.contract is not None:
                columns = self.contract.columns
                record[columns['target_run']] = _target_run_value(
                    _normalise_target(row.get('target'))
                )
                record[columns['origin_run']] = _target_run_value(
                    _normalise_target(row.get('origin_run'))
                )
                record[columns['time_base']] = row.get('time_base')
                record[columns['offset']] = row.get('offset')
                record[columns['original_onset']] = row.get('original_onset')
                record[columns['converted_onset']] = row.get('onset')
                record[columns['converted']] = row.get('status') in _CONVERTED_STATUSES
            frames.append(record)
        if not frames:
            return pd.DataFrame()
        return pd.DataFrame(frames)


def _handoff_row_frame(row: Mapping, row_id: int) -> pd.DataFrame:
    data = row.get('data', row)
    if not isinstance(data, Mapping):
        raise TimeBaseHandoffError('Each handoff row needs a mapping under "data".')
    frame = pd.DataFrame([dict(data)])
    frame.index = pd.Index([row_id], name='row_id')
    for column in frame.columns:
        if frame[column].dtype == object:
            frame[column] = frame[column].where(frame[column].notna(), np.nan)
    return frame


def _reprocess_handoff_row(
    row: Mapping,
    contract: TimeBaseContract | None,
    *,
    row_id: int,
) -> tuple[pd.DataFrame, dict, str]:
    """Re-import one handoff row.

    Returns ``(frame, reconciliation_record, bucket)`` where bucket is one of
    ``accepted``, ``quarantined``, or ``skipped``.
    """
    evidence = row.get('evidence') or {}
    frame = _handoff_row_frame(row, row_id)
    target = _normalise_target(row.get('target') or evidence.get('target_run'))
    origin = _normalise_target(row.get('origin_run') or evidence.get('origin_run'))
    record = _reconciliation_record(
        row_id=row_id,
        source_row=evidence.get('source_row'),
        source_file=evidence.get('source_file'),
        target_run=_target_run_value(target),
        target_entities=_jsonable(target),
        origin_run=_target_run_value(origin),
        raw_onset=_jsonable(row.get('raw_onset')),
        duration=_jsonable(row.get('duration')),
        original_onset=_jsonable(row.get('original_onset')),
        offset=_jsonable(row.get('offset')),
        offset_source=evidence.get('offset_source'),
        converted_onset=_jsonable(row.get('onset')),
        time_base=row.get('time_base'),
        sidecar=evidence.get('sidecar') or (contract.path if contract else None),
        sidecar_source=evidence.get('sidecar_source')
        or ('contract' if contract and contract.path else None),
        contract=evidence.get('contract') or (contract.path if contract else None),
        contract_sha256=evidence.get('contract_sha256') or (contract.sha256 if contract else None),
        action='none',
    )

    # A row whose origin and declared target differ cannot be assigned one
    # run.  This catches, for example, a run-1 record labelled as run 2's
    # converted result.
    if _contradicts(origin, target):
        record.update(status=STATUS_QUARANTINED, reason='cross_run_evidence')
        return frame, record, 'quarantined'

    if target is None:
        record.update(status=STATUS_QUARANTINED, reason='no_target')
        return frame, record, 'quarantined'

    status = row.get('status')
    original_onset = _to_float(row.get('original_onset'))
    recorded_offset = _to_float(row.get('offset'))
    recorded_onset = _to_float(row.get('onset'))

    if status == STATUS_NATIVE or (
        contract is not None and contract.is_native_time_base(row.get('time_base'))
    ):
        record.update(
            status=STATUS_NATIVE,
            action='kept_native',
            converted_onset=original_onset if original_onset is not None else recorded_onset,
        )
        if record['converted_onset'] is not None:
            frame.loc[row_id, 'onset'] = record['converted_onset']
        return frame, record, 'accepted'

    if status == STATUS_MISSING:
        record.update(
            status=STATUS_MISSING,
            reason='missing_onset',
            action='preserved_missing',
            converted_onset=None,
        )
        frame.loc[row_id, 'onset'] = np.nan
        return frame, record, 'accepted'

    if status == STATUS_QUARANTINED:
        record.update(
            status=STATUS_QUARANTINED,
            reason=row.get('reason') or 'quarantined_in_handoff',
            converted_onset=None,
        )
        return frame, record, 'quarantined'

    if status == STATUS_OTHER_RUN:
        record.update(status=STATUS_OTHER_RUN, reason='other_run_in_handoff')
        return frame, record, 'skipped'

    if original_onset is None:
        # Without a verifiable original onset, a converted value cannot be
        # safely recomputed.  Do not fall back to the converted onset.
        record.update(
            status=STATUS_QUARANTINED,
            reason='insufficient_evidence',
            converted_onset=None,
        )
        return frame, record, 'quarantined'

    if contract is None:
        # Without a contract a handoff can only be replayed, and only when it
        # carries a complete recorded result.
        if recorded_onset is not None and recorded_offset is not None:
            record.update(
                status=STATUS_KEPT,
                action='kept_recorded',
                converted_onset=recorded_onset,
            )
            frame.loc[row_id, 'onset'] = recorded_onset
            return frame, record, 'accepted'
        record.update(status=STATUS_QUARANTINED, reason='no_offset')
        return frame, record, 'quarantined'

    try:
        match = contract.resolve_offset(target, evidence.get('source_file'))
    except TimeBaseContractError as err:
        record.update(
            status=STATUS_QUARANTINED, reason=f'contract_error: {err}', converted_onset=None
        )
        return frame, record, 'quarantined'

    current_offset = match.offset
    if current_offset is None:
        # No declaration for this run.  Keep a fully evidenced recorded result
        # rather than inventing an offset; otherwise list it for review.
        if recorded_onset is not None and recorded_offset is not None:
            record.update(
                status=STATUS_KEPT,
                action='kept_recorded_no_declaration',
                converted_onset=recorded_onset,
                offset=recorded_offset,
                offset_source=evidence.get('offset_source') or 'recorded',
            )
            frame.loc[row_id, 'onset'] = recorded_onset
            return frame, record, 'accepted'
        record.update(status=STATUS_QUARANTINED, reason=match.reason or 'no_offset')
        return frame, record, 'quarantined'

    record.update(
        offset=current_offset,
        offset_source=match.source,
        sidecar=(match.run.sidecar if match.run and match.run.sidecar else contract.path),
    )

    if recorded_offset is not None and _same_offset(recorded_offset, current_offset):
        if recorded_onset is None:
            record.update(status=STATUS_QUARANTINED, reason='insufficient_evidence')
            return frame, record, 'quarantined'
        # The declaration is unchanged: keep the recorded result.
        record.update(
            status=STATUS_KEPT,
            action='kept_recorded',
            converted_onset=recorded_onset,
        )
        frame.loc[row_id, 'onset'] = recorded_onset
        return frame, record, 'accepted'

    # The center corrected this run's offset (or the handoff did not record
    # one).  Recompute from the original room-clock onset, never from the
    # already-converted onset.
    recomputed = _apply_offset(original_onset, current_offset, contract.direction)
    record.update(
        status=STATUS_RECOMPUTED,
        action='recomputed_from_original',
        converted_onset=recomputed,
    )
    frame.loc[row_id, 'onset'] = recomputed
    return frame, record, 'accepted'


def import_timebase_handoff(
    handoff: TimeBaseHandoff | Mapping | str | Path,
    raw_rows: pd.DataFrame | None = None,
    *,
    contract: Any = None,
    target: Any = None,
    source: str | Path | None = None,
    include_skipped: bool = False,
) -> TimeBaseImportResult:
    """Re-import a handoff together with newly received raw event rows.

    Previously converted rows retain their recorded result when their target
    run and recorded offset still match the declaration.  When the center has
    corrected a run's offset, the row is recomputed from its recorded
    original onset.  Raw rows are converted with the same contract.

    Parameters
    ----------
    handoff : TimeBaseHandoff, dict, or path
        A handoff produced by :meth:`TimeBaseImportResult.to_handoff`.
    raw_rows : pandas.DataFrame, optional
        Newly received rows that have not yet been converted.
    contract : TimeBaseContract, dict, or path, optional
        The current contract.  If omitted, the contract embedded in the
        handoff is used.
    target : dict, int, str, or None
        Target run for raw rows that do not carry their own target column.
    source : str or pathlib.Path, optional
        Source file for raw rows, used in provenance.
    include_skipped : bool
        Whether other-run rows should be kept in ``rows`` unchanged.

    Returns
    -------
    TimeBaseImportResult

    """
    if isinstance(handoff, (str, Path)):
        handoff_obj = TimeBaseHandoff.read(handoff)
    elif isinstance(handoff, Mapping):
        handoff_obj = TimeBaseHandoff.from_dict(handoff)
    elif isinstance(handoff, TimeBaseHandoff):
        handoff_obj = handoff
    else:
        raise TypeError(f'Cannot interpret {type(handoff).__name__} as a time-base handoff.')

    contract_obj = coerce_contract(contract) if contract is not None else handoff_obj.contract
    # Without a contract, a handoff can still be replayed from the evidence it
    # carries.  New raw rows always need a contract to be converted.
    if contract_obj is None and raw_rows is not None and not raw_rows.empty:
        raise TimeBaseContractError(
            'No contract was supplied; new raw rows cannot be converted without one.'
        )

    accepted_frames: list[pd.DataFrame] = []
    quarantine_frames: list[pd.DataFrame] = []
    skipped_frames: list[pd.DataFrame] = []
    records: list[dict] = []
    row_id = 0

    for row in handoff_obj.rows:
        frame, record, bucket = _reprocess_handoff_row(row, contract_obj, row_id=row_id)
        records.append(record)
        if bucket == 'accepted':
            accepted_frames.append(frame)
        elif bucket == 'quarantined':
            frame['timebase_status'] = STATUS_QUARANTINED
            frame['timebase_reason'] = record.get('reason')
            quarantine_frames.append(frame)
        else:
            skipped_frames.append(frame)
        row_id += 1

    for row in handoff_obj.quarantined:
        frame = _handoff_row_frame(row, row_id)
        frame['timebase_status'] = STATUS_QUARANTINED
        frame['timebase_reason'] = row.get('reason')
        quarantine_frames.append(frame)
        records.append(
            _reconciliation_record(
                row_id=row_id,
                source_row=(row.get('evidence') or {}).get('source_row'),
                source_file=(row.get('evidence') or {}).get('source_file'),
                target_run=_target_run_value(_normalise_target(row.get('target'))),
                target_entities=_jsonable(_normalise_target(row.get('target'))),
                origin_run=_target_run_value(_normalise_target(row.get('origin_run'))),
                status=STATUS_QUARANTINED,
                reason=row.get('reason') or 'quarantined_in_handoff',
                action='none',
            )
        )
        row_id += 1

    for row in handoff_obj.skipped:
        data = row.get('data', row)
        frame = _handoff_row_frame(row, row_id)
        frame['timebase_status'] = STATUS_OTHER_RUN
        frame['timebase_reason'] = row.get('reason') or 'other_run_in_handoff'
        skipped_frames.append(frame)
        evidence = row.get('evidence') or {}
        records.append(
            _reconciliation_record(
                row_id=row_id,
                source_row=evidence.get('source_row'),
                source_file=evidence.get('source_file'),
                target_run=_target_run_value(_normalise_target(row.get('target'))),
                target_entities=_jsonable(_normalise_target(row.get('target'))),
                origin_run=_target_run_value(_normalise_target(row.get('origin_run'))),
                status=STATUS_OTHER_RUN,
                reason=row.get('reason') or 'other_run_in_handoff',
                action='none',
                duration=_jsonable(data.get('duration')) if isinstance(data, Mapping) else None,
            )
        )
        row_id += 1

    if raw_rows is not None:
        if not isinstance(raw_rows, pd.DataFrame):
            raise TypeError('raw_rows must be a pandas DataFrame.')
        raw_result = convert_event_table(
            raw_rows,
            contract_obj,
            target=target,
            source=source,
            include_skipped=include_skipped,
        )
        shift = row_id
        if not raw_result.reconciliation.empty:
            raw_rec = raw_result.reconciliation.copy()
            raw_rec['row_id'] = raw_rec['row_id'].astype(int) + shift
            records.extend(raw_rec.to_dict('records'))
        for frame in (raw_result.rows, raw_result.quarantined, raw_result.skipped):
            if not frame.empty:
                frame.index = pd.Index(frame.index.astype(int) + shift, name='row_id')
        accepted_frames.append(raw_result.rows)
        quarantine_frames.append(raw_result.quarantined)
        skipped_frames.append(raw_result.skipped)
        row_id += len(raw_rows)

    rows = _combine_frames(accepted_frames)
    quarantine = _combine_frames(quarantine_frames)
    skipped = _combine_frames(skipped_frames)
    reconciliation = pd.DataFrame(records, columns=_RECONCILIATION_COLUMNS)

    runs = (
        {}
        if contract_obj is None
        else {_json_default(run.entities): run.to_dict() for run in contract_obj.runs}
    )
    return TimeBaseImportResult(
        rows=rows,
        reconciliation=reconciliation,
        quarantined=quarantine,
        skipped=skipped,
        contract=contract_obj,
        runs=runs,
        metadata={
            'origin': contract_obj.origin if contract_obj else None,
            'direction': contract_obj.direction if contract_obj else None,
            'from_handoff': True,
        },
    )


def merge_handoff_table(
    handoff: TimeBaseHandoff | Mapping | str | Path,
    raw_rows: pd.DataFrame | None = None,
) -> pd.DataFrame:
    """Build one events table from a handoff's converted rows and new raw rows.

    The handoff's original (room-clock) onsets are restored in ``onset`` and
    every row keeps the provenance needed to recognize an existing conversion.
    The resulting table can be written to a *new* events TSV and then read with
    :func:`bids.variables.load_variables` or
    :meth:`bids.layout.BIDSDataFile.get_df`; both apply the current contract
    with the same rules as :func:`import_timebase_handoff`.

    The source TSV is never modified.
    """
    if isinstance(handoff, (str, Path)):
        handoff_obj = TimeBaseHandoff.read(handoff)
    elif isinstance(handoff, Mapping):
        handoff_obj = TimeBaseHandoff.from_dict(handoff)
    elif isinstance(handoff, TimeBaseHandoff):
        handoff_obj = handoff
    else:
        raise TypeError(f'Cannot interpret {type(handoff).__name__} as a time-base handoff.')

    frame = handoff_obj.to_frame(include_provenance=True)
    if raw_rows is not None:
        if not isinstance(raw_rows, pd.DataFrame):
            raise TypeError('raw_rows must be a pandas DataFrame.')
        if not frame.empty:
            raw_rows = raw_rows.reindex(columns=list(dict.fromkeys([*frame.columns, *raw_rows])))
        frame = pd.concat([frame, raw_rows], ignore_index=True, sort=False)
    return frame.reset_index(drop=True)


def _combine_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    frames = [frame for frame in frames if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame()
    combined = pd.concat(frames, sort=False)
    return combined.sort_index()


read_handoff = TimeBaseHandoff.read
write_handoff = TimeBaseHandoff.write
import_handoff = import_timebase_handoff


__all__ = [
    'CONTRACT_FORMAT',
    'HANDOFF_FORMAT',
    'FORMAT_VERSION',
    'STATUS_CONVERTED',
    'STATUS_KEPT',
    'STATUS_MISSING',
    'STATUS_NATIVE',
    'STATUS_OTHER_RUN',
    'STATUS_QUARANTINED',
    'STATUS_RECOMPUTED',
    'OffsetMatch',
    'RunOffset',
    'TimeBaseContract',
    'TimeBaseContractError',
    'TimeBaseError',
    'TimeBaseHandoff',
    'TimeBaseHandoffError',
    'TimeBaseImportResult',
    'coerce_contract',
    'convert_event_table',
    'convert_events',
    'import_handoff',
    'import_timebase_handoff',
    'merge_handoff_table',
    'read_handoff',
    'write_handoff',
]
