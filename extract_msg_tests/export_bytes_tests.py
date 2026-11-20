__all__ = [
    'ExportBytesBoundaryTests',
    'ExportBytesFidelityTests',
]


import io
import struct
import unittest

import extract_msg

from extract_msg.enums import AttachmentType
from extract_msg.exceptions import StandardViolationError
from extract_msg.ole_writer import OleWriter


# Shared fixture content. The parent message and the innermost message of the
# two-level original deliberately use the *same* property definitions with
# different values so the tests can prove which message a copy represents.
PARENT_BODY = 'Parent body: this copy must never be archived.'
PARENT_SUBJECT = 'Parent subject'
PARENT_SENDER = 'parent-only-sender@example.com'
PARENT_NAMED = 'parent-named-value'
PARENT_ATT_DATA = b'PARENT-LOCAL-ATTACHMENT-\x00\x01\x02'

MIDDLE_BODY = 'Middle body: first forward level.'
MIDDLE_SUBJECT = 'Middle subject'

INNER_BODY = 'Innermost body: this is the copy to archive.'
INNER_SUBJECT = 'Innermost subject'
INNER_NAMED = 'innermost-named-value'
INNER_ATT_DATA = b'INNER-LOCAL-ATTACHMENT-\x03\x04\x05'

# Attachment name shared by the parent and the innermost message. The contents
# differ, so a mix-up is detectable.
SHARED_ATT_NAME = 'shared.txt'

# String-named property used in every fixture. The GUID bytes below unpack to
# this exact string through extract_msg.utils.bytesToGuid.
NAMED_PROP_NAME = 'ArchiveTag'
NAMED_PROP_GUID = '{11111111-2222-3333-4455-66778899AABB}'

# Boundary input 1 (embedded properties stream uses the top level layout).
B1_ROOT_BODY = 'Boundary 1 root body.'
B1_BODY = 'Boundary 1 embedded body.'
B1_NAMED = 'boundary-1-named-value'

# Boundary input 2 (embedded message carries its own named property catalog
# that disagrees with the root definition).
B2_ROOT_BODY = 'Boundary 2 root body.'
B2_BODY = 'Boundary 2 embedded body.'
B2_NAMED = 'boundary-2-named-value'
B2_FOREIGN_NAME = 'ForgedTag'


def _u(text: str) -> bytes:
    """
    Encodes a string the way Unicode (001F) streams are stored.
    """
    return text.encode('utf-16-le')


def _int32Prop(propertyID: int, value: int) -> bytes:
    """
    Builds a 16 byte fixed length property entry of type PtypInteger32.
    """
    return struct.pack('<HHI', 0x0003, propertyID, 0) + struct.pack('<I', value) + b'\x00' * 4


def _propertiesStream(entries, kind: str) -> bytes:
    """
    Builds a __properties_version1.0 stream from 16 byte property entries.

    :param kind: ``'message'`` for the 32 byte top level header, ``'embed'``
        for the 24 byte embedded message header, or ``'attachment'`` for the
        8 byte attachment/recipient header.
    """
    if kind == 'attachment':
        header = b'\x00' * 8
    else:
        # NextRecipientID, NextAttachmentID, RecipientCount, AttachmentCount.
        header = struct.pack('<8x4I', 0, 0, 0, 0)
        if kind == 'message':
            # Top level property streams carry 8 extra reserved bytes.
            header += b'\x00' * 8
        elif kind != 'embed':
            raise ValueError(f'Unknown properties stream kind: {kind}')
    return header + b''.join(entries)


def _namedPropertyStreams(propName: str, pid: int = 0):
    """
    Builds the three __nameid_version1.0 streams that map ``propName`` (in
    NAMED_PROP_GUID) to the property stream ID 0x8000 + ``pid``.
    """
    guidStream = struct.pack('<IHH', 0x11111111, 0x2222, 0x3333) + bytes.fromhex('445566778899AABB')
    nameBytes = propName.encode('utf-16-le')
    stringStream = struct.pack('<i', len(nameBytes)) + nameBytes
    if len(stringStream) & 3:
        stringStream += b'\x00' * (4 - (len(stringStream) & 3))
    # Entry: name offset 0 in the string stream, GUID index 3 (first GUID in
    # the GUID stream), string-named kind, property ID pid.
    entryStream = struct.pack('<IHH', 0, (3 << 1) | 1, pid)
    return guidStream, entryStream, stringStream


