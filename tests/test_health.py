"""健全性判定 (cli.health) のテスト。"""

import threading
import urllib.error
import urllib.request
from collections import namedtuple
from http.server import ThreadingHTTPServer

import pytest
import requests
import responses
from typer.testing import CliRunner

from cli import health
from cli.health import CachedHealth, Check

VM = "http://vm.test:8428"
NOW = 1_800_000_000
Usage = namedtuple("Usage", "total used free")


def fake_run(units_out="active\n" * 7, backup_out=f"Result=success\nExecMainExitTimestamp=@{NOW - 3600}\n"):
    calls = []

    def run(args):
        calls.append(list(args))
        if args[1] == "is-active":
            return units_out
        if args[1] == "show":
            return backup_out
        raise AssertionError(args)

    run.calls = calls
    return run


def vm_age(rsps, age):
    """全ての鮮度クエリに同じ age を返す。None なら系列なし。"""
    result = [] if age is None else [{"metric": {}, "value": [NOW, str(age)]}]
    rsps.get(f"{VM}/api/v1/query", json={"status": "success", "data": {"resultType": "vector", "result": result}})


# --- services -------------------------------------------------------------

def test_units_all_active():
    c = health.check_units(fake_run())
    assert c.ok and c.name == "services"


def test_units_reports_each_non_active_unit():
    out = "active\nactivating\nactive\nfailed\nactive\nactive\nactive\n"
    c = health.check_units(fake_run(units_out=out))
    assert not c.ok
    assert c.detail == "echonet.service=activating grafana-server.service=failed"


def test_units_short_output_counts_as_unknown():
    # systemctl が途中で落ちて行数が足りない場合も OK にしない
    c = health.check_units(fake_run(units_out="active\n"))
    assert not c.ok and "rpi-metrics.timer=unknown" in c.detail


# --- freshness ------------------------------------------------------------

@responses.activate
def test_age_within_limit_is_ok():
    vm_age(responses, 44.3)
    c = health.check_age("climate", VM, "q", 600, 1)
    assert c.ok and c.detail.startswith("44s")


@responses.activate
def test_age_over_limit_is_ng():
    vm_age(responses, 601)
    assert not health.check_age("climate", VM, "q", 600, 1).ok


@responses.activate
def test_age_in_future_is_ng():
    vm_age(responses, -121)
    assert not health.check_age("climate", VM, "q", 600, 1).ok


@responses.activate
def test_age_without_series_is_ng():
    vm_age(responses, None)
    c = health.check_age("power", VM, "q", 600, 1)
    assert not c.ok and c.detail == "データなし"


@responses.activate
def test_age_query_error_is_ng():
    responses.get(f"{VM}/api/v1/query", json={"status": "error", "error": "bad query"})
    c = health.check_age("power", VM, "q", 600, 1)
    assert not c.ok and "ValueError" in c.detail


@responses.activate
def test_age_vm_down_is_ng():
    responses.get(f"{VM}/api/v1/query", body=requests.ConnectionError("refused"))
    c = health.check_age("power", VM, "q", 600, 1)
    assert not c.ok and "ConnectionError" in c.detail


@responses.activate
def test_age_http_error_is_ng():
    responses.get(f"{VM}/api/v1/query", status=500)
    assert not health.check_age("power", VM, "q", 600, 1).ok


# --- backup ---------------------------------------------------------------

def test_backup_recent_success_is_ok():
    run = fake_run()
    c = health.check_backup(run, NOW)
    assert c.ok and c.detail == "1h 前に成功"
    assert "--timestamp=unix" in run.calls[0]


def test_backup_failed_result_is_ng():
    c = health.check_backup(fake_run(backup_out="Result=exit-code\nExecMainExitTimestamp=@1\n"), NOW)
    assert not c.ok and c.detail == "前回結果 exit-code"


def test_backup_too_old_is_ng():
    out = f"Result=success\nExecMainExitTimestamp=@{NOW - 37 * 3600}\n"
    assert not health.check_backup(fake_run(backup_out=out), NOW).ok


def test_backup_not_run_since_boot_is_ok():
    c = health.check_backup(fake_run(backup_out="Result=success\nExecMainExitTimestamp=\n"), NOW)
    assert c.ok and c.detail == "起動後未実行"


def test_backup_unreadable_is_ng():
    assert not health.check_backup(fake_run(backup_out=""), NOW).ok


# --- disk -----------------------------------------------------------------

def test_disk_below_limit_is_ok():
    assert health.check_disk(lambda _: Usage(100, 89, 11)).ok


def test_disk_at_limit_is_ng():
    c = health.check_disk(lambda _: Usage(100, 90, 10))
    assert not c.ok and c.detail.startswith("90%")


# --- collect / CLI --------------------------------------------------------

@responses.activate
def test_collect_runs_every_check():
    vm_age(responses, 30)
    checks = health.collect(VM, 1, run=fake_run(), usage=lambda _: Usage(100, 50, 50), now=lambda: NOW)
    assert [c.name for c in checks] == ["services", "climate", "power", "cost", "backup", "disk"]
    assert all(c.ok for c in checks)


def test_format_checks():
    text = health.format_checks([Check("disk", True, "50%"), Check("power", False, "データなし")])
    assert text.splitlines() == ["OK  disk      50%", "NG  power     データなし"]


@pytest.mark.parametrize("ok, code", [(True, 0), (False, 1)])
def test_check_command_exit_code(monkeypatch, ok, code):
    # health-check.sh はこの終了コードで OK/NG を判定する (以前は常に 0 で、異常を機械的に拾えなかった)
    monkeypatch.setattr(health, "collect", lambda *a, **k: [Check("services", True, "x"), Check("disk", ok, "y")])
    result = CliRunner().invoke(health.app, ["check"])
    assert result.exit_code == code
    assert "services" in result.output


# --- cache ----------------------------------------------------------------

def test_cache_reuses_result_within_ttl_and_logs_only_changes():
    t = [0.0]
    states = iter([True, True, False, False])
    calls, logs = [], []

    def compute():
        calls.append(1)
        return [Check("power", next(states), "d")]

    h = CachedHealth(compute, ttl_s=30, clock=lambda: t[0], log=logs.append)
    assert h.ok() and h.ok()          # 2 回目はキャッシュ
    assert len(calls) == 1
    t[0] = 30
    assert h.ok()                      # 再計算したが状態は同じ → ログなし
    t[0] = 60
    assert not h.ok()
    t[0] = 90
    assert not h.ok()
    assert len(calls) == 4
    assert logs == ["health OK", "health NG (power: d)"]


# --- HTTP -----------------------------------------------------------------

class StubHealth:
    def __init__(self, ok):
        self.value = ok

    def ok(self):
        return self.value


@pytest.fixture
def server():
    stub = StubHealth(True)
    srv = ThreadingHTTPServer(("127.0.0.1", 0), health.make_handler(stub))
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    yield stub, f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get(url, method="GET"):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method=method), timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def test_healthz_ok(server):
    stub, base = server
    assert _get(f"{base}/healthz") == (200, b"OK\n")


def test_healthz_ng_returns_503_without_details(server):
    stub, base = server
    stub.value = False
    assert _get(f"{base}/healthz?x=1") == (503, b"NG\n")


def test_healthz_head(server):
    _, base = server
    assert _get(f"{base}/healthz", method="HEAD") == (200, b"")


def test_other_paths_are_404(server):
    _, base = server
    assert _get(f"{base}/")[0] == 404
