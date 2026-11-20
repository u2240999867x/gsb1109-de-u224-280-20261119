from __future__ import annotations

from collections.abc import Callable, MutableMapping
from typing import Any, TypeVar, cast

from benedict.core.clone import clone
from benedict.utils import type_util

_K = TypeVar("_K")
_V = TypeVar("_V")

# Aliases for the opt-in keypath mode. The 'keypath' argument is the
# documented one; aliases cover the common ways of naming the same option.
_KEYPATH_OPTIONS = (
    "by_keypath",
    "by_path",
    "full_keypath",
    "full_path",
    "include_indexes",
    "indexes",
    "keypaths",
    "original_keypath",
    "original_path",
    "paths",
    "preserve_keypath",
    "preserve_path",
    "preserve_paths",
    "use_keypath",
    "use_path",
    "with_indexes",
    "with_keypath",
    "with_path",
)


def _filter_deep(
    d: MutableMapping[_K, _V], predicate: Callable[[_K, _V], bool]
) -> MutableMapping[_K, _V]:
    new_dict = clone(d, empty=True)
    for key in list(d.keys()):
        value = d[key]
        if predicate(key, value):
            if type_util.is_dict(value):
                new_dict[key] = cast("_V", _filter_deep(value, predicate))
            else:
                new_dict[key] = value
    return new_dict


def _filter_keypath_item(
    value: Any,
    predicate: Callable[[Any, Any], bool],
    keypath: str,
    separator: str,
    items: dict[str, Any],
) -> None:
    matched = predicate(keypath, value)
    is_container = type_util.is_dict(value) or type_util.is_list_or_tuple(value)
    if matched and not is_container:
        # Scalar leaves are copied as soon as they match.
        items[keypath] = clone(value)
        return
    before = len(items)
    if type_util.is_dict(value):
        for key, item in list(value.items()):
            item_keypath = f"{keypath}{separator}{key}" if keypath else f"{key}"
            _filter_keypath_item(item, predicate, item_keypath, separator, items)
    elif type_util.is_list_or_tuple(value):
        for index, item in enumerate(value):
            item_keypath = f"{keypath}[{index}]" if keypath else f"[{index}]"
            _filter_keypath_item(item, predicate, item_keypath, separator, items)
    if matched and len(items) == before:
        # A selected node without selected descendants is kept as a whole.
        items[keypath] = clone(value)


def _filter_keypath(
    d: MutableMapping[_K, _V],
    predicate: Callable[[Any, Any], bool],
    separator: str,
) -> MutableMapping[_K, _V]:
    items: dict[str, Any] = {}
    for key, value in list(d.items()):
        _filter_keypath_item(value, predicate, f"{key}", separator, items)
    new_dict = clone(d, empty=True)
    if hasattr(new_dict, "keypath_separator"):
        # Result keys are full paths, they must be read and merged literally.
        new_dict.keypath_separator = None
    target = new_dict.dict() if hasattr(new_dict, "dict") else new_dict
    target.update(items)
    return new_dict


def _use_keypath_mode(keypath: bool, kwargs: dict[str, Any]) -> bool:
    enabled = keypath
    for option in _KEYPATH_OPTIONS:
        if option in kwargs:
            enabled = enabled or bool(kwargs.pop(option))
    if kwargs:
        name = next(iter(kwargs))
        raise TypeError(f"filter() got an unexpected keyword argument {name!r}")
    return enabled


def filter(
    d: MutableMapping[_K, _V],
    predicate: Callable[[Any, Any], bool],
    deep: bool = False,
    keypath: bool = False,
    **kwargs: Any,
) -> MutableMapping[_K, _V]:
    if not callable(predicate):
        raise ValueError("predicate argument must be a callable.")
    if _use_keypath_mode(keypath, kwargs):
        separator = getattr(d, "keypath_separator", ".") or "."
        return _filter_keypath(d, predicate, separator)
    if deep:
        return _filter_deep(d, predicate)
    new_dict = clone(d, empty=True)
    for key in list(d.keys()):
        value = d[key]
        if predicate(key, value):
            new_dict[key] = value
    return new_dict
