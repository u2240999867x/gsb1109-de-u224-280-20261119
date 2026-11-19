#!/usr/bin/env python

import csv
import json
import os.path
import sys
from itertools import chain

import agate

from csvkit.cli import CSVKitUtility, isatty, make_default_headers
from csvkit.exceptions import CSVStackError

DIALECTS = {
    'colon': {'delimiter': ':'},
    'comma': {'delimiter': ','},
    'excel': {},
    'excel-tab': {'delimiter': '\t'},
    'pipe': {'delimiter': '|'},
    'semicolon': {'delimiter': ';'},
    'space': {'delimiter': ' '},
    'tab': {'delimiter': '\t'},
    'tsv': {'delimiter': '\t'},
}

QUOTING_NAMES = {
    'all': csv.QUOTE_ALL,
    'minimal': csv.QUOTE_MINIMAL,
    'none': csv.QUOTE_NONE,
    'nonnumeric': csv.QUOTE_NONNUMERIC,
}

if hasattr(csv, 'QUOTE_NOTNULL'):
    QUOTING_NAMES['notnull'] = csv.QUOTE_NOTNULL
if hasattr(csv, 'QUOTE_STRINGS'):
    QUOTING_NAMES['strings'] = csv.QUOTE_STRINGS

BOOLEANS = {
    '0': False,
    '1': True,
    'false': False,
    'no': False,
    'off': False,
    'on': True,
    'true': True,
    'yes': True,
}

ESCAPES = {
    '\\\\': '\\',
    '\\"': '"',
    "\\'": "'",
    '\\n': '\n',
    '\\r': '\r',
    '\\t': '\t',
}


def _unescape(value):
    for escaped, character in ESCAPES.items():
        value = value.replace(escaped, character)

    return value


def _parse_boolean(value):
    if not isinstance(value, bool):
        value = str(value).strip().lower()
        if value not in BOOLEANS:
            raise ValueError(f'expected a boolean, got {value!r}')
        return BOOLEANS[value]

    return value


def _split(value, separator=','):
    """
    Split a string on a separator, ignoring separators within quotes.
    """
    parts = []
    part = []
    quote = None
    escaped = False

    for character in value:
        if escaped:
            part.append(character)
            escaped = False
        elif character == '\\':
            part.append(character)
            escaped = True
        elif quote:
            part.append(character)
            if character == quote:
                quote = None
        elif character in '"\'':
            part.append(character)
            quote = character
        elif character == separator:
            parts.append(''.join(part).strip())
            part = []
        else:
            part.append(character)

    parts.append(''.join(part).strip())

    return parts


def _parse_dialect(value):
    """
    Parse a dialect declaration, like ``semicolon``, ``;`` or ``delimiter=; quotechar="'``.
    """
    if isinstance(value, dict):
        declaration = value
    else:
        raw = str(value)
        value = raw.strip()

        if len(raw) == 1 and raw.isspace():
            declaration = {'delimiter': raw}
        elif value.lower() in DIALECTS:
            declaration = dict(DIALECTS[value.lower()])
        elif len(value) == 1:
            declaration = {'delimiter': value}
        elif value == '\\t':
            declaration = {'delimiter': '\t'}
        elif len(value) >= 3 and value[0] == value[-1] and value[0] in '"\'':
            declaration = {'delimiter': _unescape(value[1:-1])}
        elif value.startswith('{'):
            declaration = json.loads(value)
        else:
            declaration = {}

            try:
                for key, item in _parse_options(value):
                    declaration[key] = item
            except ValueError:
                declaration = {}

            if not declaration:
                raise ValueError(
                    f'expected a dialect name, a single delimiter or key=value pairs, got {value!r}')

    return _parse_dialect_options(declaration)


def _parse_options(value):
    """
    Parse a space-separated list of ``key=value`` options, where values can be quoted.
    """
    tokens = []
    token = []
    quote = None
    escaped = False

    for character in value:
        if escaped:
            token.append(character)
            escaped = False
        elif character == '\\':
            token.append(character)
            escaped = True
        elif quote:
            token.append(character)
            if character == quote:
                quote = None
        elif character in '"\'':
            token.append(character)
            quote = character
        elif character.isspace():
            if token:
                tokens.append(''.join(token))
                token = []
        else:
            token.append(character)

    if quote:
        raise ValueError(f'unterminated quote in {value!r}')

    if token:
        tokens.append(''.join(token))

    options = []

    for token in tokens:
        key, separator, item = token.partition('=')

        if not separator:
            raise ValueError(f'expected key=value, got {token!r}')

        key = key.lower().replace('-', '_')
        item = item.strip()

        if len(item) >= 2 and item[0] == item[-1] and item[0] in '"\'':
            item = item[1:-1]

        options.append((key, _unescape(item)))

    return options


