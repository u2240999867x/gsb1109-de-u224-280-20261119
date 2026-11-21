from __future__ import annotations

from decimal import ROUND_HALF_UP, Decimal
from typing import Any

import pandas as pd
import pyarrow as pa
import pytest

from procurement_metering import (
    DeclaredRange,
    MeterStatus,
    ShardProof,
    finalize_merged,
    finalize_proof,
    merge_proofs,
    shard_proofs,
)

AMOUNT_SCALE = 2
WEIGHT_SCALE = 3
RESULT_SCALE = 2
DECLARED = DeclaredRange.from_precision(weight_precision=38, weighted_precision=38)
DECIMAL128_MAX = 2**127 - 1

LOT = "LOT-1"
BIG_AMOUNT_A = Decimal("2000000000000000000.00")
BIG_AMOUNT_B = Decimal("1000000000000000000.00")
BIG_WEIGHT = Decimal("1000000000000000.000")

# Shard 1: each product and the shard net product exceed decimal128/int64 widths.
SHARD1_ROWS = [
    {"batch_key": LOT, "amount": BIG_AMOUNT_A, "weight": BIG_WEIGHT},
    {"batch_key": LOT, "amount": BIG_AMOUNT_B, "weight": BIG_WEIGHT},
    {"batch_key": "LOT-OV", "amount": BIG_AMOUNT_A, "weight": BIG_WEIGHT},
]
# Shard 2: return weights cancelling shard 1's products; a zero-denominator batch.
SHARD2_ROWS = [
    {"batch_key": LOT, "amount": BIG_AMOUNT_A, "weight": -BIG_WEIGHT},
    {"batch_key": LOT, "amount": BIG_AMOUNT_B, "weight": -BIG_WEIGHT},
    {"batch_key": "LOT-ZD", "amount": Decimal("10.00"), "weight": Decimal("5.000")},
    {"batch_key": "LOT-ZD", "amount": Decimal("20.00"), "weight": Decimal("-5.000")},
]
# Shard 3: the small final payable, plus edge-case rows.
SHARD3_ROWS = [
    {"batch_key": LOT, "amount": Decimal("0.01"), "weight": Decimal("1.000")},
    {"batch_key": LOT, "amount": None, "weight": Decimal("5.000")},
    {"batch_key": LOT, "amount": Decimal("7.50"), "weight": Decimal("0.000")},
    {"batch_key": "", "amount": Decimal("2.00"), "weight": Decimal("3.000")},
    {"batch_key": "LOT-EMPTY", "amount": None, "weight": Decimal("1.000")},
    {"batch_key": "LOT-EMPTY", "amount": Decimal("5.00"), "weight": None},
]
ALL_SHARDS = [SHARD1_ROWS, SHARD2_ROWS, SHARD3_ROWS]


def build(backend: str, rows: list[dict[str, Any]]) -> Any:
    if backend == "pandas":
        return pd.DataFrame(
            {
                "batch_key": pd.Series([r["batch_key"] for r in rows], dtype=object),
                "amount": pd.Series([r["amount"] for r in rows], dtype=object),
                "weight": pd.Series([r["weight"] for r in rows], dtype=object),
            }
        )
    return pa.table(
        {
            "batch_key": pa.array([r["batch_key"] for r in rows], type=pa.string()),
            "amount": pa.array(
                [r["amount"] for r in rows], type=pa.decimal128(38, AMOUNT_SCALE)
            ),
            "weight": pa.array(
                [r["weight"] for r in rows], type=pa.decimal128(38, WEIGHT_SCALE)
            ),
        }
    )


def proofs_for(
    backend: str, rows: list[dict[str, Any]], *, include_empty_key: bool = False
) -> dict[str | None, ShardProof]:
    proofs = shard_proofs(
        build(backend, rows),
        amount_scale=AMOUNT_SCALE,
        weight_scale=WEIGHT_SCALE,
        include_empty_key=include_empty_key,
    )
    return {proof.batch_key: proof for proof in proofs}


def pipeline(
    backend: str,
    shard_rows: list[list[dict[str, Any]]],
    *,
    include_empty_key: bool = False,
) -> dict[str | None, Any]:
    proofs = [
        proof
        for rows in shard_rows
        for proof in shard_proofs(
            build(backend, rows),
            amount_scale=AMOUNT_SCALE,
            weight_scale=WEIGHT_SCALE,
            include_empty_key=include_empty_key,
        )
    ]
    return finalize_merged(
        merge_proofs(proofs), result_scale=RESULT_SCALE, declared=DECLARED
    )


