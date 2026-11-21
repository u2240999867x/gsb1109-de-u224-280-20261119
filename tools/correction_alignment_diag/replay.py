"""Replay the sensor-correction hand-off and localise the first divergence.

The recipes intentionally share the same raw frame and the same correction
values.  One recipe uses native pandas label arithmetic; another hands the
same data to Narwhals, which treats a ``with_columns`` operand positionally;
a third uses Polars as a no-index control.

Run from the repository root:

    .venv/bin/python tools/correction_alignment_diag/replay.py

To write the report and Markdown diagnosis alongside the script:

    .venv/bin/python tools/correction_alignment_diag/replay.py \
        --out-dir tools/correction_alignment_diag/artifacts

The script exits non-zero if any checked invariant changes.
"""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import narwhals as nw
import pandas as pd
import polars as pl

RAW_ROWS = [
    {"row_label": 4103, "business_key": "S-B", "business_time": 35, "raw": 120.0},
    {"row_label": 4101, "business_key": "S-C", "business_time": 20, "raw": 200.0},
    {"row_label": 4102, "business_key": "S-A", "business_time": 10, "raw": 100.0},
]

# Deliberately not in business-key order, and the S-B correction is absent.
CORRECTION_ROWS = [
    {
        "correction_id": "C-01",
        "row_label": 4102,
        "business_key": "S-A",
        "correction": 7.0,
    },
    {
        "correction_id": "C-03",
        "row_label": 4101,
        "business_key": "S-C",
        "correction": 3.0,
    },
    {
        "correction_id": "C-08",
        "row_label": 4103,
        "business_key": "S-B",
        "correction": None,
    },
]

BUSINESS_HORIZON = 25
VERIFIED_S_A = 7.0

# Expected values by business key at each observable stage.  These are the
# auditable values the duty log should contain, independent of row position.
TRUTH: dict[str, dict[str, dict[str, Any]]] = {
    "attach correction": {
        "correction": {"S-B": None, "S-C": 3.0, "S-A": 7.0}
    },
    "adjusted arithmetic": {
        "adjusted": {"S-B": None, "S-C": 203.0, "S-A": 107.0}
    },
    "attach adjusted": {
        "correction": {"S-B": None, "S-C": 3.0, "S-A": 7.0},
        "adjusted": {"S-B": None, "S-C": 203.0, "S-A": 107.0},
    },
    "filter business_time <= 25": {
        "correction": {"S-C": 3.0, "S-A": 7.0},
        "adjusted": {"S-C": 203.0, "S-A": 107.0},
    },
    "sort business_time": {
        "correction": {"S-C": 3.0, "S-A": 7.0},
        "adjusted": {"S-C": 203.0, "S-A": 107.0},
    },
    "patch S-A with one-row business-key Series": {
        "correction": {"S-C": 3.0, "S-A": 7.0},
        "adjusted": {"S-C": 203.0, "S-A": 107.0},
    },
    "recompute adjusted": {
        "correction": {"S-C": 3.0, "S-A": 7.0},
        "adjusted": {"S-C": 203.0, "S-A": 107.0},
    },
}


def norm(value: Any) -> Any:
    """Normalise NaN/NA to ``None`` for comparison and JSON output."""
    if value is None:
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


@dataclass
class CallRecord:
    """One observable call boundary in a recipe."""

    stage: int
    call: str
    row_labels: list[Any]
    business_keys: list[Any]
    values: dict[str, list[Any]]
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "stage": self.stage,
            "call": self.call,
            "row_labels": self.row_labels,
            "business_keys": self.business_keys,
            "values": {
                key: [norm(item) for item in items] for key, items in self.values.items()
            },
            "note": self.note,
        }


