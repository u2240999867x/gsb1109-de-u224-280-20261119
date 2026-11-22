#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/dispatch.py
#
#  Redistribution and use in source and binary forms, with or without
#  modification, are permitted provided that the following conditions are
#  met:
#
#  * Redistributions of source code must retain the above copyright
#    notice, this list of conditions and the following disclaimer.
#  * Redistributions in binary form must reproduce the above
#    copyright notice, this list of conditions and the following disclaimer
#    in the documentation and/or other materials provided with the
#    distribution.
#  * Neither the name of the project nor the names of its
#    contributors may be used to endorse or promote products derived from
#    this software without specific prior written permission.
#
#  THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS
#  "AS IS" AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT
#  LIMITED TO, THE IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR
#  A PARTICULAR PURPOSE ARE DISCLAIMED. IN NO EVENT SHALL THE COPYRIGHT
#  OWNER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT, INCIDENTAL,
#  SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES (INCLUDING, BUT NOT
#  LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR SERVICES; LOSS OF USE,
#  DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER CAUSED AND ON ANY
#  THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT LIABILITY, OR TORT
#  (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY OUT OF THE USE
#  OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH DAMAGE.
#

"""
Versioned supplier dictionaries backing auditable work-order dispatch.

Each supplier owns an append-only sequence of immutable dictionary snapshots. A snapshot maps each
source code (``原码``) to the fact kinds it is allowed to evidence and maps tag aliases onto the
source code they stand for. Every dispatch conclusion (``Evaluation``) records the supplier and
dictionary version it was derived from, so the proof can be re-checked against that exact snapshot
forever, even after later patches change the supplier's dictionary.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Sequence


class FactKind(Enum):
	"""The kinds of dispatch facts a source code can evidence."""

	RETURN = 'return'
	"""退单: a returned order (the codes ``R`` and ``RMA`` both resolve to this kind)."""
	EXPEDITE = 'expedite'
	"""加急: an expedited order (e.g. the code ``X`` may resolve here)."""
	NIGHT = 'night'
	"""夜班或节假日: eligibility for the night / holiday shift (e.g. the code ``Y`` may resolve here)."""


REQUIRED_FACTS: tuple[FactKind, ...] = (FactKind.RETURN, FactKind.EXPEDITE, FactKind.NIGHT)
"""The fact kinds that must each be evidenced once before an order can be dispatched."""


class Outcome(Enum):
	"""The auditable classification of an :class:`Evaluation`."""

	PROVEN = 'proven'
	"""真实的派班证明: every required fact is evidenced by a distinct source code."""
	INSUFFICIENT = 'insufficient'
	"""明确不足: the code is known and fields are present, but required facts cannot all be matched."""
	MISSING_FIELDS = 'missing_fields'
	"""字段缺失: the work order is missing one of the fields needed to audit it."""
	UNKNOWN_CODE = 'unknown_code'
	"""只有未知原码: the order's source code does not exist in this dictionary version."""


@dataclass(frozen=True)
class Fact:
	"""A single piece of dispatch evidence: one source code backing one fact kind on one order."""

	kind: FactKind
	"""The fact kind that was evidenced."""
	code: str
	"""The source code (``原码``) backing the fact, after alias normalization."""
	alias: str
	"""The tag alias that actually appeared on the work order."""
	vendor: str
	"""The supplier whose dictionary authorized this fact."""
	version: int
	"""The dictionary version the fact was taken from."""


@dataclass(frozen=True)
class Ticket:
	"""A work order (工单) whose dispatch eligibility must be evidenced.

	Any field may be ``None`` so that an incomplete order can be classified as
	:py:attr:`Outcome.MISSING_FIELDS` instead of raising.
	"""

	ticket_id: str | None
	vendor: str | None
	code: str | None = None
	tags: tuple[str, ...] | None = None

	@classmethod
	def from_mapping(cls, value: Mapping[str, Any]) -> 'Ticket':
		tags = value.get('tags')
		if tags is not None:
			tags = tuple(tags)
		return cls(
			ticket_id=value.get('ticket_id'),
			vendor=value.get('vendor'),
			code=value.get('code'),
			tags=tags,
		)


