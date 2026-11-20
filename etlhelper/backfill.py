"""
Resumable backfill of append-only source events.

This module is deliberately separate from the existing ``copy_rows``,
``copy_table_rows`` and ``executemany`` entry points.  Those functions keep
their original behaviour.  A backfill run uses explicit destination-side state:

* ``run``      - the frozen event-number window for each run id
* ``coverage`` - each block that was read and committed
* ``reject``   - each event that the destination refused

A block of archive rows, its reject records and its coverage marker are
committed in one destination transaction.  Coverage therefore means "this
block was read and its outcome was committed", not "the largest archived id
happens to be N".  This distinction is what lets a restart tell a missed read
apart from an event that was read and rejected.
"""
from __future__ import annotations

import logging
from collections import deque, namedtuple
from collections.abc import (
    Callable,
    Iterable,
    Mapping,
    Sequence,
)
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, NamedTuple, Optional, Union

from etlhelper.db_helper_factory import DB_HELPER_FACTORY
from etlhelper.etl import generate_insert_sql, validate_identifier
from etlhelper.exceptions import (
    ETLHelperBackfillError,
    ETLHelperCommitUnconfirmedError,
    ETLHelperInsertError,
    ETLHelperQueryError,
)
from etlhelper.types import (
    Chunk,
    Connection,
    Transform,
)

logger = logging.getLogger('etlhelper')
CHUNKSIZE = 5000

DEFAULT_RUN_TABLE = 'etlhelper_backfill_run'
DEFAULT_COVERAGE_TABLE = 'etlhelper_backfill_coverage'
DEFAULT_REJECT_TABLE = 'etlhelper_backfill_reject'

RUNNING = 'running'
COMPLETE = 'complete'


@dataclass(frozen=True, order=True)
class EventRange:
    """Inclusive range of stable source event numbers."""

    start: int
    end: int

    def __post_init__(self) -> None:
        if self.start > self.end:
            raise ValueError(f'Invalid event range: {self.start} > {self.end}')

    def __contains__(self, event_id: int) -> bool:
        return self.start <= event_id <= self.end


@dataclass(frozen=True)
class BackfillTables:
    """Names of the destination tables that hold backfill state."""

    run: str = DEFAULT_RUN_TABLE
    coverage: str = DEFAULT_COVERAGE_TABLE
    reject: str = DEFAULT_REJECT_TABLE

    def __post_init__(self) -> None:
        for table in (self.run, self.coverage, self.reject):
            validate_identifier(table)


@dataclass
class BackfillResult:
    """
    Result of one :func:`backfill` invocation.

    ``coverage`` is the contiguous range confirmed by explicit coverage rows.
    ``unconfirmed`` is set only when the destination could not be asked
    whether a commit succeeded and ``raise_on_unconfirmed`` was false.
    """

    run_id: str
    window: EventRange
    coverage: Optional[EventRange] = None
    accepted: int = 0
    rejected: int = 0
    processed: int = 0
    skipped_blocks: int = 0
    rejected_event_ids: tuple[int, ...] = ()
    complete: bool = False
    unconfirmed: Optional[EventRange] = None
    stopped_reason: Optional[str] = None
    blocks_committed: list[EventRange] = field(default_factory=list)

    @property
    def next_event_id(self) -> Optional[int]:
        """First event number not yet covered, or None when complete."""
        if self.coverage is None:
            return self.window.start
        if self.coverage.end >= self.window.end:
            return None
        return self.coverage.end + 1


class _RunRecord(NamedTuple):
    run_id: str
    window_start: int
    window_end: int
    status: str


class _CoverageRecord(NamedTuple):
    block_start: int
    block_end: int
    accepted_count: int
    rejected_count: int


class _BlockOutcome(NamedTuple):
    committed: bool
    accepted: int
    rejected: int
    needs_reconnect: bool = False
    new_conn: Optional[Connection] = None


def create_backfill_tables(
        conn: Connection,
        tables: Optional[BackfillTables] = None,
        ) -> None:
    """Create the run, coverage and reject tables in ``conn`` if absent."""
    BackfillState(conn, tables).ensure_tables()


