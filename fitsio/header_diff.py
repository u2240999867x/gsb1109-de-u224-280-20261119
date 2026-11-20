"""
Applyable change sets for FITS headers.

This module records the difference between two headers as a list of
changes that keep the identity of each card.  The identity of a card is
more than its keyword: ``COMMENT`` and ``HISTORY`` may occur many times
and two occurrences can have exactly the same value.  A change therefore
carries the occurrence number, the logical value, and the surrounding
anchors that identify *which* occurrence is meant.

The changes are applied atomically: either every change can be located
and applied, or the header is left untouched and the changes that could
not be located are reported.

Typical use::

    old = fitsio.read_header(filename, ext=0)
    new = fitsio.read_header(revision, ext=0)

    diff = old.diff(new)          # HeaderDiff
    for change in diff:
        print(change)

    applied = diff.apply(old)     # new FITSHDR, old untouched
    diff.apply(hdu.read_header(), in_place=True)

A change set can be serialized for an audit trail::

    payload = diff.to_dict()
    restored = HeaderDiff.from_dict(payload)

Continuation cards are folded into the logical card they belong to.  A
long string is one change, not one change for the keyword and separate
changes for each ``CONTINUE`` card.
"""

from __future__ import print_function

import copy
import difflib
import hashlib
import json

__all__ = [
    'HeaderChange',
    'HeaderDiff',
    'HeaderDiffError',
    'HeaderDiffResult',
    'diff_headers',
    'apply_header_diff',
    'normalize_header_records',
    'records_to_cards',
    'header_to_cards',
    'write_header',
    'write_header_diff',
]

# How many records to scan on each side when looking for an anchor that
# uniquely identifies a position.
_ANCHOR_SCAN = 12
# Number of immediately adjacent records remembered for insertions.
_IMMEDIATE = 2
# Number of identifying anchors remembered on each side.
_IDENTIFYING = 3

_CHANGE_KINDS = ('modify', 'insert', 'delete')


# ---------------------------------------------------------------------------
# record normalization helpers
# ---------------------------------------------------------------------------


def _name_of(record):
    name = record.get('name')
    if name is None:
        return None
    return str(name).upper()


def _norm_text(value):
    """
    Normalize a value for comparison.

    Trailing blanks in FITS strings are not significant, so they are
    stripped for matching.  The original value is still kept in the
    change so it can be written back exactly.
    """
    if isinstance(value, str):
        return ('s', value.rstrip())
    if isinstance(value, bool):
        return ('b', value)
    if isinstance(value, int):
        return ('i', value)
    if isinstance(value, float):
        return ('f', repr(value))
    if value is None:
        return ('n', None)
    return ('o', repr(value))


def _record_signature(record):
    """
    Signature used for sequence alignment.

    COMMENT/HISTORY cards have no FITS comment field of their own; their
    text is the logical value.  For ordinary keywords the value and the
    comment both participate.
    """
    name = _name_of(record)
    if name is None:
        # Blank keyword cards carry their text in the comment field.
        return (name, _norm_text(record.get('comment', '')))
    value = record.get('value')
    if name in ('COMMENT', 'HISTORY'):
        return (name, _norm_text(value))
    return (name, _norm_text(value), _norm_text(record.get('comment', '')))


def _anchor_of(record):
    """
    A compact, JSON-friendly description of a record used as context.
    """
    name = _name_of(record)
    if name is None:
        return {
            'name': name,
            'value': None,
            'comment': _plain(record.get('comment', '')),
        }
    if name in ('COMMENT', 'HISTORY'):
        return {
            'name': name,
            'value': _plain(record.get('value')),
        }
    return {
        'name': name,
        'value': _plain(record.get('value')),
        'comment': _plain(record.get('comment', '')),
    }


def _plain(value):
    """
    Convert numpy scalars and similar objects to plain python values.
    """
    if isinstance(value, (str, bool, int, float)) or value is None:
        return value
    if hasattr(value, 'item'):
        try:
            return value.item()
        except Exception:
            pass
    return str(value)


def _anchor_signature(anchor):
    name = anchor.get('name')
    if name is not None:
        name = str(name).upper()
    if name is None:
        return (name, _norm_text(anchor.get('comment', '')))
    value = anchor.get('value')
    if name in ('COMMENT', 'HISTORY'):
        return (name, _norm_text(value))
    return (
        name,
        _norm_text(value),
        _norm_text(anchor.get('comment', '')),
    )


def _canonical_record(record):
    """
    Copy a record to the small, plain representation used internally.
    """
    name = _name_of(record)
    value = record.get('value')
    comment = record.get('comment', '')

    if name is None:
        # A blank keyword: the text lives in the comment field.
        return {'name': None, 'value': None, 'comment': comment}

    if name in ('COMMENT', 'HISTORY'):
        # The FITSCard parser puts the text in both value and comment for
        # these cards.  Keep just the logical value.
        text = value
        if text is None:
            text = comment
        return {'name': name, 'value': text, 'comment': ''}

    return {
        'name': name,
        'value': copy.deepcopy(value),
        'comment': copy.deepcopy(comment),
    }


def _continuation_text(record):
    """
    Extract the text of a CONTINUE card.

    The returned text is the part inside the quotes, with the trailing
    ampersand removed.  Text after the closing quote is a comment and is
    not part of the continued value.
    """
    card = record.get('card_string')
    if card is None:
        card = record.get('value', '')
        if isinstance(card, str) and card[:8].upper() == 'CONTINUE':
            segment = card[8:]
        else:
            segment = card
    else:
        segment = card[8:]

    if not isinstance(segment, str):
        segment = str(segment)

    segment = segment.rstrip()
    first = segment.find("'")
    if first < 0:
        text = segment.strip()
    else:
        last = segment.rfind("'")
        if last <= first:
            last = len(segment)
        text = segment[first + 1 : last]

    if text.endswith('&'):
        text = text[:-1]
    return text


