import unittest
from io import BytesIO

import pandas as pd
from urllib3 import HTTPResponse

from influxdb_client.client.flux_csv_parser import FluxCsvParser, FluxSerializationMode
from influxdb_client.client.flux_data_frame import (
    GROUP_SOURCE_COLUMN,
    RESULT_SOURCE_COLUMN,
    DataFrameMergeConfig,
    FluxMergedDataFrameException,
    merge_tables_to_data_frame,
    select_source_rows,
)
from influxdb_client.client.flux_table import FluxColumn, FluxTable
from influxdb_client.client.query_api import QueryApi

# Declaration-only table (no data rows), a nullable long table with a real zero and a table that
# reuses table number 0 in another result but declares the same column name '_value' as a string.
CONFLICTING_RESPONSE = (
    "#datatype,string,long,dateTime:RFC3339,long,string\n"
    "#group,false,false,false,true,true\n"
    "#default,_result,,,,north\n"
    ",result,table,_time,_value,host\n"
    "\n\n"
    "#datatype,string,long,dateTime:RFC3339,long,string\n"
    "#group,false,false,false,true,true\n"
    "#default,_result,,,,\n"
    ",result,table,_time,_value,host\n"
    ",,1,2023-01-01T00:00:00Z,,south\n"
    ",,1,2023-01-02T00:00:00Z,0,south\n"
    "\n\n"
    "#datatype,string,long,dateTime:RFC3339,string,string\n"
    "#group,false,false,false,true,true\n"
    "#default,_other,,,,\n"
    ",result,table,_time,_value,host\n"
    ",,0,2023-01-03T00:00:00Z,high,east\n"
    "\n\n"
)

# Only compatible declarations: a long beyond the binary floating point precision, a missing value
# filled from the '#default' declaration and a row with a genuinely missing value. Both tables
# reuse table number 0 to prove table numbers cannot collapse rows of different groups/results.
COMPATIBLE_RESPONSE = (
    "#datatype,string,long,dateTime:RFC3339,long,string\n"
    "#group,false,false,false,true,true\n"
    "#default,_result,,,42,\n"
    ",result,table,_time,_value,host\n"
    ",,0,2023-01-01T00:00:00Z,,north\n"
    ",,0,2023-01-02T00:00:00Z,9007199254740993,north\n"
    "\n\n"
    "#datatype,string,long,dateTime:RFC3339,long,string\n"
    "#group,false,false,false,true,true\n"
    "#default,_result,,,,\n"
    ",result,table,_time,_value,host\n"
    ",,0,2023-01-03T00:00:00Z,,south\n"
    ",,0,2023-01-04T00:00:00Z,7,south\n"
    "\n\n"
)

COMPATIBLE_RESPONSE_SWAPPED = (
    "#datatype,string,long,dateTime:RFC3339,long,string\n"
    "#group,false,false,false,true,true\n"
    "#default,_result,,,,\n"
    ",result,table,_time,_value,host\n"
    ",,0,2023-01-03T00:00:00Z,,south\n"
    ",,0,2023-01-04T00:00:00Z,7,south\n"
    "\n\n"
    "#datatype,string,long,dateTime:RFC3339,long,string\n"
    "#group,false,false,false,true,true\n"
    "#default,_result,,,42,\n"
    ",result,table,_time,_value,host\n"
    ",,0,2023-01-01T00:00:00Z,,north\n"
    ",,0,2023-01-02T00:00:00Z,9007199254740993,north\n"
    "\n\n"
)

BIG_INTEGER = 9007199254740993


def _parse_to_tables(data):
    response = HTTPResponse(BytesIO(str.encode(data)), preload_content=False)
    parser = FluxCsvParser(response=response, serialization_mode=FluxSerializationMode.tables)
    list(parser.generator())
    return parser.tables


