"""Manifest model: trustworthy record boundaries published by the archive writer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ManifestEntry:
    """Boundary and digest for one BSON record, as published by the writer.

    :param seq: sequence number of the record within the archive.
    :param offset: byte offset of the record within the fetch.
    :param length: byte length of the record.
    :param sha256: hex SHA-256 digest of the record bytes, or ``None`` when
        the writer did not publish a digest (the record is then undeliverable).
    """

    seq: int
    offset: int
    length: int
    sha256: str | None

    @property
    def end(self) -> int:
        """Byte offset one past the end of the record."""
        return self.offset + self.length

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "offset": self.offset,
            "length": self.length,
            "sha256": self.sha256,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ManifestEntry:
        return cls(
            seq=int(raw["seq"]),
            offset=int(raw["offset"]),
            length=int(raw["length"]),
            sha256=raw.get("sha256"),
        )


@dataclass(frozen=True)
class Manifest:
    """A versioned list of record boundaries for one archive.

    :param manifest_id: identifier of this revision of the manifest.
    :param entries: the published record entries.
    """

    manifest_id: str
    entries: tuple[ManifestEntry, ...]

    def entry_for(self, seq: int) -> ManifestEntry | None:
        """Return the entry for *seq*, or ``None`` if the manifest has none."""
        for entry in self.entries:
            if entry.seq == seq:
                return entry
        return None

    def proves(self, seq: int, offset: int, length: int, sha256: str) -> bool:
        """Return True if this manifest still vouches for the given boundary.

        A previously delivered record is only considered proven by a new
        manifest if the manifest carries an entry with the identical
        sequence number, byte range, and digest.
        """
        entry = self.entry_for(seq)
        return entry is not None and (entry.offset, entry.length, entry.sha256) == (
            offset,
            length,
            sha256,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "manifest_id": self.manifest_id,
            "entries": [entry.to_dict() for entry in self.entries],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> Manifest:
        return cls(
            manifest_id=str(raw["manifest_id"]),
            entries=tuple(ManifestEntry.from_dict(e) for e in raw["entries"]),
        )
