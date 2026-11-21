import re
import warnings

from . import err

#: Regular expression for :meth:`Cursor.executemany`.
#: executemany only supports simple bulk insert.
#: You can use it to load large dataset.
RE_INSERT_VALUES = re.compile(
    r"\s*((?:INSERT|REPLACE)\b.+\bVALUES?\s*)"
    + r"(\(\s*(?:%s|%\([^)]+\)s)\s*(?:,\s*(?:%s|%\([^)]+\)s)\s*)*\))"
    + r'(\s*(?:AS\s+(?:`[^`]+`|"[^"]+"|[0-9A-Za-z_$]+)\s*'
    + r'(?:\(\s*(?:`[^`]+`|"[^"]+"|[0-9A-Za-z_$]+)\s*'
    + r'(?:,\s*(?:`[^`]+`|"[^"]+"|[0-9A-Za-z_$]+)\s*)*\))?\s*)?'
    + r"(?:ON DUPLICATE.*)?);?\s*\Z",
    re.IGNORECASE | re.DOTALL,
)


_CLIENT_ERROR_CODES = range(2000, 2073)


def _backquote_escape(s):
    return s.replace("`", "``")


def _is_server_rejection(exc) -> bool:
    """Return True when *exc* came from an error packet, not the transport.

    Server errors use MySQL server error numbers (normally below 2000 or at
    least 3000) and may carry a SQLSTATE.  Client-side errors use the
    ``CR_*`` range (2000-2072) or are raised by the client without a numeric
    error code at all.  Only an error packet proves that the whole statement
    was rejected; a broken connection leaves its outcome unknown.
    """
    if not isinstance(exc, err.Error):
        return False
    if getattr(exc, "sqlstate", None):
        return True
    if not exc.args:
        return False
    code = exc.args[0]
    if not isinstance(code, int) or code <= 0:
        return False
    return code not in _CLIENT_ERROR_CODES


class _ItemRecord:
    """Progress of one parameter, identified by its original input index."""

    __slots__ = ("index", "state", "batch", "formatted")

    def __init__(self, index):
        self.index = index
        #: One of the ``WriteProgress`` states.
        self.state = WriteProgress.PULLED
        self.batch = None
        #: True once the client has bound and encoded the value.
        self.formatted = False

    def __repr__(self):
        return (
            f"<ItemRecord index={self.index} state={self.state!r}"
            f" formatted={self.formatted}>"
        )


class BatchRecord:
    """One merged statement built by :meth:`Cursor.executemany`.

    ``start`` and ``stop`` are the original input indices of the parameters in
    this statement, as a half-open range ``[start, stop)``.  A batch that has
    been built but not sent (for example the parameter formatted just after a
    length-driven split) keeps ``state == WriteProgress.NOT_SENT``.
    """

    def __init__(self, start):
        self.start = start
        self.stop = start
        #: Current lifecycle step; see the ``WriteProgress`` constants.
        self.state = WriteProgress.NOT_SENT
        #: Last execution outcome, retained after a rollback changes ``state``.
        self.execution_state = None
        #: The server error or transport error for a failed statement.
        self.error = None
        #: Rows reported by the server for an acknowledged statement.
        self.affected_rows = None
        #: Exact statement bytes written for this batch, when it was sent.
        self.sql = None
        self._indices = []

    @property
    def range(self):
        """The original input range as ``(start, stop)`` (``stop`` excluded)."""
        return (self.start, self.stop)

    @property
    def indices(self):
        """Original input indices in this statement, in input order."""
        return tuple(self._indices)

    @property
    def count(self):
        """Number of original parameters in this batch."""
        return len(self._indices)

    @property
    def sent(self):
        """True once the statement was written to the connection."""
        return self.sql is not None

    @property
    def landed(self):
        """True only after a confirmed commit, never merely after an OK."""
        return self.state == WriteProgress.COMMITTED

    def __repr__(self):
        return (
            f"<BatchRecord range={self.range} state={self.state!r} count={self.count}>"
        )


