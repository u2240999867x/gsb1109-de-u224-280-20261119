from __future__ import annotations


__all__ = [
    'StringStreamKind',
    'StringStreamResult',
]


import enum

from typing import Optional


class StringStreamKind(enum.Enum):
    """
    The kind of physical stream a :class:`StringStreamResult` was read from.
    """

    #: The Unicode (``PtypString`` / type ``001F``) stream.
    UNICODE = '001F'
    #: The legacy code page (``PtypString8`` / type ``001E``) stream.
    ANSI = '001E'
    #: No stream of either representation was present.
    MISSING = 'missing'
    #: Streams were present but none could be decoded.
    UNDECODABLE = 'undecodable'


class StringStreamResult:
    """
    The outcome of reading a string property with field-level encoding
    fallback enabled.

    The object distinguishes between a stream that does not exist and one that
    exists but is genuinely empty, records which physical representation was
    actually read and the encoding used to decode it, and whether the value is
    the result of a fallback from the preferred representation.

    Attributes:
        streamName: The name of the stream, without the type suffix.
        value: The decoded value, or ``None`` when the stream was missing or
            could not be interpreted. An empty but readable stream is reported
            as ``""``, never as ``None``.
        kind: Which representation the value was actually read from, or
            :attr:`StringStreamKind.MISSING` when neither was available.
        streamPath: The full name of the physical stream the value was read
            from (including the type suffix), or ``None`` when no value could
            be read.
        encoding: The encoding used to decode the bytes that produced
            :attr:`value`, or ``None`` when no value could be decoded.
        usedFallback: Whether a representation other than the preferred one
            was used to produce :attr:`value`.
        missing: Whether every representation of the field was absent.
        empty: Whether the stream that was read existed but contained no data.
        undecodable: Whether every present representation failed to decode.
        reason: A human readable explanation of why the field could not be
            interpreted, or ``None`` when a value is available.
    """

    def __init__(self,
                 streamName: str,
                 value: Optional[str],
                 kind: StringStreamKind,
                 encoding: Optional[str],
                 usedFallback: bool,
                 missing: bool = False,
                 empty: bool = False,
                 undecodable: bool = False,
                 reason: Optional[str] = None):
        self.__streamName = streamName
        self.__value = value
        self.__kind = kind
        self.__encoding = encoding
        self.__usedFallback = usedFallback
        self.__missing = missing
        self.__empty = empty
        self.__undecodable = undecodable
        self.__reason = reason

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, StringStreamResult):
            return NotImplemented
        return (
            self.streamName == other.streamName
            and self.value == other.value
            and self.kind == other.kind
            and self.encoding == other.encoding
            and self.usedFallback == other.usedFallback
            and self.missing == other.missing
            and self.empty == other.empty
            and self.undecodable == other.undecodable
            and self.reason == other.reason
        )

    def __hash__(self) -> int:
        return hash((
            self.__streamName,
            self.__value,
            self.__kind,
            self.__encoding,
            self.__usedFallback,
            self.__missing,
            self.__empty,
            self.__undecodable,
            self.__reason,
        ))

    def __repr__(self) -> str:
        return (
            f'StringStreamResult(streamName={self.streamName!r}, '
            f'value={self.value!r}, kind={self.kind!r}, '
            f'encoding={self.encoding!r}, usedFallback={self.usedFallback!r}, '
            f'missing={self.missing!r}, empty={self.empty!r}, '
            f'undecodable={self.undecodable!r}, reason={self.reason!r})'
        )

    @property
    def streamName(self) -> str:
        """
        The name of the stream, without the type suffix.
        """
        return self.__streamName

    @property
    def streamPath(self) -> Optional[str]:
        """
        The full physical stream name the value was read from.

        This is :attr:`streamName` with the type suffix of the representation
        that was actually used. It is ``None`` when no representation could be
        read.
        """
        if self.__kind in (StringStreamKind.MISSING, StringStreamKind.UNDECODABLE):
            return None
        return f'{self.__streamName}{self.__kind.value}'

    @property
    def value(self) -> Optional[str]:
        """
        The decoded value.

        ``None`` means no value could be determined; an empty string means the
        source stream was present but genuinely empty.
        """
        return self.__value

    @property
    def kind(self) -> StringStreamKind:
        """
        The physical representation the value was read from.
        """
        return self.__kind

    @property
    def encoding(self) -> Optional[str]:
        """
        The encoding used to decode the value, if one was decoded.
        """
        return self.__encoding

    @property
    def usedFallback(self) -> bool:
        """
        Whether the value came from a non-preferred representation.
        """
        return self.__usedFallback

    @property
    def missing(self) -> bool:
        """
        Whether neither the Unicode nor the legacy stream exists.
        """
        return self.__missing

    @property
    def empty(self) -> bool:
        """
        Whether the read stream existed but contained no data.
        """
        return self.__empty

    @property
    def undecodable(self) -> bool:
        """
        Whether every present stream was unreadable in its declared encoding.
        """
        return self.__undecodable

    @property
    def reason(self) -> Optional[str]:
        """
        An explanation of why the field could not be interpreted, if it could
        not be.
        """
        return self.__reason
