"""同一合法区域、Trino 真实空表轮次：权威空答，不回退（离线运行）。"""

from common import (
    NOW_EPOCH,
    POSITION_COLUMNS,
    RecordedREST,
    ScriptedTrino,
    TAIL,
    render_round,
    run_position_round,
    state_payload,
)

import pandas as pd

# REST 替身装填一个完好的可交付点；按约定它不应被调用
ROWS = [
    ["39ac44", "AFR123", "FR", NOW_EPOCH - 10, NOW_EPOCH - 5, 2.55, 49.01]
    + TAIL,
]


def section(title: str = "Trino 真实空表轮次") -> str:
    rest = RecordedREST(state_payload(NOW_EPOCH - 3, ROWS))
    trino = ScriptedTrino(pd.DataFrame(columns=list(POSITION_COLUMNS)))
    result = run_position_round(trino, rest)
    return render_round(title, result)


if __name__ == "__main__":
    print(section())
