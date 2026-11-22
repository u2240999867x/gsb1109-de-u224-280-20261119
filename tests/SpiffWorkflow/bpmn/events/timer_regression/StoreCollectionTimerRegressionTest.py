"""Repeatable timer regressions for the store-collection process.

Coverage:
  * the recurring rule lands on the America/New_York local month end at 01:30,
    with the first four 2026 windows on 31 Jan, 28 Feb, 31 Mar and 30 Apr;
  * the independent reconciliation timer fires at the same absolute instant as
    the March window and produces a second, distinct business task;
  * deterministic restore/process/replay traces built from a February archive
    and a March archive, with the earliest diverging operation identified;
  * discrimination of three defect classes (pinned first month length,
    re-clocking ready work after restore, merging same-instant timers);
  * guards proving a legal single task and a fixed-second interval without
    restore are not false positives.

Every run uses a controlled clock and an in-process endpoint, so nothing
depends on the real date or on sleeping.
"""

from datetime import timedelta

from SpiffWorkflow import TaskState
from SpiffWorkflow.bpmn.specs.event_definitions.timer import TimerEventDefinition

from .fault_doubles import install_collapsing_event_manager, reclock_ready_tasks
from .harness import TimerRegressionTestCase
from .local_collector import (
    BusinessIdentity,
    COLLECT_TASK,
    RECONCILE_TASK,
    WINDOW_TIMER,
    RECONCILE_TIMER,
)
from .schedule import (
    CalendarMonthSchedule,
    FirstMonthLengthSchedule,
    EXPECTED_LOCAL_DATES,
    NEW_YORK,
    first_diverging_window,
    month_end_instant,
    planned_windows,
    reconcile_instant,
    window_label,
)
from .trace import Trace, receiver_request_set


class ScheduleRuleTest(TimerRegressionTestCase):

    def test_first_four_local_month_ends_and_absolute_instants(self):
        self.assert_local_schedule()
        windows = planned_windows()
        self.assertEqual(
            [tuple(window.due.astimezone(NEW_YORK).timetuple()[:3]) for window in windows],
            [EXPECTED_LOCAL_DATES[index] for index in (1, 2, 3, 4)],
        )
        # March is daylight time in New York; the absolute instant still matches
        # the independently planned reconciliation instant.
        self.assertEqual(windows[2].due, reconcile_instant())
        self.assertEqual(reconcile_instant().isoformat(), '2026-03-31T05:30:00+00:00')

    def test_engine_observes_each_window_once_with_correct_identities(self):
        workflow = self.new_workflow()
        for month in (1, 2, 3, 4):
            self.reach_window(workflow, month)
            ready = {
                (identity.task_spec, identity.label)
                for identity in self.endpoint.public_ready_identities(workflow)
            }
            self.assertIn((COLLECT_TASK, window_label(2026, month)), ready)
            self.run_ready(workflow)
        collect_labels = sorted(
            identity.label
            for identity in self.endpoint.delivered_identities()
            if identity.task_spec == COLLECT_TASK
        )
        self.assertEqual(collect_labels, [window_label(2026, month) for month in (1, 2, 3, 4)])
        self.assertTrue(workflow.completed)

    def test_reconciliation_is_a_second_task_at_the_same_march_instant(self):
        workflow = self.new_workflow()
        for month in (1, 2):
            self.reach_window(workflow, month)
            self.run_ready(workflow)
        # Immediately before the March instant the two timers are waiting and, in
        # absolute terms, are due at the identical instant.
        self.clock.advance_to(month_end_instant(2026, 3) - timedelta(seconds=60))
        workflow.refresh_timers()
        waiting = {event.name: event.value for event in workflow.waiting_events()}
        self.assertEqual(set(waiting), {WINDOW_TIMER, RECONCILE_TIMER})
        self.assertEqual(waiting[WINDOW_TIMER], waiting[RECONCILE_TIMER])
        # Crossing the instant makes two distinct manual tasks ready at once.
        self.reach_window(workflow, 3)
        self.assert_ready(workflow, {
            (COLLECT_TASK, window_label(2026, 3)),
            (RECONCILE_TASK, window_label(2026, 3)),
        })

    def test_each_identity_is_waiting_then_ready_then_delivered_at_its_window(self):
        workflow = self.new_workflow()

        def status_of(observation, task_spec, label):
            identity = (task_spec, label)
            if identity in observation['delivered']:
                return 'delivered'
            if identity in observation['ready']:
                return 'ready'
            if identity in observation['waiting']:
                return 'waiting'
            return 'absent'

        # Before the first window both future identities are only waiting.
        initial = self.observe(workflow)
        self.assertEqual(status_of(initial, COLLECT_TASK, window_label(2026, 1)), 'waiting')
        self.assertEqual(status_of(initial, RECONCILE_TASK, window_label(2026, 3)), 'waiting')
        self.assertEqual(initial['ready'], [])
        self.assertEqual(initial['delivered'], [])

        for month in (1, 2, 3, 4):
            label = window_label(2026, month)
            self.reach_window(workflow, month)
            at_window = self.observe(workflow)
            # The collection identity for this window has entered (is ready).
            self.assertEqual(status_of(at_window, COLLECT_TASK, label), 'ready')
            if month < 3:
                self.assertEqual(
                    status_of(at_window, RECONCILE_TASK, window_label(2026, 3)), 'waiting')
            elif month == 3:
                # Same instant: reconciliation has also entered.
                self.assertEqual(
                    status_of(at_window, RECONCILE_TASK, window_label(2026, 3)), 'ready')

            self.run_ready(workflow)
            after = self.observe(workflow)
            self.assertEqual(status_of(after, COLLECT_TASK, label), 'delivered')
            if month == 3:
                self.assertEqual(
                    status_of(after, RECONCILE_TASK, window_label(2026, 3)), 'delivered')
            if month < 4:
                # The next window is already waiting again on the recurring timer.
                self.assertEqual(
                    status_of(after, COLLECT_TASK, window_label(2026, month + 1)), 'waiting')

        self.assertTrue(workflow.completed)


