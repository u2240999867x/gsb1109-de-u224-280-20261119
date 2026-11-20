import pytest
import sqlalchemy as sa

from datajudge import WithinRequirement
from datajudge.constraints import aliases
from datajudge.constraints.aliases import (
    AliasDependencyConfig,
    analyze_alias_dependency,
)

DETAIL = [
    # X's north-region record entered through the namespaced alias chain.
    # It is re-reported verbatim.
    {
        "report_id": 1,
        "source_namespace": "jia",
        "source_code": "07",
        "region": "north",
        "confirmed": True,
    },
    {
        "report_id": 2,
        "source_namespace": "jia",
        "source_code": "07",
        "region": "north",
        "confirmed": True,
    },
    # X's south-region record submitted directly under the canonical id.
    # Also re-reported.
    {
        "report_id": 3,
        "source_namespace": "canonical",
        "source_code": "X",
        "region": "south",
        "confirmed": True,
    },
    {
        "report_id": 4,
        "source_namespace": "canonical",
        "source_code": "X",
        "region": "south",
        "confirmed": True,
    },
    # Y has the same code 07 in the other registration system, but a different
    # region. It is normal and independent.
    {
        "report_id": 5,
        "source_namespace": "yi",
        "source_code": "07",
        "region": "east",
        "confirmed": True,
    },
    # A different store uses the approved alternative spelling of south.
    {
        "report_id": 6,
        "source_namespace": "canonical",
        "source_code": "W",
        "region": "south ",
        "confirmed": True,
    },
    {
        "report_id": 7,
        "source_namespace": "canonical",
        "source_code": "W",
        "region": "South",
        "confirmed": True,
    },
    # Missing region: unknown, never treated as a known region.
    {
        "report_id": 8,
        "source_namespace": "canonical",
        "source_code": "X",
        "region": None,
        "confirmed": True,
    },
]

ALIASES = [
    # Jia's 07 points at an old Jia code, which in turn points at X.
    {
        "source_namespace": "jia",
        "alias": "07",
        "target_namespace": "jia",
        "target_code": "OLD-07",
    },
    {
        "source_namespace": "jia",
        "alias": "OLD-07",
        "target_namespace": "canonical",
        "target_code": "X",
    },
    # Yi's 07 is an independent edge to Y.
    {
        "source_namespace": "yi",
        "alias": "07",
        "target_namespace": "canonical",
        "target_code": "Y",
    },
]

EQUIVALENCE = [
    {"region": "north", "canonical_region": "north"},
    {"region": "south", "canonical_region": "south"},
    {"region": "south ", "canonical_region": "south"},
    {"region": "South", "canonical_region": "south"},
    {"region": "east", "canonical_region": "east"},
]


def _config(**kwargs):
    return AliasDependencyConfig(**kwargs)


def test_namespaced_chain_and_direct_evidence_are_both_reported():
    report = analyze_alias_dependency(DETAIL, ALIASES, EQUIVALENCE)

    assert not report.satisfied
    assert [conflict.store for conflict in report.conflicts] == [
        ("canonical", "X"),
    ]
    conflict = report.conflicts[0]
    assert conflict.regions == ("north", "south")
    north, south = conflict.evidence
    assert north.source_namespace == "jia"
    assert north.source_code == "07"
    assert north.raw_region == "north"
    assert north.alias_path == (
        ("jia", "07"),
        ("jia", "OLD-07"),
        ("canonical", "X"),
    )
    assert north.row["report_id"] in {1, 2}
    assert south.source_namespace == "canonical"
    assert south.source_code == "X"
    assert south.raw_region == "south"
    assert south.row["report_id"] in {3, 4}
    assert all(evidence.store == ("canonical", "X") for evidence in conflict.evidence)


