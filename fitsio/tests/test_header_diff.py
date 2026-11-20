import os
import tempfile

import numpy as np
import pytest

from ..fitslib import FITS
from ..header import FITSHDR
from ..header_diff import (
    HeaderChange,
    HeaderDiff,
    HeaderDiffError,
    apply_header_diff,
    diff_headers,
    normalize_header_records,
    write_header_diff,
)


# Long enough to need at least one CONTINUE card in a real FITS file.
LONG_OLD = (
    'The revised calibration note is stored as one long string card and it '
    'must travel with its CONTINUE cards as a single logical change. '
    'OLD-VALUE'
)
LONG_NEW = LONG_OLD.replace('OLD-VALUE', 'NEW-VALUE')

HISTORY_TEXT = 'reduced with pipeline version one'
REVISED_HISTORY = 'revised after the audit'
NEW_COMMENT = 'group one revised by the audit'


def make_header(records):
    return FITSHDR([{'name': name, 'value': value} for name, value in records])


def old_records():
    return [
        ('OBSNUM1', 'observation one'),
        ('HISTORY', HISTORY_TEXT),
        ('LONGSTR', LONG_OLD),
        ('TAIL', 17),
        ('OBSNUM2', 'observation two'),
        ('HISTORY', HISTORY_TEXT),
        ('TRAILER', 'end of group two'),
    ]


def revised_records():
    return [
        ('OBSNUM1', 'observation one'),
        ('COMMENT', NEW_COMMENT),
        ('HISTORY', HISTORY_TEXT),
        ('LONGSTR', LONG_NEW),
        ('TAIL', 17),
        ('OBSNUM2', 'observation two'),
        ('HISTORY', REVISED_HISTORY),
        ('TRAILER', 'end of group two'),
    ]


def shifted_header():
    """
    The original header with an unrelated HISTORY calibration note added
    at the front.  The target cards keep their relative order.
    """
    records = [('HISTORY', 'calibration note added before the audit')]
    records.extend(old_records())
    return make_header(records)


def without_observation_markers():
    return make_header(
        [
            (name, value)
            for name, value in old_records()
            if not name.startswith('OBSNUM')
        ]
    )


def test_diff_records_occurrence_and_logical_value():
    """
    The diff must say which occurrence and which logical value changed.
    """
    diff = diff_headers(
        make_header(old_records()), make_header(revised_records())
    )

    assert len(diff) == 3

    by_key = {}
    for change in diff:
        by_key.setdefault(change.keyword, []).append(change)

    assert sorted(by_key) == ['COMMENT', 'HISTORY', 'LONGSTR']

    history = by_key['HISTORY'][0]
    assert history.action == 'modify'
    assert history.occurrence == 2
    assert history.old_value == HISTORY_TEXT
    assert history.new_value == REVISED_HISTORY

    long_change = by_key['LONGSTR'][0]
    assert long_change.action == 'modify'
    assert long_change.occurrence == 1
    assert long_change.old_value == LONG_OLD
    assert long_change.new_value == LONG_NEW

    comment = by_key['COMMENT'][0]
    assert comment.action == 'insert'
    assert comment.occurrence == 1
    assert comment.new_value == NEW_COMMENT

    # Each change answers the audit question in prose as well.
    assert 'occurrence 2' in history.describe()
    assert HISTORY_TEXT in history.describe()
    assert REVISED_HISTORY in history.describe()


def test_diff_ignores_unchanged_duplicate_history():
    """
    The first HISTORY and the equally-named cards around it are not
    reported just because a later occurrence changed.
    """
    diff = diff_headers(
        make_header(old_records()), make_header(revised_records())
    )
    history_changes = [
        change for change in diff if change.keyword == 'HISTORY'
    ]
    assert len(history_changes) == 1
    assert history_changes[0].occurrence == 2


