"""End-to-end offline repro for a collection rename and an archived duty officer.

The duty officer watches the old ``shop.orders`` collection, receives the
rename-triggered ``invalidate``, then sees the application resume twice for the
new ``shop.orders_v2`` collection:

1. an intentionally bad ``resumeAfter`` of the invalidate token (must fail), and
2. two legal resumes that both leave with the same empty-batch batch token,
   followed by a dropped connection:
   - one keeps the old collection name in its filter,
   - one uses the new collection name in its filter.

The second empty read looks identical on the old-filter path even though that
path silently loses the first event of the new collection. The script pauses
only after both attempts have read the same batch token, verifies both cursors
are still alive, then restores the network and records which parameter
(``startAfter``/``resumeAfter``) the automatic re-aggregation chooses and
whether the next new-collection event arrives.
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pymongo import monitoring
from pymongo.asynchronous.mongo_client import AsyncMongoClient
from pymongo.errors import OperationFailure
from repro.change_stream_rename import model
from repro.change_stream_rename.wire import ScriptedServer

URI = "mongodb://fakehost:27017/?directConnection=true"


class CommandRecorder(monitoring.CommandListener):
    """Record the driver-visible aggregate/getMore starting commands in order."""

    def __init__(self) -> None:
        self.names: list[str] = []

    def started(self, event: monitoring.CommandStartedEvent) -> None:
        if event.command_name in ("aggregate", "getMore"):
            self.names.append(event.command_name)

    def succeeded(self, event: monitoring.CommandSucceededEvent) -> None:
        pass

    def failed(self, event: monitoring.CommandFailedEvent) -> None:
        pass


@dataclass
class Attempt:
    label: str
    filter: list[dict[str, Any]]
    stream: Any
    batch_token: dict[str, Any] | None = None
    empty_getmore: model.CommandRecord | None = None
    cursor_alive_at_pause: bool = False
    seen_before_pause: list[dict[str, Any]] = field(default_factory=list)

    @property
    def resume_token_at_pause(self) -> dict[str, Any] | None:
        return copy.deepcopy(self.stream.resume_token)


@dataclass
class Report:
    invalidate_event: dict[str, Any]
    first_transition_resume_parameter: str
    first_transition_resume_token: dict[str, Any]
    attempts: list[Attempt]
    records: list[model.CommandRecord]
    resume_parameters: dict[str, str | None]
    resume_tokens: dict[str, dict[str, Any] | None]
    after_restore_events: dict[str, dict[str, Any] | None]
    pause_observations: dict[str, Any]
    command_listener_names: list[str]
    diagnosis: dict[str, Any]

    def to_json(self) -> str:
        return json.dumps(_report_dict(self), indent=2, sort_keys=True)


async def run_repro() -> Report:
    responder = model.ChangeStreamResponder()
    recorder = CommandRecorder()

    async with ScriptedServer(responder.handle):
        clients = [
            AsyncMongoClient(URI, event_listeners=[recorder], serverSelectionTimeoutMS=3000)
            for _ in range(3)
        ]
        try:
            return await _drive(*clients, responder=responder, recorder=recorder)
        finally:
            for client in clients:
                await client.close()


async def _drive(
    control_client: AsyncMongoClient,
    old_client: AsyncMongoClient,
    new_client: AsyncMongoClient,
    *,
    responder: model.ChangeStreamResponder,
    recorder: CommandRecorder,
) -> Report:
    old_collection = control_client[model.DB][model.OLD_COLLECTION]
    stream = await old_collection.watch()
    invalidate = await stream.next()
    assert invalidate["operationType"] == "invalidate"

    # First transition: deliberately resume with the invalidate token using
    # resumeAfter, which is invalid because the token belongs to the old
    # collection's invalidate event.
    first_error = None
    try:
        await old_collection.watch(resume_after=invalidate["_id"])
    except OperationFailure as exc:
        first_error = exc
    assert first_error is not None
    assert first_error.code == 280

    old_filter = [{"$match": {"ns.coll": model.OLD_COLLECTION}}]
    new_filter = [{"$match": {"ns.coll": model.NEW_COLLECTION}}]

    # Both legal recoveries resume after the invalidate with startAfter, which
    # is the only legal way to continue past an invalidate token. They differ
    # only in the application filter, and each uses its own client so the
    # outage cannot spill from one attempt to the other.
    old_stream = await old_client[model.DB][model.NEW_COLLECTION].watch(
        pipeline=old_filter, start_after=invalidate["_id"]
    )
    new_stream = await new_client[model.DB][model.NEW_COLLECTION].watch(
        pipeline=new_filter, start_after=invalidate["_id"]
    )

    old_attempt = Attempt("old-filter", old_filter, old_stream)
    new_attempt = Attempt("new-filter", new_filter, new_stream)
    attempts = (old_attempt, new_attempt)

    # Arm both connections to drop right after their next empty batch. This
    # makes the two empty rounds identical from the clients' point of view.
    responder.arm_connection_drop()

    # Read each stream until the application-visible resume token is P. The
    # old-filter stream sees no event at all; the new-filter stream sees the
    # first new-collection event before its empty batch.
    for attempt in attempts:
        seen = []
        for _ in range(5):
            change = await attempt.stream.try_next()
            if change is not None:
                seen.append(change)
            if attempt.stream.resume_token == model.BATCH_TOKEN_P:
                break
        else:
            raise AssertionError(f"{attempt.label} never reached batch token P")
        attempt.seen_before_pause = seen

    old_attempt.batch_token = copy.deepcopy(old_stream.resume_token)
    new_attempt.batch_token = copy.deepcopy(new_stream.resume_token)
    assert old_attempt.batch_token == model.BATCH_TOKEN_P
    assert new_attempt.batch_token == model.BATCH_TOKEN_P
    assert len(old_attempt.seen_before_pause) == 0
    assert [doc["operationType"] for doc in new_attempt.seen_before_pause] == ["insert"]
    assert new_attempt.seen_before_pause[0]["ns"]["coll"] == model.NEW_COLLECTION

    # Both cursors received the empty batch in the same round. The server has
    # already dropped both connections; the drivers have not noticed yet. Pause
    # here and verify both cursors remain alive and that the application-visible
    # resume token is exactly the same as the empty result's
    # postBatchResumeToken.
    pause_observations = {}
    for attempt in attempts:
        pause_observations[attempt.label] = {
            "cursor_alive": bool(attempt.stream.alive),
            "resume_token": attempt.resume_token_at_pause,
            "batch_token": copy.deepcopy(attempt.batch_token),
            "token_matches_empty_batch": attempt.resume_token_at_pause == attempt.batch_token,
            "events_seen_before_pause": [
                {"operationType": doc["operationType"], "ns": doc["ns"]}
                for doc in attempt.seen_before_pause
            ],
        }
    old_attempt.cursor_alive_at_pause = bool(old_stream.alive)
    new_attempt.cursor_alive_at_pause = bool(new_stream.alive)
    assert old_attempt.cursor_alive_at_pause
    assert new_attempt.cursor_alive_at_pause

    # Record the empty getMore records belonging to this paused round.
    for record in responder.records:
        if record.cursor_id is not None and record.kind == "getMore":
            if record.next_batch == [] and record.post_batch_resume_token == model.BATCH_TOKEN_P:
                attempt = old_attempt if record.filter == old_filter else new_attempt
                if attempt.empty_getmore is None:
                    attempt.empty_getmore = record
    assert old_attempt.empty_getmore is not None
    assert new_attempt.empty_getmore is not None

    # Restore the network. Each stream now drives its own automatic
    # re-aggregation through a single try_next().
    responder.restore_network()
    second_old, second_new = await asyncio.gather(
        old_stream.try_next(),
        new_stream.try_next(),
    )

    resume_parameters: dict[str, str | None] = {}
    resume_tokens: dict[str, dict[str, Any] | None] = {}
    for label in ("old-filter", "new-filter"):
        aggregates = [
            record
            for record in responder.records
            if record.phase == "resume"
            and record.kind == "aggregate"
            and record.collection == model.NEW_COLLECTION
            and record.filter == (old_filter if label == "old-filter" else new_filter)
        ]
        assert aggregates, f"no re-aggregation recorded for {label}"
        # The last aggregate for this filter is the automatic resume.
        record = aggregates[-1]
        resume_parameters[label] = record.resume_parameter
        resume_tokens[label] = copy.deepcopy(record.resume_token)

    # Both resumes carry the same P, but the parameter differs because the
    # driver's startAfter state depends on whether the stream observed any
    # change after the option was supplied. That difference is diagnostic.
    assert resume_tokens["old-filter"] == model.BATCH_TOKEN_P
    assert resume_tokens["new-filter"] == model.BATCH_TOKEN_P
    assert resume_parameters["old-filter"] == "startAfter"
    assert resume_parameters["new-filter"] == "resumeAfter"

    # The old filter resumes cleanly but silently loses the first new-collection
    # event. The new filter delivers the next new-collection event.
    assert second_old is None
    assert second_new is not None
    assert second_new["operationType"] == "insert"
    assert second_new["ns"]["coll"] == model.NEW_COLLECTION
    assert second_new["_id"] == model.SECOND_EVENT_TOKEN

    return Report(
        invalidate_event=invalidate,
        first_transition_resume_parameter="resumeAfter",
        first_transition_resume_token=copy.deepcopy(invalidate["_id"]),
        attempts=[old_attempt, new_attempt],
        records=responder.records,
        resume_parameters=resume_parameters,
        resume_tokens=resume_tokens,
        after_restore_events={
            "old-filter": second_old,
            "new-filter": second_new,
        },
        pause_observations=pause_observations,
        command_listener_names=recorder.names,
        diagnosis=_diagnose(responder.records, old_attempt, new_attempt),
    )


def _report_dict(report: Report) -> dict[str, Any]:
    return {
        "invalidate_event": {
            "operationType": report.invalidate_event["operationType"],
            "ns": report.invalidate_event["ns"],
            "_id": report.invalidate_event["_id"],
        },
        "first_transition": {
            "resume_parameter": report.first_transition_resume_parameter,
            "resume_token": report.first_transition_resume_token,
        },
        "attempts": [_attempt_dict(attempt) for attempt in report.attempts],
        "records": [_record_dict(record) for record in report.records],
        "resume_parameters": report.resume_parameters,
        "resume_tokens": report.resume_tokens,
        "after_restore_events": {
            label: (
                {
                    "operationType": doc["operationType"],
                    "ns": doc["ns"],
                    "_id": doc["_id"],
                }
                if doc is not None
                else None
            )
            for label, doc in report.after_restore_events.items()
        },
        "pause_observations": report.pause_observations,
        "command_listener_names": report.command_listener_names,
        "diagnosis": report.diagnosis,
    }


def _diagnose(
    records: list[model.CommandRecord],
    old_attempt: Attempt,
    new_attempt: Attempt,
) -> dict[str, Any]:
    """Classify each symptom independently, without relying on the others."""
    invalid_resume = next(
        record for record in records if record.kind == "aggregate" and record.error_code == 280
    )
    old_after_pause = _first_resume_event(records, old_attempt.filter)
    new_after_pause = _first_resume_event(records, new_attempt.filter)

    return {
        "token_legality": {
            "invalid_attempt": {
                "resume_parameter": invalid_resume.resume_parameter,
                "resume_token": invalid_resume.resume_token,
                "server_error_code": invalid_resume.error_code,
                "verdict": "illegal",
            },
            "legal_attempts": [
                {
                    "label": old_attempt.label,
                    "resume_parameter": "startAfter",
                    "resume_token": model.BATCH_TOKEN_P,
                    "verdict": "legal",
                },
                {
                    "label": new_attempt.label,
                    "resume_parameter": "resumeAfter",
                    "resume_token": model.BATCH_TOKEN_P,
                    "verdict": "legal",
                },
            ],
        },
        "filter_loss": {
            "old_filter": old_attempt.filter,
            "first_event_after_pause": old_after_pause,
            "new_filter_first_event_after_pause": new_after_pause,
            "lost_event": model.FIRST_EVENT_TOKEN if old_after_pause is None else None,
            "verdict": "silent-loss" if old_after_pause is None else "delivered",
        },
        "normal_empty_batch": {
            "post_batch_resume_token": model.BATCH_TOKEN_P,
            "old_filter_events_before_pause": len(old_attempt.seen_before_pause),
            "new_filter_events_before_pause": len(new_attempt.seen_before_pause),
            "received_before_drop": old_attempt.empty_getmore is not None
            and new_attempt.empty_getmore is not None,
            "verdict": (
                "normal"
                if old_attempt.empty_getmore is not None and new_attempt.empty_getmore is not None
                else "anomalous"
            ),
        },
        "why_single_signal_is_misleading": {
            "resume_token_only": (
                "Both legal attempts expose the identical P, so a token-only "
                "check calls the old-filter path healthy even though it never "
                "delivers the first new-collection event."
            ),
            "empty_read_only": (
                "Both attempts receive the same empty batch and the same P, so "
                "an empty-read-only check cannot distinguish a real gap from a "
                "normal poll."
            ),
            "before_after_first_event": (
                "The new-filter path delivered one event before the empty batch; "
                "the old-filter path delivered none. That before/after contrast, "
                "not P and not the empty batch, identifies the lost event."
            ),
        },
    }


def _first_resume_event(
    records: list[model.CommandRecord],
    filter_: list[dict[str, Any]],
) -> dict[str, Any] | None:
    for record in records:
        if record.phase != "resume" or record.filter != filter_:
            continue
        documents = record.first_batch or record.next_batch
        if documents:
            document = documents[0]
            return {
                "operationType": document["operationType"],
                "ns": document["ns"],
                "_id": document["_id"],
            }
    return None


def _attempt_dict(attempt: Attempt) -> dict[str, Any]:
    return {
        "label": attempt.label,
        "filter": attempt.filter,
        "batch_token": attempt.batch_token,
        "empty_getmore": (
            _record_dict(attempt.empty_getmore) if attempt.empty_getmore is not None else None
        ),
        "cursor_alive_at_pause": attempt.cursor_alive_at_pause,
        "seen_before_pause": [
            {"operationType": doc["operationType"], "ns": doc["ns"]}
            for doc in attempt.seen_before_pause
        ],
    }


def _record_dict(record: model.CommandRecord) -> dict[str, Any]:
    return {
        "phase": record.phase,
        "kind": record.kind,
        "collection": record.collection,
        "filter": record.filter,
        "resume_parameter": record.resume_parameter,
        "resume_token": record.resume_token,
        "cursor_id": record.cursor_id,
        "post_batch_resume_token": record.post_batch_resume_token,
        "first_batch": [doc.get("operationType") for doc in record.first_batch],
        "next_batch": [doc.get("operationType") for doc in record.next_batch],
        "error_code": record.error_code,
    }


def write_report(report: Report, path: Path | None = None) -> Path:
    out = path or Path(__file__).with_name("report.json")
    out.write_text(report.to_json() + "\n")
    return out
