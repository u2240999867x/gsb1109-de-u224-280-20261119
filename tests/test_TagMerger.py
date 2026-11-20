# coding: utf-8

from __future__ import absolute_import

import asyncio
import unittest

import httpretty

import influxdb_client
from influxdb_client import InfluxDBClient, OMIT_TAG, Point, TagMergeOptions, TagOrigin
from influxdb_client.client.write.tag_merger import TagMerger
from influxdb_client.client.write_api import SYNCHRONOUS, PointSettings


def _point_records():
    return [
        Point("sensor").tag("host", "h1").tag("region", "us-west-2").field("level", 1).time(1),
        Point("sensor").tag("host", "h1").field("level", 2).time(2),
        Point("sensor").tag("host", "h1").tag("region", OMIT_TAG).field("level", 3).time(3),
    ]


def _nested_dict_records():
    return [
        {"measurement": "sensor", "tags": {"host": "h1", "region": "us-west-2"},
         "fields": {"level": 1}, "time": 1},
        {"measurement": "sensor", "tags": {"host": "h1"},
         "fields": {"level": 2}, "time": 2},
        {"measurement": "sensor", "tags": {"host": "h1", "region": OMIT_TAG},
         "fields": {"level": 3}, "time": 3},
    ]


def _flat_dict_records():
    return [
        {"name": "sensor", "host": "h1", "region": "us-west-2", "level": 1, "created": 1},
        {"name": "sensor", "host": "h1", "level": 2, "created": 2},
        {"name": "sensor", "host": "h1", "region": OMIT_TAG, "level": 3, "created": 3},
    ]


_FLAT_KWARGS = dict(record_measurement_key="name", record_time_key="created",
                    record_tag_keys=["host", "region"], record_field_keys=["level"])

DEFAULT_REGION = "us-east-1"
OTHER_REGION = "eu-west-1"
EXPLICIT_REGION = "us-west-2"