def _fold_continuations(records):
    """
    Return logical records with all CONTINUE cards folded into the
    preceding long-string card.

    A header read through cfitsio already has the long string assembled
    into one record.  Headers built directly from card strings may still
    contain explicit CONTINUE cards; those are folded here so that they
    never show up as independent changes.
    """
    out = []

    for record in records:
        name = _name_of(record)

        if name == 'CONTINUE':
            if not out:
                # A stray continuation cannot be attached to a card.  Keep
                # it as a record rather than silently dropping it.
                out.append(_canonical_record(record))
                continue

            previous = out[-1]
            prev_name = _name_of(previous)
            if prev_name in ('COMMENT', 'HISTORY', None):
                # Continuations belong to string-valued keyword cards.
                out.append(_canonical_record(record))
                continue

            prev_value = previous.get('value')
            if not isinstance(prev_value, str):
                prev_value = str(prev_value)

            if prev_value.endswith('&'):
                prev_value = prev_value[:-1]

            previous['value'] = prev_value + _continuation_text(record)
            continue

        out.append(_canonical_record(record))

    return out


def normalize_header_records(header):
    """
    Return the logical records of a header.

    ``header`` may be a FITSHDR, a list of record dictionaries, a list of
    card strings, or a sequence already in that form.  CONTINUE cards are
    folded into the card they continue.
    """
    if hasattr(header, 'records'):
        records = header.records()
    else:
        records = header

    return _fold_continuations(list(records))


# ---------------------------------------------------------------------------
# occurrence and anchor helpers
# ---------------------------------------------------------------------------


def _occurrences(records):
    """
    Map keyword -> list of record indices.
    """
    occurrences = {}
    for index, record in enumerate(records):
        occurrences.setdefault(_name_of(record), []).append(index)
    return occurrences


def _occurrence_number(records, index):
    """
    One-based occurrence number of the record at ``index``.
    """
    name = _name_of(records[index])
    return _occurrences(records)[name].index(index) + 1


def _identifying_anchors(records, index, counts):
    """
    Find records near ``index`` whose signature is unique in the header.

    These are the cards that say *which* of several identical HISTORY
    cards is meant, for example an observation-number marker in the same
    group.  They are deliberately kept separate from the immediately
    adjacent cards so that unrelated cards can be inserted nearby without
    invalidating the change.
    """
    before = []
    after = []

    for position in range(index - 1, max(-1, index - _ANCHOR_SCAN - 1), -1):
        signature = _record_signature(records[position])
        if counts.get(signature, 0) == 1:
            before.append(_anchor_of(records[position]))
            if len(before) >= _IDENTIFYING:
                break

    for position in range(
        index + 1, min(len(records), index + _ANCHOR_SCAN + 1)
    ):
        signature = _record_signature(records[position])
        if counts.get(signature, 0) == 1:
            after.append(_anchor_of(records[position]))
            if len(after) >= _IDENTIFYING:
                break

    # Store them in header order rather than scan order.
    before.reverse()
    return before, after


def _anchor_matches(record, anchor):
    return _record_signature(record) == _anchor_signature(anchor)


def _find_identifying(records, anchors, start, stop, step):
    """
    Find ``anchors`` in records[start:stop] in the given direction.

    All anchors must occur in order.  Returns True if they do.
    """
    if not anchors:
        return True

    position = start
    for anchor in anchors:
        found = False
        while position != stop:
            if _anchor_matches(records[position], anchor):
                found = True
                position += step
                break
            position += step
        if not found:
            return False
    return True


def _resolve_by_anchor(records, change):
    """
    Find the record index (modify/delete) or insertion boundary (insert)
    that matches a change.

    Returns ``(index, reason)``.  ``index`` is None if the change could
    not be resolved.
    """
    key = change.keyword
    kind = change.action

    if kind == 'insert':
        return _resolve_insert(records, change)

    candidates = _occurrences(records).get(key, [])

    # Prefer the recorded index when it still has the expected value.
    ordered = []
    if change.old_index is not None and 0 <= change.old_index < len(records):
        ordered.append(change.old_index)
    ordered.extend(i for i in candidates if i not in ordered)

    for index in ordered:
        if not _value_matches(
            records[index], change.old_value, change.old_comment
        ):
            continue
        if _anchors_confirm(records, index, change):
            return index, None

    if not candidates:
        return None, "keyword '%s' not found" % (key,)

    if len(candidates) == 1:
        index = candidates[0]
        return index, None

    return None, (
        "keyword '%s' occurs %d times and the surrounding anchors do not "
        "identify which occurrence is meant" % (key, len(candidates))
    )


def _resolve_insert(records, change):
    """
    Find the insertion boundary for an inserted card.
    """
    if not _anchors_confirm_insert(records, change):
        return None, (
            "the position for the new '%s' card cannot be confirmed"
            % (change.keyword,)
        )

    # Immediate context determines the boundary.  If there is no immediate
    # context at all (empty header) the only possible boundary is zero.
    immediate_before = change.anchor_before_immediate
    immediate_after = change.anchor_after_immediate

    positions = []
    for boundary in range(len(records) + 1):
        if boundary < len(immediate_before):
            continue
        if boundary > len(records) - len(immediate_after):
            continue

        ok = True
        for offset, anchor in enumerate(immediate_before):
            position = boundary - len(immediate_before) + offset
            if not _anchor_matches(records[position], anchor):
                ok = False
                break
        if not ok:
            continue

        for offset, anchor in enumerate(immediate_after):
            if not _anchor_matches(records[boundary + offset], anchor):
                ok = False
                break
        if not ok:
            continue

        positions.append(boundary)

    if len(positions) == 1:
        return positions[0], None

    if not positions:
        return None, (
            "the position for the new '%s' card cannot be confirmed: its "
            "neighbouring cards were not found" % (change.keyword,)
        )

    # Several boundaries have the same immediate neighbourhood (for
    # example adjacent identical HISTORY cards).  The recorded position is
    # still trustworthy when it is one of them and its identifying anchors
    # were already confirmed above.
    if change.old_index in positions:
        return change.old_index, None

    return None, (
        "the position for the new '%s' card is ambiguous" % (change.keyword,)
    )


