import unittest

from SpiffWorkflow import TaskState, Workflow
from SpiffWorkflow.specs import WorkflowSpec, Simple


class FinishedTaskTest(unittest.TestCase):
    """A finished task's result is final: running, completing, or erroring it
    again cannot change the determined result, so replays and late arrivals
    are harmless."""

    def setUp(self):
        spec = WorkflowSpec(name='finished_task_test', addstart=True)
        self.first = Simple(spec, 'first', manual=True)
        self.second = Simple(spec, 'second', manual=True)
        spec.start.connect(self.first)
        self.first.connect(self.second)
        self.workflow = Workflow(spec)
        self.workflow.run_all()

    def test_run_completed_task_has_no_effect(self):
        task = self.workflow.get_next_task(spec_name='first', state=TaskState.READY)
        task.run()
        self.assertEqual(task.state, TaskState.COMPLETED)
        self.assertIsNone(task.run())
        self.assertEqual(task.state, TaskState.COMPLETED)

    def test_complete_completed_task_has_no_effect(self):
        task = self.workflow.get_next_task(spec_name='first', state=TaskState.READY)
        task.run()
        task.complete()
        self.assertEqual(task.state, TaskState.COMPLETED)

    def test_cancelled_task_cannot_be_run_or_completed(self):
        task = self.workflow.get_next_task(spec_name='first', state=TaskState.READY)
        task.cancel()
        self.assertEqual(task.state, TaskState.CANCELLED)
        self.assertIsNone(task.run())
        self.assertEqual(task.state, TaskState.CANCELLED)
        task.complete()
        self.assertEqual(task.state, TaskState.CANCELLED)

    def test_finished_task_cannot_be_errored(self):
        task = self.workflow.get_next_task(spec_name='first', state=TaskState.READY)
        task.run()
        task.error()
        self.assertEqual(task.state, TaskState.COMPLETED)
        waiting = self.workflow.get_next_task(spec_name='second', state=TaskState.READY)
        waiting.cancel()
        waiting.error()
        self.assertEqual(waiting.state, TaskState.CANCELLED)

    def test_cancel_does_not_revert_completed_tasks(self):
        first = self.workflow.get_next_task(spec_name='first', state=TaskState.READY)
        first.run()
        self.workflow.cancel()
        states = {t.task_spec.name: t.state for t in self.workflow.get_tasks()}
        self.assertEqual(states['first'], TaskState.COMPLETED)
        self.assertEqual(states['second'], TaskState.CANCELLED)
        # Cancelling again changes nothing
        self.workflow.cancel()
        states = {t.task_spec.name: t.state for t in self.workflow.get_tasks()}
        self.assertEqual(states['first'], TaskState.COMPLETED)
        self.assertEqual(states['second'], TaskState.CANCELLED)

    def test_cancelled_workflow_is_not_successful(self):
        first = self.workflow.get_next_task(spec_name='first', state=TaskState.READY)
        first.run()
        self.workflow.cancel()
        self.assertFalse(self.workflow.success)
        self.assertTrue(self.workflow.completed)
        # Completed results remain available after the workflow is stopped
        completed = [t.task_spec.name for t in self.workflow.get_tasks(state=TaskState.COMPLETED)]
        self.assertIn('first', completed)


if __name__ == '__main__':
    unittest.main()
