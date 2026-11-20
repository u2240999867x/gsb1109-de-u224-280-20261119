from __future__ import annotations

import json
import unittest
from typing import Any

from benedict.core import MISSING
from benedict.core import clone as _clone
from benedict.core import groupby as _groupby
from benedict.core.groupby import GroupKey, groupby_diff


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

    def test_groupby_with_multiple_keys_and_fallback(self) -> None:
        ls: list[dict[str, Any]] = [
            # same content, only the source line number differs
            {"source": "s1", "line": 1, "old_category": [], "new_category": None},
            {"source": "s1", "line": 2, "old_category": [], "new_category": None},
            # no new-category value: fall back to the same row's old path
            {"source": "s1", "line": 3, "old_category": []},
            {"source": "s1", "line": 4, "old_category": []},
            {"source": "s2", "line": 5, "old_category": []},
        ]
        ls_clone = _clone(ls)
        groups = _groupby(
            ls,
            ["source", "new_category"],
            fallback_keys={"new_category": "old_category"},
        )
        self.assertEqual(ls, ls_clone)
        self.assertEqual(len(groups), 3)
        self.assertTrue(all(isinstance(key, GroupKey) for key in groups))

        s1_null = GroupKey(
            [
                ("source", True, "s1"),
                ("new_category", True, None),
            ]
        )
        s1_empty = GroupKey(
            [
                ("source", True, "s1"),
                ("new_category", True, []),
            ]
        )
        s2_empty = GroupKey(
            [
                ("source", True, "s2"),
                ("new_category", True, []),
            ]
        )
        self.assertEqual([item["line"] for item in groups[s1_null]], [1, 2])
        self.assertEqual([item["line"] for item in groups[s1_empty]], [3, 4])
        self.assertEqual([item["line"] for item in groups[s2_empty]], [5])

    def test_groupby_missing_is_distinct_from_values(self) -> None:
        ls: list[dict[str, Any]] = [
            {"source": "s1", "new_category": None},
            {"source": "s1", "new_category": []},
            {"source": "s1"},
        ]
        groups = _groupby(ls, ["source", "new_category"])
        self.assertEqual(len(groups), 3)
        present_none = GroupKey([("source", True, "s1"), ("new_category", True, None)])
        present_empty = GroupKey([("source", True, "s1"), ("new_category", True, [])])
        missing = GroupKey([("source", True, "s1"), ("new_category", False, None)])
        self.assertIn(present_none, groups)
        self.assertIn(present_empty, groups)
        self.assertIn(missing, groups)
        self.assertEqual(groups[missing][0]["source"], "s1")

    def test_groupby_explicit_value_never_falls_back(self) -> None:
        ls: list[dict[str, Any]] = [
            # explicit null must stay null, even when the old category exists
            {"source": "s1", "new_category": None, "old_category": ["a"]},
            # explicit empty list must stay empty
            {"source": "s1", "new_category": [], "old_category": ["a"]},
        ]
        groups = _groupby(
            ls,
            ["source", "new_category"],
            fallback_keys={"new_category": "old_category"},
        )
        self.assertEqual(len(groups), 2)
        null_group = GroupKey([("source", True, "s1"), ("new_category", True, None)])
        empty_group = GroupKey([("source", True, "s1"), ("new_category", True, [])])
        self.assertTrue(null_group in groups)
        self.assertTrue(empty_group in groups)

    def test_groupby_key_json_round_trip(self) -> None:
        ls: list[dict[str, Any]] = [
            {"source": "s1", "new_category": None},
            {"source": "s1", "new_category": []},
            {"source": "s1"},
            {"source": "s2", "new_category": "x"},
        ]
        groups = _groupby(ls, ["source", "new_category"])
        for key in groups:
            self.assertIsInstance(key, str)
            decoded = json.loads(key)
            self.assertIn("parts", decoded)
            rebuilt = GroupKey.from_json(key)
            self.assertEqual(rebuilt, key)
            self.assertEqual(rebuilt.present, key.present)

    def test_groupby_diff_only_affected_groups(self) -> None:
        rows: list[dict[str, Any]] = [
            {"source": "s1", "line": 1, "old_category": [], "new_category": None},
            {"source": "s1", "line": 2, "old_category": [], "new_category": None},
        ]
        first = _groupby(
            rows,
            ["source", "new_category"],
            fallback_keys={"new_category": "old_category"},
            identity_key="line",
        )
        # late correction: the earlier occurrence changes from explicit null to
        # missing; its old-category value is empty, so it moves to the [] group.
        corrected = [dict(row) for row in rows]
        del corrected[0]["new_category"]
        second = _groupby(
            corrected,
            ["source", "new_category"],
            fallback_keys={"new_category": "old_category"},
            identity_key="line",
        )
        changes = first.diff(second)
        null_group = GroupKey([("source", True, "s1"), ("new_category", True, None)])
        empty_group = GroupKey([("source", True, "s1"), ("new_category", True, [])])
        self.assertEqual(set(changes), {null_group, empty_group})
        self.assertEqual([item["line"] for item in changes[null_group]["removed"]], [1])
        self.assertEqual(changes[null_group]["added"], [])
        self.assertEqual(changes[empty_group]["removed"], [])
        self.assertEqual([item["line"] for item in changes[empty_group]["added"]], [1])
        # the other identical row is still in the null group
        self.assertEqual([item["line"] for item in second[null_group]], [2])
        # resending the same correction must not change group membership
        third = _groupby(
            corrected,
            ["source", "new_category"],
            fallback_keys={"new_category": "old_category"},
            identity_key="line",
        )
        self.assertEqual(second.diff(third), {})

    def test_groupby_diff_without_identity_uses_content_occurrences(self) -> None:
        rows = [
            {"source": "s1", "new_category": None},
            {"source": "s1", "new_category": None},
        ]
        first = _groupby(rows, ["source", "new_category"])
        key = GroupKey([("source", True, "s1"), ("new_category", True, None)])
        self.assertEqual(len(first[key]), 2)
        second = _groupby(rows[:1], ["source", "new_category"])
        changes = groupby_diff(first, second)
        self.assertEqual(len(changes[key]["removed"]), 1)
        self.assertEqual(changes[key]["added"], [])

    def test_groupby_with_missing_both_paths(self) -> None:
        ls: list[dict[str, Any]] = [
            {"source": "s1", "line": 1},
            {"source": "s1", "line": 2, "old_category": []},
        ]
        groups = _groupby(
            ls,
            ["source", "new_category"],
            fallback_keys={"new_category": "old_category"},
        )
        missing = GroupKey([("source", True, "s1"), ("new_category", False, None)])
        empty = GroupKey([("source", True, "s1"), ("new_category", True, [])])
        self.assertEqual([item["line"] for item in groups[missing]], [1])
        self.assertEqual([item["line"] for item in groups[empty]], [2])
        self.assertIs(MISSING, MISSING)

    def test_groupby_legacy_single_key_still_works(self) -> None:
        ls = [
            {"id": 1, "name": "John"},
            {"id": 2, "name": "Frank"},
            {"id": 1, "name": "Charles"},
        ]
        groups = _groupby(ls, "id")
        self.assertEqual(len(groups), 2)
        self.assertEqual(
            groups[1], [{"id": 1, "name": "John"}, {"id": 1, "name": "Charles"}]
        )
        self.assertIs(type(groups), dict)
