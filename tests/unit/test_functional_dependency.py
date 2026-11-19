import pytest
import sqlalchemy as sa

from datajudge import WithinRequirement
from datajudge.data_source import TableDataSource
from datajudge.db_access import DataReference, get_functional_dependency_violations


@pytest.fixture()
def engine():
    engine = sa.create_engine("sqlite:///:memory:")
    with engine.begin() as connection:
        connection.execute(
            sa.text("CREATE TABLE t (branch INTEGER, region VARCHAR(20))")
        )
        connection.execute(
            sa.text("INSERT INTO t VALUES (:branch, :region)"),
            [
                {"branch": 1, "region": "north"},
                {"branch": 1, "region": "north"},
                {"branch": 1, "region": "north"},
                {"branch": 1, "region": "south"},
                {"branch": 1, "region": "south"},
                {"branch": 2, "region": "east"},
                {"branch": 2, "region": "east"},
                {"branch": 3, "region": None},
                {"branch": 3, "region": "west"},
                {"branch": 4, "region": None},
                {"branch": None, "region": "north"},
                {"branch": None, "region": "south"},
            ],
        )
    return engine


def _reference():
    return DataReference(
        TableDataSource(db_name="db", table_name="t"),
        columns=["branch", "region"],
    )


def test_repeated_relations_are_counted_once(engine):
    violations, _ = get_functional_dependency_violations(
        engine, _reference(), ["branch"]
    )

    assert [violation.key for violation in violations] == [(1,), (3,)]
    branch_one = violations[0]
    assert branch_one.n_distinct_values == 2
    assert branch_one.conflicting_values == (("north",), ("south",))


def test_report_contains_two_contradictory_samples(engine):
    req = WithinRequirement.from_table(db_name="db", schema_name=None, table_name="t")
    req.add_functional_dependency_constraint(["branch"], ["region"])
    message = req[0].test(engine).failure_message

    assert "two contradictory examples are ('north',) vs ('south',)" in message
    assert "sample 1: branch = 1 , region = north" in message
    assert "sample 2: branch = 1 , region = south" in message
    assert message.count("region = north") == 1


def test_key_with_null_is_ignored(engine):
    violations, _ = get_functional_dependency_violations(
        engine, _reference(), ["branch"]
    )
    assert all(None not in violation.key for violation in violations)


def test_single_null_relation_is_not_a_conflict(engine):
    # Branch 4 maps only to a missing region.
    violations, _ = get_functional_dependency_violations(
        engine, _reference(), ["branch"]
    )
    assert (4,) not in {violation.key for violation in violations}


def test_null_separate_policy(engine):
    violations, _ = get_functional_dependency_violations(
        engine, _reference(), ["branch"], null_value_policy="separate"
    )
    branch_three = next(violation for violation in violations if violation.key == (3,))
    assert branch_three.conflicting_values == (("west",),)
    assert branch_three.missing_values == ((None,),)
    assert branch_three.witness_values == ((None,), ("west",))


def test_null_conflict_policy(engine):
    violations, _ = get_functional_dependency_violations(
        engine, _reference(), ["branch"], null_value_policy="conflict"
    )
    branch_three = next(violation for violation in violations if violation.key == (3,))
    assert branch_three.conflicting_values == ((None,), ("west",))
    assert branch_three.missing_values == ()


def test_invalid_null_policy_rejected(engine):
    with pytest.raises(ValueError, match="null_value_policy"):
        get_functional_dependency_violations(
            engine, _reference(), ["branch"], null_value_policy="ignore"
        )

    req = WithinRequirement.from_table(db_name="db", schema_name=None, table_name="t")
    with pytest.raises(ValueError, match="null_value_policy"):
        req.add_functional_dependency_constraint(
            ["branch"], ["region"], null_value_policy="ignore"
        )
