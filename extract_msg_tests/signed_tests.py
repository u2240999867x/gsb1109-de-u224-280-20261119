__all__ = [
    'SignedTests',
]


import pathlib
import tempfile
import unittest

from .constants import TEST_FILE_DIR, USER_TEST_DIR
from extract_msg import openMsg, utils
from extract_msg.msg_classes import MessageSignedBase
from extract_msg.msg_path import MSGPath
from extract_msg.ole_writer import OleWriter


def _multipartContainer(body: bytes = b'Body text\r\n', pdf: bytes = b'CONTAINER-PDF',
                        signature: bytes = b'SIGNATURE-DATA', close: bool = True) -> bytes:
    """
    Builds a multipart/signed container with a plain body and a PDF attachment.
    """
    inner = (
        b'--INNER\r\nContent-Type: text/plain; charset="us-ascii"\r\n\r\n'
        + body +
        b'--INNER\r\nContent-Type: application/pdf\r\n'
        b'Content-Disposition: attachment; filename="note.pdf"\r\n\r\n'
        + pdf + b'\r\n--INNER--\r\n'
    )
    container = (
        b'Content-Type: multipart/signed; protocol="application/x-pkcs7-signature"; '
        b'boundary="CONT"\r\n\r\n'
        b'--CONT\r\nContent-Type: multipart/mixed; boundary="INNER"\r\n\r\n' + inner
    )
    if close:
        container += (
            b'\r\n--CONT\r\nContent-Type: application/pkcs7-signature; name="smime.p7s"\r\n'
            b'Content-Disposition: attachment; filename="smime.p7s"\r\n\r\n'
            + signature + b'\r\n--CONT--\r\n'
        )
    else:
        # Drop the signature and the closing boundary.
        container = container.rsplit(b'--INNER--\r\n', 1)[0]
    return container


class SignedTests(unittest.TestCase):
    def testUnwrap(self, testFileDir: pathlib.Path = TEST_FILE_DIR):
        """
        Tests the results of extract_msg.utils.unwrapMultipart().

        The initial test is ensuring the attachments generate at all. After
        that, test to make sure the types in the signedBody and signedHtmlBody
        are correct.
        """
        for path in TEST_FILE_DIR.glob('*.msg'):
            with openMsg(path, strict = False, delayAttachments = True) as msg:
                if isinstance(msg, MessageSignedBase):
                    with self.subTest(str(path)):
                        msg.attachments
                        if msg.signedBody:
                            self.assertIsInstance(msg.signedBody, str)
                        if msg.signedHtmlBody:
                            self.assertIsInstance(msg.signedHtmlBody, bytes)

    def testSignatureNotBusinessAttachment(self):
        """
        The detached signature part can be looked up but is not counted as a
        business attachment.
        """
        with openMsg(TEST_FILE_DIR / 'signed.msg', strict = False, delayAttachments = True) as msg:
            self.assertIsInstance(msg, MessageSignedBase)
            self.assertEqual(len(msg.signatureParts), 1)
            self.assertIs(msg.signatureParts[0], msg.attachments[-1])
            self.assertIs(msg.signatureParts[0].role, 'signature')
            self.assertTrue(msg.signatureParts[0].isSignature)
            self.assertNotIn(msg.signatureParts[0], msg.businessAttachments)

    def testUnwrapSeparatesSignature(self):
        """
        unwrapMultipart() keeps signature parts out of the business attachment
        list and reports a complete parse when the boundary is intact.
        """
        unwrapped = utils.unwrapMultipart(_multipartContainer())
        self.assertTrue(unwrapped['complete'])
        self.assertEqual(len(unwrapped['attachments']), 1)
        self.assertEqual(len(unwrapped['signature_parts']), 1)
        self.assertEqual(unwrapped['attachments'][0]['name'], 'note.pdf')
        self.assertEqual(unwrapped['attachments'][0]['layer'], 'signed')
        self.assertEqual(unwrapped['signature_parts'][0]['role'], 'signature')

    def testUnwrapTruncatedIsIncomplete(self):
        """
        A truncated signed container is reported as incomplete, but the parts
        that could be read are still returned.
        """
        unwrapped = utils.unwrapMultipart(_multipartContainer(close = False))
        self.assertFalse(unwrapped['complete'])
        self.assertEqual([att['name'] for att in unwrapped['attachments']], ['note.pdf'])
        self.assertIs(unwrapped['attachments'][0]['complete'], False)

    def testContainerOrderDoesNotChangeLayers(self):
        """
        Reordering the ordinary attachment and the signed container does not
        change the content or the layer of either business attachment.
        """
        ordinary = ('note.pdf', 'application/pdf', b'TOP-LEVEL-PDF')
        container = ('smime.p7m', 'multipart/signed', _multipartContainer())

        with _builtMessage([ordinary, container]) as first, \
                _builtMessage([container, ordinary]) as second:
            self.assertIsInstance(first, MessageSignedBase)
            self.assertIsInstance(second, MessageSignedBase)

            def business(msg):
                return sorted((att.layer, att.name.rstrip('\x00'), att.data) for att in msg.businessAttachments)

            self.assertEqual(business(first), business(second))
            self.assertTrue(first.signedMessageComplete)
            self.assertTrue(second.signedMessageComplete)

    def testTruncatedContainerIncomplete(self):
        """
        A truncated signed container still allows the ordinary note.pdf to be
        archived, and the container itself is marked as not fully parsed.
        """
        ordinary = ('note.pdf', 'application/pdf', b'TOP-LEVEL-PDF')
        container = ('smime.p7m', 'multipart/signed', _multipartContainer(close = False))

        _tempDir = tempfile.mkdtemp()
        with _builtMessage([ordinary, container]) as msg:
            self.assertFalse(msg.signedContainerComplete)
            self.assertFalse(msg.signedMessageComplete)

            # The ordinary attachment is still readable and can be archived.
            ordinaryAtts = msg.ordinaryAttachments
            self.assertEqual(len(ordinaryAtts), 1)
            self.assertEqual(ordinaryAtts[0].data, b'TOP-LEVEL-PDF')
            result = ordinaryAtts[0].save(customPath = _tempDir, customFilename = 'ordinary.pdf')
            self.assertTrue(pathlib.Path(result[1]).is_file())

            # The container is preserved and marked as incomplete.
            self.assertIsNotNone(msg.signedContainer)
            self.assertFalse(msg.signedContainer.complete)

            # The readable part of the container is available, but is not
            # presented as a complete signed message.
            containerAtts = msg.containerAttachments
            self.assertTrue(containerAtts)
            self.assertTrue(all(not att.complete for att in containerAtts))

    def testAmbiguousContainersPreserved(self):
        """
        When two attachments can each be explained as the signed container and
        the original is not enough to decide, neither is chosen. Both are kept
        as ambiguous material.
        """
        first = ('first.p7m', 'application/octet-stream', _multipartContainer(pdf = b'FIRST'))
        second = ('second.p7m', 'application/octet-stream', _multipartContainer(pdf = b'SECOND'))

        with _builtMessage([first, second]) as msg:
            self.assertIsNone(msg.signedContainer)
            self.assertFalse(msg.signedMessageComplete)
            self.assertEqual(len(msg.signedAmbiguous), 2)
            self.assertEqual([att.name.rstrip('\x00') for att in msg.signedAmbiguous], ['first.p7m', 'second.p7m'])
            # Both candidates are preserved as ambiguous material.
            self.assertTrue(all(att.ambiguous for att in msg.signedAmbiguous))
            # Nothing is selected as a successful result.
            self.assertEqual(msg.businessAttachments, [])

    @unittest.skipIf(USER_TEST_DIR is None, 'User test files not defined.')
    def testUserStdin(self):
        self.testUnwrap(USER_TEST_DIR)


