#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/projection.py
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
#  OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION WITH THE SOFTWARE OR
#  THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#

"""
Staged field projection for rules.

This module analyzes the abstract syntax tree of a :py:class:`~rule_engine.engine.Rule` and derives a
*staged projection* from it: a description of which fields of the evaluated object the rule actually reads,
split into two stages so a caller can fetch data incrementally (for example, reading small control columns
from a columnar store first and only then deciding which large detail fields to load).

* **Control reads** (:py:data:`CONTROL`) are the *selection fields*: values the rule uses to decide what
  else to read. These are the fields referenced by comprehension iterables and conditions, ternary
  conditions, short-circuited logic operands and dynamic item keys, plus every read that is not guarded at
  all. They are fetched in stage one.
* **Detail reads** (:py:data:`DETAIL`) are reads that sit behind a guard, such as a comprehension result
  that is only evaluated for elements matching the condition, or a ternary branch that is only evaluated
  when the condition takes the corresponding side. They are resolved in stage two by :py:meth:`.ProjectionPlan.bind`
  which evaluates the guards against the stage-one (control) projection and emits concrete paths for the
  branches that are actually taken.

Comprehension variables are tracked as lexical bindings, so a comprehension that reuses a root symbol name
(for example ``for items in items['rows']``) attributes reads through the variable to the iterated path and
not to the root symbol. Dynamic item keys (``row[mapping[row['kind']]]``) are resolved while binding; when a
key can not be resolved (for example an optional entry of a batch-level mapping is absent), the projection
falls back to the candidate values of the mapping the key was drawn from, restricted to the element being
processed. It never falls back to projecting the whole root document.

The projection is conservative: if a guard or key can not be evaluated against the control projection, the
corresponding reads are included rather than dropped, so evaluating a rule against
``projection.project(thing, bound.paths)`` yields the same result as evaluating it against *thing* for
documents whose control flow depends only on control fields.
"""

import collections.abc
import copy
import decimal
import itertools
from typing import Any, Iterable, Iterator, TYPE_CHECKING

from . import ast, errors

if TYPE_CHECKING:
	from .engine import Rule

CONTROL = 'control'
"""The stage of reads that act as selection fields and are fetched first."""

DETAIL = 'detail'
"""The stage of guarded reads that are resolved once the control values are known."""

class _Subtree(object):
	"""A terminal path segment marking that the whole value below this point is read."""
	_instance: '_Subtree | None' = None
	def __new__(cls) -> '_Subtree':
		if cls._instance is None:
			cls._instance = super().__new__(cls)
		return cls._instance
	def __repr__(self) -> str:
		return 'SUBTREE'

SUBTREE = _Subtree()
"""Terminal path segment indicating the entire value at the path is part of the projection."""

class _Wildcard(object):
	"""A path segment matching every element of a sequence (or every key of a mapping)."""
	__slots__ = ('identity',)
	_ids = itertools.count()
	def __init__(self) -> None:
		self.identity = next(self._ids)
	def __eq__(self, other: Any) -> bool:
		return isinstance(other, _Wildcard) and self.identity == other.identity
	def __hash__(self) -> int:
		return hash(('_Wildcard', self.identity))
	def __repr__(self) -> str:
		return '*'

class DynamicKey(object):
	"""
	A path segment whose key is computed by an expression at projection-bind time, for example the
	``mapping[row['kind']]`` key of ``row[mapping[row['kind']]]``.
	"""
	__slots__ = ('expression', 'variables')
	def __init__(self, expression: ast.ExpressionBase, variables: tuple[tuple[str, tuple], ...]) -> None:
		self.expression = expression
		self.variables = variables
	def __eq__(self, other: Any) -> bool:
		return isinstance(other, DynamicKey) and self.expression is other.expression and self.variables == other.variables
	def __hash__(self) -> int:
		return hash((id(self.expression), self.variables))
	def __repr__(self) -> str:
		return '?'

