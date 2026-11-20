from __future__ import annotations


__all__ = [
    'RecipientConflict',
    'RecipientEvidence',
    'RecipientIndex',
    'RecipientIndexEntry',
    'RecipientSource',
    'buildRecipientIndex',
    'normalizeAddress',
]


import enum
import logging
import re

from dataclasses import dataclass, field
from email.parser import HeaderParser
from email.policy import compat32
from email.utils import getaddresses
from typing import (
        Dict, List, Mapping, Optional, Sequence, Tuple, TYPE_CHECKING, Union
    )

from .enums import RecipientType
from .utils import decodeRfc2047


if TYPE_CHECKING:
    from .msg_classes.message_base import MessageBase

logger = logging.getLogger(__name__)
logger.addHandler(logging.NullHandler())

# The transport header fields that describe recipients, in lookup order.
_HEADER_RECIPIENT_FIELDS = ('to', 'cc', 'bcc')
_INTERNAL_ID_PREFIX = 'id:'


class RecipientSource(enum.Enum):
    """
    Where a piece of recipient evidence came from.
    """

    #: Parsed from the original transport header stream
    #: (``PR_TRANSPORT_MESSAGE_HEADERS``).
    HEADER = 'header'
    #: Parsed from a header that the library generated from structured records.
    #: Evidence from this source is for display only and MUST NOT be treated as
    #: confirmation from an independent source.
    GENERATED_HEADER = 'generated_header'
    #: Read from a structured recipient substorage (the ``__recip`` folders).
    STRUCTURED = 'structured'


@dataclass(frozen = True)
class RecipientEvidence:
    """
    A single appearance of a recipient inside a message.

    The same SMTP address can have multiple pieces of evidence (for example a
    ``To`` and a ``Cc`` appearance in the header, plus structured occurrences).
    """

    source: RecipientSource
    #: The recipient role as it actually appeared: ``"to"``, ``"cc"`` or
    #: ``"bcc"``.
    role: str
    #: Where in the message the evidence was found (for example
    #: ``"header:To"`` or ``"recip:1"``).
    location: str
    #: The normalized SMTP address found in this appearance, if the appearance
    #: carries one.
    address: Optional[str] = None
    #: The display name found in this appearance, if any.
    displayName: Optional[str] = None
    #: The internal identifier of the appearance (entry ID, account, ...) when
    #: the appearance does not directly expose an SMTP address.
    internalId: Optional[str] = None

    @property
    def isConfirmation(self) -> bool:
        """
        Whether this evidence may be used to confirm an identity.

        Generated display headers are derived from the structured records, so
        they are not an independent source and never count as confirmation.
        """
        return self.source is not RecipientSource.GENERATED_HEADER


@dataclass(frozen = True)
class RecipientConflict:
    """
    An internal identifier that points at more than one SMTP address.

    Conflicting identifiers are never merged with an address through a shared
    display name; they are reported separately instead.
    """

    internalId: str
    displayName: Optional[str]
    #: Every normalized SMTP address the identifier was seen to point at.
    candidates: Tuple[str, ...]
    evidence: Tuple[RecipientEvidence, ...]


@dataclass
class RecipientIndexEntry:
    """
    One indexed recipient identity.

    An identity is either a confirmed SMTP address (possibly backed by several
    appearances) or an unconfirmed internal identifier that could not be tied
    to an address.
    """

    #: The index key: the normalized SMTP address for confirmed identities, or
    #: ``"id:<internal identifier>"`` for unconfirmed internal entries.
    key: str
    #: The confirmed SMTP address, or ``None`` for unconfirmed entries.
    smtpAddress: Optional[str]
    displayNames: List[str] = field(default_factory = list)
    internalIds: List[str] = field(default_factory = list)
    evidence: List[RecipientEvidence] = field(default_factory = list)

    @property
    def isConfirmed(self) -> bool:
        """
        Whether the entry resolves to a concrete SMTP address.
        """
        return self.smtpAddress is not None

    def roles(self) -> Dict[str, List[RecipientSource]]:
        """
        The roles the recipient actually appeared in, mapped to the sources of
        the evidence for each role.
        """
        result: Dict[str, List[RecipientSource]] = {}
        for item in self.evidence:
            sources = result.setdefault(item.role, [])
            if item.source not in sources:
                sources.append(item.source)
        return result

    def evidenceForRole(self, role: str) -> List[RecipientEvidence]:
        """
        All evidence recorded for the specified role.
        """
        return [item for item in self.evidence if item.role == role.lower()]


