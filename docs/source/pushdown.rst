Safe SQL Push-Down With an Unknown Audit Function
==================================================

.. contents:: Contents
   :local:

This note explains how to let a relational database (SQLite via SQLAlchemy) discard
orders that a Rule Engine expression can *prove* false, without ever invoking a custom,
pure review function such as ``$audit(reviews)``.  The companion, runnable artifacts are
the :file:`examples/pushdown_audit.py` example and its regression tests in
:file:`tests/pushdown.py`.

Safety contract
---------------

The push-down layer only emits a predicate that identifies rows which are simultaneously:

#. **provably non-truthy** under the engine's own evaluation semantics, and
#. **unable to raise** before the expression settles.

Anything that cannot be settled from SQL-visible columns and relationship cardinality is
*retained*, including every row on which the audit function could return ``true``, return
``false`` or raise (a missing-relationship error or a key-targeted error).  The audit
function is pure and deterministic — the same order and inputs always yield the same
outcome — but that outcome is unknown until the engine runs, and the SQL phase never
imports or calls it.

The residual set is then evaluated **once** by the unchanged Rule Engine rule, so the
final per-order fate (match, no match, or a propagated error) is identical to evaluating
the original rule over the full order set.

Three semantic gaps that make a literal SQL translation unsound
----------------------------------------------------------------

1. Two-valued engine equality versus three-valued SQL.
   ``==`` and ``!=`` in Rule Engine never produce ``NULL``: when the operand types differ
   (including ``NULL`` versus a string), ``==`` is ``False`` and ``!=`` is ``True``.
   SQL ``=`` / ``!=`` evaluate to ``UNKNOWN`` on ``NULL``, and an outer ``NOT`` flips
   ``UNKNOWN`` back to ``UNKNOWN``, silently dropping the row.  Every pushed predicate is
   therefore made explicitly two-valued with ``IS NULL`` / ``IS NOT NULL`` branches.

2. Short-circuit order.
   ``a and b`` and ``a or b`` only evaluate ``b`` once ``a`` is settled.  Swapping the
   operands changes which rows can reach the audit call, so the analysis is order
   sensitive and never rewrites operand order.

3. The opaque call has three outcomes, not two.
   A function call may return ``true``, return ``false`` or raise.  Determinism means two
   calls with the same arguments are *one* fact, not two independent draws.  In
   particular, SQL retaining a row through a truthy branch says nothing about whether the
   same fact can raise through a negated branch.

Per-construct exclusion boundaries
-----------------------------------

For each construct below, the exclusion is the strongest predicate provable from
SQL-visible information.  Each entry ends with a pair of rows that share every
SQL-visible fact yet diverge in engine fate, proving the boundary cannot be tightened
without inspecting the function.

``col == "v"``
   Rows safe to drop: ``col IS NULL OR col != 'v'``.  Cannot raise.  The explicit
   ``IS NULL`` branch is required because plain SQL ``!=`` is ``UNKNOWN`` on ``NULL``,
   whereas the engine's equality is two-valued.  Counterexample: two rows with
   ``col = 'v'`` and the same review cardinality, one with the audit verdict ``true``
   and the other ``false``.

``col != "v"``
   Rows safe to drop: ``col IS NOT NULL AND col = 'v'``.  Cannot raise.
   Counterexample: a ``NULL`` row and an ``'x'`` row are both truthy in the engine and
   therefore both retained.

``col == null``
   Rows safe to drop: ``col IS NOT NULL``.  Cannot raise.  Counterexample: two non-null
   rows whose surrounding rule diverges only through the audit verdict.

``not (col == "v")``
   Rows safe to drop: ``col IS NOT NULL AND col = 'v'``.  Cannot raise.  Counterexample:
   the ``NULL`` row (the negated comparison is truthy) versus the exact-``'v'`` row
   (false), which the ``IS NOT NULL`` guard keeps apart under the outer ``NOT``.

``$audit(reviews)``
   No exclusion (the predicate is ``FALSE``); the subtree can raise.  Counterexample:
   review rows that return ``true`` versus rows that raise a key-targeted error; and the
   empty relationship (missing-field error) versus a non-empty one returning ``false``.