def backfill(
        run_id: str,
        source_conn: Connection,
        dest_conn: Connection,
        *,
        start_event_id: int,
        end_event_id: int,
        source_table: Optional[str] = None,
        dest_table: Optional[str] = None,
        event_column: str = 'event_id',
        tables: Optional[BackfillTables] = None,
        blocks: Optional[Iterable[Union[EventRange, Sequence[int]]]] = None,
        chunk_size: int = CHUNKSIZE,
        select_query: Optional[str] = None,
        insert_query: Optional[str] = None,
        transform: Optional[Transform] = None,
        create_tables: bool = True,
        confirm_commit: Optional[Callable[[str, EventRange], Optional[bool]]] = None,
        dest_reconnect: Optional[Callable[[], Connection]] = None,
        raise_on_unconfirmed: bool = True,
        ) -> BackfillResult:
    """
    Backfill an append-only event range using destination-confirmed progress.

    The event window is frozen when ``run_id`` is first seen.  Re-running the
    same ``run_id`` continues after the last explicitly covered block; events
    that arrived in the source after the window was frozen (for example event
    46 in a 41-45 window) are not included.

    Rows rejected by the destination are isolated by a row-by-row trial before
    the block is committed.  Accepted rows, reject records and the coverage
    marker share one transaction, so a committed block never needs to be
    rewritten.

    :param run_id: stable identifier for this backfill window
    :param source_conn: connection containing the append-only readings
    :param dest_conn: connection containing the archive and state tables
    :param start_event_id: first event number in the frozen window
    :param end_event_id: last event number in the frozen window, inclusive
    :param source_table: source table name; used to build a default SELECT
    :param dest_table: target table name; used to build a default INSERT
    :param event_column: source column containing the stable event number
    :param tables: optional names for the run/coverage/reject tables
    :param blocks: optional explicit, contiguous partition of the window.
                   Each block is committed as one unit.  Without ``blocks`` the
                   whole remaining window is one block, fetched with
                   ``chunk_size`` rows at a time.
    :param chunk_size: fetch size used while reading the source
    :param select_query: custom SELECT accepting two positional parameters
                         (block start, block end), inclusive.  It must return
                         ``event_column``.
    :param insert_query: custom INSERT; parameters are taken from each row.
    :param transform: optional transform applied to each block before insert
    :param create_tables: create missing state tables automatically
    :param confirm_commit: callback ``(run_id, block) -> True/False/None``
                           used after a lost commit acknowledgement.  ``True``
                           means the block is present, ``False`` means it is
                           absent, and ``None`` means it cannot be determined.
    :param dest_reconnect: optional factory returning a fresh destination
                           connection, used to verify a lost acknowledgement
                           after the original connection died
    :param raise_on_unconfirmed: if true (default), raise
                                 :class:`ETLHelperCommitUnconfirmedError` when
                                 the commit outcome cannot be determined.
                                 If false, return a result with
                                 ``unconfirmed`` set instead.
    :return: :class:`BackfillResult`
    """
    window = EventRange(int(start_event_id), int(end_event_id))
    if not run_id:
        raise ETLHelperBackfillError('run_id must not be empty')
    _validate_request(source_table, dest_table, select_query, insert_query,
                      event_column, chunk_size)

    state = BackfillState(dest_conn, tables)

    try:
        if create_tables:
            state.ensure_tables()
    except Exception as exc:
        # No archive rows have been written at this point.  The journal is a
        # prerequisite, so fail plainly rather than reporting an unconfirmed
        # commit range.
        raise ETLHelperBackfillError(
            f'Could not prepare backfill state tables: {exc}'
        ) from exc

    try:
        state.start_run(run_id, window)
        boundary = state.confirmed_boundary(run_id, window.start, window.end)
    except ETLHelperBackfillError:
        raise
    except Exception as exc:
        # Without readable coverage, a committed block is indistinguishable
        # from an unwritten one.  Never fall back to rewriting it.
        _handle_unconfirmed(
            run_id, window, exc, raise_on_unconfirmed, 'coverage_unavailable'
        )
        return BackfillResult(
            run_id=run_id, window=window, unconfirmed=window,
            stopped_reason='coverage_unavailable',
        )

    block_ranges = _normalise_blocks(blocks, window)
    result = BackfillResult(
        run_id=run_id,
        window=window,
        coverage=_coverage_range(window, boundary),
        rejected_event_ids=state.rejected_event_ids(run_id),
    )
    if boundary == window.end:
        _finish_run(state, run_id)
        result.complete = True
        return result

    source_sql = select_query or _default_select_sql(source_table, event_column)
    explicit_blocks = block_ranges is not None
    pending_blocks = deque(block_ranges or [])

    while boundary != window.end:
        if explicit_blocks:
            if not pending_blocks:
                break
            requested_block = _clip_block(pending_blocks.popleft(), boundary,
                                          window)
            if requested_block is None:
                result.skipped_blocks += 1
                continue
            block = requested_block
        else:
            current = window.start if boundary is None else boundary + 1
            block = EventRange(current, window.end)

        try:
            rows = _fetch_rows(
                source_conn,
                source_sql,
                _source_parameters(source_conn, block),
                limit=None if explicit_blocks else chunk_size,
            )
        except Exception as exc:
            result.stopped_reason = f'source_read_failed: {exc}'
            result.coverage = _coverage_range(window, boundary)
            result.rejected_event_ids = state.rejected_event_ids(run_id)
            return result

        rows, event_ids, block, gap_after = _contiguous_prefix(
            rows, event_column, block
        )
        if block is None:
            # The event at the expected boundary is absent.  A missing event
            # is a missed read, not a processed reject, so coverage stops
            # here instead of being advanced over the gap.
            result.stopped_reason = 'missing_event_at_boundary'
            result.coverage = _coverage_range(window, boundary)
            result.rejected_event_ids = state.rejected_event_ids(run_id)
            return result

        if gap_after is not None and explicit_blocks:
            # The rest of this explicit block has not been read yet.
            pending_blocks.appendleft(gap_after)

        try:
            outcome = _commit_block(
                run_id=run_id,
                dest_conn=dest_conn,
                state=state,
                block=block,
                rows=rows,
                event_ids=event_ids,
                event_column=event_column,
                dest_table=dest_table,
                insert_query=insert_query,
                transform=transform,
                confirm_commit=confirm_commit,
                dest_reconnect=dest_reconnect,
                raise_on_unconfirmed=raise_on_unconfirmed,
            )
        except ETLHelperCommitUnconfirmedError:
            raise
        except Exception as exc:
            result.stopped_reason = f'block_write_failed: {exc}'
            result.coverage = _coverage_range(window, boundary)
            result.rejected_event_ids = state.rejected_event_ids(run_id)
            return result

        if outcome is None:
            result.coverage = _coverage_range(window, boundary)
            result.unconfirmed = block
            result.rejected_event_ids = _try_rejected_ids(state, run_id,
                                                          result.rejected_event_ids)
            return result

        if not outcome.committed:
            result.stopped_reason = 'commit_not_applied'
            result.coverage = _coverage_range(window, boundary)
            result.rejected_event_ids = _try_rejected_ids(state, run_id,
                                                          result.rejected_event_ids)
            return result

        result.accepted += outcome.accepted
        result.rejected += outcome.rejected
        result.processed += outcome.accepted + outcome.rejected
        result.blocks_committed.append(block)
        boundary = block.end
        result.coverage = _coverage_range(window, boundary)

        if outcome.needs_reconnect:
            if outcome.new_conn is not None:
                dest_conn = outcome.new_conn
                state = BackfillState(dest_conn, tables)
            elif dest_reconnect is None:
                # The block is committed, but this connection is unusable.
                # Do not turn a status read failure into a second failure.
                result.stopped_reason = 'connection_lost_after_commit'
                result.rejected_event_ids = _try_rejected_ids(
                    state, run_id, result.rejected_event_ids
                )
                return result
            else:
                dest_conn = dest_reconnect()
                state = BackfillState(dest_conn, tables)
                state.ensure_tables()

        result.rejected_event_ids = _try_rejected_ids(
            state, run_id, result.rejected_event_ids
        )

    if boundary == window.end:
        _finish_run(state, run_id)
        result.complete = True
    result.coverage = _coverage_range(window, boundary)
    result.rejected_event_ids = _try_rejected_ids(state, run_id,
                                                  result.rejected_event_ids)
    return result


