# Trino history 卡住时 REST states 能否替身：离线对照

## 判定约定

- 冻结判断时钟：NOW=2026-11-22 12:00:00+00:00（epoch 1795348800），
  三类请求与三轮对照共用同一时刻；
- 合法区域：(2.2, 48.8, 2.8, 49.2)（west, south, east, north），三轮对照使用同一区域；
- 新鲜度阈值：位置 120s，
  接触 300s；
- 回退策略：Trino 成功空表为有效答复，不再访问 REST；
  仅当 Trino 失败（如明确超时）才允许回退到 REST；
- 报告区分五类判定：
- 权威查询确认为空
- 回退交付
- 允许回退后无可交付行
- 新鲜度证据不足
- 过滤或字段不可表达，不能回退
- REST 根级 time 不由 `REST.states()` 返回；
  本对照中该观测全部来自 RecordedREST 传输层记录的原始 JSON；
- 全部离线运行：REST 走 httpx.MockTransport，Trino 走脚本化 query()，
  两个入口的过滤按原样记录（REST URL 与 Trino SQL 均为真实构造）。

运行方式：`uv run --no-sync python scripts/duty_fallback/run_all.py`
（或 `.venv/bin/python scripts/duty_fallback/run_all.py`；
各小节脚本也可单独运行，全程离线）。

## 三类请求的过滤与字段对照

三类请求共用同一个冻结判断时钟 NOW=2026-11-22 12:00:00+00:00（epoch 1795348800）。

### 类别 A：当前地理窗口的最近有效位置

窗口：(2.2, 48.8, 2.8, 49.2)（west, south, east, north）；判断时钟：冻结的 NOW。

**Trino 原样发出的 history 查询：**

```sql
SELECT state_vectors_data4.time, state_vectors_data4.icao24, state_vectors_data4.callsign, state_vectors_data4.lat, state_vectors_data4.lon, state_vectors_data4.lastcontact, state_vectors_data4.lastposupdate 
FROM state_vectors_data4 
WHERE state_vectors_data4.lon >= 2.2 AND state_vectors_data4.lon <= 2.8 AND state_vectors_data4.lat >= 48.8 AND state_vectors_data4.lat <= 49.2 AND state_vectors_data4.time >= 1795347900 AND state_vectors_data4.time <= 1795348800 AND state_vectors_data4.hour >= 1795345200 AND state_vectors_data4.hour < 1795348800
```

**Trino 返回字段：** `['time', 'icao24', 'callsign', 'lat', 'lon', 'lastcontact', 'lastposupdate']`

**REST 原样发出的 URL：**

- `https://opensky-network.org/api/states/all?lamin=48.8&lamax=49.2&lomin=2.2&lomax=2.8`

**REST 返回字段：** `['icao24', 'callsign', 'origin_country', 'last_position', 'timestamp', 'longitude', 'latitude', 'altitude', 'onground', 'groundspeed', 'track', 'vertical_rate', 'sensors', 'geoaltitude', 'squawk', 'spi', 'position_source']`

**REST 根级 time：** 2026-11-22 11:59:45Z（NOW-15s）（`REST.states()` 的返回不含该字段；此观测来自 RecordedREST 传输层记录的原始 JSON）

REST 单行三个时刻：根级 time=NOW-15s、last_contact=NOW-20s、time_position=NOW-30s，分别落在不同秒。

**REST 行级位置判定（冻结时钟 NOW）：**

| icao24 | 判定 |
| --- | --- |
| 39ac44 | 可交付 |
| 4b1a3c | 可交付 |

Trino 权威有数据，本类别无需回退；REST 判定仅作对照。

### 类别 B：指定历史秒且指定接收站

历史秒：2026-11-22 09:45:00+00:00；接收站 serial：140020160。

**Trino 原样发出的 history 查询：**

```sql
SELECT state_vectors_data4.time, state_vectors_data4.icao24, state_vectors_data4.callsign, state_vectors_data4.lat, state_vectors_data4.lon, state_vectors_data4.lastcontact, state_vectors_data4.lastposupdate 
FROM state_vectors_data4 
WHERE contains(state_vectors_data4.serials, 140020160) AND state_vectors_data4.time >= 1795340700 AND state_vectors_data4.time <= 1795340700 AND state_vectors_data4.hour >= 1795338000 AND state_vectors_data4.hour < 1795341600
```

