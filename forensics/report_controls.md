# 单条件对照矩阵

每个对照都只改变一个条件；其余文件布局、四层尺寸、Deflate 条带、uint8 和零值页保持不变。判断规则：来源匹配但预览与原件直读不符，即 `REF_OK_PIXELS_WRONG`。

| 案例 | 唯一变化 | Z1 来源/预览/状态 | Z2 来源/预览/状态 |
|---|---|---|---|
| base | 无（基线） | M.ome.tif:IFD1 via M.ome.tif:IFD1; [10, 13, 17, 22]; OK | M.ome.tif:IFD2 via M.ome.tif:IFD1; [20, 44, 73, 108]; REF_OK_PIXELS_WRONG |
| control_ref_order | 只把 B 的 TiffData 引用移到 M 之前 | M.ome.tif:IFD1 via M.ome.tif:IFD0; [10, 3, 4, 5]; REF_OK_PIXELS_WRONG | M.ome.tif:IFD2 via M.ome.tif:IFD0; [20, 24, 29, 35]; OK |
| control_swap_pages | 只把 Z1/Z2 指向的 M IFD 交换为 2/1 | M.ome.tif:IFD2 via M.ome.tif:IFD2; [20, 24, 29, 35]; OK | M.ome.tif:IFD1 via M.ome.tif:IFD2; [10, 3, 4, 5]; REF_OK_PIXELS_WRONG |
| control_swap_predictor | 只交换 M IFD1/IFD2 的差分布局 | M.ome.tif:IFD1 via M.ome.tif:IFD1; [10, 13, 17, 22]; OK | M.ome.tif:IFD2 via M.ome.tif:IFD1; [20, 4, 5, 6]; REF_OK_PIXELS_WRONG |
| control_pixels | 只改 M IFD1/IFD2 的行内像素 | M.ome.tif:IFD1 via M.ome.tif:IFD1; [200, 30, 40, 50]; OK | M.ome.tif:IFD2 via M.ome.tif:IFD1; [90, 95, 102, 111]; REF_OK_PIXELS_WRONG |

`base` 与四个 `control_*` 的原始条件保存在各自的 `manifest.json` 中。`verify` 会对 Z1/Z2 的状态、来源和首行做断言，任一不符即以非零状态退出。
