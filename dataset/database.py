import logging
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal
from urllib.parse import parse_qs, urlparse

from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import Connection, Engine, Transaction, create_engine, event, inspect
from sqlalchemy.engine.reflection import Inspector
from sqlalchemy.schema import MetaData
from sqlalchemy.sql import text
from sqlalchemy.sql.expression import Executable

from dataset.table import Table
from dataset.types import ColumnType, Types
from dataset.util import (
    QUERY_STEP,
    DatasetError,
    ResultIter,
    RowFactory,
    normalize_table_name,
    row_factory,
    safe_url,
)

log = logging.getLogger(__name__)


class ReadTransaction:
    """Handle for an explicit, consistent read transaction.

    Instances are produced by :py:meth:`Database.read_transaction` and are
    also returned as the context variable of the ``with`` statement. They
    identify the database transaction that backs a consistent read and never
    commit any writes made by an enclosing transaction.

    The handle is truthy while the transaction is active and falsy once it
    has been closed::

        with db.read_transaction() as rt:
            assert rt  # the read transaction is open
        assert not rt
    """

    def __init__(
        self,
        db: "Database",
        transaction: Transaction,
        owns_transaction: bool,
    ):
        self._db = db
        self._transaction = transaction
        # Only the handle that started the SQLAlchemy transaction is allowed
        # to roll it back; nested or outer-tx-reusing handles merely close.
        self._owns_transaction = owns_transaction
        self._closed = False

    @property
    def transaction(self) -> Transaction:
        """The SQLAlchemy ``Transaction`` backing this consistent read."""
        return self._transaction

    @property
    def connection(self) -> Connection:
        """The connection on which the read transaction is running."""
        return self._db.executable

    @property
    def is_active(self) -> bool:
        """Whether the read transaction is still open."""
        return not self._closed and self._transaction.is_active

    @property
    def read_only(self) -> bool:
        """Always ``True``: writes are rejected inside this context."""
        return True

    def close(self) -> None:
        """End the read transaction.

        A read transaction that owns its SQLAlchemy transaction is rolled
        back here, discarding the read snapshot without ever committing an
        enclosing write transaction. A handle that merely joined an existing
        transaction leaves that transaction untouched. Closing twice is a
        no-op.
        """
        if self._closed:
            return
        self._closed = True
        self._db._end_read_transaction(self)

    def __bool__(self) -> bool:
        return self.is_active

    def __repr__(self) -> str:
        state = "active" if self.is_active else "closed"
        return f"<ReadTransaction({state})>"


