"""离线对照的共享基座：冻结时钟、夹具、判定逻辑与离线替身。

对照对象：Trino ``history``（权威入口）与 OpenSky REST ``states``
（回退替身）。所有脚本离线运行：REST 走 ``httpx.MockTransport``
记录真实发出的 URL；Trino 走脚本化 ``query()``，记录真实构造的 SQL。
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any

import httpx
from sqlalchemy import Select, TextClause
from trino.sqlalchemy.dialect import TrinoDialect

import pandas as pd
from pyopensky.rest import REST
from pyopensky.trino import Trino

# --- 冻结的判断时钟：三类请求与三轮对照共用同一时刻 ---
NOW = pd.Timestamp("2026-11-22 12:00:00", tz="utc")
NOW_EPOCH = int(NOW.timestamp())

# --- 同一个合法区域 (west, south, east, north)，LFPG 周边窗口 ---
REGION = (2.20, 48.80, 2.80, 49.20)

# --- 值班新鲜度阈值 ---
POSITION_MAX_AGE = pd.Timedelta(seconds=120)
CONTACT_MAX_AGE = pd.Timedelta(seconds=300)

# --- 类别 B：指定历史秒与指定接收站 ---
HISTORICAL_SECOND = pd.Timestamp("2026-11-22 09:45:00", tz="utc")
RECEIVER_SERIAL = 140020160

POSITION_COLUMNS = (
    "time",
    "icao24",
    "callsign",
    "lat",
    "lon",
    "lastcontact",
    "lastposupdate",
)
CONTACT_COLUMNS = ("time", "icao24", "callsign", "lastcontact")

# OpenSky state vector 第 8..17 字段，与本对照无关，取常量
TAIL: list[Any] = [
    11250.0,
    False,
    230.0,
    92.0,
    0.0,
    None,
    11500.0,
    "1000",
    False,
    0,
]


class Verdict(enum.Enum):
    """报告需要区分的五类判定。"""

    AUTHORITATIVE_EMPTY = "权威查询确认为空"
    FALLBACK_DELIVERED = "回退交付"
    FALLBACK_NO_DELIVERABLE = "允许回退后无可交付行"
    INSUFFICIENT_FRESHNESS = "新鲜度证据不足"
    NOT_EXPRESSIBLE = "过滤或字段不可表达，不能回退"


class RowVerdict(enum.Enum):
    """单行 REST state vector 的判定。"""

    DELIVERABLE = "可交付"
    EMPTY_COORDINATES = "空坐标"
    EXPIRED_POSITION = "过期位置"
    MISSING_POSITION_TIME = "缺位置时刻（新鲜度证据不足）"
    MISSING_CONTACT = "缺接触时刻（新鲜度证据不足）"
    EXPIRED_CONTACT = "过期接触"


_UNDECIDABLE = {
    RowVerdict.MISSING_POSITION_TIME,
    RowVerdict.MISSING_CONTACT,
}


def judge_position_row(row: pd.Series) -> RowVerdict:
    """以冻结时钟判定一行是否可交付位置。"""
    if pd.isna(row["latitude"]) or pd.isna(row["longitude"]):
        return RowVerdict.EMPTY_COORDINATES
    if pd.isna(row["last_position"]):
        return RowVerdict.MISSING_POSITION_TIME
    if NOW - row["last_position"] > POSITION_MAX_AGE:
        return RowVerdict.EXPIRED_POSITION
    return RowVerdict.DELIVERABLE


def judge_contact_row(row: pd.Series) -> RowVerdict:
    """以冻结时钟判定一行是否可交付最近接触状态。"""
    if pd.isna(row["timestamp"]):
        return RowVerdict.MISSING_CONTACT
    if NOW - row["timestamp"] > CONTACT_MAX_AGE:
        return RowVerdict.EXPIRED_CONTACT
    return RowVerdict.DELIVERABLE


def aggregate(row_verdicts: list[RowVerdict]) -> Verdict:
    """行级判定汇总为轮次级判定。"""
    if any(v is RowVerdict.DELIVERABLE for v in row_verdicts):
        return Verdict.FALLBACK_DELIVERED
    if any(v in _UNDECIDABLE for v in row_verdicts):
        return Verdict.INSUFFICIENT_FRESHNESS
    return Verdict.FALLBACK_NO_DELIVERABLE


def state_payload(root_time: int, rows: list[list[Any]]) -> dict[str, Any]:
    """构造 OpenSky `/api/states/all` 的原始 JSON 响应。"""
    return {"time": root_time, "states": rows}


def fmt_epoch(epoch: int | None) -> str:
    """把 epoch 秒格式化为相对冻结时钟的可读形式。"""
    if epoch is None:
        return "—"
    ts = pd.Timestamp(epoch, unit="s", tz="utc")
    age = (NOW - ts).total_seconds()
    return f"{ts:%Y-%m-%d %H:%M:%S}Z（NOW-{age:.0f}s）"


def fmt_ts(ts: Any) -> str:
    """把已解析的时间戳格式化为相对冻结时钟的可读形式。"""
    if pd.isna(ts):
        return "—"
    age = (NOW - ts).total_seconds()
    return f"{ts:%H:%M:%S}Z（NOW-{age:.0f}s）"


def fmt_coord(value: Any) -> str:
    return "—" if pd.isna(value) else str(value)


class RecordedREST(REST):
    """离线 REST 替身：记录实际发出的 URL，回放装填的 JSON。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        super().__init__()
        # 无凭据：headers 为空，states() 走 states/all，不发认证请求
        self.client_id = None
        self.client_secret = None
        self.payload = payload
        self.urls: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.urls.append(str(request.url))
            return httpx.Response(200, json=self.payload)

        self.client = httpx.Client(transport=httpx.MockTransport(handler))