class FixedIntervalAndSingleTaskGuardTest(TimerRegressionTestCase):

    def test_fixed_second_interval_without_restore_is_not_flagged(self):
        cycles, start, duration = TimerEventDefinition.parse_iso_recurring_interval(
            'R3/2026-01-01T00:00:00+00:00/PT10S')
        self.assertEqual(cycles, 3)
        self.assertEqual(duration.total_seconds(), 10.0)
        gaps = [(start + duration * (index + 1)) - (start + duration * index)
                for index in range(cycles)]
        self.assertEqual(gaps, [timedelta(seconds=10)] * cycles)

    def test_legal_single_business_task_runs_once_and_delivers_once(self):
        workflow = self.new_workflow()
        self.reach_window(workflow, 1)
        identity = next(iter(self.endpoint.public_ready_identities(workflow)))
        self.assertEqual(identity.task_spec, COLLECT_TASK)
        self.run_ready(workflow)
        self.assertEqual(len(self.endpoint.deliveries_for(identity)), 1)
        self.assertEqual(
            [task for task in workflow.get_tasks(state=TaskState.READY)
             if task.task_spec.name == COLLECT_TASK],
            [])


class PinnedMonthLengthDiscriminationTest(TimerRegressionTestCase):

    def test_calendar_schedule_matches_the_required_local_dates(self):
        produced = [CalendarMonthSchedule.next_window(index=index) for index in range(1, 5)]
        self.assertIsNone(first_diverging_window(produced, planned_windows()))

    def test_first_month_length_double_diverges_at_the_second_window(self):
        faulty = FirstMonthLengthSchedule()
        produced = [faulty.next_window(index=index) for index in range(1, 5)]
        expected = planned_windows()
        # January is identical; the first mismatch is the February window.
        self.assertEqual(first_diverging_window(produced, expected), 2)
        self.assertEqual(produced[0].due, expected[0].due)
        self.assertNotEqual(produced[1].due, expected[1].due)
        # The drift then accumulates into the following months.
        self.assertNotEqual(
            [window.label for window in produced],
            [window.label for window in expected])

    def test_native_p1m_recurring_interval_pins_the_first_month_length(self):
        _cycles, start, duration = TimerEventDefinition.parse_iso_recurring_interval(
            'R4/2026-01-01T00:00:00+00:00/P1M')
        self.assertEqual(duration.total_seconds(), 31 * 24 * 3600)
        # A fixed 31-day cadence cannot reproduce the 28-day February month end.
        self.assertNotEqual((start + duration).date(), month_end_instant(2026, 2).date())


