#!/usr/bin/env python

import io
import itertools
import json
import locale
import sys
import warnings
from collections import Counter, OrderedDict
from decimal import Decimal

import agate

from csvkit.cli import CSVKitUtility, default_float_decimal, make_default_headers, parse_column_identifiers

locale.setlocale(locale.LC_ALL, '')
OPERATIONS = OrderedDict([
    ('type', {
        'aggregation': None,
        'label': 'Type of data: ',
    }),
    ('nulls', {
        'aggregation': agate.HasNulls,
        'label': 'Contains null values: ',
    }),
    ('nonnulls', {
        'aggregation': agate.Count,
        'label': 'Non-null values: ',
    }),
    ('unique', {
        'aggregation': None,
        'label': 'Unique values: ',
    }),
    ('min', {
        'aggregation': agate.Min,
        'label': 'Smallest value: ',
    }),
    ('max', {
        'aggregation': agate.Max,
        'label': 'Largest value: ',
    }),
    ('sum', {
        'aggregation': agate.Sum,
        'label': 'Sum: ',
    }),
    ('mean', {
        'aggregation': agate.Mean,
        'label': 'Mean: ',
    }),
    ('median', {
        'aggregation': agate.Median,
        'label': 'Median: ',
    }),
    ('stdev', {
        'aggregation': agate.StDev,
        'label': 'StDev: ',
    }),
    ('len', {
        'aggregation': agate.MaxLength,
        'label': 'Longest value: ',
    }),
    ('maxprecision', {
        'aggregation': agate.MaxPrecision,
        'label': 'Most decimal places: ',
    }),
    ('freq', {
        'aggregation': None,
        'label': 'Most common values: ',
    }),
])


