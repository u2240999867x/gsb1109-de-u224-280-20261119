"""窗口报表双结果集分析器。

用法（仓库根目录）:
    .venv/bin/python window_report/explain.py

做什么:
1. 用 sql-metadata 的 Parser 提取查询的结构字段角色（表、列、别名、子查询）。
2. 用一个小型窗口引擎在给定样本上枚举内层 ORDER BY sec 并列时的所有合法
   次序（有/无 id 第二排序键），计算每条路径的中间 rolling 与外层总和。
3. 核对同一次执行内输出组合的一致性（不允许跨路径拼接）。
4. 覆盖变种：内层只取上一行（空帧语义）、并列输入等值、只输出一行、
   无样本（只报结构）、缺少排序数据（共同次序不可确定）。
5. 把可核对的证据写入 window_report/catalog/。
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path

from sql_metadata import Parser

ROOT = Path(__file__).parent
CATALOG = ROOT / "catalog"

QUERY = (ROOT / "query.sql").read_text()
QUERY_SINGLE_ROW = (ROOT / "query_single_row.sql").read_text()

# 样本：id、设备、机构、秒数、数量。机构全部相同。
SAMPLE = [
    {"id": 1, "device": "A", "org": "O1", "sec": 1, "qty": 10},
    {"id": 2, "device": "A", "org": "O1", "sec": 1, "qty": 20},
    {"id": 3, "device": "B", "org": "O1", "sec": 2, "qty": 30},
    {"id": 4, "device": "A", "org": "O1", "sec": 3, "qty": 40},
]

REQUESTED_IDS = (1, 3)

# 内层帧：(起点前偏移, 终点前偏移)，CURRENT ROW 记 0。
FRAME_CURRENT_AND_PREV = (1, 0)  # ROWS BETWEEN 1 PRECEDING AND CURRENT ROW
FRAME_PREV_ONLY = (1, 1)  # ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING

FRAME_LABELS = {
    FRAME_CURRENT_AND_PREV: "ROWS BETWEEN 1 PRECEDING AND CURRENT ROW",
    FRAME_PREV_ONLY: "ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING",
}


def structural_roles() -> dict:
    """没有样本时也只报这些结构上的字段角色。"""
    parser = Parser(QUERY)
    variant_parser = Parser(QUERY_SINGLE_ROW)
    return {
        "source": "sql_metadata.Parser",
        "tables": parser.tables,
        "columns": parser.columns,
        "columns_aliases": parser.columns_aliases,
        "subqueries": parser.subqueries,
        "query_type": str(parser.query_type),
        "field_roles": {
            "id": "行标识；最外层 ORDER BY id DESC 只控制展示顺序，与内层并列排序无关",
            "device": "内层 w 的 PARTITION BY 键",
            "org": "外层 w 的 PARTITION BY 键（本样本中全部相同）",
            "sec": "两层窗口共用的 ORDER BY 键（内层 ROWS、外层 RANGE）",
            "qty": "内层 SUM 的度量",
            "rolling": "内层输出别名 = SUM(qty) OVER 内层 w；同时是外层 SUM 的度量",
            "rolling_total": "外层输出别名 = SUM(rolling) OVER 外层 w",
            "w": "两层各自命名的窗口，同名但作用域互不覆盖",
        },
        "single_row_variant": {
            "tables": variant_parser.tables,
            "subqueries": list(variant_parser.subqueries),
            "note": "WHERE id = 1 在最外层，过滤发生在两层窗口计算之后",
        },
    }


def _check_ordering_data(rows: list[dict]) -> list[int]:
    """返回缺少排序键（sec）的行 id。缺失时共同次序不可确定。"""
    return [r["id"] for r in rows if r["sec"] is None]


def inner_window(rows: list[dict], frame: tuple[int, int], tie_order: list[int]):
    """内层 w：PARTITION BY device ORDER BY sec（+ tie_order 决胜）ROWS 帧。

    返回 {id: {rolling, inputs, ...}} 与每个分区的实际行序。
    空帧 => rolling 为 None，inputs 为空列表：空帧没有任何输入，
    NULL 不是原行的零贡献。
    """
    by_device: dict[str, list[dict]] = {}
    for row in rows:
        by_device.setdefault(row["device"], []).append(row)
    rank = {rid: pos for pos, rid in enumerate(tie_order)}
    result: dict[int, dict] = {}
    partition_orders: dict[str, list[int]] = {}
    for device, members in by_device.items():
        ordered = sorted(members, key=lambda r: (r["sec"], rank.get(r["id"], 0)))
        partition_orders[device] = [r["id"] for r in ordered]
        last = len(ordered) - 1
        for pos, row in enumerate(ordered):
            lo, hi = pos - frame[0], pos - frame[1]
            frame_rows = [ordered[j] for j in range(max(lo, 0), min(hi, last) + 1)]
            result[row["id"]] = {
                "id": row["id"],
                "device": device,
                "sec": row["sec"],
                "rolling": sum(fr["qty"] for fr in frame_rows) if frame_rows else None,
                "inputs": [fr["id"] for fr in frame_rows],
                "empty_frame": not frame_rows,
            }
    return result, partition_orders


def outer_window(rows: list[dict], inner: dict[int, dict]) -> dict[int, dict]:
    """外层 w：PARTITION BY org ORDER BY sec RANGE 1 PRECEDING..CURRENT ROW。

    SUM 忽略 NULL；每个输出记录经过哪一个中间 id、真正参与的输入行。
    """
    by_org: dict[str, list[dict]] = {}
    for row in rows:
        by_org.setdefault(row["org"], []).append(row)
    outputs: dict[int, dict] = {}
    for members in by_org.values():
        for row in members:
            sec = row["sec"]
            frame_rows = [m for m in members if sec - 1 <= m["sec"] <= sec]
            via = []
            values = []
            for member in sorted(frame_rows, key=lambda m: m["id"]):
                mid = inner[member["id"]]
                via.append(
                    {
                        "inner_id": member["id"],
                        "rolling": mid["rolling"],
                        "inputs": mid["inputs"],
                        "empty_frame": mid["empty_frame"],
                    }
                )
                if mid["rolling"] is not None:
                    values.append(mid["rolling"])
            usage = Counter(iid for v in via for iid in v["inputs"])
            outputs[row["id"]] = {
                "id": row["id"],
                "sec": sec,
                "rolling_total": sum(values) if values else None,
                "via": via,
                "distinct_inputs": sorted(usage),
                "reused_inputs": sorted(k for k, c in usage.items() if c > 1),
            }
    return outputs


def run_path(rows: list[dict], frame: tuple[int, int], tie_order: list[int],
             label: str) -> dict:
    inner, partition_orders = inner_window(rows, frame, tie_order)
    outputs = outer_window(rows, inner)
    return {
        "path": label,
        "inner_frame": FRAME_LABELS[frame],
        "inner_tiebreaker": tie_order,
        "inner_partition_orders": partition_orders,
        "intermediate": [inner[rid] for rid in sorted(inner)],
        "outputs": [outputs[rid] for rid in sorted(outputs)],
        "requested": {str(rid): outputs[rid]["rolling_total"] for rid in REQUESTED_IDS},
    }


def check_consistency(paths: list[dict]) -> dict:
    """同一次执行内层次序固定，输出组合只能整条来自同一路径。"""
    combos = sorted(
        tuple(p["requested"][str(rid)] for rid in REQUESTED_IDS) for p in paths
    )
    values = {str(rid): sorted({p["requested"][str(rid)] for p in paths})
              for rid in REQUESTED_IDS}
    mixed = [
        combo for combo in
        [(a, b) for a in values[str(REQUESTED_IDS[0])]
         for b in values[str(REQUESTED_IDS[1])]]
        if list(combo) not in combos
    ]
    return {
        "allowed_combos_same_execution": [list(c) for c in combos],
        "forbidden_mixed_combos": [list(c) for c in mixed],
        "rule": "内层并列次序在一次执行内固定，不能把一路径的 id=1 与另一路径的 id=3 拼成同一次输出",
    }


def scenario_main() -> dict:
    path_a = run_path(SAMPLE, FRAME_CURRENT_AND_PREV, [1, 2], "A: 内层第二顺序 id ASC")
    path_b = run_path(SAMPLE, FRAME_CURRENT_AND_PREV, [2, 1], "B: 无第二顺序时的另一合法次序")
    return {
        "scenario": "主查询：内层当前行+上一行，查询同一次执行里的 id=1 和 id=3",
        "paths": [path_a, path_b],
        "consistency": check_consistency([path_a, path_b]),
    }


def scenario_prev_only() -> dict:
    path_a = run_path(SAMPLE, FRAME_PREV_ONLY, [1, 2], "A: 内层第二顺序 id ASC")
    path_b = run_path(SAMPLE, FRAME_PREV_ONLY, [2, 1], "B: 无第二顺序时的另一合法次序")
    return {
        "scenario": "变种：内层只取上一行（ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING）",
        "empty_frame_rule": "空帧没有任何输入；rolling 为 NULL 不是原行的零贡献，外层 SUM 忽略 NULL",
        "paths": [path_a, path_b],
        "consistency": check_consistency([path_a, path_b]),
    }


def scenario_equal_qty() -> dict:
    rows = [dict(r, qty=10) if r["id"] in (1, 2) else dict(r) for r in SAMPLE]
    path_a = run_path(rows, FRAME_CURRENT_AND_PREV, [1, 2], "A: 内层第二顺序 id ASC")
    path_b = run_path(rows, FRAME_CURRENT_AND_PREV, [2, 1], "B: 无第二顺序时的另一合法次序")
    numbers_coincide = path_a["requested"] == path_b["requested"]
    return {
        "scenario": "变种：两条并列输入（id=1、id=2）数量都改为 10",
        "sample": rows,
        "numbers_coincide": numbers_coincide,
        "note": "结果数可能相同，但中间 rolling 挂在不同 id 上，目录仍保留两条计算路径的证据",
        "paths": [path_a, path_b],
    }


def scenario_single_row(main: dict) -> dict:
    displayed = []
    for path in main["paths"]:
        shown = [o for o in path["outputs"] if o["id"] == 1]
        hidden = [o for o in path["outputs"] if o["id"] != 1]
        displayed.append(
            {
                "path": path["path"],
                "displayed": shown,
                "computed_but_not_displayed": hidden,
            }
        )
    return {
        "scenario": "变种：最外层 WHERE id = 1，只输出一行",
        "note": "过滤在两层窗口之外；另一个输出未展示，也不改变前面层级已有的窗口与中间值",
        "paths": displayed,
    }


def scenario_missing_order_data() -> dict:
    rows = [dict(r, sec=None) if r["id"] == 2 else dict(r) for r in SAMPLE]
    missing = _check_ordering_data(rows)
    return {
        "scenario": "样本缺少一部分排序数据（id=2 的 sec 未知）",
        "missing_order_rows": missing,
        "verdict": "共同次序不可确定：无法枚举合法路径，也不按输入文本次序定档",
        "structural_only": structural_roles()["field_roles"],
    }


def write_catalog() -> None:
    CATALOG.mkdir(exist_ok=True)
    structural = structural_roles()
    (CATALOG / "structural_roles.json").write_text(
        json.dumps(structural, ensure_ascii=False, indent=2))

    main = scenario_main()
    (CATALOG / "main_path_a.json").write_text(
        json.dumps(main["paths"][0], ensure_ascii=False, indent=2))
    (CATALOG / "main_path_b.json").write_text(
        json.dumps(main["paths"][1], ensure_ascii=False, indent=2))
    (CATALOG / "main_consistency.json").write_text(
        json.dumps(main["consistency"], ensure_ascii=False, indent=2))

    (CATALOG / "variant_prev_only.json").write_text(
        json.dumps(scenario_prev_only(), ensure_ascii=False, indent=2))
    (CATALOG / "variant_equal_qty.json").write_text(
        json.dumps(scenario_equal_qty(), ensure_ascii=False, indent=2))
    (CATALOG / "variant_single_row.json").write_text(
        json.dumps(scenario_single_row(main), ensure_ascii=False, indent=2))
    (CATALOG / "missing_order_data.json").write_text(
        json.dumps(scenario_missing_order_data(), ensure_ascii=False, indent=2))

    # 自检：主场景两组结果与题目一致，且混合组合被禁止。
    assert main["paths"][0]["requested"] == {"1": 40, "3": 70}
    assert main["paths"][1]["requested"] == {"1": 50, "3": 80}
    assert [40, 80] in main["consistency"]["forbidden_mixed_combos"]
    assert [50, 70] in main["consistency"]["forbidden_mixed_combos"]
    print("catalog written to", CATALOG)
    print("main combos:", main["consistency"]["allowed_combos_same_execution"])


if __name__ == "__main__":
    write_catalog()
