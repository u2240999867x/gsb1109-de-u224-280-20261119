#!/usr/bin/env python3
# -*- coding: utf-8 -*-
#
#  rule_engine/diagnostics.py
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

from __future__ import annotations

import contextlib
from typing import TYPE_CHECKING, Any, Iterator

from . import errors

if TYPE_CHECKING:
    import types as pytypes

    from .ast.base import ExpressionBase

class DiagnosticPathEntry(object):
    """
    One step in the path from the object a rule was evaluated on to the original record a :py:class:`.Diagnostic`
    belongs to. Each entry corresponds to one iteration of a comprehension and identifies the record by its position
    in the array that was iterated, not by its position in the array the comprehension produced.

    .. versionadded:: 5.3.0
    """
    __slots__ = ('expression', 'variable', 'index')
    def __init__(self, *, expression: str | None, variable: str, index: int) -> None:
        """
        :param expression: The source text of the expression whose value was iterated, or ``None`` when the rule was
                not parsed from source text.
        :param str variable: The name of the comprehension variable the record was assigned to.
        :param int index: The position of the record in the original array that was iterated. When the iterated value
                was itself produced by a slice, this is the position in the array the slice was taken from, not in the
                slice.
        """
        self.expression = expression
        """The source text of the expression whose value was iterated, or ``None`` when it is not known."""
        self.variable = variable
        """The name of the comprehension variable the record was assigned to."""
        self.index = index
        """The position of the record in the original array that was iterated."""

    def __repr__(self) -> str:
        return "<{0} expression={1!r} variable={2!r} index={3!r} >".format(
                self.__class__.__name__, self.expression, self.variable, self.index
        )

    def __eq__(self, other: Any) -> bool:
        if not isinstance(other, DiagnosticPathEntry):
            return NotImplemented
        return (self.expression, self.variable, self.index) == (other.expression, other.variable, other.index)

class Diagnostic(object):
    """
    A single evaluation failure collected by :py:meth:`~rule_engine.engine.Rule.evaluate_diagnostics`. It combines
    the original exception with the location of the rule text that failed and the path to the original record that
    was being evaluated.

    .. versionadded:: 5.3.0
    """
    __slots__ = ('error', 'span', 'path', '_text')
    def __init__(
            self,
            *,
            error: errors.EvaluationError,
            span: tuple[int, int] | None,
            path: tuple[DiagnosticPathEntry, ...],
            text: str
    ) -> None:
        """
        :param error: The original exception that evaluation raised.
        :type error: :py:exc:`~rule_engine.errors.EvaluationError`
        :param span: The ``(start, end)`` character offsets into the rule text of the expression that actually
                failed, suitable for selecting the corresponding characters in an editor. ``None`` when the rule was
                not parsed from source text.
        :param path: The path of comprehension iterations that leads to the original record which was being
                evaluated when the error occurred.
        :param str text: The rule text the *span* offsets refer to.
        """
        self.error = error
        """The original exception that evaluation raised."""
        self.span = span
        """The ``(start, end)`` character offsets into the rule text of the expression that actually failed."""
        self.path = path
        """The path to the original record which was being evaluated when the error occurred."""
        self._text = text

    def __repr__(self) -> str:
        return "<{0} error={1!r} span={2!r} path={3!r} >".format(self.__class__.__name__, self.error, self.span, self.path)

    @property
    def segment(self) -> str | None:
        """The segment of the rule text that failed, or ``None`` when :py:attr:`.span` is not set."""
        if self.span is None:
            return None
        return self._text[self.span[0]:self.span[1]]