class Database:
    """A database object represents a SQL database with multiple tables."""

    def __init__(
        self,
        url: str,
        schema: str | None = None,
        engine_kwargs: dict[str, Any] | None = None,
        ensure_schema: bool = True,
        row_type: RowFactory = row_factory,
        sqlite_wal_mode: bool = True,
        on_connect_statements: list[str] | None = None,
    ) -> None:
        """Configure and connect to the database."""
        if engine_kwargs is None:
            engine_kwargs = {}

        parsed_url = urlparse(url)
        # if parsed_url.scheme.lower() in 'sqlite':
        #     # ref: https://github.com/pudo/dataset/issues/163
        #     if 'poolclass' not in engine_kwargs:
        #         engine_kwargs['poolclass'] = StaticPool

        self.lock = threading.RLock()
        self.local = threading.local()
        self.connections: dict[int, Connection] = {}

        if len(parsed_url.query):
            query = parse_qs(parsed_url.query)
            if schema is None:
                schema_qs = query.get("schema", query.get("searchpath", []))
                if len(schema_qs):
                    schema = schema_qs.pop()

        self.schema = schema
        self.engine: Engine | None = create_engine(url, **engine_kwargs)
        assert self.engine is not None
        self.is_postgres = self.engine.dialect.name == "postgresql"
        self.is_sqlite = self.engine.dialect.name == "sqlite"
        self.is_mysql = "mysql" in self.engine.dialect.name
        if on_connect_statements is None:
            on_connect_statements = []

        def _run_on_connect(dbapi_con: Any, con_record: Any) -> None:
            # reference:
            # https://stackoverflow.com/questions/9671490/how-to-set-sqlite-pragma-statements-with-sqlalchemy
            # https://stackoverflow.com/a/7831210/1890086
            for statement in on_connect_statements:
                dbapi_con.execute(statement)

        if self.is_sqlite and parsed_url.path != "" and sqlite_wal_mode:
            # we only enable WAL mode for sqlite databases that are not in-memory
            on_connect_statements.append("PRAGMA journal_mode=WAL")

        if len(on_connect_statements):
            event.listen(self.engine, "connect", _run_on_connect)

        self.types = Types(is_postgres=self.is_postgres)
        self.url = url
        self.row_type: RowFactory = row_type
        self.ensure_schema = ensure_schema
        self._tables: dict[str, Table] = {}

    @property
    def executable(self) -> Connection:
        """Connection against which statements will be executed."""
        with self.lock:
            tid = threading.get_ident()
            if tid not in self.connections:
                if self.engine is None:
                    raise RuntimeError("Database is closed")
                self.connections[tid] = self.engine.connect()
            return self.connections[tid]

    @property
    def op(self) -> Operations:
        """Get an alembic operations context."""
        ctx = MigrationContext.configure(self.executable)
        return Operations(ctx)

    @property
    def inspect(self) -> Inspector:
        """Get a SQLAlchemy inspector."""
        return inspect(self.executable)

    def has_table(self, name: str) -> bool:
        return self.inspect.has_table(name, schema=self.schema)

    @property
    def metadata(self) -> MetaData:
        """Return a SQLAlchemy schema cache object."""
        return MetaData(schema=self.schema)

    @property
    def in_transaction(self) -> bool:
        """Check if this database is in a transactional context."""
        if not hasattr(self.local, "tx"):
            return False
        return len(self.local.tx) > 0

    def _read_transactions(self) -> list[ReadTransaction]:
        """The active read-transaction handles for the current thread."""
        handles = getattr(self.local, "read_transactions", None)
        if handles is None:
            handles = []
            self.local.read_transactions = handles
        return handles

    @property
    def current_read_transaction(self) -> ReadTransaction | None:
        """The innermost active read transaction, if any.

        All reads performed while a read transaction is active are served
        from the SQLAlchemy transaction exposed by the returned handle.
        """
        handles = self._read_transactions()
        return handles[-1] if handles else None

    @property
    def is_read_only(self) -> bool:
        """Whether a read transaction is currently active on this thread."""
        return bool(self._read_transactions())

    @contextmanager
    def read_transaction(self) -> Iterator[ReadTransaction]:
        """Open an explicit, consistent read transaction.

        All statements issued through this ``Database`` (and its tables) on
        the current thread run against the same database transaction, so a
        sequence of reads such as a table-existence check, paging through
        :py:meth:`Table.find` and :py:meth:`Table.count` observes one stable
        snapshot instead of drifting when another process commits rows in
        between::

            with db.read_transaction() as rt:
                print(rt.transaction)  # the transaction backing the report
                total = table.count()
                page = table.find(_limit=100)

        If a write transaction is already active on the thread, its
        transaction is reused: uncommitted rows written there are visible to
        the reads, and leaving the read transaction only ends the read scope
        and never commits the outer write transaction. When no transaction
        is active, a new one is started and rolled back on exit, so the read
        never leaves any committed state behind.

        On SQLite, a driver-level ``BEGIN`` is issued when the read owns the
        connection so that all reads really share one snapshot; the pysqlite
        driver otherwise defers transactions until the first write. Other
        backends run all reads on the same connection and transaction, whose
        exact isolation guarantees follow the backend's default isolation
        level.

        Writes through the :py:class:`Table` API are rejected while the read
        transaction is active by raising :class:`dataset.DatasetError`.
        """
        handles = self._read_transactions()
        conn = self.executable
        if handles or self.in_transaction:
            # Reuse an active read handle's transaction or the caller's
            # explicit (possibly write) transaction. The caller's
            # uncommitted rows stay visible, and we must never commit it.
            transaction = conn.get_transaction()
            assert transaction is not None
            owns_transaction = False
        else:
            # No explicit user transaction: take over either a fresh
            # connection or an autobegin transaction started by earlier
            # reads, and own its rollback.
            if conn.in_transaction():
                transaction = conn.get_transaction()
                assert transaction is not None
            else:
                transaction = conn.begin()
            owns_transaction = True
            if self.is_sqlite:
                # SQLAlchemy's pysqlite dialect implements do_begin() as a
                # no-op, so plain SELECTs would otherwise run in autocommit
                # mode without pinning a snapshot. Force the driver-level
                # transaction explicitly and take its read mark right away:
                # in WAL mode a read snapshot is only established by the
                # first statement touching a real table, not by BEGIN alone
                # (and a prior autobegin read must not delay that either).
                dbapi_connection = conn.connection.dbapi_connection
                assert dbapi_connection is not None
                try:
                    if not dbapi_connection.in_transaction:
                        dbapi_connection.execute("BEGIN")
                    conn.execute(text("SELECT 1 FROM sqlite_master LIMIT 1"))
                except Exception:
                    transaction.rollback()
                    self._release_connection()
                    raise
        handle = ReadTransaction(self, transaction, owns_transaction)
        handles.append(handle)
        try:
            yield handle
        finally:
            if handles and handles[-1] is handle:
                handle.close()

    def _end_read_transaction(self, handle: ReadTransaction) -> None:
        """Roll back a read-only transaction and pop its handle."""
        handles = self._read_transactions()
        if handle not in handles:
            return
        # Nested handles must be closed innermost-first.
        while handles:
            current = handles.pop()
            current._closed = True
            if current is handle:
                break
        if handle._owns_transaction and handle.transaction.is_active:
            # This transaction was started solely for the consistent read;
            # roll it back so no state is ever committed by a read.
            handle.transaction.rollback()
            self._release_connection()

    def _ensure_writable(self) -> None:
        """Reject writes that would violate an active read transaction."""
        if self._read_transactions():
            raise DatasetError(
                "Cannot write while a read transaction is active; close it "
                "with the surrounding 'with db.read_transaction()' block first."
            )

    def _release_connection(self) -> None:
        """Close and release the current thread's connection back to the pool."""
        with self.lock:
            tid = threading.get_ident()
            conn = self.connections.pop(tid, None)
            if conn is not None:
                conn.close()

    def _flush_tables(self) -> None:
        """Clear the table metadata after transaction rollbacks."""
        for table in self._tables.values():
            table._table = None

    def _auto_commit(self) -> None:
        """Commit pending changes when not in an explicit transaction.

        In SQLAlchemy 2.x, connections use "autobegin" which starts a
        transaction on first use. This method commits that transaction
        after each write operation when the user has not started an
        explicit transaction via ``begin()``/``with db:``.
        """
        if not self.in_transaction:
            self.executable.commit()

    def begin(self) -> None:
        """Enter a transaction explicitly.

        No data will be written until the transaction has been committed.
        """
        self._ensure_writable()
        if not hasattr(self.local, "tx"):
            self.local.tx = []
        if not self.executable.in_transaction():
            # No active transaction; start an explicit one (master semantics).
            self.local.tx.append(self.executable.begin())
        else:
            # An autobegin transaction is already active (e.g., from a read);
            # track the nesting depth without starting a second transaction.
            self.local.tx.append(True)

    def commit(self) -> None:
        """Commit the current transaction.

        Make all statements executed since the transaction was begun permanent.
        """
        if hasattr(self.local, "tx") and self.local.tx:
            tx = self.local.tx.pop()
            if not self.local.tx:
                if tx is not True:
                    tx.commit()
                else:
                    self.executable.commit()
                self._release_connection()

    def rollback(self) -> None:
        """Roll back the current transaction.

        Discard all statements executed since the transaction was begun.
        """
        if hasattr(self.local, "tx") and self.local.tx:
            tx = self.local.tx.pop()
            if not self.local.tx:
                if tx is not True:
                    tx.rollback()
                else:
                    self.executable.rollback()
                self._release_connection()
            self._flush_tables()

    def __enter__(self) -> "Database":
        """Start a transaction."""
        self.begin()
        return self

    def __exit__(
        self, error_type: object, error_value: object, traceback: object
    ) -> None:
        """End a transaction by committing or rolling back."""
        if error_type is None:
            try:
                self.commit()
            except Exception:
                self.rollback()
                raise
        else:
            self.rollback()

    def close(self) -> None:
        """Close all database connections and dispose of the engine.

        Releases all pooled connections and makes this object unusable.
        This should be called when the database is no longer needed,
        especially in multi-threaded or connection-pooled setups.
        """
        with self.lock:
            for conn in self.connections.values():
                conn.close()
            self.connections.clear()
        if self.engine is not None:
            self.engine.dispose()
        self._tables = {}
        self.engine = None

    @property
    def tables(self) -> list[str]:
        """Get a listing of all tables that exist in the database."""
        return self.inspect.get_table_names(schema=self.schema)

    @property
    def views(self) -> list[str]:
        """Get a listing of all views that exist in the database."""
        return self.inspect.get_view_names(schema=self.schema)

    def __contains__(self, table_name: str) -> bool:
        """Check if the given table name exists in the database."""
        try:
            table_name = normalize_table_name(table_name)
            if table_name in self.tables:
                return True
            return table_name in self.views
        except ValueError:
            return False

    def create_table(
        self,
        table_name: str,
        primary_id: str | Literal[False] | None = None,
        primary_type: ColumnType | None = None,
        primary_increment: bool | None = None,
    ) -> Table:
        """Create a new table.

        Either loads a table or creates it if it doesn't exist yet. You can
        define the name and type of the primary key field, if a new table is to
        be created. The default is to create an auto-incrementing integer,
        ``id``. You can also set the primary key to be a string or big integer.
        The caller will be responsible for the uniqueness of ``primary_id`` if
        it is defined as a text type. You can disable auto-increment behaviour
        for numeric primary keys by setting `primary_increment` to `False`.

        Returns a :py:class:`Table <dataset.Table>` instance.
        ::

            table = db.create_table('population')

            # custom id and type
            table2 = db.create_table('population2', 'age')
            table3 = db.create_table('population3',
                                     primary_id='city',
                                     primary_type=db.types.text)
            # custom length of String
            table4 = db.create_table('population4',
                                     primary_id='city',
                                     primary_type=db.types.string(25))
            # no primary key
            table5 = db.create_table('population5',
                                     primary_id=False)
        """
        assert not isinstance(primary_type, str), (
            "Text-based primary_type support is dropped, use db.types."
        )
        table_name = normalize_table_name(table_name)
        with self.lock:
            if table_name not in self._tables:
                self._tables[table_name] = Table(
                    self,
                    table_name,
                    primary_id=primary_id,
                    primary_type=primary_type,
                    primary_increment=primary_increment,
                    auto_create=True,
                )
            return self._tables[table_name]

    def load_table(self, table_name: str) -> Table:
        """Load a table.

        This will fail if the tables does not already exist in the database. If
        the table exists, its columns will be reflected and are available on
        the :py:class:`Table <dataset.Table>` object.

        Returns a :py:class:`Table <dataset.Table>` instance.
        ::

            table = db.load_table('population')
        """
        table_name = normalize_table_name(table_name)
        with self.lock:
            if table_name not in self._tables:
                self._tables[table_name] = Table(self, table_name)
            return self._tables[table_name]

    def get_table(
        self,
        table_name: str,
        primary_id: str | Literal[False] | None = None,
        primary_type: ColumnType | None = None,
        primary_increment: bool | None = None,
    ) -> Table:
        """Load or create a table.

        This is now the same as ``create_table``.
        ::

            table = db.get_table('population')
            # you can also use the short-hand syntax:
            table = db['population']
        """
        if not self.ensure_schema:
            return self.load_table(table_name)
        return self.create_table(
            table_name, primary_id, primary_type, primary_increment
        )

    def __getitem__(self, table_name: str) -> Table:
        """Get a given table."""
        return self.get_table(table_name)

    def _ipython_key_completions_(self) -> list[str]:
        """Completion for table names with IPython."""
        return self.tables

    def query(self, query: str | Executable, **kwargs: Any) -> ResultIter:
        """Run a statement on the database directly.

        Allows for the execution of arbitrary read/write queries. A query can
        either be a plain text string, or a `SQLAlchemy expression
        <https://docs.sqlalchemy.org/en/21/tutorial/data_select.html#tutorial-selecting-data>`_.
        If a plain string is passed in, it will be converted to an expression
        automatically.

        Keyword arguments will be used for parameter binding. Use a named bind
        parameter in the query (i.e. ``SELECT * FROM tbl WHERE a = :foo``) and
        pass the value as a keyword argument (i.e. ``foo='bar'``).
        ::

            statement = 'SELECT user, COUNT(*) c FROM photos GROUP BY user'
            for row in db.query(statement):
                print(row['user'], row['c'])

        The returned iterator will yield each result sequentially.
        """
        if isinstance(query, str):
            query = text(query)
        _step = kwargs.pop("_step", QUERY_STEP)
        if _step is False or _step == 0:
            _step = None
        if kwargs:
            rp = self.executable.execute(query, kwargs)
        else:
            rp = self.executable.execute(query)
        return ResultIter(rp, row_type=self.row_type, step=_step)

    def __repr__(self) -> str:
        """Text representation contains the URL."""
        return f"<Database({safe_url(self.url)})>"