def _parse_dialect_options(declaration):
    options = {}

    for key, value in declaration.items():
        key = str(key).lower().replace('-', '_')

        if key in ('delim', 'delimiter', 'd', 'sep', 'separator'):
            value = _unescape(str(value))
            if value.lower() == 'tab':
                value = '\t'
            if len(value) != 1:
                raise ValueError('the delimiter must be a single character')
            options['delimiter'] = value
        elif key in ('quote', 'quotechar', 'q'):
            value = _unescape(str(value))
            if value and len(value) != 1:
                raise ValueError('the quotechar must be a single character')
            options['quotechar'] = value or None
        elif key in ('quoting', 'u'):
            if isinstance(value, str):
                if value.lower() in QUOTING_NAMES:
                    value = QUOTING_NAMES[value.lower()]
                else:
                    value = int(value)
            options['quoting'] = int(value)
        elif key in ('doublequote', 'b'):
            options['doublequote'] = _parse_boolean(value)
        elif key == 'no_doublequote':
            options['doublequote'] = not _parse_boolean(value)
        elif key in ('escape', 'escapechar', 'p'):
            value = _unescape(str(value))
            if value and len(value) != 1:
                raise ValueError('the escapechar must be a single character')
            options['escapechar'] = value or None
        elif key in ('skip_initial_space', 'skipinitialspace', 's'):
            options['skipinitialspace'] = _parse_boolean(value)
        elif key == 'strict':
            options['strict'] = _parse_boolean(value)
        elif key == 'tabs' and _parse_boolean(value):
            options['delimiter'] = '\t'
        else:
            raise ValueError(f'unknown dialect option {key!r}')

    return options


def _split_declaration(value):
    """
    Split a declaration into a filename and a dialect.

    ``FILE=DIALECT`` and ``FILE:DIALECT`` are accepted. A backslash-escaped separator can appear in a filename.
    """
    if not isinstance(value, str):
        raise ValueError('a declaration must be a string')

    separator = None
    for i, character in enumerate(value):
        if i and value[i - 1] == '\\':
            continue
        if character == '=':
            separator = i
            break
    if separator is None:
        for i, character in enumerate(value):
            if i and value[i - 1] == '\\':
                continue
            if character == ':' and not (i == 1 and value[0].isalpha() and value[2:3] in ('\\', '/')):
                separator = i
                break
    if separator is None:
        return None

    filename = value[:separator].replace('\\=', '=').replace('\\:', ':').strip()
    dialect = value[separator + 1:].strip()
    if not filename or not dialect:
        return None

    return filename, dialect


def _read_declaration_file(path):
    with open(path, encoding='utf-8-sig') as f:
        text = f.read()

    try:
        value = json.loads(text)
    except ValueError:
        declarations = []
        lines = [
            (line_number, line.strip())
            for line_number, line in enumerate(text.splitlines(), 1)
            if line.strip() and not line.strip().startswith('#')
        ]

        if lines:
            try:
                cells = next(csv.reader([lines[0][1]]))
            except csv.Error:
                cells = []

            if [cell.strip().lower() for cell in cells[:2]] == ['file', 'dialect']:
                lines = lines[1:]

        for line_number, line in lines:
            declaration = _split_declaration(line)

            if declaration is None:
                try:
                    cells = next(csv.reader([line]))
                except csv.Error:
                    cells = []

                if len(cells) == 2 and all(cells):
                    declaration = (cells[0].strip(), cells[1].strip())

            if declaration is None:
                raise CSVStackError(f'{path}: line {line_number}: expected "FILE=DIALECT"')

            declarations.append(declaration)

        return declarations

    if isinstance(value, dict):
        return list(value.items())

    if isinstance(value, list):
        declarations = []
        for item in value:
            if isinstance(item, str):
                declaration = _split_declaration(item)
                if declaration is None:
                    raise CSVStackError(f'{path}: expected "FILE=DIALECT", got {item!r}')
                declarations.append(declaration)
            elif isinstance(item, dict) and 'file' in item and 'dialect' in item:
                declarations.append((item['file'], item['dialect']))
            else:
                raise CSVStackError(f'{path}: expected an object with "file" and "dialect", got {item!r}')
        return declarations

    raise CSVStackError(f'{path}: expected a JSON object mapping files to dialects')


def _skip_lines(f, args):
    if isinstance(args.skip_lines, int):
        skip_lines = args.skip_lines
        while skip_lines > 0:
            f.readline()
            skip_lines -= 1
    else:
        raise ValueError('skip_lines argument must be an int')

    return args.skip_lines


