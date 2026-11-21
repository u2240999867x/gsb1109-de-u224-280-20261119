"""同一合法区域、Trino 明确超时轮次：REST 回退判定（离线运行）。"""

import httpx

from common import (
    NOW_EPOCH,
    RecordedREST,
    ScriptedTrino,
    TAIL,
    render_round,
    run_position_round,
    state_payload,
)

ROWS = [
    # dd4012：两轮之间唯一被修改的一行（位置时刻与坐标）
    ["dd4012", "NEWCTC", "FR", None, NOW_EPOCH - 5, None, None] + TAIL,
    # dd4013：位置时刻新鲜，但经纬度为空
    ["dd4013", "NOGEO", "FR", NOW_EPOCH - 10, NOW_EPOCH - 8, None, None]
    + TAIL,
    # dd4014：坐标有效，但位置时刻过旧
    ["dd4014", "OLDPOS", "FR", NOW_EPOCH - 3600, NOW_EPOCH - 6, 2.50, 49.05]
    + TAIL,
]


def section(title: str = "Trino 超时轮次") -> str:
    rest = RecordedREST(state_payload(NOW_EPOCH - 3, ROWS))
    trino = ScriptedTrino(httpx.ReadTimeout("Trino query timed out"))
    result = run_position_round(trino, rest)
    return render_round(title, result)


if __name__ == "__main__":
    print(section())
