"""新鲜度反例：根级时刻不能替代行级时刻（离线运行）。

REST 响应的根级 time、单行 last_contact、time_position 分别落在不同秒。
判断时钟为 common.py 中冻结的 NOW，与三类请求、三轮对照共用。
"""

from common import (
    NOW_EPOCH,
    REGION,
    TAIL,
    RecordedREST,
    aggregate,
    fmt_epoch,
    judge_contact_row,
    judge_position_row,
    state_payload,
)


def evaluate(title: str, payload: dict, mode: str) -> str:
    rest = RecordedREST(payload)
    df = rest.states(bounds=REGION)
    judge = judge_position_row if mode == "position" else judge_contact_row
    row_verdicts = [
        (str(row["icao24"]), judge(row)) for _, row in df.iterrows()
    ]
    verdict = aggregate([v for _, v in row_verdicts])
    row = payload["states"][0]
    lines = [
        f"### {title}",
        "",
        "原始 JSON 三个时刻（不同秒）："
        f"根级 time={fmt_epoch(payload['time'])}；"
        f"last_contact={fmt_epoch(row[4])}；"
        f"time_position={fmt_epoch(row[3])}。",
        "",
        f"REST 发出的 URL：`{rest.urls[0]}`",
        "",
        "根级 time 不由 `REST.states()` 返回；"
        "以上根级观测来自 RecordedREST 传输层记录的原始 JSON。",
        "",
        "| icao24 | 判定 |",
        "| --- | --- |",
    ]
    for icao24, row_verdict in row_verdicts:
        lines.append(f"| {icao24} | {row_verdict.value} |")
    lines += ["", f"**判定：{verdict.value}**", ""]
    return "\n".join(lines)


def section() -> str:
    fresh_root_stale_position = state_payload(
        NOW_EPOCH - 1,
        [
            ["dd5001", "STALE1", "FR"]
            + [NOW_EPOCH - 1200, NOW_EPOCH - 2, 2.55, 49.01]
            + TAIL
        ],
    )
    stale_root_fresh_contact = state_payload(
        NOW_EPOCH - 1800,
        [
            ["dd5002", "FRESHC", "FR"]
            + [NOW_EPOCH - 1500, NOW_EPOCH - 2, 2.55, 49.01]
            + TAIL
        ],
    )
    missing_position_time = state_payload(
        NOW_EPOCH - 1,
        [
            ["dd5003", "NOTIME", "FR"]
            + [None, NOW_EPOCH - 2, 2.55, 49.01]
            + TAIL
        ],
    )
    parts = [
        "## 新鲜度反例",
        "",
        "根级 time 只是响应封套时刻，既不是位置时刻也不是接触时刻；"
        "以下反例固定同一冻结时钟 NOW。",
        "",
        evaluate(
            "反例一：新鲜根级时刻 + 陈旧位置（按类别 A 判定）",
            fresh_root_stale_position,
            "position",
        ),
        "新鲜的根级 time 不能作为位置新鲜度证据；"
        "判定只认行级 time_position，该行位置已过期。",
        "",
        evaluate(
            "反例二：陈旧根级时刻 + 最近接触（按类别 C 判定）",
            stale_root_fresh_contact,
            "contact",
        ),
        "陈旧的根级 time 不能否定行级 last_contact；"
        "只要最近接触的请求仍可交付。",
        "",
        evaluate(
            "缺失位置时刻：坐标有效但 time_position 为空（按类别 A 判定）",
            missing_position_time,
            "position",
        ),
        "坐标有效但没有任何位置时刻，无法证明新鲜也无法证明过期，"
        "只能判为新鲜度证据不足，不得交付。",
        "",
    ]
    return "\n".join(parts)


if __name__ == "__main__":
    print(section())
