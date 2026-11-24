"""Point handoff and reply resolution for sparse monitoring grids.

This module is an *opt-in* extension on top of :class:`xarray.Dataset`.  It
lets a gridded survey be handed to external processors as a one-dimensional
point table and lets their replies be evaluated per original grid cell.  The
archival grid is not rewritten: axis order, index labels and positions are
preserved, and a calibration that varies only along a track stays attached
to that track.

Point numbers are only meaningful inside one handoff batch, so a point is
identified by ``(processor, handoff_batch, point_number)``.  Every offset
reply carries such an identity at both endpoints and may appear in any row
order.  Replies are mapped back to the grid through the handoff tables, so
different processors may use different point numbers for the same cell.

Resolution rules
----------------
Offsets are signed integer millimetres and can be inverted.  A cell is
delivered only when at least one simple route (no repeated grid position)
reaches an absolute reading, and every simple route to every absolute
reading of the same connected component implies the same value.  Cells that
were explicitly reported missing, handed off without a reply, never handed
off, or only have offsets without a route to an absolute reading stay
distinguishable.

Value uniqueness is decided from the offset graph's biconnected blocks
instead of listing every route.  Inside a biconnected block all simple
routes imply the same signed difference exactly when the block's offset
potentials are consistent.  If a block is inconsistent, two concrete routes
are reconstructed only for the conflict report.
"""

from __future__ import annotations

import json
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, cast

import numpy as np
import pandas as pd

import xarray as xr
from xarray.core.indexing import (
    ExplicitlyIndexedNDArrayMixin,
    ExplicitIndexer,
    OuterIndexer,
    VectorizedIndexer,
)
from xarray.core.variable import Variable

__all__ = [
    "CellResolution",
    "ConflictDetail",
    "HandoffCatalog",
    "HandoffRow",
    "JsonFileBlock",
    "MemoryBlock",
    "MetadataBlock",
    "PointHandoffAccessor",
    "ReplyCatalog",
    "RouteBranch",
    "RouteStep",
    "attach",
    "build_handoff",
    "load_catalog",
    "load_handoff",
    "load_replies",
    "open_point_handoff",
    "register_point_handoff_accessor",
    "save_catalog",
    "save_handoff",
    "save_replies",
]

_HANDOFF_ATTR = "__point_handoff__"
_POSITION_PREFIX = "__point_handoff_position__"

Status = Literal[
    "value",
    "conflict",
    "pending_baseline",
    "explicitly_missing",
    "no_reply",
    "never_handed_off",
]

STATUS_LABELS: dict[str, str] = {
    "value": "值",
    "conflict": "冲突",
    "pending_baseline": "待定基准",
    "explicitly_missing": "明确报缺",
    "no_reply": "交出但无回包",
    "never_handed_off": "从未交出",
}

PointRef = tuple[str, str, int]
Position = tuple[int, ...]


@dataclass(frozen=True)
class RouteStep:
    """One signed offset edge used on a route.

    Attributes
    ----------
    from_point, to_point : tuple of (str, str, int)
        Endpoint identities ``(processor, handoff batch, point number)``.
    offset_mm : int
        Signed difference ``value(to_point) - value(from_point)`` in integer
        millimetres.
    block : str
        Independently readable number block holding the offset.
    from_position, to_position : tuple of int
        Original grid positions of the endpoints.
    """

    from_point: PointRef
    to_point: PointRef
    offset_mm: int
    block: str
    from_position: Position
    to_position: Position

    def as_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation."""
        return {
            "from_point": self.from_point,
            "to_point": self.to_point,
            "offset_mm": self.offset_mm,
            "block": self.block,
            "from_position": self.from_position,
            "to_position": self.to_position,
        }


@dataclass(frozen=True)
class RouteBranch:
    """A simple route from a queried position to an absolute reading.

    Attributes
    ----------
    value_mm : int
        Value implied by this route.
    anchor : tuple of (str, str, int)
        Point identity carrying the absolute reading.
    absolute_mm : int
        The absolute reading itself.
    steps : tuple of RouteStep
        Offset steps in walking order.
    blocks : tuple of str
        Number blocks used by the route, in walking order.
    """

    value_mm: int
    anchor: PointRef
    absolute_mm: int
    steps: tuple[RouteStep, ...]
    blocks: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation."""
        return {
            "value_mm": self.value_mm,
            "anchor": self.anchor,
            "absolute_mm": self.absolute_mm,
            "steps": tuple(step.as_dict() for step in self.steps),
            "blocks": self.blocks,
        }


@dataclass(frozen=True)
class ConflictDetail:
    """Two routes that imply different values for the same grid position.

    Attributes
    ----------
    first, second : RouteBranch
        The two disagreeing routes in deterministic order.
    """

    first: RouteBranch
    second: RouteBranch

    @property
    def values_mm(self) -> tuple[int, int]:
        """The two implied values in integer millimetres."""
        return (self.first.value_mm, self.second.value_mm)

    def as_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation."""
        return {
            "values_mm": self.values_mm,
            "routes": (self.first.as_dict(), self.second.as_dict()),
        }


@dataclass(frozen=True)
class CellResolution:
    """Resolution of one grid cell.

    Attributes
    ----------
    status : str
        ``"value"``, ``"conflict"``, ``"pending_baseline"``,
        ``"explicitly_missing"``, ``"no_reply"`` or ``"never_handed_off"``.
    position : tuple of int
        Positional index in the original grid, in original axis order.
    points : tuple of (str, str, int)
        Handoff point identities referring to this cell.
    value_mm : int or None
        Delivered value in integer millimetres.
    routes : tuple of RouteBranch
        Routes used for the decision.  For a conflict at least the two
        disagreeing routes are present.
    conflict : ConflictDetail or None
        The disagreement, when ``status == "conflict"``.
    reason : str or None
        Explanation for non-delivered cells.
    blocks : tuple of str
        Number blocks used for this cell.
    """

    status: str
    position: Position
    points: tuple[PointRef, ...] = ()
    value_mm: int | None = None
    routes: tuple[RouteBranch, ...] = ()
    conflict: ConflictDetail | None = None
    reason: str | None = None
    blocks: tuple[str, ...] = ()

    @property
    def value(self) -> int | None:
        """Alias of :attr:`value_mm`."""
        return self.value_mm

    @property
    def status_label(self) -> str:
        """Human readable status label."""
        return STATUS_LABELS[self.status]

    @property
    def grid_position(self) -> Position:
        """Alias of :attr:`position`."""
        return self.position

    def as_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation."""
        return {
            "status": self.status,
            "status_label": self.status_label,
            "position": self.position,
            "points": self.points,
            "value_mm": self.value_mm,
            "routes": tuple(route.as_dict() for route in self.routes),
            "conflict": None if self.conflict is None else self.conflict.as_dict(),
            "reason": self.reason,
            "blocks": self.blocks,
        }


@dataclass(frozen=True)
class HandoffRow:
    """One row of a one-dimensional handoff point table.

    Attributes
    ----------
    point : int
        Within-batch point number.
    position : tuple of int
        Positional index in the original grid, in original axis order.
    track : any
        Original track label.
    lon : any
        Original longitude label.
    note : str or None
        Free-form note carried through to the point table.
    """

    point: int
    position: Position
    track: Any = None
    lon: Any = None
    note: str | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a plain dictionary representation."""
        return {
            "point": self.point,
            "position": list(self.position),
            "track": _jsonable(self.track),
            "lon": _jsonable(self.lon),
            "note": self.note,
        }


@dataclass
class HandoffCatalog:
    """One handoff batch: a one-dimensional table of grid cells.

    Attributes
    ----------
    processor : str
        Processor receiving the batch.
    batch : str
        Handoff batch identifier.
    rows : tuple of HandoffRow
        Point rows in table order.
    axes : tuple of str
        Original grid axis names, in order.
    axis_values : mapping
        Original index labels of each axis.
    attrs : dict
        Free-form metadata.
    """

    processor: str
    batch: str
    rows: tuple[HandoffRow, ...]
    axes: tuple[str, ...]
    axis_values: dict[str, tuple[Any, ...]] = field(default_factory=dict)
    attrs: dict[str, Any] = field(default_factory=dict)

    @property
    def point_ids(self) -> tuple[int, ...]:
        """Within-batch point numbers in table order."""
        return tuple(row.point for row in self.rows)

    def table(self) -> pd.DataFrame:
        """Return the handoff as a one-dimensional point table."""
        data: dict[str, Any] = {"point": [row.point for row in self.rows]}
        for index, axis in enumerate(self.axes):
            data[f"{axis}_position"] = [row.position[index] for row in self.rows]
            if axis in self.axis_values:
                data[axis] = [
                    self.axis_values[axis][row.position[index]] for row in self.rows
                ]
        data["track"] = [row.track for row in self.rows]
        data["lon"] = [row.lon for row in self.rows]
        data["note"] = [row.note for row in self.rows]
        return pd.DataFrame(data)

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable handoff description."""
        return {
            "processor": self.processor,
            "batch": self.batch,
            "axes": list(self.axes),
            "axis_values": {
                key: [_jsonable(value) for value in values]
                for key, values in self.axis_values.items()
            },
            "points": [row.as_dict() for row in self.rows],
            "attrs": dict(self.attrs),
        }

    @classmethod
    def from_dict(cls, mapping: Mapping[str, Any]) -> HandoffCatalog:
        """Rebuild a handoff description from :meth:`to_dict` output."""
        return cls(
            processor=str(mapping["processor"]),
            batch=str(mapping["batch"]),
            rows=tuple(
                HandoffRow(
                    point=int(row["point"]),
                    position=tuple(int(p) for p in row["position"]),
                    track=row.get("track"),
                    lon=row.get("lon"),
                    note=row.get("note"),
                )
                for row in mapping["points"]
            ),
            axes=tuple(mapping["axes"]),
            axis_values={
                key: tuple(value)
                for key, value in mapping.get("axis_values", {}).items()
            },
            attrs=dict(mapping.get("attrs", {})),
        )


