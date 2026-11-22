# Store-collection timer regressions

Deterministic, repeatable regressions for a store-collection process that has a
recurring month-end timer and an independent reconciliation timer. Nothing here
sleeps or reads the real clock: every run is driven by a controlled virtual
clock and an in-process collection endpoint.

## The process under test

`../data/timer-regression-store-collection.bpmn` is a single executable process
with a parallel split:

* a **recurring collection loop** — `WaitForWindow` (time-date) → the manual
  `CollectStoreData` task → `ComputeNextWindow`, which recomputes the next
  America/New_York local month end at 01:30 and loops back;
* an **independent reconciliation branch** — `WaitForReconciliation` → the
  manual `ReconcileReceipts` task, due once at the March month end.

The recurring branch recomputes each due date on the calendar rather than
adding a fixed duration, so shorter months (February) do not drift.

## Modules

* `controlled_clock.py` — patches the single `datetime.now` used by the timer
  event definitions with a mutable, virtual "now".
* `schedule.py` — the New York month-end oracle, the required first-four-2026
  dates (31 Jan, 28 Feb, 31 Mar, 30 Apr), and the pinned-first-month fault rule.
* `local_collector.py` — the in-process endpoint. Business identity is the
  planned window label, not a task id, so restored task objects are never
  counted as new events; duplicate receipts are rejected as replays.
* `harness.py` — workflow construction, clock movement, archive/restore and the
  public `observe()` snapshot (ready / waiting / delivered per identity).
* `trace.py` — ordered operation traces and `first_difference`, which names the
  earliest diverging operation even when final request sets are identical.
* `fault_doubles.py` — test-side doubles for the two engine-level defects.
* `StoreCollectionTimerRegressionTest.py` — the regressions.

## What is asserted

* the first four windows land on the required local dates and times, including
  the New York daylight-saving change;
* the March reconciliation fires at the same absolute instant as the March
  collection window and produces a second, distinct manual task;
* from one February archive (task runnable, result not sent), the three
  operations restore → process → duplicate-receipt are run in both orders;
* from one March archive (both items ready), the executor is restored before
  and after sending just one item;
* every identity is observed as waiting, then ready (entered), then delivered
  at its scheduled window via the public task API and the endpoint;
* three defect classes are told apart and each fails only its own check:
  pinned first month length, re-clocking a restored ready task, and merging two
  same-instant timers;
* a legal single task and a no-restore fixed-second interval are not flagged.

## Running

```
python -m unittest discover -s tests/SpiffWorkflow/bpmn/events/timer_regression \
    -p '*Test.py' -t .
```
