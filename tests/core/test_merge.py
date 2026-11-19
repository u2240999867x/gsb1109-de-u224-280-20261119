import unittest

from benedict.core import merge as _merge
from benedict.exceptions import MergeConflictError


class merge_test_case(unittest.TestCase):
    """
    This class describes a merge test case.
    """

    def test_merge_with_flatten_dict(self) -> None:
        d = {
            "a": 1,
            "b": 1,
        }
        m = {
            "b": 2,
            "c": 3,
        }
        _merge(d, m)
        r = {
            "a": 1,
            "b": 2,
            "c": 3,
        }
        self.assertEqual(d, r)

    def test_merge_with_lists(self) -> None:
        d = {
            "a": [0, 1, 2],
            "b": [5, 6, 7],
            "c": [],
            "d": [],
        }
        m = {
            "a": [3, 4, 5],
            "b": [8, 9, 0],
            "c": [-1],
        }
        _merge(d, m)
        r = {
            "a": [3, 4, 5],
            "b": [8, 9, 0],
            "c": [-1],
            "d": [],
        }
        self.assertEqual(d, r)

    def test_merge_with_lists_and_concat(self) -> None:
        d = {
            "a": [0, 1, 2],
            "b": [5, 6, 7],
            "c": [],
            "d": [],
        }
        m = {
            "a": [3, 4, 5],
            "b": [8, 9, 0],
            "c": [-1],
        }
        _merge(d, m, concat=True)
        r = {
            "a": [0, 1, 2, 3, 4, 5],
            "b": [5, 6, 7, 8, 9, 0],
            "c": [-1],
            "d": [],
        }
        self.assertEqual(d, r)

    def test_merge_with_multiple_dicts(self) -> None:
        d = {
            "a": 1,
            "b": 1,
        }
        a = {
            "b": 2,
            "c": 3,
            "d": 3,
        }
        b = {
            "d": 5,
            "e": 5,
        }
        c = {
            "d": 4,
            "f": 6,
        }
        _merge(d, a, b, c)
        r = {
            "a": 1,
            "b": 2,
            "c": 3,
            "d": 4,
            "e": 5,
            "f": 6,
        }
        self.assertEqual(d, r)

    def test_merge_with_nested_dict(self) -> None:
        d = {
            "a": 1,
            "b": {
                "c": {
                    "x": 2,
                    "y": 3,
                },
                "d": {
                    "x": 4,
                    "y": 5,
                },
                "e": {
                    "x": 6,
                    "y": 7,
                },
            },
        }
        m = {
            "a": 0,
            "b": {
                "c": 1,
                "d": {
                    "y": 1,
                    "z": 2,
                },
                "e": {
                    "f": {
                        "x": 2,
                        "y": 3,
                    },
                    "g": {
                        "x": 4,
                        "y": 5,
                    },
                },
            },
        }
        _merge(d, m)
        r = {
            "a": 0,
            "b": {
                "c": 1,
                "d": {
                    "x": 4,
                    "y": 1,
                    "z": 2,
                },
                "e": {
                    "f": {
                        "x": 2,
                        "y": 3,
                    },
                    "g": {
                        "x": 4,
                        "y": 5,
                    },
                    "x": 6,
                    "y": 7,
                },
            },
        }
        self.assertEqual(d, r)

    def test_merge_without_overwrite(self) -> None:
        d = {
            "a": 1,
            "b": {
                "c": {
                    "x": 2,
                    "y": 3,
                },
                "d": {
                    "x": 4,
                    "y": 5,
                },
                "e": {
                    "x": 6,
                    "y": 7,
                },
            },
        }
        m = {
            "a": 0,
            "b": {
                "c": 1,
                "d": {
                    "y": 1,
                    "z": 2,
                },
                "e": {
                    "f": {
                        "x": 2,
                        "y": 3,
                    },
                    "g": {
                        "x": 4,
                        "y": 5,
                    },
                },
            },
        }
        _merge(d, m, overwrite=False)
        r = {
            "a": 1,
            "b": {
                "c": {
                    "x": 2,
                    "y": 3,
                },
                "d": {
                    "x": 4,
                    "y": 5,
                    "z": 2,
                },
                "e": {
                    "f": {
                        "x": 2,
                        "y": 3,
                    },
                    "g": {
                        "x": 4,
                        "y": 5,
                    },
                    "x": 6,
                    "y": 7,
                },
            },
        }
        self.assertEqual(d, r)

    def test_merge_by_keypath_by_key(self) -> None:
        d = {
            "order_id": "A1",
            "items": [
                {"sku": "S1", "qty": 2, "delivery": "2026-12-01"},
                {"sku": "S2", "qty": 1, "delivery": "2026-12-05"},
            ],
        }
        m = {
            "order_id": "A1",
            "items": [
                {"sku": "S3", "qty": 4, "delivery": "2026-12-10"},
                {"sku": "S1", "delivery": None},
            ],
        }
        _merge(d, m, keypath="items", by_key="sku")
        r = {
            "order_id": "A1",
            "items": [
                {"sku": "S1", "qty": 2, "delivery": None},
                {"sku": "S2", "qty": 1, "delivery": "2026-12-05"},
                {"sku": "S3", "qty": 4, "delivery": "2026-12-10"},
            ],
        }
        self.assertEqual(d, r)

    def test_merge_by_nested_keypath_by_key(self) -> None:
        d = {
            "data": {
                "lines": [
                    {"sku": "A", "delivery": "old"},
                    {"sku": "B", "delivery": "old"},
                ]
            },
            "meta": {"source": "first"},
        }
        m = {
            "data": {
                "lines": [
                    {"sku": "B", "delivery": "new"},
                    {"sku": "C", "delivery": "new"},
                ]
            },
            "meta": {"version": 2},
        }
        _merge(d, m, keypath="data.lines", by_key="sku")
        r = {
            "data": {
                "lines": [
                    {"sku": "A", "delivery": "old"},
                    {"sku": "B", "delivery": "new"},
                    {"sku": "C", "delivery": "new"},
                ]
            },
            "meta": {"source": "first", "version": 2},
        }
        self.assertEqual(d, r)

    def test_merge_by_keypath_by_key_with_custom_separator(self) -> None:
        d = {"a": {"b": [{"sku": "A", "delivery": "old"}]}}
        m = {"a": {"b": [{"sku": "A", "delivery": "new"}, {"sku": "B"}]}}
        _merge(d, m, keypath="a/b", by_key="sku", keypath_separator="/")
        r = {"a": {"b": [{"sku": "A", "delivery": "new"}, {"sku": "B"}]}}
        self.assertEqual(d, r)

    def test_merge_by_keypath_by_key_with_missing_keypath(self) -> None:
        d = {"order_id": "A1"}
        m = {"items": [{"sku": "S1", "delivery": "2026-12-01"}]}
        _merge(d, m, keypath="items", by_key="sku")
        self.assertEqual(d, {"order_id": "A1", **m})

    def test_merge_by_keypath_by_key_with_duplicate_key(self) -> None:
        d = {"items": [{"sku": "S1", "delivery": "2026-12-01"}]}
        m = {"items": [{"sku": "S2"}, {"sku": "S2"}]}
        with self.assertRaisesRegex(MergeConflictError, "Duplicate merge key 'sku'"):
            _merge(d, m, keypath="items", by_key="sku")
        self.assertEqual(d, {"items": [{"sku": "S1", "delivery": "2026-12-01"}]})

    def test_merge_by_keypath_by_key_with_duplicate_key_in_destination(self) -> None:
        d = {"items": [{"sku": "S1"}, {"sku": "S1"}]}
        m = {"items": [{"sku": "S1"}]}
        with self.assertRaises(MergeConflictError):
            _merge(d, m, keypath="items", by_key="sku")
        self.assertEqual(d, {"items": [{"sku": "S1"}, {"sku": "S1"}]})

    def test_merge_by_keypath_by_key_with_multiple_dicts(self) -> None:
        d = {"items": [{"sku": "S1", "delivery": "old"}]}
        a = {"items": [{"sku": "S1", "delivery": "a"}, {"sku": "S2"}]}
        b = {"items": [{"sku": "S2", "delivery": "b"}, {"sku": "S3"}]}
        _merge(d, a, b, keypath="items", by_key="sku")
        r = {
            "items": [
                {"sku": "S1", "delivery": "a"},
                {"sku": "S2", "delivery": "b"},
                {"sku": "S3"},
            ]
        }
        self.assertEqual(d, r)

    def test_merge_by_keypath_by_key_without_overwrite(self) -> None:
        d = {"items": [{"sku": "S1", "delivery": "old", "qty": 1}]}
        m = {"items": [{"sku": "S1", "delivery": "new", "note": "x"}]}
        _merge(d, m, keypath="items", by_key="sku", overwrite=False)
        r = {"items": [{"sku": "S1", "delivery": "old", "qty": 1, "note": "x"}]}
        self.assertEqual(d, r)

    def test_merge_by_keypath_by_key_with_invalid_arguments(self) -> None:
        d = {"items": [{"sku": "S1"}]}
        with self.assertRaises(ValueError):
            _merge(d, {"items": []}, by_key="sku")
        # keypath without by_key merges by the default SKU key.
        _merge(d, {"items": [{"sku": "S1", "delivery": "x"}]}, keypath="items")
        self.assertEqual(d, {"items": [{"sku": "S1", "delivery": "x"}]})
        with self.assertRaises(ValueError):
            _merge(d, {"items": []}, key="items", by_key="sku", keypath="other")
        with self.assertRaises(TypeError):
            _merge(d, {"items": []}, keypath="items", by_key="sku", unknown=True)

    def test_merge_by_keypath_by_key_with_invalid_items(self) -> None:
        d = {"items": [{"sku": "S1"}]}
        with self.assertRaises(ValueError):
            _merge(d, {"items": ["S2"]}, keypath="items", by_key="sku")
        with self.assertRaises(ValueError):
            _merge(d, {"items": [{"name": "S2"}]}, keypath="items", by_key="sku")
        with self.assertRaises(ValueError):
            _merge(d, {"items": {}}, keypath="items", by_key="sku")
        with self.assertRaises(ValueError):
            _merge(
                d,
                {"items": [{"sku": ["not", "hashable"]}]},
                keypath="items",
                by_key="sku",
            )
        self.assertEqual(d, {"items": [{"sku": "S1"}]})

    def test_merge_by_keypath_by_key_preserves_unmatched_order(self) -> None:
        d = {"items": [{"sku": "S2"}, {"sku": "S1"}, {"sku": "S3"}]}
        m = {"items": [{"sku": "S3", "delivery": "a"}, {"sku": "S4"}, {"sku": "S5"}]}
        _merge(d, m, keypath="items", by_key="sku")
        self.assertEqual(
            [item["sku"] for item in d["items"]], ["S2", "S1", "S3", "S4", "S5"]
        )
        self.assertEqual(d["items"][2], {"sku": "S3", "delivery": "a"})
