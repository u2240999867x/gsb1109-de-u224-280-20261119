:mod:`engine`
=============

.. module:: rule_engine.engine
   :synopsis:

This module contains the primary externally facing API for the package.

Functions
---------

.. autofunction:: resolve_attribute

.. autofunction:: resolve_item

.. autofunction:: type_resolver_from_dataclass

.. autofunction:: type_resolver_from_dict

.. autofunction:: type_resolver_from_sqlalchemy

Classes
-------

.. autoclass:: Chain
   :members:
   :show-inheritance:
   :special-members: __init__
   :undoc-members:

.. autoclass:: ChainEntry
   :members:
   :show-inheritance:

.. autoclass:: LegacyGradeContext
   :members:
   :show-inheritance:
   :special-members: __init__
   :undoc-members:

.. autoclass:: PendingEvaluation
   :members:
   :show-inheritance:

.. autoclass:: ResumableMapping
   :members:
   :show-inheritance:

.. autoclass:: DeferredDataError
   :members:
   :show-inheritance:

.. autoattribute:: DeferredValue

.. autoclass:: Context
   :members:
   :show-inheritance:
   :special-members: __init__
   :undoc-members:

.. autoclass:: Rule
   :members:
   :show-inheritance:
   :special-members: __init__
   :undoc-members:
