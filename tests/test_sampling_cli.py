import csv
import io
from argparse import ArgumentParser

from parquet_tools.cli import main
from parquet_tools.commands.csv import configure_parser as configure_csv
from parquet_tools.commands.sampling import SampleToken
from parquet_tools.commands.show import configure_parser as configure_show
from tests.fixtures.sampling_data import full_read_order


def _run_cli(monkeypatch, argv):
    monkeypatch.setattr('sys.argv', ['parquet-tools'] + argv)
    main()


def _psql_data_lines(output):
    '''Data rows of a psql table, excluding header and border lines.'''
    return [
        line for line in output.splitlines()
        if line.startswith('|')
        and line.strip('|+- ').strip() != ''
        and 'amount' not in line
    ]


def test_show_parser_exposes_sampling_arguments():
    args = configure_show(ArgumentParser()).parse_args([
        '--sample', '20', '--where', 'status == OK',
        '--sample-token', 'in.json', '--emit-token', 'out.json',
        '--reconcile', 'f.parquet'])
    assert args.sample == 20
    assert args.where == 'status == OK'
    assert args.sample_token == 'in.json'
    assert args.emit_token == 'out.json'
    assert args.reconcile is True


def test_csv_parser_exposes_sampling_arguments():
    args = configure_csv(ArgumentParser()).parse_args([
        '--sample', '50', '--where', 'status == OK',
        '--columns', 'amount,source', 'f.parquet'])
    assert args.sample == 50
    assert args.columns == ['amount', 'source']


def test_show_and_csv_share_sampling_order(workspace, monkeypatch, capsys, tmp_path):
    token_path = str(tmp_path / 'credential.json')

    _run_cli(monkeypatch, [
        'show', workspace['file1'], workspace['file2'],
        '--sample', '20', '--where', 'status == OK',
        '--columns', 'amount', '--emit-token', token_path,
    ])
    show_out = capsys.readouterr().out
    show_amounts = [line.strip('|').split()[-1]
                    for line in _psql_data_lines(show_out)]
    assert len(show_amounts) == 20

    # csv continues the same credential with a different display projection.
    _run_cli(monkeypatch, [
        'csv', workspace['file1'], workspace['file2'],
        '--sample', '50', '--where', 'status == OK',
        '--columns', 'amount,source', '--sample-token', token_path,
    ])
    csv_out = capsys.readouterr().out
    rows = list(csv.DictReader(io.StringIO(csv_out)))
    assert len(rows) == 50
    assert list(rows[0].keys()) == ['amount', 'source']

    matching = [row for row in full_read_order(workspace['file1'],
                                                workspace['file2'])
                if row['status'] == 'OK']
    assert [float(row['amount']) for row in rows] == [
        row['amount'] for row in matching[:50]]

    # The first twenty shown earlier are the same rows, in the same order.
    assert [float(row['amount']) for row in rows[:20]] == [
        float(value) for value in show_amounts]

    # The original twenty have their own source descriptions.
    g1_amounts = {row['amount'] for row in matching if row['row_group'] == 1}
    for row in rows[:20]:
        if float(row['amount']) in g1_amounts:
            assert row['source'] == 'g1'
        else:
            assert row['source'] == 'g2'

    # File 2 lacks the source column: the cell is declared empty.
    assert rows[-1]['source'] == ''
    assert all(row['source'] == '' for row in rows[-14:])


def test_replay_with_same_quota_does_not_duplicate(workspace, monkeypatch,
                                                    capsys, tmp_path):
    token_path = str(tmp_path / 'credential.json')
    argv = [
        'show', workspace['file1'], workspace['file2'],
        '--sample', '20', '--where', 'status == OK',
        '--columns', 'amount',
    ]

    _run_cli(monkeypatch, argv + ['--emit-token', token_path])
    capsys.readouterr()
    _run_cli(monkeypatch, argv + ['--sample-token', token_path])
    replay = capsys.readouterr().out

    # Exactly twenty data rows: replay must not duplicate any selection.
    assert len(_psql_data_lines(replay)) == 20


