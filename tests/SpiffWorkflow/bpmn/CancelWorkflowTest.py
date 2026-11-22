from SpiffWorkflow import TaskState
from SpiffWorkflow.bpmn import BpmnWorkflow, BpmnEvent
from SpiffWorkflow.bpmn.specs.event_definitions.message import MessageEventDefinition
from SpiffWorkflow.exceptions import WorkflowException

from .BpmnWorkflowTestCase import BpmnWorkflowTestCase


class CancelWorkflowTest(BpmnWorkflowTestCase):
    """Tests for stopping a workflow: determined results are final, late
    arrivals from the stopped batch cannot change them, and a new round
    only consumes its own responses."""

    def setUp(self):
        self.spec, self.subprocesses = self.load_workflow_spec('cancel_parallel.bpmn', 'Cancel_Parallel')

    def _advance(self):
        """A completes on the first branch; B is transmitted but unconfirmed
        and C is still waiting on the second branch."""
        workflow = BpmnWorkflow(self.spec, self.subprocesses)
        workflow.do_engine_steps()
        task_a = workflow.get_next_task(spec_name='Task_A', state=TaskState.READY)
        task_a.set_data(result='A')
        task_a.run()
        task_b = workflow.get_next_task(spec_name='Task_B', state=TaskState.READY)
        task_b._set_state(TaskState.STARTED)
        workflow.do_engine_steps()
        return workflow

    def _completed_names(self, workflow):
        return [t.task_spec.name for t in workflow.get_tasks(state=TaskState.COMPLETED)]

    def test_cancel_preserves_completed_results(self):
        workflow = self._advance()
        cancelled = workflow.cancel()
        self.assertIn(workflow.get_next_task(spec_name='Task_B'), cancelled)
        # The stop does not count as a success and determined results remain
        self.assertFalse(workflow.success)
        self.assertTrue(workflow.completed)
        self.assertIn('Task_A', self._completed_names(workflow))
        task_a = workflow.get_next_task(spec_name='Task_A')
        self.assertEqual(task_a.data['result'], 'A')
        # Unfinished tasks were cancelled and cannot re-enter execution
        self.assertEqual(workflow.get_next_task(spec_name='Task_B').state, TaskState.CANCELLED)
        self.assertEqual(workflow.get_next_task(spec_name='Task_C').state, TaskState.CANCELLED)
        self.assertIsNone(workflow.get_next_task(spec_name='Task_B').run())
        self.assertEqual(workflow.get_next_task(spec_name='Task_B').state, TaskState.CANCELLED)
        # No publish requests are re-sent from the stopped batch
        self.assertEqual(workflow.get_events(), [])

    def test_confirmation_within_tentative_bound_is_counted(self):
        workflow = self._advance()
        task_b = workflow.get_next_task(spec_name='Task_B')
        # B's confirmation arrives before the stop is final
        task_b.set_data(result='B')
        task_b.complete()
        workflow.cancel()
        self.assertIn('Task_B', self._completed_names(workflow))
        self.assertFalse(workflow.success)

    def test_late_confirmation_is_rejected_after_stop(self):
        workflow = self._advance()
        workflow.cancel()
        task_b = workflow.get_next_task(spec_name='Task_B')
        # B's confirmation only arrives after the stop: it cannot be written
        # as completed and remains identifiable as a late submission
        task_b.complete()
        self.assertEqual(task_b.state, TaskState.CANCELLED)
        self.assertNotIn('Task_B', self._completed_names(workflow))
        self.assertFalse(workflow.success)

    def test_save_restore_and_replay(self):
        workflow = self._advance()
        workflow.cancel()
        self.workflow = workflow
        self.save_restore()
        restored = self.workflow
        # The restored workflow preserves the determined results
        self.assertFalse(restored.success)
        self.assertTrue(restored.completed)
        self.assertIn('Task_A', self._completed_names(restored))
        # Replayed results for A and C are harmless
        task_a = restored.get_next_task(spec_name='Task_A')
        task_a.complete()
        self.assertEqual(task_a.state, TaskState.COMPLETED)
        task_c = restored.get_next_task(spec_name='Task_C')
        task_c.cancel()
        self.assertEqual(task_c.state, TaskState.CANCELLED)
        # B's late confirmation still cannot change the determined result
        task_b = restored.get_next_task(spec_name='Task_B')
        task_b.complete()
        self.assertEqual(task_b.state, TaskState.CANCELLED)

    def test_replayed_tentative_signals_cannot_revert_results(self):
        workflow = self._advance()
        workflow.cancel()
        # A resent stop request cannot un-complete A or otherwise change results
        workflow.cancel()
        self.assertIn('Task_A', self._completed_names(workflow))
        self.assertEqual(workflow.get_next_task(spec_name='Task_B').state, TaskState.CANCELLED)
        self.assertFalse(workflow.success)


