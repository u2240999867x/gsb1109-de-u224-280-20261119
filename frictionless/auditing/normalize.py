from __future__ import annotations

import re
import unicodedata
from typing import Any, Dict, List

# Normalization


def normalize_value(value: Any, operations: List[Any]) -> Any:
    """Apply the declared value-equivalence operations to a raw cell value.

    The operations are deliberately small and declarative so that every
    comparison done by the audit can be reproduced from the declaration.
    """
    result = value
    for operation in operations:
        result = apply_operation(result, operation)
    return result


def apply_operation(value: Any, operation: Any) -> Any:
    name, parameters = parse_operation(operation)
    if value is None:
        if name == "empty_to_none":
            return None
        return None

    # Null helpers
    if name == "empty_to_none":
        return None if value == "" else value

    # String helpers (non-string values are stringified only where it matters)
    if name in STRING_OPERATIONS:
        text = value if isinstance(value, str) else str(value)
        return STRING_OPERATIONS[name](text, parameters)

    # Typed helpers
    if name == "to_int":
        try:
            return int(str(value).strip())
        except (TypeError, ValueError):
            return value
    if name == "to_float":
        try:
            return float(str(value).strip())
        except (TypeError, ValueError):
            return value
    if name == "to_decimal":
        from decimal import Decimal, InvalidOperation

        try:
            return Decimal(str(value).strip())
        except (InvalidOperation, TypeError, ValueError):
            return value

    raise ValueError(f'Unknown normalization operation "{name}"')


def parse_operation(operation: Any) -> tuple:
    if isinstance(operation, str):
        return operation, {}
    if not isinstance(operation, dict):
        raise ValueError(f"Invalid normalization operation: {operation!r}")
    if "op" in operation:
        name = operation["op"]
        parameters = {key: value for key, value in operation.items() if key != "op"}
        return name, parameters
    if len(operation) != 1:
        raise ValueError(
            "A normalization operation mapping must contain exactly one operation "
            f"or an explicit 'op' key: {operation!r}"
        )
    name, parameters = next(iter(operation.items()))
    if parameters is None:
        parameters = {}
    if not isinstance(parameters, dict):
        parameters = {"value": parameters}
    return name, parameters


# String operations


def _strip(text: str, parameters: Dict[str, Any]) -> str:
    return text.strip(parameters.get("chars"))


def _lstrip(text: str, parameters: Dict[str, Any]) -> str:
    return text.lstrip(parameters.get("chars"))


def _rstrip(text: str, parameters: Dict[str, Any]) -> str:
    return text.rstrip(parameters.get("chars"))


def _lower(text: str, parameters: Dict[str, Any]) -> str:
    return text.lower()


def _upper(text: str, parameters: Dict[str, Any]) -> str:
    return text.upper()


def _casefold(text: str, parameters: Dict[str, Any]) -> str:
    return text.casefold()


def _collapse_whitespace(text: str, parameters: Dict[str, Any]) -> str:
    return re.sub(r"\s+", " ", text).strip()


def _remove_whitespace(text: str, parameters: Dict[str, Any]) -> str:
    return re.sub(r"\s+", "", text)


def _remove(text: str, parameters: Dict[str, Any]) -> str:
    chars = parameters.get("chars", parameters.get("value", ""))
    return text.translate({ord(char): None for char in chars})


def _replace(text: str, parameters: Dict[str, Any]) -> str:
    pattern = parameters.get("pattern", parameters.get("old", ""))
    replacement = parameters.get("with", parameters.get("new", ""))
    if parameters.get("literal"):
        return text.replace(pattern, replacement)
    return re.sub(pattern, replacement, text)


def _map(text: str, parameters: Dict[str, Any]) -> str:
    mapping = parameters.get("values", parameters.get("value", parameters))
    if isinstance(mapping, dict) and text in mapping:
        return mapping[text]
    return text


def _strip_prefix(text: str, parameters: Dict[str, Any]) -> str:
    prefix = parameters.get("value", parameters.get("prefix", ""))
    return text[len(prefix) :] if prefix and text.startswith(prefix) else text


def _strip_suffix(text: str, parameters: Dict[str, Any]) -> str:
    suffix = parameters.get("value", parameters.get("suffix", ""))
    return text[: -len(suffix)] if suffix and text.endswith(suffix) else text


def _prepend(text: str, parameters: Dict[str, Any]) -> str:
    prefix = parameters.get("value", parameters.get("prefix", ""))
    return f"{prefix}{text}"


def _append(text: str, parameters: Dict[str, Any]) -> str:
    suffix = parameters.get("value", parameters.get("suffix", ""))
    return f"{text}{suffix}"


def _zero_pad(text: str, parameters: Dict[str, Any]) -> str:
    width = int(parameters.get("width", parameters.get("value", 0)))
    return text.zfill(width)


def _trim_zeros(text: str, parameters: Dict[str, Any]) -> str:
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text


def _unicode_nfkc(text: str, parameters: Dict[str, Any]) -> str:
    return unicodedata.normalize("NFKC", text)


def _truncate(text: str, parameters: Dict[str, Any]) -> str:
    length = int(parameters.get("length", parameters.get("value", 0)))
    return text[:length]


def _regex_extract(text: str, parameters: Dict[str, Any]) -> str:
    pattern = parameters.get("pattern", "")
    match = re.search(pattern, text)
    if not match:
        return text
    if match.groups():
        return match.group(1)
    return match.group(0)


STRING_OPERATIONS = {
    "strip": _strip,
    "lstrip": _lstrip,
    "rstrip": _rstrip,
    "lower": _lower,
    "upper": _upper,
    "casefold": _casefold,
    "collapse_whitespace": _collapse_whitespace,
    "remove_whitespace": _remove_whitespace,
    "remove": _remove,
    "replace": _replace,
    "map": _map,
    "strip_prefix": _strip_prefix,
    "strip_suffix": _strip_suffix,
    "prepend": _prepend,
    "append": _append,
    "zero_pad": _zero_pad,
    "trim_zeros": _trim_zeros,
    "unicode_nfkc": _unicode_nfkc,
    "truncate": _truncate,
    "regex_extract": _regex_extract,
}


# Tokens


def key_token(values: List[Any]) -> str:
    """Build a stable token from normalized business-key values."""
    parts = []
    for value in values:
        if value is None:
            parts.append("\x00")
        else:
            parts.append(str(value))
    return "\x1f".join(parts)