def test_diff_does_not_split_continue_cards():
    """
    A long string and its CONTINUE cards are one logical change.
    """
    old_cards = [
        "OBSNUM1 = 'one'",
        "HISTORY text",
        "LONGSTR = '" + 'a' * 67 + "&'",
        "CONTINUE  '" + 'b' * 67 + "&'",
        "CONTINUE  '" + 'c' * 10 + "'",
        "TAIL    = 1",
    ]
    new_cards = list(old_cards)
    new_cards[2:5] = [
        "LONGSTR = '" + 'a' * 67 + "&'",
        "CONTINUE  '" + 'b' * 67 + "&'",
        "CONTINUE  '" + 'd' * 10 + "'",
    ]

    old_header = FITSHDR([card.ljust(80) for card in old_cards])
    new_header = FITSHDR([card.ljust(80) for card in new_cards])

    records = normalize_header_records(old_header)
    assert [record['name'] for record in records] == [
        'OBSNUM1',
        'HISTORY',
        'LONGSTR',
        'TAIL',
    ]
    assert records[2]['value'] == 'a' * 67 + 'b' * 67 + 'c' * 10

    diff = diff_headers(old_header, new_header)
    assert len(diff) == 1
    change = diff[0]
    assert change.keyword == 'LONGSTR'
    assert change.old_value == 'a' * 67 + 'b' * 67 + 'c' * 10
    assert change.new_value == 'a' * 67 + 'b' * 67 + 'd' * 10

    # No change is ever recorded against a CONTINUE keyword.
    assert all(item.keyword != 'CONTINUE' for item in diff)


def test_identical_duplicates_change_the_right_occurrence():
    """
    When every HISTORY card has the same value, only the position can tell
    them apart.  Applying the change must reproduce the revision exactly.
    """
    old = make_header(
        [
            ('OB1', 'one'),
            ('HISTORY', 'same'),
            ('OB2', 'two'),
            ('HISTORY', 'same'),
        ]
    )

    revisions = [
        [
            ('OB1', 'one'),
            ('HISTORY', 'changed'),
            ('OB2', 'two'),
            ('HISTORY', 'same'),
        ],
        [
            ('OB1', 'one'),
            ('HISTORY', 'same'),
            ('OB2', 'two'),
            ('HISTORY', 'changed'),
        ],
    ]

    for revision in revisions:
        new = make_header(revision)
        diff = diff_headers(old, new)
        applied = diff.apply(old)
        assert [record['value'] for record in applied.records()] == [
            value for _, value in revision
        ]


def test_apply_original_header():
    diff = diff_headers(
        make_header(old_records()), make_header(revised_records())
    )
    original = make_header(old_records())

    result = diff.apply_result(original)
    assert result.success
    assert result.failed_changes == []
    assert len(result.applied_changes) == 3

    records = result.header.records()
    names = [record['name'] for record in records]

    # The inserted COMMENT lands in the first group.
    comment_index = names.index('COMMENT')
    assert names[comment_index - 1] == 'OBSNUM1'

    # Both original HISTORY cards survive, in their original order.
    history_indices = [i for i, name in enumerate(names) if name == 'HISTORY']
    assert len(history_indices) == 2
    assert history_indices[0] < names.index('OBSNUM2')
    assert history_indices[1] > names.index('OBSNUM2')

    assert records[history_indices[0]]['value'] == HISTORY_TEXT
    assert records[history_indices[1]]['value'] == REVISED_HISTORY

    # The original header is unchanged by a non in-place application.
    assert [r['value'] for r in original.records()] == [
        value for _, value in old_records()
    ]


def test_apply_shifted_header():
    """
    An unrelated HISTORY prepended to the header must not be mistaken for
    the HISTORY that is supposed to change.
    """
    diff = diff_headers(
        make_header(old_records()), make_header(revised_records())
    )
    shifted = shifted_header()

    result = diff.apply_result(shifted)
    assert result.success

    records = result.header.records()
    names = [record['name'] for record in records]

    # The unrelated calibration HISTORY keeps its value and position.
    assert names[0] == 'HISTORY'
    assert records[0]['value'] == 'calibration note added before the audit'

    history_values = [
        record['value'] for record in records if record['name'] == 'HISTORY'
    ]
    assert history_values == [
        'calibration note added before the audit',
        HISTORY_TEXT,
        REVISED_HISTORY,
    ]

    # The new COMMENT is in the first group, after OBSNUM1.
    comment_index = names.index('COMMENT')
    assert names[comment_index - 1] == 'OBSNUM1'

    # No original card was reordered by the insertion.
    assert names.index('LONGSTR') < names.index('OBSNUM2')