class _Guard(object):
	"""
	An executable condition gating a detail read. The guard holds when *expression* evaluates to *expected*
	with the comprehension *variables* bound to the elements selected while binding the projection.
	"""
	__slots__ = ('expression', 'expected', 'variables')
	def __init__(self, expression: ast.ExpressionBase, expected: bool, variables: tuple[tuple[str, tuple], ...]) -> None:
		self.expression = expression
		self.expected = expected
		self.variables = variables
	def __eq__(self, other: Any) -> bool:
		return (
			isinstance(other, _Guard)
			and self.expression is other.expression
			and self.expected == other.expected
			and self.variables == other.variables
		)
	def __hash__(self) -> int:
		return hash((id(self.expression), self.expected, self.variables))

class ReadOrigin(object):
	"""The provenance of a retained field: which rule and which read within that rule required it."""
	__slots__ = ('rule_text', 'path')
	def __init__(self, rule_text: str, path: str) -> None:
		self.rule_text = rule_text
		"""The text of the rule whose read retains the field."""
		self.path = path
		"""The formatted path template of the read within the rule."""
	def __eq__(self, other: Any) -> bool:
		return isinstance(other, ReadOrigin) and self.rule_text == other.rule_text and self.path == other.path
	def __hash__(self) -> int:
		return hash((self.rule_text, self.path))
	def __repr__(self) -> str:
		return "<ReadOrigin {0!r} {1} >".format(self.rule_text, self.path)

class ReadSite(object):
	"""A single read of the evaluated object, found by statically analyzing a rule."""
	__slots__ = ('template', 'stage', 'guards', 'origin')
	def __init__(self, template: tuple, stage: str, guards: tuple[_Guard, ...], origin: ReadOrigin) -> None:
		self.template = template
		"""The path template of the read; segments are strings, integers, :py:class:`_Wildcard`, :py:class:`DynamicKey` or :py:data:`SUBTREE`."""
		self.stage = stage
		"""Either :py:data:`CONTROL` or :py:data:`DETAIL`."""
		self.guards = guards
		"""The guards that must all hold for the read to happen (empty for unconditional reads)."""
		self.origin = origin
		"""The :py:class:`ReadOrigin` tracing this read back to its rule."""
	def __eq__(self, other: Any) -> bool:
		return (
			isinstance(other, ReadSite)
			and self.template == other.template
			and self.stage == other.stage
			and self.guards == other.guards
		)
	def __hash__(self) -> int:
		return hash((self.template, self.stage, self.guards))
	def __repr__(self) -> str:
		return "<ReadSite {0} stage={1} >".format(format_path(self.template), self.stage)

def format_path(path: Iterable[Any]) -> str:
	"""
	Format a path or path template as a string. Wildcards render as ``[*]``, dynamic keys as ``[?]`` and the
	:py:data:`SUBTREE` terminal as ``[...]``.

	:param path: The path to format.
	:return: The formatted path, for example ``items.rows[*].active``.
	"""
	parts = []
	for segment in path:
		if segment is SUBTREE:
			parts.append('[...]')
		elif isinstance(segment, _Wildcard):
			parts.append('[*]')
		elif isinstance(segment, DynamicKey):
			parts.append('[?]')
		elif isinstance(segment, str):
			parts.append(('.' if parts else '') + segment)
		else:
			parts.append('[{0}]'.format(segment))
	return ''.join(parts)

################################################################################
# projection of documents
################################################################################
def _merge_tries(*tries: dict) -> dict:
	merged: dict = {}
	for trie in tries:
		if SUBTREE in trie:
			return {SUBTREE: {}}
		for segment, sub in trie.items():
			if segment in merged:
				merged[segment] = _merge_tries(merged[segment], sub)
			else:
				merged[segment] = sub
	return merged

def _skeleton(value: Any) -> Any:
	# the structure of *value* without any leaf values, used for paths that are read for their shape only
	# (such as a comprehension iterable whose elements are never inspected)
	if isinstance(value, collections.abc.Mapping):
		return {key: _skeleton(item) for key, item in value.items()}
	if isinstance(value, collections.abc.Sequence) and not isinstance(value, (str, bytes, bytearray)):
		return [_skeleton(item) for item in value]
	return None