def copy_rows_resumable(
        run_id: str,
        source_conn: Connection,
        dest_conn: Connection,
        *,
        start_event_id: int,
        end_event_id: int,
        **kwargs: Any,
        ) -> BackfillResult:
    """Alias for :func:`backfill`, named after the established copy API."""
    return backfill(
        run_id,
        source_conn,
        dest_conn,
        start_event_id=start_event_id,
        end_event_id=end_event_id,
        **kwargs,
    )


resumable_copy = copy_rows_resumable


class BackfillState:
    """Read and write the destination-side backfill journal."""

    def __init__(self, conn: Connection, tables: Optional[BackfillTables] = None):
        self.conn = conn
        self.tables = tables or BackfillTables()
        self.helper = DB_HELPER_FACTORY.from_conn(conn)

    # -- schema ----------------------------------------------------------

    def ensure_tables(self) -> None:
        """Create missing run, coverage and reject tables."""
        for sql in self._create_table_sql():
            self._execute_ddl(sql)

    def _create_table_sql(self) -> list[str]:
        dialect = self.helper.__class__.__name__
        run = self.tables.run
        coverage = self.tables.coverage
        reject = self.tables.reject

        if dialect == 'OracleDbHelper':
            return [
                _oracle_create_sql(
                    run,
                    'run_id VARCHAR2(4000) PRIMARY KEY, '
                    'window_start NUMBER(19) NOT NULL, '
                    'window_end NUMBER(19) NOT NULL, '
                    'status VARCHAR2(4000) NOT NULL, '
                    'created_at VARCHAR2(4000) NOT NULL, '
                    'updated_at VARCHAR2(4000) NOT NULL',
                ),
                _oracle_create_sql(
                    coverage,
                    'run_id VARCHAR2(4000) NOT NULL, '
                    'block_start NUMBER(19) NOT NULL, '
                    'block_end NUMBER(19) NOT NULL, '
                    'accepted_count NUMBER(19) NOT NULL, '
                    'rejected_count NUMBER(19) NOT NULL, '
                    'committed_at VARCHAR2(4000) NOT NULL, '
                    'PRIMARY KEY (run_id, block_start)',
                ),
                _oracle_create_sql(
                    reject,
                    'run_id VARCHAR2(4000) NOT NULL, '
                    'event_id NUMBER(19) NOT NULL, '
                    'error VARCHAR2(4000), '
                    'rejected_at VARCHAR2(4000) NOT NULL, '
                    'PRIMARY KEY (run_id, event_id)',
                ),
            ]

        if dialect == 'MSSQLDbHelper':
            return [
                _mssql_create_sql(
                    run,
                    'run_id NVARCHAR(4000) PRIMARY KEY, '
                    'window_start BIGINT NOT NULL, '
                    'window_end BIGINT NOT NULL, '
                    'status NVARCHAR(4000) NOT NULL, '
                    'created_at NVARCHAR(4000) NOT NULL, '
                    'updated_at NVARCHAR(4000) NOT NULL',
                ),
                _mssql_create_sql(
                    coverage,
                    'run_id NVARCHAR(4000) NOT NULL, '
                    'block_start BIGINT NOT NULL, '
                    'block_end BIGINT NOT NULL, '
                    'accepted_count BIGINT NOT NULL, '
                    'rejected_count BIGINT NOT NULL, '
                    'committed_at NVARCHAR(4000) NOT NULL, '
                    'PRIMARY KEY (run_id, block_start)',
                ),
                _mssql_create_sql(
                    reject,
                    'run_id NVARCHAR(4000) NOT NULL, '
                    'event_id BIGINT NOT NULL, '
                    'error NVARCHAR(4000), '
                    'rejected_at NVARCHAR(4000) NOT NULL, '
                    'PRIMARY KEY (run_id, event_id)',
                ),
            ]

        # SQLite and PostgreSQL.
        return [
            f"""CREATE TABLE IF NOT EXISTS {run} (
                    run_id TEXT PRIMARY KEY,
                    window_start BIGINT NOT NULL,
                    window_end BIGINT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )""",
            f"""CREATE TABLE IF NOT EXISTS {coverage} (
                    run_id TEXT NOT NULL,
                    block_start BIGINT NOT NULL,
                    block_end BIGINT NOT NULL,
                    accepted_count BIGINT NOT NULL,
                    rejected_count BIGINT NOT NULL,
                    committed_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, block_start)
                )""",
            f"""CREATE TABLE IF NOT EXISTS {reject} (
                    run_id TEXT NOT NULL,
                    event_id BIGINT NOT NULL,
                    error TEXT,
                    rejected_at TEXT NOT NULL,
                    PRIMARY KEY (run_id, event_id)
                )""",
        ]

    # -- query helpers ---------------------------------------------------

    def _execute_ddl(self, sql: str) -> None:
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql)
            self.conn.commit()
        except self.helper.sql_exceptions as exc:
            _safe_rollback(self.conn)
            raise ETLHelperBackfillError(
                f'Could not create backfill state table: {exc}'
            ) from exc
        finally:
            cursor.close()

    def _query(
            self,
            sql: str,
            parameters: Sequence[Any] = (),
            ) -> list[tuple]:
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql, parameters)
            return list(cursor.fetchall())
        except self.helper.sql_exceptions:
            _safe_rollback(self.conn)
            raise
        finally:
            cursor.close()

    def _execute(self, sql: str, parameters: Sequence[Any] = ()) -> None:
        cursor = self.conn.cursor()
        try:
            cursor.execute(sql, parameters)
            self.conn.commit()
        except self.helper.sql_exceptions:
            _safe_rollback(self.conn)
            raise
        finally:
            cursor.close()

    def _placeholder_list(self, count: int, start: int = 0) -> list[str]:
        if self.helper.positional_paramstyle == 'numeric':
            return [f':{i}' for i in range(start + 1, start + count + 1)]
        return _positional_placeholders(self.helper, count)

    def _placeholders(self, count: int) -> str:
        return ', '.join(self._placeholder_list(count))

    # -- run lifecycle ---------------------------------------------------

    def get_run(self, run_id: str) -> Optional[_RunRecord]:
        sql = (
            f'SELECT run_id, window_start, window_end, status '
            f'FROM {self.tables.run} '
            f'WHERE run_id = {self._placeholders(1)}'
        )
        rows = self._query(sql, (run_id,))
        if not rows:
            return None
        return _RunRecord(*rows[0])

    def start_run(self, run_id: str, window: EventRange) -> _RunRecord:
        existing = self.get_run(run_id)
        if existing is None:
            now = _utcnow()
            sql = (
                f'INSERT INTO {self.tables.run} '
                f'(run_id, window_start, window_end, status, created_at, '
                f'updated_at) VALUES ({self._placeholders(6)})'
            )
            self._execute(sql, (run_id, window.start, window.end, RUNNING,
                                now, now))
            return _RunRecord(run_id, window.start, window.end, RUNNING)

        existing_window = EventRange(existing.window_start, existing.window_end)
        if existing_window != window:
            raise ETLHelperBackfillError(
                f'Run {run_id!r} was started for window '
                f'{existing.window_start}-{existing.window_end}, not '
                f'{window.start}-{window.end}.  Use a new run_id for a new '
                'window.'
            )
        return existing

    def finish_run(self, run_id: str) -> None:
        status, updated_at, run_id_param = self._placeholder_list(3)
        sql = (
            f'UPDATE {self.tables.run} '
            f'SET status = {status}, updated_at = {updated_at} '
            f'WHERE run_id = {run_id_param}'
        )
        self._execute(sql, (COMPLETE, _utcnow(), run_id))

    # -- coverage reads --------------------------------------------------

    def coverage_rows(self, run_id: str) -> list[_CoverageRecord]:
        sql = (
            f'SELECT block_start, block_end, accepted_count, rejected_count '
            f'FROM {self.tables.coverage} '
            f'WHERE run_id = {self._placeholders(1)} '
            f'ORDER BY block_start'
        )
        return [_CoverageRecord(*row) for row in self._query(sql, (run_id,))]

    def confirmed_boundary(
            self,
            run_id: str,
            window_start: int,
            window_end: int,
            ) -> Optional[int]:
        """
        Return the end of the contiguous coverage starting at window_start.

        A hole stops the boundary before the hole.  Maximum event id and
        maximum coverage id are never used as substitutes for contiguity.
        """
        boundary = window_start - 1
        for record in self.coverage_rows(run_id):
            if record.block_start > boundary + 1:
                break
            if record.block_end > boundary:
                boundary = record.block_end
        if boundary < window_start:
            return None
        return min(boundary, window_end)

    def rejected_event_ids(self, run_id: str) -> tuple[int, ...]:
        sql = (
            f'SELECT event_id FROM {self.tables.reject} '
            f'WHERE run_id = {self._placeholders(1)} ORDER BY event_id'
        )
        return tuple(int(row[0]) for row in self._query(sql, (run_id,)))

    def block_committed(self, run_id: str, block: EventRange) -> bool:
        run_id_param, block_start_param = self._placeholder_list(2)
        sql = (
            f'SELECT block_start, block_end FROM {self.tables.coverage} '
            f'WHERE run_id = {run_id_param} '
            f'AND block_start = {block_start_param}'
        )
        rows = self._query(sql, (run_id, block.start))
        if not rows:
            return False
        return int(rows[0][1]) >= block.end

    # -- transactional writes --------------------------------------------

    def insert_rejects(
            self,
            cursor,
            run_id: str,
            rejected: Sequence[tuple[int, str]],
            ) -> None:
        if not rejected:
            return
        sql = (
            f'INSERT INTO {self.tables.reject} '
            f'(run_id, event_id, error, rejected_at) '
            f'VALUES ({self._placeholders(4)})'
        )
        now = _utcnow()
        cursor.executemany(sql, [(run_id, event_id, error, now)
                                 for event_id, error in rejected])

    def insert_coverage(
            self,
            cursor,
            run_id: str,
            block: EventRange,
            accepted_count: int,
            rejected_count: int,
            ) -> None:
        sql = (
            f'INSERT INTO {self.tables.coverage} '
            f'(run_id, block_start, block_end, accepted_count, '
            f'rejected_count, committed_at) '
            f'VALUES ({self._placeholders(6)})'
        )
        cursor.execute(sql, (run_id, block.start, block.end, accepted_count,
                             rejected_count, _utcnow()))


