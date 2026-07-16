"""
ECHONET Lite クライアントと家庭内電力機器の収集 CLI。

対応機器 (実機 = Panasonic エネルギー計測ユニット MKN7350S1 / ダイキン機器):
- solar      (0x0279 住宅用太陽光発電): 瞬時発電電力, 積算発電量
- powerboard (0x0287 分電盤メータリング): 主幹瞬時電力(符号付き, 負=売電),
  積算買電/売電量, 回路別瞬時電力 (EPC 0xB7)
- aircon     (0x0130 家庭用エアコン): 運転状態, 消費電力, 室温, 外気温, 設定温度
- ecocute    (0x026B 電気給湯器): 運転状態, 消費電力, 残湯量

応答は送信元ポートではなく UDP 3610 宛てに返る実装が多いため、
ソケットは必ず 3610 に bind する。
"""

from __future__ import annotations

import os
import socket
import time
from dataclasses import dataclass
from typing import Annotated, Dict, Iterable, List, Optional, Tuple

import requests
import typer
from dotenv import load_dotenv

app = typer.Typer(add_completion=False, help="Home power: ECHONET Lite -> InfluxDB/VictoriaMetrics writer")

PORT = 3610
MULTICAST = "224.0.23.0"
CONTROLLER_EOJ = bytes([0x05, 0xFF, 0x01])
NODE_PROFILE_EOJ = bytes([0x0E, 0xF0, 0x01])

ESV_GET = 0x62
ESV_GET_RES = 0x72
ESV_GET_SNA = 0x52  # 一部プロパティのみ応答 (失敗した EPC は PDC=0)

DEVICE_EOJ = {
    "solar": bytes([0x02, 0x79, 0x01]),
    "powerboard": bytes([0x02, 0x87, 0x01]),
    "aircon": bytes([0x01, 0x30, 0x01]),
    "ecocute": bytes([0x02, 0x6B, 0x01]),
}

# 積算電力量の単位コード (EPC 0xC2) -> kWh 係数
CUMULATIVE_UNIT = {
    0x00: 1.0, 0x01: 0.1, 0x02: 0.01, 0x03: 0.001, 0x04: 0.0001,
    0x0A: 10.0, 0x0B: 100.0, 0x0C: 1000.0, 0x0D: 10000.0,
}

MAX_CONSECUTIVE_ERRORS = 5


@dataclass(frozen=True)
class ElTarget:
    """収集対象の ECHONET Lite 機器。"""

    ip: str
    device_type: str
    alias: str


@dataclass
class Reading:
    """1機器分の計測値。fields は line protocol にそのまま乗る。"""

    measurement: str
    tags: Dict[str, str]
    fields: Dict[str, float | int]


def parse_el_target(spec: str) -> ElTarget:
    """``IP@type[=alias]`` 形式の指定をパースする (SWITCHBOT_BLE_DEVICES と同じ流儀)。"""
    if not spec:
        raise ValueError("Empty ECHONET target specification")
    alias = None
    rest = spec
    if "=" in spec:
        rest, alias = spec.split("=", 1)
        alias = alias.strip() or None
    if "@" not in rest:
        raise ValueError(f"Missing @type in '{spec}'")
    ip, dtype = rest.split("@", 1)
    ip = ip.strip()
    dtype = dtype.strip().lower()
    if dtype not in DEVICE_EOJ:
        raise ValueError(f"Unsupported device type '{dtype}' in '{spec}'")
    try:
        socket.inet_aton(ip)
    except OSError as exc:
        raise ValueError(f"Invalid IP address '{ip}' in '{spec}'") from exc
    return ElTarget(ip=ip, device_type=dtype, alias=alias or ip)


def parse_el_targets(values: Iterable[str]) -> list[ElTarget]:
    targets = []
    for raw in values:
        spec = raw.strip()
        if spec:
            targets.append(parse_el_target(spec))
    return targets