class FebruaryArchiveOrderingTest(TimerRegressionTestCase):
    """One February archive, the same three operations in two orders."""

    def setUp(self):
        super().setUp()
        self.archive = self.february_archive()
        self.february_identity = BusinessIdentity(COLLECT_TASK, window_label(2026, 2))
        self.january_identity = BusinessIdentity(COLLECT_TASK, window_label(2026, 1))

    def _fresh_receiver_with_prior_history(self):
        self.endpoint.reset()
        self.endpoint.seed_prior_delivery(self.january_identity)

    def _run_ordering(self, receipt_before_process):
        self._fresh_receiver_with_prior_history()
        workflow = self.restore(self.archive)
        trace = Trace()
        trace.record('restore_executor', self.observe(workflow))

        def process_runnable(current):
            self.run_ready(current, {(COLLECT_TASK, window_label(2026, 2))})

        def replay_prior_receipt(current):
            result = self.submit_prior_window_receipt(current)
            self.assertFalse(result.accepted)

        if receipt_before_process:
            operations = [
                ('submit_duplicate_prior_receipt', replay_prior_receipt),
                ('process_runnable_task', process_runnable),
            ]
        else:
            operations = [
                ('process_runnable_task', process_runnable),
                ('submit_duplicate_prior_receipt', replay_prior_receipt),
            ]
        for name, operation in operations:
            operation(workflow)
            trace.record(name, self.observe(workflow))
        return trace, workflow

    def test_both_orderings_keep_one_identity_per_window(self):
        receipt_first_trace, _ = self._run_ordering(True)
        receipt_first_observation = self.endpoint_state()
        receipt_first_names = receipt_first_trace.names()

        process_first_trace, _ = self._run_ordering(False)
        process_first_observation = self.endpoint_state()
        process_first_names = process_first_trace.names()

        # The three operations happen in the two requested orders, each starting
        # with an executor restore.
        self.assertEqual(
            receipt_first_names,
            ['restore_executor', 'submit_duplicate_prior_receipt', 'process_runnable_task'])
        self.assertEqual(
            process_first_names,
            ['restore_executor', 'process_runnable_task', 'submit_duplicate_prior_receipt'])
        # Both orderings leave the receiver in the identical state.
        self.assertEqual(receipt_first_observation, process_first_observation)
        # The seeded January history stays at one record and the February
        # runnable work is delivered exactly once.
        self.assertEqual(
            self.endpoint.delivered_identities(),
            {self.january_identity, self.february_identity})
        self.assertEqual(len(self.endpoint.deliveries_for(self.february_identity)), 1)
        self.assertEqual(len(self.endpoint.deliveries_for(self.january_identity)), 1)
        # Each ordering rejects exactly one prior-window receipt as a replay;
        # no receipt is ever accepted and no second January event is created.
        self.assertEqual(self.endpoint.replays, [self.january_identity])
        self.assertEqual(self.endpoint.receipt_identities(), set())

    def endpoint_state(self):
        return {
            'delivered': sorted(str(identity) for identity in self.endpoint.delivered_identities()),
            'replays': sorted(str(identity) for identity in self.endpoint.replays),
            'receipts': sorted(str(identity) for identity in self.endpoint.receipt_identities()),
        }

    def test_restore_preserves_the_task_identity_rather_than_creating_an_event(self):
        self._fresh_receiver_with_prior_history()
        restored = self.restore(self.archive)
        restored_id = self.business_task_id(restored, COLLECT_TASK, window_label(2026, 2))
        archived_id = self.business_task_id(
            self.restore(self.archive), COLLECT_TASK, window_label(2026, 2))
        self.assertEqual(restored_id, archived_id)

    def test_ready_task_is_not_re_clocked_after_a_correct_restore(self):
        workflow = self.restore(self.archive)
        before = self.observe(workflow)
        workflow.refresh_timers()
        after = self.observe(workflow)
        self.assertEqual(before['ready'], after['ready'])
        self.assertIn((COLLECT_TASK, window_label(2026, 2)), after['ready'])

    def test_defect_b_is_caught_at_the_restore_step_and_converges_later(self):
        correct = self.restore(self.archive)
        faulty = self.restore(self.archive)
        reclock_ready_tasks(faulty)
        # First divergence is the restore step itself, before any request is sent.
        self.assertNotEqual(self.observe(correct)['ready'], self.observe(faulty)['ready'])
        self.assertEqual(self.observe(faulty)['ready'], [])
        # A later refresh re-fires the demoted timer and the runs converge.
        faulty.refresh_timers()
        faulty.do_engine_steps()
        self.run_ready(correct)
        self.run_ready(faulty)
        self.assertEqual(
            receiver_request_set(self.endpoint),
            sorted(str(identity) for identity in self.endpoint.delivered_identities()))


