"""Session-aware audit-log analysis on top of :class:`Parser`.

The regular :class:`~sql_metadata.parser.Parser` answers "which tables does
*this single SQL statement* touch?".  Audit reviews of concurrent sessions
need more: statements belong to sessions and transactions, temporary
tables are rebound inside transactions behind savepoints, and a log only
guarantees statement order *within* each session — cross-session ordering
has to be supplied explicitly rather than guessed.

This module layers that reasoning on top of ``Parser``:

* :func:`split_sql_statements` splits a script into single statements while
  respecting semicolons inside string literals and comments.
* :class:`AuditLog` parses tagged log rows (session, transaction, SQL) into
  :class:`LogEntry` records using ``Parser`` for table extraction and
  sqlglot for transaction-control classification.
* :class:`SessionAuditAnalyzer` replays savepoint rollbacks to find the
  temporary-table binding that actually survives, and enumerates the
  version pairings that audit rows may observe under a given isolation
  level — never inventing ordering that the log does not support.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum

import sqlglot
from sqlglot import exp
from sqlglot.errors import ParseError

from sql_metadata.comments import _has_hash_variables
from sql_metadata.parser import Parser


def _parse_one_quiet(sql: str) -> exp.Expression | None:
    """Parse one statement, suppressing sqlglot's fallback warnings.

    Transaction-control statements like ``SAVEPOINT`` make sqlglot emit a
    "contains unsupported syntax" warning while it falls back to an opaque
    :class:`exp.Command`.  The analyzer handles those nodes deliberately,
    so the warning would only mislead callers — mirror the silencing used
    by :class:`~sql_metadata.dialect_parser.DialectParser`.
    """
    logger = logging.getLogger("sqlglot")
    old_level = logger.level
    logger.setLevel(logging.CRITICAL)
    try:
        return sqlglot.parse_one(sql)
    finally:
        logger.setLevel(old_level)


# ---------------------------------------------------------------------------
# Enums and data containers
# ---------------------------------------------------------------------------


class AuditKind(str, Enum):
    """Classification of an audited SQL statement."""

    BEGIN = "BEGIN"
    COMMIT = "COMMIT"
    ROLLBACK = "ROLLBACK"
    SAVEPOINT = "SAVEPOINT"
    RELEASE = "RELEASE"
    SET = "SET"
    CREATE = "CREATE"
    DROP = "DROP"
    TRUNCATE = "TRUNCATE"
    INSERT = "INSERT"
    SELECT = "SELECT"
    UNKNOWN = "UNKNOWN"


class IsolationLevel(str, Enum):
    """Transaction isolation levels understood by the analyzer."""

    READ_COMMITTED = "READ COMMITTED"
    REPEATABLE_READ = "REPEATABLE READ"


#: Statement kinds that can reference tables and therefore need a Parser pass.
_TABLE_BEARING_KINDS = frozenset(
    {
        AuditKind.CREATE,
        AuditKind.DROP,
        AuditKind.TRUNCATE,
        AuditKind.INSERT,
        AuditKind.SELECT,
        AuditKind.UNKNOWN,
    }
)


@dataclass(frozen=True)
class LogEntry:
    """One parsed statement from the audit log.

    Table names come from :class:`Parser`; ``sources`` are the parsed tables
    minus the statement's own write target.
    """

    index: int
    session: str | None
    transaction: str | None
    sql: str
    kind: AuditKind
    tables: tuple[str, ...]
    target: str | None
    sources: tuple[str, ...]
    temporary: bool
    savepoint: str | None
    isolation: IsolationLevel | None
    row_id: str | None


@dataclass(frozen=True)
class AuditPair:
    """The table-version pairing of one audit write statement.

    ``permanent`` maps a permanent table name to the version label the
    statement may have read; ``temporary`` maps a temporary name to the
    source bound in the reading session.
    """

    audit_table: str
    entry_index: int
    permanent: dict[str, str]
    temporary: dict[str, str]


@dataclass(frozen=True)
class AuditPossibility:
    """One globally consistent pairing of all audit rows."""

    name: str
    final_source_of_p: str
    committed_writes: dict[str, str]
    pairs: tuple[AuditPair, ...]
    explanation: str


@dataclass(frozen=True)
class TempBindingEvidence:
    """Evidence for the effective binding of one temporary table read."""

    session: str
    name: str
    source: str
    evidence_entry_index: int | None
    note: str


@dataclass(frozen=True)
class AuditAnalysis:
    """Result of analyzing one audit log under one isolation level."""

    entries: tuple[LogEntry, ...]
    isolation: IsolationLevel
    possibilities: tuple[AuditPossibility, ...]
    shared_ordering: tuple[str, ...]
    unknown_orderings: tuple[str, ...]
    effective_temp_bindings: tuple[TempBindingEvidence, ...]
    notes: tuple[str, ...]


# ---------------------------------------------------------------------------
# Statement splitting (semicolon-aware, string/comment safe)
# ---------------------------------------------------------------------------


def _comment_span(sql: str, start: int, hash_comments: bool) -> int | None:
    """Return the index just past a comment starting at *start*, or ``None``."""
    if sql.startswith("--", start):
        newline = sql.find("\n", start)
        return len(sql) if newline == -1 else newline + 1
    if sql.startswith("/*", start):
        close = sql.find("*/", start + 2)
        return len(sql) if close == -1 else close + 2
    if hash_comments and sql.startswith("#", start):
        newline = sql.find("\n", start)
        return len(sql) if newline == -1 else newline + 1
    return None


def split_sql_statements(sql: str) -> list[str]:
    """Split a script on top-level semicolons.

    Semicolons inside single-quoted strings, double-quoted identifiers,
    backtick identifiers, ``--``, ``#`` and ``/* ... */`` comments are not
    treated as separators (``#`` counts as a comment only when it is not an
    MSSQL ``#temp`` table or template variable, matching
    :func:`sql_metadata.comments._has_hash_variables`).  Doubled quote
    escapes (``'it''s'``) are recognised.  Empty statements (``;;``) are
    dropped.

    :param sql: One or more SQL statements, optionally separated by ``;``.
    :returns: Trimmed individual statement strings in source order.
    """
    statements: list[str] = []
    buffer: list[str] = []
    quote = ""
    hash_comments = "#" in sql and not _has_hash_variables(sql)
    i = 0
    n = len(sql)
    while i < n:
        if quote:
            i, quote = _consume_quoted(sql, i, quote, buffer)
        else:
            i, quote = _consume_unquoted(sql, i, hash_comments, buffer, statements)
    tail = "".join(buffer).strip()
    if tail:
        statements.append(tail)
    return statements


def _consume_quoted(sql: str, i: int, quote: str, buffer: list[str]) -> tuple[int, str]:
    """Process one character inside an open string/identifier quote."""
    char = sql[i]
    buffer.append(char)
    if char == quote and sql[i + 1 : i + 2] == quote:
        # Doubled delimiter is an escaped quote, e.g. 'it''s'.
        buffer.append(sql[i + 1])
        return i + 2, quote
    if char == quote:
        return i + 1, ""
    return i + 1, quote


def _consume_unquoted(
    sql: str,
    i: int,
    hash_comments: bool,
    buffer: list[str],
    statements: list[str],
) -> tuple[int, str]:
    """Process one top-level character, returning the next index and quote."""
    char = sql[i]
    comment_end = _comment_span(sql, i, hash_comments)
    if comment_end is not None:
        buffer.append(sql[i:comment_end])
        return comment_end, ""
    if char in ("'", '"', "`"):
        buffer.append(char)
        return i + 1, char
    if char == ";":
        statement = "".join(buffer).strip()
        if statement:
            statements.append(statement)
        buffer.clear()
    else:
        buffer.append(char)
    return i + 1, ""


# ---------------------------------------------------------------------------
# Statement classification
# ---------------------------------------------------------------------------


def _isolation_from_text(text: str) -> IsolationLevel | None:
    """Map an isolation-level clause fragment to the enum."""
    upper = text.upper()
    if "REPEATABLE READ" in upper:
        return IsolationLevel.REPEATABLE_READ
    if "READ COMMITTED" in upper:
        return IsolationLevel.READ_COMMITTED
    return None


def _savepoint_name(node: exp.Expression) -> str | None:
    """Extract a savepoint name from a ROLLBACK / SAVEPOINT / RELEASE node."""
    if isinstance(node, exp.Rollback):
        marker = node.args.get("savepoint")
        return marker.name if isinstance(marker, exp.Identifier) else None
    if isinstance(node, exp.Command):
        # sqlglot wraps the trailing text in an exp.Literal, whose str()
        # rendering adds quotes — read the raw literal value instead.
        expression_node = node.expression
        if isinstance(expression_node, exp.Literal):
            text = expression_node.this
        else:
            text = str(expression_node or "")
        return text.removeprefix("SAVEPOINT").strip() or None
    return None


def _create_target(node: exp.Create) -> str | None:
    """Return the target table name of a CREATE statement."""
    target = node.this
    table = target if isinstance(target, exp.Table) else target.find(exp.Table)
    return table.name if table is not None else None


def _is_temporary_create(node: exp.Expression) -> bool:
    """Return whether a CREATE node carries a TEMPORARY property."""
    if not isinstance(node, exp.Create):
        return False
    properties = node.args.get("properties")
    if properties is None:
        return False
    return any(
        isinstance(prop, exp.TemporaryProperty) for prop in properties.expressions
    )


def _target_name(node: exp.Expression) -> str | None:
    """Return the write-target table name for DML/DDL nodes."""
    if isinstance(node, exp.Create):
        return _create_target(node)
    if isinstance(node, exp.Insert) and isinstance(node.this, exp.Table):
        return str(node.this.name)
    if isinstance(node, (exp.Drop, exp.TruncateTable)):
        tables = list(node.find_all(exp.Table))
        return tables[0].name if tables else None
    return None


#: Exact AST root type → statement kind for everything except opaque
#: commands, which need their leading command word inspected instead.
_NODE_KIND_MAP: dict[type[exp.Expression], AuditKind] = {
    exp.Transaction: AuditKind.BEGIN,
    exp.Commit: AuditKind.COMMIT,
    exp.Rollback: AuditKind.ROLLBACK,
    exp.Set: AuditKind.SET,
    exp.Create: AuditKind.CREATE,
    exp.Drop: AuditKind.DROP,
    exp.TruncateTable: AuditKind.TRUNCATE,
    exp.Insert: AuditKind.INSERT,
    exp.Select: AuditKind.SELECT,
}


def _classify_kind(node: exp.Expression) -> AuditKind:
    """Map a sqlglot root node to an :class:`AuditKind`."""
    if isinstance(node, exp.Command):
        return _command_kind(node)
    return _NODE_KIND_MAP.get(type(node), AuditKind.UNKNOWN)


def _command_kind(node: exp.Command) -> AuditKind:
    """Classify an opaque command sqlglot could not model as a real node."""
    command = str(node.this or "").upper()
    if command == "SAVEPOINT":
        return AuditKind.SAVEPOINT
    if command == "RELEASE":
        return AuditKind.RELEASE
    return AuditKind.UNKNOWN


def _isolation_from_set(node: exp.Expression, sql: str) -> IsolationLevel | None:
    """Extract the isolation level from a SET statement, with regex fallback."""
    if isinstance(node, exp.Set):
        for var in node.find_all(exp.Var):
            isolation = _isolation_from_text(var.this)
            if isolation is not None:
                return isolation
    return _isolation_from_text(sql) if "ISOLATION" in sql.upper() else None


def parse_statement(
    sql: str,
    *,
    session: str | None = None,
    transaction: str | None = None,
    index: int = 0,
    row_id: str | None = None,
) -> LogEntry:
    """Parse one SQL statement into a :class:`LogEntry`.

    Transaction-control statements (``BEGIN``, ``COMMIT``, savepoints, …)
    are classified via sqlglot even though :class:`Parser` itself rejects
    them as unsupported query types; their table list is empty.

    :param sql: A single SQL statement.
    :param session: Owning session identifier from the log.
    :param transaction: Owning transaction identifier from the log.
    :param index: Zero-based position of the entry in the whole log.
    :param row_id: Optional explicit row identifier for ordering edges.
    """
    try:
        node = _parse_one_quiet(sql)
    except ParseError:
        node = None

    kind = _classify_kind(node) if node is not None else AuditKind.UNKNOWN
    target = _target_name(node) if node is not None else None
    temporary = _is_temporary_create(node) if node is not None else False
    savepoint = _savepoint_name(node) if node is not None else None
    isolation = _isolation_from_set(node, sql) if node is not None else None

    if kind in _TABLE_BEARING_KINDS:
        try:
            tables = tuple(Parser(sql).tables)
        except ValueError:
            tables = ()
    else:
        # Transaction-control statements reference no tables; skip Parser
        # entirely so its unsupported-type logging is not triggered.
        tables = ()
    sources = tuple(name for name in tables if name != target)

    return LogEntry(
        index=index,
        session=session,
        transaction=transaction,
        sql=sql,
        kind=kind,
        tables=tables,
        target=target,
        sources=sources,
        temporary=temporary,
        savepoint=savepoint,
        isolation=isolation,
        row_id=row_id,
    )


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------


class AuditLog:
    """A parsed session audit log.

    Construct from dictionaries carrying ``session``, ``transaction`` and
    ``sql`` keys.  Optional keys:

    * ``id`` — stable row identifier used in ordering edges.
    * ``isolation`` — explicit isolation level for the row's transaction.
    * ``happens_before`` — list of row ids whose statements are known to
      complete before this row starts.  Only cross-session edges need to be
      listed; within-session order is derived from row order.
    """

    def __init__(self, rows: list[dict[str, object]]):
        self._entries, self._explicit_edges = self._parse_rows(rows)
        self._edges = list(self._explicit_edges)
        self._add_session_edges(self._entries, self._edges)

    @property
    def entries(self) -> tuple[LogEntry, ...]:
        """All parsed statements in log order."""
        return tuple(self._entries)

    @property
    def ordering_edges(self) -> tuple[tuple[int, int], ...]:
        """Known ``(earlier_entry_index, later_entry_index)`` ordering edges.

        Includes both explicit cross-session ``happens_before`` edges and
        the consecutive within-session edges derived from log order.
        """
        return tuple(self._edges)

    @property
    def explicit_edges(self) -> tuple[tuple[int, int], ...]:
        """Ordering edges declared explicitly via ``happens_before`` only."""
        return tuple(self._explicit_edges)

    def entries_of_session(self, session: str) -> tuple[LogEntry, ...]:
        """Return the entries of one session in log order."""
        return tuple(entry for entry in self._entries if entry.session == session)

    def _parse_rows(
        self, rows: list[dict[str, object]]
    ) -> tuple[list[LogEntry], list[tuple[int, int]]]:
        entries: list[LogEntry] = []
        # A row may expand to several statements; remember each row's span so
        # a "row X completes before row Y starts" edge joins X's last entry
        # to Y's first entry rather than two row beginnings.
        id_to_span: dict[str, tuple[int, int]] = {}
        pending_edges: list[tuple[str, str]] = []
        for row in rows:
            session = self._as_str(row.get("session"))
            transaction = self._as_str(row.get("transaction"))
            row_id = self._as_str(row.get("id"))
            explicit_isolation = self._as_str(row.get("isolation"))
            sql_text = self._as_str(row.get("sql")) or ""
            first_index = len(entries)
            for statement in split_sql_statements(sql_text):
                entry = parse_statement(
                    statement,
                    session=session,
                    transaction=transaction,
                    index=len(entries),
                    row_id=row_id,
                )
                if explicit_isolation and entry.isolation is None:
                    entry = self._with_isolation(entry, explicit_isolation)
                entries.append(entry)
            if row_id is not None:
                id_to_span[row_id] = (first_index, len(entries) - 1)
            for earlier in self._as_str_list(row.get("happens_before")):
                pending_edges.append((earlier, row_id or ""))

        edges: list[tuple[int, int]] = []
        for earlier, later in pending_edges:
            if earlier not in id_to_span or later not in id_to_span:
                raise ValueError(
                    "happens_before references an unknown row id: "
                    f"{earlier!r} -> {later!r}"
                )
            earlier_end = id_to_span[earlier][1]
            later_start = id_to_span[later][0]
            edges.append((earlier_end, later_start))
        return entries, edges

    @staticmethod
    def _add_session_edges(
        entries: list[LogEntry], edges: list[tuple[int, int]]
    ) -> None:
        """Add consecutive within-session ordering edges."""
        previous_by_session: dict[str, int] = {}
        for entry in entries:
            if entry.session is None:
                continue
            previous = previous_by_session.get(entry.session)
            if previous is not None:
                edges.append((previous, entry.index))
            previous_by_session[entry.session] = entry.index

    @staticmethod
    def _with_isolation(entry: LogEntry, label: str) -> LogEntry:
        isolation = _isolation_from_text(label)
        if isolation is None:
            return entry
        return LogEntry(
            index=entry.index,
            session=entry.session,
            transaction=entry.transaction,
            sql=entry.sql,
            kind=entry.kind,
            tables=entry.tables,
            target=entry.target,
            sources=entry.sources,
            temporary=entry.temporary,
            savepoint=entry.savepoint,
            isolation=isolation,
            row_id=entry.row_id,
        )

    @staticmethod
    def _as_str(value: object) -> str | None:
        return value if isinstance(value, str) else None

    @staticmethod
    def _as_str_list(value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [item for item in value if isinstance(item, str)]


# ---------------------------------------------------------------------------
# Transaction replay (savepoint-aware binding tracking)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _Snapshot:
    permanent: dict[str, str]
    temporary: dict[str, str]
    temporary_origin: dict[str, int]


class _TransactionReplay:
    """Replay one session's entries to track effective table bindings.

    Permanent tables map to the source of their currently visible contents;
    temporary tables are tracked in a separate session-private namespace.
    Savepoints store full state snapshots and form a LIFO stack so a
    repeated savepoint name shadows the outer one.
    """

    def __init__(self, baseline: dict[str, str] | None = None):
        self._permanent: dict[str, str] = dict(baseline or {})
        self._temporary: dict[str, str] = {}
        self._temporary_origin: dict[str, int] = {}
        self._stack: list[tuple[str, _Snapshot]] = []
        self.committed: dict[str, str] = {}

    @property
    def temporary(self) -> dict[str, str]:
        return dict(self._temporary)

    def origin_of(self, name: str) -> int | None:
        """Return the log entry index that established *name*'s binding."""
        return self._temporary_origin.get(name)

    def run(self, entries: tuple[LogEntry, ...]) -> None:
        for entry in entries:
            self._step(entry)

    def _step(self, entry: LogEntry) -> None:
        handler = self._HANDLERS.get(entry.kind)
        if handler is not None:
            handler(self, entry)

    def _handle_savepoint(self, entry: LogEntry) -> None:
        if not entry.savepoint:
            return
        self._stack.append(
            (
                entry.savepoint,
                _Snapshot(
                    dict(self._permanent),
                    dict(self._temporary),
                    dict(self._temporary_origin),
                ),
            )
        )

    def _handle_rollback(self, entry: LogEntry) -> None:
        if entry.savepoint:
            self._rollback_to(entry.savepoint)

    def _handle_release(self, entry: LogEntry) -> None:
        if entry.savepoint:
            self._release(entry.savepoint)

    def _handle_temp_create(self, entry: LogEntry) -> None:
        if entry.temporary and entry.target:
            self._temporary[entry.target] = self._resolve(self._source_label(entry))
            self._temporary_origin[entry.target] = entry.index

    def _handle_insert(self, entry: LogEntry) -> None:
        self._apply_insert(entry)

    def _handle_truncate(self, entry: LogEntry) -> None:
        if entry.target:
            self._permanent[entry.target] = "<truncated>"

    def _handle_commit(self, _entry: LogEntry) -> None:
        self.committed = dict(self._permanent)

    _HANDLERS: dict[AuditKind, Callable[["_TransactionReplay", LogEntry], None]] = {
        AuditKind.SAVEPOINT: _handle_savepoint,
        AuditKind.ROLLBACK: _handle_rollback,
        AuditKind.RELEASE: _handle_release,
        AuditKind.CREATE: _handle_temp_create,
        AuditKind.INSERT: _handle_insert,
        AuditKind.TRUNCATE: _handle_truncate,
        AuditKind.COMMIT: _handle_commit,
    }

    def _resolve(self, name: str) -> str:
        """Resolve a table name through the current temporary bindings."""
        return self._temporary.get(name, name)

    def _apply_insert(self, entry: LogEntry) -> None:
        target = entry.target
        if target is None:
            return
        label = self._resolve(self._source_label(entry))
        if target in self._temporary:
            self._temporary[target] = label
            self._temporary_origin[target] = entry.index
        else:
            self._permanent[target] = label

    @staticmethod
    def _source_label(entry: LogEntry) -> str:
        return entry.sources[0] if entry.sources else "<literal>"

    def _rollback_to(self, name: str) -> None:
        for depth in range(len(self._stack) - 1, -1, -1):
            if self._stack[depth][0] != name:
                continue
            snapshot = self._stack[depth][1]
            self._permanent = dict(snapshot.permanent)
            self._temporary = dict(snapshot.temporary)
            self._temporary_origin = dict(snapshot.temporary_origin)
            del self._stack[depth + 1 :]
            return

    def _release(self, name: str) -> None:
        for depth in range(len(self._stack) - 1, -1, -1):
            if self._stack[depth][0] == name:
                del self._stack[depth:]
                return


