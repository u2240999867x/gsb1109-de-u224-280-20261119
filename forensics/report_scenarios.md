# 同一交件的五次预览与页检查：base

所有读数都来自当前仓库的 tifffile 读取路径。“原件直读”是用新的 TiffFile 直接取某个文件的一个 IFD，不经过 OME series 的跨页 keyframe。

### A_missing_first_preview — B 未到时打开 M 做整组预览

基线：M IFD0 不分差，M IFD1 水平差分，M IFD2 不分差

| 层 | 交件预期 | 实际页来源 | 预览解码规则(keyframe) | 原件直读首行 | 预览首行 | 状态 | 零值判别 | 入库结论 |
|---|---|---|---|---|---|---|---|---|
| Z0 | B.ome.tif:IFD0 | M.ome.tif:IFD0 (TiffPage @ 8) | M.ome.tif:IFD0 predictor=1 | — | [0, 0, 0, 0] | SOURCE_MISMATCH | substitute_master_real_page; fillvalue255 探针首行 [0, 0, 0, 0] (读数变化=False) | 暂缓：实际页来源与交件清单不符 |
| Z1 | M.ome.tif:IFD1 | M.ome.tif:IFD1 (TiffFrame @ 1302) | M.ome.tif:IFD0 predictor=1 | 首行 [10, 13, 17, 22]; predictor=2; raw=[10, 3, 4, 5] | [10, 3, 4, 5] | REF_OK_PIXELS_WRONG | — | 暂缓：引用对但像素按错规则解释 |
| Z2 | M.ome.tif:IFD2 | M.ome.tif:IFD2 (TiffFrame @ 1558) | M.ome.tif:IFD0 predictor=1 | 首行 [20, 24, 29, 35]; predictor=1; raw=[20, 24, 29, 35] | [20, 24, 29, 35] | OK | — | 可入库 |
| Z3 | B.ome.tif:IFD1 | 无（None，fillvalue 填充） | — | — | [0, 0, 0, 0] | FILL_MISSING | fill_missing_page; fillvalue255 探针首行 [255, 255, 255, 255] (读数变化=True) | 暂缓：伴随文件缺失，当前为填充值 |


#### 像素规则核对（来源正确、像素错误）

| 层 | 源 IFD 自身 predictor | 源 IFD 规则下首行 | 预览所用 keyframe predictor | 该规则下首行 | 预览实际首行 | 判定 |
|---|---|---|---|---|---|---|
| Z1 | 2 | [10, 13, 17, 22] | 1 | [10, 3, 4, 5] | [10, 3, 4, 5] | 预览等于自身规则=False; 预览等于 keyframe 规则=True |

核对逻辑：源 IFD 被按 `preview_keyframe_predictor` 解释时，首行恰好等于预览；而按它自己的 predictor 解释时等于原件直读。这就把“读对文件、读错规则”钉在解码规则，而不是字节或引用。

- Z0 的后续 TiffData 引用未生效，仍指向 M IFD0 的真实零页。
- Z3 无页可选，按 fillvalue=0 填充。
- 唯一 M keyframe 是 M IFD0（predictor=1），M IFD1/2 都以它为规则。

### B_same_instance_after_companion — 保留 B 未到时打开的实例；补件后同实例重看

基线：M IFD0 不分差，M IFD1 水平差分，M IFD2 不分差

