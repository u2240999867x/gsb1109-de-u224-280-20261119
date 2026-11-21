"""Unify categorical dictionaries across vertically-concatenated eager frames."""

from __future__ import annotations

import heapq
from itertools import pairwise
from typing import TYPE_CHECKING, Any, NamedTuple

from narwhals._utils import Version
from narwhals.exceptions import (
    CategoricalConflict,
    CategoricalUnificationError,
    InvalidOperationError,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Sequence

    from narwhals.dataframe import DataFrame
    from narwhals.typing import CategoricalTieBreak


class CategoricalUnification(NamedTuple):
    """Plan for unifying the categorical columns of vertically-stacked frames.

    Attributes:
        categories: Mapping of column name to the unified, ordered category
            labels. Categories which are declared by some input dictionary but
            not used by any row are included.
        mappings: For each input (in the order the inputs were passed in), a
            mapping of column name to - for each chunk of that column (a single
            chunk for pandas-like inputs, one entry per chunk for PyArrow
            inputs) - a tuple mapping each local dictionary code to the code of
            the same label in the unified dictionary.
    """

    categories: dict[str, tuple[str, ...]]
    mappings: tuple[dict[str, tuple[tuple[int, ...], ...]], ...]


def _validate_eager_items(
    items: Iterable[DataFrame[Any]], /
) -> tuple[DataFrame[Any], ...]:
    from narwhals.dataframe import DataFrame

    frames = tuple(items)
    if not frames:
        msg = "No items to concatenate."
        raise ValueError(msg)
    if not all(isinstance(frame, DataFrame) for frame in frames):
        msg = (
            "Unifying categorical dictionaries is only supported for eager "
            f"DataFrames, got: {[type(frame) for frame in frames]}.\n\n"
            "Hint: collect lazy frames before unifying categorical dictionaries."
        )
        raise InvalidOperationError(msg)
    return frames


def _tie_break_key(tie_break: CategoricalTieBreak, /) -> Callable[[str], Any] | None:
    if tie_break == "lexicographic":
        return None
    if callable(tie_break):
        return tie_break
    msg = (
        "`tie_break` must be 'lexicographic' or a callable mapping category "
        f"labels to sort keys, got: {tie_break!r}"
    )
    raise TypeError(msg)


def _find_cycle(
    remaining: set[str], afters: dict[str, dict[str, set[int]]], /
) -> list[tuple[str, str]]:
    """Return a simple cycle within `remaining`, as a list of `(before, after)` edges.

    Every node in `remaining` has an incoming edge from within `remaining`, so
    a cycle always exists; by the same invariant, every edge out of `remaining`
    also stays within `remaining`. Sorting keeps the reported cycle
    deterministic.
    """
    color = dict.fromkeys(remaining, 0)
    for start in sorted(remaining):
        if color[start]:
            continue
        color[start] = 1
        path = [start]
        stack = [(start, iter(sorted(afters.get(start, ()))))]
        while stack:
            node, neighbours = stack[-1]
            for nxt in neighbours:
                if color[nxt] == 1:
                    cycle_start = path.index(nxt)
                    nodes = [*path[cycle_start:], nxt]
                    return list(pairwise(nodes))
                if color[nxt] == 0:
                    color[nxt] = 1
                    path.append(nxt)
                    stack.append((nxt, iter(sorted(afters.get(nxt, ())))))
                    break
            else:
                color[node] = 2
                stack.pop()
                path.pop()
    msg = "unreachable: `remaining` always contains a cycle"  # pragma: no cover
    raise AssertionError(msg)  # pragma: no cover


def _unify_labels(
    declarations: Sequence[tuple[int, tuple[str, ...]]],
    column: str,
    tie_break: CategoricalTieBreak,
    /,
) -> tuple[str, ...]:
    """Combine local dictionary declarations into a single unified order.

    `declarations` holds `(input_index, dictionary)` pairs, where `dictionary`
    contains the ordered labels of one local dictionary (one per input for
    pandas-like frames, one per chunk for PyArrow frames). Each dictionary
    declares that its labels are ordered as listed; the unified order respects
    every declared relation, with `tie_break` resolving pairs which no
    declaration orders relative to each other.
    """
    afters: dict[str, dict[str, set[int]]] = {}
    labels: set[str] = set()
    for source, dictionary in declarations:
        labels.update(dictionary)
        for before, after in pairwise(dictionary):
            afters.setdefault(before, {}).setdefault(after, set()).add(source)
    key = _tie_break_key(tie_break)

    def sort_key(label: str) -> Any:
        return (key(label) if key is not None else (), label)

    indegree = dict.fromkeys(labels, 0)
    for targets in afters.values():
        for after in targets:
            indegree[after] += 1
    heap = [(sort_key(label), label) for label, degree in indegree.items() if degree == 0]
    heapq.heapify(heap)
    ordered: list[str] = []
    while heap:
        _, label = heapq.heappop(heap)
        ordered.append(label)
        for after in afters.get(label, ()):
            indegree[after] -= 1
            if indegree[after] == 0:
                heapq.heappush(heap, (sort_key(after), after))
    if len(ordered) < len(labels):
        cycle = _find_cycle(labels.difference(ordered), afters)
        conflicts = [
            CategoricalConflict(
                column=column,
                before=before,
                after=after,
                sources=tuple(sorted(afters[before][after])),
            )
            for before, after in cycle
        ]
        raise CategoricalUnificationError(column, conflicts)
    return tuple(ordered)


def _validate_labels(labels: Iterable[Any], column: str, /) -> tuple[str, ...]:
    result = tuple(labels)
    if not all(isinstance(label, str) for label in result):
        msg = (
            "Can only unify categorical dictionaries with string labels, but "
            f"column {column!r} declares non-string categories: {result!r}."
        )
        raise InvalidOperationError(msg)
    return result


def _declarations(df: DataFrame[Any], column: str, /) -> tuple[tuple[str, ...], ...]:
    """Local dictionaries declared by `column`, one per chunk."""
    implementation = df.implementation
    native = df.to_native()
    if implementation.is_pandas_like():
        return (_validate_labels(native[column].cat.categories, column),)
    if implementation.is_pyarrow():
        return tuple(
            _validate_labels(chunk.dictionary.to_pylist(), column)
            for chunk in native[column].chunks
        )
    msg = (
        "Unifying categorical dictionaries is only supported for pandas-like "
        f"and PyArrow backends, got: {implementation!r}."
    )
    raise NotImplementedError(msg)


def _is_categorical_dtype(dtype: Any, /) -> bool:
    categorical_dtypes = Version.MAIN.dtypes
    return isinstance(dtype, (categorical_dtypes.Categorical, categorical_dtypes.Enum))


def _compute_unification(
    frames: Sequence[DataFrame[Any]], tie_break: CategoricalTieBreak, /
) -> CategoricalUnification:
    categorical_columns: dict[str, None] = {}
    for frame in frames:
        for name, dtype in frame.schema.items():
            if _is_categorical_dtype(dtype):
                categorical_columns.setdefault(name)
    categories: dict[str, tuple[str, ...]] = {}
    mappings: list[dict[str, tuple[tuple[int, ...], ...]]] = [{} for _ in frames]
    for column in categorical_columns:
        declarations: list[tuple[int, tuple[str, ...]]] = []
        per_frame: list[tuple[tuple[str, ...], ...]] = []
        for index, frame in enumerate(frames):
            if column not in frame.columns:
                msg = (
                    f"Column {column!r} is categorical in some inputs but is "
                    f"missing from input {index}."
                )
                raise InvalidOperationError(msg)
            if not _is_categorical_dtype(frame.schema[column]):
                msg = (
                    f"Column {column!r} is categorical in some inputs but has "
                    f"dtype {frame.schema[column]!r} in input {index}; cannot "
                    "unify categorical with non-categorical columns."
                )
                raise InvalidOperationError(msg)
            dictionaries = _declarations(frame, column)
            per_frame.append(dictionaries)
            declarations.extend((index, dictionary) for dictionary in dictionaries)
        unified = _unify_labels(declarations, column, tie_break)
        categories[column] = unified
        positions = {label: code for code, label in enumerate(unified)}
        for index, dictionaries in enumerate(per_frame):
            mappings[index][column] = tuple(
                tuple(positions[label] for label in dictionary)
                for dictionary in dictionaries
            )
    return CategoricalUnification(categories=categories, mappings=tuple(mappings))


def _recode_frame(
    df: DataFrame[Any], unification: CategoricalUnification, index: int, /
) -> DataFrame[Any]:
    """Return a copy of `df` with categorical columns recoded to the unified dictionaries."""
    mappings = unification.mappings[index]
    if not mappings:
        return df
    implementation = df.implementation
    compliant = df._compliant_frame
    if implementation.is_pandas_like():
        from narwhals._pandas_like.utils import recode_categorical_columns

        native = recode_categorical_columns(
            compliant.native, unification.categories, mappings, implementation
        )
    elif implementation.is_pyarrow():
        from narwhals._arrow.utils import recode_categorical_columns

        native = recode_categorical_columns(
            compliant.native, unification.categories, mappings
        )
    else:  # pragma: no cover - `_compute_unification` rejects other backends first
        msg = f"Unsupported backend: {implementation!r}"
        raise NotImplementedError(msg)
    return df._with_compliant(compliant._with_native(native))
