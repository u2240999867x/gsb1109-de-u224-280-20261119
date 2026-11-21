"""Characterization tests for subject IDs that collide after integer coercion.

The fixture ``tests/data/subject_id_collision`` contains three subjects whose
labels are ``007``, ``7`` and ``17``.  Every subject occurs in the raw root and
in two derivative roots, ``derivatives/strproc`` and
``derivatives/intproc``, that use the *same* subject capture pattern.  The only
difference is the configured dtype: ``strproc`` keeps ``subject`` as ``str``
while ``intproc`` stores it as ``int``.

These tests pin down the current (surprising) behaviour rather than proposing a
fix:

* one exact-match query on the int root folds ``007`` and ``7`` together at the
  SQL level, because the query uses ``CAST(tags._value AS INTEGER)``;
* the same query on the str roots keeps them apart;
* ``regex_search=True`` never coerces types, so an anchored regex still matches
  the original literal text even on the int root;
* ``return_type='id'`` drops *additional* duplicates on top of the SQL result,
  which makes the summary differ from the per-root file sets;
* the two roots can be attached in either order without changing file results,
  but the order of the deduplicated id summary is unstable;
* saving/reopening only preserves the int root when the parent records the
  derivative paths, not when it records ``derivatives=True``.
"""

import os
import sqlite3

import pytest
import sqlalchemy as sa
from sqlalchemy import event

from bids import BIDSLayout
from bids.layout.utils import PaddedInt
from bids.reports import BIDSReport

SUBJECTS = ('007', '7', '17')
FIXTURE = 'subject_id_collision'


@pytest.fixture
def collision_root(tests_dir):
    return tests_dir / 'data' / FIXTURE


def _config(root, kind):
    return str(root / 'code' / f'subject_{kind}.json')


def _attach(layout, root, order=('strproc', 'intproc')):
    """Attach derivatives directly, bypassing the automatic indexer.

    The automatic indexer always uses the child directory's own
    ``layout_config.json`` for every derivative, which makes per-root dtype
    configurations impossible (they collide on ``entities.name``).  Wiring the
    children up manually is what lets each root carry its own dtype.
    """
    layout.derivatives.clear()
    for name in order:
        layout.derivatives[name] = BIDSLayout(
            str(root / 'derivatives' / name),
            is_derivative=True,
            config=_config(root, 'int' if name == 'intproc' else 'str'),
            sources=layout,
        )
    return layout


def _build(root, order=('strproc', 'intproc')):
    layout = BIDSLayout(root, validate=False, config=_config(root, 'str'))
    return _attach(layout, root, order)


def _path(value):
    return value.path if hasattr(value, 'path') else str(value)


def _rel(files, root):
    return sorted(
        os.path.relpath(_path(f), str(root)) for f in files if _path(f).endswith('.nii.gz')
    )


def _subject_rows(layout):
    return sorted(
        (os.path.relpath(path, layout.root), value, dtype)
        for path, value, dtype in layout.session.execute(
            sa.text("SELECT file_path, _value, _dtype FROM tags WHERE entity_name = 'subject'")
        ).all()
    )


def _capture(layout, **kwargs):
    """Execute one query per root and record SQL text and DBAPI bindings."""
    captured = {}

    def make_listener(name):
        def listener(conn, cursor, statement, parameters, context, executemany):
            if 'FROM files' in statement:
                captured[name] = {
                    'sql': ' '.join(statement.replace('\n', ' ').split()),
                    'params': parameters,
                }

        return listener

    for each in layout._get_layouts_in_scope('all'):
        name = each.source_pipeline
        listener = make_listener(name)
        engine = each.connection_manager.engine
        event.listen(engine, 'before_cursor_execute', listener)
        try:
            each.get(scope='self', **kwargs)
        finally:
            event.remove(engine, 'before_cursor_execute', listener)

    return captured


# ---------------------------------------------------------------------------
# Independent parsing of each root
# ---------------------------------------------------------------------------
def test_each_root_parses_subject_independently(collision_root):
    parent = _build(collision_root)

    assert parent.get_entities(scope='self')['subject']._dtype == 'str'
    assert parent.derivatives['strproc'].get_entities(scope='self')['subject']._dtype == 'str'
    assert parent.derivatives['intproc'].get_entities(scope='self')['subject']._dtype == 'int'

    for name in ('strproc', 'intproc'):
        root = parent.derivatives[name]
        entities = root.get_entities(scope='self')
        assert entities['subject'].dtype is (PaddedInt if name == 'intproc' else str)
        assert isinstance(root.get_subjects(scope='self')[0], str if name == 'strproc' else int)


