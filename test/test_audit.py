"""Tests for the session-aware audit-log analysis in :mod:`sql_metadata.audit`.

The central fixture reproduces the two-session audit scenario:

* Writer session A rebuilds a temporary ``stage`` table from three sources
  behind two same-named savepoints ``z``, writes ``q``/``r``, rolls back to
  the inner savepoint, and finally truncates and rewrites permanent table
  ``p`` (originally ``seed_old``).
* Reader session B built its own same-named temporary ``stage`` from
  ``source_other`` before A's transaction, and writes ``audit_1`` (reads
  ``p``) and ``audit_2`` (reads its own ``stage``).

The log only guarantees within-session order; the single cross-session fact
is that A commits before B's second query starts.
"""

import logging

import pytest
import sqlglot

from sql_metadata import (
    AuditKind,
    AuditLog,
    IsolationLevel,
    SessionAuditAnalyzer,
    analyze_audit_log,
    split_sql_statements,
)
from sql_metadata.audit import (
    AuditPossibility,
    LogEntry,
    _classify_kind,
    _isolation_from_text,
    _savepoint_name,
    _TransactionReplay,
    parse_statement,
)

WRITER = "session-a"
READER = "session-b"


def writer_rows(branch: str) -> list[dict[str, object]]:
    """Build session A rows.

    ``branch="inner"`` rolls back only the inner savepoint (the default
    continuation); ``branch="outer"`` also rolls back to the outer
    savepoint, discarding the ``q`` write.
    """
    rows: list[dict[str, object]] = [
        {"id": "a-begin", "session": WRITER, "transaction": "ta", "sql": "BEGIN"},
        {
            "id": "a-stage-a",
            "session": WRITER,
            "transaction": "ta",
            "sql": "CREATE TEMPORARY TABLE stage AS SELECT * FROM source_a",
        },
        {
            "id": "a-sp-outer",
            "session": WRITER,
            "transaction": "ta",
            "sql": "SAVEPOINT z",
        },
        {
            "id": "a-stage-b",
            "session": WRITER,
            "transaction": "ta",
            "sql": (
                "CREATE OR REPLACE TEMPORARY TABLE stage "
                "AS SELECT * FROM source_b -- rebuild; semicolon in comment\n"
            ),
        },
        {
            "id": "a-q",
            "session": WRITER,
            "transaction": "ta",
            "sql": "INSERT INTO q SELECT * FROM stage",
        },
        {
            "id": "a-sp-inner",
            "session": WRITER,
            "transaction": "ta",
            "sql": "SAVEPOINT z",
        },
        {
            "id": "a-stage-c",
            "session": WRITER,
            "transaction": "ta",
            "sql": "CREATE OR REPLACE TEMPORARY TABLE stage AS SELECT * FROM source_c",
        },
        {
            "id": "a-r",
            "session": WRITER,
            "transaction": "ta",
            "sql": "INSERT INTO r SELECT * FROM stage",
        },
        {
            "id": "a-rb-inner",
            "session": WRITER,
            "transaction": "ta",
            "sql": "ROLLBACK TO SAVEPOINT z",
        },
        {
            "id": "a-release",
            "session": WRITER,
            "transaction": "ta",
            "sql": "RELEASE SAVEPOINT z",
        },
    ]
    if branch == "outer":
        rows.append(
            {
                "id": "a-rb-outer",
                "session": WRITER,
                "transaction": "ta",
                "sql": "ROLLBACK TO SAVEPOINT z",
            }
        )
    rows.extend(
        [
            {
                "id": "a-trunc",
                "session": WRITER,
                "transaction": "ta",
                "sql": "TRUNCATE TABLE p",
            },
            {
                "id": "a-p",
                "session": WRITER,
                "transaction": "ta",
                "sql": "INSERT INTO p SELECT * FROM stage",
            },
            {
                "id": "a-commit",
                "session": WRITER,
                "transaction": "ta",
                "sql": "COMMIT",
            },
        ]
    )
    return rows


