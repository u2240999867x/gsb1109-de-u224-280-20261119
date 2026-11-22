#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/delivery.py
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are
#  met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following disclaimer
#    in the documentation and/or other materials provided with the
#    distribution.
#  * Neither the name of the project nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
#  A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
#  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
#  SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
#  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
#  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
#  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

from __future__ import annotations

import abc
import enum
from collections import deque
from collections.abc import Sized
from dataclasses import dataclass
from typing import Any, Callable, Deque, Dict, FrozenSet, Iterable, Iterator, Mapping, Optional, Tuple

__all__ = (
    'ConfirmationLost',
    'DeliveryOutcome',
    'DeliveryPipeline',
    'DeliveryRecord',
    'DeliverySink',
    'PumpResult',
    'SinkTerminated',
    'SinkUnavailable',
    'SourceFailed',
)


class DeliveryOutcome(enum.Enum):
    """The three mutually exclusive destinations an evaluated row can be routed to."""
    ACCEPTED = 'accepted'
    REJECTED = 'rejected'
    ERROR = 'error'


class DeliveryError(Exception):
    """Base class for the signals exchanged between the pipeline and its sinks."""


class SinkUnavailable(DeliveryError):
    """Raised by a sink to signal a transient rejection; the record was not written."""


class SinkTerminated(DeliveryError):
    """Raised by a sink to signal that it has stopped permanently."""


class ConfirmationLost(DeliveryError):
    """Raised after a record was written but its confirmation could not be returned."""


class SourceFailed(DeliveryError):
    """Raised internally when the source iterator itself fails.

    :param position: the source position that could not be produced.
    :param original: the exception raised by the iterator.
    """
    def __init__(self, position: int, original: BaseException):
        super().__init__('source iterator failed at position {0}: {1!r}'.format(position, original))
        self.position = position
        self.original = original


@dataclass(frozen=True)
class DeliveryRecord:
    """An evaluated row and everything needed to deliver it, including its stable identity."""
    identity: Tuple[DeliveryOutcome, int]
    branch: DeliveryOutcome
    source_position: int
    business_id: Any
    value: Any = None
    error: Optional[BaseException] = None


class DeliverySink(abc.ABC):
    """Abstract write endpoint for one delivery branch.

    Implementations must persist records under their stable :attr:`DeliveryRecord.identity`
    so that :meth:`is_delivered` can answer after a lost confirmation.
    """
    @abc.abstractmethod
    def deliver(self, record: DeliveryRecord) -> None:
        """Write *record*.

        Raise :class:`SinkUnavailable` for a transient rejection, :class:`ConfirmationLost`
        after the record was written but the confirmation was lost, or
        :class:`SinkTerminated` when the sink has stopped permanently.
        """

    @abc.abstractmethod
    def is_delivered(self, identity: Tuple[DeliveryOutcome, int]) -> bool:
        """Return whether a record with *identity* has already landed."""

    def confirm(self, record: DeliveryRecord) -> None:
        """Acknowledge a record that was found already delivered. The default is a no-op."""


@dataclass(frozen=True)
class PumpResult:
    """A snapshot of the pipeline state produced after a :meth:`DeliveryPipeline.pump` round."""
    delivered: Tuple[DeliveryRecord, ...]
    buffer_used: int
    buffer_size: int
    buffer_full: bool
    blocked: Mapping[DeliveryOutcome, DeliveryRecord]
    blocked_reasons: Mapping[DeliveryOutcome, str]
    unconfirmed: FrozenSet[Tuple[DeliveryOutcome, int]]
    terminated: FrozenSet[DeliveryOutcome]
    next_source_position: int
    source_exhausted: bool
    source_failure: Optional[SourceFailed]


# blockage reasons reported in PumpResult.blocked_reasons
_REASON_UNAVAILABLE = 'unavailable'
_REASON_UNCONFIRMED = 'unconfirmed'
_REASON_TERMINATED = 'terminated'


