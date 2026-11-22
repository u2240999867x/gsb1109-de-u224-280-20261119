"""Command-line driver for a shared continuation batch.

Every command opens the shared directory, performs one durable step and
exits.  That makes each invocation equivalent to a separate machine/process:
no claim, lease or capture state lives in memory between commands.

Exit codes
----------
0   success
2   claim refused (another live owner)
3   index message fenced / already indexed
4   capture evidence could not be verified
5   capture interrupted on purpose (controlled failure)
6   unknown page or malformed manifest
"""

import argparse
import json
import sys

from pywb.recorder.continuation import (
    ClaimError, ContinuationManifest, IndexFenced, ManifestError,
    VerificationError, read_capture_payload, read_cdx_entries,
)
from pywb.recorder.continuationwriter import (
    ContinuationWriter, InterruptedCapture, VolumeConflict,
)

EXIT_OK = 0
EXIT_CLAIM = 2
EXIT_FENCED = 3
EXIT_VERIFY = 4
EXIT_INTERRUPTED = 5
EXIT_MANIFEST = 6


def _json(obj):
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, sort_keys=True) + '\n')


def build_parser():
    parser = argparse.ArgumentParser(prog='pywb-continuation')
    parser.add_argument('root', help='shared continuation directory')
    parser.add_argument('--lease', type=float, default=ContinuationManifest.DEFAULT_LEASE,
                        help='claim lease in seconds')

    sub = parser.add_subparsers(dest='command', required=True)

    p = sub.add_parser('init', help='create a manifest for a list of URLs')
    p.add_argument('--url', action='append', required=True)
    p.add_argument('--unauthorized', action='append', default=[])

    p = sub.add_parser('bootstrap', help='seed completed pages from a CDX(J) index')
    p.add_argument('--index', required=True)
    p.add_argument('--volume-root', default=None)

    p = sub.add_parser('plan', help='print the per-page continuation plan')

    p = sub.add_parser('capture', help='claim, write and publish one page')
    p.add_argument('--owner', required=True)
    p.add_argument('--url', required=True)
    p.add_argument('--preferred', required=True)
    p.add_argument('--payload-file', required=True)
    p.add_argument('--status', default='200 OK')
    p.add_argument('--content-type', default='text/html')
    p.add_argument('--header', action='append', default=[])
    p.add_argument('--takeover', action='store_true')
    p.add_argument('--adopt-first', action='store_true')
    p.add_argument('--dispatch-id', default=None)
    p.add_argument('--interrupt', default=None,
                   help="fraction of the response, or 'none'")
    p.add_argument('--stop-after', choices=('write', 'record', 'index'),
                   default='index')
    p.add_argument('--reindex', action='store_true')

    p = sub.add_parser('adopt', help='take over and adopt an orphaned capture')
    p.add_argument('--owner', required=True)
    p.add_argument('--url', required=True)
    p.add_argument('--preferred', default=None)
    p.add_argument('--dispatch-id', default=None)

    p = sub.add_parser('resume', help='recover and publish without capturing')
    p.add_argument('--url', required=True)
    p.add_argument('--force', action='store_true',
                   help='recover even if a lease claims to be live')

    p = sub.add_parser('submit', help='submit an index message for a capture')
    p.add_argument('--url', required=True)
    p.add_argument('--token', default=None)
    p.add_argument('--claim-token', dest='token', default=None)

    p = sub.add_parser('read', help='read a proven capture payload')
    p.add_argument('--url', required=True)
    p.add_argument('--output', default=None)

    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    try:
        if args.command == 'init':
            return _init(args)
        manifest = ContinuationManifest(args.root, lease_seconds=args.lease)
    except ManifestError as exc:
        return _fail(str(exc), EXIT_MANIFEST)

    try:
        handler = {
            'bootstrap': _bootstrap,
            'plan': _plan,
            'capture': _capture,
            'adopt': _adopt,
            'resume': _resume,
            'submit': _submit,
            'read': _read,
        }[args.command]
    except KeyError:  # pragma: no cover
        return _fail('unknown command', EXIT_MANIFEST)

    try:
        return handler(manifest, args)
    except ClaimError as exc:
        return _fail(str(exc), EXIT_CLAIM)
    except IndexFenced as exc:
        return _fail(exc.reason, EXIT_FENCED)
    except VerificationError as exc:
        return _fail(str(exc), EXIT_VERIFY)
    except ManifestError as exc:
        return _fail(str(exc), EXIT_MANIFEST)


def _init(args):
    pages = [{'url': url,
              'authorized': url not in set(args.unauthorized)}
             for url in args.url]
    ContinuationManifest.create(args.root, pages, lease_seconds=args.lease)
    _json({'created': args.root, 'pages': [p['url'] for p in pages]})
    return EXIT_OK


def _bootstrap(manifest, args):
    entries = list(read_cdx_entries(args.index, args.volume_root))
    result = manifest.bootstrap_from_index(entries)
    _json(result)
    return EXIT_OK


