"""Layout-aware combination of tiled ground observations.

This module augments :py:func:`xarray.combine_by_coords` with an explicit tile
:py:class:`TileLayout`. Tiles may store the ``y`` axis in reverse order and may
sample shared strips of pixels along their edges. The shared strips are paired
by their actual pixel centers and used to estimate additive correction terms
that bring every tile onto the calibration baseline.

Observations are only read when a window is materialized, and edge comparisons
are cached per pair of tile versions so reopening windows never repeats the
same comparison.
"""

from __future__ import annotations

import math
from collections.abc import Hashable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal

import numpy as np

from xarray.core import dtypes
from xarray.core.dataarray import DataArray
from xarray.core.dataset import Dataset
from xarray.structure.combine import combine_by_coords

if TYPE_CHECKING:
    from xarray.core.types import CompatOptions, JoinOptions

__all__ = [
    "CombinedTileWindow",
    "SharedStrip",
    "TileCalibrationConflict",
    "TileCalibrationDiagnostic",
    "TileLayout",
    "TileLayoutCombiner",
    "TileLayoutError",
    "TilePlacement",
    "combine_tiled_by_coords",
]

# Tolerances are deliberately tight: a tile offset by half a pixel must be
# rejected, while ordinary floating point round-off must pass.
_CENTER_RTOL = 1e-9
_DELTA_RTOL = 1e-7
_DELTA_ATOL = 1e-8

WindowStatus = Literal["ok", "no_baseline", "conflict"]
EdgeAxis = Literal["x", "y"]
_PropagationItem = tuple[
    Hashable,
    float,
    tuple[Hashable, Hashable] | None,
    Hashable | None,
]


class TileLayoutError(ValueError):
    """Raised when a tile does not fit the declared layout.

    The error message always names the offending tile and the problematic
    pixel-center coordinates. A raised error rejects the new tile version; the
    previously accepted version keeps working.
    """


@dataclass(frozen=True)
class TilePlacement:
    """Position and internal extent of one tile within the whole image.

    Attributes
    ----------
    position : tuple of int
        ``(row, column)`` of the tile in the tile grid, starting at zero.
    x_bounds, y_bounds : tuple of float
        Inclusive ``(minimum, maximum)`` pixel-center coordinates of the
        tile's internal extent in whole-image coordinates.
    y_reversed : bool, default False
        Whether the tile stores its ``y`` axis in decreasing order.
    """

    position: tuple[int, int]
    x_bounds: tuple[float, float]
    y_bounds: tuple[float, float]
    y_reversed: bool = False


@dataclass(frozen=True)
class SharedStrip:
    """A shared edge on which two tiles may be compared.

    Only tiles sharing an edge may be paired. The actual pixel pairs are
    derived from the tiles' pixel centers at comparison time.

    Attributes
    ----------
    tiles : tuple of hashable
        The two tile ids adjacent to the edge.
    axis : {"x", "y"}
        Normal direction of the edge: ``"x"`` for side-by-side tiles and
        ``"y"`` for vertically stacked tiles.
    """

    tiles: tuple[Hashable, Hashable]
    axis: EdgeAxis


@dataclass(frozen=True)
class TileLayout:
    """Whole-image pixel centers, tile extents and comparable shared strips.

    Attributes
    ----------
    x_centers, y_centers : sequence of float
        Pixel-center coordinates of the whole image, in increasing order.
    tiles : mapping
        Tile id to :py:class:`TilePlacement`. Missing tiles simply have no
        entry while keeping their grid position empty.
    shared_strips : sequence of SharedStrip
        Edges that are allowed to be used for calibration.
    baseline : mapping
        Known additive correction for anchor tiles, e.g. ``{"A": 0.0}``.
        Every connected tile group needs at least one reachable baseline tile.
    x_dim, y_dim : hashable, default "x" / "y"
        Names of the horizontal and vertical dimensions and coordinates.
    """

    x_centers: Sequence[float]
    y_centers: Sequence[float]
    tiles: Mapping[Hashable, TilePlacement]
    shared_strips: Sequence[SharedStrip]
    baseline: Mapping[Hashable, float] = field(default_factory=dict)
    x_dim: Hashable = "x"
    y_dim: Hashable = "y"


@dataclass(frozen=True)
class TileCalibrationConflict:
    """One contradictory correction reached for a tile.

    Attributes
    ----------
    variable : hashable
        Data variable for which the contradiction was observed.
    tile : hashable
        Tile whose correction is ambiguous.
    corrections : tuple of float
        The competing corrections, in the same order as ``via``.
    via : tuple of hashable
        Neighbor tile through which each correction was propagated.
    edges : tuple of tuple
        The shared edge (two tile ids) used for each correction.
    tile_versions : tuple of tuple
        ``((tile id, version number), ...)`` of the two tiles backing each
        edge estimate.
    """

    variable: Hashable
    tile: Hashable
    corrections: tuple[float, ...]
    via: tuple[Hashable, ...]
    edges: tuple[tuple[Hashable, Hashable], ...]
    tile_versions: tuple[tuple[tuple[Hashable, int], tuple[Hashable, int]], ...]


