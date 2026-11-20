from __future__ import annotations


__all__ = [
    'StringFallbackTests',
]


import io
import struct
import unittest

import extract_msg

from extract_msg.ole_writer import OleWriter
from extract_msg.structures.string_stream import StringStreamKind, StringStreamResult


def _fixedProp(pid: int, type_: int, value: int) -> bytes:
    return struct.pack('<HHI', type_, pid, 0) + struct.pack('<II', value, 0)


def _messageProperties(unicode_: bool, codepage: int = None, embedded: bool = False) -> bytes:
    """
    Builds the bytes of a ``__properties_version1.0`` stream for a synthetic
    message, including the string flag and code page properties.
    """
    ret = b'\x00' * 8 + struct.pack('<IIII', 0, 0, 0, 0)
    if not embedded:
        ret += b'\x00' * 8
    flags = 0x00040000 if unicode_ else 0
    ret += _fixedProp(0x340D, 0x0003, flags)
    if codepage is not None:
        ret += _fixedProp(0x3FFD, 0x0003, codepage)
    return ret


def _attachmentProperties() -> bytes:
    return b'\x00' * 8 + _fixedProp(0x3705, 0x0003, 5)


def _addNamedPropertyStorage(writer: OleWriter) -> None:
    """
    Adds the empty named property storage that the parser expects to exist.
    """
    storage = '__nameid_version1.0'
    writer.addEntry(storage, storage = True)
    for stream in ('__substg1.0_00020102', '__substg1.0_00030102', '__substg1.0_00040102'):
        writer.addEntry(storage + '/' + stream, b'')


def _corruptUnicode(text: str = 'seed') -> bytes:
    """
    Produces UTF-16-LE bytes that cannot be strictly decoded.
    """
    return text.encode('utf-16-le') + b'\x00\xd8'


def _buildUnicodeMessage() -> bytes:
    """
    Builds a Unicode-flagged message with two embedded messages.

    The outer message uses code page 1252, the first embedded message its own
    code page 1251, and the second embedded message an unrecognized code page.
    """
    writer = OleWriter()
    _addNamedPropertyStorage(writer)
    writer.addEntry('__properties_version1.0', _messageProperties(True, 1252))
    writer.addEntry('__substg1.0_001A001F', 'IPM.Note'.encode('utf-16-le'))

    # Body: corrupted Unicode bytes, valid legacy code page bytes.
    writer.addEntry('__substg1.0_1000001F', _corruptUnicode('Olvid'))
    writer.addEntry('__substg1.0_1000001E', 'Olvid\xf3'.encode('windows-1252'))
    # Subject: two valid, intentionally different representations.
    writer.addEntry('__substg1.0_0037001F', 'UniSubject'.encode('utf-16-le'))
    writer.addEntry('__substg1.0_0037001E', b'LegacySubject')
    # Comment: a genuinely empty Unicode stream plus a non-empty legacy one.
    writer.addEntry('__substg1.0_3004001F', b'')
    writer.addEntry('__substg1.0_3004001E', b'LegacyComment')
    # Conversation topic: only the legacy representation exists.
    writer.addEntry('__substg1.0_0070001E', b'LegacyOnly')

    attachmentA = '__attach_version1.0_#00000000'
    embeddedA = attachmentA + '/__substg1.0_3701000D'
    writer.addEntry(attachmentA, storage = True)
    writer.addEntry(attachmentA + '/__properties_version1.0', _attachmentProperties())
    writer.addEntry(embeddedA, storage = True)
    writer.addEntry(embeddedA + '/__properties_version1.0', _messageProperties(True, 1251, True))
    writer.addEntry(embeddedA + '/__substg1.0_001A001F', 'IPM.Note'.encode('utf-16-le'))
    # Embedded subject: Unicode broken, legacy valid under its own cp1251.
    writer.addEntry(embeddedA + '/__substg1.0_0037001F', _corruptUnicode())
    writer.addEntry(embeddedA + '/__substg1.0_0037001E', 'Тема'.encode('windows-1251'))
    # Embedded comment: Unicode broken and no legacy stream at all.
    writer.addEntry(embeddedA + '/__substg1.0_3004001F', _corruptUnicode('x'))
    # Embedded keywords: a genuinely empty Unicode stream, and nothing else.
    writer.addEntry(embeddedA + '/__substg1.0_1085001F', b'')

    attachmentB = '__attach_version1.0_#00000001'
    embeddedB = attachmentB + '/__substg1.0_3701000D'
    writer.addEntry(attachmentB, storage = True)
    writer.addEntry(attachmentB + '/__properties_version1.0', _attachmentProperties())
    writer.addEntry(embeddedB, storage = True)
    # This embedded message names a code page with no supported encoding.
    writer.addEntry(embeddedB + '/__properties_version1.0', _messageProperties(True, 99999, True))
    writer.addEntry(embeddedB + '/__substg1.0_001A001F', 'IPM.Note'.encode('utf-16-le'))
    # Subject: Unicode broken, legacy bytes present but no code page basis.
    writer.addEntry(embeddedB + '/__substg1.0_0037001F', _corruptUnicode())
    writer.addEntry(embeddedB + '/__substg1.0_0037001E', b'legacy bytes')

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def _buildAnsiMessage() -> bytes:
    """
    Builds a legacy (non-Unicode-flagged) message using code page 932.
    """
    writer = OleWriter()
    _addNamedPropertyStorage(writer)
    writer.addEntry('__properties_version1.0', _messageProperties(False, 932))
    writer.addEntry('__substg1.0_001A001E', b'IPM.Note')

    # Subject: the preferred legacy representation is valid.
    writer.addEntry('__substg1.0_0037001E', '件名'.encode('shift_jis'))
    writer.addEntry('__substg1.0_0037001F', 'UnicodeSubject'.encode('utf-16-le'))
    # Body: the legacy bytes cannot be decoded, but the Unicode copy is valid.
    writer.addEntry('__substg1.0_1000001E', b'\xff\xfe')
    writer.addEntry('__substg1.0_1000001F', 'UnicodeBody'.encode('utf-16-le'))
    # Comment: only the legacy representation exists.
    writer.addEntry('__substg1.0_3004001E', '本文'.encode('shift_jis'))

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


