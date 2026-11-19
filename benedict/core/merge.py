from __future__ import annotations

from collections.abc import Mapping, MutableMapping, Sequence
from typing import Any, TypeVar

from benedict.core.source_map import MergeResult, SourceMap
from benedict.utils import type_util

_K = TypeVar("_K")
_V = TypeVar("_V")

_Path = tuple[Any, ...]
_Labels = Any


def _merge_dict(
    d: MutableMapping[_K, _V],
    other: Mapping[_K, _V],
    overwrite: bool = True,
    concat: bool = False,
    sources: SourceMap | None = None,
    label: Any = None,
    path: _Path = (),
) -> None:
    for key, value in other.items():
        _merge_item(
            d,
            key,
            value,
            overwrite=overwrite,
            concat=concat,
            sources=sources,
            label=label,
            path=path,
        )


def _merge_item(
    d: MutableMapping[_K, _V],
    key: _K,
    value: _V,
    overwrite: bool = True,
    concat: bool = False,
    sources: SourceMap | None = None,
    label: Any = None,
    path: _Path = (),
) -> None:
    item_path = path + (key,)
    if key in d:
        item = d.get(key)
        if type_util.is_dict(item) and type_util.is_dict(value):
            # sub-dictionaries are merged: existing fields keep their source
            # and only the fields coming from 'other' are re-labelled.
            _merge_dict(
                item,
                value,
                overwrite=overwrite,
                concat=concat,
                sources=sources,
                label=label,
                path=item_path,
            )
        elif concat and type_util.is_list(item) and type_util.is_list(value):
            first_new_index = len(item) if sources is not None else 0
            item += value  # type: ignore[assignment]
            if sources is not None:
                for index in range(first_new_index, len(item)):
                    sources.assign(item_path + (index,), label, item[index])
        elif overwrite:
            d[key] = value
            if sources is not None:
                # the item is replaced as a whole: the sources of its previous
                # nested fields must not leak into the new ones.
                sources.assign(item_path, label, value)
    else:
        d[key] = value
        if sources is not None:
            sources.assign(item_path, label, value)


def _merge_labels(
    others: Sequence[Mapping[Any, Any]], sources: _Labels
) -> list[Any]:
    if sources is None:
        labels = ["base"]
        labels += [
            "other" if index == 0 else f"other{index + 1}"
            for index in range(len(others))
        ]
        return labels
    if isinstance(sources, (str, bytes)):
        # a single label applied to all the merged dicts, the starting
        # dict keeps the 'base' label
        return ["base"] + [sources] * len(others)
    if isinstance(sources, Mapping) or not isinstance(sources, Sequence):
        raise TypeError(
            "The 'sources' argument must be a label or a sequence of labels, "
            f"got: {type(sources).__name__}."
        )
    labels = list(sources)
    if len(labels) != len(others) + 1:
        raise ValueError(
            "The 'sources' argument must contain one label for the starting "
            f"dict and one for each merged dict, expected {len(others) + 1}, "
            f"got {len(labels)}."
        )
    return labels


def merge_sources(
    d: MutableMapping[Any, Any],
    other: Mapping[Any, Any],
    *args: Mapping[Any, Any],
    sources: _Labels = None,
    base_sources: SourceMap | None = None,
    overwrite: bool = True,
    concat: bool = False,
    separator: str = ".",
) -> SourceMap:
    """
    Merge one or more dicts into the given dict, tracking the source of every
    merged field.

    The 'sources' argument contains the label of each merged mapping, in order,
    including the starting dict: eg. sources=["yaml", "json"]. A single label
    is applied to all the merged dicts. If omitted, the starting dict is
    labelled "base" and each other mapping "other", "other2", and so on.
    The 'base_sources' argument contains the sources already recorded for the
    starting dict; fields that are not overwritten keep their existing source.

    Return a SourceMap that can be queried by keypath or by keylist to know
    which source a specific field comes from. The 'separator' argument is the
    keypath separator used to query the returned source map.
    """
    others = [other] + list(args)
    labels = _merge_labels(others, sources)
    source_map = (
        base_sources.copy(separator=separator)
        if base_sources is not None
        else SourceMap(separator=separator)
    )
    if base_sources is None:
        source_map.update(d, labels[0])
    for other, label in zip(others, labels[1:]):
        _merge_dict(
            d,
            other,
            overwrite=overwrite,
            concat=concat,
            sources=source_map,
            label=label,
        )
    return source_map


def merge_with_sources(
    d: MutableMapping[Any, Any],
    other: Mapping[Any, Any],
    *args: Mapping[Any, Any],
    **kwargs: Any,
) -> MergeResult:
    """
    Merge one or more dicts into the given dict, like 'merge_sources', and
    return both the merged dict and the source map of its fields.
    """
    sources = merge_sources(d, other, *args, **kwargs)
    return MergeResult(merged=d, sources=sources)


def merge(
    d: MutableMapping[_K, _V], other: Mapping[_K, _V], *args: Any, **kwargs: Any
) -> Any:
    sources = kwargs.pop("sources", None)
    source = kwargs.pop("source", None)
    track_sources = kwargs.pop("track_sources", False)
    others = [other] + list(args)
    if sources is None and source is not None:
        sources = ["base"] + [source] * len(others)
    if sources is not None:
        return merge_sources(d, *others, sources=sources, **kwargs)
    if track_sources:
        return merge_sources(d, *others, **kwargs)
    overwrite = kwargs.get("overwrite", True)
    concat = kwargs.get("concat", False)
    for other in others:
        _merge_dict(d, other, overwrite=overwrite, concat=concat)
    return d
