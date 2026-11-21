"""CLI for resumable S3 wildcard listings."""
from __future__ import annotations

import json
import os
import sys
from argparse import ArgumentParser, Namespace
from parquet_tools.s3_manifest import (
    S3Manifest,
    S3ManifestStatus,
    save_manifest_checkpoint,
)

from .utils import InvalidCommandExcpetion, S3ParquetFile, to_parquet_file


EXIT_COMPLETE = 0
EXIT_PARTIAL = 2
EXIT_CONFLICT = 3
EXIT_STOPPED = 4

_EXIT_BY_STATUS = {
    S3ManifestStatus.COMPLETE: EXIT_COMPLETE,
    S3ManifestStatus.PARTIAL: EXIT_PARTIAL,
    S3ManifestStatus.CONFLICT: EXIT_CONFLICT,
    S3ManifestStatus.STOPPED: EXIT_STOPPED,
}


def configure_parser(parser: ArgumentParser) -> ArgumentParser:
    parser.add_argument(
        'file',
        metavar='FILE',
        type=str,
        help='S3 wildcard expression, e.g. s3://bucket-name/prefix/*',
    )
    parser.add_argument(
        '--checkpoint',
        type=str,
        required=False,
        help='Read a previously saved manifest checkpoint and continue it.',
    )
    parser.add_argument(
        '--save-checkpoint',
        type=str,
        required=False,
        help='Write the resulting manifest checkpoint as JSON before exiting.',
    )
    parser.add_argument(
        '--page-size',
        type=int,
        required=False,
        default=None,
        help='MaxKeys for each ListObjectsV2 request.',
    )
    parser.add_argument(
        '--once',
        action='store_true',
        default=False,
        help='Fetch only the next page, then save and exit.',
    )
    parser.add_argument(
        '--keys-only',
        action='store_true',
        default=False,
        help='Print only the confirmed keys, one per line.',
    )
    parser.add_argument(
        '--pretty',
        action='store_true',
        default=False,
        help='Pretty-print the manifest JSON.',
    )
    parser.add_argument(
        '--format',
        '-f',
        choices=['json', 'text', 'keys'],
        default='json',
        help='Output format (default: json).',
    )
    parser.add_argument(
        '--awsprofile',
        type=str,
        required=False,
        help='awscli profile in ~/.aws/credentials.',
    )
    parser.add_argument(
        '--endpoint-url',
        type=str,
        required=False,
        default=os.environ.get('ENDPOINT_URL'),
        help='A custom S3 endpoint URL',
    )
    parser.set_defaults(handler=_cli)
    return parser


def _cli(args: Namespace) -> None:
    try:
        pf = to_parquet_file(
            file_exp=args.file,
            awsprofile=args.awsprofile,
            endpoint_url=args.endpoint_url,
        )
        if not isinstance(pf, S3ParquetFile):
            print('manifest requires an s3:// wildcard expression.', file=sys.stderr)
            sys.exit(EXIT_STOPPED)
        if not pf.is_wildcard():
            print(
                'manifest requires a wildcard at the end of the S3 key.',
                file=sys.stderr,
            )
            sys.exit(EXIT_STOPPED)

        checkpoint = None
        if args.checkpoint:
            checkpoint = S3Manifest.load(args.checkpoint)

        manifest = pf.resolve_wildcard_manifest(
            checkpoint=checkpoint,
            page_size=args.page_size,
            fetch_all=not args.once,
        )
    except InvalidCommandExcpetion as exc:
        print(str(exc), file=sys.stderr)
        sys.exit(EXIT_STOPPED)
    except Exception as exc:  # noqa: BLE001 - CLI reports, scheduler exits
        print(f'{type(exc).__name__}: {exc}', file=sys.stderr)
        sys.exit(EXIT_STOPPED)

    if args.save_checkpoint:
        save_manifest_checkpoint(manifest, args.save_checkpoint)

    output_format = 'keys' if args.keys_only else args.format
    if output_format == 'keys':
        for key in manifest.confirmed_keys:
            print(key)
    elif output_format == 'text':
        print(manifest.format_text())
    else:
        print(
            json.dumps(
                manifest.to_dict(),
                ensure_ascii=False,
                indent=2 if args.pretty else None,
                sort_keys=True,
            )
        )

    if manifest.error:
        print(manifest.error, file=sys.stderr)
    if manifest.conflicts:
        for conflict in manifest.conflicts:
            print(
                f'{conflict.key}: {conflict.message}',
                file=sys.stderr,
            )
    if manifest.stop_reason:
        print(f'stopped: {manifest.stop_reason}', file=sys.stderr)

    sys.exit(_EXIT_BY_STATUS.get(manifest.status, EXIT_STOPPED))
