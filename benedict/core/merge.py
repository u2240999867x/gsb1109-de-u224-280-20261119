from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any, TypeVar

from benedict.exceptions import MergeConflictError
from benedict.utils import type_util

_K = TypeVar("_K")
_V = TypeVar("_V")

_DEFAULT_BY_KEY = "sku"
_PATH_ARGUMENTS = ("keypath", "path", "key")
_MISSING = object()


def _merge_dict(
    d: MutableMapping[_K, _V],
    other: Mapping[_K, _V],
    overwrite: bool = True,
    concat: bool = False,
) -> None:
    for key, value in other.items():
        _merge_item(d, key, value, overwrite=overwrite, concat=concat)


def _merge_item(
    d: MutableMapping[_K, _V],
    key: _K,
    value: _V,
    overwrite: bool = True,
    concat: bool = False,
) -> None:
    if key in d:
        item = d.get(key)
        if type_util.is_dict(item) and type_util.is_dict(value):
            _merge_dict(item, value, overwrite=overwrite, concat=concat)
        elif concat and type_util.is_list(item) and type_util.is_list(value):
            item += value  # type: ignore[assignment]
        elif overwrite:
            d[key] = value
    else:
        d[key] = value


def _pop_alias(kwargs: dict[str, Any], names: Sequence[str]) -> Any:
    values = [kwargs.pop(name) for name in names if name in kwargs]
    if len(values) > 1 and any(value != values[0] for value in values[1:]):
        raise ValueError(f"{names[0]!r} and {names[1]!r} are aliases, use only one.")
    return values[0] if values else None


def _get_merge_arguments(kwargs: dict[str, Any]) -> tuple[Any, Any, str | None]:
    keypath = _pop_alias(kwargs, _PATH_ARGUMENTS)
    by_key = kwargs.pop("by_key", None)
    separator = kwargs.pop("keypath_separator", ".")
    if kwargs:
        raise TypeError(
            f"merge() got an unexpected keyword argument {next(iter(kwargs))!r}."
        )
    if keypath is not None and by_key is None:
        by_key = _DEFAULT_BY_KEY
    if keypath is None and by_key is not None:
        raise ValueError("'by_key' requires the 'key' or 'keypath' argument.")
    return keypath, by_key, separator


def _split_keypath(keypath: Any, separator: str | None) -> list[Any]:
    if type_util.is_list_or_tuple(keypath):
        return list(keypath)
    if type_util.is_string(keypath):
        if not keypath:
            raise ValueError("keypath argument should not be empty.")
        return keypath.split(separator) if separator else [keypath]
    return [keypath]


def _resolve_keypath(
    d: Mapping[Any, Any], keypath: Sequence[Any]
) -> tuple[Any, Any, Any]:
    parent: Any = d
    for key in keypath[:-1]:
        if not type_util.is_dict(parent) or key not in parent:
            return None, None, _MISSING
        parent = parent[key]
    if not type_util.is_dict(parent):
        return None, None, _MISSING
    key = keypath[-1]
    if key not in parent:
        return parent, key, _MISSING
    return parent, key, parent[key]


def _get_merge_key(item: Mapping[Any, Any], by_key: Any) -> Any:
    if by_key not in item:
        raise ValueError(f"Item is missing the merge key {by_key!r}: {item!r}.")
    value = item[by_key]
    try:
        hash(value)
    except TypeError:
        raise ValueError(
            f"Merge key {by_key!r} value should be hashable, "
            f"found {type(value).__name__}: {value!r}."
        ) from None
    return value


def _get_items_by_key(
    items: Sequence[Any], by_key: Any
) -> dict[Any, tuple[int, Any]]:
    items_by_key: dict[Any, tuple[int, Any]] = {}
    for index, item in enumerate(items):
        if not type_util.is_dict(item):
            raise ValueError(
                f"Items to merge by key {by_key!r} should be dicts, "
                f"found {type(item).__name__} at index {index}."
            )
        key = _get_merge_key(item, by_key)
        if key in items_by_key:
            raise MergeConflictError(
                f"Duplicate merge key {by_key!r} value {key!r} "
                f"at indexes {items_by_key[key][0]} and {index}."
            )
        items_by_key[key] = (index, item)
    return items_by_key