class ScriptedTrino(Trino):
    """离线 Trino 替身：记录真实构造的 SQL，回放脚本化结果或异常。"""

    def __init__(self, outcome: pd.DataFrame | Exception) -> None:
        super().__init__()
        self.outcome = outcome
        self.sql: list[str] = []

    def query(
        self,
        query: str | TextClause | Select[Any],
        cached: bool = True,
        compress: bool = False,
    ) -> pd.DataFrame:
        assert isinstance(query, Select)
        self.sql.append(
            str(
                query.compile(
                    dialect=TrinoDialect(),
                    compile_kwargs={"literal_binds": True},
                )
            )
        )
        if isinstance(self.outcome, Exception):
            raise self.outcome
        return self.outcome


def position_history(trino: Trino) -> pd.DataFrame | None:
    """类别 A 的 history 构造：当前地理窗口的最近有效位置。"""
    return trino.history(
        NOW - pd.Timedelta("15min"),
        NOW,
        bounds=REGION,
        selected_columns=POSITION_COLUMNS,
        cached=False,
    )


@dataclass
class RoundResult:
    """一轮对照的全部观测。"""

    verdict: Verdict
    trino_sql: list[str]
    trino_outcome: str
    rest_urls: list[str]
    rest_root_time: int | None = None
    rest_fields: list[str] = field(default_factory=list)
    rest_df: pd.DataFrame | None = None
    row_verdicts: list[tuple[str, RowVerdict]] = field(default_factory=list)
    delivered: pd.DataFrame | None = None
    armed_rows: list[list[Any]] = field(default_factory=list)


def judge_raw_armed(row: list[Any]) -> str:
    """对装填但未调用的 REST 原始行做事后说明（不触发请求）。"""
    time_position, lon, lat = row[3], row[5], row[6]
    if lat is None or lon is None:
        return "空坐标"
    if time_position is None:
        return "缺位置时刻"
    age = NOW_EPOCH - int(time_position)
    if age > POSITION_MAX_AGE.total_seconds():
        return f"过期位置（龄期 {age:.0f}s）"
    return f"可交付（位置龄期 {age:.0f}s）"


