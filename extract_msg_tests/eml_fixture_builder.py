"""
Builds the synthetic MSG fixtures used by the EML conversion tests.

The fixtures are written with :class:`extract_msg.ole_writer.OleWriter` so the
suite does not depend on Outlook-generated binaries. Two top-level messages are
produced:

* ``eml-forwarded-msg.msg``: an outer message containing an inline image, a
  scanned document, a regular attachment and an embedded (forwarded) ``.msg``
  that keeps its own recipients, inline image and regular attachment.
* ``eml-missing-outer-image.msg``: the same structure except the outer message
  has no inline image part even though its HTML still references the shared
  Content-ID, while the embedded message still carries an image for that ID.

Both HTML bodies reference the same Content-ID (``image001``) but back it with
different bytes, and both messages contain a regular attachment named
``report.txt`` with different contents.
"""

__all__ = [
    'SHARED_CID',
    'buildFixtures',
]


import io
import struct

from pathlib import Path
from typing import Dict, List, Optional, Tuple

from extract_msg.ole_writer import OleWriter


# Property type tags from [MS-OXCDATA].
PT_LONG = 0x0003
PT_BOOLEAN = 0x000B
PT_UNICODE = 0x001F
PT_BINARY = 0x0102

SHARED_CID = 'image001@archive.test'

OUTER_IMAGE = (
    b'\x89PNG\r\n\x1a\n'
    b'OUTER-IMAGE-BYTES'
)
INNER_IMAGE = (
    b'\x89PNG\r\n\x1a\n'
    b'INNER-IMAGE-BYTES'
)
SCAN_BYTES = b'%PDF-1.4\nouter scanned document bytes\n%%EOF\n'
OUTER_REPORT = b'outer report attachment contents\n'
INNER_REPORT = b'inner report attachment contents\n'


def _propEntry(propType: int, propId: int, value) -> bytes:
    """
    Serializes a single 16-byte property descriptor plus its value bytes.
    """
    if propType == PT_LONG:
        return struct.pack('<HHIII', propType, propId, 6, int(value), 0)
    if propType == PT_BOOLEAN:
        return struct.pack('<HHIHHI', propType, propId, 6, 1 if value else 0, 0, 0)
    if propType == PT_UNICODE:
        if isinstance(value, str):
            value = value.encode('utf-16-le') + b'\x00\x00'
        return struct.pack('<HHIII', propType, propId, 6, len(value), 0) + value
    if propType == PT_BINARY:
        return struct.pack('<HHIII', propType, propId, 6, len(value), 0) + value
    raise ValueError(f'Unsupported property type: {propType}')


def _propertyStream(entries: List[bytes], embedded: bool = False,
                    header: Optional[Tuple[int, int, int, int]] = None) -> bytes:
    """
    Builds a ``__properties_version1.0`` stream from descriptor entries.
    """
    stream = b'\x00' * 8
    if header is not None:
        stream += struct.pack('<IIII', *header)
        # Top-level message streams carry an additional 8 reserved bytes.
        if not embedded:
            stream += b'\x00' * 8
    stream += b''.join(entries)
    return stream


def _u16(text: str) -> bytes:
    """
    Encodes a string as an MSG UTF-16 stream without a NUL terminator.
    """
    return text.encode('utf-16-le')


def _addNameIdStorage(writer: OleWriter) -> None:
    """
    Adds an empty named-property storage, which MSG readers require to exist.
    """
    writer.addEntry('__nameid_version1.0', storage = True)
    for name in ('__substg1.0_00020102', '__substg1.0_00030102',
                 '__substg1.0_00040102'):
        writer.addEntry('__nameid_version1.0/' + name, b'')


def _addRecipient(writer: OleWriter, base: str, index: int, name: str,
                  email: str, recipientType: int) -> None:
    directory = f'{base}/__recip_version1.0_#{index:08X}'
    writer.addEntry(directory, storage = True)
    writer.addEntry(directory + '/__substg1.0_3001001F', _u16(name))
    writer.addEntry(directory + '/__substg1.0_3003001F', _u16(email))
    writer.addEntry(directory + '/__properties_version1.0', _propertyStream([
        _propEntry(PT_LONG, 0x0C15, recipientType),
        _propEntry(PT_UNICODE, 0x3001, _u16(name)),
        _propEntry(PT_UNICODE, 0x3003, _u16(email)),
    ]))


