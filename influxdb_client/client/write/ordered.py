"""Order-preserving batching for the ``WriteApi``.

The default batching pipeline sends batches independently.  That is fine when
writes are independent, but it is not enough when a later batch *corrects* a
value written by an earlier batch to the same timestamp: the two requests must
reach InfluxDB in enqueue order.

This module implements an opt-in ordered batch writer.  Every
``(bucket, org, precision)`` target owns a FIFO queue and a single worker.  A
worker sends one batch at a time and does not release the next batch until the
previous one has been acknowledged with a definitive result.  Different
targets have different workers, so they can make progress in parallel.

If an attempt finishes without a definitive answer (for example a read timeout
after the request may have reached the server), the target is *blocked*.  The
batch that failed is reported as ``unconfirmed`` and all following batches stay
``unsent``.  The caller has to explicitly continue or abandon the target; the
writer never silently re-orders or re-sends queued batches to unblock itself.
"""

# coding: utf-8
import logging
import socket
import threading
import time
from collections import deque
from enum import Enum
from random import random
from typing import List, Optional

from urllib3.exceptions import HTTPError, MaxRetryError

from influxdb_client.client.exceptions import InfluxDBError

logger = logging.getLogger('influxdb_client.client.write.ordered')


class OrderedBatchStatus(str, Enum):
    """State of a batch managed by the ordered writer."""

    PENDING = 'pending'
    IN_FLIGHT = 'in_flight'
    UNSENT = 'unsent'
    UNCONFIRMED = 'unconfirmed'
    SENT = 'sent'
    FAILED = 'failed'
    ABANDONED = 'abandoned'

    def __str__(self) -> str:
        return self.value


class OrderedWriteError(InfluxDBError):
    """Base class for errors reported by the ordered writer."""

    def __init__(self, message: str = None, batch=None, cause: Exception = None):
        super().__init__(message=message)
        self.batch = batch
        self.cause = cause


class WriteResultUnknownError(OrderedWriteError):
    """The write may or may not have been applied by the server."""


class BatchNotSentError(OrderedWriteError):
    """A queued batch was never sent because the target was stopped."""


class OrderedBatch(object):
    """Immutable snapshot of a batch tracked by the ordered writer."""

    def __init__(self, key, data, size, status: OrderedBatchStatus,
                 exception: Exception = None, sequence: int = None) -> None:
        self.key = key
        self.data = data
        self.size = size
        self.status = status
        self.exception = exception
        self.sequence = sequence

    @property
    def bucket(self):
        """Target bucket."""
        return self.key.bucket

    @property
    def org(self):
        """Target organization."""
        return self.key.org

    @property
    def precision(self):
        """Target timestamp precision."""
        return self.key.precision

    def to_key_tuple(self):
        """Return ``(bucket, organization, precision)``."""
        return self.key.bucket, self.key.org, self.key.precision

    def __str__(self) -> str:
        return "OrderedBatch[key:'{}', status:'{}', size:'{}']" \
            .format(str(self.key), str(self.status), self.size)

    def __repr__(self) -> str:
        return self.__str__()


class _Record(object):
    """Mutable internal representation of a batch."""

    def __init__(self, key, data, size, sequence) -> None:
        self.key = key
        self.data = data
        self.size = size
        self.sequence = sequence
        self.status = OrderedBatchStatus.PENDING
        self.exception = None
        self.callback_fired = False
        # Set by close() when a result arrives after the writer stopped waiting.
        self.forced = False

    def to_public(self) -> OrderedBatch:
        return OrderedBatch(self.key, self.data, self.size, self.status,
                            self.exception, self.sequence)


class _Target(object):
    """Per ``(bucket, org, precision)`` ordering state."""

    def __init__(self, key) -> None:
        self.key = key
        self.pending: List[_Record] = []
        self.queue = deque()
        self.in_flight: Optional[_Record] = None
        self.unconfirmed: List[_Record] = []
        self.blocked = False
        self.stopped = False
        self.busy = False
        self.timer: Optional[threading.Timer] = None
        self.worker: Optional[threading.Thread] = None


def _status_of(exception: Exception):
    status = getattr(exception, 'status', None)
    if status is None:
        response = getattr(exception, 'response', None)
        status = getattr(response, 'status', None)
    return status


