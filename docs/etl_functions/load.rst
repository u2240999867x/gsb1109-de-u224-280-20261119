
Load
^^^^

Functions
---------

ETL Helper provides two functions for loading of data.

- :func:`load() <etlhelper.load>`: Inserts data from an iterable of dictionaries
  or namedtuples into an existing target table.

.. literalinclude:: ../code_demos/load/demo_load_min.py
   :language: python

NOTE: the ``load`` function uses the first row of data to generate the
list of column for the insert query. If later items in the data contain
extra columns, those columns will not be inserted and no error will be
raised.

Column mappings
---------------

Source records do not have to use the same field names as the target
table.  Pass a ``column_mapping`` that maps each target column to the
source field that supplies it.  Values are bound by position in the
order of the mapping, so the order of keys inside each source
dictionary is irrelevant, and fields absent from the mapping (e.g.
diagnostic fields) are never written to business columns.

.. code-block:: python

    mapping = {
        "ReportCode": "code",
        "Station": "station",
        "Quality": "quality",
    }
    load("sensor_data", conn, rows, column_mapping=mapping)

Records produced by different collector versions can use different
source field names for the same target column.  In that case, map the
target column to a dictionary of source fields keyed by version and
pass the name of the field that identifies each record's version as
``version_field``.  The value of the version field selects the source
field declared by that record.

.. code-block:: python

    # Old collector uses `code`; the new collector uses `report_code`.
    # On old records `report_code` is only a diagnostic field.
    mapping = {
        "ReportCode": {"v1": "code", "v2": "report_code"},
        "Station": "station",
        "Quality": "quality",
    }
    load("sensor_data", conn, rows,
         column_mapping=mapping, version_field="collector_version")

If two different source fields would assign the same target column and
there is no way to choose between them, an
``ETLHelperMappingConflictError`` is raised before any data are
written.  Records missing a value for a required target column
(``NOT NULL`` with no default) are rejected instead of being sent to
the database; rejected records are included in the failure count and,
when ``on_error`` is set, passed to it as ``(row, exception)`` tuples.
Nullable columns receive SQL ``NULL`` when their source field is
missing or explicitly ``None``.  Each ``load`` call validates and uses
only its own mapping.

- :func:`executemany() <etlhelper.executemany>`: Applies SQL query with parameters
  supplied by iterable of data.  Customising the SQL query allows fine control.

.. literalinclude:: ../code_demos/load/demo_executemany_named.py
   :language: python
  
The INSERT query must container placeholders with an appropriate format for
the input data e.g. positional for tuples, named for dictionaries. 

.. literalinclude:: ../code_demos/load/demo_executemany_positional.py
   :language: python

:func:`executemany() <etlhelper.executemany>` can also be used with UPDATE commands.

Keyword arguments
-----------------

chunk_size
""""""""""

:func:`load() <etlhelper.load>` uses :func:`executemany() <etlhelper.executemany>`
behind the scenes.
Large datasets are broken into chunks and inserted in batches to reduce the number
of queries, and only one chunk of data is held in memory at any time.
Within a data processing pipeline, the next step can begin as soon as the first
chunk has been processed.
The database connection must remain open until all data have been processed.

The ``chunk_size`` default is 5,000 and it can be set with a keyword
argument.

commit_chunks
"""""""""""""

The ``commit_chunks`` flag defaults to ``True``. This ensures
that an error during a large data transfer doesn’t require all the
records to be sent again. Some work may be required to determine which
records remain to be sent. Setting ``commit_chunks`` to ``False`` will
roll back the entire transfer in case of an error.

transform
"""""""""

The ``transform`` parameter takes a callable (e.g. function) that
transforms the data before returning it.
See the :ref:`Transform <transform>` section for details.

on_error
""""""""

Accepts a Python function that will be applied to failed rows.
See :ref:`on_error <on_error>` section for details.


Return values
-------------

The number of rows that were processed and the number that failed is
returned as a tuple.
