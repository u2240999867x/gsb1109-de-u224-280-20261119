import datetime
import hashlib
import heapq
from decimal import Decimal

import agate

from csvkit.hyperloglog import HyperLogLog


class FrequentValues:
    """
    Track the most common values, exactly while the number of distinct values is small, and with
    a bounded Space-Saving summary for larger inputs.

    :param exact_limit:
        The number of distinct values to track exactly.
    :param capacity:
        The number of values to track after switching to the Space-Saving summary.
    """
    def __init__(self, exact_limit=10000, capacity=1000):
        self.exact_limit = exact_limit
        self.capacity = capacity
        self.counts = {}
        self.summary = None

    @property
    def is_exact(self):
        return self.summary is None

    def add(self, value):
        if self.summary is None:
            self.counts[value] = self.counts.get(value, 0) + 1

            if len(self.counts) > self.exact_limit:
                self.summary = _SpaceSaving(self.capacity)

                for key, count in self.counts.items():
                    self.summary.add(key, count)

                self.counts = None
        else:
            self.summary.add(value)

    def most_common(self, n):
        if self.summary is None:
            return self._most_common_exact(n)

        return self.summary.most_common(n)

    def _most_common_exact(self, n):
        # Match Counter.most_common, including its tie-breaking.
        return heapq.nlargest(n, self.counts.items(), key=lambda item: item[1])


class _SpaceSaving:
    """
    A bounded summary of frequent values, using the Space-Saving algorithm.

    The estimated count of a value is an overestimate, and is never less than its true count.
    """
    def __init__(self, capacity):
        self.capacity = capacity
        self.counts = {}
        self.first_seen = {}
        self.heap = []
        self.sequence = 0

    def add(self, value, count=1):
        self.sequence += 1

        if value in self.counts:
            self.counts[value] += count
            heapq.heappush(self.heap, (self.counts[value], self.first_seen[value], value))
            self._compact()
            return

        if len(self.counts) < self.capacity:
            self.counts[value] = count
            self.first_seen[value] = self.sequence
            heapq.heappush(self.heap, (count, self.sequence, value))
            return

        while self.heap:
            minimum, sequence, candidate = heapq.heappop(self.heap)

            if candidate in self.counts and self.counts[candidate] == minimum:
                break
        else:
            return

        del self.counts[candidate]
        del self.first_seen[candidate]

        self.counts[value] = minimum + count
        self.first_seen[value] = self.sequence
        heapq.heappush(self.heap, (minimum + count, self.sequence, value))

    def _compact(self):
        if len(self.heap) > 4 * self.capacity:
            self.heap = [(count, self.first_seen[value], value) for value, count in self.counts.items()]
            heapq.heapify(self.heap)

    def most_common(self, n):
        return sorted(
            self.counts.items(),
            key=lambda item: (-item[1], self.first_seen[item[0]]),
        )[:n]


class MedianValues:
    """
    Track values for an approximate median, storing every value while the input is small, and a
    deterministic uniform sample for larger inputs.
    """
    def __init__(self, capacity=10000):
        self.capacity = capacity
        self.sample = []
        self.seen = 0

    @property
    def is_exact(self):
        return self.seen <= self.capacity

    def add(self, value):
        self.seen += 1

        if len(self.sample) < self.capacity:
            self.sample.append(value)
            return

        digest = hashlib.blake2b(str(self.seen).encode('ascii'), digest_size=8).digest()
        index = int.from_bytes(digest, 'big') % self.seen

        if index < self.capacity:
            self.sample[index] = value

    def median(self):
        values = sorted(self.sample)
        length = len(values)

        if not length:
            return None

        middle = length // 2

        if length % 2:
            return values[middle]

        return (values[middle - 1] + values[middle]) / 2