**Trino 返回字段：** `['time', 'icao24', 'callsign', 'lat', 'lon', 'lastcontact', 'lastposupdate']`

**REST 入口可表达性：**

- `REST.states` 签名为 `(self, own: 'bool' = False, bounds: 'str | HasBounds | tuple[float, float, float, float] | None' = None, retry: 'int' = 5) -> 'pd.DataFrame'`：没有历史秒参数，也没有接收站参数；
- 唯一可发出的 URL 是 `https://opensky-network.org/api/states/all`（无任何过滤）；
- 返回字段中虽有 `sensors`（接收站列表），但只能在响应之后事后过滤，无法作为请求过滤发出；
- 历史秒更不可达：pyopensky 的 `states()` 未暴露 `time` 参数，且 OpenSky API 的 `time` 参数本身只允许最近 1 小时内（指定秒距 NOW 已 2 小时 15 分）。

**判定：过滤或字段不可表达，不能回退**

### 类别 C：只要最近接触状态

窗口：(2.2, 48.8, 2.8, 49.2)；判定只依据 last_contact，不要求位置。

**Trino 原样发出的 history 查询：**

```sql
SELECT state_vectors_data4.time, state_vectors_data4.icao24, state_vectors_data4.callsign, state_vectors_data4.lastcontact 
FROM state_vectors_data4 
WHERE state_vectors_data4.lon >= 2.2 AND state_vectors_data4.lon <= 2.8 AND state_vectors_data4.lat >= 48.8 AND state_vectors_data4.lat <= 49.2 AND state_vectors_data4.time >= 1795345200 AND state_vectors_data4.time <= 1795348800 AND state_vectors_data4.hour >= 1795345200 AND state_vectors_data4.hour < 1795348800
```

**Trino 返回字段：** `['time', 'icao24', 'callsign', 'lastcontact']`

**REST 原样发出的 URL：**

- `https://opensky-network.org/api/states/all?lamin=48.8&lamax=49.2&lomin=2.2&lomax=2.8`

**REST 返回字段：** `['icao24', 'callsign', 'origin_country', 'last_position', 'timestamp', 'longitude', 'latitude', 'altitude', 'onground', 'groundspeed', 'track', 'vertical_rate', 'sensors', 'geoaltitude', 'squawk', 'spi', 'position_source']`

**REST 根级 time：** 2026-11-22 11:59:45Z（NOW-15s）（`REST.states()` 的返回不含该字段；此观测来自 RecordedREST 传输层记录的原始 JSON）

**REST 行级接触判定（冻结时钟 NOW）：**

| icao24 | 判定 |
| --- | --- |
| 39ac44 | 可交付 |

本类别只需最近接触：REST 的 `timestamp`（last_contact）字段可表达该需求；位置字段（time_position、经纬度）不参与判定。

## 新鲜度反例

根级 time 只是响应封套时刻，既不是位置时刻也不是接触时刻；以下反例固定同一冻结时钟 NOW。

### 反例一：新鲜根级时刻 + 陈旧位置（按类别 A 判定）

原始 JSON 三个时刻（不同秒）：根级 time=2026-11-22 11:59:59Z（NOW-1s）；last_contact=2026-11-22 11:59:58Z（NOW-2s）；time_position=2026-11-22 11:40:00Z（NOW-1200s）。

REST 发出的 URL：`https://opensky-network.org/api/states/all?lamin=48.8&lamax=49.2&lomin=2.2&lomax=2.8`

根级 time 不由 `REST.states()` 返回；以上根级观测来自 RecordedREST 传输层记录的原始 JSON。

| icao24 | 判定 |
| --- | --- |
| dd5001 | 过期位置 |

**判定：允许回退后无可交付行**

新鲜的根级 time 不能作为位置新鲜度证据；判定只认行级 time_position，该行位置已过期。

### 反例二：陈旧根级时刻 + 最近接触（按类别 C 判定）

