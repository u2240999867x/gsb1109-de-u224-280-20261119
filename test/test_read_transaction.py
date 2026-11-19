import tempfile

import pytest

from dataset import DatasetError, connect


def test_read_transaction_handle_identifies_backing_transaction(db, table):
    with db.read_transaction() as rt:
        assert rt.is_active
        assert bool(rt)
        assert rt.read_only is True
        assert rt.connection is db.executable
        assert rt.transaction is db.executable.get_transaction()
        assert db.current_read_transaction is rt
        assert db.is_read_only
        assert "active" in repr(rt)
    assert not rt.is_active
    assert not bool(rt)
    assert db.current_read_transaction is None
    assert not db.is_read_only
    assert "closed" in repr(rt)


def test_read_transaction_gives_consistent_snapshot():
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        writer = connect(f"sqlite:///{tmp.name}")
        reader = connect(f"sqlite:///{tmp.name}")
        try:
            snap_table = writer["snapshot"]
            snap_table.insert_many([{"value": i} for i in range(5)])

            with reader.read_transaction() as rt:
                assert "snapshot" in reader
                assert snap_table.name in reader.tables
                assert reader["snapshot"].count() == 5

                # Another connection commits rows while the report is built.
                writer["snapshot"].insert_many([{"value": 6}, {"value": 7}])

                # Existence check, count and paged reads share one snapshot.
                assert reader["snapshot"].count() == 5
                page = list(reader["snapshot"].find(_limit=10))
                assert len(page) == 5
                assert rt.transaction.is_active

            # A fresh read after the read transaction ends sees the new rows.
            assert reader["snapshot"].count() == 7
        finally:
            writer.close()
            reader.close()


def test_read_transaction_pins_snapshot_after_prior_reads():
    # A SELECT issued before entering the read transaction leaves an
    # SQLAlchemy autobegin transaction whose pysqlite driver has not really
    # started a transaction. The read transaction must still pin a snapshot
    # at entry rather than observe rows committed afterwards.
    with tempfile.NamedTemporaryFile(suffix=".db") as tmp:
        writer = connect(f"sqlite:///{tmp.name}")
        reader = connect(f"sqlite:///{tmp.name}")
        try:
            table = writer["snapshot"]
            table.insert_many([{"value": i} for i in range(3)])
            assert reader["snapshot"].count() == 3  # triggers autobegin

            with reader.read_transaction():
                writer["snapshot"].insert({"value": 3})
                assert reader["snapshot"].count() == 3
                assert len(list(reader["snapshot"].find())) == 3

            assert reader["snapshot"].count() == 4
        finally:
            writer.close()
            reader.close()


def test_read_transaction_rolls_back_its_own_transaction(db, table):
    initial = table.count()
    conn = db.executable
    with db.read_transaction():
        assert conn.in_transaction()
    assert not conn.in_transaction()
    # The connection used by the finished read was released; a new one works.
    assert table.count() == initial


def test_read_transaction_sees_uncommitted_outer_writes(db):
    with db:
        db["report"].insert({"place": "inside outer tx"})
        assert db.in_transaction

        with db.read_transaction() as rt:
            # The read rides on the outer transaction and sees its own rows.
            assert rt.transaction is db.executable.get_transaction()
            assert db["report"].count() == 1
            assert db["report"].find_one(place="inside outer tx") is not None

        # Ending the read must not commit (or end) the outer transaction.
        assert db.in_transaction
        assert db["report"].count() == 1
        assert not rt.is_active

    # Outer block committed normally; the row is now durable.
    assert db["report"].count() == 1


def test_read_transaction_never_commits_outer_write_on_rollback(db):
    db.begin()
    db["report"].insert({"place": "uncommitted"})
    try:
        with db.read_transaction():
            assert db["report"].count() == 1
        assert db.in_transaction
    finally:
        db.rollback()
    assert not db.in_transaction
    assert db["report"].count() == 0


def test_read_transaction_exception_still_releases_snapshot(db, table):
    with (
        pytest.raises(RuntimeError),
        db.read_transaction() as rt,
    ):
        raise RuntimeError("boom")
    assert not rt.is_active
    assert not db.is_read_only
    # Writes work again once the read scope is gone.
    table.insert({"place": "after failure"})


def test_nested_read_transactions_share_snapshot(db, table):
    with db.read_transaction() as outer:
        with db.read_transaction() as inner:
            assert db.current_read_transaction is inner
            assert inner.transaction is outer.transaction
            with pytest.raises(DatasetError):
                table.insert({"place": "noped"})
        assert db.current_read_transaction is outer
        assert not inner.is_active
        assert outer.is_active
        with pytest.raises(DatasetError):
            table.insert({"place": "still noped"})
    assert db.current_read_transaction is None
    table.insert({"place": "allowed again"})


@pytest.mark.parametrize(
    "operation",
    [
        lambda t: t.insert({"place": "x"}),
        lambda t: t.insert_many([{"place": "x"}]),
        lambda t: t.update({"place": "x"}, ["id"]),
        lambda t: t.update_many([{"id": 1, "place": "x"}], ["id"]),
        lambda t: t.upsert({"id": 1, "place": "x"}, ["id"]),
        lambda t: t.upsert_many([{"id": 1, "place": "x"}], ["id"]),
        lambda t: t.insert_ignore({"id": 1, "place": "x"}, ["id"]),
        lambda t: t.delete(),
        lambda t: t.create_column("extra", t.db.types.text),
        lambda t: t.create_index(["place"]),
        lambda t: t.drop(),
    ],
)
def test_writes_are_rejected_inside_read_transaction(db, operation):
    table = db["guarded"]
    table.insert({"place": "seed"})
    with db.read_transaction(), pytest.raises(DatasetError):
        operation(table)
    # The rejected write left the table intact and usable.
    assert table.exists
    assert table.count() == 1


def test_streamed_find_rejected_inside_read_transaction(db, table):
    with db.read_transaction(), pytest.raises(DatasetError):
        table.find(_streamed=True)


def test_beginning_a_write_transaction_inside_read_transaction_rejected(db):
    with db.read_transaction(), pytest.raises(DatasetError):
        db.begin()
    assert not db.in_transaction
    # A normal write transaction still works after the read block ends.
    with db:
        db["later"].insert({"place": "ok"})
    assert db["later"].count() == 1


def test_manual_close_is_idempotent(db, table):
    rt = db.read_transaction()
    handle = rt.__enter__()
    assert handle.is_active
    handle.close()
    handle.close()
    assert not handle.is_active
    assert not db.is_read_only
    rt.__exit__(None, None, None)
    table.insert({"place": "after manual close"})
