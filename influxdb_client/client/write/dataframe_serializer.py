"""
Functions for serialize Pandas DataFrame.

Much of the code here is inspired by that in the aioinflux packet found here: https://github.com/gusutabopb/aioinflux
"""

import logging
import math
import re
from typing import NamedTuple

from influxdb_client import WritePrecision
from influxdb_client.client.write.point import _ESCAPE_KEY, _ESCAPE_STRING, _ESCAPE_MEASUREMENT, DEFAULT_WRITE_PRECISION

logger = logging.getLogger('influxdb_client.client.write.dataframe_serializer')


class DataframeRejection(NamedTuple):
    """A DataFrame row rejected during serialization into Line Protocol."""

    #: zero-based positional index of the rejected row within the original DataFrame
    position: int
    #: human-readable reason why the row was rejected
    reason: str


def _itertuples(data_frame):
    cols = [data_frame.iloc[:, k] for k in range(len(data_frame.columns))]
    return zip(data_frame.index, *cols)


def _resolve_timestamp_value(value, precision, timestamp_timezone):
    """
    Parse a single timestamp-column value into a timezone-aware ``Timestamp``.

    A caller-specified ``timestamp_timezone`` is used to interpret naive values only;
    values that already carry an offset keep their own offset.

    :return: a ``(timestamp, rejection_reason)`` tuple; exactly one element is ``None``
    """
    from ...extras import pd
    if pd.isna(value):
        return None, 'missing timestamp'
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        timestamp = pd.to_datetime(value, unit=precision)
    else:
        timestamp = pd.to_datetime(value)
    if timestamp.tzinfo is None:
        if timestamp_timezone is not None:
            try:
                timestamp = timestamp.tz_localize(timestamp_timezone, ambiguous='raise', nonexistent='raise')
            except Exception as error:
                return None, (f"cannot resolve local time {value!r} "
                              f"in timezone {timestamp_timezone!r}: {error}")
        else:
            timestamp = timestamp.tz_localize('UTC')
    return timestamp, None


