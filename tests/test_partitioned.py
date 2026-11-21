import os

import boto3
import pandas as pd
import pyarrow.parquet as pq
import pytest

from parquet_tools.commands.partitioned import (
    PartitionConflictError, PartitionDeclaration, PartitionError,
    apply_filters, build_receipts, expand_manifest, parse_filter,
    parse_partitions, read_report, render_receipts, verify_file)
from parquet_tools.commands.utils import LocalParquetFile, S3ParquetFile

DECLARATION_COLUMNS = ('date', 'region')
DATE_FILTER = 'date==2026-01-02'


def _declaration(root):
    return PartitionDeclaration(
        business_root=root, columns=DECLARATION_COLUMNS)


def _by_name(receipts):
    return {os.path.basename(receipt.path): receipt for receipt in receipts}


# ---------------------------------------------------------------------------
# Manifest expansion and path decoding
# ---------------------------------------------------------------------------
class TestManifestAndPaths:
    def test_manifest_is_limited_to_business_root(self, partitioned_archive):
        archive = partitioned_archive
        declaration = _declaration(archive['root'])
        manifest = expand_manifest(declaration)
        paths = [pf.path for pf in manifest]

        assert len(manifest) == 6
        assert all(path.startswith(archive['root']) for path in paths)
        assert archive['decoy.parquet'] not in paths

    def test_plus_and_percent_segments_decode_once(self, partitioned_archive):
        archive = partitioned_archive
        declaration = _declaration(archive['root'])

        clean = parse_partitions(
            archive['clean.parquet'], declaration, is_s3=False)
        west = parse_partitions(
            archive['conflict.parquet'], declaration, is_s3=False)
        south = parse_partitions(
            archive['nulls.parquet'], declaration, is_s3=False)

        assert clean['region'] == 'us+east'
        assert west['region'] == 'eu+west'
        assert south['region'] == 'ap%south'
        assert south['region'] != 'ap+south', 'percent segment must be decoded only once'
        assert clean['date'] == '2026-01-01'

    def test_repeated_conflicting_layers_report_concrete_path(
            self, tmp_path):
        root = tmp_path / 'archive'
        target = root / 'region=a' / 'region=b' / 'date=2026-01-01' / 'f.parquet'
        target.parent.mkdir(parents=True)
        target.write_bytes(b'PAR1')
        declaration = _declaration(str(root))

        with pytest.raises(PartitionConflictError) as excinfo:
            parse_partitions(str(target), declaration, is_s3=False)
        error = excinfo.value
        assert error.column == 'region'
        assert error.expected == 'a'
        assert error.actual == 'b'
        assert str(target) in str(error)

    def test_non_partition_segments_are_ignored(self, partitioned_archive):
        archive = partitioned_archive
        declaration = _declaration(archive['root'])
        partitions = parse_partitions(
            archive['withheld.parquet'], declaration, is_s3=False)
        assert partitions == {'region': 'us+east', 'date': '2026-01-01'}

    def test_declaration_requires_columns(self):
        with pytest.raises(PartitionError):
            PartitionDeclaration(business_root='root', columns=())


