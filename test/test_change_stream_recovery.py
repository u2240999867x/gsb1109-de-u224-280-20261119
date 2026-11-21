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

"""Test durable change stream recovery points without a server."""

from __future__ import annotations

import json
import sys
from collections import deque
from typing import Any

sys.path[0:0] = [""]

from bson import SON
from pymongo.change_stream_recovery import ChangeStreamRecoveryPoint
from pymongo.errors import InvalidOperation
from pymongo.synchronous.change_stream import ChangeStream
from test import UnitTest

_IS_SYNC = True


class _StubCursor:
    """The parts of CommandCursor that ChangeStream.try_next uses."""

    alive = True

    def __init__(self, batches: list[dict[str, Any]]) -> None:
        self._batches = deque(batches)
        self._changes: deque[dict[str, Any]] = deque()
        self._postbatchresumetoken: Any = None

    @property
    def _post_batch_resume_token(self) -> Any:
        return self._postbatchresumetoken

    def _has_next(self) -> bool:
        return bool(self._changes)

    def _try_next(self, get_more_allowed: bool) -> dict[str, Any] | None:
        if not self._changes:
            if not self._batches:
                return None
            batch = self._batches.popleft()
            self._changes.extend(batch.get("changes", []))
            self._postbatchresumetoken = batch.get("postBatchResumeToken")
            if not self._changes:
                return None
        return self._changes.popleft()

    def close(self) -> None:
        self.alive = False


def _new_change_stream(batches: list[dict[str, Any]], resume_after: Any = None) -> ChangeStream:
    stream = ChangeStream.__new__(ChangeStream)
    stream._closed = False
    stream._cursor = _StubCursor(batches)
    stream._resume_token = resume_after
    stream._last_event_token = None
    stream._uses_start_after = False
    stream._uses_resume_after = resume_after is not None
    stream._start_at_operation_time = None
    stream._decode_custom = False
    stream._timeout = None
    return stream


def _change(token: str) -> dict[str, Any]:
    return SON([("_id", SON([("_data", token)])), ("operationType", "insert")])


class FakeArchive:
    """An idempotent archive keyed by each event's own resume token."""

    def __init__(self) -> None:
        self.records: list[tuple[Any, dict[str, Any]]] = []
        self.writes: list[Any] = []
        self.confirmed: list[Any] = []

    def put(self, change: dict[str, Any]) -> Any:
        token = change["_id"]
        self.writes.append(token)
        if not any(record_token == token for record_token, _ in self.records):
            self.records.append((token, change))
        return token

    def confirm(self, token: Any) -> bool:
        exists = any(record_token == token for record_token, _ in self.records)
        if exists and not any(confirmed == token for confirmed in self.confirmed):
            self.confirmed.append(token)
        return exists

    def count(self, token: Any) -> int:
        return sum(1 for record_token, _ in self.records if record_token == token)

    def confirmed_count(self, token: Any) -> int:
        return sum(1 for confirmed in self.confirmed if confirmed == token)

    def all_tokens(self) -> list[Any]:
        return [token for token, _ in self.records]


class TestChangeStreamTokenIdentity(UnitTest):
    """Drive the real change stream token logic with a controlled cursor."""

    def test_last_event_token_differs_from_batch_token(self) -> None:
        token_a = _change("A")["_id"]
        token_b = _change("B")["_id"]
        token_p = SON([("_data", "P")])
        stream = _new_change_stream(
            [{"changes": [_change("A"), _change("B")], "postBatchResumeToken": token_p}]
        )

        change_a = stream.try_next()
        assert change_a is not None
        self.assertEqual(stream.last_event_token, token_a)
        # A is not the last event, so the public token is its own _id.
        self.assertEqual(stream.resume_token, token_a)

        change_b = stream.try_next()
        assert change_b is not None
        self.assertEqual(stream.last_event_token, token_b)
        # The public token is already the batch token even though B was just
        # returned.
        self.assertEqual(stream.resume_token, token_p)

        self.assertIsNone(stream.try_next())
        self.assertEqual(stream.last_event_token, token_b)
        self.assertEqual(stream.resume_token, token_p)
        self.assertEqual(stream.post_batch_resume_token, token_p)

    def test_empty_batch_advances_public_token_only(self) -> None:
        token_b = _change("B")["_id"]
        token_p = SON([("_data", "P")])
        token_q = SON([("_data", "Q")])
        stream = _new_change_stream(
            [
                {"changes": [_change("B")], "postBatchResumeToken": token_p},
                {"changes": [], "postBatchResumeToken": token_q},
            ]
        )

        change = stream.try_next()
        assert change is not None
        self.assertEqual(stream.last_event_token, token_b)
        self.assertEqual(stream.resume_token, token_p)

        self.assertIsNone(stream.try_next())
        self.assertEqual(stream.last_event_token, token_b)
        self.assertEqual(stream.resume_token, token_q)
        self.assertEqual(stream.post_batch_resume_token, token_q)


