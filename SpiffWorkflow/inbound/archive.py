"""Workflow archive for an inbound receiving review.

The archive wraps a real :class:`SpiffWorkflow.Workflow` whose task states are
the authoritative "workflow display state".  Domain data (the form, the
per-line verification identities, confirmed snapshots and the archived
snapshot) lives alongside it and is serialized with it.

The archive intentionally has no write access to the reservation ledger and
the ledger has no knowledge of the archive.  Callers coordinate the two; the
two stores are never updated in a single transaction.
"""

import json
from copy import deepcopy
from datetime import datetime, timezone
from typing import Any, Optional

from SpiffWorkflow import TaskState, Workflow
from SpiffWorkflow.serializer.json import JSONSerializer
from SpiffWorkflow.specs import Simple, WorkflowSpec

from .model import (
    FormVersion,
    FormVersionError,
    PreverificationError,
    ArchivedVersionError,
    DeliveryError,
    as_form_version,
    changed_line_values,
    merge_versions,
    normalize_line_map,
    validate_complete,
)


ARCHIVE_FORMAT = 'spiffworkflow-inbound-review/1'


class Stage:
    DRAFT = 'draft'
    PREVERIFIED = 'preverified'
    CONFIRMED = 'confirmed'
    DELIVERY = 'delivery'
    BLOCKED = 'blocked'
    PUBLISHED = 'published'
    ROLLED_BACK = 'rolled_back'

# Linear milestone workflow.  The number is how many milestone tasks must be
# completed for the workflow to display that stage.
_MILESTONES = ['Draft', 'Preverify', 'Confirm', 'Delivery', 'Published', 'Complete']

# Index (in _MILESTONES) of the task that is READY while the batch is in a
# stage; every earlier milestone task is completed.
_STAGE_READY_INDEX = {
    Stage.DRAFT: 0,
    Stage.PREVERIFIED: 2,
    Stage.CONFIRMED: 3,
    Stage.DELIVERY: 4,
    Stage.BLOCKED: 3,
    Stage.PUBLISHED: 5,
    Stage.ROLLED_BACK: 2,
}


def _build_spec(name: str) -> WorkflowSpec:
    spec = WorkflowSpec(name=name, addstart=True)
    previous = spec.start
    for task_name in _MILESTONES:
        task = Simple(spec, task_name, manual=True)
        previous.connect(task)
        previous = task
    return spec


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