@dataclass
class Recipe:
    name: str
    calls: list[CallRecord] = field(default_factory=list)
    result: pd.DataFrame | None = None

    def record(
        self, stage: int, call: str, df: pd.DataFrame, note: str = ""
    ) -> pd.DataFrame:
        values = {
            col: df[col].tolist()
            for col in df.columns
            if col not in {"business_key", "business_time"}
        }
        self.calls.append(
            CallRecord(
                stage=stage,
                call=call,
                row_labels=list(df.index),
                business_keys=(
                    df["business_key"].tolist() if "business_key" in df else []
                ),
                values=values,
                note=note,
            )
        )
        return df

    def record_polars(
        self, stage: int, call: str, df: pl.DataFrame, note: str = ""
    ) -> pl.DataFrame:
        records = df.to_dicts()
        values = {
            key: [record[key] for record in records]
            for key in records[0]
            if key not in {"row_label", "business_key", "business_time"}
        }
        self.calls.append(
            CallRecord(
                stage=stage,
                call=call,
                row_labels=[record["row_label"] for record in records],
                business_keys=[record["business_key"] for record in records],
                values=values,
                note=note,
            )
        )
        return df

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "calls": [call.as_dict() for call in self.calls],
            "result": None if self.result is None else payload(self.result),
        }


def build_raw() -> pd.DataFrame:
    raw = pd.DataFrame(RAW_ROWS).set_index("row_label")
    return raw[["business_key", "business_time", "raw"]]


def build_correction() -> pd.Series:
    return pd.Series(
        [row["correction"] for row in CORRECTION_ROWS],
        index=pd.Index([row["row_label"] for row in CORRECTION_ROWS], name="row_label"),
        name="correction",
        dtype="float64",
    )


def build_correction_table() -> pd.DataFrame:
    return pd.DataFrame(CORRECTION_ROWS)


def payload(df: pd.DataFrame) -> list[dict[str, Any]]:
    """Canonical row payload; NaN and null become ``None``."""
    return [
        {key: norm(value) for key, value in record.items()}
        for record in df.reset_index().to_dict("records")
    ]


def native_label_recipe() -> Recipe:
    """Native pandas labels through assignment and arithmetic."""
    recipe = Recipe("native-pandas-labels")
    raw = build_raw()
    correction = build_correction()

    df = raw.copy()
    df["correction"] = correction
    recipe.record(0, "attach correction", df, "pandas __setitem__ aligns by label")

    arithmetic = df[["business_key"]].copy()
    arithmetic["adjusted"] = df["raw"] + correction
    recipe.record(
        1,
        "adjusted arithmetic",
        arithmetic,
        "df['raw'] + Series aligns by label before the sum",
    )

    df["adjusted"] = arithmetic["adjusted"]
    recipe.record(1, "attach adjusted", df)

    df = df[df["business_time"] <= BUSINESS_HORIZON]
    recipe.record(2, "filter business_time <= 25", df)

    df = df.sort_values("business_time", kind="stable")
    recipe.record(3, "sort business_time", df)

    # A one-row Series indexed by business key, assigned to a subset.  The
    # target subset's index is a row label, so this patch misses in native
    # pandas: the non-overlapping labels reindex to NaN.
    patch = pd.Series([VERIFIED_S_A], index=["S-A"], name="correction")
    mask = df["business_key"] == "S-A"
    df.loc[mask, "correction"] = patch
    recipe.record(
        4,
        "patch S-A with one-row business-key Series",
        df,
        "patch index ['S-A'] does not overlap target row label 4102",
    )

    df["adjusted"] = df["raw"] + df["correction"]
    recipe.record(5, "recompute adjusted", df)
    recipe.result = df
    return recipe


def narwhals_positional_recipe() -> Recipe:
    """The same data handed to Narwhals, which resets the operand index."""
    recipe = Recipe("narwhals-positional")
    raw = build_raw()
    correction = build_correction()

    frame = nw.from_native(raw)
    series = nw.from_native(correction, series_only=True)
    frame = frame.with_columns(correction=series)
    recipe.record(
        0,
        "attach correction",
        frame.to_native(),
        "with_columns inserts the Series positionally",
    )

    frame = frame.with_columns(adjusted=nw.col("raw") + nw.col("correction"))
    recipe.record(1, "attach adjusted", frame.to_native())

    frame = frame.filter(nw.col("business_time") <= BUSINESS_HORIZON)
    recipe.record(2, "filter business_time <= 25", frame.to_native())

    frame = frame.sort("business_time")
    recipe.record(3, "sort business_time", frame.to_native())
    recipe.result = frame.to_native()
    return recipe