class TestChangeStreamRecoveryPoint(UnitTest):
    def test_out_of_order_receipt_and_restart(self) -> None:
        token_a = _change("A")["_id"]
        token_b = _change("B")["_id"]
        token_p_zero = SON([("_data", "P0")])
        token_p = SON([("_data", "P")])
        token_q = SON([("_data", "Q")])
        initial = SON([("_data", "initial")])
        batches = [
            {
                "changes": [_change("A"), _change("B")],
                "postBatchResumeToken": token_p_zero,
            },
            {"changes": [], "postBatchResumeToken": token_p},
        ]

        archive = FakeArchive()
        point = ChangeStreamRecoveryPoint(initial)
        persisted = point.to_document()

        stream = _new_change_stream(batches, resume_after=point.resume_token)
        self.assertEqual(stream.resume_token, initial)

        change_a = stream.try_next()
        assert change_a is not None
        point.observe_change(change_a)
        change_b = stream.try_next()
        assert change_b is not None
        point.observe_change(change_b)
        # The last event in the batch already exposes the batch token.
        self.assertEqual(stream.last_event_token, token_b)
        self.assertEqual(stream.resume_token, token_p_zero)

        # Only B reaches the archive before the crash. Its receipt has not
        # been recorded in the recovery point, so B is still pending.
        archive.put(change_b)
        point.observe_batch_token(stream.post_batch_resume_token)

        # Before the crash the archive has B and not A.
        self.assertEqual(archive.count(token_a), 0)
        self.assertEqual(archive.count(token_b), 1)
        self.assertEqual(archive.all_tokens(), [token_b])

        # The next empty poll sees P, which is after both A and B.
        self.assertIsNone(stream.try_next())
        self.assertEqual(stream.post_batch_resume_token, token_p)
        point.observe_batch_token(stream.post_batch_resume_token)

        # Neither A nor B is acknowledged, so B and P are not safe recovery
        # points.
        self.assertEqual(point.pending_tokens, [token_a, token_b])
        self.assertEqual(point.resume_token, initial)
        persisted = point.to_document()

        # Restart from the persisted document.
        restarted = ChangeStreamRecoveryPoint.from_document(persisted)
        self.assertEqual(restarted.resume_token, initial)
        self.assertEqual(restarted.pending_tokens, [token_a, token_b])

        # Look up each pending event's own token in the archive. B's
        # pre-crash receipt is confirmed without another write.
        self.assertEqual(restarted.reconcile(archive.confirm), [token_b])
        self.assertEqual(restarted.pending_tokens, [token_a])
        self.assertEqual(archive.count(token_b), 1)

        replay_stream = _new_change_stream(batches, resume_after=restarted.resume_token)
        # A restart before A is confirmed must resume before A, not after B
        # or P.
        self.assertEqual(replay_stream.resume_token, initial)

        replayed_a = replay_stream.try_next()
        assert replayed_a is not None
        restarted.observe_change(replayed_a)
        self.assertTrue(restarted.is_pending(token_a))

        replayed_b = replay_stream.try_next()
        assert replayed_b is not None
        restarted.observe_change(replayed_b)
        # B was already confirmed by reconcile, so it is not rewritten.
        self.assertFalse(restarted.is_pending(token_b))

        # Releasing A's receipt confirms A exactly once. Only now, with both
        # events durably confirmed, does the seen batch token P become the
        # recovery point.
        self.assertEqual(point.resume_token, initial)
        archive.put(replayed_a)
        self.assertTrue(archive.confirm(token_a))
        restarted.acknowledge(token_a)
        self.assertEqual(restarted.resume_token, token_p)

        self.assertIsNone(replay_stream.try_next())
        restarted.observe_batch_token(replay_stream.post_batch_resume_token)

        self.assertFalse(restarted.has_pending)
        self.assertEqual(restarted.resume_token, token_p)
        self.assertEqual(archive.count(token_a), 1)
        self.assertEqual(archive.count(token_b), 1)
        self.assertEqual(archive.confirmed_count(token_a), 1)
        self.assertEqual(archive.confirmed_count(token_b), 1)

        # The next restart may resume from P and must not replay A or B.
        after_p = ChangeStreamRecoveryPoint.from_document(restarted.to_document())
        self.assertEqual(after_p.resume_token, token_p)
        empty_stream = _new_change_stream(
            [{"changes": [], "postBatchResumeToken": token_q}],
            resume_after=after_p.resume_token,
        )
        self.assertEqual(empty_stream.resume_token, token_p)
        self.assertIsNone(empty_stream.try_next())
        after_p.observe_batch_token(empty_stream.post_batch_resume_token)
        self.assertEqual(after_p.resume_token, token_q)
        self.assertEqual(archive.count(token_a), 1)
        self.assertEqual(archive.count(token_b), 1)

    def test_observe_poll_uses_event_and_batch_tokens_separately(self) -> None:
        token_b = _change("B")["_id"]
        token_p = SON([("_data", "P")])
        token_q = SON([("_data", "Q")])
        stream = _new_change_stream(
            [
                {"changes": [_change("B")], "postBatchResumeToken": token_p},
                {"changes": [], "postBatchResumeToken": token_q},
            ]
        )
        point = ChangeStreamRecoveryPoint()
        point.observe_poll(stream.try_next(), stream)
        # The last event's public token is P, but its identity is B.
        self.assertEqual(point.pending_tokens, [token_b])
        self.assertEqual(point.resume_token, None)
        point.observe_poll(stream.try_next(), stream)
        self.assertEqual(point.pending_tokens, [token_b])

        point.acknowledge(token_b)
        self.assertEqual(point.resume_token, token_q)

    def test_acknowledge_advances_only_over_acknowledged_prefix(self) -> None:
        token_a = _change("A")["_id"]
        token_b = _change("B")["_id"]
        token_c = _change("C")["_id"]
        point = ChangeStreamRecoveryPoint()

        for change in (_change("A"), _change("B"), _change("C")):
            point.observe_change(change)
        point.acknowledge(token_b)
        self.assertEqual(point.resume_token, None)
        self.assertEqual(point.pending_tokens, [token_a, token_c])

        point.acknowledge(token_a)
        self.assertEqual(point.resume_token, token_b)
        self.assertEqual(point.pending_tokens, [token_c])

        point.acknowledge(token_c)
        self.assertEqual(point.resume_token, token_c)
        self.assertFalse(point.has_pending)

    def test_batch_token_waits_for_all_prior_events(self) -> None:
        token_a = _change("A")["_id"]
        token_b = _change("B")["_id"]
        token_p = SON([("_data", "P")])
        point = ChangeStreamRecoveryPoint()

        point.observe_change(_change("A"))
        point.observe_change(_change("B"))
        point.observe_batch_token(token_p)
        self.assertEqual(point.resume_token, None)

        point.acknowledge(token_b)
        self.assertEqual(point.resume_token, None)
        point.acknowledge(token_a)
        self.assertEqual(point.resume_token, token_p)

    def test_is_pending_distinguishes_unconfirmed_events(self) -> None:
        token_a = _change("A")["_id"]
        token_b = _change("B")["_id"]
        point = ChangeStreamRecoveryPoint()
        point.observe_change(_change("A"))
        point.observe_change(_change("B"))
        self.assertTrue(point.is_pending(token_a))
        self.assertTrue(point.is_pending(token_b))
        point.acknowledge(token_b)
        self.assertTrue(point.is_pending(token_a))
        self.assertFalse(point.is_pending(token_b))
        self.assertFalse(point.is_pending(SON([("_data", "unknown")])))

    def test_duplicate_observations_and_receipts_are_idempotent(self) -> None:
        token_a = _change("A")["_id"]
        point = ChangeStreamRecoveryPoint()
        point.observe_change(_change("A"))
        point.observe_change(_change("A"))
        point.observe_change(_change("A"))
        self.assertEqual(point.pending_tokens, [token_a])
        point.acknowledge(token_a)
        point.acknowledge(token_a)
        self.assertEqual(point.resume_token, token_a)
        self.assertFalse(point.has_pending)

    def test_reconcile_confirms_records_written_before_the_crash(self) -> None:
        token_a = _change("A")["_id"]
        token_b = _change("B")["_id"]
        token_p = SON([("_data", "P")])
        initial = SON([("_data", "initial")])
        point = ChangeStreamRecoveryPoint(initial)
        for change in (_change("A"), _change("B")):
            point.observe_change(change)
        point.observe_batch_token(token_p)
        # Only B's record survived the crash.
        archive = FakeArchive()
        archive.put(_change("B"))

        checked = []

        def has_record(token: Any) -> bool:
            checked.append(token)
            return archive.confirm(token)

        acknowledged = point.reconcile(has_record)
        self.assertEqual(checked, [token_a, token_b])
        self.assertEqual(acknowledged, [token_b])
        self.assertEqual(point.pending_tokens, [token_a])
        self.assertEqual(point.resume_token, initial)

        # Confirming A advances to the batch token observed after both events.
        archive.put(_change("A"))
        self.assertEqual(point.reconcile(has_record), [token_a])
        self.assertEqual(point.resume_token, token_p)

    def test_resume_token_returns_a_copy(self) -> None:
        token_a = _change("A")["_id"]
        point = ChangeStreamRecoveryPoint()
        point.observe_change(_change("A"))
        point.acknowledge(token_a)
        resume_token = point.resume_token
        assert resume_token is not None
        resume_token["_data"] = "tampered"
        self.assertEqual(point.resume_token, token_a)

    def test_document_round_trip_through_json(self) -> None:
        token_a = _change("A")["_id"]
        token_p = SON([("_data", "P")])
        point = ChangeStreamRecoveryPoint(SON([("_data", "initial")]))
        point.observe_change(_change("A"))
        point.observe_batch_token(token_p)
        point.acknowledge(token_a)

        document = json.loads(json.dumps(point.to_document()))
        restored = ChangeStreamRecoveryPoint.from_document(document)
        self.assertEqual(restored.resume_token, token_p)
        self.assertFalse(restored.has_pending)

    def test_invalid_documents(self) -> None:
        with self.assertRaises(TypeError):
            ChangeStreamRecoveryPoint([])  # type: ignore[arg-type]
        with self.assertRaises(TypeError):
            ChangeStreamRecoveryPoint.from_document([])  # type: ignore[arg-type]
        with self.assertRaises(ValueError):
            ChangeStreamRecoveryPoint.from_document({})
        with self.assertRaises(ValueError):
            ChangeStreamRecoveryPoint.from_document({"resume_token": "not-a-token"})
        with self.assertRaises(ValueError):
            ChangeStreamRecoveryPoint.from_document(
                {"resume_token": None, "observations": "not-a-list"}
            )
        with self.assertRaises(ValueError):
            ChangeStreamRecoveryPoint.from_document(
                {"resume_token": None, "observations": [{"token": None, "type": "event"}]}
            )
        with self.assertRaises(ValueError):
            ChangeStreamRecoveryPoint.from_document(
                {
                    "resume_token": None,
                    "observations": [
                        {"token": {"_data": "A"}, "type": "unknown", "acknowledged": False}
                    ],
                }
            )

    def test_observe_change_requires_an_id(self) -> None:
        point = ChangeStreamRecoveryPoint()
        with self.assertRaises(InvalidOperation):
            point.observe_change({"operationType": "insert"})
