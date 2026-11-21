from tempfile import TemporaryDirectory

import pyarrow.parquet as pq
import pytest

from parquet_tools.commands.sampling import (Checkpoint, Predicate,
                                             SampleToken, run_sampling)
from parquet_tools.commands.utils import LocalParquetFile
from tests.fixtures.sampling_data import full_read_order


@pytest.fixture
def read_spy(monkeypatch):
    calls = []

    class SpyParquetFile(pq.ParquetFile):
        def __init__(self, source, *args, **kwargs):
            super().__init__(source, *args, **kwargs)
            self._spy_path = str(source)

        def read_row_group(self, group_index, columns=None, **kwargs):
            calls.append({
                'path': self._spy_path,
                'group': group_index,
                'columns': list(columns or []),
            })
            return super().read_row_group(group_index, columns=columns, **kwargs)

    monkeypatch.setattr(
        'parquet_tools.commands.sampling.pq.ParquetFile', SpyParquetFile)
    return calls


def _two_files(workspace):
    return [LocalParquetFile(workspace['file1']),
            LocalParquetFile(workspace['file2'])]


def _matching_order(workspace):
    return [row for row in full_read_order(workspace['file1'],
                                           workspace['file2'])
            if row['status'] == 'OK']


def test_predicate_parsing_and_pruning():
    predicate = Predicate.parse('status == OK')
    assert predicate.column == 'status'
    assert predicate.op == '=='
    assert predicate.literal == 'OK'

    class FakeStats:
        has_min_max = True
        has_null_count = True
        null_count = 0
        min = 'AAA'
        max = 'ZZZ'

    # The interval contains OK: statistics cannot prove absence, do not prune.
    assert predicate.can_prune(FakeStats(), 'OK') is False

    class AllBad(FakeStats):
        min = 'BAD'
        max = 'BAD'

    assert predicate.can_prune(AllBad(), 'OK') is True

    class NoStats:
        has_min_max = False

    assert predicate.can_prune(NoStats(), 'OK') is False


def test_first_twenty_uses_projection_and_filter_column(workspace, read_spy):
    pfs = _two_files(workspace)

    with TemporaryDirectory() as token_dir:
        token_path = f'{token_dir}/credential.json'
        df, token = run_sampling(
            pfs,
            quota=20,
            predicate_spec='status == OK',
            columns=['amount'],
        )
        token.dump(token_path)

        assert len(df) == 20
        assert list(df.columns) == ['amount']
        assert df['amount'].tolist() == [
            row['amount'] for row in _matching_order(workspace)[:20]]

        # The 20th selected row sits inside group 2, which still has matches.
        assert token.refs[-1].file == 0
        assert token.refs[-1].row_group == 2
        assert token.refs[-1].row == 12
        # The credential resumes exactly at the next row of the same group.
        assert token.checkpoints[0] == Checkpoint(row_group=2, row=13)

        # Group 0 is pruned on reliable statistics: never read.
        assert not any(call['path'] == workspace['file1']
                       and call['group'] == 0
                       and call['columns'] == ['status']
                       for call in read_spy)
        # Group 1 has no statistics: real rows are filtered anyway.
        assert any(call['path'] == workspace['file1']
                   and call['group'] == 1
                   and call['columns'] == ['status']
                   for call in read_spy)
        # Quota reached inside file 1: file 2 is never opened.
        assert all(call['path'] != workspace['file2'] for call in read_spy)

        persisted = SampleToken.load(token_path)
        assert persisted.predicate == 'status == OK'
        assert len(persisted.refs) == 20


def test_continue_to_fifty_adds_column_and_keeps_order(workspace, read_spy):
    pfs = _two_files(workspace)

    _, first_token = run_sampling(
        pfs, quota=20, predicate_spec='status == OK', columns=['amount'])
    first_amounts = [ref.seq for ref in first_token.refs]

    read_spy.clear()
    df, token = run_sampling(
        pfs,
        quota=50,
        predicate_spec='status == OK',
        columns=['amount', 'source'],
        token=first_token,
    )

    assert len(df) == 50
    assert list(df.columns) == ['amount', 'source']
    matching = _matching_order(workspace)
    assert df['amount'].tolist() == [
        row['amount'] for row in matching[:50]]

    # New samples continue strictly after the original twentieth.
    assert [ref.seq for ref in token.refs[:20]] == first_amounts
    assert token.refs[20].row_group == 2
    assert token.refs[20].row == 13

    # File 1's pruned/uncertain groups are not re-evaluated; only group 2 resumes.
    file1_status_calls = [call for call in read_spy
                          if call['path'] == workspace['file1']
                          and call['columns'] == ['status']]
    assert {call['group'] for call in file1_status_calls} == {2}

    # The new display column is fetched only for row groups of selected rows.
    source_calls = [call for call in read_spy if 'source' in call['columns']]
    assert all(call['group'] in (1, 2) for call in source_calls)
    assert not any(call['group'] == 0 for call in source_calls)

    # Original twenty carry their correct source description in the new view.
    first_twenty = df.head(20)
    assert set(first_twenty['source'].dropna().unique()) <= {'g1', 'g2'}
    g1_rows = first_twenty[first_twenty['amount'].isin(
        [row['amount'] for row in matching if row['row_group'] == 1][:7])]
    assert (g1_rows['source'] == 'g1').all()

    # The second file has no source column: it is rendered empty, not failed.
    file2_rows = df.tail(14)
    assert file2_rows['source'].isna().all()
    assert file2_rows['amount'].tolist() == [
        row['amount'] for row in matching[36:50]]


