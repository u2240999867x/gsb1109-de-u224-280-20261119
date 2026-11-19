import unittest

from benedict.core import rename as _rename
from benedict.core import rename_items as _rename_items


class rename_test_case(unittest.TestCase):
    """
    This class describes a rename test case.
    """

    def test_rename(self) -> None:
        d = {
            "a": {
                "x": 1,
                "y": 1,
            },
            "b": {
                "x": 2,
                "y": 2,
            },
            "c": {
                "x": 3,
                "y": 3,
            },
        }
        _rename(d, "a", "d")
        r = {
            "b": {
                "x": 2,
                "y": 2,
            },
            "c": {
                "x": 3,
                "y": 3,
            },
            "d": {
                "x": 1,
                "y": 1,
            },
        }
        self.assertEqual(d, r)

    def test_rename_with_same_key(self) -> None:
        d = {
            "a": 1,
            "b": 2,
        }
        _rename(d, "a", "a")
        r = {
            "a": 1,
            "b": 2,
        }
        self.assertEqual(d, r)

    def test_rename_to_existing_name(self) -> None:
        d = {
            "a": 1,
            "b": 2,
            "c": 3,
        }
        with self.assertRaises(KeyError):
            _rename(d, "a", "c")

    def test_rename_deep(self) -> None:
        d = {
            "fname": "Alice",
            "address": {
                "fname": "street",
                "city": "Rome",
            },
        }
        _rename(d, "fname", "first_name", deep=True)
        r = {
            "first_name": "Alice",
            "address": {
                "first_name": "street",
                "city": "Rome",
            },
        }
        self.assertEqual(d, r)

    def test_rename_deep_key_not_at_every_level(self) -> None:
        d = {
            "a": 1,
            "nested": {
                "b": 2,
                "c": 3,
            },
            "x": {
                "a": 10,
                "d": 4,
            },
        }
        _rename(d, "a", "z", deep=True)
        r = {
            "z": 1,
            "nested": {
                "b": 2,
                "c": 3,
            },
            "x": {
                "z": 10,
                "d": 4,
            },
        }
        self.assertEqual(d, r)

    def test_rename_deep_inside_list(self) -> None:
        d = {
            "users": [
                {"fname": "Alice", "age": 30},
                {"fname": "Bob", "age": 25},
            ],
        }
        _rename(d, "fname", "first_name", deep=True)
        r = {
            "users": [
                {"first_name": "Alice", "age": 30},
                {"first_name": "Bob", "age": 25},
            ],
        }
        self.assertEqual(d, r)

    def test_rename_deep_without_deep_leaves_nested_untouched(self) -> None:
        d = {
            "a": 1,
            "nested": {
                "a": 2,
            },
        }
        _rename(d, "a", "z")
        r = {
            "z": 1,
            "nested": {
                "a": 2,
            },
        }
        self.assertEqual(d, r)

    def test_rename_deep_inside_list_nested_in_dict(self) -> None:
        d = {
            "section": {
                "users": [
                    {"fname": "Alice", "age": 30},
                    {"fname": "Bob", "age": 25},
                ],
            },
        }
        _rename(d, "fname", "first_name", deep=True)
        r = {
            "section": {
                "users": [
                    {"first_name": "Alice", "age": 30},
                    {"first_name": "Bob", "age": 25},
                ],
            },
        }
        self.assertEqual(d, r)

    def test_rename_deep_inside_tuple_nested_in_dict(self) -> None:
        d = {
            "section": {
                "users": (
                    {"fname": "Alice", "age": 30},
                    {"fname": "Bob", "age": 25},
                ),
            },
        }
        _rename(d, "fname", "first_name", deep=True)
        r = {
            "section": {
                "users": (
                    {"first_name": "Alice", "age": 30},
                    {"first_name": "Bob", "age": 25},
                ),
            },
        }
        self.assertEqual(d, r)

    def test_rename_deep_inside_nested_lists(self) -> None:
        d = {"users": [[{"fname": "Alice"}], [42, [{"fname": "Bob"}]]]}
        _rename(d, "fname", "first_name", deep=True)
        self.assertEqual(
            d,
            {"users": [[{"first_name": "Alice"}], [42, [{"first_name": "Bob"}]]]},
        )

    def test_rename_deep_inside_nested_tuples_under_dict(self) -> None:
        d = {"section": {"users": ([({"fname": "Alice"},)],)}}
        _rename(d, "fname", "first_name", deep=True)
        self.assertEqual(d, {"section": {"users": ([({"first_name": "Alice"},)],)}})


