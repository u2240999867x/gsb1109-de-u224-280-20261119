"""Tests for metadata provenance (audit evidence) tracking."""

import json

import pytest

from bids.layout import BIDSLayout
from bids.layout.models import MetadataProvenance
from bids.layout.provenance import AMBIGUOUS, EXPLICIT_NULL, OVERRIDE, SOLE

BOLD = 'sub-01/func/sub-01_task-rest_run-1_bold.nii.gz'


def _write_dataset(root, spec):
    """Materialize a dataset from a mapping of relative path -> payload.

    A payload of ``None`` creates an empty (data) file; a dict is serialized
    to JSON.
    """
    for rel, payload in spec.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if payload is None:
            path.touch()
        else:
            path.write_text(json.dumps(payload))


def _base_spec():
    """Three inheritance levels plus same-level and non-matching sidecars.

    * root level declares a numeric value, an option, and an unconflicted key
    * run level overrides only the numeric value
    * a nearby same-level sidecar explicitly nulls the option
    * two same-level sidecars that cannot specialize give opposing values for
      AmbiguousField
    * near-miss sidecars differ on session or task and must never be eligible
    """
    return {
        'dataset_description.json': {'Name': 'provenance', 'BIDSVersion': '1.8.0'},
        BOLD: None,
        'task-rest_bold.json': {
            'NumericField': 10,
            'ChoiceField': 'A',
            'UnconflictedField': 'keep',
        },
        'sub-01/func/sub-01_task-rest_run-1_bold.json': {'NumericField': 20},
        'sub-01/func/sub-01_task-rest_run-1_choice-null_bold.json': {'ChoiceField': None},
        'sub-01/func/sub-01_task-rest_run-1_omega-tag_bold.json': {'AmbiguousField': 'alphaValue'},
        'sub-01/func/sub-01_task-rest_run-1_alpha-tag_bold.json': {'AmbiguousField': 'omegaValue'},
        'sub-01/func/sub-01_ses-other_task-rest_run-1_bold.json': {'SessionOnlyField': 'nope'},
        'sub-01/func/sub-01_task-other_run-1_bold.json': {'TaskOnlyField': 'nope'},
    }


def _bold(layout):
    return layout.get(extension='.nii.gz')[0]


def _basenames(field_provenance):
    return [source.path.rsplit('/', 1)[-1] for source in field_provenance.sources]


def test_ordinary_metadata_is_unchanged(tmp_path):
    _write_dataset(tmp_path, _base_spec())
    layout = BIDSLayout(tmp_path, validate=False)

    metadata = layout.get_metadata(_bold(layout).path)
    # Null option is dropped; numeric override wins; unconflicted key survives.
    assert metadata['NumericField'] == 20
    assert metadata['UnconflictedField'] == 'keep'
    assert 'ChoiceField' not in metadata


def test_three_level_override_and_explicit_null(tmp_path):
    _write_dataset(tmp_path, _base_spec())
    layout = BIDSLayout(tmp_path, validate=False)
    report = layout.get_metadata_provenance(_bold(layout).path)

    numeric = report['NumericField']
    assert numeric.reason == OVERRIDE
    assert numeric.value == 20
    assert {source.value for source in numeric.sources} == {10, 20}
    # Closer (run-level) sidecar has the smaller discovery order.
    run_source = min(numeric.sources, key=lambda s: s.discovery_order)
    assert run_source.path.endswith('sub-01_task-rest_run-1_bold.json')
    assert run_source.value == 20

    choice = report['ChoiceField']
    assert choice.reason == EXPLICIT_NULL
    assert choice.value is None
    assert any(source.is_null for source in choice.sources)
    assert {source.value for source in choice.sources if not source.is_null} == {'A'}

    assert report['UnconflictedField'].reason == SOLE
    assert report['UnconflictedField'].value == 'keep'


def test_same_level_ambiguity_names_field_and_competing_candidates(tmp_path):
    _write_dataset(tmp_path, _base_spec())
    layout = BIDSLayout(tmp_path, validate=False)
    report = layout.get_metadata_provenance(_bold(layout).path)

    ambiguous = report['AmbiguousField']
    assert ambiguous.is_ambiguous
    assert ambiguous.reason == AMBIGUOUS
    assert ambiguous.value is None
    assert set(ambiguous.competing_values) == {'alphaValue', 'omegaValue'}
    # Both same-level competitors are listed with their paths.
    assert set(_basenames(ambiguous)) == {
        'sub-01_task-rest_run-1_alpha-tag_bold.json',
        'sub-01_task-rest_run-1_omega-tag_bold.json',
    }
    assert set(report.ambiguous_fields) == {'AmbiguousField'}


