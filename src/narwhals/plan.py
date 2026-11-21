"""Schema-only join planning with column provenance.

`NamePlan` mirrors a chain of `join`/`select`/`rename` operations using only
table names and column names, so the output columns of a multi-join report -
and the original table/column each of them comes from - can be inspected
before any data is read.
"""

# narwhals-canary: delete this line to confirm you have read the full diff
from __future__ import annotations

from typing import TYPE_CHECKING, Literal, NamedTuple

from narwhals.exceptions import (
    ColumnMappingConflictError,
    ColumnNotFoundError,
    DuplicateError,
)

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence


__all__ = ["ColumnSource", "NamePlan", "PlannedFrame"]

_PlannedJoinStrategy = Literal["inner", "left", "full"]
_SUPPORTED_JOINS: tuple[_PlannedJoinStrategy, ...] = ("inner", "left", "full")


class ColumnSource(NamedTuple):
    """Original table and column a planned output column comes from."""

    table: str
    column: str


class PlannedFrame:
    """Handle to a named frame registered in a [`NamePlan`][narwhals.plan.NamePlan].

    Instances are created by `NamePlan` methods and can be passed back to them
    in place of the frame's name.
    """

    __slots__ = ("_name", "_plan")

    def __init__(self, plan: NamePlan, name: str) -> None:
        self._plan = plan
        self._name = name

    @property
    def name(self) -> str:
        """Name of this frame within the plan."""
        return self._name

    @property
    def columns(self) -> list[str]:
        """Planned output column names, in order."""
        return self._plan.columns(self)

    @property
    def sources(self) -> dict[str, ColumnSource]:
        """Mapping of each output column name to its original table and column."""
        return self._plan.sources(self)

    def source_of(self, column: str) -> ColumnSource:
        """Return the original table and column behind `column`."""
        return self._plan.source_of(self, column)