def test_continuation_persists_advanced_credential(workspace, monkeypatch,
                                                   capsys, tmp_path):
    token_path = str(tmp_path / 'credential.json')
    base = [
        'csv', workspace['file1'], workspace['file2'],
        '--where', 'status == OK', '--columns', 'amount',
    ]

    _run_cli(monkeypatch, base + [
        '--sample', '20', '--emit-token', token_path])
    capsys.readouterr()
    assert len(SampleToken.load(token_path).refs) == 20

    # No --emit-token here: the continuation must still rewrite the credential.
    _run_cli(monkeypatch, base + [
        '--sample', '50', '--sample-token', token_path])
    capsys.readouterr()
    assert len(SampleToken.load(token_path).refs) == 50

    # A further continuation builds on the advanced credential, not the first.
    _run_cli(monkeypatch, base + [
        '--sample', '54', '--sample-token', token_path])
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert len(rows) == 54
    assert len(SampleToken.load(token_path).refs) == 54


def test_reconcile_output_locates_every_row(workspace, monkeypatch, capsys):
    _run_cli(monkeypatch, [
        'csv', workspace['file1'], workspace['file2'],
        '--sample', '50', '--where', 'status == OK',
        '--columns', 'amount', '--reconcile',
    ])
    rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    matching = [row for row in full_read_order(workspace['file1'],
                                                workspace['file2'])
                if row['status'] == 'OK'][:50]
    for actual, expected in zip(rows, matching):
        assert actual['_source_file'].endswith(
            'first.parquet' if expected['file'] == 0 else 'second.parquet')
        assert int(actual['_row_group']) == expected['row_group']
        assert int(actual['_row_in_group']) == expected['row_in_group']


def test_legacy_usage_without_sample_unchanged(workspace, monkeypatch, capsys):
    _run_cli(monkeypatch, [
        'show', workspace['file1'], '--head', '3',
        '--columns', 'amount,status',
    ])
    out = capsys.readouterr().out
    assert 'amount' in out and 'status' in out
    # Three data rows; header and border lines are excluded.
    assert len(_psql_data_lines(out)) == 3

    _run_cli(monkeypatch, ['csv', workspace['file2'], '--head', '2'])
    csv_rows = list(csv.DictReader(io.StringIO(capsys.readouterr().out)))
    assert len(csv_rows) == 2
    assert set(csv_rows[0].keys()) == {'amount', 'status'}


def test_first_run_without_where_is_rejected(workspace, monkeypatch, capsys):
    _run_cli(monkeypatch, [
        'show', workspace['file1'], '--sample', '20',
        '--columns', 'amount',
    ])
    assert 'where' in capsys.readouterr().err


def test_token_rejected_when_file_order_changes(workspace, monkeypatch,
                                                capsys, tmp_path):
    token_path = str(tmp_path / 'credential.json')
    _run_cli(monkeypatch, [
        'show', workspace['file1'], workspace['file2'],
        '--sample', '20', '--where', 'status == OK',
        '--columns', 'amount', '--emit-token', token_path,
    ])
    capsys.readouterr()

    _run_cli(monkeypatch, [
        'show', workspace['file2'], workspace['file1'],
        '--sample', '50', '--where', 'status == OK',
        '--columns', 'amount', '--sample-token', token_path,
    ])
    err = capsys.readouterr().err
    assert 'credential' in err


def test_token_rejected_when_predicate_changes(workspace, monkeypatch,
                                               capsys, tmp_path):
    token_path = str(tmp_path / 'credential.json')
    _run_cli(monkeypatch, [
        'csv', workspace['file1'], workspace['file2'],
        '--sample', '20', '--where', 'status == OK',
        '--columns', 'amount', '--emit-token', token_path,
    ])
    capsys.readouterr()

    _run_cli(monkeypatch, [
        'csv', workspace['file1'], workspace['file2'],
        '--sample', '50', '--where', 'status == BAD',
        '--columns', 'amount', '--sample-token', token_path,
    ])
    assert 'credential' in capsys.readouterr().err
