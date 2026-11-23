# 校准流切换本地诊断报告

本报告由 `python -m diagnostics.calibration_switch` 生成，
只使用当前 `streamz` 实现，不修改库行为。

## 证据边界

- 现场可保留：源已读取、下游写入、确认回调，以及切换请求编号。
- 仅本地复现：`combine_latest` 的 `upstreams`、`last`、`metadata`、
  `missing`、闸门暂存队列、`RefCounter` 计数、数据库行形态。
- 时间线中“现场可见”以外的事件不能当作现场证据。
- 源值内容不进入报告；记录只以来源和引用编号出现。

## 判别规则

1. `传输中（闸门暂存）`：记录仍在闸门里，合并节点尚未收到；合法等待。
2. `合并节点仍持有`：记录在某槽位的 `last/metadata` 中；合法持有。
3. `引用未归零且无归属`：槽位已弹出或从未建立，但引用仍大于 0；
   切换半完成造成的泄漏。
4. `无可用引用且永远等不到确认`：引用为 0，且没有下游能再释放它；
   既不是断线，也不是已入库。

不能只用引用计数是否为 0 判断断线，也不能让一次断线报错
解释此后全部停滞。

## 两轮相同的观测

在两轮里，切换请求之前的现场记录完全相同：`A1` 已读、`B1` 已读、
切换请求号 `SW-1`，随后 `B1` 确认未回、数据库无新行。
所以仅凭切换前的现场记录，无法区分“校准数据还在传输”与
“合并节点已经合法持有上一版”。

### 最早哪条实证足以定案

最早的一条充分实证是本地复现中 `disconnect` 返回/抛出后的拓扑快照：

- T1：`disconnect_ok` 之后 `{"consistent": true, "last_present": [true], "metadata_rids": [["A1"]], "missing": [], "upstreams": ["A"]}`，槽位数与上游数一致；
- T2：`disconnect_error` 之后 `{"consistent": false, "last_present": [true], "metadata_rids": [["A1"]], "missing": [], "upstreams": ["A", "B@merge"]}`，槽位已被弹出、上游仍保留 B，
  `consistent=False`；旧 B 从此没有可用槽位。

这条实证的局限是：它只在本地复现里可得。现场若只留下切换请求号，
必须先在本地复现同一拓扑，才能用这条判据；不能反过来宣称现场曾观察到它。

若严格只用现场记录，最早的分叉出现在 `A2` 与合并节点相遇之后。
比较时排除只在 T2 出现的 `B2`，并把确认按集合比较，避免回调先后
被误读为因果：

- T1：`A2` 写出 `{"arity": 2, "rids": ["A2", "C1"]}`；
- T2a：`A2` 不写出（null）；
- T2b：`A2` 写出 `{"arity": 1, "rids": ["A2"]}`。

完整现场差异（仅列出不同的类别）：

- T2a 相对 T1：`{"acks": {"merged": ["A1"], "transit": ["A1", "B1"]}, "writes": {"merged": [], "transit": [{"arity": 2, "rids": ["A2", "C1"]}]}}`；
- T2b 相对 T1：`{"acks": {"merged": ["A1"], "transit": ["A1", "B1"]}, "writes": {"merged": [{"arity": 1, "rids": ["A2"]}], "transit": [{"arity": 2, "rids": ["A2", "C1"]}]}}`。
这些分叉都晚于切换，因此不能用来反推切换前 B1 的位置。

## 场景结果

### T1：B1 停在合并节点之前

断开与接入均成功，拓扑保持一致。

切换前后拓扑变化（本地观察）：`{"missing": {"after": ["C"], "before": ["B@merge"]}, "upstreams": {"after": ["A", "C"], "before": ["A", "B@merge"]}}`

拓扑边界：

| 边界 | upstreams | last 有值槽位 | metadata 引用 | missing | 槽位/上游 | 一致 |
| --- | --- | --- | --- | --- | --- | --- |
| boundary_before_switch | ["A", "B@merge"] | [true, false] | [["A1"], null] | ["B@merge"] | 2/2 | 是 |
| boundary_after_switch | ["A", "C"] | [true, false] | [["A1"], null] | ["C"] | 2/2 | 是 |
| boundary_after_c1 | ["A", "C"] | [true, true] | [["A1"], ["C1"]] | [] | 2/2 | 是 |
| boundary_after_a2 | ["A", "C"] | [true, true] | [["A2"], ["C1"]] | [] | 2/2 | 是 |
| boundary_final | ["A", "C"] | [true, true] | [["A2"], ["C1"]] | [] | 2/2 | 是 |

记录最终归属（槽位与引用计数为本地复现值）：

| 记录 | 来源 | 本地引用计数 | 收到确认 | 确认时无下游 | 经过闸门 | 已离开闸门 | 尝试合并 | 合并槽位 | 库行 | 归属 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| A1 | A | 0 | 是 | 否 | 否 | 否 | 是 | - | - | 已确认归零 |
| B1 | B | 0 | 是 | 是 | 是 | 是 | 否 | - | - | 已确认归零 |
| C1 | C | 1 | 否 | 否 | 否 | 否 | 是 | [1] | [0] | 合并节点仍持有 |
| A2 | A | 1 | 否 | 否 | 否 | 否 | 是 | [0] | [0] | 合并节点仍持有 |

