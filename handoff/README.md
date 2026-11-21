# PyBIDS 索引交接复核方案

面向“另一个实验室接手索引库 + 当前来件、没有原建库机器”的场景。目标不是重新解释数据，
而是让接手人能回答一个可审计的问题：**某条命中，究竟是当前目录里的描述/规则产生的，
还是建库时那份已经不在场的描述/规则产生的？**

本目录自带一个小型可运行样例和只读复核脚本，结论均由实际运行产生：

- `build_fixture.py` 构造归档根、建库时字节快照、旧库和另路径新库
- `audit_handoff.py` 只读地 dump 旧库实际保存的规则，并做重开/传参/对照查询
- `relocation_probe.py` 验证换机器、改根、缺描述、缺配置时的失败方式
- `verify.sh` 一键重跑，生成 `EVIDENCE.json`、`BINDING_MANIFEST.json`、`RELOCATION.json`

不修改 PyBIDS 源码；所有会写库的操作只发生在 `.work/` 下的副本上。

## 1. 先记住五条机制事实

这五条决定了整个交接方案，均可在本仓库源码和生成证据中核对。

1. **旧库重开时，`root`、`derivatives`、`config` 三个参数由库内 `layout_info` 覆盖。**
   传入的 `config=`/`root=` 不会生效（`src/bids/layout/layout.py:147-160`）。
   因此“重开时指定当前配置”不能证明旧库按当前配置建成。
2. **库保存的是解析后的规则快照，不是建库输入。**
   `entities.pattern`、`configs.name`、`config_to_entity_map` 是实际生效的规则
   （`src/bids/layout/models.py:101-311`）；`layout_info._config` 只记传入的路径/名称。
   路径上的 JSON 后来被改，库内正则不随之改变；反之，把同名 JSON 塞回去也不构成证据。
3. **描述文件完全不入库。**
   `dataset_description.json` 每次打开都现场读取（`src/bids/layout/validation.py:40-124`）；
   DB schema 中没有 `BIDSVersion`、`DatasetType`、`GeneratedBy`、`PipelineDescription`、
   `source_pipeline` 或任何内容哈希。所以“库能查到文件”与“当前描述是什么”是两条独立事实。

其中最后两条是交接硬约束：

4. **`files.path` / `tags.file_path` / `associations` 存的是建库时的绝对路径。**
   把 DB 拷到另一台机器，即使手工改 `layout_info.root`，查询仍返回旧机器的绝对路径，
   `BIDSLayout.get_file()` 和 `BIDSFile.relpath` 随之失效
   （`src/bids/layout/models.py:482-487`；见 `RELOCATION.json`）。
5. **派生数据集按目录发现，按显示名解析。**
   父库没有子库注册表；`derivatives/<folder>/dataset_description.json` 现场读取，
   显示名取自 `GeneratedBy[0].Name`，缺失时回退 `PipelineDescription.Name`
   （`src/bids/layout/validation.py:101-115`）。显示名与目录名可不同，且可在归档后被改。
   仅凭显示名 `dataset.derivatives[<name>]` 解析会产生歧义或指错子库
   （`src/bids/layout/models.py:1001-1041`）。

## 2. 样例场景

`build_fixture.py` 生成：

```text
archive_root/                          唯一的归档根，建库后原地被改
  dataset_description.json              BIDSVersion 1.11.2 -> 1.9.0，并加 GeneratedBy
  configs/incoming_entities.json        建库时传入的配置路径，内容后改为新实体词表
  sub-01/micr/..._stain-HE_desc-foo_TEM.png
  derivatives/deriv_legacy/             PipelineDescription 自述；建库后改为 GeneratedBy
  derivatives/deriv_modern/             GeneratedBy.Name = "deriv_legacy"（与兄弟目录名/显示名重叠）
  derivatives/no_description/           文件名合规但无 dataset_description.json
  orphan/                               文件名合规但无 dataset_description.json
build_snapshot/                         建库那一刻的字节快照（交接必须保存的证据）
archive_db/                             建库后从未重建的旧索引
rebuilt_db/                             在另一路径、用当前文件真实新建的对照索引
```

