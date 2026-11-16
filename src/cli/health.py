"""システムの健全性判定。

`health check` (scripts/health-check.sh が ssh 越しに呼ぶ) と `health serve` (外部監視用の
/healthz) が同じ判定を使う。/healthz は OK/NG しか返さない (詳細はここで外に出さない)。

判定項目:
  services  常駐サービスとタイマーが active
  climate   温湿度の最新サンプルが CLIMATE_MAX_AGE_S 以内 (watchdog の発火閾値と同じ)
  power     電力の最新サンプルが POWER_MAX_AGE_S 以内
  cost      コスト系列が COST_MAX_AGE_S 以内 (毎晩更新。昨日0時打点なので正常でも最大約48h)
  backup    vm-backup.service の前回結果が success で、BACKUP_MAX_AGE_S 以内に走っている
  disk      / の使用率が DISK_MAX_USED 未満
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, List, Optional, Sequence

import requests
import typer
from dotenv import load_dotenv

app = typer.Typer(add_completion=False, help="Home server health: checks shared by health-check.sh and /healthz")

SERVICES = ("switchbot.service", "echonet.service", "victoria-metrics.service", "grafana-server.service")
TIMERS = ("collector-watchdog.timer", "vm-backup.timer", "rpi-metrics.timer")
CLIMATE_MAX_AGE_S = 600
POWER_MAX_AGE_S = 600
COST_MAX_AGE_S = 60 * 3600
BACKUP_UNIT = "vm-backup.service"
BACKUP_MAX_AGE_S = 36 * 3600  # 毎晩 03:30 (+最大10分) なので 1 日半あれば必ず 1 回は走っている
DISK_MAX_USED = 0.90
CACHE_TTL_S = 30.0

Runner = Callable[[Sequence[str]], str]


@dataclass(frozen=True)
class Check:
    name: str
    ok: bool
    detail: str


def run_cmd(args: Sequence[str]) -> str:
    """コマンドの標準出力を返す。systemctl is-active は非 active で非 0 を返すので終了コードは見ない。"""
    return subprocess.run(list(args), capture_output=True, text=True, timeout=10, check=False).stdout


def check_units(run: Runner, units: Sequence[str] = SERVICES + TIMERS) -> Check:
    states = run(["systemctl", "is-active", *units]).split()
    states += ["unknown"] * (len(units) - len(states))
    bad = [f"{u}={s}" for u, s in zip(units, states) if s != "active"]
    if bad:
        return Check("services", False, " ".join(bad))
    return Check("services", True, f"{len(units)} units active")


def query_age(vm_url: str, expr: str, timeout_s: float) -> Optional[float]:
    """`time() - max(timestamp(...))` の結果 (秒)。系列が無ければ None。取得失敗は例外。"""
    r = requests.get(f"{vm_url}/api/v1/query", params={"query": expr}, timeout=timeout_s)
    r.raise_for_status()
    body = r.json()
    if body.get("status") != "success":
        raise ValueError(f"query failed: {body.get('error', body.get('status'))}")
    result = body["data"]["result"]
    if not result:
        return None
    return float(result[0]["value"][1])


def check_age(name: str, vm_url: str, expr: str, max_age_s: float, timeout_s: float) -> Check:
    try:
        age = query_age(vm_url, expr, timeout_s)
    except (requests.RequestException, ValueError, KeyError, IndexError) as e:
        return Check(name, False, f"取得失敗 ({type(e).__name__})")
    if age is None:
        return Check(name, False, "データなし")
    # 未来の時刻 (時計ずれ・誤データ) も異常として扱う (watchdog と同じ -120s の許容)
    ok = -120 <= age <= max_age_s
    return Check(name, ok, f"{int(age)}s (上限 {int(max_age_s)}s)")


def check_backup(run: Runner, now: float, max_age_s: float = BACKUP_MAX_AGE_S) -> Check:
    out = run(["systemctl", "show", BACKUP_UNIT, "-p", "Result", "-p", "ExecMainExitTimestamp", "--timestamp=unix"])
    props = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
    result = props.get("Result", "")
    if result != "success":
        return Check("backup", False, f"前回結果 {result or '不明'}")
    ts = props.get("ExecMainExitTimestamp", "").lstrip("@")
    if not ts.isdigit():
        # 起動後まだ一度も走っていない (再起動直後)。Result は既定値なので判断しない
        return Check("backup", True, "起動後未実行")
    age = now - int(ts)
    return Check("backup", age <= max_age_s, f"{int(age // 3600)}h 前に成功")


def check_disk(usage: Callable[[str], Any] = shutil.disk_usage, max_used: float = DISK_MAX_USED) -> Check:
    u = usage("/")
    used = u.used / u.total
    return Check("disk", used < max_used, f"{used:.0%} used (上限 {max_used:.0%})")


def collect(vm_url: str, timeout_s: float = 10, run: Runner = run_cmd,
            usage=shutil.disk_usage, now: Callable[[], float] = time.time) -> List[Check]:
    def age_expr(metric: str) -> str:
        return f"time() - max(timestamp({metric}[1h]))"

    return [
        check_units(run),
        check_age("climate", vm_url, age_expr("climate_temperature"), CLIMATE_MAX_AGE_S, timeout_s),
        check_age("power", vm_url, age_expr("power_generation_w"), POWER_MAX_AGE_S, timeout_s),
        check_age("cost", vm_url, "time() - max(timestamp(last_over_time(cost_day_yen[3d])))",
                  COST_MAX_AGE_S, timeout_s),
        check_backup(run, now()),
        check_disk(usage),
    ]


def format_checks(checks: Sequence[Check]) -> str:
    return "\n".join(f"{'OK' if c.ok else 'NG'}  {c.name:<9} {c.detail}" for c in checks)


class CachedHealth:
    """判定結果を CACHE_TTL_S だけ使い回す (連続アクセスで VM や systemctl を叩きすぎない)。
    OK/NG が変わったときだけ失敗項目をログに出す (journal に変化の記録を残す)。"""

    def __init__(self, compute: Callable[[], List[Check]], ttl_s: float = CACHE_TTL_S,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] = print):
        self._compute = compute
        self._ttl_s = ttl_s
        self._clock = clock
        self._log = log
        self._lock = threading.Lock()
        self._at: Optional[float] = None
        self._ok: Optional[bool] = None

    def ok(self) -> bool:
        with self._lock:
            now = self._clock()
            if self._at is None or now - self._at >= self._ttl_s:
                checks = self._compute()
                ok = all(c.ok for c in checks)
                if ok != self._ok:
                    failed = ", ".join(f"{c.name}: {c.detail}" for c in checks if not c.ok)
                    self._log(f"health {'OK' if ok else 'NG'}" + (f" ({failed})" if failed else ""))
                self._ok, self._at = ok, now
            return bool(self._ok)


def make_handler(health: CachedHealth) -> type[BaseHTTPRequestHandler]:
    class Handler(BaseHTTPRequestHandler):
        def _respond(self, with_body: bool) -> None:
            if self.path.split("?", 1)[0] != "/healthz":
                status, body = 404, b"not found\n"
            elif health.ok():
                status, body = 200, b"OK\n"
            else:
                status, body = 503, b"NG\n"
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            if with_body:
                self.wfile.write(body)

        def do_GET(self) -> None:  # noqa: N802 (http.server の命名規約)
            self._respond(with_body=True)

        def do_HEAD(self) -> None:  # noqa: N802
            self._respond(with_body=False)

        def log_message(self, format: str, *args) -> None:  # noqa: A002
            pass  # アクセスログは出さない (5分ごとの監視で journal が埋まるため)

    return Handler


def _env() -> tuple[str, float]:
    load_dotenv()
    return os.getenv("INFLUX_URL", "http://localhost:8428"), float(os.getenv("REQUEST_TIMEOUT_S", "10"))


@app.command()
def check() -> None:
    """判定結果を表示する。1 つでも NG なら終了コード 1。"""
    vm_url, timeout_s = _env()
    checks = collect(vm_url, timeout_s)
    typer.echo(format_checks(checks))
    raise typer.Exit(0 if all(c.ok for c in checks) else 1)


@app.command()
def serve(
    host: str = typer.Option("127.0.0.1", help="待ち受けアドレス (cloudflared が同じホストから繋ぐので localhost のみ)"),
    port: int = typer.Option(9199, help="待ち受けポート"),
) -> None:  # pragma: no cover - 常駐ループ (ハンドラと判定は個別にテストしている)
    """GET /healthz に 200 OK / 503 NG だけを返す。"""
    vm_url, timeout_s = _env()
    health = CachedHealth(lambda: collect(vm_url, timeout_s),
                          log=lambda m: print(m, file=sys.stderr, flush=True))
    server = ThreadingHTTPServer((host, port), make_handler(health))
    print(f"serving /healthz on {host}:{port}", file=sys.stderr, flush=True)
    server.serve_forever()
