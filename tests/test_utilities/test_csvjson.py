import io
import json
import os
import sys
import tempfile

import agate
from unittest.mock import patch

from csvkit.declaration import DeclarationError
from csvkit.utilities.csvjson import CSVJSON, launch_new_instance
from tests.utils import CSVKitTestCase, EmptyFileTests


class TestCSVJSON(CSVKitTestCase, EmptyFileTests):
    Utility = CSVJSON

    def test_launch_new_instance(self):
        with patch.object(sys, 'argv', [self.Utility.__name__.lower(), 'examples/dummy.csv']):
            launch_new_instance()

    def test_options(self):
        self.assertError(
            launch_new_instance,
            ['--key', 'value', '--stream'],
            '--key is only allowed with --stream when --lat and --lon are also specified.',
        )

    def test_latlon_options(self):
        for option, message in (
            ('lat', '--lon is required whenever --lat is specified.'),
            ('lon', '--lat is required whenever --lon is specified.'),
            ('crs', '--crs is only allowed when --lat and --lon are also specified.'),
            ('type', '--type is only allowed when --lat and --lon are also specified.'),
            ('geometry', '--geometry is only allowed when --lat and --lon are also specified.'),
        ):
            with self.subTest(option=option):
                self.assertError(launch_new_instance, [f'--{option}', 'value'], message)

    def test_simple(self):
        js = json.loads(self.get_output(['examples/dummy.csv']))
        self.assertDictEqual(js[0], {'a': True, 'c': 3.0, 'b': 2.0})

    def test_sniff_limit(self):
        js = json.loads(self.get_output(['examples/sniff_limit.csv']))
        self.assertDictEqual(js[0], {'a': True, 'c': 3.0, 'b': 2.0})

    def test_tsv(self):
        js = json.loads(self.get_output(['examples/dummy.tsv']))
        self.assertDictEqual(js[0], {'a': True, 'c': 3.0, 'b': 2.0})

    def test_tsv_streaming(self):
        js = json.loads(self.get_output(['--stream', '--no-inference',
                        '--snifflimit', '0', '--tabs', 'examples/dummy.tsv']))
        self.assertDictEqual(js, {'a': '1', 'c': '3', 'b': '2'})

    def test_no_blanks(self):
        js = json.loads(self.get_output(['examples/blanks.csv']))
        self.assertDictEqual(js[0], {'a': None, 'b': None, 'c': None, 'd': None, 'e': None, 'f': None})

    def test_blanks(self):
        js = json.loads(self.get_output(['--blanks', 'examples/blanks.csv']))
        self.assertDictEqual(js[0], {'a': '', 'b': 'NA', 'c': 'N/A', 'd': 'NONE', 'e': 'NULL', 'f': '.'})

    def test_no_header_row(self):
        js = json.loads(self.get_output(['--no-header-row', 'examples/no_header_row.csv']))
        self.assertDictEqual(js[0], {'a': True, 'c': 3.0, 'b': 2.0})

    def test_no_inference(self):
        js = json.loads(self.get_output(['--no-inference', 'examples/dummy.csv']))
        self.assertDictEqual(js[0], {'a': '1', 'c': '3', 'b': '2'})

    def test_indentation(self):
        output = self.get_output(['-i', '4', 'examples/dummy.csv'])
        js = json.loads(output)
        self.assertDictEqual(js[0], {'a': True, 'c': 3.0, 'b': 2.0})
        self.assertRegex(output, '        "a": true,')

    def test_keying(self):
        js = json.loads(self.get_output(['-k', 'a', 'examples/dummy.csv']))
        self.assertDictEqual(js, {'True': {'a': True, 'c': 3.0, 'b': 2.0}})

    def test_duplicate_keys(self):
        output_file = io.StringIO()
        utility = CSVJSON(['-k', 'a', 'examples/dummy3.csv'], output_file)
        self.assertRaisesRegex(ValueError,
                               'Value True is not unique in the key column.',
                               utility.run)
        output_file.close()

    def test_geojson_with_id(self):
        geojson = json.loads(self.get_output(['--lat', 'latitude', '--lon',
                             'longitude', '-k', 'slug', 'examples/test_geo.csv']))

        self.assertEqual(geojson['type'], 'FeatureCollection')
        self.assertNotIn('crs', geojson)
        self.assertEqual(geojson['bbox'], [-95.334619, 32.299076986939205, -95.250699, 32.351434])
        self.assertEqual(len(geojson['features']), 17)

        for feature in geojson['features']:
            self.assertEqual(feature['type'], 'Feature')
            self.assertIn('id', feature)
            self.assertIn('properties', feature)
            self.assertIsInstance(feature['properties'], dict)
            self.assertGreater(len(feature['properties']), 1)

            geometry = feature['geometry']

            self.assertEqual(len(geometry['coordinates']), 2)
            self.assertIsInstance(geometry['coordinates'][0], float)
            self.assertIsInstance(geometry['coordinates'][1], float)

    def test_geojson_point(self):
        geojson = json.loads(self.get_output(['--lat', 'latitude', '--lon', 'longitude', 'examples/test_geo.csv']))

        self.assertEqual(geojson['type'], 'FeatureCollection')
        self.assertNotIn('crs', geojson)
        self.assertEqual(geojson['bbox'], [-95.334619, 32.299076986939205, -95.250699, 32.351434])
        self.assertEqual(len(geojson['features']), 17)

        for feature in geojson['features']:
            self.assertEqual(feature['type'], 'Feature')
            self.assertNotIn('id', feature)
            self.assertIn('properties', feature)
            self.assertIsInstance(feature['properties'], dict)
            self.assertGreater(len(feature['properties']), 1)

            geometry = feature['geometry']

            self.assertEqual(len(geometry['coordinates']), 2)
            self.assertIsInstance(geometry['coordinates'][0], float)
            self.assertIsInstance(geometry['coordinates'][1], float)

    def test_geojson_no_coordinates(self):
        geojson = json.loads(self.get_output(['--lat', 'latitude', '--lon', 'longitude',
                             'examples/test_geo_no_coordinates.csv']))

        self.assertNotIn('bbox', geojson)

    def test_geojson_missing_coordinates(self):
        geojson = json.loads(self.get_output(['--lat', 'latitude', '--lon', 'longitude',
                             'examples/test_geo_null.csv']))

        self.assertEqual(geojson['bbox'], [-95.28174, 32.33396, -95.28174, 32.33396])
        self.assertEqual(len(geojson['features']), 2)
        self.assertIsNone(geojson['features'][1]['geometry'])

    def test_geojson_zero_coordinates(self):
        geojson = json.loads(self.get_output(['--lat', 'latitude', '--lon', 'longitude',
                             'examples/test_geo_zero.csv']))

        self.assertEqual(geojson['bbox'], [0.0, 0.0, 2.35, 51.48])
        self.assertEqual(len(geojson['features']), 3)
        self.assertEqual(geojson['features'][0]['geometry']['coordinates'], [0.0, 0.0])
        self.assertEqual(geojson['features'][1]['geometry']['coordinates'], [0.0, 51.48])
        self.assertEqual(geojson['features'][2]['geometry']['coordinates'], [2.35, 0.0])

    def test_geojson_shape(self):
        geojson = json.loads(self.get_output(['--lat', 'latitude', '--lon', 'longitude',
                             '--type', 'type', '--geometry', 'geojson', 'examples/test_geojson.csv']))

        self.assertEqual(geojson['type'], 'FeatureCollection')
        self.assertNotIn('crs', geojson)
        self.assertEqual(geojson['bbox'], [100.0, 0.0, 105.0, 1.0])
        self.assertEqual(len(geojson['features']), 3)

        for feature in geojson['features']:
            self.assertEqual(feature['type'], 'Feature')
            self.assertNotIn('id', feature)
            self.assertIn('properties', feature)
            self.assertIsInstance(feature['properties'], dict)
            self.assertIn('prop0', feature['properties'].keys())

            geometry = feature['geometry']

            self.assertIn('coordinates', geometry)
            self.assertIsNotNone(geometry['coordinates'])

        self.assertEqual(geojson['features'][0]['geometry']['type'], 'Point')
        self.assertEqual(geojson['features'][1]['geometry']['type'], 'LineString')
        self.assertEqual(geojson['features'][2]['geometry']['type'], 'Polygon')

        self.assertEqual(geojson['features'][0]['geometry']['coordinates'], [102.0, 0.5])
        self.assertEqual(geojson['features'][1]['geometry']['coordinates'],
                         [[102.0, 0.0], [103.0, 1.0], [104.0, 0.0], [105.0, 1.0]])
        self.assertEqual(geojson['features'][2]['geometry']['coordinates'],
                         [[[100.0, 0.0], [101.0, 0.0], [101.0, 1.0], [100.0, 1.0], [100.0, 0.0]]])

    def test_geojson_with_crs(self):
        geojson = json.loads(self.get_output(['--lat', 'latitude', '--lon',
                             'longitude', '--crs', 'EPSG:4269', 'examples/test_geo.csv']))
        self.assertIn('crs', geojson)
        self.assertEqual(geojson['crs'], {'type': 'name', 'properties': {'name': 'EPSG:4269'}})

    def test_geojson_with_no_bbox(self):
        geojson = json.loads(self.get_output(['--lat', 'latitude', '--lon',
                             'longitude', '--no-bbox', 'examples/test_geo.csv']))
        self.assertNotIn('bbox', geojson)

    def test_ndjson(self):
        self.assertLines(['--stream', 'examples/testjson_converted.csv'], [
            '{"text": "Chicago Reader", "float": 1.0, "datetime": "1971-01-01T04:14:00", "boolean": true, "time": "4:14:00", "date": "1971-01-01", "integer": 40.0}',  # noqa: E501
            '{"text": "Chicago Sun-Times", "float": 1.27, "datetime": "1948-01-01T14:57:13", "boolean": true, "time": "14:57:13", "date": "1948-01-01", "integer": 63.0}',  # noqa: E501
            '{"text": "Chicago Tribune", "float": 41800000.01, "datetime": "1920-01-01T00:00:00", "boolean": false, "time": "0:00:00", "date": "1920-01-01", "integer": 164.0}',  # noqa: E501
            '{"text": "This row has blanks", "float": null, "datetime": null, "boolean": null, "time": null, "date": null, "integer": null}',  # noqa: E501
            '{"text": "Unicode! Σ", "float": null, "datetime": null, "boolean": null, "time": null, "date": null, "integer": null}',  # noqa: E501
        ])

    def test_ndjson_streaming(self):
        self.assertLines(['--stream', '--no-inference', '--snifflimit', '0', 'examples/testjson_converted.csv'], [
            '{"text": "Chicago Reader", "float": "1.0", "datetime": "1971-01-01T04:14:00", "boolean": "True", "time": "4:14:00", "date": "1971-01-01", "integer": "40"}',  # noqa: E501
            '{"text": "Chicago Sun-Times", "float": "1.27", "datetime": "1948-01-01T14:57:13", "boolean": "True", "time": "14:57:13", "date": "1948-01-01", "integer": "63"}',  # noqa: E501
            '{"text": "Chicago Tribune", "float": "41800000.01", "datetime": "1920-01-01T00:00:00", "boolean": "False", "time": "0:00:00", "date": "1920-01-01", "integer": "164"}',  # noqa: E501
            '{"text": "This row has blanks", "float": "", "datetime": "", "boolean": "", "time": "", "date": "", "integer": ""}',  # noqa: E501
            '{"text": "Unicode! Σ", "float": "", "datetime": "", "boolean": "", "time": "", "date": "", "integer": ""}',  # noqa: E501
        ])

    def test_ndgeojson(self):
        self.maxDiff = 5000
        self.assertLines(['--lat', 'latitude', '--lon', 'longitude', '--stream', 'examples/test_geo.csv'], [
            '{"type": "Feature", "properties": {"slug": "dcl", "title": "Downtown Coffee Lounge", "description": "In addition to being the only coffee shop in downtown Tyler, DCL also features regular exhibitions of work by local artists.", "address": "200 West Erwin Street", "type": "Gallery", "last_seen_date": "2012-03-30"}, "geometry": {"type": "Point", "coordinates": [-95.30181, 32.35066]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "tyler-museum", "title": "Tyler Museum of Art", "description": "The Tyler Museum of Art on the campus of Tyler Junior College is the largest art museum in Tyler. Visit them online at <a href=\\"http://www.tylermuseum.org/\\">http://www.tylermuseum.org/</a>.", "address": "1300 South Mahon Avenue", "type": "Museum", "last_seen_date": "2012-04-02"}, "geometry": {"type": "Point", "coordinates": [-95.28174, 32.33396]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "genecov", "title": "Genecov Sculpture", "description": "Stainless Steel Sculpture", "address": "1350 Dominion Plaza", "type": "Sculpture", "photo_url": "http://i.imgur.com/DICdn.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "2012-04-04"}, "geometry": {"type": "Point", "coordinates": [-95.31571447849274, 32.299076986939205]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "gallery-main-street", "title": "Gallery Main Street", "description": "The only dedicated art gallery in Tyler. Visit them online at <a href=\\"http://www.heartoftyler.com/downtowntylerarts/\\">http://www.heartoftyler.com/downtowntylerarts/</a>.", "address": "110 West Erwin Street", "type": "Gallery", "last_seen_date": "2012-04-09"}, "geometry": {"type": "Point", "coordinates": [-95.30123, 32.35066]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "spirit-of-progress", "title": "The Spirit of Progress", "address": "100 block of North Spring Avenue", "type": "Relief", "photo_url": "http://media.hacktyler.com/artmap/photos/spirit-of-progress.jpg", "photo_credit": "Photo by Christopher Groskopf. Used with permission.", "last_seen_date": "2012-04-11"}, "geometry": {"type": "Point", "coordinates": [-95.2995, 32.3513]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "celestial-conversations-2", "title": "Celestial Conversations #2", "artist": "Simon Saleh", "description": "Steel Sculpture", "address": "100 block of North Spring Avenue", "type": "Sculpture", "photo_url": "http://media.hacktyler.com/artmap/photos/celestial-conversations-2.jpg", "photo_credit": "Photo by Christopher Groskopf. Used with permission.", "last_seen_date": "2012-04-11"}, "geometry": {"type": "Point", "coordinates": [-95.2995, 32.351]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "pivot-bounce", "title": "Pivot Bounce", "artist": "Chelsea Cope", "address": "100 block of North Spring Avenue", "type": "Sculpture", "photo_url": "http://i.imgur.com/pmxyi.jpg?1", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "2012-04-11"}, "geometry": {"type": "Point", "coordinates": [-95.29944, 32.351434]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "children-of-peace", "title": "Children of Peace", "artist": "Gary Price", "description": "Cast Bronze", "address": "900 South Broadway Avenue", "type": "Sculpture", "photo_url": "http://i.imgur.com/rUikO.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "2012-04-15"}, "geometry": {"type": "Point", "coordinates": [-95.300222, 32.339826]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "ross-bears", "title": "Ross\' Bears", "description": "Granite Sculpture", "address": "900 South Broadway Avenue", "type": "Sculpture", "photo_url": "http://i.imgur.com/SpJQI.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "2012-04-15"}, "geometry": {"type": "Point", "coordinates": [-95.300034, 32.339776]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "butterfly-garden", "title": "Butterfly Garden", "description": "Cast Bronze", "type": "Sculpture", "photo_url": "http://i.imgur.com/0L8DF.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "2012-04-15"}, "geometry": {"type": "Point", "coordinates": [-95.300219, 32.339559]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "goose-fountain", "title": "TJC Goose Fountain", "description": "Copper (?) Sculpture", "address": "1300 S. Mahon Ave.", "type": "Sculpture", "photo_url": "http://i.imgur.com/UWfS6.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "2012-04-15"}, "geometry": {"type": "Point", "coordinates": [-95.28263, 32.333944]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "tjc-cement-totems", "description": "Cast Cement Totems", "address": "1300 S. Mahon Ave.", "type": "Sculpture", "photo_url": "http://i.imgur.com/lRmYd.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "2012-04-15"}, "geometry": {"type": "Point", "coordinates": [-95.283894, 32.333899]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "alison", "title": "Alison", "description": "Cast Bronze", "address": "900 South Broadway Avenue", "type": "Sculpture", "photo_url": "http://i.imgur.com/7OcrG.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "2012-04-15"}, "geometry": {"type": "Point", "coordinates": [-95.299887, 32.339809]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "jackson", "title": "Jackson", "description": "Cast Bronze", "address": "900 South Broadway Avenue", "type": "Sculpture", "photo_url": "http://i.imgur.com/aQJfv.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "2012-04-15"}, "geometry": {"type": "Point", "coordinates": [-95.299939, 32.339828]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "505-third", "title": "Untitled", "description": "Stainless Steel", "address": "505 Third St.", "type": "Sculpture", "photo_url": "http://i.imgur.com/0moUY.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "2012-04-15"}, "geometry": {"type": "Point", "coordinates": [-95.305429, 32.333082]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "obeidder", "title": "Obeidder Monster", "description": "Sharpie and Spray Paint", "address": "3319 Seaton St.", "type": "Street Art", "photo_url": "http://i.imgur.com/3aX7E.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "2012-04-15"}, "geometry": {"type": "Point", "coordinates": [-95.334619, 32.314431]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "sensor-device", "title": "Sensor Device", "artist": "Kurt Dyrhaug", "address": "University of Texas, Campus Drive", "type": "Sculpture", "photo_url": "http://media.hacktyler.com/artmap/photos/sensor-device.jpg", "photo_credit": "Photo by Christopher Groskopf. Used with permission.", "last_seen_date": "2012-04-16"}, "geometry": {"type": "Point", "coordinates": [-95.250699, 32.317216]}}',  # noqa: E501
        ])

    def test_ndgeojson_streaming(self):
        self.maxDiff = 5000
        self.assertLines(['--stream', '--no-inference', '--snifflimit', '0', '--lat', 'latitude', '--lon', 'longitude',
                          '--stream', 'examples/test_geo.csv'], [
            '{"type": "Feature", "properties": {"slug": "dcl", "title": "Downtown Coffee Lounge", "description": "In addition to being the only coffee shop in downtown Tyler, DCL also features regular exhibitions of work by local artists.", "address": "200 West Erwin Street", "type": "Gallery", "last_seen_date": "3/30/12"}, "geometry": {"type": "Point", "coordinates": [-95.30181, 32.35066]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "tyler-museum", "title": "Tyler Museum of Art", "description": "The Tyler Museum of Art on the campus of Tyler Junior College is the largest art museum in Tyler. Visit them online at <a href=\\"http://www.tylermuseum.org/\\">http://www.tylermuseum.org/</a>.", "address": "1300 South Mahon Avenue", "type": "Museum", "last_seen_date": "4/2/12"}, "geometry": {"type": "Point", "coordinates": [-95.28174, 32.33396]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "genecov", "title": "Genecov Sculpture", "description": "Stainless Steel Sculpture", "address": "1350 Dominion Plaza", "type": "Sculpture", "photo_url": "http://i.imgur.com/DICdn.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "4/4/12"}, "geometry": {"type": "Point", "coordinates": [-95.31571447849274, 32.299076986939205]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "gallery-main-street", "title": "Gallery Main Street", "description": "The only dedicated art gallery in Tyler. Visit them online at <a href=\\"http://www.heartoftyler.com/downtowntylerarts/\\">http://www.heartoftyler.com/downtowntylerarts/</a>.", "address": "110 West Erwin Street", "type": "Gallery", "last_seen_date": "4/9/12"}, "geometry": {"type": "Point", "coordinates": [-95.30123, 32.35066]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "spirit-of-progress", "title": "The Spirit of Progress", "address": "100 block of North Spring Avenue", "type": "Relief", "photo_url": "http://media.hacktyler.com/artmap/photos/spirit-of-progress.jpg", "photo_credit": "Photo by Christopher Groskopf. Used with permission.", "last_seen_date": "4/11/12"}, "geometry": {"type": "Point", "coordinates": [-95.2995, 32.3513]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "celestial-conversations-2", "title": "Celestial Conversations #2", "artist": "Simon Saleh", "description": "Steel Sculpture", "address": "100 block of North Spring Avenue", "type": "Sculpture", "photo_url": "http://media.hacktyler.com/artmap/photos/celestial-conversations-2.jpg", "photo_credit": "Photo by Christopher Groskopf. Used with permission.", "last_seen_date": "4/11/12"}, "geometry": {"type": "Point", "coordinates": [-95.2995, 32.351]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "pivot-bounce", "title": "Pivot Bounce", "artist": "Chelsea Cope", "address": "100 block of North Spring Avenue", "type": "Sculpture", "photo_url": "http://i.imgur.com/pmxyi.jpg?1", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "4/11/12"}, "geometry": {"type": "Point", "coordinates": [-95.29944, 32.351434]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "children-of-peace", "title": "Children of Peace", "artist": "Gary Price", "description": "Cast Bronze", "address": "900 South Broadway Avenue", "type": "Sculpture", "photo_url": "http://i.imgur.com/rUikO.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "4/15/12"}, "geometry": {"type": "Point", "coordinates": [-95.300222, 32.339826]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "ross-bears", "title": "Ross\' Bears", "description": "Granite Sculpture", "address": "900 South Broadway Avenue", "type": "Sculpture", "photo_url": "http://i.imgur.com/SpJQI.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "4/15/12"}, "geometry": {"type": "Point", "coordinates": [-95.300034, 32.339776]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "butterfly-garden", "title": "Butterfly Garden", "description": "Cast Bronze", "type": "Sculpture", "photo_url": "http://i.imgur.com/0L8DF.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "4/15/12"}, "geometry": {"type": "Point", "coordinates": [-95.300219, 32.339559]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "goose-fountain", "title": "TJC Goose Fountain", "description": "Copper (?) Sculpture", "address": "1300 S. Mahon Ave.", "type": "Sculpture", "photo_url": "http://i.imgur.com/UWfS6.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "4/15/12"}, "geometry": {"type": "Point", "coordinates": [-95.28263, 32.333944]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "tjc-cement-totems", "description": "Cast Cement Totems", "address": "1300 S. Mahon Ave.", "type": "Sculpture", "photo_url": "http://i.imgur.com/lRmYd.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "4/15/12"}, "geometry": {"type": "Point", "coordinates": [-95.283894, 32.333899]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "alison", "title": "Alison", "description": "Cast Bronze", "address": "900 South Broadway Avenue", "type": "Sculpture", "photo_url": "http://i.imgur.com/7OcrG.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "4/15/12"}, "geometry": {"type": "Point", "coordinates": [-95.299887, 32.339809]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "jackson", "title": "Jackson", "description": "Cast Bronze", "address": "900 South Broadway Avenue", "type": "Sculpture", "photo_url": "http://i.imgur.com/aQJfv.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "4/15/12"}, "geometry": {"type": "Point", "coordinates": [-95.299939, 32.339828]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "505-third", "title": "Untitled", "description": "Stainless Steel", "address": "505 Third St.", "type": "Sculpture", "photo_url": "http://i.imgur.com/0moUY.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "4/15/12"}, "geometry": {"type": "Point", "coordinates": [-95.305429, 32.333082]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "obeidder", "title": "Obeidder Monster", "description": "Sharpie and Spray Paint", "address": "3319 Seaton St.", "type": "Street Art", "photo_url": "http://i.imgur.com/3aX7E.jpg", "photo_credit": "Photo by Justin Edwards. Used with permission.", "last_seen_date": "4/15/12"}, "geometry": {"type": "Point", "coordinates": [-95.334619, 32.314431]}}',  # noqa: E501
            '{"type": "Feature", "properties": {"slug": "sensor-device", "title": "Sensor Device", "artist": "Kurt Dyrhaug", "address": "University of Texas, Campus Drive", "type": "Sculpture", "photo_url": "http://media.hacktyler.com/artmap/photos/sensor-device.jpg", "photo_credit": "Photo by Christopher Groskopf. Used with permission.", "last_seen_date": "4/16/12"}, "geometry": {"type": "Point", "coordinates": [-95.250699, 32.317216]}}',  # noqa: E501
        ])

    def test_declaration_streaming(self):
        js = [json.loads(line) for line in self.get_output(
            ['--schema', 'examples/test_declaration.json', '--stream', 'examples/test_declared.csv']
        ).splitlines()]

        self.assertEqual(js, [
            {'number': '1', 'external_id': '00042', 'amount': 1234.56, 'note': '第一行，含合法换行\n第二行'},
            {'number': '2', 'external_id': '00043', 'amount': 12345.67, 'note': '正常'},
            {'number': '4', 'external_id': '00045', 'amount': 9876.54, 'note': '继续'},
        ])

    def test_declaration_array(self):
        js = json.loads(self.get_output(['--schema', 'examples/test_declaration.json', 'examples/test_declared.csv']))

        self.assertEqual(js, [
            {'number': '1', 'external_id': '00042', 'amount': 1234.56, 'note': '第一行，含合法换行\n第二行'},
            {'number': '2', 'external_id': '00043', 'amount': 12345.67, 'note': '正常'},
            {'number': '4', 'external_id': '00045', 'amount': 9876.54, 'note': '继续'},
        ])

    def test_declaration_streaming_and_array_agree(self):
        args = ['--schema', 'examples/test_declaration.json', 'examples/test_declared.csv']

        lines = [
            json.loads(line) for line in self.get_output(args + ['--stream']).splitlines()
        ]
        array = json.loads(self.get_output(args))

        self.assertEqual(lines, array)

    def test_declaration_multiline_does_not_add_records(self):
        js = json.loads(self.get_output(['--schema', 'examples/test_declaration.json', 'examples/test_declared.csv']))

        # Four data records, one of which is rejected, and one of which contains a newline.
        self.assertEqual(len(js), 3)
        self.assertEqual(js[0]['note'], '第一行，含合法换行\n第二行')

    def run_with_files(self, args, rejected=None):
        output_file = io.TextIOWrapper(io.BytesIO(), encoding='utf-8', newline='', write_through=True)

        utility = CSVJSON(args, output_file)
        utility.run()

        output = output_file.buffer.getvalue().decode('utf-8')
        output_file.close()

        rows = None
        if rejected:
            with open(rejected, encoding='utf-8', newline='') as f:
                rows = list(agate.csv.reader(f))

        return output, rows

    def test_declaration_rejected_records(self):
        with tempfile.TemporaryDirectory() as directory:
            rejected = os.path.join(directory, 'rejected.csv')
            _output, rows = self.run_with_files(
                ['--schema', 'examples/test_declaration.json', '--stream', '--rejected', rejected,
                 'examples/test_declared.csv'],
                rejected,
            )

        self.assertEqual(rows[0], ['line_start', 'line_end', 'column', 'source_name', 'field', 'value', 'msg'])
        self.assertEqual(rows[1][:6], ['5', '5', '3', '金额', 'amount', '1.234,56,7'])

    def test_declaration_both_modes_reject_identically(self):
        with tempfile.TemporaryDirectory() as directory:
            stream_rejected = os.path.join(directory, 'stream.csv')
            array_rejected = os.path.join(directory, 'array.csv')

            _stream, stream_rows = self.run_with_files(
                ['--schema', 'examples/test_declaration.json', '--stream', '--rejected', stream_rejected,
                 'examples/test_declared.csv'],
                stream_rejected,
            )
            _array, array_rows = self.run_with_files(
                ['--schema', 'examples/test_declaration.json', '--rejected', array_rejected,
                 'examples/test_declared.csv'],
                array_rejected,
            )

        self.assertEqual(stream_rows, array_rows)

    def test_declaration_rejection_does_not_shift_later_records(self):
        with tempfile.TemporaryDirectory() as directory:
            rejected = os.path.join(directory, 'rejected.csv')
            output, _rows = self.run_with_files(
                ['--schema', 'examples/test_declaration.json', '--stream', '--rejected', rejected,
                 'examples/test_declared.csv'],
                rejected,
            )

        # The record on lines 5-5 is rejected, and the later valid record is still output.
        self.assertIn('"number": "4"', output)

    def test_declaration_with_csv_file(self):
        with tempfile.TemporaryDirectory() as directory:
            declaration = os.path.join(directory, 'declaration.csv')
            with open(declaration, 'w', encoding='utf-8', newline='') as f:
                f.write(
                    'version,name,column,type,locale\n'
                    'v2,number,1,text,\n'
                    'v2,external_id,2,text,\n'
                    'v2,amount,3,number,de_DE\n'
                    'v2,note,4,text,\n'
                )

            js = json.loads(self.get_output(['--schema', declaration, 'examples/test_declared.csv']))

        self.assertEqual(js[0]['external_id'], '00042')
        self.assertEqual(js[0]['amount'], 1234.56)

    def test_declaration_version(self):
        with tempfile.TemporaryDirectory() as directory:
            input_path = os.path.join(directory, 'v1.csv')
            with open(input_path, 'w', encoding='utf-8', newline='') as f:
                f.write('编号,金额\n1,"1.234,56"\n2,"12.000,00"\n')

            js = json.loads(self.get_output([
                '--schema', 'examples/test_declaration.json', '--declaration-version', 'v1', input_path,
            ]))

        self.assertEqual(js[0], {'number': '1', 'amount': 1234.56})

    def test_declaration_missing_physical_column(self):
        with tempfile.TemporaryDirectory() as directory:
            declaration = os.path.join(directory, 'declaration.json')
            with open(declaration, 'w', encoding='utf-8') as f:
                json.dump({'fields': [
                    {'name': 'number', 'column': 1},
                    {'name': 'amount', 'column': 9, 'type': 'number'},
                ]}, f)

            utility = CSVJSON(
                ['--schema', declaration, '--stream', 'examples/test_declared.csv'],
                io.StringIO(),
            )

            with self.assertRaisesRegex(DeclarationError, 'physical column 9 is missing'):
                utility.run()

    def test_declaration_skips_missing_optional_field(self):
        with tempfile.TemporaryDirectory() as directory:
            declaration = os.path.join(directory, 'declaration.csv')
            with open(declaration, 'w', encoding='utf-8', newline='') as f:
                f.write('name,column,type,required\nnumber,1,text,true\nmissing,9,text,false\n')

            js = json.loads(self.get_output(['--schema', declaration, 'examples/test_declared.csv']))

        self.assertEqual(js[0]['number'], '1')

    def test_declaration_duplicate_source_names(self):
        with tempfile.TemporaryDirectory() as directory:
            declaration = os.path.join(directory, 'declaration.json')
            with open(declaration, 'w', encoding='utf-8') as f:
                json.dump({'fields': [
                    {'name': 'number', 'column': 1, 'type': 'text'},
                    {'name': 'external_id', 'column': 2, 'type': 'text'},
                ]}, f)

            js = json.loads(self.get_output(['--schema', declaration, 'examples/test_declared.csv']))

        self.assertEqual(js[0]['number'], '1')
        self.assertEqual(js[0]['external_id'], '00042')

    def test_declaration_reads_input_once(self):
        output_file = io.TextIOWrapper(io.BytesIO(), encoding='utf-8', newline='', write_through=True)
        utility = CSVJSON(
            ['--schema', 'examples/test_declaration.json', '--stream', 'examples/test_declared.csv'],
            output_file,
        )

        original_reader = agate.csv.reader

        reads = []

        def counting_reader(f, **kwargs):
            reader = original_reader(f, **kwargs)

            class CountingReader:
                def __iter__(self):
                    return self

                def __next__(self):
                    row = next(reader)
                    reads.append(row)
                    return row

                def __getattr__(self, name):
                    return getattr(reader, name)

            return CountingReader()

        with patch('agate.csv.reader', counting_reader):
            utility.run()

        output_file.close()

        # The header plus four data records, each read exactly once.
        self.assertEqual(len(reads), 5)
