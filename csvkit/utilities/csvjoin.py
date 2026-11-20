#!/usr/bin/env python

import itertools
import sys

import agate

from csvkit.cli import CSVKitUtility, isatty, match_column_identifier
from csvkit.exceptions import JoinKeyConflictError

# Suffix used for temporary columns that hold mapped join-key components.
_KEY_COLUMN_PREFIX = '__csvjoin_key_'


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
            help='The column name(s) on which to join. Should be either one name (or index) or a comma-separated list '
                 'with one name (or index) per file, in the same order in which the files were specified. To join on '
                 'a composite key, separate the per-file column lists with semicolons and the columns within each '
                 'list with commas, for example "region_a,store_a;region_b,store_b". If not specified, the two files '
                 'will be joined sequentially without matching.')
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
            '--map-file', dest='map_files', action='append', default=[], metavar='FILE',
            help='A CSV file mapping old numbers to current numbers for the store-number component of the join key. '
                 'The mapping CSV must have "old_number" and "new_number" columns and may have a "region" column. '
                 'Only numbers explicitly listed are mapped; every other value is kept as-is. Repeat this option once '
                 'per input file, in input order, or prefix the path with the 1-based input position and a colon '
                 '(for example "2:mappings.csv") to target a specific file.')
        self.argparser.add_argument(
            '--map-region', dest='map_regions', action='append', default=[], metavar='REGION',
            help='Scope a mapping file without a "region" column to one region. Repeat in the same order as '
                 '--map-file, or give a single value that applies to every mapping file.')
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

        if (self.args.left_join or self.args.right_join or self.args.outer_join) and not self.args.columns:
            self.argparser.error('You must provide join column names when performing an outer join.')

        if self.args.left_join and self.args.right_join:
            self.argparser.error('It is not valid to specify both a left and a right join.')

        if self.args.map_files and not self.args.columns:
            self.argparser.error('You must provide join column names with -c when using --map-file.')

        mappings = self._parse_map_files(len(self.input_files))

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

        join_column_ids = []

        if self.args.columns:
            for i, table in enumerate(tables):
                join_column_ids.append([
                    match_column_identifier(table.column_names, name) for name in join_column_names[i]
                ])

        composite = self.args.columns and any(len(ids) > 1 for ids in join_column_ids)
        mapped = any(mapping is not None for mapping in mappings)

        if mapped or composite:
            jointab = self._multiway_join(tables, join_column_ids, mappings)
        else:
            jointab = self._agate_join(tables, join_column_ids)

        jointab.to_csv(self.output_file, **self.writer_kwargs)

    def _agate_join(self, tables, join_column_ids):
        """
        The legacy join path, delegating directly to agate with its original
        column selection, output ordering and duplicate-column naming.
        """
        def key(ids):
            if len(ids) == 1:
                return ids[0]
            return ids

        jointab = tables[0]

        if self.args.left_join:
            # Left outer join
            for i, table in enumerate(tables[1:]):
                jointab = agate.Table.join(jointab, table, key(join_column_ids[0]), key(join_column_ids[i + 1]))
        elif self.args.right_join:
            # Right outer join
            jointab = tables[-1]

            remaining_tables = tables[:-1]
            remaining_tables.reverse()

            for i, table in enumerate(remaining_tables):
                jointab = agate.Table.join(jointab, table, key(join_column_ids[-1]), key(join_column_ids[-(i + 2)]))
        elif self.args.outer_join:
            # Full outer join
            for i, table in enumerate(tables[1:]):
                jointab = agate.Table.join(
                    jointab, table, key(join_column_ids[0]), key(join_column_ids[i + 1]), full_outer=True)
        elif self.args.columns:
            # Inner join
            for i, table in enumerate(tables[1:]):
                jointab = agate.Table.join(
                    jointab, table, key(join_column_ids[0]), key(join_column_ids[i + 1]), inner=True)
        else:
            # Sequential join
            for table in tables[1:]:
                jointab = agate.Table.join(jointab, table, full_outer=True)

        return jointab

    def _multiway_join(self, tables, join_column_ids, mappings):
        """
        Join every table simultaneously on its join key.

        Each input keeps its original columns and values; mappings only affect
        auxiliary key columns. Rows whose key contains a blank component never
        match another row and remain attributed to their own source. A mapping
        that collapses two source rows of one input onto the same key aborts
        the whole batch before any output is written.
        """
        key_tables = []
        key_builders = []
        original_widths = []

        for i, table in enumerate(tables):
            ids = join_column_ids[i]
            mapping = mappings[i]
            key_names = [f'{_KEY_COLUMN_PREFIX}{j}' for j in range(len(ids))]
            original_widths.append(len(table.columns))

            computations = []
            for j, column_id in enumerate(ids):
                if mapping is not None and j == len(ids) - 1:
                    def make_func(number_col=column_id, region_col=ids[0], number_mapping=mapping):
                        def func(row):
                            number = row[number_col]
                            if number is None:
                                return None
                            region = row[region_col] if len(ids) > 1 else None
                            return number_mapping.get((region, str(number)), number_mapping.get((None, str(number)),
                                                                                                 number))
                        return func

                    computations.append((key_names[j], agate.Formula(agate.Text(), make_func(), cast=False)))
                else:
                    def make_func(number_col=column_id):
                        return lambda row: row[number_col]

                    computations.append((key_names[j], agate.Formula(agate.Text(), make_func(), cast=False)))

            key_table = table.compute(computations)
            key_tables.append(key_table)
            key_builders.append([key_table.column_names.index(name) for name in key_names])

            if mapping is not None:
                self._check_key_conflicts(i, table, key_table, key_builders[-1], ids)

        groups = {}
        group_order = []
        blank_rows = [[] for _ in tables]

        for file_index, key_table in enumerate(key_tables):
            for row_index, row in enumerate(key_table.rows):
                key_values = tuple(row[col] for col in key_builders[file_index])

                if any(value is None for value in key_values):
                    blank_rows[file_index].append(row_index)
                    continue

                if key_values not in groups:
                    groups[key_values] = [[] for _ in tables]
                    group_order.append(key_values)

                groups[key_values][file_index].append(row_index)

        output_names, output_types = self._output_schema(tables)
        output_rows = []

        def keep_group(participants):
            if self.args.outer_join or not self.args.columns:
                return True
            if self.args.left_join:
                return bool(participants[0])
            if self.args.right_join:
                return bool(participants[-1])
            return all(participants)

        for key_values in group_order:
            participants = groups[key_values]

            if not keep_group(participants):
                continue

            choices_per_table = [indices or [None] for indices in participants]

            for choices in itertools.product(*choices_per_table):
                output_rows.append(self._assemble_row(key_tables, original_widths, choices))

        if self.args.outer_join or not self.args.columns or self.args.left_join or self.args.right_join:
            for file_index, row_indices in enumerate(blank_rows):
                if self.args.left_join and file_index != 0:
                    continue
                if self.args.right_join and file_index != len(tables) - 1:
                    continue

                for row_index in row_indices:
                    choices = [None] * len(tables)
                    choices[file_index] = row_index
                    output_rows.append(self._assemble_row(key_tables, original_widths, choices))

        return agate.Table(output_rows, column_names=output_names, column_types=output_types)

    def _output_schema(self, tables):
        column_names = []
        column_types = []

        for table in tables:
            for column in table.columns:
                candidate = column.name

                if candidate in column_names:
                    suffix = 2
                    while f'{column.name}{suffix}' in column_names:
                        suffix += 1
                    candidate = f'{column.name}{suffix}'

                column_names.append(candidate)
                column_types.append(column.data_type)

        return column_names, column_types

    def _assemble_row(self, key_tables, original_widths, choices):
        values = []

        for file_index, row_index in enumerate(choices):
            width = original_widths[file_index]

            if row_index is None:
                values.extend([None] * width)
            else:
                values.extend(key_tables[file_index].rows[row_index].values()[:width])

        return values

    def _check_key_conflicts(self, file_index, source_table, key_table, key_ids, source_key_ids):
        seen = {}

        for row_index, row in enumerate(key_table.rows):
            key_values = tuple(row[col] for col in key_ids)

            if any(value is None for value in key_values):
                continue

            if key_values in seen:
                earlier_index = seen[key_values]
                earlier = source_table.rows[earlier_index]
                current = source_table.rows[row_index]

                def format_row(source_row):
                    return ', '.join(
                        f'{name}={source_row[col]!r}'
                        for col, name in enumerate(source_table.column_names))

                original_key = ', '.join(
                    f'{source_table.column_names[col]}={earlier[col]!r}' for col in source_key_ids)
                current_key = ', '.join(
                    f'{source_table.column_names[col]}={current[col]!r}' for col in source_key_ids)
                formatted_key = ', '.join(repr(value) for value in key_values)

                raise JoinKeyConflictError(
                    f'Join key conflict in input {file_index + 1} ({self.args.input_paths[file_index]}). '
                    f'Two source records map to the same join key ({formatted_key}):\n'
                    f'  source row {earlier_index + 1} [{original_key}]: {format_row(earlier)}\n'
                    f'  source row {row_index + 1} [{current_key}]: {format_row(current)}\n'
                    'The input declares one row per business identity, so the mapping is ambiguous. '
                    'No output was written.')

            seen[key_values] = row_index

    def _parse_map_files(self, file_count):
        mappings = [None] * file_count
        next_position = 0

        for index, raw_path in enumerate(self.args.map_files):
            prefix, separator, remainder = raw_path.partition(':')

            if separator and prefix.isdigit():
                path = remainder
                position = int(prefix) - 1

                if not 0 <= position < file_count:
                    self.argparser.error(
                        f'--map-file position {prefix} is invalid for {file_count} input file(s).')
            else:
                path = raw_path

                while next_position < file_count and mappings[next_position] is not None:
                    next_position += 1

                position = next_position
                next_position += 1

                if position >= file_count:
                    self.argparser.error('More --map-file options were given than input files. '
                                         'Use the "N:path" form to target a specific input file.')

            region_scope = None
            if self.args.map_regions:
                if len(self.args.map_regions) == 1:
                    region_scope = self.args.map_regions[0]
                elif index < len(self.args.map_regions):
                    region_scope = self.args.map_regions[index]
                else:
                    self.argparser.error('Each --map-file needs a --map-region, or give a single --map-region.')

            mappings[position] = self._load_mapping(path, region_scope)

        return mappings

    def _load_mapping(self, path, region_scope):
        mapping_table = agate.Table.from_csv(
            path,
            column_types=agate.TypeTester(types=[agate.Text()]),
            sniff_limit=self.args.sniff_limit if self.args.sniff_limit != -1 else None,
        )

        required = ('old_number', 'new_number')
        missing = [name for name in required if name not in mapping_table.column_names]

        if missing:
            self.argparser.error(f'Mapping file {path} is missing required column(s): {", ".join(missing)}.')

        region_index = mapping_table.column_names.index('region') if 'region' in mapping_table.column_names else None
        old_index = mapping_table.column_names.index('old_number')
        new_index = mapping_table.column_names.index('new_number')
        mapping = {}

        for row in mapping_table.rows:
            if row[old_index] is None or row[new_index] is None:
                continue

            region = row[region_index] if region_index is not None else region_scope
            old_number = str(row[old_index])
            new_number = str(row[new_index])
            mapping_key = (region, old_number)

            if mapping_key in mapping and mapping[mapping_key] != new_number:
                region_label = f'region {region!r}, ' if region is not None else ''
                self.argparser.error(
                    f'Mapping file {path} maps {region_label}old number {old_number!r} to both '
                    f'{mapping[mapping_key]!r} and {new_number!r}.')

            mapping[mapping_key] = new_number

            # A region-scoped mapping file (no "region" column of its own) must also
            # match single-column join keys, which carry no region component.
            if region_index is None and region_scope is not None:
                mapping.setdefault((None, old_number), new_number)

        return mapping

    def _parse_join_column_names(self, join_string):
        """
        Parse a list of join columns. Semicolons separate per-file column lists
        (composite keys); commas separate files in the legacy syntax, or the
        key columns within a per-file list when semicolons are used.
        """
        if ';' in join_string:
            return [
                [name.strip() for name in group.split(',') if name.strip()]
                for group in join_string.split(';')
                if group.strip()
            ]

        return [[name.strip()] for name in join_string.split(',')]


def launch_new_instance():
    utility = CSVJoin()
    utility.run()


if __name__ == '__main__':
    launch_new_instance()
