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

"""Regression tests for BSON and Extended JSON datetime archiving.

The expected values in this module are fixed by hand and are deliberately not
derived by running the inverse BSON or JSON operation. This keeps wire
milliseconds, display offsets, and decoded Python types independently checkable.
"""

from __future__ import annotations

import calendar
import datetime
import json

from bson import BSON, DatetimeMS, encode, json_util
from bson.codec_options import CodecOptions, DatetimeConversion
from bson.errors import InvalidBSON
from bson.json_util import (
    CANONICAL_JSON_OPTIONS,
    RELAXED_JSON_OPTIONS,
    JSONOptions,
)
from test import unittest

_EPOCH_MINUS_ONE_MS = -1
_EPOCH_PLUS_ONE_MS = 1
_FALL_BACK_FIRST_MS = 1730611800001
_FALL_BACK_SECOND_MS = 1730615400001
_MAX_UTC_MS = 253402300799999
_BEYOND_UTC_MS = 253402300800000
_PLAIN_ISO = "2024-11-03T01:30:00.001-04:00"

_UTC_PLUS_05_30 = datetime.timezone(datetime.timedelta(hours=5, minutes=30), "UTC+05:30")


class _NewYorkFallBack(datetime.tzinfo):
    """America/New_York offsets around the 2024 fall-back transition.

    The explicit rules keep the fixtures reproducible without depending on the
    host timezone database. Outside 2024 the zone uses standard time.
    """

    _EDT = datetime.timedelta(hours=-4)
    _EST = datetime.timedelta(hours=-5)
    _DST_START_LOCAL = datetime.datetime(2024, 3, 10, 3, 0)
    _DST_END_LOCAL = datetime.datetime(2024, 11, 3, 2, 0)
    _DST_START_UTC = datetime.datetime(2024, 3, 10, 7, 0)
    _FALL_BACK_START_UTC = datetime.datetime(2024, 11, 3, 6, 0)
    _FALL_BACK_END_UTC = datetime.datetime(2024, 11, 3, 7, 0)

    def _is_dst_wall(self, dt: datetime.datetime) -> bool:
        wall = dt.replace(tzinfo=None)
        if datetime.datetime(2024, 11, 3, 1, 0) <= wall < self._DST_END_LOCAL:
            return dt.fold == 0
        return self._DST_START_LOCAL <= wall < self._DST_END_LOCAL

    def utcoffset(self, dt: datetime.datetime | None) -> datetime.timedelta:
        if dt is None:
            return self._EST
        return self._EDT if self._is_dst_wall(dt) else self._EST

    def dst(self, dt: datetime.datetime | None) -> datetime.timedelta:
        if self.utcoffset(dt) == self._EDT:
            return self._EDT - self._EST
        return datetime.timedelta(0)

    def tzname(self, dt: datetime.datetime | None) -> str:
        return "EDT" if self.utcoffset(dt) == self._EDT else "EST"

    def fromutc(self, dt: datetime.datetime) -> datetime.datetime:
        if dt.tzinfo is not self:
            raise ValueError("fromutc: dt.tzinfo is not self")
        utc_naive = dt.replace(tzinfo=None)
        if self._FALL_BACK_START_UTC <= utc_naive < self._FALL_BACK_END_UTC:
            return (utc_naive + self._EST).replace(tzinfo=self, fold=1)
        if self._DST_START_UTC <= utc_naive < self._FALL_BACK_START_UTC:
            return (utc_naive + self._EDT).replace(tzinfo=self, fold=0)
        return (utc_naive + self._EST).replace(tzinfo=self, fold=0)


_NEW_YORK = _NewYorkFallBack()


def _utc_millis(value: datetime.datetime) -> int:
    """Convert an aware datetime to UTC milliseconds without bson helpers."""
    utc_value = value.astimezone(datetime.timezone.utc)
    return calendar.timegm(utc_value.timetuple()) * 1000 + utc_value.microsecond // 1000


_ARCHIVE_BSON = bytes.fromhex(
    "a00000000465706f63685f6d73001b000000093000ffffffffffffffff09310001000000000000"
    "00000466616c6c5f6261636b001b000000093000c16780f0920100000931004156b7f092010000"
    "00046c696d697473001b000000093000ffdb1fd277e6000009310000dc1fd277e600000002706c"
    "61696e5f69736f001e000000323032342d31312d30335430313a33303a30302e3030312d30343a"
    "30300000"
)

_MAX_UTC_BSON = bytes.fromhex("10000000097400ffdb1fd277e6000000")
_BEYOND_UTC_BSON = bytes.fromhex("1000000009740000dc1fd277e6000000")