def build_get_frame(tid: int, deoj: bytes, epcs: Iterable[int]) -> bytes:
    epcs = list(epcs)
    return (
        bytes([0x10, 0x81, (tid >> 8) & 0xFF, tid & 0xFF])
        + CONTROLLER_EOJ
        + deoj
        + bytes([ESV_GET, len(epcs)])
        + b"".join(bytes([e, 0x00]) for e in epcs)
    )


def parse_response(data: bytes) -> Optional[Tuple[int, bytes, int, Dict[int, bytes]]]:
    """フレームを (tid, seoj, esv, {epc: value}) にパースする。不正なら None。"""
    if len(data) < 12 or data[0] != 0x10 or data[1] != 0x81:
        return None
    tid = (data[2] << 8) | data[3]
    seoj = data[4:7]
    esv = data[10]
    opc = data[11]
    props: Dict[int, bytes] = {}
    i = 12
    for _ in range(opc):
        if i + 2 > len(data):
            return None
        epc, pdc = data[i], data[i + 1]
        value = data[i + 2:i + 2 + pdc]
        if len(value) != pdc:
            return None
        props[epc] = value
        i += 2 + pdc
    return tid, seoj, esv, props


def decode_property_map(value: bytes) -> list[int]:
    """プロパティマップ (EPC 0x9F 等) を EPC のリストへ。16個以上はビットマップ形式。"""
    if not value:
        return []
    count = value[0]
    if count < 16:
        return sorted(value[1:1 + count])
    epcs = []
    for y in range(16):
        if 1 + y >= len(value):
            break
        b = value[1 + y]
        for bit in range(8):
            if b & (1 << bit):
                epcs.append(0x80 + bit * 0x10 + y)
    return sorted(epcs)


def decode_circuit_list(value: bytes) -> Dict[int, int]:
    """分電盤 EPC 0xB7 (回路別瞬時電力リスト) をパースする。

    形式: 開始チャンネル(1B) + チャンネル数(1B) + 符号付き4B × チャンネル数。
    計測不能マーカー (0x7FFFFFFD 以上) は除外する。
    """
    if len(value) < 2:
        return {}
    start, count = value[0], value[1]
    circuits: Dict[int, int] = {}
    for i in range(count):
        chunk = value[2 + i * 4:6 + i * 4]
        if len(chunk) < 4:
            break
        watts = int.from_bytes(chunk, "big", signed=True)
        if abs(watts) >= 0x7FFFFFFD:
            continue
        circuits[start + i] = watts
    return circuits


def _signed_byte_temp(value: bytes) -> Optional[float]:
    """室温/外気温 (符号付き1バイト)。0x7E(計測不能) 等の範囲外は None。"""
    if len(value) != 1:
        return None
    v = int.from_bytes(value, "big", signed=True)
    if -50 <= v <= 60:
        return float(v)
    return None


def _setpoint_temp(value: bytes) -> Optional[float]:
    """設定温度 (符号なし1バイト, 0〜50℃)。0xFD(自動運転時の未定義) 等は None。

    符号付きで読むと 0xFD が -3℃ という妥当な温度に化けるので、必ず符号なしで扱う。
    """
    if len(value) != 1:
        return None
    v = value[0]
    if v <= 50:
        return float(v)
    return None


