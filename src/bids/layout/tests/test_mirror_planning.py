"""Offline tests for mirror resolution and content-pinned download planning.

These tests never touch the network. Local directories stand in for the two
remote archive roots (which expose the same relative paths), and separate
local directories stand in for the mirror mounts the offline node is allowed
to write. A single saved :class:`BIDSLayout` provides the indexed file
queries, source URIs and file-to-file relationships.
"""

import dataclasses
import hashlib

import pytest
from upath import UPath

from bids.exceptions import PlanValidationError, UnregisteredRootError
from bids.layout import (
    BIDSLayout,
    ContentManifest,
    ContentRecord,
    DownloadPlan,
    FileState,
    MirrorMap,
    MirrorMount,
    MirrorPlanner,
    RemoteArchiveRegistry,
)

ALPHA = 'sub-01/anat/sub-01_T1w.nii.gz'
ALPHA_JSON = 'sub-01/anat/sub-01_T1w.json'
BETA = 'sub-01/func/sub-01_task-rest_bold.nii.gz'
GAMMA = 'sub-01/func/sub-01_task-rest_events.tsv'

# The BIDS data files under test (excludes the root dataset_description.json).
DATA_RELS = (ALPHA, ALPHA_JSON, BETA, GAMMA)

ALPHA_V1 = b'alpha-content-v1'
ALPHA_V2 = b'alpha-content-v2'
BETA_V1 = b'beta-content-v1'
GAMMA_V1 = b'onset\tduration\n0\t1\n'


def digest(content):
    return 'sha256:' + hashlib.sha256(content).hexdigest()


def write_rel(root, rel, content):
    path = UPath(root) / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding='utf-8')


def build_archive(root, alpha_content):
    """Populate an archive root with the shared relative-path layout."""
    write_rel(root, 'dataset_description.json', '{"Name": "mirror", "BIDSVersion": "1.8.0"}')
    write_rel(root, ALPHA, alpha_content)
    write_rel(root, ALPHA_JSON, '{"Manufacturer": "Siemens"}')
    write_rel(root, BETA, BETA_V1)
    write_rel(root, GAMMA, GAMMA_V1)


@pytest.fixture(scope='module')
def env(tmp_path_factory):
    base = tmp_path_factory.mktemp('mirror')
    root_a = base / 'archiveA'
    root_b = base / 'archiveB'
    build_archive(root_a, ALPHA_V1)
    build_archive(root_b, ALPHA_V1)

    layout = BIDSLayout(str(root_a), validate=False)

    manifest_v1 = ContentManifest(
        [
            ContentRecord(ALPHA, digest=digest(ALPHA_V1), version='1'),
            ContentRecord(ALPHA_JSON, digest=digest(b'{"Manufacturer": "Siemens"}'), version='1'),
            ContentRecord(BETA, digest=digest(BETA_V1), version='1'),
            # Version-only evidence: no digest, so presence can never be confirmed.
            ContentRecord(GAMMA, digest=None, version='7'),
        ]
    )
    manifest_v2 = ContentManifest(
        [
            ContentRecord(ALPHA, digest=digest(ALPHA_V2), version='2'),
            ContentRecord(BETA, digest=digest(BETA_V1), version='1'),
            ContentRecord(GAMMA, digest=None, version='7'),
        ]
    )

    registry = RemoteArchiveRegistry([str(root_a), str(root_b)])

    return {
        'base': base,
        'root_a': str(root_a),
        'root_b': str(root_b),
        'layout': layout,
        'manifest_v1': manifest_v1,
        'manifest_v2': manifest_v2,
        'registry': registry,
    }


def _mount_with(base, name, files):
    """Create a mount directory containing only the given rel->content map."""
    mount = base / name
    for rel, content in files.items():
        write_rel(mount, rel, content)
    return str(mount)