数据库结果：

| 记录 | 是否入库 | 入库形态 |
| --- | --- | --- |
| A1 | 否 | - |
| B1 | 否 | - |
| C1 | 是 | (A2, C1)；元组长度 2 |
| A2 | 是 | (A2, C1)；元组长度 2 |
| B2 | 否 | - |

因果时间线：

| t | 证据范围 | 事件 | 关键内容 |
| ---: | --- | --- | --- |
| 1 | 现场可见 | source_read | `{"rid": "A1", "source": "A"}` |
| 2 | 仅本地复现 | merge_update_begin | `{"consistent": true, "last_present": [false, false], "metadata_rids": [null, null], "missing": ["B@merge", "A"], "rids": ["A1"], "upstreams": ["A", "B@merge"], "who": "A"}` |
| 3 | 仅本地复现 | merge_update_end | `{"consistent": true, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["B@merge"], "rids": ["A1"], "upstreams": ["A", "B@merge"], "who": "A"}` |
| 4 | 现场可见 | source_read | `{"rid": "B1", "source": "B"}` |
| 5 | 仅本地复现 | transport_hold | `{"downstreams": ["merge"], "gate": "B@merge", "ref_counts_local": [2], "rids": ["B1"]}` |
| 6 | 仅本地复现 | boundary_before_switch | `{"consistent": true, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["B@merge"], "upstreams": ["A", "B@merge"]}` |
| 7 | 现场可见 | switch_request | `{"request": "SW-1"}` |
| 8 | 仅本地复现 | switch_begin | `{"consistent": true, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["B@merge"], "on_error": "continue", "upstreams": ["A", "B@merge"]}` |
| 9 | 仅本地复现 | disconnect_ok | `{"consistent": true, "last_present": [true], "metadata_rids": [["A1"]], "missing": [], "upstreams": ["A"]}` |
| 10 | 仅本地复现 | connect_ok | `{"consistent": true, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "upstreams": ["A", "C"]}` |
| 11 | 仅本地复现 | switch_end | `{"consistent": true, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "result": {"error": null, "ok": true, "stage": null}, "upstreams": ["A", "C"]}` |
| 12 | 仅本地复现 | boundary_after_switch | `{"consistent": true, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "upstreams": ["A", "C"]}` |
| 13 | 现场可见 | source_read | `{"rid": "C1", "source": "C"}` |
| 14 | 仅本地复现 | merge_update_begin | `{"consistent": true, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "rids": ["C1"], "upstreams": ["A", "C"], "who": "C"}` |
| 15 | 仅本地复现 | merge_update_end | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["C1"]], "missing": [], "rids": ["C1"], "upstreams": ["A", "C"], "who": "C"}` |
| 16 | 仅本地复现 | boundary_after_c1 | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["C1"]], "missing": [], "upstreams": ["A", "C"]}` |
| 17 | 现场可见 | source_read | `{"rid": "A2", "source": "A"}` |
| 18 | 仅本地复现 | merge_update_begin | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["C1"]], "missing": [], "rids": ["A2"], "upstreams": ["A", "C"], "who": "A"}` |
| 19 | 现场可见 | db_write | `{"arity": 2, "rids": ["A2", "C1"]}` |
| 20 | 仅本地复现 | db_write_local | `{"arity": 2, "rids": ["A2", "C1"]}` |
| 21 | 仅本地复现 | merge_update_end | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A2"], ["C1"]], "missing": [], "rids": ["A2"], "upstreams": ["A", "C"], "who": "A"}` |
| 22 | 现场可见 | ack | `{"rid": "A1", "source": "A"}` |
| 23 | 仅本地复现 | ack_local | `{"count": 0, "rid": "A1"}` |
| 24 | 仅本地复现 | boundary_after_a2 | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A2"], ["C1"]], "missing": [], "upstreams": ["A", "C"]}` |
| 25 | 仅本地复现 | transport_release_begin | `{"downstreams": [], "gate": "B@merge", "rids": ["B1"]}` |
| 26 | 现场可见 | ack | `{"rid": "B1", "source": "B"}` |
| 27 | 仅本地复现 | ack_local | `{"count": 0, "rid": "B1"}` |
| 28 | 仅本地复现 | transport_release_end | `{"downstreams": [], "gate": "B@merge", "rids": ["B1"]}` |
| 29 | 仅本地复现 | boundary_final | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A2"], ["C1"]], "missing": [], "upstreams": ["A", "C"]}` |

### T2a：B1 已到达合并节点，断线报错后仍接入 C

切换在 `disconnect` 阶段失败：`KeyError: <B@merge; Gate>`。

切换前后拓扑变化（本地观察）：`{"consistent": {"after": false, "before": true}, "last_present": {"after": [true, false], "before": [true, true]}, "metadata_rids": {"after": [["A1"], null], "before": [["A1"], ["B1"]]}, "missing": {"after": ["C"], "before": []}, "upstreams": {"after": ["A", "B@merge", "C"], "before": ["A", "B@merge"]}}`

拓扑边界：

