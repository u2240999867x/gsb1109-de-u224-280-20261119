import re
import warnings
from dataclasses import dataclass

from . import err

#: A result set is still being read.
RESULT_SET_READING = "reading"

#: A result set reached its natural end (EOF).
RESULT_SET_COMPLETED = "completed"

#: A result set was left before EOF, e.g. by nextset() or close().
RESULT_SET_ABANDONED = "abandoned"

#: Reading a result set raised an error.
RESULT_SET_FAILED = "failed"


@dataclass
class ResultSetRecord:
    """Delivery state for one column-bearing result set.

    ``delivered_rows`` counts only rows returned to the caller by
    ``fetchone()``, ``fetchmany()``, ``fetchall()`` or iteration. Rows
    consumed by ``scroll()`` or discarded while moving to another result
    set are not included.

    The ``status`` is one of :data:`RESULT_SET_READING`,
    :data:`RESULT_SET_COMPLETED`, :data:`RESULT_SET_ABANDONED`, or
    :data:`RESULT_SET_FAILED`. A failed read stays failed even if later
    cleanup successfully drains the rest of the result set.
    """

    index: int
    delivered_rows: int = 0
    skipped_rows: int = 0
    discarded_rows: int = 0
    status: str = RESULT_SET_READING

    @property
    def rowcount(self):
        """Alias for :attr:`delivered_rows`."""
        return self.delivered_rows

    @property
    def end_status(self):
        """Alias for :attr:`status`."""
        return self.status

    @property
    def completed(self):
        return self.status == RESULT_SET_COMPLETED

    @property
    def abandoned(self):
        return self.status == RESULT_SET_ABANDONED

    @property
    def failed(self):
        return self.status == RESULT_SET_FAILED


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