def truth_meters(
    shard_rows: list[list[dict[str, Any]]], *, include_empty_key: bool = False
) -> dict[str | None, dict[str, Any]]:
    totals: dict[str | None, list[Any]] = {}
    for rows in shard_rows:
        for row in rows:
            key = row["batch_key"] or None
            if key is None and not include_empty_key:
                continue
            entry = totals.setdefault(key, [0, Decimal(0), Decimal(0)])
            amount, weight = row["amount"], row["weight"]
            if amount is None or weight is None:
                continue
            entry[0] += 1
            entry[1] += weight
            entry[2] += amount * weight
    result = {}
    for key, (count, weight_sum, weighted_sum) in totals.items():
        weight_units = int(weight_sum.scaleb(WEIGHT_SCALE))
        weighted_units = int(weighted_sum.scaleb(AMOUNT_SCALE + WEIGHT_SCALE))
        average = None
        if count == 0:
            status = MeterStatus.NO_VALID_ROWS
        elif weight_units == 0:
            status = MeterStatus.ZERO_DENOMINATOR
        elif (
            abs(weight_units) > DECLARED.max_weight_units
            or abs(weighted_units) > DECLARED.max_weighted_units
        ):
            status = MeterStatus.OVERFLOW
        else:
            status = MeterStatus.OK
            average = (weighted_sum / weight_sum).quantize(
                Decimal(1).scaleb(-RESULT_SCALE), rounding=ROUND_HALF_UP
            )
        result[key] = {
            "status": status,
            "valid_count": count,
            "weight_units": weight_units,
            "weighted_units": weighted_units,
            "average": average,
        }
    return result


@pytest.mark.parametrize("backend", ["pandas", "pyarrow"])
def test_shard1_proof_exceeds_native_decimal_width(backend: str) -> None:
    proof = proofs_for(backend, SHARD1_ROWS)[LOT]
    assert proof.valid_count == 2
    assert proof.weight_units == 2 * 10**18
    assert proof.weighted_units == 3 * 10**38
    assert DECIMAL128_MAX < 2 * 10**38
    assert proof.weighted_units > DECIMAL128_MAX
    assert proof.report() == {
        "batch_key": LOT,
        "backend": backend,
        "valid_count": 2,
        "weight_units": 2 * 10**18,
        "weighted_units": 3 * 10**38,
    }


@pytest.mark.parametrize("backend", ["pandas", "pyarrow"])
def test_merge_order_invariance(backend: str) -> None:
    p1 = proofs_for(backend, SHARD1_ROWS)[LOT]
    p2 = proofs_for(backend, SHARD2_ROWS)[LOT]
    p3 = proofs_for(backend, SHARD3_ROWS)[LOT]

    first_1_3 = merge_proofs([merge_proofs([p1, p3])[LOT], p2])[LOT]
    first_1_2 = merge_proofs([merge_proofs([p1, p2])[LOT], p3])[LOT]
    flat = merge_proofs([p3, p1, p2])[LOT]
    assert first_1_3 == first_1_2 == flat

    meter = finalize_proof(flat, result_scale=RESULT_SCALE, declared=DECLARED)
    assert meter.status is MeterStatus.OK
    assert meter.valid_count == 6
    assert meter.weight_units == 1000
    assert meter.weighted_units == 1000
    assert meter.average == Decimal("0.01")


def test_cross_backend_proofs_are_interchangeable() -> None:
    for rows in ALL_SHARDS:
        pandas_proofs = proofs_for("pandas", rows)
        arrow_proofs = proofs_for("pyarrow", rows)
        assert pandas_proofs.keys() == arrow_proofs.keys()
        for key in pandas_proofs:
            pa_proof, ar_proof = pandas_proofs[key], arrow_proofs[key]
            assert pa_proof.backend == "pandas"
            assert ar_proof.backend == "pyarrow"
            assert (
                pa_proof.valid_count,
                pa_proof.weight_units,
                pa_proof.weighted_units,
            ) == (ar_proof.valid_count, ar_proof.weight_units, ar_proof.weighted_units)

    mixed = merge_proofs(
        [
            proofs_for("pandas", SHARD1_ROWS)[LOT],
            proofs_for("pyarrow", SHARD2_ROWS)[LOT],
            proofs_for("pandas", SHARD3_ROWS)[LOT],
        ]
    )
    merged = mixed[LOT]
    assert merged.backend == "mixed"
    meter = finalize_proof(merged, result_scale=RESULT_SCALE, declared=DECLARED)
    assert meter.status is MeterStatus.OK
    assert meter.average == Decimal("0.01")


