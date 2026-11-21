from io import BytesIO, StringIO
from unittest import TestCase

from json_stream import ANY, MISSING, Retain, load, load_many
from json_stream.base import TransientStreamingJSONObject, TransientStreamingJSONList
from json_stream.retain import (
    RetainedDict,
    RetainedList,
    RetainingStreamingJSONObject,
    RetainingStreamingJSONList,
)
from json_stream.select_tokenizer import default_tokenizer


DECLARATION = Retain(
    (ANY, "order_id"),
    (ANY, "header", "order_id"),
    (ANY, "header", "summary", "total"),
)


def controlled_tokenizer(fail_at_token):
    """Tokenizer wrapper that raises once ``fail_at_token`` tokens were yielded."""
    def tokenizer(fp, **kwargs):
        for index, token in enumerate(default_tokenizer(fp, **kwargs)):
            if index == fail_at_token:
                raise ValueError("controlled truncation")
            yield token
    return tokenizer


class TestRetain(TestCase):
    def test_retain_selected_fields_after_transient_consumption(self):
        payload = (
            '[{"order_id": "A1", "header": {"order_id": "A1", '
            '"summary": {"total": 10, "currency": "USD"}, '
            '"audit": {"blob": "huge"}}, "logs": ["l1", "l2", "l3"]}, '
            '{"logs": ["L1"], "header": {"audit": {"blob": "bigger"}, '
            '"summary": {"total": 20}, "order_id": "B2"}, "order_id": "B2"}]'
        )
        data = load(StringIO(payload), retain=DECLARATION)
        self.assertIsInstance(data, RetainingStreamingJSONList)

        orders = iter(data)
        first = next(orders)
        self.assertIsInstance(first, RetainingStreamingJSONObject)

        # consume only part of the big log array in this pass
        logs = first["logs"]
        self.assertIsInstance(logs, TransientStreamingJSONList)
        self.assertEqual(next(iter(logs)), "l1")
        self.assertFalse(first.is_confirmed())

        # advancing the outer stream finishes/drains the first order
        second = next(orders)
        self.assertTrue(first.is_confirmed())
        self.assertFalse(second.is_confirmed())

        # selected fields are still available from the finished order
        snapshot1 = first.retained()
        self.assertEqual(snapshot1["order_id"], "A1")
        self.assertEqual(snapshot1["header"]["order_id"], "A1")
        self.assertEqual(snapshot1["header"]["summary"]["total"], 10)

        # the whole header, logs and unselected branches are not resident
        self.assertEqual(set(snapshot1.keys()), {"order_id", "header"})
        self.assertEqual(set(snapshot1["header"].keys()), {"order_id", "summary"})
        self.assertEqual(set(snapshot1["header"]["summary"].keys()), {"total"})
        self.assertNotIn("logs", snapshot1)
        self.assertNotIn("audit", snapshot1["header"])
        self.assertNotIn("currency", snapshot1["header"]["summary"])

        # finish the second order; field order was swapped, same declaration works
        data.read_all()
        self.assertTrue(second.is_confirmed())
        snapshot2 = second.retained()
        self.assertEqual(snapshot2["order_id"], "B2")
        self.assertEqual(snapshot2["header"]["order_id"], "B2")
        self.assertEqual(snapshot2["header"]["summary"]["total"], 20)
        self.assertNotIn("audit", snapshot2["header"])

    def test_retain_explicit_null_distinguishable_from_missing(self):
        payload = (
            '[{"order_id": "A1", "header": {"order_id": null, '
            '"summary": {"total": null}}, "logs": []}, '
            '{"order_id": "B2", "header": {"summary": {}}, "logs": []}]'
        )
        data = load(StringIO(payload), retain=DECLARATION)
        orders = list(iter(data))
        data.read_all()

        first, second = orders
        snapshot1 = first.retained()
        # explicit nulls are retained as None
        self.assertIsNone(snapshot1["header"]["order_id"])
        self.assertIsNone(snapshot1["header"]["summary"]["total"])
        self.assertEqual(snapshot1["header"].missing(), ())

        snapshot2 = second.retained()
        # missing declared paths use the MISSING sentinel
        self.assertIs(snapshot2["header"]["order_id"], MISSING)
        self.assertIs(snapshot2["header"]["summary"]["total"], MISSING)
        self.assertEqual(snapshot2["header"].missing(), ("order_id",))
        self.assertEqual(snapshot2["header"]["summary"].missing(), ("total",))
        self.assertIn("order_id", snapshot2["header"])
        self.assertNotEqual(snapshot2["header"]["order_id"], None)

    def test_retain_duplicate_target_key_records_every_occurrence(self):
        payload = (
            '[{"order_id": "first", "order_id": "second", '
            '"header": {"order_id": "h1", "summary": {"total": 1, "total": 2}}, '
            '"logs": []}]'
        )
        data = load(StringIO(payload), retain=DECLARATION)
        order = next(iter(data))
        data.read_all()

        snapshot = order.retained()
        # mapping access sees the last occurrence
        self.assertEqual(snapshot["order_id"], "second")
        # every occurrence is inspectable and the conflict is reported
        self.assertEqual(snapshot.occurrences("order_id"), ["first", "second"])
        self.assertEqual(snapshot.conflicts(), {"order_id": ["first", "second"]})

        summary = snapshot["header"]["summary"]
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary.occurrences("total"), [1, 2])
        self.assertEqual(summary.conflicts(), {"total": [1, 2]})
        self.assertEqual(snapshot["header"].conflicts(), {})

    def test_retain_does_not_cache_fields_merely_read_by_caller(self):
        payload = (
            '[{"order_id": "A1", "header": {"order_id": "A1", '
            '"summary": {"total": 10}, "audit": {"blob": "x"}}, '
            '"logs": ["l1", "l2"], "note": "ephemeral"}]'
        )
        data = load(StringIO(payload), retain=DECLARATION)
        order = next(iter(data))
        # caller reads unselected transient fields while consuming the order,
        # respecting transient stream order
        header = order["header"]
        self.assertEqual(header["audit"]["blob"], "x")
        logs = order["logs"]  # resumes the order stream and drains the header
        self.assertEqual(list(logs), ["l1", "l2"])
        self.assertEqual(order["note"], "ephemeral")
        data.read_all()

        snapshot = order.retained()
        self.assertNotIn("note", snapshot)
        self.assertNotIn("logs", snapshot)
        self.assertNotIn("audit", snapshot["header"])
        # only the declared values survive
        self.assertEqual(
            snapshot.to_standard(),
            {"order_id": "A1", "header": {"order_id": "A1",
                                          "summary": {"total": 10}}},
        )

    def test_retain_unretained_branch_advanced_at_any_level(self):
        # the huge audit branch lives under header, which itself is not retained;
        # logs live at the root - neither must block retrieval of the summary
        payload = (
            '[{"order_id": "A1", "header": {"audit": ["big", "bigger", "biggest"], '
            '"order_id": "A1", "summary": {"total": 10}}, '
            '"logs": [{"entry": "x"}, {"entry": "y"}]}, '
            '{"order_id": "B2", "logs": [], "header": {"summary": {"total": 20}, '
            '"order_id": "B2", "audit": {"deep": {"blob": [1, 2, 3]}}}}]'
        )
        data = load(StringIO(payload), retain=DECLARATION)
        orders = iter(data)
        first = next(orders)
        # partial log consumption, then let the outer loop drain the rest
        log_iter = iter(first["logs"])
        next(log_iter)
        second = next(orders)
        self.assertEqual(first.retained()["header"]["summary"]["total"], 10)

        second.read_all()
        self.assertEqual(second.retained()["header"]["summary"]["total"], 20)
        self.assertEqual(second.retained()["order_id"], "B2")

    def test_retain_truncated_last_order_is_not_confirmed(self):
        complete_orders = (
            '[{"order_id": "A1", "header": {"order_id": "A1", '
            '"summary": {"total": 10}, "audit": {"blob": "a"}}, "logs": ["l1"]}, '
            '{"order_id": "B2", "header": {"order_id": "B2", '
            '"summary": {"total": 20}, "audit": {"blob": "b"}}, "logs": ["l2"]}, '
            '{"order_id": "C3", "header": {"audit": {"blob": "truncated"}'
        )
        tokens = list(default_tokenizer(StringIO(complete_orders)))
        # fail right after the un-retained audit object of the last order opens
        last_audit = max(i for i, token in enumerate(tokens) if token == (1, "audit"))
        next_object = next(
            i for i in range(last_audit, len(tokens)) if tokens[i] == (0, "{")
        )
        fail_at = next_object + 1

        with self.assertRaisesRegex(ValueError, "controlled truncation"):
            load(StringIO(complete_orders), retain=DECLARATION,
                 tokenizer=controlled_tokenizer(fail_at)).read_all()

        # rerun and inspect state at the point of failure
        data = load(StringIO(complete_orders), retain=DECLARATION,
                    tokenizer=controlled_tokenizer(fail_at))
        orders = iter(data)
        first = next(orders)
        second = next(orders)
        third = next(orders)
        # the broken order exists but never reaches its end
        self.assertFalse(third.is_confirmed())
        self.assertEqual(data.retained().indices(), [0, 1])
        with self.assertRaisesRegex(ValueError, "controlled truncation"):
            data.read_all()
        # it still cannot enter the confirmed results after the failure
        self.assertFalse(third.is_confirmed())
        self.assertEqual(data.retained().indices(), [0, 1])

        self.assertTrue(first.is_confirmed())
        self.assertTrue(second.is_confirmed())
        # confirmed orders keep their selected values regardless of the failure
        self.assertEqual(first.retained()["order_id"], "A1")
        self.assertEqual(first.retained()["header"]["summary"]["total"], 10)
        self.assertEqual(second.retained()["order_id"], "B2")
        self.assertEqual(second.retained()["header"]["summary"]["total"], 20)
        self.assertNotIn("audit", first.retained()["header"])

    def test_retain_same_declaration_across_load_many_documents(self):
        document_declaration = Retain(
            ("order_id",),
            ("header", "order_id"),
            ("header", "summary", "total"),
        )
        payload = (
            '{"order_id": "A1", "header": {"order_id": "A1", '
            '"summary": {"total": 10}, "audit": {}}, "logs": ["l1"]} '
            '{"logs": [], "header": {"summary": {"total": null}, "order_id": "B2"}, '
            '"order_id": "B2"}'
        )
        documents = list(load_many(StringIO(payload), retain=document_declaration))
        self.assertEqual(2, len(documents))
        first, second = documents
        self.assertTrue(first.is_confirmed())
        self.assertTrue(second.is_confirmed())
        self.assertEqual(first.retained()["header"]["order_id"], "A1")
        self.assertEqual(second.retained()["order_id"], "B2")
        self.assertIsNone(second.retained()["header"]["summary"]["total"])
        self.assertNotIn("logs", first.retained())

    def test_retain_with_list_index_selection(self):
        retain = Retain((0, "order_id"), (1, "header", "summary", "total"))
        payload = (
            '[{"order_id": "A1", "logs": []}, '
            '{"order_id": "B2", "header": {"summary": {"total": 7}}, "logs": []}, '
            '{"order_id": "C3", "logs": []}]'
        )
        data = load(StringIO(payload), retain=retain)
        data.read_all()
        snapshot = data.retained()
        self.assertIsInstance(snapshot, RetainedList)
        self.assertEqual(snapshot.indices(), [0, 1])
        self.assertEqual(snapshot[0]["order_id"], "A1")
        self.assertEqual(snapshot[1]["header"]["summary"]["total"], 7)
        # the third element was never selected
        self.assertNotIn(2, snapshot)
        self.assertEqual(snapshot.to_standard(),
                         [{"order_id": "A1"},
                          {"header": {"summary": {"total": 7}}}])

    def test_retain_empty_path_retains_whole_subtree(self):
        retain = Retain((ANY, "header", "summary"))
        payload = (
            '[{"order_id": "A1", "header": {"summary": {"total": 10, "tax": 1}, '
            '"audit": {"blob": "x"}}, "logs": ["l1"]}]'
        )
        data = load(StringIO(payload), retain=retain)
        data.read_all()
        summary = data.retained()[0]["header"]["summary"]
        self.assertIsInstance(summary, RetainedDict)
        # the whole summary object is retained, but audit and logs are not
        self.assertEqual(summary.to_standard(), {"total": 10, "tax": 1})
        self.assertNotIn("audit", data.retained()[0]["header"])

    def test_retain_binary_source(self):
        payload = (
            '[{"order_id": "A1", "header": {"order_id": "A1", '
            '"summary": {"total": 10}}, "logs": []}]'
        ).encode()
        data = load(BytesIO(payload), retain=DECLARATION)
        data.read_all()
        snapshot = data.retained()[0]
        self.assertEqual(snapshot["order_id"], "A1")
        self.assertEqual(snapshot["header"]["summary"]["total"], 10)

    def test_retain_rejects_persistent_and_bad_types(self):
        with self.assertRaisesRegex(ValueError, "retain cannot be combined"):
            load(StringIO("{}"), persistent=True, retain=Retain(("a",)))
        with self.assertRaises(TypeError):
            load(StringIO("{}"), retain=("a",))

    def test_legacy_transient_and_persistent_modes_unchanged(self):
        payload = '{"a": 1, "b": [2, 3]}'
        transient = load(StringIO(payload))
        self.assertIsInstance(transient, TransientStreamingJSONObject)
        self.assertEqual(transient["a"], 1)

        persistent = load(StringIO(payload), persistent=True)
        self.assertEqual(persistent["b"][1], 3)
        self.assertEqual(persistent["a"], 1)

    def test_retain_primitive_root_is_yielded_directly(self):
        self.assertEqual(load(StringIO("42"), retain=Retain()), 42)
        self.assertEqual(load(StringIO('"s"'), retain=Retain(("a",))), "s")
        self.assertIsNone(load(StringIO("null"), retain=Retain()))

    def test_retain_any_applies_to_object_keys_and_list_elements(self):
        data = load(
            StringIO('{"x": {"id": 1}, "y": {"id": 2, "other": 9}}'),
            retain=Retain((ANY, "id")),
        )
        data.read_all()
        self.assertEqual(
            data.retained().to_standard(),
            {"x": {"id": 1}, "y": {"id": 2}},
        )

    def test_retain_nested_index_beyond_length_is_missing(self):
        data = load(
            StringIO('[{"logs": ["only"]}]'),
            retain=Retain((ANY, "logs", 2)),
        )
        data.read_all()
        logs = data.retained()[0]["logs"]
        self.assertIs(logs[2], MISSING)
        self.assertEqual(logs.missing(), (2,))

    def test_retain_empty_declaration_keeps_whole_document(self):
        payload = '{"a": {"b": [1, 2, 3]}, "big": "x"}'
        data = load(StringIO(payload), retain=Retain())
        data.read_all()
        self.assertEqual(
            data.retained().to_standard(),
            {"a": {"b": [1, 2, 3]}, "big": "x"},
        )

    def test_retained_snapshot_types_and_repr(self):
        payload = '[{"order_id": "A1", "header": {"summary": {"total": 1}}}]'
        data = load(StringIO(payload), retain=DECLARATION)
        data.read_all()
        snapshot = data.retained()
        self.assertIsInstance(snapshot, RetainedList)
        self.assertIsInstance(snapshot[0], RetainedDict)
        self.assertIn("RetainedDict(", repr(snapshot[0]))
        self.assertEqual(
            snapshot.to_standard(),
            [{"order_id": "A1", "header": {"summary": {"total": 1}}}],
        )
