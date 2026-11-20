# 行筛选回查错行：可重放诊断

本目录的诊断不修改 `TableHDU.where` 或 native 筛选接口。它只合成表、调用
现有接口，并把两路命中恢复成独立的 `ROWID`。

## 回放

```bash
.venv/bin/python -m fitsio.tests.where_row_diagnostic
.venv/bin/python -m fitsio.tests.where_row_diagnostic --json
.venv/bin/python -m pytest fitsio/tests/test_where_row_identity.py -q
```

诊断文件：`fitsio/tests/where_row_diagnostic.py`
回归测试：`fitsio/tests/test_where_row_identity.py`

## 合成表

主表 12 行，`ROWID` 与亮度独立：

| 全局行 | ROWID | 物理亮度 | 存储值 |
|---:|---:|---:|---:|
| 0 | 1001 | 6.0 | 5.0 |
| 1 | 1017 | 5.0 | 6.0 |
| 2 | 1034 | 8.0 | 3.0 |
| 3 | 1051 | 8.0 | 3.0 |
| 4 | 1068 | 7.0 | 4.0 |
| 5 | 1085 | 6.0 | 5.0 |
| 6 | 1102 | 5.0 | 6.0 |
| 7 | 1119 | 4.0 | 7.0 |
| 8 | 1136 | 3.0 | 8.0 |
| 9 | 1153 | 6.0 | 5.0 |
| 10 | 1170 | 5.0 | 6.0 |
| 11 | 1187 | 4.0 | 7.0 |

列定义：`BRIGHT` 存储浮点值，`TSCAL2=-1.0`、`TZERO2=11.0`，因此
`物理值 = -1.0 * 存储值 + 11.0`。表中刻意包含：重复亮度（5.0 和 6.0 各三次）、
阈值恰好相等（三行物理值 5.0）、窗口左端等于阈值、窗口右端外一行等于阈值。

对照表使用完全不同的 `ROWID`（7003 起），亮度重排，但保持相同的筛选形状。
整数缩放列另放在一个独立的只读对照中，避免把读路径缺陷与筛选语义混为一谈。

## 实际观察

全表谓词 `BRIGHT >= 5.0`：

```text
where 全局索引       [0, 1, 2, 3, 4, 5, 6, 9, 10]         共 9
对应 ROWID           [1001, 1017, 1034, 1051, 1068, 1085, 1102, 1153, 1170]
存储值 >= 5.0 的索引 [0, 1, 5, 6, 7, 8, 9, 10, 11]       共 9
对应 ROWID           [1001, 1017, 1085, 1102, 1119, 1136, 1153, 1170, 1187]
```

窗口 `[3, 9)`、同一谓词：

```text
where 返回（相对）   [0, 1, 2, 3]          <- 窗口内 0 基，不是全表行号
正确全表行           [3, 4, 5, 6]
正确 ROWID           [1051, 1068, 1085, 1102]
按存储值解释（相对） [2, 3, 4, 5]          <- 4 行，但与正确行不同
存储值解释 ROWID     [1085, 1102, 1119, 1136]
把相对索引当全表行   [0, 1, 2, 3]          <- 4 行，同样不是正确行
偏移误读 ROWID       [1001, 1017, 1034, 1051]
```

三组命中数量都是 4；正确行与偏移误读只重叠 1 行，与存储值解释只重叠 2 行。
对照表窗口也恰好 4 行，但 `ROWID` 为 `[7053, 7070, 7104, 7138]`；其存储值
解释为 `[7053, 7087, 7104, 7121]`。数量相同、读回亮度也相同，仍不能据此认定为
同一行。

## 机理：索引与阈值分别在哪一层

`fitsio/hdu/table.py:196-207`：

```python
if firstrow is None:
    firstrow = 0
if lastrow is None:
    lastrow = self._info['nrows']
nrows = lastrow - firstrow
return self._FITS.where(self._ext + 1, expression, firstrow + 1, nrows)
```

- Python 的 `firstrow,lastrow` 是半开区间、0 基。
- native 收到的是 `firstrow + 1`（1 基起始行）和 `nrows`（长度，不是结束行）。
- `fitsio/fitsio_pywrap.c:6340` 拒绝 `firstrow < 1` 或 `nrows < 1`。
- `fitsio/fitsio_pywrap.c:6358` 调 `fits_find_rows`，由 cfitsio 的
  `fffrw`（`fitscore` 的 long-name 映射，实现在 `eval_f.c`）执行谓词。
- `eval_f.c:117-123` 将 native 的 1 基 `firstrow` 转成迭代偏移
  `firstrow - 1`，只读取该窗口。
- `eval_f.c:1053-1055` 把窗口末行限定为 `firstrow + maxRows - 1`，所以
  `lastrow` 不进 native；native 只知道“起点 + 长度”。
