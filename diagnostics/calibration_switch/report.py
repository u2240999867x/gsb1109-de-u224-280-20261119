"""Render the calibration-switch diagnosis as reviewable Markdown or JSON."""

from __future__ import annotations

import json

from .model import field_divergence, summarize


STATE_LABELS = {
    "transport": "传输中（闸门暂存）",
    "merge_slot": "合并节点仍持有",
    "settled": "已确认归零",
    "merge_rejected": "尝试进入合并失败、引用未释放",
    "orphaned_ref": "引用未归零且无归属",
    "unadmitted_zero_ref": "无可用引用且永远等不到确认",
}

CASE_LABELS = {
    "transit": "T1：B1 停在合并节点之前",
    "merged_continue": "T2a：B1 已到达合并节点，断线报错后仍接入 C",
    "merged_abort": "T2b：B1 已到达合并节点，断线报错后中止切换",
    "control": "对照：不换线，仅用 B2 替换旧校准值",
    "reversed": "对照：同一拓扑颠倒 A1/B1 到达顺序",
}

CASE_SHORT = {
    "transit": "T1",
    "merged_continue": "T2a",
    "merged_abort": "T2b",
    "control": "对照-替换",
    "reversed": "对照-颠倒",
}

FIELD_KINDS = {"source_read", "switch_request", "ack", "db_write"}


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _event_at(bench, kind):
    return next(
        (event for event in bench.trace.local if event["kind"] == kind),
        None,
    )


def _ownership_at(bench, boundary_kind):
    event = _event_at(bench, boundary_kind)
    if event is None:
        return []
    gate_rids = set(event.get("gate_held_rids") or [])
    slot_by_rid = {}
    for index, metadata in enumerate(event.get("metadata_rids") or []):
        for item in metadata or []:
            slot_by_rid[item] = index
    rows = []
    for rid, record in bench.trace.records.items():
        if rid in gate_rids:
            state = "传输中（闸门暂存）"
            owner = "gate buffer"
        elif rid in slot_by_rid:
            state = "合并节点仍持有"
            owner = f"merge slot {slot_by_rid[rid]}"
        elif record.ack:
            state = "已确认归零"
            owner = "none"
        else:
            state = "尚未被该边界观察"
            owner = "none"
        rows.append(
            {
                "rid": rid,
                "source": record.source,
                "state": state,
                "owner_local": owner,
            }
        )
    return rows


def _timeline_rows(result, include_values=False):
    bench = result["bench"]
    events = [(event["t"], "field", event) for event in bench.trace.field]
    events += [(event["t"], "local", event) for event in bench.trace.local]
    events.sort(key=lambda item: (item[0], item[1] != "field"))

    rows = []
    for t, scope, event in events:
        kind = event["kind"]
        detail = _detail(event, include_values)
        rows.append(
            {
                "t": t,
                "scope": scope,
                "evidence": "现场可见" if kind in FIELD_KINDS else "仅本地复现",
                "kind": kind,
                "detail": detail,
            }
        )
    return rows