class RecipientIndex:
    """
    An index of the recipients found in a message (or a collection of
    messages).

    Confirmed appearances of the same SMTP address are collapsed into a single
    :class:`RecipientIndexEntry`, while every appearance (including its role
    and source) remains available through :attr:`RecipientIndexEntry.evidence`.
    Internal identifiers without confirmation stay as their own entries and are
    never merged through matching display names.
    """

    def __init__(self) -> None:
        self.__entries: Dict[str, RecipientIndexEntry] = {}
        self.__conflicts: List[RecipientConflict] = []

    def __iter__(self):
        return iter(self.__entries.values())

    def __len__(self) -> int:
        return len(self.__entries)

    def __contains__(self, key: str) -> bool:
        return key in self.__entries

    @property
    def entries(self) -> Mapping[str, RecipientIndexEntry]:
        """
        The indexed entries, keyed by normalized SMTP address or
        ``"id:<internal identifier>"``.
        """
        return self.__entries

    @property
    def conflicts(self) -> Tuple[RecipientConflict, ...]:
        """
        The internal identifiers that resolved to conflicting addresses.
        """
        return tuple(self.__conflicts)

    @property
    def addressEntries(self) -> Tuple[RecipientIndexEntry, ...]:
        """
        All entries that resolve to a confirmed SMTP address.
        """
        return tuple(entry for entry in self.__entries.values() if entry.isConfirmed)

    @property
    def unconfirmedEntries(self) -> Tuple[RecipientIndexEntry, ...]:
        """
        All entries that are only known by an internal identifier.
        """
        return tuple(entry for entry in self.__entries.values() if not entry.isConfirmed)

    def getEntry(self, key: str) -> Optional[RecipientIndexEntry]:
        """
        Gets the entry for an address (normalized) or an internal identifier.
        """
        if not key.startswith(_INTERNAL_ID_PREFIX):
            normalized = normalizeAddress(key)
            if normalized is not None:
                key = normalized
        return self.__entries.get(key)

    def rolesFor(self, address: str) -> Dict[str, List[RecipientSource]]:
        """
        The roles (and their sources) recorded for an SMTP address.
        """
        entry = self.getEntry(address)
        return entry.roles() if entry is not None else {}

    def evidenceFor(self, address: str) -> List[RecipientEvidence]:
        """
        Every appearance recorded for an SMTP address.
        """
        entry = self.getEntry(address)
        return list(entry.evidence) if entry is not None else []

    def entryForAddress(self, address: str) -> RecipientIndexEntry:
        entry = self.__entries.get(address)
        if entry is None:
            entry = RecipientIndexEntry(key = address, smtpAddress = address)
            self.__entries[address] = entry
        return entry

    def entryForInternalId(self, internalId: str) -> RecipientIndexEntry:
        key = _INTERNAL_ID_PREFIX + internalId
        entry = self.__entries.get(key)
        if entry is None:
            entry = RecipientIndexEntry(key = key, smtpAddress = None)
            self.__entries[key] = entry
        if internalId not in entry.internalIds:
            entry.internalIds.append(internalId)
        return entry

    def recordConflict(self, internalId: str, candidates: Tuple[str, ...],
                       evidence: Optional[RecipientEvidence] = None,
                       displayName: Optional[str] = None) -> None:
        """
        Records (or extends) the conflict for an internal identifier.
        """
        for position, conflict in enumerate(self.__conflicts):
            if conflict.internalId != internalId:
                continue
            mergedName = conflict.displayName or displayName
            mergedEvidence = conflict.evidence
            if evidence is not None:
                mergedEvidence += (evidence,)
            self.__conflicts[position] = RecipientConflict(
                internalId = internalId,
                displayName = mergedName,
                candidates = candidates,
                evidence = mergedEvidence,
            )
            return
        self.__conflicts.append(RecipientConflict(
            internalId = internalId,
            displayName = displayName,
            candidates = candidates,
            evidence = (evidence,) if evidence is not None else (),
        ))


def normalizeAddress(address: Optional[str]) -> Optional[str]:
    """
    Normalizes an SMTP address for indexing.

    Strips the null padding and surrounding whitespace that MSG streams use and
    lowercases the address. Returns ``None`` when the value does not look like
    an SMTP address.
    """
    if not address:
        return None
    cleaned = address.replace('\x00', '').strip()
    if not cleaned or any(char.isspace() for char in cleaned):
        return None
    if '<' in cleaned or '>' in cleaned:
        return None
    if '@' not in cleaned:
        return None
    return cleaned.lower()


def _safeDecodeRfc2047(value: str) -> str:
    """
    Decodes RFC 2047 words, keeping the raw value when a word is malformed.

    Some messages carry encoded words whose bytes do not match the declared
    charset; dropping the whole recipient field because of the display name
    would lose the address, so we degrade to the undecoded text instead.
    """
    try:
        return decodeRfc2047(value)
    except UnicodeDecodeError:
        logger.info('Failed to decode an RFC 2047 word in a header; using the raw value.')
        return value


