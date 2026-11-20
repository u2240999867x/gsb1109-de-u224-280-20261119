# 每日到仓 Frictionless 预检

在**不改动已保存合同 Schema** 的前提下，用 Frictionless v5 的字段级 reader 单遍扫描
当天到仓 CSV，判断实际数据能否按旧合同进库，并把超出合同的原始输入原样留存。

## 目录

- `contract/inbound-contract.schema.json` — 已保存的旧合同（只读，扫描前后描述符不变）
- `data/batch-arrival-2026-11-21.csv` — 完整批次（8 行）
- `data/batch-arrival-2026-11-21.head-only.csv` — 仅收到相同开头（前 4 行），后段未到
- `src/intake_precheck/precheck.py` — 单遍流式预检、列画像、候选推断
- `src/intake_precheck/report.py` — Markdown / JSON 报告渲染
- `run_precheck.py` — 入口：全量、不同抽样边界、延迟开头三种场景
- `tests/` — 15 个 pytest 用例
- `reports/` — 生成的报告；`reports/evidence/` 保存被拒原始记录

## 合同要点（保存后不再修改）

| 字段 | 合同声明 |
| --- | --- |
| `arrival_date` | `date`（`%Y-%m-%d`），`required: true`；带时区的时刻无法 cast 成日期；空白按缺失拒绝 |
| `note` | `string`，`missingValues: ["", "N/A"]`，空串与缺词都合法为 null |
| `quantity` | `number`，`constraints.maximum = 1000`；超界值类型仍是 number，只违反上界 |
| `remark` | `string`，本批始终为空 |
| `tail_note` | `string`，开头纯空、后段才出现实际值 |

## 运行

```bash
../.venv/bin/python run_precheck.py
../.venv/bin/python -m pytest tests/ -q
```

## 设计约束如何落实

- **单遍输入、不回源重读**：`scan_resource` 打开一次 `TableResource.cell_stream`，
  字段 reader 由保存的 Schema 预建并复用；同一完整记录的合同结论只取决于该记录
  与合同，不随抽样大小改变（`test_sampling_boundaries_keep_shared_record_verdicts_stable`）。
- **三类拒绝原因分开**：Frictionless 的 note key 映射为 `type`（cast 失败）、
  `missing`（required 缺失）、`constraint`（maximum 等约束），报告给出最早行号与
  原始单元格；证据同时写 CSV（整行原始值）和 JSONL（逐格原因 + 原始行）。
- **纯空不等于换类型**：空白单元格匹配 Schema/字段的 missing values，记为 blank，
  不产生任何拒绝；全空列（`remark`）与“空片段在前”的列（`tail_note`）都不会被
  说成类型变化。
- **候选推断只是线索**：`Detector.detect_schema` 的输出放在报告独立分区并标注
  hint-only；全空列推断器只能给 `any` 兜底，报告显式警告这不是类型变化证据；
  候选描述符从不参与任何接受/拒绝判断。
- **部分扫描必须写明确认范围**：`partial` 报告标注 `confirmed_range`（如
  “data rows 1..4”）并声明开头兼容不等同整批兼容；物理上只到开头的文件通过
  `expected_rows` 同样标记为 partial。
- **合同描述符保持不变**：扫描只调用 Schema 的读取接口，测试断言扫描前后
  `schema.to_descriptor()` 完全一致。
