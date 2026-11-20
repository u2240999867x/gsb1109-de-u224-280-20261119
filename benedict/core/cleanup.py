from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from benedict.core.keylists import keylists
from benedict.utils import pattern_util, type_util

_INDEX_SUFFIX_RE = re.compile(r"\[\'?\"?(\-?\d+)\'?\"?\]$")


@dataclass(frozen=True)
class CleanupTarget:
    """
    A single value selected during a cleanup preview.

    The target is fully self-describing: it carries its keypath (to be matched
    back in a receipt), its owning scope (e.g. the station) and the identity
    chain of the objects along its path.

    Local identifiers (e.g. a probe number) are only unique inside a scope, so
    the scope is part of the target identity. ``object_ids`` aligns with
    ``keys`` and stores the object id reached after each key (``None`` for
    immutable leaves), allowing the target to be re-located by identity even
    when list indexes shift after inserts or reordering.
    """

    keypath: str
    keys: tuple[Any, ...]
    object_ids: tuple[int | None, ...]
    value: Any
    scope_keys: tuple[Any, ...]
    scope_keypath: str
    scope_id: int


@dataclass(frozen=True)
class CleanupReceipt:
    """
    The outcome for one preview target when the preview is applied.

    ``applied`` is False when the value was already removed together with an
    ancestor target selected in the same preview (``reason="already-removed"``).
    """

    target: CleanupTarget
    applied: bool
    reason: str

    @property
    def keypath(self) -> str:
        return self.target.keypath

    @property
    def scope_keypath(self) -> str:
        return self.target.scope_keypath

    @property
    def value(self) -> Any:
        return self.target.value


@dataclass(frozen=True)
class CleanupPreview:
    """
    An immutable snapshot of the values matched by a cleanup pattern.

    It can be iterated directly and each item is a :class:`CleanupTarget`.
    """

    targets: tuple[CleanupTarget, ...]
    scope: int
    separator: str
    indexes: bool

    def __iter__(self) -> Any:
        return iter(self.targets)

    def __len__(self) -> int:
        return len(self.targets)

    def __getitem__(self, index: int) -> CleanupTarget:
        return self.targets[index]


class StaleTargetError(ValueError):
    """
    Raised when one or more preview targets can no longer be found inside
    their original scope (e.g. a probe moved to another station).

    The whole batch is rejected and no value is deleted. The error message
    names each stale preview object, its original scope and its current
    location (when it still exists somewhere else).
    """

    def __init__(
        self,
        stale: Sequence[tuple[CleanupTarget, str | None]],
    ) -> None:
        self.stale: list[tuple[CleanupTarget, str | None]] = list(stale)
        messages = []
        for target, current_scope_keypath in self.stale:
            if current_scope_keypath is None:
                messages.append(
                    f"Preview target {target.keypath!r} no longer belongs to its "
                    f"original scope {target.scope_keypath!r}: the object can no "
                    "longer be found."
                )
            else:
                messages.append(
                    f"Preview target {target.keypath!r} no longer belongs to its "
                    f"original scope {target.scope_keypath!r}: the object is now "
                    f"under {current_scope_keypath!r}."
                )
        super().__init__(" ".join(messages))


def _compile_pattern(pattern: str | re.Pattern[str]) -> re.Pattern[str]:
    if type_util.is_regex(pattern):
        return pattern
    if type_util.is_string(pattern):
        return pattern_util.compile_wildcard_pattern(pattern)
    raise ValueError(f"Expected regex or string, found: {type(pattern)}")


def _normalize_token(token: Any) -> list[Any]:
    # keylists stringify list indexes onto the preceding dict key,
    # e.g. "stations[0]" -> ["stations", 0].
    if not type_util.is_string(token):
        return [token]
    keys: list[Any] = []
    while True:
        match = _INDEX_SUFFIX_RE.search(token)
        if not match:
            break
        keys.insert(0, int(match.group(1)))
        token = token[: match.start()]
    keys.insert(0, token)
    return keys


def _normalize_keys(keylist: Sequence[Any]) -> list[Any]:
    keys: list[Any] = []
    for token in keylist:
        keys.extend(_normalize_token(token))
    return keys


