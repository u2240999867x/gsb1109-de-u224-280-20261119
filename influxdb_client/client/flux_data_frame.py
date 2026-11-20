"""
Explicit merge of several Flux tables into a single Pandas DataFrame.

:meth:`~influxdb_client.client.query_api.QueryApi.query_data_frame` returns one DataFrame per Flux
table whenever the schemas differ. The helpers in this module instead merge the records of selected
(and compatible) Flux tables into one DataFrame while keeping:

* the original Flux column declaration (the data type) for every shared column,
* the source result name and the group key of every row,
* missing values and integers outside of the binary floating point precision.

Tables are selected with :class:`DataFrameMergeConfig` - by source result name (the value of the
``result`` column) and by group key values. Tables that declare a different data type for a shared
column cannot be merged: :class:`FluxMergedDataFrameException` reports the exact source tables and
both declarations instead of silently falling back to ``object`` or ``float64``.
"""
from typing import Dict, List, Optional

from influxdb_client.client.flux_csv_parser import FluxCsvParserException, _convert_flux_value
from influxdb_client.client.flux_table import FluxColumn, FluxTable

#: Name of the output column that keeps the source result name for every row.
RESULT_SOURCE_COLUMN = "_flux_result"
#: Name of the output column that keeps the source group key mapping for every row.
GROUP_SOURCE_COLUMN = "_flux_group"

# Flux data types mapped to Pandas extension dtypes so missing values never force a numeric cast.
_PANDAS_DTYPES = {
    "long": "Int64",
    "double": "Float64",
    "boolean": "boolean",
    "string": "string",
    "dateTime:RFC3339": "datetime64[ns, UTC]",
    "dateTime:RFC3339Nano": "datetime64[ns, UTC]",
    "duration": "Int64",
    # unsignedLong may exceed the signed Int64 range and base64Binary holds bytes: keep Python
    # objects so the original parsed values are preserved without overflow or coercion.
    "unsignedLong": object,
    "base64Binary": object,
}


class FluxMergedDataFrameException(FluxCsvParserException):
    """Raised when Flux tables selected for a merge declare incompatible columns."""


class DataFrameMergeConfig:
    """
    Configure which Flux tables participate in an explicit DataFrame merge.

    A table is merged when it matches every active criterion:

    * its source result name (the value of the ``result`` column) is in ``result_names`` - unless
      ``result_names`` is ``None``, which selects every result;
    * its group key matches at least one of the ``groups`` mappings - unless ``groups`` is ``None``,
      which does not filter by group key.

    :param result_names: source result names to merge; ``None`` selects all results
    :param groups: group key mappings to merge, e.g. ``[{"host": "north"}]``; every key must be a
                   group column of the table and hold the given value
    """

    def __init__(self, result_names: Optional[List[str]] = None,
                 groups: Optional[List[Dict[str, object]]] = None) -> None:
        self.result_names = list(result_names) if result_names is not None else None
        self.groups = [dict(group) for group in groups] if groups is not None else None

    def selects(self, result_name: Optional[str], group_key: Dict[str, object]) -> bool:
        """Return ``True`` when a table with the given identity is part of the merge."""
        if self.result_names is not None and result_name not in self.result_names:
            return False
        if self.groups is not None and not any(self._group_matches(group_key, group) for group in self.groups):
            return False
        return True

    @staticmethod
    def _group_matches(group_key: Dict[str, object], rule: Dict[str, object]) -> bool:
        for label, expected in rule.items():
            if label not in group_key or group_key[label] != expected:
                return False
        return True


class _TableSource:
    """A Flux table together with the merge identity derived from its declaration."""

    def __init__(self, table: FluxTable, result_name: Optional[str], group_key: Dict[str, object],
                 table_id: Optional[int]) -> None:
        self.table = table
        self.result_name = result_name
        self.group_key = group_key
        self.table_id = table_id

    def describe(self) -> str:
        group = ", ".join(f"{label}={value!r}" for label, value in self.group_key.items())
        table = f"table {self.table_id}" if self.table_id is not None else "declaration-only table"
        return f"result {self.result_name!r}, {table}, group key {{{group}}}"