class TagMergerSemanticsTest(unittest.TestCase):

    def _merger(self):
        return TagMerger(default_tags={"region": DEFAULT_REGION})

    def test_three_input_forms_produce_identical_tags_and_origins(self):
        expected_lines = [
            "sensor,host=h1,region=us-west-2 level=1i 1",
            f"sensor,host=h1,region={DEFAULT_REGION} level=2i 2",
            "sensor,host=h1 level=3i 3",
        ]
        expected_origins = [
            {"host": TagOrigin.RECORD, "region": TagOrigin.RECORD},
            {"host": TagOrigin.RECORD, "region": TagOrigin.DEFAULT},
            {"host": TagOrigin.RECORD, "region": TagOrigin.OMITTED},
        ]

        point_batch = self._merger().merge_batch(_point_records())
        nested_batch = self._merger().merge_batch(_nested_dict_records())
        flat_batch = self._merger().merge_batch(_flat_dict_records(), **_FLAT_KWARGS)

        for batch in (point_batch, nested_batch, flat_batch):
            self.assertEqual(expected_lines, [record.line_protocol for record in batch.records])
            for record, origins in zip(batch.records, expected_origins):
                self.assertEqual(origins, record.tag_origins())
            self.assertEqual([], batch.discarded_records())

        for point_record, nested_record, flat_record in zip(
                point_batch.records, nested_batch.records, flat_batch.records):
            self.assertEqual(point_record.record_tags, nested_record.record_tags)
            self.assertEqual(nested_record.record_tags, flat_record.record_tags)
            self.assertEqual(point_record.default_tags, nested_record.default_tags)
            self.assertEqual(nested_record.default_tags, flat_record.default_tags)
            self.assertEqual(point_record.omitted_tags, flat_record.omitted_tags)

    def test_explicit_value_wins_and_only_missing_gets_default(self):
        batch = self._merger().merge_batch(_point_records())
        explicit, missing, omitted = batch.records

        self.assertEqual({"host": "h1", "region": EXPLICIT_REGION}, explicit.record_tags)
        self.assertEqual({}, explicit.default_tags)
        self.assertEqual([], explicit.omitted_tags)

        self.assertEqual({"host": "h1"}, missing.record_tags)
        self.assertEqual({"region": DEFAULT_REGION}, missing.default_tags)

        self.assertEqual({"host": "h1"}, omitted.record_tags)
        self.assertEqual({}, omitted.default_tags)
        self.assertEqual(["region"], omitted.omitted_tags)

    def test_configured_omit_keys_are_not_repaired_by_defaults(self):
        merger = TagMerger(default_tags={"region": DEFAULT_REGION, "env": "prod", "debug": "on"},
                           omit_tag_keys=["region", "debug"])
        records = [
            Point("sensor").tag("region", EXPLICIT_REGION).field("level", 1).time(1),
            Point("sensor").field("level", 2).time(2),
        ]
        batch = merger.merge_batch(records)
        first, second = batch.records

        self.assertEqual(["debug", "region"], first.omitted_tags)
        self.assertNotIn("region", first.line_protocol)
        self.assertNotIn("debug", first.line_protocol)
        self.assertEqual({"env": "prod"}, first.default_tags)

        # 'debug' is absent from the record yet configured as an omission: it
        # must still be reported and must not receive its default value.
        self.assertEqual(["debug", "region"], second.omitted_tags)
        self.assertEqual({"env": "prod"}, second.default_tags)
        self.assertNotIn("debug", second.line_protocol)
        self.assertNotIn("region", second.line_protocol)
        self.assertEqual("sensor,env=prod level=2i 2", second.line_protocol)

    def test_inputs_are_not_mutated_and_can_target_another_default(self):
        points = _point_records()
        nested = _nested_dict_records()

        first_batch = self._merger().merge_batch(points)
        other_merger = TagMerger(default_tags={"region": OTHER_REGION})
        second_batch = other_merger.merge_batch(points)

        # Original Point objects keep their original tags.
        self.assertEqual(EXPLICIT_REGION, points[0]._tags["region"])
        self.assertNotIn("region", points[1]._tags)
        self.assertIs(OMIT_TAG, points[2]._tags["region"])

        self.assertIn(DEFAULT_REGION, first_batch.records[1].line_protocol)
        self.assertIn(OTHER_REGION, second_batch.records[1].line_protocol)
        self.assertNotIn(OTHER_REGION, first_batch.records[1].line_protocol)
        self.assertNotIn(DEFAULT_REGION, second_batch.records[1].line_protocol)
        # Explicit and omitted records behave the same regardless of default.
        for batch in (first_batch, second_batch):
            self.assertIn(EXPLICIT_REGION, batch.records[0].line_protocol)
            self.assertNotIn("region", batch.records[2].line_protocol)

        first_nested = self._merger().merge_batch(nested)
        second_nested = other_merger.merge_batch(nested)
        self.assertNotIn("region", nested[1].get("tags"))
        self.assertIs(OMIT_TAG, nested[2]["tags"]["region"])
        self.assertIn(DEFAULT_REGION, first_nested.records[1].line_protocol)
        self.assertIn(OTHER_REGION, second_nested.records[1].line_protocol)

    def test_preview_keeps_source_reference_for_every_record(self):
        points = _point_records()
        batch = self._merger().merge_batch(points)
        for original, merged in zip(points, batch.records):
            self.assertIs(original, merged.source)
            self.assertIsNot(original, merged.point)

    def test_records_without_fields_are_not_previewed_as_writable(self):
        records = [
            Point("sensor").tag("host", "h1"),
            Point("sensor").field("level", 1).time(1),
        ]
        batch = self._merger().merge_batch(records)
        discarded, writable = batch.records

        self.assertTrue(discarded.discarded)
        self.assertEqual("", discarded.line_protocol)
        self.assertFalse(writable.discarded)
        self.assertEqual(1, len(batch.writable_records()))
        self.assertEqual([discarded], batch.discarded_records())
        self.assertEqual(writable.line_protocol, batch.line_protocol)

    def test_unstructured_records_are_rejected(self):
        merger = self._merger()
        for record in ("h2o level=1", b"h2o level=1", 42):
            with self.assertRaises(TypeError):
                merger.merge(record)

    def test_options_from_point_settings(self):
        settings = PointSettings(region=DEFAULT_REGION)
        options = TagMergeOptions.from_point_settings(settings, omit_tag_keys=["debug"])
        self.assertEqual({"region": DEFAULT_REGION}, options.default_tags)
        self.assertEqual(["debug"], options.omit_tag_keys)


