# 索引交接单（模板）

交付方填写此单并与索引一起移交。任何“未提供”项都视为接手方无法证明的结论。

## 1. 交付物

| 项 | 路径 | SHA-256 | 说明 |
|---|---|---|---|
| 索引根目录 | | | 不含缓存、不含临时重建 |
| 父索引 | `layout_index.sqlite` | | |
| 子索引 | `derivatives/<folder>/layout_index.sqlite` | | 每行一个 |
| 根描述快照 | `dataset_description.json` | | 建库时字节 |
| 配置快照 | | | 每个传入路径一个 |
| 派生根描述快照 | | | 每个子根一个 |
| 文件清单 | | | 路径+大小+SHA-256 |
| 绑定清单 | `BINDING_MANIFEST.json` | | |

## 2. 建库调用

- 命令/脚本原文：
- 根绝对路径（建库时）：
- `config`（原样，含名称或路径）：
- `derivatives`：
- `validate`：
- `indexer`：`ignore` / `force_index` / `index_metadata` / `config_filename`
- `reset_database`：
- PyBIDS 版本 / commit：
- `bidsschematools` 版本；若用 schema，解析到的 `bids_version`：
- Python / SQLAlchemy：
- 主机 / 起止时间 / 退出码：

## 3. 根与索引绑定

对每个根（父 + 每个子）填写：

| 字段 | 值 |
|---|---|
| `root_id` |
| 父 `root_id`（子根） |
| 目录名（子根） |
| `layout_info.root` |
| `layout_info._config` |
| `layout_info._derivatives` |
| 库内配置名 |
| 库内实体清单（或指向 `EVIDENCE.json`） |
| `rule_sha256`（本方案规范化） |
| 建库描述 SHA-256 |
| 当前描述 SHA-256 |
| 建库配置 SHA-256 |
| 当前配置 SHA-256 |
| 绑定结论（可证 / 仅库内自洽） |

## 4. 访问条件

- 接手方挂载路径：
- 是否与建库绝对路径相同：是 / 否
- 若否：
  - [ ] 只做标签/实体查询，不按 `path` 取文件
  - [ ] 已提供绝对路径重写脚本与验收用例（禁止 `reset_database`）
- 只读校验：

## 5. 已知差异

| 文件/实体 | 旧库值 | 当前规则值 | 证据 |
|---|---|---|---|
| | | | |

## 6. 声明

- [ ] 未在交付库上执行过 `reset_database=True`
- [ ] 未在交付库上执行过 `save()` 覆盖
- [ ] 所有“当前描述/配置”与“建库快照”的差异已列在第 5 节
- [ ] 无法证明的事项已明确标注，未用当前 JSON 反推建库规则

交付方：____________  日期：____________
接手方：____________  日期：____________