def _addNamedPropertyMapping(writer: OleWriter, propName: str, pid: int = 0, prefix: str = '') -> None:
    """
    Adds a __nameid_version1.0 catalog mapping a single string-named property.
    """
    guidStream, entryStream, stringStream = _namedPropertyStreams(propName, pid)
    base = f'{prefix}/__nameid_version1.0' if prefix else '__nameid_version1.0'
    writer.addEntry(base, storage = True)
    writer.addEntry(base + '/__substg1.0_00020102', guidStream)
    writer.addEntry(base + '/__substg1.0_00030102', entryStream)
    writer.addEntry(base + '/__substg1.0_00040102', stringStream)


def _addMessageStreams(writer: OleWriter, prefix: str, streams) -> None:
    """
    Adds the streams of one message under ``prefix`` (empty for the root).
    """
    base = f'{prefix}/' if prefix else ''
    for name, data in streams.items():
        writer.addEntry(base + name, data)


def _addDataAttachment(writer: OleWriter, prefix: str, attDir: str, filename: str, data: bytes) -> None:
    """
    Adds a plain data attachment (attach method 1) to a message.
    """
    base = f'{prefix}/{attDir}' if prefix else attDir
    writer.addEntry(base, storage = True)
    writer.addEntry(base + '/__properties_version1.0', _propertiesStream([_int32Prop(0x3705, 1)], 'attachment'))
    writer.addEntry(base + '/__substg1.0_3707001F', _u(filename))
    writer.addEntry(base + '/__substg1.0_37010102', data)


def _addEmbeddedAttachment(writer: OleWriter, prefix: str, attDir: str, displayName: str) -> str:
    """
    Adds an embedded message attachment (attach method 5) to a message and
    returns the prefix at which the embedded message streams must be placed.
    """
    base = f'{prefix}/{attDir}' if prefix else attDir
    writer.addEntry(base, storage = True)
    writer.addEntry(base + '/__properties_version1.0', _propertiesStream([_int32Prop(0x3705, 5)], 'attachment'))
    writer.addEntry(base + '/__substg1.0_3001001F', _u(displayName))
    writer.addEntry(base + '/__substg1.0_3701000D', storage = True)
    return base + '/__substg1.0_3701000D'


def _storeSupportMaskUnicode() -> bytes:
    """
    Property 340D0003 with the unicode strings flag (0x00040000) set.
    """
    return _int32Prop(0x340D, 0x00040000)


def _write(writer: OleWriter) -> bytes:
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def _buildTwoLevelOriginal() -> bytes:
    """
    Builds the specimen file entirely offline: a parent message holding an
    embedded middle message which itself holds the innermost message.

    The parent and the innermost message define the same properties (class
    type, body, subject, and the ArchiveTag named property) with different
    values, and both carry a local attachment named ``shared.txt`` with
    different contents.
    """
    writer = OleWriter()
    # The named property catalog only exists at the root of the file.
    _addNamedPropertyMapping(writer, NAMED_PROP_NAME)

    # Root (parent) message.
    _addMessageStreams(writer, '', {
        '__properties_version1.0': _propertiesStream([_storeSupportMaskUnicode()], 'message'),
        '__substg1.0_001A001F': _u('IPM.Note'),
        '__substg1.0_1000001F': _u(PARENT_BODY),
        '__substg1.0_0037001F': _u(PARENT_SUBJECT),
        # A property that only the parent defines.
        '__substg1.0_0C1A001F': _u(PARENT_SENDER),
        '__substg1.0_8000001F': _u(PARENT_NAMED),
    })
    _addDataAttachment(writer, '', '__attach_version1.0_#00000000', SHARED_ATT_NAME, PARENT_ATT_DATA)

    # Middle message (first embed level).
    middlePrefix = _addEmbeddedAttachment(writer, '', '__attach_version1.0_#00000001', 'middle.msg')
    _addMessageStreams(writer, middlePrefix, {
        '__properties_version1.0': _propertiesStream([_storeSupportMaskUnicode()], 'embed'),
        '__substg1.0_001A001F': _u('IPM.Note'),
        '__substg1.0_1000001F': _u(MIDDLE_BODY),
        '__substg1.0_0037001F': _u(MIDDLE_SUBJECT),
    })

    # Innermost message (second embed level).
    innerPrefix = _addEmbeddedAttachment(writer, middlePrefix, '__attach_version1.0_#00000000', 'inner.msg')
    _addMessageStreams(writer, innerPrefix, {
        '__properties_version1.0': _propertiesStream([_storeSupportMaskUnicode()], 'embed'),
        '__substg1.0_001A001F': _u('IPM.Note'),
        '__substg1.0_1000001F': _u(INNER_BODY),
        '__substg1.0_0037001F': _u(INNER_SUBJECT),
        '__substg1.0_8000001F': _u(INNER_NAMED),
    })
    _addDataAttachment(writer, innerPrefix, '__attach_version1.0_#00000000', SHARED_ATT_NAME, INNER_ATT_DATA)

    return _write(writer)


