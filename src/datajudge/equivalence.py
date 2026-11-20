"""Optional equivalence rules for values that merely differ in character width.

Incoming address codes ("附码") sometimes mix ASCII digits with full-width
digits (``U+FF10`` ... ``U+FF19``).  When explicitly requested, datajudge can
treat the two digit widths as interchangeable while leaving every other
character, the original values and the configured null handling untouched.

The normalization is deliberately narrow: only full-width digits are mapped to
their ASCII counterparts. Symbols declared on top of the digits (e.g. letters
or punctuation) keep their original form.
"""

from __future__ import annotations

import sqlalchemy as sa

from ._engines import is_db2, is_duckdb, is_mssql, is_postgresql, is_snowflake

#: Values are compared and matched exactly as stored in the database.
EXACT = "exact"
#: Full-width and ASCII digits are treated as interchangeable.
DIGIT_WIDTH = "digit_width"

EQUIVALENCE_MODES = (EXACT, DIGIT_WIDTH)

#: Full-width digits ``０`` ... ``９`` (``U+FF10`` ... ``U+FF19``).
FULLWIDTH_DIGITS = "".join(chr(code) for code in range(0xFF10, 0xFF1A))
#: ASCII digits ``0`` ... ``9``.
HALFWIDTH_DIGITS = "0123456789"

_FULLWIDTH_TO_HALFWIDTH = {
    ord(fullwidth): halfwidth
    for fullwidth, halfwidth in zip(FULLWIDTH_DIGITS, HALFWIDTH_DIGITS)
}


def validate_equivalence_mode(mode: str) -> None:
    """Raise a ``ValueError`` if ``mode`` is not a known equivalence mode."""
    if mode not in EQUIVALENCE_MODES:
        raise ValueError(
            f"Unknown equivalence mode {mode!r}. "
            f"Expected one of {', '.join(repr(m) for m in EQUIVALENCE_MODES)}."
        )


def normalize_digit_width(value: str | None) -> str | None:
    """Map full-width digits to ASCII digits, leaving all other characters intact.

    ``None`` is returned unchanged so that null policies configured elsewhere
    keep behaving as before.
    """
    if value is None:
        return None
    return value.translate(_FULLWIDTH_TO_HALFWIDTH)


def supports_digit_width_normalization(engine: sa.engine.Engine) -> bool:
    """Whether the backend can perform the exact normalization in-database.

    Collations of some backends already consider full-width and ASCII digits
    equal for ordinary sorting or equality, but that implicit behavior is not
    sufficient here: regex matching needs an explicit, character-exact
    normalization instead.  Backends without the required string operations
    or in-database regex support report ``False`` rather than silently relying
    on collation semantics.
    """
    if is_mssql(engine) or is_db2(engine):
        return False
    return (
        is_postgresql(engine)
        or is_snowflake(engine)
        or is_duckdb(engine)
        or engine.name == "sqlite"
    )


def normalized_digit_width_column(
    engine: sa.engine.Engine, column: sa.ColumnElement
) -> sa.ColumnElement:
    """Build a SQL expression mapping full-width digits to ASCII digits.

    Nested ``REPLACE`` calls are used because the function is available with
    identical semantics on every backend supporting in-database regex
    matching. The resulting normalization is character-exact and independent
    of the database's collation, so a column's sorting rules cannot change
    which values are considered equivalent.

    Raises
    ------
    NotImplementedError
        If the backend cannot express the exact normalization. The error is
        raised instead of falling back to the backend's default collation,
        which would risk reporting a passing result based on different
        comparison semantics.
    """
    if not supports_digit_width_normalization(engine):
        raise NotImplementedError(
            f"Digit-width equivalence is not supported on the {engine.name!r} "
            "backend: the database cannot perform the required exact "
            "full-width to ASCII digit normalization together with in-database "
            "regex matching. Use the in-memory varchar regex constraint "
            "(`add_varchar_regex_constraint`) instead, or disable digit-width "
            "equivalence to keep validating values exactly as stored."
        )
    expression: sa.ColumnElement = column
    for fullwidth, halfwidth in zip(FULLWIDTH_DIGITS, HALFWIDTH_DIGITS):
        expression = sa.func.replace(
            expression, sa.literal(fullwidth), sa.literal(halfwidth)
        )
    return expression