class CancelledSubprocessTest(BpmnWorkflowTestCase):
    """A stopped or superseded round must not be affected by late responses
    from its subprocesses."""

    def setUp(self):
        self.spec, self.subprocesses = self.load_workflow_spec('cancel_call_activity.bpmn', 'Top_Process')

    def _advance(self):
        workflow = BpmnWorkflow(self.spec, self.subprocesses)
        workflow.do_engine_steps()
        task = workflow.get_next_task(spec_name='Call_Sub')
        subprocess = workflow.subprocesses[task.id]
        return workflow, task, subprocess

    def _complete_subprocess(self, subprocess):
        user_task = subprocess.get_next_task(spec_name='Sub_User')
        user_task.run()
        subprocess.run_all()

    def test_late_subprocess_completion_does_not_resurrect_task(self):
        workflow, task, subprocess = self._advance()
        workflow.cancel()
        self.assertEqual(task.state, TaskState.CANCELLED)
        # The old round's late success arrives after the cancel
        self._complete_subprocess(subprocess)
        self.assertEqual(task.state, TaskState.CANCELLED)
        self.assertFalse(workflow.success)

    def test_previous_round_cannot_release_new_round_slot(self):
        workflow, task, old_subprocess = self._advance()
        # A new batch is started with the same device and filename
        workflow.reset_from_task_id(task.id)
        task = workflow.get_next_task(spec_name='Call_Sub')
        new_subprocess = workflow.subprocesses[task.id]
        self.assertIsNot(new_subprocess, old_subprocess)
        # The old round's late success arrives after the new batch started
        self._complete_subprocess(old_subprocess)
        self.assertTrue(old_subprocess.completed)
        # It must not release the new round's slot
        self.assertEqual(task.state, TaskState.STARTED)
        self.assertFalse(new_subprocess.completed)
        # The new batch only consumes its own responses
        self._complete_subprocess(new_subprocess)
        self.assertEqual(task.state, TaskState.READY)

    def test_slot_requires_previous_round_to_finish(self):
        workflow, task, subprocess = self._advance()
        # The device cannot be reassigned while the original round is active
        with self.assertRaises(WorkflowException):
            workflow.create_subprocess(task, 'Sub_Process')
        # Once the original round's cancel is confirmed, it can be reassigned
        workflow.cancel()
        replacement = workflow.create_subprocess(task, 'Sub_Process')
        self.assertIsNot(replacement, subprocess)


class RemovedTaskEventTest(BpmnWorkflowTestCase):
    """Tasks removed from the workflow must not catch events; a new round
    only consumes responses that belong to it."""

    def setUp(self):
        self.spec, self.subprocesses = self.load_workflow_spec('reset_waiting_event.bpmn', 'Evt_Process')

    def test_removed_tasks_do_not_catch_events(self):
        workflow = BpmnWorkflow(self.spec, self.subprocesses)
        workflow.do_engine_steps()
        user_task = workflow.get_next_task(spec_name='User_1')
        user_task.run()
        workflow.do_engine_steps()
        old_catch = workflow.get_next_task(spec_name='Catch_1')
        self.assertEqual(old_catch.state, TaskState.WAITING)

        # Resetting removes the waiting task from the tree and the event manager
        workflow.reset_from_task_id(user_task.id)
        self.assertEqual(len(workflow.event_manager.tasks), 0)

        # The old round's late response is not consumed by a stale registration
        workflow.catch(BpmnEvent(MessageEventDefinition('Msg_1')))
        self.assertIsNone(old_catch.internal_data.get('event_fired'))

        # The new round waits for and consumes its own response
        user_task = workflow.get_next_task(spec_name='User_1')
        user_task.run()
        workflow.do_engine_steps()
        new_catch = workflow.get_next_task(spec_name='Catch_1')
        self.assertEqual(new_catch.state, TaskState.WAITING)
        workflow.catch(BpmnEvent(MessageEventDefinition('Msg_1')))
        self.assertEqual(new_catch.state, TaskState.READY)


if __name__ == '__main__':
    unittest.main()