def _buildTopLevelPropsEmbedOriginal() -> bytes:
    """
    Boundary input 1: the embedded message's properties stream uses the top
    level (32 byte header) layout instead of the embedded (24 byte header)
    layout. The stream also omits the store support mask so that the repair
    performed in allowBadEmbed mode is observable.
    """
    writer = OleWriter()
    _addNamedPropertyMapping(writer, NAMED_PROP_NAME)

    _addMessageStreams(writer, '', {
        '__properties_version1.0': _propertiesStream([_storeSupportMaskUnicode()], 'message'),
        '__substg1.0_001A001F': _u('IPM.Note'),
        '__substg1.0_1000001F': _u(B1_ROOT_BODY),
    })

    innerPrefix = _addEmbeddedAttachment(writer, '', '__attach_version1.0_#00000000', 'inner.msg')
    _addMessageStreams(writer, innerPrefix, {
        # Anomaly: top level layout inside an embedded message.
        '__properties_version1.0': _propertiesStream([], 'message'),
        '__substg1.0_001A001F': _u('IPM.Note'),
        '__substg1.0_1000001F': _u(B1_BODY),
        '__substg1.0_8000001F': _u(B1_NAMED),
    })

    return _write(writer)


def _buildForeignCatalogOriginal() -> bytes:
    """
    Boundary input 2: the embedded message unexpectedly carries its own
    __nameid_version1.0 catalog, and that catalog disagrees with the root
    definition (it maps a different name to the same property stream ID).
    """
    writer = OleWriter()
    # Root catalog: ArchiveTag -> stream 0x8000.
    _addNamedPropertyMapping(writer, NAMED_PROP_NAME)

    _addMessageStreams(writer, '', {
        '__properties_version1.0': _propertiesStream([_storeSupportMaskUnicode()], 'message'),
        '__substg1.0_001A001F': _u('IPM.Note'),
        '__substg1.0_1000001F': _u(B2_ROOT_BODY),
    })

    innerPrefix = _addEmbeddedAttachment(writer, '', '__attach_version1.0_#00000000', 'inner.msg')
    _addMessageStreams(writer, innerPrefix, {
        '__properties_version1.0': _propertiesStream([_storeSupportMaskUnicode()], 'embed'),
        '__substg1.0_001A001F': _u('IPM.Note'),
        '__substg1.0_1000001F': _u(B2_BODY),
        '__substg1.0_8000001F': _u(B2_NAMED),
    })
    # Anomaly: the embedded message has its own catalog, mapping a *different*
    # name (ForgedTag) to the same stream ID 0x8000.
    _addNamedPropertyMapping(writer, B2_FOREIGN_NAME, prefix = innerPrefix)

    return _write(writer)


def _getAttachmentOfType(msg, attachmentType: AttachmentType):
    """
    Returns the single attachment of the requested type.
    """
    matches = [att for att in msg.attachments if att.type == attachmentType]
    if len(matches) != 1:
        raise AssertionError(f'Expected exactly 1 attachment of type {attachmentType}, found {len(matches)}.')
    return matches[0]



