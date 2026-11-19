from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any, NamedTuple

PathType = tuple[Any, ...]

_MISSING: Any = object()


class MergeResult(NamedTuple):
    """
    Result of a source-tracked merge.
    It can be unpacked (``merged, sources = ...``) or queried by attribute.
    """

    merged: Any
    sources: SourceMap

    @property
    def data(self) -> Any:
        return self.merged

    @property
    def value(self) -> Any:
        return self.merged

    @property
    def source_map(self) -> SourceMap:
        return self.sources

    @property
    def field_sources(self) -> SourceMap:
        return self.sources


def normalize_path(key: Any, separator: str | None = ".") -> PathType:
    """
    Normalize a key (keypath string, keylist, tuple, or single key) to a path tuple.
    """
    if isinstance(key, tuple):
        return key
    if isinstance(key, list):
        return tuple(key)
    if separator and isinstance(key, str):
        # imported here to avoid a circular import with 'benedict.dicts'
        from benedict.dicts.keypath import keypath_util

        keys = keypath_util.parse_keys(key, separator)
        if keys:
            return tuple(keys)
    return (key,)


def format_path(path: Sequence[Any], separator: str = ".") -> str:
    """
    Format a path tuple as a keypath string, eg. ('a', 'b', 0) -> 'a.b[0]'.
    """
    value = ""
    for key in path:
        if isinstance(key, int):
            value += f"[{key}]"
        else:
            value = f"{value}{separator}{key}" if value else f"{key}"
    return value


class SourceNode(str):
    """
    A source label that also exposes the sources of its nested fields.

    It behaves like the source label string, eg. ``node == "yaml"``, while
    ``node["field"]`` returns the source of the nested field.
    """

    _children: SourceMap

    def __new__(cls, label: Any, children: SourceMap) -> SourceNode:
        obj = super().__new__(cls, label)
        obj._children = children
        return obj

    def __getitem__(self, key: Any) -> Any:
        if isinstance(key, slice):
            return super().__getitem__(key)
        if key in self._children:
            return self._children[key]
        raise KeyError(key)

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    @property
    def sources(self) -> SourceMap:
        return self._children


