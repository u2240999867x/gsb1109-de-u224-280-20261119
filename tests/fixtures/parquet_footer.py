import struct

from thrift.protocol import TCompactProtocol
from thrift.transport import TTransport

from parquet_tools.gen_py.parquet.ttypes import FileMetaData


def strip_column_statistics(path, row_group, column_name):
    """Remove statistics for one column of one row group in place.

    Only the thrift footer is rewritten; the data pages stay byte-for-byte
    intact.  This emulates writers that omit statistics for selected groups,
    which forces the reader to decode the group instead of trusting metadata.
    """
    with open(path, 'rb') as handle:
        blob = handle.read()
    footer_size = struct.unpack(b'<i', blob[-8:-4])[0]
    footer_start = len(blob) - footer_size - 8
    prefix = blob[:footer_start]

    transport = TTransport.TMemoryBuffer(blob[footer_start:len(blob) - 8])
    protocol = TCompactProtocol.TCompactProtocol(transport)
    metadata = FileMetaData()
    metadata.read(protocol)

    group = metadata.row_groups[row_group]
    for chunk in group.columns:
        if '.'.join(chunk.meta_data.path_in_schema) == column_name:
            chunk.meta_data.statistics = None

    out_transport = TTransport.TMemoryBuffer()
    out_protocol = TCompactProtocol.TCompactProtocol(out_transport)
    metadata.write(out_protocol)
    new_footer = out_transport.getvalue()

    with open(path, 'wb') as handle:
        handle.write(prefix)
        handle.write(new_footer)
        handle.write(struct.pack(b'<i', len(new_footer)))
        handle.write(b'PAR1')