def _layout_snapshot(layout):
    """Immutable evidence taken from the saved layout."""
    files = {(bf.relpath, bf.path) for bf in layout.get() if not bf.is_dir}
    associations = {
        bf.relpath: tuple(sorted(a.relpath for a in bf.get_associations()))
        for bf in layout.get()
        if not bf.is_dir
    }
    return files, associations


def _data_rels(reports):
    return {r.relpath for r in reports if r.relpath in DATA_RELS}


def test_first_round_plans_missing_alpha_and_confirms_beta(env):
    base = env['base']
    # Root A is mounted and writable, but alpha is absent; beta is confirmed.
    mount_a = _mount_with(base, 'mntA_round1', {BETA: BETA_V1, GAMMA: GAMMA_V1})
    mirror_map = MirrorMap([MirrorMount(root=env['root_a'], mount_point=mount_a)])
    planner = MirrorPlanner(env['layout'], env['registry'], env['manifest_v1'], mirror_map)

    plan, reports = planner.plan()
    by_state = planner.reports_by_state(reports)

    # Alpha is locally missing -> a fetch item is generated.
    alpha_item = plan.get(ALPHA)
    assert alpha_item is not None
    assert alpha_item.digest == digest(ALPHA_V1)
    assert alpha_item.source_uri == str(UPath(env['root_a']) / ALPHA)
    assert alpha_item.root == env['root_a']

    # Beta is already present with the confirmed digest -> no fetch item.
    assert plan.get(BETA) is None
    assert _data_rels(by_state[FileState.PRESENT]) == {BETA}
    assert _data_rels(by_state[FileState.MISSING]) == {ALPHA, ALPHA_JSON}

    # Gamma is present but only version evidence exists -> version unknown.
    assert _data_rels(by_state[FileState.VERSION_UNKNOWN]) == {GAMMA}


def test_second_archive_same_named_file_is_not_a_fallback(env):
    base = env['base']
    # Archive B is mounted with a same-named alpha present.
    mount_b = _mount_with(base, 'mntB', {ALPHA: ALPHA_V1, BETA: BETA_V1})
    # Archive A's mount still lacks alpha.
    mount_a = _mount_with(base, 'mntA_noalpha', {BETA: BETA_V1})
    mirror_map = MirrorMap(
        [
            MirrorMount(root=env['root_a'], mount_point=mount_a),
            MirrorMount(root=env['root_b'], mount_point=mount_b),
        ]
    )
    planner = MirrorPlanner(env['layout'], env['registry'], env['manifest_v1'], mirror_map)

    report = planner.evaluate(ALPHA)
    # Indexed alpha belongs to root A; root B's same-named copy must not satisfy it.
    assert report.state is FileState.MISSING
    assert report.root == env['root_a']
    # The empty, permitted location is on A's mount -- never B's same-named copy.
    assert report.local_path.startswith(mount_a)
    assert not report.local_path.startswith(mount_b)


def test_plan_round_trip_and_immutability(env, tmp_path):
    base = env['base']
    mount_a = _mount_with(base, 'mntA_save', {BETA: BETA_V1})
    planner = MirrorPlanner(
        env['layout'],
        env['registry'],
        env['manifest_v1'],
        MirrorMap([MirrorMount(root=env['root_a'], mount_point=mount_a)]),
    )
    plan, _ = planner.plan()
    plan_path = tmp_path / 'plan.json'
    plan.save(plan_path)

    reloaded = DownloadPlan.load(plan_path)
    assert reloaded.get(ALPHA).to_dict() == plan.get(ALPHA).to_dict()

    with pytest.raises(dataclasses.FrozenInstanceError):
        reloaded.get(ALPHA).digest = 'tampered'