def _commit_block(
        run_id: str,
        dest_conn: Connection,
        state: BackfillState,
        block: EventRange,
        rows: Chunk,
        event_ids: Sequence[int],
        event_column: str,
        dest_table: Optional[str],
        insert_query: Optional[str],
        transform: Optional[Transform],
        confirm_commit: Optional[Callable[[str, EventRange], Optional[bool]]],
        dest_reconnect: Optional[Callable[[], Connection]],
        raise_on_unconfirmed: bool,
        ) -> Optional[_BlockOutcome]:
    """Write one block and its journal entries, then commit."""
    input_rows = list(rows)
    if transform and input_rows:
        source_ids = list(event_ids)
        input_rows = list(transform(input_rows))
        event_ids = [_event_id(row, event_column) for row in input_rows]
        if sorted(event_ids) != sorted(source_ids):
            raise ETLHelperBackfillError(
                'transform must preserve the event ids in the block; '
                f'expected {sorted(source_ids)}, got {sorted(event_ids)}'
            )

    if not input_rows:
        accepted: Chunk = []
        rejected: list[tuple[int, str]] = []
    else:
        helper = DB_HELPER_FACTORY.from_conn(dest_conn)
        query = insert_query
        if query is None:
            table = dest_table
            if table is None:
                raise ETLHelperBackfillError(
                    'dest_table or insert_query is required'
                )
            input_rows = _rows_for_paramstyle(input_rows, helper)
            query = generate_insert_sql(table, input_rows[0], dest_conn)
        elif helper.named_paramstyle is None:
            # Drivers such as pyodbc have no named style; positional rows are
            # needed even when the caller supplied the SQL.
            input_rows = _rows_for_paramstyle(input_rows, helper)
        accepted, rejected = _insert_rows_with_rejects(
            dest_conn, helper, query, input_rows, event_ids, block
        )

    cursor = dest_conn.cursor()
    try:
        state.insert_rejects(cursor, run_id, rejected)
        state.insert_coverage(cursor, run_id, block, len(accepted),
                              len(rejected))
    except Exception:
        _safe_rollback(dest_conn)
        cursor.close()
        raise
    else:
        try:
            dest_conn.commit()
        except Exception as exc:
            cursor.close()
            confirmed, new_conn = _confirm_commit(
                confirm_commit, state, run_id, block, dest_conn, dest_reconnect
            )
            if confirmed is True:
                logger.warning(
                    'Commit acknowledgement lost for %s events %s-%s, but '
                    'the coverage marker is present; the block is committed.',
                    run_id, block.start, block.end,
                )
                return _BlockOutcome(
                    True, len(accepted), len(rejected),
                    needs_reconnect=True,
                    new_conn=new_conn,
                )
            if confirmed is False:
                logger.warning(
                    'Commit for %s events %s-%s was not applied; the block '
                    'will be read again on the next invocation.',
                    run_id, block.start, block.end,
                )
                _safe_rollback(dest_conn)
                return _BlockOutcome(False, 0, 0)
            _handle_unconfirmed(
                run_id, block, exc, raise_on_unconfirmed, 'commit_unconfirmed'
            )
            return None
        else:
            cursor.close()
            return _BlockOutcome(True, len(accepted), len(rejected))