class ExportBytesFidelityTests(unittest.TestCase):
    """
    Itemized fidelity checks for re-rooting the innermost message of a
    two-level embedded original with ``MSGFile.exportBytes``.
    """

    @classmethod
    def setUpClass(cls):
        cls.originalBytes = _buildTwoLevelOriginal()

    def testFixtureReadsBackAsBuilt(self):
        """
        Sanity check that the original reads back with the intended split of
        values between parent and innermost message.
        """
        with extract_msg.openMsg(self.originalBytes) as rootMsg:
            with self.subTest('parent body'):
                self.assertEqual(rootMsg.body, PARENT_BODY)
            with self.subTest('parent local attachment'):
                parentAtt = _getAttachmentOfType(rootMsg, AttachmentType.DATA)
                self.assertEqual(parentAtt.longFilename, SHARED_ATT_NAME)
                self.assertEqual(parentAtt.data, PARENT_ATT_DATA)

            middleMsg = _getAttachmentOfType(rootMsg, AttachmentType.MSG).data
            innerMsg = _getAttachmentOfType(middleMsg, AttachmentType.MSG).data

            with self.subTest('inner body'):
                self.assertEqual(innerMsg.body, INNER_BODY)
            with self.subTest('inner subject'):
                self.assertEqual(innerMsg.getStringStream('__substg1.0_0037'), INNER_SUBJECT)
            with self.subTest('inner named property resolves through the root catalog'):
                self.assertEqual(innerMsg.getNamedProp(NAMED_PROP_NAME, NAMED_PROP_GUID), INNER_NAMED)
            with self.subTest('inner local attachment'):
                innerAtt = _getAttachmentOfType(innerMsg, AttachmentType.DATA)
                self.assertEqual(innerAtt.longFilename, SHARED_ATT_NAME)
                self.assertEqual(innerAtt.data, INNER_ATT_DATA)

    def testInnermostExportRoundTrip(self):
        """
        Exports the innermost message and opens the product as a new root,
        checking body, local attachment, and named property item by item.
        """
        with extract_msg.openMsg(self.originalBytes) as rootMsg:
            middleMsg = _getAttachmentOfType(rootMsg, AttachmentType.MSG).data
            innerMsg = _getAttachmentOfType(middleMsg, AttachmentType.MSG).data
            exportedBytes = innerMsg.exportBytes()

        with extract_msg.openMsg(exportedBytes) as rooted:
            with self.subTest('class type survives re-rooting'):
                self.assertEqual(rooted.classType, 'IPM.Note')
            with self.subTest('body matches the innermost message'):
                self.assertEqual(rooted.body, INNER_BODY)
            with self.subTest('subject matches the innermost message'):
                self.assertEqual(rooted.getStringStream('__substg1.0_0037'), INNER_SUBJECT)
            with self.subTest('named property matches the innermost message'):
                self.assertEqual(rooted.getNamedProp(NAMED_PROP_NAME, NAMED_PROP_GUID), INNER_NAMED)
            with self.subTest('local attachment is the innermost one'):
                self.assertEqual(len(rooted.attachments), 1)
                att = rooted.attachments[0]
                self.assertEqual(att.type, AttachmentType.DATA)
                self.assertEqual(att.longFilename, SHARED_ATT_NAME)
                self.assertEqual(att.data, INNER_ATT_DATA)

    def testRerootingLayoutChangeIsNotDamage(self):
        """
        The embedded properties stream must gain the 8 reserved bytes of the
        top level layout when re-rooted. This container change is expected and
        must leave the property data itself readable.
        """
        with extract_msg.openMsg(self.originalBytes) as rootMsg:
            middleMsg = _getAttachmentOfType(rootMsg, AttachmentType.MSG).data
            innerMsg = _getAttachmentOfType(middleMsg, AttachmentType.MSG).data
            exportedBytes = innerMsg.exportBytes()

        with extract_msg.openMsg(exportedBytes) as rooted:
            with self.subTest('properties stream now uses the top level layout'):
                expected = _propertiesStream([_storeSupportMaskUnicode()], 'message')
                self.assertEqual(rooted.getStream('__properties_version1.0'), expected)
            with self.subTest('store support mask still parses'):
                self.assertEqual(rooted.getPropertyVal('340D0003'), 0x00040000)
            with self.subTest('root catalog was copied into the copy'):
                self.assertIsNotNone(rooted.getStream('__nameid_version1.0/__substg1.0_00030102'))

    def testParentContentDoesNotLeakIntoCopy(self):
        """
        The parent (and middle) message must not be able to supply or
        contaminate the content of the exported innermost copy.
        """
        with extract_msg.openMsg(self.originalBytes) as rootMsg:
            middleMsg = _getAttachmentOfType(rootMsg, AttachmentType.MSG).data
            innerMsg = _getAttachmentOfType(middleMsg, AttachmentType.MSG).data
            exportedBytes = innerMsg.exportBytes()

        with self.subTest('parent attachment data absent from container'):
            self.assertNotIn(PARENT_ATT_DATA, exportedBytes)
        with self.subTest('parent body absent from container'):
            self.assertNotIn(_u(PARENT_BODY), exportedBytes)
        with self.subTest('middle body absent from container'):
            self.assertNotIn(_u(MIDDLE_BODY), exportedBytes)
        with self.subTest('parent named value absent from container'):
            self.assertNotIn(_u(PARENT_NAMED), exportedBytes)

        with extract_msg.openMsg(exportedBytes) as rooted:
            with self.subTest('parent-only property was not filled in'):
                self.assertIsNone(rooted.getStringStream('__substg1.0_0C1A'))
            with self.subTest('body is not the parent body'):
                self.assertNotEqual(rooted.body, PARENT_BODY)
            with self.subTest('named value is not the parent value'):
                self.assertNotEqual(rooted.getNamedProp(NAMED_PROP_NAME, NAMED_PROP_GUID), PARENT_NAMED)
            with self.subTest('local attachment is not the parent attachment'):
                self.assertNotEqual(rooted.attachments[0].data, PARENT_ATT_DATA)



