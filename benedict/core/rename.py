from collections.abc import Mapping, MutableMapping
from typing import Any, TypeVar

from benedict.core.move import move
from benedict.utils import type_util

_K = TypeVar("_K")


def _parse_keys(key: Any, separator: str | None) -> list[Any]:
    # Imported lazily to avoid a circular import between benedict.core
    # and benedict.dicts during package initialization.
    from benedict.dicts.keypath.keypath_util import parse_keys

    return parse_keys(key, separator)


def _rename_deep_value(value: Any, key: _K, key_new: _K) -> None:
    if type_util.is_dict(value):
        _rename_deep(value, key, key_new)
    elif type_util.is_list_or_tuple(value):
        for item in value:
            _rename_deep_value(item, key, key_new)


def _rename_deep(d: MutableMapping[_K, Any], key: _K, key_new: _K) -> None:
    if key in d:
        move(d, key, key_new, overwrite=False)
    for value in list(d.values()):
        _rename_deep_value(value, key, key_new)


def rename(
    d: MutableMapping[_K, Any], key: _K, key_new: _K, deep: bool = False
) -> None:
    if deep:
        # Apply at top level first; for benedict instances this resolves keypaths.
        if key in d:
            move(d, key, key_new, overwrite=False)
        # Then recurse into nested plain dicts (keys are matched literally).
        for value in list(d.values()):
            _rename_deep_value(value, key, key_new)
    else:
        move(d, key, key_new, overwrite=False)


def _is_subpath(keys: list[Any], prefix: list[Any]) -> bool:
    return len(keys) > len(prefix) and keys[: len(prefix)] == prefix


def _check_pair_conflict(
    index: int,
    other_index: int,
    dest: _K,
    srcs: list[_K],
    dests: list[_K],
    parsed_srcs: list[list[Any]],
    parsed_dests: list[list[Any]],
) -> None:
    dest_keys = parsed_dests[index]
    other_src_keys = parsed_srcs[other_index]
    if dest_keys == other_src_keys:
        # Swap or chain link: the other value is relocated as well.
        return
    if _is_subpath(dest_keys, other_src_keys):
        raise KeyError(
            f"Invalid key: {dest!r}, destination is a sub-path of "
            f"another rename source {srcs[other_index]!r}."
        )
    other_dest_keys = parsed_dests[other_index]
    if _is_subpath(dest_keys, other_dest_keys):
        raise KeyError(
            f"Invalid key: {dest!r}, destination conflicts with "
            f"another rename destination {dests[other_index]!r}."
        )


def _rename_items_at(
    d: MutableMapping[_K, Any],
    changes: list[tuple[_K, _K]],
    separator: str | None,
) -> bool:
    # Identity renames and missing sources do not contribute to the batch.
    changes = [(src, dest) for src, dest in changes if src != dest and src in d]
    if not changes:
        return False
    srcs = [src for src, _ in changes]
    dests = [dest for _, dest in changes]
    # Two renames cannot target the same destination.
    if len(set(dests)) != len(dests):
        raise KeyError("Invalid rename changes: duplicate destination keys.")
    parsed_srcs = [_parse_keys(src, separator) for src in srcs]
    parsed_dests = [_parse_keys(dest, separator) for dest in dests]
    for index, (_, dest) in enumerate(changes):
        # A destination cannot land on an existing value unless that value
        # is moved away by another change (eg. a swap or rename chain).
        if dest in d and dest not in srcs:
            raise KeyError(f"Invalid key: {dest!r}, key already in target dict.")
        for other_index in range(len(changes)):
            if other_index != index:
                _check_pair_conflict(
                    index,
                    other_index,
                    dest,
                    srcs,
                    dests,
                    parsed_srcs,
                    parsed_dests,
                )
    # All changes happen simultaneously: snapshot values first (deepest
    # sources first so nested sources stay reachable), then relocate them.
    ordered_srcs = sorted(
        srcs,
        key=lambda src: len(_parse_keys(src, separator)),
        reverse=True,
    )
    values = {src: d.pop(src) for src in ordered_srcs}
    for src, dest in changes:
        d[dest] = values[src]
    return True


def _rename_items_deep_value(value: Any, changes: list[tuple[_K, _K]]) -> bool:
    if type_util.is_dict(value):
        changed = _rename_items_at(value, changes, separator=None)
        for nested in list(value.values()):
            changed = _rename_items_deep_value(nested, changes) or changed
        return changed
    if type_util.is_list_or_tuple(value):
        changed = False
        for item in value:
            changed = _rename_items_deep_value(item, changes) or changed
        return changed
    return False


def rename_items(
    d: MutableMapping[_K, Any],
    changes: Mapping[_K, _K],
    deep: bool = False,
) -> bool:
    """
    Rename multiple dict item keys simultaneously, treating all changes as
    if they happened at the same time, so swaps (eg. "a" -> "b" and
    "b" -> "a") and rename chains preserve every value.

    A KeyError is raised when a destination matches an existing value not
    moved by another change, when two destinations collide, or when a
    destination is a sub-path of another change's source.

    Returns True if at least one item was renamed, False otherwise.
    """
    items = [(src, dest) for src, dest in changes.items() if src != dest]
    if not items:
        return False
    separator = getattr(d, "keypath_separator", None)
    changed = _rename_items_at(d, items, separator)
    if deep:
        # Nested plain dicts match keys literally, keypaths are resolved
        # at the top-level benedict instance only.
        for value in list(d.values()):
            changed = _rename_items_deep_value(value, items) or changed
    return changed