def _project_value(value: Any, trie: dict) -> Any:
	if SUBTREE in trie:
		return copy.deepcopy(value)
	if not trie:
		return _skeleton(value)
	if isinstance(value, collections.abc.Mapping):
		wildcard_subs = [sub for segment, sub in trie.items() if isinstance(segment, _Wildcard)]
		projected = {}
		for segment, sub in trie.items():
			if isinstance(segment, str) and segment in value:
				projected[segment] = _project_value(value[segment], _merge_tries(sub, *wildcard_subs))
		for key, item in value.items():
			if key not in projected and wildcard_subs:
				projected[key] = _project_value(item, _merge_tries(*wildcard_subs))
		return projected
	if isinstance(value, collections.abc.Sequence) and not isinstance(value, (str, bytes, bytearray)):
		collected: dict[int, dict] = {}
		for segment, sub in trie.items():
			if isinstance(segment, int) and not isinstance(segment, bool):
				collected[segment] = _merge_tries(collected.get(segment, {}), sub)
			elif isinstance(segment, _Wildcard):
				for index in range(len(value)):
					collected[index] = _merge_tries(collected.get(index, {}), sub)
		# elements are compacted: indices in the projection refer to the original document
		elements = [_project_value(value[index], collected[index]) for index in sorted(collected) if 0 <= index < len(value)]
		return tuple(elements) if isinstance(value, tuple) else elements
	# a scalar can not be descended into; keep it as-is
	return value

def project(thing: Any, paths: Iterable[tuple]) -> Any:
	"""
	Restrict *thing* to the specified *paths*, returning a new document containing only the selected fields.
	Path segments are strings (mapping keys), integers (sequence indices), :py:class:`_Wildcard` (every
	element), or the :py:data:`SUBTREE` terminal (the whole remaining value). Sequence elements that are not
	selected are dropped, so indices in *paths* refer to the original document.

	:param thing: The document to restrict, composed of mappings, sequences and scalars.
	:param paths: The paths to retain.
	:return: The restricted document.
	"""
	trie: dict = {}
	for path in paths:
		node = trie
		for segment in path:
			node = node.setdefault(segment, {})
	return _project_value(thing, trie)

################################################################################
# static analysis
################################################################################
def _iter_child_expressions(expr: ast.ExpressionBase) -> Iterator[ast.ExpressionBase]:
	seen = set()
	for cls in type(expr).__mro__:
		for slot in getattr(cls, '__slots__', ()):
			seen.add(slot)
			try:
				value = getattr(expr, slot)
			except AttributeError:
				continue
			yield from _iter_expression_values(value)
	for key, value in getattr(expr, '__dict__', {}).items():
		if key in seen:
			continue
		yield from _iter_expression_values(value)

def _iter_expression_values(value: Any) -> Iterator[ast.ExpressionBase]:
	if isinstance(value, ast.ExpressionBase):
		yield value
	elif isinstance(value, (tuple, list)):
		for item in value:
			yield from _iter_expression_values(item)

def _literal_key(expression: ast.ExpressionBase) -> Any:
	"""The static key of a GetItem item expression, or :py:data:`errors.UNDEFINED` if it is dynamic."""
	if isinstance(expression, ast.StringExpression):
		return expression.value
	if isinstance(expression, ast.FloatExpression):
		value = expression.value
		return int(value) if float(value).is_integer() else value
	return errors.UNDEFINED

def _chain(expression: ast.ExpressionBase, variables: dict[str, tuple]) -> 'tuple | None':
	"""
	The path template that *expression* reads from the evaluated object, or ``None`` if *expression* is not
	an access chain rooted at a document symbol or a tracked comprehension variable.
	"""
	if isinstance(expression, ast.SymbolExpression):
		if expression.scope is not None:
			return None
		if expression.name in variables:
			return variables[expression.name]
		return (expression.name,)
	if isinstance(expression, ast.GetAttributeExpression):
		base = _chain(expression.object, variables)
		return None if base is None else base + (expression.name,)
	if isinstance(expression, ast.GetItemExpression):
		base = _chain(expression.container, variables)
		if base is None:
			return None
		key = _literal_key(expression.item)
		if key is not errors.UNDEFINED:
			return base + (key,)
		return base + (DynamicKey(expression.item, tuple(variables.items())),)
	return None

