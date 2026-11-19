"""Evidence: what each early-exit pattern leaves behind on the connection pool.

Scenario: a QC sampler reads rows via dataset, stops at the first row that
fails validation, then must move on to the next batch. We run three read
patterns and inspect pool/connection state after each early exit.

Run: .venv/bin/python experiments/early_exit_pool_evidence.py
"""

import os
import tempfile
import threading

from sqlalchemy import event

import dataset

# Small pool, no overflow: leaks become visible immediately.
ENGINE_KW = dict(pool_size=2, max_overflow=0, pool_timeout=1)


def make_db():
    path = os.path.join(tempfile.mkdtemp(), "qc.db")
    db = dataset.Database(f"sqlite:///{path}", engine_kwargs=dict(ENGINE_KW))
    events = []
    event.listen(db.engine, "checkout", lambda *a: events.append("checkout"))
    event.listen(db.engine, "checkin", lambda *a: events.append("checkin"))
    table = db.create_table("items")
    # row id=3 fails QC; list (not generator) so insert_many can re-iterate
    table.insert_many([dict(id=i, ok=(i != 3)) for i in range(1, 51)])
    return db, table, events


def pool_state(db):
    pool = db.engine.pool
    return (
        f"pool[checkedout={pool.checkedout()} checkedin={pool.checkedin()}] "
        f"db.connections={len(db.connections)} "
        f"thread_conn_open={threading.get_ident() in db.connections}"
    )


def sample_normal_find(table):
    """Pattern A: plain find(), break at first bad row, no close."""
    for row in table.find(order_by="id"):
        if not row["ok"]:
            return row["id"]  # early exit, iterator abandoned
    return None


def sample_streamed_find(table):
    """Pattern B: find(_streamed=True), break at first bad row, no close."""
    for row in table.find(order_by="id", _streamed=True):
        if not row["ok"]:
            return row["id"]
    return None


def sample_normal_find_closed(table):
    """Pattern A-fixed: plain find(), explicit close on early exit."""
    it = table.find(order_by="id")
    try:
        for row in it:
            if not row["ok"]:
                return row["id"]
    finally:
        it.close()
    return None


def sample_streamed_find_closed(table):
    """Pattern B-fixed: streamed find(), explicit close on early exit."""
    it = table.find(order_by="id", _streamed=True)
    try:
        for row in it:
            if not row["ok"]:
                return row["id"]
    finally:
        it.close()
    return None


def banner(title):
    print(f"\n=== {title} ===")


# ---------------------------------------------------------------- A: plain find
banner("A. plain find(), break early, NO close  (5 batches)")
db, table, events = make_db()
print("before:", pool_state(db))
for batch in range(5):
    bad = sample_normal_find(table)
    print(f"batch {batch}: bad_row={bad}  {pool_state(db)}")
it = table.find(order_by="id")
for row in it:
    break  # abandon after first row
print(
    "abandoned iterator: result_proxy.closed="
    f"{it.result_proxy.closed}  thread-conn tx_open={db.executable.in_transaction()}"
)
it.close()
print(f"after it.close(): result_proxy.closed={it.result_proxy.closed}")
db.close()

# ------------------------------------------------------------- B: streamed find
banner("B. find(_streamed=True), break early, NO close  (pool_size=2)")
db, table, events = make_db()
print("before:", pool_state(db))
for batch in range(5):
    try:
        bad = sample_streamed_find(table)
        print(f"batch {batch}: bad_row={bad}  {pool_state(db)}")
    except Exception as exc:
        print(f"batch {batch}: POOL EXHAUSTED -> {type(exc).__name__}: {exc}")
        break
db.close()

# ------------------------------------------------- C: inside explicit transaction
banner("C. plain find() inside `with db:` write transaction, break early")
db, table, events = make_db()
with db:  # outer write transaction
    table.insert(dict(id=999, ok=True))
    bad = sample_normal_find(table)
    print(f"bad_row={bad}  in_tx={db.in_transaction}  {pool_state(db)}")
    print("after `with` exit the tx commits; connection released then")
print("after with:", pool_state(db), " row999=", table.find_one(id=999) is not None)
db.close()

# ------------------------------------------- D: streamed find inside transaction
banner("D. streamed find() inside `with db:` — separate connection evidence")
db, table, events = make_db()
with db:
    table.insert(dict(id=999, ok=True))  # uncommitted in outer tx
    via_tx = sum(1 for _ in table.find(id=999))  # same (thread) connection
    it = table.find(id=999, _streamed=True)  # brand-new pooled connection
    via_stream = sum(1 for _ in it)
    it.close()
    print(
        f"uncommitted row visible via tx-conn: {via_tx == 1}, "
        f"via stream-conn: {via_stream == 1}  (stream conn is NOT in the tx)"
    )
db.close()

# ------------------------------------------------------- E: fixed patterns
banner("E. same patterns WITH close()  (5 batches each)")
db, table, events = make_db()
print("before:", pool_state(db))
for batch in range(5):
    bad = sample_normal_find_closed(table)
    print(f"plain+close   batch {batch}: bad_row={bad}  {pool_state(db)}")
for batch in range(5):
    bad = sample_streamed_find_closed(table)
    print(f"stream+close  batch {batch}: bad_row={bad}  {pool_state(db)}")

# ------------------------------------ F: close() preserves outer write tx
banner("F. close() inside outer write transaction — tx survives, commits")
db, table, events = make_db()
with db:
    table.insert(dict(id=999, ok=True))
    bad = sample_normal_find_closed(table)
    print(f"bad_row={bad}  still_in_tx={db.in_transaction}")
    table.insert(dict(id=1000, ok=True))  # outer tx still usable
print("immediately after with:", pool_state(db))
print(
    "committed:",
    table.find_one(id=999) is not None and table.find_one(id=1000) is not None,
)
db.close()
