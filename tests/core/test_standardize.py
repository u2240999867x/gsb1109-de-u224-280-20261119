import unittest

from benedict.core import standardize as _standardize
from benedict.core.standardize import (
    StandardizationConflictError,
    build_standardization_plan,
)


class standardize_test_case(unittest.TestCase):
    """
    This class describes a standardize test case.
    """

    def test_standardize(self) -> None:
        d = {
            "CamelCase": 1,
            "CamelCamelCase": 1,
            "Camel2Camel2Case": 1,
            "getHTTPResponseCode": 1,
            "get2HTTPResponseCode": 1,
            "HTTPResponseCode": 1,
            "HTTPResponseCodeXYZ": 1,
            " LocationCoordinates ": {
                "Lat. ": 0.0,
                "Lng. ": 0.0,
            },
            " LocationHistoryCoordinates ": [
                {
                    "Lat. ": 0.0,
                    "Lng. ": 0.0,
                },
                {
                    "Lat. ": 0.0,
                    "Lng. ": 0.0,
                },
            ],
        }
        _standardize(d)
        r = {
            "camel_case": 1,
            "camel_camel_case": 1,
            "camel2_camel2_case": 1,
            "get_http_response_code": 1,
            "get2_http_response_code": 1,
            "http_response_code": 1,
            "http_response_code_xyz": 1,
            "location_coordinates": {
                "lat": 0.0,
                "lng": 0.0,
            },
            "location_history_coordinates": [
                {
                    "lat": 0.0,
                    "lng": 0.0,
                },
                {
                    "lat": 0.0,
                    "lng": 0.0,
                },
            ],
        }
        # print(_dump(d))
        # print(_dump(r))
        self.assertEqual(d, r)

    def test_standardize_strict_conflict_at_root(self) -> None:
        d = {"Item-ID": 1, "item_id": 2}
        with self.assertRaises(StandardizationConflictError) as ctx:
            _standardize(d, strict=True)
        # The original input is left untouched when strict mode fails.
        self.assertEqual(d, {"Item-ID": 1, "item_id": 2})
        plan = ctx.exception.plan
        self.assertTrue(plan.has_conflicts)
        self.assertEqual(len(plan.conflicts), 1)
        conflict = plan.conflicts[0]
        self.assertEqual(conflict.path, "")
        self.assertEqual(conflict.standardized_key, "item_id")
        self.assertEqual(conflict.original_keys, ("Item-ID", "item_id"))
        self.assertEqual(conflict.original_paths, ("Item-ID", "item_id"))

    def test_standardize_strict_conflict_keeps_original_paths_with_parent_rename(
        self,
    ) -> None:
        d = {
            "Dealer-Info": {
                "Item-ID": 1,
                "item_id": 2,
            }
        }
        with self.assertRaises(StandardizationConflictError) as ctx:
            _standardize(d, strict=True)
        self.assertEqual(
            d,
            {
                "Dealer-Info": {
                    "Item-ID": 1,
                    "item_id": 2,
                }
            },
        )
        plan = ctx.exception.plan
        conflict = plan.conflicts[0]
        self.assertEqual(conflict.path, "Dealer-Info")
        self.assertEqual(conflict.standardized_key, "item_id")
        self.assertEqual(
            conflict.original_paths,
            ("Dealer-Info.Item-ID", "Dealer-Info.item_id"),
        )
        # The parent rename itself is still reported as a planned change.
        self.assertIn(
            ("Dealer-Info", "dealer_info"),
            [(change.original_key, change.standardized_key) for change in plan.changes],
        )

    def test_standardize_strict_conflict_in_list_item(self) -> None:
        d = {
            "dealers": [
                {"Item-ID": 1, "item_id": 2},
            ]
        }
        with self.assertRaises(StandardizationConflictError) as ctx:
            _standardize(d, strict=True)
        conflict = ctx.exception.plan.conflicts[0]
        self.assertEqual(conflict.path, "dealers[0]")
        self.assertEqual(
            conflict.original_paths,
            ("dealers[0].Item-ID", "dealers[0].item_id"),
        )
        self.assertEqual(d, {"dealers": [{"Item-ID": 1, "item_id": 2}]})

    def test_standardize_non_strict_conflict_raises_key_error(self) -> None:
        d = {"Item-ID": 1, "item_id": 2}
        with self.assertRaises(KeyError):
            _standardize(d)

    def test_standardize_strict_without_conflict_applies_changes(self) -> None:
        d = {"Item-ID": 1, "Product Name": {"Sub-Field": 2}}
        _standardize(d, strict=True)
        self.assertEqual(
            d,
            {"item_id": 1, "product_name": {"sub_field": 2}},
        )

    def test_build_standardization_plan_does_not_mutate_input(self) -> None:
        d = {
            "Dealer-Info": {
                "Item-ID": [{"Sub-Field": 1}],
                "Safe-Field": ({"Tuple-Key": 2},),
            },
            3: "non-string key",
        }
        plan = build_standardization_plan(d)
        self.assertEqual(
            d,
            {
                "Dealer-Info": {
                    "Item-ID": [{"Sub-Field": 1}],
                    "Safe-Field": ({"Tuple-Key": 2},),
                },
                3: "non-string key",
            },
        )
        self.assertFalse(plan.has_conflicts)
        self.assertEqual(
            plan.preview,
            {
                "dealer_info": {
                    "item_id": [{"sub_field": 1}],
                    "safe_field": ({"tuple_key": 2},),
                },
                3: "non-string key",
            },
        )

    def test_build_standardization_plan_preview_with_conflict(self) -> None:
        d = {
            "Dealer-Info": {
                "Item-ID": 1,
                "item_id": 2,
                "Untouched-Section": {"Ok-Key": 3},
            }
        }
        plan = build_standardization_plan(d)
        self.assertTrue(plan.has_conflicts)
        # Non-conflicting sub-structures are still available in the preview.
        self.assertEqual(
            plan.preview["dealer_info"]["untouched_section"],
            {"ok_key": 3},
        )
        self.assertEqual(
            plan.conflicts[0].original_paths,
            ("Dealer-Info.Item-ID", "Dealer-Info.item_id"),
        )

    def test_build_standardization_plan_reports_all_changes_with_paths(self) -> None:
        d = {"Root-Key": [{"Child-Key": 1}]}
        plan = build_standardization_plan(d)
        changes = {
            change.original_path: (
                change.original_key,
                change.standardized_key,
            )
            for change in plan.changes
        }
        self.assertEqual(
            changes["Root-Key"],
            ("Root-Key", "root_key"),
        )
        self.assertEqual(
            changes["Root-Key[0].Child-Key"],
            ("Child-Key", "child_key"),
        )

    def test_build_standardization_plan_empty(self) -> None:
        plan = build_standardization_plan({})
        self.assertEqual(plan.changes, [])
        self.assertEqual(plan.conflicts, [])
        self.assertEqual(plan.preview, {})
        self.assertFalse(plan.has_conflicts)
