"""Mergeable selection of the currently effective record per device.

Supplier device feeds arrive as *occurrences*: one observation of one business key
(a device) at one effective instant, from one source with one source rank. Backfill
batches may arrive late, in any order, and may withdraw an occurrence they previously
supplied. This module defines one selection rule and applies it identically on every
Narwhals eager backend.

Selection order
---------------
For each device, among occurrences that are not withdrawn and that are eligible under
the chosen missing-time policy, the winner is the first row under:

1. effective instant descending (the actual instant, not the local wall clock),
2. source rank descending,
3. original input sequence descending,
4. occurrence id ascending, only as a deterministic last resort.

Arrival order, merge order, and shard completion order are never ranking keys.

Missing-effective-time policies
-------------------------------
``MissingTimePolicy.RANK_LAST``
    An occurrence without an effective time sorts after every occurrence that has
    one. It can still win if it is the only remaining occurrence for its device.
``MissingTimePolicy.REJECT``
    An occurrence without an effective time is excluded outright.

The missing instant is never replaced by the current date.

Merging
-------
A ledger is a union of occurrence records and withdrawals. ``DeviceLedger.merge`` is
commutative and associative for the final state: occurrences are keyed by
``occurrence_id`` and withdrawals are a set of occurrence ids. Withdrawing an
occurrence, replaying that withdrawal, or merging the same shard twice does not change
the reconciled result.

Audit reasons
-------------
Every occurrence appears in ``decisions`` with a ``selected`` flag and one reason:

``selected`` / ``selected_missing_time``
    Chosen for its device. The second spelling means it is the only eligible
    occurrence and has no effective time.
``withdrawn``
    A withdrawal points at this exact occurrence id.
``missing_time_rejected``
    The ``reject`` policy excluded an occurrence without an effective time.
``missing_time``
    It has no effective time and lost to an occurrence that has one.
``earlier_effective_at``
    Its effective instant is earlier than the winner's.
``lower_source_rank``
    Same instant, but its source rank is lower.
``lower_input_seq``
    Same instant and rank, but its original input sequence is lower.
``occurrence_id_tiebreak``
    Every ranking key is equal; the occurrence id resolved the tie.

Examples:
    >>> from datetime import datetime, timezone
    >>> from narwhals.reconcile import DeviceLedger, Occurrence
    >>> ledger = DeviceLedger(
    ...     [
    ...         Occurrence(
    ...             "dev-1", "occ-a", datetime(2024, 1, 1, tzinfo=timezone.utc), 1, 10
    ...         ),
    ...         Occurrence(
    ...             "dev-1", "occ-b", datetime(2024, 1, 2, tzinfo=timezone.utc), 1, 11
    ...         ),
    ...     ]
    ... )
    >>> ledger.reconcile().selected.to_dict(as_series=False)["occurrence_id"]
    ['occ-b']

Notes:
    The same ``occurrence_id`` must always carry the same contents. Merging two
    different definitions of one occurrence raises
    [`ConflictingOccurrenceError`][narwhals.reconcile.ConflictingOccurrenceError]
    rather than silently picking one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from enum import Enum
from numbers import Integral
from typing import TYPE_CHECKING, Any, cast

from narwhals.dataframe import DataFrame
from narwhals.dependencies import is_narwhals_dataframe
from narwhals.functions import col, from_dict, lit, when
from narwhals.translate import from_native

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping, Sequence
    from types import ModuleType

    from typing_extensions import Self

    from narwhals._typing import IntoBackend
    from narwhals.typing import IntoDataFrame

__all__ = [
    "ConflictingOccurrenceError",
    "DeviceLedger",
    "MissingTimePolicy",
    "Occurrence",
    "ReconcileResult",
    "Withdrawal",
    "merge_ledgers",
    "reconcile",
    "reconcile_devices",
]

_EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
_DEFAULT_DEVICE = "device_id"
_DEFAULT_OCCURRENCE = "occurrence_id"
_DEFAULT_EFFECTIVE_AT = "effective_at"
_DEFAULT_SOURCE_RANK = "source_rank"
_DEFAULT_INPUT_SEQ = "input_seq"
_DEFAULT_ARRIVAL_SEQ = "arrival_seq"

_REASON_WITHDRAWN = "withdrawn"
_REASON_MISSING_REJECTED = "missing_time_rejected"
_REASON_MISSING = "missing_time"
_REASON_SELECTED = "selected"
_REASON_SELECTED_MISSING = "selected_missing_time"
_REASON_EARLIER = "earlier_effective_at"
_REASON_LOWER_RANK = "lower_source_rank"
_REASON_LOWER_SEQ = "lower_input_seq"
_REASON_DUPLICATE = "occurrence_id_tiebreak"

_CORE_COLUMNS = (
    _DEFAULT_DEVICE,
    _DEFAULT_OCCURRENCE,
    _DEFAULT_EFFECTIVE_AT,
    _DEFAULT_SOURCE_RANK,
    _DEFAULT_INPUT_SEQ,
    _DEFAULT_ARRIVAL_SEQ,
)
_AUDIT_COLUMNS = (
    "has_effective_time",
    "effective_instant_us",
    "withdrawn",
    "selected",
    "reason",
)
_INTERNAL_COLUMNS = (
    "_missing_time",
    "_instant_us",
    "_winning_occurrence",
    "_winning_instant_us",
    "_winning_source_rank",
    "_winning_input_seq",
    "_winning_has_time",
)
_RESERVED_COLUMNS = frozenset((*_CORE_COLUMNS, *_AUDIT_COLUMNS, *_INTERNAL_COLUMNS))


class MissingTimePolicy(Enum):
    """How occurrences without an effective time take part in selection."""

    RANK_LAST = "rank_last"
    REJECT = "reject"


def _coerce_policy(policy: MissingTimePolicy | str) -> MissingTimePolicy:
    if isinstance(policy, MissingTimePolicy):
        return policy
    normalized = str(policy).strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "rank_last": MissingTimePolicy.RANK_LAST,
        "last": MissingTimePolicy.RANK_LAST,
        "behind": MissingTimePolicy.RANK_LAST,
        "missing_last": MissingTimePolicy.RANK_LAST,
        "reject": MissingTimePolicy.REJECT,
        "drop": MissingTimePolicy.REJECT,
        "exclude": MissingTimePolicy.REJECT,
    }
    try:
        return aliases[normalized]
    except KeyError:
        msg = (
            "`policy` must be one of {'rank_last', 'reject'} "
            f"(or a `MissingTimePolicy`), got: {policy!r}"
        )
        raise ValueError(msg) from None


def _backend_name(backend: Any) -> Any:
    if backend is None:
        return "pandas"
    if isinstance(name := getattr(backend, "__name__", None), str):
        return name.rsplit(".", 1)[-1]
    value = getattr(backend, "value", None)
    return value if isinstance(value, str) else backend


def _as_narwhals_frame(frame: IntoDataFrame | DataFrame[Any]) -> DataFrame[Any]:
    return frame if is_narwhals_dataframe(frame) else from_native(frame, eager_only=True)


def _infer_backend(frame: IntoDataFrame | DataFrame[Any]) -> Any:
    return _as_narwhals_frame(frame).implementation


def _is_missing(value: Any) -> bool:
    if value is None:
        return True
    try:
        return bool(value != value)  # noqa: PLR0124 - NaN/NaT are never equal to themselves
    except (TypeError, ValueError):  # pragma: no cover - exotic nullable scalars
        return False


def _coerce_effective_at(value: Any) -> datetime | None:
    if _is_missing(value):
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            value = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError as exc:
            msg = f"Cannot parse `effective_at` value {value!r} as an ISO-8601 datetime"
            raise ValueError(msg) from exc
    if isinstance(value, datetime):
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)
    if isinstance(value, date):
        msg = (
            "`effective_at` must be a datetime (date-only values have no effective "
            f"instant), got: {value!r}"
        )
        raise TypeError(msg)
    msg = f"`effective_at` must be a datetime, ISO-8601 string, or None, got: {value!r}"
    raise TypeError(msg)


def _instant_us(value: datetime | None) -> int | None:
    if value is None:
        return None
    delta = value - _EPOCH
    return delta.days * 86_400_000_000 + delta.seconds * 1_000_000 + delta.microseconds


def _require_int(name: str, value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, Integral):
        msg = f"`{name}` must be an integer, got: {value!r}"
        raise TypeError(msg)
    return int(value)


@dataclass(frozen=True, slots=True)
class Occurrence:
    """One supplier observation of one device at one effective instant.

    Arguments:
        device_id: Business key.
        occurrence_id: Stable identity of this occurrence, unique across the feed.
            Withdrawals point at this id.
        effective_at: Actual effective datetime, or `None` if the source omitted it.
            Timezone-aware values are converted to UTC; naive values are interpreted
            as UTC. Missing values are never replaced by the current date.
        source_rank: Business source priority. Higher is more authoritative.
        input_seq: Original input sequence number assigned by the source. It is the
            final ranking key, so shard completion order cannot break ties.
        payload: Additional columns to carry through to the reconciliation result.
        arrival_seq: Optional informational arrival sequence. It is reported but
            never used for selection.
    """

    device_id: str
    occurrence_id: str
    effective_at: datetime | None
    source_rank: int
    input_seq: int
    payload: Mapping[str, Any] = field(default_factory=dict)
    arrival_seq: int | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.device_id, str) or not self.device_id:
            msg = f"`device_id` must be a non-empty string, got: {self.device_id!r}"
            raise TypeError(msg)
        if not isinstance(self.occurrence_id, str) or not self.occurrence_id:
            msg = (
                f"`occurrence_id` must be a non-empty string, got: {self.occurrence_id!r}"
            )
            raise TypeError(msg)
        object.__setattr__(self, "effective_at", _coerce_effective_at(self.effective_at))
        object.__setattr__(
            self, "source_rank", _require_int("source_rank", self.source_rank)
        )
        object.__setattr__(self, "input_seq", _require_int("input_seq", self.input_seq))
        if self.arrival_seq is not None:
            object.__setattr__(
                self, "arrival_seq", _require_int("arrival_seq", self.arrival_seq)
            )
        payload = dict(self.payload)
        if reserved := sorted(_RESERVED_COLUMNS.intersection(payload)):
            msg = (
                f"`payload` keys collide with reserved reconciliation columns: {reserved}"
            )
            raise ValueError(msg)
        object.__setattr__(self, "payload", payload)

    @property
    def instant_us(self) -> int | None:
        """Effective instant as integer microseconds since the Unix epoch."""
        return _instant_us(self.effective_at)


@dataclass(frozen=True, slots=True)
class Withdrawal:
    """A retraction that applies to exactly one occurrence id."""

    occurrence_id: str

    def __post_init__(self) -> None:
        if not isinstance(self.occurrence_id, str) or not self.occurrence_id:
            msg = (
                f"`occurrence_id` must be a non-empty string, got: {self.occurrence_id!r}"
            )
            raise TypeError(msg)


class ConflictingOccurrenceError(ValueError):
    """Raised when one occurrence id is merged with different contents."""


@dataclass(frozen=True, slots=True)
class ReconcileResult:
    """Reconciliation of one merged ledger under one missing-time policy.

    Attributes:
        selected: One row per device with a selected occurrence.
        excluded: Every occurrence that was not selected, with its reason.
        decisions: Every occurrence, selected or not, with `selected` and `reason`.
        policy: The policy used for this reconciliation.
        dangling_withdrawals: Withdrawn occurrence ids that never appeared.
    """

    selected: DataFrame[Any]
    excluded: DataFrame[Any]
    decisions: DataFrame[Any]
    policy: MissingTimePolicy
    dangling_withdrawals: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, dict[str, list[Any]]]:
        """Return all three result frames as column-oriented dictionaries."""
        return {
            "selected": self.selected.to_dict(as_series=False),
            "excluded": self.excluded.to_dict(as_series=False),
            "decisions": self.decisions.to_dict(as_series=False),
        }

    def to_native(self) -> dict[str, Any]:
        """Return all three result frames as native backend frames."""
        return {
            "selected": self.selected.to_native(),
            "excluded": self.excluded.to_native(),
            "decisions": self.decisions.to_native(),
        }


def _payload_columns(records: Sequence[Occurrence]) -> list[str]:
    columns: set[str] = set()
    for record in records:
        columns.update(record.payload)
    return sorted(columns)


def _records_to_data(
    records: Sequence[Occurrence],
    *,
    payload_columns: Sequence[str],
    withdrawn: Mapping[str, bool],
) -> dict[str, list[Any]]:
    data: dict[str, list[Any]] = {
        _DEFAULT_DEVICE: [record.device_id for record in records],
        _DEFAULT_OCCURRENCE: [record.occurrence_id for record in records],
        _DEFAULT_EFFECTIVE_AT: [record.effective_at for record in records],
        _DEFAULT_SOURCE_RANK: [record.source_rank for record in records],
        _DEFAULT_INPUT_SEQ: [record.input_seq for record in records],
        _DEFAULT_ARRIVAL_SEQ: [
            None if record.arrival_seq is None else float(record.arrival_seq)
            for record in records
        ],
        "has_effective_time": [record.effective_at is not None for record in records],
        "effective_instant_us": [record.instant_us for record in records],
        "withdrawn": [withdrawn[record.occurrence_id] for record in records],
        "selected": [False] * len(records),
        "reason": [""] * len(records),
    }
    for name in payload_columns:
        data[name] = [record.payload.get(name) for record in records]
    return data


def _schema(payload_columns: Sequence[str]) -> dict[str, Any]:
    from narwhals import dtypes

    schema: dict[str, Any] = {
        _DEFAULT_DEVICE: dtypes.String,
        _DEFAULT_OCCURRENCE: dtypes.String,
        _DEFAULT_EFFECTIVE_AT: dtypes.Datetime("us", "UTC"),
        _DEFAULT_SOURCE_RANK: dtypes.Int64,
        _DEFAULT_INPUT_SEQ: dtypes.Int64,
        _DEFAULT_ARRIVAL_SEQ: dtypes.Float64,
        "has_effective_time": dtypes.Boolean,
        "effective_instant_us": dtypes.Float64,
        "withdrawn": dtypes.Boolean,
        "selected": dtypes.Boolean,
        "reason": dtypes.String,
    }
    for name in payload_columns:
        schema[name] = None
    return schema


def _instant_expr() -> Any:
    from narwhals import dtypes

    return (
        col(_DEFAULT_EFFECTIVE_AT).cast(dtypes.Datetime("us", "UTC")).dt.timestamp("us")
    )


def _build_frame(
    records: Sequence[Occurrence],
    *,
    payload_columns: Sequence[str],
    withdrawn_ids: set[str],
    backend: Any,
) -> DataFrame[Any]:
    withdrawn = {
        record.occurrence_id: record.occurrence_id in withdrawn_ids for record in records
    }
    data = _records_to_data(records, payload_columns=payload_columns, withdrawn=withdrawn)
    return from_dict(data, schema=_schema(payload_columns), backend=backend)


def _select_winners(
    frame: DataFrame[Any], *, policy: MissingTimePolicy
) -> DataFrame[Any]:
    from narwhals import dtypes

    eligible = frame.filter(~col("withdrawn"))
    if policy is MissingTimePolicy.REJECT:
        eligible = eligible.filter(~col(_DEFAULT_EFFECTIVE_AT).is_null())
    ranked = (
        eligible.with_columns(
            _instant_us=_instant_expr().fill_null(0).cast(dtypes.Float64),
            _missing_time=col(_DEFAULT_EFFECTIVE_AT).is_null().cast(dtypes.Int8),
        )
        .sort(
            [
                _DEFAULT_DEVICE,
                "_missing_time",
                "_instant_us",
                _DEFAULT_SOURCE_RANK,
                _DEFAULT_INPUT_SEQ,
                _DEFAULT_OCCURRENCE,
            ],
            descending=[False, False, True, True, True, False],
            nulls_last=True,
        )
        .with_row_index("_global_position")
        .with_columns(
            _is_first=col("_global_position")
            == col("_global_position").min().over(_DEFAULT_DEVICE)
        )
    )
    return ranked.filter(col("_is_first")).select(
        _DEFAULT_DEVICE,
        col(_DEFAULT_OCCURRENCE).alias("_winning_occurrence"),
        col("effective_instant_us").alias("_winning_instant_us"),
        col("has_effective_time").alias("_winning_has_time"),
        col(_DEFAULT_SOURCE_RANK).alias("_winning_source_rank"),
        col(_DEFAULT_INPUT_SEQ).alias("_winning_input_seq"),
    )


def _decisions(
    frame: DataFrame[Any], *, policy: MissingTimePolicy, payload_columns: Sequence[str]
) -> DataFrame[Any]:
    from narwhals import dtypes

    frame = frame.with_columns(
        _instant_us=_instant_expr().fill_null(0).cast(dtypes.Float64),
        _missing_time=col(_DEFAULT_EFFECTIVE_AT).is_null().cast(dtypes.Int8),
        effective_instant_us=_instant_expr().cast(dtypes.Float64),
    )
    winners = _select_winners(frame, policy=policy)
    decisions = frame.join(winners, on=_DEFAULT_DEVICE, how="left")
    selected_expr = (~col("_winning_occurrence").is_null()) & (
        col(_DEFAULT_OCCURRENCE) == col("_winning_occurrence")
    )
    # A record without a time loses to a timed winner ("missing_time"), but two
    # timeless records are ranked by the remaining keys like any other pair.
    winners_are_timed = col("_winning_has_time").fill_null(False)
    decisions = decisions.with_columns(
        selected=selected_expr,
        reason=(
            when(col("withdrawn"))
            .then(lit(_REASON_WITHDRAWN))
            .when((col("_missing_time") == 1) & lit(policy is MissingTimePolicy.REJECT))
            .then(lit(_REASON_MISSING_REJECTED))
            .when(selected_expr & (col("_missing_time") == 0))
            .then(lit(_REASON_SELECTED))
            .when(selected_expr)
            .then(lit(_REASON_SELECTED_MISSING))
            .when((col("_missing_time") == 1) & winners_are_timed)
            .then(lit(_REASON_MISSING))
            .when(col("_instant_us") < col("_winning_instant_us"))
            .then(lit(_REASON_EARLIER))
            .when(col(_DEFAULT_SOURCE_RANK) < col("_winning_source_rank"))
            .then(lit(_REASON_LOWER_RANK))
            .when(col(_DEFAULT_INPUT_SEQ) < col("_winning_input_seq"))
            .then(lit(_REASON_LOWER_SEQ))
            .otherwise(lit(_REASON_DUPLICATE))
        ),
    )
    present = [name for name in _INTERNAL_COLUMNS if name in decisions.columns]
    decisions = decisions.drop(*present)
    ordered = [
        _DEFAULT_DEVICE,
        _DEFAULT_OCCURRENCE,
        _DEFAULT_EFFECTIVE_AT,
        _DEFAULT_SOURCE_RANK,
        _DEFAULT_INPUT_SEQ,
        _DEFAULT_ARRIVAL_SEQ,
        "has_effective_time",
        "effective_instant_us",
        "withdrawn",
        "selected",
        "reason",
        *payload_columns,
    ]
    return decisions.select(*ordered).sort([_DEFAULT_DEVICE, _DEFAULT_OCCURRENCE])


class DeviceLedger:
    """An immutable, mergeable set of occurrences plus occurrence-scoped withdrawals.

    Construct an empty ledger, add records/withdrawals, and merge ledgers in any order.
    Reconciliation always reads the union, so incremental composition and a full
    replay of the same records produce identical decisions.
    """

    __slots__ = ("_backend", "_policy", "_records", "_withdrawals")

    def __init__(
        self,
        records: Iterable[Occurrence] = (),
        withdrawals: Iterable[str | Withdrawal] = (),
        *,
        backend: IntoBackend[Any] | ModuleType | str | None = "pandas",
        policy: MissingTimePolicy | str = MissingTimePolicy.RANK_LAST,
    ) -> None:
        self._backend = _backend_name(backend)
        self._policy = _coerce_policy(policy)
        self._records: dict[str, Occurrence] = {}
        self._withdrawals: dict[str, None] = {}
        self._insert_records(records)
        self._insert_withdrawals(withdrawals)

    def _insert_records(self, records: Iterable[Occurrence]) -> None:
        for record in records:
            if not isinstance(record, Occurrence):
                msg = f"Expected `Occurrence`, got: {type(record)!r}"
                raise TypeError(msg)
            existing = self._records.get(record.occurrence_id)
            if existing is not None and existing != record:
                msg = (
                    "Conflicting definitions for occurrence "
                    f"{record.occurrence_id!r}: {existing!r} != {record!r}"
                )
                raise ConflictingOccurrenceError(msg)
            self._records[record.occurrence_id] = record

    def _insert_withdrawals(self, withdrawals: Iterable[str | Withdrawal]) -> None:
        for withdrawal in withdrawals:
            if isinstance(withdrawal, Withdrawal):
                occurrence_id = withdrawal.occurrence_id
            elif isinstance(withdrawal, str):
                occurrence_id = withdrawal
            else:
                msg = f"Expected `str` or `Withdrawal`, got: {type(withdrawal)!r}"
                raise TypeError(msg)
            if not occurrence_id:
                msg = "Withdrawal `occurrence_id` must be non-empty"
                raise ValueError(msg)
            self._withdrawals[occurrence_id] = None

    @classmethod
    def from_frame(
        cls,
        frame: IntoDataFrame | DataFrame[Any],
        *,
        backend: IntoBackend[Any] | ModuleType | str | None = None,
        policy: MissingTimePolicy | str = MissingTimePolicy.RANK_LAST,
        device_id: str = _DEFAULT_DEVICE,
        occurrence_id: str = _DEFAULT_OCCURRENCE,
        effective_at: str = _DEFAULT_EFFECTIVE_AT,
        source_rank: str = _DEFAULT_SOURCE_RANK,
        input_seq: str = _DEFAULT_INPUT_SEQ,
        arrival_seq: str | None = _DEFAULT_ARRIVAL_SEQ,
    ) -> Self:
        """Build a ledger from an eager frame on any Narwhals backend."""
        frame_backend = _infer_backend(frame)
        return cls(
            records_from_frame(
                frame,
                device_id=device_id,
                occurrence_id=occurrence_id,
                effective_at=effective_at,
                source_rank=source_rank,
                input_seq=input_seq,
                arrival_seq=arrival_seq,
            ),
            backend=frame_backend if backend is None else backend,
            policy=policy,
        )

    @property
    def backend(self) -> Any:
        """Backend used when materializing reconciliation result frames."""
        return self._backend

    @property
    def policy(self) -> MissingTimePolicy:
        """Default missing-time policy."""
        return self._policy

    @property
    def records(self) -> tuple[Occurrence, ...]:
        """Occurrences in deterministic identity order."""
        return tuple(self._records[key] for key in sorted(self._records))

    @property
    def withdrawals(self) -> tuple[Withdrawal, ...]:
        """Withdrawals in deterministic identity order."""
        return tuple(Withdrawal(key) for key in sorted(self._withdrawals))

    def __len__(self) -> int:
        return len(self._records)

    def __bool__(self) -> bool:
        return bool(self._records or self._withdrawals)

    def add_records(self, records: Iterable[Occurrence]) -> Self:
        """Return a ledger with `records` unioned into this one."""
        result = self.copy()
        result._insert_records(records)
        return result

    def add_frame(
        self,
        frame: IntoDataFrame | DataFrame[Any],
        *,
        device_id: str = _DEFAULT_DEVICE,
        occurrence_id: str = _DEFAULT_OCCURRENCE,
        effective_at: str = _DEFAULT_EFFECTIVE_AT,
        source_rank: str = _DEFAULT_SOURCE_RANK,
        input_seq: str = _DEFAULT_INPUT_SEQ,
        arrival_seq: str | None = _DEFAULT_ARRIVAL_SEQ,
    ) -> Self:
        """Return a ledger with all occurrences from `frame` unioned into this one."""
        return self.add_records(
            records_from_frame(
                frame,
                device_id=device_id,
                occurrence_id=occurrence_id,
                effective_at=effective_at,
                source_rank=source_rank,
                input_seq=input_seq,
                arrival_seq=arrival_seq,
            )
        )

    def add_withdrawals(self, withdrawals: Iterable[str | Withdrawal]) -> Self:
        """Return a ledger with additional occurrence-scoped withdrawals."""
        result = self.copy()
        result._insert_withdrawals(withdrawals)
        return result

    def withdraw(self, *occurrence_ids: str) -> Self:
        """Return a ledger with `occurrence_ids` withdrawn."""
        return self.add_withdrawals(occurrence_ids)

    def copy(
        self,
        *,
        backend: IntoBackend[Any] | ModuleType | str | None = None,
        policy: MissingTimePolicy | str | None = None,
    ) -> Self:
        """Copy this ledger, optionally changing its output backend or policy."""
        return type(self)(
            self.records,
            self.withdrawals,
            backend=self._backend if backend is None else backend,
            policy=self._policy if policy is None else policy,
        )

    def merge(self, *others: DeviceLedger) -> Self:
        """Return the union of this ledger and `others`, independent of merge order."""
        result = self.copy()
        for other in others:
            if not isinstance(other, DeviceLedger):
                msg = f"Expected `DeviceLedger`, got: {type(other)!r}"
                raise TypeError(msg)
            result._insert_records(other.records)
            result._insert_withdrawals(other.withdrawals)
        return result

    def __or__(self, other: DeviceLedger) -> Self:
        return self.merge(other)

    def dangling_withdrawals(self) -> tuple[str, ...]:
        """Withdrawn occurrence ids that have not appeared in the ledger."""
        return tuple(sorted(key for key in self._withdrawals if key not in self._records))

    def reconcile(self, policy: MissingTimePolicy | str | None = None) -> ReconcileResult:
        """Apply the selection rule to the current union of records."""
        active_policy = self._policy if policy is None else _coerce_policy(policy)
        return _reconcile_records(
            self.records,
            withdrawn_ids=set(self._withdrawals),
            policy=active_policy,
            backend=self._backend,
        )


def records_from_frame(
    frame: IntoDataFrame | DataFrame[Any],
    *,
    device_id: str = _DEFAULT_DEVICE,
    occurrence_id: str = _DEFAULT_OCCURRENCE,
    effective_at: str = _DEFAULT_EFFECTIVE_AT,
    source_rank: str = _DEFAULT_SOURCE_RANK,
    input_seq: str = _DEFAULT_INPUT_SEQ,
    arrival_seq: str | None = _DEFAULT_ARRIVAL_SEQ,
) -> tuple[Occurrence, ...]:
    """Extract occurrences from an eager frame without relying on row order."""
    data = _as_narwhals_frame(frame).to_dict(as_series=False)
    identifiers = [device_id, occurrence_id, effective_at, source_rank, input_seq]
    if missing := [name for name in identifiers if name not in data]:
        msg = f"Frame is missing required reconciliation column(s): {missing}"
        raise ValueError(msg)
    arrival_name = arrival_seq if arrival_seq in data else None
    payload_names = [name for name in data if name not in {*identifiers, arrival_name}]
    columns = [
        data[device_id],
        data[occurrence_id],
        data[effective_at],
        data[source_rank],
        data[input_seq],
        data[arrival_name] if arrival_name is not None else [None] * len(data[device_id]),
        *[data[name] for name in payload_names],
    ]
    records = []
    for row in zip(*columns, strict=True):
        values = list(row)
        records.append(
            Occurrence(
                device_id=values[0],
                occurrence_id=values[1],
                effective_at=values[2],
                source_rank=values[3],
                input_seq=values[4],
                arrival_seq=values[5],
                payload=dict(zip(payload_names, values[6:], strict=True)),
            )
        )
    return tuple(records)


def _reconcile_records(
    records: Sequence[Occurrence],
    *,
    withdrawn_ids: set[str],
    policy: MissingTimePolicy,
    backend: Any,
) -> ReconcileResult:
    record_ids = {record.occurrence_id for record in records}
    dangling = tuple(sorted(withdrawn_ids - record_ids))
    payload_columns = _payload_columns(records)
    frame = _build_frame(
        records,
        payload_columns=payload_columns,
        withdrawn_ids=withdrawn_ids,
        backend=backend,
    )
    decisions = _decisions(frame, policy=policy, payload_columns=payload_columns)
    selected = decisions.filter(col("selected")).sort(_DEFAULT_DEVICE)
    excluded = decisions.filter(~col("selected")).sort(
        [_DEFAULT_DEVICE, _DEFAULT_OCCURRENCE]
    )
    return ReconcileResult(
        selected=selected,
        excluded=excluded,
        decisions=decisions,
        policy=policy,
        dangling_withdrawals=dangling,
    )


def reconcile(
    records: Iterable[Occurrence | DeviceLedger] | IntoDataFrame | DataFrame[Any] = (),
    *,
    withdrawals: Iterable[str | Withdrawal] = (),
    policy: MissingTimePolicy | str = MissingTimePolicy.RANK_LAST,
    backend: IntoBackend[Any] | ModuleType | str | None = None,
    device_id: str = _DEFAULT_DEVICE,
    occurrence_id: str = _DEFAULT_OCCURRENCE,
    effective_at: str = _DEFAULT_EFFECTIVE_AT,
    source_rank: str = _DEFAULT_SOURCE_RANK,
    input_seq: str = _DEFAULT_INPUT_SEQ,
    arrival_seq: str | None = _DEFAULT_ARRIVAL_SEQ,
) -> ReconcileResult:
    """Reconcile occurrences from dataclasses, a frame, or a sequence of shards.

    `records` may be:

    - an iterable of `Occurrence`,
    - one eager frame (Narwhals or native) using the configured column names,
    - an iterable of such frames or `DeviceLedger`s, merged in the given order.
      Merge order does not affect selection or exclusion reasons.

    `withdrawals` are applied to the merged ledger, so it does not matter whether a
    withdrawal arrives before or after the occurrence it points at.
    """
    if isinstance(records, DeviceLedger):
        ledger = records.copy(backend=backend, policy=policy)
    elif isinstance(records, DataFrame) or is_narwhals_dataframe(records):
        ledger = DeviceLedger.from_frame(
            records,
            backend=backend,
            policy=policy,
            device_id=device_id,
            occurrence_id=occurrence_id,
            effective_at=effective_at,
            source_rank=source_rank,
            input_seq=input_seq,
            arrival_seq=arrival_seq,
        )
    elif isinstance(records, Occurrence):
        ledger = DeviceLedger([records], backend=backend, policy=policy)
    elif _looks_like_frame(records):
        ledger = DeviceLedger.from_frame(
            cast("IntoDataFrame", records),
            backend=backend,
            policy=policy,
            device_id=device_id,
            occurrence_id=occurrence_id,
            effective_at=effective_at,
            source_rank=source_rank,
            input_seq=input_seq,
            arrival_seq=arrival_seq,
        )
    else:
        items = tuple(records)
        frames = [
            item for item in items if not isinstance(item, (Occurrence, DeviceLedger))
        ]
        effective_backend = (
            _infer_backend(cast("IntoDataFrame", frames[0]))
            if backend is None and frames
            else backend
        )
        ledger = DeviceLedger(backend=effective_backend, policy=policy)
        for item in items:
            if isinstance(item, DeviceLedger):
                ledger = ledger.merge(item)
            elif isinstance(item, Occurrence):
                ledger = ledger.add_records([item])
            else:
                ledger = ledger.add_frame(
                    cast("IntoDataFrame", item),
                    device_id=device_id,
                    occurrence_id=occurrence_id,
                    effective_at=effective_at,
                    source_rank=source_rank,
                    input_seq=input_seq,
                    arrival_seq=arrival_seq,
                )
    return ledger.add_withdrawals(withdrawals).reconcile()


def _looks_like_frame(obj: Any) -> bool:
    return hasattr(obj, "to_dict") and hasattr(obj, "columns")


def reconcile_devices(
    records: Iterable[Occurrence | DeviceLedger] | IntoDataFrame | DataFrame[Any] = (),
    *,
    withdrawals: Iterable[str | Withdrawal] = (),
    policy: MissingTimePolicy | str = MissingTimePolicy.RANK_LAST,
    backend: IntoBackend[Any] | ModuleType | str | None = None,
    device_id: str = _DEFAULT_DEVICE,
    occurrence_id: str = _DEFAULT_OCCURRENCE,
    effective_at: str = _DEFAULT_EFFECTIVE_AT,
    source_rank: str = _DEFAULT_SOURCE_RANK,
    input_seq: str = _DEFAULT_INPUT_SEQ,
    arrival_seq: str | None = _DEFAULT_ARRIVAL_SEQ,
) -> ReconcileResult:
    """Alias for [`reconcile`][narwhals.reconcile.reconcile]."""
    return reconcile(
        records,
        withdrawals=withdrawals,
        policy=policy,
        backend=backend,
        device_id=device_id,
        occurrence_id=occurrence_id,
        effective_at=effective_at,
        source_rank=source_rank,
        input_seq=input_seq,
        arrival_seq=arrival_seq,
    )


def merge_ledgers(*ledgers: DeviceLedger, backend: Any = None) -> DeviceLedger:
    """Merge any number of ledgers into one, deterministically."""
    if not ledgers:
        return DeviceLedger(backend=backend)
    result = ledgers[0].copy(backend=backend)
    return result.merge(*ledgers[1:])
