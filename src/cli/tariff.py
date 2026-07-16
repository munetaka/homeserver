"""
電気料金モデル: 東京電力EP「くらし上手L」(10kVA) + FIT売電。

単価は全て税込・円/kWh。実請求書2ヶ月分(2026年6月請求¥11,996 / 7月請求¥13,273)を
1円未満の誤差で再現することをテストで担保している。

検針は毎月10日〆: 請求期間は「前月10日〜当月9日」で、燃料費調整単価は請求月のものを適用する。
単価改定時はこのファイルの定数を更新する(履歴が git に残る)。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

# --- くらし上手L (2026-07 請求書より) -------------------------------------
BASE_MONTHLY_YEN = 4257.0        # 基本料金 (10kVA, 検針期間の日数に依らず月額固定)
TIER_FIXED_YEN = 3670.40         # 定額料金 (最初の TIER_THRESHOLD_KWH まで)
TIER_THRESHOLD_KWH = 120.0
TIER_RATE_YEN = 30.72            # 120kWh 超過分の従量単価

# --- 売電 (2024年度FIT認定) -------------------------------------------------
FIT_SELL_YEN = 16.0

# --- 再エネ発電賦課金 (年度=5月分〜翌年4月分) -------------------------------
RENEWABLE_LEVY_YEN = {
    2025: 3.98,   # 2025-05〜2026-04 分
    2026: 4.18,   # 2026-05〜2027-04 分 (2026-07請求書で確認)
}

# --- 燃料費調整単価 (TEPCO公式一覧, 低圧・従量制, 税込) ---------------------
# https://www.tepco.co.jp/ep/private/fuelcost2/newlist/index-j.html (2026-07-15 取得)
FUEL_ADJUSTMENT_YEN = {
    "2025-06": -6.39, "2025-07": -6.88, "2025-08": -9.25, "2025-09": -9.90,
    "2025-10": -9.65, "2025-11": -7.65, "2025-12": -7.70,
    "2026-01": -7.72, "2026-02": -12.22, "2026-03": -12.09, "2026-04": -8.93,
    "2026-05": -7.37, "2026-06": -7.30, "2026-07": -7.19, "2026-08": -10.27,
}

# --- 国の負担軽減措置 (請求書の注記より。対象月のみ) -------------------------
SUBSIDY_YEN = {
    "2026-08": -3.5, "2026-09": -4.5, "2026-10": -3.5,
}


def billing_month(day: date) -> str:
    """日付が属する請求月 (10日〆)。6/10〜7/9 → "2026-07"。"""
    if day.day >= 10:
        y, m = (day.year + 1, 1) if day.month == 12 else (day.year, day.month + 1)
    else:
        y, m = day.year, day.month
    return f"{y:04d}-{m:02d}"


def billing_period(month: str) -> tuple[date, date]:
    """請求月 → (開始日, 終了日) = 前月10日〜当月9日。"""
    y, m = int(month[:4]), int(month[5:7])
    end = date(y, m, 9)
    py, pm = (y - 1, 12) if m == 1 else (y, m - 1)
    return date(py, pm, 10), end


def _levy_for(month: str) -> float:
    y, m = int(month[:4]), int(month[5:7])
    fiscal = y if m >= 5 else y - 1
    if fiscal not in RENEWABLE_LEVY_YEN:
        raise KeyError(f"再エネ賦課金が未登録の年度です: {fiscal} (tariff.py を更新してください)")
    return RENEWABLE_LEVY_YEN[fiscal]


@dataclass(frozen=True)
class MonthlyRates:
    """請求月ごとの kWh 単価内訳。"""

    fuel: float      # 燃料費調整 (負が普通)
    levy: float      # 再エネ賦課金
    subsidy: float   # 国の軽減措置 (対象月のみ負値)

    @property
    def per_kwh_extra(self) -> float:
        """従量単価に上乗せされる変動分の合計。"""
        return self.fuel + self.levy + self.subsidy


def rates_for(month: str) -> MonthlyRates:
    if month not in FUEL_ADJUSTMENT_YEN:
        raise KeyError(f"燃料費調整単価が未登録の月です: {month} (tariff.py を更新してください)")
    return MonthlyRates(
        fuel=FUEL_ADJUSTMENT_YEN[month],
        levy=_levy_for(month),
        subsidy=SUBSIDY_YEN.get(month, 0.0),
    )


def marginal_buy_rate_yen(month: str) -> float:
    """買電1kWhの限界単価 (月120kWh超で買っている前提 = この家では常に成立)。

    自家消費1kWhの節約額もこの単価で評価する。
    """
    return TIER_RATE_YEN + rates_for(month).per_kwh_extra


def period_bill_yen(month: str, total_buy_kwh: float) -> float:
    """請求期間の買電量合計から請求額(税込)を見積もる。

    TEPCOの請求書は「電力量料金」(定額+従量+燃調)と「再エネ賦課金」を
    それぞれ円未満切り捨ててから合算する(実請求書2ヶ月分で確認)。
    """
    import math

    rates = rates_for(month)
    tier = max(0.0, total_buy_kwh - TIER_THRESHOLD_KWH) * TIER_RATE_YEN
    energy_charge = TIER_FIXED_YEN + tier + total_buy_kwh * (rates.fuel + rates.subsidy)
    levy_charge = total_buy_kwh * rates.levy
    return BASE_MONTHLY_YEN + math.floor(energy_charge) + math.floor(levy_charge)


def savings_yen(month: str, self_consumption_kwh: float) -> float:
    """自家消費による節約額 (買わずに済んだ電力の限界単価評価)。"""
    return self_consumption_kwh * marginal_buy_rate_yen(month)


def sell_income_yen(sell_kwh: float) -> float:
    return sell_kwh * FIT_SELL_YEN


def iter_billing_days(month: str):
    """請求期間の日付を順に返す。"""
    start, end = billing_period(month)
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)
