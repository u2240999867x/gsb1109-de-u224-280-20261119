# JSON / TEXT 离线解码诊断

完全离线、无需 MySQL 服务端：用假 socket 把预制的 MySQL 报文喂给 PyMySQL
**原生读取流程**（`Connection._read_packet` → `MySQLResult` →
字段描述包解析 → `_read_row_from_packet`），只替换传输层。

## 背景：两家“视图”为何最终 Python 类型相同

`pymysql/connections.py` 的 `MySQLResult._get_descriptions` 决定转换器收到的
Python 类型：

- `FIELD_TYPE.JSON`(245)：**无论 `charsetnr` 是不是 63（binary）**，只要
  `use_unicode=True`，都按连接编码（utf8mb4 → `utf8`）先做字符解码；
- TEXT/BLOB 系（如 `BLOB` 252）：`charsetnr=63` 时被视为二进制，**不做解码**，
  转换器收到 `bytes`；
- 普通多字节 TEXT（`VAR_STRING` 253，`charsetnr=45`）：`use_unicode=True` 时
  解码成 `str`，`use_unicode=False` 时保留 `bytes`；
- SQL NULL 在 `_read_row_from_packet` 中直接短路，**不经过任何 converter**；
  真正的零长度值（长度编码整数 0）会正常进入 converter，可被观察。

业务转换器再把所有非空值统一包成同一个业务类型 `TextValue`，因此“最终 Python
类型相同”，但转换器入参类型（`str` vs `bytes`）暴露了服务端字段语义与客户端
`use_unicode` 设置的差异。

## 文件

- `capture.py`：可离线重复运行的样例（事实来源）。包含两个多结果集场景的
  字段描述包、原始行字节、EOF 包以及两种 `use_unicode` 下的显式预期；
  直接运行可导出 `offline_json_text_capture.json` 静态快照。
- `diagnose_json_text.py`：可执行诊断入口。挂钩包读入口，按物理结果组和列序
  回放真实字段描述、converter 前输入与公开结果，并与样例预期逐项校验，
  失败时退出码非零；同时写出 `diagnostic_report.json`。
- `test_diagnose.py`：pytest 离线用例。

## 运行

```sh
python offline_diag/diagnose_json_text.py
python -m offline_diag.capture
pytest offline_diag/
```

## 场景

两组结果复用全部列名与列序：`v_text`、`v_bin`、`v_json`、`v_empty`。
第一组含普通多字节 TEXT、`charsetnr=63` 的 TEXT/BLOB、同样标 63 的 JSON、
SQL NULL 与真正的零长度值。

- `swapped_types`：第二组把第一份 JSON 与一个 TEXT 的协议类型码对调
  （`v_text` 变 JSON=245，`v_json` 变 VAR_STRING=253），证明驱动按
  **服务端下发的 type_code** 而不是列名选择解码路径。
- `passthrough_json`：同一脚本再跑一次，仅在第二组开始前的
  `next_result` 边界把 JSON converter 换成透传；第二组只有一列 JSON。
  此时第一组仍是 `TextValue`，第二组公开结果直接是 `str`/`bytes`，
  把差异精确定位到“业务转换阶段 + 第二物理结果组”。

每个场景都分别以 `use_unicode=True/False`、流式游标（`SSCursor`，先
`fetchone` 一条再读后续组）和缓冲游标（`Cursor`，从头读取）各跑一遍，
两种游标产出的分组结果完全一致。
