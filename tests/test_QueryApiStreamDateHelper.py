import unittest
from datetime import datetime, timezone
from io import BytesIO

from urllib3 import HTTPResponse

import influxdb_client.client.util.date_utils as date_utils
from influxdb_client.client.util.date_utils import DateHelper
from influxdb_client.client.util.date_utils_pandas import PandasDateTimeHelper

NANO_RESPONSE = """#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,double
#group,false,false,false,false,false,false
#default,_result,,,2020-02-27T16:20:00.111222333+05:45,,7
,result,table,_time,_archived,_optional,_value
,,0,2020-02-27T16:20:00.123456789+05:45,,,1
,,0,2020-02-27T16:20:00.123456012+05:45,,,2

#datatype,string,long,dateTime:RFC3339Nano,dateTime:RFC3339,dateTime:RFC3339,double
#group,false,false,false,false,false,false
#default,_result,,,2020-02-27T16:20:00.999888777+05:45,,8
,result,table,_time,_archived,_optional,_value
,,0,2020-02-27T16:20:01.000000158+05:45,,,4
,,0,2020-02-27T16:20:01.000000159+05:45,,,5
"""

BAD_NANO_RESPONSE = """#datatype,string,long,dateTime:RFC3339Nano,dateTime:RFC3339,double
#group,false,false,false,false,false
#default,_result,,,2020-02-27T16:20:00.999888777+05:45,8
,result,table,_time,_archived,_value
,,0,2020-02-27T16:20:00.000000158+05:45,,1

#datatype,string,long,dateTime:RFC3339Nano,dateTime:RFC3339,double
#group,false,false,false,false,false
#default,_result,,,2020-02-27T16:20:00.999888777+05:45,8
,result,table,_time,_archived,_value
,,0,not-a-timestamp,,2
"""

ARCHIVE_RESPONSE = """#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string
#group,false,false,false,false,false
#default,_result,,,2020-02-27T16:20:00.222333444+05:45,
,result,table,_time,_archived,_field
,,0,2020-02-27T16:20:00.123456789+05:45,,water_level

#datatype,string,long,dateTime:RFC3339Nano,dateTime:RFC3339,string
#group,false,false,false,false,false
#default,_result,,,2020-02-27T16:20:00.888777666+05:45,
,result,table,_time,_archived,_field
,,0,2020-02-27T10:35:01.000001159Z,,water_level
"""


class RecordingHelper(DateHelper):
    def __init__(self):
        super().__init__()
        self.values = []

    def parse_date(self, date_string: str):
        value = super().parse_date(date_string)
        self.values.append(date_string)
        return value


class ExplodingHelper(DateHelper):
    def parse_date(self, date_string: str):
        raise AssertionError("the process default must not be used by already started streams")


def _response(data: str) -> HTTPResponse:
    return HTTPResponse(BytesIO(data.encode()), preload_content=False)


