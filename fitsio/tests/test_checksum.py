"""Offline regressions for fitsio checksum evidence.

The fixture is built with the normal write path: several image HDUs get
CHECKSUM and DATASUM via ``write_checksum``, and one HDU is left without one
of those keywords.  The baseline header snapshot is captured after that
deliberate omission.  Damaged copies are made by editing bytes in place, so
the original CHECKSUM and DATASUM cards are never recomputed.

The evidence matrix documented by these tests is:

    HDU state          public call                 dataok/hduok
    healthy            returns                    1 / 1
    header card only   "hdu checksum failed"      1 / -1
    data only          "data checksum failed"     -1 / -1
    header and data    "data checksum failed"     -1 / -1
    DATASUM missing    "data checksum failed"      0 / -1
    CHECKSUM missing   "hdu checksum failed"       1 / 0

The public method raises on the first failure and therefore hides both
numeric statuses, including ``hduok`` whenever data also fails.  It also
reports a missing keyword with the same message as a real mismatch.  The raw
status plus the retained header snapshot are needed to identify the extension
and to separate missing evidence, real mismatches, and unchanged headers.

A deleted DATASUM card changes the header bytes, so a retained CHECKSUM
legitimately reports ``hduok == -1``.  That is why the tests model missing
keywords and header damage as separate facts instead of collapsing them.
"""

from dataclasses import dataclass

import numpy as np
import pytest

from ..fitslib import FITS

_EXT_NAMES = ('HEALTHY', 'HDRONLY', 'DATAONLY', 'BOTH', 'MISSING')
_CARD_LEN = 80
_BLOCK_LEN = 2880
_HEADER_KEY = 'SITE'
_ORIGINAL = b'ORIGINAL'
_TAMPERED = b'TAMPERED'


@dataclass(frozen=True)
class _BaselineHDU:
    name: str
    extnum: int
    header_start: int
    data_start: int
    header_bytes: bytes
    checksum: str | None
    datasum: str | None


def _build_baseline(path, missing_keyword='DATASUM'):
    """Write the shared healthy baseline and retain its header evidence."""
    with FITS(str(path), 'rw', clobber=True) as fits:
        for index, name in enumerate(_EXT_NAMES):
            data = np.arange(8, dtype='i4') + 100 * (index + 1)
            fits.write(
                data,
                extname=name,
                header={'ARCHIVE': 'ORIGINAL', _HEADER_KEY: 'ORIGINAL'},
            )

        fits.update_hdu_list()
        for i in range(len(fits)):
            fits[i].write_checksum()

        if missing_keyword is not None:
            fits['MISSING'].delete_key(missing_keyword)

    with FITS(str(path), 'r') as fits:
        raw = fits.read_raw()
        return raw, _capture_baseline(fits, raw)


def _capture_baseline(fits, raw):
    baseline = {}
    for i in range(len(fits)):
        hdu = fits[i]
        info = hdu.get_info()
        header_bytes = raw[info['header_start']:info['data_start']]
        header = hdu.read_header()
        name = hdu.get_extname()
        baseline[name] = _BaselineHDU(
            name=name,
            extnum=info['extnum'],
            header_start=info['header_start'],
            data_start=info['data_start'],
            header_bytes=header_bytes,
            checksum=header.get('CHECKSUM'),
            datasum=header.get('DATASUM'),
        )

    assert set(baseline) == set(_EXT_NAMES)
    return baseline


def _split_cards(header_bytes):
    assert len(header_bytes) % _CARD_LEN == 0
    return [
        header_bytes[offset:offset + _CARD_LEN]
        for offset in range(0, len(header_bytes), _CARD_LEN)
    ]


def _card_name(card):
    return card[:8].decode('ascii', errors='replace').strip()


def _changed_card_names(before, after):
    before_cards = _split_cards(before)
    after_cards = _split_cards(after)
    if len(before_cards) != len(after_cards):
        return {'<header-shape>'}
    return {
        _card_name(old)
        for old, new in zip(before_cards, after_cards)
        if old != new
    }


def _tamper_header(raw, evidence):
    """Change one non-checksum header card, preserving card length."""
    header = bytes(raw[evidence.header_start:evidence.data_start])
    card_offset = None
    for offset in range(0, len(header), _CARD_LEN):
        card = header[offset:offset + _CARD_LEN]
        if _card_name(card) == _HEADER_KEY:
            card_offset = evidence.header_start + offset
            break

    assert card_offset is not None, "test header card is missing"

    value_offset = raw.find(_ORIGINAL, card_offset, card_offset + _CARD_LEN)
    assert value_offset >= 0, "test header value is missing"
    assert len(_ORIGINAL) == len(_TAMPERED)
    raw[value_offset:value_offset + len(_TAMPERED)] = _TAMPERED


