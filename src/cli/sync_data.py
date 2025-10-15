# src/cli/sync_data.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SwitchBot (Cloud API v1.1) -> InfluxDB v2/v3 Writer (Typer CLI)

機能:
- SwitchBot の温湿度/CO2/電池を取得
- 絶対湿度 (abs_humidity, g/m^3) を「岡田の式(液水, -30..50℃) + 増強係数 f(T,P)」で算出
  - -30℃未満は安全側で Goff–Gratch(氷) にフォールバック
  - f(T,P) は EF_MODEL=none/buck/its90 で切替（既定 none）
- InfluxDB v2/v3 へ Line Protocol でバッチ書き込み
  - v3 ネイティブ: /api/v3/write_lp (USE_V3_NATIVE=true)
  - v2 互換     : /api/v2/write     (既定, v2/v3 どちらでも利用可)

ENV (.env 推奨):
  SWITCHBOT_TOKEN, SWITCHBOT_SECRET
  INFLUX_URL              (例: http://localhost:8086)
  INFLUX_BUCKET_OR_DB     (v2: bucket名 / v3: DB名)
  INFLUX_TOKEN
  LOCATION_PREFIX         (任意, 既定 "")
  REQUEST_TIMEOUT_S       (任意, 既定 "10")
  USE_V3_NATIVE           ("true"/"false", 既定 "false")
  EF_MODEL                ("none"|"buck"|"its90", 既定 "none")
"""

from __future__ import annotations

import base64
import math
import os
import time
import uuid
import hmac
import hashlib
from typing import Annotated, Dict, List, Optional

import requests
import typer
from dotenv import load_dotenv
from urllib.parse import urljoin

app = typer.Typer(add_completion=False, help="Home sensors: SwitchBot -> InfluxDB writer")

# ---------------------------------------------------------------------
# 設定/定数
# ---------------------------------------------------------------------

SB_BASE = "https://api.switch-bot.com/v1.1/"

# 温湿度メーター + CO2 メーター系（deviceType 名は世代/地域で揺れるため緩めに）
METER_TYPES = {
    "Meter", "MeterPlus", "WoSensorTH", "Outdoor Meter", "MeterTH", "MeterPro",
    "CO2 Meter", "WoCO2", "Indoor Air Quality Monitor", "Air Quality Monitor",
}

# 岡田の式（液水, -30..50℃）の係数（log10(es[hPa]) = a0 + a1*T + a2*T^2 + a3*T^3 + a4*T^4）
_OKADA_WATER_COEFF = {
    "a0": 1.809378,
    "a1": 0.07266115,
    "a2": -3.003879e-4,
    "a3": 1.181765e-6,
    "a4": -3.863083e-9,
}

# ---------------------------------------------------------------------
# 共通ユーティリティ
# ---------------------------------------------------------------------

def _require(v: Optional[str], name: str):
    if not v:
        raise typer.BadParameter(f"環境変数 {name} が未設定です")

def _load_env():
    load_dotenv()
    env = {
        "SWITCHBOT_TOKEN": os.getenv("SWITCHBOT_TOKEN"),
        "SWITCHBOT_SECRET": os.getenv("SWITCHBOT_SECRET"),
        "INFLUX_URL": os.getenv("INFLUX_URL", "http://localhost:8086"),
        "INFLUX_BUCKET_OR_DB": os.getenv("INFLUX_BUCKET_OR_DB"),
        "INFLUX_TOKEN": os.getenv("INFLUX_TOKEN"),
        "LOCATION_PREFIX": os.getenv("LOCATION_PREFIX", ""),
        "REQUEST_TIMEOUT_S": float(os.getenv("REQUEST_TIMEOUT_S", "10")),
        "USE_V3_NATIVE": os.getenv("USE_V3_NATIVE", "false").lower() == "true",
        "EF_MODEL": os.getenv("EF_MODEL", "none"),
    }
    return env

# ---------------------------------------------------------------------
# SwitchBot API
# ---------------------------------------------------------------------

def _sb_headers(token: str, secret: str) -> Dict[str, str]:
    t = str(int(time.time() * 1000))  # ms
    nonce = str(uuid.uuid4())
    to_sign = token + t + nonce
    sign = base64.b64encode(hmac.new(secret.encode(), to_sign.encode(), hashlib.sha256).digest()).decode()
    # ヘッダは小文字キーが無難
    return {
        "authorization": token,
        "sign": sign,
        "t": t,
        "nonce": nonce,
        "Content-Type": "application/json; charset=utf8",
    }

def _sb_get(path: str, token: str, secret: str, timeout_s: float):
    url = urljoin(SB_BASE, path)
    r = requests.get(url, headers=_sb_headers(token, secret), timeout=timeout_s)
    r.raise_for_status()
    data = r.json()
    if data.get("statusCode") != 100:
        raise RuntimeError(f"SwitchBot API error: {data}")
    return data

def _get_devices(token: str, secret: str, timeout_s: float):
    return _sb_get("devices", token, secret, timeout_s).get("body", {}).get("deviceList", [])

def _get_status(device_id: str, token: str, secret: str, timeout_s: float):
    return _sb_get(f"devices/{device_id}/status", token, secret, timeout_s).get("body", {})

# ---------------------------------------------------------------------
# 飽和水蒸気圧・絶対湿度（岡田の式 + 増強係数）
# ---------------------------------------------------------------------

def es_okada_water_hpa(temp_c: float) -> float:
    """岡田の式（液水，-30..50℃）で飽和水蒸気圧[hPa]を返す"""
    T = float(temp_c)
    a0 = _OKADA_WATER_COEFF["a0"]; a1 = _OKADA_WATER_COEFF["a1"]
    a2 = _OKADA_WATER_COEFF["a2"]; a3 = _OKADA_WATER_COEFF["a3"]; a4 = _OKADA_WATER_COEFF["a4"]
    log10_es = a0 + a1*T + a2*T*T + a3*T*T*T + a4*T*T*T*T
    return 10.0 ** log10_es

def es_goff_gratch_ice_hpa(temp_c: float) -> float:
    """Goff–Gratch（氷，T<0℃）の飽和水蒸気圧[hPa]"""
    T = temp_c + 273.15
    T0 = 273.16
    term = (-9.09718 * (T0 / T - 1.0)
            - 3.56654 * math.log10(T0 / T)
            + 0.876793 * (1.0 - T / T0)
            + math.log10(6.1071))
    return 10.0 ** term

def enhancement_factor(temp_c: float, pres_hpa: float, model: str = "none") -> float:
    """
    増強係数 f(T,P) を返す（室内・常圧近傍の実務想定）。
    model: "none" | "buck" | "its90"
    """
    m = (model or "none").lower()
    if m == "none":
        return 1.0
    elif m == "buck":
        # Buck の簡便式（P[hPa]依存のみを採用）
        return 1.0007 + 3.46e-6 * pres_hpa
    elif m == "its90":
        # ITS-90/Hardy 近似の簡略形
        return 1.00062 + 3.14e-6 * pres_hpa + 5.6e-7 * (temp_c * temp_c)
    else:
        return 1.0

def calc_abs_humidity_gm3_okada(
    temp_c: float,
    rh_percent: float,
    pres_hpa: float = 1013.25,
    f_model: str = "none",
) -> float:
    """
    絶対湿度[g/m^3]を Okada(液水) + f(T,P) で計算（室内域向け）。
    -30..50℃は岡田（液水）。それより低温域は安全側で Goff–Gratch（氷）にフォールバック。
    """
    T_K = temp_c + 273.15
    RH = max(0.0, min(100.0, float(rh_percent))) / 100.0

    if temp_c >= -30.0:
        es_hpa = es_okada_water_hpa(temp_c)
    else:
        es_hpa = es_goff_gratch_ice_hpa(temp_c)

    f = enhancement_factor(temp_c, pres_hpa, f_model)
    e_hpa = RH * es_hpa * f
    e_kpa = e_hpa / 10.0
    return 216.7 * e_kpa / T_K

# ---------------------------------------------------------------------
# InfluxDB（Line Protocol）
# ---------------------------------------------------------------------

def _lp(measurement: str, tags: Dict[str, str], fields: Dict[str, float | int | bool], ts_ms: int) -> str:
    def esc(s): return str(s).replace(" ", r"\ ").replace(",", r"\,")
    tag_str = ",".join(f"{esc(k)}={esc(v)}" for k, v in tags.items() if v is not None)
    fparts: List[str] = []
    for k, v in fields.items():
        if isinstance(v, bool):
            fparts.append(f"{esc(k)}={str(v).lower()}")
        elif isinstance(v, int):
            fparts.append(f"{esc(k)}={v}i")
        else:
            fparts.append(f"{esc(k)}={float(v)}")
    ts_ns = ts_ms * 1_000_000
    return f"{esc(measurement)},{tag_str} {','.join(fparts)} {ts_ns}"

def _write_influx(
    lines: List[str],
    influx_url: str,
    bucket_or_db: str,
    token: str,
    timeout_s: float,
    use_v3_native: bool,
):
    headers = {"Authorization": f"Token {token}", "Content-Type": "text/plain"}
    payload = "\n".join(lines)
    if use_v3_native:
        url = f"{influx_url}/api/v3/write_lp"
        params = {"db": bucket_or_db, "precision": "ns"}
    else:
        url = f"{influx_url}/api/v2/write"
        params = {"bucket": bucket_or_db, "precision": "ns"}  # v2互換（v3でもOK）

    r = requests.post(url, params=params, data=payload.encode("utf-8"),
                      headers=headers, timeout=timeout_s)
    r.raise_for_status()

# ---------------------------------------------------------------------
# データ収集
# ---------------------------------------------------------------------

def _collect_once(
    location_prefix: str,
    token: str,
    secret: str,
    timeout_s: float,
    ef_model: str = "none",
) -> list[str]:
    devices = _get_devices(token, secret, timeout_s)
    ts_ms = int(time.time() * 1000)
    lines: list[str] = []

    for d in devices:
        dtype = d.get("deviceType", "")
        device_id = d.get("deviceId")
        name = d.get("deviceName") or device_id

        # まずはタイプでフィルタ。ただし不明タイプでも status に必要キーがあれば拾う
        if dtype in METER_TYPES:
            st = _get_status(device_id, token, secret, timeout_s)
        else:
            try:
                st = _get_status(device_id, token, secret, timeout_s)
            except Exception:
                continue
            if not any(k in st for k in ("temperature", "humidity", "co2")):
                continue

        temp = st.get("temperature")
        hum  = st.get("humidity")
        batt = st.get("battery")
        co2  = st.get("co2")

        if temp is None and hum is None and co2 is None:
            continue

        abs_h = None
        if temp is not None and hum is not None:
            try:
                abs_h = calc_abs_humidity_gm3_okada(
                    float(temp), float(hum),
                    pres_hpa=1013.25,
                    f_model=ef_model,
                )
            except Exception:
                abs_h = None

        tags = {
            "location": f"{location_prefix}{name}",
            "device_id": device_id,
            "type": dtype,
        }
        fields: dict[str, float | int] = {}
        if temp is not None:
            fields["temperature"] = float(temp)          # ℃
        if hum is not None:
            fields["humidity"] = float(hum)              # RH %
        if abs_h is not None:
            fields["abs_humidity"] = float(abs_h)        # g/m^3
        if co2 is not None:
            try:
                fields["co2"] = int(round(float(co2)))   # ppm
            except Exception:
                pass
        if batt is not None:
            try:
                fields["battery"] = int(batt)            # %
            except Exception:
                pass

        if not fields:
            continue

        lines.append(_lp("climate", tags, fields, ts_ms))

    return lines

# ---------------------------------------------------------------------
# Typer コマンド
# ---------------------------------------------------------------------

@app.command(help="SwitchBot の温湿度/CO2 を取得し、InfluxDB に1回だけ書き込みます。")
def push(
    use_v3_native: Annotated[bool, typer.Option("--use-v3-native", help="InfluxDB v3 ネイティブAPI (/api/v3/write_lp) を使用")] = False,
    bucket_or_db: Annotated[Optional[str], typer.Option("--bucket-or-db", help="v2: bucket名 / v3: DB名。未指定ならENVを使用")] = None,
    influx_url: Annotated[Optional[str], typer.Option("--influx-url", help="Influx URL（例: http://localhost:8086）")] = None,
    token_influx: Annotated[Optional[str], typer.Option("--influx-token", help="Influx API トークン")] = None,
    location_prefix: Annotated[Optional[str], typer.Option("--location-prefix", help="location タグの接頭辞")] = None,
    ef_model: Annotated[str, typer.Option("--ef-model", help="増強係数モデル: none|buck|its90")] = "none",
    timeout_s: Annotated[Optional[float], typer.Option("--timeout-s", help="HTTP タイムアウト（秒）")] = None,
):
    env = _load_env()
    sb_token  = env["SWITCHBOT_TOKEN"]
    sb_secret = env["SWITCHBOT_SECRET"]
    _require(sb_token, "SWITCHBOT_TOKEN")
    _require(sb_secret, "SWITCHBOT_SECRET")

    influx_url   = influx_url or env["INFLUX_URL"]
    bucket_or_db = bucket_or_db or env["INFLUX_BUCKET_OR_DB"]
    token_influx = token_influx or env["INFLUX_TOKEN"]
    location_prefix = location_prefix or env["LOCATION_PREFIX"]
    timeout_s    = timeout_s or env["REQUEST_TIMEOUT_S"]
    use_v3_native = use_v3_native or env["USE_V3_NATIVE"]
    if ef_model == "none":
        ef_model = env["EF_MODEL"]

    _require(bucket_or_db, "INFLUX_BUCKET_OR_DB")
    _require(token_influx, "INFLUX_TOKEN")

    lines = _collect_once(location_prefix, sb_token, sb_secret, timeout_s, ef_model=ef_model)
    if not lines:
        typer.echo("no datapoints")
        raise typer.Exit(code=0)

    _write_influx(lines, influx_url, bucket_or_db, token_influx, timeout_s, use_v3_native)
    typer.echo(f"wrote {len(lines)} points")

@app.command(help="SwitchBot デバイス一覧を表示します。")
def devices(
    timeout_s: Annotated[Optional[float], typer.Option("--timeout-s", help="HTTP タイムアウト（秒）")] = None,
):
    env = _load_env()
    sb_token  = env["SWITCHBOT_TOKEN"]
    sb_secret = env["SWITCHBOT_SECRET"]
    _require(sb_token, "SWITCHBOT_TOKEN")
    _require(sb_secret, "SWITCHBOT_SECRET")
    timeout_s = timeout_s or env["REQUEST_TIMEOUT_S"]

    devs = _get_devices(sb_token, sb_secret, timeout_s)
    if not devs:
        typer.echo("no devices")
        raise typer.Exit()

    for d in devs:
        typer.echo(f"- {d.get('deviceName') or d.get('deviceId')} "
                   f"(type={d.get('deviceType')}, id={d.get('deviceId')})")

@app.command(help="指定間隔で push を繰り返します（cron 代替）。")
def run(
    interval: Annotated[int, typer.Option("--interval", "-i", help="実行間隔（秒）", min=30)] = 300,
    use_v3_native: Annotated[bool, typer.Option("--use-v3-native")] = False,
    bucket_or_db: Annotated[Optional[str], typer.Option("--bucket-or-db")] = None,
    influx_url: Annotated[Optional[str], typer.Option("--influx-url")] = None,
    token_influx: Annotated[Optional[str], typer.Option("--influx-token")] = None,
    location_prefix: Annotated[Optional[str], typer.Option("--location-prefix")] = None,
    ef_model: Annotated[str, typer.Option("--ef-model", help="増強係数モデル: none|buck|its90")] = "none",
    timeout_s: Annotated[Optional[float], typer.Option("--timeout-s")] = None,
):
    env = _load_env()
    sb_token  = env["SWITCHBOT_TOKEN"]
    sb_secret = env["SWITCHBOT_SECRET"]
    _require(sb_token, "SWITCHBOT_TOKEN")
    _require(sb_secret, "SWITCHBOT_SECRET")

    influx_url   = influx_url or env["INFLUX_URL"]
    bucket_or_db = bucket_or_db or env["INFLUX_BUCKET_OR_DB"]
    token_influx = token_influx or env["INFLUX_TOKEN"]
    location_prefix = location_prefix or env["LOCATION_PREFIX"]
    timeout_s    = timeout_s or env["REQUEST_TIMEOUT_S"]
    use_v3_native = use_v3_native or env["USE_V3_NATIVE"]
    if ef_model == "none":
        ef_model = env["EF_MODEL"]

    _require(bucket_or_db, "INFLUX_BUCKET_OR_DB")
    _require(token_influx, "INFLUX_TOKEN")

    typer.echo(f"Starting loop: every {interval}s (Ctrl+C to stop)")
    try:
        while True:
            try:
                lines = _collect_once(location_prefix, sb_token, sb_secret, timeout_s, ef_model=ef_model)
                if lines:
                    _write_influx(lines, influx_url, bucket_or_db, token_influx, timeout_s, use_v3_native)
                    typer.echo(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] wrote {len(lines)} points")
                else:
                    typer.echo(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] no datapoints")
            except Exception as e:
                typer.echo(f"error: {e}")
            time.sleep(interval)
    except KeyboardInterrupt:
        typer.echo("stopped")

if __name__ == "__main__":
    app()

