from __future__ import annotations

import unittest
from typing import Any

from benedict.core import clone as _clone
from benedict.core import groupby as _groupby
from benedict.core.groupby import MISSING


class groupby_test_case(unittest.TestCase):
    """
    This class describes a groupby test case.
    """

    def test_groupby(self) -> None:
        ls: list[dict[str, Any]] = [
            {"id": 1, "name": "John"},
            {"id": 2, "name": "Frank"},
            {"id": 3, "name": "Tony"},
            {"id": 4, "name": "Jimmy"},
            {"id": 3, "name": "Sam"},
            {"id": 1, "name": "Charles"},
            {"id": 3, "name": "Bob"},
            {"id": 4, "name": "Paul"},
            {"id": 1, "name": "Michael"},
        ]
        ls_clone = _clone(ls)
        d = _groupby(ls, "id")
        self.assertEqual(ls, ls_clone)
        self.assertTrue(isinstance(d, dict))
        self.assertEqual(len(d), 4)
        self.assertTrue(
            all(
                [
                    1 in d,
                    2 in d,
                    3 in d,
                    4 in d,
                ]
            )
        )
        self.assertTrue(
            all(
                [
                    isinstance(d[1], list),
                    isinstance(d[2], list),
                    isinstance(d[3], list),
                    isinstance(d[4], list),
                ]
            )
        )
        self.assertEqual(len(d[1]), 3)
        self.assertEqual(len(d[2]), 1)
        self.assertEqual(len(d[3]), 3)
        self.assertEqual(len(d[4]), 2)

    def test_groupby_with_wrong_input(self) -> None:
        ls: list[list[dict[str, Any]]] | dict[str, Any] = {"id": 1, "name": "John"}
        with self.assertRaises(ValueError):
            _ = _groupby(ls, "id")  # type: ignore[arg-type]
        ls = [
            [{"id": 1, "name": "John"}],
            [{"id": 2, "name": "Frank"}],
        ]
        with self.assertRaises(ValueError):
            _ = _groupby(ls, "id")  # type: ignore[arg-type]

    def test_groupby_with_composite_paths(self) -> None:
        ls: list[dict[str, Any]] = [
            {"id": 1, "category": {"group": "A", "code": "10"}},
            {"id": 2, "category": {"group": "A", "code": "10"}},
            {"id": 3, "category": {"group": "A", "code": "20"}},
            {"id": 4, "category": {"group": "B", "code": "10"}},
        ]
        ls_clone = _clone(ls)
        for by_key in (
            ["category.group", "category.code"],
            ("category.group", "category.code"),
            [["category", "group"], ["category", "code"]],
        ):
            d = _groupby(ls, by_key)
            self.assertEqual(ls, ls_clone)
            self.assertEqual(len(d), 3)
            self.assertEqual(d[("A", "10")], ls[0:2])
            self.assertEqual(d[("A", "20")], ls[2:3])
            self.assertEqual(d[("B", "10")], ls[3:4])

    def test_groupby_with_composite_paths_preserves_items_order(self) -> None:
        ls: list[dict[str, Any]] = [
            {"id": 4, "a": 1, "b": 2},
            {"id": 2, "a": 1, "b": 2},
            {"id": 3, "a": 1, "b": 2},
            {"id": 1, "a": 2, "b": 2},
        ]
        d = _groupby(ls, ("a", "b"))
        self.assertEqual([item["id"] for item in d[(1, 2)]], [4, 2, 3])
        self.assertEqual([item["id"] for item in d[(2, 2)]], [1])

    def test_groupby_distinguishes_null_missing_and_empty_list(self) -> None:
        ls: list[dict[str, Any]] = [
            {"id": 1, "category": {"code": "A"}},
            {"id": 2, "category": {"code": None}},
            {"id": 3, "category": {}},
            {"id": 4},
            {"id": 5, "category": {"code": []}},
            {"id": 6, "category": {"code": []}},
            {"id": 7, "category": {"code": "A"}},
        ]
        d = _groupby(ls, "category.code")
        self.assertEqual(len(d), 4)
        self.assertEqual([item["id"] for item in d["A"]], [1, 7])
        self.assertEqual([item["id"] for item in d[None]], [2])
        self.assertEqual([item["id"] for item in d[MISSING]], [3, 4])
        self.assertEqual([item["id"] for item in d[[]]], [5, 6])

    def test_groupby_with_equal_nested_dict_values(self) -> None:
        ls: list[dict[str, Any]] = [
            {"id": 1, "category": {"group": "A", "tags": ["x", "y"]}},
            {"id": 2, "category": {"tags": ["x", "y"], "group": "A"}},
            {"id": 3, "category": {"group": "A", "tags": []}},
            {"id": 4, "category": None},
            {"id": 5},
        ]
        d = _groupby(ls, "category")
        self.assertEqual(len(d), 4)
        self.assertEqual(
            [item["id"] for item in d[{"group": "A", "tags": ["x", "y"]}]],
            [1, 2],
        )
        self.assertEqual([item["id"] for item in d[{"group": "A", "tags": []}]], [3])
        self.assertEqual([item["id"] for item in d[None]], [4])
        self.assertEqual([item["id"] for item in d[MISSING]], [5])
