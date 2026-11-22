"""Shared harness for the repeatable store-collection timer regressions.

The harness owns the controlled clock, the local collection endpoint and the
serializer used to take/restore archives.  All observations come from the
public workflow API (``get_tasks`` / ``waiting_events``) and from the endpoint,
never from private engine internals.
"""

import copy
import json
from calendar import monthrange
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from SpiffWorkflow import TaskState
from SpiffWorkflow.bpmn import BpmnWorkflow
from SpiffWorkflow.bpmn.script_engine import PythonScriptEngine, TaskDataEnvironment
from SpiffWorkflow.bpmn.specs.mixins.events.event_types import CatchingEvent

from ...BpmnWorkflowTestCase import BpmnWorkflowTestCase
from .controlled_clock import ControlledClock
from .local_collector import (
    LocalCollectionEndpoint,
    BusinessIdentity,
    identity_for_task,
    COLLECT_TASK,
    RECONCILE_TASK,
    WINDOW_TIMER,
    RECONCILE_TIMER,
    BUSINESS_TASKS,
)
from .schedule import (
    EXPECTED_LOCAL_DATES,
    NEW_YORK,
    month_end_instant,
    planned_windows,
    reconcile_instant,
    window_label,
)

BPMN_FILE = 'timer-regression-store-collection.bpmn'
PROCESS_ID = 'store_collection_process'

# Timer firing uses a strict "next_event < now" comparison, so the virtual clock
# has to be moved an epsilon past the planned instant.
EPSILON = timedelta(seconds=1)

# Which catching timer feeds which manual business task.
_TIMER_FOR_TASK = {
    COLLECT_TASK: WINDOW_TIMER,
    RECONCILE_TASK: RECONCILE_TIMER,
}