class WriteProgress:
    """Opt-in progress report for :meth:`Cursor.executemany`.

    Pass an instance as ``cursor.executemany(query, args, progress=report)``.
    The report is updated as parameters are pulled from the one-pass iterator,
    formatted on the client, sent in merged statements and acknowledged or
    rejected by the server.  Original input indices are available through
    :meth:`status` and :meth:`batch`.

    The length of the iterator is never assumed: :attr:`total` is always
    ``None`` and parameters that were never pulled have ``None`` status.  A
    parameter that was pulled and formatted but not included in a sent
    statement is reported as :attr:`NOT_SENT`.

    States
    ------
    ``pulled``
        Taken from the iterator but not yet formatted on the client.
    ``not_sent``
        Bound and encoded on the client, but not part of any command written
        to the connection yet.
    ``sent``
        Included in a statement whose write is in progress.
    ``acknowledged``
        The server returned an OK packet inside the current transaction.  This
        does not mean the rows are committed.
    ``rejected``
        The server returned an error packet for the whole statement, so the
        entire original range failed together.
    ``outcome_unknown``
        The connection failed after the statement was sent, so the server may
        or may not have executed it.
    ``rolled_back``
        The transaction was confirmed rolled back, so acknowledged rows did
        not land.
    ``committed``
        The transaction was confirmed committed.
    """

    PULLED = "pulled"
    NOT_SENT = "not_sent"
    SENT = "sent"
    ACKNOWLEDGED = "acknowledged"
    REJECTED = "rejected"
    OUTCOME_UNKNOWN = "outcome_unknown"
    ROLLED_BACK = "rolled_back"
    COMMITTED = "committed"

    #: Alias kept for readability when inspecting a formatted lookahead item.
    FORMATTED = NOT_SENT

    def __init__(self):
        #: Batch records in original input order, including a batch that was
        #: built but never sent.
        self.batches = []
        #: Always ``None``: a one-pass iterator has no known total up front.
        self.total = None
        #: Transaction outcome once the connection confirms it.
        self.transaction_state = "open"
        self._pulled = 0
        self._acknowledged_rows = 0
        self._items = {}
        self._bound = False
        self._connection = None

    @property
    def pulled(self):
        """How many parameters were actually taken from the iterator."""
        return self._pulled

    @property
    def rolled_back(self):
        """True after a ROLLBACK was confirmed for the bound connection."""
        return self.transaction_state == "rolled_back"

    @property
    def affected_rows(self):
        """Rows acknowledged by the server inside the still-open transaction.

        This is not proof that the rows were committed; use :attr:`landed_rows`
        or :attr:`BatchRecord.landed` for that.
        """
        return self._acknowledged_rows

    @property
    def landed_rows(self):
        """Rows belonging to batches whose transaction was confirmed committed."""
        return sum(
            batch.affected_rows or 0
            for batch in self.batches
            if batch.state == self.COMMITTED
        )

    @property
    def pending(self):
        """Original indices that were pulled but never sent."""
        return tuple(
            index
            for index, item in sorted(self._items.items())
            if item.state in (self.PULLED, self.NOT_SENT)
        )

    def status(self, index):
        """Return the state for an original input index, or ``None``.

        ``None`` means the iterator has not yielded that index (its tail is
        unknown), not that the parameter was rejected or left unsent.
        """
        item = self._items.get(index)
        return item.state if item is not None else None

    def batch(self, index):
        """Return the :class:`BatchRecord` containing *index*, if any."""
        item = self._items.get(index)
        return item.batch if item is not None else None

    def item(self, index):
        """Return the internal item record for *index*, if it was pulled."""
        return self._items.get(index)

    def states(self):
        """Map every pulled original index to its current state."""
        return {index: item.state for index, item in self._items.items()}

    def __len__(self):
        return len(self._items)

    def __contains__(self, index):
        return index in self._items

    # -- hooks used by Cursor.executemany() -----------------------------

    def _bind(self, connection):
        if self._bound:
            raise ValueError("WriteProgress instance was already used")
        self._bound = True
        self._connection = connection
        register = getattr(connection, "_register_batch_progress", None)
        if register is not None:
            register(self)

    def _begin_batch(self, start=None):
        return BatchRecord(start)

    def _pulled_item(self, index):
        self._pulled = index + 1
        item = _ItemRecord(index)
        self._items[index] = item
        return item

    def _formatted_item(self, index):
        item = self._items[index]
        item.formatted = True
        if item.batch is None:
            item.state = self.NOT_SENT

    def _add_item(self, batch, index):
        if batch.start is None:
            batch.start = index
        batch._indices.append(index)
        batch.stop = index + 1
        batch.state = self.NOT_SENT
        self._items[index].batch = batch
        if batch not in self.batches:
            self.batches.append(batch)

    def _set_batch_state(self, batch, state):
        if batch is None:
            return
        batch.state = state
        for index in batch._indices:
            self._items[index].state = state

    def _sent_batch(self, batch, sql):
        if batch is None:
            return
        batch.sql = bytes(sql)
        batch.execution_state = self.SENT
        self._set_batch_state(batch, self.SENT)

    def _acknowledge_batch(self, batch, affected_rows):
        if batch is None:
            return
        batch.affected_rows = affected_rows
        batch.execution_state = self.ACKNOWLEDGED
        self._acknowledged_rows += affected_rows or 0
        self._set_batch_state(batch, self.ACKNOWLEDGED)

    def _fail_batch(self, batch, exc):
        if batch is None:
            return
        state = self.REJECTED if _is_server_rejection(exc) else self.OUTCOME_UNKNOWN
        batch.error = exc
        batch.execution_state = state
        self._set_batch_state(batch, state)
        if state == self.OUTCOME_UNKNOWN:
            self.transaction_state = "unknown"

    def _mark_rolled_back(self):
        """Called only after the server confirms a ROLLBACK."""
        self.transaction_state = "rolled_back"
        for batch in self.batches:
            if batch.state == self.ACKNOWLEDGED:
                self._set_batch_state(batch, self.ROLLED_BACK)

    def _mark_committed(self):
        """Called only after the server confirms a COMMIT."""
        self.transaction_state = "committed"
        for batch in self.batches:
            if batch.state == self.ACKNOWLEDGED:
                self._set_batch_state(batch, self.COMMITTED)