def test_apply_ambiguous_header_reports_failures_without_modifying():
    """
    With the observation markers gone, the second HISTORY cannot be
    distinguished from the first.  The change set is rejected and the
    header is left alone.
    """
    diff = diff_headers(
        make_header(old_records()), make_header(revised_records())
    )
    ambiguous = without_observation_markers()
    before = [dict(record) for record in ambiguous.records()]

    result = diff.apply_result(ambiguous)
    assert not result.success
    assert result.applied_changes == []
    assert result.changed is False

    failed = result.failed_changes
    assert failed
    assert all(isinstance(change, HeaderChange) for change in failed)
    assert any(change.keyword == 'HISTORY' for change in failed)
    assert all(failure['reason'] for failure in result.failures)

    # Nothing was written to the header.
    assert [dict(record) for record in ambiguous.records()] == before

    with pytest.raises(HeaderDiffError) as excinfo:
        diff.apply(ambiguous, in_place=True)
    assert excinfo.value.failed_changes
    assert [dict(record) for record in ambiguous.records()] == before


def test_apply_in_place():
    diff = diff_headers(
        make_header(old_records()), make_header(revised_records())
    )
    header = make_header(old_records())

    result = diff.apply_result(header, in_place=True)
    assert result.success
    assert result.header is header
    assert header['OBSNUM1'] == 'observation one'
    assert header['TAIL'] == 17


def test_serialized_change_set_roundtrip():
    diff = diff_headers(
        make_header(old_records()), make_header(revised_records())
    )
    payload = diff.to_dict()
    restored = HeaderDiff.from_dict(payload)

    assert restored.to_dict() == payload
    assert diff.fingerprint() == restored.fingerprint()

    original = make_header(old_records())
    assert restored.can_apply(original)
    assert [r['value'] for r in restored.apply(original).records()] == [
        r['value'] for r in diff.apply(original).records()
    ]

    assert apply_header_diff(original, payload)['TAIL'] == 17


def _write_image_with_header(fname, header):
    data = np.arange(100, dtype='i4').reshape(10, 10)
    with FITS(fname, 'rw') as fits:
        fits.write(data, header=header)
    return data


