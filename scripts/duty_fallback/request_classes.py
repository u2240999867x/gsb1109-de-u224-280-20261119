"""三类请求的过滤与字段对照（离线运行）。

三类请求共用 common.py 中同一个冻结判断时钟 NOW：
- 类别 A：当前地理窗口的最近有效位置；
- 类别 B：指定历史秒且指定接收站；
- 类别 C：只要最近接触状态。
每类记录两个入口原样发出的过滤（REST URL / Trino SQL）与实际返回字段。
"""

import inspect

from common import (
    CONTACT_COLUMNS,
    HISTORICAL_SECOND,
    NOW,
    NOW_EPOCH,
    POSITION_COLUMNS,
    RECEIVER_SERIAL,
    REGION,
    TAIL,
    RecordedREST,
    ScriptedTrino,
    Verdict,
    fmt_epoch,
    judge_contact_row,
    judge_position_row,
    position_history,
    state_payload,
)

import pandas as pd
from pyopensky.rest import REST


def trino_sample_df(columns: tuple[str, ...]) -> pd.DataFrame:
    full = {
        "time": [NOW - pd.Timedelta("30s")],
        "icao24": ["39ac44"],
        "callsign": ["AFR123"],
        "lat": [49.01],
        "lon": [2.55],
        "lastcontact": [float(NOW_EPOCH - 20)],
        "lastposupdate": [float(NOW_EPOCH - 30)],
    }
    return pd.DataFrame({c: full[c] for c in columns})


def render_trino(trino: ScriptedTrino, df: pd.DataFrame | None) -> list[str]:
    lines = ["**Trino 原样发出的 history 查询：**", "", "```sql"]
    lines += trino.sql
    lines += ["```", ""]
    fields = [] if df is None else [str(c) for c in df.columns]
    lines.append(f"**Trino 返回字段：** `{fields}`")
    lines.append("")
    return lines


def render_rest_meta(rest: RecordedREST, df: pd.DataFrame) -> list[str]:
    lines = ["**REST 原样发出的 URL：**", ""]
    lines += [f"- `{url}`" for url in rest.urls]
    lines += [
        "",
        f"**REST 返回字段：** `{[str(c) for c in df.columns]}`",
        "",
        "**REST 根级 time：** "
        f"{fmt_epoch(int(rest.payload['time']))}"
        "（`REST.states()` 的返回不含该字段；"
        "此观测来自 RecordedREST 传输层记录的原始 JSON）",
        "",
    ]
    return lines


def class_a() -> str:
    lines = [
        "### 类别 A：当前地理窗口的最近有效位置",
        "",
        f"窗口：{REGION}（west, south, east, north）；"
        "判断时钟：冻结的 NOW。",
        "",
    ]
    trino = ScriptedTrino(trino_sample_df(POSITION_COLUMNS))
    trino_df = position_history(trino)
    lines += render_trino(trino, trino_df)
    rows = [
        # 根级 time、last_contact、time_position 分别落在不同秒
        ["39ac44", "AFR123", "FR", NOW_EPOCH - 30, NOW_EPOCH - 20, 2.55, 49.01]
        + TAIL,
        ["4b1a3c", "BAW456", "GB", NOW_EPOCH - 45, NOW_EPOCH - 25, 2.62, 48.95]
        + TAIL,
    ]
    rest = RecordedREST(state_payload(NOW_EPOCH - 15, rows))
    rest_df = rest.states(bounds=REGION)
    lines += render_rest_meta(rest, rest_df)
    lines.append(
        "REST 单行三个时刻：根级 time=NOW-15s、last_contact=NOW-20s、"
        "time_position=NOW-30s，分别落在不同秒。"
    )
    lines.append("")
    lines.append("**REST 行级位置判定（冻结时钟 NOW）：**")
    lines.append("")
    lines.append("| icao24 | 判定 |")
    lines.append("| --- | --- |")
    for _, row in rest_df.iterrows():
        lines.append(f"| {row['icao24']} | {judge_position_row(row).value} |")
    lines.append("")
    lines.append("Trino 权威有数据，本类别无需回退；REST 判定仅作对照。")
    lines.append("")
    return "\n".join(lines)


def class_b() -> str:
    lines = [
        "### 类别 B：指定历史秒且指定接收站",
        "",
        f"历史秒：{HISTORICAL_SECOND}；接收站 serial：{RECEIVER_SERIAL}。",
        "",
    ]
    trino = ScriptedTrino(trino_sample_df(POSITION_COLUMNS))
    trino_df = trino.history(
        HISTORICAL_SECOND,
        HISTORICAL_SECOND,
        serials=RECEIVER_SERIAL,
        selected_columns=POSITION_COLUMNS,
        cached=False,
    )
    lines += render_trino(trino, trino_df)
    signature = inspect.signature(REST.states)
    lines += [
        "**REST 入口可表达性：**",
        "",
        f"- `REST.states` 签名为 `{signature}`："
        "没有历史秒参数，也没有接收站参数；",
        "- 唯一可发出的 URL 是 "
        "`https://opensky-network.org/api/states/all`（无任何过滤）；",
        "- 返回字段中虽有 `sensors`（接收站列表），但只能在响应之后"
        "事后过滤，无法作为请求过滤发出；",
        "- 历史秒更不可达：pyopensky 的 `states()` 未暴露 `time` 参数，"
        "且 OpenSky API 的 `time` 参数本身只允许最近 1 小时内"
        "（指定秒距 NOW 已 2 小时 15 分）。",
        "",
        f"**判定：{Verdict.NOT_EXPRESSIBLE.value}**",
        "",
    ]
    return "\n".join(lines)


def class_c() -> str:
    lines = [
        "### 类别 C：只要最近接触状态",
        "",
        f"窗口：{REGION}；判定只依据 last_contact，不要求位置。",
        "",
    ]
    trino = ScriptedTrino(trino_sample_df(CONTACT_COLUMNS))
    trino_df = trino.history(
        NOW - pd.Timedelta("1h"),
        NOW,
        bounds=REGION,
        selected_columns=CONTACT_COLUMNS,
        cached=False,
    )
    lines += render_trino(trino, trino_df)
    rows = [
        ["39ac44", "AFR123", "FR", None, NOW_EPOCH - 20, None, None] + TAIL,
    ]
    rest = RecordedREST(state_payload(NOW_EPOCH - 15, rows))
    rest_df = rest.states(bounds=REGION)
    lines += render_rest_meta(rest, rest_df)
    lines.append("**REST 行级接触判定（冻结时钟 NOW）：**")
    lines.append("")
    lines.append("| icao24 | 判定 |")
    lines.append("| --- | --- |")
    for _, row in rest_df.iterrows():
        lines.append(f"| {row['icao24']} | {judge_contact_row(row).value} |")
    lines.append("")
    lines.append(
        "本类别只需最近接触：REST 的 `timestamp`（last_contact）字段"
        "可表达该需求；位置字段（time_position、经纬度）不参与判定。"
    )
    lines.append("")
    return "\n".join(lines)


def section() -> str:
    parts = [
        "## 三类请求的过滤与字段对照",
        "",
        "三类请求共用同一个冻结判断时钟 "
        f"NOW={NOW}（epoch {NOW_EPOCH}）。",
        "",
        class_a(),
        class_b(),
        class_c(),
    ]
    return "\n".join(parts)


if __name__ == "__main__":
    print(section())
