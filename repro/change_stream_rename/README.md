# 集合改名后的归档值班复现

这是一个离线、可重复运行的端到端复现，使用 PyMongo 真实的 `watch()` /
`aggregate` / `getMore` 路径，以及一个按真实 wire 协议应答的脚本化响应替身。

运行方式（无需 MongoDB 进程，也无需监听端口）：

```bash
.venv/bin/python -m repro.change_stream_rename
.venv/bin/python -m pytest repro/change_stream_rename -q
```

运行后会生成 `report.json`，其中包含每一次 `aggregate` 和 `getMore` 实际发出的：

- 集合名（`aggregate` 的目标，或游标命名空间）
- 应用过滤（`pipeline[1:]`）
- 恢复参数名（`startAfter` / `resumeAfter`）和原样续点
- 响应里的 `firstBatch` / `nextBatch` / `postBatchResumeToken`

## 场景

固定事件时间线（`model.py`）：

| pos | 集合 | 事件 | 说明 |
| --- | --- | --- | --- |
| 10 | `shop.orders` | `invalidate` | 旧集合交付后失效 |
| 20 | `shop.orders_v2` | `insert` | 新集合首条事件，暂停点之前 |
| 25 | — | 批次令牌 P | 空批的 `postBatchResumeToken` |
| 30 | `shop.orders_v2` | `insert` | 恢复网络后才可见 |

驱动脚本（`scenario.py`）的动作顺序：

1. 对旧集合 `watch()`，收到 `invalidate`。
2. 第一轮故意用 `resumeAfter=<invalidate token>` 恢复，服务端返回错误码 `280`
   （`NonResumableChangeStreamError`）。
3. 两个合法恢复都以 `startAfter=<invalidate token>` 打开新集合，且各自使用
   独立的 `MongoClient`/连接池：
   - 旧过滤：`[{"$match": {"ns.coll": "orders"}}]`
   - 新过滤：`[{"$match": {"ns.coll": "orders_v2"}}]`
4. 两个流都读到 `P` 时暂停。服务端在空批写出后立即断开连接，但驱动尚未发现。
   暂停点核对：
   - 两个游标仍 `alive`
   - 应用看到的 `resume_token` 与这一轮空批的 `postBatchResumeToken` 完全相同
   - 唯一区别：新过滤的流在空批之前已经交付了新集合首条事件，旧过滤没有任何事件
5. 恢复网络后各调用一次 `try_next()`，驱动自动重新聚合。记录：
   - 这次自动恢复用 `startAfter` 还是 `resumeAfter` 传 `P`
   - 下一条新集合事件是否交付

## 实际结果

`report.json` 的 `records` 与 `diagnosis` 给出：

- 错误恢复：`resumeAfter` + invalidate 令牌 → 错误码 `280`，令牌非法。
- 旧过滤自动恢复：`startAfter` + `P`，重新聚合成功，但没有任何事件；
  新集合首条事件（`event:20`）被过滤静默丢掉。
- 新过滤自动恢复：`resumeAfter` + `P`，重新聚合成功，并交付下一条新集合事件
  （`event:30`）。

两次合法恢复携带的 `P` 完全相同，参数名却不同。原因是驱动的
`_uses_start_after` 状态：收到 `startAfter` 后只要交付过任意事件，就会切换为
`resumeAfter`（`pymongo/change_stream_shared.py` / `change_stream.py`）。因此：

- 旧过滤没有交付过事件，自动恢复仍用 `startAfter` + `P`
- 新过滤交付过首条事件，自动恢复改用了 `resumeAfter` + `P`

两者都是合法请求，服务端都接受，因此仅看请求合法性无法发现丢事件。

## 独立判据

报告中的 `diagnosis` 把三类现象分开：

- `token_legality`：非法的 `resumeAfter` + invalidate 令牌 → `280`；
  两个 `P` 的合法恢复 → `startAfter` / `resumeAfter` 均被接受。
- `filter_loss`：旧过滤恢复成功但 `first_event_after_pause` 为 `null`，
  判为 `silent-loss`；新过滤恢复成功且拿到 `event:30`。
- `normal_empty_batch`：两边都收到空批和同一个 `P`，且连接在空批之后才断开，
  判为 `normal`。

为什么只看 `resume_token` 或只看这次空读会误判：

- 只看 `resume_token`：两个合法请求都带着同一个 `P`，看起来都正常，
  旧过滤丢事件完全不会暴露。
- 只看这次空读：两边都是空批 + `P`，与一次正常的无变化轮询没有区别。
- 正确对照：新过滤在空批之前已经交付过首条事件，旧过滤没有。用“收到首条事件
  前后”的对照（`pause_observations.events_seen_before_pause`）才能识别：
  旧过滤的“正常空批”实际上跳过了 `event:20`。

## 响应替身

`wire.py` 用 `socket.socketpair()` 连接驱动连接池与脚本化服务端，按真实 OP_MSG
收发；`model.py` 按驱动实际发送的 watch 目标、`$changeStream` 选项和过滤执行
同一份固定事件。驱动正常的 `_resume()` 路径被保留，服务端只控制：

- 可见时间范围（暂停前 `pos <= 25`，恢复后 `pos <= 40`）
- 网络是否可用
- 空批后是否断开连接、游标是否随连接失效

空批写出并刷到客户端之后，连接才断开；下一次 `getMore` 因连接丢失和游标失效
返回 `CursorNotFound`，驱动据此走真实的 `_resume()` 并重新发 `aggregate`。
如果服务端保留游标，驱动会合法地继续轮询同一个游标而不重新聚合，这一点在调试
时容易误认为“恢复没有发生”。

`report.json` 连续三次运行字节级一致，可离线重复运行。
