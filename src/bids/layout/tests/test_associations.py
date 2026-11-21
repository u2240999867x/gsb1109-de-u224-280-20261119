"""Offline regression tests for indexed BIDS associations.

These tests build a real (if tiny) BIDS dataset and let the standard
:class:`BIDSLayout` indexer create every association. No FileAssociation
records are inserted by hand and no image data is read.
"""

from __future__ import annotations

import json
from collections import Counter

import pytest

from bids.layout import BIDSLayout

IMAGE_RUN_1 = 'sub-01/func/sub-01_task-shared_run-1_bold.nii.gz'
IMAGE_RUN_2 = 'sub-01/func/sub-01_task-shared_run-2_bold.nii.gz'

DEDICATED_EVENTS = 'sub-01/func/sub-01_task-shared_run-1_events.tsv'
DEDICATED_EVENTS_JSON = 'sub-01/func/sub-01_task-shared_run-1_events.json'

SHARED_EVENTS = 'task-shared_events.tsv'
SHARED_EVENTS_JSON = 'task-shared_events.json'

LOOKALIKE_EVENTS = 'sub-01/func/sub-01_task-other_run-1_events.tsv'
LOOKALIKE_EVENTS_JSON = 'sub-01/func/sub-01_task-other_run-1_events.json'

UNRELATED_EVENTS_JSON = 'sub-01/func/sub-01_task-shared_run-2_events.json'

NEAREST_KWARGS = {
    'extension': '.tsv',
    'suffix': 'events',
    'all_': True,
    'full_search': True,
    'ignore_strict_entities': ['suffix', 'extension'],
}


def _write(root, relpath, content):
    path = root / relpath
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, dict):
        content = json.dumps(content)
    path.write_text(content, encoding='utf-8')


def _expected(root, *relpaths):
    return sorted(str(root / relpath) for relpath in relpaths)


def _paths(associations):
    return [association.path for association in associations]


def _counts(associations):
    return Counter(_paths(associations))


@pytest.fixture(scope='module')
def assoc_root(tmp_path_factory):
    root = tmp_path_factory.mktemp('association_dataset')
    _write(
        root,
        'dataset_description.json',
        {'Name': 'Association regression dataset', 'BIDSVersion': '1.10.1'},
    )
    _write(root, 'participants.tsv', 'participant_id\nsub-01\n')

    # Shared events apply to both runs through filename entities. Their JSON
    # also serves as the inherited parent for the run-specific events JSON.
    _write(root, SHARED_EVENTS, 'onset\tduration\tvalue\n0\t1\t1\n')
    _write(
        root,
        SHARED_EVENTS_JSON,
        {'Columns': ['value'], 'InheritedMarker': True},
    )

    for run in ('1', '2'):
        _write(root, f'sub-01/func/sub-01_task-shared_run-{run}_bold.nii.gz', '')
        _write(
            root,
            f'sub-01/func/sub-01_task-shared_run-{run}_bold.json',
            {'RepetitionTime': 2},
        )

    # Run 1 has a dedicated event file in addition to the shared event file.
    _write(root, DEDICATED_EVENTS, 'onset\tduration\tvalue\n0\t1\t1\n')
    _write(
        root,
        DEDICATED_EVENTS_JSON,
        {'Columns': ['value'], 'DedicatedMarker': True},
    )

    # Same directory and suffix as the run-1 events JSON, but the run entity
    # makes it inapplicable to run 1.
    _write(
        root,
        UNRELATED_EVENTS_JSON,
        {'Columns': ['value'], 'Run2OnlyMarker': True},
    )

    # Same subject and run as the run-1 images, but a different task.
    _write(root, LOOKALIKE_EVENTS, 'onset\tduration\tvalue\n0\t1\t1\n')
    _write(
        root,
        LOOKALIKE_EVENTS_JSON,
        {'Columns': ['value'], 'OtherTaskMarker': True},
    )

    return root


@pytest.fixture(scope='module')
def assoc_index_dir(assoc_root, tmp_path_factory):
    index_dir = tmp_path_factory.mktemp('association_index') / 'saved'
    layout = BIDSLayout(assoc_root, validate=False)
    layout.save(str(index_dir), replace_connection=False)
    layout.connection_manager.session.close()
    return index_dir


