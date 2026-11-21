"""Tests for incremental JSON-sidecar metadata refresh.

These tests exercise ``BIDSLayout.refresh_metadata`` without re-scanning any
data files. They cover edited values, deletion that restores inheritance,
explicit ``null`` overrides, newly added sidecars, IntendedFor reference
changes, unaffected tasks, malformed-sidecar atomicity, and equivalence with a
from-scratch index over the same on-disk inputs.
"""

import json
import os

import pytest

from bids.layout import BIDSLayout, BIDSLayoutIndexer
from bids.layout.models import FileAssociation


DATASET_DESCRIPTION = {'Name': 'Refresh', 'BIDSVersion': '1.8.0'}

PARENT_SIDECAR = {
    'Units': 'parent-unit',
    'RevealedKey': 'parent-revealed',
    'NullKey': 'parent-nullval',
    'SharedKey': 'parent-shared',
    'ParentOnly': 'parent-only-val',
}

NEAR_SIDECAR_INITIAL = {
    'Units': 'old-unit',
    'RevealedKey': 'near-revealed',
    'NullKey': 'near-nullval',
    'IntendedFor': ['anat/sub-01_T1w.nii.gz'],
}

NEAR_SIDECAR_EDITED = {
    'Units': 'new-unit',
    'NullKey': None,
}

RUN2_SIDECAR = {
    'Units': 'run2-specific-unit',
    'SpecificKey': 'specific-val',
}


def _write(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, 'w', encoding='utf-8') as handle:
        json.dump(payload, handle)


def _build_dataset(root):
    _write(root / 'dataset_description.json', DATASET_DESCRIPTION)

    func = root / 'sub-01' / 'func'
    anat = root / 'sub-01' / 'anat'
    func.mkdir(parents=True)
    anat.mkdir(parents=True)

    # Shared task-level sidecar inherited by both task-one runs.
    _write(root / 'task-one_bold.json', PARENT_SIDECAR)
    # Near sidecar for the first run; masks parent values and holds an intent.
    _write(func / 'sub-01_task-one_run-1_bold.json', NEAR_SIDECAR_INITIAL)

    # Data files are only ever touched (created) here, never rewritten.
    run1 = func / 'sub-01_task-one_run-1_bold.nii.gz'
    run2 = func / 'sub-01_task-one_run-2_bold.nii.gz'
    # Second task sits in the same directory but has no sidecar / inheritance.
    run_other = func / 'sub-01_task-two_run-1_bold.nii.gz'
    target = anat / 'sub-01_T1w.nii.gz'
    for data_file in (run1, run2, run_other, target):
        data_file.touch()

    return {
        'root': root,
        'parent': root / 'task-one_bold.json',
        'near': func / 'sub-01_task-one_run-1_bold.json',
        'run2_sidecar': func / 'sub-01_task-one_run-2_bold.json',
        'other_sidecar': func / 'sub-01_task-two_run-1_bold.json',
        'run1': run1,
        'run2': run2,
        'run_other': run_other,
        'target': target,
    }


def _rel(layout_root, path):
    return os.path.relpath(str(path), str(layout_root))


def _snapshot(layout):
    """Capture every query-observable fact keyed by relative file path."""
    root = layout.root

    # Association rows include both directional edges exactly as stored.
    outgoing = {}
    for row in layout.session.query(FileAssociation).all():
        outgoing.setdefault(row.src, set()).add((_rel(root, row.dst), row.kind))

    snap = {}
    for bf in layout.get():
        rel = _rel(root, bf.path)
        snap[rel] = {
            'entities': dict(bf.entities),
            'metadata': bf.get_metadata(),
            'associations': outgoing.get(bf.path, set()),
        }
    return snap


def _assert_equivalent_to_fresh(layout, root):
    assert _snapshot(layout) == _snapshot(BIDSLayout(root, validate=False))


