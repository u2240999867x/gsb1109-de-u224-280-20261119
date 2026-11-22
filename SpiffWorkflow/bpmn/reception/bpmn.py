# Copyright (C) 2026 Sartography
#
# This file is part of SpiffWorkflow.
#
# SpiffWorkflow is free software; you can redistribute it and/or
# modify it under the terms of the GNU Lesser General Public
# License as published by the Free Software Foundation; either
# version 3.0 of the License, or (at your option) any later version.
#
# SpiffWorkflow is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the GNU
# Lesser General Public License for more details.
#
# You should have received a copy of the GNU Lesser General Public
# License along with this program; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston, MA
# 02110-1301  USA

"""Bind a reception session to actual BPMN message waiting points.

The workflow remains responsible for its own state.  This adapter only maps
a receipt onto the message event the session is currently waiting for, and
is careful never to deliver a checksum receipt in place of a file receipt.
"""

from SpiffWorkflow import TaskState
from SpiffWorkflow.bpmn import BpmnEvent

from .models import ClaimStatus, ReceiptKind, SubmissionStatus
from .session import ReceptionSession, SessionStatus


class BpmnReceptionWorkflow:
    """Drive a reception workflow through its own archive.

    ``archive`` is a serialized workflow produced by
    :meth:`save_archive`; restoring it is the only way the adapter learns
    local progress.  External claim knowledge still comes from the service.
    """

    def __init__(self, workflow, session, serializer):
        self.workflow = workflow
        self.session = session
        self.serializer = serializer

    @classmethod
    def start(cls, workflow, session, serializer):
        instance = cls(workflow, session, serializer)
        session.save_to(workflow)
        return instance

    @classmethod
    def restore(cls, archive, service, serializer, registration=None):
        workflow = serializer.deserialize_json(archive)
        session = ReceptionSession.restore_from(workflow, service, registration)
        return cls(workflow, session, serializer)

    def save_archive(self):
        self.session.save_to(self.workflow)
        return self.serializer.serialize_json(self.workflow)

    @property
    def status(self):
        return SessionStatus(self.session.state.status)

    def waiting_event_names(self):
        return [
            task.task_spec.event_definition.name
            for task in self.workflow.get_tasks(state=TaskState.WAITING)
            if hasattr(task.task_spec, "event_definition")
        ]

    def deliver(self, receipt, fault=None):
        """Deliver a receipt to the workflow at its current waiting point."""

        result = self.session.receive(receipt, fault=fault)
        if result.status is SubmissionStatus.CONFLICT:
            return result
        if receipt.kind is ReceiptKind.FILE:
            return self._deliver_file(receipt)
        return self._deliver_checksum(receipt)

    def continue_after_handoff(self):
        """Adopt the handoff and advance the archive to the resumed step.

        When the predecessor already claimed the file, the successor's own
        archive may still be parked on a file wait that will never receive
        another file.  The file wait is completed locally; the successor then
        waits for the checksum instead of claiming the file again.
        """

        outcome = self.session.continue_after_handoff()
        if outcome.status is SessionStatus.FILE_CLAIMED:
            task = self._waiting_task("File Received")
            if task is not None and task.state == TaskState.WAITING:
                # The file is already claimed elsewhere; this local wait can
                # never be satisfied by another file receipt.
                task.run()
                self.workflow.do_engine_steps()
        return outcome

    def _deliver_file(self, receipt):
        if not self.session.is_owner():
            return self.session.report_unknown()
        outcome = self.session.claim_and_commit(receipt)
        if outcome.status is SessionStatus.FILE_CLAIMED:
            self._catch("File Received", receipt)
        return outcome

    def send_checksum(self):
        """Issue the checksum request as a separate, explicit step.

        Keeping this out of file delivery is what makes the handoff window
        observable: a claim can succeed while the checksum request has not
        yet been emitted.
        """

        outcome = self.session.send_checksum()
        return outcome

    def _deliver_checksum(self, receipt):
        if self.session.claim_status(evidence=True) is not ClaimStatus.CLAIMED:
            return self.session.report_unknown()
        outcome = self.session.receive_checksum(receipt)
        if outcome.status is SessionStatus.COMPLETE:
            self._catch("Checksum Received", receipt)
        return outcome

    def _catch(self, message_name, receipt):
        payload = dict(receipt.payload)
        task = self._waiting_task(message_name)
        if task is None:
            return
        self.workflow.catch(BpmnEvent(task.task_spec.event_definition, payload, {}))
        self.workflow.do_engine_steps()

    def _waiting_task(self, name):
        for state in (TaskState.WAITING, TaskState.READY):
            for task in self.workflow.get_tasks(state=state):
                spec = getattr(task.task_spec, "event_definition", None)
                if spec is not None and spec.name == name:
                    return task
        return None
