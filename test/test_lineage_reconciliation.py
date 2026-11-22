"""Lineage analysis for the supplier reconciliation query.

The query first builds ``j`` from ``p FULL OUTER JOIN q USING(id)`` and then
applies ``j NATURAL LEFT JOIN r`` keeping only rows where ``r.region`` is not
null.  The region table ``r`` exists in two deployments behind the same SQL
text:

* ``r(id, region)``            — the natural join only shares ``id``;
* ``r(id, status, region)``    — the natural join shares ``id`` *and*
  ``status``, so an extra equality predicate is applied implicitly.

Sample data::

    p = (1, P1, S1), (3, P3, P3)
    q = (2, Q2, Q2), (3, Q3, Q3)

``r(id, region)`` keeps all three join rows: id 1 is p-only, id 2 is q-only
and id 3 is matched on both sides.  ``r(id, status, region)`` keeps only
id 3: for id 1 the q side is missing so ``j.status`` is the null produced by
the outer join, and for id 2 ``j.status`` is ``Q2`` which differs from
``r.status = S2``.  Neither null- nor mismatch-based evidence may be treated
as a value source for the output.

Only the ordinary :class:`~sql_metadata.Parser` ``tables`` / ``columns`` /
``columns_dict`` / ``subqueries`` properties are used; the lineage
interpretation itself is encoded in :data:`LINEAGE` and cross-checked against
an in-memory SQLite database.
"""

import sqlite3

import pytest

from sql_metadata import Parser

# ---------------------------------------------------------------------------
# Query texts — identical SQL for both deployments of r
# ---------------------------------------------------------------------------

RECONCILIATION_QUERY = """
SELECT j.id, j.p_key, j.q_key, j.p_status, j.status, r.region
FROM (
    SELECT p.id AS id, p.p_key, q.q_key,
           p.status AS p_status, q.status AS status
    FROM p
    FULL OUTER JOIN q USING(id)
) j
NATURAL LEFT JOIN r
WHERE r.region IS NOT NULL
"""

# Same pipeline, but the outer projection only exposes the last id.
NARROWED_QUERY = """
SELECT t.id
FROM (
    SELECT j.id
    FROM (
        SELECT p.id AS id, p.p_key, q.q_key,
               p.status AS p_status, q.status AS status
        FROM p FULL OUTER JOIN q USING(id)
    ) j
    NATURAL LEFT JOIN r
    WHERE r.region IS NOT NULL
) t
WHERE t.id = 3
"""

# ---------------------------------------------------------------------------
# Sample data
# ---------------------------------------------------------------------------

P_ROWS = [
    (1, "P1", "S1"),
    (3, "P3", "P3"),
]
Q_ROWS = [
    (2, "Q2", "Q2"),
    (3, "Q3", "Q3"),
]
# r(id, region) — region values are the same in both deployments.
R_ID_REGION_ROWS = [
    (1, "east"),
    (2, "south"),
    (3, "west"),
]
# r(id, status, region) — statuses S1, S2, Q3 for ids 1, 2, 3.
R_ID_STATUS_REGION_ROWS = [
    (1, "S1", "east"),
    (2, "S2", "south"),
    (3, "Q3", "west"),
]

REGION_ID_ONLY = "id_region"
REGION_WITH_STATUS = "id_status_region"

# ---------------------------------------------------------------------------
# Static lineage interpretation
#
# For every deployment the output id can only be fed by p.id or q.id (the
# COALESCE'd USING join column inside j).  r never supplies an output value:
# r.id / r.status are natural-join matching evidence and r.region only decides
# whether a row survives.  Evidence is kept per deployment so that a row that
# exists in one deployment can never inherit the other deployment's proof.
# ---------------------------------------------------------------------------

