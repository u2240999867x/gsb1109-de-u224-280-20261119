"""物流入库门禁：用 datajudge 分别检查"已发货缺号"和"仓内编号撞号"。

背景：
- 接口会重报同一货件的轨迹，源表（shipment_reports）只追加不覆盖，全库历史保留不删。
- 核对口径：按接口给的货件键（shipment_key）+ 可比较版本（version）取每个货件的
  最新版本；完全相同的重报（同批重复）去重；同键同版本但内容不一致的"并版冲突"
  不任选一份，列入冲突清单并从发货、编号两种分母中排除。
- 缺号阈值只以"当前已发"的货件为分母；撞号检查只看"已发且有编号"的货件，
  按仓报告参与量和有冲突的代表货件。
- 未开启这一口径的既有约束保持原义（直接作用于原始历史表，不做任何去重）。

运行：python logistics_gate.py
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

import sqlalchemy as sa

from datajudge import WithinRequirement

# 缺号阈值：当前已发货件中允许的最大缺号比例
MISSING_NO_THRESHOLD = 0.15

# 当前状态口径：最新版本 + 同批重复去重 + 并版冲突剔除
CURRENT_STATE_CTE = """
WITH ranked AS (
    SELECT
        report_id, shipment_key, version, status, tracking_no, warehouse,
        MAX(version) OVER (PARTITION BY shipment_key) AS max_version
    FROM shipment_reports
),
latest AS (
    SELECT * FROM ranked WHERE version = max_version
),
parallel_version_conflicts AS (
    SELECT shipment_key
    FROM latest
    GROUP BY shipment_key
    HAVING COUNT(DISTINCT
        status || '|' || COALESCE(tracking_no, '<null>') || '|' || warehouse
    ) > 1
),
current_state AS (
    SELECT DISTINCT shipment_key, version, status, tracking_no, warehouse
    FROM latest
    WHERE shipment_key NOT IN (SELECT shipment_key FROM parallel_version_conflicts)
)
"""

# 缺号检查数据源：当前已发货件（分母只含当前已发）
CURRENT_SHIPPED_QUERY = (
    CURRENT_STATE_CTE
    + """
SELECT shipment_key, tracking_no
FROM current_state
WHERE status = 'shipped'
"""
)

# 撞号检查数据源：当前已发且有编号的货件
CURRENT_NUMBERED_QUERY = (
    CURRENT_STATE_CTE
    + """
SELECT warehouse, tracking_no
FROM current_state
WHERE status = 'shipped' AND tracking_no IS NOT NULL
"""
)

# 并版冲突清单
CONFLICT_LIST_QUERY = (
    CURRENT_STATE_CTE
    + """
SELECT shipment_key, version, status, tracking_no, warehouse
FROM latest
WHERE shipment_key IN (SELECT shipment_key FROM parallel_version_conflicts)
ORDER BY shipment_key, report_id
"""
)

# 按仓参与量（已发且有编号）
WAREHOUSE_PARTICIPATION_QUERY = (
    CURRENT_STATE_CTE
    + """
SELECT warehouse, COUNT(DISTINCT shipment_key) AS n_shipments
FROM current_state
WHERE status = 'shipped' AND tracking_no IS NOT NULL
GROUP BY warehouse
ORDER BY warehouse
"""
)

# 仓内撞号的代表货件
COLLISION_QUERY = (
    CURRENT_STATE_CTE
    + """