@pytest.fixture(params=['reindexed', 'reloaded'])
def layout(request, assoc_root, assoc_index_dir):
    if request.param == 'reindexed':
        layout = BIDSLayout(assoc_root, validate=False)
    else:
        layout = BIDSLayout.load(str(assoc_index_dir))
    yield layout
    layout.connection_manager.session.close()


def test_image_informedby_edges_are_direct_and_exact(layout, assoc_root):
    image_run_1 = layout.get_file(IMAGE_RUN_1)
    image_run_2 = layout.get_file(IMAGE_RUN_2)

    assert _expected(assoc_root, DEDICATED_EVENTS, SHARED_EVENTS) == sorted(
        _paths(image_run_1.get_associations('InformedBy'))
    )
    assert _expected(assoc_root, SHARED_EVENTS) == _paths(
        image_run_2.get_associations('InformedBy')
    )
    assert not image_run_1.get_associations('IntendedFor')
    assert not image_run_2.get_associations('IntendedFor')


def test_event_intendedfor_edges_point_back_to_images(layout, assoc_root):
    dedicated = layout.get_file(DEDICATED_EVENTS)
    shared = layout.get_file(SHARED_EVENTS)
    lookalike = layout.get_file(LOOKALIKE_EVENTS)

    assert _expected(assoc_root, IMAGE_RUN_1) == _paths(dedicated.get_associations('IntendedFor'))
    assert _expected(assoc_root, IMAGE_RUN_1, IMAGE_RUN_2) == sorted(
        _paths(shared.get_associations('IntendedFor'))
    )
    assert not lookalike.get_associations('IntendedFor')


def test_child_and_parent_edges_have_the_right_orientation(layout, assoc_root):
    dedicated_events = layout.get_file(DEDICATED_EVENTS)
    dedicated_json = layout.get_file(DEDICATED_EVENTS_JSON)
    shared_events = layout.get_file(SHARED_EVENTS)
    shared_json = layout.get_file(SHARED_EVENTS_JSON)

    assert _expected(assoc_root, SHARED_EVENTS) == _paths(
        dedicated_events.get_associations('Child')
    )
    assert _expected(assoc_root, SHARED_EVENTS_JSON) == _paths(
        dedicated_json.get_associations('Child')
    )

    assert _expected(assoc_root, DEDICATED_EVENTS) == _paths(
        shared_events.get_associations('Parent')
    )
    assert _expected(assoc_root, DEDICATED_EVENTS_JSON) == _paths(
        shared_json.get_associations('Parent')
    )

    assert not dedicated_events.get_associations('Parent')
    assert not dedicated_json.get_associations('Parent')
    assert not shared_events.get_associations('Child')
    assert not shared_json.get_associations('Child')


def test_direct_edge_is_not_rewritten_as_inherited_duplicate(layout, assoc_root):
    image = layout.get_file(IMAGE_RUN_1)

    direct = image.get_associations('InformedBy')
    inherited = image.get_associations('InformedBy', include_parents=True)

    # Both views expose the same set of paths, which is exactly why a set
    # comparison cannot detect a regression that loses the direct edge.
    assert (
        set(_paths(direct))
        == set(_paths(inherited))
        == set(_expected(assoc_root, DEDICATED_EVENTS, SHARED_EVENTS))
    )

    assert _counts(direct) == Counter(
        {
            str(assoc_root / DEDICATED_EVENTS): 1,
            str(assoc_root / SHARED_EVENTS): 1,
        }
    )
    assert _counts(inherited) == Counter(
        {
            str(assoc_root / DEDICATED_EVENTS): 1,
            str(assoc_root / SHARED_EVENTS): 2,
        }
    )

    # The second occurrence of the shared file comes from this Child edge.
    assert _expected(assoc_root, SHARED_EVENTS) == _paths(
        layout.get_file(DEDICATED_EVENTS).get_associations('Child')
    )
    assert str(assoc_root / SHARED_EVENTS) in _paths(direct)


