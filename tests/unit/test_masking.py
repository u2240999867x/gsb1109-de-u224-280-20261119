import sqlalchemy as sa

from datajudge.constraints.base import _format_sample
from datajudge.constraints.miscs import FunctionalDependency, Uniqueness
from datajudge.constraints.uniques import UniquesEquality
from datajudge.data_source import ExpressionDataSource
from datajudge.db_access import DataReference
from datajudge.utils import ColumnMasker


def _make_table(rows):
    engine = sa.create_engine("sqlite://")
    metadata = sa.MetaData()
    table = sa.Table(
        "users",
        metadata,
        sa.Column("id", sa.Integer),
        sa.Column("email", sa.String),
    )
    metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(table.insert(), rows)
    return engine, table


def test_mask_value_consistent_and_distinct():
    masker = ColumnMasker(["email"])

    token1 = masker.mask_value("email", "alice@example.com")
    token2 = masker.mask_value("email", "alice@example.com")
    token3 = masker.mask_value("email", "bob@example.com")

    assert token1 == token2
    assert token1 != token3
    assert "alice@example.com" not in token1
    assert "bob@example.com" not in token3


def test_mask_value_per_column():
    masker = ColumnMasker(["email", "phone"])

    assert masker.mask_value("email", "x@example.com") != masker.mask_value(
        "phone", "x@example.com"
    )
    # columns not registered with the masker are passed through
    assert masker.mask_value("name", "Alice") == "Alice"
    # None values are passed through
    assert masker.mask_value("email", None) is None


def test_mask_value_unhashable():
    masker = ColumnMasker(["tags"])

    token1 = masker.mask_value("tags", ["a", "b"])
    token2 = masker.mask_value("tags", ["a", "b"])

    assert token1 == token2


def test_mask_value_custom_placeholder():
    masker = ColumnMasker(["email"], placeholder="masked-{column}-{index}")

    assert masker.mask_value("email", "alice@example.com") == "masked-email-1"
    assert masker.mask_value("email", "bob@example.com") == "masked-email-2"


def test_mask_row():
    masker = ColumnMasker(["email"])

    masked = masker.mask_row(["id", "email"], (1, "alice@example.com"))

    assert masked[0] == 1
    assert masked[1] == masker.mask_value("email", "alice@example.com")
    # rows longer than the column list keep their values and length
    assert masker.mask_row(["email"], ("a@example.com", "extra")) == (
        masker.mask_value("email", "a@example.com"),
        "extra",
    )


def test_format_sample_with_masker():
    _, table = _make_table([])
    ref = DataReference(ExpressionDataSource(table, "users"), ["id", "email"])
    masker = ColumnMasker(["email"])

    sample_string = _format_sample((1, "alice@example.com"), ref, masker)

    assert "alice@example.com" not in sample_string
    assert "id = 1" in sample_string
    assert f"email = {masker.mask_value('email', 'alice@example.com')}" in sample_string


def test_format_sample_without_masker_unchanged():
    _, table = _make_table([])
    ref = DataReference(ExpressionDataSource(table, "users"), ["id", "email"])

    sample_string = _format_sample((1, "alice@example.com"), ref)

    assert "alice@example.com" in sample_string


def test_uniqueness_constraint_masks_failure_message():
    engine, table = _make_table(
        [
            {"id": 1, "email": "alice@example.com"},
            {"id": 1, "email": "alice@example.com"},
            {"id": 2, "email": "bob@example.com"},
        ]
    )
    ref = DataReference(ExpressionDataSource(table, "users"), ["id", "email"])
    masker = ColumnMasker(["email"])
    constraint = Uniqueness(ref, masker=masker)

    test_result = constraint.test(engine)

    assert not test_result.outcome
    failure_message = test_result.failure_message
    # raw values must not leak into the (machine-readable) failure message
    assert "alice@example.com" not in failure_message
    assert "bob@example.com" not in failure_message
    assert masker.mask_value("email", "alice@example.com") in failure_message
    # statistics are still computed on the original data
    assert "3 rows > 2 uniques" in failure_message
    # unmasked columns are still shown in the clear
    assert "id = 1" in failure_message


def test_uniqueness_constraint_success_untouched_by_masker():
    engine, table = _make_table(
        [
            {"id": 1, "email": "alice@example.com"},
            {"id": 2, "email": "bob@example.com"},
        ]
    )
    ref = DataReference(ExpressionDataSource(table, "users"), ["id", "email"])
    constraint = Uniqueness(ref, masker=ColumnMasker(["email"]))

    assert constraint.test(engine).outcome


def test_functional_dependency_masks_violations():
    _, table = _make_table([])
    ref = DataReference(ExpressionDataSource(table, "users"), ["id", "email"])
    masker = ColumnMasker(["email"])
    constraint = FunctionalDependency(ref, key_columns=["id"], masker=masker)

    masked = constraint._apply_output_formatting(
        [(1, "alice@example.com"), (2, "alice@example.com"), (3, "bob@example.com")]
    )

    assert masked == [
        (1, masker.mask_value("email", "alice@example.com")),
        (2, masker.mask_value("email", "alice@example.com")),
        (3, masker.mask_value("email", "bob@example.com")),
    ]


def test_uniques_equality_masks_scalar_values():
    _, table = _make_table([])
    ref = DataReference(ExpressionDataSource(table, "users"), ["email"])
    masker = ColumnMasker(["email"])
    constraint = UniquesEquality(
        ref,
        uniques={"alice@example.com"},
        filter_func=lambda values: values,
        masker=masker,
    )

    masked = constraint._apply_output_formatting(
        {"alice@example.com", "bob@example.com"}
    )

    assert set(masked) == {
        masker.mask_value("email", "alice@example.com"),
        masker.mask_value("email", "bob@example.com"),
    }


def test_shared_masker_aligns_tokens_across_constraints():
    """Two constraints of one report must pseudonymize an identifier identically."""
    _, table = _make_table([])
    masker = ColumnMasker(["email"])
    ref1 = DataReference(ExpressionDataSource(table, "users"), ["id", "email"])
    ref2 = DataReference(ExpressionDataSource(table, "users"), ["id", "email"])
    constraint1 = FunctionalDependency(ref1, key_columns=["id"], masker=masker)
    constraint2 = FunctionalDependency(ref2, key_columns=["id"], masker=masker)

    masked1 = constraint1._apply_output_formatting([(1, "alice@example.com")])
    masked2 = constraint2._apply_output_formatting([(2, "alice@example.com")])

    assert masked1[0][1] == masked2[0][1]
