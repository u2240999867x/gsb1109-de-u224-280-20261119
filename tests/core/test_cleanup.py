from __future__ import annotations

import re
import unittest

from benedict import benedict
from benedict.core import cleanup as _cleanup


def _build_inventory() -> benedict:
    return benedict(
        {
            "stations": [
                {
                    "name": "south",
                    "probes": [
                        {
                            "id": "P1",
                            "note": "south-p1-note",
                            "debugs": [{"id": "D1", "note": "south-d1-note"}],
                        },
                        {"id": "P2", "note": "south-p2-note"},
                    ],
                },
                {
                    "name": "north",
                    "probes": [
                        {
                            "id": "P1",
                            "note": "north-p1-note",
                            "debugs": [{"id": "D1", "note": "north-d1-note"}],
                        },
                        {
                            "id": "P2",
                            "note": "north-p2-note",
                            "debugs": [{"id": "D2", "note": "north-p2-d2-note"}],
                        },
                    ],
                },
            ]
        }
    )


# both north selections used by the two end-to-end scenarios
_SELECTION = re.compile(
    r"stations\[1\]\.probes\[0\]\.note$"
    r"|stations\[1\]\.probes\[1\]\.debugs\[0\]$"
)


class cleanup_preview_test_case(unittest.TestCase):
    def test_preview_matches_values_without_mutating_data(self) -> None:
        inventory = _build_inventory()
        preview = inventory.cleanup_preview(_SELECTION, scope=2)
        self.assertEqual(len(preview), 2)
        self.assertEqual(
            [target.keypath for target in preview],
            [
                "stations[1].probes[0].note",
                "stations[1].probes[1].debugs[0]",
            ],
        )
        self.assertEqual(preview[0].value, "north-p1-note")
        self.assertEqual(preview[1].value, {"id": "D2", "note": "north-p2-d2-note"})
        # nothing was deleted by previewing
        self.assertEqual(inventory["stations[1].probes[0].note"], "north-p1-note")
        self.assertEqual(len(inventory["stations[1].probes[1]"]["debugs"]), 1)

    def test_preview_targets_carry_their_owning_scope(self) -> None:
        inventory = _build_inventory()
        preview = inventory.cleanup_preview(_SELECTION, scope=2)
        for target in preview:
            self.assertEqual(target.scope_keypath, "stations[1]")
            self.assertEqual(target.scope_keys, ("stations", 1))

    def test_preview_distinguishes_same_probe_id_across_stations(self) -> None:
        inventory = _build_inventory()
        preview = inventory.cleanup_preview("stations[1].probes[0].note", scope=2)
        self.assertEqual(len(preview), 1)
        self.assertEqual(preview[0].value, "north-p1-note")
        receipts = inventory.cleanup_apply(preview)
        self.assertTrue(receipts[0].applied)
        # the south station P1 note, same probe id, is untouched
        self.assertEqual(inventory["stations"][0]["probes"][0]["note"], "south-p1-note")

    def test_preview_supports_wildcard_pattern(self) -> None:
        inventory = benedict({"stations": [{"name": "south"}, {"name": "north"}]})
        preview = inventory.cleanup_preview("stations[*].name", scope=1)
        self.assertEqual(
            [target.keypath for target in preview],
            ["stations[0].name", "stations[1].name"],
        )
        self.assertEqual([target.value for target in preview], ["south", "north"])

    def test_preview_rejects_invalid_arguments(self) -> None:
        inventory = _build_inventory()
        with self.assertRaises(ValueError):
            inventory.cleanup_preview("note", scope=0)
        with self.assertRaises(ValueError):
            _cleanup.cleanup_preview(inventory, "note", separator="")
        with self.assertRaises(ValueError):
            inventory.cleanup_preview(100)  # type: ignore[arg-type]