def test_ambiguity_is_not_decided_by_scan_order(tmp_path):
    _write_dataset(tmp_path, _base_spec())
    layout = BIDSLayout(tmp_path, validate=False)
    explanation = layout.explain_metadata_provenance(_bold(layout).path)

    assert 'AmbiguousField' in explanation
    assert 'alpha-tag_bold.json' in explanation
    assert 'omega-tag_bold.json' in explanation
    # The reviewer, not the scan order, must resolve the conflict.
    assert 'No winner was chosen by scan order' in explanation


def test_nonmatching_sidecars_are_not_evidence(tmp_path):
    _write_dataset(tmp_path, _base_spec())
    layout = BIDSLayout(tmp_path, validate=False)
    report = layout.get_metadata_provenance(_bold(layout).path)

    all_sources = {
        source.path.rsplit('/', 1)[-1]
        for field_provenance in report.fields.values()
        for source in field_provenance.sources
    }
    assert 'sub-01_ses-other_task-rest_run-1_bold.json' not in all_sources
    assert 'sub-01_task-other_run-1_bold.json' not in all_sources
    assert 'SessionOnlyField' not in report.fields
    assert 'TaskOnlyField' not in report.fields


def test_snapshot_reconciles_with_ordinary_values(tmp_path):
    _write_dataset(tmp_path, _base_spec())
    layout = BIDSLayout(tmp_path, validate=False)
    metadata = layout.get_metadata(_bold(layout).path)
    report = layout.get_metadata_provenance(_bold(layout).path)

    reconciliation = report.reconcile(metadata)
    # Ambiguous keys are excluded; every resolvable key must agree.
    assert 'AmbiguousField' not in reconciliation
    assert reconciliation == {
        'NumericField': True,
        'ChoiceField': True,
        'UnconflictedField': True,
    }


def test_provenance_rows_record_null_and_discovery_order(tmp_path):
    _write_dataset(tmp_path, _base_spec())
    layout = BIDSLayout(tmp_path, validate=False)
    bold_path = _bold(layout).path

    null_rows = (
        layout.session.query(MetadataProvenance)
        .filter_by(file_path=bold_path, key='ChoiceField', is_null=True)
        .all()
    )
    assert len(null_rows) == 1
    assert null_rows[0].value is None

    orders = [
        row.discovery_order
        for row in layout.session.query(MetadataProvenance)
        .filter_by(file_path=bold_path, key='AmbiguousField')
        .all()
    ]
    # Discovery order is the sidecar's position across all candidates, so the
    # two same-level competitors get distinct, ordered positions.
    assert len(orders) == 2
    assert len(set(orders)) == 2
    assert min(orders) < max(orders)


def test_provenance_frozen_after_disk_changes_and_reopen(tmp_path):
    dataset = tmp_path / 'dataset'
    db_path = tmp_path / 'index'
    _write_dataset(dataset, _base_spec())

    layout = BIDSLayout(dataset, validate=False)
    bold_path = _bold(layout).path
    first_report = layout.get_metadata_provenance(bold_path)
    assert first_report['NumericField'].competing_values == [20, 10]
    layout.save(db_path)

    # Mutate the root value and withdraw one conflicting sidecar after the
    # index was saved.
    root_sidecar = dataset / 'task-rest_bold.json'
    contents = json.loads(root_sidecar.read_text())
    contents['NumericField'] = 999
    root_sidecar.write_text(json.dumps(contents))
    (dataset / 'sub-01/func/sub-01_task-rest_run-1_omega-tag_bold.json').unlink()

    # Reopen the original database without reindexing: evidence is frozen.
    reopened = BIDSLayout(dataset, validate=False, database_path=db_path)
    old_metadata = reopened.get_metadata(bold_path)
    old_report = reopened.get_metadata_provenance(bold_path)

    assert old_metadata['NumericField'] == 20
    assert old_report['NumericField'].competing_values == [20, 10]
    assert old_report['AmbiguousField'].is_ambiguous
    # The withdrawn sidecar is still documented in the frozen snapshot.
    frozen_sources = _basenames(old_report['AmbiguousField'])
    assert 'sub-01_task-rest_run-1_omega-tag_bold.json' in frozen_sources
    assert old_report.reconcile(old_metadata)['NumericField'] is True

    # A brand-new index reflects current disk state and cannot backfill the
    # old release with evidence that no longer exists.
    fresh_db = tmp_path / 'fresh_index'
    reindexed = BIDSLayout(dataset, validate=False, database_path=fresh_db)
    new_report = reindexed.get_metadata_provenance(bold_path)
    assert new_report['NumericField'].competing_values == [20, 999]
    new_sources = {
        source.path.rsplit('/', 1)[-1]
        for field_provenance in new_report.fields.values()
        for source in field_provenance.sources
    }
    assert 'sub-01_task-rest_run-1_omega-tag_bold.json' not in new_sources
    # Only one competitor remains, so the field is no longer ambiguous.
    assert not new_report['AmbiguousField'].is_ambiguous


