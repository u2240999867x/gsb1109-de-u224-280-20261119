#!/usr/bin/env python

import json
import sys
from collections import OrderedDict

import agate
from agate.exceptions import CastError

from csvkit.cli import (
    CSVKitUtility,
    default_str_decimal,
    match_column_identifier,
    parse_column_type_declarations,
    resolve_column_type_declarations,
)
from csvkit.exceptions import InvalidValueForColumnTypeException


class JSONValue(agate.DataType):
    """
    A pass-through data type for values that are already JSON-serializable.
    """
    def cast(self, d):
        return d

    def jsonify(self, d):
        return d


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
            '--column-types', '--column-type', dest='column_types', action='append',
            type=parse_column_type_declarations, default=[], metavar='COLUMN:TYPE',
            help='Declare the type of one or more columns, as comma-separated COLUMN:TYPE pairs, for example '
                 '"id:text,amount:number". COLUMN is a column name or a 1-based index subject to --zero. TYPE is '
                 'one of: boolean, date, datetime, number, text, timedelta. Declared columns are converted strictly '
                 'to their type, in both array and --stream output. Other columns are unaffected. This option can '
                 'be specified multiple times.')
        self.argparser.add_argument(
            '-y', '--snifflimit', dest='sniff_limit', type=int, default=1024,
            help='Limit CSV dialect sniffing to the specified number of bytes. '
                 'Specify "0" to disable sniffing entirely, or "-1" to sniff the entire file.')
        self.argparser.add_argument(
            '-I', '--no-inference', dest='no_inference', action='store_true',
            help='Disable type inference (and --locale, --date-format, --datetime-format, --no-leading-zeroes) '
                 'when parsing the input.')

    def get_column_type_declarations(self):
        declarations = []

        for value in self.args.column_types:
            declarations.extend(value)

        return declarations

    def has_column_type_declarations(self):
        return bool(self.args.column_types)

    def get_first_record_number(self):
        """
        Return the CSV record number of the first data record.

        Record numbers include the header row, if any, and any lines skipped with --skip-lines, so that an error
        can be located in the input file.
        """
        return self.args.skip_lines + (0 if self.args.no_header_row else 1) + 1

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

        self.json_kwargs = {
            'indent': self.args.indent,
        }

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

    def dump_json(self, data, newline=False):
        json.dump(data, self.output_file, default=default_str_decimal, ensure_ascii=False, **self.json_kwargs)
        if newline:
            self.output_file.write("\n")

    def can_stream(self):
        return (
            self.args.streamOutput
            and self.args.no_inference
            and self.args.sniff_limit == 0
            and not self.args.skip_lines
        )

    def is_geo(self):
        return self.args.lat and self.args.lon

    def get_declared_column_types(self, column_names):
        """
        Resolve --column-types against the header row, returning a dictionary mapping column indices to type names.
        """
        if not column_names:
            return {}

        return resolve_column_type_declarations(
            self.get_column_type_declarations(),
            column_names,
            self.get_column_offset(),
        )

    def to_json_value(self, value, column_name, type_name, record):
        """
        Convert a raw CSV value to its JSON value according to a declared type.

        Unlike type inference, a declared type is strict: a value that doesn't conform to the declared type is an
        error that identifies the input record and field, instead of being silently treated as text.
        """
        data_type = self.get_data_type(type_name)

        try:
            return data_type.jsonify(data_type.cast(value))
        except CastError as e:
            raise InvalidValueForColumnTypeException(record, column_name, value, type_name) from e

    def read_csv_to_table(self):
        sniff_limit = self.args.sniff_limit if self.args.sniff_limit != -1 else None
        return agate.Table.from_csv(
            self.input_file,
            skip_lines=self.args.skip_lines,
            sniff_limit=sniff_limit,
            column_types=self.get_column_types(self.get_column_type_declarations()),
            **self.reader_kwargs,
        )

    def read_csv_to_declared_table(self):
        """
        Read the CSV, strictly converting declared columns, and return a table.

        Declared columns hold their JSON values in pass-through columns. Undeclared columns keep their inferred
        types and values, so that they are output exactly as they are without --column-types.
        """
        table = self.read_csv_to_table()
        declared = self.get_declared_column_types(table.column_names)
        first_record = self.get_first_record_number()
        column_types = list(table.column_types)
        rows = []

        for index, row in enumerate(table.rows):
            record = first_record + index
            values = list(row)

            for i, type_name in declared.items():
                if i < len(values):
                    values[i] = self.to_json_value(values[i], table.column_names[i], type_name, record)

            rows.append(values)

        for i in declared:
            column_types[i] = JSONValue()

        return agate.Table(rows, table.column_names, column_types=column_types)

    def output_json(self):
        if self.has_column_type_declarations():
            table = self.read_csv_to_declared_table()
        else:
            table = self.read_csv_to_table()

        table.to_json(
            self.output_file,
            key=self.args.key,
            newline=self.args.streamOutput,
            indent=self.args.indent,
        )

    def output_geojson(self):
        if self.has_column_type_declarations():
            table = self.read_csv_to_declared_table()
        else:
            table = self.read_csv_to_table()

        geojson_generator = self.GeoJsonGenerator(self.args, table.column_names)

        if self.args.streamOutput:
            for row in table.rows:
                self.dump_json(geojson_generator.feature_for_row(row), newline=True)
        else:
            self.dump_json(geojson_generator.generate_feature_collection(table))

    def streaming_output_ndjson(self):
        rows = agate.csv.reader(self.input_file, **self.reader_kwargs)
        column_names = next(rows)
        declared = self.get_declared_column_types(column_names)
        first_record = self.get_first_record_number()

        for index, row in enumerate(rows):
            record = first_record + index
            data = OrderedDict()

            for i, column in enumerate(column_names):
                value = row[i] if i < len(row) else None

                if i in declared:
                    data[column] = self.to_json_value(value, column, declared[i], record)
                else:
                    data[column] = value

            self.dump_json(data, newline=True)

    def streaming_output_ndgeojson(self):
        rows = agate.csv.reader(self.input_file, **self.reader_kwargs)
        column_names = next(rows)
        declared = self.get_declared_column_types(column_names)
        first_record = self.get_first_record_number()
        geojson_generator = self.GeoJsonGenerator(self.args, column_names)

        for index, row in enumerate(rows):
            record = first_record + index
            values = list(row)

            for i, type_name in declared.items():
                if i < len(values):
                    values[i] = self.to_json_value(values[i], column_names[i], type_name, record)

            self.dump_json(geojson_generator.feature_for_row(values), newline=True)

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

        def generate_feature_collection(self, rows):
            features = []
            bounds = self.GeoJsonBounds()

            for row in rows:
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
