#!/usr/bin/env python

import heapq
import itertools
import pickle
import shutil
import tempfile

import agate
from agate.utils import letter_name

from csvkit.cli import CSVKitUtility, parse_column_identifiers


def ignore_case_sort(key):

    def inner(row):
        return tuple(
            agate.NullOrder() if row[n] is None else (row[n].upper() if isinstance(row[n], str) else row[n])
            for n in key
        )

    return inner


def sequence_sort_key(key):

    def inner(row):
        return tuple(agate.NullOrder() if row[n] is None else row[n] for n in key)

    return inner


class ReverseOrder:
    """
    Wrapper that reverses the sort order of a key, so that a min-heap can merge in descending order
    while ties are still broken by chunk order (matching the stability of sorted(reverse=True)).
    """

    __slots__ = ('key',)

    def __init__(self, key):
        self.key = key

    def __lt__(self, other):
        return other.key < self.key

    def __eq__(self, other):
        return self.key == other.key


class CSVSort(CSVKitUtility):
    description = 'Sort CSV files. Like the Unix "sort" command, but for tabular data.'

    def add_arguments(self):
        self.argparser.add_argument(
            '-n', '--names', dest='names_only', action='store_true',
            help='Display column names and indices from the input CSV and exit.')
        self.argparser.add_argument(
            '-c', '--columns', dest='columns',
            help='A comma-separated list of column indices, names or ranges to sort by, e.g. "1,id,3-5". '
                 'Defaults to all columns.')
        self.argparser.add_argument(
            '-r', '--reverse', dest='reverse', action='store_true',
            help='Sort in descending order.')
        self.argparser.add_argument(
            '-i', '--ignore-case', dest='ignore_case', action='store_true',
            help='Perform case-independent sorting.')
        self.argparser.add_argument(
            '-y', '--snifflimit', dest='sniff_limit', type=int, default=1024,
            help='Limit CSV dialect sniffing to the specified number of bytes. '
                 'Specify "0" to disable sniffing entirely, or "-1" to sniff the entire file.')
        self.argparser.add_argument(
            '-I', '--no-inference', dest='no_inference', action='store_true',
            help='Disable type inference (and --locale, --date-format, --datetime-format, --no-leading-zeroes) '
                 'when parsing the input.')
        self.argparser.add_argument(
            '--max-rows', dest='max_rows', type=int, default=None,
            help='The maximum number of rows to sort in memory at once. If the input contains more rows, '
                 'it is sorted in chunks of this size which are spilled to temporary files and merged, '
                 'so files too large to fit in memory can be sorted. The output is identical to sorting '
                 'the entire file in a single pass.')

    def main(self):
        if self.args.names_only:
            self.print_column_names()
            return

        if self.additional_input_expected():
            self.argparser.error('You must provide an input file or piped data.')

        sniff_limit = self.args.sniff_limit if self.args.sniff_limit != -1 else None

        if self.args.max_rows is not None:
            if self.args.max_rows < 1:
                self.argparser.error('--max-rows must be at least 1.')

            self.external_sort(sniff_limit)
            return

        table = agate.Table.from_csv(
            self.input_file,
            skip_lines=self.args.skip_lines,
            sniff_limit=sniff_limit,
            column_types=self.get_column_types(),
            **self.reader_kwargs,
        )

        key = parse_column_identifiers(
            self.args.columns,
            table.column_names,
            self.get_column_offset(),
        )

        if self.args.ignore_case:
            key = ignore_case_sort(key)

        table = table.order_by(key, reverse=self.args.reverse)
        table.to_csv(self.output_file, **self.writer_kwargs)

    def external_sort(self, sniff_limit):
        """
        Sort the input in chunks that fit in memory, spilling each sorted chunk to a temporary file, then
        merge the chunks. Column types are inferred over the whole input before any chunk is sorted, and
        both the per-chunk sort and the merge are stable, so the output is identical to sorting the entire
        file in a single pass.
        """
        input_file = self.input_file
        spool = None

        try:
            seekable = input_file.seekable()
        except (AttributeError, OSError, ValueError):
            seekable = False

        if not seekable:
            # The input must be read twice (once to infer column types, once to sort), so non-seekable
            # input such as a pipe is first spooled to a temporary file.
            spool = tempfile.TemporaryFile(mode='w+', encoding='utf-8')
            shutil.copyfileobj(input_file, spool)
            spool.seek(0)
            input_file = spool

        spills = []

        try:
            # First pass: infer column types over the whole input, so that every chunk is cast and
            # ordered with identical types, whatever values happen to fall into each chunk.
            column_names, rows = self._read_rows(input_file, sniff_limit)
            column_types = self.get_column_types().run(rows, column_names)

            key = parse_column_identifiers(
                self.args.columns,
                column_names,
                self.get_column_offset(),
            )

            if self.args.ignore_case:
                key = ignore_case_sort(key)

            # Second pass: cast, sort and spill one chunk at a time.
            _, rows = self._read_rows(input_file, sniff_limit)

            chunk = []

            for row in rows:
                chunk.append(row)

                if len(chunk) >= self.args.max_rows:
                    spills.append(self._spill_chunk(chunk, column_names, column_types, key))
                    chunk = []

            if chunk:
                spills.append(self._spill_chunk(chunk, column_names, column_types, key))

            # Merge the sorted chunks with a k-way heap. Ties are broken by chunk order (and chunks
            # are stable-sorted), so rows with equal keys keep their original relative order, exactly
            # as sorted() would order them in a single pass, in either direction.
            if callable(key):
                merge_key = key
            else:
                merge_key = sequence_sort_key(key)

            if self.args.reverse:
                heap_key = lambda row: ReverseOrder(merge_key(row))  # noqa: E731
            else:
                heap_key = merge_key

            merged = self._merge_spills(spills, heap_key)

            writer_kwargs = dict(self.writer_kwargs)
            if 'lineterminator' not in writer_kwargs:
                writer_kwargs['lineterminator'] = '\n'

            writer = agate.csv.writer(self.output_file, **writer_kwargs)
            writer.writerow(column_names)

            csv_funcs = [c.csvify for c in column_types]

            for row in merged:
                writer.writerow(tuple(csv_funcs[i](d) for i, d in enumerate(row)))
        finally:
            for spill in spills:
                spill.close()

            if spool is not None:
                spool.close()

    def _read_rows(self, input_file, sniff_limit):
        """
        Rewind the input and return its column names and an iterator over its rows, applying the same
        skip_lines, sniffing and header handling as agate.Table.from_csv.
        """
        input_file.seek(0)

        skip_lines = self.args.skip_lines
        while skip_lines > 0:
            input_file.readline()
            skip_lines -= 1

        kwargs = dict(self.reader_kwargs)
        header = kwargs.pop('header', True)

        if sniff_limit is None:
            offset = input_file.tell()
            sample = input_file.read()
            input_file.seek(offset)
            kwargs['dialect'] = agate.csv.Sniffer().sniff(sample)
        elif sniff_limit > 0:
            offset = input_file.tell()
            sample = input_file.read(sniff_limit)
            input_file.seek(offset)
            kwargs['dialect'] = agate.csv.Sniffer().sniff(sample)

        reader = agate.csv.reader(input_file, header=header, **kwargs)

        if header:
            try:
                column_names = next(reader)
            except StopIteration:
                column_names = []

            return column_names, reader

        first_row = next(reader, None)

        if first_row is None:
            return [], reader

        column_names = [letter_name(i) for i in range(len(first_row))]

        return column_names, itertools.chain([first_row], reader)

    def _spill_chunk(self, chunk, column_names, column_types, key):
        """
        Cast and sort a chunk of rows exactly as the single-pass sort would, write it to a temporary
        file and return the file, positioned at the beginning.
        """
        table = agate.Table(chunk, column_names, column_types)
        table = table.order_by(key, reverse=self.args.reverse)

        spill = tempfile.TemporaryFile()

        for row in table.rows:
            # Rows are pickled so that typed values (numbers, dates, ...) survive the round trip
            # to disk exactly, with no change to their meaning for the merge comparisons.
            pickle.dump(tuple(row), spill)

        spill.seek(0)

        return spill

    @staticmethod
    def _iter_spill(spill):
        while True:
            try:
                yield pickle.load(spill)
            except EOFError:
                return

    @classmethod
    def _merge_spills(cls, spills, heap_key):
        heap = []

        for index, spill in enumerate(spills):
            iterator = cls._iter_spill(spill)
            row = next(iterator, None)

            if row is not None:
                # The chunk index breaks ties between equal keys, so the merge is stable.
                heapq.heappush(heap, (heap_key(row), index, row, iterator))

        while heap:
            _, index, row, iterator = heapq.heappop(heap)

            yield row

            row = next(iterator, None)

            if row is not None:
                heapq.heappush(heap, (heap_key(row), index, row, iterator))


def launch_new_instance():
    utility = CSVSort()
    utility.run()


if __name__ == '__main__':
    launch_new_instance()
