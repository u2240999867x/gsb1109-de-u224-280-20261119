"""汇总运行全部对照并生成 REPORT.md（离线运行）。

用法：uv run python scripts/duty_fallback/run_all.py
"""

import difflib
from pathlib import Path

import counterexamples
import request_classes
import round1_trino_empty
import round2_trino_timeout
import round3_trino_timeout_delivered
from common import (
    CONTACT_MAX_AGE,
    NOW,
    NOW_EPOCH,
    POSITION_MAX_AGE,
    REGION,
    Verdict,
)

HERE = Path(__file__).parent


def intro() -> str:
    verdicts = "\n".join(f"- {v.value}" for v in Verdict)
    return f"""\
# Trino history 卡住时 REST states 能否替身：离线对照

## 判定约定

- 冻结判断时钟：NOW={NOW}（epoch {NOW_EPOCH}），
  三类请求与三轮对照共用同一时刻；
- 合法区域：{REGION}（west, south, east, north），三轮对照使用同一区域；
- 新鲜度阈值：位置 {POSITION_MAX_AGE.total_seconds():.0f}s，
  接触 {CONTACT_MAX_AGE.total_seconds():.0f}s；
- 回退策略：Trino 成功空表为有效答复，不再访问 REST；
  仅当 Trino 失败（如明确超时）才允许回退到 REST；
- 报告区分五类判定：
{verdicts}
- REST 根级 time 不由 `REST.states()` 返回；
  本对照中该观测全部来自 RecordedREST 传输层记录的原始 JSON；
- 全部离线运行：REST 走 httpx.MockTransport，Trino 走脚本化 query()，
  两个入口的过滤按原样记录（REST URL 与 Trino SQL 均为真实构造）。

运行方式：`uv run --no-sync python scripts/duty_fallback/run_all.py`
（或 `.venv/bin/python scripts/duty_fallback/run_all.py`；
各小节脚本也可单独运行，全程离线）。
"""


def diff_section() -> str:
    before = (HERE / "round2_trino_timeout.py").read_text().splitlines()
    after = (
        (HERE / "round3_trino_timeout_delivered.py")
        .read_text()
        .splitlines()
    )
    diff = list(
        difflib.unified_diff(
            before,
            after,
            fromfile="round2_trino_timeout.py",
            tofile="round3_trino_timeout_delivered.py",
            lineterm="",
        )
    )
    changed = [
        line
        for line in diff
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---"))
    ]
    assert len(changed) == 2, f"第三轮应只改一行，实际 {changed}"
    body = "\n".join(diff)
    return f"""\
## 第二轮与第三轮的脚本差异

已断言两个脚本仅相差一行（位置时刻与坐标）：

```diff
{body}
```
"""


def main() -> None:
    sections = [
        intro(),
        request_classes.section(),
        counterexamples.section(),
        "## 三轮空结果对照（同一合法区域）",
        "",
        round1_trino_empty.section("第一轮：Trino 真实空表"),
        round2_trino_timeout.section(
            "第二轮：Trino 明确超时，REST 只有新接触、空坐标或过期位置"
        ),
        round3_trino_timeout_delivered.section(
            "第三轮：只改一行的位置时刻与坐标后，REST 可交付"
        ),
        diff_section(),
    ]
    report = HERE / "REPORT.md"
    report.write_text("\n".join(sections))
    print(f"已生成 {report}")


if __name__ == "__main__":
    main()
