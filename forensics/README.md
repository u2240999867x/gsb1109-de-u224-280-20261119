# 分批 OME-TIFF 入库取证手册

本目录只增加取证工具与复现交件，不修改 `tifffile` 的读取行为。所有结论都可以在当前仓库的 `tifffile` 2026.11.16 上重跑。

## 1. 结论先行

同一批四层交件在预览中会出现三种“零值”和两种“像素变了”，它们必须分开判断：

- **来源对、值也是零**：Z0、Z3 分别来自 B 的 IFD0、IFD1，B 两页本来就是全零。零是真实像素，不是缺件。
- **来源对、规则错**：补件后新开 M 做整组预览，Z2 确实取自 M 的 IFD2，但预览按 M 的 IFD1 的 decoder 规则（`predictor=2`）解释。原始条带没有水平差分，却被累加了一次，首行从 `[20,24,29,35]` 变成 `[20,44,73,108]`。这类层必须暂缓。
- **来源错、值是零**：B 未到时，Z0 的 B 引用没有生效，落回 M 的 IFD0；M 的 IFD0 也是真零，所以看起来仍是零。Z3 则根本没有页，是 `None` 加 `fillvalue=0`。这两种零来源不同，处置也不同：Z0 是替代页，Z3 是缺件填充。

根因不是文件内容变了，而是 OME series 组装时给每个文件只挑一个 `TiffPage` 当 keyframe。

- B 缺失时：Z0 仍是先写入的 M IFD0，Z3 是 `None`。M 的第一个页是 IFD0，于是 M 的 IFD1/IFD2 都成为以 IFD0（`predictor=1`）为 keyframe 的 `TiffFrame`；Z1 被错误地按“不分差”解释，Z2 恰好正确。
- B 补齐后新开：Z0 被 B 的 IFD0 覆盖，series 里第一个 M 页变成 IFD1。组装时 IFD1 被提升为 M 的 keyframe（`predictor=2`），IFD2 随之变成借用 IFD1 规则的 `TiffFrame`；Z1 正确，Z2 被错误累加。
- 文件内容、IFD 偏移、条带字节都没有变，变的只是“哪个 IFD 当该文件的解码规则代表”。

## 2. 用到的交件

`forensics/cases/base` 是可重复生成的基线：

| 对象 | IFD | compression | predictor | 真实首行 | 在交件里的角色 |
|---|---|---|---|---|---|
| M | 0 | Deflate | 1 | `[0,0,0,0]` | 初始占位；B 缺失时被 Z0 借用 |
| M | 1 | Deflate | 2（水平差分） | `[10,13,17,22]` | Z1 |
| M | 2 | Deflate | 1 | `[20,24,29,35]` | Z2 |
| B | 0 | Deflate | 1 | `[0,0,0,0]` | Z0 |
| B | 1 | Deflate | 1 | `[0,0,0,0]` | Z3 |

所有页 `4×4`、`uint8`、`rowsperstrip=2`，Deflate 条带布局一致。M 的 OME-XML 先声明 M IFD0→Z0、IFD1→Z1、IFD2→Z2，再用两条 `TiffData` 把 Z0 和 Z3 指向 B 的 IFD0、IFD1。B 自己的 OME-XML 带自己的 UUID。生成命令：

```bash
python forensics/forensic_ome.py build --out forensics/cases
```

## 3. 五次操作的实际结果

`report_scenarios.md` 是原始表格。下面是值班人员需要记住的差别。

| 操作 | Z0 | Z1 | Z2 | Z3 |
|---|---|---|---|---|
| A：B 未到，首次整组预览 | M IFD0，替代页真零 | M IFD1，规则=M IFD0，`[10,3,4,5]` 错 | M IFD2，规则=M IFD0，`[20,24,29,35]` 对 | `None`，`fillvalue=0` |
| B：同一个实例，补件后再看 | 不变 | 不变 | 不变 | 仍为 `None` |
| C：补件后新开，整组预览 | B IFD0，真零 | M IFD1，规则自洽，对 | M IFD2，规则=M IFD1，`[20,44,73,108]` 错 | B IFD1，真零 |
| D：新开，先经页入口缓存 M IFD2，再预览 | 同 C | 同 C | M IFD2 的 `TiffPage` 作自身 keyframe，对 | 同 C |
| E：先整组预览，再查 M IFD2，再看预览 | 同 C | 同 C | 仍是缓存的 `TiffFrame`，错 | 同 C |

关键解释：

