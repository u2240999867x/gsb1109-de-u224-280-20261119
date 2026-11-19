import unittest
from collections.abc import Iterator

from benedict.core import remove as _remove
from benedict.core import remove_keypaths as _remove_keypaths
from benedict.core import remove_manifest as _remove_manifest


class remove_test_case(unittest.TestCase):
    """
    This class describes a remove test case.
    """

    def test_remove_legacy_sequence_keys(self) -> None:
        class Keys:
            def __getitem__(self, index: int) -> str:
                return ["a", "b"][index]

        data = {"a": 1, "b": 2, "keep": 3}
        _remove(data, Keys())
        self.assertEqual(data, {"keep": 3})

    def test_remove_preserves_iteration_errors(self) -> None:
        def keys() -> Iterator[str]:
            yield "a"
            raise TypeError("keys iteration failed")

        data = {"a": 1, "b": 2}
        with self.assertRaisesRegex(TypeError, "keys iteration failed"):
            _remove(data, keys())
        self.assertEqual(data, {"a": 1, "b": 2})

    def test_remove_scalar_nonstring_keys(self) -> None:
        for key in (0, 12, None):
            with self.subTest(key=key):
                data = {key: "removed", "keep": 1}
                _remove(data, key)
                self.assertEqual(data, {"keep": 1})

    def test_remove_scalar_keys_deep_and_with_args(self) -> None:
        data = {0: "top", 1: "other", "nested": {0: "child", "keep": 1}}
        _remove(data, 0, 1, deep=True)
        self.assertEqual(data, {"nested": {"keep": 1}})

    def test_remove_with_single_key(self) -> None:
        d = {
            "a": 1,
            "b": 2,
            "c": 3,
        }
        _remove(d, "c")
        r = {
            "a": 1,
            "b": 2,
        }
        self.assertEqual(d, r)

    def test_remove_with_multiple_keys_as_args(self) -> None:
        d = {
            "a": 1,
            "b": 2,
            "c": 3,
            "d": 4,
            "e": 5,
        }
        _remove(d, "c", "d", "e")
        r = {
            "a": 1,
            "b": 2,
        }
        self.assertEqual(d, r)

    def test_remove_with_multiple_keys_as_list(self) -> None:
        d = {
            "a": 1,
            "b": 2,
            "c": 3,
            "d": 4,
            "e": 5,
        }
        _remove(d, ["c", "d", "e"])
        r = {
            "a": 1,
            "b": 2,
        }
        self.assertEqual(d, r)

    def test_remove_deep_with_single_key(self) -> None:
        d = {
            "a": 1,
            "b": {
                "password": "secret",
                "name": "test",
            },
            "password": "topsecret",
        }
        _remove(d, "password", deep=True)
        r = {
            "a": 1,
            "b": {
                "name": "test",
            },
        }
        self.assertEqual(d, r)

    def test_remove_deep_with_multiple_keys(self) -> None:
        d = {
            "a": 1,
            "secret": "x",
            "b": {
                "secret": "y",
                "token": "z",
                "name": "test",
                "c": {
                    "token": "w",
                    "value": 42,
                },
            },
        }
        _remove(d, ["secret", "token"], deep=True)
        r = {
            "a": 1,
            "b": {
                "name": "test",
                "c": {
                    "value": 42,
                },
            },
        }
        self.assertEqual(d, r)

    def test_remove_deep_inside_list(self) -> None:
        d = {
            "users": [
                {"name": "Alice", "password": "abc"},
                {"name": "Bob", "password": "xyz"},
            ],
            "password": "admin",
        }
        _remove(d, "password", deep=True)
        r = {
            "users": [
                {"name": "Alice"},
                {"name": "Bob"},
            ],
        }
        self.assertEqual(d, r)

    def test_remove_without_deep_leaves_nested_untouched(self) -> None:
        d = {
            "a": 1,
            "b": {
                "password": "secret",
            },
            "password": "topsecret",
        }
        _remove(d, "password")
        r = {
            "a": 1,
            "b": {
                "password": "secret",
            },
        }
        self.assertEqual(d, r)

    def test_remove_deep_inside_list_nested_in_dict(self) -> None:
        d = {
            "section": {
                "users": [
                    {"name": "Alice", "password": "abc"},
                    {"name": "Bob", "password": "xyz"},
                ],
            },
        }
        _remove(d, "password", deep=True)
        r = {
            "section": {
                "users": [
                    {"name": "Alice"},
                    {"name": "Bob"},
                ],
            },
        }
        self.assertEqual(d, r)

    def test_remove_deep_inside_tuple_nested_in_dict(self) -> None:
        d = {
            "section": {
                "users": (
                    {"name": "Alice", "password": "abc"},
                    {"name": "Bob", "password": "xyz"},
                ),
            },
        }
        _remove(d, "password", deep=True)
        r = {
            "section": {
                "users": (
                    {"name": "Alice"},
                    {"name": "Bob"},
                ),
            },
        }
        self.assertEqual(d, r)

    def test_remove_deep_inside_nested_lists(self) -> None:
        d = {"users": [[{"name": "Alice", "password": "abc", "token": "xyz"}]]}
        _remove(d, ["password"], "token", deep=True)
        self.assertEqual(d, {"users": [[{"name": "Alice"}]]})

    def test_remove_deep_inside_mixed_nested_sequences(self) -> None:
        d = {"users": ([({"name": "Alice", "password": "abc"},)],)}
        _remove(d, "password", deep=True)
        self.assertEqual(d, {"users": ([({"name": "Alice"},)],)})


