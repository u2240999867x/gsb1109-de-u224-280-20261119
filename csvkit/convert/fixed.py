#!/usr/bin/env python

import codecs
from collections import namedtuple
from io import BufferedIOBase, RawIOBase, StringIO

import agate

from csvkit.exceptions import FieldBoundaryError


def fixed2csv(f, schema, output=None, skip_lines=0, **kwargs):
    """
    Convert a fixed-width file to csv using a CSV-formatted schema description.

    A schema CSV must start with a header row with (at least) columns labeled
    "column","start", and "length". (Other columns will be ignored.) For each
    subsequent row, therefore, those columns will be used to identify a column
    name, the starting index of the column (an integer), and the length of the
    column (also an integer).

    Values in the 'start' column are assumed to be zero-based, unless the first
    value for 'start' is 1, in which case all values are assumed to be
    one-based.

    If output is specified, rows will be written to that object, otherwise the
    complete data will be returned.

    :param skip_lines:
        The number of lines to skip from the top of the file.
    :param positions:
        Whether the schema's "start" and "length" values index "characters"
        (the default) or "bytes". Byte positions require an "encoding" and a
        binary input file, and a :class:`FieldBoundaryError` is raised if a
        column boundary splits a multibyte character.
    """
    streaming = bool(output)

    if not streaming:
        output = StringIO()

    encoding = kwargs.get('encoding')
    positions = kwargs.get('positions', 'characters')

    writer = agate.csv.writer(output)

    reader = FixedWidthReader(
        f, schema, encoding=encoding, positions=positions, skip_lines=skip_lines)
    writer.writerows(reader)

    if not streaming:
        data = output.getvalue()
        output.close()

        return data

    # Return empty string when streaming
    return ''


class FixedWidthReader:
    """
    Given a fixed-width file and a schema file, produce an analog to a csv
    reader that yields a row of strings for each line in the fixed-width file,
    preceded with a row of headers as provided in the schema.  (This might be
    problematic if fixed-width-files ever have header rows also, but I haven't
    seen that.)

    The schema_file should be in CSV format with a header row which has columns
    'column', 'start', and 'length'. (Other columns will be ignored.)  Values
    in the 'start' column are assumed to be "zero-based" unless the first value
    is "1" in which case all values are assumed to be "one-based."

    When ``positions`` is "characters" (the default), the schema positions are
    character offsets in the decoded text. When it is "bytes", they are byte
    offsets in the raw input, which requires a binary file and an encoding.
    """

    def __init__(self, f, schema, encoding=None, positions='characters', skip_lines=0):
        if not isinstance(skip_lines, int):
            raise ValueError('skip_lines argument must be an int')

        self.parser = FixedWidthRowParser(schema)
        self.positions = positions
        self.encoding = encoding
        self.header = True
        self.line_number = 0

        if positions == 'bytes':
            if encoding is None:
                raise ValueError('an encoding is required when positions is "bytes"')
            self.file = None
            self.binary = _as_binary(f)
            for _ in range(skip_lines):
                self.binary.readline()
        elif positions == 'characters':
            self.binary = None
            if encoding is not None:
                f = codecs.iterdecode(f, encoding)
            for _ in range(skip_lines):
                f.readline()
            self.file = f
        else:
            raise ValueError('positions must be "characters" or "bytes"')

    def __iter__(self):
        return self

    def __next__(self):
        if self.header:
            self.header = False
            return self.parser.headers

        if self.positions == 'bytes':
            return self._next_byte_row()

        return self.parser.parse(next(self.file))

    def _next_byte_row(self):
        line = self.binary.readline()

        if not line:
            raise StopIteration

        self.line_number += 1

        line_ending = b''
        for ending in (b'\r\n', b'\n', b'\r'):
            if line.endswith(ending):
                line_ending = ending
                break

        data = line[:len(line) - len(line_ending)] if line_ending else line

        # A BOM is not part of the column data, so strip it before mapping the
        # schema's byte positions onto the remaining payload.
        codec = self.encoding
        if codec.endswith('-sig') and data.startswith(codecs.BOM_UTF8):
            data = data[len(codecs.BOM_UTF8):]
            codec = codec[:-len('-sig')]

        # Decode the whole line first, then map every character boundary to its
        # byte offset. Validating field boundaries against this map catches a
        # split at either edge of a column, including cases where the orphaned
        # bytes would themselves decode to a different, valid character.
        text = data.decode(codec)
        boundaries = self._character_boundaries(text, codec)

        values = []
        line_length = len(data)
        for field in self.parser.fields:
            start = field.start
            end = min(start + field.length, line_length)

            if start > line_length:
                values.append('')
            elif start not in boundaries or end not in boundaries:
                raise FieldBoundaryError(
                    self.line_number, field, line, self.encoding)
            else:
                values.append(text[boundaries[start]:boundaries[end]].strip())

        return values

    def _character_boundaries(self, text, codec):
        boundaries = {0: 0}
        byte_offset = 0

        for char_index, char in enumerate(text):
            byte_offset += len(char.encode(codec))
            boundaries[byte_offset] = char_index + 1

        return boundaries


