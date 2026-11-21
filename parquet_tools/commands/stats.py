import os
import sys
from argparse import ArgumentParser, Namespace

from parquet_tools.parquet.stats import build_report

from .utils import FileNotFoundException, ParquetFile, to_parquet_file


def configure_parser(paser: ArgumentParser) -> ArgumentParser:
    paser.add_argument('file',
                       metavar='FILE',
                       type=str,
                       help='''The parquet file to report on
                       e.g. ./target.parquet or s3://bucket-name/target.parquet
                       ''')
    paser.add_argument('--column',
                       dest='columns',
                       metavar='PATH',
                       action='append',
                       required=False,
                       help='''Full dot-separated field path of a leaf column to report
                       (e.g. --column readings.value). Repeatable.
                       Defaults to all leaf columns.''')
    paser.add_argument('--awsprofile',
                       type=str,
                       required=False,
                       help='awscli profile in ~/.aws/credentials. You use this option when you read parquet file on s3.')
    paser.add_argument('--endpoint-url',
                       type=str,
                       required=False,
                       default=os.environ.get('ENDPOINT_URL'),
                       help='A custom S3 endpoint URL')

    paser.set_defaults(handler=_cli)
    return paser


def _cli(args: Namespace) -> None:
    pf: ParquetFile = to_parquet_file(file_exp=args.file, awsprofile=args.awsprofile, endpoint_url=args.endpoint_url)
    if pf.is_wildcard():
        print('Cannot use wildcard for inspection.', file=sys.stderr)
    else:
        try:
            with pf.get_local_path() as local_path:
                print(build_report(local_path, args.columns))
        except (FileNotFoundException, ValueError) as e:
            print(str(e), file=sys.stderr)