def _jsonable(value: Any) -> Any:
    if isinstance(value, np.generic):
        return value.item()
    return value


class ReplyBlock(Protocol):
    """An independently readable reply number block.

    ``metadata`` holds only the points named by the block and, for offsets,
    both endpoints of every row.  ``read`` returns the numbers.
    """

    name: str
    kind: str

    def metadata(self) -> Any:
        """Return metadata without reading numbers."""
        ...

    def read(self) -> Any:
        """Read and return the numbers."""
        ...


@dataclass
class MemoryBlock:
    """A reply block kept in memory."""

    name: str
    kind: str
    meta: Any
    values: Any

    def metadata(self) -> Any:
        """Return metadata without reading numbers."""
        return self.meta

    def read(self) -> Any:
        """Return the block numbers."""
        return self.values


@dataclass
class JsonFileBlock:
    """A reply block stored in its own JSON file."""

    name: str
    kind: str
    meta: Any
    path: Path

    def metadata(self) -> Any:
        """Return metadata without reading numbers."""
        return self.meta

    def read(self) -> Any:
        """Load the block numbers from its file."""
        with open(self.path, encoding="utf-8") as handle:
            return json.load(handle)


def _parse_block_payload(kind: str, rows: Sequence[Mapping[str, Any]]) -> Any:
    if kind == "offset":
        parsed: dict[tuple[PointRef, PointRef], int] = {}
        for row in rows:
            start = _point_id(
                (row["start"]["processor"], row["start"]["batch"], row["start"]["point"])
            )
            end = _point_id(
                (row["end"]["processor"], row["end"]["batch"], row["end"]["point"])
            )
            parsed[(start, end)] = int(row["offset_mm"])
        return parsed
    if kind == "absolute":
        return {
            _point_id((row["processor"], row["batch"], row["point"])): int(
                row["value_mm"]
            )
            for row in rows
        }
    return {
        _point_id((row["processor"], row["batch"], row["point"])): str(row["status"])
        for row in rows
    }


def _edge_sort_key(edge: "_Edge") -> tuple[Any, ...]:
    """Canonical ordering independent of reply row order and block layout."""
    return (
        edge.start,
        edge.end,
        edge.block,
        edge.start_position,
        edge.end_position,
    )


def _point_id(value: Any) -> PointRef:
    processor, batch, point = value
    return (str(processor), str(batch), int(point))


def _point_dict(point: PointRef) -> dict[str, Any]:
    return {"processor": point[0], "batch": point[1], "point": point[2]}


class ReplyCatalog:
    """Reply rows, separated from their independently readable blocks.

    Parameters
    ----------
    absolutes : mapping of block to ``{point_identity: value_mm}``
        Absolute readings.  Each block can be read on its own.
    offsets : mapping of block to rows of ``(start, end, offset_mm)``
        Signed offsets between point identities.
    statuses : mapping of block to ``{point_identity: status}``
        Per-point status rows, for example explicit missing reports.
    blocks : sequence of blocks, optional
        Pre-built or file-backed blocks, instead of the mappings above.
    """

    def __init__(
        self,
        absolutes: Mapping[str, Mapping[PointRef, int]] | None = None,
        offsets: Mapping[str, Iterable[Sequence[Any]]] | None = None,
        statuses: Mapping[str, Mapping[PointRef, str]] | None = None,
        *,
        blocks: Sequence[ReplyBlock] | None = None,
    ) -> None:
        if blocks is not None and any(
            value is not None for value in (absolutes, offsets, statuses)
        ):
            raise TypeError("pass either blocks or the mapping arguments, not both")
        built: list[ReplyBlock] = list(blocks or ())
        for name, values in (absolutes or {}).items():
            parsed = {_point_id(key): int(value) for key, value in values.items()}
            built.append(
                MemoryBlock(
                    name=str(name),
                    kind="absolute",
                    meta=tuple(parsed),
                    values=parsed,
                )
            )
        for name, rows in (offsets or {}).items():
            parsed_offsets: dict[tuple[PointRef, PointRef], int] = {}
            order: list[tuple[PointRef, PointRef]] = []
            for row in rows:
                start, end, value = row
                key = (_point_id(start), _point_id(end))
                if key in parsed_offsets and parsed_offsets[key] != int(value):
                    raise ValueError(
                        f"block {name!r} gives conflicting offsets for {key!r}"
                    )
                if key not in parsed_offsets:
                    order.append(key)
                parsed_offsets[key] = int(value)
            built.append(
                MemoryBlock(
                    name=str(name),
                    kind="offset",
                    meta=tuple(order),
                    values=parsed_offsets,
                )
            )
        for name, status_values in (statuses or {}).items():
            parsed_status = {
                _point_id(key): str(value) for key, value in status_values.items()
            }
            built.append(
                MemoryBlock(
                    name=str(name),
                    kind="status",
                    meta=parsed_status,
                    values=parsed_status,
                )
            )
        self._blocks: dict[str, ReplyBlock] = {}
        self._indexes: dict[str, Any] = {}
        for block in built:
            if block.name in self._blocks:
                raise ValueError(f"duplicate reply block name {block.name!r}")
            self._blocks[block.name] = block
        for block in self._blocks.values():
            self._indexes[block.name] = block.metadata()
        self._cache: dict[str, Any] = {}
        self.reset_read_log()

    @property
    def blocks(self) -> tuple[ReplyBlock, ...]:
        """Blocks in name order."""
        return tuple(self._blocks[name] for name in sorted(self._blocks))

    def _blocks_of_kind(self, kind: str) -> tuple[str, ...]:
        return tuple(
            sorted(name for name, block in self._blocks.items() if block.kind == kind)
        )

    @property
    def absolute_blocks(self) -> tuple[str, ...]:
        """Absolute-reading block names, sorted."""
        return self._blocks_of_kind("absolute")

    @property
    def offset_blocks(self) -> tuple[str, ...]:
        """Offset block names, sorted."""
        return self._blocks_of_kind("offset")

    @property
    def status_blocks(self) -> tuple[str, ...]:
        """Status block names, sorted."""
        return self._blocks_of_kind("status")

    def reset_read_log(self) -> None:
        """Forget which number blocks have been read so far."""
        self._read: list[str] = []
        self._read_set: set[str] = set()

    def _mark_read(self, name: str) -> None:
        if name not in self._read_set:
            self._read_set.add(name)
            self._read.append(name)

    def read_block(self, name: str) -> Any:
        """Read a number block once, remembering that it was read.

        In-memory blocks already hold their values.  File-backed blocks are
        parsed from their JSON rows into the same mapping shape.
        """
        cached = self._cache.get(name)
        if cached is None:
            self._mark_read(name)
            raw = self._blocks[name].read()
            if isinstance(raw, list):
                cached = _parse_block_payload(self._blocks[name].kind, raw)
            else:
                cached = raw
            self._cache[name] = cached
        return cached

    @property
    def read_blocks(self) -> tuple[str, ...]:
        """Block names read since the last :meth:`reset_read_log`."""
        return tuple(sorted(self._read))

    @property
    def read_log(self) -> tuple[str, ...]:
        """Block names in first-read order."""
        return tuple(self._read)

    def absolute_points(self) -> tuple[PointRef, ...]:
        """Points with an absolute reading, from block indexes only."""
        found: list[PointRef] = []
        for name in self.absolute_blocks:
            for point in self._indexes[name]:
                if point not in found:
                    found.append(point)
        return tuple(found)

    def explicitly_missing_points(self) -> tuple[PointRef, ...]:
        """Points explicitly reported missing, from block indexes only."""
        found: list[PointRef] = []
        for name in self.status_blocks:
            for point, status in self._indexes[name].items():
                if status == "explicitly_missing" and point not in found:
                    found.append(point)
        return tuple(found)

    def offset_endpoints(self) -> tuple[dict[str, Any], ...]:
        """Both endpoints of every offset, from block indexes only."""
        result = []
        for name in self.offset_blocks:
            for start, end in self._indexes[name]:
                result.append(
                    {"block": name, "start_point": start, "end_point": end}
                )
        return tuple(result)

    def catalog(self) -> dict[str, Any]:
        """Summarise reply availability without reading number blocks."""
        return {
            "absolutes": list(self.absolute_points()),
            "explicitly_missing": list(self.explicitly_missing_points()),
            "offset_endpoints": [dict(item) for item in self.offset_endpoints()],
            "absolute_blocks": list(self.absolute_blocks),
            "offset_blocks": list(self.offset_blocks),
            "status_blocks": list(self.status_blocks),
        }

    def has_absolute(self, point: PointRef) -> bool:
        """Whether any absolute block names the point."""
        return any(point in self._indexes[name] for name in self.absolute_blocks)

    def absolutes_at(self, point: PointRef) -> tuple[tuple[str, int], ...]:
        """Read absolute blocks naming a point, returning ``(block, value)``."""
        result = {}
        for name in self.absolute_blocks:
            if point not in self._indexes[name]:
                continue
            values = self.read_block(name)
            result[name] = int(values[point])
        return tuple(sorted(result.items()))

    def status_at(self, point: PointRef) -> tuple[str, str] | None:
        """Return ``(block, status)`` for a point.

        An explicit missing report wins over any other status.
        """
        found: tuple[str, str] | None = None
        for name in self.status_blocks:
            status = self._indexes[name].get(point)
            if status is None:
                continue
            if status == "explicitly_missing":
                return name, status
            if found is None:
                found = (name, status)
        return found

    def offset_value(self, block: str, start: PointRef, end: PointRef) -> int:
        """Read one offset block and return one signed offset.

        A row may be stored in either direction; the inverse direction is
        returned negated so reordered reply rows stay equivalent.
        """
        values = self.read_block(block)
        if (start, end) in values:
            return int(values[(start, end)])
        if (end, start) in values:
            return -int(values[(end, start)])
        raise KeyError(
            f"offset block {block!r} has no row {start!r} <-> {end!r}"
        )

    def offset_rows(self, block: str) -> dict[tuple[PointRef, PointRef], int]:
        """Read one offset block and return all rows."""
        return dict(self.read_block(block))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable reply description including numbers."""
        absolutes: dict[str, list[dict[str, Any]]] = {}
        for name in self.absolute_blocks:
            values = self.read_block(name)
            absolutes[name] = [
                {
                    "processor": point[0],
                    "batch": point[1],
                    "point": point[2],
                    "value_mm": int(values[point]),
                }
                for point in self._indexes[name]
            ]
        offsets: dict[str, list[dict[str, Any]]] = {}
        for name in self.offset_blocks:
            values = self.read_block(name)
            offsets[name] = [
                {
                    "start": _point_dict(start),
                    "end": _point_dict(end),
                    "offset_mm": int(values[(start, end)]),
                }
                for start, end in self._indexes[name]
            ]
        statuses: dict[str, list[dict[str, Any]]] = {}
        for name in self.status_blocks:
            statuses[name] = [
                {
                    "processor": point[0],
                    "batch": point[1],
                    "point": point[2],
                    "status": status,
                }
                for point, status in self._indexes[name].items()
            ]
        return {
            "absolutes": absolutes,
            "offsets": offsets,
            "statuses": statuses,
        }

    @classmethod
    def from_dict(cls, mapping: Mapping[str, Any]) -> ReplyCatalog:
        """Rebuild a reply catalog from :meth:`to_dict` output."""

        def identity(row: Mapping[str, Any]) -> PointRef:
            return (str(row["processor"]), str(row["batch"]), int(row["point"]))

        return cls(
            absolutes={
                block: {identity(row): int(row["value_mm"]) for row in rows}
                for block, rows in mapping.get("absolutes", {}).items()
            },
            offsets={
                block: [
                    (
                        identity(row["start"]),
                        identity(row["end"]),
                        int(row["offset_mm"]),
                    )
                    for row in rows
                ]
                for block, rows in mapping.get("offsets", {}).items()
            },
            statuses={
                block: {identity(row): str(row["status"]) for row in rows}
                for block, rows in mapping.get("statuses", {}).items()
            },
        )


@dataclass(frozen=True)
class _Edge:
    """An offset row with point identities and grid positions."""

    start: PointRef
    end: PointRef
    start_position: Position
    end_position: Position
    block: str
    order: int

    def oriented(self, forward: bool) -> tuple[PointRef, PointRef, Position, Position]:
        if forward:
            return self.start, self.end, self.start_position, self.end_position
        return self.end, self.start, self.end_position, self.start_position


class _HandoffState:
    """Handoff and reply description stored on a dataset's ``encoding``."""

    __slots__ = ("catalogs", "replies", "axes", "_by_point", "_by_position")

    def __init__(
        self,
        catalogs: Mapping[tuple[str, str], HandoffCatalog],
        replies: ReplyCatalog,
        axes: tuple[str, ...],
    ) -> None:
        self.catalogs = dict(catalogs)
        self.replies = replies
        self.axes = axes
        self._by_point: dict[PointRef, Position] = {}
        self._by_position: dict[Position, list[PointRef]] = defaultdict(list)
        for (processor, batch), catalog in self.catalogs.items():
            for row in catalog.rows:
                point = (processor, batch, row.point)
                self._by_point[point] = row.position
                self._by_position[row.position].append(point)

    def position_of(self, point: PointRef) -> Position | None:
        return self._by_point.get(point)

    def points_at(self, position: Position) -> tuple[PointRef, ...]:
        return tuple(self._by_position.get(position, ()))

    def point_at(self, processor: str, batch: str, point: int) -> PointRef:
        return processor, batch, point