- **先看一页会改变后续预览**，只在页缓存或 keyframe 已经建立时发生。D 的 `pages[2]`（或 `pages.get(2, cache=True)`）缓存了真正的 `TiffPage`；它保留自己的 `predictor=1`，所以 C 的错误在 D 消失。`pages[2]` 默认不缓存，只是临时读一次，之后整组预览仍会重建成 `TiffFrame`，不能当成 D 的保护。
- **事后页检查不能改已建好的预览**。E 中 `pages[2]` 返回的是已经缓存的 `TiffFrame`，读数与错误预览一致；`page.aspage()` 会从文件重读 `TiffPage`，能读到正确的 `[20,24,29,35]`，但不会替换 series 中的 frame，也不会清 `_series`。只有新开 `TiffFile` 才会重新组装。
- **补件前后同一实例不刷新**。`TiffFile._series` 已经缓存，`B` 在 A 之后出现也不会触发重新加载 B；B 仍未被登记进 `_files`，Z3 仍是 `None`。

## 4. 怎么看零点

对每个全零层，按顺序做三步，不要只看数值。

1. **看来源**：审计表的 `实际页来源` 和 `预览解码规则`。`None` 是填充；有文件、有 IFD 才是真实页。
2. **看预期**：来源与交件清单（`交件预期`）一致，才是“该来的真零”；否则是替代页或错误引用。
3. **做 255 探针**：预览时用 `fillvalue=255` 重读整组或单层。真实页读数不变（例如 Z0、Z3）；填充页会从 0 变成 255（例如缺件时的 Z3）。这一步能把“真零页”与“0 填充”分开，而且不需要相信灰度图。

`report_base_audit.md` 的 `零值判别` 列会直接给出 `true_zero_expected_page`、`substitute_master_real_page`、`fill_missing_page` 三种标签，并附 255 探针首行。

## 5. 可疑层的像素规则核对

对 `REF_OK_PIXELS_WRONG`，要证明“读的是对的页，用的是错的规则”。

1. 在隔离实例中直接读该层的预期 IFD，例如 `M` 的 IFD2：
   - `predictor=1`；原始条带首行 `[20,24,29,35]`；按自身规则得到 `[20,24,29,35]`。
2. 在预览所属 series 中查该层的 `keyframe`，得到 `keyframe=M.ome.tif:IFD1, predictor=2`。
3. 对同一份原始条带套用 `predictor=2` 的累加规则，得到 `[20,44,73,108]`，与预览首行逐位相等。
4. 因此：引用（FileName+IFD）正确；像素错误来自解码规则选择，不是字节损坏、不是错页。

工具会把这一步自动写成 `像素规则核对` 小表，并检查两个等式：预览等于 keyframe 规则为 True，预览等于源 IFD 自身规则为 False。

## 6. 单条件对照

`report_controls.md` 是自动断言过的矩阵，`verify` 命令跑不过会直接报错。四组对照都只改一个条件：

| 案例 | 唯一变化 | 结果 |
|---|---|---|
| `base` | 无 | Z2 预览 `[20,44,73,108]`，错；Z1 对 |
| `control_ref_order` | 只把 B 的两条 `TiffData` 移到 M 之前 | keyframe 变成 M IFD0；Z1 错 `[10,3,4,5]`，Z2 对 |
| `control_swap_pages` | 只交换 Z1/Z2 指向 M 的 IFD 为 2/1 | Z1（M IFD2）对，Z2（M IFD1）错 `[10,3,4,5]` |
| `control_swap_predictor` | 只交换 M IFD1/IFD2 的 `predictor` 为 1/2 | Z1 对，Z2 错 `[20,4,5,6]` |
| `control_pixels` | 只改 M IFD1/IFD2 的行内像素 | Z1 对 `[200,30,40,50]`，Z2 错 `[90,95,102,111]` |

`control_ref_order` 说明“错的是哪个 keyframe”取决于引用顺序；`control_swap_pages` 说明同一个错误规则会跟着页号落到另一层；`control_pixels` 说明数值换了但错误模式不变：预览仍等于“错误规则下的累加”，而不是任何原件读数。值班人员不需要记住具体页号，只需要按同一套三步核对。

## 7. 值班人员的只读复核流程

对一批新交件，按下面做，不需要整组解码大图：

1. **先验交件齐全**：在打开主件前确认伴随文件都在同一目录，文件名大小写与 OME-XML 完全一致。缺件时打开的实例会缓存“缺件结果”，补件后必须新开，不能复用旧实例。
2. **只查可疑层**：用 `--layers Z2` 只解码该层及它的 keyframe 供体页。工具会同时给出来源、keyframe 规则、原件直读、预览读数、255 探针和入库结论。
3. **来源先对**：`实际页来源` 必须等于清单中的 `FileName`+`IFD`。不等就暂缓，不要继续用像素值解释。
4. **再对像素**：来源对但预览首行与 `原件直读首行` 不等，判为“规则错”，暂缓；如果还要证明，就看 `像素规则核对` 里两个等式的方向。
5. **零点三选一**：`None`+全 255 探针是缺件填充；`M` 的页出现在本应由 `B` 提供的层，是替代页；`B` 的页且清单也要求 `B`，是真零。
6. **决定**：只有 `OK` 才入库；`SOURCE_MISMATCH`、`REF_OK_PIXELS_WRONG`、`FILL_MISSING` 一律暂缓，并在交件单上记录状态、实际来源和首行读数。
7. **复核留痕**：把 `report_base_audit.md`、`report_scenarios.md`、`report_controls.md` 或 `--json` 输出附在交件记录里，不依赖预览截图。