class LineIdentity:
    """A concrete preverification identity for one material line.

    The identity, not the visible station/quantity pair, is what an external
    reservation system can confirm.  Two identities can describe the same
    visible values and still be different reservations.
    """

    def __init__(self, line_no, reservation_id, station, quantity,
                 preverified_version=None, expires_at=None):
        self.line_no = line_no
        self.reservation_id = reservation_id
        self.station = station
        self.quantity = quantity
        self.preverified_version = preverified_version
        self.expires_at = expires_at

    def matches_values(self, line) -> bool:
        return self.station == line.station and self.quantity == line.quantity

    def to_dict(self) -> dict:
        return {
            'line_no': self.line_no,
            'reservation_id': self.reservation_id,
            'station': self.station,
            'quantity': self.quantity,
            'preverified_version': self.preverified_version,
            'expires_at': self.expires_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> 'LineIdentity':
        return cls(
            line_no=data['line_no'],
            reservation_id=data['reservation_id'],
            station=data['station'],
            quantity=data['quantity'],
            preverified_version=data.get('preverified_version'),
            expires_at=data.get('expires_at'),
        )

    def __repr__(self):
        return (f'LineIdentity(line={self.line_no}, reservation={self.reservation_id}, '
                f'{self.station}/{self.quantity})')


class InboundReviewBatch:
    """The canonical review of one inbound batch."""

    def __init__(self, batch_id: str, form: Any, workflow: Optional[Workflow] = None):
        self.batch_id = batch_id
        self.form = as_form_version(form)
        self.stage = Stage.DRAFT
        self.identities = {}
        self.confirmed = None
        self.confirmations = []
        self.archived = None
        self.blocked_stations = []
        self.events = []
        self._workflow = workflow or Workflow(_build_spec(f'inbound-review-{batch_id}'))
        if workflow is None:
            self._sync_workflow(Stage.DRAFT)

    # ------------------------------------------------------------------
    # Workflow lifecycle
    # ------------------------------------------------------------------
    @property
    def workflow(self) -> Workflow:
        return self._workflow

    def _completed_milestones(self) -> list:
        done = []
        for name in _MILESTONES:
            tasks = self._workflow.get_tasks(spec_name=name, state=TaskState.COMPLETED)
            if tasks:
                done.append(name)
        return done

    def _ready_manual_task(self):
        for task in self._workflow.get_tasks(state=TaskState.READY):
            if task.task_spec.manual:
                return task
        return None

    def _next_ready_task(self):
        for task in self._workflow.get_tasks(state=TaskState.READY):
            return task
        return None

    def _sync_workflow(self, stage: str) -> None:
        """Advance or rewind the milestone workflow to display ``stage``."""
        target = _STAGE_READY_INDEX[stage]

        for _ in range(len(_MILESTONES) * 2 + 1):
            ready = self._workflow.get_tasks(state=TaskState.READY)
            ready_names = [task.task_spec.name for task in ready]
            if _MILESTONES[target] in ready_names:
                return
            task = self._next_ready_task()
            if task is None:
                break
            if task.task_spec.name in _MILESTONES:
                index = _MILESTONES.index(task.task_spec.name)
                if index > target:
                    break
            task.run()

        # Rewinding: reset the milestone that should be READY again.
        name = _MILESTONES[target]
        tasks = self._workflow.get_tasks(spec_name=name)
        if not tasks:
            raise ValueError(f'cannot rewind workflow: missing milestone {name!r}')
        self._workflow.reset_from_task_id(tasks[0].id)
        self._workflow.completed = False

        for _ in range(len(_MILESTONES) * 2 + 1):
            ready = self._workflow.get_tasks(state=TaskState.READY)
            if any(task.task_spec.name == name for task in ready):
                break
            task = self._next_ready_task()
            if task is None or (task.task_spec.name in _MILESTONES
                                and _MILESTONES.index(task.task_spec.name) > target):
                break
            task.run()

    def workflow_status(self) -> dict:
        ready = self._ready_manual_task()
        return {
            'stage': self.stage,
            'completed_tasks': self._completed_milestones(),
            'ready_task': ready.task_spec.name if ready is not None else None,
            'workflow_completed': self._workflow.is_completed(),
        }

    def _enter_stage(self, stage: str) -> None:
        self.stage = stage
        self._sync_workflow(stage)

    # ------------------------------------------------------------------
    # Preverification
    # ------------------------------------------------------------------
    def preverify(self, ledger, line_numbers=None) -> dict:
        """Allocate a fresh reservation identity for one or more lines.

        Every invocation creates new reservation identities.  It is never
        valid to treat a previous confirmation as applying to a newly
        allocated reservation, even when the station and quantity are equal.

        While a delivery outcome is unknown, working-draft preverification is
        still allowed: it neither submits a delivery nor touches the saved
        confirmation.  The workflow remains ``blocked`` until the outcome is
        resolved.
        """
        self._assert_not_archived('preverify')
        if self.stage == Stage.CONFIRMED:
            raise PreverificationError(
                'the version is already confirmed; roll back before preverifying new identities'
            )
        validate_complete(self.form, 'preverification')

        targets = self.form.lines
        if line_numbers is not None:
            wanted = set(line_numbers)
            targets = [line for line in self.form.lines if line.line_no in wanted]

        allocated = {}
        for line in targets:
            reservation = ledger.reserve(
                form_id=self.batch_id,
                line_no=line.line_no,
                station=line.station,
                quantity=line.quantity,
                preverified_version=self.form.version,
            )
            identity = LineIdentity(
                line_no=line.line_no,
                reservation_id=reservation.reservation_id,
                station=line.station,
                quantity=line.quantity,
                preverified_version=self.form.version,
                expires_at=reservation.expires_at.isoformat() if reservation.expires_at else None,
            )
            self.identities[line.line_no] = identity
            allocated[line.line_no] = identity

        if self.blocked_stations:
            self.stage = Stage.BLOCKED
        elif self._all_identified():
            self._enter_stage(Stage.PREVERIFIED)
        self._record('preverify', {
            'version': self.form.version,
            'reservations': {line_no: identity.reservation_id
                             for line_no, identity in allocated.items()},
        })
        return allocated

    def identity_for(self, line_no: int) -> Optional[LineIdentity]:
        return self.identities.get(line_no)

    def assert_identity_current(self, line_no: int, identity: Optional[LineIdentity] = None) -> None:
        """Raise unless the supplied identity is the current live identity.

        Equal station/quantity values are explicitly not enough.  This is the
        guard against claiming that a prior confirmation survives a re-entry
        of the original values.
        """
        current = self.identities.get(line_no)
        if current is None:
            raise PreverificationError(f'line {line_no} has no preverified identity')
        if identity is not None and identity.reservation_id != current.reservation_id:
            raise PreverificationError(
                f'line {line_no} has reservation {current.reservation_id}; '
                f'{identity.reservation_id} is a different identity'
            )
        line = self.form.line(line_no)
        if not current.matches_values(line):
            raise PreverificationError(
                f'line {line_no} reservation {current.reservation_id} was for '
                f'{current.station}/{current.quantity}, but the form now shows '
                f'{line.station}/{line.quantity}'
            )

    # ------------------------------------------------------------------
    # Confirmation and publication
    # ------------------------------------------------------------------
    def confirm(self) -> dict:
        """Snapshot the complete form and every line's current identity."""
        self._assert_not_archived('confirm')
        if self.blocked_stations:
            raise DeliveryError(
                'cannot modify the saved original confirmation while a delivery outcome is unknown'
            )
        validate_complete(self.form, 'confirmation')
        if self.stage not in (Stage.PREVERIFIED, Stage.ROLLED_BACK):
            raise PreverificationError(f'form cannot be confirmed from stage {self.stage}')
        for line in self.form.lines:
            self.assert_identity_current(line.line_no, self.identities.get(line.line_no))

        snapshot = {
            'version': self.form.version,
            'confirmed_at': _utcnow_iso(),
            'form': self.form.to_dict(),
            'identities': {
                line.line_no: self.identities[line.line_no].to_dict()
                for line in self.form.lines
            },
        }
        self.confirmed = snapshot
        self.confirmations.append(deepcopy(snapshot))
        self._enter_stage(Stage.CONFIRMED)
        self._record('confirm', {'version': self.form.version})
        return deepcopy(snapshot)

    def confirmed_lines(self) -> dict:
        if self.confirmed is None:
            return {}
        return {int(line_no): LineIdentity.from_dict(data)
                for line_no, data in self.confirmed['identities'].items()}

    def latest_confirmation_lines(self) -> dict:
        """The most recent confirmation snapshot, even if it was invalidated."""
        if self.confirmations:
            snapshot = self.confirmations[-1]
        elif self.confirmed is not None:
            snapshot = self.confirmed
        else:
            return {}
        return {int(line_no): LineIdentity.from_dict(data)
                for line_no, data in snapshot['identities'].items()}

    def mark_station_blocked(self, station: str, request_id: str) -> None:
        if station not in self.blocked_stations:
            self.blocked_stations.append(station)
        self._enter_stage(Stage.BLOCKED)
        self._record('delivery-unknown', {'station': station, 'request_id': request_id})

    def resolve_station_outcome(self, station: str, accepted: bool) -> None:
        """Record the external resolution of an unknown delivery."""
        if station in self.blocked_stations:
            self.blocked_stations.remove(station)
        if self.blocked_stations:
            return
        self._enter_stage(Stage.CONFIRMED)
        self._record('delivery-resolved', {'station': station, 'accepted': accepted})

    def roll_back(self, reason: str) -> None:
        """Abandon the current confirmation and reopen it for a new one.

        The prior confirmation snapshots are retained for audit.  Rolling back
        is only permitted when no delivery outcome is unknown.
        """
        self._assert_not_archived('roll back')
        if self.blocked_stations:
            raise DeliveryError('cannot roll back while a delivery outcome is unknown')
        if self.confirmed is None:
            raise PreverificationError('there is no confirmation to roll back')
        self.confirmed = None
        self._enter_stage(Stage.ROLLED_BACK)
        self._record('rollback', {'reason': reason})
        if not self._all_identified():
            self._enter_stage(Stage.DRAFT)

    def _assert_not_archived(self, operation: str) -> None:
        if self.archived is not None:
            raise ArchivedVersionError(
                f'cannot {operation} batch {self.batch_id}: version '
                f'{self.archived["version"]} is already warehoused'
            )

    def publish(self, ledger) -> dict:
        """Publish the confirmed version when all of its deliveries were accepted.

        The archived version is the *confirmed snapshot*, never the current
        working form.  A working form that happens to show identical station
        and quantity values is not evidence that a newer reservation identity
        was delivered, and later draft edits cannot change what was confirmed
        and delivered.
        """
        self._assert_not_archived('publish')
        if self.blocked_stations or ledger.has_pending_request(self.batch_id):
            raise DeliveryError('cannot publish while a delivery outcome is unknown')
        if self.confirmed is None:
            raise DeliveryError('cannot publish before the version is confirmed')

        confirmed_form = FormVersion.from_dict(self.confirmed['form'])
        validate_complete(confirmed_form, 'publication')

        confirmed_identities = self.confirmed_lines()
        for line in confirmed_form.lines:
            identity = confirmed_identities[line.line_no]
            if identity.station != line.station or identity.quantity != line.quantity:
                raise DeliveryError(f'confirmed identity for line {line.line_no} does not match the form')
            if not ledger.identity_was_accepted(self.batch_id, identity.station,
                                                identity.reservation_id):
                raise DeliveryError(
                    f'reservation {identity.reservation_id} for line {line.line_no} '
                    f'has not been accepted by station {identity.station!r}'
                )

        self.archived = deepcopy(self.confirmed)
        self.archived['published_at'] = _utcnow_iso()
        self._enter_stage(Stage.PUBLISHED)
        self._record('publish', {
            'version': self.archived['version'],
            'identities': {line_no: identity.reservation_id
                           for line_no, identity in confirmed_identities.items()},
        })
        return deepcopy(self.archived)

    @property
    def is_archived(self) -> bool:
        return self.archived is not None

    @property
    def is_visible(self) -> bool:
        return self.is_archived

    # ------------------------------------------------------------------
    # Edits and merging
    # ------------------------------------------------------------------
    def begin_edit(self, base: Any = None) -> 'EditSession':
        """Start an edit session against the current batch.

        ``base`` is the draft the editor holds.  It may be older than the
        canonical form; station/quantity changes are merged by stable line
        number, while row order and remarks compose with a concurrent editor.
        """
        return EditSession(self, base if base is not None else self.form)

    def apply_edit(self, session: 'EditSession') -> FormVersion:
        if self.archived is not None:
            raise ArchivedVersionError(
                f'cannot modify batch {self.batch_id}: version '
                f'{self.archived["version"]} is already warehoused'
            )
        merged = merge_versions(self.form, session.base, session.candidate)
        changed = changed_line_values(self.form, merged)

        for line_no in changed:
            if line_no not in session.identities and session.ledger is not None:
                session._allocate(line_no, session.ledger)
            if line_no in session.identities:
                identity = session.identities[line_no]
                line = merged.line(line_no)
                if identity.matches_values(line):
                    self.identities[line_no] = identity
                else:
                    self.identities.pop(line_no, None)
            else:
                self.identities.pop(line_no, None)

        self.form = FormVersion(
            version=max(self.form.version, session.candidate.version),
            form_id=merged.form_id,
            lines=merged.lines,
            notes=merged.notes,
        )

        # The saved original confirmation is immutable while a delivery
        # outcome is unknown.  Outside that window, a visible change that no
        # longer matches the confirmation invalidates it.
        if self.confirmed is not None and not self.blocked_stations:
            confirmed_values = FormVersion.from_dict(self.confirmed['form']).line_values()
            if confirmed_values != self.form.line_values():
                self.confirmed = None
                self._enter_stage(Stage.PREVERIFIED if self._all_identified() else Stage.DRAFT)
                self._record('confirmation-invalidated', {'version': self.form.version})

        self._record('merge', {
            'editor': session.editor,
            'changed_lines': sorted(changed),
            'metadata_only': not changed,
        })
        return self.form

    def _all_identified(self) -> bool:
        return all(line.line_no in self.identities for line in self.form.lines)

    def _record(self, event: str, details: dict) -> None:
        self.events.append({'event': event, 'at': _utcnow_iso(), **details})

    # ------------------------------------------------------------------
    # Reconciliation
    # ------------------------------------------------------------------
    def reconcile(self, ledger) -> dict:
        """Reconcile station occupancy, workflow display, and line identity."""
        stations = {}
        confirmed_identities = self.confirmed_lines()
        latest_identities = self.latest_confirmation_lines()
        for station in self.form.stations():
            requests = [request for request in ledger.requests_for_form(self.batch_id)
                        if request.station == station]
            stations[station] = {
                'quota': ledger.quota(station),
                'occupancy': ledger.occupancy(station),
                'available': ledger.available(station),
                'requests': [
                    {
                        'request_id': request.request_id,
                        'status': request.status,
                        'reservation_ids': list(request.reservation_ids),
                    }
                    for request in requests
                ],
            }
        lines = []
        for line in self.form.lines:
            identity = self.identities.get(line.line_no)
            confirmed = confirmed_identities.get(line.line_no)
            latest = latest_identities.get(line.line_no)
            lines.append({
                'line_no': line.line_no,
                'visible': {
                    'station': line.station,
                    'quantity': line.quantity,
                },
                'identity': identity.reservation_id if identity else None,
                'confirmed_identity': confirmed.reservation_id if confirmed else None,
                'latest_confirmed_identity': latest.reservation_id if latest else None,
                'matches_confirmation': bool(
                    identity is not None and confirmed is not None
                    and identity.reservation_id == confirmed.reservation_id
                ),
                'values_match_confirmation': bool(
                    latest is not None
                    and line.station == latest.station
                    and line.quantity == latest.quantity
                ),
            })
        return {
            'batch_id': self.batch_id,
            'stage': self.stage,
            'visible': self.is_visible,
            'archived_version': self.archived['version'] if self.archived else None,
            'workflow': self.workflow_status(),
            'stations': stations,
            'lines': lines,
        }

    # ------------------------------------------------------------------
    # Save and restore
    # ------------------------------------------------------------------
    def state_dict(self) -> dict:
        return {
            'batch_id': self.batch_id,
            'form': self.form.to_dict(),
            'stage': self.stage,
            'identities': [identity.to_dict() for identity in self.identities.values()],
            'confirmed': deepcopy(self.confirmed),
            'confirmations': deepcopy(self.confirmations),
            'archived': deepcopy(self.archived),
            'blocked_stations': list(self.blocked_stations),
            'events': deepcopy(self.events),
        }

    def dump(self) -> str:
        payload = {
            'format': ARCHIVE_FORMAT,
            'state': self.state_dict(),
            'workflow': JSONSerializer().serialize_workflow(self._workflow),
        }
        return json.dumps(payload, sort_keys=True)

    @classmethod
    def load(cls, serialization: str) -> 'InboundReviewBatch':
        payload = json.loads(serialization)
        if payload.get('format') != ARCHIVE_FORMAT:
            raise ValueError(f'unsupported archive format {payload.get("format")!r}')
        workflow = Workflow.deserialize(JSONSerializer(), payload['workflow'])
        state = payload['state']
        batch = cls(state['batch_id'], FormVersion.from_dict(state['form']), workflow=workflow)
        batch.stage = state['stage']
        batch.identities = {
            int(identity['line_no']): LineIdentity.from_dict(identity)
            for identity in state.get('identities', [])
        }
        batch.confirmed = deepcopy(state.get('confirmed'))
        if batch.confirmed is not None:
            batch.confirmed['identities'] = normalize_line_map(batch.confirmed['identities'])
        batch.confirmations = deepcopy(state.get('confirmations', []))
        for confirmation in batch.confirmations:
            confirmation['identities'] = normalize_line_map(confirmation['identities'])
        batch.archived = deepcopy(state.get('archived'))
        if batch.archived is not None:
            batch.archived['identities'] = normalize_line_map(batch.archived['identities'])
        batch.blocked_stations = list(state.get('blocked_stations', []))
        batch.events = deepcopy(state.get('events', []))
        return batch


class EditSession:
    """An editor's pending changes merged by stable line number.

    Metadata-only changes (row order and form remarks) do not allocate any
    reservation.  Any station or quantity change can allocate a new
    reservation identity immediately, following the preverification rule.
    """

    def __init__(self, batch: InboundReviewBatch, base: Any, ledger=None):
        self.batch = batch
        self.base = as_form_version(base)
        self.ledger = ledger
        self.candidate = FormVersion(
            version=max(batch.form.version, self.base.version) + 1,
            form_id=batch.form.form_id,
            lines=deepcopy(self.base.lines),
            notes=self.base.notes,
        )
        self.identities = {}
        self.editor = None
        self.changed = set()

    def set_editor(self, editor: str) -> 'EditSession':
        self.editor = editor
        return self

    def reorder(self, line_numbers) -> 'EditSession':
        """Reorder rows without allocating reservations."""
        lines = {line.line_no: line for line in self.candidate.lines}
        if set(line_numbers) != set(lines):
            raise FormVersionError('a reorder must contain exactly the stable line numbers')
        self.candidate.lines = [deepcopy(lines[line_no]) for line_no in line_numbers]
        return self

    def set_notes(self, notes: str) -> 'EditSession':
        """Change the form remark without allocating reservations."""
        self.candidate.notes = notes
        return self

    def set_line(self, line_no: int, station: Optional[str] = None,
                 quantity: Optional[int] = None, ledger=None) -> 'EditSession':
        line = self.candidate.line(line_no)
        new_station = line.station if station is None else station
        new_quantity = line.quantity if quantity is None else quantity
        if new_station == line.station and new_quantity == line.quantity:
            return self
        new_line = type(line)(
            line_no=line_no,
            station=new_station,
            quantity=new_quantity,
            note=line.note,
        )
        index = [candidate.line_no for candidate in self.candidate.lines].index(line_no)
        self.candidate.lines[index] = new_line
        self.changed.add(line_no)
        if ledger is not None:
            self._allocate(line_no, ledger)
        return self

    def _allocate(self, line_no: int, ledger) -> 'EditSession':
        line = self.candidate.line(line_no)
        reservation = ledger.reserve(
            form_id=self.batch.batch_id,
            line_no=line_no,
            station=line.station,
            quantity=line.quantity,
            preverified_version=self.candidate.version,
        )
        self.identities[line_no] = LineIdentity(
            line_no=line_no,
            reservation_id=reservation.reservation_id,
            station=line.station,
            quantity=line.quantity,
            preverified_version=self.candidate.version,
            expires_at=reservation.expires_at.isoformat() if reservation.expires_at else None,
        )
        return self

    def merged_form(self) -> FormVersion:
        return merge_versions(self.batch.form, self.base, self.candidate)

    def commit(self) -> FormVersion:
        return self.batch.apply_edit(self)
