"""Persistent progress state (the *credentials*) for the archive reader.

Every record kept in the state carries its sequence number, its actual byte
range, and the identifiers of the manifest revision and the fetch it came
from, so a later read can verify that nothing confirmed was silently swapped.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class DeliveredRecord:
    """A record that has been delivered and is part of the credentials.

    :param seq: sequence number of the record.
    :param offset: byte offset of the record within the fetch.
    :param length: byte length of the record.
    :param sha256: hex SHA-256 digest of the delivered bytes.
    :param manifest_id: manifest revision that proved this boundary.
    :param fetch_id: fetch the bytes were read from.
    """

    seq: int
    offset: int
    length: int
    sha256: str
    manifest_id: str
    fetch_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "offset": self.offset,
            "length": self.length,
            "sha256": self.sha256,
            "manifest_id": self.manifest_id,
            "fetch_id": self.fetch_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> DeliveredRecord:
        return cls(
            seq=int(raw["seq"]),
            offset=int(raw["offset"]),
            length=int(raw["length"]),
            sha256=str(raw["sha256"]),
            manifest_id=str(raw["manifest_id"]),
            fetch_id=str(raw["fetch_id"]),
        )


@dataclass(frozen=True)
class PendingRecord:
    """A record whose byte range is only partially received so far.

    :param seq: sequence number of the record.
    :param offset: byte offset of the record within the fetch.
    :param length: full byte length of the record per the manifest.
    :param received: how many bytes of the range are present in the fetch.
    :param manifest_id: manifest revision that published this boundary.
    :param fetch_id: fetch the partial bytes belong to.
    """

    seq: int
    offset: int
    length: int
    received: int
    manifest_id: str
    fetch_id: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "offset": self.offset,
            "length": self.length,
            "received": self.received,
            "manifest_id": self.manifest_id,
            "fetch_id": self.fetch_id,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PendingRecord:
        return cls(
            seq=int(raw["seq"]),
            offset=int(raw["offset"]),
            length=int(raw["length"]),
            received=int(raw["received"]),
            manifest_id=str(raw["manifest_id"]),
            fetch_id=str(raw["fetch_id"]),
        )


class ProgressState:
    """Resumable credentials for one fetch of an archive.

    The state is bound to a single ``fetch_id``. It records which records
    were delivered (with their byte ranges, digests, and proving manifest)
    and which records are still waiting for more bytes.
    """

    def __init__(
        self,
        fetch_id: str,
        fetch_name: str,
        delivered: dict[int, DeliveredRecord] | None = None,
        pending: dict[int, PendingRecord] | None = None,
    ) -> None:
        self.fetch_id = fetch_id
        self.fetch_name = fetch_name
        self.delivered: dict[int, DeliveredRecord] = dict(delivered or {})
        self.pending: dict[int, PendingRecord] = dict(pending or {})

    def to_dict(self) -> dict[str, Any]:
        return {
            "fetch_id": self.fetch_id,
            "fetch_name": self.fetch_name,
            "delivered": [
                record.to_dict() for record in sorted(self.delivered.values(), key=_seq)
            ],
            "pending": [
                record.to_dict() for record in sorted(self.pending.values(), key=_seq)
            ],
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ProgressState:
        delivered = {int(r["seq"]): DeliveredRecord.from_dict(r) for r in raw["delivered"]}
        pending = {int(r["seq"]): PendingRecord.from_dict(r) for r in raw["pending"]}
        return cls(
            fetch_id=str(raw["fetch_id"]),
            fetch_name=str(raw["fetch_name"]),
            delivered=delivered,
            pending=pending,
        )

    def save(self, path: str | Path) -> None:
        """Persist the credentials to *path* as JSON."""
        Path(path).write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))

    @classmethod
    def load(cls, path: str | Path) -> ProgressState:
        """Load credentials previously written with :meth:`save`."""
        return cls.from_dict(json.loads(Path(path).read_text()))

    def __repr__(self) -> str:
        return (
            f"ProgressState(fetch_id={self.fetch_id!r}, fetch_name={self.fetch_name!r}, "
            f"delivered={sorted(self.delivered)}, pending={sorted(self.pending)})"
        )


def _seq(record: DeliveredRecord | PendingRecord) -> int:
    return record.seq