def _tamper_data(raw, evidence):
    """Change one image pixel, leaving the recorded checksums untouched."""
    offset = evidence.data_start
    value = int(np.frombuffer(raw[offset:offset + 4], dtype='>i4')[0])
    raw[offset:offset + 4] = np.array([value + 1], dtype='>i4').tobytes()


def _write_variant(baseline_raw, baseline, path, target, header=False,
                   data=False):
    raw = bytearray(baseline_raw)
    if header:
        _tamper_header(raw, baseline[target])
    if data:
        _tamper_data(raw, baseline[target])
    with open(path, 'wb') as stream:
        stream.write(raw)
    return path


def _checksum_cards(header_bytes):
    return [
        card
        for card in _split_cards(header_bytes)
        if _card_name(card) in ('CHECKSUM', 'DATASUM')
    ]


def _public_outcome(fits, name):
    """Capture the only result the public HDU API exposes."""
    try:
        fits[name].verify_checksum()
    except ValueError as exc:
        return str(exc)
    return 'ok'


def _raw_status(fits, name):
    """Capture the status dictionary before the public call discards it."""
    hdu = fits[name]
    return dict(fits._FITS.verify_checksum(hdu._ext + 1))


def _checksum_presence(fits, name):
    header = fits[name].read_header()
    return {key: key in header for key in ('CHECKSUM', 'DATASUM')}


def _recorded_checksums(fits, name):
    header = fits[name].read_header()
    return header.get('CHECKSUM'), header.get('DATASUM')


def _public_hides(public, status):
    """Name the distinctions the single public exception does not carry.

    The public method returns no status dict.  On failure it emits one fixed
    message, so the numeric ``0`` (keyword absent) versus ``-1`` (real
    mismatch) distinction, and the other layer when it was never reported,
    have to be recovered from the raw status or the retained header snapshot.
    """
    if public == 'data checksum failed':
        hidden = ['dataok', 'hduok']
        if status['dataok'] == 0:
            hidden.append('datasum_absent')
    elif public == 'hdu checksum failed':
        hidden = ['dataok', 'hduok']
        if status['hduok'] == 0:
            hidden.append('checksum_absent')
    else:
        hidden = []
    return tuple(hidden)


def _diagnose(status, present, changed_cards=None):
    """Separate raw evidence facts from the conclusion they support.

    ``facts`` keeps the two checksum layers independent.  In particular a
    missing DATASUM is not silently merged with an HDU mismatch: removing or
    omitting the card still changes the header bytes, so a retained CHECKSUM
    legitimately reports ``hduok == -1``.  ``conclusion`` says what the
    evidence on hand actually supports, including when the baseline is absent
    and a header change cannot be ruled out.
    """
    facts = set()

    if not present['DATASUM']:
        facts.add('datasum_absent')
    if not present['CHECKSUM']:
        facts.add('checksum_absent')
    if status['dataok'] == 0:
        facts.add('data_unverifiable')
    if status['hduok'] == 0:
        facts.add('hdu_unverifiable')
    if status['dataok'] == -1:
        facts.add('data_mismatch')
    if status['dataok'] == 1:
        facts.add('data_match')
    if status['hduok'] == -1:
        facts.add('hdu_mismatch')
    if status['hduok'] == 1:
        facts.add('hdu_match')

    if changed_cards is not None:
        if changed_cards:
            facts.add('header_changed')
        else:
            facts.add('header_unchanged')

    if 'data_mismatch' in facts and 'hdu_mismatch' in facts:
        if changed_cards is None:
            conclusion = 'data_mismatch_header_unresolved'
        elif changed_cards:
            conclusion = 'header_and_data_mismatch'
        else:
            conclusion = 'data_mismatch'
    elif 'hdu_mismatch' in facts and 'data_match' in facts:
        conclusion = 'header_mismatch'
    elif 'data_mismatch' in facts:
        conclusion = 'data_mismatch'
    elif 'data_unverifiable' in facts:
        conclusion = 'datasum_unavailable'
    elif 'hdu_unverifiable' in facts:
        conclusion = 'checksum_unavailable'
    elif 'data_match' in facts and 'hdu_match' in facts:
        conclusion = 'verified'
    else:
        conclusion = 'unexpected_status'

    return {
        'facts': tuple(sorted(facts)),
        'conclusion': conclusion,
    }


