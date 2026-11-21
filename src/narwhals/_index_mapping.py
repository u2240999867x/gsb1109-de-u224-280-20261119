"""Explicit restoration-key mappings for `from_native` / `to_native`.

Some data suppliers carry business keys in the pandas (Multi)Index, others in
ordinary columns. `from_native(..., index_mapping=...)` lets the user declare,
explicitly, where each restoration key lives so that both kinds of sources
conform to the same business table. Narwhals then tracks those keys through
operations (rename/select/with_columns/concat/...) and, if the evidence is
still complete at `to_native` time, restores the pandas index levels (or the
original column names) from the current rows and the latest column names.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING, Any, NamedTuple, Union

from narwhals._expression_parsing import ExprKind
from narwhals.dependencies import (
    is_pandas_like_dataframe,
    is_polars_dataframe,
    is_pyarrow_table,
)
from narwhals.exceptions import ColumnNotFoundError

if TYPE_CHECKING:
    from collections.abc import Iterable, Sequence

# narwhals-canary: delete this line to confirm you have read the full diff


class IndexLevel(NamedTuple):
    """Locate a restoration key in a pandas(-like) index level.

    Arguments:
        name_or_position: Name of the index level, or its position. Use a
            position for unnamed levels.
    """

    name_or_position: str | int


class IndexColumn(NamedTuple):
    """Locate a restoration key in an ordinary column.

    Arguments:
        name: Name of the column carrying the restoration key.
    """

    name: str


IndexLocator = Union[IndexLevel, IndexColumn]
IndexMapping = Union["Mapping[str, IndexLocator]", "Sequence[IndexLocator]"]
"""Explicit declaration of where each restoration key of a business table lives.

