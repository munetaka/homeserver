# src/cli/sync_data.py
#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
SwitchBot BLE -> VictoriaMetrics Writer (Typer CLI)

機能:
- SwitchBot センサーの温湿度/CO2/電池を BLE アドバタイズから直接取得
- 絶対湿度 (abs_humidity, g/m^3) を「岡田の式(液水, -30..50℃) + 増強係数 f(T,P)」で算出
  - -30℃未満は安全側で Goff–Gratch(氷) にフォールバック
  - f(T,P) は EF_MODEL=none/buck/its90 で切替（既定 none）
- InfluxDB line protocol (/api/v2/write 互換) でバッチ書き込み。宛先は VictoriaMetrics
  (line protocol を受ける他のDBにもそのまま書ける)

かつて存在した SwitchBot Cloud API モードと InfluxDB v2/v3 サポートは
2026-07-16 に撤去した (VictoriaMetrics + BLE 運用では未使用のため。必要なら git 履歴参照)。

ENV (.env 推奨):
  INFLUX_URL              (任意, 既定 "http://localhost:8428")
  LOCATION_PREFIX         (任意, 既定 "")
  REQUEST_TIMEOUT_S       (任意, 既定 "10")
  EF_MODEL                ("none"|"buck"|"its90", 既定 "none")
  SWITCHBOT_BLE_DEVICES   (例: "AA:BB:CC:DD:EE:FF@meter=Living,11:22:33:44:55:66@co2=Office")
  SWITCHBOT_BLE_SCAN_TIMEOUT (任意, 既定 "5")
