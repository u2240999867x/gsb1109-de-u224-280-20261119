import os
import sys
from argparse import ArgumentParser, Namespace
from logging import getLogger
from typing import List

import pandas as pd
from tabulate import tabulate

from .sampling import (SampleToken, add_sampling_arguments,
                       run_sampling)
from .utils import (FileNotFoundException, InvalidCommandExcpetion,
                    ParquetFile, get_datafame_from_objs,
                    to_parquet_file)

logger = getLogger(__name__)


def configure_parser(paser: ArgumentParser) -> ArgumentParser:
    paser.add_argument('file',
                       metavar='FILE',
                       type=str,
                       nargs='+',
                       help='''
                       The parquet file to print to stdout.
                       e.g. ./target.parquet or s3://bucket-name/target.parquet or s3://bucket-name/*
                       ''')
    paser.add_argument('--format', '-f',
                       action='store',
                       required=False,
                       default='psql',
                       choices=[
                           'psql', 'github'
                       ],
                       help='''Table format(default: psql).
                             ''')
    paser.add_argument('--columns', '-c',
                       type=lambda s: s.split(","),
                       default=[],
                       help='''Show only the given column, can be specified more than once.
                             e.g. --columns email,name
                             ''')
    paser.add_argument('--head', '-n',
                       type=int,
                       required=False,
                       default=-1,
                       help='Show only head record(default:infinity)')
    paser.add_argument('--awsprofile',
                       type=str,
                       required=False,
                       help='awscli profile in ~/.aws/credentials. You use this option when you read parquet file on s3.')
    paser.add_argument('--endpoint-url',
                       type=str,
                       required=False,
                       default=os.environ.get('ENDPOINT_URL'),
                       help='A custom S3 endpoint URL')
    add_sampling_arguments(paser)

    paser.set_defaults(handler=_cli)
    return paser


def _cli(args: Namespace) -> None:
    try:
        pfs: List[ParquetFile] = [
            to_parquet_file(file_exp=f, awsprofile=args.awsprofile, endpoint_url=args.endpoint_url)
            for f in args.file]
        if args.sample is not None:
            token = SampleToken.load(args.sample_token) if args.sample_token else None
            df, new_token = run_sampling(
                pfs,
                quota=args.sample,
                predicate_spec=args.where,
                columns=args.columns,
                token=token,
                reconcile=args.reconcile,
            )
            # A continuation updates the credential in place so the next
            # request resumes from the advanced checkpoint instead of re-scanning.
            persist_path = args.emit_token or args.sample_token
            if persist_path:
                new_token.dump(persist_path)
            _print_sample(df, table_format=args.format)
            return
        with get_datafame_from_objs(pfs, args.head) as df:
            if df is None:
                raise FileNotFoundException('File matching that expression not found.')
            _execute(
                df=df,
                format=args.format,
                head=args.head,
                columns=args.columns
            )
    except InvalidCommandExcpetion as e:
        print(str(e), file=sys.stderr)
    except FileNotFoundException as e:
        print(str(e), file=sys.stderr)


def _print_sample(df: pd.DataFrame, table_format: str) -> None:
    print(tabulate(df, df.columns, tablefmt=table_format, showindex=False))


def _execute(df: pd.DataFrame, format: str, head: int, columns: list) -> None:
    # head
    df_head: pd.DataFrame = df.head(head) if head > 0 else df
    # select columns
    df_select: pd.DataFrame = df_head[columns] if len(columns) else df_head
    print(tabulate(df_select, df_select.columns, tablefmt=format, showindex=False))