def _value_matches(record, value, comment):
    if _name_of(record) is None:
        # Blank keyword cards only have a comment field.
        return _norm_text(record.get('comment', '')) == _norm_text(comment)
    if _norm_text(record.get('value')) != _norm_text(value):
        return False
    if _name_of(record) in ('COMMENT', 'HISTORY'):
        return True
    return _norm_text(record.get('comment', '')) == _norm_text(comment)


def _anchors_confirm(records, index, change):
    before = change.anchor_before
    after = change.anchor_after

    if before:
        # The anchors are stored in header order; walking backwards means
        # starting with the one closest to the change.
        if not _find_identifying(
            records, list(reversed(before)), index - 1, -1, -1
        ):
            return False
    if after:
        if not _find_identifying(records, after, index + 1, len(records), 1):
            return False
    return True


def _anchors_confirm_insert(records, change):
    before = change.anchor_before
    after = change.anchor_after

    # The boundary is located using the immediate context, so only the
    # identifying anchors need to be present here for identity.
    if before and not _find_identifying(
        records, list(reversed(before)), len(records) - 1, -1, -1
    ):
        return False
    if after and not _find_identifying(records, after, 0, len(records), 1):
        return False
    return True


# ---------------------------------------------------------------------------
# change objects
# ---------------------------------------------------------------------------


class HeaderChange(object):
    """
    One applicable change to a FITS header.

    Attributes
    ----------
    action: string
        'modify', 'insert' or 'delete'.
    keyword: string or None
        Card keyword.  COMMENT/HISTORY are ordinary keywords here, so
        each occurrence is a separate change.
    occurrence: int
        One-based occurrence of the keyword.  For an insertion this is
        the occurrence in the revised header; otherwise it is the
        occurrence in the original header.
    old_value, new_value:
        Logical values before and after the change.
    old_comment, new_comment:
        Comment fields before and after the change.  For COMMENT and
        HISTORY the logical value is the card text.
    old_index, new_index: int or None
        Position in the logical record list of the old/new header.
    anchor_before, anchor_after: list of dict
        Identifying cards around the change.  These are what distinguish
        the second identical HISTORY from the first one.
    """

    def __init__(
        self,
        action,
        keyword,
        occurrence,
        old_value=None,
        new_value=None,
        old_comment='',
        new_comment='',
        old_index=None,
        new_index=None,
        anchor_before=None,
        anchor_after=None,
        anchor_before_immediate=None,
        anchor_after_immediate=None,
    ):
        if action not in _CHANGE_KINDS:
            raise ValueError(
                "action must be one of %s, got %r"
                % (', '.join(_CHANGE_KINDS), action)
            )

        self.action = action
        self.keyword = keyword
        self.occurrence = occurrence
        self.old_value = old_value
        self.new_value = new_value
        self.old_comment = old_comment
        self.new_comment = new_comment
        self.old_index = old_index
        self.new_index = new_index
        self.anchor_before = list(anchor_before or [])
        self.anchor_after = list(anchor_after or [])
        self.anchor_before_immediate = list(anchor_before_immediate or [])
        self.anchor_after_immediate = list(anchor_after_immediate or [])

    @property
    def name(self):
        return self.keyword

    @property
    def key(self):
        return self.keyword

    @property
    def kind(self):
        return self.action

    def to_dict(self):
        return {
            'action': self.action,
            'keyword': self.keyword,
            'name': self.keyword,
            'occurrence': self.occurrence,
            'old_value': _plain(self.old_value),
            'new_value': _plain(self.new_value),
            'old_comment': _plain(self.old_comment),
            'new_comment': _plain(self.new_comment),
            'old_index': self.old_index,
            'new_index': self.new_index,
            'anchor_before': [dict(a) for a in self.anchor_before],
            'anchor_after': [dict(a) for a in self.anchor_after],
            'anchor_before_immediate': [
                dict(a) for a in self.anchor_before_immediate
            ],
            'anchor_after_immediate': [
                dict(a) for a in self.anchor_after_immediate
            ],
        }

    as_dict = to_dict

    @classmethod
    def from_dict(cls, payload):
        return cls(
            action=payload['action'],
            keyword=payload.get('keyword', payload.get('name')),
            occurrence=payload.get('occurrence', 1),
            old_value=payload.get('old_value'),
            new_value=payload.get('new_value'),
            old_comment=payload.get('old_comment', ''),
            new_comment=payload.get('new_comment', ''),
            old_index=payload.get('old_index'),
            new_index=payload.get('new_index'),
            anchor_before=payload.get('anchor_before', []),
            anchor_after=payload.get('anchor_after', []),
            anchor_before_immediate=payload.get('anchor_before_immediate', []),
            anchor_after_immediate=payload.get('anchor_after_immediate', []),
        )

    def __getitem__(self, key):
        return self.to_dict()[key]

    def keys(self):
        return self.to_dict().keys()

    def items(self):
        return self.to_dict().items()

    def __eq__(self, other):
        if isinstance(other, HeaderChange):
            return self.to_dict() == other.to_dict()
        if isinstance(other, dict):
            return self.to_dict() == other
        return NotImplemented

    def describe(self):
        if self.action == 'modify':
            return "modify %s occurrence %d (record %s): %r -> %r" % (
                self.keyword,
                self.occurrence,
                self.old_index,
                self.old_value,
                self.new_value,
            )
        elif self.action == 'insert':
            return "insert %s occurrence %d before record %s: %r" % (
                self.keyword,
                self.occurrence,
                self.old_index,
                self.new_value,
            )
        else:
            return "delete %s occurrence %d (record %s): %r" % (
                self.keyword,
                self.occurrence,
                self.old_index,
                self.old_value,
            )

    def __repr__(self):
        return "HeaderChange(%s)" % (self.describe(),)

    def __str__(self):
        return self.describe()