@dataclass
class _BlockInfo:
    identifier: int
    edges: tuple[int, ...]
    vertices: frozenset[Position]


class _OffsetGraph:
    """Signed offset graph over original grid positions.

    Nodes are original grid positions.  Edges are offset reply rows and
    carry both point identities for provenance.  Numbers stay unread until
    a block is needed.
    """

    __slots__ = (
        "state",
        "positions",
        "edges",
        "adjacency",
        "blocks",
        "point_blocks",
        "component",
        "_block_values",
        "_block_potentials",
        "_block_local",
        "_tree_edges",
    )

    def __init__(self, state: _HandoffState) -> None:
        self.state = state
        self.edges: list[_Edge] = []
        self.positions: list[Position] = []
        seen_positions: set[Position] = set()
        order = 0
        for block in state.replies.offset_blocks:
            for start, end in state.replies._indexes[block]:
                start_position = state.position_of(start)
                end_position = state.position_of(end)
                if start_position is None or end_position is None:
                    raise ValueError(
                        f"offset endpoint {start!r} or {end!r} is not present "
                        "in any handoff table"
                    )
                if start_position == end_position:
                    continue
                self.edges.append(
                    _Edge(
                        start=start,
                        end=end,
                        start_position=start_position,
                        end_position=end_position,
                        block=block,
                        order=order,
                    )
                )
                order += 1
                for position in (start_position, end_position):
                    if position not in seen_positions:
                        seen_positions.add(position)
                        self.positions.append(position)
        self.adjacency: dict[Position, list[tuple[Position, int]]] = defaultdict(list)
        for edge in self.edges:
            self.adjacency[edge.start_position].append((edge.end_position, edge.order))
            self.adjacency[edge.end_position].append((edge.start_position, edge.order))
        self.blocks: list[_BlockInfo] = []
        self.point_blocks: dict[Position, list[int]] = defaultdict(list)
        self.component: dict[Position, int] = {}
        self._block_values: dict[int, dict[tuple[PointRef, PointRef], int]] = {}
        self._block_potentials: dict[int, tuple[dict[Position, int], bool]] = {}
        self._block_local: dict[int, dict[Position, list[tuple[Position, int]]]] = {}
        self._tree_edges: dict[int, set[int]] = {}
        self._build_components()
        self._build_blocks()

    # -- construction --------------------------------------------------
    def _build_components(self) -> None:
        identifier = 0
        for point in self.positions:
            if point in self.component:
                continue
            stack = [point]
            self.component[point] = identifier
            while stack:
                node = stack.pop()
                for neighbour, _ in self.adjacency[node]:
                    if neighbour not in self.component:
                        self.component[neighbour] = identifier
                        stack.append(neighbour)
            identifier += 1

    def _build_blocks(self) -> None:
        discovery: dict[Position, int] = {}
        low: dict[Position, int] = {}
        parent_edge: dict[Position, int] = {}
        edge_stack: list[int] = []
        counter = 0

        def visit(node: Position) -> None:
            nonlocal counter
            discovery[node] = low[node] = counter
            counter += 1
            for neighbour, edge_id in self.adjacency[node]:
                if edge_id == parent_edge.get(node, -1):
                    continue
                if neighbour not in discovery:
                    parent_edge[neighbour] = edge_id
                    edge_stack.append(edge_id)
                    visit(neighbour)
                    low[node] = min(low[node], low[neighbour])
                    if low[neighbour] >= discovery[node]:
                        component_edges = []
                        while edge_stack:
                            candidate = edge_stack.pop()
                            component_edges.append(candidate)
                            if candidate == edge_id:
                                break
                        self._add_block(component_edges)
                elif discovery[neighbour] < discovery[node]:
                    edge_stack.append(edge_id)
                    low[node] = min(low[node], discovery[neighbour])

        for node in self.positions:
            if node not in discovery:
                parent_edge[node] = -1
                visit(node)
        if edge_stack:
            self._add_block(edge_stack)

    def _add_block(self, edge_ids: list[int]) -> None:
        if not edge_ids:
            return
        identifier = len(self.blocks)
        vertices: set[Position] = set()
        for edge_id in edge_ids:
            edge = self.edges[edge_id]
            vertices.add(edge.start_position)
            vertices.add(edge.end_position)
        self.blocks.append(
            _BlockInfo(
                identifier=identifier,
                edges=tuple(sorted(edge_ids)),
                vertices=frozenset(vertices),
            )
        )
        for vertex in vertices:
            self.point_blocks[vertex].append(identifier)

    # -- values and potentials ----------------------------------------
    def _values_for_block(self, block_id: int) -> dict[tuple[PointRef, PointRef], int]:
        cached = self._block_values.get(block_id)
        if cached is not None:
            return cached
        block = self.blocks[block_id]
        values: dict[tuple[PointRef, PointRef], int] = {}
        for edge_id in block.edges:
            edge = self.edges[edge_id]
            value = self.state.replies.offset_value(edge.block, edge.start, edge.end)
            values[(edge.start, edge.end)] = value
        self._block_values[block_id] = values
        return values

    def _local_adjacency(
        self, block_id: int
    ) -> dict[Position, list[tuple[Position, int, _Edge, bool, int]]]:
        cached = self._block_local.get(block_id)
        if cached is not None:
            return cast(Any, cached)
        block = self.blocks[block_id]
        values = self._values_for_block(block_id)
        local: dict[Position, list[tuple[Position, int, _Edge, bool, int]]] = defaultdict(list)
        for edge_id in block.edges:
            edge = self.edges[edge_id]
            value = values[(edge.start, edge.end)]
            local[edge.start_position].append(
                (edge.end_position, edge_id, edge, True, value)
            )
            local[edge.end_position].append(
                (edge.start_position, edge_id, edge, False, -value)
            )
        self._block_local[block_id] = cast(Any, local)
        return local

    def _potential(self, block_id: int) -> tuple[dict[Position, int], bool]:
        cached = self._block_potentials.get(block_id)
        if cached is not None:
            return cached
        block = self.blocks[block_id]
        local = self._local_adjacency(block_id)
        root = min(block.vertices)
        potential = {root: 0}
        stack = [root]
        balanced = True
        while stack:
            node = stack.pop()
            for neighbour, _, _, _, offset in local[node]:
                expected = potential[node] + offset
                if neighbour in potential:
                    if potential[neighbour] != expected:
                        balanced = False
                else:
                    potential[neighbour] = expected
                    stack.append(neighbour)
        result = (potential, balanced)
        self._block_potentials[block_id] = result
        return result

    # -- routes --------------------------------------------------------
    def block_route(
        self, start: Position, end: Position
    ) -> tuple[str, Any]:
        """Blocks joined at articulation points, or a failure marker."""
        if start == end:
            return "ok", []
        if start not in self.point_blocks or end not in self.point_blocks:
            return "none", None
        adjacency: dict[tuple[str, Any], list[tuple[str, Any]]] = defaultdict(list)
        for block in self.blocks:
            for vertex in block.vertices:
                adjacency[("p", vertex)].append(("b", block.identifier))
                adjacency[("b", block.identifier)].append(("p", vertex))
        parent: dict[tuple[str, Any], tuple[str, Any] | None] = {("p", start): None}
        queue: deque[tuple[str, Any]] = deque([("p", start)])
        target: tuple[str, Any] | None = None
        while queue:
            node = queue.popleft()
            if node == ("p", end):
                target = node
                break
            for neighbour in adjacency[node]:
                if neighbour not in parent:
                    parent[neighbour] = node
                    queue.append(neighbour)
        if target is None:
            return "none", None
        chain: list[tuple[str, Any]] = []
        cursor: tuple[str, Any] | None = target
        while cursor is not None:
            chain.append(cursor)
            cursor = parent[cursor]
        chain.reverse()
        result: list[tuple[int, Position, Position]] = []
        for index, item in enumerate(chain):
            if item[0] != "b":
                continue
            block_id = cast(int, item[1])
            entry = start if index == 0 else cast(Position, chain[index - 1][1])
            exit_point = (
                cast(Position, chain[index + 1][1])
                if index + 1 < len(chain)
                else end
            )
            result.append((block_id, entry, exit_point))
        return "ok", result

    def delta(self, start: Position, end: Position) -> tuple[str, Any]:
        """Signed difference ``value(end) - value(start)``.

        Returns ``("ok", delta)``, ``("conflict", (block_id, entry, exit))``
        or ``("none", None)`` when the positions are not connected.
        """
        status, route = self.block_route(start, end)
        if status != "ok":
            return status, route
        total = 0
        for block_id, entry, exit_point in route:
            potential, balanced = self._potential(block_id)
            if not balanced:
                return "conflict", (block_id, entry, exit_point)
            total += potential[exit_point] - potential[entry]
        return "ok", total

    def _block_walk(
        self, block_id: int, start: Position, end: Position
    ) -> list[tuple[_Edge, bool, int]]:
        local = self._local_adjacency(block_id)
        if start == end:
            return []
        parent: dict[Position, tuple[Position, _Edge, bool, int]] = {}
        seen = {start}
        queue: deque[Position] = deque([start])
        while queue:
            node = queue.popleft()
            if node == end:
                break
            for neighbour, _, edge, forward, offset in sorted(
                local[node], key=lambda item: _edge_sort_key(item[2])
            ):
                if neighbour in seen:
                    continue
                seen.add(neighbour)
                parent[neighbour] = (node, edge, forward, offset)
                queue.append(neighbour)
        if end not in parent:
            raise KeyError(f"no route inside block {block_id}")
        path: list[tuple[_Edge, bool, int]] = []
        node = end
        while node != start:
            previous, edge, forward, offset = parent[node]
            path.append((edge, forward, offset))
            node = previous
        path.reverse()
        return path

    def steps(self, start: Position, end: Position) -> tuple[RouteStep, ...]:
        """One simple route as signed provenance steps."""
        status, route = self.block_route(start, end)
        if status != "ok":
            raise KeyError(f"no route from {start!r} to {end!r}")
        steps: list[RouteStep] = []
        for block_id, entry, exit_point in route:
            for edge, forward, offset in self._block_walk(block_id, entry, exit_point):
                from_point, to_point, from_position, to_position = edge.oriented(forward)
                steps.append(
                    RouteStep(
                        from_point=from_point,
                        to_point=to_point,
                        offset_mm=offset,
                        block=edge.block,
                        from_position=from_position,
                        to_position=to_position,
                    )
                )
        return tuple(steps)

    def block_route_options(
        self, block_id: int, start: Position, end: Position
    ) -> list[tuple[int, tuple[RouteStep, ...]]]:
        """Up to two distinct signed-route differences inside one block.

        One consistent option is returned for a balanced block.  An
        inconsistent block yields two, constructed from a nonzero
        fundamental cycle and disjoint connector paths, without expanding
        every simple route.
        """
        if start == end:
            return [(0, ())]
        potential, balanced = self._potential(block_id)
        if balanced:
            delta = potential[end] - potential[start]
            steps = tuple(self._block_walk_steps(block_id, start, end))
            return [(delta, steps)]
        alternatives = self._two_routes_via_cycle(block_id, start, end)
        if alternatives is None:
            delta = potential[end] - potential[start]
            steps = tuple(self._block_walk_steps(block_id, start, end))
            return [(delta, steps)]
        return alternatives

    def _two_routes_via_cycle(
        self, block_id: int, start: Position, end: Position
    ) -> list[tuple[int, tuple[RouteStep, ...]]] | None:
        """Two simple routes whose difference is a nonzero cycle total."""
        block = self.blocks[block_id]
        local = self._local_adjacency(block_id)
        potentials, _ = self._potential(block_id)

        # Find a nonzero fundamental cycle: a non-tree edge whose endpoints'
        # potential difference disagrees with the edge offset.
        tree_edges = self._spanning_tree_edges(block_id)
        cycle: list[tuple[_Edge, bool]] | None = None
        for edge_id in block.edges:
            if edge_id in tree_edges:
                continue
            edge = self.edges[edge_id]
            tree_path = self._tree_path(block_id, edge.end_position, edge.start_position)
            if tree_path is None:
                continue
            candidate = [(edge, True)] + tree_path
            total = sum(self._cycle_offset(item) for item in candidate)
            if total != 0:
                cycle = candidate
                break
        if cycle is None:
            return None

        cycle_vertices: list[Position] = []
        for edge, forward in cycle:
            from_point, to_point, from_position, to_position = edge.oriented(forward)
            if not cycle_vertices:
                cycle_vertices.append(from_position)
            cycle_vertices.append(to_position)
        if cycle_vertices[0] == cycle_vertices[-1]:
            cycle_vertices.pop()

        # Two connector vertices on the cycle, with disjoint connector paths
        # from start/end, keep both spliced routes simple.
        options = self._connector_pair(block_id, cycle_vertices, start, end)
        if options is None:
            return None
        attach_start, attach_end, path_start, path_end = options

        index_start = cycle_vertices.index(attach_start)
        index_end = cycle_vertices.index(attach_end)
        if index_start == index_end:
            return None
        cycle_steps = [
            RouteStep(
                from_point=edge.oriented(forward)[0],
                to_point=edge.oriented(forward)[1],
                offset_mm=offset,
                block=edge.block,
                from_position=edge.oriented(forward)[2],
                to_position=edge.oriented(forward)[3],
            )
            for edge, forward, offset in self._cycle_walks(cycle)
        ]
        count = len(cycle_vertices)
        first_arc: list[RouteStep] = []
        index = index_start
        while index != index_end:
            first_arc.append(cycle_steps[index])
            index = (index + 1) % count
        second_arc: list[RouteStep] = []
        index = index_start
        while index != index_end:
            index = (index - 1) % count
            second_arc.append(_reverse_step(cycle_steps[index]))
        connector_start = tuple(path_start)
        connector_end = tuple(_reverse_step(step) for step in reversed(path_end))
        first_steps = connector_start + tuple(first_arc) + connector_end
        second_steps = connector_start + tuple(second_arc) + connector_end
        first_value = sum(step.offset_mm for step in first_steps)
        second_value = sum(step.offset_mm for step in second_steps)
        if first_value == second_value:
            return None
        return [
            (first_value, first_steps),
            (second_value, second_steps),
        ]

    def _cycle_offset(self, item: tuple[_Edge, bool]) -> int:
        edge, forward = item
        value = self.state.replies.offset_value(edge.block, edge.start, edge.end)
        return value if forward else -value

    def _cycle_walks(
        self, cycle: list[tuple[_Edge, bool]]
    ) -> list[tuple[_Edge, bool, int]]:
        return [
            (edge, forward, self._cycle_offset((edge, forward)))
            for edge, forward in cycle
        ]

    def _spanning_tree_edges(self, block_id: int) -> set[int]:
        cached = self._tree_edges.get(block_id)
        if cached is not None:
            return cached
        block = self.blocks[block_id]
        edges: set[int] = set()
        seen: set[Position] = set()
        start = min(block.vertices)
        stack = [start]
        seen.add(start)
        while stack:
            node = stack.pop()
            for edge_id in block.edges:
                edge = self.edges[edge_id]
                if edge.start_position == node and edge.end_position not in seen:
                    edges.add(edge_id)
                    seen.add(edge.end_position)
                    stack.append(edge.end_position)
                elif edge.end_position == node and edge.start_position not in seen:
                    edges.add(edge_id)
                    seen.add(edge.start_position)
                    stack.append(edge.start_position)
        self._tree_edges[block_id] = edges
        return edges

    def _tree_path(
        self, block_id: int, start: Position, end: Position
    ) -> list[tuple[_Edge, bool]] | None:
        tree_edges = self._spanning_tree_edges(block_id)
        local: dict[Position, list[tuple[Position, _Edge, bool]]] = defaultdict(list)
        for edge_id in tree_edges:
            edge = self.edges[edge_id]
            local[edge.start_position].append((edge.end_position, edge, True))
            local[edge.end_position].append((edge.start_position, edge, False))
        parent: dict[Position, tuple[Position, _Edge, bool]] = {}
        seen = {start}
        queue: deque[Position] = deque([start])
        while queue:
            node = queue.popleft()
            if node == end:
                break
            for neighbour, edge, forward in local[node]:
                if neighbour in seen:
                    continue
                seen.add(neighbour)
                parent[neighbour] = (node, edge, forward)
                queue.append(neighbour)
        if end not in parent:
            return None
        chain: list[tuple[Position, Position, _Edge, bool]] = []
        node = end
        while node != start:
            previous, edge, forward = parent[node]
            chain.append((previous, node, edge, forward))
            node = previous
        chain.reverse()
        return [(edge, forward) for _, _, edge, forward in chain]

    def _connector_pair(
        self,
        block_id: int,
        cycle_vertices: Sequence[Position],
        start: Position,
        end: Position,
    ) -> tuple[
        Position,
        Position,
        list[RouteStep],
        list[RouteStep],
    ] | None:
        """Two cycle attachment vertices with vertex-disjoint connectors.

        The connectors are only allowed to touch the cycle at their own
        attachment vertex, so both spliced routes stay simple.
        """
        cycle_set = set(cycle_vertices)
        for attach_start in cycle_vertices if start not in cycle_set else (start,):
            if start == attach_start:
                first: list[RouteStep] | None = []
            else:
                first = self._path_avoiding(
                    block_id,
                    start,
                    attach_start,
                    banned=frozenset(cycle_set - {attach_start}) | {end},
                )
            if first is None:
                continue
            first_vertices = {start}
            first_vertices.update(step.from_position for step in first)
            first_vertices.update(step.to_position for step in first)
            for attach_end in cycle_vertices if end not in cycle_set else (end,):
                if attach_end == attach_start:
                    continue
                if end == attach_end:
                    second: list[RouteStep] | None = []
                else:
                    second = self._path_avoiding(
                        block_id,
                        end,
                        attach_end,
                        banned=(
                            frozenset(cycle_set - {attach_end})
                            | frozenset(first_vertices)
                        ),
                    )
                if second is None:
                    continue
                return attach_start, attach_end, first, second
        return None

    def _path_avoiding(
        self,
        block_id: int,
        source: Position,
        target: Position,
        *,
        banned: frozenset[Position],
    ) -> list[RouteStep] | None:
        """Shortest simple path from source to target avoiding banned vertices."""
        if source in banned or target in banned:
            return None
        local = self._local_adjacency(block_id)
        parent: dict[Position, tuple[Position, _Edge, bool, int]] = {}
        seen = {source} | set(banned)
        queue: deque[Position] = deque([source])
        while queue:
            node = queue.popleft()
            if node == target:
                break
            for neighbour, _, edge, forward, offset in sorted(
                local[node], key=lambda item: _edge_sort_key(item[2])
            ):
                if neighbour in seen:
                    continue
                seen.add(neighbour)
                parent[neighbour] = (node, edge, forward, offset)
                queue.append(neighbour)
        if target not in parent:
            return None
        return self._path_to(parent, source, target, block_id)

    def _path_to(
        self,
        parent: dict[Position, tuple[Position, _Edge, bool, int]],
        source: Position,
        target: Position,
        block_id: int,
    ) -> list[RouteStep]:
        chain: list[tuple[Position, Position, _Edge, bool, int]] = []
        node = target
        while node != source:
            previous, edge, forward, offset = parent[node]
            chain.append((previous, node, edge, forward, offset))
            node = previous
        chain.reverse()
        steps: list[RouteStep] = []
        for previous, node, edge, forward, offset in chain:
            from_point, to_point, from_position, to_position = edge.oriented(forward)
            steps.append(
                RouteStep(
                    from_point=from_point,
                    to_point=to_point,
                    offset_mm=offset,
                    block=edge.block,
                    from_position=from_position,
                    to_position=to_position,
                )
            )
        return steps

    def _block_walk_steps(
        self, block_id: int, start: Position, end: Position
    ) -> list[RouteStep]:
        """One simple walk inside a block as provenance steps."""
        return [
            RouteStep(
                from_point=edge.oriented(forward)[0],
                to_point=edge.oriented(forward)[1],
                offset_mm=offset,
                block=edge.block,
                from_position=edge.oriented(forward)[2],
                to_position=edge.oriented(forward)[3],
            )
            for edge, forward, offset in self._block_walk(block_id, start, end)
        ]

    def degree(self, position: Position) -> int:
        """Number of incident offset rows."""
        return len(self.adjacency.get(position, ()))

    def edge_objects_at(self, position: Position) -> tuple[_Edge, ...]:
        """Offset rows incident to a position."""
        return tuple(
            self.edges[edge_id] for _, edge_id in self.adjacency.get(position, ())
        )

    def component_positions(self, position: Position) -> set[Position]:
        identifier = self.component.get(position)
        if identifier is None:
            return {position}
        return {
            candidate
            for candidate, value in self.component.items()
            if value == identifier
        }


