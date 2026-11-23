"""Tests for the ingest ledger state machine and merge semantics."""

from __future__ import annotations

import math

import numpy as np
import pytest

from station_ingest import (
    OBS_MISSING,
    OBS_VALUE,
    STATE_CONFLICT,
    STATE_MISSING,
    STATE_UNCOVERED,
    STATE_VALUE,
    ConflictingRecordError,
    DuplicateRecordError,
    IngestProgress,
    PrunedRecordError,
    Revocation,
    SealedTargetError,
    SealingReceipt,
    Submission,
    consensus_grid,
    obs_missing,
    obs_value,
)

S, T0, T1, T2 = "ST1", "t0", "t1", "t2"
TEMP = "temperature"


def c(time: str, variable: str = TEMP, station: str = S) -> tuple[str, str, str]:
    return station, time, variable


def sub(
    record_id: int,
    collector: str,
    cells: dict[tuple[str, str], float | None],
) -> Submission:
    observations = {
        c(time, variable): (
            obs_missing() if value is None else obs_value(value)
        )
        for (variable, time), value in cells.items()
    }
    return Submission(record_id, collector, observations)


def status_of(progress: IngestProgress, time: str) -> int:
    return int(progress.view(TEMP).status[0, progress.times.index(time)])


def value_of(progress: IngestProgress, time: str) -> float:
    return float(progress.view(TEMP).value[0, progress.times.index(time)])


def test_three_coverage_states_and_valid_zero() -> None:
    progress = IngestProgress()
    view = progress.view(TEMP)
    assert view.status.shape == (0, 0)

    progress.add_submission(sub(1, "A", {(TEMP, T0): 0.0, (TEMP, T1): 5.0}))
    progress.add_submission(
        Submission(2, "B", {c(T1): obs_missing()})
    )
    assert status_of(progress, T0) == STATE_VALUE
    assert value_of(progress, T0) == 0.0
    assert status_of(progress, T1) == STATE_CONFLICT  # value vs explicit missing
    assert math.isnan(value_of(progress, T1))

    only_missing = IngestProgress()
    only_missing.add_submission(Submission(1, "A", {c(T0): obs_missing()}))
    assert status_of(only_missing, T0) == STATE_MISSING
    assert only_missing.source_states(S, T0, TEMP)["A"][1] == OBS_MISSING


def test_consensus_requires_agreement_and_lists_parties() -> None:
    progress = IngestProgress()
    progress.add_submission(sub(1, "A", {(TEMP, T0): 12.0}))
    progress.add_submission(sub(2, "B", {(TEMP, T0): 12.0}))
    assert status_of(progress, T0) == STATE_VALUE and value_of(progress, T0) == 12.0
    assert progress.view(TEMP).contributor_count[0, 0] == 2

    progress.add_submission(sub(3, "C", {(TEMP, T0): 13.0}))
    view = progress.view(TEMP)
    assert view.status[0, 0] == STATE_CONFLICT
    details = view.conflict_details()[0]
    assert {party["collector"] for party in details["parties"]} == {"A", "B", "C"}
    assert {party["value"] for party in details["parties"]} == {12.0, 13.0}
    assert math.isnan(view.value[0, 0])


def test_submissions_order_by_record_id_including_late_smaller_id() -> None:
    ordered = IngestProgress()
    ordered.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    ordered.add_submission(sub(2, "C", {(TEMP, T0): 2.0}))

    shuffled = IngestProgress()
    shuffled.add_submission(sub(2, "C", {(TEMP, T0): 2.0}))
    shuffled.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))

    assert value_of(shuffled, T0) == value_of(ordered, T0) == 2.0
    assert shuffled.source_states(S, T0, TEMP)["C"][0] == 2


def test_duplicate_id_with_different_content_is_rejected() -> None:
    progress = IngestProgress()
    progress.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    # Re-delivering the identical record is a no-op.
    progress.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    with pytest.raises(DuplicateRecordError):
        progress.add_submission(sub(1, "C", {(TEMP, T0): 9.0}))


