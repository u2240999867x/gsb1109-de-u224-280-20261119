import datetime
import hashlib
import math
from decimal import Decimal


def canonical_bytes(value):
    """
    Return a stable byte representation of a value, so that estimates don't depend on Python's
    randomized hash seed, or on how a value happened to be parsed.
    """
    if value is None:
        return b'\x00'
    if isinstance(value, bool):
        return b'\x01' + (b'1' if value else b'0')
    if isinstance(value, Decimal):
        if value == 0:
            return b'\x02' + b'0'
        return b'\x02' + str(value.normalize()).encode('ascii')
    if isinstance(value, int):
        return b'\x03' + str(value).encode('ascii')
    if isinstance(value, float):
        return b'\x04' + repr(value).encode('ascii')
    if isinstance(value, datetime.datetime):
        return b'\x05' + value.isoformat().encode('ascii')
    if isinstance(value, datetime.date):
        return b'\x06' + value.isoformat().encode('ascii')
    if isinstance(value, datetime.timedelta):
        return b'\x07' + str(value // datetime.timedelta(microseconds=1)).encode('ascii')
    if isinstance(value, bytes):
        return b'\x08' + value
    if isinstance(value, str):
        return b'\x09' + value.encode('utf-8')
    return b'\x0a' + type(value).__name__.encode('utf-8') + b':' + repr(value).encode('utf-8')


class HyperLogLog:
    """
    A deterministic HyperLogLog cardinality estimator.

    The estimator stores a fixed array of registers. Merging two estimators takes the maximum of
    each pair of registers, which makes the result independent of how the input was split into
    chunks, and of the order in which chunks are merged.

    :param precision:
        The number of bits used for the register index, between 4 and 18. The relative standard
        error is about ``1.04 / sqrt(2 ** precision)``. Defaults to 14 (about 0.81%).
    """
    def __init__(self, precision=14):
        if not 4 <= precision <= 18:
            raise ValueError('precision must be between 4 and 18')

        self.precision = precision
        self.m = 1 << precision
        self.registers = bytearray(self.m)

    def _add_digest(self, digest):
        x = int.from_bytes(digest, 'big')
        index = x >> (64 - self.precision)
        remaining = x & ((1 << (64 - self.precision)) - 1)

        if remaining:
            rank = 64 - self.precision - remaining.bit_length() + 1
        else:
            rank = 64 - self.precision + 1

        if rank > self.registers[index]:
            self.registers[index] = rank

    def add(self, value):
        """
        Add a value to the estimator.
        """
        self._add_digest(hashlib.blake2b(canonical_bytes(value), digest_size=8).digest())

    def add_canonical(self, value):
        """
        Add a value that is already in the form returned by :func:`canonical_bytes`.
        """
        self._add_digest(hashlib.blake2b(value, digest_size=8).digest())

    update = add

    def merge(self, other):
        """
        Merge another estimator into this one. The result is the same as if every value had been
        added to a single estimator.
        """
        if self.precision != other.precision:
            raise ValueError('Cannot merge HyperLogLog estimators with different precisions')

        for index, rank in enumerate(other.registers):
            if rank > self.registers[index]:
                self.registers[index] = rank

        return self

    def __or__(self, other):
        return HyperLogLog(self.precision).merge(self).merge(other)

    def estimate(self):
        """
        Return the estimated number of distinct values as a float.
        """
        m = self.m
        if m == 16:
            alpha = 0.673
        elif m == 32:
            alpha = 0.697
        elif m == 64:
            alpha = 0.709
        else:
            alpha = 0.7213 / (1 + 1.079 / m)

        total = sum(2.0 ** -rank for rank in self.registers)
        estimate = alpha * m * m / total
        zeros = self.registers.count(0)

        if estimate <= 2.5 * m and zeros:
            estimate = m * math.log(m / zeros)
        elif estimate > (1 << 64) / 30:
            estimate = -(1 << 64) * math.log(1 - estimate / (1 << 64))

        return estimate

    def count(self):
        """
        Return the estimated number of distinct values as an integer.
        """
        return int(round(self.estimate()))

    def __len__(self):
        return self.count()