原始 JSON 三个时刻（不同秒）：根级 time=2026-11-22 11:30:00Z（NOW-1800s）；last_contact=2026-11-22 11:59:58Z（NOW-2s）；time_position=2026-11-22 11:35:00Z（NOW-1500s）。

REST 发出的 URL：`https://opensky-network.org/api/states/all?lamin=48.8&lamax=49.2&lomin=2.2&lomax=2.8`

根级 time 不由 `REST.states()` 返回；以上根级观测来自 RecordedREST 传输层记录的原始 JSON。

| icao24 | 判定 |
| --- | --- |
| dd5002 | 可交付 |

**判定：回退交付**

陈旧的根级 time 不能否定行级 last_contact；只要最近接触的请求仍可交付。

### 缺失位置时刻：坐标有效但 time_position 为空（按类别 A 判定）

原始 JSON 三个时刻（不同秒）：根级 time=2026-11-22 11:59:59Z（NOW-1s）；last_contact=2026-11-22 11:59:58Z（NOW-2s）；time_position=—。

REST 发出的 URL：`https://opensky-network.org/api/states/all?lamin=48.8&lamax=49.2&lomin=2.2&lomax=2.8`

根级 time 不由 `REST.states()` 返回；以上根级观测来自 RecordedREST 传输层记录的原始 JSON。

| icao24 | 判定 |
| --- | --- |
| dd5003 | 缺位置时刻（新鲜度证据不足） |

**判定：新鲜度证据不足**

坐标有效但没有任何位置时刻，无法证明新鲜也无法证明过期，只能判为新鲜度证据不足，不得交付。

## 三轮空结果对照（同一合法区域）

### 第一轮：Trino 真实空表

**Trino 原样发出的 history 查询：**

```sql
SELECT state_vectors_data4.time, state_vectors_data4.icao24, state_vectors_data4.callsign, state_vectors_data4.lat, state_vectors_data4.lon, state_vectors_data4.lastcontact, state_vectors_data4.lastposupdate 
FROM state_vectors_data4 
WHERE state_vectors_data4.lon >= 2.2 AND state_vectors_data4.lon <= 2.8 AND state_vectors_data4.lat >= 48.8 AND state_vectors_data4.lat <= 49.2 AND state_vectors_data4.time >= 1795347900 AND state_vectors_data4.time <= 1795348800 AND state_vectors_data4.hour >= 1795345200 AND state_vectors_data4.hour < 1795348800
```

**Trino 结果：** 成功返回真实空表（history() 返回 None）→ 有效答复

**REST：** 未调用（约定：Trino 成功空表即有效答复；已断言 `rest.urls` 为空）。

- 替身已装填 `39ac44`：若被误调用，该行判定为可交付（位置龄期 10s）

**判定：权威查询确认为空**

### 第二轮：Trino 明确超时，REST 只有新接触、空坐标或过期位置

**Trino 原样发出的 history 查询：**

```sql
SELECT state_vectors_data4.time, state_vectors_data4.icao24, state_vectors_data4.callsign, state_vectors_data4.lat, state_vectors_data4.lon, state_vectors_data4.lastcontact, state_vectors_data4.lastposupdate 
FROM state_vectors_data4 
WHERE state_vectors_data4.lon >= 2.2 AND state_vectors_data4.lon <= 2.8 AND state_vectors_data4.lat >= 48.8 AND state_vectors_data4.lat <= 49.2 AND state_vectors_data4.time >= 1795347900 AND state_vectors_data4.time <= 1795348800 AND state_vectors_data4.hour >= 1795345200 AND state_vectors_data4.hour < 1795348800
```

**Trino 结果：** 明确超时（ReadTimeout）→ 允许回退

**REST 原样发出的 URL：**

- `https://opensky-network.org/api/states/all?lamin=48.8&lamax=49.2&lomin=2.2&lomax=2.8`

**REST 返回字段：** `['icao24', 'callsign', 'origin_country', 'last_position', 'timestamp', 'longitude', 'latitude', 'altitude', 'onground', 'groundspeed', 'track', 'vertical_rate', 'sensors', 'geoaltitude', 'squawk', 'spi', 'position_source']`