def test_revocation_restores_predecessor_and_supports_partial_cells() -> None:
    progress = IngestProgress()
    progress.add_submission(sub(1, "C", {(TEMP, T0): 1.0, (TEMP, T1): 7.0}))
    progress.add_submission(sub(2, "C", {(TEMP, T0): 2.0, (TEMP, T1): 8.0}))
    progress.add_revocation(Revocation(3, 2, "C", [c(T0)]))

    assert value_of(progress, T0) == 1.0  # predecessor restored
    assert progress.source_states(S, T0, TEMP)["C"][0] == 1
    assert value_of(progress, T1) == 8.0  # untouched cell keeps latest


def test_revocation_without_predecessor_uncovers_cell() -> None:
    progress = IngestProgress(stations=[S], times=[T0], variables=[TEMP])
    progress.add_submission(sub(1, "C", {(TEMP, T0): 4.0}))
    progress.add_revocation(Revocation(2, 1, "C", [c(T0)]))
    assert status_of(progress, T0) == STATE_UNCOVERED
    assert "C" not in progress.source_states(S, T0, TEMP)


def test_explicit_missing_only_revived_by_its_own_revocation() -> None:
    progress = IngestProgress()
    progress.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    progress.add_submission(
        Submission(2, "C", {c(T0): obs_missing()})
    )
    assert status_of(progress, T0) == STATE_MISSING
    # An earlier id arriving late cannot replace the active missing marker.
    progress.add_submission(sub(0, "C", {(TEMP, T0): 9.0}))
    assert status_of(progress, T0) == STATE_MISSING
    progress.add_revocation(Revocation(3, 2, "C", [c(T0)]))
    assert status_of(progress, T0) == STATE_VALUE
    assert value_of(progress, T0) == 1.0


def test_revocation_can_arrive_before_its_target() -> None:
    progress = IngestProgress(stations=[S], times=[T0], variables=[TEMP])
    progress.add_revocation(Revocation(5, 4, "C", [c(T0)]))  # target 4 missing
    progress.add_submission(sub(4, "C", {(TEMP, T0): 4.0}))
    # Revocation activates as soon as the target lands; no predecessor -> uncovered.
    assert status_of(progress, T0) == STATE_UNCOVERED

    with pytest.raises(ValueError, match="another collector"):
        progress.add_revocation(Revocation(6, 4, "B", [c(T0)]))


def test_late_target_with_mismatched_collector_is_rejected() -> None:
    progress = IngestProgress(stations=[S], times=[T0], variables=[TEMP])
    progress.add_revocation(Revocation(5, 4, "B", [c(T0)]))
    with pytest.raises(ValueError, match="another collector"):
        progress.add_submission(sub(4, "C", {(TEMP, T0): 4.0}))


def test_revocation_chain_falls_back_through_revoked_submissions() -> None:
    progress = IngestProgress()
    progress.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    progress.add_submission(sub(2, "C", {(TEMP, T0): 2.0}))
    progress.add_submission(sub(3, "C", {(TEMP, T0): 3.0}))
    progress.add_revocation(Revocation(4, 3, "C", [c(T0)]))
    progress.add_revocation(Revocation(5, 2, "C", [c(T0)]))
    assert value_of(progress, T0) == 1.0
    assert progress.source_states(S, T0, TEMP)["C"][0] == 1


def test_sealing_receipt_completes_only_when_all_records_present() -> None:
    progress = IngestProgress()
    progress.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    receipt = SealingReceipt(9, [1, 2])
    progress.add_sealing_receipt(receipt)
    assert progress.pending_receipts and progress.sealed_records == frozenset()

    progress.add_revocation(Revocation(2, 1, "C", [c(T0)]))
    assert not progress.pending_receipts
    assert progress.sealed_records == frozenset({1, 2})

    # A smaller, unnamed record may still arrive late.
    progress.add_submission(sub(0, "C", {(TEMP, T1): 6.0}))
    assert value_of(progress, T1) == 6.0

    # Sealed submissions cannot be targeted by new revocations, but the same
    # coordinates remain open for fresh submissions.
    with pytest.raises(SealedTargetError):
        progress.add_revocation(Revocation(3, 1, "C", [c(T0)]))
    progress.add_submission(sub(4, "C", {(TEMP, T0): 4.0}))
    assert value_of(progress, T0) == 4.0


