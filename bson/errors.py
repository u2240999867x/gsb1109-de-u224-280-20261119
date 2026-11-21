# Copyright 2009-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Exceptions raised by the BSON package."""

from __future__ import annotations

from typing import Any


class BSONError(Exception):
    """Base class for all BSON exceptions."""


class InvalidBSON(BSONError):
    """Raised when trying to create a BSON object from invalid data."""


class BSONPathError(BSONError):
    """Base class for selective raw BSON path errors.

    Instances expose the requested path, the path prefixes that were safely
    checked, and the byte offset where the lookup stopped.

    .. versionadded:: 4.21
    """

    def __init__(
        self,
        message: str,
        *,
        path: tuple[str | int, ...] = (),
        checked_paths: tuple[tuple[str | int, ...], ...] = (),
        segment: str | int | None = None,
        segment_index: int | None = None,
        byte_offset: int | None = None,
        reason: str | None = None,
    ) -> None:
        super().__init__(message)
        self.path = path
        self.checked_paths = checked_paths
        self.segment = segment
        self.segment_index = segment_index
        self.byte_offset = byte_offset
        self.reason = reason


class InvalidBSONPath(BSONPathError, InvalidBSON):
    """Raised when a selective raw BSON path cannot be traversed safely.

    Unlike :class:`InvalidBSON`, this error does not imply that the document
    is invalid. It means that the lookup could not confirm the boundary of an
    element without decoding it.

    .. versionadded:: 4.21
    """


class BSONPathNotFound(BSONPathError, KeyError):
    """Raised when a selective raw BSON path does not exist.

    .. versionadded:: 4.21
    """


class BSONPathTypeError(BSONPathError, TypeError):
    """Raised when a path segment does not match the encountered container.

    For example, an integer segment used against an embedded document or a
    string segment used against an array.

    .. versionadded:: 4.21
    """


RawBSONPathError = InvalidBSONPath
"""Alias for :class:`InvalidBSONPath`."""

RawBSONPathNotFound = BSONPathNotFound
"""Alias for :class:`BSONPathNotFound`."""

RawBSONPathTypeError = BSONPathTypeError
"""Alias for :class:`BSONPathTypeError`."""


class InvalidStringData(BSONError):
    """Raised when trying to encode a string containing non-UTF8 data."""


class InvalidDocument(BSONError):
    """Raised when trying to create a BSON object from an invalid document."""

    def __init__(self, message: str, document: Any | None = None) -> None:
        super().__init__(message)
        self._document = document

    @property
    def document(self) -> Any:
        """The invalid document that caused the error.

        .. versionadded:: 4.16
        """
        return self._document


class InvalidId(BSONError):
    """Raised when trying to create an ObjectId from invalid data."""