def _dynamic_key_expressions(expression: ast.ExpressionBase) -> Iterator[ast.ExpressionBase]:
	"""The non-literal item expressions along the access chain spine of *expression*."""
	while isinstance(expression, (ast.GetAttributeExpression, ast.GetItemExpression)):
		if isinstance(expression, ast.GetItemExpression):
			if _literal_key(expression.item) is errors.UNDEFINED:
				yield expression.item
			expression = expression.container
		else:
			expression = expression.object

class _Analyzer(object):
	def __init__(self, rule: 'Rule') -> None:
		self.rule = rule
		self.sites: list[ReadSite] = []

	def analyze(self) -> tuple[ReadSite, ...]:
		self._walk(self.rule.statement.expression, variables=(), guards=(), selector=False)
		return tuple(dict.fromkeys(self.sites))

	def _origin(self, template: tuple) -> ReadOrigin:
		return ReadOrigin(self.rule.text, format_path(template))

	def _record(self, template: tuple, guards: tuple[_Guard, ...], selector: bool, subtree: bool = True) -> None:
		stage = CONTROL if selector or not guards else DETAIL
		if subtree:
			template = template + (SUBTREE,)
		if stage == CONTROL:
			# a control read with a dynamic key can not be fetched key-by-key in stage one; fetch the whole
			# (small) container the key is drawn from instead, e.g. the batch-level mapping
			for index, segment in enumerate(template):
				if isinstance(segment, DynamicKey):
					template = template[:index] + (SUBTREE,)
					break
		self.sites.append(ReadSite(template, stage, guards, self._origin(template)))

	def _walk(self, expr: ast.ExpressionBase, variables: tuple, guards: tuple[_Guard, ...], selector: bool) -> None:
		if isinstance(expr, ast.ComprehensionExpression):
			self._walk_comprehension(expr, variables, guards)
			return
		if isinstance(expr, ast.TernaryExpression):
			self._walk(expr.condition, variables, guards, selector=True)
			self._walk(expr.case_true, variables, guards + (_Guard(expr.condition, True, variables),), selector=False)
			self._walk(expr.case_false, variables, guards + (_Guard(expr.condition, False, variables),), selector=False)
			return
		if isinstance(expr, ast.LogicExpression):
			self._walk(expr.left, variables, guards, selector)
			guard = _Guard(expr.left, expr.type == 'and', variables)
			self._walk(expr.right, variables, guards + (guard,), selector)
			return
		env = dict(variables)
		template = _chain(expr, env)
		if template is not None:
			self._record(template, guards, selector=selector)
			for key_expression in _dynamic_key_expressions(expr):
				self._walk(key_expression, variables, guards, selector=True)
			return
		for child in _iter_child_expressions(expr):
			self._walk(child, variables, guards, selector)

	def _walk_comprehension(self, expr: ast.ComprehensionExpression, variables: tuple, guards: tuple[_Guard, ...]) -> None:
		iterable_template = _chain(expr.iterable, dict(variables))
		if iterable_template is not None:
			# the iterable is read for its structure; its elements are read through the comprehension variable
			self._record(iterable_template, guards, selector=True, subtree=False)
			for key_expression in _dynamic_key_expressions(expr.iterable):
				self._walk(key_expression, variables, guards, selector=True)
			element_template: 'tuple | None' = iterable_template + (_Wildcard(),)
		else:
			self._walk(expr.iterable, variables, guards, selector=True)
			element_template = None
		# the variable is a lexical binding and shadows any root symbol of the same name
		inner_variables = variables + ((expr.variable, element_template),)
		result_guards = guards
		if expr.condition is not None:
			self._walk(expr.condition, inner_variables, guards, selector=True)
			result_guards = guards + (_Guard(expr.condition, True, inner_variables),)
		self._walk(expr.result, inner_variables, result_guards, selector=False)

################################################################################
# binding a plan to control data
################################################################################
def _descend(value: Any, segment: Any) -> Any:
	if isinstance(value, collections.abc.Mapping):
		return value[segment]
	if isinstance(value, collections.abc.Sequence) and not isinstance(value, (str, bytes, bytearray)):
		return value[segment]
	raise TypeError('can not descend into {0!r}'.format(type(value).__name__))

def _descend_lenient(value: Any, segment: Any) -> Any:
	try:
		return _descend(value, segment)
	except (KeyError, IndexError, TypeError):
		return None