| 层 | 交件预期 | 实际页来源 | 预览解码规则(keyframe) | 原件直读首行 | 预览首行 | 状态 | 零值判别 | 入库结论 |
|---|---|---|---|---|---|---|---|---|
| Z0 | B.ome.tif:IFD0 | M.ome.tif:IFD0 (TiffPage @ 8) | M.ome.tif:IFD0 predictor=1 | 首行 [0, 0, 0, 0]; predictor=1; raw=[0, 0, 0, 0] | [0, 0, 0, 0] | SOURCE_MISMATCH | substitute_master_real_page; fillvalue255 探针首行 [0, 0, 0, 0] (读数变化=False) | 暂缓：实际页来源与交件清单不符 |
| Z1 | M.ome.tif:IFD1 | M.ome.tif:IFD1 (TiffFrame @ 1302) | M.ome.tif:IFD0 predictor=1 | 首行 [10, 13, 17, 22]; predictor=2; raw=[10, 3, 4, 5] | [10, 3, 4, 5] | REF_OK_PIXELS_WRONG | — | 暂缓：引用对但像素按错规则解释 |
| Z2 | M.ome.tif:IFD2 | M.ome.tif:IFD2 (TiffFrame @ 1558) | M.ome.tif:IFD0 predictor=1 | 首行 [20, 24, 29, 35]; predictor=1; raw=[20, 24, 29, 35] | [20, 24, 29, 35] | OK | — | 可入库 |
| Z3 | B.ome.tif:IFD1 | 无（None，fillvalue 填充） | — | 首行 [0, 0, 0, 0]; predictor=1; raw=[0, 0, 0, 0] | [0, 0, 0, 0] | FILL_MISSING | fill_missing_page; fillvalue255 探针首行 [255, 255, 255, 255] (读数变化=True) | 暂缓：伴随文件缺失，当前为填充值 |


#### 像素规则核对（来源正确、像素错误）

| 层 | 源 IFD 自身 predictor | 源 IFD 规则下首行 | 预览所用 keyframe predictor | 该规则下首行 | 预览实际首行 | 判定 |
|---|---|---|---|---|---|---|
| Z1 | 2 | [10, 13, 17, 22] | 1 | [10, 3, 4, 5] | [10, 3, 4, 5] | 预览等于自身规则=False; 预览等于 keyframe 规则=True |

核对逻辑：源 IFD 被按 `preview_keyframe_predictor` 解释时，首行恰好等于预览；而按它自己的 predictor 解释时等于原件直读。这就把“读对文件、读错规则”钉在解码规则，而不是字节或引用。

- 同一个 TiffFile 实例的 _series 缓存已建立，不会因 B 出现而重跑 OME 解析。
- 来源仍是 M IFD0/1/2，Z3 仍是 None 填充；与补件前的读数逐位相同。

补件前同实例读数：
| 层 | 实际页来源 | 原件直读首行 | 预览首行 | 状态 | 零值判别 |
|---|---|---|---|---|---|
| Z0 | M.ome.tif:IFD0 | — | [0, 0, 0, 0] | SOURCE_MISMATCH | substitute_master_real_page |
| Z1 | M.ome.tif:IFD1 | [10, 13, 17, 22] | [10, 3, 4, 5] | REF_OK_PIXELS_WRONG | — |
| Z2 | M.ome.tif:IFD2 | [20, 24, 29, 35] | [20, 24, 29, 35] | OK | — |
| Z3 | None | — | [0, 0, 0, 0] | FILL_MISSING | fill_missing_page |

### C_new_instance_whole_preview — B 补齐后新开 M 做整组预览

基线：M IFD0 不分差，M IFD1 水平差分，M IFD2 不分差

| 层 | 交件预期 | 实际页来源 | 预览解码规则(keyframe) | 原件直读首行 | 预览首行 | 状态 | 零值判别 | 入库结论 |
|---|---|---|---|---|---|---|---|---|
| Z0 | B.ome.tif:IFD0 | B.ome.tif:IFD0 (TiffPage @ 8) | B.ome.tif:IFD0 predictor=1 | 首行 [0, 0, 0, 0]; predictor=1; raw=[0, 0, 0, 0] | [0, 0, 0, 0] | OK | true_zero_expected_page; fillvalue255 探针首行 [0, 0, 0, 0] (读数变化=False) | 可入库 |
| Z1 | M.ome.tif:IFD1 | M.ome.tif:IFD1 (TiffPage @ 1302) | M.ome.tif:IFD1 predictor=2 | 首行 [10, 13, 17, 22]; predictor=2; raw=[10, 3, 4, 5] | [10, 13, 17, 22] | OK | — | 可入库 |
| Z2 | M.ome.tif:IFD2 | M.ome.tif:IFD2 (TiffFrame @ 1558) | M.ome.tif:IFD1 predictor=2 | 首行 [20, 24, 29, 35]; predictor=1; raw=[20, 24, 29, 35] | [20, 44, 73, 108] | REF_OK_PIXELS_WRONG | — | 暂缓：引用对但像素按错规则解释 |
| Z3 | B.ome.tif:IFD1 | B.ome.tif:IFD1 (TiffFrame @ 630) | B.ome.tif:IFD0 predictor=1 | 首行 [0, 0, 0, 0]; predictor=1; raw=[0, 0, 0, 0] | [0, 0, 0, 0] | OK | true_zero_expected_page; fillvalue255 探针首行 [0, 0, 0, 0] (读数变化=False) | 可入库 |


