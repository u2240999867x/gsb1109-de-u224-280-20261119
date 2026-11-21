"""Passive observation of a record-by-record Flux response consumption."""

from typing import Optional


class FluxProgress:
    """
    Snapshot of how much of a Flux response has been delivered to the consumer.

    The values describe only data that has already been handed to the iterator
    caller. Nothing is counted before the consumer has actually received it.
    """

    __slots__ = ('records_delivered', 'tables_started', 'tables_completed',
                 'table_in_progress', 'last_table')

    def __init__(self, records_delivered: int = 0, tables_started: int = 0, tables_completed: int = 0,
                 table_in_progress: bool = False, last_table: Optional[object] = None) -> None:
        """
        Initialize progress snapshot.

        :param records_delivered: number of business records already yielded to the caller
        :param tables_started: number of business tables observed so far
        :param tables_completed: number of business tables whose end boundary was confirmed
        :param table_in_progress: whether the last started business table is still being read
        :param last_table: the last started business table, ``None`` before any table appears
        """
        self.records_delivered = records_delivered
        self.tables_started = tables_started
        self.tables_completed = tables_completed
        self.table_in_progress = table_in_progress
        self.last_table = last_table

    def __repr__(self):
        """Format for inspection."""
        return (f"<{type(self).__name__}: records_delivered={self.records_delivered}, "
                f"tables_started={self.tables_started}, tables_completed={self.tables_completed}, "
                f"table_in_progress={self.table_in_progress}>")


class FluxProgressObserver(object):
    """
    Consume progress notifications from a Flux response parser.

    Each method is invoked only after the corresponding event is real for the
    iterator caller. Implementations may override any of them; the default
    methods do nothing. The latest snapshot is always available through
    :attr:`progress`, so a paused stream can be inspected without reading
    further records.

    Observer exceptions never interrupt the business iteration or replace the
    parser's own errors.
    """

    def __init__(self) -> None:
        """Initialize an empty progress snapshot."""
        self._progress = FluxProgress()

    @property
    def progress(self) -> FluxProgress:
        """Return the latest snapshot reported by the parser."""
        return self._progress

    @property
    def records_delivered(self) -> int:
        """Return the number of business records already handed to the caller."""
        return self._progress.records_delivered

    @property
    def tables_completed(self) -> int:
        """Return the number of business tables whose end boundary was confirmed."""
        return self._progress.tables_completed

    @property
    def table_in_progress(self) -> bool:
        """Return whether the last started business table is still being read."""
        return self._progress.table_in_progress

    def _set_progress(self, progress: FluxProgress) -> None:
        self._progress = progress

    def on_record(self, record, progress: FluxProgress) -> None:
        """A business record has been handed to the caller."""

    def on_table_started(self, table, progress: FluxProgress) -> None:
        """A new business table was observed."""

    def on_table_completed(self, table, progress: FluxProgress) -> None:
        """A business table reached a confirmed end boundary."""

    def on_complete(self, progress: FluxProgress) -> None:
        """The response reached a real EOF."""
