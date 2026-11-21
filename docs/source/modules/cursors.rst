Cursor Objects
==============

.. module:: pymysql.cursors

.. autoclass:: Cursor
   :members:
   :exclude-members: DataError, DatabaseError, Error, InterfaceError,
                     IntegrityError, InternalError, NotSupportedError,
                     OperationalError, ProgrammingError, Warning

.. autoclass:: SSCursor
   :members:

.. autoclass:: DictCursor
   :members:

.. autoclass:: SSDictCursor
   :members:

.. autoclass:: WriteProgress
   :members: status, batch, states, pending, pulled, affected_rows, landed_rows,
             total, transaction_state

.. autoclass:: BatchRecord
   :members: range, indices, count, state, execution_state, error,
             affected_rows, sql, sent, landed