class EchonetClient:
    """UDP 3610 を使い回す同期クライアント。run ループでは1つを使い続ける。"""

    def __init__(self, timeout_s: float = 3.0, retries: int = 1):
        self.timeout_s = timeout_s
        self.retries = retries
        self._tid = int(time.time()) & 0x7FFF
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", PORT))
        self.sock.settimeout(self.timeout_s)

    def close(self) -> None:
        self.sock.close()

    def get(self, ip: str, deoj: bytes, epcs: list[int]) -> Dict[int, bytes]:
        """GET を送り、成功したプロパティだけ返す。全滅なら RuntimeError。"""
        last_error: Optional[Exception] = None
        for _ in range(self.retries + 1):
            self._tid = (self._tid + 1) & 0xFFFF
            frame = build_get_frame(self._tid, deoj, epcs)
            try:
                self.sock.sendto(frame, (ip, PORT))
                deadline = time.time() + self.timeout_s
                while time.time() < deadline:
                    try:
                        data, addr = self.sock.recvfrom(4096)
                    except socket.timeout:
                        break
                    parsed = parse_response(data)
                    if parsed is None:
                        continue
                    tid, seoj, esv, props = parsed
                    if addr[0] != ip or tid != self._tid or seoj != deoj:
                        continue  # 他機器の自発通知などは無視
                    if esv not in (ESV_GET_RES, ESV_GET_SNA):
                        continue
                    return {epc: v for epc, v in props.items() if v}
            except OSError as exc:
                last_error = exc
        raise RuntimeError(f"no response from {ip} (eoj={deoj.hex()}): {last_error or 'timeout'}")


# ---------------------------------------------------------------------
# 機器別リーダー: ElTarget -> list[Reading]
# ---------------------------------------------------------------------

def read_solar(client: EchonetClient, target: ElTarget) -> List[Reading]:
    props = client.get(target.ip, DEVICE_EOJ["solar"], [0xE0, 0xE1])
    fields: Dict[str, float | int] = {}
    if 0xE0 in props and len(props[0xE0]) >= 2:
        fields["generation_w"] = int.from_bytes(props[0xE0], "big")
    if 0xE1 in props and len(props[0xE1]) >= 4:
        fields["generation_total_kwh"] = int.from_bytes(props[0xE1], "big") * 0.001
    if not fields:
        return []
    return [Reading("power", {"location": target.alias, "type": "solar"}, fields)]


def read_powerboard(client: EchonetClient, target: ElTarget) -> List[Reading]:
    props = client.get(target.ip, DEVICE_EOJ["powerboard"], [0xC2, 0xC6, 0xC0, 0xC1, 0xB7])
    readings: List[Reading] = []
    unit = CUMULATIVE_UNIT.get(props.get(0xC2, b"\x00")[0] if props.get(0xC2) else 0x00, 1.0)
    fields: Dict[str, float | int] = {}
    if 0xC6 in props and len(props[0xC6]) == 4:
        fields["grid_w"] = int.from_bytes(props[0xC6], "big", signed=True)  # 正=買電, 負=売電
    if 0xC0 in props and len(props[0xC0]) == 4:
        fields["buy_total_kwh"] = int.from_bytes(props[0xC0], "big") * unit
    if 0xC1 in props and len(props[0xC1]) == 4:
        fields["sell_total_kwh"] = int.from_bytes(props[0xC1], "big") * unit
    if fields:
        readings.append(Reading("power", {"location": target.alias, "type": "powerboard"}, fields))
    if 0xB7 in props:
        for ch, watts in decode_circuit_list(props[0xB7]).items():
            readings.append(
                Reading(
                    "power_circuit",
                    {"location": target.alias, "circuit": f"{ch:02d}"},
                    {"watts": watts},
                )
            )
    return readings


def read_aircon(client: EchonetClient, target: ElTarget) -> List[Reading]:
    props = client.get(target.ip, DEVICE_EOJ["aircon"], [0x80, 0x84, 0xB3, 0xBB, 0xBE])
    fields: Dict[str, float | int] = {}
    if 0x80 in props:
        fields["on"] = 1 if props[0x80] == b"\x30" else 0
    if 0x84 in props and len(props[0x84]) == 2:
        fields["power_w"] = int.from_bytes(props[0x84], "big")
    for epc, name, decode in (
        (0xBB, "room_temp", _signed_byte_temp),
        (0xBE, "outdoor_temp", _signed_byte_temp),
        (0xB3, "setpoint", _setpoint_temp),
    ):
        if epc in props:
            v = decode(props[epc])
            if v is not None:
                fields[name] = v
    if not fields:
        return []
    return [Reading("appliance", {"location": target.alias, "type": "aircon"}, fields)]