class rename_items_test_case(unittest.TestCase):
    """
    This class describes a rename_items test case.
    """

    def test_rename_items_swap(self) -> None:
        # Telemetry temperature meanings are swapped between A and B.
        d = {"temperature_a": 10, "temperature_b": 20, "unit": "celsius"}
        changed = _rename_items(
            d, {"temperature_a": "temperature_b", "temperature_b": "temperature_a"}
        )
        self.assertTrue(changed)
        r = {"temperature_a": 20, "temperature_b": 10, "unit": "celsius"}
        self.assertEqual(d, r)

    def test_rename_items_swap_keeps_both_values(self) -> None:
        d = {"a": {"x": 1}, "b": {"y": 2}}
        _rename_items(d, {"a": "b", "b": "a"})
        self.assertEqual(d, {"a": {"y": 2}, "b": {"x": 1}})

    def test_rename_items_swap_dicts_are_independent(self) -> None:
        d = {"a": {"x": 1}, "b": {"y": 2}}
        _rename_items(d, {"a": "b", "b": "a"})
        d["a"]["z"] = 3
        self.assertEqual(d["b"], {"x": 1})

    def test_rename_items_chain(self) -> None:
        d = {"a": 1, "b": 2, "c": 3}
        _rename_items(d, {"a": "b", "b": "c", "c": "a"})
        self.assertEqual(d, {"a": 3, "b": 1, "c": 2})

    def test_rename_items_move_nested_field_at_top_level(self) -> None:
        d = {"temperature": {"a": 10, "b": 20}, "meta": {"id": 1}}
        changed = _rename_items(d, {"temperature": "readings", "meta": "info"})
        self.assertTrue(changed)
        r = {"readings": {"a": 10, "b": 20}, "info": {"id": 1}}
        self.assertEqual(d, r)

    def test_rename_items_swap_inside_nested_dict(self) -> None:
        d = {"telemetry": {"a": 10, "b": 20}}
        _rename_items(d, {"a": "b", "b": "a"}, deep=True)
        self.assertEqual(d, {"telemetry": {"a": 20, "b": 10}})

    def test_rename_items_deep_inside_list(self) -> None:
        d = {"readings": [{"a": 1, "b": 2}, {"a": 3, "b": 4}]}
        _rename_items(d, {"a": "b", "b": "a"}, deep=True)
        r = {"readings": [{"a": 2, "b": 1}, {"a": 4, "b": 3}]}
        self.assertEqual(d, r)

    def test_rename_items_deep_inside_tuple(self) -> None:
        d = {"readings": ({"a": 1, "b": 2},)}
        _rename_items(d, {"a": "b", "b": "a"}, deep=True)
        self.assertEqual(d, {"readings": ({"a": 2, "b": 1},)})

    def test_rename_items_destination_is_existing_key(self) -> None:
        d = {"a": 1, "b": 2, "c": 3}
        with self.assertRaises(KeyError):
            _rename_items(d, {"a": "c"})
        self.assertEqual(d, {"a": 1, "b": 2, "c": 3})

    def test_rename_items_swap_replaces_existing_destination_value(self) -> None:
        # In a swap the existing destination value is itself moved away.
        d = {"a": 1, "b": 2, "c": 3}
        _rename_items(d, {"a": "b", "b": "a"})
        self.assertEqual(d, {"a": 2, "b": 1, "c": 3})

    def test_rename_items_duplicate_destinations(self) -> None:
        d = {"a": 1, "b": 2}
        with self.assertRaises(KeyError):
            _rename_items(d, {"a": "z", "b": "z"})
        self.assertEqual(d, {"a": 1, "b": 2})

    def test_rename_items_missing_source_is_ignored(self) -> None:
        d = {"a": 1, "b": 2}
        changed = _rename_items(d, {"a": "z", "missing": "w"})
        self.assertTrue(changed)
        self.assertEqual(d, {"z": 1, "b": 2})

    def test_rename_items_no_changes_returns_false(self) -> None:
        d = {"a": 1, "b": 2}
        self.assertFalse(_rename_items(d, {"x": "y"}))
        self.assertFalse(_rename_items(d, {"a": "a"}))
        self.assertEqual(d, {"a": 1, "b": 2})

    def test_rename_items_empty_changes_returns_false(self) -> None:
        d = {"a": 1}
        self.assertFalse(_rename_items(d, {}))
        self.assertEqual(d, {"a": 1})

    def test_rename_items_plain_dict_literal_dotted_keys(self) -> None:
        # Plain dicts have no keypath separator: dotted keys are literal.
        d = {"a.x": 1, "a": {"x": 2}}
        _rename_items(d, {"a.x": "a.y"})
        self.assertEqual(d, {"a.y": 1, "a": {"x": 2}})