- `fitsio_pywrap.c:6385-6393` 遍历 `row_status[0..nrows)`，写入 `i`，所以
  Python 返回的是**窗口相对的 0 基索引**。全表行号必须
  `firstrow + result`；`ROWID` 再经 `rowids[...]` 映射。

因此 `where` 的全表结果 `[0,1,2,3,4,5,6,9,10]` 和窗口结果 `[0,1,2,3]`
表示不同的索引基准：后者不是前者的子集，不能直接回查。

谓词比较层由 cfitsio 决定。`eval_f.c:2450-2485` 对 `TBYTE/TSHORT/TLONG`
读取 `TZEROn/TSCALn`；有缩放时把迭代列声明为 `TDOUBLE`，由
`fits_read_col` 完成物理值转换。`eval_f.c:2287-2322` 对图像列同样按
`BZERO/BSCALE` 决定 `LONG` 或 `DOUBLE`。所以 `where` 比较的是**物理值
（TSCAL/TZERO 已应用）**，不是磁盘存储原值。实测 `>= 5.0` 命中物理值 5.0
的三行，而不是存储值等于 5.0 的三行。

## 三类解释的判定

### 1. 拿错行（偏移误读）

**最小对照**：同一窗口，正确全表行 `[3,4,5,6]` 与直接使用返回的
`[0,1,2,3]`。两者数量相同，但 `ROWID` 仅 1 行重叠。

**实际观察**：`[1001,1017,1034,1051]` 对 `[1051,1068,1085,1102]`。
复核时应固定使用 `firstrow + relative_index`，并把结果还原到独立 `ROWID`。

### 2. 阈值语义不同（缩放层）

**最小对照**：`where('BRIGHT >= 5.0')` 与
`np.where(stored >= 5.0)`。物理层与存储层在同一阈值上给出相同数量 9，
但只有 7 个 `ROWID` 重合；窗口内同样 4 行、2 行重合。

**实际观察**：物理层命中 `[1001,1017,1034,1051,1068,1085,1102,1153,1170]`；
存储层命中 `[1001,1017,1085,1102,1119,1136,1153,1170,1187]`。
若业务阈值定义在存储值上，应先反缩放阈值，或明确改为存储值比较；不能仅凭
读回亮度相同判断。

### 3. 只是返回表示不同

**最小对照**：`where(..., 3, 9)` 与
`np.where(physical[3:9] >= 5.0)[0]` 完全相同，均为 `[0,1,2,3]`。
这是同一批行的窗口局部表示；差异只在索引基准。

**实际观察**：`np.where(physical >= 5.0)[0]` 为
`[0,1,2,3,4,5,6,9,10]`，截取 `[3,9)` 后为 `[3,4,5,6]`，与
`where(...) + 3` 一致。此时无需改动谓词或数据，只需恢复绝对行号。

## 边界与空窗口

- 空窗口 `[3,3)` 在 Python 层通过 `nrows=0` 传给 native，native
  `firstrow < 1 || nrows < 1` 主动抛
  `ValueError: firstrow and nrows must be positive integers`。它没有走
  `fits_find_rows`，也不会返回空数组。
- 直接以 native 参数 `firstrow=4, nrows=0` 调用，得到同一异常；说明空窗口在
  native 入口被同一条校验路径拒绝。
- `firstrow < 0`、`lastrow < firstrow`、`lastrow > nrows` 各有独立异常。
- 合法但无命中（如 `BRIGHT > 1e99`）返回
  `numpy` 空数组（`len=0`），不抛异常。
- 因此“边界拒绝”和“真正无命中”必须保留各自结果，不能把异常吞成空命中。

## 独立的读取层对照

整数存储列（`i2`）、`TSCAL2=0.5`、`TZERO2=1.0` 的对照显示：`where`
仍按物理值工作（`SCALED > 2.0` 命中全局行 `[2,3,4,5]`，符合
`[1.5,2.0,2.5,3.0,3.5,4.0]`），但公开 `read()` 在该快照中返回
`[1,1,1,1,1,1]` 且 `ROWID` 被错误解释。这是读取路径的独立问题，不是
`where` 的窗口或缩放语义；本次不修改任何筛选接口，仅将其作为对照记录下来，
以免把“读回值相同”当作行身份相同的证据。

浮点存储列的对照中，`read()` 与 `where` 的物理值一致，可用于验证
“缩放解释”本身；整数列的异常属于另一层，不应混入行筛选结论。

## 结论

- `where` 返回窗口相对的 0 基索引；绝对行号为 `firstrow + result`。
- Python 端以半开窗口 `[firstrow,lastrow)` 进入 native；native 收到 1 基
  起点和长度，末端由起点加长度决定。
- 谓词比较 cfitsio 应用 `TSCAL/TZERO` 后的物理值。
- 相同命中数量、相同读回亮度都不足以证明同一行；必须还原到独立
  `ROWID` 并用集合/重叠检查。
- 本次未改动原有筛选接口。
