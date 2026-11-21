"""Fixed change stream timeline and scripted server semantics for the repro.

The responder does not special case individual driver calls. It executes one
fixed event timeline against whatever watch target and pipeline filter the
driver actually sent, exactly like a real server would. The only scenario
controls are the visible time horizon and whether the network is up.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from repro.change_stream_rename.wire import Request

DB = "shop"
OLD_COLLECTION = "orders"
NEW_COLLECTION = "orders_v2"

INVALIDATE_TOKEN: dict[str, Any] = {"_data": "invalidate:10", "pos": 10}
FIRST_EVENT_TOKEN: dict[str, Any] = {"_data": "event:20", "pos": 20}
BATCH_TOKEN_P: dict[str, Any] = {"_data": "batch:25", "pos": 25}
SECOND_EVENT_TOKEN: dict[str, Any] = {"_data": "event:30", "pos": 30}

BEFORE_PAUSE_HORIZON = 25
AFTER_RESTORE_HORIZON = 40


@dataclass(frozen=True)
class ChangeEvent:
    pos: int
    token: dict[str, Any]
    collection: str
    operation_type: str
    full_document: dict[str, Any] | None = None
    before_pause: bool = True

    @property
    def namespace(self) -> dict[str, str]:
        return {"db": DB, "coll": self.collection}

    def document(self) -> dict[str, Any]:
        doc: dict[str, Any] = {
            "_id": copy.deepcopy(self.token),
            "operationType": self.operation_type,
            "ns": self.namespace,
        }
        if self.full_document is not None:
            doc["fullDocument"] = copy.deepcopy(self.full_document)
        return doc


EVENTS: tuple[ChangeEvent, ...] = (
    ChangeEvent(
        pos=10,
        token=INVALIDATE_TOKEN,
        collection=OLD_COLLECTION,
        operation_type="invalidate",
    ),
    ChangeEvent(
        pos=20,
        token=FIRST_EVENT_TOKEN,
        collection=NEW_COLLECTION,
        operation_type="insert",
        full_document={"_id": 1, "order": "A-1001", "state": "new"},
    ),
    ChangeEvent(
        pos=30,
        token=SECOND_EVENT_TOKEN,
        collection=NEW_COLLECTION,
        operation_type="insert",
        full_document={"_id": 2, "order": "A-1002", "state": "new"},
        before_pause=False,
    ),
)


@dataclass
class CommandRecord:
    """One aggregate or getMore sent by the driver, as observed on the wire."""

    phase: str
    kind: str
    collection: str | None
    filter: list[dict[str, Any]]
    resume_parameter: str | None = None
    resume_token: dict[str, Any] | None = None
    cursor_id: int | None = None
    post_batch_resume_token: dict[str, Any] | None = None
    first_batch: list[dict[str, Any]] = field(default_factory=list)
    next_batch: list[dict[str, Any]] = field(default_factory=list)
    error_code: int | None = None


@dataclass
class _Cursor:
    cursor_id: int
    collection: str | None
    filter: list[dict[str, Any]]
    phase: str
    pos: int
    drop_after_empty: bool = False
    dropped: bool = False
    expired: bool = False

    @property
    def namespace(self) -> str:
        coll = self.collection if self.collection is not None else "$cmd.aggregate"
        return f"{DB}.{coll}"


class ChangeStreamResponder:
    """Server-side double for ``$changeStream`` aggregate and getMore."""

    def __init__(self) -> None:
        self.network_up = True
        self.phase = "setup"
        self.horizon = BEFORE_PAUSE_HORIZON
        self.records: list[CommandRecord] = []
        self._cursors: dict[int, _Cursor] = {}
        self._next_cursor_id = 101

    def restore_network(self) -> None:
        """Bring the network back and expose the post-pause event horizon."""
        self.network_up = True
        self.phase = "resume"
        self.horizon = AFTER_RESTORE_HORIZON

    def arm_connection_drop(self) -> None:
        """Drop the connection after the next empty batch is delivered.

        Both pre-pause streams are armed before either reads its empty batch,
        so the two empty rounds contain identical timing from the clients'
        point of view.
        """
        for cursor in self._cursors.values():
            if cursor.phase == "setup":
                cursor.drop_after_empty = True

    def handle(self, request: Request) -> dict[str, Any] | None:
        if request.name == "aggregate":
            return self._aggregate(request)
        if request.name == "getMore":
            return self._get_more(request)
        if request.name in ("endSessions", "killCursors"):
            return {"ok": 1}
        return {"ok": 1}

    def _aggregate(self, request: Request) -> dict[str, Any]:
        command = request.command
        pipeline = command.get("pipeline", [])
        change_stream = pipeline[0]["$changeStream"] if pipeline else {}
        target = command["aggregate"]
        collection = target if isinstance(target, str) else None
        filters = copy.deepcopy(pipeline[1:])
        resume_parameter = None
        resume_token = None
        if "resumeAfter" in change_stream:
            resume_parameter = "resumeAfter"
            resume_token = change_stream["resumeAfter"]
        elif "startAfter" in change_stream:
            resume_parameter = "startAfter"
            resume_token = change_stream["startAfter"]

        if not self.network_up:
            request.connection.drop()
            return None

        if resume_parameter == "resumeAfter" and resume_token == INVALIDATE_TOKEN:
            record = CommandRecord(
                phase=self.phase,
                kind="aggregate",
                collection=collection,
                filter=filters,
                resume_parameter=resume_parameter,
                resume_token=copy.deepcopy(resume_token),
                error_code=280,
            )
            self.records.append(record)
            return {
                "ok": 0,
                "errmsg": "cannot resume with resumeAfter after an invalidate event",
                "code": 280,
                "codeName": "ChangeStreamFatalError",
                "errorLabels": ["NonResumableChangeStreamError"],
            }

        try:
            start_pos = _token_position(resume_token)
        except ValueError:
            record = CommandRecord(
                phase=self.phase,
                kind="aggregate",
                collection=collection,
                filter=filters,
                resume_parameter=resume_parameter,
                resume_token=copy.deepcopy(resume_token),
                error_code=280,
            )
            self.records.append(record)
            return {
                "ok": 0,
                "errmsg": f"unknown resume token {resume_token!r}",
                "code": 280,
                "codeName": "ChangeStreamFatalError",
                "errorLabels": ["NonResumableChangeStreamError"],
            }

        events = self._select(collection, filters, start_pos)
        first_batch = [event.document() for event in events[:1]]
        last_pos = events[0].pos if first_batch else max(start_pos, self.horizon)

        cursor_id = self._next_cursor_id
        self._next_cursor_id += 1
        cursor = _Cursor(
            cursor_id=cursor_id,
            collection=collection,
            filter=filters,
            phase=self.phase,
            pos=last_pos,
        )
        self._cursors[cursor_id] = cursor

        record = CommandRecord(
            phase=self.phase,
            kind="aggregate",
            collection=collection,
            filter=filters,
            resume_parameter=resume_parameter,
            resume_token=copy.deepcopy(resume_token),
            cursor_id=cursor_id,
            first_batch=first_batch,
        )
        self.records.append(record)

        # A real server does not send postBatchResumeToken on a non-empty
        # firstBatch, and it leaves it out of the initial aggregate when the
        # stream still has a cursor to poll. That forces the first empty result
        # to come from getMore, which is the round this repro pauses on.
        cursor_doc: dict[str, Any] = {
            "id": cursor_id,
            "ns": cursor.namespace,
            "firstBatch": first_batch,
        }
        return {"ok": 1, "cursor": cursor_doc, "operationTime": _operation_time()}

    def _get_more(self, request: Request) -> dict[str, Any]:
        command = request.command
        cursor_id = command["getMore"]
        cursor = self._cursors.get(cursor_id)
        if cursor is None or cursor.expired:
            record = CommandRecord(
                phase=self.phase,
                kind="getMore",
                collection=None,
                filter=[],
                cursor_id=cursor_id,
                error_code=43,
            )
            self.records.append(record)
            return {
                "ok": 0,
                "errmsg": f"cursor {cursor_id} not found",
                "code": 43,
                "codeName": "CursorNotFound",
                "errorLabels": ["ResumableChangeStreamError"],
            }

        events = self._select(cursor.collection, cursor.filter, cursor.pos)
        next_batch = [event.document() for event in events[:1]]
        post_batch_token = None
        if next_batch:
            cursor.pos = events[0].pos
        else:
            cursor.pos = max(cursor.pos, self.horizon)
            post_batch_token = copy.deepcopy(_batch_token_at(self.horizon))

        record = CommandRecord(
            phase=self.phase,
            kind="getMore",
            collection=cursor.collection,
            filter=copy.deepcopy(cursor.filter),
            cursor_id=cursor_id,
            post_batch_resume_token=copy.deepcopy(post_batch_token),
            next_batch=next_batch,
        )
        self.records.append(record)

        if not next_batch and cursor.drop_after_empty and not cursor.dropped:
            # The empty batch must reach the client before the socket dies, so
            # the application observes the same empty result and the same
            # batch token P with a still-live cursor. The cursor itself dies
            # with the interrupted connection: the next getMore after the
            # reconnect gets CursorNotFound, which drives the driver's real
            # automatic re-aggregation path.
            cursor.dropped = True
            cursor.expired = True
            request.connection.drop_after_reply = True

        return {
            "ok": 1,
            "cursor": {
                "id": cursor_id,
                "ns": cursor.namespace,
                "nextBatch": next_batch,
                "postBatchResumeToken": post_batch_token,
            },
        }

    def _select(
        self,
        collection: str | None,
        filters: list[dict[str, Any]],
        after_pos: int,
    ) -> list[ChangeEvent]:
        selected = []
        for event in EVENTS:
            if event.pos <= after_pos or event.pos > self.horizon:
                continue
            if not event.before_pause and self.phase != "resume":
                continue
            if not self._watch_matches(collection, filters, event):
                continue
            selected.append(event)
        return selected

    def _watch_matches(
        self,
        collection: str | None,
        filters: list[dict[str, Any]],
        event: ChangeEvent,
    ) -> bool:
        if collection is not None and event.collection != collection:
            return False
        return _matches_filters(event, filters)


def _token_position(token: dict[str, Any] | None) -> int:
    if token is None:
        return 0
    pos = token.get("pos")
    if not isinstance(pos, int):
        raise ValueError(f"resume token has no position: {token!r}")
    return pos


def _batch_token_at(horizon: int) -> dict[str, Any]:
    if horizon <= BEFORE_PAUSE_HORIZON:
        return BATCH_TOKEN_P
    return {"_data": f"batch:{horizon}", "pos": horizon}


def _operation_time() -> Any:
    from bson.timestamp import Timestamp

    return Timestamp(1, 1)


def _matches_filters(event: ChangeEvent, filters: list[dict[str, Any]]) -> bool:
    for stage in filters:
        if "$match" in stage:
            if not _matches(event, stage["$match"]):
                return False
    return True


def _matches(event: ChangeEvent, query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        if key == "$and":
            if not all(_matches(event, clause) for clause in expected):
                return False
        elif key == "$or":
            if not any(_matches(event, clause) for clause in expected):
                return False
        else:
            actual: Any = {
                "operationType": event.operation_type,
                "ns": event.namespace,
                "ns.db": DB,
                "ns.coll": event.collection,
            }.get(key)
            if key not in ("operationType", "ns", "ns.db", "ns.coll"):
                actual = _lookup(event.document(), key)
            if actual != expected:
                return False
    return True


def _lookup(document: dict[str, Any], dotted: str) -> Any:
    current: Any = document
    for part in dotted.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current