@dataclass(frozen=True)
class TileCalibrationDiagnostic:
    """Calibration state visible through a window.

    Attributes
    ----------
    status : {"ok", "no_baseline", "conflict"}
        Overall state; ``"conflict"`` takes precedence over ``"no_baseline"``.
    corrections : mapping
        Correction reached for every tile that could be anchored, keyed by
        tile id. Tiles that no usable edge path could reach are absent.
    no_baseline_tiles : tuple of hashable
        Tiles needed by a window whose correction could not be anchored to a
        baseline, plus every tile of baseline-free connected groups.
    conflicts : tuple of TileCalibrationConflict
        Every contradictory correction, with the edges and tile versions that
        produced it.
    unusable_edges : tuple
        Edges that could not calibrate (fewer than two valid pairs or
        inconsistent deltas), given as ``(variable, (tile id, tile id))``.
    """

    status: WindowStatus
    corrections: Mapping[Hashable, float]
    no_baseline_tiles: tuple[Hashable, ...]
    conflicts: tuple[TileCalibrationConflict, ...]
    unusable_edges: tuple[tuple[Hashable, tuple[Hashable, Hashable]], ...]


@dataclass(frozen=True)
class _TileGeometry:
    """Matched pixel centers of one accepted tile version."""

    x_indices: np.ndarray
    y_indices: np.ndarray
    x_labels: np.ndarray
    y_labels: np.ndarray
    x_internal: np.ndarray
    y_internal: np.ndarray


@dataclass(frozen=True)
class _EdgeEstimate:
    usable: bool
    delta: float | None
    pair_count: int
    reason: str | None


def _nearest_index(values: np.ndarray, grid: np.ndarray, tol: float) -> np.ndarray:
    """Return the grid index nearest to each value or -1 when farther than tol."""
    positions = np.searchsorted(grid, values)
    indices = np.empty(values.shape, dtype=np.int64)
    for i, value in enumerate(values):
        candidates = []
        pos = positions[i]
        if pos < grid.size:
            candidates.append(pos)
        if pos > 0:
            candidates.append(pos - 1)
        best = min(candidates, key=lambda j: abs(float(grid[j]) - float(value)))
        indices[i] = best if abs(float(grid[best]) - float(value)) <= tol else -1
    return indices


def _duplicate_positions(indices: np.ndarray) -> np.ndarray:
    unique, counts = np.unique(indices, return_counts=True)
    duplicated = unique[counts > 1]
    return np.isin(indices, duplicated)


class CombinedTileWindow:
    """A crop window over a :py:class:`TileLayoutCombiner`.

    Windows are immutable snapshots: replacing tiles after opening a window
    does not change the versions a previously opened window reads. Observation
    values are only touched when :py:meth:`read` or :py:meth:`read_raw` is
    called (or :py:attr:`status` / :py:attr:`diagnostic` is inspected).
    """

    def __init__(
        self,
        combiner: TileLayoutCombiner,
        versions: Mapping[Hashable, int],
        x_labels: Sequence[float],
        y_labels: Sequence[float],
        variables: Sequence[Hashable] | None,
    ) -> None:
        self._combiner = combiner
        self._versions = dict(versions)
        self._x_labels = np.asarray(x_labels, dtype=float)
        self._y_labels = np.asarray(y_labels, dtype=float)
        self._variables = None if variables is None else list(variables)
        self._diagnostic: TileCalibrationDiagnostic | None = None
        self._variable_corrections: Mapping[Hashable, Mapping[Hashable, float]] = {}
        self._withheld_variables: set[Hashable] = set()

    @property
    def layout(self) -> TileLayout:
        return self._combiner.layout

    @property
    def x(self) -> np.ndarray:
        return self._x_labels

    @property
    def y(self) -> np.ndarray:
        return self._y_labels

    @property
    def status(self) -> WindowStatus:
        return self._ensure_diagnostic().status

    @property
    def diagnostic(self) -> TileCalibrationDiagnostic:
        return self._ensure_diagnostic()

    def _ensure_diagnostic(self) -> TileCalibrationDiagnostic:
        if self._diagnostic is None:
            (
                self._diagnostic,
                self._variable_corrections,
                self._withheld_variables,
            ) = self._combiner._calibrate(
                self._versions, self._affected_tiles(), self._variables
            )
        return self._diagnostic

    def _affected_tiles(self) -> set[Hashable]:
        layout = self._combiner.layout
        x_min, x_max = float(self._x_labels.min()), float(self._x_labels.max())
        y_min, y_max = float(self._y_labels.min()), float(self._y_labels.max())
        affected: set[Hashable] = set()
        for tile_id, placement in layout.tiles.items():
            if tile_id not in self._versions:
                continue
            if (
                placement.x_bounds[1] >= x_min
                and placement.x_bounds[0] <= x_max
                and placement.y_bounds[1] >= y_min
                and placement.y_bounds[0] <= y_max
            ):
                affected.add(tile_id)
        return affected

    def read_raw(self) -> Dataset:
        """Assemble the window without applying any correction."""
        return self._combiner._assemble(
            self._versions,
            self._x_labels,
            self._y_labels,
            self._variables,
            corrections={},
            apply_correction=False,
        )

    def read(self) -> Dataset:
        """Assemble the window with calibrated readings.

        Variables whose tile group cannot be anchored or whose edge-derived
        corrections contradict each other are withheld (filled with missing
        values), while :py:attr:`status` / :py:attr:`diagnostic` explains why.
        Other variables are still delivered calibrated. Coordinates and
        intrinsically missing readings are always preserved.
        """
        self._ensure_diagnostic()
        return self._combiner._assemble(
            self._versions,
            self._x_labels,
            self._y_labels,
            self._variables,
            corrections=self._variable_corrections,
            apply_correction=True,
            withheld_variables=self._withheld_variables,
        )


