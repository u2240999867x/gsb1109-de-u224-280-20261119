#!/usr/bin/env python

import functools
import sys
from dataclasses import dataclass
from io import BytesIO
from os.path import splitext

import agate
import agatedbf  # noqa: F401
import agateexcel  # noqa: F401
import openpyxl
import xlrd

from csvkit import convert
from csvkit.cli import CSVKitUtility
from csvkit.convert.fixed import fixed2csv
from csvkit.convert.geojs import geojson2csv

SUPPORTED_FORMATS = ['csv', 'dbf', 'fixed', 'geojson', 'json', 'ndjson', 'xls', 'xlsx']

# The string with which to join the values of a multi-row header.
HEADER_SEPARATOR = ' '


@dataclass(frozen=True)
class MergedRange:
    min_row: int
    min_col: int
    max_row: int
    max_col: int

    @property
    def spans_columns(self):
        return self.max_col > self.min_col


def cell_value(rows, row, column):
    """
    Return the value of the 1-based cell in a list of header rows.
    """
    if 1 <= row <= len(rows) and 1 <= column <= len(rows[row - 1]):
        return rows[row - 1][column - 1]
    return None


def combine_header_rows(rows, ranges=()):
    """
    Combine one or more header rows into column names.

    A merged cell's value applies to every cell in its range. Unmerged blank
    cells stay blank, so they don't inherit a preceding group name.
    """
    width = max((len(row) for row in rows), default=0)
    expanded = [list(row) + [None] * (width - len(row)) for row in rows]

    for cell_range in ranges:
        value = cell_value(rows, cell_range.min_row, cell_range.min_col)

        if value is None or str(value).strip() == '':
            continue

        for row in range(max(cell_range.min_row, 1), min(cell_range.max_row, len(expanded)) + 1):
            for column in range(max(cell_range.min_col, 1), min(cell_range.max_col, width) + 1):
                expanded[row - 1][column - 1] = value

    names = []

    for column in range(width):
        parts = []

        for row in expanded:
            value = row[column]

            if value is None or str(value).strip() == '':
                continue

            value = str(value).strip()

            if not parts or parts[-1] != value:
                parts.append(value)

        names.append(HEADER_SEPARATOR.join(parts))

    return names


def group_header_rows(rows, ranges=()):
    """
    Return the number of rows to use as a header.

    The first two rows are treated as a header if the first row has a merged
    group name and the second row names the fields within that group.
    """
    if len(rows) < 2:
        return 1

    for cell_range in ranges:
        if cell_range.min_row != 1 or cell_range.max_row != 1 or not cell_range.spans_columns:
            continue

        values = [
            cell_value(rows, 2, column)
            for column in range(cell_range.min_col, cell_range.max_col + 1)
        ]
        values = [value for value in values if value is not None and str(value).strip()]

        if values:
            return 2

    return 1


def xlsx_header(path, sheet_name, skip_lines=0, header_rows=0):
    """
    Return the number of header rows and the names of the columns in an XLSX
    worksheet.
    """
    workbook = openpyxl.load_workbook(path, read_only=False, data_only=True)

    try:
        if isinstance(sheet_name, str):
            worksheet = workbook[sheet_name]
        elif isinstance(sheet_name, int):
            worksheet = workbook.worksheets[sheet_name]
        else:
            worksheet = workbook.active

        first_row = skip_lines + 1
        ranges = [
            MergedRange(
                cell_range.min_row - first_row + 1,
                cell_range.min_col,
                cell_range.max_row - first_row + 1,
                cell_range.max_col,
            )
            for cell_range in worksheet.merged_cells.ranges
            if cell_range.min_row <= first_row and cell_range.max_row >= first_row
        ]

        rows = [
            list(row)
            for row in worksheet.iter_rows(
                min_row=first_row,
                max_row=first_row + (header_rows or 2) - 1,
                max_col=worksheet.max_column,
                values_only=True,
            )
        ]
    finally:
        workbook.close()

    if not header_rows:
        header_rows = group_header_rows(rows, ranges)

    return header_rows, combine_header_rows(rows[:header_rows], ranges)


def xls_header(path, sheet_name, skip_lines=0, header_rows=0, encoding_override=None):
    """
    Return the number of header rows and the names of the columns in an XLS
    worksheet.
    """
    contents = path.read() if hasattr(path, 'read') else None
    workbook = xlrd.open_workbook(
        file_contents=contents, encoding_override=encoding_override, formatting_info=True
    )

    try:
        if isinstance(sheet_name, str):
            worksheet = workbook.sheet_by_name(sheet_name)
        elif isinstance(sheet_name, int):
            worksheet = workbook.sheet_by_index(sheet_name)
        else:
            worksheet = workbook.sheet_by_index(0)

        first_row = skip_lines
        ranges = [
            MergedRange(
                min_row - first_row + 1,
                min_col + 1,
                max_row - first_row,
                max_col,
            )
            for min_row, max_row, min_col, max_col in worksheet.merged_cells
            if min_row <= first_row < max_row
        ]

        rows = [
            worksheet.row_values(row, 0, worksheet.ncols)
            for row in range(first_row, min(first_row + (header_rows or 2), worksheet.nrows))
        ]
    finally:
        if hasattr(workbook, 'release_resources'):
            workbook.release_resources()

    if not header_rows:
        header_rows = group_header_rows(rows, ranges)

    return header_rows, combine_header_rows(rows[:header_rows], ranges)