def test_get_nearest_returns_only_events_with_direct_edges(layout, assoc_root):
    image_run_1 = layout.get_file(IMAGE_RUN_1)
    image_run_2 = layout.get_file(IMAGE_RUN_2)

    nearest_run_1 = layout.get_nearest(image_run_1.path, **NEAREST_KWARGS)
    nearest_run_2 = layout.get_nearest(image_run_2.path, **NEAREST_KWARGS)

    assert _expected(assoc_root, DEDICATED_EVENTS, SHARED_EVENTS) == sorted(nearest_run_1)
    assert _expected(assoc_root, SHARED_EVENTS) == sorted(nearest_run_2)

    for nearest_path in nearest_run_1:
        intended = _paths(layout.get_file(nearest_path).get_associations('IntendedFor'))
        assert image_run_1.path in intended

    assert image_run_2.path in _paths(
        layout.get_file(str(assoc_root / SHARED_EVENTS)).get_associations('IntendedFor')
    )


def test_name_similar_event_is_not_turned_into_an_edge(layout, assoc_root):
    image = layout.get_file(IMAGE_RUN_1)
    lookalike = layout.get_file(LOOKALIKE_EVENTS)

    # A loose nearest search can legitimately surface the other-task file, but
    # proximity or a similar name must not create an index association.
    loose = layout.get_nearest(image.path, strict=False, **NEAREST_KWARGS)
    assert str(assoc_root / LOOKALIKE_EVENTS) in loose
    assert not lookalike.get_associations('IntendedFor')
    assert str(assoc_root / LOOKALIKE_EVENTS) not in _paths(image.get_associations('InformedBy'))


def test_nearby_json_must_match_entities_to_establish_metadata_edge(layout, assoc_root):
    dedicated = layout.get_file(DEDICATED_EVENTS)
    unrelated_json = layout.get_file(UNRELATED_EVENTS_JSON)

    metadata = layout.get_metadata(dedicated.path)
    assert metadata['DedicatedMarker'] is True
    assert metadata['InheritedMarker'] is True
    assert 'Run2OnlyMarker' not in metadata

    assert not unrelated_json.get_associations()
    assert str(assoc_root / UNRELATED_EVENTS_JSON) not in _paths(
        dedicated.get_associations('Metadata')
    )
    assert str(assoc_root / UNRELATED_EVENTS_JSON) not in _paths(
        layout.get_file(DEDICATED_EVENTS_JSON).get_associations('Child')
    )


def test_saved_index_preserves_edge_provenance_in_both_directions(assoc_index_dir, assoc_root):
    reloaded = BIDSLayout.load(str(assoc_index_dir))

    try:
        image = reloaded.get_file(IMAGE_RUN_1)
        dedicated = reloaded.get_file(DEDICATED_EVENTS)
        shared = reloaded.get_file(SHARED_EVENTS)

        # Image -> events (direct and inherited).
        direct = image.get_associations('InformedBy')
        inherited = image.get_associations('InformedBy', include_parents=True)
        assert _counts(direct) == Counter(
            {
                str(assoc_root / DEDICATED_EVENTS): 1,
                str(assoc_root / SHARED_EVENTS): 1,
            }
        )
        assert _counts(inherited) == Counter(
            {
                str(assoc_root / DEDICATED_EVENTS): 1,
                str(assoc_root / SHARED_EVENTS): 2,
            }
        )

        # Events -> image (direct).
        assert _expected(assoc_root, IMAGE_RUN_1) == _paths(
            dedicated.get_associations('IntendedFor')
        )
        assert _expected(assoc_root, IMAGE_RUN_1, IMAGE_RUN_2) == sorted(
            _paths(shared.get_associations('IntendedFor'))
        )

        # Inheritance direction survives the round trip.
        assert _expected(assoc_root, SHARED_EVENTS) == _paths(dedicated.get_associations('Child'))
        assert _expected(assoc_root, DEDICATED_EVENTS) == _paths(shared.get_associations('Parent'))
    finally:
        reloaded.connection_manager.session.close()