@dataclass(frozen=True)
class Evaluation:
	"""The versioned, re-checkable result of evaluating one :class:`Ticket`."""

	ticket_id: str | None
	vendor: str | None
	version: int
	outcome: Outcome
	facts: tuple[Fact, ...] = ()
	missing_kinds: tuple[FactKind, ...] = ()
	missing_fields: tuple[str, ...] = ()
	unknown_aliases: tuple[str, ...] = ()
	unused_aliases: tuple[str, ...] = ()

	@property
	def shift(self) -> str | None:
		"""The assigned shift; only a fully proven order carrying a night fact is dispatched."""
		return FactKind.NIGHT.value if self.outcome is Outcome.PROVEN else None

	@property
	def is_night_eligible(self) -> bool:
		return self.outcome is Outcome.PROVEN


class DictionarySnapshot:
	"""
	An immutable, versioned supplier dictionary.

	``entries`` maps a source code to the ordered, de-duplicated fact kinds it may evidence. A code
	may carry several candidate interpretations (e.g. ``X`` may be either a return or an expedite
	fact), but on any single order a code backs at most one fact; repeated aliases normalizing to the
	same code therefore count as only one fact.
	"""

	__slots__ = ('vendor', 'version', 'entries', 'aliases', 'parent')

	def __init__(
		self,
		vendor: str,
		version: int,
		entries: Mapping[str, tuple[FactKind, ...]],
		aliases: Mapping[str, str],
		parent: 'DictionarySnapshot | None' = None,
	):
		self.vendor = vendor
		self.version = version
		self.entries = MappingProxyType(dict(entries))
		self.aliases = MappingProxyType(dict(aliases))
		self.parent = parent

	def __repr__(self) -> str:
		return "{0}(vendor={1!r}, version={2})".format(type(self).__name__, self.vendor, self.version)

	def normalize_alias(self, alias: str) -> str:
		"""Resolve a tag alias to its source code, identity mapping when no alias is registered."""
		return self.aliases.get(alias, alias)

	def kinds_for(self, code: str) -> tuple[FactKind, ...] | None:
		return self.entries.get(code)

	def fact_candidates(self, codes: Mapping[str, str]) -> dict[FactKind, list[str]]:
		"""Build, per fact kind, the distinct known codes available on an order.

		Codes are offered in the order they first appear on the ticket (primary code first, then its
		tag aliases), which is the documented priority when several codes can evidence the same kind.
		"""
		candidates: dict[FactKind, list[str]] = {}
		for code in codes:
			for kind in self.entries[code]:
				candidates.setdefault(kind, []).append(code)
		return candidates


def _normalize_entry(raw: Any) -> tuple[FactKind, ...]:
	if isinstance(raw, FactKind):
		raw = (raw,)
	if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes, bytearray)):
		raise TypeError("dictionary entry must be a sequence of FactKind values, not {0}".format(type(raw).__name__))
	kinds: list[FactKind] = []
	for item in raw:
		if not isinstance(item, FactKind):
			raise TypeError("dictionary entry values must be FactKind members, not {0}".format(type(item).__name__))
		if item not in kinds:
			kinds.append(item)
	if not kinds:
		raise ValueError('a dictionary entry must evidence at least one fact kind; use None in a patch to delete it')
	return tuple(kinds)