| 边界 | upstreams | last 有值槽位 | metadata 引用 | missing | 槽位/上游 | 一致 |
| --- | --- | --- | --- | --- | --- | --- |
| boundary_before_switch | ["A", "B@merge"] | [true, true] | [["A1"], ["B1"]] | [] | 2/2 | 是 |
| boundary_after_switch | ["A", "B@merge", "C"] | [true, false] | [["A1"], null] | ["C"] | 2/3 | 否 |
| boundary_after_c1 | ["A", "B@merge", "C"] | [true, false] | [["A1"], null] | ["C"] | 2/3 | 否 |
| boundary_after_a2 | ["A", "B@merge", "C"] | [true, false] | [["A2"], null] | ["C"] | 2/3 | 否 |
| boundary_final | ["A", "B@merge", "C"] | [true, false] | [["A2"], null] | ["C"] | 2/3 | 否 |

记录最终归属（槽位与引用计数为本地复现值）：

| 记录 | 来源 | 本地引用计数 | 收到确认 | 确认时无下游 | 经过闸门 | 已离开闸门 | 尝试合并 | 合并槽位 | 库行 | 归属 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| A1 | A | 0 | 是 | 否 | 否 | 否 | 是 | - | - | 已确认归零 |
| B1 | B | 1 | 否 | 否 | 是 | 是 | 是 | - | - | 引用未归零且无归属 |
| C1 | C | 2 | 否 | 否 | 否 | 否 | 是 | - | - | 尝试进入合并失败、引用未释放 |
| A2 | A | 1 | 否 | 否 | 否 | 否 | 是 | [0] | - | 合并节点仍持有 |
| B2 | B | 0 | 是 | 是 | 是 | 是 | 否 | - | - | 已确认归零 |

数据库结果：

| 记录 | 是否入库 | 入库形态 |
| --- | --- | --- |
| A1 | 否 | - |
| B1 | 否 | - |
| C1 | 否 | - |
| A2 | 否 | - |
| B2 | 否 | - |

因果时间线：