def _get_path(thing: Any, path: tuple) -> Any:
	value = thing
	for segment in path:
		if segment is SUBTREE:
			break
		value = _descend(value, segment)
	return value

def _expand(template: tuple, thing: Any) -> Iterator[tuple[tuple, dict[int, Any]]]:
	"""
	Expand the wildcards of a dynamic-key-free *template* against *thing*, yielding each concrete path with
	the binding of wildcard identities to indices (or mapping keys) that produced it.
	"""
	states: list[tuple[tuple, Any, dict[int, Any]]] = [((), thing, {})]
	for segment in template:
		if segment is SUBTREE:
			states = [(path + (SUBTREE,), None, binding) for path, _value, binding in states]
			continue
		next_states = []
		for path, value, binding in states:
			if isinstance(segment, _Wildcard):
				if isinstance(value, collections.abc.Mapping):
					for key, item in value.items():
						next_states.append((path + (key,), item, {**binding, segment.identity: key}))
				elif isinstance(value, collections.abc.Sequence) and not isinstance(value, (str, bytes, bytearray)):
					for index, item in enumerate(value):
						next_states.append((path + (index,), item, {**binding, segment.identity: index}))
			else:
				next_states.append((path + (segment,), _descend_lenient(value, segment), binding))
		states = next_states
	for path, _value, binding in states:
		yield path, binding

def _split_dynamic(template: tuple) -> tuple[tuple, tuple]:
	"""Split *template* at its first :py:class:`DynamicKey` segment."""
	for index, segment in enumerate(template):
		if isinstance(segment, DynamicKey):
			return template[:index], template[index:]
	return template, ()

def _resolve_variable(template: tuple, binding: dict[int, Any], thing: Any, context: Any) -> Any:
	"""Resolve a comprehension variable's path *template* to its value in *thing* under *binding*."""
	value = thing
	for segment in template:
		if segment is SUBTREE:
			break
		if isinstance(segment, _Wildcard):
			segment = binding[segment.identity]
		elif isinstance(segment, DynamicKey):
			segment = _evaluate_key(segment, binding, thing, context)
		value = _descend(value, segment)
	return value

def _assignments(context: Any, variables: tuple[tuple[str, tuple], ...], binding: dict[int, Any], thing: Any) -> 'list[ast.Assignment]':
	return [
		ast.Assignment(name, value=_resolve_variable(template, binding, thing, context))
		for name, template in variables
		if template is not None
	]

def _guards_pass(guards: tuple[_Guard, ...], binding: dict[int, Any], thing: Any, context: Any) -> bool:
	for guard in guards:
		try:
			with context.assignments(*_assignments(context, guard.variables, binding, thing)):
				result = guard.expression.evaluate(thing)
		except (errors.EngineError, KeyError, IndexError, TypeError, AttributeError):
			# the guard can not be decided from the control projection; keep the read rather than drop it
			continue
		if bool(result) != guard.expected:
			return False
	return True

def _evaluate_key(dynamic_key: DynamicKey, binding: dict[int, Any], thing: Any, context: Any) -> Any:
	with context.assignments(*_assignments(context, dynamic_key.variables, binding, thing)):
		return dynamic_key.expression.evaluate(thing)

def _key_candidates(dynamic_key: DynamicKey, binding: dict[int, Any], thing: Any, context: Any) -> 'list | None':
	"""
	The candidate keys for an unresolvable *dynamic_key*: the values of the mapping the key expression draws
	from, e.g. the candidate columns of ``mapping[...]``. ``None`` when no such mapping can be identified.
	"""
	expression = dynamic_key.expression
	if not isinstance(expression, ast.GetItemExpression):
		return None
	container_template = _chain(expression.container, dict(dynamic_key.variables))
	if container_template is None:
		return None
	try:
		container = _resolve_variable(container_template, binding, thing, context)
	except (errors.EngineError, KeyError, IndexError, TypeError, AttributeError):
		return None
	if not isinstance(container, collections.abc.Mapping):
		return None
	return [value for value in container.values() if isinstance(value, (str, int)) and not isinstance(value, bool)]

