"""Recalculation and versioned publication over a parallel multi-instance task.

The parallel multi-instance task stands in for the collection summary: each instance
is one entry of the startup manifest.  A manifest entry never disappears, even while
a recalculation is in flight.

Domain mapping:
  * manifest entry / MI instance ...... a sampling job at one location
  * sampling record ................... fixed when the sample is first taken; a
                                        recalculation reuses it, it is never re-sampled
  * calibration sheet ................. versioned, with the time range it actually
                                        applies to; a newer sheet covering a sample
                                        supersedes older ones for that sample
  * calculation receipt ............... evidence left by a completed calculation
                                        (sample record, calibration, sampling time)
  * release ........................... immutable snapshot, published only once every
                                        location has a result based on its currently
                                        valid sheet; old release numbers stay queryable
"""

import copy
import json
import unittest

from SpiffWorkflow import TaskState
from SpiffWorkflow.bpmn import BpmnWorkflow

from tests.SpiffWorkflow.bpmn.BpmnWorkflowTestCase import BpmnWorkflowTestCase


PENDING = 'pending'

NORTH = 'north'
EAST = 'east'

# Fixed startup manifest order: north morning, east all day, north afternoon.
# The two north jobs share a station id, but keep separate sampling records.
LOCATIONS = ['north-am', 'east-all', 'north-pm']

SAMPLES = {
    'north-am': {'station': NORTH, 'taken_at': 1, 'record': 'sample-north-am'},
    'east-all': {'station': EAST, 'taken_at': 3, 'record': 'sample-east'},
    'north-pm': {'station': NORTH, 'taken_at': 2, 'record': 'sample-north-pm'},
}


