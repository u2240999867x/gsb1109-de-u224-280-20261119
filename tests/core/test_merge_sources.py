import unittest

from benedict.core import merge as _merge
from benedict.core import merge_sources as _merge_sources
from benedict.core import merge_with_sources as _merge_with_sources


class merge_sources_test_case(unittest.TestCase):
    """
    This class describes a merge with sources test case.
    """

    def test_merge_sources_returns_merged_dict(self) -> None:
        d = {"a": 1}
        m = {"b": 2}
        source_map = _merge_sources(d, m, sources=["yaml", "json"])
        self.assertEqual(d, {"a": 1, "b": 2})
        self.assertEqual(source_map["a"], "yaml")
        self.assertEqual(source_map["b"], "json")

    def test_merge_with_sources_returns_both(self) -> None:
        d = {"a": 1}
        m = {"b": 2}
        merged, source_map = _merge_with_sources(d, m, sources=["yaml", "json"])
        self.assertIs(merged, d)
        self.assertEqual(merged, {"a": 1, "b": 2})
        self.assertEqual(source_map["b"], "json")

    def test_merge_sources_with_default_labels(self) -> None:
        d = {"a": 1}
        source_map = _merge_sources(d, {"b": 2})
        self.assertEqual(source_map["a"], "base")
        self.assertEqual(source_map["b"], "other")

    def test_merge_sources_with_default_labels_and_multiple_dicts(self) -> None:
        d = {"a": 1}
        source_map = _merge_sources(d, {"b": 2}, {"c": 3})
        self.assertEqual(source_map["a"], "base")
        self.assertEqual(source_map["b"], "other")
        self.assertEqual(source_map["c"], "other2")

    def test_merge_sources_with_single_label(self) -> None:
        d = {"a": 1}
        source_map = _merge_sources(d, {"b": 2}, sources="json")
        self.assertEqual(source_map["a"], "base")
        self.assertEqual(source_map["b"], "json")

    def test_merge_sources_with_invalid_labels_count(self) -> None:
        with self.assertRaises(ValueError):
            _merge_sources({"a": 1}, {"b": 2}, sources=["yaml"])

    def test_merge_sources_query_by_keypath_and_keylist(self) -> None:
        d = {"db": {"host": "y", "port": 5432}}
        m = {"db": {"host": "j"}}
        source_map = _merge_sources(d, m, sources=["yaml", "json"])
        self.assertEqual(source_map["db.host"], "json")
        self.assertEqual(source_map[["db", "host"]], "json")
        self.assertEqual(source_map[("db", "host")], "json")
        self.assertEqual(source_map["db.port"], "yaml")

    def test_merge_sources_nested_query(self) -> None:
        d = {"db": {"host": "y", "port": 5432}}
        m = {"db": {"host": "j"}}
        source_map = _merge_sources(d, m, sources=["yaml", "json"])
        self.assertEqual(source_map["db"]["host"], "json")
        self.assertEqual(source_map["db"]["port"], "yaml")
        # the parent label behaves like the label string
        self.assertEqual(source_map["db"], "yaml")

    def test_merge_sources_with_custom_separator(self) -> None:
        d = {"db": {"host": "y"}}
        m = {"db": {"host": "j"}}
        source_map = _merge_sources(d, m, sources=["yaml", "json"], separator="/")
        self.assertEqual(source_map["db/host"], "json")
        self.assertEqual(source_map[["db", "host"]], "json")

    def test_merge_sources_keeps_source_of_untouched_sibling_fields(self) -> None:
        d = {"db": {"host": "y", "port": 5432}}
        m = {"db": {"host": "j"}}
        source_map = _merge_sources(d, m, sources=["yaml", "json"])
        self.assertEqual(source_map["db.port"], "yaml")
        self.assertEqual(source_map["db.host"], "json")

    def test_merge_sources_preserves_parent_source_for_new_sibling(self) -> None:
        d = {"db": {"host": "y", "port": 5432}}
        m = {"db": {"user": "u"}}
        source_map = _merge_sources(d, m, sources=["yaml", "json"])
        # both dicts are merged: the parent and its existing fields keep
        # their source, the new sibling gets the merged dict source
        self.assertEqual(source_map["db"], "yaml")
        self.assertEqual(source_map["db.host"], "yaml")
        self.assertEqual(source_map["db.port"], "yaml")
        self.assertEqual(source_map["db.user"], "json")

    def test_merge_sources_with_dict_replaced_by_scalar(self) -> None:
        d = {"db": {"host": "y", "port": 5432}}
        m = {"db": "sqlite"}
        source_map = _merge_sources(d, m, sources=["yaml", "json"])
        self.assertNotIn("db.host", source_map)
        self.assertNotIn("db.port", source_map)
        self.assertEqual(source_map["db"], "json")

    def test_merge_sources_with_list_replaced_by_dict(self) -> None:
        d = {"db": ["a", "b"]}
        m = {"db": {"host": "j"}}
        source_map = _merge_sources(d, m, sources=["yaml", "json"])
        self.assertNotIn("db[0]", source_map)
        self.assertNotIn("db[1]", source_map)
        self.assertEqual(source_map["db"], "json")
        self.assertEqual(source_map["db.host"], "json")

    def test_merge_sources_with_scalar_replaced_by_dict(self) -> None:
        d = {"db": "sqlite"}
        m = {"db": {"host": "j"}}
        source_map = _merge_sources(d, m, sources=["yaml", "json"])
        self.assertEqual(source_map["db"], "json")
        self.assertEqual(source_map["db.host"], "json")

    def test_merge_sources_with_dict_replaced_by_list(self) -> None:
        d = {"tags": {"a": 1}}
        m = {"tags": ["x", "y"]}
        source_map = _merge_sources(d, m, sources=["yaml", "json"])
        self.assertNotIn("tags.a", source_map)
        self.assertEqual(source_map["tags"], "json")
        self.assertEqual(source_map["tags[0]"], "json")
        self.assertEqual(source_map["tags[1]"], "json")

    def test_merge_sources_with_list_replaced(self) -> None:
        d = {"tags": ["a", "b", "c"]}
        m = {"tags": ["d"]}
        source_map = _merge_sources(d, m, sources=["yaml", "json"])
        self.assertEqual(source_map["tags"], "json")
        self.assertEqual(source_map["tags[0]"], "json")
        self.assertNotIn("tags[1]", source_map)
        self.assertNotIn("tags[2]", source_map)

    def test_merge_sources_with_list_concat(self) -> None:
        d = {"tags": ["a", "b"]}
        m = {"tags": ["c"]}
        source_map = _merge_sources(
            d, m, sources=["yaml", "json"], concat=True
        )
        self.assertEqual(d, {"tags": ["a", "b", "c"]})
        self.assertEqual(source_map["tags"], "yaml")
        self.assertEqual(source_map["tags[0]"], "yaml")
        self.assertEqual(source_map["tags[1]"], "yaml")
        self.assertEqual(source_map["tags[2]"], "json")

    def test_merge_sources_without_overwrite(self) -> None:
        d = {"a": 1, "db": {"host": "y", "port": 1}}
        m = {"a": 2, "db": {"host": "j", "user": "u"}}
        source_map = _merge_sources(
            d, m, sources=["yaml", "json"], overwrite=False
        )
        self.assertEqual(
            d,
            {"a": 1, "db": {"host": "y", "port": 1, "user": "u"}},
        )
        self.assertEqual(source_map["a"], "yaml")
        self.assertEqual(source_map["db.host"], "yaml")
        self.assertEqual(source_map["db.user"], "json")

    def test_merge_sources_with_multiple_dicts(self) -> None:
        d = {"a": 1}
        source_map = _merge_sources(
            d,
            {"a": 2, "b": 2},
            {"b": 3, "c": 3},
            sources=["yaml", "rev1", "rev2"],
        )
        self.assertEqual(source_map["a"], "rev1")
        self.assertEqual(source_map["b"], "rev2")
        self.assertEqual(source_map["c"], "rev2")

    def test_merge_sources_missing_key(self) -> None:
        source_map = _merge_sources({"a": 1}, {"b": 2})
        with self.assertRaises(KeyError):
            source_map["c"]
        self.assertIsNone(source_map.get("c"))
        self.assertEqual(source_map.get("c", "default"), "default")

    def test_merge_sources_flat_and_nested(self) -> None:
        d = {"db": {"host": "y", "port": 5432}}
        m = {"db": {"host": "j"}, "name": "svc"}
        source_map = _merge_sources(d, m, sources=["yaml", "json"])
        self.assertEqual(
            source_map.flat,
            {
                "db": "yaml",
                "db.host": "json",
                "db.port": "yaml",
                "name": "json",
            },
        )
        self.assertEqual(
            source_map.to_nested(),
            {
                "db": {"__source__": "yaml", "host": "json", "port": "yaml"},
                "name": "json",
            },
        )
        self.assertEqual(source_map.as_dict(), source_map.flat)
