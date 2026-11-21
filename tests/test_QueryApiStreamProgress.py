import itertools
import unittest

from httpretty import httpretty

from influxdb_client import InfluxDBClient
from influxdb_client.client.flux_observer import FluxProgressObserver
from influxdb_client.client.query_api import QueryOptions

_RESPONSE = """#datatype,string,long,string,string
#group,false,false,true,false
#default,_result,,,
,result,table,host,note
,,0,A,"line1
line2"
,,0,B,second
,,5,C,third
,,5,D,fourth

#datatype,string,long,string
#group,false,false,true
#default,_profiler,,
,result,table,_measurement
,,0,profiler/query

#datatype,string,long,string,string
#group,false,false,true,false
#default,_result,,,
,result,table,host,note
,,0,E,last1
,,0,F,last2
,,0,G,last3
"""


class _Observer(FluxProgressObserver):
    def __init__(self):
        self.snapshots = []

    def on_record(self, record, progress):
        self.snapshots.append(progress)

    def on_complete(self, progress):
        self.snapshots.append(progress)


class QueryStreamProgressTest(unittest.TestCase):

    def setUp(self) -> None:
        httpretty.enable()
        httpretty.reset()
        httpretty.register_uri(httpretty.POST, uri="http://localhost/api/v2/query", status=200, body=_RESPONSE)
        self.client = InfluxDBClient("http://localhost", "my-token", org="my-org", enable_gzip=False)

    def tearDown(self) -> None:
        self.client.close()
        httpretty.disable()
        httpretty.reset()

    def test_stream_pause_and_complete(self):
        observer = _Observer()
        query_api = self.client.query_api(query_options=QueryOptions(profilers=["query"],
                                                                    progress_observer=observer))
        records = query_api.query_stream('from(bucket:"my-bucket") |> range(start: -10m)')

        self.assertEqual([], observer.snapshots)

        delivered = []
        delivered.append(next(records))
        self.assertEqual(1, observer.snapshots[-1].records_delivered)
        self.assertEqual(0, observer.snapshots[-1].tables_completed)
        self.assertTrue(observer.snapshots[-1].table_in_progress)

        delivered.append(next(records))
        delivered.append(next(records))
        self.assertEqual(2, observer.snapshots[-1].tables_started)
        self.assertEqual(1, observer.snapshots[-1].tables_completed)

        delivered.extend(records)
        self.assertEqual(7, len(delivered))
        self.assertEqual(7, observer.snapshots[-1].records_delivered)
        self.assertEqual(3, observer.snapshots[-1].tables_started)
        self.assertEqual(3, observer.snapshots[-1].tables_completed)
        self.assertFalse(observer.snapshots[-1].table_in_progress)

    def test_stream_early_close_keeps_open_table(self):
        observer = _Observer()
        query_api = self.client.query_api(query_options=QueryOptions(profilers=["query"],
                                                                    progress_observer=observer))
        records = query_api.query_stream('from(bucket:"my-bucket") |> range(start: -10m)')

        delivered = list(itertools.islice(records, 4))
        records.close()

        self.assertEqual(4, len(delivered))
        self.assertEqual(4, observer.snapshots[-1].records_delivered)
        self.assertEqual(1, observer.snapshots[-1].tables_completed)
        self.assertTrue(observer.snapshots[-1].table_in_progress)

    def test_per_call_observer_and_no_profilers(self):
        observer = _Observer()
        query_api = self.client.query_api()
        records = query_api.query_stream('from(bucket:"my-bucket") |> range(start: -10m)',
                                         progress_observer=observer)

        delivered = list(records)

        self.assertEqual(8, len(delivered))
        self.assertEqual(8, observer.snapshots[-1].records_delivered)
