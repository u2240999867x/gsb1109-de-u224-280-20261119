.. currentmodule:: xarray

Point handoff
=============

.. currentmodule:: xarray.point_handoff

The point handoff layer is opt-in.  Nothing changes for datasets that do not
call :func:`register_point_handoff_accessor`.  Register the accessor once,
attach handoff and reply descriptions to the archival grid, then select grid
cells normally and read :attr:`PointHandoffAccessor.value`.

.. code-block:: python

    import xarray as xr
    from xarray import point_handoff

    point_handoff.register_point_handoff_accessor()

    catalog = point_handoff.build_handoff(ds, "A", "A-1", [(0, 1), (0, 0)])
    replies = point_handoff.ReplyCatalog(
        absolutes={"abs-1": {("A", "A-1", 1): 20}},
        offsets={"off-1": [(("A", "A-1", 2), ("A", "A-1", 1), 1)]},
        statuses={"status-1": {("A", "A-1", 3): "explicitly_missing"}},
    )
    tagged = point_handoff.attach(ds, [catalog], replies)
    tagged.sel(zone="north").isel(leg=1, lon=[3, 1, 0]).point_handoff.value

Handoff catalogs
----------------
.. autosummary::
   :toctree: ../generated/

   HandoffCatalog
   HandoffRow
   build_handoff
   save_handoff
   load_handoff

Replies and number blocks
-------------------------
.. autosummary::
   :toctree: ../generated/

   ReplyCatalog
   MemoryBlock
   JsonFileBlock
   MetadataBlock
   save_replies
   load_replies
   save_catalog
   load_catalog

Attach and reopen
-----------------
.. autosummary::
   :toctree: ../generated/

   attach
   open_point_handoff
   register_point_handoff_accessor

Resolution results
------------------
.. autosummary::
   :toctree: ../generated/

   PointHandoffAccessor
   CellResolution
   ConflictDetail
   RouteBranch
   RouteStep