| t | 证据范围 | 事件 | 关键内容 |
| ---: | --- | --- | --- |
| 1 | 现场可见 | source_read | `{"rid": "A1", "source": "A"}` |
| 2 | 仅本地复现 | merge_update_begin | `{"consistent": true, "last_present": [false, false], "metadata_rids": [null, null], "missing": ["B@merge", "A"], "rids": ["A1"], "upstreams": ["A", "B@merge"], "who": "A"}` |
| 3 | 仅本地复现 | merge_update_end | `{"consistent": true, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["B@merge"], "rids": ["A1"], "upstreams": ["A", "B@merge"], "who": "A"}` |
| 4 | 现场可见 | source_read | `{"rid": "B1", "source": "B"}` |
| 5 | 仅本地复现 | transport_hold | `{"downstreams": ["merge"], "gate": "B@merge", "ref_counts_local": [2], "rids": ["B1"]}` |
| 6 | 仅本地复现 | transport_release_begin | `{"downstreams": ["merge"], "gate": "B@merge", "rids": ["B1"]}` |
| 7 | 仅本地复现 | merge_update_begin | `{"consistent": true, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["B@merge"], "rids": ["B1"], "upstreams": ["A", "B@merge"], "who": "B@merge"}` |
| 8 | 仅本地复现 | merge_update_end | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["B1"]], "missing": [], "rids": ["B1"], "upstreams": ["A", "B@merge"], "who": "B@merge"}` |
| 9 | 仅本地复现 | transport_release_end | `{"downstreams": ["merge"], "gate": "B@merge", "rids": ["B1"]}` |
| 10 | 仅本地复现 | boundary_before_switch | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["B1"]], "missing": [], "upstreams": ["A", "B@merge"]}` |
| 11 | 现场可见 | switch_request | `{"request": "SW-1"}` |
| 12 | 仅本地复现 | switch_begin | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["B1"]], "missing": [], "on_error": "continue", "upstreams": ["A", "B@merge"]}` |
| 13 | 仅本地复现 | disconnect_error | `{"consistent": false, "error": "KeyError", "last_present": [true], "metadata_rids": [["A1"]], "missing": [], "upstreams": ["A", "B@merge"]}` |
| 14 | 仅本地复现 | connect_ok | `{"consistent": false, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "upstreams": ["A", "B@merge", "C"]}` |
| 15 | 仅本地复现 | switch_end | `{"consistent": false, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "result": {"error": "KeyError: <B@merge; Gate>", "ok": false, "stage": "disconnect"}, "upstreams": ["A", "B@merge", "C"]}` |
| 16 | 仅本地复现 | boundary_after_switch | `{"consistent": false, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "upstreams": ["A", "B@merge", "C"]}` |
| 17 | 现场可见 | source_read | `{"rid": "C1", "source": "C"}` |
| 18 | 仅本地复现 | merge_update_begin | `{"consistent": false, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "rids": ["C1"], "upstreams": ["A", "B@merge", "C"], "who": "C"}` |
| 19 | 仅本地复现 | merge_update_error | `{"consistent": false, "error": "IndexError", "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "rids": ["C1"], "upstreams": ["A", "B@merge", "C"], "who": "C"}` |
| 20 | 仅本地复现 | source_emit_error | `{"error": "IndexError", "message": "list index out of range", "rids": ["C1"]}` |
| 21 | 仅本地复现 | boundary_after_c1 | `{"consistent": false, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "upstreams": ["A", "B@merge", "C"]}` |
| 22 | 现场可见 | source_read | `{"rid": "A2", "source": "A"}` |
| 23 | 仅本地复现 | merge_update_begin | `{"consistent": false, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "rids": ["A2"], "upstreams": ["A", "B@merge", "C"], "who": "A"}` |
| 24 | 仅本地复现 | merge_update_end | `{"consistent": false, "last_present": [true, false], "metadata_rids": [["A2"], null], "missing": ["C"], "rids": ["A2"], "upstreams": ["A", "B@merge", "C"], "who": "A"}` |
| 25 | 现场可见 | ack | `{"rid": "A1", "source": "A"}` |
| 26 | 仅本地复现 | ack_local | `{"count": 0, "rid": "A1"}` |
| 27 | 仅本地复现 | boundary_after_a2 | `{"consistent": false, "last_present": [true, false], "metadata_rids": [["A2"], null], "missing": ["C"], "upstreams": ["A", "B@merge", "C"]}` |
| 28 | 现场可见 | source_read | `{"rid": "B2", "source": "B"}` |
| 29 | 仅本地复现 | transport_hold | `{"downstreams": [], "gate": "B@merge", "ref_counts_local": [2], "rids": ["B2"]}` |
| 30 | 仅本地复现 | transport_release_begin | `{"downstreams": [], "gate": "B@merge", "rids": ["B2"]}` |
| 31 | 现场可见 | ack | `{"rid": "B2", "source": "B"}` |
| 32 | 仅本地复现 | ack_local | `{"count": 0, "rid": "B2"}` |
| 33 | 仅本地复现 | transport_release_end | `{"downstreams": [], "gate": "B@merge", "rids": ["B2"]}` |
| 34 | 仅本地复现 | boundary_final | `{"consistent": false, "last_present": [true, false], "metadata_rids": [["A2"], null], "missing": ["C"], "upstreams": ["A", "B@merge", "C"]}` |

### T2b：B1 已到达合并节点，断线报错后中止切换

切换在 `disconnect` 阶段失败：`KeyError: <B@merge; Gate>`。

切换前后拓扑变化（本地观察）：`{"consistent": {"after": false, "before": true}, "last_present": {"after": [true], "before": [true, true]}, "metadata_rids": {"after": [["A1"]], "before": [["A1"], ["B1"]]}}`

拓扑边界：

| 边界 | upstreams | last 有值槽位 | metadata 引用 | missing | 槽位/上游 | 一致 |
| --- | --- | --- | --- | --- | --- | --- |
| boundary_before_switch | ["A", "B@merge"] | [true, true] | [["A1"], ["B1"]] | [] | 2/2 | 是 |
| boundary_after_switch | ["A", "B@merge"] | [true] | [["A1"]] | [] | 1/2 | 否 |
| boundary_after_c1 | ["A", "B@merge"] | [true] | [["A1"]] | [] | 1/2 | 否 |
| boundary_after_a2 | ["A", "B@merge"] | [true] | [["A2"]] | [] | 1/2 | 否 |
| boundary_final | ["A", "B@merge"] | [true] | [["A2"]] | [] | 1/2 | 否 |

记录最终归属（槽位与引用计数为本地复现值）：

| 记录 | 来源 | 本地引用计数 | 收到确认 | 确认时无下游 | 经过闸门 | 已离开闸门 | 尝试合并 | 合并槽位 | 库行 | 归属 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| A1 | A | 0 | 是 | 否 | 否 | 否 | 是 | - | - | 已确认归零 |
| B1 | B | 1 | 否 | 否 | 是 | 是 | 是 | - | - | 引用未归零且无归属 |
| C1 | C | 0 | 否 | 否 | 否 | 否 | 否 | - | - | 无可用引用且永远等不到确认 |
| A2 | A | 1 | 否 | 否 | 否 | 否 | 是 | [0] | [0] | 合并节点仍持有 |
| B2 | B | 0 | 是 | 是 | 是 | 是 | 否 | - | - | 已确认归零 |

数据库结果：

| 记录 | 是否入库 | 入库形态 |
| --- | --- | --- |
| A1 | 否 | - |
| B1 | 否 | - |
| C1 | 否 | - |
| A2 | 是 | (A2)；元组长度 1 |
| B2 | 否 | - |

因果时间线：

| t | 证据范围 | 事件 | 关键内容 |
| ---: | --- | --- | --- |
| 1 | 现场可见 | source_read | `{"rid": "A1", "source": "A"}` |
| 2 | 仅本地复现 | merge_update_begin | `{"consistent": true, "last_present": [false, false], "metadata_rids": [null, null], "missing": ["B@merge", "A"], "rids": ["A1"], "upstreams": ["A", "B@merge"], "who": "A"}` |
| 3 | 仅本地复现 | merge_update_end | `{"consistent": true, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["B@merge"], "rids": ["A1"], "upstreams": ["A", "B@merge"], "who": "A"}` |
| 4 | 现场可见 | source_read | `{"rid": "B1", "source": "B"}` |
| 5 | 仅本地复现 | transport_hold | `{"downstreams": ["merge"], "gate": "B@merge", "ref_counts_local": [2], "rids": ["B1"]}` |
| 6 | 仅本地复现 | transport_release_begin | `{"downstreams": ["merge"], "gate": "B@merge", "rids": ["B1"]}` |
| 7 | 仅本地复现 | merge_update_begin | `{"consistent": true, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["B@merge"], "rids": ["B1"], "upstreams": ["A", "B@merge"], "who": "B@merge"}` |
| 8 | 仅本地复现 | merge_update_end | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["B1"]], "missing": [], "rids": ["B1"], "upstreams": ["A", "B@merge"], "who": "B@merge"}` |
| 9 | 仅本地复现 | transport_release_end | `{"downstreams": ["merge"], "gate": "B@merge", "rids": ["B1"]}` |
| 10 | 仅本地复现 | boundary_before_switch | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["B1"]], "missing": [], "upstreams": ["A", "B@merge"]}` |
| 11 | 现场可见 | switch_request | `{"request": "SW-1"}` |
| 12 | 仅本地复现 | switch_begin | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["B1"]], "missing": [], "on_error": "abort", "upstreams": ["A", "B@merge"]}` |
| 13 | 仅本地复现 | disconnect_error | `{"consistent": false, "error": "KeyError", "last_present": [true], "metadata_rids": [["A1"]], "missing": [], "upstreams": ["A", "B@merge"]}` |
| 14 | 仅本地复现 | switch_abort | `{"consistent": false, "last_present": [true], "metadata_rids": [["A1"]], "missing": [], "upstreams": ["A", "B@merge"]}` |
| 15 | 仅本地复现 | boundary_after_switch | `{"consistent": false, "last_present": [true], "metadata_rids": [["A1"]], "missing": [], "upstreams": ["A", "B@merge"]}` |
| 16 | 现场可见 | source_read | `{"rid": "C1", "source": "C"}` |
| 17 | 仅本地复现 | boundary_after_c1 | `{"consistent": false, "last_present": [true], "metadata_rids": [["A1"]], "missing": [], "upstreams": ["A", "B@merge"]}` |
| 18 | 现场可见 | source_read | `{"rid": "A2", "source": "A"}` |
| 19 | 仅本地复现 | merge_update_begin | `{"consistent": false, "last_present": [true], "metadata_rids": [["A1"]], "missing": [], "rids": ["A2"], "upstreams": ["A", "B@merge"], "who": "A"}` |
| 20 | 现场可见 | db_write | `{"arity": 1, "rids": ["A2"]}` |
| 21 | 仅本地复现 | db_write_local | `{"arity": 1, "rids": ["A2"]}` |
| 22 | 仅本地复现 | merge_update_end | `{"consistent": false, "last_present": [true], "metadata_rids": [["A2"]], "missing": [], "rids": ["A2"], "upstreams": ["A", "B@merge"], "who": "A"}` |
| 23 | 现场可见 | ack | `{"rid": "A1", "source": "A"}` |
| 24 | 仅本地复现 | ack_local | `{"count": 0, "rid": "A1"}` |
| 25 | 仅本地复现 | boundary_after_a2 | `{"consistent": false, "last_present": [true], "metadata_rids": [["A2"]], "missing": [], "upstreams": ["A", "B@merge"]}` |
| 26 | 现场可见 | source_read | `{"rid": "B2", "source": "B"}` |
| 27 | 仅本地复现 | transport_hold | `{"downstreams": [], "gate": "B@merge", "ref_counts_local": [2], "rids": ["B2"]}` |
| 28 | 仅本地复现 | transport_release_begin | `{"downstreams": [], "gate": "B@merge", "rids": ["B2"]}` |
| 29 | 现场可见 | ack | `{"rid": "B2", "source": "B"}` |
| 30 | 仅本地复现 | ack_local | `{"count": 0, "rid": "B2"}` |
| 31 | 仅本地复现 | transport_release_end | `{"downstreams": [], "gate": "B@merge", "rids": ["B2"]}` |
| 32 | 仅本地复现 | boundary_final | `{"consistent": false, "last_present": [true], "metadata_rids": [["A2"]], "missing": [], "upstreams": ["A", "B@merge"]}` |

### 对照：不换线，仅用 B2 替换旧校准值

未执行切换。

拓扑边界：

| 边界 | upstreams | last 有值槽位 | metadata 引用 | missing | 槽位/上游 | 一致 |
| --- | --- | --- | --- | --- | --- | --- |
| boundary_after_replace | ["A", "B@merge"] | [true, true] | [["A1"], ["B2"]] | [] | 2/2 | 是 |
| boundary_final | ["A", "B@merge"] | [true, true] | [["A2"], ["B2"]] | [] | 2/2 | 是 |

记录最终归属（槽位与引用计数为本地复现值）：

| 记录 | 来源 | 本地引用计数 | 收到确认 | 确认时无下游 | 经过闸门 | 已离开闸门 | 尝试合并 | 合并槽位 | 库行 | 归属 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| A1 | A | 0 | 是 | 否 | 否 | 否 | 是 | - | - | 已确认归零 |
| B1 | B | 0 | 是 | 否 | 是 | 是 | 是 | - | - | 已确认归零 |
| B2 | B | 1 | 否 | 否 | 是 | 是 | 是 | [1] | [0] | 合并节点仍持有 |
| A2 | A | 1 | 否 | 否 | 否 | 否 | 是 | [0] | [0] | 合并节点仍持有 |

数据库结果：

| 记录 | 是否入库 | 入库形态 |
| --- | --- | --- |
| A1 | 否 | - |
| B1 | 否 | - |
| C1 | 否 | - |
| A2 | 是 | (A2, B2)；元组长度 2 |
| B2 | 是 | (A2, B2)；元组长度 2 |

因果时间线：

| t | 证据范围 | 事件 | 关键内容 |
| ---: | --- | --- | --- |
| 1 | 现场可见 | source_read | `{"rid": "A1", "source": "A"}` |
| 2 | 仅本地复现 | merge_update_begin | `{"consistent": true, "last_present": [false, false], "metadata_rids": [null, null], "missing": ["B@merge", "A"], "rids": ["A1"], "upstreams": ["A", "B@merge"], "who": "A"}` |
| 3 | 仅本地复现 | merge_update_end | `{"consistent": true, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["B@merge"], "rids": ["A1"], "upstreams": ["A", "B@merge"], "who": "A"}` |
| 4 | 现场可见 | source_read | `{"rid": "B1", "source": "B"}` |
| 5 | 仅本地复现 | transport_hold | `{"downstreams": ["merge"], "gate": "B@merge", "ref_counts_local": [2], "rids": ["B1"]}` |
| 6 | 仅本地复现 | transport_release_begin | `{"downstreams": ["merge"], "gate": "B@merge", "rids": ["B1"]}` |
| 7 | 仅本地复现 | merge_update_begin | `{"consistent": true, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["B@merge"], "rids": ["B1"], "upstreams": ["A", "B@merge"], "who": "B@merge"}` |
| 8 | 仅本地复现 | merge_update_end | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["B1"]], "missing": [], "rids": ["B1"], "upstreams": ["A", "B@merge"], "who": "B@merge"}` |
| 9 | 仅本地复现 | transport_release_end | `{"downstreams": ["merge"], "gate": "B@merge", "rids": ["B1"]}` |
| 10 | 现场可见 | source_read | `{"rid": "B2", "source": "B"}` |
| 11 | 仅本地复现 | transport_hold | `{"downstreams": ["merge"], "gate": "B@merge", "ref_counts_local": [2], "rids": ["B2"]}` |
| 12 | 仅本地复现 | transport_release_begin | `{"downstreams": ["merge"], "gate": "B@merge", "rids": ["B2"]}` |
| 13 | 仅本地复现 | merge_update_begin | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["B1"]], "missing": [], "rids": ["B2"], "upstreams": ["A", "B@merge"], "who": "B@merge"}` |
| 14 | 仅本地复现 | merge_update_end | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["B2"]], "missing": [], "rids": ["B2"], "upstreams": ["A", "B@merge"], "who": "B@merge"}` |
| 15 | 现场可见 | ack | `{"rid": "B1", "source": "B"}` |
| 16 | 仅本地复现 | ack_local | `{"count": 0, "rid": "B1"}` |
| 17 | 仅本地复现 | transport_release_end | `{"downstreams": ["merge"], "gate": "B@merge", "rids": ["B2"]}` |
| 18 | 仅本地复现 | boundary_after_replace | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["B2"]], "missing": [], "upstreams": ["A", "B@merge"]}` |
| 19 | 现场可见 | source_read | `{"rid": "A2", "source": "A"}` |
| 20 | 仅本地复现 | merge_update_begin | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["B2"]], "missing": [], "rids": ["A2"], "upstreams": ["A", "B@merge"], "who": "A"}` |
| 21 | 现场可见 | db_write | `{"arity": 2, "rids": ["A2", "B2"]}` |
| 22 | 仅本地复现 | db_write_local | `{"arity": 2, "rids": ["A2", "B2"]}` |
| 23 | 仅本地复现 | merge_update_end | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A2"], ["B2"]], "missing": [], "rids": ["A2"], "upstreams": ["A", "B@merge"], "who": "A"}` |
| 24 | 现场可见 | ack | `{"rid": "A1", "source": "A"}` |
| 25 | 仅本地复现 | ack_local | `{"count": 0, "rid": "A1"}` |
| 26 | 仅本地复现 | boundary_final | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A2"], ["B2"]], "missing": [], "upstreams": ["A", "B@merge"]}` |