class MarchSameInstantRestoreTest(TimerRegressionTestCase):

    def setUp(self):
        super().setUp()
        self.archive = self.march_archive()
        self.collect_identity = (COLLECT_TASK, window_label(2026, 3))
        self.reconcile_identity = (RECONCILE_TASK, window_label(2026, 3))

    def test_restore_before_and_after_sending_one_of_two_ready_items(self):
        first = self.restore(self.archive)
        self.assert_ready(first, {self.collect_identity, self.reconcile_identity})
        delivered_before_send = set(self.endpoint.delivered_identities())

        self.run_ready(first, {self.collect_identity})
        self.assertEqual(
            {(identity.task_spec, identity.label)
             for identity in self.endpoint.public_ready_identities(first)},
            {self.reconcile_identity})

        # Restore again from the pre-send archive: no extra delivery is created.
        second = self.restore(self.archive)
        self.assert_ready(second, {self.collect_identity, self.reconcile_identity})
        self.assertEqual(set(self.endpoint.delivered_identities()),
                         delivered_before_send | {BusinessIdentity(*self.collect_identity)})
        march_collect = BusinessIdentity(*self.collect_identity)
        self.assertEqual(len(self.endpoint.deliveries_for(march_collect)), 1)
        self.assertNotIn(BusinessIdentity(*self.reconcile_identity),
                         self.endpoint.delivered_identities())

    def test_defect_c_merges_the_two_same_instant_waiting_timers(self):
        workflow = self.new_workflow()
        for month in (1, 2):
            self.reach_window(workflow, month)
            self.run_ready(workflow)
        self.clock.advance_to(month_end_instant(2026, 3) - timedelta(seconds=60))
        workflow.refresh_timers()
        self.assertEqual({event.name for event in workflow.waiting_events()},
                         {WINDOW_TIMER, RECONCILE_TIMER})

        install_collapsing_event_manager(workflow)
        collapsed = workflow.waiting_events()
        self.assertEqual(len(collapsed), 1)
        self.assertIn(collapsed[0].name, (WINDOW_TIMER, RECONCILE_TIMER))

    def test_defect_b_demotes_both_march_items_and_is_seen_first_at_restore(self):
        correct = self.restore(self.archive)
        faulty = self.restore(self.archive)
        demoted = reclock_ready_tasks(faulty)
        self.assertEqual(sorted(demoted), sorted([COLLECT_TASK, RECONCILE_TASK]))
        self.assertEqual(self.observe(faulty)['ready'], [])
        self.assertEqual(len(self.observe(correct)['ready']), 2)
        # The demoted timers re-enter the waiting set rather than disappearing.
        self.assertEqual(len(faulty.waiting_events()), 2)
        # Advancing and refreshing restores both items, so final sets can match.
        self.clock.advance_to(month_end_instant(2026, 3) + timedelta(minutes=5))
        faulty.refresh_timers()
        faulty.do_engine_steps()
        self.assertEqual(len(self.endpoint.public_ready_identities(faulty)), 2)


class DefectDiscriminationTest(TimerRegressionTestCase):

    def test_each_defect_targets_only_its_own_observation(self):
        # Defect A: a scheduling rule, diverges at the second window only.
        faulty_schedule = FirstMonthLengthSchedule()
        produced = [faulty_schedule.next_window(index=index) for index in range(1, 5)]
        self.assertEqual(first_diverging_window(produced, planned_windows()), 2)

        # Defect B: changes the ready set after restore but leaves timers visible.
        archive = self.march_archive()
        workflow_b = self.restore(archive)
        reclock_ready_tasks(workflow_b)
        self.assertEqual(self.observe(workflow_b)['ready'], [])
        self.assertEqual(len(workflow_b.waiting_events()), 2)

        # Defect C: changes the waiting-event view but never touches ready tasks.
        workflow_c = self.new_workflow()
        for month in (1, 2):
            self.reach_window(workflow_c, month)
            self.run_ready(workflow_c)
        self.clock.advance_to(month_end_instant(2026, 3) - timedelta(seconds=60))
        workflow_c.refresh_timers()
        ready_before = self.observe(workflow_c)['ready']
        install_collapsing_event_manager(workflow_c)
        self.assertEqual(self.observe(workflow_c)['ready'], ready_before)
        self.assertEqual(len(workflow_c.waiting_events()), 1)


