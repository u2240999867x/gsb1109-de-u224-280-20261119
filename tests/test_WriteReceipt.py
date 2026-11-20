# coding: utf-8

"""Tests for the client-side per-source write receipt handed off after (partial) writes.

Multi-line correlation (identical lines, partial/conflicting diagnostics, lost
responses, package position isolation) is covered by deterministic unit tests on
the receipt builder. The batching end-to-end tests use ``batch_size=1`` plus an
explicit ``flush()`` and assert on receipt content keyed by source/occurrence so
they do not depend on wall-clock windows.
"""

from __future__ import absolute_import

import json
import time
import unittest

import httpretty

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write.receipt import LineOrigin, LineStatus, build_receipt, parse_line_diagnostics
from influxdb_client.client.write_api import WriteApi, WriteOptions, WriteType

WRITE_URL = "http://localhost/api/v2/write"


def _diagnostics_body(entries):
    return json.dumps({"code": "invalid", "message": "partial failure", "lines": entries})


def _origins(source, lines):
    """Build provenance for ``lines`` handed in by one source (1-based occurrences)."""
    return [LineOrigin(source=source, occurrence=index + 1, line=line.encode())
            for index, line in enumerate(lines)]


class ReceiptBuilderTest(unittest.TestCase):
    """Deterministic tests of the evidence rules without any HTTP/batching machinery."""

    def test_full_success_confirms_every_line(self):
        origins = _origins("A", ["m f=1 1", "m f=2 2"])
        receipt = build_receipt(origins, http_status=204, response_body=None, request_index=7)

        self.assertEqual(7, receipt.request_index)
        self.assertTrue(all(result.status is LineStatus.CONFIRMED for result in receipt))
        self.assertEqual([1, 2], receipt.for_source("A").confirmed)
        self.assertTrue(receipt.for_source("A").is_fully_confirmed)

    def test_identical_lines_from_two_sources_distinguished_by_position(self):
        identical = "m,tag=x f=1 100"
        origins = [LineOrigin(source="B", occurrence=1, line=identical.encode()),
                   LineOrigin(source="C", occurrence=1, line=identical.encode())]
        body = _diagnostics_body([
            {"line": 0, "status": "accepted"},
            {"line": 1, "status": "rejected", "reason": "field type conflict"},
        ])
        receipt = build_receipt(origins, http_status=400, response_body=body)

        self.assertEqual(2, len(receipt.lines))
        # Same bytes, but the in-request position maps each line back to a different source.
        self.assertEqual(["B", "C"], [result.source for result in receipt.lines])
        self.assertIs(LineStatus.CONFIRMED, receipt.status_of("B", 1))
        self.assertIs(LineStatus.REJECTED, receipt.status_of("C", 1))
        self.assertEqual("field type conflict", receipt.for_source("C").rejected[0].reason)

    def test_one_source_multiple_lines_partial_accept_keeps_each_evidence(self):
        origins = _origins("F", ["m f=1 1", "badline", "m f=3 3", "anotherbad"])
        body = _diagnostics_body([
            {"line": 0, "status": "accepted"},
            {"line": 1, "status": "rejected", "reason": "bad field"},
            {"line": 2, "status": "accepted"},
            {"line": 3, "status": "rejected", "reason": "missing field"},
        ])
        receipt = build_receipt(origins, http_status=400, response_body=body)

        self.assertIs(LineStatus.CONFIRMED, receipt.status_of("F", 1))
        self.assertIs(LineStatus.REJECTED, receipt.status_of("F", 2))
        self.assertIs(LineStatus.CONFIRMED, receipt.status_of("F", 3))
        self.assertIs(LineStatus.REJECTED, receipt.status_of("F", 4))
        rejected = receipt.for_source("F").rejected
        self.assertEqual([2, 4], [result.occurrence for result in rejected])
        self.assertEqual(["badline", "anotherbad"], [result.line.decode() for result in rejected])

    def test_partial_response_only_claims_evidenced_lines(self):
        origins = _origins("E", ["m f=1 1", "m f=2 2", "m f=3 3"])
        # Only positions 0 and 2 are described; position 1 must remain unknown.
        body = _diagnostics_body([
            {"line_index": 0, "status": "accepted"},
            {"line_index": 2, "status": "rejected", "reason": "unparseable timestamp"},
        ])
        receipt = build_receipt(origins, http_status=207, response_body=body)

        source_receipt = receipt.for_source("E")
        self.assertEqual([1], source_receipt.confirmed)
        self.assertEqual([2], source_receipt.unknown)
        self.assertEqual([3], [result.occurrence for result in source_receipt.rejected])

    def test_aggregate_reject_count_leaves_everything_unknown(self):
        origins = _origins("D", ["m f=1 1", "m f=2 2", "m f=3 3"])
        body = json.dumps({"code": "invalid", "message": "3 points rejected", "rejected": 3})
        receipt = build_receipt(origins, http_status=400, response_body=body)

        self.assertTrue(all(result.status is LineStatus.UNKNOWN for result in receipt))
        self.assertEqual([1, 2, 3], receipt.for_source("D").unknown)

    def test_number_inside_error_message_is_not_evidence(self):
        origins = _origins("D", ["m f=1 1", "m f=2 2"])
        body = json.dumps({"code": "invalid", "message": "line 5 failed somewhere"})
        receipt = build_receipt(origins, http_status=400, response_body=body)
        self.assertTrue(all(result.status is LineStatus.UNKNOWN for result in receipt))

    def test_conflicting_diagnostics_claim_nothing(self):
        origins = _origins("G", ["m f=1 1", "m f=2 2"])
        body = _diagnostics_body([
            {"line": 1, "status": "accepted"},
            {"line": 1, "status": "rejected", "reason": "contradiction"},
        ])
        receipt = build_receipt(origins, http_status=400, response_body=body)
        self.assertTrue(all(result.status is LineStatus.UNKNOWN for result in receipt))

    def test_lost_response_leaves_every_line_unknown(self):
        origins = _origins("H", ["m f=1 1", "m f=2 2"])
        receipt = build_receipt(origins, http_status=None, response_body=None)
        self.assertTrue(all(result.status is LineStatus.UNKNOWN for result in receipt))
        self.assertEqual([1, 2], receipt.for_source("H").unknown)

    def test_package_positions_are_independent(self):
        # Package A: position 0 explicitly rejected.
        receipt_a = build_receipt(
            _origins("package-A", ["m f=1 1"]), http_status=400,
            response_body=_diagnostics_body([{"line": 0, "status": "rejected",
                                              "reason": "package A failure"}]),
            request_index=1)
        # Package B: only an aggregate count, so its position 0 is not inferred from package A.
        receipt_b = build_receipt(
            _origins("package-B", ["m f=2 2"]), http_status=400,
            response_body=json.dumps({"message": "1 rejected", "rejected": 1}),
            request_index=2)

        self.assertEqual(1, receipt_a.request_index)
        self.assertEqual(2, receipt_b.request_index)
        self.assertIs(LineStatus.REJECTED, receipt_a.status_of("package-A", 1))
        self.assertIs(LineStatus.UNKNOWN, receipt_b.status_of("package-B", 1))
        self.assertIsNone(receipt_b.lines[0].reason)