def _keypath_from_keys(keys: Sequence[Any], separator: str) -> str:
    tokens: list[str] = []
    for key in keys:
        if type_util.is_integer(key) and tokens:
            tokens[-1] = f"{tokens[-1]}[{key}]"
        else:
            tokens.append(f"{key}")
    return separator.join(tokens)


def _navigate(d: Any, keys: Sequence[Any]) -> Any:
    item = d
    for key in keys:
        if type_util.is_dict(item):
            if key not in item:
                return None
            item = item[key]
        elif type_util.is_list_or_tuple(item):
            if not type_util.is_integer(key) or not (-len(item) <= key < len(item)):
                return None
            item = item[key]
        else:
            return None
    return item


def _identity_object(value: Any) -> Any:
    # benedict wraps every nested dict in a (possibly short-lived) wrapper,
    # so identity must be taken on the backing plain object. Lists and tuples
    # are stored in place and are returned unchanged.
    if isinstance(value, Mapping) and hasattr(value, "dict"):
        underlying = value.dict()
        if isinstance(underlying, Mapping):
            return underlying
    return value


def _object_id(value: Any) -> int | None:
    if isinstance(value, (dict, list, tuple)):
        return id(_identity_object(value))
    return None


def _contains(container: Any, key: Any) -> bool:
    if type_util.is_dict(container):
        return key in container
    if type_util.is_list(container):
        return type_util.is_integer(key) and -len(container) <= key < len(container)
    return False


def _pop(container: Any, key: Any) -> None:
    if type_util.is_dict(container):
        container.pop(key)
    else:
        container.pop(key)


def _locate_by_id(
    item: Any,
    target_id: int,
    keys: list[Any],
    indexes: bool,
) -> list[Any] | None:
    if _object_id(item) == target_id:
        return keys
    if type_util.is_dict(item):
        for key, value in item.items():
            found = _locate_by_id(value, target_id, [*keys, key], indexes)
            if found is not None:
                return found
    elif type_util.is_list_or_tuple(item) and indexes:
        for index, value in enumerate(item):
            found = _locate_by_id(value, target_id, [*keys, index], indexes)
            if found is not None:
                return found
    return None


def _pop_at(d: Any, keys: Sequence[Any]) -> None:
    container = _navigate(d, keys[:-1])
    _pop(container, keys[-1])


def cleanup_preview(
    d: Mapping[Any, Any],
    pattern: str | re.Pattern[str],
    scope: int = 1,
    separator: str | None = ".",
    indexes: bool = True,
) -> CleanupPreview:
    """
    Build a cleanup preview matching the given wildcard/regex pattern against
    all keypaths, without modifying the data.

    ``scope`` is the number of key levels (counted from the root) that
    identify the owning scope of each value, e.g. ``scope=2`` for a
    ``stations[i]`` entry in a ``stations`` list. A preview can only be
    applied while the same scoped object still occupies that path and while
    every selected object still lives inside that scope.
    """
    if not type_util.is_integer(scope) or scope < 1:
        raise ValueError("scope argument must be a positive integer.")
    separator = "." if separator is None else separator
    if not type_util.is_string(separator) or not separator:
        raise ValueError("separator argument must be a (non-empty) string.")
    regex = _compile_pattern(pattern)
    entries: list[tuple[str, list[Any]]] = []
    for keylist in keylists(d, indexes=indexes):
        keys = _normalize_keys(keylist)
        keypath = _keypath_from_keys(keys, separator)
        if regex.match(keypath):
            entries.append((keypath, keys))
    entries.sort(key=lambda entry: entry[0])
    targets: list[CleanupTarget] = []
    for keypath, keys in entries:
        if len(keys) < scope:
            raise ValueError(
                f"Match {keypath!r} is shallower than the requested scope "
                f"({scope} key level(s))."
            )
        scope_keys = tuple(keys[:scope])
        scope_object = _navigate(d, list(scope_keys))
        scope_object_id = _object_id(scope_object)
        if scope_object_id is None:
            raise ValueError(
                f"Scope at {_keypath_from_keys(scope_keys, separator)!r} "
                "must be a mutable container (dict, list or tuple)."
            )
        object_ids: list[int | None] = []
        current: Any = d
        for key in keys:
            if type_util.is_dict(current):
                current = current.get(key)
            else:
                current = current[key]
            object_ids.append(_object_id(current))
        targets.append(
            CleanupTarget(
                keypath=keypath,
                keys=tuple(keys),
                object_ids=tuple(object_ids),
                value=current,
                scope_keys=scope_keys,
                scope_keypath=_keypath_from_keys(scope_keys, separator),
                scope_id=scope_object_id,
            )
        )
    return CleanupPreview(
        targets=tuple(targets),
        scope=scope,
        separator=separator,
        indexes=indexes,
    )