"""

from __future__ import annotations

import asyncio
import math
import os
import time
from typing import Annotated, Dict, List, Optional

import requests
import typer
from dotenv import load_dotenv

from .switchbot_ble import (
    BleTarget,
    SwitchBotReading,
    collect_ble_readings,
    collect_ble_readings_async,
    parse_ble_targets,
    scan_switchbot_devices,
)

app = typer.Typer(add_completion=False, help="Home sensors: SwitchBot BLE -> VictoriaMetrics writer")

# ---------------------------------------------------------------------
# 設定/定数
# ---------------------------------------------------------------------

# 岡田の式（液水, -30..50℃）の係数（log10(es[hPa]) = a0 + a1*T + a2*T^2 + a3*T^3 + a4*T^4）
_OKADA_WATER_COEFF = {
    "a0": 1.809378,
    "a1": 0.07266115,
    "a2": -3.003879e-4,
    "a3": 1.181765e-6,
    "a4": -3.863083e-9,
}

# run ループがこの回数連続で失敗したら異常終了する。
# systemd 側の Restart=on-failure に再起動させ、BLE/D-Bus 接続の
# 壊れたプロセスがエラーを吐き続けたまま生き残るのを防ぐ。
MAX_CONSECUTIVE_ERRORS = 5


def _load_env():
    load_dotenv()
    env = {
        "INFLUX_URL": os.getenv("INFLUX_URL", "http://localhost:8428"),
        "LOCATION_PREFIX": os.getenv("LOCATION_PREFIX", ""),
        "REQUEST_TIMEOUT_S": float(os.getenv("REQUEST_TIMEOUT_S", "10")),
        "EF_MODEL": os.getenv("EF_MODEL", "none"),
        "SWITCHBOT_BLE_DEVICES": os.getenv("SWITCHBOT_BLE_DEVICES", ""),
        "SWITCHBOT_BLE_SCAN_TIMEOUT": float(os.getenv("SWITCHBOT_BLE_SCAN_TIMEOUT", "5")),
    }
    return env

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
# Line Protocol / 書き込み
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

def _write_influx(lines: List[str], influx_url: str, timeout_s: float):
    r = requests.post(
        f"{influx_url}/api/v2/write",
        params={"precision": "ns"},
        data="\n".join(lines).encode("utf-8"),
        headers={"Content-Type": "text/plain"},
        timeout=timeout_s,
    )
    r.raise_for_status()

# ---------------------------------------------------------------------
# データ収集
# ---------------------------------------------------------------------

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


def _readings_to_lines(
    readings: List[SwitchBotReading],
    location_prefix: str,
    ef_model: str,
    ts_ms: int,
) -> List[str]:
    lines: List[str] = []
    for reading in readings:
        line = _reading_to_line(reading, location_prefix, ef_model, ts_ms)
        if line:
            lines.append(line)
    return lines


def _collect_once(
    location_prefix: str,
    ef_model: str,
    ble_targets: Optional[List[BleTarget]] = None,
    ble_scan_timeout_s: float = 5.0,
) -> list[str]:
    """1回スキャンして line protocol 行に変換する (one-shot 用)。"""
    ts_ms = int(time.time() * 1000)
    readings = collect_ble_readings(ble_targets or [], ble_scan_timeout_s)
    return _readings_to_lines(readings, location_prefix, ef_model, ts_ms)


async def _collect_once_async(
    location_prefix: str,
    ef_model: str,
    ble_targets: Optional[List[BleTarget]] = None,
    ble_scan_timeout_s: float = 5.0,
) -> list[str]:
    """_collect_once の async 版。呼び出し元のイベントループ上でBLEスキャンする。"""
    ts_ms = int(time.time() * 1000)
    readings = await collect_ble_readings_async(ble_targets or [], ble_scan_timeout_s)
    return _readings_to_lines(readings, location_prefix, ef_model, ts_ms)


async def _run_loop_async(
    interval: int,
    location_prefix: str,
    ef_model: str,
    ble_targets: Optional[List[BleTarget]],
    ble_scan_timeout_s: float,
    influx_url: str,
    timeout_s: float,
) -> None:
    """`run` の常駐ループ。プロセスの生存期間中この1つのイベントループを使い続ける。"""
    consecutive_errors = 0
    while True:
        try:
            lines = await _collect_once_async(
                location_prefix=location_prefix,
                ef_model=ef_model,
                ble_targets=ble_targets,
                ble_scan_timeout_s=ble_scan_timeout_s,
            )
            if lines:
                _write_influx(lines, influx_url, timeout_s)
                typer.echo(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] wrote {len(lines)} points")
            else:
                typer.echo(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] no datapoints")
            consecutive_errors = 0
        except Exception as e:
            consecutive_errors += 1
            typer.echo(f"error: {e}")
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                typer.echo(
                    f"{consecutive_errors} consecutive errors; exiting so systemd can restart the service"
                )
                raise typer.Exit(code=1)
        await asyncio.sleep(interval)


def _resolve_ble_targets(env: dict, ble_device: Optional[List[str]]) -> list[BleTarget]:
    ble_specs = list(ble_device or [])
    if not ble_specs and env["SWITCHBOT_BLE_DEVICES"]:
        ble_specs = [s.strip() for s in env["SWITCHBOT_BLE_DEVICES"].split(",") if s.strip()]
    try:
        return parse_ble_targets(ble_specs)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc

# ---------------------------------------------------------------------
# Typer コマンド
# ---------------------------------------------------------------------

@app.command(help="SwitchBot の温湿度/CO2 を BLE で取得し、1回だけ書き込みます。")
def push(
    influx_url: Annotated[Optional[str], typer.Option("--influx-url", help="書き込み先URL（例: http://localhost:8428）")] = None,
    location_prefix: Annotated[Optional[str], typer.Option("--location-prefix", help="location タグの接頭辞")] = None,
    ef_model: Annotated[str, typer.Option("--ef-model", help="増強係数モデル: none|buck|its90")] = "none",
    timeout_s: Annotated[Optional[float], typer.Option("--timeout-s", help="HTTP タイムアウト（秒）")] = None,
    ble_device: Annotated[Optional[List[str]], typer.Option("--ble-device", help="BLE デバイス指定 (MAC[@type][=alias])", metavar="SPEC")] = None,
    ble_scan_timeout: Annotated[Optional[float], typer.Option("--ble-scan-timeout", help="BLE スキャンタイムアウト（秒）")] = None,
):
    env = _load_env()
    influx_url = influx_url or env["INFLUX_URL"]
    location_prefix = location_prefix or env["LOCATION_PREFIX"]
    timeout_s = timeout_s or env["REQUEST_TIMEOUT_S"]
    if ef_model == "none":
        ef_model = env["EF_MODEL"]
    ble_targets = _resolve_ble_targets(env, ble_device)
    ble_scan_timeout = ble_scan_timeout or env["SWITCHBOT_BLE_SCAN_TIMEOUT"]

    lines = _collect_once(
        location_prefix=location_prefix,
        ef_model=ef_model,
        ble_targets=ble_targets,
        ble_scan_timeout_s=ble_scan_timeout,
    )
    if not lines:
        typer.echo("no datapoints")
        raise typer.Exit(code=0)

    _write_influx(lines, influx_url, timeout_s)
    typer.echo(f"wrote {len(lines)} points")


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


@app.command(help="指定間隔で push を繰り返します（systemd 常駐用）。")
def run(
    interval: Annotated[int, typer.Option("--interval", "-i", help="実行間隔（秒）", min=30)] = 300,
    influx_url: Annotated[Optional[str], typer.Option("--influx-url")] = None,
    location_prefix: Annotated[Optional[str], typer.Option("--location-prefix")] = None,
    ef_model: Annotated[str, typer.Option("--ef-model", help="増強係数モデル: none|buck|its90")] = "none",
    timeout_s: Annotated[Optional[float], typer.Option("--timeout-s")] = None,
    ble_device: Annotated[Optional[List[str]], typer.Option("--ble-device", help="BLE デバイス指定 (MAC[@type][=alias])", metavar="SPEC")] = None,
    ble_scan_timeout: Annotated[Optional[float], typer.Option("--ble-scan-timeout", help="BLE スキャンタイムアウト（秒）")] = None,
):
    env = _load_env()
    influx_url = influx_url or env["INFLUX_URL"]
    location_prefix = location_prefix or env["LOCATION_PREFIX"]
    timeout_s = timeout_s or env["REQUEST_TIMEOUT_S"]
    if ef_model == "none":
        ef_model = env["EF_MODEL"]
    ble_targets = _resolve_ble_targets(env, ble_device)
    ble_scan_timeout = ble_scan_timeout or env["SWITCHBOT_BLE_SCAN_TIMEOUT"]

    typer.echo(f"Starting loop: every {interval}s (Ctrl+C to stop)")
    # ループ全体を1つのイベントループで回す。bleak はイベントループごとに
    # D-Bus 接続を張るため、サイクルごとに asyncio.run() すると接続がリークし、
    # dbus-daemon の UID あたり256接続の上限で BLE スキャンが全滅する。
    try:
        asyncio.run(
            _run_loop_async(
                interval=interval,
                location_prefix=location_prefix,
                ef_model=ef_model,
                ble_targets=ble_targets,
                ble_scan_timeout_s=ble_scan_timeout,
                influx_url=influx_url,
                timeout_s=timeout_s,
            )
        )
    except KeyboardInterrupt:
        typer.echo("stopped")

if __name__ == "__main__":
    app()