工具命令：

```bash
# 生成全部复现交件与单条件对照
python forensics/forensic_ome.py build --out forensics/cases

# 全量审计（含原件直读、零值 255 探针、规则核对）
python forensics/forensic_ome.py audit --case forensics/cases/base

# 只查可疑层及其 keyframe 供体页
python forensics/forensic_ome.py audit --case forensics/cases/base --layers Z2

# 复现五次操作序列（在临时副本里隐藏/恢复 B，不碰原始交件）
python forensics/forensic_ome.py scenarios --case forensics/cases/base

# 跑单条件对照断言
python forensics/forensic_ome.py verify --cases forensics/cases
```

## 8. 现场操作注意事项

- **不要复用旧 `TiffFile` 判断补件**：`_series` 和 `_files` 都是实例级缓存。补件后要新开。
- **不要把 `pages[i]` 的一次性读取当成缓存**：默认 `cache=False`；只有 `pages.cache=True` 或 `get(..., cache=True)` 才保留 `TiffPage`，才会影响后续 series。
- **不要用 `page.aspage()` 证明预览已修复**：它只返回该页本身，不修改已缓存的 series。
- **不要只看灰度**：真零、替代页真零、0 填充在灰度上完全一样；必须看来源和 255 探针。
- **不要把“引用正确”当成“像素正确”**：本次 Z2 就是引用正确、规则错误。
- **`fillvalue=0` 是缺件时的默认值**，不是文件里的零；判缺件要看 `None` 来源和 255 探针。

## 9. 当前 tifffile 的实现依据

- OME series 在 `series_ome` 中为每个文件维护一个 `keyframes` 字典；同一文件的后续页若不是自身 `TiffPage`，就赋该文件的第一个 keyframe（`tifffile/tifffile.py:19726-19747`）。
- `TiffFrame.decode` 直接返回 `keyframe.decode`（`tifffile/tifffile.py:9830-9840`），而该 decoder 是在 `TiffPage.decode` 初始化时按 keyframe 自己的 `predictor` 选择并缓存的（`tifffile/tifffile.py:6869-6873`, `6929-6933`）；因此 keyframe 的 `predictor` 会直接决定该帧的字节解释。注意 `TiffFrame` 自身没有 `predictor` 属性，`page.predictor` 会报 `AttributeError`，必须读 `page.keyframe.predictor`。
- `TiffFrame.keyframe` 只能设置一次，已设置后报 `cannot reset keyframe`（`tifffile/tifffile.py:9639-9651`）；series 一旦建立，页检查或 `_clear` 都无法把已有的错误 frame 换成正确的 page。
- `TiffPages.set_keyframe` 会把 `TiffPage` 放进 `_loaded` 缓存（`tifffile/tifffile.py:10034-10069`）；`_getitem` 在 `cache=True` 或 keyframe 时直接返回缓存页（`tifffile/tifffile.py:10623-10669`）。这解释了 D 与 C 的差别。
- `TiffFile._series` 按 `(kind, squeeze)` 缓存整个 series（`tifffile/tifffile.py:5098-5120`, `5222-5230`），这是“同实例补件后不刷新”和“事后检查不改预览”的直接原因。
- OME 外部文件缺失时用 `None` 占位并记 `Missing data are filled`（`tifffile/tifffile.py:19604-19622`），预览再按 `fillvalue` 填充（`tifffile/tifffile.py:8276-8308`）。

## 10. 交付物

- `forensics/forensic_ome.py`：生成交件、审计、场景复现、对照断言。
- `forensics/cases/base` 与四个 `control_*`：确定性四层灰度交件及清单。
- `forensics/report_base_audit.md`：基线的逐层审计与规则核对。
- `forensics/report_selective_audit.md`：只查可疑层 Z2 及其 keyframe 供体页的示例输出。
- `forensics/report_selective_audit.json`：同一选择性审计的机器可读版本。
- `forensics/report_scenarios.md`：五次操作序列的来源、读数与差异。
- `forensics/report_controls.md`：单条件对照矩阵。

本次只做取证与复现，没有修改 `tifffile` 的读法。若需要从根上修复“一个文件只保留一个 keyframe”，应单独评审解码器缓存和 OME series keyframe 的分配策略，不应在值班流程里临时改读法。