def _parseHeaderAddresses(header, field: str) -> List[Tuple[Optional[str], Optional[str]]]:
    """
    Parses one recipient header field into ``(display name, address)`` pairs.
    """
    values = header.get_all(field, [])
    if not values:
        return []
    # Decode RFC 2047 words first, then split the combined field into
    # addresses. The library joins generated recipients with ";", while
    # ``getaddresses`` only splits on ",", so normalize the separator first.
    decoded = []
    for value in values:
        text = _safeDecodeRfc2047(value).replace('\x00', '').replace(';', ',')
        # Unfold continuation lines; some malformed headers then leave two
        # angle-addr groups without a comma between them.
        text = re.sub(r'\r?\n[ \t]+', ' ', text)
        text = re.sub(r'>[ \t]+(?=\S)', '>, ', text)
        decoded.append(text)
    parsed: List[Tuple[Optional[str], Optional[str]]] = []
    for displayName, rawAddress in getaddresses(decoded):
        # ``getaddresses`` returns ("", "") for empty/malformed segments.
        if not displayName and not rawAddress:
            continue
        address: Optional[str] = None if rawAddress in ('', 'None') else rawAddress
        parsed.append((displayName or None, address))
    return parsed


def _collectHeaderEvidence(msg: MessageBase) -> List[RecipientEvidence]:
    """
    Collects recipient evidence from the transport header.

    When the message has no original header stream the library generates a
    display header from the structured records; that evidence is flagged so it
    can never be treated as an independent confirmation.
    """
    headerText = msg.headerText
    source = RecipientSource.HEADER if headerText else RecipientSource.GENERATED_HEADER
    if headerText:
        if headerText.startswith('Microsoft Mail Internet Headers Version 2.0'):
            headerText = headerText[43:].lstrip()
        header = HeaderParser(policy = compat32).parsestr(headerText)
    else:
        # Accessing ``header`` builds the generated display header from the
        # structured recipient records when the raw stream is missing.
        header = msg.header

    evidence: List[RecipientEvidence] = []
    for fieldName in _HEADER_RECIPIENT_FIELDS:
        for displayName, address in _parseHeaderAddresses(header, fieldName):
            normalized = normalizeAddress(address)
            evidence.append(RecipientEvidence(
                source = source,
                role = fieldName,
                location = f'header:{fieldName.capitalize()}',
                address = normalized,
                displayName = displayName or None,
            ))
    return evidence


def _recipientInternalIds(recipient) -> List[str]:
    """
    Gathers the internal identifiers carried by a structured recipient.

    The account (``PR_ACCOUNT``) and entry ID (``PR_ENTRYID``) are the primary
    internal identifiers. The record key and search key are collected as
    fallbacks because different snapshots expose different identifiers.
    """
    identifiers: List[str] = []
    if recipient.account:
        account = recipient.account.replace('\x00', '').strip()
        if account:
            identifiers.append(account)
    for streamName, label in (
            ('__substg1.0_0FFF0102', 'entryid'),
            ('__substg1.0_0FF90102', 'recordkey'),
            ('__substg1.0_300B0102', 'searchkey')):
        try:
            raw = recipient.getStream(streamName)
        except Exception:
            logger.exception(f'Failed to read identifier stream "{streamName}".')
            raw = None
        if raw:
            identifiers.append(f'{label}:{raw.hex()}')
    return identifiers


def _collectStructuredEvidence(msg: MessageBase) -> List[Tuple[RecipientEvidence, List[str]]]:
    """
    Collects structured recipient records as ``(evidence, internal ids)``.
    """
    result: List[Tuple[RecipientEvidence, List[str]]] = []
    for index, recipient in enumerate(msg.recipients):
        if isinstance(recipient.type, RecipientType):
            role = recipient.type.name.lower()
        else:
            role = str(int(recipient.type))
        displayName = recipient.name.replace('\x00', '').strip() if recipient.name else None
        normalized = normalizeAddress(recipient.email)
        internalIds = _recipientInternalIds(recipient)
        evidence = RecipientEvidence(
            source = RecipientSource.STRUCTURED,
            role = role,
            location = f'recip:{index}',
            address = normalized,
            displayName = displayName,
            internalId = internalIds[0] if internalIds else None,
        )
        result.append((evidence, internalIds))
    return result


