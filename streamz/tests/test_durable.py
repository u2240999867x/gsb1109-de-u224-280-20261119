import json

import pytest

from streamz import Stream
from streamz.durable import DurableWriteTable
from streamz.utils_test import await_for


URGENT = "urgent"
NORMAL = "normal"


def make_event(priority, seq):
    return {"priority": priority, "payload": f"{priority}-{seq}"}


@pytest.fixture
def state_file(tmp_path):
    return str(tmp_path / "dispatch-state.json")


def records_by_id(dispatcher):
    return {record["event_id"]: record
            for record in dispatcher.event_records()}


@pytest.mark.asyncio
async def test_burst_limit_failover_and_late_receipt(state_file):
    table = DurableWriteTable()
    source = Stream(asynchronous=True)
    dispatcher = source.durable_dispatch(table, state_file, start_paused=True,
                                    urgent_capacity=10, normal_capacity=10)

    # One normal event starts waiting, then four urgent events arrive.
    await source.emit(make_event(NORMAL, 1))
    for seq in range(1, 5):
        await source.emit(make_event(URGENT, seq))

    # Paused: everything is queued, nothing has reached the writer.
    assert table.dispatch_log == []

    table.admit(3)
    dispatcher.resume()
    await await_for(lambda: len(table.dispatch_log) == 3, timeout=1)
    assert table.dispatch_log == [
        {"event_id": "urgent-1", "attempt": 1},
        {"event_id": "urgent-2", "attempt": 1},
        {"event_id": "urgent-3", "attempt": 1},
    ]

    snapshot = records_by_id(dispatcher)
    assert snapshot["urgent-1"]["state"] == "pending"
    assert snapshot["urgent-2"]["state"] == "pending"
    # Third urgent was accepted by the writer but its receipt is still in the
    # mail: it must not be marked successful, nor dispatched a second time.
    assert snapshot["urgent-3"]["state"] == "pending"
    assert snapshot["urgent-3"]["attempts"] == [
        {"attempt": 1, "status": "pending", "error": None,
         "superseded": False}]
    assert snapshot["normal-1"]["state"] == "queued"
    assert snapshot["urgent-4"]["state"] == "queued"

    # The checkpoint must carry the in-flight attempt (both lanes still have
    # capacity) before the executor hands over.
    dispatcher.save_progress()
    with open(state_file) as fp:
        on_disk = json.load(fp)
    inflight_ids = {(item["event_id"], item["attempt"])
                    for item in on_disk["inflight"]}
    assert ("urgent-3", 1) in inflight_ids
    assert ("normal-1", 1) not in inflight_ids
    assert any(item["event_id"] == "normal-1" for item in on_disk["queued"])
    dispatcher.close()

    # --- A fresh executor takes over, sharing the same writer table. ---
    source2 = Stream(asynchronous=True)
    successor = source2.durable_dispatch(table, state_file,
                                urgent_capacity=10, normal_capacity=10)

    # A new urgent event is only queued on recovery.
    await source2.emit(make_event(URGENT, 5))
    await await_for(lambda: records_by_id(successor)["urgent-5"]["state"]
                    == "queued", timeout=1)
    assert len(table.dispatch_log) == 3  # no permit, and no speculative redispatch

    # The late receipt for the original third urgent attempt arrives.
    table.succeed(table.handle("urgent-3", 1))
    await await_for(
        lambda: records_by_id(successor)["urgent-3"]["state"] == "confirmed",
        timeout=1)
    successor_records = records_by_id(successor)
    assert successor_records["urgent-3"]["attempts"] == [
        {"attempt": 1, "status": "confirmed", "error": None,
         "superseded": False}]
    assert len(table.attempts_for("urgent-3")) == 1  # never re-dispatched

    # The next brand new dispatch must be the long-waiting normal event.
    table.admit(1)
    await await_for(lambda: len(table.dispatch_log) == 4, timeout=1)
    assert table.dispatch_log[-1] == {"event_id": "normal-1", "attempt": 1}

    # After the normal event the urgent lane drains in enqueue order.
    table.admit(2)
    await await_for(lambda: len(table.dispatch_log) == 6, timeout=1)
    assert [item["event_id"] for item in table.dispatch_log] == [
        "urgent-1", "urgent-2", "urgent-3",
        "normal-1", "urgent-4", "urgent-5"]

    for event_id, attempt in [
        ("urgent-1", 1), ("urgent-2", 1), ("normal-1", 1),
        ("urgent-4", 1), ("urgent-5", 1)]:
        table.succeed(table.handle(event_id, attempt))
    await await_for(
        lambda: all(record["state"] == "confirmed"
                    for record in successor.event_records()), timeout=1)

    receipts = {(item["event_id"], item["attempt"]): item["status"]
                for item in table.receipts}
    assert receipts[("urgent-3", 1)] == "confirmed"
    assert all(status == "confirmed" for status in receipts.values())


