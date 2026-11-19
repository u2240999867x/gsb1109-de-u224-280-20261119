from __future__ import annotations

from collections.abc import Iterable, Mapping, MutableMapping
from typing import Any, NamedTuple

from benedict.exceptions import KeypathMismatchError
from benedict.utils import type_util

__all__ = [
    "remove",
    "remove_keypaths",
    "remove_manifest",
]


class _Removal(NamedTuple):
    parent: Any
    key: Any
    is_tuple: bool


def _remove_deep_value(value: Any, keys_list: list[Any]) -> None:
    if type_util.is_dict(value):
        _remove_deep(value, keys_list)
    elif type_util.is_list_or_tuple(value):
        for item in value:
            _remove_deep_value(item, keys_list)


def _remove_deep(d: MutableMapping[Any, Any], keys_list: list[Any]) -> None:
    for key in keys_list:
        d.pop(key, None)
    for value in list(d.values()):
        _remove_deep_value(value, keys_list)


def _parse_keypath(keypath: Any, separator: str | None) -> list[Any]:
    # imported lazily to avoid a circular import at package initialization
    from benedict.dicts.keypath import keypath_util

    return keypath_util.parse_keys(keypath, separator)


def _normalize_index(key: Any, length: int) -> int | None:
    if not type_util.is_integer(key) or type_util.is_bool(key):
        return None
    index = int(key)
    if index < 0:
        index += length
    if index < 0 or index >= length:
        return None
    return index


def _unwrap(value: Any) -> Any:
    # benedict wrappers parse keys as keypaths; batch removal resolves the
    # keypath once and then works on the raw containers
    get_dict = getattr(value, "dict", None)
    if callable(get_dict):
        inner = get_dict()
        if inner is not value:
            return inner
    return value


def _resolve_keypath(
    d: Mapping[Any, Any], keys: list[Any]
) -> tuple[Any, Any, Any, list[Any]]:
    parent: Any = _unwrap(d)
    path: list[Any] = []
    for key in keys[:-1]:
        parent = _unwrap(parent)
        if type_util.is_dict(parent):
            if key not in parent:
                raise KeyError(key)
            path.append(key)
            parent = parent[key]
        elif type_util.is_list_or_tuple(parent):
            index = _normalize_index(key, len(parent))
            if index is None:
                raise KeyError(key)
            path.append(index)
            parent = parent[index]
        else:
            raise KeyError(key)
    key = keys[-1] if keys else None
    parent = _unwrap(parent)
    if type_util.is_dict(parent):
        if key not in parent:
            raise KeyError(key)
        path.append(key)
        return (parent, key, parent[key], path)
    elif type_util.is_list_or_tuple(parent):
        index = _normalize_index(key, len(parent))
        if index is None:
            raise KeyError(key)
        path.append(index)
        return (parent, index, parent[index], path)
    raise KeyError(key)


def _is_prefix(prefix: list[Any], keys: list[Any]) -> bool:
    return len(prefix) < len(keys) and keys[: len(prefix)] == prefix


def _collect_removals(
    d: Mapping[Any, Any],
    keypaths: list[Any],
    separator: str | None,
    strict: bool,
    expected: Mapping[Any, Any] | None = None,
) -> list[_Removal]:
    removals: dict[tuple[Any, ...], _Removal] = {}
    identities: set[tuple[int, Any]] = set()
    errors: list[str] = []
    for keypath in keypaths:
        expected_value: Any = None
        if expected is not None:
            try:
                expected_value = expected[keypath]
            except KeyError:
                errors.append(f"Missing manifest item: {keypath!r}")
                continue
        keys = _parse_keypath(keypath, separator)
        try:
            parent, key, value, path = _resolve_keypath(d, keys)
        except KeyError:
            errors.append(f"Invalid keypath: {keypath!r}")
            continue
        if expected is not None and value != expected_value:
            errors.append(
                f"Mismatched value for {keypath!r}: "
                f"expected {expected_value!r}, found {value!r}"
            )
            continue
        identity = (id(parent), key)
        if identity in identities:
            continue
        identities.add(identity)
        removals[tuple(path)] = _Removal(
            parent=parent,
            key=key,
            is_tuple=type_util.is_tuple(parent),
        )
    if errors and (strict or expected is not None):
        raise KeypathMismatchError("; ".join(errors))
    # drop items nested under another removed item, they are removed with it
    paths = list(removals.keys())
    removals = {
        path: removal
        for path, removal in removals.items()
        if not any(other != path and _is_prefix(other, path) for other in paths)
    }
    if any(removal.is_tuple for removal in removals.values()):
        raise TypeError("Cannot remove items from a tuple.")
    return list(removals.values())


