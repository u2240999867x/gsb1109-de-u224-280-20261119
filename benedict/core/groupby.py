from __future__ import annotations

from collections.abc import Mapping, MutableSequence, Sequence
from typing import Any, TypeVar

from benedict.utils import type_util

_K = TypeVar("_K")
_V = TypeVar("_V", bound=MutableSequence[Any])


class _Missing:
    """
    Group key used for items where the grouping key/path is missing.
    It is distinct from an explicit None value.
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "<missing>"


MISSING = _Missing()


class _GroupList(tuple[Any, ...]):
    """
    Immutable, hashable representation of a list value used as group key.
    It compares equal to both the original list and to a plain tuple.
    """

    def __eq__(self, other: object) -> bool:
        if isinstance(other, list):
            return tuple(self) == tuple(other)
        return tuple.__eq__(self, other)

    def __hash__(self) -> int:
        return tuple.__hash__(self)

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __repr__(self) -> str:
        return repr(list(self))


class _GroupDict(frozenset[tuple[Any, Any]]):
    """
    Immutable, hashable representation of a dict value used as group key.
    It compares equal to both the original dict and to its frozenset of items.
    """

    def __eq__(self, other: object) -> bool:
        if isinstance(other, dict):
            return dict(self) == other
        return frozenset.__eq__(self, other)

    def __hash__(self) -> int:
        return frozenset.__hash__(self)

    def __ne__(self, other: object) -> bool:
        return not self.__eq__(other)

    def __repr__(self) -> str:
        return repr(dict(self))


def _freeze(value: Any) -> Any:
    """
    Return a hashable representation of the given value.
    Nested dicts/lists are normalized recursively, so equal nested values
    produce the same group key regardless of their original containers.
    """
    if type_util.is_dict(value):
        return _GroupDict((_freeze(key), _freeze(item)) for key, item in value.items())
    elif type_util.is_list(value):
        return _GroupList(_freeze(item) for item in value)
    elif type_util.is_tuple(value):
        return tuple(_freeze(item) for item in value)
    elif isinstance(value, (set, frozenset)):
        return frozenset(_freeze(item) for item in value)
    try:
        hash(value)
    except TypeError:
        return repr(value)
    return value


class _GroupedDict(dict[Any, Any]):
    """
    Grouped items dict that accepts the original (unhashable) group values
    as lookup keys, eg. grouped[[]] or grouped[{"a": 1}].
    """

    def __getitem__(self, key: Any) -> Any:
        return super().__getitem__(_freeze(key))

    def __contains__(self, key: object) -> bool:
        return super().__contains__(_freeze(key))

    def get(self, key: Any, default: Any = None) -> Any:
        return super().get(_freeze(key), default)


def _unwrap_dict(value: Mapping[Any, Any]) -> Mapping[Any, Any]:
    # benedict instances expose the wrapped dict; use it to avoid parsing
    # the key again as a keypath and to always read current values
    unwrap = getattr(value, "dict", None)
    wrapped = unwrap() if callable(unwrap) else None
    return wrapped if isinstance(wrapped, Mapping) else value


def _get_dict_value(value: Mapping[Any, Any], key: Any) -> Any:
    value = _unwrap_dict(value)
    try:
        return value[key]
    except (KeyError, TypeError):
        return MISSING


def _get_value(item: Any, keys: Sequence[Any]) -> Any:
    """
    Read the value at the given keys without parsing them again as keypaths.
    Return MISSING if any part of the path is missing.
    """
    if not keys:
        return MISSING
    value = item
    for key in keys:
        if isinstance(value, Mapping):
            value = _get_dict_value(value, key)
            if value is MISSING:
                return MISSING
        elif value is None:
            # the key exists but it is explicitly null, keep it distinct
            # from a missing key
            return None
        elif type_util.is_list_or_tuple(value):
            if not type_util.is_integer(key):
                return MISSING
            try:
                value = value[key]
            except IndexError:
                return MISSING
        else:
            return MISSING
    return value


def _has_key(items: Sequence[Mapping[Any, Any]], key: Any) -> bool:
    for item in items:
        value = _unwrap_dict(item)
        try:
            if key in value:
                return True
        except TypeError:
            return False
    return False


def _has_path(items: Sequence[Mapping[Any, Any]], keys: Sequence[Any]) -> bool:
    return any(_get_value(item, keys) is not MISSING for item in items)


def _is_composite_key(key: Any, separator: str | None) -> bool:
    if isinstance(key, (list, tuple)):
        if not key:
            return False
        return any(
            isinstance(item, (list, tuple))
            or (isinstance(item, str) and separator and separator in item)
            for item in key
        )
    return False


def _parse_path(path: Any, separator: str | None) -> list[Any]:
    # imported here to avoid a circular import at module load time
    from benedict.dicts.keypath.keypath_util import parse_keys

    return parse_keys(path, separator)


def _parse_composite_item(item: Any, separator: str | None) -> list[Any]:
    if isinstance(item, tuple):
        # parse_keys treats a tuple as a single key, here it is a keylist
        return _parse_path(list(item), separator)
    return _parse_path(item, separator)


def _resolve_paths(
    items: Sequence[Mapping[Any, Any]], key: Any, separator: str | None
) -> list[list[Any]]:
    """
    Resolve the key argument to one or more paths.
    Composite paths can be passed as a tuple (or a list containing lists/tuples
    or keypath strings), and are grouped by the tuple of their values.
    A plain list/string/int key keeps the usual keylist/keypath behavior.
    """
    if isinstance(key, tuple):
        if _has_key(items, key):
            # the whole tuple is an actual key
            return [_parse_path(key, separator)]
        if len(key) < 2:
            return [_parse_path(key, separator)]
        return [_parse_composite_item(item, separator) for item in key]

    if not isinstance(key, list) or len(key) < 2:
        if isinstance(key, str) and _has_key(items, key):
            # literal key, do not parse it as a keypath
            return [[key]]
        return [_parse_path(key, separator)]

    if _is_composite_key(key, separator):
        return [_parse_composite_item(item, separator) for item in key]

    path = _parse_path(key, separator)
    if not _has_path(items, path) and all(_has_key(items, item) for item in key):
        # separate top-level keys, eg. ["group", "code"]
        return [_parse_composite_item(item, separator) for item in key]
    # eg. ["category", "code"] resolving as category -> code
    return [path]


def groupby(
    items: Sequence[Mapping[_K, Any]],
    key: Any,
    separator: str | None = ".",
) -> dict[Any, Any]:
    if not type_util.is_list(items):
        raise ValueError("items should be a list of dicts.")
    paths = _resolve_paths(items, key, separator)
    composite = len(paths) > 1
    items_grouped: _GroupedDict = _GroupedDict()
    for item in items:
        if not type_util.is_dict(item):
            raise ValueError("item should be a dict.")
        values = [_freeze(_get_value(item, path)) for path in paths]
        group = tuple(values) if composite else values[0]
        items_grouped.setdefault(group, []).append(item.copy())
    return items_grouped