def read_ecocute(client: EchonetClient, target: ElTarget) -> List[Reading]:
    props = client.get(target.ip, DEVICE_EOJ["ecocute"], [0x80, 0x84, 0xE1])
    fields: Dict[str, float | int] = {}
    if 0x80 in props:
        fields["on"] = 1 if props[0x80] == b"\x30" else 0
    if 0x84 in props and len(props[0x84]) == 2:
        fields["power_w"] = int.from_bytes(props[0x84], "big")
    if 0xE1 in props and len(props[0xE1]) == 2:
        fields["tank_l"] = int.from_bytes(props[0xE1], "big")
    if not fields:
        return []
    return [Reading("appliance", {"location": target.alias, "type": "ecocute"}, fields)]


READERS = {
    "solar": read_solar,
    "powerboard": read_powerboard,
    "aircon": read_aircon,
    "ecocute": read_ecocute,
}


def collect_readings(client: EchonetClient, targets: list[ElTarget]) -> Tuple[List[Reading], List[str]]:
    """全ターゲットを読む。個別故障はスキップしてエラー文字列で返す。"""
    readings: List[Reading] = []
    errors: List[str] = []
    for target in targets:
        try:
            readings.extend(READERS[target.device_type](client, target))
        except Exception as exc:
            errors.append(f"{target.alias} ({target.ip}): {exc}")
    return readings, errors


# ---------------------------------------------------------------------
# AiSEG2 履歴CSV (rireki_*.zip) のインポート
# ---------------------------------------------------------------------

# 30minhistory_rc / dayhistory_rc のサマリー列 -> kind タグ
HISTORY_SUMMARY_COLUMNS = {
    "太陽光発電(PV1)": "generation",
    "主幹買電": "buy",
    "主幹売電": "sell",
    "使用電力量": "consumption",
}


def parse_history_header(header: List[str]) -> Tuple[Dict[int, str], Dict[int, Tuple[int, str]]]:
    """履歴CSVのヘッダーから (サマリー列 idx->kind, 回路列 idx->(回路番号, 名称)) を得る。

    回路列は「無効8」と「無効9」に挟まれた28列で、並び順が分1〜分28に対応する。
    同名回路 (台所コンセント×2 等) は2つ目以降に連番を付け、ライブ収集の命名と揃える。
    """
    summary = {i: HISTORY_SUMMARY_COLUMNS[h] for i, h in enumerate(header) if h in HISTORY_SUMMARY_COLUMNS}
    i8, i9 = header.index("無効8"), header.index("無効9")
    circuits: Dict[int, Tuple[int, str]] = {}
    seen: Dict[str, int] = {}
    for ch, idx in enumerate(range(i8 + 1, i9), start=1):
        name = header[idx].replace(" ", "").replace("・", "・")
        seen[name] = seen.get(name, 0) + 1
        if seen[name] > 1:
            name = f"{name}{seen[name]}"
        circuits[idx] = (ch, name)
    return summary, circuits


def parse_history_timestamp(value: str) -> int:
    """計測日時 (``202607120030+0900`` または日単位の ``20250616``) を epoch ms へ。"""
    from datetime import datetime, timedelta, timezone

    value = value.strip()
    if len(value) == 8:  # 日単位: JST の日付のみ
        dt = datetime.strptime(value, "%Y%m%d").replace(tzinfo=timezone(timedelta(hours=9)))
    else:
        dt = datetime.strptime(value, "%Y%m%d%H%M%z")
    return int(dt.timestamp() * 1000)