def _rows_for_paramstyle(rows: Chunk, helper) -> Chunk:
    """
    Convert mapping rows to namedtuples for drivers without named parameters.

    The default fetch format is dictionaries, which work with the named
    parameter styles of PostgreSQL, SQLite and Oracle.  MSSQL/pyodbc only has
    positional placeholders, so mappings are converted to namedtuples, which
    ``generate_insert_sql`` renders positionally.
    """
    if helper.named_paramstyle is not None:
        return rows

    converted: Chunk = []
    for row in rows:
        if isinstance(row, Mapping):
            Row = namedtuple('Row', list(row.keys()))
            converted.append(Row(*row.values()))
        else:
            converted.append(row)
    return converted


def _insert_rows_with_rejects(
        dest_conn: Connection,
        helper,
        query: str,
        rows: Chunk,
        event_ids: Sequence[int],
        block: EventRange,
        ) -> tuple[Chunk, list[tuple[int, str]]]:
    """
    Insert rows and isolate destination rejections without committing.

    A bulk attempt is made first.  If it fails, every row is trialled in its
    own rolled-back transaction to identify the exact rejects.  Accepted rows
    are then inserted together in one open transaction, which the caller
    commits atomically with the reject records and coverage marker.

    Trialling a rejected row and rolling it back has no lasting effect, so a
    reject cannot be mistaken for an archived row.
    """
    if not rows:
        return [], []

    _safe_rollback(dest_conn)

    cursor = dest_conn.cursor()
    try:
        helper.executemany(cursor, query, rows)
    except helper.sql_exceptions as exc:
        _safe_rollback(dest_conn)
        if _is_connection_error(exc):
            raise
        bulk_error = exc
    else:
        return list(rows), []
    finally:
        cursor.close()

    rejected: list[tuple[int, str]] = []
    rejected_indices: set[int] = set()
    row_errors: list[Exception] = []

    cursor = dest_conn.cursor()
    try:
        for index, row in enumerate(rows):
            try:
                cursor.execute(query, row)
            except helper.sql_exceptions as exc:
                if _is_connection_error(exc):
                    raise
                rejected_indices.add(index)
                rejected.append((event_ids[index], _format_error(exc)))
                row_errors.append(exc)
            finally:
                # Trial inserts must not survive, whether accepted or not.
                _safe_rollback(dest_conn)
    finally:
        cursor.close()

    if not rejected_indices:
        # The bulk failure was not reproducible row by row.  Do not record a
        # bogus rejection; surface the original error instead.
        _safe_rollback(dest_conn)
        raise ETLHelperInsertError(
            f'Bulk insert for block {block.start}-{block.end} failed but no '
            f'individual row could be rejected: {bulk_error}'
        )

    accepted = [row for index, row in enumerate(rows)
                if index not in rejected_indices]

    if not accepted:
        _safe_rollback(dest_conn)
        if not _looks_like_row_rejection(row_errors):
            raise ETLHelperInsertError(
                f'All rows in block {block.start}-{block.end} failed with a '
                f'non-row error: {bulk_error}'
            )
        # A fully rejected block is still a processed block.  Its reject
        # records and coverage marker are committed by the caller.
        return [], rejected

    _safe_rollback(dest_conn)
    cursor = dest_conn.cursor()
    try:
        helper.executemany(cursor, query, accepted)
    except helper.sql_exceptions as exc:
        _safe_rollback(dest_conn)
        raise ETLHelperInsertError(
            f'Accepted rows for block {block.start}-{block.end} could not be '
            f'inserted after reject isolation: {exc}'
        ) from exc
    finally:
        cursor.close()

    return accepted, rejected


