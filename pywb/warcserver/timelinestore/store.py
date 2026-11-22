"""Disk-backed merge engine for multi-archive TimeMaps.

Design constraints (see the acceptance scenario for this module):

* archives may answer out of order and in several paginated rounds;
* several distinct memento URLs may share one UTC second;
* the same memento URL may be reported by more than one archive;
* a later round must be able to correct an earlier round (retract a claim);
* a late response from an older round must not resurrect a retracted claim;
* a source that completes without captures is *complete*, not failed;
* a source whose last page breaks is *partial*, and everything verified so
  far must remain pageable;
* the merger may only keep two request pages in memory at a time;
* source originals and the actual add/retract journal live on disk, never a
  second full sorted copy of the timeline.

Durable state under the store root:

* ``sources/<id>/round-<n>/page-<m>.json`` -- untouched source originals;
* ``journal.jsonl``                         -- every applied add/retract;
* ``evidence/<ts>-<url>.json``              -- current per-memento sources;
* journal add entries embed the source-original round/page pointer;
* ``runs/run-<n>.jsonl``                    -- sorted per-publish delta runs;
* ``versions/<id>.json``                    -- immutable version descriptors.
"""

import json
import os
import threading

from contextlib import contextmanager

from pywb.warcserver.timelinestore.budget import MemoryBudget, BoundedList
from pywb.warcserver.timelinestore.runs import external_sort_delta


class StaleRoundError(Exception):
    """A response arrived for a round that has already been superseded."""


class MergeClosedError(Exception):
    """The merge session has been closed and accepts no further responses."""


class SourceStatus(object):
    PENDING = 'pending'
    COMPLETE = 'complete'
    PARTIAL = 'partial'

    ALL = (PENDING, COMPLETE, PARTIAL)


def _atomic_write(path, text):
    tmp = path + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as fh:
        fh.write(text)
    os.replace(tmp, path)


def _write_json(path, obj):
    _atomic_write(path, json.dumps(obj, sort_keys=True, ensure_ascii=False))


def _read_json(path, default):
    if not os.path.exists(path):
        return default
    with open(path, encoding='utf-8') as fh:
        return json.load(fh)


class Version(object):
    """Immutable descriptor of one published timeline version."""

    def __init__(self, version_id, seq, journal_offset, source_states,
                 runs=None):
        self.version_id = version_id
        self.seq = seq
        self.journal_offset = journal_offset
        self.source_states = dict(source_states)
        # sorted-run files visible to this version; later runs are invisible
        self.runs = list(runs or [])

    def to_dict(self):
        return {
            'version_id': self.version_id,
            'seq': self.seq,
            'journal_offset': self.journal_offset,
            'source_states': self.source_states,
            'runs': self.runs,
        }

    @classmethod
    def from_dict(cls, data):
        return cls(data['version_id'], data['seq'],
                   data['journal_offset'], data['source_states'],
                   data.get('runs', []))

    def coverage(self):
        """Per-source coverage: one of the :class:`SourceStatus` values."""
        return dict(self.source_states)

    def is_complete(self):
        return bool(self.source_states) and all(
            state == SourceStatus.COMPLETE
            for state in self.source_states.values())