### 对照：同一拓扑颠倒 A1/B1 到达顺序

切换在 `disconnect` 阶段失败：`KeyError: <B@merge; Gate>`。

切换前后拓扑变化（本地观察）：`{"consistent": {"after": false, "before": true}, "last_present": {"after": [true, false], "before": [true, true]}, "metadata_rids": {"after": [["A1"], null], "before": [["A1"], ["B1"]]}, "missing": {"after": ["C"], "before": []}, "upstreams": {"after": ["A", "B@merge", "C"], "before": ["A", "B@merge"]}}`

拓扑边界：

| 边界 | upstreams | last 有值槽位 | metadata 引用 | missing | 槽位/上游 | 一致 |
| --- | --- | --- | --- | --- | --- | --- |
| boundary_after_a1 | ["A", "B@merge"] | [true, true] | [["A1"], ["B1"]] | [] | 2/2 | 是 |
| boundary_final | ["A", "B@merge", "C"] | [true, false] | [["A2"], null] | ["C"] | 2/3 | 否 |

记录最终归属（槽位与引用计数为本地复现值）：

| 记录 | 来源 | 本地引用计数 | 收到确认 | 确认时无下游 | 经过闸门 | 已离开闸门 | 尝试合并 | 合并槽位 | 库行 | 归属 |
| --- | --- | ---: | --- | --- | --- | --- | --- | --- | --- | --- |
| B1 | B | 1 | 否 | 否 | 是 | 是 | 是 | - | [0] | 引用未归零且无归属 |
| A1 | A | 0 | 是 | 否 | 否 | 否 | 是 | - | [0] | 已确认归零 |
| C1 | C | 2 | 否 | 否 | 否 | 否 | 是 | - | - | 尝试进入合并失败、引用未释放 |
| A2 | A | 1 | 否 | 否 | 否 | 否 | 是 | [0] | - | 合并节点仍持有 |
| B2 | B | 0 | 是 | 是 | 是 | 是 | 否 | - | - | 已确认归零 |

