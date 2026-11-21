"""Incremental inventory of objects in the OpenSky S3 hourly archive.

The first scan of an hour prefix often runs before all objects are
published, and publishers may replace an existing key with a new version.
This module keeps track, for each hour prefix, of:

- **scans**: every listing attempt, whether it completed, and which object
  versions it returned (a failed pagination keeps the objects already seen
  but is never marked complete);
- **discovered**: all object versions ever seen, identified by
  (key, etag, version_id) so a replaced key yields a new entry;
- **confirmations**: versions that have been downloaded and confirmed;
- **pending**: discovered versions still worth entering the download queue.

When a listing is incomplete, previously discovered objects are kept and
unseen objects are *not* declared nonexistent. When a listing is complete
(including empty ones), only the versions it returned remain candidates for
the download queue; a queued version that is no longer obtainable is
reported as missing rather than silently replaced by another version.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from minio import datatypes
from minio.error import S3Error

from .s3 import S3Client, object_prefix
from .time import timelike

_log = logging.getLogger(__name__)


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fingerprint(key: str, etag: str | None, version_id: str | None) -> str:
    return f"{key}@{etag}:{version_id}"


@dataclass
class ObjectVersion:
    """One distinguishable version of an object in the archive."""

    bucket: str
    key: str
    etag: str | None
    version_id: str | None
    size: int | None
    last_modified: str | None

    @classmethod
    def from_object(cls, obj: datatypes.Object) -> ObjectVersion:
        assert obj.object_name is not None
        return cls(
            bucket=obj.bucket_name,
            key=obj.object_name,
            etag=obj.etag,
            version_id=obj.version_id,
            size=obj.size,
            last_modified=obj.last_modified.isoformat()
            if obj.last_modified is not None
            else None,
        )

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.key, self.etag, self.version_id)

    def to_object(self) -> datatypes.Object:
        return datatypes.Object(
            self.bucket,
            self.key,
            etag=self.etag,
            size=self.size,
            version_id=self.version_id,
        )


@dataclass
class ScanRecord:
    """One listing attempt on an hour prefix."""

    prefix: str
    timestamp: str
    complete: bool
    error: str | None
    fingerprints: list[str] = field(default_factory=list)


@dataclass
class Confirmation:
    """Record that a specific object version has been downloaded."""

    key: str
    etag: str | None
    version_id: str | None
    path: str
    timestamp: str

    @property
    def fingerprint(self) -> str:
        return _fingerprint(self.key, self.etag, self.version_id)


@dataclass
class HourRecord:
    prefix: str
    scans: list[ScanRecord] = field(default_factory=list)
    discovered: dict[str, ObjectVersion] = field(default_factory=dict)
    confirmations: list[Confirmation] = field(default_factory=list)


@dataclass
class FetchReport:
    """Outcome of handing pending versions to the downloader."""

    downloaded: list[Path] = field(default_factory=list)
    missing: list[ObjectVersion] = field(default_factory=list)


class Inventory:
    """Persistent, version-aware inventory of hourly archive objects."""

    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.hours: dict[str, HourRecord] = {}
        if self.path.exists():
            raw = json.loads(self.path.read_text())
            for prefix, record in raw.items():
                self.hours[prefix] = HourRecord(
                    prefix=prefix,
                    scans=[ScanRecord(**scan) for scan in record["scans"]],
                    discovered={
                        fp: ObjectVersion(**version)
                        for fp, version in record["discovered"].items()
                    },
                    confirmations=[
                        Confirmation(**confirmation)
                        for confirmation in record["confirmations"]
                    ],
                )

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        raw = {
            prefix: {
                "scans": [asdict(scan) for scan in record.scans],
                "discovered": {
                    fp: asdict(version)
                    for fp, version in record.discovered.items()
                },
                "confirmations": [
                    asdict(confirmation)
                    for confirmation in record.confirmations
                ],
            }
            for prefix, record in self.hours.items()
        }
        self.path.write_text(json.dumps(raw, indent=2))

    def _record(self, hour: timelike, table: str, folder: str) -> HourRecord:
        prefix = object_prefix(hour, table=table, folder=folder)
        if prefix not in self.hours:
            self.hours[prefix] = HourRecord(prefix=prefix)
        return self.hours[prefix]

    def scan(
        self,
        client: S3Client,
        hour: timelike,
        table: str = "state_vectors",
        folder: str = "tables_v4",
        **kwargs: Any,
    ) -> ScanRecord:
        """List one hour prefix and merge the result into the inventory.

        Objects seen before a pagination failure are kept, but the scan is
        marked incomplete: unseen objects are not declared nonexistent and
        all previously discovered versions remain download candidates.
        """
        record = self._record(hour, table, folder)
        seen: list[ObjectVersion] = []
        error: str | None = None
        try:
            for obj in client.list_objects(
                hour, table=table, folder=folder, **kwargs
            ):
                seen.append(ObjectVersion.from_object(obj))
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            _log.warning(
                f"Incomplete listing of {record.prefix}: {error} "
                f"({len(seen)} objects kept)"
            )
        for version in seen:
            record.discovered[version.fingerprint] = version
        scan = ScanRecord(
            prefix=record.prefix,
            timestamp=_utcnow(),
            complete=error is None,
            error=error,
            fingerprints=[version.fingerprint for version in seen],
        )
        record.scans.append(scan)
        self.save()
        return scan

    def scans(
        self,
        hour: timelike,
        table: str = "state_vectors",
        folder: str = "tables_v4",
    ) -> list[ScanRecord]:
        """All listing attempts recorded for one hour prefix."""
        return list(self._record(hour, table, folder).scans)

    def is_listing_complete(
        self,
        hour: timelike,
        table: str = "state_vectors",
        folder: str = "tables_v4",
    ) -> bool:
        """Whether the most recent scan of the hour prefix completed."""
        record = self._record(hour, table, folder)
        return len(record.scans) > 0 and record.scans[-1].complete

    def discovered(
        self,
        hour: timelike,
        table: str = "state_vectors",
        folder: str = "tables_v4",
    ) -> list[ObjectVersion]:
        """All object versions ever seen on the hour prefix."""
        return list(self._record(hour, table, folder).discovered.values())

    def confirmed(
        self,
        hour: timelike,
        table: str = "state_vectors",
        folder: str = "tables_v4",
    ) -> list[Confirmation]:
        """All download confirmations recorded for the hour prefix."""
        return list(self._record(hour, table, folder).confirmations)

    def pending(
        self,
        hour: timelike,
        table: str = "state_vectors",
        folder: str = "tables_v4",
    ) -> list[ObjectVersion]:
        """Discovered versions still worth entering the download queue.

        After a complete scan, only the versions it returned are
        candidates. After an incomplete scan, all discovered unconfirmed
        versions remain candidates. A replaced key yields a new candidate:
        confirmations are matched by version, never by key alone.
        """
        record = self._record(hour, table, folder)
        if not record.scans:
            return []
        last = record.scans[-1]
        if last.complete:
            candidates = [
                record.discovered[fp]
                for fp in last.fingerprints
                if fp in record.discovered
            ]
        else:
            candidates = list(record.discovered.values())
        done = {
            confirmation.fingerprint for confirmation in record.confirmations
        }
        return [v for v in candidates if v.fingerprint not in done]

    def confirm(
        self,
        hour: timelike,
        version: ObjectVersion,
        path: Path | str,
        table: str = "state_vectors",
        folder: str = "tables_v4",
    ) -> Confirmation:
        """Record that a specific object version has been downloaded."""
        record = self._record(hour, table, folder)
        confirmation = Confirmation(
            key=version.key,
            etag=version.etag,
            version_id=version.version_id,
            path=str(path),
            timestamp=_utcnow(),
        )
        record.confirmations.append(confirmation)
        self.save()
        return confirmation

    def is_available(self, client: S3Client, version: ObjectVersion) -> bool:
        """Whether this exact version can still be fetched from S3."""
        try:
            stat = client.s3client.stat_object(
                version.bucket, version.key, version_id=version.version_id
            )
        except S3Error:
            return False
        return version.etag is None or stat.etag == version.etag

    def fetch_pending(
        self,
        client: S3Client,
        hour: timelike,
        directory: Path | str,
        table: str = "state_vectors",
        folder: str = "tables_v4",
    ) -> FetchReport:
        """Download pending versions, reporting those no longer obtainable.

        A queued version that cannot be fetched any more (replaced or
        deleted by the publisher) is reported in
        :attr:`FetchReport.missing`; no other version is silently archived
        under its key.
        """
        report = FetchReport()
        for version in self.pending(hour, table=table, folder=folder):
            if not self.is_available(client, version):
                _log.warning(
                    f"Version {version.fingerprint} is no longer available"
                )
                report.missing.append(version)
                continue
            path = client.download_object(version.to_object(), Path(directory))
            self.confirm(hour, version, path, table=table, folder=folder)
            report.downloaded.append(path)
        return report
