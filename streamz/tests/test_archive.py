from datetime import datetime, timedelta, timezone

from streamz import Stream
from streamz.archive import DeviceHourArchive

UTC = timezone.utc
T0 = datetime(2026, 11, 23, 12, 0, 0, tzinfo=UTC)
DEVICE = "dev-1"


def at(**kwargs):
    return T0 + timedelta(**kwargs)


def obs(source, seq, moment, epoch=0, device=DEVICE, value=None):
    return {
        "kind": "observation",
        "device": device,
        "source": source,
        "epoch": epoch,
        "seq": seq,
        "event_time": moment,
        "value": value if value is not None else seq,
    }


def watermark(source, moment, epoch=0):
    return {"kind": "watermark", "source": source, "epoch": epoch,
            "time": moment}


def handoff(old_last_seq, new_start_seq, source="s2", device=DEVICE,
            old_epoch=0, new_epoch=1):
    return {
        "kind": "handoff",
        "device": device,
        "source": source,
        "old_epoch": old_epoch,
        "new_epoch": new_epoch,
        "old_last_seq": old_last_seq,
        "new_start_seq": new_start_seq,
    }


def epoch_end(last_seq, source="s2", device=DEVICE, epoch=0):
    return {"kind": "epoch_end", "device": device, "source": source,
            "epoch": epoch, "last_seq": last_seq}


def make_archive():
    source = Stream()
    archive = source.device_hour_archive()
    assert isinstance(archive, DeviceHourArchive)
    sealed = archive.sealed.sink_to_list()
    late = archive.late.sink_to_list()
    return source, archive, sealed, late


def drive_handoff_flow(source):
    """The shared prefix: both legs produce data, s2 hands off mid-window."""
    # first leg observations and its watermark past 12:01
    for seq, seconds in [(1, 5), (2, 15), (3, 25)]:
        source.emit(obs("s1", seq, at(seconds=seconds)))
    # second leg, old epoch, first observations
    for seq in range(1, 6):
        source.emit(obs("s2", seq, at(seconds=7 * seq), epoch=0))
    # reader-head handoff in the middle of the 12:00 window
    source.emit(handoff(old_last_seq=8, new_start_seq=6))
    # first leg watermark crosses 12:01
    source.emit(watermark("s1", at(minutes=1)))


def test_handoff_out_of_order_seals_window():
    source, archive, sealed, late = make_archive()
    drive_handoff_flow(source)
    assert sealed == []

    # out-of-order arrival: new-epoch watermark first
    source.emit(watermark("s2", at(minutes=1), epoch=1))
    assert sealed == []
    # then a replayed old sequence from the new epoch
    source.emit(obs("s2", 6, at(seconds=42), epoch=1))
    assert sealed == []
    # the last old-epoch observations, one of them a replay duplicate
    source.emit(obs("s2", 8, at(seconds=56), epoch=0))
    source.emit(obs("s2", 6, at(seconds=42), epoch=0))
    source.emit(obs("s2", 7, at(seconds=49), epoch=0))
    assert sealed == []
    reasons = archive.pending_reasons(DEVICE, T0)
    assert any(r["kind"] == "watermark_behind" and r["source"] == "s2"
               and r["epoch"] == 0 for r in reasons)

    # finally the old-epoch end receipt: handoff interval fully accounted
    source.emit(epoch_end(last_seq=8))
    assert len(sealed) == 1
    event = sealed[0]
    assert event["kind"] == "window_sealed"
    assert event["device"] == DEVICE
    assert event["window_start"] == T0
    assert event["window_end"] == at(minutes=1)
    assert event["attempt"] == 1

    records = event["records"]
    # three first-leg + eight second-leg observations, replay counted once
    assert len(records) == 11
    s2_seqs = [r["seq"] for r in records if r["source"] == "s2"]
    assert sorted(s2_seqs) == [1, 2, 3, 4, 5, 6, 7, 8]
    assert archive.duplicates(DEVICE) == 1
    assert late == []
    assert archive.window_state(DEVICE, T0) == "sealed"


