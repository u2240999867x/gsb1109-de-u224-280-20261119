### base — 整组预览审计

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

