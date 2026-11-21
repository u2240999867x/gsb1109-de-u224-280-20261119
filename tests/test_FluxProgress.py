import asyncio
import csv as csv_parser
import itertools
import unittest
from io import BytesIO
from io import StringIO

from urllib3 import HTTPResponse

from influxdb_client.client.flux_csv_parser import FluxCsvParser, FluxSerializationMode, FluxQueryException
from influxdb_client.client.flux_observer import FluxProgressObserver
from influxdb_client.client.query_api import QueryOptions


class _AsyncRows:
    """Minimal async CSV row source asserting that parsing is driven row by row."""

    def __init__(self, text):
        self._rows = iter(csv_parser.reader(StringIO(text)))
        self.rows_read = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            row = next(self._rows)
        except StopIteration:
            raise StopAsyncIteration
        self.rows_read += 1
        return row


class _FailingResponse:
    """HTTP response that delivers all data and then fails instead of reaching EOF."""

    def __init__(self, data: bytes, error: Exception) -> None:
        self.closed = False
        self.data = data
        self.error = error

    def __iter__(self):
        for line in self.data.splitlines(keepends=True):
            yield line
        raise self.error

    def close(self):
        self.closed = True


class _RecordingObserver(FluxProgressObserver):
    def __init__(self):
        self.events = []

    def on_record(self, record, progress):
        self.events.append(('record', record, progress))

    def on_table_started(self, table, progress):
        self.events.append(('started', table, progress))

    def on_table_completed(self, table, progress):
        self.events.append(('completed', table, progress))

    def on_complete(self, progress):
        self.events.append(('complete', None, progress))

    def last(self):
        return self.events[-1][2]


_HEADER = "#datatype,string,long,string,string\n" \
          "#group,false,false,true,false\n" \
          "#default,_result,,,\n" \
          ",result,table,host,note\n"

_PROFILER = "#datatype,string,long,string\n" \
            "#group,false,false,true\n" \
            "#default,_profiler,,\n" \
            ",result,table,_measurement\n" \
            ",,0,profiler/query\n"

_RESPONSE = _HEADER \
            + ",,0,A,\"line1\nline2\"\n" \
            + ",,0,B,second\n" \
            + ",,5,C,third\n" \
            + ",,5,D,fourth\n" \
            + "\n" \
            + _HEADER \
            + "\n" \
            + _PROFILER \
            + "\n" \
            + _HEADER \
            + ",,0,E,last1\n" \
            + ",,0,F,last2\n" \
            + ",,0,G,last3\n"