def reader_rows() -> list[dict[str, object]]:
    return [
        {
            "id": "b-begin",
            "session": READER,
            "transaction": "tb",
            "sql": ("SET TRANSACTION ISOLATION LEVEL READ COMMITTED; BEGIN"),
        },
        {
            "id": "b-stage",
            "session": READER,
            "transaction": "tb",
            "sql": "CREATE TEMPORARY TABLE stage AS SELECT * FROM source_other",
        },
        {
            "id": "b-audit-1",
            "session": READER,
            "transaction": "tb",
            "sql": ("INSERT INTO audit_1 SELECT id, 'note; x''y' AS note FROM p"),
        },
        {
            "id": "b-audit-2",
            "session": READER,
            "transaction": "tb",
            "sql": "INSERT INTO audit_2 SELECT * FROM stage",
            "happens_before": ["a-commit"],
        },
        {
            "id": "b-commit",
            "session": READER,
            "transaction": "tb",
            "sql": "COMMIT",
        },
    ]


def build_log(branch: str) -> AuditLog:
    return AuditLog(writer_rows(branch) + reader_rows())


def analyze(branch: str, isolation: IsolationLevel):
    return SessionAuditAnalyzer(build_log(branch)).analyze(
        writer_session=WRITER,
        reader_session=READER,
        permanent_table="p",
        baseline_source="seed_old",
        isolation=isolation,
    )


# ---------------------------------------------------------------------------
# Statement splitting
# ---------------------------------------------------------------------------


def test_split_respects_semicolons_in_strings_and_comments():
    script = (
        "INSERT INTO q SELECT 'a;b' FROM t; "
        "-- line; comment\n"
        "INSERT INTO r SELECT 'it''s; ok'; "
        "/* block; comment */ SELECT 1; "
        "# hash; comment\n"
        "SELECT 2"
    )
    assert split_sql_statements(script) == [
        "INSERT INTO q SELECT 'a;b' FROM t",
        "-- line; comment\nINSERT INTO r SELECT 'it''s; ok'",
        "/* block; comment */ SELECT 1",
        "# hash; comment\nSELECT 2",
    ]


def test_split_handles_quoted_identifiers_and_empty_statements():
    assert split_sql_statements('SELECT "a;b"; ; SELECT `c;d`;') == [
        'SELECT "a;b"',
        "SELECT `c;d`",
    ]
    assert split_sql_statements("  ") == []
    assert split_sql_statements("SELECT 1;") == ["SELECT 1"]


def test_split_hash_temp_table_is_not_a_comment():
    # The # introduces an MSSQL temp table, so the semicolon after it still
    # splits the script normally.
    statements = split_sql_statements("SELECT * FROM #tmp; SELECT * FROM #var")
    assert statements == ["SELECT * FROM #tmp", "SELECT * FROM #var"]


def test_split_unterminated_block_comment():
    statements = split_sql_statements("SELECT 1; /* never closed")
    assert statements == ["SELECT 1", "/* never closed"]


# ---------------------------------------------------------------------------
# Single-statement parsing / classification
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("sql", "kind"),
    [
        ("BEGIN", AuditKind.BEGIN),
        ("BEGIN WORK", AuditKind.BEGIN),
        ("COMMIT", AuditKind.COMMIT),
        ("ROLLBACK", AuditKind.ROLLBACK),
        ("ROLLBACK TO SAVEPOINT z", AuditKind.ROLLBACK),
        ("SAVEPOINT z", AuditKind.SAVEPOINT),
        ("RELEASE SAVEPOINT z", AuditKind.RELEASE),
        ("RELEASE z", AuditKind.RELEASE),
        (
            "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
            AuditKind.SET,
        ),
        ("CREATE TEMPORARY TABLE stage AS SELECT * FROM s", AuditKind.CREATE),
        ("CREATE TABLE perm AS SELECT * FROM s", AuditKind.CREATE),
        ("DROP TABLE stage", AuditKind.DROP),
        ("TRUNCATE TABLE p", AuditKind.TRUNCATE),
        ("INSERT INTO q SELECT * FROM stage", AuditKind.INSERT),
        ("SELECT * FROM p", AuditKind.SELECT),
    ],
)
def test_parse_statement_classification(sql, kind):
    assert parse_statement(sql, index=0).kind is kind


