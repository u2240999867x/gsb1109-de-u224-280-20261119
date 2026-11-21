import os
import sys
from argparse import ArgumentParser, Namespace
from logging import getLogger
from typing import List, Optional

import pandas as pd
from tabulate import tabulate

from .partitioned import (PartitionDeclaration, PartitionError,
                          build_receipts, expand_manifest, parse_filter,
                          read_report, render_receipts)
from .utils import get_aws_session

logger = getLogger(__name__)

DEFAULT_PARTITION_COLUMNS = 'date,region'


def _declaration(root: str, columns_text: str) -> PartitionDeclaration:
    columns = tuple(
        column.strip() for column in columns_text.split(',') if column.strip())
    return PartitionDeclaration(business_root=root, columns=columns)


def _expand(root: str, declaration: PartitionDeclaration,
            awsprofile: Optional[str], endpoint_url: Optional[str]):
    if root.startswith('s3://'):
        session = get_aws_session(awsprofile)
        return expand_manifest(declaration, aws_session=session,
                               endpoint_url=endpoint_url)
    return expand_manifest(declaration)


def configure_report_parser(parser: ArgumentParser) -> ArgumentParser:
    parser.add_argument('root',
                        metavar='ROOT',
                        type=str,
                        help='''Business root containing partition directories.
                        e.g. ./archive or s3://bucket-name/archive''')
    parser.add_argument('--partition-columns',
                        type=str,
                        default=DEFAULT_PARTITION_COLUMNS,
                        help='Comma-separated partition columns '
                             '(default: date,region)')
    parser.add_argument('--columns', '-c',
                        type=lambda text: [c.strip() for c in text.split(',')
                                           if c.strip()],
                        default=[],
                        help='Columns to project, comma-separated '
                             '(default: measurement)')
    parser.add_argument('--filter',
                        dest='filters',
                        action='append',
                        default=[],
                        help='Row filter, repeatable. e.g. --filter '
                             'date==2026-01-01 --filter region==us+east')
    parser.add_argument('--head', '-n',
                        type=int,
                        default=-1,
                        help='Show only head record (default: infinity)')
    parser.add_argument('--format', '-f',
                        action='store',
                        default='psql',
                        choices=['psql', 'github'],
                        help='Table format (default: psql)')
    parser.add_argument('--awsprofile',
                        type=str,
                        required=False,
                        help='awscli profile in ~/.aws/credentials')
    parser.add_argument('--endpoint-url',
                        type=str,
                        required=False,
                        default=os.environ.get('ENDPOINT_URL'),
                        help='A custom S3 endpoint URL')
    parser.set_defaults(handler=_report_cli)
    return parser


def _report_cli(args: Namespace) -> None:
    try:
        declaration = _declaration(args.root, args.partition_columns)
        manifest = _expand(args.root, declaration, args.awsprofile,
                           args.endpoint_url)
        filters = [parse_filter(expression) for expression in args.filters]
        head = args.head if args.head > 0 else None
        result = read_report(manifest, declaration,
                             columns=args.columns or None,
                             filters=filters, head=head)
        frame: pd.DataFrame = result.frame
        if frame.empty:
            print('No rows passed reconciliation and filters.')
        else:
            print(tabulate(frame, frame.columns, tablefmt=args.format,
                           showindex=False))
    except PartitionError as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)


def configure_receipt_parser(parser: ArgumentParser) -> ArgumentParser:
    parser.add_argument('root',
                        metavar='ROOT',
                        type=str,
                        help='''Business root containing partition directories.
                        e.g. ./archive or s3://bucket-name/archive''')
    parser.add_argument('--partition-columns',
                        type=str,
                        default=DEFAULT_PARTITION_COLUMNS,
                        help='Comma-separated partition columns '
                             '(default: date,region)')
    parser.add_argument('--filter',
                        dest='filters',
                        action='append',
                        default=[],
                        help='Report filter used only to annotate filtered '
                             'rows, repeatable. e.g. --filter date==2026-01-01')
    parser.add_argument('--awsprofile',
                        type=str,
                        required=False,
                        help='awscli profile in ~/.aws/credentials')
    parser.add_argument('--endpoint-url',
                        type=str,
                        required=False,
                        default=os.environ.get('ENDPOINT_URL'),
                        help='A custom S3 endpoint URL')
    parser.set_defaults(handler=_receipt_cli)
    return parser


def _receipt_cli(args: Namespace) -> None:
    try:
        declaration = _declaration(args.root, args.partition_columns)
        manifest: List = _expand(args.root, declaration, args.awsprofile,
                                 args.endpoint_url)
        filters = [parse_filter(expression) for expression in args.filters]
        receipts = build_receipts(manifest, declaration, filters=filters)
        print(render_receipts(receipts), end='')
        if any(not receipt.ok for receipt in receipts):
            sys.exit(1)
    except PartitionError as error:
        print(str(error), file=sys.stderr)
        sys.exit(2)