class TimerRegressionTestCase(BpmnWorkflowTestCase):

    def setUp(self):
        super().setUp()
        self.initial_instant = month_end_instant(2026, 1) - timedelta(days=30)
        self.clock = ControlledClock(self.initial_instant).start()
        self.spec, self.subprocesses = self.load_workflow_spec(BPMN_FILE, PROCESS_ID)
        self.script_engine = PythonScriptEngine(environment=TaskDataEnvironment({
            'ZoneInfo': ZoneInfo,
            'monthrange': monthrange,
            'datetime': datetime,
            'timezone': timezone,
        }))
        self.endpoint = LocalCollectionEndpoint(self.clock)

    def tearDown(self):
        self.endpoint.detach()
        self.clock.stop()
        super().tearDown()

    # -- workflow construction ---------------------------------------------

    def new_workflow(self):
        # Every fresh workflow starts before the first window regardless of any
        # clock movement made while building an archive earlier in the test.
        self.clock.advance_to(self.initial_instant)
        workflow = BpmnWorkflow(self.spec, self.subprocesses, script_engine=self.script_engine)
        start = workflow.get_next_task(state=TaskState.READY)
        start.set_data(
            window_index=1,
            window_due=month_end_instant(2026, 1).isoformat(),
            window_label=window_label(2026, 1),
            reconcile_due=reconcile_instant().isoformat(),
            reconcile_label=window_label(2026, 3),
        )
        workflow.do_engine_steps()
        self.endpoint.attach(workflow)
        return workflow

    # -- clock / scheduling -------------------------------------------------

    def reach_instant(self, workflow, instant):
        self.clock.advance_to(instant + EPSILON)
        workflow.refresh_timers()
        workflow.do_engine_steps()
        return workflow

    def reach_window(self, workflow, month):
        return self.reach_instant(workflow, month_end_instant(2026, month))

    # -- archives -----------------------------------------------------------

    def archive(self, workflow):
        return json.loads(json.dumps(self.serializer.to_dict(workflow)))

    def restore(self, state):
        workflow = self.serializer.from_dict(copy.deepcopy(state))
        workflow.script_engine = self.script_engine
        self.endpoint.attach(workflow)
        return workflow

    def february_archive(self):
        """Feb window just became runnable; its result has not been produced."""
        workflow = self.new_workflow()
        self.reach_window(workflow, 1)
        self.run_ready(workflow)  # deliver January, then compute the February due date
        self.reach_window(workflow, 2)
        february = BusinessIdentity(COLLECT_TASK, window_label(2026, 2))
        self.assert_ready(workflow, {(february.task_spec, february.label)})
        # The February work is runnable but has not been sent yet.
        self.assertNotIn(february, self.endpoint.delivered_identities())
        return self.archive(workflow)

    def march_archive(self):
        """Both March items (collect window and reconciliation) are ready."""
        workflow = self.new_workflow()
        for month in (1, 2):
            self.reach_window(workflow, month)
            self.run_ready(workflow)
        self.reach_window(workflow, 3)
        march_collect = BusinessIdentity(COLLECT_TASK, window_label(2026, 3))
        march_reconcile = BusinessIdentity(RECONCILE_TASK, window_label(2026, 3))
        self.assert_ready(workflow, {
            (march_collect.task_spec, march_collect.label),
            (march_reconcile.task_spec, march_reconcile.label),
        })
        # Both March items are runnable but neither has been sent yet.
        self.assertNotIn(march_collect, self.endpoint.delivered_identities())
        self.assertNotIn(march_reconcile, self.endpoint.delivered_identities())
        return self.archive(workflow)

    # -- driving ------------------------------------------------------------

    def run_ready(self, workflow, identities=None):
        """Complete ready manual business tasks.

        ``identities`` optionally restricts the run to specific
        ``(task_spec, label)`` pairs, allowing one of two same-instant tasks to
        be sent while the other stays ready.
        """
        wanted = None if identities is None else set(identities)
        run = []
        for task in list(workflow.get_tasks(state=TaskState.READY)):
            if task.task_spec.name not in BUSINESS_TASKS:
                continue
            identity = identity_for_task(task)
            if wanted is None or (identity.task_spec, identity.label) in wanted:
                task.run()
                run.append(identity)
        workflow.do_engine_steps()
        return run

    def submit_prior_window_receipt(self, workflow, task_spec=COLLECT_TASK, label=window_label(2026, 1)):
        """Re-submit a receipt for a window that was already delivered."""
        task = self.find_business_task(workflow, task_spec, label)
        return self.endpoint.submit_receipt(task)

    def find_business_task(self, workflow, task_spec, label):
        for task in workflow.get_tasks():
            if task.task_spec.name != task_spec:
                continue
            identity = identity_for_task(task)
            if identity is not None and identity.label == label:
                return task
        raise AssertionError(f'no task for {task_spec}[{label}]')

    def business_task_id(self, workflow, task_spec, label):
        return str(self.find_business_task(workflow, task_spec, label).id)

    # -- observation --------------------------------------------------------

    def waiting_identity(self, timer_task):
        timer_name = timer_task.task_spec.name
        business_task = next(name for name, value in _TIMER_FOR_TASK.items() if value == timer_name)
        label_key = 'window_label' if business_task == COLLECT_TASK else 'reconcile_label'
        label = timer_task.get_data(label_key)
        pending = timer_task.task_spec.event_definition.details(timer_task).value
        return business_task, label, pending

    def observe(self, workflow):
        """Snapshot every business identity's public status at this clock.

        The waiting-event list is read from the public
        :meth:`BpmnWorkflow.waiting_events` view (the same view an external
        scheduler polls), so it reflects how the engine publishes timers rather
        than how many timer task objects happen to exist internally.
        """
        ready, waiting, delivered = set(), set(), set()
        for task in workflow.get_tasks():
            if isinstance(task.task_spec, CatchingEvent) and task.state == TaskState.WAITING:
                business_task, label, _pending = self.waiting_identity(task)
                waiting.add((business_task, label))
            if task.task_spec.name in BUSINESS_TASKS and task.state == TaskState.READY:
                identity = identity_for_task(task)
                ready.add((identity.task_spec, identity.label))
        for identity in self.endpoint.delivered_identities():
            delivered.add((identity.task_spec, identity.label))
        public_events = sorted(
            (event.name, event.value) for event in workflow.waiting_events())
        return {
            'clock': self.clock.current.isoformat(),
            'waiting_events': public_events,
            'ready': sorted(ready),
            'waiting': sorted(waiting),
            'delivered': sorted(delivered),
        }

    def assert_ready(self, workflow, identities):
        observed = {
            (identity.task_spec, identity.label)
            for identity in self.endpoint.public_ready_identities(workflow)
        }
        self.assertEqual(observed, set(identities))

    def assert_local_schedule(self):
        """The first four 2026 windows must fall on the required local dates."""
        windows = planned_windows()
        self.assertEqual([window.index for window in windows], [1, 2, 3, 4])
        for window in windows:
            year, month, day = EXPECTED_LOCAL_DATES[window.index]
            local_date = window.due.astimezone(NEW_YORK)
            self.assertEqual((local_date.year, local_date.month, local_date.day), (year, month, day))
            self.assertEqual((local_date.hour, local_date.minute), (1, 30))
