from __future__ import annotations


__all__ = [
    'AuditManifestTests',
]


import hashlib
import json
import os
import struct
import tempfile
import unittest

from unittest import mock

import extract_msg
import olefile

from extract_msg.audit import (
        buildStreamManifest, StreamManifest, StreamStatus
    )
from extract_msg.msg_classes.msg import MSGFile
from extract_msg.ole_writer import OleWriter


def _utf16(value: str) -> bytes:
    return value.encode('utf-16-le')


def _writeNestedMessage(path) -> None:
    """
    Writes a three-level MSG file.

    The root has a named-property definition.  The first embedded message has
    two embedded messages below it.  Both innermost messages deliberately have
    a ``__substg1.0_0037001F`` stream; one is empty, as is a
    ``__substg1.0_003D001F`` stream in the second.  No inner message has its own
    ``__nameid_version1.0`` storage.
    """
    writer = OleWriter()

    writer.addEntry('__properties_version1.0', b'\x00' * 32)
    writer.addEntry('__substg1.0_001A001F', _utf16('IPM.Note'))
    writer.addEntry('__substg1.0_0037001F', _utf16('root subject'))
    writer.addEntry('__nameid_version1.0/__substg1.0_00020102', b'')
    writer.addEntry('__nameid_version1.0/__substg1.0_00030102', b'')
    writer.addEntry('__nameid_version1.0/__substg1.0_00040102', b'')

    level1 = '__attach_version1.0_#00000000/__substg1.0_3701000D'
    writer.addEntry('__attach_version1.0_#00000000/__properties_version1.0', b'\x00' * 32)
    writer.addEntry(level1 + '/__properties_version1.0', b'\x00' * 32)
    writer.addEntry(level1 + '/__substg1.0_001A001F', _utf16('IPM.Note'))
    writer.addEntry(level1 + '/__substg1.0_0037001F', _utf16('level 1 subject'))

    level2a = level1 + '/__attach_version1.0_#00000000/__substg1.0_3701000D'
    writer.addEntry(level1 + '/__attach_version1.0_#00000000/__properties_version1.0', b'\x00' * 32)
    writer.addEntry(level2a + '/__properties_version1.0', b'\x00' * 32)
    writer.addEntry(level2a + '/__substg1.0_001A001F', _utf16('IPM.Note'))
    writer.addEntry(level2a + '/__substg1.0_0037001F', b'')

    level2b = level1 + '/__attach_version1.0_#00000001/__substg1.0_3701000D'
    writer.addEntry(level1 + '/__attach_version1.0_#00000001/__properties_version1.0', b'\x00' * 32)
    writer.addEntry(level2b + '/__properties_version1.0', b'\x00' * 32)
    writer.addEntry(level2b + '/__substg1.0_001A001F', _utf16('IPM.Note'))
    writer.addEntry(level2b + '/__substg1.0_0037001F', _utf16('level 2 subject'))
    writer.addEntry(level2b + '/__substg1.0_003D001F', b'')

    writer.write(path)