def _detail(event, include_values=False):
    kind = event["kind"]
    if kind == "source_read":
        return {"source": event["source"], "rid": event["rid"]}
    if kind == "source_value_local":
        detail = {"source": event["source"], "rid": event["rid"]}
        if include_values:
            detail["value"] = event["value"]
        return detail
    if kind == "switch_request":
        return {"request": event["request"]}
    if kind == "ack":
        return {"source": event["source"], "rid": event["rid"]}
    if kind == "ack_local":
        return {"rid": event["rid"], "count": event["count"]}
    if kind == "db_write":
        return {"rids": event["rids"], "arity": event["arity"]}
    if kind == "db_write_local":
        detail = {"rids": event["rids"], "arity": event["arity"]}
        if include_values:
            detail["values"] = event["values"]
        return detail
    if kind.startswith("transport_"):
        detail = {
            "gate": event["gate"],
            "rids": event["rids"],
            "downstreams": event.get("downstreams"),
        }
        if "counts" in event:
            detail["ref_counts_local"] = event["counts"]
        if event.get("error"):
            detail["error"] = event["error"]
        return detail
    if kind in ("merge_update_begin", "merge_update_end", "merge_update_error"):
        detail = {
            "who": event["who"],
            "rids": event["rids"],
            "upstreams": event["upstreams"],
            "last_present": event["last_present"],
            "metadata_rids": event["metadata_rids"],
            "missing": event["missing"],
            "consistent": event["consistent"],
        }
        if event.get("error"):
            detail["error"] = event["error"]
        return detail
    if kind.startswith("boundary_"):
        return {
            "upstreams": event["upstreams"],
            "last_present": event["last_present"],
            "metadata_rids": event["metadata_rids"],
            "missing": event["missing"],
            "consistent": event["consistent"],
        }
    if kind in (
        "switch_begin",
        "disconnect_ok",
        "disconnect_error",
        "connect_ok",
        "connect_error",
        "switch_abort",
        "switch_end",
    ):
        detail = {
            "upstreams": event["upstreams"],
            "last_present": event["last_present"],
            "metadata_rids": event["metadata_rids"],
            "missing": event["missing"],
            "consistent": event["consistent"],
        }
        if event.get("on_error"):
            detail["on_error"] = event["on_error"]
        if event.get("error"):
            detail["error"] = event["error"]
        if "result" in event:
            detail["result"] = event["result"]
        return detail
    if kind in ("source_emit_error", "transport_release_error"):
        return {
            "rids": event.get("rids"),
            "error": event.get("error"),
            "message": event.get("message"),
        }
    if kind == "transport_release_empty":
        return {"gate": event["gate"]}
    return {}


def _topology_table(result):
    bench = result["bench"]
    lines = [
        "| 边界 | upstreams | last 有值槽位 | metadata 引用 | missing | 槽位/上游 | 一致 |",
        "| --- | --- | --- | --- | --- | --- | --- |",
    ]
    for event in bench.trace.local:
        if not event["kind"].startswith("boundary_"):
            continue
        lines.append(
            "| {kind} | {up} | {last} | {md} | {missing} | {slots}/{ups} | {ok} |".format(
                kind=event["kind"],
                up=_json(event["upstreams"]),
                last=_json(event["last_present"]),
                md=_json(event["metadata_rids"]),
                missing=_json(event["missing"]),
                slots=event["slot_count"],
                ups=event["upstream_count"],
                ok="是" if event["consistent"] else "否",
            )
        )
    return lines