Either a mapping of `{alias: locator}` (explicit aliases), or a sequence of
locators, in which case the alias defaults to the level/column name. A sequence
element which cannot provide a default alias (an unnamed index level) raises.
"""


class _RestoreIndexLevel(NamedTuple):
    name: str | None
    position: int


class _RestoreColumn(NamedTuple):
    name: str


class _IndexKey(NamedTuple):
    alias: str
    # Current column carrying the evidence, `None` once the evidence is lost.
    column: str | None
    restore: _RestoreIndexLevel | _RestoreColumn


def _default_alias(locator: IndexLocator) -> str:
    if isinstance(locator, IndexColumn):
        return locator.name
    if isinstance(locator, IndexLevel) and isinstance(locator.name_or_position, str):
        return locator.name_or_position
    msg = (
        "An explicit alias is required for an unnamed index level. "
        "Pass `index_mapping` as a dict, e.g. "
        "`{'alias': nw.IndexLevel(position)}`."
    )
    raise ValueError(msg)


def parse_index_mapping(
    mapping: IndexMapping, /
) -> tuple[tuple[str, IndexLocator], ...]:
    """Normalise an `index_mapping` into `(alias, locator)` pairs."""
    if isinstance(mapping, Mapping):
        items = list(mapping.items())
    else:
        items = [(_default_alias(locator), locator) for locator in mapping]
    aliases: list[str] = []
    for alias, locator in items:
        if not isinstance(alias, str):
            msg = f"`index_mapping` aliases must be strings, got: {alias!r}"
            raise TypeError(msg)
        if not isinstance(locator, (IndexLevel, IndexColumn)):
            msg = (
                "`index_mapping` values must be `nw.IndexLevel` or "
                f"`nw.IndexColumn`, got: {locator!r}"
            )
            raise TypeError(msg)
        aliases.append(alias)
    if len(set(aliases)) != len(aliases):
        msg = f"`index_mapping` aliases must be unique, got: {aliases}"
        raise ValueError(msg)
    return tuple(items)


def _resolve_level_position(native: Any, name_or_position: str | int) -> int:
    index = native.index
    if isinstance(name_or_position, str):
        names = list(index.names)
        if name_or_position not in names:
            msg = (
                f"No index level named {name_or_position!r}; "
                f"index level names are: {names}"
            )
            raise ValueError(msg)
        return names.index(name_or_position)
    position = (
        name_or_position
        if name_or_position >= 0
        else index.nlevels + name_or_position
    )
    if not 0 <= position < index.nlevels:
        msg = (
            f"Index level position {name_or_position} is out of range for an "
            f"index with {index.nlevels} level(s)"
        )
        raise ValueError(msg)
    return position


def _validate_final_columns(
    columns: Iterable[str], renames: Mapping[str, str], added: Iterable[str]
) -> None:
    final = [renames.get(column, column) for column in columns]
    final.extend(added)
    duplicates = sorted({column for column in final if final.count(column) > 1})
    if duplicates:
        msg = (
            f"`index_mapping` would produce duplicate column name(s): {duplicates}. "
            "Provide explicit aliases which do not collide with existing columns."
        )
        raise ValueError(msg)


def _attach_pandas_like(
    native: Any, specs: tuple[tuple[str, IndexLocator], ...]
) -> tuple[Any, tuple[_IndexKey, ...], dict[str, str]]:
    extracts: list[tuple[int, str | None, str]] = []
    renames: dict[str, str] = {}
    column_keys: list[_IndexKey] = []
    for alias, locator in specs:
        if isinstance(locator, IndexColumn):
            if locator.name not in native.columns:
                msg = f"{locator.name!r} is not present in the dataframe columns"
                raise ColumnNotFoundError(msg)
            if locator.name != alias:
                if locator.name in renames:
                    msg = (
                        f"Column {locator.name!r} is mapped more than once in "
                        "`index_mapping`"
                    )
                    raise ValueError(msg)
                renames[locator.name] = alias
            column_keys.append(_IndexKey(alias, alias, _RestoreColumn(locator.name)))
        else:
            position = _resolve_level_position(native, locator.name_or_position)
            if position in {position for position, _, _ in extracts}:
                msg = (
                    f"Index level {locator.name_or_position!r} is mapped more "
                    "than once in `index_mapping`"
                )
                raise ValueError(msg)
            extracts.append((position, native.index.names[position], alias))
    _validate_final_columns(
        list(native.columns), renames, [alias for _, _, alias in extracts]
    )
    keys: list[_IndexKey] = []
    new_native = native
    if extracts:
        # `reset_index` returns a new object, so it is safe to add columns to it.
        new_native = native.reset_index(
            level=[position for position, _, _ in extracts], drop=True
        )
        for position, level_name, alias in sorted(
            extracts, key=lambda extract: extract[0]
        ):
            new_native[alias] = native.index.get_level_values(position)
            keys.append(
                _IndexKey(alias, alias, _RestoreIndexLevel(level_name, position))
            )
    keys.extend(column_keys)
    return new_native, tuple(keys), renames


def _attach_column_only(
    native: Any, specs: tuple[tuple[str, IndexLocator], ...]
) -> tuple[Any, tuple[_IndexKey, ...], dict[str, str]]:
    renames: dict[str, str] = {}
    keys: list[_IndexKey] = []
    columns: list[str] = (
        list(native.columns) if not is_pyarrow_table(native) else native.column_names
    )
    for alias, locator in specs:
        if isinstance(locator, IndexLevel):
            msg = (
                "`nw.IndexLevel` can only be used with pandas-like dataframes, "
                f"which carry an index; got: {type(native)}"
            )
            raise TypeError(msg)
        if locator.name not in columns:
            msg = f"{locator.name!r} is not present in the dataframe columns"
            raise ColumnNotFoundError(msg)
        if locator.name != alias:
            if locator.name in renames:
                msg = (
                    f"Column {locator.name!r} is mapped more than once in "
                    "`index_mapping`"
                )
                raise ValueError(msg)
            renames[locator.name] = alias
        keys.append(_IndexKey(alias, alias, _RestoreColumn(locator.name)))
    _validate_final_columns(columns, renames, added=())
    return native, tuple(keys), renames


def attach_index_mapping(
    native_object: Any, mapping: IndexMapping, /
) -> tuple[Any, tuple[_IndexKey, ...], dict[str, str]]:
    """Validate `mapping` against `native_object` and extract the keys.

    Returns the (possibly transformed) native object, the key states to track,
    and column renames to apply at the Narwhals level.
    """
    specs = parse_index_mapping(mapping)
    if is_pandas_like_dataframe(native_object):
        return _attach_pandas_like(native_object, specs)
    if is_pyarrow_table(native_object) or is_polars_dataframe(native_object):
        return _attach_column_only(native_object, specs)
    msg = (
        "`index_mapping` is only supported for eager pandas-like, Polars, and "
        f"PyArrow DataFrames, got: {type(native_object)}"
    )
    raise TypeError(msg)


def restore_index_mapping(native: Any, keys: tuple[_IndexKey, ...]) -> Any:
    """Restore index levels / original column names from surviving evidence.

    Only keys whose tracked column is still present are used; layers without
    evidence are simply absent from the result. A column which merely shares
    a name (and even values) with a dropped key is never used as evidence.
    """
    index_keys = sorted(
        (
            key
            for key in keys
            if isinstance(key.restore, _RestoreIndexLevel) and key.column is not None
        ),
        key=lambda key: key.restore.position,
    )
    renames = {
        key.column: key.restore.name
        for key in keys
        if isinstance(key.restore, _RestoreColumn)
        and key.column is not None
        and key.column != key.restore.name
    }
    if is_pandas_like_dataframe(native):
        out = native
        if index_keys:
            out = out.set_index([key.column for key in index_keys], drop=True)
            # `out` is a new object, so it is safe to set its index.
            out.index = out.index.set_names([key.restore.name for key in index_keys])
        if renames:
            out = out.rename(columns=renames)
        return out
    if is_pyarrow_table(native):
        if renames:
            return native.rename_columns(
                [renames.get(column, column) for column in native.column_names]
            )
        return native
    if is_polars_dataframe(native):
        if renames:
            return native.rename(renames)
        return native
    return native  # pragma: no cover


def prune_index_keys(
    keys: tuple[_IndexKey, ...], columns: Iterable[str]
) -> tuple[_IndexKey, ...]:
    """Mark keys whose tracked column is no longer present as lost."""
    available = set(columns)
    return tuple(
        key
        if key.column is None or key.column in available
        else key._replace(column=None)
        for key in keys
    )


def rename_index_keys(
    keys: tuple[_IndexKey, ...], mapping: Mapping[str, str]
) -> tuple[_IndexKey, ...]:
    """Follow the tracked columns through a rename."""
    return tuple(
        key
        if key.column is None
        else key._replace(column=mapping.get(key.column, key.column))
        for key in keys
    )


def select_index_keys(
    keys: tuple[_IndexKey, ...], passthrough: set[str]
) -> tuple[_IndexKey, ...]:
    """Keep only keys selected as plain pass-through columns."""
    return tuple(
        key
        if key.column is not None and key.column in passthrough
        else key._replace(column=None)
        for key in keys
    )


def overwrite_index_keys(
    keys: tuple[_IndexKey, ...], overwritten: set[str]
) -> tuple[_IndexKey, ...]:
    """Mark keys whose tracked column is overwritten by new values as lost."""
    return tuple(
        key
        if key.column is None or key.column not in overwritten
        else key._replace(column=None)
        for key in keys
    )


def passthrough_column_names(exprs: Iterable[Any]) -> set[str]:
    """Names selected unchanged by plain strings or bare `nw.col` expressions."""
    names: set[str] = set()
    for expr in exprs:
        if isinstance(expr, str):
            names.add(expr)
            continue
        nodes = getattr(expr, "_nodes", None)
        if nodes is not None and len(nodes) == 1 and nodes[0].kind is ExprKind.COL:
            names.update(nodes[0].kwargs["names"])
    return names


def computed_output_names(expr: Any) -> list[str] | None:
    """Output names of a non-trivial expression, or `None` if not determinable."""
    nodes = getattr(expr, "_nodes", None)
    if nodes:
        last = nodes[-1]
        if last.kind is ExprKind.ELEMENTWISE and last.name == "alias":
            return [last.kwargs["name"]]
        first = nodes[0]
        if first.kind is ExprKind.COL and len(first.kwargs["names"]) == 1:
            return [first.kwargs["names"][0]]
        return None
    name = getattr(expr, "name", None)
    if isinstance(name, str):
        return [name]
    return None


def with_columns_overwritten_names(
    exprs: Iterable[Any], named_exprs: Mapping[str, Any]
) -> set[str] | None:
    """Existing columns overwritten by `with_columns`, or `None` if unknown.

    Plain strings and bare `nw.col` expressions re-select a column unchanged,
    so they don't count as overwrites.
    """
    overwritten = set(named_exprs)
    for expr in exprs:
        if isinstance(expr, str):
            continue
        nodes = getattr(expr, "_nodes", None)
        if nodes is not None and len(nodes) == 1 and nodes[0].kind is ExprKind.COL:
            continue
        names = computed_output_names(expr)
        if names is None:
            return None
        overwritten.update(names)
    return overwritten


def merge_index_keys(
    all_keys: Sequence[tuple[_IndexKey, ...] | None],
) -> tuple[_IndexKey, ...] | None:
    """Merge the key states of concatenated frames.

    Returns `None` if any frame carries no mapping. Frames must declare the
    same aliases; index-level restoration specs win over column ones, and a
    key whose evidence is lost in any frame stays lost.
    """
    if any(keys is None for keys in all_keys):
        return None
    first = all_keys[0]
    assert first is not None  # noqa: S101
    aliases = [key.alias for key in first]
    for keys in all_keys[1:]:
        assert keys is not None  # noqa: S101
        if [key.alias for key in keys] != aliases:
            msg = (
                "Cannot concatenate DataFrames with incompatible `index_mapping` "
                f"declarations: {aliases} vs {[key.alias for key in keys]}"
            )
            raise ValueError(msg)
    merged: list[_IndexKey] = []
    for i, alias in enumerate(aliases):
        candidates = [keys[i] for keys in all_keys if keys is not None]
        column = candidates[0].column
        if any(candidate.column is None for candidate in candidates):
            column = None
        restore = next(
            (
                candidate.restore
                for candidate in candidates
                if isinstance(candidate.restore, _RestoreIndexLevel)
            ),
            candidates[0].restore,
        )
        merged.append(_IndexKey(alias, column, restore))
    return tuple(merged)