def merge_tables_to_data_frame(tables: List[FluxTable], config: DataFrameMergeConfig = None,
                               data_frame_index: List[str] = None):
    """
    Merge selected Flux tables into a single Pandas DataFrame.

    Every output row keeps its origin in the :data:`RESULT_SOURCE_COLUMN` and
    :data:`GROUP_SOURCE_COLUMN` columns. The regular Flux columns (including group key columns such
    as ``host``) stay ordinary output columns and are never overwritten by the source information.

    :param tables: Flux tables returned by :meth:`~influxdb_client.client.query_api.QueryApi.query`
    :param config: selection of source result names and group keys; ``None`` selects every table
    :param data_frame_index: optional columns to use as the DataFrame index
    :return: one :class:`~pandas.DataFrame` with the records of all selected compatible tables
    :raises FluxMergedDataFrameException: selected tables declare different data types for a shared
                                          column name, or a business column uses a reserved name
    """
    from ..extras import pd

    config = config if config is not None else DataFrameMergeConfig()
    sources = [_source_identity(table) for table in tables]
    selected = [source for source in sources
                if config.selects(source.result_name, source.group_key)]

    for source in selected:
        _check_reserved_columns(source.table)

    _check_compatibility(selected)

    frames = [_table_to_frame(pd, source) for source in selected]
    if frames:
        # Declaration-only tables contribute empty frames whose columns still carry the declared
        # types, so concatenating them never turns a column into an untyped object.
        data_frame = pd.concat(frames, ignore_index=True)
    else:
        data_frame = pd.DataFrame({
            RESULT_SOURCE_COLUMN: pd.array([], dtype="string"),
            GROUP_SOURCE_COLUMN: pd.array([], dtype=object),
        })

    if data_frame_index:
        data_frame = data_frame.set_index(data_frame_index)

    return data_frame


def select_source_rows(data_frame, result_name: str, group: Dict[str, object] = None):
    """
    Retrieve the rows of a merged DataFrame that belong to one source table.

    :param data_frame: DataFrame produced by :func:`merge_tables_to_data_frame`
    :param result_name: the source result name of the requested table
    :param group: optional group key mapping used to distinguish tables inside one result
    """
    mask = data_frame[RESULT_SOURCE_COLUMN] == result_name
    if group is not None:
        def _matches(group_key):
            return all(group_key.get(label) == value for label, value in group.items())

        mask = mask & data_frame[GROUP_SOURCE_COLUMN].map(_matches)
    return data_frame[mask]


def _source_identity(table: FluxTable) -> _TableSource:
    first_record = table.records[0] if table.records else None
    result_name = None
    table_id = None
    group_key: Dict[str, object] = {}

    for column in table.columns:
        if first_record is not None:
            value = first_record.values.get(column.label)
        else:
            value = _declared_default(column)
        if column.label == "result":
            result_name = value
        if column.group:
            group_key[column.label] = value
        if column.label == "table" and first_record is not None:
            table_id = value

    return _TableSource(table=table, result_name=result_name, group_key=group_key, table_id=table_id)


def _declared_default(column: FluxColumn):
    if column.default_value is None or column.default_value == "":
        return None
    return _convert_flux_value(column.default_value, column.data_type)


def _check_reserved_columns(table: FluxTable) -> None:
    labels = {column.label for column in table.columns}
    for reserved in (RESULT_SOURCE_COLUMN, GROUP_SOURCE_COLUMN):
        if reserved in labels:
            raise FluxMergedDataFrameException(
                f"Cannot merge table: the Flux column name '{reserved}' is reserved for the "
                f"source result and group information")


def _check_compatibility(sources: List[_TableSource]) -> Dict[str, FluxColumn]:
    declarations: Dict[str, FluxColumn] = {}
    owners: Dict[str, _TableSource] = {}
    for source in sources:
        for column in source.table.columns:
            existing = declarations.get(column.label)
            if existing is not None and existing.data_type != column.data_type:
                raise FluxMergedDataFrameException(
                    f"Cannot merge Flux tables: the column '{column.label}' is declared as "
                    f"'{existing.data_type}' in ({owners[column.label].describe()}) but as "
                    f"'{column.data_type}' in ({source.describe()}). Select compatible result "
                    f"names and group keys with DataFrameMergeConfig.")
            declarations.setdefault(column.label, column)
            owners.setdefault(column.label, source)
    return declarations


def _table_to_frame(pd, source: _TableSource):
    table = source.table
    columns: Dict[str, FluxColumn] = {}
    for column in table.columns:
        columns.setdefault(column.label, column)

    data = {}
    for label, column in columns.items():
        values = [record.values.get(label) for record in table.records]
        data[label] = pd.array(values, dtype=_pandas_dtype(column.data_type))

    frame = pd.DataFrame(data)

    record_count = len(table.records)
    frame[RESULT_SOURCE_COLUMN] = pd.array([source.result_name] * record_count, dtype="string")
    frame[GROUP_SOURCE_COLUMN] = pd.array(
        [dict(source.group_key) for _ in range(record_count)], dtype=object)

    return frame


def _pandas_dtype(flux_data_type: str):
    return _PANDAS_DTYPES.get(flux_data_type, object)
