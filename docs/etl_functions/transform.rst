.. _transform:

Transform
^^^^^^^^^

ETL Helper functions accept a function as the ``transform`` keyword argument,
which enables transformation of data in flight.

This is any Python callable (e.g. function or class) that takes an iterable
and returns another iterable (e.g. list or generator via the ``yield``
statement).
Transform functions are applied to data as they are read
from the database (in the case of data fetching functions and
``copy_rows``), or before they are passed as query parameters (to
``executemany`` or ``load``).
When used with ``copy_rows`` or ``executemany`` the INSERT query must contain
the correct parameter placeholders for the transformed result.

The simplest transform functions modify data returned mutable row
factories e.g., ``dict_row_factory`` in-place. The ``yield`` keyword
makes ``my_transform`` a generator function that returns an ``Iterator``
that can loop over the rows.

.. literalinclude:: ../code_demos/transform/demo_yield.py
   :language: python

It is also possible to assemble the complete transformed chunk and
return it.
This code demonstrates that the returned chunk can have a
different number of rows, and be of different length, to the input.
Because ``namedtuple``\ s are immutable, we have to create a ``new_row``
from each input ``row``.

.. literalinclude:: ../code_demos/transform/demo_return.py
   :language: python

Any Python code can be used within the function and extra data can
result from a calculation, a call to a webservice or a query against
another database.
As a standalone function with known inputs and
outputs, the transform functions are also easy to test.

Source rows
"""""""""""

If an output is derived from one or more input rows, the transform can report
which rows they were by yielding a wrapped output.  This is useful when a
database rejects the output: the ``on_error`` callback can then relate the
rejected row back to the input records that produced it.

.. code:: python

   from etlhelper import with_source, with_sources, no_source

   def my_transform(chunk):
       source_a, source_b = chunk
       yield with_source({"kind": "flag_a"}, source_a)
       yield with_sources({"kind": "comparison"}, [source_a, source_b])
       yield no_source({"kind": "unattributed"})

Sources must belong to the same input chunk and are matched by object
identity.  Equal field values, equal record IDs and position in the chunk are
not used to infer a source.  Refer to :ref:`source_rows` for details.

The wrapper is only used to carry metadata: only the row itself is passed to
the database, and a transform that returns plain rows is unaffected.

The ``iter_chunks`` and ``iter_rows`` functions return generators.
Each chunk or row of data is only accessed when it is
required. This allows data transformation to be performed via
`memory-efficient
iterator-chains <https://dbader.org/blog/python-iterator-chains>`__.