class DeliveryPipeline:
    """A buffered, forward-only fan-out pipeline with per-branch ordering.

    Rows are pulled from *source* (an iterable of ``(business_id, value)`` pairs) only while
    the shared buffer has a free reservation. Each row is evaluated exactly once by
    *resolver*, which returns a :class:`DeliveryOutcome` or raises (routing the row to the
    :data:`DeliveryOutcome.ERROR` branch). Evaluated rows keep their verdict forever and the
    resolver is never visited again for them.

    A transient sink rejection only blocks the head of that branch's queue; the other
    branches keep delivering. Buffering is bounded by *buffer_size* across all branches.
    """
    def __init__(
        self,
        source: Iterable[Tuple[Any, Any]],
        resolver: Callable[[Tuple[Any, Any]], DeliveryOutcome],
        sinks: Mapping[DeliveryOutcome, DeliverySink],
        *,
        buffer_size: int
    ):
        if buffer_size < 1:
            raise ValueError('buffer_size must be at least 1')
        missing = tuple(outcome for outcome in DeliveryOutcome if outcome not in sinks)
        if missing:
            raise ValueError('missing sinks for outcomes: {0}'.format(missing))
        self._source = source
        self._iterator: Iterator[Tuple[Any, Any]] = iter(source)
        self._resolver = resolver
        self._sinks: Dict[DeliveryOutcome, DeliverySink] = dict(sinks)
        self._buffer_size = buffer_size
        self._queues: Dict[DeliveryOutcome, Deque[DeliveryRecord]] = {
            outcome: deque() for outcome in DeliveryOutcome
        }
        self._unconfirmed: set[Tuple[DeliveryOutcome, int]] = set()
        self._terminated: set[DeliveryOutcome] = set()
        self._blocked_reasons: Dict[DeliveryOutcome, str] = {}
        self._next_position = 0
        self._exhausted = False
        self._source_failure: Optional[SourceFailed] = None
        self._resolver_calls: Dict[int, int] = {}
        self._delivered: Dict[DeliveryOutcome, list[DeliveryRecord]] = {
            outcome: [] for outcome in DeliveryOutcome
        }

    @property
    def buffer_size(self) -> int:
        return self._buffer_size

    @property
    def buffer_used(self) -> int:
        """Total reserved slots across every branch."""
        return sum(len(queue) for queue in self._queues.values())

    @property
    def buffer_full(self) -> bool:
        return self.buffer_used >= self._buffer_size

    @property
    def consumed_count(self) -> int:
        """Number of rows successfully pulled from the source."""
        return self._next_position

    @property
    def next_source_position(self) -> int:
        """Position of the row that would be pulled next, including when the buffer is full."""
        return self._next_position

    @property
    def source_exhausted(self) -> bool:
        return self._exhausted

    @property
    def source_failure(self) -> Optional[SourceFailed]:
        return self._source_failure

    @property
    def unconsumed_count(self) -> Optional[int]:
        """Rows still in a sized source, or ``None`` when the source has no known length."""
        try:
            if not isinstance(self._source, Sized):
                raise TypeError
            total = len(self._source)
        except TypeError:
            return None
        return total - self._next_position

    @property
    def terminated_branches(self) -> FrozenSet[DeliveryOutcome]:
        return frozenset(self._terminated)

    @property
    def unconfirmed_identities(self) -> FrozenSet[Tuple[DeliveryOutcome, int]]:
        return frozenset(self._unconfirmed)

    def resolver_call_count(self, source_position: int) -> int:
        """How often the resolver was visited for *source_position* (always 0 or 1)."""
        return self._resolver_calls.get(source_position, 0)

    @property
    def resolver_calls(self) -> Mapping[int, int]:
        return dict(self._resolver_calls)

    def pending_records(self, branch: Optional[DeliveryOutcome] = None) -> Tuple[DeliveryRecord, ...]:
        """Records still buffered, in branch then source order."""
        branches = (branch,) if branch is not None else tuple(DeliveryOutcome)
        return tuple(record for outcome in branches for record in self._queues[outcome])

    def delivered_records(self, branch: Optional[DeliveryOutcome] = None) -> Tuple[DeliveryRecord, ...]:
        """Records confirmed delivered, in the order each branch accepted them."""
        branches = (branch,) if branch is not None else tuple(DeliveryOutcome)
        return tuple(record for outcome in branches for record in self._delivered[outcome])

    def blocked_branches(self) -> Mapping[DeliveryOutcome, DeliveryRecord]:
        """The branch heads actually preventing further progress, mapped to their record."""
        return {
            outcome: self._queues[outcome][0]
            for outcome, reason in self._blocked_reasons.items()
            if self._queues[outcome]
        }

    def snapshot(self) -> PumpResult:
        return self._result(())

    def pump(self) -> PumpResult:
        """Run one scheduling round until no slot can be filled or freed in this round.

        Source rows are pulled into free reservations, then each non-terminated branch
        attempts its head record. Successful deliveries release slots, which lets the round
        pull again. The round ends when the buffer is full and every buffered head is
        blocked, the source is exhausted or failed, and no blocked head could move.
        """
        delivered: list[DeliveryRecord] = []
        # a blocked head is attempted at most once per round; external signals only change between pumps
        attempted: set[DeliveryOutcome] = set()
        progress = True
        while progress:
            progress = False
            while not self.buffer_full and self._can_pull():
                record = self._pull_next()
                if record is None:
                    break
                progress = True
            for branch in DeliveryOutcome:
                queue = self._queues[branch]
                if not queue or branch in self._terminated or branch in attempted:
                    continue
                attempted.add(branch)
                head = queue[0]
                sink = self._sinks[branch]
                if head.identity in self._unconfirmed:
                    # the record already landed once; never call deliver() again, only look it up
                    if sink.is_delivered(head.identity):
                        sink.confirm(head)
                        self._release_head(branch, delivered)
                        attempted.discard(branch)
                        progress = True
                    else:
                        self._blocked_reasons[branch] = _REASON_UNCONFIRMED
                    continue
                try:
                    sink.deliver(head)
                except SinkUnavailable:
                    self._blocked_reasons[branch] = _REASON_UNAVAILABLE
                    continue
                except ConfirmationLost:
                    self._unconfirmed.add(head.identity)
                    self._blocked_reasons[branch] = _REASON_UNCONFIRMED
                    continue
                except SinkTerminated:
                    self._terminated.add(branch)
                    self._blocked_reasons[branch] = _REASON_TERMINATED
                    continue
                self._release_head(branch, delivered)
                attempted.discard(branch)
                progress = True
        return self._result(tuple(delivered))

    def _can_pull(self) -> bool:
        return not self._exhausted and self._source_failure is None

    def _pull_next(self) -> Optional[DeliveryRecord]:
        position = self._next_position
        try:
            row = next(self._iterator)
        except StopIteration:
            self._exhausted = True
            return None
        except BaseException as exc:
            self._source_failure = SourceFailed(position, exc)
            return None
        self._next_position += 1
        self._resolver_calls[position] = self._resolver_calls.get(position, 0) + 1
        evaluation_error: Optional[BaseException] = None
        try:
            outcome = self._resolver(row)
        except BaseException as exc:
            outcome = DeliveryOutcome.ERROR
            evaluation_error = exc
        if not isinstance(outcome, DeliveryOutcome):
            raise TypeError('resolver returned {0!r}, expected a DeliveryOutcome'.format(outcome))
        business_id, value = row
        record = DeliveryRecord(
            identity=(outcome, position),
            branch=outcome,
            source_position=position,
            business_id=business_id,
            value=value,
            error=evaluation_error,
        )
        self._queues[outcome].append(record)
        return record

    def _release_head(
        self,
        branch: DeliveryOutcome,
        delivered: list[DeliveryRecord]
    ) -> None:
        record = self._queues[branch].popleft()
        self._unconfirmed.discard(record.identity)
        self._blocked_reasons.pop(branch, None)
        self._delivered[branch].append(record)
        delivered.append(record)

    def _result(self, delivered: Tuple[DeliveryRecord, ...]) -> PumpResult:
        blocked = self.blocked_branches()
        return PumpResult(
            delivered=delivered,
            buffer_used=self.buffer_used,
            buffer_size=self._buffer_size,
            buffer_full=self.buffer_full,
            blocked=dict(blocked),
            blocked_reasons={outcome: self._blocked_reasons[outcome] for outcome in blocked},
            unconfirmed=frozenset(self._unconfirmed),
            terminated=frozenset(self._terminated),
            next_source_position=self._next_position,
            source_exhausted=self._exhausted,
            source_failure=self._source_failure,
        )