def test_tag_storage_and_restored_values_per_root(collision_root):
    parent = _build(collision_root)

    for name in ('strproc', 'intproc'):
        root = parent.derivatives[name]
        expected_dtype = 'int' if name == 'intproc' else 'str'
        rows = _subject_rows(root)

        assert {row[1] for row in rows} == set(SUBJECTS)
        assert {row[2] for row in rows} == {expected_dtype}

        # The stored text keeps the original padding even for the int root.
        if name == 'intproc':
            assert {row[1] for row in rows} == {'007', '7', '17'}

        for bf in root.get(extension='.nii.gz', scope='self'):
            value = bf.get_entities()['subject']
            expected_type = PaddedInt if name == 'intproc' else str
            assert type(value) is expected_type
            if name == 'intproc':
                # PaddedInt equals both its literal and its integer value.
                assert value == int(value)
                assert value == str(value).zfill(len(str(value)))


def test_padded_int_equals_literal_and_integer():
    padded = PaddedInt('007')

    assert padded == '007'
    assert padded == 7
    assert hash(padded) == hash(7)
    assert isinstance(padded, int)
    # ... yet it is not equal to the *other* literal, which is what lets the
    # two roots disagree once strings are involved.
    assert not (padded == '7')


# ---------------------------------------------------------------------------
# Exact vs anchored regex vs unanchored regex
# ---------------------------------------------------------------------------
def test_exact_query_is_integer_equal_only_on_int_root(collision_root):
    parent = _build(collision_root)

    str_files = _rel(
        parent.get(subject='007', scope='strproc', return_type='file'), collision_root
    )
    int_files = _rel(
        parent.get(subject='007', scope='intproc', return_type='file'), collision_root
    )

    assert str_files == ['derivatives/strproc/sub-007/anat/sub-007_T1w.nii.gz']
    assert int_files == [
        'derivatives/intproc/sub-007/anat/sub-007_T1w.nii.gz',
        'derivatives/intproc/sub-7/anat/sub-7_T1w.nii.gz',
    ]

    # Asking for the *other* spelling returns the same two files on the int
    # root, which is the coalescing behaviour under test.
    assert (
        _rel(parent.get(subject='7', scope='intproc', return_type='file'), collision_root)
        == int_files
    )
    # On the string root the two spellings stay distinct.
    assert _rel(parent.get(subject='7', scope='strproc', return_type='file'), collision_root) == [
        'derivatives/strproc/sub-7/anat/sub-7_T1w.nii.gz'
    ]


def test_anchored_regex_keeps_literal_identity_on_both_roots(collision_root):
    parent = _build(collision_root)

    for scope, pattern, label in (
        ('strproc', '^007$', '007'),
        ('intproc', '^007$', '007'),
        ('strproc', '^7$', '7'),
        ('intproc', '^7$', '7'),
    ):
        files = _rel(
            parent.get(subject=pattern, regex_search=True, scope=scope, return_type='file'),
            collision_root,
        )
        assert files == [f'derivatives/{scope}/sub-{label}/anat/sub-{label}_T1w.nii.gz']


