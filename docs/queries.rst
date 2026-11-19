
.. _advanced_filters:

Advanced filters
================

``dataset`` provides two methods for running queries: :py:meth:`table.find() <dataset.Table.find>`
and :py:meth:`db.query() <dataset.Database.query>`. The table find helper method provides 
limited, but simple filtering options::

    results = table.find(column={operator: value})
    # e.g.:
    results = table.find(name={'like': '%mole rat%'})

A special form is using keyword searches on specific columns::

    results = table.find(value=5)
    # equal to:
    results = table.find(value={'=': 5})

    # Lists, tuples and sets are turned into `IN` queries:
    results = table.find(category=('foo', 'bar'))
    # equal to:
    results = table.find(value={'in': ('foo', 'bar')})

The following comparison operators are supported:

============== ============================================================
Operator       Description
============== ============================================================
gt, >          Greater than
lt, <          Less than
gte, >=        Greater or equal
lte, <=        Less or equal
!=, <>, not    Not equal to a single value
in             Value is in the given sequence
notin          Value is not in the given sequence
like, ilike    Text search, ILIKE is case-insensitive. Use ``%`` as a wildcard
notlike        Like text search, except check if pattern does not exist
between, ..    Value is between two values in the given tuple
startswith     String starts with
endswith       String ends with
============== ============================================================

Querying for a specific value on a column that does not exist on the table
will return no results.

You can also pass `SQLAlchemy core expressions`_ directly into the
:py:meth:`table.find() <dataset.Table.find>` method as positional arguments.
Access the underlying SQLAlchemy table via ``table.table`` and its columns
via ``table.table.columns``::

    from sqlalchemy import or_

    # Get a column object:
    city = table.table.columns.city
    # Use a SQLAlchemy clause:
    results = table.find(city.ilike('amsterda%'))

    # Combine with OR:
    country = table.table.columns.country
    results = table.find(or_(city == 'Amsterdam', country == 'Germany'))

    # Combine SQLAlchemy clauses with keyword filters:
    results = table.find(city.ilike('new%'), country='US')

These clauses also work with :py:meth:`table.count() <dataset.Table.count>`,
:py:meth:`table.find_one() <dataset.Table.find_one>`, and
:py:meth:`table.delete() <dataset.Table.delete>`.

Keyset pagination
-----------------

``_offset`` skips a fixed number of rows, so rows inserted before the current
position shift the rest of the result and a row may be returned twice (or
skipped). For incremental exports, pass the last row of the previous page as
``_after`` instead::

    page = list(table.find(order_by='-updated_at', _limit=100))
    while page:
        process(page)
        page = list(
            table.find(order_by='-updated_at', _limit=100, _after=page[-1])
        )

The primary key is appended to ``order_by`` as an ascending tie-breaker. Rows
with the same ``updated_at`` are therefore ordered by ascending ``id``, and a
descending timestamp sort becomes ``updated_at DESC, id ASC``. Ordering by a
timestamp alone is not stable, since the database may return equal timestamps
in any order.

The cursor condition follows the database's own ``NULL`` ordering. SQLite and
MySQL sort ``NULL`` values before other values, PostgreSQL sorts them after.
In both cases ``NULL`` timestamps are read exactly once, in the same position
as in an unpaged ``order_by`` query::

    # Ascending: NULL timestamps come first on SQLite/MySQL, last on PostgreSQL
    table.find(order_by='updated_at')
    # Descending: NULL timestamps come last on SQLite/MySQL, first on PostgreSQL
    table.find(order_by='-updated_at')

``_after`` accepts the full row as returned by :py:meth:`find`, or a
sequence with one value per effective sort column (the requested columns
plus the primary key). Filtering with keyword arguments still applies to every
page and must be repeated on each call. When ``_after`` is used without
``order_by``, the primary key is used as the sort order, provided the table
has one.

Queries using raw SQL
---------------------

To run more complex queries with JOINs, or to perform GROUP BY-style
aggregation, you can also use :py:meth:`db.query() <dataset.Database.query>`
to run raw SQL queries instead. This also supports parameterisation to avoid
SQL injections::

    statement = 'SELECT user, COUNT(*) c FROM photos GROUP BY user'
    for row in db.query(statement):
        print(row['user'], row['c'])

    # With parameter binding:
    results = db.query('SELECT * FROM users WHERE age > :min_age', min_age=21)

For fully programmatic, composable query building, consider using
`SQLAlchemy core expressions`_ directly.

.. _SQLAlchemy core expressions: https://docs.sqlalchemy.org/en/21/tutorial/data_select.html#tutorial-selecting-data