# ---------------------------------------------------------------------------
# Reconciliation receipts on local storage
# ---------------------------------------------------------------------------
class TestLocalReceipts:
    def _receipts(self, partitioned_archive, filters=None):
        archive = partitioned_archive
        declaration = _declaration(archive['root'])
        manifest = expand_manifest(declaration)
        return build_receipts(manifest, declaration, filters=filters), manifest

    def test_statuses_for_every_scenario(self, partitioned_archive):
        receipts, _ = self._receipts(partitioned_archive)
        by_name = _by_name(receipts)

        assert by_name['clean.parquet'].status == 'passed'
        assert by_name['fill_region.parquet'].status == 'passed'
        assert by_name['nostats.parquet'].status == 'passed'
        assert by_name['withheld.parquet'].status == 'withheld'
        assert by_name['conflict.parquet'].status == 'failed'
        assert by_name['nulls.parquet'].status == 'failed'

    def test_clean_groups_verified_from_statistics_without_decoding(
            self, partitioned_archive, monkeypatch):
        archive = partitioned_archive
        decoded_groups = []
        original = pq.ParquetFile.read_row_group

        def spy(self, index, columns=None, **kwargs):
            decoded_groups.append(index)
            return original(self, index, columns=columns, **kwargs)

        monkeypatch.setattr(pq.ParquetFile, 'read_row_group', spy)
        receipt = verify_file(
            LocalParquetFile(path=archive['clean.parquet']),
            _declaration(archive['root']))

        assert receipt.status == 'passed'
        assert [group.status for group in receipt.groups] == [
            'verified', 'verified']
        assert decoded_groups == []

    def test_missing_physical_region_is_filled_from_path(
            self, partitioned_archive):
        archive = partitioned_archive
        receipt = verify_file(
            LocalParquetFile(path=archive['fill_region.parquet']),
            _declaration(archive['root']))

        assert receipt.region_filled is True
        assert receipt.conflicts == []
        assert len(receipt.groups) == 3
        assert all(group.status == 'filled_from_path'
                   for group in receipt.groups)
        # Even the row group whose measurement statistics were stripped needs
        # no region decoding because no physical region column exists.
        assert all('us+east' in group.detail for group in receipt.groups)

    def test_matching_file_with_stripped_group_statistics_is_decoded(
            self, partitioned_archive, monkeypatch):
        archive = partitioned_archive
        decoded_groups = []
        original = pq.ParquetFile.read_row_group

        def spy(self, index, columns=None, **kwargs):
            decoded_groups.append(index)
            return original(self, index, columns=columns, **kwargs)

        monkeypatch.setattr(pq.ParquetFile, 'read_row_group', spy)
        receipt = verify_file(
            LocalParquetFile(path=archive['nostats.parquet']),
            _declaration(archive['root']))

        assert receipt.region_filled is False
        assert receipt.status == 'passed'
        assert receipt.conflicts == []
        # Only the statistics-stripped late group is decoded; the first two
        # are settled from trusted metadata.
        assert decoded_groups == [2]
        assert [group.status for group in receipt.groups] == [
            'verified', 'verified', 'verified']
        assert 'decoded group matches' in receipt.groups[2].detail

    def test_path_only_partition_annotates_filtered_statistics_groups(
            self, partitioned_archive):
        archive = partitioned_archive
        # fill_region.parquet has no physical region column, so the region
        # filter can only be answered from the path declaration.
        receipt = verify_file(
            LocalParquetFile(path=archive['fill_region.parquet']),
            _declaration(archive['root']),
            filters=[parse_filter('region==eu+west')])

        assert receipt.region_filled is True
        assert len(receipt.groups) == 3
        assert all(group.filtered_for_report
                   for group in receipt.groups)

    def test_physical_nulls_are_distinct_from_missing_column(
            self, partitioned_archive):
        archive = partitioned_archive
        receipt = verify_file(
            LocalParquetFile(path=archive['nulls.parquet']),
            _declaration(archive['root']))

        assert receipt.region_filled is False
        assert receipt.null_rows == [2]
        assert receipt.status == 'failed'
        assert any(group.status == 'null' for group in receipt.groups)

    def test_conflict_survives_report_date_filter_and_early_groups(
            self, partitioned_archive):
        archive = partitioned_archive
        filters = [parse_filter(DATE_FILTER)]
        receipts, _ = self._receipts(partitioned_archive, filters=filters)
        conflict = _by_name(receipts)['conflict.parquet']

        assert conflict.status == 'failed'
        assert [group.status for group in conflict.groups[:2]] == [
            'verified', 'verified']
        assert len(conflict.conflicts) == 1
        record = conflict.conflicts[0]
        assert record.row_group == 2
        assert record.row_index == 4
        assert record.expected == 'eu+west'
        assert record.actual == 'ap-south-1'
        assert record.filtered_for_report is True

        # Without the report filter the same contradiction is still reported,
        # now annotated as visible to the report.
        unfiltered, _ = self._receipts(partitioned_archive)
        bare_conflict = _by_name(unfiltered)['conflict.parquet']
        assert bare_conflict.conflicts[0].filtered_for_report is False

    def test_insufficient_statistics_force_decoding_late_group(
            self, partitioned_archive, monkeypatch):
        archive = partitioned_archive
        decoded_groups = []
        original = pq.ParquetFile.read_row_group

        def spy(self, index, columns=None, **kwargs):
            decoded_groups.append(index)
            return original(self, index, columns=columns, **kwargs)

        monkeypatch.setattr(pq.ParquetFile, 'read_row_group', spy)
        verify_file(
            LocalParquetFile(path=archive['conflict.parquet']),
            _declaration(archive['root']))

        assert decoded_groups == [2]

    def test_unfinished_file_is_withheld_even_when_rows_match(
            self, partitioned_archive):
        archive = partitioned_archive
        receipt = verify_file(
            LocalParquetFile(path=archive['withheld.parquet']),
            _declaration(archive['root']))

        assert receipt.unfinished is True
        assert receipt.status == 'withheld'
        assert receipt.accepted_rows == 0

    def test_receipt_rendering_locates_rows_and_regions(
            self, partitioned_archive):
        receipts, _ = self._receipts(
            partitioned_archive, filters=[parse_filter(DATE_FILTER)])
        text = render_receipts(receipts)

        assert 'CONFLICT row_group=2 row=4' in text
        assert "expected='eu+west'" in text
        assert "actual='ap-south-1'" in text
        assert 'PHYSICAL NULL column=region rows=2' in text
        assert 'withheld' in text
        assert '[filtered for report]' in text

    def test_receipts_never_short_circuit_after_sampling(
            self, partitioned_archive):
        archive = partitioned_archive
        declaration = _declaration(archive['root'])
        manifest = expand_manifest(declaration)

        result = read_report(manifest, declaration, head=2)
        assert len(result.frame) == 2

        # The same manifest still gets a full, untruncated receipt run.
        receipts = build_receipts(manifest, declaration)
        assert len(receipts) == 6
        assert {receipt.status for receipt in receipts} == {
            'passed', 'failed', 'withheld'}