class DictionaryRegistry:
	"""
	Holds the immutable dictionary snapshots of every supplier.

	Patches never mutate a published snapshot: they derive a new snapshot carrying the next version
	number, so conclusions taken under an older version remain re-checkable forever.
	"""

	def __init__(self) -> None:
		self._lock = threading.RLock()
		self._snapshots: dict[tuple[str, int], DictionarySnapshot] = {}
		self._heads: dict[str, int] = {}

	def register(
		self,
		vendor: str,
		entries: Mapping[str, Sequence[FactKind]],
		aliases: Mapping[str, str] | None = None,
	) -> DictionarySnapshot:
		"""Register the first (version ``1``) dictionary snapshot for a supplier."""
		with self._lock:
			if vendor in self._heads:
				raise ValueError("supplier {0!r} already has a dictionary; use patch() to update it".format(vendor))
			normalized = {code: _normalize_entry(entry) for code, entry in entries.items()}
			snapshot = DictionarySnapshot(vendor, 1, normalized, dict(aliases or {}), parent=None)
			self._snapshots[(vendor, 1)] = snapshot
			self._heads[vendor] = 1
			return snapshot

	def patch(
		self,
		vendor: str,
		changes: Mapping[str, Sequence[FactKind] | None],
		alias_changes: Mapping[str, str | None] | None = None,
	) -> DictionarySnapshot:
		"""
		Apply a partial update to a supplier's dictionary, producing the next version.

		An entry mapped to ``None`` deletes that source code; an alias mapped to ``None`` removes the
		alias. Codes and aliases absent from the patch are inherited unchanged.
		"""
		with self._lock:
			head_version = self._heads.get(vendor)
			if head_version is None:
				raise KeyError("supplier {0!r} has no registered dictionary".format(vendor))
			parent = self._snapshots[(vendor, head_version)]
			entries = dict(parent.entries)
			for code, entry in changes.items():
				if entry is None:
					entries.pop(code, None)
				else:
					entries[code] = _normalize_entry(entry)
			aliases = dict(parent.aliases)
			for alias, target in (alias_changes or {}).items():
				if target is None:
					aliases.pop(alias, None)
				else:
					aliases[alias] = target
			next_version = head_version + 1
			snapshot = DictionarySnapshot(vendor, next_version, entries, aliases, parent=parent)
			self._snapshots[(vendor, next_version)] = snapshot
			self._heads[vendor] = next_version
			return snapshot

	def get(self, vendor: str, version: int | None = None) -> DictionarySnapshot:
		"""Return the snapshot for a supplier; ``version`` defaults to the latest one."""
		with self._lock:
			if version is None:
				version = self._heads.get(vendor)
				if version is None:
					raise KeyError("supplier {0!r} has no registered dictionary".format(vendor))
			try:
				return self._snapshots[(vendor, version)]
			except KeyError:
				raise KeyError("no version {0} dictionary for supplier {1!r}".format(version, vendor)) from None

	def latest_version(self, vendor: str) -> int | None:
		with self._lock:
			return self._heads.get(vendor)

	def versions(self, vendor: str) -> tuple[int, ...]:
		with self._lock:
			return tuple(version for (owner, version) in sorted(self._snapshots) if owner == vendor)


def _select_facts(
	candidates: Mapping[FactKind, Sequence[str]],
	first_alias: Mapping[str, str],
	vendor: str,
	version: int,
) -> tuple[tuple[Fact, ...], tuple[FactKind, ...]]:
	"""
	Match required fact kinds to distinct source codes as a maximum bipartite matching.

	Each code can be assigned at most once (one source code backs a single fact on an order), so an
	ambiguous code such as ``X`` never yields two facts. Kuhn's augmenting-path algorithm walks the
	required kinds in fixed order and the candidate codes in ticket evidence order, giving a
	deterministic result that prefers the earliest alias and still re-routes earlier picks when a
	complete match is possible. The maximum partial match is returned when no complete match exists.
	"""
	match_to_kind: dict[str, FactKind] = {}

	def augment(kind: FactKind, seen: set[str]) -> bool:
		for code in candidates.get(kind, ()):
			if code in seen:
				continue
			seen.add(code)
			holder = match_to_kind.get(code)
			if holder is None or augment(holder, seen):
				match_to_kind[code] = kind
				return True
		return False

	for kind in REQUIRED_FACTS:
		augment(kind, set())

	kind_to_code = {matched_kind: code for code, matched_kind in match_to_kind.items()}
	facts = tuple(
		Fact(kind=kind, code=kind_to_code[kind], alias=first_alias[kind_to_code[kind]], vendor=vendor, version=version)
		for kind in REQUIRED_FACTS
		if kind in kind_to_code
	)
	missing = tuple(kind for kind in REQUIRED_FACTS if kind not in kind_to_code)
	return facts, missing


_TICKET_FIELDS = ('ticket_id', 'vendor', 'code', 'tags')