class ExportBytesBoundaryTests(unittest.TestCase):
    """
    Records the earliest visible rejection or repair for the two boundary
    inputs, in default (strict) mode and in allowBadEmbed mode, and which
    evidence remains recoverable from each copy.
    """

    def testTopLevelPropertiesStreamEmbed(self):
        """
        Boundary input 1: embedded properties stream uses the top level
        layout.

        Strict mode must reject the export; allowBadEmbed must repair the
        stream (adding the missing store support mask) and produce a copy
        whose body and named property still reconcile with the original.
        """
        with extract_msg.openMsg(_buildTopLevelPropsEmbedOriginal()) as rootMsg:
            innerMsg = _getAttachmentOfType(rootMsg, AttachmentType.MSG).data

            with self.subTest('strict mode rejects at the properties stream'):
                with self.assertRaises(StandardViolationError) as ctx:
                    innerMsg.exportBytes()
                self.assertIn('top level properties stream', str(ctx.exception))

            exportedBytes = innerMsg.exportBytes(allowBadEmbed = True)

        with extract_msg.openMsg(exportedBytes) as rooted:
            with self.subTest('body preserved by lenient export'):
                self.assertEqual(rooted.body, B1_BODY)
            with self.subTest('named property preserved by lenient export'):
                self.assertEqual(rooted.getNamedProp(NAMED_PROP_NAME, NAMED_PROP_GUID), B1_NAMED)
            with self.subTest('repair added the missing store support mask'):
                self.assertEqual(rooted.getPropertyVal('340D0003'), 0x00040000)
            with self.subTest('properties stream has a root-compatible layout'):
                propsStream = rooted.getStream('__properties_version1.0')
                self.assertEqual(len(propsStream) % 16, 0)

    def testForeignNamedPropertyCatalog(self):
        """
        Boundary input 2: the embedded message carries its own named property
        catalog that disagrees with the root definition.

        Strict mode must reject the export. allowBadEmbed produces a copy, but
        the copy keeps the *foreign* catalog: the body survives while the
        named property is lost under its proper identity. These are asserted
        independently so the divergence is visible on its own.
        """
        with extract_msg.openMsg(_buildForeignCatalogOriginal()) as rootMsg:
            innerMsg = _getAttachmentOfType(rootMsg, AttachmentType.MSG).data

            with self.subTest('original resolves the named property through the root catalog'):
                self.assertEqual(innerMsg.getNamedProp(NAMED_PROP_NAME, NAMED_PROP_GUID), B2_NAMED)

            with self.subTest('strict mode rejects at the named property catalog'):
                with self.assertRaises(StandardViolationError) as ctx:
                    innerMsg.exportBytes()
                self.assertIn('own named streams', str(ctx.exception))

            exportedBytes = innerMsg.exportBytes(allowBadEmbed = True)

        with extract_msg.openMsg(exportedBytes) as rooted:
            with self.subTest('body preserved by lenient export'):
                self.assertEqual(rooted.body, B2_BODY)
            with self.subTest('named property lost under its proper identity'):
                self.assertIsNone(rooted.getNamedProp(NAMED_PROP_NAME, NAMED_PROP_GUID))
            with self.subTest('value still recoverable under the foreign identity'):
                self.assertEqual(rooted.getNamedProp(B2_FOREIGN_NAME, NAMED_PROP_GUID), B2_NAMED)
            with self.subTest('raw value stream still present in the copy'):
                self.assertEqual(rooted.getStream('__substg1.0_8000001F'), _u(B2_NAMED))
