"""Explicit two-page memory budget.

The merger and the readers are only allowed to hold two request pages of
capture data in memory at a time. Every sizable buffer in this package is
allocated through :class:`BoundedList`, which reports its live item count to a
shared :class:`MemoryBudget`. Tests read ``budget.peak_items`` to prove that no
code path ever held the whole timeline.
"""

import threading


class MemoryBudget(object):
    def __init__(self, page_size):
        self.page_size = page_size
        self.current_items = 0
        self.peak_items = 0
        self.peak_note = ''
        self._lock = threading.RLock()

    def acquire(self, count, note=''):
        with self._lock:
            self.current_items += count
            if self.current_items > self.peak_items:
                self.peak_items = self.current_items
                self.peak_note = note

    def release(self, count):
        with self._lock:
            self.current_items -= count

    def limit_for(self, run_count):
        # two full request pages, plus one merge head per on-disk run
        return 2 * self.page_size + max(run_count, 0)


class BoundedList(object):
    """List-like buffer whose size counts against a :class:`MemoryBudget`."""

    def __init__(self, budget, note='bounded-list'):
        self._items = []
        self._budget = budget
        self._note = note

    def append(self, item):
        self._items.append(item)
        self._budget.acquire(1, self._note)

    def extend(self, items):
        items = list(items)
        self._items.extend(items)
        self._budget.acquire(len(items), self._note)

    def pop(self):
        item = self._items.pop()
        self._budget.release(1)
        return item

    def clear(self):
        count = len(self._items)
        self._items = []
        if count:
            self._budget.release(count)

    def __len__(self):
        return len(self._items)

    def __iter__(self):
        return iter(self._items)

    def __getitem__(self, index):
        return self._items[index]

    def __setitem__(self, index, value):
        self._items[index] = value

    def __delitem__(self, index):
        del self._items[index]
        self._budget.release(1)

    def __bool__(self):
        return bool(self._items)

    def take_items(self):
        # ownership transfer: hand the underlying list to the caller without
        # allocating a second copy; the budget stops counting it here
        items = self._items
        self._items = []
        self._budget.release(len(items))
        return items

    def release_all(self):
        self.clear()