class StreamingColumn:
    """
    Accumulate the statistics of one column, while reading its values one row at a time.

    Type inference matches :class:`agate.TypeTester`: a candidate type is discarded as soon as a
    value can't be cast to it, and the first remaining type (in the configured order) is used.

    The count of unique values is estimated with :class:`HyperLogLog`. The counts of null and
    non-null values, and the row count, are exact.
    """
    def __init__(self, name, data_types, precision=14):
        self.name = name
        self.data_types = list(data_types)
        self.possible = list(self.data_types)

        self._estimators = {data_type: HyperLogLog(precision) for data_type in self.data_types}
        self._has_nulls = {data_type: False for data_type in self.data_types}
        self._nonnulls = {data_type: 0 for data_type in self.data_types}
        self._minimums = {data_type: None for data_type in self.data_types}
        self._maximums = {data_type: None for data_type in self.data_types}
        self._totals = {data_type: None for data_type in self.data_types}
        self._sums_of_squares = {data_type: None for data_type in self.data_types}
        self._max_lengths = {data_type: 0 for data_type in self.data_types}
        self._max_precisions = {data_type: 0 for data_type in self.data_types}
        self._frequencies = {data_type: FrequentValues() for data_type in self.data_types}
        self._medians = {data_type: MedianValues() for data_type in self.data_types}
        self._has_non_finite = {data_type: False for data_type in self.data_types}

        for data_type in self.data_types:
            if isinstance(data_type, agate.Number):
                self._totals[data_type] = Decimal(0)
                self._sums_of_squares[data_type] = Decimal(0)
            elif isinstance(data_type, agate.TimeDelta):
                self._totals[data_type] = datetime.timedelta()

    def add(self, value, row_index):
        for data_type in list(self.possible):
            try:
                cast_value = data_type.cast(value)
            except agate.CastError as e:
                if len(self.possible) == 1:
                    raise agate.CastError(str(e) + f' Error at row {row_index} column {self.name}.')

                self.possible.remove(data_type)
                del self._estimators[data_type]
                del self._has_nulls[data_type]
                del self._nonnulls[data_type]
                del self._minimums[data_type]
                del self._maximums[data_type]
                del self._totals[data_type]
                del self._sums_of_squares[data_type]
                del self._max_lengths[data_type]
                del self._max_precisions[data_type]
                del self._frequencies[data_type]
                del self._medians[data_type]
            else:
                self._add_value(data_type, cast_value)

    def _add_value(self, data_type, value):
        self._estimators[data_type].add(value)
        self._frequencies[data_type].add(value)

        if value is None:
            self._has_nulls[data_type] = True
            return

        self._nonnulls[data_type] += 1

        if isinstance(data_type, (agate.Number, agate.Date, agate.DateTime, agate.TimeDelta)):
            if not (isinstance(data_type, agate.Number) and not value.is_finite()):
                if self._minimums[data_type] is None or value < self._minimums[data_type]:
                    self._minimums[data_type] = value

                if self._maximums[data_type] is None or value > self._maximums[data_type]:
                    self._maximums[data_type] = value

        if isinstance(data_type, agate.Number):
            if value.is_finite():
                self._medians[data_type].add(value)
                self._sums_of_squares[data_type] += value * value

                exponent = value.normalize().as_tuple().exponent
                if exponent < 0:
                    self._max_precisions[data_type] = max(self._max_precisions[data_type], -exponent)
            else:
                self._has_non_finite[data_type] = True

            self._totals[data_type] += value
        elif isinstance(data_type, agate.TimeDelta):
            self._totals[data_type] += value
        elif isinstance(data_type, agate.Text):
            self._max_lengths[data_type] = max(self._max_lengths[data_type], len(value))

    @property
    def data_type(self):
        return self.possible[0]

    def unique(self):
        """
        Return the estimated number of unique values as an integer.
        """
        return self._estimators[self.data_type].count()

    def is_estimated(self, operation):
        data_type = self.data_type

        if operation == 'unique':
            return True
        if operation == 'freq':
            return not self._frequencies[data_type].is_exact
        if operation == 'median':
            return isinstance(data_type, agate.Number) and not self._medians[data_type].is_exact

        return False

    def get(self, operation, freq_count=5):
        data_type = self.data_type

        if operation == 'type':
            return type(data_type).__name__

        if operation == 'nulls':
            return self._has_nulls[data_type]

        if operation == 'nonnulls':
            return self._nonnulls[data_type]

        if operation == 'unique':
            return self.unique()

        if operation == 'min':
            if isinstance(data_type, (agate.Number, agate.Date, agate.DateTime, agate.TimeDelta)):
                if isinstance(data_type, agate.Number) and self._has_non_finite[data_type]:
                    return None
                return self._minimums[data_type]

        elif operation == 'max':
            if isinstance(data_type, (agate.Number, agate.Date, agate.DateTime, agate.TimeDelta)):
                if isinstance(data_type, agate.Number) and self._has_non_finite[data_type]:
                    return None
                return self._maximums[data_type]

        elif operation == 'sum':
            if isinstance(data_type, (agate.Number, agate.TimeDelta)):
                return self._totals[data_type]

        elif operation == 'mean':
            if isinstance(data_type, (agate.Number, agate.TimeDelta)) and self._nonnulls[data_type]:
                return self._totals[data_type] / self._nonnulls[data_type]

        elif operation == 'median':
            if isinstance(data_type, agate.Number) and self._nonnulls[data_type]:
                if self._has_non_finite[data_type]:
                    return None
                return self._medians[data_type].median()

        elif operation == 'stdev':
            n = self._nonnulls[data_type]

            if isinstance(data_type, agate.Number) and n > 1:
                if self._has_non_finite[data_type]:
                    return Decimal('NaN')

                total = self._totals[data_type]
                variance = (self._sums_of_squares[data_type] - total * total / n) / (n - 1)

                if variance >= 0:
                    return variance.sqrt()

        elif operation == 'len':
            if isinstance(data_type, agate.Text):
                return Decimal(self._max_lengths[data_type])

        elif operation == 'maxprecision':
            if isinstance(data_type, agate.Number):
                return self._max_precisions[data_type]

        elif operation == 'freq':
            return [
                {'value': value, 'count': count}
                for value, count in self._frequencies[data_type].most_common(freq_count)
            ]


class StreamingTable:
    """
    A minimal table-like object for outputting statistics collected by :class:`StreamingColumn`.
    """
    is_streaming = True

    def __init__(self, column_names, columns, row_count):
        self.column_names = column_names
        self.columns = columns
        self.row_count = row_count

    @property
    def rows(self):
        return range(self.row_count)