class AuditManifestTests(unittest.TestCase):
    def setUp(self):
        self._tempDir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self._tempDir.name, 'nested.msg')
        _writeNestedMessage(self.path)
        self.addCleanup(self._tempDir.cleanup)

    def _open(self):
        msg = extract_msg.openMsg(self.path, delayAttachments = True)
        self.addCleanup(msg.close)
        return msg

    def _innerMessages(self):
        msg = self._open()
        level1 = msg.attachments[0].data
        return msg, [x.data for x in level1.attachments]

    def testManifestLacksBodyAndMatchesDirectory(self):
        """
        The manifest lists the directory without retaining stream contents.
        """
        msg = self._open()
        manifest = msg.getStreamManifest()
        self.assertIsInstance(manifest, StreamManifest)
        self.assertEqual(
            {x.source for x in manifest},
            {x for x in (str(y) for y in msg.listDir(True, False, True))},
        )
        for entry in manifest:
            self.assertFalse(hasattr(entry, 'data'))
            self.assertNotIn('data', entry.asDict())
            self.assertNotIn('content', entry.asDict())

    def testDigestsOnlyForReadableBytes(self):
        """
        Digest values verify successfully read bytes and are absent otherwise.
        """
        msg = self._open()
        payload = msg.getStream('__substg1.0_0037001F')
        entry = buildStreamManifest(msg, ('__substg1.0_0037001F',)).bySource(
            '__substg1.0_0037001F'
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.status, StreamStatus.OK)
        self.assertEqual(entry.digest, hashlib.sha256(payload).hexdigest())
        self.assertEqual(entry.byteLength, len(payload))

        missing = msg.getStreamManifest(expected = ('__substg1.0_NOPE001F',))
        missingEntry = missing.bySource('__substg1.0_NOPE001F')
        self.assertIsNotNone(missingEntry)
        self.assertEqual(missingEntry.status, StreamStatus.MISSING)
        self.assertIsNone(missingEntry.digest)
        self.assertIsNone(missingEntry.byteLength)

    def testZeroByteIsNotMissing(self):
        """
        An empty stream is distinct from an expected stream that never occurs.
        """
        _, inners = self._innerMessages()
        empty = inners[0].getStreamManifest().bySource(
            str(inners[0].prefix / '__substg1.0_0037001F')
        )
        self.assertIsNotNone(empty)
        self.assertEqual(empty.status, StreamStatus.ZERO_BYTES)
        self.assertEqual(empty.byteLength, 0)
        self.assertEqual(empty.digest, hashlib.sha256(b'').hexdigest())

        absentPath = str(inners[0].prefix / '__substg1.0_DEAD001F')
        absent = inners[0].getStreamManifest(
            expected = ('__substg1.0_DEAD001F',)
        ).bySource(absentPath)
        self.assertIsNotNone(absent)
        self.assertEqual(absent.status, StreamStatus.MISSING)
        self.assertIsNone(absent.digest)

    def testReadErrorIsIsolated(self):
        """
        A failing stream does not hide other streams, in this or other inners.
        """
        _, inners = self._innerMessages()
        failing = inners[1]
        original = MSGFile.getStream
        badPath = str(failing.prefix / '__substg1.0_003D001F')

        def fakeGetStream(msg, filename, prefix = True):
            if msg is failing and str(msg.fixPath(filename, prefix)) == badPath:
                raise OSError('simulated read failure')
            return original(msg, filename, prefix)

        with mock.patch.object(MSGFile, 'getStream', fakeGetStream):
            manifest = failing.getStreamManifest()

        broken = manifest.bySource(badPath)
        self.assertIsNotNone(broken)
        self.assertEqual(broken.status, StreamStatus.READ_ERROR)
        self.assertIsNone(broken.digest)
        self.assertEqual(broken.errorType, 'OSError')
        self.assertEqual(
            manifest.bySource(str(failing.prefix / '__substg1.0_0037001F')).status,
            StreamStatus.OK,
        )
        self.assertEqual(len(manifest), 4)

        # The bad inner does not affect the other inner when the original file
        # is listed as a whole.
        root = failing.treePath[0]()
        whole = root.getStreamManifest()
        self.assertEqual(
            whole.bySource(str(inners[0].prefix / '__substg1.0_0037001F')).status,
            StreamStatus.ZERO_BYTES,
        )

    def _corruptStreamStart(self, msgPath: str, streamPath: str) -> None:
        """
        Makes a directory-listed stream's sector chain invalid.

        The stream stays in the directory, so it is still listed, but reading
        it raises from ``olefile``.
        """
        with olefile.OleFileIO(msgPath) as ole:
            sid = ole._find(streamPath)

        with open(msgPath, 'rb') as f:
            data = bytearray(f.read())

        sectorSize = 1 << struct.unpack_from('<H', data, 30)[0]
        firstDirSector = struct.unpack_from('<I', data, 48)[0]
        entryOffset = (firstDirSector + 1) * sectorSize + sid * 128
        struct.pack_into('<I', data, entryOffset + 116, 0x7FFFFFF0)

        with open(msgPath, 'wb') as f:
            f.write(data)

    def testRealReadErrorDoesNotHideOtherStreams(self):
        """
        A genuinely unreadable stream is reported as ``read_error``; its
        siblings and the other inner message remain intact.
        """
        _, inners = self._innerMessages()
        badInner = inners[1]
        goodInner = inners[0]
        badPath = str(badInner.prefix / '__substg1.0_0037001F')
        self._corruptStreamStart(self.path, badPath)

        msg = self._open()
        whole = msg.getStreamManifest()
        broken = whole.bySource(badPath)
        self.assertIsNotNone(broken)
        self.assertEqual(broken.status, StreamStatus.READ_ERROR)
        self.assertIsNone(broken.digest)
        self.assertEqual(broken.declaredSize, 30)
        self.assertIsNotNone(broken.errorType)

        # The sibling stream in the same inner message is still readable.
        sibling = whole.bySource(str(badInner.prefix / '__substg1.0_003D001F'))
        self.assertEqual(sibling.status, StreamStatus.ZERO_BYTES)

        # The other inner message is unaffected.
        other = whole.bySource(str(goodInner.prefix / '__substg1.0_0037001F'))
        self.assertEqual(other.status, StreamStatus.ZERO_BYTES)
        self.assertEqual(
            len([x for x in whole if x.owner.startswith(str(goodInner.prefix))]),
            3,
        )

        # The manifest itself is still complete: the bad read did not drop
        # surrounding streams from the inventory.
        self.assertEqual(len(whole), 19)

        # The bad inner does not hide the other inner when each is inspected
        # through the actual attachment tree.
        root = msg.attachments[0].data
        first = root.attachments[0].data.getStreamManifest()
        second = root.attachments[1].data.getStreamManifest()
        self.assertEqual(
            first.bySource(str(goodInner.prefix / '__substg1.0_0037001F')).status,
            StreamStatus.ZERO_BYTES,
        )
        self.assertEqual(
            second.bySource(badPath).status, StreamStatus.READ_ERROR
        )
        self.assertEqual(
            second.bySource(str(badInner.prefix / '__substg1.0_003D001F')).status,
            StreamStatus.ZERO_BYTES,
        )

    def testInnerRelativeAndPrefixedSourcesMatch(self):
        """
        ``prefix = True`` and ``prefix = False`` resolve to the same sources,
        and expected misses stay local to each inner.
        """
        msg, inners = self._innerMessages()
        whole = msg.getStreamManifest()

        for inner in inners:
            prefix = str(inner.prefix)
            relative = [
                x.relativePath for x in whole
                if x.source.startswith(prefix + '/')
            ]
            expectedRelative = relative + ['__substg1.0_DEAD001F']
            expectedAbsolute = [
                x.source for x in whole if x.source.startswith(prefix + '/')
            ] + [prefix + '/__substg1.0_DEAD001F']

            with self.subTest(prefix):
                byRelative = inner.getStreamManifest(expectedRelative)
                byPrefix = inner.getStreamManifest(expectedAbsolute, prefix = False)
                self.assertEqual(
                    [x.source for x in byRelative], [x.source for x in byPrefix]
                )
                self.assertEqual(
                    [x.status for x in byRelative], [x.status for x in byPrefix]
                )
                self.assertEqual(
                    [x.digest for x in byRelative], [x.digest for x in byPrefix]
                )
                for entry in byRelative:
                    if entry.status is StreamStatus.MISSING:
                        self.assertIsNone(whole.bySource(entry.source))
                    else:
                        self.assertTrue(
                            whole.bySource(entry.source) is not None,
                            f'{entry.source} is not in the whole-file manifest',
                        )

                # Exactly the one injected path is missing.
                self.assertEqual(
                    [x.source for x in byRelative.withStatus('missing')],
                    [prefix + '/__substg1.0_DEAD001F'],
                )

    def testRootDefinitionReferenceIsNotRestored(self):
        """
        Asking an inner for a root-stored definition returns that root source,
        not a copy attributed to the inner.
        """
        msg, inners = self._innerMessages()
        inner = inners[1]
        definition = '__nameid_version1.0/__substg1.0_00040102'
        manifest = inner.getStreamManifest(expected = (definition,))
        entry = manifest.bySource(definition)
        self.assertIsNotNone(entry)
        self.assertFalse(entry.inScope)
        self.assertTrue(entry.referenced)
        self.assertEqual(entry.owner, '.')
        self.assertEqual(entry.source, definition)
        self.assertEqual(entry.digest, hashlib.sha256(b'').hexdigest())

        whole = msg.getStreamManifest()
        self.assertEqual(whole.bySource(definition).source, entry.source)
        self.assertEqual(whole.bySource(definition).owner, '.')

        # No inner entry owns that path.
        for other in inners:
            self.assertFalse(
                any(x.source == definition and x.owner for x in other.getStreamManifest())
            )

    def testSingleEntryRelativeAndFullPath(self):
        """
        A single-entry lookup accepts the same two path forms as a manifest.
        """
        _, inners = self._innerMessages()
        inner = inners[1]
        relative = inner.getStreamManifestEntry('__substg1.0_0037001F')
        full = inner.getStreamManifestEntry(
            inner.prefix / '__substg1.0_0037001F', prefix = False
        )
        self.assertEqual(relative.source, full.source)
        self.assertEqual(relative.digest, full.digest)
        self.assertEqual(relative.status, StreamStatus.OK)
        self.assertEqual(
            inner.getStreamManifestEntry('__substg1.0_DEAD001F').status,
            StreamStatus.MISSING,
        )

    def testRootNamedDefinitionsAreCountedOnce(self):
        """
        Named-property definitions stored at the root are not duplicated into
        inner message manifests.
        """
        msg, inners = self._innerMessages()
        whole = msg.getStreamManifest()
        named = [x for x in whole if x.source.startswith('__nameid_version1.0/')]
        self.assertEqual(len(named), 3)
        self.assertTrue(all(x.owner == '.' for x in named))

        for inner in inners:
            manifest = inner.getStreamManifest()
            self.assertFalse(
                any('__nameid_version1.0' in x.source for x in manifest),
                'Root named property definitions were copied into an inner manifest.',
            )
        # References do not change the physical occurrence count.
        self.assertEqual(
            len([x for x in whole if x.source.startswith('__nameid_version1.0/')]),
            3,
        )

    def testExpectedExistingPathIsNotDuplicated(self):
        """
        Listing a real stream through ``expected`` does not add a second entry.
        """
        msg = self._open()
        plain = msg.getStreamManifest()
        targeted = msg.getStreamManifest(expected = ('__substg1.0_001A001F',))
        self.assertEqual(len(plain), len(targeted))
        self.assertEqual([x.source for x in plain], [x.source for x in targeted])

    def testExpectedStorageIsMissingNotReadError(self):
        """
        A storage name supplied as an expected stream is ``missing``.
        """
        msg = self._open()
        storage = '__attach_version1.0_#00000000'
        entry = msg.getStreamManifest(expected = (storage,)).bySource(storage)
        self.assertIsNotNone(entry)
        self.assertEqual(entry.status, StreamStatus.MISSING)
        self.assertIsNone(entry.digest)

    def testOrderAndDuplicatesAreStable(self):
        """
        Repeated manifests have the same order and keep equal-content entries.
        """
        msg = self._open()
        first = msg.getStreamManifest()
        second = msg.getStreamManifest()
        self.assertEqual([x.source for x in first], [x.source for x in second])
        self.assertEqual(first.toJSON(), second.toJSON())
        parsed = json.loads(first.toJSON())
        self.assertEqual(
            [x['source'] for x in parsed['entries']],
            [x.source for x in first],
        )

        # Same relative name in different inners and same digest are retained.
        sameName = [x for x in first if x.relativePath == '__substg1.0_001A001F']
        self.assertGreaterEqual(len(sameName), 3)
        self.assertEqual(len({x.source for x in sameName}), len(sameName))

        zeroDigest = hashlib.sha256(b'').hexdigest()
        zeroes = [x for x in first if x.digest == zeroDigest]
        self.assertGreaterEqual(len(zeroes), 2)
        self.assertEqual(len({x.source for x in zeroes}), len(zeroes))

    def testOwnerLevels(self):
        """
        Ownership follows physical message storages, not logical references.
        """
        msg = self._open()
        manifest = msg.getStreamManifest()
        owners = {x.owner for x in manifest}
        self.assertIn('.', owners)
        self.assertIn('__attach_version1.0_#00000000/__substg1.0_3701000D', owners)
        level2 = {
            x for x in owners
            if x.count('__substg1.0_3701000D') == 2
        }
        self.assertEqual(len(level2), 2)
        for entry in manifest:
            if entry.owner in level2:
                self.assertEqual(entry.ownerLevel, 2)
                self.assertFalse(entry.relativePath.startswith('__attach_version1.0_'))
            elif entry.owner == '.':
                self.assertEqual(entry.ownerLevel, 0)
            else:
                self.assertEqual(entry.ownerLevel, 1)

    def testGenericAllExampleFiles(self):
        """
        The manifest covers every stream in each shipped example file.
        """
        from .constants import TEST_FILE_DIR
        for path in sorted(TEST_FILE_DIR.glob('*.msg')):
            with self.subTest(path.name):
                with extract_msg.openMsg(path, delayAttachments = True) as msg:
                    manifest = msg.getStreamManifest()
                    expected = [str(x) for x in msg.listDir(True, False, True)]
                    self.assertCountEqual([x.source for x in manifest], expected)
                    self.assertEqual(manifest.toJSON(), msg.getStreamManifest().toJSON())