关键设计：

- 根描述在建库时自述 `BIDSVersion: 1.11.2`（较新），但第一次建库显式传入的是
  `configs/incoming_entities.json` 这份旧实体配置。这正是要防止的“描述看起来新、
  规则其实是旧”的组合。
- 在争议实体上，旧配置只定义 `staining`/`proc`，当前配置只定义
  `stain`/`processing`/`description`（两代都保留 `subject`/`sample`/`suffix` 等公共实体）。
  同一文件 `..._stain-HE_desc-foo_TEM.png` 在旧库得到 `staining=HE`，在新库得到
  `stain=HE` 和 `description=foo`。
- 两个派生根的显示名都解析为 `deriv_legacy`：一个来自自身 `PipelineDescription`，
  一个来自 `GeneratedBy.Name`。这使“按显示名找子库”在**建库当时**就已经有歧义。
- `deriv_legacy` 的描述在归档后又被改，使重开旧库时显示名静默变化。

## 3. 交接时最少必须保存的证据

按“能不能在接手方复算”排序。没有第 1-3 项，后面的结论只能降级为“库内自洽”。

**A. 建库输入快照（最重要，且必须与 DB 分离存放）**

1. 建库时的根 `dataset_description.json` 原始字节 + SHA-256。
2. 传给 `BIDSLayout(config=[...])` 的每一个路径的原始字节 + SHA-256；
   如果传的是名称（`bids` / `bids-schema` / `derivatives`），记录解析后的实际文件或
   schema 版本，而不只是名称。
3. 每个派生根建库时的 `dataset_description.json` 原始字节 + SHA-256（用于证明显示名来源）。
4. 建库时根目录下所有文件（至少所有描述、配置、`layout_config.json`）的路径清单 +
   SHA-256 + 大小。`build_snapshot` 的 `tree_sha256` 是这一项的汇总。
5. 若目录内有 `layout_config.json`，逐个记录其相对路径、字节、SHA-256；它参与该目录
   及子目录的实体合并（`src/bids/layout/index.py:188-208`），且完全不入库。

**B. 建库调用本身**

6. 实际命令行/脚本，至少包含：`root`、`config`、`derivatives`、`validate`、`indexer` 的
   `ignore`/`force_index`/`index_metadata`/`config_filename`、`reset_database`。
7. PyBIDS 版本（含 commit 或 wheel 哈希）、`bidsschematools` 版本、Python 与 SQLAlchemy 版本。
8. 建库起止时间、执行主机、退出码。
9. 若用 schema：`bids_version` / `schema_path` 及其内容哈希；`bids-schema` 的解析结果
   随 `bidsschematools` 版本变化，仅写 `bids-schema` 不够。

**C. 索引库自身**

> 本目录的 `rule_sha256` / `tree_sha256` 是本方案定义的规范化 SHA-256
> （见 `audit_handoff.py` 的 `canon()`），不是 PyBIDS 内建字段。交接时不要只交哈希：
> 必须同时交 `entities`/`configs` 明细或原始字节，否则接手方无法复算。

10. 每个 `layout_index.sqlite` 的 SHA-256（父库与每个子库分开）。
11. 每个库的 `layout_info` 整行、`configs`/`entities`/`config_to_entity_map` 全量导出，
    或至少 `EVIDENCE.json` 中的 `rule_sha256` 与规则明细。
12. 库的 `files`/`tags` 行数与全量导出（或可复算的摘要）。
13. 父库到子库的目录结构清单（`derivatives/<folder>/layout_index.sqlite`）。

**D. 绑定与访问**

14. 一份 `BINDING_MANIFEST.json`：每个根、每个子库、建库绝对路径、当前路径、
    描述快照、配置快照、库指纹的对应关系。
15. 接手方机器的只读挂载路径；若与原绝对路径不同，必须同时提供“重写方案”或
    接受“只能做标签级查询，不能按路径取文件”的限制。

**E. 不需要保存但常被误当证据的**