def _plan(manifest, args):
    _json(manifest.plan())
    return EXIT_OK


def _capture(manifest, args):
    with open(args.payload_file, 'rb') as fh:
        payload = fh.read()

    claim = manifest.claim(args.url, args.owner,
                           preferred_name=args.preferred,
                           takeover=args.takeover or args.adopt_first,
                           dispatch_id=args.dispatch_id)
    if not claim:
        page = manifest.page(args.url)
        if page['state'] in ('captured', 'fenced', 'indexed'):
            _json({'state': page['state'], 'already': True,
                   'capture': page.get('capture')})
            return EXIT_OK
        return _fail('claim refused', EXIT_CLAIM)

    if args.takeover or args.adopt_first:
        # A dead writer may already have persisted a complete volume.  Adopt
        # it before allocating another volume, so a proven page is never
        # recorded again as if it were unstarted.
        adopted = manifest.adopt(args.url, claim['claim_token'])
        if adopted:
            adopted['adopted'] = True
            _json(adopted)
            return EXIT_OK

    path = manifest.allocate_volume(claim, 'archive', args.preferred)
    writer = ContinuationWriter(manifest.root)
    headers = []
    for item in args.header:
        name, _, value = item.partition(':')
        headers.append((name.strip(), value.strip()))

    interrupt = _parse_interrupt(args.interrupt)
    try:
        capture = writer.write_capture(path, args.url, payload,
                                       content_type=args.content_type,
                                       status=args.status,
                                       headers=headers,
                                       interrupt_after=interrupt)
    except InterruptedCapture as exc:
        manifest.abandon(args.url, claim['claim_token'],
                         'capture-interrupted', discard_volume=path)
        _json({'state': 'interrupted', 'written': exc.written,
               'volume': manifest._rel(path),
               'claim_token': claim['claim_token'],
               'attempt': claim['attempt']})
        return EXIT_INTERRUPTED

    if args.stop_after == 'write':
        _json({'state': 'written-unrecorded', 'capture': capture,
               'claim_token': claim['claim_token'],
               'attempt': claim['attempt'], 'volume': manifest._rel(path)})
        return EXIT_OK

    manifest.record_capture(args.url, claim['claim_token'],
                            capture['filename'], capture['offset'],
                            capture['length'], capture['payload_digest'],
                            capture['record_id'], capture['http_status'],
                            allow_reindex=args.reindex)

    if args.stop_after == 'record':
        page = manifest.page(args.url)
        _json({'state': page['state'], 'capture': page['capture'],
               'claim_token': claim['claim_token'],
               'attempt': claim['attempt'], 'volume': manifest._rel(path)})
        return EXIT_OK

    result = manifest.recover(args.url)
    result['capture'] = capture
    result['claim_token'] = claim['claim_token']
    result['attempt'] = claim['attempt']
    _json(result)
    return EXIT_OK


def _adopt(manifest, args):
    claim = manifest.claim(args.url, args.owner,
                           preferred_name=args.preferred,
                           takeover=True,
                           dispatch_id=args.dispatch_id)
    if not claim:
        page = manifest.page(args.url)
        if page['state'] in ('captured', 'fenced', 'indexed'):
            _json({'state': page['state'], 'already': True,
                   'capture': page.get('capture')})
            return EXIT_OK
        return _fail('claim refused', EXIT_CLAIM)

    result = manifest.adopt(args.url, claim['claim_token'])
    if not result:
        _json({'state': 'writing', 'adopted': False,
               'claim_token': claim['claim_token'],
               'attempt': claim['attempt']})
        return EXIT_OK
    result['adopted'] = True
    _json(result)
    return EXIT_OK


def _resume(manifest, args):
    _json(manifest.recover(args.url, force=args.force))
    return EXIT_OK


def _submit(manifest, args):
    result = manifest.index_message(args.url, args.token)
    _json({'state': 'indexed', 'result': result})
    return EXIT_OK


def _read(manifest, args):
    page = manifest.page(args.url)
    capture = page.get('capture')
    if not capture:
        entry = manifest.index.lookup(args.url)
        if not entry:
            return _fail('no proven capture', EXIT_VERIFY)
        capture = entry

    info, payload = read_capture_payload(
        manifest._resolve(capture['filename']),
        capture['offset'], capture['length'],
        uri=args.url, digest=capture.get('payload_digest'))

    if args.output:
        with open(args.output, 'wb') as fh:
            fh.write(payload)
        _json({'state': page['state'], 'capture': capture,
               'output': args.output, 'bytes': len(payload)})
    else:
        _json({'state': page['state'], 'capture': info,
               'payload': payload.decode('utf-8', 'replace')})
    return EXIT_OK


def _parse_interrupt(value):
    if value is None:
        return None
    if value == 'none':
        return 'none'
    return float(value)


def _fail(message, code):
    _json({'error': message})
    return code


if __name__ == '__main__':  # pragma: no cover
    sys.exit(main())
