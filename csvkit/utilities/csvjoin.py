#!/usr/bin/env python

import sys

import agate

from csvkit.cli import CSVKitUtility, isatty, match_column_identifier


class _NullKey:
    """
    A placeholder join key for a row in which any part of the key is null.
    Each instance is unique, so such rows never match any other row.
    """


def _make_join_key(column_names):
    """
    Create a function that returns the composite join key for a row, or a
    unique _NullKey if any part of the key is null.
    """
    def join_key(row):
        key = tuple(row[column_name] for column_name in column_names)

        if any(value is None for value in key):
            return _NullKey()

        return key

    return join_key


class CSVJoin(CSVKitUtility):
    description = 'Execute a SQL-like join to merge CSV files on a specified column or columns.'
    epilog = "Note that the join operation requires reading all files into memory. Don't try this on very large files."
    # Override 'f' because the utility accepts multiple files.
    override_flags = ['f']

    def add_arguments(self):
        self.argparser.add_argument(
            metavar='FILE', nargs='*', dest='input_paths', default=['-'],
            help='The CSV files to operate on. If only one is specified, it will be copied to STDOUT.')
        self.argparser.add_argument(
            '-c', '--columns', dest='columns',
            help='The column name(s) on which to join. To join on a composite key, separate the names (or indices) '
                 'with "+", for example "region+store". To use different columns in each file, provide a '
                 'comma-separated list with one such specification per file, in the same order in which the files '
                 'were specified. If not specified, the two files will be joined sequentially without matching.')
        self.argparser.add_argument(
            '--outer', dest='outer_join', action='store_true',
            help='Perform a full outer join, rather than the default inner join.')
        self.argparser.add_argument(
            '--left', dest='left_join', action='store_true',
            help='Perform a left outer join, rather than the default inner join. If more than two files are provided '
                 'this will be executed as a sequence of left outer joins, starting at the left.')
        self.argparser.add_argument(
            '--right', dest='right_join', action='store_true',
            help='Perform a right outer join, rather than the default inner join. If more than two files are provided '
                 'this will be executed as a sequence of right outer joins, starting at the right.')
        self.argparser.add_argument(
            '--right-unique', dest='right_unique', action='store_true',
            help='Require that each join key appears in at most one row of every right-hand file. If any key is '
                 'duplicated, the duplicated keys are reported and no output is written.')
        self.argparser.add_argument(
            '-y', '--snifflimit', dest='sniff_limit', type=int, default=1024,
            help='Limit CSV dialect sniffing to the specified number of bytes. '
                 'Specify "0" to disable sniffing entirely, or "-1" to sniff the entire file.')
        self.argparser.add_argument(
            '-I', '--no-inference', dest='no_inference', action='store_true',
            help='Disable type inference (and --locale, --date-format, --datetime-format, --no-leading-zeroes) '
                 'when parsing the input.')

    def main(self):
        if isatty(sys.stdin) and self.args.input_paths == ['-']:
            self.argparser.error('You must provide an input file or piped data.')

        self.input_files = []

        for path in self.args.input_paths:
            self.input_files.append(self._open_input_file(path))

        if self.args.columns:
            join_column_names = self._parse_join_column_names(self.args.columns)

            if len(join_column_names) == 1:
                join_column_names = join_column_names * len(self.input_files)

            if len(join_column_names) != len(self.input_files):
                self.argparser.error('The number of join column names must match the number of files, or be a single '
                                     'column name that exists in all files.')

            if len({len(names) for names in join_column_names}) != 1:
                self.argparser.error('Each file must be joined on the same number of columns.')

        if (self.args.left_join or self.args.right_join or self.args.outer_join) and not self.args.columns:
            self.argparser.error('You must provide join column names when performing an outer join.')

        if self.args.left_join and self.args.right_join:
            self.argparser.error('It is not valid to specify both a left and a right join.')

        if self.args.right_unique and not self.args.columns:
            self.argparser.error('You must provide join column names when using --right-unique.')

        tables = []
        sniff_limit = self.args.sniff_limit if self.args.sniff_limit != -1 else None
        column_types = self.get_column_types()

        for f in self.input_files:
            tables.append(agate.Table.from_csv(
                f,
                skip_lines=self.args.skip_lines,
                sniff_limit=sniff_limit,
                column_types=column_types,
                **self.reader_kwargs,
            ))
            f.close()

        join_columns = []
        join_keys = []

        if self.args.columns:
            for i, table in enumerate(tables):
                column_names = [
                    table.column_names[match_column_identifier(table.column_names, name)]
                    for name in join_column_names[i]
                ]
                join_columns.append(column_names)
                join_keys.append(_make_join_key(column_names))

        if self.args.right_unique:
            if self.args.right_join:
                right_hand_indices = range(len(tables) - 1)
            else:
                right_hand_indices = range(1, len(tables))

            for i in right_hand_indices:
                self._check_unique_keys(tables[i], join_keys[i], self.args.input_paths[i])

        jointab = tables[0]

        if self.args.left_join:
            # Left outer join
            for i, table in enumerate(tables[1:]):
                jointab = self._join_tables(
                    jointab, table, join_keys[0], join_keys[i + 1],
                    self._non_key_columns(table, join_columns[i + 1]))
        elif self.args.right_join:
            # Right outer join
            jointab = tables[-1]

            remaining_tables = tables[:-1]
            remaining_tables.reverse()

            for i, table in enumerate(remaining_tables):
                jointab = self._join_tables(
                    jointab, table, join_keys[-1], join_keys[-(i + 2)],
                    self._non_key_columns(table, join_columns[-(i + 2)]))
        elif self.args.outer_join:
            # Full outer join
            for i, table in enumerate(tables[1:]):
                jointab = agate.Table.join(jointab, table, join_keys[0], join_keys[i + 1], full_outer=True)
        elif self.args.columns:
            # Inner join
            for i, table in enumerate(tables[1:]):
                jointab = self._join_tables(
                    jointab, table, join_keys[0], join_keys[i + 1],
                    self._non_key_columns(table, join_columns[i + 1]), inner=True)
        else:
            # Sequential join
            for table in tables[1:]:
                jointab = agate.Table.join(jointab, table, full_outer=True)

        jointab.to_csv(self.output_file, **self.writer_kwargs)

    def _parse_join_column_names(self, join_string):
        """
        Parse a list of join columns. Each file's columns are separated by
        commas, and the columns of a composite key are separated by "+".
        """
        return [list(map(str.strip, part.split('+'))) for part in join_string.split(',')]

    @staticmethod
    def _join_tables(left_table, right_table, left_key, right_key, right_column_names, inner=False):
        """
        Join two tables, keeping only the given columns of the right table.
        """
        if right_column_names:
            return agate.Table.join(left_table, right_table, left_key, right_key, inner=inner,
                                    columns=right_column_names)

        # agate can't select an empty set of columns, so join on all columns
        # and then drop the right-hand columns, which follow the left's.
        left_column_names = list(left_table.column_names)
        joined = agate.Table.join(left_table, right_table, left_key, right_key, inner=inner)

        return joined.select(left_column_names)

    @staticmethod
    def _non_key_columns(table, key_column_names):
        """
        Return the names of the table's columns that are not join keys.
        """
        return [name for name in table.column_names if name not in key_column_names]

    def _check_unique_keys(self, table, join_key, path):
        """
        Error out if any join key appears in more than one row of the table.
        Rows with null keys are ignored, since they can never match.
        """
        key_counts = {}

        for row in table.rows:
            key = join_key(row)

            if isinstance(key, _NullKey):
                continue

            key_counts[key] = key_counts.get(key, 0) + 1

        duplicates = [(key, count) for key, count in key_counts.items() if count > 1]

        if duplicates:
            self.argparser.error(
                'The right-hand file "%s" contains duplicate join keys: %s' % (
                    path,
                    '; '.join(
                        '%s (%d rows)' % (', '.join(str(value) for value in key), count)
                        for key, count in duplicates
                    ),
                ))


def launch_new_instance():
    utility = CSVJoin()
    utility.run()


if __name__ == '__main__':
    launch_new_instance()
