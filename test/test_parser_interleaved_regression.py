"""Reproducible regression coverage for interleaved Parser reads.

The module exercises ``columns``, ``tables``, ``output_columns`` and
``columns_aliases`` in different access orders, using deterministic SQL
generators and controllable failures.  Judgements use public ``Parser``
attributes only; the production interface is left untouched.

Two kinds of tests live here:

* **Contract tests** — must pass on a correct parser.  They cover normal
  read-order equivalence, the TOP 5 equivalent source, truncated INSERT
  target-column recovery, and alarm/no-alarm detector controls.
* **Known-defect reproductions** — marked ``known_defect`` and left as
  real failures so the snapshot problem stays visible instead of being
  hidden behind a green run.  Each one raises through
  :func:`replay_message`, which prints the SQL, the fault phase, and the
  shortest public access prefix that reaches the first wrong source.
  Run only the contract tests with ``-m "not known_defect"`` once the
  defects are fixed, then remove the marks.
"""

from __future__ import annotations

import itertools
from dataclasses import dataclass
from typing import Any, Iterable

import pytest

from sql_metadata import Parser
from sql_metadata.nested_resolver import NestedResolver

#: Marker for reproductions of defects present in this snapshot.  They are
#: expected to fail today; once fixed they must start passing and the mark
#: should be removed.  Contract tests can be isolated with
#: ``-m "not known_defect"``.
known_defect = pytest.mark.known_defect

# ---------------------------------------------------------------------------
# Fixed, reproducible SQL inputs
# ---------------------------------------------------------------------------

BASE_TABLE = "orders"
BASE_COLUMN = "id"
OUTER_ALIAS = "order_id"
FINAL_ALIAS = "id"


def cte_chain(layers: int = 3, top: int | None = None) -> str:
    """Build a deterministic multi-layer CTE chain.

    ``orders.id`` is aliased to ``order_id`` in the innermost CTE, each
    intermediate CTE carries ``order_id`` forward, and the outermost
    SELECT renames it back to ``id``.  ``top`` injects ``TOP n`` into the
    innermost source so equivalent sources can be compared.

    Example for ``layers=3``::

        WITH raw AS (SELECT id AS order_id FROM orders),
             layer1 AS (SELECT order_id FROM raw),
             outer_cte AS (SELECT order_id FROM layer1)
        SELECT order_id AS id FROM outer_cte
    """
    if layers < 2:
        raise ValueError("layers must be at least 2")
    names = ["raw"]
    names.extend(f"layer{index}" for index in range(1, layers - 1))
    names.append("outer_cte")

    prefix = f"TOP {top} " if top is not None else ""
    parts = [f"raw AS (SELECT {prefix}id AS order_id FROM {BASE_TABLE})"]
    previous = "raw"
    for name in names[1:]:
        parts.append(f"{name} AS (SELECT order_id FROM {previous})")
        previous = name

    return "WITH " + ",\n     ".join(parts) + f"\nSELECT order_id AS id FROM {previous}"


MULTI = cte_chain(3)
MULTI_TOP5 = cte_chain(3, top=5)


def nested_cte(layers: int = 2, top: int | None = None) -> str:
    """Build nested (not sibling) CTEs so inner/outer completion is ordered.

    The innermost query reads ``orders.id AS order_id``.  Each enclosing
    CTE references the one directly inside it, and the final SELECT
    renames ``order_id`` back to ``id``.  Unlike :func:`cte_chain`, the
    inner layer finishes parsing before the outer layer starts, which is
    what makes an "inner complete, outer pending" interruption meaningful.
    """
    if layers < 2:
        raise ValueError("layers must be at least 2")
    prefix = f"TOP {top} " if top is not None else ""
    sql = f"SELECT {prefix}id AS order_id FROM {BASE_TABLE}"
    for depth in range(layers - 1):
        name = "inner_cte" if depth == 0 else f"layer{depth}"
        sql = f"WITH {name} AS ({sql})\nSELECT order_id FROM {name}"
    return f"WITH outer_cte AS (\n{sql}\n)\nSELECT order_id AS id FROM outer_cte"


NESTED = nested_cte(2)
NESTED_TOP5 = nested_cte(2, top=5)

#: A truncated INSERT log that still carries the real target column list.
TRUNCATED_INSERT = "INSERT INTO orders (id, order_id) VALUES (1, 2, 'unclosed"

#: Truncated INSERT whose only ``INTO ... (...)`` text lives inside a
#: string literal.  It must never be used as a target-column source.
QUOTED_INTO_TRUNCATED = "INSERT INTO audit_log VALUES ('x INTO orders (fake_a, fake_b)"