LINEAGE = {
    REGION_ID_ONLY: {
        "natural_join_columns": ("id",),
        "matching_evidence": [{"table": "r", "columns": ("id",)}],
        "row_filter": {"table": "r", "column": "region", "predicate": "IS NOT NULL"},
        "rows": {
            1: {"id_sources": ("p",)},
            2: {"id_sources": ("q",)},
            3: {"id_sources": ("p", "q")},
        },
        "rejected_rows": {},
    },
    REGION_WITH_STATUS: {
        "natural_join_columns": ("id", "status"),
        "matching_evidence": [{"table": "r", "columns": ("id", "status")}],
        "row_filter": {"table": "r", "column": "region", "predicate": "IS NOT NULL"},
        "rows": {
            3: {"id_sources": ("p", "q")},
        },
        "rejected_rows": {
            1: "j.status is NULL (q side missing); NULL never equals r.status S1",
            2: "j.status Q2 does not equal r.status S2",
        },
    },
}

VALUE_SOURCE_TABLES = ("p", "q")


# ---------------------------------------------------------------------------
# SQLite helpers — runtime semantics
# ---------------------------------------------------------------------------


def _sqlite_supports_full_outer_join() -> bool:
    connection = sqlite3.connect(":memory:")
    try:
        connection.execute("CREATE TABLE a(x INT)")
        connection.execute("CREATE TABLE b(x INT)")
        connection.execute("SELECT a.x FROM a FULL OUTER JOIN b ON a.x = b.x")
    except sqlite3.OperationalError:
        return False
    return True


SUPPORTS_FULL_OUTER_JOIN = _sqlite_supports_full_outer_join()
pytestmark = pytest.mark.skipif(
    not SUPPORTS_FULL_OUTER_JOIN,
    reason="sqlite3 in this runtime does not support FULL OUTER JOIN",
)


def _reconciliation_sql(region_table: str) -> str:
    # SQLite's FULL OUTER JOIN ... USING keeps the USING column NULL on the
    # unmatched side instead of COALESCE-ing it as the SQL standard requires.
    # Emulate standard USING(id) semantics with an explicit COALESCE so the
    # natural join against r behaves as specified.
    return f"""
SELECT j.id, j.p_key, j.q_key, j.p_status, j.status, {region_table}.region
FROM (
    SELECT COALESCE(p.id, q.id) AS id, p.p_key, q.q_key,
           p.status AS p_status, q.status AS status
    FROM p FULL OUTER JOIN q ON p.id = q.id
) j
NATURAL LEFT JOIN {region_table}
WHERE {region_table}.region IS NOT NULL
ORDER BY j.id
"""


def _narrowed_sql(region_table: str) -> str:
    return f"""
SELECT t.id
FROM (
    SELECT j.id
    FROM (
        SELECT COALESCE(p.id, q.id) AS id, p.p_key, q.q_key,
               p.status AS p_status, q.status AS status
        FROM p FULL OUTER JOIN q ON p.id = q.id
    ) j
    NATURAL LEFT JOIN {region_table}
    WHERE {region_table}.region IS NOT NULL
) t
WHERE t.id = 3
"""


def _build_connection(region_kind: str) -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    cur = connection.cursor()
    cur.execute("CREATE TABLE p(id INT, p_key TEXT, status TEXT)")
    cur.execute("CREATE TABLE q(id INT, q_key TEXT, status TEXT)")
    cur.executemany("INSERT INTO p VALUES (?, ?, ?)", P_ROWS)
    cur.executemany("INSERT INTO q VALUES (?, ?, ?)", Q_ROWS)
    if region_kind == REGION_ID_ONLY:
        cur.execute("CREATE TABLE r1(id INT, region TEXT)")
        cur.executemany("INSERT INTO r1 VALUES (?, ?)", R_ID_REGION_ROWS)
    else:
        cur.execute("CREATE TABLE r2(id INT, status TEXT, region TEXT)")
        cur.executemany(
            "INSERT INTO r2 VALUES (?, ?, ?)", R_ID_STATUS_REGION_ROWS
        )
    return connection


# ---------------------------------------------------------------------------
# Static sql-metadata metadata — ordinary columns / tables usage
# ---------------------------------------------------------------------------


def test_tables_list_all_three_inputs() -> None:
    parser = Parser(RECONCILIATION_QUERY)
    assert parser.tables == ["p", "q", "r"]
    # The derived table j is exposed as a subquery, not as a base table.
    assert list(parser.subqueries) == ["j"]
    assert "FULL OUTER JOIN q USING (id)" in parser.subqueries["j"]


