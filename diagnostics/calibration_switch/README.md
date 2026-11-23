# 校准流切换诊断

这个目录是针对以下拓扑的本地、确定性、可复核诊断，不改动 `streamz` 库行为：

```
A ──────────────┐
                ├─ combine_latest(emit_on=A) ── DB
B ── gate ──────┘
```

`gate` 是受控闸门，用来复现“源已读取、但尚未进入合并节点”的传输窗口。
C 在切换时接入同一合并节点。每条记录带有独立的 `RefCounter` 和确认回调；
B1 与 C1 使用完全相同的数值，只有 `rid` 不同。

## 运行

```bash
python -m diagnostics.calibration_switch
python -m diagnostics.calibration_switch --json
python -m diagnostics.calibration_switch --json --include-values
python -m pytest diagnostics/calibration_switch/tests -q
```

`--include-values` 才会输出本地记录值；默认报告只出现来源与 `rid`。

## 证据范围

| 范围 | 内容 | 用途 |
| --- | --- | --- |
| 现场可见 | 源已读取、切换请求号、数据库写入、确认回调 | 现场历史里可能保留的记录 |
| 仅本地复现 | `combine_latest` 的 `upstreams`/`last`/`metadata`/`missing`、闸门队列、引用计数、库行形态 | 只能在本地复现时观察，不能当作现场已有的证据 |

时间线里每个事件都标记为“现场可见”或“仅本地复现”。

## 场景

| 场景 | 构造 | 目的 |
| --- | --- | --- |
| T1 | `B1` 被闸门挡在合并节点之前，切换后接入 C | 合法等待 |
| T2a | `B1` 已进入合并槽位，断开报错后仍接入 C | 切换后的状态不一致 + 第二处 `IndexError` |
| T2b | `B1` 已进入合并槽位，断开报错后中止切换 | 无下游、引用为 0、永远等不到确认 |
| 对照-替换 | 不换线，只用 `B2` 替换旧校准值 | 证明“旧值仍被持有”本身不是故障 |
| 对照-颠倒 | 同一拓扑下 `B1` 先于 `A1` 到达 | 证明入库与确认可以分离 |

## 判别规则

- **传输中**：记录在闸门队列，合并节点尚未收到；合法等待。
- **合并节点仍持有**：记录在 `last/metadata` 的槽位中；合法持有。
- **引用未归零且无归属**：槽位已弹出或从未建立，但引用仍大于 0；切换半完成造成的泄漏。
- **无可用引用且永远等不到确认**：引用为 0，且没有下游能释放它。

不能只看“引用是否归零”判定断线，也不能用一次断线报错解释此后所有停滞。
确认回调只证明引用被释放；没有下游时它仍会触发，因此不能证明记录进入了合并节点。

## 当前库的行为要点

`combine_latest._remove_upstream` 先弹出 `last`/`metadata`，再执行
`missing.remove(upstream)`。当 B 的值已经到达、`missing` 为空时，`remove`
抛 `KeyError`，而前面的弹出已经生效，于是：

- `len(upstreams) != len(last)`（T2a 为 3/2，T2b 为 2/1）；
- 旧 B 的槽位已经消失，但它的 `RefCounter` 没有被释放；
- 之后按 `upstreams.index(who)` 取槽会错位：C1 触发 `IndexError`，
  或 A2 覆盖第 0 槽并把旧 A1 引用释放。

这是当前库的真实行为，诊断只观察、不修改。
