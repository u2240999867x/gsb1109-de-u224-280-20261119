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
from __future__ import annotations

import base64
import io
import json
import os
import struct
import sys
import tempfile
import unittest

sys.path[0:0] = [""]

from bson import decode, decode_all, encode
from bson.batch_archive import (
    BatchArchive,
    BatchArchiveError,
    FrameLocation,
    PacketDirectory,
    RangeTrackingReader,
    build_packet_directory,
)
from bson.errors import InvalidBSON


def frame_length(frame: bytes, index: int = 0) -> int:
    return struct.unpack_from("<i", frame, index)[0]


def doc(index: int, payload_size: int = 16) -> dict:
    return {"_id": index, "payload": "x" * payload_size, "tag": f"doc-{index}"}


def small_doc(index: int) -> dict:
    return {"_id": index, "n": index, "blob": "z" * 256}


def with_corrupt_content(frame: bytes) -> bytes:
    """Flip an element-type byte while preserving the outer envelope."""
    corrupted = bytearray(frame)
    # The first element follows the four-byte length prefix.
    corrupted[4] = 0x7F
    return bytes(corrupted)


def filler_document(target_length: int) -> dict:
    """Build a filler document whose encoded size is exactly target_length."""
    overhead = len(encode({"_id": 2, "filler": ""}))
    return {"_id": 2, "filler": "y" * (target_length - overhead)}


