"""Compare the in-memory and in-database regex paths on identical data.

The tests run on SQLite so that both implementations can be exercised without
an external database server.
"""

import re

import pytest
import sqlalchemy as sa

from datajudge import WithinRequirement
from datajudge.db_access import apply_patches

POSTAL_CODE_REGEX = r"^[0-9]{5}$"
POSTAL_CODES = [
    "10115",  # standard digits
    "１２３４５",  # full-width digits, valid once normalized
    "99999",
    "1011A",  # genuinely malformed
    "１０１１Ａ",  # full-width and malformed even after normalization
    " 12345",  # leading whitespace, malformed
    None,
]

_COUNTS_PATTERN = re.compile(
    r"In absolute terms, (\d+) of the (\d+) samples violated the regex"
)


@pytest.fixture()
def postal_codes_engine():
    engine = sa.create_engine("sqlite://")
    apply_patches(engine)
    with engine.begin() as connection:
        connection.execute(sa.text("CREATE TABLE postal_codes (code TEXT)"))
        connection.execute(
            sa.text("INSERT INTO postal_codes (code) VALUES (:code)"),
            [{"code": code} for code in POSTAL_CODES],
        )
        connection.execute(sa.text("CREATE TABLE full_width_codes (code TEXT)"))
        connection.execute(
            sa.text("INSERT INTO full_width_codes (code) VALUES (:code)"),
            [{"code": "１２３４５"}, {"code": "６７８９０"}, {"code": None}],
        )
    return engine


def _test_result(engine, *, in_database, **kwargs):
    requirement = WithinRequirement.from_table("main", "main", "postal_codes")
    if in_database:
        requirement.add_varchar_regex_constraint_db("code", POSTAL_CODE_REGEX, **kwargs)
    else:
        requirement.add_varchar_regex_constraint("code", POSTAL_CODE_REGEX, **kwargs)
    return requirement[0].test(engine)


def _counts(test_result):
    match = _COUNTS_PATTERN.search(test_result.failure_message or "")
    return (int(match.group(1)), int(match.group(2))) if match else None


@pytest.mark.parametrize("aggregated", [True, False])
@pytest.mark.parametrize("unicode_normalization", ["NFKC", "nfc", None])
def test_db_and_memory_paths_agree(
    postal_codes_engine, aggregated, unicode_normalization
):
    kwargs = dict(
        allow_none=True,
        aggregated=aggregated,
        unicode_normalization=unicode_normalization,
    )
    in_memory = _test_result(postal_codes_engine, in_database=False, **kwargs)
    in_database = _test_result(postal_codes_engine, in_database=True, **kwargs)

    assert in_memory.outcome == in_database.outcome
    assert _counts(in_memory) == _counts(in_database)


@pytest.mark.parametrize("in_database", [True, False])
def test_full_width_only_data_passes_after_normalization(
    postal_codes_engine, in_database
):
    requirement = WithinRequirement.from_table("main", "main", "full_width_codes")
    kwargs = dict(allow_none=True, unicode_normalization="NFKC")
    if in_database:
        requirement.add_varchar_regex_constraint_db("code", POSTAL_CODE_REGEX, **kwargs)
    else:
        requirement.add_varchar_regex_constraint("code", POSTAL_CODE_REGEX, **kwargs)
    test_result = requirement[0].test(postal_codes_engine)
    assert test_result.outcome, test_result.failure_message


def test_nfkc_accepts_full_width_digits(postal_codes_engine):
    without_normalization = _test_result(
        postal_codes_engine, in_database=True, allow_none=True
    )
    with_normalization = _test_result(
        postal_codes_engine,
        in_database=True,
        allow_none=True,
        unicode_normalization="NFKC",
    )

    # The full-width "１２３４５" is a violation without normalization but valid
    # with it, so normalization removes exactly one violation.
    assert _counts(without_normalization) == (4, 6)
    assert _counts(with_normalization) == (3, 6)
    # Values that remain invalid are reported as stored.
    assert "１０１１Ａ" in with_normalization.failure_message
    assert "１２３４５" not in with_normalization.failure_message


def test_counterexamples_keep_raw_input(postal_codes_engine):
    test_result = _test_result(
        postal_codes_engine,
        in_database=True,
        allow_none=True,
        n_counterexamples=-1,
        unicode_normalization="NFKC",
    )
    message = test_result.failure_message
    assert message is not None
    # Malformed values are reported exactly as stored, including full-width
    # characters that survive normalization unchanged.
    assert "１０１１Ａ" in message
    assert "1011A" in message
    assert " 12345" in message


def test_none_rejected_consistently(postal_codes_engine):
    in_memory = _test_result(postal_codes_engine, in_database=False, allow_none=False)
    in_database = _test_result(postal_codes_engine, in_database=True, allow_none=False)
    assert not in_memory.outcome
    assert in_memory.outcome == in_database.outcome
    assert "allow_none=True" in in_memory.failure_message
    assert "allow_none=True" in in_database.failure_message


@pytest.mark.parametrize("in_database", [True, False])
def test_invalid_normalization_form_rejected(postal_codes_engine, in_database):
    with pytest.raises(ValueError, match="unicode_normalization"):
        _test_result(
            postal_codes_engine,
            in_database=in_database,
            unicode_normalization="NOPE",
        )