SELECT warehouse, tracking_no, GROUP_CONCAT(DISTINCT shipment_key) AS shipments
FROM current_state
WHERE status = 'shipped' AND tracking_no IS NOT NULL
GROUP BY warehouse, tracking_no
HAVING COUNT(DISTINCT shipment_key) > 1
ORDER BY warehouse, tracking_no
"""
)


def create_schema(engine: sa.engine.Engine) -> None:
    with engine.begin() as conn:
        conn.execute(
            sa.text(
                """
                CREATE TABLE shipment_reports (
                    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    shipment_key TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    status TEXT NOT NULL,
                    tracking_no TEXT,
                    warehouse TEXT NOT NULL
                )
                """
            )
        )


def report(
    conn: sa.engine.Connection,
    shipment_key: str,
    version: int,
    status: str,
    tracking_no: str | None,
    warehouse: str,
) -> None:
    """追加一条轨迹报告；源表只插不删不改，全库历史保留。"""
    conn.execute(
        sa.text(
            """
            INSERT INTO shipment_reports
                (shipment_key, version, status, tracking_no, warehouse)
            VALUES (:key, :version, :status, :tracking_no, :warehouse)
            """
        ),
        {
            "key": shipment_key,
            "version": version,
            "status": status,
            "tracking_no": tracking_no,
            "warehouse": warehouse,
        },
    )


def run_gate(engine: sa.engine.Engine, label: str) -> dict[str, bool]:
    """用 datajudge 分别执行缺号、撞号两项检查，并输出按仓报告。"""
    print(f"\n===== {label} =====")

    # 并版冲突清单：不任选一份，列出并排除
    with engine.connect() as conn:
        conflicts = conn.execute(sa.text(CONFLICT_LIST_QUERY)).all()
        participation = conn.execute(sa.text(WAREHOUSE_PARTICIPATION_QUERY)).all()
        collisions = conn.execute(sa.text(COLLISION_QUERY)).all()
        n_history = conn.execute(
            sa.text("SELECT COUNT(*) FROM shipment_reports")
        ).scalar_one()

    print(f"历史表总行数（全量保留不删）: {n_history}")
    if conflicts:
        print("并版冲突（同键同版不同内容，整票排除出两种分母）:")
        for row in conflicts:
            print(f"  {row.shipment_key} v{row.version} "
                  f"status={row.status} tracking_no={row.tracking_no} "
                  f"warehouse={row.warehouse}")
    else:
        print("并版冲突: 无")

    print("按仓参与量（已发且有编号）:")
    for row in participation:
        print(f"  仓 {row.warehouse}: {row.n_shipments} 票")

    if collisions:
        print("仓内撞号（代表货件）:")
        for row in collisions:
            print(f"  仓 {row.warehouse} 编号 {row.tracking_no}: {row.shipments}")
    else:
        print("仓内撞号: 无")

    # 检查一：已发货缺号（分母 = 当前已发货件）
    missing_req = WithinRequirement.from_raw_query(
        CURRENT_SHIPPED_QUERY,
        name="current_shipped",
        columns=["shipment_key", "tracking_no"],
    )
    missing_req.add_max_null_fraction_constraint(
        "tracking_no",
        max_null_fraction=MISSING_NO_THRESHOLD,
        name="已发货缺号比例",
    )

    # 检查二：仓内编号撞号（只看已发且有编号；异仓同号允许）
    collision_req = WithinRequirement.from_raw_query(
        CURRENT_NUMBERED_QUERY,
        name="current_numbered",
        columns=["warehouse", "tracking_no"],
    )
    collision_req.add_uniqueness_constraint(
        columns=["warehouse", "tracking_no"],
        name="仓内编号唯一",
    )

    outcomes: dict[str, bool] = {}
    for check_name, req in (
        ("已发货缺号比例", missing_req),
        ("仓内编号唯一", collision_req),
    ):
        for result in req.test(engine):
            outcomes[check_name] = result.outcome
            status_text = "通过" if result.outcome else "未通过"
            print(f"[{status_text}] {check_name}")
            if not result.outcome:
                print(f"  {result.failure_message}")
    return outcomes


def main() -> None:
    engine = sa.create_engine("sqlite://")
    create_schema(engine)

    # 第一批：接口重报、同批重复、并版冲突、两个真实已发货件（其一缺号）
    with engine.begin() as conn:
        report(conn, "A", 1, "not_shipped", None, "WH1")   # A 旧报告：未发、无号
        report(conn, "A", 2, "shipped", "T", "WH1")        # A 新报告：已发、有号 T
        report(conn, "A", 2, "shipped", "T", "WH1")        # A 新报告的重报（同批重复）
        report(conn, "B", 1, "shipped", "T", "WH1")        # B 同仓已发，同号 T
        report(conn, "C", 1, "shipped", "T", "WH2")        # C 异仓用 T，应当允许
        report(conn, "D", 1, "shipped", "TD1", "WH1")      # D 并版冲突：同版本
        report(conn, "D", 1, "not_shipped", "TD2", "WH1")  #   不同状态和编号
        report(conn, "E", 1, "shipped", "TE", "WH1")       # 真实已发，有号
        report(conn, "F", 1, "shipped", None, "WH2")       # 真实已发，缺号

    round1 = run_gate(engine, "第一批核对")

    # 首轮 T 撞号应发生在两个真实货件 A、B 之间，而非 A 的重报所致
    with engine.connect() as conn:
        collisions = conn.execute(sa.text(COLLISION_QUERY)).all()
    assert [(r.warehouse, r.tracking_no) for r in collisions] == [("WH1", "T")]
    assert set(collisions[0].shipments.split(",")) == {"A", "B"}

    # 更新：货件 B 的撤销报告（更高版本），重新核对
    with engine.begin() as conn:
        report(conn, "B", 2, "cancelled", None, "WH1")

    round2 = run_gate(engine, "B 撤销后重新核对")

    # B 撤销后从活动撞号移除
    with engine.connect() as conn:
        collisions = conn.execute(sa.text(COLLISION_QUERY)).all()
    assert collisions == []

    # 未开启本口径的既有约束保持原义：直接作用于原始历史表，
    # 看到的仍是全部历史行（含旧状态与同批重复），不做任何去重。
    raw_req = WithinRequirement.from_table(
        db_name="main", schema_name=None, table_name="shipment_reports"
    )
    raw_req.add_n_rows_equality_constraint(n_rows=10, name="历史表全量行数")
    (raw_result,) = raw_req.test(engine)
    assert raw_result.outcome, raw_result.failure_message
    print("\n[通过] 历史表全量行数 = 10（既有约束原义不变，历史保留不删）")

    print("\n汇总:")
    print(f"  第一轮: {round1}")
    print(f"  第二轮: {round2}")


if __name__ == "__main__":
    main()