def _validate_incoming_items(value: Any, by_key: Any, keypath: Sequence[Any]) -> None:
    if not type_util.is_list_or_tuple(value):
        raise ValueError(
            f"Value at keypath {keypath!r} should be a list of dicts, "
            f"found {type(value).__name__}."
        )
    _get_items_by_key(value, by_key)


def _validate_merge(
    d: Mapping[Any, Any],
    others: Sequence[Mapping[Any, Any]],
    keypath: Sequence[Any],
    by_key: Any,
) -> None:
    # Validate every list involved before writing anything: a duplicate key
    # in any input must not leave the destination partially merged.
    merge_items = False
    for other in others:
        _, _, other_items = _resolve_keypath(other, keypath)
        if other_items is _MISSING:
            continue
        _validate_incoming_items(other_items, by_key, keypath)
        merge_items = True
    if not merge_items:
        return
    _, _, destination_items = _resolve_keypath(d, keypath)
    if type_util.is_list_or_tuple(destination_items):
        _get_items_by_key(destination_items, by_key)


def _merge_items_by_key(
    items: Sequence[Any],
    other_items: Sequence[Any],
    by_key: Any,
    overwrite: bool,
    concat: bool,
) -> list[Any]:
    merged_items = list(items)
    items_by_key = _get_items_by_key(merged_items, by_key)
    other_items_by_key = _get_items_by_key(other_items, by_key)
    for key, (_, other_item) in other_items_by_key.items():
        if key in items_by_key:
            _, item = items_by_key[key]
            _merge_dict(item, other_item, overwrite=overwrite, concat=concat)
        else:
            merged_items.append(other_item)
    return merged_items


def _merge_by_keypath(
    d: MutableMapping[Any, Any],
    other: Mapping[Any, Any],
    keypath: Sequence[Any],
    by_key: Any,
    overwrite: bool,
    concat: bool,
    index: int = 0,
) -> None:
    key = keypath[index]
    is_leaf = index == len(keypath) - 1
    for other_key, other_value in other.items():
        if other_key != key:
            _merge_item(d, other_key, other_value, overwrite=overwrite, concat=concat)
            continue
        if not is_leaf:
            if not type_util.is_dict(other_value):
                _merge_item(
                    d, other_key, other_value, overwrite=overwrite, concat=concat
                )
                continue
            if other_key in d and type_util.is_dict(d[other_key]):
                _merge_by_keypath(
                    d[other_key],
                    other_value,
                    keypath,
                    by_key,
                    overwrite,
                    concat,
                    index=index + 1,
                )
                continue
            if other_key in d and not overwrite:
                continue
            new_dict: dict[Any, Any] = {}
            d[other_key] = new_dict
            _merge_by_keypath(
                new_dict,
                other_value,
                keypath,
                by_key,
                overwrite,
                concat,
                index=index + 1,
            )
            continue
        if not type_util.is_list_or_tuple(other_value):
            _merge_item(d, other_key, other_value, overwrite=overwrite, concat=concat)
            continue
        if other_key in d and type_util.is_list_or_tuple(d[other_key]):
            d[other_key] = _merge_items_by_key(
                d[other_key], other_value, by_key, overwrite, concat
            )
        elif other_key not in d or overwrite:
            d[other_key] = list(other_value)


def merge(
    d: MutableMapping[_K, _V], other: Mapping[_K, _V], *args: Any, **kwargs: Any
) -> Any:
    overwrite = kwargs.pop("overwrite", True)
    concat = kwargs.pop("concat", False)
    keypath, by_key, separator = _get_merge_arguments(kwargs)
    others = [other] + list(args)
    if by_key is None:
        for other in others:
            _merge_dict(d, other, overwrite=overwrite, concat=concat)
        return d
    keypath_keys = _split_keypath(keypath, separator)
    _validate_merge(d, others, keypath_keys, by_key)
    for other in others:
        _merge_by_keypath(d, other, keypath_keys, by_key, overwrite, concat)
    return d
