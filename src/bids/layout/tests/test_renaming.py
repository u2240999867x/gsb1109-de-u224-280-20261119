"""Tests for batch path-renaming previews."""

from bids.layout.renaming import plan_renames


def _func(**entities):
    base = {'subject': '01', 'task': 'rest', 'datatype': 'func'}
    base.update(entities)
    return base


INLINE = (
    'sub-{subject}/func/sub-{subject}_task-{task}[_acq-{acquisition}]'
    '[_run-{run}]_{suffix}{extension}'
)
BY_ACQ_DIR = (
    'sub-{subject}/func/acq-{acquisition}/sub-{subject}_task-{task}'
    '[_run-{run}]_{suffix}{extension}'
)
BY_TASK_DIR = (
    'sub-{subject}/func/task-{task}/sub-{subject}[_acq-{acquisition}]'
    '[_run-{run}]_{suffix}{extension}'
)
TASK_THEN_ACQ = (
    'sub-{subject}/func/task-{task}/acq-{acquisition}/sub-{subject}'
    '[_run-{run}]_{suffix}{extension}'
)
TASK_ONLY = 'sub-{subject}/func/task-{task}/sub-{subject}_{suffix}{extension}'


def test_preview_does_not_touch_filesystem(tmp_path):
    source = tmp_path / 'sub-01' / 'func' / 'sub-01_task-rest_bold.nii.gz'
    source.parent.mkdir(parents=True)
    source.write_text('data')

    plan = plan_renames(
        {str(source): _func(suffix='bold', extension='.nii.gz')},
        ['sub-{subject}/func/renamed/sub-{subject}_task-{task}_{suffix}{extension}'],
        config='bids',
    )
    assert plan.feasible
    assert source.exists()
    assert not (tmp_path / 'sub-01' / 'func' / 'renamed').exists()


def test_shared_sidecar_and_dedicated_events_stay_with_runs():
    shared = 'sub-01/func/sub-01_task-rest_bold.json'
    event = 'sub-01/func/sub-01_task-rest_acq-A_run-1_events.tsv'
    image_a = 'sub-01/func/sub-01_task-rest_acq-A_run-1_bold.nii.gz'
    image_b = 'sub-01/func/sub-01_task-rest_acq-B_run-1_bold.nii.gz'
    sources = {
        shared: _func(suffix='bold', extension='.json'),
        event: _func(acquisition='A', run='1', suffix='events', extension='.tsv'),
        image_a: _func(acquisition='A', run='1', suffix='bold', extension='.nii.gz'),
        image_b: _func(acquisition='B', run='1', suffix='bold', extension='.nii.gz'),
    }

    plan = plan_renames(sources, [INLINE, BY_ACQ_DIR], config='bids')
    assert plan.feasible
    # The shared sidecar remains the common metadata ancestor of both runs.
    assert plan.mapping[shared] == shared
    assert ('metadata', image_a, shared) in plan.associations_after
    assert ('metadata', image_b, shared) in plan.associations_after
    # The events table still only intends run A.
    assert ('intended', event, image_a) in plan.associations_after
    assert ('intended', event, image_b) not in plan.associations_after


def test_global_priority_beats_per_file_greedy():
    shared = 'sub-01/func/sub-01_task-rest_bold.json'
    image_a = 'sub-01/func/sub-01_task-rest_acq-A_run-1_bold.nii.gz'
    image_b = 'sub-01/func/sub-01_task-rest_acq-B_run-1_bold.nii.gz'
    sources = {
        shared: _func(suffix='bold', extension='.json'),
        image_a: _func(acquisition='A', run='1', suffix='bold', extension='.nii.gz'),
        image_b: _func(acquisition='B', run='1', suffix='bold', extension='.nii.gz'),
    }

    # The first pattern would send each image to its own acq directory, which
    # breaks the shared sidecar's common-ancestor relation.  A feasible batch
    # exists only one pattern later, and the planner must find it rather than
    # committing each file to its own first choice.
    breaking = (
        'sub-{subject}/func/acq-{acquisition}/sub-{subject}_task-{task}'
        '_run-{run}_{suffix}{extension}'
    )
    preserving = TASK_THEN_ACQ
    shared_pattern = TASK_ONLY
    plan = plan_renames(
        sources,
        [breaking, preserving, shared_pattern],
        config='bids',
    )

    assert plan.feasible
    assert plan.moves[image_a].priority == 1
    assert plan.moves[image_b].priority == 1
    abandoned = [c for c in plan.conflicts if c.kind == 'abandoned']
    assert any(c.source == image_a for c in abandoned)
    assert any(
        'metadata relation' in c.detail or 'association hit set' in c.detail for c in abandoned
    )
    assert plan.associations_before <= plan.associations_after