class TestBatchArchive(unittest.TestCase):
    def setUp(self) -> None:
        # First batch: two complete frames, then a third frame whose declared
        # length runs past the end of this batch.
        first_two = encode(doc(0)) + encode(doc(1))
        oversized = encode(doc(2, payload_size=4096))
        self.batch1 = first_two + oversized[:12]

        # Second batch starts with a frame whose size arithmetically fills the
        # bytes missing past the end of batch 1.
        declared = frame_length(self.batch1, len(first_two))
        present = len(self.batch1) - len(first_two)
        gap = declared - present
        self.filler_doc = filler_document(gap)
        filler = encode(self.filler_doc)
        self.assertEqual(len(filler), gap)
        self.batch2 = filler + encode(doc(3)) + encode(doc(4))

        # A larger, fully complete batch. Its first document is identical to
        # the first document of batch 1; the recapture has the same size and
        # packet count, with only the later samples differing. Frames at index
        # 1 and 2 share a length so swapped directory ranges stay decodable.
        self.batch3 = encode(doc(0)) + encode(small_doc(10)) + encode(small_doc(20))
        self.batch3_alt = encode(doc(0)) + encode(small_doc(11)) + encode(small_doc(21))
        self.assertEqual(len(self.batch3), len(self.batch3_alt))
        self.assertEqual(
            len(build_packet_directory("probe", self.batch3).locations),
            len(build_packet_directory("probe", self.batch3_alt).locations),
        )
        self.assertEqual(self.batch3[: len(encode(doc(0)))], encode(doc(0)))
        self.assertNotEqual(self.batch3, self.batch3_alt)

        self.archive = BatchArchive()
        self.sealed1 = self.archive.seal_batch("batch-1", self.batch1)
        self.sealed2 = self.archive.seal_batch("batch-2", self.batch2)
        self.sealed3 = self.archive.seal_batch("batch-3", self.batch3)
        self.assertGreater(len(self.batch3), len(self.batch1))

    def test_ingest_records_packet_directories(self) -> None:
        directory1 = self.sealed1.directory
        self.assertFalse(directory1.complete)
        self.assertEqual(3, directory1.packet_count)
        first = directory1.locations[0]
        second = directory1.locations[1]
        third = directory1.locations[2]
        self.assertEqual((0, len(encode(doc(0)))), (first.offset, first.frame_length))
        self.assertEqual(first.offset + first.frame_length, second.offset)
        self.assertEqual(second.offset + second.frame_length, third.offset)
        # The broken third envelope is kept on record with its overshooting length.
        self.assertGreater(third.offset + third.frame_length, len(self.batch1))

        self.assertTrue(self.sealed2.directory.complete)
        self.assertEqual(3, self.sealed2.directory.packet_count)
        self.assertTrue(self.sealed3.directory.complete)
        self.assertEqual(3, self.sealed3.directory.packet_count)

    def test_original_bytes_are_preserved(self) -> None:
        self.assertEqual(self.batch1, self.sealed1.data)
        self.assertEqual(self.batch2, self.sealed2.data)
        self.assertEqual(self.batch3, self.sealed3.data)
        # Plain find_raw_batches output still decodes for complete batches.
        self.assertEqual([self.filler_doc, doc(3), doc(4)], decode_all(self.batch2))

    def test_first_frame_of_first_batch_then_second_batch(self) -> None:
        opened1 = self.archive.open("batch-1")
        first_frame = opened1.read_frame(0)
        self.assertEqual(encode(doc(0)), first_frame)
        self.assertEqual(doc(0), decode(first_frame))

        opened2 = self.archive.open("batch-2")
        self.assertEqual(self.filler_doc, decode(opened2.read_frame(0)))
        self.assertEqual(doc(3), decode(opened2.read_frame(1)))

        # The truncated third frame of batch 1 cannot be completed from batch 2.
        with self.assertRaises(BatchArchiveError):
            opened1.read_frame(2)

    def test_reopen_selects_later_sample_with_small_ranges(self) -> None:
        reader = RangeTrackingReader(io.BytesIO(self.sealed3.data))
        opened = self.archive.open("batch-3", reader)
        result = opened.verify_frame(2)
        self.assertTrue(result.authenticated)
        self.assertTrue(result.envelope_complete)
        self.assertTrue(result.digest_matched)
        self.assertTrue(result.content_valid)

        location = self.sealed3.directory.location(2)
        # Only the four-byte directory segment and this frame's bytes opened.
        self.assertEqual(
            (
                (location.offset, location.offset + 4),
                (location.offset + 4, location.offset + location.frame_length),
            ),
            result.opened_ranges,
        )
        self.assertEqual(location.frame_length, reader.bytes_opened)
        self.assertLess(reader.bytes_opened, len(self.batch3))
        self.assertEqual(small_doc(20), decode(opened.read_frame(2)))

    def test_verification_only_covers_the_read_frame(self) -> None:
        reader = RangeTrackingReader(io.BytesIO(self.sealed1.data))
        opened = self.archive.open("batch-1", reader)
        result = opened.verify_frame(1)
        self.assertTrue(result.authenticated)
        self.assertEqual(doc(1), decode(opened.read_frame(1)))
        # Nothing beyond frame 1 was opened, so the broken frame 2 was not rechecked.
        last_opened_end = max(end for _, end in reader.opened_ranges)
        expected_end = self.sealed1.directory.location(1).offset + result.frame_length
        self.assertEqual(expected_end, last_opened_end)

        broken = opened.verify_frame(2)
        self.assertFalse(broken.envelope_complete)
        self.assertFalse(broken.authenticated)
        self.assertIsNotNone(broken.error)

    def test_swapped_directory_segment_does_not_authenticate(self) -> None:
        # A new capture with the same outer shape supplies the directory
        # segment for the same ordinal position.
        recapture_directory = build_packet_directory("batch-3", self.batch3_alt)
        reader = RangeTrackingReader(io.BytesIO(self.sealed3.data))
        opened = self.archive.open("batch-3", reader)
        opened.replace_directory(recapture_directory)

        result = opened.verify_frame(1)
        # The foreign entry still lands on a decodable document...
        self.assertTrue(result.content_valid)
        # ...but the digest no longer binds the bytes to a confirmed batch.
        self.assertFalse(result.digest_matched)
        self.assertFalse(result.authenticated)

    def test_swapped_frame_ranges_still_decode_but_fail_authentication(self) -> None:
        directory = self.sealed3.directory
        location1 = directory.location(1)
        location2 = directory.location(2)
        # The two legal frames share a length so swapped ranges stay decodable.
        self.assertEqual(location1.frame_length, location2.frame_length)
        # Swap only the intervals; each ordinal keeps its confirmed digest so
        # the credential remains bound to the original frame position.
        swapped_locations = list(directory.locations)
        swapped_locations[1] = FrameLocation(
            index=1,
            offset=location2.offset,
            frame_length=location2.frame_length,
            digest=location1.digest,
        )
        swapped_locations[2] = FrameLocation(
            index=2,
            offset=location1.offset,
            frame_length=location1.frame_length,
            digest=location2.digest,
        )
        swapped = PacketDirectory(
            batch_id=directory.batch_id,
            batch_length=directory.batch_length,
            complete=directory.complete,
            locations=tuple(swapped_locations),
        )

        reader = RangeTrackingReader(io.BytesIO(self.sealed3.data))
        opened = self.archive.open("batch-3", reader)
        opened.replace_directory(swapped)
        for index in (1, 2):
            result = opened.verify_frame(index)
            self.assertTrue(result.envelope_complete)
            self.assertTrue(result.content_valid)
            self.assertFalse(result.digest_matched)
            self.assertFalse(result.authenticated)

    def test_content_corruption_is_separate_from_envelope_failure(self) -> None:
        good_frame = encode(doc(5, payload_size=32))
        corrupt_frame = with_corrupt_content(good_frame)
        # Outer envelope intact: declared length and trailing null survive.
        self.assertEqual(frame_length(good_frame), frame_length(corrupt_frame))
        self.assertEqual(0, corrupt_frame[-1])
        with self.assertRaises(InvalidBSON):
            decode(corrupt_frame)

        batch = encode(doc(0)) + corrupt_frame
        sealed = self.archive.seal_batch("corrupt-content", batch)
        self.assertTrue(sealed.directory.complete)
        opened = self.archive.open("corrupt-content")
        result = opened.verify_frame(1)
        self.assertTrue(result.envelope_complete)
        self.assertTrue(result.digest_matched)
        self.assertTrue(result.authenticated)
        self.assertFalse(result.content_valid)

    def test_save_and_reload_rebinds_credentials(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "archive.json")
            self.archive.save(path)
            reloaded = BatchArchive.load(path)

        self.assertEqual(self.archive.batch_ids, reloaded.batch_ids)
        opened = reloaded.open("batch-3")
        result = opened.verify_frame(2)
        self.assertTrue(result.authenticated)
        self.assertTrue(result.content_valid)
        self.assertEqual(
            self.sealed3.directory.to_dict(), reloaded.get("batch-3").directory.to_dict()
        )

    def test_neighbor_batch_cannot_fill_a_broken_frame(self) -> None:
        # Arithmetic alone would close the gap, but ingestion never crosses
        # batch boundaries.
        third = self.sealed1.directory.location(2)
        missing = third.offset + third.frame_length - len(self.batch1)
        self.assertEqual(missing, len(encode(self.filler_doc)))
        directory = build_packet_directory("batch-1", self.batch1)
        self.assertFalse(directory.complete)
        self.assertEqual(third, directory.locations[2])

    def test_unknown_batch_and_frame_are_rejected(self) -> None:
        with self.assertRaises(BatchArchiveError):
            self.archive.get("missing")
        with self.assertRaises(BatchArchiveError):
            self.archive.open("missing")
        with self.assertRaises(BatchArchiveError):
            self.archive.open("batch-1").read_frame(99)

    def test_reload_rejects_tampered_batch_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "archive.json")
            self.archive.save(path)
            with open(path, encoding="utf-8") as handle:
                payload = json.load(handle)
            record = payload["batches"]["batch-3"]
            stored = bytearray(base64.b64decode(record["data"]))
            stored[8] ^= 0x01
            # Keep the confirmed digest while changing the stored bytes.
            record["data"] = base64.b64encode(bytes(stored)).decode("ascii")
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            with self.assertRaises(BatchArchiveError):
                BatchArchive.load(path)


if __name__ == "__main__":
    unittest.main()
