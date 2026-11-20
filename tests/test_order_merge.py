import unittest

from benedict import benedict

from order_merge import MergeResult, OrderMerger, UnmatchableVersionError


def _base_order():
    return {
        "order_no": "PO-2026-0001",
        "version": 20,
        "lines": [
            {
                "line_id": "L1",
                "sku": "A",
                "quantity": 10,
                "delivery_date": "2026-12-01",
            },
            {
                "line_id": "L2",
                "sku": "B",
                "quantity": 5,
                "delivery_date": "2026-12-05",
            },
        ],
    }


def _warehouse_batch():
    return {
        "batch_id": "WH-21",
        "source": "warehouse",
        "base_version": 20,
        "operations": [
            {"op": "remove_line", "line_id": "L1"},
            {
                "op": "add_line",
                "line": {
                    "line_id": "L3",
                    "sku": "A",
                    "quantity": 12,
                    "delivery_date": "2027-01-15",
                },
            },
        ],
    }


def _supplier_batch():
    return {
        "batch_id": "SUP-21",
        "source": "supplier",
        "base_version": 20,
        "operations": [
            {"op": "patch_line", "line_id": "L1", "sku": "A", "delivery_date": None},
            {"op": "patch_line", "line_id": "L2", "sku": "B", "delivery_date": "2026-12-20"},
        ],
    }


class order_merger_test_case(unittest.TestCase):
    """
    This class describes an order merger test case.
    """

    def test_warehouse_revision_then_late_supplier_patch_then_replay(self):
        merger = OrderMerger(_base_order())

        warehouse_result = merger.apply(_warehouse_batch())
        self.assertTrue(warehouse_result.applied)
        self.assertEqual(warehouse_result.conflicts, [])

        supplier_result = merger.apply(_supplier_batch())
        self.assertTrue(supplier_result.applied)
        self.assertEqual(len(supplier_result.conflicts), 1)

        # replay both batches as-is: no-op, nothing is duplicated
        replay_warehouse = merger.apply(_warehouse_batch())
        replay_supplier = merger.apply(_supplier_batch())
        self.assertFalse(replay_warehouse.applied)
        self.assertFalse(replay_supplier.applied)
        self.assertEqual(replay_warehouse.conflicts, [])
        self.assertEqual(replay_supplier.conflicts, [])

        lines = merger.order["lines"]
        self.assertEqual(len(lines), 2)

        # the new A line keeps its own delivery date untouched
        new_a = [line for line in lines if line["sku"] == "A"]
        self.assertEqual(len(new_a), 1)
        self.assertEqual(new_a[0]["line_id"], "L3")
        self.assertEqual(new_a[0]["delivery_date"], "2027-01-15")

        # B delivery date updated by the supplier patch
        line_b = [line for line in lines if line["sku"] == "B"][0]
        self.assertEqual(line_b["delivery_date"], "2026-12-20")

        # exactly one conflict, locating the source and the old line
        self.assertEqual(len(merger.conflicts), 1)
        conflict = merger.conflicts[0]
        self.assertEqual(conflict["source"], "supplier")
        self.assertEqual(conflict["batch_id"], "SUP-21")
        self.assertEqual(conflict["reason"], "line_not_found")
        self.assertEqual(conflict["patch"]["line_id"], "L1")
        self.assertEqual(conflict["patch"]["sku"], "A")
        self.assertIsNone(conflict["patch"]["delivery_date"])
        self.assertIsNotNone(conflict["line"])
        self.assertEqual(conflict["line"]["line_id"], "L1")
        self.assertEqual(conflict["line"]["sku"], "A")
        self.assertEqual(conflict["line"]["delivery_date"], "2026-12-01")

    def test_inbound_result_contains_only_business_fields_and_keeps_order(self):
        merger = OrderMerger(_base_order())
        merger.apply(_warehouse_batch())
        merger.apply(_supplier_batch())

        result = merger.inbound_result()
        self.assertIsInstance(result, benedict)
        self.assertEqual(result["order_no"], "PO-2026-0001")
        self.assertEqual(result["version"], 20)

        lines = result["lines"]
        # removed line is gone, remaining order preserved, new line appended
        self.assertEqual([line["sku"] for line in lines], ["B", "A"])
        for line in lines:
            self.assertNotIn("line_id", line)
        self.assertEqual(lines[0]["delivery_date"], "2026-12-20")
        self.assertEqual(lines[1]["delivery_date"], "2027-01-15")

    def test_absent_value_means_no_change_and_null_means_clear(self):
        merger = OrderMerger(_base_order())
        merger.apply(
            {
                "batch_id": "SUP-22",
                "source": "supplier",
                "operations": [
                    # no delivery_date key: B must stay unchanged
                    {"op": "patch_line", "line_id": "L2", "sku": "B"},
                    # explicit null: A delivery date must be cleared
                    {"op": "patch_line", "line_id": "L1", "sku": "A", "delivery_date": None},
                ],
            }
        )
        lines = merger.order["lines"]
        line_a = [line for line in lines if line["sku"] == "A"][0]
        line_b = [line for line in lines if line["sku"] == "B"][0]
        self.assertIsNone(line_a["delivery_date"])
        self.assertEqual(line_a["quantity"], 10)
        self.assertEqual(line_b["delivery_date"], "2026-12-05")
        self.assertEqual(merger.conflicts, [])

    def test_duplicate_sku_in_version_is_reported_unmatchable(self):
        order = _base_order()
        order["lines"].append(
            {"line_id": "L9", "sku": "A", "quantity": 1, "delivery_date": "2027-02-01"}
        )
        merger = OrderMerger(order)

        with self.assertRaises(UnmatchableVersionError) as ctx:
            merger.apply(_supplier_batch())
        self.assertEqual(ctx.exception.version, 20)
        self.assertEqual(ctx.exception.sku, "A")

        # the late-patch conflict handling must not kick in
        self.assertEqual(merger.conflicts, [])
        # the order is left untouched
        self.assertEqual(len(merger.order["lines"]), 3)

    def test_add_line_with_existing_sku_is_reported_unmatchable(self):
        merger = OrderMerger(_base_order())
        batch = {
            "batch_id": "WH-22",
            "source": "warehouse",
            "operations": [
                {
                    "op": "add_line",
                    "line": {"line_id": "L4", "sku": "A", "quantity": 3},
                },
            ],
        }
        with self.assertRaises(UnmatchableVersionError) as ctx:
            merger.apply(batch)
        self.assertEqual(ctx.exception.sku, "A")
        self.assertEqual(merger.conflicts, [])
        # batch not applied atomically: original lines unchanged
        self.assertEqual(len(merger.order["lines"]), 2)
        # the failed batch is not marked as applied and can be retried
        retry = merger.apply({**batch, "operations": []})
        self.assertTrue(retry.applied)

    def test_patch_matching_by_sku_without_line_id(self):
        merger = OrderMerger(_base_order())
        merger.apply(
            {
                "batch_id": "SUP-23",
                "source": "supplier",
                "operations": [
                    {"op": "patch_line", "sku": "B", "delivery_date": "2026-12-31"},
                ],
            }
        )
        line_b = [line for line in merger.order["lines"] if line["sku"] == "B"][0]
        self.assertEqual(line_b["delivery_date"], "2026-12-31")

    def test_merge_result_repr(self):
        result = MergeResult(applied=False)
        self.assertIn("applied=False", repr(result))


if __name__ == "__main__":
    unittest.main()
