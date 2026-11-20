__all__ = [
    'EmlConversionTests',
]


import tempfile
import unittest

from email.parser import BytesParser
from email.policy import default as default_policy
from pathlib import Path
from typing import List

from .constants import TEST_FILE_DIR
from .eml_fixture_builder import (
        INNER_IMAGE, INNER_REPORT, OUTER_IMAGE, OUTER_REPORT, SCAN_BYTES,
        SHARED_CID, buildFixtures
    )
from extract_msg import openMsg


def _iterParts(part, includeRfc822: bool = False):
    """
    Yields every MIME part below *part*, optionally stopping at the boundary of
    a message/rfc822 attachment so the outer and forwarded messages stay scoped.
    """
    for child in part.iter_parts():
        yield child
        if child.get_content_type() == 'message/rfc822' and not includeRfc822:
            continue
        if child.is_multipart():
            yield from _iterParts(child, includeRfc822)


def _partsByFilename(part, filename: str) -> List:
    return [item for item in _iterParts(part)
            if item.get_filename() == filename]


def _partsByType(part, contentType: str) -> List:
    return [item for item in _iterParts(part)
            if item.get_content_type() == contentType]


def _decoded(part) -> bytes:
    """
    Returns the transfer-decoded payload bytes regardless of declared charset,
    which is what an extraction tool would write to disk.
    """
    return part.get_payload(decode = True)


class EmlConversionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tempDir = tempfile.TemporaryDirectory()
        cls.fixturePaths = buildFixtures(Path(cls._tempDir.name))

    @classmethod
    def tearDownClass(cls):
        cls._tempDir.cleanup()

    def _writeEml(self, fixtureKey: str) -> Path:
        """
        Converts the MSG fixture to an EML file on disk and parses it back with
        a separate, standard MIME reader.
        """
        emlPath = Path(self._tempDir.name) / f'{fixtureKey}.eml'
        with openMsg(self.fixturePaths[fixtureKey]) as msg:
            emlPath.write_bytes(msg.asEmailMessage().as_bytes())
        with emlPath.open('rb') as f:
            return BytesParser(policy = default_policy).parse(f)

    def _outerAndForwarded(self, root):
        forwardedNodes = _partsByType(root, 'message/rfc822')
        self.assertEqual(len(forwardedNodes), 1)
        forwardedNode = forwardedNodes[0]
        forwarded = forwardedNode.get_content()
        return forwardedNode, forwarded

    def testOuterBodies(self):
        """
        The outer message keeps its own plain text and HTML bodies.
        """
        root = self._writeEml('complete')
        plain = [part for part in _iterParts(root)
                 if part.get_content_type() == 'text/plain'
                 and part.get_content_disposition() is None]
        html = [part for part in _iterParts(root)
                if part.get_content_type() == 'text/html']
        self.assertEqual(len(plain), 1)
        self.assertEqual(len(html), 1)
        self.assertIn('outer plain text body', plain[0].get_content())
        self.assertIn('outer html body', html[0].get_content())
        self.assertIn(f'cid:{SHARED_CID}', html[0].get_content())

    def testForwardedPartIsIndependentEmlAttachment(self):
        """
        The embedded MSG becomes a message/rfc822 attachment marked as a
        forward that a MIME reader can open as a standalone email.
        """
        root = self._writeEml('complete')
        forwardedNode, forwarded = self._outerAndForwarded(root)

        self.assertEqual(forwardedNode.get_content_disposition(), 'attachment')
        self.assertEqual(forwardedNode.get_filename(), 'Forwarded original.eml')

        self.assertEqual(forwarded['Subject'], 'Forwarded original subject')
        plain = [part for part in forwarded.walk()
                 if part.get_content_type() == 'text/plain'
                 and part.get_content_disposition() is None]
        html = [part for part in forwarded.walk()
                if part.get_content_type() == 'text/html']
        self.assertEqual(len(plain), 1)
        self.assertEqual(len(html), 1)
        self.assertIn('inner plain text body', plain[0].get_content())
        self.assertIn('inner html body', html[0].get_content())

        # The forwarded part must also survive being written and reopened on
        # its own, outside the outer message.
        standalonePath = Path(self._tempDir.name) / 'standalone-forward.eml'
        standalonePath.write_bytes(forwarded.as_bytes())
        with standalonePath.open('rb') as f:
            reopened = BytesParser(policy = default_policy).parse(f)
        self.assertEqual(reopened['Subject'], 'Forwarded original subject')
        self.assertTrue(any(part.get_content_type() == 'image/png'
                            for part in reopened.walk()))

    def testInlineImagesKeepSeparateCidBoundaries(self):
        """
        The same Content-ID resolves to different image bytes in each message.
        """
        root = self._writeEml('complete')
        _, forwarded = self._outerAndForwarded(root)

        outerImages = [part for part in _iterParts(root)
                       if part.get_content_type() == 'image/png']
        innerImages = [part for part in forwarded.walk()
                       if part.get_content_type() == 'image/png']
        self.assertEqual(len(outerImages), 1)
        self.assertEqual(len(innerImages), 1)

        outerImage, innerImage = outerImages[0], innerImages[0]
        self.assertEqual(outerImage.get('Content-ID'), f'<{SHARED_CID}>')
        self.assertEqual(innerImage.get('Content-ID'), f'<{SHARED_CID}>')
        self.assertEqual(_decoded(outerImage), OUTER_IMAGE)
        self.assertEqual(_decoded(innerImage), INNER_IMAGE)
        self.assertNotEqual(_decoded(outerImage), _decoded(innerImage))

    def testRecipientsStayWithinTheirMessage(self):
        """
        Recipients of the embedded message do not leak into the outer headers.
        """
        root = self._writeEml('complete')
        _, forwarded = self._outerAndForwarded(root)

        outerTo = root['To']
        self.assertIn('outer@example.com', outerTo)
        self.assertNotIn('inner@example.com', outerTo or '')
        self.assertNotIn('inner-copy@example.com', root['Cc'] or '')

        self.assertIn('inner@example.com', forwarded['To'])
        self.assertIn('inner-copy@example.com', forwarded['Cc'])
        self.assertNotIn('outer@example.com', forwarded['To'] or '')

    def testRegularAttachmentsStayScopedAndByteExact(self):
        """
        A regular attachment name shared by both layers yields two extractable
        parts whose bytes are preserved without re-encoding damage.
        """
        root = self._writeEml('complete')
        _, forwarded = self._outerAndForwarded(root)

        outerReports = _partsByFilename(root, 'report.txt')
        innerReports = _partsByFilename(forwarded, 'report.txt')
        self.assertEqual(len(outerReports), 1)
        self.assertEqual(len(innerReports), 1)
        self.assertEqual(_decoded(outerReports[0]), OUTER_REPORT)
        self.assertEqual(_decoded(innerReports[0]), INNER_REPORT)

        outerScans = _partsByFilename(root, 'scan.pdf')
        self.assertEqual(len(outerScans), 1)
        self.assertEqual(_decoded(outerScans[0]), SCAN_BYTES)
        self.assertEqual(_partsByFilename(forwarded, 'scan.pdf'), [])

    def testMissingOuterImageLeavesCidUnresolved(self):
        """
        When the outer message lacks its inline image, the cid reference stays
        unresolved and the embedded message's same-cid image is not borrowed.
        """
        root = self._writeEml('missingImage')
        _, forwarded = self._outerAndForwarded(root)

        outerImages = _partsByType(root, 'image/png')
        self.assertEqual(outerImages, [])
        outerHtml = [part for part in _iterParts(root)
                     if part.get_content_type() == 'text/html'][0]
        self.assertIn(f'cid:{SHARED_CID}', outerHtml.get_content())

        innerImages = [part for part in forwarded.walk()
                       if part.get_content_type() == 'image/png']
        self.assertEqual(len(innerImages), 1)
        self.assertEqual(innerImages[0].get('Content-ID'), f'<{SHARED_CID}>')
        self.assertEqual(_decoded(innerImages[0]), INNER_IMAGE)

        # The outer message still carries its ordinary attachments.
        self.assertEqual(len(_partsByFilename(root, 'scan.pdf')), 1)
        self.assertEqual(len(_partsByFilename(root, 'report.txt')), 1)

    def testExistingMessageWithoutEmbedsStillConverts(self):
        """
        Conversion of a pre-existing MSG without embedded messages keeps
        working and preserves its binary attachments byte for byte.
        """
        emlPath = Path(self._tempDir.name) / 'unicode.eml'
        with openMsg(TEST_FILE_DIR / 'unicode.msg') as msg:
            expected = {att.getFilename(): att.data for att in msg.attachments}
            emlPath.write_bytes(msg.asEmailMessage().as_bytes())

        with emlPath.open('rb') as f:
            root = BytesParser(policy = default_policy).parse(f)

        self.assertEqual(root.get_content_type(), 'multipart/mixed')
        self.assertEqual(_partsByType(root, 'message/rfc822'), [])
        for filename, data in expected.items():
            parts = _partsByFilename(root, filename)
            self.assertEqual(len(parts), 1)
            self.assertEqual(_decoded(parts[0]), data)

    def testHeaderFieldsArePreserved(self):
        """
        Transport headers from the original MSG survive the conversion.
        """
        emlPath = Path(self._tempDir.name) / 'multi-to.eml'
        with openMsg(TEST_FILE_DIR / 'multi-to.msg') as msg:
            emlPath.write_bytes(msg.asEmailMessage().as_bytes())

        with emlPath.open('rb') as f:
            root = BytesParser(policy = default_policy).parse(f)

        self.assertEqual(sum(1 for key in root.keys() if key.lower() == 'to'), 1)
        self.assertIn('alice@example.com', root['To'])
        self.assertIn('carol@example.com', root['To'])
        self.assertIn('dave@example.com', root['Cc'])
        self.assertIn('bob@example.com', root['From'])
        self.assertTrue(root.get('Received'))

    def testSyntheticFixtureHeadersArePreserved(self):
        """
        Both layers carry their own transport headers, including the message
        IDs and any custom archive header, through the conversion.
        """
        root = self._writeEml('complete')
        _, forwarded = self._outerAndForwarded(root)

        self.assertEqual(root['Message-ID'], '<outer-msg-id@archive.test>')
        self.assertEqual(root['X-Archive-Batch'], 'batch-u224')
        self.assertEqual(root['From'], 'Outer Sender <outer-sender@example.com>')

        self.assertEqual(forwarded['Message-ID'], '<inner-msg-id@archive.test>')
        self.assertEqual(forwarded['From'],
                         'Inner Sender <inner-sender@example.com>')