def _evidence_report(fits, baseline=None, order=_EXT_NAMES):
    raw = fits.read_raw()
    report = {}
    for name in order:
        hdu = fits[name]
        info = hdu.get_info()
        header_bytes = raw[info['header_start']:info['data_start']]

        changed_cards = None
        if baseline is not None:
            evidence = baseline[name]
            assert info['header_start'] == evidence.header_start
            assert info['data_start'] == evidence.data_start
            changed_cards = _changed_card_names(
                evidence.header_bytes, header_bytes
            )

        status = _raw_status(fits, name)
        present = _checksum_presence(fits, name)
        public = _public_outcome(fits, name)
        report[name] = {
            'name': name,
            'extnum': info['extnum'],
            'public': public,
            'public_hides': _public_hides(public, status),
            'status': status,
            'present': present,
            'changed_cards': changed_cards,
            'diagnosis': _diagnose(status, present, changed_cards),
        }
    return report


def _probe_in_order(fits, order):
    return {
        name: {
            'public': _public_outcome(fits, name),
            'status': _raw_status(fits, name),
        }
        for name in order
    }


def test_public_entry_point_collapses_two_status_layers(tmp_path):
    """Keep the literal public result and show exactly what it omits."""
    baseline_path = tmp_path / 'baseline.fits'
    baseline_raw, baseline = _build_baseline(baseline_path)
    both_path = _write_variant(
        baseline_raw, baseline, tmp_path / 'both.fits', 'BOTH',
        header=True, data=True,
    )

    with FITS(str(both_path), 'r') as fits:
        assert fits['HEALTHY'].verify_checksum() is None

        with pytest.raises(ValueError) as excinfo:
            fits['BOTH'].verify_checksum()
        assert str(excinfo.value) == 'data checksum failed'

        raw_result = fits._FITS.verify_checksum(fits['BOTH']._ext + 1)

    assert raw_result == {'dataok': -1, 'hduok': -1}
    # The public exception names only the data layer.  It cannot say whether
    # the header also changed; that needs the raw status plus the baseline.
    assert 'hdu' not in str(excinfo.value)
    assert baseline['BOTH'].checksum in repr(baseline_raw)


def test_baseline_has_checksums_and_retains_header_evidence(tmp_path):
    baseline_path = tmp_path / 'baseline.fits'
    baseline_raw, baseline = _build_baseline(baseline_path)

    assert len(baseline_raw) % _BLOCK_LEN == 0
    for name in _EXT_NAMES:
        evidence = baseline[name]
        assert len(evidence.header_bytes) % _BLOCK_LEN == 0
        assert evidence.checksum is not None

        if name == 'MISSING':
            assert evidence.datasum is None
        else:
            assert evidence.datasum is not None

    with FITS(str(baseline_path), 'r') as fits:
        for name in _EXT_NAMES:
            status = _raw_status(fits, name)
            if name == 'MISSING':
                assert status == {'dataok': 0, 'hduok': -1}
            else:
                assert status == {'dataok': 1, 'hduok': 1}
            assert _public_outcome(fits, name) == (
                'data checksum failed' if name == 'MISSING' else 'ok'
            )