@pytest.mark.asyncio
async def test_explicit_retry_is_distinct_attempt_and_keeps_normal_slot(
        state_file):
    table = DurableWriteTable()
    source = Stream(asynchronous=True)
    dispatcher = source.durable_dispatch(table, state_file, start_paused=True,
                                    urgent_capacity=10, normal_capacity=10)
    await source.emit(make_event(NORMAL, 1))
    for seq in range(1, 4):
        await source.emit(make_event(URGENT, seq))

    table.admit(3)
    dispatcher.resume()
    await await_for(lambda: len(table.dispatch_log) == 3, timeout=1)
    dispatcher.save_progress()
    dispatcher.close()

    source2 = Stream(asynchronous=True)
    successor = source2.durable_dispatch(table, state_file,
                                urgent_capacity=10, normal_capacity=10)
    successor.pause()  # hold the queue while the retry is set up explicitly

    # Explicitly re-submit the third urgent event before any receipt arrives.
    table.admit(1)
    second_attempt = await successor.retry(URGENT, 3)
    assert second_attempt == 2
    assert table.attempts_for("urgent-3") == [
        {"event_id": "urgent-3", "attempt": 1},
        {"event_id": "urgent-3", "attempt": 2}]

    # The old attempt's failure arrives first: it must not fail the new attempt.
    table.fail(table.handle("urgent-3", 1))
    await await_for(
        lambda: any(
            item["attempt"] == 1 and item["status"] == "failed"
            for item in records_by_id(successor)["urgent-3"]["attempts"]),
        timeout=1)
    snapshot = records_by_id(successor)["urgent-3"]
    assert snapshot["state"] == "pending"
    assert [item["status"] for item in snapshot["attempts"]] == [
        "failed", "pending"]
    assert snapshot["attempts"][0]["superseded"] is True
    assert len(table.dispatch_log) == 4  # only the explicit retry was added

    # The retry must not consume the dispatch slot promised to the normal event.
    table.admit(1)
    successor.resume()
    await await_for(lambda: len(table.dispatch_log) == 5, timeout=1)
    assert table.dispatch_log[-1] == {"event_id": "normal-1", "attempt": 1}
    assert table.attempts_for("normal-1") == [
        {"event_id": "normal-1", "attempt": 1}]

    # The new attempt then confirms on its own receipt.
    table.succeed(table.handle("urgent-3", 2))
    await await_for(
        lambda: records_by_id(successor)["urgent-3"]["state"] == "confirmed",
        timeout=1)
    final = records_by_id(successor)["urgent-3"]
    assert [(item["attempt"], item["status"]) for item in final["attempts"]] == [
        (1, "failed"), (2, "confirmed")]

    receipts = {(item["event_id"], item["attempt"]): item["status"]
                for item in table.receipts}
    assert receipts[("urgent-3", 1)] == "failed"
    assert receipts[("urgent-3", 2)] == "confirmed"


@pytest.mark.asyncio
async def test_late_success_of_superseded_attempt_is_ignored(state_file):
    table = DurableWriteTable()
    source = Stream(asynchronous=True)
    dispatcher = source.durable_dispatch(table, state_file, start_paused=True,
                                    urgent_capacity=10, normal_capacity=10)
    await source.emit(make_event(URGENT, 1))

    table.admit(1)
    dispatcher.resume()
    await await_for(lambda: len(table.dispatch_log) == 1, timeout=1)
    dispatcher.save_progress()
    dispatcher.close()

    source2 = Stream(asynchronous=True)
    successor = source2.durable_dispatch(table, state_file,
                                urgent_capacity=10, normal_capacity=10)
    successor.pause()
    table.admit(1)
    await successor.retry(URGENT, 1)

    # Old attempt succeeds after the retry was requested.
    table.succeed(table.handle("urgent-1", 1))
    table.fail(table.handle("urgent-1", 2))
    await await_for(
        lambda: records_by_id(successor)["urgent-1"]["state"] == "queued",
        timeout=1)
    snapshot = records_by_id(successor)["urgent-1"]
    assert [(item["attempt"], item["status"]) for item in snapshot["attempts"]] == [
        (1, "confirmed"), (2, "failed")]
    # The failed current attempt requeues the event instead of confirming it.
    assert table.attempts_for("urgent-1") == [
        {"event_id": "urgent-1", "attempt": 1},
        {"event_id": "urgent-1", "attempt": 2}]