数据库结果：

| 记录 | 是否入库 | 入库形态 |
| --- | --- | --- |
| A1 | 是 | (A1, B1)；元组长度 2 |
| B1 | 是 | (A1, B1)；元组长度 2 |
| C1 | 否 | - |
| A2 | 否 | - |
| B2 | 否 | - |

因果时间线：

| t | 证据范围 | 事件 | 关键内容 |
| ---: | --- | --- | --- |
| 1 | 现场可见 | source_read | `{"rid": "B1", "source": "B"}` |
| 2 | 仅本地复现 | transport_hold | `{"downstreams": ["merge"], "gate": "B@merge", "ref_counts_local": [2], "rids": ["B1"]}` |
| 3 | 仅本地复现 | transport_release_begin | `{"downstreams": ["merge"], "gate": "B@merge", "rids": ["B1"]}` |
| 4 | 仅本地复现 | merge_update_begin | `{"consistent": true, "last_present": [false, false], "metadata_rids": [null, null], "missing": ["A", "B@merge"], "rids": ["B1"], "upstreams": ["A", "B@merge"], "who": "B@merge"}` |
| 5 | 仅本地复现 | merge_update_end | `{"consistent": true, "last_present": [false, true], "metadata_rids": [null, ["B1"]], "missing": ["A"], "rids": ["B1"], "upstreams": ["A", "B@merge"], "who": "B@merge"}` |
| 6 | 仅本地复现 | transport_release_end | `{"downstreams": ["merge"], "gate": "B@merge", "rids": ["B1"]}` |
| 7 | 现场可见 | source_read | `{"rid": "A1", "source": "A"}` |
| 8 | 仅本地复现 | merge_update_begin | `{"consistent": true, "last_present": [false, true], "metadata_rids": [null, ["B1"]], "missing": ["A"], "rids": ["A1"], "upstreams": ["A", "B@merge"], "who": "A"}` |
| 9 | 现场可见 | db_write | `{"arity": 2, "rids": ["A1", "B1"]}` |
| 10 | 仅本地复现 | db_write_local | `{"arity": 2, "rids": ["A1", "B1"]}` |
| 11 | 仅本地复现 | merge_update_end | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["B1"]], "missing": [], "rids": ["A1"], "upstreams": ["A", "B@merge"], "who": "A"}` |
| 12 | 仅本地复现 | boundary_after_a1 | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["B1"]], "missing": [], "upstreams": ["A", "B@merge"]}` |
| 13 | 现场可见 | switch_request | `{"request": "SW-1"}` |
| 14 | 仅本地复现 | switch_begin | `{"consistent": true, "last_present": [true, true], "metadata_rids": [["A1"], ["B1"]], "missing": [], "on_error": "continue", "upstreams": ["A", "B@merge"]}` |
| 15 | 仅本地复现 | disconnect_error | `{"consistent": false, "error": "KeyError", "last_present": [true], "metadata_rids": [["A1"]], "missing": [], "upstreams": ["A", "B@merge"]}` |
| 16 | 仅本地复现 | connect_ok | `{"consistent": false, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "upstreams": ["A", "B@merge", "C"]}` |
| 17 | 仅本地复现 | switch_end | `{"consistent": false, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "result": {"error": "KeyError: <B@merge; Gate>", "ok": false, "stage": "disconnect"}, "upstreams": ["A", "B@merge", "C"]}` |
| 18 | 现场可见 | source_read | `{"rid": "C1", "source": "C"}` |
| 19 | 仅本地复现 | merge_update_begin | `{"consistent": false, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "rids": ["C1"], "upstreams": ["A", "B@merge", "C"], "who": "C"}` |
| 20 | 仅本地复现 | merge_update_error | `{"consistent": false, "error": "IndexError", "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "rids": ["C1"], "upstreams": ["A", "B@merge", "C"], "who": "C"}` |
| 21 | 仅本地复现 | source_emit_error | `{"error": "IndexError", "message": "list index out of range", "rids": ["C1"]}` |
| 22 | 现场可见 | source_read | `{"rid": "A2", "source": "A"}` |
| 23 | 仅本地复现 | merge_update_begin | `{"consistent": false, "last_present": [true, false], "metadata_rids": [["A1"], null], "missing": ["C"], "rids": ["A2"], "upstreams": ["A", "B@merge", "C"], "who": "A"}` |
| 24 | 仅本地复现 | merge_update_end | `{"consistent": false, "last_present": [true, false], "metadata_rids": [["A2"], null], "missing": ["C"], "rids": ["A2"], "upstreams": ["A", "B@merge", "C"], "who": "A"}` |
| 25 | 现场可见 | ack | `{"rid": "A1", "source": "A"}` |
| 26 | 仅本地复现 | ack_local | `{"count": 0, "rid": "A1"}` |
| 27 | 现场可见 | source_read | `{"rid": "B2", "source": "B"}` |
| 28 | 仅本地复现 | transport_hold | `{"downstreams": [], "gate": "B@merge", "ref_counts_local": [2], "rids": ["B2"]}` |
| 29 | 仅本地复现 | transport_release_begin | `{"downstreams": [], "gate": "B@merge", "rids": ["B2"]}` |
| 30 | 现场可见 | ack | `{"rid": "B2", "source": "B"}` |
| 31 | 仅本地复现 | ack_local | `{"count": 0, "rid": "B2"}` |
| 32 | 仅本地复现 | transport_release_end | `{"downstreams": [], "gate": "B@merge", "rids": ["B2"]}` |
| 33 | 仅本地复现 | boundary_final | `{"consistent": false, "last_present": [true, false], "metadata_rids": [["A2"], null], "missing": ["C"], "upstreams": ["A", "B@merge", "C"]}` |