def test_same_id_summary_from_different_processing_paths(collision_root):
    """The headline symptom.

    Exact ``007`` and anchored ``^007$`` produce the *same* final subject-id
    summary, yet the exact query also matches ``sub-7`` in the int root because
    it compares integers, while the anchored regex compares literal text.
    """
    parent = _build(collision_root)

    exact_ids = parent.get(subject='007', return_type='id', target='subject')
    anchored_ids = parent.get(
        subject='^007$', regex_search=True, return_type='id', target='subject'
    )
    exact_summary = sorted((type(i).__name__, repr(i)) for i in exact_ids)
    anchored_summary = sorted((type(i).__name__, repr(i)) for i in anchored_ids)
    assert (
        exact_summary
        == anchored_summary
        == [
            ('PaddedInt', '007'),
            ('str', "'007'"),
        ]
    )

    exact_files = _rel(
        parent.get(subject='007', extension=['.nii.gz'], return_type='file'),
        collision_root,
    )
    anchored_files = _rel(
        parent.get(
            subject='^007$',
            regex_search=True,
            extension=['.nii.gz'],
            return_type='file',
        ),
        collision_root,
    )
    assert exact_files == [
        'derivatives/intproc/sub-007/anat/sub-007_T1w.nii.gz',
        'derivatives/intproc/sub-7/anat/sub-7_T1w.nii.gz',
        'derivatives/strproc/sub-007/anat/sub-007_T1w.nii.gz',
        'sub-007/anat/sub-007_T1w.nii.gz',
    ]
    assert anchored_files == [
        'derivatives/intproc/sub-007/anat/sub-007_T1w.nii.gz',
        'derivatives/strproc/sub-007/anat/sub-007_T1w.nii.gz',
        'sub-007/anat/sub-007_T1w.nii.gz',
    ]
    assert set(exact_files) - set(anchored_files) == {
        'derivatives/intproc/sub-7/anat/sub-7_T1w.nii.gz'
    }


def test_unanchored_regex_is_the_counter_proof(collision_root):
    parent = _build(collision_root)

    for scope in ('strproc', 'intproc'):
        files = _rel(
            parent.get(subject='7', regex_search=True, scope=scope, return_type='file'),
            collision_root,
        )
        # The unanchored pattern matches every literal containing a 7 on both
        # roots, including the distractor subject 17.
        assert files == [
            f'derivatives/{scope}/sub-007/anat/sub-007_T1w.nii.gz',
            f'derivatives/{scope}/sub-17/anat/sub-17_T1w.nii.gz',
            f'derivatives/{scope}/sub-7/anat/sub-7_T1w.nii.gz',
        ], scope


def test_actual_sql_and_bindings(collision_root):
    parent = _build(collision_root)

    exact = _capture(parent, subject='007', return_type='file')
    regex = _capture(parent, subject='^007$', regex_search=True, return_type='file')

    assert 'CAST(tags_1._value AS INTEGER) IN (?)' in exact['intproc']['sql']
    assert 'CAST' not in exact['strproc']['sql']
    assert 'tags_1._value IN (?)' in exact['strproc']['sql']

    # Runtime bindings, as passed to the DBAPI.
    int_param = exact['intproc']['params'][1]
    assert isinstance(int_param, PaddedInt)
    assert int_param == 7
    # SQLite compares the integer parameter (not the literal) against the
    # CAST column, so '007' and '7' land in the same bucket.
    assert sqlite3.connect(':memory:').execute(
        "SELECT '007' = 7, CAST('007' AS INTEGER) = 7, '7' = CAST('007' AS INTEGER)"
    ).fetchone() == (0, 1, 1)

    # The regex branch is dtype-blind and binds the original string, even on
    # the root whose Entity dtype is int.
    for name in ('strproc', 'intproc'):
        assert 'REGEXP' in regex[name]['sql']
        assert 'CAST' not in regex[name]['sql']
        assert regex[name]['params'][1] == '^007$'
        assert isinstance(regex[name]['params'][1], str)


# ---------------------------------------------------------------------------
# Scope='all' summaries vs per-root file hits
# ---------------------------------------------------------------------------
def test_all_scope_files_come_from_both_roots_but_ids_collapse(collision_root):
    parent = _build(collision_root)

    files = _rel(parent.get(subject='007', return_type='file'), collision_root)
    assert files == [
        'derivatives/intproc/sub-007/anat/sub-007_T1w.nii.gz',
        'derivatives/intproc/sub-7/anat/sub-7_T1w.nii.gz',
        'derivatives/strproc/sub-007/anat/sub-007_T1w.nii.gz',
        'sub-007/anat/sub-007_T1w.nii.gz',
    ]

    ids = parent.get(subject='007', return_type='id', target='subject')
    assert sorted((type(i).__name__, repr(i)) for i in ids) == [
        ('PaddedInt', '007'),
        ('str', "'007'"),
    ]

    # The int root contributes one id for the two files it matched.
    int_ids = parent.get(subject='007', scope='intproc', return_type='id', target='subject')
    assert len(int_ids) == 1
    assert type(int_ids[0]) is PaddedInt


