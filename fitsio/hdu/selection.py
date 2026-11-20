"""
Strict, evidence-based selection of FITS extensions.

The ordinary ``FITS[name]`` and ``FITS[(name, version)]`` lookups keep their
historical behavior, including silently taking the first matching extension.
Archive imports that must know exactly which on-disk extension was selected
use the strict machinery in this module instead.

A strict selection never defaults to the first or last match.  When more than
one extension matches, the full list of candidates together with the header
fields that made them match is returned (or raised) so the caller can pick an
extension by its physical number.
"""

from ..util import isinteger, mks


# Modern FITS keywords.
EXTNAME_KEYWORD = 'EXTNAME'
EXTVER_KEYWORD = 'EXTVER'

# Deprecated predecessor keywords, kept for reading old files only.
HDUNAME_KEYWORD = 'HDUNAME'
HDUVER_KEYWORD = 'HDUVER'


class ExtensionAmbiguousError(Exception):
    """
    Raised when a strict selection matches more than one extension.

    No winner is chosen.  The ``candidates`` attribute holds every matching
    extension, including the header fields each match came from.  Select one
    explicitly by its physical extension number.
    """

    def __init__(self, message, candidates):
        super(ExtensionAmbiguousError, self).__init__(message)
        self.message = message
        self.candidates = list(candidates)

    def __str__(self):
        return self.message


class ExtensionCandidate(object):
    """
    One extension that matched a strict selection request.

    attributes
    ----------
    extnum: int
        Zero-based physical extension number of the matching HDU.
    extname: str
        The extension name exactly as it appears on disk.
    name_keyword: str
        The header keyword that supplied ``extname``: ``'EXTNAME'`` or, for
        older files, ``'HDUNAME'``.
    extver: int
        The modern EXTVER value, or 0 when no EXTVER keyword is present.
    hduver: int
        The legacy HDUVER value, or 0 when absent.  HDUVER is never treated
        as a modern version when matching an explicit version request.
    version_keyword: str or None
        ``'EXTVER'`` when the modern version keyword is present, otherwise
        None.  A legacy HDUVER is reported through ``hduver`` only.
    """

    def __init__(self, hdu):
        info = hdu.get_info()
        self.extnum = int(info['extnum'])

        raw_extname = info['extname'].strip()
        if raw_extname:
            self.extname = raw_extname
            self.name_keyword = EXTNAME_KEYWORD
        else:
            self.extname = info['hduname'].strip()
            self.name_keyword = HDUNAME_KEYWORD

        self.extver = int(info['extver'])
        self.hduver = int(info['hduver'])
        self.version_keyword = EXTVER_KEYWORD if self.extver > 0 else None

    def as_dict(self):
        """
        Return the candidate evidence as a plain dictionary.
        """
        return {
            'extnum': self.extnum,
            'extname': self.extname,
            'name_keyword': self.name_keyword,
            'extver': self.extver,
            'hduver': self.hduver,
            'version_keyword': self.version_keyword,
        }

    def version_line(self):
        """
        Human-readable description of the version evidence.
        """
        if self.version_keyword == EXTVER_KEYWORD:
            return 'EXTVER=%d' % self.extver
        if self.hduver > 0:
            return 'HDUVER=%d (legacy, no EXTVER)' % self.hduver
        return 'no version keyword'

    def __repr__(self):
        return (
            'ExtensionCandidate(extnum=%d, extname=%r, extver=%d, '
            'hduver=%d, name_keyword=%r, version_keyword=%r)'
            % (
                self.extnum,
                self.extname,
                self.extver,
                self.hduver,
                self.name_keyword,
                self.version_keyword,
            )
        )


