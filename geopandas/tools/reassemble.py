"""Merge reviewed single-part geometries back into multi-part geometries."""

import pandas as pd

from geopandas import GeoDataFrame
from geopandas.array import from_shapely

__all__ = ["ReassemblyResult", "reassemble_parts"]


def _geoms_equal(left, right):
    """Check two returned parts for identical content."""
    if left is None or right is None:
        return left is None and right is None
    if left.is_empty or right.is_empty:
        return left.is_empty and right.is_empty and left.geom_type == right.geom_type
    return left.equals(right)


class ReassemblyResult:
    """Outcome of :func:`reassemble_parts`.

    Attributes
    ----------
    facilities : GeoDataFrame
        One row per source facility (and revision), in the original row
        order and with the original attributes, index and CRS. The geometry
        column holds the reassembled multi-part geometry for fully reviewed
        facilities and the partially reassembled geometry for facilities
        with missing or contradictory returns. The ``reassembled`` column
        flags the fully reassembled facilities.
    conflicts : GeoDataFrame
        The returns that contradict each other for the same part of the
        same facility revision.
    missing : DataFrame
        The parts that were expected but never returned.
    deleted : DataFrame
        The parts that were explicitly returned as deleted (missing
        geometry), as opposed to not returned at all.
    """

    def __init__(self, facilities, conflicts, missing, deleted):
        self.facilities = facilities
        self.conflicts = conflicts
        self.missing = missing
        self.deleted = deleted

    @property
    def complete(self):
        """The facilities for which every part was resolved."""
        return self.facilities[self.facilities["reassembled"]]

    @property
    def incomplete(self):
        """The facilities with missing or contradictory returns."""
        return self.facilities[~self.facilities["reassembled"]]

    def __repr__(self):
        n_complete = int(self.facilities["reassembled"].sum())
        return (
            f"<ReassemblyResult: {n_complete} of {len(self.facilities)} "
            f"facilities reassembled, {len(self.conflicts)} conflicting "
            f"returns, {len(self.missing)} missing parts, "
            f"{len(self.deleted)} deleted parts>"
        )