def test_occupied_path_in_lower_priority_pattern_is_reported():
    source = 'sub-01/func/sub-01_task-rest_acq-A_run-01_bold.nii.gz'
    sources = {source: _func(acquisition='A', run='01', suffix='bold', extension='.nii.gz')}
    # Both patterns force a real move, so neither can coincide with the source.
    high = (
        'sub-{subject}/func/preferred/sub-{subject}_task-{task}'
        '_acq-{acquisition}_run-{run}_{suffix}{extension}'
    )
    low = BY_ACQ_DIR
    high_path = 'sub-01/func/preferred/sub-01_task-rest_acq-A_run-01_bold.nii.gz'
    low_path = 'sub-01/func/acq-A/sub-01_task-rest_run-01_bold.nii.gz'

    # The high-priority destination is available; only the low-priority
    # alternative is seated on an existing, non-moving file.
    plan = plan_renames(sources, [high, low], config='bids', occupied=[low_path])
    assert plan.feasible
    assert plan.mapping[source] == high_path
    assert any(c.kind == 'occupied' and c.blockers == (low_path,) for c in plan.conflicts)

    # With both alternatives occupied there is no path left for the batch.
    plan = plan_renames(
        sources,
        [high, low],
        config='bids',
        occupied=[high_path, low_path],
    )
    assert not plan.feasible
    assert plan.moves == {}
    kinds = {c.kind for c in plan.conflicts}
    assert 'occupied' in kinds
    assert any(c.blockers == (low_path,) for c in plan.conflicts)
    assert plan.blocking_sources == (source,)
    assert 'occupied' in plan.explain()


def test_leading_zeros_and_default_entities_are_distinguishable():
    no_run = 'sub-01/func/sub-01_task-rest_bold.nii.gz'
    run_one = 'sub-01/func/sub-01_task-rest_run-1_bold.nii.gz'
    run_zero_one = 'sub-01/func/sub-01_task-rest_run-01_bold.nii.gz'
    sources = {
        no_run: _func(suffix='bold', extension='.nii.gz'),
        run_one: _func(run='1', suffix='bold', extension='.nii.gz'),
        run_zero_one: _func(run='01', suffix='bold', extension='.nii.gz'),
    }
    pattern = 'sub-{subject}/func/sub-{subject}_task-{task}[_run-{run}]_{suffix|bold}{extension}'

    plan = plan_renames(sources, [pattern], config='bids', occupied=list(sources))
    assert plan.feasible
    assert str(plan.moves[run_one].readback['run']) == '1'
    assert str(plan.moves[run_zero_one].readback['run']) == '01'
    assert 'run' not in plan.moves[no_run].readback
    assert 'run' not in plan.moves[no_run].defaults

    # Reading back through the target config distinguishes the two runs.
    assert str(plan.moves[run_one].readback['run']) != str(
        plan.moves[run_zero_one].readback['run']
    )


def test_pattern_default_entity_is_reported_and_readable():
    source = 'sub-01/func/sub-01_task-rest_bold.nii.gz'
    sources = {source: _func(suffix='bold', extension='.nii.gz', run='')}
    pattern = 'sub-{subject}/func/sub-{subject}_task-{task}_run-{run|01}_{suffix}{extension}'
    plan = plan_renames(sources, [pattern], config='bids', occupied=[source])
    assert plan.feasible
    move = plan.moves[source]
    assert move.defaults == {'run': '01'}
    assert str(move.readback['run']) == '01'


def test_input_order_does_not_change_the_business_choice():
    shared = 'sub-01/func/sub-01_task-rest_bold.json'
    image_a = 'sub-01/func/sub-01_task-rest_acq-A_run-1_bold.nii.gz'
    image_b = 'sub-01/func/sub-01_task-rest_acq-B_run-1_bold.nii.gz'
    entries = {
        shared: _func(suffix='bold', extension='.json'),
        image_a: _func(acquisition='A', run='1', suffix='bold', extension='.nii.gz'),
        image_b: _func(acquisition='B', run='1', suffix='bold', extension='.nii.gz'),
    }
    patterns = [BY_ACQ_DIR, TASK_THEN_ACQ, TASK_ONLY]

    plans = [
        plan_renames(dict(reversed(list(entries.items()))), patterns, config='bids'),
        plan_renames(entries, patterns, config='bids'),
    ]
    assert plans[0].mapping == plans[1].mapping
    assert plans[0].patterns_used == plans[1].patterns_used