@pytest.mark.parametrize(
    'variant,target,damage_header,damage_data,expected',
    [
        (
            'header_only',
            'HDRONLY',
            True,
            False,
            {
                'public': 'hdu checksum failed',
                'status': {'dataok': 1, 'hduok': -1},
                'changed_cards': {'SITE'},
                'public_conclusion': 'header_mismatch',
                'public_facts': ('data_match', 'hdu_mismatch'),
                'baseline_conclusion': 'header_mismatch',
                'baseline_facts': (
                    'data_match',
                    'hdu_mismatch',
                    'header_changed',
                ),
                'public_hides': ('dataok', 'hduok'),
            },
        ),
        (
            'data_only',
            'DATAONLY',
            False,
            True,
            {
                'public': 'data checksum failed',
                'status': {'dataok': -1, 'hduok': -1},
                'changed_cards': set(),
                'public_conclusion': 'data_mismatch_header_unresolved',
                'public_facts': ('data_mismatch', 'hdu_mismatch'),
                'baseline_conclusion': 'data_mismatch',
                'baseline_facts': (
                    'data_mismatch',
                    'hdu_mismatch',
                    'header_unchanged',
                ),
                'public_hides': ('dataok', 'hduok'),
            },
        ),
        (
            'header_and_data',
            'BOTH',
            True,
            True,
            {
                'public': 'data checksum failed',
                'status': {'dataok': -1, 'hduok': -1},
                'changed_cards': {'SITE'},
                'public_conclusion': 'data_mismatch_header_unresolved',
                'public_facts': ('data_mismatch', 'hdu_mismatch'),
                'baseline_conclusion': 'header_and_data_mismatch',
                'baseline_facts': (
                    'data_mismatch',
                    'hdu_mismatch',
                    'header_changed',
                ),
                'public_hides': ('dataok', 'hduok'),
            },
        ),
    ],
)
def test_damage_report_forward_and_reverse(
    tmp_path, variant, target, damage_header, damage_data, expected
):
    baseline_path = tmp_path / 'baseline.fits'
    baseline_raw, baseline = _build_baseline(baseline_path)
    damaged_path = _write_variant(
        baseline_raw,
        baseline,
        tmp_path / ('%s.fits' % variant),
        target,
        header=damage_header,
        data=damage_data,
    )

    with FITS(str(damaged_path), 'r') as fits:
        forward = _probe_in_order(fits, _EXT_NAMES)
        reverse = _probe_in_order(fits, tuple(reversed(_EXT_NAMES)))
        assert forward == reverse

        report = _evidence_report(fits, baseline)
        reverse_report = _evidence_report(
            fits, baseline, order=tuple(reversed(_EXT_NAMES))
        )
        assert report == reverse_report
        public_only = _evidence_report(fits)
        recorded = _recorded_checksums(fits, target)
        damaged_raw = fits.read_raw()

    evidence = baseline[target]
    assert _checksum_cards(
        damaged_raw[evidence.header_start:evidence.data_start]
    ) == _checksum_cards(evidence.header_bytes)

    actual = report[target]
    assert actual['name'] == target
    assert actual['extnum'] == baseline[target].extnum
    assert recorded == (baseline[target].checksum, baseline[target].datasum)
    assert actual['public'] == expected['public']
    assert actual['status'] == expected['status']
    assert actual['changed_cards'] == expected['changed_cards']
    assert actual['public_hides'] == expected['public_hides']

    public_diag = public_only[target]['diagnosis']
    assert public_diag['conclusion'] == expected['public_conclusion']
    assert public_diag['facts'] == expected['public_facts']

    baseline_diag = actual['diagnosis']
    assert baseline_diag['conclusion'] == expected['baseline_conclusion']
    assert baseline_diag['facts'] == expected['baseline_facts']

    healthy = report['HEALTHY']
    assert public_only['HEALTHY']['diagnosis']['conclusion'] == 'verified'
    assert healthy['public'] == 'ok'
    assert healthy['public_hides'] == ()
    assert healthy['status'] == {'dataok': 1, 'hduok': 1}
    assert healthy['changed_cards'] == set()
    assert healthy['diagnosis'] == {
        'facts': ('data_match', 'hdu_match', 'header_unchanged'),
        'conclusion': 'verified',
    }

    missing = report['MISSING']
    assert missing['public'] == 'data checksum failed'
    assert missing['status'] == {'dataok': 0, 'hduok': -1}
    assert missing['present'] == {'CHECKSUM': True, 'DATASUM': False}
    assert missing['public_hides'] == (
        'dataok',
        'hduok',
        'datasum_absent',
    )
    assert missing['changed_cards'] == set()

    # The retained CHECKSUM was written while DATASUM still existed and is
    # kept as-is after the deliberate removal.  Relative to the post-removal
    # baseline the header is unchanged, so the HDU mismatch is explained by
    # the missing keyword itself and must not be read as independent header
    # damage.
    assert missing['diagnosis'] == {
        'facts': (
            'data_unverifiable',
            'datasum_absent',
            'hdu_mismatch',
            'header_unchanged',
        ),
        'conclusion': 'datasum_unavailable',
    }

    # Without the baseline the same status cannot separate a deleted DATASUM
    # card from data damage plus an unrelated header edit.
    public_missing = public_only['MISSING']['diagnosis']
    assert public_missing['facts'] == (
        'data_unverifiable',
        'datasum_absent',
        'hdu_mismatch',
    )
    assert public_missing['conclusion'] == 'datasum_unavailable'

    for name in _EXT_NAMES:
        if name in ('HEALTHY', 'MISSING', target):
            continue
        assert report[name]['diagnosis']['conclusion'] == 'verified'
        assert report[name]['changed_cards'] == set()