def test_missing_epoch_end_keeps_window_pending():
    source, archive, sealed, late = make_archive()
    source.emit(obs("s1", 1, at(seconds=5)))
    # old epoch delivers every sequence except 6
    for seq in [1, 2, 3, 4, 5, 7, 8]:
        source.emit(obs("s2", seq, at(seconds=7 * seq), epoch=0))
    # the new epoch starts at 7, so it will never cover sequence 6
    source.emit(handoff(old_last_seq=8, new_start_seq=7))
    source.emit(watermark("s1", at(minutes=1)))
    source.emit(watermark("s2", at(minutes=1), epoch=1))
    source.emit(obs("s2", 7, at(seconds=49), epoch=1))  # replay duplicate
    # no old-epoch end receipt ever arrives

    # both active watermarks are past 12:01, yet the window must stay open
    assert sealed == []
    assert archive.window_state(DEVICE, T0) == "open"

    reasons = archive.pending_reasons(DEVICE, T0)
    missing = [r for r in reasons if r["kind"] == "missing_sequences"]
    assert len(missing) == 1
    assert missing[0]["source"] == "s2"
    assert missing[0]["epoch"] == 0
    assert (missing[0]["seq_start"], missing[0]["seq_end"]) == (6, 6)
    assert missing[0]["coverable_by_replay"] is False
    assert any(r["kind"] == "watermark_behind" and r["source"] == "s2"
               and r["epoch"] == 0 for r in reasons)


def seal_first_window():
    source, archive, sealed, late = make_archive()
    drive_handoff_flow(source)
    source.emit(watermark("s2", at(minutes=1), epoch=1))
    source.emit(obs("s2", 6, at(seconds=42), epoch=1))
    source.emit(obs("s2", 8, at(seconds=56), epoch=0))
    source.emit(obs("s2", 7, at(seconds=49), epoch=0))
    source.emit(epoch_end(last_seq=8))
    assert len(sealed) == 1
    return source, archive, sealed, late


def test_late_and_boundary_records_after_seal():
    source, archive, sealed, late = seal_first_window()
    batch = list(sealed[0]["records"])

    # a record inside the sealed minute goes to the late outlet
    source.emit(obs("s2", 9, at(seconds=59), epoch=1))
    assert len(late) == 1
    assert late[0]["record"]["seq"] == 9
    assert late[0]["window_start"] == T0
    assert len(sealed) == 1
    assert sealed[0]["records"] == batch  # sealed batch untouched

    # a record exactly on the boundary belongs to the next minute
    source.emit(obs("s2", 10, at(minutes=1), epoch=1))
    assert len(late) == 1
    assert archive.window_state(DEVICE, at(minutes=1)) == "open"

    # the next minute seals once both legs pass 12:02; the ended old
    # epoch does not block it
    source.emit(watermark("s1", at(minutes=2)))
    source.emit(watermark("s2", at(minutes=2), epoch=1))
    assert len(sealed) == 2
    assert sealed[1]["window_start"] == at(minutes=1)
    assert [r["seq"] for r in sealed[1]["records"]] == [10]


def test_failed_write_retry_and_confirmation():
    source, archive, sealed, late = seal_first_window()
    batch = list(sealed[0]["records"])

    # an open window cannot be confirmed
    source.emit(obs("s2", 10, at(minutes=1, seconds=10), epoch=1))
    assert archive.confirm_write(DEVICE, at(minutes=1), []) is False

    # only the exact batch of raw observations can be confirmed
    tampered = batch + [obs("s2", 9, at(seconds=59), epoch=1)]
    assert archive.confirm_write(DEVICE, T0, tampered) is False
    assert archive.window_state(DEVICE, T0) == "sealed"

    # the write of the old minute fails: resubmit identical content
    assert archive.fail_write(DEVICE, T0) is True
    assert archive.window_state(DEVICE, T0) == "failed"
    retry = archive.retry_write(DEVICE, T0)
    assert retry["attempt"] == 2
    assert retry["records"] == batch
    assert len(sealed) == 2
    assert sealed[1]["records"] == sealed[0]["records"]

    # confirm the batch that was actually written
    assert archive.confirm_write(DEVICE, T0, list(batch)) is True
    assert archive.window_state(DEVICE, T0) == "confirmed"
    assert archive.retry_write(DEVICE, T0) is None
    assert archive.fail_write(DEVICE, T0) is False

    # a late watermark from the old reader head changes nothing
    emissions = len(sealed) + len(late)
    source.emit(watermark("s2", at(minutes=5), epoch=0))
    assert len(sealed) + len(late) == emissions
    assert archive.window_state(DEVICE, T0) == "confirmed"
    assert archive.window_state(DEVICE, at(minutes=1)) == "open"
    reasons = archive.pending_reasons(DEVICE, at(minutes=1))
    assert all(not (r["source"] == "s2" and r.get("epoch") == 0)
               for r in reasons)
