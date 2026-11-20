=======
csvjson
=======

Description
===========

Converts a CSV file into JSON or GeoJSON (depending on flags):

.. code-block:: none

   usage: csvjson [-h] [-d DELIMITER] [-t] [-q QUOTECHAR] [-u {0,1,2,3,4,5}]
                  [-b] [-p ESCAPECHAR] [-z FIELD_SIZE_LIMIT] [-e ENCODING]
                  [-L LOCALE] [-S] [--blanks]
                  [--null-value NULL_VALUES [NULL_VALUES ...]]
                  [--date-format DATE_FORMAT] [--datetime-format DATETIME_FORMAT]
                  [--no-leading-zeroes] [-H] [-K SKIP_LINES] [-v] [-l]
                  [--add-bom] [--zero] [-V] [-i INDENT] [-k KEY] [--lat LAT]
                  [--lon LON] [--type TYPE] [--geometry GEOMETRY] [--crs CRS]
                  [--no-bbox] [--stream] [-y SNIFF_LIMIT] [-I] [-s DECLARATION]
                  [--declaration-version DECLARATION_VERSION]
                  [--rejected REJECTED] [--include-undeclared]
                  [--exclude-undeclared]
                  [FILE]

   Convert a CSV file into JSON (or GeoJSON).

   positional arguments:
     FILE                  The CSV file to operate on. If omitted, will accept
                           input as piped data via STDIN.

   optional arguments:
     -h, --help            show this help message and exit
     -i INDENT, --indent INDENT
                           Indent the output JSON this many spaces. Disabled by
                           default.
     -k KEY, --key KEY     Output JSON as an object keyed by a given column, KEY,
                           rather than as an array. All column values must be
                           unique. If --lat and --lon are specified, this column
                           is used as the GeoJSON Feature ID.
     --lat LAT             A column index or name containing a latitude. Output
                           will be GeoJSON instead of JSON. Requires --lon.
     --lon LON             A column index or name containing a longitude. Output
                           will be GeoJSON instead of JSON. Requires --lat.
     --type TYPE           A column index or name containing a GeoJSON type.
                           Output will be GeoJSON instead of JSON. Requires --lat
                           and --lon.
     --geometry GEOMETRY   A column index or name containing a GeoJSON geometry.
                           Output will be GeoJSON instead of JSON. Requires --lat
                           and --lon.
     --crs CRS             A coordinate reference system string to be included
                           with GeoJSON output. Requires --lat and --lon.
     --no-bbox             Disable the calculation of a bounding box.
     --stream              Output JSON as a stream of newline-separated objects,
                           rather than an as an array.
     -y SNIFF_LIMIT, --snifflimit SNIFF_LIMIT
                           Limit CSV dialect sniffing to the specified number of
                           bytes. Specify "0" to disable sniffing entirely, or
                           "-1" to sniff the entire file.
     -I, --no-inference    Disable type inference (and --locale, --date-format,
                           --datetime-format, --no-leading-zeroes) when parsing
                           the input.
     -s DECLARATION, --schema DECLARATION, --declaration DECLARATION
                           A JSON or CSV column declaration that selects, renames
                           and types fields. See `Column declarations`_.
     --declaration-version DECLARATION_VERSION, --schema-version DECLARATION_VERSION
                           The version of the column declaration to apply, if the
                           declaration describes multiple versions.
     --rejected REJECTED   Write rejected records, with their source line range
                           and the declared column, as CSV to this file.
     --include-undeclared  Output input columns that the declaration does not
                           declare (the default).
     --exclude-undeclared  Output only the columns that the declaration declares.

See also: :doc:`../common_arguments`.

Column declarations
===================

A column declaration (``-s/--schema``) selects, renames and types fields. It can be JSON or CSV.

Every field identifies its source column by its **physical position** in the input (1-based, or 0-based with
:code:`--zero`), so a declaration keeps referring to the same source column even if two source headers are
identical. A field can, alternatively, identify its source column by header.

A declared field keeps its value as **text** unless it declares a type. In particular, an identifier like
:code:`00042` keeps its leading zeroes. A declared type can also be a number in a locale's format:

.. code-block:: json

   {
     "versions": {
       "v1": {
         "match": ["编号", "金额"],
         "fields": [
           {"name": "number", "column": 1, "type": "text"},
           {"name": "amount", "column": 2, "type": "number", "locale": "de_DE"}
         ]
       },
       "v2": {
         "match": ["外部编号"],
         "fields": [
           {"name": "number", "column": 1, "type": "text"},
           {"name": "external_id", "column": 2, "type": "text"},
           {"name": "amount", "column": 3, "type": "number", "locale": "de_DE"},
           {"name": "note", "column": 4, "type": "text"}
         ]
       }
     }
   }

A version with a :code:`match` list is selected automatically if the input's header contains all of its headers.
Otherwise, if the declaration has one version, that version is used. If the declaration has multiple versions and
the input matches more than one, the most specific (the version that declares the most fields) is used, and
:code:`--declaration-version` can select a version explicitly.

The same declaration can be written as CSV:

.. code-block:: none

   version,name,column,type,locale
   v2,number,1,text,
   v2,external_id,2,text,
   v2,amount,3,number,de_DE
   v2,note,4,text,

Fields with the same name are kept as distinct JSON keys (for example, :code:`编号` and :code:`编号_2`), rather
than one overwriting the other.

Rejected records
----------------

If a declared field's value can't be converted to its type, only that record is rejected: it is omitted from the
JSON output, and later records keep their correct source line numbers. :code:`--rejected` writes the rejected
records as CSV, with the source line range (``line_start`` and ``line_end``, 1-based, including a record with a
multiline field) and the declared physical ``column``:

.. code-block:: none

   line_start,line_end,column,source_name,field,value,msg
   5,5,3,金额,amount,"1.234,56,7","Unable to convert ""1.234,56,7"" to type number"

A rejection is also summarized on standard error.

The streaming (:code:`--stream`) and array outputs reject the same records in the same order, and emit the same
valid records. A missing required physical column is reported before the first record is output. Set a field's
:code:`required` to :code:`false` in the declaration to skip a missing field instead.

Undeclared columns
------------------

By default, input columns that the declaration doesn't declare are also output, with csvjson's existing behavior:
streaming output emits their raw text, and array output infers their types (unless :code:`--no-inference` is set).
Use :code:`--exclude-undeclared` to output only declared fields.

Examples
========

Convert veteran's education dataset to JSON keyed by state abbreviation:

.. code-block:: console

   $ csvjson -k "State Abbreviate" -i 4 examples/realdata/FY09_EDU_Recipients_by_State.csv
   {
       "AL": {
           "State Name": "ALABAMA",
           "State Abbreviate": "AL",
           "Code": 1.0,
           "Montgomery GI Bill-Active Duty": 6718.0,
           "Montgomery GI Bill- Selective Reserve": 1728.0,
           "Dependents' Educational Assistance": 2703.0,
           "Reserve Educational Assistance Program": 1269.0,
           "Post-Vietnam Era Veteran's Educational Assistance Program": 8.0,
           "TOTAL": 12426.0,
           "j": null
       },
       "...": {
           "...": "..."
       }
   }

Convert locations of public art into GeoJSON:

.. code-block:: console

   $ csvjson --lat latitude --lon longitude --k slug --crs EPSG:4269 -i 4 examples/test_geo.csv
   {
       "type": "FeatureCollection", 
       "bbox": [
           -95.334619, 
           32.299076986939205, 
           -95.250699, 
           32.351434
       ], 
       "crs": {
           "type": "name", 
           "properties": {
               "name": "EPSG:4269"
           }
       }, 
       "features": [
           {
               "type": "Feature", 
               "id": "dcl", 
               "geometry": {
                   "type": "Point", 
                   "coordinates": [
                       -95.30181, 
                       32.35066
                   ]
               }, 
               "properties": {
                   "title": "Downtown Coffee Lounge", 
                   "artist": null, 
                   "description": "In addition to being the only coffee shop in downtown Tyler, DCL also features regular exhibitions of work by local artists.", 
                   "install_date": null, 
                   "address": "200 West Erwin Street", 
                   "type": "Gallery", 
                   "photo_url": null, 
                   "photo_credit": null, 
                   "last_seen_date": "2012-03-30"
               }
           }, 
           {
               "...": "..."
           }
       ], 
       "crs": {
           "type": "name", 
           "properties": {
               "name": "EPSG:4269"
           }
       }
   }