class HeaderDiffResult(object):
    """
    Result of applying a :class:`HeaderDiff`.

    Attributes
    ----------
    header: FITSHDR
        The header the changes were applied to.  On failure this is the
        original, unchanged header.
    applied_changes: list
        Changes that were applied (all of them on success).
    failed_changes: list
        Changes that could not be located (empty on success).
    failures: list of dict
        Each entry has 'change' and 'reason'.
    success: bool
    changed: bool
        True if at least one record differs.
    """

    def __init__(
        self,
        header,
        applied_changes=None,
        failures=None,
        in_place=False,
    ):
        self.header = header
        self.applied_changes = list(applied_changes or [])
        self.failures = list(failures or [])
        self.in_place = in_place

    @property
    def failed_changes(self):
        return [failure['change'] for failure in self.failures]

    @property
    def success(self):
        return not self.failures

    @property
    def changed(self):
        return bool(self.applied_changes)

    def __bool__(self):
        return self.success

    __nonzero__ = __bool__

    def raise_if_failed(self):
        if self.failures:
            raise HeaderDiffError(self.failures)
        return self.header


class HeaderDiffError(ValueError):
    """
    Raised when one or more changes cannot be located.

    The header is never modified when this is raised.  ``failed_changes``
    is the list of :class:`HeaderChange` objects that failed; ``failures``
    contains the same changes with a reason.
    """

    def __init__(self, failures, message=None):
        self.failures = list(failures)
        self.failed_changes = [failure['change'] for failure in self.failures]
        self.failed = self.failed_changes

        if message is None:
            lines = [
                "%d of %d header changes could not be applied; the header "
                "was not modified"
                % (len(self.failed_changes), len(self.failed_changes))
            ]
            for failure in self.failures:
                lines.append(
                    "  - %s: %s"
                    % (failure['change'].describe(), failure['reason'])
                )
            message = '\n'.join(lines)
        super(HeaderDiffError, self).__init__(message)


# ---------------------------------------------------------------------------
# diff construction
# ---------------------------------------------------------------------------


def _make_anchor_context(records, index, counts, is_insert=False):
    """
    Build the anchor lists for a change.

    ``index`` is the record index for modify/delete, or the insertion
    boundary for insert.
    """
    if is_insert:
        immediate_before = records[max(0, index - _IMMEDIATE) : index]
        immediate_after = records[index : index + _IMMEDIATE]
        # Identifying anchors are searched on the respective side of the
        # boundary.
        before = []
        for position in range(
            index - 1, max(-1, index - _ANCHOR_SCAN - 1), -1
        ):
            signature = _record_signature(records[position])
            if counts.get(signature, 0) == 1:
                before.append(_anchor_of(records[position]))
                if len(before) >= _IDENTIFYING:
                    break
        after = []
        for position in range(index, min(len(records), index + _ANCHOR_SCAN)):
            signature = _record_signature(records[position])
            if counts.get(signature, 0) == 1:
                after.append(_anchor_of(records[position]))
                if len(after) >= _IDENTIFYING:
                    break
        before.reverse()
        return (
            before,
            after,
            [_anchor_of(r) for r in immediate_before],
            [_anchor_of(r) for r in immediate_after],
        )

    before, after = _identifying_anchors(records, index, counts)
    immediate_before = records[max(0, index - _IMMEDIATE) : index]
    immediate_after = records[index + 1 : index + 1 + _IMMEDIATE]
    return (
        before,
        after,
        [_anchor_of(r) for r in immediate_before],
        [_anchor_of(r) for r in immediate_after],
    )


def _new_record(change):
    """
    Return the record represented by an inserted/modified change.
    """
    if change.keyword is None:
        return {
            'name': None,
            'value': None,
            'comment': copy.deepcopy(change.new_comment),
        }
    return {
        'name': change.keyword,
        'value': copy.deepcopy(change.new_value),
        'comment': copy.deepcopy(change.new_comment),
    }