def _as_binary(f):
    buffer = getattr(f, 'buffer', None)

    if buffer is not None:
        return buffer

    if isinstance(f, (RawIOBase, BufferedIOBase)):
        return f

    raise ValueError('a binary input file is required to read byte positions')


FixedWidthField = namedtuple('FixedWidthField', ['name', 'start', 'length'])


class FixedWidthRowParser:
    """
    Instantiated with a schema, able to return a sequence of trimmed strings
    representing fields given a fixed-length line. Flexible about where the
    columns are, as long as they are headed with the literal names 'column',
    'start', and 'length'.
    """

    def __init__(self, schema):
        self.fields = []  # A list of FixedWidthFields

        schema_reader = agate.csv.reader(schema)
        schema_decoder = SchemaDecoder(next(schema_reader))

        for i, row in enumerate(schema_reader):
            try:
                self.fields.append(schema_decoder(row))
            except Exception as e:
                raise ValueError("Error reading schema at line %i: %s" % (i + 2, e))

    def parse(self, line):
        values = []

        for field in self.fields:
            values.append(line[field.start:field.start + field.length].strip())

        return values

    def parse_dict(self, line):
        """
        Convenience method returns a dict. Equivalent to
        ``dict(zip(self.headers,self.parse(line)))``.
        """
        return dict(zip(self.headers, self.parse(line)))

    @property
    def headers(self):
        return [field.name for field in self.fields]


class SchemaDecoder:
    """
    Extracts column, start, and length columns from schema rows. Once
    instantiated, each time the instance is called with a row, a
    ``(column,start,length)`` tuple will be returned based on values in that
    row and the constructor kwargs.
    """
    REQUIRED_COLUMNS = [('column', None), ('start', int), ('length', int)]

    start = None
    length = None
    column = None
    one_based = None

    def __init__(self, header):
        """
        Constructs a schema row decoder.
        """
        for p, val_type in self.REQUIRED_COLUMNS:
            try:
                if val_type:
                    setattr(self, p, val_type(header.index(p)))
                else:
                    setattr(self, p, header.index(p))
            except ValueError:
                raise ValueError(f'A column named "{p}" must exist in the schema file.')

    def __call__(self, row):
        """
        Return a tuple (column, start, length) based on this instance's
        parameters. If the first time this is called, the row's 'start'
        value is 1, then all 'start' values including the first will be one
        less than in the actual input data, to adjust for one-based
        specifications.  Values for 'start' and 'length' will be cast to
        integers.
        """
        if self.one_based is None:
            self.one_based = int(row[self.start]) == 1

        if self.one_based:
            adjusted_start = int(row[self.start]) - 1
        else:
            adjusted_start = int(row[self.start])

        return FixedWidthField(row[self.column], adjusted_start, int(row[self.length]))