- 仅 `layout_info._config` 的路径字符串：同名路径内容可改，不能证明规则。
- 仅 `configs.name`：本样例两代规则都叫 `legacy-entities-v1`，名称完全相同。
- 仅当前目录里的 `dataset_description.json`：它从不入库，重开时才读，不能反推建库时。
- 仅查询命中列表：不同规则可给出相同命中（见第 5 节第二组）。

## 4. 每份根和索引怎么绑定

`BINDING_MANIFEST.json` 是机器可读版本。绑定规则如下。

**4.1 父根 = 一个 `layout_info` + 一份描述快照 + 一个规则快照**

一个索引目录唯一对应一个根，绑定键是库内 `layout_info.root`（绝对路径），不是当前目录名。
一条完整绑定必须同时成立：

| 检查 | 证据 |
|---|---|
| `layout_info.root` == 建库时根的绝对路径 | `layout_info.root` 与建库日志/清单 |
| `layout_info._derivatives` == 建库时 `derivatives` 参数 | `layout_info._derivatives` |
| `layout_info._config` == 建库时传入的每个配置路径/名称 | `layout_info._config` |
| 库内 `entities`/`configs` == 建库时配置内容的解析结果 | `rule_sha256` 对比 |
| 当前根的 `dataset_description.json` == 建库时快照 | 两个 SHA-256 |
| 当前配置路径的字节 == 建库时快照 | 两个 SHA-256 |

任何一项为“否”，该索引只能声明“按库内规则自洽”，不能声明“按当前目录描述建立”。

**4.2 派生根 = 父库下的目录 + 子库 `layout_info` + 子描述快照**

父库不保存子库清单，子库也不保存父库指针。绑定必须用三元组：

```text
(父根, derivatives/<folder>/, 子库 layout_info.root)
```

而不是显示名。`source_pipeline` 是**每次打开时**从当前子描述重算的，不是建库事实；
本样例中它在建库时是 `deriv_legacy`，归档后重开变成 `post_archive_display`。

规则：

- 子库目录名以父库 `derivatives/` 下的**目录名**为准；`derivatives/deriv_modern` 的
  显示名是 `deriv_legacy`，两者不可互换。
- 子库 `layout_info.root` 必须等于 `<父根>/derivatives/<folder>`；否则该子库不属于此父根。
- 每个子库单独记 `rule_sha256`：本样例所有子库都用默认 `['bids','derivatives']`，
  父库用自定义配置；父库配置**不会**下传给子库（`src/bids/layout/layout.py:203-215`,
  `610-664`）。
- 子库描述缺失时，该目录会被静默跳过（`RELOCATION.json` 中 `missing_derivative_description`
  只剩 `deriv_modern`），但旧子库文件仍在磁盘上。交接时必须把“描述缺失”和“索引缺失”
  分开记录。

**4.3 路径绑定**

`files.path` 是绝对路径，无法用“相对父根”自动修复。交接时应二选一：

- 接手方在与建库完全相同的绝对路径只读挂载；或
- 明确记录“本索引只用于标签/实体查询，不用于按 `path` 取文件”，并把 `RELOCATION.json`
  的失败模式写入交接说明。

不要用 `reset_database=True` 去“修复”路径：它会丢弃旧索引并按当前文件重建
（`src/bids/layout/db.py:26-43`, `102-112`），这正是要防止的“把旧证据替换成新规则”。

## 5. 实测结论（本仓库实际运行）

### 5.0 一页对照：库内规则 / 现场自述 / 入库标签 / 按根查询

