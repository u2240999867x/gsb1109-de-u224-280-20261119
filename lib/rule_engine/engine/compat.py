#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/engine/compat.py
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

import collections.abc
import contextlib
import threading
from typing import Any, Callable, Mapping, Sequence

from .. import errors

from .context import Context

__all__ = (
        'DeferredDataError',
        'DeferredValue',
        'LegacyDataProvider',
        'LegacyGradeContext',
        'PendingEvaluation',
        'ResumableMapping',
)

class DeferredDataError(Exception):
    """
    An exception that a :py:class:`LegacyDataProvider` raises to report that the legacy value of a field has not
    arrived yet. It does not reject the object being evaluated: a :py:class:`LegacyGradeContext` chain evaluation
    suspends instead, so the same evaluation can be resumed once the data arrives.
    """

class _DeferredValueType(object):
    """The type of the :py:data:`DeferredValue` sentinel."""
    __slots__ = ()
    def __repr__(self) -> str:
        return 'DeferredValue'

DeferredValue = _DeferredValueType()
"""
A sentinel a :py:class:`LegacyDataProvider` can return (instead of raising :py:class:`DeferredDataError`) when the
legacy value has not arrived yet. Either form suspends the evaluation; every other return value, including ``None``,
is used as-is.
"""

class _PendingSignal(BaseException):
    """
    An internal control-flow exception that carries a suspension request out of a rule evaluation. It inherits from
    :py:exc:`BaseException` so ordinary ``except Exception`` handlers (such as the one around function calls) do not
    mistake it for an evaluation failure.
    """
    def __init__(self, container: 'ResumableMapping', key: Any) -> None:
        self.container = container
        self.key = key

class _RootState(object):
    """State shared by every wrapper of one resumable evaluation: the provider config, caches and access logs."""
    __slots__ = ('provider', 'current_group', 'legacy_group', 'grade_key', 'root', 'access_log', 'provider_calls', 'bulk_depth', 'lock')

    def __init__(
            self,
            provider: 'LegacyDataProvider',
            current_group: str,
            legacy_group: str,
            grade_key: str
    ) -> None:
        self.provider = provider
        self.current_group = current_group
        self.legacy_group = legacy_group
        self.grade_key = grade_key
        # the top-level wrapper, set by it once it is constructed; members reached through sequences keep no parent
        self.root: 'ResumableMapping | None' = None
        # every read that reached the underlying data, as a path of keys from the root
        self.access_log: list[tuple[Any, ...]] = []
        # every provider lookup, recorded as (legacy group, key)
        self.provider_calls: list[tuple[str, Any]] = []
        # >0 while the type system is bulk-iterating a mapping (e.g. DataType.from_value); reads then still cache but
        # are not semantic field accesses, so they are not recorded in the access log
        self.bulk_depth = 0
        self.lock = threading.Lock()

    @contextlib.contextmanager
    def bulk_inspect(self):
        with self.lock:
            self.bulk_depth += 1
        try:
            yield
        finally:
            with self.lock:
                self.bulk_depth -= 1

