__all__ = [
    'AttachmentManifestTests',
]


import json
import pathlib
import struct
import tempfile
import unittest
import zipfile

import extract_msg

from extract_msg.attachments import AttachmentManifest


def _prop(pid: int, typ: int, value: bytes, flags: int = 0x6) -> bytes:
    return struct.pack('<HHI', typ, pid, flags) + value


def _varprop(pid: int, typ: int, data: bytes, flags: int = 0x6) -> bytes:
    return _prop(pid, typ, struct.pack('<II', len(data), 0), flags)


def _createThreeOccurrenceMsg(path: pathlib.Path) -> None:
    """
    Creates an outer message and two embedded forwards.

    The outer message has ``invoice.pdf``. The first forward has an identical
    display name; the second has ``invoice?.pdf``, which sanitizes to the same
    file name. Both forwards have identical bytes, so only occurrence identity
    distinguishes them.
    """
    writer = extract_msg.OleWriter()

    writer.addEntry('__nameid_version1.0', storage = True)
    writer.addEntry('__nameid_version1.0/__substg1.0_00020102', b'')
    writer.addEntry('__nameid_version1.0/__substg1.0_00030102', b'')
    writer.addEntry('__nameid_version1.0/__substg1.0_00040102', b'')

    writer.addEntry('__substg1.0_001A001F', 'IPM.Note'.encode('utf-16-le'))
    writer.addEntry('__substg1.0_0037001F', 'Archive test'.encode('utf-16-le'))
    writer.addEntry('__substg1.0_1000001F', 'body'.encode('utf-16-le'))
    writer.addEntry(
        '__properties_version1.0',
        b'\x00' * 8 + struct.pack('<4I', 0, 1, 0, 0) + b'\x00' * 8
    )

    def addDataAttachment(prefix: str, index: int, name: str, data: bytes) -> None:
        base = f'{prefix}/__attach_version1.0_#{index:08d}'
        encoded = name.encode('utf-16-le')
        writer.addEntry(base, storage = True)
        props = b'\x00' * 8
        props += _prop(0x3705, 0x0003, struct.pack('<I', 1) + b'\x00' * 4)
        props += _varprop(0x3707, 0x001F, encoded)
        props += _varprop(0x3704, 0x001F, encoded)
        writer.addEntry(base + '/__properties_version1.0', props)
        writer.addEntry(base + '/__substg1.0_37010102', data)
        writer.addEntry(base + '/__substg1.0_3707001F', encoded)
        writer.addEntry(base + '/__substg1.0_3704001F', encoded)

    addDataAttachment('', 0, 'invoice.pdf', b'OUTER-PDF' * 64)

    forwards = (
        ('invoice.pdf', b'FORWARDED-PDF' * 64),
        ('invoice?.pdf', b'FORWARDED-PDF' * 64),
    )
    for index, (name, data) in enumerate(forwards, 1):
        attachmentBase = f'__attach_version1.0_#{index:08d}'
        encoded = name.encode('utf-16-le')
        writer.addEntry(attachmentBase, storage = True)
        props = b'\x00' * 8
        props += _prop(0x3705, 0x0003, struct.pack('<I', 5) + b'\x00' * 4)
        props += _varprop(0x3707, 0x001F, encoded)
        writer.addEntry(attachmentBase + '/__properties_version1.0', props)
        writer.addEntry(attachmentBase + '/__substg1.0_3707001F', encoded)

        embedded = attachmentBase + '/__substg1.0_3701000D'
        writer.addEntry(embedded, storage = True)
        writer.addEntry(embedded + '/__properties_version1.0', b'\x00' * 24)
        writer.addEntry(
            embedded + '/__substg1.0_001A001F',
            'IPM.Note'.encode('utf-16-le')
        )
        writer.addEntry(
            embedded + '/__substg1.0_0037001F',
            f'Forwarded {index}'.encode('utf-16-le')
        )
        writer.addEntry(
            embedded + '/__substg1.0_1000001F',
            'nested body'.encode('utf-16-le')
        )
        addDataAttachment(embedded, 0, name, data)

    writer.write(path)