def polars_positional_recipe() -> Recipe:
    """A no-index backend control; Polars has no pandas label alignment."""
    recipe = Recipe("polars-no-index")
    raw = build_raw()
    correction = build_correction()

    frame = pl.from_pandas(raw.reset_index())
    frame = frame.with_columns(
        correction=pl.Series("correction", correction.tolist(), dtype=pl.Float64)
    )
    recipe.record_polars(
        0,
        "attach correction",
        frame,
        "with_columns inserts the Series positionally; no index exists",
    )

    frame = frame.with_columns(adjusted=pl.col("raw") + pl.col("correction"))
    recipe.record_polars(1, "attach adjusted", frame)

    frame = frame.filter(pl.col("business_time") <= BUSINESS_HORIZON)
    recipe.record_polars(2, "filter business_time <= 25", frame)

    frame = frame.sort("business_time", maintain_order=True)
    recipe.record_polars(3, "sort business_time", frame)
    recipe.result = frame.to_pandas().set_index("row_label")
    return recipe


def records_by_stage(recipe: Recipe) -> dict[int, CallRecord]:
    """Keep the first call recorded at each stage."""
    out: dict[int, CallRecord] = {}
    for call in recipe.calls:
        out.setdefault(call.stage, call)
    return out


def first_divergence(left: Recipe, right: Recipe) -> dict[str, Any] | None:
    """Return the first stage whose observable row/key/value map differs."""
    left_stages = records_by_stage(left)
    right_stages = records_by_stage(right)
    for stage in sorted(set(left_stages) & set(right_stages)):
        a = left_stages[stage]
        b = right_stages[stage]
        a_keys = list(zip(a.row_labels, a.business_keys))
        b_keys = list(zip(b.row_labels, b.business_keys))
        a_values = {
            key: [norm(item) for item in items] for key, items in a.values.items()
        }
        b_values = {
            key: [norm(item) for item in items] for key, items in b.values.items()
        }
        if a_keys != b_keys or a_values != b_values:
            return {
                "stage": stage,
                "left_call": a.call,
                "right_call": b.call,
                "left_rows": [
                    {"row_label": x, "business_key": y} for x, y in a_keys
                ],
                "right_rows": [
                    {"row_label": x, "business_key": y} for x, y in b_keys
                ],
                "left_values": a_values,
                "right_values": b_values,
            }
    return None


def first_truth_deviation(recipe: Recipe) -> dict[str, Any] | None:
    """Return the earliest stage/column/key where a path stops matching truth."""
    for index, call in enumerate(recipe.calls):
        expected = TRUTH.get(call.call)
        if expected is None:
            continue
        for column, expected_by_key in expected.items():
            actual_values = call.values.get(column, [])
            for position, (key, actual) in enumerate(
                zip(call.business_keys, actual_values)
            ):
                if key in expected_by_key and norm(actual) != expected_by_key[key]:
                    return {
                        "call_index": index,
                        "stage": call.stage,
                        "call": call.call,
                        "column": column,
                        "business_key": key,
                        "expected": expected_by_key[key],
                        "actual": norm(actual),
                        "row_label": call.row_labels[position],
                    }
    return None


def business_key_join_repair(
    raw: pd.DataFrame, correction_table: pd.DataFrame
) -> pd.DataFrame:
    """Repair row/key association explicitly, without positional alignment."""
    merged = raw.reset_index().merge(
        correction_table[["business_key", "correction"]],
        on="business_key",
        how="left",
        validate="one_to_one",
    )
    merged["adjusted"] = merged["raw"] + merged["correction"]
    return merged.set_index("row_label")


def scalar_repair(result: pd.DataFrame) -> pd.DataFrame:
    """Set the audited business amount as a genuine scalar broadcast."""
    repaired = result.copy()
    mask = repaired["business_key"] == "S-A"
    repaired.loc[mask, "correction"] = VERIFIED_S_A
    repaired["adjusted"] = repaired["raw"] + repaired["correction"]
    return repaired