class ResumableMapping(collections.abc.Mapping):
    """
    A read-only :py:class:`~collections.abc.Mapping` wrapper that makes one chain evaluation resumable.

    Every value that is actually read is cached, including legacy values filled in while resuming, so resuming the
    evaluation never re-reads a field that was already read. Nested mappings and sequences are wrapped lazily and
    share the same state. A missing :py:attr:`LegacyGradeContext.grade_key` on a current-review group falls back to
    the legacy provider; when the provider has no answer yet the wrapper raises the internal suspension signal
    instead of inventing a value. A key that is present is returned untouched, so an explicit ``None`` keeps its null
    semantics and a value of the wrong type keeps raising its normal type error.
    """
    __slots__ = ('_data', '_state', '_parent', '_parent_key', '_path', '_cache', '_lock')

    def __init__(
            self,
            data: Mapping[str, Any],
            state: _RootState,
            parent: 'ResumableMapping | None' = None,
            parent_key: Any = None,
            path: tuple[Any, ...] = ()
    ) -> None:
        self._data = data
        self._state = state
        self._parent = parent
        self._parent_key = parent_key
        self._path = path
        self._cache: dict[Any, Any] = {}
        # reentrant because __getitem__ may enter legacy_lookup, which locks the same instance again
        self._lock = threading.RLock()

    def _wrap(self, value: Any, key: Any) -> Any:
        if isinstance(value, collections.abc.Mapping):
            return ResumableMapping(value, self._state, parent=self, parent_key=key, path=self._path + (key,))
        if isinstance(value, collections.abc.Sequence) and not isinstance(value, (str, bytes, bytearray)):
            return _ResumableSequence(value, self._state, path=self._path + (key,))
        return value

    def __getitem__(self, key: Any) -> Any:
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            try:
                raw_value = self._data[key]
            except KeyError:
                # bracket-style access (mapping["grade"]) reaches the same fallback as symbol resolution
                value = self.legacy_lookup(key)
            else:
                value = self._wrap(raw_value, key)
                if self._state.bulk_depth == 0:
                    with self._state.lock:
                        self._state.access_log.append(self._path + (key,))
            self._cache[key] = value
            return value

    def __iter__(self):
        # iterating keys never reads values, so nothing is logged or wrapped
        return iter(self._data)

    def keys(self):
        return self._data.keys()

    def values(self):
        # the Mapping ABC default would call self[key] for every key; build the wrapped members directly so type
        # inference over .values() does not record each member as a semantic field access
        with self._state.bulk_inspect():
            return [self._wrap(value, key) for key, value in self._data.items()]

    def items(self):
        with self._state.bulk_inspect():
            return [(key, self._wrap(value, key)) for key, value in self._data.items()]

    def __len__(self) -> int:
        return len(self._data)

    def __contains__(self, key: Any) -> bool:
        # containment reflects the underlying data only; a missing grade is still reported as absent by ``in`` even
        # though a legacy fallback for it exists
        return key in self._cache or key in self._data

    def is_legacy_eligible(self, key: Any) -> bool:
        """Whether a missing *key* on this group may fall back to its legacy value."""
        # only a missing grade on a current-review group that has a legacy sibling falls back
        if key != self._state.grade_key or self._parent is None or self._parent_key != self._state.current_group:
            return False
        sibling = self._parent._data
        return isinstance(sibling, collections.abc.Mapping) and self._state.legacy_group in sibling

    def legacy_lookup(self, key: Any) -> Any:
        """Read the legacy value for a missing *key*, suspending when the provider has no answer yet."""
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        if not self.is_legacy_eligible(key):
            raise KeyError(key)
        with self._state.lock:
            self._state.provider_calls.append((self._state.legacy_group, key))
        try:
            value = self._state.provider(self._root(), self._state.legacy_group, key)
        except DeferredDataError:
            raise _PendingSignal(self, key) from None
        if value is DeferredValue or isinstance(value, _DeferredValueType):
            raise _PendingSignal(self, key)
        value = self._wrap(value, key)
        with self._lock:
            self._cache[key] = value
        return value

    def _root(self) -> 'ResumableMapping':
        # members reached through a sequence wrapper have no parent, so the root is whatever the shared state recorded
        root = self._state.root
        if root is not None:
            return root
        node = self
        while node._parent is not None:
            node = node._parent
        return node

    def _fill(self, key: Any, value: Any) -> None:
        """Supply the legacy *value* for a missing *key* before the evaluation resumes."""
        with self._lock:
            self._cache[key] = self._wrap(value, key)

    @property
    def access_log(self) -> list[tuple[Any, ...]]:
        """The field paths read from the underlying data so far (provider-filled reads are not in it)."""
        return self._state.access_log

    @property
    def provider_calls(self) -> list[tuple[str, Any]]:
        """The legacy provider lookups made so far, as ``(legacy group, key)`` pairs."""
        return self._state.provider_calls