def _ordered_conflict(
    first: RouteBranch, second: RouteBranch
) -> tuple[RouteBranch, RouteBranch]:
    """Order two conflicting routes by value, then by route shape."""
    def key(route: RouteBranch) -> tuple[Any, ...]:
        return (
            route.value_mm,
            route.anchor,
            tuple(
                (step.from_point, step.to_point, step.offset_mm, step.block)
                for step in route.steps
            ),
        )

    return (first, second) if key(first) <= key(second) else (second, first)


def _reverse_step(step: RouteStep) -> RouteStep:
    return RouteStep(
        from_point=step.to_point,
        to_point=step.from_point,
        offset_mm=-step.offset_mm,
        block=step.block,
        from_position=step.to_position,
        to_position=step.from_position,
    )


def _branch(
    anchor: PointRef,
    absolute_mm: int,
    value_mm: int,
    steps: Sequence[RouteStep],
    *,
    absolute_block: str | None = None,
) -> RouteBranch:
    blocks: list[str] = []
    for step in steps:
        if step.block not in blocks:
            blocks.append(step.block)
    if absolute_block is not None and absolute_block not in blocks:
        blocks.append(absolute_block)
    return RouteBranch(
        value_mm=value_mm,
        anchor=anchor,
        absolute_mm=absolute_mm,
        steps=tuple(steps),
        blocks=tuple(blocks),
    )