def test_replay_same_request_does_not_duplicate(workspace, read_spy):
    pfs = _two_files(workspace)

    _, token = run_sampling(
        pfs, quota=20, predicate_spec='status == OK', columns=['amount'])
    read_spy.clear()

    replay_df, replay_token = run_sampling(
        pfs,
        quota=20,
        predicate_spec='status == OK',
        columns=['amount'],
        token=token,
    )

    assert len(replay_df) == 20
    assert len(replay_token.refs) == 20
    assert len({(ref.file, ref.row_group, ref.row)
                for ref in replay_token.refs}) == 20
    # Replay performs no predicate scanning at all.
    assert not any(call['columns'] == ['status'] for call in read_spy)


def test_reconcile_columns_match_full_read(workspace):
    pfs = _two_files(workspace)

    df, _ = run_sampling(
        pfs,
        quota=50,
        predicate_spec='status == OK',
        columns=['amount'],
        reconcile=True,
    )

    expected = _matching_order(workspace)[:50]
    assert len(df) == len(expected)
    for actual, row in zip(df.to_dict('records'), expected):
        assert actual['_source_file'].endswith(
            'first.parquet' if row['file'] == 0 else 'second.parquet')
        assert actual['_row_group'] == row['row_group']
        assert actual['_row_in_group'] == row['row_in_group']
        assert actual['amount'] == row['amount']


def test_token_rejected_for_changed_file_order(workspace):
    pfs = _two_files(workspace)
    _, token = run_sampling(
        pfs, quota=20, predicate_spec='status == OK', columns=['amount'])

    swapped = [LocalParquetFile(workspace['file2']),
               LocalParquetFile(workspace['file1'])]
    with pytest.raises(Exception) as excinfo:
        run_sampling(swapped, quota=50,
                     predicate_spec='status == OK',
                     columns=['amount'], token=token)
    assert 'credential' in str(excinfo.value)


def test_token_rejected_for_changed_predicate(workspace):
    pfs = _two_files(workspace)
    _, token = run_sampling(
        pfs, quota=20, predicate_spec='status == OK', columns=['amount'])

    with pytest.raises(Exception) as excinfo:
        run_sampling(pfs, quota=50,
                     predicate_spec='status == BAD',
                     columns=['amount'], token=token)
    assert 'credential' in str(excinfo.value)


def test_token_json_roundtrip(workspace):
    pfs = [LocalParquetFile(workspace['file1'])]
    _, token = run_sampling(
        pfs, quota=5, predicate_spec='status == OK', columns=['amount'])
    restored = SampleToken.from_json(token.to_json())
    assert restored.to_json() == token.to_json()
    assert restored.refs == token.refs
    assert restored.checkpoints == token.checkpoints


def test_first_run_requires_predicate(workspace):
    pfs = [LocalParquetFile(workspace['file1'])]
    with pytest.raises(Exception):
        run_sampling(pfs, quota=20, predicate_spec=None, columns=['amount'])


def test_quota_larger_than_matches_returns_all(workspace):
    pfs = _two_files(workspace)
    df, token = run_sampling(
        pfs, quota=1000, predicate_spec='status == OK', columns=['amount'])
    total_ok = len(_matching_order(workspace))
    assert len(df) == total_ok
    assert len(token.refs) == total_ok
    assert all(token.files_done)


def test_quota_stops_before_next_row_group_and_file(workspace, read_spy):
    pfs = _two_files(workspace)

    df, token = run_sampling(
        pfs, quota=5, predicate_spec='status == OK', columns=['amount'])

    # All five come from the statistics-less group 1.
    assert len(df) == 5
    assert all(ref.file == 0 and ref.row_group == 1 for ref in token.refs)
    # The quota is reached mid group 1; later groups and file 2 stay unread.
    status_calls = [call for call in read_spy
                    if call['columns'] == ['status']]
    assert {call['group'] for call in status_calls
            if call['path'] == workspace['file1']} == {1}
    assert all(call['path'] != workspace['file2'] for call in read_spy)
    assert token.checkpoints[0] == Checkpoint(row_group=1, row=8)
    assert token.files_done == [False, False]


def test_zero_matches_returns_empty_frame_with_columns(workspace):
    pfs = _two_files(workspace)
    df, token = run_sampling(
        pfs, quota=20, predicate_spec='status == MISSING', columns=['amount'])
    assert list(df.columns) == ['amount']
    assert len(df) == 0
    assert token.refs == []
    assert all(token.files_done)


def test_numeric_predicate_coerces_literal(workspace):
    pfs = _two_files(workspace)
    df, _ = run_sampling(
        pfs, quota=10, predicate_spec='amount > 50', columns=['amount'])
    expected = [row['amount'] for row in full_read_order(
        workspace['file1'], workspace['file2']) if row['amount'] > 50][:10]
    assert df['amount'].tolist() == expected


def test_unknown_predicate_column_is_rejected(workspace):
    pfs = _two_files(workspace)
    with pytest.raises(Exception):
        run_sampling(
            pfs, quota=5, predicate_spec='nope == x', columns=['amount'])


def test_invalid_predicate_syntax_is_rejected(workspace):
    pfs = _two_files(workspace)
    with pytest.raises(Exception):
        run_sampling(
            pfs, quota=5, predicate_spec='status', columns=['amount'])
