"""Calendar oracle for the store-collection schedule.

All times are planned in ``America/New_York`` local time and converted to the
absolute UTC instants the engine compares against.  The module deliberately has
no engine dependency so the same oracle drives both the workflow tests and the
pure schedule-discrimination tests.
"""

from calendar import monthrange
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

NEW_YORK = ZoneInfo('America/New_York')
UTC = timezone.utc

PLAN_YEAR = 2026
WINDOW_HOUR = 1
WINDOW_MINUTE = 30
FIRST_FOUR_MONTHS = (1, 2, 3, 4)

# The required local month-end dates for the first four 2026 windows.
EXPECTED_LOCAL_DATES = {
    1: (2026, 1, 31),
    2: (2026, 2, 28),
    3: (2026, 3, 31),
    4: (2026, 4, 30),
}

RECONCILE_MONTH = 3


def month_end_instant(year, month, hour=WINDOW_HOUR, minute=WINDOW_MINUTE):
    """Return the UTC instant for the local month-end at 01:30 New York time."""
    last_day = monthrange(year, month)[1]
    local = datetime(year, month, last_day, hour, minute, tzinfo=NEW_YORK)
    return local.astimezone(UTC)


def window_label(year, month):
    return f'{year:04d}-{month:02d}'


def first_four_window_instants():
    return [month_end_instant(PLAN_YEAR, month) for month in FIRST_FOUR_MONTHS]


def reconcile_instant():
    return month_end_instant(PLAN_YEAR, RECONCILE_MONTH)


@dataclass(frozen=True)
class PlannedWindow:
    index: int
    label: str
    due: datetime

    @property
    def local_date(self):
        return self.due.astimezone(NEW_YORK).date()


def planned_windows(months=FIRST_FOUR_MONTHS):
    return [
        PlannedWindow(index=idx, label=window_label(PLAN_YEAR, month), due=month_end_instant(PLAN_YEAR, month))
        for idx, month in enumerate(months, start=1)
    ]


class CalendarMonthSchedule:
    """Correct rule: each window lands on the local last day at 01:30."""

    @staticmethod
    def next_window(previous=None, index=1):
        month = ((index - 1) % 12) + 1
        year = PLAN_YEAR + (index - 1) // 12
        label = window_label(year, month)
        return PlannedWindow(index=index, label=label, due=month_end_instant(year, month))


class FirstMonthLengthSchedule:
    """Fault-A double: every interval reuses the first (January) month length.

    This mirrors the native ``P1M`` recurring-interval behaviour, which converts
    a month duration once into a fixed number of seconds and then adds that same
    delta forever.
    """

    def __init__(self):
        first = CalendarMonthSchedule.next_window(index=1)
        self._fixed_delta = timedelta(days=monthrange(PLAN_YEAR, 1)[1])
        self._previous = first.due

    def next_window(self, previous=None, index=1):
        if index == 1:
            return CalendarMonthSchedule.next_window(index=1)
        due = self._previous + self._fixed_delta
        self._previous = due
        label = window_label(due.year, due.month)
        return PlannedWindow(index=index, label=label, due=due)


def first_diverging_window(produced, expected):
    """Return the first index whose planned instant differs, else ``None``."""
    for produced_window, expected_window in zip(produced, expected):
        if produced_window.due != expected_window.due or produced_window.label != expected_window.label:
            return expected_window.index
    if len(produced) != len(expected):
        return min(len(produced), len(expected)) + 1
    return None