class AttachmentManifestTests(unittest.TestCase):
    def setUp(self):
        self.tempDir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tempDir.cleanup)
        self.root = pathlib.Path(self.tempDir.name)
        self.msgPath = self.root / 'three-occurrences.msg'
        _createThreeOccurrenceMsg(self.msgPath)

    def testOccurrencesAreDistinct(self):
        with extract_msg.openMsg(self.msgPath) as msg:
            manifest = AttachmentManifest(msg)
            self.assertEqual(len(manifest.entries), 3)
            self.assertEqual(len(manifest.containers), 2)

            entries = manifest.entries
            self.assertEqual([x.embeddedDepth for x in entries], [0, 1, 1])

            # The two forwards have identical bytes but must remain separate
            # occurrences.
            first, second = entries[1], entries[2]
            self.assertEqual(
                first._toDict()['source']['contentSha256'],
                second._toDict()['source']['contentSha256'],
            )
            self.assertNotEqual(first.occurrenceId, second.occurrenceId)
            self.assertNotEqual(
                first.parentOccurrenceId, second.parentOccurrenceId
            )

            # The second forward's display name collides with the first only
            # after invalid filename characters are removed.
            self.assertEqual(first.sanitizedName, 'invoice.pdf')
            self.assertEqual(second.sanitizedName, 'invoice.pdf')

    def testFolderAndZipSuffixesDiverge(self):
        with extract_msg.openMsg(self.msgPath) as msg:
            manifest = AttachmentManifest(msg)

            folder = self.root / 'folder'
            folder.mkdir()
            (folder / 'invoice.pdf').write_bytes(b'UNRELATED-FOLDER')

            folderPaths = msg.saveAttachmentFiles(
                customPath = folder,
                attachmentManifest = manifest,
            )
            self.assertEqual(len(folderPaths), 3)
            self.assertEqual(
                [pathlib.Path(x).name for x in folderPaths],
                ['invoice (2).pdf', 'invoice (3).pdf', 'invoice (4).pdf'],
            )

            archive = self.root / 'archive.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                # Deliberately different pre-existing conflicts from the
                # folder destination.
                z.writestr('export/invoice.pdf', b'UNRELATED-ZIP')
                z.writestr('export/invoice (2).pdf', b'UNRELATED-ZIP-2')

            with zipfile.ZipFile(archive, 'a') as z:
                zipPaths = msg.saveAttachmentFiles(
                    customPath = pathlib.Path('export'),
                    zip = z,
                    attachmentManifest = manifest,
                )

            self.assertEqual(
                zipPaths,
                [
                    'export/invoice (3).pdf',
                    'export/invoice (4).pdf',
                    'export/invoice (5).pdf',
                ],
            )

            # The same three occurrences carry exactly one record per
            # destination, with destination-specific paths.
            self.assertEqual(len(manifest.destinations), 2)
            for entry in manifest.entries:
                records = {x['destinationKind']: x for x in entry.saves}
                self.assertEqual(set(records), {'folder', 'zip'})
                self.assertEqual(records['folder']['status'], 'file')
                self.assertEqual(records['zip']['status'], 'file')
                self.assertNotEqual(
                    records['folder']['relativePath'],
                    records['zip']['relativePath'],
                )
                self.assertTrue(
                    (folder / records['folder']['relativePath']).is_file()
                )
                with zipfile.ZipFile(archive) as z:
                    self.assertIn(
                        'export/' + records['zip']['relativePath'],
                        z.namelist(),
                    )

            # The manifest itself is written into each destination.
            self.assertTrue((folder / 'attachment_manifest.json').is_file())
            with zipfile.ZipFile(archive) as z:
                manifestData = json.loads(
                    z.read('export/attachment_manifest.json')
                )
            self.assertEqual(
                [x['occurrenceId'] for x in manifestData['attachments']],
                [x.occurrenceId for x in manifest.entries],
            )

    def testEmbeddedMsgOnlyDoesNotExpandNestedAttachments(self):
        with extract_msg.openMsg(self.msgPath) as msg:
            manifest = AttachmentManifest(msg)

            msg.save(
                customPath = self.root,
                attachmentsOnly = True,
                extractEmbedded = True,
                attachmentManifest = manifest,
            )

            destination = manifest.destinations[0]
            self.assertEqual(destination['kind'], 'folder')

            for entry in manifest.allEntries:
                record = entry.saves[0]
                if entry.embeddedDepth == 0 and entry.occurrenceId in (
                    x.occurrenceId for x in manifest.containers
                ):
                    self.assertEqual(record['status'], 'file')
                    self.assertTrue(
                        (pathlib.Path(record['path'])).is_file()
                    )
                elif entry.embeddedDepth == 1:
                    self.assertEqual(record['status'], 'not-expanded')
                    self.assertEqual(record['paths'], [])
                    self.assertIsNone(record['path'])
                    self.assertIsNone(record['relativePath'])
                    self.assertIn('not expanded', record['reason'])

    def testEmptyManifestBindsOnSave(self):
        with extract_msg.openMsg(self.msgPath) as msg:
            manifest = AttachmentManifest()
            folder = self.root / 'empty-manifest'
            folder.mkdir()
            msg.saveAttachments(
                customPath = folder,
                attachmentManifest = manifest,
            )
            self.assertEqual(len(manifest.entries), 3)
            self.assertTrue((folder / 'attachment_manifest.json').is_file())

    def testDefaultFolderAndZipConflictsAreRecorded(self):
        """
        The normal ``save`` path must record the real folder/ZIP suffixes and
        keep the source occurrence identity stable across destinations.
        """
        with extract_msg.openMsg(self.msgPath) as msg:
            manifest = AttachmentManifest(msg)

            folderRoot = self.root / 'default-folder'
            folderRoot.mkdir()
            (folderRoot / 'invoice.pdf').write_bytes(b'UNRELATED')

            msg.saveAttachments(
                customPath = folderRoot,
                attachmentManifest = manifest,
            )
            folderManifestPath = folderRoot / 'attachment_manifest.json'
            self.assertTrue(folderManifestPath.is_file())

            archive = self.root / 'default.zip'
            with zipfile.ZipFile(archive, 'w') as z:
                z.writestr('export/invoice.pdf', b'UNRELATED-ZIP')
                z.writestr('export/invoice (2).pdf', b'UNRELATED-ZIP-2')
            with zipfile.ZipFile(archive, 'a') as z:
                msg.saveAttachments(
                    customPath = 'export',
                    zip = z,
                    attachmentManifest = manifest,
                )

            # Source identity and content correspondence are unchanged by the
            # destination.
            identities = [x.occurrenceId for x in manifest.entries]
            hashes = [
                x._toDict()['source']['contentSha256']
                for x in manifest.entries
            ]
            self.assertEqual(len(set(identities)), 3)
            self.assertEqual(hashes[1], hashes[2])
            self.assertNotEqual(hashes[0], hashes[1])

            self.assertEqual(len(manifest.destinations), 2)
            for index, entry in enumerate(manifest.entries):
                records = {x['destinationKind']: x for x in entry.saves}
                self.assertEqual(records['folder']['status'], 'file')
                self.assertEqual(records['zip']['status'], 'file')
                self.assertTrue(
                    (folderRoot / records['folder']['relativePath']).is_file()
                )
                if index == 0:
                    # The top-level occurrence sees the different pre-existing
                    # conflicts in each destination.
                    self.assertNotEqual(
                        records['folder']['relativePath'],
                        records['zip']['relativePath'],
                    )

            with zipfile.ZipFile(archive) as z:
                zipManifest = json.loads(
                    z.read('export/attachment_manifest.json')
                )
            self.assertEqual(
                [x['occurrenceId'] for x in zipManifest['attachments']],
                identities,
            )

    def testDefaultSaveBehaviorUnchangedWithoutManifest(self):
        with extract_msg.openMsg(self.msgPath) as msg:
            container = self.root / 'plain'
            container.mkdir()
            msg.save(
                customPath = container,
                attachmentsOnly = True,
            )
            self.assertFalse(
                (container / 'attachment_manifest.json').exists()
            )
