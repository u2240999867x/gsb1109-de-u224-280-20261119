"""Test-side doubles that deliberately reproduce the timer defect classes.

The doubles change exactly the behaviour named by their defect, so a regression
that fails only against the matching double proves the suite can tell that
defect apart from the others and from the correct executor.

Defect A (an interval that keeps the first month's length forever) is a
scheduling rule and is exercised through :mod:`schedule` plus a direct
observation of the native ``P1M`` recurring-interval parsing; the doubles below
cover the two engine-level defects.
"""

from SpiffWorkflow import TaskState
from SpiffWorkflow.bpmn.specs.mixins.events.event_types import CatchingEvent
from SpiffWorkflow.bpmn.util.event import EventManager

from .local_collector import BUSINESS_TASKS


def reclock_ready_tasks(workflow):
    """Defect B double: after restore, treat ready business work as still timed.

    A correct restore leaves an already-ready manual task ready.  This double
    demotes each ready business task back to WAITING and re-arms the catching
    timer that feeds it, so the work disappears from the public ready set until
    a later ``refresh_timers`` happens to re-fire the timer.  It returns the
    names of the tasks it demoted so a test can pin the first divergence.
    """
    re_clocked = []
    for task in list(workflow.get_tasks(state=TaskState.READY)):
        if task.task_spec.name not in BUSINESS_TASKS:
            continue
        timer = task.parent
        while timer is not None and not isinstance(timer.task_spec, CatchingEvent):
            timer = timer.parent
        task._set_state(TaskState.WAITING)
        if timer is not None:
            timer._set_state(TaskState.WAITING)
            timer.internal_data.pop('event_fired', None)
            workflow.event_manager.add_task(timer)
        re_clocked.append(task.task_spec.name)
    return re_clocked


class CollapsingEventManager(EventManager):
    """Defect C double: merge timers that share an absolute instant.

    A correct manager reports one pending event per waiting task, so two timers
    due at the same instant are reported separately.  This manager collapses
    entries that have identical ``value`` into a single event.
    """

    def get_waiting_tasks(self):
        collapsed = []
        seen_values = set()
        for pending in super().get_waiting_tasks():
            if pending.value in seen_values:
                continue
            seen_values.add(pending.value)
            collapsed.append(pending)
        return collapsed


def install_collapsing_event_manager(workflow):
    replacement = CollapsingEventManager(workflow)
    replacement.tasks.update(workflow.event_manager.tasks)
    workflow.event_manager = replacement
    return replacement
