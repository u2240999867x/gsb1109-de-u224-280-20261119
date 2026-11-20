__all__ = [
    'RecipientIndexTests',
]


import io
import struct
import unittest

from extract_msg import (
        OleWriter, RecipientSource, buildRecipientIndex, normalizeAddress,
        openMsg
    )
from extract_msg.constants.st import ST_PROP_BASE, ST_PROP_VAR

from .constants import TEST_FILE_DIR


def _propEntry(propType: int, propId: int, length: int, flags: int = 0) -> bytes:
    """
    Builds one 16-byte entry for a properties stream.
    """
    return ST_PROP_BASE.pack(propType, propId, flags) + ST_PROP_VAR.pack(length, 0)


def _fixedIntEntry(propId: int, value: int) -> bytes:
    """
    Builds a 16-byte entry for a 32-bit integer property.
    """
    return ST_PROP_BASE.pack(0x0003, propId, 0) + struct.pack('<II', value, 0)


def _utf16(text: str) -> bytes:
    return (text + '\x00').encode('utf-16-le')


def _buildMsg(recipients, headerText = None) -> bytes:
    """
    Builds the bytes of an MSG file with the specified structured recipients.

    Each recipient is a dict with the keys ``type`` (1 = To, 2 = Cc, 3 = Bcc),
    ``name``, optional ``email``, optional ``account`` and optional
    ``entryId``.
    """
    writer = OleWriter()

    topEntries = [
        # PidTagStoreSupportMask: the STORE_UNICODE_OK bit marks the strings
        # as Unicode.
        _fixedIntEntry(0x340D, 0x40000),
        _propEntry(0x001F, 0x001A, len(_utf16('IPM.Note'))),
        _propEntry(0x001F, 0x0037, len(_utf16('Recipient index test'))),
    ]
    writer.addEntry('__substg1.0_001A001F', _utf16('IPM.Note'))
    writer.addEntry('__substg1.0_0037001F', _utf16('Recipient index test'))

    if headerText is not None:
        headerBytes = headerText.encode('utf-16-le')
        topEntries.append(_propEntry(0x001F, 0x007D, len(headerBytes)))
        writer.addEntry('__substg1.0_007D001F', headerBytes)

    topProps = b'\x00' * 32 + b''.join(topEntries)
    writer.addEntry('__properties_version1.0', topProps)

    for index, recipient in enumerate(recipients):
        storage = f'__recip_version1.0_#000000{index:02X}'
        propEntries = [_fixedIntEntry(0x0C15, recipient['type'])]

        nameBytes = _utf16(recipient['name'])
        propEntries.append(_propEntry(0x001F, 0x3001, len(nameBytes)))
        writer.addEntry(f'{storage}/__substg1.0_3001001F', nameBytes)

        if recipient.get('email'):
            emailBytes = _utf16(recipient['email'])
            propEntries.append(_propEntry(0x001F, 0x3003, len(emailBytes)))
            writer.addEntry(f'{storage}/__substg1.0_3003001F', emailBytes)

        if recipient.get('account'):
            accountBytes = _utf16(recipient['account'])
            propEntries.append(_propEntry(0x001F, 0x3A00, len(accountBytes)))
            writer.addEntry(f'{storage}/__substg1.0_3A00001F', accountBytes)

        if recipient.get('entryId'):
            entryId = recipient['entryId']
            propEntries.append(_propEntry(0x0102, 0x0FFF, len(entryId)))
            writer.addEntry(f'{storage}/__substg1.0_0FFF0102', entryId)

        recipientProps = b'\x00' * 8 + b''.join(propEntries)
        writer.addEntry(f'{storage}/__properties_version1.0', recipientProps)

    stream = io.BytesIO()
    writer.write(stream)
    return stream.getvalue()


# The two internal-only recipients share this display name with the confirmed
# address; merging must never happen on the strength of the name alone.
_SHARED_NAME = 'Alice Smith'
_CONFIRMED_ADDRESS = 'alice@example.com'
_CONFIRMABLE_ID = 'GALID-CONFIRMABLE'
_CONFLICTING_ID = 'GALID-CONFLICTING'

