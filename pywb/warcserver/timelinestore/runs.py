"""Sorted delta runs: external sort and bounded k-way merge.

Each publish flushes *only* the journal adds/retracts produced since the
previous publish into one sorted run file. Runs therefore hold the actual
add/delete facts (the same facts as the journal), never a per-version copy of
the whole capture list.

Readers merge the run prefix visible to a version with a k-way heap: one head
entry per run plus (in the pager) one output page -- inside the two-page
budget regardless of how many captures exist.
"""

import heapq
import json
import os

from pywb.warcserver.timelinestore.budget import BoundedList


def _sort_key(entry):
    # newest first; ties (same UTC second) ordered by memento url so that two
    # distinct real addresses sharing a second are both kept and stable
    return (-_ts_int(entry['timestamp']), entry['memento_url'],
            entry['seq'])


def _ts_int(timestamp):
    return int(timestamp)


def external_sort_delta(entries, run_path, budget, page_size, note='sort'):
    """Sort journal delta entries using one-page chunk files on disk.

    Memory stays inside two request pages: chunks are filled one page at a
    time, then merged iteratively with a fan-in of two (two heap heads plus
    one output page), however large the delta is.
    """
    chunk_paths = []
    chunk = BoundedList(budget, note + ':chunk')
    chunk_index = 0
    for entry in entries:
        chunk.append(entry)
        if len(chunk) >= page_size:
            chunk_paths.append(_flush_chunk(chunk, run_path, chunk_index))
            chunk_index += 1
            chunk.clear()
    if chunk:
        chunk_paths.append(_flush_chunk(chunk, run_path, chunk_index))
        chunk.clear()

    if not chunk_paths:
        open(run_path, 'w').close()
        return run_path

    # Iterative bounded fan-in merge: never more than two merge heads plus
    # one output page resident at once.
    generation = 0
    while len(chunk_paths) > 1:
        next_paths = []
        for pair_index in range(0, len(chunk_paths), 2):
            pair = chunk_paths[pair_index:pair_index + 2]
            merged_path = '{0}.merge-{1}-{2:04d}'.format(
                run_path, generation, pair_index)
            _merge_files(pair, merged_path, budget, page_size, note)
            next_paths.append(merged_path)
        for path in chunk_paths:
            if os.path.exists(path):
                os.remove(path)
        chunk_paths = next_paths
        generation += 1

    os.replace(chunk_paths[0], run_path)
    return run_path


def _merge_files(input_paths, output_path, budget, page_size, note):
    handles = [open(path, encoding='utf-8') for path in input_paths]
    try:
        heap = []
        for index, handle in enumerate(handles):
            line = handle.readline()
            if line:
                entry = json.loads(line)
                heapq.heappush(heap, (_sort_key(entry), index, entry))
                budget.acquire(1, note + ':merge-head')
        output = BoundedList(budget, note + ':output')
        with open(output_path, 'w', encoding='utf-8') as out:
            while heap:
                _, index, entry = heapq.heappop(heap)
                budget.release(1)
                output.append(entry)
                line = handles[index].readline()
                if line:
                    nxt = json.loads(line)
                    heapq.heappush(heap, (_sort_key(nxt), index, nxt))
                    budget.acquire(1, note + ':merge-head')
                if len(output) >= page_size:
                    for item in output:
                        out.write(json.dumps(item, sort_keys=True) + '\n')
                    output.clear()
            for item in output:
                out.write(json.dumps(item, sort_keys=True) + '\n')
            output.clear()
    finally:
        for handle in handles:
            handle.close()


def _flush_chunk(chunk, run_path, chunk_index):
    path = '{0}.chunk-{1:04d}'.format(run_path, chunk_index)
    ordered = sorted(chunk, key=_sort_key)
    with open(path, 'w', encoding='utf-8') as fh:
        for entry in ordered:
            fh.write(json.dumps(entry, sort_keys=True) + '\n')
    return path


class RunMerge(object):
    """Streaming k-way merge of the run prefix visible to one version.

    Yields resolved live captures as ``(timestamp, memento_url, sources)``,
    newest first. Retracted mementos are dropped; per-key source attribution
    is reconstructed by replaying add/retract ops in journal sequence order.
    Memory used: one head per run plus the tiny state of one key group.
    """

    def __init__(self, store, version, budget):
        self.store = store
        self.version = version
        self.budget = budget
        self._heads = 0
        self.run_paths = [
            os.path.join(store.runs_dir, name)
            for name in sorted(os.listdir(store.runs_dir))
            if name.endswith('.jsonl')
        ]

    def __iter__(self):
        if not self.run_paths:
            return iter(())
        handles = [open(path, encoding='utf-8') for path in self.run_paths]
        heap = []
        try:
            for run_index, handle in enumerate(handles):
                line = handle.readline()
                if line:
                    entry = json.loads(line)
                    heapq.heappush(
                        heap, (_sort_key(entry), run_index, entry))
                    self._heads += 1
                    self.budget.acquire(1, 'run-merge:head')

            def next_entry(run_index):
                line = handles[run_index].readline()
                if not line:
                    return None
                entry = json.loads(line)
                heapq.heappush(heap, (_sort_key(entry), run_index, entry))
                self.budget.acquire(1, 'run-merge:head')
                return entry

            while heap:
                key, run_index, first = heapq.heappop(heap)
                self.budget.release(1)
                self._heads -= 1
                timestamp = first['timestamp']
                memento_url = first['memento_url']
                sources = {}
                self._apply(first, sources)
                next_entry(run_index)

                # drain every op belonging to the same capture key
                while heap and heap[0][0][0] == -_ts_int(timestamp) and \
                        heap[0][0][1] == memento_url:
                    _, run_index, entry = heapq.heappop(heap)
                    self.budget.release(1)
                    self._heads -= 1
                    self._apply(entry, sources)
                    next_entry(run_index)

                if sources:
                    yield timestamp, memento_url, dict(sources)
        finally:
            for handle in handles:
                handle.close()

    @staticmethod
    def _apply(entry, sources):
        op = entry['op']
        source = entry['source']
        if op == 'add':
            sources[source] = {
                'round': entry.get('round'),
                'page_index': entry.get('page_index'),
            }
        else:
            # 'retract' and 'retract-source': this source no longer vouches
            # for the memento in the reconstructed version
            sources.pop(source, None)