class _ResolutionCache:
    """Shared resolution cache and offset graph for one dataset view."""

    __slots__ = ("state", "graph", "cells")

    def __init__(self, state: _HandoffState) -> None:
        self.state = state
        self.graph = _OffsetGraph(state)
        self.cells: dict[Position, CellResolution] = {}

    def resolve(self, position: Position) -> CellResolution:
        cached = self.cells.get(position)
        if cached is None:
            cached = _resolve_position(self.state, self.graph, position)
            self.cells[position] = cached
        return cached


def _anchors(
    state: _HandoffState, graph: _OffsetGraph, position: Position
) -> tuple[dict[str, Any], ...]:
    """Absolute readings in the same component, by original position."""
    component = graph.component.get(position)
    if component is None:
        positions = {position}
    else:
        positions = graph.component_positions(position)
    result: list[dict[str, Any]] = []
    for anchor_position in sorted(positions):
        for point in state.points_at(anchor_position):
            if not state.replies.has_absolute(point):
                continue
            for block, value in state.replies.absolutes_at(point):
                result.append(
                    {
                        "position": anchor_position,
                        "point": point,
                        "block": block,
                        "absolute_mm": value,
                    }
                )
    result.sort(key=lambda item: (item["position"], item["point"], item["block"]))
    return tuple(result)


def _resolve_position(
    state: _HandoffState, graph: _OffsetGraph, position: Position
) -> CellResolution:
    """Resolve a cell from every handoff point that refers to it.

    A point's own reply status only affects that point.  If one processor
    has no usable reply while another processor delivered a value for the
    same cell, the cell still has that value.
    """
    points = state.points_at(position)
    if not points:
        return CellResolution(
            status="never_handed_off",
            position=position,
            reason="the cell was never handed off",
        )

    resolved = [_resolve_point(state, graph, point, position) for point in points]
    delivered = [item for item in resolved if item.status == "value"]
    if delivered:
        first = delivered[0]
        if all(item.value_mm == first.value_mm for item in delivered):
            return CellResolution(
                status="value",
                position=position,
                points=points,
                value_mm=first.value_mm,
                routes=tuple(
                    route for item in delivered for route in item.routes
                ),
                blocks=_dedupe_blocks(item.blocks for item in delivered),
            )
        second = next(
            item for item in delivered[1:] if item.value_mm != first.value_mm
        )
        first_route = first.routes[0]
        second_route = second.routes[0]
        first_route, second_route = _ordered_conflict(first_route, second_route)
        return CellResolution(
            status="conflict",
            position=position,
            points=points,
            routes=(first_route, second_route),
            conflict=ConflictDetail(first=first_route, second=second_route),
            reason="multiple handoff replies disagree for the same cell",
            blocks=_dedupe_blocks((first.blocks, second.blocks)),
        )

    for status in ("conflict", "pending_baseline", "explicitly_missing", "no_reply"):
        for item in resolved:
            if item.status == status:
                return item
    return resolved[0]


