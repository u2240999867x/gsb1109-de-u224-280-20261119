#!/usr/bin/env python3
"""
Trace where a settlement reading first becomes impossible to reconstruct.

The probe calls etlhelper.copy_rows() unchanged.  Source and target DBAPI
objects are recording stand-ins, so the value recorded at each boundary is the
actual object ETLHelper passed across that boundary.

Run from the repository root:

    .venv/bin/python diagnostics/numeric_boundary_probe.py

Inputs : diagnostics/ledger.csv
Outputs: console report, diagnostics/report.md, diagnostics/observations.json

No external database is used.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
from contextlib import contextmanager
from dataclasses import asdict, dataclass, field
from decimal import Decimal, ROUND_HALF_UP, localcontext
from pathlib import Path
from typing import Any
from unittest.mock import patch

from etlhelper import copy_rows
from etlhelper.db_helper_factory import DB_HELPER_FACTORY
from etlhelper.db_helpers.sqlite import SQLiteDbHelper
from etlhelper.row_factories import dict_row_factory

TWO_PLACES = Decimal("0.01")

# The order is the causal order through ETLHelper.  A boundary is only a
# label; the observation stored against it is the real object at that point.
STAGES = (
    ("dbapi_source", "raw DBAPI source value (ledger text)"),
    ("source_cursor", "source cursor handed to ETLHelper"),
    ("row_factory", "ETLHelper row factory output"),
    ("callback", "transform callback output"),
    ("bind", "parameter handed to target driver"),
    ("target_store", "target column conversion"),
    ("target_readback", "target SELECT/readback"),
)
STAGE_NAMES = tuple(stage for stage, _ in STAGES)
STAGE_LABELS = dict(STAGES)


def q2(value: Decimal) -> Decimal:
    """Quantize to two decimal places using the target policy in the probe."""
    with localcontext() as ctx:
        ctx.prec = max(60, len(value.as_tuple().digits) + 10)
        return value.quantize(TWO_PLACES, rounding=ROUND_HALF_UP)


def decimal_from(value: Any) -> Decimal:
    """Convert a recorded value to Decimal without going through binary float."""
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def display_2dp(value: Any) -> str:
    """The two-decimal presentation used by the problematic logs."""
    if value is None:
        return "-"
    try:
        return f"{q2(decimal_from(value)):.2f}"
    except Exception:
        return str(value)


def normalized_text(value: Any) -> str:
    """Canonical decimal text used to compare values, not types."""
    if value is None:
        return ""
    try:
        return str(decimal_from(value))
    except Exception:
        return str(value)


def log_2dp_view(message: str) -> str:
    """Approximate a log formatter that renders numeric values with two decimals."""
    def decimal_repl(match: re.Match) -> str:
        return display_2dp(Decimal(match.group(1)))

    def float_repl(match: re.Match) -> str:
        return display_2dp(Decimal(match.group(1)))

    message = re.sub(r"Decimal\('([^']+)'\)", decimal_repl, message)
    message = re.sub(r"(?<![\w.])(-?\d+\.\d+)(?![\w.])", float_repl, message)
    return message


@dataclass
class Observation:
    scenario: str
    row_id: str
    stage: str
    type_name: str
    value_repr: str
    value_text: str
    display_2dp: str
    exact_decimal_equal: bool
    text_recovers_input: bool
    same_object_as_previous: bool | None
    input_text: str


@dataclass
class ScenarioResult:
    name: str
    role: str
    description: str
    path: str
    rows: list[tuple[str, str]]
    observations: list[Observation] = field(default_factory=list)
    target_records: list[dict[str, Any]] = field(default_factory=list)
    readback_records: list[dict[str, Any]] = field(default_factory=list)
    log_messages: list[str] = field(default_factory=list)
    processed: int = 0
    failed: int = 0

    def __post_init__(self) -> None:
        self.raw_by_id = dict(self.rows)
        self._obs: dict[tuple[str, str], Observation] = {}
        # Hold a reference to every recorded object so identity comparisons
        # cannot be confused by Python reusing a freed object's address.
        self._held: list[Any] = []
        self._last_object: dict[str, Any] = {}

    def record(self, row_id: str, stage: str, value: Any) -> None:
        canonical = Decimal(self.raw_by_id[row_id])
        previous = self._last_object.get(row_id)
        self._held.append(value)
        self._last_object[row_id] = value
        if isinstance(value, float):
            exact_value = Decimal(value)
        else:
            exact_value = decimal_from(value)
        obs = Observation(
            scenario=self.name,
            row_id=row_id,
            stage=stage,
            type_name=type(value).__name__,
            value_repr=repr(value),
            value_text=normalized_text(value),
            display_2dp=display_2dp(value),
            exact_decimal_equal=(exact_value == canonical),
            text_recovers_input=(normalized_text(value) == str(canonical)),
            same_object_as_previous=(None if previous is None else previous is value),
            input_text=self.raw_by_id[row_id],
        )
        self.observations.append(obs)
        self._obs[(row_id, stage)] = obs

    def obs(self, row_id: str, stage: str) -> Observation:
        return self._obs[(row_id, stage)]

    def first_row_log(self) -> str:
        for message in self.log_messages:
            if message.startswith("First row:"):
                return message
        return "(no First row log emitted)"


class CollectingHandler(logging.Handler):
    def __init__(self) -> None:
        super().__init__()
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class FakeSourceCursor:
    """A DBAPI cursor that records specifically what it hands to ETLHelper."""

    def __init__(self, conn: "FakeSourceConnection") -> None:
        self.conn = conn
        self.description = [("id", None, None, None, None, None),
                            ("reading", None, None, None, None, None)]
        self.rowcount = -1
        self._served = False

    def execute(self, query: str, parameters: Any = ()) -> "FakeSourceCursor":
        self.conn.queries.append((query, tuple(parameters)))
        return self

    def fetchmany(self, size: int) -> list[tuple[str, Decimal]]:
        if self._served:
            return []
        self._served = True
        rows = []
        for row_id, text in self.conn.rows:
            self.conn.result.record(row_id, "dbapi_source", text)
            value = Decimal(text)
            if self.conn.source_round:
                value = q2(value)
            self.conn.result.record(row_id, "source_cursor", value)
            rows.append((row_id, value))
        self.rowcount = len(rows)
        return rows

    def close(self) -> None:
        pass


class FakeSourceConnection:
    def __init__(self, result: ScenarioResult, rows: list[tuple[str, str]],
                 source_round: bool) -> None:
        self.result = result
        self.rows = rows
        self.source_round = source_round
        self.queries: list[tuple[str, tuple[Any, ...]]] = []

    def cursor(self) -> FakeSourceCursor:
        return FakeSourceCursor(self)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class FakeTargetCursor:
    """A DBAPI cursor that records the parameters ETLHelper passes to it."""

    def __init__(self, conn: "FakeTargetConnection") -> None:
        self.conn = conn

    def executemany(self, query: str, chunk: list[dict[str, Any]]) -> None:
        self.conn.queries.append((query, None))
        for row in chunk:
            row_id = row["id"]
            value = row["reading"]
            self.conn.result.record(row_id, "bind", value)
            stored = self.conn.store(value)
            self.conn.result.record(row_id, "target_store", stored)
            self.conn.result.target_records.append(
                {"id": row_id, "reading": stored}
            )

    def close(self) -> None:
        pass


class FakeTargetConnection:
    def __init__(self, result: ScenarioResult, target_round: bool) -> None:
        self.result = result
        self.target_round = target_round
        self.queries: list[tuple[str, Any]] = []

    def cursor(self) -> FakeTargetCursor:
        return FakeTargetCursor(self)

    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass

    def store(self, value: Any) -> Any:
        if not self.target_round:
            return value
        if isinstance(value, Decimal):
            return q2(value)
        if isinstance(value, float):
            # A numeric target receiving a binary float must first express it
            # as a decimal.  The probe makes that conversion explicit.
            return q2(Decimal(repr(value)))
        if isinstance(value, str):
            return q2(Decimal(value))
        return value

    def emit_readback(self) -> None:
        for record in self.result.target_records:
            value = record["reading"]
            # Readback is a separate observation, not a reuse of the write.
            readback = Decimal(str(value))
            self.result.record(record["id"], "target_readback", readback)
            self.result.readback_records.append(
                {"id": record["id"], "reading": readback}
            )


@contextmanager
def sqlite_helper_for_standins():
    """
    Let ETLHelper's public copy_rows path use its SQLite helper for cursor
    context management and paramstyle while all I/O goes to the stand-ins.
    """
    with patch.object(DB_HELPER_FACTORY, "from_conn",
                      return_value=SQLiteDbHelper()):
        yield


def run_scenario(spec: dict[str, Any], ledger: dict[str, str]) -> ScenarioResult:
    rows = [(row_id, ledger[row_id]) for row_id in spec["row_ids"]]
    result = ScenarioResult(
        name=spec["name"],
        role=spec["role"],
        description=spec["description"],
        path=spec["path"],
        rows=rows,
    )
    source = FakeSourceConnection(result, rows, spec["source_round"])
    target = FakeTargetConnection(result, spec["target_round"])

    def transform(chunk):
        output = []
        for row in chunk:
            row_id = row["id"]
            value = row["reading"]
            # The row factory has already run inside ETLHelper; this is the
            # object the transform callback receives.
            result.record(row_id, "row_factory", value)
            if spec["callback"] == "float":
                value = float(value)
            result.record(row_id, "callback", value)
            output.append({"id": row_id, "reading": value})
        return output

    handler = CollectingHandler()
    logger = logging.getLogger("etlhelper")
    old_level = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG)
    try:
        with sqlite_helper_for_standins():
            processed, failed = copy_rows(
                "SELECT id, reading FROM ledger ORDER BY id",
                source,
                "INSERT INTO target (id, reading) VALUES (:id, :reading)",
                target,
                row_factory=dict_row_factory,
                transform=transform,
                chunk_size=100,
            )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    result.log_messages = handler.messages
    result.processed = processed
    result.failed = failed
    target.emit_readback()
    return result


def individual_boundary(result: ScenarioResult,
                        row_id: str) -> tuple[str, str | None, str, str | None]:
    """Return last/first boundaries for exact value and for decimal text recovery."""
    series = [result.obs(row_id, stage) for stage in STAGE_NAMES]

    def boundary(attribute: str) -> tuple[str, str | None]:
        for index, obs in enumerate(series):
            if not getattr(obs, attribute):
                previous = series[index - 1].stage if index else "(none)"
                return previous, obs.stage
        return series[-1].stage, None

    exact_last, exact_first = boundary("exact_decimal_equal")
    text_last, text_first = boundary("text_recovers_input")
    return exact_last, exact_first, text_last, text_first


def pair_boundary(result: ScenarioResult,
                  row_a: str, row_b: str) -> tuple[str | None, str | None]:
    """Return (first collapsed boundary, last distinct boundary)."""
    last_distinct = None
    for stage in STAGE_NAMES:
        same = result.obs(row_a, stage).value_text == result.obs(row_b, stage).value_text
        if same:
            return stage, last_distinct
        last_distinct = stage
    return None, last_distinct


def target_value(result: ScenarioResult, row_id: str) -> Any:
    for record in result.readback_records:
        if record["id"] == row_id:
            return record["reading"]
    return None


def md_table(headers: list[str], rows: list[list[Any]]) -> str:
    def cell(value: Any) -> str:
        return str(value).replace("|", "\\|").replace("\n", " ")

    lines = ["| " + " | ".join(headers) + " |",
             "| " + " | ".join("---" for _ in headers) + " |"]
    lines.extend("| " + " | ".join(cell(value) for value in row) + " |"
                 for row in rows)
    return "\n".join(lines)


def build_report(results: dict[str, ScenarioResult],
                 ledger_rows: list[dict[str, str]],
                 ledger_path: Path) -> str:
    lines: list[str] = []
    lines.append("# ETLHelper numeric boundary diagnosis")
    lines.append("")
    lines.append("Run from the repository root with:")
    lines.append("")
    lines.append("```bash")
    lines.append(".venv/bin/python diagnostics/numeric_boundary_probe.py")
    lines.append("```")
    lines.append("")
    lines.append(f"Ledger input: `{ledger_path}`  ")
    lines.append("Library under test: `etlhelper.copy_rows` (unchanged)  ")
    lines.append("Database dependency: none; source/target DBAPI objects are recording stand-ins.")
    lines.append("")
    lines.append("## Ledger")
    lines.append("")
    lines.append(md_table(
        ["id", "raw text", "Decimal value", "2dp display", "note"],
        [[row["id"], row["reading_text"],
          str(Decimal(row["reading_text"])),
          display_2dp(row["reading_text"]), row["note"]]
         for row in ledger_rows],
    ))
    lines.append("")
    lines.append("## Boundary order")
    lines.append("")
    lines.append("1. `dbapi_source` - raw text from the ledger/source DBAPI.")
    lines.append("2. `source_cursor` - the object the source cursor hands to ETLHelper.")
    lines.append("3. `row_factory` - ETLHelper's `dict_row_factory` output.")
    lines.append("4. `callback` - the existing transform callback output.")
    lines.append("5. `bind` - the same object ETLHelper passes to the target cursor.")
    lines.append("6. `target_store` - the target column's own 2dp conversion.")
    lines.append("7. `target_readback` - a separate read of the stored target value.")
    lines.append("")
    lines.append("## Method and fidelity")
    lines.append("")
    lines.append("- `etlhelper.copy_rows()` is called through its public API; its "
                 "extract, row-factory, transform, insert-SQL and bind ordering is "
                 "the library's own.")
    lines.append("- Source and target DBAPI connections/cursors are recording "
                 "stand-ins. The source stand-in parses the ledger with `Decimal` "
                 "and records the exact object it returns. The target stand-in "
                 "records the exact parameter ETLHelper hands to `executemany`, "
                 "applies the declared target-column policy, and then exposes a "
                 "separate readback.")
    lines.append("- `DB_HELPER_FACTORY.from_conn` is temporarily mapped to "
                 "ETLHelper's own `SQLiteDbHelper` so the unmodified `copy_rows` "
                 "gets a cursor context manager and named-parameter style. That "
                 "helper performs no rounding; all rounding is in the labelled "
                 "stand-in boundaries.")
    lines.append("- `target_store` models the stated target-column policy "
                 "(`HALF_UP` to two places). It is not a claim about any "
                 "particular database's implicit cast rules.")
    lines.append("")
    lines.append("A type change is not automatically a value loss: a `float` that "
                 "happens to retain the same decimal text is marked `yes` for text "
                 "recovery but `NO` for exact value. The exact object type is "
                 "always recorded.")
    lines.append("")

    lines.append("## Scenario paths")
    lines.append("")
    lines.append(md_table(
        ["scenario", "role", "path"],
        [[result.name, result.role, result.path]
         for result in results.values()],
    ))
    lines.append("")

    lines.append("## Observations")
    lines.append("")
    for result in results.values():
        lines.append(f"### {result.name}")
        lines.append("")
        lines.append(result.description)
        lines.append("")
        rows = []
        for row_id, _ in result.rows:
            for stage in STAGE_NAMES:
                obs = result.obs(row_id, stage)
                if obs.same_object_as_previous is None:
                    same_object = "-"
                else:
                    same_object = "yes" if obs.same_object_as_previous else "no"
                rows.append([
                    row_id,
                    stage,
                    obs.type_name,
                    f"`{obs.value_repr}`",
                    obs.display_2dp,
                    "yes" if obs.exact_decimal_equal else "**NO**",
                    "yes" if obs.text_recovers_input else "**NO**",
                    same_object,
                ])
        lines.append(md_table(
            ["row", "boundary", "Python type", "true value (repr)",
             "2dp display", "exact decimal equals input?",
             "decimal text recovers input?", "same object as previous"],
            rows,
        ))
        lines.append("")

    lines.append("## Last recoverable and first lost boundary")
    lines.append("")
    summary_rows = []
    for result in results.values():
        for row_id, _ in result.rows:
            exact_last, exact_first, text_last, text_first = individual_boundary(
                result, row_id
            )
            summary_rows.append([
                result.name, row_id,
                f"`{text_last}`", f"`{text_first}`" if text_first else "(no loss)",
                f"`{exact_last}`", f"`{exact_first}`" if exact_first else "(no loss)",
                result.description,
            ])
    lines.append(md_table(
        ["scenario", "row", "last text-recoverable boundary",
         "first text lost", "last exact-value boundary",
         "first exact-value lost", "meaning"],
        summary_rows,
    ))
    lines.append("")
    lines.append("The text boundary is what a decimal ledger can replay. The exact "
                 "boundary is stricter: `Decimal(2.346)` from a Python float is "
                 "`2.345999999999999...`, even though `repr()` is `2.346`.")
    lines.append("")

    lines.append("## Pair separation: Decimal retained vs float callback")
    lines.append("")
    pair_rows = []
    for name in ("P-DECIMAL", "P-FLOAT"):
        result = results[name]
        first_same, last_distinct = pair_boundary(result, "R-FLOAT-1", "R-FLOAT-2")
        values = []
        for stage in STAGE_NAMES:
            a = result.obs("R-FLOAT-1", stage)
            b = result.obs("R-FLOAT-2", stage)
            values.append(f"{stage}: {a.value_text} / {b.value_text}")
        pair_rows.append([
            name,
            f"`{first_same}`" if first_same else "(never collapsed)",
            f"`{last_distinct}`",
            "<br>".join(values),
        ])
    lines.append(md_table(
        ["path", "first boundary where the pair is equal",
         "last boundary where the pair differs", "values at each boundary"],
        pair_rows,
    ))
    lines.append("")

    lines.append("## Two copies with the same target value")
    lines.append("")
    same_target_rows = []
    for name in ("A-SRC-ROUND", "B-TGT-ROUND", "R-FLOAT-ROUND"):
        result = results[name]
        same_target_rows.append([
            name,
            f"`{result.obs('R-ROUND', 'source_cursor').value_repr}`",
            f"`{result.obs('R-ROUND', 'bind').value_repr}`",
            result.obs("R-ROUND", "bind").display_2dp,
            f"`{repr(target_value(result, 'R-ROUND'))}`",
        ])
    lines.append(md_table(
        ["scenario", "source cursor value", "bind parameter",
         "2dp display", "target readback"],
        same_target_rows,
    ))
    lines.append("")
    a_value = target_value(results["A-SRC-ROUND"], "R-ROUND")
    b_value = target_value(results["B-TGT-ROUND"], "R-ROUND")
    lines.append(f"`A-SRC-ROUND` and `B-TGT-ROUND` both read back "
                 f"`{a_value!r}`: the target records are equal. They differ at "
                 "`source_cursor` and at `bind`.")
    lines.append("")

    lines.append("## ETLHelper DEBUG log view")
    lines.append("")
    log_rows = []
    for result in results.values():
        raw = result.first_row_log()
        log_rows.append([result.name, f"`{raw}`", f"`{log_2dp_view(raw)}`"])
    lines.append(md_table(
        ["scenario", "raw `First row` message", "2dp formatter view"],
        log_rows,
    ))
    lines.append("")
    lines.append("ETLHelper logs only `First row` per `executemany` call, so the "
                 "second member of the large-reading pair is not in the log at all. "
                 "The 2dp view hides A/B/R-FLOAT differences that are present in "
                 "the real objects.")
    lines.append("")

    lines.append("## What the target value alone can and cannot tell you")
    lines.append("")
    lines.append("For `R-ROUND` with only `target readback = 2.35`, the following "
                 "paths remain compatible:")
    lines.append("")
    lines.append("- `A-SRC-ROUND`: source cursor rounded `2.346 -> 2.35`.")
    lines.append("- `B-TGT-ROUND`: source cursor and bind kept `2.346`; target "
                 "column rounded it to `2.35`.")
    lines.append("- `R-FLOAT-ROUND` also lands on `2.35` numerically, but its "
                 "callback object is a `float`, not `Decimal`.")
    lines.append("")
    lines.append("Minimum additional observations to separate them:")
    lines.append("")
    lines.append("1. Look at `source_cursor`: A is `Decimal('2.35')`; B and "
                 "R-FLOAT are `Decimal('2.346')`. This separates A.")
    lines.append("2. Look at the `callback` output or `bind` type: B is "
                 "`Decimal('2.346')`; R-FLOAT is `float 2.346`. This separates "
                 "B from the float path.")
    lines.append("")
    lines.append("For the large pair, a target readback of "
                 "`9007199254740994.00` for both rows is compatible with the "
                 "float path (`P-FLOAT`) and incompatible with the Decimal path "
                 "(`P-DECIMAL`). The minimum extra boundary is `row_factory` "
                 "(distinct `Decimal('...993.01')` / `Decimal('...993.02')`) or "
                 "`callback` (both `float 9007199254740994.0`).")
    lines.append("")

    lines.append("## Conclusion")
    lines.append("")
    lines.append("- Source pre-rounding: the first irreversible boundary is "
                 "`source_cursor`; the raw `dbapi_source` text is the last place "
                 "the original is recoverable.")
    lines.append("- Target-only 2dp quantization: the first irreversible boundary "
                 "is `target_store`; `bind` is the last place the original is "
                 "recoverable.")
    lines.append("- Float callback: for the large pair the first irreversible "
                 "boundary is `callback`; `row_factory` is the last place the two "
                 "inputs are distinct. For `2.346` the float's text still reads "
                 "`2.346`, but its exact value is already "
                 f"`Decimal(2.346) = {Decimal(2.346)!r}` at `callback`; the 2dp "
                 "loss appears later at `target_store`. The type change is at "
                 "`callback` even when the text boundary looks safe.")
    lines.append("- Equal target records and equal 2dp log displays are therefore "
                 "not sufficient evidence. Read `source_cursor`, `callback`/`bind`, "
                 "and `target_readback` separately.")
    lines.append("")
    lines.append("No ETLHelper numeric conversion policy was changed: all "
                 "rounding in this report occurs in the recording stand-ins, and "
                 "`copy_rows` is called through its public API.")
    lines.append("")
    return "\n".join(lines)


def build_json(results: dict[str, ScenarioResult],
               ledger_rows: list[dict[str, str]]) -> dict[str, Any]:
    scenarios = {}
    for name, result in results.items():
        per_row = {}
        for row_id, raw_text in result.rows:
            exact_last, exact_first, text_last, text_first = individual_boundary(
                result, row_id
            )
            per_row[row_id] = {
                "input_text": raw_text,
                "last_text_recoverable_boundary": text_last,
                "first_text_lost_boundary": text_first,
                "last_exact_value_boundary": exact_last,
                "first_exact_value_lost_boundary": exact_first,
                "observations": [asdict(obs) for obs in result.observations
                                 if obs.row_id == row_id],
            }
        scenarios[name] = {
            "role": result.role,
            "description": result.description,
            "path": result.path,
            "processed": result.processed,
            "failed": result.failed,
            "rows": per_row,
            "target_records": [
                {"id": rec["id"], "reading_repr": repr(rec["reading"]),
                 "reading_type": type(rec["reading"]).__name__}
                for rec in result.target_records
            ],
            "readback_records": [
                {"id": rec["id"], "reading_repr": repr(rec["reading"]),
                 "reading_type": type(rec["reading"]).__name__}
                for rec in result.readback_records
            ],
            "first_row_log": result.first_row_log(),
            "first_row_log_2dp_view": log_2dp_view(result.first_row_log()),
        }
    pair = {}
    for name in ("P-DECIMAL", "P-FLOAT"):
        result = results[name]
        first_same, last_distinct = pair_boundary(result, "R-FLOAT-1", "R-FLOAT-2")
        pair[name] = {
            "first_collapsed_boundary": first_same,
            "last_distinct_boundary": last_distinct,
        }
    return {
        "ledger": ledger_rows,
        "scenarios": scenarios,
        "pair_separation": pair,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ledger", type=Path,
                        default=Path("diagnostics/ledger.csv"))
    parser.add_argument("--report", type=Path,
                        default=Path("diagnostics/report.md"))
    parser.add_argument("--json", type=Path,
                        default=Path("diagnostics/observations.json"))
    args = parser.parse_args()

    with args.ledger.open(newline="", encoding="utf-8") as handle:
        ledger_rows = list(csv.DictReader(handle))
    ledger = {row["id"]: row["reading_text"] for row in ledger_rows}

    specs = [
        {
            "name": "A-SRC-ROUND",
            "role": "required copy 1",
            "description": "Source cursor already rounded 2.346 to 2.35.",
            "path": "source_cursor: HALF_UP to 0.01 -> callback identity -> "
                    "target column: HALF_UP to 0.01",
            "row_ids": ["R-ROUND"],
            "source_round": True,
            "callback": "identity",
            "target_round": True,
        },
        {
            "name": "B-TGT-ROUND",
            "role": "required copy 2",
            "description": "Source cursor and callback kept 2.346; only the "
                           "target column rounded.",
            "path": "source_cursor: exact Decimal -> callback identity -> "
                    "target column: HALF_UP to 0.01",
            "row_ids": ["R-ROUND"],
            "source_round": False,
            "callback": "identity",
            "target_round": True,
        },
        {
            "name": "R-FLOAT-ROUND",
            "role": "ambiguity probe",
            "description": "Same 2.346 reading through a float callback.",
            "path": "source_cursor: exact Decimal -> callback float -> "
                    "target column: HALF_UP to 0.01",
            "row_ids": ["R-ROUND"],
            "source_round": False,
            "callback": "float",
            "target_round": True,
        },
        {
            "name": "P-DECIMAL",
            "role": "large pair, Decimal retained",
            "description": "Both large readings stay Decimal end to end.",
            "path": "source_cursor: exact Decimal -> callback identity -> "
                    "target column: HALF_UP to 0.01",
            "row_ids": ["R-FLOAT-1", "R-FLOAT-2"],
            "source_round": False,
            "callback": "identity",
            "target_round": True,
        },
        {
            "name": "P-FLOAT",
            "role": "large pair, float callback",
            "description": "Both large readings pass through a float callback.",
            "path": "source_cursor: exact Decimal -> callback float -> "
                    "target column: HALF_UP to 0.01",
            "row_ids": ["R-FLOAT-1", "R-FLOAT-2"],
            "source_round": False,
            "callback": "float",
            "target_round": True,
        },
    ]

    results = {spec["name"]: run_scenario(spec, ledger) for spec in specs}
    report = build_report(results, ledger_rows, args.ledger)
    observation_json = build_json(results, ledger_rows)

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(report, encoding="utf-8")
    args.json.write_text(json.dumps(observation_json, indent=2,
                                    ensure_ascii=False) + "\n",
                          encoding="utf-8")

    print(report)
    print(f"\nWrote {args.report} and {args.json}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
