# Copyright 2026-present MongoDB, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
# https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Durable recovery points for change stream consumers."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

from pymongo.errors import InvalidOperation

_ResumeToken = Mapping[str, Any]


@dataclass
class _Observation:
    """An event or batch resume token observed on a change stream."""

    token: _ResumeToken
    is_event: bool
    acknowledged: bool = False


def _validate_token(name: str, token: Any) -> None:
    if not isinstance(token, Mapping):
        raise TypeError(f"{name} must be a mapping, not {type(token).__name__}")


def _parse_token(name: str, token: Any) -> _ResumeToken:
    try:
        _validate_token(name, token)
    except TypeError as exc:
        raise ValueError(str(exc)) from exc
    return token


class ChangeStreamRecoveryPoint:
    """A durable recovery point for a change stream consumer.

    A change stream may return a batch of changes whose archive writes
    complete out of order. The change stream's :attr:`resume_token
    <pymongo.change_stream.ChangeStream.resume_token>` is also not sufficient
    as an event identity: after the last change in a batch is returned it may
    already be the batch's ``postBatchResumeToken``, and an empty poll updates
    it again. Advancing a durable recovery point to either value can skip an
    earlier event whose archive write has not been confirmed.

    This class tracks event tokens separately from batch tokens. Record every
    change as it is read with :meth:`observe`, record every empty poll's
    :attr:`post_batch_resume_token
    <pymongo.change_stream.ChangeStream.post_batch_resume_token>` with
    :meth:`observe_batch_token`, and record an archive receipt with
    :meth:`acknowledge`. The :attr:`resume_token` property never advances past
    an unacknowledged event. Once all observed events are acknowledged, and
    the batch token was observed after them, the batch token becomes the
    recovery point.

    Callers must call :meth:`observe` before :meth:`acknowledge` for a given
    event. An acknowledgement for an event that has not been observed is
    ignored; replaying an event is safe because observing an already pending
    event token does not enqueue it twice.

    Persist :meth:`to_document` after every state change. On restart, restore
    the point with :meth:`from_document` and pass its :attr:`resume_token` as
    ``resume_after`` or ``start_after``. Events archived before the restart
    may be replayed. The archive is expected to identify records by each
    event's own ``_id`` token and report an acknowledgement for a replay
    instead of creating a duplicate record.

    A recovery point can only guarantee at-least-once delivery when it starts
    from an existing checkpoint. Without one there is no token before the
    first observed event, so callers that need that guarantee must begin with
    a resume token or ``startAtOperationTime`` recorded before consuming
    changes.

    For example::

        point = ChangeStreamRecoveryPoint(saved_document_or_token)
        async with await collection.watch(
            resume_after=point.resume_token
        ) as stream:
            # Confirm receipts written before a restart before replaying.
            point.reconcile(archive.contains)
            while stream.alive:
                change = await stream.try_next()
                if change is None:
                    point.observe_batch_token(stream.post_batch_resume_token)
                else:
                    point.observe_change(change)
                    token = change["_id"]
                    if point.is_pending(token):
                        archive.put(change)
                    if archive.confirmed(token):
                        point.acknowledge(token)
                checkpoint.save(point.to_document())

    .. versionadded:: 4.21
    """

    def __init__(self, resume_token: _ResumeToken | None = None) -> None:
        """Initialize a recovery point.

        :param resume_token: The last durably acknowledged resume token, or
            ``None`` when the consumer has no checkpoint yet.
        """
        if resume_token is not None:
            _validate_token("resume_token", resume_token)
        self._resume_token = copy.deepcopy(resume_token)
        self._observations: list[_Observation] = []

    @property
    def resume_token(self) -> _ResumeToken | None:
        """The token to use when resuming the change stream.

        This token never advances past an event that has been observed but
        not acknowledged. It is safe to pass as ``resume_after`` or
        ``start_after``.

        The returned mapping is a copy and may be persisted directly.
        """
        return copy.deepcopy(self._resume_token)

    @property
    def recovery_token(self) -> _ResumeToken | None:
        """Alias of :attr:`resume_token`."""
        return self.resume_token

    @property
    def pending_tokens(self) -> list[_ResumeToken]:
        """Unacknowledged event tokens, in change stream order."""
        return [
            copy.deepcopy(observation.token)
            for observation in self._observations
            if observation.is_event and not observation.acknowledged
        ]

    def reconcile(self, has_record: Callable[[_ResumeToken], bool]) -> list[_ResumeToken]:
        """Acknowledge pending events that the archive already contains.

        Use this after a restart to confirm receipts that were written before
        the crash but not yet recorded in the recovery point. ``has_record``
        is called once per pending event, in order, with that event's own
        token; it must return ``True`` only if the archive durably stored a
        record for that token.

        :param has_record: A callback that looks up an event's own token in
            the archive.

        :return: The tokens that were acknowledged by this call.
        """
        acknowledged = []
        for observation in list(self._observations):
            if not observation.is_event or observation.acknowledged:
                continue
            if has_record(copy.deepcopy(observation.token)):
                observation.acknowledged = True
                acknowledged.append(copy.deepcopy(observation.token))
        self._compact()
        return acknowledged

    @property
    def has_pending(self) -> bool:
        """Whether an observed event is still awaiting acknowledgement."""
        return any(
            observation.is_event and not observation.acknowledged
            for observation in self._observations
        )

    def is_pending(self, event_token: _ResumeToken) -> bool:
        """Whether an event token is observed but not yet acknowledged.

        This can be used to skip an archive write when replaying a change
        whose receipt was already confirmed.

        :param event_token: The change document's ``_id`` value.
        """
        _validate_token("event_token", event_token)
        return any(
            observation.is_event
            and not observation.acknowledged
            and observation.token == event_token
            for observation in self._observations
        )

    def observe(self, event_token: _ResumeToken) -> None:
        """Record the token of a change read from the stream.

        Call this immediately after reading an event, before handing it to the
        archive writer. Observing a token that is already tracked is a no-op,
        which makes replay after a restart safe.

        :param event_token: The change document's ``_id`` value.
        """
        _validate_token("event_token", event_token)
        if any(
            observation.is_event and observation.token == event_token
            for observation in self._observations
        ):
            return
        self._observations.append(_Observation(copy.deepcopy(event_token), True))
        # Batch tokens observed before this event are now in the safe prefix
        # and can advance the recovery point; later ones cannot yet.
        self._compact()

    def observe_change(self, change: Mapping[str, Any]) -> _ResumeToken:
        """Record an event's own ``_id`` token and return it.

        This is a convenience wrapper around :meth:`observe`. It always uses
        the change document's ``_id`` and never the change stream's public
        ``resume_token``, which may be a batch token.

        :param change: A change document returned by the change stream.

        Raises :exc:`~pymongo.errors.InvalidOperation` if the change document
        has no ``_id``.
        """
        try:
            event_token = change["_id"]
        except (KeyError, TypeError):
            raise InvalidOperation(
                "Cannot record a recovery point for a change without an _id token"
            ) from None
        self.observe(event_token)
        return copy.deepcopy(event_token)

    def observe_poll(self, change: Mapping[str, Any] | None, stream: Any) -> None:
        """Record the result of one ``try_next`` call.

        Call this with the change returned by ``try_next`` (or ``None``) and
        the stream it came from. A change is recorded by its own ``_id``; an
        empty result records the stream's current ``postBatchResumeToken``.
        The stream's public ``resume_token`` is never used, so calling this
        after the last event of a batch does not mistake the batch token for
        the event's identity.

        :param change: The value returned by ``try_next``.
        :param stream: The change stream that produced ``change``. It must
            expose ``last_event_token`` and ``post_batch_resume_token``.
        """
        if change is None:
            self.observe_batch_token(stream.post_batch_resume_token)
        else:
            self.observe_change(change)

    def observe_batch_token(self, batch_token: _ResumeToken | None) -> None:
        """Record a ``postBatchResumeToken`` seen on the stream.

        The batch token is retained as a candidate recovery point, but it is
        not used until every event observed before it is acknowledged. Passing
        ``None`` is allowed and has no effect.

        :param batch_token: A change stream's ``postBatchResumeToken``.
        """
        if batch_token is None:
            return
        _validate_token("batch_token", batch_token)
        if (
            self._observations
            and not self._observations[-1].is_event
            and self._observations[-1].token == batch_token
        ):
            return
        self._observations.append(_Observation(copy.deepcopy(batch_token), False, True))
        self._compact()

    def acknowledge(self, event_token: _ResumeToken) -> None:
        """Record that the archive durably stored an event.

        Acknowledgements may arrive in any order. An event token that is not
        currently tracked is ignored, so duplicate receipts after a replay are
        harmless.

        :param event_token: The change document's ``_id`` value.
        """
        _validate_token("event_token", event_token)
        for observation in self._observations:
            if (
                observation.is_event
                and not observation.acknowledged
                and observation.token == event_token
            ):
                observation.acknowledged = True
                break
        self._compact()

    def _compact(self) -> None:
        """Advance the durable token over the acknowledged prefix."""
        first_unacknowledged = next(
            (
                index
                for index, observation in enumerate(self._observations)
                if observation.is_event and not observation.acknowledged
            ),
            None,
        )
        if first_unacknowledged is None:
            safe = self._observations
        else:
            safe = self._observations[:first_unacknowledged]
        if not safe:
            return
        self._resume_token = copy.deepcopy(safe[-1].token)
        del self._observations[: len(safe)]

    def to_document(self) -> dict[str, Any]:
        """Return a BSON-serializable document for persistence.

        Store this document after :meth:`observe`, :meth:`observe_batch_token`,
        and :meth:`acknowledge` calls so a restart resumes from the latest safe
        position. The document contains only resume tokens and acknowledgement
        flags; it does not contain archived change documents.
        """
        observations = [
            {
                "token": copy.deepcopy(observation.token),
                "type": "event" if observation.is_event else "batch",
                "acknowledged": observation.acknowledged,
            }
            for observation in self._observations
        ]
        return {
            "resume_token": copy.deepcopy(self._resume_token),
            "observations": observations,
        }

    @classmethod
    def from_document(cls, document: Mapping[str, Any]) -> ChangeStreamRecoveryPoint:
        """Restore a recovery point from :meth:`to_document` output.

        :param document: A document previously returned by :meth:`to_document`.

        Raises :exc:`TypeError` if the document is not a mapping and
        :exc:`ValueError` if it has an invalid shape.
        """
        if not isinstance(document, Mapping):
            raise TypeError(f"document must be a mapping, not {type(document).__name__}")
        if "resume_token" not in document:
            raise ValueError("document is missing 'resume_token'")
        resume_token = document["resume_token"]
        if resume_token is not None:
            _parse_token("resume_token", resume_token)
        point = cls(resume_token)
        observations = document.get("observations", [])
        if not isinstance(observations, list):
            raise ValueError("'observations' must be a list")
        for value in observations:
            if not isinstance(value, Mapping) or "token" not in value:
                raise ValueError("each observation must be a mapping with a 'token'")
            token = value["token"]
            _parse_token("token", token)
            kind = value.get("type")
            if kind not in ("event", "batch"):
                raise ValueError("observation 'type' must be 'event' or 'batch'")
            acknowledged = value.get("acknowledged")
            if not isinstance(acknowledged, bool):
                raise ValueError("observation 'acknowledged' must be a boolean")
            point._observations.append(
                _Observation(copy.deepcopy(token), kind == "event", acknowledged)
            )
        # Normalize documents written by older checkpoints. A document written
        # by to_document already begins with an unacknowledged event, so this
        # is a no-op for those.
        point._compact()
        return point

    def __repr__(self) -> str:
        return f"{type(self).__name__}(resume_token={self._resume_token!r})"
