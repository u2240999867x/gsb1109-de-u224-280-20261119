Registration attribution
========================

The :mod:`pyopensky.attribution` module attributes historical OpenSky
positions to a regulatory registration without any network access. It keeps
two time axes independent:

* the **valid time** of each registry entry, a half-open
  ``[valid_from, valid_to)`` interval matched against the OpenSky
  ``lastposupdate`` timestamp. The collection timestamp (``record_time``)
  is stored for traceability but never used for matching;
* the **publication time** (``published_at``), which states when an entry
  actually became known. Restricting the registry to a publication cutoff
  reruns a daily report against exactly the evidence available that day.

Registry entries carry a ``source`` (the issuing channel) and may
``supersede`` an earlier entry of the *same* source and address. A
correction therefore withdraws only the issuing channel's own evidence;
independent channels keep supporting their registration.

Loading the offline sample data
--------------------------------

Both inputs are plain UTF-8 CSV files, so reports are fully reproducible
offline. The registry holds one evidence row per line and the observation
file mirrors OpenSky position records.

.. code-block:: python

    from pyopensky.attribution import (
        AttributionEngine,
        load_observations,
        load_registry,
    )

    engine = AttributionEngine(
        load_registry("data/attribution/registry.csv")
    )
    observations = load_observations("data/attribution/observations.csv")

Rerunning a report at a publication cutoff
------------------------------------------

``as_of`` returns an engine that only sees entries published by the
cutoff. Entries published later can never change an old report.

.. code-block:: python

    first_report = (
        engine.as_of("2026-05-15T12:30:00Z")
        .attribute(observations)
    )

Each output row exposes the adopted registration, the live evidences and
the replaced evidences with their replacement entry, so every historical
attribution stays auditable.

Result statuses
---------------

Every observation receives exactly one status:

* ``attributed``: one registration is effective, possibly supported by
  several agreeing evidences;
* ``conflict``: several registrations are effective at the position time;
* ``unregistered``: no entry covers the address at the position time;
* ``invalid_time``: the position has no usable ``lastposupdate``.

Comparing reruns
----------------

:func:`~pyopensky.attribution.diff_reports` compares two runs and returns
the observations whose status or adopted registration changed, including
the evidences added or removed.

.. code-block:: python

    from pyopensky.attribution import diff_reports

    final_report = (
        engine.as_of("2026-05-16T18:00:00Z")
        .attribute(observations)
    )
    changes = diff_reports(first_report, final_report)

A runnable walkthrough of a registry correction (first report, partial
correction with a B/C conflict, then full correction to C) is available in
``scripts/demo_attribution.py``.
