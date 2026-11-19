import unittest

from benedict import benedict

SAMPLE_A = """<order id="A-1">
  <item sku="X">book</item>
  <note/>
</order>"""

SAMPLE_B = """<order id="B-2">
  <item sku="X">book</item>
  <item sku="Y"></item>
  <note>   </note>
</order>"""


def force_order_items(path, key, value):
    tags = tuple(tag for tag, _attrs in path)
    return key == "item" and tags == ("order",)


class xml_contract_test_case(unittest.TestCase):
    """
    Locks down the XML entry-point semantics described in docs/xml-contract.md.
    """

    def test_default_decode_is_not_cardinality_stable(self) -> None:
        a = benedict.from_xml(SAMPLE_A)
        b = benedict.from_xml(SAMPLE_B)
        # same business field, different python types
        self.assertIsInstance(a["order"]["item"], dict)
        self.assertIsInstance(b["order"]["item"], list)
        # downstream access silently differs
        self.assertEqual(a.get("order.item.@sku"), "X")
        self.assertIsNone(b.get("order.item.@sku"))

    def test_force_list_makes_cardinality_stable(self) -> None:
        a = benedict.from_xml(SAMPLE_A, force_list=force_order_items)
        b = benedict.from_xml(SAMPLE_B, force_list=force_order_items)
        self.assertEqual(
            a["order"]["item"],
            [{"@sku": "X", "#text": "book"}],
        )
        self.assertEqual(
            b["order"]["item"],
            [{"@sku": "X", "#text": "book"}, {"@sku": "Y"}],
        )
        self.assertEqual(len(a["order"]["item"]), 1)
        self.assertEqual(len(b["order"]["item"]), 2)

    def test_force_list_is_scoped_by_path(self) -> None:
        x = "<order><item>1</item><group><item>2</item></group></order>"
        d = benedict.from_xml(x, force_list=force_order_items)
        self.assertEqual(d["order"]["item"], ["1"])
        self.assertEqual(d["order"]["group"]["item"], "2")

    def test_force_list_does_not_create_missing_keys(self) -> None:
        d = benedict.from_xml("<order><note/></order>", force_list=force_order_items)
        self.assertNotIn("item", d["order"])
        self.assertIsNone(d["order"]["note"])

    def test_empty_forms_collapse_to_none(self) -> None:
        forms = [
            "<order><note/></order>",
            "<order><note></note></order>",
            "<order><note>   </note></order>",
        ]
        for x in forms:
            with self.subTest(x=x):
                self.assertEqual(benedict.from_xml(x)["order"]["note"], None)

    def test_strip_whitespace_false_keeps_blank_text(self) -> None:
        d = benedict.from_xml(SAMPLE_B, strip_whitespace=False)
        self.assertEqual(d["order"]["note"], "   ")

    def test_text_key_is_absent_when_element_has_no_text(self) -> None:
        d = benedict.from_xml(SAMPLE_B)
        self.assertIn("#text", d["order"]["item"][0])
        self.assertNotIn("#text", d["order"]["item"][1])

    def test_attributes_and_text_roundtrip(self) -> None:
        d = benedict.from_xml(SAMPLE_A, force_list=force_order_items)
        self.assertEqual(d["order"]["@id"], "A-1")
        s = d.to_xml(pretty=False)
        again = benedict.from_xml(s, force_list=force_order_items)
        self.assertEqual(again["order"]["@id"], "A-1")
        self.assertEqual(again["order"]["item"][0]["@sku"], "X")
        self.assertEqual(again["order"]["item"][0]["#text"], "book")

    def test_empty_element_write_style_is_not_recoverable(self) -> None:
        d = benedict.from_xml(SAMPLE_A)
        expanded = d.to_xml(pretty=False)
        self.assertIn("<note></note>", expanded)
        self.assertNotIn("<note/>", expanded)
        collapsed = benedict(d.dict()).to_xml(pretty=False, short_empty_elements=True)
        self.assertIn("<note/>", collapsed)

    def test_blank_text_is_not_recoverable_after_default_decode(self) -> None:
        d = benedict.from_xml(SAMPLE_B)
        self.assertNotIn("   ", d.to_xml(pretty=False))

    def test_repeated_empty_elements_keep_their_cardinality(self) -> None:
        d = benedict.from_xml("<order><item/><item></item><item a='1'/></order>")
        self.assertEqual(d["order"]["item"], [None, None, {"@a": "1"}])

    def test_attribute_and_child_with_same_name_are_distinct(self) -> None:
        d = benedict.from_xml(SAMPLE_A)
        self.assertIn("@id", d["order"])
        x = '<order id="1"><id>2</id></order>'
        d = benedict.from_xml(x)
        self.assertEqual(d["order"]["@id"], "1")
        self.assertEqual(d["order"]["id"], "2")
        self.assertEqual(
            benedict(d).to_xml(pretty=False).split("?>")[1].strip(),
            '<order id="1"><id>2</id></order>',
        )

    def test_interleaved_children_lose_document_order(self) -> None:
        d = benedict.from_xml("<order><a>1</a><b>2</b><a>3</a></order>")
        self.assertEqual(d["order"]["a"], ["1", "3"])
        self.assertEqual(d["order"]["b"], "2")
        self.assertEqual(
            benedict(d).to_xml(pretty=False).split("?>")[1].strip(),
            "<order><a>1</a><a>3</a><b>2</b></order>",
        )

    def test_element_name_with_keypath_separator_is_rejected(self) -> None:
        with self.assertRaises(ValueError):
            benedict.from_xml("<root><a.b>1</a.b></root>")
        d = benedict.from_xml("<root><a.b>1</a.b></root>", keypath_separator=None)
        self.assertEqual(d["root"]["a.b"], "1")

    def test_multiple_roots_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            benedict.from_xml("<a>1</a><b>2</b>")


if __name__ == "__main__":
    unittest.main()