def _classification(exception: Exception):
    """Return ``(retryable, definitive)`` for an exception.

    ``definitive`` means the server answered, so it is known that the batch was
    not accepted.  A transport error has no definitive answer: the request may
    have been applied before the connection failed.
    """
    if isinstance(exception, MaxRetryError):
        reason = getattr(exception, 'reason', None)
        if reason is not None and reason is not exception:
            retryable, definitive = _classification(reason)
            if definitive:
                return retryable, definitive

    status = _status_of(exception)
    if status:
        # Matches WritesRetry.is_retry: only 429+ are retried.
        return (True, True) if status >= 429 else (False, True)

    if isinstance(exception, (HTTPError, OSError, socket.timeout, TimeoutError)):
        return True, False

    # Unknown user/transport exception: the request may or may not have been
    # transmitted, therefore it cannot be treated as a definitive failure.
    return True, False


class OrderedBatchWriter(object):
    """Schedule batches in enqueue order per target."""

    def __init__(self, write_api, write_options) -> None:
        self._write_api = write_api
        self._write_options = write_options
        self._condition = threading.Condition(threading.RLock())
        self._targets = {}
        self._sequence = 0
        # Keep a bounded history for observability without retaining every byte
        # of a long-running application.
        self._history = deque(maxlen=1_000)
        self._closing = False
        self._closed = False

    # ------------------------------------------------------------------
    # Incoming data / batching window
    # ------------------------------------------------------------------
    def add(self, batch_item) -> None:
        """Add one line-protocol item to its target queue."""
        with self._condition:
            if self._closed:
                logger.warning("The ordered writer is closed; dropping %s", batch_item)
                return

            self._sequence += 1
            sequence = self._sequence
            target = self._target(batch_item.key)
            record = _Record(batch_item.key, batch_item.data, batch_item.size, sequence)

            if target.stopped:
                record.status = OrderedBatchStatus.UNSENT
                target.queue.append(record)
                return

            if target.blocked:
                # Do not buffer indefinitely while blocked: the batch is
                # explicitly visible as unsent until the caller resolves it.
                record.status = OrderedBatchStatus.UNSENT
                target.queue.append(record)
                return

            target.pending.append(record)
            self._seal_full_batches(target)
            if target.pending:
                self._restart_timer(target)
            else:
                self._cancel_timer(target)
            self._ensure_worker(target)
            self._condition.notify_all()

    def _target(self, key) -> _Target:
        target = self._targets.get(key)
        if target is None:
            target = _Target(key)
            self._targets[key] = target
        return target

    def _seal_full_batches(self, target: _Target) -> None:
        batch_size = self._write_options.batch_size
        while batch_size and len(target.pending) >= batch_size:
            self._enqueue(target, target.pending[:batch_size])
            del target.pending[:batch_size]

    def _seal_all(self, target: _Target) -> None:
        self._cancel_timer(target)
        if target.pending:
            self._enqueue(target, target.pending[:])
            del target.pending[:]

    def _enqueue(self, target: _Target, records: List[_Record]) -> None:
        if not records:
            return
        if len(records) == 1:
            record = records[0]
        else:
            record = _Record(target.key,
                             b'\n'.join(item.data for item in records),
                             sum(item.size for item in records),
                             records[0].sequence)
        record.status = OrderedBatchStatus.UNSENT
        target.queue.append(record)
        self._ensure_worker(target)

    def _restart_timer(self, target: _Target) -> None:
        if self._write_options.flush_interval is None:
            return
        if target.timer is not None:
            return
        interval = max(0, self._write_options.flush_interval) / 1_000
        target.timer = threading.Timer(interval, self._on_timer, args=(target.key,))
        target.timer.daemon = True
        target.timer.start()

    def _cancel_timer(self, target: _Target) -> None:
        if target.timer is not None:
            target.timer.cancel()
            target.timer = None

    def _on_timer(self, key) -> None:
        with self._condition:
            target = self._targets.get(key)
            if target is None:
                return
            target.timer = None
            if target.blocked or target.stopped:
                return
            self._seal_all(target)
            self._condition.notify_all()

    def _ensure_worker(self, target: _Target) -> None:
        if target.worker is not None and target.worker.is_alive():
            return
        target.worker = threading.Thread(target=self._run_target, args=(target,),
                                         name='influxdb-ordered-writer', daemon=True)
        target.worker.start()

    def flush(self) -> None:
        """Close the current window for every target; does not block."""
        with self._condition:
            for target in self._targets.values():
                if not target.blocked and not target.stopped:
                    self._seal_all(target)
            self._condition.notify_all()

    # ------------------------------------------------------------------
    # Sending
    # ------------------------------------------------------------------
    def _run_target(self, target: _Target) -> None:
        while True:
            with self._condition:
                record = None
                while record is None:
                    if target.stopped:
                        return
                    if target.blocked:
                        if self._closing:
                            return
                        self._condition.wait()
                        continue
                    if target.queue:
                        record = target.queue.popleft()
                        record.status = OrderedBatchStatus.IN_FLIGHT
                        target.in_flight = record
                        target.busy = True
                        break
                    if self._closing:
                        return
                    self._condition.wait()
            self._process(target, record)

    def _process(self, target: _Target, record: _Record) -> None:
        status, exception = self._send_with_retries(record)

        callback = None
        with self._condition:
            target.in_flight = None
            if record.forced or self._closed:
                # close() already published a final state for this batch.
                target.busy = False
                self._condition.notify_all()
                return

            record.status = status
            record.exception = exception
            if status is OrderedBatchStatus.UNCONFIRMED:
                target.blocked = True
                target.unconfirmed.append(record)
                # Keep the following window visible as unsent instead of
                # leaving it in an ambiguous half-buffered state.
                self._seal_all(target)
            else:
                self._history.append(record)
            target.busy = False
            self._condition.notify_all()

            if status is OrderedBatchStatus.SENT:
                callback = ('success', record)
            elif status is OrderedBatchStatus.UNCONFIRMED:
                callback = ('unknown', record)
            elif status is OrderedBatchStatus.FAILED:
                callback = ('error', record)

        if callback is not None:
            self._fire(callback[0], callback[1])

    def _send_with_retries(self, record: _Record):
        max_retries = self._write_options.max_retries
        max_retry_time = self._write_options.max_retry_time
        if max_retry_time is not None and max_retry_time <= 0:
            max_retries = 0

        deadline = None
        failures = 0

        while True:
            if record.forced:
                # close() stopped waiting and already published this batch as
                # unconfirmed; do not make any further attempts.
                return OrderedBatchStatus.UNCONFIRMED, record.exception
            try:
                self._write_api._ordered_attempt(record)
                return OrderedBatchStatus.SENT, None
            except Exception as exception:  # noqa: BLE001 - user transports are arbitrary
                failures += 1
                retryable, definitive = _classification(exception)

                if deadline is None and retryable:
                    deadline = time.monotonic() + (max_retry_time or 0) / 1_000

                can_retry = retryable and failures <= max_retries
                if can_retry and deadline is not None:
                    can_retry = time.monotonic() < deadline
                if can_retry and record.forced:
                    can_retry = False
                if can_retry:
                    delay = self._retry_delay(exception, failures)
                    self._fire_retry(record, exception)
                    if delay > 0:
                        time.sleep(delay)
                    continue

                if definitive:
                    return OrderedBatchStatus.FAILED, exception
                error = WriteResultUnknownError(
                    "The result of the write is unknown: {!r}".format(exception),
                    batch=record, cause=exception)
                return OrderedBatchStatus.UNCONFIRMED, error

    def _retry_delay(self, exception: Exception, failures: int) -> float:
        retry_after = getattr(exception, 'retry_after', None)
        jitter_interval = self._write_options.jitter_interval / 1_000
        if retry_after is not None:
            try:
                delay = float(retry_after)
            except (TypeError, ValueError):
                delay = 0
            if jitter_interval:
                delay += jitter_interval * random()
            return max(0, delay)

        retry_interval = self._write_options.retry_interval / 1_000
        exponential_base = self._write_options.exponential_base
        max_retry_delay = self._write_options.max_retry_delay / 1_000

        range_start = retry_interval
        range_stop = retry_interval * exponential_base
        for _ in range(max(0, failures - 1)):
            range_start = range_stop
            range_stop = range_stop * exponential_base
            if range_stop > max_retry_delay:
                break
        if range_stop > max_retry_delay:
            range_stop = max_retry_delay
        if range_stop < range_start:
            range_stop = range_start
        return range_start + (range_stop - range_start) * random()

    # ------------------------------------------------------------------
    # Callbacks
    # ------------------------------------------------------------------
    def _claim_callback(self, record: _Record) -> bool:
        with self._condition:
            if record.callback_fired:
                return False
            record.callback_fired = True
            return True

    def _fire(self, kind: str, record: _Record) -> None:
        if not self._claim_callback(record):
            return
        key = record.key
        if kind == 'success':
            self._write_api._fire_success(key, record.data)
        else:
            # 'error', 'unknown' and 'not_sent' all notify error_callback with
            # a distinct exception type so callers can tell them apart.
            self._write_api._fire_error(key, record.data, record.exception)

    def _fire_retry(self, record: _Record, exception: Exception) -> None:
        self._write_api._fire_retry(record.key, record.data, exception)

    # ------------------------------------------------------------------
    # Explicit resolution / shutdown
    # ------------------------------------------------------------------
    def continue_target(self, key) -> bool:
        """Allow queued batches of a blocked target to be sent.

        The already attempted batch stays ``unconfirmed``; it is not re-sent.
        """
        with self._condition:
            target = self._targets.get(key)
            if target is None or not target.blocked or self._closed:
                return False
            target.blocked = False
            self._condition.notify_all()
            return True

    def abandon_target(self, key) -> bool:
        """Drop the queued batches of a blocked target without sending them."""
        callbacks = []
        with self._condition:
            target = self._targets.get(key)
            if target is None or not target.blocked:
                return False
            self._seal_all(target)
            while target.queue:
                record = target.queue.popleft()
                record.status = OrderedBatchStatus.ABANDONED
                record.exception = BatchNotSentError(
                    "The batch was not sent because the target was abandoned.",
                    batch=record)
                self._history.append(record)
                callbacks.append(record)
            target.blocked = False
            self._condition.notify_all()

        for record in callbacks:
            self._fire('not_sent', record)
        return True

    def close(self, max_wait_ms: int) -> None:
        """Stop accepting ordered work and publish the remaining state."""
        with self._condition:
            if self._closed:
                return
            self._closing = True
            for target in self._targets.values():
                self._seal_all(target)
            self._condition.notify_all()

        deadline = time.monotonic() + max(0, max_wait_ms or 0) / 1_000
        with self._condition:
            while True:
                waiting = [target for target in self._targets.values()
                           if not target.blocked and not target.stopped
                           and (target.queue or target.in_flight or target.busy or target.pending)]
                if not waiting:
                    break
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    logger.warning(
                        "Reached max_close_wait (%s ms) waiting for ordered batches. "
                        "Remaining batches stay unconfirmed/unsent.", max_wait_ms)
                    break
                self._condition.wait(remaining)

            callbacks = []
            for target in self._targets.values():
                target.stopped = True
                self._cancel_timer(target)

                if target.in_flight is not None:
                    record = target.in_flight
                    target.in_flight = None
                    record.forced = True
                    if record.status is not OrderedBatchStatus.UNCONFIRMED:
                        record.status = OrderedBatchStatus.UNCONFIRMED
                        record.exception = WriteResultUnknownError(
                            "The writer was closed before the write result was received.",
                            batch=record)
                        target.unconfirmed.append(record)
                        callbacks.append(('unknown', record))

                # Pending records that were never sealed are sealed above; be
                # defensive in case a timer raced with close.
                for record in target.pending:
                    record.status = OrderedBatchStatus.UNSENT
                    target.queue.append(record)
                target.pending.clear()

                for record in target.unconfirmed:
                    if not record.callback_fired:
                        callbacks.append(('unknown', record))

                for record in target.queue:
                    if record.status is OrderedBatchStatus.UNSENT and not record.callback_fired:
                        record.exception = BatchNotSentError(
                            "The batch was not sent before the writer was closed.",
                            batch=record)
                        callbacks.append(('not_sent', record))

            self._closed = True
            self._condition.notify_all()

        for kind, record in callbacks:
            self._fire(kind, record)

    # ------------------------------------------------------------------
    # Observation
    # ------------------------------------------------------------------
    def snapshot(self, include_completed: bool = False) -> List[OrderedBatch]:
        """Return a point-in-time view of tracked batches, in enqueue order."""
        with self._condition:
            records = []
            for target in self._targets.values():
                records.extend(target.pending)
                records.extend(target.queue)
                records.extend(target.unconfirmed)
                if target.in_flight is not None:
                    records.append(target.in_flight)
            if include_completed:
                records.extend(self._history)
            records.sort(key=lambda item: item.sequence or 0)
            return [record.to_public() for record in records]

    def blocked_targets(self):
        """Return ``(bucket, org, precision)`` keys waiting for a caller decision."""
        with self._condition:
            return [target.key.to_key_tuple() if hasattr(target.key, 'to_key_tuple')
                    else (target.key.bucket, target.key.org, target.key.precision)
                    for target in self._targets.values() if target.blocked]

    def is_closed(self) -> bool:
        """Whether the writer has been closed."""
        with self._condition:
            return self._closed