def _resolve_point(
    state: _HandoffState,
    graph: _OffsetGraph,
    point: PointRef,
    position: Position,
) -> CellResolution:
    """Resolve one handoff point without letting other points mask it."""
    status = state.replies.status_at(point)
    if status is not None and status[1] == "explicitly_missing":
        return CellResolution(
            status="explicitly_missing",
            position=position,
            points=(point,),
            reason="the processor explicitly reported the point as missing",
            blocks=(status[0],),
        )

    anchors = _anchors(state, graph, position)
    if not anchors:
        if graph.degree(position) > 0:
            return CellResolution(
                status="pending_baseline",
                position=position,
                points=(point,),
                reason=(
                    "only offsets are available and no simple route reaches "
                    "an absolute reading"
                ),
                blocks=_blocks_touching(graph, position),
            )
        return CellResolution(
            status="no_reply",
            position=position,
            points=(point,),
            reason="the point was handed off but no reply arrived",
        )

    values = _implied_values(state, graph, position, anchors)
    if not values:
        return CellResolution(
            status="conflict",
            position=position,
            points=(point,),
            reason="offset routes to absolute readings are inconsistent",
            blocks=_blocks_touching(graph, position),
        )
    if len(values) > 1:
        ordered = sorted(values)
        first_value, second_value = ordered[0], ordered[1]
        first = _branch_for_value(state, graph, position, first_value)
        second = _branch_for_value(state, graph, position, second_value)
        first, second = _ordered_conflict(first, second)
        return CellResolution(
            status="conflict",
            position=position,
            points=(point,),
            routes=(first, second),
            conflict=ConflictDetail(first=first, second=second),
            reason="simple routes to absolute readings imply different values",
            blocks=_dedupe_blocks((first.blocks, second.blocks)),
        )

    value = next(iter(values))
    route = _branch_for_value(state, graph, position, value)
    return CellResolution(
        status="value",
        position=position,
        points=(point,),
        value_mm=value,
        routes=(route,),
        blocks=route.blocks,
    )


def _route_options(
    graph: _OffsetGraph, start: Position, end: Position
) -> list[tuple[int, tuple[RouteStep, ...]]] | None:
    """Up to two distinct signed route differences, with concrete routes.

    If every simple route inside each biconnected block has the same signed
    difference, one option is returned.  An inconsistent block contributes
    its two smallest distinct route differences.  Keeping only the two
    smallest sums at each step still yields the two smallest overall sums,
    so a conflict is never missed while crossing routes are not expanded.
    """
    route = graph.block_route(start, end)
    status, segments = route
    if status != "ok":
        return None
    options: list[tuple[int, tuple[RouteStep, ...]]] = [(0, ())]
    for block_id, entry, exit_point in segments:
        block_options = graph.block_route_options(block_id, entry, exit_point)
        combined: dict[int, tuple[RouteStep, ...]] = {}
        for prefix_value, prefix_steps in options:
            for block_value, block_steps in block_options:
                total = prefix_value + block_value
                combined.setdefault(total, prefix_steps + block_steps)
        options = [
            (value, combined[value]) for value in sorted(combined)
        ][:2]
    return options


def _implied_values(
    state: _HandoffState,
    graph: _OffsetGraph,
    position: Position,
    anchors: Sequence[Mapping[str, Any]],
) -> set[int]:
    """Every value implied by a simple route to an absolute reading."""
    values: set[int] = set()
    for anchor in anchors:
        anchor_position = cast(Position, anchor["position"])
        absolute = cast(int, anchor["absolute_mm"])
        options = _route_options(graph, position, anchor_position)
        if options is None:
            continue
        for delta, _ in options:
            values.add(absolute - delta)
    return values


def _branch_for_value(
    state: _HandoffState,
    graph: _OffsetGraph,
    position: Position,
    value: int,
) -> RouteBranch:
    """A concrete route that implies ``value``, with its absolute block."""
    anchors = _anchors(state, graph, position)
    for anchor in anchors:
        anchor_position = cast(Position, anchor["position"])
        absolute = cast(int, anchor["absolute_mm"])
        block = cast(str, anchor["block"])
        options = _route_options(graph, position, anchor_position)
        if options is None:
            continue
        for delta, steps in options:
            if absolute - delta == value:
                return _branch(
                    cast(PointRef, anchor["point"]),
                    absolute,
                    value,
                    steps,
                    absolute_block=block,
                )
    first = anchors[0]
    anchor_position = cast(Position, first["position"])
    absolute = cast(int, first["absolute_mm"])
    options = _route_options(graph, position, anchor_position) or []
    delta, steps = options[0] if options else (0, ())
    return _branch(
        cast(PointRef, first["point"]),
        absolute,
        absolute - delta,
        steps,
        absolute_block=cast(str, first["block"]),
    )


def _blocks_touching(graph: _OffsetGraph, position: Position) -> tuple[str, ...]:
    """Blocks holding offsets incident to a position."""
    return tuple(sorted({edge.block for edge in graph.edge_objects_at(position)}))


def _dedupe_blocks(groups: Iterable[Iterable[str]]) -> tuple[str, ...]:
    seen: list[str] = []
    for group in groups:
        for block in group:
            if block not in seen:
                seen.append(block)
    return tuple(seen)


class _CellValues(ExplicitlyIndexedNDArrayMixin):
    """Lazy per-cell values, indexed exactly like the current grid view."""

    __slots__ = ("cache", "positions", "_shape")

    def __init__(
        self, cache: _ResolutionCache, positions: np.ndarray, shape: tuple[int, ...]
    ) -> None:
        self.cache = cache
        self.positions = positions
        self._shape = shape

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(np.float64)

    @property
    def shape(self) -> tuple[int, ...]:
        return self._shape

    def _array(self) -> np.ndarray:
        flat_positions = self.positions.reshape(-1, self.positions.shape[-1])
        values = np.empty(len(flat_positions), dtype=np.float64)
        for index, raw in enumerate(flat_positions):
            resolution = self.cache.resolve(tuple(int(value) for value in raw))
            values[index] = (
                np.nan if resolution.value_mm is None else resolution.value_mm
            )
        return values.reshape(self._shape)

    def get_duck_array(self) -> np.ndarray:
        return self._array()

    def __array__(self, dtype=None, /, *, copy=None) -> np.ndarray:
        return np.asarray(self._array(), dtype=dtype, copy=copy)

    def _indexed(self, indexer: ExplicitIndexer) -> _CellValues:
        positions = _apply_indexer(self.positions, indexer)
        return _CellValues(self.cache, positions, positions.shape[:-1])

    def __getitem__(self, indexer: ExplicitIndexer) -> _CellValues:
        self._check_and_raise_if_non_basic_indexer(indexer)
        return self._indexed(indexer)

    def _oindex_get(self, indexer: OuterIndexer) -> _CellValues:
        return self._indexed(indexer)

    def _vindex_get(self, indexer: VectorizedIndexer) -> _CellValues:
        return self._indexed(indexer)

    def transpose(self, order: Sequence[int]) -> _CellValues:
        positions = np.transpose(self.positions, tuple(order) + (self.positions.ndim - 1,))
        return _CellValues(self.cache, positions, positions.shape[:-1])


def _apply_indexer(positions: np.ndarray, indexer: ExplicitIndexer) -> np.ndarray:
    keys = indexer.tuple
    if isinstance(indexer, VectorizedIndexer):
        vector = tuple(np.asarray(key) for key in keys)
        return positions[vector]
    indexers: list[Any] = []
    for key in keys:
        if isinstance(key, slice):
            indexers.append(key)
        elif isinstance(key, (int, np.integer)):
            indexers.append(int(key))
        else:
            indexers.append(np.asarray(key))
    return positions[tuple(indexers)]


class PointHandoffAccessor:
    """Opt-in handoff/reply accessor for a monitoring grid.

    The accessor is only installed by :func:`register_point_handoff_accessor`;
    normal xarray behaviour is unchanged otherwise.
    """

    def __init__(self, xarray_obj: xr.Dataset) -> None:
        self._obj = xarray_obj

    # -- metadata ------------------------------------------------------
    @property
    def _state(self) -> _HandoffState:
        state = self._obj.encoding.get(_HANDOFF_ATTR)
        if not isinstance(state, _HandoffState):
            raise ValueError(
                "this dataset has no point handoff description; attach one "
                "with xarray.point_handoff.attach(...)"
            )
        return state

    @property
    def axes(self) -> tuple[str, ...]:
        """Original axis names, in original order."""
        return self._state.axes

    @property
    def handoffs(self) -> tuple[HandoffCatalog, ...]:
        """Handoff catalogs in batch order."""
        return tuple(self._state.catalogs.values())

    @property
    def replies(self) -> ReplyCatalog:
        """Reply catalog."""
        return self._state.replies

    @property
    def catalogs(self) -> tuple[HandoffCatalog, ...]:
        """Alias of :attr:`handoffs`."""
        return self.handoffs

    def handoff_table(self, processor: str, batch: str | None = None) -> pd.DataFrame:
        """Return the one-dimensional point table for one handoff batch."""
        state = self._state
        for (candidate_processor, candidate_batch), catalog in state.catalogs.items():
            if candidate_processor != processor:
                continue
            if batch is not None and candidate_batch != batch:
                continue
            return catalog.table()
        raise KeyError(f"no handoff batch for processor {processor!r}")

    def catalog(self) -> dict[str, Any]:
        """Summarise handoffs and replies without reading number blocks."""
        state = self._state
        return {
            "axes": list(state.axes),
            "handoffs": {
                processor: catalog.to_dict()
                for (processor, _), catalog in state.catalogs.items()
            },
            "replies": state.replies.catalog(),
        }

    def save_handoff(self, processor: str, path: str | Path, batch: str | None = None) -> None:
        """Write one handoff description, without any reply numbers."""
        state = self._state
        for (candidate_processor, candidate_batch), catalog in state.catalogs.items():
            if candidate_processor != processor:
                continue
            if batch is not None and candidate_batch != batch:
                continue
            save_handoff(catalog, path)
            return
        raise KeyError(f"no handoff batch for processor {processor!r}")

    def save_reply_catalog(self, path: str | Path) -> None:
        """Write the reply directory, without reading any number block."""
        save_replies(self._state.replies, path, with_numbers=False)

    # -- values --------------------------------------------------------
    def _cache(self) -> _ResolutionCache:
        cache = self._obj._cache.get("_point_handoff_resolution")
        if cache is None:
            state = self._state
            cache = _ResolutionCache(state)
            self._obj._cache["_point_handoff_resolution"] = cache
        return cache

    def _values(self, dims: tuple[str, ...]) -> _CellValues:
        state = self._state
        positions = _view_positions(self._obj, state.axes, dims)
        shape = tuple(self._obj.sizes[dim] for dim in dims)
        return _CellValues(self._cache(), positions, shape)

    def _dataarray(self, dims: tuple[str, ...], name: str) -> xr.DataArray:
        state = self._state
        values = self._values(dims)
        coords = {
            dim: self._obj.coords[dim]
            for dim in dims
            if dim in self._obj.coords
        }
        result = xr.DataArray(Variable(dims, values), coords=coords, name=name)
        result.encoding[_HANDOFF_ATTR] = state
        return result

    def values(self) -> xr.DataArray:
        """Lazy per-cell values on the current view's own dimensions.

        The array follows the current view's axis subset and order.  Reading
        it resolves only the selected cells and then only the number blocks
        those cells need.
        """
        state = self._state
        return self._dataarray(_view_dims(self._obj, state), "point_handoff_value")

    @property
    def value(self) -> xr.DataArray:
        """Alias of :meth:`values`."""
        return self.values()

    def status(self) -> xr.DataArray:
        """Per-cell status codes on the current view's dimensions."""
        state = self._state
        dims = _view_dims(self._obj, state)
        values = self._values(dims)
        codes = _StatusCodes(values)
        coords = {
            dim: self._obj.coords[dim]
            for dim in dims
            if dim in self._obj.coords
        }
        result = xr.DataArray(Variable(dims, codes), coords=coords, name="point_handoff_status")
        result.encoding[_HANDOFF_ATTR] = state
        return result

    def resolve(self, **indexers: int) -> CellResolution:
        """Resolve one cell of the current view by positional label.

        Pass one integer position per current dimension, for example
        ``view.point_handoff.resolve(leg=1, lon=2)``.  Dimensions that are
        absent from the view are fixed by the view itself.
        """
        state = self._state
        dims = _view_dims(self._obj, state)
        extra = set(indexers) - set(dims)
        missing = set(dims) - set(indexers)
        if extra or missing:
            raise KeyError(
                f"resolve() expects positions for {dims!r}; got {indexers!r}"
            )
        positions = _view_positions(self._obj, state.axes, dims)
        selector = tuple(indexers[dim] for dim in dims)
        raw = positions[selector]
        return self._cache().resolve(tuple(int(value) for value in raw))

    def report(self) -> pd.DataFrame:
        """Resolve every cell of the current view into a tabular report."""
        state = self._state
        dims = _view_dims(self._obj, state)
        positions = _view_positions(self._obj, state.axes, dims)
        rows = []
        for raw in positions.reshape(-1, len(state.axes)):
            resolution = self._cache().resolve(tuple(int(value) for value in raw))
            rows.append(resolution.as_dict())
        return pd.DataFrame(rows)


