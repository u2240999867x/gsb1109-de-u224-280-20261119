:mod:`projection`
==================

.. module:: rule_engine.projection
   :synopsis:

.. versionadded:: 5.3.0

This module derives *staged projections* from rules: a description of which fields of the evaluated object
a rule actually reads, split into a control stage (the selection fields, such as shape flags, row activity,
categories and column mappings) and a detail stage (the large fields behind guards, resolved per branch
actually taken). It is intended for callers that fetch data incrementally, for example reading small control
columns from a columnar store before deciding which detail fields to load.

Constants
---------

.. autodata:: CONTROL
.. autodata:: DETAIL
.. autodata:: SUBTREE

Classes
-------

.. autoclass:: BoundProjection
   :members:
   :special-members: __repr__

.. autoclass:: ProjectionPlan
   :members:
   :special-members: __repr__

.. autoclass:: ProjectionSet
   :members:
   :special-members: __repr__

.. autoclass:: ReadOrigin
   :members:
   :special-members: __repr__

.. autoclass:: ReadSite
   :members:
   :special-members: __repr__

Functions
---------

.. autofunction:: format_path

.. autofunction:: project
