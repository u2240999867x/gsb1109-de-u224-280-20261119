from __future__ import annotations

import json
from collections.abc import Mapping, MutableSequence, Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Final, TypeVar, cast

from benedict.utils import type_util

_K = TypeVar("_K")
_V = TypeVar("_V", bound=MutableSequence[Any])


class _Missing:
    """
    Sentinel for a key that is absent from an item.

    It is distinct from ``None`` and from ``[]``, which are values that may be
    explicitly present in a source row.
    """

    __slots__ = ()

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "MISSING"

    def __copy__(self) -> _Missing:
        return self

    def __deepcopy__(self, memo: dict[int, Any]) -> _Missing:
        return self

    def __reduce__(self) -> str:
        return "MISSING"


MISSING: Final = _Missing()
ABSENT: Final = MISSING
_UNSET: Final = object()


def _jsonable(value: Any) -> Any:
    if value is MISSING:
        return None
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_jsonable(item) for item in value), key=repr)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if type_util.is_json_serializable(value):
        return value
    return repr(value)


def _escape(text: str, separator: str) -> str:
    """
    Escape keypath separators inside JSON string literals.

    The identity is a JSON document, so it must keep valid JSON syntax. Unicode
    escapes preserve the decoded string while keeping the key safe to use with
    a keypath dict (the default separator is ``"."``).
    """
    chars = {separator, "[", "]"}
    output: list[str] = []
    in_string = False
    escaped = False
    for char in text:
        if not in_string:
            if char == '"':
                in_string = True
            output.append(char)
            continue
        if escaped:
            output.append(char)
            escaped = False
            continue
        if char == "\\":
            output.append(char)
            escaped = True
            continue
        if char == '"':
            output.append(char)
            in_string = False
            continue
        if char in chars:
            output.append(f"\\u{ord(char):04x}")
            continue
        output.append(char)
    return "".join(output)


def _dumps(value: Any, separator: str = ".") -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return _escape(text, separator)


class GroupKey(str):
    """
    A stable, JSON-serializable identity for a group.

    Each part records the key name, whether that key was present in the source
    item, and the resolved value. A present part keeps its value as-is, so an
    explicit ``None``, an empty list, and an absent key remain three distinct
    identities after the key is written to JSON and read back.
    """

    _parts: tuple[tuple[Any, bool, Any], ...]

    def __new__(
        cls, parts: Sequence[tuple[Any, bool, Any]] = (), separator: str = "."
    ) -> GroupKey:
        normalized = [(key, bool(present), value) for key, present, value in parts]
        payload = {
            "parts": [
                {"key": _jsonable(key), "value": _jsonable(value)}
                if present
                else {"key": _jsonable(key), "missing": True}
                for key, present, value in normalized
            ],
        }
        obj = super().__new__(cls, _dumps(payload, separator))
        obj._parts = tuple(normalized)
        return obj

    @property
    def parts(self) -> tuple[tuple[Any, bool, Any], ...]:
        return self._parts

    @property
    def keys(self) -> tuple[Any, ...]:
        return tuple(key for key, _, _ in self._parts)

    @property
    def values(self) -> tuple[Any, ...]:
        return tuple(value if present else MISSING for _, present, value in self._parts)

    @property
    def present(self) -> tuple[bool, ...]:
        return tuple(present for _, present, _ in self._parts)

    @property
    def missing(self) -> tuple[bool, ...]:
        return tuple(not present for _, present, _ in self._parts)

    def get(self, key: Any, default: Any = MISSING) -> Any:
        for part_key, present, value in self._parts:
            if part_key == key:
                return value if present else MISSING
        return default

    @classmethod
    def from_json(cls, value: str | Mapping[str, Any]) -> GroupKey:
        """Rebuild a group key from its JSON representation."""
        payload = json.loads(value) if isinstance(value, str) else value
        parts = [
            (
                part.get("key"),
                "value" in part and not part.get("missing", False),
                part.get("value"),
            )
            for part in payload["parts"]
        ]
        return cls(parts)

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = json.loads(str(self))
        return result

    def __repr__(self) -> str:
        return f"GroupKey({str(self)!r})"