class QueryApiStreamDateHelperTest(unittest.TestCase):

    def setUp(self) -> None:
        self._date_helper = date_utils.date_helper
        date_utils.date_helper = DateHelper()

    def tearDown(self) -> None:
        date_utils.date_helper = self._date_helper

    def _streams(self, responses, helper=None, **kwargs):
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.query_api import QueryOptions
        client = InfluxDBClient("http://localhost", "my-token", org="my-org")
        query_options = kwargs.pop('query_options', None)
        if query_options:
            query_api = client.query_api(query_options=QueryOptions(**query_options))
        else:
            query_api = client.query_api()
        query_api._query_api.post_query = lambda **ignored: _response(next(responses))
        if helper is not None:
            kwargs['date_helper'] = helper
        return client, query_api.query_stream("from(bucket:\"my-bucket\")", **kwargs)

    def _stream(self, data: str, helper=None, **kwargs):
        client, records = self._streams(iter([data]), helper, **kwargs)
        return client, records

    def test_nanoseconds_defaults_and_table_reuse(self):
        client, records = self._stream(NANO_RESPONSE, PandasDateTimeHelper())
        try:
            first = list(records)
        finally:
            client.close()

        self.assertEqual(4, len(first))
        self.assertEqual(789, first[0]["_time"].nanosecond)
        self.assertEqual(12, first[1]["_time"].nanosecond)
        self.assertEqual(123456, first[0]["_time"].microsecond)
        self.assertEqual(123456, first[1]["_time"].microsecond)
        self.assertNotEqual(first[0]["_time"], first[1]["_time"])

        self.assertEqual(333, first[0]["_archived"].nanosecond)
        self.assertIsNone(first[0]["_optional"])
        self.assertEqual(1.0, first[0]["_value"])

        self.assertEqual(777, first[2]["_archived"].nanosecond)
        self.assertIsNone(first[2]["_optional"])
        self.assertEqual(4.0, first[2]["_value"])
        self.assertEqual(158, first[2]["_time"].nanosecond)
        self.assertEqual(159, first[3]["_time"].nanosecond)

        self.assertEqual(345, first[0]["_time"].utcoffset().total_seconds() // 60)
        self.assertNotEqual(0, first[0]["_time"].utcoffset().total_seconds() % 3600)

    def test_standard_helper_truncates_without_changing_instant(self):
        client, records = self._stream(ARCHIVE_RESPONSE, DateHelper())
        try:
            first = list(records)
        finally:
            client.close()

        self.assertIs(type(first[0]["_time"]), datetime)
        self.assertEqual(123456, first[0]["_time"].microsecond)
        self.assertEqual(222333, first[0]["_archived"].microsecond)
        self.assertEqual(345, first[0]["_time"].utcoffset().total_seconds() // 60)
        self.assertEqual(datetime(2020, 2, 27, 10, 35, 0, 123456,
                                  tzinfo=timezone.utc).timestamp(),
                         first[0]["_time"].timestamp())

        self.assertIs(type(first[1]["_time"]), datetime)
        self.assertEqual(1, first[1]["_time"].microsecond)
        self.assertEqual(datetime(2020, 2, 27, 10, 35, 1, 1,
                                  tzinfo=timezone.utc).timestamp(),
                         first[1]["_time"].timestamp())
        self.assertEqual(888777, first[1]["_archived"].microsecond)

    def test_both_helpers_keep_the_same_instant(self):
        nano_client, nano_records = self._stream(ARCHIVE_RESPONSE, PandasDateTimeHelper())
        std_client, std_records = self._stream(ARCHIVE_RESPONSE, DateHelper())
        try:
            nano = list(nano_records)
            standard = list(std_records)
        finally:
            nano_client.close()
            std_client.close()

        for nano_record, std_record in zip(nano, standard):
            self.assertEqual(std_record["_time"], nano_record["_time"].floor('us'))
            self.assertEqual(std_record["_archived"], nano_record["_archived"].floor('us'))
        self.assertNotEqual(nano[0]["_time"], nano[0]["_archived"])

    def test_streams_are_isolated_and_capture_process_default_at_start(self):
        from influxdb_client import InfluxDBClient
        from influxdb_client.client.query_api import QueryOptions

        default_helper = RecordingHelper()
        date_utils.date_helper = default_helper

        client = InfluxDBClient("http://localhost", "my-token", org="my-org")
        responses = {"nano": BAD_NANO_RESPONSE, "archive": ARCHIVE_RESPONSE, "legacy": ARCHIVE_RESPONSE}
        apis = {
            "nano": client.query_api(query_options=QueryOptions(date_helper=PandasDateTimeHelper())),
            "archive": client.query_api(query_options=QueryOptions(date_helper=DateHelper())),
            "legacy": client.query_api(),
        }
        for name, api in apis.items():
            api._query_api.post_query = lambda _name=name, **ignored: _response(responses[_name])

        try:
            nano = iter(apis["nano"].query_stream("from(bucket:\"nano\")"))
            archive = iter(apis["archive"].query_stream("from(bucket:\"archive\")"))
            legacy = iter(apis["legacy"].query_stream("from(bucket:\"legacy\")"))

            date_utils.date_helper = ExplodingHelper()

            archive_first = next(archive)
            self.assertIs(type(archive_first["_time"]), datetime)
            nano_first = next(nano)
            self.assertEqual(158, nano_first["_time"].nanosecond)
            legacy_first = next(legacy)
            self.assertIs(type(legacy_first["_time"]), datetime)

            with self.assertRaises(Exception):
                next(nano)

            archive_second = next(archive)
            self.assertIs(type(archive_second["_time"]), datetime)

            legacy_second = next(legacy)
            self.assertIs(type(legacy_second["_time"]), datetime)

            with self.assertRaises(StopIteration):
                next(archive)
            with self.assertRaises(StopIteration):
                next(legacy)

            self.assertTrue(default_helper.values)
            self.assertIsInstance(date_utils.date_helper, ExplodingHelper)
        finally:
            client.close()

    def test_query_options_and_datetime_helper_alias(self):
        via_options_client, via_options = self._stream(
            ARCHIVE_RESPONSE, query_options={"datetime_helper": PandasDateTimeHelper()})
        alias_client, alias_records = self._stream(ARCHIVE_RESPONSE, datetime_helper=PandasDateTimeHelper())
        try:
            via_options_first = next(iter(via_options))
            alias_first = next(iter(alias_records))
        finally:
            via_options_client.close()
            alias_client.close()

        self.assertEqual(789, via_options_first["_time"].nanosecond)
        self.assertEqual(444, via_options_first["_archived"].nanosecond)
        self.assertEqual(789, alias_first["_time"].nanosecond)
        self.assertEqual(444, alias_first["_archived"].nanosecond)

    def test_helper_class_and_plain_callable(self):
        class_client, class_records = self._stream(NANO_RESPONSE, date_helper=PandasDateTimeHelper)
        callable_client, callable_records = self._stream(NANO_RESPONSE, date_helper=DateHelper().parse_date)
        try:
            class_first = next(iter(class_records))
            callable_first = next(iter(callable_records))
        finally:
            class_client.close()
            callable_client.close()

        self.assertEqual(789, class_first["_time"].nanosecond)
        self.assertIs(type(callable_first["_time"]), datetime)
        self.assertEqual(123456, callable_first["_time"].microsecond)


if __name__ == '__main__':
    unittest.main()