# ---------------------------------------------------------------------------
# Report projection
# ---------------------------------------------------------------------------
class TestReport:
    def test_report_projects_only_measurements_and_drops_bad_files(
            self, partitioned_archive):
        archive = partitioned_archive
        declaration = _declaration(archive['root'])
        manifest = expand_manifest(declaration)
        result = read_report(manifest, declaration)

        assert list(result.frame.columns) == ['measurement']
        # clean (4) + fill_region (6) + nostats (6); withheld, conflict and
        # nulls contribute nothing, even though conflict's first groups matched.
        assert sorted(result.frame['measurement'].tolist()) == [
            1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0,
            40.0, 41.0, 42.0, 43.0, 44.0, 45.0]

    def test_report_filters_by_date_and_region(self, partitioned_archive):
        archive = partitioned_archive
        declaration = _declaration(archive['root'])
        manifest = expand_manifest(declaration)
        result = read_report(
            manifest, declaration,
            filters=[parse_filter('date==2026-01-01'),
                     parse_filter('region==us+east')])

        assert list(result.frame.columns) == ['measurement']
        assert len(result.frame) == 16
        assert set(result.frame['measurement'].tolist()) == set(
            [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0, 10.0,
             40.0, 41.0, 42.0, 43.0, 44.0, 45.0])

    def test_numeric_filter_and_head(self, partitioned_archive):
        archive = partitioned_archive
        declaration = _declaration(archive['root'])
        manifest = expand_manifest(declaration)
        result = read_report(
            manifest, declaration,
            filters=[parse_filter('measurement>5')], head=3)

        assert list(result.frame.columns) == ['measurement']
        assert len(result.frame) == 3
        assert (result.frame['measurement'] > 5).all()

    def test_unknown_filter_column_raises(self, partitioned_archive):
        archive = partitioned_archive
        declaration = _declaration(archive['root'])
        manifest = expand_manifest(declaration)
        with pytest.raises(PartitionError):
            read_report(manifest, declaration,
                        filters=[parse_filter('nope==1')])

    def test_filter_helper_semantics(self):
        frame = pd.DataFrame({
            'date': ['2026-01-01', '2026-01-02', '2026-01-02'],
            'measurement': [1.0, 8.0, 3.0],
        })
        filtered = apply_filters(frame, [
            parse_filter('date==2026-01-02'),
            parse_filter('measurement>=3'),
        ])
        assert filtered['measurement'].tolist() == [8.0, 3.0]