class HeaderDiff(object):
    """
    A list of applicable changes between two FITS headers.

    The object is iterable and supports ``len()``, indexing and
    serialization with :meth:`to_dict` / :meth:`from_dict`.
    """

    def __init__(self, changes=None, old_header=None, new_header=None):
        self.changes = list(changes or [])
        self.old_header = old_header
        self.new_header = new_header

    # -- container protocol -------------------------------------------------

    def __iter__(self):
        return iter(self.changes)

    def __len__(self):
        return len(self.changes)

    def __getitem__(self, item):
        return self.changes[item]

    def __repr__(self):
        return "HeaderDiff(%d changes)" % (len(self.changes),)

    def __str__(self):
        if not self.changes:
            return "no header changes"
        return '\n'.join(change.describe() for change in self.changes)

    def __eq__(self, other):
        if isinstance(other, HeaderDiff):
            return self.changes == other.changes
        return NotImplemented

    # -- constructors -------------------------------------------------------

    @classmethod
    def from_headers(cls, old_header, new_header):
        """
        Build a change set from two headers.

        ``old_header`` and ``new_header`` may be FITSHDR objects or lists
        of records/cards.  Every COMMENT and HISTORY occurrence keeps its
        own identity.
        """
        old_records = normalize_header_records(old_header)
        new_records = normalize_header_records(new_header)
        return cls(
            _build_changes(old_records, new_records),
            old_header=old_header,
            new_header=new_header,
        )

    @classmethod
    def from_dict(cls, payload):
        return cls(
            [
                HeaderChange.from_dict(item)
                for item in payload.get('changes', [])
            ]
        )

    def to_dict(self):
        """
        Return a JSON-friendly representation suitable for an audit log.
        """
        return {
            'format': 'fitsio-header-diff',
            'version': 1,
            'changes': [change.to_dict() for change in self.changes],
        }

    as_dict = to_dict

    def to_json(self, **kwargs):
        return json.dumps(self.to_dict(), **kwargs)

    # -- applying -----------------------------------------------------------

    def apply_result(self, header, in_place=False):
        """
        Apply all changes, returning a :class:`HeaderDiffResult`.

        Regardless of ``in_place`` the application is atomic: if any
        change cannot be confirmed, no record in ``header`` is modified
        and the result lists the failed changes.
        """
        records = normalize_header_records(header)

        # First plan every change against the original header.  Nothing is
        # modified until the whole plan is known to be valid.
        plans = []
        failures = []
        for change in self.changes:
            if change.action == 'insert':
                index, reason = _resolve_insert(records, change)
            else:
                index, reason = _resolve_by_anchor(records, change)

            if index is None:
                failures.append({'change': change, 'reason': reason})
            else:
                plans.append((change, index))

        if failures:
            return HeaderDiffResult(
                header,
                applied_changes=[],
                failures=failures,
                in_place=in_place,
            )

        # Build the new record list in one pass.  Insertions are attached
        # to boundaries in the original coordinate system, so index shifts
        # cannot move a continuation or a neighbouring card.
        inserted = {}
        modifications = {}
        deletions = set()

        for change, index in plans:
            if change.action == 'insert':
                inserted.setdefault(index, []).append(change)
            elif change.action == 'modify':
                modifications[index] = change
            elif change.action == 'delete':
                deletions.add(index)

        new_records = []
        for index in range(len(records) + 1):
            for change in inserted.get(index, []):
                new_records.append(_new_record(change))

            if index == len(records):
                break

            if index in deletions:
                continue

            if index in modifications:
                new_records.append(_new_record(modifications[index]))
            else:
                new_records.append(copy.deepcopy(records[index]))

        new_header = _rebuild_header(new_records)

        if in_place:
            _replace_header_contents(header, new_header)

        return HeaderDiffResult(
            new_header if not in_place else header,
            applied_changes=[change for change, _ in plans],
            failures=[],
            in_place=in_place,
        )

    def apply(self, header, in_place=False):
        """
        Apply the changes to ``header`` and return the resulting FITSHDR.

        On success the returned header is either a new object
        (``in_place=False``) or the input object (``in_place=True``).
        On failure :class:`HeaderDiffError` is raised and ``header`` is
        not modified.
        """
        result = self.apply_result(header, in_place=in_place)
        if result.failures:
            raise HeaderDiffError(result.failures)
        return result.header

    apply_to = apply

    def can_apply(self, header):
        """
        Return True if every change can be located in ``header``.
        """
        return self.apply_result(header).success

    def failed_changes(self, header):
        """
        Return the list of changes that cannot be located in ``header``.
        """
        return self.apply_result(header).failed_changes

    # -- informative helpers ------------------------------------------------

    def summary(self):
        """
        Return a list of human-readable change descriptions.
        """
        return [change.describe() for change in self.changes]

    def fingerprint(self):
        """
        Stable digest of the serialized change set.
        """
        payload = json.dumps(
            self.to_dict(), sort_keys=True, separators=(',', ':')
        )
        return hashlib.sha256(payload.encode('utf-8')).hexdigest()


# ---------------------------------------------------------------------------
# diff algorithm
# ---------------------------------------------------------------------------


def _build_changes(old_records, new_records):
    old_signatures = [_record_signature(r) for r in old_records]
    new_signatures = [_record_signature(r) for r in new_records]

    matcher = difflib.SequenceMatcher(
        None, old_signatures, new_signatures, autojunk=False
    )

    counts = {}
    for signature in old_signatures:
        counts[signature] = counts.get(signature, 0) + 1

    changes = []

    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == 'equal':
            continue
        elif tag == 'delete':
            for index in range(i1, i2):
                changes.append(_make_delete(old_records, index, counts))
        elif tag == 'insert':
            for index in range(j1, j2):
                changes.append(
                    _make_insert(old_records, new_records, i1, index, counts)
                )
        elif tag == 'replace':
            changes.extend(
                _pair_replace(
                    old_records,
                    new_records,
                    i1,
                    i2,
                    j1,
                    j2,
                    counts,
                )
            )

    changes.sort(key=_change_sort_key)
    return _coalesce_orphans(changes, old_records, new_records, counts)