def _addDataAttachment(writer: OleWriter, base: str, index: int, *,
                       filename: str, mimetype: str, data: bytes,
                       cid: Optional[str] = None, hidden: bool = False,
                       renderingPosition: int = 0xFFFFFFFF) -> str:
    """
    Adds a regular (``afByValue``) data attachment and returns its directory.
    """
    directory = f'{base}/__attach_version1.0_#{index:08X}'
    writer.addEntry(directory, storage = True)
    writer.addEntry(directory + '/__substg1.0_37010102', data)
    writer.addEntry(directory + '/__substg1.0_3707001F', _u16(filename))
    writer.addEntry(directory + '/__substg1.0_370E001F', _u16(mimetype))
    entries = [
        _propEntry(PT_LONG, 0x3705, 1),
        _propEntry(PT_LONG, 0x370B, renderingPosition),
        _propEntry(PT_BOOLEAN, 0x7FFE, hidden),
        _propEntry(PT_UNICODE, 0x3707, _u16(filename)),
        _propEntry(PT_UNICODE, 0x370E, _u16(mimetype)),
    ]
    if cid is not None:
        writer.addEntry(directory + '/__substg1.0_3712001F', _u16(cid))
        entries.append(_propEntry(PT_UNICODE, 0x3712, _u16(cid)))
    writer.addEntry(directory + '/__properties_version1.0',
                    _propertyStream(entries))
    return directory


def _addMessageStreams(writer: OleWriter, base: str, *, classType: str,
                       subject: str, body: str, html: bytes,
                       embedded: bool, header: Tuple[int, int, int, int],
                       headerText: str) -> None:
    prefix = base + '/' if base else ''
    writer.addEntry(prefix + '__substg1.0_001A001F', _u16(classType))
    writer.addEntry(prefix + '__substg1.0_0037001F', _u16(subject))
    writer.addEntry(prefix + '__substg1.0_007D001F', _u16(headerText))
    writer.addEntry(prefix + '__substg1.0_1000001F', _u16(body))
    writer.addEntry(prefix + '__substg1.0_10130102', html)
    entries = [
        _propEntry(PT_LONG, 0x340D, 0x40000),
        _propEntry(PT_LONG, 0x0E07, 0),
        _propEntry(PT_UNICODE, 0x001A, _u16(classType)),
        _propEntry(PT_UNICODE, 0x0037, _u16(subject)),
        _propEntry(PT_UNICODE, 0x007D, _u16(headerText)),
    ]
    writer.addEntry(prefix + '__properties_version1.0',
                    _propertyStream(entries, embedded = embedded, header = header))


