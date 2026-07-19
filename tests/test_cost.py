"""コストメトリクス生成のテスト。"""

import json
from datetime import date, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
import responses

from cli import tariff
from cli.cost import build_cost_lines, build_day_kwh_lines, day_ts_ms

VM = "http://vm.test:8428"


def _mock_stored_days(rsps, stored: dict):
    """query_range (実在判定) の応答を組み立てる。stored = {date: [kind, ...]}"""
    by_kind: dict = {}
    for day, kinds in stored.items():
        for kind in kinds:
            by_kind.setdefault(kind, []).append([day_ts_ms(day) // 1000, "1"])
    rsps.get(
        f"{VM}/api/v1/query_range",
        json={"data": {"result": [
            {"metric": {"kind": k}, "values": v} for k, v in by_kind.items()
        ]}},
    )


def _mock_counter_increase(rsps, per_day: dict):
    """instant query (増分) の応答。per_day = {(date, metric): kwh}。無いものは空応答。"""
    def callback(request):
        from datetime import datetime, timezone

        q = parse_qs(urlparse(request.url).query)
        t = int(q["time"][0])  # 「その日の翌日0時JST」の epoch 秒
        jst = timezone(timedelta(hours=9))
        day = (datetime.fromtimestamp(t, jst) - timedelta(days=1)).date()
        metric = q["query"][0].split("power_")[1].split("_total")[0]
        kwh = per_day.get((day, metric))
        result = [] if kwh is None else [{"metric": {}, "value": [t, str(kwh)]}]
        return (200, {}, json.dumps({"data": {"result": result}}))

    rsps.add_callback("GET", f"{VM}/api/v1/query", callback=callback)


class TestBuildDayKwhLines:
    @responses.activate
    def test_materializes_missing_day_and_derives_consumption(self):
        day = date(2026, 7, 13)
        with responses.RequestsMock() as rsps:
            _mock_stored_days(rsps, {})
            _mock_counter_increase(rsps, {
                (day, "generation"): 7.97, (day, "sell"): 0.57, (day, "buy"): 19.39})
            lines, warnings = build_day_kwh_lines(VM, day, day, timeout_s=5)
        assert warnings == []
        ts_ns = day_ts_ms(day) * 1_000_000
        assert f"energy_day,kind=buy kwh=19.39 {ts_ns}" in lines
        assert f"energy_day,kind=generation kwh=7.97 {ts_ns}" in lines
        assert f"energy_day,kind=sell kwh=0.57 {ts_ns}" in lines
        # consumption = 発電 + 買電 - 売電
        assert f"energy_day,kind=consumption kwh={round(7.97 + 19.39 - 0.57, 3)} {ts_ns}" in lines

    @responses.activate
    def test_skips_days_with_stored_samples(self):
        imported = date(2026, 7, 12)
        missing = date(2026, 7, 13)
        with responses.RequestsMock() as rsps:
            _mock_stored_days(rsps, {imported: ["buy", "consumption", "generation", "sell"]})
            _mock_counter_increase(rsps, {
                (missing, "generation"): 1.0, (missing, "sell"): 0.5, (missing, "buy"): 2.0})
            lines, warnings = build_day_kwh_lines(VM, imported, missing, timeout_s=5)
        assert warnings == []
        # インポート済みの 7/12 は書かず、7/13 だけ実体化される
        assert all(str(day_ts_ms(missing) * 1_000_000) in l for l in lines)
        assert len(lines) == 4

    @responses.activate
    def test_skips_day_with_partial_counter_data(self):
        day = date(2026, 7, 13)
        with responses.RequestsMock() as rsps:
            _mock_stored_days(rsps, {})
            _mock_counter_increase(rsps, {(day, "buy"): 2.0})  # 太陽光カウンター欠落
            lines, warnings = build_day_kwh_lines(VM, day, day, timeout_s=5)
        assert lines == []
        assert any("実体化をスキップ" in w for w in warnings)

    @responses.activate
    def test_day_before_live_collection_is_silently_skipped(self):
        day = date(2026, 6, 1)  # カウンター系列が存在しない過去日
        with responses.RequestsMock() as rsps:
            _mock_stored_days(rsps, {})
            _mock_counter_increase(rsps, {})
            lines, warnings = build_day_kwh_lines(VM, day, day, timeout_s=5)
        assert lines == []
        assert warnings == []

    @responses.activate
    def test_partial_stored_day_warns(self):
        day = date(2026, 7, 12)
        with responses.RequestsMock() as rsps:
            _mock_stored_days(rsps, {day: ["buy"]})
            lines, warnings = build_day_kwh_lines(VM, day, day, timeout_s=5)
        assert lines == []
        assert any("部分的" in w for w in warnings)


def _full_period_daily(month: str, buy_per_day=10.0, sell_per_day=9.0, gen_per_day=15.0):
    return {
        d: {"buy": buy_per_day, "sell": sell_per_day, "generation": gen_per_day}
        for d in tariff.iter_billing_days(month)
    }


class TestBuildCostLines:
    def test_daily_lines_use_marginal_rate(self):
        day = date(2026, 7, 1)  # 2026-07 請求月 (限界単価 27.71)
        lines, warnings = build_cost_lines(
            {day: {"buy": 10.0, "sell": 9.0, "generation": 15.0}}, today=date(2026, 7, 2))
        assert warnings == []
        joined = "\n".join(lines)
        assert f"cost_day,kind=buy yen={10.0 * 27.71}" in joined
        assert f"cost_day,kind=sell_income yen={9.0 * 16.0}" in joined
        assert f"cost_day,kind=savings yen={6.0 * 27.71}" in joined  # 自家消費 = 15-9
        assert str(day_ts_ms(day) * 1_000_000) in lines[0]

    def test_unknown_month_warns_and_skips(self):
        lines, warnings = build_cost_lines(
            {date(2027, 6, 1): {"buy": 10.0}}, today=date(2027, 6, 2))
        assert lines == []
        assert any("2027" in w for w in warnings)

    def test_completed_period_writes_bill(self):
        daily = _full_period_daily("2026-07")
        lines, warnings = build_cost_lines(daily, today=date(2026, 7, 20))
        assert warnings == []
        period_lines = [l for l in lines if l.startswith("cost_period")]
        assert len(period_lines) == 3
        total_buy = 10.0 * 30
        expected_bill = tariff.period_bill_yen("2026-07", total_buy)
        bill_line = next(l for l in period_lines if "kind=bill" in l)
        assert f"yen={expected_bill}" in bill_line
        assert "month=2026-07" in bill_line
        # 期間メトリクスのタイムスタンプは期間開始日 (6/10)
        assert str(day_ts_ms(date(2026, 6, 10)) * 1_000_000) in bill_line

    def test_incomplete_past_period_is_skipped(self):
        daily = _full_period_daily("2026-07")
        del daily[date(2026, 6, 20)]  # 1日欠け
        lines, warnings = build_cost_lines(daily, today=date(2026, 8, 1))
        assert not any(l.startswith("cost_period") for l in lines)
        assert any("期間集計をスキップ" in w for w in warnings)

    def test_ongoing_period_writes_partial_estimate(self):
        # 進行中の期間 (7/10〜) は途中でも見込みを書く
        today = date(2026, 7, 15)
        daily = {
            d: {"buy": 8.0, "sell": 10.0, "generation": 16.0}
            for d in [date(2026, 7, 10) + timedelta(days=i) for i in range(5)]
        }
        lines, _ = build_cost_lines(daily, today=today)
        bill_line = next(l for l in lines if "kind=bill" in l)
        assert "month=2026-08" in bill_line
        assert f"yen={tariff.period_bill_yen('2026-08', 40.0)}" in bill_line


class TestYearAggregation:
    def test_year_window_covers_jan1_sample_and_leap_years(self):
        from cli.cost import year_window_seconds
        assert year_window_seconds(2025) == 365 * 86400 + 3600
        assert year_window_seconds(2024) == 366 * 86400 + 3600  # うるう年

    @responses.activate
    def test_build_year_lines_queries_each_year_and_kind(self):
        from cli.cost import build_year_lines

        eval_2025 = day_ts_ms(date(2026, 1, 1)) // 1000  # 2025年分の評価点 = 翌年1/1

        def reply(request):
            qs = parse_qs(urlparse(request.url).query)
            q = qs["query"][0]
            is_2025 = int(qs["time"][0]) == eval_2025
            if is_2025:
                value = {"savings": "50000", "sell_income": "40000", "bill": "120000"}[
                    "savings" if "savings" in q else "sell_income" if "sell_income" in q else "bill"]
                body = {"status": "success", "data": {"result": [{"metric": {}, "value": [0, value]}]}}
            elif "bill" in q:  # 2026年は bill だけ返す想定
                body = {"status": "success", "data": {"result": [{"metric": {}, "value": [0, "13260"]}]}}
            else:
                body = {"status": "success", "data": {"result": []}}
            return (200, {}, json.dumps(body))

        responses.add_callback(responses.GET, f"{VM}/api/v1/query", callback=reply)
        lines = build_year_lines(VM, today=date(2026, 7, 16), timeout_s=5)
        assert "cost_year,kind=savings,year=2025 yen=50000.0" in " ".join(lines)
        assert "cost_year,kind=bill,year=2025 yen=120000.0" in " ".join(lines)
        assert "cost_year,kind=bill,year=2026 yen=13260.0" in " ".join(lines)
        # 2026 の savings/sell_income は結果なし → 行を書かない
        assert not any("kind=savings,year=2026" in l for l in lines)
        # タイムスタンプは各年の1/1 0時 (JST)
        jan1_2025_ns = day_ts_ms(date(2025, 1, 1)) * 1_000_000
        assert any(l.endswith(f" {jan1_2025_ns}") for l in lines if "year=2025" in l)