#### 像素规则核对（来源正确、像素错误）

| 层 | 源 IFD 自身 predictor | 源 IFD 规则下首行 | 预览所用 keyframe predictor | 该规则下首行 | 预览实际首行 | 判定 |
|---|---|---|---|---|---|---|
| Z2 | 1 | [20, 24, 29, 35] | 2 | [20, 44, 73, 108] | [20, 44, 73, 108] | 预览等于自身规则=False; 预览等于 keyframe 规则=True |

核对逻辑：源 IFD 被按 `preview_keyframe_predictor` 解释时，首行恰好等于预览；而按它自己的 predictor 解释时等于原件直读。这就把“读对文件、读错规则”钉在解码规则，而不是字节或引用。

- B 被载入，Z0/Z3 指向 B IFD0/1；Z1/Z2 指向 M IFD1/2。
- 第一个 M 页（IFD1，predictor=2）成为该文件在 series 内的 keyframe；M IFD2 被按 predictor=2 解码。

### D_page_ifd2_cached_first — 新开 M，先经页入口读取并缓存 M IFD2，再整组预览

基线：M IFD0 不分差，M IFD1 水平差分，M IFD2 不分差

| 层 | 交件预期 | 实际页来源 | 预览解码规则(keyframe) | 原件直读首行 | 预览首行 | 状态 | 零值判别 | 入库结论 |
|---|---|---|---|---|---|---|---|---|
| Z0 | B.ome.tif:IFD0 | B.ome.tif:IFD0 (TiffPage @ 8) | B.ome.tif:IFD0 predictor=1 | 首行 [0, 0, 0, 0]; predictor=1; raw=[0, 0, 0, 0] | [0, 0, 0, 0] | OK | true_zero_expected_page; fillvalue255 探针首行 [0, 0, 0, 0] (读数变化=False) | 可入库 |
| Z1 | M.ome.tif:IFD1 | M.ome.tif:IFD1 (TiffPage @ 1302) | M.ome.tif:IFD1 predictor=2 | 首行 [10, 13, 17, 22]; predictor=2; raw=[10, 3, 4, 5] | [10, 13, 17, 22] | OK | — | 可入库 |
| Z2 | M.ome.tif:IFD2 | M.ome.tif:IFD2 (TiffPage @ 1558) | M.ome.tif:IFD2 predictor=1 | 首行 [20, 24, 29, 35]; predictor=1; raw=[20, 24, 29, 35] | [20, 24, 29, 35] | OK | — | 可入库 |
| Z3 | B.ome.tif:IFD1 | B.ome.tif:IFD1 (TiffFrame @ 630) | B.ome.tif:IFD0 predictor=1 | 首行 [0, 0, 0, 0]; predictor=1; raw=[0, 0, 0, 0] | [0, 0, 0, 0] | OK | true_zero_expected_page; fillvalue255 探针首行 [0, 0, 0, 0] (读数变化=False) | 可入库 |

- cache=True 时 pages[2] 返回并缓存真正的 TiffPage；它保留自己的 predictor=1。
- series 组装时该 TiffPage 作为自身 keyframe，M IFD2 不再被 M IFD1 的规则覆盖。
- 页入口当时的 M IFD2 首行直读：[20, 24, 29, 35]。

页入口（cache=True）读取 M IFD2 首行：[20, 24, 29, 35]

