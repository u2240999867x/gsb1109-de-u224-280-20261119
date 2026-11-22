"""Deterministic clock used by the store-collection timer regressions.

The production timer code reads the wall clock exclusively through
``datetime.now`` inside :mod:`SpiffWorkflow.bpmn.specs.event_definitions.timer`.
Patching that one reference with :class:`ControlledClock` makes every timer
decision reproducible without sleeping or depending on the real date.
"""

from datetime import datetime
from unittest import mock

from SpiffWorkflow.bpmn.specs.event_definitions import timer as timer_module


class ControlledDateTime(datetime):
    """A ``datetime`` subclass whose ``now`` reads a shared, mutable instant."""

    clock = None

    @classmethod
    def now(cls, tz=None):
        instant = cls.clock.current
        if tz is None:
            return instant
        return instant.astimezone(tz)


class ControlledClock:
    """Holds the single virtual "now" observed by the timer event definitions."""

    def __init__(self, start):
        self.current = start
        ControlledDateTime.clock = self
        self._patch = mock.patch.object(timer_module, 'datetime', ControlledDateTime)

    def start(self):
        self._patch.start()
        return self

    def stop(self):
        self._patch.stop()

    def advance_to(self, instant):
        self.current = instant

    def advance_by(self, delta):
        self.current = self.current + delta

    def __enter__(self):
        return self.start()

    def __exit__(self, exc_type, exc, traceback):
        self.stop()