class remove_batch_test_case(unittest.TestCase):
    """
    This class describes a batch keypath removal test case.
    """

    def test_remove_consecutive_list_items_uses_original_indexes(self) -> None:
        d = {"items": [{"note": "a"}, {"note": "b"}, {"note": "c"}]}
        _remove_keypaths(d, ["items[0].note", "items[1].note"])
        self.assertEqual(d, {"items": [{}, {}, {"note": "c"}]})

    def test_remove_consecutive_list_items_deletes_each_item(self) -> None:
        d = {"items": ["a", "b", "c"]}
        _remove_keypaths(d, ["items[0]", "items[1]"])
        self.assertEqual(d, {"items": ["c"]})

    def test_remove_list_items_at_multiple_levels(self) -> None:
        d = {
            "sources": [
                {"notes": [{"temp": 1}, {"temp": 2}], "temp": 3},
                {"notes": [{"temp": 4}, {"temp": 5}], "temp": 6},
            ]
        }
        _remove_keypaths(
            d,
            [
                "sources[0].notes[0].temp",
                "sources[0].notes[1].temp",
                "sources[1].notes[0].temp",
                "sources[1].notes[1].temp",
            ],
        )
        self.assertEqual(
            d,
            {
                "sources": [
                    {"notes": [{}, {}], "temp": 3},
                    {"notes": [{}, {}], "temp": 6},
                ]
            },
        )

    def test_remove_negative_indexes(self) -> None:
        d = {"items": [{"note": "a"}, {"note": "b"}]}
        _remove_keypaths(d, ["items[-1].note", "items[-2].note"])
        self.assertEqual(d, {"items": [{}, {}]})

    def test_remove_strict_stops_before_touching_anything(self) -> None:
        d = {"items": [{"note": "a"}], "keep": 1}
        with self.assertRaises(KeyError):
            _remove_keypaths(d, ["items[0].note", "missing.note"])
        self.assertEqual(d, {"items": [{"note": "a"}], "keep": 1})

    def test_remove_non_strict_keeps_removing_valid_items(self) -> None:
        d = {"a": {"b": 1}, "c": 2}
        _remove_keypaths(d, ["a.b", "missing"], strict=False)
        self.assertEqual(d, {"a": {}, "c": 2})

    def test_remove_manifest_removes_matching_items(self) -> None:
        d = {"items": [{"note": "a"}, {"note": "b"}]}
        _remove_manifest(d, {"items[0].note": "a", "items[1].note": "b"})
        self.assertEqual(d, {"items": [{}, {}]})

    def test_remove_manifest_mismatch_stops_whole_batch(self) -> None:
        d = {"items": [{"note": "a"}, {"note": "b"}]}
        with self.assertRaises(KeyError):
            _remove_manifest(d, {"items[0].note": "a", "items[1].note": "wrong"})
        self.assertEqual(d, {"items": [{"note": "a"}, {"note": "b"}]})

    def test_remove_manifest_missing_item_stops_whole_batch(self) -> None:
        d = {"items": [{"note": "a"}]}
        with self.assertRaises(KeyError):
            _remove_manifest(d, {"items[0].note": "a", "items[1].note": "b"})
        self.assertEqual(d, {"items": [{"note": "a"}]})

    def test_remove_manifest_removes_parent_with_children(self) -> None:
        d = {"items": [{"note": "a"}, {"note": "b"}], "keep": 1}
        _remove_manifest(d, {"items[0]": {"note": "a"}, "items[0].note": "a"})
        self.assertEqual(d, {"items": [{"note": "b"}], "keep": 1})

    def test_remove_tuple_item_raises(self) -> None:
        d = {"items": ({"note": "a"}, {"note": "b"})}
        with self.assertRaises(TypeError):
            _remove_keypaths(d, ["items[0]"])
        self.assertEqual(d, {"items": ({"note": "a"}, {"note": "b"})})

    def test_remove_deep_ignores_strict(self) -> None:
        d = {"password": "a", "user": {"password": "b"}}
        _remove(d, "password", deep=True, strict=True)
        self.assertEqual(d, {"user": {}})
