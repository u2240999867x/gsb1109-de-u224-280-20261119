import unittest

from benedict.core import filter as _filter


class filter_test_case(unittest.TestCase):
    """
    This class describes a filter test case.
    """

    def test_filter(self) -> None:
        i = {
            "a": 1,
            "b": 2,
            "c": "4",
            "e": "5",
            "f": 6,
            "g": 7,
        }
        with self.assertRaises(ValueError):
            _filter(i, True)  # type: ignore[arg-type]
        o = _filter(i, lambda key, val: isinstance(val, int))
        r = {
            "a": 1,
            "b": 2,
            "f": 6,
            "g": 7,
        }
        self.assertFalse(i is o)
        self.assertEqual(o, r)

    def test_filter_deep(self) -> None:
        i = {
            "a": 1,
            "b": "hello",
            "nested": {
                "c": 2,
                "d": "world",
            },
        }
        o = _filter(i, lambda key, val: not isinstance(val, str), deep=True)
        r = {
            "a": 1,
            "nested": {
                "c": 2,
            },
        }
        self.assertFalse(i is o)
        self.assertEqual(o, r)

    def test_filter_deep_predicate_removes_nested_dict_entirely(self) -> None:
        i = {
            "a": 1,
            "metadata": {
                "tag": "draft",
                "reviewed": "no",
            },
        }
        # predicate keeps only int values; the nested dict itself passes
        # (it is not a str), so it is kept but its children are filtered
        o = _filter(i, lambda key, val: not isinstance(val, str), deep=True)
        r = {
            "a": 1,
            "metadata": {},
        }
        self.assertEqual(o, r)

    def test_filter_deep_ancestor_kept_when_descendant_matches(self) -> None:
        i = {
            "a": 1,
            "private": {
                "secret": "x",
            },
        }
        # the "private" edge does not match, but its descendant does,
        # so the ancestor is automatically kept with its matching content
        o = _filter(i, lambda key, val: key != "private", deep=True)
        r = {
            "a": 1,
            "private": {
                "secret": "x",
            },
        }
        self.assertEqual(o, r)

    def test_filter_deep_without_deep_leaves_nested_untouched(self) -> None:
        i = {
            "a": 1,
            "b": "hello",
            "nested": {
                "c": 2,
                "d": "world",
            },
        }
        o = _filter(i, lambda key, val: not isinstance(val, str))
        # nested dict passes predicate (it is not a str), so it is kept as-is
        r = {
            "a": 1,
            "nested": {
                "c": 2,
                "d": "world",
            },
        }
        self.assertEqual(o, r)

    def test_filter_deep_path_predicate_receives_full_path(self) -> None:
        i = {
            "devices": [
                {"id": 1, "props": {"fw": "1.0"}},
                {"id": 2, "props": {"fw": "2.0"}},
            ],
        }
        seen = []

        def predicate(path, key, val):
            seen.append((list(path), key, val))
            return key == "fw" and val == "1.0"

        o = _filter(i, predicate, deep=True)
        r = {
            "devices": [
                {"props": {"fw": "1.0"}},
            ],
        }
        self.assertEqual(o, r)
        self.assertIn(
            (["devices", 0, "props", "fw"], "fw", "1.0"),
            seen,
        )
        self.assertIn(
            (["devices", 1, "props", "fw"], "fw", "2.0"),
            seen,
        )

    def test_filter_deep_keeps_ancestors_of_matching_descendants(self) -> None:
        i = {
            "root": {
                "child": {
                    "keep": 5,
                    "drop": "x",
                },
            },
        }
        # the predicate never matches the container edges, only the leaf
        o = _filter(i, lambda path, key, val: key == "keep", deep=True)
        r = {
            "root": {
                "child": {
                    "keep": 5,
                },
            },
        }
        self.assertEqual(o, r)

    def test_filter_deep_compacts_lists_but_path_keeps_original_indexes(self) -> None:
        i = {
            "rows": [
                {"v": "no"},
                {"v": "yes"},
                {"v": "no"},
                {"v": "yes"},
            ],
        }
        seen = []

        def predicate(path, key, val):
            if key == "v" and val == "yes":
                seen.append(list(path))
                return True
            return False

        o = _filter(i, predicate, deep=True)
        # unmatched items are removed, the surviving list is compacted...
        r = {
            "rows": [
                {"v": "yes"},
                {"v": "yes"},
            ],
        }
        self.assertEqual(o, r)
        # ...but callback paths still reference the original array indexes
        self.assertEqual(
            seen,
            [
                ["rows", 1, "v"],
                ["rows", 3, "v"],
            ],
        )

    def test_filter_deep_multiple_matches_stay_under_same_parent(self) -> None:
        i = {
            "parent": {
                "a": 1,
                "b": "x",
                "c": 2,
            },
        }
        o = _filter(i, lambda path, key, val: isinstance(val, int), deep=True)
        r = {
            "parent": {
                "a": 1,
                "c": 2,
            },
        }
        self.assertEqual(o, r)

    def test_filter_deep_matched_container_is_kept_even_when_emptied(self) -> None:
        i = {
            "keep_me": {"a": "x"},
            "drop_me": {"b": "y"},
        }
        o = _filter(i, lambda path, key, val: key == "keep_me", deep=True)
        r = {
            "keep_me": {},
        }
        self.assertEqual(o, r)

    def test_filter_deep_prunes_branches_without_any_match(self) -> None:
        i = {
            "a": {"b": "x"},
            "c": [1, 2],
        }
        o = _filter(i, lambda path, key, val: False, deep=True)
        self.assertEqual(o, {})
        self.assertFalse(i is o)

    def test_filter_deep_filters_tuples_and_returns_tuples(self) -> None:
        i = {
            "items": (
                {"id": 1, "v": "x"},
                {"id": 2, "v": "keep"},
            ),
        }
        o = _filter(i, lambda path, key, val: val == "keep", deep=True)
        r = {
            "items": ({"v": "keep"},),
        }
        self.assertEqual(o, r)
        self.assertIsInstance(o["items"], tuple)

    def test_filter_deep_legacy_two_arg_predicate_still_supported(self) -> None:
        i = {
            "a": 1,
            "b": "hello",
            "nested": {
                "c": 2,
                "d": "world",
            },
        }
        o = _filter(i, lambda key, val: not isinstance(val, str), deep=True)
        r = {
            "a": 1,
            "nested": {
                "c": 2,
            },
        }
        self.assertEqual(o, r)

    def test_filter_deep_varargs_predicate_receives_path(self) -> None:
        i = {"a": [{"b": 1}]}
        seen = []

        def predicate(*args):
            seen.append(args)
            return args[-1] == 1

        o = _filter(i, predicate, deep=True)
        r = {"a": [{"b": 1}]}
        self.assertEqual(o, r)
        self.assertIn((["a", 0, "b"], "b", 1), seen)
