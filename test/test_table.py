from datetime import datetime

import pytest
from sqlalchemy.exc import ArgumentError
from sqlalchemy.types import BIGINT, TEXT

from dataset import chunked
from dataset.util import QueryError

from .conftest import TEST_CITY_1, TEST_DATA


def test_insert(table):
    assert len(table) == len(TEST_DATA), len(table)
    last_id = table.insert(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"}
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)
    assert table.find_one(id=last_id)["place"] == "Berlin"


def test_insert_ignore(table):
    table.insert_ignore(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
        ["place"],
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)
    table.insert_ignore(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
        ["place"],
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)


def test_insert_ignore_all_key(table):
    for _i in range(0, 4):
        table.insert_ignore(
            {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
            ["date", "temperature", "place"],
        )
    assert len(table) == len(TEST_DATA) + 1, len(table)


def test_insert_json(table):
    last_id = table.insert(
        {
            "date": datetime(2011, 1, 2),
            "temperature": -10,
            "place": "Berlin",
            "info": {
                "currency": "EUR",
                "language": "German",
                "population": 3292365,
            },
        }
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)
    assert table.find_one(id=last_id)["place"] == "Berlin"


def test_upsert(table):
    table.upsert(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
        ["place"],
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)
    table.upsert(
        {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
        ["place"],
    )
    assert len(table) == len(TEST_DATA) + 1, len(table)


def test_upsert_single_column(db):
    table = db["banana_single_col"]
    table.upsert({"color": "Yellow"}, ["color"])
    assert len(table) == 1, len(table)
    table.upsert({"color": "Yellow"}, ["color"])
    assert len(table) == 1, len(table)


def test_upsert_all_key(table):
    assert len(table) == len(TEST_DATA), len(table)
    for _i in range(0, 2):
        table.upsert(
            {"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"},
            ["date", "temperature", "place"],
        )
    assert len(table) == len(TEST_DATA) + 1, len(table)


def test_upsert_id(db):
    table = db["banana_with_id"]
    data = {"id": 10, "title": "I am a banana!"}
    table.upsert(data, ["id"])
    assert len(table) == 1, len(table)


def test_update_while_iter(table):
    for row in table:
        row["foo"] = "bar"
        table.update(row, ["place", "date"])
    assert len(table) == len(TEST_DATA), len(table)


def test_weird_column_names(table):
    with pytest.raises(ValueError):
        table.insert(
            {
                "date": datetime(2011, 1, 2),
                "temperature": -10,
                "foo.bar": "Berlin",
                "qux.bar": "Huhu",
            }
        )


def test_cased_column_names(db):
    tbl = db["cased_column_names"]
    tbl.insert({"place": "Berlin"})
    tbl.insert({"Place": "Berlin"})
    tbl.insert({"PLACE ": "Berlin"})
    assert len(tbl.columns) == 2, tbl.columns
    assert len(list(tbl.find(Place="Berlin"))) == 3
    assert len(list(tbl.find(place="Berlin"))) == 3
    assert len(list(tbl.find(PLACE="Berlin"))) == 3


def test_invalid_column_names(db):
    tbl = db["weather"]
    with pytest.raises(ValueError):
        tbl.insert({None: "banana"})

    with pytest.raises(ValueError):
        tbl.insert({"": "banana"})

    with pytest.raises(ValueError):
        tbl.insert({"-": "banana"})


def test_delete(table):
    table.insert({"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"})
    original_count = len(table)
    assert len(table) == len(TEST_DATA) + 1, len(table)
    # Test bad use of API
    with pytest.raises(ArgumentError):
        table.delete({"place": "Berlin"})
    assert len(table) == original_count, len(table)

    assert table.delete(place="Berlin") is True, "should return 1"
    assert len(table) == len(TEST_DATA), len(table)
    assert table.delete() is True, "should return non zero"
    assert len(table) == 0, len(table)


def test_repr(table):
    assert repr(table) == "<Table(weather)>", (
        "the representation should be <Table(weather)>"
    )


def test_delete_nonexist_entry(table):
    assert table.delete(place="Berlin") is False, (
        "entry not exist, should fail to delete"
    )


def test_find_one(table):
    table.insert({"date": datetime(2011, 1, 2), "temperature": -10, "place": "Berlin"})
    d = table.find_one(place="Berlin")
    assert d["temperature"] == -10, d
    d = table.find_one(place="Atlantis")
    assert d is None, d


def test_count(table):
    assert len(table) == 6, len(table)
    length = table.count(place=TEST_CITY_1)
    assert length == 3, length


def test_find(table):
    ds = list(table.find(place=TEST_CITY_1))
    assert len(ds) == 3, ds
    ds = list(table.find(place=TEST_CITY_1, _limit=2))
    assert len(ds) == 2, ds
    ds = list(table.find(place=TEST_CITY_1, _limit=2, _step=1))
    assert len(ds) == 2, ds
    ds = list(table.find(place=TEST_CITY_1, _limit=1, _step=2))
    assert len(ds) == 1, ds
    ds = list(table.find(_step=2))
    assert len(ds) == len(TEST_DATA), ds
    ds = list(table.find(order_by=["temperature"]))
    assert ds[0]["temperature"] == -1, ds
    ds = list(table.find(order_by=["-temperature"]))
    assert ds[0]["temperature"] == 8, ds
    ds = list(table.find(table.table.columns.temperature > 4))
    assert len(ds) == 3, ds


def test_find_dsl(table):
    ds = list(table.find(place={"like": "%lw%"}))
    assert len(ds) == 3, ds
    ds = list(table.find(temperature={">": 5}))
    assert len(ds) == 2, ds
    ds = list(table.find(temperature={">=": 5}))
    assert len(ds) == 3, ds
    ds = list(table.find(temperature={"<": 0}))
    assert len(ds) == 1, ds
    ds = list(table.find(temperature={"<=": 0}))
    assert len(ds) == 2, ds
    ds = list(table.find(temperature={"!=": -1}))
    assert len(ds) == 5, ds
    ds = list(table.find(temperature={"between": [5, 8]}))
    assert len(ds) == 3, ds
    ds = list(table.find(place={"=": "G€lway"}))
    assert len(ds) == 3, ds
    ds = list(table.find(place={"ilike": "%LwAy"}))
    assert len(ds) == 3, ds


def test_startswith_endswith(db):
    t = db["prefix_test"]
    t.insert({"org": "acme"})
    t.insert({"org": "acme_labs"})
    t.insert({"org": "other"})
    t.insert({"org": "admin"})

    # Basic startswith
    rows = list(t.find(org={"startswith": "acme"}))
    assert len(rows) == 2, rows

    # Basic endswith
    rows = list(t.find(org={"endswith": "labs"}))
    assert len(rows) == 1, rows

    # LIKE metacharacters must be escaped (CVE-style regression test)
    rows = list(t.find(org={"startswith": "%"}))
    assert len(rows) == 0, f"startswith % should match nothing, got {rows}"

    rows = list(t.find(org={"startswith": "_"}))
    assert len(rows) == 0, f"startswith _ should match nothing, got {rows}"

    rows = list(t.find(org={"endswith": "%"}))
    assert len(rows) == 0, f"endswith % should match nothing, got {rows}"

    # Delete must not overmatch
    t.delete(org={"startswith": "%"})
    assert t.count() == 4, "delete with % startswith should not remove rows"


def test_offset(table):
    ds = list(table.find(place=TEST_CITY_1, _offset=1))
    assert len(ds) == 2, ds
    ds = list(table.find(place=TEST_CITY_1, _limit=2, _offset=2))
    assert len(ds) == 1, ds


def _keyset_rows(table):
    updated = datetime(2020, 1, 1, 12, 0, 0)
    rows = [
        {"updated": updated, "label": "a"},
        {"updated": updated, "label": "b"},
        {"updated": updated, "label": "c"},
        {"updated": updated, "label": "d"},
        {"updated": datetime(2020, 1, 2, 12, 0, 0), "label": "e"},
        {"updated": None, "label": "f"},
        {"updated": None, "label": "g"},
        {"updated": datetime(2019, 12, 31, 12, 0, 0), "label": "h"},
    ]
    table.insert_many(rows)


def _page_by_cursor(table, order_by, page_size, **filters):
    """Read the whole result one page at a time and return the row ids."""
    seen = []
    after = None
    while True:
        kwargs = dict(filters, order_by=order_by, _limit=page_size)
        if after is not None:
            kwargs["_after"] = after
        page = list(table.find(**kwargs))
        if not page:
            break
        seen.extend(row["id"] for row in page)
        after = page[-1]
        assert len(page) <= page_size
    return seen


@pytest.mark.parametrize("page_size", [1, 2, 3, 5])
def test_find_after_matches_full_scan(db, page_size):
    table = db["keyset_scan"]
    _keyset_rows(table)

    order_variants = [
        "-updated",
        "updated",
        ["-updated", "label"],
        ["label", "updated"],
    ]
    for order_by in order_variants:
        expected = [row["id"] for row in table.find(order_by=order_by)]
        assert _page_by_cursor(table, order_by, page_size) == expected


def test_find_after_desc_updated_asc_id(db):
    table = db["keyset_desc"]
    _keyset_rows(table)

    expected = [row["id"] for row in table.find(order_by="-updated")]
    assert _page_by_cursor(table, "-updated", 2) == expected

    # Rows with equal timestamps are ordered by ascending id.
    tie_rows = list(table.find(updated=datetime(2020, 1, 1, 12, 0, 0)))
    ids = [row["id"] for row in tie_rows]
    assert ids == sorted(ids)


def test_find_after_null_updates(db):
    table = db["keyset_null"]
    _keyset_rows(table)

    expected = [row["id"] for row in table.find(order_by="-updated")]
    paged = _page_by_cursor(table, "-updated", 3)
    assert paged == expected

    # NULL timestamps form one contiguous group and are still pageable.
    null_ids = [row["id"] for row in table.find(updated=None)]
    paged_null_ids = [row_id for row_id in paged if row_id in set(null_ids)]
    assert paged_null_ids == null_ids


def test_find_after_multi_column_cursor(table):
    ds = list(table.find(order_by=["-temperature", "place"], _limit=3))
    assert len(ds) == 3, ds
    nxt = list(table.find(order_by=["-temperature", "place"], _after=ds[-1]))
    assert len(nxt) == len(table) - 3, nxt
    assert not {row["id"] for row in ds} & {row["id"] for row in nxt}


def test_find_after_survives_inserts_before_position(db):
    table = db["keyset_inserts"]
    _keyset_rows(table)

    full = [row["id"] for row in table.find(order_by="-updated")]
    first = list(table.find(order_by="-updated", _limit=3))
    assert [row["id"] for row in first] == full[:3]

    # Insert rows that sort before the end of the first page.
    table.insert({"updated": datetime(2020, 1, 3), "label": "new-1"})
    table.insert({"updated": datetime(2020, 1, 4), "label": "new-2"})

    rest = []
    after = first[-1]
    while True:
        page = list(table.find(order_by="-updated", _limit=3, _after=after))
        if not page:
            break
        rest.extend(row["id"] for row in page)
        after = page[-1]

    assert [row["id"] for row in first] + rest == full


def test_find_after_sequence_cursor(db):
    table = db["keyset_sequence"]
    _keyset_rows(table)
    order_by = "-updated"
    full = list(table.find(order_by=order_by))
    position = full[2]
    expected = [row["id"] for row in full[3:]]

    numeric = list(
        table.find(
            order_by=order_by,
            _after=(position["updated"], position["id"]),
        )
    )
    assert [row["id"] for row in numeric] == expected


def test_find_after_bad_cursor(table):
    with pytest.raises(QueryError):
        list(table.find(order_by="-temperature", _after=(1, 2, 3)))
    with pytest.raises(QueryError):
        list(table.find(order_by="-temperature", _after={"id": 1}))


def test_find_after_without_order_by(table):
    full = [row["id"] for row in table.find(order_by="id")]
    first = list(table.find(order_by="id", _limit=2))
    nxt = list(table.find(_limit=2, _after=first[-1]))
    assert [row["id"] for row in first + nxt] == full[:4]


def test_find_after_custom_primary_key(db):
    table = db.create_table(
        "keyset_string_pk",
        primary_id="key",
        primary_type=db.types.string(50),
    )
    for index in range(10):
        table.insert(
            {
                "key": f"k{index:02d}",
                "updated": datetime(2020, 1, index % 3 + 1),
            }
        )

    expected = [row["key"] for row in table.find(order_by="-updated")]
    paged = []
    after = None
    while True:
        kwargs = {"order_by": "-updated", "_limit": 3}
        if after is not None:
            kwargs["_after"] = after
        page = list(table.find(**kwargs))
        if not page:
            break
        paged.extend(row["key"] for row in page)
        after = page[-1]
    assert paged == expected


def test_find_after_order_by_primary_key(db):
    table = db["keyset_pk_order"]
    _keyset_rows(table)

    expected = [row["id"] for row in table.find(order_by="-id")]
    paged = _page_by_cursor(table, "-id", 3)
    assert paged == expected


def test_find_after_random_data_matches_full_scan(db):
    import random

    rng = random.Random(1109)
    table = db["keyset_random"]
    rows = []
    for _ in range(120):
        updated = rng.choice(
            [
                None,
                datetime(2020, 1, 1),
                datetime(2020, 1, 2),
                datetime(2020, 1, 3),
            ]
        )
        rows.append(
            {
                "updated": updated,
                "bucket": rng.choice(["a", "b", "c"]),
                "label": rng.choice(["x", "y"]),
            }
        )
    table.insert_many(rows)

    for order_by in [
        "-updated",
        "updated",
        ["-updated", "-bucket"],
        ["bucket", "-updated"],
        ["-label", "updated"],
    ]:
        expected = [
            (row["id"], row["updated"], row["bucket"], row["label"])
            for row in table.find(order_by=order_by)
        ]
        for page_size in (1, 7, 13):
            paged = []
            after = None
            while True:
                kwargs = {"order_by": order_by, "_limit": page_size}
                if after is not None:
                    kwargs["_after"] = after
                page = list(table.find(**kwargs))
                if not page:
                    break
                paged.extend(
                    (row["id"], row["updated"], row["bucket"], row["label"])
                    for row in page
                )
                after = page[-1]
            assert paged == expected, (order_by, page_size)


def test_streamed_update(table):
    ds = list(table.find(place=TEST_CITY_1, _streamed=True, _step=1))
    assert len(ds) == 3, len(ds)
    for row in table.find(place=TEST_CITY_1, _streamed=True, _step=1):
        row["temperature"] = -1
        table.update(row, ["id"])


def test_distinct(table):
    x = list(table.distinct("place"))
    assert len(x) == 2, x
    x = list(table.distinct("place", "date"))
    assert len(x) == 6, x
    x = list(
        table.distinct(
            "place",
            "date",
            table.table.columns.date >= datetime(2011, 1, 2, 0, 0),
        )
    )
    assert len(x) == 4, x

    x = list(table.distinct("temperature", place="B€rkeley"))
    assert len(x) == 3, x
    x = list(table.distinct("temperature", place=["B€rkeley", "G€lway"]))
    assert len(x) == 6, x


def test_insert_many(table):
    data = TEST_DATA * 100
    table.insert_many(data, chunk_size=13)
    assert len(table) == len(data) + 6, (len(table), len(data))


def test_chunked_insert(table):
    data = TEST_DATA * 100
    with chunked.ChunkedInsert(table) as chunk_tbl:
        for item in data:
            chunk_tbl.insert(item)
    assert len(table) == len(data) + 6, (len(table), len(data))


def test_chunked_insert_callback(table):
    data = TEST_DATA * 100
    n_items = 0

    def callback(queue):
        nonlocal n_items
        n_items += len(queue)

    with chunked.ChunkedInsert(table, callback=callback) as chunk_tbl:
        for item in data:
            chunk_tbl.insert(item)
    assert len(data) == n_items
    assert len(table) == len(data) + 6


def test_update_many(db):
    tbl = db["update_many_test"]
    tbl.insert_many([{"temp": 10}, {"temp": 20}, {"temp": 30}])
    tbl.update_many([{"id": 1, "temp": 50}, {"id": 3, "temp": 50}], "id")

    # Ensure data has been updated.
    assert tbl.find_one(id=1)["temp"] == tbl.find_one(id=3)["temp"]


def test_chunked_update(db):
    tbl = db["update_many_test"]
    tbl.insert_many(
        [
            {"temp": 10, "location": "asdf"},
            {"temp": 20, "location": "qwer"},
            {"temp": 30, "location": "asdf"},
        ]
    )

    chunked_tbl = chunked.ChunkedUpdate(tbl, ["id"])
    chunked_tbl.update({"id": 1, "temp": 50})
    chunked_tbl.update({"id": 2, "location": "asdf"})
    chunked_tbl.update({"id": 3, "temp": 50})
    chunked_tbl.flush()

    # Ensure data has been updated.
    assert tbl.find_one(id=1)["temp"] == tbl.find_one(id=3)["temp"] == 50
    assert tbl.find_one(id=2)["location"] == tbl.find_one(id=3)["location"] == "asdf"


def test_upsert_many(db):
    # Also tests updating on records with different attributes
    tbl = db["upsert_many_test"]

    weight = 100
    tbl.upsert_many([{"age": 10}, {"weight": weight}], "id")
    assert tbl.find_one(id=1)["age"] == 10

    tbl.upsert_many([{"id": 1, "age": 70}, {"id": 2, "weight": weight / 2}], "id")
    assert tbl.find_one(id=2)["weight"] == weight / 2


def test_drop_operations(table):
    assert table._table is not None, "table shouldn't be dropped yet"
    table.drop()
    assert table._table is None, "table should be dropped now"
    assert list(table.all()) == [], table.all()
    assert table.count() == 0, table.count()


def test_table_drop(db, table):
    assert "weather" in db
    db["weather"].drop()
    assert "weather" not in db


def test_table_drop_then_create(db, table):
    assert "weather" in db
    db["weather"].drop()
    assert "weather" not in db
    db["weather"].insert({"foo": "bar"})


def test_columns(table):
    cols = table.columns
    assert len(list(cols)) == 4, "column count mismatch"
    assert "date" in cols and "temperature" in cols and "place" in cols


def test_drop_column(table):
    try:
        table.drop_column("date")
        assert "date" not in table.columns
    except RuntimeError:
        pass


def test_iter(table):
    c = 0
    for _row in table:
        c += 1
    assert c == len(table)


def test_update(table):
    date = datetime(2011, 1, 2)
    res = table.update(
        {"date": date, "temperature": -10, "place": TEST_CITY_1}, ["place", "date"]
    )
    assert res, "update should return True"
    m = table.find_one(place=TEST_CITY_1, date=date)
    assert m["temperature"] == -10, (
        f"new temp. should be -10 but is {m['temperature']}"
    )


def test_create_column(db, table):
    flt = db.types.float
    table.create_column("foo", flt)
    assert "foo" in table.table.c, table.table.c
    assert isinstance(table.table.c["foo"].type, flt), table.table.c["foo"].type
    assert "foo" in table.columns, table.columns


def test_ensure_column(db, table):
    flt = db.types.float
    table.create_column_by_example("foo", 0.1)
    assert "foo" in table.table.c, table.table.c
    assert isinstance(table.table.c["foo"].type, flt), table.table.c["foo"].type
    table.create_column_by_example("bar", 1)
    assert "bar" in table.table.c, table.table.c
    assert isinstance(table.table.c["bar"].type, BIGINT), table.table.c["bar"].type
    table.create_column_by_example("pippo", "test")
    assert "pippo" in table.table.c, table.table.c
    assert isinstance(table.table.c["pippo"].type, TEXT), table.table.c["pippo"].type
    table.create_column_by_example("bigbar", 11111111111)
    assert "bigbar" in table.table.c, table.table.c
    assert isinstance(table.table.c["bigbar"].type, BIGINT), table.table.c[
        "bigbar"
    ].type
    table.create_column_by_example("littlebar", -11111111111)
    assert "littlebar" in table.table.c, table.table.c
    assert isinstance(table.table.c["littlebar"].type, BIGINT), table.table.c[
        "littlebar"
    ].type


def test_key_order(db, table):
    res = db.query("SELECT temperature, place FROM weather LIMIT 1")
    keys = list(res.next().keys())
    assert keys[0] == "temperature"
    assert keys[1] == "place"


def test_empty_query(table):
    empty = list(table.find(place="not in data"))
    assert len(empty) == 0, empty
