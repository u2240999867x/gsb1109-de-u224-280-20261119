"""Bounded-memory paging over an immutable, versioned capture timeline.

Opening a version never builds or copies the full sorted capture list. The
page reader merges the sorted delta-run prefix visible to that version
(:class:`pywb.warcserver.timelinestore.runs.RunMerge`, one heap head per run)
and buffers exactly one output page plus one look-ahead page -- the two
request pages the memory budget permits -- however large the timeline is.

Each emitted entry carries provenance pointers back to the untouched source
original pages; URI and timestamp are stored once on disk and referenced, not
duplicated per visible version.
"""

import os

from pywb.warcserver.timelinestore.budget import BoundedList
from pywb.warcserver.timelinestore.runs import RunMerge


class TimelinePage(object):
    def __init__(self, version_id, page_index, page_size, entries,
                 has_next, coverage, complete):
        self.version_id = version_id
        self.page_index = page_index
        self.page_size = page_size
        self.entries = entries
        self.has_next = has_next
        self.coverage = coverage
        self.complete = complete

    def to_dict(self):
        return {
            'version_id': self.version_id,
            'page_index': self.page_index,
            'page_size': self.page_size,
            'entries': self.entries,
            'has_next': self.has_next,
            'coverage': self.coverage,
            'complete': self.complete,
        }


class VersionReader(object):
    """Streams pages of one published version, newest captures first."""

    def __init__(self, store, version):
        self.store = store
        if isinstance(version, str):
            version = store.version(version)
        self.version = version

    def _run_merge(self):
        # Only the run files listed on the version are visible, so a reader
        # of an old version never sees later rounds' corrections.
        visible = set(self.version.runs)
        merge = RunMerge(self.store, self.version, self.store.budget)
        merge.run_paths = [
            path for path in merge.run_paths
            if os.path.basename(path) in visible]
        return merge

    def _entry(self, timestamp, memento_url, sources):
        provenance = []
        for source_id in sorted(sources):
            locator = sources[source_id]
            provenance.append({
                'source_id': source_id,
                'original': {
                    'round': locator['round'],
                    'page_index': locator['page_index'],
                    'path': os.path.join(
                        self.store.source_dir, source_id,
                        'round-{0}'.format(locator['round']),
                        'page-{0:06d}.json'.format(locator['page_index'])),
                },
            })
        return {
            'timestamp': timestamp,
            'memento_url': memento_url,
            'sources': sorted(sources),
            'provenance': provenance,
        }

    def iter_pages(self, page_size):
        """Yield :class:`TimelinePage` objects, newest captures first.

        High-water memory: one filled output page, one look-ahead page and one
        heap head per run -- bounded regardless of total capture count.
        """
        if page_size <= 0:
            raise ValueError('page_size must be positive')

        budget = self.store.budget
        stream = iter(self._run_merge())
        output = BoundedList(budget, note='reader:output')
        lookahead = BoundedList(budget, note='reader:lookahead')

        def fill(buffer_, limit):
            while len(buffer_) < limit:
                try:
                    triple = next(stream)
                except StopIteration:
                    return False
                buffer_.append(self._entry(*triple))
            return True

        page_index = 0
        fill(output, page_size)
        while len(output):
            more = fill(lookahead, page_size)
            entries = output.take_items()
            page = TimelinePage(
                version_id=self.version.version_id,
                page_index=page_index,
                page_size=len(entries),
                entries=entries,
                has_next=bool(more) or bool(lookahead),
                coverage=self.version.coverage(),
                complete=self.version.is_complete())
            yield page

            # swap the look-ahead page into the output slot; the two pages
            # simply exchange roles, never coexisting beyond the budget
            output, lookahead = lookahead, BoundedList(
                budget, note='reader:lookahead')
            page_index += 1
            fill(output, page_size)

    def first_page(self, page_size):
        return next(self.iter_pages(page_size), None)

    def page(self, page_index, page_size):
        for page in self.iter_pages(page_size):
            if page.page_index == page_index:
                return page
        return None

    def empty_page(self):
        """A zero-entry page still reports coverage (e.g. all sources done
        with no captures, which is complete, not a failure)."""
        return TimelinePage(
            version_id=self.version.version_id,
            page_index=0,
            page_size=0,
            entries=[],
            has_next=False,
            coverage=self.version.coverage(),
            complete=self.version.is_complete())