def evaluate(ticket: Ticket | Mapping[str, Any], snapshot: DictionarySnapshot) -> Evaluation:
	"""Evaluate one ticket against one frozen dictionary snapshot."""
	if not isinstance(ticket, Ticket):
		ticket = Ticket.from_mapping(ticket)

	missing_fields = tuple(
		name for name in _TICKET_FIELDS if getattr(ticket, name) is None
	)
	if missing_fields:
		return Evaluation(
			ticket_id=ticket.ticket_id,
			vendor=ticket.vendor,
			version=snapshot.version,
			outcome=Outcome.MISSING_FIELDS,
			missing_fields=missing_fields,
		)

	# the primary source code and every tag alias are the same kind of evidence once normalized
	assert ticket.code is not None and ticket.tags is not None
	evidence_aliases = (ticket.code, *ticket.tags)
	first_alias: dict[str, str] = {}
	unknown_aliases: list[str] = []
	for alias in evidence_aliases:
		code = snapshot.normalize_alias(alias)
		if snapshot.kinds_for(code) is None:
			unknown_aliases.append(alias)
			continue
		# repeated aliases resolving to the same code count as a single fact source
		first_alias.setdefault(code, alias)

	if not first_alias:
		return Evaluation(
			ticket_id=ticket.ticket_id,
			vendor=ticket.vendor,
			version=snapshot.version,
			outcome=Outcome.UNKNOWN_CODE,
			unknown_aliases=tuple(unknown_aliases),
		)

	candidates = snapshot.fact_candidates(first_alias)
	facts, missing_kinds = _select_facts(candidates, first_alias, snapshot.vendor, snapshot.version)
	selected_codes = {fact.code for fact in facts}
	occurrences: dict[str, int] = {}
	unused_aliases: list[str] = []
	for alias in evidence_aliases:
		code = snapshot.normalize_alias(alias)
		if code not in first_alias:
			continue
		occurrences[code] = occurrences.get(code, 0) + 1
		if code not in selected_codes or occurrences[code] > 1:
			unused_aliases.append(alias)
	outcome = Outcome.PROVEN if not missing_kinds else Outcome.INSUFFICIENT
	return Evaluation(
		ticket_id=ticket.ticket_id,
		vendor=ticket.vendor,
		version=snapshot.version,
		outcome=outcome,
		facts=facts,
		missing_kinds=missing_kinds,
		unknown_aliases=tuple(unknown_aliases),
		unused_aliases=tuple(unused_aliases),
	)


@dataclass(frozen=True)
class NightBatchReport:
	"""The classified outcome of re-running the night batch after a dictionary patch."""

	vendor: str
	old_version: int
	new_version: int
	enqueued: tuple[str, ...] = ()
	revoked: tuple[str, ...] = ()
	rewritten: tuple[str, ...] = ()
	changed: tuple[str, ...] = ()
	unaffected: tuple[str, ...] = ()
	previous: Mapping[str, Evaluation] = field(default_factory=dict)
	current: Mapping[str, Evaluation] = field(default_factory=dict)

	def _signature(self, evaluation: Evaluation) -> tuple[Any, ...]:
		return (
			evaluation.outcome,
			tuple(sorted((fact.code, fact.kind) for fact in evaluation.facts)),
		)