class LineDiagnosticsParsingTest(unittest.TestCase):

    def test_parses_zero_based_positions(self):
        body = json.dumps({"lines": [{"line": 0, "status": "accepted"},
                                     {"line": 1, "status": "rejected"}]})
        self.assertEqual({0: LineStatus.CONFIRMED, 1: LineStatus.REJECTED},
                         parse_line_diagnostics(body, expected_count=2))

    def test_aggregate_count_returns_none(self):
        self.assertIsNone(parse_line_diagnostics(
            json.dumps({"message": "2 rejected", "rejected": 2}), expected_count=2))

    def test_out_of_range_position_is_not_evidence(self):
        body = json.dumps({"lines": [{"line": 7, "status": "rejected"}]})
        self.assertIsNone(parse_line_diagnostics(body, expected_count=2))

    def test_conflicting_positions_raise(self):
        body = json.dumps({"lines": [{"line": 0, "status": "accepted"},
                                     {"line": 0, "status": "rejected"}]})
        with self.assertRaises(ValueError):
            parse_line_diagnostics(body, expected_count=2)

    def test_non_json_body_returns_none(self):
        self.assertIsNone(parse_line_diagnostics("unable to parse line", expected_count=2))
        self.assertIsNone(parse_line_diagnostics(None, expected_count=2))

    def test_empty_diagnostics_list_returns_none(self):
        self.assertIsNone(parse_line_diagnostics(json.dumps({"lines": []}), expected_count=3))


