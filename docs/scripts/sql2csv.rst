=======
sql2csv
=======

Description
===========

Executes arbitrary commands against a SQL database and outputs the results as a CSV:

.. code-block:: none

   usage: sql2csv [-h] [-v] [-l] [-V] [--db CONNECTION_STRING] [--query QUERY]
                  [-e ENCODING] [-H] [--null-value [NULL_VALUE]]
                  [FILE]

   Execute a SQL query on a database and output the result to a CSV file.

   positional arguments:
     FILE                  The file to use as the SQL query. If FILE and --query
                           are omitted, the query is piped data via STDIN.

   optional arguments:
     -h, --help            show this help message and exit
     --db CONNECTION_STRING
                           A SQLAlchemy connection string to connect to a
                           database.
     --engine-option ENGINE_OPTION ENGINE_OPTION
                           A keyword argument to SQLAlchemy's create_engine(), as
                           a space-separated pair. This option can be specified
                           multiple times. For example: thick_mode True
     --execution-option EXECUTION_OPTION EXECUTION_OPTION
                           A keyword argument to SQLAlchemy's
                           execution_options(), as a space-separated pair. This
                           option can be specified multiple times. For example:
                           stream_results True
     --query QUERY         The SQL query to execute. Overrides FILE and STDIN.
     -e ENCODING, --encoding ENCODING
                           Specify the encoding of the input query file.
     -H, --no-header-row   Do not output column names.
     --null-value [NULL_VALUE]
                           Write SQL NULL values as this marker, so that NULL is
                           distinguishable from empty strings and from strings
                           that literally contain the marker. If the option is
                           given without a value, the default marker "\N" is
                           used. Non-NULL values beginning with the marker are
                           escaped by doubling the marker prefix, making the
                           export reversible: a field equal to the marker reads
                           back as NULL, a field beginning with two markers reads
                           back as the text after the first marker, and every
                           other field reads back as-is. The marker must be
                           non-empty and must not contain line breaks. By
                           default (without this option), NULL is written as an
                           empty field.

Examples
========

Load sample data into a table using :doc:`csvsql` and then query it using `sql2csv`:

.. code-block:: bash

   csvsql --db "sqlite:///dummy.db" --tables "test" --insert examples/dummy.csv
   sql2csv --db "sqlite:///dummy.db" --query "select * from test"

Load data about financial aid recipients into PostgreSQL. Then find the three states that received the most, while also filtering out empty rows:

.. code-block:: bash

   createdb recipients
   csvsql --db "postgresql:///recipients" --tables "fy09" --insert examples/realdata/FY09_EDU_Recipients_by_State.csv
   sql2csv --db "postgresql:///recipients" --query "select * from fy09 where \"State Name\" != '' order by fy09.\"TOTAL\" limit 3"

You can even use it as a simple SQL calculator (in this example an in-memory SQLite database is used as the default):

.. code-block:: bash

   sql2csv --query "select 300 * 47 % 14 * 27 + 7000"

The connection string `accepts parameters <https://docs.sqlalchemy.org/en/rel_1_0/core/engines.html#engine-creation-api>`_. For example, to set the encoding of a MySQL database:

.. code-block:: bash

   sql2csv --db 'mysql://user:pass@host/database?charset=utf8' --query "SELECT myfield FROM mytable"

Distinguish NULL from empty strings
===================================

By default, a SQL NULL is written as an empty CSV field, which is indistinguishable from an empty string. Use the :code:`--null-value` option to write NULL as an explicit marker instead:

.. code-block:: bash

   sql2csv --db "sqlite:///dummy.db" --null-value --query "select * from test"

Without a value, the marker is :code:`\N`, the same convention used by MySQL's :code:`SELECT ... INTO OUTFILE` and PostgreSQL's :code:`COPY ... WITH NULL`. A different marker can be given:

.. code-block:: bash

   sql2csv --db "sqlite:///dummy.db" --null-value '@NULL@' --query "select * from test"

The export is reversible. The encoding rule, applied uniformly to every row of every batch (including streaming, multi-batch result sets), is:

* A SQL NULL is written as the marker.
* A non-NULL value that begins with the marker is written with the marker doubled at the front, so that the marker can appear inside real values without ambiguity. For example, with the default marker, the text :code:`\N` is written :code:`\N\N`, and :code:`\N suffix` is written :code:`\N\N suffix`.
* Every other value (including the empty string) is written unchanged.

To read the data back, treat a field equal to the marker as NULL, remove one leading marker from a field that begins with two markers, and leave every other field as-is. The empty string therefore stays an empty string, and the literal text :code:`NULL` stays the text :code:`NULL`.

When loading the CSV with :doc:`csvsql` or :doc:`in2csv`, the same marker can be passed to :code:`--null-value` (with :code:`--blanks` to keep empty strings empty):

.. code-block:: bash

   csvsql --db "sqlite:///dummy.db" --tables "test" --insert --null-value '\N' --blanks export.csv