def test_data_only_and_header_plus_data_need_baseline(tmp_path):
    baseline_path = tmp_path / 'baseline.fits'
    baseline_raw, baseline = _build_baseline(baseline_path)

    data_path = _write_variant(
        baseline_raw, baseline, tmp_path / 'data_only.fits', 'DATAONLY',
        data=True,
    )
    both_path = _write_variant(
        baseline_raw, baseline, tmp_path / 'both.fits', 'BOTH',
        header=True, data=True,
    )

    with FITS(str(data_path), 'r') as fits:
        data_public = _evidence_report(fits)
        data_baseline = _evidence_report(fits, baseline)
        data_recorded = _recorded_checksums(fits, 'DATAONLY')
    with FITS(str(both_path), 'r') as fits:
        both_public = _evidence_report(fits)
        both_baseline = _evidence_report(fits, baseline)
        both_recorded = _recorded_checksums(fits, 'BOTH')

    assert data_recorded == (
        baseline['DATAONLY'].checksum,
        baseline['DATAONLY'].datasum,
    )
    assert both_recorded == (
        baseline['BOTH'].checksum,
        baseline['BOTH'].datasum,
    )
    assert data_public['DATAONLY']['extnum'] == baseline['DATAONLY'].extnum
    assert both_public['BOTH']['extnum'] == baseline['BOTH'].extnum

    assert data_public['DATAONLY']['public'] == 'data checksum failed'
    assert both_public['BOTH']['public'] == 'data checksum failed'
    assert data_public['DATAONLY']['status'] == both_public['BOTH']['status']
    assert data_public['DATAONLY']['public_hides'] == ('dataok', 'hduok')
    assert both_public['BOTH']['public_hides'] == ('dataok', 'hduok')

    assert (
        data_public['DATAONLY']['diagnosis']['conclusion']
        == both_public['BOTH']['diagnosis']['conclusion']
        == 'data_mismatch_header_unresolved'
    )
    assert data_public['DATAONLY']['diagnosis']['facts'] == (
        'data_mismatch',
        'hdu_mismatch',
    )
    assert both_public['BOTH']['diagnosis']['facts'] == (
        'data_mismatch',
        'hdu_mismatch',
    )

    assert data_baseline['DATAONLY']['changed_cards'] == set()
    assert (
        data_baseline['DATAONLY']['diagnosis']['conclusion']
        == 'data_mismatch'
    )
    assert data_baseline['DATAONLY']['diagnosis']['facts'] == (
        'data_mismatch',
        'hdu_mismatch',
        'header_unchanged',
    )
    assert both_baseline['BOTH']['changed_cards'] == {'SITE'}
    assert both_baseline['BOTH']['diagnosis']['conclusion'] == (
        'header_and_data_mismatch'
    )
    assert both_baseline['BOTH']['diagnosis']['facts'] == (
        'data_mismatch',
        'hdu_mismatch',
        'header_changed',
    )


def test_missing_checksum_keyword_is_not_a_mismatch(tmp_path):
    baseline_path = tmp_path / 'baseline.fits'
    baseline_raw, baseline = _build_baseline(
        baseline_path, missing_keyword='CHECKSUM'
    )
    damaged_path = _write_variant(
        baseline_raw,
        baseline,
        tmp_path / 'header_only.fits',
        'HDRONLY',
        header=True,
    )

    with FITS(str(damaged_path), 'r') as fits:
        report = _evidence_report(fits, baseline)
        recorded = _recorded_checksums(fits, 'HDRONLY')

    assert recorded == (
        baseline['HDRONLY'].checksum,
        baseline['HDRONLY'].datasum,
    )
    assert report['HDRONLY']['extnum'] == baseline['HDRONLY'].extnum

    missing = report['MISSING']
    assert missing['present'] == {'CHECKSUM': False, 'DATASUM': True}
    assert missing['status'] == {'dataok': 1, 'hduok': 0}
    assert missing['public'] == 'hdu checksum failed'
    assert missing['public_hides'] == (
        'dataok',
        'hduok',
        'checksum_absent',
    )
    assert missing['diagnosis'] == {
        'facts': (
            'checksum_absent',
            'data_match',
            'hdu_unverifiable',
            'header_unchanged',
        ),
        'conclusion': 'checksum_unavailable',
    }

    mismatch = report['HDRONLY']
    assert mismatch['present'] == {'CHECKSUM': True, 'DATASUM': True}
    assert mismatch['status'] == {'dataok': 1, 'hduok': -1}
    assert mismatch['public'] == missing['public']
    assert mismatch['diagnosis']['conclusion'] == 'header_mismatch'