class cleanup_apply_test_case(unittest.TestCase):
    def test_apply_returns_receipts_matched_back_to_preview_targets(self) -> None:
        inventory = _build_inventory()
        preview = inventory.cleanup_preview(_SELECTION, scope=2)
        receipts = inventory.cleanup_apply(preview)
        self.assertEqual(len(receipts), len(preview))
        for receipt, target in zip(receipts, preview.targets):
            self.assertIs(receipt.target, target)
            self.assertEqual(receipt.keypath, target.keypath)
            self.assertEqual(receipt.scope_keypath, target.scope_keypath)
            self.assertTrue(receipt.applied)
            self.assertEqual(receipt.reason, "applied")
        self.assertNotIn("note", inventory["stations"][1]["probes"][0])
        self.assertEqual(inventory["stations"][1]["probes"][1]["debugs"], [])

    def test_apply_survives_reordering_and_insertion_within_scopes(self) -> None:
        inventory = _build_inventory()
        preview = inventory.cleanup_preview(_SELECTION, scope=2)
        # south station reorders its probes
        inventory["stations[0].probes"].reverse()
        # north station gets a brand new probe inserted before the others
        inventory["stations[1].probes"].insert(
            0, {"id": "P9", "note": "new-probe-note"}
        )
        receipts = inventory.cleanup_apply(preview)
        self.assertTrue(all(receipt.applied for receipt in receipts))
        north_probes = inventory["stations"][1]["probes"]
        # the inserted probe is untouched and still first
        self.assertEqual(north_probes[0], {"id": "P9", "note": "new-probe-note"})
        # original P1 shifted to index 1 and only its note was removed
        self.assertEqual(north_probes[1]["id"], "P1")
        self.assertNotIn("note", north_probes[1])
        self.assertEqual(
            north_probes[1]["debugs"],
            [{"id": "D1", "note": "north-d1-note"}],
        )
        # the adjacent probe lost its whole debug object
        self.assertEqual(north_probes[2]["id"], "P2")
        self.assertEqual(north_probes[2]["debugs"], [])
        # south station was only reordered, nothing deleted there
        south_probes = inventory["stations"][0]["probes"]
        self.assertEqual([probe["id"] for probe in south_probes], ["P2", "P1"])
        self.assertEqual(south_probes[1]["note"], "south-p1-note")

    def test_apply_rejects_everything_when_probe_moves_to_another_station(
        self,
    ) -> None:
        inventory = _build_inventory()
        preview = inventory.cleanup_preview(_SELECTION, scope=2)
        north_probes = inventory["stations"][1]["probes"]
        moved_probe = north_probes.pop(0)
        inventory["stations"][0]["probes"].append(moved_probe)
        # a same-id replacement probe takes the original north P1 slot
        north_probes.insert(0, {"id": "P1", "note": "replacement-p1-note"})
        before = inventory.dict()
        with self.assertRaises(_cleanup.StaleTargetError) as context:
            inventory.cleanup_apply(preview)
        # whole batch rejected: no deletion at all
        self.assertEqual(inventory.dict(), before)
        error = context.exception
        stale_keypaths = [target.keypath for target, _ in error.stale]
        self.assertIn("stations[1].probes[0].note", stale_keypaths)
        message = str(error)
        # the receipt/error names the preview object and its original station
        self.assertIn("stations[1].probes[0].note", message)
        self.assertIn("stations[1]", message)
        # and explains the object now belongs to the south station
        self.assertIn("stations[0]", message)
        # the otherwise-valid second selection was not deleted either
        self.assertEqual(
            inventory["stations"][1]["probes"][1]["debugs"],
            [{"id": "D2", "note": "north-p2-d2-note"}],
        )
        # the replacement probe is untouched
        self.assertEqual(
            inventory["stations"][1]["probes"][0]["note"],
            "replacement-p1-note",
        )

    def test_apply_deletes_nested_object_once_with_two_receipts(self) -> None:
        inventory = _build_inventory()
        preview = inventory.cleanup_preview(
            re.compile(r"stations\[1\]\.probes\[0\]\.debugs\[0\](\.note)?$"),
            scope=2,
        )
        self.assertEqual(
            [target.keypath for target in preview],
            [
                "stations[1].probes[0].debugs[0]",
                "stations[1].probes[0].debugs[0].note",
            ],
        )
        receipts = inventory.cleanup_apply(preview)
        self.assertEqual(
            [receipt.keypath for receipt in receipts],
            [target.keypath for target in preview],
        )
        # the ancestor object is actually deleted once ...
        self.assertTrue(receipts[0].applied)
        self.assertEqual(receipts[0].reason, "applied")
        # ... and the nested note is accounted for without a second deletion
        self.assertFalse(receipts[1].applied)
        self.assertEqual(receipts[1].reason, "already-removed")
        self.assertIs(receipts[1].target, preview.targets[1])
        self.assertEqual(inventory["stations"][1]["probes"][0]["debugs"], [])

    def test_apply_on_plain_dict_through_core_functions(self) -> None:
        data = _build_inventory().dict()
        preview = _cleanup.cleanup_preview(data, "stations[1].probes[0].note", scope=2)
        receipts = _cleanup.cleanup_apply(data, preview)
        self.assertTrue(receipts[0].applied)
        self.assertNotIn("note", data["stations"][1]["probes"][0])

    def test_apply_respects_frozen_state(self) -> None:
        inventory = _build_inventory()
        preview = inventory.cleanup_preview("stations[1].probes[0].note", scope=2)
        inventory.freeze()
        with self.assertRaises(TypeError):
            inventory.cleanup_apply(preview)
        self.assertEqual(inventory["stations"][1]["probes"][0]["note"], "north-p1-note")

    def test_apply_tracks_immutable_leaves_after_container_reordering(
        self,
    ) -> None:
        data = {
            "stations": [
                {"name": "south", "probes": [{"id": "P1"}]},
                {
                    "name": "north",
                    "probes": [
                        {"id": "P1", "note": "north-p1-note"},
                        {"id": "P2"},
                    ],
                },
            ]
        }
        preview = _cleanup.cleanup_preview(data, "stations[1].probes[0].note", scope=2)
        data["stations"][1]["probes"].reverse()
        receipts = _cleanup.cleanup_apply(data, preview)
        self.assertTrue(receipts[0].applied)
        probes = data["stations"][1]["probes"]
        self.assertEqual(probes[0]["id"], "P2")
        self.assertEqual(probes[1]["id"], "P1")
        self.assertNotIn("note", probes[1])