### E_preview_then_page_check — 先整组预览，再做同样的页检查并重看预览

基线：M IFD0 不分差，M IFD1 水平差分，M IFD2 不分差

| 层 | 交件预期 | 实际页来源 | 预览解码规则(keyframe) | 原件直读首行 | 预览首行 | 状态 | 零值判别 | 入库结论 |
|---|---|---|---|---|---|---|---|---|
| Z0 | B.ome.tif:IFD0 | B.ome.tif:IFD0 (TiffPage @ 8) | B.ome.tif:IFD0 predictor=1 | 首行 [0, 0, 0, 0]; predictor=1; raw=[0, 0, 0, 0] | [0, 0, 0, 0] | OK | true_zero_expected_page; fillvalue255 探针首行 [0, 0, 0, 0] (读数变化=False) | 可入库 |
| Z1 | M.ome.tif:IFD1 | M.ome.tif:IFD1 (TiffPage @ 1302) | M.ome.tif:IFD1 predictor=2 | 首行 [10, 13, 17, 22]; predictor=2; raw=[10, 3, 4, 5] | [10, 13, 17, 22] | OK | — | 可入库 |
| Z2 | M.ome.tif:IFD2 | M.ome.tif:IFD2 (TiffFrame @ 1558) | M.ome.tif:IFD1 predictor=2 | 首行 [20, 24, 29, 35]; predictor=1; raw=[20, 24, 29, 35] | [20, 44, 73, 108] | REF_OK_PIXELS_WRONG | — | 暂缓：引用对但像素按错规则解释 |
| Z3 | B.ome.tif:IFD1 | B.ome.tif:IFD1 (TiffFrame @ 630) | B.ome.tif:IFD0 predictor=1 | 首行 [0, 0, 0, 0]; predictor=1; raw=[0, 0, 0, 0] | [0, 0, 0, 0] | OK | true_zero_expected_page; fillvalue255 探针首行 [0, 0, 0, 0] (读数变化=False) | 可入库 |


#### 像素规则核对（来源正确、像素错误）

| 层 | 源 IFD 自身 predictor | 源 IFD 规则下首行 | 预览所用 keyframe predictor | 该规则下首行 | 预览实际首行 | 判定 |
|---|---|---|---|---|---|---|
| Z2 | 1 | [20, 24, 29, 35] | 2 | [20, 44, 73, 108] | [20, 44, 73, 108] | 预览等于自身规则=False; 预览等于 keyframe 规则=True |

核对逻辑：源 IFD 被按 `preview_keyframe_predictor` 解释时，首行恰好等于预览；而按它自己的 predictor 解释时等于原件直读。这就把“读对文件、读错规则”钉在解码规则，而不是字节或引用。

- 预览已经把 M IFD2 解析成 TiffFrame，keyframe=M IFD1（predictor=2），并缓存了整个 series。
- pages[2] 返回该 TiffFrame，读数仍是错误的 cumsum 结果。
- page.aspage() 从文件重读真正的 TiffPage（predictor=1），能给出正确读数，但不会替换 series 中的 TiffFrame。
- 重看预览仍使用已缓存的 series，错误不会改变；必须新开 TiffFile。

预览后 pages[2]（缓存的 TiffFrame）首行：[20, 44, 73, 108]
同一页 page.aspage() 首行：[20, 24, 29, 35]

页检查之后再次整组预览：
| 层 | 实际页来源 | 原件直读首行 | 预览首行 | 状态 | 零值判别 |
|---|---|---|---|---|---|
| Z0 | B.ome.tif:IFD0 | [0, 0, 0, 0] | [0, 0, 0, 0] | OK | true_zero_expected_page |
| Z1 | M.ome.tif:IFD1 | [10, 13, 17, 22] | [10, 13, 17, 22] | OK | — |
| Z2 | M.ome.tif:IFD2 | [20, 24, 29, 35] | [20, 44, 73, 108] | REF_OK_PIXELS_WRONG | — |
| Z3 | B.ome.tif:IFD1 | [0, 0, 0, 0] | [0, 0, 0, 0] | OK | true_zero_expected_page |