def _confirm_commit(
        callback: Optional[Callable[[str, EventRange], Optional[bool]]],
        state: BackfillState,
        run_id: str,
        block: EventRange,
        dest_conn: Connection,
        dest_reconnect: Optional[Callable[[], Connection]],
        ) -> tuple[Optional[bool], Optional[Connection]]:
    """
    Determine whether a commit landed.

    Returns ``(confirmed, new_connection)``.  ``confirmed`` is True/False, or
    None when neither the current connection nor a reconnect can read the
    coverage marker.
    """
    if callback is not None:
        try:
            value = callback(run_id, block)
        except Exception as exc:
            logger.warning('Commit confirmation callback failed: %s', exc)
            return None, None
        if value not in (True, False, None):
            raise ETLHelperBackfillError(
                'confirm_commit must return True, False or None'
            )
        return value, None

    # Discard the failed transaction before reading.  Without this, an open
    # transaction on the same connection would make uncommitted coverage rows
    # look committed.  A successful lost-acknowledgement commit is unaffected
    # by rollback.
    _safe_rollback(dest_conn)
    try:
        return state.block_committed(run_id, block), None
    except Exception as exc:
        logger.warning('Could not confirm commit on existing connection: %s', exc)

    if dest_reconnect is not None:
        try:
            new_conn = dest_reconnect()
        except Exception as exc:
            logger.warning('Could not reconnect to confirm commit: %s', exc)
            return None, None
        try:
            new_state = BackfillState(new_conn, state.tables)
            return new_state.block_committed(run_id, block), new_conn
        except Exception as exc:
            logger.warning('Could not confirm commit after reconnect: %s', exc)
            _safe_close(new_conn)
            return None, None

    return None, None


def _normalise_blocks(
        blocks: Optional[Iterable[Union[EventRange, Sequence[int]]]],
        window: EventRange,
        ) -> Optional[list[EventRange]]:
    if blocks is None:
        return None
    result = sorted(_as_event_range(block) for block in blocks)
    if not result:
        raise ETLHelperBackfillError('blocks must not be empty')
    if result[0].start != window.start or result[-1].end != window.end:
        raise ETLHelperBackfillError(
            'blocks must partition the full window from '
            f'{window.start} to {window.end}'
        )
    for previous, current in zip(result, result[1:]):
        if current.start != previous.end + 1:
            raise ETLHelperBackfillError(
                'blocks must be contiguous and non-overlapping: '
                f'{previous.end} is followed by {current.start}'
            )
    return result