def history_row_to_readings(
    row: List[str],
    period: str,
    summary: Dict[int, str],
    circuits: Dict[int, Tuple[int, str]],
    exclude: set[int],
) -> List[Reading]:
    """履歴CSVの1行を Reading 群へ (値は Wh -> kWh)。'-' は欠測としてスキップ。

    period は "30min" | "day"。メトリクス名は energy_30min_kwh / energy_day_kwh 等になる。
    """
    readings: List[Reading] = []
    for idx, kind in summary.items():
        if idx < len(row) and row[idx] not in ("-", ""):
            readings.append(
                Reading(f"energy_{period}", {"kind": kind}, {"kwh": int(row[idx]) / 1000.0})
            )
    for idx, (ch, name) in circuits.items():
        if ch in exclude:
            continue
        if idx < len(row) and row[idx] not in ("-", ""):
            readings.append(
                Reading(f"energy_{period}_circuit", {"circuit": f"{ch:02d}", "name": name},
                        {"kwh": int(row[idx]) / 1000.0})
            )
    return readings


def parse_circuit_names(spec: str) -> Dict[int, str]:
    """``1=リビング,2=玄関ホール`` 形式の回路名設定をパースする。"""
    names: Dict[int, str] = {}
    for item in spec.split(","):
        item = item.strip()
        if not item:
            continue
        if "=" not in item:
            raise ValueError(f"Invalid circuit name entry '{item}' (expected N=名称)")
        ch, name = item.split("=", 1)
        names[int(ch.strip())] = name.strip()
    return names


def apply_circuit_config(
    readings: List[Reading],
    names: Dict[int, str],
    exclude: set[int],
) -> List[Reading]:
    """回路別 Reading に名称タグを付け、除外指定 (未使用回路) を落とす。"""
    result: List[Reading] = []
    for r in readings:
        if r.measurement == "power_circuit":
            ch = int(r.tags.get("circuit", "0"))
            if ch in exclude:
                continue
            if ch in names:
                r.tags["name"] = names[ch]
        result.append(r)
    return result


# ---------------------------------------------------------------------
# line protocol / 出力
# ---------------------------------------------------------------------

def _esc(s: str) -> str:
    return str(s).replace(" ", r"\ ").replace(",", r"\,")


def readings_to_lines(readings: List[Reading], location_prefix: str, ts_ms: int) -> List[str]:
    lines = []
    ts_ns = ts_ms * 1_000_000
    for r in readings:
        tags = dict(r.tags)
        if "location" in tags:
            tags["location"] = f"{location_prefix}{tags['location']}"
        tag_str = ",".join(f"{_esc(k)}={_esc(v)}" for k, v in tags.items())
        fparts = []
        for k, v in r.fields.items():
            if isinstance(v, int):
                fparts.append(f"{k}={v}i")
            else:
                fparts.append(f"{k}={float(v)}")
        lines.append(f"{r.measurement},{tag_str} {','.join(fparts)} {ts_ns}")
    return lines


def _load_env() -> dict:
    load_dotenv()
    return {
        "INFLUX_URL": os.getenv("INFLUX_URL", "http://localhost:8428"),
        "LOCATION_PREFIX": os.getenv("LOCATION_PREFIX", ""),
        "REQUEST_TIMEOUT_S": float(os.getenv("REQUEST_TIMEOUT_S", "10")),
        "ECHONET_DEVICES": os.getenv("ECHONET_DEVICES", ""),
        "ECHONET_TIMEOUT_S": float(os.getenv("ECHONET_TIMEOUT_S", "3")),
        "ECHONET_CIRCUIT_NAMES": os.getenv("ECHONET_CIRCUIT_NAMES", ""),
        "ECHONET_CIRCUIT_EXCLUDE": os.getenv("ECHONET_CIRCUIT_EXCLUDE", ""),
    }


def _circuit_config(env: dict) -> Tuple[Dict[int, str], set[int]]:
    try:
        names = parse_circuit_names(env["ECHONET_CIRCUIT_NAMES"])
        exclude = {int(s) for s in env["ECHONET_CIRCUIT_EXCLUDE"].split(",") if s.strip()}
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    return names, exclude


def _write_influx(lines: List[str], env: dict) -> None:
    r = requests.post(
        f"{env['INFLUX_URL']}/api/v2/write",
        params={"precision": "ns"},
        data="\n".join(lines).encode("utf-8"),
        headers={"Content-Type": "text/plain"},
        timeout=env["REQUEST_TIMEOUT_S"],
    )
    r.raise_for_status()