class ExtensionSelection(object):
    """
    Result of a strict extension lookup.

    Do not construct this directly; use ``FITS.select_extension``.

    attributes
    ----------
    requested_name: str or None
        The name that was requested, or None for a physical-number selection.
    requested_version: int or None
        The explicitly requested version, or None when no version condition
        was given.
    version_requested: bool
        True when an explicit version was requested.  This is kept separate
        from the requested value so that "no version condition" can be told
        apart from "version explicitly matched an extension without one".
    case_sensitive: bool
        Whether name matching was case sensitive.
    candidates: list of ExtensionCandidate
        Every extension that matched, in physical extension order.
    """

    def __init__(
        self,
        fits,
        requested_name,
        requested_version,
        version_requested,
        case_sensitive,
        candidates,
    ):
        self._fits = fits
        self.requested_name = requested_name
        self.requested_version = requested_version
        self.version_requested = version_requested
        self.case_sensitive = case_sensitive
        self.candidates = list(candidates)

    @property
    def is_unique(self):
        """
        True when exactly one extension matched.
        """
        return len(self.candidates) == 1

    def require_unique(self):
        """
        Return the single candidate, raising if the selection is not unique.

        raises
        ------
        OSError
            When no extension matched.
        ExtensionAmbiguousError
            When more than one extension matched.
        """
        if len(self.candidates) == 1:
            return self.candidates[0]
        if len(self.candidates) == 0:
            raise OSError(self._not_found_message())
        raise ExtensionAmbiguousError(
            self._ambiguous_message(), self.candidates
        )

    @property
    def extnum(self):
        """
        Physical extension number of the unique match.
        """
        return self.require_unique().extnum

    @property
    def hdu(self):
        """
        The HDU object of the unique match.
        """
        return self._fits.hdu_list[self.require_unique().extnum]

    def _selection_label(self):
        mode = 'case sensitive' if self.case_sensitive else 'case insensitive'
        if self.requested_name is None:
            return 'physical extension selection (%s)' % mode
        if self.version_requested:
            return "name=%r version=%d (%s)" % (
                self.requested_name,
                self.requested_version,
                mode,
            )
        return "name=%r (%s)" % (self.requested_name, mode)

    @staticmethod
    def _candidate_line(candidate):
        return 'ext %d: %s=%r, %s' % (
            candidate.extnum,
            candidate.name_keyword,
            candidate.extname,
            candidate.version_line(),
        )

    def _ambiguous_message(self):
        lines = [
            'strict extension selection for %s is ambiguous: %d matching '
            'extensions, refusing to default to first or last:'
            % (
                self._selection_label(),
                len(self.candidates),
            )
        ]
        for candidate in self.candidates:
            lines.append('  ' + self._candidate_line(candidate))
        lines.append(
            'inspect the candidates and select one by physical '
            'extension number'
        )
        return '\n'.join(lines)

    def _not_found_message(self):
        mode = (
            '(case sensitive)' if self.case_sensitive else '(case insensitive)'
        )
        if not self.version_requested:
            return 'extension not found: %s %s' % (
                self.requested_name,
                mode,
            )

        message = 'extension not found: %s, version %s %s' % (
            self.requested_name,
            self.requested_version,
            mode,
        )
        same_name = _name_matches(
            self._fits,
            self.requested_name,
            self.case_sensitive,
        )
        if same_name:
            detail = ', '.join(
                self._candidate_line(candidate) for candidate in same_name
            )
            message += (
                '; extensions sharing the name but without a matching '
                'EXTVER: %s' % detail
            )
        return message

    def __repr__(self):
        return 'ExtensionSelection(%s, %d candidate(s))' % (
            self._selection_label(),
            len(self.candidates),
        )


def _names_match(candidate_name, requested_name, case_sensitive):
    if case_sensitive:
        return candidate_name == requested_name
    return candidate_name.lower() == requested_name.lower()


def _name_matches(fits, name, case_sensitive):
    """
    All named extensions matching the requested name, ignoring version.
    """
    matches = []
    for hdu in fits.hdu_list:
        candidate = ExtensionCandidate(hdu)
        if candidate.extname == '':
            continue
        if _names_match(candidate.extname, name, case_sensitive):
            matches.append(candidate)
    return matches


def build_selection(fits, name, version=None, case_sensitive=False):
    """
    Build a strict selection from the current extensions of a FITS object.

    Only the modern EXTVER keyword satisfies an explicit version request; an
    extension carrying only the legacy HDUVER keyword can still appear in a
    name-only selection, but its evidence makes the legacy source explicit.
    """
    name = mks(name)
    version_requested = version is not None
    if version_requested:
        version = int(version)
        if version <= 0:
            raise ValueError('extver must be > 0')

    candidates = []
    for hdu in fits.hdu_list:
        candidate = ExtensionCandidate(hdu)
        if candidate.extname == '':
            continue
        if not _names_match(candidate.extname, name, case_sensitive):
            continue
        if version_requested and candidate.extver != version:
            # HDUVER is deliberately not accepted as a modern version match.
            continue
        candidates.append(candidate)

    return ExtensionSelection(
        fits,
        requested_name=name,
        requested_version=version if version_requested else None,
        version_requested=version_requested,
        case_sensitive=case_sensitive,
        candidates=candidates,
    )


def select_by_number(fits, extnum, case_sensitive):
    """
    Wrap a physical-number selection in the same evidence machinery.
    """
    if not isinteger(extnum):
        raise ValueError('physical extension number must be an integer')

    try:
        hdu = fits.hdu_list[extnum]
    except IndexError:
        raise OSError('extension not found: %s' % extnum)

    return ExtensionSelection(
        fits,
        requested_name=None,
        requested_version=None,
        version_requested=False,
        case_sensitive=case_sensitive,
        candidates=[ExtensionCandidate(hdu)],
    )