class DispatchService:
	"""
	Evaluates tickets against versioned supplier dictionaries and keeps every conclusion queryable.

	Conclusions are stored per (ticket, dictionary version) and never overwritten. The latest
	conclusion of a ticket is simply the one taken at its highest known version number, so a
	late-arriving evaluation built from an older dictionary is retained in history but can never
	clobber a newer conclusion.
	"""

	def __init__(self, registry: DictionaryRegistry | None = None) -> None:
		self.registry = registry or DictionaryRegistry()
		self._lock = threading.RLock()
		self._history: dict[tuple[str, int], Evaluation] = {}
		self._latest: dict[str, Evaluation] = {}
		self._tickets: dict[tuple[str, str], Ticket] = {}
		self._night_queue: set[str] = set()

	def _commit(self, ticket: Ticket, evaluation: Evaluation) -> Evaluation:
		assert evaluation.ticket_id is not None and evaluation.vendor is not None
		ticket_id = evaluation.ticket_id
		vendor = evaluation.vendor
		key = (ticket_id, evaluation.version)
		existing = self._history.get(key)
		if existing is not None and existing != evaluation:
			raise ValueError(
				"refusing to overwrite history for ticket {0!r} at version {1}".format(
					ticket_id, evaluation.version
				)
			)
		self._history[key] = evaluation
		self._tickets[(ticket_id, vendor)] = ticket
		current = self._latest.get(ticket_id)
		if current is None or evaluation.version > current.version:
			self._latest[ticket_id] = evaluation
			if evaluation.is_night_eligible:
				self._night_queue.add(ticket_id)
			else:
				self._night_queue.discard(ticket_id)
		return evaluation

	def evaluate_ticket(self, ticket: Ticket | Mapping[str, Any], *, version: int | None = None) -> Evaluation:
		with self._lock:
			if not isinstance(ticket, Ticket):
				ticket = Ticket.from_mapping(ticket)
			if ticket.ticket_id is None or ticket.vendor is None:
				raise ValueError('a ticket filed with the service requires both a ticket_id and a vendor')
			snapshot = self.registry.get(ticket.vendor, version)
			return self._commit(ticket, evaluate(ticket, snapshot))

	def evaluate_batch(
		self,
		tickets: Sequence[Ticket | Mapping[str, Any]],
		*,
		version: int | None = None,
	) -> tuple[Evaluation, ...]:
		return tuple(self.evaluate_ticket(ticket, version=version) for ticket in tickets)

	def apply_night_patch(
		self,
		vendor: str,
		tickets: Sequence[Ticket | Mapping[str, Any]],
		changes: Mapping[str, Sequence[FactKind] | None],
		*,
		alias_changes: Mapping[str, str | None] | None = None,
	) -> NightBatchReport:
		"""
		Patch a supplier dictionary and re-run the night batch without disturbing unrelated orders.

		Only tickets whose evidence signature actually changes are reclassified; tickets unrelated to
		the patched codes keep their prior conclusion and queue membership, so the batch never
		re-enqueues the large volume of orders that the patch does not concern.
		"""
		with self._lock:
			old_snapshot = self.registry.get(vendor)
			previous: dict[str, Evaluation] = {}
			for evaluation in self.evaluate_batch(tickets, version=old_snapshot.version):
				assert evaluation.ticket_id is not None
				previous[evaluation.ticket_id] = evaluation
			new_snapshot = self.registry.patch(vendor, changes, alias_changes)
			current: dict[str, Evaluation] = {}
			for evaluation in self.evaluate_batch(tickets, version=new_snapshot.version):
				assert evaluation.ticket_id is not None
				current[evaluation.ticket_id] = evaluation

			enqueued: list[str] = []
			revoked: list[str] = []
			rewritten: list[str] = []
			changed: list[str] = []
			unaffected: list[str] = []
			report = NightBatchReport(
				vendor=vendor,
				old_version=old_snapshot.version,
				new_version=new_snapshot.version,
			)
			for ticket_id in sorted(previous):
				old = previous[ticket_id]
				new = current[ticket_id]
				if report._signature(old) == report._signature(new):
					unaffected.append(ticket_id)
				elif old.is_night_eligible and new.is_night_eligible:
					rewritten.append(ticket_id)
				elif old.is_night_eligible:
					revoked.append(ticket_id)
				elif new.is_night_eligible:
					enqueued.append(ticket_id)
				else:
					changed.append(ticket_id)

			return NightBatchReport(
				vendor=vendor,
				old_version=old_snapshot.version,
				new_version=new_snapshot.version,
				enqueued=tuple(enqueued),
				revoked=tuple(revoked),
				rewritten=tuple(rewritten),
				changed=tuple(changed),
				unaffected=tuple(unaffected),
				previous=MappingProxyType(previous),
				current=MappingProxyType(current),
			)

	def get_evaluation(self, ticket_id: str, *, version: int | None = None) -> Evaluation | None:
		"""Return a stored conclusion; ``version`` selects history while ``None`` returns the latest."""
		with self._lock:
			if version is None:
				return self._latest.get(ticket_id)
			return self._history.get((ticket_id, version))

	def history_versions(self, ticket_id: str) -> tuple[int, ...]:
		"""Return the dictionary versions under which a ticket has stored conclusions, ascending."""
		with self._lock:
			return tuple(version for (ticket, version) in sorted(self._history) if ticket == ticket_id)

	def audit(self, ticket_id: str, *, version: int | None = None) -> Evaluation:
		"""Re-derive a stored conclusion from its exact supplier/version snapshot and check it matches."""
		with self._lock:
			stored = self.get_evaluation(ticket_id, version=version)
			if stored is None:
				raise KeyError("no stored evaluation for ticket {0!r}".format(ticket_id))
			assert stored.ticket_id is not None and stored.vendor is not None
			stored_vendor = stored.vendor
			ticket = self._tickets[(stored.ticket_id, stored_vendor)]
			snapshot = self.registry.get(stored_vendor, stored.version)
			derived = evaluate(ticket, snapshot)
			if derived != stored:
				raise AssertionError(
					"stored evaluation for ticket {0!r} does not match version {1} dictionary".format(
						ticket_id, stored.version
					)
				)
			return derived

	@property
	def night_queue(self) -> frozenset[str]:
		with self._lock:
			return frozenset(self._night_queue)