_CANONICAL_EXPECTED = {
    "epoch_ms": [
        {"$date": {"$numberLong": "-1"}},
        {"$date": {"$numberLong": "1"}},
    ],
    "fall_back": [
        {"$date": {"$numberLong": "1730611800001"}},
        {"$date": {"$numberLong": "1730615400001"}},
    ],
    "limits": [
        {"$date": {"$numberLong": "253402300799999"}},
        {"$date": {"$numberLong": "253402300800000"}},
    ],
    "plain_iso": _PLAIN_ISO,
}

_RELAXED_EXPECTED = {
    "epoch_ms": [
        {"$date": {"$numberLong": "-1"}},
        {"$date": "1969-12-31T19:00:00.001-0500"},
    ],
    "fall_back": [
        {"$date": "2024-11-03T01:30:00.001-0400"},
        {"$date": "2024-11-03T01:30:00.001-0500"},
    ],
    "limits": [
        {"$date": "9999-12-31T18:59:59.999-0500"},
        {"$date": {"$numberLong": "253402300800000"}},
    ],
    "plain_iso": _PLAIN_ISO,
}

# These samples are intentionally written by hand instead of being produced by
# json_util.dumps. The relaxed sample also uses colon-separated offsets, which
# differ from the driver's own display formatting but parse to the same instants.
_CANONICAL_JSON_SAMPLE = """
{
  "epoch_ms": [
    {"$date": {"$numberLong": "-1"}},
    {"$date": {"$numberLong": "1"}}
  ],
  "fall_back": [
    {"$date": {"$numberLong": "1730611800001"}},
    {"$date": {"$numberLong": "1730615400001"}}
  ],
  "limits": [
    {"$date": {"$numberLong": "253402300799999"}},
    {"$date": {"$numberLong": "253402300800000"}}
  ],
  "plain_iso": "2024-11-03T01:30:00.001-04:00"
}
"""

_RELAXED_JSON_SAMPLE = """
{
  "epoch_ms": [
    {"$date": {"$numberLong": "-1"}},
    {"$date": "1969-12-31T19:00:00.001-05:00"}
  ],
  "fall_back": [
    {"$date": "2024-11-03T01:30:00.001-04:00"},
    {"$date": "2024-11-03T01:30:00.001-05:00"}
  ],
  "limits": [
    {"$date": "9999-12-31T18:59:59.999-05:00"},
    {"$date": {"$numberLong": "253402300800000"}}
  ],
  "plain_iso": "2024-11-03T01:30:00.001-04:00"
}
"""