def test_reports_do_not_add_relations_and_y_is_independent():
    report = analyze_alias_dependency(DETAIL, ALIASES, EQUIVALENCE)
    relations = {
        (relation.store, relation.region): relation for relation in report.relations
    }

    assert relations[(("canonical", "X"), "north")].candidate_count == 2
    assert relations[(("canonical", "X"), "south")].candidate_count == 2
    assert relations[(("canonical", "Y"), "east")].candidate_count == 1
    assert ("canonical", "Y") not in {conflict.store for conflict in report.conflicts}

    # Re-reporting the exact same rows adds no new relation.
    duplicated = DETAIL + [dict(DETAIL[0]), dict(DETAIL[2])]
    duplicated_report = analyze_alias_dependency(duplicated, ALIASES, EQUIVALENCE)
    duplicated_relations = {
        (relation.store, relation.region) for relation in duplicated_report.relations
    }
    assert duplicated_relations == set(relations)


def test_approved_spellings_merge_but_keep_original_values():
    report = analyze_alias_dependency(DETAIL, ALIASES, EQUIVALENCE)
    relations = {
        (relation.store, relation.region): relation for relation in report.relations
    }

    relation = relations[(("canonical", "W"), "south")]
    assert relation.raw_regions == ("South", "south ")
    assert relation.candidate_count == 2
    assert len(relation.evidence) == 1
    assert relation.evidence[0].raw_region in {"South", "south "}
    assert ("canonical", "W") not in {conflict.store for conflict in report.conflicts}


def test_equivalence_chains_merge_transitively():
    equivalence = [
        {"region": "north", "canonical_region": "north"},
        {"region": "N", "canonical_region": "north"},
        {"region": "north.", "canonical_region": "N"},
        {"region": "south", "canonical_region": "south"},
        {"region": "east", "canonical_region": "east"},
    ]
    detail = [
        {
            "report_id": 1,
            "source_namespace": "canonical",
            "source_code": "Q",
            "region": "north.",
            "confirmed": True,
        },
        {
            "report_id": 2,
            "source_namespace": "canonical",
            "source_code": "Q",
            "region": "N",
            "confirmed": True,
        },
    ]

    report = analyze_alias_dependency(detail, [], equivalence)

    assert report.satisfied
    relation = report.relations[0]
    assert relation.region == "north"
    assert relation.raw_regions == ("N", "north.")


def test_unapproved_spelling_stays_a_separate_region():
    report = analyze_alias_dependency(DETAIL, ALIASES, EQUIVALENCE[:2])
    relations = {
        (relation.store, relation.region): relation for relation in report.relations
    }

    assert (("canonical", "W"), "south ") in relations
    assert (("canonical", "W"), "South") in relations
    conflict = next(
        conflict
        for conflict in report.conflicts
        if conflict.store == ("canonical", "W")
    )
    assert conflict.regions == ("South", "south ")


def test_missing_region_is_reported_as_unknown():
    report = analyze_alias_dependency(DETAIL, ALIASES, EQUIVALENCE)

    assert len(report.unknown_regions) == 1
    unknown = report.unknown_regions[0]
    assert unknown.source_namespace == "canonical"
    assert unknown.source_code == "X"
    assert unknown.raw_region is None
    assert unknown.canonical_region is None
    assert unknown.alias_path == (("canonical", "X"),)
    assert ("canonical", "X") not in {
        conflict.store
        for conflict in report.conflicts
        if (conflict.store, "unknown")
        in {(conflict.store, region) for region in conflict.regions}
    }


def test_multi_target_alias_is_unconfirmable_not_a_conflict():
    aliases = [
        *ALIASES,
        {
            "source_namespace": "jia",
            "alias": "OLD-07",
            "target_namespace": "canonical",
            "target_code": "Z",
        },
    ]
    report = analyze_alias_dependency(DETAIL, aliases, EQUIVALENCE)

    assert not report.conflicts
    assert report.satisfied
    reasons = {record.reason for record in report.unconfirmable}
    assert reasons == {"ambiguous_alias_target"}
    assert {record.evidence.source_namespace for record in report.unconfirmable} == {
        "jia"
    }
    assert all(record.evidence.raw_region == "north" for record in report.unconfirmable)
    candidates = {
        candidate
        for record in report.unconfirmable
        for candidate in record.candidate_stores
    }
    assert candidates == {("canonical", "X"), ("canonical", "Z")}

    # The direct south record still attributes X normally, so X remains present.
    relations = {(relation.store, relation.region) for relation in report.relations}
    assert (("canonical", "X"), "south") in relations