def _resolve_key(dynamic_key: DynamicKey, binding: dict[int, Any], thing: Any, context: Any) -> 'list[Any] | None':
	"""
	The concrete path segments a dynamic key resolves to. A key that can not be resolved (for example an
	optional mapping entry that is absent) yields the candidate keys of the mapping it is drawn from, or
	``None`` to fall back to the whole subtree at that point of the path.
	"""
	try:
		key = _evaluate_key(dynamic_key, binding, thing, context)
	except (errors.EngineError, KeyError, IndexError, TypeError, AttributeError):
		key = None
	if isinstance(key, str):
		return [key]
	if isinstance(key, (int, float)) and not isinstance(key, bool):
		return [int(key) if float(key).is_integer() else key]
	candidates = _key_candidates(dynamic_key, binding, thing, context)
	if candidates is not None:
		return candidates
	return None

def _resolve_tail(prefix: tuple, tail: tuple, binding: dict[int, Any], thing: Any, context: Any) -> Iterator[tuple]:
	"""Resolve the part of a detail template at and after its first dynamic key into concrete paths."""
	paths = [prefix]
	for segment in tail:
		if segment is SUBTREE:
			paths = [path + (SUBTREE,) for path in paths]
		elif isinstance(segment, _Wildcard):
			expanded: list[tuple] = []
			for path in paths:
				try:
					value = _get_path(thing, path)
				except (KeyError, IndexError, TypeError):
					continue
				if isinstance(value, collections.abc.Sequence) and not isinstance(value, (str, bytes, bytearray)):
					expanded.extend(path + (index,) for index in range(len(value)))
				elif isinstance(value, collections.abc.Mapping):
					expanded.extend(path + (key,) for key in value)
			paths = expanded
		elif isinstance(segment, DynamicKey):
			keys_per_path = []
			for path in paths:
				keys = _resolve_key(segment, binding, thing, context)
				if keys is None:
					# the key is unknown and no candidates can be determined; keep the whole subtree at
					# this point of the path (never the whole root document)
					keys_per_path.append([path + (SUBTREE,)])
				else:
					keys_per_path.append([path + (key,) for key in keys])
			paths = [path for group in keys_per_path for path in group]
		else:
			paths = [path + (segment,) for path in paths]
	yield from paths

################################################################################
# public plan API
################################################################################
class BoundProjection(object):
	"""
	The result of binding a projection plan to control data: the concrete field paths to fetch, with the
	rule references that retain each of them.
	"""
	def __init__(self, control: dict[tuple, list[ReadOrigin]], detail: dict[tuple, list[ReadOrigin]]) -> None:
		self._control = control
		self._detail = detail

	def __repr__(self) -> str:
		return "<{0} control={1} detail={2} >".format(self.__class__.__name__, len(self._control), len(self._detail))

	@property
	def control_paths(self) -> frozenset[tuple]:
		"""The concrete control (selection field) paths that were read in stage one."""
		return frozenset(self._control)

	@property
	def detail_paths(self) -> frozenset[tuple]:
		"""The concrete detail paths to fetch in stage two, following the branches that were actually taken."""
		return frozenset(self._detail)

	@property
	def paths(self) -> frozenset[tuple]:
		"""All retained paths, suitable for :py:func:`project`."""
		return frozenset(self._control) | frozenset(self._detail)

	@property
	def references(self) -> dict[tuple, tuple[ReadOrigin, ...]]:
		"""A mapping of each retained path to the :py:class:`ReadOrigin` references of the rules that retain it."""
		references: dict[tuple, list[ReadOrigin]] = {}
		for path, origins in itertools.chain(self._control.items(), self._detail.items()):
			entries = references.setdefault(path, [])
			for origin in origins:
				if origin not in entries:
					entries.append(origin)
		return {path: tuple(origins) for path, origins in references.items()}

	def project(self, thing: Any) -> Any:
		"""Restrict *thing* to the retained paths; see :py:func:`project`."""
		return project(thing, self.paths)