class In2CSV(CSVKitUtility):
    description = 'Convert common, but less awesome, tabular data formats to CSV.'
    epilog = 'Some command-line flags only pertain to specific input formats.'
    # The utility handles the input file.
    override_flags = ['f']

    def add_arguments(self):
        self.argparser.add_argument(
            metavar='FILE', nargs='?', dest='input_path',
            help='The CSV file to operate on. If omitted, will accept input as piped data via STDIN.')
        self.argparser.add_argument(
            '-f', '--format', dest='filetype', choices=SUPPORTED_FORMATS,
            help='The format of the input file. If not specified will be inferred from the file type.')
        self.argparser.add_argument(
            '-s', '--schema', dest='schema',
            help='Specify a CSV-formatted schema file for converting fixed-width files. See web documentation.')
        self.argparser.add_argument(
            '-k', '--key', dest='key',
            help='Specify a top-level key to look within for a list of objects to be converted when processing JSON.')
        self.argparser.add_argument(
            '-n', '--names', dest='names_only', action='store_true',
            help='Display sheet names from the input Excel file.')
        self.argparser.add_argument(
            '--sheet', dest='sheet',
            help='The name of the Excel sheet to operate on.')
        self.argparser.add_argument(
            '--write-sheets', dest='write_sheets',
            help='The names of the Excel sheets to write to files, or "-" to write all sheets.')
        self.argparser.add_argument(
            '--use-sheet-names', dest='use_sheet_names', action='store_true',
            help='Use the sheet names as file names when --write-sheets is set.')
        self.argparser.add_argument(
            '--reset-dimensions', dest='reset_dimensions', action='store_true', default=None,
            help='Ignore the sheet dimensions provided by the XLSX file.')
        self.argparser.add_argument(
            '--header-rows', dest='header_rows', type=int,
            help='The number of initial rows to use as the column header of an Excel sheet. By default, two rows '
                 'are combined if the first row contains merged group cells.')
        self.argparser.add_argument(
            '--encoding-xls', dest='encoding_xls',
            help='Specify the encoding of the input XLS file.')
        self.argparser.add_argument(
            '-y', '--snifflimit', dest='sniff_limit', type=int, default=1024,
            help='Limit CSV dialect sniffing to the specified number of bytes. '
                 'Specify "0" to disable sniffing entirely, or "-1" to sniff the entire file.')
        self.argparser.add_argument(
            '-I', '--no-inference', dest='no_inference', action='store_true',
            help='Disable type inference (and --locale, --date-format, --datetime-format, --no-leading-zeroes) '
                 'when parsing CSV input.')

    # This is called only from open_excel_input_file(), but is a separate method to use caching.
    @functools.lru_cache
    def stdin(self):
        return sys.stdin.buffer.read()

    def open_excel_input_file(self, path):
        if not path or path == '-':
            return BytesIO(self.stdin())
        return open(path, 'rb')

    def excel_table(self, filetype, data, sheet, kwargs):
        """
        Convert an Excel sheet to an agate table.

        The first two rows are used as a multi-row header if the first row
        contains merged group cells and the second row contains field names.
        """
        header_kwargs = dict(kwargs)
        skip_lines = header_kwargs.pop('skip_lines', 0)

        if self.args.no_header_row:
            header_kwargs['header'] = False
            header_kwargs['skip_lines'] = skip_lines
        else:
            header_kwargs['header'] = False

            if filetype == 'xls':
                header_rows, column_names = xls_header(
                    BytesIO(data), sheet, skip_lines, self.args.header_rows, self.args.encoding_xls
                )
            else:
                header_rows, column_names = xlsx_header(
                    BytesIO(data), sheet, skip_lines, self.args.header_rows
                )

            header_kwargs['column_names'] = column_names
            header_kwargs['skip_lines'] = skip_lines + header_rows

        if filetype == 'xls':
            return agate.Table.from_xls(
                BytesIO(data), sheet=sheet, encoding_override=self.args.encoding_xls, **header_kwargs
            )
        return agate.Table.from_xlsx(
            BytesIO(data), sheet=sheet, reset_dimensions=self.args.reset_dimensions, **header_kwargs
        )

    def sheet_names(self, path, filetype):
        input_file = self.open_excel_input_file(path)
        if filetype == 'xls':
            sheet_names = xlrd.open_workbook(file_contents=input_file.read()).sheet_names()
        else:  # 'xlsx'
            sheet_names = openpyxl.load_workbook(input_file, read_only=True, data_only=True).sheetnames
        input_file.close()
        return sheet_names

    def main(self):
        path = self.args.input_path

        # Determine the file type.
        if self.args.filetype:
            filetype = self.args.filetype
        elif self.args.schema:
            filetype = 'fixed'
        elif self.args.key:
            filetype = 'json'
        else:
            if not path or path == '-':
                self.argparser.error('You must specify a format when providing input as piped data via STDIN.')
            filetype = convert.guess_format(path)
            if not filetype:
                self.argparser.error('Unable to automatically determine the format of the input file. Try specifying '
                                     'a format with --format.')

        if self.args.names_only:
            if filetype in ('xls', 'xlsx'):
                sheets = self.sheet_names(path, filetype)
                for sheet in sheets:
                    self.output_file.write(f'{sheet}\n')
            else:
                self.argparser.error('You cannot use the -n or --names options with non-Excel files.')
            return

        # Set the input file.
        if filetype in ('xls', 'xlsx'):
            self.input_file = self.open_excel_input_file(path)
        else:
            self.input_file = self._open_input_file(path)

        # Set the reader's arguments.
        kwargs = {}
        sniff_limit = self.args.sniff_limit if self.args.sniff_limit != -1 else None

        if self.args.schema:
            schema = self._open_input_file(self.args.schema)
        elif filetype == 'fixed':
            raise ValueError('schema must not be null when format is "fixed"')

        if filetype == 'csv':
            kwargs.update(self.reader_kwargs)
            kwargs['sniff_limit'] = sniff_limit

        if filetype in ('xls', 'xlsx'):
            kwargs['header'] = not self.args.no_header_row

        if filetype not in ('dbf', 'geojson', 'json', 'ndjson'):  # csv, fixed, xls, xlsx
            kwargs['skip_lines'] = self.args.skip_lines

        if filetype != 'dbf':
            kwargs['column_types'] = self.get_column_types()

        excel_data = self.input_file.read() if filetype in ('xls', 'xlsx') else None

        # Convert the file.
        if (
            filetype == 'csv'
            and self.args.no_inference
            and not self.args.no_header_row
            and not self.args.skip_lines
            and sniff_limit == 0
        ):
            reader = agate.csv.reader(self.input_file, **self.reader_kwargs)
            writer = agate.csv.writer(self.output_file, **self.writer_kwargs)
            writer.writerows(reader)
        elif filetype == 'fixed':
            self.output_file.write(fixed2csv(self.input_file, schema, output=self.output_file, **kwargs))
        elif filetype == 'geojson':
            self.output_file.write(geojson2csv(self.input_file, **kwargs))
        elif filetype in ('csv', 'dbf', 'json', 'ndjson', 'xls', 'xlsx'):
            if filetype == 'csv':
                table = agate.Table.from_csv(self.input_file, **kwargs)
            elif filetype == 'json':
                table = agate.Table.from_json(self.input_file, key=self.args.key, **kwargs)
            elif filetype == 'ndjson':
                table = agate.Table.from_json(self.input_file, key=self.args.key, newline=True, **kwargs)
            elif filetype in ('xls', 'xlsx'):
                table = self.excel_table(filetype, excel_data, self.args.sheet, kwargs)
            elif filetype == 'dbf':
                if not hasattr(self.input_file, 'name'):
                    raise ValueError('DBF files can not be converted from stdin. You must pass a filename.')
                table = agate.Table.from_dbf(self.input_file.name, **kwargs)
            table.to_csv(self.output_file, **self.writer_kwargs)

        if self.args.write_sheets:
            # Don't rely on the input file, as the file object has been mutated or closed.
            self.input_file.close()

            if not path or path == '-':
                base = 'stdin'
            else:
                base = splitext(path)[0]

            sheet_names = self.sheet_names(path, filetype)
            if self.args.write_sheets == '-':
                sheets = sheet_names
            else:
                sheets = [
                    sheet_names[int(sheet)] if sheet.isdigit() else sheet
                    for sheet in self.args.write_sheets.split(',')
                ]

            for i, sheet_name in enumerate(sheets):
                table = self.excel_table(filetype, excel_data, sheet_name, kwargs)

                if self.args.use_sheet_names:
                    filename = '{}_{}.csv'.format(base, sheet_name)
                else:
                    filename = '%s_%d.csv' % (base, i)
                with open(filename, 'w') as f:
                    table.to_csv(f, **self.writer_kwargs)

        self.input_file.close()

        if self.args.schema:
            schema.close()


def launch_new_instance():
    utility = In2CSV()
    utility.run()


if __name__ == '__main__':
    launch_new_instance()