def test_evidence_selection_is_deterministic_with_more_candidates():
    detail = [
        *DETAIL,
        {
            "report_id": 10,
            "source_namespace": "jia",
            "source_code": "07",
            "region": "north",
            "confirmed": True,
        },
        {
            "report_id": 11,
            "source_namespace": "canonical",
            "source_code": "X",
            "region": "south",
            "confirmed": True,
        },
    ]
    first = analyze_alias_dependency(detail, ALIASES, EQUIVALENCE)
    second = analyze_alias_dependency(list(reversed(detail)), ALIASES, EQUIVALENCE)

    assert first.as_dict()["conflicts"] == second.as_dict()["conflicts"]
    conflict = first.conflicts[0]
    assert tuple(item.row["report_id"] for item in conflict.evidence) == (1, 3)


def test_ordinary_functional_dependency_is_unchanged():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text("CREATE TABLE directory (namespace TEXT, code TEXT, region TEXT)")
        )
        connection.execute(
            sa.text(
                "INSERT INTO directory VALUES "
                "('jia', '07', 'north'), "
                "('canonical', 'X', 'south')"
            )
        )

    requirement = WithinRequirement.from_table(
        db_name="", schema_name=None, table_name="directory"
    )
    requirement.add_functional_dependency_constraint(
        key_columns=["namespace", "code"], value_columns=["region"]
    )

    result = requirement[0].test(engine)
    assert result.outcome


def test_ordinary_multi_key_functional_dependency_still_works():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text("CREATE TABLE multi_key_directory (a INT, b INT, c INT, d INT)")
        )
        connection.execute(
            sa.text("INSERT INTO multi_key_directory VALUES (1, 1, 7, 9), (1, 1, 8, 9)")
        )

    requirement = WithinRequirement.from_table(
        db_name="", schema_name=None, table_name="multi_key_directory"
    )
    requirement.add_functional_dependency_constraint(
        key_columns=["a", "b", "c"], value_columns=["d"]
    )

    result = requirement[0].test(engine)
    assert result.outcome

    with pytest.raises(ValueError, match="exactly two key columns"):
        requirement.add_functional_dependency_constraint(
            key_columns=["a", "b", "c"],
            value_columns=["d"],
            alias_data_source="multi_key_directory",
        )


def test_requirement_api_and_sqlite_execution():
    engine = sa.create_engine("sqlite://")
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "CREATE TABLE details ("
                "report_id INTEGER, source_namespace TEXT, source_code TEXT, "
                "region TEXT, confirmed INTEGER)"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE alias ("
                "source_namespace TEXT, alias TEXT, "
                "target_namespace TEXT, target_code TEXT)"
            )
        )
        connection.execute(
            sa.text(
                "CREATE TABLE region_equivalence (region TEXT, canonical_region TEXT)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO details VALUES "
                "(1, 'jia', '07', 'north', 1), "
                "(2, 'canonical', 'X', 'south', 1), "
                "(3, 'yi', '07', 'east', 1)"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO alias VALUES "
                "('jia', '07', 'jia', 'OLD-07'), "
                "('jia', 'OLD-07', 'canonical', 'X'), "
                "('yi', '07', 'canonical', 'Y')"
            )
        )
        connection.execute(
            sa.text(
                "INSERT INTO region_equivalence VALUES "
                "('north', 'north'), ('south', 'south'), ('east', 'east')"
            )
        )

    requirement = WithinRequirement.from_table(
        db_name="", schema_name=None, table_name="details"
    )
    requirement.add_alias_dependency_constraint(
        "alias",
        region_equivalence_data_source="region_equivalence",
        detail_confirmed_column="confirmed",
        detail_identity_columns=[
            "report_id",
            "source_namespace",
            "source_code",
            "region",
            "confirmed",
        ],
    )
    result = requirement[0].test(engine)

    assert not result.outcome
    assert "conflicting regions" in result.failure_message
    assert "jia" in result.failure_message

    report = requirement[0].last_report
    assert report is not None
    assert len(report.conflicts) == 1
    assert report.conflicts[0].store == ("canonical", "X")
    assert [row.row["report_id"] for row in report.conflicts[0].evidence] == [1, 2]
    assert aliases.__name__  # imported module remains available for introspection