| 对象 | 库内实际保存 | 打开时现场读取 | 实际入库标签 | 按根查询 |
|---|---|---|---|---|
| `archive_db` 父库 | `configs.name=legacy-entities-v1`；`entities` 共 7 个，含 `staining`/`proc`，无 `stain`/`description` | 根描述已是 `BIDSVersion 1.9.0` + `GeneratedBy`；配置路径上的 JSON 已换成 `stain`/`description` | `..._stain-HE_desc-foo_TEM.png` 只有 `staining=HE`，无 `stain`/`description` | `get(staining='HE')` 命中 2 个；`get(stain='HE')` 报未识别实体；`get(subject='01',suffix='T1w',scope='raw')` 命中 `orphan/` 与 `sub-01/` 两个 T1w |
| `rebuilt_db`（另路径真实重建） | 同一 `configs.name`，但 `entities` 为 `stain`/`processing`/`description` 等新集合 | 同上（同一当前根） | 同一文件得到 `stain=HE` + `description=foo` | `get(stain='HE')` 命中 2 个；`get(staining='HE')` 无此实体；T1w 命中与旧库相同 |
| `archive_db/derivatives/deriv_legacy` 子库 | 默认 `bids`+`derivatives` 规则；`layout_info.root` 指向该目录 | 归档后描述改为 `GeneratedBy.Name=post_archive_display` | 文件名 `desc-deriv_legacy_bold` 实际得到 `desc=deriv`：旧 `derivatives` 配置的 `desc-([a-zA-Z0-9+]+)` 遇 `_` 截断 | 重开后 `source_pipeline=post_archive_display`；`get_pipeline('deriv_legacy')` 静默指向 `deriv_modern` |
| `archive_db/derivatives/deriv_modern` 子库 | 默认 `bids`+`derivatives` 规则；`layout_info.root` 指向该目录 | `GeneratedBy.Name=deriv_legacy`（与兄弟目录名重叠） | 同样 `desc=deriv`（`_` 截断），与目录名无关 | 显示名 `deriv_legacy` 与 `deriv_legacy` 子库在建库时就多义 |

关键点：父库和两个子库都**没有**保存上表“打开时现场读取”一列；可复算的是
“库内实际保存”“实际入库标签”“按根查询”三列。`rebuilt_db` 与 `archive_db` 的配置路径
字符串、配置名完全相同，但入库标签和规则快照不同。

### 5.1 同路径、不同实体（必查项）

文件：`sub-01/micr/sub-01_sample-A_stain-HE_desc-foo_TEM.png`

| 实体 | `archive_db`（旧规则） | `rebuilt_db`（当前规则） |
|---|---|---|
| `staining` | `HE` | — |
| `stain` | — | `HE` |
| `description` | — | `foo` |
| 其余（subject/sample/suffix/datatype/extension） | 相同 | 相同 |

旧库的 `layout_info._config` 与“当前配置路径”字符串完全相同，`configs.name` 也相同
（`legacy-entities-v1`），但库内 7 个 `entities` 里只有 `staining`/`proc`，没有 `stain`/`description`。
这证明：**路径相同、名称相同，规则仍可不同；必须以库内正则为准。**

旧库上 `layout.get(stain='HE')` 直接报
`ValueError: 'stain' is not a recognized entity`（建议 `staining`），
`layout.get(description='foo')` 同样不可用；新库两者都正常。这是“看似同一查询、实际不可比”
的最短判别。

### 5.2 命中相同、但规则版本无相同证据（必查项）

> 这一组是有意构造的“看似一致”：`orphan/` 与 `sub-01/` 的 T1w 在两代规则下都会被
> `subject`/`suffix` 命中，规则差异不影响这组查询。它不是“任何查询都会一致”，
> 而是“只用命中无法区分规则”。与 5.1 的差异组合起来，才是完整判据。

查询：`subject='01', suffix='T1w', scope='raw'`

- 旧库与新建库都返回同样两个文件：
  `orphan/sub-01/anat/sub-01_T1w.nii.gz` 和 `sub-01/anat/sub-01_T1w.nii.gz`。
- `hits_equal == true`，但 `rule_sha256` 不同（`archive_db` 与 `rebuilt_db` 的
  `entities`/`configs` 不同）。
- 两库的 `layout_info._config` 字符串相同，`configs.name` 也相同。

因此：**命中一致不能推出规则一致**。如果旧交付只有重开结果和当前 JSON，最多只能说
“在这组查询上两者命中相同”；不能说“旧库是按当前规则建的”。要确定规则版本，必须比对
`rule_sha256` 或库内 `entities`，而这两者只有在交付时保存了规则快照或原始配置字节才可复算。

### 5.3 旧库重开的行为

- 重开时传 `config=[当前配置]`：`config_names` 仍为 `['legacy-entities-v1']`，
  库内实体仍只有旧集合；传参被忽略。