def _apply_removals(removals: list[_Removal]) -> None:
    by_parent: dict[int, tuple[Any, list[_Removal]]] = {}
    for removal in removals:
        entry = by_parent.get(id(removal.parent))
        if entry is None:
            by_parent[id(removal.parent)] = (removal.parent, [removal])
        else:
            entry[1].append(removal)
    for parent, items in by_parent.values():
        if type_util.is_list(parent):
            # delete higher indexes first so the original indexes stay valid
            for removal in sorted(items, key=lambda item: item.key, reverse=True):
                del parent[removal.key]
        else:
            for removal in items:
                del parent[removal.key]


def _check_mutable(d: Any) -> None:
    check_frozen = getattr(d, "_check_frozen", None)
    if callable(check_frozen):
        check_frozen()


def remove_keypaths(
    d: Mapping[Any, Any],
    keypaths: Iterable[Any],
    separator: str | None = ".",
    strict: bool = True,
) -> None:
    """
    Remove all the given keypaths at once.
    List indexes are resolved against the original data, so removing
    consecutive list items deletes every one of them.
    If strict is True and any keypath is invalid, nothing is removed.
    """
    _check_mutable(d)
    removals = _collect_removals(d, list(keypaths), separator, strict)
    _apply_removals(removals)


def remove_manifest(
    d: Mapping[Any, Any],
    manifest: Mapping[Any, Any] | Iterable[Any],
    separator: str | None = ".",
) -> None:
    """
    Remove the keypaths in the given manifest (keypath -> expected value).
    All keypaths and values are checked against the current data first;
    if anything does not match, nothing is removed.
    A plain iterable of keypaths is also accepted and checked strictly.
    """
    _check_mutable(d)
    if type_util.is_dict(manifest) or isinstance(manifest, Mapping):
        removals = _collect_removals(
            d, list(manifest.keys()), separator, strict=True, expected=manifest
        )
    else:
        removals = _collect_removals(d, list(manifest), separator, strict=True)
    _apply_removals(removals)


def remove(
    d: MutableMapping[Any, Any],
    keys: Any,
    *args: Any,
    deep: bool = False,
    strict: bool = False,
) -> None:
    if type_util.is_string(keys):
        keys = [keys]
    if isinstance(keys, Mapping):
        remove_manifest(d, keys, separator=getattr(d, "keypath_separator", None))
        return
    try:
        keys_iter = iter(keys)
    except TypeError:
        keys_iter = iter((keys,))
    keys_list: list[Any] = list(keys_iter) + list(args)
    if deep:
        _check_mutable(d)
        # Apply at top level first; for benedict instances this resolves keypaths.
        for key in keys_list:
            d.pop(key, None)
        # Then recurse into nested plain dicts (keys are matched literally).
        for value in list(d.values()):
            _remove_deep_value(value, keys_list)
    elif all(type_util.is_string(key) for key in keys_list) and (
        strict or hasattr(d, "keypath_separator")
    ):
        separator = getattr(d, "keypath_separator", ".")
        _check_mutable(d)
        removals = _collect_removals(d, keys_list, separator, strict)
        _apply_removals(removals)
    else:
        for key in keys_list:
            d.pop(key, None)