def test_columns_separate_value_sources_from_join_and_filter_evidence() -> None:
    parser = Parser(RECONCILIATION_QUERY)
    columns = parser.columns
    # Output value sources inside j are qualified to p and q.
    assert "p.id" in columns
    assert "p.p_key" in columns
    assert "p.status" in columns
    assert "q.q_key" in columns
    assert "q.status" in columns
    # r.region only gates row survival via the WHERE predicate.
    assert "r.region" in columns
    assert parser.columns_dict["where"] == ["r.region"]
    # The bare USING column is join evidence, not attributed to either side.
    assert parser.columns_dict["join"] == ["id"]
    # NATURAL JOIN spells out no predicate, so r.id and r.status never appear
    # in the SQL text and the library cannot see either deployment of r.
    assert "r.id" not in columns
    assert "r.status" not in columns


def test_static_metadata_is_identical_for_both_deployments() -> None:
    # The deployment of r changes only its schema and data, never the SQL
    # text, so a static parse produces one fixed answer for both versions.
    parsed_once = Parser(RECONCILIATION_QUERY)
    parsed_again = Parser(RECONCILIATION_QUERY)
    assert list(parsed_once.tables) == list(parsed_again.tables)
    assert list(parsed_once.columns) == list(parsed_again.columns)
    assert dict(parsed_once.columns_dict) == dict(parsed_again.columns_dict)


# ---------------------------------------------------------------------------
# Lineage model invariants
# ---------------------------------------------------------------------------


def test_output_id_is_ever_sourced_from_p_or_q_never_from_r() -> None:
    for deployment, model in LINEAGE.items():
        for row_id, row in model["rows"].items():
            assert set(row["id_sources"]).issubset(set(VALUE_SOURCE_TABLES))
            assert "r" not in row["id_sources"]
            # Every surviving row must be backed by natural-join evidence.
            assert model["matching_evidence"][0]["table"] == "r"
        # r only ever appears as matching evidence or as the row filter.
        evidence_tables = {item["table"] for item in model["matching_evidence"]}
        assert evidence_tables == {"r"}
        assert model["row_filter"]["table"] == "r"


def test_lineage_evidence_is_not_mixed_across_deployments() -> None:
    id_only = LINEAGE[REGION_ID_ONLY]
    with_status = LINEAGE[REGION_WITH_STATUS]

    # The three-row result and the one-row result are separate worlds.
    assert set(id_only["rows"]) == {1, 2, 3}
    assert set(with_status["rows"]) == {3}
    assert set(with_status["rejected_rows"]) == {1, 2}

    # The shared id 3 has the same p/q sources in both deployments...
    assert id_only["rows"][3]["id_sources"] == ("p", "q")
    assert with_status["rows"][3]["id_sources"] == ("p", "q")
    # ...but id 1/2 evidence from the id-only deployment must not leak into
    # the status-aware deployment where those rows are rejected.
    assert 1 not in with_status["rows"]
    assert 2 not in with_status["rows"]
    assert not id_only["rejected_rows"]
    # Distinct natural-join column sets are what separate the two worlds.
    assert id_only["natural_join_columns"] == ("id",)
    assert with_status["natural_join_columns"] == ("id", "status")


# ---------------------------------------------------------------------------
# Runtime semantics — r(id, region)
# ---------------------------------------------------------------------------


def test_id_only_region_table_keeps_all_three_rows() -> None:
    connection = _build_connection(REGION_ID_ONLY)
    rows = connection.execute(_reconciliation_sql("r1")).fetchall()
    # id, p_key, q_key, p_status, status, region
    assert [row[0] for row in rows] == [1, 2, 3]
    # id 1 is p-only: q columns are outer-join nulls.
    assert rows[0][1] == "P1" and rows[0][2] is None and rows[0][4] is None
    # id 2 is q-only: p columns are outer-join nulls.
    assert rows[1][1] is None and rows[1][2] == "Q2" and rows[1][3] is None
    # id 3 is matched on both sides.
    assert rows[2][1] == "P3" and rows[2][2] == "Q3"


