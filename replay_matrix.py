"""Reproducible model of InfluxDB OSS v2 write replay semantics.

This does not contact InfluxDB or write to any bucket. It runs the real
``WriteApi`` batching path against an in-process fake ``/api/v2/write``
endpoint implemented with ``httpretty``. The fake parses line protocol and
models the OSS v2 duplicate-point rule:

    same measurement + tag set + timestamp -> union of field sets;
    a conflicting field key is overwritten by the later write.

This field-wise union is the one storage-side assumption in the harness. It
matches the TSM engine's per-field series layout, but can be checked on a
target OSS v2 instance with a controlled A-then-C write and a query for both
fields.

A timestamp-less line is assigned the server's receipt time. OSS v2 assigns
one default receipt time per HTTP request, so all timestamp-less lines in the
same request share that time.

Run:
    .venv/bin/python replay_matrix.py
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from typing import Dict, List, Tuple
from unittest import mock

import httpretty
import urllib3
from urllib3.exceptions import ProtocolError

from influxdb_client import InfluxDBClient, Point, WritePrecision
from influxdb_client.client.write_api import WriteApi, WriteOptions, WriteType

BUCKET = "bucket-incident-01"
ORG = "org-incident-01"
SERIES = "climate,station=lab-01"

# 2025-11-21T02:00:00Z
FIXED_TS = 1763690400000000000
# Distinct server receipt times for timestamp-less requests.
RECEIPT_BASE = 1763690401000000000
RECEIPT_STEP = 1_000_000_000


@dataclass
class Stored:
    fields: Dict[str, float] = field(default_factory=dict)
    writes: List[int] = field(default_factory=list)


class FakeOssV2:
    """In-process OSS v2 write endpoint with receipt-time and duplicate rules."""

    def __init__(self, first_lands: bool, drop_first_response: bool):
        self.first_lands = first_lands
        self.drop_first_response = drop_first_response
        self.lock = threading.Lock()
        self.received: List[dict] = []
        self.store: Dict[Tuple[str, int], Stored] = {}

    def register(self):
        httpretty.register_uri(
            httpretty.POST,
            "http://localhost/api/v2/write",
            body=self.handle,
            match_querystring=False,
        )

    def handle(self, request, uri, response_headers):
        with self.lock:
            seq = len(self.received)
            receipt = RECEIPT_BASE + seq * RECEIPT_STEP
            body = request.body.decode("utf-8")
            first = seq == 0
            should_land = not (first and not self.first_lands)

            record = {
                "seq": seq,
                "receipt": receipt,
                "body": body,
                "landed": should_land,
                "response_lost": first and self.drop_first_response,
            }
            self.received.append(record)

            if should_land:
                self._ingest(body, receipt)

            return [204, {}, ""]

    def _ingest(self, body: str, receipt: int):
        for line in body.splitlines():
            if not line:
                continue
            parts = line.split(" ")
            if len(parts) == 3:
                series, fields_blob, ts_text = parts
                timestamp = int(ts_text)
            elif len(parts) == 2:
                series, fields_blob = parts
                # OSS assigns one default receipt time for the request.
                timestamp = receipt
            else:
                raise ValueError(f"unexpected line protocol: {line!r}")

            stored = self.store.setdefault((series, timestamp), Stored())
            for pair in fields_blob.split(","):
                key, value = pair.split("=", 1)
                stored.fields[key] = _parse_field_value(value)
            stored.writes.append(receipt)

    def rows(self, series: str = SERIES):
        return [
            {
                "timestamp": ts,
                "fields": dict(sorted(stored.fields.items())),
                "writes": stored.writes,
            }
            for (stored_series, ts), stored in sorted(self.store.items())
            if stored_series == series
        ]


def _parse_field_value(value: str) -> float:
    if value.endswith("i"):
        return int(value[:-1])
    return float(value)


def point_temperature(value: float, fixed: bool) -> Point:
    point = Point("climate").tag("station", "lab-01").field("temperature", value)
    return point.time(FIXED_TS, WritePrecision.NS) if fixed else point


def point_humidity(value: float, fixed: bool) -> Point:
    point = Point("climate").tag("station", "lab-01").field("humidity", value)
    return point.time(FIXED_TS, WritePrecision.NS) if fixed else point


def batch_a(fixed: bool) -> List[Point]:
    """A is one batch: temperature and humidity for the same series and time."""
    return [point_temperature(21.5, fixed), point_humidity(40.0, fixed)]


def correction(fixed: bool) -> List[Point]:
    """The other task corrects only temperature."""
    return [point_temperature(22.0, fixed)]


class Recorder:
    def __init__(self):
        self.events: List[dict] = []
        self.terminal = threading.Event()
        self.lock = threading.Lock()

    def success(self, conf, data):
        with self.lock:
            self.events.append({"kind": "success", "data": data.decode("utf-8")})
            self.terminal.set()

    def error(self, conf, data, error):
        with self.lock:
            self.events.append({
                "kind": "error",
                "data": data.decode("utf-8"),
                "error": type(error).__name__,
                "status": getattr(getattr(error, "response", None), "status", None),
            })
            self.terminal.set()

    def retry(self, conf, data, error):
        with self.lock:
            self.events.append({
                "kind": "retry",
                "data": data.decode("utf-8"),
                "error": type(error).__name__,
                "status": getattr(getattr(error, "response", None), "status", None),
            })


def new_api(client: InfluxDBClient, recorder: Recorder, max_retries: int) -> WriteApi:
    options = WriteOptions(
        write_type=WriteType.batching,
        batch_size=2,
        flush_interval=60_000,
        jitter_interval=0,
        retry_interval=0,
        max_retries=max_retries,
        max_retry_delay=0,
        max_retry_time=60_000,
        max_close_wait=5_000,
    )
    return WriteApi(
        influxdb_client=client,
        write_options=options,
        success_callback=recorder.success,
        error_callback=recorder.error,
        retry_callback=recorder.retry,
    )


def send(client: InfluxDBClient, points: List[Point], max_retries: int) -> Recorder:
    recorder = Recorder()
    api = new_api(client, recorder, max_retries)
    try:
        api.write(BUCKET, ORG, points)
        api.flush()
        if not recorder.terminal.wait(timeout=10):
            raise TimeoutError("write did not produce a terminal callback")
    finally:
        api.close()
    return recorder


def run_case(fixed: bool, first_lands: bool, order: str):
    """order: transport-only, correction-before-replay, correction-after-replay."""
    fake = FakeOssV2(first_lands=first_lands, drop_first_response=True)
    httpretty.enable(allow_net_connect=False)
    httpretty.reset()
    fake.register()

    # httpretty invokes the server callback while the client reads the
    # response. Let that happen, then discard the first response to model a
    # timeout after the server accepted the write. urllib3 must replay the
    # identical request body.
    original_getresponse = urllib3.connection.HTTPConnection.getresponse
    lost = {"fired": False}

    def getresponse(conn, *args, **kwargs):
        response = original_getresponse(conn, *args, **kwargs)
        if fake.received and fake.received[0]["response_lost"] and not lost["fired"]:
            lost["fired"] = True
            raise ProtocolError("Response ended prematurely")
        return response

    client = InfluxDBClient(url="http://localhost", token="test-token", org=ORG)
    events: List[dict] = []
    try:
        with mock.patch.object(urllib3.connection.HTTPConnection, "getresponse", getresponse):
            events += send(client, batch_a(fixed), max_retries=2).events

            if order == "correction-before-replay":
                events += send(client, correction(fixed), max_retries=0).events
                events += send(client, batch_a(fixed), max_retries=0).events
            elif order == "correction-after-replay":
                events += send(client, batch_a(fixed), max_retries=0).events
                events += send(client, correction(fixed), max_retries=0).events
            elif order != "transport-only":
                raise ValueError(order)
    finally:
        client.close()
        httpretty.disable()

    return {
        "fixed_timestamp": fixed,
        "first_attempt_landed": first_lands,
        "order": order,
        "http_attempts": [
            {
                "seq": r["seq"],
                "receipt": r["receipt"],
                "landed": r["landed"],
                "response_lost": r["response_lost"],
                "body": r["body"],
            }
            for r in fake.received
        ],
        "client_events": events,
        "rows": fake.rows(),
    }


def same_rows(left, right) -> bool:
    return [
        (row["timestamp"], row["fields"]) for row in left
    ] == [
        (row["timestamp"], row["fields"]) for row in right
    ]


def assert_matrix(cases):
    by_key = {
        (case["fixed_timestamp"], case["first_attempt_landed"], case["order"]): case
        for case in cases
    }

    for fixed in (True, False):
        landed = by_key[(fixed, True, "transport-only")]
        missed = by_key[(fixed, False, "transport-only")]

        # The retry replays the identical bytes, but not necessarily the same
        # storage identity.
        assert landed["http_attempts"][0]["body"] == landed["http_attempts"][1]["body"]
        assert missed["http_attempts"][0]["body"] == missed["http_attempts"][1]["body"]
        assert landed["client_events"][0]["kind"] == "retry"
        assert landed["client_events"][-1]["kind"] == "success"

        if fixed:
            # Fixed time makes A's replay idempotent at the series/time key.
            assert same_rows(landed["rows"], missed["rows"])
            assert len(landed["rows"]) == 1
            assert landed["rows"][0]["timestamp"] == FIXED_TS
        else:
            # No client timestamp: the server stamps each receipt separately.
            assert len(landed["rows"]) == 2
            assert len(missed["rows"]) == 1
            assert landed["rows"][0]["timestamp"] != landed["rows"][1]["timestamp"]

    for fixed in (True, False):
        before = by_key[(fixed, True, "correction-before-replay")]
        after = by_key[(fixed, True, "correction-after-replay")]
        before_missed = by_key[(fixed, False, "correction-before-replay")]
        after_missed = by_key[(fixed, False, "correction-after-replay")]

        if fixed:
            # C then A replay: A wins, because C and A share the same key.
            assert before["rows"][0]["fields"]["temperature"] == 21.5
            # Although the first packet did not land, the final state is the same.
            assert same_rows(before["rows"], before_missed["rows"])
            # A replay then C: C wins and the humidity field remains.
            assert after["rows"][0]["fields"]["temperature"] == 22.0
            assert after["rows"][0]["fields"]["humidity"] == 40.0
            assert same_rows(after["rows"], after_missed["rows"])
        else:
            # Every application write gets a distinct server receipt time.
            assert len(before["rows"]) == 4
            assert len(before_missed["rows"]) == 3
            assert len(after["rows"]) == 4
            assert len(after_missed["rows"]) == 3

    return "matrix assertions passed"


if __name__ == "__main__":
    cases = []
    for fixed in (True, False):
        for first_lands in (False, True):
            for order in (
                "transport-only",
                "correction-before-replay",
                "correction-after-replay",
            ):
                cases.append(run_case(fixed, first_lands, order))
    assert_matrix(cases)
    print(json.dumps(cases, indent=2))
