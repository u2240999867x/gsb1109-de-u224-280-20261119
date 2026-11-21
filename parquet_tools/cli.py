import argparse
from parquet_tools.commands import show, csv, inspect
from parquet_tools.commands.partitioned_cli import (
    configure_receipt_parser, configure_report_parser)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog='parquet-tools',
        description='parquet CLI tools'
    )
    subparsers = parser.add_subparsers()
    show.configure_parser(subparsers.add_parser(
        'show',
        help='Show human readable format. see `show -h`',
        description='Show parquet file content with human readability.'))
    csv.configure_parser(subparsers.add_parser(
        'csv',
        help='Cat csv style. see `csv -h`',
        description='Cat parquet as csv style.'))
    inspect.configure_parser(subparsers.add_parser(
        'inspect',
        help='Inspect parquet file. see `inspect -h`',
        description='Inspect parquet file.'))
    configure_report_parser(subparsers.add_parser(
        'report',
        help='Project measurement rows from a declared-partition archive.',
        description='Read measurement rows from a partitioned archive with '
                    'region reconciliation.'))
    configure_receipt_parser(subparsers.add_parser(
        'receipt',
        help='Emit a region reconciliation receipt for a partitioned archive.',
        description='Reconcile physical region values against the path '
                    'declaration across all row groups.'))

    args = parser.parse_args()
    if hasattr(args, 'handler'):
        args.handler(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