def test_parse_unknown_statement():
    entry = parse_statement("FOO BAR LONG QUERY WITH MANY TOKENS")
    assert entry.kind is AuditKind.UNKNOWN
    assert entry.tables == ()


def test_savepoint_names_extracted_without_literal_quotes():
    assert parse_statement("SAVEPOINT z").savepoint == "z"
    assert parse_statement("RELEASE SAVEPOINT z").savepoint == "z"
    assert parse_statement("RELEASE z").savepoint == "z"
    assert parse_statement("ROLLBACK TO SAVEPOINT z").savepoint == "z"
    assert parse_statement("ROLLBACK").savepoint is None


def test_isolation_levels_detected():
    rc = parse_statement("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
    assert rc.isolation is IsolationLevel.READ_COMMITTED
    rr = parse_statement("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
    assert rr.isolation is IsolationLevel.REPEATABLE_READ


def test_isolation_regex_fallback():
    from sql_metadata.audit import _isolation_from_set

    node = sqlglot.parse_one("SET x = 1")
    isolation = _isolation_from_set(
        node, "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
    )
    assert isolation is IsolationLevel.READ_COMMITTED
    assert _isolation_from_set(node, "SET x = 1") is None


def test_temporary_flag_and_table_sources():
    temp = parse_statement("CREATE TEMPORARY TABLE stage AS SELECT * FROM source_a")
    assert temp.temporary is True
    assert temp.target == "stage"
    assert temp.sources == ("source_a",)
    assert temp.tables == ("stage", "source_a")

    permanent = parse_statement("CREATE TABLE p AS SELECT * FROM stage")
    assert permanent.temporary is False

    insert = parse_statement("INSERT INTO q SELECT * FROM stage")
    assert insert.target == "q"
    assert insert.sources == ("stage",)


def test_control_statements_carry_no_tables_and_stay_silent(caplog):
    with caplog.at_level(logging.ERROR):
        for sql in ("BEGIN", "COMMIT", "SAVEPOINT z", "RELEASE SAVEPOINT z"):
            entry = parse_statement(sql)
            assert entry.tables == ()
            assert entry.sources == ()
    assert not caplog.records


def test_isolation_text_helper_rejects_unrelated_text():
    assert _isolation_from_text("SERIALIZABLE") is None


def test_savepoint_helper_handles_non_literal_and_rollback_without_marker():
    from sqlglot import exp

    node = sqlglot.parse_one("SAVEPOINT z")
    # Force the non-Literal branch by stubbing the expression node.
    node.set("expression", None)
    assert _savepoint_name(node) is None
    node.set("expression", exp.Identifier(this="z"))
    assert _savepoint_name(node) == "z"
    plain_rollback = sqlglot.parse_one("ROLLBACK")
    assert _savepoint_name(plain_rollback) is None


def test_classify_kind_unknown_branches():
    from sqlglot import exp

    command = exp.Command(this="VACUUM")
    assert _classify_kind(command) is AuditKind.UNKNOWN
    assert _classify_kind(exp.Literal.number(1)) is AuditKind.UNKNOWN


def test_isolation_override_skips_when_level_already_present():
    entry = parse_statement("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
    # An explicit override that parses to a level is applied; a bogus label
    # leaves the entry untouched.
    from sql_metadata.audit import AuditLog as _AuditLog

    overridden = _AuditLog._with_isolation(entry, "REPEATABLE READ")
    assert overridden.isolation is IsolationLevel.REPEATABLE_READ
    untouched = _AuditLog._with_isolation(entry, "NOT A LEVEL")
    assert untouched.isolation is IsolationLevel.READ_COMMITTED


def test_audit_log_ignores_non_string_happens_before_items():
    log = AuditLog(
        [
            {
                "id": "r1",
                "session": "s",
                "sql": "SELECT 1",
                "happens_before": [1, None, True],
            }
        ]
    )
    assert log.explicit_edges == ()


def test_rows_without_session_are_parsed_but_not_linked():
    log = AuditLog(
        [
            {"id": "r1", "sql": "BEGIN; COMMIT"},
            {"id": "r2", "session": "s", "sql": "SELECT 1; SELECT 2"},
        ]
    )
    kinds = [entry.kind for entry in log.entries]
    assert kinds == [
        AuditKind.BEGIN,
        AuditKind.COMMIT,
        AuditKind.SELECT,
        AuditKind.SELECT,
    ]
    # Only the two session-tagged SELECTs get a within-session edge; the
    # session-less BEGIN/COMMIT pair cannot be linked.
    assert len(log.ordering_edges) == 1
    later = log.entries[log.ordering_edges[0][1]]
    assert later.session == "s"


# ---------------------------------------------------------------------------
# Audit log structure and ordering
# ---------------------------------------------------------------------------


def test_audit_log_splits_multi_statement_rows_and_tags_entries():
    log = AuditLog(
        [
            {
                "id": "r1",
                "session": "s",
                "transaction": "t",
                "sql": "BEGIN; SELECT * FROM p",
            }
        ]
    )
    entries = log.entries
    assert [entry.kind for entry in entries] == [AuditKind.BEGIN, AuditKind.SELECT]
    assert all(entry.session == "s" and entry.transaction == "t" for entry in entries)
    assert all(entry.row_id == "r1" for entry in entries)
    assert entries[1].index == 1


def test_within_session_edges_derived_from_order():
    log = build_log("inner")
    # Every consecutive pair within a session is an ordering edge.
    edge_set = set(log.ordering_edges)
    a_entries = log.entries_of_session(WRITER)
    b_entries = log.entries_of_session(READER)
    assert (a_entries[0].index, a_entries[1].index) in edge_set
    assert (b_entries[0].index, b_entries[1].index) in edge_set
    # The explicit cross-session edge is present and exposed separately.
    commit_index = next(
        entry.index for entry in a_entries if entry.kind is AuditKind.COMMIT
    )
    audit2_index = next(entry.index for entry in b_entries if entry.target == "audit_2")
    assert (commit_index, audit2_index) in set(log.explicit_edges)


def test_unknown_happens_before_id_raises():
    with pytest.raises(ValueError, match="unknown row id"):
        AuditLog(
            [
                {
                    "id": "r1",
                    "session": "s",
                    "sql": "SELECT 1",
                    "happens_before": ["ghost"],
                }
            ]
        )


def test_explicit_edge_anchors_row_end_to_next_row_start():
    # Each row expands to several statements.  "a-commit completes before
    # b-audit starts" must join a-commit's LAST entry (COMMIT) to b-audit's
    # FIRST entry — anchoring both at row beginnings would leave the commit
    # unable to reach the later audit.
    log = AuditLog(
        [
            {
                "id": "a-commit",
                "session": "w",
                "transaction": "t1",
                "sql": "BEGIN; INSERT INTO p SELECT * FROM s; COMMIT",
            },
            {
                "id": "b-audit",
                "session": "r",
                "transaction": "t2",
                "sql": "BEGIN; INSERT INTO audit_1 SELECT * FROM p",
                "happens_before": ["a-commit"],
            },
        ]
    )
    writer = log.entries_of_session("w")
    reader = log.entries_of_session("r")
    commit_index = next(
        entry.index for entry in writer if entry.kind is AuditKind.COMMIT
    )
    reader_begin_index = reader[0].index
    assert log.explicit_edges == ((commit_index, reader_begin_index),)

    result = SessionAuditAnalyzer(log).analyze(
        writer_session="w",
        reader_session="r",
        permanent_table="p",
        baseline_source="seed_old",
        isolation=IsolationLevel.READ_COMMITTED,
    )
    assert result.shared_ordering == ("a-commit before b-audit",)
    assert result.unknown_orderings == ()


def test_explicit_isolation_override_and_non_string_fields():
    log = AuditLog(
        [
            {
                "id": "r1",
                "session": "s",
                "transaction": "t",
                "sql": "SELECT * FROM p",
                "isolation": "REPEATABLE READ",
                "happens_before": 123,
            }
        ]
    )
    assert log.entries[0].isolation is IsolationLevel.REPEATABLE_READ


# ---------------------------------------------------------------------------
# Replay mechanics (savepoint stack)
# ---------------------------------------------------------------------------


def _replay(sqls: list[str]) -> _TransactionReplay:
    entries = tuple(parse_statement(sql, index=index) for index, sql in enumerate(sqls))
    replay = _TransactionReplay({"p": "seed_old"})
    replay.run(entries)
    return replay


def test_replay_inner_rollback_keeps_source_b_binding():
    replay = _replay(
        [
            "CREATE TEMPORARY TABLE stage AS SELECT * FROM source_a",
            "SAVEPOINT z",
            "CREATE OR REPLACE TEMPORARY TABLE stage AS SELECT * FROM source_b",
            "INSERT INTO q SELECT * FROM stage",
            "SAVEPOINT z",
            "CREATE OR REPLACE TEMPORARY TABLE stage AS SELECT * FROM source_c",
            "INSERT INTO r SELECT * FROM stage",
            "ROLLBACK TO SAVEPOINT z",
            "RELEASE SAVEPOINT z",
            "TRUNCATE TABLE p",
            "INSERT INTO p SELECT * FROM stage",
            "COMMIT",
        ]
    )
    # The source_c rebinding and the r write are rolled back; the surviving
    # stage binding is source_b, so q survives and p derives from source_b.
    assert replay.temporary == {"stage": "source_b"}
    assert replay.committed == {"p": "source_b", "q": "source_b"}
    assert replay.origin_of("stage") == 2


def test_replay_outer_rollback_restores_source_a_binding():
    replay = _replay(
        [
            "CREATE TEMPORARY TABLE stage AS SELECT * FROM source_a",
            "SAVEPOINT z",
            "CREATE OR REPLACE TEMPORARY TABLE stage AS SELECT * FROM source_b",
            "INSERT INTO q SELECT * FROM stage",
            "SAVEPOINT z",
            "CREATE OR REPLACE TEMPORARY TABLE stage AS SELECT * FROM source_c",
            "INSERT INTO r SELECT * FROM stage",
            "ROLLBACK TO SAVEPOINT z",
            "RELEASE SAVEPOINT z",
            "ROLLBACK TO SAVEPOINT z",
            "TRUNCATE TABLE p",
            "INSERT INTO p SELECT * FROM stage",
            "COMMIT",
        ]
    )
    # Rolling back to the outer z discards source_b and the q write too;
    # the surviving stage binding is source_a.
    assert replay.temporary == {"stage": "source_a"}
    assert replay.committed == {"p": "source_a"}


def test_replay_unknown_savepoint_names_are_noops():
    replay = _replay(
        [
            "CREATE TEMPORARY TABLE stage AS SELECT * FROM source_a",
            "SAVEPOINT z",
            # Mismatched names walk the whole stack without matching and
            # therefore leave the existing snapshot and binding intact.
            "ROLLBACK TO SAVEPOINT missing",
            "RELEASE SAVEPOINT missing",
            "COMMIT",
        ]
    )
    assert replay.temporary == {"stage": "source_a"}
    assert replay.committed == {"p": "seed_old"}


def test_replay_inserts_into_a_temporary_table_rebind_it():
    replay = _replay(
        [
            "CREATE TEMPORARY TABLE stage AS SELECT * FROM source_a",
            "INSERT INTO stage SELECT * FROM source_b",
            "COMMIT",
        ]
    )
    # An INSERT whose target is a temporary table rebinds that table rather
    # than recording a permanent write; the p baseline is untouched.
    assert replay.temporary == {"stage": "source_b"}
    assert replay.committed == {"p": "seed_old"}


def test_replay_handler_guards_ignore_missing_names_and_targets():
    replay = _TransactionReplay()
    base = dict(
        index=0,
        session="s",
        transaction="t",
        sql="",
        tables=(),
        target=None,
        sources=(),
        temporary=False,
        savepoint=None,
        isolation=None,
        row_id=None,
    )
    replay._handle_savepoint(LogEntry(kind=AuditKind.SAVEPOINT, **base))
    replay._handle_rollback(LogEntry(kind=AuditKind.ROLLBACK, **base))
    replay._handle_release(LogEntry(kind=AuditKind.RELEASE, **base))
    replay._handle_temp_create(LogEntry(kind=AuditKind.CREATE, **base))
    replay._handle_insert(LogEntry(kind=AuditKind.INSERT, **base))
    replay._handle_truncate(LogEntry(kind=AuditKind.TRUNCATE, **base))
    assert replay.temporary == {}


def test_replay_create_temporary_from_temporary_source_resolves_binding():
    replay = _replay(
        [
            "CREATE TEMPORARY TABLE stage AS SELECT * FROM source_a",
            "CREATE TEMPORARY TABLE other AS SELECT * FROM stage",
            "COMMIT",
        ]
    )
    assert replay.temporary == {
        "stage": "source_a",
        "other": "source_a",
    }


# ---------------------------------------------------------------------------
# Full scenario: audit pairings under both isolation levels
# ---------------------------------------------------------------------------


def _pair_map(result) -> dict[str, list[AuditPossibility]]:
    return {possibility.name: possibility for possibility in result.possibilities}


def test_read_committed_inner_branch_pairings():
    result = analyze("inner", IsolationLevel.READ_COMMITTED)
    by_name = _pair_map(result)
    assert set(by_name) == {"old-then-new", "both-new"}

    old_then_new = by_name["old-then-new"]
    assert old_then_new.final_source_of_p == "source_b"
    assert old_then_new.committed_writes == {"p": "source_b", "q": "source_b"}
    audit_1, audit_2 = old_then_new.pairs
    assert audit_1.audit_table == "audit_1"
    assert audit_1.permanent == {"p": "seed_old"}
    assert audit_1.temporary == {}
    assert audit_2.audit_table == "audit_2"
    assert audit_2.temporary == {"stage": "source_other"}

    both_new = by_name["both-new"]
    assert both_new.pairs[0].permanent == {"p": "source_b"}
    # The reader's own stage binding is identical in every possibility.
    assert both_new.pairs[1].temporary == {"stage": "source_other"}


def test_read_committed_outer_branch_uses_source_a():
    result = analyze("outer", IsolationLevel.READ_COMMITTED)
    by_name = _pair_map(result)
    assert by_name["old-then-new"].final_source_of_p == "source_a"
    assert by_name["old-then-new"].committed_writes == {"p": "source_a"}
    assert by_name["both-new"].pairs[0].permanent == {"p": "source_a"}


def test_repeatable_read_forbids_old_then_new_split():
    result = analyze("inner", IsolationLevel.REPEATABLE_READ)
    assert {possibility.name for possibility in result.possibilities} == {
        "both-old",
        "both-new",
    }
    # Every permanent-table read inside one possibility must share the same
    # version — a single read view cannot observe old then new.
    for possibility in result.possibilities:
        permanent_versions = [
            version for pair in possibility.pairs for version in pair.permanent.values()
        ]
        assert set(permanent_versions) in [{"seed_old"}, {"source_b"}]
    by_name = _pair_map(result)
    assert by_name["both-old"].pairs[0].permanent == {"p": "seed_old"}
    assert by_name["both-new"].pairs[0].permanent == {"p": "source_b"}
    assert "old-then-new" not in by_name


def test_ordering_uses_only_known_edges():
    result = analyze("inner", IsolationLevel.READ_COMMITTED)
    # The single guaranteed cross-session ordering: writer commit before the
    # reader's second query.
    assert result.shared_ordering == ("a-commit before b-audit-2",)
    # The first audit query vs the commit has no edge in either direction.
    assert result.unknown_orderings == ("b-audit-1 vs a-commit",)


def test_ordering_report_records_reverse_edges():
    # When the log declares the audit completed before the writer commit,
    # that direction must be reported verbatim rather than flipped.
    log = AuditLog(
        [
            {
                "id": "a-begin",
                "session": WRITER,
                "transaction": "ta",
                "sql": "BEGIN",
            },
            {
                "id": "a-commit",
                "session": WRITER,
                "transaction": "ta",
                "sql": "COMMIT",
                "happens_before": ["b-audit"],
            },
            {
                "id": "b-audit",
                "session": READER,
                "transaction": "tb",
                "sql": "INSERT INTO audit_1 SELECT * FROM p",
            },
        ]
    )
    result = SessionAuditAnalyzer(log).analyze(
        writer_session=WRITER,
        reader_session=READER,
        permanent_table="p",
        baseline_source="seed_old",
        isolation=IsolationLevel.READ_COMMITTED,
    )
    assert result.shared_ordering == ("b-audit before a-commit",)
    assert result.unknown_orderings == ()


def test_effective_temp_binding_evidence_keeps_sessions_apart():
    result = analyze("inner", IsolationLevel.READ_COMMITTED)
    evidence = {
        (item.session, item.name): item for item in result.effective_temp_bindings
    }
    reader_binding = evidence[(READER, "stage")]
    assert reader_binding.source == "source_other"
    assert reader_binding.evidence_entry_index is not None

    writer_binding = evidence[(WRITER, "stage")]
    assert writer_binding.source == "source_b"
    # The surviving binding points at the source_b CREATE entry, not the
    # rolled-back source_c one.
    origin_entry = result.entries[writer_binding.evidence_entry_index]
    assert origin_entry.sources == ("source_b",)


def test_analysis_notes_cover_isolation_semantics():
    rc = analyze("inner", IsolationLevel.READ_COMMITTED)
    assert all(
        "happens_before" in note or "session-scoped" in note for note in rc.notes
    )
    rr = analyze("inner", IsolationLevel.REPEATABLE_READ)
    assert any("read view" in note for note in rr.notes)


def test_analyze_audit_log_convenience_wrapper():
    rows = writer_rows("inner") + reader_rows()
    result = analyze_audit_log(
        rows,
        writer_session=WRITER,
        reader_session=READER,
        isolation=IsolationLevel.REPEATABLE_READ,
    )
    assert result.isolation is IsolationLevel.REPEATABLE_READ
    assert len(result.possibilities) == 2


def test_analysis_without_two_audit_writes_returns_no_possibilities():
    log = AuditLog(
        writer_rows("inner")
        + [
            {
                "id": "b-only",
                "session": READER,
                "transaction": "tb",
                "sql": "INSERT INTO audit_1 SELECT * FROM p",
                "happens_before": ["a-commit"],
            }
        ]
    )
    result = SessionAuditAnalyzer(log).analyze(
        writer_session=WRITER,
        reader_session=READER,
        permanent_table="p",
        baseline_source="seed_old",
        isolation=IsolationLevel.READ_COMMITTED,
    )
    assert result.possibilities == ()
