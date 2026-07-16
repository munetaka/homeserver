"""料金モデルのテスト。実請求書2ヶ月分を回帰ベクタとして使う。"""

from datetime import date

import pytest

from cli import tariff


class TestBillingCalendar:
    @pytest.mark.parametrize("day,expected", [
        (date(2026, 6, 10), "2026-07"),   # 期間初日
        (date(2026, 7, 9), "2026-07"),    # 期間末日
        (date(2026, 7, 10), "2026-08"),   # 翌期間へ
        (date(2025, 12, 15), "2026-01"),  # 年またぎ
    ])
    def test_billing_month(self, day, expected):
        assert tariff.billing_month(day) == expected

    def test_billing_period_roundtrip(self):
        assert tariff.billing_period("2026-07") == (date(2026, 6, 10), date(2026, 7, 9))
        assert tariff.billing_period("2026-01") == (date(2025, 12, 10), date(2026, 1, 9))
        days = list(tariff.iter_billing_days("2026-07"))
        assert len(days) == 30
        assert all(tariff.billing_month(d) == "2026-07" for d in days)


class TestRealBills:
    """実請求書との照合 (税込・端数は請求書側で切り捨てられる)。"""

    def test_july_2026_bill(self):
        # 2026-07請求 (6/10-7/9): 326kWh -> ¥13,273 ちょうど
        assert tariff.period_bill_yen("2026-07", 326.0) == 13273

    def test_june_2026_bill(self):
        # 2026-06請求 (5/10-6/9): 281kWh -> ¥11,996 ちょうど (31日間でも基本料金は月額固定)
        assert tariff.period_bill_yen("2026-06", 281.0) == 11996

    def test_july_bill_breakdown_matches_statement(self):
        # 請求書の明細行と同じ内訳になること
        rates = tariff.rates_for("2026-07")
        assert rates.fuel == -7.19
        assert rates.levy == 4.18
        assert rates.subsidy == 0.0
        assert (326 - 120) * tariff.TIER_RATE_YEN == pytest.approx(6328.32)
        assert 326 * rates.fuel == pytest.approx(-2343.94)
        assert 326 * rates.levy == pytest.approx(1362.68, abs=0.01)


class TestRates:
    def test_marginal_rate_july(self):
        # 30.72 - 7.19 + 4.18 = 27.71
        assert tariff.marginal_buy_rate_yen("2026-07") == pytest.approx(27.71)

    def test_subsidy_applies_in_august(self):
        # 8月は燃調-10.27に加え軽減措置-3.5
        assert tariff.marginal_buy_rate_yen("2026-08") == pytest.approx(30.72 - 10.27 + 4.18 - 3.5)

    def test_levy_fiscal_year_boundary(self):
        assert tariff.rates_for("2026-04").levy == 3.98  # 2025年度分
        assert tariff.rates_for("2026-05").levy == 4.18  # 2026年度分

    def test_unknown_month_raises(self):
        with pytest.raises(KeyError):
            tariff.rates_for("2027-01")

    def test_savings_and_sell(self):
        assert tariff.savings_yen("2026-07", 278.9) == pytest.approx(278.9 * 27.71)
        assert tariff.sell_income_yen(277.3) == pytest.approx(4436.8)