class MergeSession(object):
    """Collects interleaved, paginated responses from the archives.

    Memory bound: at most one request page is held while it is streamed to
    disk (the second page of the two-page budget is used only while publish
    sorting). Nothing accumulates the full timeline.
    """

    def __init__(self, store, target_uri):
        self.store = store
        self.target_uri = target_uri
        self._lock = threading.RLock()
        self._closed = False

    def ingest_page(self, source_id, round_id, page_index, mementos,
                    page_complete, more_pages):
        """Apply one response page from one archive.

        :param source_id: archive identifier
        :param round_id: monotonically increasing request round for that
            source; a page from a round older than the latest seen round is
            rejected with :class:`StaleRoundError`
        :param page_index: 0-based page index within the round
        :param mementos: iterable of ``(timestamp, memento_url)`` pairs; it is
            consumed as a single request page and streamed to disk
        :param page_complete: the page itself was received intact
        :param more_pages: the source says further pages exist
        """
        with self._lock:
            if self._closed:
                raise MergeClosedError('merge for {0} is closed'.format(
                    self.target_uri))

            state = self.store.source_state(source_id)
            if round_id < state['round']:
                raise StaleRoundError(
                    'source {0} round {1} older than current round {2}'.format(
                        source_id, round_id, state['round']))

            if round_id > state['round']:
                # Newer round: retract every claim this source alone was
                # making, so the new round can correct it.
                self.store.begin_round(source_id, round_id)

            if not page_complete:
                # Broken segment: keep everything verified so far, mark the
                # source partial, and discard this page's unverified content.
                self.store.mark_source(source_id, SourceStatus.PARTIAL)
                self._store_original(source_id, round_id, page_index,
                                     mementos=None, page_complete=False,
                                     more_pages=more_pages)
                return

            page = BoundedList(self.store.budget,
                               note='ingest:{0}'.format(source_id))
            page.extend(mementos)
            page_items = page.take_items()
            self._store_original(source_id, round_id, page_index,
                                 mementos=page_items, page_complete=True,
                                 more_pages=more_pages)

            for timestamp, memento_url in page_items:
                self.store.add_claim(source_id, timestamp, memento_url,
                                     round_id, page_index)

            if not more_pages:
                # Clean end of stream: finished, even with zero captures.
                self.store.mark_source(source_id, SourceStatus.COMPLETE)

    def mark_failed(self, source_id, round_id=None):
        """Record a hard failure (e.g. a broken last segment)."""
        with self._lock:
            if self._closed:
                raise MergeClosedError('merge for {0} is closed'.format(
                    self.target_uri))
            if round_id is not None:
                state = self.store.source_state(source_id)
                if round_id < state['round']:
                    raise StaleRoundError(
                        'source {0} failure report for stale round {1}'.format(
                            source_id, round_id))
            self.store.mark_source(source_id, SourceStatus.PARTIAL)

    def _store_original(self, source_id, round_id, page_index, mementos,
                        page_complete, more_pages):
        self.store.store_original(source_id, round_id, page_index, {
            'target_uri': self.target_uri,
            'source_id': source_id,
            'round': round_id,
            'page_index': page_index,
            'page_complete': page_complete,
            'more_pages': more_pages,
            'mementos': mementos if mementos is not None else [],
        })

    def publish(self, version_id=None):
        with self._lock:
            return self.store.publish(version_id)

    def close(self):
        with self._lock:
            self._closed = True

    @contextmanager
    def open(self):
        try:
            yield self
        finally:
            self.close()