@pytest.mark.parametrize(
    ('first_value', 'second_value'),
    [('alphaValue', 'omegaValue'), ('omegaValue', 'alphaValue')],
)
def test_opposite_discovery_order_still_ambiguous(tmp_path, first_value, second_value):
    spec = _base_spec()
    # Remove the base conflict pair and re-add the same candidates with the
    # values swapped. The alphabetically-first file is always discovered
    # first, so the two parametrizations have opposite discovery order.
    del spec['sub-01/func/sub-01_task-rest_run-1_alpha-tag_bold.json']
    del spec['sub-01/func/sub-01_task-rest_run-1_omega-tag_bold.json']
    spec['sub-01/func/sub-01_task-rest_run-1_alpha-tag_bold.json'] = {
        'AmbiguousField': first_value
    }
    spec['sub-01/func/sub-01_task-rest_run-1_omega-tag_bold.json'] = {
        'AmbiguousField': second_value
    }
    _write_dataset(tmp_path, spec)
    layout = BIDSLayout(tmp_path, validate=False)
    report = layout.get_metadata_provenance(_bold(layout).path)

    ambiguous = report['AmbiguousField']
    assert ambiguous.is_ambiguous
    ordered = sorted(ambiguous.sources, key=lambda s: s.discovery_order)
    assert ordered[0].path.endswith('alpha-tag_bold.json')
    assert ordered[0].value == first_value
    assert ordered[1].value == second_value
    assert set(ambiguous.competing_values) == {'alphaValue', 'omegaValue'}


def test_provenance_available_through_bidsfile(tmp_path):
    _write_dataset(tmp_path, _base_spec())
    layout = BIDSLayout(tmp_path, validate=False)
    bold_file = _bold(layout)

    via_file = bold_file.get_metadata_provenance()
    via_layout = layout.get_metadata_provenance(bold_file.path)
    assert set(via_file.fields) == set(via_layout.fields)


def test_no_provenance_when_metadata_indexing_disabled(tmp_path):
    _write_dataset(tmp_path, _base_spec())
    from bids.layout import BIDSLayoutIndexer

    layout = BIDSLayout(tmp_path, validate=False, indexer=BIDSLayoutIndexer(index_metadata=False))
    report = layout.get_metadata_provenance(_bold(layout).path)
    assert report.fields == {}
    assert layout.get_metadata(_bold(layout).path) == {}


def test_indexing_does_not_modify_source_sidecars(tmp_path):
    spec = _base_spec()
    _write_dataset(tmp_path, spec)
    BIDSLayout(tmp_path, validate=False)

    root_sidecar = tmp_path / 'task-rest_bold.json'
    assert json.loads(root_sidecar.read_text()) == spec['task-rest_bold.json']
    null_sidecar = tmp_path / 'sub-01/func/sub-01_task-rest_run-1_choice-null_bold.json'
    assert json.loads(null_sidecar.read_text()) == {'ChoiceField': None}


def test_explain_handles_file_without_evidence(tmp_path):
    (tmp_path / 'dataset_description.json').write_text(
        json.dumps({'Name': 'empty', 'BIDSVersion': '1.8.0'})
    )
    data_file = tmp_path / 'sub-01/func/sub-01_task-x_bold.nii.gz'
    data_file.parent.mkdir(parents=True)
    data_file.touch()
    layout = BIDSLayout(tmp_path, validate=False)

    explanation = layout.explain_metadata_provenance(str(data_file))
    assert 'No indexed sidecar evidence' in explanation
    assert layout.get_metadata_provenance(str(data_file)).fields == {}


def test_legacy_database_without_provenance_table_degrades(tmp_path):
    import sqlalchemy as sa

    dataset = tmp_path / 'dataset'
    db_path = tmp_path / 'index'
    _write_dataset(
        dataset,
        {
            'dataset_description.json': {'Name': 'legacy', 'BIDSVersion': '1.8.0'},
            BOLD: None,
            'task-rest_bold.json': {'NumericField': 10},
        },
    )
    BIDSLayout(dataset, validate=False).save(db_path)

    # Simulate a database produced before provenance existed.
    engine = sa.create_engine(f'sqlite:///{db_path}/layout_index.sqlite')
    with engine.begin() as conn:
        conn.execute(sa.text('DROP TABLE metadata_provenance'))
    engine.dispose()

    legacy = BIDSLayout(dataset, validate=False, database_path=db_path)
    bold_path = _bold(legacy).path
    # Ordinary metadata still works; provenance is simply unavailable.
    assert legacy.get_metadata(bold_path) == {'NumericField': 10}
    assert legacy.get_metadata_provenance(bold_path).fields == {}


@pytest.mark.parametrize('reason', [SOLE, OVERRIDE, EXPLICIT_NULL, AMBIGUOUS])
def test_reason_constants_are_strings(reason):
    assert isinstance(reason, str)
