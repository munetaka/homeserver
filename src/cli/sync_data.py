# src/cli/sync_data.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SwitchBot (Cloud API v1.1 / BLE) -> InfluxDB v2/v3 Writer (Typer CLI)

機能:
- SwitchBot の温湿度/CO2/電池を取得（クラウドAPIまたはBLEを選択）
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
  SWITCHBOT_MODE          ("api"|"ble", 既定 "api")
  SWITCHBOT_BLE_DEVICES   (例: "AA:BB:CC:DD:EE:FF@meter=Living,11:22:33:44:55:66@co2=Office")
  SWITCHBOT_BLE_SCAN_TIMEOUT (任意, 既定 "5")
"""

from __future__ import annotations

import hashlib
import hmac
import base64
import math
import os
import time
import uuid
from typing import Annotated, Any, Dict, List, Optional

import requests
import typer
from dotenv import load_dotenv
from urllib.parse import urljoin

from .switchbot_ble import (
    BleTarget,
    SwitchBotReading,
    collect_ble_readings,
    parse_ble_target,
    parse_ble_targets,
    scan_switchbot_devices,
)

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

SUPPORTED_MODES = {"api", "ble"}

# ---------------------------------------------------------------------
# 共通ユーティリティ
# ---------------------------------------------------------------------

def _require(v: Optional[str], name: str):
    if not v:
        raise typer.BadParameter(f"環境変数 {name} が未設定です")


def _coerce_float(value) -> Optional[float]:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coerce_int(value) -> Optional[int]:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None

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
        "SWITCHBOT_MODE": os.getenv("SWITCHBOT_MODE", "api").lower(),
        "SWITCHBOT_BLE_DEVICES": os.getenv("SWITCHBOT_BLE_DEVICES", ""),
        "SWITCHBOT_BLE_SCAN_TIMEOUT": float(os.getenv("SWITCHBOT_BLE_SCAN_TIMEOUT", "5")),
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
    # 係数は自然対数 ln(es[hPa]) 用（0℃で es=6.107 hPa となる）
    ln_es = a0 + a1*T + a2*T*T + a3*T*T*T + a4*T*T*T*T
    return math.exp(ln_es)

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
    # AH[g/m^3] = 216.7 * e[hPa] / T[K]
    return 216.7 * e_hpa / T_K

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

def _collect_api_readings(
    token: str,
    secret: str,
    timeout_s: float,
) -> List[SwitchBotReading]:
    devices = _get_devices(token, secret, timeout_s)
    readings: List[SwitchBotReading] = []
    for device in devices:
        dtype = device.get("deviceType", "") or "unknown"
        device_id = device.get("deviceId") or dtype
        name = device.get("deviceName") or device_id

        if dtype in METER_TYPES:
            status = _get_status(device_id, token, secret, timeout_s)
        else:
            try:
                status = _get_status(device_id, token, secret, timeout_s)
            except Exception:
                continue
            if not any(k in status for k in ("temperature", "humidity", "co2", "battery")):
                continue

        temperature = _coerce_float(status.get("temperature"))
        humidity = _coerce_float(status.get("humidity"))
        co2 = _coerce_int(status.get("co2"))
        battery = _coerce_int(status.get("battery"))
        if temperature is None and humidity is None and co2 is None and battery is None:
            continue

        readings.append(
            SwitchBotReading(
                device_id=device_id,
                name=name,
                device_type=dtype,
                temperature=temperature,
                humidity=humidity,
                co2=co2,
                battery=battery,
            )
        )
    return readings


def _gather_readings(
    mode: str,
    token: Optional[str],
    secret: Optional[str],
    timeout_s: float,
    ble_targets: Optional[List[BleTarget]],
    ble_scan_timeout_s: float,
) -> List[SwitchBotReading]:
    if mode == "api":
        if not token or not secret:
            raise RuntimeError("SwitchBot API token/secret are required for API mode")
        return _collect_api_readings(token, secret, timeout_s)
    elif mode == "ble":
        targets = ble_targets or []
        return collect_ble_readings(targets, ble_scan_timeout_s)
    else:
        raise RuntimeError(f"Unsupported mode '{mode}'")


def _reading_to_line(
    reading: SwitchBotReading,
    location_prefix: str,
    ef_model: str,
    ts_ms: int,
) -> Optional[str]:
    fields: Dict[str, float | int] = {}
    if reading.temperature is not None:
        fields["temperature"] = float(reading.temperature)
    if reading.humidity is not None:
        fields["humidity"] = float(reading.humidity)
    if reading.temperature is not None and reading.humidity is not None:
        try:
            abs_h = calc_abs_humidity_gm3_okada(
                float(reading.temperature),
                float(reading.humidity),
                pres_hpa=1013.25,
                f_model=ef_model,
            )
        except Exception:
            abs_h = None
        if abs_h is not None:
            fields["abs_humidity"] = float(abs_h)
    if reading.co2 is not None:
        fields["co2"] = int(reading.co2)
    if reading.battery is not None:
        fields["battery"] = int(reading.battery)
    if not fields:
        return None

    tags = {
        "location": f"{location_prefix}{reading.name}",
        "device_id": reading.device_id or reading.name,
        "type": reading.device_type,
    }
    return _lp("climate", tags, fields, ts_ms)


def _collect_once(
    location_prefix: str,
    mode: str,
    timeout_s: float,
    ef_model: str,
    token: Optional[str] = None,
    secret: Optional[str] = None,
    ble_targets: Optional[List[BleTarget]] = None,
    ble_scan_timeout_s: float = 5.0,
) -> list[str]:
    ts_ms = int(time.time() * 1000)
    readings = _gather_readings(
        mode=mode,
        token=token,
        secret=secret,
        timeout_s=timeout_s,
        ble_targets=ble_targets,
        ble_scan_timeout_s=ble_scan_timeout_s,
    )
    lines: List[str] = []
    for reading in readings:
        line = _reading_to_line(reading, location_prefix, ef_model, ts_ms)
        if line:
            lines.append(line)
    return lines


def _guess_ble_type(device_type: str) -> str:
    dt = (device_type or "").lower()
    if "co2" in dt or "air quality" in dt:
        return "co2"
    return "meter"


def _format_reading(reading: Optional[SwitchBotReading]) -> str:
    if not reading:
        return "n/a"
    parts: list[str] = []
    if reading.temperature is not None:
        parts.append(f"temp={reading.temperature:.1f}C")
    if reading.humidity is not None:
        parts.append(f"hum={reading.humidity:.0f}%")
    if reading.co2 is not None:
        parts.append(f"co2={reading.co2}ppm")
    if reading.battery is not None:
        parts.append(f"bat={reading.battery}%")
    return ", ".join(parts) if parts else "no data"

# ---------------------------------------------------------------------
# Typer コマンド
# ---------------------------------------------------------------------

@app.command(help="SwitchBot の温湿度/CO2 を取得し、InfluxDB に1回だけ書き込みます。")
def push(
    mode: Annotated[Optional[str], typer.Option("--mode", help="データ取得モード: api|ble")] = None,
    use_v3_native: Annotated[bool, typer.Option("--use-v3-native", help="InfluxDB v3 ネイティブAPI (/api/v3/write_lp) を使用")] = False,
    bucket_or_db: Annotated[Optional[str], typer.Option("--bucket-or-db", help="v2: bucket名 / v3: DB名。未指定ならENVを使用")] = None,
    influx_url: Annotated[Optional[str], typer.Option("--influx-url", help="Influx URL（例: http://localhost:8086）")] = None,
    token_influx: Annotated[Optional[str], typer.Option("--influx-token", help="Influx API トークン")] = None,
    location_prefix: Annotated[Optional[str], typer.Option("--location-prefix", help="location タグの接頭辞")] = None,
    ef_model: Annotated[str, typer.Option("--ef-model", help="増強係数モデル: none|buck|its90")] = "none",
    timeout_s: Annotated[Optional[float], typer.Option("--timeout-s", help="HTTP タイムアウト（秒）")] = None,
    ble_device: Annotated[Optional[List[str]], typer.Option("--ble-device", help="BLE デバイス指定 (MAC[@type][=alias])", metavar="SPEC")] = None,
    ble_scan_timeout: Annotated[Optional[float], typer.Option("--ble-scan-timeout", help="BLE スキャンタイムアウト（秒）")] = None,
):
    env = _load_env()
    active_mode = (mode or env["SWITCHBOT_MODE"]).lower()
    if active_mode not in SUPPORTED_MODES:
        raise typer.BadParameter(f"--mode は {', '.join(sorted(SUPPORTED_MODES))} から選択してください")

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

    ble_specs = list(ble_device or [])
    if not ble_specs and env["SWITCHBOT_BLE_DEVICES"]:
        ble_specs = [s.strip() for s in env["SWITCHBOT_BLE_DEVICES"].split(",") if s.strip()]
    try:
        ble_targets = parse_ble_targets(ble_specs)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    ble_scan_timeout = ble_scan_timeout or env["SWITCHBOT_BLE_SCAN_TIMEOUT"]

    sb_token = env["SWITCHBOT_TOKEN"]
    sb_secret = env["SWITCHBOT_SECRET"]
    if active_mode == "api":
        _require(sb_token, "SWITCHBOT_TOKEN")
        _require(sb_secret, "SWITCHBOT_SECRET")
    else:
        sb_token = None
        sb_secret = None

    lines = _collect_once(
        location_prefix=location_prefix,
        mode=active_mode,
        timeout_s=timeout_s,
        ef_model=ef_model,
        token=sb_token,
        secret=sb_secret,
        ble_targets=ble_targets,
        ble_scan_timeout_s=ble_scan_timeout,
    )
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
        name = d.get("deviceName") or d.get("deviceId")
        dtype = d.get("deviceType")
        device_id = d.get("deviceId")
        typer.echo(f"- {name} (type={dtype}, id={device_id})")
        try:
            status = _get_status(device_id, sb_token, sb_secret, timeout_s) or {}
        except Exception as exc:
            typer.echo(f"    status error: {exc}")
            continue
        for key in sorted(status.keys()):
            typer.echo(f"    {key}: {status[key]}")

@app.command(name="scan-ble", help="BLE スキャンで周囲の SwitchBot デバイス MAC アドレスを取得します。")
def scan_ble(
    timeout_s: Annotated[Optional[float], typer.Option("--timeout-s", help="BLE スキャンタイムアウト（秒）")] = None,
):
    env = _load_env()
    scan_timeout = timeout_s or env["SWITCHBOT_BLE_SCAN_TIMEOUT"]
    try:
        devices = scan_switchbot_devices(scan_timeout)
    except RuntimeError as exc:
        typer.echo(f"error: {exc}")
        raise typer.Exit(code=1)

    if not devices:
        typer.echo("no BLE devices found")
        raise typer.Exit(code=0)

    for info in devices:
        name = info.get("name") or "-"
        source = "switchbot" if info.get("is_switchbot") else "other"
        line = f"{info.get('mac')} source={source} name={name}"
        dtype = info.get("device_type")
        if dtype:
            line += f" type={dtype}"
        elif info.get("is_switchbot"):
            line += " type=unknown"
        model = info.get("device_model")
        if model:
            line += f" model={model}"
        code = info.get("device_code")
        if code is not None:
            line += f" code=0x{code:02X}"
        rssi = info.get("rssi")
        if rssi is not None:
            line += f" rssi={rssi}"
        reading: Optional[SwitchBotReading] = info.get("reading")
        metrics: list[str] = []
        if reading:
            if reading.temperature is not None:
                metrics.append(f"temp={reading.temperature:.1f}C")
            if reading.humidity is not None:
                metrics.append(f"hum={reading.humidity:.0f}%")
            if reading.co2 is not None:
                metrics.append(f"co2={reading.co2}ppm")
            if reading.battery is not None:
                metrics.append(f"bat={reading.battery}%")
        if metrics:
            line += " (" + ", ".join(metrics) + ")"
        typer.echo(line)


@app.command(help="同一デバイスの Cloud API と BLE 値を比較表示します。")
def compare(
    pair: Annotated[List[str], typer.Option("--pair", "-p", help="deviceId=BLE_MAC[@type]")],
    timeout_s: Annotated[Optional[float], typer.Option("--timeout-s", help="API タイムアウト（秒）")] = None,
    ble_scan_timeout: Annotated[Optional[float], typer.Option("--ble-scan-timeout", help="BLE スキャンタイムアウト（秒）")] = None,
):
    if not pair:
        raise typer.BadParameter("--pair を少なくとも1件指定してください")

    env = _load_env()
    sb_token = env["SWITCHBOT_TOKEN"]
    sb_secret = env["SWITCHBOT_SECRET"]
    _require(sb_token, "SWITCHBOT_TOKEN")
    _require(sb_secret, "SWITCHBOT_SECRET")

    api_timeout = timeout_s or env["REQUEST_TIMEOUT_S"]
    ble_timeout = ble_scan_timeout or env["SWITCHBOT_BLE_SCAN_TIMEOUT"]

    devices = _get_devices(sb_token, sb_secret, api_timeout)
    device_map = {d.get("deviceId"): d for d in devices}

    targets: list[BleTarget] = []
    compare_entries: list[tuple[str, dict[str, Any], BleTarget]] = []

    for item in pair:
        if "=" not in item:
            raise typer.BadParameter(f"--pair 形式エラー: {item}")
        dev_id, ble_spec = item.split("=", 1)
        dev_id = dev_id.strip()
        ble_spec = ble_spec.strip()
        if not dev_id or not ble_spec:
            raise typer.BadParameter(f"--pair 形式エラー: {item}")
        info = device_map.get(dev_id)
        if not info:
            raise typer.BadParameter(f"deviceId {dev_id} は API デバイス一覧に存在しません")

        if "@" not in ble_spec:
            inferred = _guess_ble_type(info.get("deviceType", ""))
            ble_spec = f"{ble_spec}@{inferred}"

        spec_with_alias = f"{ble_spec}={dev_id}"
        try:
            target = parse_ble_target(spec_with_alias)
        except ValueError as exc:
            raise typer.BadParameter(str(exc)) from exc
        targets.append(target)
        compare_entries.append((dev_id, info, target))

    api_readings = _collect_api_readings(sb_token, sb_secret, api_timeout)
    api_map = {r.device_id: r for r in api_readings}

    ble_readings = collect_ble_readings(targets, ble_timeout)
    ble_map = {r.name: r for r in ble_readings}  # alias に deviceId を入れている

    for dev_id, info, target in compare_entries:
        name = info.get("deviceName") or dev_id
        typer.echo(f"{dev_id} ({name}) [{target.device_type}]")

        api_read = api_map.get(dev_id)
        typer.echo(f"  API: {_format_reading(api_read)}")

        ble_read = ble_map.get(dev_id)
        typer.echo(f"  BLE: {_format_reading(ble_read)}")

        diff_parts: list[str] = []
        if api_read and ble_read:
            if api_read.temperature is not None and ble_read.temperature is not None:
                diff_parts.append(f"Δtemp={(ble_read.temperature - api_read.temperature):+.2f}")
            if api_read.humidity is not None and ble_read.humidity is not None:
                diff_parts.append(f"Δhum={(ble_read.humidity - api_read.humidity):+.1f}")
            if api_read.co2 is not None and ble_read.co2 is not None:
                diff_parts.append(f"Δco2={(ble_read.co2 - api_read.co2):+d}")
            if api_read.battery is not None and ble_read.battery is not None:
                diff_parts.append(f"Δbat={(ble_read.battery - api_read.battery):+d}")
        if diff_parts:
            typer.echo(f"  Δ  : {', '.join(diff_parts)}")
        typer.echo("")

@app.command(help="指定間隔で push を繰り返します（cron 代替）。")
def run(
    interval: Annotated[int, typer.Option("--interval", "-i", help="実行間隔（秒）", min=30)] = 300,
    mode: Annotated[Optional[str], typer.Option("--mode", help="データ取得モード: api|ble")] = None,
    use_v3_native: Annotated[bool, typer.Option("--use-v3-native")] = False,
    bucket_or_db: Annotated[Optional[str], typer.Option("--bucket-or-db")] = None,
    influx_url: Annotated[Optional[str], typer.Option("--influx-url")] = None,
    token_influx: Annotated[Optional[str], typer.Option("--influx-token")] = None,
    location_prefix: Annotated[Optional[str], typer.Option("--location-prefix")] = None,
    ef_model: Annotated[str, typer.Option("--ef-model", help="増強係数モデル: none|buck|its90")] = "none",
    timeout_s: Annotated[Optional[float], typer.Option("--timeout-s")] = None,
    ble_device: Annotated[Optional[List[str]], typer.Option("--ble-device", help="BLE デバイス指定 (MAC[@type][=alias])", metavar="SPEC")] = None,
    ble_scan_timeout: Annotated[Optional[float], typer.Option("--ble-scan-timeout", help="BLE スキャンタイムアウト（秒）")] = None,
):
    env = _load_env()
    active_mode = (mode or env["SWITCHBOT_MODE"]).lower()
    if active_mode not in SUPPORTED_MODES:
        raise typer.BadParameter(f"--mode は {', '.join(sorted(SUPPORTED_MODES))} から選択してください")

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

    ble_specs = list(ble_device or [])
    if not ble_specs and env["SWITCHBOT_BLE_DEVICES"]:
        ble_specs = [s.strip() for s in env["SWITCHBOT_BLE_DEVICES"].split(",") if s.strip()]
    try:
        ble_targets = parse_ble_targets(ble_specs)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    ble_scan_timeout = ble_scan_timeout or env["SWITCHBOT_BLE_SCAN_TIMEOUT"]

    sb_token = env["SWITCHBOT_TOKEN"]
    sb_secret = env["SWITCHBOT_SECRET"]
    if active_mode == "api":
        _require(sb_token, "SWITCHBOT_TOKEN")
        _require(sb_secret, "SWITCHBOT_SECRET")
    else:
        sb_token = None
        sb_secret = None

    typer.echo(f"Starting loop: every {interval}s (Ctrl+C to stop)")
    try:
        while True:
            try:
                lines = _collect_once(
                    location_prefix=location_prefix,
                    mode=active_mode,
                    timeout_s=timeout_s,
                    ef_model=ef_model,
                    token=sb_token,
                    secret=sb_secret,
                    ble_targets=ble_targets,
                    ble_scan_timeout_s=ble_scan_timeout,
                )
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