def _contiguous_prefix(
        rows: Chunk,
        event_column: str,
        block: EventRange,
        ) -> tuple[Chunk, list[int], Optional[EventRange], Optional[EventRange]]:
    """
    Split rows into the contiguous prefix starting at ``block.start``.

    Returns ``(rows, event_ids, actual_block, gap_after)``.  ``actual_block``
    is None when ``block.start`` itself is missing, because a missing event is
    a missed read and must not be covered.  ``gap_after`` is the remainder of
    the requested block beyond a hole in the middle of the fetched rows.
    """
    if not rows:
        return [], [], None, None

    ordered = sorted(rows, key=lambda row: _event_id(row, event_column))
    event_ids = [_event_id(row, event_column) for row in ordered]

    if event_ids[0] != block.start:
        if event_ids[0] > block.end:
            raise ETLHelperBackfillError(
                f'Query returned event {event_ids[0]} outside block '
                f'{block.start}-{block.end}.  Custom queries must respect '
                'the requested event range.'
            )
        if event_ids[0] > block.start:
            return [], [], None, None
        raise ETLHelperBackfillError(
            f'Query returned event {event_ids[0]} before block start '
            f'{block.start}.  Custom queries must respect the event range.'
        )

    prefix_rows: Chunk = [ordered[0]]
    prefix_ids: list[int] = [event_ids[0]]
    gap_after: Optional[EventRange] = None

    for row, event_id in zip(ordered[1:], event_ids[1:]):
        expected = prefix_ids[-1] + 1
        if event_id == expected:
            prefix_rows.append(row)
            prefix_ids.append(event_id)
            continue
        if event_id > expected:
            if event_id > block.end:
                raise ETLHelperBackfillError(
                    f'Query returned event {event_id} outside block '
                    f'{block.start}-{block.end}.  Custom queries must '
                    'respect the requested event range.'
                )
            gap_after = EventRange(expected, block.end)
            break
        raise ETLHelperBackfillError(
            f'Query returned duplicate or out-of-order event {event_id} '
            f'after {prefix_ids[-1]}.'
        )

    if prefix_ids[-1] > block.end:
        raise ETLHelperBackfillError(
            f'Query returned event {prefix_ids[-1]} outside block '
            f'{block.start}-{block.end}.  Custom queries must respect the '
            'event range.'
        )

    actual_block = EventRange(prefix_ids[0], prefix_ids[-1])
    if gap_after is not None and gap_after.start > actual_block.end + 1:
        gap_after = EventRange(actual_block.end + 1, block.end)
    return prefix_rows, prefix_ids, actual_block, gap_after


def _as_event_range(block: Union[EventRange, Sequence[int]]) -> EventRange:
    if isinstance(block, EventRange):
        return block
    try:
        start, end = block
    except (TypeError, ValueError) as exc:
        raise ETLHelperBackfillError(
            f'Invalid block {block!r}; expected EventRange or (start, end)'
        ) from exc
    return EventRange(int(start), int(end))


def _clip_block(
        block: EventRange,
        boundary: Optional[int],
        window: EventRange,
        ) -> Optional[EventRange]:
    effective = window.start - 1 if boundary is None else boundary
    start = max(block.start, effective + 1, window.start)
    end = min(block.end, window.end)
    if start > end:
        return None
    return EventRange(start, end)


def _validate_request(
        source_table: Optional[str],
        dest_table: Optional[str],
        select_query: Optional[str],
        insert_query: Optional[str],
        event_column: str,
        chunk_size: int,
        ) -> None:
    if select_query is None:
        if source_table is None:
            raise ETLHelperBackfillError(
                'source_table or select_query is required'
            )
        validate_identifier(source_table)
    if insert_query is None and dest_table is None:
        raise ETLHelperBackfillError('dest_table or insert_query is required')
    if dest_table is not None:
        validate_identifier(dest_table)
    validate_identifier(event_column)
    if chunk_size <= 0:
        raise ETLHelperBackfillError('chunk_size must be positive')


def _default_select_sql(
        source_table: Optional[str],
        event_column: str,
        ) -> str:
    if source_table is None:
        raise ETLHelperBackfillError('source_table or select_query is required')
    # Parameter style is filled in by _source_parameters via the source
    # connection.  Use a neutral marker that is replaced per dialect.
    return (
        f'SELECT * FROM {source_table} '
        f'WHERE {event_column} >= {{start}} '
        f'AND {event_column} <= {{end}} '
        f'ORDER BY {event_column}'
    )


def _source_parameters(
        conn: Connection,
        block: EventRange,
        ) -> Sequence[Any]:
    return (block.start, block.end)


def _fetch_rows(
        conn: Connection,
        sql: str,
        parameters: Sequence[Any],
        limit: Optional[int],
        ) -> list[dict[str, Any]]:
    helper = DB_HELPER_FACTORY.from_conn(conn)
    cursor = conn.cursor()
    try:
        cursor.execute(_bind_query(sql, helper), parameters)
        raw_rows = cursor.fetchall() if limit is None else cursor.fetchmany(limit)
        columns = [description[0] for description in cursor.description]
    except helper.sql_exceptions as exc:
        _safe_rollback(conn)
        raise ETLHelperQueryError(
            f'Backfill source query failed.\n\n{sql}\n\n{exc}'
        ) from exc
    else:
        rows = [dict(zip(columns, row)) for row in raw_rows]
        _safe_rollback(conn)
        return rows
    finally:
        cursor.close()