def _buildEmbeddedMessage(writer: OleWriter, attachBase: str) -> None:
    """
    Populates the embedded MSG storage of the outer attachment at *attachBase*.
    """
    embedded = attachBase + '/__substg1.0_3701000D'
    writer.addEntry(embedded, storage = True)

    # Outer attachment row describing the embedded message (afEmbeddedMessage).
    writer.addEntry(attachBase + '/__substg1.0_3001001F',
                    _u16('Forwarded original'))
    writer.addEntry(attachBase + '/__substg1.0_3707001F',
                    _u16('forwarded-original.msg'))
    writer.addEntry(attachBase + '/__properties_version1.0', _propertyStream([
        _propEntry(PT_LONG, 0x3705, 5),
        _propEntry(PT_LONG, 0x370B, 0xFFFFFFFF),
        _propEntry(PT_UNICODE, 0x3001, _u16('Forwarded original')),
        _propEntry(PT_UNICODE, 0x3707, _u16('forwarded-original.msg')),
    ]))

    innerHtml = (
        b'<html><body>'
        b'<p>inner html body</p>'
        b'<img src="cid:' + SHARED_CID.encode('ascii') + b'">'
        b'</body></html>'
    )
    innerHeaderText = (
        'From: Inner Sender <inner-sender@example.com>\r\n'
        'To: Inner Recipient <inner@example.com>\r\n'
        'Cc: Inner Copy <inner-copy@example.com>\r\n'
        'Subject: Forwarded original subject\r\n'
        'Message-ID: <inner-msg-id@archive.test>\r\n'
    )
    _addMessageStreams(writer, embedded,
                       classType = 'IPM.Note',
                       subject = 'Forwarded original subject',
                       body = 'inner plain text body\r\n',
                       html = innerHtml,
                       embedded = True,
                       header = (0, 0, 1, 1),
                       headerText = innerHeaderText)

    # Recipients that only exist on the embedded message.
    _addRecipient(writer, embedded, 0, 'Inner Recipient',
                  'inner@example.com', 1)
    _addRecipient(writer, embedded, 1, 'Inner Copy',
                  'inner-copy@example.com', 2)

    # The embedded message keeps its own inline image and regular attachment.
    _addDataAttachment(writer, embedded, 0,
                       filename = 'image001.png',
                       mimetype = 'image/png',
                       data = INNER_IMAGE,
                       cid = SHARED_CID,
                       hidden = True,
                       renderingPosition = 0)
    _addDataAttachment(writer, embedded, 1,
                       filename = 'report.txt',
                       mimetype = 'text/plain',
                       data = INNER_REPORT)


def _buildBytes(*, includeOuterImage: bool) -> bytes:
    writer = OleWriter()
    _addNameIdStorage(writer)

    outerHtml = (
        b'<html><body>'
        b'<p>outer html body</p>'
        b'<img src="cid:' + SHARED_CID.encode('ascii') + b'">'
        b'</body></html>'
    )
    outerHeaderText = (
        'From: Outer Sender <outer-sender@example.com>\r\n'
        'To: Outer Recipient <outer@example.com>\r\n'
        'Subject: Outer archived message\r\n'
        'Message-ID: <outer-msg-id@archive.test>\r\n'
        'X-Archive-Batch: batch-u224\r\n'
    )
    _addMessageStreams(writer, '',
                       classType = 'IPM.Note',
                       subject = 'Outer archived message',
                       body = 'outer plain text body\r\n',
                       html = outerHtml,
                       embedded = False,
                       header = (1, 1, 1, 3 if includeOuterImage else 2),
                       headerText = outerHeaderText)

    _addRecipient(writer, '', 0, 'Outer Recipient',
                  'outer@example.com', 1)

    attachmentIndex = 0
    if includeOuterImage:
        _addDataAttachment(writer, '', attachmentIndex,
                           filename = 'image001.png',
                           mimetype = 'image/png',
                           data = OUTER_IMAGE,
                           cid = SHARED_CID,
                           hidden = True,
                           renderingPosition = 0)
        attachmentIndex += 1

    # A regular scanned document carried only by the outer message.
    _addDataAttachment(writer, '', attachmentIndex,
                       filename = 'scan.pdf',
                       mimetype = 'application/pdf',
                       data = SCAN_BYTES)
    attachmentIndex += 1

    # Same attachment name as one inside the embedded message, other bytes.
    _addDataAttachment(writer, '', attachmentIndex,
                       filename = 'report.txt',
                       mimetype = 'text/plain',
                       data = OUTER_REPORT)
    attachmentIndex += 1

    # Forwarded embedded message.
    embeddedAttach = f'__attach_version1.0_#{attachmentIndex:08X}'
    writer.addEntry(embeddedAttach, storage = True)
    _buildEmbeddedMessage(writer, embeddedAttach)

    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def buildFixtures(directory: Path) -> Dict[str, Path]:
    """
    Writes both MSG fixtures into *directory* and returns their paths.
    """
    directory.mkdir(parents = True, exist_ok = True)
    paths = {
        'complete': directory / 'eml-forwarded-msg.msg',
        'missingImage': directory / 'eml-missing-outer-image.msg',
    }
    paths['complete'].write_bytes(_buildBytes(includeOuterImage = True))
    paths['missingImage'].write_bytes(_buildBytes(includeOuterImage = False))
    return paths