def test_dup_removal_happens_only_in_id_summary(collision_root):
    parent = _build(collision_root)

    # Exact '007' on all scopes: four imaging files, two ids.  Nothing was
    # dropped from the file result; only the summary collapsed.
    files = _rel(
        parent.get(subject='007', extension=['.nii', '.nii.gz'], return_type='file'),
        collision_root,
    )
    ids = parent.get(subject='007', return_type='id', target='subject')
    assert len(files) == 4
    assert len(ids) == 2

    # Unanchored regex '7' hits three imaging files per root plus raw, yet
    # reports five ids because PaddedInt('007') / PaddedInt('17') equal the
    # *integer* spelling but not the padded/literal strings.
    files = _rel(
        parent.get(
            subject='7',
            regex_search=True,
            extension=['.nii', '.nii.gz'],
            return_type='file',
        ),
        collision_root,
    )
    ids = parent.get(subject='7', regex_search=True, return_type='id', target='subject')
    assert len(files) == 9
    assert len(ids) == 5
    assert {'007', '17'} <= {repr(i) for i in ids}


@pytest.mark.parametrize('order', [('strproc', 'intproc'), ('intproc', 'strproc')])
def test_attachment_order_does_not_change_files(collision_root, order):
    parent = _build(collision_root, order=order)

    files = _rel(parent.get(subject='007', return_type='file'), collision_root)
    assert files == [
        'derivatives/intproc/sub-007/anat/sub-007_T1w.nii.gz',
        'derivatives/intproc/sub-7/anat/sub-7_T1w.nii.gz',
        'derivatives/strproc/sub-007/anat/sub-007_T1w.nii.gz',
        'sub-007/anat/sub-007_T1w.nii.gz',
    ]


# ---------------------------------------------------------------------------
# Save / reopen
# ---------------------------------------------------------------------------
def _save(parent, path):
    parent.save(str(path), replace_connection=False)
    return BIDSLayout(database_path=str(path), validate=False)


def test_reopened_derivative_keeps_int_dtype_when_paths_are_recorded(collision_root, tmp_path):
    root = collision_root
    paths = [str(root / 'derivatives' / name) for name in ('strproc', 'intproc')]
    parent = BIDSLayout(root, validate=False, config=_config(root, 'str'), derivatives=paths)
    parent = _attach(parent, root)
    assert parent.derivatives['intproc'].get_entities(scope='self')['subject']._dtype == 'int'

    reopened = _save(parent, tmp_path / 'db')
    assert reopened.connection_manager.layout_info.derivatives == paths
    assert sorted(reopened.derivatives) == ['intproc', 'strproc']

    assert reopened.derivatives['strproc'].get_entities(scope='self')['subject']._dtype == 'str'
    assert reopened.derivatives['intproc'].get_entities(scope='self')['subject']._dtype == 'int'

    # The child database that is actually opened is the typed one under
    # <db>/<pipeline>/, not a freshly indexed one under <db>/derivatives/.
    for name in ('strproc', 'intproc'):
        opened = reopened.derivatives[name].connection_manager.database_file
        assert str(opened) == str(tmp_path / 'db' / name / 'layout_index.sqlite')

    int_files = _rel(reopened.get(subject='007', scope='intproc', return_type='file'), root)
    assert int_files == [
        'derivatives/intproc/sub-007/anat/sub-007_T1w.nii.gz',
        'derivatives/intproc/sub-7/anat/sub-7_T1w.nii.gz',
    ]
    assert reopened.get(subject='007', scope='intproc', return_type='id', target='subject') == [
        PaddedInt('007')
    ]

    # The persisted Tag text is unchanged; only the restored Python type
    # differs by root.
    rows = _subject_rows(reopened.derivatives['intproc'])
    assert {row[1] for row in rows} == set(SUBJECTS)
    assert {row[2] for row in rows} == {'int'}


