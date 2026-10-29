"""気象庁 MSM (Open-Meteo 経由) の毎時気象を VictoriaMetrics に取り込む。

取得元は Open-Meteo の Historical Forecast API (models=jma_msm)。各予報ランの先頭数時間を
つなげた「予報モデルによる実況推定」で、5km 格子・毎時。過去の補完 (backfill) と毎時更新を
同じ API で行うので、どの時刻も同じ作り方の値になる。

VM は dedup (同一タイムスタンプは max 優先) なので、同じ時刻を値を変えて書き直せない。
そこで各時刻は「値が確定してから一度だけ」書く: MSM は3時間ごとのランが数時間遅れで
公開されるため、SETTLE_HOURS より新しい時刻は書かずに次回へ回す。

降水量・日射は Open-Meteo の慣例どおり「その時刻までの1時間」の合計/平均
(例: 13:00 の値は 12:00〜13:00)。気温・湿度・風は時刻ちょうどの瞬時値。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

API_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
MODEL = "jma_msm"
SETTLE_HOURS = 6
# 前回の書き込みが見つからないとき (初回・長期停止後) に遡る範囲。それ以上は backfill で入れる
DEFAULT_LOOKBACK_HOURS = 72
BACKFILL_CHUNK_DAYS = 31

# Open-Meteo の変数名 -> line protocol の field 名 (VM では weather_<field>)
FIELDS: Dict[str, str] = {
    "temperature_2m": "temp_c",
    "relative_humidity_2m": "humidity_pct",
    "dew_point_2m": "dewpoint_c",
    "precipitation": "precip_mm",
    "cloud_cover": "cloud_pct",
    "wind_speed_10m": "wind_ms",
    "wind_direction_10m": "wind_dir_deg",
    "shortwave_radiation": "ghi_wm2",
    "direct_radiation": "direct_wm2",
    "diffuse_radiation": "diffuse_wm2",
}


@dataclass(frozen=True)
class Location:
    lat: float
    lon: float


def parse_location(lat: str, lon: str) -> Location:
    """WEATHER_LAT / WEATHER_LON を検証して Location に。未設定・範囲外は ValueError。"""
    if not lat.strip() or not lon.strip():
        raise ValueError("WEATHER_LAT / WEATHER_LON が未設定です (.env)")
    loc = Location(float(lat), float(lon))
    if not (-90 <= loc.lat <= 90 and -180 <= loc.lon <= 180):
        raise ValueError(f"緯度経度が範囲外です: {loc}")
    return loc


def fetch_hourly(loc: Location, start: date, end: date, timeout_s: float) -> Dict[int, Dict[str, float]]:
    """start〜end (両端含む, UTC日付) の毎時値を {unix秒: {field: 値}} で返す。null は落とす。"""
    r = requests.get(
        API_URL,
        params={
            "latitude": loc.lat,
            "longitude": loc.lon,
            "hourly": ",".join(FIELDS),
            "models": MODEL,
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "timeformat": "unixtime",
            "timezone": "GMT",
            "wind_speed_unit": "ms",
        },
        timeout=timeout_s,
    )
    r.raise_for_status()
    return parse_hourly(r.json())


def parse_hourly(payload: dict) -> Dict[int, Dict[str, float]]:
    hourly = payload.get("hourly")
    if not hourly or "time" not in hourly:
        raise ValueError(f"hourly がありません: {str(payload)[:200]}")
    rows: Dict[int, Dict[str, float]] = {}
    for i, ts in enumerate(hourly["time"]):
        fields = {}
        for var, name in FIELDS.items():
            values = hourly.get(var) or []
            if i < len(values) and values[i] is not None:
                fields[name] = float(values[i])
        if fields:
            rows[int(ts)] = fields
    return rows


def settled_until(now: datetime) -> int:
    """書いてよい最新の時刻 (unix秒, 正時)。"""
    cutoff = now.astimezone(timezone.utc) - timedelta(hours=SETTLE_HOURS)
    return int(cutoff.replace(minute=0, second=0, microsecond=0).timestamp())


def select_rows(rows: Dict[int, Dict[str, float]], after: Optional[int], until: int) -> Dict[int, Dict[str, float]]:
    """after より後 (未書き込み) かつ until 以前 (確定済み) の時刻だけ残す。"""
    return {ts: f for ts, f in rows.items() if ts <= until and (after is None or ts > after)}


def build_lines(rows: Dict[int, Dict[str, float]]) -> List[str]:
    lines = []
    for ts in sorted(rows):
        fields = ",".join(f"{k}={v}" for k, v in sorted(rows[ts].items()))
        lines.append(f"weather,source={MODEL} {fields} {ts * 1_000_000_000}")
    return lines


def last_written(influx_url: str, timeout_s: float, lookback_days: int = 30) -> Optional[int]:
    """VM に書き込み済みの最新時刻 (unix秒)。無ければ None。"""
    r = requests.get(
        f"{influx_url}/api/v1/query",
        params={"query": f'max(tlast_over_time(weather_temp_c{{source="{MODEL}"}}[{lookback_days}d]))'},
        timeout=timeout_s,
    )
    r.raise_for_status()
    result = r.json()["data"]["result"]
    if not result:
        return None
    return int(float(result[0]["value"][1]))


def date_chunks(start: date, end: date, days: int = BACKFILL_CHUNK_DAYS) -> List[tuple[date, date]]:
    chunks = []
    cur = start
    while cur <= end:
        stop = min(end, cur + timedelta(days=days - 1))
        chunks.append((cur, stop))
        cur = stop + timedelta(days=1)
    return chunks


def update_window(now: datetime, last: Optional[int]) -> tuple[date, date, Optional[int]]:
    """毎時更新で取りに行く UTC 日付範囲と、書き込み済み境界を返す。"""
    until = settled_until(now)
    after = last if last is not None else until - DEFAULT_LOOKBACK_HOURS * 3600
    start = datetime.fromtimestamp(after + 3600, timezone.utc).date()
    end = datetime.fromtimestamp(until, timezone.utc).date()
    return start, end, after
