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

These tests drive the real ``_Bulk.execute`` batching and merge code with
canned server replies, so they need no MongoDB server. They exercise a batch
limit that splits one update run into two commands, with insert and delete
operations interleaved in the original input.
"""

from __future__ import annotations

import sys

sys.path[0:0] = [""]

from bson.codec_options import DEFAULT_CODEC_OPTIONS
from pymongo.errors import BulkWriteError, ConnectionFailure, InvalidOperation
from pymongo.operations import DeleteOne, InsertOne, UpdateOne
from pymongo.results import (
    BulkWriteOperationOutcome,
    BulkWriteOutcomeStatus,
    BulkWriteResult,
)
from pymongo.synchronous.bulk import _Bulk
from pymongo.write_concern import WriteConcern
from test import unittest


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

    def _retryable_write(self, *args, **kwargs):
        func = args[1]
        session = args[2]
        return func(session, _StubConnection(), False)


class _CannedBulk(_Bulk):
    """A _Bulk whose batches and replies are supplied by the test."""

    batch_limit = 2

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.replies: list[dict] = []
        self.batches: list[tuple[str, int]] = []

    @property
    def bulk_ctx_class(self):
        self.is_encrypted = False
        return _StubContext

    def _execute_batch(self, bwc, cmd, ops, client, on_send=None):
        ops = list(ops)
        # The batch limit models the server's maxWriteBatchSize, which applies
        # to every command. Ordered runs still batch consecutive same-type
        # operations together, so apply the limit to both.
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
    # Original input order: insert 0, update 1, delete 2, update 3, insert 4,
    # update 5. Unordered runs therefore reorder the update group to 1, 3, 5.
    return [
        InsertOne({"_id": "ins-0"}),
        UpdateOne({"_id": "upd-1"}, {"$set": {"x": 1}}),
        DeleteOne({"_id": "del-2"}),
        UpdateOne({"_id": "upd-3"}, {"$set": {"x": 3}}),
        InsertOne({"_id": "ins-4"}),
        UpdateOne({"_id": "upd-5"}, {"$set": {"x": 5}}),
    ]


class TestBulkOperationOutcomes(unittest.TestCase):
    def test_indexes_are_original_input_positions(self):
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
            bulk.execute(WriteConcern(), None, "insert")

        self.assertEqual([("insert", 2), ("update", 2), ("update", 1), ("delete", 1)], bulk.batches)
        outcomes = ctx.exception.operation_outcomes
        self.assertEqual(list(range(6)), [outcome.index for outcome in outcomes])

        # Both update commands reported command-local position 0, but they
        # resolve to input positions 1 and 5 respectively.
        self.assertEqual(
            BulkWriteOutcomeStatus.PROVEN,
            outcomes[1].status,
        )
        self.assertEqual("upserted-for-upd-1", outcomes[1].upserted_id)
        self.assertEqual(BulkWriteOutcomeStatus.WRITE_ERROR, outcomes[3].status)
        self.assertEqual(11000, outcomes[3].write_error["code"])
        self.assertEqual(BulkWriteOutcomeStatus.WRITE_ERROR, outcomes[5].status)
        self.assertEqual(121, outcomes[5].write_error["code"])

    def test_write_concern_error_keeps_write_error_and_other_commands(self):
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
            bulk.execute(WriteConcern(), None, "insert")

        outcomes = ctx.exception.operation_outcomes
        # The second update command both failed this operation and missed the
        # write concern. The explicit write error is not erased...
        self.assertEqual(BulkWriteOutcomeStatus.WRITE_ERROR, outcomes[5].status)
        self.assertIsNotNone(outcomes[5].write_error)
        self.assertEqual(100, outcomes[5].write_concern_error["code"])
        # ...and the first, acknowledged update command is not polluted.
        self.assertEqual(BulkWriteOutcomeStatus.PROVEN, outcomes[1].status)
        self.assertIsNone(outcomes[1].write_concern_error)
        self.assertIsNone(outcomes[1].write_error)

    def test_count_only_confirmation_is_not_a_proven_change(self):
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
            {"ok": 1, "n": 1},
        ]
        with self.assertRaises(BulkWriteError) as ctx:
            bulk.execute(WriteConcern(), None, "insert")

        outcomes = ctx.exception.operation_outcomes
        # A delete command that reports only n=1 does not prove which record
        # was removed, so the effect stays unknown.
        self.assertEqual(BulkWriteOutcomeStatus.ACKNOWLEDGED, outcomes[2].status)
        self.assertIsNone(outcomes[2].definitely_changed)
        self.assertIsNone(outcomes[2].write_error)
        # An update command that only reports n/nModified also stays unknown.
        self.assertEqual(BulkWriteOutcomeStatus.ACKNOWLEDGED, outcomes[5].status)
        self.assertIsNone(outcomes[5].definitely_changed)

    def test_error_in_second_command_maps_to_its_own_operation(self):
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
            bulk.execute(WriteConcern(), None, "insert")

        outcomes = ctx.exception.operation_outcomes
        # The update run is split after two operations. The write error in the
        # second command names its first operation, input position 5.
        self.assertEqual(BulkWriteOutcomeStatus.ACKNOWLEDGED, outcomes[1].status)
        self.assertEqual(BulkWriteOutcomeStatus.ACKNOWLEDGED, outcomes[3].status)
        self.assertEqual(BulkWriteOutcomeStatus.WRITE_ERROR, outcomes[5].status)
        self.assertFalse(outcomes[5].definitely_changed)
        # The delete command ran normally.
        self.assertEqual(BulkWriteOutcomeStatus.ACKNOWLEDGED, outcomes[2].status)

    def test_ordered_stop_marks_remaining_operations_not_executed(self):
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
            bulk.execute(WriteConcern(), None, "insert")

        outcomes = ctx.exception.operation_outcomes
        # Ordered runs group consecutive inserts into one command. The first
        # operation succeeds, the second fails, and the rest of that command
        # are received but not executed by the server.
        self.assertEqual(BulkWriteOutcomeStatus.PROVEN, outcomes[0].status)
        self.assertEqual(BulkWriteOutcomeStatus.WRITE_ERROR, outcomes[1].status)
        self.assertEqual(BulkWriteOutcomeStatus.NOT_EXECUTED, outcomes[2].status)
        self.assertEqual(BulkWriteOutcomeStatus.NOT_EXECUTED, outcomes[3].status)
        self.assertFalse(outcomes[2].definitely_changed)
        self.assertFalse(outcomes[3].definitely_changed)

    def test_failure_before_send_is_not_sent(self):
        bulk = _make_bulk(_models())
        bulk._execute_batch = _raise_before_send  # type: ignore[method-assign]
        with self.assertRaises(ConnectionFailure) as ctx:
            bulk.execute(WriteConcern(), None, "insert")

        outcomes = ctx.exception.operation_outcomes
        self.assertEqual(6, len(outcomes))
        for outcome in outcomes:
            self.assertEqual(BulkWriteOutcomeStatus.NOT_SENT, outcome.status)
            self.assertFalse(outcome.definitely_changed)

    def test_failure_after_batch_built_is_unknown_not_sent(self):
        bulk = _make_bulk(_models())
        bulk._execute_batch = _raise_after_send  # type: ignore[method-assign]
        with self.assertRaises(ConnectionFailure) as ctx:
            bulk.execute(WriteConcern(), None, "insert")

        outcomes = ctx.exception.operation_outcomes
        # The first batch reached the wire, so its operations are unknown. The
        # update and delete commands were never built.
        self.assertEqual(BulkWriteOutcomeStatus.UNKNOWN, outcomes[0].status)
        self.assertEqual(BulkWriteOutcomeStatus.UNKNOWN, outcomes[4].status)
        self.assertIsNone(outcomes[0].definitely_changed)
        for index in (1, 2, 3, 5):
            self.assertEqual(BulkWriteOutcomeStatus.NOT_SENT, outcomes[index].status)

    def test_success_result_exposes_outcomes(self):
        bulk = _make_bulk(_models())
        bulk.replies = [
            {"ok": 1, "n": 2},
            {"ok": 1, "n": 2, "nModified": 2},
            {"ok": 1, "n": 1, "nModified": 1},
            {"ok": 1, "n": 1},
        ]
        raw = bulk.execute(WriteConcern(), None, "insert")
        result = BulkWriteResult(raw, True, bulk.operation_outcomes())

        outcomes = result.operation_outcomes
        self.assertEqual(6, len(outcomes))
        self.assertEqual(
            {0, 4},
            {o.index for o in outcomes if o.status is BulkWriteOutcomeStatus.PROVEN},
        )
        self.assertEqual(
            BulkWriteOutcomeStatus.ACKNOWLEDGED,
            outcomes[2].status,
        )
        # Existing aggregate result is unchanged.
        self.assertEqual(2, result.inserted_count)
        self.assertEqual(1, result.deleted_count)
        self.assertEqual(3, result.matched_count)

    def test_existing_aggregate_result_is_unchanged(self):
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
            bulk.execute(WriteConcern(), None, "insert")

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

    def test_unacknowledged_result_hides_outcomes(self):
        result = BulkWriteResult({}, False)
        with self.assertRaisesRegex(InvalidOperation, "not available when"):
            result.operation_outcomes

    def test_outcome_repr_is_stable(self):
        outcome = BulkWriteOperationOutcome(3, "update")
        self.assertEqual(
            "BulkWriteOperationOutcome(index=3, operation_type='update', status='not_sent')",
            repr(outcome),
        )


def _raise_before_send(*args, **kwargs):
    raise ConnectionFailure("connection dropped before send")


def _raise_after_send(bwc, cmd, ops, client, on_send=None):
    batch = list(ops)[:2]
    if on_send is not None:
        on_send(len(batch))
    raise ConnectionFailure("connection dropped after send")


if __name__ == "__main__":
    unittest.main()