``a and b``
   Exclusion is ``exclude(a) OR exclude(b)`` only when ``a`` cannot raise; otherwise it
   is just ``exclude(a)``, since a surviving row may error inside ``a`` before ``b`` is
   evaluated.  Counterexample: ready rows where the audit verdict is ``true`` versus
   ``false`` share status and relationship cardinality.

``a or b``
   Exclusion is ``exclude(a) AND exclude(b)``; a row is false only when both sides are
   provably false.  An opaque child contributes an empty exclusion, conservatively
   emptying the whole intersection.  Counterexample: the same ready-row pair as above.

``not $audit(...)``
   No exclusion; the subtree can raise.  Counterexample: audit ``false`` (which makes the
   negation truthy) versus audit raising, which must still propagate.

``(col ?? "") =~ "meta-free-literal"``
   Rows safe to drop: ``col IS NULL OR lower(col) NOT LIKE 'v%'`` (``re.match`` anchors
   at the start; a ``re.search`` rule uses ``'%v%'``).  Cannot raise.  Counterexample:
   the ``'READY'`` row retained under the NOCASE regex versus pruned under a
   case-sensitive literal comparison.

Patterns outside this fixed subset (non-literal regexes, regex metacharacters, the SQL
``LIKE`` wildcards ``%`` and ``_``, non-column operands, any other node type) degrade to
the conservative exclusion ``FALSE`` with ``may_error=True`` rather than risking an
unsound translation.

How the judgments compose
-------------------------

The analyzer walks the parsed AST and carries a pair ``(exclude_filter, may_error)`` per
node.  A function call is the base case: it excludes nothing and marks the subtree as
able to raise.

* **and.** If the left side cannot raise, a row dropped by either side's exclusion is
  unreachable as a match, so the filters combine with ``OR``.  If the left side may
  raise, only its own exclusion is usable, because a surviving row can error before the
  right side is evaluated.
* **or.** A row is false only when both sides are provably false, so the filters combine
  with ``AND``.  An opaque child contributes an empty exclusion, which conservatively
  empties the whole intersection.
* **not.** Negation is only pushed through a pure comparison whose true region is known;
  over a subtree that may raise it yields no exclusion, since a raised error is neither
  true nor false and must survive.

The fixed rule catalog makes the order-sensitivity and the single-fact rule concrete.

* ``status == "ready" and $audit(reviews)`` prunes every non-ready row (including the
  explicit-``NULL`` order, because ``NULL == "ready"`` is false) and keeps the ready
  residual, where audit may be true, false or an error.
* ``$audit(reviews) and status == "ready"`` prunes nothing: the leading call is reached
  on every row, and the relationship-less order raises a missing-field error before the
  comparison is ever considered.
* ``not (status == "ready")`` keeps the ``NULL`` row, demonstrating the two-valued
  ``IS NOT NULL`` guard under the outer negation.
* ``(status == "ready" and $audit(r)) or (status != "ready" and not $audit(r))`` has an
  empty exclusion: every row can reach one of the two audit calls, so no row is
  error-free.  The branches are mutually exclusive on status but the unknown call on each
  side still has to run.
* ``(status == "ready" and $audit(r)) or (status == "ready" and not $audit(r))`` prunes
  only the non-ready rows.  The two calls are the same deterministic fact: a ``false``
  verdict makes the expression a tautology, while an error still propagates.  Treating
  them as independent draws, or inferring error behavior from a retained truth value,
  would both be unsound.
* ``(status ?? "") =~ "ready"`` with case-insensitive regex flags pushes to a
  ``lower(...) LIKE 'ready%'`` predicate, retaining the ``'READY'`` order that a
  case-sensitive literal comparison would prune, and pruning ``NULL`` (which discharges
  to ``''`` and cannot match).

Residual evaluation
-------------------

After SQLite applies ``NOT exclude_filter``, the surviving orders are passed to the
original, unmodified rule through :py:meth:`rule_engine.Rule.matches` exactly once.
Because every dropped order was provably false and incapable of raising, the residual
evaluation reproduces the full-set result: matched orders still match, and every error
that the original rule could raise is still raised on the same order.  No general-purpose
rule compiler is introduced — the analyzer handles only the catalogued grammar and
otherwise declines to exclude anything.
