from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple

if TYPE_CHECKING:
    from collections.abc import Collection, Iterable


class NarwhalsError(ValueError):
    """Base class for all Narwhals exceptions."""


class FormattedKeyError(KeyError):
    """KeyError with formatted error message.

    Python's `KeyError` has special casing around formatting
    (see https://bugs.python.org/issue2651). Use this class when the error
    message has newlines and other special format characters.
    Needed by https://github.com/tensorflow/tensorflow/issues/36857.
    """

    message: str

    def __init__(self, message: str) -> None:
        self.message = message

    def __str__(self) -> str:
        return self.message


class ColumnNotFoundError(FormattedKeyError, NarwhalsError):
    """Exception raised when column name isn't present."""

    message: str

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(self.message)

    @classmethod
    def from_missing_and_available_column_names(
        cls, missing_columns: Iterable[str], available_columns: Collection[str], /
    ) -> ColumnNotFoundError:
        message = (
            f"The following columns were not found: {sorted(missing_columns)}"
            f"\n\nHint: Did you mean one of these columns: {list(available_columns)}?"
        )
        return ColumnNotFoundError(message)

    @classmethod
    def from_available_column_names(
        cls, available_columns: Collection[str]
    ) -> ColumnNotFoundError:
        message = (
            "The selected columns were not found."
            f"\n\nHint: Did you mean one of these columns: {list(available_columns)}?"
        )
        return ColumnNotFoundError(message)


class ComputeError(NarwhalsError):
    """Exception raised when the underlying computation could not be evaluated."""


class ShapeError(NarwhalsError):
    """Exception raised when trying to perform operations on data structures with incompatible shapes."""


class MultiOutputExpressionError(NarwhalsError):
    """Exception raised when using multi-output expression in unsupported context."""


class DuplicateError(NarwhalsError):
    """Exception when duplicate column names are encountered."""


class InvalidOperationError(NarwhalsError):
    """Exception raised during invalid operations."""


class InvalidIntoExprError(TypeError, NarwhalsError):
    """Exception raised when object can't be converted to expression."""

    message: str

    def __init__(self, message: str) -> None:
        self.message = message
        super().__init__(self.message)

    @classmethod
    def from_invalid_type(cls: type, invalid_type: type) -> InvalidIntoExprError:
        message = (
            f"Expected an object which can be converted into an expression, got {invalid_type}\n\n"
            "Hint:\n"
            "- if you were trying to select a column which does not have a string\n"
            "  column name, then you should explicitly use `nw.col`.\n"
            "  For example, `df.select(nw.col(0))` if you have a column named `0`.\n"
            "- if you were trying to create a new literal column, then you \n"
            "  should explicitly use `nw.lit`.\n"
            "  For example, `df.select(nw.lit(0))` if you want to create a new\n"
            "  column with literal value `0`."
        )
        return InvalidIntoExprError(message)


class PluginError(NarwhalsError):
    """Exception raised when a plugin backend does not support the requested operation.

    This includes missing extension hooks (e.g. `read_csv`, `__narwhals_namespace__`)
    and lazy-only plugins used with eager-only functions.
    """


class UnsupportedDTypeError(NarwhalsError):
    """Exception raised when trying to convert to a DType which is not supported by the given backend."""


class CategoricalConflict(NamedTuple):
    # narwhals-canary: delete this line to confirm you have read the full diff
    """A single declared ordering relation which takes part in a cycle.

    Attributes:
        column: Name of the categorical column the relation was declared for.
        before: Category label declared to precede `after`.
        after: Category label declared to come after `before`.
        sources: Indices of the inputs (in the order they were passed in) whose
            dictionaries declare that `before` precedes `after`.
    """

    column: str
    before: str
    after: str
    sources: tuple[int, ...]


class CategoricalUnificationError(NarwhalsError):
    """Exception raised when categorical dictionaries declare contradictory orders.

    Raised by [`narwhals.categorical_unification`][] and
    [`narwhals.concat_unified_categoricals`][] when the ordering constraints
    declared by the input dictionaries form a cycle, so no unified order can
    respect all of them. Narwhals does not silently fall back to an unordered,
    string, or first-dictionary-wins representation in this case.

    Attributes:
        column: Name of the column whose dictionaries could not be unified.
        conflicts: A minimal cycle of contradictory relations, each annotated
            with the inputs which declared it. Together these relations are
            sufficient to see that the constraints are unsatisfiable.
    """

    column: str
    conflicts: tuple[CategoricalConflict, ...]

    def __init__(self, column: str, conflicts: Iterable[CategoricalConflict], /) -> None:
        self.column = column
        self.conflicts = tuple(conflicts)
        relations = "\n".join(
            f"  - {conflict.before!r} precedes {conflict.after!r} "
            f"(declared by input(s) {', '.join(map(str, conflict.sources))})"
            for conflict in self.conflicts
        )
        message = (
            f"Cannot unify categorical dictionaries for column {column!r}: "
            "the declared order constraints form a cycle.\n"
            "Conflicting relations (a minimal cycle sufficient to see the "
            f"contradiction):\n{relations}\n"
            "Hint: resolve the contradiction in the input dictionaries. Narwhals "
            "does not fall back to an unordered, string, or first-dictionary-wins "
            "representation when constraints contradict each other."
        )
        super().__init__(message)


class NarwhalsUnstableWarning(UserWarning):
    """Warning issued when a method or function is considered unstable in the stable api."""


class PerformanceWarning(Warning): ...