class _builtMessage:
    """
    Context manager that builds a MessageSigned MSG file with the provided
    attachments. Each attachment is a ``(name, mimetype, data)`` tuple.
    """

    def __init__(self, entries):
        self.entries = entries

    def __enter__(self):
        with openMsg(TEST_FILE_DIR / 'signed.msg', strict = False, delayAttachments = True) as original:
            writer = OleWriter()
            writer.fromMsg(original)
            template = MSGPath('__attach_version1.0_#00000000')
            templateData = {
                str(path.relative_to(template)): original.getStream(path)
                for path in original.listDir(True, False, False)
                if path.is_relative_to(template)
            }

            # Remove the template attachment before adding the new ones.
            writer.deleteEntry(template)

        for index, (name, mimetype, data) in enumerate(self.entries):
            dirName = MSGPath(f'__attach_version1.0_#{index:08X}')
            writer.addEntry(dirName, storage = True)
            for relative, templateValue in templateData.items():
                writer.addEntry(dirName / relative, templateValue)
            writer.editEntry(dirName / '__substg1.0_37010102', data = data)
            writer.editEntry(dirName / '__substg1.0_3707001F', data = name.encode('utf-16-le') + b'\x00\x00')
            writer.editEntry(dirName / '__substg1.0_3704001F', data = name[:8].encode('utf-16-le') + b'\x00\x00')
            writer.editEntry(dirName / '__substg1.0_370E001F', data = mimetype.encode('utf-16-le') + b'\x00\x00')

        handle = tempfile.NamedTemporaryFile(suffix = '.msg', delete = False)
        handle.close()
        self.path = pathlib.Path(handle.name)
        writer.write(self.path)
        return openMsg(self.path, strict = False, delayAttachments = True)

    def __exit__(self, excType, excValue, traceback):
        self.path.unlink()
        return False