class BatchingReceiptTest(unittest.TestCase):
    """End-to-end batching tests; assertions are keyed by source/occurrence."""

    def setUp(self) -> None:
        httpretty.enable()
        httpretty.reset()
        self.influxdb_client = InfluxDBClient(url="http://localhost", token="my-token")
        self.receipts = []
        self.write_api = WriteApi(
            influxdb_client=self.influxdb_client,
            write_options=WriteOptions(batch_size=1, flush_interval=60_000, max_retries=0),
            receipt_callback=self.receipts.append)

    def tearDown(self) -> None:
        self.write_api.close()
        httpretty.disable()

    def _wait_until(self, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail("condition not met; receipts=%s" % (self.receipts,))

    def _status_by_occurrence(self, source):
        mapping = {}
        for receipt in self.receipts:
            for result in receipt.lines:
                if result.source == source:
                    mapping[result.occurrence] = result.status
        return mapping

    def test_successful_write_is_confirmed(self):
        httpretty.register_uri(httpretty.POST, uri=WRITE_URL, status=204)
        self.write_api.write("b", "o", "m f=1 1", source="A")
        self.write_api.flush()
        self._wait_until(lambda: self._status_by_occurrence("A") == {1: LineStatus.CONFIRMED})

    def test_per_line_rejection_is_mapped_back_to_source(self):
        httpretty.register_uri(httpretty.POST, uri=WRITE_URL, status=400,
                               body=_diagnostics_body([{"line": 0, "status": "rejected",
                                                        "reason": "bad field"}]),
                               adding_headers={"Content-Type": "application/json"})
        self.write_api.write("b", "o", "m f=1 x", source="A")
        self.write_api.flush()
        self._wait_until(lambda: self._status_by_occurrence("A") == {1: LineStatus.REJECTED})
        rejected = self.receipts[-1].for_source("A").rejected
        self.assertEqual("bad field", rejected[0].reason)

    def test_aggregate_only_response_is_unknown(self):
        httpretty.register_uri(httpretty.POST, uri=WRITE_URL, status=400,
                               body=json.dumps({"code": "invalid", "message": "1 rejected",
                                                "rejected": 1}),
                               adding_headers={"Content-Type": "application/json"})
        self.write_api.write("b", "o", "m f=1 1", source="A")
        self.write_api.flush()
        self._wait_until(lambda: self._status_by_occurrence("A") == {1: LineStatus.UNKNOWN})

    def test_lost_response_is_unknown(self):
        import urllib3

        def boom(_request, url, _headers):
            raise urllib3.exceptions.MaxRetryError(None, url, "connection reset by peer")

        httpretty.register_uri(httpretty.POST, uri=WRITE_URL, body=boom)
        self.write_api.write("b", "o", "m f=1 1", source="H")
        self.write_api.flush()
        self._wait_until(lambda: self._status_by_occurrence("H") == {1: LineStatus.UNKNOWN})

    def test_occurrences_are_numbered_per_source(self):
        httpretty.register_uri(httpretty.POST, uri=WRITE_URL, status=204)
        self.write_api.write("b", "o", "m f=1 1", source="A")
        self.write_api.write("b", "o", "m f=2 2", source="A")
        self.write_api.write("b", "o", "m f=3 3", source="A")
        self.write_api.flush()
        self._wait_until(lambda: self._status_by_occurrence("A") == {
            1: LineStatus.CONFIRMED, 2: LineStatus.CONFIRMED, 3: LineStatus.CONFIRMED})

    def test_identical_lines_from_two_sources_keep_identity(self):
        httpretty.register_uri(httpretty.POST, uri=WRITE_URL, status=204)
        identical = "m,tag=x f=1 100"
        self.write_api.write("b", "o", [identical], source="B")
        self.write_api.write("b", "o", [identical], source="C")
        self.write_api.flush()
        self._wait_until(lambda: self._status_by_occurrence("B") == {1: LineStatus.CONFIRMED}
                         and self._status_by_occurrence("C") == {1: LineStatus.CONFIRMED})

        sources = {result.source for receipt in self.receipts for result in receipt.lines
                   if result.source in ("B", "C")}
        self.assertEqual({"B", "C"}, sources)

    def test_precision_split_preserves_occurrences(self):
        httpretty.register_uri(httpretty.POST, uri=WRITE_URL, status=204)
        points = [Point("m").field("f", 1).time(1, WritePrecision.S),
                  Point("m").field("f", 2).time(2, WritePrecision.NS)]
        self.write_api.write("b", "o", points, source="MP")
        self.write_api.flush()
        self._wait_until(lambda: self._status_by_occurrence("MP") == {
            1: LineStatus.CONFIRMED, 2: LineStatus.CONFIRMED})

        precisions = {request.querystring["precision"][0]
                      for request in httpretty.httpretty.latest_requests}
        self.assertEqual({"s", "ns"}, precisions)

    def test_source_never_added_to_wire_payload(self):
        httpretty.register_uri(httpretty.POST, uri=WRITE_URL, status=204)
        self.write_api.write("b", "o", "m f=1 1", source="must-not-appear")
        self.write_api.flush()
        self._wait_until(lambda: bool(self.receipts))
        for request in httpretty.httpretty.latest_requests:
            self.assertNotIn("must-not-appear", request.parsed_body)
            self.assertNotIn("source", request.parsed_body)

    def test_sourceless_write_is_still_tracked(self):
        httpretty.register_uri(httpretty.POST, uri=WRITE_URL, status=204)
        self.write_api.write("b", "o", "m f=1 1")
        self.write_api.flush()
        self._wait_until(lambda: any(not result.source for receipt in self.receipts
                                     for result in receipt.lines))
        result = next(result for receipt in self.receipts for result in receipt.lines
                      if not result.source)
        self.assertIsNone(result.occurrence)
        self.assertIs(LineStatus.CONFIRMED, result.status)


class SynchronousSourceTest(unittest.TestCase):
    """The source hint must not break or alter non-batching writes."""

    def setUp(self) -> None:
        httpretty.enable()
        httpretty.reset()
        self.influxdb_client = InfluxDBClient(url="http://localhost", token="my-token")
        self.write_api = WriteApi(influxdb_client=self.influxdb_client,
                                  write_options=WriteOptions(write_type=WriteType.synchronous))

    def tearDown(self) -> None:
        self.write_api.close()
        httpretty.disable()

    def test_source_ignored_on_wire_in_synchronous_mode(self):
        httpretty.register_uri(httpretty.POST, uri=WRITE_URL, status=204)
        self.write_api.write("b", "o", ["m f=1 1", "m f=2 2"], source="not-on-wire")
        request = httpretty.httpretty.latest_requests[-1]
        self.assertEqual("m f=1 1\nm f=2 2", request.parsed_body)
        self.assertNotIn("not-on-wire", request.parsed_body)
        self.assertEqual(WriteType.synchronous, self.write_api._write_options.write_type)


if __name__ == '__main__':
    unittest.main()