class NamePlan:
    """Plan output column names and provenance for a chain of joins.

    The plan works purely on column names, so it can be built and inspected
    before any data is read. Provenance is tracked by column identity, not by
    display name: a name freed by a `rename` or dropped by a `select` may later
    be reused by a different column, and `source_of` always resolves it to the
    column that actually carries it at that point in the plan.

    Examples:
        >>> import narwhals as nw
        >>> plan = nw.plan.NamePlan()
        >>> _ = plan.add_table("readings", ["device_id", "unit", "value"])
        >>> _ = plan.add_table("catalog", ["device_key", "unit"])
        >>> step = plan.join(
        ...     "readings",
        ...     "catalog",
        ...     how="full",
        ...     left_on="device_id",
        ...     right_on="device_key",
        ...     rename_left={"unit": "reading_unit"},
        ...     rename_right={"unit": "catalog_unit"},
        ... )
        >>> step.columns
        ['device_id', 'reading_unit', 'value', 'device_key', 'catalog_unit']
        >>> step.source_of("reading_unit")
        ColumnSource(table='readings', column='unit')
    """

    __slots__ = ("_counter", "_frames")

    def __init__(self) -> None:
        self._frames: dict[str, dict[str, ColumnSource]] = {}
        self._counter = 0

    def _new_name(self) -> str:
        self._counter += 1
        return f"step_{self._counter}"

    def _frame_name(self, frame: str | PlannedFrame) -> str:
        if isinstance(frame, PlannedFrame):
            if frame._plan is not self:
                msg = f"Frame {frame.name!r} belongs to a different `NamePlan`."
                raise ValueError(msg)
            return frame.name
        return frame

    def _get(self, frame: str | PlannedFrame) -> dict[str, ColumnSource]:
        name = self._frame_name(frame)
        if name not in self._frames:
            msg = f"Unknown frame {name!r}; available frames: {sorted(self._frames)}."
            raise ValueError(msg)
        return self._frames[name]

    def _register(
        self, sources: dict[str, ColumnSource], name: str | None
    ) -> PlannedFrame:
        frame_name = name if name is not None else self._new_name()
        if frame_name in self._frames:
            msg = f"A frame named {frame_name!r} is already registered in this plan."
            raise ValueError(msg)
        self._frames[frame_name] = sources
        return PlannedFrame(self, frame_name)

    @staticmethod
    def _renamed(
        sources: dict[str, ColumnSource], mapping: Mapping[str, str]
    ) -> dict[str, ColumnSource]:
        if missing := [column for column in mapping if column not in sources]:
            raise ColumnNotFoundError.from_missing_and_available_column_names(
                missing, sources
            )
        result: dict[str, ColumnSource] = {}
        for column, source in sources.items():
            new_name = mapping.get(column, column)
            if new_name in result:
                _raise_mapping_conflict(new_name, result[new_name], source)
            result[new_name] = source
        return result

    def add_table(self, name: str, columns: str | Sequence[str]) -> PlannedFrame:
        """Register a source table by name and column names.

        Arguments:
            name: Name to register the table under.
            columns: Column names of the table, in order.

        Returns:
            Handle to the registered table.
        """
        column_list = [columns] if isinstance(columns, str) else list(columns)
        if len(set(column_list)) != len(column_list):
            msg = f"Table {name!r} has duplicate column names: {column_list}."
            raise DuplicateError(msg)
        return self._register(
            {column: ColumnSource(name, column) for column in column_list}, name
        )

    def join(
        self,
        left: str | PlannedFrame,
        right: str | PlannedFrame,
        *,
        how: _PlannedJoinStrategy,
        on: str | list[str] | None = None,
        left_on: str | list[str] | None = None,
        right_on: str | list[str] | None = None,
        suffix: str = "_right",
        rename_left: Mapping[str, str] | None = None,
        rename_right: Mapping[str, str] | None = None,
        name: str | None = None,
    ) -> PlannedFrame:
        """Plan a join between two registered frames.

        Name resolution mirrors `DataFrame.join`: columns from the right frame
        that clash with a left column get `suffix` appended, and for `full`
        joins with differently-named keys both join keys are kept. Passing only
        `suffix` behaves exactly like the existing join; `rename_left` and
        `rename_right` allow giving clashing columns explicit names instead,
        which is required when the suffixed name would itself collide with a
        pre-existing column.

        Arguments:
            left: Name or handle of the left frame.
            right: Name or handle of the right frame.
            how: Join strategy, one of "inner", "left", "full".
            on: Name(s) of the join columns in both frames. If set, `left_on`
                and `right_on` should be None.
            left_on: Join column(s) of the left frame.
            right_on: Join column(s) of the right frame.
            suffix: Suffix to append to right columns with a duplicate name.
            rename_left: Explicit renames applied to the left frame's columns
                before name resolution.
            rename_right: Explicit renames applied to the right frame's columns
                before name resolution.
            name: Name to register the result under; auto-generated if None.

        Returns:
            Handle to the planned join result.
        """
        if how not in _SUPPORTED_JOINS:
            msg = f"Only the following join strategies can be planned: {_SUPPORTED_JOINS}; found {how!r}."
            raise NotImplementedError(msg)
        left_sources = self._renamed(self._get(left), rename_left or {})
        right_sources = self._renamed(self._get(right), rename_right or {})
        left_keys, right_keys, shared_keys = _join_keys(how, on, left_on, right_on)
        if missing := [key for key in left_keys if key not in left_sources]:
            raise ColumnNotFoundError.from_missing_and_available_column_names(
                missing, left_sources
            )
        if missing := [key for key in right_keys if key not in right_sources]:
            raise ColumnNotFoundError.from_missing_and_available_column_names(
                missing, right_sources
            )
        result = dict(left_sources)
        planned_right: list[tuple[str, ColumnSource]] = []
        for column, source in right_sources.items():
            if column in right_keys and (shared_keys or how != "full"):
                continue
            output = column if column not in left_sources else f"{column}{suffix}"
            planned_right.append((output, source))
        for output, source in planned_right:
            if output in result:
                first = result[output]
                msg = (
                    f"Join of {self._frame_name(left)!r} and {self._frame_name(right)!r} "
                    f"produces duplicate column {output!r}, from both "
                    f"{_format_source(first)} and {_format_source(source)}.\n\n"
                    "Hint: pass `rename_left`/`rename_right` or a different `suffix` "
                    "to disambiguate."
                )
                raise DuplicateError(msg)
            result[output] = source
        return self._register(result, name)

    def select(
        self,
        frame: str | PlannedFrame,
        columns: str | Sequence[str],
        *,
        name: str | None = None,
    ) -> PlannedFrame:
        """Plan a column selection, dropping every column not listed.

        Dropped columns are removed from the plan together with their names:
        if a later step introduces a different column under the same name,
        provenance follows the new column only.

        Arguments:
            frame: Name or handle of the frame to select from.
            columns: Columns to keep, in order.
            name: Name to register the result under; auto-generated if None.

        Returns:
            Handle to the planned selection.
        """
        sources = self._get(frame)
        column_list = [columns] if isinstance(columns, str) else list(columns)
        if missing := [column for column in column_list if column not in sources]:
            raise ColumnNotFoundError.from_missing_and_available_column_names(
                missing, sources
            )
        return self._register({column: sources[column] for column in column_list}, name)

    def rename(
        self,
        frame: str | PlannedFrame,
        mapping: Mapping[str, str],
        *,
        name: str | None = None,
    ) -> PlannedFrame:
        """Plan renaming columns of a registered frame.

        Arguments:
            frame: Name or handle of the frame to rename columns of.
            mapping: Mapping of old column names to new ones.
            name: Name to register the result under; auto-generated if None.

        Returns:
            Handle to the planned rename.

        Raises:
            ColumnMappingConflictError: If two columns would be mapped to the
                same final name; the error points to both source columns.
        """
        return self._register(self._renamed(self._get(frame), mapping), name)

    def columns(self, frame: str | PlannedFrame) -> list[str]:
        """Return the planned output column names of `frame`, in order."""
        return list(self._get(frame))

    def sources(self, frame: str | PlannedFrame) -> dict[str, ColumnSource]:
        """Return the planned output columns of `frame` mapped to their origins."""
        return dict(self._get(frame))

    def source_of(self, frame: str | PlannedFrame, column: str) -> ColumnSource:
        """Return the original table and column behind `column` in `frame`.

        The lookup follows column identity, so a display name that referred to
        a different column at an earlier step resolves to the column that
        actually carries it in `frame`.
        """
        sources = self._get(frame)
        if column not in sources:
            raise ColumnNotFoundError.from_missing_and_available_column_names(
                [column], sources
            )
        return sources[column]