**REST 根级 time：** 2026-11-22 11:59:57Z（NOW-3s）（`REST.states()` 的返回不含该字段；此观测来自 RecordedREST 传输层记录的原始 JSON）

**行级判定（冻结时钟 NOW）：**

| icao24 | last_contact | time_position | lat | lon | 判定 |
| --- | --- | --- | --- | --- | --- |
| dd4012 | 11:59:55Z（NOW-5s） | — | — | — | 空坐标 |
| dd4013 | 11:59:52Z（NOW-8s） | 11:59:50Z（NOW-10s） | — | — | 空坐标 |
| dd4014 | 11:59:54Z（NOW-6s） | 11:00:00Z（NOW-3600s） | 49.05 | 2.5 | 过期位置 |

**判定：允许回退后无可交付行**

### 第三轮：只改一行的位置时刻与坐标后，REST 可交付

**Trino 原样发出的 history 查询：**

```sql
SELECT state_vectors_data4.time, state_vectors_data4.icao24, state_vectors_data4.callsign, state_vectors_data4.lat, state_vectors_data4.lon, state_vectors_data4.lastcontact, state_vectors_data4.lastposupdate 
FROM state_vectors_data4 
WHERE state_vectors_data4.lon >= 2.2 AND state_vectors_data4.lon <= 2.8 AND state_vectors_data4.lat >= 48.8 AND state_vectors_data4.lat <= 49.2 AND state_vectors_data4.time >= 1795347900 AND state_vectors_data4.time <= 1795348800 AND state_vectors_data4.hour >= 1795345200 AND state_vectors_data4.hour < 1795348800
```

**Trino 结果：** 明确超时（ReadTimeout）→ 允许回退

**REST 原样发出的 URL：**

- `https://opensky-network.org/api/states/all?lamin=48.8&lamax=49.2&lomin=2.2&lomax=2.8`

**REST 返回字段：** `['icao24', 'callsign', 'origin_country', 'last_position', 'timestamp', 'longitude', 'latitude', 'altitude', 'onground', 'groundspeed', 'track', 'vertical_rate', 'sensors', 'geoaltitude', 'squawk', 'spi', 'position_source']`

**REST 根级 time：** 2026-11-22 11:59:57Z（NOW-3s）（`REST.states()` 的返回不含该字段；此观测来自 RecordedREST 传输层记录的原始 JSON）

**行级判定（冻结时钟 NOW）：**

| icao24 | last_contact | time_position | lat | lon | 判定 |
| --- | --- | --- | --- | --- | --- |
| dd4012 | 11:59:55Z（NOW-5s） | 11:59:52Z（NOW-8s） | 49.0 | 2.5 | 可交付 |
| dd4013 | 11:59:52Z（NOW-8s） | 11:59:50Z（NOW-10s） | — | — | 空坐标 |
| dd4014 | 11:59:54Z（NOW-6s） | 11:00:00Z（NOW-3600s） | 49.05 | 2.5 | 过期位置 |

**交付清单（值班位置）：**

| icao24 | callsign | lat | lon | last_position |
| --- | --- | --- | --- | --- |
| dd4012 | NEWCTC | 49.0 | 2.5 | 2026-11-22 11:59:52+00:00 |

**判定：回退交付**

## 第二轮与第三轮的脚本差异

已断言两个脚本仅相差一行（位置时刻与坐标）：

```diff
--- round2_trino_timeout.py
+++ round3_trino_timeout_delivered.py
@@ -14,7 +14,7 @@
 
 ROWS = [
     # dd4012：两轮之间唯一被修改的一行（位置时刻与坐标）
-    ["dd4012", "NEWCTC", "FR", None, NOW_EPOCH - 5, None, None] + TAIL,
+    ["dd4012", "NEWCTC", "FR", NOW_EPOCH - 8, NOW_EPOCH - 5, 2.5, 49.0] + TAIL,
     # dd4013：位置时刻新鲜，但经纬度为空
     ["dd4013", "NOGEO", "FR", NOW_EPOCH - 10, NOW_EPOCH - 8, None, None]
     + TAIL,
```