def _env_targets(env: dict) -> list[ElTarget]:
    specs = [s for s in env["ECHONET_DEVICES"].split(",") if s.strip()]
    try:
        targets = parse_el_targets(specs)
    except ValueError as exc:
        raise typer.BadParameter(str(exc)) from exc
    if not targets:
        raise typer.BadParameter("ECHONET_DEVICES が未設定です (例: 192.168.11.10@solar=太陽光,...)")
    return targets


# ---------------------------------------------------------------------
# Typer コマンド
# ---------------------------------------------------------------------

@app.command(help="LAN 上の ECHONET Lite ノードを発見して一覧表示します。")
def scan(
    subnet: Annotated[str, typer.Option("--subnet", help="スイープ対象 (例: 192.168.11)")] = "",
    timeout_s: Annotated[float, typer.Option("--timeout-s")] = 5.0,
):
    client = EchonetClient(timeout_s=timeout_s)
    frame = build_get_frame(1, NODE_PROFILE_EOJ, [0xD6])
    client.sock.sendto(frame, (MULTICAST, PORT))
    if subnet:
        for last in range(1, 255):
            try:
                client.sock.sendto(frame, (f"{subnet}.{last}", PORT))
            except OSError:
                pass
    deadline = time.time() + timeout_s
    seen: Dict[str, list[str]] = {}
    while time.time() < deadline:
        try:
            data, addr = client.sock.recvfrom(2048)
        except socket.timeout:
            break
        parsed = parse_response(data)
        if not parsed:
            continue
        _, seoj, esv, props = parsed
        if esv not in (ESV_GET_RES, ESV_GET_SNA) or 0xD6 not in props:
            continue
        payload = props[0xD6]
        names = []
        for i in range(payload[0] if payload else 0):
            eoj = payload[1 + i * 3:4 + i * 3]
            if len(eoj) == 3:
                names.append(f"eoj={eoj.hex()}")
        seen[addr[0]] = names
    client.close()
    for ip, names in sorted(seen.items()):
        typer.echo(f"{ip}: {', '.join(names)}")
    if not seen:
        typer.echo("no ECHONET Lite nodes found")


@app.command(help="全対象機器を1回読み取り、InfluxDB/VictoriaMetrics に書き込みます。")
def push():
    env = _load_env()
    targets = _env_targets(env)
    names, exclude = _circuit_config(env)
    client = EchonetClient(timeout_s=env["ECHONET_TIMEOUT_S"])
    try:
        readings, errors = collect_readings(client, targets)
    finally:
        client.close()
    readings = apply_circuit_config(readings, names, exclude)
    for e in errors:
        typer.echo(f"warn: {e}")
    lines = readings_to_lines(readings, env["LOCATION_PREFIX"], int(time.time() * 1000))
    if not lines:
        typer.echo("no datapoints")
        raise typer.Exit(code=1)
    _write_influx(lines, env)
    typer.echo(f"wrote {len(lines)} points")


