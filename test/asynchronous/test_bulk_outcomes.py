# Copyright 2026-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Test per-operation evidence recorded by the collection-level bulk API.

These tests drive the real ``_AsyncBulk.execute`` batching and merge code with
canned server replies, so they need no MongoDB server. They exercise a batch
limit that splits one update run into two commands, with insert and delete
operations interleaved in the original input.
"""

from __future__ import annotations

import sys

sys.path[0:0] = [""]

from bson.codec_options import DEFAULT_CODEC_OPTIONS
from pymongo.asynchronous.bulk import _AsyncBulk
from pymongo.errors import BulkWriteError, ConnectionFailure, InvalidOperation
from pymongo.operations import DeleteOne, InsertOne, UpdateOne
from pymongo.results import (
    BulkWriteOperationOutcome,
    BulkWriteOutcomeStatus,
    BulkWriteResult,
)
from pymongo.write_concern import WriteConcern
from test.asynchronous import unittest

_IS_SYNC = False


class _StubConnection:
    def validate_session(self, *args, **kwargs):
        pass

    def send_cluster_time(self, *args, **kwargs):
        pass

    def add_server_api(self, *args, **kwargs):
        pass

    def apply_timeout(self, *args, **kwargs):
        pass


class _StubContext:
    def __init__(self, *args, **kwargs):
        pass


class _StubDatabase:
    name = "db"

    def __init__(self, client):
        self.client = client


class _StubCollection:
    name = "coll"
    codec_options = DEFAULT_CODEC_OPTIONS

    def __init__(self, client):
        self.database = _StubDatabase(client)

    def with_options(self, **kwargs):
        return self


class _StubClient:
    _event_listeners = None
    _encrypter = None

    async def _retryable_write(self, *args, **kwargs):
        func = args[1]
        session = args[2]
        return await func(session, _StubConnection(), False)


class _CannedBulk(_AsyncBulk):
    """A _AsyncBulk whose batches and replies are supplied by the test."""

    batch_limit = 2

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.replies: list[dict] = []
        self.batches: list[tuple[str, int]] = []

    @property
    def bulk_ctx_class(self):
        self.is_encrypted = False
        return _StubContext

    async def _execute_batch(self, bwc, cmd, ops, client, on_send=None):
        ops = list(ops)
        batch = ops[: self.batch_limit]
        if on_send is not None:
            on_send(len(batch))
        op_type = next(key for key in ("insert", "update", "delete") if key in cmd)
        self.batches.append((op_type, len(batch)))
        return self.replies.pop(0), batch


def _make_bulk(models, ordered=False):
    client = _StubClient()
    collection = _StubCollection(client)
    bulk = _CannedBulk(collection, ordered=ordered, bypass_document_validation=None)
    for model in models:
        model._add_to_bulk(bulk)
    return bulk


def _models():
    return [
        InsertOne({"_id": "ins-0"}),
        UpdateOne({"_id": "upd-1"}, {"$set": {"x": 1}}),
        DeleteOne({"_id": "del-2"}),
        UpdateOne({"_id": "upd-3"}, {"$set": {"x": 3}}),
        InsertOne({"_id": "ins-4"}),
        UpdateOne({"_id": "upd-5"}, {"$set": {"x": 5}}),
    ]


class TestBulkOperationOutcomes(unittest.IsolatedAsyncioTestCase):
    async def test_indexes_are_original_input_positions(self):
        bulk = _make_bulk(_models())
        bulk.replies = [
            {"ok": 1, "n": 2},
            {
                "ok": 1,
                "n": 3,
                "nModified": 1,
                "writeErrors": [{"index": 1, "code": 11000, "errmsg": "dup"}],
                "upserted": [{"index": 0, "_id": "upserted-for-upd-1"}],
            },
            {
                "ok": 1,
                "n": 1,
                "nModified": 1,
                "writeErrors": [{"index": 0, "code": 121, "errmsg": "validation"}],
                "writeConcernError": {"code": 100, "errmsg": "unsatisfiable"},
            },
            {"ok": 1, "n": 1},
        ]
        with self.assertRaises(BulkWriteError) as ctx:
            await bulk.execute(WriteConcern(), None, "insert")

        self.assertEqual([("insert", 2), ("update", 2), ("update", 1), ("delete", 1)], bulk.batches)
        outcomes = ctx.exception.operation_outcomes
        self.assertEqual(list(range(6)), [outcome.index for outcome in outcomes])

        # Both update commands reported command-local position 0, but they
        # resolve to input positions 1 and 5 respectively.
        self.assertEqual(BulkWriteOutcomeStatus.PROVEN, outcomes[1].status)
        self.assertEqual("upserted-for-upd-1", outcomes[1].upserted_id)
        self.assertEqual(BulkWriteOutcomeStatus.WRITE_ERROR, outcomes[3].status)
        self.assertEqual(11000, outcomes[3].write_error["code"])
        self.assertEqual(BulkWriteOutcomeStatus.WRITE_ERROR, outcomes[5].status)
        self.assertEqual(121, outcomes[5].write_error["code"])

    async def test_write_concern_error_keeps_write_error_and_other_commands(self):
        bulk = _make_bulk(_models())
        bulk.replies = [
            {"ok": 1, "n": 2},
            {
                "ok": 1,
                "n": 3,
                "nModified": 1,
                "writeErrors": [{"index": 1, "code": 11000, "errmsg": "dup"}],
                "upserted": [{"index": 0, "_id": "upserted-for-upd-1"}],
            },
            {
                "ok": 1,
                "n": 1,
                "nModified": 1,
                "writeErrors": [{"index": 0, "code": 121, "errmsg": "validation"}],
                "writeConcernError": {"code": 100, "errmsg": "unsatisfiable"},
            },
            {"ok": 1, "n": 1},
        ]
        with self.assertRaises(BulkWriteError) as ctx:
            await bulk.execute(WriteConcern(), None, "insert")

        outcomes = ctx.exception.operation_outcomes
        self.assertEqual(BulkWriteOutcomeStatus.WRITE_ERROR, outcomes[5].status)
        self.assertIsNotNone(outcomes[5].write_error)
        self.assertEqual(100, outcomes[5].write_concern_error["code"])
        self.assertEqual(BulkWriteOutcomeStatus.PROVEN, outcomes[1].status)
        self.assertIsNone(outcomes[1].write_concern_error)
        self.assertIsNone(outcomes[1].write_error)

    async def test_count_only_confirmation_is_not_a_proven_change(self):
        bulk = _make_bulk(_models())
        bulk.replies = [
            {"ok": 1, "n": 2},
            {
                "ok": 1,
                "n": 2,
                "nModified": 0,
                "writeErrors": [{"index": 0, "code": 11000, "errmsg": "dup"}],
            },
            {"ok": 1, "n": 1, "nModified": 1},
            {"ok": 1, "n": 1, "nModified": 1},
        ]
        with self.assertRaises(BulkWriteError) as ctx:
            await bulk.execute(WriteConcern(), None, "insert")

        outcomes = ctx.exception.operation_outcomes
        self.assertEqual(BulkWriteOutcomeStatus.ACKNOWLEDGED, outcomes[2].status)
        self.assertIsNone(outcomes[2].definitely_changed)
        self.assertIsNone(outcomes[2].write_error)
        self.assertEqual(BulkWriteOutcomeStatus.ACKNOWLEDGED, outcomes[5].status)
        self.assertIsNone(outcomes[5].definitely_changed)

    async def test_error_in_second_command_maps_to_its_own_operation(self):
        bulk = _make_bulk(_models())
        bulk.replies = [
            {"ok": 1, "n": 2},
            {"ok": 1, "n": 2, "nModified": 1},
            {
                "ok": 1,
                "n": 2,
                "nModified": 1,
                "writeErrors": [{"index": 0, "code": 11000, "errmsg": "dup"}],
            },
            {"ok": 1, "n": 0, "nModified": 0},
        ]
        with self.assertRaises(BulkWriteError) as ctx:
            await bulk.execute(WriteConcern(), None, "insert")

        outcomes = ctx.exception.operation_outcomes
        self.assertEqual(BulkWriteOutcomeStatus.ACKNOWLEDGED, outcomes[1].status)
        self.assertEqual(BulkWriteOutcomeStatus.ACKNOWLEDGED, outcomes[3].status)
        self.assertEqual(BulkWriteOutcomeStatus.WRITE_ERROR, outcomes[5].status)
        self.assertFalse(outcomes[5].definitely_changed)
        self.assertEqual(BulkWriteOutcomeStatus.ACKNOWLEDGED, outcomes[2].status)

    async def test_ordered_stop_marks_remaining_operations_not_executed(self):
        models = [
            InsertOne({"_id": "a"}),
            InsertOne({"_id": "b"}),
            InsertOne({"_id": "c"}),
            InsertOne({"_id": "d"}),
        ]
        bulk = _make_bulk(models, ordered=True)
        bulk.batch_limit = 4
        bulk.replies = [
            {
                "ok": 1,
                "n": 1,
                "writeErrors": [{"index": 1, "code": 11000, "errmsg": "dup"}],
            },
        ]
        with self.assertRaises(BulkWriteError) as ctx:
            await bulk.execute(WriteConcern(), None, "insert")

        outcomes = ctx.exception.operation_outcomes
        self.assertEqual(BulkWriteOutcomeStatus.PROVEN, outcomes[0].status)
        self.assertEqual(BulkWriteOutcomeStatus.WRITE_ERROR, outcomes[1].status)
        self.assertEqual(BulkWriteOutcomeStatus.NOT_EXECUTED, outcomes[2].status)
        self.assertEqual(BulkWriteOutcomeStatus.NOT_EXECUTED, outcomes[3].status)
        self.assertFalse(outcomes[2].definitely_changed)
        self.assertFalse(outcomes[3].definitely_changed)

    async def test_failure_before_send_is_not_sent(self):
        bulk = _make_bulk(_models())
        bulk._execute_batch = _raise_before_send  # type: ignore[method-assign]
        with self.assertRaises(ConnectionFailure) as ctx:
            await bulk.execute(WriteConcern(), None, "insert")

        outcomes = ctx.exception.operation_outcomes
        self.assertEqual(6, len(outcomes))
        for outcome in outcomes:
            self.assertEqual(BulkWriteOutcomeStatus.NOT_SENT, outcome.status)
            self.assertFalse(outcome.definitely_changed)

    async def test_failure_after_batch_built_is_unknown_not_sent(self):
        bulk = _make_bulk(_models())
        bulk._execute_batch = _raise_after_send  # type: ignore[method-assign]
        with self.assertRaises(ConnectionFailure) as ctx:
            await bulk.execute(WriteConcern(), None, "insert")

        outcomes = ctx.exception.operation_outcomes
        # The first batch reached the wire, so its operations are unknown. The
        # update and delete commands were never built.
        self.assertEqual(BulkWriteOutcomeStatus.UNKNOWN, outcomes[0].status)
        self.assertEqual(BulkWriteOutcomeStatus.UNKNOWN, outcomes[4].status)
        self.assertIsNone(outcomes[0].definitely_changed)
        for index in (1, 2, 3, 5):
            self.assertEqual(BulkWriteOutcomeStatus.NOT_SENT, outcomes[index].status)

    async def test_success_result_exposes_outcomes(self):
        bulk = _make_bulk(_models())
        bulk.replies = [
            {"ok": 1, "n": 2},
            {"ok": 1, "n": 2, "nModified": 2},
            {"ok": 1, "n": 1, "nModified": 1},
            {"ok": 1, "n": 1},
        ]
        raw = await bulk.execute(WriteConcern(), None, "insert")
        result = BulkWriteResult(raw, True, bulk.operation_outcomes())

        outcomes = result.operation_outcomes
        self.assertEqual(6, len(outcomes))
        self.assertEqual(
            {0, 4},
            {o.index for o in outcomes if o.status is BulkWriteOutcomeStatus.PROVEN},
        )
        self.assertEqual(BulkWriteOutcomeStatus.ACKNOWLEDGED, outcomes[2].status)
        self.assertEqual(2, result.inserted_count)
        self.assertEqual(1, result.deleted_count)
        self.assertEqual(3, result.matched_count)
        self.assertEqual(list(range(6)), sorted(result.operation_outcome_map))

    async def test_existing_aggregate_result_is_unchanged(self):
        bulk = _make_bulk(_models())
        bulk.replies = [
            {"ok": 1, "n": 2},
            {
                "ok": 1,
                "n": 3,
                "nModified": 1,
                "writeErrors": [{"index": 1, "code": 11000, "errmsg": "dup"}],
                "upserted": [{"index": 0, "_id": "upserted-for-upd-1"}],
            },
            {"ok": 1, "n": 1, "nModified": 1},
            {"ok": 1, "n": 1},
        ]
        with self.assertRaises(BulkWriteError) as ctx:
            await bulk.execute(WriteConcern(), None, "insert")

        details = ctx.exception.details
        self.assertEqual([], details["writeConcernErrors"])
        self.assertEqual(
            [{"index": 1, "_id": "upserted-for-upd-1"}],
            details["upserted"],
        )
        self.assertEqual(
            [{"index": 3, "code": 11000}],
            [{k: e[k] for k in ("index", "code")} for e in details["writeErrors"]],
        )

    async def test_unacknowledged_result_hides_outcomes(self):
        result = BulkWriteResult({}, False)
        with self.assertRaisesRegex(InvalidOperation, "not available when"):
            result.operation_outcomes

    async def test_outcome_repr_is_stable(self):
        outcome = BulkWriteOperationOutcome(3, "update")
        self.assertEqual(
            "BulkWriteOperationOutcome(index=3, operation_type='update', status='not_sent')",
            repr(outcome),
        )


async def _raise_before_send(*args, **kwargs):
    raise ConnectionFailure("connection dropped before send")


async def _raise_after_send(bwc, cmd, ops, client, on_send=None):
    batch = list(ops)[:2]
    if on_send is not None:
        on_send(len(batch))
    raise ConnectionFailure("connection dropped after send")


if __name__ == "__main__":
    unittest.main()
