"""
Tests for strict, evidence-based FITS extension selection.

The test file deliberately contains the awkward combination an archive
importer has to handle:

- two extensions named SCI, both declaring the modern EXTVER=2
- one extension literally named SCI-2 with no version keyword at all
- one lowercase sci extension carrying only the legacy HDUVER keyword

Each extension holds distinct data so the physically selected HDU can be
proven rather than assumed.
"""

import numpy as np
import pytest

from ..fitslib import FITS
from ..hdu.selection import (
    ExtensionAmbiguousError,
    ExtensionCandidate,
    EXTNAME_KEYWORD,
    EXTVER_KEYWORD,
    HDUNAME_KEYWORD,
    HDUVER_KEYWORD,
)


def _image(fill):
    return np.full((2, 2), fill, dtype='i4')


def _build_file(fname):
    with FITS(fname, 'rw') as fits:
        fits.write_image(_image(10), extname='SCI', extver=2)
        fits.write_image(_image(20), extname='SCI', extver=2)
        fits.write_image(_image(30), extname='SCI-2')
        fits.write_image(_image(40), extname='sci')
        # legacy version field only; no modern EXTVER keyword
        fits[-1].write_key(HDUVER_KEYWORD, 2)
        assert EXTVER_KEYWORD not in fits[-1].read_header()


@pytest.fixture
def fits_path(tmp_path):
    fname = str(tmp_path / 'strict_selection.fits')
    _build_file(fname)
    return fname


def _candidate_map(selection):
    return {c.extnum: c for c in selection.candidates}


def test_candidate_field_sources(fits_path):
    with FITS(fits_path) as fits:
        selection = fits.select_extension('SCI', require_unique=False)
        candidates = _candidate_map(selection)

    assert set(candidates) == {0, 1, 3}

    assert candidates[0].extname == 'SCI'
    assert candidates[0].name_keyword == EXTNAME_KEYWORD
    assert candidates[0].extver == 2
    assert candidates[0].hduver == 0
    assert candidates[0].version_keyword == EXTVER_KEYWORD

    assert candidates[1].extver == 2
    assert candidates[1].version_keyword == EXTVER_KEYWORD

    # the lowercase extension only carries the legacy field
    assert candidates[3].extname == 'sci'
    assert candidates[3].extver == 0
    assert candidates[3].hduver == 2
    assert candidates[3].version_keyword is None


def test_literal_sci2_name_not_mixed_with_sci_version_two(fits_path):
    # name SCI-2 must match only the extension actually named SCI-2
    with FITS(fits_path) as fits:
        selection = fits.select_extension('SCI-2')

    assert selection.is_unique
    candidate = selection.candidates[0]
    assert candidate.extnum == 2
    assert candidate.extname == 'SCI-2'
    assert candidate.version_keyword is None

    # explicit version 2 cannot match the undeclared SCI-2 extension,
    # and must not be confused with SCI + EXTVER=2
    with FITS(fits_path) as fits:
        with pytest.raises(OSError) as err:
            fits.select_extension('SCI-2', 2)

    message = str(err.value)
    assert 'SCI-2' in message
    assert 'without a matching EXTVER' in message


def test_name_only_selection_case_insensitive(fits_path):
    with FITS(fits_path) as fits:
        selection = fits.select_extension('sci', require_unique=False)

    # SCI, SCI and the legacy sci all match case-insensitively
    assert [c.extnum for c in selection.candidates] == [0, 1, 3]
    assert selection.version_requested is False


def test_name_only_selection_case_sensitive(fits_path):
    with FITS(fits_path, case_sensitive=True) as fits:
        upper = fits.select_extension('SCI', require_unique=False)
        lower = fits.select_extension('sci', require_unique=False)

    assert [c.extnum for c in upper.candidates] == [0, 1]
    assert [c.extnum for c in lower.candidates] == [3]
    assert lower.candidates[0].hduver == 2
    assert lower.candidates[0].extver == 0


def test_explicit_version_requires_modern_extver(fits_path):
    # case insensitive: the two modern SCI/EXTVER=2 match, the legacy
    # HDUVER-only extension does not
    with FITS(fits_path) as fits:
        with pytest.raises(ExtensionAmbiguousError) as err:
            fits.select_extension('SCI', 2)

    assert [c.extnum for c in err.value.candidates] == [0, 1]
    for candidate in err.value.candidates:
        assert candidate.version_keyword == EXTVER_KEYWORD
        assert candidate.extver == 2

    # case sensitive: the lowercase legacy extension alone cannot satisfy
    # an explicit version request, which is distinct from omitting one
    with FITS(fits_path, case_sensitive=True) as fits:
        with pytest.raises(OSError) as err:
            fits.select_extension('sci', 2)

    message = str(err.value)
    assert 'HDUVER=2 (legacy, no EXTVER)' in message


