import json
import math
import unittest
import datetime
import pandas as pd
from io import BytesIO
from packaging import version

import pytest
from httpretty import httpretty
from urllib3 import HTTPResponse

from influxdb_client import InfluxDBClient
from influxdb_client.client.flux_csv_parser import FluxCsvParser, FluxSerializationMode, FluxQueryException, \
    FluxResponseMetadataMode
from influxdb_client.client.flux_table import FluxStructureEncoder, TableList


class FluxCsvParserTest(unittest.TestCase):

    def test_one_table(self):
        data = "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,long,long,string\n" \
               "#group,false,false,true,true,true,true,true,true,false,false,false\n" \
               "#default,_result,,,,,,,,,,\n" \
               ",result,table,_start,_stop,_field,_measurement,host,region,_value2,value1,value_str\n" \
               ",,0,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,121,11,test\n"

        tables = self._parse_to_tables(data=data)
        self.assertEqual(1, tables.__len__())
        self.assertEqual(11, tables[0].columns.__len__())
        self.assertEqual(1, tables[0].records.__len__())

    def test_more_tables(self):
        data = "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,long,long,string\n" \
               "#group,false,false,true,true,true,true,true,true,false,false,false\n" \
               "#default,_result,,,,,,,,,,\n" \
               ",result,table,_start,_stop,_field,_measurement,host,region,_value2,value1,value_str\n" \
               ",,0,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,121,11,test\n" \
               ",,1,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,B,west,484,22,test\n" \
               ",,2,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,usage_system,cpu,A,west,1444,38,test\n" \
               ",,3,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,user_usage,cpu,A,west,2401,49,test"

        tables = self._parse_to_tables(data=data)
        self.assertEqual(4, tables.__len__())
        self.assertEqual(11, tables[0].columns.__len__())
        self.assertEqual(1, tables[0].records.__len__())
        self.assertEqual(11, tables[1].columns.__len__())
        self.assertEqual(1, tables[1].records.__len__())
        self.assertEqual(11, tables[2].columns.__len__())
        self.assertEqual(1, tables[2].records.__len__())
        self.assertEqual(11, tables[3].columns.__len__())
        self.assertEqual(1, tables[3].records.__len__())

    def test_multiple_queries(self):
        data = "#datatype,string,long,string,string,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,double,string\n" \
               "#group,false,false,true,true,true,true,false,false,true\n" \
               "#default,t1,,,,,,,,\n" \
               ",result,table,_field,_measurement,_start,_stop,_time,_value,tag\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:20:00Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:21:40Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:23:20Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:25:00Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:26:40Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:28:20Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:30:00Z,2,test1\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:20:00Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:21:40Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:23:20Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:25:00Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:26:40Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:28:20Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:30:00Z,2,test2\n" \
               "\n" \
               "#datatype,string,long,string,string,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,double,string\n" \
               "#group,false,false,true,true,true,true,false,false,true\n" \
               "#default,t2,,,,,,,,\n" \
               ",result,table,_field,_measurement,_start,_stop,_time,_value,tag\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:20:00Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:21:40Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:23:20Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:25:00Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:26:40Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:28:20Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:30:00Z,2,test1\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:20:00Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:21:40Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:23:20Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:25:00Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:26:40Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:28:20Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:30:00Z,2,test2"

        tables = self._parse_to_tables(data=data)
        self.assertEqual(4, tables.__len__())
        self.assertEqual(9, tables[0].columns.__len__())
        self.assertEqual(7, tables[0].records.__len__())
        self.assertEqual(9, tables[1].columns.__len__())
        self.assertEqual(7, tables[1].records.__len__())
        self.assertEqual(9, tables[2].columns.__len__())
        self.assertEqual(7, tables[2].records.__len__())
        self.assertEqual(9, tables[3].columns.__len__())
        self.assertEqual(7, tables[3].records.__len__())

    def test_table_index_not_start_at_zero(self):
        data = "#datatype,string,long,string,string,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,double,string\n" \
               "#group,false,false,true,true,true,true,false,false,true\n" \
               "#default,t1,,,,,,,,\n" \
               ",result,table,_field,_measurement,_start,_stop,_time,_value,tag\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:20:00Z,2,test1\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:21:40Z,2,test1\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:23:20Z,2,test1\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:25:00Z,2,test1\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:26:40Z,2,test1\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:28:20Z,2,test1\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:30:00Z,2,test1\n" \
               ",,2,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:20:00Z,2,test2\n" \
               ",,2,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:21:40Z,2,test2\n" \
               ",,2,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:23:20Z,2,test2\n" \
               ",,2,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:25:00Z,2,test2\n" \
               ",,2,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:26:40Z,2,test2\n" \
               ",,2,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:28:20Z,2,test2\n" \
               ",,2,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:30:00Z,2,test2\n"

        tables = self._parse_to_tables(data=data)
        self.assertEqual(2, tables.__len__())
        self.assertEqual(9, tables[0].columns.__len__())
        self.assertEqual(7, tables[0].records.__len__())
        self.assertEqual(9, tables[1].columns.__len__())
        self.assertEqual(7, tables[1].records.__len__())

    def test_response_with_error(self):
        data = "#datatype,string,long,string,string,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,double,string\n" \
                 "#group,false,false,true,true,true,true,false,false,true\n" \
                 "#default,t1,,,,,,,,\n" \
                 ",result,table,_field,_measurement,_start,_stop,_time,_value,tag\n" \
                 ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:20:00Z,2,test1\n" \
                 ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:21:40Z,2,test1\n" \
                 ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:23:20Z,2,test1\n" \
                 ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:25:00Z,2,test1\n" \
                 "\n" \
                 "#datatype,string,string\n" \
                 "#group,true,true\n" \
                 "#default,,\n" \
                 ",error,reference\n" \
                 ",\"engine: unknown field type for value: xyz\","

        with self.assertRaises(FluxQueryException) as cm:
            self._parse_to_tables(data=data)
        exception = cm.exception

        self.assertEqual('engine: unknown field type for value: xyz', exception.message)
        self.assertEqual('', exception.reference)

    def test_ParseExportFromUserInterface(self):

        data = "#group,false,false,true,true,true,true,true,true,false,false\n" \
           + "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,double,dateTime:RFC3339\n" \
           + "#default,mean,,,,,,,,,\n" \
           + ",result,table,_start,_stop,_field,_measurement,city,location,_value,_time\n" \
           + ",,0,1754-06-26T11:30:27.613654848Z,2040-10-27T12:13:46.485Z,temperatureC,weather,London,us-midwest,30,1975-09-01T16:59:54.5Z\n" \
           + ",,1,1754-06-26T11:30:27.613654848Z,2040-10-27T12:13:46.485Z,temperatureF,weather,London,us-midwest,86,1975-09-01T16:59:54.5Z\n";

        tables = self._parse_to_tables(data=data)
        self.assertEqual(2, tables.__len__())
        self.assertEqual(1, tables[0].records.__len__())
        self.assertEqual(1, tables[1].records.__len__())
        self.assertFalse(tables[1].columns[0].group)
        self.assertFalse(tables[1].columns[1].group)
        self.assertTrue(tables[1].columns[2].group)

    def test_ParseInf(self):

        data = """#group,false,false,true,true,true,true,true,true,true,true,false,false
#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,string,string,double,double
#default,_result,,,,,,,,,,,
,result,table,_start,_stop,_field,_measurement,language,license,name,owner,le,_value
,,0,2021-06-23T06:50:11.897825012Z,2021-06-25T06:50:11.897825012Z,stars,github_repository,C#,MIT License,influxdb-client-csharp,influxdata,0,0
,,0,2021-06-23T06:50:11.897825012Z,2021-06-25T06:50:11.897825012Z,stars,github_repository,C#,MIT License,influxdb-client-csharp,influxdata,10,0
,,0,2021-06-23T06:50:11.897825012Z,2021-06-25T06:50:11.897825012Z,stars,github_repository,C#,MIT License,influxdb-client-csharp,influxdata,20,0
,,0,2021-06-23T06:50:11.897825012Z,2021-06-25T06:50:11.897825012Z,stars,github_repository,C#,MIT License,influxdb-client-csharp,influxdata,30,0
,,0,2021-06-23T06:50:11.897825012Z,2021-06-25T06:50:11.897825012Z,stars,github_repository,C#,MIT License,influxdb-client-csharp,influxdata,40,0
,,0,2021-06-23T06:50:11.897825012Z,2021-06-25T06:50:11.897825012Z,stars,github_repository,C#,MIT License,influxdb-client-csharp,influxdata,50,0
,,0,2021-06-23T06:50:11.897825012Z,2021-06-25T06:50:11.897825012Z,stars,github_repository,C#,MIT License,influxdb-client-csharp,influxdata,60,0
,,0,2021-06-23T06:50:11.897825012Z,2021-06-25T06:50:11.897825012Z,stars,github_repository,C#,MIT License,influxdb-client-csharp,influxdata,70,0
,,0,2021-06-23T06:50:11.897825012Z,2021-06-25T06:50:11.897825012Z,stars,github_repository,C#,MIT License,influxdb-client-csharp,influxdata,80,0
,,0,2021-06-23T06:50:11.897825012Z,2021-06-25T06:50:11.897825012Z,stars,github_repository,C#,MIT License,influxdb-client-csharp,influxdata,90,0
,,0,2021-06-23T06:50:11.897825012Z,2021-06-25T06:50:11.897825012Z,stars,github_repository,C#,MIT License,influxdb-client-csharp,influxdata,+Inf,15
,,0,2021-06-23T06:50:11.897825012Z,2021-06-25T06:50:11.897825012Z,stars,github_repository,C#,MIT License,influxdb-client-csharp,influxdata,-Inf,15

"""
        tables = self._parse_to_tables(data=data)
        self.assertEqual(1, tables.__len__())
        self.assertEqual(12, tables[0].records.__len__())
        self.assertEqual(math.inf, tables[0].records[10]["le"])
        self.assertEqual(-math.inf, tables[0].records[11]["le"])

    def test_to_json_by_encoder(self):
        data = "#datatype,string,long,string,string,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,double,string\n" \
               "#group,false,false,true,true,true,true,false,false,true\n" \
               "#default,_result,,,,,,,,\n" \
               ",result,table,_field,_measurement,_start,_stop,_time,_value,tag\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:20:00Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:21:40Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:23:20Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:25:00Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:26:40Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:28:20Z,2,test1\n" \
               ",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:30:00Z,2,test1\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:20:00Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:21:40Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:23:20Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:25:00Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:26:40Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:28:20Z,2,test2\n" \
               ",,1,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:30:00Z,2,test2\n"

        tables = self._parse_to_tables(data=data)
        with open('tests/query_output.json', 'r') as file:
            query_output = file.read()
            import json
            self.assertEqual(query_output, json.dumps(tables, cls=FluxStructureEncoder, indent=2))

    def test_pandas_lot_of_columns(self):
        data_types = ""
        groups = ""
        defaults = ""
        columns = ""
        values = ""
        for i in range(0, 200):
            data_types += f",long"
            groups += f",false"
            defaults += f","
            columns += f",column_{i}"
            values += f",{i}"

        data = f"#datatype,string,long,string,string,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,double,string{data_types}\n" \
               f"#group,false,false,true,true,true,true,false,false,true{groups}\n" \
               f"#default,_result,,,,,,,,{defaults}\n" \
               f",result,table,_field,_measurement,_start,_stop,_time,_value,tag{columns}\n" \
               f",,0,value,python_client_test,2010-02-27T04:48:32.752600083Z,2020-02-27T16:48:32.752600083Z,2020-02-27T16:20:00Z,2,test1{values}\n" \

        parser = self._parse(data=data, serialization_mode=FluxSerializationMode.dataFrame,
                             response_metadata_mode=FluxResponseMetadataMode.full)
        _dataFrames = list(parser.generator())
        self.assertEqual(1, _dataFrames.__len__())

    def test_pandas_column_datatype(self):
        data = "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,long,unsignedLong,string,boolean,double\n" \
               "#group,false,false,true,true,true,true,true,true,false,false,false,false,false\n" \
               "#default,_result,,,,,,,,,,,,\n" \
               ",result,table,_start,_stop,_field,_measurement,host,region,value1,value2,value3,value4,value5\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,121,11,test,true,6.56\n"
        parser = self._parse(data=data, serialization_mode=FluxSerializationMode.dataFrame,
                             response_metadata_mode=FluxResponseMetadataMode.full)
        df = list(parser.generator())[0]
        self.assertEqual(13, df.dtypes.__len__())
        self.assertEqual('object', df.dtypes['result'].name)
        self.assertEqual('int64', df.dtypes['table'].name)
        self.assertIn('datetime64[ns,', df.dtypes['_start'].name)
        self.assertIn('datetime64[ns,', df.dtypes['_stop'].name)
        self.assertEqual('object', df.dtypes['_field'].name)
        self.assertEqual('object', df.dtypes['_measurement'].name)
        self.assertEqual('object', df.dtypes['host'].name)
        self.assertEqual('object', df.dtypes['region'].name)
        self.assertEqual('int64', df.dtypes['value1'].name)
        self.assertEqual('int64', df.dtypes['value2'].name)
        self.assertEqual('object', df.dtypes['value3'].name)
        self.assertEqual('bool', df.dtypes['value4'].name)
        self.assertEqual('float64', df.dtypes['value5'].name)

    def test_pandas_column_datatype_extension_types(self):
        data = "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,long,unsignedLong,string,boolean,double\n" \
               "#group,false,false,true,true,true,true,true,true,false,false,false,false,false\n" \
               "#default,_result,,,,,,,,,,,,\n" \
               ",result,table,_start,_stop,_field,_measurement,host,region,value1,value2,value3,value4,value5\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,121,11,test,true,6.56\n"
        parser = self._parse(data=data, serialization_mode=FluxSerializationMode.dataFrame,
                             response_metadata_mode=FluxResponseMetadataMode.full,
                             use_extension_dtypes=True)
        df = list(parser.generator())[0]
        self.assertEqual(13, df.dtypes.__len__())
        self.assertEqual('string', df.dtypes['result'].name)
        self.assertEqual('Int64', df.dtypes['table'].name)
        self.assertIn('datetime64[ns,', df.dtypes['_start'].name)
        self.assertIn('datetime64[ns,', df.dtypes['_stop'].name)
        self.assertEqual('string', df.dtypes['_field'].name)
        self.assertEqual('string', df.dtypes['_measurement'].name)
        self.assertEqual('string', df.dtypes['host'].name)
        self.assertEqual('string', df.dtypes['region'].name)
        self.assertEqual('Int64', df.dtypes['value1'].name)
        self.assertEqual('Int64', df.dtypes['value2'].name)
        self.assertEqual('string', df.dtypes['value3'].name)
        self.assertEqual('boolean', df.dtypes['value4'].name)
        self.assertEqual('Float64', df.dtypes['value5'].name)

    def test_pandas_null_bool_types(self):
        data = "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,boolean\n" \
               "#group,false,false,true,true,true,true,true,true,false\n" \
               "#default,_result,,,,,,,,\n" \
               ",result,table,_start,_stop,_field,_measurement,host,region,value\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,true\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,\n"

        parser = self._parse(data=data, serialization_mode=FluxSerializationMode.dataFrame,
                             response_metadata_mode=FluxResponseMetadataMode.full)
        df = list(parser.generator())[0]
        self.assertEqual('object', df.dtypes['value'].name)

    def test_pandas_null_bool_types_extension_types(self):
        data = "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,boolean\n" \
               "#group,false,false,true,true,true,true,true,true,false\n" \
               "#default,_result,,,,,,,,\n" \
               ",result,table,_start,_stop,_field,_measurement,host,region,value\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,true\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,\n"

        parser = self._parse(data=data, serialization_mode=FluxSerializationMode.dataFrame,
                             response_metadata_mode=FluxResponseMetadataMode.full,
                             use_extension_dtypes=True)
        df = list(parser.generator())[0]
        self.assertEqual('boolean', df.dtypes['value'].name)

    def test_pandas_null_long_types(self):
        data = "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,long\n" \
               "#group,false,false,true,true,true,true,true,true,false\n" \
               "#default,_result,,,,,,,,\n" \
               ",result,table,_start,_stop,_field,_measurement,host,region,value\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,1\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,\n"

        parser = self._parse(data=data, serialization_mode=FluxSerializationMode.dataFrame,
                             response_metadata_mode=FluxResponseMetadataMode.full)
        df = list(parser.generator())[0]
        self.assertEqual('float64', df.dtypes['value'].name)  # pd.NA is converted to float('nan')

    @pytest.mark.skipif(version.parse(pd.__version__).release < (2, 0), reason="numeric nullables require pandas>=2.0 to work correctly")
    def test_pandas_null_long_types_extension_types(self):
        data = "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,long\n" \
               "#group,false,false,true,true,true,true,true,true,false\n" \
               "#default,_result,,,,,,,,\n" \
               ",result,table,_start,_stop,_field,_measurement,host,region,value\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,1\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,\n"

        parser = self._parse(data=data, serialization_mode=FluxSerializationMode.dataFrame,
                             response_metadata_mode=FluxResponseMetadataMode.full,
                             use_extension_dtypes=True)
        df = list(parser.generator())[0]
        self.assertEqual('Int64', df.dtypes['value'].name)

    def test_pandas_null_double_types(self):
        data = "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,double\n" \
               "#group,false,false,true,true,true,true,true,true,false\n" \
               "#default,_result,,,,,,,,\n" \
               ",result,table,_start,_stop,_field,_measurement,host,region,value\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,1\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,\n"

        parser = self._parse(data=data, serialization_mode=FluxSerializationMode.dataFrame,
                             response_metadata_mode=FluxResponseMetadataMode.full)
        df = list(parser.generator())[0]
        self.assertEqual('float64', df.dtypes['value'].name)

    @pytest.mark.skipif(version.parse(pd.__version__).release < (2, 0), reason="numeric nullables require pandas>=2.0 to work correctly")
    def test_pandas_null_double_types_extension_types(self):
        data = "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,double\n" \
               "#group,false,false,true,true,true,true,true,true,false\n" \
               "#default,_result,,,,,,,,\n" \
               ",result,table,_start,_stop,_field,_measurement,host,region,value\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,1\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,\n"

        parser = self._parse(data=data, serialization_mode=FluxSerializationMode.dataFrame,
                             response_metadata_mode=FluxResponseMetadataMode.full,
                             use_extension_dtypes=True)
        df = list(parser.generator())[0]
        self.assertEqual('Float64', df.dtypes['value'].name)

    def test_pandas_null_string_types(self):
        data = "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,string\n" \
               "#group,false,false,true,true,true,true,true,true,false\n" \
               "#default,_result,,,,,,,,\n" \
               ",result,table,_start,_stop,_field,_measurement,host,region,value\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,hi\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,\n"

        parser = self._parse(data=data, serialization_mode=FluxSerializationMode.dataFrame,
                             response_metadata_mode=FluxResponseMetadataMode.full)
        df = list(parser.generator())[0]
        self.assertEqual('object', df.dtypes['value'].name)

    def test_pandas_null_string_types_extension_types(self):
        data = "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,string\n" \
               "#group,false,false,true,true,true,true,true,true,false\n" \
               "#default,_result,,,,,,,,\n" \
               ",result,table,_start,_stop,_field,_measurement,host,region,value\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,hi\n" \
               ",,0,1977-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,\n"

        parser = self._parse(data=data, serialization_mode=FluxSerializationMode.dataFrame,
                             response_metadata_mode=FluxResponseMetadataMode.full,
                             use_extension_dtypes=True)
        df = list(parser.generator())[0]
        self.assertEqual('string', df.dtypes['value'].name)

    def test_parse_without_datatype(self):
        data = ",result,table,_start,_stop,_field,_measurement,host,region,_value2,value1,value_str\n" \
               ",,0,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,121,11,test\n" \
               ",,1,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,121,11,test\n"

        tables = self._parse_to_tables(data=data, response_metadata_mode=FluxResponseMetadataMode.only_names)
        self.assertEqual(2, tables.__len__())
        self.assertEqual(11, tables[0].columns.__len__())
        self.assertEqual(1, tables[0].records.__len__())
        self.assertEqual(11, tables[0].records[0].values.__len__())
        self.assertEqual('0', tables[0].records[0]['table'])
        self.assertEqual('11', tables[0].records[0]['value1'])
        self.assertEqual('west', tables[0].records[0]['region'])

    def test_parse_to_json(self):
        data = """#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,long,long,string
#group,false,false,true,true,true,true,true,true,false,false,false
#default,_result,,,,,,,,,,
,result,table,_start,_stop,_field,_measurement,host,region,_value2,value1,value_str
,,0,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,121,11,test
,,0,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,C,north,50,40,abc

#group,false,false,true,true,true,true,true,true,true,true,false,false
#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,string,string,double,double
#default,_result,,,,,,,,,,,
,result,table,_start,_stop,_field,_measurement,language,license,name,owner,le,_value
,,0,2021-06-23T06:50:11.897825012Z,2021-06-25T06:50:11.897825012Z,stars,github_repository,C#,MIT License,influxdb-client-csharp,influxdata,0,0
,,0,2021-06-23T06:50:11.897825012Z,2021-06-25T06:50:11.897825012Z,stars,github_repository,C#,MIT License,influxdb-client-csharp,influxdata,10,0

"""

        tables = self._parse_to_tables(data=data)
        parsed = json.loads(tables.to_json())
        self.assertEqual(4, parsed.__len__())

        self.assertEqual(11, parsed[0].__len__())
        self.assertEqual(11, parsed[0]['value1'])
        self.assertEqual("test", parsed[0]['value_str'])

        self.assertEqual(11, parsed[1].__len__())
        self.assertEqual(40, parsed[1]['value1'])
        self.assertEqual("abc", parsed[1]['value_str'])

        self.assertEqual(12, parsed[2].__len__())
        self.assertEqual(0, parsed[2]['le'])
        self.assertEqual("influxdata", parsed[2]['owner'])

        self.assertEqual(12, parsed[3].__len__())
        self.assertEqual(10, parsed[3]['le'])
        self.assertEqual("influxdata", parsed[3]['owner'])

    def test_parse_to_json_columns(self):
        data = """#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,long,long,string
#group,false,false,true,true,true,true,true,true,false,false,false
#default,_result,,,,,,,,,,
,result,table,_start,_stop,_field,_measurement,host,region,_value2,value1,value_str
,,0,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,121,11,test
,,0,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,C,north,50,40,abc
"""

        tables = self._parse_to_tables(data=data)
        parsed = json.loads(tables.to_json(columns=['region', 'size']))
        self.assertEqual(2, parsed.__len__())

        self.assertEqual(1, parsed[0].__len__())
        self.assertEqual("west", parsed[0]['region'])
        self.assertEqual(1, parsed[1].__len__())
        self.assertEqual("north", parsed[1]['region'])

    def test_parse_to_values(self):
        data = """#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,long,long,string
#group,false,false,true,true,true,true,true,true,false,false,false
#default,_result,,,,,,,,,,
,result,table,_start,_stop,_field,_measurement,host,region,_value2,value,value_str
,,0,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,121,11,test
,,0,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,C,north,50,40,abc

#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,long,long,string
#group,false,false,true,true,true,true,true,true,false,false,false
#default,_result,,,,,,,,,,
,result,table,_start,_stop,_field,_measurement,host,region,_value2,value,value_str
,,1,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,B,south,121,18,test
,,1,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,D,south,50,22,abc
"""

        tables = self._parse_to_tables(data=data)
        parsed = tables.to_values(['region', 'host', 'not_exits', 'value'])
        self.assertEqual(4, parsed.__len__())

        self.assertEqual(['west', 'A', None, 11], parsed[0])
        self.assertEqual(['north', 'C', None, 40], parsed[1])
        self.assertEqual(['south', 'B', None, 18], parsed[2])
        self.assertEqual(['south', 'D', None, 22], parsed[3])

    def test_parse_duplicate_column_names(self):
        data = """#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,dateTime:RFC3339,string,string,double
#group,false,false,true,true,false,true,true,false
#default,_result,,,,,,,
,result,table,_start,_stop,_time,_measurement,location,result
,,0,2022-09-13T06:14:40.469404272Z,2022-09-13T06:24:40.469404272Z,2022-09-13T06:24:33.746Z,my_measurement,Prague,25.3
,,0,2022-09-13T06:14:40.469404272Z,2022-09-13T06:24:40.469404272Z,2022-09-13T06:24:39.299Z,my_measurement,Prague,25.3
,,0,2022-09-13T06:14:40.469404272Z,2022-09-13T06:24:40.469404272Z,2022-09-13T06:24:40.454Z,my_measurement,Prague,25.3
"""
        with pytest.warns(UserWarning) as warnings:
            tables = self._parse_to_tables(data=data)
        self.assertEqual(1, len(warnings))
        self.assertEqual(1, tables.__len__())
        self.assertEqual(8, tables[0].columns.__len__())
        self.assertEqual(3, tables[0].records.__len__())
        self.assertEqual(7, tables[0].records[0].values.__len__())
        self.assertEqual(8, tables[0].records[0].row.__len__())
        self.assertEqual(25.3, tables[0].records[0].row[7])


    def test_default_annotation_before_datatype(self):
        data = "#default,_result,,,,,,,,,42,\n" \
               "#group,false,false,true,true,true,true,true,true,false,false,false\n" \
               "#datatype,string,long,dateTime:RFC3339,dateTime:RFC3339,string,string,string,string,long,long,string\n" \
               ",result,table,_start,_stop,_field,_measurement,host,region,_value2,value1,value_str\n" \
               ",,0,1677-09-21T00:12:43.145224192Z,2018-07-16T11:21:02.547596934Z,free,mem,A,west,121,,test\n"

        tables = self._parse_to_tables(data=data)
        self.assertEqual(1, tables.__len__())
        self.assertEqual(11, tables[0].columns.__len__())
        self.assertEqual("_result", tables[0].columns[0].default_value)
        self.assertEqual("long", tables[0].columns[9].data_type)
        self.assertTrue(tables[0].columns[2].group)
        record = tables[0].records[0]
        self.assertEqual("_result", record.values["result"])
        self.assertEqual(42, record.values["value1"])
        self.assertEqual(121, record.values["_value2"])

    def test_default_annotation_before_datatype_more_tables(self):
        data = "#default,first,,,\n" \
               "#datatype,string,long,string,long\n" \
               "#group,false,false,true,false\n" \
               ",result,table,host,_value\n" \
               ",,0,A,1\n" \
               "\n" \
               "#default,second,,,7\n" \
               "#group,false,false,true,false\n" \
               "#datatype,string,long,string,long\n" \
               ",result,table,host,_value\n" \
               ",,1,B,\n" \
               "\n" \
               "#datatype,string,long,string,long\n" \
               "#group,false,false,true,false\n" \
               ",result,table,host,_value\n" \
               ",,2,C,\n"

        tables = self._parse_to_tables(data=data)
        self.assertEqual(3, tables.__len__())
        self.assertEqual("first", tables[0].records[0].values["result"])
        self.assertEqual(1, tables[0].records[0].values["_value"])
        self.assertEqual("second", tables[1].records[0].values["result"])
        self.assertEqual(7, tables[1].records[0].values["_value"])
        self.assertTrue(tables[1].columns[2].group)
        # defaults of a previous table must not leak into a table without #default
        self.assertIsNone(tables[2].records[0].values["result"])
        self.assertIsNone(tables[2].records[0].values["_value"])

    @staticmethod
    def _parse_to_tables(data: str, serialization_mode=FluxSerializationMode.tables,
                         response_metadata_mode=FluxResponseMetadataMode.full) -> TableList:
        _parser = FluxCsvParserTest._parse(data, serialization_mode, response_metadata_mode=response_metadata_mode)
        list(_parser.generator())
        tables = _parser.tables
        return tables

    @staticmethod
    def _parse(data, serialization_mode, response_metadata_mode, use_extension_dtypes=False):
        fp = BytesIO(str.encode(data))
        return FluxCsvParser(response=HTTPResponse(fp, preload_content=False),
                             serialization_mode=serialization_mode, response_metadata_mode=response_metadata_mode,
                             use_extension_dtypes=use_extension_dtypes)