#: Well-formed counterpart used as a no-alarm control.
QUOTED_INTO_WELL_FORMED = (
    "INSERT INTO audit_log VALUES ('x INTO orders (fake_a, fake_b)')"
)

CORE_ATTRS = ("tables", "columns", "output_columns", "columns_aliases")
VIEW_ATTRS = CORE_ATTRS + ("columns_aliases_names",)


# ---------------------------------------------------------------------------
# Observation ledger and replay diagnostics
# ---------------------------------------------------------------------------


def freeze(value: Any) -> Any:
    """Convert public results into comparable, immutable structures."""
    if isinstance(value, dict):
        return tuple((key, freeze(item)) for key, item in sorted(value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(freeze(item) for item in value)
    return value


@dataclass(frozen=True)
class Observation:
    """One public attribute access and its observed outcome."""

    attribute: str
    kind: str  # "value" or "error"
    payload: Any

    def render(self, var: str = "parser") -> str:
        if self.kind == "error":
            return f"{var}.{self.attribute} !! {self.payload}"
        return f"{var}.{self.attribute} -> {self.payload!r}"


@dataclass(frozen=True)
class Recorded:
    """An observation together with its exact ledger position."""

    index: int
    observation: Observation

    @property
    def attribute(self) -> str:
        return self.observation.attribute

    @property
    def kind(self) -> str:
        return self.observation.kind

    @property
    def payload(self) -> Any:
        return self.observation.payload

    def render(self, var: str = "parser") -> str:
        return self.observation.render(var)


class ReadLedger:
    """Ordered record of public reads for replay and provenance checks."""

    def __init__(self, label: str) -> None:
        self.label = label
        self.steps: list[Observation] = []

    def read(self, parser: Parser, attribute: str) -> "Recorded":
        try:
            value = getattr(parser, attribute)
        except Exception as exc:  # noqa: BLE001 - recorded as evidence
            observation = Observation(attribute, "error", type(exc).__name__)
        else:
            observation = Observation(attribute, "value", freeze(value))
        self.steps.append(observation)
        return Recorded(len(self.steps) - 1, observation)

    @property
    def first_error_index(self) -> int | None:
        for index, step in enumerate(self.steps):
            if step.kind == "error":
                return index
        return None

    def recorded(self) -> list["Recorded"]:
        """Paired views of steps with their exact positions."""
        return [Recorded(index, step) for index, step in enumerate(self.steps)]

    def successful_before_error(self) -> tuple[str, ...]:
        index = self.first_error_index
        if index is None:
            return tuple(step.attribute for step in self.steps if step.kind == "value")
        return tuple(
            step.attribute for step in self.steps[:index] if step.kind == "value"
        )

    def signature(self) -> tuple[Any, ...]:
        return tuple((step.attribute, step.kind, step.payload) for step in self.steps)


def frozen_view(parser: Parser) -> dict[str, Any]:
    """Read every public view attribute in a fixed order and freeze it."""
    return {attribute: freeze(getattr(parser, attribute)) for attribute in VIEW_ATTRS}


def expected_chain_view(layers: int) -> dict[str, Any]:
    """Explicit expected view for a generated CTE chain."""
    return {
        "tables": freeze([BASE_TABLE]),
        "columns": freeze([BASE_COLUMN]),
        "output_columns": freeze([OUTER_ALIAS] * layers + [FINAL_ALIAS]),
        "columns_aliases": freeze({OUTER_ALIAS: FINAL_ALIAS, FINAL_ALIAS: OUTER_ALIAS}),
        "columns_aliases_names": freeze([OUTER_ALIAS, FINAL_ALIAS]),
    }


def first_divergence(
    actual: Iterable[Observation], expected: Iterable[Observation]
) -> int | None:
    """Index of the first observation that differs, or ``None``."""
    actual_list = list(actual)
    expected_list = list(expected)
    for index in range(min(len(actual_list), len(expected_list))):
        left = actual_list[index]
        right = expected_list[index]
        if (left.attribute, left.kind, left.payload) != (
            right.attribute,
            right.kind,
            right.payload,
        ):
            return index
    if len(actual_list) != len(expected_list):
        return min(len(actual_list), len(expected_list))
    return None


def replay_message(
    sql: str,
    steps: Iterable[Observation],
    index: int,
    expected: Iterable[Observation] | None = None,
    phase: str | None = None,
) -> str:
    """Render the shortest replayable access prefix for a failure.

    The message carries the SQL, the optional fault phase, and every
    public access up to and including the first divergent step, so the
    failure can be replayed by reading the same prefix on a fresh
    ``Parser``.
    """
    step_list = list(steps)
    expected_list = list(expected) if expected is not None else []
    lines = [
        f"First divergent access prefix (replay steps 0..{index}):",
        f"  sql = {sql!r}",
    ]
    if phase is not None:
        lines.append(f"  fault_phase = {phase!r}")
    for position, step in enumerate(step_list[: index + 1]):
        marker = "   <-- first divergence" if position == index else ""
        lines.append(f"  [{position}] {step.render()}{marker}")
    if index < len(expected_list):
        lines.append(
            f"  expected at [{index}]: {expected_list[index].render('expected')}"
        )
    else:
        lines.append(f"  expected at [{index}]: <no access>")
    return "\n".join(lines)


def assert_steps(
    actual: Iterable[Observation],
    expected: Iterable[Observation],
    *,
    sql: str,
    phase: str | None = None,
) -> None:
    """Assert two ledgers match, reporting the minimal divergent prefix."""
    actual_list = list(actual)
    expected_list = list(expected)
    index = first_divergence(actual_list, expected_list)
    if index is not None:
        raise AssertionError(
            replay_message(sql, actual_list, index, expected_list, phase)
        )


# ---------------------------------------------------------------------------
# Controllable fault injection
# ---------------------------------------------------------------------------


class InjectedFault(RuntimeError):
    """Fault raised by the test harness; never part of the library."""


class FaultGate:
    """Armed/disarmed failure gate used to interrupt an in-flight read."""

    def __init__(self, phase: str) -> None:
        self.phase = phase
        self.armed = True
        self.target_reached = False
        self.fired = 0
        self.completed_bodies: tuple[str, ...] = ()

    def reach(self) -> None:
        self.target_reached = True

    def raise_if_armed(self) -> None:
        if self.armed and self.target_reached:
            self.fired += 1
            raise InjectedFault(f"injected fault during phase {self.phase!r}")


def install_fault(monkeypatch: pytest.MonkeyPatch, phase: str) -> FaultGate:
    """Install a deterministic fault for *phase*.

    ``after_preliminary`` interrupts at the first main-resolver entry,
    after independent public clues were read but before nested column
    resolution starts.

    ``after_inner_complete`` runs inside the resolver that owns only the
    ``inner_cte`` body.  Its lookup completes, then the fault fires
    before the enclosing resolver returns — the inner layer has
    completed while the outer layer has not.
    """
    gate = FaultGate(phase)

    if phase == "after_preliminary":
        original = NestedResolver.resolve

        def faulty_resolve(
            self: NestedResolver,
            columns: Any,
            columns_dict: Any,
            columns_aliases: Any,
            subqueries_names: Any,
            subqueries: Any,
            with_names: Any,
            with_queries: Any,
        ) -> Any:
            if len(with_names) >= 2:
                gate.reach()
            gate.raise_if_armed()
            return original(
                self,
                columns,
                columns_dict,
                columns_aliases,
                subqueries_names,
                subqueries,
                with_names,
                with_queries,
            )

        monkeypatch.setattr(NestedResolver, "resolve", faulty_resolve)
        return gate

    if phase == "after_inner_complete":
        original_lookup = NestedResolver._lookup_alias_in_nested

        def faulty_lookup(
            self: NestedResolver,
            col_name: str,
            names: list[str],
            definitions: dict[str, str],
            parser_cache: dict[str, Any],
            check_columns: bool = False,
        ) -> list[str] | None:
            result = original_lookup(
                self,
                col_name,
                names,
                definitions,
                parser_cache,
                check_columns,
            )
            # The resolver owning a single CTE body is an inner layer; once
            # its lookup populated the cache, the inner layer is complete
            # while the caller (outer layer) is still running.
            if (
                not gate.target_reached
                and not check_columns
                and len(self._with_queries) == 1
                and parser_cache
            ):
                gate.reach()
                gate.completed_bodies = tuple(sorted(parser_cache))
            gate.raise_if_armed()
            return result

        monkeypatch.setattr(NestedResolver, "_lookup_alias_in_nested", faulty_lookup)
        return gate

    raise ValueError(f"unknown fault phase: {phase}")


# ---------------------------------------------------------------------------
# Normal SQL: equivalence and interleaving
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("layers", [2, 3, 4])
@pytest.mark.parametrize("top", [None, 5])
def test_cte_chain_and_its_top_variant_agree(layers: int, top: int | None) -> None:
    """A plain source and its TOP 5 equivalent expose the same view."""
    plain = cte_chain(layers)
    top_variant = cte_chain(layers, top=5)
    expected = expected_chain_view(layers)

    assert frozen_view(Parser(plain)) == expected
    assert frozen_view(Parser(top_variant)) == expected
    assert frozen_view(Parser(plain)) == frozen_view(Parser(top_variant))


@pytest.mark.parametrize("layers", [2, 3, 4])
def test_nested_cte_and_its_top_variant_agree(layers: int) -> None:
    """The nested fixture, too, is equivalent to its TOP 5 source."""
    plain = nested_cte(layers)
    top_variant = nested_cte(layers, top=5)
    expected = expected_chain_view(layers)

    assert frozen_view(Parser(plain)) == expected
    assert frozen_view(Parser(top_variant)) == expected
    assert frozen_view(Parser(plain)) == frozen_view(Parser(top_variant))


@pytest.mark.parametrize("sql", [NESTED, NESTED_TOP5])
@pytest.mark.parametrize("order", list(itertools.permutations(CORE_ATTRS)))
def test_nested_all_core_read_interleavings_agree(
    sql: str, order: tuple[str, ...]
) -> None:
    """Every permutation of the core reads agrees on the nested fixture."""
    parser = Parser(sql)
    ledger = ReadLedger(f"nested-order={order}")
    for attribute in order:
        ledger.read(parser, attribute)
    for attribute in VIEW_ATTRS:
        ledger.read(parser, attribute)

    expected = expected_chain_view(2)
    for index, step in enumerate(ledger.steps):
        assert step.kind == "value", replay_message(
            sql, ledger.steps, index, phase=f"order={order}"
        )
        assert step.payload == expected[step.attribute], replay_message(
            sql,
            ledger.steps,
            index,
            [Observation(step.attribute, "value", expected[step.attribute])],
            f"order={order}",
        )
    assert frozen_view(parser) == expected


@pytest.mark.parametrize("sql", [MULTI, MULTI_TOP5])
@pytest.mark.parametrize("order", list(itertools.permutations(CORE_ATTRS)))
def test_all_core_read_interleavings_agree(sql: str, order: tuple[str, ...]) -> None:
    """Every permutation of the four core reads yields the same view."""
    parser = Parser(sql)
    ledger = ReadLedger(f"order={order}")
    for attribute in order:
        ledger.read(parser, attribute)
    for attribute in VIEW_ATTRS:
        ledger.read(parser, attribute)

    expected = expected_chain_view(3)
    for index, step in enumerate(ledger.steps):
        assert step.kind == "value", replay_message(
            sql, ledger.steps, index, phase=f"order={order}"
        )
        assert step.payload == expected[step.attribute], replay_message(
            sql,
            ledger.steps,
            index,
            [Observation(step.attribute, "value", expected[step.attribute])],
            f"order={order}",
        )
    assert frozen_view(parser) == expected


@pytest.mark.parametrize("sql", [MULTI, MULTI_TOP5])
@pytest.mark.parametrize("priority", CORE_ATTRS)
def test_priority_field_then_supplement_agrees(sql: str, priority: str) -> None:
    """Reading one field first and supplementing the rest is equivalent."""
    priority_parser = Parser(sql)
    priority_ledger = ReadLedger(f"priority={priority}")
    priority_ledger.read(priority_parser, priority)
    for attribute in VIEW_ATTRS:
        priority_ledger.read(priority_parser, attribute)
    assert frozen_view(priority_parser) == expected_chain_view(3)

    canonical_parser = Parser(sql)
    canonical_ledger = ReadLedger("canonical")
    for attribute in VIEW_ATTRS:
        canonical_ledger.read(canonical_parser, attribute)

    assert priority_ledger.steps[-1] == canonical_ledger.steps[-1]


# ---------------------------------------------------------------------------
# Interruption paths: usable clue -> incomplete step -> genuine completion
# ---------------------------------------------------------------------------


def _read_preliminary(ledger: ReadLedger, parser: Parser, phase: str) -> None:
    ledger.read(parser, "tables")
    ledger.read(parser, "with_names")
    if phase == "after_inner_complete":
        ledger.read(parser, "with_queries")


def run_interrupted_path(
    phase: str,
) -> tuple[Parser, ReadLedger, FaultGate, Recorded, Recorded]:
    """Run one interrupted path on the nested CTE fixture."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        gate = install_fault(monkeypatch, phase)
        parser = Parser(NESTED)
        ledger = ReadLedger(phase)
        _read_preliminary(ledger, parser, phase)

        first = ledger.read(parser, "columns")
        assert first.kind == "error" and first.payload == "InjectedFault", (
            "the injected fault should interrupt the column read"
        )
        still_incomplete = ledger.read(parser, "columns")

        gate.armed = False
        recovered = ledger.read(parser, "columns")

        for attribute in ("output_columns", "columns_aliases", "columns_aliases_names"):
            ledger.read(parser, attribute)

        return parser, ledger, gate, still_incomplete, recovered


@known_defect
@pytest.mark.parametrize("phase", ["after_preliminary", "after_inner_complete"])
def test_incomplete_step_never_claims_completion(phase: str) -> None:
    """While the fault is armed, the interrupted step must stay incomplete."""
    _, ledger, _, still_incomplete, _ = run_interrupted_path(phase)
    expected = Observation("columns", "error", "InjectedFault")
    if still_incomplete.kind != "error" or still_incomplete.payload != expected.payload:
        raise AssertionError(
            replay_message(
                NESTED,
                ledger.steps,
                still_incomplete.index,
                [expected],
                phase,
            )
            + "\n  'columns' reported a value instead of staying incomplete"
        )


@known_defect
@pytest.mark.parametrize("phase", ["after_preliminary", "after_inner_complete"])
def test_fault_cleared_recomputes_the_full_view(phase: str) -> None:
    """After the fault disappears the same Parser must complete correctly."""
    parser, ledger, _, _, recovered = run_interrupted_path(phase)
    expected_columns = freeze([BASE_COLUMN])
    if recovered.kind != "value" or recovered.payload != expected_columns:
        raise AssertionError(
            replay_message(
                NESTED,
                ledger.steps,
                recovered.index,
                [Observation("columns", "value", expected_columns)],
                phase,
            )
            + "\n  a cleared fault must trigger genuine recomputation, not "
            "reuse of a partial cache"
        )
    assert frozen_view(parser) == expected_chain_view(2)


@known_defect
@pytest.mark.parametrize("phase", ["after_preliminary", "after_inner_complete"])
def test_recovered_view_matches_fresh_parser(phase: str) -> None:
    """The recovered instance must equal an independently built instance."""
    parser, ledger, _, _, _ = run_interrupted_path(phase)
    expected = expected_chain_view(2)
    for attribute in VIEW_ATTRS:
        observed = freeze(getattr(parser, attribute))
        if observed != expected[attribute]:
            index = next(
                position
                for position, step in enumerate(ledger.steps)
                if step.attribute == attribute
            )
            raise AssertionError(
                replay_message(
                    NESTED,
                    ledger.steps,
                    index,
                    [Observation(attribute, "value", expected[attribute])],
                    phase,
                )
            )
    assert frozen_view(parser) == expected


@pytest.mark.parametrize("phase", ["after_preliminary", "after_inner_complete"])
def test_interleaved_fresh_instance_stays_correct(phase: str) -> None:
    """Interleave a complete read on another instance during recovery."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        gate = install_fault(monkeypatch, phase)
        parser = Parser(NESTED)
        ledger = ReadLedger(phase)
        _read_preliminary(ledger, parser, phase)
        ledger.read(parser, "columns")
        gate.armed = False

        other = Parser(NESTED)
        other_ledger = ReadLedger(f"other@{phase}")
        for attribute in CORE_ATTRS:
            ledger.read(parser, attribute)
            other_ledger.read(other, attribute)

    assert frozen_view(other) == expected_chain_view(2)


def test_three_paths_are_distinguished_by_ledger_not_final_view() -> None:
    """Final tables/output matching must not conflate the three paths.

    The three documented paths are: uninterrupted, interrupted after the
    preliminary fields, and interrupted after an inner layer completed.
    All three end with the same public ``tables`` and ``output_columns``
    in this snapshot, so only the access ledger can tell them apart.
    """
    uninterrupted_parser = Parser(NESTED)
    uninterrupted = ReadLedger("uninterrupted")
    _read_preliminary(uninterrupted, uninterrupted_parser, "uninterrupted")
    for attribute in ("columns",) + VIEW_ATTRS:
        uninterrupted.read(uninterrupted_parser, attribute)

    interrupted = {}
    for phase in ("after_preliminary", "after_inner_complete"):
        parser, ledger, _, _, _ = run_interrupted_path(phase)
        interrupted[phase] = (parser, ledger)

    records = {"uninterrupted": (uninterrupted_parser, uninterrupted), **interrupted}

    final_pairs = {
        (freeze(parser.tables), freeze(parser.output_columns))
        for parser, _ in records.values()
    }
    assert len(final_pairs) == 1, (
        "all three paths end with the same tables/output_columns; the test "
        "must therefore compare their full ledgers"
    )

    signatures = {ledger.signature() for _, ledger in records.values()}
    assert len(signatures) == 3

    assert uninterrupted.first_error_index is None
    for phase in interrupted:
        assert interrupted[phase][1].first_error_index is not None

    preliminary_clues = interrupted["after_preliminary"][1].successful_before_error()
    inner_clues = interrupted["after_inner_complete"][1].successful_before_error()
    assert preliminary_clues != inner_clues
    assert "with_queries" in inner_clues


def test_inner_layer_had_completed_before_interruption() -> None:
    """The inner-complete path really parsed an inner body before failing."""
    with pytest.MonkeyPatch.context() as monkeypatch:
        gate = install_fault(monkeypatch, "after_inner_complete")
        parser = Parser(NESTED)
        parser.tables
        with pytest.raises(InjectedFault):
            parser.columns
    assert gate.completed_bodies == ("inner_cte",), (
        "the interruption should happen after the inner body parsed and "
        "before the outer layer finished"
    )


# ---------------------------------------------------------------------------
# Truncated / quoted bad input
# ---------------------------------------------------------------------------


BAD_STARTS = ("tables", "columns", "output_columns", "columns_aliases")
BAD_PATH = ("tables", "columns", "output_columns", "columns_aliases")


def bad_read_order(start: str) -> tuple[str, ...]:
    """Read *start* first, then the same path for the remaining fields."""
    return (start,) + tuple(attribute for attribute in BAD_PATH if attribute != start)


def _truncated_expectation(attribute: str) -> Observation:
    if attribute == "tables":
        return Observation(attribute, "error", "InvalidQueryDefinition")
    if attribute == "columns":
        return Observation(attribute, "value", freeze(["id", "order_id"]))
    if attribute == "output_columns":
        return Observation(attribute, "value", freeze([]))
    return Observation(attribute, "value", freeze({}))


@pytest.mark.parametrize("start", ["columns", "output_columns", "columns_aliases"])
def test_truncated_insert_recovers_target_columns_from_every_start(
    start: str,
) -> None:
    """The real INSERT target list survives whichever field is read first.

    The ``tables``-first path is a known snapshot defect (a failed
    ``tables`` read leaves ``columns`` unable to reach the regex
    fallback), so it is exercised separately by
    :func:`test_truncated_insert_after_failed_tables_is_known_defect`.
    """
    parser = Parser(TRUNCATED_INSERT)
    ledger = ReadLedger(f"truncated-start={start}")
    for attribute in bad_read_order(start):
        ledger.read(parser, attribute)

    expected = [
        _truncated_expectation(attribute) for attribute in bad_read_order(start)
    ]
    assert_steps(
        ledger.steps,
        expected,
        sql=TRUNCATED_INSERT,
        phase=f"start={start}",
    )

    columns_step = next(step for step in ledger.steps if step.attribute == "columns")
    assert columns_step.kind == "value"
    assert columns_step.payload == freeze(["id", "order_id"])


@pytest.mark.parametrize("start", ["columns", "output_columns", "columns_aliases"])
def test_bad_input_repeat_on_same_path_is_stable(start: str) -> None:
    """Re-reading the same path on the same instance repeats outcomes."""
    parser = Parser(TRUNCATED_INSERT)
    order = bad_read_order(start)
    first = ReadLedger(f"first-start={start}")
    for attribute in order:
        first.read(parser, attribute)
    second = ReadLedger(f"second-start={start}")
    for attribute in order:
        second.read(parser, attribute)
    assert first.signature() == second.signature()


@known_defect
def test_truncated_insert_after_failed_tables_is_known_defect() -> None:
    """Reproduce the ``tables``-first truncated-INSERT defect.

    Reading ``tables`` first raises ``InvalidQueryDefinition`` (expected
    for malformed SQL).  A subsequent ``columns`` read should fall back
    to the ``INTO`` column regex, but the snapshot raises
    ``AssertionError`` instead because the failed read left the parser's
    AST accessor in a state where ``_require_ast`` asserts.
    """
    parser = Parser(TRUNCATED_INSERT)
    ledger = ReadLedger("truncated-start=tables")
    for attribute in bad_read_order("tables"):
        ledger.read(parser, attribute)

    columns_step = next(
        step for step in ledger.recorded() if step.attribute == "columns"
    )
    expected = Observation("columns", "value", freeze(["id", "order_id"]))
    if columns_step.observation != expected:
        raise AssertionError(
            replay_message(
                TRUNCATED_INSERT,
                ledger.steps,
                columns_step.index,
                [expected],
                "start=tables",
            )
            + "\n  a failed tables read must not block the INSERT target-column "
            "fallback"
        )


@known_defect
@pytest.mark.parametrize("start", BAD_STARTS)
def test_quoted_into_text_never_invents_target_columns(start: str) -> None:
    """A quoted ``INTO ...`` must not be read as a target-column source."""
    parser = Parser(QUOTED_INTO_TRUNCATED)
    ledger = ReadLedger(f"quoted-start={start}")
    for attribute in bad_read_order(start):
        ledger.read(parser, attribute)

    columns_step = next(
        step for step in ledger.recorded() if step.attribute == "columns"
    )
    if columns_step.kind == "value":
        leaked = {"fake_a", "fake_b"} & {str(item) for item in columns_step.payload}
        if leaked:
            raise AssertionError(
                replay_message(
                    QUOTED_INTO_TRUNCATED,
                    ledger.steps,
                    columns_step.index,
                    [Observation("columns", "value", freeze([]))],
                    f"start={start}",
                )
                + f"\n  quoted INTO text leaked fake target columns: {sorted(leaked)}"
            )
    else:
        assert False, replay_message(
            QUOTED_INTO_TRUNCATED,
            ledger.steps,
            columns_step.index,
            [Observation("columns", "value", freeze([]))],
            f"start={start}",
        )


@pytest.mark.parametrize("start", BAD_STARTS)
def test_well_formed_quoted_control_has_no_columns(start: str) -> None:
    """The well-formed control must stay quiet on every start."""
    parser = Parser(QUOTED_INTO_WELL_FORMED)
    ledger = ReadLedger(f"well-formed-start={start}")
    for attribute in bad_read_order(start):
        ledger.read(parser, attribute)

    columns_step = next(step for step in ledger.steps if step.attribute == "columns")
    if columns_step.kind == "value":
        assert columns_step.payload == freeze([])
    else:
        assert columns_step.payload == "InvalidQueryDefinition"


# ---------------------------------------------------------------------------
# Alarm / no-alarm controls
# ---------------------------------------------------------------------------


def audit_views(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    """Report core view fields that disagree."""
    return [
        attribute for attribute in CORE_ATTRS if left[attribute] != right[attribute]
    ]


def audit_columns(
    observed: Iterable[str],
    expected: Iterable[str],
    forbidden: Iterable[str] = (),
) -> list[str]:
    """Report column mismatches or forbidden (quoted-leak) names."""
    observed_tuple = tuple(observed)
    issues = []
    if observed_tuple != tuple(expected):
        issues.append(f"columns differ: {observed_tuple!r} != {tuple(expected)!r}")
    leaked = sorted(set(observed_tuple) & set(forbidden))
    if leaked:
        issues.append(f"forbidden columns leaked: {leaked}")
    return issues


def test_equivalent_sources_trigger_no_alarm() -> None:
    left = frozen_view(Parser(MULTI))
    right = frozen_view(Parser(MULTI_TOP5))
    assert audit_views(left, right) == []
    assert audit_columns(left["columns"], ["id"], ["fake_a"]) == []


def test_divergent_sources_trigger_alarm() -> None:
    left = frozen_view(Parser(MULTI))
    right = frozen_view(Parser(cte_chain(3).replace("AS id", "AS other_id")))
    assert audit_views(left, right)


def test_real_truncated_columns_trigger_no_alarm() -> None:
    """Real truncated columns are a usable clue, not an alarm."""
    parser = Parser(TRUNCATED_INSERT)
    assert parser.columns == ["id", "order_id"]
    assert audit_columns(parser.columns, ["id", "order_id"], ["fake_a"]) == []


def test_quoted_into_leak_triggers_alarm() -> None:
    """A quoted INTO leak must be reported even when nothing else differs."""
    assert audit_columns(["fake_a", "fake_b"], [], ["fake_a", "fake_b"])


def test_recomputed_view_triggers_no_alarm() -> None:
    first = frozen_view(Parser(MULTI))
    second = frozen_view(Parser(MULTI))
    assert audit_views(first, second) == []


def test_stale_columns_snapshot_triggers_alarm() -> None:
    canonical = frozen_view(Parser(MULTI))
    stale = dict(canonical)
    stale["columns"] = freeze(["id", "order_id"])
    assert audit_views(stale, canonical) == ["columns"]


def cache_misread_alarm(
    partial_view: dict[str, Any], completed_view: dict[str, Any]
) -> list[str]:
    """Alarm when a cached partial view is presented as a completed one.

    A partial view differs from a completed view on at least one ``columns``
    / ``columns_aliases`` field even though both already agree on ``tables``
    and ``output_columns``.  This is exactly the shape the snapshot's
    ``_columns_extracted`` cache flag can produce.
    """
    issues = []
    if partial_view["tables"] != completed_view["tables"]:
        issues.append("tables differ")
    if partial_view["output_columns"] != completed_view["output_columns"]:
        issues.append("output_columns differ")
    for attribute in ("columns", "columns_aliases"):
        if partial_view[attribute] != completed_view[attribute]:
            issues.append(f"cached {attribute} is not the completed value")
    return issues


def completed_nested_view() -> dict[str, Any]:
    return frozen_view(Parser(NESTED))


def partial_nested_view() -> dict[str, Any]:
    """The value a stale partial ``_columns`` cache exposes."""
    partial = completed_nested_view()
    partial["columns"] = freeze(["id", "order_id"])
    return partial


def test_cache_misread_detector_alarms_on_partial_snapshot() -> None:
    """A cached partial view must alarm even when tables/output agree."""
    issues = cache_misread_alarm(partial_nested_view(), completed_nested_view())
    assert issues
    assert all(
        "tables" not in issue and "output_columns" not in issue for issue in issues
    )


def test_cache_misread_detector_quiet_on_completed_view() -> None:
    completed = completed_nested_view()
    assert cache_misread_alarm(completed, completed) == []


@known_defect
def test_recovered_reads_do_not_trigger_cache_misread_alarm() -> None:
    """A recovered Parser must not look like a cached mis-completion.

    The Parser is interrupted, the fault is cleared, and the public fields
    are read without rebuilding the instance.  On a correct parser these
    reads recompute, so the view matches a fresh instance and the
    cache-misread detector stays quiet.  The snapshot reuses the partial
    ``_columns`` cache, leaving ``columns`` wider than the completed view
    while ``tables`` / ``output_columns`` already agree.
    """
    parser, _, _, _, _ = run_interrupted_path("after_preliminary")
    observed = {
        attribute: freeze(getattr(parser, attribute)) for attribute in VIEW_ATTRS
    }
    completed = completed_nested_view()
    issues = cache_misread_alarm(observed, completed)
    if issues:
        index = next(
            position
            for position, attribute in enumerate(VIEW_ATTRS)
            if observed[attribute] != completed[attribute]
        )
        raise AssertionError(
            replay_message(
                NESTED,
                [Observation(attr, "value", observed[attr]) for attr in VIEW_ATTRS],
                index,
                [Observation(attr, "value", completed[attr]) for attr in VIEW_ATTRS],
                "after_preliminary",
            )
            + f"\n  post-recovery detector issues: {issues}"
        )


# ---------------------------------------------------------------------------
# Replay helper self-tests
# ---------------------------------------------------------------------------


def test_shortest_divergent_prefix_is_minimal_and_replayable() -> None:
    sql = MULTI
    parser = Parser(sql)
    ledger = ReadLedger("replay")
    for attribute in ("tables", "columns", "columns_aliases"):
        ledger.read(parser, attribute)

    expected = list(ledger.steps)
    expected[1] = Observation("columns", "error", "InjectedFault")

    index = first_divergence(ledger.steps, expected)
    assert index == 1

    message = replay_message(sql, ledger.steps, index, expected, "unit")
    assert "sql =" in message
    assert "[0] parser.tables" in message
    assert "[1] parser.columns" in message
    assert "first divergence" in message

    replayed = ReadLedger("replay-again")
    replay_parser = Parser(sql)
    for attribute in ("tables", "columns"):
        replayed.read(replay_parser, attribute)
    assert replayed.steps == ledger.steps[: index + 1]


def test_expected_failure_reports_the_shortest_prefix() -> None:
    """A mismatch surfaces the replay prefix, not just a diff."""
    actual = [
        Observation("tables", "value", freeze(["orders"])),
        Observation("columns", "value", freeze(["id"])),
    ]
    expected = [
        Observation("tables", "value", freeze(["orders"])),
        Observation("columns", "value", freeze(["order_id"])),
    ]
    with pytest.raises(AssertionError) as excinfo:
        assert_steps(actual, expected, sql=MULTI, phase="self-test")
    message = str(excinfo.value)
    assert "replay steps 0..1" in message
    assert "[1] parser.columns" in message
    assert "first divergence" in message
    assert "fault_phase = 'self-test'" in message
