"""Render parquet inspect reports from footer bytes.

The helpers in this module deliberately never open data pages.  A plaintext
parquet file can be viewed as ``PAR1 | data pages | footer | footer_size | PAR1``
and every report produced by ``parquet-tools inspect`` is derived from the
footer only.  Building a small synthetic file with the same footer lets
pyarrow/thrift produce exactly the same report as a local full-file inspect
without downloading the object.
"""

import io
import struct
import sys

import pyarrow.parquet as pq

from .footer import scan_footer
from .reader import parse_filemetadata


def dedent(text: str) -> str:
    return '\n'.join(map(lambda x: x.strip(), text.split('\n')))


def simple_meta_expression(file_meta, serialized_size=None) -> str:
    size = file_meta.serialized_size if serialized_size is None else serialized_size
    return dedent(f'''
    ############ file meta data ############
    created_by: {file_meta.created_by}
    num_columns: {file_meta.num_columns}
    num_rows: {file_meta.num_rows}
    num_row_groups: {file_meta.num_row_groups}
    format_version: {file_meta.format_version}
    serialized_size: {size}
    ''')


def simple_schema_expression(file_meta, schema) -> str:
    columns = schema.names
    columns_exp = '\n'.join(columns)

    exp = dedent(f'''
    ############ Columns ############
    {columns_exp}
    ''')
    for i, column in enumerate(columns):
        col = schema.column(i)
        col_meta = file_meta.row_group(0).column(i)
        if col_meta.total_uncompressed_size:
            col_compression_space_saving_ratio = 1 - (col_meta.total_compressed_size / col_meta.total_uncompressed_size)
            col_compression_space_saving_pct = col_compression_space_saving_ratio * 100
            col_compression_space_saving_pct_str = f"{col_compression_space_saving_pct:.0f}%"
        else:
            col_compression_space_saving_pct_str = 'N/A'
        col_compression = f"{col_meta.compression} (space_saved: {col_compression_space_saving_pct_str})"
        exp += dedent(f'''
        ############ Column({column}) ############
        name: {col.name}
        path: {col.path}
        max_definition_level: {col.max_definition_level}
        max_repetition_level: {col.max_repetition_level}
        physical_type: {col.physical_type}
        logical_type: {col.logical_type}
        converted_type (legacy): {col.converted_type}
        compression: {col_compression}
        ''')

    return exp


def obj_to_string(obj, toatty: bool, level: int = 1) -> str:
    from colorama import Fore, Style

    color = [
        Fore.RED,
        Fore.YELLOW,
        Fore.GREEN,
        Fore.BLUE
    ]

    extra = ''
    for i in range(level):
        extra += color[i % 4] + '■■■■' + Style.RESET_ALL if toatty else '    '
    ret = ''
    if isinstance(obj, str) or isinstance(obj, int) or isinstance(obj, bytes):
        ret += str(obj)
    else:
        ret += str(obj.__class__.__name__)
        ret += '\n'
        if isinstance(obj, list):
            for e in obj:
                add = extra + obj_to_string(e, toatty, level + 1)
                if add:
                    ret += add + '\n'
            ret = ret[:-1]
        else:
            for item in obj.__dict__:
                if obj.__dict__[item]:
                    ret += extra
                    ret += str(item) + ' = ' + obj_to_string(obj.__dict__[item], toatty, level + 1)
                    ret += '\n'
            ret = ret[:-1]
    return ret


def synthetic_file(footer_bytes: bytes) -> io.BytesIO:
    """Return a parquet-shaped stream containing only magic and footer."""
    return io.BytesIO(
        b'PAR1'
        + footer_bytes
        + struct.pack('<i', len(footer_bytes))
        + b'PAR1'
    )


def render_simple(footer_prefix: bytes, footer_length: int) -> str:
    """Render the simple report from a footer prefix.

    ``footer_prefix`` must extend at least through the ``created_by`` field
    (or through ``row_groups`` when the writer omitted it).  A STOP byte is
    appended to terminate the struct; ``serialized_size`` is reported from the
    original footer length so a partial read still matches a full inspect.
    """
    layout = scan_footer(footer_prefix, footer_length)
    if not layout.have_simple_fields:
        raise ValueError('footer prefix does not contain parquet file metadata')
    end = layout.prefix_end
    body = footer_prefix[:end] + b'\x00'
    pq_file = pq.ParquetFile(synthetic_file(body))
    return (
        simple_meta_expression(pq_file.metadata, serialized_size=footer_length)
        + simple_schema_expression(pq_file.metadata, pq_file.schema)
    )


def render_detail(footer_bytes: bytes, toatty: bool = None) -> str:
    """Render the detailed report from a complete footer."""
    if toatty is None:
        toatty = sys.stdout.isatty()
    return obj_to_string(parse_filemetadata(footer_bytes), toatty)


def simple_prefix_length(footer_bytes: bytes) -> int:
    """Number of leading footer bytes required for :func:`render_simple`."""
    layout = scan_footer(footer_bytes, len(footer_bytes))
    if not layout.have_simple_fields:
        return len(footer_bytes)
    return layout.prefix_end


def has_detail_prefix(footer_bytes: bytes) -> bool:
    """Whether the prefix already contains every non-optional top-level field."""
    layout = scan_footer(footer_bytes, len(footer_bytes))
    return layout.complete


def render_simple_from_file(file_obj) -> str:
    pq_file = pq.ParquetFile(file_obj)
    file_meta = pq_file.metadata
    return (
        simple_meta_expression(file_meta)
        + simple_schema_expression(file_meta, pq_file.schema)
    )


def render_detail_from_file(file_obj) -> str:
    return obj_to_string(get_filemetadata_for(file_obj), sys.stdout.isatty())


def get_filemetadata_for(file_obj):
    if hasattr(file_obj, 'read'):
        from .reader import _read_footer
        return _read_footer(file_obj)
    from .reader import get_filemetadata
    return get_filemetadata(file_obj)


def unavailable_detail_text(reason: str, identity: dict = None) -> str:
    lines = [
        '############ detail unavailable ############',
        f'reason: {reason}',
    ]
    if identity:
        lines.append(f'original_size: {identity.get("size")}')
        lines.append(f'original_etag: {identity.get("etag")}')
        lines.append(f'original_version_id: {identity.get("version_id")}')
        lines.append(f'original_footer_length: {identity.get("footer_length")}')
    return '\n'.join(lines)