def _coalesce_orphans(changes, old_records, new_records, counts):
    """
    Pair an inserted and a deleted card of the same keyword into a
    modification.

    Sequence alignment reports a value change as an insert plus a delete
    when the header contains another card with the old value.  For audit
    purposes that is one logical card being modified, not two unrelated
    edits, so the pair is merged.
    """
    deletes = {}
    inserts = {}
    others = []

    for change in changes:
        if change.action == 'delete':
            deletes.setdefault(change.keyword, []).append(change)
        elif change.action == 'insert':
            inserts.setdefault(change.keyword, []).append(change)
        else:
            others.append(change)

    merged = []
    for keyword in sorted(set(deletes) | set(inserts)):
        deleted = sorted(
            deletes.get(keyword, []),
            key=lambda item: (
                item.old_index if item.old_index is not None else 10**9
            ),
        )
        inserted = sorted(
            inserts.get(keyword, []),
            key=lambda item: (
                item.new_index if item.new_index is not None else 10**9
            ),
        )

        pairs = min(len(deleted), len(inserted))
        offset = 0
        while offset < pairs:
            old_change = deleted[offset]
            new_change = inserted[offset]
            if not _same_anchor_context(old_change, new_change):
                # These are separate cards in different parts of the
                # header, not two views of one modification.
                break
            merged.append(
                _make_modify(
                    old_records,
                    old_change.old_index,
                    new_records,
                    new_change.new_index,
                    counts,
                )
            )
            offset += 1

        merged.extend(deleted[offset:])
        merged.extend(inserted[offset:])

    merged.extend(others)
    merged.sort(key=_change_sort_key)
    return merged


def _anchor_signatures(anchors):
    return [_anchor_signature(anchor) for anchor in anchors]


def _same_anchor_context(left, right):
    """
    True if two changes describe the same neighbourhood.

    This keeps the insert/delete merging from combining unrelated cards
    that merely share a keyword.  The immediate neighbours are included:
    a card inserted at one end of a run of identical cards must not be
    merged with a card deleted at the other end.
    """
    return (
        _anchor_signatures(left.anchor_before)
        == _anchor_signatures(right.anchor_before)
        and _anchor_signatures(left.anchor_after)
        == _anchor_signatures(right.anchor_after)
        and _anchor_signatures(left.anchor_before_immediate)
        == _anchor_signatures(right.anchor_before_immediate)
        and _anchor_signatures(left.anchor_after_immediate)
        == _anchor_signatures(right.anchor_after_immediate)
    )


def _change_sort_key(change):
    # Insertions are keyed by their position in the old header (the
    # boundary) and then by their position in the new header, so that
    # several cards inserted at one boundary keep their revised order.
    return (
        change.old_index if change.old_index is not None else 10**9,
        change.new_index if change.new_index is not None else 10**9,
        change.keyword or '',
    )


def _make_modify(old_records, old_index, new_records, new_index, counts):
    old = old_records[old_index]
    new = new_records[new_index]
    before, after, immediate_before, immediate_after = _make_anchor_context(
        old_records, old_index, counts
    )
    name = _name_of(old)
    return HeaderChange(
        action='modify',
        keyword=name,
        occurrence=_occurrence_number(old_records, old_index),
        old_value=copy.deepcopy(old.get('value')),
        new_value=copy.deepcopy(new.get('value')),
        old_comment=copy.deepcopy(old.get('comment', '')),
        new_comment=copy.deepcopy(new.get('comment', '')),
        old_index=old_index,
        new_index=new_index,
        anchor_before=before,
        anchor_after=after,
        anchor_before_immediate=immediate_before,
        anchor_after_immediate=immediate_after,
    )


def _make_delete(old_records, old_index, counts):
    old = old_records[old_index]
    before, after, immediate_before, immediate_after = _make_anchor_context(
        old_records, old_index, counts
    )
    return HeaderChange(
        action='delete',
        keyword=_name_of(old),
        occurrence=_occurrence_number(old_records, old_index),
        old_value=copy.deepcopy(old.get('value')),
        new_value=None,
        old_comment=copy.deepcopy(old.get('comment', '')),
        new_comment='',
        old_index=old_index,
        new_index=None,
        anchor_before=before,
        anchor_after=after,
        anchor_before_immediate=immediate_before,
        anchor_after_immediate=immediate_after,
    )


def _make_insert(old_records, new_records, boundary, new_index, counts):
    new = new_records[new_index]
    before, after, immediate_before, immediate_after = _make_anchor_context(
        old_records, boundary, counts, is_insert=True
    )

    new_occurrences = _occurrences(new_records)
    occurrence = new_occurrences[_name_of(new)].index(new_index) + 1

    return HeaderChange(
        action='insert',
        keyword=_name_of(new),
        occurrence=occurrence,
        old_value=None,
        new_value=copy.deepcopy(new.get('value')),
        old_comment='',
        new_comment=copy.deepcopy(new.get('comment', '')),
        old_index=boundary,
        new_index=new_index,
        anchor_before=before,
        anchor_after=after,
        anchor_before_immediate=immediate_before,
        anchor_after_immediate=immediate_after,
    )


def _pair_replace(old_records, new_records, i1, i2, j1, j2, counts):
    """
    Turn a replace block into modifications plus any leftovers.

    Records are paired by keyword and order within the block.  This keeps
    a changed HISTORY card as a modification of that occurrence rather
    than a delete followed by an insert.
    """
    old_block = list(range(i1, i2))
    new_block = list(range(j1, j2))

    old_by_name = {}
    new_by_name = {}
    order = []

    for index in old_block:
        name = _name_of(old_records[index])
        old_by_name.setdefault(name, []).append(index)
        if name not in order:
            order.append(name)
    for index in new_block:
        name = _name_of(new_records[index])
        new_by_name.setdefault(name, []).append(index)
        if name not in order:
            order.append(name)

    changes = []

    for name in order:
        old_indices = old_by_name.get(name, [])
        new_indices = new_by_name.get(name, [])
        paired = min(len(old_indices), len(new_indices))

        for offset in range(paired):
            changes.append(
                _make_modify(
                    old_records,
                    old_indices[offset],
                    new_records,
                    new_indices[offset],
                    counts,
                )
            )

        for index in old_indices[paired:]:
            changes.append(_make_delete(old_records, index, counts))

        for index in new_indices[paired:]:
            # The insertion boundary is taken from the position of the
            # corresponding old card when possible, otherwise from the
            # start of the replaced block.
            boundary = _insert_boundary(old_records, old_indices, i1)
            changes.append(
                _make_insert(old_records, new_records, boundary, index, counts)
            )

    return changes


