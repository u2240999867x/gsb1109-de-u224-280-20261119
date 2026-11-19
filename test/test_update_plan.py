import pytest

from dataset import DatasetError, StalePreviewError

TEST_CONTRACTS = [
    {"contract": "C1", "stage": "draft", "amount": 100, "status": "open"},
    {"contract": "C1", "stage": "final", "amount": 200, "status": "open"},
    {"contract": "C2", "stage": "draft", "amount": 300, "status": "open"},
]


@pytest.fixture(scope="function")
def contracts(db):
    tbl = db["contracts"]
    tbl.insert_many(TEST_CONTRACTS)
    return tbl


def test_preview_reports_affected_primary_keys(contracts):
    plan = contracts.preview_update_many(
        [
            {"contract": "C1", "stage": "draft", "amount": 150},
            {"contract": "C1", "stage": "final", "amount": 250},
        ],
        keys=["contract", "stage"],
    )
    assert len(plan.entries) == 2
    expected = {row["id"] for row in contracts.find(contract="C1")}
    reported = {pk for entry in plan.entries for (pk,) in entry.primary_keys}
    assert reported == expected
    first = plan.entries[0].targets[0]
    assert first.changes == {"amount": 150}
    assert first.before["amount"] == 100
    assert plan.affected_primary_keys == [
        (row["id"],) for row in contracts.find(contract="C1", order_by="id")
    ]
    # Preview must not write anything.
    assert contracts.find_one(contract="C1", stage="draft")["amount"] == 100


def test_preview_simulates_dependent_corrections(contracts):
    # The second correction matches on "status", which is only set to
    # "billed" by the first correction in the same batch.
    plan = contracts.preview_update_many(
        [
            (
                {"contract": "C1", "stage": "draft", "status": "billed"},
                ["contract", "stage"],
            ),
            (
                {"contract": "C1", "status": "billed", "amount": 175},
                ["contract", "status"],
            ),
        ],
        keys=["contract", "stage"],
    )
    first, second = plan.entries
    assert len(first.targets) == 1
    # Without simulation the second correction would match nothing, since
    # no row in the database has status "billed" yet.
    assert second.primary_keys == first.primary_keys
    assert second.targets[0].before["status"] == "billed"
    assert second.targets[0].changes == {"amount": 175}


def test_preview_no_match_is_noop(contracts):
    plan = contracts.preview_update_many(
        [{"contract": "C9", "stage": "draft", "amount": 1}],
        keys=["contract", "stage"],
    )
    assert plan.is_noop
    assert plan.entries[0].targets == []
    assert contracts.apply_update_plan(plan) == 0


def test_apply_update_plan_commits_previewed_changes(contracts):
    plan = contracts.preview_update_many(
        [
            (
                {"contract": "C1", "stage": "draft", "status": "billed"},
                ["contract", "stage"],
            ),
            (
                {"contract": "C1", "status": "billed", "amount": 175},
                ["contract", "status"],
            ),
        ],
        keys=["contract", "stage"],
    )
    assert contracts.apply_update_plan(plan) == 2
    row = contracts.find_one(contract="C1", stage="draft")
    assert row["status"] == "billed"
    assert row["amount"] == 175
    # Other rows are untouched.
    assert contracts.find_one(contract="C1", stage="final")["amount"] == 200
    assert contracts.find_one(contract="C2", stage="draft")["status"] == "open"


def test_apply_update_plan_detects_concurrent_modification(contracts):
    plan = contracts.preview_update_many(
        [
            {"contract": "C1", "stage": "draft", "amount": 150},
            {"contract": "C1", "stage": "final", "amount": 250},
        ],
        keys=["contract", "stage"],
    )
    # Someone else modifies the second row after the preview was made.
    contracts.update(
        {"contract": "C1", "stage": "final", "amount": 999},
        keys=["contract", "stage"],
    )
    with pytest.raises(StalePreviewError):
        contracts.apply_update_plan(plan)
    # The whole batch is rolled back, including the first, still-valid
    # correction.
    assert contracts.find_one(contract="C1", stage="draft")["amount"] == 100
    assert contracts.find_one(contract="C1", stage="final")["amount"] == 999


def test_apply_update_plan_detects_deleted_row(contracts):
    plan = contracts.preview_update_many(
        [{"contract": "C2", "stage": "draft", "amount": 350}],
        keys=["contract", "stage"],
    )
    contracts.delete(contract="C2", stage="draft")
    with pytest.raises(StalePreviewError):
        contracts.apply_update_plan(plan)


def test_apply_update_plan_rejects_other_table(db, contracts):
    plan = contracts.preview_update_many(
        [{"contract": "C1", "stage": "draft", "amount": 150}],
        keys=["contract", "stage"],
    )
    with pytest.raises(DatasetError, match="previewed for table"):
        db["other"].apply_update_plan(plan)