def row_label_series_patch(result: pd.DataFrame) -> pd.DataFrame:
    """Control: a one-row Series aligned by row label does reach S-A."""
    repaired = result.copy()
    mask = repaired["business_key"] == "S-A"
    repaired.loc[mask, "correction"] = pd.Series(
        [VERIFIED_S_A], index=[4102], name="correction"
    )
    repaired["adjusted"] = repaired["raw"] + repaired["correction"]
    return repaired


def narwhals_scalar_repair(result: pd.DataFrame) -> pd.DataFrame:
    """The same repair expressed with Narwhals' scalar literal."""
    frame = nw.from_native(result)
    frame = frame.with_columns(
        correction=nw.when(nw.col("business_key") == "S-A")
        .then(nw.lit(VERIFIED_S_A))
        .otherwise(nw.col("correction"))
    ).with_columns(adjusted=nw.col("raw") + nw.col("correction"))
    return frame.to_native()


def duplicate_correction_rows() -> pd.DataFrame:
    """Two correction rows claim S-B; both also reuse row label 4103."""
    return pd.DataFrame(
        [
            {
                "correction_id": "C-02",
                "row_label": 4103,
                "business_key": "S-B",
                "correction": -2.0,
            },
            {
                "correction_id": "C-07",
                "row_label": 4103,
                "business_key": "S-B",
                "correction": 7.0,
            },
            {
                "correction_id": "C-01",
                "row_label": 4102,
                "business_key": "S-A",
                "correction": 7.0,
            },
        ]
    )


def duplicate_key_unique_labels() -> pd.DataFrame:
    """Two correction rows claim S-B, but every row label is unique."""
    return pd.DataFrame(
        [
            {
                "correction_id": "C-11",
                "row_label": 4103,
                "business_key": "S-B",
                "correction": -2.0,
            },
            {
                "correction_id": "C-12",
                "row_label": 4104,
                "business_key": "S-B",
                "correction": 7.0,
            },
            {
                "correction_id": "C-13",
                "row_label": 4102,
                "business_key": "S-A",
                "correction": 7.0,
            },
        ]
    )


def capture(action: Any) -> Any:
    try:
        return {"ok": True, "value": action()}
    except Exception as exc:  # noqa: BLE001 - diagnosis deliberately captures failures
        return {"ok": False, "error": type(exc).__name__, "message": str(exc)}


def duplicate_label_diagnosis() -> dict[str, Any]:
    raw = build_raw()
    duplicate_table = duplicate_correction_rows()
    duplicate_series = pd.Series(
        duplicate_table["correction"].tolist(),
        index=pd.Index(duplicate_table["row_label"].tolist(), name="row_label"),
        name="correction",
        dtype="float64",
    )
    unique_label_table = duplicate_key_unique_labels()

    def narwhals_attach(table: pd.DataFrame) -> pd.DataFrame:
        series = pd.Series(
            table["correction"].tolist(),
            index=pd.Index(table["row_label"].tolist(), name="row_label"),
            name="correction",
            dtype="float64",
        )
        return (
            nw.from_native(raw)
            .with_columns(
                correction=nw.from_native(series, series_only=True)
            )
            .to_native()
        )

    conflict = (
        duplicate_table.groupby("business_key")
        .filter(lambda group: len(group) > 1)
        .sort_values("correction_id")
    )
    return {
        "duplicate_series": {
            "row_labels": duplicate_series.index.tolist(),
            "business_keys": duplicate_table["business_key"].tolist(),
            "correction_ids": duplicate_table["correction_id"].tolist(),
            "values": duplicate_series.tolist(),
        },
        "native_assignment": capture(lambda: raw.assign(correction=duplicate_series)),
        "narwhals_assignment": capture(lambda: narwhals_attach(duplicate_table)),
        "merge_many_to_one": capture(
            lambda: raw.reset_index().merge(
                duplicate_table[["business_key", "correction"]],
                on="business_key",
                how="left",
                validate="many_to_one",
            )
        ),
        "merge_no_validation": capture(
            lambda: raw.reset_index().merge(
                duplicate_table[["business_key", "correction"]],
                on="business_key",
                how="left",
            )
        ),
        "unique_labels_duplicate_key": {
            "table": unique_label_table.to_dict("records"),
            "native_assignment": capture(
                lambda: raw.assign(
                    correction=pd.Series(
                        unique_label_table["correction"].tolist(),
                        index=pd.Index(
                            unique_label_table["row_label"].tolist(), name="row_label"
                        ),
                        name="correction",
                        dtype="float64",
                    )
                )
            ),
            "narwhals_assignment": capture(
                lambda: narwhals_attach(unique_label_table)
            ),
            "merge_many_to_one": capture(
                lambda: raw.reset_index().merge(
                    unique_label_table[["business_key", "correction"]],
                    on="business_key",
                    how="left",
                    validate="many_to_one",
                )
            ),
        },
        "conflicting_key": "S-B",
        "conflicting_rows": conflict.to_dict("records"),
    }