class FluxCsvQueryOfflineRegressionTest(unittest.TestCase):
    """Offline regression for defaults, column layout and group state defined by annotation blocks."""

    QUERY = 'from(bucket: "ops") |> range(start: 2026-11-19T07:00:00Z, stop: 2026-11-19T10:00:00Z)'

    RESPONSE = (
        "#default,_result,,,,open,5,,,\n"
        "#datatype,string,long,dateTime:RFC3339,string,string,long,string,string,double\n"
        "#group,false,false,false,true,false,false,true,true,false\n"
        ",result,table,_time,site,status,quota,_field,_measurement,_value\n"
        ",,1,2026-11-19T08:00:00Z,North,,,orders,orders_total,10\n"
        ",,1,2026-11-19T08:02:00Z,North,,,orders,orders_total,12\n"
        ",,2,2026-11-19T08:05:00Z,South,closed,0,orders,orders_total,0\n"
        ",,2,2026-11-19T08:07:00Z,South,,,orders,orders_total,3\n"
        "\n"
        "#default,_result,,,,,queued,,,\n"
        "#datatype,string,long,dateTime:RFC3339,string,string,string,string,string,double\n"
        "#group,false,false,false,false,false,false,true,true,false\n"
        ",result,table,_time,site,quota,status,_field,_measurement,_value\n"
        ",,2,2026-11-19T09:00:00Z,West,0,,orders,orders_total,0\n"
        ",,2,2026-11-19T09:05:00Z,West,,,orders,orders_total,1\n"
        ",,2,2026-11-19T09:10:00Z,West,12,held,orders,orders_total,2\n"
    )

    BUSINESS_COLUMNS = ('table', '_time', 'site', 'status', 'quota', '_field', '_measurement', '_value')

    def setUp(self) -> None:
        httpretty.enable()
        httpretty.reset()
        httpretty.register_uri(httpretty.POST, uri="http://localhost/api/v2/query", status=200,
                               body=self.RESPONSE)
        self.client = InfluxDBClient("http://localhost", "my-token", org="my-org", enable_gzip=False)
        self.query_api = self.client.query_api()

    def tearDown(self) -> None:
        self.client.close()
        httpretty.disable()

    def test_tables_keep_block_defaults_and_treat_reused_table_id_as_new_table(self):
        tables = self.query_api.query(self.QUERY)

        self.assertEqual(3, len(tables))
        self.assertEqual([2, 2, 3], [len(table.records) for table in tables])
        self.assertEqual([[1, 1], [2, 2], [2, 2, 2]],
                         [[record['table'] for record in table.records] for table in tables])

        expected = [
            [
                (1, self._utc(8, 0), 'North', 'open', 5, 'orders', 'orders_total', 10.0),
                (1, self._utc(8, 2), 'North', 'open', 5, 'orders', 'orders_total', 12.0),
            ],
            [
                (2, self._utc(8, 5), 'South', 'closed', 0, 'orders', 'orders_total', 0.0),
                (2, self._utc(8, 7), 'South', 'open', 5, 'orders', 'orders_total', 3.0),
            ],
            [
                (2, self._utc(9, 0), 'West', 'queued', '0', 'orders', 'orders_total', 0.0),
                (2, self._utc(9, 5), 'West', 'queued', None, 'orders', 'orders_total', 1.0),
                (2, self._utc(9, 10), 'West', 'held', '12', 'orders', 'orders_total', 2.0),
            ],
        ]
        self.assertEqual(expected,
                         [[self._business_record(record) for record in table.records] for table in tables])

        first_columns = self._column_definitions(tables[0])
        second_columns = self._column_definitions(tables[1])
        third_columns = self._column_definitions(tables[2])

        self.assertEqual(first_columns, second_columns)
        self.assertEqual(('string', False, 'open'), first_columns['status'])
        self.assertEqual(('long', False, '5'), first_columns['quota'])
        self.assertEqual(('string', True, ''), first_columns['site'])

        self.assertEqual(['result', 'table', '_time', 'site', 'quota', 'status',
                          '_field', '_measurement', '_value'],
                         [column.label for column in tables[2].columns])
        self.assertEqual(('string', False, ''), third_columns['quota'])
        self.assertEqual(('string', False, 'queued'), third_columns['status'])
        self.assertEqual(('string', False, ''), third_columns['site'])

        self.assertEqual(['site', '_field', '_measurement'],
                         [column.label for column in tables[0].get_group_key()])
        self.assertEqual(['site', '_field', '_measurement'],
                         [column.label for column in tables[1].get_group_key()])
        self.assertEqual(['_field', '_measurement'],
                         [column.label for column in tables[2].get_group_key()])

        self.assertIsNot(tables[1], tables[2])
        self.assertEqual(['closed', 'open'], [record['status'] for record in tables[1].records])
        self.assertEqual([0, 5], [record['quota'] for record in tables[1].records])
        self.assertEqual(['queued', 'queued', 'held'], [record['status'] for record in tables[2].records])

        self.assertIsInstance(tables[1].records[0]['quota'], int)
        self.assertIsInstance(tables[2].records[0]['quota'], str)
        self.assertEqual('0', tables[2].records[0]['quota'])
        self.assertIsNone(tables[2].records[1]['quota'])

    def test_stream_matches_tables_and_keeps_earlier_records_stable(self):
        tables = self.query_api.query(self.QUERY)
        expected = [self._business_record(record) for table in tables for record in table.records]

        stream = self.query_api.query_stream(self.QUERY)
        early_records = [next(stream) for _ in range(4)]
        early_snapshot = [self._business_record(record) for record in early_records]
        self.assertEqual(['open', 'open', 'closed', 'open'], [record['status'] for record in early_records])
        self.assertEqual([5, 5, 0, 5], [record['quota'] for record in early_records])

        remaining_records = list(stream)
        self.assertEqual(3, len(remaining_records))
        self.assertEqual('0', remaining_records[0]['quota'])
        self.assertIsInstance(remaining_records[0]['quota'], str)
        self.assertIsNone(remaining_records[1]['quota'])
        self.assertEqual('queued', remaining_records[1]['status'])

        actual = [self._business_record(record) for record in early_records + remaining_records]
        self.assertEqual(expected, actual)
        self.assertEqual(early_snapshot, [self._business_record(record) for record in early_records])

    @classmethod
    def _business_record(cls, record):
        return tuple(record[column] for column in cls.BUSINESS_COLUMNS)

    @staticmethod
    def _column_definitions(table):
        return {column.label: (column.data_type, column.group, column.default_value)
                for column in table.columns}

    @staticmethod
    def _utc(hour, minute):
        return datetime.datetime(2026, 11, 19, hour, minute, tzinfo=datetime.timezone.utc)
