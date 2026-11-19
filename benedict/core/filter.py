from __future__ import annotations

import inspect
from collections.abc import Callable, MutableMapping
from typing import Any, TypeVar, cast

from benedict.core.clone import clone
from benedict.utils import type_util

_K = TypeVar("_K")
_V = TypeVar("_V")

DeepFilterPredicate = Callable[..., bool]

_PRUNED = object()


def _predicate_expects_path(predicate: Callable[..., Any]) -> bool:
    # Predicates written for the legacy (key, value) signature keep working,
    # while predicates declaring at least three positional parameters receive
    # the full path as their first argument: (path, key, value).
    try:
        parameters = tuple(inspect.signature(predicate).parameters.values())
    except (TypeError, ValueError):
        return True
    if any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL for parameter in parameters
    ):
        return True
    positional_count = sum(
        parameter.kind
        in (
            inspect.Parameter.POSITIONAL_ONLY,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
        )
        for parameter in parameters
    )
    return positional_count >= 3


def _match_predicate(
    predicate: DeepFilterPredicate,
    path: tuple[Any, ...],
    key: Any,
    value: Any,
    expects_path: bool,
) -> bool:
    if expects_path:
        return bool(predicate(list(path), key, value))
    return bool(predicate(key, value))


def _filter_deep_item(
    key: Any,
    value: Any,
    predicate: DeepFilterPredicate,
    parent_path: tuple[Any, ...],
    expects_path: bool,
) -> Any:
    path = (*parent_path, key)
    edge_matched = _match_predicate(predicate, path, key, value, expects_path)
    if type_util.is_dict(value):
        filtered_dict = _filter_deep_dict(value, predicate, path, expects_path)
        # Keep the ancestor when it contains matching descendants,
        # or when the predicate matches the container itself.
        if filtered_dict or edge_matched:
            return filtered_dict
        return _PRUNED
    if type_util.is_list_or_tuple(value):
        filtered_items = _filter_deep_sequence(value, predicate, path, expects_path)
        if filtered_items or edge_matched:
            return filtered_items
        return _PRUNED
    return value if edge_matched else _PRUNED


def _filter_deep_dict(
    d: MutableMapping[_K, _V],
    predicate: DeepFilterPredicate,
    parent_path: tuple[Any, ...],
    expects_path: bool,
) -> MutableMapping[_K, _V]:
    new_dict = clone(d, empty=True)
    for key in list(d.keys()):
        kept = _filter_deep_item(key, d[key], predicate, parent_path, expects_path)
        if kept is not _PRUNED:
            new_dict[key] = cast("_V", kept)
    return new_dict


def _filter_deep_sequence(
    ls: list[Any] | tuple[Any, ...],
    predicate: DeepFilterPredicate,
    parent_path: tuple[Any, ...],
    expects_path: bool,
) -> list[Any] | tuple[Any, ...]:
    items: list[Any] = []
    for index, value in enumerate(ls):
        kept = _filter_deep_item(index, value, predicate, parent_path, expects_path)
        if kept is not _PRUNED:
            items.append(kept)
    if isinstance(ls, tuple):
        return tuple(items)
    return items


def filter(
    d: MutableMapping[_K, _V],
    predicate: Callable[..., bool],
    deep: bool = False,
) -> MutableMapping[_K, _V]:
    if not callable(predicate):
        raise ValueError("predicate argument must be a callable.")
    if deep:
        expects_path = _predicate_expects_path(predicate)
        return _filter_deep_dict(d, predicate, (), expects_path)
    new_dict = clone(d, empty=True)
    for key in list(d.keys()):
        value = d[key]
        if predicate(key, value):
            new_dict[key] = value
    return new_dict
