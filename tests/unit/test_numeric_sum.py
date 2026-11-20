import pytest

from datajudge.constraints.numeric import NumericSum
from datajudge.db_access import DataReference, TableDataSource


@pytest.fixture(scope="module")
def ref():
    data_source = TableDataSource("my_db", "my_table")
    return DataReference(data_source, ["column"], None)


def test_invalid_bounds(ref):
    with pytest.raises(ValueError):
        NumericSum(ref, lower_bound=10, upper_bound=-10)


def test_accepts_equal_bounds(ref):
    constraint = NumericSum(ref, lower_bound=0, upper_bound=0)
    assert constraint._lower_bound == 0
    assert constraint._upper_bound == 0