def test_lower_priority_subset_is_reported_when_complete_batch_impossible():
    image = 'sub-01/func/sub-01_task-rest_bold.nii.gz'
    sources = {image: _func(suffix='bold', extension='.nii.gz')}
    high = 'sub-{subject}/func/sub-{subject}_task-{task}_acq-{acquisition}_{suffix}{extension}'
    low = 'sub-{subject}/func/task-{task}/sub-{subject}_{suffix}{extension}'
    # Both patterns require an acquisition value the source does not have, so
    # every combination is blocked before occupancy is even considered.
    occupied = [
        'sub-01/func/sub-01_task-rest_acq-A_bold.nii.gz',
        'sub-01/func/task-rest/sub-01_bold.nii.gz',
    ]
    plan = plan_renames(sources, [high, low], config='bids', occupied=occupied)
    assert not plan.feasible
    assert plan.mapping == {}
    assert any(c.kind == 'no-candidate' for c in plan.conflicts)


def test_real_layout_preserves_shared_sidecar_and_dedicated_physio():
    from pathlib import Path

    from bids import BIDSLayout
    from bids.tests import get_test_data_path

    root = Path(get_test_data_path()) / '7t_trt'
    layout = BIDSLayout(str(root))
    sources = [
        'sub-01/ses-1/func/sub-01_ses-1_task-rest_acq-fullbrain_run-1_bold.nii.gz',
        'sub-01/ses-1/func/sub-01_ses-1_task-rest_acq-fullbrain_run-1_bold.json',
        'sub-01/ses-1/func/sub-01_ses-1_task-rest_acq-fullbrain_run-1_physio.tsv.gz',
    ]
    pattern = (
        'sub-{subject}[/ses-{session}]/{datatype<func>|func}/acq-{acquisition}'
        '/sub-{subject}[_ses-{session}]_task-{task}[_run-{run}]_{suffix}{extension}'
    )
    plan = plan_renames(sources, [pattern], config='bids', layout=layout, root=root)

    assert plan.feasible
    assert plan.mapping[sources[0]] == (
        'sub-01/ses-1/func/acq-fullbrain/sub-01_ses-1_task-rest_run-1_bold.nii.gz'
    )
    # The shared task-level sidecar is still an ancestor of this run.
    shared = 'task-rest_acq-fullbrain_bold.json'
    assert ('metadata', sources[0], shared) in plan.associations_before
    assert ('metadata', sources[0], shared) in plan.associations_after
    # The dedicated physio file only ever intended this run, and still does.
    assert ('intended', sources[2], sources[0]) in plan.associations_after
    # JSON-content IntendedFor is reported because path choices cannot fix it.
    assert any('JSON content' in note for note in plan.notes)


def test_candidate_space_and_all_destinations_blocked():
    source = 'sub-01/func/sub-01_task-rest_bold.nii.gz'
    sources = {source: _func(acquisition='B', suffix='bold', extension='.nii.gz')}
    high = 'sub-{subject}/func/acq-{acquisition}/sub-{subject}_task-{task}_{suffix}{extension}'
    low = (
        'sub-{subject}/func/legacy/acq-{acquisition}/sub-{subject}_task-{task}_{suffix}{extension}'
    )
    high_path = 'sub-01/func/acq-B/sub-01_task-rest_bold.nii.gz'
    low_path = 'sub-01/func/legacy/acq-B/sub-01_task-rest_bold.nii.gz'
    occupants = {
        high_path: {'subject': '02', 'suffix': 'bold', 'extension': '.nii.gz'},
        low_path: {'subject': '02', 'task': 'other', 'suffix': 'bold', 'extension': '.nii.gz'},
    }

    plan = plan_renames(
        sources,
        [high, low],
        config='bids',
        occupied=list(occupants),
        fixed=occupants,
    )
    assert not plan.feasible
    assert plan.mapping == {}
    assert plan.blocking_sources == (source,)
    # Every destination is exposed, marked unavailable, with its owner named.
    options = plan.to_dict()['candidates'][source]
    assert {option['path'] for option in options} == {high_path, low_path}
    assert all(not option['available'] for option in options)
    assert {option['blocker'] for option in options} == {high_path, low_path}
    assert 'occupied' in plan.explain()