class Cursor:
    """
    This is the object used to interact with the database.

    Do not create an instance of a Cursor yourself. Call
    connections.Connection.cursor().

    See `Cursor <https://www.python.org/dev/peps/pep-0249/#cursor-objects>`_ in
    the specification.
    """

    #: Max statement size which :meth:`executemany` generates.
    #:
    #: Max size of allowed statement is max_allowed_packet - packet_header_size.
    #: Default value of max_allowed_packet is 1048576.
    max_stmt_length = 1024000

    def __init__(self, connection):
        self.connection = connection
        self.warning_count = 0
        self.description = None
        self.rownumber = 0
        self.rowcount = -1
        self.arraysize = 1
        self._executed = None
        self._result = None
        self._rows = None

    def close(self):
        """
        Closing a cursor just exhausts all remaining data.
        """
        conn = self.connection
        if conn is None:
            return
        try:
            while self.nextset():
                pass
        finally:
            self.connection = None

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        del exc_info
        self.close()

    def _get_db(self):
        if not self.connection:
            raise err.ProgrammingError("Cursor closed")
        return self.connection

    def _check_executed(self):
        if not self._executed:
            raise err.ProgrammingError("execute() first")

    def _conv_row(self, row):
        return row

    def setinputsizes(self, *args):
        """Does nothing, required by DB API."""

    def setoutputsizes(self, *args):
        """Does nothing, required by DB API."""

    def _nextset(self, unbuffered=False):
        """Get the next query set."""
        conn = self._get_db()
        current_result = self._result
        if current_result is None or current_result is not conn._result:
            return None
        if not current_result.has_next:
            return None
        self._result = None
        self._clear_result()
        conn.next_result(unbuffered=unbuffered)
        self._do_get_result()
        return True

    def nextset(self):
        return self._nextset(False)

    def _mogrify(self, query, args) -> str:
        """Return query after binding args."""
        escape = self._get_db().escape

        if isinstance(args, dict):
            args = {key: escape(item) for key, item in args.items()}
        elif isinstance(args, (list, tuple)):
            args = tuple(map(escape, args))
        else:
            # Escaping a single argument is not part of the DB-API or mysqlclient.
            # It will be removed in the next version.
            warnings.warn(
                "single argument is deprecated and will be removed in the next version.",
                DeprecationWarning,
                stacklevel=3,
            )
            args = escape(args)
        try:
            return query % args
        except TypeError as m:
            raise err.ProgrammingError(str(m))

    def mogrify(self, query, args=None) -> str:
        """
        Returns the exact string that would be sent to the database by calling the
        execute() method.

        :param query: Query to mogrify.
        :type query: str

        :param args: Parameters used with query. (optional)
        :type args: tuple, list or dict

        :return: The query with argument binding applied.
        :rtype: str

        This method follows the extension to the DB API 2.0 followed by Psycopg.
        """
        if args is None:
            return query

        return self._mogrify(query, args)

    def execute(self, query, args=None):
        """Execute a query.

        :param query: Query to execute.
        :type query: str

        :param args: Parameters used with query. (optional)
        :type args: tuple, list or dict

        :return: Number of affected rows.
        :rtype: int

        If args is a list or tuple, %s can be used as a placeholder in the query.
        If args is a dict, %(name)s can be used as a placeholder in the query.
        """
        while self.nextset():
            pass

        if args is not None:
            query = self._mogrify(query, args)
        result = self._query(query)
        self._executed = query
        return result

    def executemany(self, query, args, progress=None):
        """Run several data against one query.

        :param query: Query to execute.
        :type query: str

        :param args: Sequence of sequences or mappings. It is used as parameter.
        :type args: tuple or list

        :param progress: Optional :class:`WriteProgress` report to update while
            the merged statements are built, sent and acknowledged.  The input
            iterator is consumed exactly once, as before.
        :type progress: WriteProgress or None

        :return: Number of rows affected, if any.
        :rtype: int or None

        This method improves performance on multiple-row INSERT and
        REPLACE. Otherwise it is equivalent to looping over args with
        execute().
        """
        if not args:
            return

        m = RE_INSERT_VALUES.match(query)
        if m:
            q_prefix = m.group(1) % ()
            q_values = m.group(2).rstrip()
            q_postfix = (m.group(3) or "") % ()
            assert q_values[0] == "(" and q_values[-1] == ")"
            return self._do_execute_many(
                q_prefix,
                q_values,
                q_postfix,
                args,
                self.max_stmt_length,
                self._get_db().encoding,
                progress,
            )

        if progress is None:
            self.rowcount = sum(self.execute(query, arg) for arg in args)
            return self.rowcount

        progress._bind(self._get_db())
        rows = 0
        for index, arg in enumerate(args):
            progress._pulled_item(index)
            statement = self._mogrify(query, arg)
            progress._formatted_item(index)
            batch = progress._begin_batch(index)
            progress._add_item(batch, index)
            progress._sent_batch(batch, statement.encode(self._get_db().encoding))
            try:
                affected = self.execute(statement)
            except BaseException as exc:
                progress._fail_batch(batch, exc)
                raise
            progress._acknowledge_batch(batch, affected)
            rows += affected
        self.rowcount = rows
        return rows

    def _do_execute_many(
        self,
        prefix,
        values,
        postfix,
        args,
        max_stmt_length,
        encoding,
        progress=None,
    ):
        encoding = self._get_db().encoding
        if progress is not None:
            progress._bind(self._get_db())
        if isinstance(prefix, str):
            prefix = prefix.encode(encoding)
        if isinstance(postfix, str):
            postfix = postfix.encode(encoding)
        sql = bytearray(prefix)
        args = iter(args)
        rows = 0
        index = 0

        def pull():
            nonlocal index
            arg = next(args)
            if progress is not None:
                progress._pulled_item(index)
            value = self._mogrify(values, arg).encode(encoding)
            if progress is not None:
                progress._formatted_item(index)
            original_index = index
            index += 1
            return original_index, value

        def send(batch):
            nonlocal rows
            statement = sql + postfix
            if progress is not None:
                progress._sent_batch(batch, statement)
            try:
                affected = self.execute(statement)
            except BaseException as exc:
                if progress is not None:
                    progress._fail_batch(batch, exc)
                raise
            if progress is not None:
                progress._acknowledge_batch(batch, affected)
            rows += affected

        first_index, value = pull()
        batch = progress._begin_batch(first_index) if progress is not None else None
        if progress is not None:
            progress._add_item(batch, first_index)
        sql += value

        while True:
            try:
                original_index, value = pull()
            except StopIteration:
                break
            if len(sql) + len(value) + len(postfix) + 1 > max_stmt_length:
                # Start the next batch before sending this one, so a
                # length-driven lookahead is recorded as formatted but not
                # sent even if this send fails.
                next_batch = (
                    progress._begin_batch(original_index)
                    if progress is not None
                    else None
                )
                if progress is not None:
                    progress._add_item(next_batch, original_index)
                send(batch)
                sql = bytearray(prefix)
                batch = next_batch
            else:
                sql += b","
                if progress is not None:
                    progress._add_item(batch, original_index)
            sql += value

        send(batch)
        self.rowcount = rows
        return rows

    def callproc(self, procname, args=()):
        """Execute stored procedure procname with args.

        :param procname: Name of procedure to execute on server.
        :type procname: str

        :param args: Sequence of parameters to use with procedure.
        :type args: tuple or list

        Returns the original args.

        Compatibility warning: PEP-249 specifies that any modified
        parameters must be returned. This is currently impossible
        as they are only available by storing them in a server
        variable and then retrieved by a query. Since stored
        procedures return zero or more result sets, there is no
        reliable way to get at OUT or INOUT parameters via callproc.
        The server variables are named @_procname_n, where procname
        is the parameter above and n is the position of the parameter
        (from zero). Once all result sets generated by the procedure
        have been fetched, you can issue a SELECT @_procname_0, ...
        query using .execute() to get any OUT or INOUT values.

        Compatibility warning: The act of calling a stored procedure
        itself creates an empty result set. This appears after any
        result sets generated by the procedure. This is non-standard
        behavior with respect to the DB-API. Be sure to use nextset()
        to advance through all result sets; otherwise you may get
        disconnected.
        """
        procname_escaped = _backquote_escape(procname)
        conn = self._get_db()

        if args:
            fmt = f"@`_{procname_escaped}_%d`=%s"
            self._query(
                "SET %s"
                % ",".join(
                    fmt % (index, conn.escape(arg)) for index, arg in enumerate(args)
                )
            )
            self.nextset()

        q = "CALL `{}`({})".format(
            procname_escaped,
            ",".join([f"@`_{procname_escaped}_{i}`" for i in range(len(args))]),
        )
        self._query(q)
        self._executed = q
        return args

    def fetchone(self):
        """Fetch the next row."""
        self._check_executed()
        if self._rows is None or self.rownumber >= len(self._rows):
            return None
        result = self._rows[self.rownumber]
        self.rownumber += 1
        return result

    def fetchmany(self, size=None):
        """Fetch several rows."""
        self._check_executed()
        if self._rows is None:
            # Django expects () for EOF.
            # https://github.com/django/django/blob/0c1518ee429b01c145cf5b34eab01b0b92f8c246/django/db/backends/mysql/features.py#L8
            return ()
        end = self.rownumber + (size or self.arraysize)
        result = self._rows[self.rownumber : end]
        self.rownumber = min(end, len(self._rows))
        return result

    def fetchall(self):
        """Fetch all the rows."""
        self._check_executed()
        if self._rows is None:
            return []
        if self.rownumber:
            result = self._rows[self.rownumber :]
        else:
            result = self._rows
        self.rownumber = len(self._rows)
        return result

    def scroll(self, value, mode="relative"):
        self._check_executed()
        if mode == "relative":
            r = self.rownumber + value
        elif mode == "absolute":
            r = value
        else:
            raise err.ProgrammingError("unknown scroll mode %s" % mode)

        if not (0 <= r < len(self._rows)):
            raise IndexError("out of range")
        self.rownumber = r

    def _query(self, q):
        conn = self._get_db()
        self._clear_result()
        conn.query(q)
        self._do_get_result()
        return self.rowcount

    def _clear_result(self):
        self.rownumber = 0
        self._result = None

        self.rowcount = 0
        self.warning_count = 0
        self.description = None
        self.lastrowid = None
        self._rows = None

    def _do_get_result(self):
        conn = self._get_db()

        self._result = result = conn._result

        self.rowcount = result.affected_rows
        self.warning_count = result.warning_count
        self.description = result.description
        self.lastrowid = result.insert_id
        self._rows = result.rows

    def __iter__(self):
        return self

    def __next__(self):
        row = self.fetchone()
        if row is None:
            raise StopIteration
        return row


