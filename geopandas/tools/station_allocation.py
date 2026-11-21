"""Capacity constrained nearest assignment of sensors to stations.

This module provides :func:`assign_nearest_stations`, a batch counterpart of
:func:`geopandas.sjoin_nearest` for access-network planning.  A plain nearest
join connects every row to its closest geometry; here a station can only
accept a limited number of sensors, so the closest station may not be
available for every sensor.

Every sensor gets a *fixed candidate band* that is computed before capacity is
considered:

* the band is anchored at the sensor's own nearest *working* station inside
  ``max_distance`` (no capacity competition is involved);
* a station belongs to the band when it is that nearest station or when its
  distance exceeds the anchor by strictly less than ``tolerance``;
* the band never extends beyond ``max_distance``.

Consequently a busy station is never replaced by a more distant station just
to fill the batch, and a station outside ``max_distance`` never becomes a
candidate -- not even a high-priority one.

Within those bands the assignment first maximises the number of connected
sensors.  Among all maximum assignments, sensors are fixed in the caller's
stable sensor order and each sensor takes its most preferred station that
still leaves the global maximum achievable.  Without ``station_priority`` the
preference is the nearest station; with it, the declared priority (lower value
wins) is the primary key and the stable station index label the tie-breaker.
All candidates of one sensor already form a single tolerance tier, so the
declared preference is never overruled by a raw distance difference inside
that tier.

All distances are planar distances in the CRS of the input layers.  A
geographic CRS is rejected because a degree is not a metric distance.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd

import shapely

from geopandas import GeoDataFrame
from geopandas.array import _check_crs, _crs_mismatch_warn

REASON_NO_CANDIDATE = "no_candidate"
REASON_CAPACITY_EXHAUSTED = "capacity_exhausted"


@dataclass
class StationAssignment:
    """Result of :func:`assign_nearest_stations`.

    Attributes
    ----------
    matched : GeoDataFrame
        One row per connected sensor, in the declared stable sensor order.
        It keeps all sensor columns, the selected station's columns (suffixed
        with ``_station`` on collisions), ``sensor_index``, ``station_index``,
        ``sensor_position``, ``station_position``, the assigned ``distance``
        and ``station_geometry``.  The active geometry is the sensor geometry.
    unmatched : GeoDataFrame
        One row per sensor that could not be connected, in stable sensor
        order.  ``reason`` is ``"no_candidate"`` when no working station was
        inside the access distance and ``"capacity_exhausted"`` when the
        sensor had candidates but every slot was taken by higher-ranked
        sensors.  ``nearest_distance`` and ``candidate_count`` allow a direct
        look-up of both situations.
    candidates : DataFrame
        One row per sensor/station pair inside the fixed candidate band, in
        stable sensor order and then station preference order.  Stations
        outside ``max_distance`` never appear.  ``selected`` marks the pairs
        used by the result, so every decision (including rejected candidates)
        can be audited.
    station_usage : DataFrame
        Per-station ``capacity``, ``working``, ``assigned`` and ``remaining``
        slots, in the original station order.
    """

    matched: GeoDataFrame
    unmatched: GeoDataFrame
    candidates: pd.DataFrame
    station_usage: pd.DataFrame


def _as_column(spec, frame, name, *, allow_scalar=False):
    """Resolve a column name or array-like to a one-dimensional numpy array."""
    if spec is None:
        return None
    if isinstance(spec, str):
        if spec not in frame.columns:
            raise KeyError(f"`{name}` column {spec!r} is missing")
        values = frame[spec].to_numpy()
    elif allow_scalar and np.isscalar(spec):
        return np.full(len(frame), spec)
    else:
        values = np.asarray(spec)
    if values.ndim != 1 or len(values) != len(frame):
        raise ValueError(
            f"`{name}` must have one value per row (expected {len(frame)}, "
            f"got shape {values.shape})"
        )
    return values


def _capacity_values(capacity, stations):
    """Resolve capacity to one value per station, including dict lookups."""
    if isinstance(capacity, dict):
        missing = [key for key in stations.index if key not in capacity]
        if missing:
            raise KeyError(
                f"capacity mapping is missing station index labels: {missing[:5]}"
            )
        values = np.array([capacity[key] for key in stations.index], dtype=object)
        return _numeric(values, "capacity", integer=True)
    return _numeric(
        _as_column(capacity, stations, "capacity", allow_scalar=True),
        "capacity",
        integer=True,
    )


def _numeric(values, name, *, integer=False):
    """Coerce values to finite numbers, optionally requiring whole numbers."""
    if values.dtype == object:
        if pd.isna(values).any():
            raise ValueError(f"`{name}` must not contain missing values")
        try:
            values = pd.to_numeric(pd.Series(values), errors="raise").to_numpy()
        except (TypeError, ValueError) as err:
            raise TypeError(f"`{name}` must be numeric") from err
    if not np.issubdtype(values.dtype, np.number):
        raise TypeError(f"`{name}` must be numeric")
    numeric = values.astype(float)
    if not np.isfinite(numeric).all():
        raise ValueError(f"`{name}` must not contain missing or infinite values")
    if integer:
        if (numeric < 0).any():
            raise ValueError(f"`{name}` must be non-negative")
        if not np.equal(numeric, np.floor(numeric)).all():
            raise ValueError(f"`{name}` must contain whole numbers")
        return numeric.astype(np.intp)
    return numeric


def _stable_rank(values):
    """Rank labels in ascending order, collapsing equal labels to one rank."""
    codes = pd.factorize(pd.Series(values), sort=True)[0]
    return codes.astype(float)


def _sensor_positions(sensors, sensor_order):
    """Return sensor positions in the caller's declared stable order.

    The declared order is the primary key.  Equal declarations fall back to
    the sensor index labels and finally to the original row position, so the
    order never depends on the input row order alone.
    """
    n_sensors = len(sensors)
    if sensor_order is None:
        order_values = sensors.index.to_numpy()
    else:
        order_values = _as_column(sensor_order, sensors, "sensor_order")
    if pd.isna(order_values).any():
        raise ValueError("`sensor_order` must not contain missing values")
    order_rank = _stable_rank(order_values)
    index_rank = _stable_rank(sensors.index.to_numpy())
    position_rank = np.arange(n_sensors, dtype=float)
    return np.lexsort((position_rank, index_rank, order_rank)).astype(np.intp)


def _station_preference(stations, station_priority):
    """Return station positions from most to least preferred.

    A lower priority value wins; equal priorities fall back to the stable
    station index label and then to the original row position.  Without an
    explicit priority, the caller's stable station index label is the order;
    the actual choice is distance-first (see :func:`_assign`).
    """
    n_stations = len(stations)
    if station_priority is None:
        priority = None
    else:
        raw_priority = _as_column(station_priority, stations, "station_priority")
        if pd.isna(raw_priority).any():
            raise ValueError("`station_priority` must not contain missing values")
        try:
            priority = raw_priority.astype(float)
        except (TypeError, ValueError):
            priority = _stable_rank(raw_priority)
    index_rank = _stable_rank(stations.index.to_numpy())
    position_rank = np.arange(n_stations, dtype=float)
    if priority is None:
        preference = np.lexsort((position_rank, index_rank))
    else:
        preference = np.lexsort((position_rank, index_rank, priority))
    return preference.astype(np.intp), priority


def _usable_stations(stations, working):
    """Return a boolean mask of stations that can accept sensors at all."""
    if working is None:
        return np.ones(len(stations), dtype=bool)
    values = _as_column(working, stations, "working")
    if values.dtype == bool:
        return values
    truthy = {True, "1", "true", "True", "yes", "Y", "y"}
    return np.array([value in truthy for value in values], dtype=bool)


def _candidate_bands(
    sensor_geometry, station_geometry, usable, max_distance, tolerance
):
    """Compute each sensor's fixed nearest-plus-tolerance candidate band.

    Returns the anchor distance per sensor (NaN when no usable station is
    inside ``max_distance``) and a list of ``(station_pos, distance)`` pairs
    per sensor.  Capacity is deliberately absent from this step.
    """
    n_sensors = len(sensor_geometry)
    adjacency = [[] for _ in range(n_sensors)]
    no_anchor = np.full(n_sensors, np.nan)
    usable_positions = np.flatnonzero(usable)
    if not n_sensors or not len(usable_positions):
        return no_anchor, adjacency

    usable_geometry = station_geometry.take(usable_positions)
    sindex = usable_geometry.sindex
    if not sindex:
        return no_anchor, adjacency

    nearest = sindex.nearest(
        sensor_geometry,
        return_all=True,
        max_distance=max_distance,
        return_distance=True,
    )
    nearest_sensor, _nearest_station = nearest[0]
    nearest_distance = nearest[1]
    if not len(nearest_sensor):
        return no_anchor, adjacency

    anchor = np.full(n_sensors, np.inf)
    np.minimum.at(anchor, nearest_sensor, nearest_distance)
    has_anchor = np.isfinite(anchor)

    station_values = usable_geometry.values
    for sensor_pos in np.flatnonzero(has_anchor):
        anchor_distance = anchor[sensor_pos]
        band = anchor_distance + tolerance
        if max_distance is not None:
            band = min(band, max_distance)
        query = sindex.query(
            sensor_geometry.take([sensor_pos]),
            predicate="dwithin",
            distance=float(np.nextafter(band, np.inf)),
        )
        _input_idx, station_idx = query
        station_idx = np.asarray(station_idx, dtype=np.intp)
        if not len(station_idx):
            continue
        distances = shapely.distance(
            np.repeat(sensor_geometry.take([sensor_pos]).values, len(station_idx)),
            station_values.take(station_idx),
        )
        difference = distances - anchor_distance
        keep = (difference <= 0) | (difference < tolerance)
        if max_distance is not None:
            keep &= distances <= max_distance
        for position, distance in zip(station_idx[keep], distances[keep], strict=True):
            adjacency[int(sensor_pos)].append(
                (int(usable_positions[position]), float(distance))
            )

    anchor[~has_anchor] = np.nan
    return anchor, adjacency


class _Dinic:
    """Dinic max-flow for the capacitated bipartite feasibility checks."""

    def __init__(self, n_nodes):
        self.graph = [[] for _ in range(n_nodes)]

    def add_edge(self, source, target, capacity):
        self.graph[source].append([target, capacity, len(self.graph[target])])
        self.graph[target].append([source, 0, len(self.graph[source]) - 1])

    def _bfs(self, source, sink):
        level = [-1] * len(self.graph)
        level[source] = 0
        queue = [source]
        for node in queue:
            for target, capacity, _reverse in self.graph[node]:
                if capacity > 0 and level[target] < 0:
                    level[target] = level[node] + 1
                    queue.append(target)
        self.level = level
        return level[sink] >= 0

    def _dfs(self, node, sink, flow):
        if node == sink:
            return flow
        while self.iterator[node] < len(self.graph[node]):
            edge = self.graph[node][self.iterator[node]]
            target, capacity, reverse = edge
            if capacity > 0 and self.level[target] == self.level[node] + 1:
                pushed = self._dfs(target, sink, min(flow, capacity))
                if pushed:
                    edge[1] -= pushed
                    self.graph[target][reverse][1] += pushed
                    return pushed
            self.iterator[node] += 1
        return 0

    def max_flow(self, source, sink):
        flow = 0
        while self._bfs(source, sink):
            self.iterator = [0] * len(self.graph)
            while True:
                pushed = self._dfs(source, sink, np.iinfo(np.int64).max)
                if not pushed:
                    break
                flow += pushed
        return flow


def _max_cardinality(rows, adjacency, capacities):
    """Maximum number of sensors in ``rows`` assignable with ``capacities``."""
    n_sensors = len(adjacency)
    n_stations = len(capacities)
    source = n_sensors + n_stations
    sink = source + 1
    flow = _Dinic(sink + 1)
    for sensor_pos in rows:
        if not adjacency[sensor_pos]:
            continue
        flow.add_edge(source, sensor_pos, 1)
        for station_pos, _distance in adjacency[sensor_pos]:
            flow.add_edge(sensor_pos, n_sensors + station_pos, 1)
    for station_pos, capacity in enumerate(capacities):
        if capacity > 0:
            flow.add_edge(n_sensors + station_pos, sink, int(capacity))
    return flow.max_flow(source, sink)


def _assign(adjacency, capacities, sensor_positions, station_preference, priorities):
    """Lexicographically best maximum-cardinality capacitated assignment.

    Every candidate in a sensor's band lies within ``tolerance`` of that
    sensor's own nearest distance, so all of them belong to the same distance
    tier.  The preferred station inside that tier is therefore the declared
    priority (lower wins) and then the stable station number -- raw distance is
    deliberately not used to overrule the declared preference.
    """
    n_sensors = len(adjacency)
    preference_rank = np.empty(len(capacities), dtype=np.intp)
    preference_rank[station_preference] = np.arange(len(capacities), dtype=np.intp)

    if priorities is None:
        ordered_candidates = [
            sorted(candidates, key=lambda item: (item[1], preference_rank[item[0]]))
            for candidates in adjacency
        ]
    else:
        ordered_candidates = [
            sorted(
                candidates,
                key=lambda item: (priorities[item[0]], preference_rank[item[0]]),
            )
            for candidates in adjacency
        ]

    active = [position for position in range(n_sensors) if adjacency[position]]
    maximum = _max_cardinality(active, adjacency, capacities)

    assigned = np.full(n_sensors, -1, dtype=np.intp)
    distances = np.full(n_sensors, np.nan)
    remaining = capacities.copy()
    unprocessed = set(active)
    fixed = 0

    for sensor_pos in sensor_positions:
        sensor_pos = int(sensor_pos)
        unprocessed.discard(sensor_pos)
        for station_pos, distance in ordered_candidates[sensor_pos]:
            if remaining[station_pos] <= 0:
                continue
            remaining[station_pos] -= 1
            reachable = _max_cardinality(unprocessed, adjacency, remaining)
            if fixed + 1 + reachable >= maximum:
                assigned[sensor_pos] = station_pos
                distances[sensor_pos] = distance
                fixed += 1
                break
            remaining[station_pos] += 1
    return assigned, distances


def _role_tokens(stations, tokens):
    return any(
        any(token in str(column).lower() for token in tokens)
        for column in stations.columns
        if column != stations.geometry.name
    )


def _guess_station_side(first, second):
    """Best-effort role detection from attribute names when no column hints."""
    station_tokens = ("station", "base", "site", "tower")
    sensor_tokens = ("sensor", "device", "terminal", "meter")
    first_station = _role_tokens(first, station_tokens)
    second_station = _role_tokens(second, station_tokens)
    first_sensor = _role_tokens(first, sensor_tokens)
    second_sensor = _role_tokens(second, sensor_tokens)
    if first_station and second_sensor and not second_station:
        return 0
    if second_station and first_sensor and not first_station:
        return 1
    if first_station and not second_station:
        return 0
    if second_station and not first_station:
        return 1
    return None


def _resolve_roles(
    first, second, capacity, working, station_priority, sensor_order, station_side
):
    """Detect which input is the sensor table and which is the station table.

    Column-name hints are authoritative, which makes swapping the two input
    frames safe.  ``station_side`` may force a side when only scalar arguments
    are used.  Without hints or an explicit side the first frame is the sensor
    table.
    """
    if station_side not in ("auto", "first", "second"):
        raise ValueError("`station_side` must be 'auto', 'first' or 'second'")

    station_hints = set()
    sensor_hints = set()
    specs = (
        ("capacity", capacity, station_hints),
        ("working", working, station_hints),
        ("station_priority", station_priority, station_hints),
        ("sensor_order", sensor_order, sensor_hints),
    )
    for name, spec, hints in specs:
        if not isinstance(spec, str):
            continue
        in_first = spec in first.columns
        in_second = spec in second.columns
        if in_first and in_second:
            raise ValueError(
                f"`{name}` column {spec!r} exists in both frames; rename one of them"
            )
        if not in_first and not in_second:
            raise KeyError(f"`{name}` column {spec!r} is missing from both frames")
        hints.add(0 if in_first else 1)

    hinted_side = None
    if station_hints and sensor_hints:
        if station_hints == sensor_hints:
            raise ValueError(
                "the station-side and sensor-side columns both point at the same frame"
            )
        hinted_side = station_hints.pop()
    elif station_hints:
        hinted_side = station_hints.pop()
    elif sensor_hints:
        hinted_side = 1 - sensor_hints.pop()

    explicit_side = {"auto": None, "first": 0, "second": 1}[station_side]
    if hinted_side is None and explicit_side is None:
        hinted_side = _guess_station_side(first, second)
    if hinted_side is not None and explicit_side is not None:
        if hinted_side != explicit_side:
            raise ValueError(
                "`station_side` conflicts with the side of a named capacity, "
                "working, station_priority or sensor_order column"
            )
    station_side_index = explicit_side if explicit_side is not None else hinted_side
    if station_side_index is None:
        station_side_index = 1

    if station_side_index == 1:
        return first, second
    return second, first


def assign_nearest_stations(
    sensors,
    stations,
    *,
    capacity,
    max_distance=None,
    tolerance=0.0,
    sensor_order=None,
    station_priority=None,
    working=None,
    distance_col="distance",
    station_side="auto",
):
    """Assign each sensor to a working station within its own candidate band.

    For every sensor the candidate band is anchored at that sensor's nearest
    usable station inside ``max_distance`` and widened by ``tolerance``.  The
    band is fixed before capacity is considered, so a station that is already
    full is never replaced by a more distant station, and a station outside
    ``max_distance`` never becomes a candidate.

    The assignment first maximises the number of connected sensors.  Among all
    maximum assignments, sensors are fixed in the declared stable order and
    each sensor takes its most preferred station that still leaves the global
    maximum achievable.  Without ``station_priority`` the nearest station is
    preferred; with it, the declared priority (lower wins) is the primary key
    and the stable station index label the tie-breaker.

    Parameters
    ----------
    sensors, stations : GeoDataFrame
        The two input layers.  They may be passed in either order when the
        ``capacity``, ``working``, ``station_priority`` or ``sensor_order``
        arguments name columns: the side of those columns identifies the
        corresponding layer.  Both layers must share a projected (metric) CRS.
    capacity : str, int, dict or array-like
        Number of sensors a station can accept.  A column name in the station
        frame, a non-negative integer applied to every station, a mapping from
        station index label to capacity, or one value per station.  A station
        with capacity 0 stays in the candidate band (so the failure is
        reported as ``capacity_exhausted``) but never accepts a sensor.
    max_distance : float, optional
        Maximum access distance.  Stations farther away never become
        candidates.  The limit is inclusive.
    tolerance : float, default 0.0
        Distance difference (in CRS units) treated as the same tier.  A
        station belongs to a sensor's band when its distance exceeds the
        sensor's own nearest distance by *strictly less* than ``tolerance``.
        The resulting band is additionally capped by ``max_distance``.
    sensor_order : str or array-like, optional
        Stable sensor number.  Sensors are processed in ascending order;
        input row order is not used as a tie-breaker until the declared order
        and index labels are exhausted.  Defaults to the sensor index labels.
    station_priority : str or array-like, optional
        Station priority; lower values are preferred, and categorical values
        are ranked in ascending order.  Ties are broken by the stable station
        index label.  Without it the nearest station is preferred.
    working : str or array-like, optional
        Boolean mask or column marking stations that can accept sensors.
        Non-working stations are excluded from candidate selection and from
        the nearest-distance baseline.
    distance_col : str, default "distance"
        Name of the distance column in ``matched`` and ``candidates``.
    station_side : {"auto", "first", "second"}, default "auto"
        Which argument is the station table.  With ``"auto"`` the side is
        inferred from the placement of named columns or from attribute names
        such as ``station_id`` / ``sensor_id``; otherwise the second argument
        is used.  Set it explicitly when only scalar arguments are supplied.

    Returns
    -------
    StationAssignment
        ``matched``, ``unmatched``, ``candidates`` and ``station_usage``
        frames.

    Examples
    --------
    >>> import geopandas as gpd
    >>> from shapely.geometry import Point
    >>> sensors = gpd.GeoDataFrame(
    ...     {"sensor_order": [1, 2]},
    ...     geometry=[Point(0, 0), Point(2, 0)],
    ...     crs="EPSG:3857",
    ... )
    >>> stations = gpd.GeoDataFrame(
    ...     {"capacity": [1, 1]},
    ...     geometry=[Point(1, 0), Point(1.4, 0)],
    ...     crs="EPSG:3857",
    ... )
    >>> result = assign_nearest_stations(
    ...     sensors,
    ...     stations,
    ...     capacity="capacity",
    ...     max_distance=1.5,
    ...     tolerance=0.5,
    ...     sensor_order="sensor_order",
    ... )
    >>> result.matched[["sensor_index", "station_index", "distance"]]
       sensor_index  station_index  distance
    0             0              0       1.0
    1             1              1       0.6
    """
    if not isinstance(sensors, GeoDataFrame):
        raise TypeError("`sensors` must be a GeoDataFrame")
    if not isinstance(stations, GeoDataFrame):
        raise TypeError("`stations` must be a GeoDataFrame")

    sensors, stations = _resolve_roles(
        sensors,
        stations,
        capacity,
        working,
        station_priority,
        sensor_order,
        station_side,
    )

    for name, frame in (("sensors", sensors), ("stations", stations)):
        if frame.crs is not None and frame.crs.is_geographic:
            raise ValueError(
                f"`{name}` must use a projected (metric) CRS; got "
                f"{frame.crs}. Reproject the layer before assigning stations."
            )
    if not _check_crs(sensors, stations):
        _crs_mismatch_warn(sensors, stations, stacklevel=2)

    if max_distance is not None:
        max_distance = float(max_distance)
        if not np.isfinite(max_distance) or max_distance <= 0:
            raise ValueError("`max_distance` must be a positive number")
    tolerance = float(tolerance)
    if not np.isfinite(tolerance) or tolerance < 0:
        raise ValueError("`tolerance` must be a finite, non-negative number")

    capacities = _capacity_values(capacity, stations)
    working_mask = _usable_stations(stations, working)

    sensor_positions = _sensor_positions(sensors, sensor_order)
    station_preference, priority = _station_preference(stations, station_priority)

    anchor, adjacency = _candidate_bands(
        sensors.geometry,
        stations.geometry,
        working_mask,
        max_distance,
        tolerance,
    )
    assigned, distances = _assign(
        adjacency,
        capacities,
        sensor_positions,
        station_preference,
        None if station_priority is None else priority,
    )

    matched = _matched_frame(
        sensors,
        stations,
        assigned,
        distances,
        priority,
        sensor_positions,
        distance_col,
    )
    unmatched = _unmatched_frame(sensors, anchor, adjacency, assigned, sensor_positions)
    preference_rank = np.empty(len(stations), dtype=np.intp)
    preference_rank[station_preference] = np.arange(len(stations), dtype=np.intp)
    candidates = _candidate_frame(
        sensors,
        stations,
        anchor,
        adjacency,
        assigned,
        priority,
        preference_rank,
        sensor_positions,
        distance_col,
    )
    station_usage = pd.DataFrame(
        {
            "station_index": stations.index.to_numpy(),
            "station_id": _station_id(stations),
            "capacity": capacities,
            "working": working_mask,
            "assigned": np.bincount(assigned[assigned >= 0], minlength=len(stations)),
        }
    )
    station_usage["remaining"] = np.where(
        station_usage["working"],
        station_usage["capacity"] - station_usage["assigned"],
        0,
    )
    station_usage.insert(0, "station_position", np.arange(len(stations)))

    return StationAssignment(
        matched=matched,
        unmatched=unmatched,
        candidates=candidates,
        station_usage=station_usage,
    )


def _station_id(stations):
    """Return a display identifier for stations.

    An explicit ``station_id`` column wins; otherwise the first non-geometry
    column is used.  The index label is always available as ``station_index``
    regardless of this helper.
    """
    if "station_id" in stations.columns:
        return stations["station_id"].to_numpy()
    for column in stations.columns:
        if column != stations.geometry.name:
            return stations[column].to_numpy()
    return stations.index.to_numpy()


def _matched_frame(
    sensors,
    stations,
    assigned,
    distances,
    priority,
    sensor_positions,
    distance_col,
):
    """Build the matched GeoDataFrame in stable sensor order."""
    sensor_positions = sensor_positions[assigned[sensor_positions] >= 0]
    station_positions = assigned[sensor_positions]

    sensor_part = sensors.iloc[sensor_positions].copy().reset_index(drop=True)
    station_columns = [
        column for column in stations.columns if column != stations.geometry.name
    ]
    station_part = stations.iloc[station_positions][station_columns]
    station_part = station_part.reset_index(drop=True)
    reserved = {
        "sensor_index",
        "station_index",
        "sensor_position",
        "station_position",
        distance_col,
        "station_priority",
        "station_geometry",
    }
    station_part.columns = [
        f"{column}_station"
        if column in sensor_part.columns or column in reserved
        else column
        for column in station_part.columns
    ]

    matched = pd.concat([sensor_part, station_part], axis=1)
    matched["sensor_index"] = sensors.index.to_numpy()[sensor_positions]
    matched["station_index"] = stations.index.to_numpy()[station_positions]
    matched["sensor_position"] = sensor_positions
    matched["station_position"] = station_positions
    matched[distance_col] = distances[sensor_positions]
    if priority is not None:
        matched["station_priority"] = priority[station_positions]
    matched = GeoDataFrame(
        matched,
        geometry=sensors.geometry.take(sensor_positions).values,
        crs=sensors.crs,
    )
    matched["station_geometry"] = stations.geometry.take(station_positions).values
    return matched


def _unmatched_frame(sensors, anchor, adjacency, assigned, sensor_positions):
    """Build the unmatched frame with a separate capacity/no-station reason."""
    unmatched_positions = sensor_positions[assigned[sensor_positions] < 0]
    unmatched = sensors.iloc[unmatched_positions].copy()
    order_rank = np.empty(len(sensors), dtype=np.intp)
    order_rank[sensor_positions] = np.arange(len(sensors), dtype=np.intp)
    unmatched["reason"] = [
        REASON_NO_CANDIDATE if np.isnan(anchor[position]) else REASON_CAPACITY_EXHAUSTED
        for position in unmatched_positions
    ]
    unmatched["nearest_distance"] = anchor[unmatched_positions]
    unmatched["candidate_count"] = [
        len(adjacency[int(position)]) for position in unmatched_positions
    ]
    unmatched["sensor_position"] = unmatched_positions
    unmatched["sensor_order"] = order_rank[unmatched_positions]
    return unmatched


def _candidate_frame(
    sensors,
    stations,
    anchor,
    adjacency,
    assigned,
    priority,
    preference_rank,
    sensor_positions,
    distance_col,
):
    """Build the long candidate audit frame in stable sensor order."""
    selected = set(
        zip(
            np.flatnonzero(assigned >= 0).tolist(),
            assigned[assigned >= 0].tolist(),
        )
    )
    order_rank = np.empty(len(sensors), dtype=np.intp)
    order_rank[sensor_positions] = np.arange(len(sensors), dtype=np.intp)
    station_ids = _station_id(stations)
    rows = []
    for sensor_pos in sensor_positions:
        sensor_pos = int(sensor_pos)
        if np.isnan(anchor[sensor_pos]):
            continue
        if priority is None:
            ordered = sorted(
                adjacency[sensor_pos],
                key=lambda item: (item[1], preference_rank[item[0]]),
            )
        else:
            ordered = sorted(
                adjacency[sensor_pos],
                key=lambda item: (priority[item[0]], preference_rank[item[0]]),
            )
        for station_pos, distance in ordered:
            rows.append(
                {
                    "sensor_index": sensors.index.to_numpy()[sensor_pos],
                    "station_index": stations.index.to_numpy()[station_pos],
                    "station_id": station_ids[station_pos],
                    distance_col: distance,
                    "nearest_distance": anchor[sensor_pos],
                    "distance_difference": distance - anchor[sensor_pos],
                    "station_priority": (
                        None if priority is None else priority[station_pos]
                    ),
                    "selected": (sensor_pos, int(station_pos)) in selected,
                    "sensor_order": order_rank[sensor_pos],
                    "sensor_position": sensor_pos,
                    "station_position": int(station_pos),
                }
            )
    columns = [
        "sensor_index",
        "station_index",
        "station_id",
        distance_col,
        "nearest_distance",
        "distance_difference",
        "station_priority",
        "selected",
        "sensor_order",
        "sensor_position",
        "station_position",
    ]
    return pd.DataFrame(rows, columns=columns)