class _StatusCodes(ExplicitlyIndexedNDArrayMixin):
    __slots__ = ("values",)

    _CODES = {
        "value": 0,
        "conflict": 1,
        "pending_baseline": 2,
        "explicitly_missing": 3,
        "no_reply": 4,
        "never_handed_off": 5,
    }

    def __init__(self, values: _CellValues) -> None:
        self.values = values

    @property
    def dtype(self) -> np.dtype:
        return np.dtype(np.int8)

    @property
    def shape(self) -> tuple[int, ...]:
        return self.values.shape

    def _array(self) -> np.ndarray:
        raw = self.values.get_duck_array()
        return raw

    def get_duck_array(self) -> np.ndarray:
        positions = self.values.positions.reshape(-1, self.values.positions.shape[-1])
        result = np.empty(len(positions), dtype=np.int8)
        for index, raw in enumerate(positions):
            resolution = self.values.cache.resolve(
                tuple(int(value) for value in raw)
            )
            result[index] = self._CODES[resolution.status]
        return result.reshape(self.values.shape)

    def __array__(self, dtype=None, /, *, copy=None) -> np.ndarray:
        return np.asarray(self.get_duck_array(), dtype=dtype, copy=copy)

    def _indexed(self, indexer: ExplicitIndexer) -> _StatusCodes:
        return _StatusCodes(self.values[indexer])

    def __getitem__(self, indexer: ExplicitIndexer) -> _StatusCodes:
        self._check_and_raise_if_non_basic_indexer(indexer)
        return self._indexed(indexer)

    def _oindex_get(self, indexer: OuterIndexer) -> _StatusCodes:
        return self._indexed(indexer)

    def _vindex_get(self, indexer: VectorizedIndexer) -> _StatusCodes:
        return self._indexed(indexer)

    def transpose(self, order: Sequence[int]) -> _StatusCodes:
        return _StatusCodes(self.values.transpose(order))


def _view_dims(obj: xr.Dataset, state: _HandoffState) -> tuple[str, ...]:
    """Dimensions of a view that correspond to original axes.

    A view may have dropped an axis entirely (for example ``sel(zone=...)``
    turns the original ``track`` axis into a scalar), in which case that
    axis no longer contributes a dimension.
    """
    result = []
    for axis in state.axes:
        coordinate = obj.coords.get(f"{_POSITION_PREFIX}{axis}")
        if coordinate is not None and coordinate.ndim == 0:
            continue
        if coordinate is not None and coordinate.ndim == 1 and coordinate.dims[0] in obj.dims:
            result.append(coordinate.dims[0])
            continue
        if axis in obj.dims:
            result.append(axis)
    return tuple(result)


def _view_positions(
    obj: xr.Dataset, axes: tuple[str, ...], dims: tuple[str, ...]
) -> np.ndarray:
    """Original grid positions for every element of a view.

    Each original axis is located in the view through its position
    coordinate (``__point_handoff_position__<axis>``).  xarray carries that
    coordinate through label and positional selection, including a
    MultiIndex level being promoted to its own dimension, so a view whose
    ``track`` dimension became ``leg`` still maps to the original track
    positions.
    """
    shape = tuple(obj.sizes[dim] for dim in dims)
    positions = np.zeros(shape + (len(axes),), dtype=np.int64)
    for index, axis in enumerate(axes):
        coordinate = obj.coords.get(f"{_POSITION_PREFIX}{axis}")
        if coordinate is not None and coordinate.ndim == 0:
            positions[..., index] = int(coordinate.values)
            continue
        if coordinate is not None and coordinate.ndim == 1 and coordinate.dims[0] in dims:
            axis_dim = coordinate.dims[0]
            values = np.asarray(coordinate.values, dtype=np.int64)
        elif axis in obj.dims:
            axis_dim = axis
            values = np.arange(obj.sizes[axis], dtype=np.int64)
        else:
            positions[..., index] = 0
            continue
        slot = dims.index(axis_dim)
        reshape = [1] * len(dims)
        reshape[slot] = values.size
        positions[..., index] = values.reshape(reshape)
    return positions


def attach(
    dataset: xr.Dataset,
    handoffs: Sequence[HandoffCatalog] | Mapping[str, HandoffCatalog] | None = None,
    replies: ReplyCatalog | None = None,
    *,
    axes: Sequence[str] | None = None,
) -> xr.Dataset:
    """Return a copy of ``dataset`` carrying a point handoff description.

    Parameters
    ----------
    dataset : Dataset
        The archive grid.  It is not modified.
    handoffs : sequence or mapping of HandoffCatalog, optional
        Handoff batches.  Point rows refer to original grid positions.
    replies : ReplyCatalog, optional
        Reply catalog.
    axes : sequence of str, optional
        Original axis names in order.  Defaults to all dimensions of the
        archive grid.
    """
    if not isinstance(dataset, xr.Dataset):
        raise TypeError("attach() requires an xarray.Dataset")
    if axes is None:
        axes = tuple(str(dim) for dim in dataset.dims)
    else:
        axes = tuple(str(axis) for axis in axes)
    for axis in axes:
        if axis not in dataset.dims:
            raise KeyError(f"axis {axis!r} is not a dimension of the dataset")

    if handoffs is None:
        items: Sequence[HandoffCatalog] = ()
    elif isinstance(handoffs, Mapping):
        items = tuple(handoffs.values())
    else:
        items = tuple(handoffs)
    catalogs: dict[tuple[str, str], HandoffCatalog] = {}
    for catalog in items:
        key = (catalog.processor, catalog.batch)
        if key in catalogs:
            raise ValueError(f"duplicate handoff batch {key!r}")
        if tuple(catalog.axes) != axes:
            raise ValueError(
                f"handoff batch {key!r} has axes {catalog.axes!r}, expected {axes!r}"
            )
        for row in catalog.rows:
            if len(row.position) != len(axes):
                raise ValueError(
                    f"point {row.point!r} has position {row.position!r}, "
                    f"expected {len(axes)} axes"
                )
            for axis, index in zip(axes, row.position, strict=True):
                if not 0 <= index < dataset.sizes[axis]:
                    raise IndexError(
                        f"point {row.point!r} position {row.position!r} is out "
                        f"of bounds for axis {axis!r}"
                    )
        catalogs[key] = catalog

    reply_catalog = replies if replies is not None else ReplyCatalog()
    result = dataset.copy()
    position_coords = {
        f"{_POSITION_PREFIX}{axis}": (
            axis,
            np.arange(dataset.sizes[axis], dtype=np.int64),
        )
        for axis in axes
    }
    if position_coords:
        result = result.assign_coords(position_coords)
    result.encoding[_HANDOFF_ATTR] = _HandoffState(catalogs, reply_catalog, axes)
    return result


