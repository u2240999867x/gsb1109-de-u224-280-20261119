=======
csvjoin
=======

Description
===========

Merges two or more CSV tables together using a method analogous to SQL JOIN operation. By default it performs an inner join, but full outer, left outer, and right outer are also available via flags. Key columns are specified with the -c flag (either a single column which exists in all tables, or a comma-separated list of columns with one corresponding to each). If the columns flag is not provided then the tables will be merged "sequentially", that is they will be merged in row order with no filtering:

.. code-block:: none

   usage: csvjoin [-h] [-d DELIMITER] [-t] [-q QUOTECHAR] [-u {0,1,2,3}] [-b]
                  [-p ESCAPECHAR] [-z FIELD_SIZE_LIMIT] [-e ENCODING] [-L LOCALE]
                  [-S] [--blanks] [--null-value NULL_VALUES [NULL_VALUES ...]]
                  [--date-format DATE_FORMAT] [--datetime-format DATETIME_FORMAT]
                  [-H] [-K SKIP_LINES] [-v] [-l] [--zero] [-V] [-c COLUMNS]
                  [--outer] [--left] [--right] [-y SNIFF_LIMIT] [-I]
                  [FILE [FILE ...]]

   Execute a SQL-like join to merge CSV files on a specified column or columns.

   positional arguments:
     FILE                  The CSV files to operate on. If only one is specified,
                           it will be copied to STDOUT.

   optional arguments:
     -h, --help            show this help message and exit
     -c COLUMNS, --columns COLUMNS
                           The column name(s) on which to join. Should be either
                           one name (or index) or a comma-separated list with one
                           name (or index) per file, in the same order in which
                           the files were specified. To join on a composite key,
                           separate the per-file column lists with semicolons and
                           the columns within each list with commas, for example
                           "region_a,store_a;region_b,store_b". If not specified,
                           the two files will be joined sequentially without
                           matching.
     --outer               Perform a full outer join, rather than the default
                           inner join.
     --left                Perform a left outer join, rather than the default
                           inner join. If more than two files are provided this
                           will be executed as a sequence of left outer joins,
                           starting at the left.
     --right               Perform a right outer join, rather than the default
                           inner join. If more than two files are provided this
                           will be executed as a sequence of right outer joins,
                           starting at the right.
     --map-file FILE       A CSV file mapping old numbers to current numbers for
                           the store-number component (the last column) of the
                           join key. Must have "old_number" and "new_number"
                           columns and may have a "region" column. Only listed
                           numbers are mapped. Repeat once per input file, in
                           input order, or use the "N:path" form to target input
                           file N (1-based).
     --map-region REGION   Scope a mapping file without a "region" column to one
                           region. Repeat in the same order as --map-file, or
                           give a single value that applies to every mapping
                           file.
     -y SNIFF_LIMIT, --snifflimit SNIFF_LIMIT
                           Limit CSV dialect sniffing to the specified number of
                           bytes. Specify "0" to disable sniffing entirely, or
                           "-1" to sniff the entire file.
     -I, --no-inference    Disable type inference (and --locale, --date-format,
                           --datetime-format, --no-leading-zeroes) when parsing
                           the input.

   Note that the join operation requires reading all files into memory. Don't try
   this on very large files.

See also: :doc:`../common_arguments`.

Composite keys and number mappings
==================================

Store data is often identified by two columns together, such as a region and a
store number, and the column names differ between files. Use semicolons to
separate the per-file column lists and commas to separate the columns within
each list::

   csvjoin -I --outer -c "region,store_no;area,shop_code;zone,outlet_id" \
       transactions.csv catalog.csv deliveries.csv

With a composite key every file is joined simultaneously, rather than in
chained pairs. Rows with a blank key component never match another row and
remain attributed to their own source in the full outer join output.

An upstream file may still use old store numbers. ``--map-file`` points to a
CSV with ``old_number`` and ``new_number`` columns (and an optional ``region``
column) that renumbers the last component of the join key **for key matching
only**. The original columns are written to the output unchanged, so the source
values stay available for verification. Only numbers listed in the mapping are
changed; all other values, including blank ones, are kept as-is. Mappings are
scoped per input file using the 1-based ``N:path`` form::

   csvjoin -I --outer -c "region,store_no;area,shop_code;zone,outlet_id" \
       --map-file 2:catalog_mapping.csv \
       transactions.csv catalog.csv deliveries.csv

If the mapping file has no ``region`` column, scope it explicitly with
``--map-region``. A mapping is only valid for numbers the business has
confirmed to be the same store. When one input declares a single row per
business identity but a mapping collapses two of its source rows onto the same
composite key, the whole batch aborts before writing output and reports both
source records together with the conflicting mapped key.

Examples
========

.. code-block:: bash

   csvjoin -c 1 examples/join_a.csv examples/join_b.csv

Full outer join of three store files on a composite (region, store number) key,
renumbering the catalog's old store numbers for the match::

.. code-block:: bash

   csvjoin -I --outer -c "region,store_no;area,shop_code;zone,outlet_id" \
       --map-file 2:examples/stores/catalog_mapping.csv \
       examples/stores/transactions.csv examples/stores/catalog.csv \
       examples/stores/deliveries.csv

Add two empty columns to the right of a CSV:

.. code-block:: bash

   echo "," | csvjoin examples/dummy.csv -

Add a single column to the right of a CSV:

.. code-block:: bash

   echo "new-column" | csvjoin examples/dummy.csv -