class cleanup_preview_model_test_case(unittest.TestCase):
    def test_preview_is_iterable_indexable_and_sized(self) -> None:
        inventory = _build_inventory()
        preview = inventory.cleanup_preview(
            re.compile(r"stations\[\d+\]\.probes\[\d+\]\.id$"), scope=2
        )
        self.assertEqual(len(preview), 4)
        self.assertIsInstance(preview[0], _cleanup.CleanupTarget)
        self.assertEqual(
            [target.keypath for target in list(preview)],
            [target.keypath for target in preview.targets],
        )

    def test_receipt_exposes_preview_identity_fields(self) -> None:
        inventory = _build_inventory()
        preview = inventory.cleanup_preview("stations[0].probes[1].note", scope=2)
        receipt = inventory.cleanup_apply(preview)[0]
        self.assertEqual(receipt.keypath, "stations[0].probes[1].note")
        self.assertEqual(receipt.scope_keypath, "stations[0]")
        self.assertEqual(receipt.value, "south-p2-note")


class cleanup_identity_test_case(unittest.TestCase):
    def _build_dict_scoped_inventory(self) -> benedict:
        # scopes are nested dicts (sites -> station) rather than a list;
        # benedict recreates the wrapper objects on every such access, so the
        # implementation must track backing-object identity, not wrapper id
        return benedict(
            {
                "sites": {
                    "south": {"probes": [{"id": "P1", "note": "south-p1-note"}]},
                    "north": {
                        "probes": [
                            {"id": "P1", "note": "north-p1-note"},
                            {"id": "P2"},
                        ]
                    },
                }
            }
        )

    def test_apply_with_dict_nested_scopes_and_recreated_wrappers(self) -> None:
        inventory = self._build_dict_scoped_inventory()
        preview = inventory.cleanup_preview("sites.north.probes[0].note", scope=2)
        # reorder the probes so the note's list index shifts
        inventory["sites"]["north"]["probes"].reverse()
        receipts = inventory.cleanup_apply(preview)
        self.assertTrue(receipts[0].applied)
        probes = inventory["sites"]["north"]["probes"]
        self.assertEqual(probes[0]["id"], "P2")
        self.assertEqual(probes[1]["id"], "P1")
        self.assertNotIn("note", probes[1])

    def test_apply_rejects_dict_nested_probe_moved_to_another_scope(self) -> None:
        inventory = self._build_dict_scoped_inventory()
        preview = inventory.cleanup_preview("sites.north.probes[0].note", scope=2)
        moved = inventory["sites"]["north"]["probes"].pop(0)
        inventory["sites"]["south"]["probes"].append(moved)
        before = inventory.dict()
        with self.assertRaises(_cleanup.StaleTargetError) as context:
            inventory.cleanup_apply(preview)
        self.assertEqual(inventory.dict(), before)
        message = str(context.exception)
        self.assertIn("sites.north", message)
        self.assertIn("sites.south", message)


if __name__ == "__main__":
    unittest.main()