class TimelineStore(object):
    """On-disk store for one target URI's merged capture timeline."""

    def __init__(self, root_dir, target_uri, page_size=100):
        self.root_dir = os.path.abspath(root_dir)
        self.target_uri = target_uri
        self.page_size = page_size
        self.budget = MemoryBudget(page_size)
        self.meta_dir = os.path.join(self.root_dir, 'meta')
        self.source_dir = os.path.join(self.root_dir, 'sources')
        self.evidence_dir = os.path.join(self.root_dir, 'evidence')
        self.runs_dir = os.path.join(self.root_dir, 'runs')
        self.versions_dir = os.path.join(self.root_dir, 'versions')
        for directory in (self.meta_dir, self.source_dir, self.evidence_dir,
                          self.runs_dir, self.versions_dir):
            if not os.path.isdir(directory):
                os.makedirs(directory)

        self.meta_path = os.path.join(self.meta_dir, 'timeline.json')
        self.journal_path = os.path.join(self.root_dir, 'journal.jsonl')
        self._lock = threading.RLock()
        if not os.path.exists(self.journal_path):
            open(self.journal_path, 'a').close()
        if not os.path.exists(self.meta_path):
            _write_json(self.meta_path, {
                'target_uri': target_uri,
                'next_version': 1,
                'next_seq': 0,
                'next_run': 1,
                'published': [],
            })

    def new_session(self):
        return MergeSession(self, self.target_uri)

    # -- source state ------------------------------------------------------

    def _source_state_path(self, source_id):
        return os.path.join(self.meta_dir, 'source-{0}.json'.format(source_id))

    def source_state(self, source_id):
        return _read_json(self._source_state_path(source_id), {
            'source_id': source_id,
            'round': -1,
            'status': SourceStatus.PENDING,
            'round_pages': {},
        })

    def _save_source_state(self, state):
        _write_json(self._source_state_path(state['source_id']), state)

    def mark_source(self, source_id, status):
        with self._lock:
            state = self.source_state(source_id)
            state['status'] = status
            self._save_source_state(state)

    def begin_round(self, source_id, round_id):
        with self._lock:
            state = self.source_state(source_id)
            state['round'] = round_id
            state['round_pages'] = {}
            # a new round re-opens coverage until it finishes or breaks
            state['status'] = SourceStatus.PENDING
            self._save_source_state(state)
            self._retract_source_claims(source_id)

    def store_original(self, source_id, round_id, page_index, payload):
        with self._lock:
            source_round_dir = os.path.join(
                self.source_dir, source_id, 'round-{0}'.format(round_id))
            if not os.path.isdir(source_round_dir):
                os.makedirs(source_round_dir)
            page_path = os.path.join(
                source_round_dir, 'page-{0:06d}.json'.format(page_index))
            _write_json(page_path, payload)

            state = self.source_state(source_id)
            pages = state.setdefault('round_pages', {})
            pages[str(round_id)] = sorted(
                set(pages.get(str(round_id), []) + [page_index]))
            self._save_source_state(state)

    # -- evidence, pointers and journal ------------------------------------

    def _evidence_path(self, timestamp, memento_url):
        safe_url = memento_url.replace('/', '_').replace(':', '_')
        return os.path.join(self.evidence_dir,
                            '{0}-{1}.json'.format(timestamp, safe_url))

    def _read_evidence(self, timestamp, memento_url):
        return _read_json(
            self._evidence_path(timestamp, memento_url),
            {'timestamp': timestamp, 'memento_url': memento_url,
             'sources': [], 'retracted': []})

    def _write_evidence(self, evidence):
        _write_json(self._evidence_path(evidence['timestamp'],
                                        evidence['memento_url']), evidence)

    def _next_seq(self):
        meta = _read_json(self.meta_path, {})
        seq = meta['next_seq']
        meta['next_seq'] = seq + 1
        _write_json(self.meta_path, meta)
        return seq

    def add_claim(self, source_id, timestamp, memento_url, round_id,
                  page_index):
        """Apply one ``(timestamp, url)`` claim made by one source.

        The source-original round/page pointer is embedded in the immutable
        journal entry so every published version can trace the claim back to
        the exact original page that supported it at publish time.
        """
        with self._lock:
            evidence = self._read_evidence(timestamp, memento_url)
            sources = set(evidence['sources'])
            retracted = set(evidence['retracted'])
            retracted.discard(source_id)
            existed = source_id in sources
            sources.add(source_id)
            evidence['sources'] = sorted(sources)
            evidence['retracted'] = sorted(retracted)
            self._write_evidence(evidence)

            if not existed:
                self._append_journal({
                    'op': 'add',
                    'timestamp': timestamp,
                    'memento_url': memento_url,
                    'source': source_id,
                    'round': round_id,
                    'page_index': page_index,
                    'seq': self._next_seq(),
                })

    def _retract_source_claims(self, source_id):
        """Remove one source's solo evidence; shared evidence keeps the rest.

        Only the actual retractions touch the journal; nothing rewrites or
        copies the full timeline.
        """
        if not os.path.isdir(self.evidence_dir):
            return
        for name in sorted(os.listdir(self.evidence_dir)):
            if not name.endswith('.json'):
                continue
            path = os.path.join(self.evidence_dir, name)
            evidence = _read_json(path, None)
            if evidence is None or source_id not in evidence['sources']:
                continue

            evidence['sources'].remove(source_id)
            retracted = set(evidence['retracted'])
            retracted.add(source_id)
            evidence['retracted'] = sorted(retracted)

            if evidence['sources']:
                # another archive still vouches for this memento: keep it
                self._write_evidence(evidence)
                op = 'retract-source'
            else:
                os.remove(path)
                op = 'retract'
            self._append_journal({
                'op': op,
                'timestamp': evidence['timestamp'],
                'memento_url': evidence['memento_url'],
                'source': source_id,
                'seq': self._next_seq(),
            })

    def _append_journal(self, entry):
        with open(self.journal_path, 'a', encoding='utf-8') as fh:
            fh.write(json.dumps(entry, sort_keys=True,
                                ensure_ascii=False) + '\n')

    def journal_size(self):
        return os.path.getsize(self.journal_path)

    # -- versions and sorted delta runs ------------------------------------

    def source_ids(self):
        prefix = 'source-'
        result = []
        for name in sorted(os.listdir(self.meta_dir)):
            if name.startswith(prefix) and name.endswith('.json'):
                result.append(name[len(prefix):-len('.json')])
        return result

    def current_source_states(self):
        return {source_id: self.source_state(source_id)['status']
                for source_id in self.source_ids()}

    def _journal_delta(self, start_offset, end_offset):
        # binary mode so byte offsets line up exactly with journal_size()
        with open(self.journal_path, 'rb') as fh:
            fh.seek(start_offset)
            while fh.tell() < end_offset:
                line = fh.readline()
                if not line:
                    break
                yield json.loads(line.decode('utf-8'))

    def publish(self, version_id=None):
        with self._lock:
            meta = _read_json(self.meta_path, {})
            if version_id is None:
                version_id = 'v{0}'.format(meta['next_version'])
            if any(v['version_id'] == version_id
                   for v in meta.get('published', [])):
                raise ValueError(
                    'version {0} already published'.format(version_id))

            end_offset = self.journal_size()
            run_name = 'run-{0:06d}.jsonl'.format(meta['next_run'])
            run_path = os.path.join(self.runs_dir, run_name)
            # Even an empty delta gets an (empty) run so run counts line up
            # with versions and old versions keep an immutable run prefix.
            external_sort_delta(
                self._journal_delta(
                    meta.get('last_journal_offset', 0), end_offset),
                run_path, self.budget, self.page_size,
                note='publish:{0}'.format(run_name))
            meta['next_run'] += 1
            meta['last_journal_offset'] = end_offset

            visible_runs = sorted(
                name for name in os.listdir(self.runs_dir)
                if name.endswith('.jsonl'))

            version = Version(
                version_id=version_id,
                seq=meta['next_version'],
                journal_offset=end_offset,
                source_states=self.current_source_states(),
                runs=visible_runs)

            _write_json(
                os.path.join(self.versions_dir,
                             '{0}.json'.format(version_id)),
                version.to_dict())

            meta['next_version'] += 1
            meta.setdefault('published', []).append(version.to_dict())
            _write_json(self.meta_path, meta)
            return version

    def version(self, version_id):
        path = os.path.join(self.versions_dir, '{0}.json'.format(version_id))
        return Version.from_dict(_read_json(path, None))

    def latest_version(self):
        meta = _read_json(self.meta_path, {'published': []})
        published = meta.get('published') or []
        if not published:
            return None
        return Version.from_dict(published[-1])

    def versions(self):
        meta = _read_json(self.meta_path, {'published': []})
        return [Version.from_dict(item) for item in meta.get('published', [])]

    # -- provenance --------------------------------------------------------

    def original_pages(self, source_id):
        """Yield the stored source-original pages, in on-disk order."""
        source_root = os.path.join(self.source_dir, source_id)
        if not os.path.isdir(source_root):
            return
        for round_dir in sorted(os.listdir(source_root)):
            round_path = os.path.join(source_root, round_dir)
            for page_name in sorted(os.listdir(round_path)):
                with open(os.path.join(round_path, page_name),
                          encoding='utf-8') as fh:
                    yield json.load(fh)

    def journal_entries(self):
        with open(self.journal_path, encoding='utf-8') as fh:
            for line in fh:
                line = line.strip()
                if line:
                    yield json.loads(line)