class ProjectionPlan(object):
	"""
	An executable staged projection for a single rule. :py:attr:`control_paths` lists the selection fields
	to fetch in stage one; :py:meth:`bind` evaluates the plan's guards against that control projection and
	produces the concrete detail paths for stage two.
	"""
	def __init__(self, rule: 'Rule', read_sites: Iterable[ReadSite]) -> None:
		self.rule = rule
		"""The rule this plan was derived from."""
		self.read_sites = tuple(read_sites)
		"""The :py:class:`ReadSite` reads the rule performs."""

	def __repr__(self) -> str:
		return "<{0} rule={1!r} >".format(self.__class__.__name__, self.rule.text)

	@classmethod
	def from_rule(cls, rule: 'Rule') -> 'ProjectionPlan':
		"""Analyze *rule* and return its staged projection plan."""
		return cls(rule, _Analyzer(rule).analyze())

	def _templates(self, stage: str) -> tuple[tuple, ...]:
		templates = {site.template for site in self.read_sites if site.stage == stage}
		return tuple(sorted(templates, key=format_path))

	@property
	def control_paths(self) -> tuple[tuple, ...]:
		"""
		The stage-one fetch specification: the selection fields (shape flags, row activity, categories and
		column mappings) as path templates which may contain wildcards.
		"""
		return self._templates(CONTROL)

	@property
	def detail_paths(self) -> tuple[tuple, ...]:
		"""The guarded reads that :py:meth:`bind` resolves into concrete stage-two paths."""
		return self._templates(DETAIL)

	def bind(self, control_thing: Any) -> BoundProjection:
		"""
		Bind the plan to the stage-one control projection *control_thing*. Guards are evaluated against it
		to determine which branches are actually taken and to resolve dynamic keys, yielding the concrete
		detail paths to fetch in stage two.

		:param control_thing: The control data, typically ``project(document, plan.control_paths)``.
		:return: The :py:class:`BoundProjection` of concrete paths.
		"""
		context = self.rule.context
		context._tls.reset()
		control: dict[tuple, list[ReadOrigin]] = {}
		detail: dict[tuple, list[ReadOrigin]] = {}
		with decimal.localcontext(context.decimal_context):
			for site in self.read_sites:
				if site.stage == CONTROL:
					for path, _binding in _expand(site.template, control_thing):
						origins = control.setdefault(path, [])
						if site.origin not in origins:
							origins.append(site.origin)
				else:
					prefix, tail = _split_dynamic(site.template)
					for path, binding in _expand(prefix, control_thing):
						if not _guards_pass(site.guards, binding, control_thing, context):
							continue
						for resolved in _resolve_tail(path, tail, binding, control_thing, context):
							origins = detail.setdefault(resolved, [])
							if site.origin not in origins:
								origins.append(site.origin)
		return BoundProjection(control, detail)

class ProjectionSet(object):
	"""
	A combined staged projection for multiple rules, for example the new and legacy rules compiled against
	the same :py:class:`~rule_engine.engine.Context`. Each rule keeps its own :py:class:`ProjectionPlan` (its
	own dependency scope); the set merges their fetch specifications while the bound projection still traces
	every retained field back to the rule references that require it.
	"""
	def __init__(self, plans: Iterable[ProjectionPlan]) -> None:
		self.plans = tuple(plans)
		"""The member :py:class:`ProjectionPlan` objects, one per rule."""

	def __repr__(self) -> str:
		return "<{0} plans={1} >".format(self.__class__.__name__, len(self.plans))

	@classmethod
	def from_rules(cls, rules: Iterable['Rule']) -> 'ProjectionSet':
		"""Build a projection set with one :py:class:`ProjectionPlan` per rule."""
		return cls(ProjectionPlan.from_rule(rule) for rule in rules)

	@property
	def control_paths(self) -> tuple[tuple, ...]:
		"""The union of the member plans' stage-one fetch specifications."""
		templates = {template for plan in self.plans for template in plan.control_paths}
		return tuple(sorted(templates, key=format_path))

	def bind(self, control_thing: Any) -> BoundProjection:
		"""Bind every member plan to the stage-one control projection and merge the results."""
		control: dict[tuple, list[ReadOrigin]] = {}
		detail: dict[tuple, list[ReadOrigin]] = {}
		for plan in self.plans:
			bound = plan.bind(control_thing)
			for source, target in ((bound._control, control), (bound._detail, detail)):
				for path, origins in source.items():
					entries = target.setdefault(path, [])
					for origin in origins:
						if origin not in entries:
							entries.append(origin)
		return BoundProjection(control, detail)