def test_derivatives_true_save_loses_int_dtype_on_reopen(collision_root, tmp_path):
    """``derivatives=True`` is *not* a reproducible configuration.

    The parent records ``derivatives=True``, so ``add_derivatives`` derives
    child database directories named ``derivatives/<pipeline>``.  Those are
    different directories from the ones ``save()`` wrote (``<pipeline>``), so
    reopening indexes each child fresh from its own config (or, in this
    fixture, the default ``bids`` config) and every subject becomes a string.
    """
    root = collision_root
    parent = BIDSLayout(root, derivatives=True, validate=False, config=_config(root, 'str'))
    parent = _attach(parent, root)
    assert parent.derivatives['intproc'].get_entities(scope='self')['subject']._dtype == 'int'

    db = tmp_path / 'db'
    parent.save(str(db), replace_connection=False)
    written = sorted(str(p.relative_to(db)) for p in db.rglob('layout_index.sqlite'))
    assert written == [
        'intproc/layout_index.sqlite',
        'layout_index.sqlite',
        'strproc/layout_index.sqlite',
    ]

    reopened = BIDSLayout(database_path=str(db), validate=False)
    assert sorted(reopened.derivatives) == ['derivatives/intproc', 'derivatives/strproc']
    # New databases were created at the derived paths, using the default
    # configs; the typed indexes are present on disk but unused.
    assert (
        reopened.derivatives['derivatives/intproc'].get_entities(scope='self')['subject']._dtype
        == 'str'
    )
    for child in reopened.derivatives.values():
        assert os.path.exists(
            os.path.join(
                str(db), 'derivatives', os.path.basename(child.root), 'layout_index.sqlite'
            )
        )


def test_reset_database_overwrites_typed_indexes(collision_root, tmp_path):
    root = collision_root
    paths = [str(root / 'derivatives' / name) for name in ('strproc', 'intproc')]
    parent = BIDSLayout(root, validate=False, config=_config(root, 'str'), derivatives=paths)
    parent = _attach(parent, root)
    db = tmp_path / 'db'
    parent.save(str(db), replace_connection=False)

    # Rebuilding with the default per-child configs silently replaces the
    # int index with a str one.
    reset = BIDSLayout(
        root,
        database_path=str(db),
        reset_database=True,
        validate=False,
        config=_config(root, 'str'),
        derivatives=paths,
    )
    assert reset.derivatives['intproc'].get_entities(scope='self')['subject']._dtype == 'str'
    rows = _subject_rows(reset.derivatives['intproc'])
    assert {row[2] for row in rows} == {'str'}


def test_layout_config_json_in_derivative_dir_cannot_override_dtype(collision_root, tmp_path):
    """A directory-level ``layout_config.json`` dtype is unusable today."""
    import json
    import shutil

    copy = tmp_path / 'ds'
    shutil.copytree(collision_root, copy)
    for name, dtype in (('strproc', 'str'), ('intproc', 'int')):
        (copy / 'derivatives' / name / 'layout_config.json').write_text(
            json.dumps(
                {
                    'name': name,
                    'entities': [
                        {
                            'name': 'subject',
                            'pattern': '[/\\\\]+sub-([a-zA-Z0-9+]+)',
                            'directory': '{subject}',
                            'dtype': dtype,
                        }
                    ],
                }
            )
        )

    with pytest.raises(Exception, match='UNIQUE constraint failed: entities.name'):
        BIDSLayout(copy, derivatives=True, validate=False)


# ---------------------------------------------------------------------------
# Downstream report symptom
# ---------------------------------------------------------------------------
def test_report_deduplicates_by_subject_id_not_by_provenance(collision_root):
    root = collision_root
    parent = _build(root)

    subjects = parent.get_subjects()
    assert len(subjects) == 5
    assert {repr(s) for s in subjects} == {"'007'", "'7'", "'17'", '007', '17'}

    # The int-root id '007' and the str-root id "'007'" are distinct in the
    # summary, but the int-root query for either spelling returns both files.
    hits = {
        repr(subject): _rel(parent.get(subject=subject, extension=['.nii', '.nii.gz']), root)
        for subject in subjects
    }
    assert hits['007'] == hits["'007'"]
    assert len(hits['007']) == 4

    counter = BIDSReport(parent).generate()
    # Five subject ids collapse into two distinct report texts; the "007" and
    # "7" string ids receive the same per-subject file set as the int id.
    assert sum(counter.values()) == 5
    assert len(counter) == 2

    # generate_from_files has no id key to deduplicate on and crashes when it
    # tries to sort mixed PaddedInt/str subject labels.
    files = parent.get(extension=['.nii', '.nii.gz'])
    with pytest.raises(TypeError, match='not supported between instances of'):
        BIDSReport(parent).generate_from_files(files)