@pytest.mark.parametrize("backend", ["pandas", "pyarrow"])
@pytest.mark.parametrize("include_empty_key", [False, True])
def test_matches_full_business_truth(backend: str, include_empty_key: bool) -> None:
    meters = pipeline(backend, ALL_SHARDS, include_empty_key=include_empty_key)
    truth = truth_meters(ALL_SHARDS, include_empty_key=include_empty_key)
    assert meters.keys() == truth.keys()
    for key, meter in meters.items():
        expected = truth[key]
        assert meter.status is expected["status"]
        assert meter.valid_count == expected["valid_count"]
        assert meter.weight_units == expected["weight_units"]
        assert meter.weighted_units == expected["weighted_units"]
        assert meter.average == expected["average"]


@pytest.mark.parametrize("backend", ["pandas", "pyarrow"])
def test_zero_denominator_with_nonzero_weighted_sum(backend: str) -> None:
    meter = pipeline(backend, ALL_SHARDS)["LOT-ZD"]
    assert meter.status is MeterStatus.ZERO_DENOMINATOR
    assert meter.weight_units == 0
    assert meter.weighted_units != 0
    assert meter.average is None


@pytest.mark.parametrize("backend", ["pandas", "pyarrow"])
def test_no_valid_rows(backend: str) -> None:
    proof = proofs_for(backend, SHARD3_ROWS)["LOT-EMPTY"]
    assert proof.valid_count == 0
    assert proof.weight_units == 0
    assert proof.weighted_units == 0
    meter = pipeline(backend, ALL_SHARDS)["LOT-EMPTY"]
    assert meter.status is MeterStatus.NO_VALID_ROWS
    assert meter.average is None


@pytest.mark.parametrize("backend", ["pandas", "pyarrow"])
def test_final_overflow_still_reports_exact_totals(backend: str) -> None:
    meter = pipeline(backend, ALL_SHARDS)["LOT-OV"]
    assert meter.status is MeterStatus.OVERFLOW
    assert meter.weight_units == 10**18
    assert meter.weighted_units == 2 * 10**38
    assert meter.average is None


@pytest.mark.parametrize("backend", ["pandas", "pyarrow"])
def test_invalid_and_zero_weight_rows(backend: str) -> None:
    proof = proofs_for(backend, SHARD3_ROWS)[LOT]
    # The missing-amount row is excluded; the true zero-weight row counts
    # as valid but contributes nothing to either sum.
    assert proof.valid_count == 2
    assert proof.weight_units == 1000
    assert proof.weighted_units == 1000


@pytest.mark.parametrize("backend", ["pandas", "pyarrow"])
def test_empty_key_included_when_requested(backend: str) -> None:
    meters = pipeline(backend, ALL_SHARDS, include_empty_key=True)
    meter = meters[None]
    assert meter.status is MeterStatus.OK
    assert meter.valid_count == 1
    assert meter.weight_units == 3000
    assert meter.weighted_units == 600_000
    assert meter.average == Decimal("2.00")


@pytest.mark.parametrize("backend", ["pandas", "pyarrow"])
def test_empty_key_skipped_by_default(backend: str) -> None:
    assert None not in pipeline(backend, ALL_SHARDS)


def test_serialized_proofs_merge_without_rereading_samples() -> None:
    proofs = [
        shard_proofs(
            build("pyarrow", rows), amount_scale=AMOUNT_SCALE, weight_scale=WEIGHT_SCALE
        )
        for rows in ALL_SHARDS
    ]
    revived = [
        ShardProof.from_dict(proof.to_dict()) for shard in proofs for proof in shard
    ]
    direct = merge_proofs(proof for shard in proofs for proof in shard)
    assert merge_proofs(revived) == direct
    meter = finalize_proof(direct[LOT], result_scale=RESULT_SCALE, declared=DECLARED)
    assert meter.average == Decimal("0.01")


def test_merge_rejects_mismatched_keys() -> None:
    p1 = proofs_for("pandas", SHARD1_ROWS)[LOT]
    p2 = proofs_for("pandas", SHARD2_ROWS)["LOT-ZD"]
    with pytest.raises(ValueError, match="cannot merge"):
        p1.merge(p2)


def test_finalize_rounds_half_up_only_at_the_end() -> None:
    proof = ShardProof(
        batch_key="X",
        valid_count=1,
        weight_units=8,
        weighted_units=1,
        amount_scale=0,
        weight_scale=0,
        backend="test",
    )
    declared = DeclaredRange(max_weight_units=10**6, max_weighted_units=10**6)
    meter = finalize_proof(proof, result_scale=2, declared=declared)
    assert meter.status is MeterStatus.OK
    assert meter.average == Decimal("0.13")