- 重开时传 `root=/不存在`：`reported_root` 仍为库内旧根；传参被忽略。
- 重开时传 `derivatives=False`：仍加载两个子库；该版本由库内 `_derivatives` 覆盖。
- 重开不修改任何 `.sqlite` 文件（`databases_unchanged_by_audit == true`）。
- 但根 `description` 是现场读的：归档后改成 `BIDSVersion 1.9.0`，重开就显示 1.9.0，
  与库内规则版本无关。

### 5.4 派生显示名与目录名重叠

建库时：`deriv_legacy` 来自 `PipelineDescription`，`deriv_modern` 的
`GeneratedBy.Name` 也是 `deriv_legacy`，两者**在建库当时**就同名。

- `get_pipeline('deriv_legacy')` 在建库时和重开时都报
  `BIDSChildDatasetError`（多义），必须用目录名 `derivatives/deriv_legacy`。
- 归档后把 `deriv_legacy` 的描述改为 `GeneratedBy.Name = post_archive_display`：
  重开时 `source_pipeline` 立刻变为 `post_archive_display`，`get_pipeline('deriv_legacy')`
  不再报错，而是**静默指向 `deriv_modern`**。
  这是最危险的 drift：同一句代码，归档前后指向不同子库，且没有报错。
- `derivatives['deriv_legacy']` 同样会退回显示名解析，得到 `deriv_modern`；
  只有 `derivatives['derivatives/deriv_legacy']` 是稳定的目录键。
- 同一子库的标签也不能从文件名想当然：本样例里 `desc-deriv_legacy_bold` 和
  `desc-deriv_modern_bold` 在默认 `derivatives` 配置下都得到 `desc='deriv'`
  （正则 `desc-([a-zA-Z0-9+]+)` 在 `_` 处截断），与目录名无关。新旧两库在这点上相同，
  因此它不能用来区分规则版本，但提醒接手方：实体值必须以 `tags` 表为准。

### 5.5 无描述的目录

- `orphan/`（不在 `derivatives/` 下）：`validate=False` 时被当作 raw 索引，
  旧库和新库都收录同一文件。改用 `validate=True` 重建则不会收录（`RELOCATION.json` 的
  `current_root_validate_true.orphan_included == false`），且 `sub-01/micr/..._desc-foo_TEM.png`
  也会因后缀/实体不被 validator 接受而消失。`validate` 不入库，却直接改变收录集合。
- `derivatives/no_description/`：文件名完全合规，但没有 `dataset_description.json`，
  派生发现阶段直接跳过；父库和子库都不收录。它不会报错，只是“消失”。
- 两种“无描述”行为不同，且都不入库。交接时必须把目录清单和“是否被索引”分开记录。

### 5.6 换机器/换路径

`RELOCATION.json` 实测：

- 只拷 DB、根仍在原路径：重开报告原根，查询返回原绝对路径；可用但绑定的是原机器路径。
- 根移到新路径、DB 未改：重开仍报告原根，查询返回原路径（旧机器文件）。
- 根移到新路径、同时改 `layout_info.root`（父子都改）：`reported_root` 变新根，
  但 `files.path` 仍是旧绝对路径；`relpath` 抛
  `ValueError: ... is not in the subpath of ...`，`get_file('sub-01/anat/sub-01_T1w.nii.gz')`
  返回 `None`（`RELOCATION.json` 的 `get_file_relative_after_relocation == null`）。
  相对路径查询可以“看起来能跑”，但取文件失败。
- 删根描述：默认 `validate=True` 直接 `BIDSValidationError`；`validate=False` 可开，
  但 `description=None`。
- 删某派生根描述：该子库从 `layout.derivatives` 消失，旧子库文件仍在。

结论：本索引不是可搬迁的“便携证据”。交接必须绑定原绝对路径，或明确降级为标签查询，
并提供重写绝对路径的脚本与验收用例（不要用 `reset_database`）。

## 6. 只有“重开结果 + 当前 JSON”的旧交付：能确定与必须留待核对

