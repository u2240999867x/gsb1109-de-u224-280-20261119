import argparse
import sys
from parquet_tools.commands import show, csv, inspect, diff


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
    diff.configure_parser(subparsers.add_parser(
        'diff',
        help='Compare two Parquet footer schemas. see `diff -h`',
        description='Read-only structural diff of two Parquet footer schemas.'))

    args = parser.parse_args()
    if hasattr(args, 'handler'):
        exit_code = args.handler(args)
        if isinstance(exit_code, int):
            sys.exit(exit_code)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
