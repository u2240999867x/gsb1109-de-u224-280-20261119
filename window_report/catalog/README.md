# 窗口报表双结果集目录

由 `window_report/explain.py` 生成（仓库根目录运行 `.venv/bin/python window_report/explain.py` 可重建）。
结构信息来自 sql-metadata 的 `Parser`，数值证据来自目录内的小型窗口引擎。

## 查询结构（sql-metadata 提取）

见 `structural_roles.json`。要点：

- 表：`events`；子查询 `t` 是内层窗口所在层。
- 列：`id, device, sec, qty, org`；别名链 `rolling -> qty`、`rolling_total -> rolling`。
- 字段角色：`device` 是内层 `w` 的分区键；`org` 是外层 `w` 的分区键（样本中全相同）；
  `sec` 是两层共用的排序键（内层 ROWS、外层 RANGE）；`qty` 是内层度量；
  `rolling` 是内层输出、外层度量；最外层 `ORDER BY id DESC` 只控制展示顺序。
- 两层窗口同名 `w`，但作用域各自独立，互不覆盖。

没有样本时，能报告的只有以上结构角色，见 `missing_order_data.json` 的 `structural_only`。

## 为什么同一样本有两组结果

样本：`(1,A,1,10)、(2,A,1,20)、(3,B,2,30)、(4,A,3,40)`，机构全为 `O1`。

内层 `ORDER BY sec` 在 id=1 与 id=2 上并列（同设备 A、同秒 1）。
有第二顺序 `id` 时次序固定；没有第二顺序时两种并列次序都合法，
rolling 挂在哪个 id 上随之不同，外层 RANGE 汇总也随之不同：

- 路径 A（内层第二顺序 id ASC，`main_path_a.json`）：中间 rolling 为
  id1=10、id2=30、id3=30、id4=60；同一次执行里 id=1 → 40，id=3 → 70。
- 路径 B（无第二顺序时的另一合法次序，`main_path_b.json`）：中间 rolling 为
  id1=30、id2=20、id3=30、id4=50；同一次执行里 id=1 → 50，id=3 → 80。

## 同一次执行的组合核对

`main_consistency.json`：允许的组合只有 `[40,70]` 与 `[50,80]`。
内层并列次序在一次执行内固定，因此 `[40,80]`、`[50,70]` 是禁止的混合组合——
不能把路径 A 的 40 和路径 B 的 80 拼成同一次输出。

## 每组结果真正参与的输入（经过哪一个中间 id）

以路径 A 为例（`main_path_a.json` 的 `outputs[].via`）：

- id=1 的 40 ← 中间 id1（rolling 10 ← 输入行 1）+ 中间 id2（rolling 30 ← 输入行 1、2）。
- id=3 的 70 ← 中间 id1（10 ← 行 1）+ 中间 id2（30 ← 行 1、2）+ 中间 id3（30 ← 行 3）。

原行 1 经过中间 id1 和中间 id2 两个中间值被使用两次（`reused_inputs: [1]`）。
路径 B 的同名列在 `main_path_b.json`。

## 变种：内层只取上一行

`variant_prev_only.json`：内层改为 `ROWS BETWEEN 1 PRECEDING AND 1 PRECEDING` 后，
每个分区首行帧为空。空帧没有任何输入，rolling 为 NULL 不是原行的零贡献，
外层 SUM 忽略 NULL。同一次执行的合法组合为 `[10,10]`（路径 A）与 `[20,20]`（路径 B）。

## 变种：并列输入等值

`variant_equal_qty.json`：id=1、id=2 的数量都改为 10 后，两条路径的结果数相同
（id=1 → 30，id=3 → 60，`numbers_coincide: true`）。但中间 rolling 挂在不同 id 上
（路径 A：id1=10、id2=20；路径 B：id1=20、id2=10），所以目录仍保留两条计算路径的证据。

## 变种：只输出一行

`variant_single_row.json` 与 `query_single_row.sql`：`WHERE id = 1` 在最外层，
过滤发生在两层窗口计算之后。id=3 的输出（路径 A 的 70、路径 B 的 80）未展示，
但它已被算出，且它的存在不改变前面层级已有的窗口与中间值。

## 缺少排序数据时

`missing_order_data.json`：若样本中某行的 `sec` 未知（示例把 id=2 的 sec 置空），
共同次序不可确定，无法枚举合法路径；此时只报结构角色，
不按输入文本次序定档。
