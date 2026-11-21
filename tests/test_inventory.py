"""Inventory scans against a fake local S3 backend (no network)."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, cast

import pytest
from minio import Minio, datatypes
from minio.error import S3Error

from pyopensky.inventory import Inventory, ObjectVersion
from pyopensky.s3 import S3Client

BUCKET = "opensky-hdfs-backup"
HOUR1, HOUR2, HOUR3 = (
    "2022-07-07 18:00Z",
    "2022-07-07 19:00Z",
    "2022-07-07 20:00Z",
)
PREFIX1 = "tables_v4/state_vectors/hour=1657216800"
PREFIX2 = "tables_v4/state_vectors/hour=1657220400"
PREFIX3 = "tables_v4/state_vectors/hour=1657224000"

KEY_A = f"{PREFIX2}/part-r-00001.snappy"
KEY_B = f"{PREFIX2}/part-r-00002.snappy"
KEY_C = f"{PREFIX3}/part-r-00003.snappy"
KEY_1 = f"{PREFIX1}/part-r-00004.snappy"


def make_obj(
    key: str, etag: str, size: int = 10, version_id: str | None = None
) -> datatypes.Object:
    return datatypes.Object(
        BUCKET,
        key,
        last_modified=datetime(2022, 7, 7, 23, 59, tzinfo=timezone.utc),
        etag=etag,
        size=size,
        version_id=version_id,
    )


class FakeMinio(Minio):
    """Scripted local stand-in for the Minio client."""

    def __init__(self) -> None:
        self.listing_calls: list[tuple[str, str | None]] = []
        self.scripts: dict[str, list[list[Any]]] = {}
        self.stat_results: dict[
            tuple[str, str, str | None], datatypes.Object
        ] = {}
        self.get_calls: list[tuple[str, str]] = []
        self.content: dict[str, bytes] = {}

    def list_objects(
        self,
        bucket_name: str,
        prefix: str | None = None,
        recursive: bool = False,
        **kwargs: Any,
    ) -> Iterator[datatypes.Object]:
        self.listing_calls.append((bucket_name, prefix))
        script = self.scripts[cast(str, prefix)].pop(0)
        for item in script:
            if isinstance(item, Exception):
                raise item
            yield item

    def stat_object(
        self, bucket_name: str, object_name: str, **kwargs: Any
    ) -> datatypes.Object:
        version_id = kwargs.get("version_id")
        key = (bucket_name, object_name, version_id)
        if key not in self.stat_results:
            raise S3Error(
                cast(Any, None),
                "NoSuchKey",
                "Object does not exist",
                object_name,
                None,
                None,
                bucket_name=bucket_name,
                object_name=object_name,
            )
        return self.stat_results[key]

    def get_object(
        self,
        bucket_name: str,
        object_name: str,
        offset: int = 0,
        length: int | None = None,
        **kwargs: Any,
    ) -> Any:
        self.get_calls.append((bucket_name, object_name))
        data = self.content[object_name]
        end = None if length is None else offset + length
        return SimpleNamespace(data=data[offset:end])


@pytest.fixture
def fake() -> FakeMinio:
    return FakeMinio()


@pytest.fixture
def client(fake: FakeMinio) -> S3Client:
    instance = S3Client.__new__(S3Client)
    instance.s3client = fake
    return instance


def fingerprints(versions: list[ObjectVersion]) -> set[str]:
    return {version.fingerprint for version in versions}


def test_scan_records_actual_hour_prefixes(
    tmp_path: Path, fake: FakeMinio, client: S3Client
) -> None:
    fake.scripts = {PREFIX1: [[]], PREFIX2: [[]], PREFIX3: [[]]}
    inventory = Inventory(tmp_path / "inventory.json")
    for hour in (HOUR1, HOUR2, HOUR3):
        scan = inventory.scan(client, hour)
        assert scan.complete
    assert fake.listing_calls == [
        (BUCKET, PREFIX1),
        (BUCKET, PREFIX2),
        (BUCKET, PREFIX3),
    ]


def test_pagination_failure_keeps_partial_listing(
    tmp_path: Path, fake: FakeMinio, client: S3Client
) -> None:
    obj_a, obj_b = make_obj(KEY_A, "etag-a"), make_obj(KEY_B, "etag-b")
    fake.scripts = {
        PREFIX2: [[obj_a, RuntimeError("connection reset")], [obj_a, obj_b]]
    }
    inventory = Inventory(tmp_path / "inventory.json")

    failed = inventory.scan(client, HOUR2)
    assert not failed.complete
    assert failed.error is not None and "connection reset" in failed.error
    assert not inventory.is_listing_complete(HOUR2)
    # The object seen before the failure is kept...
    assert fingerprints(inventory.discovered(HOUR2)) == {f"{KEY_A}@etag-a:None"}
    # ...and remains worth downloading, without declaring the hour complete.
    assert fingerprints(inventory.pending(HOUR2)) == {f"{KEY_A}@etag-a:None"}

    completed = inventory.scan(client, HOUR2)
    assert completed.complete
    assert inventory.is_listing_complete(HOUR2)
    assert fingerprints(inventory.pending(HOUR2)) == {
        f"{KEY_A}@etag-a:None",
        f"{KEY_B}@etag-b:None",
    }
    # Both attempts remain queryable, with what each one saw.
    first, second = inventory.scans(HOUR2)
    assert not first.complete and first.fingerprints == [f"{KEY_A}@etag-a:None"]
    assert second.complete and len(second.fingerprints) == 2


def test_empty_complete_listing_then_late_arrival(
    tmp_path: Path, fake: FakeMinio, client: S3Client
) -> None:
    obj_c = make_obj(KEY_C, "etag-c")
    fake.scripts = {PREFIX3: [[], [obj_c]]}
    inventory = Inventory(tmp_path / "inventory.json")

    scan = inventory.scan(client, HOUR3)
    assert scan.complete and scan.fingerprints == []
    assert inventory.is_listing_complete(HOUR3)
    assert inventory.discovered(HOUR3) == []
    assert inventory.pending(HOUR3) == []

    inventory.scan(client, HOUR3)
    assert fingerprints(inventory.pending(HOUR3)) == {f"{KEY_C}@etag-c:None"}


def test_replaced_key_keeps_confirmation_and_proposes_new_version(
    tmp_path: Path, fake: FakeMinio, client: S3Client
) -> None:
    obj_v1 = make_obj(KEY_1, "etag-v1")
    obj_v2 = make_obj(KEY_1, "etag-v2")
    fake.scripts = {PREFIX1: [[obj_v1], [obj_v2]]}
    fake.stat_results[(BUCKET, KEY_1, None)] = obj_v1
    fake.content[KEY_1] = b"v1 payload"
    inventory = Inventory(tmp_path / "inventory.json")

    inventory.scan(client, HOUR1)
    report = inventory.fetch_pending(client, HOUR1, tmp_path / "dl")
    assert len(report.downloaded) == 1 and report.missing == []
    confirmed = inventory.confirmed(HOUR1)
    assert len(confirmed) == 1 and confirmed[0].etag == "etag-v1"
    assert inventory.pending(HOUR1) == []

    # The publisher replaces the key with a distinguishable new version.
    inventory.scan(client, HOUR1)
    # The original confirmation is kept...
    assert [c.etag for c in inventory.confirmed(HOUR1)] == ["etag-v1"]
    # ...both versions are discovered...
    assert fingerprints(inventory.discovered(HOUR1)) == {
        f"{KEY_1}@etag-v1:None",
        f"{KEY_1}@etag-v2:None",
    }
    # ...and the new version is pending: the key alone must not skip it.
    assert fingerprints(inventory.pending(HOUR1)) == {f"{KEY_1}@etag-v2:None"}

    # The state survives a reload from disk.
    reloaded = Inventory(tmp_path / "inventory.json")
    assert fingerprints(reloaded.pending(HOUR1)) == {f"{KEY_1}@etag-v2:None"}
    assert [c.etag for c in reloaded.confirmed(HOUR1)] == ["etag-v1"]


def test_unobtainable_target_version_is_reported_missing(
    tmp_path: Path, fake: FakeMinio, client: S3Client
) -> None:
    obj_v1 = make_obj(KEY_1, "etag-v1")
    obj_v2 = make_obj(KEY_1, "etag-v2")
    fake.scripts = {PREFIX1: [[obj_v1], [obj_v2]]}
    fake.stat_results[(BUCKET, KEY_1, None)] = obj_v1
    fake.content[KEY_1] = b"v1 payload"
    inventory = Inventory(tmp_path / "inventory.json")

    inventory.scan(client, HOUR1)
    inventory.fetch_pending(client, HOUR1, tmp_path / "dl")
    inventory.scan(client, HOUR1)
    assert fingerprints(inventory.pending(HOUR1)) == {f"{KEY_1}@etag-v2:None"}

    # The key now serves yet another version: v2 is no longer obtainable.
    fake.stat_results[(BUCKET, KEY_1, None)] = make_obj(KEY_1, "etag-v3")
    fake.get_calls.clear()
    report = inventory.fetch_pending(client, HOUR1, tmp_path / "dl")
    assert report.downloaded == []
    assert fingerprints(report.missing) == {f"{KEY_1}@etag-v2:None"}
    # No other version is silently archived under the same key.
    assert fake.get_calls == []
    assert [c.etag for c in inventory.confirmed(HOUR1)] == ["etag-v1"]

    # Same outcome when the key has disappeared altogether.
    del fake.stat_results[(BUCKET, KEY_1, None)]
    report = inventory.fetch_pending(client, HOUR1, tmp_path / "dl")
    assert fingerprints(report.missing) == {f"{KEY_1}@etag-v2:None"}
    assert fake.get_calls == []