@app.command(name="import-history", help="AiSEG2 の履歴CSV (rireki_* を展開したディレクトリ) を一括インポートします。")
def import_history(
    directory: Annotated[str, typer.Argument(help="rireki を展開したディレクトリ")],
    max_day: Annotated[str, typer.Option("--max-day", help="日単位データはこの日付(YYYYMMDD)まで取り込む(ライブ収集との二重計上防止)")] = "",
    dry_run: Annotated[bool, typer.Option("--dry-run", help="書き込まずに件数だけ表示")] = False,
    batch_size: Annotated[int, typer.Option("--batch-size")] = 5000,
):
    import glob as globmod

    env = _load_env()
    _, exclude = _circuit_config(env)
    prefix = env["LOCATION_PREFIX"]

    total = 0
    pending: List[str] = []

    def flush():
        nonlocal pending, total
        if pending and not dry_run:
            _write_influx(pending, env)
        total += len(pending)
        pending = []

    for pattern, period in (("30minhistory_rc_*.csv", "30min"), ("dayhistory_rc_*.csv", "day")):
        for path in sorted(globmod.glob(os.path.join(directory, pattern))):
            text = open(path, "rb").read().decode("utf-8-sig")
            lines = text.splitlines()
            if not lines:
                continue
            summary, circuits = parse_history_header(lines[0].split(","))
            for raw in lines[1:]:
                row = raw.split(",")
                if not row or row[0] in ("-", ""):
                    continue
                if period == "day" and max_day and row[0][:8] > max_day:
                    continue
                ts_ms = parse_history_timestamp(row[0])
                if period == "30min":
                    # 30分値は区間終端で記録 (ラインが「その時刻までの実績」と読める)
                    ts_ms += 1800 * 1000
                # 日次は「その日の00:00」のまま記録する。ツールチップの日付=その日の
                # 合計になり、パネル側は右寄せバー + increase[1d] offset -1d で揃える
                readings = history_row_to_readings(row, period, summary, circuits, exclude)
                pending.extend(readings_to_lines(readings, prefix, ts_ms))
                if len(pending) >= batch_size:
                    flush()
    flush()
    typer.echo(f"{'(dry-run) ' if dry_run else ''}imported {total} points")


@app.command(name="cost-update", help="日次kWhを電気料金(円)に換算して書き込みます (tariff.py の料金モデル)。")
def cost_update(
    since: Annotated[str, typer.Option("--since", help="この日付(YYYY-MM-DD)から再計算。省略時は40日前から")] = "",
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
):
    from datetime import date, timedelta

    from .cost import build_cost_lines, fetch_daily_kwh

    env = _load_env()
    today = date.today()  # Pi は JST 運用
    start = date.fromisoformat(since) if since else today - timedelta(days=40)
    daily = fetch_daily_kwh(env["INFLUX_URL"], start, today, env["REQUEST_TIMEOUT_S"])
    lines, warnings = build_cost_lines(daily, today)
    for w in warnings:
        typer.echo(f"warn: {w}")
    if not lines:
        typer.echo("no cost datapoints")
        raise typer.Exit(code=1)
    if dry_run:
        typer.echo(f"(dry-run) {len(lines)} points")
        return
    _write_influx(lines, env)
    typer.echo(f"wrote {len(lines)} cost points ({start}〜{today})")


@app.command(help="指定間隔で読み取りを繰り返します (systemd 常駐用)。")
def run(
    interval: Annotated[int, typer.Option("--interval", "-i", min=10)] = 60,
):
    env = _load_env()
    targets = _env_targets(env)
    names, exclude = _circuit_config(env)
    client = EchonetClient(timeout_s=env["ECHONET_TIMEOUT_S"])
    typer.echo(f"Starting loop: every {interval}s, {len(targets)} devices (Ctrl+C to stop)")
    consecutive_errors = 0
    try:
        while True:
            try:
                readings, errors = collect_readings(client, targets)
                readings = apply_circuit_config(readings, names, exclude)
                for e in errors:
                    typer.echo(f"warn: {e}")
                lines = readings_to_lines(readings, env["LOCATION_PREFIX"], int(time.time() * 1000))
                if lines:
                    _write_influx(lines, env)
                    typer.echo(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] wrote {len(lines)} points")
                    consecutive_errors = 0
                else:
                    # 全機器から何も取れないサイクルはエラー扱い (self-exit 対象)
                    consecutive_errors += 1
                    typer.echo(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] no datapoints")
            except Exception as exc:
                consecutive_errors += 1
                typer.echo(f"error: {exc}")
            if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                typer.echo(
                    f"{consecutive_errors} consecutive errors; exiting so systemd can restart the service"
                )
                raise typer.Exit(code=1)
            time.sleep(interval)
    except KeyboardInterrupt:
        typer.echo("stopped")


if __name__ == "__main__":
    app()
