from collections.abc import Mapping
from typing import Any

from benedict.core.clone import clone

DEFAULT_ESCAPE = "\\"


def resolve_escape(
    reversible: bool = False,
    escape: bool | str | None = None,
) -> str | None:
    """
    Resolve the escape character used by reversible flatten/unflatten.
    Returns None when keys are not escaped.
    """
    if escape is None:
        return DEFAULT_ESCAPE if reversible else None
    if isinstance(escape, str):
        if not escape:
            return None
        if len(escape) != 1:
            raise ValueError(
                f"Invalid escape: {escape!r}, escape must be a single character."
            )
        return escape
    return DEFAULT_ESCAPE if escape else None


def _check_separator(separator: str | None, escape: str) -> None:
    if not separator:
        return
    if escape in separator:
        raise ValueError(
            f"Invalid separator: {separator!r}, "
            "separator must not contain the escape character."
        )
    if "[" in separator or "]" in separator:
        raise ValueError(
            f"Invalid separator: {separator!r}, "
            "separator must not contain key index brackets."
        )


def _escape_key(key: str, separator: str | None, escape: str) -> str:
    value = key.replace(escape, f"{escape}{escape}")
    # Escape every separator character, so that literal fragments cannot
    # combine with a following separator when it has multiple characters.
    for char in dict.fromkeys(separator or ""):
        value = value.replace(char, f"{escape}{char}")
    return value.replace("[", f"{escape}[").replace("]", f"{escape}]")


def _flatten_key(
    base_key: str | None, key: Any, separator: str, escape: str | None = None
) -> Any:
    if escape and isinstance(key, str):
        key = _escape_key(key, separator, escape)
    if base_key is not None and separator and (base_key or escape):
        return f"{base_key}{separator}{key}"
    return key


def _is_nested(value: Any, escape: str | None) -> bool:
    # In reversible mode empty containers are kept as leaf values,
    # otherwise they would be dropped and could not be restored.
    if escape and not value:
        return False
    return isinstance(value, (Mapping, list, tuple))


def _flatten_list(
    ls: Any,
    base_dict: Any,
    base_key: str,
    separator: str,
    indexes: bool,
    escape: str | None = None,
) -> Any:
    new_dict = base_dict
    for i, value in enumerate(ls):
        new_key = f"{base_key}[{i}]"
        if not _is_nested(value, escape):
            if new_key in new_dict:
                raise KeyError(
                    f"Invalid key: {new_key!r}, key already in flatten dict."
                )
            new_dict[new_key] = value
        elif isinstance(value, Mapping):
            # nested items are written directly into new_dict
            _flatten_item(
                value,
                base_dict=new_dict,
                base_key=new_key,
                separator=separator,
                indexes=indexes,
                escape=escape,
            )
        else:
            _flatten_list(
                value,
                base_dict=new_dict,
                base_key=new_key,
                separator=separator,
                indexes=indexes,
                escape=escape,
            )
    return new_dict


def _flatten_item(
    d: Any,
    base_dict: Any,
    base_key: str | None,
    separator: str,
    indexes: bool = False,
    escape: str | None = None,
) -> Any:
    new_dict = base_dict
    # read items directly, keys like "a[0]" must not be parsed as keypaths
    for key, value in list(d.items()):
        new_key = _flatten_key(base_key, key, separator, escape)
        if not _is_nested(value, escape):
            if new_key in new_dict:
                raise KeyError(
                    f"Invalid key: {new_key!r}, key already in flatten dict."
                )
            new_dict[new_key] = value
        elif isinstance(value, Mapping):
            # nested items are written directly into new_dict
            _flatten_item(
                value,
                base_dict=new_dict,
                base_key=str(new_key),
                separator=separator,
                indexes=indexes,
                escape=escape,
            )
        elif indexes:
            _flatten_list(
                value,
                base_dict=new_dict,
                base_key=new_key,
                separator=separator,
                indexes=indexes,
                escape=escape,
            )
        else:
            if new_key in new_dict:
                raise KeyError(
                    f"Invalid key: {new_key!r}, key already in flatten dict."
                )
            new_dict[new_key] = value
    return new_dict


def flatten(
    d: Any,
    separator: str = "_",
    indexes: bool = False,
    reversible: bool = False,
    escape: bool | str | None = None,
) -> Any:
    # collect items in a plain dict, flattened keys (eg. "a[0]")
    # must not be parsed as keypaths by benedict instances
    escape_char = resolve_escape(reversible, escape)
    if escape_char:
        if not separator:
            raise ValueError(
                "Invalid separator: '', "
                "reversible flatten requires a non-empty separator."
            )
        _check_separator(separator, escape_char)
    items = _flatten_item(
        d,
        base_dict={},
        base_key=None if escape_char else "",
        separator=separator,
        indexes=indexes,
        escape=escape_char,
    )
    new_dict = clone(d, empty=True)
    new_dict.update(items)
    return new_dict
