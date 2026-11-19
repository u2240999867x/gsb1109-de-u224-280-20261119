import unittest

from benedict import benedict


class benedict_sources_test_case(unittest.TestCase):
    """
    This class describes a benedict merge sources test case.
    """

    def test_merge_sources_returns_source_map(self) -> None:
        b = benedict({"name": "svc"})
        sources = b.merge_sources({"port": 80}, sources=["yaml", "json"])
        self.assertEqual(b, {"name": "svc", "port": 80})
        self.assertEqual(sources["name"], "yaml")
        self.assertEqual(sources["port"], "json")

    def test_merge_sources_tracks_sources_on_instance(self) -> None:
        b = benedict({"db": {"host": "y", "port": 5432}})
        b.merge_sources({"db": {"host": "j"}}, sources=["yaml", "json"])
        self.assertEqual(b.source_of("db.host"), "json")
        self.assertEqual(b.source_of("db.port"), "yaml")
        self.assertEqual(b.sources["db.port"], "yaml")

    def test_merge_with_sources_does_not_modify_instance(self) -> None:
        original = {"db": {"host": "y", "port": 5432}}
        b = benedict(original)
        merged, sources = b.merge_with_sources(
            {"db": {"host": "j"}}, sources=["yaml", "json"]
        )
        self.assertEqual(b, {"db": {"host": "y", "port": 5432}})
        self.assertEqual(
            merged, {"db": {"host": "j", "port": 5432}}
        )
        self.assertEqual(sources["db.host"], "json")
        self.assertEqual(sources["db.port"], "yaml")
        self.assertEqual(merged.source_of("db.port"), "yaml")

    def test_merge_with_source_argument(self) -> None:
        b = benedict({"a": 1})
        b.merge({"b": 2, "c": 3}, source="json")
        self.assertEqual(b.source_of("a"), "base")
        self.assertEqual(b.source_of("b"), "json")
        self.assertEqual(b.source_of("c"), "json")

    def test_merge_with_sources_argument(self) -> None:
        b = benedict({"a": 1})
        sources = b.merge({"a": 2, "b": 2}, sources=["yaml", "json"])
        self.assertEqual(sources["a"], "json")
        self.assertEqual(sources["b"], "json")

    def test_merge_without_sources_keeps_returning_none(self) -> None:
        b = benedict({"a": 1})
        self.assertIsNone(b.merge({"b": 2}))
        self.assertEqual(b, {"a": 1, "b": 2})

    def test_sources_without_merge_are_empty(self) -> None:
        b = benedict({"a": 1})
        self.assertEqual(b.sources.flat, {})
        self.assertFalse(b.sources)
        with self.assertRaises(KeyError):
            b.source_of("a")

    def test_source_of_unknown_key(self) -> None:
        b = benedict({"a": 1})
        b.merge_sources({"b": 2}, sources=["yaml", "json"])
        with self.assertRaises(KeyError):
            b.source_of("c")
        self.assertIsNone(b.sources.get("c"))

    def test_sources_are_kept_by_clone_and_copy(self) -> None:
        b = benedict({"db": {"host": "y", "port": 1}})
        b.merge_sources({"db": {"host": "j"}}, sources=["yaml", "json"])
        for clone in (b.clone(), b.copy(), b.deepcopy()):
            self.assertEqual(clone.source_of("db.host"), "json")
            self.assertEqual(clone.source_of("db.port"), "yaml")

    def test_sources_are_accumulated_across_merges(self) -> None:
        b = benedict({"a": 1, "db": {"host": "y", "port": 1}})
        b.merge_sources({"db": {"host": "j"}}, sources=["yaml", "rev1"])
        b.merge_sources({"db": {"user": "u"}}, sources=["yaml", "rev2"])
        self.assertEqual(b.source_of("a"), "yaml")
        self.assertEqual(b.source_of("db.host"), "rev1")
        self.assertEqual(b.source_of("db.port"), "yaml")
        self.assertEqual(b.source_of("db.user"), "rev2")

    def test_merge_sources_with_keypath_separator(self) -> None:
        b = benedict({"db": {"host": "y", "port": 1}}, keypath_separator="/")
        b.merge_sources({"db": {"host": "j"}}, sources=["yaml", "json"])
        self.assertEqual(b.source_of("db/host"), "json")
        self.assertEqual(b.source_of("db/port"), "yaml")
        self.assertEqual(b.sources["db/host"], "json")

    def test_merge_sources_with_frozen_dict(self) -> None:
        b = benedict({"a": 1}).freeze()
        with self.assertRaises(TypeError):
            b.merge_sources({"b": 2}, sources=["yaml", "json"])

    def test_merge_with_sources_keeps_frozen_state(self) -> None:
        b = benedict({"a": 1}).freeze()
        merged, sources = b.merge_with_sources({"b": 2}, sources=["yaml", "json"])
        self.assertTrue(merged.frozen)
        self.assertEqual(sources["b"], "json")

    def test_source_of_nested_dict(self) -> None:
        b = benedict({"db": {"host": "y", "port": 1}})
        b.merge_sources({"db": {"host": "j"}}, sources=["yaml", "json"])
        self.assertEqual(b["db"].source_of("host"), "json")
        self.assertEqual(b["db"].source_of("port"), "yaml")
        self.assertEqual(b.get("db").source_of("port"), "yaml")
        self.assertEqual(b.items().__iter__().__next__()[1].source_of("host"), "json")

    def test_merge_with_sources_result_attributes(self) -> None:
        b = benedict({"a": 1})
        result = b.merge_with_sources({"b": 2}, sources=["yaml", "json"])
        self.assertEqual(result.merged, {"a": 1, "b": 2})
        self.assertEqual(result.data, {"a": 1, "b": 2})
        self.assertEqual(result.sources["b"], "json")
        self.assertEqual(result.source_map["b"], "json")
        self.assertEqual(result.field_sources["a"], "yaml")

    def test_source_of_default(self) -> None:
        b = benedict({"a": 1})
        b.merge_sources({"b": 2}, sources=["yaml", "json"])
        self.assertEqual(b.source_of("c", "default"), "default")
        self.assertEqual(b.get_source("c", "default"), "default")
        self.assertIsNone(b.get_source("c"))

    def test_sources_flat_and_nested_views(self) -> None:
        b = benedict({"db": {"host": "y", "port": 1}, "name": "svc"})
        b.merge_sources({"db": {"host": "j"}}, sources=["yaml", "json"])
        self.assertEqual(
            b.sources.flat,
            {
                "db": "yaml",
                "db.host": "json",
                "db.port": "yaml",
                "name": "yaml",
            },
        )
        self.assertEqual(
            b.sources.nested,
            {
                "db": {"__source__": "yaml", "host": "json", "port": "yaml"},
                "name": "yaml",
            },
        )
        self.assertEqual(b.sources, b.sources.flat)

    def test_sources_are_pickled(self) -> None:
        import pickle

        b = benedict({"a": 1})
        b.merge_sources({"b": 2}, sources=["yaml", "json"])
        restored = pickle.loads(pickle.dumps(b))
        self.assertEqual(restored, {"a": 1, "b": 2})
        self.assertEqual(restored.source_of("b"), "json")

    def test_sources_do_not_appear_as_keys(self) -> None:
        b = benedict({"a": 1})
        b.merge_sources({"b": 2}, sources=["yaml", "json"])
        self.assertNotIn("_sources", b)
        self.assertEqual(list(b.keys()), ["a", "b"])
        self.assertEqual(len(b), 2)

    def test_source_of_list_items(self) -> None:
        b = benedict({"tags": ["a"]})
        b.merge_sources({"tags": ["b"]}, sources=["yaml", "json"], concat=True)
        self.assertEqual(b.source_of("tags[0]"), "yaml")
        self.assertEqual(b.source_of("tags[1]"), "json")

    def test_source_of_dicts_in_lists(self) -> None:
        b = benedict({"items": [{"name": "a"}]})
        b.merge_sources({"items": [{"name": "b"}]}, sources=["yaml", "json"])
        self.assertEqual(b.source_of("items[0].name"), "json")
        self.assertEqual(b["items"][0].source_of("name"), "json")
        self.assertEqual(b["items[0]"].source_of("name"), "json")
