from __future__ import annotations

import re
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from typing import Any

from slugify import slugify

from benedict.core.rename import rename
from benedict.core.traverse import traverse
from benedict.exceptions import StandardizationConflictError
from benedict.utils import type_util


def _standardize_key(key: str) -> str:
    # https://stackoverflow.com/a/12867228/2096218
    norm_key = re.sub(r"((?<=[a-z0-9])[A-Z]|(?!^)[A-Z](?=[a-z]))", r"_\1", key)
    return str(slugify(norm_key, separator="_"))


def _standardize_item(d: MutableMapping[Any, Any], key: Any, value: Any) -> None:
    if type_util.is_string(key):
        norm_key = _standardize_key(key)
        rename(d, key, norm_key)


@dataclass(frozen=True)
class StandardizationChange:
    """
    A single key rename detected while planning a standardization.

    ``original_path`` is the full path of the field in the untouched
    input (using the original names of every parent level).
    """

    original_path: str
    original_key: str
    standardized_key: str


@dataclass(frozen=True)
class StandardizationConflict:
    """
    A mapping whose keys would collide after standardization.

    ``path`` is the original path of the parent mapping (an empty string
    for the root mapping), ``original_paths`` points at every field
    involved in the collision.
    """

    path: str
    standardized_key: str
    original_keys: tuple[str, ...]
    original_paths: tuple[str, ...]


@dataclass
class StandardizationPlan:
    """
    The result of a dry-run standardization.

    It contains every planned :class:`StandardizationChange`, every
    detected :class:`StandardizationConflict`, and a ``preview`` of the
    fully standardized structure. The preview is computed without
    touching the original input; at conflict levels only one of the
    colliding fields can be represented, so ``conflicts`` must always be
    checked alongside it.
    """

    changes: list[StandardizationChange] = field(default_factory=list)
    conflicts: list[StandardizationConflict] = field(default_factory=list)
    preview: Any = None

    @property
    def has_conflicts(self) -> bool:
        return bool(self.conflicts)


def _join_path(parent_path: str, key: Any) -> str:
    key_path = f"{key}"
    return f"{parent_path}.{key_path}" if parent_path else key_path


def _indexed_path(parent_path: str, index: int) -> str:
    return f"{parent_path}[{index}]"


def _record_conflicts(
    sources: dict[Any, list[tuple[Any, str]]],
    parent_path: str,
    conflicts: list[StandardizationConflict],
) -> None:
    for standardized_key, key_sources in sources.items():
        if len(key_sources) > 1 and type_util.is_string(standardized_key):
            conflicts.append(
                StandardizationConflict(
                    path=parent_path,
                    standardized_key=standardized_key,
                    original_keys=tuple(
                        original_key for original_key, _ in key_sources
                    ),
                    original_paths=tuple(
                        original_path for _, original_path in key_sources
                    ),
                )
            )


def _build_dict(
    d: MutableMapping[Any, Any],
    parent_path: str,
    changes: list[StandardizationChange],
    conflicts: list[StandardizationConflict],
) -> dict[Any, Any]:
    sources: dict[Any, list[tuple[Any, str]]] = {}
    standardized_keys: dict[Any, Any] = {}
    for original_key in d.keys():
        standardized_key = (
            _standardize_key(original_key)
            if type_util.is_string(original_key)
            else original_key
        )
        standardized_keys[original_key] = standardized_key
        original_path = _join_path(parent_path, original_key)
        sources.setdefault(standardized_key, []).append(
            (original_key, original_path)
        )
        if (
            type_util.is_string(original_key)
            and standardized_key != original_key
        ):
            changes.append(
                StandardizationChange(
                    original_path=original_path,
                    original_key=original_key,
                    standardized_key=standardized_key,
                )
            )
    _record_conflicts(sources, parent_path, conflicts)
    return {
        standardized_keys[original_key]: _build_value(
            value,
            _join_path(parent_path, original_key),
            changes,
            conflicts,
        )
        for original_key, value in d.items()
    }


def _build_value(
    value: Any,
    parent_path: str,
    changes: list[StandardizationChange],
    conflicts: list[StandardizationConflict],
) -> Any:
    if type_util.is_dict(value):
        return _build_dict(value, parent_path, changes, conflicts)
    if type_util.is_list_or_tuple(value):
        items = [
            _build_value(item, _indexed_path(parent_path, index), changes, conflicts)
            for index, item in enumerate(value)
        ]
        return tuple(items) if type_util.is_tuple(value) else items
    return value


def build_standardization_plan(d: MutableMapping[Any, Any]) -> StandardizationPlan:
    """
    Scan the input without modifying it and return a
    :class:`StandardizationPlan` describing every rename, every conflict
    and a preview of the fully standardized structure.
    """
    changes: list[StandardizationChange] = []
    conflicts: list[StandardizationConflict] = []
    preview = _build_value(d, "", changes, conflicts)
    return StandardizationPlan(
        changes=changes,
        conflicts=conflicts,
        preview=preview,
    )


def _apply_plan(d: MutableMapping[Any, Any], plan: StandardizationPlan) -> None:
    # The plan is validated first, so the original input is replaced
    # atomically only when the whole standardization passes.
    d.clear()
    d.update(plan.preview)


def standardize(d: MutableMapping[Any, Any], *, strict: bool = False) -> None:
    plan = build_standardization_plan(d)
    if plan.has_conflicts and strict:
        raise StandardizationConflictError(plan)
    if plan.has_conflicts:
        # Backward-compatible behavior: rename keys in place and let the
        # first collision raise a KeyError, as it did before strict mode.
        traverse(d, _standardize_item)  # type: ignore[arg-type]
        return
    _apply_plan(d, plan)