def scalar_controls(result: pd.DataFrame) -> dict[str, Any]:
    single_row = pd.Series([VERIFIED_S_A], index=["S-A"], name="correction")
    return {
        "native_scalar_assignment": payload(scalar_repair(result)),
        "native_row_label_series": payload(row_label_series_patch(result)),
        "narwhals_literal_conditional": payload(narwhals_scalar_repair(result)),
        "narwhals_literal_broadcast": payload(
            nw.from_native(result)
            .with_columns(flag=nw.lit(VERIFIED_S_A))
            .to_native()
        ),
        "with_columns_one_row_series": capture(
            lambda: nw.from_native(result)
            .with_columns(
                correction=nw.from_native(single_row, series_only=True)
            )
            .to_native()
        ),
        "when_then_one_row_series": capture(
            lambda: nw.from_native(result)
            .with_columns(
                correction=nw.when(nw.col("business_key") == "S-A")
                .then(nw.from_native(single_row, series_only=True))
                .otherwise(nw.col("correction"))
            )
            .to_native()
        ),
    }


def run() -> dict[str, Any]:
    raw = build_raw()
    correction = build_correction()
    native = native_label_recipe()
    narwhals = narwhals_positional_recipe()
    polars = polars_positional_recipe()

    assert native.result is not None
    assert narwhals.result is not None

    final_equal = payload(native.result) == payload(narwhals.result)
    native_vs_narwhals = first_divergence(native, narwhals)
    narwhals_vs_polars = first_divergence(narwhals, polars)

    repaired_by_key = business_key_join_repair(raw, build_correction_table())

    return {
        "versions": {
            "pandas": pd.__version__,
            "narwhals": nw.__version__,
            "polars": pl.__version__,
        },
        "inputs": {
            "raw": payload(raw),
            "correction_series": {
                "row_labels": correction.index.tolist(),
                "business_keys": [row["business_key"] for row in CORRECTION_ROWS],
                "values": [norm(value) for value in correction.tolist()],
            },
        },
        "recipes": {
            "native": native.as_dict(),
            "narwhals": narwhals.as_dict(),
            "polars": polars.as_dict(),
        },
        "first_divergence_native_vs_narwhals": native_vs_narwhals,
        "first_divergence_narwhals_vs_polars": narwhals_vs_polars,
        "first_truth_deviation": {
            "native": first_truth_deviation(native),
            "narwhals": first_truth_deviation(narwhals),
            "polars": first_truth_deviation(polars),
        },
        "final_payloads_equal": final_equal,
        "final_payload": payload(narwhals.result),
        "audited_amounts": {
            "S-B": {"correction": None, "adjusted": None},
            "S-C": {"correction": 3.0, "adjusted": 203.0},
            "S-A": {"correction": VERIFIED_S_A, "adjusted": 107.0},
        },
        "no_index_backend_final": (
            payload(polars.result) if polars.result is not None else None
        ),
        "repairs": {
            "business_key_join": payload(repaired_by_key),
            "scalar_assignment": payload(scalar_repair(narwhals.result)),
            "narwhals_scalar_literal": payload(
                narwhals_scalar_repair(narwhals.result)
            ),
        },
        "scalar_controls": scalar_controls(narwhals.result),
        "duplicate_labels": duplicate_label_diagnosis(),
    }


