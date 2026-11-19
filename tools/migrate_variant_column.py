#!/usr/bin/env python
# ruff: noqa: T201
# flake8: noqa: T201
"""Merge dlt variant column values back into the main column, with a dry-run pre-check.

When a column (e.g. `weight`) occasionally arrives as a string, dlt keeps those
values in a variant column (e.g. `weight_v_text`). This tool moves values that
can be converted back to the main column type *losslessly*:

    movable   main column is NULL and the variant string converts losslessly
    in sync   main column already holds the same value (variant copy is cleared)
    conflict  main column holds a different value (never overwritten)
    keep      trailing units, unknown format or undeterminable precision
              (left in the variant column for manual review)

By default only a pre-check report is printed. Pass --apply to execute the
UPDATE statements for `movable` and `in sync` rows. Conflicts and `keep` rows
are never touched.

Usage:
    python tools/migrate_variant_column.py PIPELINE TABLE COLUMN [options]

Arguments:
    pipeline: dlt pipeline name (attached from --pipelines-dir)
    table:    table name in the pipeline dataset
    column:   main column name (variant column is auto-detected from the schema)

Options:
    --pipelines-dir DIR   directory where the pipeline state is stored
    --variant-column NAME override variant column name (default: auto-detect)
    --key NAME            row identifier column (default: _dlt_id when present)
    --apply               execute the migration (default: dry-run pre-check)

Exit codes:
    0 - pre-check or migration completed
    1 - unexpected failure
    2 - usage error (unknown table/column, ambiguous variant column, ...)
"""

import argparse
import math
import re
import sys
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any, List, Optional, Sequence, Tuple

# plain numeric literals only: no units, no thousands separators, no NaN/inf
NUMERIC_RE = re.compile(r"^[+-]?(\d+(\.\d*)?|\.\d+)([eE][+-]?\d+)?$")

SUPPORTED_MAIN_TYPES = ("decimal", "double", "bigint")

BIGINT_MIN = -(2**63)
BIGINT_MAX = 2**63 - 1

ACTION_MOVE = "move"
ACTION_CLEAR = "clear"
ACTION_CONFLICT = "conflict"
ACTION_KEEP = "keep"


@dataclass
class RowPlan:
    """Migration decision for a single row."""

    key: Any
    main_value: Any
    variant_value: Any
    action: str
    reason: str = ""
    parsed: Optional[Decimal] = None


@dataclass
class MigrationPlan:
    rows: List[RowPlan] = field(default_factory=list)

    def by_action(self, action: str) -> List[RowPlan]:
        return [row for row in self.rows if row.action == action]

    @property
    def movable(self) -> List[RowPlan]:
        return self.by_action(ACTION_MOVE)

    @property
    def conflicts(self) -> List[RowPlan]:
        return self.by_action(ACTION_CONFLICT)


def _fits_decimal_type(value: Decimal, precision: Optional[int], scale: Optional[int]) -> bool:
    """Check that `value` fits decimal(precision, scale) without rounding."""
    with localcontext() as ctx:
        ctx.prec = max(78, len(value.as_tuple().digits) + 8)
        if scale is not None:
            quantum = Decimal(1).scaleb(-scale)
            try:
                quantized = value.quantize(quantum)
            except InvalidOperation:
                return False
            if quantized != value:
                return False
        else:
            quantized = value
        if precision is not None:
            int_digits = max(quantized.adjusted() + 1, 1) if quantized else 1
            frac_digits = scale if scale is not None else max(-quantized.as_tuple().exponent, 0)
            if int_digits + frac_digits > precision:
                return False
    return True


def parse_lossless_value(
    raw: Any,
    data_type: str,
    precision: Optional[int] = None,
    scale: Optional[int] = None,
) -> Optional[Decimal]:
    """Parse a variant string into a Decimal only if converting it to the main
    column type is lossless. Returns None when the value must stay in the
    variant column (units, unknown format, undeterminable precision)."""
    if not isinstance(raw, str):
        return None
    text = raw.strip()
    if not NUMERIC_RE.match(text):
        return None
    try:
        value = Decimal(text)
    except InvalidOperation:
        return None
    if data_type in ("decimal", "numeric"):
        return value if _fits_decimal_type(value, precision, scale) else None
    if data_type == "double":
        # lossless only when the double round-trips to the same decimal value
        try:
            as_double = float(text)
        except OverflowError:
            return None
        if not math.isfinite(as_double):
            return None
        return value if Decimal(repr(as_double)) == value else None
    if data_type == "bigint":
        if value != value.to_integral_value():
            return None
        if not BIGINT_MIN <= int(value) <= BIGINT_MAX:
            return None
        return value
    return None


