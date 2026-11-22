"""Step-by-step trace recording and earliest-diverging-prefix comparison.

A trace is an ordered list of named operations together with the public
observation taken immediately after each one.  Two traces (a correct run and a
run containing a fault double) can be compared to name the first operation at
which they differ, even when their final, de-duplicated request sets are equal.
"""

from dataclasses import dataclass


@dataclass(frozen=True)
class RecordedStep:
    name: str
    observation: dict


class Trace:
    def __init__(self):
        self.steps = []

    def record(self, name, observation):
        self.steps.append(RecordedStep(name, observation))
        return observation

    def names(self):
        return [step.name for step in self.steps]

    def first_difference(self, other):
        """Return the first step that differs between two traces.

        Returns a dict with the step index, the operation name and the two
        differing observations, or ``None`` when the traces are identical.
        """
        for index, (left, right) in enumerate(zip(self.steps, other.steps)):
            if left.observation != right.observation:
                return {
                    'index': index,
                    'operation': left.name,
                    'correct': left.observation,
                    'observed': right.observation,
                }
        if len(self.steps) != len(other.steps):
            index = min(len(self.steps), len(other.steps))
            name = (self.steps[index].name if index < len(self.steps)
                    else other.steps[index].name)
            return {'index': index, 'operation': name, 'correct': None, 'observed': None}
        return None


def receiver_request_set(endpoint):
    """All identities the endpoint was asked to handle, de-duplicated and sorted.

    Deliveries and accepted receipts are merged into one set, mirroring what an
    outside auditor would see after removing repeats and sorting.
    """
    identities = endpoint.delivered_identities() | endpoint.receipt_identities()
    return sorted(str(identity) for identity in identities)