def verify(report: dict[str, Any]) -> None:
    assert report["final_payloads_equal"], "final payloads should be identical"
    assert report["first_truth_deviation"]["native"]["call"] == (
        "patch S-A with one-row business-key Series"
    ), "native path should first fail at the one-row Series patch"
    assert report["first_truth_deviation"]["narwhals"]["call"] == (
        "attach correction"
    ), "Narwhals path should first fail at positional attachment"
    assert report["first_divergence_native_vs_narwhals"]["stage"] == 0
    assert report["first_divergence_narwhals_vs_polars"] is None, (
        "Narwhals and Polars should agree positionally at every stage"
    )
    duplicate = report["duplicate_labels"]
    assert [row["correction_id"] for row in duplicate["conflicting_rows"]] == [
        "C-02",
        "C-07",
    ], "the two rows competing for S-B should be identified"
    assert duplicate["native_assignment"]["ok"] is False
    assert duplicate["merge_many_to_one"]["ok"] is False
    assert duplicate["unique_labels_duplicate_key"]["native_assignment"]["ok"] is True
    assert duplicate["unique_labels_duplicate_key"]["narwhals_assignment"]["ok"] is True
    assert duplicate["unique_labels_duplicate_key"]["merge_many_to_one"]["ok"] is False
    assert report["scalar_controls"]["with_columns_one_row_series"]["ok"] is False
    assert report["scalar_controls"]["native_row_label_series"][0]["adjusted"] == 107.0
    assert report["repairs"]["scalar_assignment"][0]["adjusted"] == 107.0
    assert report["repairs"]["business_key_join"][0]["adjusted"] is None
    assert report["repairs"]["business_key_join"][2]["adjusted"] == 107.0