def _join_keys(
    how: str,
    on: str | list[str] | None,
    left_on: str | list[str] | None,
    right_on: str | list[str] | None,
) -> tuple[list[str], list[str], bool]:
    if on is not None:
        if left_on is not None or right_on is not None:
            msg = "If `on` is specified, `left_on` and `right_on` should be None."
            raise ValueError(msg)
        keys = [on] if isinstance(on, str) else list(on)
        return keys, keys, True
    if left_on is None or right_on is None:
        msg = f"Either (`left_on` and `right_on`) or `on` keys should be specified for {how}."
        raise ValueError(msg)
    left_keys = [left_on] if isinstance(left_on, str) else list(left_on)
    right_keys = [right_on] if isinstance(right_on, str) else list(right_on)
    if len(left_keys) != len(right_keys):
        msg = "`left_on` and `right_on` must have the same length."
        raise ValueError(msg)
    return left_keys, right_keys, False


def _format_source(source: ColumnSource) -> str:
    return f"{source.table!r}.{source.column!r}"


def _raise_mapping_conflict(name: str, first: ColumnSource, second: ColumnSource) -> None:
    msg = (
        f"Column name {name!r} is mapped from more than one source column: "
        f"{_format_source(first)} and {_format_source(second)}."
    )
    raise ColumnMappingConflictError(msg)
