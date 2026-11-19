=======
csvstat
=======

Description
===========

Prints descriptive statistics for all columns in a CSV file. Will intelligently determine the type of each column and then print analysis relevant to that type (ranges for dates, mean and median for integers, etc.):

.. code-block:: bash

   usage: csvstat [-h] [-d DELIMITER] [-t] [-q QUOTECHAR] [-u {0,1,2,3}] [-b]
                  [-p ESCAPECHAR] [-z FIELD_SIZE_LIMIT] [-e ENCODING] [-L LOCALE]
                  [-S] [--blanks] [--null-value NULL_VALUES [NULL_VALUES ...]]
                  [--date-format DATE_FORMAT] [--datetime-format DATETIME_FORMAT]
                  [-H] [-K SKIP_LINES] [-v] [-l] [--zero] [-V] [--csv] [--json]
                  [-i INDENT] [-n] [-c COLUMNS] [--type] [--nulls] [--non-nulls]
                  [--unique] [--min] [--max] [--sum] [--mean] [--median]
                  [--stdev] [--len] [--max-precision] [--freq]
                  [--freq-count FREQ_COUNT] [--count]
                  [--decimal-format DECIMAL_FORMAT] [-G] [-y SNIFF_LIMIT] [-I]
                  [-s] [--chunk-size CHUNK_SIZE] [FILE]

   Print descriptive statistics for each column in a CSV file.

   positional arguments:
     FILE                  The CSV file to operate on. If omitted, will accept
                           input as piped data via STDIN.

   optional arguments:
     -h, --help            show this help message and exit
     --csv                 Output results as a CSV table, rather than plain text.
     --json                Output results as JSON text, rather than plain text.
     -i INDENT, --indent INDENT
                           Indent the output JSON this many spaces. Disabled by
                           default.
     -n, --names           Display column names and indices from the input CSV
                           and exit.
     -c COLUMNS, --columns COLUMNS
                           A comma-separated list of column indices, names or
                           ranges to be examined, e.g. "1,id,3-5". Defaults to
                           all columns.
     --type                Only output data type.
     --nulls               Only output whether columns contains nulls.
     --non-nulls           Only output counts of non-null values.
     --unique              Only output counts of unique values.
     --min                 Only output smallest values.
     --max                 Only output largest values.
     --sum                 Only output sums.
     --mean                Only output means.
     --median              Only output medians.
     --stdev               Only output standard deviations.
     --len                 Only output the length of the longest values.
     --max-precision       Only output the most decimal places.
     --freq                Only output lists of frequent values.
     --freq-count FREQ_COUNT
                           The maximum number of frequent values to display.
     --count               Only output total row count.
     --decimal-format DECIMAL_FORMAT
                           %-format specification for printing decimal numbers.
                           Defaults to locale-specific formatting with "%.3f".
     -G, --no-grouping-separator
                           Do not use grouping separators in decimal numbers.
     -y SNIFF_LIMIT, --snifflimit SNIFF_LIMIT
                           Limit CSV dialect sniffing to the specified number of
                           bytes. Specify "0" to disable sniffing entirely, or
                           "-1" to sniff the entire file.
     -I, --no-inference    Disable type inference (and --locale, --date-format,
                           --datetime-format, --no-leading-zeroes) when parsing
                           the input.
     -s, --stream          Read the input one row at a time, instead of loading
                           it into memory. Unique value counts are estimated;
                           estimated counts are marked as such. Null and
                           non-null counts are exact.
     --chunk-size CHUNK_SIZE
                           With --stream, read the input in chunks of this many
                           rows. The estimates do not depend on the chunk size.

See also: :doc:`../common_arguments`.

By default, :doc:`csvstat` reads the entire input into memory. For large files, use
:code:`--stream` to read the input one row at a time. In streaming mode:

*  The row count, and the number of null and non-null values, are exact.
*  The number of unique values is estimated, using HyperLogLog. The estimate has a relative
   standard error of about 0.8%.
*  Estimates are explicitly marked: in the plain text output with :code:`(estimated)`, in the
   JSON output with an :code:`estimated` list, and in the CSV output with an :code:`estimated`
   column.
*  The estimate doesn't depend on how the input is split into chunks. Processing a file in one
   pass, or merging estimates from separate chunks, produces the same result.
*  Statistics that require all values, like the median, are computed from a bounded sample when a
   column has many values. Use the default buffering mode, or a smaller input, for exact results.

Examples
========

Basic use:

.. code-block:: bash

   csvstat examples/realdata/FY09_EDU_Recipients_by_State.csv

When an statistic name is passed, only that stat will be printed:

.. code-block:: console

   $ csvstat --min examples/realdata/FY09_EDU_Recipients_by_State.csv
     1. State Name: None
     2. State Abbreviate: None
     3. Code: 1
     4. Montgomery GI Bill-Active Duty: 435
     5. Montgomery GI Bill- Selective Reserve: 48
     6. Dependents' Educational Assistance: 118
     7. Reserve Educational Assistance Program: 60
     8. Post-Vietnam Era Veteran's Educational Assistance Program: 1
     9. TOTAL: 768
    10. j: None

If a single stat *and* a single column are requested, only a value will be returned:

.. code-block:: console

   $ csvstat -c 4 --mean examples/realdata/FY09_EDU_Recipients_by_State.csv
   6,263.904

To read a large file one row at a time, and estimate the number of unique values:

.. code-block:: console

   $ csvstat --stream -c county examples/realdata/ks_1033_data.csv
     2. "county"

	Type of data:          Text
	Contains null values:  False
	Non-null values:       1575
	Unique values:         73 (estimated)
	Longest value:         12 characters
	Most common values:    WYANDOTTE (123x)
	                       FINNEY (103x)
	                       SHAWNEE (77x)
	                       SEDGWICK (63x)
	                       SALINE (59x)

   Row count: 1575