class SourceMap:
    """
    Map of the source of every merged field.

    Fields can be queried by keypath string (``"a.b"``), keylist (``["a", "b"]``)
    or tuple (``("a", "b")``). A source map for a nested field is returned when
    the queried path is a parent of other tracked fields::

        sources["a.b"]          # -> "yaml"
        sources[["a", "b"]]     # -> "yaml"
        sources["a"]["b"]       # -> "yaml"
    """

    def __init__(
        self,
        sources: Mapping[Any, Any] | None = None,
        separator: str | None = ".",
    ) -> None:
        self._separator = separator
        self._labels: dict[PathType, Any] = {}
        if sources:
            for key, value in sources.items():
                self.set(normalize_path(key, separator), value)

    def __call__(self) -> SourceMap:
        # allows both 'd.sources["a.b"]' and 'd.sources()["a.b"]'
        return self

    def __bool__(self) -> bool:
        return bool(self._labels)

    def __contains__(self, key: object) -> bool:
        path = normalize_path(key, self._separator)
        if path in self._labels:
            return True
        return any(label_path[: len(path)] == path for label_path in self._labels)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, SourceMap):
            return self._labels == other._labels
        if isinstance(other, Mapping):
            return self.flat == dict(other)
        return NotImplemented

    def __getitem__(self, key: Any) -> Any:
        path = normalize_path(key, self._separator)
        label = self._labels.get(path, _MISSING)
        children = self._submap(path)
        if children:
            if isinstance(label, str):
                return SourceNode(label, children)
            if label is not _MISSING and label is not None:
                return label
            return children
        if label is _MISSING:
            raise KeyError(key)
        return label

    def __iter__(self) -> Iterator[str]:
        return iter(self.flat)

    def __len__(self) -> int:
        return len(self._labels)

    def __repr__(self) -> str:
        return repr(self.flat)

    def get(self, key: Any, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def items(self) -> Iterator[tuple[str, Any]]:
        return iter(self.flat.items())

    def keys(self) -> Iterator[str]:
        return iter(self.flat)

    def values(self) -> Iterator[Any]:
        return iter(self.flat.values())

    @property
    def flat(self) -> dict[str, Any]:
        """
        Flat source map using keypath strings as keys.
        """
        return {
            format_path(path, self._separator or "."): label
            for path, label in self._labels.items()
        }

    @property
    def separator(self) -> str | None:
        return self._separator

    def as_dict(self, nested: bool = False) -> dict[str, Any]:
        """
        Return the source map as a dict.
        If nested is False the keys are keypath strings, otherwise the result
        mirrors the structure of the merged dict and a parent that is itself a
        source is represented by its label under the '__source__' key.
        """
        if nested:
            return self.to_nested()
        return self.flat

    @property
    def nested(self) -> dict[str, Any]:
        return self.to_nested()

    def to_nested(self) -> dict[str, Any]:
        root: dict[str, Any] = {}
        for path, label in sorted(
            self._labels.items(), key=lambda item: len(item[0])
        ):
            keys = [str(key) for key in path]
            if not keys:
                root["__source__"] = label
                continue
            node = root
            for key in keys[:-1]:
                child = node.get(key)
                if not isinstance(child, dict):
                    child = {} if child is None else {"__source__": child}
                    node[key] = child
                node = child
            leaf = keys[-1]
            current = node.get(leaf)
            if isinstance(current, dict):
                current["__source__"] = label
            else:
                node[leaf] = label
        return root

    def copy(self, separator: str | None = None) -> SourceMap:
        if separator is None:
            separator = self._separator
        return SourceMap(self._labels, separator=separator)

    def label(self, path: Sequence[Any]) -> Any:
        """
        Return the source label assigned to the given path, if any.
        """
        return self._labels.get(tuple(path))

    def set(self, path: Sequence[Any], label: Any) -> None:
        self._labels[tuple(path)] = label

    def clear(self, path: Sequence[Any] | None = None) -> None:
        """
        Remove the source of the given path and of all its nested fields.
        If path is None, all sources are removed.
        """
        if path is None:
            self._labels.clear()
            return
        keys = tuple(path)
        keys_count = len(keys)
        for label_path in list(self._labels.keys()):
            if label_path[:keys_count] == keys:
                del self._labels[label_path]

    def assign(self, path: Sequence[Any], label: Any, value: Any = None) -> None:
        """
        Assign the source label to the given path and, optionally, to all the
        fields nested in the given value.
        """
        keys = tuple(path)
        # a reassigned item is replaced as a whole: the sources of its previous
        # nested fields must not leak into the new ones.
        self.clear(keys)
        self.set(keys, label)
        if value is not None:
            self._assign_value(keys, value, label)

    def update(self, value: Any, label: Any) -> None:
        """
        Assign the source label to all the fields nested in the given value.
        """
        self._assign_value((), value, label)

    def _assign_value(self, path: PathType, value: Any, label: Any) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                item_path = path + (key,)
                self.set(item_path, label)
                self._assign_value(item_path, item, label)
        elif isinstance(value, (list, tuple)):
            for index, item in enumerate(value):
                item_path = path + (index,)
                self.set(item_path, label)
                self._assign_value(item_path, item, label)

    def submap(self, path: Sequence[Any]) -> SourceMap | None:
        """
        Return the source map of the fields nested under the given path,
        or None if no field is nested under it.
        """
        return self._submap(tuple(path))

    def _submap(self, path: PathType) -> SourceMap | None:
        keys_count = len(path)
        labels = {
            label_path[keys_count:]: label
            for label_path, label in self._labels.items()
            if len(label_path) > keys_count and label_path[:keys_count] == path
        }
        if not labels:
            return None
        return SourceMap(labels, separator=self._separator)