class FluxMergedDataFrameTest(unittest.TestCase):

    def setUp(self):
        self.conflicting_tables = _parse_to_tables(CONFLICTING_RESPONSE)
        self.compatible_tables = _parse_to_tables(COMPATIBLE_RESPONSE)
        self.swapped_tables = _parse_to_tables(COMPATIBLE_RESPONSE_SWAPPED)

    def test_selecting_all_sources_reports_exact_tables_and_declarations(self):
        with self.assertRaises(FluxMergedDataFrameException) as ctx:
            merge_tables_to_data_frame(self.conflicting_tables)

        message = str(ctx.exception)
        self.assertIn("_value", message)
        self.assertIn("'long'", message)
        self.assertIn("'string'", message)
        self.assertIn("_result", message)
        self.assertIn("_other", message)
        self.assertIn("north", message)
        self.assertIn("east", message)
        self.assertIn("table 0", message)

    def test_select_compatible_group_by_result_name_and_group_value(self):
        config = DataFrameMergeConfig(result_names=["_result"], groups=[{"host": "south"}])
        data_frame = merge_tables_to_data_frame(self.conflicting_tables, config)

        self.assertEqual(pd.DataFrame, type(data_frame))
        self.assertEqual(2, len(data_frame))
        self.assertEqual("Int64", str(data_frame["_value"].dtype))
        self.assertTrue(pd.isna(data_frame["_value"].iloc[0]))
        self.assertEqual(0, data_frame["_value"].iloc[1])
        self.assertFalse(pd.isna(data_frame["_value"].iloc[1]))
        self.assertEqual(["south", "south"], data_frame["host"].tolist())
        self.assertEqual(["_result", "_result"], data_frame[RESULT_SOURCE_COLUMN].tolist())
        for group in data_frame[GROUP_SOURCE_COLUMN]:
            self.assertEqual("south", group["host"])
            self.assertIsNone(group["_value"])

    def test_declaration_only_table_keeps_declaration_without_data_row(self):
        data_frame = merge_tables_to_data_frame(
            self.conflicting_tables, DataFrameMergeConfig(groups=[{"host": "north"}]))

        self.assertEqual(0, len(data_frame))
        self.assertEqual("Int64", str(data_frame["_value"].dtype))
        self.assertEqual("string", str(data_frame["host"].dtype))
        self.assertIn(RESULT_SOURCE_COLUMN, data_frame.columns)
        self.assertIn(GROUP_SOURCE_COLUMN, data_frame.columns)

    def test_excluding_conflicting_group_succeeds_while_conflict_stays_in_response(self):
        config = DataFrameMergeConfig(result_names=["_result"],
                                      groups=[{"host": "north"}, {"host": "south"}])
        data_frame = merge_tables_to_data_frame(self.conflicting_tables, config)

        self.assertEqual(2, len(data_frame))
        self.assertEqual("Int64", str(data_frame["_value"].dtype))
        self.assertEqual({"south"}, set(data_frame["host"]))

    def test_conflicting_group_selected_alone_keeps_its_own_declaration(self):
        data_frame = merge_tables_to_data_frame(
            self.conflicting_tables, DataFrameMergeConfig(result_names=["_other"]))

        self.assertEqual(1, len(data_frame))
        self.assertEqual("string", str(data_frame["_value"].dtype))
        self.assertEqual("high", data_frame["_value"].iloc[0])

    def test_big_integer_null_and_default_are_preserved_without_float_or_object(self):
        data_frame = merge_tables_to_data_frame(self.compatible_tables)

        self.assertEqual(4, len(data_frame))
        self.assertEqual("Int64", str(data_frame["_value"].dtype))
        self.assertNotIn("float64", [str(dtype) for dtype in data_frame.dtypes])
        self.assertEqual(BIG_INTEGER, data_frame["_value"].dropna().max())
        self.assertEqual(1, int(data_frame["_value"].isna().sum()))

        north = select_source_rows(data_frame, "_result", {"host": "north"})
        south = select_source_rows(data_frame, "_result", {"host": "south"})
        self.assertEqual(2, len(north))
        self.assertEqual(2, len(south))
        # Empty cell filled from the '#default' declaration versus a genuinely missing value.
        self.assertEqual([42, BIG_INTEGER], north["_value"].tolist())
        self.assertTrue(pd.isna(south["_value"].iloc[0]))
        self.assertEqual(7, south["_value"].iloc[1])

        # The default can be traced back to the original Flux column declaration.
        north_table = [table for table in self.compatible_tables
                       if table.records and table.records[0].values["host"] == "north"][0]
        value_column = [column for column in north_table.columns if column.label == "_value"][0]
        self.assertEqual("42", value_column.default_value)
        self.assertEqual("long", value_column.data_type)

    def test_reused_table_number_does_not_collapse_rows_from_different_groups(self):
        data_frame = merge_tables_to_data_frame(self.compatible_tables)

        groups = data_frame[GROUP_SOURCE_COLUMN].map(lambda group: group["host"]).tolist()
        self.assertEqual(2, groups.count("north"))
        self.assertEqual(2, groups.count("south"))
        self.assertEqual([0, 0, 0, 0], data_frame["table"].tolist())

    def test_swapped_table_order_gives_identical_content(self):
        original = merge_tables_to_data_frame(self.compatible_tables)
        swapped = merge_tables_to_data_frame(self.swapped_tables)

        self.assertEqual(len(original), len(swapped))
        values = swapped["_value"].dropna().tolist()
        self.assertIn(BIG_INTEGER, values)
        self.assertIn(42, values)
        self.assertIn(7, values)
        self.assertEqual(1, int(swapped["_value"].isna().sum()))
        self.assertEqual({"north", "south"},
                         set(swapped[GROUP_SOURCE_COLUMN].map(lambda group: group["host"])))
        self.assertEqual("Int64", str(swapped["_value"].dtype))

    def test_group_key_is_also_an_ordinary_output_column(self):
        data_frame = merge_tables_to_data_frame(self.compatible_tables)

        self.assertIn("host", data_frame.columns)
        self.assertEqual(["north", "north", "south", "south"], data_frame["host"].tolist())
        self.assertEqual("string", str(data_frame["host"].dtype))
        for host, group in zip(data_frame["host"], data_frame[GROUP_SOURCE_COLUMN]):
            self.assertEqual(host, group["host"])

    def test_every_row_keeps_result_and_group_identity(self):
        data_frame = merge_tables_to_data_frame(self.compatible_tables)

        self.assertEqual(["_result"] * 4, data_frame[RESULT_SOURCE_COLUMN].tolist())
        expected_business_columns = {"result", "table", "_time", "_value", "host"}
        self.assertTrue(expected_business_columns.issubset(set(data_frame.columns)))
        for group in data_frame[GROUP_SOURCE_COLUMN]:
            self.assertIn("host", group)
            self.assertIn("_value", group)

    def test_reserved_source_columns_are_not_used_to_overwrite_business_columns(self):
        table = FluxTable()
        table.columns = [FluxColumn(index=0, label=RESULT_SOURCE_COLUMN, data_type="string", group=False)]
        table.records = []

        with self.assertRaises(FluxMergedDataFrameException):
            merge_tables_to_data_frame([table])

    def test_no_selection_returns_frame_with_source_columns_only(self):
        data_frame = merge_tables_to_data_frame(
            self.conflicting_tables, DataFrameMergeConfig(groups=[{"host": "nowhere"}]))

        self.assertEqual(0, len(data_frame))
        self.assertEqual([RESULT_SOURCE_COLUMN, GROUP_SOURCE_COLUMN], list(data_frame.columns))

    def test_custom_data_frame_index(self):
        data_frame = merge_tables_to_data_frame(self.compatible_tables, data_frame_index=["_time"])

        self.assertEqual("_time", data_frame.index.name)
        self.assertNotIn("_time", data_frame.columns)

    def test_result_name_filter(self):
        data_frame = merge_tables_to_data_frame(
            self.conflicting_tables, DataFrameMergeConfig(result_names=["_other"]))

        self.assertEqual(1, len(data_frame))
        self.assertEqual({"_other"}, set(data_frame[RESULT_SOURCE_COLUMN]))

    def test_query_api_uses_explicit_merge_method(self):
        query_api = QueryApi.__new__(QueryApi)
        captured = {}

        def fake_query(query, org=None, params=None):
            captured.update(query=query, org=org, params=params)
            return self.compatible_tables

        query_api.query = fake_query
        config = DataFrameMergeConfig(groups=[{"host": "north"}])
        data_frame = query_api.query_merged_data_frame(
            "flux query", config=config, org="my-org", data_frame_index=["_time"], params={"p": 1})

        self.assertEqual("flux query", captured["query"])
        self.assertEqual("my-org", captured["org"])
        self.assertEqual({"p": 1}, captured["params"])
        self.assertEqual("_time", data_frame.index.name)
        self.assertEqual(2, len(data_frame))
        self.assertEqual("Int64", str(data_frame["_value"].dtype))

    def test_legacy_query_data_frame_behavior_untouched_in_parser(self):
        # The parser still exposes one FluxTable per annotated CSV table, including the
        # declaration-only table and the reused table number across different groups.
        self.assertEqual(3, len(self.conflicting_tables))
        self.assertEqual(0, len(self.conflicting_tables[0].records))
        self.assertEqual(2, len(self.conflicting_tables[1].records))
        self.assertEqual(1, len(self.conflicting_tables[2].records))

        self.assertEqual(2, len(self.compatible_tables))
        table_numbers = [record.values["table"]
                         for table in self.compatible_tables for record in table.records]
        self.assertEqual([0, 0, 0, 0], table_numbers)
        self.assertEqual(["north", "north"], [record.values["host"]
                                              for record in self.compatible_tables[0].records])
        self.assertEqual(["south", "south"], [record.values["host"]
                                              for record in self.compatible_tables[1].records])


if __name__ == "__main__":
    unittest.main()