class TestDatetimeArchive(unittest.TestCase):
    def _decode_archive(
        self,
        tz: datetime.tzinfo,
        conversion: DatetimeConversion = DatetimeConversion.DATETIME_AUTO,
    ) -> dict:
        options = CodecOptions(
            tz_aware=True,
            tzinfo=tz,
            datetime_conversion=conversion,
        )
        return BSON(_ARCHIVE_BSON).decode(options)

    def _json_options(
        self,
        tz: datetime.tzinfo = _NEW_YORK,
        conversion: DatetimeConversion = DatetimeConversion.DATETIME_AUTO,
    ) -> JSONOptions:
        return JSONOptions(
            tz_aware=True,
            tzinfo=tz,
            datetime_conversion=conversion,
        )

    def _assert_local_datetime(
        self,
        value: object,
        wall: tuple[int, int, int, int, int, int, int],
        offset_hours: int,
        fold: int,
        utc_millis: int,
    ) -> None:
        self.assertIsInstance(value, datetime.datetime)
        assert isinstance(value, datetime.datetime)
        self.assertIs(value.tzinfo, _NEW_YORK)
        expected = datetime.datetime(*wall[:6], wall[6] * 1000)
        self.assertEqual(value.replace(tzinfo=None), expected)
        self.assertEqual(value.utcoffset(), datetime.timedelta(hours=offset_hours))
        self.assertEqual(value.fold, fold)
        self.assertEqual(_utc_millis(value), utc_millis)

    def _assert_archive_document(self, document: dict) -> None:
        self.assertEqual(list(document), ["epoch_ms", "fall_back", "limits", "plain_iso"])

        self._assert_local_datetime(
            document["epoch_ms"][0], (1969, 12, 31, 18, 59, 59, 999), -5, 0, -1
        )
        self._assert_local_datetime(document["epoch_ms"][1], (1969, 12, 31, 19, 0, 0, 1), -5, 0, 1)

        first, second = document["fall_back"]
        self.assertEqual(len(document["fall_back"]), 2)
        self._assert_local_datetime(first, (2024, 11, 3, 1, 30, 0, 1), -4, 0, _FALL_BACK_FIRST_MS)
        self._assert_local_datetime(second, (2024, 11, 3, 1, 30, 0, 1), -5, 1, _FALL_BACK_SECOND_MS)
        self.assertIsNot(first, second)

        self._assert_local_datetime(
            document["limits"][0], (9999, 12, 31, 18, 59, 59, 999), -5, 0, _MAX_UTC_MS
        )
        self.assertIs(type(document["limits"][1]), DatetimeMS)
        self.assertEqual(int(document["limits"][1]), _BEYOND_UTC_MS)

        self.assertIs(type(document["plain_iso"]), str)
        self.assertEqual(document["plain_iso"], _PLAIN_ISO)

    def test_wire_milliseconds_from_bson_bytes(self) -> None:
        document = self._decode_archive(datetime.timezone.utc, DatetimeConversion.DATETIME_MS)

        self.assertEqual([int(value) for value in document["epoch_ms"]], [-1, 1])
        self.assertEqual(
            [int(value) for value in document["fall_back"]],
            [_FALL_BACK_FIRST_MS, _FALL_BACK_SECOND_MS],
        )
        self.assertEqual(
            [int(value) for value in document["limits"]],
            [_MAX_UTC_MS, _BEYOND_UTC_MS],
        )
        for field in ("epoch_ms", "fall_back", "limits"):
            for value in document[field]:
                self.assertIs(type(value), DatetimeMS)
        self.assertIs(type(document["plain_iso"]), str)

    def test_wire_milliseconds_from_explicit_offsets(self) -> None:
        cases = [
            (
                datetime.datetime(1969, 12, 31, 23, 59, 59, 999000, tzinfo=datetime.timezone.utc),
                "10000000097400ffffffffffffffff00",
            ),
            (
                datetime.datetime(1970, 1, 1, 0, 0, 0, 1000, tzinfo=datetime.timezone.utc),
                "10000000097400010000000000000000",
            ),
            (
                datetime.datetime(2024, 11, 3, 1, 30, 0, 1000, tzinfo=_NEW_YORK, fold=0),
                "10000000097400c16780f09201000000",
            ),
            (
                datetime.datetime(2024, 11, 3, 1, 30, 0, 1000, tzinfo=_NEW_YORK, fold=1),
                "100000000974004156b7f09201000000",
            ),
            (
                datetime.datetime(9999, 12, 31, 23, 59, 59, 999000, tzinfo=datetime.timezone.utc),
                _MAX_UTC_BSON.hex(),
            ),
            (DatetimeMS(_BEYOND_UTC_MS), _BEYOND_UTC_BSON.hex()),
        ]
        for value, expected_hex in cases:
            with self.subTest(value=value):
                self.assertEqual(encode({"t": value}).hex(), expected_hex)

    def test_fall_back_keeps_two_records_with_distinct_offsets(self) -> None:
        document = self._decode_archive(_NEW_YORK)
        first, second = document["fall_back"]

        self.assertEqual(len(document["fall_back"]), 2)
        self.assertIsInstance(first, datetime.datetime)
        self.assertIsInstance(second, datetime.datetime)
        assert isinstance(first, datetime.datetime)
        assert isinstance(second, datetime.datetime)

        self.assertEqual(first.replace(tzinfo=None), second.replace(tzinfo=None))
        self.assertEqual(
            first.replace(tzinfo=None),
            datetime.datetime(2024, 11, 3, 1, 30, 0, 1000),
        )
        self.assertEqual(first.utcoffset(), datetime.timedelta(hours=-4))
        self.assertEqual(second.utcoffset(), datetime.timedelta(hours=-5))
        self.assertEqual(first.fold, 0)
        self.assertEqual(second.fold, 1)
        self.assertEqual(_utc_millis(first), _FALL_BACK_FIRST_MS)
        self.assertEqual(_utc_millis(second), _FALL_BACK_SECOND_MS)
        self.assertLess(_utc_millis(first), _utc_millis(second))
        self.assertIsNot(first, second)

    def test_epoch_sign_in_new_york(self) -> None:
        document = self._decode_archive(_NEW_YORK)

        self._assert_local_datetime(
            document["epoch_ms"][0], (1969, 12, 31, 18, 59, 59, 999), -5, 0, -1
        )
        self._assert_local_datetime(document["epoch_ms"][1], (1969, 12, 31, 19, 0, 0, 1), -5, 0, 1)
        self.assertLess(
            datetime.datetime(1969, 12, 31, 18, 59, 59, 999000),
            datetime.datetime(1969, 12, 31, 19, 0, 0, 1000),
        )

    def test_canonical_export_from_bson_bytes(self) -> None:
        document = self._decode_archive(_NEW_YORK)
        actual = json.loads(json_util.dumps(document, json_options=CANONICAL_JSON_OPTIONS))

        self.assertEqual(actual, _CANONICAL_EXPECTED)
        for field, expected in (
            ("epoch_ms", (_EPOCH_MINUS_ONE_MS, _EPOCH_PLUS_ONE_MS)),
            ("fall_back", (_FALL_BACK_FIRST_MS, _FALL_BACK_SECOND_MS)),
            ("limits", (_MAX_UTC_MS, _BEYOND_UTC_MS)),
        ):
            self.assertEqual(
                [item["$date"]["$numberLong"] for item in actual[field]],
                [str(millis) for millis in expected],
            )
        self.assertIs(type(actual["plain_iso"]), str)
        self.assertEqual(actual["plain_iso"], _PLAIN_ISO)

    def test_relaxed_export_from_bson_bytes(self) -> None:
        document = self._decode_archive(_NEW_YORK)
        actual = json.loads(json_util.dumps(document, json_options=RELAXED_JSON_OPTIONS))

        self.assertEqual(actual, _RELAXED_EXPECTED)
        self.assertIsInstance(actual["epoch_ms"][0]["$date"], dict)
        self.assertIsInstance(actual["epoch_ms"][1]["$date"], str)
        self.assertIsInstance(actual["fall_back"][0]["$date"], str)
        self.assertIsInstance(actual["fall_back"][1]["$date"], str)
        self.assertIsInstance(actual["limits"][0]["$date"], str)
        self.assertIsInstance(actual["limits"][1]["$date"], dict)
        self.assertTrue(actual["fall_back"][0]["$date"].endswith("-0400"))
        self.assertTrue(actual["fall_back"][1]["$date"].endswith("-0500"))
        self.assertIs(type(actual["plain_iso"]), str)
        self.assertEqual(actual["plain_iso"], _PLAIN_ISO)

    def test_canonical_import_from_hand_written_sample(self) -> None:
        document = json_util.loads(
            _CANONICAL_JSON_SAMPLE,
            json_options=self._json_options(),
        )

        self._assert_archive_document(document)
        self.assertEqual(encode(document), _ARCHIVE_BSON)

    def test_relaxed_import_from_hand_written_sample(self) -> None:
        document = json_util.loads(
            _RELAXED_JSON_SAMPLE,
            json_options=self._json_options(),
        )

        self._assert_archive_document(document)
        self.assertEqual(encode(document), _ARCHIVE_BSON)

    def test_datetime_ms_import_never_returns_native_datetime(self) -> None:
        samples = (
            ("canonical", _CANONICAL_JSON_SAMPLE),
            ("relaxed", _RELAXED_JSON_SAMPLE),
        )
        for name, sample in samples:
            with self.subTest(sample=name):
                document = json_util.loads(
                    sample,
                    json_options=self._json_options(conversion=DatetimeConversion.DATETIME_MS),
                )

                self.assertEqual([int(value) for value in document["epoch_ms"]], [-1, 1])
                self.assertEqual(
                    [int(value) for value in document["fall_back"]],
                    [_FALL_BACK_FIRST_MS, _FALL_BACK_SECOND_MS],
                )
                self.assertEqual(
                    [int(value) for value in document["limits"]],
                    [_MAX_UTC_MS, _BEYOND_UTC_MS],
                )
                for field in ("epoch_ms", "fall_back", "limits"):
                    for value in document[field]:
                        self.assertIs(type(value), DatetimeMS)
                        self.assertNotIsInstance(value, datetime.datetime)
                self.assertIs(type(document["plain_iso"]), str)
                self.assertEqual(encode(document), _ARCHIVE_BSON)

    def test_datetime_auto_range_depends_on_target_timezone(self) -> None:
        utc_document = self._decode_archive(datetime.timezone.utc)
        self.assertIsInstance(utc_document["limits"][0], datetime.datetime)
        self.assertEqual(_utc_millis(utc_document["limits"][0]), _MAX_UTC_MS)
        self.assertIs(type(utc_document["limits"][1]), DatetimeMS)
        self.assertEqual(int(utc_document["limits"][1]), _BEYOND_UTC_MS)

        plus_document = self._decode_archive(_UTC_PLUS_05_30)
        self.assertIs(type(plus_document["limits"][0]), DatetimeMS)
        self.assertEqual(int(plus_document["limits"][0]), _MAX_UTC_MS)
        self.assertIs(type(plus_document["limits"][1]), DatetimeMS)
        self.assertEqual(int(plus_document["limits"][1]), _BEYOND_UTC_MS)

        utc_strict = BSON(_MAX_UTC_BSON).decode(
            CodecOptions(
                tz_aware=True,
                tzinfo=datetime.timezone.utc,
                datetime_conversion=DatetimeConversion.DATETIME,
            )
        )
        self.assertIsInstance(utc_strict["t"], datetime.datetime)
        self.assertEqual(_utc_millis(utc_strict["t"]), _MAX_UTC_MS)

        with self.assertRaises(InvalidBSON):
            BSON(_MAX_UTC_BSON).decode(
                CodecOptions(
                    tz_aware=True,
                    tzinfo=_UTC_PLUS_05_30,
                    datetime_conversion=DatetimeConversion.DATETIME,
                )
            )

        for tz in (datetime.timezone.utc, _UTC_PLUS_05_30):
            with self.subTest(boundary="beyond_utc", tz=tz):
                with self.assertRaises(InvalidBSON):
                    BSON(_BEYOND_UTC_BSON).decode(
                        CodecOptions(
                            tz_aware=True,
                            tzinfo=tz,
                            datetime_conversion=DatetimeConversion.DATETIME,
                        )
                    )

        with self.assertRaises(OverflowError):
            datetime.datetime(
                9999, 12, 31, 23, 59, 59, 999000, tzinfo=datetime.timezone.utc
            ).astimezone(_UTC_PLUS_05_30)
        with self.assertRaises(OverflowError):
            datetime.datetime(1970, 1, 1, tzinfo=datetime.timezone.utc) + datetime.timedelta(
                milliseconds=_BEYOND_UTC_MS
            )

    def test_datetime_auto_json_boundary_uses_target_timezone(self) -> None:
        max_json = '{"t": {"$date": {"$numberLong": "253402300799999"}}}'
        beyond_json = '{"t": {"$date": {"$numberLong": "253402300800000"}}}'

        utc_max = json_util.loads(max_json, json_options=self._json_options(datetime.timezone.utc))[
            "t"
        ]
        self.assertIsInstance(utc_max, datetime.datetime)
        self.assertEqual(_utc_millis(utc_max), _MAX_UTC_MS)

        plus_max = json_util.loads(max_json, json_options=self._json_options(_UTC_PLUS_05_30))["t"]
        self.assertIs(type(plus_max), DatetimeMS)
        self.assertEqual(int(plus_max), _MAX_UTC_MS)

        for tz in (datetime.timezone.utc, _UTC_PLUS_05_30):
            with self.subTest(tz=tz):
                value = json_util.loads(beyond_json, json_options=self._json_options(tz))["t"]
                self.assertIs(type(value), DatetimeMS)
                self.assertEqual(int(value), _BEYOND_UTC_MS)

        plus_ms = json_util.loads(
            max_json,
            json_options=self._json_options(_UTC_PLUS_05_30, DatetimeConversion.DATETIME_MS),
        )["t"]
        self.assertIs(type(plus_ms), DatetimeMS)
        self.assertEqual(int(plus_ms), _MAX_UTC_MS)

    def test_plain_iso_string_is_preserved(self) -> None:
        document = self._decode_archive(_NEW_YORK)

        self.assertIs(type(document["plain_iso"]), str)
        self.assertEqual(document["plain_iso"], _PLAIN_ISO)
        self.assertNotIsInstance(document["plain_iso"], datetime.datetime)

        for options in (CANONICAL_JSON_OPTIONS, RELAXED_JSON_OPTIONS):
            exported = json.loads(json_util.dumps(document, json_options=options))
            self.assertIs(type(exported["plain_iso"]), str)
            self.assertEqual(exported["plain_iso"], _PLAIN_ISO)

        for sample in (_CANONICAL_JSON_SAMPLE, _RELAXED_JSON_SAMPLE):
            for conversion in (
                DatetimeConversion.DATETIME_AUTO,
                DatetimeConversion.DATETIME_MS,
            ):
                imported = json_util.loads(
                    sample,
                    json_options=self._json_options(conversion=conversion),
                )
                self.assertIs(type(imported["plain_iso"]), str)
                self.assertEqual(imported["plain_iso"], _PLAIN_ISO)
                self.assertNotIsInstance(imported["plain_iso"], datetime.datetime)


if __name__ == "__main__":
    unittest.main()
