"""気象庁 MSM (Open-Meteo 経由) の毎時気象を VictoriaMetrics に取り込む。

取得元は Open-Meteo の Historical Forecast API (models=jma_msm)。各予報ランの先頭数時間を
つなげた「予報モデルによる実況推定」で、5km 格子・毎時。過去の補完 (backfill) と毎時更新を
同じ API で行うので、どの時刻も同じ作り方の値になる。

VM は dedup (同一タイムスタンプは max 優先) なので、同じ時刻を値を変えて書き直せない。
そこで各時刻は「値が確定してから一度だけ」書く: MSM は3時間ごとのランが約3.5時間遅れで
公開され、時刻 h の値はラン floor3(h) (h を含む直前の初期時刻) が取り込まれた時点で最後の
上書きを受ける。SETTLE_HOURS より新しい時刻は書かず、さらに Open-Meteo のモデル情報
(meta.json) で「そのランがもう公開されているか」を確かめて、ランが遅れているときは待つ。

確定前の時刻 (直近 SETTLE_HOURS + 先の FORECAST_HOURS) は Forecast API の予報値を
別の計測名 weather_forecast_* に書く。予報は毎回変わるので、書く前に系列ごと delete して
最新の予報だけが残るようにする (dedup の max 優先を避けるため)。

降水量・日射は Open-Meteo の慣例どおり「その時刻までの1時間」の合計/平均
(例: 13:00 の値は 12:00〜13:00)。気温・湿度・風は時刻ちょうどの瞬時値。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Dict, List, Optional

import requests

API_URL = "https://historical-forecast-api.open-meteo.com/v1/forecast"
FORECAST_API_URL = "https://api.open-meteo.com/v1/forecast"
META_URL = "https://api.open-meteo.com/data/jma_msm/static/meta.json"
MODEL = "jma_msm"
MEASUREMENT = "weather"
FORECAST_MEASUREMENT = "weather_forecast"
SETTLE_HOURS = 6
# MSM のラン間隔。時刻 h を最後に上書きするランは floor3(h) なので、最新ラン init が公開済みなら
# init + (RUN_INTERVAL_HOURS - 1) 時間までが確定
RUN_INTERVAL_HOURS = 3
# Open-Meteo の案内: 公開時刻から 10 分は取り込み途中の可能性があるので待つ
META_GRACE_S = 600
# 予報を書く先の時間数 (MSM の予報時間は 39h / 78h。それ以降は古いランの値)
FORECAST_HOURS = 48
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


@dataclass(frozen=True)
class ModelRun:
    """Open-Meteo に取り込み済みの最新ラン (unix秒)。"""

    init: int  # 初期時刻 (UTC)
    available: int  # API で使えるようになった時刻


def parse_location(lat: str, lon: str) -> Location:
    """WEATHER_LAT / WEATHER_LON を検証して Location に。未設定・範囲外は ValueError。"""
    if not lat.strip() or not lon.strip():
        raise ValueError("WEATHER_LAT / WEATHER_LON が未設定です (.env)")
    loc = Location(float(lat), float(lon))
    if not (-90 <= loc.lat <= 90 and -180 <= loc.lon <= 180):
        raise ValueError(f"緯度経度が範囲外です: {loc}")
    return loc


def _common_params(loc: Location) -> dict:
    return {
        "latitude": loc.lat,
        "longitude": loc.lon,
        "hourly": ",".join(FIELDS),
        "models": MODEL,
        "timeformat": "unixtime",
        "timezone": "GMT",
        "wind_speed_unit": "ms",
    }


def fetch_hourly(loc: Location, start: date, end: date, timeout_s: float) -> Dict[int, Dict[str, float]]:
    """start〜end (両端含む, UTC日付) の毎時値を {unix秒: {field: 値}} で返す。null は落とす。"""
    r = requests.get(
        API_URL,
        params={**_common_params(loc), "start_date": start.isoformat(), "end_date": end.isoformat()},
        timeout=timeout_s,
    )
    r.raise_for_status()
    return parse_hourly(r.json())


def fetch_forecast(loc: Location, past_hours: int, forecast_hours: int, timeout_s: float) -> Dict[int, Dict[str, float]]:
    """現在の正時から past_hours 前〜forecast_hours 先までの最新予報 (ランをまたいで最新値)。"""
    r = requests.get(
        FORECAST_API_URL,
        params={**_common_params(loc), "past_hours": past_hours, "forecast_hours": forecast_hours},
        timeout=timeout_s,
    )
    r.raise_for_status()
    return parse_hourly(r.json())


def fetch_model_run(timeout_s: float) -> ModelRun:
    """Open-Meteo の jma_msm モデル情報から、取り込み済みの最新ランを得る。"""
    r = requests.get(META_URL, timeout=timeout_s)
    r.raise_for_status()
    meta = r.json()
    return ModelRun(int(meta["last_run_initialisation_time"]), int(meta["last_run_availability_time"]))


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


def floor_hour(now: datetime) -> int:
    return int(now.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).timestamp())


def settled_until(now: datetime, run: Optional[ModelRun] = None) -> int:
    """書いてよい最新の時刻 (unix秒, 正時)。

    基本は now - SETTLE_HOURS。最新ランが分かるときは、そのランで最後に上書きされる時刻
    (init + RUN_INTERVAL_HOURS - 1) より先には進めない (ランの公開が遅れている間は待つ)。
    公開から META_GRACE_S 以内は取り込み途中かもしれないので、ひとつ前のランとみなす。
    """
    fixed = floor_hour(now) - SETTLE_HOURS * 3600
    if run is None:
        return fixed
    init = run.init
    if int(now.timestamp()) < run.available + META_GRACE_S:
        init -= RUN_INTERVAL_HOURS * 3600
    return min(fixed, init + (RUN_INTERVAL_HOURS - 1) * 3600)


def select_rows(rows: Dict[int, Dict[str, float]], after: Optional[int], until: int) -> Dict[int, Dict[str, float]]:
    """after より後 (未書き込み) かつ until 以前 (確定済み) の時刻だけ残す。"""
    return {ts: f for ts, f in rows.items() if ts <= until and (after is None or ts > after)}


def forecast_window(now: datetime, until: int) -> tuple[int, int]:
    """予報として書く時刻範囲 (両端含む)。確定済みの最終時刻 until から始めて破線がつながるようにする。"""
    return until, floor_hour(now) + FORECAST_HOURS * 3600


def forecast_past_hours(now: datetime, since: int) -> int:
    """Forecast API の past_hours: 現在の正時から since まで遡る時間数。"""
    return max(1, (floor_hour(now) - since) // 3600)


def select_forecast_rows(rows: Dict[int, Dict[str, float]], since: int, end: int) -> Dict[int, Dict[str, float]]:
    return {ts: f for ts, f in rows.items() if since <= ts <= end}


def build_lines(rows: Dict[int, Dict[str, float]], measurement: str = MEASUREMENT) -> List[str]:
    lines = []
    for ts in sorted(rows):
        fields = ",".join(f"{k}={v}" for k, v in sorted(rows[ts].items()))
        lines.append(f"{measurement},source={MODEL} {fields} {ts * 1_000_000_000}")
    return lines


def last_written(influx_url: str, timeout_s: float, lookback_days: int = 30) -> Optional[int]:
    """VM に書き込み済みの確定値の最新時刻 (unix秒)。無ければ None。"""
    r = requests.get(
        f"{influx_url}/api/v1/query",
        params={"query": f'max(tlast_over_time({MEASUREMENT}_temp_c{{source="{MODEL}"}}[{lookback_days}d]))'},
        timeout=timeout_s,
    )
    r.raise_for_status()
    result = r.json()["data"]["result"]
    if not result:
        return None
    return int(float(result[0]["value"][1]))


def delete_forecast(influx_url: str, timeout_s: float) -> None:
    """予報系列を丸ごと消す (書き直しのため。VM の delete は時間範囲を指定できない)。"""
    r = requests.post(
        f"{influx_url}/api/v1/admin/tsdb/delete_series",
        params={"match[]": f'{{__name__=~"{FORECAST_MEASUREMENT}_.*",source="{MODEL}"}}'},
        timeout=timeout_s,
    )
    r.raise_for_status()


def date_chunks(start: date, end: date, days: int = BACKFILL_CHUNK_DAYS) -> List[tuple[date, date]]:
    chunks = []
    cur = start
    while cur <= end:
        stop = min(end, cur + timedelta(days=days - 1))
        chunks.append((cur, stop))
        cur = stop + timedelta(days=1)
    return chunks


def update_window(last: Optional[int], until: int) -> tuple[date, date, Optional[int]]:
    """毎時更新で取りに行く UTC 日付範囲と、書き込み済み境界を返す。"""
    after = last if last is not None else until - DEFAULT_LOOKBACK_HOURS * 3600
    start = datetime.fromtimestamp(after + 3600, timezone.utc).date()
    end = datetime.fromtimestamp(until, timezone.utc).date()
    return start, end, after
