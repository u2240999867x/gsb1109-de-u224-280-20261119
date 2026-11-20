import unittest

from benedict.core import filter as _filter
from benedict.core import unflatten as _unflatten


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

    def test_filter_deep_nested_dict_removed_by_predicate(self) -> None:
        i = {
            "a": 1,
            "private": {
                "secret": "x",
            },
        }
        # predicate rejects key "private" at top level → nested dict excluded entirely
        o = _filter(i, lambda key, val: key != "private", deep=True)
        r = {
            "a": 1,
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

    def _devices(self):
        return {
            "devices": [
                {
                    "id": "device-a",
                    "probes": [
                        {
                            "id": "probe-1",
                            "readings": [
                                {"measurement": 1.0},
                                {"measurement": None},
                                {"measurement": 2.0, "calibration": {"gain": 2}},
                            ],
                        }
                    ],
                },
                {
                    "id": "device-b",
                    "probes": [
                        {
                            "id": "probe-2",
                            "readings": [
                                {"measurement": 3.0, "calibration": {"gain": 2}},
                            ],
                        }
                    ],
                },
            ]
        }

    def test_filter_keypath_returns_selected_leaves_at_original_positions(self) -> None:
        i = self._devices()
        o = _filter(
            i,
            lambda keypath, val: keypath.endswith(".measurement") and val is not None,
            keypath=True,
        )
        self.assertEqual(
            o,
            {
                "devices[0].probes[0].readings[0].measurement": 1.0,
                "devices[0].probes[0].readings[2].measurement": 2.0,
                "devices[1].probes[0].readings[0].measurement": 3.0,
            },
        )
        self.assertFalse(i is o)
        self.assertNotIn("devices[0].probes[0].readings[1].measurement", o)

    def test_filter_keypath_selected_null_is_distinct_from_not_selected(self) -> None:
        i = self._devices()
        predicate = lambda keypath, val: keypath == (  # noqa: E731
            "devices[0].probes[0].readings[1].measurement"
        )
        o = _filter(i, predicate, keypath=True)
        self.assertEqual(
            o, {"devices[0].probes[0].readings[1].measurement": None}
        )
        self.assertIn("devices[0].probes[0].readings[1].measurement", o)
        # A None-valued predicate that rejects the leaf omits it entirely.
        self.assertEqual(_filter(i, lambda keypath, val: False, keypath=True), {})

    def test_filter_keypath_missing_field_stays_missing(self) -> None:
        i = self._devices()
        o = _filter(
            i,
            lambda keypath, val: keypath == (
                "devices[0].probes[0].readings[0].calibration"
            ),
            keypath=True,
        )
        self.assertEqual(o, {})

    def test_filter_keypath_merge_measurements_and_calibration(self) -> None:
        i = self._devices()
        measurements = _filter(
            i,
            lambda keypath, val: keypath.endswith(".measurement") and val is not None,
            keypath=True,
        )
        calibration = _filter(
            i,
            lambda keypath, val: keypath.endswith(".calibration")
            and keypath.startswith("devices[0]."),
            keypath=True,
        )
        measurements.update(calibration)
        self.assertEqual(
            measurements,
            {
                "devices[0].probes[0].readings[0].measurement": 1.0,
                "devices[0].probes[0].readings[2].measurement": 2.0,
                "devices[1].probes[0].readings[0].measurement": 3.0,
                "devices[0].probes[0].readings[2].calibration": {"gain": 2},
            },
        )

    def test_filter_keypath_merged_paths_unflatten_to_original_records(self) -> None:
        i = self._devices()
        o = _filter(
            i,
            lambda keypath, val: (
                (keypath.endswith(".measurement") and val is not None)
                or (
                    keypath.endswith(".calibration")
                    and keypath.startswith("devices[0].")
                )
            ),
            keypath=True,
        )
        u = _unflatten(o, separator=".")
        self.assertEqual(
            u,
            {
                "devices": [
                    {
                        "probes": [
                            {
                                "readings": [
                                    {"measurement": 1.0},
                                    None,
                                    {
                                        "measurement": 2.0,
                                        "calibration": {"gain": 2},
                                    },
                                ]
                            }
                        ],
                    },
                    {
                        "probes": [
                            {
                                "readings": [
                                    {"measurement": 3.0},
                                ]
                            }
                        ],
                    },
                ]
            },
        )

    def test_filter_keypath_copies_selected_values(self) -> None:
        i = self._devices()
        calibration = i["devices"][0]["probes"][0]["readings"][2]["calibration"]
        o = _filter(
            i,
            lambda keypath, val: keypath.endswith(".calibration"),
            keypath=True,
        )
        selected = o["devices[0].probes[0].readings[2].calibration"]
        self.assertEqual(selected, calibration)
        self.assertFalse(selected is calibration)
        selected["unit"] = "mV"
        self.assertEqual(calibration, {"gain": 2})
        self.assertNotIn(
            "units", o["devices[0].probes[0].readings[2].calibration"]
        )
        self.assertNotEqual(
            o["devices[0].probes[0].readings[2].calibration"],
            calibration,
        )

    def test_filter_keypath_selected_node_is_not_split_into_children(self) -> None:
        i = self._devices()
        o = _filter(
            i,
            lambda keypath, val: keypath == (
                "devices[0].probes[0].readings[2].calibration"
            ),
            keypath=True,
        )
        self.assertEqual(
            o, {"devices[0].probes[0].readings[2].calibration": {"gain": 2}}
        )
        self.assertNotIn(
            "devices[0].probes[0].readings[2].calibration.gain", o
        )

    def test_filter_keypath_keeps_device_probe_and_reading_path(self) -> None:
        i = self._devices()
        o = _filter(
            i,
            lambda keypath, val: keypath.endswith(".calibration"),
            keypath=True,
        )
        self.assertEqual(
            sorted(o),
            [
                "devices[0].probes[0].readings[2].calibration",
                "devices[1].probes[0].readings[0].calibration",
            ],
        )
        for keypath, value in o.items():
            parts = keypath.split(".")
            self.assertTrue(parts[0].startswith("devices["))
            self.assertEqual(parts[1], "probes[0]")
            self.assertTrue(parts[2].startswith("readings["))
            self.assertEqual(parts[3], "calibration")
            self.assertEqual(value, {"gain": 2})

    def test_filter_keypath_with_plain_dict(self) -> None:
        i = {"a": [{"b": 1}, {"b": None}]}
        o = _filter(i, lambda keypath, val: keypath.endswith(".b"), keypath=True)
        self.assertIsInstance(o, dict)
        self.assertEqual(o, {"a[0].b": 1, "a[1].b": None})
        self.assertEqual(i, {"a": [{"b": 1}, {"b": None}]})

    def test_filter_keypath_uses_custom_separator(self) -> None:
        from benedict import benedict

        d = benedict(
            {"devices": [{"readings": [{"measurement": 1.0}]}]},
            keypath_separator="/",
        )
        o = d.filter(
            lambda keypath, val: keypath.endswith("/measurement"), keypath=True
        )
        self.assertEqual(o, {"devices[0]/readings[0]/measurement": 1.0})

    def test_filter_without_keypath_keeps_previous_behavior(self) -> None:
        i = self._devices()
        o = _filter(i, lambda key, val: key == "devices")
        self.assertEqual(o, {"devices": i["devices"]})
        self.assertTrue(o["devices"] is i["devices"])
        deep = _filter(i, lambda key, val: key != "calibration", deep=True)
        # deep=True still only recurses into nested dicts, as before.
        self.assertNotIn("calibration", deep["devices"][0])