class StringFallbackTests(unittest.TestCase):
    def testOuterMessageFallback(self):
        """
        Reads the outer body, subject, and comment in sequence and verifies
        the stream, encoding, fallback state, and value of each.
        """
        data = _buildUnicodeMessage()
        with extract_msg.openMsg(io.BytesIO(data), stringFallback = True) as msg:
            self.assertTrue(msg.stringFallback)

            body = msg.getStringStreamWithFallback('__substg1.0_1000')
            self.assertEqual(body.value, 'Olvid\xf3')
            self.assertIs(body.kind, StringStreamKind.ANSI)
            self.assertEqual(body.encoding, 'windows-1252')
            self.assertTrue(body.usedFallback)
            self.assertEqual(body.streamPath, '__substg1.0_1000001E')
            self.assertFalse(body.missing)
            self.assertFalse(body.empty)
            self.assertFalse(body.undecodable)
            self.assertIsNone(body.reason)

            subject = msg.getStringStreamWithFallback('__substg1.0_0037')
            self.assertEqual(subject.value, 'UniSubject')
            self.assertIs(subject.kind, StringStreamKind.UNICODE)
            self.assertEqual(subject.encoding, 'utf-16-le')
            self.assertEqual(subject.streamPath, '__substg1.0_0037001F')
            self.assertFalse(subject.usedFallback)

            comment = msg.getStringStreamWithFallback('__substg1.0_3004')
            # The empty Unicode stream is an original empty value, not a
            # substitution from the legacy representation.
            self.assertEqual(comment.value, '')
            self.assertTrue(comment.empty)
            self.assertIs(comment.kind, StringStreamKind.UNICODE)
            self.assertFalse(comment.usedFallback)

            legacyOnly = msg.getStringStreamWithFallback('__substg1.0_0070')
            self.assertEqual(legacyOnly.value, 'LegacyOnly')
            self.assertIs(legacyOnly.kind, StringStreamKind.ANSI)
            self.assertTrue(legacyOnly.usedFallback)

    def testMissingStreamIsReportedSeparatelyFromEmpty(self):
        """
        A field with no streams is distinct from one with an empty stream.
        """
        data = _buildUnicodeMessage()
        with extract_msg.openMsg(io.BytesIO(data), stringFallback = True) as msg:
            missing = msg.getStringStreamWithFallback('__substg1.0_9999')
            self.assertIsNone(missing.value)
            self.assertTrue(missing.missing)
            self.assertFalse(missing.empty)
            self.assertIs(missing.kind, StringStreamKind.MISSING)
            self.assertIsNone(missing.streamPath)
            self.assertIn('001F', missing.reason)
            self.assertIn('001E', missing.reason)

            empty = msg.getStringStreamWithFallback('__substg1.0_3004')
            self.assertEqual(empty.value, '')
            self.assertTrue(empty.empty)
            self.assertFalse(empty.missing)

    def testEmbeddedMessageUsesOwnEncodingBasis(self):
        """
        The inner message decodes its legacy field with its own code page,
        independent of the outer message's code page.
        """
        data = _buildUnicodeMessage()
        with extract_msg.openMsg(io.BytesIO(data), stringFallback = True) as msg:
            self.assertEqual(len(msg.attachments), 2)
            embedded = msg.attachments[0].data

            subject = embedded.getStringStreamWithFallback('__substg1.0_0037')
            self.assertEqual(subject.value, 'Тема')
            self.assertIs(subject.kind, StringStreamKind.ANSI)
            self.assertEqual(subject.encoding, 'windows-1251')
            self.assertNotEqual(subject.encoding, 'windows-1252')
            self.assertTrue(subject.usedFallback)
            self.assertTrue(subject.streamPath.endswith('/__substg1.0_0037001E'))

            keywords = embedded.getStringStreamWithFallback('__substg1.0_1085')
            self.assertEqual(keywords.value, '')
            self.assertTrue(keywords.empty)
            self.assertIs(keywords.kind, StringStreamKind.UNICODE)
            self.assertFalse(keywords.usedFallback)

    def testUndecodableFieldReportsReasonWithoutReplacement(self):
        """
        When every present representation fails, the raw reason is returned and
        no replacement text is treated as a valid value.
        """
        data = _buildUnicodeMessage()
        with extract_msg.openMsg(io.BytesIO(data), stringFallback = True) as msg:
            embeddedA = msg.attachments[0].data
            broken = embeddedA.getStringStreamWithFallback('__substg1.0_3004')
            self.assertIsNone(broken.value)
            self.assertTrue(broken.undecodable)
            self.assertIs(broken.kind, StringStreamKind.UNDECODABLE)
            self.assertIsNone(broken.streamPath)
            self.assertIn('utf-16-le', broken.reason)

            # The legacy bytes exist but the code page cannot be resolved.
            embeddedB = msg.attachments[1].data
            noBasis = embeddedB.getStringStreamWithFallback('__substg1.0_0037')
            self.assertIsNone(noBasis.value)
            self.assertTrue(noBasis.undecodable)
            self.assertIn('utf-16-le', noBasis.reason)
            self.assertIn('code page', noBasis.reason)

    def testResultsAreStableAcrossReadOrders(self):
        """
        Two independently opened objects produce identical results regardless
        of the order in which the outer and inner fields are read.
        """
        data = _buildUnicodeMessage()
        outerFields = (
            '__substg1.0_1000', '__substg1.0_0037',
            '__substg1.0_3004', '__substg1.0_0070',
            '__substg1.0_9999',
        )
        innerFields = (
            '__substg1.0_0037', '__substg1.0_3004', '__substg1.0_1085',
        )

        def snapshot(message):
            results = {field: message.getStringStreamWithFallback(field) for field in outerFields}
            for attachment in message.attachments:
                for field in innerFields:
                    results[(str(attachment.dir), field)] = attachment.data.getStringStreamWithFallback(field)
            return results

        with extract_msg.openMsg(io.BytesIO(data), stringFallback = True) as first:
            firstSnapshot = snapshot(first)
        with extract_msg.openMsg(io.BytesIO(data), stringFallback = True) as second:
            secondSnapshot = {}
            for field in reversed(outerFields):
                secondSnapshot[field] = second.getStringStreamWithFallback(field)
            for attachment in reversed(second.attachments):
                for field in reversed(innerFields):
                    secondSnapshot[(str(attachment.dir), field)] = attachment.data.getStringStreamWithFallback(field)

        self.assertEqual(firstSnapshot, secondSnapshot)

    def testLegacyFlaggedMessageFallsBackToUnicode(self):
        """
        In a legacy-flagged message the code page representation is preferred,
        and a corrupt one falls back to the valid Unicode representation.
        """
        data = _buildAnsiMessage()
        with extract_msg.openMsg(io.BytesIO(data), stringFallback = True) as msg:
            subject = msg.getStringStreamWithFallback('__substg1.0_0037')
            self.assertEqual(subject.value, '件名')
            self.assertIs(subject.kind, StringStreamKind.ANSI)
            self.assertEqual(subject.encoding, 'shift_jis')
            self.assertFalse(subject.usedFallback)

            body = msg.getStringStreamWithFallback('__substg1.0_1000')
            self.assertEqual(body.value, 'UnicodeBody')
            self.assertIs(body.kind, StringStreamKind.UNICODE)
            self.assertEqual(body.encoding, 'utf-16-le')
            self.assertTrue(body.usedFallback)

            comment = msg.getStringStreamWithFallback('__substg1.0_3004')
            self.assertEqual(comment.value, '本文')
            self.assertIs(comment.kind, StringStreamKind.ANSI)
            self.assertFalse(comment.usedFallback)

    def testExistingReadingBehaviorIsUnchanged(self):
        """
        Without selecting the fallback strategy, the original reading methods
        keep their previous behavior, including strict decode failures.
        """
        data = _buildUnicodeMessage()
        with extract_msg.openMsg(io.BytesIO(data)) as msg:
            self.assertFalse(msg.stringFallback)
            self.assertEqual(msg.getStringStream('__substg1.0_0037'), 'UniSubject')
            with self.assertRaises(UnicodeDecodeError):
                msg.getStringStream('__substg1.0_1000')

            # The detailed reader remains usable even when the option is off.
            self.assertEqual(
                msg.getStringStreamWithFallback('__substg1.0_0037').value,
                'UniSubject',
            )

    def testResultEqualityAndRepresentation(self):
        """
        Results compare by content and expose a readable representation.
        """
        first = StringStreamResult(
            'stream', 'value', StringStreamKind.ANSI, 'windows-1252', True,
        )
        second = StringStreamResult(
            'stream', 'value', StringStreamKind.ANSI, 'windows-1252', True,
        )
        self.assertEqual(first, second)
        self.assertEqual(hash(first), hash(second))
        self.assertIn('StringStreamResult', repr(first))
        self.assertIn('windows-1252', repr(first))


if __name__ == '__main__':
    unittest.main()