_STRUCTURED_RECIPIENTS = [
    # Two structured appearances that carry the address themselves.
    {'type': 1, 'name': _SHARED_NAME, 'email': _CONFIRMED_ADDRESS,
     'entryId': b'\x01' * 16},
    {'type': 2, 'name': _SHARED_NAME, 'email': _CONFIRMED_ADDRESS,
     'entryId': b'\x02' * 16},
    # Internal-only appearance that the confirmation dictionary can resolve to
    # the same address.
    {'type': 1, 'name': _SHARED_NAME, 'account': _CONFIRMABLE_ID},
    # Internal-only appearance whose identifier points at two mailboxes in
    # different snapshots.
    {'type': 2, 'name': _SHARED_NAME, 'account': _CONFLICTING_ID},
]

_HEADER_TEXT = (
    'From: Bob Sender <bob@example.com>\r\n'
    f'To: {_SHARED_NAME} <{_CONFIRMED_ADDRESS}>\r\n'
    f'Cc: {_SHARED_NAME} <{_CONFIRMED_ADDRESS}>\r\n'
    'Subject: Recipient index test\r\n'
    '\r\n'
)

_CONFIRMATIONS = {
    _CONFIRMABLE_ID: _CONFIRMED_ADDRESS,
    _CONFLICTING_ID: ('bob@example.com', 'carol@example.com'),
}