class DataframeSerializer:
    """
    Serialize DataFrame into LineProtocols.

    :ivar rejected: list of :class:`~DataframeRejection` holding the rows that were
                    rejected during initialization (e.g. missing or unresolvable
                    timestamps). The ``position`` of each rejection is the zero-based
                    positional index of the row within the original DataFrame, so it
                    identifies the row unambiguously even when the DataFrame index
                    contains duplicate labels.
    """

    def __init__(self, data_frame, point_settings, precision=DEFAULT_WRITE_PRECISION, chunk_size: int = None,
                 **kwargs) -> None:
        """
        Init serializer.

        :param data_frame: Pandas DataFrame to serialize
        :param point_settings: Default Tags
        :param precision: The precision for the unix timestamps within the body line-protocol.
        :param chunk_size: The size of chunk for serializing into chunks.
        :key data_frame_measurement_name: name of measurement for writing Pandas DataFrame
        :key data_frame_tag_columns: list of DataFrame columns which are tags, rest columns will be fields
        :key data_frame_timestamp_column: name of DataFrame column which contains a timestamp. The column can be defined as a :class:`~str` value
                                          formatted as `2018-10-26`, `2018-10-26 12:00`, `2018-10-26 12:00:00-05:00`
                                          or other formats and types supported by `pandas.to_datetime <https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.to_datetime.html#pandas.to_datetime>`_ - ``DataFrame``
        :key data_frame_timestamp_timezone: name of the timezone which is used for timestamp column - ``DataFrame``

        When ``data_frame_timestamp_column`` is specified, each value of the column is parsed
        independently. Rows whose timestamp is missing or is a naive local time that cannot be
        unambiguously resolved in ``data_frame_timestamp_timezone`` are rejected: they are not
        serialized into Line Protocol and they are not backfilled from the DataFrame index.
        Rejected rows are reported in :attr:`~DataframeSerializer.rejected`.
        """  # noqa: E501
        # This function is hard to understand but for good reason:
        # the approach used here is considerably more efficient
        # than the alternatives.
        #
        # We build up a Python expression that efficiently converts a data point
        # tuple into line-protocol entry, and then evaluate the expression
        # as a lambda so that we can call it. This avoids the overhead of
        # invoking a function on every data value - we only have one function
        # call per row instead. The expression consists of exactly
        # one f-string, so we build up the parts of it as segments
        # that are concatenated together to make the full f-string inside
        # the lambda.
        #
        # Things are made a little more complex because fields and tags with NaN
        # values and empty tags are omitted from the generated line-protocol
        # output.
        #
        # As an example, say we have a data frame with two value columns:
        #        a float
        #        b int
        #
        # This will generate a lambda expression to be evaluated that looks like
        # this:
        #
        #     lambda p: f"""{measurement_name} {keys[0]}={p[1]},{keys[1]}={p[2]}i {p[0].value}"""
        #
        # This lambda is then executed for each row p.
        #
        # When NaNs are present, the expression looks like this (split
        # across two lines to satisfy the code-style checker)
        #
        #    lambda p: f"""{measurement_name} {"" if pd.isna(p[1])
        #    else f"{keys[0]}={p[1]}"},{keys[1]}={p[2]}i {p[0].value}"""
        #
        # When there's a NaN value in column a, we'll end up with a comma at the start of the
        # fields, so we run a regexp substitution after generating the line-protocol entries
        # to remove this.
        #
        # We're careful to run these potentially costly extra steps only when NaN values actually
        # exist in the data.

        from ...extras import pd, np
        if not isinstance(data_frame, pd.DataFrame):
            raise TypeError('Must be DataFrame, but type was: {0}.'
                            .format(type(data_frame)))

        data_frame_measurement_name = kwargs.get('data_frame_measurement_name')
        if data_frame_measurement_name is None:
            raise TypeError('"data_frame_measurement_name" is a Required Argument')

        timestamp_column = kwargs.get('data_frame_timestamp_column', None)
        timestamp_timezone = kwargs.get('data_frame_timestamp_timezone', None)
        data_frame = data_frame.copy(deep=False)

        # Rows rejected because their timestamp is missing or cannot be resolved.
        # Positions are zero-based iloc positions within the original DataFrame,
        # so they stay stable no matter how the DataFrame is chunked.
        self._rejected_positions = set()
        self.rejected = []

        if timestamp_column is None:
            data_frame_timestamp = data_frame.index
            if isinstance(data_frame_timestamp, pd.PeriodIndex):
                data_frame_timestamp = data_frame_timestamp.to_timestamp()
            else:
                # TODO: this is almost certainly not what you want
                # when the index is the default RangeIndex.
                # Instead, it would probably be better to leave
                # out the timestamp unless a time column is explicitly
                # enabled.
                data_frame_timestamp = pd.to_datetime(data_frame_timestamp, unit=precision)

            if timestamp_timezone:
                if isinstance(data_frame_timestamp, pd.DatetimeIndex):
                    data_frame_timestamp = data_frame_timestamp.tz_localize(timestamp_timezone)
                else:
                    data_frame_timestamp = data_frame_timestamp.dt.tz_localize(timestamp_timezone)

            if hasattr(data_frame_timestamp, 'tzinfo') and data_frame_timestamp.tzinfo is None:
                data_frame_timestamp = data_frame_timestamp.tz_localize('UTC')
            data_frame.index = data_frame_timestamp
        else:
            # Parse every timestamp value independently so that a single batch
            # can mix offsets and so that unwritable rows can be rejected
            # individually instead of failing the whole batch.
            timestamps = []
            for position, value in enumerate(data_frame[timestamp_column]):
                timestamp, rejection = _resolve_timestamp_value(value, precision, timestamp_timezone)
                if rejection is not None:
                    self._rejected_positions.add(position)
                    self.rejected.append(DataframeRejection(position=position, reason=rejection))
                    logger.debug("Reject DataFrame row %s: %s", position, rejection)
                timestamps.append(timestamp)
            data_frame[timestamp_column] = timestamps

        data_frame_tag_columns = kwargs.get('data_frame_tag_columns')
        data_frame_tag_columns = set(data_frame_tag_columns or [])

        # keys holds a list of string keys.
        keys = []
        # tags holds a list of tag f-string segments ordered alphabetically by tag key.
        tags = []
        # fields holds a list of field f-string segments  ordered alphebetically by field key
        fields = []
        # field_indexes holds the index into each row of all the fields.
        field_indexes = []

        if point_settings.defaultTags:
            for key, value in point_settings.defaultTags.items():
                # Avoid overwriting existing data if there's a column
                # that already exists with the default tag's name.
                # Note: when a new column is added, the old DataFrame
                # that we've made a shallow copy of is unaffected.
                # TODO: when there are NaN or empty values in
                # the column, we could make a deep copy of the
                # data and fill in those values with the default tag value.
                if key not in data_frame.columns:
                    data_frame[key] = value
                    data_frame_tag_columns.add(key)

        # Get a list of all the columns sorted by field/tag key.
        # We want to iterate through the columns in sorted order
        # so that we know when we're on the first field so we
        # can know whether a comma is needed for that
        # field.
        columns = sorted(enumerate(data_frame.dtypes.items()), key=lambda col: col[1][0])

        # null_columns has a bool value for each column holding
        # whether that column contains any null (NaN or None) values.
        null_columns = data_frame.isnull().any()
        timestamp_index = 0

        # Iterate through the columns building up the expression for each column.
        for index, (key, value) in columns:
            key = str(key)
            key_format = f'{{keys[{len(keys)}]}}'
            keys.append(key.translate(_ESCAPE_KEY))
            # The field index is one more than the column index because the
            # time index is at column zero in the finally zipped-together
            # result columns.
            field_index = index + 1
            val_format = f'p[{field_index}]'

            if key in data_frame_tag_columns:
                # This column is a tag column.
                if null_columns.iloc[index]:
                    key_value = f"""{{
                            '' if {val_format} == '' or pd.isna({val_format}) else
                            f',{key_format}={{str({val_format}).translate(_ESCAPE_STRING)}}'
                        }}"""
                else:
                    key_value = f',{key_format}={{str({val_format}).translate(_ESCAPE_KEY)}}'
                tags.append(key_value)
                continue
            elif timestamp_column is not None and key == timestamp_column:
                timestamp_index = field_index
                continue

            # This column is a field column.
            # Note: no comma separator is needed for the first field.
            # It's important to omit it because when the first
            # field column has no nulls, we don't run the comma-removal
            # regexp substitution step.
            sep = '' if len(field_indexes) == 0 else ','
            if issubclass(value.type, np.integer) or issubclass(value.type, np.floating) or issubclass(value.type, np.bool_):  # noqa: E501
                suffix = 'i' if issubclass(value.type, np.integer) else ''
                if null_columns.iloc[index]:
                    field_value = f"""{{"" if pd.isna({val_format}) else f"{sep}{key_format}={{{val_format}}}{suffix}"}}"""  # noqa: E501
                else:
                    field_value = f"{sep}{key_format}={{{val_format}}}{suffix}"
            else:
                if null_columns.iloc[index]:
                    field_value = f"""{{
                            '' if pd.isna({val_format}) else
                            f'{sep}{key_format}="{{str({val_format}).translate(_ESCAPE_STRING)}}"'
                        }}"""
                else:
                    field_value = f'''{sep}{key_format}="{{str({val_format}).translate(_ESCAPE_STRING)}}"'''
            field_indexes.append(field_index)
            fields.append(field_value)

        measurement_name = str(data_frame_measurement_name).translate(_ESCAPE_MEASUREMENT)

        tags = ''.join(tags)
        fields = ''.join(fields)
        # Truncate with integer division so that timestamps close to a precision-unit
        # boundary are rounded consistently (float division can round up across the boundary).
        timestamp = '{p[%s].value}' % timestamp_index
        if precision == WritePrecision.US:
            timestamp = '{p[%s].value // 1000}' % timestamp_index
        elif precision == WritePrecision.MS:
            timestamp = '{p[%s].value // 1000000}' % timestamp_index
        elif precision == WritePrecision.S:
            timestamp = '{p[%s].value // 1000000000}' % timestamp_index

        f = eval(f'lambda p: f"""{{measurement_name}}{tags} {fields} {timestamp}"""', {
            'measurement_name': measurement_name,
            '_ESCAPE_KEY': _ESCAPE_KEY,
            '_ESCAPE_STRING': _ESCAPE_STRING,
            'keys': keys,
            'pd': pd,
        })

        for k, v in dict(data_frame.dtypes).items():
            if k in data_frame_tag_columns:
                data_frame = data_frame.replace({k: ''}, np.nan)

        def _any_not_nan(p, indexes):
            return any(map(lambda x: not pd.isna(p[x]), indexes))

        self.data_frame = data_frame
        self.f = f
        self.field_indexes = field_indexes
        self.first_field_maybe_null = null_columns.iloc[field_indexes[0] - 1]
        self._any_not_nan = _any_not_nan

        #
        # prepare chunks
        #
        if chunk_size is not None:
            self.number_of_chunks = int(math.ceil(len(data_frame) / float(chunk_size)))
            self.chunk_size = chunk_size
        else:
            self.number_of_chunks = None

    def serialize(self, chunk_idx: int = None):
        """
        Serialize chunk into LineProtocols.

        Rows reported in :attr:`~DataframeSerializer.rejected` are skipped;
        no line-protocol entry without a timestamp is ever emitted.

        :param chunk_idx: The index of chunk to serialize. If `None` then serialize whole dataframe.
        """
        if chunk_idx is None:
            chunk = self.data_frame
            offset = 0
        else:
            logger.debug("Serialize chunk %s/%s ...", chunk_idx + 1, self.number_of_chunks)
            chunk = self.data_frame[chunk_idx * self.chunk_size:(chunk_idx + 1) * self.chunk_size]
            offset = chunk_idx * self.chunk_size

        rows = _itertuples(chunk)
        if self._rejected_positions:
            rejected_positions = self._rejected_positions
            rows = (row for position, row in enumerate(rows, start=offset)
                    if position not in rejected_positions)

        if self.first_field_maybe_null:
            # When the first field is null (None/NaN), we'll have
            # a spurious leading comma which needs to be removed.
            lp = (re.sub('^(( |[^ ])* ),([a-zA-Z0-9])(.*)', '\\1\\3\\4', self.f(p))
                  for p in filter(lambda x: self._any_not_nan(x, self.field_indexes), rows))
            return list(lp)
        else:
            return list(map(self.f, rows))

    def number_of_chunks(self):
        """
        Return the number of chunks.

        :return: number of chunks or None if chunk_size is not specified.
        """
        return self.number_of_chunks


def data_frame_to_list_of_points(data_frame, point_settings, precision=DEFAULT_WRITE_PRECISION, **kwargs):
    """
    Serialize DataFrame into LineProtocols.

    :param data_frame: Pandas DataFrame to serialize
    :param point_settings: Default Tags
    :param precision: The precision for the unix timestamps within the body line-protocol.
    :key data_frame_measurement_name: name of measurement for writing Pandas DataFrame
    :key data_frame_tag_columns: list of DataFrame columns which are tags, rest columns will be fields
    :key data_frame_timestamp_column: name of DataFrame column which contains a timestamp. The column can be defined as a :class:`~str` value
                                      formatted as `2018-10-26`, `2018-10-26 12:00`, `2018-10-26 12:00:00-05:00`
                                      or other formats and types supported by `pandas.to_datetime <https://pandas.pydata.org/pandas-docs/stable/reference/api/pandas.to_datetime.html#pandas.to_datetime>`_ - ``DataFrame``
    :key data_frame_timestamp_timezone: name of the timezone which is used for timestamp column - ``DataFrame``
    """  # noqa: E501
    return DataframeSerializer(data_frame, point_settings, precision, **kwargs).serialize()