def test_new_version_query_confirms_but_old_plan_is_not_satisfied(env):
    base = env['base']
    # Remount A at a new path holding ONLY the new version (no v1 anywhere).
    mount_v2 = _mount_with(base, 'mntA_v2', {ALPHA: ALPHA_V2, BETA: BETA_V1, GAMMA: GAMMA_V1})
    mirror_map = MirrorMap([MirrorMount(root=env['root_a'], mount_point=mount_v2)])

    # A fresh query against updated (v2) evidence can confirm the new version.
    planner_v2 = MirrorPlanner(env['layout'], env['registry'], env['manifest_v2'], mirror_map)
    new_report = planner_v2.evaluate(ALPHA)
    assert new_report.state is FileState.PRESENT
    assert new_report.observed_digest == digest(ALPHA_V2).split(':', 1)[1]
    # Source URI is unchanged even though content moved to a new version.
    assert new_report.source_uri == str(UPath(env['root_a']) / ALPHA)

    # The original v1 plan, re-checked against the v2-only mount, is superseded.
    old_planner = MirrorPlanner(env['layout'], env['registry'], env['manifest_v1'], mirror_map)
    old_plan, _ = old_planner.plan()
    # Recreate the v1-pinned plan deterministically (its alpha item pins v1).
    assert old_plan.get(ALPHA).digest == digest(ALPHA_V1)

    verify_reports = old_planner.verify(old_plan)
    alpha_verify = next(r for r in verify_reports if r.relpath == ALPHA)
    assert alpha_verify.state is FileState.SUPERSEDED
    assert alpha_verify.observed_digest == digest(ALPHA_V2).split(':', 1)[1]
    assert not alpha_verify.is_satisfied

    with pytest.raises(PlanValidationError):
        old_planner.assert_executable(old_plan)


def test_old_plan_relocates_when_original_is_still_accessible(env):
    base = env['base']
    # A different mount path, but it still holds the pinned v1 content.
    mount_v1 = _mount_with(base, 'mntA_relocated_v1', {ALPHA: ALPHA_V1, BETA: BETA_V1})
    mirror_map = MirrorMap([MirrorMount(root=env['root_a'], mount_point=mount_v1)])
    planner = MirrorPlanner(env['layout'], env['registry'], env['manifest_v1'], mirror_map)

    # Re-check the original v1-pinned plan against the relocated mount.
    original_plan = DownloadPlan([_make_item(env['root_a'], ALPHA, digest(ALPHA_V1), '1')])
    planner.assert_executable(original_plan)  # does not raise
    alpha_report = planner.verify(original_plan)[0]
    assert alpha_report.state is FileState.PRESENT
    assert alpha_report.local_path == str(UPath(mount_v1) / ALPHA)


def test_old_plan_inaccessible_when_source_cannot_be_read(env):
    mirror_map = MirrorMap(
        [MirrorMount(root=env['root_a'], mount_point='/unused/mount', readable=False)]
    )
    planner = MirrorPlanner(env['layout'], env['registry'], env['manifest_v1'], mirror_map)
    plan = DownloadPlan([_make_item(env['root_a'], ALPHA, digest(ALPHA_V1), '1')])
    report = planner.verify(plan)[0]
    assert report.state is FileState.INACCESSIBLE
    with pytest.raises(PlanValidationError):
        planner.assert_executable(plan)


def test_more_specific_mapping_restricts_scope(env):
    # A more specific mapping overwrites root A, permitting only func/ paths.
    mirror_map = MirrorMap(
        [
            MirrorMount(
                root=env['root_a'],
                mount_point='/unused/mount',
                write_scope=('sub-01/func',),
            )
        ]
    )
    planner = MirrorPlanner(env['layout'], env['registry'], env['manifest_v1'], mirror_map)
    plan = DownloadPlan([_make_item(env['root_a'], ALPHA, digest(ALPHA_V1), '1')])

    alpha_report = planner.verify(plan)[0]
    assert alpha_report.state is FileState.OUT_OF_SCOPE
    with pytest.raises(PlanValidationError):
        planner.assert_executable(plan)

    # A func/ relative path is inside the new, narrower scope.
    beta_report = planner.evaluate(BETA)
    assert beta_report.state is not FileState.OUT_OF_SCOPE


