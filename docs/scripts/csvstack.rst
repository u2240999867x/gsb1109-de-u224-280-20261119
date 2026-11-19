========
csvstack
========

Description
===========

Stack up the rows from multiple CSV files, optionally adding a grouping value to each row:

.. code-block:: none

   usage: csvstack [-h] [-d DELIMITER] [-t] [-q QUOTECHAR] [-u {0,1,2,3}] [-b]
                   [-p ESCAPECHAR] [-z FIELD_SIZE_LIMIT] [-e ENCODING] [-S] [-H]
                   [-K SKIP_LINES] [-v] [-l] [--zero] [-V] [-g GROUPS]
                   [-n GROUP_NAME] [--filenames] [--dialect FILE=DIALECT]
                   [--dialect-file FILE]
                   [FILE [FILE ...]]

   Stack up the rows from multiple CSV files, optionally adding a grouping value.

   positional arguments:
     FILE                  The CSV file(s) to operate on. If omitted, will accept
                           input as piped data via STDIN.

   optional arguments:
     -h, --help            show this help message and exit
     -g GROUPS, --groups GROUPS
                           A comma-separated list of values to add as "grouping
                           factors", one per CSV being stacked. These are added
                           to the output as a new column. You may specify a name
                           for the new column using the -n flag.
     -n GROUP_NAME, --group-name GROUP_NAME
                           A name for the grouping column, e.g. "year". Only used
                           when also specifying -g.
     --filenames           Use the filename of each input file as its grouping
                           value. When specified, -g will be ignored.
     --dialect FILE=DIALECT, --dialects FILE=DIALECT
                           Specify the dialect of one input file, as
                           FILE=DIALECT. Repeat for each input. The dialect can
                           be a name (semicolon, tab, comma, pipe), a single
                           delimiter, or key=value options (delimiter, quotechar,
                           quoting, doublequote, escapechar, skipinitialspace,
                           strict). A comma-separated list of dialects is
                           applied to the inputs in order. Inputs not specified
                           use the common input options. Output always uses
                           csvkit's default format.
     --dialect-file FILE, --dialects-file FILE
                           Read FILE=DIALECT declarations from a file. Each line
                           can declare one input, or a JSON object can map
                           filenames to dialects. Can be specified multiple
                           times.

See also: :doc:`../common_arguments`.

.. warning::

    If you redirect output to an input file like :code:`csvstack file.csv > file.csv`, the file will grow indefinitely.

Input dialects
==============

By default, every input is read with the common :doc:`../common_arguments`. If inputs use different delimiters or
quoting conventions, declare each input's dialect separately:

.. code-block:: bash

   csvstack --dialect daily-01.csv=semicolon --dialect daily-02.tsv=tab daily-01.csv daily-02.tsv

The declaration is ``FILE=DIALECT``. ``FILE`` can be a path, a basename, ``-`` for standard input, or a 1-based input
position. ``DIALECT`` can be a name (``comma``, ``semicolon``, ``tab``, ``pipe``), a single delimiter, or a
space-separated list of ``key=value`` options: ``delimiter``, ``quotechar``, ``quoting``, ``doublequote``,
``escapechar``, ``skipinitialspace`` and ``strict``. For example:

.. code-block:: bash

   csvstack --dialect "daily-01.csv=delimiter=; quotechar='\\"'" daily-01.csv

A comma-separated list of dialects is applied to the inputs in order:

.. code-block:: bash

   csvstack --dialect "semicolon,tab" daily-01.csv daily-02.tsv

Inputs not listed in a declaration are read with the common input options, as before. The output is always written
with csvkit's default format (comma-delimited, double-quoted, Unix line endings), regardless of the input dialects.

Declarations can also be kept in a file, one ``FILE=DIALECT`` per line, with blank lines and ``#`` comments ignored:

.. code-block:: text

   # daily reports from the same batch
   daily-01.csv=semicolon
   daily-02.tsv=tab

.. code-block:: bash

   csvstack --dialect-file daily-dialects.txt daily-01.csv daily-02.tsv

The file can instead be a JSON object that maps filenames to dialects. A header row of ``file,dialect`` is also
accepted.

If an input does not match its declared dialect, :code:`csvstack` exits with status 1 and reports the input filename
and the source line at which the bad record started, for example:

.. code-block:: text

   daily-01.csv: line 3: unexpected end of data

Examples
========

Join a set of files for different years:

.. code-block:: bash

   csvstack -g 2009,2010 examples/realdata/FY09_EDU_Recipients_by_State.csv examples/realdata/Datagov_FY10_EDU_recp_by_State.csv

Add a single column to the left of a CSV:

.. code-block:: bash

   csvstack -n NEWCOL -g "" examples/dummy.csv