class _ResumableSequence(collections.abc.Sequence):
    """A sequence wrapper that lazily wraps mapping members with :py:class:`ResumableMapping`."""
    __slots__ = ('_data', '_state', '_path', '_cache', '_lock')

    def __init__(self, data: Sequence[Any], state: _RootState, path: tuple[Any, ...]) -> None:
        self._data = data
        self._state = state
        self._path = path
        self._cache: dict[int, Any] = {}
        self._lock = threading.Lock()

    def __getitem__(self, index):
        if isinstance(index, slice):
            return _ResumableSequence(self._data[index], self._state, path=self._path)
        if not isinstance(index, int):
            # the engine represents integers as Decimal and only converts them itself for tuple/str/bytes containers
            index = int(index)
        with self._lock:
            if index in self._cache:
                return self._cache[index]
            value = self._data[index]
            if isinstance(value, collections.abc.Mapping):
                value = ResumableMapping(value, self._state, path=self._path + (index,))
            elif isinstance(value, collections.abc.Sequence) and not isinstance(value, (str, bytes, bytearray)):
                value = _ResumableSequence(value, self._state, path=self._path + (index,))
            self._cache[index] = value
            return value

    def __len__(self) -> int:
        return len(self._data)

    def __iter__(self):
        # type inference iterates the whole sequence (DataType.from_value); those reads are not semantic accesses
        with self._state.bulk_inspect():
            for index in range(len(self._data)):
                yield self[index]

LegacyDataProvider = Callable[[Any, str, str], Any]
"""
The signature of a legacy data provider: it receives the root object, the legacy group name and the missing field
name, and returns the legacy value. Raising :py:class:`DeferredDataError` or returning :py:data:`DeferredValue`
suspends the evaluation.
"""

class LegacyGradeContext(Context):
    """
    A :py:class:`.Context` that opts a :py:class:`.Chain` evaluation into resumable legacy-grade compatibility.

    A chain evaluated with this context wraps its object in a :py:class:`ResumableMapping`. When the ``grade`` field
    of a current-review sub-group is genuinely missing, the same-named field of the legacy sub-group is read through
    *legacy_provider*. If that data is late, :py:meth:`~.Chain.evaluate` returns a :py:class:`PendingEvaluation`
    instead of rejecting the object, falling through to the next rule or blocking a thread; the evaluation is later
    resumed from the exact suspension point with the provider's answer.

    Fields that are present keep their original behavior: explicit ``None``, wrong types and grades that exist in
    the signed-for group never reach the provider. Chains evaluated with an ordinary :py:class:`.Context` keep using
    the direct, non-suspending call path.
    """

    def __init__(
            self,
            legacy_provider: LegacyDataProvider,
            *,
            current_group: str = 'current',
            legacy_group: str = 'legacy',
            grade_key: str = 'grade',
            **kwargs: Any
    ) -> None:
        """
        :param legacy_provider: Callable taking the root object, the legacy group name and the missing field name.
                Raising :py:class:`DeferredDataError` or returning :py:data:`DeferredValue` suspends the evaluation.
        :param current_group: Name of the current-review sub-group, whose missing grades may fall back.
        :param legacy_group: Name of the legacy sub-group consulted by *legacy_provider*.
        :param grade_key: The only field name that may trigger a legacy lookup.
        """
        super().__init__(**kwargs)
        self.legacy_provider = legacy_provider
        self.current_group = current_group
        self.legacy_group = legacy_group
        self.grade_key = grade_key

    def __getstate__(self) -> dict[str, Any]:
        state = super().__getstate__()
        state.update({
                'legacy_provider': self.legacy_provider,
                'current_group': self.current_group,
                'legacy_group': self.legacy_group,
                'grade_key': self.grade_key,
        })
        return state

    def __setstate__(self, state: dict[str, Any]) -> None:
        provider = state.pop('legacy_provider')
        current_group = state.pop('current_group')
        legacy_group = state.pop('legacy_group')
        grade_key = state.pop('grade_key')
        super().__setstate__(state)
        self.legacy_provider = provider
        self.current_group = current_group
        self.legacy_group = legacy_group
        self.grade_key = grade_key

    def _new_root_state(self) -> _RootState:
        return _RootState(self.legacy_provider, self.current_group, self.legacy_group, self.grade_key)

    def wrap(self, thing: Any) -> ResumableMapping:
        """Wrap *thing* in a fresh :py:class:`ResumableMapping` with an empty cache and access log."""
        if not isinstance(thing, collections.abc.Mapping):
            raise TypeError('the resumable legacy compatibility path requires a mapping object, not ' + type(thing).__name__)
        root = ResumableMapping(thing, self._new_root_state())
        root._state.root = root
        return root

    def resolve(self, thing: Any, name: str, scope: str | None = None) -> Any:
        # a genuinely missing grade on a current-review group is answered by the legacy provider; everything else,
        # including assignment scopes, present keys and unrelated missing symbols, keeps the normal resolution path
        if scope is None and isinstance(thing, ResumableMapping) and not self._is_assigned(name):
            if name in thing:
                return thing[name]
            try:
                return thing.legacy_lookup(name)
            except KeyError:
                pass
        return super().resolve(thing, name, scope=scope)

