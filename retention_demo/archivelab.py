"""Offline WARC archive helpers for the retention demo.

Everything here works on plain files in a shared directory: no network, no
redis.  Two real response volumes carry the *same* payload, URI and WARC-Date
but live in different retention volumes, plus deliberately broken CDX entries
(a moved-volume CDX and a wrong-offset CDX).
"""

import json
import os
import shutil
from io import BytesIO

from warcio.statusandheaders import StatusAndHeaders
from warcio.warcwriter import WARCWriter

from pywb.indexer.cdxindexer import write_cdx_index
from pywb.warcserver.index.cdxobject import CDXObject
from pywb.warcserver.index.indexsource import FileIndexSource
from pywb.warcserver.index.query import CDXQuery
from pywb.warcserver.resource.blockrecordloader import BlockArcWarcRecordLoader
from pywb.warcserver.resource.pathresolvers import PrefixResolver
from pywb.warcserver.resource.resolvingloader import ResolvingLoader


URI = 'http://example.invalid/payload'
WARC_DATE_ISO = '2026-11-23T10:00:00Z'
WARC_DATE_TS = '20261123100000'

# identical body in every real volume; the provenance marker is only used in
# log output, never as a byte-level distinction
PAYLOAD = (b'retention-demo identical payload\r\n'
           b'two volumes carry these exact bytes, so replay must prove\r\n'
           b'which concrete WARC record supplied them, not merely equality.\r\n')

VOL_A = 'volume-a.warc.gz'
VOL_B = 'volume-b.warc.gz'


def make_http_headers(capture_name):
    return StatusAndHeaders(
        '200 OK',
        [('Content-Type', 'text/plain; charset=utf-8'),
         ('Content-Length', str(len(PAYLOAD))),
         ('X-Capture', capture_name)],
        protocol='HTTP/1.1')


def write_response_volume(path, volume_name, capture_name):
    with open(path, 'wb') as fh:
        writer = WARCWriter(fh, gzip=True)
        writer.write_record(writer.create_warc_record(
            URI, 'warcinfo',
            payload=BytesIO(('software: retention-demo\r\nvolume: {0}\r\n'
                             .format(volume_name)).encode('utf-8')),
            warc_headers_dict={'WARC-Date': WARC_DATE_ISO}))
        writer.write_record(writer.create_warc_record(
            URI, 'response',
            payload=BytesIO(PAYLOAD),
            http_headers=make_http_headers(capture_name),
            warc_headers_dict={'WARC-Date': WARC_DATE_ISO}))


def cdx_lines(path, filename):
    out = BytesIO()
    with open(path, 'rb') as fh:
        write_cdx_index(out, fh, filename, cdxj=True, append_post=True)
    return [line for line in out.getvalue().rstrip().split(b'\n') if line]


def response_cdx(path, filename):
    for line in cdx_lines(path, filename):
        cdx = CDXObject(line)
        if cdx.get('mime') != 'warc/revisit':
            return cdx
    raise RuntimeError('no response record in {0}'.format(filename))


def write_index(path, cdx, **overrides):
    cdx = CDXObject(str(cdx).encode('utf-8'))
    for key, value in overrides.items():
        if value is None:
            cdx.pop(key, None)
        else:
            cdx[key] = value
    with open(path, 'w') as fh:
        fh.write(cdx.to_cdxj())
    return cdx


def build_seed_archive(root):
    """Create volumes, indexes and the two deliberately broken CDX entries."""
    warc_dir = os.path.join(root, 'warcs')
    index_dir = os.path.join(root, 'indexes')
    recorded_dir = os.path.join(root, 'recorded')
    signals_dir = os.path.join(root, 'signals')
    os.makedirs(warc_dir)
    os.makedirs(index_dir)
    os.makedirs(recorded_dir)
    os.makedirs(signals_dir)

    vol_a_path = os.path.join(warc_dir, VOL_A)
    vol_b_path = os.path.join(warc_dir, VOL_B)
    write_response_volume(vol_a_path, VOL_A, 'original-volume-a')
    write_response_volume(vol_b_path, VOL_B, 'original-volume-b')

    cdx_a = response_cdx(vol_a_path, VOL_A)
    cdx_b = response_cdx(vol_b_path, VOL_B)

    # healthy per-volume index sources
    write_index(os.path.join(index_dir, 'source-a.cdxj'), cdx_a)
    write_index(os.path.join(index_dir, 'source-b.cdxj'), cdx_b)

    # moved-volume CDX: index still advertises a file name that no longer exists
    write_index(os.path.join(index_dir, 'moved-a.cdxj'), cdx_a,
                filename='moved-offline-volume-a.warc.gz')

    # wrong-offset CDX: file exists but the offset does not point at the record
    write_index(os.path.join(index_dir, 'bad-offset-a.cdxj'), cdx_a,
                offset=str(int(cdx_a['offset']) + 7))

    return {'warc_dir': warc_dir,
            'index_dir': index_dir,
            'recorded_dir': recorded_dir,
            'signals_dir': signals_dir,
            'cdx_a': cdx_a,
            'cdx_b': cdx_b}