def build_handoff(
    dataset: xr.Dataset,
    processor: str,
    batch: str,
    positions: Iterable[Position],
    *,
    axes: Sequence[str] | None = None,
    notes: Sequence[str | None] | None = None,
) -> HandoffCatalog:
    """Build a one-dimensional handoff table from original grid positions.

    Point numbers are assigned in the given order.
    """
    if axes is None:
        axes = tuple(str(dim) for dim in dataset.dims)
    else:
        axes = tuple(str(axis) for axis in axes)
    axis_values = {
        axis: tuple(_jsonable(value) for value in dataset.coords[axis].values)
        if axis in dataset.coords
        else tuple(range(dataset.sizes[axis]))
        for axis in axes
    }
    rows: list[HandoffRow] = []
    for index, raw in enumerate(positions, start=1):
        position = tuple(int(value) for value in raw)
        track = None
        lon = None
        if "track" in dataset.coords and position:
            track = _jsonable(dataset.coords["track"].values[position[0]])
        if "lon" in dataset.coords:
            lon = _jsonable(dataset.coords["lon"].values[position[-1]])
        note = None if notes is None else notes[index - 1]
        rows.append(
            HandoffRow(
                point=index,
                position=position,
                track=track,
                lon=lon,
                note=note,
            )
        )
    return HandoffCatalog(
        processor=processor,
        batch=batch,
        rows=tuple(rows),
        axes=axes,
        axis_values=axis_values,
    )


def save_handoff(catalog: HandoffCatalog, path: str | Path) -> None:
    """Write one handoff description (no reply numbers) as JSON."""
    Path(path).write_text(
        json.dumps(catalog.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def load_handoff(path: str | Path) -> HandoffCatalog:
    """Read one handoff description written by :func:`save_handoff`."""
    return HandoffCatalog.from_dict(json.loads(Path(path).read_text(encoding="utf-8")))


def save_catalog(reply_catalog: ReplyCatalog, path: str | Path) -> None:
    """Write a reply directory (no numbers) as JSON."""
    save_replies(reply_catalog, path, with_numbers=False)


def _block_payload(block: ReplyBlock, values: Any) -> Any:
    if block.kind == "offset":
        return [
            {
                "start": _point_dict(start),
                "end": _point_dict(end),
                "offset_mm": int(value),
            }
            for (start, end), value in values.items()
        ]
    if block.kind == "absolute":
        return [
            {**_point_dict(point), "value_mm": int(value)}
            for point, value in values.items()
        ]
    return [
        {**_point_dict(point), "status": status}
        for point, status in values.items()
    ]


def save_replies(
    reply_catalog: ReplyCatalog,
    path: str | Path,
    *,
    with_numbers: bool = True,
) -> None:
    """Write a reply catalog and, optionally, its number blocks.

    The catalog index always contains only metadata: which points have an
    absolute reading, which are explicitly missing and both endpoints of
    every offset.  With ``with_numbers=True`` (the default) every block's
    numbers go to its own file under ``<path>/blocks/``, so they can be
    read independently and in any order.
    """
    root = Path(path)
    root.mkdir(parents=True, exist_ok=True)
    index: dict[str, Any] = {
        "blocks": [
            {"name": block.name, "kind": block.kind}
            for block in reply_catalog.blocks
        ],
        "absolutes": {
            name: [_point_dict(point) for point in reply_catalog._indexes[name]]
            for name in reply_catalog.absolute_blocks
        },
        "explicitly_missing": [
            _point_dict(point)
            for point in reply_catalog.explicitly_missing_points()
        ],
        "statuses": {
            name: [
                {**_point_dict(point), "status": status}
                for point, status in reply_catalog._indexes[name].items()
            ]
            for name in reply_catalog.status_blocks
        },
        "offsets": {
            name: [
                {"start": _point_dict(start), "end": _point_dict(end)}
                for start, end in reply_catalog._indexes[name]
            ]
            for name in reply_catalog.offset_blocks
        },
    }
    if with_numbers:
        blocks_dir = root / "blocks"
        blocks_dir.mkdir(exist_ok=True)
        for block in reply_catalog.blocks:
            payload = _block_payload(block, reply_catalog.read_block(block.name))
            (blocks_dir / f"{block.name}.json").write_text(
                json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        index["files"] = {
            block.name: f"blocks/{block.name}.json"
            for block in reply_catalog.blocks
        }
    (root / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (root / "catalog.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def _index_from_catalog(index: Mapping[str, Any]) -> dict[str, Any]:
    blocks: dict[str, dict[str, Any]] = {}
    offsets: dict[str, tuple[tuple[PointRef, PointRef], ...]] = {}
    absolutes: dict[str, tuple[PointRef, ...]] = {}
    statuses: dict[str, dict[PointRef, str]] = {}
    for item in index.get("blocks", ()):
        name = str(item["name"])
        blocks[name] = {"kind": str(item["kind"])}
    for name, rows in index.get("offsets", {}).items():
        offsets[str(name)] = tuple(
            (
                _point_id(
                    (row["start"]["processor"], row["start"]["batch"], row["start"]["point"])
                ),
                _point_id(
                    (row["end"]["processor"], row["end"]["batch"], row["end"]["point"])
                ),
            )
            for row in rows
        )
    for name, points in index.get("absolutes", {}).items():
        absolutes[str(name)] = tuple(
            _point_id((row["processor"], row["batch"], row["point"]))
            for row in points
        )
    for name, rows in index.get("statuses", {}).items():
        statuses[str(name)] = {
            _point_id((row["processor"], row["batch"], row["point"])): str(
                row["status"]
            )
            for row in rows
        }
    return {
        "blocks": blocks,
        "offsets": offsets,
        "absolutes": absolutes,
        "statuses": statuses,
    }


def load_replies(path: str | Path) -> ReplyCatalog:
    """Read a reply catalog written with :func:`save_replies`.

    Building the catalog reads only the index.  Each number block stays
    file-backed and is read the first time it is actually needed.
    """
    root = Path(path)
    index_path = root / "index.json"
    if not index_path.exists():
        raise ValueError(f"{root} does not contain a reply index")
    index = json.loads(index_path.read_text(encoding="utf-8"))
    parsed = _index_from_catalog(index)
    files = index.get("files", {})
    blocks: list[ReplyBlock] = []
    for item in index["blocks"]:
        name = str(item["name"])
        kind = str(item["kind"])
        if kind == "offset":
            metadata = parsed["offsets"][name]
        elif kind == "absolute":
            metadata = parsed["absolutes"][name]
        else:
            metadata = parsed["statuses"][name]
        if name not in files:
            raise ValueError(
                f"reply directory {root} was written without numbers for block {name!r}"
            )
        blocks.append(
            JsonFileBlock(
                name=name,
                kind=kind,
                meta=metadata,
                path=root / files[name],
            )
        )
    return ReplyCatalog(blocks=blocks)


@dataclass
class MetadataBlock:
    """A reply block that carries only metadata and no readable numbers."""

    name: str
    kind: str
    meta: Any

    def metadata(self) -> Any:
        """Return metadata without reading numbers."""
        return self.meta

    def read(self) -> Any:
        """Raise: this block was archived without numbers."""
        raise ValueError(
            f"reply block {self.name!r} was archived without numbers"
        )


def load_catalog(path: str | Path) -> ReplyCatalog:
    """Read a reply directory that was written without numbers.

    The result can describe which points have readings, which are missing
    and both endpoints of every offset, but cannot produce values.
    """
    root = Path(path)
    catalog_path = root / "catalog.json"
    index_path = root / "index.json"
    if not catalog_path.exists() and not index_path.exists():
        raise ValueError(f"{root} does not contain a reply catalog")
    index = json.loads((catalog_path if catalog_path.exists() else index_path).read_text(encoding="utf-8"))
    parsed = _index_from_catalog(index)
    blocks: list[ReplyBlock] = []
    for name, info in parsed["blocks"].items():
        kind = info["kind"]
        if kind == "offset":
            blocks.append(MetadataBlock(name=name, kind=kind, meta=parsed["offsets"].get(name, ())))
        elif kind == "absolute":
            blocks.append(MetadataBlock(name=name, kind=kind, meta=parsed["absolutes"].get(name, ())))
        else:
            blocks.append(MetadataBlock(name=name, kind=kind, meta=parsed["statuses"].get(name, {})))
    return ReplyCatalog(blocks=blocks)


def register_point_handoff_accessor() -> None:
    """Install the opt-in ``point_handoff`` dataset accessor."""
    xr.register_dataset_accessor("point_handoff")(PointHandoffAccessor)


def open_point_handoff(
    dataset: xr.Dataset,
    *,
    handoff: str | Path | HandoffCatalog | Sequence[str | Path | HandoffCatalog] | None = None,
    replies: str | Path | ReplyCatalog | None = None,
    axes: Sequence[str] | None = None,
) -> xr.Dataset:
    """Reopen an archived grid together with its handoff and replies.

    Parameters
    ----------
    dataset : Dataset
        The original archive grid, as reopened from its file.
    handoff : path, catalog or sequence of either, optional
        Handoff description(s) written by :func:`save_handoff`.
    replies : path or ReplyCatalog, optional
        Reply directory written by :func:`save_replies`.
    axes : sequence of str, optional
        Original axis names.  Taken from the handoff description when it
        carries them.
    """
    if handoff is None:
        items: list[HandoffCatalog] = []
    elif isinstance(handoff, (HandoffCatalog, str, Path)):
        items = [_load_handoff_item(handoff)]
    else:
        items = [_load_handoff_item(item) for item in handoff]

    reply_catalog: ReplyCatalog | None = None
    if isinstance(replies, ReplyCatalog):
        reply_catalog = replies
    elif replies is not None:
        reply_path = Path(replies)
        reply_catalog = (
            load_replies(reply_path)
            if _has_number_blocks(reply_path)
            else load_catalog(reply_path)
        )

    resolved_axes = (
        tuple(str(axis) for axis in axes)
        if axes is not None
        else (
            items[0].axes
            if items
            else tuple(str(dim) for dim in dataset.dims)
        )
    )
    return attach(dataset, items, reply_catalog, axes=resolved_axes)


def _load_handoff_item(item: str | Path | HandoffCatalog) -> HandoffCatalog:
    if isinstance(item, HandoffCatalog):
        return item
    return load_handoff(item)


def _has_number_blocks(path: Path) -> bool:
    if not (path / "blocks").exists():
        return False
    index_path = path / "index.json"
    if not index_path.exists():
        return False
    index = json.loads(index_path.read_text(encoding="utf-8"))
    return bool(index.get("files"))