def reassemble_parts(source, returns, by, version=None, part=None):
    """Merge reviewed geometry parts back into one geometry per facility.

    Facilities with multi-part geometries (such as inspection routes stored
    as MultiLineStrings) are often exploded into single-part geometries that
    are handed out to review batches and come back edited, deleted, or not
    at all. This function merges such review returns back into one geometry
    per facility (and revision), keyed by a stable business identifier and
    the zero-based position of each part within its original multi-part
    geometry. Returns are matched to parts by key and position only, so
    edited coordinates, identical parts, and out-of-order or duplicate
    returns never move a part away from its original position.

    .. versionadded:: 1.3

    Parameters
    ----------
    source : GeoDataFrame
        One row per facility (and revision) holding the original
        geometries. Row order, attributes, index and CRS are carried over
        to the result. Rows with empty or missing geometry are restored
        as-is.
    returns : GeoDataFrame
        The reviewed parts. Each row carries the business identifier, the
        revision (if ``version`` is given) and the zero-based part position,
        either in the column named by ``part`` or in the last level of a
        multi-index as produced by ``GeoDataFrame.explode(index_parts=True)``.
        A return with missing geometry marks the part as explicitly deleted,
        which is reported separately from parts that were never returned.
        Identical duplicate returns are merged; contradictory returns for
        the same part are reported as conflicts and leave the facility
        incomplete.
    by : str
        Column with the stable business identifier of the facility, present
        in both ``source`` and ``returns``. Facilities are matched on this
        identifier (and ``version``), never on the DataFrame index, so
        facilities sharing index labels stay separate.
    version : str, default None
        Optional column with the revision identifier, present in both
        ``source`` and ``returns``. Parts are only merged within the same
        revision: returns of one revision never complete another revision
        of the same facility.
    part : str, default None
        Column in ``returns`` with the zero-based position of each part
        within its original multi-part geometry. If None, the position is
        taken from the last level of the index of ``returns``.

    Returns
    -------
    ReassemblyResult
        The reassembled facilities together with the conflicting, missing
        and explicitly deleted parts. Facilities with missing or
        contradictory returns are flagged as not ``reassembled`` and keep
        the partially reassembled geometry of the parts that were resolved.

    See Also
    --------
    GeoDataFrame.explode : explode multi-part geometries into single parts.
    GeoDataFrame.dissolve : dissolve geometries into a single observation.

    Examples
    --------
    >>> import geopandas
    >>> from geopandas.tools import reassemble_parts
    >>> from shapely.geometry import LineString, MultiLineString

    >>> source = geopandas.GeoDataFrame(
    ...     {
    ...         "facility": ["a", "b"],
    ...         "geometry": [
    ...             MultiLineString([[(0, 0), (1, 1)], [(2, 2), (3, 3)]]),
    ...             MultiLineString([[(4, 4), (5, 5)]]),
    ...         ],
    ...     },
    ...     crs=4326,
    ... )

    Explode the multi-part lines and hand the parts out for review; the
    business identifier travels with every part:

    >>> returns = source.explode(index_parts=True)
    >>> returns["geometry"] = [
    ...     LineString([(0, 0), (1, 1)]),
    ...     LineString([(2, 2), (9, 9)]),
    ...     LineString([(4, 4), (5, 5)]),
    ... ]

    Merge the reviewed parts back, one geometry per facility:

    >>> result = reassemble_parts(source, returns, by="facility")
    >>> result.facilities["reassembled"].tolist()
    [True, True]
    >>> result.facilities.geometry.iloc[0].wkt
    'MULTILINESTRING ((0 0, 1 1), (2 2, 9 9))'
    """
    if not isinstance(source, GeoDataFrame):
        raise TypeError("'source' must be a GeoDataFrame")
    if not isinstance(returns, GeoDataFrame):
        raise TypeError("'returns' must be a GeoDataFrame")
    for frame, name in ((source, "source"), (returns, "returns")):
        if by not in frame.columns:
            raise ValueError(f"Column {by!r} not found in {name}")
        if version is not None and version not in frame.columns:
            raise ValueError(f"Column {version!r} not found in {name}")

    if part is None:
        if not isinstance(returns.index, pd.MultiIndex):
            raise ValueError(
                "Pass the column holding the part positions via 'part' or use "
                "a multi-indexed 'returns' frame as produced by "
                "GeoDataFrame.explode(index_parts=True)"
            )
        part_values = list(returns.index.get_level_values(-1))
        part_name = returns.index.names[-1]
        if part_name is None:
            part_name = "part"
    else:
        if part not in returns.columns:
            raise ValueError(f"Column {part!r} not found in returns")
        part_values = list(returns[part])
        part_name = part

    return_versions = [None] * len(returns)
    if version is not None:
        return_versions = list(returns[version])
    return_keys = list(zip(returns[by], return_versions))
    return_geoms = list(returns.geometry)

    groups = {}
    for pos, (key, position) in enumerate(zip(return_keys, part_values)):
        groups.setdefault(key, {}).setdefault(position, []).append(pos)

    resolved = {}
    conflicted = {}
    conflict_positions = []
    for key, parts_by_position in groups.items():
        for position, positions in parts_by_position.items():
            geoms = [return_geoms[pos] for pos in positions]
            first = geoms[0]
            if all(_geoms_equal(first, other) for other in geoms[1:]):
                resolved.setdefault(key, {})[position] = first
            else:
                conflicted.setdefault(key, set()).add(position)
                conflict_positions.extend(positions)

    source_versions = [None] * len(source)
    if version is not None:
        source_versions = list(source[version])
    source_keys = list(zip(source[by], source_versions))
    source_geoms = list(source.geometry)

    report_columns = [by]
    if version is not None:
        report_columns.append(version)
    report_columns.append(part_name)

    new_geoms = []
    flags = []
    missing_records = []
    deleted_records = []

    for key, geom in zip(source_keys, source_geoms):
        if geom is None:
            new_geoms.append(None)
            flags.append(True)
            continue
        if geom.is_empty:
            new_geoms.append(geom)
            flags.append(True)
            continue

        parts_resolved = resolved.get(key, {})
        parts_conflicted = conflicted.get(key, set())
        if geom.geom_type.startswith("Multi"):
            n_parts = len(geom.geoms)
            multi = True
        else:
            n_parts = 1
            multi = False

        record = {by: key[0]}
        if version is not None:
            record[version] = key[1]

        missing_positions = [
            position
            for position in range(n_parts)
            if position not in parts_resolved and position not in parts_conflicted
        ]
        for position in missing_positions:
            missing_records.append({**record, part_name: position})
        for position in range(n_parts):
            if position in parts_resolved and parts_resolved[position] is None:
                deleted_records.append({**record, part_name: position})

        kept = [
            parts_resolved[position]
            for position in range(n_parts)
            if position in parts_resolved and parts_resolved[position] is not None
        ]
        if multi:
            new_geoms.append(type(geom)(kept))
        elif kept:
            new_geoms.append(kept[0])
        else:
            new_geoms.append(type(geom)())
        flags.append(not missing_positions and not parts_conflicted)

    facilities = source.copy()
    facilities[source.geometry.name] = from_shapely(new_geoms, crs=source.crs)
    facilities["reassembled"] = flags

    conflicts = returns.iloc[sorted(conflict_positions)].copy()
    missing = pd.DataFrame(missing_records, columns=report_columns)
    deleted = pd.DataFrame(deleted_records, columns=report_columns)

    return ReassemblyResult(facilities, conflicts, missing, deleted)