class CSVStat(CSVKitUtility):
    description = 'Print descriptive statistics for each column in a CSV file.'

    def add_arguments(self):
        self.argparser.add_argument(
            '--csv', dest='csv_output', action='store_true',
            help='Output results as a CSV table, rather than plain text.')
        self.argparser.add_argument(
            '--json', dest='json_output', action='store_true',
            help='Output results as JSON text, rather than plain text.')
        self.argparser.add_argument(
            '-i', '--indent', dest='indent', type=int,
            help='Indent the output JSON this many spaces. Disabled by default.')
        self.argparser.add_argument(
            '-n', '--names', dest='names_only', action='store_true',
            help='Display column names and indices from the input CSV and exit.')
        self.argparser.add_argument(
            '-c', '--columns', dest='columns',
            help='A comma-separated list of column indices, names or ranges to be examined, e.g. "1,id,3-5". '
                 'Defaults to all columns.')
        self.argparser.add_argument(
            '--type', dest='type_only', action='store_true',
            help='Only output data type.')
        self.argparser.add_argument(
            '--nulls', dest='nulls_only', action='store_true',
            help='Only output whether columns contains nulls.')
        self.argparser.add_argument(
            '--non-nulls', dest='nonnulls_only', action='store_true',
            help='Only output counts of non-null values.')
        self.argparser.add_argument(
            '--unique', dest='unique_only', action='store_true',
            help='Only output counts of unique values.')
        self.argparser.add_argument(
            '--min', dest='min_only', action='store_true',
            help='Only output smallest values.')
        self.argparser.add_argument(
            '--max', dest='max_only', action='store_true',
            help='Only output largest values.')
        self.argparser.add_argument(
            '--sum', dest='sum_only', action='store_true',
            help='Only output sums.')
        self.argparser.add_argument(
            '--mean', dest='mean_only', action='store_true',
            help='Only output means.')
        self.argparser.add_argument(
            '--median', dest='median_only', action='store_true',
            help='Only output medians.')
        self.argparser.add_argument(
            '--stdev', dest='stdev_only', action='store_true',
            help='Only output standard deviations.')
        self.argparser.add_argument(
            '--len', dest='len_only', action='store_true',
            help='Only output the length of the longest values.')
        self.argparser.add_argument(
            '--max-precision', dest='maxprecision_only', action='store_true',
            help='Only output the most decimal places.')
        self.argparser.add_argument(
            '--freq', dest='freq_only', action='store_true',
            help='Only output lists of frequent values.')
        self.argparser.add_argument(
            '--freq-count', dest='freq_count', type=int,
            help='The maximum number of frequent values to display.')
        self.argparser.add_argument(
            '--count', dest='count_only', action='store_true',
            help='Only output total row count.')
        self.argparser.add_argument(
            '--decimal-format', dest='decimal_format', type=str, default='%.3f',
            help='%%-format specification for printing decimal numbers. '
                 'Defaults to locale-specific formatting with "%%.3f".')
        self.argparser.add_argument(
            '-G', '--no-grouping-separator', dest='no_grouping_separator', action='store_true',
            help='Do not use grouping separators in decimal numbers.')
        self.argparser.add_argument(
            '-y', '--snifflimit', dest='sniff_limit', type=int, default=1024,
            help='Limit CSV dialect sniffing to the specified number of bytes. '
                 'Specify "0" to disable sniffing entirely, or "-1" to sniff the entire file.')
        self.argparser.add_argument(
            '-I', '--no-inference', dest='no_inference', action='store_true',
            help='Disable type inference (and --locale, --date-format, --datetime-format, --no-leading-zeroes) '
                 'when parsing the input.')
        self.argparser.add_argument(
            '-s', '--stream', '--streaming', dest='stream', action='store_true',
            help='Read the input one row at a time, instead of loading it into memory. Unique value counts are '
                 'estimated; estimated counts are marked as such. Null and non-null counts are exact.')
        self.argparser.add_argument(
            '--chunk-size', '--chunksize', dest='chunk_size', type=int,
            help='With --stream, read the input in chunks of this many rows. The estimates do not depend on the '
                 'chunk size.')

    def main(self):
        if self.args.names_only:
            self.print_column_names()
            return

        if self.additional_input_expected():
            self.argparser.error('You must provide an input file or piped data.')

        operations = [op for op in OPERATIONS.keys() if getattr(self.args, op + '_only')]

        if len(operations) > 1:
            self.argparser.error('Only one operation argument may be specified (--mean, --median, etc).')

        if self.args.chunk_size is not None:
            if not self.args.stream:
                self.argparser.error('--chunk-size is only allowed with --stream.')
            if self.args.chunk_size < 1:
                self.argparser.error('--chunk-size must be a positive integer.')

        if operations and self.args.csv_output:
            self.argparser.error(
                'You may not specify --csv and an operation (--mean, --median, etc) at the same time.')
        if operations and self.args.json_output:
            self.argparser.error(
                'You may not specify --json and an operation (--mean, --median, etc) at the same time.')
        if operations and self.args.count_only:
            self.argparser.error(
                'You may not specify --count and an operation (--mean, --median, etc) at the same time.')

        if self.args.count_only:
            count = sum(1 for _ in agate.csv.reader(self.skip_lines(), **self.reader_kwargs))

            if not self.args.no_header_row:
                count -= 1

            self.output_file.write('%i\n' % count)

            return

        sniff_limit = self.args.sniff_limit if self.args.sniff_limit != -1 else None

        if self.args.stream:
            table = self.read_streaming_table(sniff_limit)
        else:
            table = agate.Table.from_csv(
                self.input_file,
                skip_lines=self.args.skip_lines,
                sniff_limit=sniff_limit,
                column_types=self.get_column_types(),
                **self.reader_kwargs,
            )

        column_ids = parse_column_identifiers(
            self.args.columns,
            table.column_names,
            self.get_column_offset(),
        )

        kwargs = {}

        if self.args.freq_count:
            kwargs['freq_count'] = self.args.freq_count

        # Output a single stat
        if operations:
            column_ids = list(column_ids)

            if len(column_ids) == 1:
                self.print_one(table, column_ids[0], operations[0], label=False, **kwargs)
            else:
                for column_id in column_ids:
                    self.print_one(table, column_id, operations[0], **kwargs)
        else:
            column_ids = list(column_ids)
            stats = {}

            for column_id in column_ids:
                stats[column_id] = self.calculate_stats(table, column_id, **kwargs)

            if self.args.csv_output:
                self.print_csv(table, column_ids, stats)
            elif self.args.json_output:
                self.print_json(table, column_ids, stats)
            else:
                self.print_stats(table, column_ids, stats)

    def read_streaming_table(self, sniff_limit):
        """
        Read the input one row at a time, accumulating statistics without retaining the rows.
        """
        from csvkit.streaming import StreamingColumn, StreamingTable

        self.skip_lines()

        kwargs = dict(self.reader_kwargs)
        kwargs.pop('line_numbers', None)

        if sniff_limit is not None and sniff_limit > 0:
            if self.input_file is sys.stdin:
                sample = self.input_file.buffer.peek(sniff_limit).decode(self.args.encoding, 'ignore')[:sniff_limit]
            else:
                offset = self.input_file.tell()
                sample = self.input_file.read(sniff_limit)
                self.input_file.seek(offset)

            kwargs['dialect'] = agate.csv.Sniffer().sniff(sample)
        elif sniff_limit is None:
            sample = self.input_file.read()
            kwargs['dialect'] = agate.csv.Sniffer().sniff(sample)
            self.input_file = io.StringIO(sample)

        rows = agate.csv.reader(self.input_file, **kwargs)
        if self.args.no_header_row:
            try:
                first_row = next(rows)
            except StopIteration:
                first_row = None

            column_names = make_default_headers(len(first_row)) if first_row is not None else []
            rows = itertools.chain([first_row], rows) if first_row is not None else iter(())
        else:
            try:
                column_names = next(rows)
            except StopIteration:
                column_names = []

        column_names = agate.utils.deduplicate(column_names, column_names=True)

        data_types = self.get_column_types()._possible_types
        columns = [StreamingColumn(name, data_types) for name in column_names]

        row_count = 0
        batches = self._chunks(rows, self.args.chunk_size) if self.args.chunk_size else (rows,)

        for batch in batches:
            for row in batch:
                row_count += 1

                if len(row) > len(columns):
                    raise ValueError(
                        f'Row {row_count - 1} has {len(row)} values, but Table only has {len(columns)} columns.')

                for i, column in enumerate(columns):
                    column.add(row[i] if i < len(row) else None, row_count - 1)

        return StreamingTable(column_names, columns, row_count)

    @staticmethod
    def _chunks(rows, size):
        while True:
            chunk = list(itertools.islice(rows, size))

            if not chunk:
                break

            yield chunk

    def is_finite_decimal(self, value):
        return isinstance(value, Decimal) and value.is_finite()

    def _calculate_stat(self, table, column_id, op_name, op_data, **kwargs):
        if getattr(table, 'is_streaming', False):
            column = table.columns[column_id]

            return format_stat(
                column.get(op_name, **kwargs),
                op_name,
                self.args,
                estimated=column.is_estimated(op_name),
            )

        getter = globals().get(f'get_{op_name}')

        with warnings.catch_warnings():
            warnings.simplefilter('ignore', agate.NullCalculationWarning)

            try:
                if getter:
                    return getter(table, column_id, **kwargs)

                op = op_data['aggregation']
                v = table.aggregate(op(column_id))

                if self.is_finite_decimal(v) and not self.args.json_output:
                    return format_decimal(v, self.args.decimal_format, self.args.no_grouping_separator)

                return v
            except Exception:
                pass

    def print_one(self, table, column_id, op_name, label=True, **kwargs):
        """
        Print data for a single statistic.
        """
        column_name = table.column_names[column_id]

        stat = self._calculate_stat(table, column_id, op_name, OPERATIONS[op_name], **kwargs)

        # Formatting
        if op_name == 'freq':
            stat = ', '.join([f"\"{str(row['value'])}\": {row['count']}" for row in stat])
            stat = '{ %s }' % stat

            if getattr(table, 'is_streaming', False) and table.columns[column_id].is_estimated(op_name):
                stat += ' (estimated)'

        if label:
            self.output_file.write('%3i. %s: %s\n' % (column_id + 1, column_name, stat))
        else:
            self.output_file.write(f'{stat}\n')

    def calculate_stats(self, table, column_id, **kwargs):
        """
        Calculate stats for all valid operations.
        """
        return {
            op_name: self._calculate_stat(table, column_id, op_name, op_data, **kwargs)
            for op_name, op_data in OPERATIONS.items()
        }

    def print_stats(self, table, column_ids, stats):
        """
        Print data for all statistics.
        """
        label_column_width = max(len(op_data['label']) for op_data in OPERATIONS.values())

        for column_id in column_ids:
            column_name = table.column_names[column_id]
            column = table.columns[column_id]
            column_stats = stats[column_id]

            self.output_file.write('%3i. "%s"\n\n' % (column_id + 1, column_name))

            for op_name, op_data in OPERATIONS.items():
                if column_stats[op_name] is None:
                    continue

                label = '{label:{label_column_width}}'.format(**{
                    'label_column_width': label_column_width,
                    'label': op_data['label'],
                })

                if op_name == 'freq':
                    frequencies = column_stats['freq']
                    frequency_estimated = getattr(table, 'is_streaming', False) and column.is_estimated(op_name)

                    for i, row in enumerate(frequencies):
                        if i == 0:
                            self.output_file.write(f'\t{label} ')
                        else:
                            self.output_file.write('\t{label:{label_column_width}} '.format(**{
                                'label_column_width': label_column_width,
                                'label': '',
                            }))

                        if isinstance(column.data_type, agate.Number):
                            v = row['value']

                            if self.is_finite_decimal(v):
                                v = format_decimal(v, self.args.decimal_format, self.args.no_grouping_separator)
                        else:
                            v = str(row['value'])

                        marker = ' (estimated)' if frequency_estimated and i == len(frequencies) - 1 else ''
                        self.output_file.write(f"{v} ({row['count']}x){marker}\n")

                    continue

                v = column_stats[op_name]

                if op_name == 'nulls' and v:
                    v = f'{v} (excluded from calculations)'
                if op_name == 'len':
                    v = f'{v} characters'

                self.output_file.write(f'\t{label} {v}\n')

            self.output_file.write('\n')

        self.output_file.write(f'Row count: {len(table.rows)}\n')

    def print_csv(self, table, column_ids, stats):
        """
        Print data for all statistics as a CSV table.
        """
        header = ['column_id', 'column_name'] + list(OPERATIONS)

        estimated = getattr(table, 'is_streaming', False)

        if estimated:
            header.append('estimated')

        writer = agate.csv.DictWriter(self.output_file, fieldnames=header)
        writer.writeheader()

        for row in self._rows(table, column_ids, stats):
            if 'freq' in row:
                row['freq'] = ', '.join([str(row['value']) for row in row['freq']])

            if estimated:
                row['estimated'] = ', '.join(row['estimated'])

            writer.writerow(row)

    def print_json(self, table, column_ids, stats):
        """
        Print data for all statistics as a JSON text.
        """
        data = list(self._rows(table, column_ids, stats))

        json.dump(data, self.output_file, default=default_float_decimal, ensure_ascii=False, indent=self.args.indent)

    def _rows(self, table, column_ids, stats):
        for column_id in column_ids:
            column_name = table.column_names[column_id]
            column_stats = stats[column_id]

            output_row = {'column_id': column_id + 1, 'column_name': column_name}
            for op_name, _op_data in OPERATIONS.items():
                if column_stats[op_name] is not None:
                    output_row[op_name] = column_stats[op_name]

            if getattr(table, 'is_streaming', False):
                output_row['estimated'] = [
                    op_name for op_name in OPERATIONS if table.columns[column_id].is_estimated(op_name)
                ]

            yield output_row


