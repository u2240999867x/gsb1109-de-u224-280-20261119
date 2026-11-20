import types
import unittest

from influxdb_client import InfluxDBClient
from influxdb_client.client.flux_csv_parser import FluxCsvParserException, FluxQueryException
from influxdb_client.client.flux_table import FluxRecordStream


class _FakeResponse(object):
    """A transport response that records how its connection is finished."""

    def __init__(self, data, events, name):
        self._lines = data.encode('utf-8').splitlines(keepends=True)
        self.events = events
        self.name = name
        self.position = 0
        self.reads = 0

    def __iter__(self):
        return self

    def __next__(self):
        if self.position >= len(self._lines):
            raise StopIteration
        line = self._lines[self.position]
        self.position += 1
        self.reads += 1
        return line

    def close(self):
        self.events.append(('close', self.name))

    def release_conn(self):
        self.events.append(('release', self.name))


class _FakeQueryService(object):
    """A transport that answers each query with the next prepared response."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = 0

    def post_query(self, **kwargs):
        response = self.responses[self.calls]
        self.calls += 1
        return response


def _table(result, values):
    data = "#datatype,string,long,dateTime:RFC3339,string\n" \
           "#group,false,false,true,false\n" \
           f"#default,{result},,,\n" \
           ",result,table,_time,_value\n"
    for index, value in enumerate(values):
        data += f",,0,2020-01-01T00:00:0{index}Z,{value}\n"
    return data


def _response_data(result='_result', values=('a', 'b')):
    return _table(result, values)


def _two_tables():
    return _table('first', ('a', 'b')) + "\n" + _table('second', ('x', 'y'))


class QueryApiStreamCloseTest(unittest.TestCase):

    def setUp(self) -> None:
        self.client = InfluxDBClient(url="http://localhost", token="my-token", org="my-org")
        self.query_api = self.client.query_api()
        self.events = []
        self._responses = []
        self._service = _FakeQueryService(self._responses)
        self.query_api._query_api = self._service

    def tearDown(self) -> None:
        self.client.close()

    def _stream(self, name='r1', data=None):
        response = _FakeResponse(data or _response_data(), self.events, name)
        self._responses.append(response)
        return self.query_api.query_stream('from(bucket:"my-bucket") |> range(start: -10m)')

    def test_close_without_iteration_releases_response(self):
        stream = self._stream()

        stream.close()

        self.assertEqual([('close', 'r1')], self.events)
        self.assertTrue(isinstance(stream, types.GeneratorType))

    def test_break_closes_connection_only_once(self):
        stream = self._stream()

        self.assertEqual('a', next(stream)['_value'])
        stream.close()
        stream.close()
        stream.close()

        self.assertEqual([('close', 'r1')], self.events)

    def test_consumer_exception_closes_connection(self):
        stream = self._stream()

        with self.assertRaises(RuntimeError):
            with stream:
                self.assertEqual('a', next(stream)['_value'])
                raise RuntimeError('consumer failed')

        self.assertEqual([('close', 'r1')], self.events)

    def test_natural_read_releases_connection_only_once(self):
        stream = self._stream(data=_response_data(values=('a', 'b')))

        values = [record['_value'] for record in stream]
        stream.close()

        self.assertEqual(['a', 'b'], values)
        self.assertEqual([('release', 'r1')], self.events)

    def test_parse_error_is_propagated_and_response_is_closed(self):
        data = _response_data(values=('a',)) + "\n,error,reference\n,\"boom\",ref1\n"
        stream = self._stream(data=data)

        with self.assertRaises(FluxQueryException) as cm:
            list(stream)

        self.assertEqual('boom', cm.exception.message)
        self.assertEqual('ref1', cm.exception.reference)
        self.assertEqual([('close', 'r1')], self.events)

    def test_malformed_response_is_closed(self):
        stream = self._stream(data=",,0,2020-01-01T00:00:00Z,a\n")

        with self.assertRaises(FluxCsvParserException):
            list(stream)

        self.assertEqual([('close', 'r1')], self.events)

    def test_close_one_stream_does_not_affect_others(self):
        # The first response was received, but its iteration has not started yet.
        first = self._stream(name='r1', data=_two_tables())
        # The second one is positioned between the first record and the second table.
        second = self._stream(name='r2', data=_two_tables())

        self.assertEqual('a', next(second)['_value'])

        first.close()
        first.close()

        self.assertEqual([('close', 'r1')], self.events)
        # The second stream continues from its original position, the second table is intact.
        remaining = [record['_value'] for record in second]
        self.assertEqual(['b', 'x', 'y'], remaining)
        second.close()

        # The shared client can issue a third request after both streams ended.
        third = self._stream(name='r3')
        self.assertEqual(['a', 'b'], [record['_value'] for record in third])
        third.close()

        self.assertEqual(3, self._service.calls)
        self.assertEqual([('close', 'r1'), ('release', 'r2'), ('release', 'r3')], self.events)

    def test_interrupted_response_is_not_drained(self):
        data = _response_data(values=tuple(str(i) for i in range(100)))
        stream = self._stream(data=data)

        self.assertEqual('0', next(stream)['_value'])
        response = self._responses[0]
        reads_before_close = response.reads
        stream.close()

        self.assertEqual(reads_before_close, response.reads)
        self.assertLess(response.position, len(response._lines))
        self.assertEqual([('close', 'r1')], self.events)


class FluxRecordStreamTest(unittest.TestCase):

    def test_legacy_generator_check_and_iteration(self):
        from io import BytesIO
        from urllib3 import HTTPResponse
        from influxdb_client.client.flux_csv_parser import FluxCsvParser, FluxSerializationMode

        parser = FluxCsvParser(response=HTTPResponse(BytesIO(_response_data().encode()), preload_content=False),
                               serialization_mode=FluxSerializationMode.stream)
        stream = FluxRecordStream(parser)

        self.assertTrue(isinstance(stream, types.GeneratorType))
        self.assertEqual(['a', 'b'], [record['_value'] for record in stream])


if __name__ == '__main__':
    unittest.main()
