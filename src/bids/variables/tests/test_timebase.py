"""Tests for center-contract time-base conversion of event tables."""

import json

import numpy as np
import pandas as pd
import pytest

from bids.layout import BIDSLayout
from bids.variables import (
    STATUS_CONVERTED,
    STATUS_KEPT,
    STATUS_MISSING,
    STATUS_NATIVE,
    STATUS_OTHER_RUN,
    STATUS_QUARANTINED,
    STATUS_RECOMPUTED,
    TimeBaseContract,
    TimeBaseContractError,
    TimeBaseHandoff,
    TimeBaseHandoffError,
    convert_event_table,
    import_timebase_handoff,
    load_variables,
    merge_handoff_table,
)

CONTRACT = {
    'format': 'bids-center-timebase-contract',
    'name': 'center-a',
    'origin': 'scanner_room_entry',
    'runs': [
        {'entities': {'subject': '01', 'task': 'shared', 'run': 1}, 'offset': 100.0},
        {'entities': {'subject': '01', 'task': 'shared', 'run': 2}, 'offset': 250.0},
    ],
}
CORRECTED = {
    'format': 'bids-center-timebase-contract',
    'name': 'center-a',
    'origin': 'scanner_room_entry',
    'runs': [
        {'entities': {'subject': '01', 'task': 'shared', 'run': 1}, 'offset': 80.0},
        {'entities': {'subject': '01', 'task': 'shared', 'run': 2}, 'offset': 250.0},
    ],
}


def make_shared_table():
    """A shared events table used for two consecutive runs.

    It contains run-1 events, run-2 events, and one row already expressed on
    the run clock.  Durations are preserved through conversion.
    """
    return pd.DataFrame(
        {
            'onset': [100.0, 110.0, 250.0, 260.0, 275.0, 25.0],
            'duration': [1.0, 2.0, 1.0, 1.5, 1.0, 1.0],
            'trial_type': ['a', 'b', 'a', 'b', 'c', 'native'],
            'center_run': [1, 1, 2, 2, 2, 2],
            'center_time_base': ['room', 'room', 'room', 'room', 'room', 'run'],
        }
    )


def target_for(run):
    return {'subject': '01', 'task': 'shared', 'run': run}


def two_run_handoff(tmp_path):
    table = make_shared_table()
    contract = TimeBaseContract(CONTRACT)
    run1 = convert_event_table(table, contract, target=target_for(1), source='shared.tsv')
    run2 = convert_event_table(table, contract, target=target_for(2), source='shared.tsv')
    handoff = TimeBaseHandoff.combine([run1.to_handoff(), run2.to_handoff()])
    path = tmp_path / 'handoff.json'
    handoff.write(path)
    return path


def test_shared_table_imports_to_two_runs_separately():
    table = make_shared_table()
    contract = TimeBaseContract(CONTRACT)
    run1 = convert_event_table(table, contract, target=target_for(1), source='shared.tsv')
    run2 = convert_event_table(table, contract, target=target_for(2), source='shared.tsv')

    # Only run-1 rows (including the native-clock row belonging to run 2).
    assert list(run1.rows['onset']) == [0.0, 10.0]
    assert list(run2.rows['onset']) == [0.0, 10.0, 25.0, 25.0]
    # The native row is not shifted.
    native = run2.reconciliation.loc[run2.reconciliation['status'] == STATUS_NATIVE]
    assert native['converted_onset'].tolist() == [25.0]
    # Durations are unchanged in every case.
    assert run1.rows.loc[[0, 1], 'duration'].tolist() == [1.0, 2.0]


def test_handoff_correcting_one_run_recomputes_from_original(tmp_path):
    path = two_run_handoff(tmp_path)
    result = import_timebase_handoff(
        path, contract=CORRECTED, target=target_for(1), source='shared.tsv'
    )

    run1 = result.reconciliation[result.reconciliation['target_run'] == 1]
    run2 = result.reconciliation[result.reconciliation['target_run'] == 2]

    # Run 1 was corrected: recomputed from the original room-clock onsets.
    assert run1['status'].tolist() == [STATUS_RECOMPUTED, STATUS_RECOMPUTED]
    assert run1['original_onset'].tolist() == [100.0, 110.0]
    assert run1['offset'].tolist() == [80.0, 80.0]
    assert run1['converted_onset'].tolist() == [20.0, 30.0]

    # Run 2 was not corrected: its recorded results are kept verbatim.
    assert set(run2['status']) == {STATUS_KEPT, STATUS_NATIVE}
    assert run2.loc[run2['status'] == STATUS_KEPT, 'original_onset'].tolist() == [
        250.0,
        260.0,
        275.0,
    ]
    assert run2.loc[run2['status'] == STATUS_KEPT, 'converted_onset'].tolist() == [
        0.0,
        10.0,
        25.0,
    ]