# ---------------------------------------------------------------------------
# Mocked S3 equivalence
# ---------------------------------------------------------------------------
class TestS3Equivalence:
    def test_s3_manifest_and_decoding(self, partitioned_archive_s3):
        archive = partitioned_archive_s3
        declaration = _declaration(archive['s3_root'])
        manifest = expand_manifest(
            declaration, aws_session=boto3.Session())

        assert len(manifest) == 6
        assert all(isinstance(pf, S3ParquetFile) for pf in manifest)
        receipts = build_receipts(manifest, declaration)
        by_name = _by_name(receipts)

        assert by_name['clean.parquet'].status == 'passed'
        assert by_name['fill_region.parquet'].region_filled is True
        assert by_name['nostats.parquet'].status == 'passed'
        assert by_name['withheld.parquet'].status == 'withheld'
        conflict = by_name['conflict.parquet']
        assert conflict.status == 'failed'
        assert conflict.conflicts[0].filtered_for_report is False
        assert by_name['nulls.parquet'].null_rows == [2]

        west_key = next(pf.key for pf in manifest
                        if pf.key.endswith('conflict.parquet'))
        partitions = parse_partitions(
            west_key, declaration, is_s3=True)
        assert partitions['region'] == 'eu+west'

    def test_s3_report_and_filtered_conflict(self, partitioned_archive_s3):
        archive = partitioned_archive_s3
        declaration = _declaration(archive['s3_root'])
        session = boto3.Session()
        manifest = expand_manifest(declaration, aws_session=session)

        result = read_report(
            manifest, declaration, head=2,
            filters=[parse_filter(DATE_FILTER)])
        # All date==2026-01-02 files failed reconciliation, so no formal rows.
        assert result.frame.empty

        receipts = build_receipts(
            manifest, declaration, filters=[parse_filter(DATE_FILTER)])
        conflict = _by_name(receipts)['conflict.parquet']
        assert conflict.conflicts[0].filtered_for_report is True
        assert conflict.status == 'failed'

    def test_s3_and_local_receipts_are_equivalent(
            self, partitioned_archive_s3):
        archive = partitioned_archive_s3
        local_declaration = _declaration(archive['root'])
        s3_declaration = _declaration(archive['s3_root'])
        session = boto3.Session()

        local = build_receipts(
            expand_manifest(local_declaration), local_declaration)
        s3 = build_receipts(
            expand_manifest(s3_declaration, aws_session=session),
            s3_declaration)

        def summary(receipts):
            return sorted(
                (os.path.basename(r.path), r.status, r.region_filled,
                 tuple(r.null_rows),
                 tuple((c.row_group, c.row_index, c.expected, c.actual)
                       for c in r.conflicts))
                for r in receipts)

        assert summary(local) == summary(s3)


# ---------------------------------------------------------------------------
# CLI smoke tests
# ---------------------------------------------------------------------------
class TestCli:
    def test_report_command(self, partitioned_archive, monkeypatch, capsys):
        from parquet_tools.cli import main
        monkeypatch.setattr(
            'sys.argv',
            ['parquet-tools', 'report', partitioned_archive['root'],
             '--head', '2'])
        main()
        out = capsys.readouterr().out
        assert 'measurement' in out
        assert out.count('\n') >= 3

    def test_receipt_command_exits_nonzero_on_conflict(
            self, partitioned_archive, monkeypatch, capsys):
        from parquet_tools.cli import main
        monkeypatch.setattr(
            'sys.argv',
            ['parquet-tools', 'receipt', partitioned_archive['root'],
             '--filter', DATE_FILTER])
        with pytest.raises(SystemExit) as excinfo:
            main()
        assert excinfo.value.code == 1
        out = capsys.readouterr().out
        assert 'CONFLICT' in out