@pytest.mark.asyncio
async def test_checkpoint_after_receipt_means_no_retry_on_recovery(state_file):
    table = DurableWriteTable()
    source = Stream(asynchronous=True)
    dispatcher = source.durable_dispatch(table, state_file, start_paused=True,
                                    urgent_capacity=10, normal_capacity=10)
    await source.emit(make_event(NORMAL, 1))
    for seq in range(1, 4):
        await source.emit(make_event(URGENT, seq))

    table.admit(3)
    dispatcher.resume()
    await await_for(lambda: len(table.dispatch_log) == 3, timeout=1)

    # The third urgent receipt arrives *before* progress is saved.
    table.succeed(table.handle("urgent-3", 1))
    await await_for(
        lambda: records_by_id(dispatcher)["urgent-3"]["state"] == "confirmed",
        timeout=1)
    dispatcher.save_progress()
    with open(state_file) as fp:
        on_disk = json.load(fp)
    assert ["urgent", 3] in on_disk["confirmed"]
    assert not any(item["event_id"] == "urgent-3"
                   for item in on_disk["inflight"])
    dispatcher.close()

    source2 = Stream(asynchronous=True)
    successor = source2.durable_dispatch(table, state_file,
                                urgent_capacity=10, normal_capacity=10)
    table.admit(1)
    await await_for(lambda: len(table.dispatch_log) == 4, timeout=1)
    assert table.dispatch_log[-1] == {"event_id": "normal-1", "attempt": 1}
    assert len(table.attempts_for("urgent-3")) == 1  # no redundant resubmission
    assert records_by_id(successor)["urgent-3"]["state"] == "confirmed"

    with pytest.raises(RuntimeError, match="already confirmed"):
        await successor.retry(URGENT, 3)


@pytest.mark.asyncio
async def test_single_priority_keeps_draining(state_file):
    table = DurableWriteTable()
    source = Stream(asynchronous=True)
    dispatcher = source.durable_dispatch(table, state_file, start_paused=True,
                                    urgent_capacity=10, normal_capacity=10)
    for seq in range(1, 6):
        await source.emit(make_event(URGENT, seq))

    table.admit(5)
    dispatcher.resume()
    await await_for(lambda: len(table.dispatch_log) == 5, timeout=1)
    assert [item["event_id"] for item in table.dispatch_log] == [
        f"urgent-{seq}" for seq in range(1, 6)]

    for seq in range(1, 6):
        table.succeed(table.handle(f"urgent-{seq}", 1))
    await await_for(
        lambda: all(record["state"] == "confirmed"
                    for record in dispatcher.event_records()), timeout=1)


@pytest.mark.asyncio
async def test_normal_only_lane_keeps_draining(state_file):
    table = DurableWriteTable()
    source = Stream(asynchronous=True)
    dispatcher = source.durable_dispatch(
        table, state_file, start_paused=True,
        urgent_capacity=10, normal_capacity=10)
    for seq in range(1, 4):
        await source.emit(make_event(NORMAL, seq))

    table.admit(3)
    dispatcher.resume()
    await await_for(lambda: len(table.dispatch_log) == 3, timeout=1)
    assert [item["event_id"] for item in table.dispatch_log] == [
        "normal-1", "normal-2", "normal-3"]


@pytest.mark.asyncio
async def test_lane_capacity_blocks_extra_events(state_file):
    table = DurableWriteTable()
    source = Stream(asynchronous=True)
    dispatcher = source.durable_dispatch(
        table, state_file, start_paused=True,
        urgent_capacity=2, normal_capacity=2)
    for seq in range(1, 4):
        await source.emit(make_event(URGENT, seq))

    table.admit(2)
    dispatcher.resume()
    await await_for(lambda: len(table.dispatch_log) == 2, timeout=1)
    assert [item["event_id"] for item in table.dispatch_log] == [
        "urgent-1", "urgent-2"]
    assert dispatcher._inflight_count(URGENT) == 2
    assert records_by_id(dispatcher)["urgent-3"]["state"] == "queued"

    # Freeing one slot lets the queued event go; it never overran the lane.
    table.succeed(table.handle("urgent-1", 1))
    table.admit(1)
    await await_for(lambda: len(table.dispatch_log) == 3, timeout=1)
    assert table.dispatch_log[-1] == {"event_id": "urgent-3", "attempt": 1}


@pytest.mark.asyncio
async def test_retry_guards(state_file):
    table = DurableWriteTable()
    source = Stream(asynchronous=True)
    dispatcher = source.durable_dispatch(table, state_file, start_paused=True,
                                    urgent_capacity=10, normal_capacity=10)
    await source.emit(make_event(URGENT, 1))

    with pytest.raises(RuntimeError, match="not in flight"):
        await dispatcher.retry(URGENT, 1)

    table.admit(1)
    dispatcher.resume()
    await await_for(lambda: len(table.dispatch_log) == 1, timeout=1)
    table.succeed(table.handle("urgent-1", 1))
    await await_for(
        lambda: records_by_id(dispatcher)["urgent-1"]["state"] == "confirmed",
        timeout=1)
    with pytest.raises(RuntimeError, match="already confirmed"):
        await dispatcher.retry(URGENT, 1)