def test_repeated_import_keeps_matching_rows(tmp_path):
    path = two_run_handoff(tmp_path)
    first = import_timebase_handoff(path, contract=CORRECTED, target=target_for(1))
    second = import_timebase_handoff(path, contract=CORRECTED, target=target_for(1))

    keep_cols = ['row_id', 'target_run', 'original_onset', 'offset', 'converted_onset', 'status']
    pd.testing.assert_frame_equal(
        first.reconciliation[keep_cols].reset_index(drop=True),
        second.reconciliation[keep_cols].reset_index(drop=True),
    )
    # Nothing was subtracted twice: onsets are identical on the second pass.
    first_rows = first.rows.sort_values(['center_run', 'onset']).reset_index(drop=True)
    second_rows = second.rows.sort_values(['center_run', 'onset']).reset_index(drop=True)
    pd.testing.assert_frame_equal(first_rows, second_rows)


def test_negative_onset_missing_and_mislabelled_rows(tmp_path):
    path = two_run_handoff(tmp_path)
    raw = pd.DataFrame(
        {
            'onset': [75.0, np.nan, 5.0],
            'duration': [1.0, np.nan, 1.0],
            'trial_type': ['negative', 'missing', 'mislabelled'],
            'center_run': [1, 1, 2],
            'center_origin_run': [np.nan, np.nan, 1],
            'center_time_base': ['room', 'room', 'room'],
        }
    )
    result = import_timebase_handoff(
        path, raw, contract=CORRECTED, target=target_for(1), source='new.tsv'
    )

    # A legitimate negative run-relative onset is produced by a large offset.
    negative = result.reconciliation.loc[
        result.reconciliation['target_run'] == 1, 'converted_onset'
    ].dropna()
    assert -5.0 in negative.tolist()

    # A missing onset stays missing, never becomes a shifted number.
    missing = result.reconciliation[result.reconciliation['status'] == STATUS_MISSING]
    assert len(missing) == 1
    assert pd.isna(missing.iloc[0]['converted_onset'])
    assert pd.isna(result.rows.loc[missing.index[0], 'onset'])
    assert pd.isna(result.rows.loc[missing.index[0], 'duration'])

    # The run-1 record labelled as run-2 evidence is quarantined, not converted.
    quarantined = result.quarantined
    assert len(quarantined) == 1
    assert quarantined.iloc[0]['trial_type'] == 'mislabelled'
    assert quarantined.iloc[0]['timebase_reason'] == 'cross_run_evidence'
    assert 'mislabelled' not in result.rows['trial_type'].tolist()


def test_cross_run_rows_are_listed_not_converted():
    contract = TimeBaseContract(
        {
            'runs': [
                {'entities': {'subject': '01', 'task': 'shared', 'run': 1}, 'offset': 100.0},
                {'entities': {'subject': '01', 'task': 'shared', 'run': 2}, 'offset': 250.0},
            ]
        }
    )
    table = pd.DataFrame(
        {
            'onset': [100.0],
            'duration': [1.0],
            'trial_type': ['x'],
            'center_run': [1],
            'center_origin_run': [2],
            'center_time_base': ['room'],
        }
    )
    result = convert_event_table(table, contract, target=target_for(1))
    assert result.rows.empty
    assert len(result.quarantined) == 1
    assert result.reconciliation.iloc[0]['status'] == STATUS_QUARANTINED


def test_converted_row_without_original_is_quarantined():
    contract = TimeBaseContract(
        {'runs': [{'entities': {'subject': '01', 'task': 'shared', 'run': 1}, 'offset': 100.0}]}
    )
    table = pd.DataFrame(
        {
            'onset': [25.0],
            'duration': [1.0],
            'trial_type': ['x'],
            'center_run': [1],
            'center_time_base': ['room'],
            'center_converted': [True],
        }
    )
    result = convert_event_table(table, contract, target=target_for(1))
    assert result.rows.empty
    assert result.reconciliation.iloc[0]['status'] == STATUS_QUARANTINED
    assert result.reconciliation.iloc[0]['reason'] == 'insufficient_evidence'