class DiagnosticsReport(object):
    """
    The result of :py:meth:`~rule_engine.engine.Rule.evaluate_diagnostics`: the value the rule evaluated to together
    with every independent error that was collected along the way.

    .. versionadded:: 5.3.0
    """
    __slots__ = ('value', 'errors', 'text')
    def __init__(self, *, value: Any, errors: tuple[Diagnostic, ...], text: str) -> None:
        """
        :param value: The value the rule evaluated to. This is :py:data:`~rule_engine.errors.UNDEFINED` when no value
                could be produced, and may be a partial result when some errors were collected.
        :param errors: The errors that were collected during evaluation, in the order they occurred.
        :param str text: The rule text the diagnostics refer to.
        """
        self.value = value
        """The value the rule evaluated to, possibly partial or :py:data:`~rule_engine.errors.UNDEFINED`."""
        self.errors = errors
        """A tuple of the :py:class:`.Diagnostic` objects that were collected, in the order they occurred."""
        self.text = text
        """The rule text the diagnostics refer to."""

    def __repr__(self) -> str:
        return "<{0} value={1!r} errors={2!r} >".format(self.__class__.__name__, self.value, self.errors)

    def __bool__(self) -> bool:
        return self.ok

    @property
    def ok(self) -> bool:
        """Whether or not evaluation completed without collecting any errors."""
        return not self.errors

class _OriginSequence(tuple):
    """
    A tuple of values that were sliced out of a larger sequence, remembering the position each member had in the
    sequence the slice was taken from so diagnostics can point back to the original records.
    """
    origin_indices: tuple[int, ...]
    def __new__(cls, value: tuple[Any, ...], origin_indices: tuple[int, ...]) -> '_OriginSequence':
        self = super(_OriginSequence, cls).__new__(cls, value)
        self.origin_indices = origin_indices
        return self

def _innermost_expression_node(tb: 'pytypes.TracebackType | None') -> 'ExpressionBase | None':
    """
    Find the innermost AST node involved in *tb*, i.e. the node whose evaluation actually raised. Nodes created by
    the diagnostics machinery itself are indistinguishable from evaluation frames here, so the deepest frame whose
    ``self`` is an expression node wins.
    """
    from .ast.base import ExpressionBase  # avoid a circular import at the module level
    node = None
    while tb is not None:
        candidate = tb.tb_frame.f_locals.get('self')
        if isinstance(candidate, ExpressionBase):
            node = candidate
        tb = tb.tb_next
    return node

class _Collector(object):
    """
    The evaluation-time state for diagnostics mode: the path of comprehension iterations that is currently being
    evaluated and the errors that were collected so far. An instance is single-use and thread-affine, it is created
    by :py:meth:`~rule_engine.engine.Rule.evaluate_diagnostics` and passed to
    :py:meth:`~rule_engine.ast.ExpressionBase.evaluate_diagnostic`.
    """
    def __init__(self, text: str) -> None:
        self.text = text
        self.errors: list[Diagnostic] = []
        self._path: list[DiagnosticPathEntry] = []

    def segment(self, span: tuple[int, int] | None) -> str | None:
        if span is None:
            return None
        return self.text[span[0]:span[1]]

    def record(self, error: errors.EvaluationError, node: 'ExpressionBase | None' = None, tb: 'pytypes.TracebackType | None' = None) -> None:
        """
        Record *error* as a diagnostic. When *tb* is available, the span is taken from the innermost AST node
        involved in it so the diagnostic points at the expression that actually failed rather than at the boundary
        that caught it.
        """
        if tb is not None:
            node = _innermost_expression_node(tb) or node
        span = getattr(node, 'span', None) if node is not None else None
        self.errors.append(Diagnostic(error=error, span=span, path=tuple(self._path), text=self.text))

    @contextlib.contextmanager
    def location(self, *, expression_span: tuple[int, int] | None, variable: str, index: int) -> Iterator[None]:
        """Push a :py:class:`DiagnosticPathEntry` for one comprehension iteration while it is being evaluated."""
        self._path.append(DiagnosticPathEntry(expression=self.segment(expression_span), variable=variable, index=index))
        try:
            yield
        finally:
            self._path.pop()

    def report(self, value: Any) -> DiagnosticsReport:
        return DiagnosticsReport(value=value, errors=tuple(self.errors), text=self.text)