def test_prune_keeps_current_values_but_drops_dead_history() -> None:
    progress = IngestProgress()
    progress.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    progress.add_submission(
        Submission(2, "C", {c(T0): obs_missing()})
    )
    progress.add_revocation(Revocation(3, 2, "C", [c(T0)]))
    progress.add_sealing_receipt(SealingReceipt(9, [1, 2, 3]))

    before = consensus_grid(progress, TEMP)
    pruned = progress.prune_archived()
    assert tuple(pruned) == (1, 2, 3)
    assert progress.tombstones == frozenset({1, 2, 3})
    assert progress.submissions == () and progress.revocations == ()
    after = progress.view(TEMP)
    np.testing.assert_array_equal(before.status, after.status)
    np.testing.assert_array_equal(
        np.nan_to_num(before.value, nan=-1.0), np.nan_to_num(after.value, nan=-1.0)
    )
    assert progress.source_states(S, T0, TEMP)["C"] == (1, OBS_VALUE, 1.0)

    with pytest.raises(PrunedRecordError):
        progress.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    with pytest.raises(PrunedRecordError):
        progress.add_revocation(Revocation(3, 2, "C", [c(T0)]))


def test_merge_commutes_and_is_idempotent() -> None:
    left = IngestProgress()
    left.add_submission(sub(1, "A", {(TEMP, T0): 12.0}))
    left.add_submission(sub(3, "C", {(TEMP, T0): 13.0}))

    right = IngestProgress()
    right.add_submission(sub(1, "A", {(TEMP, T0): 12.0}))  # identical shared record
    right.add_submission(sub(2, "B", {(TEMP, T0): 12.0}))

    lr = IngestProgress.merge(left, right)
    rl = IngestProgress.merge(right.copy(), left.copy())
    assert status_of(lr, T0) == STATE_CONFLICT
    assert lr.view(TEMP).contributor_count[0, 0] == 3
    np.testing.assert_array_equal(lr.view(TEMP).status, rl.view(TEMP).status)
    assert {s.record_id for s in lr.submissions} == {1, 2, 3}

    again = IngestProgress.merge(lr, right)
    assert {s.record_id for s in again.submissions} == {1, 2, 3}
    assert again.view(TEMP).contributor_count[0, 0] == 3


def test_merge_rejects_incompatible_record_content() -> None:
    left = IngestProgress()
    left.add_submission(sub(1, "A", {(TEMP, T0): 1.0}))
    right = IngestProgress()
    right.add_submission(sub(1, "A", {(TEMP, T0): 2.0}))
    with pytest.raises(ConflictingRecordError):
        IngestProgress.merge(left, right)


def test_archived_snapshot_cannot_revive_revoked_or_superseded_values() -> None:
    # Authority side: full chain sealed and pruned, current value is 1 from id 1.
    authority = IngestProgress()
    authority.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    authority.add_submission(
        Submission(2, "C", {c(T0): obs_missing()})
    )
    authority.add_revocation(Revocation(3, 2, "C", [c(T0)]))
    authority.add_sealing_receipt(SealingReceipt(9, [1, 2, 3]))
    authority.prune_archived()

    # Stale snapshot saved before the revocation arrived.
    stale = IngestProgress()
    stale.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    stale.add_submission(Submission(2, "C", {c(T0): obs_missing()}))

    for merged in (
        IngestProgress.merge(authority, stale),
        IngestProgress.merge(stale, authority.copy()),
    ):
        assert status_of(merged, T0) == STATE_VALUE
        assert value_of(merged, T0) == 1.0
        assert merged.tombstones == frozenset({1, 2, 3})
        assert merged.source_states(S, T0, TEMP)["C"][0] == 1