class TileLayoutCombiner:
    """Lazy, layout-aware combination of tiled observations.

    Use :py:meth:`open_window` to crop a window and :py:meth:`replace_tiles` to
    swap in a new version of one or more tiles. Every accepted tile version is
    kept, so windows opened earlier remain readable.
    """

    def __init__(
        self,
        tiles: Mapping[Hashable, Dataset],
        layout: TileLayout,
    ) -> None:
        self._layout = layout
        self._archive: dict[Hashable, list[Dataset]] = {}
        self._geometry: dict[tuple[Hashable, int], _TileGeometry] = {}
        self._coord_reads: set[tuple[Hashable, int, str]] = set()
        self._edge_cache: dict[tuple[Hashable, ...], _EdgeEstimate] = {}
        self._value_reads: list[
            tuple[Hashable, int, Hashable, tuple[float, ...], tuple[float, ...]]
        ] = []
        self._validate_layout()
        self._adopt_tiles(tiles)

    @property
    def layout(self) -> TileLayout:
        return self._layout

    @property
    def tile_versions(self) -> dict[Hashable, int]:
        """Currently active version number (starting at zero) per tile id."""
        return {
            tile_id: len(versions) - 1 for tile_id, versions in self._archive.items()
        }

    @property
    def value_reads(self) -> tuple[tuple, ...]:
        """Observation regions read so far, mainly useful for testing."""
        return tuple(self._value_reads)

    @property
    def coordinate_reads(self) -> tuple[tuple, ...]:
        """Tile coordinate arrays read so far, mainly useful for testing."""
        return tuple(sorted(self._coord_reads))

    def _validate_layout(self) -> None:
        layout = self._layout
        x = np.asarray(layout.x_centers, dtype=float)
        y = np.asarray(layout.y_centers, dtype=float)
        if (
            x.size < 1
            or y.size < 1
            or np.any(np.diff(x) <= 0)
            or np.any(np.diff(y) <= 0)
        ):
            raise TileLayoutError("layout pixel centers must be strictly increasing")
        for strip in layout.shared_strips:
            a, b = strip.tiles
            if a not in layout.tiles or b not in layout.tiles:
                raise TileLayoutError(
                    f"shared strip {strip.tiles!r} references an unknown tile"
                )
            pa, pb = layout.tiles[a], layout.tiles[b]
            if strip.axis == "x":
                if pa.position[0] != pb.position[0]:
                    raise TileLayoutError(
                        f"strip {strip.tiles!r} is declared as a vertical edge "
                        "but the tiles are not in the same row"
                    )
                if abs(pa.position[1] - pb.position[1]) != 1:
                    raise TileLayoutError(
                        f"strip {strip.tiles!r} can only compare tiles sharing "
                        "an edge, but the tiles are not adjacent columns"
                    )
            if strip.axis == "y":
                if pa.position[1] != pb.position[1]:
                    raise TileLayoutError(
                        f"strip {strip.tiles!r} is declared as a horizontal "
                        "edge but the tiles are not in the same column"
                    )
                if abs(pa.position[0] - pb.position[0]) != 1:
                    raise TileLayoutError(
                        f"strip {strip.tiles!r} can only compare tiles sharing "
                        "an edge, but the tiles are not adjacent rows"
                    )
        for tile_id in layout.baseline:
            if tile_id not in layout.tiles:
                raise TileLayoutError(
                    f"baseline tile {tile_id!r} is not part of the layout"
                )

    def _adopt_tiles(self, tiles: Mapping[Hashable, Dataset]) -> None:
        # Validate every candidate before committing anything, so a rejected
        # replacement leaves the previously accepted versions in place.
        candidates: dict[Hashable, tuple[Dataset, _TileGeometry]] = {}
        for tile_id, dataset in tiles.items():
            if tile_id not in self._layout.tiles:
                raise TileLayoutError(
                    f"tile {tile_id!r} has no placement in the layout"
                )
            version = len(self._archive.get(tile_id, ()))
            geometry = self._build_geometry(tile_id, dataset)
            candidates[tile_id] = (dataset, geometry)
        for tile_id, (dataset, geometry) in candidates.items():
            self._archive.setdefault(tile_id, []).append(dataset)
            version = len(self._archive[tile_id]) - 1
            self._geometry[(tile_id, version)] = geometry

    def _build_geometry(
        self, tile_id: Hashable, dataset: Dataset
    ) -> _TileGeometry:
        layout = self._layout
        placement = layout.tiles[tile_id]
        global_x = np.asarray(layout.x_centers, dtype=float)
        global_y = np.asarray(layout.y_centers, dtype=float)
        x_tol = _CENTER_RTOL * max(1.0, float(np.ptp(global_x)))
        y_tol = _CENTER_RTOL * max(1.0, float(np.ptp(global_y)))

        x_labels = np.sort(
            self._read_centers(tile_id, dataset, layout.x_dim, "x")
        )
        y_labels = np.sort(
            self._read_centers(tile_id, dataset, layout.y_dim, "y")
        )

        x_indices = _nearest_index(x_labels, global_x, x_tol)
        y_indices = _nearest_index(y_labels, global_y, y_tol)
        bad_x = x_labels[x_indices < 0]
        bad_y = y_labels[y_indices < 0]
        if bad_x.size or bad_y.size:
            raise TileLayoutError(
                f"tile {tile_id!r} is offset from the layout pixel grid: "
                f"x={bad_x.tolist()}, y={bad_y.tolist()} (a half-pixel shift "
                "is not accepted)"
            )
        if np.unique(x_indices).size != x_indices.size:
            dup = x_labels[_duplicate_positions(x_indices)]
            raise TileLayoutError(
                f"tile {tile_id!r} samples the same whole-image column more "
                f"than once; duplicated x centers: {dup.tolist()}"
            )
        if np.unique(y_indices).size != y_indices.size:
            dup = y_labels[_duplicate_positions(y_indices)]
            raise TileLayoutError(
                f"tile {tile_id!r} samples the same whole-image row more than "
                f"once; duplicated y centers: {dup.tolist()}"
            )

        x_lo, x_hi = placement.x_bounds
        y_lo, y_hi = placement.y_bounds
        self._check_internal_coverage(
            tile_id, "x", x_labels, x_indices, global_x, x_lo, x_hi
        )
        self._check_internal_coverage(
            tile_id, "y", y_labels, y_indices, global_y, y_lo, y_hi
        )
        x_internal = (x_labels >= x_lo - x_tol) & (x_labels <= x_hi + x_tol)
        y_internal = (y_labels >= y_lo - y_tol) & (y_labels <= y_hi + y_tol)
        return _TileGeometry(
            x_indices, y_indices, x_labels, y_labels, x_internal, y_internal
        )

    @staticmethod
    def _check_internal_coverage(
        tile_id: Hashable,
        axis: str,
        labels: np.ndarray,
        indices: np.ndarray,
        grid: np.ndarray,
        low: float,
        high: float,
    ) -> None:
        tol = _CENTER_RTOL * max(1.0, float(np.ptp(grid)))
        lo_pos = int(np.searchsorted(grid, low))
        hi_pos = int(np.searchsorted(grid, high))
        if not (
            lo_pos < grid.size
            and hi_pos < grid.size
            and math.isclose(float(grid[lo_pos]), low, abs_tol=tol)
            and math.isclose(float(grid[hi_pos]), high, abs_tol=tol)
        ):
            raise TileLayoutError(
                f"tile {tile_id!r} declares an internal {axis} extent "
                f"({low}, {high}) that is not part of the layout pixel grid"
            )
        expected = set(range(lo_pos, hi_pos + 1))
        actual = set(indices.tolist())
        missing = sorted(expected - actual)
        if missing:
            coords = [float(grid[pos]) for pos in missing]
            raise TileLayoutError(
                f"tile {tile_id!r} is missing internal {axis} pixel center(s) "
                f"{coords}; extra sampled columns cannot replace missing "
                "internal columns"
            )

    def _read_centers(
        self,
        tile_id: Hashable,
        dataset: Dataset,
        coord_name: Hashable,
        axis: str,
    ) -> np.ndarray:
        version = len(self._archive.get(tile_id, ()))
        self._coord_reads.add((tile_id, version, axis))
        return np.asarray(dataset[coord_name].values, dtype=float)

    def replace_tiles(self, tiles: Mapping[Hashable, Dataset]) -> None:
        """Validate and install new tile versions.

        Raises :py:class:`TileLayoutError` naming the offending tile and
        coordinates when a new tile does not fit the layout. Nothing is
        committed in that case, so the previously accepted versions remain
        usable.
        """
        self._adopt_tiles(tiles)

    def open_window(
        self,
        x: float | Sequence[float] | slice,
        y: float | Sequence[float] | slice,
        variables: Sequence[Hashable] | None = None,
    ) -> CombinedTileWindow:
        """Open an immutable crop window without reading observations."""
        x_labels = self._resolve_labels(x, self._layout.x_centers, self._layout.x_dim)
        y_labels = self._resolve_labels(y, self._layout.y_centers, self._layout.y_dim)
        return CombinedTileWindow(
            self, self.tile_versions, x_labels, y_labels, variables
        )

    def _resolve_labels(
        self,
        selector: float | Sequence[float] | slice,
        grid: Sequence[float],
        dim: Hashable,
    ) -> np.ndarray:
        grid_array = np.asarray(grid, dtype=float)
        if isinstance(selector, slice):
            start = (
                None
                if selector.start is None
                else self._grid_position(selector.start, grid_array, dim)
            )
            stop = (
                None
                if selector.stop is None
                else self._grid_position(selector.stop, grid_array, dim)
            )
            return grid_array[slice(start, None if stop is None else stop + 1)]
        if np.isscalar(selector):
            pos = self._grid_position(selector, grid_array, dim)
            return grid_array[pos : pos + 1]
        positions = sorted(
            self._grid_position(v, grid_array, dim) for v in selector
        )
        return grid_array[positions]

    @staticmethod
    def _grid_position(value: float, grid: np.ndarray, dim: Hashable) -> int:
        tol = _CENTER_RTOL * max(1.0, float(np.ptp(grid)))
        pos = int(np.searchsorted(grid, value))
        if pos >= grid.size or not math.isclose(
            float(grid[pos]), float(value), abs_tol=tol
        ):
            raise KeyError(f"{value!r} is not a pixel center of dimension {dim!r}")
        return pos

    def _data_variables(self, variables: Sequence[Hashable] | None) -> list[Hashable]:
        layout = self._layout
        names: set[Hashable] = set()
        for versions in self._archive.values():
            dataset = versions[-1]
            for name in dataset.data_vars:
                dims = dataset[name].dims
                if layout.x_dim in dims and layout.y_dim in dims and len(dims) == 2:
                    names.add(name)
        selected = sorted(names, key=lambda item: str(item))
        if variables is not None:
            unknown = [name for name in variables if name not in selected]
            if unknown:
                raise KeyError(f"unknown spatial data variable(s): {unknown}")
            selected = [name for name in selected if name in variables]
        return selected

    def _tile_labels(
        self,
        tile_id: Hashable,
        version: int,
        axis: str,
        global_labels: np.ndarray,
    ) -> np.ndarray:
        geometry = self._geometry[(tile_id, version)]
        grid = np.asarray(
            self._layout.x_centers if axis == "x" else self._layout.y_centers,
            dtype=float,
        )
        own = geometry.x_labels if axis == "x" else geometry.y_labels
        indices = geometry.x_indices if axis == "x" else geometry.y_indices
        global_positions = np.searchsorted(grid, global_labels)
        label_by_position = dict(zip(indices.tolist(), own.tolist(), strict=True))
        return np.array(
            [label_by_position[int(pos)] for pos in global_positions], dtype=float
        )

    def _load_tile_region(
        self,
        tile_id: Hashable,
        version: int,
        variable: Hashable,
        global_x: np.ndarray,
        global_y: np.ndarray,
    ) -> DataArray:
        """Read one observation region. Override in tests to count IO."""
        dataset = self._archive[tile_id][version]
        tile_x = self._tile_labels(tile_id, version, "x", global_x)
        tile_y = self._tile_labels(tile_id, version, "y", global_y)
        self._value_reads.append(
            (
                tile_id,
                version,
                variable,
                tuple(float(v) for v in global_x),
                tuple(float(v) for v in global_y),
            )
        )
        return dataset[variable].sel(
            {self._layout.x_dim: tile_x, self._layout.y_dim: tile_y}
        )

    def _edge_geometry(
        self,
        versions: Mapping[Hashable, int],
        strip: SharedStrip,
    ) -> tuple[Hashable, Hashable, np.ndarray, np.ndarray]:
        a, b = strip.tiles
        ga = self._geometry[(a, versions[a])]
        gb = self._geometry[(b, versions[b])]
        if strip.axis == "x":
            shared = np.intersect1d(ga.x_indices, gb.x_indices)
            cross = np.intersect1d(ga.y_indices, gb.y_indices)
        else:
            shared = np.intersect1d(ga.y_indices, gb.y_indices)
            cross = np.intersect1d(ga.x_indices, gb.x_indices)
        return a, b, shared, cross

    def _estimate_edge(
        self,
        versions: Mapping[Hashable, int],
        strip: SharedStrip,
        variable: Hashable,
    ) -> _EdgeEstimate:
        layout = self._layout
        a, b, shared, cross = self._edge_geometry(versions, strip)
        cache_key = (
            variable,
            strip.axis,
            (a, versions[a]),
            (b, versions[b]),
            tuple(shared.tolist()),
            tuple(cross.tolist()),
        )
        if cache_key in self._edge_cache:
            return self._edge_cache[cache_key]

        if shared.size == 0 or cross.size == 0:
            estimate = _EdgeEstimate(False, None, 0, "no overlapping pixel centers")
        else:
            global_x = np.asarray(layout.x_centers, dtype=float)
            global_y = np.asarray(layout.y_centers, dtype=float)
            if strip.axis == "x":
                x_labels = global_x[shared]
                y_labels = global_y[cross]
            else:
                x_labels = global_x[cross]
                y_labels = global_y[shared]
            data_a = self._load_tile_region(
                a, versions[a], variable, x_labels, y_labels
            )
            data_b = self._load_tile_region(
                b, versions[b], variable, x_labels, y_labels
            )
            values_a = np.asarray(data_a.values, dtype=float)
            values_b = np.asarray(data_b.values, dtype=float)
            valid = ~np.isnan(values_a) & ~np.isnan(values_b)
            pair_count = int(valid.sum())
            if pair_count < 2:
                estimate = _EdgeEstimate(
                    False, None, pair_count, "fewer than two valid shared pairs"
                )
            else:
                # A reading that is lower by ``d`` needs ``d`` more correction,
                # so the correction difference equals raw_a - raw_b.
                deltas = values_a[valid] - values_b[valid]
                reference = float(deltas[0])
                if np.all(
                    np.isclose(deltas, reference, rtol=_DELTA_RTOL, atol=_DELTA_ATOL)
                ):
                    estimate = _EdgeEstimate(True, reference, pair_count, None)
                else:
                    estimate = _EdgeEstimate(
                        False,
                        None,
                        pair_count,
                        "pair deltas disagree; edge cannot calibrate",
                    )
        self._edge_cache[cache_key] = estimate
        return estimate

    def _connected_components(
        self, versions: Mapping[Hashable, int]
    ) -> list[set[Hashable]]:
        adjacency: dict[Hashable, set[Hashable]] = {
            tile_id: set() for tile_id in versions
        }
        for strip in self._layout.shared_strips:
            a, b = strip.tiles
            if a in versions and b in versions:
                adjacency[a].add(b)
                adjacency[b].add(a)
        components: list[set[Hashable]] = []
        seen: set[Hashable] = set()
        for tile_id in versions:
            if tile_id in seen:
                continue
            stack = [tile_id]
            component: set[Hashable] = set()
            while stack:
                current = stack.pop()
                if current in component:
                    continue
                component.add(current)
                stack.extend(adjacency[current] - component)
            seen |= component
            components.append(component)
        return components

    def _calibrate(
        self,
        versions: Mapping[Hashable, int],
        affected: set[Hashable],
        variable_names: Sequence[Hashable] | None = None,
    ) -> tuple[
        TileCalibrationDiagnostic,
        dict[Hashable, dict[Hashable, float]],
        set[Hashable],
    ]:
        variables = self._data_variables(variable_names)
        components = [
            component
            for component in self._connected_components(versions)
            if component & affected
        ]

        merged_corrections: dict[Hashable, float] = {}
        per_variable_corrections: dict[Hashable, dict[Hashable, float]] = {}
        conflicts: list[TileCalibrationConflict] = []
        unusable: list[tuple[Hashable, tuple[Hashable, Hashable]]] = []
        conflict_variables: set[Hashable] = set()
        no_baseline: set[Hashable] = set()
        withheld_variables: set[Hashable] = set()

        for component in components:
            if not any(tile_id in self._layout.baseline for tile_id in component):
                no_baseline.update(component)

        for variable in variables:
            var_corrections, var_conflicts, var_unusable = self._calibrate_variable(
                variable, versions, components, affected
            )
            per_variable_corrections[variable] = dict(
                sorted(var_corrections.items(), key=lambda item: str(item[0]))
            )
            for tile_id, value in var_corrections.items():
                merged_corrections.setdefault(tile_id, value)
            conflict_tiles = {conflict.tile for conflict in var_conflicts}
            if var_conflicts:
                conflict_variables.add(variable)
            conflicts.extend(var_conflicts)
            unusable.extend(var_unusable)
            # Contradictory tiles are reported as conflicts, not as missing
            # baselines; only genuinely unreachable tiles count here.
            missing = affected - set(var_corrections) - conflict_tiles
            if missing:
                no_baseline.update(missing)
                withheld_variables.add(variable)
        withheld_variables |= conflict_variables

        status: WindowStatus = "ok"
        if no_baseline:
            status = "no_baseline"
        if conflicts:
            status = "conflict"
        diagnostic = TileCalibrationDiagnostic(
            status,
            dict(sorted(merged_corrections.items(), key=lambda item: str(item[0]))),
            tuple(sorted(no_baseline, key=str)),
            tuple(conflicts),
            tuple(unusable),
        )
        return diagnostic, per_variable_corrections, withheld_variables

    def _calibrate_variable(
        self,
        variable: Hashable,
        versions: Mapping[Hashable, int],
        components: list[set[Hashable]],
        affected: set[Hashable],
    ) -> tuple[
        dict[Hashable, float],
        list[TileCalibrationConflict],
        list[tuple[Hashable, tuple[Hashable, Hashable]]],
    ]:
        corrections: dict[Hashable, float] = {}
        conflicts: list[TileCalibrationConflict] = []
        unusable: list[tuple[Hashable, tuple[Hashable, Hashable]]] = []

        for component in components:
            targets = component & affected
            strips = self._component_edges(component)
            adjacency: dict[Hashable, list[tuple[Hashable, Hashable]]] = {
                tile_id: [] for tile_id in component
            }
            for strip in strips:
                edge = self._ordered_edge(strip)
                adjacency[edge[0]].append(edge)
                adjacency[edge[1]].append(edge)

            # An edge is read only when it lies on at least one baseline ->
            # window-tile path. Branch strips never leading to a target tile
            # are not touched.
            baselines = component & set(self._layout.baseline)
            qualifying = [
                strip
                for strip in strips
                if self._edge_qualifies(
                    self._ordered_edge(strip), adjacency, baselines, targets
                )
            ]

            estimates: dict[tuple[Hashable, Hashable], _EdgeEstimate] = {}
            usable_adjacency: dict[Hashable, list[tuple[Hashable, Hashable]]] = {
                tile_id: [] for tile_id in component
            }
            for strip in qualifying:
                edge = self._ordered_edge(strip)
                estimate = self._estimate_edge(versions, strip, variable)
                if estimate.usable:
                    estimates[edge] = estimate
                    usable_adjacency[edge[0]].append(edge)
                    usable_adjacency[edge[1]].append(edge)
                else:
                    unusable.append((variable, edge))

            # One entry per (node, last edge on a propagation path), so every
            # edge-derived correction can be cross-checked against the others.
            candidates: dict[
                Hashable, list[tuple[float, tuple[Hashable, Hashable], Hashable]]
            ] = {tile_id: [] for tile_id in component}
            queue: list[_PropagationItem] = []
            for tile_id in sorted(component, key=str):
                if tile_id in self._layout.baseline:
                    queue.append(
                        (tile_id, float(self._layout.baseline[tile_id]), None, None)
                    )
            seen_pairs: set[tuple[Hashable, Hashable | None]] = set()
            while queue:
                node, value, entry_edge, via = queue.pop(0)
                pair = (node, entry_edge)
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)
                if entry_edge is not None and via is not None:
                    candidates[node].append((value, entry_edge, via))
                else:
                    candidates[node].append((value, (node, node), node))
                for edge in usable_adjacency[node]:
                    neighbor, delta = self._cross_edge(edge, node, estimates[edge])
                    if neighbor is None or neighbor not in component:
                        continue
                    queue.append((neighbor, value + delta, edge, node))

            for tile_id in sorted(component, key=str):
                options = candidates[tile_id]
                if not options:
                    continue
                distinct: dict[float, tuple[tuple[Hashable, Hashable], Hashable]] = {}
                for value, edge, via in options:
                    if not any(
                        np.isclose(value, other, rtol=_DELTA_RTOL, atol=_DELTA_ATOL)
                        for other in distinct
                    ):
                        distinct[value] = (edge, via)
                if len(distinct) > 1:
                    conflicts.append(
                        self._build_conflict(
                            variable, tile_id, distinct, versions
                        )
                    )
                else:
                    corrections[tile_id] = options[0][0]
        return corrections, conflicts, unusable

    @staticmethod
    def _edge_qualifies(
        edge: tuple[Hashable, Hashable],
        adjacency: Mapping[Hashable, list[tuple[Hashable, Hashable]]],
        baselines: set[Hashable],
        targets: set[Hashable],
    ) -> bool:
        # The edge lies on a baseline -> target simple path exactly when one
        # endpoint is reachable from a baseline without using the edge while
        # the other endpoint can reach a target without it.
        if not baselines or not targets:
            return False
        a, b = edge

        def component_of(seeds: set[Hashable]) -> set[Hashable]:
            reached = set(seeds)
            queue = list(seeds)
            while queue:
                node = queue.pop(0)
                for candidate in adjacency.get(node, ()):
                    neighbor = (
                        candidate[1] if candidate[0] == node else candidate[0]
                    )
                    pair = (node, neighbor)
                    if pair == edge or pair == (b, a) or neighbor in reached:
                        continue
                    reached.add(neighbor)
                    queue.append(neighbor)
            return reached

        from_baseline = component_of(baselines)
        from_target = component_of(targets)
        return (a in from_baseline and b in from_target) or (
            b in from_baseline and a in from_target
        )

    def _component_edges(self, component: set[Hashable]) -> list[SharedStrip]:
        return [
            strip
            for strip in self._layout.shared_strips
            if strip.tiles[0] in component and strip.tiles[1] in component
        ]

    def _ordered_edge(self, strip: SharedStrip) -> tuple[Hashable, Hashable]:
        a, b = strip.tiles
        pa = self._layout.tiles[a].position
        pb = self._layout.tiles[b].position
        return (a, b) if pa <= pb else (b, a)

    def _cross_edge(
        self,
        edge: tuple[Hashable, Hashable],
        node: Hashable,
        estimate: _EdgeEstimate,
    ) -> tuple[Hashable | None, float]:
        a, b = edge
        delta = estimate.delta if estimate.delta is not None else 0.0
        if node == a:
            return b, delta
        if node == b:
            return a, -delta
        return None, 0.0

    def _build_conflict(
        self,
        variable: Hashable,
        tile: Hashable,
        distinct: dict[float, tuple[tuple[Hashable, Hashable], Hashable]],
        versions: Mapping[Hashable, int],
    ) -> TileCalibrationConflict:
        ordered = sorted(distinct.items())
        corrections = tuple(value for value, _ in ordered)
        edges = tuple(meta[0] for _, meta in ordered)
        via = tuple(meta[1] for _, meta in ordered)
        tile_versions = tuple(
            tuple((tid, versions[tid]) for tid in edge) for edge in edges
        )
        return TileCalibrationConflict(
            variable=variable,
            tile=tile,
            corrections=corrections,
            via=via,
            edges=edges,
            tile_versions=tile_versions,
        )

    def _assemble(
        self,
        versions: Mapping[Hashable, int],
        x_labels: np.ndarray,
        y_labels: np.ndarray,
        variables: Sequence[Hashable] | None,
        corrections: Mapping[Hashable, Mapping[Hashable, float]],
        apply_correction: bool,
        withheld_variables: set[Hashable] | None = None,
    ) -> Dataset:
        withheld_variables = withheld_variables or set()
        layout = self._layout
        global_x = np.asarray(layout.x_centers, dtype=float)
        global_y = np.asarray(layout.y_centers, dtype=float)
        x_pos = {value: i for i, value in enumerate(global_x)}
        y_pos = {value: i for i, value in enumerate(global_y)}
        window_x_idx = np.array([x_pos[float(v)] for v in x_labels])
        window_y_idx = np.array([y_pos[float(v)] for v in y_labels])

        data_vars: dict[Hashable, DataArray] = {}
        for variable in self._data_variables(variables):
            canvas = np.full((y_labels.size, x_labels.size), np.nan, dtype=float)
            if variable not in withheld_variables:
                variable_corrections = corrections.get(variable, {})
                for tile_id in sorted(versions, key=str):
                    version = versions[tile_id]
                    geometry = self._geometry[(tile_id, version)]
                    in_x = (
                        np.isin(geometry.x_indices, window_x_idx)
                        & geometry.x_internal
                    )
                    in_y = (
                        np.isin(geometry.y_indices, window_y_idx)
                        & geometry.y_internal
                    )
                    if not in_x.any() or not in_y.any():
                        continue
                    tile_x_global = global_x[geometry.x_indices[in_x]]
                    tile_y_global = global_y[geometry.y_indices[in_y]]
                    array = self._load_tile_region(
                        tile_id, version, variable, tile_x_global, tile_y_global
                    )
                    values = np.asarray(array.values, dtype=float)
                    # Corrections apply only to the tile's own internal
                    # readings; shared-strip pixels are never assembled.
                    if apply_correction and tile_id in variable_corrections:
                        values = values + variable_corrections[tile_id]
                    target_x = np.searchsorted(x_labels, tile_x_global)
                    target_y = np.searchsorted(y_labels, tile_y_global)
                    target = canvas[np.ix_(target_y, target_x)]
                    mask = np.isnan(target) & ~np.isnan(values)
                    target[mask] = values[mask]
                    both = ~np.isnan(target) & ~np.isnan(values)
                    target[both] = (target[both] + values[both]) / 2.0
                    canvas[np.ix_(target_y, target_x)] = target
            data_vars[variable] = DataArray(
                canvas,
                dims=(layout.y_dim, layout.x_dim),
                coords={layout.x_dim: x_labels, layout.y_dim: y_labels},
                name=variable,
            )
        return Dataset(data_vars)