def test_already_converted_row_keeps_recorded_result():
    contract = TimeBaseContract(
        {'runs': [{'entities': {'subject': '01', 'task': 'shared', 'run': 1}, 'offset': 100.0}]}
    )
    table = pd.DataFrame(
        {
            'onset': [10.0],
            'duration': [1.0],
            'trial_type': ['x'],
            'center_run': [1],
            'center_time_base': ['room'],
            'center_converted': [True],
            'center_original_onset': [110.0],
            'center_offset': [100.0],
        }
    )
    result = convert_event_table(table, contract, target=target_for(1))
    assert result.rows.iloc[0]['onset'] == 10.0
    assert result.reconciliation.iloc[0]['status'] == STATUS_KEPT


def test_get_df_and_load_variables_share_conversion(tmp_path):
    root = tmp_path / 'ds'
    func = root / 'sub-01' / 'func'
    func.mkdir(parents=True)

    # A minimal dataset with a shared events table used by two runs.
    table = make_shared_table()
    table.to_csv(func / 'sub-01_task-shared_events.tsv', sep='\t', index=False)
    for run in (1, 2):
        with open(func / f'sub-01_task-shared_run-0{run}_bold.json', 'w') as handle:
            json.dump({'RepetitionTime': 2.0}, handle)
        import nibabel as nb

        nb.Nifti1Image(np.zeros((2, 2, 2, 10)), np.eye(4)).to_filename(
            func / f'sub-01_task-shared_run-0{run}_bold.nii.gz'
        )

    contract = TimeBaseContract(CONTRACT)

    # Table read for run 1 only sees run-1 rows, using the same converter.
    events_file = func / 'sub-01_task-shared_events.tsv'
    layout = BIDSLayout(root, validate=False)
    bf = layout.get(suffix='events', extension='tsv')[0]
    df = bf.get_df(timebase=contract, target=target_for(1))
    assert list(df['onset']) == [0.0, 10.0]

    index = load_variables(
        layout, types='events', scan_length=20, timebase=contract, subject='01', task='shared'
    )
    runs = index.get_nodes('run', {'subject': '01', 'task': 'shared'})
    run1 = [r for r in runs if int(r.entities['run']) == 1][0]
    var = run1.variables['trial_type']
    assert list(var.onset) == [0.0, 10.0]

    # Source TSV untouched.
    raw = pd.read_csv(events_file, sep='\t')
    assert list(raw['onset']) == [100.0, 110.0, 250.0, 260.0, 275.0, 25.0]


def test_handoff_as_table_then_layout_reopen(tmp_path):
    """Re-import the handoff's two runs plus new rows through the table route."""
    table = make_shared_table()
    contract = TimeBaseContract(CONTRACT)
    run1 = convert_event_table(table, contract, target=target_for(1), source='shared.tsv')
    run2 = convert_event_table(table, contract, target=target_for(2), source='shared.tsv')
    handoff = TimeBaseHandoff.combine([run1.to_handoff(), run2.to_handoff()])

    # Merge the handoff's converted rows with newly received raw rows.
    converted_frame = handoff.to_frame()
    new_raw = pd.DataFrame(
        {
            'onset': [75.0, np.nan, 5.0],
            'duration': [1.0, np.nan, 1.0],
            'trial_type': ['negative', 'missing', 'mislabelled'],
            'center_run': [1, 1, 2],
            'center_origin_run': [np.nan, np.nan, 1],
            'center_time_base': ['room', 'room', 'room'],
        }
    )
    combined = pd.concat([converted_frame, new_raw], ignore_index=True)
    combined.to_csv(tmp_path / 'sub-01_task-shared_events.tsv', sep='\t', index=False)

    root = tmp_path / 'ds'
    func = root / 'sub-01' / 'func'
    func.mkdir(parents=True)
    combined.to_csv(func / 'sub-01_task-shared_events.tsv', sep='\t', index=False)
    for run in (1, 2):
        with open(func / f'sub-01_task-shared_run-0{run}_bold.json', 'w') as handle:
            json.dump({'RepetitionTime': 2.0}, handle)
        import nibabel as nb

        nb.Nifti1Image(np.zeros((2, 2, 2, 10)), np.eye(4)).to_filename(
            func / f'sub-01_task-shared_run-0{run}_bold.nii.gz'
        )

    corrected = TimeBaseContract(CORRECTED)
    layout = BIDSLayout(root, validate=False)
    bf = layout.get(suffix='events', extension='tsv')[0]
    df = bf.get_df(timebase=corrected, target=target_for(1))
    audit = bf.timebase_result.reconciliation

    run1 = audit[audit['target_run'] == 1]
    assert set(run1['status']) == {STATUS_RECOMPUTED, STATUS_CONVERTED, STATUS_MISSING}
    # Recomputed from the original onsets, including the negative result.
    assert sorted(run1['converted_onset'].dropna().tolist()) == [-5.0, 20.0, 30.0]
    assert (
        run1.loc[run1['status'] == STATUS_RECOMPUTED, 'original_onset'] == [100.0, 110.0]
    ).all()
    assert list(df['onset'][df['trial_type'] == 'negative']) == [-5.0]
    assert df.loc[df['trial_type'] == 'missing', 'onset'].isna().all()

    # Run 2 rows are listed as other-run and not converted with run 1's offset.
    run2 = audit[(audit['target_run'] == 2) & (audit['status'] != STATUS_QUARANTINED)]
    assert set(run2['status']) == {STATUS_OTHER_RUN}

    # The run-1 record labelled as run-2 evidence is quarantined.
    bad = audit[audit['reason'] == 'cross_run_evidence']
    assert len(bad) == 1
    assert bad.iloc[0]['target_run'] == 2
    assert 'mislabelled' not in df['trial_type'].tolist()

    # The reconciliation table exposes original values, the used offset, the
    # target run, and the sidecar source.
    for column in ('raw_onset', 'original_onset', 'target_run', 'offset', 'sidecar'):
        assert column in audit.columns
    recomputed = audit[audit['status'] == STATUS_RECOMPUTED]
    assert (recomputed['offset'] == 80.0).all()
    assert (recomputed['original_onset'] == recomputed['raw_onset']).all()
    assert (recomputed['target_run'] == 1).all()
    assert (recomputed['duration'] == [1.0, 2.0]).all()

    # Reopen the Layout from disk and query again: results are stable.
    reopened = BIDSLayout(root, validate=False)
    df2 = reopened.get(suffix='events', extension='tsv')[0].get_df(
        timebase=corrected, target=target_for(1)
    )
    pd.testing.assert_frame_equal(df, df2)

    index = load_variables(
        reopened, types='events', scan_length=20, timebase=corrected, subject='01', task='shared'
    )
    runs = index.get_nodes('run', {'subject': '01', 'task': 'shared'})
    run1_node = [r for r in runs if int(r.entities['run']) == 1][0]
    trial_type = run1_node.variables['trial_type']
    onsets = np.asarray(trial_type.onset, dtype=float)
    assert sorted(onsets[~np.isnan(onsets)].tolist()) == [-5.0, 20.0, 30.0]
    assert np.isnan(onsets).sum() == 1
    # Contract provenance columns are not exposed as model variables.
    assert not {'center_run', 'center_offset', 'center_time_base'} & set(run1_node.variables)