def run_position_round(
    trino: ScriptedTrino, rest: RecordedREST
) -> RoundResult:
    """执行一轮位置对照，应用系统约定的回退策略。

    约定：Trino 成功空表为有效答复，不再访问 REST；
    仅当 Trino 失败（如超时）才允许回退到 REST。
    """
    try:
        df = position_history(trino)
    except httpx.TimeoutException as exc:
        rest_df = rest.states(bounds=REGION)
        row_verdicts = [
            (str(row["icao24"]), judge_position_row(row))
            for _, row in rest_df.iterrows()
        ]
        verdict = aggregate([v for _, v in row_verdicts])
        mask = [v is RowVerdict.DELIVERABLE for _, v in row_verdicts]
        return RoundResult(
            verdict=verdict,
            trino_sql=list(trino.sql),
            trino_outcome=(
                f"明确超时（{type(exc).__name__}）→ 允许回退"
            ),
            rest_urls=list(rest.urls),
            rest_root_time=int(rest.payload["time"]),
            rest_fields=[str(c) for c in rest_df.columns],
            rest_df=rest_df,
            row_verdicts=row_verdicts,
            delivered=(
                rest_df[mask] if verdict is Verdict.FALLBACK_DELIVERED else None
            ),
            armed_rows=rest.payload["states"],
        )
    assert df is None, "本轮次脚本只覆盖空表或超时两种 Trino 结果"
    assert not rest.urls, "约定：Trino 成功空表后不得访问 REST"
    return RoundResult(
        verdict=Verdict.AUTHORITATIVE_EMPTY,
        trino_sql=list(trino.sql),
        trino_outcome="成功返回真实空表（history() 返回 None）→ 有效答复",
        rest_urls=list(rest.urls),
        armed_rows=rest.payload["states"],
    )


def render_round(title: str, result: RoundResult) -> str:
    """把一轮观测渲染为报告小节。"""
    lines = [f"### {title}", ""]
    lines += ["**Trino 原样发出的 history 查询：**", "", "```sql"]
    lines += result.trino_sql
    lines += ["```", ""]
    lines.append(f"**Trino 结果：** {result.trino_outcome}")
    lines.append("")
    if not result.rest_urls:
        lines.append(
            "**REST：** 未调用（约定：Trino 成功空表即有效答复；"
            "已断言 `rest.urls` 为空）。"
        )
        lines.append("")
        for row in result.armed_rows:
            lines.append(
                f"- 替身已装填 `{row[0]}`：若被误调用，"
                f"该行判定为{judge_raw_armed(row)}"
            )
        lines.append("")
    else:
        lines.append("**REST 原样发出的 URL：**")
        lines.append("")
        lines += [f"- `{url}`" for url in result.rest_urls]
        lines.append("")
        lines.append(f"**REST 返回字段：** `{result.rest_fields}`")
        lines.append("")
        lines.append(
            "**REST 根级 time：** "
            f"{fmt_epoch(result.rest_root_time)}"
            "（`REST.states()` 的返回不含该字段；"
            "此观测来自 RecordedREST 传输层记录的原始 JSON）"
        )
        lines.append("")
        lines.append("**行级判定（冻结时钟 NOW）：**")
        lines.append("")
        header = "| icao24 | last_contact | time_position | lat | lon | 判定 |"
        lines.append(header)
        lines.append("| --- | --- | --- | --- | --- | --- |")
        assert result.rest_df is not None
        details = {
            str(row["icao24"]): row for _, row in result.rest_df.iterrows()
        }
        for icao24, row_verdict in result.row_verdicts:
            row = details[icao24]
            lines.append(
                f"| {icao24} | {fmt_ts(row['timestamp'])} "
                f"| {fmt_ts(row['last_position'])} "
                f"| {fmt_coord(row['latitude'])} "
                f"| {fmt_coord(row['longitude'])} "
                f"| {row_verdict.value} |"
            )
        lines.append("")
        if result.delivered is not None:
            lines.append("**交付清单（值班位置）：**")
            lines.append("")
            lines.append("| icao24 | callsign | lat | lon | last_position |")
            lines.append("| --- | --- | --- | --- | --- |")
            for _, row in result.delivered.iterrows():
                lines.append(
                    f"| {row['icao24']} | {row['callsign']} "
                    f"| {row['latitude']} | {row['longitude']} "
                    f"| {row['last_position']} |"
                )
            lines.append("")
    lines.append(f"**判定：{result.verdict.value}**")
    lines.append("")
    return "\n".join(lines)
