#!/usr/bin/env python

import sys

import agate

from csvkit.cleanup import RowChecker
from csvkit.cli import CSVKitUtility


class CSVClean(CSVKitUtility):
    description = 'Report and fix common errors in a CSV file.'
    override_flags = ['L', 'I']

    def add_arguments(self):
        self.argparser.add_argument(
            '--length-mismatch', dest='length_mismatch', action='store_true',
            help='Report data rows that are shorter or longer than the header row.')
        self.argparser.add_argument(
            '--empty-columns', dest='empty_columns', action='store_true',
            help='Report empty columns as errors.')
        self.argparser.add_argument(
            '-a', '--enable-all-checks', dest='enable_all_checks', action='store_true',
            help='Enable all error reporting.')
        self.argparser.add_argument(
            '--omit-error-rows', dest='omit_error_rows', action='store_true',
            help='Omit data rows that contain errors, from standard output.')
        self.argparser.add_argument(
            '--label', dest='label',
            help='Add a "label" column to standard error. Useful in automated workflows. '
                 'Use "-" to default to the input filename.')
        self.argparser.add_argument(
            '--header-normalize-space', dest='header_normalize_space', action='store_true',
            help='Strip leading and trailing whitespace and replace sequences of whitespace characters by a single '
                 'space in the header.')
        self.argparser.add_argument(
            '--join-short-rows', dest='join_short_rows', action='store_true',
            help='Merges short rows into a single row.')
        self.argparser.add_argument(
            '--separator', dest='separator', default='\n',
            help='The string with which to join short rows. Defaults to a newline.')
        self.argparser.add_argument(
            '--fill-short-rows', dest='fill_short_rows', action='store_true',
            help='Fill short rows with the missing cells.')
        self.argparser.add_argument(
            '--fillvalue', dest='fillvalue',
            help='The value with which to fill short rows. Defaults to none.')
        self.argparser.add_argument(
            '--remove-empty-columns', dest='remove_empty_columns', action='store_true',
            help='Remove empty columns from standard output.')
        self.argparser.add_argument(
            '--line-map', dest='line_map', metavar='PATH',
            help='Write a CSV file to PATH mapping each output row to the 1-based physical line numbers of the '
                 'input file that produced it, including rows that span multiple lines, rows merged by '
                 '--join-short-rows, and error rows omitted from standard output.')

    def main(self):
        if self.additional_input_expected():
            sys.stderr.write('No input file or piped data provided. Waiting for standard input:\n')

        if (
            # Checks
            not self.args.length_mismatch
            and not self.args.empty_columns
            and not self.args.enable_all_checks
            # Fixes
            and not self.args.header_normalize_space
            and not self.args.join_short_rows
            and not self.args.fill_short_rows
            and not self.args.remove_empty_columns
        ):
            self.argparser.error('No checks or fixes were enabled. See available options with: csvclean --help')

        if self.args.join_short_rows and self.args.fill_short_rows:
            self.argparser.error('The --join-short-rows and --fill-short-rows options are mutually exclusive.')

        default = self.args.enable_all_checks

        # skip_lines() consumes lines from the input file, so capture the offset to keep
        # the line map relative to the original file.
        line_offset = self.args.skip_lines if isinstance(self.args.skip_lines, int) else 0

        reader = agate.csv.reader(self.skip_lines(), **self.reader_kwargs)

        checker = RowChecker(
            reader,
            # Checks
            length_mismatch=default or self.args.length_mismatch,
            empty_columns=default or self.args.empty_columns,
            # Fixes
            header_normalize_space=self.args.header_normalize_space,
            join_short_rows=self.args.join_short_rows,
            separator=self.args.separator,
            fill_short_rows=self.args.fill_short_rows,
            fillvalue=self.args.fillvalue,
            remove_empty_columns=self.args.remove_empty_columns,
            # Other
            zero_based=self.args.zero_based,
            omit_error_rows=self.args.omit_error_rows,
        )

        label = self.args.label
        if label == '-':
            if self.input_file == sys.stdin:
                label = 'stdin'
            else:
                label = self.input_file.name

        output_writer = agate.csv.writer(self.output_file, **self.writer_kwargs)
        rows = checker.checked_rows()
        # checked_rows() is a generator. We need to consume it to get the output_column_names, without empty columns.
        if self.args.remove_empty_columns:
            rows = list(rows)
        output_writer.writerow(checker.output_column_names)
        line_map_entries = []
        if self.args.line_map and checker.header_line_start is not None:
            line_map_entries.append(
                ('header', 1, checker.header_line_start + line_offset, checker.header_line_end + line_offset)
            )
        output_line = 1
        for row in rows:
            output_line += 1
            output_writer.writerow(row)
            if self.args.line_map:
                line_start, line_end = checker.row_line_spans[output_line - 2]
                line_map_entries.append(('data', output_line, line_start + line_offset, line_end + line_offset))

        if self.args.line_map:
            for error in checker.errors:
                if error.line_start is not None:
                    line_map_entries.append(
                        ('error', '', error.line_start + line_offset, error.line_end + line_offset)
                    )
            line_map_entries.sort(key=lambda entry: (entry[2], entry[3], entry[0]))
            with open(self.args.line_map, 'w', newline='', encoding='utf-8') as line_map_file:
                line_map_writer = agate.csv.writer(line_map_file, **self.writer_kwargs)
                line_map_writer.writerow(['kind', 'output_line', 'input_line_start', 'input_line_end'])
                line_map_writer.writerows(line_map_entries)

        if checker.errors:
            error_writer = agate.csv.writer(self.error_file, **self.writer_kwargs)

            fieldnames = ['line_number', 'msg'] + checker.column_names
            if self.args.label:
                fieldnames.insert(0, 'label')
            error_writer.writerow(fieldnames)

            for error in checker.errors:
                row = [error.line_number, error.msg] + error.row
                if self.args.label:
                    row.insert(0, label)
                error_writer.writerow(row)

            sys.exit(1)


def launch_new_instance():
    utility = CSVClean()
    utility.run()


if __name__ == '__main__':
    launch_new_instance()