class _InputReader:
    """
    Wrap a CSV reader to add the input filename and source line to parse errors.

    The CSV module reports the line on which it detected an error, which is often the row before an unterminated
    quoted field. So, include the line on which the failing row started and, if different, the line on which the
    CSV module detected the error.

    If ``validate`` is true, rows must also have the same number of columns as the header row, so that a row broken
    by an unexpected line break is rejected instead of being silently stacked.
    """
    def __init__(self, reader, path, line_offset=0, validate=False):
        self.reader = reader
        self.path = path
        self.line_offset = line_offset
        self.validate = validate
        self.width = None

    def __iter__(self):
        return self

    def __next__(self):
        start_line = self._start_line()

        try:
            row = next(self.reader)
        except csv.Error as e:
            raise CSVStackError(self._message(start_line, e)) from None

        if self.validate:
            self._check_width(row, start_line)

        return row

    @property
    def fieldnames(self):
        start_line = self._start_line()

        try:
            fieldnames = self.reader.fieldnames
        except csv.Error as e:
            raise CSVStackError(self._message(start_line, e)) from None

        if self.validate:
            self.width = len(fieldnames)

        return fieldnames

    @property
    def line_num(self):
        return self.reader.line_num

    def _start_line(self):
        if isinstance(self.reader, agate.csv.DictReader) and self.reader.line_num == 0:
            self.reader.fieldnames

        return self.reader.line_num + 1 + self.line_offset

    def _check_width(self, row, start_line):
        if isinstance(row, dict):
            if self.width is None:
                self.width = len(row)

            if None in row:
                found = self.width + len(row[None])
            else:
                found = sum(value is not None for value in row.values())
        else:
            if self.width is None:
                self.width = len(row)

            found = len(row)

        if found != self.width:
            raise CSVStackError(
                f'{self.path}: line {start_line}: expected {self.width} columns, found {found}')

    def _message(self, start_line, error):
        try:
            detected_line = self.reader.line_num + self.line_offset
        except AttributeError:
            detected_line = start_line

        message = f'{self.path}: line {start_line}: {error}'

        if detected_line > start_line:
            message += f' (reached line {detected_line})'

        return message