def test_later_live_delivery_beats_archived_value_on_merge() -> None:
    archived = IngestProgress()
    archived.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    archived.add_sealing_receipt(SealingReceipt(9, [1]))
    archived.prune_archived()

    fresh = IngestProgress()
    fresh.add_submission(sub(2, "C", {(TEMP, T0): 2.0}))

    merged = IngestProgress.merge(archived, fresh)
    assert value_of(merged, T0) == 2.0
    assert merged.source_states(S, T0, TEMP)["C"][0] == 2


def test_shuffled_intake_converges_to_ordered_result() -> None:
    records: list[Submission | Revocation] = [
        sub(1, "A", {(TEMP, T0): 12.0}),
        sub(2, "B", {(TEMP, T0): 12.0, (TEMP, T1): 7.0}),
        sub(3, "C", {(TEMP, T0): 13.0, (TEMP, T1): 0.0}),
        Submission(4, "C", {c(T0): obs_missing(), c(T2): obs_value(21.0)}),
        Revocation(5, 4, "C", [c(T0)]),
    ]
    ordered = IngestProgress()
    for record in records:
        if isinstance(record, Revocation):
            ordered.add_revocation(record)
        else:
            ordered.add_submission(record)

    shuffled = IngestProgress()
    for record in reversed(records):
        if isinstance(record, Revocation):
            shuffled.add_revocation(record)
        else:
            shuffled.add_submission(record)

    for variable in ordered.variables:
        np.testing.assert_array_equal(
            ordered.view(variable).status, shuffled.view(variable).status
        )
    # T0 (13 vs 12) and T1 (valid zero vs 7) are conflicts; C's restored
    # current values are still exposed per party.
    assert shuffled.source_states(S, T0, TEMP)["C"] == (3, OBS_VALUE, 13.0)
    assert shuffled.source_states(S, T1, TEMP)["C"] == (3, OBS_VALUE, 0.0)
    assert status_of(shuffled, T0) == STATE_CONFLICT
    assert status_of(shuffled, T1) == STATE_CONFLICT
    assert value_of(shuffled, T2) == 21.0


def test_copy_is_independent() -> None:
    progress = IngestProgress()
    progress.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    clone = progress.copy()
    clone.add_submission(sub(2, "C", {(TEMP, T0): 2.0}))
    assert value_of(progress, T0) == 1.0
    assert value_of(clone, T0) == 2.0


def test_per_collector_coverage_matrix() -> None:
    progress = IngestProgress()
    progress.add_submission(sub(1, "A", {(TEMP, T0): 1.0}))
    progress.add_submission(sub(2, "B", {(TEMP, T1): 2.0}))
    coverage = progress.view(TEMP).coverage
    np.testing.assert_array_equal(coverage["A"], np.array([[True, False]]))
    np.testing.assert_array_equal(coverage["B"], np.array([[False, True]]))


def test_unnamed_revocation_rejected_once_sealing_completes_locally() -> None:
    progress = IngestProgress(stations=[S], times=[T0, T1], variables=[TEMP])
    progress.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    # Revocation 2 arrives while sealing is still pending and is not named.
    progress.add_revocation(Revocation(2, 1, "C", [c(T0)]))
    progress.add_sealing_receipt(SealingReceipt(9, [1, 3]))  # record 3 missing
    assert progress.sealed_records == frozenset()
    before = progress.generation
    with pytest.raises(SealedTargetError):
        # Completing the batch seals submission 1; revocation 2 is outside it.
        progress.add_submission(sub(3, "C", {(TEMP, T1): 5.0}))
    # The rejected intake leaves the ledger unchanged.
    assert progress.generation == before
    assert 3 not in {s.record_id for s in progress.submissions}
    assert progress.sealed_records == frozenset()
    assert progress.pending_receipts[0].completed is False