def _normalizeConfirmations(
        confirmations: Optional[Mapping[str, Union[str, Sequence[str]]]]
        ) -> Dict[str, Tuple[str, ...]]:
    """
    Normalizes the confirmation mapping into ``internal id -> addresses``.

    A value may be a single address or a sequence of addresses (when the same
    identifier points at different mailboxes across snapshots).
    """
    normalized: Dict[str, Tuple[str, ...]] = {}
    if not confirmations:
        return normalized
    for rawId, rawAddresses in confirmations.items():
        internalId = str(rawId).replace('\x00', '').strip()
        if not internalId:
            continue
        if isinstance(rawAddresses, (str, bytes)):
            addresses = [rawAddresses]
        else:
            addresses = list(rawAddresses)
        cleaned: List[str] = []
        for address in addresses:
            normalizedAddress = normalizeAddress(address)
            if normalizedAddress is not None and normalizedAddress not in cleaned:
                cleaned.append(normalizedAddress)
        if cleaned:
            normalized[internalId] = tuple(cleaned)
    return normalized


def _addEvidence(entry: RecipientIndexEntry, evidence: RecipientEvidence) -> None:
    entry.evidence.append(evidence)
    if evidence.displayName and evidence.displayName not in entry.displayNames:
        entry.displayNames.append(evidence.displayName)


def buildRecipientIndex(
        msg: MessageBase,
        confirmations: Optional[Mapping[str, Union[str, Sequence[str]]]] = None) -> RecipientIndex:
    """
    Builds a recipient index for a message using both available sources.

    The original transport header and the structured recipient records are
    indexed together. Appearances that share a confirmed SMTP address are
    collapsed into one entry while every appearance keeps its own role and
    source as evidence.

    :param confirmations: Optional mapping of internal identifier to SMTP
        address. A sequence of addresses means the identifier pointed at
        different mailboxes in different snapshots; such conflicts are reported
        separately and never merged. The mapping is the only source allowed to
        tie an internal identifier to an address - matching display names alone
        never causes a merge, and callers do not need access to a current
        address book.
    """
    index = RecipientIndex()
    confirmedAddresses = _normalizeConfirmations(confirmations)

    # Internal identifiers that resolve to more than one address stay detached
    # from every address entry and are reported as conflicts instead.
    conflictingIds = {
        internalId: addresses
        for internalId, addresses in confirmedAddresses.items()
        if len(addresses) > 1
    }
    unambiguousIds = {
        internalId: addresses[0]
        for internalId, addresses in confirmedAddresses.items()
        if len(addresses) == 1
    }

    # First pass: header evidence (original header or generated display header).
    for evidence in _collectHeaderEvidence(msg):
        if evidence.address is None:
            logger.info(f'Ignoring header appearance without a usable SMTP address: {evidence!r}.')
            continue
        _addEvidence(index.entryForAddress(evidence.address), evidence)

    # Second pass: structured recipient records.
    for evidence, internalIds in _collectStructuredEvidence(msg):
        targetEntry: Optional[RecipientIndexEntry] = None
        matchedId: Optional[str] = None
        conflictingId: Optional[str] = None

        # An explicit SMTP address on the record confirms its own identity.
        if evidence.address is not None:
            targetEntry = index.entryForAddress(evidence.address)

        # Otherwise the only way to attach the record is an unambiguous
        # confirmation of one of its internal identifiers.
        if targetEntry is None:
            for candidateId in internalIds:
                if candidateId in conflictingIds:
                    conflictingId = candidateId
                    break
                if candidateId in unambiguousIds:
                    targetEntry = index.entryForAddress(unambiguousIds[candidateId])
                    matchedId = candidateId
                    break

        if conflictingId is not None:
            # Keep the conflicting record as its own internal entry; the
            # candidate addresses are reported separately rather than merged.
            internalEntry = index.entryForInternalId(conflictingId)
            _addEvidence(internalEntry, evidence)
            index.recordConflict(conflictingId, conflictingIds[conflictingId],
                                 evidence, evidence.displayName)
            continue

        if targetEntry is None:
            # Unconfirmed internal identifier: retain the original record. It
            # must never be merged through a matching display name.
            internalId: Optional[str] = (
                evidence.internalId or (internalIds[0] if internalIds else None)
            )
            if internalId is None:
                logger.info(f'Ignoring structured record without address or identifier: {evidence!r}.')
                continue
            internalEntry = index.entryForInternalId(internalId)
            for extraId in internalIds:
                if extraId not in internalEntry.internalIds:
                    internalEntry.internalIds.append(extraId)
            _addEvidence(internalEntry, evidence)
            continue

        if matchedId is not None and matchedId not in targetEntry.internalIds:
            targetEntry.internalIds.append(matchedId)
        for internalId in internalIds:
            if internalId not in conflictingIds and internalId not in targetEntry.internalIds:
                targetEntry.internalIds.append(internalId)
        _addEvidence(targetEntry, evidence)

    # Surface conflicts for identifiers that never appeared as structured
    # records but were supplied through the confirmation mapping.
    seenConflicts = {item.internalId for item in index.conflicts}
    for internalId, addresses in conflictingIds.items():
        if internalId not in seenConflicts:
            index.recordConflict(internalId, addresses, None)

    return index