设旧交付 = 一个能重开的索引目录 + 当前目录里的描述和配置 JSON，没有建库快照、
没有建库命令、没有版本记录。

**可以确定（由库内数据直接读出）**

1. 库内实际生效的实体名称与正则：`entities` 表。可据此判断某实体在该库中是否存在、
   模式是什么。例如 `staining` 存在、`stain` 不存在。
2. 库内实际关联的配置名称与路径模板：`configs` 与 `config_to_entity_map`。
3. 每条文件记录实际拥有的标签：`files` + `tags`。对任一文件可列出它在旧库中的实体值。
4. 库内的建库根绝对路径与 `derivatives`/`config` 参数：`layout_info`。
5. 在上述基础上，旧库对某查询的实际命中集合（在旧绝对路径可访问的前提下）。
6. 当前根描述“现在是什么”，以及它是否与库内规则自洽（例如库内无 `stain`，
   则当前描述即使写 `BIDSVersion 1.11.2` 也不能证明库按新 schema 建）。
7. 若库内规则与当前 JSON 的解析结果**逐实体完全一致**，可确定“当前 JSON 能复现库内规则”；
   但这仍不等于“建库时用的就是这份 JSON”（内容可能被改回）。

**必须留待核对（旧交付本身无法证明）**

1. 建库时的 `dataset_description.json` 内容（`BIDSVersion`、`DatasetType`、`GeneratedBy`、
   `PipelineDescription`）——库里没有。
2. 建库时的配置文件名/路径对应的字节；`layout_info._config` 只是路径字符串，
   当前路径内容可能已被替换。
3. `config` 是 `bids`/`bids-schema` 名称时，具体解析到哪个 schema 版本——
   取决于当时的 `bidsschematools`，库里没有。
4. `validate`、`ignore`、`force_index`、`index_metadata`、`config_filename` 等 indexer 参数——
   库里没有；它们直接决定哪些文件被收录。
5. 每个派生根建库时的显示名与来源；`source_pipeline` 是重开时现算的，不是建库值。
6. 建库时的 PyBIDS/依赖版本；同一份 JSON 在不同版本下解析结果可能不同。
7. 文件内容是否被改过；库里只有路径，没有哈希/mtime。
8. 旧库是否曾经被 `reset_database` 或重新 `save` 覆盖；仅凭单份 DB 无法判断历史。
9. 目录内 `layout_config.json` 是否在建库时存在、内容是什么、优先级如何——不入库。
10. 无描述目录当时是否被有意排除；库中“没有”既可能是规则排除，也可能是目录当时不存在。

**判定模板**

- 能说：“在该库中，实体 X 的规则是 R，文件 F 的标签是 V，查询 Q 命中 H。”
- 不能说：“该库是按当前目录的描述/配置建立的。”
- 只有同时持有第 3 节 A/B 类证据并能复算出相同 `rule_sha256`，才能说：
  “该库的规则与建库快照一致。”

## 7. 可跑的小型复核流程

前置：本仓库根目录，已安装 `.venv`。不需要新工具，不改源码，不触碰交付库原件。

```bash
# 0) 一键复现（会重建 handoff/.work 下的样例与三个 JSON 报告）
./handoff/verify.sh
```

分步等价命令：

```bash
# 1) 构造：归档根 + 建库快照 + 旧库 + 另路径新库
.venv/bin/python handoff/build_fixture.py --out handoff/.work --reset

# 2) 只读审计：库内规则、重开、传参、同路径漂移、同命中不同规则
.venv/bin/python handoff/audit_handoff.py \
  --work handoff/.work \
  --report handoff/EVIDENCE.json \
  --manifest handoff/BINDING_MANIFEST.json

# 3) 搬迁/缺件探针：只操作 .work 内副本
.venv/bin/python handoff/relocation_probe.py \
  --work handoff/.work --report handoff/RELOCATION.json
```

### 7.1 接到真实旧库时的最小操作（只读）

把交付库复制到临时目录（不要在原目录操作），然后：

