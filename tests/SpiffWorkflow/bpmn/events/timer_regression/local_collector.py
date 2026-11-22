"""In-process stand-in for the local store-collection endpoint.

Nothing here talks to a network or the real clock.  The endpoint subscribes to
the workflow's task-spec completion signals, so it observes the same task
objects the public workflow API exposes.  Business identities are derived from
the plan (window labels) rather than from transient task ids, which keeps a
restored process instance from being mistaken for a brand-new delivery.
"""

from dataclasses import dataclass

from SpiffWorkflow import TaskState

COLLECT_TASK = 'CollectStoreData'
RECONCILE_TASK = 'ReconcileReceipts'
WINDOW_TIMER = 'WaitForWindow'
RECONCILE_TIMER = 'WaitForReconciliation'

BUSINESS_TASKS = (COLLECT_TASK, RECONCILE_TASK)
_IDENTITY_DATA_KEY = {
    COLLECT_TASK: 'window_label',
    RECONCILE_TASK: 'reconcile_label',
}


@dataclass(frozen=True)
class BusinessIdentity:
    """A stable identity for one planned unit of work.

    ``task_spec`` names the business activity and ``label`` identifies the
    scheduled window (e.g. ``2026-02``).  Task objects produced by different
    restorations of the same archive share one identity.
    """

    task_spec: str
    label: str

    def __str__(self):
        return f'{self.task_spec}[{self.label}]'


def identity_for_task(task):
    key = _IDENTITY_DATA_KEY.get(task.task_spec.name)
    if key is None:
        return None
    label = task.get_data(key)
    return BusinessIdentity(task.task_spec.name, label)


@dataclass(frozen=True)
class Delivery:
    identity: BusinessIdentity
    task_id: str
    clock: str


@dataclass(frozen=True)
class ReceiptResult:
    accepted: bool
    identity: BusinessIdentity
    detail: str


class LocalCollectionEndpoint:
    """Records deliveries and rejects duplicate receipts for a window."""

    def __init__(self, clock=None):
        self.clock = clock
        self.deliveries = []
        self.receipts = {}
        self.replays = []
        self._subscriptions = []

    def attach(self, workflow):
        """Subscribe to completion of the two manual business tasks.

        Signals live on the (de)serialized task spec, so a restored workflow
        keeps notifying the same endpoint.  Specs are reused across workflows
        built in one test, hence the idempotency guard.
        """
        for spec_name in BUSINESS_TASKS:
            spec = workflow.spec.task_specs.get(spec_name)
            if spec.completed_event.is_connected(self._on_completed):
                continue
            spec.completed_event.connect(self._on_completed)
            self._subscriptions.append((spec, self._on_completed))
        return self

    def detach(self):
        for spec, callback in self._subscriptions:
            if spec.completed_event.is_connected(callback):
                spec.completed_event.disconnect(callback)
        self._subscriptions.clear()

    def reset(self):
        self.deliveries.clear()
        self.receipts.clear()
        self.replays.clear()

    def seed_prior_delivery(self, identity, task_id='prior-record'):
        """Record an already-delivered window as established history.

        This models the receiver remembering a previous window across an
        executor restart, so a later receipt for that identity is a replay.
        """
        self.deliveries.append(Delivery(identity, task_id, self._now()))

    def _now(self):
        return None if self.clock is None else self.clock.current.isoformat()

    def _on_completed(self, _workflow, task):
        identity = identity_for_task(task)
        if identity is None:
            return
        self.deliveries.append(Delivery(identity, str(task.id), self._now()))

    def submit_receipt(self, task):
        """Hand in a receipt for the window represented by ``task``.

        A receipt is only accepted for an identity that has never been
        delivered and never receipted.  A receipt for an already-delivered
        prior window (or a second receipt after restore) is a replay and is
        rejected rather than counted as a new delivery.
        """
        identity = identity_for_task(task)
        if identity in self.delivered_identities() or identity in self.receipts:
            self.replays.append(identity)
            return ReceiptResult(False, identity, 'duplicate receipt rejected')
        self.receipts[identity] = str(task.id)
        return ReceiptResult(True, identity, 'receipt accepted')

    def delivered_identities(self):
        return {delivery.identity for delivery in self.deliveries}

    def receipt_identities(self):
        return set(self.receipts)

    def deliveries_for(self, identity):
        return [delivery for delivery in self.deliveries if delivery.identity == identity]

    def public_ready_identities(self, workflow):
        return {
            identity
            for task in workflow.get_tasks(state=TaskState.READY)
            if task.task_spec.name in BUSINESS_TASKS
            for identity in [identity_for_task(task)]
            if identity is not None
        }