def test_merge_rejects_unnamed_revocation_against_sealed_submission() -> None:
    sealed_side = IngestProgress(stations=[S], times=[T0], variables=[TEMP])
    sealed_side.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    sealed_side.add_sealing_receipt(SealingReceipt(9, [1]))

    rogue_side = IngestProgress(stations=[S], times=[T0], variables=[TEMP])
    rogue_side.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    rogue_side.add_revocation(Revocation(2, 1, "C", [c(T0)]))

    with pytest.raises(SealedTargetError):
        IngestProgress.merge(sealed_side, rogue_side)


def test_stations_are_independent_across_the_grid() -> None:
    other = "ST2"
    progress = IngestProgress(
        stations=[S, other], times=[T0, T1], variables=[TEMP]
    )
    progress.add_submission(
        Submission(1, "C", {c(T0): obs_value(1.0), (other, T1, TEMP): obs_value(2.0)})
    )
    progress.add_revocation(Revocation(2, 1, "C", [c(T0)]))

    view = progress.view(TEMP)
    assert view.stations == [S, other]
    assert view.status[0, 0] == STATE_UNCOVERED  # revoked on ST1
    assert view.status[1, 1] == STATE_VALUE and view.value[1, 1] == 2.0
    assert view.status[0, 1] == STATE_UNCOVERED
    assert view.status[1, 0] == STATE_UNCOVERED
    assert "C" not in progress.source_states(S, T0, TEMP)
    assert progress.source_states(other, T1, TEMP)["C"] == (1, OBS_VALUE, 2.0)


def test_multiple_receipts_archive_in_batches() -> None:
    progress = IngestProgress(stations=[S], times=[T0, T1], variables=[TEMP])
    progress.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    progress.add_submission(sub(2, "C", {(TEMP, T0): 2.0}))
    progress.add_revocation(Revocation(3, 2, "C", [c(T0)]))
    progress.add_submission(sub(4, "C", {(TEMP, T1): 4.0}))

    progress.add_sealing_receipt(SealingReceipt(8, [1, 2, 3]))
    assert tuple(progress.prune_archived()) == (1, 2, 3)
    assert value_of(progress, T0) == 1.0
    assert {s.record_id for s in progress.submissions} == {4}

    # A second, independent batch archives the remaining record.
    progress.add_sealing_receipt(SealingReceipt(9, [4]))
    assert tuple(progress.prune_archived()) == (4,)
    assert progress.tombstones == frozenset({1, 2, 3, 4})
    assert progress.receipts == ()


def test_merge_with_empty_progress_is_neutral() -> None:
    progress = IngestProgress(stations=[S], times=[T0], variables=[TEMP])
    progress.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))

    merged = IngestProgress.merge(progress, IngestProgress())
    np.testing.assert_array_equal(
        merged.view(TEMP).status, progress.view(TEMP).status
    )
    reverse = IngestProgress.merge(IngestProgress(), progress.copy())
    assert {s.record_id for s in reverse.submissions} == {1}
    assert reverse.view(TEMP).value[0, 0] == 1.0
    assert reverse.times == [T0]


def test_record_id_space_is_shared_between_submissions_and_revocations() -> None:
    progress = IngestProgress(stations=[S], times=[T0], variables=[TEMP])
    progress.add_submission(sub(1, "C", {(TEMP, T0): 1.0}))
    with pytest.raises(DuplicateRecordError):
        progress.add_revocation(Revocation(1, 1, "C", [c(T0)]))

    left = IngestProgress(stations=[S], times=[T0], variables=[TEMP])
    left.add_submission(sub(5, "C", {(TEMP, T0): 1.0}))
    right = IngestProgress(stations=[S], times=[T0], variables=[TEMP])
    right.add_revocation(Revocation(5, 99, "C", [c(T0)]))
    with pytest.raises(ConflictingRecordError, match="both submission and revocation"):
        IngestProgress.merge(left, right)
