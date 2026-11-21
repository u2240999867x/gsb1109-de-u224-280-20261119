"""Offline regression tests for two ways of damaging a readable parquet file.

The fixture family lives in ``tests/fixtures/corrupt`` and is rebuilt by
``tests/fixtures/corrupt/build_fixtures.py``.  Both damaged files are derived
from the same tiny source with two row groups and two columns:

* ``data_page_corrupt.parquet`` keeps the source footer byte-for-byte and only
  rewrites the first value-length word in row group 1 / column ``payload``.
* ``wrong_offset_same_page_boundary.parquet`` keeps every data byte and only
  rewrites the footer's ``data_page_offset`` for row group 1 / column
  ``payload`` so it points at the row group 0 / column ``id`` page.
* ``wrong_offset_mid_header.parquet`` is a footer-only wrong-offset control
  whose declared offset lands inside a page header.

The assertions deliberately establish the fixtures from source bytes and
footer fields before looking at any reader error, and the CLI is exercised as a
subprocess so the public output and exit status are what users see.
"""

import struct
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
import pytest
from thrift.protocol import TCompactProtocol
from thrift.transport import TTransport

from parquet_tools.gen_py.parquet.ttypes import PageHeader
from parquet_tools.parquet.reader import get_filemetadata

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "corrupt"

SOURCE = FIXTURE_DIR / "source.parquet"
NEIGHBOR = FIXTURE_DIR / "neighbor.parquet"
PAGE_CORRUPT = FIXTURE_DIR / "data_page_corrupt.parquet"
WRONG_OFFSET = FIXTURE_DIR / "wrong_offset_same_page_boundary.parquet"
WRONG_OFFSET_MID_HEADER = FIXTURE_DIR / "wrong_offset_mid_header.parquet"

CORRUPTED = [PAGE_CORRUPT, WRONG_OFFSET, WRONG_OFFSET_MID_HEADER]
SOURCE_PAYLOAD = [f"p{index:02d}-alpha" for index in range(8)]
NEIGHBOR_PAYLOAD = [f"n{index:02d}-omega" for index in range(8)]


def _read_bytes(path: Path) -> bytes:
    with open(path, "rb") as file:
        return file.read()


def _footer_bounds(data: bytes):
    assert data[:4] == b"PAR1"
    assert data[-4:] == b"PAR1"
    footer_len = struct.unpack("<i", data[-8:-4])[0]
    footer_start = len(data) - 8 - footer_len
    assert 4 <= footer_start < len(data) - 8
    return footer_start, footer_len


def _metadata(path: Path):
    return get_filemetadata(str(path))


def _page_header(data: bytes, offset: int):
    transport = TTransport.TMemoryBuffer(data[offset:])
    header = PageHeader()
    header.read(TCompactProtocol.TCompactProtocol(transport))
    return header, transport._buffer.tell()


def _target_column(metadata, row_group=1, column=1):
    return metadata.row_groups[row_group].columns[column].meta_data


def _target_value_start(source_data: bytes, source_metadata) -> int:
    target = _target_column(source_metadata)
    header, header_len = _page_header(source_data, target.data_page_offset)
    assert header.type == 0
    payload_start = target.data_page_offset + header_len
    definition_len = struct.unpack("<i", source_data[payload_start:payload_start + 4])[0]
    return payload_start + 4 + definition_len


def _first_difference(left: bytes, right: bytes) -> int:
    for index, (a, b) in enumerate(zip(left, right)):
        if a != b:
            return index
    if len(left) != len(right):
        return min(len(left), len(right))
    raise AssertionError("byte sequences are identical")


def _table_rows(stdout: str):
    return [
        [cell.strip() for cell in line.strip().strip("|").split("|")]
        for line in stdout.splitlines()
        if line.startswith("|") and "---" not in line and "payload" not in line and "id" not in line
    ]


