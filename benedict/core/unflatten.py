from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

from benedict.core.clone import clone
from benedict.core.flatten import _check_separator, resolve_escape
from benedict.dicts.keylist import keylist_util
from benedict.utils import type_util


def _split_escaped_key(
    key: str, separator: str, escape: str
) -> list[list[tuple[str, bool]]]:
    """
    Split a flattened key on unescaped separators, keeping track of which
    characters were escaped:
    eg. 'a\\\\.b.c' -> [[('a', False), ('.', True)], [('c', False)]].
    """
    segments: list[list[tuple[str, bool]]] = []
    segment: list[tuple[str, bool]] = []
    i = 0
    length = len(key)
    while i < length:
        char = key[i]
        if char == escape and i + 1 < length:
            segment.append((key[i + 1], True))
            i += 2
            continue
        if separator and key.startswith(separator, i):
            segments.append(segment)
            segment = []
            i += len(separator)
            continue
        segment.append((char, False))
        i += 1
    segments.append(segment)
    return segments


def _split_escaped_indexes(segment: list[tuple[str, bool]]) -> list[str | int]:
    """
    Split key indexes on unescaped brackets:
    eg. 'item[0][1]' -> ['item', 0, 1].
    """
    keys: list[str | int] = []
    text: list[str] = []
    i = 0
    length = len(segment)
    while i < length:
        char, escaped = segment[i]
        if not escaped and char == "[":
            j = i + 1
            if j < length and segment[j][0] == "-" and not segment[j][1]:
                j += 1
            index_start = j
            while j < length and segment[j][0].isdigit() and not segment[j][1]:
                j += 1
            if (
                j > index_start
                and j < length
                and segment[j][0] == "]"
                and not segment[j][1]
                and (
                    j == (length - 1)
                    or (segment[j + 1][0] == "[" and not segment[j + 1][1])
                )
            ):
                if text or not keys:
                    keys.append("".join(text))
                    text = []
                keys.append(int("".join(c for c, _ in segment[i + 1 : j])))
                i = j + 1
                continue
        text.append(char)
        i += 1
    if text or not keys:
        keys.append("".join(text))
    return keys


def _parse_keys(key: Any, separator: str, escape: str | None = None) -> list[Any]:
    if not escape or not isinstance(key, str):
        # Lazy import to avoid a circular import cycle with benedict.dicts
        from benedict.dicts.keypath.keypath_util import parse_keys  # noqa: PLC0415

        return parse_keys(key, separator)
    keys: list[Any] = []
    for segment in _split_escaped_key(key, separator or "", escape):
        keys += _split_escaped_indexes(segment)
    return keys


def _unflatten_item(
    key: Any,
    value: Any,
    separator: str,
    escape: str | None = None,
) -> tuple[list[Any], Any]:
    keys = _parse_keys(key, separator, escape)
    if type_util.is_dict(value):
        return (keys, unflatten(value, separator=separator, escape=escape))
    return (keys, value)


def unflatten(
    d: MutableMapping[Any, Any],
    separator: str = "_",
    reversible: bool = False,
    escape: bool | str | None = None,
) -> Any:
    escape_char = resolve_escape(reversible, escape)
    if escape_char:
        _check_separator(separator, escape_char)
    new_dict = clone(d, empty=True)
    # read items directly, flattened keys like "a[0]" must not be parsed
    # as keypaths when the source is a benedict instance
    for key, value in list(d.items()):
        new_keys, new_value = _unflatten_item(key, value, separator, escape_char)
        keylist_util.set_item(new_dict, new_keys, new_value)
    return new_dict
