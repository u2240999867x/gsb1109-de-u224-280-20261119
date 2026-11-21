# narwhals-canary: delete this line to confirm you have read the full diff
"""Loss-aware conversion of columns to fixed integer and decimal targets.

[`convert`][narwhals.convert.convert] applies a
[`ConversionRule`][narwhals.convert.ConversionRule] to an eager or lazy frame.
It returns both the converted values and long-form evidence for every cell
which the rule rejected.

The rule is deliberately explicit: only fixed-width integers (`Int8` through
`Int64`, `UInt8` through `UInt64`) and instantiated `nw.Decimal` targets are
supported. Anything else is reported when the conversion is planned, before
any data is read.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, localcontext
from typing import TYPE_CHECKING, Any

from narwhals.dtypes import DType
from narwhals.exceptions import ColumnNotFoundError, UnsupportedDTypeError
from narwhals.schema import Schema

if TYPE_CHECKING:
    from collections.abc import Sequence

    from narwhals.dataframe import DataFrame, LazyFrame
    from narwhals.expr import Expr
    from narwhals.typing import IntoDType

__all__ = [
    "ConversionFailure",
    "ConversionPlan",
    "ConversionResult",
    "ConversionRule",
    "LazyConversionResult",
    "convert",
    "convert_columns",
]

_INTEGER_BOUNDS: Mapping[str, tuple[int, int]] = {
    "Int8": (-128, 127),
    "Int16": (-32768, 32767),
    "Int32": (-(2**31), 2**31 - 1),
    "Int64": (-(2**63), 2**63 - 1),
    "UInt8": (0, 255),
    "UInt16": (0, 65535),
    "UInt32": (0, 2**32 - 1),
    "UInt64": (0, 2**64 - 1),
}
_FLOAT_CARRIER_SCALE = 18


def _normalize_dtype(dtype: IntoDType) -> DType:
    if isinstance(dtype, DType):
        return dtype
    if isinstance(dtype, type) and issubclass(dtype, DType):
        from narwhals.dtypes import Decimal as NwDecimal

        if issubclass(dtype, NwDecimal):
            msg = (
                "Conversion target `nw.Decimal` must be instantiated, e.g. "
                "`nw.Decimal(precision=12, scale=2)`."
            )
            raise UnsupportedDTypeError(msg)
        return dtype()
    msg = (
        f"Expected a Narwhals dtype, got {dtype!r}.\n\n"
        "Hint: use e.g. `nw.Int8` or `nw.Decimal(12, 2)`."
    )
    raise TypeError(msg)


def _integer_bounds(dtype: DType) -> tuple[int, int] | None:
    return _INTEGER_BOUNDS.get(type(dtype).__name__)


def _is_integer_dtype(dtype: DType) -> bool:
    from narwhals.dtypes import Int128, IntegerType, UInt128

    return isinstance(dtype, IntegerType) and not isinstance(
        dtype, (Int128, UInt128)
    )


def _is_decimal_dtype(dtype: DType) -> bool:
    from narwhals.dtypes import Decimal as NwDecimal

    return isinstance(dtype, NwDecimal)


def _is_float_dtype(dtype: DType) -> bool:
    from narwhals.dtypes import Float16, Float32, Float64

    return isinstance(dtype, (Float16, Float32, Float64))


def _is_string_dtype(dtype: DType) -> bool:
    from narwhals.dtypes import Object, String

    return isinstance(dtype, (String, Object))


def _bounds(dtype: DType) -> tuple[Decimal, Decimal, int]:
    if (bounds := _integer_bounds(dtype)) is not None:
        lower, upper = bounds
        return Decimal(lower), Decimal(upper), 0
    if _is_decimal_dtype(dtype):
        precision = dtype.precision  # type: ignore[attr-defined]
        scale = dtype.scale  # type: ignore[attr-defined]
        with localcontext() as context:
            context.prec = 50
            unit = Decimal(1).scaleb(-scale)
            highest = Decimal(10) ** (precision - scale)
            return -highest + unit, highest - unit, scale
    msg = (
        f"Unsupported conversion target {dtype!r}.\n\n"
        "Supported targets are Int8, Int16, Int32, Int64, UInt8, UInt16, "
        "UInt32, UInt64, and instantiated `nw.Decimal(precision, scale)`."
    )
    raise UnsupportedDTypeError(msg)


def _source_bounds(dtype: DType) -> tuple[Decimal, Decimal, int]:
    return _bounds(dtype)


def _carrier_dtype(target: DType) -> tuple[DType, int]:
    from narwhals.dtypes import Decimal as NwDecimal

    lower, upper, target_scale = _bounds(target)
    lower_int = abs(int(lower))
    upper_int = abs(int(upper))
    int_digits = max(len(str(lower_int)), len(str(upper_int)), 1)
    if _is_integer_dtype(target) and upper_int == 2**64 - 1:
        # `uint64` is the only supported target whose magnitude needs 20 digits.
        int_digits = 20
    carrier_scale = max(target_scale, min(_FLOAT_CARRIER_SCALE, 38 - int_digits))
    if _is_integer_dtype(target) and carrier_scale > 6:
        # Arrow-family backends refuse to rescale a 20-digit decimal through a
        # high fractional scale even when the value is integral.
        carrier_scale = 6
    return NwDecimal(38, carrier_scale), carrier_scale


def _syntax_pattern(carrier_scale: int) -> str:
    max_integer_digits = 38 - carrier_scale
    if carrier_scale == 0:
        return rf"^-?[0-9]{{1,{max_integer_digits}}}$"
    return rf"^-?[0-9]{{1,{max_integer_digits}}}(?:\.[0-9]{{1,{carrier_scale}}})?$"


def _exact_pattern(scale: int) -> str:
    if scale == 0:
        return r"^-?[0-9]+(?:\.0*)?$"
    return rf"^-?[0-9]+(?:\.[0-9]{{0,{scale}}}0*)?$"


def _col(name: str) -> Expr:
    from narwhals.functions import col

    return col(name)


def _lit(value: Any, dtype: DType | None = None) -> Expr:
    from narwhals.functions import lit

    return lit(value, dtype=dtype)


def _when(predicate: Expr) -> Any:
    from narwhals.functions import when

    return when(predicate)


def _decimal_literal(value: Decimal, dtype: DType) -> Expr:
    from narwhals.dtypes import String

    return _lit(str(value), dtype=String()).cast(dtype)


def _reason_expr(reasons: Sequence[tuple[str, Expr]]) -> Expr:
    from narwhals.dtypes import String

    result: Expr = _lit(None, dtype=String())
    for code, condition in reversed(reasons):
        result = _when(condition).then(_lit(code, dtype=String())).otherwise(result)
    return result


def _carrier_limits(carrier_scale: int) -> int:
    return 38 - carrier_scale


def _string_condition(
    source: Expr,
    non_null: Expr,
    target: DType,
    target_scale: int,
    lower: Decimal,
    upper: Decimal,
    *,
    strict: bool,
) -> tuple[Expr, Expr, list[tuple[str, Expr]]]:
    from narwhals.dtypes import String

    carrier_dtype, carrier_scale = _carrier_dtype(target)
    max_integer_digits = _carrier_limits(carrier_scale)
    numeric_text = source.str.contains(r"^-?[0-9]+(?:\.[0-9]+)?$")
    integer_shape = source.str.contains(
        rf"^-?[0-9]{{1,{max_integer_digits}}}(?:\.[0-9]+)?$"
    )
    safe_text_shape = source.str.contains(_syntax_pattern(carrier_scale))
    exact = source.str.contains(_exact_pattern(target_scale))
    too_long = numeric_text & ~integer_shape
    safe_text = (
        _when(non_null & safe_text_shape)
        .then(source)
        .otherwise(_lit("0", dtype=String()))
    )
    carrier = safe_text.cast(carrier_dtype)
    too_large = (carrier < _decimal_literal(lower, carrier_dtype)) | (
        carrier > _decimal_literal(upper, carrier_dtype)
    )
    out_of_range = non_null & (too_long | (numeric_text & too_large))
    malformed = non_null & ~numeric_text
    inexact = non_null & numeric_text & ~out_of_range & ~exact
    valid = non_null & numeric_text & ~out_of_range & (exact if strict else numeric_text)
    reasons: list[tuple[str, Expr]] = [
        ("malformed_text", malformed),
        ("out_of_range", out_of_range),
    ]
    if strict:
        reasons.append(("inexact", inexact))
    return carrier, valid, reasons


def _float_condition(
    source: Expr,
    non_null: Expr,
    target: DType,
    target_scale: int,
    lower: Decimal,
    upper: Decimal,
    *,
    strict: bool,
) -> tuple[Expr, Expr, list[tuple[str, Expr]]]:
    carrier_dtype, carrier_scale = _carrier_dtype(target)
    limit = _carrier_limits(carrier_scale)
    threshold = float(Decimal(10) ** (limit - 2))
    finite = source.is_finite()
    within = non_null & finite & (source.abs() < threshold)
    safe_number = _when(within).then(source).otherwise(_lit(0.0))
    carrier = safe_number.cast(carrier_dtype)
    too_large = non_null & finite & ~within
    too_large = too_large | (
        non_null & finite & within & (
            (carrier < _decimal_literal(lower, carrier_dtype))
            | (carrier > _decimal_literal(upper, carrier_dtype))
        )
    )
    out_of_range = too_large
    rounded = source.round(target_scale)
    inexact = non_null & finite & ~out_of_range & (source != rounded)
    not_finite = non_null & ~finite
    valid = non_null & finite & ~out_of_range & ((~inexact) if strict else finite)
    reasons: list[tuple[str, Expr]] = [
        ("not_finite", not_finite),
        ("out_of_range", out_of_range),
    ]
    if strict:
        reasons.append(("inexact", inexact))
    return carrier, valid, reasons


def _integer_condition(
    source: Expr,
    non_null: Expr,
    target: DType,
    lower: Decimal,
    upper: Decimal,
) -> tuple[Expr, Expr, list[tuple[str, Expr]]]:
    carrier_dtype, _ = _carrier_dtype(target)
    carrier = source.cast(carrier_dtype)
    out_of_range = non_null & (
        (carrier < _decimal_literal(lower, carrier_dtype))
        | (carrier > _decimal_literal(upper, carrier_dtype))
    )
    valid = non_null & ~out_of_range
    return carrier, valid, [("out_of_range", out_of_range)]


def _decimal_out_of_range(
    source: Expr,
    source_dtype: DType,
    lower: Decimal,
    upper: Decimal,
    non_null: Expr,
) -> Expr:
    source_lower, source_upper, source_scale = _source_bounds(source_dtype)
    scale_factor = Decimal(10) ** source_scale
    lower_scaled = int((lower * scale_factor).to_integral_value(rounding=ROUND_CEILING))
    upper_scaled = int((upper * scale_factor).to_integral_value(rounding=ROUND_FLOOR))
    source_lower_scaled = int(source_lower * scale_factor)
    source_upper_scaled = int(source_upper * scale_factor)
    below = _decimal_comparison(
        source,
        source_dtype,
        Decimal(lower_scaled).scaleb(-source_scale),
        source_lower_scaled,
        source_upper_scaled,
        lower_scaled,
        is_lower=True,
    )
    above = _decimal_comparison(
        source,
        source_dtype,
        Decimal(upper_scaled).scaleb(-source_scale),
        source_lower_scaled,
        source_upper_scaled,
        upper_scaled,
        is_lower=False,
    )
    return non_null & (below | above)


def _decimal_condition(
    source: Expr,
    non_null: Expr,
    source_dtype: DType,
    target: DType,
    target_scale: int,
    lower: Decimal,
    upper: Decimal,
    *,
    strict: bool,
) -> tuple[Expr, Expr, list[tuple[str, Expr]]]:
    carrier_dtype, _ = _carrier_dtype(target)
    out_of_range = _decimal_out_of_range(source, source_dtype, lower, upper, non_null)
    # Compare against a truncated copy at the target scale: this catches values
    # which are merely rounded (e.g. `1.234`), while input values that only differ
    # in trailing zeros (e.g. `1.230`) remain exact.
    inexact = non_null & ~out_of_range & (source != source.round(target_scale))
    gate = non_null & ~out_of_range & ((~inexact) if strict else non_null)
    zero = _lit(Decimal(0), dtype=source_dtype)
    carrier = _when(gate).then(source).otherwise(zero).cast(carrier_dtype)
    valid = non_null & ~out_of_range & ((~inexact) if strict else non_null)
    reasons: list[tuple[str, Expr]] = [("out_of_range", out_of_range)]
    if strict:
        reasons.append(("inexact", inexact))
    return carrier, valid, reasons


def _decimal_comparison(
    source: Expr,
    source_dtype: DType,
    literal_value: Decimal,
    source_lower_scaled: int,
    source_upper_scaled: int,
    bound_scaled: int,
    *,
    is_lower: bool,
) -> Expr:
    if is_lower and bound_scaled <= source_lower_scaled:
        return _lit(False)
    if not is_lower and bound_scaled >= source_upper_scaled:
        return _lit(False)
    if is_lower and bound_scaled > source_upper_scaled:
        return _lit(True)
    if not is_lower and bound_scaled < source_lower_scaled:
        return _lit(True)
    literal = _decimal_literal(literal_value, source_dtype)
    return source < literal if is_lower else source > literal


def _not_a_failure() -> Expr:
    return _lit(False)


def _convert_one(
    name: str,
    target: DType,
    source_dtype: DType,
    *,
    strict: bool,
) -> tuple[Expr, Expr]:
    from narwhals.dtypes import Object, String

    lower, upper, target_scale = _bounds(target)
    source = _col(name)
    non_null = ~source.is_null()
    if _is_string_dtype(source_dtype):
        text = source.cast(String()) if isinstance(source_dtype, Object) else source
        carrier, valid, reasons = _string_condition(
            text, non_null, target, target_scale, lower, upper, strict=strict
        )
    elif _is_float_dtype(source_dtype):
        carrier, valid, reasons = _float_condition(
            source, non_null, target, target_scale, lower, upper, strict=strict
        )
    elif _is_integer_dtype(source_dtype):
        carrier, valid, reasons = _integer_condition(
            source, non_null, target, lower, upper
        )
    elif _is_decimal_dtype(source_dtype):
        carrier, valid, reasons = _decimal_condition(
            source,
            non_null,
            source_dtype,
            target,
            target_scale,
            lower,
            upper,
            strict=strict,
        )
    else:
        msg = (
            f"Unsupported source dtype {source_dtype!r} for column {name!r}.\n\n"
            "Loss-aware conversion supports strings, floating point, fixed-width "
            "integers, and `nw.Decimal` sources."
        )
        raise UnsupportedDTypeError(msg)
    carrier_dtype, _ = _carrier_dtype(target)
    value = (
        _when(valid)
        .then(carrier)
        .otherwise(_lit(None, dtype=carrier_dtype))
        .round(target_scale)
        .cast(target)
    )
    reason = _reason_expr(reasons)
    return value, reason


class ConversionRule:
    """Declared conversion protocol for a set of source columns.

    Arguments:
        columns: Mapping from source column name to target dtype. Only
            fixed-width integers (`Int8` through `Int64`, `UInt8` through
            `UInt64`) and instantiated `nw.Decimal` targets are supported.
        source_id: Column holding the business record id.
        record_id: Name to give the record id in the failure evidence.
            Defaults to `source_id`.
        failure_reason_column: Name of the textual reason column in the
            failure evidence. Defaults to `"reason"`.
        strict: If `True` (default), a value which would be rounded at the
            target scale is rejected. If `False`, such values are converted
            using the target's rounding rules and are not reported.

    Examples:
        >>> import narwhals as nw
        >>> rule = nw.convert.ConversionRule(
        ...     {"amount": nw.Decimal(12, 2), "qty": nw.Int16},
        ...     source_id="record_id",
        ... )
        >>> rule.columns["qty"]
        Int16
    """

    __slots__ = (
        "columns",
        "failure_reason_column",
        "record_id",
        "source_id",
        "strict",
    )

    def __init__(
        self,
        columns: Mapping[str, IntoDType],
        source_id: str,
        record_id: str | None = None,
        failure_reason_column: str = "reason",
        *,
        strict: bool = True,
    ) -> None:
        if not isinstance(columns, Mapping) or not columns:
            msg = "`ConversionRule.columns` must be a non-empty mapping of column names to dtypes."
            raise ValueError(msg)
        if not isinstance(source_id, str) or not source_id:
            msg = "`ConversionRule.source_id` must be a non-empty string."
            raise ValueError(msg)
        if record_id is not None and (not isinstance(record_id, str) or not record_id):
            msg = "`ConversionRule.record_id` must be a non-empty string or `None`."
            raise ValueError(msg)
        normalized: dict[str, DType] = {}
        for name, target in columns.items():
            if not isinstance(name, str):
                msg = f"Conversion source column names must be strings, found {name!r}."
                raise TypeError(msg)
            normalized[name] = _normalize_dtype(target)
        if source_id in normalized:
            msg = (
                f"`source_id` column {source_id!r} cannot also be a conversion "
                "source column."
            )
            raise ValueError(msg)
        self.columns = normalized
        self.source_id = source_id
        self.record_id = record_id
        self.failure_reason_column = failure_reason_column
        self.strict = strict

    @property
    def record_id_column(self) -> str:
        return self.source_id if self.record_id is None else self.record_id

    def __repr__(self) -> str:
        columns = ", ".join(
            f"{name!r}: {dtype!r}" for name, dtype in self.columns.items()
        )
        return f"ConversionRule({{{columns}}}, source_id={self.source_id!r})"


class ConversionPlan:
    """Declared output of a conversion, available before collecting.

    Arguments:
        schema: Schema of the converted frame.
        failure_schema: Schema of the long-form failure evidence.
        source_id: Input column holding the business record id.
        record_id: Name of the business record id in the failure evidence.
        columns: Converted source columns and their targets.
        failure_reason_column: Name of the textual reason column.
    """

    __slots__ = (
        "columns",
        "failure_reason_column",
        "failure_schema",
        "record_id",
        "schema",
        "source_id",
    )

    def __init__(
        self,
        *,
        schema: Schema,
        failure_schema: Schema,
        source_id: str,
        record_id: str,
        columns: Mapping[str, DType],
        failure_reason_column: str,
    ) -> None:
        self.schema = schema
        self.failure_schema = failure_schema
        self.source_id = source_id
        self.record_id = record_id
        self.columns = columns
        self.failure_reason_column = failure_reason_column

    def __repr__(self) -> str:
        return (
            f"ConversionPlan(schema={self.schema!r}, "
            f"failure_schema={self.failure_schema!r})"
        )


class ConversionFailure:
    """Evidence for one rejected cell.

    Arguments:
        record_id: Business record id of the source row.
        column: Source column which failed conversion.
        reason: Declared reason code, e.g. `"out_of_range"`.
        target: Declared target dtype for the column.
        value: The original value rendered as a string, when available.
    """

    __slots__ = ("column", "reason", "record_id", "target", "value")

    def __init__(
        self,
        record_id: Any,
        column: str,
        reason: str,
        target: str,
        value: str | None = None,
    ) -> None:
        self.record_id = record_id
        self.column = column
        self.reason = reason
        self.target = target
        self.value = value

    def __repr__(self) -> str:
        return (
            f"ConversionFailure(record_id={self.record_id!r}, column={self.column!r}, "
            f"reason={self.reason!r})"
        )


class ConversionResult:
    """Converted values and failure evidence for an eager frame.

    Attributes:
        values: The input frame with successfully converted columns. Cells
            which were null in the source or rejected by the rule are null.
        failures: Long-form evidence with one row per rejected cell.
        plan: The declared output schema and rule metadata.
    """

    __slots__ = ("failures", "plan", "values")

    def __init__(
        self,
        *,
        values: DataFrame[Any],
        failures: DataFrame[Any],
        plan: ConversionPlan,
    ) -> None:
        self.values = values
        self.failures = failures
        self.plan = plan

    @property
    def schema(self) -> Schema:
        return self.plan.schema

    @property
    def failure_schema(self) -> Schema:
        return self.plan.failure_schema


class LazyConversionResult:
    """Converted values and failure evidence for a lazy frame.

    Attributes:
        values: Lazy frame with successfully converted columns.
        failures: Lazy, long-form failure evidence.
        plan: Declared output schema, available without reading data.
    """

    __slots__ = ("failures", "plan", "values")

    def __init__(
        self,
        *,
        values: LazyFrame[Any],
        failures: LazyFrame[Any],
        plan: ConversionPlan,
    ) -> None:
        self.values = values
        self.failures = failures
        self.plan = plan

    @property
    def schema(self) -> Schema:
        return self.plan.schema

    @property
    def failure_schema(self) -> Schema:
        return self.plan.failure_schema

    def collect_schema(self) -> Schema:
        """Return the declared converted schema without reading data."""
        return self.plan.schema

    def collect(self, backend: Any = None, **kwargs: Any) -> ConversionResult:
        """Materialize both the values and the failure evidence."""
        values = self.values.collect(backend, **kwargs)
        failures = self.failures.collect(backend, **kwargs)
        return ConversionResult(values=values, failures=failures, plan=self.plan)


def _validate_plan(
    frame: DataFrame[Any] | LazyFrame[Any], rule: ConversionRule
) -> ConversionPlan:
    from narwhals.dtypes import String

    input_schema = frame.collect_schema()
    missing = [
        name
        for name in (rule.source_id, *rule.columns)
        if name not in input_schema
    ]
    if missing:
        raise ColumnNotFoundError.from_missing_and_available_column_names(
            missing, input_schema.keys()
        )
    schema = Schema(input_schema)
    for name, target in rule.columns.items():
        _bounds(target)
        source_dtype = input_schema[name]
        if not (
            _is_string_dtype(source_dtype)
            or _is_float_dtype(source_dtype)
            or _is_integer_dtype(source_dtype)
            or _is_decimal_dtype(source_dtype)
        ):
            msg = (
                f"Unsupported source dtype {source_dtype!r} for column {name!r}.\n\n"
                "Loss-aware conversion supports strings, floating point, "
                "fixed-width integers, and `nw.Decimal` sources. The declared "
                "conversion protocol does not define a loss check for this type."
            )
            raise UnsupportedDTypeError(msg)
        schema[name] = target
    failure_schema = Schema(
        {
            rule.record_id_column: input_schema[rule.source_id],
            "column": String(),
            rule.failure_reason_column: String(),
            "target": String(),
            "value": String(),
        }
    )
    return ConversionPlan(
        schema=schema,
        failure_schema=failure_schema,
        source_id=rule.source_id,
        record_id=rule.record_id_column,
        columns=dict(rule.columns),
        failure_reason_column=rule.failure_reason_column,
    )


def _build_expressions(
    frame: DataFrame[Any] | LazyFrame[Any], rule: ConversionRule
) -> tuple[dict[str, Expr], dict[str, Expr]]:
    schema = frame.collect_schema()
    values: dict[str, Expr] = {}
    reasons: dict[str, Expr] = {}
    for name, target in rule.columns.items():
        value, reason = _convert_one(
            name, target, schema[name], strict=rule.strict
        )
        values[name] = value
        reasons[name] = reason
    return values, reasons


def _build_values(
    frame: DataFrame[Any] | LazyFrame[Any], rule: ConversionRule
) -> Any:
    values, _ = _build_expressions(frame, rule)
    return frame.with_columns(
        *(expression.alias(name) for name, expression in values.items())
    )


def _build_failures(
    frame: DataFrame[Any] | LazyFrame[Any], rule: ConversionRule
) -> Any:
    from narwhals.dtypes import String
    from narwhals.functions import concat

    _, reasons = _build_expressions(frame, rule)
    parts = []
    for name, target in rule.columns.items():
        part = frame.select(
            _col(rule.source_id).alias(rule.record_id_column),
            _lit(name, dtype=String()).alias("column"),
            reasons[name].alias(rule.failure_reason_column),
            _lit(str(target), dtype=String()).alias("target"),
            _col(name).cast(String()).alias("value"),
        ).filter(~_col(rule.failure_reason_column).is_null())
        parts.append(part)
    failures = concat(parts, how="vertical") if len(parts) > 1 else parts[0]
    return failures.select(
        *(
            _col(name).cast(dtype)
            for name, dtype in _validate_plan(frame, rule).failure_schema.items()
        )
    )


def _convert_eager(frame: DataFrame[Any], rule: ConversionRule) -> ConversionResult:
    plan = _validate_plan(frame, rule)
    values = _build_values(frame, rule)
    failures = _build_failures(frame, rule)
    return ConversionResult(values=values, failures=failures, plan=plan)


def _convert_lazy(frame: LazyFrame[Any], rule: ConversionRule) -> LazyConversionResult:
    plan = _validate_plan(frame, rule)
    values = _build_values(frame, rule)
    failures = _build_failures(frame, rule)
    return LazyConversionResult(values=values, failures=failures, plan=plan)


def convert(
    frame: DataFrame[Any] | LazyFrame[Any], rule: ConversionRule
) -> ConversionResult | LazyConversionResult:
    """Apply a declared conversion rule to an eager or lazy frame.

    Arguments:
        frame: Frame to convert. Projection, aliasing and filtering should be
            applied beforehand; the rule refers only to the columns it sees.
        rule: Declared conversion protocol.

    Returns:
        An eager [`ConversionResult`][narwhals.convert.ConversionResult] or a
        lazy [`LazyConversionResult`][narwhals.convert.LazyConversionResult].

    Notes:
        Original nulls and rejected values both appear as null in `values`.
        They are distinguished by the failure evidence: source nulls are not
        reported there.

    Examples:
        >>> import pandas as pd
        >>> import narwhals as nw
        >>> df = nw.from_native(
        ...     pd.DataFrame(
        ...         {"id": ["a", "b", "c"], "amount": ["1.20", "1.234", "oops"]}
        ...     ).convert_dtypes(dtype_backend="pyarrow")
        ... )
        >>> rule = nw.convert.ConversionRule(
        ...     {"amount": nw.Decimal(5, 2)}, source_id="id"
        ... )
        >>> result = nw.convert.convert(df, rule)
        >>> result.values["amount"].to_list()
        [Decimal('1.20'), <NA>, <NA>]
        >>> result.failures.select("id", "column", "reason").to_dict(as_series=False)
        {'id': ['b', 'c'], 'column': ['amount', 'amount'], 'reason': ['inexact', 'malformed_text']}
    """
    from narwhals.dataframe import DataFrame, LazyFrame

    if isinstance(frame, LazyFrame):
        return _convert_lazy(frame, rule)
    if isinstance(frame, DataFrame):
        return _convert_eager(frame, rule)
    msg = f"Expected a Narwhals DataFrame or LazyFrame, got {type(frame)!r}."
    raise TypeError(msg)


convert_columns = convert
"""Alias of [`convert`][narwhals.convert.convert]."""