# ---------------------------------------------------------------------------
# Ordering graph
# ---------------------------------------------------------------------------


class _OrderingGraph:
    """Reachability over within-session order plus explicit edges."""

    def __init__(self, entries: tuple[LogEntry, ...], edges: list[tuple[int, int]]):
        self._entries = entries
        self._after: dict[int, set[int]] = {entry.index: set() for entry in entries}
        for earlier, later in edges:
            self._after[earlier].add(later)
        self._closure = {index: self._reach(index) for index in self._after}

    def _reach(self, start: int) -> set[int]:
        seen: set[int] = set()
        pending = [start]
        while pending:
            current = pending.pop()
            for later in self._after[current]:
                if later not in seen:
                    seen.add(later)
                    pending.append(later)
        return seen

    def ordered(self, earlier: int, later: int) -> bool:
        """Return whether *earlier* is known to happen before *later*."""
        return later in self._closure[earlier]

    def label(self, index: int) -> str:
        entry = self._entries[index]
        return entry.row_id or f"entry-{entry.index}"


# ---------------------------------------------------------------------------
# Analyzer
# ---------------------------------------------------------------------------


class SessionAuditAnalyzer:
    """Analyze a two-session writer/reader audit log.

    The writer session replays savepoints and commits a permanent table
    ``p``; the reader session audits its reads into ``audit_*`` tables.
    Only ordering supported by the log edges is used — unrelated cross-
    session event pairs are reported as unknown rather than assumed.
    """

    def __init__(self, log: AuditLog):
        self._log = log
        self._entries = log.entries

    def analyze(
        self,
        *,
        writer_session: str,
        reader_session: str,
        permanent_table: str,
        baseline_source: str,
        isolation: IsolationLevel,
    ) -> AuditAnalysis:
        """Run the full analysis and return an :class:`AuditAnalysis`."""
        writer = self._replay(writer_session, {permanent_table: baseline_source})
        reader = self._replay(reader_session, None)
        new_label = writer.committed.get(permanent_table, baseline_source)

        audit_entries = self._audit_entries(reader_session)
        possibilities = self._possibilities(
            audit_entries,
            writer,
            reader,
            permanent_table,
            baseline_source,
            new_label,
            isolation,
        )
        graph = _OrderingGraph(self._entries, list(self._log.ordering_edges))
        shared, unknown = self._ordering_report(graph, writer_session, reader_session)
        bindings = self._temp_evidence(writer_session, writer, reader_session, reader)
        notes = self._notes(isolation)
        return AuditAnalysis(
            entries=self._entries,
            isolation=isolation,
            possibilities=possibilities,
            shared_ordering=tuple(shared),
            unknown_orderings=tuple(unknown),
            effective_temp_bindings=tuple(bindings),
            notes=tuple(notes),
        )

    def _replay(
        self, session: str, baseline: dict[str, str] | None
    ) -> _TransactionReplay:
        replay = _TransactionReplay(baseline)
        replay.run(self._log.entries_of_session(session))
        return replay

    def _audit_entries(self, session: str) -> tuple[LogEntry, ...]:
        return tuple(
            entry
            for entry in self._log.entries_of_session(session)
            if entry.kind is AuditKind.INSERT and str(entry.target).startswith("audit")
        )

    def _possibilities(
        self,
        audit_entries: tuple[LogEntry, ...],
        writer: _TransactionReplay,
        reader: _TransactionReplay,
        permanent_table: str,
        old_label: str,
        new_label: str,
        isolation: IsolationLevel,
    ) -> tuple[AuditPossibility, ...]:
        if len(audit_entries) < 2:
            return ()
        versions = self._version_choices(isolation, old_label, new_label)
        possibilities = []
        for name, first_version, second_version in versions:
            pairs = self._build_pairs(
                audit_entries,
                reader,
                permanent_table,
                (first_version, second_version),
            )
            possibilities.append(
                AuditPossibility(
                    name=name,
                    final_source_of_p=new_label,
                    committed_writes=dict(writer.committed),
                    pairs=tuple(pairs),
                    explanation=self._explain(name, isolation, old_label, new_label),
                )
            )
        return tuple(possibilities)

    @staticmethod
    def _version_choices(
        isolation: IsolationLevel, old_label: str, new_label: str
    ) -> tuple[tuple[str, str, str], ...]:
        if isolation is IsolationLevel.REPEATABLE_READ:
            return (
                ("both-old", old_label, old_label),
                ("both-new", new_label, new_label),
            )
        return (
            ("old-then-new", old_label, new_label),
            ("both-new", new_label, new_label),
        )

    def _build_pairs(
        self,
        audit_entries: tuple[LogEntry, ...],
        reader: _TransactionReplay,
        permanent_table: str,
        versions: tuple[str, str],
    ) -> list[AuditPair]:
        pairs = []
        temp_bindings = reader.temporary
        for position, entry in enumerate(audit_entries):
            permanent: dict[str, str] = {}
            temporary: dict[str, str] = {}
            for source in entry.sources:
                if source in temp_bindings:
                    temporary[source] = temp_bindings[source]
                elif source == permanent_table:
                    permanent[source] = versions[position]
            pairs.append(
                AuditPair(
                    audit_table=str(entry.target),
                    entry_index=entry.index,
                    permanent=permanent,
                    temporary=temporary,
                )
            )
        return pairs

    @staticmethod
    def _explain(
        name: str,
        isolation: IsolationLevel,
        old_label: str,
        new_label: str,
    ) -> str:
        if isolation is IsolationLevel.REPEATABLE_READ:
            return (
                f"REPEATABLE READ fixes one read view at the first query: "
                f"both audits see {old_label} or both see {new_label}; "
                f"an old-then-new split is impossible ({name})."
            )
        return (
            f"READ COMMITTED takes a fresh snapshot per statement: the "
            f"second query starts after the writer commit and sees "
            f"{new_label}, while the first may still see {old_label} ({name})."
        )

    def _ordering_report(
        self,
        graph: _OrderingGraph,
        writer_session: str,
        reader_session: str,
    ) -> tuple[list[str], list[str]]:
        """Render known cross-session ordering and genuinely unknown pairs.

        Only the writer's commits and the reader's audit writes are
        compared.  An edge in either direction is a shared, guaranteed
        ordering; pairs with no edge either way are reported as unknown —
        the analyzer never picks an ordering the log does not support.
        """
        commits = [
            entry
            for entry in self._entries
            if entry.session == writer_session and entry.kind is AuditKind.COMMIT
        ]
        audits = self._audit_entries(reader_session)
        shared: list[str] = []
        unknown: list[str] = []
        for commit in commits:
            commit_label = graph.label(commit.index)
            for audit in audits:
                audit_label = graph.label(audit.index)
                if graph.ordered(commit.index, audit.index):
                    shared.append(f"{commit_label} before {audit_label}")
                elif graph.ordered(audit.index, commit.index):
                    shared.append(f"{audit_label} before {commit_label}")
                else:
                    unknown.append(f"{audit_label} vs {commit_label}")
        return shared, unknown

    @staticmethod
    def _temp_evidence(
        writer_session: str,
        writer: _TransactionReplay,
        reader_session: str,
        reader: _TransactionReplay,
    ) -> list[TempBindingEvidence]:
        evidence = [
            TempBindingEvidence(
                session=reader_session,
                name=name,
                source=source,
                evidence_entry_index=reader.origin_of(name),
                note=(
                    "session-private temporary table; the writer's same-named "
                    "rebindings can never alter this binding"
                ),
            )
            for name, source in sorted(reader.temporary.items())
        ]
        for name, source in sorted(writer.temporary.items()):
            evidence.append(
                TempBindingEvidence(
                    session=writer_session,
                    name=name,
                    source=source,
                    evidence_entry_index=writer.origin_of(name),
                    note=(
                        "surviving binding after savepoint replay; rolled-back "
                        "rebindings are discarded and never committed"
                    ),
                )
            )
        return evidence

    @staticmethod
    def _notes(isolation: IsolationLevel) -> list[str]:
        notes = [
            "only within-session statement order and explicit happens_before "
            "edges are used; no other cross-session ordering is assumed",
            "temporary tables are session-scoped, so identical names in two "
            "sessions are distinct bindings",
        ]
        if isolation is IsolationLevel.REPEATABLE_READ:
            notes.append(
                "a read view is taken at the first query and reused, so an "
                "old-then-new reconciliation across the two audits is rejected"
            )
        return notes


def analyze_audit_log(
    rows: list[dict[str, object]],
    *,
    writer_session: str,
    reader_session: str,
    permanent_table: str = "p",
    baseline_source: str = "seed_old",
    isolation: IsolationLevel = IsolationLevel.READ_COMMITTED,
) -> AuditAnalysis:
    """Convenience wrapper parsing *rows* and running the analyzer in one call."""
    log = AuditLog(rows)
    return SessionAuditAnalyzer(log).analyze(
        writer_session=writer_session,
        reader_session=reader_session,
        permanent_table=permanent_table,
        baseline_source=baseline_source,
        isolation=isolation,
    )