def test_write_header_diff_preserves_image_data():
    diff = diff_headers(
        make_header(old_records()), make_header(revised_records())
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        fname = os.path.join(tmpdir, 'image.fits')
        data = _write_image_with_header(fname, make_header(old_records()))

        with FITS(fname) as fits:
            before = fits[0].get_offsets()
        with open(fname, 'rb') as fobj:
            original_bytes = fobj.read()

        write_header_diff(fname, diff, ext=0)

        with FITS(fname) as fits:
            after = fits[0].get_offsets()
            assert np.array_equal(fits[0].read(), data)

            header = fits[0].read_header()
            assert header['TAIL'] == 17
            assert header['LONGSTR'] == LONG_NEW
            assert header['OBSNUM2'] == 'observation two'

            names = [record['name'] for record in header.records()]
            assert names.count('HISTORY') == 2
            assert names.count('COMMENT') == 3  # 2 standard + the audit one

        with open(fname, 'rb') as fobj:
            revised_bytes = fobj.read()

        old_data = original_bytes[before['data_start'] : before['data_end']]
        new_data = revised_bytes[after['data_start'] : after['data_end']]
        assert old_data == new_data


def test_write_header_diff_preserves_table_data():
    diff = diff_headers(
        make_header(old_records()), make_header(revised_records())
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        fname = os.path.join(tmpdir, 'table.fits')
        columns = [np.array([1, 2, 3]), np.array([1.5, 2.5, 3.5])]
        with FITS(fname, 'rw') as fits:
            fits.write_table(
                columns,
                names=['a', 'b'],
                header=make_header(old_records()),
            )

        with FITS(fname) as fits:
            table = fits[1].read()
            before = fits[1].get_offsets()
        with open(fname, 'rb') as fobj:
            original_bytes = fobj.read()

        write_header_diff(fname, diff, ext=1)

        with FITS(fname) as fits:
            after = fits[1].get_offsets()
            revised = fits[1].read()
            header = fits[1].read_header()

            assert np.array_equal(table['a'], revised['a'])
            assert np.array_equal(table['b'], revised['b'])
            assert header['OBSNUM2'] == 'observation two'
            assert header['TAIL'] == 17
            assert header['LONGSTR'] == LONG_NEW

        with open(fname, 'rb') as fobj:
            revised_bytes = fobj.read()

        old_data = original_bytes[before['data_start'] : before['data_end']]
        new_data = revised_bytes[after['data_start'] : after['data_end']]
        assert old_data == new_data


def test_write_header_diff_ambiguous_leaves_file_untouched():
    diff = diff_headers(
        make_header(old_records()), make_header(revised_records())
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        fname = os.path.join(tmpdir, 'image.fits')
        _write_image_with_header(fname, without_observation_markers())

        with open(fname, 'rb') as fobj:
            original = fobj.read()

        with pytest.raises(HeaderDiffError):
            write_header_diff(fname, diff, ext=0)

        with open(fname, 'rb') as fobj:
            assert fobj.read() == original


def test_write_header_diff_resizes_header_blocks():
    """
    Growing or shrinking the header across a 2880-byte boundary must not
    move into or truncate the data section.
    """
    small = make_header([('A', 1)])
    large = make_header([('A', 1)] + [('B%d' % i, i) for i in range(80)])

    for source, diff in (
        (small, diff_headers(small, large)),
        (large, diff_headers(large, small)),
    ):
        with tempfile.TemporaryDirectory() as tmpdir:
            fname = os.path.join(tmpdir, 'table.fits')
            with FITS(fname, 'rw') as fits:
                fits.write_table(
                    [np.array([1, 2, 3])], names=['a'], header=source
                )

            with FITS(fname) as fits:
                before = fits[1].get_offsets()
                table = fits[1].read()

            with open(fname, 'rb') as fobj:
                original = fobj.read()

            write_header_diff(fname, diff, ext=1)

            with FITS(fname) as fits:
                after = fits[1].get_offsets()
                revised = fits[1].read()
                assert np.array_equal(table['a'], revised['a'])

            with open(fname, 'rb') as fobj:
                rewritten = fobj.read()

            assert (
                original[before['data_start'] : before['data_end']]
                == rewritten[after['data_start'] : after['data_end']]
            )


def test_ordinary_keyword_access_still_works():
    """
    The normal FITSHDR keyword lookups keep their meaning.
    """
    header = make_header(old_records())
    assert header['OBSNUM1'] == 'observation one'
    assert header['OBSNUM2'] == 'observation two'
    assert header['TAIL'] == 17
    assert header.get('missing', 'default') == 'default'
    assert 'OBSNUM1' in header
    assert 'missing' not in header


def test_records_after_write_have_continue_for_long_string():
    """
    The rewritten file stores the long string as a keyword card plus
    CONTINUE cards, and reading it back folds them into one value.
    """
    diff = diff_headers(
        make_header(old_records()), make_header(revised_records())
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        fname = os.path.join(tmpdir, 'image.fits')
        _write_image_with_header(fname, make_header(old_records()))
        write_header_diff(fname, diff, ext=0)

        cards = []
        with open(fname, 'rb') as fobj:
            while True:
                card = fobj.read(80).decode('ascii')
                if card.rstrip() == 'END':
                    break
                cards.append(card)

        long_cards = [card for card in cards if card.startswith('LONGSTR')]
        continue_cards = [
            card for card in cards if card.startswith('CONTINUE')
        ]
        assert len(long_cards) == 1
        assert len(continue_cards) >= 1
        assert long_cards[0].rstrip().endswith("&'")

        with FITS(fname) as fits:
            header = fits[0].read_header()
            assert header['LONGSTR'] == LONG_NEW
