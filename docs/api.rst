
API documentation
=================

Connecting
----------

.. autofunction:: dataset.connect

Database
--------

.. autoclass:: dataset.Database
   :members: tables, views, has_table, get_table, create_table, load_table, query, begin, commit, rollback, close
   :special-members:


Table
-----

.. autoclass:: dataset.Table
   :members: exists, columns, find, find_page, find_pending, find_one, all, count, count_matches, count_pending, count_excluded, counts, filter, distinct, insert, insert_ignore, insert_many, update, update_many, upsert, upsert_many, delete, create_column, create_column_by_example, drop_column, create_index, drop, has_column, has_index
   :special-members: __len__, __iter__

Filters
-------

.. autoclass:: dataset.Filter
   :members: all, any, not_, column, raw, from_spec, match_clause, unknown_clause, pending_clause, excluded_clause

.. autoclass:: dataset.FilteredTable
   :members: find, find_pending, paginate, count, count_matches, count_pending, count_excluded, counts

.. autoclass:: dataset.FilterResult
   :members: matched_count, match_count, pending_count, pending_total, counts, total


Data Export
-----------

  **Note:** Data exporting has been extracted into a stand-alone package, datafreeze. See the relevant repository here_.

.. _here: https://github.com/pudo/datafreeze

