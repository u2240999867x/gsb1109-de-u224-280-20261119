"""Command-line interface for the incremental batch index.

The ``wb-batch`` command (also available as ``wb-manager batch-*``) drives
:class:`pywb.indexer.batchindexer.BatchIndex`: it adds response and request
volumes in separate submissions, shows what is still waiting for its request
or under conflict review, confirms or withdraws conflicts, and prints the
current CDX index path for a pywb collection.

Exit codes are 0 on success and 1 on a batch index error, so a delivery job
can retry the same submission id and get the same single update.
"""

import json
import os
import sys

from argparse import ArgumentParser, RawTextHelpFormatter

from pywb.indexer.batchindexer import (ArchiveEvidence, BatchIndex,
                                       BatchIndexError)


def _split_volume(value):
    if '=' in value and '://' not in value.split('=', 1)[0]:
        volume, path = value.split('=', 1)
        return path, volume
    return value, None


def _add_volume(parser):
    parser.add_argument('--volume', '-v', action='append', default=[],
                        metavar='[VOLUME=]PATH',
                        help='archive volume to add, optionally naming it')
    parser.add_argument('--evidence', action='append', default=[],
                        metavar='PATH',
                        help='saved evidence JSON to add instead of a volume')


def _index_from_args(args):
    return BatchIndex(args.root)


def cmd_add(args):
    index = _index_from_args(args)
    volumes = [_split_volume(v) for v in args.volume]
    evidence = []
    for path in args.evidence:
        with open(path, 'r', encoding='utf-8') as fh:
            loaded = json.load(fh)
        if isinstance(loaded, list):
            evidence.extend(loaded)
        else:
            evidence.append(loaded)

    if not volumes and not evidence:
        raise BatchIndexError('add requires --volume or --evidence')

    view = index.submit(args.submission, volumes=volumes, evidence=evidence)
    print(json.dumps(_summary(view), ensure_ascii=False, indent=2))
    return 0


def cmd_export(args):
    """Print the pairing evidence of volumes without publishing a view."""
    volumes = [_split_volume(v) for v in args.volume]
    if not volumes:
        raise BatchIndexError('export requires --volume')
    out = []
    for path, volume in volumes:
        evidence = ArchiveEvidence.from_volume(
            path, volume=volume or os.path.basename(path))
        out.append(evidence.to_dict())
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


def cmd_status(args):
    index = _index_from_args(args)
    print(json.dumps(index.status(), ensure_ascii=False, indent=2))
    return 0


def cmd_conflicts(args):
    index = _index_from_args(args)
    view = index.view()
    if not view:
        print(json.dumps({}, ensure_ascii=False, indent=2))
        return 0
    print(json.dumps(view.conflicts(), ensure_ascii=False, indent=2))
    return 0


def cmd_confirm(args):
    index = _index_from_args(args)
    view = index.confirm_conflicts(args.submission, args.conflict or None)
    print(json.dumps(_summary(view), ensure_ascii=False, indent=2))
    return 0


def cmd_withdraw(args):
    index = _index_from_args(args)
    view = index.withdraw_volume(args.submission, args.volume_name)
    print(json.dumps(_summary(view), ensure_ascii=False, indent=2))
    return 0


def cmd_restore(args):
    index = _index_from_args(args)
    view = index.restore_volume(args.submission, args.volume_name)
    print(json.dumps(_summary(view), ensure_ascii=False, indent=2))
    return 0


def cmd_index(args):
    index = _index_from_args(args)
    view = index.view()
    if not view:
        raise BatchIndexError('no published view')
    if args.output:
        with open(view.index_path, 'rb') as src, open(args.output, 'wb') as dst:
            dst.write(src.read())
        print(args.output)
    else:
        print(view.index_path)
    return 0


def cmd_show(args):
    index = _index_from_args(args)
    view = index.view()
    if not view:
        return 0
    sys.stdout.write(''.join(line + '\n' for line in view.index_lines()))
    return 0


def _summary(view):
    summary = view.summary()
    summary['index'] = view.index_path
    summary['waiting'] = view.waiting()
    summary['conflict_ids'] = sorted(view.conflicts())
    return summary


def build_parser():
    parser = ArgumentParser(
        prog='wb-batch',
        description='Incrementally index WARC volumes whose request and '
                    'response records arrive in separate deliveries.',
        formatter_class=RawTextHelpFormatter)
    parser.add_argument('--root', default='./batch-index',
                        help='batch index directory (default: ./batch-index)')

    sub = parser.add_subparsers(dest='command')
    sub.required = True

    add = sub.add_parser('add', help='add one batch submission of volumes')
    _add_volume(add)
    add.add_argument('--submission', '-s', required=True,
                     help='stable submission id; retry with the same id')
    add.set_defaults(func=cmd_add)

    export = sub.add_parser('export',
                            help='print the pairing evidence of volumes')
    _add_volume(export)
    export.set_defaults(func=cmd_export)

    status = sub.add_parser('status', help='show the current view summary')
    status.set_defaults(func=cmd_status)

    conflicts = sub.add_parser('conflicts', help='list conflicts of the current view')
    conflicts.set_defaults(func=cmd_conflicts)

    confirm = sub.add_parser('confirm', help='confirm conflicts (withdraw affected keys)')
    confirm.add_argument('--submission', '-s', required=True)
    confirm.add_argument('--conflict', '-c', action='append', default=[])
    confirm.set_defaults(func=cmd_confirm)

    withdraw = sub.add_parser('withdraw', help='withdraw a conflicting volume')
    withdraw.add_argument('--submission', '-s', required=True)
    withdraw.add_argument('volume_name')
    withdraw.set_defaults(func=cmd_withdraw)

    restore = sub.add_parser('restore', help='restore a withdrawn volume')
    restore.add_argument('--submission', '-s', required=True)
    restore.add_argument('volume_name')
    restore.set_defaults(func=cmd_restore)

    index = sub.add_parser('index', help='print or copy the current CDX index')
    index.add_argument('--output', '-o')
    index.set_defaults(func=cmd_index)

    show = sub.add_parser('show', help='print the current CDX index')
    show.set_defaults(func=cmd_show)

    return parser


def main(args=None):
    parser = build_parser()
    cmd = parser.parse_args(args=args)
    try:
        return cmd.func(cmd)
    except (BatchIndexError, OSError) as e:
        sys.stderr.write('wb-batch: {0}\n'.format(e))
        return 1


if __name__ == '__main__':
    sys.exit(main())