def _main_equals(main_value: Any, parsed: Decimal) -> bool:
    try:
        return Decimal(str(main_value)) == parsed
    except InvalidOperation:
        return False


def classify_row(
    key: Any,
    main_value: Any,
    variant_value: Any,
    data_type: str,
    precision: Optional[int] = None,
    scale: Optional[int] = None,
) -> RowPlan:
    parsed = parse_lossless_value(variant_value, data_type, precision, scale)
    if parsed is None:
        return RowPlan(
            key,
            main_value,
            variant_value,
            ACTION_KEEP,
            reason=f"not losslessly convertible to {data_type}",
        )
    if main_value is None:
        return RowPlan(key, main_value, variant_value, ACTION_MOVE, parsed=parsed)
    if _main_equals(main_value, parsed):
        return RowPlan(
            key, main_value, variant_value, ACTION_CLEAR, reason="already in sync", parsed=parsed
        )
    return RowPlan(
        key,
        main_value,
        variant_value,
        ACTION_CONFLICT,
        reason="main column holds a different value",
        parsed=parsed,
    )


def plan_migration(
    rows: Sequence[Tuple[Any, Any, Any]],
    data_type: str,
    precision: Optional[int] = None,
    scale: Optional[int] = None,
) -> MigrationPlan:
    plan = MigrationPlan()
    for key, main_value, variant_value in rows:
        plan.rows.append(
            classify_row(key, main_value, variant_value, data_type, precision, scale)
        )
    return plan


def sql_literal(value: Any) -> str:
    if value is None:
        return "NULL"
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bool):
        return "TRUE" if value else "FALSE"
    if isinstance(value, (int, float)):
        return repr(value)
    return "'" + str(value).replace("'", "''") + "'"


def build_update_statement(
    qualified_table: str,
    key_col: str,
    main_col: str,
    variant_col: str,
    row: RowPlan,
) -> str:
    """Build a single-row UPDATE. Only MOVE and CLEAR rows are migratable."""
    if row.action == ACTION_MOVE:
        set_clause = f"{main_col} = {sql_literal(row.parsed)}, {variant_col} = NULL"
    elif row.action == ACTION_CLEAR:
        set_clause = f"{variant_col} = NULL"
    else:
        raise ValueError(f"row action {row.action!r} is not migratable")
    return (
        f"UPDATE {qualified_table} SET {set_clause}"
        f" WHERE {key_col} = {sql_literal(row.key)} AND {variant_col} IS NOT NULL"
    )


def find_variant_column(
    table_columns: dict,
    column: str,
    variant_column: Optional[str] = None,
) -> str:
    """Resolve the variant column name from the schema (explicit or auto-detect)."""
    if variant_column:
        if variant_column not in table_columns:
            raise ValueError(f"variant column `{variant_column}` not found in table schema")
        return variant_column
    candidates = [
        name
        for name, col in table_columns.items()
        if col.get("variant") and name.startswith(f"{column}_v_")
    ]
    if not candidates:
        raise ValueError(f"no variant column found for `{column}` in table schema")
    if len(candidates) > 1:
        raise ValueError(
            f"several variant columns found for `{column}`: {', '.join(sorted(candidates))}."
            " Pass --variant-column to pick one."
        )
    return candidates[0]