def test_refresh_edits_null_inheritance_and_intent(tmp_path):
    paths = _build_dataset(tmp_path)
    layout = BIDSLayout(tmp_path, validate=False)

    run1 = layout.get_file(paths['run1'])
    run2 = layout.get_file(paths['run2'])
    run_other = layout.get_file(paths['run_other'])

    # Baseline: near override masks parent values; bidirectional intent exists.
    baseline_md = run1.get_metadata()
    assert baseline_md['Units'] == 'old-unit'
    assert baseline_md['RevealedKey'] == 'near-revealed'
    assert baseline_md['NullKey'] == 'near-nullval'
    assert run_other.get_metadata() == {}
    assert str(paths['target']) in {a.path for a in run1.get_associations('IntendedFor')}
    assert run1.path in {
        a.path for a in layout.get_file(paths['target']).get_associations('InformedBy')
    }

    # Data files are never rewritten; hold their identity and timestamps.
    data_files = [bf.path for bf in layout.get() if bf.path.endswith('.nii.gz')]
    mtimes = {path: os.path.getmtime(path) for path in data_files}

    _write(paths['near'], NEAR_SIDECAR_EDITED)
    _write(paths['run2_sidecar'], RUN2_SIDECAR)

    summary = layout.refresh_metadata([paths['near'], paths['run2_sidecar']])

    # Scope is limited to the two recent runs; the other task is untouched.
    assert sorted(_rel(tmp_path, p) for p in summary['affected_data']) == sorted(
        [_rel(tmp_path, paths['run1']), _rel(tmp_path, paths['run2'])]
    )
    assert sorted(_rel(tmp_path, p) for p in summary['added_sidecars']) == [
        _rel(tmp_path, paths['run2_sidecar'])
    ]
    assert [bf.path for bf in layout.get() if bf.path.endswith('.nii.gz')] == data_files
    assert all(os.path.getmtime(p) == mtimes[p] for p in data_files)

    md1 = run1.get_metadata()
    # Deleted override reveals the parent value.
    assert md1['RevealedKey'] == 'parent-revealed'
    # Explicit null masks the parent value; no tag is published.
    assert 'NullKey' not in md1
    assert 'NullKey' not in run1.get_entities(metadata=True)
    # Edited value is effective; shared/parent keys remain inherited.
    assert md1['Units'] == 'new-unit'
    assert md1['SharedKey'] == 'parent-shared'
    assert md1['ParentOnly'] == 'parent-only-val'

    # The sibling run now uses its brand-new near sidecar plus the shared parent.
    md2 = run2.get_metadata()
    assert md2['Units'] == 'run2-specific-unit'
    assert md2['SpecificKey'] == 'specific-val'
    assert md2['RevealedKey'] == 'parent-revealed'
    assert md2['NullKey'] == 'parent-nullval'

    # The unaffected task keeps its old (empty) result.
    assert run_other.get_metadata() == {}

    # Removed reference drops both directional links; metadata link survives.
    assert str(paths['target']) not in {a.path for a in run1.get_associations('IntendedFor')}
    assert run1.path not in {
        a.path for a in layout.get_file(paths['target']).get_associations('InformedBy')
    }
    assert str(paths['near']) in {a.path for a in run1.get_associations('Metadata')}

    # Newly added sidecar now has index edges of its own.
    assert layout.get_file(paths['run2_sidecar']) is not None
    assert run2.path in {
        a.path for a in layout.get_file(paths['run2_sidecar']).get_associations('Metadata')
    }

    # Stale values are gone from metadata-backed queries; new values resolve.
    assert not layout.get(RevealedKey='near-revealed')
    assert not layout.get(Units='old-unit')
    assert {_rel(tmp_path, f.path) for f in layout.get(Units='new-unit')} == {
        _rel(tmp_path, paths['run1'])
    }
    assert {_rel(tmp_path, f.path) for f in layout.get(SpecificKey='specific-val')} == {
        _rel(tmp_path, paths['run2'])
    }

    _assert_equivalent_to_fresh(layout, tmp_path)


