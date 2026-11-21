# Copyright 2026-present MongoDB, Inc.
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

"""Offline archives of raw BSON batches with per-frame packet directories.

The batches emitted by ``Collection.find_raw_batches`` are concatenated BSON
documents. Ingesting such a batch through :class:`BatchArchive` scans it once,
in order, and writes a packet directory: one :class:`FrameLocation` per BSON
frame with the frame's offset, byte length, and content digest. An offline
consumer can then reopen a confirmed directory and read a single sample by
opening only the four length-prefix bytes and the frame itself, without
loading the whole batch or every offset into memory.

A directory credential only authenticates the exact frame bytes it was sealed
with. A directory entry that happens to land on another decodable BSON
document fails verification because the digest binds the bytes to the
confirmed batch and frame position.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import struct
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from bson import decode
from bson.errors import BSONError, InvalidBSON

_DIGEST_ALGORITHM = "sha256"
_INT32_FORMAT = "<i"
_INT32_SIZE = 4
_ARCHIVE_VERSION = 1
_DIRECTORY_VERSION = 1


class BatchArchiveError(BSONError):
    """Raised when an archived batch cannot be read as directed by its directory."""


@dataclass(frozen=True)
class FrameLocation:
    """Directory entry locating one BSON frame inside a sealed batch.

    :param index: Zero-based frame position in the ingested batch.
    :param offset: Byte offset of the frame within the batch.
    :param frame_length: Declared BSON frame length, in bytes.
    :param digest: Hex digest of the frame's bytes.
    """

    index: int
    offset: int
    frame_length: int
    digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "offset": self.offset,
            "length": self.frame_length,
            _DIGEST_ALGORITHM: self.digest,
        }

    @classmethod
    def from_dict(cls, entry: dict[str, Any]) -> FrameLocation:
        return cls(
            index=entry["index"],
            offset=entry["offset"],
            frame_length=entry["length"],
            digest=entry[_DIGEST_ALGORITHM],
        )


@dataclass(frozen=True)
class PacketDirectory:
    """Confirmed packet directory for one ingested raw batch.

    :param batch_id: Identifier of the batch this directory describes.
    :param batch_length: Total length, in bytes, of the ingested batch.
    :param complete: Whether every declared frame ends exactly at the end of
        the batch. ``False`` records a broken outer envelope, such as a final
        frame whose declared length runs past the batch boundary.
    :param locations: One entry per frame discovered during the ordered scan.
    """

    batch_id: str
    batch_length: int
    complete: bool
    locations: tuple[FrameLocation, ...]

    @property
    def packet_count(self) -> int:
        return len(self.locations)

    def location(self, index: int) -> FrameLocation:
        if index < 0 or index >= len(self.locations):
            raise BatchArchiveError(f"frame {index} is not in the directory of {self.batch_id!r}")
        return self.locations[index]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": _DIRECTORY_VERSION,
            "batch_id": self.batch_id,
            "batch_length": self.batch_length,
            "complete": self.complete,
            "packet_count": len(self.locations),
            "locations": [location.to_dict() for location in self.locations],
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PacketDirectory:
        locations = tuple(FrameLocation.from_dict(entry) for entry in data["locations"])
        return cls(
            batch_id=data["batch_id"],
            batch_length=data["batch_length"],
            complete=data["complete"],
            locations=locations,
        )


def _frame_digest(frame: bytes) -> str:
    return hashlib.sha256(frame).hexdigest()


def build_packet_directory(batch_id: str, batch: bytes) -> PacketDirectory:
    """Scan a raw batch once, in order, and build its packet directory.

    The scan validates each frame's outer envelope (declared length and
    trailing null byte). A final frame whose declared length extends past the
    end of the batch is recorded with its declared offset and length and the
    directory is marked incomplete; neighboring batches are never consulted to
    fill the gap.

    :param batch_id: Identifier to bind the directory to.
    :param batch: Raw BSON batch bytes, as returned by ``find_raw_batches``.
    """
    if not isinstance(batch, bytes):
        raise TypeError("batch must be bytes")
    locations: list[FrameLocation] = []
    position = 0
    complete = True
    while position < len(batch):
        if len(batch) - position < _INT32_SIZE:
            complete = False
            break
        frame_length = struct.unpack_from(_INT32_FORMAT, batch, position)[0]
        frame_end = position + frame_length
        if frame_length < 1 or frame_end > len(batch):
            # Keep the declared location so the broken envelope is on record.
            locations.append(
                FrameLocation(
                    index=len(locations),
                    offset=position,
                    frame_length=frame_length,
                    digest=_frame_digest(batch[position:]),
                )
            )
            complete = False
            break
        frame = batch[position:frame_end]
        if frame[-1] != 0:
            raise InvalidBSON("bad eoo")
        locations.append(
            FrameLocation(
                index=len(locations),
                offset=position,
                frame_length=frame_length,
                digest=_frame_digest(frame),
            )
        )
        position = frame_end
    return PacketDirectory(
        batch_id=batch_id,
        batch_length=len(batch),
        complete=complete,
        locations=tuple(locations),
    )


@dataclass(frozen=True)
class SealedBatch:
    """A batch's original bytes together with the credentials sealed at ingest.

    :param batch_id: Identifier of the batch.
    :param data: The original raw batch bytes, unchanged.
    :param directory: The confirmed packet directory.
    :param digest: Hex digest of the full batch bytes.
    """

    batch_id: str
    data: bytes
    directory: PacketDirectory
    digest: str

    @property
    def complete(self) -> bool:
        return self.directory.complete


@dataclass(frozen=True)
class BatchVerification:
    """Outcome of verifying one frame against a confirmed directory.

    :param frame_index: Position of the verified frame.
    :param offset: Offset read from the directory.
    :param frame_length: Frame length read from the directory.
    :param envelope_complete: Whether the full frame was present and its outer
        envelope (length prefix and trailing null) was intact.
    :param digest_matched: Whether the read bytes hash to the directory digest,
        binding them to the confirmed batch and frame position.
    :param content_valid: Whether the frame bytes decode as one BSON document.
    :param error: Description of the first failed check, if any.
    :param opened_ranges: Byte ranges opened on the reader for this check.
    """

    frame_index: int
    offset: int
    frame_length: int
    envelope_complete: bool
    digest_matched: bool
    content_valid: bool
    error: str | None
    opened_ranges: tuple[tuple[int, int], ...]

    @property
    def authenticated(self) -> bool:
        """True only when the exact directory-bound frame bytes were read."""
        return self.envelope_complete and self.digest_matched


class RangeTrackingReader:
    """Binary reader that records every byte range it opens.

    Wraps a binary stream so tests and offline tools can prove that locating
    one sample only touched the directory segment and that frame's bytes.
    """

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream
        self._ranges: list[tuple[int, int]] = []

    @property
    def opened_ranges(self) -> tuple[tuple[int, int], ...]:
        return tuple(self._ranges)

    @property
    def bytes_opened(self) -> int:
        return sum(end - start for start, end in self._ranges)

    def seek(self, offset: int) -> None:
        self._stream.seek(offset)

    def tell(self) -> int:
        return self._stream.tell()

    def read(self, size: int) -> bytes:
        start = self._stream.tell()
        data = self._stream.read(size)
        if data:
            self._ranges.append((start, start + len(data)))
        return data


@dataclass
class OpenedBatch:
    """A sealed batch reopened through a specific reader and directory.

    The directory may be replaced with a captured alternative to test whether
    verification still binds the read bytes to the originally confirmed
    credentials.
    """

    sealed: SealedBatch
    reader: RangeTrackingReader
    directory: PacketDirectory = field(init=False)

    def __post_init__(self) -> None:
        self.directory = self.sealed.directory

    def replace_directory(self, directory: PacketDirectory) -> None:
        self.directory = directory

    def _read_frame(self, location: FrameLocation) -> tuple[bytes | None, str | None]:
        self.reader.seek(location.offset)
        prefix = self.reader.read(_INT32_SIZE)
        if len(prefix) != _INT32_SIZE:
            return None, "frame length prefix is cut off inside the batch"
        declared_length = struct.unpack(_INT32_FORMAT, prefix)[0]
        if declared_length != location.frame_length:
            return None, (
                f"declared frame length {declared_length} does not match directory length "
                f"{location.frame_length}"
            )
        body = self.reader.read(location.frame_length - _INT32_SIZE)
        frame = prefix + body
        if len(frame) != location.frame_length:
            return frame, "declared frame runs past the end of the batch"
        if frame[-1] != 0:
            return frame, "frame is missing its trailing null byte"
        return frame, None

    def read_frame(self, index: int) -> bytes:
        """Read one frame's bytes using only its directory entry.

        :raises BatchArchiveError: if the frame cannot be fully read within
            the batch as directed by the directory.
        """
        location = self.directory.location(index)
        frame, error = self._read_frame(location)
        if error is not None or frame is None:
            raise BatchArchiveError(error or "frame could not be read")
        return frame

    def verify_frame(self, index: int) -> BatchVerification:
        """Verify one frame without validating the rest of the batch.

        Only the directory segment and the bytes of this frame are opened; the
        unread portions of the batch are not rechecked. Decodable content from
        a foreign location does not authenticate the frame: the directory
        digest must match the read bytes.
        """
        location = self.directory.location(index)
        frame, error = self._read_frame(location)
        envelope_complete = error is None and frame is not None
        digest_matched = False
        content_valid = False
        if frame is not None:
            digest_matched = _frame_digest(frame) == location.digest
            try:
                decode(frame)
            except BSONError:
                content_valid = False
            else:
                content_valid = True
        return BatchVerification(
            frame_index=index,
            offset=location.offset,
            frame_length=location.frame_length,
            envelope_complete=envelope_complete,
            digest_matched=digest_matched,
            content_valid=content_valid,
            error=error,
            opened_ranges=self.reader.opened_ranges,
        )


class BatchArchive:
    """An offline archive of raw BSON batches and their packet directories."""

    def __init__(self) -> None:
        self._batches: dict[str, SealedBatch] = {}

    @property
    def batch_ids(self) -> tuple[str, ...]:
        return tuple(self._batches)

    def seal_batch(self, batch_id: str, batch: bytes) -> SealedBatch:
        """Ingest one raw batch, preserving its bytes and directory credentials."""
        directory = build_packet_directory(batch_id, batch)
        sealed = SealedBatch(
            batch_id=batch_id,
            data=batch,
            directory=directory,
            digest=_frame_digest(batch),
        )
        self._batches[batch_id] = sealed
        return sealed

    def get(self, batch_id: str) -> SealedBatch:
        try:
            return self._batches[batch_id]
        except KeyError:
            raise BatchArchiveError(f"batch {batch_id!r} is not in the archive") from None

    def open(self, batch_id: str, reader: RangeTrackingReader | None = None) -> OpenedBatch:
        """Reopen a sealed batch, optionally through a range-tracking reader."""
        sealed = self.get(batch_id)
        if reader is None:
            reader = RangeTrackingReader(io.BytesIO(sealed.data))
        return OpenedBatch(sealed=sealed, reader=reader)

    def save(self, path: str | Path) -> None:
        """Persist the original batch bytes and sealed directory credentials."""
        payload = {
            "version": _ARCHIVE_VERSION,
            "batches": {
                batch_id: {
                    "data": base64.b64encode(sealed.data).decode("ascii"),
                    "digest": sealed.digest,
                    "directory": sealed.directory.to_dict(),
                }
                for batch_id, sealed in self._batches.items()
            },
        }
        Path(path).write_text(json.dumps(payload), encoding="utf-8")

    @classmethod
    def load(cls, path: str | Path) -> BatchArchive:
        """Reload an archive written by :meth:`save` and recheck every digest."""
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        archive = cls()
        for batch_id, record in payload["batches"].items():
            data = base64.b64decode(record["data"])
            sealed = SealedBatch(
                batch_id=batch_id,
                data=data,
                directory=PacketDirectory.from_dict(record["directory"]),
                digest=record["digest"],
            )
            if _frame_digest(data) != sealed.digest:
                raise BatchArchiveError(f"stored bytes of {batch_id!r} no longer match its digest")
            archive._batches[batch_id] = sealed
        return archive
