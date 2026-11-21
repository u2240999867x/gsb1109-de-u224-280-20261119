import os
import sys
from argparse import ArgumentParser, Namespace

from parquet_tools.parquet.credential import (
    CredentialError,
    InspectionCredential,
)
from parquet_tools.parquet.remote import S3Store
from parquet_tools.parquet.report import (
    obj_to_string,
    render_detail_from_file,
    render_simple_from_file,
)
from parquet_tools.parquet.tail import InspectionError, inspect_tail

from .utils import (
    FileNotFoundException,
    ParquetFile,
    S3ParquetFile,
    to_parquet_file,
)


def configure_parser(paser: ArgumentParser) -> ArgumentParser:
    paser.add_argument('file',
                       metavar='FILE',
                       type=str,
                       help='''The parquet file to inspect
                       e.g. ./target.parquet or s3://bucket-name/target.parquet
                       ''')
    paser.add_argument('--awsprofile',
                       type=str,
                       required=False,
                       help='awscli profile in ~/.aws/credentials. You use this option when you read parquet file on s3.')
    paser.add_argument('--detail',
                       action='store_true',
                       required=False,
                       default=False,
                       help='Detail expression using thrift.')
    paser.add_argument('--endpoint-url',
                       type=str,
                       required=False,
                       default=os.environ.get('ENDPOINT_URL'),
                       help='A custom S3 endpoint URL')
    paser.add_argument('--credential',
                       type=str,
                       required=False,
                       default=None,
                       help='Path to a saved inspection credential to resume from')
    paser.add_argument('--save-credential',
                       type=str,
                       required=False,
                       default=None,
                       help='Path to write the inspection credential after this run')
    paser.add_argument('--full-download',
                       action='store_true',
                       required=False,
                       default=False,
                       help='Explicitly download the whole S3 object instead of reading only its footer')

    paser.set_defaults(handler=_cli)
    return paser


def _cli(args: Namespace) -> None:
    pf: ParquetFile = to_parquet_file(file_exp=args.file, awsprofile=args.awsprofile, endpoint_url=args.endpoint_url)
    if pf.is_wildcard():
        print('Cannot use wildcard for inspection.', file=sys.stderr)
        return
    if isinstance(pf, S3ParquetFile) and not args.full_download:
        _cli_s3_tail(pf, args)
        return
    try:
        if isinstance(pf, S3ParquetFile):
            with pf.get_local_path_with_version() as (local_path, version):
                _write_report(local_path, args)
                if args.save_credential:
                    _save_download_credential(pf, version, args.save_credential, args.detail)
        else:
            with pf.get_local_path() as local_path:
                _write_report(local_path, args)
    except FileNotFoundException as e:
        print(str(e), file=sys.stderr)


def _write_report(local_path: str, args: Namespace) -> None:
    if args.detail:
        _execute_detail(filename=local_path)
    else:
        _execute_simple(filename=local_path)


def _save_download_credential(pf: S3ParquetFile, version: dict, path: str, detail: bool) -> None:
    """Record the identity of an explicit full download for later tail reads.

    Only object identity and the checked key/endpoint are recorded; the
    downloaded bytes themselves are not persisted.
    """
    from parquet_tools.parquet.credential import EndpointRef, ObjectIdentity

    identity = ObjectIdentity(
        size=int(version['size']),
        etag=version.get('etag'),
        version_id=version.get('version_id'),
    )
    credential = InspectionCredential(
        endpoint=EndpointRef.for_client(pf.endpoint_url, None),
        bucket=pf.bucket,
        key=pf.key,
        identity=identity,
    )
    credential.save(path)


def _cli_s3_tail(pf: S3ParquetFile, args: Namespace) -> None:
    credential = None
    if args.credential:
        try:
            credential = InspectionCredential.load(args.credential)
        except (OSError, ValueError, CredentialError) as exc:
            print(f'Cannot load credential {args.credential}: {exc}', file=sys.stderr)
            return
    try:
        store = S3Store(
            session=pf.aws_session,
            endpoint_url=pf.endpoint_url,
        )
    except Exception as exc:
        print(f's3://{pf.bucket}/{pf.key} not found or cannot access: {exc}', file=sys.stderr)
        return
    try:
        result = inspect_tail(
            store,
            pf.bucket,
            pf.key,
            detail=args.detail,
            credential=credential,
        )
    except FileNotFoundException as e:
        print(str(e), file=sys.stderr)
        return
    except InspectionError as e:
        print(str(e), file=sys.stderr)
        return
    print(result.report)
    if args.save_credential:
        try:
            result.credential.save(args.save_credential)
        except OSError as exc:
            print(f'Cannot save credential {args.save_credential}: {exc}', file=sys.stderr)


def _execute_simple(filename) -> None:
    print(render_simple_from_file(filename))


def _execute_detail(filename) -> None:
    print(render_detail_from_file(filename))


def _obj_to_string(obj, toatty: bool, level: int = 1) -> str:
    return obj_to_string(obj, toatty, level)
