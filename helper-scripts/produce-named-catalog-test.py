"""
Generates ``named-property-catalog.msg`` for the named property catalog tests.

The file contains a parent message with one embedded (forwarded) message. Both
messages share the same named property definitions, stored once in the root
``__nameid_version1.0`` folder. Three custom named properties are deliberately
defined so that their textual identifiers collide:

* a numerical named property whose ID renders as the hex string ``"000A"``;
* a string named property literally named ``"000A"`` in the same property set;
* another string named property called ``"000A"`` in a second property set.

The parent carries values for all three. The forwarded message only carries two
of them, and one of those uses a different property type than the parent's
value. This lets the tests prove that the definitions are kept separate, that
ambiguous textual names return candidates, and that a missing value is never
filled in from the other message.

Run this script from the repository root to (re)generate the fixture.
"""
import struct

import extract_msg


# Two custom property sets. Neither collides with the built-in sets.
GUID_1 = '{11111111-1111-1111-1111-111111111111}'
GUID_2 = '{22222222-2222-2222-2222-222222222222}'

# Property type codes used by the values.
PT_I32 = 0x0003
PT_UNICODE = 0x001F
PT_BINARY = 0x0102

# Named property kinds.
KIND_NUMERICAL = 0
KIND_STRING = 1

# Named property index (the "pid" field); value streams are 0x8000 + index.
PID_NUM = 0
PID_STR_1 = 1
PID_STR_2 = 2

STREAM_NUM = f'{0x8000 + PID_NUM:04X}'     # 8000
STREAM_STR_1 = f'{0x8000 + PID_STR_1:04X}'  # 8001
STREAM_STR_2 = f'{0x8000 + PID_STR_2:04X}'  # 8002

NAME_TEXT = '000A'
NAME_OFFSET = 0


def guidBytes(guid: str) -> bytes:
    """
    Converts a ``{XXXXXXXX-...}`` GUID string into the 16 little-endian bytes
    stored in the GUID stream.
    """
    clean = guid.strip('{}')
    d1, d2, d3, d4, d5 = clean.split('-')
    return struct.pack('<IHH', int(d1, 16), int(d2, 16), int(d3, 16)) + bytes.fromhex(d4 + d5)


def nameIdStreams():
    """
    Builds the GUID, entry, and name streams for the shared definitions.
    """
    # GUID indices 0, 1, 2 are reserved for None, PS_MAPI and PS_PUBLIC_STRINGS.
    guidStream = guidBytes(GUID_1) + guidBytes(GUID_2)
    guidIndex1 = 3
    guidIndex2 = 4

    nameBytes = NAME_TEXT.encode('utf-16-le')
    namesStream = struct.pack('<I', len(nameBytes)) + nameBytes

    # Each entry is: name offset/ID (I), GUID index with kind flag (H), pid (H).
    entries = [
        # Numerical property whose ID (0x000A) renders as the text "000A".
        struct.pack('<IHH', 0x000A, guidIndex1 << 1 | KIND_NUMERICAL, PID_NUM),
        # String property literally named "000A" in the first property set.
        struct.pack('<IHH', NAME_OFFSET, guidIndex1 << 1 | KIND_STRING, PID_STR_1),
        # String property named "000A" again, but in the second property set.
        struct.pack('<IHH', NAME_OFFSET, guidIndex2 << 1 | KIND_STRING, PID_STR_2),
    ]
    entryStream = b''.join(entries)

    return guidStream, entryStream, namesStream


def fixedProp(streamID: str, propType: int, value: int) -> bytes:
    """
    Builds a 16 byte fixed length property store entry.
    """
    return struct.pack('<HHI', propType, int(streamID, 16), 0) + struct.pack('<I', value) + b'\x00' * 4


def unicodeProp(streamID: str, text: str) -> bytes:
    """
    Builds a 16 byte variable length (string) property store entry.
    """
    data = text.encode('utf-16-le')
    return struct.pack('<HHI', PT_UNICODE, int(streamID, 16), 0) + struct.pack('<II', len(data), 0)


def binaryProp(streamID: str, length: int) -> bytes:
    """
    Builds a 16 byte variable length (binary) property store entry.
    """
    return struct.pack('<HHI', PT_BINARY, int(streamID, 16), 0) + struct.pack('<II', length, 0)


def propsStream(entries: bytes, embedded: bool) -> bytes:
    """
    Builds a full ``__properties_version1.0`` stream.
    """
    headerSize = 24 if embedded else 32
    header = b'\x00' * headerSize
    return header + entries


def main():
    guidStream, entryStream, namesStream = nameIdStreams()

    writer = extract_msg.OleWriter()

    # Shared named property definition folder.
    writer.addEntry('__nameid_version1.0', storage = True)
    writer.addEntry('__nameid_version1.0/__substg1.0_00020102', guidStream)
    writer.addEntry('__nameid_version1.0/__substg1.0_00030102', entryStream)
    writer.addEntry('__nameid_version1.0/__substg1.0_00040102', namesStream)

    # Parent values: all three properties are present.
    parentProps = b''.join([
        fixedProp(STREAM_NUM, PT_I32, 10),
        unicodeProp(STREAM_STR_1, 'parent-text-one'),
        unicodeProp(STREAM_STR_2, 'parent-text-two'),
    ])
    writer.addEntry('__properties_version1.0', propsStream(parentProps, False))
    writer.addEntry('__substg1.0_001A001F', 'IPM.Note'.encode('utf-16-le'))
    writer.addEntry(f'__substg1.0_{STREAM_STR_1}{PT_UNICODE:04X}',
                    'parent-text-one'.encode('utf-16-le'))
    writer.addEntry(f'__substg1.0_{STREAM_STR_2}{PT_UNICODE:04X}',
                    'parent-text-two'.encode('utf-16-le'))

    # Embedded (forwarded) message storage. It carries only two of the three
    # properties, and the first uses a different type (binary instead of int).
    childPrefix = '__attach_version1.0_#00000000/__substg1.0_3701000D'
    childBinary = b'\xDE\xAD\xBE\xEF'
    childProps = b''.join([
        binaryProp(STREAM_NUM, len(childBinary)),
        unicodeProp(STREAM_STR_1, 'child-text-one'),
    ])
    writer.addEntry('__attach_version1.0_#00000000', storage = True)
    # Attachment properties: the 8 byte reserved region followed by the attach
    # method (PR_ATTACH_METHOD, 0x37050003) set to afEmbeddedMessage (5).
    attachProps = b'\x00' * 8 + struct.pack('<HHI', PT_I32, 0x3705, 0) + struct.pack('<I', 5) + b'\x00' * 4
    writer.addEntry('__attach_version1.0_#00000000/__properties_version1.0', attachProps)
    writer.addEntry(childPrefix, storage = True)
    writer.addEntry(f'{childPrefix}/__properties_version1.0', propsStream(childProps, True))
    writer.addEntry(f'{childPrefix}/__substg1.0_001A001F', 'IPM.Note'.encode('utf-16-le'))
    writer.addEntry(f'{childPrefix}/__substg1.0_{STREAM_NUM}{PT_BINARY:04X}', childBinary)
    writer.addEntry(f'{childPrefix}/__substg1.0_{STREAM_STR_1}{PT_UNICODE:04X}',
                    'child-text-one'.encode('utf-16-le'))

    writer.write('example-msg-files/named-property-catalog.msg')


if __name__ == '__main__':
    main()