def _bind_query(sql: str, helper) -> str:
    """Replace neutral ``{start}/{end}`` markers with DBAPI placeholders."""
    if '{start}' not in sql and '{end}' not in sql:
        return sql
    placeholders = _positional_placeholders(helper, 2)
    return sql.format(start=placeholders[0], end=placeholders[1])


def _positional_placeholders(helper, count: int) -> list[str]:
    style = helper.positional_paramstyle
    if style == 'qmark':
        return ['?'] * count
    if style == 'numeric':
        return [f':{i}' for i in range(1, count + 1)]
    if style in ('format', 'pyformat'):
        return ['%s'] * count
    raise ETLHelperBackfillError(
        f'Unsupported parameter style for backfill: {style}'
    )


def confirmed_coverage(
        run_id: str,
        conn: Connection,
        *,
        start_event_id: int,
        end_event_id: int,
        tables: Optional[BackfillTables] = None,
        ) -> Optional[EventRange]:
    """
    Return the contiguous range committed for ``run_id``.

    This reads the explicit coverage table.  It never infers coverage from the
    maximum event id in the archive.
    """
    state = BackfillState(conn, tables)
    window = EventRange(int(start_event_id), int(end_event_id))
    boundary = state.confirmed_boundary(run_id, window.start, window.end)
    return _coverage_range(window, boundary)


def rejected_events(
        run_id: str,
        conn: Connection,
        tables: Optional[BackfillTables] = None,
        ) -> tuple[int, ...]:
    """Return the event ids rejected by the destination for ``run_id``."""
    return BackfillState(conn, tables).rejected_event_ids(run_id)


def _event_id(row: Any, event_column: str) -> int:
    try:
        if isinstance(row, Mapping):
            value = row[event_column]
        elif hasattr(row, '_asdict'):
            value = getattr(row, event_column)
        else:
            value = row[event_column]
        return int(value)
    except (KeyError, AttributeError, TypeError, ValueError, IndexError) as exc:
        raise ETLHelperBackfillError(
            f'Source rows must contain integer column {event_column!r}'
        ) from exc


def _finish_run(state: BackfillState, run_id: str) -> None:
    """
    Mark the run complete, treating the status flag as advisory.

    Coverage rows are the durable proof that every block was committed, so a
    failure to update the status must not turn a complete backfill into an
    error.
    """
    try:
        state.finish_run(run_id)
    except Exception as exc:
        logger.warning('Could not mark backfill run %s complete: %s',
                       run_id, exc)


def _try_rejected_ids(
        state: BackfillState,
        run_id: str,
        fallback: tuple[int, ...],
        ) -> tuple[int, ...]:
    """Read rejects without turning a lost connection into a second failure."""
    try:
        return state.rejected_event_ids(run_id)
    except Exception as exc:
        logger.warning('Could not read reject list for %s: %s', run_id, exc)
        return fallback


def _coverage_range(
        window: EventRange,
        boundary: Optional[int],
        ) -> Optional[EventRange]:
    if boundary is None or boundary < window.start:
        return None
    return EventRange(window.start, min(boundary, window.end))


def _handle_unconfirmed(
        run_id: str,
        block: EventRange,
        cause: Exception,
        raise_on_unconfirmed: bool,
        reason: str,
        ) -> None:
    logger.error(
        'Commit outcome for %s events %s-%s is unknown (%s): %s',
        run_id, block.start, block.end, reason, cause,
    )
    if raise_on_unconfirmed:
        raise ETLHelperCommitUnconfirmedError(
            f'Commit outcome for run {run_id!r}, events '
            f'{block.start}-{block.end}, could not be confirmed.  Refusing to '
            'rewrite the block.',
            run_id=run_id,
            unconfirmed_range=block,
        ) from cause


def _is_connection_error(exc: Exception) -> bool:
    name = exc.__class__.__name__.lower()
    return any(part in name for part in (
        'operational', 'interface', 'connection', 'disconnect', 'network'
    ))


def _looks_like_row_rejection(errors: Sequence[Exception]) -> bool:
    if not errors:
        return False
    markers = (
        'constraint', 'unique', 'duplicate', 'check', 'not null',
        'foreign key', 'integrity', 'ora-00001', 'ora-02290', 'ora-01400',
        'cannot insert null', 'violates',
    )
    for error in errors:
        message = str(error).lower()
        if any(marker in message for marker in markers):
            return True
    return False


def _format_error(exc: Exception) -> str:
    return str(exc)[:4000]


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _safe_rollback(conn: Connection) -> None:
    try:
        conn.rollback()
    except Exception:
        pass


def _safe_close(conn: Connection) -> None:
    try:
        conn.close()
    except Exception:
        pass


def _oracle_create_sql(table: str, body: str) -> str:
    return (
        'BEGIN\n'
        f"  EXECUTE IMMEDIATE 'CREATE TABLE {table} ({body})';\n"
        'EXCEPTION\n'
        '  WHEN OTHERS THEN\n'
        '    IF SQLCODE != -955 THEN RAISE; END IF;\n'
        'END;'
    )


def _mssql_create_sql(table: str, body: str) -> str:
    return f"IF OBJECT_ID(N'{table}', N'U') IS NULL CREATE TABLE {table} ({body})"
