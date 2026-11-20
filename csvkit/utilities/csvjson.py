#!/usr/bin/env python

import csv
import io
import itertools
import json
import sys
from collections import OrderedDict

import agate
from agate.exceptions import CastError

from csvkit.cli import CSVKitUtility, default_str_decimal, make_default_headers, match_column_identifier
from csvkit.declaration import (
    jsonify_value,
    load_declaration,
    split_declaration_path,
)


class CSVJSON(CSVKitUtility):
    description = 'Convert a CSV file into JSON (or GeoJSON).'

    def add_arguments(self):
        self.argparser.add_argument(
            '-i', '--indent', dest='indent', type=int,
            help='Indent the output JSON this many spaces. Disabled by default.')
        self.argparser.add_argument(
            '-k', '--key', dest='key',
            help='Output JSON as an object keyed by a given column, KEY, rather than as an array. All column values '
                 'must be unique. If --lat and --lon are specified, this column is used as the GeoJSON Feature ID.')
        self.argparser.add_argument(
            '--lat', dest='lat',
            help='A column index or name containing a latitude. Output will be GeoJSON instead of JSON. '
                 'Requires --lon.')
        self.argparser.add_argument(
            '--lon', dest='lon',
            help='A column index or name containing a longitude. Output will be GeoJSON instead of JSON. '
                 'Requires --lat.')
        self.argparser.add_argument(
            '--type', dest='type',
            help='A column index or name containing a GeoJSON type. Output will be GeoJSON instead of JSON. '
                 'Requires --lat and --lon.')
        self.argparser.add_argument(
            '--geometry', dest='geometry',
            help='A column index or name containing a GeoJSON geometry. Output will be GeoJSON instead of JSON. '
                 'Requires --lat and --lon.')
        self.argparser.add_argument(
            '--crs', dest='crs',
            help='A coordinate reference system string to be included with GeoJSON output. Requires --lat and --lon.')
        self.argparser.add_argument(
            '--no-bbox', dest='no_bbox', action='store_true',
            help='Disable the calculation of a bounding box.')
        self.argparser.add_argument(
            '--stream', dest='streamOutput', action='store_true',
            help='Output JSON as a stream of newline-separated objects, rather than an as an array.')
        self.argparser.add_argument(
            '-y', '--snifflimit', dest='sniff_limit', type=int, default=1024,
            help='Limit CSV dialect sniffing to the specified number of bytes. '
                 'Specify "0" to disable sniffing entirely, or "-1" to sniff the entire file.')
        self.argparser.add_argument(
            '-I', '--no-inference', dest='no_inference', action='store_true',
            help='Disable type inference (and --locale, --date-format, --datetime-format, --no-leading-zeroes) '
                 'when parsing the input.')
        self.argparser.add_argument(
            '-s', '--schema', '--declaration', dest='declaration',
            help='A JSON or CSV column declaration that selects, renames and types fields. See web documentation.')
        self.argparser.add_argument(
            '--declaration-version', '--schema-version', dest='declaration_version',
            help='The version of the column declaration to apply, if the declaration describes multiple versions.')
        self.argparser.add_argument(
            '--rejected', '--rejects', '--reject-file', '--errors', dest='rejected',
            help='Write rejected records, with their source line range and the declared column, as CSV to this file.')
        self.argparser.add_argument(
            '--include-undeclared', dest='include_undeclared', action='store_true', default=None,
            help='Output input columns that the declaration does not declare (the default).')
        self.argparser.add_argument(
            '--exclude-undeclared', dest='include_undeclared', action='store_false',
            help='Output only the columns that the declaration declares.')

    def main(self):
        """
        Convert CSV to JSON.
        """
        if self.args.lat and not self.args.lon:
            self.argparser.error('--lon is required whenever --lat is specified.')
        if self.args.lon and not self.args.lat:
            self.argparser.error('--lat is required whenever --lon is specified.')

        if self.args.crs and not self.args.lat:
            self.argparser.error('--crs is only allowed when --lat and --lon are also specified.')
        if self.args.type and not self.args.lat:
            self.argparser.error('--type is only allowed when --lat and --lon are also specified.')
        if self.args.geometry and not self.args.lat:
            self.argparser.error('--geometry is only allowed when --lat and --lon are also specified.')

        if self.args.key and self.args.streamOutput and not (self.args.lat and self.args.lon):
            self.argparser.error('--key is only allowed with --stream when --lat and --lon are also specified.')

        if self.args.declaration and self.is_geo():
            self.argparser.error('--schema is not allowed with --lat and --lon.')

        if self.args.rejected and self.is_geo():
            self.argparser.error('--rejected is not allowed with --lat and --lon.')

        self.json_kwargs = {
            'indent': self.args.indent,
        }

        self.declaration = self.read_declaration()
        self.rejected_records = []
        self.rejected_file = None

        if self.args.rejected:
            self.rejected_file = open(self.args.rejected, 'w', encoding='utf-8', newline='')

        try:
            if self.can_stream():
                if self.additional_input_expected():
                    sys.stderr.write('No input file or piped data provided. Waiting for standard input:\n')

                if self.is_geo():
                    self.streaming_output_ndgeojson()
                else:
                    self.streaming_output_ndjson()
            else:
                if self.additional_input_expected():
                    self.argparser.error('You must provide an input file or piped data.')

                if self.is_geo():
                    self.output_geojson()
                else:
                    self.output_json()
        finally:
            if self.rejected_file is not None:
                self.write_rejected_file()
                self.rejected_file.close()
                self.rejected_file = None

    def read_declaration(self):
        if not self.args.declaration:
            return None

        path, version = split_declaration_path(self.args.declaration)
        if version is None:
            version = self.args.declaration_version

        declaration = load_declaration(path, encoding=self.args.encoding, version=version)
        if self.args.include_undeclared is not None:
            declaration.select_undeclared = self.args.include_undeclared
        return declaration

    def dump_json(self, data, newline=False):
        json.dump(data, self.output_file, default=default_str_decimal, ensure_ascii=False, **self.json_kwargs)
        if newline:
            self.output_file.write("\n")

    def can_stream(self):
        if self.args.streamOutput and self.declaration is not None:
            return True
        return (
            self.args.streamOutput
            and self.args.no_inference
            and self.args.sniff_limit == 0
            and not self.args.skip_lines
        )

    def is_geo(self):
        return self.args.lat and self.args.lon

    def sniff(self, sample):
        """
        Like agate, ignore an undetectable dialect and fall back to the standard CSV dialect.
        """
        if not sample:
            return None

        try:
            return agate.csv.Sniffer().sniff(sample)
        except csv.Error:  # "Could not determine delimiter"
            return None

    def csv_reader(self):
        """
        Return a reader over the input, skipping lines and sniffing the dialect like agate does.

        The data rows are consumed only once: sniffing reads at most the configured sample and seeks back, and
        reading the entire input is only done when the input is known to fit in memory (sniff_limit -1).
        """
        if self.args.skip_lines:
            self._skipped_lines = self.args.skip_lines
            self.skip_lines()
        else:
            self._skipped_lines = 0

        sniff_limit = self.args.sniff_limit if self.args.sniff_limit != -1 else None

        if sniff_limit == 0:
            return agate.csv.reader(self.input_file, **self.reader_kwargs)

        kwargs = dict(self.reader_kwargs)

        if sniff_limit is None:
            sample = self.input_file.read()
            dialect = self.sniff(sample)
            if dialect is not None:
                kwargs['dialect'] = dialect
            return agate.csv.reader(io.StringIO(sample, newline=''), **kwargs)

        if self.input_file == sys.stdin:
            sample = self.input_file.buffer.peek(sniff_limit).decode(self.args.encoding, 'ignore')[:sniff_limit]
            dialect = self.sniff(sample)
            if dialect is not None:
                kwargs['dialect'] = dialect
            return agate.csv.reader(self.input_file, **kwargs)

        if not self.input_file.seekable():
            # A pipe or process substitution (e.g. <(cmd)): read once into memory, since sniffing needs a sample
            # and the input can't be rewound.
            sample = self.input_file.read()
            dialect = self.sniff(sample[:sniff_limit] if sniff_limit > 0 else sample)
            if dialect is not None:
                kwargs['dialect'] = dialect
            return agate.csv.reader(io.StringIO(sample, newline=''), **kwargs)

        offset = self.input_file.tell()
        sample = self.input_file.read(sniff_limit)
        self.input_file.seek(offset)

        dialect = self.sniff(sample)
        if dialect is not None:
            kwargs['dialect'] = dialect
        return agate.csv.reader(self.input_file, **kwargs)

    def read_header(self, reader):
        try:
            row = next(reader)
        except StopIteration:
            return itertools.chain([], reader), [], 0

        if self.args.no_header_row:
            return itertools.chain([row], reader), make_default_headers(len(row)), 0
        return reader, row, reader.line_num

    def run_records(self):
        """
        Read the input once, yielding :code:`(data, row, start_line, end_line)` for each valid record.

        A record with a declared field that can't be cast is rejected in its entirety. Line numbers come from
        the CSV reader, so a valid multiline field doesn't shift the location of later records.
        """
        reader = self.csv_reader()
        rows, column_names, header_end = self.read_header(reader)

        resolved = None
        if self.declaration is not None:
            column_offset = 0 if self.args.zero_based else 1
            resolved = self.declaration.select_version(
                self.declaration.requested_version, column_names, column_offset, self.args
            )
            self.resolved_declaration = resolved

        skipped_lines = getattr(self, '_skipped_lines', 0)
        previous_line = header_end + skipped_lines

        for row in rows:
            start_line = previous_line + 1
            end_line = reader.line_num + skipped_lines
            previous_line = end_line

            if resolved is None:
                data = OrderedDict()
                for i, column in enumerate(column_names):
                    data[column] = row[i] if i < len(row) else None
            else:
                data, rejected_field = self.declared_record(row, resolved)
                if rejected_field is not None:
                    self.reject(rejected_field, row, start_line, end_line)
                    continue

            yield data, row, start_line, end_line

    def declared_record(self, row, resolved):
        data = OrderedDict()
        for field in resolved.fields:
            value = row[field.index] if field.index < len(row) else None

            data_type = field.data_type
            if data_type is not None:
                try:
                    value = data_type.cast(value)
                except CastError:
                    return None, field
                value = jsonify_value(data_type, value)

            data[field.name] = value

        return data, None

    def reject(self, field, row, start_line, end_line):
        column = self.rejected_column(field)

        value = row[field.index] if field.index < len(row) else ''
        type_name = field.declaration.type or 'declared type'
        message = f'Unable to convert "{value}" to type {type_name}'

        self.rejected_records.append({
            'start_line': start_line,
            'end_line': end_line,
            'column': column,
            'source_name': field.source_name,
            'field': field.name,
            'value': value,
            'msg': message,
            'row': row,
        })
        self.error_file.write(
            f'Rejected record at lines {start_line}-{end_line}: {message} '
            f'(column {column}, field "{field.name}").\n'
        )

    def write_rejected_file(self):
        writer = agate.csv.writer(self.rejected_file)
        writer.writerow(['line_start', 'line_end', 'column', 'source_name', 'field', 'value', 'msg'])
        for record in self.rejected_records:
            writer.writerow([
                record['start_line'], record['end_line'], record['column'], record['source_name'],
                record['field'], record['value'], record['msg'],
            ])

    def rejected_column(self, field):
        """
        The declared physical column, in the declaration's numbering.
        """
        if field.declaration.column is not None:
            return field.declaration.column
        return field.index + (0 if self.args.zero_based else 1)

    def read_csv_to_table(self):
        sniff_limit = self.args.sniff_limit if self.args.sniff_limit != -1 else None
        return agate.Table.from_csv(
            self.input_file,
            skip_lines=self.args.skip_lines,
            sniff_limit=sniff_limit,
            column_types=self.get_column_types(),
            **self.reader_kwargs,
        )

    def output_json(self):
        if self.declaration is None:
            self.read_csv_to_table().to_json(
                self.output_file,
                key=self.args.key,
                newline=self.args.streamOutput,
                indent=self.args.indent,
            )
            return

        records = list(self.run_records())
        if getattr(self.args, 'no_inference', False) is False:
            self.infer_undeclared_fields(records)

        if self.args.key:
            output = OrderedDict()
            for data, _row, _start, _end in records:
                k = str(data.get(self.args.key))
                if k in output:
                    raise ValueError('Value %s is not unique in the key column.' % k)
                output[k] = data
            self.dump_json(output)
        else:
            self.dump_json([data for data, _row, _start, _end in records])

    def infer_undeclared_fields(self, records):
        """
        In array output, undeclared fields retain csvjson's existing behavior: type inference, unless
        --no-inference is set. Streaming output doesn't infer, as before.
        """
        if not records:
            return

        resolved = self.resolved_declaration
        undeclared = [field for field in resolved.fields if not field.declared]
        if not undeclared:
            return

        rows = [row for _data, row, _start, _end in records]
        projected = [[row[field.index] if field.index < len(row) else None for field in undeclared] for row in rows]
        column_types = self.get_column_types().run(projected, [field.name for field in undeclared])

        for record, row in zip(records, rows):
            data = record[0]
            for field, data_type in zip(undeclared, column_types):
                value = row[field.index] if field.index < len(row) else None
                try:
                    data[field.name] = jsonify_value(data_type, data_type.cast(value))
                except CastError:
                    data[field.name] = value

    def output_geojson(self):
        table = self.read_csv_to_table()
        geojson_generator = self.GeoJsonGenerator(self.args, table.column_names)

        if self.args.streamOutput:
            for row in table.rows:
                self.dump_json(geojson_generator.feature_for_row(row), newline=True)
        else:
            self.dump_json(geojson_generator.generate_feature_collection(table))

    def streaming_output_ndjson(self):
        for data, _row, _start, _end in self.run_records():
            self.dump_json(data, newline=True)

    def streaming_output_ndgeojson(self):
        rows = agate.csv.reader(self.input_file, **self.reader_kwargs)
        column_names = next(rows)
        geojson_generator = self.GeoJsonGenerator(self.args, column_names)

        for row in rows:
            self.dump_json(geojson_generator.feature_for_row(row), newline=True)

    class GeoJsonGenerator:
        def __init__(self, args, column_names):
            self.args = args
            self.column_names = column_names

            self.lat_column = match_column_identifier(column_names, self.args.lat, self.args.zero_based)

            self.lon_column = match_column_identifier(column_names, self.args.lon, self.args.zero_based)

            if self.args.type:
                self.type_column = match_column_identifier(column_names, self.args.type, self.args.zero_based)
            else:
                self.type_column = None

            if self.args.geometry:
                self.geometry_column = match_column_identifier(column_names, self.args.geometry, self.args.zero_based)
            else:
                self.geometry_column = None

            if self.args.key:
                self.id_column = match_column_identifier(column_names, self.args.key, self.args.zero_based)
            else:
                self.id_column = None

        def generate_feature_collection(self, table):
            features = []
            bounds = self.GeoJsonBounds()

            for row in table.rows:
                feature = self.feature_for_row(row)

                if not self.args.no_bbox:
                    bounds.add_feature(feature)

                features.append(feature)

            items = [
                ('type', 'FeatureCollection'),
                ('features', features),
            ]

            if not self.args.no_bbox and bounds.min_lon is not None:
                items.insert(1, ('bbox', bounds.bbox()))

            if self.args.crs:
                items.append(('crs', OrderedDict([
                    ('type', 'name'),
                    ('properties', {
                        'name': self.args.crs,
                    }),
                ])))

            return OrderedDict(items)

        def feature_for_row(self, row):
            feature = OrderedDict([
                ('type', 'Feature'),
                ('properties', OrderedDict()),
            ])

            for i, c in enumerate(row):
                # Prevent "type" or geo fields from being added to properties.
                if c is None or i in (self.type_column, self.lat_column, self.lon_column, self.geometry_column):
                    continue
                if i == self.id_column:
                    feature['id'] = c
                elif c:
                    feature['properties'][self.column_names[i]] = c

            feature['geometry'] = self.geometry_for_row(row)

            return feature

        def geometry_for_row(self, row):
            lat = None
            lon = None

            if self.geometry_column is not None:
                return json.loads(row[self.geometry_column])

            if self.lat_column is not None and self.lon_column is not None:
                try:
                    lon = float(row[self.lon_column])
                    lat = float(row[self.lat_column])
                except (TypeError, ValueError):
                    lon = None
                    lat = None

            if lon is not None and lat is not None:
                return OrderedDict([
                    ('type', 'Point'),
                    ('coordinates', [lon, lat]),
                ])

        class GeoJsonBounds:
            def __init__(self):
                self.min_lon = None
                self.min_lat = None
                self.max_lon = None
                self.max_lat = None

            def bbox(self):
                return [self.min_lon, self.min_lat, self.max_lon, self.max_lat]

            def add_feature(self, feature):
                if feature.get('geometry') and 'coordinates' in feature['geometry']:
                    self.update_coordinates(feature['geometry']['coordinates'])

            def update_lat(self, lat):
                if self.min_lat is None or lat < self.min_lat:
                    self.min_lat = lat
                if self.max_lat is None or lat > self.max_lat:
                    self.max_lat = lat

            def update_lon(self, lon):
                if self.min_lon is None or lon < self.min_lon:
                    self.min_lon = lon
                if self.max_lon is None or lon > self.max_lon:
                    self.max_lon = lon

            def update_coordinates(self, coordinates):
                if len(coordinates) <= 3 and isinstance(coordinates[0], (float, int)):
                    self.update_lon(coordinates[0])
                    self.update_lat(coordinates[1])
                else:
                    for coordinate in coordinates:
                        self.update_coordinates(coordinate)


def launch_new_instance():
    utility = CSVJSON()
    utility.run()


if __name__ == '__main__':
    launch_new_instance()