def test_ambiguous_selection_lists_every_candidate(fits_path):
    with FITS(fits_path) as fits:
        with pytest.raises(ExtensionAmbiguousError) as err:
            fits.select_extension('SCI', 2)

        candidates = err.value.candidates
        assert len(candidates) == 2
        message = str(err.value)
        assert 'refusing to default to first or last' in message
        assert "EXTNAME='SCI'" in message
        assert 'EXTVER=2' in message
        # the actual data behind each candidate differs and stays reachable
        data = {c.extnum: fits[c.extnum].read().ravel()[0] for c in candidates}
        assert data == {0: 10, 1: 20}


def test_physical_number_selection_keeps_evidence(fits_path):
    with FITS(fits_path) as fits:
        selection = fits.select_extension(3)

        assert selection.is_unique
        candidate = selection.require_unique()
        assert candidate.extnum == 3
        assert candidate.extname == 'sci'
        assert candidate.hduver == 2
        assert candidate.extver == 0

        # the physical selection still proves the actual content
        assert selection.hdu.read().ravel()[0] == 40

        with pytest.raises(OSError):
            fits.select_extension(99)


def test_append_reflects_fresh_ambiguity(fits_path):
    with FITS(fits_path, 'rw') as fits:
        before = fits.select_extension('SCI', 2, require_unique=False)
        assert [c.extnum for c in before.candidates] == [0, 1]

        fits.write_image(_image(50), extname='SCI', extver=2)

        after = fits.select_extension('SCI', 2, require_unique=False)
        assert [c.extnum for c in after.candidates] == [0, 1, 4]

        with pytest.raises(ExtensionAmbiguousError) as err:
            fits.select_extension('SCI', 2)
        assert [c.extnum for c in err.value.candidates] == [0, 1, 4]

        data = {
            c.extnum: fits[c.extnum].read().ravel()[0]
            for c in err.value.candidates
        }
        assert data == {0: 10, 1: 20, 4: 50}


def test_unique_result_is_not_cached_after_append(tmp_path):
    fname = str(tmp_path / 'unique_then_duplicate.fits')
    with FITS(fname, 'rw') as fits:
        fits.write_image(_image(1), extname='FLAT', extver=1)

        first = fits.select_extension('FLAT', 1)
        assert first.is_unique
        assert first.extnum == 0

        fits.write_image(_image(2), extname='FLAT', extver=1)

        with pytest.raises(ExtensionAmbiguousError) as err:
            fits.select_extension('FLAT', 1)
        assert [c.extnum for c in err.value.candidates] == [0, 1]


def test_no_version_vs_explicit_undeclared_are_distinct(fits_path):
    with FITS(fits_path, case_sensitive=True) as fits:
        # no version condition: the undeclared extension is eligible
        no_version = fits.select_extension('SCI-2')
        assert no_version.is_unique
        assert no_version.version_requested is False

        # explicit version: undeclared version cannot match
        with pytest.raises(OSError):
            fits.select_extension('SCI-2', 2)


def test_legacy_indexing_behavior_unchanged(fits_path):
    with FITS(fits_path) as fits:
        # ordinary name indexing still defaults to the first match
        assert fits['SCI'].get_extnum() == 0
        assert fits['sci'].get_extnum() == 0
        # ordinary physical indexing is untouched
        assert fits[3].get_extnum() == 3
        assert fits[2].get_extname() == 'SCI-2'


def test_extension_candidate_dict_roundtrip(fits_path):
    with FITS(fits_path) as fits:
        candidate = ExtensionCandidate(fits[3])

    as_dict = candidate.as_dict()
    assert as_dict == {
        'extnum': 3,
        'extname': 'sci',
        'name_keyword': EXTNAME_KEYWORD,
        'extver': 0,
        'hduver': 2,
        'version_keyword': None,
    }


def test_hduname_legacy_name_keyword(tmp_path):
    fname = str(tmp_path / 'legacy_hduname.fits')
    with FITS(fname, 'rw') as fits:
        fits.write_image(_image(60), extname='OLD')
        fits[-1].write_key(HDUNAME_KEYWORD, 'OLDNAME')
        fits[-1].delete_key(EXTNAME_KEYWORD)

    with FITS(fname) as fits:
        selection = fits.select_extension('OLDNAME')

    candidate = selection.require_unique()
    assert candidate.extnum == 0
    assert candidate.name_keyword == HDUNAME_KEYWORD
    assert candidate.extname == 'OLDNAME'


def test_invalid_explicit_version(fits_path):
    with FITS(fits_path) as fits:
        with pytest.raises(ValueError):
            fits.select_extension('SCI', 0)
        with pytest.raises(ValueError):
            fits.select_extension('SCI', -1)