def combine_tiled_by_coords(
    tiles: Mapping[Hashable, Dataset] | Sequence[Dataset],
    layout: TileLayout | None = None,
    compat: CompatOptions = "no_conflicts",
    fill_value: object = dtypes.NA,
    join: JoinOptions = "outer",
) -> TileLayoutCombiner | Dataset:
    """Combine tiled observations, optionally driven by an explicit layout.

    Without ``layout`` this is a thin wrapper around
    :py:func:`xarray.combine_by_coords` and keeps its coordinate rules. With a
    :py:class:`TileLayout` it returns a :py:class:`TileLayoutCombiner` that
    supports cropped lazy windows and edge-based calibration of overlapping
    tile strips.

    Parameters
    ----------
    tiles : mapping of tile id to Dataset or sequence of Dataset
        The tile observations. A mapping is required when ``layout`` is given;
        the keys must match the tile ids used in the layout.
    layout : TileLayout, optional
        Whole-image pixel centers, tile extents, shared strips and baseline.
    compat, fill_value, join
        Forwarded to :py:func:`xarray.combine_by_coords` when no layout is
        given.

    Returns
    -------
    TileLayoutCombiner or Dataset
        A lazy combiner when ``layout`` is given, otherwise the ordinary
        combined dataset.
    """
    if layout is None:
        objects = list(tiles.values()) if isinstance(tiles, Mapping) else list(tiles)
        return combine_by_coords(
            objects, compat=compat, fill_value=fill_value, join=join
        )
    if not isinstance(tiles, Mapping):
        raise TypeError(
            "a mapping of tile id to Dataset is required when a layout is provided"
        )
    return TileLayoutCombiner(tiles, layout)