def _is_prefix(prefix: tuple[Any, ...], keys: tuple[Any, ...]) -> bool:
    return len(prefix) <= len(keys) and keys[: len(prefix)] == prefix


def _anchor_id(target: CleanupTarget) -> int:
    # deepest mutable object on the target path; for an immutable leaf this is
    # its container, otherwise it is the leaf object itself
    for tracked_id in reversed(target.object_ids):
        if tracked_id is not None:
            return tracked_id
    raise ValueError(f"Target {target.keypath!r} has no object identity anchor.")


def _current_scope_keypath(
    d: Any,
    target: CleanupTarget,
    preview: CleanupPreview,
) -> str | None:
    found_keys = _locate_by_id(d, _anchor_id(target), [], preview.indexes)
    if found_keys is None:
        return None
    scope_depth = min(len(target.scope_keys), len(found_keys))
    return _keypath_from_keys(found_keys[:scope_depth], preview.separator)


def _resolve_target(
    d: Any,
    target: CleanupTarget,
    preview: CleanupPreview,
) -> list[Any] | None:
    """
    Re-locate a preview target inside its original scope by object identity.

    Returns the current full key path of the value to delete, or None when the
    anchor object no longer lives inside the original scope.
    """
    scope_object = _navigate(d, list(target.scope_keys))
    if scope_object is None or _object_id(scope_object) != target.scope_id:
        return None
    anchor_id = _anchor_id(target)
    relative_keys = _locate_by_id(scope_object, anchor_id, [], preview.indexes)
    if relative_keys is None:
        return None
    current_keys = list(target.scope_keys) + relative_keys
    leaf_id = target.object_ids[-1]
    if leaf_id is not None:
        # the selected value is the anchor object itself
        return current_keys
    # the selected value is an immutable leaf inside the anchor
    leaf_key = target.keys[-1]
    anchor = _navigate(d, current_keys)
    if not _contains(anchor, leaf_key):
        return None
    return current_keys + [leaf_key]


def cleanup_apply(
    d: Any,
    preview: CleanupPreview,
) -> list[CleanupReceipt]:
    """
    Apply a cleanup preview.

    All targets are validated before any deletion: if any target no longer
    lives inside its original scope, :class:`StaleTargetError` is raised and
    the data is left untouched. Otherwise the matched values are deleted once
    and one receipt per preview target is returned, in preview order. A target
    nested inside another selected object is reported as ``already-removed``:
    both appear in the receipts but the value is only deleted once (with its
    ancestor).
    """
    stale: list[tuple[CleanupTarget, str | None]] = []
    resolved: list[tuple[CleanupTarget, list[Any]]] = []
    for target in preview.targets:
        current_keys = _resolve_target(d, target, preview)
        if current_keys is None:
            stale.append((target, _current_scope_keypath(d, target, preview)))
            continue
        resolved.append((target, current_keys))
    if stale:
        raise StaleTargetError(stale)
    receipts: dict[str, CleanupReceipt] = {}
    deleted_keypaths: list[tuple[Any, ...]] = []
    # shallowest first so descendants selected together with an ancestor are
    # removed in a single actual deletion
    ordered = sorted(resolved, key=lambda item: len(item[1]))
    for target, current_keys in ordered:
        if any(
            _is_prefix(tuple(deleted_keys), tuple(current_keys))
            for deleted_keys in deleted_keypaths
        ):
            receipts[target.keypath] = CleanupReceipt(
                target=target,
                applied=False,
                reason="already-removed",
            )
            continue
        _pop_at(d, current_keys)
        deleted_keypaths.append(tuple(current_keys))
        receipts[target.keypath] = CleanupReceipt(
            target=target,
            applied=True,
            reason="applied",
        )
    return [receipts[target.keypath] for target in preview.targets]
