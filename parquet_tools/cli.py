import argparse
from parquet_tools.commands import show, csv, inspect, manifest


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
    manifest.configure_parser(subparsers.add_parser(
        'manifest',
        help='List an S3 wildcard resumably. see `manifest -h`',
        description='Fetch a resumable, complete S3 wildcard listing.'))

    args = parser.parse_args()
    if hasattr(args, 'handler'):
        args.handler(args)
    else:
        parser.print_help()


if __name__ == '__main__':
    main()
