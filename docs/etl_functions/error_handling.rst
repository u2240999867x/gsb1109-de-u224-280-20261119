Error Handling
^^^^^^^^^^^^^^

This section describes Exception classes, ``on_error`` functions and error
handling via SQL.


ETLHelperError
--------------

ETL Helper has a :ref:`variety of Exception classes <exceptions>`, all of which are subclasses
of the :class:`ETLHelperError <etlhelper.exceptions.ETLHelperError>` base class.

To aid debugging,
the :class:`ETLHelperQueryError <etlhelper.exceptions.ETLHelperQueryError>`,
:class:`ETLHelperExtractError <etlhelper.exceptions.ETLHelperExtractError>` and
:class:`ETLHelperInsertError <etlhelper.exceptions.ETLHelperInsertError>`
classes print the SQL query and the required paramstyle as well as the error
message returned by the database.

.. literalinclude:: ../code_demos/error_handling/demo_extract_error.py
   :language: python

The output is:

.. code:: bash

    etlhelper.exceptions.ETLHelperExtractError: SQL query raised an error.

    SELECT * FROM bad_table

    Required paramstyle: qmark

    no such table: bad_table

also handling errors in SQL e.g. ON CONFLICT

.. _on_error:

on_error
--------

The default behaviour of ``etlhelper`` is to raise an exception on the
first error and abort the transfer. Sometimes it is desirable to ignore
the errors and to do something else with the failed rows. The
``on_error`` parameter allows a function to be passed that is applied to
the failed rows of each chunk. The input is a list of (row, exception)
tuples.

Different examples are given here. The simplest approach is to collect
all the errors into a list to process at the end.

.. code:: python

   errors = []
   etl.executemany(sql, conn, rows, on_error=errors.extend)

   if errors:
       do_something()

Errors can be logged to the ``etlhelper`` logger.

.. literalinclude:: ../code_demos/error_handling/demo_log_error.py
   :language: python

The IDs of failed rows can be written to a file.

.. code:: python

   def write_bad_ids(failed_rows):
       with open('bad_ids.txt', 'at') as out_file:
           for row, exception in failed_rows:
               out_file.write(f"{row.id}\n")

   etl.executemany(sql, conn, rows, on_error=write_bad_ids)

``executemany``, ``load``, ``copy_rows`` and ``copy_table_rows`` can all
take an ``on_error`` parameter. They each return a tuple containing the
number of rows processed and the number of rows that failed.

.. _source_rows:

Source rows of failed outputs
-----------------------------

When a transform function creates output rows from input rows, it can attach
the input rows that produced each output.  If the database rejects an output,
the corresponding failed row carries this source information in its
``sources`` (also available as ``source_rows``) attribute.  This makes it
possible to trace a rejected output back to the original records.

Attach sources with :func:`with_sources <etlhelper.with_sources>` (several
inputs), :func:`with_source <etlhelper.with_source>` (one input) or
:func:`no_source <etlhelper.no_source>` (explicitly no confirmable source):

.. code:: python

   from etlhelper import with_source, with_sources, no_source

   def transform(chunk):
       by_station = {row["station"]: row for row in chunk if not row["void"]}

       comparison = {"kind": "comparison", "value": 20.0}
       flag_a = {"kind": "flag", "value": 1.0}
       flag_b = {"kind": "flag", "value": 1.0}

       yield with_source(flag_a, by_station["A"])
       yield with_sources(comparison, [by_station["A"], by_station["B"]])
       yield with_sources(flag_b, [by_station["B"]])

   def on_error(failed_rows):
       for failed in failed_rows:
           row, exception = failed
           print(f"Rejected {row} due to {exception}")
           print(f"Source rows: {failed.sources}")

Sources must be rows from the same transform input.  They are matched by
object identity, never by field values, record IDs or position in the chunk.
Two outputs with identical fields remain distinct, and a source must have
been part of the input block; an
:class:`ETLHelperInvalidSourceError <etlhelper.exceptions.ETLHelperInvalidSourceError>`
is raised before any output from that block is written if a source was not.

Only business rows are sent to the database: provenance wrappers are removed
before the query parameters are built.  A transform that returns only rows,
and an ``on_error`` function that expects ``(row, exception)`` tuples, continue
to work unchanged.  ``FailedRow`` still behaves as a two-item tuple when
unpacked; source information is available as an attribute.

Source information is recorded per output, not per write batch.  A rejected
output therefore reports the same sources regardless of the ``chunk_size``
used to write it.


Error handling via SQL
----------------------

The ``on_error`` functions allow individual failed rows to be processed,
however this flexibility can come at the expense of speed.
Each chunk of data that contains a bad row will be retried on a row-by-row
basis.

Databases also have methods for handling errors e.g. duplicate primary keys
using SQL.
By customising an INSERT query (which can be programmatically generated with
:func:`generate_insert_query() <etlhelper.generate_insert_query>`) the database
can be instructed how to process such rows.

The following example for SQLite will ignore duplicate rows.
Different databases have different syntax and capabilities, including
``upsert`` and ``merge``.

.. literalinclude:: ../code_demos/error_handling/demo_sql_conflict_error.py
   :language: python

The output is:

.. code:: bash

   {'id': 1, 'name': 'basalt', 'grain_size': 'fine'}