def _insert_boundary(old_records, old_indices, fallback):
    if old_indices:
        return min(len(old_records), old_indices[0] + 1)
    return fallback


# ---------------------------------------------------------------------------
# rebuilding headers
# ---------------------------------------------------------------------------


def _rebuild_header(records):
    """
    Build a FITSHDR from logical records.
    """
    from .header import FITSHDR

    header = FITSHDR()
    for record in records:
        header.add_record(copy.deepcopy(record))
    return header


def _replace_header_contents(target, source):
    """
    Replace the records of ``target`` with those of ``source`` in place.
    """
    target._record_list = list(source._record_list)
    target._record_map = dict(source._record_map)
    target._index_map = dict(source._index_map)


# ---------------------------------------------------------------------------
# module level convenience functions
# ---------------------------------------------------------------------------


def diff_headers(old_header, new_header):
    """
    Return a :class:`HeaderDiff` describing how to turn ``old_header``
    into ``new_header``.
    """
    return HeaderDiff.from_headers(old_header, new_header)


def apply_header_diff(header, changes, in_place=False):
    """
    Apply a change set to ``header``.

    ``changes`` may be a :class:`HeaderDiff`, its serialized dict form, or
    a list of :class:`HeaderChange` / dict changes.
    """
    if isinstance(changes, HeaderDiff):
        diff = changes
    elif isinstance(changes, dict):
        diff = HeaderDiff.from_dict(changes)
    else:
        diff = HeaderDiff(
            [
                item
                if isinstance(item, HeaderChange)
                else HeaderChange.from_dict(item)
                for item in changes
            ]
        )
    return diff.apply(header, in_place=in_place)


# ---------------------------------------------------------------------------
# converting logical records back to FITS cards
# ---------------------------------------------------------------------------


def _quote_string(value):
    """
    Quote a string value for a FITS card, doubling embedded quotes.
    """
    value = str(value)
    value = value.replace("'", "''")
    return "'%s'" % (value,)


def _split_long_string(text, first_prefix, cont_prefix, comment=''):
    """
    Split a string into FITS long-string segments.

    The first card has the keyword prefix, every following card has the
    ``CONTINUE  '`` prefix.  Each segment except the last ends with ``&``.
    When a comment is given, room is reserved for it on the last card.
    """
    text = str(text)

    # Reserve space for the opening quote, the closing quote and the
    # trailing ampersand on every segment.
    first_capacity = 80 - len(first_prefix) - 3
    cont_capacity = 80 - len(cont_prefix) - 3

    if comment:
        # The comment goes after the closing quote on the last card.
        reserved = len(' / ' + str(comment))
        if cont_capacity - reserved >= 1:
            cont_capacity -= reserved

    segments = []
    first = text[:first_capacity]
    remaining = text[first_capacity:]
    segments.append(first)

    while remaining:
        piece = remaining[:cont_capacity]
        remaining = remaining[cont_capacity:]
        segments.append(piece)

    return segments


def _keyword_prefix(name):
    """
    Return the card prefix that introduces a keyword and its equals sign.
    """
    name = str(name)
    if len(name) > 8:
        return 'HIERARCH %s = ' % (name,), True
    return '%-8s= ' % (name,), False


def _format_keyword_card(name, value, comment):
    """
    Format an ordinary keyword card.
    """
    prefix, hierarch = _keyword_prefix(name)

    if isinstance(value, str):
        head = '%s%s' % (prefix, _quote_string(value))
    else:
        if value is True:
            vstr = 'T'
        elif value is False:
            vstr = 'F'
        elif value is None:
            vstr = ''
        else:
            vstr = str(value).upper()
        if hierarch:
            head = '%s%s' % (prefix, vstr)
        else:
            head = '%s%20s' % (prefix, vstr)

    if comment:
        head += ' / %s' % (str(comment),)

    return head[:80].ljust(80)


def record_to_cards(record):
    """
    Convert one logical record into a list of 80-character FITS cards.

    A long string produces its keyword card followed by one or more
    ``CONTINUE`` cards, so the continuation never appears as a separate
    logical change.
    """
    name = _name_of(record)
    value = record.get('value')
    comment = record.get('comment', '')

    if name is None:
        text = comment if comment is not None else ''
        return [('         ' + str(text))[:80].ljust(80)]

    if name == 'COMMENT':
        text = value if value is not None else comment
        return [('COMMENT ' + str(text))[:80].ljust(80)]

    if name == 'HISTORY':
        text = value if value is not None else comment
        return [('HISTORY ' + str(text))[:80].ljust(80)]

    if name == 'CONTINUE':
        return [('CONTINUE  ' + str(value))[:80].ljust(80)]

    if isinstance(value, str):
        prefix, _ = _keyword_prefix(name)
        first_prefix = prefix
        cont_prefix = 'CONTINUE  '
        single = _format_keyword_card(name, value, comment)
        if (
            len(single.rstrip()) <= 80
            and len(_quote_string(value)) + len(first_prefix) <= 80
        ):
            return [single]

        segments = _split_long_string(
            value, first_prefix, cont_prefix, comment
        )
        cards = []

        for index, segment in enumerate(segments):
            last = index == len(segments) - 1
            prefix = first_prefix if index == 0 else cont_prefix
            if last:
                card = "%s'%s'" % (prefix, segment)
                if comment:
                    candidate = '%s / %s' % (card, comment)
                    if len(candidate) <= 80:
                        card = candidate
            else:
                card = "%s'%s&'" % (prefix, segment)
            cards.append(card[:80].ljust(80))

        return cards

    return [_format_keyword_card(name, value, comment)]


