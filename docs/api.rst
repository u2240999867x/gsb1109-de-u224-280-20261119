API
===

etlhelper
^^^^^^^^^

.. automodule:: etlhelper
    :members:


etlhelper.row_factories
^^^^^^^^^^^^^^^^^^^^^^^

.. automodule:: etlhelper.row_factories
    :members:


etlhelper.backfill
^^^^^^^^^^^^^^^^^^

.. automodule:: etlhelper.backfill
    :members: backfill, copy_rows_resumable, create_backfill_tables,
              confirmed_coverage, rejected_events, BackfillResult,
              BackfillTables, EventRange


.. _db_helpers:

DB Helpers
^^^^^^^^^^^

.. automodule:: etlhelper.db_helpers
.. autoclass:: DbHelper
.. autoclass:: SQLiteDbHelper
.. autoclass:: PostgresDbHelper
.. autoclass:: OracleDbHelper
.. autoclass:: MSSQLDbHelper


.. _exceptions:

Exceptions
^^^^^^^^^^^

.. automodule:: etlhelper.exceptions
.. autoclass:: etlhelper.exceptions.ETLHelperError
.. autoclass:: etlhelper.exceptions.ETLHelperConnectionError
.. autoclass:: etlhelper.exceptions.ETLHelperQueryError
.. autoclass:: etlhelper.exceptions.ETLHelperDbParamsError
.. autoclass:: etlhelper.exceptions.ETLHelperExtractError
.. autoclass:: etlhelper.exceptions.ETLHelperInsertError
.. autoclass:: etlhelper.exceptions.ETLHelperAbort
.. autoclass:: etlhelper.exceptions.ETLHelperHelperError
.. autoclass:: etlhelper.exceptions.ETLHelperBadIdentifierError
.. autoclass:: etlhelper.exceptions.ETLHelperBackfillError
.. autoclass:: etlhelper.exceptions.ETLHelperCommitUnconfirmedError