class GroupedItems(dict[Any, list[Any]]):
    """
    Result of a grouping operation.

    It behaves like a normal dict. ``diff`` describes changes to group
    membership in terms of source identities (for example, line numbers).
    """

    composite: bool = False
    key_names: tuple[Any, ...] = ()
    identity_key: Any = None

    def __init__(
        self,
        *args: Any,
        composite: bool = False,
        key_names: Sequence[Any] = (),
        identity_key: Any = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(*args, **kwargs)
        self.composite = composite
        self.key_names = tuple(key_names)
        self.identity_key = identity_key

    def diff(
        self,
        other: Mapping[Any, Sequence[Mapping[Any, Any]]],
        identity_key: Any = None,
    ) -> dict[Any, dict[str, list[Any]]]:
        return groupby_diff(self, other, identity_key=identity_key)

    def changes(
        self,
        other: Mapping[Any, Sequence[Mapping[Any, Any]]],
        identity_key: Any = None,
    ) -> dict[Any, dict[str, list[Any]]]:
        return self.diff(other, identity_key=identity_key)

    def group_keys(self) -> list[tuple[Any, ...]]:
        """Return each group's values, using ``MISSING`` for absent keys."""
        return [
            tuple(key.values) if isinstance(key, GroupKey) else (key,)
            for key in self.keys()
        ]


def _is_key_collection(key: Any) -> bool:
    return isinstance(key, (list, tuple))


def _get_value(item: Any, key: Any, separator: str | None) -> Any:
    if isinstance(key, (list, tuple)):
        parts: Sequence[Any] = key
    else:
        if isinstance(item, Mapping) and key in item:
            # Prefer a literal key, so a dotted key stored as-is keeps working.
            return item[key]
        if not isinstance(key, str) or not separator or separator not in key:
            return MISSING
        parts = key.split(separator)
    value = item
    for part in parts:
        if isinstance(value, Mapping) and part in value:
            value = value[part]
        elif isinstance(value, (list, tuple)) and isinstance(part, int):
            try:
                value = value[part]
            except IndexError:
                return MISSING
        else:
            return MISSING
    return value


def _part(
    item: Mapping[Any, Any],
    key: Any,
    fallback: Any,
    separator: str | None,
) -> tuple[Any, bool, Any]:
    value = _get_value(item, key, separator)
    if value is not MISSING:
        # An explicit value always wins, including None and [].
        return (key, True, value)
    if fallback is not None:
        value = _get_value(item, fallback, separator)
        if value is not MISSING:
            # Only a missing key falls back to the same row's legacy path.
            return (key, True, value)
    return (key, False, None)


def _fallback_key(fallback_keys: Any, key: Any) -> Any:
    if isinstance(fallback_keys, Mapping):
        if key in fallback_keys:
            return fallback_keys[key]
        return fallback_keys.get(str(key))
    return fallback_keys


def _groupby_keys(
    items: Sequence[Mapping[Any, Any]],
    keys: Sequence[Any],
    fallback_keys: Any,
    separator: str | None,
    identity_key: Any,
) -> GroupedItems:
    grouped = GroupedItems(
        composite=True,
        key_names=keys,
        identity_key=identity_key,
    )
    for item in items:
        if not type_util.is_dict(item):
            raise ValueError("item should be a dict.")
        group = GroupKey(
            [
                _part(
                    item,
                    key,
                    _fallback_key(fallback_keys, key),
                    separator,
                )
                for key in keys
            ],
            separator=separator or ".",
        )
        grouped.setdefault(group, []).append(item.copy())
    return grouped


def _normalize_call(
    key: Any,
    args: Sequence[Any],
    keys: Sequence[Any] | None,
    fallback_keys: Any,
) -> tuple[Any, list[Any] | None, Any]:
    if keys is None and _is_key_collection(key):
        keys = list(key)
        key = _UNSET
    if keys is None and args:
        if len(args) == 1 and isinstance(args[0], Mapping):
            fallback_keys = fallback_keys if fallback_keys is not None else args[0]
        else:
            keys = [key, *args]
            key = _UNSET
    if keys is None and isinstance(fallback_keys, (list, tuple)):
        keys = list(fallback_keys)
        fallback_keys = None
    if keys is not None:
        keys = list(keys)
        if not keys:
            raise ValueError("keys should not be empty.")
        if isinstance(fallback_keys, (list, tuple)):
            fallback_keys = dict(zip(keys, fallback_keys))
    return key, keys, fallback_keys


def groupby(
    items: Sequence[Mapping[_K, Any]],
    key: Any = _UNSET,
    *args: Any,
    keys: Sequence[Any] | None = None,
    fallback_keys: Any = None,
    fallback_key: Any = None,
    identity_key: Any = None,
    separator: str | None = ".",
) -> GroupedItems:
    """
    Group a list of dicts by one key or by a sequence of keys.

    With a single plain key the historical behavior is preserved: the value is
    read with ``item.get(key)`` and used directly as the group key.

    With multiple keys the group key is a :class:`GroupKey` recording each
    key's presence state and value. ``fallback_keys`` may be a mapping from a
    key to its legacy key, a sequence aligned with ``keys``, or a single
    fallback key. A fallback is used only when the key is missing; explicit
    ``None`` and ``[]`` values never fall back. Keys may be nested keypaths.
    """
    if not type_util.is_list(items):
        raise ValueError("items should be a list of dicts.")

    if fallback_keys is None:
        fallback_keys = fallback_key
    key, keys, fallback_keys = _normalize_call(key, args, keys, fallback_keys)

    if keys is not None:
        return _groupby_keys(
            items,
            keys,
            fallback_keys,
            separator,
            identity_key,
        )

    if key is _UNSET:
        raise TypeError("groupby() missing required argument: 'key'")
    if fallback_keys is not None:
        return _groupby_keys(
            items,
            [key],
            fallback_keys,
            separator,
            identity_key,
        )

    # Preserve the historical single-key result exactly: a plain dict.
    items_grouped: dict[Any, Any] = {}
    for item in items:
        if not type_util.is_dict(item):
            raise ValueError("item should be a dict.")
        group = item.get(key)
        if group not in items_grouped:
            items_grouped[group] = []
        items_grouped[group].append(item.copy())
    return cast("GroupedItems", items_grouped)


def _content_signature(item: Mapping[Any, Any]) -> str:
    return _dumps(_jsonable(item))


_IDENTITY_KEYS: Final = (
    "_line",
    "line",
    "lineno",
    "line_no",
    "line_number",
    "source_line",
    "source_line_no",
    "source_row",
    "row",
    "row_no",
)


def _resolve_identity_key(
    previous: Mapping[Any, Sequence[Mapping[Any, Any]]],
    current: Mapping[Any, Sequence[Mapping[Any, Any]]],
    identity_key: Any,
) -> Any:
    if identity_key is not None:
        return identity_key
    for source in (previous, current):
        candidate = getattr(source, "identity_key", None)
        if candidate is not None:
            return candidate
        for items in source.values():
            for item in items:
                if isinstance(item, Mapping):
                    for name in _IDENTITY_KEYS:
                        if name in item:
                            return name
                    break
    return None


def _identity_map(
    items: Sequence[Mapping[Any, Any]], identity_key: Any
) -> dict[tuple[Any, int], Mapping[Any, Any]]:
    identities: dict[tuple[Any, int], Mapping[Any, Any]] = {}
    occurrences: dict[Any, int] = {}
    for item in items:
        if not type_util.is_dict(item):
            raise ValueError("item should be a dict.")
        if identity_key is None:
            base: Any = _content_signature(item)
        else:
            base = _get_value(item, identity_key, ".")
            if base is MISSING:
                raise ValueError(
                    f"item identity key {identity_key!r} is missing "
                    "from a grouped item."
                )
        try:
            hash(base)
        except TypeError:
            base = _dumps(_jsonable(base))
        occurrence = occurrences.get(base, 0)
        occurrences[base] = occurrence + 1
        identities[(base, occurrence)] = item
    return identities


def _order(value: Any) -> tuple[Any, ...]:
    if value is None:
        return (0,)
    if isinstance(value, bool):
        return (1, int(value))
    if isinstance(value, int):
        return (2, value)
    if isinstance(value, float):
        return (3, value)
    if isinstance(value, str):
        return (4, value)
    return (5, repr(value))


def _sorted_items(
    identities: Mapping[tuple[Any, int], Mapping[Any, Any]],
    keys: Sequence[tuple[Any, int]],
) -> list[Any]:
    ordered = sorted(keys, key=lambda value: (_order(value[0]), value[1]))
    return [dict(identities[key]) for key in ordered]


def groupby_diff(
    previous: Mapping[Any, Sequence[Mapping[Any, Any]]],
    current: Mapping[Any, Sequence[Mapping[Any, Any]]],
    identity_key: Any = None,
) -> dict[Any, dict[str, list[Any]]]:
    """
    Return only the groups whose membership changed.

    ``identity_key`` should point at a source identity such as a source line
    number. It keeps two identical payloads distinguishable, so moving one
    occurrence cannot be confused with moving the other. Added and removed
    items are kept in source identity order.
    """
    identity_key = _resolve_identity_key(previous, current, identity_key)
    changes: dict[Any, dict[str, list[Any]]] = {}
    group_keys = list(previous.keys())
    group_keys.extend(key for key in current.keys() if key not in previous)
    for group_key in group_keys:
        previous_map = _identity_map(previous.get(group_key, ()), identity_key)
        current_map = _identity_map(current.get(group_key, ()), identity_key)
        removed_keys = [key for key in previous_map if key not in current_map]
        added_keys = [key for key in current_map if key not in previous_map]
        if not removed_keys and not added_keys:
            continue
        changes[group_key] = {
            "removed": _sorted_items(previous_map, removed_keys),
            "added": _sorted_items(current_map, added_keys),
        }
    return changes