## 有限结论

- 两轮切换前的现场观测完全相同。
- T1 是合法等待：`B1` 一直只在闸门中；切换成功后迟到的 `B1`
  没有下游可进入，但能被释放并收到确认；`C1` 与 `A2` 组成
  `(A2,C1)` 入库。
- T2 是切换后的状态不一致：`B1` 已进入合并槽位，断开在
  `missing.remove` 处抛 `KeyError`，但此前已弹出 `last/metadata`，
  槽位数与上游数从此不一致；`B1` 没有槽位且引用泄漏。
- T2a 继续接入 C 后，`C1` 在 `metadata[idx]` 处触发 `IndexError`，
  `A2` 虽更新第 0 槽，但因 `missing={'C'}` 永不写出；库无新行。
  这是与断线报错不同的第二处故障点。
- T2b 中止切换后，`C1` 没有下游：引用为 0、确认永不回来；
  `A2` 写出退化的单元组 `(A2,)`，属于长度不一致的下游形态。
- 对照运行说明：不换线时 `B2` 合法替换 `B1`，`B1` 归零并确认，
  `A2` 写出 `(A2,B2)`；因此“旧值仍被合并节点持有”不是故障。
- 顺序颠倒说明：`B1` 先到、`A1` 后到时，`(A1,B1)` 可以先入库；
  随后同一次半完成断开仍会让 `B1` 泄漏。已有库行不等于全部确认，
  确认缺失也不等于没有入库；同一记录可以同时“已入库”和“引用泄漏”。
