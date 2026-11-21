"""
Path-based retention for transient streams.

A :class:`Retain` declaration names the paths whose values must stay available
after a transient object has finished streaming.  Everything else (large log
arrays, unselected audit branches, ...) is still consumed transiently and is
never cached just because the caller happened to read it.
"""
from collections import OrderedDict

from json_stream.base import (
    StreamingJSONBase,
    StreamingJSONList,
    StreamingJSONObject,
    TransientStreamingJSONBase,
)

__all__ = ['Retain', 'RetainedDict', 'RetainedList', 'MISSING', 'ANY']


class _Missing:
    def __repr__(self):
        return '<MISSING>'


#: Sentinel recorded when a retained path is absent from the document.  An
#: explicit JSON ``null`` is retained as ``None``, so the two stay distinct.
MISSING = _Missing()


class _Any:
    def __repr__(self):
        return '<ANY>'


#: Wildcard path segment: apply the rest of the declaration to every element
#: of an array, e.g. ``Retain((Retain.ANY, "header", "order_id"))``.
ANY = _Any()


class Retain:
    """
    Declaration of the paths to retain while reading transiently.

    Paths are tuples of object keys (strings), list indices (integers) or the
    :data:`ANY` wildcard, e.g.
    ``Retain(("header", "order_id"), ("header", "summary", "total"))`` or
    ``Retain((Retain.ANY, "order_id"),)`` for every element of a root array.
    A path may also be given as a single string/int.  An empty path retains the
    whole value of the object the declaration is attached to.
    """

    ANY = ANY

    def __init__(self, *paths):
        tree = {}
        for path in paths:
            if not isinstance(path, tuple):
                path = (path,)
            node = tree
            for segment in path:
                node = node.setdefault(segment, {})
        self._tree = tree

    def subtree(self, segment):
        """Declaration below ``segment``, or ``None`` when it is not selected."""
        return self._tree.get(segment)

    def retains_whole_value(self):
        """True when every value of this object must be retained."""
        return not self._tree

    def __repr__(self):
        return f"Retain({self._tree!r})"


class RetainedDict:
    """
    Retained view of a streamed object.

    Only declared paths are present.  A key that never occurred maps to the
    :data:`MISSING` sentinel (and is listed by :meth:`missing`), while an
    explicit JSON ``null`` is stored as ``None``.  Duplicate keys keep every
    occurrence (see :meth:`occurrences` and :meth:`conflicts`).
    """

    __slots__ = ('_values', '_occurrences', '_missing')

    def __init__(self):
        self._values = OrderedDict()
        self._occurrences = OrderedDict()
        self._missing = []

    def _store(self, key, value):
        self._occurrences.setdefault(key, []).append(value)
        self._values[key] = value

    def mark_missing(self, key):
        if key not in self._values:
            self._values[key] = MISSING
            self._missing.append(key)

    def occurrences(self, key):
        """All values seen for ``key`` in stream order (duplicates included)."""
        return list(self._occurrences.get(key, ()))

    def conflicts(self):
        """Mapping of keys that occurred more than once to all their values."""
        return OrderedDict(
            (key, values) for key, values in self._occurrences.items() if len(values) > 1
        )

    def missing(self):
        """Declared keys that did not occur in the object."""
        return tuple(self._missing)

    def get(self, key, default=None):
        return self._values.get(key, default)

    def __getitem__(self, key):
        return self._values[key]

    def __contains__(self, key):
        return key in self._values

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def keys(self):
        return self._values.keys()

    def items(self):
        return self._values.items()

    def values(self):
        return self._values.values()

    def to_standard(self):
        # declared-but-absent keys are dropped: an explicit null stays as None
        return {
            key: to_standard(value)
            for key, value in self._values.items()
            if value is not MISSING
        }

    def __eq__(self, other):
        if isinstance(other, RetainedDict):
            return self._values == other._values
        return NotImplemented

    def __repr__(self):
        return f"RetainedDict({dict(self._values)!r})"