def print_report(plan: MigrationPlan) -> None:
    groups = (
        (ACTION_MOVE, "movable (main column is NULL, value converts losslessly)"),
        (ACTION_CLEAR, "already in sync (variant copy will be cleared)"),
        (ACTION_CONFLICT, "conflicts (main column keeps its value, never overwritten)"),
        (ACTION_KEEP, "kept in variant column (manual review needed)"),
    )
    for action, title in groups:
        rows = plan.by_action(action)
        print(f"\n== {title}: {len(rows)}")
        for row in rows:
            detail = f"key={row.key} main={row.main_value!r} variant={row.variant_value!r}"
            if row.action == ACTION_MOVE:
                detail += f" -> {row.parsed}"
            if row.reason:
                detail += f" ({row.reason})"
            print(f"  {detail}")
    print(
        f"\nsummary: {len(plan.movable)} movable,"
        f" {len(plan.by_action(ACTION_CLEAR))} in sync,"
        f" {len(plan.conflicts)} conflicts,"
        f" {len(plan.by_action(ACTION_KEEP))} kept in variant column"
    )


def run(
    pipeline_name: str,
    table: str,
    column: str,
    pipelines_dir: Optional[str] = None,
    variant_column: Optional[str] = None,
    key_column: Optional[str] = None,
    apply: bool = False,
) -> int:
    import dlt

    pipeline = dlt.attach(pipeline_name=pipeline_name, pipelines_dir=pipelines_dir)
    schema = pipeline.default_schema
    schema_tables = schema.tables
    if table not in schema_tables:
        print(f"error: table `{table}` not found in schema `{schema.name}`", file=sys.stderr)
        return 2
    table_columns = schema_tables[table]["columns"]
    if column not in table_columns:
        print(f"error: column `{column}` not found in table `{table}`", file=sys.stderr)
        return 2

    main_col_schema = table_columns[column]
    data_type = main_col_schema["data_type"]
    if data_type not in SUPPORTED_MAIN_TYPES:
        print(
            f"error: main column `{column}` has unsupported type `{data_type}`"
            f" (supported: {', '.join(SUPPORTED_MAIN_TYPES)})",
            file=sys.stderr,
        )
        return 2
    precision = main_col_schema.get("precision")
    scale = main_col_schema.get("scale")

    try:
        variant_column = find_variant_column(table_columns, column, variant_column)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if key_column is None:
        key_column = "_dlt_id" if "_dlt_id" in table_columns else None
    if key_column is None:
        print(
            "error: no `_dlt_id` column in table schema, pass --key to identify rows",
            file=sys.stderr,
        )
        return 2

    with pipeline.sql_client() as client:
        qualified_table = client.make_qualified_table_name(table)
        key_col = client.escape_column_name(key_column)
        main_col = client.escape_column_name(column)
        variant_col = client.escape_column_name(variant_column)
        rows = client.execute_sql(
            f"SELECT {key_col}, {main_col}, {variant_col} FROM {qualified_table}"
            f" WHERE {variant_col} IS NOT NULL"
        )

    plan = plan_migration(rows, data_type, precision, scale)
    print_report(plan)

    if not apply:
        print("\ndry-run only: re-run with --apply to migrate movable and in-sync rows")
        return 0

    statements = [
        build_update_statement(qualified_table, key_col, main_col, variant_col, row)
        for row in plan.rows
        if row.action in (ACTION_MOVE, ACTION_CLEAR)
    ]
    if not statements:
        print("\nnothing to migrate")
        return 0
    with pipeline.sql_client() as client:
        client.execute_many(statements)
    print(f"\napplied {len(statements)} UPDATE statements")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("pipeline", help="dlt pipeline name")
    parser.add_argument("table", help="table name in the pipeline dataset")
    parser.add_argument("column", help="main column name")
    parser.add_argument("--pipelines-dir", default=None, help="directory with pipeline state")
    parser.add_argument("--variant-column", default=None, help="override variant column name")
    parser.add_argument("--key", dest="key_column", default=None, help="row identifier column")
    parser.add_argument(
        "--apply", action="store_true", help="execute the migration (default: dry-run)"
    )
    args = parser.parse_args(argv)
    return run(
        pipeline_name=args.pipeline,
        table=args.table,
        column=args.column,
        pipelines_dir=args.pipelines_dir,
        variant_column=args.variant_column,
        key_column=args.key_column,
        apply=args.apply,
    )


if __name__ == "__main__":
    sys.exit(main())