```python
from pathlib import Path
import json, sqlite3
from bids.layout import BIDSLayout

db = Path("/scratch/delivery_db_copy")          # 复制品，不是原件
root = Path("/scratch/delivery_root_copy")      # 当前来件复制品

# 1) 库内规则，不经过 ORM、不看当前 JSON
#    先复制交付库再打开：不要让审计过程写入原件
con = sqlite3.connect(f"file:{db/'layout_index.sqlite'}?mode=ro", uri=True)
con.row_factory = sqlite3.Row
print(dict(con.execute("select * from layout_info").fetchone()))
print([dict(r) for r in con.execute("select name, pattern from entities order by name")])
print([dict(r) for r in con.execute(
    "select file_path, entity_name, _value from tags "
    "where file_path like '%_stain-%' order by file_path, entity_name")])
con.close()

# 2) 重开旧库：记录它报告的 root/config 和实际实体，而不是传入值
layout = BIDSLayout(database_path=db, derivatives=True)
print(layout.root, sorted(layout.config), sorted(layout.get_entities(scope="self")))
for key, child in layout.derivatives.items():
    print(key, child.root, child.source_pipeline, sorted(child.config))

# 3) 在另一个全新路径，用当前文件与当前配置真实重建，作对照
fresh = BIDSLayout(root, database_path=Path("/scratch/fresh_db"),
                   derivatives=True, validate=False,
                   config=[root/"configs"/"incoming_entities.json"])
# 4) 对同一相对路径逐实体比较；再对同一查询比较命中
```

### 7.2 验收判据

1. 同一相对路径在旧库与新库的 `get_entities()` 是否逐键相等？
   不等时，记录差集（本样例：`staining` vs `stain`+`description`）。
2. 同一查询的命中是否相等？若相等，再比 `rule_sha256`/`entities`；
   只有规则也相等，才能写“规则一致”。
3. 旧库重开时报告的 `root`/`config` 是否等于传入值？若传入值不同而报告值不变，
   说明传参被库覆盖；此时不能把传入值当作建库事实。
4. 每个子库是否能用 `(父根, 目录名)` 唯一绑定？显示名是否多义或漂移？
5. 当前根/子描述与建库快照的 SHA-256 是否相等？不等时，所有“当前描述 → 旧库规则”
   的推断都必须撤销。
6. 审计前后交付副本的 SHA-256 是否不变？（`databases_unchanged_by_audit`）

### 7.3 已知边界

- 本样例用 `validate=False` 以便把无描述目录纳入对照；真实 raw 数据若用
  `validate=True`，收录集合还会受 BIDS validator 影响，需单独记录。`RELOCATION.json`
  已给出同一当前根在 `validate=True` 下的对照清单。
- `derivatives=False` 在重开旧库时被库内 `_derivatives` 覆盖，属于 `layout.py:154-160`
  的版本行为；接手方不能靠传参关闭子库。
- 本方案不判断科学正确性，只判断“规则/描述/索引三者的版本绑定是否可证”。
- 根描述中的 `BIDSVersion` 只是自述，不参与 `Config.load` 的规则选择
  （`src/bids/config.py` 只有名称/路径映射，`layout.py` 不读 `BIDSVersion` 选配置）。
  因此“根自述 1.11.2”不能推出“库用了 schema 1.11.2”；本样例正是如此。

## 8. 交接清单（可直接勾选）

- [ ] 根描述建库字节 + SHA-256
- [ ] 每个 config 参数路径/名称的建库字节或解析结果 + SHA-256
- [ ] 每个派生根描述建库字节 + SHA-256
- [ ] 建库文件清单（路径 + SHA-256 + 大小），含 `layout_config.json`
- [ ] 建库命令与 indexer 参数
- [ ] PyBIDS / bidsschematools / Python / SQLAlchemy 版本
- [ ] 每个 `layout_index.sqlite` 的 SHA-256
- [ ] 每个库的 `layout_info` + `entities` + `configs` 导出（或 `rule_sha256`）
- [ ] 父库→子库目录清单与 `BINDING_MANIFEST.json`
- [ ] 接手方只读挂载路径；若不同，附路径重写方案与验收用例
- [ ] 明确声明：不得用 `reset_database=True` 打开交付库做“验证”