class CSVStack(CSVKitUtility):
    description = 'Stack up the rows from multiple CSV files, optionally adding a grouping value.'
    # Override 'f' because the utility accepts multiple files.
    override_flags = ['f', 'L', 'I']

    def add_arguments(self):
        self.argparser.add_argument(
            metavar='FILE', nargs='*', dest='input_paths', default=['-'],
            help='The CSV file(s) to operate on. If omitted, will accept input as piped data via STDIN.')
        self.argparser.add_argument(
            '-g', '--groups', dest='groups',
            help='A comma-separated list of values to add as "grouping factors", one per CSV being stacked. These are '
                 'added to the output as a new column. You may specify a name for the new column using the -n flag.')
        self.argparser.add_argument(
            '-n', '--group-name', dest='group_name',
            help='A name for the grouping column, e.g. "year". Only used when also specifying -g.')
        self.argparser.add_argument(
            '--filenames', dest='group_by_filenames', action='store_true',
            help='Use the filename of each input file as its grouping value. When specified, -g will be ignored.')
        self.argparser.add_argument(
            '--dialect', '--dialects', '--delimiters', '--input-dialect', '--input-dialects',
            dest='dialects', action='append', default=[], metavar='FILE=DIALECT',
            help='Specify the dialect of one input file, as FILE=DIALECT. Repeat for each input. The dialect can be a '
                 'name (semicolon, tab, comma, pipe), a single delimiter, or key=value options (delimiter, quotechar, '
                 'quoting, doublequote, escapechar, skipinitialspace, strict). A comma-separated list of dialects is '
                 'applied to the inputs in order. Inputs not specified use the common input options. Output always '
                 "uses csvkit's default format.")
        self.argparser.add_argument(
            '--dialect-file', '--dialects-file', '--dialect-map', '--dialect-map-file',
            dest='dialect_files', action='append', default=[], metavar='FILE',
            help='Read FILE=DIALECT declarations from a file. Each line can declare one input, or a JSON object can '
                 'map filenames to dialects. Can be specified multiple times.')

    def main(self):
        if isatty(sys.stdin) and self.args.input_paths == ['-']:
            sys.stderr.write('No input file or piped data provided. Waiting for standard input:\n')

        has_groups = self.args.groups is not None or self.args.group_by_filenames

        if self.args.groups is not None and not self.args.group_by_filenames:
            groups = self.args.groups.split(',')

            if len(groups) != len(self.args.input_paths):
                self.argparser.error(
                    'The number of grouping values must be equal to the number of CSV files being stacked.')
        else:
            groups = None

        group_name = self.args.group_name if self.args.group_name else 'group'
        use_fieldnames = not self.args.no_header_row
        dialects = self._parse_dialects()

        if use_fieldnames:
            Reader = agate.csv.DictReader
        else:
            Reader = agate.csv.reader

        try:
            headers, kept = self._read_headers(Reader, dialects, use_fieldnames)
        except CSVStackError as e:
            self._error(e)

        if has_groups:
            headers.insert(0, group_name)

        if use_fieldnames:
            output = agate.csv.DictWriter(self.output_file, fieldnames=headers, **self.writer_kwargs)
            output.writeheader()
        else:
            output = agate.csv.writer(self.output_file, **self.writer_kwargs)
            output.writerow(headers)

        try:
            for i, path in enumerate(self.args.input_paths):
                if i in kept:
                    f, rows = kept[i]
                else:
                    f, rows = self._open_reader(path, Reader, dialects, i)

                if has_groups:
                    if groups:
                        group = groups[i]
                    else:
                        group = os.path.basename(f.name)

                for row in rows:
                    if has_groups:
                        if use_fieldnames:
                            row[group_name] = group
                        else:
                            row.insert(0, group)

                    output.writerow(row)

                f.close()
        except CSVStackError as e:
            self._error(e)

    def _read_headers(self, Reader, dialects, use_fieldnames):
        headers = []
        # Inputs that can't be reopened (standard input and pipes), as (file, reader) after any header row.
        kept = {}

        for i, path in enumerate(self.args.input_paths):
            f, rows = self._open_reader(path, Reader, dialects, i)

            if use_fieldnames:
                if rows.fieldnames:
                    for field in rows.fieldnames:
                        if field not in headers:
                            headers.append(field)
            else:
                row = next(rows, [])
                headers = list(make_default_headers(len(row)))
                if row:
                    rows = chain([row], rows)

            if path == '-' or not os.path.isfile(path):
                kept[i] = (f, rows)
            else:
                f.close()

            # If we aren't using header rows, we only look at the first file and stack columns in the same order.
            if not use_fieldnames:
                break

        return headers, kept

    def _open_reader(self, path, Reader, dialects, i):
        f = self._open_input_file(path)
        skipped = _skip_lines(f, self.args)

        reader_kwargs = self.reader_kwargs
        dialect = self._dialect_for(path, i, dialects)
        validate = dialect is not None
        if dialect is not None:
            reader_kwargs = dict(reader_kwargs)
            reader_kwargs.update(dialect)
            reader_kwargs.setdefault('strict', True)

        rows = Reader(f, **reader_kwargs)
        return f, _InputReader(rows, self._display_path(path, f), skipped, validate)

    def _dialect_for(self, path, i, dialects):
        for key in (path, os.path.basename(path), str(i + 1)):
            if key in dialects:
                return dialects[key]
        return None

    def _display_path(self, path, f):
        if path == '-':
            return 'standard input'
        return getattr(f, 'name', path)

    def _parse_dialects(self):
        declarations = []

        for value in self.args.dialects:
            declarations.extend(self._parse_declaration_values(value))

        for path in self.args.dialect_files:
            declarations.extend(self._parse_declaration_file(path))

        dialects = {}

        for declaration in declarations:
            if isinstance(declaration, dict):
                for filename, dialect in declaration.items():
                    dialects[str(filename)] = self._parse_dialect(str(filename), dialect)
            else:
                filename, dialect = declaration
                dialects[str(filename)] = self._parse_dialect(str(filename), dialect)

        return dialects

    def _parse_declaration_values(self, value):
        declarations = []

        for part in _split(value):
            if os.path.isfile(part) and not _split_declaration(part):
                declarations.extend(self._parse_declaration_file(part))
                continue

            declaration = _split_declaration(part)
            if declaration is None:
                dialects = _split(value)

                if len(dialects) == len(self.args.input_paths) and all(self._is_dialect(item) for item in dialects):
                    return list(enumerate(dialects, 1))

                self.argparser.error(f'Invalid dialect declaration: {part!r}. Expected FILE=DIALECT.')

            declarations.append(declaration)

        return declarations

    def _is_dialect(self, value):
        try:
            _parse_dialect(value)
        except (TypeError, ValueError):
            return False
        return True

    def _parse_declaration_file(self, path):
        try:
            return _read_declaration_file(path)
        except OSError as e:
            self.argparser.error(f'Unable to read dialect file {path!r}: {e}.')
        except CSVStackError as e:
            self._error(e)

    def _parse_dialect(self, filename, value):
        try:
            return _parse_dialect(value)
        except (TypeError, ValueError) as e:
            self.argparser.error(f'Invalid dialect for {filename!r}: {e}.')

    def _error(self, error):
        self.error_file.write(f'{error}\n')
        raise SystemExit(1)


def launch_new_instance():
    utility = CSVStack()
    utility.run()


if __name__ == '__main__':
    launch_new_instance()
