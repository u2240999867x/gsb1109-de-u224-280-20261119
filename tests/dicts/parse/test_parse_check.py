import unittest
from datetime import date

from benedict import benedict
from benedict.dicts.parse import ParseCheckResult, ParseDict
from benedict.dicts.parse.parse_dict import (
    PARSE_CHECK_STATUS_INVALID,
    PARSE_CHECK_STATUS_MISSING,
    PARSE_CHECK_STATUS_NULL,
    PARSE_CHECK_STATUS_OK,
)


class parse_check_test_case(unittest.TestCase):
    """
    This class describes a ParseDict.check_parse test case.
    """

    def test_check_parse_statuses(self) -> None:
        d = {
            "count": 0,
            "name": None,
            "date": "not-a-date",
            "qty": "42",
        }
        b = ParseDict(d)
        results = b.check_parse(
            {
                "count": "int",
                "missing": "int",
                "name": "str",
                "date": "date",
                "qty": "int",
            }
        )
        self.assertEqual(len(results), 5)
        statuses = {r.key: r.status for r in results}
        self.assertEqual(statuses["count"], PARSE_CHECK_STATUS_OK)
        self.assertEqual(statuses["missing"], PARSE_CHECK_STATUS_MISSING)
        self.assertEqual(statuses["name"], PARSE_CHECK_STATUS_NULL)
        self.assertEqual(statuses["date"], PARSE_CHECK_STATUS_INVALID)
        self.assertEqual(statuses["qty"], PARSE_CHECK_STATUS_OK)

    def test_check_parse_valid_zero_is_ok(self) -> None:
        d = {
            "i": 0,
            "f": 0.0,
            "b": False,
            "s": "0",
        }
        b = ParseDict(d)
        results = b.check_parse(
            {
                "i": "int",
                "f": "float",
                "b": "bool",
                "s": "int",
            }
        )
        for r in results:
            self.assertTrue(r.ok)
            self.assertEqual(r.status, PARSE_CHECK_STATUS_OK)
        values = {r.key: r.parsed_value for r in results}
        self.assertEqual(values["i"], 0)
        self.assertEqual(values["f"], 0.0)
        self.assertEqual(values["b"], False)
        self.assertEqual(values["s"], 0)

    def test_check_parse_missing_and_null_attributes(self) -> None:
        d = {
            "n": None,
        }
        b = ParseDict(d)
        r_missing, r_null = b.check_parse({"m": "int", "n": "int"})
        self.assertFalse(r_missing.ok)
        self.assertIsNone(r_missing.value)
        self.assertIsNone(r_missing.parsed_value)
        self.assertFalse(r_null.ok)
        self.assertIsNone(r_null.value)
        self.assertIsNone(r_null.parsed_value)

    def test_check_parse_invalid_keeps_raw_value(self) -> None:
        d = {
            "qty": "abc",
        }
        b = ParseDict(d)
        (r,) = b.check_parse({"qty": "int"})
        self.assertEqual(r.status, PARSE_CHECK_STATUS_INVALID)
        self.assertEqual(r.value, "abc")
        self.assertIsNone(r.parsed_value)

    def test_check_parse_with_keypaths(self) -> None:
        d = {
            "profile": {
                "age": "33",
                "born": "1990-05-01",
            },
        }
        b = benedict(d)
        results = b.check_parse(
            {
                "profile.age": "int",
                "profile.born": {"type": "date", "format": "%Y-%m-%d"},
                "profile.email": "email",
            }
        )
        statuses = {r.key: r.status for r in results}
        self.assertEqual(statuses["profile.age"], PARSE_CHECK_STATUS_OK)
        self.assertEqual(statuses["profile.born"], PARSE_CHECK_STATUS_OK)
        self.assertEqual(statuses["profile.email"], PARSE_CHECK_STATUS_MISSING)
        values = {r.key: r.parsed_value for r in results}
        self.assertEqual(values["profile.age"], 33)
        self.assertEqual(values["profile.born"], date(1990, 5, 1))

    def test_check_parse_with_date_format(self) -> None:
        d = {
            "d1": "2026-11-19",
            "d2": "19/11/2026",
        }
        b = ParseDict(d)
        r1, r2 = b.check_parse(
            {
                "d1": {"type": "date", "format": "%Y-%m-%d"},
                "d2": {"type": "date", "format": "%Y-%m-%d"},
            }
        )
        self.assertTrue(r1.ok)
        self.assertEqual(r1.parsed_value, date(2026, 11, 19))
        self.assertFalse(r2.ok)
        self.assertEqual(r2.status, PARSE_CHECK_STATUS_INVALID)

    def test_check_parse_plan_as_pairs(self) -> None:
        d = {
            "a": "1",
            "b": "x",
        }
        b = ParseDict(d)
        results = b.check_parse([("a", "int"), ("b", "int"), ("a", "str")])
        self.assertEqual(len(results), 3)
        self.assertEqual([r.key for r in results], ["a", "b", "a"])
        self.assertEqual(
            [r.status for r in results],
            [PARSE_CHECK_STATUS_OK, PARSE_CHECK_STATUS_INVALID, PARSE_CHECK_STATUS_OK],
        )

    def test_check_parse_with_invalid_type(self) -> None:
        b = ParseDict({"a": 1})
        with self.assertRaises(ValueError):
            b.check_parse({"a": "not-a-type"})

    def test_check_parse_result_to_dict(self) -> None:
        b = ParseDict({"a": "7"})
        (r,) = b.check_parse({"a": "int"})
        self.assertIsInstance(r, ParseCheckResult)
        self.assertEqual(
            r.to_dict(),
            {
                "key": "a",
                "type": "int",
                "value": "7",
                "parsed_value": 7,
                "status": PARSE_CHECK_STATUS_OK,
            },
        )