def make_capture_record(writer, capture_name, warc_date=WARC_DATE_ISO):
    return writer.create_warc_record(
        URI, 'response',
        payload=BytesIO(PAYLOAD),
        http_headers=make_http_headers(capture_name),
        warc_headers_dict={'WARC-Date': warc_date})


class TrackingRecordLoader(BlockArcWarcRecordLoader):
    """Record loader remembering every (path, offset) it actually opened."""

    def __init__(self):
        super(TrackingRecordLoader, self).__init__()
        self.opened = []

    def load(self, url, offset, length, no_record_parse=False):
        self.opened.append((url, int(offset)))
        return super(TrackingRecordLoader, self).load(
            url, offset, length, no_record_parse=no_record_parse)


def ordered_cdx_loader(sources):
    """Return a cdx_loader that queries sources in the configured order.

    Source order, not timestamp merging, decides which identical-payload
    candidate is offered first.  The standard ``filter`` params used by
    revisit resolution (digest, !mime:warc/revisit) are still honored.
    """
    from pywb.warcserver.index.cdxops import cdx_filter
    from pywb.warcserver.index.query import CDXQuery

    def loader(params):
        qparams = CDXQuery(dict(params)).params
        cdx_iter = (cdx for source in sources
                    for cdx in source.load_index(qparams))
        filters = qparams.get('filter')
        if filters:
            cdx_iter = cdx_filter(cdx_iter, filters)
        return cdx_iter
    return loader


def replay(root, index_paths, search_dirs=None, tracking=None,
           provenance=True):
    """Replay the URI through ordinary revisit resolution.

    Returns ``(http_headers, payload_bytes, chosen_cdx, tracking)``.
    ``index_paths`` order is the order in which payload candidates are tried.

    When ``provenance`` is True the index sources are resolved exactly as the
    warcserver does (``cdx_resolve_revisits``): the revisit CDX gets the
    original's mime and explicit ``orig.*`` fields, so the payload is loaded
    directly from the agreed concrete record regardless of source order.
    When False the pre-resolved fields are dropped and the ordinary
    WARC-Refers-To lookup path used by existing archives is exercised instead.
    """
    warc_dir = os.path.join(root, 'warcs')
    search_dirs = search_dirs or [warc_dir]
    sources = [FileIndexSource(path) for path in index_paths]

    params = {'url': URI, 'closest': WARC_DATE_TS}
    qparams = CDXQuery(dict(params)).params
    all_cdx = [cdx for source in sources
               for cdx in source.load_index(qparams)]

    revisits = [entry for entry in all_cdx
                if entry.get('mime') == 'warc/revisit']

    if not revisits:
        # self contained capture: no revisit resolution, load directly
        cdx = all_cdx[-1]
        cdx_loader = None
    else:
        revisit_cdx = revisits[-1]
        if provenance:
            # mirror what resolveRevisits produces, but keep the orig.* fields
            # the recorder itself wrote: they pin the payload to the concrete
            # record verified at capture time, independent of source ordering
            digest = revisit_cdx.get('digest')
            original = next(entry for entry in all_cdx
                            if entry.get('mime') != 'warc/revisit'
                            and entry.get('digest') == digest)
            cdx = CDXObject(str(revisit_cdx).encode('utf-8'))
            cdx['mime'] = original['mime']
            cdx['status'] = original.get('status', '-')
            cdx_loader = None
        else:
            # ordinary same-URL revisit resolution: drop pre-resolved orig.*
            # fields so the standard WARC-Refers-To lookup walks sources itself
            cdx = CDXObject(str(revisit_cdx).encode('utf-8'))
            cdx.pop('orig.filename', None)
            cdx.pop('orig.offset', None)
            cdx.pop('orig.length', None)
            cdx_loader = ordered_cdx_loader(sources)

    loader = tracking or TrackingRecordLoader()
    resolvers = [PrefixResolver(directory + os.path.sep)
                 for directory in search_dirs]
    resolving = ResolvingLoader(resolvers, record_loader=loader)

    headers, stream = resolving(cdx, [], cdx_loader)
    body = stream.read()
    return headers, body, cdx, loader


def reset_signals(signals_dir):
    if os.path.isdir(signals_dir):
        shutil.rmtree(signals_dir)
    os.makedirs(signals_dir)


def signal_path(signals_dir, name):
    return os.path.join(signals_dir, name)


def write_signal(signals_dir, name, payload=''):
    with open(signal_path(signals_dir, name), 'w') as fh:
        fh.write(payload)


def wait_for_signal(signals_dir, name, timeout=20.0):
    import time
    deadline = time.time() + timeout
    path = signal_path(signals_dir, name)
    while time.time() < deadline:
        if os.path.exists(path):
            with open(path) as fh:
                return fh.read()
        time.sleep(0.02)
    raise RuntimeError('timed out waiting for signal {0}'.format(name))


def read_json(path):
    with open(path) as fh:
        return json.load(fh)