def _backquote_escape(s):
    return s.replace("`", "``")


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

    def executemany(self, query, args):
        """Run several data against one query.

        :param query: Query to execute.
        :type query: str

        :param args: Sequence of sequences or mappings. It is used as parameter.
        :type args: tuple or list

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
            )

        self.rowcount = sum(self.execute(query, arg) for arg in args)
        return self.rowcount

    def _do_execute_many(
        self, prefix, values, postfix, args, max_stmt_length, encoding
    ):
        encoding = self._get_db().encoding
        if isinstance(prefix, str):
            prefix = prefix.encode(encoding)
        if isinstance(postfix, str):
            postfix = postfix.encode(encoding)
        sql = bytearray(prefix)
        args = iter(args)
        sql += self._mogrify(values, next(args)).encode(encoding)
        rows = 0
        for arg in args:
            v = self._mogrify(values, arg).encode(encoding)
            if len(sql) + len(v) + len(postfix) + 1 > max_stmt_length:
                rows += self.execute(sql + postfix)
                sql = bytearray(prefix)
            else:
                sql += b","
            sql += v
        rows += self.execute(sql + postfix)
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

    def __init__(self, connection):
        super().__init__(connection)
        self._result_sets = []
        self._current_result_set = None
        self._record_result_sets = True

    @property
    def result_sets(self):
        """Records for column-bearing result sets entered by this cursor.

        The records are kept after :meth:`close` so that delivery and end
        status can be inspected later.
        """
        return self._result_sets

    @property
    def result_set_records(self):
        """Alias for :attr:`result_sets`."""
        return self._result_sets

    def get_result_set_records(self):
        """Return a copy of the per-result-set delivery records."""
        return list(self._result_sets)

    def _do_get_result(self):
        super()._do_get_result()
        self._current_result_set = None
        if (
            self._record_result_sets
            and self._result is not None
            and self._result.description is not None
        ):
            self._current_result_set = ResultSetRecord(len(self._result_sets))
            self._result_sets.append(self._current_result_set)

    def _clear_result(self):
        super()._clear_result()
        self._current_result_set = None

    def _record_delivered(self, count):
        record = getattr(self, "_current_result_set", None)
        if count and record is not None:
            record.delivered_rows += count

    def _record_skipped(self, count):
        record = getattr(self, "_current_result_set", None)
        if count and record is not None:
            record.skipped_rows += count

    def _mark_completed(self):
        record = getattr(self, "_current_result_set", None)
        if record is not None and record.status not in (
            RESULT_SET_FAILED,
            RESULT_SET_ABANDONED,
        ):
            record.status = RESULT_SET_COMPLETED

    def _mark_failed(self):
        record = getattr(self, "_current_result_set", None)
        if record is not None and record.status == RESULT_SET_READING:
            record.status = RESULT_SET_FAILED

    def _mark_abandoned(self):
        record = getattr(self, "_current_result_set", None)
        if record is not None and record.status != RESULT_SET_FAILED:
            record.status = RESULT_SET_ABANDONED

    def _finish_current_result_set(self):
        """Discard unread rows of the current result set.

        Returns the number of row packets discarded.
        """
        result = self._result
        if result is None or not result.unbuffered_active:
            # The EOF packet was already consumed, so the result set
            # completed naturally. Nothing was skipped by this operation.
            self._mark_completed()
            return 0

        discarded = result._finish_unbuffered_query()
        record = self._current_result_set
        if record is not None:
            record.discarded_rows += discarded
            if discarded:
                self._mark_abandoned()
            else:
                self._mark_completed()
        return discarded

    def close(self):
        conn = getattr(self, "connection", None)
        if conn is None:
            return

        result = getattr(self, "_result", None)
        if result is not None and result is getattr(conn, "_result", None):
            if result.unbuffered_active:
                try:
                    self._finish_current_result_set()
                except Exception:
                    self._mark_failed()
                    raise

        try:
            self._record_result_sets = False
            while self.nextset():
                pass
        finally:
            self._record_result_sets = True
            self.connection = None

    __del__ = close

    def execute(self, query, args=None):
        conn = self.connection
        if (
            self._result is not None
            and conn is not None
            and self._result is conn._result
            and self._result.unbuffered_active
        ):
            warnings.warn("Previous unbuffered result was left incomplete")
            self._finish_current_result_set()

        # Remaining result sets belong to the previous execution and are
        # drained automatically, not entered by the caller.
        self._record_result_sets = False
        try:
            while self.nextset():
                pass
        finally:
            self._record_result_sets = True

        if args is not None:
            query = self._mogrify(query, args)
        result = self._query(query)
        self._executed = query
        return result

    def _query(self, q):
        conn = self._get_db()
        self._clear_result()
        conn.query(q, unbuffered=True)
        self._do_get_result()
        return self.rowcount

    def nextset(self):
        conn = self._get_db()
        current_result = self._result
        if current_result is None or current_result is not conn._result:
            return None

        if current_result.unbuffered_active:
            try:
                self._finish_current_result_set()
            except Exception:
                self._mark_failed()
                raise

        if not current_result.has_next:
            return None

        self._result = None
        self._clear_result()
        conn.next_result(unbuffered=True)
        self._do_get_result()
        return True

    def read_next(self):
        """Read next row."""
        try:
            row = self._result._read_rowdata_packet_unbuffered()
            row = self._conv_row(row)
        except Exception:
            self._mark_failed()
            raise
        if row is None:
            self._mark_completed()
        return row

    def fetchone(self):
        """Fetch next row."""
        self._check_executed()
        row = self.read_next()
        if row is None:
            self.warning_count = self._result.warning_count
            return None
        self.rownumber += 1
        self._record_delivered(1)
        return row

    def fetchall(self):
        """
        Fetch all, as per MySQLdb. Pretty useless for large queries, as
        it is buffered. See fetchall_unbuffered(), if you want an unbuffered
        generator version of this method.
        """
        self._check_executed()
        rows = []
        while True:
            row = self.read_next()
            if row is None:
                self.warning_count = self._result.warning_count
                break
            rows.append(row)
            self.rownumber += 1
        self._record_delivered(len(rows))
        return rows

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
        self._record_delivered(len(rows))
        return rows

    def scroll(self, value, mode="relative"):
        self._check_executed()

        if mode == "relative":
            if value < 0:
                raise err.NotSupportedError(
                    "Backwards scrolling not supported by this cursor"
                )

            end = self.rownumber + value
        elif mode == "absolute":
            if value < self.rownumber:
                raise err.NotSupportedError(
                    "Backwards scrolling not supported by this cursor"
                )

            end = value
        else:
            raise err.ProgrammingError("unknown scroll mode %s" % mode)

        skipped = 0
        while self.rownumber < end:
            row = self.read_next()
            if row is None:
                break
            skipped += 1
            self.rownumber += 1
        self._record_skipped(skipped)
        self.rownumber = end


class SSDictCursor(DictCursorMixin, SSCursor):
    """An unbuffered cursor, which returns results as a dictionary"""