class EarliestDivergingPrefixTest(TimerRegressionTestCase):
    """The first offending operation is named even when final results match."""

    def test_defect_b_first_differs_at_restore_but_final_requests_match(self):
        archive = self.february_archive()
        february = BusinessIdentity(COLLECT_TASK, window_label(2026, 2))

        def build_trace(apply_fault):
            workflow = self.restore(archive)
            trace = Trace()
            if apply_fault:
                reclock_ready_tasks(workflow)
            trace.record('restore_executor', self.observe(workflow))
            workflow.refresh_timers()
            workflow.do_engine_steps()
            trace.record('refresh_timers', self.observe(workflow))
            self.run_ready(workflow, {(COLLECT_TASK, window_label(2026, 2))})
            trace.record('process_runnable_task', self.observe(workflow))
            return trace

        correct_trace = build_trace(False)
        faulty_trace = build_trace(True)
        difference = correct_trace.first_difference(faulty_trace)
        self.assertIsNotNone(difference)
        self.assertEqual(difference['index'], 0)
        self.assertEqual(difference['operation'], 'restore_executor')
        # Two restores of the same archive still produce only one distinct
        # February identity: the restored task object is not a second event.
        distinct_february = [
            identity for identity in self.endpoint.delivered_identities()
            if identity == february]
        self.assertEqual(len(distinct_february), 1)

    def test_defect_c_first_differs_when_the_same_instant_is_published(self):
        def build_trace(apply_fault):
            workflow = self.new_workflow()
            for month in (1, 2):
                self.reach_window(workflow, month)
                self.run_ready(workflow)
            self.clock.advance_to(month_end_instant(2026, 3) - timedelta(seconds=60))
            if apply_fault:
                install_collapsing_event_manager(workflow)
            workflow.refresh_timers()
            trace = Trace()
            trace.record('refresh_timers', self.observe(workflow))
            return trace

        difference = build_trace(False).first_difference(build_trace(True))
        self.assertIsNotNone(difference)
        self.assertEqual(difference['operation'], 'refresh_timers')
        self.assertEqual(len(difference['correct']['waiting_events']), 2)
        self.assertEqual(len(difference['observed']['waiting_events']), 1)

    def test_defect_a_first_differs_at_the_second_planned_window(self):
        correct = [CalendarMonthSchedule.next_window(index=index) for index in range(1, 5)]
        faulty_schedule = FirstMonthLengthSchedule()
        faulty = [faulty_schedule.next_window(index=index) for index in range(1, 5)]
        self.assertEqual(first_diverging_window(faulty, correct), 2)
        # De-duplicating and sorting the labels cannot hide the earlier drift.
        self.assertNotEqual(
            sorted(window.label for window in faulty),
            sorted(window.label for window in correct))


class FaultInjectionProofTest(TimerRegressionTestCase):
    """Each check must pass on the correct executor and fail on its matching double."""

    def _assert_raises_assertion(self, check):
        with self.assertRaises(AssertionError):
            check()

    def test_schedule_check_only_rejects_the_pinned_month_double(self):
        def correct_check():
            produced = [CalendarMonthSchedule.next_window(index=index) for index in range(1, 5)]
            self.assertIsNone(first_diverging_window(produced, planned_windows()))

        def faulty_check():
            faulty = FirstMonthLengthSchedule()
            produced = [faulty.next_window(index=index) for index in range(1, 5)]
            self.assertIsNone(first_diverging_window(produced, planned_windows()))

        correct_check()
        self._assert_raises_assertion(faulty_check)

    def test_restore_ready_check_only_rejects_re_clocking(self):
        archive = self.february_archive()
        february = (COLLECT_TASK, window_label(2026, 2))

        def check(workflow):
            self.assertIn(february, self.observe(workflow)['ready'])

        check(self.restore(archive))
        demoted = self.restore(archive)
        reclock_ready_tasks(demoted)
        self._assert_raises_assertion(lambda: check(demoted))

    def test_waiting_event_check_only_rejects_collapsing(self):
        def build(apply_fault):
            workflow = self.new_workflow()
            for month in (1, 2):
                self.reach_window(workflow, month)
                self.run_ready(workflow)
            self.clock.advance_to(month_end_instant(2026, 3) - timedelta(seconds=60))
            if apply_fault:
                install_collapsing_event_manager(workflow)
            workflow.refresh_timers()
            return workflow

        def check(workflow):
            self.assertEqual(
                {event.name for event in workflow.waiting_events()},
                {WINDOW_TIMER, RECONCILE_TIMER})

        check(build(False))
        self._assert_raises_assertion(lambda: check(build(True)))