def test_contract_ambiguity_and_file_roundtrip(tmp_path):
    ambiguous = {
        'runs': [
            {'entities': {'subject': '01', 'run': 1}, 'offset': 10.0},
            {'entities': {'subject': '01', 'run': 1}, 'offset': 20.0},
        ]
    }
    contract = TimeBaseContract(ambiguous)
    with pytest.raises(TimeBaseContractError):
        contract.resolve_offset({'subject': '01', 'run': 1})

    path = tmp_path / 'contract.json'
    path.write_text(json.dumps(CONTRACT))
    loaded = TimeBaseContract.from_file(path)
    assert loaded.sha256 is not None
    assert loaded.resolve_offset(target_for(1)).offset == 100.0


def test_converted_frame_has_provenance_and_does_not_touch_source():
    table = make_shared_table()
    contract = TimeBaseContract(CONTRACT)
    result = convert_event_table(table, contract, target=target_for(1), source='shared.tsv')
    frame = result.to_frame(include_provenance=True)
    assert 'timebase_status' in frame.columns
    assert 'timebase_original_onset' in frame.columns
    assert 'center_offset' in frame.columns
    # Original table unchanged.
    assert list(table['onset']) == [100.0, 110.0, 250.0, 260.0, 275.0, 25.0]


def test_handoff_replay_without_contract_keeps_recorded_results():
    table = make_shared_table()
    contract = TimeBaseContract(CONTRACT)
    result = convert_event_table(table, contract, target=target_for(1), source='shared.tsv')
    payload = result.to_handoff().to_dict()
    payload['contract'] = {}
    replayed = import_timebase_handoff(TimeBaseHandoff.from_dict(payload))
    assert set(replayed.reconciliation['status']) == {STATUS_KEPT}
    assert replayed.rows['onset'].tolist() == [0.0, 10.0]


def test_combine_rejects_conflicting_contracts():
    table = make_shared_table()
    original = TimeBaseContract(CONTRACT)
    conflicting = TimeBaseContract(CORRECTED)
    first = convert_event_table(table, original, target=target_for(1)).to_handoff()
    second = convert_event_table(table, conflicting, target=target_for(1)).to_handoff()
    with pytest.raises(TimeBaseHandoffError):
        TimeBaseHandoff.combine([first, second])


