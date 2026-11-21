"""Tests for subtree-scoped index refresh after BIDS session renames."""

import hashlib
import json
import os
import shutil
from pathlib import Path

import pytest

from bids.layout import BIDSLayout, BIDSLayoutIndexer
from bids.layout.models import BIDSFile, FileAssociation, Tag


def _write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding='utf-8')


def _snapshot_files(root):
    """Map every file under root to a content hash."""
    snapshot = {}
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            full = Path(dirpath) / name
            snapshot[str(full)] = hashlib.md5(full.read_bytes()).hexdigest()
    return snapshot


class CountingIndexer(BIDSLayoutIndexer):
    """Indexer that records every directory handed to the recursive walker."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.scanned_paths = []

    def _index_dir(self, path, config, force=None):
        self.scanned_paths.append(str(path))
        return super()._index_dir(path, config, force=force)


@pytest.fixture
def dataset(tmp_path):
    """Build a dataset with a movable session, an external fieldmap and an
    unrelated subject."""
    root = tmp_path / 'bids'
    _write_json(
        root / 'dataset_description.json',
        {'Name': 'refresh dataset', 'BIDSVersion': '1.8.0'},
    )

    func_old = root / 'sub-01' / 'ses-old' / 'func'
    bold_old = func_old / 'sub-01_ses-old_task-rest_run-1_bold.nii.gz'
    bold_old.parent.mkdir(parents=True)
    bold_old.touch()
    (func_old / 'sub-01_ses-old_task-rest_run-1_events.tsv').write_text(
        'onset\tduration\n0\t1\n', encoding='utf-8'
    )
    _write_json(
        func_old / 'sub-01_ses-old_task-rest_run-1_events.json',
        {'EventsKey': 'events-sidecar'},
    )
    _write_json(
        func_old / 'sub-01_ses-old_task-rest_run-1_bold.json',
        {'LocalKey': 'local-value', 'RepetitionTime': 2.0},
    )

    # Higher-level JSON that participates in inheritance (stays put).
    _write_json(
        root / 'sub-01' / 'sub-01_task-rest_bold.json',
        {'InheritedKey': 'inherited-value', 'LocalKey': 'inherited-wins-lost'},
    )

    # Another session whose fieldmap declares against the (old) run.
    fmap_dir = root / 'sub-01' / 'ses-fmap' / 'fmap'
    fmap = fmap_dir / 'sub-01_ses-fmap_phasediff.nii.gz'
    fmap.parent.mkdir(parents=True)
    fmap.touch()
    _write_json(
        fmap_dir / 'sub-01_ses-fmap_phasediff.json',
        {
            'IntendedFor': [
                'ses-old/func/sub-01_ses-old_task-rest_run-1_bold.nii.gz'
            ]
        },
    )

    # Unrelated subject, used for reconciliation after the refresh.
    func_99 = root / 'sub-99' / 'func'
    bold_99 = func_99 / 'sub-99_task-rest_bold.nii.gz'
    bold_99.parent.mkdir(parents=True)
    bold_99.touch()
    _write_json(func_99 / 'sub-99_task-rest_bold.json', {'Sub99Key': 'keep-me'})

    return {
        'root': root,
        'old_dir': root / 'sub-01' / 'ses-old',
        'new_dir': root / 'sub-01' / 'ses-new',
        'bold_old': bold_old,
        'events_old': func_old / 'sub-01_ses-old_task-rest_run-1_events.tsv',
        'bold_json_old': func_old / 'sub-01_ses-old_task-rest_run-1_bold.json',
        'fmap': fmap,
        'bold_99': bold_99,
    }


def _move_session(ds):
    """Rename the session directory and every ses-old token inside it."""
    old_dir, new_dir = ds['old_dir'], ds['new_dir']
    new_dir.mkdir()
    for dirpath, _dirnames, filenames in os.walk(old_dir):
        rel = Path(dirpath).relative_to(old_dir)
        target_dir = new_dir / rel if str(rel) != '.' else new_dir
        target_dir.mkdir(parents=True, exist_ok=True)
        for name in filenames:
            (Path(dirpath) / name).rename(target_dir / name.replace('ses-old', 'ses-new'))
    shutil.rmtree(old_dir)

    ds['bold_new'] = (
        new_dir / 'func' / 'sub-01_ses-new_task-rest_run-1_bold.nii.gz'
    )
    ds['events_new'] = (
        new_dir / 'func' / 'sub-01_ses-new_task-rest_run-1_events.tsv'
    )
    ds['bold_json_new'] = (
        new_dir / 'func' / 'sub-01_ses-new_task-rest_run-1_bold.json'
    )


def _association_pairs(layout, kind):
    rows = layout.session.query(FileAssociation).filter_by(kind=kind).all()
    return {(row.src, row.dst) for row in rows}


def test_refresh_renamed_session_updates_index_and_derived_state(dataset):
    ds = dataset
    indexer = CountingIndexer(validate=False)
    layout = BIDSLayout(ds['root'], validate=False, indexer=indexer)

    n_files_initial = len(layout.get())
    metadata_before = layout.get_metadata(str(ds['bold_old']))
    assert metadata_before['InheritedKey'] == 'inherited-value'
    assert metadata_before['LocalKey'] == 'local-value'

    events_bf = layout.get_file(str(ds['events_old']))
    assert str(ds['bold_old']) in {
        a.path for a in events_bf.get_associations('IntendedFor')
    }

    disk_before = _snapshot_files(ds['root'])
    _move_session(ds)
    disk_after_move = _snapshot_files(ds['root'])

    indexer.scanned_paths = []
    result = layout.refresh(
        old_paths=[str(ds['old_dir'])], new_paths=[str(ds['new_dir'])]
    )

    # Only the moved subtree was walked; the unrelated subject is never touched.
    assert all('sub-99' not in path for path in indexer.scanned_paths)
    assert all('ses-fmap' not in path for path in indexer.scanned_paths)
    assert result['removed_files'] == 4
    assert result['added_files'] == 4

    # The refresh only updates the index: incoming files are not modified.
    assert _snapshot_files(ds['root']) == disk_after_move
    assert disk_before != disk_after_move

    # Old paths and the old session identity drop out of query results.
    assert layout.get_file(str(ds['bold_old'])) is None
    assert layout.get(session='old') == []
    assert layout.get(target='session', return_type='id') == ['fmap', 'new']

    # New paths are fully queryable, with identity interpreted per new directory.
    new_bold = layout.get_file(str(ds['bold_new']))
    assert new_bold is not None
    assert new_bold.entities['session'] == 'new'
    assert {f.path for f in layout.get(session='new')} == {
        str(ds['bold_new']),
        str(ds['events_new']),
        str(ds['bold_json_new']),
        str(ds['new_dir'] / 'func' / 'sub-01_ses-new_task-rest_run-1_events.json'),
    }

    # Metadata inheritance is re-evaluated under the new location: the
    # higher-level JSON still applies and the moved local sidecar wins.
    metadata_after = layout.get_metadata(str(ds['bold_new']))
    assert metadata_after['InheritedKey'] == 'inherited-value'
    assert metadata_after['LocalKey'] == 'local-value'
    assert metadata_after['RepetitionTime'] == 2.0

    # Derived run association follows the events file to its new location.
    events_new_bf = layout.get_file(str(ds['events_new']))
    assert str(ds['bold_new']) in {
        a.path for a in events_new_bf.get_associations('IntendedFor')
    }
    assert str(ds['events_new']) in {
        a.path for a in new_bold.get_associations('InformedBy')
    }

    # No duplicate or lost rows overall.
    assert len(layout.get()) == n_files_initial
    assert layout.session.query(BIDSFile).count() == n_files_initial

    # Unrelated subject survives untouched and reconciles to the same result.
    bold_99 = layout.get_file(str(ds['bold_99']))
    assert bold_99 is not None
    assert layout.get_metadata(str(ds['bold_99']))['Sub99Key'] == 'keep-me'


def test_refresh_state_persists_in_reopened_database(dataset, tmp_path):
    ds = dataset
    layout = BIDSLayout(ds['root'], validate=False)
    _move_session(ds)
    layout.refresh(old_paths=[str(ds['old_dir'])], new_paths=[str(ds['new_dir'])])

    db_path = tmp_path / 'index_db'
    layout.save(str(db_path))
    reloaded = BIDSLayout(ds['root'], validate=False, database_path=str(db_path))

    assert reloaded.get_file(str(ds['bold_new'])) is not None
    assert reloaded.get_file(str(ds['bold_old'])) is None
    assert reloaded.get(target='session', return_type='id') == ['fmap', 'new']

    metadata = reloaded.get_metadata(str(ds['bold_new']))
    assert metadata['InheritedKey'] == 'inherited-value'
    assert metadata['LocalKey'] == 'local-value'

    intended = _association_pairs(reloaded, 'IntendedFor')
    informed = _association_pairs(reloaded, 'InformedBy')
    assert (str(ds['fmap']), str(ds['bold_old'])) in intended
    assert (str(ds['bold_old']), str(ds['fmap'])) in informed


def test_dangling_outside_declaration_is_preserved_not_repointed(dataset):
    ds = dataset
    layout = BIDSLayout(ds['root'], validate=False)
    _move_session(ds)
    layout.refresh(old_paths=[str(ds['old_dir'])], new_paths=[str(ds['new_dir'])])

    intended = _association_pairs(layout, 'IntendedFor')
    informed = _association_pairs(layout, 'InformedBy')

    # The fieldmap (outside the moved subtree) keeps its declaration verbatim.
    assert (str(ds['fmap']), str(ds['bold_old'])) in intended
    assert (str(ds['bold_old']), str(ds['fmap'])) in informed

    # Nothing silently rewrites the declaration toward the new target.
    fmap_targets = [dst for src, dst in intended if src == str(ds['fmap'])]
    assert fmap_targets == [str(ds['bold_old'])]
    assert not any(dst == str(ds['bold_new']) for src, dst in intended if src == str(ds['fmap']))

    # The dangling edge does not resolve to a usable BIDSFile in either
    # direction, and the moved run does not inherit the stale fieldmap.
    fmap_bf = layout.get_file(str(ds['fmap']))
    assert str(ds['bold_old']) not in {a.path for a in fmap_bf.get_associations()}
    new_bold = layout.get_file(str(ds['bold_new']))
    assert str(ds['fmap']) not in {a.path for a in new_bold.get_associations()}

    # The events-to-run link remains the only healthy IntendedFor edge.
    assert (str(ds['events_new']), str(ds['bold_new'])) in intended


def test_refresh_is_idempotent_without_files_under_old_path(dataset):
    ds = dataset
    layout = BIDSLayout(ds['root'], validate=False)
    _move_session(ds)

    layout.refresh(old_paths=[str(ds['old_dir'])], new_paths=[str(ds['new_dir'])])
    n_files = layout.session.query(BIDSFile).count()
    n_tags = layout.session.query(Tag).count()
    intended_before = _association_pairs(layout, 'IntendedFor')

    # The old scope contains no files anymore; repeating must not duplicate rows.
    layout.refresh(old_paths=[str(ds['old_dir'])], new_paths=[str(ds['new_dir'])])
    layout.refresh(old_paths=[str(ds['new_dir'])], new_paths=[str(ds['new_dir'])])

    assert layout.session.query(BIDSFile).count() == n_files
    assert layout.session.query(Tag).count() == n_tags
    assert _association_pairs(layout, 'IntendedFor') == intended_before
    assert layout.get_file(str(ds['bold_new'])) is not None
    assert layout.get_file(str(ds['bold_old'])) is None


def test_failed_refresh_keeps_full_previous_state(dataset):
    ds = dataset
    layout = BIDSLayout(ds['root'], validate=False)

    n_files = layout.session.query(BIDSFile).count()
    n_tags = layout.session.query(Tag).count()
    intended_before = _association_pairs(layout, 'IntendedFor')
    metadata_before = layout.get_metadata(str(ds['bold_old']))

    _move_session(ds)
    # A target sidecar that cannot be decoded must abort the refresh.
    ds['bold_json_new'].write_text('{broken json', encoding='utf-8')

    with pytest.raises(OSError, match='decode JSON'):
        layout.refresh(
            old_paths=[str(ds['old_dir'])], new_paths=[str(ds['new_dir'])]
        )

    # The index still exposes the complete previous state.
    assert layout.session.query(BIDSFile).count() == n_files
    assert layout.session.query(Tag).count() == n_tags
    assert _association_pairs(layout, 'IntendedFor') == intended_before
    assert layout.get_file(str(ds['bold_old'])) is not None
    assert layout.get_file(str(ds['bold_new'])) is None
    assert layout.get(target='session', return_type='id') == ['fmap', 'old']
    assert layout.get_metadata(str(ds['bold_old'])) == metadata_before

    # Repair the sidecar and rerun the same refresh successfully.
    _write_json(
        ds['bold_json_new'],
        {'LocalKey': 'local-value', 'RepetitionTime': 2.0},
    )
    layout.refresh(old_paths=[str(ds['old_dir'])], new_paths=[str(ds['new_dir'])])

    assert layout.get_file(str(ds['bold_new'])) is not None
    assert layout.get_file(str(ds['bold_old'])) is None
    assert layout.session.query(BIDSFile).count() == n_files
    assert layout.get_metadata(str(ds['bold_new']))['LocalKey'] == 'local-value'


def test_refresh_accepts_relative_scopes(dataset):
    ds = dataset
    layout = BIDSLayout(ds['root'], validate=False)
    _move_session(ds)

    layout.refresh(
        old_paths=['sub-01/ses-old'],
        new_paths=['sub-01/ses-new'],
    )
    assert layout.get_file(str(ds['bold_new'])) is not None
    assert layout.get_file(str(ds['bold_old'])) is None


def test_refresh_supports_single_file_scope(dataset):
    ds = dataset
    layout = BIDSLayout(ds['root'], validate=False)
    initial_count = layout.session.query(BIDSFile).count()

    # Move just one file and refresh using file-level scopes.
    target = ds['root'] / 'sub-99' / 'func' / 'sub-99_task-rest_renamed_bold.nii.gz'
    ds['bold_99'].rename(target)
    layout.refresh(old_paths=[str(ds['bold_99'])], new_paths=[str(target)])

    assert layout.get_file(str(ds['bold_99'])) is None
    assert layout.get_file(str(target)) is not None
    assert layout.session.query(BIDSFile).count() == initial_count


def test_refresh_updates_file_based_sqlite_index_in_place(dataset, tmp_path):
    ds = dataset
    db_path = tmp_path / 'sqlite_db'
    layout = BIDSLayout(ds['root'], validate=False, database_path=str(db_path))
    assert (db_path / 'layout_index.sqlite').exists()

    _move_session(ds)
    layout.refresh(old_paths=[str(ds['old_dir'])], new_paths=[str(ds['new_dir'])])

    # A fresh connection to the same SQLite file sees the refreshed index.
    reopened = BIDSLayout(ds['root'], validate=False, database_path=str(db_path))
    assert reopened.get_file(str(ds['bold_new'])) is not None
    assert reopened.get_file(str(ds['bold_old'])) is None
    assert reopened.get_metadata(str(ds['bold_new']))['InheritedKey'] == 'inherited-value'
    assert (str(ds['fmap']), str(ds['bold_old'])) in _association_pairs(
        reopened, 'IntendedFor'
    )


def test_refresh_requires_indexer(dataset, tmp_path):
    ds = dataset
    layout = BIDSLayout(ds['root'], validate=False)
    db_path = tmp_path / 'persisted_db'
    layout.save(str(db_path))

    reloaded = BIDSLayout(ds['root'], validate=False, database_path=str(db_path))
    with pytest.raises(RuntimeError, match='indexer'):
        reloaded.refresh(old_paths=['sub-01/ses-old'], new_paths=['sub-01/ses-new'])
