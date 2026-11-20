API
===

etlhelper
^^^^^^^^^

.. automodule:: etlhelper
    :members:


etlhelper.provenance
^^^^^^^^^^^^^^^^^^^^

.. automodule:: etlhelper.provenance
    :members: Provenance, SourceRef, Output, NO_SOURCE, with_source,
              with_sources, with_provenance, no_source, unwrap_row,
              get_sources, get_provenance


etlhelper.row_factories
^^^^^^^^^^^^^^^^^^^^^^^

.. automodule:: etlhelper.row_factories
    :members:


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
.. autoclass:: etlhelper.exceptions.ETLHelperInvalidSourceError
