"""コストメトリクス生成のテスト。"""

from datetime import date, timedelta

import pytest

from cli import tariff
from cli.cost import build_cost_lines, day_ts_ms


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