class WriteApiMergedTest(unittest.TestCase):

    def setUp(self) -> None:
        httpretty.enable()
        httpretty.reset()
        httpretty.register_uri(httpretty.POST, uri="http://localhost/api/v2/write", status=204)

        conf = influxdb_client.configuration.Configuration()
        conf.host = "http://localhost"
        conf.debug = False
        self.influxdb_client = InfluxDBClient(url=conf.host, token="my-token", org="my-org")
        self.write_api = self.influxdb_client.write_api(write_options=SYNCHRONOUS)

    def tearDown(self) -> None:
        self.influxdb_client.close()
        httpretty.disable()

    def _requests(self):
        return httpretty.httpretty.latest_requests

    def test_preview_then_write_matches_line_by_line(self):
        options = TagMergeOptions(default_tags={"region": DEFAULT_REGION})
        records = _point_records()

        preview = self.write_api.preview_tags(records, tag_merge_options=options)
        result = self.write_api.write_merged("bucket-a", "my-org", records,
                                             tag_merge_options=options)
        self.assertIsNone(result)

        body = self._requests()[0].parsed_body
        self.assertEqual(preview.line_protocol, body)
        expected_lines = [
            "sensor,host=h1,region=us-west-2 level=1i 1",
            f"sensor,host=h1,region={DEFAULT_REGION} level=2i 2",
            "sensor,host=h1 level=3i 3",
        ]
        self.assertEqual(expected_lines, body.split("\n"))
        # Provenance metadata must never reach InfluxDB.
        self.assertNotIn("origin", body)
        self.assertNotIn("omitted", body)

    def test_three_forms_write_identical_bodies(self):
        options = TagMergeOptions(default_tags={"region": DEFAULT_REGION})
        batches = [
            (_point_records(), {}),
            (_nested_dict_records(), {}),
            (_flat_dict_records(), _FLAT_KWARGS),
        ]
        bodies = []
        for records, kwargs in batches:
            self.write_api.write_merged("bucket-a", "my-org", records,
                                        tag_merge_options=options, **kwargs)
            bodies.append(self._requests()[-1].parsed_body)

        self.assertEqual(1, len(set(bodies)))

    def test_switching_targets_does_not_pollute_tags(self):
        primary = TagMergeOptions(default_tags={"region": DEFAULT_REGION})
        secondary = TagMergeOptions(default_tags={"region": OTHER_REGION})
        records = _point_records()

        self.write_api.write_merged("bucket-a", "my-org", records, tag_merge_options=primary)
        primary_body = self._requests()[-1].parsed_body
        self.write_api.write_merged("bucket-b", "my-org", records, tag_merge_options=secondary)
        secondary_body = self._requests()[-1].parsed_body

        self.assertIn(DEFAULT_REGION, primary_body)
        self.assertNotIn(OTHER_REGION, primary_body)
        self.assertIn(OTHER_REGION, secondary_body)
        self.assertNotIn(DEFAULT_REGION, secondary_body)
        # Explicit and omitted semantics survive the target switch.
        self.assertIn(EXPLICIT_REGION, secondary_body)
        self.assertEqual(2, secondary_body.count("region="))

    def test_discarded_records_are_not_sent(self):
        options = TagMergeOptions(default_tags={"region": DEFAULT_REGION})
        records = [Point("sensor").tag("host", "h1"),
                   Point("sensor").field("level", 1).time(1)]
        preview = self.write_api.preview_tags(records, tag_merge_options=options)
        self.assertEqual(1, len(preview.discarded_records()))

        self.write_api.write_merged("bucket-a", "my-org", records, tag_merge_options=options)
        body = self._requests()[0].parsed_body
        self.assertEqual(preview.line_protocol, body)
        self.assertEqual("sensor,region=us-east-1 level=1i 1", body)

    def test_reject_unstructured_records_on_new_path(self):
        with self.assertRaises(TypeError):
            self.write_api.preview_tags("h2o level=1")
        with self.assertRaises(TypeError):
            self.write_api.write_merged("bucket-a", "my-org", b"h2o level=1")

    def test_plain_write_still_works_unchanged(self):
        line = "h2o_feet,location=coyote_creek level=1i 1"
        self.write_api.write("bucket-a", "my-org", line)
        self.assertEqual(line, self._requests()[0].parsed_body)

    def test_nested_dict_with_omitted_key_is_sanitized_before_write(self):
        options = TagMergeOptions(default_tags={"region": DEFAULT_REGION},
                                  omit_tag_keys=["debug"])
        record = {"measurement": "sensor",
                  "tags": {"host": "h1", "debug": "true", "region": OMIT_TAG},
                  "fields": {"level": 1}, "time": 1}
        preview = self.write_api.preview_tags([record], tag_merge_options=options)
        self.assertEqual(["debug", "region"], preview.records[0].omitted_tags)

        self.write_api.write_merged("bucket-a", "my-org", [record], tag_merge_options=options)
        body = self._requests()[0].parsed_body
        self.assertEqual("sensor,host=h1 level=1i 1", body)
        self.assertNotIn("debug", body)

    def test_writer_point_settings_are_used_and_not_mutated(self):
        settings = PointSettings(region=DEFAULT_REGION)
        write_api = self.influxdb_client.write_api(write_options=SYNCHRONOUS,
                                                   point_settings=settings)
        preview = write_api.preview_tags([Point("sensor").field("level", 1).time(1)])
        self.assertEqual({"region": DEFAULT_REGION}, preview.records[0].default_tags)
        # Writer defaults stay intact after the preview.
        self.assertEqual({"region": DEFAULT_REGION}, settings.defaultTags)

        write_api.write_merged("bucket-a", "my-org",
                               [Point("sensor").tag("region", OMIT_TAG).field("level", 2).time(2)])
        body = httpretty.httpretty.latest_requests[-1].parsed_body
        self.assertEqual("sensor level=2i 2", body)
        self.assertEqual({"region": DEFAULT_REGION}, settings.defaultTags)


