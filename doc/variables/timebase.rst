.. _timebase:

=====================================================
``timebase``: Center time-base conversion for events
=====================================================

.. contents:: **Contents**
    :local:
    :depth: 1

|

Overview
========

Some acquisition centers export legacy events on the scanner-room clock, where
``onset`` is measured from room entry rather than from the start of each run.
A **center time-base contract** describes that site-local mapping.  It is not
part of the BIDS specification and is never written into a source TSV; it is a
separate sidecar that names the center's custom columns and declares the offset
for each run.

The same conversion is used by ``BIDSDataFile.get_df`` and by
``load_variables``, so a table read and the model event variables always agree.
When no contract is supplied, both paths behave exactly as before.

Contract format
===============

.. code-block:: json

    {
      "format": "bids-center-timebase-contract",
      "name": "center-a",
      "origin": "scanner_room_entry",
      "direction": "subtract",
      "runs": [
        {"entities": {"subject": "01", "task": "shared", "run": 1}, "offset": 120.0},
        {"entities": {"subject": "01", "task": "shared", "run": 2}, "offset": 240.0}
      ],
      "columns": {
        "target_run": "center_run",
        "origin_run": "center_origin_run",
        "time_base": "center_time_base",
        "offset": "center_offset",
        "original_onset": "center_original_onset",
        "converted": "center_converted"
      }
    }

Offsets are applied as ``run_onset = room_onset - offset``.  The most specific
matching declaration wins; equally specific conflicting declarations raise
``TimeBaseContractError`` instead of guessing.  A per-row offset in the
``columns.offset`` column overrides the run-level offset for a new row.  Rows
whose ``time_base`` is a native run-relative value are never shifted.

Usage
=====

.. code-block:: python

    from bids.layout import BIDSLayout
    from bids.variables import TimeBaseContract, convert_event_table, load_variables

    contract = TimeBaseContract.from_file('center-a_timebase.json')

    # Table read and model variables use the same converter.
    layout = BIDSLayout('/data/ds')
    events = layout.get(suffix='events', extension='tsv')[0]
    df = events.get_df(timebase=contract, target={'subject': '01', 'run': 1})

    index = load_variables(layout, types='events', timebase=contract)

A shared events table can serve two consecutive runs.  Import it once per run;
each import applies that run's offset, and rows declaring another run are
listed separately rather than converted with the wrong offset.

Handoff and reconciliation
==========================

A handoff is a JSON document that carries the converted rows of one or more
runs together with per-row evidence: the original room-clock onset, the offset
that was used, its sidecar, and the target run.

.. code-block:: python

    from bids.variables import TimeBaseHandoff, import_timebase_handoff

    handoff = TimeBaseHandoff.read('handoff.json')
    result = import_timebase_handoff(
        handoff,
        new_raw_rows,          # pd.DataFrame of newly received rows
        contract=corrected,    # center's current contract
        target={'subject': '01', 'run': 1},
        source='new_events.tsv',
    )

    result.rows              # accepted rows, with converted onsets
    result.reconciliation    # one row per input row: original, offset, result, status
    result.quarantined       # rows that cannot be safely assigned/converted
    result.skipped           # rows that declare a different run

To feed a handoff's two runs plus new raw rows through the normal table path,
merge them first and write a *new* events file:

.. code-block:: python

    from bids.variables import merge_handoff_table

    table = merge_handoff_table(handoff, new_raw_rows)
    table.to_csv('sub-01_task-shared_events_converted.tsv', sep='\t', index=False)
    df = events_file.get_df(timebase=corrected, target={'subject': '01', 'run': 1})
    index = load_variables(layout, types='events', timebase=corrected)

Repeated imports are stable:

* A converted row whose target run and recorded offset still match keeps its
  recorded result.
* If the center corrects a run's offset, the row is recomputed from its
  recorded original onset.  A converted value is never shifted a second time.
* A row with contradictory run evidence, or with no usable offset and no
  verifiable original onset, is quarantined instead of being converted.
* A missing onset and its duration keep their original missing semantics.

The reconciliation table exposes the raw value, the target run, the offset that
was used, the result, and the sidecar that supplied it.