def trace_table(report: dict[str, Any], recipe_name: str) -> list[str]:
    lines = [
        f"### {recipe_name}",
        "",
        "| stage | call | row_label | business_key | correction | adjusted |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for call in report["recipes"][recipe_name]["calls"]:
        corrections = [norm(item) for item in call["values"].get("correction", [])]
        adjusted = [norm(item) for item in call["values"].get("adjusted", [])]
        for position, (label, key) in enumerate(
            zip(call["row_labels"], call["business_keys"])
        ):
            correction = corrections[position] if position < len(corrections) else "-"
            amount = adjusted[position] if position < len(adjusted) else "-"
            lines.append(
                f"| {call['stage']} | {call['call']} | {label} | {key} "
                f"| {correction} | {amount} |"
            )
    lines.append("")
    return lines


def render_diagnosis(report: dict[str, Any]) -> str:
    native_dev = report["first_truth_deviation"]["native"]
    narwhals_dev = report["first_truth_deviation"]["narwhals"]
    divergence = report["first_divergence_native_vs_narwhals"]
    final = report["final_payload"]
    duplicate = report["duplicate_labels"]

    lines = [
        "# Sensor correction alignment diagnosis",
        "",
        "Generated by `tools/correction_alignment_diag/replay.py`.",
        "",
        "## Masked final state",
        "",
        f"- Final payloads identical: `{report['final_payloads_equal']}`",
        f"- Final rows: `{json.dumps(final, ensure_ascii=False)}`",
        "- Both paths therefore report two rows and one null at `S-A`; that",
        "  agreement is not evidence that either path was correct earlier.",
        "",
        "## First divergence",
        "",
        f"- Stage `{divergence['stage']}` (`{divergence['left_call']}`)",
        f"- Native corrections: `{divergence['left_values']['correction']}`",
        f"- Narwhals corrections: `{divergence['right_values']['correction']}`",
        "- Row labels and business keys are identical at this stage; only the",
        "  value-to-business-key mapping differs.",
        "",
        "## First deviation from auditable truth",
        "",
        f"- Native: `{native_dev['call']}` -> `{native_dev['business_key']}`",
        f"  `{native_dev['column']}` expected `{native_dev['expected']}`,",
        f"  got `{native_dev['actual']}` at row label `{native_dev['row_label']}`.",
        f"- Narwhals: `{narwhals_dev['call']}` -> `{narwhals_dev['business_key']}`",
        f"  `{narwhals_dev['column']}` expected `{narwhals_dev['expected']}`,",
        f"  got `{narwhals_dev['actual']}` at row label `{narwhals_dev['row_label']}`.",
        "",
        "## Why the final appearance is misleading",
        "",
        "- Native pandas aligned the correction Series by label at attachment,",
        "  so `S-A` began correct (`7.0`). It became null only at the final",
        "  one-row `pd.Series([7.0], index=['S-A'])` assignment, whose index",
        "  labels do not intersect the target's row label `4102`.",
        "- Narwhals reset the operand index in `with_columns` and inserted by",
        "  position, so the missing `S-B` slot travelled to `S-A` immediately.",
        "- The `S-B` row (position 0) is filtered out because",
        "  `business_time=35 > 25`; this removes the row that would make the",
        "  Narwhals corruption obvious.",
        "- The positional path also adds `7.0` to `S-B`, producing `127.0`,",
        "  before that row is discarded. A surviving-output-only check cannot",
        "  see that mis-add at all.",
        "- A null count, a row count, or an index that looks familiar cannot",
        "  identify the cause: both recipes finish with the same two-row,",
        "  one-null shape. The call-level `(row_label, business_key, value)`",
        "  trace is the evidence that separates them.",
        "",
        "## Call-by-call trace",
        "",
    ]
    for recipe_name in ("native", "narwhals", "polars"):
        lines.extend(trace_table(report, recipe_name))
    lines.extend(
        [
            "## Duplicate correction labels",
            "",
            f"- Conflicting business key: `{duplicate['conflicting_key']}`",
            "- Contesting rows: "
            + ", ".join(
                f"`{row['correction_id']}` -> `{row['correction']}`"
                for row in duplicate["conflicting_rows"]
            ),
            f"- Native label assignment: `{duplicate['native_assignment']['error']}`",
            "- The duplicate row label `4103` alone is enough to make native pandas",
            "  reject the assignment, even before the business-key conflict is",
            "  considered.",
            "- With unique row labels but the same duplicate `S-B` key, native and",
            "  Narwhals positional assignment both succeed silently, while the",
            "  business-key merge rejects the ambiguity. The merge `validate`",
            "  argument is what turns a hidden many-to-one conflict into an error.",
            "- Merging without validation duplicates the `S-B` business row, proving",
            "  that row count is also not a reliable post-hoc signal.",
            "",
            "## What each repair actually fixes",
            "",
            "- Business-key merge: attaches `S-A -> 7.0` and leaves `S-B` null; it",
            "  repairs row/key association, not missing source data.",
            "- Scalar broadcast (`df.loc[mask, col] = 7.0` or `nw.lit(7.0)`):",
            "  rewrites the audited value on the intended rows; it does not repair",
            "  a mis-associated series.",
            "- A one-row `Series` is not a scalar: Narwhals raises `ShapeError`, and",
            "  native pandas aligns it by label and silently misses. The same",
            "  one-row Series reaches `S-A` when its index is the row label",
            "  `4102` instead of the business label `S-A`, which isolates label",
            "  alignment as the mechanism.",
            "",
            "## Correct business amounts",
            "",
            "| business_key | correction | adjusted |",
            "| --- | --- | --- |",
            "| S-A | 7.0 | 107.0 |",
            "| S-B | null (no source correction) | null |",
            "| S-C | 3.0 | 203.0 |",
            "",
        ]
    )
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=None,
        help="Write report.json and diagnosis.md to this directory.",
    )
    args = parser.parse_args()

    report = run()
    verify(report)
    rendered = json.dumps(report, indent=2, ensure_ascii=False, default=str)
    if args.out_dir is not None:
        args.out_dir.mkdir(parents=True, exist_ok=True)
        (args.out_dir / "report.json").write_text(rendered + "\n", encoding="utf-8")
        (args.out_dir / "diagnosis.md").write_text(
            render_diagnosis(report), encoding="utf-8"
        )
    print(rendered)


if __name__ == "__main__":
    main()