- `B2` 的确认回来只证明它被释放，不证明它被合并节点接收；
  T2a/T2b 中 `B2` 的下游都为空。

## 逐笔去向

| 场景 | A1 | B1 | C1 | A2 | B2 |
| --- | --- | --- | --- | --- | --- |
| T1 | 未写入；已确认归零 | 未写入；已确认归零 | 已写入；合并节点仍持有 | 已写入；合并节点仍持有 | 未产生 |
| T2a | 未写入；已确认归零 | 未写入；引用未归零且无归属 | 未写入；尝试进入合并失败、引用未释放 | 未写入；合并节点仍持有 | 未写入；已确认归零 |
| T2b | 未写入；已确认归零 | 未写入；引用未归零且无归属 | 未写入；无可用引用且永远等不到确认 | 已写入；合并节点仍持有 | 未写入；已确认归零 |
| 对照-替换 | 未写入；已确认归零 | 未写入；已确认归零 | 未产生 | 已写入；合并节点仍持有 | 已写入；合并节点仍持有 |
| 对照-颠倒 | 已写入；已确认归零 | 已写入；引用未归零且无归属 | 未写入；尝试进入合并失败、引用未释放 | 未写入；合并节点仍持有 | 未写入；已确认归零 |

## 值班顺序

1. 先查切换：现场通常只有请求号、没有异常栈；优先复现切换时刻拓扑。
   若上游数不等于槽位数，或断开后 `missing` 与槽位不符，
   按切换状态不一致处理。
2. 再查校准输入：拓扑一致且 `B1` 只在闸门中时，先等传输或检查闸门，
   不要把未确认当作断线。
3. 最后逐条核对记录归属；引用计数、单次断线报错都只能解释局部。

## 复现

```bash
python -m diagnostics.calibration_switch
python -m diagnostics.calibration_switch --json
python -m pytest diagnostics/calibration_switch/tests -q
```