class CalibrationRecalculationTest(BpmnWorkflowTestCase):

    def setUp(self):
        spec, _ = self.load_workflow_spec('parallel_multiinstance_loop_input.bpmn', 'main')
        self.workflow = BpmnWorkflow(spec)
        self.workflow.get_next_task(end_at_spec='Start').data = {'input_data': LOCATIONS}
        self.workflow.do_engine_steps()
        self.assertEqual(len(self.ready_children()), 3)

        # Registration order defines which sheet is newest; ranges are inclusive.
        # Only v1 exists when the first summary is produced; v2 arrives later.
        self.calibrations = {
            NORTH: [
                ('v1', {'applies_from': 1, 'applies_to': 2}),
            ],
            EAST: [
                ('v1', {'applies_from': 1, 'applies_to': 3}),
            ],
        }
        self.ledger = {location: [] for location in LOCATIONS}
        self.in_flight = set()
        self.reset_locations = []
        self.releases = {}
        self.confirmations = set()

    # -- calibration applicability ------------------------------------------------

    def register_sheet(self, station, sheet, applies_from, applies_to):
        self.calibrations[station].append((sheet, {
            'applies_from': applies_from,
            'applies_to': applies_to,
        }))

    def sheet_covers(self, station, sheet, taken_at):
        ranges = dict(self.calibrations[station])
        applies = ranges[sheet]
        return applies['applies_from'] <= taken_at <= applies['applies_to']

    def effective_sheet(self, location):
        sample = SAMPLES[location]
        covering = [
            sheet for sheet, applies in self.calibrations[sample['station']]
            if applies['applies_from'] <= sample['taken_at'] <= applies['applies_to']
        ]
        return covering[-1]

    # -- workflow driving ---------------------------------------------------------

    def child(self, location):
        tasks = [task for task in self.workflow.get_tasks()
                 if task.data.get('input_item') == location]
        self.assertEqual(len(tasks), 1)
        return tasks[0]

    def ready_children(self):
        return self.workflow.get_tasks(state=TaskState.READY)

    def parent_task(self):
        return self.workflow.get_next_task(spec_name='any_task')

    def instance_state(self):
        """Locations whose instances are completed vs. still running."""
        info = self.parent_task().task_spec.task_info(self.parent_task())
        completed = [LOCATIONS[index] for index in info['completed']]
        running = [LOCATIONS[index] for index in info['running']]
        # The manifest never loses an entry while a recalculation is open.
        self.assertEqual(len(info['instance_map']), len(LOCATIONS))
        return completed, running

    def final_slots(self):
        """Once the loop completes, output slots line up with the manifest order."""
        self.assertTrue(self.workflow.completed)
        slots = self.workflow.data['output_data']
        self.assertEqual(len(slots), len(LOCATIONS))
        return dict(zip(LOCATIONS, slots))

    def retained_slot(self, location):
        """While an instance is open, the last full table keeps the slot's old value."""
        self.assertFalse(self.workflow.completed)
        slots = self.workflow.data['output_data']
        return slots[LOCATIONS.index(location)]

    def begin_recalc(self, location, sheet):
        """Reset one instance without touching its sampling record."""
        sample = SAMPLES[location]
        self.assertTrue(self.sheet_covers(sample['station'], sheet, sample['taken_at']))
        self.workflow.reset_from_task_id(self.child(location).id)
        self.workflow.do_engine_steps()
        self.reset_locations.append(location)
        self.in_flight.add(location)
        self.assertEqual(self.child(location).state, TaskState.READY)
        # While open, only this instance is running and the manifest keeps all entries.
        completed, running = self.instance_state()
        self.assertEqual(running, [location])
        self.assertNotIn(location, completed)
        self.assertEqual(len(completed) + len(running), len(LOCATIONS))
        return self.snapshot()

    def accept(self, location, sheet, result):
        """Complete an instance and accept its receipt, rejecting stale submissions."""
        sample = SAMPLES[location]
        history = self.ledger[location]

        # A submission on a sheet no longer valid for this sample is always stale,
        # even if it matches an earlier accepted receipt.
        if sheet != self.effective_sheet(location):
            raise ValueError(
                f'stale submission at {location}: {sheet} is no longer valid for '
                f"sample taken at {sample['taken_at']}"
            )

        # Replayed success for the same sample and sheet: deduplicated, not appended.
        if history and history[-1]['sample'] == sample['record'] \
                and history[-1]['calibration'] == sheet:
            return history[-1]

        task = self.child(location)
        self.assertEqual(task.state, TaskState.READY)
        self.assertEqual(task.data['input_item'], location)
        task.data['output_item'] = result
        task.run()
        self.workflow.do_engine_steps()
        # When the loop is whole again, the new receipt is in its own manifest slot.
        if self.workflow.completed:
            self.assertEqual(self.final_slots()[location], result)

        receipt = {
            'location': location,
            'sample': sample['record'],
            'taken_at': sample['taken_at'],
            'calibration': sheet,
            'result': result,
            'revision': len(history) + 1,
        }
        history.append(receipt)
        self.in_flight.discard(location)
        return receipt

    # -- archiving and replay -----------------------------------------------------

    def snapshot(self):
        return {
            'workflow': json.loads(json.dumps(self.serializer.to_dict(self.workflow))),
            'ledger': copy.deepcopy(self.ledger),
            'in_flight': set(self.in_flight),
            'reset_locations': list(self.reset_locations),
            'releases': copy.deepcopy(self.releases),
            'confirmations': set(self.confirmations),
        }

    def restore(self, archive):
        self.workflow = self.serializer.from_dict(copy.deepcopy(archive['workflow']))
        self.ledger = copy.deepcopy(archive['ledger'])
        self.in_flight = set(archive['in_flight'])
        self.reset_locations = list(archive['reset_locations'])
        self.releases = copy.deepcopy(archive['releases'])
        self.confirmations = set(archive['confirmations'])

    def confirm(self, release_number):
        self.assertIn(release_number, self.releases)
        self.confirmations.add(release_number)

    # -- summary and publication ---------------------------------------------------

    def summary(self):
        self.assertEqual(set(self.ledger), set(LOCATIONS))
        current = {}
        for location in LOCATIONS:
            if location in self.in_flight:
                current[location] = PENDING
            else:
                current[location] = self.ledger[location][-1]
        return current

    def assert_receipt_valid(self, location):
        """Three-way check: sampling time, the sheet's real range, and the receipt."""
        receipt = self.ledger[location][-1]
        sample = SAMPLES[location]
        self.assertEqual(receipt['sample'], sample['record'])
        self.assertEqual(receipt['taken_at'], sample['taken_at'])
        self.assertTrue(
            self.sheet_covers(sample['station'], receipt['calibration'], sample['taken_at']))
        self.assertEqual(receipt['calibration'], self.effective_sheet(location))
        return receipt

    def publish(self):
        summary = self.summary()
        if PENDING in summary.values():
            return None
        for location in LOCATIONS:
            receipt = summary[location]
            sample = SAMPLES[location]
            if receipt['calibration'] != self.effective_sheet(location) or not \
                    self.sheet_covers(sample['station'], receipt['calibration'],
                                      sample['taken_at']):
                return None
        release_number = len(self.releases) + 1
        self.releases[release_number] = copy.deepcopy(summary)
        return release_number

    def release_view(self, release_number):
        return copy.deepcopy(self.releases[release_number])

    # -- shared timeline up to the v2/v3 overlap ----------------------------------

    def first_version(self):
        for location in LOCATIONS:
            self.accept(location, 'v1', f'{location}-result-v1')
        self.assertTrue(self.workflow.completed)
        release_one = self.publish()
        self.assertEqual(release_one, 1)
        # Accepted, but the confirmation has not arrived yet; it stays queryable.
        self.assertNotIn(1, self.confirmations)
        return release_one

    def v2_wave(self):
        # The north v2 sheet covers both north samples and only recalculates them;
        # nothing is re-sampled.
        self.register_sheet(NORTH, 'v2', applies_from=1, applies_to=2)
        self.begin_recalc('north-am', 'v2')
        self.accept('north-am', 'v2', 'north-am-result-v2')

        # The afternoon calculation is still running when v3 is announced.
        archive = self.begin_recalc('north-pm', 'v2')
        return archive

    def settle_v2_afternoon(self, archive):
        # Archive/restore at the old/new overlap, then let the v2 afternoon arrive.
        self.restore(archive)
        self.assertEqual(
            [t.data['input_item'] for t in self.ready_children()], ['north-pm'])
        self.assertEqual(self.retained_slot('north-pm'), 'north-pm-result-v1')
        self.accept('north-pm', 'v2', 'north-pm-result-v2')
        # Replay messages delivered while the archive was open; both are idempotent.
        self.confirm(1)
        self.accept('north-am', 'v2', 'north-am-result-v2')
        self.assertIn(1, self.confirmations)
        for location in LOCATIONS:
            self.assert_receipt_valid(location)

    def testFirstReleaseRemainsQueryableBeforeConfirmation(self):
        release_one = self.first_version()
        view = self.release_view(release_one)
        self.assertEqual(
            [view[location]['calibration'] for location in LOCATIONS],
            ['v1', 'v1', 'v1'])
        self.assertEqual(
            [view[location]['sample'] for location in LOCATIONS],
            [SAMPLES[location]['record'] for location in LOCATIONS])

    def testScenarioAMorningOnlyV3(self):
        release_one = self.first_version()
        archive_v2 = self.v2_wave()
        self.settle_v2_afternoon(archive_v2)

        # v3 only adjusts the morning slot; the afternoon sample is outside its range.
        self.register_sheet(NORTH, 'v3', applies_from=1, applies_to=1)
        self.assertEqual(self.effective_sheet('north-am'), 'v3')
        self.assertEqual(self.effective_sheet('north-pm'), 'v2')

        resets_before_v3 = len(self.reset_locations)
        archive_v3 = self.begin_recalc('north-am', 'v3')
        self.restore(archive_v3)
        # The restored workflow still has the morning instance open and its old
        # slot value is retained until the v3 receipt arrives.
        self.assertEqual(
            [t.data['input_item'] for t in self.ready_children()], ['north-am'])
        self.assertEqual(self.retained_slot('north-am'), 'north-am-result-v2')
        self.accept('north-am', 'v3', 'north-am-result-v3')
        self.confirm(1)

        # The afternoon v2 result is reused: the v3 wave only resets the morning.
        self.assertEqual(self.reset_locations[resets_before_v3:], ['north-am'])
        self.assertEqual(self.reset_locations.count('north-pm'), 1)
        self.assertEqual(self.ledger['north-pm'][-1]['calibration'], 'v2')
        self.assertEqual(self.ledger['north-pm'][-1]['sample'], 'sample-north-pm')

        # East is never recalculated.
        self.assertNotIn('east-all', self.reset_locations)
        self.assertEqual(len(self.ledger['east-all']), 1)

        summary = self.summary()
        self.assertNotIn(PENDING, summary.values())
        release_two = self.publish()
        self.assertEqual(release_two, 2)
        self.confirm(release_two)

        current = self.summary()
        self.assertEqual(current['north-am']['calibration'], 'v3')
        self.assertEqual(current['north-am']['sample'], 'sample-north-am')
        self.assertEqual(current['north-pm']['calibration'], 'v2')
        self.assertEqual(current['east-all']['calibration'], 'v1')
        for location in LOCATIONS:
            self.assert_receipt_valid(location)
        # The engine's ordered output reconciles with the accepted receipts one to one.
        self.assertEqual(self.workflow.data['output_data'],
                         [current[location]['result'] for location in LOCATIONS])

        # The original release number still resolves to what was actually accepted.
        first_view = self.release_view(release_one)
        self.assertEqual(
            [first_view[location]['calibration'] for location in LOCATIONS],
            ['v1', 'v1', 'v1'])
        self.assertEqual(
            [self.release_view(2)[location]['calibration'] for location in LOCATIONS],
            ['v3', 'v1', 'v2'])

    def testScenarioBV3CoversTheAfternoon(self):
        release_one = self.first_version()
        archive_v2 = self.v2_wave()
        self.settle_v2_afternoon(archive_v2)

        # v3 reaches into the afternoon, so both north samples must be based on v3.
        self.register_sheet(NORTH, 'v3', applies_from=1, applies_to=2)
        self.assertEqual(self.effective_sheet('north-am'), 'v3')
        self.assertEqual(self.effective_sheet('north-pm'), 'v3')

        self.begin_recalc('north-am', 'v3')
        self.accept('north-am', 'v3', 'north-am-result-v3')
        archive_v3 = self.begin_recalc('north-pm', 'v3')

        # While the afternoon is open, the summary keeps its manifest entry as pending.
        summary = self.summary()
        self.assertEqual(set(summary), set(LOCATIONS))
        self.assertEqual(summary['north-pm'], PENDING)
        self.assertEqual(summary['north-am']['calibration'], 'v3')
        self.assertIsNone(self.publish())

        # A late v2 submission must not overwrite the new version while it is in flight.
        with self.assertRaises(ValueError):
            self.accept('north-pm', 'v2', 'north-pm-stale-v2')
        # Nothing moved: the instance stays open and the manifest entry stays pending.
        self.assertEqual(self.child('north-pm').state, TaskState.READY)
        self.assertEqual(self.summary()['north-pm'], PENDING)

        self.restore(archive_v3)
        # After the archive is restored the afternoon instance is still open; its
        # slot keeps the previously accepted v2 value until a valid receipt lands.
        self.assertEqual(
            [t.data['input_item'] for t in self.ready_children()], ['north-pm'])
        self.assertEqual(self.retained_slot('north-pm'), 'north-pm-result-v2')
        self.accept('north-pm', 'v3', 'north-pm-result-v3')
        self.confirm(1)
        self.accept('north-am', 'v3', 'north-am-result-v3')

        release_two = self.publish()
        self.assertEqual(release_two, 2)
        self.confirm(release_two)

        current = self.summary()
        self.assertEqual(
            [current[location]['calibration'] for location in LOCATIONS],
            ['v3', 'v1', 'v3'])
        for location in LOCATIONS:
            self.assert_receipt_valid(location)
        self.assertNotIn('east-all', self.reset_locations)
        self.assertEqual(self.workflow.data['output_data'],
                         [current[location]['result'] for location in LOCATIONS])

        # Both north samples kept their original sampling records.
        self.assertEqual(current['north-am']['sample'], 'sample-north-am')
        self.assertEqual(current['north-pm']['sample'], 'sample-north-pm')

        # New version data can never be written onto the first release's confirmation.
        first_view = self.release_view(release_one)
        self.assertEqual(
            [first_view[location]['calibration'] for location in LOCATIONS],
            ['v1', 'v1', 'v1'])
        first_view['north-am']['calibration'] = 'tampered'
        self.assertEqual(self.release_view(release_one)['north-am']['calibration'], 'v1')


if __name__ == '__main__':
    unittest.main()