def _run_cli(*arguments: str):
    return subprocess.run(
        [sys.executable, "-m", "parquet_tools.cli", *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )


def _normalized_stderr(completed, path: Path) -> str:
    return completed.stderr.replace(str(path), "<file>")


def _last_line(text: str) -> str:
    lines = [line for line in text.strip().splitlines() if line.strip()]
    return lines[-1] if lines else ""


def _arrow_exception(path: Path, columns=None, row_group=None):
    try:
        if row_group is None:
            pq.read_table(str(path), columns=columns)
        else:
            pq.ParquetFile(str(path)).read_row_group(row_group, columns=columns)
    except BaseException as error:  # noqa: BLE001 - the type is the assertion
        return error
    raise AssertionError(f"expected a read failure for {path.name}")


def _read_trace(path: Path, columns, row_group=1):
    """Record exact byte reads Arrow performs for one row group."""

    class RecordingFile:
        def __init__(self, file):
            self._file = file
            self.reads = []

        def read(self, size=-1):
            data = self._file.read(size)
            self.reads.append((self._file.tell() - len(data), len(data)))
            return data

        def read_at(self, size, offset):
            self._file.seek(offset)
            data = self._file.read(size)
            self.reads.append((offset, len(data)))
            return data

        def seek(self, offset, whence=0):
            return self._file.seek(offset, whence)

        def tell(self):
            return self._file.tell()

        def close(self):
            self._file.close()

        def readable(self):
            return True

        def writable(self):
            return False

        def seekable(self):
            return True

        @property
        def closed(self):
            return self._file.closed

    recording = RecordingFile(open(path, "rb"))
    python_file = pa.PythonFile(recording, mode="r")
    try:
        parquet_file = pq.ParquetFile(python_file)
        recording.reads.clear()
        error = None
        try:
            parquet_file.read_row_group(row_group, columns=columns)
        except BaseException as caught:  # noqa: BLE001 - the trace is the evidence
            error = caught
        return error, recording.reads
    finally:
        python_file.close()


class TestFixtureProvenance:
    """Confirm fixture damage from bytes and footer fields, not from errors."""

    def test_source_has_two_row_groups_two_columns_and_distinct_values(self):
        metadata = _metadata(SOURCE)
        assert len(metadata.schema) == 3  # root + id + payload
        assert [element.name for element in metadata.schema] == ["schema", "id", "payload"]
        assert len(metadata.row_groups) == 2
        assert [group.num_rows for group in metadata.row_groups] == [4, 4]

        table = pq.read_table(str(SOURCE))
        assert table.column_names == ["id", "payload"]
        assert table.column("id").to_pylist() == list(range(8))
        assert table.column("payload").to_pylist() == SOURCE_PAYLOAD
        assert len(set(SOURCE_PAYLOAD)) == 8
        assert set(SOURCE_PAYLOAD[:4]).isdisjoint(SOURCE_PAYLOAD[4:])

        neighbor = pq.read_table(str(NEIGHBOR))
        assert neighbor.column("id").to_pylist() == list(range(100, 108))
        assert neighbor.column("payload").to_pylist() == NEIGHBOR_PAYLOAD
        assert set(SOURCE_PAYLOAD).isdisjoint(NEIGHBOR_PAYLOAD)

    def test_page_corruption_preserves_footer_and_only_rewrites_a_value_length(self):
        source = _read_bytes(SOURCE)
        damaged = _read_bytes(PAGE_CORRUPT)
        source_footer_start, _ = _footer_bounds(source)
        damaged_footer_start, _ = _footer_bounds(damaged)

        assert len(damaged) == len(source)
        assert source_footer_start == damaged_footer_start
        assert damaged[damaged_footer_start:] == source[source_footer_start:]

        metadata = _metadata(SOURCE)
        values_start = _target_value_start(source, metadata)
        original_length = struct.unpack("<i", source[values_start:values_start + 4])[0]
        corrupted_length = struct.unpack("<i", damaged[values_start:values_start + 4])[0]
        assert original_length == len(SOURCE_PAYLOAD[4])
        assert corrupted_length == 5
        assert corrupted_length != original_length

        changed = [
            index
            for index, (left, right) in enumerate(zip(damaged, source))
            if left != right
        ]
        assert changed == [values_start]
        assert damaged[values_start + 4:values_start + 4 + corrupted_length] == b"p04-a"

    @pytest.mark.parametrize(
        "damaged_path, expected_offset",
        [
            (WRONG_OFFSET, lambda metadata: metadata.row_groups[0].columns[0].meta_data.data_page_offset),
            (WRONG_OFFSET_MID_HEADER, lambda metadata: metadata.row_groups[0].columns[0].meta_data.data_page_offset + 5),
        ],
    )
    def test_wrong_offset_preserves_all_data_pages_and_changes_only_footer(self, damaged_path, expected_offset):
        source = _read_bytes(SOURCE)
        damaged = _read_bytes(damaged_path)
        source_footer_start, _ = _footer_bounds(source)
        damaged_footer_start, _ = _footer_bounds(damaged)
        assert damaged[:damaged_footer_start] == source[:source_footer_start]

        source_metadata = _metadata(SOURCE)
        damaged_metadata = _metadata(damaged_path)
        expected = deepcopy(source_metadata)
        declared = expected_offset(source_metadata)
        expected.row_groups[1].columns[1].meta_data.data_page_offset = declared

        assert damaged_metadata == expected
        assert damaged_metadata != source_metadata
        assert _target_column(damaged_metadata).data_page_offset == declared
        assert _target_column(source_metadata).data_page_offset != declared

        for row_group in range(len(source_metadata.row_groups)):
            for column in range(len(source_metadata.row_groups[row_group].columns)):
                if (row_group, column) == (1, 1):
                    continue
                assert (damaged_metadata.row_groups[row_group].columns[column]
                        == source_metadata.row_groups[row_group].columns[column])

    def test_wrong_offset_points_at_a_valid_foreign_page(self):
        source = _read_bytes(SOURCE)
        metadata = _metadata(SOURCE)
        declared = _target_column(_metadata(WRONG_OFFSET)).data_page_offset
        assert declared == metadata.row_groups[0].columns[0].meta_data.data_page_offset

        header, header_len = _page_header(source, declared)
        assert header.type == 0  # DATA_PAGE
        assert header.compressed_page_size == 38
        assert header.uncompressed_page_size == 38
        assert header.data_page_header.num_values == 4
        assert header_len + header.compressed_page_size <= len(source) - declared

    def test_mid_header_wrong_offset_is_not_a_readable_page(self):
        source = _read_bytes(SOURCE)
        metadata = _metadata(SOURCE)
        declared = _target_column(_metadata(WRONG_OFFSET_MID_HEADER)).data_page_offset
        assert declared == metadata.row_groups[0].columns[0].meta_data.data_page_offset + 5

        header, _ = _page_header(source, declared)
        assert header.type is None
        assert header.compressed_page_size is None
        assert header.data_page_header is None


class TestInspectAndPublicFailures:
    """Exercise the public CLI surface for both damaged files and controls."""

    def test_source_first_sample_and_full_export_are_the_reference_output(self):
        first_row = _table_rows(_run_cli("show", "--head", "1", str(SOURCE)).stdout)
        assert first_row == [["0", SOURCE_PAYLOAD[0]]]

        first_csv = _run_cli("csv", "--head", "1", str(SOURCE))
        assert first_csv.returncode == 0
        assert first_csv.stderr == ""
        assert first_csv.stdout.splitlines()[:2] == ["id,payload", "0,p00-alpha"]

        full_csv = _run_cli("csv", str(SOURCE))
        assert full_csv.returncode == 0
        assert full_csv.stderr == ""
        rows = [line.split(",") for line in full_csv.stdout.strip().splitlines()[1:]]
        assert rows == [[str(index), value] for index, value in enumerate(SOURCE_PAYLOAD)]

        shown = _run_cli("show", str(SOURCE))
        assert shown.returncode == 0
        assert shown.stderr == ""
        assert _table_rows(shown.stdout) == [[str(index), value] for index, value in enumerate(SOURCE_PAYLOAD)]

    @pytest.mark.parametrize("damaged_path", [PAGE_CORRUPT, WRONG_OFFSET])
    def test_simple_and_detail_inspect_work_on_both_damaged_files(self, damaged_path):
        source_simple = _run_cli("inspect", str(SOURCE))
        assert source_simple.returncode == 0
        assert source_simple.stderr == ""

        simple = _run_cli("inspect", str(damaged_path))
        assert simple.returncode == 0
        assert simple.stderr == ""
        assert "num_columns: 2" in simple.stdout
        assert "num_rows: 8" in simple.stdout
        assert "num_row_groups: 2" in simple.stdout
        assert "Column(payload)" in simple.stdout

        detail = _run_cli("inspect", "--detail", str(damaged_path))
        assert detail.returncode == 0
        assert detail.stderr == ""
        assert detail.stdout.startswith("FileMetaData")
        assert detail.stdout.count("data_page_offset") == 4
        assert "path_in_schema = list" in detail.stdout
        assert "num_rows = 8" in detail.stdout

    def test_inspect_reports_the_declared_footer_offsets_verbatim(self):
        source_metadata = _metadata(SOURCE)
        expected_offsets = [
            column.meta_data.data_page_offset
            for group in source_metadata.row_groups
            for column in group.columns
        ]
        assert expected_offsets == sorted(expected_offsets)

        def declared_offsets(path):
            output = _run_cli("inspect", "--detail", str(path))
            assert output.returncode == 0
            return [
                int(line.split("=")[1])
                for line in output.stdout.splitlines()
                if "data_page_offset =" in line
            ]

        assert declared_offsets(SOURCE) == expected_offsets
        assert declared_offsets(PAGE_CORRUPT) == expected_offsets

        wrong_offsets = list(expected_offsets)
        wrong_offsets[-1] = source_metadata.row_groups[0].columns[0].meta_data.data_page_offset
        assert declared_offsets(WRONG_OFFSET) == wrong_offsets

        mid_offsets = list(expected_offsets)
        mid_offsets[-1] = source_metadata.row_groups[0].columns[0].meta_data.data_page_offset + 5
        assert declared_offsets(WRONG_OFFSET_MID_HEADER) == mid_offsets

    def test_show_and_csv_share_the_outer_message_for_page_and_wrong_offset(self):
        page_show = _run_cli("show", str(PAGE_CORRUPT))
        offset_show = _run_cli("show", str(WRONG_OFFSET))
        assert page_show.returncode == 0
        assert offset_show.returncode == 0
        assert page_show.stdout == ""
        assert offset_show.stdout == ""
        assert _normalized_stderr(page_show, PAGE_CORRUPT) == _normalized_stderr(offset_show, WRONG_OFFSET)
        assert "cannot be read as parquet." in page_show.stderr
        assert "File matching that expression not found." in page_show.stderr

        page_csv = _run_cli("csv", str(PAGE_CORRUPT))
        offset_csv = _run_cli("csv", str(WRONG_OFFSET))
        assert page_csv.returncode != 0
        assert offset_csv.returncode != 0
        assert page_csv.stdout == ""
        assert offset_csv.stdout == ""
        assert "FileNotFoundException: File matching that expression not found." in _last_line(page_csv.stderr)
        assert "FileNotFoundException: File matching that expression not found." in _last_line(offset_csv.stderr)

    @pytest.mark.parametrize("damaged_path", [PAGE_CORRUPT, WRONG_OFFSET])
    @pytest.mark.parametrize(
        "arguments",
        [
            ("show", "--head", "1"),
            ("show", "--columns", "id"),
            ("csv", "--head", "1"),
            ("csv", "--columns", "id"),
        ],
    )
    def test_head_and_column_selection_do_not_bypass_the_damaged_later_group(self, damaged_path, arguments):
        # `id` is healthy in both row groups, and --head 1 is below the first
        # row group.  Failure therefore proves the CLI read all row groups and
        # all columns before applying head/column selection.
        completed = _run_cli(*arguments, str(damaged_path))
        assert completed.stdout == ""
        assert "File matching that expression not found." in completed.stderr

    @pytest.mark.parametrize("damaged_path", [PAGE_CORRUPT, WRONG_OFFSET])
    def test_arrow_projection_succeeds_exactly_where_the_cli_options_do_not(self, damaged_path):
        # Arrow can read only `id` from the affected row group; the CLI cannot,
        # because it loads the whole file before applying --columns/--head.
        parquet_file = pq.ParquetFile(str(damaged_path))
        assert parquet_file.read_row_group(1, columns=["id"]).column("id").to_pylist() == [4, 5, 6, 7]
        assert pq.read_table(str(damaged_path), columns=["id"]).column("id").to_pylist() == list(range(8))

        for arguments in (("show", "--columns", "id"), ("show", "--head", "1"),
                          ("csv", "--columns", "id"), ("csv", "--head", "1")):
            completed = _run_cli(*arguments, str(damaged_path))
            assert completed.stdout == ""
            assert completed.returncode != 0 or "matching that expression" in completed.stderr

    def test_inspect_accepts_exactly_one_input_file(self):
        multiple = _run_cli("inspect", str(SOURCE), str(NEIGHBOR))
        assert multiple.returncode != 0
        assert multiple.stdout == ""
        assert "unrecognized arguments" in multiple.stderr

        for path in (SOURCE, NEIGHBOR):
            single = _run_cli("inspect", str(path))
            assert single.returncode == 0
            assert single.stderr == ""
            assert "num_row_groups: 2" in single.stdout

    def test_missing_path_is_only_reported_as_missing(self, tmp_path):
        missing = tmp_path / "does-not-exist.parquet"
        for command in ("inspect", "show"):
            completed = _run_cli(command, str(missing))
            assert completed.returncode == 0
            assert completed.stdout == ""
            assert completed.stderr == f"File({missing}) not found\n"
            assert "cannot be read as parquet" not in completed.stderr
            assert "matching that expression" not in completed.stderr

        csv_result = _run_cli("csv", str(missing))
        assert csv_result.returncode != 0
        assert csv_result.stdout == ""
        assert f"FileNotFoundException: File({missing}) not found" in _last_line(csv_result.stderr)
        assert "cannot be read as parquet" not in csv_result.stderr
        assert "matching that expression" not in csv_result.stderr

    @pytest.mark.parametrize("command", ["show", "csv"])
    def test_mid_header_wrong_offset_escapes_the_arrow_invalid_layer(self, command):
        completed = _run_cli(command, str(WRONG_OFFSET_MID_HEADER))
        assert completed.returncode != 0
        assert completed.stdout == ""
        assert "Deserializing page header failed." in _last_line(completed.stderr)
        assert "cannot be read as parquet" not in completed.stderr
        assert "matching that expression" not in completed.stderr
        assert "not found" not in completed.stderr


class TestArrowReadFailures:
    """Read correct/wrong columns and row groups with Arrow directly."""

    @pytest.mark.parametrize("damaged_path", CORRUPTED)
    def test_healthy_row_group_and_healthy_column_stay_readable(self, damaged_path):
        source_table = pq.read_table(str(SOURCE))
        parquet_file = pq.ParquetFile(str(damaged_path))

        assert parquet_file.read_row_group(0).equals(source_table.slice(0, 4))
        assert (parquet_file.read_row_group(0, columns=["payload"])
                .equals(source_table.slice(0, 4).select(["payload"])))
        assert parquet_file.read_row_group(1, columns=["id"]).column("id").to_pylist() == [4, 5, 6, 7]
        assert pq.read_table(str(damaged_path), columns=["id"]).column("id").to_pylist() == list(range(8))

        source = _read_bytes(SOURCE)
        damaged = _read_bytes(damaged_path)
        source_metadata = _metadata(SOURCE)
        first_group_end = source_metadata.row_groups[1].columns[0].meta_data.data_page_offset
        assert damaged[4:first_group_end] == source[4:first_group_end]

        # The id page inside the affected row group is also a healthy
        # neighboring page, even when the payload page is corrupted.
        id_page_start = source_metadata.row_groups[1].columns[0].meta_data.data_page_offset
        payload_page_start = source_metadata.row_groups[1].columns[1].meta_data.data_page_offset
        assert damaged[id_page_start:payload_page_start] == source[id_page_start:payload_page_start]

    @pytest.mark.parametrize(
        "damaged_path, expected_error",
        [
            (PAGE_CORRUPT, pa.ArrowInvalid),
            (WRONG_OFFSET, pa.ArrowInvalid),
            (WRONG_OFFSET_MID_HEADER, OSError),
        ],
    )
    def test_affected_row_group_and_column_fail_at_the_expected_layer(self, damaged_path, expected_error):
        parquet_file = pq.ParquetFile(str(damaged_path))
        with pytest.raises(expected_error):
            parquet_file.read_row_group(1)
        with pytest.raises(expected_error):
            parquet_file.read_row_group(1, columns=["payload"])
        with pytest.raises(expected_error):
            pq.read_table(str(damaged_path), columns=["payload"])
        with pytest.raises(expected_error):
            pq.read_table(str(damaged_path))

    def test_page_and_wrong_offset_share_an_arrow_exception_but_diverge_earlier(self):
        source = _read_bytes(SOURCE)
        page = _read_bytes(PAGE_CORRUPT)
        offset = _read_bytes(WRONG_OFFSET)
        source_footer_start, _ = _footer_bounds(source)
        page_footer_start, _ = _footer_bounds(page)
        offset_footer_start, _ = _footer_bounds(offset)

        # The page-corruption file has a valid footer identical to the source.
        # Its bytes diverge first inside the payload data page.
        page_error = _arrow_exception(PAGE_CORRUPT, columns=["payload"])
        offset_error = _arrow_exception(WRONG_OFFSET, columns=["payload"])
        assert isinstance(page_error, pa.ArrowInvalid)
        assert isinstance(offset_error, pa.ArrowInvalid)
        assert str(page_error) == str(offset_error) == "Invalid or truncated PLAIN-encoded BYTE_ARRAY data"

        metadata = _metadata(SOURCE)
        values_start = _target_value_start(source, metadata)
        assert _first_difference(page, source) == values_start
        assert values_start < page_footer_start
        assert _metadata(PAGE_CORRUPT) == metadata

        # The wrong-offset file keeps all data pages and diverges first in the
        # footer, before any page is decoded.  Its eventual Arrow exception is
        # the same public cause, but the earliest anomaly point is metadata.
        assert _first_difference(offset, source) >= offset_footer_start
        assert offset[:offset_footer_start] == source[:source_footer_start]
        assert _metadata(WRONG_OFFSET) != metadata

        page_cli = _run_cli("show", str(PAGE_CORRUPT))
        offset_cli = _run_cli("show", str(WRONG_OFFSET))
        assert _normalized_stderr(page_cli, PAGE_CORRUPT) == _normalized_stderr(offset_cli, WRONG_OFFSET)

    def test_mid_header_wrong_offset_is_not_caught_by_the_arrow_invalid_layer(self):
        page_error = _arrow_exception(PAGE_CORRUPT, columns=["payload"])
        mid_error = _arrow_exception(WRONG_OFFSET_MID_HEADER, columns=["payload"])
        assert isinstance(page_error, pa.ArrowInvalid)
        assert isinstance(mid_error, OSError)
        assert not isinstance(mid_error, pa.ArrowInvalid)
        assert "page header" in str(mid_error)

    def test_read_trace_shows_different_earliest_anomaly_points(self):
        source = _read_bytes(SOURCE)
        metadata = _metadata(SOURCE)
        target = _target_column(metadata)
        declared = _target_column(_metadata(WRONG_OFFSET)).data_page_offset
        assert declared != target.data_page_offset

        def final_read(path):
            error, reads = _read_trace(path, ["payload"])
            assert error is not None
            return error, reads[-1]

        page_error, page_read = final_read(PAGE_CORRUPT)
        offset_error, offset_read = final_read(WRONG_OFFSET)

        # Both files fail while reading row group 1 / payload with the same
        # Arrow exception, but they do not read the same region: the damaged
        # page file is read at the true data_page_offset, while the wrong
        # offset file is read at the foreign offset declared in the footer.
        assert isinstance(page_error, pa.ArrowInvalid)
        assert isinstance(offset_error, pa.ArrowInvalid)
        assert page_read[0] == target.data_page_offset
        assert offset_read[0] == declared
        assert page_read[0] != offset_read[0]
        assert page_read[1] == target.total_compressed_size
        assert offset_read[1] == target.total_compressed_size

        source_page = _read_bytes(SOURCE)[target.data_page_offset:target.data_page_offset + target.total_compressed_size]
        damaged_page = _read_bytes(PAGE_CORRUPT)[target.data_page_offset:target.data_page_offset + target.total_compressed_size]
        foreign_page = _read_bytes(WRONG_OFFSET)[declared:declared + target.total_compressed_size]
        assert source_page != damaged_page
        assert source_page == _read_bytes(WRONG_OFFSET)[target.data_page_offset:target.data_page_offset + target.total_compressed_size]
        assert damaged_page != foreign_page

    def test_restoring_the_changed_page_length_recovers_the_source(self, tmp_path):
        source = _read_bytes(SOURCE)
        damaged = bytearray(_read_bytes(PAGE_CORRUPT))
        metadata = _metadata(SOURCE)
        values_start = _target_value_start(source, metadata)

        recovered = bytearray(damaged)
        recovered[values_start:values_start + 4] = source[values_start:values_start + 4]
        assert bytes(recovered) == source

        output = tmp_path / "recovered-page.parquet"
        output.write_bytes(recovered)
        assert pq.read_table(str(output)).equals(pq.read_table(str(SOURCE)))

    @pytest.mark.parametrize("damaged_path", [WRONG_OFFSET, WRONG_OFFSET_MID_HEADER])
    def test_restoring_the_changed_footer_recovers_the_source(self, tmp_path, damaged_path):
        source = _read_bytes(SOURCE)
        damaged = bytearray(_read_bytes(damaged_path))
        source_footer_start, _ = _footer_bounds(source)
        damaged_footer_start, _ = _footer_bounds(damaged)
        assert damaged[:damaged_footer_start] == source[:source_footer_start]

        recovered = damaged[:damaged_footer_start] + source[source_footer_start:]
        assert recovered == source

        output = tmp_path / f"recovered-{damaged_path.stem}.parquet"
        output.write_bytes(recovered)
        assert pq.read_table(str(output)).equals(pq.read_table(str(SOURCE)))


class TestInputExpressionsAndEntryPointScope:
    """Compare expressions with a healthy neighbor and measure read scope."""

    @pytest.mark.parametrize("damaged_path", [PAGE_CORRUPT, WRONG_OFFSET])
    def test_neighbor_and_damaged_file_in_the_same_expression(self, damaged_path):
        neighbor_table = pq.read_table(str(NEIGHBOR)).to_pydict()
        neighbor_cells = _table_rows(_run_cli("show", str(NEIGHBOR)).stdout)
        assert neighbor_cells == [
            [str(value), text]
            for value, text in zip(neighbor_table["id"], neighbor_table["payload"])
        ]

        for files in ([str(NEIGHBOR), str(damaged_path)], [str(damaged_path), str(NEIGHBOR)]):
            shown = _run_cli("show", *files)
            assert shown.returncode == 0
            assert _table_rows(shown.stdout) == neighbor_cells
            assert "cannot be read as parquet." in shown.stderr
            # A healthy neighbor yields a dataframe, so the aggregate
            # "no matching file" message is not reached.
            assert "File matching that expression not found." not in shown.stderr
            assert "p04-alpha" not in shown.stdout

            exported = _run_cli("csv", *files)
            assert exported.returncode == 0
            assert exported.stdout.splitlines()[0] == "id,payload"
            assert "100,n00-omega" in exported.stdout
            assert "104,n04-omega" in exported.stdout
            assert "p04-alpha" not in exported.stdout
            assert "cannot be read as parquet." in exported.stderr

    @pytest.mark.parametrize("damaged_path", [PAGE_CORRUPT, WRONG_OFFSET])
    def test_cross_file_head_is_the_only_early_stop(self, damaged_path):
        stopped = _run_cli("show", "--head", "8", str(NEIGHBOR), str(damaged_path))
        assert stopped.returncode == 0
        assert "n07-omega" in stopped.stdout
        assert stopped.stderr == ""

        crossed = _run_cli("show", "--head", "9", str(NEIGHBOR), str(damaged_path))
        assert crossed.returncode == 0
        assert "n07-omega" in crossed.stdout
        assert "cannot be read as parquet." in crossed.stderr
        # The neighbor already exceeds the row limit, so a dataframe exists and
        # the "no matching file" fallback is not printed; only the per-file
        # read warning identifies the damaged file.
        assert "File matching that expression not found." not in crossed.stderr

        exported_stopped = _run_cli("csv", "--head", "8", str(NEIGHBOR), str(damaged_path))
        assert exported_stopped.returncode == 0
        assert "n07-omega" in exported_stopped.stdout
        assert exported_stopped.stderr == ""

        exported_crossed = _run_cli("csv", "--head", "9", str(NEIGHBOR), str(damaged_path))
        assert exported_crossed.returncode == 0
        assert "n07-omega" in exported_crossed.stdout
        assert "cannot be read as parquet." in exported_crossed.stderr

    def test_mid_header_failure_prevents_the_neighbor_from_being_printed(self):
        for files in ([str(NEIGHBOR), str(WRONG_OFFSET_MID_HEADER)],
                      [str(WRONG_OFFSET_MID_HEADER), str(NEIGHBOR)]):
            shown = _run_cli("show", *files)
            assert shown.returncode != 0
            assert shown.stdout == ""
            assert "Deserializing page header failed." in _last_line(shown.stderr)

    def test_inspect_reads_only_footer_metadata_even_when_all_pages_are_zeroed(self, tmp_path):
        source = _read_bytes(SOURCE)
        footer_start, _ = _footer_bounds(source)
        zeroed = bytearray(source)
        zeroed[4:footer_start] = b"\x00" * (footer_start - 4)
        damaged_pages = tmp_path / "zeroed-data-pages.parquet"
        damaged_pages.write_bytes(zeroed)

        with pytest.raises((OSError, pa.ArrowInvalid)):
            pq.read_table(str(damaged_pages))
        failed_show = _run_cli("show", str(damaged_pages))
        assert failed_show.returncode != 0
        assert failed_show.stdout == ""

        source_inspect = _run_cli("inspect", str(SOURCE))
        zeroed_inspect = _run_cli("inspect", str(damaged_pages))
        assert zeroed_inspect.returncode == 0
        assert zeroed_inspect.stderr == ""
        assert zeroed_inspect.stdout == source_inspect.stdout

        source_detail = _run_cli("inspect", "--detail", str(SOURCE))
        zeroed_detail = _run_cli("inspect", "--detail", str(damaged_pages))
        assert zeroed_detail.returncode == 0
        assert zeroed_detail.stderr == ""
        assert zeroed_detail.stdout == source_detail.stdout
        assert "data_page_offset" in zeroed_detail.stdout

    def test_missing_page_wrong_offset_and_same_layer_cases_are_distinct(self, tmp_path):
        missing = tmp_path / "absent.parquet"
        missing_result = _run_cli("show", str(missing))
        page_result = _run_cli("show", str(PAGE_CORRUPT))
        offset_result = _run_cli("show", str(WRONG_OFFSET))
        mid_result = _run_cli("show", str(WRONG_OFFSET_MID_HEADER))

        assert f"File({missing}) not found" in missing_result.stderr
        assert "cannot be read as parquet" not in missing_result.stderr
        assert "matching that expression" not in missing_result.stderr

        assert "cannot be read as parquet." in page_result.stderr
        assert "matching that expression" in page_result.stderr
        assert f"File({PAGE_CORRUPT}) not found" not in page_result.stderr

        assert _normalized_stderr(page_result, PAGE_CORRUPT) == _normalized_stderr(offset_result, WRONG_OFFSET)

        assert "Deserializing page header failed." in _last_line(mid_result.stderr)
        assert "cannot be read as parquet" not in mid_result.stderr
        assert "matching that expression" not in mid_result.stderr
        assert f"File({WRONG_OFFSET_MID_HEADER}) not found" not in mid_result.stderr

        assert _normalized_stderr(page_result, PAGE_CORRUPT) != _normalized_stderr(mid_result, WRONG_OFFSET_MID_HEADER)
        assert missing_result.stderr != page_result.stderr