class DictCursorMixin:
    # You can override this to use OrderedDict or other dict-like types.
    dict_type = dict

    def _do_get_result(self):
        super()._do_get_result()
        fields = []
        if self.description:
            for f in self._result.fields:
                name = f.name
                if name in fields:
                    name = f.table_name + "." + name
                fields.append(name)
            self._fields = fields

        if fields and self._rows:
            self._rows = [self._conv_row(r) for r in self._rows]

    def _conv_row(self, row):
        if row is None:
            return None
        return self.dict_type(zip(self._fields, row))


class DictCursor(DictCursorMixin, Cursor):
    """A cursor which returns results as a dictionary"""


class SSCursor(Cursor):
    """
    Unbuffered Cursor, mainly useful for queries that return a lot of data,
    or for connections to remote servers over a slow network.

    Instead of copying every row of data into a buffer, this will fetch
    rows as needed. The upside of this is the client uses much less memory,
    and rows are returned much faster when traveling over a slow network
    or if the result set is very big.

    There are limitations, though. The MySQL protocol doesn't support
    returning the total number of rows, so the only way to tell how many rows
    there are is to iterate over every row returned. Also, it currently isn't
    possible to scroll backwards, as only the current row is held in memory.
    """

    def _conv_row(self, row):
        return row

    def close(self):
        conn = self.connection
        if conn is None:
            return

        if self._result is not None and self._result is conn._result:
            self._result._finish_unbuffered_query()

        try:
            while self.nextset():
                pass
        finally:
            self.connection = None

    __del__ = close

    def _query(self, q):
        conn = self._get_db()
        self._clear_result()
        conn.query(q, unbuffered=True)
        self._do_get_result()
        return self.rowcount

    def nextset(self):
        return self._nextset(unbuffered=True)

    def read_next(self):
        """Read next row."""
        return self._conv_row(self._result._read_rowdata_packet_unbuffered())

    def fetchone(self):
        """Fetch next row."""
        self._check_executed()
        row = self.read_next()
        if row is None:
            self.warning_count = self._result.warning_count
            return None
        self.rownumber += 1
        return row

    def fetchall(self):
        """
        Fetch all, as per MySQLdb. Pretty useless for large queries, as
        it is buffered. See fetchall_unbuffered(), if you want an unbuffered
        generator version of this method.
        """
        return list(self.fetchall_unbuffered())

    def fetchall_unbuffered(self):
        """
        Fetch all, implemented as a generator, which isn't to standard,
        however, it doesn't make sense to return everything in a list, as that
        would use ridiculous memory for large result sets.
        """
        return iter(self.fetchone, None)

    def fetchmany(self, size=None):
        """Fetch many."""
        self._check_executed()
        if size is None:
            size = self.arraysize

        rows = []
        for i in range(size):
            row = self.read_next()
            if row is None:
                self.warning_count = self._result.warning_count
                break
            rows.append(row)
            self.rownumber += 1
        if not rows:
            # Django expects () for EOF.
            # https://github.com/django/django/blob/0c1518ee429b01c145cf5b34eab01b0b92f8c246/django/db/backends/mysql/features.py#L8
            return ()
        return rows

    def scroll(self, value, mode="relative"):
        self._check_executed()

        if mode == "relative":
            if value < 0:
                raise err.NotSupportedError(
                    "Backwards scrolling not supported by this cursor"
                )

            for _ in range(value):
                self.read_next()
            self.rownumber += value
        elif mode == "absolute":
            if value < self.rownumber:
                raise err.NotSupportedError(
                    "Backwards scrolling not supported by this cursor"
                )

            end = value - self.rownumber
            for _ in range(end):
                self.read_next()
            self.rownumber = value
        else:
            raise err.ProgrammingError("unknown scroll mode %s" % mode)


class SSDictCursor(DictCursorMixin, SSCursor):
    """An unbuffered cursor, which returns results as a dictionary"""
