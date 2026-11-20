from typing import Any, Callable, Dict, List, Literal, Optional, Sequence, TypeVar, Union

from dlt.common.typing import (
    TSortOrder,
    TTableHintTemplate,
    TColumnNames,
    TypedDict,
    NotRequired,
)

TCursorValue = TypeVar("TCursorValue", bound=Any)
LastValueFunc = Callable[[Sequence[TCursorValue]], Any]
OnCursorValueMissing = Literal["raise", "include", "exclude"]

TIncrementalRange = Literal["open", "closed"]


class IncrementalColumnState(TypedDict):
    initial_value: Optional[Any]
    last_value: Optional[Any]
    unique_hashes: List[str]
    start_value: Optional[Any]
    partitions: NotRequired[Dict[str, "IncrementalColumnState"]]
    """Cursor state of each partition, present only when the incremental uses `partition_key`.
    Keys are partition identifiers, see `Incremental.partition_state_key`."""


class IncrementalArgs(TypedDict, total=False):
    cursor_path: str
    initial_value: Optional[Any]
    last_value_func: Optional[Union[LastValueFunc[str], Literal["min", "max"]]]
    """Last value callable or name of built in function"""
    primary_key: Optional[TTableHintTemplate[TColumnNames]]
    partition_key: Optional[TTableHintTemplate[TColumnNames]]
    """Column or columns that identify a partition, each partition keeps its own cursor"""
    end_value: Optional[Any]
    row_order: Optional[TSortOrder]
    allow_external_schedulers: Optional[bool]
    lag: Optional[Union[float, int]]
    on_cursor_value_missing: Optional[OnCursorValueMissing]
    range_start: Optional[TIncrementalRange]
    range_end: Optional[TIncrementalRange]