class RecipientIndexTests(unittest.TestCase):
    def testNormalizeAddress(self):
        """
        Tests that MSG padding is stripped and addresses are normalized.
        """
        self.assertEqual(normalizeAddress('Alice@Example.com\x00\x00'),
                         'alice@example.com')
        self.assertIsNone(normalizeAddress(''))
        self.assertIsNone(normalizeAddress('not an address'))
        self.assertIsNone(normalizeAddress('Alice <alice@example.com>'))

    def testIndexWithConfirmationsMergesOneAddress(self):
        """
        Confirmed appearances of one address collapse into a single entry that
        keeps every role and source.
        """
        data = _buildMsg(_STRUCTURED_RECIPIENTS, _HEADER_TEXT)
        with openMsg(data) as msg:
            index = buildRecipientIndex(msg, _CONFIRMATIONS)

        # One confirmed address, one unconfirmed (conflicting) identifier.
        self.assertEqual(len(index.addressEntries), 1)
        self.assertEqual(len(index.unconfirmedEntries), 1)

        entry = index.getEntry(_CONFIRMED_ADDRESS)
        self.assertIsNotNone(entry)
        self.assertTrue(entry.isConfirmed)
        self.assertIn(_CONFIRMABLE_ID, entry.internalIds)
        self.assertNotIn(_CONFLICTING_ID, entry.internalIds)

        roles = entry.roles()
        self.assertEqual(set(roles), {'to', 'cc'})
        self.assertEqual(set(roles['to']),
                         {RecipientSource.HEADER, RecipientSource.STRUCTURED})
        self.assertEqual(set(roles['cc']),
                         {RecipientSource.HEADER, RecipientSource.STRUCTURED})

        # The header alone contributes both roles.
        headerEvidence = [item for item in entry.evidence
                          if item.source is RecipientSource.HEADER]
        self.assertEqual(sorted(item.role for item in headerEvidence),
                         ['cc', 'to'])
        # The confirmable internal record is attached as structured evidence.
        self.assertTrue(any(
            item.source is RecipientSource.STRUCTURED
            and item.internalId == _CONFIRMABLE_ID
            for item in entry.evidence
        ))

        # The conflicting identifier is reported separately and never merged,
        # despite sharing the display name.
        conflict = next(item for item in index.conflicts
                        if item.internalId == _CONFLICTING_ID)
        self.assertEqual(conflict.candidates,
                         ('bob@example.com', 'carol@example.com'))
        conflictEntry = index.getEntry(f'id:{_CONFLICTING_ID}')
        self.assertIsNotNone(conflictEntry)
        self.assertFalse(conflictEntry.isConfirmed)
        self.assertEqual(conflictEntry.displayNames, [_SHARED_NAME])

    def testRemovingConfirmationOnlyDropsThatRelation(self):
        """
        Without the determined relation the dependent record detaches, while
        the address keeps its own To and Cc appearances.
        """
        data = _buildMsg(_STRUCTURED_RECIPIENTS, _HEADER_TEXT)
        with openMsg(data) as msg:
            fullIndex = buildRecipientIndex(msg, _CONFIRMATIONS)
            # Rebuild with only the conflicting relation left in the dict.
            reducedConfirmations = {
                key: value for key, value in _CONFIRMATIONS.items()
                if key != _CONFIRMABLE_ID
            }
            reducedIndex = buildRecipientIndex(msg, reducedConfirmations)

        # The address still exists with both of its own roles.
        entry = reducedIndex.getEntry(_CONFIRMED_ADDRESS)
        self.assertIsNotNone(entry)
        self.assertEqual(set(entry.roles()), {'to', 'cc'})
        self.assertNotIn(_CONFIRMABLE_ID, entry.internalIds)
        self.assertFalse(any(
            item.internalId == _CONFIRMABLE_ID for item in entry.evidence
        ))

        # The formerly confirmed record is now its own unconfirmed entry.
        detached = reducedIndex.getEntry(f'id:{_CONFIRMABLE_ID}')
        self.assertIsNotNone(detached)
        self.assertFalse(detached.isConfirmed)
        self.assertEqual(detached.displayNames, [_SHARED_NAME])
        self.assertEqual([item.role for item in detached.evidence], ['to'])

        # Nothing else changed: the conflict and the address evidence count
        # (minus the detached record) line up with the full index.
        self.assertEqual(len(reducedIndex.conflicts), len(fullIndex.conflicts))
        fullEvidence = [item for item in fullIndex.getEntry(_CONFIRMED_ADDRESS).evidence
                        if item.internalId != _CONFIRMABLE_ID]
        self.assertEqual(entry.evidence, fullEvidence)

    def testGeneratedHeaderIsDisplayOnly(self):
        """
        A display header generated from structured records is not an
        independent confirmation source.
        """
        data = _buildMsg(_STRUCTURED_RECIPIENTS)
        with openMsg(data) as msg:
            self.assertIsNone(msg.headerText)
            index = buildRecipientIndex(msg, _CONFIRMATIONS)

        entry = index.getEntry(_CONFIRMED_ADDRESS)
        self.assertIsNotNone(entry)

        # No original-header evidence exists; every header appearance is
        # generated and flagged as non-confirming.
        self.assertFalse(any(
            item.source is RecipientSource.HEADER for item in entry.evidence
        ))
        generated = [item for item in entry.evidence
                     if item.source is RecipientSource.GENERATED_HEADER]
        self.assertTrue(generated)
        self.assertTrue(all(not item.isConfirmation for item in generated))

        # The internal record is still attached through the dictionary, not
        # through the generated header.
        self.assertIn(_CONFIRMABLE_ID, entry.internalIds)

        # The same merge rules still apply to the structured sources.
        roles = entry.roles()
        self.assertEqual(set(roles['to']),
                         {RecipientSource.GENERATED_HEADER,
                          RecipientSource.STRUCTURED})
        self.assertEqual(set(roles['cc']),
                         {RecipientSource.GENERATED_HEADER,
                          RecipientSource.STRUCTURED})

        # Without confirmations the internal-only records stay detached even
        # though the generated header shows the same display name.
        with openMsg(data) as msg:
            unconfirmedIndex = buildRecipientIndex(msg)
        self.assertEqual(len(unconfirmedIndex.addressEntries), 1)
        self.assertEqual(
            len(unconfirmedIndex.getEntry(f'id:{_CONFIRMABLE_ID}').evidence),
            1
        )

    def testNoMergeThroughDisplayNameOnly(self):
        """
        Matching display names never merge entries without a confirmed address.
        """
        data = _buildMsg(_STRUCTURED_RECIPIENTS, _HEADER_TEXT)
        with openMsg(data) as msg:
            index = buildRecipientIndex(msg)

        entry = index.getEntry(_CONFIRMED_ADDRESS)
        self.assertIsNotNone(entry)
        # Both internal-only records remain standalone entries.
        for internalId in (_CONFIRMABLE_ID, _CONFLICTING_ID):
            standalone = index.getEntry(f'id:{internalId}')
            self.assertIsNotNone(standalone)
            self.assertFalse(standalone.isConfirmed)
            self.assertNotIn(standalone, index.addressEntries)
        self.assertEqual(index.conflicts, ())

    def testMalformedEncodedWordKeepsAddresses(self):
        """
        A malformed RFC 2047 word in a display name does not drop the
        accompanying addresses from the index.
        """
        with openMsg(TEST_FILE_DIR / 'unicode-header.msg') as msg:
            index = buildRecipientIndex(msg)

        for address in ('alice@example.com', 'bob2@example.com',
                        'maxchen@example.com'):
            self.assertIsNotNone(index.getEntry(address))
            self.assertTrue(any(
                item.source is RecipientSource.HEADER
                and item.role == 'to'
                for item in index.evidenceFor(address)
            ))
        self.assertEqual(index.conflicts, ())
