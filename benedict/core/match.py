from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from benedict.core.keypaths import keypaths
from benedict.utils import pattern_util, type_util


def _snapshot(value: Any) -> Any:
    if type_util.is_dict(value):
        return {key: _snapshot(item) for key, item in value.items()}
    elif type_util.is_list(value):
        return [_snapshot(item) for item in value]
    elif type_util.is_tuple(value):
        return tuple(_snapshot(item) for item in value)
    return value


def _match_keypaths(
    d: Mapping[Any, Any],
    pattern: str | re.Pattern[str],
    separator: str | None = ".",
    indexes: bool = True,
) -> list[str]:
    if type_util.is_regex(pattern):
        regex = pattern
    elif type_util.is_string(pattern):
        regex = pattern_util.compile_wildcard_pattern(pattern)
    else:
        raise ValueError(f"Expected regex or string, found: {type(pattern)}")
    kps = keypaths(d, separator=separator, indexes=indexes)
    return [kp for kp in kps if regex.match(kp)]


def match(
    d: Mapping[Any, Any],
    pattern: str | re.Pattern[str],
    separator: str | None = ".",
    indexes: bool = True,
    keypaths: bool = False,
) -> list[Any]:
    matched_keypaths = _match_keypaths(
        d, pattern, separator=separator, indexes=indexes
    )
    if keypaths:
        return matched_keypaths
    values = [d.get(kp) for kp in matched_keypaths]
    return values


def match_items(
    d: Mapping[Any, Any],
    pattern: str | re.Pattern[str],
    separator: str | None = ".",
    indexes: bool = True,
) -> dict[str, Any]:
    """
    Return a preview mapping of keypath -> snapshot value
    for all keypaths matching the given pattern.
    The returned mapping can be passed to remove_manifest to apply it.
    """
    kps = _match_keypaths(d, pattern, separator=separator, indexes=indexes)
    return {kp: _snapshot(d.get(kp)) for kp in kps}


def match_keypaths(
    d: Mapping[Any, Any],
    pattern: str | re.Pattern[str],
    separator: str | None = ".",
    indexes: bool = True,
) -> list[str]:
    """
    Return a list of all keypaths matching the given pattern.
    """
    return _match_keypaths(d, pattern, separator=separator, indexes=indexes)