class RetainedList:
    """Retained view of a streamed array (sparse; only declared indices exist)."""

    __slots__ = ('_values', '_missing')

    def __init__(self):
        self._values = {}
        self._missing = set()

    def _store(self, index, value):
        self._values[index] = value

    def mark_missing(self, index):
        if index not in self._values:
            self._values[index] = MISSING
            self._missing.add(index)

    def missing(self):
        return tuple(sorted(self._missing))

    def get(self, index, default=None):
        return self._values.get(index, default)

    def __getitem__(self, index):
        return self._values[index]

    def __contains__(self, index):
        return index in self._values

    def __iter__(self):
        for index in sorted(self._values):
            yield index, self._values[index]

    def __len__(self):
        return len(self._values)

    def indices(self):
        return sorted(self._values)

    def to_standard(self):
        # present indices only, in index order; declared-but-absent indices
        # remain available via missing() instead of appearing as nulls
        return [to_standard(self._values[index]) for index in sorted(self._values)
                if self._values[index] is not MISSING]

    def __eq__(self, other):
        if isinstance(other, RetainedList):
            return self._values == other._values
        return NotImplemented

    def __repr__(self):
        return f"RetainedList({self._values!r})"


def to_standard(value):
    """Recursively convert retained snapshots to plain dicts/lists."""
    if isinstance(value, (RetainedDict, RetainedList)):
        return value.to_standard()
    if isinstance(value, list):
        return [to_standard(item) for item in value]
    if isinstance(value, dict):
        return {key: to_standard(item) for key, item in value.items()}
    return value


class RetainingStreamingJSONBase(TransientStreamingJSONBase):
    """Transient node that caches only the paths named by a :class:`Retain`."""

    def __init__(self, token_stream, retain, parent_snapshot, segment):
        super().__init__(token_stream)
        self._retain = retain
        self._parent_snapshot = parent_snapshot
        self._segment = segment
        self._retained = self._init_retained()
        self._confirmed = False

    def is_confirmed(self):
        """True once this node has reached its end and registered its snapshot."""
        return self._confirmed

    def _init_retained(self):
        raise NotImplementedError()  # pragma: no cover

    def retained(self):
        """The retained snapshot (populated once this node has finished)."""
        return self._retained

    def _iter_items(self):
        try:
            yield from super()._iter_items()
        finally:
            if not self.streaming and not self._confirmed:
                self._finish()

    def _finish(self):
        self._confirmed = True
        self._mark_missing()
        if self._parent_snapshot is not None:
            self._parent_snapshot._store(self._segment, self._retained)

    def _mark_missing(self):
        raise NotImplementedError()  # pragma: no cover

    def _is_selected(self, segment):
        tree = self._retain._tree
        return self._retain.retains_whole_value() or segment in tree or ANY in tree

    def _create_child(self, token, segment):
        if self._retain.retains_whole_value():
            subtree = self._retain._tree
        else:
            subtree = self._retain.subtree(segment)
            if subtree is None and ANY in self._retain._tree:
                subtree = self._retain._tree[ANY]
        if subtree is None:
            return StreamingJSONBase.factory(token, self._stream, False)
        child_retain = Retain.__new__(Retain)
        child_retain._tree = subtree
        return _RETAIN_FACTORY[token](
            self._stream,
            child_retain,
            self._retained,
            segment,
        )


class RetainingStreamingJSONObject(RetainingStreamingJSONBase, StreamingJSONObject):
    def _init_retained(self):
        return RetainedDict()

    def _mark_missing(self):
        for segment in self._retain._tree:
            if segment is not ANY:
                self._retained.mark_missing(segment)

    def _on_value(self, key, value):
        if self._is_selected(key):
            self._retained._store(key, value)


class RetainingStreamingJSONList(RetainingStreamingJSONBase, StreamingJSONList):
    def __init__(self, token_stream, retain, parent_snapshot, segment):
        super().__init__(token_stream, retain, parent_snapshot, segment)
        self._index = -1

    def _init_retained(self):
        return RetainedList()

    def _mark_missing(self):
        for index in self._retain._tree:
            if index is not ANY:
                self._retained.mark_missing(index)

    def _on_value(self, value):
        if self._is_selected(self._index):
            self._retained._store(self._index, value)


_RETAIN_FACTORY = {
    '{': RetainingStreamingJSONObject,
    '[': RetainingStreamingJSONList,
}


def create_retaining_root(token, token_stream, retain):
    """Create the root retaining node for a top-level ``{`` or ``[`` token."""
    try:
        klass = _RETAIN_FACTORY[token]
    except KeyError:
        raise ValueError(f"Unknown operator {token}") from None
    return klass(token_stream, retain, None, None)