class PendingEvaluation(object):
    """
    The resumable handle returned instead of a chain result while an evaluation is waiting for a legacy grade.

    The handle keeps the chain entry snapshot, the wrapped object and the exact suspension point, so publishing new
    rules or evaluating other objects in the meantime does not affect it. Each handle continues its evaluation at
    most once: a second :py:meth:`resume` call raises, and delivering the same legacy field again can not continue
    the evaluation a second time.
    """
    __slots__ = ('_context', '_entries', '_entry_index', '_default_value', '_thing', '_container', '_key', '_done', '_lock')

    def __init__(
            self,
            context: LegacyGradeContext,
            entries: Sequence[Any],
            entry_index: int,
            default_value: Any,
            thing: ResumableMapping,
            container: ResumableMapping,
            key: Any
    ) -> None:
        self._context = context
        self._entries = entries
        self._entry_index = entry_index
        self._default_value = default_value
        self._thing = thing
        self._container = container
        self._key = key
        self._done = False
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return "<{0} key={1!r} done={2!r} >".format(self.__class__.__name__, self._key, self._done)

    @property
    def key(self) -> Any:
        """The field whose legacy value is being waited on."""
        return self._key

    @property
    def is_done(self) -> bool:
        """Whether :py:meth:`resume` has already been called."""
        return self._done

    @property
    def thing(self) -> ResumableMapping:
        """The wrapped object the suspended evaluation is scoped to."""
        return self._thing

    def resume(self, legacy_value: Any = DeferredValue) -> Any:
        """
        Continue the suspended evaluation.

        :param legacy_value: The legacy grade to fill in, or :py:data:`DeferredValue` to ask the provider again
                (e.g. after it has been fed the late data). An explicit ``None`` is a real answer, not another wait.
        :return: The value of the first matching rule, the chain's default value, or another
                :py:class:`PendingEvaluation` if a later field is waiting on legacy data too.
        """
        with self._lock:
            if self._done:
                raise errors.EvaluationError('the pending evaluation has already been resumed')
            self._done = True
            if legacy_value is not DeferredValue and not isinstance(legacy_value, _DeferredValueType):
                self._container._fill(self._key, legacy_value)
        return run_resumable(
                self._context,
                self._entries,
                self._default_value,
                self._thing,
                start_index=self._entry_index
        )

def run_resumable(
        context: LegacyGradeContext,
        entries: Sequence[Any],
        default_value: Any,
        thing: ResumableMapping,
        start_index: int = 0
) -> Any:
    """
    Evaluate *entries* against *thing*, suspending at the first legacy field whose data is late. Entries before
    *start_index* were already evaluated in an earlier pass and are not visited again.
    """
    try:
        for index in range(start_index, len(entries)):
            if entries[index].rule.matches(thing):
                return entries[index].value
    except _PendingSignal as signal:
        return PendingEvaluation(
                context,
                entries,
                index,
                default_value,
                thing,
                signal.container,
                signal.key
        )
    if default_value is errors.UNDEFINED:
        raise errors.ChainExhaustedError(thing=thing)
    return default_value