def test_prefix_similar_uri_is_unregistered(env):
    near_uri = env['root_a'] + '_extra/' + ALPHA
    assert env['registry'].root_for(near_uri) is None
    assert env['registry'].root_for(env['root_a'] + '/' + ALPHA) == env['root_a']

    with pytest.raises(UnregisteredRootError):
        env['registry'].require_registered(near_uri)

    planner = MirrorPlanner(
        env['layout'],
        env['registry'],
        env['manifest_v1'],
        MirrorMap([MirrorMount(root=env['root_a'], mount_point='/unused/mount', readable=False)]),
    )
    report = planner.evaluate_uri(near_uri, relpath=ALPHA)
    assert report.state is FileState.UNREGISTERED
    assert env['root_a'] in (report.detail or '')


def test_state_queries_are_separable(env):
    base = env['base']
    mount = _mount_with(base, 'mntA_states', {ALPHA: ALPHA_V2, BETA: BETA_V1, GAMMA: GAMMA_V1})
    planner = MirrorPlanner(
        env['layout'],
        env['registry'],
        env['manifest_v1'],
        MirrorMap([MirrorMount(root=env['root_a'], mount_point=mount)]),
    )
    # v1 manifest against a v2 file: alpha superseded, beta present, gamma unknown.
    assert _data_rels(planner.query_state(FileState.SUPERSEDED)) == {ALPHA}
    assert _data_rels(planner.query_state(FileState.PRESENT)) == {BETA}
    assert _data_rels(planner.query_state(FileState.VERSION_UNKNOWN)) == {GAMMA}
    assert _data_rels(planner.query_state(FileState.MISSING)) == {ALPHA_JSON}


def test_planning_retries_leave_layout_unchanged(env):
    base = env['base']
    mount = _mount_with(base, 'mntA_immutable', {BETA: BETA_V1})
    planner = MirrorPlanner(
        env['layout'],
        env['registry'],
        env['manifest_v1'],
        MirrorMap([MirrorMount(root=env['root_a'], mount_point=mount)]),
    )

    before_files, before_assocs = _layout_snapshot(env['layout'])
    for _ in range(3):
        plan, _ = planner.plan()
        planner.evaluate(ALPHA)
        planner.verify(plan)
    after_files, after_assocs = _layout_snapshot(env['layout'])

    assert after_files == before_files
    assert after_assocs == before_assocs
    # The alpha->sidecar relationship indexed in the first round is preserved.
    alpha_file = env['layout'].get_file(ALPHA)
    assert ALPHA_JSON in [a.relpath for a in alpha_file.get_associations()]


def test_planner_works_against_saved_and_reloaded_layout(env, tmp_path):
    base = env['base']
    mount = _mount_with(base, 'mntA_saved', {BETA: BETA_V1})
    mirror_map = MirrorMap([MirrorMount(root=env['root_a'], mount_point=mount)])

    # Persist the index, then build a brand-new layout from the saved database.
    db_path = tmp_path / 'layoutdb'
    env['layout'].save(str(db_path))
    reloaded = BIDSLayout.load(str(db_path))

    fresh_planner = MirrorPlanner(reloaded, env['registry'], env['manifest_v1'], mirror_map)
    plan, reports = fresh_planner.plan()

    # Source URIs and relative paths come straight from the reloaded index.
    alpha_report = next(r for r in reports if r.relpath == ALPHA)
    assert alpha_report.source_uri == str(UPath(env['root_a']) / ALPHA)
    assert plan.get(ALPHA).digest == digest(ALPHA_V1)
    assert plan.get(BETA) is None
    # File-to-file relationships survive the save/reload round trip.
    assert ALPHA_JSON in [a.relpath for a in reloaded.get_file(ALPHA).get_associations()]


def _make_item(root, relpath, digest_value, version):
    from bids.layout import PlanItem

    return PlanItem(
        source_uri=str(UPath(root) / relpath),
        root=root,
        relpath=relpath,
        digest=digest_value,
        version=version,
    )