class FluxProgressTest(unittest.TestCase):

    def test_pause_and_cross_table_id(self):
        observer = _RecordingObserver()
        parser = self._stream(_RESPONSE, observer)
        records = parser.generator()

        self.assertEqual(0, observer.events.__len__())

        self.assertEqual('line1\nline2', next(records).values['note'])
        self.assertEqual((1, 1, 0, True), self._state(observer.last()))

        self.assertEqual('second', next(records).values['note'])
        self.assertEqual((2, 1, 0, True), self._state(observer.last()))

        self.assertEqual('third', next(records).values['note'])
        self.assertEqual((3, 2, 1, True), self._state(observer.last()))
        self.assertEqual(1, len([e for e in observer.events if e[0] == 'completed']))

        self.assertEqual('fourth', next(records).values['note'])
        self.assertEqual((4, 2, 1, True), self._state(observer.last()))

        self._drain(records)
        self.assertEqual((7, 4, 4, False), self._state(parser.progress))
        self.assertEqual(1, len([e for e in observer.events if e[0] == 'complete']))

    def test_profiler_and_empty_table_are_not_counted_as_records(self):
        observer = _RecordingObserver()
        profilers = []
        parser = self._stream(_RESPONSE, observer, profiler_callback=profilers.append)

        records = list(parser.generator())
        self.assertEqual(7, len(records))
        self.assertEqual(['profiler/query'], [r.values['_measurement'] for r in profilers])
        self.assertEqual(4, parser.progress.tables_started)
        self.assertEqual(4, parser.progress.tables_completed)
        self.assertFalse(any(r.values.get('_measurement') == 'profiler/query' for r in records))
        self.assertEqual(7, len([e for e in observer.events if e[0] == 'record']))

    def test_empty_business_table_completes_without_records(self):
        data = _HEADER + ",,0,A,one\n" + "\n" + _HEADER + "\n" + _HEADER + ",,0,B,two\n"
        parser = self._stream(data, _RecordingObserver())

        records = list(parser.generator())
        self.assertEqual(2, len(records))
        self.assertEqual(3, parser.progress.tables_started)
        self.assertEqual(3, parser.progress.tables_completed)
        self.assertEqual(2, parser.progress.records_delivered)

    def test_reused_table_id_in_new_block_is_another_table(self):
        data = _HEADER + ",,0,A,one\n" + "\n" + _HEADER + ",,0,B,two\n"
        parser = self._stream(data, _RecordingObserver())

        list(parser.generator())
        self.assertEqual(2, parser.progress.tables_started)
        self.assertEqual(2, parser.progress.tables_completed)

    def test_table_id_switch_within_same_block_is_another_table(self):
        data = _HEADER + ",,0,A,one\n" + ",,7,B,two\n" + ",,7,C,three\n"
        parser = self._stream(data, _RecordingObserver())

        list(parser.generator())
        self.assertEqual(2, parser.progress.tables_started)
        self.assertEqual(2, parser.progress.tables_completed)

    def test_network_error_keeps_last_table_in_progress(self):
        observer = _RecordingObserver()
        data = _RESPONSE.split(',,0,G,last3\n')[0].encode('utf-8')
        response = _FailingResponse(data, ConnectionError('connection reset'))
        parser = FluxCsvParser(response=response, serialization_mode=FluxSerializationMode.stream,
                               query_options=QueryOptions(profilers=['query'], progress_observer=observer))

        records = parser.generator()
        delivered = []
        with self.assertRaises(ConnectionError):
            while True:
                delivered.append(next(records))

        self.assertEqual(6, len(delivered))
        self.assertEqual((6, 4, 3, True), self._state(parser.progress))
        self.assertEqual(0, len([e for e in observer.events if e[0] == 'complete']))

    def test_early_exit_keeps_last_table_in_progress(self):
        parser = self._stream(_RESPONSE, _RecordingObserver())
        records = parser.generator()

        self.assertEqual(2, len(list(itertools.islice(records, 2))))
        records.close()

        self.assertEqual((2, 1, 0, True), self._state(parser.progress))

    def test_observation_does_not_pre_read_or_change_iteration(self):
        plain = list(self._stream(_RESPONSE).generator())

        observer = _RecordingObserver()
        parser = self._stream(_RESPONSE, observer)
        observed = parser.generator()

        self.assertEqual(0, observer.events.__len__())
        first = next(observed)
        self.assertEqual(1, len([e for e in observer.events if e[0] == 'record']))
        remaining = [first] + list(observed)

        self.assertEqual([r.values for r in plain], [r.values for r in remaining])
        self.assertEqual([r.table for r in plain], [r.table for r in remaining])

    def test_async_generator_reports_delivered_records(self):
        observer = _RecordingObserver()
        rows = _AsyncRows(_RESPONSE)
        parser = FluxCsvParser(response=HTTPResponse(BytesIO(b''), preload_content=False),
                               serialization_mode=FluxSerializationMode.stream,
                               query_options=QueryOptions(profilers=['query'], progress_observer=observer))
        parser._reader = rows

        async def collect():
            delivered = []
            async for record in parser.generator_async():
                delivered.append(record)
                if len(delivered) == 2:
                    self.assertEqual(2, observer.last().records_delivered)
                    self.assertEqual(0, observer.last().tables_completed)
                    self.assertTrue(observer.last().table_in_progress)
                    self.assertLess(rows.rows_read, _RESPONSE.count('\n'))
            return delivered

        delivered = asyncio.run(collect())

        self.assertEqual(7, len(delivered))
        self.assertEqual((7, 4, 4, False), self._state(observer.last()))
        self.assertEqual(1, len([e for e in observer.events if e[0] == 'complete']))

    def test_observer_base_retains_latest_snapshot(self):
        observer = FluxProgressObserver()
        parser = self._stream(_RESPONSE, observer)
        records = parser.generator()

        self.assertEqual(0, observer.records_delivered)
        next(records)
        self.assertEqual(1, observer.records_delivered)
        self.assertEqual(0, observer.tables_completed)
        self.assertTrue(observer.table_in_progress)

        records.close()
        self.assertEqual(1, observer.records_delivered)
        self.assertTrue(observer.table_in_progress)

    def test_existing_error_propagation_is_unchanged(self):
        data = _HEADER + ",,0,A,one\n" + "\n" \
               + "#datatype,string,string\n" \
               + "#group,true,true\n" \
               + "#default,,\n" \
               + ",error,reference\n" \
               + ",internal error,123\n"
        parser = self._stream(data, _RecordingObserver())

        with self.assertRaises(FluxQueryException) as error:
            list(parser.generator())
        self.assertEqual('internal error', error.exception.message)
        self.assertEqual(1, parser.progress.tables_completed)

    def test_bulk_tables_mode_does_not_observe_as_delivered(self):
        observer = _RecordingObserver()
        parser = FluxCsvParser(response=HTTPResponse(BytesIO(_RESPONSE.encode('utf-8')), preload_content=False),
                               serialization_mode=FluxSerializationMode.tables,
                               query_options=QueryOptions(profilers=['query'], progress_observer=observer))

        list(parser.generator())

        self.assertEqual(7, sum(len(table.records) for table in parser.table_list()))
        self.assertEqual([], observer.events)
        self.assertEqual(0, parser.progress.records_delivered)
        self.assertEqual(0, parser.progress.tables_completed)

    @staticmethod
    def _drain(records):
        for _ in records:
            pass

    @staticmethod
    def _state(progress):
        return (progress.records_delivered, progress.tables_started,
                progress.tables_completed, progress.table_in_progress)

    @staticmethod
    def _stream(data, observer=None, profiler_callback=None):
        query_options = QueryOptions(profilers=['query'],
                                     profiler_callback=profiler_callback,
                                     progress_observer=observer)
        return FluxCsvParser(response=HTTPResponse(BytesIO(data.encode('utf-8')), preload_content=False),
                             serialization_mode=FluxSerializationMode.stream,
                             query_options=query_options)