class BatchingBoundaryTest(unittest.TestCase):

    def setUp(self) -> None:
        conf = influxdb_client.configuration.Configuration()
        conf.host = "http://localhost"
        conf.debug = False
        self.influxdb_client = InfluxDBClient(url=conf.host, token="my-token", org="my-org")
        self.write_api = self.influxdb_client.write_api(write_options=SYNCHRONOUS)
        self.sent_bodies = []

        def record_post_write(_async_req, bucket, org, body, precision, **kwargs):
            self.sent_bodies.append(body.decode("utf-8"))

        self.write_api._post_write = record_post_write

    def tearDown(self) -> None:
        self.influxdb_client.close()

    @staticmethod
    def _chunks(items, size):
        for start in range(0, len(items), size):
            yield items[start:start + size]

    def test_batch_boundary_change_and_reversed_preparation_order(self):
        options = TagMergeOptions(default_tags={"region": DEFAULT_REGION})
        records = _point_records() + [
            Point("sensor").tag("host", "h2").field("level", 4).time(4)
        ]

        # First boundary: chunks of two records.
        forward_preview = self.write_api.preview_tags(records, tag_merge_options=options)
        forward_lines = [item.line_protocol for item in forward_preview.records]

        for chunk in self._chunks(records, 2):
            self.write_api.write_merged("bucket-a", "my-org", chunk, tag_merge_options=options)

        self.assertEqual(2, len(self.sent_bodies))
        self.assertEqual(forward_lines[:2], self.sent_bodies[0].split("\n"))
        self.assertEqual(forward_lines[2:], self.sent_bodies[1].split("\n"))
        self.assertEqual(forward_preview.line_protocol, "\n".join(self.sent_bodies))

        # New boundary (chunks of three) and reversed preparation order.
        reversed_records = list(reversed(records))
        reversed_preview = self.write_api.preview_tags(reversed_records, tag_merge_options=options)
        self.assertEqual(list(reversed(forward_lines)),
                         [item.line_protocol for item in reversed_preview.records])

        reversed_bodies = []
        for chunk in self._chunks(reversed_records, 3):
            preview = self.write_api.preview_tags(chunk, tag_merge_options=options)
            self.write_api.write_merged("bucket-a", "my-org", chunk, tag_merge_options=options)
            reversed_bodies.append(preview.line_protocol)

        new_bodies = self.sent_bodies[2:]
        self.assertEqual(2, len(new_bodies))
        self.assertEqual(3, len(new_bodies[0].split("\n")))
        self.assertEqual(1, len(new_bodies[1].split("\n")))
        self.assertEqual(reversed_bodies, new_bodies)
        self.assertEqual(reversed_preview.line_protocol, "\n".join(new_bodies))


class AsyncPreviewTest(unittest.TestCase):

    def setUp(self) -> None:
        try:
            import aiohttp  # noqa: F401
        except ModuleNotFoundError:
            self.skipTest("aiohttp is required for the asynchronous client")
        from influxdb_client.client.influxdb_client_async import InfluxDBClientAsync
        self.client = InfluxDBClientAsync(url="http://localhost:8086", token="my-token", org="my-org")
        self.write_api = self.client.write_api(
            point_settings=PointSettings(region=DEFAULT_REGION))

    def tearDown(self) -> None:
        loop = asyncio.new_event_loop()
        loop.run_until_complete(self.client.close())
        loop.close()

    def test_async_preview_uses_writer_defaults(self):
        preview = self.write_api.preview_tags(_nested_dict_records())
        self.assertEqual(DEFAULT_REGION, preview.records[1].default_tags["region"])
        self.assertEqual(["region"], preview.records[2].omitted_tags)

    def test_async_write_merged_sends_only_line_protocol(self):
        captured = {}

        async def fake_post_write_async(**kwargs):
            captured["body"] = kwargs["body"]
            return None, 204

        self.write_api._write_service.post_write_async = fake_post_write_async
        options = TagMergeOptions(default_tags={"region": OTHER_REGION})
        preview = self.write_api.preview_tags(_point_records(), tag_merge_options=options)

        result = asyncio.new_event_loop().run_until_complete(
            self.write_api.write_merged("bucket-a", "my-org", _point_records(),
                                        tag_merge_options=options))
        self.assertTrue(result)
        self.assertEqual(preview.line_protocol, captured["body"].decode("utf-8"))
        self.assertNotIn("origin", captured["body"].decode("utf-8"))


if __name__ == "__main__":
    unittest.main()