def _record_table(rows):
    header = [
        "记录", "来源", "本地引用计数", "收到确认", "确认时无下游",
        "经过闸门", "已离开闸门", "尝试合并", "合并槽位", "库行", "归属",
    ]
    lines = [
        "| " + " | ".join(header) + " |",
        "| --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for row in rows:
        cells = [
            row["rid"],
            row["source"],
            str(row["ref_count_local"]),
            "是" if row["ack"] else "否",
            "是" if row["ack_without_merge"] else "否",
            "是" if row["gate_ever"] else "否",
            "是" if row["left_gate"] else "否",
            "是" if row["merge_attempted"] else "否",
            str(row["merge_slots_local"] or "-"),
            str(row["db_rows"] or "-"),
            STATE_LABELS[row["state"]],
        ]
        lines.append("| " + " | ".join(cells) + " |")
    return lines


def _db_table(summary):
    reached = {}
    for row in summary["db"]:
        for rid in row["rids"]:
            reached.setdefault(rid, []).append(row)
    lines = [
        "| 记录 | 是否入库 | 入库形态 |",
        "| --- | --- | --- |",
    ]
    for rid in ("A1", "B1", "C1", "A2", "B2"):
        rows = reached.get(rid, [])
        if rows:
            shape = ", ".join(
                "({})".format(", ".join(row["rids"])) for row in rows
            )
            arity = ", ".join(str(row["arity"]) for row in rows)
            lines.append(f"| {rid} | 是 | {shape}；元组长度 {arity} |")
        else:
            lines.append(f"| {rid} | 否 | - |")
    return lines


def _timeline_table(rows):
    lines = [
        "| t | 证据范围 | 事件 | 关键内容 |",
        "| ---: | --- | --- | --- |",
    ]
    for row in rows:
        lines.append(
            "| {t} | {evidence} | {kind} | `{detail}` |".format(
                t=row["t"],
                evidence=row["evidence"],
                kind=row["kind"],
                detail=_json(row["detail"]),
            )
        )
    return lines


def _switch_story(summary):
    switch = summary["switch"]
    if switch is None:
        return "未执行切换。"
    if switch["ok"]:
        return "断开与接入均成功，拓扑保持一致。"
    return "切换在 `{}` 阶段失败：`{}`。".format(
        switch["stage"], switch["error"]
    )


def _case_section(result, summaries, include_values=False):
    summary = summaries[result["case"]]
    lines = ["### {}".format(CASE_LABELS[result["case"]]), ""]
    lines.append(_switch_story(summary))
    lines.append("")
    state = summary["switch_state"]
    if state is not None:
        lines.append(
            "切换前后拓扑变化（本地观察）：`{}`".format(_json(state["delta"]))
        )
        lines.append("")
    lines.append("拓扑边界：")
    lines.append("")
    lines.extend(_topology_table(result))
    lines.append("")
    lines.append("记录最终归属（槽位与引用计数为本地复现值）：")
    lines.append("")
    lines.extend(_record_table(summary["records"]))
    lines.append("")
    lines.append("数据库结果：")
    lines.append("")
    lines.extend(_db_table(summary))
    lines.append("")
    lines.append("因果时间线：")
    lines.append("")
    lines.extend(_timeline_table(_timeline_rows(result, include_values)))
    lines.append("")
    return lines


def render_markdown(results, include_values=False):
    summaries = {name: summarize(result) for name, result in results.items()}
    lines = [
        "# 校准流切换本地诊断报告",
        "",
        "本报告由 `python -m diagnostics.calibration_switch` 生成，",
        "只使用当前 `streamz` 实现，不修改库行为。",
        "",
        "## 证据边界",
        "",
        "- 现场可保留：源已读取、下游写入、确认回调，以及切换请求编号。",
        "- 仅本地复现：`combine_latest` 的 `upstreams`、`last`、`metadata`、",
        "  `missing`、闸门暂存队列、`RefCounter` 计数、数据库行形态。",
        "- 时间线中“现场可见”以外的事件不能当作现场证据。",
        "- 源值内容不进入报告；记录只以来源和引用编号出现。",
        "",
        "## 判别规则",
        "",
        "1. `传输中（闸门暂存）`：记录仍在闸门里，合并节点尚未收到；合法等待。",
        "2. `合并节点仍持有`：记录在某槽位的 `last/metadata` 中；合法持有。",
        "3. `引用未归零且无归属`：槽位已弹出或从未建立，但引用仍大于 0；",
        "   切换半完成造成的泄漏。",
        "4. `无可用引用且永远等不到确认`：引用为 0，且没有下游能再释放它；",
        "   既不是断线，也不是已入库。",
        "",
        "不能只用引用计数是否为 0 判断断线，也不能让一次断线报错",
        "解释此后全部停滞。",
        "",
        "## 两轮相同的观测",
        "",
        "在两轮里，切换请求之前的现场记录完全相同：`A1` 已读、`B1` 已读、",
        "切换请求号 `{}`，随后 `B1` 确认未回、数据库无新行。".format(
            _request_id(summaries["transit"])
        ),
        "所以仅凭切换前的现场记录，无法区分“校准数据还在传输”与",
        "“合并节点已经合法持有上一版”。",
        "",
        "### 最早哪条实证足以定案",
        "",
        "最早的一条充分实证是本地复现中 `disconnect` 返回/抛出后的拓扑快照：",
        "",
        "- T1：`disconnect_ok` 之后 `{}`，槽位数与上游数一致；".format(
            _json(_switch_point(summaries["transit"], "disconnect_ok"))
        ),
        "- T2：`disconnect_error` 之后 `{}`，槽位已被弹出、上游仍保留 B，".format(
            _json(_switch_point(summaries["merged_continue"],
                                "disconnect_error"))
        ),
        "  `consistent=False`；旧 B 从此没有可用槽位。",
        "",
        "这条实证的局限是：它只在本地复现里可得。现场若只留下切换请求号，",
        "必须先在本地复现同一拓扑，才能用这条判据；不能反过来宣称现场曾观察到它。",
        "",
        "若严格只用现场记录，最早的分叉出现在 `A2` 与合并节点相遇之后。",
        "比较时排除只在 T2 出现的 `B2`，并把确认按集合比较，避免回调先后",
        "被误读为因果：",
        "",
        "- T1：`A2` 写出 `{}`；".format(
            _json(summaries["transit"]["earliest_a2_write"])
        ),
        "- T2a：`A2` 不写出（{}）；".format(
            _json(summaries["merged_continue"]["earliest_a2_write"])
        ),
        "- T2b：`A2` 写出 `{}`。".format(
            _json(summaries["merged_abort"]["earliest_a2_write"])
        ),
        "",
        "完整现场差异（仅列出不同的类别）：",
        "",
        "- T2a 相对 T1：`{}`；".format(
            _json(field_divergence(
                results["transit"], results["merged_continue"]
            ))
        ),
        "- T2b 相对 T1：`{}`。".format(
            _json(field_divergence(results["transit"], results["merged_abort"]))
        ),
        "这些分叉都晚于切换，因此不能用来反推切换前 B1 的位置。",
        "",
        "## 场景结果",
        "",
    ]
    for key in (
        "transit",
        "merged_continue",
        "merged_abort",
        "control",
        "reversed",
    ):
        lines.extend(_case_section(results[key], summaries, include_values))
    lines.extend(_conclusions(results, summaries))
    return "\n".join(lines)


def _request_id(summary):
    return next(
        (
            item.get("request")
            for item in summary["field"]
            if item["kind"] == "switch_request"
        ),
        None,
    )


def _switch_point(summary, kind):
    event = next(
        (item for item in summary["local"] if item["kind"] == kind), None
    )
    if event is None:
        return None
    return {
        "upstreams": event["upstreams"],
        "last_present": event["last_present"],
        "metadata_rids": event["metadata_rids"],
        "missing": event["missing"],
        "consistent": event["consistent"],
    }


def _conclusions(results, summaries):
    transit = summaries["transit"]
    merged_continue = summaries["merged_continue"]
    merged_abort = summaries["merged_abort"]
    control = summaries["control"]
    reversed_case = summaries["reversed"]
    same_prefix = (
        transit["field_prefix_before_switch"]
        == merged_continue["field_prefix_before_switch"]
        == merged_abort["field_prefix_before_switch"]
    )
    return [
        "## 有限结论",
        "",
        "- 两轮切换前的现场观测{}。".format(
            "完全相同" if same_prefix else "不完全相同"
        ),
        "- T1 是合法等待：`B1` 一直只在闸门中；切换成功后迟到的 `B1`",
        "  没有下游可进入，但能被释放并收到确认；`C1` 与 `A2` 组成",
        "  `(A2,C1)` 入库。",
        "- T2 是切换后的状态不一致：`B1` 已进入合并槽位，断开在",
        "  `missing.remove` 处抛 `KeyError`，但此前已弹出 `last/metadata`，",
        "  槽位数与上游数从此不一致；`B1` 没有槽位且引用泄漏。",
        "- T2a 继续接入 C 后，`C1` 在 `metadata[idx]` 处触发 `IndexError`，",
        "  `A2` 虽更新第 0 槽，但因 `missing={'C'}` 永不写出；库无新行。",
        "  这是与断线报错不同的第二处故障点。",
        "- T2b 中止切换后，`C1` 没有下游：引用为 0、确认永不回来；",
        "  `A2` 写出退化的单元组 `(A2,)`，属于长度不一致的下游形态。",
        "- 对照运行说明：不换线时 `B2` 合法替换 `B1`，`B1` 归零并确认，",
        "  `A2` 写出 `(A2,B2)`；因此“旧值仍被合并节点持有”不是故障。",
        "- 顺序颠倒说明：`B1` 先到、`A1` 后到时，`(A1,B1)` 可以先入库；",
        "  随后同一次半完成断开仍会让 `B1` 泄漏。已有库行不等于全部确认，",
        "  确认缺失也不等于没有入库；同一记录可以同时“已入库”和“引用泄漏”。",
        "- `B2` 的确认回来只证明它被释放，不证明它被合并节点接收；",
        "  T2a/T2b 中 `B2` 的下游都为空。",
        "",
        "## 逐笔去向",
        "",
        "| 场景 | A1 | B1 | C1 | A2 | B2 |",
        "| --- | --- | --- | --- | --- | --- |",
    ] + [
        "| {} | {} |".format(
            CASE_SHORT[key],
            " | ".join(
                _record_outcome(summaries[key], rid)
                for rid in ("A1", "B1", "C1", "A2", "B2")
            ),
        )
        for key in ("transit", "merged_continue", "merged_abort",
                    "control", "reversed")
    ] + [
        "",
        "## 值班顺序",
        "",
        "1. 先查切换：现场通常只有请求号、没有异常栈；优先复现切换时刻拓扑。",
        "   若上游数不等于槽位数，或断开后 `missing` 与槽位不符，",
        "   按切换状态不一致处理。",
        "2. 再查校准输入：拓扑一致且 `B1` 只在闸门中时，先等传输或检查闸门，",
        "   不要把未确认当作断线。",
        "3. 最后逐条核对记录归属；引用计数、单次断线报错都只能解释局部。",
        "",
        "## 复现",
        "",
        "```bash",
        "python -m diagnostics.calibration_switch",
        "python -m diagnostics.calibration_switch --json",
        "python -m pytest diagnostics/calibration_switch/tests -q",
        "```",
        "",
    ]


def _record_outcome(summary, rid):
    row = next(
        (item for item in summary["records"] if item["rid"] == rid), None
    )
    if row is None:
        return "未产生"
    write = _write_state(summary, rid)
    if write == "否":
        return STATE_LABELS[row["state"]]
    return "{}；{}".format(write, STATE_LABELS[row["state"]])


def _write_state(summary, rid):
    for row in summary["db"]:
        if rid in row["rids"]:
            return "已写入"
    return "未写入"


def render_json(results, include_values=False):
    summaries = {name: summarize(result) for name, result in results.items()}
    payload = {
        "tool": "calibration_switch",
        "streamz_version": _streamz_version(),
        "evidence_scopes": {
            "field": [
                "source_read",
                "switch_request",
                "db_write",
                "ack",
            ],
            "local_only": [
                "gate internal buffer",
                "combine_latest upstreams/last/metadata/missing",
                "RefCounter counts",
                "database row shape",
            ],
        },
        "cases": {},
    }
    for name, result in results.items():
        summary = summaries[name]
        payload["cases"][name] = {
            "label": CASE_LABELS[name],
            "switch": summary["switch"],
            "switch_state": summary["switch_state"],
            "field_prefix_before_switch": summary["field_prefix_before_switch"],
            "field": summary["field"],
            "topology": summary["topology"],
            "ownership_before_switch": _ownership_at(
                result["bench"], "boundary_before_switch"
            ),
            "records": summary["records"],
            "db": summary["db"],
            "timeline": _timeline_rows(result, include_values),
        }
        if not include_values:
            for row in payload["cases"][name]["db"]:
                row.pop("values_local", None)
            for row in payload["cases"][name]["records"]:
                row.pop("value_local", None)
            for event in payload["cases"][name]["timeline"]:
                event["detail"].pop("value", None)
                event["detail"].pop("values", None)
    payload["field_divergence"] = {
        "transit_vs_merged_continue": field_divergence(
            results["transit"], results["merged_continue"]
        ),
        "transit_vs_merged_abort": field_divergence(
            results["transit"], results["merged_abort"]
        ),
    }
    payload["same_field_prefix_before_switch"] = (
        summaries["transit"]["field_prefix_before_switch"]
        == summaries["merged_continue"]["field_prefix_before_switch"]
        == summaries["merged_abort"]["field_prefix_before_switch"]
    )
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)


def _streamz_version():
    try:
        import streamz

        return streamz.__version__
    except Exception:
        return None