def test_handoff_to_frame_restores_original_onset():
    table = make_shared_table()
    contract = TimeBaseContract(CONTRACT)
    result = convert_event_table(table, contract, target=target_for(1), source='shared.tsv')
    handoff = result.to_handoff()
    frame = handoff.to_frame()
    # `onset` is restored to the room-clock value; the result is in the
    # provenance column so a re-import can recompute rather than re-subtract.
    assert frame['onset'].tolist() == [100.0, 110.0]
    assert frame['timebase_converted_onset'].tolist() == [0.0, 10.0]
    assert frame['timebase_original_onset'].tolist() == [100.0, 110.0]
    assert frame['center_converted'].tolist() == [True, True]


def test_handoff_mislabelled_row_is_quarantined():
    contract = TimeBaseContract(CONTRACT)
    handoff = TimeBaseHandoff(
        contract,
        rows=[
            {
                'data': {'onset': 5.0, 'duration': 1.0, 'trial_type': 'bad'},
                'target': {'run': 2},
                'origin_run': {'run': 1},
                'status': 'converted',
                'original_onset': 105.0,
                'offset': 100.0,
                'onset': 5.0,
                'evidence': {'sidecar': 'center.json', 'source_file': 'f.tsv', 'source_row': 0},
            },
            {
                'data': {'onset': 0.0, 'duration': 1.0, 'trial_type': 'good'},
                'target': {'run': 1},
                'origin_run': {'run': 1},
                'status': 'converted',
                'original_onset': 100.0,
                'offset': 100.0,
                'onset': 0.0,
                'evidence': {'sidecar': 'center.json', 'source_file': 'f.tsv', 'source_row': 1},
            },
        ],
    )
    result = import_timebase_handoff(handoff, contract=contract)
    assert result.rows['trial_type'].tolist() == ['good']
    assert result.quarantined['trial_type'].tolist() == ['bad']
    assert result.reconciliation.iloc[0]['reason'] == 'cross_run_evidence'


def test_no_declaration_keeps_only_fully_evidenced_rows():
    contract = TimeBaseContract({'runs': [{'entities': {'run': 2}, 'offset': 250.0}]})
    handoff = TimeBaseHandoff(
        contract,
        rows=[
            {
                'data': {'onset': 0.0, 'duration': 1.0, 'trial_type': 'evidenced'},
                'target': {'run': 1},
                'status': 'converted',
                'original_onset': 100.0,
                'offset': 100.0,
                'onset': 0.0,
                'evidence': {'sidecar': 'old.json', 'source_file': 'f.tsv', 'source_row': 0},
            },
            {
                'data': {'onset': 0.0, 'duration': 1.0, 'trial_type': 'no_offset'},
                'target': {'run': 1},
                'status': 'converted',
                'original_onset': 100.0,
                'onset': 0.0,
                'evidence': {'sidecar': 'old.json', 'source_file': 'f.tsv', 'source_row': 1},
            },
        ],
    )
    result = import_timebase_handoff(handoff, contract=contract)
    assert result.rows['trial_type'].tolist() == ['evidenced']
    assert result.quarantined['trial_type'].tolist() == ['no_offset']
    kept = result.reconciliation.loc[0]
    assert kept['status'] == STATUS_KEPT
    assert kept['sidecar'] == 'old.json'


def test_merge_handoff_table_is_reimportable(tmp_path):
    table = make_shared_table()
    contract = TimeBaseContract(CONTRACT)
    run1 = convert_event_table(table, contract, target=target_for(1), source='shared.tsv')
    run2 = convert_event_table(table, contract, target=target_for(2), source='shared.tsv')
    handoff = TimeBaseHandoff.combine([run1.to_handoff(), run2.to_handoff()])
    raw = pd.DataFrame(
        {
            'onset': [75.0],
            'duration': [1.0],
            'trial_type': ['negative'],
            'center_run': [1],
            'center_time_base': ['room'],
        }
    )
    merged = merge_handoff_table(handoff, raw)
    # Original onsets are restored for converted rows, and raw rows are intact.
    assert merged.loc[merged['trial_type'] == 'a', 'onset'].tolist() == [100.0, 250.0]
    assert merged.loc[merged['trial_type'] == 'negative', 'onset'].tolist() == [75.0]

    result = convert_event_table(
        merged, TimeBaseContract(CORRECTED), target=target_for(1), source='merged.tsv'
    )
    assert sorted(result.reconciliation['converted_onset'].dropna().tolist()) == [-5.0, 20.0, 30.0]
    assert result.reconciliation.iloc[0]['status'] == STATUS_RECOMPUTED