def format_decimal(d, f='%.3f', no_grouping_separator=False):
    formatted = locale.format_string(f, d, grouping=not no_grouping_separator)
    decimal_point = locale.localeconv()['decimal_point']
    if decimal_point and decimal_point in formatted:
        formatted = formatted.rstrip('0').rstrip(decimal_point)
    return formatted


def format_stat(value, op_name, args, estimated=False):
    """
    Format a statistic for output.
    """
    if value is None:
        return None

    if op_name == 'freq':
        return value

    if isinstance(value, Decimal) and value.is_finite() and not args.json_output:
        value = format_decimal(value, args.decimal_format, args.no_grouping_separator)

    if estimated and not (args.csv_output or args.json_output):
        value = f'{value} (estimated)'

    return value


# These are accessed via: globals().get(f'get_{op_name}')
def get_type(table, column_id, **kwargs):
    return f'{table.columns[column_id].data_type.__class__.__name__}'


def get_unique(table, column_id, **kwargs):
    return len(table.columns[column_id].values_distinct())


def get_freq(table, column_id, freq_count=5, **kwargs):
    values = table.columns[column_id].values()
    return [
        {'value': r[0], 'count': r[1]}
        for r in Counter(values).most_common(freq_count)
    ]


def launch_new_instance():
    utility = CSVStat()
    utility.run()


if __name__ == '__main__':
    launch_new_instance()