def records_to_cards(records):
    """
    Convert logical records into a list of 80-character cards (no END).
    """
    cards = []
    for record in records:
        cards.extend(record_to_cards(record))
    return cards


def header_to_cards(header):
    """
    Convert a header to the cards that represent its logical records.
    """
    return records_to_cards(normalize_header_records(header))


# ---------------------------------------------------------------------------
# writing a header back into a FITS file without touching the data
# ---------------------------------------------------------------------------


def _header_blocks(cards):
    """
    Number of bytes occupied by ``cards`` including the END card and the
    2880-byte padding.
    """
    needed = len(cards) * 80
    return (needed + 2879) // 2880 * 2880


def _cards_byte_size(card_count):
    """
    Padded size in bytes of a header made of ``card_count`` cards.
    """
    return ((card_count * 80 + 2879) // 2880) * 2880


def write_header(filename, header, ext=0):
    """
    Replace the header of an existing HDU in place.

    The data section is moved as an opaque byte block; table or image
    values are not re-written or re-encoded.  This is what makes it safe
    to record header revisions on a file whose data must not change.
    """
    if ext is None:
        raise ValueError("ext must be an integer extension number")

    extension = int(ext)
    if extension < 0:
        raise ValueError("ext must be zero or greater")

    cards = header_to_cards(header)
    cards.append('END'.ljust(80))

    with open(filename, 'r+b') as fobj:
        offset = _find_header_start(fobj, extension)

        fobj.seek(offset)
        card_count = 0
        while True:
            card = fobj.read(80)
            if len(card) < 80:
                raise ValueError(
                    "unexpected end of file while reading the header"
                )
            card_count += 1
            if card.rstrip() == b'END':
                break
            if card_count > 100000:
                raise ValueError("could not find the END card")

        old_header_size = _cards_byte_size(card_count)
        data_start = offset + old_header_size

        fobj.seek(data_start)
        tail = fobj.read()

        new_header_size = _header_blocks(cards)
        block = ''.join(cards).encode('ascii', 'replace')
        block = block.ljust(new_header_size, b' ')

        fobj.seek(offset)
        fobj.write(block)
        fobj.write(tail)
        fobj.truncate(offset + new_header_size + len(tail))
        fobj.flush()

    return extension


def _find_header_start(fobj, extension):
    """
    Walk the file HDU by HDU and return the byte offset of the header of
    the requested extension.
    """
    offset = 0
    for current in range(extension + 1):
        fobj.seek(offset)
        cards = []
        while True:
            card = fobj.read(80)
            if len(card) < 80:
                raise ValueError("extension %d not found" % (extension,))
            cards.append(card)
            if card.rstrip() == b'END':
                break

        header_size = _cards_byte_size(len(cards))
        data_start = offset + header_size

        if current == extension:
            return offset

        data_size = _data_size(cards)
        offset = data_start + data_size

    raise ValueError("extension %d not found" % (extension,))


def _card_value(cards, name):
    prefix = name.ljust(8) + '='
    for card in cards:
        if isinstance(card, bytes):
            text = card.decode('ascii', 'replace')
        else:
            text = card
        if text[:9] == prefix:
            return text
    return None


def _card_int(cards, name):
    text = _card_value(cards, name)
    if text is None:
        return None
    value = text[10:].split('/')[0].strip()
    try:
        return int(value)
    except ValueError:
        return None


def _data_size(cards):
    """
    Compute the padded data size of an HDU from its header cards.

    Only the structural keywords (XTENSION, BITPIX, NAXIS*, PCOUNT and
    GCOUNT) are read; the data itself is never interpreted.
    """
    text_cards = [
        c.decode('ascii', 'replace') if isinstance(c, bytes) else c
        for c in cards
    ]

    xtension = None
    for card in text_cards:
        if card[:8].strip() == 'XTENSION':
            pieces = card[10:].split("'")
            if len(pieces) > 1:
                xtension = pieces[1].strip()

    naxis = _card_int(text_cards, 'NAXIS') or 0
    dims = []
    for index in range(1, naxis + 1):
        dims.append(_card_int(text_cards, 'NAXIS%d' % index) or 0)

    if xtension in ('BINTABLE', 'A3DTABLE'):
        rowlen = dims[0] if dims else 0
        nrows = dims[1] if len(dims) > 1 else 0
        return _round_up(nrows * rowlen)

    if xtension == 'TABLE':
        rowlen = dims[0] if dims else 0
        nrows = dims[1] if len(dims) > 1 else 0
        return _round_up(nrows * rowlen)

    bitpix = _card_int(text_cards, 'BITPIX') or 0
    pcount = _card_int(text_cards, 'PCOUNT') or 0
    gcount = _card_int(text_cards, 'GCOUNT') or 1

    if naxis == 0:
        # No data axes; only PCOUNT (used by random groups) remains.
        return _round_up((abs(bitpix) // 8) * pcount * gcount)

    count = 1
    for dim in dims:
        count *= dim

    # The standard formula covers both the primary array and image
    # extensions (PCOUNT is zero for ordinary images).
    nbytes = (abs(bitpix) // 8) * gcount * (pcount + count)
    return _round_up(nbytes)


def _round_up(size):
    if size <= 0:
        return 0
    return (size + 2879) // 2880 * 2880


def write_header_diff(filename, diff, ext=0, in_place=True):
    """
    Apply a change set directly to a FITS file on disk.

    The HDU's header is read, the changes are applied atomically, and the
    new header is written back.  The data section is preserved verbatim.
    If no change can be applied the file is not modified and
    :class:`HeaderDiffError` is raised.
    """
    from .fitslib import read_header

    header = read_header(filename, ext=ext)
    new_header = diff.apply(header, in_place=False)
    write_header(filename, new_header, ext=ext)
    return new_header
