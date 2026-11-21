"""Mergeable batch-level metering for procurement sampling weighted averages.

Shards may arrive as pandas DataFrames or PyArrow Tables. Each shard is
reduced to an exact integer proof (valid row count, weight sum, weighted
amount sum) that backends produce identically and that merges in any order
without re-reading the samples. Amounts and weights are fixed-point values
with declared scales; only rows where both are valid contribute. Range
checks are deferred to finalization, after all shards have merged, so
intermediate totals may safely exceed native fixed-decimal widths.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation, localcontext
from typing import TYPE_CHECKING, Any

import narwhals as nw

if TYPE_CHECKING:
    from collections.abc import Iterable

__all__ = [
    "DeclaredRange",
    "FinalMeter",
    "MeterStatus",
    "ShardProof",
    "finalize_merged",
    "finalize_proof",
    "merge_proofs",
    "shard_proofs",
]

_FINALIZE_PRECISION = 100


class MeterStatus(enum.Enum):
    OK = "ok"
    OVERFLOW = "overflow"
    ZERO_DENOMINATOR = "zero_denominator"
    NO_VALID_ROWS = "no_valid_rows"


@dataclass(frozen=True)
class DeclaredRange:
    """Caller-declared representable range for the merged totals, in units."""

    max_weight_units: int
    max_weighted_units: int

    @classmethod
    def from_precision(
        cls, *, weight_precision: int, weighted_precision: int
    ) -> DeclaredRange:
        return cls(
            max_weight_units=10**weight_precision - 1,
            max_weighted_units=10**weighted_precision - 1,
        )


@dataclass(frozen=True)
class ShardProof:
    """Exact per-shard, per-batch proof. Merges associatively and commutatively.

    `weight_units` is the exact sum of weights in units of 10^-weight_scale.
    `weighted_units` is the exact sum of amount*weight products in units of
    10^-(amount_scale + weight_scale). Both are unbounded Python integers, so
    a single shard may legitimately exceed native decimal128/int64 widths.
    """

    batch_key: str | None
    valid_count: int
    weight_units: int
    weighted_units: int
    amount_scale: int
    weight_scale: int
    backend: str

    def merge(self, other: ShardProof) -> ShardProof:
        if (
            self.batch_key != other.batch_key
            or self.amount_scale != other.amount_scale
            or self.weight_scale != other.weight_scale
        ):
            msg = f"cannot merge proofs for {self.batch_key!r} and {other.batch_key!r}"
            raise ValueError(msg)
        return ShardProof(
            batch_key=self.batch_key,
            valid_count=self.valid_count + other.valid_count,
            weight_units=self.weight_units + other.weight_units,
            weighted_units=self.weighted_units + other.weighted_units,
            amount_scale=self.amount_scale,
            weight_scale=self.weight_scale,
            backend=self.backend if self.backend == other.backend else "mixed",
        )

    def report(self) -> dict[str, Any]:
        return {
            "batch_key": self.batch_key,
            "backend": self.backend,
            "valid_count": self.valid_count,
            "weight_units": self.weight_units,
            "weighted_units": self.weighted_units,
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_key": self.batch_key,
            "valid_count": self.valid_count,
            "weight_units": self.weight_units,
            "weighted_units": self.weighted_units,
            "amount_scale": self.amount_scale,
            "weight_scale": self.weight_scale,
            "backend": self.backend,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> ShardProof:
        return cls(
            batch_key=payload["batch_key"],
            valid_count=payload["valid_count"],
            weight_units=payload["weight_units"],
            weighted_units=payload["weighted_units"],
            amount_scale=payload["amount_scale"],
            weight_scale=payload["weight_scale"],
            backend=payload["backend"],
        )


@dataclass(frozen=True)
class FinalMeter:
    batch_key: str | None
    status: MeterStatus
    valid_count: int
    weight_units: int
    weighted_units: int
    average: Decimal | None


def _parse_decimal_text(text: str) -> Decimal | None:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return Decimal(stripped)
    except InvalidOperation:
        return None


def _to_decimal(value: Any) -> Decimal | None:
    dec: Decimal | None
    if value is None or isinstance(value, bool):
        dec = None
    elif isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, float):
        dec = Decimal(str(value)) if math.isfinite(value) else None
    elif isinstance(value, str):
        dec = _parse_decimal_text(value)
    else:
        dec = None
    return dec if dec is not None and dec.is_finite() else None


def _to_units(value: Any, scale: int) -> int | None:
    dec = _to_decimal(value)
    if dec is None:
        return None
    units = dec.scaleb(scale)
    if units != units.to_integral_value():
        return None
    return int(units)


def _normalize_key(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, float) and math.isnan(value):
        return None
    text = str(value).strip()
    return text or None


def shard_proofs(
    native_df: Any,
    *,
    amount_scale: int,
    weight_scale: int,
    batch_key_col: str = "batch_key",
    amount_col: str = "amount",
    weight_col: str = "weight",
    include_empty_key: bool = False,
) -> list[ShardProof]:
    """Reduce one pandas/PyArrow shard to exact per-batch proofs.

    Rows participate only when amount and weight are both valid fixed-point
    values at their declared scales. Rows with an empty batch key are grouped
    under `None` when `include_empty_key` is set, otherwise skipped.
    """
    nw_df = nw.from_native(native_df, eager_only=True)
    backend = str(nw_df.implementation)
    keys = nw_df.get_column(batch_key_col).to_list()
    amounts = nw_df.get_column(amount_col).to_list()
    weights = nw_df.get_column(weight_col).to_list()

    totals: dict[str | None, list[int]] = {}
    for raw_key, raw_amount, raw_weight in zip(keys, amounts, weights, strict=True):
        key = _normalize_key(raw_key)
        if key is None and not include_empty_key:
            continue
        amount_units = _to_units(raw_amount, amount_scale)
        weight_units = _to_units(raw_weight, weight_scale)
        entry = totals.setdefault(key, [0, 0, 0])
        if amount_units is None or weight_units is None:
            continue
        entry[0] += 1
        entry[1] += weight_units
        entry[2] += amount_units * weight_units

    return [
        ShardProof(
            batch_key=key,
            valid_count=count,
            weight_units=weight_units,
            weighted_units=weighted_units,
            amount_scale=amount_scale,
            weight_scale=weight_scale,
            backend=backend,
        )
        for key, (count, weight_units, weighted_units) in totals.items()
    ]


def merge_proofs(proofs: Iterable[ShardProof]) -> dict[str | None, ShardProof]:
    """Merge already-computed proofs by batch key, in any order."""
    merged: dict[str | None, ShardProof] = {}
    for proof in proofs:
        existing = merged.get(proof.batch_key)
        merged[proof.batch_key] = proof if existing is None else existing.merge(proof)
    return merged


def finalize_proof(
    proof: ShardProof, *, result_scale: int, declared: DeclaredRange
) -> FinalMeter:
    """Finalize a fully merged proof.

    The declared-range check happens only here, after all shards have merged.
    The weighted average is rounded to `result_scale` (ROUND_HALF_UP) only
    when the merged totals are representable.
    """
    status = MeterStatus.OK
    average: Decimal | None = None
    if proof.valid_count == 0:
        status = MeterStatus.NO_VALID_ROWS
    elif proof.weight_units == 0:
        status = MeterStatus.ZERO_DENOMINATOR
    elif (
        abs(proof.weight_units) > declared.max_weight_units
        or abs(proof.weighted_units) > declared.max_weighted_units
    ):
        status = MeterStatus.OVERFLOW
    else:
        quantum = Decimal(1).scaleb(-result_scale)
        with localcontext() as ctx:
            ctx.prec = _FINALIZE_PRECISION
            raw = Decimal(proof.weighted_units) / Decimal(proof.weight_units)
            average = raw.scaleb(-proof.amount_scale).quantize(
                quantum, rounding=ROUND_HALF_UP
            )
    return FinalMeter(
        batch_key=proof.batch_key,
        status=status,
        valid_count=proof.valid_count,
        weight_units=proof.weight_units,
        weighted_units=proof.weighted_units,
        average=average,
    )


def finalize_merged(
    merged: dict[str | None, ShardProof], *, result_scale: int, declared: DeclaredRange
) -> dict[str | None, FinalMeter]:
    return {
        key: finalize_proof(proof, result_scale=result_scale, declared=declared)
        for key, proof in merged.items()
    }