def test_refresh_matches_fresh_index_for_full_refresh(tmp_path):
    paths = _build_dataset(tmp_path)
    layout = BIDSLayout(tmp_path, validate=False)

    # A no-argument refresh treats every indexed sidecar as changed and must
    # reproduce a from-scratch index exactly.
    layout.refresh_metadata()
    _assert_equivalent_to_fresh(layout, tmp_path)


def test_refresh_deleted_sidecar_reverts_to_parent(tmp_path):
    paths = _build_dataset(tmp_path)
    layout = BIDSLayout(tmp_path, validate=False)
    run1 = layout.get_file(paths['run1'])

    assert run1.get_metadata()['Units'] == 'old-unit'
    os.remove(paths['near'])
    summary = layout.refresh_metadata([paths['near']])

    assert summary['affected_data'] == [run1.path]
    reverted = run1.get_metadata()
    assert reverted['Units'] == 'parent-unit'
    assert reverted['RevealedKey'] == 'parent-revealed'
    assert reverted['NullKey'] == 'parent-nullval'
    # The deleted sidecar record and its edges are gone.
    assert layout.get_file(paths['near']) is None
    _assert_equivalent_to_fresh(layout, tmp_path)


def test_refresh_unparseable_sidecar_publishes_nothing(tmp_path):
    paths = _build_dataset(tmp_path)
    layout = BIDSLayout(tmp_path, validate=False)
    before = _snapshot(layout)

    paths['other_sidecar'].write_text('{ not valid json ,,, }')
    with pytest.raises(OSError):
        layout.refresh_metadata([paths['other_sidecar']])

    # The previous version remains in effect; no mixed index is published.
    assert _snapshot(layout) == before
    assert layout.get_file(paths['other_sidecar']) is None
    assert layout.get_file(paths['run_other']).get_metadata() == {}


def test_refresh_parent_sidecar_edit_recomputes_all_dependents(tmp_path):
    paths = _build_dataset(tmp_path)
    layout = BIDSLayout(tmp_path, validate=False)

    _write(
        paths['parent'],
        {**PARENT_SIDECAR, 'SharedKey': 'parent-shared-2', 'BrandNew': 'brand-new'},
    )
    summary = layout.refresh_metadata([paths['parent']])

    # A parent sidecar has no direct Metadata edges, but both dependent runs
    # must still be recomputed from the newly applicable match.
    assert sorted(summary['affected_data']) == sorted(
        [layout.get_file(paths['run1']).path, layout.get_file(paths['run2']).path]
    )
    assert layout.get_file(paths['run1']).get_metadata()['SharedKey'] == 'parent-shared-2'
    assert layout.get_file(paths['run2']).get_metadata()['BrandNew'] == 'brand-new'
    # The near override still wins for run1's units.
    assert layout.get_file(paths['run1']).get_metadata()['Units'] == 'old-unit'
    _assert_equivalent_to_fresh(layout, tmp_path)


def test_refresh_metadata_disabled_raises(tmp_path):
    _build_dataset(tmp_path)
    layout = BIDSLayout(
        tmp_path, validate=False, indexer=BIDSLayoutIndexer(index_metadata=False)
    )
    with pytest.raises(RuntimeError):
        layout.refresh_metadata()


def test_refresh_accepts_relative_sidecar_path(tmp_path):
    paths = _build_dataset(tmp_path)
    layout = BIDSLayout(tmp_path, validate=False)

    _write(paths['near'], NEAR_SIDECAR_EDITED)
    relative = _rel(tmp_path, paths['near'])
    layout.refresh_metadata([relative])

    assert layout.get_file(paths['run1']).get_metadata()['Units'] == 'new-unit'
    _assert_equivalent_to_fresh(layout, tmp_path)