def test_id_only_lineage_matches_runtime_rows() -> None:
    connection = _build_connection(REGION_ID_ONLY)
    runtime_ids = {
        row[0] for row in connection.execute(_reconciliation_sql("r1")).fetchall()
    }
    assert runtime_ids == set(LINEAGE[REGION_ID_ONLY]["rows"])


# ---------------------------------------------------------------------------
# Runtime semantics — r(id, status, region)
# ---------------------------------------------------------------------------


def test_status_aware_region_table_keeps_only_the_double_matched_row() -> None:
    connection = _build_connection(REGION_WITH_STATUS)
    rows = connection.execute(_reconciliation_sql("r2")).fetchall()
    assert [row[0] for row in rows] == [3]
    assert rows[0][4] == "Q3"  # j.status equals r.status Q3


def test_rejected_rows_fail_for_distinct_reasons() -> None:
    connection = _build_connection(REGION_WITH_STATUS)
    join_rows = connection.execute(
        """
        SELECT COALESCE(p.id, q.id) AS id, p.status AS p_status, q.status AS status
        FROM p FULL OUTER JOIN q ON p.id = q.id
        ORDER BY id
        """
    ).fetchall()
    by_id = {row[0]: row for row in join_rows}
    # id 1: the q side is missing, so j.status is an outer-join null.
    assert by_id[1][1] == "S1" and by_id[1][2] is None
    # id 2: j.status is Q2 and therefore not equal to r.status S2.
    assert by_id[2][1] is None and by_id[2][2] == "Q2"
    # id 3: both statuses line up with r.status Q3.
    assert by_id[3][2] == "Q3"


def test_status_aware_lineage_matches_runtime_rows() -> None:
    connection = _build_connection(REGION_WITH_STATUS)
    runtime_ids = {
        row[0] for row in connection.execute(_reconciliation_sql("r2")).fetchall()
    }
    assert runtime_ids == set(LINEAGE[REGION_WITH_STATUS]["rows"])
    assert runtime_ids.isdisjoint(LINEAGE[REGION_WITH_STATUS]["rejected_rows"])


# ---------------------------------------------------------------------------
# Narrowed outer projection — only the last id remains visible
# ---------------------------------------------------------------------------


def test_narrowed_query_still_carries_filter_evidence_in_metadata() -> None:
    parser = Parser(NARROWED_QUERY)
    # Even with t.id as the sole output column, all three inputs and the
    # r.region IS NOT NULL filter remain visible in the static metadata.
    assert parser.tables == ["p", "q", "r"]
    assert "r.region" in parser.columns
    assert "r.region" in parser.columns_dict["where"]
    assert set(parser.subqueries) == {"j", "t"}


def test_narrowed_result_is_identical_but_does_not_prove_earlier_rows() -> None:
    id_only_connection = _build_connection(REGION_ID_ONLY)
    with_status_connection = _build_connection(REGION_WITH_STATUS)

    narrowed_id_only = id_only_connection.execute(
        _narrowed_sql("r1")
    ).fetchall()
    narrowed_with_status = with_status_connection.execute(
        _narrowed_sql("r2")
    ).fetchall()
    # The visible result is indistinguishable: only id 3 in both deployments.
    assert narrowed_id_only == [(3,)]
    assert narrowed_with_status == [(3,)]

    # The full pipelines still differ: three surviving rows versus one, so
    # the shared last row cannot be used as evidence that rows 1 and 2 also
    # matched under the status-aware deployment.
    full_id_only = id_only_connection.execute(
        _reconciliation_sql("r1")
    ).fetchall()
    full_with_status = with_status_connection.execute(
        _reconciliation_sql("r2")
    ).fetchall()
    assert [row[0] for row in full_id_only] == [1, 2, 3]
    assert [row[0] for row in full_with_status] == [3]


def test_static_resolution_is_name_based_not_row_value_lineage() -> None:
    parser = Parser(NARROWED_QUERY)
    # The library resolves t.id through the subquery chain back to p.id by
    # name matching.  That is structural attribution, not proof that every
    # runtime id value originates from p: for the q-only row (id 2) the
    # COALESCE'd USING value comes from q.  Row-level value lineage therefore
    # cannot be read off columns_dict alone.
    assert "p.id" in parser.columns_dict["where"]
